#!/usr/bin/env python3
"""The Settings page shows this request's result, never the last one's
(issue #356).

The page writes `requested-action` and polls for the action's result
file; the last run's file was still there, so the first poll took it for
this run's and stopped. Now the page writes an id to `request-id` first,
the host (onionpress.handle_action, and the Linux launcher's
update_settings_page_check) echoes it as `request_id` in the result, and
the poll takes only a result carrying it. A result without one (a host
from before the id) counts only when written since the request; a page
that sent no id gets today's behaviour. The three sides must agree on the
id's charset and on which file answers which action.

The PHP half (the poll's rule, `onionpress_action_result_is_this_requests`)
runs under php where it is installed (CI); locally it is skipped without
php. The rest reads the sources.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

from onionpress import handle_action as ha  # noqa: E402

SETTINGS = os.path.join(ROOT, "app", "Resources", "plugins", "onionpress-settings.php")
LINUX = os.path.join(ROOT, "linux", "onionpress")
PHP = shutil.which("php")
HOST = "/var/lib/onionpress/"


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _function(src, name):
    m = re.search(r"^( *)function " + re.escape(name) + r"\(", src, re.M)
    assert m, f"no function {name}"
    end = src.index("\n" + m.group(1) + "}\n", m.end())
    return src[m.start():end + len(m.group(1)) + 2]


def _shell_function(src, name):
    m = re.search(r"^" + re.escape(name) + r"\(\) \{\n", src, re.M)
    assert m, f"no shell function {name}"
    return src[m.start():src.index("\n}\n", m.end()) + 3]


def _block(src, start, end):
    i = src.index(start)
    return src[i:src.index(end, i)]


class TestSources(unittest.TestCase):

    def setUp(self):
        self.php = _read(SETTINGS)
        self.request = _block(self.php, "    $file = '/var/lib/onionpress/requested-action';",
                              "\n} );\n")
        self.poll = _block(self.php, "add_action( 'wp_ajax_onionpress_poll_action'", "\n} );\n")

    def test_the_id_is_written_before_the_request(self):
        """The host looks for requested-action and reads request-id with
        it: the id must be there first."""
        r = self.request
        at = [r.index(s) for s in (
            "@unlink( $result_files[ $cmd ] );",
            "$request_id   = wp_generate_password( 16, false, false );",
            "$requested_at = time();",
            "@file_put_contents( '/var/lib/onionpress/request-id', $request_id )",
            "@file_put_contents( $file, $cmd )",
        )]
        self.assertEqual(at, sorted(at))
        # Alphanumeric only (no special characters, no extra specials):
        # what the host echoes.
        self.assertNotIn("wp_generate_password( 16, true", r)
        # Could not be written: no id, and the poll's fallback.
        self.assertIn("$request_id = '';", r)

    def test_the_notice_carries_the_id_and_the_time(self):
        self.assertIn("data-op-request-id=\"' . esc_attr( $request_id ) . '\"", self.request)
        self.assertIn("data-op-requested-at=\"' . esc_attr( (string) $requested_at ) . '\"",
                      self.request)
        js = _block(self.php, "var notice = document.querySelector('.op-action-notice');",
                    "}, 3000);")
        self.assertIn("notice.getAttribute('data-op-request-id')", js)
        self.assertIn("notice.getAttribute('data-op-requested-at')", js)
        self.assertIn("'&op_request_id=' + encodeURIComponent(requestId)", js)
        self.assertIn("'&op_requested_at=' + encodeURIComponent(requestedAt)", js)

    def test_the_poll_takes_only_this_requests_result(self):
        p = self.poll
        self.assertIn("preg_match( '/^[A-Za-z0-9_-]{1,64}$/D', $_GET['op_request_id'] )", p)
        self.assertIn("? (int) $_GET['op_requested_at'] : 0;", p)
        # Both the action results and the service result go through the rule.
        self.assertEqual(p.count("onionpress_action_result_is_this_requests("), 2)
        action = _block(p, "if ( isset( $result_files[ $action ] ) ) {", "wp_send_json( array( 'done' => false ) );")
        self.assertIn("if ( onionpress_action_result_is_this_requests( $result, $rf, $request_id, $requested_at ) ) {",
                      action)
        service = _block(p, "if ( file_exists( $service_result ) ) {", "if ( file_exists( $service_pending ) ) {")
        self.assertLess(service.index("@unlink( $service_result );"),
                        service.index("onionpress_action_result_is_this_requests( $result, $service_result,"))

    def test_the_three_sides_agree_on_the_id(self):
        pattern = ha.REQUEST_ID_RE.pattern
        self.assertEqual(pattern, "[A-Za-z0-9_-]{1,64}")
        self.assertIn(f"'/^{pattern}$/D'", self.poll)
        linux = _shell_function(_read(LINUX), "update_settings_page_put_result")
        self.assertIn(f're.fullmatch(r"{pattern}", rid)', linux)
        self.assertIn('os.environ.get("ONIONPRESS_REQUEST_ID", "")', linux)
        check = _shell_function(_read(LINUX), "update_settings_page_check")
        self.assertEqual(check.count("update_settings_page_put_result"), 2)
        self.assertNotIn("wp_put", check)
        self.assertEqual(ha.REQUEST_ID_FILE, "request-id")
        self.assertIn("'/var/lib/onionpress/request-id'", self.request)

    def test_the_page_and_the_host_agree_on_the_result_files(self):
        def files(block):
            m = re.search(r"\$result_files = array\((.*?)\);", block, re.S)
            return dict(re.findall(r"'([a-z-]+)'\s*=> '/var/lib/onionpress/([a-z-]+\.json)'", m.group(1)))
        self.assertEqual(files(self.request), ha.RESULT_FILES)
        polled = files(self.poll)
        self.assertEqual(polled, {k: v for k, v in ha.RESULT_FILES.items()
                                  if v != "service-result.json"})

    def test_the_host_echoes_the_id_centrally(self):
        src = _read(ha.__file__)
        write = _function_py(src, "write_result")
        self.assertIn('data = {**data, "request_id": self.request_id}', write)
        handle = _function_py(src, "handle")
        self.assertIn("host.request_id = checked_request_id(host.read_shared(REQUEST_ID_FILE), host.log)",
                      handle)
        self.assertLess(handle.index("host.request_id = checked_request_id("),
                        handle.index("host.remove_shared(*stale)"))
        self.assertLess(handle.index("host.remove_shared(*stale)"), handle.index("handlers = {"))


def _function_py(src, name):
    m = re.search(r"^( *)def " + re.escape(name) + r"\(", src, re.M)
    assert m, f"no def {name}"
    nxt = re.search(r"^" + m.group(1) + r"(?:def |class |# --)", src[m.end():], re.M)
    return src[m.start():m.end() + (nxt.start() if nxt else len(src))]


HARNESS = r"""<?php
%(function)s
$in = json_decode( file_get_contents( 'php://stdin' ), true );
$out = array();
foreach ( $in['cases'] as $case ) {
    $file = $in['dir'] . '/' . $case['name'];
    if ( null !== $case['mtime'] ) {
        file_put_contents( $file, 'x' );
        touch( $file, (int) $case['mtime'] );
    }
    $out[] = onionpress_action_result_is_this_requests(
        $case['result'], $file, $case['request_id'], $case['requested_at'] );
}
echo json_encode( $out );
"""


@unittest.skipUnless(PHP, "php not installed")
class TestRuleUnderPhp(unittest.TestCase):

    def run_cases(self, cases):
        tmp = tempfile.mkdtemp(prefix="onionpress-settings-results-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        harness = os.path.join(tmp, "harness.php")
        with open(harness, "w") as f:
            f.write(HARNESS % {"function": _function(_read(SETTINGS),
                                                     "onionpress_action_result_is_this_requests")})
        for i, c in enumerate(cases):
            c["name"] = f"result-{i}.json"
        proc = subprocess.run(
            [PHP, "-d", "error_reporting=E_ALL", "-d", "display_errors=stderr", harness],
            input=json.dumps({"dir": tmp, "cases": cases}), capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertEqual(proc.stderr, "")
        return json.loads(proc.stdout)

    def test_the_rule(self):
        now = int(time.time())
        cases = [
            # (result, file mtime, the page's id, requested_at) -> taken?
            ({"success": True, "request_id": "abc"}, now - 100, "abc", now, True),
            ({"success": True, "request_id": "old"}, now + 100, "abc", now, False),
            ({"success": True}, now - 100, "abc", now, False),           # older host: stale
            ({"success": True}, now + 5, "abc", now, True),              # older host: written since
            ({"success": True}, now, "abc", now, True),                  # same second counts
            ({"success": True, "request_id": "old"}, now - 100, "", 0, True),   # page without id: as before
            ({"success": True, "request_id": "old"}, now - 100, "", now, False),  # ...but never older
            (None, now - 100, "abc", now, False),                        # unreadable result: by time
            (None, now + 5, "abc", now, True),
            ({"request_id": 7}, now - 100, "abc", now, False),           # not a string: no id
            ({"success": True, "request_id": "abc"}, None, "abc", now, True),   # no file: the id decides
            ({"success": True}, None, "abc", now, True),                 # no file to date: taken
        ]
        got = self.run_cases([{"result": r, "mtime": m, "request_id": rid, "requested_at": at}
                              for r, m, rid, at, _ in cases])
        self.assertEqual(got, [want for *_, want in cases])


if __name__ == "__main__":
    unittest.main()

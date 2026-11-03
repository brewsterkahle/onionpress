#!/usr/bin/env python3
"""The Settings page's copies of host constants, held to their Python
source (issue #528, item 4), as tests/test_backup_download.py does for the
backup file name. The page is PHP in the WordPress image; the host is
Python; neither reads the other at run time, so each literal the page
repeats is checked here:

  - the fields (onionpress_settings_fields): every key the page sends is
    in config.SETTINGS_PAGE_KEYS, every read-only one is not, a select's
    options are values the host accepts, a number's min and max are the
    host's bounds;
  - the Cloudflare token pattern (config.CLOUDFLARE_TOKEN_PATTERN);
  - the keys that take a restart ($restart_keys: config.NEEDS_RESTART
    within the page's keys, the held token aside);
  - the actions and their result files (handle_action.ACTIONS,
    RESULT_FILES), in the request handler and in the poll;
  - the request id's shape (handle_action.REQUEST_ID_RE);
  - the v3 onion address's shape, everywhere the image's PHP checks one;

and what the page no longer does (items 1, 2, 5, 6): ask GitHub, touch a
file the daemon fished for with `docker exec`, write config-current.json,
or (the status page) write requested-action.
"""

import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from onionpress import config, handle_action, redact, status_writer  # noqa: E402

SETTINGS = os.path.join(ROOT, "app", "Resources", "plugins", "onionpress-settings.php")
STATUS_PAGE = os.path.join(ROOT, "app", "Resources", "plugins", "onionpress-status.php")
RESOURCES = os.path.join(ROOT, "app", "Resources")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _function(src, name):
    start = src.index(f"function {name}(")
    return src[start:src.index("\n}\n", start)]


def _fields():
    """{key: {readonly, secret, options, min, max, config_keys}} from the
    page's onionpress_settings_fields()."""
    body = _function(_read(SETTINGS), "onionpress_settings_fields")
    blocks = re.split(r"\n        '([A-Z_]+)' => array\(", body)[1:]
    fields = {}
    for key, block in zip(blocks[::2], blocks[1::2]):
        options = re.search(r"'options'\s*=>\s*array\((.*?)\),\n", block, re.S)
        config_key = re.search(r"'config_key'\s*=>\s*(.*?),\n", block)
        low = re.search(r"'min'\s*=>\s*(\d+)", block)
        high = re.search(r"'max'\s*=>\s*(\d+)", block)
        fields[key] = {
            "readonly": re.search(r"'readonly'\s*=>\s*true", block) is not None,
            "secret": re.search(r"'type'\s*=>\s*'secret'", block) is not None,
            "options": set(re.findall(r"'([^']*)'\s*=>", options.group(1))) if options else None,
            "min": int(low.group(1)) if low else None,
            "max": int(high.group(1)) if high else None,
            "config_keys": (set(re.findall(r"'([A-Z_]+)'", config_key.group(1)))
                            if config_key else {key}),
        }
    return fields


class TestFields(unittest.TestCase):

    def test_the_fields_were_found(self):
        self.assertGreaterEqual(len(_fields()), 8)

    def test_every_key_the_page_sends_is_the_hosts(self):
        for key, field in _fields().items():
            with self.subTest(key=key):
                if field["readonly"]:
                    self.assertFalse(field["config_keys"] & set(config.SETTINGS_PAGE_KEYS),
                                     "a read-only field the host would take")
                    continue
                self.assertLessEqual(field["config_keys"], set(config.SETTINGS_PAGE_KEYS))

    def test_options_and_bounds_are_the_hosts(self):
        for _key, field in _fields().items():
            if field["readonly"] or field["secret"]:
                continue
            for config_key in field["config_keys"]:
                check = config.SETTINGS_PAGE_KEYS[config_key]
                with self.subTest(key=config_key):
                    if check.allowed is not None:
                        self.assertIsNotNone(field["options"], "a free field for a choice")
                        self.assertLessEqual(field["options"], check.allowed)
                    if check.bounds is not None:
                        self.assertEqual((field["min"], field["max"]), check.bounds)
                    else:
                        self.assertIsNone(field["min"])

    def test_the_token_pattern(self):
        self.assertIn(f"preg_match( '#^{config.CLOUDFLARE_TOKEN_PATTERN}$#D', $cf_token )",
                      _read(SETTINGS))

    def test_the_restart_keys(self):
        m = re.search(r"\$restart_keys = array\( (.*?) \);", _read(SETTINGS))
        self.assertEqual(set(re.findall(r"'([A-Z_]+)'", m.group(1))),
                         (config.NEEDS_RESTART & set(config.SETTINGS_PAGE_KEYS))
                         - config.PAGE_HELD_KEYS)


class TestActions(unittest.TestCase):

    def setUp(self):
        self.src = _read(SETTINGS)

    def maps(self):
        """Every `$result_files = array( … )` in the page, as dicts."""
        found = []
        for m in re.finditer(r"\$result_files = array\((.*?)\);", self.src, re.S):
            found.append(dict(re.findall(
                r"'([a-z-]+)'\s*=>\s*'/var/lib/onionpress/([a-z-]+\.json)'", m.group(1))))
        return found

    def test_the_result_files_are_handle_actions(self):
        request, poll = self.maps()
        self.assertEqual(request, handle_action.RESULT_FILES)
        # The poll reads the service result itself (it is read once).
        self.assertEqual(poll, {a: f for a, f in handle_action.RESULT_FILES.items()
                                if f != "service-result.json"})

    def test_every_action_the_page_sends_is_a_verb(self):
        m = re.search(r"if \( ! in_array\( \$action, array\( (.*?) \), true \) \)", self.src)
        sent = set(re.findall(r"'([a-z-]+)'", m.group(1)))
        cmd_map = re.search(r"\$cmd_map = array\((.*?)\);", self.src, re.S)
        mapped = dict(re.findall(r"'([a-z-]+)' => '([a-z-]+)'", cmd_map.group(1)))
        self.assertEqual(set(mapped), sent)
        self.assertLessEqual(set(mapped.values()), set(handle_action.ACTIONS))

    def test_the_request_id_shape(self):
        self.assertIn(f"preg_match( '/^{handle_action.REQUEST_ID_RE.pattern}$/D', "
                      "$_GET['op_request_id'] )", self.src)


class TestOnionAddressShape(unittest.TestCase):
    """Every check of a v3 address in the image's PHP (and the page's JS)
    is the one shape: 56 base32 characters and .onion."""

    def test_one_shape(self):
        seen = 0
        for folder, _dirs, files in os.walk(RESOURCES):
            for name in files:
                if not name.endswith(".php"):
                    continue
                src = _read(os.path.join(folder, name))
                for m in re.finditer(r"\[a-z2-7\][^\\]*\\\.onion", src):
                    seen += 1
                    with self.subTest(file=name, at=m.start()):
                        self.assertEqual(m.group(0), r"[a-z2-7]{56}\.onion")
        self.assertGreaterEqual(seen, 20)


class TestWhatThePageNoLongerDoes(unittest.TestCase):

    def test_no_release_lookup_of_its_own(self):
        """Item 1: the release line is status.json's "release", the host's
        last check (handle_action.check_release's cache)."""
        src = _read(SETTINGS)
        self.assertNotIn("api.github.com", src)
        self.assertNotIn("onionpress_latest_release", src)
        body = src[src.index("add_action( 'wp_ajax_onionpress_check_update'"):]
        body = body[:body.index("\n} );")]
        self.assertIn("$status['release']", body)
        self.assertNotIn("onionpress_curl_tor", body)
        self.assertEqual(status_writer.RELEASE_CACHE, handle_action.UPDATE_CHECK_CACHE)
        self.assertIn("release", redact.WORDPRESS_STATUS_KEYS)

    def test_share_now_is_a_request(self):
        """Item 2: no REST route touching a file in the volume."""
        src = _read(SETTINGS)
        self.assertNotIn("'/upload-analytics'", src)
        self.assertNotIn(".upload-analytics", src)
        self.assertIn("['onionpress_action', 'upload-analytics']", src)

    def test_config_current_has_one_writer(self):
        """Item 5: the host (status_writer) writes config-current.json."""
        for path in (SETTINGS, STATUS_PAGE):
            src = _read(path)
            with self.subTest(os.path.basename(path)):
                self.assertNotRegex(src, r"file_put_contents\( \$config_file")
                self.assertNotRegex(src, r"file_put_contents\([^;]*config-current\.json")
        self.assertIn("function onionpress_settings_shown_config()", _read(SETTINGS))

    def test_the_status_page_asks_for_nothing(self):
        """Item 6: no requested-action from the status page."""
        src = _read(STATUS_PAGE)
        self.assertNotIn("requested-action", src)
        self.assertNotIn("file_put_contents", src)

    def test_no_security_report_file(self):
        """Item 6: nothing read security-report.txt."""
        audit = _read(os.path.join(RESOURCES, "docker", "wordpress",
                                   "onionpress-security-audit.sh"))
        self.assertNotIn("report_tmp", audit)
        self.assertIn('rm -f "$OLD_REPORT"', audit)



class TestArchiveLoginIsTheHosts(unittest.TestCase):
    """Item 3 (issue #528, Ivar's decision): the page's archive.org login
    is handle-action's archive-login, the host's one login over Tor."""

    def setUp(self):
        self.src = _read(SETTINGS)

    def test_no_login_in_wordpress(self):
        for gone in ("xauthn", "onionpress_fetch_ia_s3_keys", "onionpress_ia_request",
                     "onionpress_curl_tor", "curl_init", "s3.php"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, self.src)

    def test_the_request(self):
        fn = _function(self.src, "onionpress_request_archive_login")
        # Only to a host that takes the verb, so the file is never left.
        self.assertIn("in_array( 'archive-login', $status['page_actions'], true )", fn)
        self.assertLess(fn.index("page_actions"), fn.index("file_put_contents"))
        # 0600 from the first byte, then the request.
        write = fn.index("file_put_contents( $dir . '/" + handle_action.ARCHIVE_LOGIN_FILE + "',")
        self.assertLess(fn.index("umask( 077 )"), write)
        self.assertLess(write, fn.index("'/requested-action', 'archive-login'"))
        self.assertIn("'/archive-login-result.json'", fn)
        self.assertEqual(handle_action.RESULT_FILES["archive-login"], "archive-login-result.json")
        self.assertIn("archive-login", status_writer.PAGE_ACTIONS)
        self.assertIn("page_actions", redact.WORDPRESS_STATUS_KEYS)

    def test_asked_from_the_owner_gated_save(self):
        save = self.src[self.src.index("// Archive.org credentials: the host logs in"):]
        save = save[:save.index("// Check if a restart is actually needed")]
        self.assertIn("onionpress_request_archive_login( $ia_account, $ia_password )", save)
        self.assertIn('data-op-action="archive-login"', save)
        # The save handler is the owner's and checks its nonce first.
        handler = self.src[:self.src.index("// Archive.org credentials: the host logs in")]
        handler = handler[handler.rindex("add_action( 'admin_init'"):]
        self.assertIn("onionpress_settings_owner_allowed()", handler)
        self.assertIn("wp_verify_nonce", handler)


if __name__ == "__main__":
    unittest.main()

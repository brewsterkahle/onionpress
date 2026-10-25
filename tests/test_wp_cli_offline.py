#!/usr/bin/env python3
"""The host's provisioning wp-cli calls make no outbound request (issue #373).

`onionpress provision-post-install` (multisite.provision_post_install, run
from both launchers' start) failed with subprocess.TimeoutExpired on its
`wp theme list` whenever Tor was down: wp-cli loads WordPress, `wp theme
list` runs wp_update_themes(), the onionpress-tor-proxy mu-plugin sends
that through Tor, and with Tor down the request waited for the proxy's
timeout until the host's call expired. The start then reported "START
SUMMARY: failed components: provision-post-install" although the site was
up. Same for setup-bundled-plugins' calls.

So wp_files.wp_cli_args(offline=True) sets ONIONPRESS_WP_OFFLINE=1 in the
wp-cli process's environment, and the mu-plugin's first pre_http_request
filter answers every request to a host outside Docker with a WP_Error
before the SOCKS probe or anything else runs. multisite._wp passes it by
default (nothing there needs the network); other callers (the setup's
`wp language core install`, which downloads) are unchanged.

The mu-plugin is installed from app/Resources/plugins by
provision-post-install itself (MU_PLUGINS), before the first call that
runs an update check, so this ships with the app, not the image.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))
sys.path.insert(0, os.path.dirname(__file__))

from onionpress import multisite, wp_files  # noqa: E402
import test_no_clearnet_dns as dns_test  # noqa: E402

TOR_PROXY = os.path.join(PROJECT_ROOT, "app", "Resources", "plugins",
                         "onionpress-tor-proxy.php")
ENV_FLAG = f"{wp_files.OFFLINE_ENV}=1"


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _completed(stdout="", returncode=0):
    return subprocess.CompletedProcess(["docker"], returncode, stdout, "")


class TestWpCliArgs(unittest.TestCase):

    def test_offline_adds_the_variable_after_the_cache_dir(self):
        self.assertEqual(
            wp_files.wp_cli_args("theme", "list", offline=True),
            ["exec", "-u", "www-data", "-e", "WP_CLI_CACHE_DIR=/tmp/wp-cli-cache",
             "-e", ENV_FLAG, "onionpress-wordpress", "wp", "theme", "list"])
        self.assertEqual(
            wp_files.wp_cli_args("option", "update", "k", stdin=True, offline=True),
            ["exec", "-i", "-u", "www-data", "-e", "WP_CLI_CACHE_DIR=/tmp/wp-cli-cache",
             "-e", ENV_FLAG, "onionpress-wordpress", "wp", "option", "update", "k"])

    def test_default_is_unchanged(self):
        """The launchers' wp_cli copies and every other caller: no variable."""
        args = wp_files.wp_cli_args("core", "is-installed")
        self.assertNotIn(ENV_FLAG, args)
        self.assertNotIn(wp_files.OFFLINE_ENV, " ".join(args))


class TestMultisiteCallsAreOffline(unittest.TestCase):

    def _argvs(self, fn, *args, **kwargs):
        with mock.patch.object(multisite.subprocess, "run",
                               return_value=_completed()) as run:
            fn(*args, **kwargs)
        return [c.args[0] for c in run.call_args_list]

    def test_wp_passes_offline_by_default(self):
        argvs = self._argvs(multisite._wp, "theme", "list", docker_bin="docker")
        self.assertEqual(argvs, [["docker"] + wp_files.wp_cli_args("theme", "list", offline=True)])

    def test_wp_can_be_asked_not_to(self):
        argvs = self._argvs(multisite._wp, "x", docker_bin="docker", offline=False)
        self.assertEqual(argvs, [["docker"] + wp_files.wp_cli_args("x")])

    def test_every_wp_cli_call_of_the_provisioning_is_offline(self):
        """provision_post_install, and the plugin setup's steps, against a
        docker that answers everything: each `wp` argv carries the flag."""
        tmp = tempfile.mkdtemp(prefix="onionpress-offline-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        themes = os.path.join(tmp, "themes", "onionpress")
        os.makedirs(themes)
        with open(os.path.join(themes, "style.css"), "w") as f:
            f.write("/* Theme Name: OnionPress */\n")
        plugins = os.path.join(PROJECT_ROOT, "app", "Resources", "plugins")

        def fake_run(argv, **kwargs):
            if "wp" in argv and argv[argv.index("wp") + 1:][:2] == ["theme", "list"]:
                return _completed("twentytwentyfour\n")
            return _completed()

        with mock.patch.object(multisite.subprocess, "run", side_effect=fake_run) as run, \
             mock.patch.object(wp_files.subprocess, "run", side_effect=fake_run) as run2:
            multisite.deactivate_wp_statistics(docker_bin="docker")
            multisite.provision_post_install(themes_dir=os.path.dirname(themes),
                                             plugins_dir=plugins, docker_bin="docker")
            # setup_bundled_plugins' Python steps.
            multisite.configure_cache_enabler(docker_bin="docker")
            multisite.apply_cache_policy(docker_bin="docker")
            multisite.configure_ia_plugin(docker_bin="docker")
        wp_calls = [c.args[0] for c in run.call_args_list + run2.call_args_list
                    if "wp" in c.args[0]]
        self.assertTrue(wp_calls)
        for argv in wp_calls:
            with self.subTest(argv=argv):
                self.assertIn("-e", argv)
                self.assertIn(ENV_FLAG, argv[:argv.index("wp")])


class TestMuPluginReadsTheSameName(unittest.TestCase):

    def setUp(self):
        self.src = _read(TOR_PROXY)

    def test_the_variable_name_is_the_mu_plugins(self):
        self.assertIn(f"'1' !== getenv( '{wp_files.OFFLINE_ENV}' )", self.src)
        # ...and nothing else reads an ONIONPRESS_* variable there.
        self.assertEqual(re.findall(r"getenv\( '([A-Z_]+)' \)", self.src),
                         [wp_files.OFFLINE_ENV])

    def test_the_filter_runs_first(self):
        """Before the SOCKS probe (priority 10): with the variable set no
        socket is opened, and the later filters hand a set $pre on."""
        self.assertIn("add_filter( 'pre_http_request', 'onionpress_tor_proxy_offline', 1, 3 );",
                      self.src)
        for prio in re.findall(r"add_filter\( 'pre_http_request', [^;]*?, (\d+), 3 \);", self.src):
            self.assertGreaterEqual(int(prio), 1)
        fn = dns_test._function(self.src, "onionpress_tor_proxy_offline")
        self.assertIn("onionpress_is_local_host( $host )", fn)
        self.assertNotIn("fsockopen", fn)
        self.assertNotIn("onionpress_socks_proxy_for", fn)
        # The proxy's own filters do nothing once a request is answered.
        probe = self.src[self.src.index("add_filter( 'pre_http_request', function ( $pre, $args, $url ) {"):]
        probe = probe[:probe.index("}, 10, 3 );")]
        self.assertIn("if ( false !== $pre ) {", probe)
        check = dns_test._function(self.src, "onionpress_tor_proxy_check_url")
        self.assertIn("false !== $pre ||", check)


# test_no_clearnet_dns's harness: WordPress's request path and hooks, the
# plugin loaded, PHP's lookups disabled and recording. Here the SOCKS probe
# records too, and an "env" case sets a variable for the cases after it.
_SOCKS_STUB = "    return fopen( 'php://memory', 'r' );\n}\n"
_OUT_LINE = "    $out[] = array( 'result' => $result, 'dns' => $GLOBALS['op_dns'] );\n"
_FIRST_KIND = "    if ( 'safe' === $kind || 'plain' === $kind ) {\n"


def _harness():
    src = dns_test.HARNESS
    for needle in (_SOCKS_STUB, _OUT_LINE, _FIRST_KIND, "$GLOBALS['op_dns'] = array();\n"):
        assert needle in src, needle
    src = src.replace(_SOCKS_STUB,
                      "    $GLOBALS['op_socks'][] = $host;\n" + _SOCKS_STUB)
    src = src.replace(_OUT_LINE,
                      "    $out[] = array( 'result' => $result, 'dns' => $GLOBALS['op_dns'],"
                      " 'socks' => $GLOBALS['op_socks'] );\n")
    src = src.replace("    $GLOBALS['op_dns'] = array();\n",
                      "    $GLOBALS['op_dns'] = array();\n    $GLOBALS['op_socks'] = array();\n")
    src = src.replace(_FIRST_KIND,
                      "    if ( 'env' === $kind ) {\n"
                      "        putenv( $case[1] );\n"
                      "        $result = 'set';\n"
                      "    } elseif ( 'safe' === $kind || 'plain' === $kind ) {\n")
    return src


@unittest.skipUnless(dns_test.PHP, "php not installed")
class TestOfflineUnderPhp(unittest.TestCase):

    def _run(self, cases):
        tmp = tempfile.mkdtemp(prefix="onionpress-wp-offline-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        harness = os.path.join(tmp, "harness.php")
        with open(harness, "w") as f:
            f.write(_harness() % {"onion": json.dumps(dns_test.ONION),
                                  "plugin": json.dumps(TOR_PROXY)})
        proc = subprocess.run(
            [dns_test.PHP, "-d", "error_reporting=E_ALL", "-d", "display_errors=stderr",
             "-d", "disable_functions=" + ",".join(dns_test.LOOKUPS + ("fsockopen", "pfsockopen")),
             harness],
            input=json.dumps({"cases": cases, "external": False, "ports": []}),
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "", proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(len(out), len(cases))
        return out

    REMOTE = ["https://api.wordpress.org/themes/update-check/1.1/",
              "https://example.com/feed/",
              f"http://{dns_test.OTHER_ONION}/feed/"]
    LOCAL = ["http://localhost/wp-cron.php", "http://wordpress/wp-cron.php"]

    def test_without_the_variable_the_proxy_probes_as_before(self):
        out = self._run([["safe", u] for u in self.REMOTE])
        # onionpress_socks_proxy_for() probes a daemon once per process and
        # answers later requests for it from its cache: the update host asks
        # onionheaven first, the other two onionpress-tor, so the .onion
        # (the second onionpress-tor request) is served without a probe.
        expected = [["onionheaven"], ["onionpress-tor"], []]
        for url, r, probes in zip(self.REMOTE, out, expected):
            with self.subTest(url=url):
                self.assertEqual(r["result"], "sent")
                self.assertEqual(r["dns"], [])
                self.assertEqual(r["socks"], probes)

    def test_with_the_variable_nothing_leaves(self):
        cases = [["env", ENV_FLAG]]
        cases += [["safe", u] for u in self.REMOTE] + [["plain", u] for u in self.REMOTE]
        cases += [["plain", u] for u in self.LOCAL]
        out = self._run(cases)
        self.assertEqual(out[0]["result"], "set")
        for case, r in zip(cases[1:1 + 2 * len(self.REMOTE)], out[1:1 + 2 * len(self.REMOTE)]):
            with self.subTest(case=case):
                self.assertEqual(r, {"result": "error:onionpress_offline", "dns": [], "socks": []})
        for case, r in zip(cases[-len(self.LOCAL):], out[-len(self.LOCAL):]):
            with self.subTest(case=case):
                self.assertEqual(r["result"], "sent")
                self.assertEqual(r["socks"], [])

    def test_only_the_value_1_counts(self):
        out = self._run([["env", f"{wp_files.OFFLINE_ENV}=yes"], ["safe", self.REMOTE[1]],
                         ["env", wp_files.OFFLINE_ENV], ["safe", self.REMOTE[1]]])
        self.assertEqual([r["result"] for r in out], ["set", "sent", "set", "sent"])


if __name__ == "__main__":
    unittest.main()

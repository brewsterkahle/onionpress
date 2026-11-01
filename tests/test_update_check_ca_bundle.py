#!/usr/bin/env python3
"""The update check runs where the CA bundle is not macOS's (issue #355).

updater.check_for_update ran `curl --cacert /etc/ssl/cert.pem`: that is
macOS's bundle, Debian/Ubuntu/Raspberry Pi OS keep theirs at
/etc/ssl/certs/ca-certificates.crt, so on Linux curl failed before
connecting (exit 77). check-for-update --json still exited 0 with `error`
set and update_available false, and the tray's Check for Updates… showed
"Up to date". Now --cacert is passed only where the file exists (both curl
calls), and the tray reads `error` as "Update check failed".
"""

import ast
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from typing import NamedTuple, Optional
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from onionpress import cli, updater  # noqa: E402

TRAY = os.path.join(PROJECT_ROOT, "linux", "onionpress-tray")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class TestCurlCaBundle(unittest.TestCase):

    def _check(self, bundle):
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, json.dumps({"tag_name": "v0.0.1"}), "")

        with mock.patch.object(updater, "CA_BUNDLE", bundle), \
                mock.patch.object(updater.subprocess, "run", fake_run):
            self.assertIsNone(updater.check_for_update("1.0.0", log=lambda m: None))
        self.assertEqual(len(calls), 1)
        return calls[0]

    def test_no_cacert_when_the_mac_bundle_is_missing(self):
        argv = self._check(os.path.join(tempfile.gettempdir(), "no-such-cert.pem"))
        self.assertEqual(argv[0], "curl")
        self.assertNotIn("--cacert", argv)
        self.assertIn("https://api.github.com/repos/brewsterkahle/onionpress/releases/latest", argv)

    def test_cacert_when_the_bundle_exists(self):
        with tempfile.NamedTemporaryFile(suffix=".pem") as f:
            argv = self._check(f.name)
            self.assertEqual(argv[argv.index("--cacert") + 1], f.name)

    def test_every_curl_call_goes_through_the_helper(self):
        tree = ast.parse(_read(os.path.join(PROJECT_ROOT, "src/onionpress/updater.py")))
        curls = []
        for node in ast.walk(tree):
            if isinstance(node, ast.List) and node.elts and isinstance(node.elts[0], ast.Constant) \
                    and node.elts[0].value == "curl":
                curls.append(node)
        # The release check, the DMG, and the release's SHA256SUMS (#386).
        self.assertEqual(len(curls), 3)
        for lst in curls:
            names = [e.value.func.id for e in lst.elts
                     if isinstance(e, ast.Starred) and isinstance(e.value, ast.Call)]
            self.assertEqual(names, ["_curl_tls_args"])
            self.assertNotIn("--cacert", [getattr(e, "value", None) for e in lst.elts])
        self.assertEqual(updater.CA_BUNDLE, "/etc/ssl/cert.pem")


class TestCheckForUpdateReportSaysError(unittest.TestCase):
    """The JSON contract the UIs read: exit 0, `error` set, update_available
    false. A consumer must look at `error`, never take a 0 for up to date."""

    def test_a_curl_failure_is_the_error_field(self):
        def failing(current, log=None):
            log("Update check curl failed: exit=77 stderr=error setting certificate file")
            return None

        out = io.StringIO()
        with mock.patch.object(updater, "check_for_update", failing), \
                mock.patch.object(updater, "detect_other_instances", return_value=[]), \
                contextlib.redirect_stdout(out):
            rc = cli.check_for_update_report("1.2.3", lambda m: None, json_output=True)
        self.assertEqual(rc, 0)
        report = json.loads(out.getvalue())
        self.assertIn("curl failed", report["error"])
        self.assertFalse(report["update_available"])
        self.assertIsNone(report["latest"])


class TestTrayOutcome(unittest.TestCase):
    """ui_client.update_outcome (#526): what the tray's notification and
    the menubar's dialog say about one check's answer."""

    def setUp(self):
        from onionpress import ui_client
        self.outcome = ui_client.update_outcome

    def _report(self, **fields):
        report = {"current": "2.5.6", "latest": "2.5.6", "update_available": False,
                  "release_url": None, "error": None}
        report.update(fields)
        return json.dumps(report, indent=2)

    def test_an_error_is_never_up_to_date(self):
        o = self.outcome(0, self._report(
            latest=None, error="Update check curl failed: exit=77 stderr=..."), "")
        self.assertEqual((o.kind, o.title), ("failed", "Update check failed"))
        self.assertIn("exit=77", o.body)
        self.assertNotIn("Up to date", o.title)
        self.assertIsNone(o.release_url)

    def test_a_non_zero_exit_and_no_json_fail_too(self):
        o = self.outcome(1, "", "Traceback ...")
        self.assertEqual(o.title, "Update check failed")
        self.assertIn("Traceback", o.body)
        o = self.outcome(0, "", "")
        self.assertEqual(o.title, "Update check failed")
        o = self.outcome(0, "[]", "")
        self.assertEqual(o.title, "Update check failed")
        o = self.outcome(0, json.dumps({"current": "2.5.6"}), "")
        self.assertEqual(o.title, "Update check failed")

    def test_up_to_date(self):
        o = self.outcome(0, self._report(), "")
        self.assertEqual((o.kind, o.title, o.urgency), ("current", "Up to date", "low"))
        self.assertIn("2.5.6", o.body)

    def test_update_available(self):
        url = "https://github.com/brewsterkahle/onionpress/releases/tag/v2.6.0"
        o = self.outcome(0, self._report(latest="2.6.0", update_available=True,
                                         release_url=url), "")
        self.assertEqual((o.kind, o.title), ("available", "OnionPress 2.6.0 is available"))
        self.assertEqual(o.release_url, url)

    def test_the_menu_item_uses_it(self):
        tray = _read(TRAY)
        handler = tray[tray.index("def _on_check_updates("):tray.index("def _on_uninstall(")]
        self.assertIn('Launcher.run_json("check-for-update", "--json")', handler)
        self.assertIn("ui_client.update_outcome(rc, out, err)", handler)
        self.assertNotIn("sys.executable", handler)
        self.assertIn("onionpress update", handler)
        self.assertNotIn("Up to date", handler)
        # "Up to date" is said in one place, after the error field was read.
        self.assertEqual(tray.count("Up to date"), 0)
        ui_client = _read("src/onionpress/ui_client.py")
        self.assertEqual(ui_client.count('UpdateOutcome("current", "Up to date"'), 1)
        menubar = _read("src/menubar.py")
        handler = menubar[menubar.index("def check_for_updates("):menubar.index("def _install_update(")]
        self.assertIn("ui_client.update_outcome(rc, out, err)", handler)
        self.assertIn('outcome.kind == "failed"', handler)


if __name__ == "__main__":
    unittest.main()

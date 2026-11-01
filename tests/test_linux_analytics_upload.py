#!/usr/bin/env python3
"""The analytics upload on Linux (issues #207, #203, #407).

Before #207 no Linux install ever uploaded; the heartbeat client then ran
onionpress.analytics_sharing's loop through an adapter that advertised the
launcher's plain, bash-rotated files under stable names. Since #407 the
daemon runs on Linux too, so the upload reads the daemon's own rotating
logs (onionpress.log_rotation: dated, per-roll names, IPs pseudonymized
at rotation) exactly as on a Mac. These tests drive
analytics_sharing._do_upload_cycle against a Linux daemon with its logs
written and never touch Docker: every log type the daemon keeps is
offered once, and nothing sensitive leaves the machine.
"""
import base64
import gzip
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_DIR, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_mac_daemon import FakeDocker, FakeHealth  # noqa: E402

from onionpress import analytics_sharing, daemon as op_daemon, key_manager, onion_auth  # noqa: E402
from onionpress.platform import OS, resolve_paths  # noqa: E402

CONTENT = "op2" + "a" * 53 + ".onion"
HC = "h" * 56 + ".onion"


def _random_keypair():
    priv = os.urandom(32) + os.urandom(32)
    return priv, onion_auth.derive_public_key(priv)


def _read(rel):
    with open(os.path.join(PROJECT_DIR, rel), encoding="utf-8") as f:
        return f.read()


class _LinuxDaemonUpload(unittest.TestCase):
    HOME = "/home/op-test-user"

    def setUp(self):
        self.data = tempfile.mkdtemp(prefix="op-upload-")
        self.addCleanup(shutil.rmtree, self.data, True)
        with open(os.path.join(self.data, "config"), "w") as f:
            f.write("SHARE_ANALYTICS_WITH_ONIONHOME=yes\nREGISTER_WITH_ONIONHEAVEN=no\n")
        paths = resolve_paths(data_dir=self.data, app_bundle="")
        self.d = op_daemon.Daemon(paths, version="9.9", docker=FakeDocker({}),
                                  health_checker=FakeHealth(), os_name=OS.LINUX)
        self.d.onion_address = CONTENT
        self.d.healthcheck_address = HC
        self.logged = []
        self.d.log = self.logged.append
        p = mock.patch.object(key_manager, "extract_keys", _random_keypair)
        p.start()
        self.addCleanup(p.stop)
        self.posts = []
        self.uploads = {}

        def fake_post(app, url, payload):
            self.posts.append((url, payload))
            if url.endswith("/logs/manifest"):
                return {"wanted": [f["name"] for f in payload["files"]]}
            self.uploads[payload["file_name"]] = gzip.decompress(
                base64.b64decode(payload["file_content"])).decode("utf-8")
            return {"stored": True}
        p = mock.patch.object(analytics_sharing, "_post_json", fake_post)
        p.start()
        self.addCleanup(p.stop)

    def _cycle(self):
        with mock.patch.dict(os.environ, {"HOME": self.HOME}):
            return analytics_sharing._do_upload_cycle(self.d, include_active=True)


class TestInstallPathsLayDownLib(unittest.TestCase):

    def test_install_paths_lay_down_lib(self):
        # One file list for the .deb, install.sh and `onionpress update`
        # (tests/test_linux_install_tree.py ties them to it): the daemon
        # imports onionpress.analytics_sharing from there.
        self.assertRegex(_read("linux/install-tree.sh"), r"\ntree +src/onionpress +lib/onionpress\n")
        for rel in ("linux/install.sh", "build/build-linux.sh"):
            self.assertIn("linux/install-tree.sh", _read(rel), rel)


class TestUploadCycleOnLinux(_LinuxDaemonUpload):

    def test_offers_every_daemon_log_once_under_dated_names(self):
        self.d._onionpress_log.write("[2026-10-10 06:00:00] OnionPress daemon starting\n")
        self.d._tor_log.write("Oct 10 06:00:00.000 [notice] Bootstrapped 100%\n")
        self.d._wp_errors_log.write("[10-Oct-2026 06:00:00 UTC] [OnionPress Wayback] state: idle -> sweeping\n")
        self.d._wp_visitors_log.write('10.0.0.5 - - "GET / HTTP/1.1" 200\n')
        self.d._onionheaven_log.write("heaven\n")
        self.d._last_status_payload = {"state": "running", "platform": "linux"}
        result = self._cycle()
        self.assertEqual(result["status"], "ok", self.logged)
        manifest_url, manifest = self.posts[0]
        self.assertTrue(manifest_url.endswith(":8083/logs/manifest"))
        self.assertEqual(manifest["content_address"], CONTENT)
        self.assertEqual(manifest["healthcheck_address"], HC)
        self.assertNotEqual(manifest["os_version"], "")
        names = [f["name"] for f in manifest["files"]]
        self.assertEqual(len(names), len(set(names)), f"duplicate offer: {names}")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for kind in ("onionpress", "container-tor", "container-wordpress", "wordpress-visitors",
                     "container-onionheaven"):
            with self.subTest(kind=kind):
                # The active file ships gzipped under its dated, per-roll name.
                self.assertEqual([n for n in names if re.match(rf"^{kind}-{today}-\d{{3}}\.log\.gz$", n)],
                                 [f"{kind}-{today}-001.log.gz"], names)
        self.assertIn(f"status-{today}.json.gz", names)
        self.assertEqual(result["uploaded"], len(names))
        self.assertEqual(result["wanted"], len(names))
        # Empty logs (nothing written) are not offered.
        self.assertFalse([n for n in names if n.startswith("container-db")], names)

    def test_manifest_failure_is_reported_not_raised(self):
        self.d._onionpress_log.write("x\n")
        with mock.patch.object(analytics_sharing, "_post_json", lambda app, url, payload: None):
            result = self._cycle()
        self.assertEqual(result["status"], "manifest_failed")


class TestUploadIsScrubbedOnLinux(_LinuxDaemonUpload):
    """Every byte passes redact.scrub_for_upload on its way out: the home
    directory, auth parameters, credential headers, and visitor IPs in
    visitor-facing logs."""

    LEAKS = ("op-test-user", "s3cr3tT0k", "n0nceV4l", "c00kieV4l", "192.168.1.77")

    def test_nothing_sensitive_leaves_the_machine(self):
        with mock.patch.dict(os.environ, {"HOME": self.HOME}):
            self.d._onionpress_log.write(
                f"[2026-10-10 06:00:00] daemon starting in {self.HOME}/.onionpress\n"
                "[2026-10-10 06:00:01] GET http://x.onion/?token=s3cr3tT0k\n")
            self.d._wp_visitors_log.write(
                '192.168.1.77 - - "GET /wp-admin/?_wpnonce=n0nceV4l HTTP/1.1" 200\n'
                "Cookie: wordpress_logged_in_x=c00kieV4l\n")
            self.d._tor_log.write("Oct 01 06:00:00.000 [notice] Opening connection to 203.0.113.5\n")
        self.d._last_status_payload = {
            "state": "running", "platform": "linux",
            "wp_admin_password_path": f"{self.HOME}/.onionpress/wp-admin-password"}
        result = self._cycle()
        self.assertEqual(result["status"], "ok", self.logged)
        self.assertEqual(len(self.uploads), 4, sorted(self.uploads))
        for name, body in self.uploads.items():
            for leak in self.LEAKS:
                self.assertNotIn(leak, body, f"{leak!r} shipped in {name}")
        visitors = next(b for n, b in self.uploads.items() if n.startswith("wordpress-visitors-"))
        self.assertIn("_wpnonce=<redacted>", visitors)
        self.assertIn("Cookie: <redacted>", visitors)
        self.assertRegex(visitors, r"^fd[0-9a-f]{2}:", "visitor IP not pseudonymized")
        tor = next(b for n, b in self.uploads.items() if n.startswith("container-tor-"))
        self.assertIn("203.0.113.5", tor, "a destination log keeps its addresses")
        status = next(b for n, b in self.uploads.items() if n.startswith("status-"))
        self.assertEqual(json.loads(status)["wp_admin_password_path"], "~/.onionpress/wp-admin-password")

    def test_a_file_that_cannot_be_scrubbed_is_not_offered(self):
        self.d._onionpress_log.write("a\n")
        self.d._wp_visitors_log.write("b\n")
        self.d._last_status_payload = {"state": "running"}
        real = analytics_sharing.redact.scrub_for_upload

        def flaky(raw, app_support, log_name, home=None):
            if log_name.startswith("wordpress-visitors"):
                raise OSError("salt dir not writable")
            return real(raw, app_support, log_name, home)
        with mock.patch.object(analytics_sharing.redact, "scrub_for_upload", flaky):
            result = self._cycle()
        self.assertEqual(result["status"], "ok", self.logged)
        self.assertFalse([n for n in self.uploads if n.startswith("wordpress-visitors")], sorted(self.uploads))
        self.assertTrue(any("not offering wordpress-visitors-" in m for m in self.logged), self.logged)


if __name__ == "__main__":
    unittest.main()

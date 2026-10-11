#!/usr/bin/env python3
"""Linux analytics upload (issue #207).

Until this change `analytics_sharing.start_analytics_sharing()` was only
ever called from the Mac menubar app, so no Linux install uploaded its
logs to OnionHome — the opt-in flag the tray wrote was read by nothing.
The heartbeat daemon (linux/onionpress-heartbeat.py) now runs the same
loop through an `AnalyticsHost` adapter.

These tests load the daemon as a module under a throwaway HOME (it opens
~/.onionpress/onionpress.log at import time) and never touch Docker:
key extraction and the curl-over-Tor POST are stubbed at the
analytics_sharing module boundary.
"""
import importlib.util
import json
import os
import platform
import secrets
import shutil
import sys
import tempfile
import threading
import types
import unittest
from datetime import datetime, timezone

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(PROJECT_DIR, "src"))

from onionpress import analytics_sharing, key_manager, onion_auth  # noqa: E402

HEARTBEAT_PATH = os.path.join(PROJECT_DIR, "linux", "onionpress-heartbeat.py")

# The daemon's HOME for the whole test module: DATA_DIR is computed from
# it at import, and a FileHandler on $DATA_DIR/onionpress.log is opened.
_HOME = tempfile.mkdtemp(prefix="op-linux-analytics-")
_DATA_DIR = os.path.join(_HOME, ".onionpress")
os.makedirs(_DATA_DIR)


def _load_heartbeat():
    """Import linux/onionpress-heartbeat.py the way systemd runs it, minus
    /opt/onionpress: its flat `import key_manager` / `import onion_auth`
    resolve to the package modules."""
    saved_home = os.environ.get("HOME")
    os.environ["HOME"] = _HOME
    sys.modules.setdefault("key_manager", key_manager)
    sys.modules.setdefault("onion_auth", onion_auth)
    try:
        spec = importlib.util.spec_from_file_location(
            "onionpress_heartbeat_under_test", HEARTBEAT_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        if saved_home is None:
            del os.environ["HOME"]
        else:
            os.environ["HOME"] = saved_home
    return mod


hb = _load_heartbeat()

CONTENT = "op2pi7xqz4n3m2kbdj6f5sy7gqwz4txtvzpqfsnwsmfq3rhzpcojamyd.onion"
HC = "vipwsi6bjeychxvdo3xjvojr2yvw7ggwagekemekmeccscryuhehp2id.onion"


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)


def _read(rel):
    with open(os.path.join(PROJECT_DIR, rel)) as fh:
        return fh.read()


class TestDaemonImportPath(unittest.TestCase):
    """The package the daemon needs is installed under lib/ by both install
    paths, and the daemon puts that directory on sys.path."""

    def test_daemon_reads_from_opt_lib(self):
        self.assertEqual(hb.DATA_DIR, _DATA_DIR)
        self.assertEqual(hb.LIB_DIR, "/opt/onionpress/lib")
        self.assertIn(hb.LIB_DIR, sys.path)

    def test_install_paths_lay_down_lib(self):
        for rel in ("linux/install.sh", "build/build-linux.sh"):
            src = _read(rel)
            self.assertRegex(
                src, r'cp -r "\$[A-Za-z_]+/src/onionpress(/\.)?" "\$[A-Za-z_]+/lib/onionpress',
                f"{rel} must copy src/onionpress to <install>/lib/onionpress — "
                "the heartbeat daemon imports onionpress.analytics_sharing from there.",
            )

    def test_main_starts_analytics_before_hub_self_check(self):
        src = _read("linux/onionpress-heartbeat.py")
        main_body = src[src.index("\ndef main():"):src.index("\ndef _cached_content_address")]
        start = main_body.index("_start_analytics_sharing(_content_addr, _hc_addr)")
        hub_check = main_body.index("This node IS the OnionHeaven hub")
        self.assertLess(
            start, hub_check,
            "Analytics sharing must start before the self-hub early return: "
            "the OnionHeaven hub ships its own logs to OnionHome too.",
        )
        reread = main_body.index("# Re-read config each cycle")
        self.assertIn(
            "_poll_analytics_opt_in(analytics_opt_in, config)",
            main_body[reread:],
            "The per-cycle config re-read must poll the analytics opt-in so "
            "enabling it needs no daemon restart.",
        )


class TestAdvertisedNames(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="op-names-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_name_is_stable_while_file_grows(self):
        p = os.path.join(self.dir, "onionpress.log")
        _write(p, "[2026-10-09 12:00:00] OnionPress starting\n")
        first = hb._advertised_log_name("onionpress", p)
        self.assertRegex(first, r"^onionpress-2026-10-09-[0-9a-f]{8}\.log$")
        with open(p, "a") as fh:
            fh.write("[2026-10-09 12:00:01] more\n" * 50)
        self.assertEqual(hb._advertised_log_name("onionpress", p), first)

    def test_name_differs_after_rotation(self):
        p = os.path.join(self.dir, "onionpress.log")
        _write(p, "[2026-10-09 12:00:00] OnionPress starting\n")
        before = hb._advertised_log_name("onionpress", p)
        os.rename(p, p + ".1")
        _write(p, "[2026-10-10 08:00:00] OnionPress starting\n")
        self.assertEqual(hb._advertised_log_name("onionpress", p + ".1"), before)
        self.assertNotEqual(hb._advertised_log_name("onionpress", p), before)

    def test_undated_first_line_and_incomplete_files(self):
        p = os.path.join(self.dir, "container-tor.log")
        _write(p, "Oct 09 12:00:00.000 [notice] Tor 0.4.8 opening log file.\n")
        self.assertRegex(hb._advertised_log_name("container-tor", p),
                         r"^container-tor-[0-9a-f]{8}\.log$")
        _write(p, "")
        self.assertIsNone(hb._advertised_log_name("container-tor", p))
        _write(p, "no newline yet")
        self.assertIsNone(hb._advertised_log_name("container-tor", p))
        self.assertIsNone(hb._advertised_log_name("container-tor",
                                                   os.path.join(self.dir, "missing")))

    def test_names_pass_the_servers_safe_name_check(self):
        p = os.path.join(self.dir, "onionpress.log")
        _write(p, "[2026-10-09 12:00:00] x\n")
        name = hb._advertised_log_name("onionpress", p)
        self.assertIsNotNone(analytics_sharing._LOG_NAME_RE.match(name + ".gz"))

    def test_rotated_plain_log_lists_suffix_files(self):
        p = os.path.join(self.dir, "onionpress.log")
        _write(p, "[2026-10-10 00:00:00] active\n")
        _write(p + ".1", "[2026-10-09 00:00:00] rolled once\n")
        _write(p + ".2", "[2026-10-08 00:00:00] rolled twice\n")
        _write(p + ".4", "[2026-10-01 00:00:00] orphan, gap at .3\n")
        rl = hb._RotatedPlainLog(p, "onionpress")
        self.assertEqual(rl.current_path(), p)
        self.assertRegex(rl.current_name(), r"^onionpress-2026-10-10-[0-9a-f]{8}\.log$")
        _write(p, "")
        self.assertEqual(rl.current_name(), "onionpress.log")
        done = rl.completed_files()
        self.assertEqual([f["path"] for f in done], [p + ".1", p + ".2"])
        for f in done:
            self.assertEqual(f["size"], os.path.getsize(f["path"]))
            self.assertRegex(f["name"], r"^onionpress-2026-10-0[89]-[0-9a-f]{8}\.log$")


class TestAnalyticsHost(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp(prefix="op-host-")
        self.host = hb.AnalyticsHost(CONTENT, HC, data_dir=self.data)
        self._config = hb.CONFIG_PATH
        hb.CONFIG_PATH = os.path.join(self.data, "config")

    def tearDown(self):
        hb.CONFIG_PATH = self._config
        shutil.rmtree(self.data, ignore_errors=True)

    def test_exposes_what_the_sharing_loop_reads(self):
        self.assertEqual(self.host.onion_address, CONTENT)
        self.assertEqual(self.host.healthcheck_address, HC)
        self.assertEqual(self.host.app_support, self.data)
        self.assertFalse(self.host._sleeping)
        self.assertIsInstance(self.host.version, str)
        self.assertTrue(os.path.isdir(os.path.join(self.data, "logs")))
        self.assertEqual(self.host.docker_command(), ("docker", None))
        self.assertIsInstance(self.host.check_internet_connectivity(), bool)

    def test_config_default_is_opted_out(self):
        self.assertEqual(
            self.host.read_config_value("SHARE_ANALYTICS_WITH_ONIONHOME", "no"), "no")
        _write(hb.CONFIG_PATH, "# comment\nSHARE_ANALYTICS_WITH_ONIONHOME=yes\n")
        self.assertEqual(
            self.host.read_config_value("SHARE_ANALYTICS_WITH_ONIONHOME", "no"), "yes")

    def test_status_payload_is_the_launchers_status_json(self):
        self.assertIsNone(self.host._last_status_payload)
        _write(os.path.join(self.data, "status.json"), "{not json")
        self.assertIsNone(self.host._last_status_payload)
        _write(os.path.join(self.data, "status.json"),
               json.dumps({"state": "running", "platform": "linux"}))
        self.assertEqual(self.host._last_status_payload["platform"], "linux")


class TestUploadCycleOnLinux(unittest.TestCase):
    """Drive analytics_sharing._do_upload_cycle with the Linux host and the
    Linux on-disk log layout; docker is never called."""

    def setUp(self):
        self.data = tempfile.mkdtemp(prefix="op-cycle-")
        self.host = hb.AnalyticsHost(CONTENT, HC, data_dir=self.data)
        self.logged = []
        self.host.log = self.logged.append
        self._config = hb.CONFIG_PATH
        hb.CONFIG_PATH = os.path.join(self.data, "config")

        _write(os.path.join(self.data, "onionpress.log"),
               "[2026-10-10 06:00:00] heartbeat client starting...\n")
        _write(os.path.join(self.data, "onionpress.log.1"),
               "[2026-10-01 06:00:00] OnionPress starting\n" * 20)
        _write(os.path.join(self.data, "logs", "container-tor.log"),
               "Oct 10 06:00:00.000 [notice] Bootstrapped 100%\n")
        _write(os.path.join(self.data, "logs", "container-tor.log.1"),
               "Oct 01 06:00:00.000 [notice] Bootstrapped 100%\n" * 20)
        _write(os.path.join(self.data, "status.json"),
               json.dumps({"state": "running", "platform": "linux"}))

        self._orig_extract = key_manager.extract_keys
        key_manager.extract_keys = lambda: (secrets.token_bytes(64), secrets.token_bytes(32))
        self._orig_post = analytics_sharing._post_json
        self.posts = []

        def fake_post(app, url, payload):
            self.posts.append((url, payload))
            if url.endswith("/logs/manifest"):
                return {"wanted": [f["name"] for f in payload["files"]]}
            return {"stored": True}
        analytics_sharing._post_json = fake_post

    def tearDown(self):
        key_manager.extract_keys = self._orig_extract
        analytics_sharing._post_json = self._orig_post
        hb.CONFIG_PATH = self._config
        shutil.rmtree(self.data, ignore_errors=True)

    def test_offers_every_linux_log_once_under_stable_names(self):
        result = analytics_sharing._do_upload_cycle(self.host, include_active=True)
        self.assertEqual(result["status"], "ok", self.logged)

        manifest_url, manifest = self.posts[0]
        self.assertTrue(manifest_url.endswith(":8083/logs/manifest"))
        self.assertEqual(manifest["content_address"], CONTENT)
        self.assertEqual(manifest["healthcheck_address"], HC)
        self.assertNotEqual(manifest["os_version"], "")

        names = [f["name"] for f in manifest["files"]]
        self.assertEqual(len(names), len(set(names)), f"duplicate offer: {names}")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        patterns = [
            r"^onionpress-2026-10-10-[0-9a-f]{8}\.log\.gz$",   # active launcher/daemon log
            r"^onionpress-2026-10-01-[0-9a-f]{8}\.log\.gz$",   # onionpress.log.1
            r"^container-tor-[0-9a-f]{8}\.log\.gz$",           # active + .1 (two matches)
            rf"^status-{today}\.json\.gz$",
        ]
        import re
        for pat in patterns:
            self.assertTrue(any(re.match(pat, n) for n in names), f"{pat} not in {names}")
        self.assertEqual(sum(1 for n in names if n.startswith("container-tor-")), 2)
        # The on-disk names must not leak through _do_upload_cycle's own
        # container-*.log scan: the active file ships once, under the
        # advertised name.
        self.assertNotIn("container-tor.log.gz", names)
        self.assertNotIn("container-tor.log", names)
        self.assertEqual(result["uploaded"], len(names))
        self.assertEqual(result["wanted"], len(names))

        uploads = [p for u, p in self.posts[1:]]
        self.assertEqual(len(uploads), len(names))
        for payload in uploads:
            self.assertEqual(payload["content_address"], CONTENT)
            self.assertIn("file_content", payload)
        uploaded_lines = [m for m in self.logged if m.startswith("Analytics sharing: uploaded ")]
        self.assertEqual(len(uploaded_lines), len(names))

    def test_manifest_failure_is_reported_not_raised(self):
        analytics_sharing._post_json = lambda app, url, payload: None
        result = analytics_sharing._do_upload_cycle(self.host, include_active=True)
        self.assertEqual(result["status"], "manifest_failed")


class TestPostJsonDockerRouting(unittest.TestCase):
    """_post_json reaches docker through the host's docker_command() on
    Linux and through the Colima socket helpers on the Mac, unchanged."""

    def setUp(self):
        self.calls = []
        self._orig_popen = analytics_sharing.subprocess.Popen
        calls = self.calls

        class FakeProc:
            returncode = 0

            def __init__(self, cmd, **kw):
                calls.append((cmd, kw.get("env")))

            def communicate(self, input=None, timeout=None):
                return '{"ok": true}', ""
        analytics_sharing.subprocess.Popen = FakeProc

    def tearDown(self):
        analytics_sharing.subprocess.Popen = self._orig_popen

    def test_linux_host_uses_plain_docker_and_ambient_env(self):
        host = hb.AnalyticsHost(CONTENT, HC, data_dir=tempfile.mkdtemp(prefix="op-pj-"))
        resp = analytics_sharing._post_json(host, "http://x.onion:8083/logs/manifest", {"a": 1})
        self.assertEqual(resp, {"ok": True})
        cmd, env = self.calls[0]
        self.assertEqual(cmd[:4], ["docker", "exec", "-i", "onionpress-tor"])
        self.assertIn("--socks5-hostname", cmd)
        self.assertIsNone(env)

    def test_mac_app_still_uses_colima_socket(self):
        mac = types.SimpleNamespace(
            bin_dir="/Applications/OnionPress.app/Contents/Resources/bin",
            colima_home="/Users/me/.onionpress/colima",
            app_support="/Users/me/.onionpress",
            log=lambda msg: None,
        )
        analytics_sharing._post_json(mac, "http://x.onion:8083/logs/manifest", {"a": 1})
        cmd, env = self.calls[0]
        self.assertEqual(cmd[0], "/Applications/OnionPress.app/Contents/Resources/bin/docker")
        self.assertEqual(env["DOCKER_HOST"], "unix:///Users/me/.onionpress/colima/default/docker.sock")


class TestOsVersion(unittest.TestCase):
    def test_is_never_mac_only(self):
        self.assertTrue(analytics_sharing._os_version())
        orig_system, orig_release = platform.system, getattr(platform, "freedesktop_os_release", None)
        platform.system = lambda: "Linux"
        platform.freedesktop_os_release = lambda: {"PRETTY_NAME": "Debian GNU/Linux 13 (trixie)"}
        try:
            self.assertTrue(analytics_sharing._os_version().startswith("Debian GNU/Linux 13"))
            platform.freedesktop_os_release = lambda: (_ for _ in ()).throw(OSError())
            self.assertTrue(analytics_sharing._os_version().startswith("Linux"))
        finally:
            platform.system = orig_system
            if orig_release is None:
                del platform.freedesktop_os_release
            else:
                platform.freedesktop_os_release = orig_release


class TestOptInGate(unittest.TestCase):
    """The flag is read at every wake and only an explicit `yes` uploads."""

    def setUp(self):
        self.data = tempfile.mkdtemp(prefix="op-gate-")
        self._config = hb.CONFIG_PATH
        hb.CONFIG_PATH = os.path.join(self.data, "config")
        self.host = hb.AnalyticsHost(CONTENT, HC, data_dir=self.data)
        self.host.log = lambda msg: None
        self.host.check_internet_connectivity = lambda: True
        self.ran = threading.Event()
        self._orig_cycle = analytics_sharing._do_upload_cycle

        def fake_cycle(app, include_active=False):
            self.ran.set()
            return {"status": "ok", "wanted": 0, "uploaded": 0}
        analytics_sharing._do_upload_cycle = fake_cycle

    def tearDown(self):
        analytics_sharing._do_upload_cycle = self._orig_cycle
        hb.CONFIG_PATH = self._config
        shutil.rmtree(self.data, ignore_errors=True)

    def test_loop_uploads_only_on_explicit_yes(self):
        thread = analytics_sharing.start_analytics_sharing(self.host)
        self.assertTrue(thread.daemon)

        # Unset → no upload, even when woken explicitly.
        analytics_sharing.trigger_upload()
        self.assertFalse(self.ran.wait(1.5), "uploaded with the flag unset")

        _write(hb.CONFIG_PATH, "SHARE_ANALYTICS_WITH_ONIONHOME=no\n")
        analytics_sharing.trigger_upload()
        self.assertFalse(self.ran.wait(1.5), "uploaded with the flag set to no")

        _write(hb.CONFIG_PATH, "SHARE_ANALYTICS_WITH_ONIONHOME=yes\n")
        analytics_sharing.trigger_upload()
        self.assertTrue(self.ran.wait(10), "no upload after opting in")


class TestDaemonWiring(unittest.TestCase):
    def setUp(self):
        self._host = hb._analytics_host
        hb._analytics_host = None
        self._orig_start = analytics_sharing.start_analytics_sharing
        self._orig_trigger = analytics_sharing.trigger_upload
        self.started = []
        self.triggers = []
        analytics_sharing.start_analytics_sharing = lambda app: self.started.append(app)
        analytics_sharing.trigger_upload = lambda: self.triggers.append(1)

    def tearDown(self):
        analytics_sharing.start_analytics_sharing = self._orig_start
        analytics_sharing.trigger_upload = self._orig_trigger
        hb._analytics_host = self._host

    def test_start_hands_the_loop_a_linux_host_once(self):
        host = hb._start_analytics_sharing(CONTENT, HC)
        self.assertIsInstance(host, hb.AnalyticsHost)
        self.assertEqual(host.onion_address, CONTENT)
        self.assertEqual(host.healthcheck_address, HC)
        self.assertEqual(host.app_support, _DATA_DIR)
        self.assertEqual(self.started, [host])
        self.assertIs(hb._start_analytics_sharing(CONTENT, HC), host)
        self.assertEqual(len(self.started), 1, "loop must be started once per daemon")

    def test_poll_wakes_the_loop_once_when_flag_flips_to_yes(self):
        hb._start_analytics_sharing(CONTENT, HC)
        self.assertEqual(hb._poll_analytics_opt_in(None, {}), "no")
        self.assertEqual(hb._poll_analytics_opt_in("no", {"SHARE_ANALYTICS_WITH_ONIONHOME": "no"}), "no")
        self.assertEqual(self.triggers, [])
        self.assertEqual(hb._poll_analytics_opt_in("no", {"SHARE_ANALYTICS_WITH_ONIONHOME": "Yes "}), "yes")
        self.assertEqual(len(self.triggers), 1)
        self.assertEqual(hb._poll_analytics_opt_in("yes", {"SHARE_ANALYTICS_WITH_ONIONHOME": "yes"}), "yes")
        self.assertEqual(len(self.triggers), 1, "steady-state yes must not re-trigger every minute")
        self.assertEqual(hb._poll_analytics_opt_in("yes", {}), "no")
        self.assertEqual(hb._poll_analytics_opt_in("no", {"SHARE_ANALYTICS_WITH_ONIONHOME": "yes"}), "yes")
        self.assertEqual(len(self.triggers), 2)

    def test_poll_without_a_running_loop_never_triggers(self):
        hb._analytics_host = None
        self.assertEqual(hb._poll_analytics_opt_in(None, {"SHARE_ANALYTICS_WITH_ONIONHOME": "yes"}), "yes")
        self.assertEqual(self.triggers, [])


if __name__ == "__main__":
    unittest.main()

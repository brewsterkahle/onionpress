#!/usr/bin/env python3
"""Every setup ends in the same state: `.setup_complete` in the data dir
(issue #374).

The Mac menubar shows the welcome screen until that marker exists
(src/menubar.py). Until this change only the Mac daemon wrote it, once the
site was fully operational, so a node set up headlessly with
`onionpress setup` (no menubar; or stopped before the site was reachable
over Tor) had a working site and a first menubar launch that asked to set
it up. Now every setup that installs WordPress writes the marker when it
completes: setup_logic.install_fresh_wordpress (the Mac setup window, the
Linux paths) and the Mac launcher's `onionpress setup` (both branches, and
once more for an installed site, so a node from before this change gets
it from a run of `onionpress setup`). The daemon's write stays.
"""

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from onionpress import setup_logic  # noqa: E402

MAC_LAUNCHER = "app/MacOS/onionpress"
ONION = "op2homeiwjb4fdqnfkj5kbokvcee45zpk2pwgvpz5rrkanp5qqwxzbyd.onion"


def _read(rel):
    with open(os.path.join(PROJECT_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _ok():
    return subprocess.CompletedProcess(["docker"], 0, "", "")


def _fail():
    return subprocess.CompletedProcess(["docker"], 1, "", "DB connection refused")


class TestOneMarkerName(unittest.TestCase):
    """The literal in each writer and the reader is setup_logic's."""

    def test_the_name(self):
        self.assertEqual(setup_logic.SETUP_COMPLETE_MARKER, ".setup_complete")

    def test_menubar_reads_it(self):
        self.assertIn('os.path.join(self.app_support, ".setup_complete")',
                      _read("src/menubar.py"))

    def test_daemon_writes_it_when_operational(self):
        self.assertIn('self._marker(".setup_complete")', _read("src/onionpress/daemon.py"))

    def test_mac_launchers_setup_writes_it(self):
        self.assertEqual(_setup_case().count('touch "$DATA_DIR/.setup_complete"'), 2)


class TestInstallFreshWordpressWritesIt(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="onionpress-setup-marker-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.marker = os.path.join(self.tmpdir, ".setup_complete")

    def _run(self, result):
        with mock.patch("onionpress.setup_logic.subprocess.run", return_value=result), \
             mock.patch.object(setup_logic, "start_bundled_plugin_setup", return_value=True):
            return setup_logic.install_fresh_wordpress(
                site_title="My Blog", onionname="alice", password="hunter22",
                onion_addr=ONION, launcher_bin="/usr/local/bin/onionpress",
                data_dir=self.tmpdir)

    def test_after_a_completed_install(self):
        self.assertTrue(self._run(_ok()))
        self.assertTrue(os.path.exists(self.marker))

    def test_not_after_a_failed_one(self):
        self.assertFalse(self._run(_fail()))
        self.assertFalse(os.path.exists(self.marker))

    def test_mark_setup_complete_reports_an_unwritable_dir(self):
        logged = []
        missing = os.path.join(self.tmpdir, "file-not-dir")
        with open(missing, "w") as f:
            f.write("x")
        self.assertFalse(setup_logic.mark_setup_complete(
            os.path.join(missing, "sub"), logged.append))
        self.assertEqual(len(logged), 1)
        self.assertIn("WARNING", logged[0])
        self.assertTrue(setup_logic.mark_setup_complete(self.tmpdir, logged.append))
        self.assertEqual(len(logged), 1)


def _setup_case():
    """The Mac launcher's `setup)` case body, up to its `;;`."""
    text = _read(MAC_LAUNCHER)
    start = text.index("\n        setup)\n") + len("\n        setup)\n")
    end = text.index("\n            ;;\n", start)
    return text[start:end] + "\n"


STUBS = r"""
log() { echo "log: $1" >> "$LOG_FILE"; }
record() { echo "$*" >> "$CALLS"; }
setup_db_passwords() { record setup_db_passwords; }
wp_is_installed() { record wp_is_installed; return "${STUB_INSTALLED_RC:-1}"; }
wp_cli() { record "wp_cli $*"; return "${STUB_WP_CLI_RC:-0}"; }
setup_wordpress_cli() {
    record "setup_wordpress_cli $*"
    SETUP_BACKUP_KEY_RESTORED="${STUB_KEY_RESTORED:-}"
    return "${STUB_SETUP_RC:-0}"
}
docker() {
    record "docker $*"
    [ "$*" = "compose ps tor --format json" ] && echo '{"Name":"onionpress-tor","State":"running"}'
    return 0
}
migrate_key_volume() { record migrate_key_volume; return 0; }
stop_containers() { record stop_containers; }
start_containers() { record start_containers; }
wait_for_services() { record wait_for_services; }
vanity_key_dir() { echo "$DATA_DIR/shared/vanity-keys/$ONION"; }
onionheaven_release() { record "onionheaven_release $*"; }
"""


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class TestMacLauncherSetupWritesIt(unittest.TestCase):
    """The `setup)` case under bash against stubs for everything it runs."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="onionpress-launcher-setup-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data_dir = os.path.join(self.tmp, "data")
        os.mkdir(self.data_dir)
        self.calls = os.path.join(self.tmp, "calls")
        self.marker = os.path.join(self.data_dir, ".setup_complete")

    def _run(self, *args, **stub_env):
        script = (
            "set -e\n"
            f"DATA_DIR={shlex.quote(self.data_dir)}\n"
            f"DOCKER_DIR={shlex.quote(self.tmp)}\n"
            f"LOG_FILE={shlex.quote(os.path.join(self.tmp, 'onionpress.log'))}\n"
            f"CALLS={shlex.quote(self.calls)}\n"
            f"ONION={ONION}\n"
            "ONIONPRESS_WP_PORT=8080\n"
            + STUBS
            + "setup_case() {\n" + _setup_case() + "}\n"
            + 'setup_case "$@"\n'
        )
        env = dict(os.environ, **{k: str(v) for k, v in stub_env.items()})
        return subprocess.run(["bash", "-c", script, "bash", "setup", *args], env=env,
                              capture_output=True, text=True, timeout=30)

    def _calls(self):
        if not os.path.exists(self.calls):
            return []
        with open(self.calls) as f:
            return f.read().splitlines()

    def test_a_completed_setup(self):
        r = self._run("--user", "alice", "--pass", "hunter22", "--email", "a@example.org")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Setup complete!", r.stdout)
        self.assertTrue(os.path.exists(self.marker))
        self.assertIn("setup_wordpress_cli --user alice --pass hunter22 --email a@example.org",
                      self._calls())
        self.assertFalse(os.path.exists(os.path.join(self.data_dir, ".import-key-pending")))

    def test_a_completed_setup_from_backup(self):
        r = self._run("--from-backup", "pw", "/x/backup.zip", STUB_KEY_RESTORED="yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Setup complete!", r.stdout)
        self.assertTrue(os.path.exists(self.marker))
        calls = self._calls()
        self.assertTrue(os.path.exists(os.path.join(self.data_dir, ".import-key-pending")))
        self.assertLess(calls.index("stop_containers"), calls.index("start_containers"))
        self.assertIn("wait_for_services", calls)

    def test_a_failed_setup_writes_nothing(self):
        r = self._run("--user", "alice", STUB_SETUP_RC=1)
        self.assertEqual(r.returncode, 1)
        self.assertFalse(os.path.exists(self.marker))
        self.assertNotIn("Setup complete!", r.stdout)

    def test_no_wordpress_container_writes_nothing(self):
        r = self._run("--user", "alice", STUB_WP_CLI_RC=1)
        self.assertEqual(r.returncode, 1)
        self.assertIn("not running", r.stdout)
        self.assertFalse(os.path.exists(self.marker))
        self.assertNotIn("setup_wordpress_cli --user alice", self._calls())

    def test_an_installed_site_is_set_up(self):
        """A node set up before the marker existed: `onionpress setup`
        says so and writes it."""
        r = self._run(STUB_INSTALLED_RC=0)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("already installed", r.stdout)
        self.assertTrue(os.path.exists(self.marker))
        self.assertNotIn("setup_wordpress_cli", " ".join(self._calls()))


if __name__ == "__main__":
    unittest.main()

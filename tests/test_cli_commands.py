#!/usr/bin/env python3
"""Every command handler in src/onionpress/cli.py, run against stubs.

tests/test_cli_dispatch.py holds the census of subcommands and checks
that `main()` reaches each handler with the parsed arguments; the handlers
themselves were covered unevenly (update, uninstall at a terminal, the
lifecycle commands, update-images, generate-vanity, the hub recovery key,
the Settings page's backup and prepare-restore, restore's answer, and the
helpers that read a password or a recovery key had no test at all). This
file runs each one: OnionPressCLI is built with its Docker, port probe
and secrets stubbed (as test_onionpress_cli.TestCmdGet builds it), every
module it calls is patched where the handler imports it, and the
assertions are about what the user sees (stdout, the JSON answer, the
exit code), what is logged, and what reaches docker or the module.
Nothing here touches a container, the network or the home folder.
"""

import contextlib
import dataclasses
import io
import json
import os
import plistlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from onionpress import cli, confirm, hub_backup, onionheaven, updater  # noqa: E402
from onionpress.backup import (  # noqa: E402
    HUB_DATA_NOT_RESTORED_NOTE, HUB_DATA_RESTORED_LOG, ONION_KEY_LEFT_OUT_NOTE,
    SettingsPageBackupRefused, SettingsPageRestoreRefused, sealed_skipped_message,
)
from onionpress.cli import OnionPressCLI  # noqa: E402
from onionpress.config import PortConfig, Secrets  # noqa: E402
from onionpress.containers import ContainerStatus  # noqa: E402
from onionpress.docker import DockerResult  # noqa: E402
from onionpress.launcher_ops import BACKUP_PASSWORD_ENV  # noqa: E402
from onionpress.platform import OS, resolve_paths  # noqa: E402

ADDR = "a" * 56 + ".onion"
OTHER = "c" * 56 + ".onion"
HUB = "b" * 56 + ".onion"
KID = "0123456789abcdef"


def sodium_available():
    return getattr(hub_backup, "_SealedBox", None) is not None


def make_cli(data_dir):
    """An OnionPressCLI whose setup touched nothing but *data_dir*
    (config written there), with its docker, containers and log
    replaced by stubs."""
    with mock.patch("onionpress.ports.resolve_at_start",
                    return_value=PortConfig(0, 8080, 9050, 9077)), \
            mock.patch("onionpress.cli.ensure_secrets",
                       return_value=Secrets("p1", "p2", "p3")), \
            mock.patch("onionpress.cli.Docker") as MockDocker:
        MockDocker.return_value = mock.Mock()
        obj = OnionPressCLI(data_dir=data_dir, app_bundle="")
    obj.docker = mock.Mock()
    obj.docker.docker_bin = "/stub/docker"
    obj.docker.run.return_value = DockerResult(0, "", "")
    obj.containers = mock.Mock()
    obj.logged = []
    obj.log = obj.logged.append
    return obj


class Sandbox(unittest.TestCase):
    """A data directory of its own; stdout and stderr captured."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="op-cli-cmd-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.out = io.StringIO()
        self.err = io.StringIO()
        for name, stream in (("stdout", self.out), ("stderr", self.err)):
            p = mock.patch.object(sys, name, stream)
            p.start()
            self.addCleanup(p.stop)
        self.logged = []
        # Nothing here may read this user's own environment.
        env = mock.patch.dict(os.environ, {"HOME": self.dir})
        env.start()
        self.addCleanup(env.stop)
        for key in (BACKUP_PASSWORD_ENV, "ONIONPRESS_PORT_OFFSET", "ONIONPRESS_APP_PATH",
                    "ONIONPRESS_LAUNCHER", "DOCKER_HOST"):
            os.environ.pop(key, None)

    def log(self, msg):
        self.logged.append(msg)

    def no_tty(self, text=""):
        """stdin is a pipe carrying *text*."""
        p = mock.patch.object(sys, "stdin", io.StringIO(text))
        p.start()
        self.addCleanup(p.stop)

    def tty(self, answers=()):
        """stdin is a terminal; input() answers with *answers* in turn."""
        p = mock.patch.object(sys, "stdin", mock.Mock(isatty=lambda: True))
        p.start()
        self.addCleanup(p.stop)
        q = mock.patch("builtins.input", side_effect=list(answers))
        q.start()
        self.addCleanup(q.stop)

    def answer(self):
        """The JSON object the command printed last."""
        doc = confirm.last_json_line(self.out.getvalue())
        self.assertIsNotNone(doc, self.out.getvalue())
        return doc

    def write(self, rel, text=""):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return path


# ---------------------------------------------------------------------------
# the helpers: passwords, recovery keys, logs, the hub context
# ---------------------------------------------------------------------------

class TestReadBackupPassword(Sandbox):

    def test_a_positional_argument_is_refused(self):
        # Issue #504: never from argv, whatever the environment holds.
        os.environ[BACKUP_PASSWORD_ENV] = "from-env"
        with self.assertRaises(SystemExit) as cm:
            cli.read_backup_password("argv")
        self.assertEqual(cm.exception.code, 2)
        # Removed from the environment either way: docker children must
        # not inherit it.
        self.assertNotIn(BACKUP_PASSWORD_ENV, os.environ)
        self.assertIn("ERROR: a password on the command line", self.err.getvalue())
        self.assertNotIn("argv", self.err.getvalue().replace("the command line", ""))

    def test_the_environment_is_taken_and_removed(self):
        os.environ[BACKUP_PASSWORD_ENV] = "from-env"
        self.assertEqual(cli.read_backup_password(), "from-env")
        self.assertNotIn(BACKUP_PASSWORD_ENV, os.environ)
        self.assertEqual(self.err.getvalue(), "")

    def test_a_line_on_a_pipe(self):
        self.no_tty("piped\nrest\n")
        self.assertEqual(cli.read_backup_password(), "piped")

    def test_no_stdin_is_no_password(self):
        with mock.patch.object(sys, "stdin", None):
            self.assertEqual(cli.read_backup_password(), "")

    def test_prompted_at_a_terminal(self):
        self.tty()
        with mock.patch("onionpress.cli.getpass.getpass", return_value="typed") as gp:
            self.assertEqual(cli.read_backup_password(), "typed")
        self.assertIn("Backup password", gp.call_args.args[0])

    def test_an_interrupted_prompt_is_no_password(self):
        self.tty()
        for exc in (EOFError, KeyboardInterrupt):
            with mock.patch("onionpress.cli.getpass.getpass", side_effect=exc):
                self.assertEqual(cli.read_backup_password(), "")


class TestReadHubRecoveryKey(Sandbox):

    def test_the_environment_first(self):
        self.no_tty("other\n")
        self.assertEqual(cli.read_hub_recovery_key("env-key", stdin_ok=True, prompt="?"),
                         "env-key")

    def test_no_stdin(self):
        with mock.patch.object(sys, "stdin", None):
            self.assertEqual(cli.read_hub_recovery_key("", stdin_ok=True, prompt="?"), "")

    def test_a_pipe_only_when_allowed(self):
        self.no_tty("  piped-key  \n")
        self.assertEqual(cli.read_hub_recovery_key("", stdin_ok=False, prompt="?"), "")
        self.assertEqual(cli.read_hub_recovery_key("", stdin_ok=True, prompt="?"),
                         "piped-key")

    def test_prompted_at_a_terminal(self):
        self.tty()
        with mock.patch("onionpress.cli.getpass.getpass", return_value=" k ") as gp:
            self.assertEqual(cli.read_hub_recovery_key("", stdin_ok=False, prompt="Key: "), "k")
        self.assertEqual(gp.call_args.args[0], "Key: ")
        with mock.patch("onionpress.cli.getpass.getpass", side_effect=EOFError):
            self.assertEqual(cli.read_hub_recovery_key("", stdin_ok=False, prompt="Key: "), "")


@unittest.skipUnless(sodium_available(), "PyNaCl is not installed")
class TestHubKeyForBackup(Sandbox):
    """_hub_key_for_backup: the recovery key a restore uses, checked
    against the backup's key id, asked for again only at a terminal."""

    def setUp(self):
        super().setUp()
        self.text, self.kid = hub_backup.create_recovery_key(os.path.join(self.dir, "a"))
        self.other, _ = hub_backup.create_recovery_key(os.path.join(self.dir, "b"))
        self.sealed = {"onionheaven_data": "sealed", "onionheaven_recovery_key_id": self.kid}

    def test_a_backup_without_sealed_data_ignores_a_key(self):
        self.no_tty()
        self.assertIsNone(cli._hub_key_for_backup({"onionheaven_data": "plain"}, self.text,
                                                  self.log))
        self.assertIn("holds no sealed hub data", self.logged[0])
        self.logged.clear()
        self.assertIsNone(cli._hub_key_for_backup({}, "", self.log))
        self.assertEqual(self.logged, [])

    def test_the_right_key_from_the_environment(self):
        self.no_tty()
        raw = cli._hub_key_for_backup(self.sealed, self.text, self.log)
        self.assertEqual(raw, hub_backup.parse_private_key(self.text))
        self.assertEqual(self.err.getvalue(), "")

    def test_a_wrong_key_off_a_terminal_is_refused_once(self):
        self.no_tty()
        self.assertIsNone(cli._hub_key_for_backup(self.sealed, self.other, self.log))
        self.assertEqual(self.err.getvalue().count("ERROR:"), 1)
        self.assertTrue(any("was not used" in m for m in self.logged))
        self.assertTrue(any(f"sealed to key {self.kid}" in m for m in self.logged))

    def test_at_a_terminal_three_tries_then_without_the_hub_data(self):
        self.tty()
        with mock.patch("onionpress.cli.getpass.getpass",
                        side_effect=["garbage", self.other, "garbage"]) as gp:
            self.assertIsNone(cli._hub_key_for_backup(self.sealed, "", self.log))
        self.assertEqual(gp.call_count, 3)
        self.assertEqual(self.err.getvalue().count("ERROR:"), 3)
        self.assertIn("sealed to recovery key " + self.kid, self.err.getvalue())

    def test_a_right_key_after_a_wrong_one_at_the_terminal(self):
        self.tty()
        with mock.patch("onionpress.cli.getpass.getpass", side_effect=["garbage", self.text]):
            raw = cli._hub_key_for_backup(self.sealed, "", self.log)
        self.assertEqual(raw, hub_backup.parse_private_key(self.text))

    def test_an_empty_answer_restores_without_the_hub_data(self):
        self.tty()
        with mock.patch("onionpress.cli.getpass.getpass", return_value="") as gp:
            self.assertIsNone(cli._hub_key_for_backup(self.sealed, "", self.log))
        self.assertEqual(gp.call_count, 1)
        self.assertNotIn("ERROR", self.err.getvalue())

    def test_an_unknown_key_id_is_said_so(self):
        self.no_tty()
        meta = {"onionheaven_data": "sealed", "onionheaven_recovery_key_id": "nope"}
        # Any key opens a backup whose id is unknown: only the seal can tell.
        self.assertIsNotNone(cli._hub_key_for_backup(meta, self.other, self.log))
        self.assertIsNone(cli._hub_key_for_backup(meta, "", self.log))
        self.assertTrue(any("sealed to key unknown" in m for m in self.logged))


class TestLogFuncs(Sandbox):

    def test_make_log_func_writes_stderr_and_the_file(self):
        path = os.path.join(self.dir, "op.log")
        cli._make_log_func(path)("hello")
        self.assertRegex(self.err.getvalue(), r"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\] hello\n$")
        with open(path) as f:
            self.assertIn("] hello\n", f.read())

    def test_make_log_func_survives_an_unwritable_file(self):
        cli._make_log_func(os.path.join(self.dir, "missing", "op.log"))("hello")
        self.assertIn("hello", self.err.getvalue())

    def test_file_log_func(self):
        cli._file_log_func(None)("dropped")
        cli._file_log_func(os.path.join(self.dir, "missing", "op.log"))("dropped")
        path = os.path.join(self.dir, "op.log")
        cli._file_log_func(path)("kept")
        with open(path) as f:
            self.assertIn("] kept\n", f.read())
        self.assertEqual(self.err.getvalue(), "")
        self.assertEqual(self.out.getvalue(), "")

    def test_launcher_log_func_prints_the_launchers_format(self):
        cli._launcher_log_func()("step done")
        self.assertRegex(self.out.getvalue(), r"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\] step done\n$")

    def test_the_recorder_keeps_what_it_passes_on(self):
        rec = cli._LogRecorder(self.log)
        rec("one")
        rec("two")
        self.assertEqual(rec.seen, ["one", "two"])
        self.assertEqual(self.logged, ["one", "two"])


class TestHubContext(Sandbox):

    def test_the_docker_environment_and_the_hub_address(self):
        bin_dir = os.path.join(self.dir, "bin")
        os.mkdir(bin_dir)
        self.write("config", f"ONIONHEAVEN_ADDRESS={HUB}\n")
        paths = dataclasses.replace(resolve_paths(data_dir=self.dir, app_bundle=""),
                                    bin_dir=bin_dir)
        saved = onionheaven.ONIONHEAVEN_ADDRESS
        self.addCleanup(setattr, onionheaven, "ONIONHEAVEN_ADDRESS", saved)
        with mock.patch("onionpress.onionheaven.resolve_paths", return_value=paths), \
                mock.patch.dict(os.environ, {"PATH": "/usr/bin"}):
            ctx = cli._hub_context(self.dir, self.log)
            self.assertEqual(os.environ["DOCKER_HOST"], f"unix://{paths.docker_socket}")
            self.assertEqual(os.environ["PATH"].split(os.pathsep)[0], bin_dir)
            # A second call adds the bin dir once and keeps a DOCKER_HOST
            # the launcher exported.
            os.environ["DOCKER_HOST"] = "unix:///launcher"
            cli._hub_context(self.dir, self.log)
            self.assertEqual(os.environ["DOCKER_HOST"], "unix:///launcher")
            self.assertEqual(os.environ["PATH"].split(os.pathsep).count(bin_dir), 1)
        self.assertIsInstance(ctx, onionheaven.HubContext)
        self.assertEqual(onionheaven.ONIONHEAVEN_ADDRESS, HUB)


# ---------------------------------------------------------------------------
# check-for-update, update, uninstall
# ---------------------------------------------------------------------------

def _release(result, assets=("OnionPress.dmg", "onionpress.deb", "SHA256SUMS")):
    """An updater.Release from the (data, version) pairs these tests
    describe releases with."""
    data, version = result
    return updater.Release(f"v{version}", version, data, list(assets))


class TestCheckForUpdateReport(Sandbox):

    def _report(self, result, *, others=(), offsets=(), json_output=False, error=None,
                os_name="macos"):
        """*result* is (data, version) or None (up to date: the release is
        the installed 2.5.0); *error* is what the lookup failed with."""
        if error is not None:
            find = mock.patch("onionpress.updater.find_release",
                              side_effect=updater.UpdateError(error))
        else:
            find = mock.patch("onionpress.updater.find_release",
                              return_value=_release(result or ({}, "2.5.0")))
        with find, mock.patch("onionpress.updater.detect_other_instances",
                              return_value=list(others)), \
                mock.patch("onionpress.updater.detect_active_offsets",
                           return_value=list(offsets)) as active:
            rc = cli.check_for_update_report("2.5.0", self.log, json_output, os_name=os_name)
        self.assertEqual(rc, 0)
        self.active = active
        return self.out.getvalue()

    def test_an_update_with_its_notes(self):
        text = self._report(({"body": " Fixes.\n", "published_at": "2026-10-01"}, "2.6.0"))
        self.assertIn("Installed: 2.5.0", text)
        self.assertIn("Update available: 2.6.0  (published 2026-10-01)", text)
        self.assertIn("\nFixes.\n", text)
        self.assertIn("Run 'onionpress update' to install it.", text)
        self.assertNotIn("other users", text)

    def test_long_notes_are_cut(self):
        text = self._report(({"body": "x" * 900}, "2.6.0"))
        self.assertIn("x" * 800 + "\n... (truncated)", text)
        self.assertNotIn("x" * 801, text)
        self.assertIn("published ?", text)

    def test_up_to_date(self):
        self.assertIn("Already up to date (release v2.5.0).", self._report(None))

    def test_a_failed_check_is_never_up_to_date(self):
        text = self._report(None, error="no network")
        self.assertIn("Update check failed: no network", text)
        self.assertNotIn("up to date", text)
        self.out.truncate(0)
        self.out.seek(0)
        self._report(None, json_output=True, error="no network")
        doc = json.loads(self.out.getvalue())
        self.assertEqual(doc["error"], "no network")
        self.assertIsNone(doc["latest"])
        self.assertFalse(doc["update_available"])
        self.assertFalse(doc["installable"])
        self.assertTrue(any("failed" in m for m in self.logged))

    def test_the_json_report(self):
        os.environ["ONIONPRESS_PORT_OFFSET"] = "10000"
        self._report(({"body": "", "published_at": "2026-10-01"}, "2.6.0"), json_output=True,
                     others=[{"user": "bob", "pid": 42}], offsets=[10000])
        doc = json.loads(self.out.getvalue())
        self.assertEqual(doc["current"], "2.5.0")
        self.assertEqual(doc["latest"], "2.6.0")
        self.assertTrue(doc["update_available"])
        self.assertTrue(doc["installable"])
        self.assertIsNone(doc["install_problem"])
        self.assertEqual(doc["release_url"],
                         "https://github.com/brewsterkahle/onionpress/releases/tag/v2.6.0")
        self.assertIsNone(doc["error"])
        self.assertEqual(doc["other_instances"], [{"user": "bob", "pid": 42}])
        self.assertEqual(doc["active_offsets"], [10000])
        self.assertEqual(doc["port_offset"], 10000)
        self.assertRegex(doc["checked_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_a_release_the_platform_cannot_install(self):
        # The Mac wants a .dmg and SHA256SUMS; Linux its .deb and the sums.
        with mock.patch("onionpress.updater.find_release",
                        return_value=_release(({}, "2.6.0"), assets=("onionpress.deb",))), \
                mock.patch("onionpress.updater.detect_other_instances", return_value=[]):
            cli.check_for_update_report("2.5.0", self.log, True, os_name="macos")
            mac = json.loads(self.out.getvalue())
            self.out.truncate(0)
            self.out.seek(0)
            cli.check_for_update_report("2.5.0", self.log, True, os_name="linux")
            linux = json.loads(self.out.getvalue())
            self.out.truncate(0)
            self.out.seek(0)
            cli.check_for_update_report("2.5.0", self.log, False, os_name="linux")
            text = self.out.getvalue()
        self.assertEqual((mac["update_available"], mac["installable"]), (True, False))
        self.assertIn("has no .dmg", mac["install_problem"])
        self.assertEqual((linux["update_available"], linux["installable"]), (True, False))
        self.assertIn("publishes no SHA256SUMS", linux["install_problem"])
        self.assertIn("cannot install it: release v2.6.0 publishes no SHA256SUMS", text)

    def test_offsets_are_asked_only_with_other_users(self):
        os.environ["ONIONPRESS_PORT_OFFSET"] = "not-a-number"
        self._report(None, json_output=True)
        doc = json.loads(self.out.getvalue())
        self.assertEqual(doc["port_offset"], 0)
        self.assertEqual(doc["active_offsets"], [])
        self.active.assert_not_called()
        self.assertEqual(doc["latest"], "2.5.0")

    def test_other_users_are_named(self):
        text = self._report(None, others=[{"user": "bob", "pid": 42}])
        self.assertIn("  - bob (pid 42)", text)
        self.assertIn("replaces the shared app bundle for everyone", text)


class TestBundleOwnerHint(Sandbox):

    def test_this_accounts_bundle(self):
        import pwd
        me = pwd.getpwuid(os.getuid()).pw_name
        with mock.patch.dict(os.environ, {"USER": me}):
            hint = cli._bundle_owner_hint(self.dir)
        self.assertIn(f'owned by the "{me}" account', hint)
        self.assertNotIn("sudo chown", hint)

    def test_another_accounts_bundle_gets_the_chown(self):
        with mock.patch.dict(os.environ, {"USER": "someone-else"}):
            hint = cli._bundle_owner_hint(self.dir)
        self.assertIn(f'sudo chown -R $(whoami):admin "{self.dir}"', hint)

    def test_a_missing_bundle_is_no_hint(self):
        self.assertEqual(cli._bundle_owner_hint(os.path.join(self.dir, "nope")), "")


class TestCmdUpdate(Sandbox):
    """`onionpress update` on a Mac: the report, the questions, the staged
    swap with the daemon stopped and started around it."""

    def setUp(self):
        super().setUp()
        self.app = os.path.join(self.dir, "OnionPress.app")
        os.makedirs(os.path.join(self.app, "Contents", "MacOS"))
        with open(os.path.join(self.app, "Contents", "Info.plist"), "wb") as f:
            plistlib.dump({"CFBundleShortVersionString": "2.5.0"}, f)
        self.paths = resolve_paths(data_dir=self.dir, app_bundle=self.app)
        self.release = ({"body": "Notes here", "published_at": "2026-10-01",
                         "assets": []}, "2.6.0")
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        p = self.stack.enter_context
        # GitHub's answer: the release (newer), or the installed 2.5.0.
        self.check = p(mock.patch("onionpress.updater.find_release",
                                  return_value=_release(self.release)))
        self.install = p(mock.patch("onionpress.updater.download_and_install",
                                    return_value="2.6.0"))
        self.others = p(mock.patch("onionpress.updater.detect_other_instances",
                                   return_value=[]))
        self.daemon_pid = p(mock.patch("onionpress.daemon.daemon_pid", return_value=None))
        self.agent_pid = p(mock.patch("onionpress.daemon.agent_loaded_pid",
                                      return_value=(None, "")))
        self.stop_agent = p(mock.patch("onionpress.daemon.stop_agent"))
        self.start_agent = p(mock.patch("onionpress.daemon.start_agent"))

    def _update(self, **kw):
        return cli.cmd_update(self.paths, self.log, os_name="macos", **kw)

    def up_to_date(self):
        self.check.return_value = _release(({}, "2.5.0"))

    def test_check_reports_the_bundles_version(self):
        with mock.patch("onionpress.cli.check_for_update_report", return_value=0) as report:
            self.assertEqual(self._update(check=True, json_output=True, pre=True), 0)
        report.assert_called_once_with("2.5.0", self.log, True, version=None, pre=True,
                                       os_name="macos")
        self.install.assert_not_called()

    def test_without_a_plist_the_running_version_is_the_current_one(self):
        os.remove(os.path.join(self.app, "Contents", "Info.plist"))
        self.check.return_value = _release(({}, cli.__version__))
        self.assertEqual(self._update(json_output=True), 0)
        self.assertEqual(self.answer()["current"], cli.__version__)

    def test_a_lookup_failure_installs_nothing(self):
        self.check.side_effect = updater.UpdateError("GitHub's answer did not describe a release.")
        self.assertEqual(self._update(yes=True, json_output=True), 1)
        self.assertIn("did not describe a release. Nothing was installed.", self.answer()["error"])
        self.install.assert_not_called()

    def test_a_release_without_a_dmg_installs_nothing(self):
        self.check.return_value = _release(self.release, assets=("SHA256SUMS",))
        self.assertEqual(self._update(yes=True, json_output=True), 1)
        self.assertIn("has no .dmg. Nothing was installed.", self.answer()["error"])
        self.install.assert_not_called()

    def test_force_installs_a_release_that_is_not_newer(self):
        self.up_to_date()
        self.assertEqual(self._update(yes=True, json_output=True), 0)
        self.install.assert_not_called()
        self.assertEqual(self._update(yes=True, force=True, json_output=True), 0)
        self.install.assert_called_once()
        self.assertEqual(self.install.call_args.args[1], "2.5.0")

    def test_a_chosen_version_reaches_the_lookup(self):
        self._update(yes=True, version="2.6.0", pre=True)
        self.check.assert_called_with("2.6.0", True, log=self.log)

    def test_up_to_date(self):
        self.up_to_date()
        self.assertEqual(self._update(), 0)
        self.assertIn("Installed: 2.5.0\nChecking GitHub for updates...\nAlready up to date "
                      "(OnionPress 2.5.0; release v2.5.0). Nothing to install.\n"
                      "To install v2.5.0 anyway: onionpress update --version 2.5.0 --force",
                      self.out.getvalue())
        self.assertEqual(self._update(json_output=True), 0)
        self.assertEqual(self.answer(), {"ok": True, "update_available": False, "latest": "2.5.0",
                                         "current": "2.5.0"})

    def test_off_a_terminal_an_update_needs_yes(self):
        self.no_tty()
        self.assertEqual(self._update(json_output=True), 2)
        self.assertIn("re-run with --yes", self.answer()["error"])
        self.install.assert_not_called()

    def test_at_a_terminal_no_aborts(self):
        self.tty(["n"])
        self.others.return_value = [{"user": "bob", "pid": 42}]
        self.assertEqual(self._update(), 0)
        text = self.out.getvalue()
        self.assertIn("Update available: 2.6.0  (published 2026-10-01)", text)
        self.assertIn("Notes here", text)
        self.assertIn("  - bob (pid 42)", text)
        self.assertIn("Aborted.", text)
        self.install.assert_not_called()

    def test_an_interrupted_question_aborts(self):
        self.tty()
        with mock.patch("builtins.input", side_effect=KeyboardInterrupt):
            self.assertEqual(self._update(), 0)
        self.assertIn("Aborted.", self.out.getvalue())
        self.install.assert_not_called()

    def test_yes_installs_with_the_daemon_stopped_around_the_swap(self):
        self.daemon_pid.return_value = 123
        order = []
        self.stop_agent.side_effect = lambda *a, **k: order.append("stop")
        self.start_agent.side_effect = lambda *a, **k: order.append("start")

        def install(release_data, latest, app_path, log=None, before_swap=None,
                    after_swap=None, **kw):
            self.assertEqual((latest, app_path), ("2.6.0", self.app))
            order.append("staged")
            before_swap()
            order.append("swap")
            after_swap()
            return latest
        self.install.side_effect = install
        os.environ["ONIONPRESS_PORT_OFFSET"] = "10000"
        self.assertEqual(self._update(yes=True, json_output=True), 0)
        self.assertEqual(order, ["staged", "stop", "swap", "start"])
        self.assertEqual(self.answer(), {"ok": True, "version": "2.6.0", "current": "2.5.0"})
        self.assertFalse(self.stop_agent.call_args.kwargs["offline"])
        launcher = self.start_agent.call_args.args[0]
        self.assertEqual(launcher, os.path.join(self.app, "Contents", "MacOS", "onionpress"))
        self.assertEqual(self.start_agent.call_args.kwargs["port_offset"], "10000")
        self.assertTrue(any("downloading and installing v2.6.0" in m for m in self.logged))

    def test_no_daemon_means_no_stop_but_a_start(self):
        self.install.side_effect = lambda *a, before_swap, after_swap, **k: (
            before_swap(), after_swap(), "2.6.0")[-1]
        self.assertEqual(self._update(yes=True), 0)
        self.stop_agent.assert_not_called()
        self.start_agent.assert_called_once()
        self.assertIn("Installed v2.6.0. Quit and relaunch OnionPress", self.out.getvalue())

    def test_a_daemon_known_only_to_launchd_is_stopped_too(self):
        self.agent_pid.return_value = (456, "running")
        self.install.side_effect = lambda *a, before_swap, after_swap, **k: (
            before_swap(), "2.6.0")[-1]
        self.assertEqual(self._update(yes=True), 0)
        self.stop_agent.assert_called_once()

    def test_the_app_path_from_the_environment_wins(self):
        other = os.path.join(self.dir, "Other.app")
        os.makedirs(os.path.join(other, "Contents"))
        with open(os.path.join(other, "Contents", "Info.plist"), "wb") as f:
            plistlib.dump({"CFBundleShortVersionString": "2.4.0"}, f)
        os.environ["ONIONPRESS_APP_PATH"] = other
        self.assertEqual(self._update(yes=True, json_output=True), 0)
        self.assertEqual(self.install.call_args.args[2], other)
        self.assertEqual(self.answer()["current"], "2.4.0")

    def test_permission_denied_names_the_owner(self):
        self.install.side_effect = PermissionError("no")
        self.assertEqual(self._update(yes=True, json_output=True), 1)
        doc = self.answer()
        self.assertTrue(doc["permission_denied"])
        self.assertEqual(doc["version"], "2.6.0")
        self.assertIn("another user account owns the app bundle", doc["error"])
        self.assertTrue(any("permission denied" in m for m in self.logged))
        self.assertEqual(self._update(yes=True), 1)
        self.assertIn("ERROR: OnionPress v2.6.0 was downloaded", self.err.getvalue())

    def test_any_other_failure_is_the_error(self):
        self.install.side_effect = RuntimeError("no dmg asset")
        self.assertEqual(self._update(yes=True, json_output=True), 1)
        self.assertEqual(self.answer()["error"], "no dmg asset")
        self.assertTrue(any("Auto-update failed: no dmg asset" in m for m in self.logged))


class TestCmdUninstallAtATerminal(Sandbox):
    """cmd_uninstall's own questions (the hub, then DELETE) at a terminal,
    and what the run reports. The --check and non-terminal refusals are in
    test_cli_confirm_protocol."""

    def setUp(self):
        super().setUp()
        self.paths = resolve_paths(data_dir=self.dir, app_bundle="")
        self.docker = mock.Mock()
        self.hubs = [{"content_address": ADDR, "onionheaven_address": HUB}]
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)

        def check(paths, docker, log, confirmed):
            if self.hubs and "hub-holds-key" not in confirmed:
                confirm.require("hub-holds-key", confirmed, hubs=self.hubs)
            confirm.require("uninstall", confirmed, deletes=["the site", "the key"],
                            data_dir=paths.data_dir)
            return {"running": False, "hubs": len(self.hubs)}
        self.stack.enter_context(mock.patch("onionpress.uninstall.check", side_effect=check))
        self.run = self.stack.enter_context(
            mock.patch("onionpress.uninstall.run", return_value=[]))

    def _uninstall(self, **kw):
        return cli.cmd_uninstall(self.paths, self.docker, self.log, "/x/onionpress", **kw)

    def test_declining_the_hub_question_cancels(self):
        self.tty(["n"])
        self.assertEqual(self._uninstall(), 1)
        self.assertIn(f"{HUB} (key of {ADDR})", self.out.getvalue())
        self.assertIn("Cancelled.", self.out.getvalue())
        self.run.assert_not_called()

    def test_anything_but_delete_cancels(self):
        self.tty(["y", "yes"])
        self.assertEqual(self._uninstall(), 1)
        self.assertIn("the site", self.out.getvalue())
        self.assertIn("Cancelled.", self.out.getvalue())
        self.run.assert_not_called()

    def test_an_interrupted_question_cancels(self):
        self.hubs.clear()
        self.tty()
        with mock.patch("builtins.input", side_effect=EOFError):
            self.assertEqual(self._uninstall(), 1)
        self.run.assert_not_called()

    def test_both_answers_run_the_uninstall(self):
        self.tty(["y", "delete"])
        self.assertEqual(self._uninstall(), 0)
        self.run.assert_called_once()
        self.assertEqual(self.run.call_args.args[3], "/x/onionpress")
        self.assertIn("OnionPress has been uninstalled.", self.out.getvalue())
        self.assertNotIn("Note:", self.out.getvalue())

    def test_check_with_everything_confirmed_is_the_facts(self):
        self.assertEqual(self._uninstall(check=True, json_output=True,
                                         confirmed={"hub-holds-key", "uninstall"}), 0)
        self.assertEqual(self.answer(), {"ok": True, "running": False, "hubs": 1})
        self.run.assert_not_called()

    def test_what_remains_on_a_hub_is_reported(self):
        self.hubs.clear()
        left = [{"content_address": ADDR, "onionheaven_address": HUB, "why": "unanswered"}]
        self.run.return_value = left
        self.assertEqual(self._uninstall(confirmed={"uninstall"}), 0)
        self.assertIn("Note: ", self.out.getvalue())
        self.assertIn(HUB, self.out.getvalue())
        self.assertEqual(self._uninstall(confirmed={"uninstall"}, json_output=True), 0)
        self.assertEqual(self.answer(), {"ok": True, "remaining": left})
        # With --json the run's progress goes to stderr, the answer alone
        # to stdout.
        self.assertIs(self.run.call_args.kwargs["out"], sys.stderr)


# ---------------------------------------------------------------------------
# stop, status, logs
# ---------------------------------------------------------------------------

class TestLifecycleCommands(Sandbox):

    def setUp(self):
        super().setUp()
        self.cli = make_cli(self.dir)

    def test_stop_releases_the_lock(self):
        self.write("onionpress.pid", str(os.getpid()))
        self.assertEqual(self.cli.cmd_stop(), 0)
        self.cli.containers.stop.assert_called_once()
        self.assertFalse(os.path.exists(self.cli.paths.pid_file))
        self.assertIn("OnionPress stopped", self.cli.logged)


    def test_status_is_json(self):
        self.cli.containers.get_status.return_value = ContainerStatus(
            runtime="docker-system", onion_address=ADDR, wp_ready=True,
            tor_bootstrapped=False, services=[{"name": "onionpress-tor", "state": "running"}])
        self.assertEqual(self.cli.cmd_status(), 0)
        self.assertEqual(json.loads(self.out.getvalue()), {
            "onion_address": ADDR, "wp_ready": True, "tor_bootstrapped": False,
            "services": [{"name": "onionpress-tor", "state": "running"}]})

    def test_logs_follows_compose_and_hands_its_status_back(self):
        self.cli.docker.compose.return_value = DockerResult(3, "", "")
        self.assertEqual(self.cli.cmd_logs(), 3)
        call = self.cli.docker.compose.call_args
        self.assertEqual(call.args[0], ["logs", "-f"])
        self.assertEqual(call.kwargs["timeout"], 0)
        compose_files = call.kwargs["compose_files"]
        if self.cli.paths.docker_dir:
            self.assertEqual(compose_files,
                             [os.path.join(self.cli.paths.docker_dir, "docker-compose.yml")])
        else:
            self.assertIsNone(compose_files)


# ---------------------------------------------------------------------------
# backup and restore
# ---------------------------------------------------------------------------

class TestCmdBackupEdges(Sandbox):
    """cmd_backup's refusals and its default output path; the happy path
    and --check are in test_backup_* and test_cli_confirm_protocol."""

    def setUp(self):
        super().setUp()
        self.cli = make_cli(self.dir)
        self.cli.containers.get_onion_address.return_value = ADDR
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        p = self.stack.enter_context
        p(mock.patch("onionpress.backup.get_admin_username", return_value="owner"))
        self.verify = p(mock.patch("onionpress.backup.verify_wp_admin", return_value=(True, "")))
        p(mock.patch("onionpress.backup.served_onion_address", return_value=""))
        self.create = p(mock.patch("onionpress.backup.create_backup", return_value={}))

    def test_a_wrong_password_is_the_error(self):
        self.verify.return_value = (False, "Wrong password for owner")
        self.assertEqual(self.cli.cmd_backup("pw", json_output=True), 1)
        self.assertEqual(self.answer()["error"], "Wrong password for owner")
        self.create.assert_not_called()

    def test_the_default_output_is_under_the_home_folder(self):
        self.assertEqual(self.cli.cmd_backup("pw", check=True, json_output=True), 0)
        doc = self.answer()
        backups = os.path.join(self.dir, "OnionPress", "backups")
        self.assertEqual(os.path.dirname(doc["output"]), backups)
        self.assertEqual(doc["address"], ADDR)
        self.assertEqual(doc["username"], "owner")
        self.assertRegex(doc["filename"], r"^OnionPress-.*\.zip$")
        # --check makes no folder; a backup does.
        self.assertFalse(os.path.exists(backups))
        self.assertEqual(self.cli.cmd_backup("pw", json_output=True), 0)
        self.assertTrue(os.path.isdir(backups))
        self.assertEqual(self.create.call_args.kwargs["output_path"], doc["output"])

    def test_the_owners_backup_keeps_everything(self):
        self.assertEqual(self.cli.cmd_backup("pw", "/tmp/x.zip", "owner", json_output=True), 0)
        kw = self.create.call_args.kwargs
        self.assertTrue(kw["include_hub_data"] and kw["include_host_secrets"]
                        and kw["include_onion_key"])
        self.assertEqual(self.answer(), {"ok": True, "output": "/tmp/x.zip", "bytes": 0,
                                         "notes": []})

    def test_a_failed_backup_is_the_error(self):
        self.create.side_effect = RuntimeError("disk full")
        self.assertEqual(self.cli.cmd_backup("pw", "/tmp/x.zip", json_output=True), 1)
        self.assertEqual(self.answer()["error"], "disk full")
        self.assertIn("Backup failed: disk full", self.cli.logged)

    def test_unsealed_hub_data_is_a_warning(self):
        from onionpress.backup import HUB_DATA_UNSEALED_NOTE
        self.create.return_value = {"onionheaven_data": "unsealed"}
        self.assertEqual(self.cli.cmd_backup("pw", "/tmp/x.zip", json_output=True), 0)
        self.assertEqual(self.answer()["notes"], [HUB_DATA_UNSEALED_NOTE])
        self.assertIn(f"WARNING: {HUB_DATA_UNSEALED_NOTE}", self.err.getvalue())


class TestCmdRestoreCheckEdges(Sandbox):

    def test_an_unreadable_file_is_said_so(self):
        obj = make_cli(self.dir)
        with mock.patch("onionpress.backup.read_backup_metadata", side_effect=OSError("gone")):
            self.assertEqual(obj.cmd_restore_check("pw", "/b.zip", "", frozenset(), True), 1)
        self.assertEqual(self.answer()["error"], "Could not read backup: gone")


class TestCmdRestore(Sandbox):
    """cmd_restore end to end against stubs: the tor image, the sealed
    hub data's four fates, the key seed, a backup without a key, and the
    JSON answer."""

    def setUp(self):
        super().setUp()
        self.cli = make_cli(self.dir)
        self.cli.containers.ensure_tor_image.return_value = "tor:pinned"
        self.staging = os.path.join(self.dir, "staging")
        os.mkdir(self.staging)
        self.write(".install-from-backup", "")
        self.seed_ok = True
        self.runs = []

        def run(args, timeout=30, **kw):
            self.runs.append(list(args))
            if args[0] == "run":
                return DockerResult(0 if self.seed_ok else 1, "", "")
            return DockerResult(0, "", "")
        self.cli.docker.run.side_effect = run
        self.cli.cmd_import_backup_artifacts = mock.Mock(return_value=0)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        p = self.stack.enter_context
        self.meta = {"onion_address": ADDR}
        self.read_meta = p(mock.patch("onionpress.backup.read_backup_metadata",
                                      side_effect=lambda *a: dict(self.meta)))

        def prepare(zip_path, password, log, *, data_dir=None, after_extract=None):
            if after_extract:
                after_extract(self.staging, dict(self.meta))
            return self.staging, dict(self.meta)
        p(mock.patch("onionpress.backup.prepare_install_from_backup", side_effect=prepare))
        self.key_address = p(mock.patch("onionpress.backup.staged_onion_key_address",
                                        return_value=ADDR))
        self.open_sealed = p(mock.patch("onionpress.backup.open_sealed_hub_data",
                                        return_value={"registry": 1}))
        self.name_registry = p(mock.patch("onionpress.backup.restore_staged_name_registry"))
        self.spawn = p(mock.patch("onionpress.derivatives.spawn_regeneration"))
        self.forget = p(mock.patch("onionpress.containers.forget_key_volume_migration"))
        self.release = p(mock.patch("onionpress.cli.release_onionheaven", return_value=[]))
        p(mock.patch("onionpress.onionheaven.registrations_left",
                     return_value=[(ADDR, HUB), (OTHER, HUB)]))
        self.no_tty()

    def _restore(self, key="", confirmed=frozenset(), json_output=True):
        return self.cli.cmd_restore("pw", "/b.zip", key, confirmed=confirmed,
                                    json_output=json_output)

    def volumes_removed(self):
        return [a[2] for a in self.runs if a[:2] == ["volume", "rm"]]

    def test_the_whole_path_for_a_plain_backup(self):
        self.assertEqual(self._restore(), 0)
        self.assertEqual(self.answer(), {
            "ok": True, "address": ADDR, "hub_data": "none",
            "left_on_hubs": [{"content_address": OTHER, "onionheaven_address": HUB,
                              "why": "no-key"}]})
        c = self.cli.containers
        c.stop.assert_called_once()
        self.assertEqual(self.volumes_removed(), [
            "onionpress-onion-keys", "onionpress-arti-state", "onionpress-tor-state",
            "onionpress-db-data", "onionpress-wordpress-data", "onionpress-persistent-data"])
        self.forget.assert_called_once_with(self.dir)
        self.release.assert_called_once()
        self.assertEqual(self.release.call_args.kwargs["keep_address"], ADDR)
        seed = next(a for a in self.runs if a[0] == "run")
        self.assertIn("tor:pinned", seed)
        self.assertIn("--network", seed)
        c.start_core.assert_called_once()
        self.cli.cmd_import_backup_artifacts.assert_called_once_with(
            self.staging, name_registry=False, hub_data=None)
        c.start_tor.assert_called_once()
        self.name_registry.assert_called_once()
        self.spawn.assert_called_once()
        self.assertFalse(os.path.exists(self.staging))
        self.assertFalse(os.path.exists(os.path.join(self.dir, ".install-from-backup")))
        self.assertIn("Restore complete.", self.out.getvalue())
        self.assertEqual(self._restore(json_output=False), 0)

    def test_a_failed_import_starts_tor_again_and_keeps_the_staging(self):
        """#508: Linux stayed down until a restart by hand."""
        self.cli.cmd_import_backup_artifacts.return_value = 1
        self.assertEqual(self._restore(), 1)
        c = self.cli.containers
        c.start_core.assert_called_once()
        c.start_tor.assert_called_once()
        self.assertTrue(os.path.isdir(self.staging))
        self.assertTrue(os.path.exists(os.path.join(self.dir, ".install-from-backup")))
        self.assertTrue(any("starting Tor again" in m for m in self.cli.logged), self.cli.logged)
        self.spawn.assert_not_called()

    def test_a_failure_after_the_teardown_starts_tor_again(self):
        self.name_registry.side_effect = RuntimeError("registry")
        self.assertEqual(self._restore(), 1)
        self.assertGreaterEqual(self.cli.containers.start_tor.call_count, 1)
        self.assertTrue(any("after the containers were rebuilt" in m for m in self.cli.logged))
        # Tor's own start failing is said, not raised.
        self.cli.containers.start_tor.side_effect = RuntimeError("compose")
        self.assertEqual(self._restore(), 1)
        self.assertTrue(any("Tor could not be started again" in m for m in self.cli.logged))

    def test_without_the_tor_image_nothing_is_changed(self):
        self.cli.containers.ensure_tor_image.return_value = ""
        self.assertEqual(self._restore(), 1)
        self.cli.containers.stop.assert_not_called()
        self.read_meta.assert_not_called()
        self.assertTrue(any("tor image" in m for m in self.cli.logged))

    def test_a_bad_address_in_the_backup_stops_before_the_teardown(self):
        self.meta["onion_address"] = "not-an-address"
        self.assertEqual(self._restore(), 1)
        self.cli.containers.stop.assert_not_called()
        self.assertTrue(any("Restore failed during extract/seed" in m for m in self.cli.logged))

    def test_sealed_hub_data_off_a_terminal_needs_a_key_or_the_confirmation(self):
        self.meta.update(onionheaven_data="sealed", onionheaven_recovery_key_id=KID)
        self.assertEqual(self._restore(), 3)
        self.assertIn("--confirmed sealed-hub-data-left-out", self.answer()["error"])
        self.cli.containers.stop.assert_not_called()

    def test_sealed_hub_data_left_out_when_confirmed(self):
        self.meta.update(onionheaven_data="sealed", onionheaven_recovery_key_id=KID)
        self.assertEqual(self._restore(confirmed=frozenset({"sealed-hub-data-left-out"})), 0)
        doc = self.answer()
        self.assertEqual(doc["hub_data"], "left-out")
        self.assertEqual(doc["hub_data_note"], HUB_DATA_NOT_RESTORED_NOTE)
        self.open_sealed.assert_not_called()
        self.cli.cmd_import_backup_artifacts.assert_called_once_with(
            self.staging, name_registry=False, hub_data=None)

    @unittest.skipUnless(sodium_available(), "PyNaCl is not installed")
    def test_sealed_hub_data_restored_with_the_key(self):
        text, kid = hub_backup.create_recovery_key(os.path.join(self.dir, "k"))
        self.meta.update(onionheaven_data="sealed", onionheaven_recovery_key_id=kid)
        self.cli.cmd_import_backup_artifacts.side_effect = lambda *a, **k: (
            self.cli.log(HUB_DATA_RESTORED_LOG + ": 3 keys"), 0)[-1]
        self.assertEqual(self._restore(key=text), 0)
        self.assertEqual(self.answer()["hub_data"], "restored")
        self.assertEqual(self.open_sealed.call_args.args[2], hub_backup.parse_private_key(text))
        self.assertEqual(self.cli.cmd_import_backup_artifacts.call_args.kwargs["hub_data"],
                         {"registry": 1})

    @unittest.skipUnless(sodium_available(), "PyNaCl is not installed")
    def test_hub_data_that_does_not_open_is_left_out(self):
        text, kid = hub_backup.create_recovery_key(os.path.join(self.dir, "k"))
        self.meta.update(onionheaven_data="sealed", onionheaven_recovery_key_id=kid)
        self.open_sealed.side_effect = hub_backup.HubDataError("damaged")
        self.assertEqual(self._restore(key=text), 0)
        self.assertEqual(self.answer()["hub_data"], "left-out")
        self.assertTrue(any("will not be restored: damaged" in m for m in self.cli.logged))

    def test_plain_hub_data(self):
        self.meta["onionheaven_data"] = "plain"
        self.assertEqual(self._restore(), 0)
        self.assertEqual(self.answer()["hub_data"], "plain")

    def test_a_failed_key_seed_removes_the_volume_and_stops(self):
        self.seed_ok = False
        self.assertEqual(self._restore(), 1)
        self.assertEqual(self.volumes_removed().count("onionpress-onion-keys"), 2)
        self.cli.containers.start_core.assert_not_called()
        self.assertTrue(any("not starting Tor under a new address" in m for m in self.cli.logged))

    def test_a_backup_without_a_key_keeps_this_installs_identity(self):
        self.key_address.return_value = ""
        self.assertEqual(self._restore(), 0)
        self.assertEqual(self.volumes_removed(), [
            "onionpress-db-data", "onionpress-wordpress-data", "onionpress-persistent-data"])
        self.assertFalse(any(a[0] == "run" for a in self.runs))
        self.forget.assert_not_called()
        self.release.assert_not_called()
        self.assertTrue(any("keeps its key volumes" in m for m in self.cli.logged))

    def test_a_hub_that_will_not_let_go_does_not_stop_the_restore(self):
        self.release.side_effect = RuntimeError("tor down")
        self.assertEqual(self._restore(), 0)
        self.assertTrue(any("could not ask the OnionHeaven hub" in m for m in self.cli.logged))

    def test_a_failure_after_the_teardown_is_logged(self):
        self.cli.containers.start_core.side_effect = RuntimeError("compose")
        self.assertEqual(self._restore(), 1)
        self.assertIn("Restore failed: compose", self.cli.logged)


class TestImportBackupArtifacts(Sandbox):

    def setUp(self):
        super().setUp()
        self.cli = make_cli(self.dir)
        self.cli.docker.wp.return_value = DockerResult(0, "", "")
        self.staging = os.path.join(self.dir, "staging")
        os.mkdir(self.staging)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.restore = self.stack.enter_context(
            mock.patch("onionpress.backup.restore_container_artifacts"))
        self.stack.enter_context(mock.patch("onionpress.backup.validate_backup_metadata",
                                            side_effect=lambda m: m))

    def test_the_metadata_and_the_schema_migration(self):
        with open(os.path.join(self.staging, "metadata.json"), "w") as f:
            json.dump({"onion_address": ADDR}, f)
        self.assertEqual(self.cli.cmd_import_backup_artifacts(self.staging), 0)
        args, kw = self.restore.call_args
        self.assertEqual(args[:2], (self.staging, {"onion_address": ADDR}))
        self.assertEqual(kw, {"name_registry": True, "hub_data": None, "data_dir": self.dir})
        self.cli.docker.wp.assert_called_once_with("core", "update-db", timeout=120)
        self.assertIn("Install-from-backup: artifacts imported", self.cli.logged)

    def test_without_metadata_and_with_a_failed_migration(self):
        self.cli.docker.wp.return_value = DockerResult(1, "", "no db")
        self.assertEqual(self.cli.cmd_import_backup_artifacts(
            self.staging, name_registry=False, hub_data={"k": 1}), 0)
        self.assertEqual(self.restore.call_args.args[1], {})
        self.assertEqual(self.restore.call_args.kwargs["hub_data"], {"k": 1})
        self.assertTrue(any("update-db reported a problem" in m for m in self.cli.logged))

    def test_a_failed_import_is_1(self):
        self.restore.side_effect = RuntimeError("cp failed")
        self.assertEqual(self.cli.cmd_import_backup_artifacts(self.staging), 1)
        self.assertIn("Install-from-backup import failed: cp failed", self.cli.logged)


class TestStagingHelpers(Sandbox):
    """The static helpers the bash launchers run mid-backup and
    mid-restore: each hands the module's answer through and turns a
    failure into one ERROR line and exit 1 (3 and 4 where the launcher
    tells those apart)."""

    def setUp(self):
        super().setUp()
        self.cli = make_cli(self.dir)

    def test_regenerate_derivatives(self):
        with mock.patch("onionpress.derivatives.regenerate_if_pending",
                        side_effect=[True, False]) as regen, \
                mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(self.cli.cmd_regenerate_derivatives(), 0)
            self.assertEqual(self.cli.cmd_regenerate_derivatives(), 1)
        # Detached (stderr is not a terminal): the log goes to the file only.
        log = regen.call_args.args[1]
        log("from the sweep")
        self.assertEqual(self.cli.logged, [])
        with open(self.cli.paths.log_file) as f:
            self.assertIn("from the sweep", f.read())
        self.assertEqual(regen.call_args.kwargs["docker_bin"], "/stub/docker")
        with mock.patch("onionpress.derivatives.regenerate_if_pending",
                        return_value=True) as regen, \
                mock.patch.object(sys, "stderr", mock.Mock(isatty=lambda: True)):
            self.cli.cmd_regenerate_derivatives()
        self.assertIs(regen.call_args.args[1], self.cli.log)

    def test_put_wp_tree(self):
        with mock.patch("onionpress.wp_files.put_tree", side_effect=[
                mock.Mock(returncode=0), mock.Mock(returncode=1, stderr="tar: denied\n"),
                OSError("no docker")]) as put:
            self.assertEqual(OnionPressCLI.cmd_put_wp_tree("/src/theme", "/var/www/x", True), 0)
            self.assertEqual(put.call_args.kwargs, {"merge": True})
            self.assertEqual(OnionPressCLI.cmd_put_wp_tree("/src/theme", "/var/www/x"), 1)
            self.assertEqual(OnionPressCLI.cmd_put_wp_tree("/src/theme", "/var/www/x"), 1)
        err = self.err.getvalue()
        self.assertIn("Could not copy theme into WordPress: tar: denied", err)
        self.assertIn("Could not copy theme into WordPress: no docker", err)

    def test_passthroughs(self):
        with mock.patch("onionpress.wayback_smoke.smoke_test_wayback", return_value=5) as smoke:
            self.assertEqual(self.cli.cmd_smoke_test_wayback(), 5)
        smoke.assert_called_once_with(self.cli.log)
        with mock.patch("onionpress.cli.check_for_update_report", return_value=0) as report:
            self.assertEqual(self.cli.cmd_check_for_update(json_output=True), 0)
            report.assert_called_once_with(cli.__version__, self.cli.log, True)
            self.cli.cmd_check_for_update(current="1.0.0")
            self.assertEqual(report.call_args.args[0], "1.0.0")


# ---------------------------------------------------------------------------
# the hub's backup recovery key
# ---------------------------------------------------------------------------

@unittest.skipUnless(sodium_available(), "PyNaCl is not installed")
class TestHubRecoveryKeyCommands(Sandbox):

    def private_text(self):
        """The private half the create command printed."""
        for line in self.out.getvalue().splitlines():
            if line.strip().startswith(hub_backup.PRIVATE_PREFIX):
                return line.strip()
        self.fail("no private key printed:\n" + self.out.getvalue())

    def test_create_show_and_check(self):
        self.assertEqual(OnionPressCLI.cmd_hub_recovery_key_create(self.dir), 0)
        text = self.private_text()
        kid = hub_backup.key_id(hub_backup.load_public_key(self.dir))
        self.assertIn(f"created (key id {kid})", self.out.getvalue())
        self.assertIn(hub_backup.public_key_path(self.dir), self.out.getvalue())
        # Shown once: nowhere on disk.
        for name in os.listdir(self.dir):
            with open(os.path.join(self.dir, name)) as f:
                self.assertNotIn(text, f.read())
        # A second create refuses; --force makes another.
        self.assertEqual(OnionPressCLI.cmd_hub_recovery_key_create(self.dir), 1)
        self.assertIn("ERROR:", self.err.getvalue())
        self.out.truncate(0)
        self.out.seek(0)
        self.assertEqual(OnionPressCLI.cmd_hub_recovery_key_create(self.dir, force=True), 0)
        self.assertNotEqual(hub_backup.key_id(hub_backup.load_public_key(self.dir)), kid)
        text = self.private_text()
        self.out.truncate(0)
        self.out.seek(0)
        self.assertEqual(OnionPressCLI.cmd_hub_recovery_key_show(self.dir), 0)
        kid = hub_backup.key_id(hub_backup.load_public_key(self.dir))
        self.assertIn(f"Hub backup recovery key: key id {kid}", self.out.getvalue())
        # check: the environment, then stdin.
        self.assertEqual(OnionPressCLI.cmd_hub_recovery_key_check(self.dir, text), 0)
        self.assertIn(f"That is this hub's backup recovery key (key id {kid}).",
                      self.out.getvalue())
        self.no_tty(text + "\n")
        self.assertEqual(OnionPressCLI.cmd_hub_recovery_key_check(self.dir), 0)

    def test_check_refuses_another_key_or_garbage(self):
        OnionPressCLI.cmd_hub_recovery_key_create(self.dir)
        other, other_kid = hub_backup.create_recovery_key(os.path.join(self.dir, "b"))
        self.assertEqual(OnionPressCLI.cmd_hub_recovery_key_check(self.dir, other), 1)
        self.assertIn(f"a different recovery key (key id {other_kid})", self.out.getvalue())
        self.assertEqual(OnionPressCLI.cmd_hub_recovery_key_check(self.dir, "garbage"), 1)
        self.assertIn("ERROR:", self.err.getvalue())

    def test_show_and_check_without_a_key_or_with_a_damaged_file(self):
        self.assertEqual(OnionPressCLI.cmd_hub_recovery_key_show(self.dir), 3)
        self.assertIn("No hub backup recovery key is set up", self.out.getvalue())
        self.assertEqual(OnionPressCLI.cmd_hub_recovery_key_check(self.dir, "x"), 1)
        self.assertIn("no hub backup recovery key is set up here", self.err.getvalue())
        with open(hub_backup.public_key_path(self.dir), "w") as f:
            f.write("garbage\n")
        self.assertEqual(OnionPressCLI.cmd_hub_recovery_key_show(self.dir), 1)
        self.assertEqual(OnionPressCLI.cmd_hub_recovery_key_check(self.dir, "x"), 1)
        self.assertEqual(self.err.getvalue().count("ERROR:"), 3)

    def test_restore(self):
        text, kid = hub_backup.create_recovery_key(self.dir)
        meta = {"onionheaven_data": "sealed", "onionheaven_recovery_key_id": kid}
        with mock.patch("onionpress.backup.read_sealed_hub_member",
                        return_value=(meta, b"blob")), \
                mock.patch("onionpress.hub_backup.unseal", return_value=b"tar") as unseal, \
                mock.patch("onionpress.hub_backup.unpack", return_value="hub"), \
                mock.patch("onionpress.hub_backup.check", return_value="checked-hub"), \
                mock.patch("onionpress.backup.write_hub_data") as write:
            self.assertEqual(OnionPressCLI.cmd_hub_recovery_key_restore("/b.zip", "pw", text), 0)
            self.assertEqual(unseal.call_args.args, (b"blob", hub_backup.parse_private_key(text)))
            self.assertEqual(write.call_args.args[0], "checked-hub")
            self.assertIn("registrants' keys restored.", self.out.getvalue())
            # The key is checked before anything is written.
            other, _ = hub_backup.create_recovery_key(os.path.join(self.dir, "b"))
            self.assertEqual(OnionPressCLI.cmd_hub_recovery_key_restore("/b.zip", "pw", other), 1)
            self.assertEqual(write.call_count, 1)
            # A failed write.
            write.side_effect = RuntimeError("docker cp")
            self.assertEqual(OnionPressCLI.cmd_hub_recovery_key_restore("/b.zip", "pw", text), 1)
            self.assertIn("could not restore the OnionHeaven data: docker cp", self.err.getvalue())
        with mock.patch("onionpress.backup.read_sealed_hub_member",
                        side_effect=ValueError("This backup holds no sealed hub data.")):
            self.assertEqual(OnionPressCLI.cmd_hub_recovery_key_restore("/b.zip", "pw", text), 1)
        self.assertIn("ERROR: This backup holds no sealed hub data.", self.err.getvalue())
        with mock.patch("onionpress.backup.read_sealed_hub_member",
                        return_value=({**meta, "onionheaven_sealed_format": 9}, b"blob")):
            self.assertEqual(OnionPressCLI.cmd_hub_recovery_key_restore("/b.zip", "pw", text), 1)
        self.assertIn(hub_backup.NEWER_SEAL_MSG.split("\n")[0][:40], self.err.getvalue())


# ---------------------------------------------------------------------------
# generate-vanity, admin-password
# ---------------------------------------------------------------------------

class TestGenerateVanity(Sandbox):

    def setUp(self):
        super().setUp()
        self.cli = make_cli(self.dir)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.has = self.stack.enter_context(
            mock.patch("onionpress.launcher_ops.tor_image_has_mkp224o", return_value=True))
        self.seen = {}

        def generate(prefix, vanity_dir, log_func=None, **kw):
            self.seen["prefix"] = prefix
            self.seen["vanity_dir"] = vanity_dir
            self.seen["marker"] = os.path.exists(os.path.join(self.dir, ".vanity-running"))
            return self.address
        self.address = "op2" + "x" * 53 + ".onion"
        self.generate = self.stack.enter_context(
            mock.patch("onionpress.launcher_ops.generate_vanity_in_container",
                       side_effect=generate))
        self.formats = self.stack.enter_context(
            mock.patch("onionpress.key_manager.ensure_key_formats"))

    def test_an_invalid_prefix_is_refused_with_the_closest_valid_one(self):
        self.write("config", "ADDRESS_PREFIX=Op19\n")
        self.assertEqual(self.cli.cmd_generate_vanity(), 2)
        self.assertTrue(any("ADDRESS_PREFIX is invalid" in m for m in self.cli.logged))
        self.assertTrue(any("Set ADDRESS_PREFIX=op in" in m for m in self.cli.logged))
        self.generate.assert_not_called()

    def test_without_mkp224o_in_the_image(self):
        self.has.return_value = False
        self.assertEqual(self.cli.cmd_generate_vanity(), 3)
        self.generate.assert_not_called()

    def test_nothing_generated_is_1(self):
        self.address = None
        self.assertEqual(self.cli.cmd_generate_vanity(), 1)
        self.assertFalse(os.path.exists(os.path.join(self.dir, ".vanity-running")))
        self.formats.assert_not_called()

    def test_the_generated_address_is_printed_and_converted(self):
        self.write("config", "ADDRESS_PREFIX=abc\n")
        self.assertEqual(self.cli.cmd_generate_vanity(), 0)
        self.assertEqual(self.out.getvalue(), self.address + "\n")
        self.assertEqual(self.seen["prefix"], "abc")
        vanity_dir = os.path.join(self.cli.paths.shared_dir, "vanity-keys")
        self.assertEqual(self.seen["vanity_dir"], vanity_dir)
        # The running marker is there while mkp224o runs and gone after.
        self.assertTrue(self.seen["marker"])
        self.assertFalse(os.path.exists(os.path.join(self.dir, ".vanity-running")))
        self.formats.assert_called_once_with(os.path.join(vanity_dir, self.address))

    def test_a_failed_key_conversion_is_a_warning(self):
        self.formats.side_effect = RuntimeError("no libsodium")
        self.assertEqual(self.cli.cmd_generate_vanity(), 0)
        self.assertTrue(any("WARNING: ensure_key_formats failed: no libsodium" in m
                            for m in self.cli.logged))
        self.assertEqual(self.out.getvalue(), self.address + "\n")

    def test_the_default_prefix(self):
        self.assertEqual(self.cli.cmd_generate_vanity(), 0)
        self.assertEqual(self.seen["prefix"], "op2")


class TestAdminPassword(Sandbox):

    def test_prints_the_password_or_fails(self):
        obj = make_cli(self.dir)
        with mock.patch("onionpress.launcher_ops.get_admin_password",
                        side_effect=[None, "s3cret"]) as get:
            self.assertEqual(obj.cmd_admin_password(), 1)
            self.assertIn("No admin password file found", self.err.getvalue())
            self.assertEqual(obj.cmd_admin_password(), 0)
        self.assertEqual(self.out.getvalue(), "s3cret\n")
        self.assertEqual(get.call_args.args, (self.dir,))


# ---------------------------------------------------------------------------
# reset
# ---------------------------------------------------------------------------

class TestResetAtATerminal(Sandbox):
    """cmd_reset's terminal questions and its wipe; the --check/--confirmed
    protocol and the hub release order are in test_onionheaven_lifecycle."""

    def setUp(self):
        super().setUp()
        self.cli = make_cli(self.dir)
        self.cli.docker.container_running.return_value = False
        self.volumes = "onionpress-db-data\nonionpress-onion-keys\n"
        self.runs = []

        def run(args, timeout=15, **kw):
            self.runs.append(list(args))
            if args[:2] == ["volume", "ls"]:
                return DockerResult(0, self.volumes, "")
            return DockerResult(0, "", "")
        self.cli.docker.run.side_effect = run
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.release = self.stack.enter_context(
            mock.patch("onionpress.cli.release_onionheaven", return_value=[]))
        self.held = self.stack.enter_context(
            mock.patch("onionpress.onionheaven.registrations_left", return_value=[]))
        for name in ("secrets", "config", "config.bak", "onion_address", "onionpress.log",
                     "healthcheck-address", ".last_status_state", "onionpress.pid",
                     onionheaven.REGISTRATION_RECORD):
            self.write(name, "x")
        self.vanity = os.path.join(self.cli.paths.shared_dir, "vanity-keys", ADDR)
        os.makedirs(self.vanity)

    def test_enter_goes_on_and_wipes_everything(self):
        self.tty([""])
        self.assertEqual(self.cli.cmd_reset(), 0)
        self.assertIn("This will ERASE everything", self.out.getvalue())
        self.assertIn("Reset complete.", self.out.getvalue())
        self.cli.containers.stop.assert_called_once()
        self.assertIn(["volume", "rm", "onionpress-db-data"], self.runs)
        self.assertIn(["volume", "rm", "onionpress-onion-keys"], self.runs)
        self.assertEqual(sorted(os.listdir(self.dir)), sorted(["shared", "logs"])
                         if os.path.isdir(os.path.join(self.dir, "logs")) else ["shared"])
        self.assertFalse(os.path.exists(os.path.dirname(self.vanity)))
        self.assertIn("Removed volume: onionpress-db-data", self.cli.logged)

    def test_ctrl_c_at_the_question_cancels(self):
        self.tty()
        with mock.patch("builtins.input", side_effect=KeyboardInterrupt):
            self.assertEqual(self.cli.cmd_reset(), 1)
        self.assertIn("Cancelled.", self.out.getvalue())
        self.cli.containers.stop.assert_not_called()
        self.assertTrue(os.path.exists(os.path.join(self.dir, "secrets")))

    def test_a_hub_keeping_the_key_is_asked_about_and_the_record_stays(self):
        left = [{"content_address": ADDR, "onionheaven_address": HUB, "why": "unanswered"}]
        self.release.return_value = left
        self.tty(["", "y"])
        self.assertEqual(self.cli.cmd_reset(), 0)
        self.cli.containers.stop.assert_called_once()
        self.assertTrue(os.path.exists(os.path.join(self.dir, onionheaven.REGISTRATION_RECORD)))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "secrets")))
        self.out.truncate(0)
        self.out.seek(0)
        self.assertEqual(self.cli.cmd_reset(json_output=True, yes=True), 0)
        self.assertEqual(self.answer(), {"ok": True, "remaining": left})
        self.assertIs(self.release.call_args.kwargs["out"], sys.stderr)

    def test_an_interrupted_hub_question_cancels(self):
        self.release.return_value = [{"content_address": ADDR, "onionheaven_address": HUB,
                                      "why": "busy"}]
        self.tty([""])
        with mock.patch("builtins.input", side_effect=["", EOFError]):
            self.assertEqual(self.cli.cmd_reset(), 1)
        self.cli.containers.stop.assert_not_called()

    def test_a_hub_that_cannot_be_asked_again_is_no_question(self):
        self.release.return_value = [{"content_address": ADDR, "onionheaven_address": HUB,
                                      "why": "no-key"}]
        self.tty([""])
        self.assertEqual(self.cli.cmd_reset(), 0)
        self.cli.containers.stop.assert_called_once()

    def test_check_asks_only_reset_when_no_hub_holds_a_key(self):
        self.assertEqual(self.cli.cmd_reset(check=True, json_output=True), 4)
        doc = self.answer()
        self.assertEqual(doc["confirm"], "reset")
        self.assertEqual(len(doc["deletes"]), 3)
        self.cli.containers.stop.assert_not_called()
        self.assertEqual(self.cli.cmd_reset(check=True, json_output=True,
                                            confirmed=frozenset({"reset"})), 0)
        self.assertEqual(self.answer(), {"ok": True, "running": False})

    def test_a_hub_holding_the_key_while_tor_runs_is_no_question(self):
        self.held.return_value = [(ADDR, HUB)]
        self.cli.docker.container_running.return_value = True
        self.assertEqual(self.cli.cmd_reset(check=True, json_output=True), 4)
        self.assertEqual(self.answer()["confirm"], "reset")

    def test_yes_skips_the_questions_and_a_failed_volume_list_removes_none(self):
        self.no_tty()
        self.cli.docker.run.side_effect = None
        self.cli.docker.run.return_value = DockerResult(1, "", "daemon down")
        self.assertEqual(self.cli.cmd_reset(yes=True), 0)
        self.assertFalse(any("Removed volume" in m for m in self.cli.logged))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "config")))

    def test_a_failed_stop_does_not_stop_the_wipe(self):
        self.cli.containers.stop.side_effect = RuntimeError("compose")
        self.assertEqual(self.cli.cmd_reset(yes=True), 0)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "secrets")))


# ---------------------------------------------------------------------------
# the Settings page's backup and restore, the welcome screen's staging
# ---------------------------------------------------------------------------

class TestSettingsPageBackup(Sandbox):

    def setUp(self):
        super().setUp()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        p = self.stack.enter_context
        p(mock.patch("onionpress.backup.served_onion_address", return_value=ADDR))
        p(mock.patch("onionpress.backup.get_admin_username", return_value="owner"))
        self.create = p(mock.patch("onionpress.backup.create_backup",
                                   return_value={"excludes_onion_key": True}))

    def _backup(self, json_output=True):
        return cli.cmd_settings_page_backup("/out.zip", "pw", self.dir, json_output)

    def test_what_the_page_gets(self):
        self.assertEqual(self._backup(), 0)
        self.assertEqual(self.answer(), {"ok": True, "note": ONION_KEY_LEFT_OUT_NOTE})
        args, kw = self.create.call_args
        self.assertEqual(args[:4], (ADDR, "owner", "pw", "/out.zip"))
        self.assertEqual(args[4], cli.__version__)
        self.assertEqual(kw, {"include_hub_data": False, "include_host_secrets": False,
                              "include_onion_key": False})

    def test_an_older_backup_without_the_flag_has_no_note(self):
        self.create.return_value = {}
        self.assertEqual(self._backup(), 0)
        self.assertEqual(self.answer(), {"ok": True})
        self.assertEqual(self._backup(json_output=False), 0)

    def test_no_address_is_unknown(self):
        with mock.patch("onionpress.backup.served_onion_address", return_value=""):
            self.assertEqual(self._backup(), 0)
        self.assertEqual(self.create.call_args.args[0], "unknown")

    def test_refused_on_a_hub(self):
        self.create.side_effect = SettingsPageBackupRefused("this node is a hub")
        self.assertEqual(self._backup(), 3)
        self.assertEqual(self.answer()["error"], "this node is a hub")

    def test_a_failure(self):
        self.create.side_effect = RuntimeError("disk full")
        self.assertEqual(self._backup(), 1)
        self.assertEqual(self.answer()["error"], "disk full")
        with open(resolve_paths(data_dir=self.dir).log_file) as f:
            self.assertIn("Settings page backup failed: disk full", f.read())


class TestSettingsPageRestore(Sandbox):
    """The Mac's: the address-change question and the hub release. Linux
    refuses the change instead (tests/test_settings_page_onion_key.py),
    so the OS is pinned here."""

    def _restore(self, confirmed=frozenset(), json_output=True):
        with mock.patch("onionpress.cli.detect_os", return_value=cli.OS.MACOS):
            return cli.cmd_settings_page_restore("/up.zip", "pw", self.dir, json_output, confirmed)

    def test_the_hubs_are_asked_before_the_key_changes_and_a_refusal_is_logged(self):
        seen = {}

        def restore(zip_path, password, log, *, through_wordpress, before_key_change,
                    confirm_address_change):
            seen["through"] = through_wordpress
            seen["confirmed"] = confirm_address_change(ADDR, OTHER)
            before_key_change(OTHER)
        with mock.patch("onionpress.backup.restore_from_backup", side_effect=restore), \
                mock.patch("onionpress.cli.release_onionheaven",
                           side_effect=RuntimeError("tor down")) as release:
            self.assertEqual(self._restore(confirmed=frozenset({"address-change"})), 0)
        self.assertEqual(self.answer(), {"ok": True})
        self.assertTrue(seen["through"] and seen["confirmed"])
        self.assertEqual(release.call_args.args[0], "restore")
        self.assertEqual(release.call_args.kwargs["keep_address"], OTHER)
        with open(resolve_paths(data_dir=self.dir).log_file) as f:
            self.assertIn("could not ask OnionHeaven to delete the old key (continuing): tor down",
                          f.read())

    def test_the_address_question_is_refused_without_the_confirmation(self):
        def restore(zip_path, password, log, *, confirm_address_change, **kw):
            confirm_address_change(ADDR, OTHER)
        with mock.patch("onionpress.backup.restore_from_backup", side_effect=restore):
            self.assertEqual(self._restore(), 3)
        doc = self.answer()
        self.assertEqual(doc["confirm"], "address-change")
        self.assertEqual((doc["current"], doc["new"]), (ADDR, OTHER))

    def test_refused_and_failed(self):
        with mock.patch("onionpress.backup.restore_from_backup",
                        side_effect=SettingsPageRestoreRefused("a hub's keys")):
            self.assertEqual(self._restore(), 3)
        self.assertEqual(self.answer()["error"], "a hub's keys")
        with mock.patch("onionpress.backup.restore_from_backup",
                        side_effect=RuntimeError("bad zip")):
            self.assertEqual(self._restore(), 1)
        self.assertEqual(self.answer()["error"], "bad zip")


class TestPrepareRestore(Sandbox):

    def _prepare(self, meta):
        with mock.patch("onionpress.backup.prepare_install_from_backup",
                        return_value=("/stg", meta)) as prepare:
            rc = cli.cmd_prepare_restore("/b.zip", "pw", self.dir, True)
        self.assertEqual(prepare.call_args.kwargs, {"data_dir": self.dir})
        return rc

    def test_a_plain_backup(self):
        self.assertEqual(self._prepare({"onion_address": ADDR}), 0)
        self.assertEqual(self.answer(), {"ok": True, "onion_address": ADDR,
                                         "onionheaven_data": None})

    def test_sealed_hub_data_is_named_as_left_out(self):
        meta = {"onion_address": ADDR, "onionheaven_data": "sealed",
                "onionheaven_recovery_key_id": KID}
        self.assertEqual(self._prepare(meta), 0)
        doc = self.answer()
        self.assertEqual(doc["onionheaven_data"], "sealed")
        self.assertEqual(doc["note"], sealed_skipped_message(meta))
        self.assertIn(KID, doc["note"])

    def test_a_failure_is_the_error_on_both_streams(self):
        with mock.patch("onionpress.backup.prepare_install_from_backup",
                        side_effect=lambda zp, pw, log, **kw: (log("extracting"),
                                                               (_ for _ in ()).throw(
                                                                   ValueError("wrong password")))):
            self.assertEqual(cli.cmd_prepare_restore("/b.zip", "pw", self.dir, True), 1)
        self.assertEqual(self.answer()["error"], "wrong password")
        self.assertIn("extracting", self.err.getvalue())
        with open(resolve_paths(data_dir=self.dir).log_file) as f:
            text = f.read()
        self.assertIn("extracting", text)
        self.assertIn("prepare failed: wrong password", text)
        self.assertEqual(cli.cmd_prepare_restore("/b.zip", "pw", self.dir, False), 1)
        self.assertIn("ERROR: ", self.err.getvalue())


class TestPrepareRestoreCheck(Sandbox):
    """`prepare-restore --check` (issue #475): the welcome screen's
    backup opened by the CLI, nothing written."""

    def _check(self, **kw):
        with mock.patch("onionpress.backup.peek_backup_metadata", **kw) as peek, \
                mock.patch("onionpress.backup.prepare_install_from_backup") as prepare:
            rc = cli.cmd_prepare_restore_check("/b.zip", "pw", True)
        prepare.assert_not_called()
        if peek.called:
            peek.assert_called_once_with("/b.zip", "pw")
        return rc

    def test_a_backup_with_its_key(self):
        self.assertEqual(self._check(return_value={"onion_address": ADDR}), 0)
        self.assertEqual(self.answer(), {"ok": True, "onion_address": ADDR,
                                         "excludes_onion_key": False})

    def test_a_settings_page_backup_says_its_key_is_left_out(self):
        meta = {"onion_address": ADDR, "excludes_onion_key": True}
        self.assertEqual(self._check(return_value=meta), 0)
        self.assertIs(self.answer()["excludes_onion_key"], True)

    def test_only_a_true_excludes_and_a_text_address_pass(self):
        meta = {"onion_address": 7, "excludes_onion_key": "yes"}
        self.assertEqual(self._check(return_value=meta), 0)
        self.assertEqual(self.answer(), {"ok": True, "onion_address": "",
                                         "excludes_onion_key": False})

    def test_a_wrong_password_is_the_error(self):
        self.assertEqual(self._check(side_effect=ValueError("wrong password")), 1)
        self.assertIn("wrong password", self.answer()["error"])
        self.assertEqual(cli.cmd_prepare_restore_check("/nonexistent.zip", "pw", False), 1)
        self.assertIn("ERROR: ", self.err.getvalue())


# ---------------------------------------------------------------------------
# main(): what the dispatch census leaves
# ---------------------------------------------------------------------------

class TestMainEdges(Sandbox):

    def test_onionname_without_a_subcommand_is_the_help(self):
        with mock.patch("onionpress.cli.Docker"):
            self.assertEqual(cli.main(["--data-dir", self.dir, "onionname"]), 1)
        self.assertIn("usage: onionpress onionname", self.out.getvalue())

    def test_a_grouping_command_without_a_subcommand_is_the_help(self):
        for argv in (["config"], ["hub-recovery-key"]):
            with self.subTest(argv=argv):
                self.assertEqual(cli.main(["--data-dir", self.dir, *argv]), 1)
                self.assertIn(f"usage: onionpress {argv[0]}", self.out.getvalue())


if __name__ == "__main__":
    unittest.main()

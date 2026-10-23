#!/usr/bin/env python3
"""The check/confirm protocol (onionpress.confirm, issue #366): every
destructive command checks with `--check --json` and answers {"ok": true},
{"confirm": "<what>", ...facts} or {"error"}; the real run refuses past
the check without `--confirmed <what>` (or $ONIONPRESS_CONFIRMED). Each
flow runs here against stubs: config set ADDRESS_PREFIX (prefix-change),
restore (sealed-hub-data-left-out), reset and uninstall (hub-holds-key,
then the command's own question), the Settings page's restore of another
address and the Mac launcher's import-key (address-change; the launcher
itself in tests/test_onion_key_replacement.py), and backup --check.
No docker: DOCKER_HOST points at nothing and every container call is
stubbed."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from onionpress import backup, cli, confirm, config as op_config, uninstall  # noqa: E402
from onionpress.cli import OnionPressCLI  # noqa: E402
from onionpress.platform import resolve_paths  # noqa: E402

ADDR = "a" * 56 + ".onion"
OTHER = "b" * 56 + ".onion"
HUB = "h" * 56 + ".onion"
NO_DOCKER = "unix:///var/empty/no-docker-for-tests.sock"


def _read(rel):
    with open(os.path.join(PROJECT_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _last_json(text):
    if hasattr(text, "getvalue"):
        text = text.getvalue()
    return json.loads(text.strip().splitlines()[-1])


class TestConfirmModule(unittest.TestCase):

    def test_given_from_flags_and_environment(self):
        args = mock.Mock(confirmed=["uninstall"])
        self.assertEqual(confirm.given(args, {}), frozenset({"uninstall"}))
        self.assertEqual(confirm.given(args, {confirm.CONFIRMED_ENV: "reset, address-change bogus"}),
                         frozenset({"uninstall", "reset", "address-change"}))
        self.assertEqual(confirm.given(None, {}), frozenset())

    def test_require_raises_the_question_with_its_facts(self):
        confirm.require("reset", {"reset"})
        with self.assertRaises(confirm.NeedsConfirmation) as cm:
            confirm.require("address-change", set(), current=ADDR, new=OTHER)
        self.assertEqual(cm.exception.answer(),
                         {"confirm": "address-change", "current": ADDR, "new": OTHER})
        with self.assertRaises(ValueError):
            confirm.NeedsConfirmation("format-disk")

    def test_answers(self):
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(confirm.answer_ok(True, out, filename="x"), 0)
        self.assertEqual(confirm.answer_ok(False, out, text="done"), 0)
        exc = confirm.NeedsConfirmation("uninstall", deletes=["everything"])
        self.assertEqual(confirm.answer_confirm(exc, True, out, err), confirm.EXIT_CONFIRM)
        self.assertEqual(confirm.answer_confirm(exc, False, out, err), confirm.EXIT_CONFIRM)
        self.assertEqual(confirm.answer_error("no", True, out, err), 1)
        self.assertEqual(confirm.answer_error("no", False, out, err, code=confirm.EXIT_REFUSED), 3)
        self.assertEqual(confirm.answer_ask("password", True, out, err, backup_user="me"),
                         confirm.EXIT_ASK)
        lines = out.getvalue().splitlines()
        self.assertEqual(json.loads(lines[0]), {"ok": True, "filename": "x"})
        self.assertEqual(lines[1], "done")
        self.assertEqual(json.loads(lines[2]), {"confirm": "uninstall", "deletes": ["everything"]})
        self.assertTrue(lines[3].startswith("CONFIRM uninstall:"))
        self.assertEqual(json.loads(lines[4]), {"error": "no"})
        self.assertEqual(json.loads(lines[5]), {"ask": "password", "backup_user": "me"})
        self.assertIn("--confirmed uninstall", err.getvalue())
        self.assertIn("ERROR: no", err.getvalue())

    def test_last_json_line(self):
        self.assertEqual(confirm.last_json_line('log line\n{"ok": true}\n'), {"ok": True})
        self.assertEqual(confirm.last_json_line('{\n  "a": 1\n}\n'), {"a": 1})
        self.assertIsNone(confirm.last_json_line("nothing"))
        self.assertIsNone(confirm.last_json_line('{"a": 1}\nthen text'))
        self.assertIsNone(confirm.last_json_line("[1, 2]"))

    def test_every_question_has_a_dialog_in_the_menubar(self):
        menubar = _read("src/menubar.py")
        asked = menubar[menubar.index("def _ask_cli_question("):menubar.index("def _ask_to_replace_address(")]
        for what in confirm.QUESTIONS:
            with self.subTest(what=what):
                if what == "sealed-hub-data-left-out":
                    # Its dialog takes the key too: _ask_hub_recovery_key
                    self.assertIn('"sealed-hub-data-left-out"', menubar)
                else:
                    self.assertIn(f'what == "{what}"', asked)
        # Cancel is the default in every dialog (tests/test_menubar_actions.py
        # runs them)
        self.assertNotIn("default_button=1", asked)

    def test_the_parser_refuses_an_unknown_question(self):
        with mock.patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.main(["uninstall", "--confirmed", "format-disk"])


class TestConfigSetPrefixChange(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="op-confirm-prefix-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        with open(os.path.join(self.dir, "config"), "w") as f:
            f.write("ADDRESS_PREFIX=aaa\n")
        with open(os.path.join(self.dir, "onion_address"), "w") as f:
            f.write(ADDR + "\n")
        p = mock.patch.object(op_config, "apply_side_effects", return_value=[])
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop(confirm.CONFIRMED_ENV, None)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(sys, "stdin", io.StringIO()):
            rc = cli.main(["--data-dir", self.dir, "config", "set", *args])
        return rc, out.getvalue(), err.getvalue()

    def prefix(self):
        return op_config.read_value(os.path.join(self.dir, "config"), "ADDRESS_PREFIX")

    def test_check_asks_and_changes_nothing(self):
        rc, out, _ = self.run_cli("ADDRESS_PREFIX", "op2", "--check", "--json")
        self.assertEqual(rc, confirm.EXIT_CONFIRM)
        self.assertEqual(_last_json(out), {"confirm": "prefix-change", "current": "aaa",
                                           "new": "op2", "address": ADDR})
        self.assertEqual(self.prefix(), "aaa")

    def test_the_run_refuses_without_the_confirmation(self):
        """Refused is exit 3 (4 is --check's); the answer still names the
        question so a client that skipped --check can ask it."""
        rc, out, err = self.run_cli("ADDRESS_PREFIX", "op2")
        self.assertEqual(rc, confirm.EXIT_REFUSED)
        self.assertIn("--confirmed prefix-change", err)
        self.assertEqual(self.prefix(), "aaa")
        rc, out, _ = self.run_cli("ADDRESS_PREFIX", "op2", "--json")
        self.assertEqual(rc, confirm.EXIT_REFUSED)
        ans = _last_json(out)
        self.assertEqual(ans["confirm"], "prefix-change")
        self.assertIn("--confirmed prefix-change", ans["error"])
        self.assertEqual(self.prefix(), "aaa")

    def test_confirmed_by_flag_or_environment(self):
        rc, out, _ = self.run_cli("ADDRESS_PREFIX", "op2", "--confirmed", "prefix-change", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual(_last_json(out)["changed"], True)
        self.assertEqual(self.prefix(), "op2")
        with mock.patch.dict(os.environ, {confirm.CONFIRMED_ENV: "prefix-change"}):
            rc, out, _ = self.run_cli("ADDRESS_PREFIX", "zzz")
        self.assertEqual(rc, 0)
        self.assertEqual(self.prefix(), "zzz")

    def test_a_prefix_the_address_has_needs_no_question(self):
        rc, out, _ = self.run_cli("ADDRESS_PREFIX", "aa", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual(self.prefix(), "aa")
        rc, out, _ = self.run_cli("ADDRESS_PREFIX", "", "--check", "--json")
        # "" means the default, op2, which this address lacks
        self.assertEqual(rc, confirm.EXIT_CONFIRM)

    def test_no_known_address_asks_too(self):
        """QA on 2026-10-22: a headless start on a not-yet-installed site
        leaves no onion_address; the prefix is then asked about, never ok."""
        os.remove(os.path.join(self.dir, "onion_address"))
        rc, out, _ = self.run_cli("ADDRESS_PREFIX", "op2", "--check", "--json")
        self.assertEqual(rc, confirm.EXIT_CONFIRM)
        self.assertEqual(_last_json(out), {"confirm": "prefix-change", "current": "aaa",
                                           "new": "op2", "address": ""})
        rc, out, _ = self.run_cli("ADDRESS_PREFIX", "op2", "--json")
        self.assertEqual(rc, confirm.EXIT_REFUSED)
        self.assertEqual(self.prefix(), "aaa")
        rc, out, _ = self.run_cli("ADDRESS_PREFIX", "op2", "--confirmed", "prefix-change", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual(self.prefix(), "op2")
        # The prefix the file already has asks nothing
        rc, out, _ = self.run_cli("ADDRESS_PREFIX", "op2", "--check", "--json")
        self.assertEqual((rc, _last_json(out)["ok"]), (0, True))

    def test_unset_takes_json_too(self):
        rc, out, _ = self.run_cli("PREVENT_SLEEP", "never", "--json")
        self.assertEqual(rc, 0)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["--data-dir", self.dir, "config", "unset", "PREVENT_SLEEP", "--json"])
        self.assertEqual(rc, 0)
        ans = _last_json(out)
        self.assertEqual((ans["ok"], ans["key"], ans["changed"]), (True, "PREVENT_SLEEP", True))
        self.assertEqual(op_config.read_value(os.path.join(self.dir, "config"), "PREVENT_SLEEP", ""), "")

    def test_the_log_names_the_effective_default_for_an_unset_key(self):
        change = op_config.set_config_value(os.path.join(self.dir, "config"), "PREVENT_SLEEP", "never")
        self.assertEqual(change.describe(), "PREVENT_SLEEP: 'normal' (unset) → 'never'")
        change = op_config.set_config_value(os.path.join(self.dir, "config"), "PREVENT_SLEEP", "normal")
        self.assertEqual(change.describe(), "PREVENT_SLEEP: 'never' → 'normal'")

    def test_other_keys_check_and_json(self):
        rc, out, _ = self.run_cli("PREVENT_SLEEP", "never", "--check", "--json")
        self.assertEqual((rc, _last_json(out)), (0, {"ok": True, "key": "PREVENT_SLEEP"}))
        self.assertEqual(op_config.read_value(os.path.join(self.dir, "config"), "PREVENT_SLEEP", ""), "")
        rc, out, _ = self.run_cli("PREVENT_SLEEP", "bogus", "--json")
        self.assertEqual(rc, 2)
        self.assertIn("error", _last_json(out))


class _Sealed(unittest.TestCase):
    """A restore's metadata as cmd_restore_check reads it."""

    def _check(self, metadata, key="", confirmed=frozenset()):
        obj = object.__new__(OnionPressCLI)
        obj.log = lambda m: None
        out = io.StringIO()
        with mock.patch.object(backup, "read_backup_metadata", return_value=metadata), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = obj.cmd_restore_check("pw", "/x/b.zip", key, confirmed, True)
        return rc, _last_json(out)


class TestRestoreCheck(_Sealed):

    META = {"onion_address": ADDR, "backup_date": "2026-10-01T12:00:00Z",
            "onionpress_version": "2.5.6", "username": "admin"}

    def test_the_facts(self):
        rc, ans = self._check(self.META)
        self.assertEqual(rc, 0)
        self.assertEqual(ans, {"ok": True, "onion_address": ADDR, "backup_date": "2026-10-01T12:00:00Z",
                               "username": "admin", "onionpress_version": "2.5.6",
                               "excludes_onion_key": False, "onionheaven_data": None})

    def test_a_keyless_backup_says_so(self):
        rc, ans = self._check({**self.META, "excludes_onion_key": True})
        self.assertEqual((rc, ans["excludes_onion_key"]), (0, True))

    def test_sealed_asks_unless_a_key_or_the_confirmation_is_given(self):
        meta = {**self.META, "onionheaven_data": "sealed", "onionheaven_recovery_key_id": "0" * 16}
        rc, ans = self._check(meta)
        self.assertEqual(rc, confirm.EXIT_CONFIRM)
        self.assertEqual(ans["confirm"], "sealed-hub-data-left-out")
        self.assertEqual(ans["key_id"], "0" * 16)
        self.assertEqual(ans["onion_address"], ADDR)
        rc, ans = self._check(meta, confirmed=frozenset({"sealed-hub-data-left-out"}))
        self.assertEqual((rc, ans["ok"]), (0, True))
        rc, ans = self._check(meta, key="not a key")
        self.assertEqual(rc, 1)
        self.assertIn("error", ans)

    def test_a_wrong_password_is_the_error(self):
        obj = object.__new__(OnionPressCLI)
        obj.log = lambda m: None
        out = io.StringIO()
        with mock.patch.object(backup, "read_backup_metadata", side_effect=ValueError("Wrong password")), \
                contextlib.redirect_stdout(out):
            rc = obj.cmd_restore_check("pw", "/x/b.zip", "", frozenset(), True)
        self.assertEqual((rc, _last_json(out)), (1, {"error": "Wrong password"}))

    def test_without_a_password_the_cli_asks_for_one(self):
        out = io.StringIO()
        env = {k: v for k, v in os.environ.items() if k != "ONIONPRESS_BACKUP_PASSWORD"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(sys, "stdin", io.StringIO()), \
                mock.patch("onionpress.cli.OnionPressCLI") as M, \
                contextlib.redirect_stdout(out):
            rc = cli.main(["restore", "/x/OnionPress-abcdefgh-bob-2026-10-22-12-00.zip",
                           "--check", "--json"])
        self.assertEqual(rc, confirm.EXIT_ASK)
        self.assertEqual(_last_json(out), {"ask": "password", "backup_user": "bob"})
        M.assert_not_called()


class TestSettingsPageRestoreAddressChange(unittest.TestCase):
    """cli.cmd_settings_page_restore: restore_from_backup's
    confirm_address_change callback stops at the question (exit 4) unless
    --confirmed address-change was given; a refusal is exit 3."""

    def _run(self, confirmed, restore):
        out = io.StringIO()
        with mock.patch.object(backup, "restore_from_backup", side_effect=restore), \
                mock.patch.object(cli, "release_onionheaven", return_value=[]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = cli.cmd_settings_page_restore("/x/upload.zip", "pw", tempfile.gettempdir(),
                                               True, confirmed)
        return rc, _last_json(out)

    def test_the_question_then_the_confirmed_run(self):
        seen = []

        def restore(zip_path, password, log, **kw):
            seen.append(kw)
            self.assertTrue(kw["through_wordpress"])
            self.assertTrue(kw["confirm_address_change"](ADDR, OTHER))
            kw["before_key_change"](OTHER)
            return {}
        rc, ans = self._run(frozenset(), restore)
        self.assertEqual(rc, confirm.EXIT_REFUSED)
        self.assertEqual((ans["confirm"], ans["current"], ans["new"]),
                         ("address-change", ADDR, OTHER))
        self.assertIn("--confirmed address-change", ans["error"])
        rc, ans = self._run(frozenset({"address-change"}), restore)
        self.assertEqual((rc, ans), (0, {"ok": True}))
        self.assertEqual(len(seen), 2)

    def test_a_refusal_is_exit_3(self):
        def restore(*a, **kw):
            raise backup.SettingsPageRestoreRefused(backup.HUB_DATA_RESTORE_REFUSED_MSG)
        rc, ans = self._run(frozenset(), restore)
        self.assertEqual((rc, ans), (3, {"error": backup.HUB_DATA_RESTORE_REFUSED_MSG}))

    def test_a_failure_is_exit_1(self):
        def restore(*a, **kw):
            raise ValueError("Wrong password")
        rc, ans = self._run(frozenset(), restore)
        self.assertEqual((rc, ans), (1, {"error": "Wrong password"}))

    def test_without_a_password_nothing_runs(self):
        out = io.StringIO()
        env = {k: v for k, v in os.environ.items() if k != "ONIONPRESS_BACKUP_PASSWORD"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(sys, "stdin", io.StringIO()), \
                mock.patch.object(backup, "restore_from_backup") as restore, \
                contextlib.redirect_stdout(out):
            rc = cli.main(["settings-page-restore", "/x/upload.zip", "--json"])
        self.assertEqual(rc, 1)
        restore.assert_not_called()


class TestUninstallCheck(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="op-confirm-uninstall-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.paths = resolve_paths(data_dir=self.dir, app_bundle="")
        self.docker = mock.Mock()
        self.docker.container_running.return_value = False
        self.hubs = [(ADDR, HUB)]
        p = mock.patch.object(uninstall, "hubs_holding_key", side_effect=lambda *a: self.hubs)
        p.start()
        self.addCleanup(p.stop)
        self.ran = mock.patch.object(uninstall, "run", return_value=[])
        self.run_mock = self.ran.start()
        self.addCleanup(self.ran.stop)

    def _cmd(self, *, check=False, confirmed=frozenset()):
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO()), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = cli.cmd_uninstall(self.paths, self.docker, lambda m: None, "/x/onionpress",
                                   check=check, json_output=True, confirmed=confirmed)
        return rc, _last_json(out)

    def test_the_questions_in_order(self):
        rc, ans = self._cmd(check=True)
        self.assertEqual(rc, confirm.EXIT_CONFIRM)
        self.assertEqual(ans, {"confirm": "hub-holds-key",
                               "hubs": [{"content_address": ADDR, "onionheaven_address": HUB}]})
        rc, ans = self._cmd(check=True, confirmed=frozenset({"hub-holds-key"}))
        self.assertEqual(rc, confirm.EXIT_CONFIRM)
        self.assertEqual(ans["confirm"], "uninstall")
        self.assertEqual(ans["deletes"], list(uninstall.DELETES))
        rc, ans = self._cmd(check=True, confirmed=frozenset({"hub-holds-key", "uninstall"}))
        self.assertEqual((rc, ans["ok"]), (0, True))
        self.run_mock.assert_not_called()

    def test_running_or_no_hub_skips_the_hub_question(self):
        self.docker.container_running.return_value = True
        rc, ans = self._cmd(check=True)
        self.assertEqual(ans["confirm"], "uninstall")
        self.docker.container_running.return_value = False
        self.hubs = []
        rc, ans = self._cmd(check=True)
        self.assertEqual(ans["confirm"], "uninstall")

    def test_the_run_refuses_without_the_confirmations(self):
        for confirmed in (frozenset(), frozenset({"uninstall"}), frozenset({"hub-holds-key"})):
            with self.subTest(confirmed=sorted(confirmed)):
                rc, ans = self._cmd(confirmed=confirmed)
                self.assertEqual(rc, confirm.EXIT_REFUSED)
                self.assertIn("--confirmed", ans["error"])
                self.run_mock.assert_not_called()
        rc, ans = self._cmd(confirmed=frozenset({"hub-holds-key", "uninstall"}))
        self.assertEqual((rc, ans), (0, {"ok": True, "remaining": []}))
        self.run_mock.assert_called_once()

    def test_linux_has_its_own(self):
        out = io.StringIO()
        with mock.patch("onionpress.cli.detect_os", return_value=mock.Mock(value="linux")), \
                contextlib.redirect_stdout(out):
            rc = cli.main(["--data-dir", self.dir, "uninstall", "--json"])
        self.assertEqual(rc, 1)
        self.assertIn("Linux launcher", _last_json(out)["error"])


class TestUninstallRun(unittest.TestCase):
    """uninstall.run: the release, the stop through the launcher, the
    Colima VM, the login item, the data directory; in that order."""

    def test_the_order(self):
        tmp = tempfile.mkdtemp(prefix="op-uninstall-run-")
        self.addCleanup(shutil.rmtree, tmp, True)
        data_dir = os.path.join(tmp, "data")
        os.makedirs(os.path.join(data_dir, "colima"))
        paths = resolve_paths(data_dir=data_dir, app_bundle="")
        calls = []

        def run(argv, **kwargs):
            calls.append(argv[:2])
            return mock.Mock(returncode=0)
        with mock.patch.object(uninstall.subprocess, "run", side_effect=run), \
                mock.patch.object(uninstall, "hubs_holding_key", return_value=[(ADDR, HUB)]), \
                mock.patch("onionpress.cli.release_onionheaven", return_value=["left"]) as release, \
                mock.patch.object(uninstall.login_item, "remove", return_value=True) as remove, \
                mock.patch.object(uninstall.op_daemon, "stop_agent", return_value=True) as stop_daemon, \
                mock.patch.object(uninstall.op_daemon, "remove_agent") as remove_agent, \
                mock.patch.object(uninstall.time, "sleep"):
            remaining = uninstall.run(paths, mock.Mock(), lambda m: None, "/x/onionpress")
        self.assertEqual(remaining, ["left"])
        # The daemon (its heartbeat holds the key) stops before the release.
        stop_daemon.assert_called_once()
        self.assertFalse(stop_daemon.call_args.kwargs.get("offline", True))
        remove_agent.assert_called_once()
        release.assert_called_once()
        self.assertEqual(calls[0], ["/x/onionpress", "stop"])
        self.assertEqual([c[1] for c in calls[1:3]], ["stop", "delete"])
        self.assertEqual(calls[3], ["pkill", "-f"])
        remove.assert_called_once()
        self.assertFalse(os.path.exists(data_dir))


class TestBackupCheck(unittest.TestCase):

    def test_check_verifies_and_names_the_file_without_a_backup(self):
        obj = object.__new__(OnionPressCLI)
        obj.paths = resolve_paths(data_dir=tempfile.gettempdir(), app_bundle="")
        obj.log = lambda m: None
        obj.containers = mock.Mock()
        obj.containers.get_onion_address.return_value = ADDR
        out = io.StringIO()
        with mock.patch.object(backup, "verify_wp_admin", return_value=(True, None)), \
                mock.patch.object(backup, "served_onion_address", return_value=""), \
                mock.patch.object(backup, "create_backup") as create, \
                contextlib.redirect_stdout(out):
            rc = obj.cmd_backup("pw", "/x/out.zip", "admin", check=True, json_output=True)
        self.assertEqual(rc, 0)
        ans = _last_json(out)
        self.assertEqual((ans["ok"], ans["address"], ans["username"], ans["output"]),
                         (True, ADDR, "admin", "/x/out.zip"))
        self.assertTrue(ans["filename"].startswith("OnionPress-aaaaaaaa-admin-"))
        create.assert_not_called()
        out = io.StringIO()
        with mock.patch.object(backup, "verify_wp_admin", return_value=(False, "Wrong password")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = obj.cmd_backup("pw", None, "admin", check=True, json_output=True)
        self.assertEqual((rc, _last_json(out)), (1, {"error": "Wrong password"}))


class TestLauncherDelegations(unittest.TestCase):
    """The Mac launcher's destructive commands are the CLI's, flags and all."""

    def test_mac_cases(self):
        mac = _read("app/MacOS/onionpress")
        for cmd, tail in (("uninstall", 'uninstall "${@:2}"'),
                          ("update-images", 'update-images "${@:2}"'),
                          ("update", 'update "${@:2}"'),
                          ("reset", 'reset "${reset_args[@]}"')):
            with self.subTest(cmd=cmd):
                start = mac.index(f"\n        {cmd})\n")
                case = mac[start:mac.index("\n            ;;\n", start)]
                self.assertIn(f'bundled_python -m onionpress.cli --data-dir "$DATA_DIR" {tail}', case)
        self.assertIn('[ "${ONIONPRESS_YES:-}" != "true" ] || reset_args+=(--yes)', mac)
        self.assertIn('ONIONPRESS_APP_PATH="$(dirname "$APP_DIR")"', mac)
        help_text = mac[mac.index("help|-h|--help)"):mac.index("Your WordPress site will be available at")]
        for word in ("  uninstall     ", "  update-images ", "handle-action", "--check --json",
                     "--confirmed <what>", "ONIONPRESS_CONFIRMED"):
            self.assertIn(word, help_text)

    def test_no_launcher_takes_a_secret_on_argv_for_them(self):
        for rel in ("app/MacOS/onionpress", "linux/onionpress"):
            text = _read(rel)
            with self.subTest(rel):
                self.assertNotIn('--password "$', text)
                self.assertNotIn("--recovery-key", text)


if __name__ == "__main__":
    unittest.main()

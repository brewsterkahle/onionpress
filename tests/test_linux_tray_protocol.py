#!/usr/bin/env python3
"""The Linux tray speaks the check/confirm protocol for backup and restore
(issue #375, onionpress.confirm).

The tray ran `onionpress backup` and `onionpress restore` straight: a hub's
sealed backup restored without the recovery key was refused off a terminal
(exit 3) and could not be asked about, and the restore had no "what is in
this backup" dialog. Now Restore... runs `restore <zip> --check --json`
first (plan_restore: the password it asks for, the facts for the dialog,
the "sealed-hub-data-left-out" question with a recovery-key dialog) and
Backup... runs `backup --check --json` (check_backup), as the Mac menubar
does. The flow is GTK-free: the dialogs are a RestoreUI the caller
supplies, so it runs here against a stub launcher. The Linux launcher's
`backup)` case hands --check to the shared CLI.
"""

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import types
import unittest
from typing import NamedTuple, Optional
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from onionpress import confirm  # noqa: E402

TRAY = os.path.join(PROJECT_ROOT, "linux", "onionpress-tray")
LAUNCHER = "/opt/onionpress/onionpress"
BACKUP_PASSWORD_ENV = "ONIONPRESS_BACKUP_PASSWORD"
HUB_RECOVERY_KEY_ENV = "ONIONPRESS_HUB_RECOVERY_KEY"
ZIP = "/home/u/OnionPress/backups/OnionPress-aaaaaaaa-admin-2026-10-24.zip"
ADDR = "a" * 56 + ".onion"
PASSWORD = "s3cret-passw0rd"
GOOD_KEY = "good-recovery-key-value"
BAD_KEY = "bad-recovery-key-value"

KEEP = {
    "Launcher", "RestoreUI", "RestorePlan",
    "_loggable", "_last_json_line", "_without_log_stamp", "launcher_failure", "launcher_answer",
    "check_backup", "plan_restore",
    "BACKUP_PASSWORD_ENV", "HUB_RECOVERY_KEY_ENV", "_UNLOGGED_ARGS",
    "SEALED_HUB_DATA_QUESTION", "QUESTION_TIMEOUT_SEC",
}


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _load_tray(fake_subprocess, logs, confirm_module=confirm, launcher_ops_module=None):
    """The protocol client out of the tray script (it imports GTK at
    module level): the Launcher wrapper, the helpers and the flow."""
    tree = ast.parse(_read(TRAY))
    keep = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in KEEP:
            keep.append(node)
        elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in KEEP for t in node.targets):
            keep.append(node)
    ns = {
        "os": os, "json": json, "re": re, "subprocess": fake_subprocess,
        "Optional": Optional, "NamedTuple": NamedTuple,
        "LAUNCHER": LAUNCHER, "log": logs.append, "_confirm": confirm_module,
        "launcher_ops": launcher_ops_module,
    }
    exec(compile(ast.Module(body=keep, type_ignores=[]), TRAY, "exec"), ns)
    return ns


FACTS = {
    "ok": True, "onion_address": ADDR, "backup_date": "2026-10-24 10:00",
    "username": "admin", "onionpress_version": "2.5.6",
    "excludes_onion_key": False, "onionheaven_data": None,
}
SEALED_FACTS = {
    "confirm": "sealed-hub-data-left-out", "key_id": "0123456789abcdef",
    "recovery_key_id": "0123456789abcdef", "onion_address": ADDR,
    "backup_date": "2026-10-24 10:00", "username": "admin",
    "onionpress_version": "2.5.6", "excludes_onion_key": False,
    "onionheaven_data": "sealed",
}


class _StubLauncher:
    """Answers each `onionpress restore <zip> --check --json` from the
    environment it was given, as the CLI would."""

    def __init__(self):
        self.calls = []       # (argv, env or None, stdin)
        self.without_password = (5, {"ask": "password", "backup_user": "admin"})
        self.with_password = (0, FACTS)
        self.with_key = {GOOD_KEY: (0, {**FACTS, "recovery_key": "accepted"}),
                         BAD_KEY: (1, {"error": "this is not the key the backup was sealed to"})}
        self.backup = (0, {"ok": True, "filename": "OnionPress-aaaaaaaa-admin-2026-10-24.zip",
                           "output": ZIP, "address": ADDR, "username": "admin"})
        self.raw = None       # (rc, stdout, stderr) answers everything when set

    def run(self, argv, **kw):
        env = kw.get("env")
        self.calls.append((list(argv), env, kw.get("stdin")))
        if self.raw is not None:
            rc, out, err = self.raw
            return subprocess.CompletedProcess(argv, rc, out, err)
        pw = (env or {}).get(BACKUP_PASSWORD_ENV)
        key = (env or {}).get(HUB_RECOVERY_KEY_ENV)
        if argv[1] == "backup":
            rc, ans = self.backup
        elif not pw:
            rc, ans = self.without_password
        elif key:
            rc, ans = self.with_key.get(key, (1, {"error": "unknown key"}))
        else:
            rc, ans = self.with_password
        return subprocess.CompletedProcess(argv, rc, json.dumps(ans) + "\n", "")


class _UI:
    """A RestoreUI that records the dialogs shown and answers as told."""

    def __init__(self, password=PASSWORD, confirm=True, keys=()):
        self.shown = []
        self.password = password
        self.confirmed = confirm
        self.keys = list(keys)
        self.errors = []
        self.facts = None
        self.problems = []

    def ask_password(self, backup_user):
        self.shown.append(("ask_password", backup_user))
        return self.password

    def confirm(self, facts):
        self.shown.append(("confirm", facts.get("onion_address")))
        self.facts = facts
        return self.confirmed

    def ask_recovery_key(self, facts, problem):
        self.shown.append(("ask_recovery_key", facts.get("recovery_key_id")))
        self.problems.append(problem)
        return self.keys.pop(0) if self.keys else None

    def error(self, title, message):
        self.shown.append(("error", title))
        self.errors.append((title, message))


class _Base(unittest.TestCase):

    def setUp(self):
        self.logs = []
        self.stub = _StubLauncher()
        fake = types.SimpleNamespace(
            run=self.stub.run, Popen=mock.Mock(), DEVNULL=subprocess.DEVNULL,
            TimeoutExpired=subprocess.TimeoutExpired,
            CompletedProcess=subprocess.CompletedProcess)
        self.ns = _load_tray(fake, self.logs)

    def _checks(self):
        return [c for c in self.stub.calls if "--check" in c[0]]

    def _no_secret_anywhere(self):
        for argv, env, _ in self.stub.calls:
            for secret in (PASSWORD, GOOD_KEY, BAD_KEY):
                self.assertNotIn(secret, argv)
        for line in self.logs:
            for secret in (PASSWORD, GOOD_KEY, BAD_KEY):
                self.assertNotIn(secret, line)


class TestPlanRestore(_Base):

    def test_password_then_facts_then_the_plan(self):
        ui = _UI()
        plan = self.ns["plan_restore"](ZIP, ui)
        self.assertEqual(ui.shown, [("ask_password", "admin"), ("confirm", ADDR)])
        self.assertEqual(plan, self.ns["RestorePlan"]([ZIP, "--json"], PASSWORD, {}))
        self.assertEqual(len(self.stub.calls), 2)
        first, second = self.stub.calls
        self.assertEqual(first[0], [LAUNCHER, "restore", ZIP, "--check", "--json"])
        self.assertIsNone(first[1])                  # no password yet
        self.assertEqual(second[0], first[0])
        self.assertEqual(second[1][BACKUP_PASSWORD_ENV], PASSWORD)
        self.assertNotIn(HUB_RECOVERY_KEY_ENV, second[1])
        for call in self.stub.calls:
            self.assertEqual(call[2], subprocess.DEVNULL)   # no terminal
        self.assertEqual(ui.facts["backup_date"], "2026-10-24 10:00")
        self._no_secret_anywhere()

    def test_the_password_prompt_is_worded_without_a_login_when_the_cli_names_none(self):
        self.stub.without_password = (5, {"ask": "password"})
        ui = _UI()
        self.ns["plan_restore"](ZIP, ui)
        self.assertEqual(ui.shown[0], ("ask_password", None))

    def test_cancel_at_the_password(self):
        ui = _UI(password=None)
        self.assertIsNone(self.ns["plan_restore"](ZIP, ui))
        self.assertEqual(len(self.stub.calls), 1)
        self.assertEqual(ui.shown, [("ask_password", "admin")])

    def test_an_error_is_shown_and_nothing_runs(self):
        self.stub.with_password = (1, {"error": "Wrong password or not a valid OnionPress backup"})
        ui = _UI()
        self.assertIsNone(self.ns["plan_restore"](ZIP, ui))
        self.assertEqual(ui.shown, [("ask_password", "admin"), ("error", "Invalid backup")])
        self.assertEqual(ui.errors, [("Invalid backup", "Wrong password or not a valid OnionPress backup")])
        self.assertEqual(len(self.stub.calls), 2)

    def test_a_launcher_without_an_answer_shows_its_last_error_line(self):
        self.stub.raw = (1, "", "2026-10-24 10:00:00 ERROR: Containers must be running\n")
        ui = _UI()
        self.assertIsNone(self.ns["plan_restore"](ZIP, ui))
        self.assertEqual(ui.errors, [("Invalid backup", "Containers must be running")])

    def test_a_launcher_that_cannot_run_is_an_error_dialog(self):
        self.stub.run = mock.Mock(side_effect=FileNotFoundError(LAUNCHER))
        self.ns["subprocess"].run = self.stub.run
        ui = _UI()
        self.assertIsNone(self.ns["plan_restore"](ZIP, ui))
        self.assertEqual(ui.shown[-1], ("error", "Invalid backup"))
        self.assertTrue(any("could not run" in line for line in self.logs))

    def test_declining_the_confirmation(self):
        ui = _UI(confirm=False)
        self.assertIsNone(self.ns["plan_restore"](ZIP, ui))
        self.assertEqual(ui.shown, [("ask_password", "admin"), ("confirm", ADDR)])

    def test_sealed_hub_data_restored_without_the_key_is_confirmed(self):
        self.stub.with_password = (4, SEALED_FACTS)
        ui = _UI(keys=[""])
        plan = self.ns["plan_restore"](ZIP, ui)
        self.assertEqual(ui.shown, [("ask_password", "admin"), ("confirm", ADDR),
                                    ("ask_recovery_key", "0123456789abcdef")])
        self.assertEqual(plan.args, [ZIP, "--json", "--confirmed", "sealed-hub-data-left-out"])
        self.assertEqual((plan.password, plan.extra_env), (PASSWORD, {}))
        self.assertEqual(len(self.stub.calls), 2)
        self.assertEqual(ui.problems, [None])

    def test_a_refused_key_is_asked_again_and_an_accepted_one_goes_in_the_environment(self):
        self.stub.with_password = (4, SEALED_FACTS)
        ui = _UI(keys=[BAD_KEY, GOOD_KEY])
        plan = self.ns["plan_restore"](ZIP, ui)
        self.assertEqual([s[0] for s in ui.shown],
                         ["ask_password", "confirm", "ask_recovery_key", "ask_recovery_key"])
        self.assertEqual(ui.problems, [None, "this is not the key the backup was sealed to"])
        self.assertEqual(plan.args, [ZIP, "--json"])
        self.assertEqual(plan.extra_env, {HUB_RECOVERY_KEY_ENV: GOOD_KEY})
        key_checks = [c for c in self.stub.calls if c[1] and HUB_RECOVERY_KEY_ENV in c[1]]
        self.assertEqual([c[1][HUB_RECOVERY_KEY_ENV] for c in key_checks], [BAD_KEY, GOOD_KEY])
        for argv, env, _ in key_checks:
            self.assertEqual(argv, [LAUNCHER, "restore", ZIP, "--check", "--json"])
            self.assertEqual(env[BACKUP_PASSWORD_ENV], PASSWORD)
        self._no_secret_anywhere()

    def test_cancel_at_the_key(self):
        self.stub.with_password = (4, SEALED_FACTS)
        ui = _UI(keys=[None])
        self.assertIsNone(self.ns["plan_restore"](ZIP, ui))
        self.assertEqual(ui.shown[-1], ("ask_recovery_key", "0123456789abcdef"))

    def test_an_unknown_question_is_cancelled_and_said(self):
        self.stub.with_password = (4, {**FACTS, "ok": None, "confirm": "address-change",
                                       "current": ADDR, "new": "b" * 56 + ".onion"})
        ui = _UI()
        self.assertIsNone(self.ns["plan_restore"](ZIP, ui))
        self.assertEqual(ui.shown[-1], ("error", "Restore not started"))
        self.assertIn("address-change", ui.errors[0][1])
        self.assertTrue(any("unexpected question 'address-change'" in line for line in self.logs))

    def test_the_real_run_is_the_plan(self):
        """What show_restore_dialog does with the plan: the launcher's
        restore with the plan's args, password and key, no time limit."""
        self.stub.with_password = (4, SEALED_FACTS)
        plan = self.ns["plan_restore"](ZIP, _UI(keys=[GOOD_KEY]))
        self.stub.calls.clear()
        self.ns["Launcher"].run("restore", *plan.args, capture=True, timeout=None,
                                password=plan.password, extra_env=plan.extra_env)
        argv, env, stdin = self.stub.calls[0]
        self.assertEqual(argv, [LAUNCHER, "restore", ZIP, "--json"])
        self.assertEqual(env[BACKUP_PASSWORD_ENV], PASSWORD)
        self.assertEqual(env[HUB_RECOVERY_KEY_ENV], GOOD_KEY)
        self.assertEqual(stdin, subprocess.DEVNULL)
        self._no_secret_anywhere()


class TestCheckBackup(_Base):

    def test_ok_gives_the_facts(self):
        facts, why = self.ns["check_backup"](PASSWORD)
        self.assertIsNone(why)
        self.assertEqual(facts["output"], ZIP)
        argv, env, stdin = self.stub.calls[0]
        self.assertEqual(argv, [LAUNCHER, "backup", "--check", "--json"])
        self.assertEqual(env[BACKUP_PASSWORD_ENV], PASSWORD)
        self.assertEqual(stdin, subprocess.DEVNULL)
        self._no_secret_anywhere()

    def test_a_refusal_is_the_reason(self):
        self.stub.backup = (1, {"error": "Password does not match"})
        self.assertEqual(self.ns["check_backup"](PASSWORD), (None, "Password does not match"))

    def test_no_answer_is_the_last_line(self):
        self.stub.raw = (1, "", "ERROR: Containers must be running\n")
        self.assertEqual(self.ns["check_backup"](PASSWORD), (None, "Containers must be running"))
        self.stub.raw = (0, "", "")
        self.assertEqual(self.ns["check_backup"](PASSWORD), (None, "Could not verify the password."))


class TestHelpers(_Base):

    def test_last_json_line_without_the_confirm_module(self):
        fake = types.SimpleNamespace(run=self.stub.run)
        ns = _load_tray(fake, self.logs, confirm_module=None)
        self.assertEqual(ns["_last_json_line"]('log line\n{"ok": true}\n'), {"ok": True})
        self.assertIsNone(ns["_last_json_line"]("nothing"))
        self.assertIsNone(ns["_last_json_line"]("[1]"))

    def test_the_failure_line_loses_its_stamp_with_or_without_launcher_ops(self):
        from onionpress import launcher_ops
        for module in (launcher_ops, None):
            with self.subTest(launcher_ops=module is not None):
                ns = _load_tray(types.SimpleNamespace(run=self.stub.run), self.logs,
                                launcher_ops_module=module)
                failure = ns["launcher_failure"]
                self.assertEqual(failure("", "[2026-10-24 10:00:00] ERROR: no\n", "d"), "no")
                self.assertEqual(failure("2026-10-24 10:00:00 ERROR: no\n", "", "d"), "no")
                self.assertEqual(failure("x\n2026-10-24 10:00:00 last\n", "", "d"), "last")
                self.assertEqual(failure("", "", "d"), "d")

    def test_the_launcher_wrapper_merges_the_key_with_the_password(self):
        self.ns["Launcher"].run("restore", ZIP, capture=True, timeout=None,
                                password=PASSWORD, extra_env={HUB_RECOVERY_KEY_ENV: GOOD_KEY})
        env = self.stub.calls[0][1]
        self.assertEqual((env[BACKUP_PASSWORD_ENV], env[HUB_RECOVERY_KEY_ENV]), (PASSWORD, GOOD_KEY))
        self.assertEqual(env.get("PATH"), os.environ.get("PATH"))
        self.ns["Launcher"].run("stop", capture=True)
        self.assertIsNone(self.stub.calls[1][1])
        self.assertEqual(self.stub.calls[1][2], subprocess.DEVNULL)

    def test_the_timeout_is_five_minutes(self):
        self.assertEqual(self.ns["QUESTION_TIMEOUT_SEC"], 300)
        self.assertEqual(self.ns["SEALED_HUB_DATA_QUESTION"], "sealed-hub-data-left-out")
        self.assertIn("sealed-hub-data-left-out", confirm.QUESTIONS)


class TestTrayDialogs(unittest.TestCase):
    """The GTK side, by its source: the dialogs are run through
    _run_dialog (Cancel after the timeout), Cancel is the default, and the
    flows hand the launcher what the checks established."""

    def setUp(self):
        self.tray = _read(TRAY)
        self.tree = ast.parse(self.tray)

    def _source(self, name):
        for node in self.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name == name:
                return ast.get_source_segment(self.tray, node)
        self.fail(name)

    def test_restore_runs_the_plan(self):
        body = self._source("show_restore_dialog")
        self.assertIn("plan = plan_restore(backup_path, ui)", body)
        self.assertIn("ui = _GtkRestoreUI(parent, backup_path)", body)
        self.assertRegex(body, r'Launcher\.run\("restore", \*plan\.args,\s*capture=True, timeout=None, '
                               r'password=plan\.password,\s*extra_env=plan\.extra_env\)')
        self.assertNotIn("verify_wp_admin_password_any", self.tray)

    def test_backup_checks_first_and_backs_up_at_the_named_file(self):
        body = self._source("show_backup_dialog")
        self.assertIn("facts, msg = check_backup(password)", body)
        self.assertIn('checked["output"] = facts.get("output") or ""', body)
        self.assertIn('output_args = ["-o", checked["output"]] if checked.get("output") else []', body)
        self.assertRegex(body, r'Launcher\.run\("backup", \*output_args, capture=True, timeout=None,\s*'
                               r'password=password\)')

    def test_every_question_dialog_defaults_to_cancel_and_times_out(self):
        ui = self._source("_GtkRestoreUI")
        for method in ("_confirm", "_ask_recovery_key"):
            with self.subTest(method):
                start = ui.index(f"def {method}(")
                end = ui.find("\n    def ", start + 1)
                body = ui[start:end if end > 0 else None]
                self.assertIn("dlg.set_default_response(Gtk.ResponseType.CANCEL)", body)
                self.assertIn("response = _run_dialog(dlg)", body)
                self.assertNotIn("dlg.run()", body)
        for method in ("ask_password", "confirm", "ask_recovery_key", "error"):
            self.assertIn(f"def {method}(", ui)
            self.assertIn("_on_main_thread(", ui[ui.index(f"def {method}("):])
        run_dialog = self._source("_run_dialog")
        self.assertIn("GLib.timeout_add_seconds(timeout, _give_up)", run_dialog)
        self.assertIn("dlg.response(Gtk.ResponseType.CANCEL)", run_dialog)
        self.assertIn("timeout: int = QUESTION_TIMEOUT_SEC", run_dialog)

    def test_the_facts_dialog_says_what_the_backup_holds(self):
        confirm_body = self._source("_GtkRestoreUI")
        for fact in ("onion_address", "backup_date", "username", "onionpress_version",
                     "excludes_onion_key"):
            self.assertIn(fact, confirm_body)
        self.assertIn("its key is not in this backup", confirm_body)
        self.assertIn("recovery_key_id", confirm_body)


class TestLinuxLauncherBackupCheck(unittest.TestCase):
    """`onionpress backup ...` on Linux is the shared CLI's, the password
    in the environment: the check (`--check --json [--user U]`) and the
    backup itself (issue #406; the bash create_backup is gone)."""

    def _case(self):
        text = _read(os.path.join(PROJECT_ROOT, "linux", "onionpress"))
        start = text.index("\n        backup)\n")
        return text[start:text.index("\n            ;;\n", start)]

    def _run(self, *args, env_password=None):
        script = (
            "#!/bin/bash\nset -u\nDATA_DIR=/d\nHOME=/h\n"
            "op_py() { echo \"op_py $*\"; echo \"op_py sees password=${ONIONPRESS_BACKUP_PASSWORD:-none}\"; exit 4; }\n"
            "main() {\n    case \"$1\" in" + self._case() + "\n            ;;\n    esac\n}\n"
            "main \"$@\"\n")
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        if env_password is not None:
            env["ONIONPRESS_BACKUP_PASSWORD"] = env_password
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
            f.write(script)
        try:
            return subprocess.run(["bash", f.name, "backup", *args], capture_output=True,
                                  text=True, env=env, stdin=subprocess.DEVNULL, timeout=30)
        finally:
            os.unlink(f.name)

    def test_check_is_the_cli_with_the_password_in_the_environment(self):
        r = self._run("--check", "--json", "--user", "bob", env_password="pw")
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn('op_py --data-dir /d backup --check --json --user bob\n', r.stdout)
        self.assertIn("op_py sees password=pw", r.stdout)
        self.assertNotIn("create_backup", r.stdout)

    def test_check_without_a_password_still_asks_the_cli(self):
        r = self._run("--check", "--json")
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn("op_py sees password=none", r.stdout)
        self.assertNotIn("Usage", r.stdout)

    def test_check_passes_the_output_path_on(self):
        r = self._run("--check", "--json", "-o", "/x/b.zip", env_password="pw")
        self.assertIn("op_py --data-dir /d backup --check --json -o /x/b.zip\n", r.stdout)

    def test_the_backup_itself_is_the_clis(self):
        for argv in (("-o", "/x/b.zip"), ("--json",), ()):
            with self.subTest(argv=argv):
                r = self._run(*argv, env_password="pw")
                self.assertEqual(r.returncode, 4, r.stderr)
                self.assertIn("op_py --data-dir /d backup" + "".join(" " + a for a in argv) + "\n",
                              r.stdout)
                self.assertIn("op_py sees password=pw", r.stdout)
                self.assertNotIn("create_backup", r.stdout)


if __name__ == "__main__":
    unittest.main()

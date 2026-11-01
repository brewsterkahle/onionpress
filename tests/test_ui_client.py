#!/usr/bin/env python3
"""onionpress.ui_client: the one launcher client of the Mac menubar and the
Linux tray (issue #526). Both UIs call the launcher only through it and
read the CLI's answers only through it (onionpress.confirm); the dialogs'
wording is its too, one per question. Run here against a stub launcher:
the Launcher, launcher_failure, the check/confirm loop, the backup and
restore flows, the restore summary, the update outcome and the copy.
"""
import ast
import json
import os
import subprocess
import sys
import types
import unittest
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from onionpress import confirm, launcher_ops, ui_client  # noqa: E402

LAUNCHER = "/opt/onionpress/onionpress"
BACKUP_PASSWORD_ENV = "ONIONPRESS_BACKUP_PASSWORD"
HUB_RECOVERY_KEY_ENV = "ONIONPRESS_HUB_RECOVERY_KEY"
ZIP = "/home/u/OnionPress/backups/OnionPress-aaaaaaaa-admin-2026-10-24.zip"
ADDR = "a" * 56 + ".onion"
HUB = "oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion"
PASSWORD = "s3cret-passw0rd"
GOOD_KEY = "good-recovery-key-value"
BAD_KEY = "bad-recovery-key-value"

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


def _read(rel):
    with open(os.path.join(PROJECT_ROOT, rel), encoding="utf-8") as f:
        return f.read()


class _StubLauncher:
    """Answers `onionpress restore <zip> --check --json`, `backup --check
    --json` and the rest from the environment it was given, as the CLI
    would; records every call."""

    def __init__(self):
        self.calls = []       # (argv, env or None, stdin, input)
        self.without_password = (5, {"ask": "password", "backup_user": "admin"})
        self.with_password = (0, FACTS)
        self.with_key = {GOOD_KEY: (0, {**FACTS, "recovery_key": "accepted"}),
                         BAD_KEY: (1, {"error": "this is not the key the backup was sealed to"})}
        self.backup_check = (0, {"ok": True, "filename": "OnionPress-aaaaaaaa-admin-2026-10-24.zip",
                                 "output": ZIP, "address": ADDR, "username": "admin"})
        self.backup = (0, {"ok": True, "output": ZIP, "bytes": 3 * 1024 * 1024,
                           "notes": ["A hub note."]})
        self.answers = {}     # verb -> (rc, dict or str) for anything else
        self.raw = None       # (rc, stdout, stderr) answers everything when set

    def run(self, argv, **kw):
        env = kw.get("env")
        self.calls.append((list(argv), env, kw.get("stdin"), kw.get("input")))
        if self.raw is not None:
            rc, out, err = self.raw
            return subprocess.CompletedProcess(argv, rc, out, err)
        pw = (env or {}).get(BACKUP_PASSWORD_ENV)
        key = (env or {}).get(HUB_RECOVERY_KEY_ENV)
        verb = argv[1]
        if verb == "backup":
            rc, ans = self.backup_check if "--check" in argv else self.backup
        elif verb == "restore":
            if not pw:
                rc, ans = self.without_password
            elif key:
                rc, ans = self.with_key.get(key, (1, {"error": "unknown key"}))
            else:
                rc, ans = self.with_password
        elif verb in self.answers:
            rc, ans = self.answers[verb]
            if callable(ans):
                rc, ans = ans(argv)
        else:
            rc, ans = 0, {"ok": True}
        out = ans if isinstance(ans, str) else json.dumps(ans) + "\n"
        return subprocess.CompletedProcess(argv, rc, out, "")


class _Base(unittest.TestCase):

    def setUp(self):
        self.logs = []
        self.stub = _StubLauncher()
        for name, target in (("run", self.stub.run), ("Popen", mock.Mock())):
            patcher = mock.patch.object(ui_client.subprocess, name, target)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.launcher = ui_client.Launcher(LAUNCHER, self.logs.append)

    def _no_secret_anywhere(self):
        for argv, env, _, _ in self.stub.calls:
            for secret in (PASSWORD, GOOD_KEY, BAD_KEY):
                self.assertNotIn(secret, argv)
        for line in self.logs:
            for secret in (PASSWORD, GOOD_KEY, BAD_KEY):
                self.assertNotIn(secret, line)


# ─── The Launcher ────────────────────────────────────────────────────────

class TestLauncher(_Base):

    def test_a_password_and_a_key_go_in_the_environment_never_argv(self):
        self.launcher.run("restore", ZIP, timeout=None, password=PASSWORD,
                          extra_env={HUB_RECOVERY_KEY_ENV: GOOD_KEY})
        argv, env, stdin, _ = self.stub.calls[0]
        self.assertEqual(argv, [LAUNCHER, "restore", ZIP])
        self.assertEqual((env[BACKUP_PASSWORD_ENV], env[HUB_RECOVERY_KEY_ENV]), (PASSWORD, GOOD_KEY))
        self.assertEqual(env.get("PATH"), os.environ.get("PATH"))
        self.assertEqual(stdin, subprocess.DEVNULL)
        self._no_secret_anywhere()
        self.assertIn(f"launcher: {LAUNCHER} restore <1 args not logged>", self.logs)

    def test_no_secret_means_the_inherited_environment(self):
        self.launcher.run("stop")
        argv, env, stdin, _ = self.stub.calls[0]
        self.assertEqual(argv, [LAUNCHER, "stop"])
        self.assertIsNone(env)
        self.assertEqual(stdin, subprocess.DEVNULL)
        self.assertIn(f"launcher: {LAUNCHER} stop", self.logs)

    def test_the_old_argv_forms_are_not_logged(self):
        self.launcher.run("restore", PASSWORD, ZIP)
        self.launcher.run_async("backup", PASSWORD)
        self.launcher.run("setup", "--pass", PASSWORD)
        self.assertEqual(len([ln for ln in self.logs if ln.startswith("launcher")]), 3)
        for line in self.logs:
            self.assertNotIn(PASSWORD, line)
        self.launcher.run_async("onionheaven", "enable")
        self.assertIn("onionheaven enable", self.logs[-1])

    def test_a_timeout_is_logged_without_argv_and_raised(self):
        self.stub.run = mock.Mock(side_effect=subprocess.TimeoutExpired(
            [LAUNCHER, "restore", PASSWORD, ZIP], 600))
        with mock.patch.object(ui_client.subprocess, "run", self.stub.run):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.launcher.run("restore", PASSWORD, ZIP, timeout=600)
        self._no_secret_anywhere()
        self.assertTrue(any("timed out" in line for line in self.logs))

    def test_wait_is_the_exit_status_or_minus_one(self):
        self.stub.answers["start"] = (3, "")
        self.assertEqual(self.launcher.wait("start"), 3)
        self.assertFalse(self.stub.calls[0][0][0] is None)
        with mock.patch.object(ui_client.subprocess, "run", mock.Mock(side_effect=FileNotFoundError(LAUNCHER))):
            self.assertEqual(self.launcher.wait("start"), -1)

    def test_run_json_reads_the_last_json_line(self):
        self.stub.answers["version"] = (0, 'log line\n{"ok": true, "version": "2.6.1"}\n')
        rc, ans, out, err = self.launcher.run_json("version", "--json")
        self.assertEqual((rc, ans), (0, {"ok": True, "version": "2.6.1"}))
        self.stub.answers["version"] = (3, "no json\n")
        rc, ans, out, err = self.launcher.run_json("version", "--json")
        self.assertEqual((rc, ans), (3, None))
        self.assertIn("no json", out)

    def test_run_json_that_cannot_run_is_minus_one(self):
        with mock.patch.object(ui_client.subprocess, "run", mock.Mock(side_effect=FileNotFoundError(LAUNCHER))):
            rc, ans, out, err = self.launcher.run_json("backup", "--check", "--json")
        self.assertEqual((rc, ans, out), (-1, None, ""))
        self.assertTrue(any("could not run" in line for line in self.logs))


class TestLauncherForReal(unittest.TestCase):
    """run_json against /bin/sh: the environment reaches the command,
    stdin is /dev/null unless an input is given, stderr lines stream to
    on_line with their stamps removed."""

    def setUp(self):
        self.logs = []
        self.launcher = ui_client.Launcher("/bin/sh", self.logs.append)

    def test_environment_devnull_and_input(self):
        rc, ans, out, err = self.launcher.run_json(
            "-c", 'echo "[2026-10-22 12:00:00] working" >&2; echo "{\\"ok\\": true, \\"env\\": \\"$SECRET\\"}"',
            env={"SECRET": "s3"})
        self.assertEqual((rc, ans), (0, {"ok": True, "env": "s3"}))
        rc, ans, out, err = self.launcher.run_json("-c", "read x; echo no-input-means-devnull-$x; exit 3")
        self.assertEqual((rc, ans), (3, None))
        self.assertIn("no-input-means-devnull-", out)
        rc, ans, out, err = self.launcher.run_json(
            "-c", 'read x; echo "{\\"ok\\": true, \\"got\\": \\"$x\\"}"', input="hello\n")
        self.assertEqual((rc, ans), (0, {"ok": True, "got": "hello"}))

    def test_on_line_streams_stderr_without_the_stamp(self):
        lines = []
        rc, ans, out, err = self.launcher.run_json(
            "-c", 'echo "[2026-10-22 12:00:00] step one" >&2; read x; echo "{\\"ok\\": true, \\"got\\": \\"$x\\"}"',
            input="hello\n", on_line=lines.append)
        self.assertEqual((rc, ans, lines), (0, {"ok": True, "got": "hello"}, ["step one"]))
        self.assertIn("step one", err)

    def test_a_backups_progress_lines_stream(self):
        on_line = []
        plan = ui_client.BackupPlan(["-c", 'echo "[2026-10-24 10:00:00] Backup: copying" >&2; echo "{\\"ok\\": true, \\"output\\": \\"/b.zip\\"}"'],
                                    {}, "admin", {})
        result = ui_client.run_backup(self.launcher, plan, on_line=on_line.append)
        self.assertEqual((result.ok, on_line), (True, ["Backup: copying"]))
        self.assertEqual(result.message, "Backup saved to b.zip")

    def test_a_missing_launcher(self):
        launcher = ui_client.Launcher("/nonexistent/onionpress", self.logs.append)
        self.assertEqual(launcher.run_json("backup")[:2], (-1, None))
        self.assertEqual(launcher.run_json("backup", on_line=lambda ln: None)[:2], (-1, None))


class TestLauncherFailure(unittest.TestCase):

    def test_the_last_error_line_else_the_last_line_else_the_default(self):
        failure = ui_client.launcher_failure
        self.assertEqual(failure("", "[2026-10-24 10:00:00] ERROR: no\n", "d"), "no")
        self.assertEqual(failure("ERROR: no\n", "", "d"), "no")
        self.assertEqual(failure("x\n[2026-10-24 10:00:00] last\n", "", "d"), "last")
        self.assertEqual(failure("", "", "d"), "d")
        self.assertEqual(failure("", "ERROR:\n", "d"), "d")


# ─── The check/confirm loop ──────────────────────────────────────────────

class TestRunConfirmed(_Base):

    QUESTION = {"confirm": "prefix-change", "current": "op2", "new": "abc", "address": ADDR}

    def _config_set(self, argv):
        if "--confirmed" in argv:
            return 0, {"ok": True}
        return 3, {"error": "needs a confirmation", **self.QUESTION}

    def test_a_question_is_asked_then_answered_with_confirmed(self):
        self.stub.answers["config"] = (0, self._config_set)
        for answer in (True, False):
            with self.subTest(answer=answer):
                self.stub.calls.clear()
                ask = mock.Mock(return_value=answer)
                outcome = ui_client.run_confirmed(
                    self.launcher, ["config", "set", "ADDRESS_PREFIX", "abc", "--json"], ask,
                    action="Change", timeout=120)
                ask.assert_called_once_with(
                    "prefix-change", {"error": "needs a confirmation", **self.QUESTION}, action="Change")
                if answer:
                    self.assertTrue(outcome.ok)
                    self.assertEqual(outcome.confirmed, ["prefix-change"])
                    self.assertEqual(self.stub.calls[1][0][-2:], ["--confirmed", "prefix-change"])
                    self.assertEqual(outcome.flags, ["--confirmed", "prefix-change"])
                else:
                    self.assertTrue(outcome.declined)
                    self.assertEqual(outcome.answer["confirm"], "prefix-change")
                    self.assertEqual(len(self.stub.calls), 1)

    def test_a_check_loop_ends_with_the_facts_and_the_flags(self):
        def uninstall(argv):
            confirmed = [argv[i + 1] for i, a in enumerate(argv) if a == "--confirmed"]
            if "hub-holds-key" not in confirmed:
                return 4, {"confirm": "hub-holds-key", "hubs": [{"content_address": ADDR, "onionheaven_address": HUB}]}
            if "uninstall" not in confirmed:
                return 4, {"confirm": "uninstall", "deletes": ["everything"]}
            return 0, {"ok": True, "running": False, "hubs": 1}
        self.stub.answers["uninstall"] = (0, uninstall)
        asked = []

        def ask(what, facts, action):
            asked.append((what, action))
            return True
        outcome = ui_client.run_confirmed(self.launcher, ["uninstall", "--check", "--json"], ask,
                                          action="Uninstall")
        self.assertTrue(outcome.ok)
        self.assertEqual(asked, [("hub-holds-key", "Uninstall"), ("uninstall", "Uninstall")])
        self.assertEqual(outcome.flags, ["--confirmed", "hub-holds-key", "--confirmed", "uninstall"])
        self.assertEqual(self.stub.calls[-1][0], [LAUNCHER, "uninstall", "--check", "--json",
                                                  "--confirmed", "hub-holds-key", "--confirmed", "uninstall"])

    def test_an_error_ends_it_without_a_question(self):
        self.stub.answers["uninstall"] = (1, {"error": "docker is not running"})
        ask = mock.Mock()
        outcome = ui_client.run_confirmed(self.launcher, ["uninstall", "--check", "--json"], ask)
        ask.assert_not_called()
        self.assertFalse(outcome.ok)
        self.assertFalse(outcome.declined)
        self.assertEqual(outcome.failure("d"), "docker is not running")

    def test_a_launcher_that_cannot_run_is_a_failure_with_its_reason(self):
        with mock.patch.object(ui_client.subprocess, "run", mock.Mock(side_effect=FileNotFoundError(LAUNCHER))):
            outcome = ui_client.run_confirmed(self.launcher, ["uninstall", "--check", "--json"], mock.Mock())
        self.assertEqual(outcome.rc, -1)
        self.assertFalse(outcome.ok)
        self.assertIn(LAUNCHER, outcome.failure("d"))

    def test_a_question_asked_twice_does_not_loop(self):
        self.stub.answers["reset"] = (4, {"confirm": "reset"})
        ask = mock.Mock(return_value=True)
        outcome = ui_client.run_confirmed(self.launcher, ["reset", "--check", "--json"], ask)
        self.assertEqual(ask.call_count, 1)
        self.assertEqual(len(self.stub.calls), 2)
        self.assertFalse(outcome.ok)

    def test_a_secret_on_stdin_reaches_every_run(self):
        self.stub.answers["config"] = (0, self._config_set)
        outcome = ui_client.run_confirmed(
            self.launcher, ["config", "set", "CLOUDFLARE_TUNNEL_TOKEN", "-", "--json"],
            mock.Mock(return_value=True), input="tok\n")
        self.assertTrue(outcome.ok)
        for argv, env, stdin, given in self.stub.calls:
            self.assertEqual(given, "tok\n")
            self.assertNotIn("tok", argv)


# ─── The questions ───────────────────────────────────────────────────────

class TestQuestionText(unittest.TestCase):

    FACTS = {
        "address-change": {"current": ADDR, "new": "b" * 56 + ".onion"},
        "sealed-hub-data-left-out": {"recovery_key_id": "0123456789abcdef"},
        "hub-holds-key": {"hubs": [{"content_address": ADDR, "onionheaven_address": HUB}]},
        "prefix-change": {"current": "op2", "new": "abc", "address": "op2" + "a" * 53 + ".onion"},
        "uninstall": {"deletes": ["everything"]},
        "reset": {"deletes": []},
        "clearnet-tunnel": {"replace": True},
    }

    def test_every_question_has_one_wording(self):
        self.assertEqual(set(self.FACTS), set(confirm.QUESTIONS))
        for what in confirm.QUESTIONS:
            with self.subTest(what=what):
                q = ui_client.question_text(what, self.FACTS[what], "Uninstall")
                self.assertIsNotNone(q)
                self.assertEqual(q.what, what)
                self.assertTrue(q.title and q.message and q.yes)
                self.assertNotEqual(q.yes, "Cancel")
                self.assertIn(q.style, ("critical", "warning"))

    def test_an_unknown_question_has_none(self):
        self.assertIsNone(ui_client.question_text("format-disk", {}))

    def test_the_facts_reach_the_dialog(self):
        q = ui_client.question_text("hub-holds-key", self.FACTS["hub-holds-key"], "Reset")
        self.assertIn(HUB, q.message)
        self.assertIn(ADDR, q.message)
        self.assertEqual(q.yes, "Reset Anyway")
        self.assertIn("reset again", q.message)
        q = ui_client.question_text("prefix-change", self.FACTS["prefix-change"])
        self.assertIn("'op2'", q.message)
        self.assertIn("'abc'", q.message)
        self.assertIn("op2" + "a" * 53, q.message)
        self.assertEqual((q.yes, q.style), ("Change Prefix", "warning"))
        q = ui_client.question_text("sealed-hub-data-left-out", self.FACTS["sealed-hub-data-left-out"])
        self.assertIn("0123456789abcdef", q.message)
        self.assertTrue(q.secret_field)
        self.assertEqual((q.yes, q.alternative), ("Restore With Key", "Restore Without"))

    def test_the_address_change_is_worded_for_a_key_import_or_a_restore(self):
        facts = self.FACTS["address-change"]
        q = ui_client.question_text("address-change", facts)
        self.assertEqual(q.yes, "Replace")
        self.assertIn("replace this site's onion service key", q.message)
        self.assertIn(ADDR, q.message)
        self.assertIn("b" * 56, q.message)
        self.assertIn("replaced-keys", q.message)
        q = ui_client.question_text("address-change", {**facts, "action": "restore-backup"})
        self.assertEqual(q.yes, "Restore")
        self.assertIn("restore a backup of another onion address", q.message)
        self.assertIn("Restore it only if you asked for this yourself.", q.message)
        q = ui_client.question_text("address-change", {"new": "b" * 56 + ".onion"})
        self.assertIn("(not known yet)", q.message)

    def test_the_tunnel_question_says_set_or_replace(self):
        q = ui_client.question_text("clearnet-tunnel", {"replace": True})
        self.assertEqual(q.title, "Replace the Cloudflare Tunnel token?")
        q = ui_client.question_text("clearnet-tunnel", {})
        self.assertEqual(q.title, "Put this site on the regular internet?")
        self.assertIn("IP address", q.message)
        self.assertNotIn("Mac", q.message)
        self.assertEqual(q.yes, "Set Token")

    def test_uninstall_wants_delete_typed(self):
        q = ui_client.question_text("uninstall", {"deletes": []}, "Uninstall")
        self.assertEqual((q.typed, q.yes), ("DELETE", "Confirm Deletion"))
        for text, want in (("DELETE", True), ("delete", True), (" Delete ", True),
                           ("yes", False), ("", False), (None, False)):
            self.assertIs(ui_client.typed_confirmation(text, "DELETE"), want, text)
        self.assertIn("(You typed: 'yes')", ui_client.typed_mismatch_message("yes", "DELETE", "Uninstall"))
        self.assertTrue(ui_client.typed_mismatch_message("yes", "DELETE", "Uninstall").startswith("Uninstall cancelled."))

    def test_the_uninstall_warning(self):
        text = ui_client.uninstall_warning("Do you want to create a backup before uninstalling?")
        for item in ui_client.UNINSTALL_DELETES:
            self.assertIn(item, text)
        self.assertIn("CANNOT BE RECOVERED", text)
        self.assertTrue(text.endswith("Do you want to create a backup before uninstalling?"))

    def test_the_timeout_is_five_minutes(self):
        self.assertEqual(ui_client.QUESTION_TIMEOUT_SEC, 300)
        self.assertEqual(ui_client.SEALED_HUB_DATA_QUESTION, "sealed-hub-data-left-out")
        self.assertIn("sealed-hub-data-left-out", confirm.QUESTIONS)


# ─── Backup ──────────────────────────────────────────────────────────────

class _BackupUI(ui_client.BackupUI):

    def __init__(self, creds=(("admin", PASSWORD),), output=ZIP, stop_on_problem=False):
        self.creds = list(creds)
        self.output = output
        self.stop_on_problem = stop_on_problem
        self.shown = []
        self.problems = []

    def ask_credentials(self, default_user, problem):
        self.shown.append(("ask_credentials", default_user))
        self.problems.append(problem)
        if problem and self.stop_on_problem:
            return None
        return self.creds.pop(0) if self.creds else None

    def choose_output(self, facts):
        self.shown.append(("choose_output", facts.get("filename")))
        return self.output


class TestPlanBackup(_Base):

    def test_credentials_checked_then_the_file_then_the_plan(self):
        ui = _BackupUI()
        plan = ui_client.plan_backup(self.launcher, ui, default_user="admin", log=self.logs.append)
        self.assertEqual(ui.shown, [("ask_credentials", "admin"),
                                    ("choose_output", "OnionPress-aaaaaaaa-admin-2026-10-24.zip")])
        self.assertEqual(plan.args, ["backup", "--json", "-o", ZIP, "--user", "admin"])
        self.assertEqual(plan.env, {BACKUP_PASSWORD_ENV: PASSWORD})
        self.assertEqual(plan.username, "admin")
        argv, env, stdin, _ = self.stub.calls[0]
        self.assertEqual(argv, [LAUNCHER, "backup", "--check", "--json", "--user", "admin"])
        self.assertEqual(env[BACKUP_PASSWORD_ENV], PASSWORD)
        self.assertEqual(stdin, subprocess.DEVNULL)
        self._no_secret_anywhere()

    def test_no_username_means_the_clis_owner_and_no_chooser_means_its_file(self):
        """The Linux tray: the password alone, the file the check named."""
        ui = _BackupUI(creds=((None, PASSWORD),), output="")
        plan = ui_client.plan_backup(self.launcher, ui)
        self.assertEqual(self.stub.calls[0][0], [LAUNCHER, "backup", "--check", "--json"])
        self.assertEqual(plan.args, ["backup", "--json"])
        self.assertEqual(plan.username, "admin")      # the check's answer

    def test_a_refused_password_is_asked_again_with_the_reason(self):
        self.stub.backup_check = (1, {"error": "Password does not match"})
        ui = _BackupUI(creds=(("admin", "wrong"), ("admin", PASSWORD)))
        plan = ui_client.plan_backup(self.launcher, ui)
        self.assertIsNone(plan)                       # refused twice: creds ran out
        self.assertEqual(ui.problems, [None, "Password does not match", "Password does not match"])

    def test_a_ui_that_stops_at_a_refusal(self):
        """The Mac: the refusal is an alert and the backup ends."""
        self.stub.backup_check = (1, {"error": "Password does not match"})
        ui = _BackupUI(stop_on_problem=True)
        self.assertIsNone(ui_client.plan_backup(self.launcher, ui))
        self.assertEqual(ui.problems, [None, "Password does not match"])
        self.assertEqual(len(self.stub.calls), 1)

    def test_no_answer_is_the_last_line(self):
        self.stub.raw = (1, "", "ERROR: Containers must be running\n")
        ui = _BackupUI(stop_on_problem=True)
        self.assertIsNone(ui_client.plan_backup(self.launcher, ui))
        self.assertEqual(ui.problems[-1], "Containers must be running")
        self.stub.raw = (0, "", "")
        ui = _BackupUI(stop_on_problem=True)
        self.assertIsNone(ui_client.plan_backup(self.launcher, ui))
        self.assertEqual(ui.problems[-1], "Could not verify the credentials")

    def test_cancel_at_the_credentials_or_the_file(self):
        self.assertIsNone(ui_client.plan_backup(self.launcher, _BackupUI(creds=())))
        self.assertEqual(self.stub.calls, [])
        self.assertIsNone(ui_client.plan_backup(self.launcher, _BackupUI(output=None)))
        self.assertEqual(len(self.stub.calls), 1)

    def test_the_real_run_and_its_message(self):
        plan = ui_client.plan_backup(self.launcher, _BackupUI())
        self.stub.calls.clear()
        result = ui_client.run_backup(self.launcher, plan, log=self.logs.append)
        argv, env, stdin, _ = self.stub.calls[0]
        self.assertEqual(argv, [LAUNCHER, "backup", "--json", "-o", ZIP, "--user", "admin"])
        self.assertEqual(env[BACKUP_PASSWORD_ENV], PASSWORD)
        self.assertEqual(stdin, subprocess.DEVNULL)
        self.assertTrue(result.ok)
        self.assertEqual(result.message,
                         "Backup saved to OnionPress-aaaaaaaa-admin-2026-10-24.zip (3.0 MB)\n\nA hub note.")
        self._no_secret_anywhere()

    def test_a_linux_answer_without_a_size(self):
        self.stub.backup = (0, {"ok": True, "output": ZIP})
        plan = ui_client.plan_backup(self.launcher, _BackupUI(output=""))
        result = ui_client.run_backup(self.launcher, plan)
        self.assertEqual(result.message, "Backup saved to OnionPress-aaaaaaaa-admin-2026-10-24.zip")

    def test_a_failed_backup_says_why(self):
        plan = ui_client.plan_backup(self.launcher, _BackupUI())
        self.stub.backup = (1, {"error": "disk full"})
        result = ui_client.run_backup(self.launcher, plan)
        self.assertEqual((result.ok, result.message), (False, "Backup failed: disk full"))
        self.stub.backup = (1, "ERROR: tar failed\n")
        self.stub.raw = (1, "", "[2026-10-24 10:00:00] ERROR: tar failed\n")
        result = ui_client.run_backup(self.launcher, plan)
        self.assertEqual(result.message, "Backup failed: tar failed")
        self.stub.raw = (0, "", "")
        result = ui_client.run_backup(self.launcher, plan)
        self.assertEqual(result.message, "Backup failed: see the OnionPress log")

    def test_the_real_run_has_no_time_limit(self):
        plan = ui_client.plan_backup(self.launcher, _BackupUI())
        with mock.patch.object(ui_client.subprocess, "run", wraps=self.stub.run) as run:
            ui_client.run_backup(self.launcher, plan)
        self.assertIsNone(run.call_args.kwargs["timeout"])


# ─── Restore ─────────────────────────────────────────────────────────────

class _RestoreUI(ui_client.RestoreUI):
    """A RestoreUI that records the dialogs shown and answers as told."""

    def __init__(self, password=PASSWORD, confirm=True, keys=(), questions=True):
        self.shown = []
        self.password = password
        self.confirmed = confirm
        self.keys = list(keys)
        self.questions = questions
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

    def ask_question(self, what, facts, action="Restore"):
        self.shown.append(("ask_question", what, action))
        return self.questions

    def error(self, title, message):
        self.shown.append(("error", title))
        self.errors.append((title, message))


class TestPlanRestore(_Base):

    def _checks(self):
        return [c for c in self.stub.calls if "--check" in c[0]]

    def test_password_then_facts_then_the_plan(self):
        ui = _RestoreUI()
        plan = ui_client.plan_restore(self.launcher, ZIP, ui, log=self.logs.append)
        self.assertEqual(ui.shown, [("ask_password", "admin"), ("confirm", ADDR)])
        self.assertEqual(plan, ui_client.RestorePlan(ZIP, ["--json"], PASSWORD, {}, FACTS))
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
        ui = _RestoreUI()
        ui_client.plan_restore(self.launcher, ZIP, ui)
        self.assertEqual(ui.shown[0], ("ask_password", None))

    def test_cancel_at_the_password(self):
        ui = _RestoreUI(password=None)
        self.assertIsNone(ui_client.plan_restore(self.launcher, ZIP, ui))
        self.assertEqual(len(self.stub.calls), 1)
        self.assertEqual(ui.shown, [("ask_password", "admin")])

    def test_an_error_is_shown_and_nothing_runs(self):
        self.stub.with_password = (1, {"error": "Wrong password or not a valid OnionPress backup"})
        ui = _RestoreUI()
        self.assertIsNone(ui_client.plan_restore(self.launcher, ZIP, ui, log=self.logs.append))
        self.assertEqual(ui.shown, [("ask_password", "admin"), ("error", "Invalid backup")])
        self.assertEqual(ui.errors, [("Invalid backup", "Wrong password or not a valid OnionPress backup")])
        self.assertEqual(len(self.stub.calls), 2)
        self.assertTrue(any("failed to read backup metadata" in line for line in self.logs))

    def test_a_launcher_without_an_answer_shows_its_last_error_line(self):
        self.stub.raw = (1, "", "2026-10-24 10:00:00 ERROR: Containers must be running\n")
        ui = _RestoreUI()
        self.assertIsNone(ui_client.plan_restore(self.launcher, ZIP, ui))
        self.assertEqual(ui.errors, [("Invalid backup", "2026-10-24 10:00:00 ERROR: Containers must be running")])
        self.stub.raw = (1, "", "[2026-10-24 10:00:00] ERROR: Containers must be running\n")
        ui = _RestoreUI()
        self.assertIsNone(ui_client.plan_restore(self.launcher, ZIP, ui))
        self.assertEqual(ui.errors, [("Invalid backup", "Containers must be running")])

    def test_a_launcher_that_cannot_run_is_an_error_dialog(self):
        with mock.patch.object(ui_client.subprocess, "run", mock.Mock(side_effect=FileNotFoundError(LAUNCHER))):
            ui = _RestoreUI()
            self.assertIsNone(ui_client.plan_restore(self.launcher, ZIP, ui))
        self.assertEqual(ui.shown[-1], ("error", "Invalid backup"))
        self.assertTrue(any("could not run" in line for line in self.logs))

    def test_declining_the_confirmation(self):
        ui = _RestoreUI(confirm=False)
        self.assertIsNone(ui_client.plan_restore(self.launcher, ZIP, ui))
        self.assertEqual(ui.shown, [("ask_password", "admin"), ("confirm", ADDR)])

    def test_sealed_hub_data_restored_without_the_key_is_confirmed(self):
        self.stub.with_password = (4, SEALED_FACTS)
        ui = _RestoreUI(keys=[""])
        plan = ui_client.plan_restore(self.launcher, ZIP, ui, log=self.logs.append)
        self.assertEqual(ui.shown, [("ask_password", "admin"), ("confirm", ADDR),
                                    ("ask_recovery_key", "0123456789abcdef")])
        self.assertEqual(plan.extra_args, ["--json", "--confirmed", "sealed-hub-data-left-out"])
        self.assertEqual((plan.password, plan.extra_env), (PASSWORD, {}))
        self.assertEqual(plan.facts["onionheaven_data"], "sealed")
        # The check once more, with the confirmation: what the CLI answers
        # then is "ok".
        self.assertEqual(self.stub.calls[-1][0], [LAUNCHER, "restore", ZIP, "--check", "--json",
                                                  "--confirmed", "sealed-hub-data-left-out"])
        self.assertEqual(ui.problems, [None])
        self.assertTrue(any("will not be restored" in line for line in self.logs))

    def test_a_refused_key_is_asked_again_and_an_accepted_one_goes_in_the_environment(self):
        self.stub.with_password = (4, SEALED_FACTS)
        ui = _RestoreUI(keys=[BAD_KEY, GOOD_KEY])
        plan = ui_client.plan_restore(self.launcher, ZIP, ui)
        self.assertEqual([s[0] for s in ui.shown],
                         ["ask_password", "confirm", "ask_recovery_key", "ask_recovery_key"])
        self.assertEqual(ui.problems, [None, "this is not the key the backup was sealed to"])
        self.assertEqual(plan.extra_args, ["--json"])
        self.assertEqual(plan.extra_env, {HUB_RECOVERY_KEY_ENV: GOOD_KEY})
        key_checks = [c for c in self.stub.calls if c[1] and HUB_RECOVERY_KEY_ENV in c[1]]
        self.assertEqual([c[1][HUB_RECOVERY_KEY_ENV] for c in key_checks], [BAD_KEY, GOOD_KEY, GOOD_KEY])
        for argv, env, _, _ in key_checks:
            self.assertEqual(argv[:5], [LAUNCHER, "restore", ZIP, "--check", "--json"])
            self.assertEqual(env[BACKUP_PASSWORD_ENV], PASSWORD)
        self._no_secret_anywhere()

    def test_cancel_at_the_key(self):
        self.stub.with_password = (4, SEALED_FACTS)
        ui = _RestoreUI(keys=[None])
        self.assertIsNone(ui_client.plan_restore(self.launcher, ZIP, ui))
        self.assertEqual(ui.shown[-1], ("ask_recovery_key", "0123456789abcdef"))

    def test_any_other_question_is_asked_through_the_ui(self):
        question = {**FACTS, "ok": None, "confirm": "address-change",
                    "current": ADDR, "new": "b" * 56 + ".onion"}

        def restore(argv):
            if "--confirmed" in argv:
                return 0, FACTS
            return 4, question
        self.stub.answers["restore"] = (0, restore)
        self.stub.with_password = None
        # the stub routes "restore" by password first: give it the generic path
        self.stub.without_password = (4, question)
        original = self.stub.run

        def run(argv, **kw):
            env = kw.get("env") or {}
            self.stub.calls.append((list(argv), kw.get("env"), kw.get("stdin"), kw.get("input")))
            if not env.get(BACKUP_PASSWORD_ENV):
                rc, ans = 5, {"ask": "password", "backup_user": "admin"}
            else:
                rc, ans = restore(argv)
            return subprocess.CompletedProcess(argv, rc, json.dumps(ans) + "\n", "")
        with mock.patch.object(ui_client.subprocess, "run", run):
            ui = _RestoreUI(questions=True)
            plan = ui_client.plan_restore(self.launcher, ZIP, ui)
            self.assertEqual(ui.shown[-1], ("ask_question", "address-change", "Restore"))
            self.assertEqual(plan.extra_args, ["--json", "--confirmed", "address-change"])
            ui = _RestoreUI(questions=False)
            self.assertIsNone(ui_client.plan_restore(self.launcher, ZIP, ui, log=self.logs.append))
            self.assertEqual(ui.shown[-1], ("ask_question", "address-change", "Restore"))
        self.assertTrue(any("not confirmed (address-change)" in line for line in self.logs))

    def test_the_real_run_is_the_plan(self):
        """run_restore: the launcher's restore with the plan's args,
        password and key, no time limit (launcher_ops.run_launcher_restore)."""
        self.stub.with_password = (4, SEALED_FACTS)
        plan = ui_client.plan_restore(self.launcher, ZIP, _RestoreUI(keys=[GOOD_KEY]))
        self.stub.calls.clear()
        self.stub.answers["restore"] = None
        with mock.patch.object(launcher_ops.subprocess, "run", self.stub.run) as run:
            self.stub.with_key[GOOD_KEY] = (0, {"ok": True, "address": ADDR, "left_on_hubs": [],
                                                "hub_data": "restored"})
            result = ui_client.run_restore(self.launcher, plan, data_dir="/nonexistent/d",
                                           log_path="/nonexistent/d/onionpress.log")
        argv, env, stdin, _ = self.stub.calls[0]
        self.assertEqual(argv, [LAUNCHER, "restore", ZIP, "--json"])
        self.assertEqual(env[BACKUP_PASSWORD_ENV], PASSWORD)
        self.assertEqual(env[HUB_RECOVERY_KEY_ENV], GOOD_KEY)
        self.assertEqual(stdin, subprocess.DEVNULL)
        self.assertTrue(result.ok)
        self.assertEqual(result.answer["hub_data"], "restored")
        self._no_secret_anywhere()
        src = _read("src/onionpress/ui_client.py")
        body = src[src.index("def run_restore("):src.index("def restore_summary(")]
        self.assertIn("timeout=None", body)
        self.assertIn("launcher_ops.run_launcher_restore(", body)

    def test_a_failed_run_says_the_launchers_last_lines(self):
        plan = ui_client.plan_restore(self.launcher, ZIP, _RestoreUI())
        self.stub.with_password = (1, {"error": "the key in the backup is damaged"})
        with mock.patch.object(launcher_ops.subprocess, "run", self.stub.run):
            result = ui_client.run_restore(self.launcher, plan, data_dir="/nonexistent/d",
                                           log_path="/nonexistent/d/onionpress.log")
        self.assertFalse(result.ok)
        self.assertIn("the key in the backup is damaged", result.failure)
        self.assertIn("/nonexistent/d/onionpress.log", result.failure)


class TestRestoreFactsText(unittest.TestCase):

    def test_says_what_the_backup_holds(self):
        text = ui_client.restore_facts_text(FACTS)
        for fact in (ADDR, "2026-10-24 10:00", "admin", "2.5.6"):
            self.assertIn(fact, text)
        self.assertIn("overwrite your current site, database, and onion address", text)
        self.assertNotIn("its key is not in this backup", text)

    def test_a_keyless_backup_keeps_the_address(self):
        text = ui_client.restore_facts_text({**FACTS, "excludes_onion_key": True})
        self.assertIn("its key is not in this backup", text)
        self.assertIn("keeps its onion address", text)
        self.assertEqual(ui_client.RESTORE_CONFIRM_TITLE, "Restore from this backup?")

    def test_unknown_facts(self):
        text = ui_client.restore_facts_text({})
        self.assertEqual(text.count("unknown"), 4)


class TestRestoreSummary(unittest.TestCase):

    def test_the_address_is_the_clis_then_the_facts(self):
        self.assertIn(f"Onion address: {ADDR}",
                      ui_client.restore_summary({"ok": True, "address": ADDR}, {}))
        self.assertIn("Onion address: " + "b" * 56,
                      ui_client.restore_summary({"ok": True, "address": ADDR}, {}, "b" * 56 + ".onion"))
        self.assertIn(f"Onion address: {ADDR}", ui_client.restore_summary({}, FACTS))
        self.assertTrue(ui_client.restore_summary({}, {}).startswith("Site restored successfully."))

    def test_what_a_hub_may_still_hold(self):
        left = [{"onionheaven_address": HUB, "content_address": "b" * 56 + ".onion", "why": "unreachable"}]
        text = ui_client.restore_summary({"address": ADDR, "left_on_hubs": left}, FACTS)
        self.assertIn(HUB, text)
        self.assertIn("may still hold a copy", text)

    def test_the_hubs_vm_memory_on_a_mac_only(self):
        answer = {"address": HUB}
        self.assertIn("VM memory will increase to 5 GB", ui_client.restore_summary(answer, {}, vm_memory_gb="2"))
        self.assertIn("VM memory: 8 GB", ui_client.restore_summary(answer, {}, vm_memory_gb="8"))
        self.assertIn("VM memory will increase", ui_client.restore_summary(answer, {}, vm_memory_gb="x"))
        self.assertNotIn("VM memory", ui_client.restore_summary(answer, {}))
        self.assertNotIn("VM memory", ui_client.restore_summary({"address": ADDR}, {}, vm_memory_gb="2"))

    def test_sealed_hub_data(self):
        sealed = {"onionheaven_data": "sealed"}
        self.assertIn("registrants' keys: restored.",
                      ui_client.restore_summary({"hub_data": "restored"}, sealed))
        self.assertIn("left out, says the CLI",
                      ui_client.restore_summary({"hub_data": "left-out", "hub_data_note": "left out, says the CLI"}, sealed))
        self.assertIn("registrants' keys: not restored.",
                      ui_client.restore_summary({"hub_data": "left-out"}, sealed))
        self.assertNotIn("registrants' keys", ui_client.restore_summary({"hub_data": "restored"}, FACTS))


# ─── Update check ────────────────────────────────────────────────────────

class TestUpdateOutcome(unittest.TestCase):

    def _report(self, **fields):
        report = {"current": "2.5.6", "latest": "2.5.6", "update_available": False,
                  "release_url": None, "error": None}
        report.update(fields)
        return json.dumps(report, indent=2)

    def test_an_error_is_never_up_to_date(self):
        o = ui_client.update_outcome(0, self._report(
            latest=None, error="Update check curl failed: exit=77 stderr=..."), "")
        self.assertEqual((o.kind, o.title), ("failed", "Update check failed"))
        self.assertIn("exit=77", o.body)
        self.assertIn("exit=77", o.error)
        self.assertIsNone(o.release_url)

    def test_a_non_zero_exit_and_no_json_fail_too(self):
        o = ui_client.update_outcome(1, "", "Traceback ...")
        self.assertEqual(o.title, "Update check failed")
        self.assertIn("Traceback", o.body)
        for out in ("", "[]", json.dumps({"current": "2.5.6"})):
            o = ui_client.update_outcome(0, out, "")
            self.assertEqual((o.kind, o.title), ("failed", "Update check failed"), out)
        o = ui_client.update_outcome(1, "", "")
        self.assertIn("exited 1", o.body)

    def test_an_indented_report_with_other_users_parses(self):
        """check_for_update_report prints json.dumps(indent=2): a list of
        other users puts `{` lines inside it (confirm.last_json_line)."""
        o = ui_client.update_outcome(0, "log line\n" + self._report(
            other_instances=[{"user": "bob", "pid": 7}, {"user": "eve", "pid": 9}]), "")
        self.assertEqual(o.kind, "current")
        self.assertEqual([u["user"] for u in o.other_instances], ["bob", "eve"])

    def test_up_to_date(self):
        o = ui_client.update_outcome(0, "log line\n" + self._report(), "")
        self.assertEqual((o.kind, o.title, o.urgency), ("current", "Up to date", "low"))
        self.assertIn("2.5.6", o.body)
        self.assertEqual(o.current, "2.5.6")

    def test_update_available(self):
        url = "https://github.com/brewsterkahle/onionpress/releases/tag/v2.6.0"
        o = ui_client.update_outcome(0, self._report(
            latest="2.6.0", update_available=True, release_url=url,
            other_instances=[{"user": "bob", "pid": 7}], active_offsets=[0, 10000]), "")
        self.assertEqual((o.kind, o.title), ("available", "OnionPress 2.6.0 is available"))
        self.assertEqual((o.latest, o.release_url), ("2.6.0", url))
        self.assertEqual(o.other_instances, ({"user": "bob", "pid": 7},))
        self.assertEqual(o.active_offsets, (0, 10000))
        self.assertEqual(o.body, "")       # how to install is the UI's


# ─── The copy ────────────────────────────────────────────────────────────

class TestCopy(unittest.TestCase):

    def test_short_onion(self):
        self.assertEqual(ui_client.short_onion(ADDR), "aaaaaaaa….onion")
        self.assertEqual(ui_client.short_onion("op2" + "b" * 53 + ".onion"), "op2bbbbb….onion")
        self.assertEqual(ui_client.short_onion("Starting..."), "Starting...")
        self.assertIsNone(ui_client.short_onion(None))

    def test_site_url_is_the_shareable_url(self):
        from onionpress import app_commands
        for name in ("", "alice"):
            self.assertEqual(ui_client.site_url(ADDR, name), app_commands.shareable_url(ADDR, name))
        self.assertEqual(ui_client.site_url(ADDR, "alice"), f"http://{ADDR}/alice/")
        self.assertEqual(ui_client.site_url(ADDR), f"http://{ADDR}/")

    def test_about(self):
        text = ui_client.about_text("2.6.1", "your Mac")
        self.assertTrue(text.startswith("OnionPress v2.6.1\n\nRun your own website from your Mac."))
        for feature in ui_client.ABOUT_FEATURES:
            self.assertIn(f"• {feature}", text)
        self.assertTrue(text.endswith("Created by the Internet Archive\nLicense: AGPL v3 or later"))
        self.assertIn("from your computer.", ui_client.about_comments())


# ─── The module's shape, and its clients ─────────────────────────────────

MENUBAR = "src/menubar.py"
TRAY = "linux/onionpress-tray"


def _launcher_argv_calls(rel, launcher_names):
    """Every subprocess.run/Popen/check_output in *rel* whose argv's first
    element names the launcher."""
    found = []
    for node in ast.walk(ast.parse(_read(rel))):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess"
                and node.func.attr in ("run", "Popen", "check_output", "check_call", "call")):
            continue
        if not node.args or not isinstance(node.args[0], ast.List) or not node.args[0].elts:
            continue
        first = node.args[0].elts[0]
        text = ast.unparse(first)
        if any(name in text for name in launcher_names):
            found.append(f"{rel}:{node.lineno} {text}")
    return found


class TestModuleShape(unittest.TestCase):

    def test_imports_are_the_standard_library_confirm_and_launcher_ops(self):
        """So the menubar's docker-free import closure holds
        (tests/test_mac_daemon.py); onionheaven only lazily, for a
        restore's summary."""
        tree = ast.parse(_read("src/onionpress/ui_client.py"))
        top = {a.name for n in tree.body if isinstance(n, ast.ImportFrom) and n.level for a in n.names}
        self.assertEqual(top, {"confirm", "launcher_ops"})
        lazy = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level
                for a in n.names} - top
        self.assertEqual(lazy, {"onionheaven"})
        self.assertNotIn("import docker", _read("src/onionpress/ui_client.py"))

    def test_the_bundle_includes_it(self):
        self.assertIn("'onionpress.ui_client'", _read("setup.py"))

    def test_no_launcher_calling_code_in_the_uis(self):
        """Both UIs call the launcher only through ui_client.Launcher, and
        read no answer themselves."""
        self.assertEqual(_launcher_argv_calls(MENUBAR, ("launcher_script", "launcher")), [])
        self.assertEqual(_launcher_argv_calls(TRAY, ("LAUNCHER",)), [])
        for rel in (MENUBAR, TRAY):
            with self.subTest(rel=rel):
                src = _read(rel)
                self.assertNotIn("last_json_line(", src)
                self.assertNotIn("json.loads(", src.replace("json.load(", ""))
                self.assertNotIn("def launcher_failure", src)
                self.assertNotIn("def _run_launcher_json", src)
                self.assertNotIn("def plan_restore", src)
                self.assertNotIn("def update_check_outcome", src)
                self.assertNotIn("class Launcher", src)

    def test_the_menubar_has_the_client_and_the_dialogs(self):
        src = _read(MENUBAR)
        self.assertIn("self.launcher = ui_client.Launcher(self.launcher_script, self.log)", src)
        self.assertIn("class _MacBackupUI(ui_client.BackupUI):", src)
        self.assertIn("class _MacRestoreUI(ui_client.RestoreUI):", src)
        self.assertIn("ui_client.plan_backup(", src)
        self.assertIn("ui_client.run_backup(", src)
        self.assertIn("ui_client.plan_restore(", src)
        self.assertIn("ui_client.run_restore(", src)
        self.assertIn("ui_client.restore_summary(", src)
        self.assertIn("ui_client.update_outcome(", src)
        self.assertIn("ui_client.question_text(", src)
        self.assertIn("ui_client.about_text(", src)
        self.assertEqual(src.count("ui_client.run_confirmed("), 2)     # config set, uninstall --check

    def test_the_tray_has_the_client_and_the_dialogs_and_no_fallbacks(self):
        src = _read(TRAY)
        self.assertIn("Launcher = ui_client.Launcher(LAUNCHER, log)", src)
        self.assertIn("class _GtkBackupUI(ui_client.BackupUI):", src)
        self.assertIn("class _GtkRestoreUI(ui_client.RestoreUI):", src)
        self.assertIn("def _ask_question(parent, what: str, facts: dict, action: str = ", src)
        for call in ("ui_client.plan_backup(", "ui_client.run_backup(", "ui_client.plan_restore(",
                     "ui_client.run_restore(", "ui_client.restore_summary(", "ui_client.update_outcome(",
                     "ui_client.question_text(", "ui_client.about_comments(", "ui_client.site_url(",
                     "ui_client.short_onion(", "ui_client.uninstall_warning("):
            with self.subTest(call=call):
                self.assertIn(call, src)
        # The package is installed beside the tray: hard imports, no
        # degraded mode.
        self.assertNotIn("except ImportError", src)
        self.assertNotIn("is None:\n", src[src.index("from onionpress import ui_client"):src.index("class Status")])
        for gone in ("launcher_ops is None", "_setup_logic is None", "_confirm is None",
                     "def _last_json_line", "def _without_log_stamp", "def launcher_answer",
                     "def check_backup", "class RestoreUI", "class RestorePlan",
                     "class UpdateCheckOutcome", "_UNLOGGED_ARGS", "xdg-open\", base"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, src)
        self.assertIn("from onionpress import launcher_ops  # noqa: E402", src)
        self.assertIn("from onionpress import setup_logic as _setup_logic  # noqa: E402", src)

    def test_every_question_dialog_in_the_tray_defaults_to_cancel_and_times_out(self):
        src = _read(TRAY)
        ask = src[src.index("def _ask_question("):src.index("class _GtkRestoreUI")]
        self.assertIn("dlg.set_default_response(Gtk.ResponseType.CANCEL)", ask)
        self.assertIn("response = _run_dialog(dlg)", ask)
        self.assertIn("ui_client.typed_confirmation(typed, q.typed)", ask)
        ui = src[src.index("class _GtkRestoreUI"):src.index("class _GtkBackupUI")]
        for method in ("_confirm", "_ask_recovery_key"):
            with self.subTest(method):
                start = ui.index(f"def {method}(")
                end = ui.find("\n    def ", start + 1)
                body = ui[start:end if end > 0 else None]
                self.assertIn("dlg.set_default_response(Gtk.ResponseType.CANCEL)", body)
                self.assertIn("response = _run_dialog(dlg)", body)
                self.assertNotIn("dlg.run()", body)
        for method in ("ask_password", "confirm", "ask_recovery_key", "ask_question", "error"):
            self.assertIn(f"def {method}(", ui)
            self.assertIn("_on_main_thread(", ui[ui.index(f"def {method}("):])
        run_dialog = src[src.index("def _run_dialog("):src.index("def _on_main_thread(")]
        self.assertIn("GLib.timeout_add_seconds(timeout, _give_up)", run_dialog)
        self.assertIn("dlg.response(Gtk.ResponseType.CANCEL)", run_dialog)
        self.assertIn("timeout: int = QUESTION_TIMEOUT_SEC", run_dialog)
        self.assertIn("QUESTION_TIMEOUT_SEC = ui_client.QUESTION_TIMEOUT_SEC", src)

    def test_the_menubar_dialogs_default_to_cancel(self):
        src = _read(MENUBAR)
        ask = src[src.index("def _ask_cli_question("):src.index("def _ask_settings_page_question(")]
        self.assertIn('buttons=["Cancel", q.yes]', ask)
        self.assertIn("default_button=0, cancel_button=0", ask)
        self.assertIn('cancel="Cancel"', ask)
        self.assertIn("ui_client.typed_confirmation(response.text, q.typed)", ask)
        self.assertIn("max_wait = 300", src[src.index("def show_native_alert("):])
        restore_ui = src[src.index("class _MacRestoreUI"):src.index("class OnionPressApp")]
        self.assertIn('buttons=["Cancel", "Restore"], default_button=0, cancel_button=0', restore_ui)
        self.assertIn("NSSecureTextField", restore_ui)
        self.assertIn('addButtonWithTitle_("Cancel").setKeyEquivalent_("\\x1b")', restore_ui)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""The Linux tray speaks the check/confirm protocol for backup and restore
(issue #375, onionpress.confirm) through the UIs' shared client
(onionpress.ui_client, issue #526: tests/test_ui_client.py runs the
flows against a stub launcher, once for both UIs). What is the tray's
own is here: the GTK side, by its source (the dialogs are run through
_run_dialog, Cancel after the timeout, Cancel the default, the flows hand
ui_client what it needs), and the Linux launcher's `backup --check`.
"""

import ast
import os
import re
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from onionpress import confirm  # noqa: E402

TRAY = os.path.join(PROJECT_ROOT, "linux", "onionpress-tray")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class TestTrayDialogs(unittest.TestCase):
    """The GTK side, by its source."""

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
        self.assertIn("plan = ui_client.plan_restore(Launcher, backup_path, ui, log=log)", body)
        self.assertIn("ui = _GtkRestoreUI(parent, backup_path)", body)
        self.assertRegex(body, r"result = ui_client\.run_restore\(\s*Launcher, plan, data_dir=DATA_DIR,")
        self.assertIn("ui_client.restore_summary(result.answer, plan.facts)", body)
        self.assertNotIn("verify_wp_admin_password_any", self.tray)

    def test_backup_checks_first_and_backs_up_at_the_named_file(self):
        body = self._source("show_backup_dialog")
        self.assertIn("plan = ui_client.plan_backup(Launcher, ui, log=log)", body)
        self.assertIn("ui = _GtkBackupUI(parent)", body)
        self.assertIn("result = ui_client.run_backup(Launcher, plan, log=log)", body)
        ui = self._source("_GtkBackupUI")
        # The password alone (the CLI knows the owner's login), the file
        # the check named.
        self.assertIn("return None, password", ui)
        self.assertIn('return facts.get("output") or ""', ui)
        self.assertIn("_on_main_thread(", ui)
        self.assertIn("response = _run_dialog(dlg)", ui)

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
        for method in ("ask_password", "confirm", "ask_recovery_key", "ask_question", "error"):
            self.assertIn(f"def {method}(", ui)
            self.assertIn("_on_main_thread(", ui[ui.index(f"def {method}("):])
        ask = self._source("_ask_question")
        self.assertIn("dlg.set_default_response(Gtk.ResponseType.CANCEL)", ask)
        self.assertIn("response = _run_dialog(dlg)", ask)
        self.assertNotIn("dlg.run()", ask)
        run_dialog = self._source("_run_dialog")
        self.assertIn("GLib.timeout_add_seconds(timeout, _give_up)", run_dialog)
        self.assertIn("dlg.response(Gtk.ResponseType.CANCEL)", run_dialog)
        self.assertIn("timeout: int = QUESTION_TIMEOUT_SEC", run_dialog)
        self.assertIn("QUESTION_TIMEOUT_SEC = ui_client.QUESTION_TIMEOUT_SEC", self.tray)

    def test_the_facts_dialog_is_ui_clients(self):
        ui = self._source("_GtkRestoreUI")
        self.assertIn("ui_client.restore_facts_text(facts)", ui)
        self.assertIn("text=ui_client.RESTORE_CONFIRM_TITLE", ui)
        self.assertIn("ui_client.question_text(ui_client.SEALED_HUB_DATA_QUESTION, facts)", ui)
        self.assertIn("dlg.add_button(q.alternative, Gtk.ResponseType.REJECT)", ui)

    def test_every_question_has_a_dialog(self):
        """_ask_question renders any of confirm.QUESTIONS (worded by
        ui_client.question_text); a word to type is checked."""
        ask = self._source("_ask_question")
        self.assertIn("ui_client.question_text(what, facts, action)", ask)
        self.assertIn("ui_client.typed_confirmation(typed, q.typed)", ask)
        self.assertIn("ui_client.typed_mismatch_message(typed, q.typed, action)", ask)
        self.assertIn("sealed-hub-data-left-out", confirm.QUESTIONS)

    def test_the_uninstall_asks_the_clis_questions_then_opens_the_terminal(self):
        # #529: the questions (the hub, the typed DELETE) are the CLI's
        # (`uninstall --check --json` through run_confirmed, each a dialog
        # of _ask_question), and the answers go to the launcher's uninstall
        # in the terminal as --confirmed flags.
        body = self._source("OnionPressTray")
        body = body[body.index("def _on_uninstall("):body.index("def _on_quit(")]
        self.assertIn('ui_client.run_confirmed(\n            Launcher, ["uninstall", "--check", "--json"],', body)
        self.assertIn("_ask_question(None, what, facts, action)", body)
        self.assertIn('f"/opt/onionpress/onionpress uninstall {flags};"', body)
        self.assertIn("flags = \" \".join(outcome.flags)", body)
        self.assertNotIn("Your data is preserved", body)
        self.assertNotIn("uninstall_warning(", body)


class TestTrayReadsTheDaemonsState(unittest.TestCase):
    """The tray's Status poller reads daemon-state.json through
    daemon_client.read_state, as the menubar does (#523), never
    status.json itself."""

    def setUp(self):
        self.tray = _read(TRAY)

    def test_the_poller_is_a_view_of_the_state_file(self):
        self.assertIn("from onionpress import daemon_client  # noqa: E402", self.tray)
        self.assertIn("STATE_FILE = daemon_client.state_path(DATA_DIR)", self.tray)
        self.assertIn("state = daemon_client.read_state(DATA_DIR)", self.tray)
        self.assertIn('status = state.get("status") if state else None', self.tray)
        for gone in ("STATUS_FILE", "status.json\")", "json.load", "import json", '"error_message"'):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, self.tray)
        tree = ast.parse(self.tray)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
                self.assertFalse(node.args and isinstance(node.args[0], ast.Name)
                                 and node.args[0].id.endswith("FILE"),
                                 f"line {node.lineno}: the tray opens a status file itself")

    def test_the_states_it_shows(self):
        # The writer's `state` is the daemon's display state: "stuck" and
        # "offline" are shown as starting, not as stopped.
        self.assertIn('STARTING_STATES = ("starting", "stuck", "offline")', self.tray)
        for reader in ("_update_icon", "_update_status_line", "_maybe_announce", "_maybe_show_setup"):
            with self.subTest(reader=reader):
                body = self.tray[self.tray.index(f"    def {reader}("):]
                body = body[:body.index("\n    def ", 10)]
                self.assertIn("STARTING_STATES", body)


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

    def test_the_trays_backup_form_is_accepted(self):
        """What ui_client.run_backup runs for the tray: `backup --json`
        with no path (the CLI's default) and the password in the
        environment, passed on to the CLI as given (issue #406)."""
        r = self._run("--json", env_password="pw")
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn("op_py --data-dir /d backup --json\n", r.stdout)
        self.assertIn("op_py sees password=pw", r.stdout)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""src/menubar.py performs no operation itself (issue #366, CLI-first
phase 2): every menu action and the Settings page's requests run
`onionpress <cmd>` through the launcher; the menubar keeps the dialogs,
the progress window and the notifications, and asks the questions the
CLI tells it to ask (onionpress.confirm). AppKit, so checked as source."""
import ast
import os
import re
import sys
import textwrap
import types
import unittest
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from onionpress import confirm  # noqa: E402

MENUBAR = os.path.join(PROJECT_ROOT, "src", "menubar.py")

# What the menubar may still use these modules for: reading the address
# or status, writing status.json as www-data, wp-cli reads. Never an
# action (a backup, a restore, an update, a key, an upload).
ALLOWED = {
    "key_manager": {"is_valid_onion_address"},
    "wp_files": {"WP_USER", "put_file_args", "wp_cli_args"},
}
NEVER_IMPORTED = ("backup", "hub_backup", "updater")

# Every action's launcher argv, as the source spells it.
ACTIONS = (
    '["backup", "--check", "--json", "--user", username]',
    '["backup", "--json", "-o", output_path, "--user", username]',
    '["restore", zip_path, "--check", "--json"]',
    'launcher_ops.run_launcher_restore(',
    '["update", "--check", "--json"]',
    '["update", "--yes", "--json"]',
    '["update-images", "--json"]',
    '["uninstall", "--check", "--json", *flags]',
    '["uninstall", "--json", *flags]',
    '[self.launcher_script, "handle-action", "--json", *args]',
    '[self.launcher_script, "config", "set", key]',
)


def _src():
    with open(MENUBAR, encoding="utf-8") as f:
        return f.read()


def _method(src, name):
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node)
    raise AssertionError(f"{name} not found in src/menubar.py")


class TestImports(unittest.TestCase):

    def setUp(self):
        self.src = _src()
        self.tree = ast.parse(self.src)

    def test_action_modules_are_not_imported(self):
        imported = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ImportFrom) and node.module == "onionpress":
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("onionpress."):
                imported.add(node.module.split(".", 1)[1])
        for name in NEVER_IMPORTED:
            with self.subTest(name=name):
                self.assertNotIn(name, imported)
        for alias in ("backup_manager", "hub_backup", "updater"):
            self.assertNotRegex(self.src, rf"\b{alias}\.")

    def test_the_remaining_uses_only_read(self):
        for module, allowed in ALLOWED.items():
            used = set(re.findall(rf"\b{module}\.([A-Za-z_]+)", self.src))
            with self.subTest(module=module):
                self.assertTrue(used, f"{module} is not used at all; drop it from ALLOWED")
                self.assertLessEqual(used, allowed, used - allowed)

    def test_the_old_handlers_are_gone(self):
        for name in ("_handle_requested_action", "poll_config_updates", "poll_requested_actions",
                     "_settings_page_key_import", "_settings_page_restore_confirmed",
                     "_release_onionheaven_before_restore", "_prune_dangling_images",
                     "_write_result"):
            with self.subTest(name=name):
                self.assertNotIn(f"def {name}(", self.src)
        self.assertNotIn("create_backup(", self.src)
        self.assertNotIn("restore_from_backup(", self.src)
        self.assertNotIn("download_and_install(", self.src)
        self.assertNotIn("apply_settings_updates(", self.src)
        self.assertNotIn("/var/lib/onionpress/requested-action", self.src)
        self.assertNotIn("config-updates.json", self.src)

    def test_every_action_is_a_launcher_subprocess(self):
        for argv in ACTIONS:
            with self.subTest(argv=argv):
                self.assertIn(argv, self.src)
        # No docker in an action: the status loop's reads are the only
        # docker calls left (status.json, the address, wp-cli reads).
        for name in ("backup", "restore", "check_for_updates", "_install_update",
                     "update_docker_images", "uninstall", "_proceed_with_uninstall",
                     "_view_tick", "_apply_state", "_answer_settings_page_question"):
            with self.subTest(method=name):
                body = _method(self.src, name)
                self.assertNotIn('"docker"', body)
                self.assertNotIn("self._docker.", body)

    def test_the_status_loop_runs_handle_action(self):
        # The status loop is the daemon's (onionpress.daemon, issue #367):
        # it runs handle-action every tick and leaves the question it
        # stops at in its state for the menubar.
        daemon = open(os.path.join(PROJECT_ROOT, "src", "onionpress", "daemon.py"), encoding="utf-8").read()
        self.assertIn("self.write_status_to_volume()\n                self.poll_settings_page()", daemon)
        self.assertIn('[self.launcher_script, "handle-action", "--json", *args]', self.src)


class TestDialogs(unittest.TestCase):
    """The questions the CLI asks, as native dialogs: Cancel is the
    default, no answer in 5 minutes is Cancel, every question has one."""

    def _app(self, answer, window_answer=None):
        src = _src()
        ns = {"rumps": mock.Mock()}
        if window_answer is not None:
            ns["rumps"].Window.return_value.run.return_value = window_answer
        for name in ("_ask_cli_question", "_ask_to_replace_address"):
            exec(textwrap.dedent(_method(src, name)), ns)
        app = types.SimpleNamespace(log=mock.Mock(), show_native_alert=mock.Mock(return_value=answer))
        for name in ("_ask_cli_question", "_ask_to_replace_address"):
            setattr(app, name, types.MethodType(ns[name], app))
        return app, ns["rumps"]

    def test_cancel_is_the_default_everywhere(self):
        facts = {"address-change": {"current": "a" * 56 + ".onion", "new": "b" * 56 + ".onion"},
                 "hub-holds-key": {"hubs": [{"content_address": "a" * 56 + ".onion",
                                             "onionheaven_address": "h" * 56 + ".onion"}]},
                 "reset": {"deletes": []},
                 "prefix-change": {"current": "op2", "new": "abc", "address": "op2" + "a" * 53 + ".onion"},
                 "clearnet-tunnel": {"replace": True}}
        for what, f in facts.items():
            for answer, want in ((1, True), (0, False), (None, False)):
                with self.subTest(what=what, answer=answer):
                    app, _ = self._app(answer)
                    self.assertIs(app._ask_cli_question(what, f, action="Uninstall"), want)
                    kwargs = app.show_native_alert.call_args.kwargs
                    self.assertEqual(kwargs.get("default_button"), 0)
                    self.assertEqual(kwargs.get("cancel_button"), 0)
                    self.assertEqual(kwargs["buttons"][0], "Cancel")

    def test_the_facts_reach_the_dialog(self):
        app, _ = self._app(1)
        app._ask_cli_question("hub-holds-key", {"hubs": [{"content_address": "x" * 56 + ".onion",
                                                          "onionheaven_address": "h" * 56 + ".onion"}]},
                              action="Reset")
        message = app.show_native_alert.call_args.kwargs["message"]
        self.assertIn("h" * 56, message)
        self.assertEqual(app.show_native_alert.call_args.kwargs["buttons"], ["Cancel", "Reset Anyway"])
        app._ask_cli_question("prefix-change", {"current": "op2", "new": "abc", "address": "z" * 56 + ".onion"})
        message = app.show_native_alert.call_args.kwargs["message"]
        self.assertIn("'op2'", message)
        self.assertIn("'abc'", message)
        self.assertIn("z" * 56, message)

    def test_uninstall_wants_delete_typed(self):
        for clicked, text, want in ((1, "DELETE", True), (1, "delete", True), (1, "yes", False),
                                    (0, "DELETE", False)):
            with self.subTest(clicked=clicked, text=text):
                app, rumps = self._app(1, window_answer=types.SimpleNamespace(clicked=clicked, text=text))
                self.assertIs(app._ask_cli_question("uninstall", {"deletes": []}), want)
                self.assertEqual(rumps.Window.call_args.kwargs["cancel"], "Cancel")

    def test_an_unknown_question_is_no(self):
        app, _ = self._app(1)
        self.assertIs(app._ask_cli_question("format-disk", {}), False)
        app.show_native_alert.assert_not_called()

    def test_every_question_is_handled(self):
        src = _src()
        body = _method(src, "_ask_cli_question")
        for what in confirm.QUESTIONS:
            if what == "sealed-hub-data-left-out":
                self.assertIn('facts.get("confirm") == "sealed-hub-data-left-out"', src)
            else:
                self.assertIn(f'if what == "{what}":', body)

    def test_no_answer_in_five_minutes_is_cancel(self):
        self.assertIn("max_wait = 300", _method(_src(), "show_native_alert"))


class TestFailuresSurface(unittest.TestCase):
    """A failed subprocess is a dialog or a notification, never only a log
    line (as the Linux tray's _notify_failed)."""

    def test_each_action_shows_the_clis_message(self):
        src = _src()
        for name, shown in (("backup", 'rumps.alert(title="Verification Failed", message=failure)'),
                            ("backup", 'pw.finish(\n                    f"Backup failed: {failure}"'),
                            ("restore", 'rumps.alert(title="Invalid Backup", message=failure)'),
                            ("restore", 'pw.finish(failure,\n                                                   title="Restore Failed")'),
                            ("check_for_updates", 'title="Update Check Failed"'),
                            ("_install_update", '"Update Failed",\n            f"Could not install the update automatically.\\n\\n{failure}'),
                            ("_proceed_with_uninstall", 'title="Uninstall Error"'),
                            ("_apply_state", '"Settings Page Requests Not Handled"')):
            with self.subTest(name=name):
                self.assertIn(shown, _method(src, name))

    def test_the_launcher_json_runner(self):
        """_run_launcher_json: the answer is the CLI's last JSON line, a
        secret goes in the environment, stdin is /dev/null unless input
        is given, and a run that cannot start is (-1, None)."""
        import subprocess
        src = _src()
        ns = {"os": os, "subprocess": subprocess, "threading": __import__("threading"),
              "op_confirm": confirm, "launcher_ops": __import__("onionpress.launcher_ops").launcher_ops}
        exec(textwrap.dedent(_method(src, "_run_launcher_json")), ns)
        app = types.SimpleNamespace(launcher_script="/bin/sh", log=mock.Mock())
        run = types.MethodType(ns["_run_launcher_json"], app)
        rc, ans, out, err = run(["-c", 'echo "[2026-10-22 12:00:00] working" >&2; echo "{\\"ok\\": true, \\"env\\": \\"$SECRET\\"}"'],
                                env={"SECRET": "s3"})
        self.assertEqual((rc, ans), (0, {"ok": True, "env": "s3"}))
        lines = []
        rc, ans, out, err = run(["-c", 'echo "[2026-10-22 12:00:00] step one" >&2; read x; echo "{\\"ok\\": true, \\"got\\": \\"$x\\"}"'],
                                input="hello\n", on_line=lines.append)
        self.assertEqual((rc, ans, lines), (0, {"ok": True, "got": "hello"}, ["step one"]))
        rc, ans, out, err = run(["-c", "read x; echo no-input-means-devnull-$x; exit 3"])
        self.assertEqual((rc, ans), (3, None))
        self.assertIn("no-input-means-devnull-", out)
        app.launcher_script = "/nonexistent/onionpress"
        rc, ans, out, err = run(["backup"])
        self.assertEqual((rc, ans), (-1, None))
        app.log.assert_called()


if __name__ == "__main__":
    unittest.main()

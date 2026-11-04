#!/usr/bin/env python3
"""src/menubar.py performs no operation itself (issue #366, CLI-first
phase 2; #526): every menu action and the Settings page's requests run
`onionpress <cmd>` through the launcher, by the UIs' shared client
(onionpress.ui_client, tests/test_ui_client.py: the protocol, the backup
and restore flows, the questions' wording); the menubar keeps the
dialogs, the progress window and the notifications. AppKit, so checked
as source."""
import ast
import os
import re
import sys
import textwrap
import time
import types
import unittest
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from onionpress import confirm, ui_client  # noqa: E402

# The UI processes' import closure is computed once, in test_mac_daemon.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_mac_daemon import ui_import_closure  # noqa: E402

MENUBAR = os.path.join(PROJECT_ROOT, "src", "menubar.py")

# What the menubar may still use these modules for: reading the address
# or status, writing status.json as www-data, wp-cli reads. Never an
# action (a backup, a restore, an update, a key, an upload).
ALLOWED = {
    "key_manager": {"is_valid_onion_address"},
}
NEVER_IMPORTED = ("backup", "hub_backup", "updater")

# Every action's launcher call, as the source spells it: through
# ui_client (the Launcher, or a flow of its own).
ACTIONS = (
    'ui_client.plan_backup(\n            self.launcher, _MacBackupUI(self)',
    'ui_client.run_backup(self.launcher, plan',
    'ui_client.plan_restore(self.launcher, zip_path, _MacRestoreUI(self)',
    'ui_client.run_restore(\n                    self.launcher, plan',
    'self.launcher.run_json(\n            "prepare-restore", zip_path, "--check", "--json", password=password',
    'self.launcher.run_json(\n                "prepare-restore", sw.restore_zip, "--json",\n                password=sw.restore_password',
    'self.launcher.run_json("update", "--check", "--json"',
    'self.launcher.run_json("update", "--yes", "--json"',
    'ui_client.run_confirmed(\n            self.launcher, ["uninstall", "--check", "--json"], self._ask_cli_question',
    'self.launcher.run_json(\n                    "uninstall", "--json", *flags',
    'self.launcher.run_json("handle-action", "--json", *args)',
    'ui_client.run_confirmed(\n            self.launcher, args, self._ask_cli_question, action="Change"',
    'self.launcher.run("daemon", "start"',
    'self.launcher.wait("start")',
    'self.launcher.wait("stop")',
    'self.launcher.wait("restart")',
    'self.launcher.run("quit", "--leave-menubar"',
    # Issue #527: the first-run install, the onionname, the browser.
    'self.launcher.run_json(\n                "setup", "--user", name, "--title", sw.site_title,',
    'self.launcher.run_json(\n                "onionname", "set", new_name, "--rename-admin", "--json"',
    'self.launcher.run_json("open", "onion", "--json"',
    'self.launcher.run_async("open", "site")',
    # Issue #546: the download page too; the first run's extension wait
    # is the CLI's.
    'self.launcher.run_async("open", "get-tor-browser")',
    'self._open_onion(extension_wait=FIRST_RUN_EXTENSION_WAIT)',
)


def _src():
    with open(MENUBAR, encoding="utf-8") as f:
        return f.read()


# Issue #547: what either UI may still run itself, by (method, program).
# Everything else is a launcher run through ui_client.Launcher (which is
# not here: it is ui_client's subprocess, never the UI's). The menubar's
# bundle-permissions fix and host cloudflared check, and the tray's
# `systemctl --user stop`, were operations of their own: now the
# updater's and the daemon's, and the launcher's `quit`.
SUBPROCESS_CALLS = ("run", "Popen", "call", "check_call", "check_output",
                    "getoutput", "getstatusoutput")
UI_SUBPROCESS_ALLOWED = {
    "src/menubar.py": {
        # The menubar's own single-instance pid file: a pid file older
        # than the boot is stale (a read of the host, for the UI itself).
        ("__init__", "sysctl"),
        # Desktop openers: the clipboard, a URL, a browser app.
        ("copy_address", "pbcopy"),
        ("show_browser_install_dialog", "open"),
        ("_auto_open_browser_inner", "open"),
        # After an update: waits for this process to exit, then `open`s
        # the new bundle (the menubar relaunching itself).
        ("_relaunch_app", "bash"),
    },
    "linux/onionpress-tray": {
        # Desktop openers: the data folder, a release page.
        ("_open_data_folder", "xdg-open"),
        ("_run", "xdg-open"),
        # A terminal emulator running the launcher's `uninstall` (argv
        # from the tray's list of terminals, so not a literal).
        ("_on_uninstall", None),
    },
}


def ui_subprocess_calls(rel):
    """Every subprocess call in *rel*, as (enclosing function, the argv's
    first word when it is a literal, else None)."""
    with open(os.path.join(PROJECT_ROOT, rel), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    found = []

    def visit(node, fn):
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
            if (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                    and child.func.attr in SUBPROCESS_CALLS
                    and isinstance(child.func.value, ast.Name)
                    and child.func.value.id in ("subprocess", "_sp")):
                prog = None
                first = child.args[0] if child.args else None
                if (isinstance(first, (ast.List, ast.Tuple)) and first.elts
                        and isinstance(first.elts[0], ast.Constant)):
                    prog = first.elts[0].value
                found.append((fn, prog))
            visit(child, name)
    visit(tree, None)
    return found


class UISubprocessCensus:
    """Mixed into a TestCase with REL: the UI runs no program but its
    allowed ones, and no other way to start one."""

    REL = ""

    def test_every_subprocess_call_is_allowed(self):
        allowed = UI_SUBPROCESS_ALLOWED[self.REL]
        calls = ui_subprocess_calls(self.REL)
        for call in calls:
            with self.subTest(call=call):
                self.assertIn(call, allowed, f"{self.REL} runs {call[1]!r} in {call[0]}: "
                              "an operation is a launcher verb (ui_client.Launcher) "
                              "or a daemon step, not the UI's own subprocess")
        # The list stays exact: a removed call leaves the list.
        self.assertEqual(set(calls), allowed)

    def test_no_other_way_to_run_a_program(self):
        with open(os.path.join(PROJECT_ROOT, self.REL), encoding="utf-8") as f:
            text = f.read()
        for needle in ("from subprocess import", "os.system(", "os.popen(", "os.spawn",
                       "os.exec", "pty.spawn", "asyncio.create_subprocess"):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, text)
        for m in re.finditer(r"^\s*import subprocess(?: as (\w+))?$", text, re.M):
            self.assertIn(m.group(1), (None, "_sp"))


class TestTheMenubarRunsNoOperation(UISubprocessCensus, unittest.TestCase):
    REL = "src/menubar.py"

    def test_the_moved_operations_are_gone(self):
        src = _src()
        for gone in ("_fix_app_bundle_permissions", "_check_host_cloudflared",
                     '"chmod"', '"chgrp"', '"pgrep"'):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, src)
        # It shows the daemon's answer.
        self.assertIn('ui_client.host_cloudflared_notice(state, "macos")', src)


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
        # The protocol is ui_client's: the menubar reads no answer itself.
        self.assertNotIn("confirm", imported)
        self.assertNotIn("launcher_ops", imported)
        self.assertIn("ui_client", imported)

    def test_action_modules_are_not_imported_transitively(self):
        """Issue #475: neither by the menubar, nor the windows it opens,
        nor anything those import, at the top or lazily (a subprocess
        imports them headless; test_mac_daemon.ui_import_closure). The
        welcome screen's restore opens the backup through `onionpress
        prepare-restore --check`, never backup.peek_backup_metadata."""
        loaded = ui_import_closure()
        for name in NEVER_IMPORTED:
            with self.subTest(name=name):
                self.assertNotIn(f"onionpress.{name}", loaded)
        self.assertIn("onionpress.ui_client", loaded)

    def test_the_welcome_restore_opens_the_backup_through_the_launcher(self):
        body = _method(self.src, "_check_welcome_backup")
        self.assertIn('"prepare-restore", zip_path, "--check", "--json", password=password', body)
        with open(os.path.join(os.path.dirname(MENUBAR), "onionpress", "setup_window.py"),
                  encoding="utf-8") as f:
            setup_window = f.read()
        self.assertNotIn("peek_backup_metadata", setup_window)
        self.assertIn("sw.set_on_check_backup(self._check_welcome_backup)", self.src)

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
                     "_write_result", "_run_launcher_json", "_launcher_failure",
                     "_ask_hub_recovery_key", "_ask_to_replace_address", "_short_onion",
                     "run_command",
                     # The onionname is `onionpress onionname set`'s, the
                     # admin's login the CLI's (backup.find_admin_username).
                     "_save_onionname", "_rename_wp_admin", "_read_onion_address"):
            with self.subTest(name=name):
                self.assertNotIn(f"def {name}(", self.src)
        self.assertNotIn("create_backup(", self.src)
        self.assertNotIn("restore_from_backup(", self.src)
        self.assertNotIn("download_and_install(", self.src)
        self.assertNotIn("apply_settings_updates(", self.src)
        self.assertNotIn("/var/lib/onionpress/requested-action", self.src)
        self.assertNotIn("config-updates.json", self.src)
        self.assertNotIn("UPDATE wp_users", self.src)
        self.assertNotIn("Registrar(", self.src)
        self.assertNotIn("validate_name", self.src)

    def test_every_action_is_a_launcher_subprocess(self):
        for argv in ACTIONS:
            with self.subTest(argv=argv):
                self.assertIn(argv, self.src)
        # Through the client only: no subprocess argv names the launcher
        # (tests/test_ui_client.py holds both UIs to that), and the one
        # client is made once.
        self.assertEqual(self.src.count("ui_client.Launcher("), 1)
        self.assertNotIn("[self.launcher_script", self.src)
        # No docker in an action: the status loop's reads are the only
        # docker calls left (status.json, the address, wp-cli reads).
        for name in ("backup", "restore", "check_for_updates", "_install_update",
                     "uninstall", "_proceed_with_uninstall",
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
        self.assertIn('self.launcher.run_json("handle-action", "--json", *args)', self.src)


class TestSettingsDialog(unittest.TestCase):
    """The Mac settings dialog (settings_ui.py) runs the launcher through
    the menubar's ui_client.Launcher and checks VM_MEMORY / VM_CPU with
    config's own checks (what `config set` takes), no limits of its own."""

    def setUp(self):
        with open(os.path.join(os.path.dirname(MENUBAR), "onionpress", "settings_ui.py"),
                  encoding="utf-8") as f:
            self.src = f.read()

    def test_the_hub_check_goes_through_the_client(self):
        self.assertIn('launcher.run_json("validate-oh-address", oh_addr, timeout=90)', self.src)
        self.assertNotIn("subprocess", self.src)
        self.assertNotIn("json.loads", self.src)
        self.assertIn("launcher=self.launcher,", _src())

    def test_vm_limits_are_configs(self):
        self.assertIn('for key, title in (("VM_MEMORY", "Invalid VM Memory"), ("VM_CPU", "Invalid VM CPUs")):',
                      self.src)
        self.assertIn("op_config.check_config_value(key, new_values[key])", self.src)
        self.assertNotIn('int(new_values["VM_MEMORY"])', self.src)
        self.assertNotIn('int(new_values["VM_CPU"])', self.src)


class TestViewTickKeepsTheState(unittest.TestCase):
    """The view loop's tick copies a fresh daemon state into self.state:
    auto_start's wait for the runtime, its port-conflict gate and the
    setup window's steps read it there. Without the copy self.state
    stayed the empty dict from __init__, _wait_for_state never came
    true and every launch waited the full 3 minutes (2.6.0, 2.6.1)."""

    def _app(self, state):
        src = _src()
        ns = {"os": os,
              "time": types.SimpleNamespace(time=time.time, monotonic=time.monotonic,
                                            sleep=lambda s: None),
              "op_daemon": types.SimpleNamespace(
                  read_state=lambda data_dir: state,
                  state_is_fresh=lambda doc, now: bool(doc))}
        for name in ("_view_tick", "_wait_for_state"):
            exec(textwrap.dedent(_method(src, name)), ns)
        app = types.SimpleNamespace(
            app_support=os.path.join(os.sep, "nonexistent", "onionpress"),
            state={}, _state_fresh=False, _daemon_seen=False, _daemon_missing_since=None,
            display_state="stopped", onion_address="", cloudflare_tunnel_enabled=False,
            _last_bootstrap_pct=0, wp_port=8080, _start_failed_rc=None, _last_painted=None,
            read_config_value=lambda key, default="": default,
            log=mock.Mock(), _apply_state=mock.Mock(), ensure_daemon_running=mock.Mock(),
            update_menu=mock.Mock(), handle_reopen=mock.Mock())
        for name in ("_view_tick", "_wait_for_state"):
            setattr(app, name, types.MethodType(ns[name], app))
        return app

    def test_a_fresh_state_is_copied_and_the_wait_sees_it(self):
        state = {"pid": 1, "version": "2.6.1", "updated_at": time.time(),
                 "runtime_ready": True, "port_conflict": [8080]}
        app = self._app(state)
        app._view_tick()
        self.assertTrue(app._state_fresh)
        self.assertEqual(app.state, state)
        app._apply_state.assert_called_once_with(state)
        self.assertTrue(app._wait_for_state(lambda s: s.get("runtime_ready"), 5))
        self.assertEqual(app.state.get("port_conflict"), [8080])
        app.ensure_daemon_running.assert_not_called()

    def test_no_state_is_not_fresh_and_the_wait_times_out(self):
        app = self._app(None)
        app._view_tick()
        self.assertFalse(app._state_fresh)
        self.assertEqual(app.state, {})
        app._apply_state.assert_not_called()
        app.ensure_daemon_running.assert_called_once()
        self.assertFalse(app._wait_for_state(lambda s: True, 0.05))


class TestDialogs(unittest.TestCase):
    """The questions the CLI asks, as native dialogs: worded by
    ui_client.question_text, Cancel is the default, no answer in 5
    minutes is Cancel, every question has one."""

    def _app(self, answer, window_answer=None):
        src = _src()
        ns = {"rumps": mock.Mock(), "ui_client": ui_client}
        if window_answer is not None:
            ns["rumps"].Window.return_value.run.return_value = window_answer
        for name in ("_ask_cli_question", "_ask_settings_page_question"):
            exec(textwrap.dedent(_method(src, name)), ns)
        app = types.SimpleNamespace(log=mock.Mock(), show_native_alert=mock.Mock(return_value=answer))
        for name in ("_ask_cli_question", "_ask_settings_page_question"):
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
                    q = ui_client.question_text(what, f, "Uninstall")
                    self.assertEqual(kwargs["buttons"][1], q.yes)
                    self.assertEqual((kwargs["title"], kwargs["message"]), (q.title, q.message))

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
                self.assertIs(app._ask_cli_question("uninstall", {"deletes": []}, action="Uninstall"), want)
                self.assertEqual(rumps.Window.call_args.kwargs["cancel"], "Cancel")
                self.assertEqual(rumps.Window.call_args.kwargs["ok"], "Confirm Deletion")
                if clicked and not want:
                    self.assertIn("(You typed: 'yes')", rumps.alert.call_args.kwargs["message"])

    def test_an_unknown_question_is_no(self):
        app, _ = self._app(1)
        self.assertIs(app._ask_cli_question("format-disk", {}), False)
        app.show_native_alert.assert_not_called()
        self.assertIs(app._ask_settings_page_question({"confirm": "format-disk"}), False)
        self.assertIs(app._ask_settings_page_question({"confirm": "reset"}), False)
        app.show_native_alert.assert_not_called()

    def test_the_settings_page_questions(self):
        for question, button in (({"confirm": "address-change", "current": "a" * 56 + ".onion",
                                   "new": "b" * 56 + ".onion"}, "Replace"),
                                 ({"confirm": "address-change", "current": "a" * 56 + ".onion",
                                   "new": "b" * 56 + ".onion", "action": "restore-backup"}, "Restore"),
                                 ({"confirm": "clearnet-tunnel", "replace": False}, "Set Token")):
            for answer, want in ((1, True), (0, False), (None, False)):
                with self.subTest(what=question["confirm"], button=button, answer=answer):
                    app, _ = self._app(answer)
                    self.assertIs(app._ask_settings_page_question(question), want)
                    kwargs = app.show_native_alert.call_args.kwargs
                    self.assertEqual(kwargs["buttons"], ["Cancel", button])
                    self.assertEqual((kwargs["default_button"], kwargs["cancel_button"]), (0, 0))
                    self.assertIn("confirmed" if want else "not confirmed", app.log.call_args.args[0])

    def test_every_question_is_handled(self):
        """Every question in confirm.QUESTIONS is worded (test_ui_client
        runs them); the menubar asks the sealed-hub-data one with its key
        field (_MacRestoreUI.ask_recovery_key), the rest in
        _ask_cli_question."""
        src = _src()
        for what in confirm.QUESTIONS:
            self.assertIsNotNone(ui_client.question_text(what, {}), what)
        self.assertIn("ui_client.question_text(ui_client.SEALED_HUB_DATA_QUESTION, facts)",
                      src[src.index("class _MacRestoreUI"):src.index("class OnionPressApp")])
        self.assertIn("ui_client.question_text(what, facts, action)", _method(src, "_ask_cli_question"))

    def test_no_answer_in_five_minutes_is_cancel(self):
        self.assertIn("max_wait = 300", _method(_src(), "show_native_alert"))
        self.assertEqual(ui_client.QUESTION_TIMEOUT_SEC, 300)


class TestFailuresSurface(unittest.TestCase):
    """A failed subprocess is a dialog or a notification, never only a log
    line (as the Linux tray's _notify_failed)."""

    def test_each_action_shows_the_clis_message(self):
        src = _src()
        for name, shown in (("backup", "result.message"),
                            ("restore", 'pw.finish(result.failure,\n                                                   title="Restore Failed")'),
                            ("check_for_updates", 'title="Update Check Failed"'),
                            ("_install_update", '"Update Failed",\n            f"Could not install the update automatically.\\n\\n{failure}'),
                            ("_proceed_with_uninstall", 'title="Uninstall Error"'),
                            ("_apply_state", '"Settings Page Requests Not Handled"')):
            with self.subTest(name=name):
                self.assertIn(shown, _method(src, name))
        ui = src[src.index("class _MacBackupUI"):src.index("class _MacRestoreUI")]
        self.assertIn('rumps.alert(title="Verification Failed", message=problem)', ui)
        ui = src[src.index("class _MacRestoreUI"):src.index("class OnionPressApp")]
        self.assertIn("rumps.alert(title=title, message=message)", ui)
        self.assertIn('rumps.alert(title="Recovery Key Not Accepted", message=problem)', ui)


if __name__ == "__main__":
    unittest.main()

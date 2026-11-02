#!/usr/bin/env python3
"""The backup password never travels in argv and never reaches a log.

Other local users can read any process's command line (ps on macOS,
/proc/<pid>/cmdline on Linux) but not its environment, so the menubar, the
Linux tray, scrub and both launchers hand the password to
`onionpress backup|restore` and `python -m onionpress.cli backup|restore` in
ONIONPRESS_BACKUP_PASSWORD (launcher_ops.BACKUP_PASSWORD_ENV). The tray used
to log the full launcher argv, which put the password in tray.log.

No docker: every subprocess is mocked, and the bash checks run only the
extracted read_backup_password function.
"""

import ast
import io
import os
import re
import subprocess
import sys
import types
import unittest
from typing import Optional
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from onionpress import backup, backup_archive, cli, scrub  # noqa: E402
from onionpress.launcher_ops import BACKUP_PASSWORD_ENV  # noqa: E402

SECRET = "s3cret pass'word"
LAUNCHERS = ["app/MacOS/onionpress", "linux/onionpress"]
TRAY = "linux/onionpress-tray"


def _read(rel_path):
    with open(os.path.join(PROJECT_ROOT, rel_path), encoding="utf-8") as f:
        return f.read()


def _bash_function(text, name):
    start = text.index(f"\n{name}() {{")
    end = text.index("\n}\n", start)
    return text[start:end + 3]


class TestEnvName(unittest.TestCase):
    def test_every_component_uses_the_same_variable(self):
        self.assertEqual(BACKUP_PASSWORD_ENV, "ONIONPRESS_BACKUP_PASSWORD")
        # The tray's launcher client is ui_client's (#526), which takes
        # the name from launcher_ops.
        self.assertIn("BACKUP_PASSWORD_ENV = launcher_ops.BACKUP_PASSWORD_ENV",
                      _read("src/onionpress/ui_client.py"))
        self.assertIn("Launcher = ui_client.Launcher(LAUNCHER, log)", _read(TRAY))
        for path in LAUNCHERS:
            body = _bash_function(_read(path), "read_backup_password")
            self.assertIn(f'"${{{BACKUP_PASSWORD_ENV}:-}}"', body, path)
            self.assertIn(f"unset {BACKUP_PASSWORD_ENV}", body, path)


class TestReadBackupPassword(unittest.TestCase):
    """onionpress.cli.read_backup_password: env, stdin, prompt; a positional is refused."""

    def test_env_is_used_and_removed(self):
        with mock.patch.dict(os.environ, {BACKUP_PASSWORD_ENV: SECRET}):
            self.assertEqual(cli.read_backup_password(None), SECRET)
            self.assertNotIn(BACKUP_PASSWORD_ENV, os.environ)

    def test_positional_is_refused(self):
        # Issue #504: exit 2, the environment emptied all the same, the
        # message naming no password.
        err = io.StringIO()
        with mock.patch.dict(os.environ, {BACKUP_PASSWORD_ENV: "other"}), \
             mock.patch("sys.stderr", err):
            with self.assertRaises(SystemExit) as cm:
                cli.read_backup_password(SECRET)
            self.assertNotIn(BACKUP_PASSWORD_ENV, os.environ)
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("ERROR", err.getvalue())
        self.assertNotIn(SECRET, err.getvalue())

    def test_stdin_line_when_not_a_terminal(self):
        env = {k: v for k, v in os.environ.items() if k != BACKUP_PASSWORD_ENV}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("sys.stdin", io.StringIO(SECRET + "\nignored\n")):
            self.assertEqual(cli.read_backup_password(None), SECRET)

    def test_prompt_without_echo_on_a_terminal(self):
        env = {k: v for k, v in os.environ.items() if k != BACKUP_PASSWORD_ENV}
        tty = mock.Mock()
        tty.isatty.return_value = True
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("sys.stdin", tty), \
             mock.patch("getpass.getpass", return_value=SECRET) as gp:
            self.assertEqual(cli.read_backup_password(None), SECRET)
        gp.assert_called_once()

    def test_nothing_given(self):
        env = {k: v for k, v in os.environ.items() if k != BACKUP_PASSWORD_ENV}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("sys.stdin", io.StringIO("")):
            self.assertEqual(cli.read_backup_password(None), "")


class TestCLIParsing(unittest.TestCase):
    """`backup [-o PATH] [--user U]` and `restore <zip>` take the password
    from the environment; the old positional forms are refused (#504)."""

    def test_backup_without_positionals(self):
        with mock.patch("onionpress.cli.OnionPressCLI") as M, \
             mock.patch.dict(os.environ, {BACKUP_PASSWORD_ENV: SECRET}):
            M.return_value.cmd_backup.return_value = 0
            self.assertEqual(cli.main(["backup"]), 0)
        M.return_value.cmd_backup.assert_called_once_with(
            SECRET, None, None, check=False, json_output=False)

    def test_backup_output_flag_and_user(self):
        with mock.patch("onionpress.cli.OnionPressCLI") as M, \
             mock.patch.dict(os.environ, {BACKUP_PASSWORD_ENV: SECRET}):
            M.return_value.cmd_backup.return_value = 0
            cli.main(["backup", "--output", "/tmp/b.zip", "--user", "bob"])
        M.return_value.cmd_backup.assert_called_once_with(
            SECRET, "/tmp/b.zip", "bob", check=False, json_output=False)

    def test_restore_with_path_only(self):
        with mock.patch("onionpress.cli.OnionPressCLI") as M, \
             mock.patch.dict(os.environ, {BACKUP_PASSWORD_ENV: SECRET}):
            M.return_value.cmd_restore.return_value = 0
            cli.main(["restore", "/tmp/b.zip"])
        M.return_value.cmd_restore.assert_called_once_with(
            SECRET, "/tmp/b.zip", "", confirmed=frozenset(), json_output=False)

    def test_legacy_positional_forms_are_refused(self):
        for argv in (["backup", "pw", "/tmp/b.zip"], ["restore", "pw", "/tmp/b.zip"]):
            err = io.StringIO()
            with self.subTest(argv=argv), \
                 mock.patch("onionpress.cli.OnionPressCLI") as M, \
                 mock.patch("sys.stderr", err), \
                 self.assertRaises(SystemExit) as cm:
                cli.main(argv)
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("visible to other users", err.getvalue())
            M.return_value.cmd_backup.assert_not_called()
            M.return_value.cmd_restore.assert_not_called()

    def test_password_leaves_environ_before_the_cli_is_built(self):
        # The Docker wrapper copies os.environ for its children when the CLI
        # is built, so the password has to be gone by then.
        seen = []

        def build(*a, **kw):
            seen.append(os.environ.get(BACKUP_PASSWORD_ENV))
            inst = mock.Mock()
            inst.cmd_restore.return_value = 0
            return inst

        with mock.patch("onionpress.cli.OnionPressCLI", side_effect=build), \
             mock.patch.dict(os.environ, {BACKUP_PASSWORD_ENV: SECRET}):
            cli.main(["restore", "/tmp/b.zip"])
        self.assertEqual(seen, [None])

    def test_no_password_fails_before_doing_anything(self):
        env = {k: v for k, v in os.environ.items() if k != BACKUP_PASSWORD_ENV}
        with mock.patch("onionpress.cli.OnionPressCLI") as M, \
             mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("sys.stdin", io.StringIO("")), \
             mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(cli.main(["backup"]), 1)
            self.assertEqual(cli.main(["restore", "/tmp/b.zip"]), 1)
        M.assert_not_called()


class TestTrayLauncher(unittest.TestCase):
    """The tray's `Launcher` is a ui_client.Launcher (#526): the password
    in the environment, the argument-carrying verbs logged by name."""

    def setUp(self):
        from onionpress import ui_client
        self.logs = []
        self.run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        for name, target in (("run", self.run), ("Popen", mock.Mock())):
            patcher = mock.patch.object(ui_client.subprocess, name, target)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.Launcher = ui_client.Launcher("/opt/onionpress/onionpress", self.logs.append)
        self.assertIn("Launcher = ui_client.Launcher(LAUNCHER, log)", _read(TRAY))

    def _assert_no_secret_in_logs(self):
        for line in self.logs:
            self.assertNotIn(SECRET, line)

    def test_backup_password_goes_in_env(self):
        self.Launcher.run("backup", capture=True, timeout=300, password=SECRET)
        argv = self.run.call_args.args[0]
        self.assertEqual(argv, ["/opt/onionpress/onionpress", "backup"])
        self.assertEqual(self.run.call_args.kwargs["env"][BACKUP_PASSWORD_ENV], SECRET)
        self._assert_no_secret_in_logs()

    def test_restore_password_goes_in_env(self):
        self.Launcher.run("restore", "/home/u/b.zip", capture=True, password=SECRET)
        argv = self.run.call_args.args[0]
        self.assertEqual(argv, ["/opt/onionpress/onionpress", "restore", "/home/u/b.zip"])
        self.assertNotIn(SECRET, argv)
        self.assertEqual(self.run.call_args.kwargs["env"][BACKUP_PASSWORD_ENV], SECRET)
        self._assert_no_secret_in_logs()

    def test_no_password_means_inherited_env(self):
        self.Launcher.run("stop", capture=True)
        self.assertIsNone(self.run.call_args.kwargs["env"])
        self.assertIn("/opt/onionpress/onionpress stop", self.logs[0])

    def test_old_argv_forms_are_not_logged(self):
        self.Launcher.run("restore", SECRET, "/home/u/b.zip")
        self.Launcher.run_async("backup", SECRET)
        self.Launcher.run("setup", "--pass", SECRET)
        self.assertEqual(len(self.logs), 3)
        self._assert_no_secret_in_logs()

    def test_timeout_is_logged_without_argv(self):
        self.run.side_effect = subprocess.TimeoutExpired(
            ["/opt/onionpress/onionpress", "restore", SECRET, "/b.zip"], 600)
        with self.assertRaises(subprocess.TimeoutExpired):
            self.Launcher.run("restore", SECRET, "/b.zip", timeout=600)
        self._assert_no_secret_in_logs()
        self.assertTrue(any("timed out" in line for line in self.logs))

    def test_other_commands_keep_their_args_in_the_log(self):
        self.Launcher.run_async("onionheaven", "enable")
        self.assertIn("onionheaven enable", self.logs[0])


_PASSWORDISH = re.compile(r"pass|^pw$|^pw_|_pw$", re.IGNORECASE)


def _names(node):
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            yield sub.id
        elif isinstance(sub, ast.Attribute):
            yield sub.attr


def _is_launcher_ref(node):
    return ((isinstance(node, ast.Name)
             and node.id in ("LAUNCHER", "LAUNCHER_BIN", "launcher_bin"))
            or (isinstance(node, ast.Attribute) and node.attr == "launcher_script"))


class TestNoPasswordInLauncherArgv(unittest.TestCase):
    """No Python caller builds a launcher argv out of a password variable."""

    FILES = [
        "src/menubar.py", TRAY,
        *sorted(os.path.join("src/onionpress", f)
                for f in os.listdir(os.path.join(PROJECT_ROOT, "src/onionpress"))
                if f.endswith(".py")),
    ]

    def test_launcher_argv_lists(self):
        offenders = []
        for path in self.FILES:
            tree = ast.parse(_read(path))
            for node in ast.walk(tree):
                if isinstance(node, (ast.List, ast.Tuple)) and node.elts \
                        and _is_launcher_ref(node.elts[0]):
                    for elt in node.elts[1:]:
                        for name in _names(elt):
                            if _PASSWORDISH.search(name):
                                offenders.append(f"{path}:{node.lineno} {name}")
        self.assertEqual(offenders, [], "pass the password in "
                         f"{BACKUP_PASSWORD_ENV}, not argv")

    def test_tray_launcher_calls(self):
        offenders = []
        tree = ast.parse(_read(TRAY))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr in ("run", "run_async") \
                    and isinstance(node.func.value, ast.Name) \
                    and node.func.value.id == "Launcher":
                for arg in node.args:
                    for name in _names(arg):
                        if _PASSWORDISH.search(name):
                            offenders.append(f"{TRAY}:{node.lineno} {name}")
        self.assertEqual(offenders, [])

    def test_menubar_restore_passes_env(self):
        # search + assertTrue: assertRegex would print all of menubar.py.
        # The menubar goes through ui_client.run_restore, which goes
        # through launcher_ops.run_launcher_restore (#323, #526).
        self.assertIn("ui_client.run_restore(", _read("src/menubar.py"))
        self.assertTrue(re.search(
            r'launcher_ops\.run_launcher_restore\(\s*\n\s*'
            r'launcher\.script, plan\.backup_path, plan\.password,',
            _read("src/onionpress/ui_client.py")),
            "ui_client.run_restore must go through launcher_ops.run_launcher_restore")
        self.assertTrue(re.search(
            r'\[launcher_script, "restore", zip_path, \*\(extra_args or \[\]\)\],\s*\n\s*'
            r'env=\{\*\*os\.environ, BACKUP_PASSWORD_ENV: password\} \| \(extra_env or \{\}\)',
            _read("src/onionpress/launcher_ops.py")),
            "run_launcher_restore must pass the password in the launcher's environment")


class TestScrubHandoff(unittest.TestCase):

    def test_phase_backup(self):
        with mock.patch.object(scrub, "_get_onion_address", return_value=""), \
             mock.patch.object(scrub, "_find_repo_dir", return_value="/home/x/op"), \
             mock.patch.object(scrub, "_run", return_value=subprocess.CompletedProcess(
                 [], 0, "", "")) as run, \
             mock.patch("os.path.isfile", return_value=True), \
             mock.patch("os.path.getsize", return_value=1000):
            state = scrub.phase_backup(SECRET, log=lambda _: None)
        argv = run.call_args.args[0]
        self.assertEqual(argv, [scrub.LAUNCHER_BIN, "backup", "--output", state.backup_path])
        self.assertEqual(run.call_args.kwargs["env"][BACKUP_PASSWORD_ENV], SECRET)

    def test_phase_restore(self):
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess(
                [], 0, "", "")) as run:
            scrub.phase_restore(SECRET, "/b.zip", log=lambda _: None)
        self.assertEqual(run.call_args.args[0], [scrub.LAUNCHER_BIN, "restore", "/b.zip"])
        self.assertEqual(run.call_args.kwargs["env"][BACKUP_PASSWORD_ENV], SECRET)


class TestUnzipTimeoutLog(unittest.TestCase):
    """str(TimeoutExpired) is the argv, and unzip's argv has -P <password>."""

    def test_timeout_log_has_no_password(self):
        logs = []
        argv = ["unzip", "-o", "-qq", "-P", SECRET, "/b.zip"]

        class _Stuck:
            """An unzip that never finishes until killed."""
            killed = False

            def wait(self, timeout=None):
                if self.killed:
                    return -9
                raise subprocess.TimeoutExpired(argv, timeout)

            def kill(self):
                self.killed = True

        with mock.patch("shutil.which", return_value="/usr/bin/unzip"), \
             mock.patch.object(backup.subprocess, "Popen", return_value=_Stuck()), \
             mock.patch.object(backup_archive, "_UNZIP_TIMEOUT_S", 0), \
             mock.patch.object(backup_archive, "_free_bytes", return_value=0):
            self.assertFalse(backup._unzip_cli_extract(
                "/b.zip", SECRET, "/tmp/x", logs.append,
                max_file_bytes=1, max_total_bytes=1))
        self.assertTrue(any("timed out" in line for line in logs), logs)
        for line in logs:
            self.assertNotIn(SECRET, line)


class TestLaunchers(unittest.TestCase):

    def test_helper_is_identical(self):
        bodies = [_bash_function(_read(p), "read_backup_password") for p in LAUNCHERS]
        self.assertEqual(bodies[0], bodies[1],
                         "read_backup_password() must be identical in both launchers")

    def test_no_xtrace(self):
        for path in LAUNCHERS:
            self.assertIsNone(re.search(r"(?m)^\s*set -[a-z]*x", _read(path)),
                              f"{path}: xtrace would print the password")

    def test_no_password_in_backup_or_restore_argv(self):
        call = re.compile(r'(?:op_py|onionpress\.cli)\s+(?:--data-dir\s+"\$DATA_DIR"\s+)?'
                          r"(?:backup|restore)\b([^\n]*)")
        for path in LAUNCHERS:
            # backup and restore (both pass-throughs since #535/#529).
            self.assertGreaterEqual(len(call.findall(_read(path))), 2, path)
            for m in call.finditer(_read(path)):
                self.assertNotRegex(m.group(1), r"(?i)\$\{?\w*pass",
                                    f"{path}: {m.group(0).strip()}")

    def test_restore_hands_everything_to_the_cli(self):
        # #529: the restore case is the pass-through; the CLI reads the
        # password itself (cli.read_backup_password: the environment,
        # stdin or a prompt) and refuses one on argv. A hub's recovery key
        # still travels in the environment. `setup --from-backup` keeps
        # the bash read (read_backup_password, below) for its own argv.
        for path in LAUNCHERS:
            self.assertTrue(re.search(
                r'ONIONPRESS_HUB_RECOVERY_KEY="\$_HUB_RECOVERY_KEY" '
                r'(?:op_py|bundled_python -m onionpress\.cli) --data-dir "\$DATA_DIR" '
                r'restore "\$\{@:2\}"', _read(path)), path)
            self.assertNotIn('restore "$restore_path"', _read(path), path)

    def _run_helper(self, arg="", env_pw=None, stdin="", rc=0):
        func = _bash_function(_read("linux/onionpress"), "read_backup_password")
        script = (
            "set -e\n" + func +
            'read_backup_password "$1"\n'
            "printf '%s\\n' \"$BACKUP_PASS\"\n"
            "sh -c 'printf \"%s\\n\" \"${ONIONPRESS_BACKUP_PASSWORD-unset}\"'\n"
        )
        env = {k: v for k, v in os.environ.items() if k != BACKUP_PASSWORD_ENV}
        if env_pw is not None:
            env[BACKUP_PASSWORD_ENV] = env_pw
        r = subprocess.run(["bash", "-c", script, "bash", arg], input=stdin,
                           capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual(r.returncode, rc, r.stderr)
        if rc:
            return r.stdout, "", r.stderr
        got, child = r.stdout.split("\n")[:2]
        return got, child, r.stderr

    def test_helper_reads_env_and_hides_it_from_children(self):
        got, child, _ = self._run_helper(env_pw=SECRET)
        self.assertEqual(got, SECRET)
        self.assertEqual(child, "unset")

    def test_helper_reads_a_stdin_line(self):
        got, _, _ = self._run_helper(stdin=SECRET + "\nmore\n")
        self.assertEqual(got, SECRET)

    def test_helper_survives_empty_stdin_under_set_e(self):
        got, _, _ = self._run_helper(stdin="")
        self.assertEqual(got, "")

    def test_helper_refuses_a_positional_password(self):
        # Issue #504: the old positional form is an error (exit 2), not a
        # warning; nothing is printed or passed on, the message names no
        # password.
        out, _, err = self._run_helper(arg=SECRET, env_pw="other", rc=2)
        self.assertEqual(out, "")
        self.assertIn("ERROR", err)
        self.assertIn("refused", err)
        self.assertNotIn(SECRET, err)
        self.assertNotIn("other\n", err)


if __name__ == "__main__":
    unittest.main()

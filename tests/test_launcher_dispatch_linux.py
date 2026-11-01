#!/usr/bin/env python3
"""The Linux launcher's dispatch, run: `onionpress <verb> ...` through
linux/onionpress's real `main` (and its real top level, under a scratch
HOME and install dir), with docker, systemctl, the Python CLI and the
heavy helpers stubbed (tests/launcher_harness.py).

Until these tests only `backup`, `update`, `import-key` and the Settings
page's cases were run; `start`'s lock, `stop`'s report of a failed stop,
`restore`'s parser and its heartbeat hand-off, `status`, bare `logs`, the
global -y filter and the unknown verb were read as text or not at all.
The last class holds the tray to the launcher: every verb it runs is one
`main` has.

Runs under /bin/bash where that is all there is (macOS: 3.2), so the
launcher stays within what both platforms' bash accept."""

import ast
import os
import re
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from launcher_harness import BASH, LINUX, PROJECT_ROOT, Launcher  # noqa: E402

TRAY = os.path.join(PROJECT_ROOT, "linux", "onionpress-tray")


def _dead_pid():
    p = subprocess.Popen(["/usr/bin/true"])
    p.wait()
    return p.pid


@unittest.skipUnless(os.path.exists(BASH), "needs /bin/bash")
class _Case(unittest.TestCase):
    STUBS = ()

    def setUp(self):
        self.l = Launcher(self, "linux", stub=self.STUBS)

    def dd(self):
        return f"--data-dir {self.l.data_dir}"


class TestStart(_Case):
    STUBS = ("setup_db_passwords", "start_containers", "wait_for_services",
             "start_container_log_capture", "write_status", "_status_poll_loop")

    def test_a_start_runs_the_stack_then_waits_on_the_poll_loop(self):
        r = self.l.run("start")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.l.fn_calls(), [
            "setup_db_passwords", "start_containers", "wait_for_services"])
        # The daemon after the stack; the address line reads the served
        # address through the CLI (#529).
        self.assertEqual(self.l.py_calls(), ["check-docker",
                                             f"{self.dd()} daemon start --launcher {self.l.script}",
                                             f"{self.dd()} address"])
        self.assertIn("OnionPress is running!", r.stdout)
        self.assertIn("Onion address: none yet", r.stdout)
        self.assertFalse(os.path.exists(self.l.pidfile), "the EXIT trap removes the lock")

    def test_no_verb_means_start(self):
        r = self.l.run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("start_containers", self.l.fn_calls())

    def test_no_docker_no_start(self):
        r = self.l.run("start", env={"STUB_RC_DOCKER_INFO": "1"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("Docker is not available", r.stdout)
        self.assertEqual(self.l.fn_calls(), [])

    def test_a_live_start_holds_the_lock(self):
        os.makedirs(self.l.data_dir, exist_ok=True)
        with open(self.l.pidfile, "w") as f:
            f.write(str(os.getpid()))
        r = self.l.run("start")
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"already running (PID {os.getpid()})", r.stderr)
        self.assertEqual(self.l.fn_calls(), [])
        self.assertTrue(os.path.exists(self.l.pidfile), "another start's lock is kept")

    def test_a_dead_starts_lock_is_taken_over(self):
        os.makedirs(self.l.data_dir, exist_ok=True)
        with open(self.l.pidfile, "w") as f:
            f.write(str(_dead_pid()))
        r = self.l.run("start")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("start_containers", self.l.fn_calls())

    def test_the_start_returns_and_the_daemon_is_the_units(self):
        """onionpress.service is a oneshot that stays active (#407): no
        systemd-notify, no loop; the daemon is asked to start, its
        failure a warning."""
        r = self.l.run("start", env={"NOTIFY_SOCKET": "/run/x"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("bin systemd-notify --ready", self.l.calls())
        self.assertNotIn("_status_poll_loop", self.l.fn_calls())


class TestStartDaemon(_Case):
    STUBS = ("setup_db_passwords", "start_containers", "wait_for_services")

    def test_start_runs_daemon_start_after_the_stack(self):
        r = self.l.run("start")
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.l.calls()
        # The daemon after the stack; the address line's `address` (#529) follows.
        self.assertEqual(self.l.py_calls()[-2:],
                         [f"{self.dd()} daemon start --launcher {self.l.script}", f"{self.dd()} address"])
        self.assertLess(calls.index("fn wait_for_services"),
                        calls.index(f"py {self.dd()} daemon start --launcher {self.l.script}"))

    def test_a_daemon_that_does_not_start_is_only_a_warning(self):
        # start_daemon alone (STUB_RC_PY would fail the start's own
        # check-docker call first): its op_py failing is a logged warning.
        with open(LINUX) as f:
            text = f.read()
        start = text.index("\nstart_daemon() {")
        fn = text[start:text.index("\n}\n", start) + 3]
        log = os.path.join(self.l.data_dir, "onionpress.log")
        os.makedirs(self.l.data_dir, exist_ok=True)
        script = (f"DATA_DIR={self.l.data_dir!r}\nLOG_FILE={log!r}\nINSTALL_DIR=/opt/onionpress\n"
                  'log() { echo "$1" >> "$LOG_FILE"; }\nop_py() { return 1; }\n'
                  + fn + "start_daemon\necho rc=$?\n")
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.stdout.strip(), "rc=0", r.stderr)
        with open(log) as f:
            self.assertIn("WARNING: the OnionPress daemon could not be started", f.read())


class TestStopAndRestart(_Case):
    STUBS = ("setup_db_passwords", "stop_onionheaven_farm", "start_containers",
             "wait_for_services", "start_daemon")

    def test_stop_asks_the_daemon_offline_then_stops_the_stack(self):
        r = self.l.run("stop")
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.l.calls()
        # Always with the cloudflared override, so a removed token's tunnel goes too.
        down = "bin docker compose -f docker-compose.yml -f docker-compose.cloudflare.yml down"
        self.assertEqual(self.l.py_calls(), [f"{self.dd()} daemon offline"])
        self.assertLess(calls.index(f"py {self.dd()} daemon offline"), calls.index(down))
        self.assertIn("Containers stopped", r.stdout)
        with open(os.path.join(self.l.data_dir, ".previous-port-offset")) as f:
            self.assertEqual(f.read().strip(), "0")

    def test_a_daemon_that_cannot_go_offline_does_not_stop_the_stop(self):
        r = self.l.run("stop", env={"STUB_RC_PY": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Containers stopped", r.stdout)

    def test_a_failed_compose_down_is_the_exit_status(self):
        r = self.l.run("stop", env={"STUB_RC_DOCKER_DOWN": "3"})
        self.assertEqual(r.returncode, 3)
        self.assertIn("ERROR: stopping the containers failed", r.stdout)
        self.assertNotIn("write_status", self.l.fn_calls(), "the daemon's pass reports the state")

    def test_restart(self):
        r = self.l.run("restart")
        self.assertEqual(r.returncode, 0, r.stderr)
        fns = self.l.fn_calls()
        calls = self.l.calls()
        self.assertLess(calls.index(f"py {self.dd()} daemon offline"), calls.index("fn start_containers"))
        self.assertEqual(fns[-1], "start_daemon")
        self.assertNotIn("start_container_log_capture", fns)

    def test_a_failed_stop_still_restarts(self):
        r = self.l.run("restart", env={"STUB_RC_DOCKER_DOWN": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("WARNING: restart: the stop failed", r.stdout)
        self.assertIn("start_containers", self.l.fn_calls())
        self.assertEqual(self.l.fn_calls()[-1], "start_daemon")


class TestStatusAndLogs(_Case):
    def test_status_is_the_clis(self):
        # #529: `onionpress status` (cmd_status's JSON), no bash compose ps.
        r = self.l.run("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.l.py_calls(), [f"{self.dd()} status"])
        self.assertNotIn("bin docker compose ps", "\n".join(self.l.calls()))

    def test_bare_logs_follow_the_containers(self):
        r = self.l.run("logs")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("bin docker compose logs -f", self.l.calls())
        self.assertEqual(self.l.py_calls(), [])

    def test_logs_with_flags_are_the_clis(self):
        r = self.l.run("logs", "--launcher", "-n", "20")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.l.py_calls(), [f"{self.dd()} logs --launcher -n 20"])


class TestDelegations(_Case):
    STUBS = ("setup_db_passwords",)

    def test_the_cli_gets_the_data_dir_and_every_argument(self):
        rows = [
            (("address", "--url"), "address --url"),
            (("version", "--json"), "version --json"),
            (("open", "settings"), "open settings"),
            (("onionname", "show", "--json"), "onionname show --json"),
            (("config", "set", "PREVENT_SLEEP", "never"), "config set PREVENT_SLEEP never"),
            (("config", "unset", "X", "--json"), "config unset X --json"),
            (("check-reachability", "--json"), "check-reachability --json"),
            (("hub-recovery-key", "check"), "hub-recovery-key check"),
        ]
        for argv, want in rows:
            with self.subTest(argv=argv):
                r = self.l.run(*argv)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(self.l.py_calls(), [f"{self.dd()} {want}"])

    def test_the_clis_exit_status_is_the_launchers(self):
        r = self.l.run("config", "get", "NOPE", env={"STUB_RC_PY": "2"})
        self.assertEqual(r.returncode, 2)

    def test_yes_is_taken_out_of_every_argument_list(self):
        # The global filter: -y/--yes anywhere means ONIONPRESS_YES=true and
        # never reaches the verb (scripts run `uninstall -y`, `reset --yes`).
        r = self.l.run("config", "list", "-y", "--json", "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.l.py_calls(), [f"{self.dd()} config list --json"])


class TestUpdate(_Case):
    """`update` is the Python's (one updater on both platforms); the
    launcher keeps the two install leaves it calls back."""
    STUBS = ("update_install_deb", "update_from_checkout")

    def test_update_delegates_with_this_launcher(self):
        for argv, want in ((("update",), "update --launcher {l}"),
                           (("update", "--check", "--json"), "update --launcher {l} --check --json"),
                           (("update", "--version", "2.6.0", "--pre", "--force"),
                            "update --launcher {l} --version 2.6.0 --pre --force"),
                           (("update", "--from-checkout"), "update --launcher {l} --from-checkout")):
            with self.subTest(argv=argv):
                r = self.l.run(*argv)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(self.l.py_calls(), [f"{self.dd()} {want.format(l=self.l.script)}"])
                self.assertEqual(self.l.fn_calls(), [])

    def test_yes_reaches_the_python(self):
        # The global -y filter takes --yes out of argv: it comes back.
        r = self.l.run("update", "-y")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.l.py_calls(), [f"{self.dd()} update --launcher {self.l.script} --yes"])
        r = self.l.run("update", "--check", env={"ONIONPRESS_YES": "true"})
        self.assertEqual(self.l.py_calls(),
                         [f"{self.dd()} update --launcher {self.l.script} --check --yes"])

    def test_the_leaves_run_the_bash(self):
        r = self.l.run("update-install-deb", "/tmp/x/onionpress.deb", "--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.l.calls(), ["fn update_install_deb /tmp/x/onionpress.deb --force"])
        r = self.l.run("update-from-checkout")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.l.calls(), ["fn update_from_checkout"])
        r = self.l.run("update-install-deb", "/x.deb", env={"STUB_RC_UPDATE_INSTALL_DEB": "1"})
        self.assertEqual(r.returncode, 1)


class TestBackup(_Case):
    """`backup` is the CLI's, as the Mac's (issue #406): every argument
    passed on, the password left in the environment for the CLI to read
    (it refuses a positional one, issue #504, and parses the flags), the
    CLI's exit status the launcher's."""

    STUBS = ("setup_db_passwords",)

    def test_the_backup_is_the_clis_with_the_arguments_passed_on(self):
        for argv in (("-o", "/b/x.zip"), ("--output", "/b/x.zip"), ("--output=/b/x.zip",),
                     (), ("--check", "--user=bob"), ("-o", "/b/x.zip", "--json"),
                     ("--confirmed", "x"), ("oldpw", "/b/y.zip")):
            with self.subTest(argv=argv):
                r = self.l.run("backup", *argv, env={"ONIONPRESS_BACKUP_PASSWORD": "pw"})
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(self.l.py_calls(), [f"{self.dd()} backup" + "".join(" " + a for a in argv)])
                self.assertIn("py-env pw", self.l.calls())
                self.assertEqual(self.l.fn_calls(), [])

    def test_the_clis_status_is_the_launchers(self):
        r = self.l.run("backup", "--check", "--json", env={"STUB_RC_PY": "5"})
        self.assertEqual(r.returncode, 5)
        self.assertEqual(self.l.py_calls(), [f"{self.dd()} backup --check --json"])
        self.assertIn("py-env <none>", self.l.calls())

    def test_nothing_of_the_backup_is_bash(self):
        with open(LINUX) as f:
            text = f.read()
        for name in ("create_backup", "restore_backup", "key_volume_cat", "db_client", "db_import"):
            self.assertNotIn(f"\n{name}() {{", text, name)


class TestRestore(_Case):
    """`restore` is the CLI's argparse (#529): every argument reaches it,
    the password travels in the environment (or on stdin, to the CLI
    itself), and the CLI's exit status is the launcher's."""

    STUBS = ("setup_db_passwords",)

    def py(self, *rest):
        return f"{self.dd()} restore " + " ".join(rest)

    def test_the_password_goes_in_the_environment(self):
        r = self.l.run("restore", "/b/x.zip", env={"ONIONPRESS_BACKUP_PASSWORD": "s3cret"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.l.py_calls(), [self.py("/b/x.zip")])
        self.assertEqual(self.l.fn_calls(), ["setup_db_passwords"])
        self.assertIn("py-env s3cret", self.l.calls())
        self.assertNotIn("s3cret", " ".join(self.l.py_calls()))

    def test_every_argument_reaches_the_cli_verbatim(self):
        # The legacy positional password, a bare `restore`, --confirmed in
        # either form, an unknown flag: the CLI's parser answers them
        # (cli.read_backup_password's refusal, argparse's usage), not bash.
        for argv in (("oldpw", "/b/x.zip"), (), ("--confirmed", "sealed-hub-data-left-out", "/b/x.zip", "--json"),
                     ("/b/x.zip", "--confirmed=sealed-hub-data-left-out"), ("/b/x.zip", "--bogus"),
                     ("/b/x.zip", "--check", "--json")):
            with self.subTest(argv=argv):
                r = self.l.run("restore", *argv, env={"ONIONPRESS_BACKUP_PASSWORD": "pw"})
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(self.l.py_calls(), [self.py(*argv).rstrip()])

    def test_the_clis_exit_status_is_the_launchers(self):
        # 5: {"ask": "password"} for --check without one; 3: refused; 1: failed.
        for rc in ("5", "3", "1"):
            with self.subTest(rc=rc):
                r = self.l.run("restore", "/b/x.zip", "--check", "--json", env={"STUB_RC_PY": rc})
                self.assertEqual(r.returncode, int(rc))
                self.assertEqual(self.l.py_calls(), [self.py("/b/x.zip", "--check", "--json")])
                self.assertEqual(r.stdout + r.stderr, "", "nothing of bash's own is said")

    def test_the_recovery_key_travels_in_the_environment(self):
        self.l.add_stubs('op_py() { _op_rec "hubkey ${ONIONPRESS_HUB_RECOVERY_KEY:-unset}"; _op_py "$@"; }\n'
                         'bundled_python() { shift 2; op_py "$@"; }')
        r = self.l.run("restore", "/b/x.zip", env={"ONIONPRESS_HUB_RECOVERY_KEY": "rk",
                                                   "ONIONPRESS_BACKUP_PASSWORD": "pw"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("hubkey rk", self.l.calls())
        self.assertNotIn("rk", " ".join(self.l.py_calls()))


class TestUsage(_Case):
    def test_help_is_the_clis_text_and_exits_0(self):
        # #529: one text for both platforms (cli.launcher_help, tested in
        # tests/test_launcher_help.py); the case prints it and exits 0.
        for flag in ("help", "-h", "--help"):
            with self.subTest(flag=flag):
                r = self.l.run(flag)
                self.assertEqual(r.returncode, 0)
                self.assertEqual(self.l.py_calls(), [f"{self.dd()} launcher-help"])

    def test_an_unknown_verb_reaches_the_cli(self):
        """The default case hands every other verb to onionpress.cli (the
        tray's check-for-update has no case of its own); the CLI's own
        usage and exit status answer one it does not have either."""
        r = self.l.run("frobnicate", "--json", env={"STUB_RC_PY": "2"})
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stdout, "")
        self.assertEqual(self.l.py_calls(), [f"{self.dd()} frobnicate --json"])
        self.assertEqual(self.l.fn_calls(), [])


class TestTrayRunsOnlyLauncherVerbs(unittest.TestCase):
    """Every `Launcher.run*("<verb>", ...)` in linux/onionpress-tray (the
    shared ui_client.Launcher) names a case of linux/onionpress's main or
    a subcommand of onionpress.cli (the main's default case hands those
    to it); `handle-action` takes its request from the request files,
    never a positional argument (the tray's old Regenerate item ran
    `handle-action regenerate-vanity`, which the CLI refuses with exit 2).
    The tray runs no `python -m onionpress.cli` of its own."""

    def _verbs(self):
        with open(LINUX, encoding="utf-8") as f:
            text = f.read()
        main = text[text.index("\nmain() {"):]
        verbs = set()
        for m in re.finditer(r"^        ([a-z|-]+)\)$", main, re.M):
            verbs.update(m.group(1).split("|"))
        from test_cli_dispatch import parser_tree
        verbs.update(key[0] for key in parser_tree())
        return verbs

    def _tray_calls(self):
        with open(TRAY, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and node.args
                    and isinstance(node.args[0], ast.Constant)):
                continue
            f = node.func
            if (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                    and f.value.id == "Launcher"):
                yield node.lineno, [a.value if isinstance(a, ast.Constant) else None for a in node.args]

    def test_the_tray_runs_no_python_of_its_own(self):
        with open(TRAY, encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn('"onionpress.cli"', text)
        self.assertNotIn("sys.executable", text)

    def test_every_verb_is_the_launchers(self):
        verbs = self._verbs()
        self.assertIn("restore", verbs)
        calls = list(self._tray_calls())
        self.assertGreater(len(calls), 5)
        for line, args in calls:
            with self.subTest(line=line):
                self.assertIn(args[0], verbs)

    def test_handle_action_gets_no_arguments(self):
        for line, args in self._tray_calls():
            if args[0] == "handle-action":
                self.assertEqual(args, ["handle-action"], f"onionpress-tray:{line}")


if __name__ == "__main__":
    unittest.main()

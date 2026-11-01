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
import json
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
            "setup_db_passwords", "start_containers", "wait_for_services",
            "start_container_log_capture", "write_status", "_status_poll_loop"])
        self.assertIn("OnionPress is running!", r.stdout)
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

    def test_systemd_is_told_when_ready(self):
        r = self.l.run("start", env={"NOTIFY_SOCKET": "/run/x"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("bin systemd-notify --ready", self.l.calls())


class TestStopAndRestart(_Case):
    STUBS = ("setup_db_passwords", "stop_heartbeat_client", "start_heartbeat_client",
             "stop_container_log_capture",
             "stop_onionheaven_farm", "write_status", "start_containers",
             "wait_for_services", "start_container_log_capture")

    def test_stop_stops_the_heartbeat_then_the_stack(self):
        r = self.l.run("stop")
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.l.calls()
        # Always with the cloudflared override, so a removed token's tunnel goes too.
        down = "bin docker compose -f docker-compose.yml -f docker-compose.cloudflare.yml down"
        self.assertLess(calls.index("fn stop_heartbeat_client"), calls.index(down))
        self.assertIn("Containers stopped", r.stdout)
        with open(os.path.join(self.l.data_dir, ".previous-port-offset")) as f:
            self.assertEqual(f.read().strip(), "0")

    def test_a_failed_compose_down_is_the_exit_status(self):
        r = self.l.run("stop", env={"STUB_RC_DOCKER_DOWN": "3"})
        self.assertEqual(r.returncode, 3)
        self.assertIn("ERROR: stopping the containers failed", r.stdout)
        self.assertIn("write_status", self.l.fn_calls(), "the tray still learns the state")

    def test_restart(self):
        r = self.l.run("restart")
        self.assertEqual(r.returncode, 0, r.stderr)
        fns = self.l.fn_calls()
        self.assertLess(fns.index("stop_heartbeat_client"), fns.index("start_containers"))
        self.assertEqual(fns[-1], "start_heartbeat_client")
        self.assertIn("start_container_log_capture", fns)

    def test_a_failed_stop_still_restarts(self):
        r = self.l.run("restart", env={"STUB_RC_DOCKER_DOWN": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("WARNING: restart: the stop failed", r.stdout)
        self.assertIn("start_containers", self.l.fn_calls())
        self.assertEqual(self.l.fn_calls()[-1], "start_heartbeat_client")


class TestStatusAndLogs(_Case):
    def test_status_is_one_json_array(self):
        r = self.l.run("status", env={"STUB_PS": '{"Name":"onionpress-tor"}\n{"Name":"onionheaven"}'})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([c["Name"] for c in json.loads(r.stdout)],
                         ["onionpress-tor", "onionheaven"])

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


class TestBackup(_Case):
    STUBS = ("setup_db_passwords", "create_backup")

    def test_the_backup_is_create_backups_at_the_path_given(self):
        for flag in (("-o", "/b/x.zip"), ("--output", "/b/x.zip"), ("--output=/b/x.zip",)):
            with self.subTest(flag=flag):
                r = self.l.run("backup", *flag, env={"ONIONPRESS_BACKUP_PASSWORD": "pw"})
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("fn create_backup pw /b/x.zip", self.l.calls())
                self.assertIn("Backup saved to: /b/x.zip", r.stdout)

    def test_the_default_path_is_under_onionpress_backups(self):
        r = self.l.run("backup", env={"ONIONPRESS_BACKUP_PASSWORD": "pw"})
        self.assertEqual(r.returncode, 0, r.stderr)
        call = [c for c in self.l.calls() if c.startswith("fn create_backup")][0]
        self.assertRegex(call, re.escape(os.path.join(self.l.home, "OnionPress", "backups"))
                         + r"/onionpress-\d{4}-\d\d-\d\d-\d+\.zip$")

    def test_json_adds_the_answer(self):
        r = self.l.run("backup", "-o", "/b/x.zip", "--json", env={"ONIONPRESS_BACKUP_PASSWORD": "pw"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout.strip().splitlines()[-1]),
                         {"ok": True, "output": "/b/x.zip"})

    def test_the_old_positional_password_is_refused(self):
        # Issue #504: nothing runs, the message names no password.
        r = self.l.run("backup", "oldpw", "/b/y.zip")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertNotIn("create_backup", self.l.fn_calls())
        self.assertIn("refused", r.stderr)
        self.assertNotIn("oldpw", r.stderr + r.stdout)

    def test_user_equals_reaches_the_check(self):
        r = self.l.run("backup", "--check", "--user=bob", env={"ONIONPRESS_BACKUP_PASSWORD": "pw"})
        self.assertEqual(self.l.py_calls(), [f"{self.dd()} backup --check --user bob"])

    def test_a_bad_command_line_is_refused_before_anything_runs(self):
        for argv, msg in ((("--confirmed", "x"), "unknown option --confirmed"),
                          (("--bogus",), "unknown option --bogus"),
                          (("a", "b", "c"), "too many arguments")):
            with self.subTest(argv=argv):
                r = self.l.run("backup", *argv, env={"ONIONPRESS_BACKUP_PASSWORD": "pw"})
                self.assertEqual(r.returncode, 2)
                self.assertIn(msg, r.stderr)
                self.assertNotIn("create_backup", self.l.fn_calls())
                self.assertEqual(self.l.py_calls(), [])


class TestRestore(_Case):
    STUBS = ("setup_db_passwords", "stop_heartbeat_client", "start_heartbeat_client")

    def py(self, *rest):
        return f"{self.dd()} restore " + " ".join(rest)

    def test_the_restore_runs_between_the_heartbeat_stop_and_start(self):
        r = self.l.run("restore", "/b/x.zip", env={"ONIONPRESS_BACKUP_PASSWORD": "s3cret"})
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.l.calls()
        self.assertLess(calls.index("fn stop_heartbeat_client"), calls.index(f"py {self.py('/b/x.zip')}"))
        self.assertEqual(calls[-1], "fn start_heartbeat_client")
        self.assertIn("py-env s3cret", calls)
        self.assertIn("Done! OnionPress rebuilt from backup.", r.stdout)

    def test_a_failed_restore_starts_the_heartbeat_again_and_fails(self):
        r = self.l.run("restore", "/b/x.zip", env={"ONIONPRESS_BACKUP_PASSWORD": "pw",
                                                   "STUB_RC_PY": "1"})
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.l.calls()[-1], "fn start_heartbeat_client")
        self.assertIn("Restore failed", r.stderr)

    def test_check_changes_nothing_and_hands_back_the_clis_answer(self):
        r = self.l.run("restore", "/b/x.zip", "--check", "--json", env={"STUB_RC_PY": "5"})
        self.assertEqual(r.returncode, 5)
        self.assertEqual(self.l.py_calls(), [self.py("/b/x.zip", "--check", "--json")])
        self.assertNotIn("stop_heartbeat_client", self.l.fn_calls())

    def test_the_old_positional_password_is_refused(self):
        r = self.l.run("restore", "oldpw", "/b/x.zip")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(self.l.py_calls(), [])
        self.assertIn("refused", r.stderr)
        self.assertNotIn("oldpw", r.stderr + r.stdout)

    def test_usage_and_a_missing_password(self):
        r = self.l.run("restore")
        self.assertEqual(r.returncode, 1)
        self.assertIn("restore <backup_file.zip>", r.stdout)
        r = self.l.run("restore", "/b/x.zip")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Password is required", r.stderr)
        self.assertEqual(self.l.py_calls(), [])

    def test_confirmed_takes_the_next_word_or_an_equals(self):
        for argv, want in ((("--confirmed", "sealed-hub-data-left-out"), "--confirmed sealed-hub-data-left-out"),
                           (("--confirmed=sealed-hub-data-left-out",), "--confirmed=sealed-hub-data-left-out")):
            with self.subTest(argv=argv):
                r = self.l.run("restore", "/b/x.zip", "--json", *argv,
                               env={"ONIONPRESS_BACKUP_PASSWORD": "pw"})
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(self.l.py_calls(), [self.py("/b/x.zip", "--json", want)])
                self.assertIn("py-env pw", self.l.calls())

    def test_a_bad_command_line_is_refused_before_the_cli(self):
        for argv, msg in ((("/b/x.zip", "--bogus"), "unknown option --bogus"),
                          (("/b/x.zip", "--confirmed"), "--confirmed needs a value"),
                          (("a", "b", "/b/x.zip"), "too many arguments")):
            with self.subTest(argv=argv):
                r = self.l.run("restore", *argv, env={"ONIONPRESS_BACKUP_PASSWORD": "pw"})
                self.assertEqual(r.returncode, 2)
                self.assertIn(msg, r.stderr)
                self.assertEqual(self.l.py_calls(), [])
                self.assertNotIn("stop_heartbeat_client", self.l.fn_calls())


class TestUsage(_Case):
    def test_help_exits_0(self):
        for flag in ("help", "-h", "--help"):
            with self.subTest(flag=flag):
                r = self.l.run(flag)
                self.assertEqual(r.returncode, 0)
                self.assertIn("|backup|restore|", r.stdout)
                self.assertNotIn("alerts", r.stdout)

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
    """Every `Launcher.run*("<verb>", ...)` in linux/onionpress-tray names
    a case of linux/onionpress's main; `handle-action` takes its request
    from the request files, never a positional argument (the tray's old
    Regenerate item ran `handle-action regenerate-vanity`, which the CLI
    refuses with exit 2)."""

    def _verbs(self):
        with open(LINUX, encoding="utf-8") as f:
            text = f.read()
        main = text[text.index("\nmain() {"):]
        verbs = set()
        for m in re.finditer(r"^        ([a-z|-]+)\)$", main, re.M):
            verbs.update(m.group(1).split("|"))
        return verbs

    def _tray_calls(self):
        with open(TRAY, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "Launcher"
                    and node.args and isinstance(node.args[0], ast.Constant)):
                yield node.lineno, [a.value if isinstance(a, ast.Constant) else None for a in node.args]

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

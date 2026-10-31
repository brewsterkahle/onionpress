#!/usr/bin/env python3
"""The Mac launcher's dispatch, run: `onionpress <verb> ...` through
app/MacOS/onionpress's real `main` (and its real top level, under a
scratch HOME), with docker, the Python CLI and the heavy helpers stubbed
(tests/launcher_harness.py).

Until these tests the dispatch was only read as text: the PID lock of
`start`, `stop`'s and `quit`'s report of a failed stop, `quit`'s flags,
`restore`'s argument parser, `status`, bare `logs` and the unknown verb
had never been run. Each class below holds one verb's paths: which
helpers run in which order, the argv the Python CLI gets, where the
backup password travels (the environment, never argv) and the exit
status a caller (the menubar, scripts) sees.

Runs under macOS's /bin/bash 3.2, what the app runs it with."""

import os
import signal
import json
import re
import subprocess
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from launcher_harness import BASH, Launcher  # noqa: E402

# Always with the Cloudflare override file, so a tunnel whose token was
# just removed goes down too (tests/test_cloudflare_tunnel.py).
MAC_COMPOSE_DOWN = ("bin docker compose -f docker-compose.yml"
                    " -f docker-compose.cloudflare.yml down")

START_STUBS = ("detect_container_runtime", "install_docker", "ensure_docker_socket",
               "setup_db_passwords", "start_containers", "wait_for_services",
               "log_start_summary")


def _dead_pid():
    p = subprocess.Popen(["/usr/bin/true"])
    p.wait()
    return p.pid


@unittest.skipUnless(os.path.exists(BASH), "needs /bin/bash")
class _Case(unittest.TestCase):
    STUBS = ()

    def setUp(self):
        self.l = Launcher(self, "mac", stub=self.STUBS)

    def dd(self):
        return f'--data-dir {self.l.data_dir}'


class TestStart(_Case):
    STUBS = START_STUBS

    def test_a_start_runs_the_daemon_then_the_stack_in_order(self):
        r = self.l.run("start")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.l.py_calls()[0],
                         f"{self.dd()} daemon start --launcher {self.l.script}")
        self.assertEqual(self.l.fn_calls(), [
            "detect_container_runtime", "ensure_docker_socket", "setup_db_passwords",
            "start_containers", "wait_for_services", "log_start_summary"])
        self.assertIn("OnionPress is running!", r.stdout)
        self.assertFalse(os.path.exists(self.l.pidfile), "the EXIT trap removes the lock")

    def test_no_verb_means_start(self):
        r = self.l.run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("start_containers", self.l.fn_calls())

    def test_a_daemon_that_does_not_start_is_only_a_warning(self):
        r = self.l.run("start", env={"STUB_RC_PY": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("WARNING: the OnionPress daemon could not be started", r.stdout)
        self.assertIn("start_containers", self.l.fn_calls())

    def test_a_live_start_holds_the_lock(self):
        os.makedirs(self.l.data_dir, exist_ok=True)
        with open(self.l.pidfile, "w") as f:
            f.write(str(os.getpid()))
        r = self.l.run("start")
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"already running (PID {os.getpid()})", r.stderr)
        self.assertIn("bin osascript", "\n".join(self.l.calls()))
        self.assertNotIn("start_containers", self.l.fn_calls())
        with open(self.l.pidfile) as f:
            self.assertEqual(f.read().strip(), str(os.getpid()), "another start's lock is kept")

    def test_a_dead_starts_lock_is_taken_over(self):
        os.makedirs(self.l.data_dir, exist_ok=True)
        with open(self.l.pidfile, "w") as f:
            f.write(str(_dead_pid()))
        r = self.l.run("start")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("start_containers", self.l.fn_calls())

    @unittest.skipUnless(sys.platform == "darwin",
                         "the launcher reads the boot time with macOS's sysctl and stat -f")
    def test_a_lock_from_before_the_boot_is_stale_even_if_its_pid_lives(self):
        # The pid may have been recycled since: the file's age decides.
        os.makedirs(self.l.data_dir, exist_ok=True)
        with open(self.l.pidfile, "w") as f:
            f.write(str(os.getpid()))
        # A minute before this boot: well above any usec, which a greedy
        # parse of kern.boottime once read as the boot time.
        boot = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.boottime"],
                              capture_output=True, text=True, check=True).stdout
        boot_sec = int(re.match(r"\{ sec = (\d+),", boot).group(1))
        os.utime(self.l.pidfile, (boot_sec - 60, boot_sec - 60))
        r = self.l.run("start")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Removing stale PID file from previous boot", r.stdout)
        self.assertIn("start_containers", self.l.fn_calls())

    def test_no_runtime_installs_one_and_stops_when_it_still_is_missing(self):
        r = self.l.run("start", env={"STUB_RC_DETECT_CONTAINER_RUNTIME": "1"})
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.l.fn_calls(), [
            "detect_container_runtime", "install_docker", "detect_container_runtime"])
        self.assertIn("Container runtime still not available after install", r.stdout)

    def test_a_failed_install_is_the_exit_status(self):
        r = self.l.run("start", env={"STUB_RC_DETECT_CONTAINER_RUNTIME": "1",
                                     "STUB_RC_INSTALL_DOCKER": "7"})
        self.assertEqual(r.returncode, 7)
        self.assertNotIn("start_containers", self.l.fn_calls())


class TestStop(_Case):
    STUBS = ("setup_db_passwords", "stop_onionheaven_farm")

    def test_stop_asks_the_daemon_offline_then_stops_the_stack(self):
        r = self.l.run("stop")
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.l.calls()
        self.assertEqual(self.l.py_calls(), [f"{self.dd()} daemon offline"])
        self.assertLess(calls.index(f"py {self.dd()} daemon offline"),
                        calls.index(MAC_COMPOSE_DOWN))
        self.assertIn("Containers stopped", r.stdout)

    def test_a_daemon_that_cannot_go_offline_does_not_stop_the_stop(self):
        r = self.l.run("stop", env={"STUB_RC_PY": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(MAC_COMPOSE_DOWN, self.l.calls())

    def test_a_failed_compose_down_is_the_exit_status(self):
        r = self.l.run("stop", env={"STUB_RC_DOCKER_DOWN": "3"})
        self.assertEqual(r.returncode, 3)
        self.assertIn("ERROR: stopping the containers failed", r.stdout)
        self.assertNotIn("Containers stopped", r.stdout)


class TestQuit(_Case):
    STUBS = ("setup_db_passwords", "stop_onionheaven_farm", "reap_lima_usernet")

    def setUp(self):
        super().setUp()
        colima = os.path.join(self.l.resources, "bin", "colima")
        with open(colima, "w") as f:
            f.write('#!/bin/sh\necho "bin colima $*" >> "$OP_CALLS"\nexit "${STUB_RC_COLIMA:-0}"\n')
        os.chmod(colima, 0o755)
        self.colima = colima

    def test_a_clean_quit(self):
        r = self.l.run("quit", "--leave-menubar")
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.l.calls()
        self.assertEqual(calls[0], f"py {self.dd()} daemon stop")
        self.assertIn(MAC_COMPOSE_DOWN, calls)
        self.assertIn("bin colima stop", calls)
        self.assertIn("fn reap_lima_usernet", calls)
        self.assertLess(calls.index(MAC_COMPOSE_DOWN), calls.index("bin colima stop"))
        self.assertIn("quit: run from the menubar", r.stdout)

    def test_the_flags_are_read_in_either_order(self):
        for argv in (("--leave-menubar", "--no-offline"), ("--no-offline", "--leave-menubar")):
            with self.subTest(argv=argv):
                r = self.l.run("quit", *argv)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(self.l.py_calls()[0], f"{self.dd()} daemon stop --no-offline")
                self.assertIn("quit: run from the menubar", r.stdout)

    def test_an_unknown_flag_is_ignored_with_a_warning(self):
        r = self.l.run("quit", "--leave-menubar", "--bogus")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ignoring unknown option --bogus", r.stderr)
        self.assertIn("bin colima stop", self.l.calls())

    def test_a_failed_container_stop_fails_the_quit(self):
        r = self.l.run("quit", "--leave-menubar", env={"STUB_RC_DOCKER_DOWN": "1"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("some steps failed: containers", r.stderr)
        self.assertIn("bin colima stop", self.l.calls(), "the VM is stopped all the same")

    def test_each_failed_step_is_named(self):
        r = self.l.run("quit", "--leave-menubar",
                       env={"STUB_RC_PY": "1", "STUB_RC_COLIMA": "1"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("some steps failed: daemon colima", r.stderr)

    def test_no_colima_binary(self):
        os.unlink(self.colima)
        r = self.l.run("quit", "--leave-menubar")
        self.assertEqual(r.returncode, 1)
        self.assertIn("colima-missing", r.stderr)

    def _fake_menubar(self):
        """A process standing in for the MenubarApp, named in menubar.pid.
        A thread reaps it as soon as it ends: the launcher's `kill -0`
        counts an unreaped child of ours as alive."""
        menubar = subprocess.Popen(["/bin/sleep", "30"])
        self.addCleanup(lambda: menubar.poll() is None and menubar.kill())
        threading.Thread(target=menubar.wait, daemon=True).start()
        os.makedirs(self.l.data_dir, exist_ok=True)
        with open(os.path.join(self.l.data_dir, "menubar.pid"), "w") as f:
            f.write(f"{menubar.pid}\n")
        return menubar

    def test_the_menubar_is_sent_sigterm_and_its_lock_removed(self):
        menubar = self._fake_menubar()
        with open(self.l.pidfile, "w") as f:
            f.write(str(menubar.pid))
        start = time.monotonic()
        r = self.l.run("quit")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(menubar.returncode, -signal.SIGTERM)
        self.assertLess(time.monotonic() - start, 30)
        self.assertIn(f"SIGTERM to MenubarApp (pid {menubar.pid})", r.stdout)
        self.assertFalse(os.path.exists(self.l.pidfile))

    def test_a_lock_that_is_not_the_menubars_is_kept(self):
        self._fake_menubar()
        with open(self.l.pidfile, "w") as f:
            f.write("12345")
        r = self.l.run("quit")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(self.l.pidfile), "a start that raced in keeps its lock")


class TestRestart(_Case):
    STUBS = ("docker_socket_forward_is_foreign", "lima_vm_running",
             "recover_docker_socket_forward", "recover_lima_vm", "setup_db_passwords",
             "update_images", "stop_containers", "start_containers", "wait_for_services")

    BOUNCE = ["setup_db_passwords", "update_images", "stop_containers",
              "start_containers", "wait_for_services"]

    def test_with_docker_up_it_bounces_the_containers(self):
        r = self.l.run("restart")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.l.fn_calls(), self.BOUNCE)

    def test_a_foreign_socket_forward_on_a_running_vm_is_recovered_first(self):
        r = self.l.run("restart", env={"STUB_RC_DOCKER_INFO": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.l.fn_calls(), [
            "docker_socket_forward_is_foreign", "lima_vm_running",
            "recover_docker_socket_forward"] + self.BOUNCE)
        self.assertIn("fn recover_docker_socket_forward restart", self.l.calls())

    def test_otherwise_the_vm_is_recovered(self):
        r = self.l.run("restart", env={"STUB_RC_DOCKER_INFO": "1",
                                       "STUB_RC_DOCKER_SOCKET_FORWARD_IS_FOREIGN": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("fn recover_lima_vm restart", self.l.calls())
        self.assertNotIn("recover_docker_socket_forward", self.l.fn_calls())

    def test_a_failed_recovery_stops_the_restart(self):
        r = self.l.run("restart", env={"STUB_RC_DOCKER_INFO": "1",
                                       "STUB_RC_DOCKER_SOCKET_FORWARD_IS_FOREIGN": "1",
                                       "STUB_RC_RECOVER_LIMA_VM": "1"})
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("start_containers", self.l.fn_calls())

    def test_a_failed_stop_still_starts_the_containers(self):
        r = self.l.run("restart", env={"STUB_RC_STOP_CONTAINERS": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.l.fn_calls(), self.BOUNCE)
        self.assertIn("WARNING: restart: the stop failed", r.stdout)


class TestStatus(_Case):
    def test_compose_ps_lines_become_one_json_array(self):
        r = self.l.run("status", env={"STUB_PS": '{"Name":"onionpress-tor"}\n{"Name":"onionpress-db"}'})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([c["Name"] for c in json.loads(r.stdout)],
                         ["onionpress-tor", "onionpress-db"])

    def test_nothing_running_is_an_empty_array(self):
        r = self.l.run("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip().replace(" ", "").replace("\n", ""), "[]")


class TestDelegations(_Case):
    """Verbs that are the Python CLI's: the argv it gets, its exit status."""

    STUBS = ("setup_db_passwords",)

    def test_the_cli_gets_the_data_dir_and_every_argument(self):
        rows = [
            (("address", "--url"), "address --url"),
            (("address", "--copy"), "address --copy"),
            (("version", "--json"), "version --json"),
            (("check-reachability", "--json"), "check-reachability --json"),
            (("open", "admin"), "open admin"),
            (("onionname", "set", "mysite", "--json"), "onionname set mysite --json"),
            (("config", "set", "PREVENT_SLEEP", "never"), "config set PREVENT_SLEEP never"),
            (("config", "list", "--json"), "config list --json"),
            (("onionheaven", "status", "--json"), "onionheaven status --json"),
            (("logs", "--app", "-n", "5"), "logs --app -n 5"),
            (("logs", "--web", "-f"), "logs --web -f"),
            (("get", "http://x.onion/", "-i"), "get http://x.onion/ -i"),
            (("backup", "-o", "/b/x.zip", "--json"), "backup -o /b/x.zip --json"),
            (("backup", "--check", "--json", "--user", "bob"), "backup --check --json --user bob"),
            (("update", "--check", "--json"), "update --check --json"),
            (("update-images",), "update-images"),
            (("uninstall", "--check", "--json"), "uninstall --check --json"),
            (("reset", "--check", "--json"), "reset --check --json"),
            (("hub-recovery-key", "show"), "hub-recovery-key show"),
            (("upload-analytics",), "upload-analytics"),
            (("prepare-restore", "--check"), "prepare-restore --check"),
        ]
        for argv, want in rows:
            with self.subTest(argv=argv):
                r = self.l.run(*argv)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(self.l.py_calls(), [f"{self.dd()} {want}"])

    def test_the_clis_exit_status_is_the_launchers(self):
        for argv in (("backup",), ("config", "get", "X"), ("update", "--check"),
                     ("address",), ("reset", "--check", "--json")):
            with self.subTest(argv=argv):
                r = self.l.run(*argv, env={"STUB_RC_PY": "4"})
                self.assertEqual(r.returncode, 4)

    def test_onionpress_yes_means_reset_yes(self):
        r = self.l.run("reset", env={"ONIONPRESS_YES": "true"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.l.py_calls(), [f"{self.dd()} reset --yes"])

    def test_the_update_names_this_bundle(self):
        self.l.add_stubs('bundled_python() { _op_rec "app $ONIONPRESS_APP_PATH"; }')
        self.l.run("update", "--check")
        self.assertIn(f"app {os.path.dirname(os.path.dirname(os.path.dirname(self.l.script)))}",
                      self.l.calls())

    def test_bare_logs_follow_the_containers(self):
        r = self.l.run("logs")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.l.py_calls(), [])
        self.assertIn("bin docker compose logs -f", self.l.calls())


class TestRestore(_Case):
    STUBS = ("setup_db_passwords",)

    def py(self, *rest):
        return f"{self.dd()} restore " + " ".join(rest)

    def test_the_password_goes_in_the_environment(self):
        r = self.l.run("restore", "/b/x.zip", env={"ONIONPRESS_BACKUP_PASSWORD": "s3cret"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.l.py_calls(), [self.py("/b/x.zip")])
        self.assertIn("py-env s3cret", self.l.calls())
        self.assertNotIn("s3cret", " ".join(self.l.py_calls()))
        self.assertIn("Done! OnionPress rebuilt from backup.", r.stdout)

    def test_from_stdin_when_not_a_terminal(self):
        r = self.l.run("restore", "/b/x.zip", input="piped\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("py-env piped", self.l.calls())

    def test_the_old_positional_password_works_with_a_warning(self):
        r = self.l.run("restore", "oldpw", "/b/x.zip")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.l.py_calls(), [self.py("/b/x.zip")])
        self.assertIn("py-env oldpw", self.l.calls())
        self.assertIn("visible to other users", r.stderr)

    def test_no_path_is_the_usage(self):
        r = self.l.run("restore")
        self.assertEqual(r.returncode, 1)
        self.assertIn("restore <backup_file.zip>", r.stdout)
        self.assertEqual(self.l.py_calls(), [])

    def test_no_password_is_refused_before_the_cli(self):
        r = self.l.run("restore", "/b/x.zip")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Password is required", r.stderr)
        self.assertEqual(self.l.py_calls(), [])

    def test_check_without_a_password_asks_the_cli(self):
        # The CLI answers {"ask": "password"} (exit 5): its status is ours.
        r = self.l.run("restore", "/b/x.zip", "--check", "--json", env={"STUB_RC_PY": "5"})
        self.assertEqual(r.returncode, 5)
        self.assertEqual(self.l.py_calls(), [self.py("/b/x.zip", "--check", "--json")])
        self.assertIn("py-env <none>", self.l.calls())
        self.assertNotIn("Done!", r.stdout)

    def test_confirmed_takes_the_next_word_or_an_equals(self):
        for argv, want in (
                (("--confirmed", "sealed-hub-data-left-out"), "--confirmed sealed-hub-data-left-out"),
                (("--confirmed=sealed-hub-data-left-out",), "--confirmed=sealed-hub-data-left-out")):
            with self.subTest(argv=argv):
                r = self.l.run("restore", *argv, "/b/x.zip", "--json",
                               env={"ONIONPRESS_BACKUP_PASSWORD": "pw"})
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(self.l.py_calls(), [self.py("/b/x.zip", want, "--json")])
                self.assertIn("py-env pw", self.l.calls(), "the flag is never the password")

    def test_a_bad_command_line_is_refused_before_the_cli(self):
        for argv, msg in (
                (("/b/x.zip", "--bogus"), "unknown option --bogus"),
                (("/b/x.zip", "--confirmed"), "--confirmed needs a value"),
                (("a", "b", "/b/x.zip"), "too many arguments")):
            with self.subTest(argv=argv):
                r = self.l.run("restore", *argv, env={"ONIONPRESS_BACKUP_PASSWORD": "pw"})
                self.assertEqual(r.returncode, 2)
                self.assertIn(msg, r.stderr)
                self.assertEqual(self.l.py_calls(), [])

    def test_a_failed_restore_fails(self):
        r = self.l.run("restore", "/b/x.zip", env={"ONIONPRESS_BACKUP_PASSWORD": "pw",
                                                   "STUB_RC_PY": "3"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("Restore failed", r.stderr)
        self.assertNotIn("Done!", r.stdout)


class TestStartTor(_Case):
    STUBS = ("setup_db_passwords", "migrate_key_volume")

    def test_tor_starts_once_its_key_volume_is_in_place(self):
        r = self.l.run("start-tor")
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.l.calls()
        self.assertLess(calls.index("fn migrate_key_volume"), calls.index("bin docker compose up -d tor"))

    def test_no_key_volume_no_tor(self):
        r = self.l.run("start-tor", env={"STUB_RC_MIGRATE_KEY_VOLUME": "1"})
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("bin docker compose up -d tor", self.l.calls())

    def test_a_failed_compose_up_is_the_exit_status(self):
        r = self.l.run("start-tor", env={"STUB_RC_DOCKER_UP_TOR": "5"})
        self.assertEqual(r.returncode, 5)
        self.assertIn("Failed to start tor container (rc=5)", r.stdout)


class TestScrub(_Case):
    """`scrub`'s password: read_backup_password, never required on argv;
    the backup it makes stays in the data dir, not the shared /tmp."""

    STUBS = ("setup_db_passwords", "onionheaven_release", "stop_containers")

    def test_no_password_stops_before_the_backup(self):
        r = self.l.run("scrub", env={"ONIONPRESS_YES": "true"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("Password is required", r.stderr)
        self.assertEqual(self.l.py_calls(), [])

    def test_the_password_from_the_environment_reaches_the_backup(self):
        # The hub release refused: the scrub stops after its backup.
        r = self.l.run("scrub", env={"ONIONPRESS_YES": "true", "ONIONPRESS_BACKUP_PASSWORD": "pw",
                                     "STUB_RC_ONIONHEAVEN_RELEASE": "1"})
        self.assertEqual(r.returncode, 1)
        backup = self.l.py_calls()[0]
        self.assertTrue(backup.startswith(f"{self.dd()} backup --output {self.l.data_dir}/onionpress-scrub-"),
                        backup)
        self.assertIn("py-env pw", self.l.calls())
        self.assertIn("Cancelled. Backup kept at:", r.stdout)
        self.assertNotIn("stop_containers", self.l.fn_calls())

    def test_the_old_positional_password_warns(self):
        r = self.l.run("scrub", "pw", env={"ONIONPRESS_YES": "true",
                                           "STUB_RC_ONIONHEAVEN_RELEASE": "1"})
        self.assertIn("visible to other users", r.stderr)
        self.assertIn("py-env pw", self.l.calls())


class TestUsage(_Case):
    def test_help_lists_the_commands_and_exits_0(self):
        for flag in ("help", "-h", "--help"):
            with self.subTest(flag=flag):
                r = self.l.run(flag)
                self.assertEqual(r.returncode, 0)
                for verb in ("start", "stop", "quit", "restart", "status", "backup",
                             "restore", "config", "onionheaven", "update", "reset"):
                    self.assertIn(f"\n  {verb}", r.stdout)

    def test_help_names_no_removed_command(self):
        # The hub message channel went on 2026-10-25 (CLAUDE.md).
        self.assertNotIn("alerts", self.l.run("help").stdout)

    def test_an_unknown_verb_is_refused(self):
        r = self.l.run("frobnicate")
        self.assertEqual(r.returncode, 1)
        self.assertIn("help' for the full list", r.stderr)
        self.assertEqual(self.l.calls(), [])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""onionpress.scrub's helpers around the destructive phases: the password,
the confirmation, sudo's keep-alive, the uninstall step and the slow
post-restore checks.

What is held:

- the WordPress admin password is checked before anything is torn down:
  one passed in is verified too (a typo on the command line is as bad as
  one at the prompt), three bad tries or an empty one or a closed prompt
  give up with None, and the password never reaches the log;
- the password reaches the launcher's backup/restore in the environment
  (ONIONPRESS_BACKUP_PASSWORD), never in argv, which every local user can
  read;
- _confirm is "no" off a terminal unless ONIONPRESS_ASSUME_YES=1, and only
  y/yes is a yes at one;
- sudo is authenticated once up front (SUDO_ASKPASS with -A when set, a
  cached ticket with -nv, an interactive -v only at a terminal) and kept
  warm with -nv;
- phase_uninstall stops the tray and runs the launcher's uninstall with
  ONIONPRESS_YES, a failure an error;
- _hub_registered / _wayback_creds_present poll until true or the deadline;
- _run's capture/quiet/timeout/env reach subprocess.run.

subprocess.run, getpass, the clock and the verifier are stand-ins.
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from onionpress import scrub  # noqa: E402
from onionpress.launcher_ops import BACKUP_PASSWORD_ENV  # noqa: E402

GOOD = "correct horse"


def _verifier(good=GOOD):
    calls = []

    def verify(pw):
        calls.append(pw)
        return (pw == good), {}
    return verify, calls


class TestPasswordCheck(unittest.TestCase):

    def check(self, initial, prompts=(), good=GOOD, **kw):
        verify, calls = _verifier(good)
        lines = []
        answers = list(prompts)

        def getpass(prompt):
            self.assertEqual(prompt, scrub._INTERACTIVE_PROMPT)
            a = answers.pop(0)
            if isinstance(a, BaseException):
                raise a
            return a
        with mock.patch("onionpress.backup.verify_wp_admin_password_any", verify), \
                mock.patch.object(scrub.getpass, "getpass", side_effect=getpass) as gp:
            result = scrub._prompt_and_validate_wp_password(initial, log=lines.append, **kw)
        self.prompted = gp.call_count
        self.verified = calls
        self.lines = lines
        # the password, right or wrong, is never written to the log
        for line in lines:
            for pw in [initial, *prompts]:
                if isinstance(pw, str) and pw:
                    self.assertNotIn(pw, line)
        return result

    def test_a_good_password_passed_in_is_verified_not_prompted(self):
        self.assertEqual(self.check(GOOD), GOOD)
        self.assertEqual(self.prompted, 0)
        self.assertEqual(self.verified, [GOOD])

    def test_a_typo_passed_in_is_caught_and_reprompted(self):
        self.assertEqual(self.check("correct hrose", prompts=[GOOD]), GOOD)
        self.assertEqual(self.verified, ["correct hrose", GOOD])
        self.assertIn("  (2 more attempts remaining)", self.lines)

    def test_three_bad_tries_give_up(self):
        self.assertIsNone(self.check(None, prompts=["a1", "b2", "c3"]))
        self.assertEqual(self.verified, ["a1", "b2", "c3"])
        self.assertIn("  (1 more attempt remaining)", self.lines)
        self.assertIn("  Too many bad attempts — aborting.", self.lines)

    def test_max_attempts_is_honoured(self):
        self.assertIsNone(self.check("wr0ng-pw", max_attempts=1))
        self.assertEqual(self.prompted, 0)

    def test_an_empty_answer_gives_up_without_verifying(self):
        self.assertIsNone(self.check(None, prompts=[""]))
        self.assertEqual(self.verified, [])
        self.assertIn("  (empty password — aborting)", self.lines)

    def test_a_closed_prompt_gives_up(self):
        for exc in (EOFError(), KeyboardInterrupt()):
            with self.subTest(exc=type(exc).__name__):
                self.assertIsNone(self.check(None, prompts=[exc]))
                self.assertEqual(self.verified, [])


class TestPasswordOffArgv(unittest.TestCase):

    def test_password_env_carries_it(self):
        with mock.patch.dict(os.environ, {"KEEP": "1"}):
            env = scrub._password_env(GOOD)
        self.assertEqual(env[BACKUP_PASSWORD_ENV], GOOD)
        self.assertEqual(env["KEEP"], "1")

    def test_restore_passes_it_in_the_environment_only(self):
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0))
        with mock.patch.object(scrub.subprocess, "run", run):
            scrub.phase_restore(GOOD, "/b/x.zip", lambda m: None)
        argv = run.call_args.args[0]
        self.assertEqual(argv, [scrub.LAUNCHER_BIN, "restore", "/b/x.zip"])
        self.assertNotIn(GOOD, " ".join(argv))
        self.assertEqual(run.call_args.kwargs["env"][BACKUP_PASSWORD_ENV], GOOD)

    def test_a_failed_restore_raises(self):
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 2))
        with mock.patch.object(scrub.subprocess, "run", run):
            with self.assertRaisesRegex(RuntimeError, "restore exited 2"):
                scrub.phase_restore(GOOD, "/b/x.zip", lambda m: None)

    def test_backup_passes_it_in_the_environment_only(self):
        seen = []

        def run(args, **kw):
            seen.append((args, kw))
            return subprocess.CompletedProcess(args, 0)
        home = tempfile.mkdtemp(prefix="op-scrub-home-")
        self.addCleanup(shutil.rmtree, home, True)
        with mock.patch.object(scrub, "_get_onion_address", return_value=""), \
                mock.patch.object(scrub, "_find_repo_dir", return_value="/r"), \
                mock.patch.object(scrub, "_run", side_effect=run), \
                mock.patch.dict(os.environ, {"HOME": home}), \
                mock.patch("os.path.isfile", return_value=True), \
                mock.patch("os.path.getsize", return_value=10):
            scrub.phase_backup(GOOD, lambda m: None)
        args, kw = seen[0]
        self.assertEqual(args[:2], [scrub.LAUNCHER_BIN, "backup"])
        self.assertNotIn(GOOD, " ".join(args))
        self.assertEqual(kw["env"][BACKUP_PASSWORD_ENV], GOOD)


class TestConfirm(unittest.TestCase):

    def confirm(self, tty, reply=None, env=None):
        stdin = mock.Mock()
        stdin.isatty.return_value = tty
        inp = mock.Mock(side_effect=reply) if isinstance(reply, BaseException) \
            else mock.Mock(return_value=reply)
        with mock.patch.object(scrub.sys, "stdin", stdin), \
                mock.patch("builtins.input", inp), \
                mock.patch.dict(os.environ, env or {}, clear=False):
            if env is None:
                os.environ.pop("ONIONPRESS_ASSUME_YES", None)
            return scrub._confirm()

    def test_off_a_terminal_it_is_no(self):
        self.assertFalse(self.confirm(False))
        self.assertFalse(self.confirm(False, env={"ONIONPRESS_ASSUME_YES": "yes"}))

    def test_off_a_terminal_assume_yes_opts_in(self):
        self.assertTrue(self.confirm(False, env={"ONIONPRESS_ASSUME_YES": "1"}))

    def test_at_a_terminal_only_y_or_yes(self):
        for reply, want in (("y", True), (" YES \n", True), ("n", False),
                            ("", False), ("yep", False)):
            with self.subTest(reply=reply):
                self.assertEqual(self.confirm(True, reply), want)

    def test_a_closed_terminal_is_no(self):
        self.assertFalse(self.confirm(True, EOFError()))
        self.assertFalse(self.confirm(True, KeyboardInterrupt()))


class TestSudoKeepAlive(unittest.TestCase):

    def start(self, answers, tty=False, askpass=None):
        """answers: argv tuple -> returncode."""
        calls = []

        def run(argv, **kw):
            calls.append(tuple(argv))
            return subprocess.CompletedProcess(argv, answers.get(tuple(argv), 1))
        stdin = mock.Mock()
        stdin.isatty.return_value = tty
        env = {"SUDO_ASKPASS": askpass} if askpass else {}
        k = scrub._SudoKeepAlive()
        self.addCleanup(k.stop)
        with mock.patch.object(scrub.subprocess, "run", run), \
                mock.patch.object(scrub.sys, "stdin", stdin), \
                mock.patch.object(scrub.threading, "Thread") as thread, \
                mock.patch.dict(os.environ, env):
            if not askpass:
                os.environ.pop("SUDO_ASKPASS", None)
            ok = k.start()
        return ok, calls, thread

    def test_askpass_is_used_when_set(self):
        ok, calls, thread = self.start({("sudo", "-A", "-v"): 0}, askpass="/x/askpass")
        self.assertTrue(ok)
        self.assertEqual(calls, [("sudo", "-A", "-v")])
        thread.return_value.start.assert_called_once()

    def test_a_failed_askpass_falls_back_to_a_cached_ticket(self):
        ok, calls, _ = self.start({("sudo", "-nv"): 0}, askpass="/x/askpass")
        self.assertTrue(ok)
        self.assertEqual(calls, [("sudo", "-A", "-v"), ("sudo", "-nv")])

    def test_no_ticket_off_a_terminal_fails_without_prompting(self):
        ok, calls, thread = self.start({})
        self.assertFalse(ok)
        self.assertEqual(calls, [("sudo", "-nv")])
        thread.return_value.start.assert_not_called()

    def test_at_a_terminal_it_prompts_once(self):
        ok, calls, _ = self.start({("sudo", "-v"): 0}, tty=True)
        self.assertTrue(ok)
        self.assertEqual(calls, [("sudo", "-nv"), ("sudo", "-v")])

    def test_a_wrong_sudo_password_fails(self):
        ok, calls, thread = self.start({}, tty=True)
        self.assertFalse(ok)
        thread.return_value.start.assert_not_called()

    def test_the_loop_refreshes_without_prompting_until_stopped(self):
        k = scrub._SudoKeepAlive()
        waits = iter([False, False, True])
        k._stop.wait = lambda t: next(waits)
        run = mock.Mock(side_effect=[subprocess.CompletedProcess([], 0),
                                     subprocess.TimeoutExpired("sudo", 5)])
        with mock.patch.object(scrub.subprocess, "run", run):
            k._loop()
        self.assertEqual([c.args[0] for c in run.call_args_list],
                         [["sudo", "-nv"], ["sudo", "-nv"]])

    def test_stop_ends_the_loop(self):
        k = scrub._SudoKeepAlive()
        k.stop()
        with mock.patch.object(scrub.subprocess, "run") as run:
            k._loop()
        run.assert_not_called()


class TestPhaseUninstall(unittest.TestCase):

    def uninstall(self, rc):
        calls = []

        def run(argv, **kw):
            calls.append((list(argv), kw))
            return subprocess.CompletedProcess(argv, rc if argv[0] != "pkill" else 1)
        with mock.patch.object(scrub.subprocess, "run", run), \
                mock.patch.object(scrub.getpass, "getuser", return_value="pat"):
            scrub.phase_uninstall(lambda m: None)
        return calls

    def test_tray_stopped_then_uninstall_without_its_prompt(self):
        calls = self.uninstall(0)
        self.assertEqual(calls[0][0], ["pkill", "-u", "pat", "-f", "onionpress-tray"])
        self.assertEqual(calls[1][0], [scrub.LAUNCHER_BIN, "uninstall"])
        self.assertEqual(calls[1][1]["env"]["ONIONPRESS_YES"], "true")

    def test_a_failed_uninstall_raises(self):
        with self.assertRaisesRegex(RuntimeError, "uninstall exited 3"):
            self.uninstall(3)


class _Clock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


class TestPolls(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="op-scrub-poll-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.clock = _Clock()
        for name in ("monotonic", "sleep"):
            p = mock.patch.object(scrub.time, name, getattr(self.clock, name))
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(scrub, "DATA_DIR", self.dir)
        p.start()
        self.addCleanup(p.stop)

    def write_registration(self, text):
        with open(os.path.join(self.dir, "onionheaven-registration.json"), "w") as f:
            f.write(text)

    def test_registered_is_true(self):
        self.write_registration(json.dumps({"registered": True}))
        self.assertTrue(scrub._hub_registered())
        self.assertEqual(self.clock.t, 0)

    def test_not_registered_by_the_deadline(self):
        self.write_registration(json.dumps({"registered": "true"}))  # not a bool
        self.assertFalse(scrub._hub_registered(timeout_s=30))
        self.assertGreaterEqual(self.clock.t, 30)

    def test_a_garbled_or_missing_file_waits(self):
        self.assertFalse(scrub._hub_registered(timeout_s=20))
        self.write_registration("{not json")
        self.clock.t = 0
        self.assertFalse(scrub._hub_registered(timeout_s=20))

    def test_wayback_creds_appear(self):
        answers = iter([subprocess.CompletedProcess([], 1, stdout=""),
                        subprocess.CompletedProcess([], 0, stdout="  \n"),
                        subprocess.CompletedProcess([], 0, stdout="AKIA\n")])
        with mock.patch.object(scrub.wp_files, "wp_cli",
                               side_effect=lambda *a, **k: next(answers)) as wp:
            self.assertTrue(scrub._wayback_creds_present())
        self.assertEqual(wp.call_args.args, ("option", "get", "onionpress_archive_s3_access"))
        self.assertEqual(self.clock.t, 20)

    def test_wayback_creds_never_come(self):
        with mock.patch.object(scrub.wp_files, "wp_cli",
                               return_value=subprocess.CompletedProcess([], 1, stdout="")):
            self.assertFalse(scrub._wayback_creds_present(timeout_s=25))


class TestRunWrapper(unittest.TestCase):

    def kwargs(self, **kw):
        with mock.patch.object(scrub.subprocess, "run") as run:
            scrub._run(["x"], **kw)
        return run.call_args.kwargs

    def test_defaults_inherit_stdio(self):
        self.assertEqual(self.kwargs(), {"check": False})

    def test_capture(self):
        kw = self.kwargs(capture=True, timeout=7, env={"A": "1"}, check=True)
        self.assertEqual(kw, {"check": True, "capture_output": True, "text": True,
                              "encoding": "utf-8", "errors": "replace",
                              "timeout": 7, "env": {"A": "1"}})

    def test_quiet(self):
        kw = self.kwargs(quiet=True)
        self.assertIs(kw["stdout"], subprocess.DEVNULL)
        self.assertIs(kw["stderr"], subprocess.DEVNULL)

    def test_capture_wins_over_quiet(self):
        kw = self.kwargs(capture=True, quiet=True)
        self.assertNotIn("stdout", kw)
        self.assertTrue(kw["capture_output"])


class TestVerifyProbes(unittest.TestCase):

    def run_with(self, rc, out):
        return mock.patch.object(scrub, "_run", return_value=subprocess.CompletedProcess(
            [], rc, stdout=out))

    def test_container_running(self):
        with self.run_with(0, "onionpress-tor\nonionpress-db\n"):
            self.assertTrue(scrub._container_running("onionpress-db"))
            self.assertFalse(scrub._container_running("onionpress"))
        with self.run_with(1, "onionpress-db\n"):
            self.assertFalse(scrub._container_running("onionpress-db"))

    def test_wp_responds(self):
        for rc, out, want in ((0, "200", (True, "200")), (0, "302", (True, "302")),
                              (0, "500", (False, "500")), (0, "", (False, "000")),
                              (7, "200", (False, "000"))):
            with self.subTest(rc=rc, out=out), self.run_with(rc, out):
                self.assertEqual(scrub._wp_responds(), want)

    def test_onion_address(self):
        addr = "a" * 56 + ".onion"
        with self.run_with(0, addr + "\n"):
            self.assertEqual(scrub._get_onion_address(), addr)
        with self.run_with(1, addr):
            self.assertEqual(scrub._get_onion_address(), "")

    def test_user_service_enabled(self):
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0))
        with mock.patch.object(scrub.subprocess, "run", run), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("XDG_RUNTIME_DIR", None)
            self.assertTrue(scrub._user_service_enabled("onionpress"))
        self.assertEqual(run.call_args.args[0],
                         ["systemctl", "--user", "is-enabled", "onionpress"])
        self.assertEqual(run.call_args.kwargs["env"]["XDG_RUNTIME_DIR"],
                         f"/run/user/{os.getuid()}")


class TestRunScrubCleanup(unittest.TestCase):
    """--clean removes the backup only after a pass."""

    def run_scrub(self, clean, checks_ok, backup_path):
        state = scrub.PreScrubState(onion_address="", backup_path=backup_path, repo_dir="/r")
        lines = []
        keep = mock.Mock()
        keep.start.return_value = True
        with mock.patch.object(scrub, "_confirm", return_value=True), \
                mock.patch.object(scrub, "_SudoKeepAlive", return_value=keep), \
                mock.patch.object(scrub.signal, "signal"), \
                mock.patch.object(scrub, "_prompt_and_validate_wp_password", return_value=GOOD), \
                mock.patch.object(scrub, "phase_backup", return_value=state), \
                mock.patch.object(scrub, "phase_uninstall"), \
                mock.patch.object(scrub, "phase_install"), \
                mock.patch.object(scrub, "phase_restore") as restore, \
                mock.patch.object(scrub, "phase_verify",
                                  return_value=[scrub.Check("x", checks_ok)]):
            rc = scrub.run_scrub(GOOD, clean=clean, log_func=lines.append)
        restore.assert_called_once_with(GOOD, backup_path, mock.ANY)
        keep.stop.assert_called()
        self.assertFalse(any(GOOD in l for l in lines))
        return rc, lines

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="op-scrub-clean-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.zip = os.path.join(self.dir, "b.zip")
        with open(self.zip, "w") as f:
            f.write("z")

    def test_clean_pass_removes_the_backup(self):
        rc, _ = self.run_scrub(True, True, self.zip)
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.zip))

    def test_a_pass_without_clean_keeps_it(self):
        rc, lines = self.run_scrub(False, True, self.zip)
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(self.zip))
        self.assertTrue(any("Backup retained at" in l for l in lines))

    def test_a_failure_keeps_it_even_with_clean(self):
        rc, lines = self.run_scrub(True, False, self.zip)
        self.assertEqual(rc, 1)
        self.assertTrue(os.path.exists(self.zip))
        self.assertTrue(any("retained for recovery" in l for l in lines))

    def test_clean_with_the_backup_already_gone_is_fine(self):
        os.remove(self.zip)
        self.assertEqual(self.run_scrub(True, True, self.zip)[0], 0)

    def test_a_failed_uninstall_is_an_exit_status_not_a_traceback(self):
        state = scrub.PreScrubState(onion_address="", backup_path=self.zip, repo_dir="/r")
        lines = []
        keep = mock.Mock()
        keep.start.return_value = True
        with mock.patch.object(scrub, "_confirm", return_value=True), \
                mock.patch.object(scrub, "_SudoKeepAlive", return_value=keep), \
                mock.patch.object(scrub.signal, "signal"), \
                mock.patch.object(scrub, "_prompt_and_validate_wp_password", return_value=GOOD), \
                mock.patch.object(scrub, "phase_backup", return_value=state), \
                mock.patch.object(scrub, "phase_uninstall",
                                  side_effect=RuntimeError("uninstall exited 3")), \
                mock.patch.object(scrub, "phase_install") as install:
            rc = scrub.run_scrub(GOOD, clean=True, log_func=lines.append)
        self.assertEqual(rc, 1)
        install.assert_not_called()
        keep.stop.assert_called()
        self.assertIn("ERROR: uninstall exited 3", lines)
        self.assertTrue(os.path.exists(self.zip))
        self.assertTrue(any(self.zip in l and "retained" in l for l in lines))


if __name__ == "__main__":
    unittest.main()

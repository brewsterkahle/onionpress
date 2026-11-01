#!/usr/bin/env python3
"""The Mac's scrub cycle is onionpress.scrub's (issue #529): backup →
reset in place → restore → verify, what the Mac launcher's 150-line
`scrub)` case did, run by `onionpress scrub` on a Mac."""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

from onionpress import onionheaven, scrub  # noqa: E402

SITE = "op2" + "a" * 53 + ".onion"
OTHER = "op2" + "b" * 53 + ".onion"
LAUNCHER = "/Applications/OnionPress.app/Contents/MacOS/onionpress"


class _Case(unittest.TestCase):

    def setUp(self):
        self.data = tempfile.mkdtemp(prefix="op-scrub-mac-")
        self.addCleanup(shutil.rmtree, self.data, True)
        self.logged = []
        self.runs = []

    def log(self, msg):
        self.logged.append(msg)

    def fake_run(self, answers):
        """scrub._run / subprocess.run stand-in: records argv, answers by
        prefix with (rc, stdout)."""
        def run(args, **kw):
            line = " ".join(args)
            self.runs.append((line, kw))
            for prefix, (rc, out) in answers.items():
                if line.startswith(prefix):
                    if "backup --output" in line and rc == 0:
                        with open(args[-1], "w") as f:
                            f.write("zip")
                    return mock.Mock(returncode=rc, stdout=out)
            return mock.Mock(returncode=0, stdout="")
        return run


class TestPhaseBackupMac(_Case):

    def test_the_backup_lands_in_the_data_dir_with_the_identity_and_a_log_mark(self):
        with open(os.path.join(self.data, "onionpress.log"), "w") as f:
            f.write("one\ntwo\n")
        run = self.fake_run({f"{LAUNCHER} backup --output": (0, ""),
                             "docker exec onionpress-tor cat": (0, SITE + "\n")})
        with mock.patch.object(scrub, "_run", run), \
                mock.patch.dict(os.environ, {}, clear=False):
            state = scrub.phase_backup_mac("pw", self.log, launcher=LAUNCHER, data_dir=self.data)
        self.assertTrue(state.backup_path.startswith(os.path.join(self.data, "onionpress-scrub-")))
        self.assertTrue(state.backup_path.endswith(".zip"))
        self.assertEqual((state.onion_address, state.repo_dir), (SITE, "2"))
        line, kw = self.runs[0]
        self.assertEqual(line, f"{LAUNCHER} backup --output {state.backup_path}")
        self.assertEqual(kw["env"]["ONIONPRESS_BACKUP_PASSWORD"], "pw")
        self.assertEqual(self.logged[-1], f"  Pre-scrub address: {SITE}")

    def test_a_failed_backup_raises(self):
        run = self.fake_run({f"{LAUNCHER} backup --output": (1, "")})
        with mock.patch.object(scrub, "_run", run):
            with self.assertRaisesRegex(RuntimeError, "Backup file missing or empty"):
                scrub.phase_backup_mac("pw", self.log, launcher=LAUNCHER, data_dir=self.data)


class TestPhaseResetInPlace(_Case):

    def files(self, *names):
        for name in names:
            path = os.path.join(self.data, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write("x")

    def reset(self, remaining, confirm=True):
        self.files("secrets", "onionpress.log", ".last_status_state", "config", "config.bak",
                   "onionheaven-registration.json", "onion_address", "healthcheck-address",
                   "shared/vanity-keys/" + SITE + "/hostname", "colima/keep", "docker-config/config.json")
        run = self.fake_run({})
        docker = mock.Mock()

        def docker_run(args, **kw):
            self.runs.append(("docker " + " ".join(args), kw))
            if args[:2] == ["volume", "ls"]:
                return mock.Mock(ok=True, stdout="onionpress-onion-keys\nonionpress-tor-state\n")
            return mock.Mock(ok=True, stdout="")
        docker.run.side_effect = docker_run
        with mock.patch.object(scrub, "_run", run), \
                mock.patch.object(scrub.subprocess, "run", run), \
                mock.patch.object(onionheaven, "release_onionheaven", return_value=remaining) as rel, \
                mock.patch.object(scrub, "_confirm", return_value=confirm) as conf:
            scrub.phase_reset_in_place(self.log, launcher=LAUNCHER, data_dir=self.data, docker=docker)
        return rel, conf

    def left(self):
        out = []
        for root, _dirs, files in os.walk(self.data):
            for f in files:
                out.append(os.path.relpath(os.path.join(root, f), self.data))
        return sorted(out)

    def test_the_hubs_let_go_then_everything_of_the_identity_goes(self):
        rel, conf = self.reset([])
        rel.assert_called_once_with("scrub", data_dir=self.data, log_func=self.log)
        conf.assert_not_called()
        lines = [l for l, _ in self.runs]
        self.assertEqual(lines, [f"{LAUNCHER} stop",
                                 "docker volume ls -q --filter name=onionpress-",
                                 "docker volume rm onionpress-onion-keys",
                                 "docker volume rm onionpress-tor-state"])
        self.assertEqual(self.left(), ["colima/keep", "docker-config/config.json"])
        self.assertEqual(self.logged[-1], "  Reset complete.")

    def test_a_hub_that_keeps_the_key_is_a_question_and_the_record_stays(self):
        remaining = [{"why": "unanswered", "content_address": SITE, "onionheaven_address": OTHER}]
        rel, conf = self.reset(remaining, confirm=True)
        conf.assert_called_once()
        self.assertIn("onionheaven-registration.json", self.left())
        self.assertNotIn("secrets", self.left())

    def test_declined_nothing_is_touched(self):
        remaining = [{"why": "busy", "content_address": SITE, "onionheaven_address": OTHER}]
        with self.assertRaisesRegex(RuntimeError, "Cancelled"):
            self.reset(remaining, confirm=False)
        self.assertEqual(self.runs, [])
        self.assertIn("secrets", self.left())

    def test_a_hub_that_cannot_be_asked_again_is_no_question(self):
        remaining = [{"why": "refused", "content_address": SITE, "onionheaven_address": OTHER}]
        rel, conf = self.reset(remaining)
        conf.assert_not_called()
        self.assertIn("onionheaven-registration.json", self.left())


class TestPhaseVerifyMac(_Case):

    def verify(self, before=SITE, after=SITE, log_text="", running=True, http="200"):
        with open(os.path.join(self.data, "onionpress.log"), "w") as f:
            f.write("old line\n" + log_text)
        names = "\n".join(scrub.EXPECTED_CONTAINERS) if running else "onionpress-db"
        run = self.fake_run({"docker ps --format": (0, names + "\n"),
                             "docker exec onionpress-tor cat": (0, after + "\n"),
                             "docker exec onionpress-wordpress curl": (0, http)})
        state = scrub.PreScrubState(onion_address=before, repo_dir="1")
        with mock.patch.object(scrub, "_run", run):
            return scrub.phase_verify_mac(state, self.log, data_dir=self.data)

    def test_everything_good(self):
        checks = self.verify()
        self.assertTrue(scrub.scrub_passed(checks))
        self.assertEqual([c.name for c in checks][-3:],
                         ["Address preserved (rebuilt from backup key, no regeneration)",
                          "No vanity regeneration during restore", "WordPress responding"])
        self.assertTrue(any(l.startswith("  ✓ WordPress responding: HTTP 200") for l in self.logged))

    def test_a_changed_address_fails(self):
        checks = self.verify(after=OTHER)
        self.assertFalse(scrub.scrub_passed(checks))
        failed = [c for c in checks if not c.ok]
        self.assertEqual(failed[0].name, "Address preserved")
        self.assertIn(f"was {SITE}, now {OTHER}", failed[0].message)

    def test_a_regeneration_since_the_backup_fails(self):
        checks = self.verify(log_text="[ts] Generating custom onion address with prefix op2\n")
        self.assertFalse(scrub.scrub_passed(checks))
        self.assertFalse(next(c for c in checks if c.name == "No vanity regeneration during restore").ok)

    def test_a_regeneration_before_the_mark_does_not_count(self):
        with open(os.path.join(self.data, "onionpress.log"), "w") as f:
            f.write("Generated custom address earlier\n")
        checks = self.verify()
        self.assertTrue(scrub.scrub_passed(checks))

    def test_containers_down_or_wordpress_silent_fail(self):
        self.assertFalse(scrub.scrub_passed(self.verify(running=False)))
        self.assertFalse(scrub.scrub_passed(self.verify(http="000")))
        self.assertFalse(scrub.scrub_passed(self.verify(after="")))


class TestRunScrubMac(_Case):

    def run_mac(self, **overrides):
        defaults = {"_confirm": True, "_prompt_and_validate_wp_password": "pw",
                    "phase_backup_mac": scrub.PreScrubState(onion_address=SITE, backup_path=
                                                            os.path.join(self.data, "b.zip")),
                    "phase_reset_in_place": None, "phase_restore": None,
                    "phase_verify_mac": [scrub.Check("ok", True)]}
        defaults.update(overrides)
        stops = []
        with mock.patch.object(scrub, "_confirm", return_value=defaults["_confirm"]), \
                mock.patch.object(scrub, "_prompt_and_validate_wp_password",
                                  return_value=defaults["_prompt_and_validate_wp_password"]), \
                mock.patch.object(scrub, "phase_backup_mac", return_value=defaults["phase_backup_mac"]), \
                mock.patch.object(scrub, "phase_reset_in_place",
                                  side_effect=defaults["phase_reset_in_place"]) as reset, \
                mock.patch.object(scrub, "phase_restore", side_effect=defaults["phase_restore"]) as restore, \
                mock.patch.object(scrub, "phase_verify_mac", return_value=defaults["phase_verify_mac"]), \
                mock.patch.object(scrub.subprocess, "run",
                                  side_effect=lambda args, **kw: stops.append(args)):
            rc = scrub.run_scrub("pw", clean=overrides.get("clean", False), log_func=self.log,
                                 os_name="macos", launcher=LAUNCHER, data_dir=self.data,
                                 docker=mock.Mock())
        return rc, reset, restore, stops

    def test_the_happy_path_stops_the_containers_at_the_end(self):
        with open(os.path.join(self.data, "b.zip"), "w") as f:
            f.write("zip")
        rc, reset, restore, stops = self.run_mac()
        self.assertEqual(rc, 0)
        reset.assert_called_once_with(self.log, launcher=LAUNCHER, data_dir=self.data, docker=mock.ANY)
        restore.assert_called_once_with("pw", os.path.join(self.data, "b.zip"), self.log,
                                        launcher=LAUNCHER, step="3/4")
        self.assertEqual(stops, [[LAUNCHER, "stop"]])
        self.assertIn("Scrub PASSED — backup/reset/restore all working.", self.logged)
        self.assertIn("This will: backup → reset → restore → verify", self.logged)
        self.assertEqual(self.logged[-1], "Done. Launch OnionPress from /Applications to start fresh.")
        self.assertTrue(os.path.exists(os.path.join(self.data, "b.zip")), "kept without --clean")

    def test_clean_removes_the_backup(self):
        with open(os.path.join(self.data, "b.zip"), "w") as f:
            f.write("zip")
        rc, *_ = self.run_mac(clean=True)
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(os.path.join(self.data, "b.zip")))

    def test_a_declined_reset_keeps_the_backup_and_restores_nothing(self):
        rc, reset, restore, stops = self.run_mac(phase_reset_in_place=RuntimeError("Cancelled"))
        self.assertEqual(rc, 1)
        restore.assert_not_called()
        self.assertEqual(stops, [])
        self.assertIn(f"Cancelled. Backup kept at: {os.path.join(self.data, 'b.zip')}", self.logged)

    def test_a_failed_verify_is_1_and_still_stops(self):
        rc, _r, _s, stops = self.run_mac(phase_verify_mac=[scrub.Check("bad", False)])
        self.assertEqual(rc, 1)
        self.assertIn("Scrub FAILED — see errors above.", self.logged)
        self.assertEqual(stops, [[LAUNCHER, "stop"]])

    def test_a_failed_restore_is_1(self):
        rc, *_ = self.run_mac(phase_restore=RuntimeError("restore exited 1 — aborting scrub."))
        self.assertEqual(rc, 1)
        self.assertIn("ERROR: restore exited 1 — aborting scrub.", self.logged)

    def test_user_declines(self):
        rc, reset, *_ = self.run_mac(_confirm=False)
        self.assertEqual(rc, 0)
        reset.assert_not_called()

    def test_a_bad_password_aborts_before_anything(self):
        rc, reset, *_ = self.run_mac(_prompt_and_validate_wp_password=None)
        self.assertEqual(rc, 1)
        reset.assert_not_called()

    def test_no_sudo_on_a_mac(self):
        with mock.patch.object(scrub, "_SudoKeepAlive") as keep:
            self.run_mac()
        keep.assert_not_called()


class TestConfirmYes(unittest.TestCase):
    def test_the_launchers_dash_y_answers_yes(self):
        with mock.patch.dict(os.environ, {"ONIONPRESS_YES": "true"}), \
                mock.patch("builtins.input", side_effect=AssertionError("asked")):
            self.assertTrue(scrub._confirm())
        with mock.patch.dict(os.environ, {"ONIONPRESS_YES": "false"}), \
                mock.patch.object(scrub.sys.stdin, "isatty", return_value=False):
            self.assertFalse(scrub._confirm())


class TestLinuxDefaults(unittest.TestCase):
    def test_run_scrub_on_linux_keeps_its_cycle(self):
        with mock.patch.object(scrub, "_confirm", return_value=False) as conf:
            self.assertEqual(scrub.run_scrub(os_name="linux", log_func=lambda m: None), 0)
        conf.assert_called_once()


class TestLauncher(unittest.TestCase):
    def test_both_scrub_cases_are_the_clis(self):
        root = os.path.dirname(HERE)
        for rel, runner in (("app/MacOS/onionpress", "bundled_python -m onionpress.cli"),
                            ("linux/onionpress", "op_py")):
            with open(os.path.join(root, rel), encoding="utf-8") as f:
                text = f.read()
            i = text.index("\n        scrub)\n")
            case = text[i:text.index("\n            ;;\n", i)]
            with self.subTest(rel):
                self.assertIn(f'{runner} --data-dir "$DATA_DIR" scrub "${{@:2}}"', case)
                self.assertNotIn("docker volume", case)
                self.assertNotIn("read_backup_password", case)


if __name__ == "__main__":
    unittest.main()

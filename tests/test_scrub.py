#!/usr/bin/env python3
"""Tests for src/onionpress/scrub.py — the ported full-lifecycle test.

These mock out every subprocess + filesystem touch so they run without
docker, sudo, or a real OnionPress install. Behavioral tests (the actual
backup→uninstall→install→restore round-trip works) live in the
adversarial CI harness (#252); these tests cover the orchestration glue,
verify checks, and the failure-path branches.
"""

import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from onionpress import scrub  # noqa: E402
from fake_docker import FakeDocker  # noqa: E402


def _ok(stdout: str = "", code: int = 0):
    return subprocess.CompletedProcess(args=[], returncode=code,
                                       stdout=stdout, stderr="")


class TestScrubPassed(unittest.TestCase):
    """`scrub_passed` returns True iff every fail-severity check passed;
    warn-severity failures don't block the overall result.
    """

    def test_all_pass(self):
        checks = [
            scrub.Check("a", True),
            scrub.Check("b", True),
        ]
        self.assertTrue(scrub.scrub_passed(checks))

    def test_one_fail_blocks(self):
        checks = [
            scrub.Check("a", True),
            scrub.Check("b", False),
        ]
        self.assertFalse(scrub.scrub_passed(checks))

    def test_warn_failures_dont_block(self):
        # Wayback creds and hub re-registration are warn-severity —
        # the install IS functional without them, they just need more
        # time to converge.
        checks = [
            scrub.Check("a", True),
            scrub.Check("wayback", False, severity="warn"),
            scrub.Check("hub", False, severity="warn"),
        ]
        self.assertTrue(
            scrub.scrub_passed(checks),
            "warn-severity failures must not block scrub PASS",
        )


class TestPhaseVerify(unittest.TestCase):
    """phase_verify should produce one Check per assertion and never
    raise — even when every probe fails, we get a list of negative
    Checks back so the orchestrator can summarize cleanly.
    """

    def _state(self, addr="op2happy.onion", port=18080):
        return scrub.PreScrubState(
            onion_address=addr, wp_port=port,
            backup_path="/tmp/x.zip", repo_dir="/home/x/onionpress")

    def test_all_checks_run_even_when_everything_fails(self):
        with mock.patch.object(scrub, "_container_running", return_value=False), \
             mock.patch.object(scrub, "_get_onion_address", return_value=""), \
             mock.patch.object(scrub, "_wp_responds", return_value=(False, "000")), \
             mock.patch.object(scrub, "_user_service_enabled", return_value=False), \
             mock.patch.object(scrub, "_hub_registered", return_value=False), \
             mock.patch.object(scrub, "_wayback_creds_present", return_value=False), \
             mock.patch.dict(os.environ, {"ONIONPRESS_WP_PORT": "28080"}):
            checks = scrub.phase_verify(self._state(), log=lambda _: None, docker=FakeDocker())
        # 4 containers + 1 address + 1 wp + 1 service + 1 port + 1 hub + 1 wayback = 10
        self.assertEqual(len(checks), 10)
        # All fail-severity checks are False
        fails = [c for c in checks if c.severity == "fail" and not c.ok]
        self.assertGreaterEqual(len(fails), 7,
                                "every fail-severity check should have failed")
        self.assertFalse(scrub.scrub_passed(checks))

    def test_port_drift_caught(self):
        # WP_PORT env says we came up on 28080, but pre-scrub state had
        # 18080. The verify must catch this.
        with mock.patch.object(scrub, "_container_running", return_value=True), \
             mock.patch.object(scrub, "_get_onion_address",
                               return_value="op2happy.onion"), \
             mock.patch.object(scrub, "_wp_responds", return_value=(True, "200")), \
             mock.patch.object(scrub, "_user_service_enabled", return_value=True), \
             mock.patch.object(scrub, "_hub_registered", return_value=True), \
             mock.patch.object(scrub, "_wayback_creds_present", return_value=True), \
             mock.patch.dict(os.environ, {"ONIONPRESS_WP_PORT": "28080"}):
            checks = scrub.phase_verify(self._state(port=18080),
                                        log=lambda _: None, docker=FakeDocker())
        port_check = next(c for c in checks if "WP port" in c.name)
        self.assertFalse(port_check.ok, "port drift must be caught")
        self.assertIn("18080", port_check.message)
        self.assertIn("28080", port_check.message)

    def test_address_change_caught(self):
        with mock.patch.object(scrub, "_container_running", return_value=True), \
             mock.patch.object(scrub, "_get_onion_address",
                               return_value="DIFFERENT.onion"), \
             mock.patch.object(scrub, "_wp_responds", return_value=(True, "200")), \
             mock.patch.object(scrub, "_user_service_enabled", return_value=True), \
             mock.patch.object(scrub, "_hub_registered", return_value=True), \
             mock.patch.object(scrub, "_wayback_creds_present", return_value=True), \
             mock.patch.dict(os.environ, {"ONIONPRESS_WP_PORT": "18080"}):
            checks = scrub.phase_verify(self._state(addr="ORIGINAL.onion"),
                                        log=lambda _: None, docker=FakeDocker())
        addr_check = next(c for c in checks if "Onion address" in c.name)
        self.assertFalse(addr_check.ok, "vanity-key drift must be caught")

    def test_hub_registration_is_warn_not_fail(self):
        # Hub re-registration can lag — it converges on next heartbeat.
        # Must NOT fail the scrub if everything else is healthy.
        with mock.patch.object(scrub, "_container_running", return_value=True), \
             mock.patch.object(scrub, "_get_onion_address",
                               return_value="op2happy.onion"), \
             mock.patch.object(scrub, "_wp_responds", return_value=(True, "200")), \
             mock.patch.object(scrub, "_user_service_enabled", return_value=True), \
             mock.patch.object(scrub, "_hub_registered", return_value=False), \
             mock.patch.object(scrub, "_wayback_creds_present", return_value=True), \
             mock.patch.dict(os.environ, {"ONIONPRESS_WP_PORT": "18080"}):
            checks = scrub.phase_verify(self._state(), log=lambda _: None, docker=FakeDocker())
        hub = next(c for c in checks if "OnionHeaven" in c.name)
        self.assertEqual(hub.severity, "warn")
        self.assertTrue(scrub.scrub_passed(checks),
                        "hub warn must not block scrub PASS")


class TestPhaseBackup(unittest.TestCase):

    def test_captures_pre_scrub_state(self):
        with mock.patch.object(scrub, "_get_onion_address",
                               return_value="op2happy.onion"), \
             mock.patch.object(scrub, "_find_repo_dir",
                               return_value="/home/x/onionpress"), \
             mock.patch.object(scrub, "_run", return_value=_ok()), \
             mock.patch("os.path.isfile", return_value=True), \
             mock.patch("os.path.getsize", return_value=1_000_000), \
             mock.patch.dict(os.environ, {"ONIONPRESS_WP_PORT": "18080"}):
            state = scrub.phase_backup("pw", log=lambda _: None, docker=FakeDocker())
        self.assertEqual(state.onion_address, "op2happy.onion")
        self.assertEqual(state.wp_port, 18080)
        self.assertEqual(state.repo_dir, "/home/x/onionpress")
        expected_dir = os.path.expanduser("~/OnionPress/backups")
        self.assertTrue(state.backup_path.startswith(
            os.path.join(expected_dir, "onionpress-scrub-")))
        self.assertTrue(state.backup_path.endswith(".zip"))

    def test_aborts_when_no_repo_dir(self):
        with mock.patch.object(scrub, "_get_onion_address", return_value=""), \
             mock.patch.object(scrub, "_find_repo_dir", return_value=""):
            with self.assertRaises(RuntimeError) as ctx:
                scrub.phase_backup("pw", log=lambda _: None, docker=FakeDocker())
        self.assertIn("Cannot find OnionPress git repo", str(ctx.exception))

    def test_aborts_when_backup_zip_is_empty(self):
        with mock.patch.object(scrub, "_get_onion_address", return_value=""), \
             mock.patch.object(scrub, "_find_repo_dir",
                               return_value="/home/x/onionpress"), \
             mock.patch.object(scrub, "_run", return_value=_ok()), \
             mock.patch("os.path.isfile", return_value=True), \
             mock.patch("os.path.getsize", return_value=0):
            with self.assertRaises(RuntimeError) as ctx:
                scrub.phase_backup("pw", log=lambda _: None, docker=FakeDocker())
        self.assertIn("missing or empty", str(ctx.exception))


class TestRunScrubBranches(unittest.TestCase):
    """The top-level orchestrator. Mock every phase to verify the
    error-path branches return the right exit codes.
    """

    def _patch_all(self, **overrides):
        """Helper: stub out every external dep with sensible defaults
        that simulate a healthy scrub, then let the test override
        individual ones to simulate failures.
        """
        defaults = {
            "_confirm": True,
            "_SudoKeepAlive": mock.MagicMock(return_value=mock.MagicMock(
                start=mock.MagicMock(return_value=True),
                stop=mock.MagicMock(),
            )),
            "_prompt_and_validate_wp_password": "pw",
            "phase_backup": scrub.PreScrubState(
                onion_address="op2happy.onion",
                wp_port=18080,
                backup_path="/tmp/x.zip",
                repo_dir="/home/x/onionpress",
            ),
            "phase_uninstall": None,
            "phase_install": None,
            "phase_restore": None,
            "phase_verify": [scrub.Check("ok", True)],
        }
        defaults.update(overrides)
        patches = [
            mock.patch.object(scrub, "_confirm", return_value=defaults["_confirm"]),
            mock.patch.object(scrub, "_SudoKeepAlive",
                              defaults["_SudoKeepAlive"]),
            mock.patch.object(scrub, "_prompt_and_validate_wp_password",
                              return_value=defaults["_prompt_and_validate_wp_password"]),
            mock.patch.object(scrub, "phase_backup",
                              return_value=defaults["phase_backup"]),
            mock.patch.object(scrub, "phase_uninstall",
                              return_value=defaults["phase_uninstall"]),
            mock.patch.object(scrub, "phase_install",
                              return_value=defaults["phase_install"]),
            mock.patch.object(scrub, "phase_restore",
                              return_value=defaults["phase_restore"]),
            mock.patch.object(scrub, "phase_verify",
                              return_value=defaults["phase_verify"]),
        ]
        return patches

    def test_bad_password_aborts_before_any_destructive_action(self):
        """If the WP admin password validation fails 3 times in a row,
        scrub must abort with rc=1 WITHOUT calling phase_backup or any
        subsequent destructive phase. This is the fix for today's bug
        where bad passwords left the install half-uninstalled.
        """
        with mock.patch.object(scrub, "_confirm", return_value=True), \
             mock.patch.object(scrub, "_SudoKeepAlive",
                               return_value=mock.MagicMock(
                                   start=mock.MagicMock(return_value=True),
                                   stop=mock.MagicMock())), \
             mock.patch.object(scrub, "_prompt_and_validate_wp_password",
                               return_value=None) as pv, \
             mock.patch.object(scrub, "phase_backup") as backup, \
             mock.patch.object(scrub, "phase_uninstall") as uninstall, \
             mock.patch.object(scrub, "phase_install") as install:
            rc = scrub.run_scrub(password="wrong", docker=FakeDocker(), log_func=lambda _: None)
        self.assertEqual(rc, 1)
        pv.assert_called_once_with("wrong", docker=mock.ANY, log=mock.ANY)
        backup.assert_not_called()
        uninstall.assert_not_called()
        install.assert_not_called()

    def test_user_cancels(self):
        with mock.patch.object(scrub, "_confirm", return_value=False):
            rc = scrub.run_scrub(password="pw", docker=FakeDocker(), log_func=lambda _: None)
        self.assertEqual(rc, 0)

    def test_sudo_auth_fails(self):
        ka = mock.MagicMock()
        ka.start.return_value = False
        with mock.patch.object(scrub, "_confirm", return_value=True), \
             mock.patch.object(scrub, "_SudoKeepAlive",
                               return_value=ka):
            rc = scrub.run_scrub(password="pw", docker=FakeDocker(), log_func=lambda _: None)
        self.assertEqual(rc, 1)

    def test_happy_path_returns_zero(self):
        for p in self._patch_all():
            p.start()
        try:
            rc = scrub.run_scrub(password="pw", docker=FakeDocker(), log_func=lambda _: None)
        finally:
            mock.patch.stopall()
        self.assertEqual(rc, 0)

    def test_install_failure_returns_one(self):
        # When phase_install raises, we abort with exit 1 and the backup
        # is retained for recovery.
        patches = self._patch_all()
        for p in patches:
            p.start()
        try:
            with mock.patch.object(scrub, "phase_install",
                                   side_effect=RuntimeError("install.sh failed")):
                rc = scrub.run_scrub(password="pw", docker=FakeDocker(), log_func=lambda _: None)
        finally:
            mock.patch.stopall()
        self.assertEqual(rc, 1)

    def test_verify_failure_returns_one(self):
        # Every phase ran fine, but verify caught a regression.
        patches = self._patch_all(
            phase_verify=[scrub.Check("port match", False)],
        )
        for p in patches:
            p.start()
        try:
            rc = scrub.run_scrub(password="pw", docker=FakeDocker(), log_func=lambda _: None)
        finally:
            mock.patch.stopall()
        self.assertEqual(rc, 1)

    def test_clean_removes_backup_on_success(self):
        patches = self._patch_all()
        for p in patches:
            p.start()
        unlinked: list[str] = []
        try:
            with mock.patch("os.unlink", side_effect=lambda p: unlinked.append(p)):
                rc = scrub.run_scrub(
                    password="pw", docker=FakeDocker(), clean=True, log_func=lambda _: None)
        finally:
            mock.patch.stopall()
        self.assertEqual(rc, 0)
        self.assertIn("/tmp/x.zip", unlinked,
                      "clean=True must unlink the backup on PASS")


if __name__ == "__main__":
    unittest.main()

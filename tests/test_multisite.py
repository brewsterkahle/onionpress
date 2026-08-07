#!/usr/bin/env python3
"""Tests for src/onionpress/multisite.py — the WordPress post-install
provisioning module shared between Mac and Linux.

These mock out subprocess so they run without docker. The behavioral
tests (containers actually come up, theme actually activates) live in
the adversarial-CI harness (#252) — these tests only verify the
orchestration glue: right wp-cli calls in the right order, right
docker cp invocations, right error handling.
"""

import subprocess
import unittest
from unittest import mock

from onionpress import multisite


def _ok():
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def _err(stderr="failed", code=1):
    return subprocess.CompletedProcess(args=[], returncode=code, stdout="", stderr=stderr)


class TestProvisionPostInstallOrdering(unittest.TestCase):
    """The critical invariant: ensure_multisite MUST run BEFORE
    install_multisite_domain_map, because the latter drops sunrise.php
    + sets SUNRISE=true, and sunrise.php queries wp_site on every WP
    load. If wp_site doesn't exist yet (multisite-convert hasn't run),
    every subsequent wp-cli call breaks and the theme install silently
    skips. Linux had this backwards before — see commit history.
    """

    def test_provision_runs_steps_in_order(self):
        calls = []

        def fake(name):
            def _inner(**kwargs):
                calls.append(name)
                return True
            return _inner

        with mock.patch.object(multisite, "ensure_multisite", fake("ensure_multisite")), \
             mock.patch.object(multisite, "install_multisite_domain_map", fake("install_multisite_domain_map")), \
             mock.patch.object(multisite, "install_onionpress_theme", fake("install_onionpress_theme")), \
             mock.patch.object(multisite, "fix_onionpress_permissions", fake("fix_onionpress_permissions")), \
             mock.patch.object(multisite, "fix_wordpress_uploads_permissions", fake("fix_wordpress_uploads_permissions")), \
             mock.patch.object(multisite, "write_shared_onion_address", fake("write_shared_onion_address")):
            multisite.provision_post_install(
                themes_dir="/x/themes", plugins_dir="/x/plugins")

        # ensure_multisite comes BEFORE install_multisite_domain_map —
        # the entire reason this module exists.
        self.assertLess(
            calls.index("ensure_multisite"),
            calls.index("install_multisite_domain_map"),
            "ensure_multisite must run BEFORE install_multisite_domain_map. "
            "sunrise.php (dropped by the latter) queries wp_site on every "
            "WP load; if wp_site doesn't exist yet, every subsequent wp-cli "
            "call errors out and the theme install silently skips.",
        )
        # install_multisite_domain_map BEFORE install_onionpress_theme —
        # the theme uses sunrise.php's domain rewrites.
        self.assertLess(
            calls.index("install_multisite_domain_map"),
            calls.index("install_onionpress_theme"),
        )


class TestEnsureMultisite(unittest.TestCase):

    def test_skips_when_wp_not_installed(self):
        logs = []
        with mock.patch.object(multisite, "wp_is_installed", return_value=False):
            multisite.ensure_multisite(log_func=logs.append)
        self.assertTrue(any("not installed" in s for s in logs))

    def test_skips_when_already_multisite(self):
        logs = []
        with mock.patch.object(multisite, "wp_is_installed", return_value=True), \
             mock.patch.object(multisite, "_wp", return_value=_ok()) as wp:
            multisite.ensure_multisite(log_func=logs.append)
        # The is-installed --network check returned 0, so we skipped convert.
        # Only the `core is-installed --network` call should have happened.
        self.assertTrue(any("already active" in s for s in logs))
        # No `multisite-convert` call.
        called = [c.args[0] for c in wp.call_args_list]
        for argv in called:
            self.assertNotIn("multisite-convert", argv)

    def test_runs_convert_when_not_multisite(self):
        calls = []

        def fake_wp(*args, **kwargs):
            calls.append(args)
            # is-installed --network returns 1 (not multisite); everything else 0.
            if "is-installed" in args and "--network" in args:
                return _err()
            return _ok()

        with mock.patch.object(multisite, "wp_is_installed", return_value=True), \
             mock.patch.object(multisite, "_wp", side_effect=fake_wp):
            multisite.ensure_multisite(log_func=lambda _msg: None)

        # multisite-convert must have been called.
        self.assertTrue(any("multisite-convert" in a for a in calls),
                        f"expected multisite-convert call, got: {calls}")
        # Each of the 7 constants must have been set.
        set_constants = [a for a in calls if "set" in a and "constant" in str(a)]
        self.assertEqual(
            len(set_constants), len(multisite.MULTISITE_CONSTANTS),
            "each of MULTISITE_CONSTANTS must be wp config set",
        )


class TestInstallOnionpressTheme(unittest.TestCase):

    def test_skips_when_wp_not_installed(self):
        with mock.patch.object(multisite, "wp_is_installed", return_value=False), \
             mock.patch.object(multisite, "_docker_cp") as cp:
            multisite.install_onionpress_theme(
                themes_dir="/x", plugins_dir="/y",
                log_func=lambda _: None)
        cp.assert_not_called()

    def test_pre_deletes_theme_dir_before_cp(self):
        # docker cp into existing dir copies INTO it — must rm first.
        # This is THE bug that bit before: the Linux version was missing
        # the rm and ended up with /themes/onionpress/onionpress/.
        exec_calls = []

        def fake_exec(cmd, **kwargs):
            exec_calls.append(cmd)
            return _ok()

        cp_calls = []

        def fake_cp(src, dest, **kwargs):
            cp_calls.append((src, dest))
            return _ok()

        with mock.patch.object(multisite, "wp_is_installed", return_value=True), \
             mock.patch.object(multisite, "_exec_sh", side_effect=fake_exec), \
             mock.patch.object(multisite, "_docker_cp", side_effect=fake_cp), \
             mock.patch.object(multisite, "_wp", return_value=_ok()), \
             mock.patch("os.path.isdir", return_value=True):
            multisite.install_onionpress_theme(
                themes_dir="/x/themes", plugins_dir="/x/plugins",
                log_func=lambda _: None)

        # Find the rm of the theme dir, and the cp of the theme dir.
        # Order matters: rm must come before cp.
        rm_idx = next(
            (i for i, c in enumerate(exec_calls)
             if "rm -rf" in c and "themes/onionpress" in c),
            -1,
        )
        cp_idx = next(
            (i for i, (_src, dest) in enumerate(cp_calls)
             if "themes/onionpress" in dest),
            -1,
        )
        self.assertGreaterEqual(rm_idx, 0, "must rm theme dir before cp")
        self.assertGreaterEqual(cp_idx, 0, "must docker cp theme dir")

    def test_does_not_override_user_chosen_non_default_theme(self):
        # If current theme is some custom thing, the activate should be skipped.
        wp_calls = []

        def fake_wp(*args, **kwargs):
            wp_calls.append(args)
            if "list" in args and "--status=active" in args:
                # Return a non-default theme name.
                return subprocess.CompletedProcess(
                    args=[], returncode=0,
                    stdout="custom-theme-by-user\n", stderr="")
            return _ok()

        with mock.patch.object(multisite, "wp_is_installed", return_value=True), \
             mock.patch.object(multisite, "_exec_sh", return_value=_ok()), \
             mock.patch.object(multisite, "_docker_cp", return_value=_ok()), \
             mock.patch.object(multisite, "_wp", side_effect=fake_wp), \
             mock.patch("os.path.isdir", return_value=True):
            multisite.install_onionpress_theme(
                themes_dir="/x", plugins_dir="/y",
                log_func=lambda _: None)

        activate_calls = [a for a in wp_calls
                          if "activate" in a and "onionpress" in a]
        self.assertEqual(
            activate_calls, [],
            "must NOT activate onionpress theme when user has a custom theme — "
            f"got activate calls: {activate_calls}",
        )


class TestMuPluginsList(unittest.TestCase):
    """The list of bundled mu-plugins lives in MU_PLUGINS at module scope
    so Mac and Linux see the same set. Catch the easy "added a plugin
    to one platform's list, forgot the other" regression by asserting
    a few critical names are present.
    """

    def test_critical_mu_plugins_listed(self):
        critical = {
            "onionpress-domain-map.php",
            "onionpress-auto-login.php",
            "onionpress-wayback-archive.php",
            "onionpress-onboarding.php",
            "onionpress-avatar.php",
            "onionpress-research-vault.php",
        }
        missing = critical - set(multisite.MU_PLUGINS)
        self.assertFalse(
            missing,
            f"Critical mu-plugins missing from MU_PLUGINS: {missing}",
        )


class TestConfigureIaPlugin(unittest.TestCase):
    def test_skips_when_wp_not_installed(self):
        with mock.patch.object(multisite, "wp_is_installed", return_value=False), \
             mock.patch.object(multisite, "_wp") as wp:
            multisite.configure_ia_plugin(log_func=lambda _: None)
        wp.assert_not_called()

    def test_skips_when_already_configured(self):
        # wizard_completed = "1" → short-circuit, no option-update calls.
        def fake_wp(*args, **kwargs):
            if "get" in args and "iawmlf_setup_wizard_completed" in args:
                return subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="1\n", stderr="")
            return _ok()

        update_calls = []
        def tracker(*args, **kwargs):
            r = fake_wp(*args, **kwargs)
            if "update" in args:
                update_calls.append(args)
            return r

        with mock.patch.object(multisite, "wp_is_installed", return_value=True), \
             mock.patch.object(multisite, "_exec_sh", return_value=_ok()), \
             mock.patch.object(multisite, "_wp", side_effect=tracker):
            multisite.configure_ia_plugin(log_func=lambda _: None)
        self.assertEqual(
            update_calls, [],
            "must NOT re-write IA plugin options when wizard already done",
        )


class TestDeactivateWpStatistics(unittest.TestCase):
    def test_noop_when_plugin_absent(self):
        # test -f returns 1 → plugin not present → no wp calls.
        wp_calls = []
        def tracker(*args, **kwargs):
            wp_calls.append(args)
            return _ok()
        with mock.patch.object(multisite, "wp_is_installed", return_value=True), \
             mock.patch.object(multisite, "_exec_sh", return_value=_err()), \
             mock.patch.object(multisite, "_wp", side_effect=tracker):
            multisite.deactivate_wp_statistics(log_func=lambda _: None)
        self.assertEqual(
            wp_calls, [],
            "must not deactivate/delete a plugin that isn't installed",
        )

    def test_removes_when_plugin_present(self):
        wp_calls = []
        def tracker(*args, **kwargs):
            wp_calls.append(args)
            return _ok()
        with mock.patch.object(multisite, "wp_is_installed", return_value=True), \
             mock.patch.object(multisite, "_exec_sh", return_value=_ok()), \
             mock.patch.object(multisite, "_wp", side_effect=tracker):
            multisite.deactivate_wp_statistics(log_func=lambda _: None)
        # Must have deactivated AND deleted the plugin.
        deactivates = [a for a in wp_calls
                       if "deactivate" in a and "wp-statistics" in a]
        deletes = [a for a in wp_calls
                   if "delete" in a and "wp-statistics" in a]
        self.assertTrue(deactivates, "must call `wp plugin deactivate wp-statistics`")
        self.assertTrue(deletes, "must call `wp plugin delete wp-statistics`")


class TestEnsureArchiveS3Keys(unittest.TestCase):
    def test_skips_when_keys_already_set(self):
        def fake_wp(*args, **kwargs):
            if "get" in args and "onionpress_archive_s3_access" in args:
                return subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="ALREADY_SET\n", stderr="")
            return _ok()
        with mock.patch.object(multisite, "wp_is_installed", return_value=True), \
             mock.patch.object(multisite, "_wp", side_effect=fake_wp), \
             mock.patch("subprocess.run") as srun:
            multisite.ensure_archive_s3_keys(log_func=lambda _: None)
        # Must NOT have hit archive.org if keys were already set.
        srun.assert_not_called()

    def test_writes_keys_on_successful_login(self):
        wp_updates = []
        def fake_wp(*args, **kwargs):
            if "update" in args:
                wp_updates.append(args)
            return _ok()  # get returns empty stdout → keys not set
        login_response = (
            '{"success": true, "values": {"s3": '
            '{"access": "AKEY", "secret": "SKEY"}}}'
        )
        with mock.patch.object(multisite, "wp_is_installed", return_value=True), \
             mock.patch.object(multisite, "_wp", side_effect=fake_wp), \
             mock.patch("subprocess.run", return_value=subprocess.CompletedProcess(
                 args=[], returncode=0, stdout=login_response, stderr="")):
            result = multisite.ensure_archive_s3_keys(log_func=lambda _: None)
        self.assertTrue(result)
        access = [a for a in wp_updates
                  if "onionpress_archive_s3_access" in a and "AKEY" in a]
        secret = [a for a in wp_updates
                  if "onionpress_archive_s3_secret" in a and "SKEY" in a]
        self.assertTrue(access, "must update onionpress_archive_s3_access")
        self.assertTrue(secret, "must update onionpress_archive_s3_secret")

    def test_handles_tor_login_failure_gracefully(self):
        logs = []
        with mock.patch.object(multisite, "wp_is_installed", return_value=True), \
             mock.patch.object(multisite, "_wp", return_value=_ok()), \
             mock.patch("subprocess.run", return_value=subprocess.CompletedProcess(
                 args=[], returncode=0, stdout="", stderr="")):
            result = multisite.ensure_archive_s3_keys(log_func=logs.append)
        self.assertFalse(result)
        self.assertTrue(any("Could not reach archive.org" in s for s in logs))


if __name__ == "__main__":
    unittest.main()

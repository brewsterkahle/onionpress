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


class TestInstallStaticSiteConf(unittest.TestCase):
    """The Apache static-first conf is injected at runtime (docker cp +
    a2enconf), NOT baked into the pulled WordPress image. See
    install_static_site_conf's docstring for why.
    """

    def test_copies_conf_and_enables_it(self):
        cp_calls = []
        exec_calls = []

        def fake_cp(src, dest, **kwargs):
            cp_calls.append((src, dest))
            return _ok()

        def fake_exec(command, **kwargs):
            exec_calls.append(command)
            return _ok()

        with mock.patch.object(multisite, "_docker_cp", side_effect=fake_cp), \
             mock.patch.object(multisite, "_exec_sh", side_effect=fake_exec), \
             mock.patch("os.path.isfile", return_value=True):
            ok = multisite.install_static_site_conf(
                conf_dir="/x/docker/wordpress", log_func=lambda _: None)

        self.assertTrue(ok)
        # Copied the conf into Apache's conf-available.
        self.assertEqual(len(cp_calls), 1)
        src, dest = cp_calls[0]
        self.assertTrue(src.endswith("onionpress-static-site.conf"))
        self.assertIn("/etc/apache2/conf-available/", dest)
        # Enabled the module + conf and reloaded Apache in one exec.
        self.assertEqual(len(exec_calls), 1)
        cmd = exec_calls[0]
        self.assertIn("a2enmod rewrite", cmd)
        self.assertIn("a2enconf onionpress-static-site", cmd)
        self.assertIn("apache2ctl graceful", cmd)

    def test_skips_when_conf_file_missing(self):
        logs = []
        with mock.patch.object(multisite, "_docker_cp") as cp, \
             mock.patch.object(multisite, "_exec_sh") as ex, \
             mock.patch("os.path.isfile", return_value=False):
            ok = multisite.install_static_site_conf(
                conf_dir="/nope", log_func=logs.append)

        self.assertFalse(ok)
        cp.assert_not_called()
        ex.assert_not_called()
        self.assertTrue(any("not found" in s for s in logs))

    def test_returns_false_when_cp_fails(self):
        with mock.patch.object(multisite, "_docker_cp", return_value=_err()), \
             mock.patch.object(multisite, "_exec_sh") as ex, \
             mock.patch("os.path.isfile", return_value=True):
            ok = multisite.install_static_site_conf(
                conf_dir="/x", log_func=lambda _: None)
        self.assertFalse(ok)
        # Never tries to enable a conf it failed to copy.
        ex.assert_not_called()

    def test_backs_the_conf_out_when_the_reload_fails(self):
        # a2enconf && apache2ctl graceful is not atomic. If the symlink is
        # created and then the reload fails, the conf LOOKS installed to
        # ensure_static_site_conf's presence probe while Apache is still
        # serving the old config — so every later start short-circuits on a
        # conf that never took effect. Undoing the symlink keeps the probe
        # honest and lets the next start retry.
        exec_calls = []

        def fake_exec(command, **kwargs):
            exec_calls.append(command)
            return _err(stderr="apache2ctl: syntax error")

        with mock.patch.object(multisite, "_docker_cp", return_value=_ok()), \
             mock.patch.object(multisite, "_exec_sh", side_effect=fake_exec), \
             mock.patch("os.path.isfile", return_value=True):
            ok = multisite.install_static_site_conf(
                conf_dir="/x", log_func=lambda _: None)

        self.assertFalse(ok)
        self.assertEqual(len(exec_calls), 2)
        self.assertIn("a2disconf onionpress-static-site", exec_calls[1])

    def test_reload_failure_survives_a_failing_rollback(self):
        # The rollback is best-effort: it shells out too, and must not turn
        # a reported failure into a raised one on the launcher's start path.
        def fake_exec(command, **kwargs):
            if "a2disconf" in command:
                raise subprocess.TimeoutExpired(cmd="docker", timeout=60)
            return _err(stderr="boom")

        with mock.patch.object(multisite, "_docker_cp", return_value=_ok()), \
             mock.patch.object(multisite, "_exec_sh", side_effect=fake_exec), \
             mock.patch("os.path.isfile", return_value=True):
            ok = multisite.install_static_site_conf(
                conf_dir="/x", log_func=lambda _: None)

        self.assertFalse(ok)


class TestEnsureStaticSiteConf(unittest.TestCase):
    """The conf lands in container rootfs, not a volume, so a container
    recreate drops it — and the launcher's already-running fast path exits
    before provision_post_install could put it back. ensure_static_site_conf
    is the guard for exactly that, and it has to stay cheap when nothing is
    wrong because it runs on every start.
    """

    def test_no_op_when_conf_already_enabled(self):
        with mock.patch.object(multisite, "_exec_sh",
                               return_value=_ok()) as ex, \
             mock.patch.object(multisite, "install_static_site_conf") as inst:
            ok = multisite.ensure_static_site_conf(
                conf_dir="/x/docker/wordpress", log_func=lambda _: None)

        self.assertTrue(ok)
        # Just the presence probe: no reinstall, and — the point of the
        # cheap path — no Apache reload on an already-healthy start.
        inst.assert_not_called()
        self.assertEqual(len(ex.call_args_list), 1)
        self.assertIn("conf-enabled/onionpress-static-site.conf",
                      ex.call_args_list[0].args[0])

    def test_reinstalls_when_conf_missing(self):
        # `test -e` on a missing file: rc=1 with NOTHING on stderr. That
        # empty stderr is load-bearing — see the docker-error test below.
        with mock.patch.object(multisite, "_exec_sh",
                               return_value=_err(stderr="")), \
             mock.patch.object(multisite, "install_static_site_conf",
                               return_value=True) as inst:
            ok = multisite.ensure_static_site_conf(
                conf_dir="/x/docker/wordpress", docker_bin="/b/docker",
                log_func=lambda _: None)

        self.assertTrue(ok)
        inst.assert_called_once()
        self.assertEqual(inst.call_args.kwargs.get("conf_dir"),
                         "/x/docker/wordpress")
        # The launcher passes a bundled docker binary; losing it here would
        # send the repair to a docker that may not be on PATH.
        self.assertEqual(inst.call_args.kwargs.get("docker_bin"), "/b/docker")

    def test_docker_error_is_not_mistaken_for_a_missing_conf(self):
        # A stopped container or dead daemon also exits non-zero, but writes
        # to stderr. Treating that as "conf missing" would log the
        # recreate-detected line and run a doomed copy — turning the one
        # diagnostic this function exists to emit into a false alarm.
        logged = []
        with mock.patch.object(
                multisite, "_exec_sh",
                return_value=_err(stderr="Error: No such container")), \
             mock.patch.object(multisite, "install_static_site_conf") as inst:
            ok = multisite.ensure_static_site_conf(
                conf_dir="/x", log_func=logged.append)

        self.assertFalse(ok)
        inst.assert_not_called()
        self.assertTrue(any("could not check" in m for m in logged), logged)
        self.assertFalse(any("recreated" in m for m in logged), logged)

    def test_never_raises_when_docker_unavailable(self):
        # This runs on the launcher's start path, so an exception escaping
        # here would turn a perfectly healthy already-running stack into a
        # failed `start`.
        with mock.patch.object(multisite, "_exec_sh",
                               side_effect=OSError("docker gone")), \
             mock.patch.object(multisite, "install_static_site_conf") as inst:
            ok = multisite.ensure_static_site_conf(
                conf_dir="/x", log_func=lambda _: None)

        self.assertFalse(ok)
        inst.assert_not_called()

    def test_never_raises_when_the_repair_itself_raises(self):
        # The probe is not the only thing that can throw: the reinstall
        # shells out too, and _docker_cp/_exec_sh raise TimeoutExpired on a
        # wedged daemon rather than returning non-zero. A `try` around only
        # the probe would let that escape into the launcher.
        with mock.patch.object(multisite, "_exec_sh",
                               return_value=_err(stderr="")), \
             mock.patch.object(
                 multisite, "install_static_site_conf",
                 side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=60)):
            ok = multisite.ensure_static_site_conf(
                conf_dir="/x", log_func=lambda _: None)

        self.assertFalse(ok)

    def test_reports_failure_when_the_repair_fails(self):
        # A failed reinstall must not be laundered into a success — the
        # publisher would go on publishing to a site the onion never serves.
        with mock.patch.object(multisite, "_exec_sh",
                               return_value=_err(stderr="")), \
             mock.patch.object(multisite, "install_static_site_conf",
                               return_value=False):
            ok = multisite.ensure_static_site_conf(
                conf_dir="/x", log_func=lambda _: None)

        self.assertFalse(ok)


class TestUploadsIniOverlayPath(unittest.TestCase):
    """The overlay's filename is the whole mechanism, so pin it. Unlike the
    static-site conf, the WordPress image already ships an
    onionpress-uploads.ini — present, but frozen at whatever limits the
    image was built with. We override it rather than overwrite it, which
    only works because PHP reads conf.d alphabetically and takes the last
    value it sees.
    """

    def test_overlay_sorts_after_the_images_own_ini(self):
        directory, _, name = multisite.UPLOADS_INI_PATH.rpartition("/")
        self.assertEqual(directory, "/usr/local/etc/php/conf.d")
        self.assertGreater(
            name, "onionpress-uploads.ini",
            "The overlay must sort AFTER the image's own ini or PHP reads "
            "it first and the image's stale limits win.",
        )

    def test_overlay_does_not_overwrite_the_images_own_ini(self):
        self.assertNotEqual(
            multisite.UPLOADS_INI_PATH,
            "/usr/local/etc/php/conf.d/onionpress-uploads.ini",
            "Writing over the image's own file would silently clobber an "
            "upstream revision of it, and leaves nothing clean to roll back.",
        )


class TestInstallUploadsIni(unittest.TestCase):
    """The PHP limits ini is injected at runtime for the same reason the
    static-site conf is — the WordPress image is pulled by digest from a
    registry the fork does not own, so a Dockerfile COPY only reaches
    whoever builds it. See install_uploads_ini's docstring.
    """

    def test_copies_overlay_and_reloads_apache(self):
        cp_calls = []
        exec_calls = []

        with mock.patch.object(
                multisite, "_docker_cp",
                side_effect=lambda s, d, **k: cp_calls.append((s, d)) or _ok()), \
             mock.patch.object(
                multisite, "_exec_sh",
                side_effect=lambda c, **k: exec_calls.append(c) or _ok()), \
             mock.patch("os.path.isfile", return_value=True):
            ok = multisite.install_uploads_ini(
                conf_dir="/x/docker/wordpress", log_func=lambda _: None)

        self.assertTrue(ok)
        self.assertEqual(len(cp_calls), 1)
        src, dest = cp_calls[0]
        self.assertTrue(src.endswith("onionpress-uploads.ini"))
        self.assertEqual(dest, f"onionpress-wordpress:{multisite.UPLOADS_INI_PATH}")
        # mod_php reads conf.d when a worker initialises, so the copy alone
        # leaves every live worker on the image's limits. Verified against
        # a real container: the served values only move after the graceful.
        self.assertEqual(exec_calls, ["apache2ctl graceful"])

    def test_skips_when_ini_missing(self):
        logs = []
        with mock.patch.object(multisite, "_docker_cp") as cp, \
             mock.patch.object(multisite, "_exec_sh") as ex, \
             mock.patch("os.path.isfile", return_value=False):
            ok = multisite.install_uploads_ini(
                conf_dir="/nope", log_func=logs.append)

        self.assertFalse(ok)
        cp.assert_not_called()
        ex.assert_not_called()
        self.assertTrue(any("not found" in s for s in logs))

    def test_returns_false_when_cp_fails(self):
        with mock.patch.object(multisite, "_docker_cp", return_value=_err()), \
             mock.patch.object(multisite, "_exec_sh") as ex, \
             mock.patch("os.path.isfile", return_value=True):
            ok = multisite.install_uploads_ini(
                conf_dir="/x", log_func=lambda _: None)
        self.assertFalse(ok)
        # Never reloads Apache for a file that failed to land.
        ex.assert_not_called()

    def test_removes_the_overlay_when_the_reload_fails(self):
        # Copy-then-reload is not atomic. A copied-but-unloaded overlay is
        # exactly what ensure_uploads_ini's presence probe reads as healthy,
        # so leaving it behind would short-circuit every later start on
        # limits Apache never applied.
        exec_calls = []

        def fake_exec(command, **kwargs):
            exec_calls.append(command)
            return _err(stderr="apache2ctl: syntax error")

        with mock.patch.object(multisite, "_docker_cp", return_value=_ok()), \
             mock.patch.object(multisite, "_exec_sh", side_effect=fake_exec), \
             mock.patch("os.path.isfile", return_value=True):
            ok = multisite.install_uploads_ini(
                conf_dir="/x", log_func=lambda _: None)

        self.assertFalse(ok)
        self.assertEqual(len(exec_calls), 2)
        self.assertIn(f"rm -f {multisite.UPLOADS_INI_PATH}", exec_calls[1])

    def test_reload_failure_survives_a_failing_rollback(self):
        # The rollback shells out too, and must not turn a reported failure
        # into a raised one on the launcher's start path.
        def fake_exec(command, **kwargs):
            if command.startswith("rm -f"):
                raise subprocess.TimeoutExpired(cmd="docker", timeout=60)
            return _err(stderr="boom")

        with mock.patch.object(multisite, "_docker_cp", return_value=_ok()), \
             mock.patch.object(multisite, "_exec_sh", side_effect=fake_exec), \
             mock.patch("os.path.isfile", return_value=True):
            ok = multisite.install_uploads_ini(
                conf_dir="/x", log_func=lambda _: None)

        self.assertFalse(ok)


class TestEnsureUploadsIni(unittest.TestCase):
    """conf.d is container rootfs, so a recreate drops the overlay and
    restores the image's lower limits — and the launcher's
    already-running fast path exits before provisioning could put it back.
    Has to stay cheap: it runs on every start.
    """

    def test_no_op_when_overlay_already_present(self):
        with mock.patch.object(multisite, "_exec_sh",
                               return_value=_ok()) as ex, \
             mock.patch.object(multisite, "install_uploads_ini") as inst:
            ok = multisite.ensure_uploads_ini(
                conf_dir="/x/docker/wordpress", log_func=lambda _: None)

        self.assertTrue(ok)
        # Just the probe: no copy, and — the point of the cheap path — no
        # Apache reload on an already-healthy start.
        inst.assert_not_called()
        self.assertEqual(len(ex.call_args_list), 1)
        self.assertIn(multisite.UPLOADS_INI_PATH, ex.call_args_list[0].args[0])

    def test_reinstalls_when_overlay_missing(self):
        # `test -e` on a missing file: rc=1 with NOTHING on stderr. That
        # empty stderr is load-bearing — see the docker-error test below.
        with mock.patch.object(multisite, "_exec_sh",
                               return_value=_err(stderr="")), \
             mock.patch.object(multisite, "install_uploads_ini",
                               return_value=True) as inst:
            ok = multisite.ensure_uploads_ini(
                conf_dir="/x/docker/wordpress", docker_bin="/b/docker",
                log_func=lambda _: None)

        self.assertTrue(ok)
        inst.assert_called_once()
        self.assertEqual(inst.call_args.kwargs.get("conf_dir"),
                         "/x/docker/wordpress")
        # The launcher passes a bundled docker binary; losing it here would
        # send the repair to a docker that may not be on PATH.
        self.assertEqual(inst.call_args.kwargs.get("docker_bin"), "/b/docker")

    def test_docker_error_is_not_mistaken_for_a_missing_overlay(self):
        # A stopped container or dead daemon also exits non-zero, but writes
        # to stderr. Treating that as "overlay missing" would claim a
        # recreate happened and run a copy that cannot land.
        logged = []
        with mock.patch.object(
                multisite, "_exec_sh",
                return_value=_err(stderr="Error: No such container")), \
             mock.patch.object(multisite, "install_uploads_ini") as inst:
            ok = multisite.ensure_uploads_ini(conf_dir="/x",
                                              log_func=logged.append)

        self.assertFalse(ok)
        inst.assert_not_called()
        self.assertTrue(any("could not check" in m for m in logged), logged)
        self.assertFalse(any("recreated" in m for m in logged), logged)

    def test_never_raises_when_the_probe_raises(self):
        # An exception escaping here turns a perfectly healthy
        # already-running stack into a failed `start`.
        with mock.patch.object(multisite, "_exec_sh",
                               side_effect=OSError("docker gone")), \
             mock.patch.object(multisite, "install_uploads_ini") as inst:
            ok = multisite.ensure_uploads_ini(conf_dir="/x",
                                              log_func=lambda _: None)

        self.assertFalse(ok)
        inst.assert_not_called()

    def test_never_raises_when_the_repair_raises(self):
        # The probe is not the only thing that can throw: the repair shells
        # out too, and _docker_cp/_exec_sh raise TimeoutExpired on a wedged
        # daemon rather than returning non-zero. A `try` around only the
        # probe would let that escape into the launcher.
        with mock.patch.object(multisite, "_exec_sh",
                               return_value=_err(stderr="")), \
             mock.patch.object(
                 multisite, "install_uploads_ini",
                 side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=60)):
            ok = multisite.ensure_uploads_ini(conf_dir="/x",
                                              log_func=lambda _: None)

        self.assertFalse(ok)

    def test_reports_failure_when_the_repair_fails(self):
        # A failed reinstall must not be laundered into a success — the
        # next large upload would fail at the image's stale limits with
        # nothing in the log to say the repair had been attempted and lost.
        with mock.patch.object(multisite, "_exec_sh",
                               return_value=_err(stderr="")), \
             mock.patch.object(multisite, "install_uploads_ini",
                               return_value=False):
            ok = multisite.ensure_uploads_ini(conf_dir="/x",
                                              log_func=lambda _: None)

        self.assertFalse(ok)


class TestProvisionInjectsStaticConf(unittest.TestCase):
    """provision_post_install only injects the runtime confs when a conf_dir
    is supplied — earlier callers that omit it keep the old behavior. Both
    the static-site conf and the PHP limits overlay come from that one
    directory and share the guard.
    """

    def _patch_all_but_conf(self):
        def fake(**kwargs):
            return True
        return [
            mock.patch.object(multisite, "ensure_multisite", fake),
            mock.patch.object(multisite, "install_multisite_domain_map", fake),
            mock.patch.object(multisite, "install_onionpress_theme", fake),
            mock.patch.object(multisite, "fix_onionpress_permissions", fake),
            mock.patch.object(multisite, "fix_wordpress_uploads_permissions", fake),
            mock.patch.object(multisite, "write_shared_onion_address", fake),
        ]

    def test_injects_when_conf_dir_given(self):
        patches = self._patch_all_but_conf()
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        with mock.patch.object(multisite, "install_static_site_conf") as static, \
             mock.patch.object(multisite, "install_uploads_ini") as ini:
            multisite.provision_post_install(
                themes_dir="/t", plugins_dir="/p",
                conf_dir="/d/docker/wordpress")
        for inj in (static, ini):
            inj.assert_called_once()
            self.assertEqual(inj.call_args.kwargs.get("conf_dir"),
                             "/d/docker/wordpress")

    def test_skips_when_no_conf_dir(self):
        patches = self._patch_all_but_conf()
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        with mock.patch.object(multisite, "install_static_site_conf") as static, \
             mock.patch.object(multisite, "install_uploads_ini") as ini:
            multisite.provision_post_install(themes_dir="/t", plugins_dir="/p")
        static.assert_not_called()
        ini.assert_not_called()


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

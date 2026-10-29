#!/usr/bin/env python3
"""Tests for src/onionpress/multisite.py — the WordPress post-install
provisioning module shared between Mac and Linux.

These mock out subprocess so they run without docker. The behavioral
tests (containers actually come up, theme actually activates) live in
the adversarial-CI harness (#252) — these tests only verify the
orchestration glue: right wp-cli calls in the right order, right
docker cp invocations, right error handling.
"""

import pathlib
import re
import subprocess
import unittest
from unittest import mock
from urllib.parse import quote_plus

from onionpress import multisite
from onionpress.onionnames_client import _NAME_ALLOWED_RE


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
             mock.patch.object(multisite, "_put_tree") as put:
            multisite.install_onionpress_theme(
                themes_dir="/x", plugins_dir="/y",
                log_func=lambda _: None)
        put.assert_not_called()

    def test_theme_and_plugins_are_replaced_whole_as_www_data(self):
        # A copy into an existing dir lands INSIDE it — THE bug that bit
        # before: the Linux version ended up with /themes/onionpress/onionpress/.
        # _put_tree replaces the folder (wp_files.REPLACE_TREE_SCRIPT), as
        # www-data: no root rm -rf / docker cp / chown -R, which followed
        # links www-data can plant in wp-content.
        exec_calls = []

        def fake_exec(cmd, **kwargs):
            exec_calls.append(cmd)
            return _ok()

        put_calls = []

        def fake_put(src, dest, **kwargs):
            put_calls.append((src, dest))
            return _ok()

        with mock.patch.object(multisite, "wp_is_installed", return_value=True), \
             mock.patch.object(multisite, "_exec_sh", side_effect=fake_exec), \
             mock.patch.object(multisite, "_put_tree", side_effect=fake_put), \
             mock.patch.object(multisite, "_wp", return_value=_ok()), \
             mock.patch("os.path.isdir", return_value=True):
            multisite.install_onionpress_theme(
                themes_dir="/x/themes", plugins_dir="/x/plugins",
                log_func=lambda _: None)

        self.assertEqual(put_calls, [
            ("/x/themes/onionpress", "/var/www/html/wp-content/themes/onionpress"),
            ("/x/plugins/onionpress-hit-counter",
             "/var/www/html/wp-content/plugins/onionpress-hit-counter"),
            ("/x/plugins/onionpress-creations",
             "/var/www/html/wp-content/plugins/onionpress-creations"),
        ])
        self.assertEqual([c for c in exec_calls if "rm -rf" in c or "chown" in c], [])
        self.assertIn('rm -rf "$1" && mv -f "$n" "$1"', multisite.wp_files.REPLACE_TREE_SCRIPT)

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
             mock.patch.object(multisite, "_put_tree", return_value=_ok()), \
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
            # #257: without it a restored site shows broken images until
            # the background regeneration finishes.
            "onionpress-image-fallback.php",
        }
        missing = critical - set(multisite.MU_PLUGINS)
        self.assertFalse(
            missing,
            f"Critical mu-plugins missing from MU_PLUGINS: {missing}",
        )

    # The directory's files are what an install can deploy; MU_PLUGINS is
    # what provision-post-install deploys (both platforms). Linux's
    # `onionpress update` copies every *.php there into mu-plugins/, so a
    # file left out of the tuple reaches updated Linux sites and no other
    # install: the Reddit and Blog importers were in that state (#229).
    PLUGINS_DIR = pathlib.Path(__file__).resolve().parent.parent / "app" / "Resources" / "plugins"
    # Installed as wp-content/sunrise.php by provision_post_install, never
    # as an mu-plugin.
    NOT_MU_PLUGINS = {"onionpress-sunrise.php"}

    def test_mu_plugins_is_the_directory(self):
        on_disk = {
            p.name for p in self.PLUGINS_DIR.iterdir()
            if p.is_file() and p.name.startswith("onionpress-")
            and p.name.endswith(".php")
        }
        self.assertTrue(on_disk, f"no mu-plugins found in {self.PLUGINS_DIR}")
        listed = set(multisite.MU_PLUGINS)
        self.assertEqual(len(multisite.MU_PLUGINS), len(listed), "duplicate in MU_PLUGINS")
        self.assertFalse(
            listed & self.NOT_MU_PLUGINS,
            f"{listed & self.NOT_MU_PLUGINS} is not an mu-plugin",
        )
        not_deployed = on_disk - self.NOT_MU_PLUGINS - listed
        self.assertFalse(
            not_deployed,
            f"in app/Resources/plugins but not in MU_PLUGINS (never installed): {not_deployed}",
        )
        not_on_disk = listed - on_disk
        self.assertFalse(
            not_on_disk,
            f"in MU_PLUGINS but not in app/Resources/plugins: {not_on_disk}",
        )


class TestConfigureIaPlugin(unittest.TestCase):
    def test_skips_when_wp_not_installed(self):
        with mock.patch.object(multisite, "wp_is_installed", return_value=False), \
             mock.patch.object(multisite, "_wp") as wp:
            multisite.configure_ia_plugin(log_func=lambda _: None)
        wp.assert_not_called()

    def test_skips_when_already_configured(self):
        # The site carries OnionPress's marker at the current version →
        # short-circuit, no option-update calls (tests/test_bundled_plugins.py
        # covers the rest).
        def fake_wp(*args, **kwargs):
            if "get" in args and multisite.IA_CONFIG_MARKER in args:
                return subprocess.CompletedProcess(
                    args=[], returncode=0,
                    stdout=multisite.IA_CONFIG_VERSION + "\n", stderr="")
            return _ok()

        update_calls = []
        def tracker(*args, **kwargs):
            r = fake_wp(*args, **kwargs)
            if "update" in args:
                update_calls.append(args)
            return r

        with mock.patch.object(multisite, "wp_is_installed", return_value=True), \
             mock.patch.object(multisite, "_exec_www_data", return_value=_ok()), \
             mock.patch.object(multisite, "_wp", side_effect=tracker):
            self.assertTrue(multisite.configure_ia_plugin(log_func=lambda _: None))
        self.assertEqual(
            update_calls, [],
            "must NOT re-write IA plugin options when already configured",
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
                wp_updates.append((args, kwargs.get("input")))
            return _ok()  # get returns empty stdout → keys not set
        login_response = (
            '{"success": true, "values": {"s3": '
            '{"access": "AKEY", "secret": "SKEY"}}}'
        )
        with mock.patch.object(multisite, "wp_is_installed", return_value=True), \
             mock.patch.object(multisite, "_wp", side_effect=fake_wp), \
             mock.patch("subprocess.run", return_value=subprocess.CompletedProcess(
                 args=[], returncode=0, stdout=login_response, stderr="")) as srun:
            result = multisite.ensure_archive_s3_keys(log_func=lambda _: None)
        self.assertTrue(result)
        # The keys reach `wp option update` on stdin, not in argv.
        access = [a for a, i in wp_updates
                  if "onionpress_archive_s3_access" in a and i == "AKEY"]
        secret = [a for a, i in wp_updates
                  if "onionpress_archive_s3_secret" in a and i == "SKEY"]
        self.assertTrue(access, "must update onionpress_archive_s3_access")
        self.assertTrue(secret, "must update onionpress_archive_s3_secret")
        for args, _ in wp_updates:
            self.assertNotIn("SKEY", args)
        # The login form goes to curl on stdin too.
        argv = srun.call_args.args[0]
        self.assertEqual(argv[1:4], ["exec", "-i", "onionheaven"])
        self.assertEqual(argv[argv.index("--data-binary") + 1], "@-")
        self.assertIn("password=" + quote_plus(multisite._ARCHIVE_LOGIN_PASS),
                      srun.call_args.kwargs["input"])
        for arg in argv:
            self.assertNotIn(multisite._ARCHIVE_LOGIN_PASS, arg)
        # archive.org's onion, never clearnet (issue #322).
        self.assertEqual(argv[-1], multisite._ARCHIVE_ONION + "/services/xauthn/?op=login")

    def test_login_timeout_is_a_warning_not_an_exception(self):
        logs = []
        with mock.patch.object(multisite, "wp_is_installed", return_value=True), \
             mock.patch.object(multisite, "_wp", return_value=_ok()), \
             mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired(
                 ["docker", "exec", "-i", "onionheaven", "curl"], 75)):
            result = multisite.ensure_archive_s3_keys(log_func=logs.append)
        self.assertFalse(result)
        self.assertTrue(any("timed out" in s for s in logs), logs)

    def test_wp_helper_adds_dash_i_only_with_input(self):
        with mock.patch.object(multisite.subprocess, "run",
                               return_value=_ok()) as srun:
            multisite._wp("option", "get", "x")
            multisite._wp("option", "update", "x", input="v")
        plain, piped = (c.args[0] for c in srun.call_args_list)
        # As www-data, never root, and never --allow-root; and with no
        # outbound request from the WordPress it loads (issue #373).
        self.assertEqual(plain, ["docker", "exec", "-u", "www-data", "-e",
                                 "WP_CLI_CACHE_DIR=/tmp/wp-cli-cache",
                                 "-e", "ONIONPRESS_WP_OFFLINE=1",
                                 "onionpress-wordpress", "wp", "option", "get", "x"])
        self.assertEqual(piped[:4], ["docker", "exec", "-i", "-u"])
        self.assertEqual(piped[1:], ["exec", "-i", "-u", "www-data", "-e",
                                     "WP_CLI_CACHE_DIR=/tmp/wp-cli-cache",
                                     "-e", "ONIONPRESS_WP_OFFLINE=1",
                                     "onionpress-wordpress", "wp", "option",
                                     "update", "x"])
        self.assertEqual(srun.call_args_list[1].kwargs["input"], "v")

    def test_handles_tor_login_failure_gracefully(self):
        logs = []
        with mock.patch.object(multisite, "wp_is_installed", return_value=True), \
             mock.patch.object(multisite, "_wp", return_value=_ok()), \
             mock.patch("subprocess.run", return_value=subprocess.CompletedProcess(
                 args=[], returncode=0, stdout="", stderr="")):
            result = multisite.ensure_archive_s3_keys(log_func=logs.append)
        self.assertFalse(result)
        self.assertTrue(any("Could not reach archive.org" in s for s in logs))


def _wp_content_strip_rule():
    """Extract the .htaccess RewriteRule that strips the leading
    /<onionname>/ segment before shared wp-content/wp-admin/wp-includes
    requests reach WordPress, as a compiled regex for testing.
    """
    m = re.search(
        r"RewriteRule \^\((\[[^\]]+\]\+)/\)\?\(wp-\(content\|admin\|includes\)",
        multisite.HTACCESS_BODY,
    )
    assert m, "could not find the wp-content strip rule in HTACCESS_BODY"
    return re.compile("^" + m.group(1) + "/")


def _extract_mac_launcher_htaccess():
    """Pull the heredoc the Mac launcher writes when .htaccess is missing
    or empty (app/MacOS/onionpress, before provision-post-install runs).
    """
    path = (pathlib.Path(__file__).resolve().parent.parent
            / "app" / "MacOS" / "onionpress")
    m = re.search(
        r'wp_put /var/www/html/\.htaccess 644 [^\n]*<< "HTEOF"\n(.*?)\nHTEOF\n',
        path.read_text(encoding="utf-8"), re.S,
    )
    assert m, "could not find the .htaccess heredoc in app/MacOS/onionpress"
    return m.group(1)


class TestHtaccessMatchesOnionnameCharset(unittest.TestCase):
    """Regression test: the .htaccess strip rule's character class must
    cover every character onionnames_client._NAME_ALLOWED_RE allows.
    A onionname with a character the strip rule doesn't recognize falls
    through to WordPress's PHP front controller for every static asset
    request, instead of being served directly by Apache — broken CSS/JS/
    images with no error anywhere.
    """

    SAMPLE_ONIONNAMES = [
        "myname",
        "my-name",
        "my_name",
        "my.name",
        "abc123",
        "a.b-c_d9",
    ]

    def test_validator_and_htaccess_charset_examples_agree(self):
        prefix_re = _wp_content_strip_rule()
        for name in self.SAMPLE_ONIONNAMES:
            self.assertTrue(_NAME_ALLOWED_RE.match(name),
                             f"{name!r} should be a valid onionname")
            self.assertTrue(
                prefix_re.match(name + "/"),
                f"htaccess strip rule does not match onionname {name!r} - "
                "static assets would silently fall through to PHP for this user"
            )

    def test_dotted_onionname_regression(self):
        # The original bug: '.' was missing from the strip rule's
        # character class even though it's a valid onionname character.
        prefix_re = _wp_content_strip_rule()
        self.assertTrue(prefix_re.match("my.name/"))


class TestHtaccessDuplicatesStayInSync(unittest.TestCase):
    """The .htaccess rewrite rules are hand-duplicated in multisite.py
    (Linux / generic-static path) and the Mac launcher's missing-.htaccess
    fallback. Nothing else enforces the copies match, so a fix applied to
    one and not the others silently reintroduces the bug on the other
    platform.
    """

    def test_onion_proxy_has_no_copy(self):
        # Its /setup goes through setup_logic.install_fresh_wordpress and
        # provision-post-install (issue #481), which write HTACCESS_BODY.
        text = (pathlib.Path(__file__).resolve().parent.parent
                / "src" / "onionpress" / "onion_proxy.py").read_text()
        self.assertNotIn("BEGIN WordPress Multisite", text)
        self.assertNotIn("multisite-install", text)

    def test_mac_launcher_htaccess_matches_multisite(self):
        self.assertEqual(
            multisite.HTACCESS_BODY.strip(),
            _extract_mac_launcher_htaccess().strip(),
            "app/MacOS/onionpress's .htaccess heredoc has drifted from "
            "multisite.HTACCESS_BODY - keep the two in sync"
        )


class TestThemeOnionnameIsTheSubsiteOwner(unittest.TestCase):
    """The theme's onionpress_get_onionname() names the Follow button. It
    read $blog->user_id, which WP_Site does not have, so a subsite fell
    through to its lowest-ID administrator: the network owner, whom
    `wp site create` adds to every site (issue #321). The owner is the
    user whose login is the subsite's path, /<onionname>/."""

    def test_reads_the_path_not_a_missing_property(self):
        root = pathlib.Path(__file__).resolve().parent.parent
        src = (root / "app/Resources/themes/onionpress/functions.php").read_text()
        start = src.index("function onionpress_get_onionname()")
        body = src[start:src.index("\n}\n", start)]
        self.assertNotIn("->user_id", body)
        path = body.index("trim( (string) $blog->path, '/' )")
        login = body.index("get_user_by( 'login', $slug )")
        member = body.index("is_user_member_of_blog( $user->ID, $blog_id )")
        fallback = body.index("'role'    => 'administrator'")
        self.assertLess(path, login)
        self.assertLess(login, member)
        self.assertLess(member, fallback)
        # create_primary_subsite names the subsite after the login.
        logic = (root / "src/onionpress/setup_logic.py").read_text()
        self.assertIn('f"--slug={onionname}"', logic)


if __name__ == "__main__":
    unittest.main()

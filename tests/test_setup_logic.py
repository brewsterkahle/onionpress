"""Tests for src/onionpress/setup_logic.py.

Focused on install_fresh_wordpress() — the shared install path used by
the GTK SetupDialog, Mac SetupWindow, and `onionpress setup` SSH CLI.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from onionpress import setup_logic  # noqa: E402

ONION = "abc" + "d" * 53 + ".onion"


def _ok(stdout=""):
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout=stdout, stderr="",
    )


def _fail(stderr="error", code=1):
    return subprocess.CompletedProcess(
        args=[], returncode=code, stdout="", stderr=stderr,
    )


class TestInstallFreshWordpress(unittest.TestCase):
    """install_fresh_wordpress: wp core install + user_url + post-install."""

    def setUp(self):
        # Each test gets a temp data_dir so the ONIONNAME config write
        # doesn't escape into the user's real ~/.onionpress.
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._rmtree)

    def _rmtree(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run(self, **kwargs):
        defaults = dict(
            site_title="My Blog",
            onionname="alice",
            password="hunter22",
            onion_addr=ONION,
            launcher_bin="/usr/local/bin/onionpress",
            data_dir=self.tmpdir,
        )
        defaults.update(kwargs)
        # Never start a real launcher (a machine running the tests may have
        # one at these paths, with a live site behind it).
        with mock.patch.object(setup_logic, "start_bundled_plugin_setup",
                               return_value=True) as self.plugin_setup:
            return setup_logic.install_fresh_wordpress(**defaults)

    def test_happy_path_invokes_wp_core_install(self):
        with mock.patch(
            "onionpress.setup_logic.subprocess.run", return_value=_ok(),
        ) as mrun:
            ok = self._run()
        self.assertTrue(ok)
        # First call must be `wp core install` with the user-typed name;
        # the password is set on stdin after it (test_secrets_off_argv_setup).
        first_call_args = mrun.call_args_list[0].args[0]
        self.assertIn("core", first_call_args)
        self.assertIn("install", first_call_args)
        self.assertIn("--admin_user=alice", first_call_args)
        self.assertFalse(any(a.startswith("--admin_password")
                             for a in first_call_args))
        self.assertIn(f"--url=http://{ONION}", first_call_args)
        self.assertIn("--title=My Blog", first_call_args)

    def test_failure_at_wp_core_install_returns_false(self):
        with mock.patch(
            "onionpress.setup_logic.subprocess.run",
            return_value=_fail("DB connection refused"),
        ):
            ok = self._run()
        self.assertFalse(ok)

    def test_post_install_subcommand_invoked_with_launcher_bin(self):
        # Capture every subprocess.run call; assert one of them invokes
        # the launcher's provision-post-install subcommand.
        with mock.patch(
            "onionpress.setup_logic.subprocess.run", return_value=_ok(),
        ) as mrun:
            self._run(launcher_bin="/opt/onionpress/onionpress")
        post_install_calls = [
            c for c in mrun.call_args_list
            if c.args and isinstance(c.args[0], list)
            and "provision-post-install" in c.args[0]
        ]
        self.assertEqual(len(post_install_calls), 1)
        self.assertEqual(
            post_install_calls[0].args[0],
            ["/opt/onionpress/onionpress", "provision-post-install"],
        )

    def test_post_install_skipped_when_launcher_bin_missing(self):
        with mock.patch(
            "onionpress.setup_logic.subprocess.run", return_value=_ok(),
        ) as mrun:
            self._run(launcher_bin=None)
        # No `provision-post-install` should be invoked.
        for c in mrun.call_args_list:
            argv = c.args[0] if c.args else []
            self.assertNotIn(
                "provision-post-install", argv,
                "launcher_bin=None should suppress the post-install hop",
            )

    def test_onionname_persisted_to_config(self):
        with mock.patch(
            "onionpress.setup_logic.subprocess.run", return_value=_ok(),
        ):
            ok = self._run(onionname="bobby")
        self.assertTrue(ok)
        with open(os.path.join(self.tmpdir, "config")) as f:
            contents = f.read()
        self.assertIn("ONIONNAME=bobby", contents)

    def test_user_url_update_uses_per_user_path(self):
        # The "Website" link on the WP user profile should point to
        # http://<onion>/<onionname>/ not the bare onion root.
        with mock.patch(
            "onionpress.setup_logic.subprocess.run", return_value=_ok(),
        ) as mrun:
            self._run(onionname="alice", onion_addr=ONION)
        user_url_calls = [
            c for c in mrun.call_args_list
            if c.args and isinstance(c.args[0], list)
            and "user" in c.args[0] and "update" in c.args[0]
            and any("user_url" in a for a in c.args[0])
        ]
        self.assertTrue(user_url_calls)
        argv = user_url_calls[0].args[0]
        self.assertIn(f"--user_url=http://{ONION}/alice/", argv)


class TestProvisionInteractiveRouting(unittest.TestCase):
    """provision_interactive (the SSH/TUI path) routes through the right
    install fn based on whether WP is already installed."""

    def setUp(self):
        # Stub input/getpass to feed deterministic values into the
        # interactive prompts so we can run the function headless.
        patches = [
            mock.patch("builtins.input", side_effect=["My Blog", "alice"]),
            mock.patch("getpass.getpass", side_effect=["hunter22", "hunter22"]),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_routes_to_install_fresh_when_wp_not_installed(self):
        with mock.patch(
            "onionpress.setup_logic._wp_is_installed", return_value=False,
        ), mock.patch(
            "onionpress.setup_logic._read_onion_address",
            return_value="abc.onion",
        ), mock.patch(
            "onionpress.setup_logic.install_fresh_wordpress",
            return_value=True,
        ) as m_fresh, mock.patch(
            "onionpress.setup_logic.provision_existing_wordpress",
            return_value=True,
        ) as m_existing:
            ok = setup_logic.provision_interactive()
        self.assertTrue(ok)
        m_fresh.assert_called_once()
        m_existing.assert_not_called()
        # The fresh path must pass the live onion address through.
        self.assertEqual(m_fresh.call_args.kwargs["onion_addr"], "abc.onion")
        self.assertEqual(m_fresh.call_args.kwargs["onionname"], "alice")

    def test_routes_to_existing_when_wp_already_installed(self):
        with mock.patch(
            "onionpress.setup_logic._wp_is_installed", return_value=True,
        ), mock.patch(
            "onionpress.setup_logic.install_fresh_wordpress",
            return_value=True,
        ) as m_fresh, mock.patch(
            "onionpress.setup_logic.provision_existing_wordpress",
            return_value=True,
        ) as m_existing:
            ok = setup_logic.provision_interactive()
        self.assertTrue(ok)
        m_existing.assert_called_once()
        m_fresh.assert_not_called()


class TestProvisionPostInstallSubcommandPresent(unittest.TestCase):
    """Invariant: both bash launchers must expose a
    `provision-post-install` subcommand. install_fresh_wordpress shells
    out to `<launcher> provision-post-install` after `wp core install`,
    so deleting it from either launcher silently breaks the install
    path on that platform.
    """

    def _read(self, *path_parts):
        full = os.path.join(os.path.dirname(__file__), "..", *path_parts)
        with open(full) as f:
            return f.read()

    def test_linux_launcher_has_subcommand(self):
        src = self._read("linux", "onionpress")
        self.assertIn(
            "provision-post-install)", src,
            "linux/onionpress is missing the `provision-post-install)` "
            "case — install_fresh_wordpress's post-install hop will fail",
        )

    def test_mac_launcher_has_subcommand(self):
        src = self._read("app", "MacOS", "onionpress")
        self.assertIn(
            "provision-post-install)", src,
            "app/MacOS/onionpress is missing the `provision-post-install)` "
            "case — install_fresh_wordpress's post-install hop will fail",
        )


class TestDebPrermKillsTray(unittest.TestCase):
    """Invariant: the .deb's prerm script must kill the running tray.

    Without it, `apt remove onionpress` leaves the in-memory tray
    process up; its indicator keeps painting and every status poll
    ENOENTs on the now-deleted /opt/onionpress files until logout.
    """

    def test_prerm_pkill_onionpress_tray(self):
        build_script = os.path.join(
            os.path.dirname(__file__), "..", "build", "build-linux.sh",
        )
        with open(build_script) as f:
            src = f.read()
        self.assertIn(
            "pkill -f /opt/onionpress/onionpress-tray", src,
            "build-linux.sh's prerm must pkill the tray on remove",
        )


class TestNoBootstrapPasswordOnNewInstalls(unittest.TestCase):
    """Invariant: the bash launcher must no longer auto-write
    ~/.onionpress/wp-admin-password on non-interactive (systemd) start.

    Tested by grepping the launcher source for the abandoned pattern.
    If a future refactor needs to re-introduce a bootstrap password,
    delete this test deliberately rather than working around it.
    """

    def test_launcher_does_not_echo_password_to_data_dir(self):
        launcher_path = os.path.join(
            os.path.dirname(__file__), "..", "linux", "onionpress",
        )
        with open(launcher_path) as f:
            src = f.read()
        self.assertNotIn(
            'echo "$auto_pass" > "$DATA_DIR/wp-admin-password"', src,
            "bash launcher should not auto-generate + persist a "
            "bootstrap WP admin password on first systemd start; "
            "the SetupDialog / `onionpress setup` does the install now",
        )


class TestOnionnameErrorMessages(unittest.TestCase):
    """Issue #324: the Linux tray and `onionpress setup` said "min 2" and
    "max 63" while the rule enforced 5 and 40. Every front end's message
    must name the limits the rule actually enforces."""

    REPO = os.path.join(os.path.dirname(__file__), "..")

    def _read(self, rel):
        with open(os.path.join(self.REPO, rel), encoding="utf-8") as f:
            return f.read()

    def test_local_rule_matches_onionnames_client(self):
        from onionpress import onionnames_client
        self.assertEqual(setup_logic._ONIONNAME_MIN_LEN, onionnames_client.MIN_NAME_LEN)
        self.assertEqual(setup_logic._ONIONNAME_MAX_LEN, onionnames_client.MAX_NAME_LEN)

    def test_messages_name_the_enforced_limits(self):
        lo, hi = setup_logic._ONIONNAME_MIN_LEN, setup_logic._ONIONNAME_MAX_LEN
        short = "a" * (lo - 1)
        long_ = "a" * (hi + 1)
        self.assertEqual(setup_logic.validate_onionname(short)[1], "too_short")
        self.assertEqual(setup_logic.validate_onionname(long_)[1], "too_long")
        self.assertIn(f"min {lo} ", setup_logic.onionname_error_message("too_short"))
        self.assertIn(f"max {hi} ", setup_logic.onionname_error_message("too_long"))
        # The limits themselves are valid lengths.
        self.assertTrue(setup_logic.validate_onionname("a" * lo)[0])
        self.assertTrue(setup_logic.validate_onionname("a" * hi)[0])

    def test_every_reason_has_its_own_message(self):
        fallback = setup_logic.onionname_error_message("no-such-reason")
        for reason in ("empty", "too_short", "too_long", "invalid_chars", "all_numeric"):
            self.assertNotEqual(setup_logic.onionname_error_message(reason), fallback, reason)

    def test_linux_front_ends_use_the_shared_messages(self):
        """No hard-coded length message in the tray or the CLI setup."""
        for rel in ("linux/onionpress-tray", "src/onionpress/setup_logic.py"):
            src = self._read(rel)
            self.assertNotRegex(src, r"(?i)(min|max) \d+ characters\"", rel)
        self.assertIn("onionname_error_message(reason)",
                      self._read("linux/onionpress-tray"))

    def test_mac_setup_window_names_the_enforced_limits(self):
        src = self._read("src/onionpress/setup_window.py")
        self.assertIn(f"at least {setup_logic._ONIONNAME_MIN_LEN} characters", src)
        self.assertIn(f"{setup_logic._ONIONNAME_MAX_LEN} characters or fewer", src)


if __name__ == "__main__":
    unittest.main()

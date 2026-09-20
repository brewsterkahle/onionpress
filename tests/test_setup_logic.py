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
            onion_addr="abc.onion",
            launcher_bin="/usr/local/bin/onionpress",
            data_dir=self.tmpdir,
        )
        defaults.update(kwargs)
        return setup_logic.install_fresh_wordpress(**defaults)

    def test_happy_path_invokes_wp_core_install(self):
        with mock.patch(
            "onionpress.setup_logic.subprocess.run", return_value=_ok(),
        ) as mrun:
            ok = self._run()
        self.assertTrue(ok)
        # First call must be `wp core install` with the user-typed creds.
        first_call_args = mrun.call_args_list[0].args[0]
        self.assertIn("core", first_call_args)
        self.assertIn("install", first_call_args)
        self.assertIn("--admin_user=alice", first_call_args)
        self.assertIn("--admin_password=hunter22", first_call_args)
        self.assertIn("--url=http://abc.onion", first_call_args)
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
            ok = self._run(onionname="bob")
        self.assertTrue(ok)
        with open(os.path.join(self.tmpdir, "config")) as f:
            contents = f.read()
        self.assertIn("ONIONNAME=bob", contents)

    def test_user_url_update_uses_per_user_path(self):
        # The "Website" link on the WP user profile should point to
        # http://<onion>/<onionname>/ not the bare onion root.
        with mock.patch(
            "onionpress.setup_logic.subprocess.run", return_value=_ok(),
        ) as mrun:
            self._run(onionname="alice", onion_addr="abc.onion")
        user_url_calls = [
            c for c in mrun.call_args_list
            if c.args and isinstance(c.args[0], list)
            and "user" in c.args[0] and "update" in c.args[0]
            and any("user_url" in a for a in c.args[0])
        ]
        self.assertTrue(user_url_calls)
        argv = user_url_calls[0].args[0]
        self.assertIn("--user_url=http://abc.onion/alice/", argv)


class TestProvisionInteractiveRouting(unittest.TestCase):
    """provision_interactive (the SSH/TUI path) routes through the right
    install fn based on whether WP is already installed."""

    def setUp(self):
        # Stub input/getpass to feed deterministic values into the
        # interactive prompts so we can run the function headless.
        # First input is the WordPress-vs-static chooser ("" -> wordpress,
        # the default), then the existing site title / username prompts.
        patches = [
            mock.patch("builtins.input", side_effect=["", "My Blog", "alice"]),
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


class TestProvisionStaticSite(unittest.TestCase):
    """provision_static_site: content dir + placeholder + persisted config,
    no wp-cli involved anywhere."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._rmtree)

    def _rmtree(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_creates_site_dir_with_placeholder(self):
        docs_dir = os.path.join(self.tmpdir, "OnionPress")
        ok = setup_logic.provision_static_site(
            onionname="alice", documents_dir=docs_dir, data_dir=self.tmpdir,
        )
        self.assertTrue(ok)
        site_dir = os.path.join(docs_dir, "Site")
        self.assertTrue(os.path.isdir(site_dir))
        self.assertTrue(os.path.isfile(os.path.join(site_dir, "index.html")))

    def test_does_not_overwrite_existing_content(self):
        docs_dir = os.path.join(self.tmpdir, "OnionPress")
        site_dir = os.path.join(docs_dir, "Site")
        os.makedirs(site_dir)
        with open(os.path.join(site_dir, "index.html"), "w") as f:
            f.write("already published")

        setup_logic.provision_static_site(
            onionname="alice", documents_dir=docs_dir, data_dir=self.tmpdir,
        )
        with open(os.path.join(site_dir, "index.html")) as f:
            self.assertEqual(f.read(), "already published")

    def test_persists_site_type_and_onionname(self):
        setup_logic.provision_static_site(
            onionname="alice",
            documents_dir=os.path.join(self.tmpdir, "OnionPress"),
            data_dir=self.tmpdir,
        )
        with open(os.path.join(self.tmpdir, "config")) as f:
            contents = f.read()
        self.assertIn("SITE_TYPE=static", contents)
        self.assertIn("ONIONNAME=alice", contents)


class TestProvisionInteractiveStaticRouting(unittest.TestCase):
    """Choosing "static" at the first prompt must route to
    provision_static_site() and skip every WordPress-specific step
    entirely (no wp_is_installed probe, no onion-address read)."""

    def test_routes_to_static_site_and_skips_wordpress(self):
        with mock.patch(
            "builtins.input", side_effect=["static", "alice"],
        ), mock.patch(
            "onionpress.setup_logic._wp_is_installed",
        ) as m_wp_installed, mock.patch(
            "onionpress.setup_logic.install_fresh_wordpress",
        ) as m_fresh, mock.patch(
            "onionpress.setup_logic.provision_existing_wordpress",
        ) as m_existing, mock.patch(
            "onionpress.setup_logic.provision_static_site",
            return_value=True,
        ) as m_static:
            ok = setup_logic.provision_interactive()
        self.assertTrue(ok)
        m_static.assert_called_once()
        self.assertEqual(m_static.call_args.kwargs["onionname"], "alice")
        m_wp_installed.assert_not_called()
        m_fresh.assert_not_called()
        m_existing.assert_not_called()


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


class TestLinuxSetupStaticFlagParsed(unittest.TestCase):
    """Invariant: `onionpress setup --static` must actually be parsed.

    The usage comment has always documented `onionpress setup --static
    [--user ONIONNAME]`, but on a FRESH install ONIONPRESS_SITE_TYPE (read
    from config at script startup) can't see a flag on the very invocation
    that's supposed to set SITE_TYPE=static — a bug that shipped once
    already (the static branch was gated purely on that env var, so
    --static silently fell through to the WordPress path with the flag
    left as an unrecognized argument). Assert the setup) case actually
    scans its own "$@" for --static, and that the scan happens BEFORE the
    branch it's meant to unlock.
    """

    def test_setup_case_scans_args_for_static_before_branching(self):
        import re

        full = os.path.join(os.path.dirname(__file__), "..", "linux", "onionpress")
        with open(full) as f:
            lines = f.readlines()

        # Top-level case arms in this script are consistently `        name)`
        # on their own line (8-space indent, nothing else) — distinct from
        # the nested `--user)`/`*)` arms inside setup)'s own arg-parsing
        # loop, which are indented further and never match this alone.
        arm_re = re.compile(r"^        [a-zA-Z_-]+\)\s*$")
        setup_idx = next(
            (i for i, l in enumerate(lines) if l.rstrip() == "        setup)"), None)
        self.assertIsNotNone(setup_idx, "linux/onionpress is missing the "
                              "`setup)` case entirely")

        next_arm_idx = next(
            (i for i in range(setup_idx + 1, len(lines)) if arm_re.match(lines[i])),
            len(lines))
        block_text = "".join(lines[setup_idx + 1:next_arm_idx])

        scan_idx = block_text.find('"$_setup_arg" = "--static"')
        gate_idx = block_text.find('"$ONIONPRESS_SITE_TYPE" = "static"')
        self.assertNotEqual(scan_idx, -1,
            "linux/onionpress's `setup)` case doesn't scan its own "
            "arguments for --static — a fresh `onionpress setup --static` "
            "would silently run the WordPress path instead")
        self.assertNotEqual(gate_idx, -1,
            "linux/onionpress's `setup)` case lost its static-mode branch")
        self.assertLess(scan_idx, gate_idx,
            "the --static arg scan must run BEFORE the branch that checks "
            "ONIONPRESS_SITE_TYPE, or the flag arrives too late to matter")


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


if __name__ == "__main__":
    unittest.main()

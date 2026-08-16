#!/usr/bin/env python3
"""Static source-level invariants that guard against install-flow regressions
we've hit before.

These are text/AST checks, not behavioral tests — they can't prove the
install flow works, only that the specific call sequences that went wrong
in past install tests are still wired up the way they should be. Each check
below has a comment pointing at the incident it's guarding against.
"""

import ast
import os
import re
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _read(rel_path):
    with open(os.path.join(PROJECT_ROOT, rel_path), "r", encoding="utf-8") as f:
        return f.read()


class TestMacOSBuildBundlesKeyManager(unittest.TestCase):
    """The macOS shell launcher invokes $SCRIPTS_DIR/key_manager.py directly
    (app/MacOS/onionpress:504,638,702). If that file isn't bundled, vanity
    generation silently falls back to a random address AND OnionHeaven
    registration hits "sign_failed" forever — both observed on an install
    test where build-dmg-simple.sh copied menubar.py but not key_manager.py.
    """

    def test_build_script_copies_key_manager_into_scripts_dir(self):
        script = _read("build/build-dmg-simple.sh")
        self.assertRegex(
            script,
            r'cp\s+"\$PROJECT_DIR/src/onionpress/key_manager\.py"\s+'
            r'"\$APP_PATH/Contents/Resources/scripts/"',
            "build-dmg-simple.sh must copy src/onionpress/key_manager.py "
            "into Contents/Resources/scripts/ — the macOS shell launcher "
            "invokes it from there.",
        )

    def test_build_script_verifies_file_not_substring(self):
        """The old check was `grep -rq "key_manager" MenubarApp` — that
        passes even when the actual file the shell launcher needs is
        missing, because the string appears in py2app's bundled bytecode.
        The real check has to be `test -f` against the launcher's path
        and must exit non-zero on failure.
        """
        script = _read("build/build-dmg-simple.sh")
        self.assertNotRegex(
            script,
            r'grep\s+-rq\s+"key_manager"',
            "Substring grep can pass when the file is missing. Use "
            "`test -f` against Contents/Resources/scripts/key_manager.py.",
        )
        self.assertRegex(
            script,
            r'\[\s*-f\s+"\$APP_PATH/Contents/Resources/scripts/key_manager\.py"\s*\]',
            "Need a `test -f` check for the bundled key_manager.py.",
        )
        # The verification must fail the build, not just log a warning.
        # Look for an `exit 1` inside the key_manager check's else-branch.
        km_block = re.search(
            r'if\s+\[\s*-f\s+"\$APP_PATH/Contents/Resources/scripts/key_manager\.py"\s*\];\s*then'
            r'(.*?)fi',
            script,
            re.DOTALL,
        )
        self.assertIsNotNone(km_block, "Could not find key_manager.py test -f block")
        self.assertIn(
            "exit 1",
            km_block.group(1),
            "The key_manager.py check must `exit 1` on miss, not just warn — "
            "otherwise broken bundles ship.",
        )


class TestMacOSBuildBundlesMkp224o(unittest.TestCase):
    """mkp224o must be bundled at Contents/Resources/bin/mkp224o. On first run
    the macOS shell launcher's generate_vanity_address() (app/MacOS/onionpress)
    execs $BIN_DIR/mkp224o to mint the vanity (op2…) address. If it's missing,
    vanity generation fails and the install SILENTLY falls back to a random
    .onion — shipped in 2.4.101 and hit by multiple users on fresh install.

    The build builds mkp224o in an isolated subshell (so a flaky libsodium
    cross-compile won't abort the DMG), which means the only thing standing
    between a failed mkp224o build and a broken release is the copy step
    below failing hard. These checks guard that it stays a hard failure.
    """

    def test_build_script_copies_mkp224o_into_bin_dir(self):
        script = _read("build/build-dmg-simple.sh")
        self.assertRegex(
            script,
            r'cp\s+"\$TEMP_BIN_DIR/mkp224o"\s+"\$BIN_DIR/mkp224o"',
            "build-dmg-simple.sh must copy mkp224o into Contents/Resources/bin/ "
            "— the macOS shell launcher execs it from there for vanity addresses.",
        )

    def test_missing_mkp224o_aborts_the_build(self):
        """A missing mkp224o must `exit 1`, not just `WARNING`. The original
        bug was an `else echo "WARNING: mkp224o not available"` that let the
        DMG ship without the binary, so every fresh install got a random
        .onion.
        """
        script = _read("build/build-dmg-simple.sh")
        block = re.search(
            r'if\s+\[\s*-f\s+"\$TEMP_BIN_DIR/mkp224o"\s*\];\s*then(.*?)fi',
            script,
            re.DOTALL,
        )
        self.assertIsNotNone(
            block,
            "Could not find the mkp224o install block in build-dmg-simple.sh "
            "— has it been renamed? Update this test.",
        )
        self.assertIn(
            "exit 1",
            block.group(1),
            "The mkp224o install block must `exit 1` when the binary is "
            "missing — a warning lets a vanity-less DMG ship (the 2.4.101 bug).",
        )

    def test_validate_bundle_requires_mkp224o(self):
        """validate-bundle.sh's required universal-binaries list must keep
        mkp224o, so any bundle check (CI or manual) catches its absence.
        """
        script = _read("build/validate-bundle.sh")
        self.assertRegex(
            script,
            r'UNIVERSAL_BINARIES=\([^)]*\bmkp224o\b[^)]*\)',
            "validate-bundle.sh must list mkp224o among the required universal "
            "binaries it verifies are present in the bundle.",
        )


class TestTorImplDefaultsToCTor(unittest.TestCase):
    """C Tor (TOR_IMPL=tor) has been the default since 2026-03-16. But the
    CLI-rewrite foundation (commit c15d8dd9, 2026-03-20) introduced
    read_value(..., "TOR_IMPL", "arti") in containers.py and cli.py — so on
    a fresh install with no TOR_IMPL in config (the normal case, since the
    value only gets written when it's non-default), those paths brought the
    stack up as Arti and the menubar settings window showed "arti". Every
    TOR_IMPL default must be "tor" to match config.py DEFAULTS, the bash
    launcher, settings_ui, and menubar.py.
    """

    def test_no_tor_impl_default_is_arti_or_unknown(self):
        import glob
        offenders = []
        pat = re.compile(r'TOR_IMPL"\s*,\s*"(arti|unknown)"')
        py_files = glob.glob(os.path.join(PROJECT_ROOT, "src", "**", "*.py"),
                             recursive=True)
        for path in py_files:
            with open(path, "r", encoding="utf-8") as f:
                for lineno, line in enumerate(f, 1):
                    if pat.search(line):
                        rel = os.path.relpath(path, PROJECT_ROOT)
                        offenders.append(f"{rel}:{lineno}: {line.strip()}")
        self.assertEqual(
            offenders, [],
            "TOR_IMPL must default to \"tor\" (C Tor) everywhere. Found "
            "non-tor defaults:\n" + "\n".join(offenders),
        )

    def test_config_defaults_tor_impl_is_tor(self):
        cfg = _read("src/onionpress/config.py")
        self.assertRegex(
            cfg,
            r'"TOR_IMPL":\s*"tor"',
            "config.py DEFAULTS must keep TOR_IMPL = \"tor\".",
        )


class TestMakefilePrecheckUsesCorrectPath(unittest.TestCase):
    """The Makefile's `make test` target asserts required source files
    exist before a build. After the move to the `onionpress` package the
    precheck still pointed at the pre-move path `src/key_manager.py`, so
    it couldn't catch a missing file — confirmed by the bug that this
    test suite exists to guard against.
    """

    def test_precheck_points_at_package_path(self):
        mk = _read("Makefile")
        self.assertRegex(
            mk,
            r'test\s+-f\s+src/onionpress/key_manager\.py',
            "Makefile `test` target must precheck the real source path "
            "src/onionpress/key_manager.py.",
        )
        self.assertNotRegex(
            mk,
            r'test\s+-f\s+src/key_manager\.py\b',
            "Stale flat-path check for src/key_manager.py — file was moved "
            "into src/onionpress/ and this check silently passes.",
        )


class TestSubsiteGetsOnionPressTheme(unittest.TestCase):
    """RECURRING regression: after `wp site create` creates the primary
    subsite at /<onionname>/, the new subsite gets WordPress's default
    theme (twentytwentyfive), not OnionPress. The earlier
    install_onionpress_theme() shell helper only activates on blog_id=1,
    so without an explicit per-subsite activation users see the onionpress
    theme flash briefly and then — once the subsite is created and
    onionpress-root-redirect.php starts bouncing / → /<onionname>/ — the
    default theme takes over.

    The logic now lives in setup_logic.provision_primary_subsite() which is
    shared between Mac and Linux. This test parses that function's AST and
    asserts both calls are present.
    """

    def setUp(self):
        src = _read("src/onionpress/setup_logic.py")
        tree = ast.parse(src)
        self.func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "provision_primary_subsite":
                self.func = node
                break
        self.assertIsNotNone(
            self.func,
            "Could not find provision_primary_subsite in "
            "src/onionpress/setup_logic.py — has it been renamed? Update this test.",
        )

    def _subprocess_run_calls(self):
        """Return every wp-cli call inside the function.

        Handles two call patterns:
          1. subprocess.run(["docker", "exec", ..., "wp", ...], ...)
             — used by old menubar.py inline style
          2. wp("site", "create", ...)
             — used by setup_logic.py's inner wp() helper
        Both are normalised to a flat list of string tokens so the
        same is_wp_cli() checks work for both patterns.
        """
        def _extract_str_args(nodes):
            result = []
            for elt in nodes:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    result.append(elt.value)
                elif isinstance(elt, ast.JoinedStr):  # f-string
                    pieces = []
                    for v in elt.values:
                        if isinstance(v, ast.Constant):
                            pieces.append(str(v.value))
                        elif isinstance(v, ast.FormattedValue):
                            pieces.append("{__fmt__}")
                    result.append("".join(pieces))
            return result

        calls = []
        for node in ast.walk(self.func):
            if not isinstance(node, ast.Call):
                continue
            func = node.func

            # Pattern 1: subprocess.run(["docker", "exec", ...], ...)
            if isinstance(func, ast.Attribute) and func.attr == "run" and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.List):
                    calls.append(_extract_str_args(arg.elts))

            # Pattern 2: wp("site", "create", ...) — inner helper used by
            # setup_logic.provision_primary_subsite(). Prepend a canonical
            # docker-exec prefix so is_wp_cli() matches the same way.
            elif isinstance(func, ast.Name) and func.id == "wp" and node.args:
                tokens = _extract_str_args(node.args)
                if tokens:
                    calls.append(
                        ["docker", "exec", "onionpress-wordpress",
                         "wp", "--allow-root"] + tokens
                    )

        return calls

    def test_creates_subsite_and_activates_theme(self):
        calls = self._subprocess_run_calls()

        def is_wp_cli(argv, *tokens):
            joined = " ".join(argv)
            return all(t in joined for t in ("wp",) + tokens)

        create_idx = None
        activate_idx = None
        for i, argv in enumerate(calls):
            if is_wp_cli(argv, "site", "create"):
                create_idx = i
            if is_wp_cli(argv, "theme", "activate", "onionpress"):
                # Must be keyed on the subsite URL, not blog_id=1
                joined = " ".join(argv)
                if "http://localhost/{__fmt__}/" in joined or re.search(
                    r"http://localhost/\{?onionname\}?/", joined
                ):
                    activate_idx = i

        self.assertIsNotNone(
            create_idx, "_provision_primary_subsite must call `wp site create`."
        )
        self.assertIsNotNone(
            activate_idx,
            "_provision_primary_subsite must call "
            "`wp theme activate onionpress --url=http://localhost/<onionname>/` "
            "after creating the subsite — otherwise the subsite keeps the "
            "WP default theme (twentytwentyfive) and the user never sees "
            "OnionPress, because root-redirect.php bounces / → /<onionname>/ "
            "once the subsite exists.",
        )
        self.assertGreater(
            activate_idx, create_idx,
            "theme activate must come AFTER wp site create — the subsite "
            "has to exist before we can activate a theme on it.",
        )


class TestSubsiteSetsPrimaryBlog(unittest.TestCase):
    """Second half of the subsite regression: even after the role and
    theme are set on the new subsite, users still hit blog_id=1's admin
    from "+ New", "My Sites", and wp-admin redirects until their
    primary_blog usermeta points at the subsite. Observed on a fresh
    install where `+ New → Post` routed to http://<onion>/wp-admin/
    instead of http://<onion>/<onionname>/wp-admin/.

    The logic now lives in setup_logic.provision_primary_subsite().
    """

    def test_primary_blog_is_set_on_new_subsite(self):
        src = _read("src/onionpress/setup_logic.py")
        import ast
        tree = ast.parse(src)
        func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "provision_primary_subsite":
                func = node
                break
        self.assertIsNotNone(func)

        body = ast.unparse(func)
        self.assertIn("primary_blog", body,
            "_provision_primary_subsite must update the user's primary_blog "
            "meta to point at the newly-created subsite. Without this, "
            'wp-admin\'s "+ New" still routes to blog_id=1 (network root) '
            "and users end up posting on the wrong site.")
        self.assertIn("'user', 'meta', 'update'", body,
            "primary_blog should be set via `wp user meta update`.")
        # The update must reference the user (onionname) and a resolved
        # blog_id — guard against a hardcoded `1` or `2` accidentally
        # shipping.
        self.assertIn("get_blog_id_from_url", body,
            "Resolve the subsite's blog_id dynamically (the onionname "
            "varies per install) rather than hardcoding a literal.")


class TestLinuxInstallTray(unittest.TestCase):
    """Guard against regressions in the Linux install tray setup.

    Incident: after a reinstall the tray icon never appeared because
    install.sh (a) had no symlink for onionpress-tray, (b) never wrote
    an autostart .desktop file, and (c) never launched the tray process.
    All three were added together; these checks ensure they stay wired up.
    """

    def setUp(self):
        self.script = _read("linux/install.sh")

    def test_tray_binary_copied_to_install_dir(self):
        self.assertRegex(
            self.script,
            r'cp\s+.*linux/onionpress-tray.*INSTALL_DIR',
            "install.sh must copy linux/onionpress-tray into INSTALL_DIR — "
            "without it the symlink and tray launch both fail silently.",
        )

    def test_tray_symlink_created(self):
        self.assertRegex(
            self.script,
            r'ln\s+-sf\s+["\$\w/]*onionpress-tray["\s]+/usr/local/bin/onionpress-tray',
            "install.sh must symlink onionpress-tray into /usr/local/bin/ — "
            "without it the autostart .desktop Exec= path doesn't resolve.",
        )

    def test_usr_local_bin_created_before_symlink(self):
        """Guard against the install completing but `onionpress` being absent
        from PATH because /usr/local/bin didn't exist on a minimal image and
        the original `ln -sf … 2>/dev/null || true` swallowed the failure.

        The fix: mkdir -p /usr/local/bin while sudo is held, and remove the
        error-suppressing `|| true` so a genuine symlink failure aborts the
        install instead of producing a half-working state.
        """
        # mkdir must appear before the symlink lines. Match the actual
        # `ln -sf … /usr/local/bin/onionpress…` command, not just any
        # mention of the path (comments referring to the symlink target
        # earlier in the file would otherwise pass-through-pollute this).
        import re
        mkdir_pos = self.script.find("mkdir -p /usr/local/bin")
        ln_match = re.search(
            r'ln\s+-sf\s+[^\n]*\s/usr/local/bin/onionpress', self.script)
        self.assertGreaterEqual(
            mkdir_pos, 0,
            "install.sh must `mkdir -p /usr/local/bin` — that dir doesn't "
            "exist on minimal Ubuntu/Debian images.",
        )
        self.assertIsNotNone(
            ln_match,
            "install.sh must `ln -sf … /usr/local/bin/onionpress`.",
        )
        self.assertLess(
            mkdir_pos, ln_match.start(),
            "`mkdir -p /usr/local/bin` must come BEFORE the `ln -sf` lines.",
        )
        # The symlink lines must NOT swallow errors any more — otherwise a
        # real failure (filesystem read-only, etc.) silently produces a
        # broken install.
        self.assertNotRegex(
            self.script,
            r'ln\s+-sf\s+[^\n]*/usr/local/bin/onionpress[^\n]*\|\|\s*true',
            "Symlink command must NOT end with `|| true` — that was the bug "
            "that hid the missing /usr/local/bin and produced an install "
            "with no `onionpress` on PATH.",
        )

    def test_autostart_desktop_file_written(self):
        self.assertIn(
            "onionpress-tray.desktop",
            self.script,
            "install.sh must write ~/.config/autostart/onionpress-tray.desktop "
            "so the tray relaunches on every login.",
        )
        self.assertRegex(
            self.script,
            r'AUTOSTART_DIR.*\.config/autostart',
            "install.sh must place the autostart file under ~/.config/autostart/.",
        )

    def test_autostart_desktop_exec_points_at_tray(self):
        # The heredoc block must contain an Exec= pointing at the tray binary.
        self.assertIsNotNone(
            re.search(r'cat\s*>.*onionpress-tray\.desktop.*<<TRAY_EOF', self.script),
            "install.sh must write onionpress-tray.desktop via a heredoc.",
        )
        self.assertRegex(
            self.script,
            r'Exec=.*onionpress-tray',
            "The autostart .desktop Exec= must point at the onionpress-tray binary.",
        )

    def test_tray_launched_on_gui_install(self):
        # The display guard and tray launch may be on separate lines, so use
        # re.DOTALL explicitly rather than assertRegex (which doesn't set it).
        self.assertIsNotNone(
            re.search(
                r'DISPLAY.*WAYLAND_DISPLAY.*onionpress-tray',
                self.script,
                re.DOTALL,
            ),
            "install.sh must launch onionpress-tray immediately when a "
            "display is present — otherwise the icon only appears after the "
            "next login.",
        )

    def test_stale_tray_killed_before_relaunch(self):
        self.assertRegex(
            self.script,
            r'pkill.*onionpress-tray',
            "install.sh must kill any running tray before relaunching — "
            "on reinstall the old process would otherwise persist alongside "
            "the new one.",
        )


class TestLinuxSymlinkBeforeTrayLaunch(unittest.TestCase):
    """Guard against the SetupDialog → provision-post-install race.

    Incident: install.sh launched the tray (which exposes a Setup dialog
    that the user can click into immediately) before it created the
    /usr/local/bin/onionpress symlink. A fast user could complete Setup
    while the symlink still didn't exist; setup_logic.install_fresh_
    wordpress then ran `subprocess.run(["/usr/local/bin/onionpress",
    "provision-post-install"])`, which raised FileNotFoundError. The
    error got swallowed into the dialog's log buffer (not onionpress.log),
    install_fresh_wordpress still returned True, and WP came up themeless
    and single-site. Pin the order so this can't regress.
    """

    def test_symlink_created_before_tray_launch(self):
        script = _read("linux/install.sh")
        ln_match = re.search(
            r'ln\s+-sf\s+[^\n]*\s/usr/local/bin/onionpress\b', script)
        tray_launch = re.search(
            r'run_as_user\s+["\$\w/]*onionpress-tray', script)
        self.assertIsNotNone(
            ln_match,
            "install.sh must symlink onionpress into /usr/local/bin/.",
        )
        self.assertIsNotNone(
            tray_launch,
            "install.sh must launch the tray via run_as_user.",
        )
        self.assertLess(
            ln_match.start(), tray_launch.start(),
            "/usr/local/bin/onionpress symlink must be created BEFORE the "
            "tray is launched — SetupDialog shells out to that path to "
            "install the theme + convert to multisite. If the symlink "
            "isn't there yet and the user clicks Setup fast, the post-"
            "install step silently fails and WP comes up themeless.",
        )


class TestLinuxProvisionPostInstallOrder(unittest.TestCase):
    """Guard the ensure_multisite → install_multisite_domain_map order.

    Incident: provision-post-install ran install_multisite_domain_map
    (which sets SUNRISE=true and drops sunrise.php) BEFORE ensure_multisite
    (which runs `wp core multisite-convert`). sunrise.php queries wp_site
    on every WP load; with the constant set but the table missing, every
    subsequent wp-cli call errored, ensure_multisite's wp_is_installed
    guard returned false, and install_onionpress_theme silently skipped.
    The Mac launcher has the correct order; the Linux one didn't.
    """

    def _ordered_calls(self, body):
        # Position of the first ensure_multisite vs install_multisite_domain_map
        # call inside the given body of bash. Skip comments.
        em = re.search(r'^\s*ensure_multisite\b', body, re.MULTILINE)
        mdm = re.search(r'^\s*install_multisite_domain_map\b', body, re.MULTILINE)
        theme = re.search(r'^\s*install_onionpress_theme\b', body, re.MULTILINE)
        return em, mdm, theme

    def test_provision_post_install_subcommand_delegates_to_python(self):
        """The bash provision-post-install case used to inline all the
        step calls; it now delegates to src/onionpress/multisite.py via
        the python CLI. The step-ordering invariant lives in
        tests/test_multisite.py:test_provision_runs_steps_in_order.
        This test just verifies the delegation is wired up correctly
        on both platforms.
        """
        for path, py_cmd in (
            ("linux/onionpress", r'op_py\s+provision-post-install'),
            ("app/MacOS/onionpress",
             r'bundled_python\s+-m\s+onionpress\.cli\s+provision-post-install'),
        ):
            script = _read(path)
            m = re.search(
                r'provision-post-install\)(.*?);;', script, re.DOTALL)
            self.assertIsNotNone(
                m, f"provision-post-install case must exist in {path}")
            self.assertRegex(
                m.group(1), py_cmd,
                f"{path}: provision-post-install must delegate to the "
                f"Python module (multisite.provision_post_install) — "
                f"the step ordering invariant now lives there, not "
                f"in two parallel bash copies that can silently drift.",
            )
            self.assertRegex(
                m.group(1), r'--themes-dir',
                f"{path}: must pass --themes-dir to the python CLI",
            )
            self.assertRegex(
                m.group(1), r'--plugins-dir',
                f"{path}: must pass --plugins-dir to the python CLI",
            )

    def test_start_containers_delegates_wp_provisioning_to_python(self):
        """start_containers used to inline 5 bash function calls
        (ensure_multisite, install_multisite_domain_map, install_onionpress_
        theme, fix_onionpress_permissions, fix_wordpress_uploads_permissions).
        It now delegates to the Python module via op_py / bundled_python.
        Step ordering is enforced inside multisite.provision_post_install
        — see tests/test_multisite.py.
        """
        for path, py_cmd in (
            ("linux/onionpress", r'op_py\s+provision-post-install'),
            ("app/MacOS/onionpress",
             r'bundled_python\s+-m\s+onionpress\.cli\s+provision-post-install'),
        ):
            script = _read(path)
            m = re.search(
                r'start_containers\(\)\s*\{(.*?)^\}', script,
                re.DOTALL | re.MULTILINE)
            self.assertIsNotNone(
                m, f"start_containers must exist in {path}")
            self.assertRegex(
                m.group(1), py_cmd,
                f"{path}: start_containers must delegate WP provisioning to "
                "the Python module — keeps Mac and Linux in sync.",
            )


class TestLinuxSleepHookWiring(unittest.TestCase):
    """Guard the suspend/resume → OnionHeaven notification path.

    Without this wiring, a laptop close-lid leaves a stale onion descriptor
    on the DHT for minutes (until the hub's missed-heartbeat timeout fires)
    and resume takes a full 60s heartbeat tick before the hub stops fronting
    our site via Wayback fallback. Mac fires `notify_onionheaven_offline()`
    in `handle_sleep`; Linux mirrors that via a systemd system-sleep hook.
    """

    def test_sleep_hook_script_exists(self):
        hook = _read("linux/onionpress-sleep-hook")
        self.assertRegex(
            hook, r'#!/bin/bash', "Hook must be a bash script.")
        # Hook must call the launcher with sleep-pre and sleep-post.
        self.assertRegex(
            hook, r'sleep-\$ACTION',
            "Hook must invoke /opt/onionpress/onionpress sleep-$ACTION so "
            "pre and post both reach the launcher subcommand.",
        )
        # Bound to systemd's pre/post + sleep/hibernate contract.
        self.assertIn(
            "suspend|hibernate|hybrid-sleep|suspend-then-hibernate", hook,
            "Hook must cover every sleep type systemd-logind dispatches.",
        )
        # Per-user timeout so a stuck Docker can't hold up suspend.
        self.assertRegex(
            hook, r'timeout\s+\d+',
            "Hook must wrap the per-user launcher invocation in `timeout` "
            "so a hung Docker can't block the suspend.",
        )

    def test_install_sh_installs_sleep_hook(self):
        script = _read("linux/install.sh")
        # The install command can wrap across lines via `\` continuation,
        # so match with DOTALL and `.` allowed to span newlines.
        self.assertRegex(
            script,
            r'install\s+-m\s+0755[\s\S]*?onionpress-sleep-hook[\s\S]*?'
            r'/usr/lib/systemd/system-sleep/onionpress',
            "install.sh must drop linux/onionpress-sleep-hook into "
            "/usr/lib/systemd/system-sleep/onionpress (mode 0755).",
        )

    def test_deb_packages_sleep_hook(self):
        script = _read("build/build-linux.sh")
        self.assertIn(
            "/usr/lib/systemd/system-sleep", script,
            "build-linux.sh must stage the sleep hook into the .deb's "
            "/usr/lib/systemd/system-sleep/ directory.",
        )
        self.assertRegex(
            script, r'onionpress-sleep-hook',
            "build-linux.sh must reference linux/onionpress-sleep-hook.",
        )

    def test_launcher_has_sleep_pre_and_post_subcommands(self):
        script = _read("linux/onionpress")
        self.assertRegex(
            script, r'sleep-pre\|sleep-post\)',
            "linux/onionpress must dispatch a sleep-pre|sleep-post case so "
            "the system-sleep hook has something to call.",
        )
        # Both signals must reach the heartbeat client (USR1 = /offline,
        # USR2 = immediate /online).
        self.assertIn(
            "onionpress-heartbeat", script,
            "Sleep subcommand must kill -s SIGUSR{1,2} the heartbeat unit.",
        )

    def test_heartbeat_handles_usr1_and_usr2(self):
        src = _read("linux/onionpress-heartbeat.py")
        self.assertRegex(
            src, r'signal\.signal\(signal\.SIGUSR1',
            "Heartbeat client must register a SIGUSR1 handler — sleep-pre "
            "uses it to flush /offline before suspend.",
        )
        self.assertRegex(
            src, r'signal\.signal\(signal\.SIGUSR2',
            "Heartbeat client must register a SIGUSR2 handler — sleep-post "
            "uses it to send an immediate /online after resume.",
        )
        # The flag-based pattern: handler sets a global, main loop reacts.
        # Direct send_offline() inside a signal handler would be unsafe.
        self.assertRegex(
            src, r'_pending_offline\s*=\s*True',
            "USR1 handler should flip a flag and let the main loop send "
            "/offline — network I/O inside a signal handler is unsafe.",
        )
        self.assertRegex(
            src, r'_pending_online\s*=\s*True',
            "USR2 handler should flip a flag for the same reason.",
        )


class TestLinuxWaitThenOpenUsesWrapper(unittest.TestCase):
    """Guard against the firefox.real-direct regression.

    Incident: _wait_then_open spawned `firefox.real -new-tab URL` directly,
    but Tor Browser's remoting only works via the start-tor-browser wrapper
    (which sets HOME/cwd/env so the running profile is found). Without the
    wrapper, -new-tab spawns a fresh process, hits the profile lock, and
    Tor Browser pops its native "Tor Browser is already running" dialog
    instead of opening the URL in a new tab. The wrapper path is already
    the canonical pattern in src/onionpress/launcher_ops.py's
    open_in_browser(); _wait_then_open just copy-pasted the wrong binary.
    """

    def test_wait_then_open_prefers_start_tor_browser_wrapper(self):
        src = _read("linux/onionpress-tray")
        # Carve the _wait_then_open function body so we don't accidentally
        # match an unrelated `firefox.real` reference elsewhere in the file.
        m = re.search(
            r'def\s+_wait_then_open\(\)[^\n]*:(.*?)(?=\n\s{0,8}def\s+\w|\Z)',
            src, re.DOTALL)
        self.assertIsNotNone(
            m, "_wait_then_open helper must exist in linux/onionpress-tray")
        body = m.group(1)
        # Wrapper path must be referenced.
        self.assertRegex(
            body, r'start-tor-browser',
            "_wait_then_open must launch the URL via the start-tor-browser "
            "wrapper. Spawning firefox.real with -new-tab directly hits "
            "the profile lock and pops Tor Browser's native 'already "
            "running' dialog instead of opening the URL in a new tab.",
        )


class TestLinuxOnionAddressSharedToWp(unittest.TestCase):
    """Guard against the missing @onion-host on the theme header.

    Incident: when the site is hit via localhost:18080 (the menu's "Open
    Local Site"), the theme's onionpress_follow_get_own_address() can't
    read the .onion from the Host header — it falls back to reading
    /var/lib/onionpress/onion_address inside the WP container. That file
    is mounted from the shared volume; it's written by the launcher.
    The bash launcher only wrote it on the both-services-ready code path
    in wait_for_services, which bails early when WP isn't yet installed.
    Result: on fresh installs the file was never written, the theme
    header showed "ubuntupress" without "@op2…onion".
    """

    def test_launcher_defines_write_shared_onion_address(self):
        script = _read("linux/onionpress")
        self.assertRegex(
            script, r'write_shared_onion_address\(\)\s*\{',
            "linux/onionpress must define a write_shared_onion_address "
            "helper that copies hostname into the shared volume.",
        )

    def test_provision_post_install_writes_shared_address(self):
        # provision-post-install delegates to multisite.provision_post_install
        # in Python; that function must call write_shared_onion_address as
        # the last step. The bash case body is now just an op_py call so
        # we read the Python module directly.
        py = _read("src/onionpress/multisite.py")
        m = re.search(
            r'def\s+provision_post_install\([^)]*\)[^:]*:(.*?)(?=^def\s|\Z)',
            py, re.DOTALL | re.MULTILINE)
        self.assertIsNotNone(
            m, "provision_post_install must exist in src/onionpress/multisite.py")
        self.assertIn(
            "write_shared_onion_address", m.group(1),
            "provision_post_install must call write_shared_onion_address — "
            "this is the post-Setup belt-and-braces for the WP theme's "
            "header on localhost.",
        )


class TestLinuxReinstallBrowserOpen(unittest.TestCase):
    """Guard the fix for browser not auto-opening after reinstall.

    Incident: on reinstall the launcher imports the existing onion key and
    writes ~/.onionpress/onion_address before Python starts, so
    _had_cached_address=True even on a first run.  The old guard
    `auto_opened_browser = self._had_cached_address` then silently skipped
    the browser open.  The fix ANDs in `not self._is_first_run` so a first
    run always opens the browser regardless of a pre-written address file.
    """

    def test_auto_opened_browser_respects_first_run(self):
        src = _read("src/menubar.py")
        # The assignment must include the _is_first_run guard.
        self.assertRegex(
            src,
            r'self\.auto_opened_browser\s*=\s*self\._had_cached_address\s+and\s+not\s+self\._is_first_run',
            "auto_opened_browser must be False when _is_first_run is True, "
            "regardless of _had_cached_address — a reinstall imports the "
            "existing key before Python starts, making _had_cached_address "
            "True even on a fresh install, which suppressed the browser open.",
        )


# ─────────────────────────────────────────────────────────────────────
# "Cheap" static invariants added after a real uninstall→reinstall test
# exposed several Linux-only regressions that Mac had silently solved
# years ago. The pattern across them: a Mac function never got ported,
# OR a path-specific helper (uninstall, stop, scrub) skipped a cleanup
# step that the equivalent .deb prerm or Mac menubar already does.
# These tests are the cheap text/AST kind — no docker, no containers,
# just grep against the launcher scripts.
# ─────────────────────────────────────────────────────────────────────


class TestMacLinuxFunctionParity(unittest.TestCase):
    """Functions present in the Mac launcher must also exist in the Linux
    launcher (with a small allowlist for genuinely platform-specific code).
    Catches the "ported on Mac, forgotten on Linux" class of bug.

    Incident: `ensure_archive_s3_keys` lived on Mac for a long time and
    was never ported to Linux. Wayback Machine archiving silently never
    worked on Linux installs because the auth header stayed empty, so
    the sweep loop found posts and submitted zero of them forever.
    """

    # Functions that should exist on BOTH bash launchers. Most of the
    # WP-provisioning helpers (ensure_multisite, install_onionpress_theme,
    # etc.) USED to be here but have been ported to
    # src/onionpress/multisite.py — see TestMultisiteModuleExports below
    # for the equivalent assertion at the Python level. What stays bash:
    # install_ia_plugin (shells out to install_plugin.sh + activate_
    # plugin.sh helpers; bigger lift to port) and detect_port_offset
    # (needs to run before the Python lib path is set up).
    REQUIRED_ON_BOTH = {
        "install_ia_plugin",
        "detect_port_offset",
    }
    # `fetch_archive_s3_keys` exists only on Linux: it's a helper used by
    # the SSH/CLI `onionpress setup` interactive path, which on Mac is
    # handled by the AppKit setup window. Same shape of work, different
    # call site — not a parity bug.

    def _fns(self, path):
        return set(re.findall(r"^([a-z_][a-z_0-9]*)\(\)", _read(path), re.MULTILINE))

    def test_shared_functions_exist_on_both(self):
        mac = self._fns("app/MacOS/onionpress")
        linux = self._fns("linux/onionpress")
        missing_mac = self.REQUIRED_ON_BOTH - mac
        missing_linux = self.REQUIRED_ON_BOTH - linux
        self.assertFalse(
            missing_mac,
            f"Required functions missing from app/MacOS/onionpress: {missing_mac}",
        )
        self.assertFalse(
            missing_linux,
            f"Required functions missing from linux/onionpress: {missing_linux} "
            "— port from Mac before shipping. The wayback bug was caused by "
            "exactly this: ensure_archive_s3_keys existed on Mac for a long "
            "time and was never ported, so fresh Linux installs never got "
            "Archive.org credentials.",
        )


class TestMultisiteModuleExports(unittest.TestCase):
    """The WP-provisioning helpers used to live as parallel bash
    implementations in app/MacOS/onionpress and linux/onionpress.
    They've moved to src/onionpress/multisite.py; this test asserts the
    module exports the expected functions so the bash launchers don't
    have to (the bash side calls them via op_py / bundled_python).

    Pairs with TestMacLinuxFunctionParity above: anything we removed
    from the REQUIRED_ON_BOTH set there should appear here instead.
    """

    REQUIRED_PY_FUNCTIONS = {
        "ensure_multisite",
        "install_multisite_domain_map",
        "install_onionpress_theme",
        "fix_onionpress_permissions",
        "fix_wordpress_uploads_permissions",
        "write_shared_onion_address",
        "configure_ia_plugin",
        "deactivate_wp_statistics",
        "ensure_archive_s3_keys",
        "provision_post_install",  # orchestrator
    }

    def test_multisite_module_exports(self):
        from onionpress import multisite
        actual = {name for name in dir(multisite)
                  if not name.startswith("_") and callable(getattr(multisite, name))}
        missing = self.REQUIRED_PY_FUNCTIONS - actual
        self.assertFalse(
            missing,
            f"Required functions missing from src/onionpress/multisite.py: "
            f"{missing}. Either re-add them or update REQUIRED_PY_FUNCTIONS "
            f"(but each removed function needs new callers wired up).",
        )


class TestScrubPreAuthsSudo(unittest.TestCase):
    """The scrub flow does sudo work AFTER a 1-2 minute backup step.
    Sudo's password cache will have expired by then, so any sudo call
    in the uninstall section will block on a prompt nobody is watching,
    time out, and leave the install half-done (units removed, volumes
    gone, but /opt/onionpress and ~/.onionpress untouched).

    Incident: a real scrub run died with `sudo: timed out` immediately
    after `docker compose down`, leaving the system unbootable.

    Fix: `sudo -v` at the very start of scrub (before backup) caches
    the credential. A background `sudo -nv` keep-alive every 60s
    refreshes the timestamp through the long phases. Both must be
    present and ordered correctly: pre-auth THEN keep-alive THEN the
    rest of the scrub work.
    """

    def test_scrub_preauths_and_keeps_alive(self):
        # Sudo pre-auth + keep-alive now live in scrub.py's
        # _SudoKeepAlive class. The bash side just delegates to op_py.
        body = _read("src/onionpress/scrub.py")

        # `sudo -v` must appear somewhere in the scrub module.
        pre_auth = re.search(r'\bsudo[\'\"]?,\s*[\'\"]\-v', body)
        self.assertIsNotNone(
            pre_auth,
            "scrub must call `sudo -v` to pre-authenticate sudo before any "
            "destructive work. Without it, a sudo prompt will time out "
            "after backup and leave the install half-done.",
        )

        # A background keep-alive (sudo -nv in a loop) must appear too.
        self.assertRegex(
            body, r'[\'\"]sudo[\'\"]\s*,\s*[\'\"]\-nv',
            "scrub must keep the sudo timestamp refreshed while the long "
            "phases run — a `sudo -nv` in a background loop is the standard "
            "pattern. Without it, the keep-alive expires and later sudo "
            "calls block.",
        )
        self.assertIsNotNone(
            pre_auth,
            "scrub module must call `sudo -v` (via subprocess.run(['sudo', "
            "'-v'])) to pre-authenticate before any destructive sudo work. "
            "Without it, the destructive sudo blocks on a prompt nobody is "
            "watching, times out, and the script aborts half-uninstalled.",
        )


class TestUninstallPathsKillTray(unittest.TestCase):
    """Every uninstall code path must `pkill onionpress-tray` so the
    tray icon doesn't sit there showing a stale "running" state while
    the launcher and containers are gone. The tray is a separate user
    process (launched by the autostart .desktop, not by the systemd
    onionpress.service unit), so stopping the service does NOT touch it.

    Incident: after scrub's uninstall step, the tray icon stayed purple
    for 2+ hours showing "running" while the service was actually dead.
    The user thought everything was fine.

    The .deb's prerm has had this fix for a while (`pkill onionpress-tray`);
    scrub now does too.
    """

    def test_scrub_uninstall_kills_tray(self):
        # Lives in scrub.py:phase_uninstall now (was inline bash).
        py = _read("src/onionpress/scrub.py")
        self.assertRegex(
            py, r'pkill[^\n]*onionpress-tray',
            "scrub.phase_uninstall must `pkill -u $USER -f onionpress-tray` "
            "so the tray icon disappears with the rest of the install. "
            "Without this it polls a deleted DATA_DIR for hours.",
        )

    def test_deb_prerm_kills_tray(self):
        script = _read("build/build-linux.sh")
        # Carve out the prerm heredoc (PRERM is the marker we just used).
        m = re.search(
            r"<<'PRERM'(.*?)PRERM\s*$", script, re.DOTALL | re.MULTILINE)
        self.assertIsNotNone(
            m, "build-linux.sh must define a DEBIAN/prerm via a heredoc")
        self.assertRegex(
            m.group(1), r'pkill\s+[^\n]*onionpress-tray',
            "The .deb's prerm must pkill the tray on `apt remove` so the "
            "icon goes with the package. Without this it stays around "
            "showing a stale state until the user logs out.",
        )


class TestStopContainersWritesStoppedStatus(unittest.TestCase):
    """When the launcher stops containers, it must write a final
    status.json with state="stopped" so the tray (which polls the file
    every 2s) flips its icon to gray within one cycle. Without this,
    the tray holds the previous "running" snapshot indefinitely.

    The launcher's write_status() already maps an empty `docker compose ps`
    to state="stopped" — stop_containers just has to call it.
    """

    def test_stop_containers_writes_status(self):
        script = _read("linux/onionpress")
        m = re.search(
            r'stop_containers\(\)\s*\{(.*?)^\}', script,
            re.DOTALL | re.MULTILINE)
        self.assertIsNotNone(m, "stop_containers() must exist")
        body = m.group(1)
        # write_status must be called after the compose down — order matters:
        # the call needs to see the post-down `docker compose ps` (empty).
        down = re.search(r'docker\s+compose\s+down', body)
        ws = re.search(r'\bwrite_status\b', body)
        self.assertIsNotNone(down, "stop_containers must run `docker compose down`")
        self.assertIsNotNone(
            ws,
            "stop_containers must call write_status so the tray flips its "
            "icon to gray. Linux tray is a separate process from the "
            "launcher (file-based IPC via status.json); without this, the "
            "icon stays purple/yellow indefinitely after the service stops.",
        )
        self.assertLess(
            down.start(), ws.start(),
            "write_status must come AFTER `docker compose down` — write_status "
            "calls `docker compose ps` to derive state; calling it before "
            "down would still report the running containers.",
        )


class TestOnionpressOnboardedUsesSiteOption(unittest.TestCase):
    """`onionpress_onboarded` is set network-wide by the onboarding mu-
    plugin via update_site_option(), so every reader/writer in the
    launcher and setup_logic must use the site_option API too. A plain
    `wp option update onionpress_onboarded 1` writes wp_options (per-blog)
    while the plugin reads from wp_sitemeta (multisite) → admin gets
    redirected to ?page=onionpress-onboarding on every wp-admin visit.

    Incident: fresh installs hit the wizard redirect loop even though
    setup completed cleanly.
    """

    def test_no_plain_option_writes_to_onionpress_onboarded(self):
        for path in ("src/onionpress/setup_logic.py",
                     "linux/onionpress",
                     "app/MacOS/onionpress"):
            src = _read(path)
            # The bad pattern: `wp option update onionpress_onboarded ...`
            # (whether shell args or Python list literal).
            self.assertNotRegex(
                src,
                r'\bwp\b[^\n]*\boption\b\s+update[^\n]*onionpress_onboarded',
                f"{path}: do not write onionpress_onboarded via plain "
                "`wp option update` — that writes wp_options (per-blog) "
                "while the onboarding mu-plugin reads via get_site_option "
                "(wp_sitemeta, network-wide on multisite). Use "
                "`wp eval 'update_site_option(...)'` instead.",
            )

    def test_no_plain_option_reads_for_onionpress_onboarded(self):
        for path in ("linux/onionpress", "src/onionpress/setup_logic.py"):
            src = _read(path)
            self.assertNotRegex(
                src,
                r'\bwp\b[^\n]*\boption\b\s+get[^\n]*onionpress_onboarded',
                f"{path}: do not read onionpress_onboarded via plain "
                "`wp option get` — the value lives in wp_sitemeta on "
                "multisite. Use `wp eval 'echo get_site_option(...)'` "
                "instead, matching the mu-plugin's reader.",
            )


class TestTrayIconSize(unittest.TestCase):
    """The flat tray-*.png files in linux/assets/ are what GNOME's
    AppIndicator resolves via the bare icon name ("tray-running",
    etc.). They must be 22×22 — the standard status-icon size most
    shells slot directly without scaling. Shipping 32×32 here led to
    the icon being clipped (shell crops oversized icons rather than
    scaling them on some setups).
    """

    def test_flat_tray_icons_are_22x22(self):
        import struct
        for state in ("running", "starting", "stopped"):
            path = os.path.join(PROJECT_ROOT, "linux", "assets",
                                f"tray-{state}.png")
            self.assertTrue(os.path.isfile(path),
                            f"{path} must exist")
            with open(path, "rb") as f:
                head = f.read(24)
            # PNG: 8-byte signature, IHDR chunk starts at offset 8:
            # 4 length, 4 "IHDR", 4 width, 4 height (big-endian).
            self.assertEqual(head[:8], b'\x89PNG\r\n\x1a\n',
                             f"{path} must be a PNG")
            width = struct.unpack(">I", head[16:20])[0]
            height = struct.unpack(">I", head[20:24])[0]
            self.assertEqual(
                (width, height), (22, 22),
                f"{path} must be 22x22 (got {width}x{height}). "
                "Anything larger gets clipped by GNOME shell status-icon "
                "slots on some setups.",
            )


class TestRestoreReconcilesDbPassword(unittest.TestCase):
    """When restoring a backup into a fresh install, the WP container
    comes up with a NEW auto-generated DB password (in ~/.onionpress/
    secrets), but the backup's mariadb-dump only dumped the wordpress
    database — mysql.user is whatever the current containers initialized
    with. If the DB volume happens to be reused from a prior install
    (interrupted scrub, docker compose down without -v, etc.), mysql.user
    still has the OLD password hash and WP can't connect. Force a
    re-grant after import so the password reliably matches.
    """

    def test_restore_reasserts_wordpress_user_password(self):
        src = _read("src/onionpress/backup.py")
        # Allow the f-string to span a line break (DOTALL).
        self.assertRegex(
            src,
            r"ALTER USER 'wordpress'@'%'[\s\S]{0,40}IDENTIFIED BY",
            "restore_from_backup must `ALTER USER 'wordpress'@'%' "
            "IDENTIFIED BY '<current-secrets-password>'` after the SQL "
            "import. Without it, restoring into a stale DB volume leaves "
            "WP unable to connect (Database Error on every page).",
        )


class TestPortOffsetSticky(unittest.TestCase):
    """When a quit/start happens within ~60s, the previous offset's
    ports are still in TIME_WAIT. Naive port-detection sees them as
    "taken" and bumps the offset, changing the user's wp_port for the
    rest of the session. The Mac menubar fixes this with a .previous-
    port-offset sentinel (src/menubar.py:258). Linux launcher must do
    the same — write sentinel on stop, read+wait on start.
    """

    def test_linux_writes_previous_offset_on_stop(self):
        src = _read("linux/onionpress")
        # Carve out the stop_containers function body (anchor on the
        # function definition, end at the next column-0 closing brace).
        m = re.search(
            r'^stop_containers\(\)\s*\{(.*?)^\}', src,
            re.DOTALL | re.MULTILINE)
        self.assertIsNotNone(m, "stop_containers() must exist")
        self.assertRegex(
            m.group(1), r'\.previous-port-offset',
            "linux/onionpress stop_containers must write "
            "$DATA_DIR/.previous-port-offset before docker compose down "
            "so the next start can wait for the prior ports to free.",
        )

    def test_linux_reads_previous_offset_on_start(self):
        src = _read("linux/onionpress")
        # Detection block must read the sentinel and wait for the
        # desired port to come back.
        self.assertRegex(
            src, r'_prev_sentinel.*\.previous-port-offset',
            "linux/onionpress port-offset detection must read "
            "$DATA_DIR/.previous-port-offset at startup so a quick "
            "restart reuses the same offset instead of bumping.",
        )


class TestScrubBashDelegatesToPython(unittest.TestCase):
    """The scrub flow used to be 300 lines of bash inline in linux/
    onionpress. It's now in src/onionpress/scrub.py; the bash case is
    just a delegation stub. This test pins the wiring.

    The original-bash behaviour-pinning (port match, hub re-registration,
    wayback creds) now lives in tests/test_scrub.py at the Python level
    where each check is independently mockable.
    """

    def test_scrub_case_delegates_via_op_py(self):
        script = _read("linux/onionpress")
        m = re.search(
            r'^ {8}scrub\)(.*?)^ {8}[a-z_-]+\)', script,
            re.DOTALL | re.MULTILINE)
        self.assertIsNotNone(m, "scrub) case must exist in linux/onionpress")
        body = m.group(1)
        # Must invoke the Python CLI's scrub subcommand.
        self.assertRegex(
            body, r'op_py\s+scrub',
            "scrub) must delegate to `op_py scrub` — the phase orchestration "
            "+ verify checks live in src/onionpress/scrub.py now.",
        )
        # Must forward args (positional password + --clean).
        self.assertRegex(
            body, r'"\${@:2}"',
            "scrub) must forward args via \"${@:2}\" — skipping the case "
            "label itself but preserving the user's password + --clean.",
        )


class TestScrubVerifyChecks(unittest.TestCase):
    """The scrub Step 5 (verify) must check the three things that have
    historically drifted across a backup→uninstall→install→restore cycle
    without anyone noticing:

      1. Port match — port detection is non-deterministic when a previous
         socket is in TIME_WAIT, so the offset can bump from +10000 to
         +20000 across a restart, leaving all the URLs pointing at a
         different number than what the user/tray knows about.
      2. OnionHeaven re-registration — the local heartbeat state survives
         restore (it's in the backup), but the HUB needs to see our /online
         within a heartbeat interval, otherwise visitors get fronted by
         wayback fallback for minutes.
      3. Wayback creds — `ensure_archive_s3_keys` runs in background after
         start_containers; if the Tor-routed archive.org login fails, the
         sweep silently can't submit. The install is otherwise functional
         so this is a warning, not a hard fail.
    """

    def setUp(self):
        # Verify checks moved from inline bash to src/onionpress/scrub.py.
        # The bash side is now a 2-line delegation stub; the checks
        # live in the phase_verify function.
        self.py = _read("src/onionpress/scrub.py")

    def test_pre_scrub_port_captured(self):
        # PreScrubState now owns the capture; phase_backup populates it.
        self.assertRegex(
            self.py, r'class\s+PreScrubState[\s\S]{0,400}wp_port',
            "scrub.PreScrubState must hold wp_port so phase_verify can "
            "compare pre/post values and catch port-offset drift.",
        )

    def test_post_scrub_port_matches(self):
        # phase_verify must read ONIONPRESS_WP_PORT and compare to state.wp_port.
        self.assertRegex(
            self.py,
            r'ONIONPRESS_WP_PORT[\s\S]{0,200}state\.wp_port',
            "phase_verify must compare the current ONIONPRESS_WP_PORT to "
            "state.wp_port — otherwise port-offset drift across the restart "
            "silently leaves all the URLs pointing at the wrong number.",
        )

    def test_hub_registration_check(self):
        self.assertRegex(
            self.py, r'onionheaven-registration\.json',
            "scrub verify must poll onionheaven-registration.json to "
            "confirm the hub knows we're back — without it visitors get "
            "wayback fallback for up to a heartbeat-interval after restore.",
        )

    def test_wayback_creds_check(self):
        self.assertRegex(
            self.py, r'onionpress_archive_s3_access',
            "scrub verify must check that ensure_archive_s3_keys landed "
            "Archive.org credentials. Without them the wayback sweep "
            "silently can't submit anything for the lifetime of the install.",
        )


class TestLauncherStartIsIdempotent(unittest.TestCase):
    """`start` must no-op when the stack is already up, and that check must
    come BEFORE the PID lock.

    Incident: the menu-bar app re-enters `start` on every launch
    (menubar.auto_start -> start_service), so merely launching it next to a
    running stack re-ran the up-to-120s wait_for_services and popped a
    blocking osascript dialog over a perfectly healthy site — `start`
    conflating "already fine" with "this failed".

    Ordering is the load-bearing half and is invisible to a behavioural test
    that only checks the exit code: an early `exit 0` placed BELOW
    `echo $$ > $PIDFILE` + `trap 'rm -f $PIDFILE' EXIT` would strip the lock
    belonging to the *other*, still-running invocation on its way out.
    (tests/test_launcher_start_idempotent.py covers the behaviour itself, but
    only on macOS.)
    """

    # assertIn/assertRegex would dump the whole 2700-line launcher into the
    # failure message; these check a boolean so a failure reads as one line.
    def _assert_has(self, pattern, message):
        self.assertTrue(re.search(pattern, self.script), message)

    def setUp(self):
        self.script = _read("app/MacOS/onionpress")

    def test_probe_uses_the_shared_receiver_discovery_idiom(self):
        self._assert_has(
            r'receiver_answering_port',
            "start must probe whether a receiver is already answering.",
        )
        self._assert_has(
            r'for\s+port\s+in\s+8080\s+18080\s+28080\s+38080\s+48080',
            "Probe must walk the same port ladder as the publisher's "
            "RECEIVER_PORTS and test-receiver.sh (docs/static-publish-protocol.md).",
        )
        self._assert_has(
            r'/wp-json/onionpress/v1/status',
            "Probe must hit the receiver's /status route.",
        )
        self._assert_has(
            r'curl[^\n]*--max-time\s+[12]\b',
            "Probe must be bounded — `start` must not gain a slow path just "
            "to discover it has nothing to do.",
        )
        self._assert_has(
            r"grep -q 'receiver_version'",
            "Readiness is judged by the receiver ANSWERING (a body carrying "
            "receiver_version), never by an exit code.",
        )

    def test_probe_runs_before_the_pid_lock_is_taken(self):
        probe_call = "if _receiver_port=$(receiver_answering_port); then"
        self._assert_has(
            re.escape(probe_call),
            "start must call the receiver probe before anything else.",
        )
        probe = self.script.index(probe_call)
        pid_write = self.script.index('echo $$ > "$PIDFILE"')
        trap_install = self.script.index("trap 'rm -f \"$PIDFILE\"")

        self.assertLess(
            probe, pid_write,
            "The already-running probe must run before $PIDFILE is written.",
        )
        self.assertLess(
            probe, trap_install,
            "The already-running probe must run before the EXIT trap is "
            "installed, or a no-op `start` deletes another invocation's "
            "PID file on the way out.",
        )
        self.assertIn(
            "exit 0", self.script[probe:pid_write],
            "An already-running stack must exit 0 — it is a no-op, not a "
            "conflict.",
        )


class TestGenerationUploadCarrier(unittest.TestCase):
    """POST /wp-json/onionpress/v1/generation is multipart-only as of
    receiver_version 2.0. The removed raw-body carrier forced WordPress's
    REST server to buffer the whole tar into a PHP string before the route
    callback ever ran — a ~65MB site needed a 512M memory_limit raise just
    to reach the plugin's own code. Multipart streams to upload_tmp_dir at
    constant memory, so that raise was deleted with the carrier. These
    invariants keep both halves from silently regressing.
    """

    def test_uploads_ini_does_not_resurrect_memory_limit(self):
        ini = _read("app/Resources/docker/wordpress/onionpress-uploads.ini")
        self.assertIsNone(
            re.search(r"^memory_limit\s*=", ini, re.MULTILINE),
            "onionpress-uploads.ini sets memory_limit again — that raise "
            "existed solely for the raw-body upload carrier removed at "
            "receiver_version 2.0. If it is back, either the raw carrier "
            "crept back into onionpress-static-receiver.php (fix that "
            "instead) or a new buffering path needs its own justification "
            "here.",
        )

    def test_uploads_ini_still_bounds_multipart(self):
        # Multipart still needs the size ceilings even with no memory raise:
        # upload_max_filesize/post_max_size are what bound the streamed part.
        ini = _read("app/Resources/docker/wordpress/onionpress-uploads.ini")
        for key in ("upload_max_filesize", "post_max_size"):
            self.assertRegex(
                ini, r"(?m)^%s\s*=\s*\d+[GM]$" % key,
                "onionpress-uploads.ini must keep %s — it is the bound on "
                "the multipart part that replaced the raw body." % key,
            )

    def test_receiver_has_no_raw_body_branch(self):
        php = _read("app/Resources/plugins/onionpress-static-receiver.php")
        self.assertNotIn(
            "get_body()", php,
            "onionpress-static-receiver.php reads get_body() again — the "
            "raw-body carrier was removed at receiver_version 2.0 because "
            "WP buffers it into PHP memory before the route can reject it. "
            "Uploads must stay multipart-only (get_file_params).",
        )


if __name__ == "__main__":
    unittest.main()

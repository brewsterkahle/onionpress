#!/usr/bin/env python3
"""Cache Enabler and the Internet Archive Link Fixer: installed, configured,
activated, and held to what an onion service needs.

Neither plugin was ever activated. activate_plugin.sh set DOCKER_HOST to
the Colima socket itself (nothing on Linux), wrapped wp-cli in timeout(1)
(macOS has none) and, until recently, passed docker exec a -T it does not
take; the .deb shipped neither script. And had it worked: it activated the
Link Fixer on the main site only, where the user's posts are not, after it
had been configured there; and Cache Enabler with its own defaults, on a
WordPress that answers to its .onion, localhost and the container names.

Now (onionpress.bundled_plugins, both launchers delegating to it):
  install-bundled-plugins: the pinned release, in place
      (an installed copy of another version is replaced)
  provision-post-install: the mu-plugins, among them the two policies
  setup-bundled-plugins: configure-* → activate_plugin.sh → apply-cache-policy
      (Cache Enabler network-wide, the Link Fixer site by site)
and once setup has made the user's site, setup_logic starts the same.

onionpress-cache-policy.php keeps pages for the site's own onion address
only, never per-visitor ones, sends Cache Enabler's clearing there, and
sets the exclusions serving relies on. onionpress-link-fixer-policy.php
keeps archive.org from being asked about onion, local or private links,
verifies its TLS, bounds its timeouts and paces imports.
"""

import fnmatch
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from onionpress import bundled_plugins, multisite, setup_logic, wp_files  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

LAUNCHERS = ("app/MacOS/onionpress", "linux/onionpress")
ACTIVATE = os.path.join(PROJECT_ROOT, "app/Resources/scripts/activate_plugin.sh")
INSTALL = os.path.join(PROJECT_ROOT, "app/Resources/scripts/install_plugin.sh")
PLUGINS = os.path.join(PROJECT_ROOT, "app/Resources/plugins")
CACHE_POLICY = os.path.join(PLUGINS, "onionpress-cache-policy.php")
LINK_FIXER_POLICY = os.path.join(PLUGINS, "onionpress-link-fixer-policy.php")
TOR_PROXY = os.path.join(PLUGINS, "onionpress-tor-proxy.php")
PHP = shutil.which("php")

CE = "cache-enabler/cache-enabler.php"
IA = ("internet-archive-wayback-machine-link-fixer/"
      "internet-archive-wayback-machine-link-fixer.php")
ONION = "op2homeiwjb4fdqnfkj5kbokvcee45zpk2pwgvpz5rrkanp5qqwxzbyd.onion"
OTHER_ONION = "archivep75mbjunhxc6x4j5mwjmomyxb573v42baldlqu56ruil2oiad.onion"


def _read(rel):
    with open(os.path.join(PROJECT_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _bash_function(text, name):
    start = text.index(f"\n{name}() {{") + 1
    return text[start:text.index("\n}\n", start) + 3]


def _done(stdout="", code=0, stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=stdout, stderr=stderr)


# ─── activate_plugin.sh ──────────────────────────────────────────────

DOCKER_STUB = r"""#!/bin/bash
# Records each call (argv, and the DOCKER_HOST it was run with) and answers
# wp-cli the way STUB_* say.
printf '%s\n' "DOCKER_HOST=${DOCKER_HOST:-}" "$*" >> "$STUB_LOG"
args=" $* "
case "$args" in
    *" core is-installed --network "*) exit "${STUB_NETWORK:-0}" ;;
    *" core is-installed "*) exit "${STUB_INSTALLED:-0}" ;;
    *" site list --field=url "*) printf '%b' "${STUB_SITES:-}"; exit 0 ;;
    *" plugin activate "*)
        for bad in ${STUB_FAIL_URLS:-}; do
            case "$args" in *" --url=$bad "*) echo "Error: no" >&2; exit 1 ;; esac
        done
        echo "Success: Plugin activated."; exit 0 ;;
esac
exit 0
"""


class TestActivatePluginScript(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        stub = os.path.join(self.tmp, "docker")
        with open(stub, "w") as f:
            f.write(DOCKER_STUB)
        os.chmod(stub, 0o755)
        self.log = os.path.join(self.tmp, "calls")

    def _run(self, *args, **stub):
        env = {"PATH": self.tmp + ":/usr/bin:/bin", "STUB_LOG": self.log,
               "DOCKER_HOST": "unix:///run/user/1000/docker.sock"}
        env.update({f"STUB_{k.upper()}": v for k, v in stub.items()})
        r = subprocess.run(["bash", ACTIVATE, *args], env=env,
                           capture_output=True, text=True, timeout=30)
        calls = []
        if os.path.exists(self.log):
            with open(self.log) as f:
                lines = f.read().splitlines()
            calls = list(zip(lines[0::2], lines[1::2]))
            os.unlink(self.log)
        return r, calls

    def _activations(self, calls):
        return [argv.split(" wp ", 1)[1] for _host, argv in calls if " plugin activate " in argv]

    def test_syntax(self):
        subprocess.run(["bash", "-n", ACTIVATE], check=True)

    def test_none_of_what_kept_it_from_running(self):
        code = "\n".join(line for line in _read("app/Resources/scripts/activate_plugin.sh").splitlines()
                         if not line.lstrip().startswith("#"))
        self.assertNotRegex(code, r"\btimeout\b")          # macOS has none
        self.assertNotRegex(code, r"exec -T\b|\s-T\s")      # docker exec has no -T
        self.assertNotIn("DOCKER_HOST", code)              # the launcher's own
        self.assertNotIn("DOCKER_CONFIG", code)
        self.assertNotIn("/Applications", code)
        self.assertNotIn("--allow-root", code)

    def test_every_call_is_wp_cli_as_www_data_in_the_callers_environment(self):
        r, calls = self._run("onionpress-wordpress", CE, "--network")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        prefix = " ".join(wp_files.wp_cli_args(container="onionpress-wordpress")) + " "
        self.assertTrue(calls)
        for host, argv in calls:
            self.assertEqual(host, "DOCKER_HOST=unix:///run/user/1000/docker.sock")
            self.assertTrue(argv.startswith(prefix), argv)

    def test_not_installed_yet(self):
        r, calls = self._run("onionpress-wordpress", CE, "--network", installed="1")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self._activations(calls), [])

    def test_network(self):
        r, calls = self._run("onionpress-wordpress", CE, "--network")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self._activations(calls), [f"plugin activate {CE} --network"])

    def test_single_site_gets_a_plain_activation(self):
        for scope in ("--network", "--each-site", None):
            with self.subTest(scope):
                args = ["onionpress-wordpress", IA] + ([scope] if scope else [])
                r, calls = self._run(*args, network="1")
                self.assertEqual(r.returncode, 0)
                self.assertEqual(self._activations(calls), [f"plugin activate {IA}"])

    def test_each_site(self):
        sites = "http://localhost/\\nhttp://localhost/alice/\\nWarning: junk\\n"
        r, calls = self._run("onionpress-wordpress", IA, "--each-site", sites=sites)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self._activations(calls), [
            f"plugin activate {IA} --url=http://localhost/",
            f"plugin activate {IA} --url=http://localhost/alice/",
        ])

    def test_each_site_goes_on_past_a_failure_and_reports_it(self):
        sites = "http://localhost/\\nhttp://localhost/alice/\\n"
        r, calls = self._run("onionpress-wordpress", IA, "--each-site", sites=sites,
                             fail_urls="http://localhost/")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(len(self._activations(calls)), 2)

    def test_each_site_with_no_sites_listed_fails(self):
        r, calls = self._run("onionpress-wordpress", IA, "--each-site", sites="")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self._activations(calls), [])

    def test_refuses_what_is_not_a_plugin_path_or_an_option(self):
        for args in ((CE, "--everywhere"), ("../../wp-config.php",), ("cache-enabler",),
                     ("cache-enabler/x.php --network",), ()):
            with self.subTest(args):
                r, calls = self._run("onionpress-wordpress", *args)
                self.assertEqual(r.returncode, 1)
                self.assertEqual(calls, [])


# ─── the launchers ───────────────────────────────────────────────────

class TestLaunchers(unittest.TestCase):

    def test_syntax(self):
        for rel in LAUNCHERS + ("app/Resources/scripts/install_plugin.sh",):
            with self.subTest(rel):
                subprocess.run(["bash", "-n", os.path.join(PROJECT_ROOT, rel)], check=True)

    def test_start_installs_then_provisions_then_sets_up(self):
        for rel in LAUNCHERS:
            start = _bash_function(_read(rel), "start_containers")
            with self.subTest(rel):
                install = start.index("\n    install_bundled_plugins\n")
                provision = start.index('onionpress.cli --data-dir "$DATA_DIR" provision-post-install' if "MacOS" in rel
                                        else "op_py provision-post-install")
                setup = start.index("\n    setup_bundled_plugins\n")
                self.assertLess(install, provision)
                self.assertLess(provision, setup)
                # Configuration happens in setup_bundled_plugins only.
                self.assertNotIn("configure-ia-plugin", start)
                self.assertNotIn("configure_cache_enabler", start)

    def test_the_bash_functions_are_the_delegation(self):
        """#442.3: both launchers' bodies are the one call to the CLI,
        tee'd into the log, never stopping a start."""
        for rel, runner in ((LAUNCHERS[0], "bundled_python -m onionpress.cli"),
                            (LAUNCHERS[1], "op_py")):
            text = _read(rel)
            for name, sub in (("install_bundled_plugins", "install-bundled-plugins"),
                              ("setup_bundled_plugins", "setup-bundled-plugins")):
                with self.subTest(rel=rel, name=name):
                    self.assertEqual(
                        _bash_function(text, name),
                        f"{name}() {{\n"
                        f'    {runner} --data-dir "$DATA_DIR" {sub} \\\n'
                        f'        --scripts-dir "$SCRIPTS_DIR" 2>&1 | tee -a "$LOG_FILE"\n'
                        "    return 0\n}\n")
            with self.subTest(rel=rel):
                for gone in ("plugin_wanted", "wp_plugin_present", "wp_plugin_version",
                             "install_wp_org_plugin", "install_ia_plugin",
                             "install_cache_enabler"):
                    self.assertNotIn(f"\n{gone}() {{", text)
                self.assertNotIn("install_plugin.sh", text)
                self.assertNotIn("activate_plugin.sh", text)

    def test_subcommand_for_after_setup(self):
        for rel in LAUNCHERS:
            with self.subTest(rel):
                self.assertRegex(_read(rel),
                                 r"\n        setup-bundled-plugins\)\n(?:            #[^\n]*\n)*"
                                 r"            setup_bundled_plugins\n            ;;\n")

    def test_install_plugin_prints_the_pinned_version(self):
        for slug, version in (("cache-enabler", "1.8.17"),
                              ("internet-archive-wayback-machine-link-fixer", "1.4.2")):
            with self.subTest(slug):
                r = subprocess.run(["bash", INSTALL, "--pinned-version", slug],
                                   capture_output=True, text=True, timeout=30)
                self.assertEqual((r.returncode, r.stdout), (0, version + "\n"))
        r = subprocess.run(["bash", INSTALL, "--pinned-version", "hello-dolly"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual((r.returncode, r.stdout), (1, ""))


# ─── install and set up (onionpress.bundled_plugins) ─────────────────

class _Result:
    def __init__(self, ok=True, stdout=""):
        self.ok, self.stdout, self.stderr = ok, stdout, ""


class _FakeDocker:
    """docker for bundled_plugins: wp-cli answers by *installed*, the
    plugins folder is *root* on this machine (test -f and sed run here, on
    the path under it)."""

    def __init__(self, root, installed=True):
        self.root, self.installed, self.calls = root, installed, []

    @property
    def env(self):
        """What the scripts run with: docker's environment."""
        return dict(os.environ)

    def run(self, args, timeout=None, quiet=False):
        self.calls.append(list(args))
        if "wp" in args:
            return _Result(self.installed)
        user = args.index("-u")
        self.assert_www_data(args, user)
        local = [a.replace(bundled_plugins.PLUGINS_DIR, self.root) for a in args[user + 3:]]
        done = subprocess.run(local, capture_output=True, text=True, timeout=30)
        return _Result(done.returncode == 0, done.stdout)

    @staticmethod
    def assert_www_data(args, user):
        assert args[:1] == ["exec"] and args[user + 1] == "www-data", args


SCRIPT_STUB = """#!/bin/bash
printf '%s\\n' "$(basename "$0") $*" >> "$CALLS"
if [ "$1" = --pinned-version ]; then
    case "$2" in cache-enabler) echo 1.8.17 ;; internet-archive-wayback-machine-link-fixer) echo 1.4.2 ;; *) exit 1 ;; esac
    exit 0
fi
case "$(basename "$0")" in
    install_plugin.sh) [ -z "${FAIL_DOWNLOAD:-}" ] || exit 1; mkdir -p "$2/$1" ;;
    activate_plugin.sh) [ -z "${FAIL_ACTIVATE:-}" ] || exit 1 ;;
esac
echo "script output"
"""


class TestBundledPluginsPython(unittest.TestCase):
    """onionpress.bundled_plugins against a fake docker, stub scripts and a
    recording step runner: the bash bodies' order, words and conditions."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "plugins")
        os.makedirs(self.root)
        self.scripts = os.path.join(self.tmp, "scripts")
        os.makedirs(self.scripts)
        for name in ("install_plugin.sh", "activate_plugin.sh"):
            path = os.path.join(self.scripts, name)
            with open(path, "w") as f:
                f.write(SCRIPT_STUB)
            os.chmod(path, 0o755)
        self.config = os.path.join(self.tmp, "config")
        open(self.config, "w").close()
        self.calls_file = os.path.join(self.tmp, "calls")
        self.log_file = os.path.join(self.tmp, "log")
        self.lines, self.steps = [], []
        self.step_rc = {}
        self.env = mock.patch.dict(os.environ, {"CALLS": self.calls_file})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.docker = _FakeDocker(self.root)

    def host(self):
        def step(argv):
            self.steps.append(" ".join(argv))
            return self.step_rc.get(argv[0], 0)
        return bundled_plugins.PluginHost(
            docker=self.docker, config_file=self.config, scripts_dir=self.scripts,
            log_file=self.log_file, log=self.lines.append, run_step=step)

    def plugin(self, slug, version):
        os.makedirs(os.path.join(self.root, slug), exist_ok=True)
        with open(os.path.join(self.root, slug, slug + ".php"), "w") as f:
            f.write(f"<?php\n/*\n * Version: {version}\n */\n")

    def scripts_run(self):
        try:
            with open(self.calls_file) as f:
                return [re.sub(r"/\S*/tmp\w*", "TMP", l) for l in f.read().splitlines()]
        except FileNotFoundError:
            return []

    def test_installs_the_pinned_release_of_each(self):
        bundled_plugins.install_bundled_plugins(self.host())
        self.assertEqual(self.lines, [
            "Installing Internet Archive Wayback Machine Link Fixer 1.4.2...",
            "✓ Internet Archive Wayback Machine Link Fixer 1.4.2 installed with verified checksum",
            "Installing Cache Enabler 1.8.17...",
            "✓ Cache Enabler 1.8.17 installed with verified checksum"])
        self.assertEqual(self.scripts_run(), [
            "install_plugin.sh --pinned-version internet-archive-wayback-machine-link-fixer",
            "install_plugin.sh internet-archive-wayback-machine-link-fixer TMP",
            "install_plugin.sh --pinned-version cache-enabler",
            "install_plugin.sh cache-enabler TMP"])
        self.assertEqual([re.sub(r"/\S*/tmp\w*", "TMP", s) for s in self.steps], [
            "put-wp-tree --src TMP/internet-archive-wayback-machine-link-fixer --dest "
            "/var/www/html/wp-content/plugins/internet-archive-wayback-machine-link-fixer",
            "put-wp-tree --src TMP/cache-enabler --dest /var/www/html/wp-content/plugins/cache-enabler"])
        with open(self.log_file) as f:
            self.assertEqual(f.read(), "script output\n" * 2)

    def test_the_pinned_one_installed_is_left(self):
        self.plugin("cache-enabler", "1.8.17")
        self.plugin("internet-archive-wayback-machine-link-fixer", "1.4.2")
        bundled_plugins.install_bundled_plugins(self.host())
        self.assertEqual(self.lines, [
            "Internet Archive Wayback Machine Link Fixer 1.4.2 already installed",
            "Cache Enabler 1.8.17 already installed"])
        self.assertEqual(self.steps, [])

    def test_install_replaces_an_unpinned_copy(self):
        self.plugin("cache-enabler", "1.8.16")
        with open(self.config, "w") as f:
            f.write("INSTALL_IA_PLUGIN=no\n")
        bundled_plugins.install_bundled_plugins(self.host())
        self.assertEqual(self.lines, [
            "Internet Archive plugin installation disabled in config",
            "Replacing Cache Enabler 1.8.16 with the pinned release 1.8.17...",
            "✓ Cache Enabler 1.8.17 installed with verified checksum"])

    def test_failures_are_lines_never_a_stop(self):
        with open(self.config, "w") as f:
            f.write("INSTALL_CACHE_ENABLER=no\n")
        with mock.patch.dict(os.environ, {"FAIL_DOWNLOAD": "1"}):
            bundled_plugins.install_bundled_plugins(self.host())
        self.assertEqual(self.lines[1:], [
            "Failed to download and verify Internet Archive Wayback Machine Link Fixer"
            " - check log for details",
            "Cache Enabler plugin installation disabled in config"])
        self.lines.clear()
        self.step_rc["put-wp-tree"] = 1
        bundled_plugins.install_bundled_plugins(self.host())
        self.assertEqual(self.lines[1], "Failed to copy Internet Archive Wayback Machine "
                                        "Link Fixer to container")

    def test_no_scripts_no_pin(self):
        shutil.rmtree(self.scripts)
        bundled_plugins.install_bundled_plugins(self.host())
        self.assertEqual(self.lines, [
            "Plugin installation scripts not available, skipping Internet Archive "
            "Wayback Machine Link Fixer",
            "Plugin installation scripts not available, skipping Cache Enabler"])

    def test_the_temporary_folder_goes(self):
        made = []
        real = tempfile.mkdtemp
        with mock.patch.object(bundled_plugins.tempfile, "mkdtemp",
                               side_effect=lambda: made.append(real()) or made[-1]):
            bundled_plugins.install_bundled_plugins(self.host())
        self.assertEqual(len(made), 2)
        self.assertFalse(any(os.path.exists(d) for d in made))

    def test_setup_configures_then_activates_then_applies(self):
        self.plugin("cache-enabler", "1.8.17")
        self.plugin("internet-archive-wayback-machine-link-fixer", "1.4.2")
        bundled_plugins.setup_bundled_plugins(self.host())
        self.assertEqual(self.steps, ["configure-cache-enabler", "apply-cache-policy",
                                      "configure-ia-plugin"])
        self.assertEqual(self.scripts_run(), [
            f"activate_plugin.sh onionpress-wordpress {CE} --network",
            f"activate_plugin.sh onionpress-wordpress {IA} --each-site"])
        self.assertEqual(self.lines, [
            "✓ Cache Enabler active, cache policy applied",
            "✓ Internet Archive Wayback Machine Link Fixer active on every site"])

    def test_each_step_only_after_the_one_before(self):
        self.plugin("cache-enabler", "1.8.17")
        self.plugin("internet-archive-wayback-machine-link-fixer", "1.4.2")
        self.step_rc["configure-cache-enabler"] = 1
        with mock.patch.dict(os.environ, {"FAIL_ACTIVATE": "1"}):
            bundled_plugins.setup_bundled_plugins(self.host())
        self.assertEqual(self.steps, ["configure-cache-enabler", "configure-ia-plugin"])
        self.assertEqual(self.scripts_run(), [
            f"activate_plugin.sh onionpress-wordpress {IA} --each-site"])
        self.assertEqual(self.lines, [
            f"WARNING: Cache Enabler not set up — see {self.log_file}",
            f"WARNING: Internet Archive Link Fixer not set up — see {self.log_file}"])

    def test_not_before_wordpress_is_installed(self):
        self.plugin("cache-enabler", "1.8.17")
        self.docker.installed = False
        bundled_plugins.setup_bundled_plugins(self.host())
        self.assertEqual(self.lines, ["WordPress not installed yet — Cache Enabler and the "
                                      "Link Fixer are set up after setup"])
        self.assertEqual((self.steps, self.scripts_run()), ([], []))

    def test_unwanted_or_absent_plugins_are_left(self):
        self.plugin("cache-enabler", "1.8.17")
        with open(self.config, "w") as f:
            f.write("INSTALL_CACHE_ENABLER=no\n")
        bundled_plugins.setup_bundled_plugins(self.host())
        self.assertEqual((self.lines, self.steps, self.scripts_run()), ([], [], []))

    def test_every_look_is_wp_cli_or_www_data(self):
        self.plugin("cache-enabler", "1.8.17")
        bundled_plugins.install_bundled_plugins(self.host())
        bundled_plugins.setup_bundled_plugins(self.host())
        self.assertTrue(self.docker.calls)
        for call in self.docker.calls:
            with self.subTest(call=call):
                self.assertNotIn("--allow-root", call)
                self.assertEqual(call[call.index("-u") + 1], "www-data")

    def test_installed_version_is_read_from_the_plugin_header(self):
        headers = {
            "cache-enabler":
                "<?php\n/*\nPlugin Name: Cache Enabler\nText Domain: cache-enabler\n"
                "Version: 1.8.16\n*/\n",
            "internet-archive-wayback-machine-link-fixer":
                "<?php\n/**\n * The wayback-link-fixer bootstrap file.\n *\n"
                " * @version     1.4.2\n * @wordpress-plugin\n"
                " * Plugin Name:             Internet Archive Wayback Machine Link Fixer\n"
                " * Version:                 1.4.2\r\n * Requires at least:       6.4\n */\n",
            "evil": "<?php\n/*\n * Version: 1.0$(touch /tmp/x)\x1b[2J\n */\n",
        }
        for slug, text in headers.items():
            os.makedirs(os.path.join(self.root, slug), exist_ok=True)
            with open(os.path.join(self.root, slug, slug + ".php"), "w") as f:
                f.write(text)
        found = {slug: bundled_plugins.plugin_version(self.host(), slug)
                 for slug in list(headers) + ["missing"]}
        self.assertEqual(found, {"cache-enabler": "1.8.16",
                                 "internet-archive-wayback-machine-link-fixer": "1.4.2",
                                 "evil": "1.0touchtmpx2J", "missing": ""})

    def test_plugin_wanted(self):
        def wanted(config):
            if config is None:
                os.unlink(self.config)
            else:
                with open(self.config, "w") as f:
                    f.write(config)
            return bundled_plugins.plugin_wanted(self.config, "INSTALL_IA_PLUGIN")
        self.assertTrue(wanted(None))
        self.assertTrue(wanted("OTHER=no\n"))
        self.assertTrue(wanted("INSTALL_IA_PLUGIN=\n"))
        self.assertTrue(wanted("INSTALL_IA_PLUGIN=yes\n"))
        self.assertFalse(wanted("INSTALL_IA_PLUGIN=no\n"))
        # A duplicated key: the first line, config.read_value's rule (#404).
        self.assertFalse(wanted("INSTALL_IA_PLUGIN=no\nINSTALL_IA_PLUGIN=yes\n"))
        self.assertTrue(wanted("INSTALL_IA_PLUGIN=yes\nINSTALL_IA_PLUGIN=no\n"))


class TestSetupStartsThePluginSetup(unittest.TestCase):

    def _install(self, launcher_bin):
        order = []
        data = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, data, True)
        with mock.patch.object(setup_logic.subprocess, "run", return_value=_done()), \
             mock.patch.object(setup_logic, "provision_primary_subsite",
                               side_effect=lambda **k: order.append("subsite")), \
             mock.patch.object(setup_logic, "start_bundled_plugin_setup",
                               side_effect=lambda b, log: order.append(("plugins", b))), \
             mock.patch("threading.Thread"):
            setup_logic.install_fresh_wordpress(
                site_title="t", onionname="alice", password="pw-pw-pw-pw",
                onion_addr=ONION, launcher_bin=launcher_bin, data_dir=data)
        return order

    def test_after_the_users_site_exists(self):
        self.assertEqual(self._install("/x/onionpress"),
                         ["subsite", ("plugins", "/x/onionpress")])

    def test_not_without_a_launcher(self):
        self.assertEqual(self._install(None), ["subsite"])

    def test_in_the_background(self):
        logs = []
        with mock.patch.object(setup_logic.subprocess, "Popen") as popen:
            self.assertTrue(setup_logic.start_bundled_plugin_setup("/x/onionpress", logs.append))
        self.assertEqual(popen.call_args.args[0], ["/x/onionpress", "setup-bundled-plugins"])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        with mock.patch.object(setup_logic.subprocess, "Popen", side_effect=OSError("gone")):
            self.assertFalse(setup_logic.start_bundled_plugin_setup("/x/onionpress", logs.append))


# ─── packaging ───────────────────────────────────────────────────────

class TestShipped(unittest.TestCase):

    def test_deb_carries_the_plugin_scripts(self):
        """collect_files, run for real into a scratch folder."""
        dest = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, dest, True)
        fn = _bash_function(_read("build/build-linux.sh"), "collect_files")
        subprocess.run(["bash", "-c", "set -e\n" + fn + 'collect_files "$1"', "bash", dest],
                       check=True, env={"PROJECT_DIR": PROJECT_ROOT, "VERSION": "0.0.0",
                                        "PATH": "/usr/bin:/bin"},
                       capture_output=True, timeout=120)
        for name in ("install_plugin.sh", "activate_plugin.sh"):
            with self.subTest(name):
                path = os.path.join(dest, "scripts", name)
                self.assertTrue(os.path.isfile(path))
                self.assertTrue(os.stat(path).st_mode & stat.S_IXUSR)
        for name in ("onionpress-cache-policy.php", "onionpress-link-fixer-policy.php"):
            with self.subTest(name):
                self.assertTrue(os.path.isfile(os.path.join(dest, "plugins", name)))

    def test_install_sh_puts_them_in_scripts_itself(self):
        """install.sh installs linux/install-tree.sh's list, which puts each
        script in scripts/ itself (a `cp -r` of the folder put them in
        scripts/scripts/ on a reinstall)."""
        text = _read("linux/install.sh")
        self.assertIn('linux/install-tree.sh" install "$REPO_DIR" "$INSTALL_DIR"', text)
        self.assertNotRegex(text, r'cp -r "\$REPO_DIR/app/Resources/scripts"')
        tree = _read("linux/install-tree.sh")
        for name in ("install_plugin.sh", "activate_plugin.sh"):
            with self.subTest(name):
                self.assertRegex(tree, rf"\nfile +app/Resources/scripts/{name} +"
                                       rf"scripts/{name} +0755\n")

    def test_dmg_carries_the_scripts(self):
        self.assertIn('cp -R "$PROJECT_DIR/app/Resources/scripts" '
                      '"$APP_PATH/Contents/Resources/scripts"', _read("build/build-dmg-simple.sh"))

    def test_policies_are_installed_mu_plugins(self):
        for name in (multisite.CACHE_POLICY_MU_PLUGIN, multisite.IA_POLICY_MU_PLUGIN):
            with self.subTest(name):
                self.assertIn(name, multisite.MU_PLUGINS)
                self.assertTrue(os.path.isfile(os.path.join(PLUGINS, name)))


# ─── configuration (Python) ──────────────────────────────────────────

class _FakeWp:
    """Stands in for multisite._wp: options per --url, a call log."""

    def __init__(self, sites=("http://localhost/", "http://localhost/alice/"),
                 options=None, fail=()):
        self.sites = list(sites)
        self.options = options or {}
        self.calls = []
        self.fail = set(fail)

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        if args[:2] == ("site", "list"):
            return _done("\n".join(self.sites) + "\n")
        url = next((a[6:] for a in args if a.startswith("--url=")), None)
        opts = self.options.setdefault(url, {})
        if args[:2] == ("option", "get"):
            if args[2] not in opts:
                return _done(code=1, stderr="Error: Could not get option.")
            value = opts[args[2]]
            return _done((json.dumps(value) if "--format=json" in args else value) + "\n")
        if args[:2] == ("option", "update"):
            if args[2] in self.fail:
                return _done(code=1, stderr="Error: no")
            opts[args[2]] = json.loads(args[3]) if "--format=json" in args else args[3]
            return _done("Success")
        return _done()


class TestConfigureIaPlugin(unittest.TestCase):

    def _configure(self, wp, present=True, policy=True):
        def in_container(cmd, **kwargs):
            if IA in cmd:
                return _done(code=0 if present else 1)
            if multisite.IA_POLICY_MU_PLUGIN in cmd:
                return _done(code=0 if policy else 1)
            return _done(code=1)
        logs = []
        with mock.patch.object(multisite, "wp_is_installed", return_value=True), \
             mock.patch.object(multisite, "_exec_www_data", side_effect=in_container), \
             mock.patch.object(multisite, "_wp", side_effect=wp):
            ok = multisite.configure_ia_plugin(log_func=logs.append)
        return ok, logs

    def test_options(self):
        self.assertEqual(dict(multisite.IA_PLUGIN_OPTIONS), {
            "iawmlf_process_links": "1",
            "iawmlf_fixer_option": "replace_link",
            "iawmlf_scan_existing_posts": "1",
            "iawmlf_cast_to_https": "1",
            "iawmlf_allow_own_content_submissions": "0",
            "iawmlf_setup_wizard_completed": "1",
        })
        # The wizard is marked done last.
        self.assertEqual(multisite.IA_PLUGIN_OPTIONS[-1][0], "iawmlf_setup_wizard_completed")

    def test_every_site_gets_options_exclusions_and_the_marker(self):
        wp = _FakeWp()
        ok, _ = self._configure(wp)
        self.assertTrue(ok)
        for url in wp.sites:
            with self.subTest(url):
                opts = wp.options[url]
                for name, value in multisite.IA_PLUGIN_OPTIONS:
                    self.assertEqual(opts[name], value)
                self.assertEqual(opts["iawmlf_link_exclusions"], list(multisite.IA_LINK_EXCLUSIONS))
                self.assertEqual(opts[multisite.IA_CONFIG_MARKER], multisite.IA_CONFIG_VERSION)
        self.assertNotIn(None, wp.options)  # nothing ran without --url

    def test_a_completed_wizard_keeps_its_choices(self):
        wp = _FakeWp(sites=("http://localhost/",), options={"http://localhost/": {
            "iawmlf_setup_wizard_completed": "1", "iawmlf_fixer_option": "do_nothing",
            "iawmlf_link_exclusions": ["https://example.com/*", "*://*.onion"]}})
        ok, _ = self._configure(wp)
        self.assertTrue(ok)
        opts = wp.options["http://localhost/"]
        self.assertEqual(opts["iawmlf_fixer_option"], "do_nothing")
        self.assertNotIn("iawmlf_scan_existing_posts", opts)
        self.assertEqual(opts["iawmlf_link_exclusions"][:2], ["https://example.com/*", "*://*.onion"])
        self.assertEqual(sorted(opts["iawmlf_link_exclusions"]),
                         sorted(set(opts["iawmlf_link_exclusions"])))
        self.assertTrue(set(multisite.IA_LINK_EXCLUSIONS) <= set(opts["iawmlf_link_exclusions"]))

    def test_once_per_site_and_version(self):
        wp = _FakeWp(options={u: {multisite.IA_CONFIG_MARKER: multisite.IA_CONFIG_VERSION}
                              for u in ("http://localhost/", "http://localhost/alice/")})
        ok, _ = self._configure(wp)
        self.assertTrue(ok)
        self.assertEqual([c for c in wp.calls if c[:2] == ("option", "update")], [])

    def test_a_failed_write_leaves_the_marker_off_and_fails(self):
        wp = _FakeWp(sites=("http://localhost/",), fail=("iawmlf_cast_to_https",))
        ok, _ = self._configure(wp)
        self.assertFalse(ok)
        self.assertNotIn(multisite.IA_CONFIG_MARKER, wp.options["http://localhost/"])

    def test_refuses_without_its_policy(self):
        wp = _FakeWp()
        ok, logs = self._configure(wp, policy=False)
        self.assertFalse(ok)
        self.assertEqual(wp.calls, [])
        self.assertTrue(any(multisite.IA_POLICY_MU_PLUGIN in m for m in logs))

    def test_nothing_to_do_without_the_plugin(self):
        wp = _FakeWp()
        ok, _ = self._configure(wp, present=False)
        self.assertTrue(ok)
        self.assertEqual(wp.calls, [])

    def test_exclusions_cover_onion_and_local_links_only(self):
        def excluded(url):
            return any(fnmatch.fnmatchcase(url, p) for p in multisite.IA_LINK_EXCLUSIONS)
        for url in (f"http://{ONION}", f"http://{ONION}/", f"http://{OTHER_ONION}/a/b?c=d",
                    f"https://{ONION}:8080/x", f"http://{ONION}?x", f"http://{ONION}#top",
                    "http://localhost", "http://localhost:8080/wp-admin/"):
            with self.subTest(url):
                self.assertTrue(excluded(url))
        for url in ("https://example.com/", "https://www.torproject.org/download/",
                    "https://web.archive.org/web/2020/https://example.com/"):
            with self.subTest(url):
                self.assertFalse(excluded(url))


class TestConfigureCacheEnabler(unittest.TestCase):

    def _configure(self, fs_method=None, policy=True, present=True):
        calls = []

        def wp(*args, **kwargs):
            calls.append(args)
            if args[:3] == ("config", "get", "FS_METHOD"):
                return _done(fs_method + "\n") if fs_method else _done(code=1)
            return _done()

        def in_container(cmd, **kwargs):
            if CE in cmd:
                return _done(code=0 if present else 1)
            if multisite.CACHE_POLICY_MU_PLUGIN in cmd:
                return _done(code=0 if policy else 1)
            return _done(code=1)

        with mock.patch.object(multisite, "wp_is_installed", return_value=True), \
             mock.patch.object(multisite, "_exec_www_data", side_effect=in_container), \
             mock.patch.object(multisite, "_wp", side_effect=wp):
            ok = multisite.configure_cache_enabler(log_func=lambda _m: None)
        return ok, calls

    def test_sets_fs_method_once(self):
        ok, calls = self._configure()
        self.assertTrue(ok)
        self.assertIn(("config", "set", "FS_METHOD", "direct", "--type=constant"), calls)
        ok, calls = self._configure(fs_method="direct")
        self.assertTrue(ok)
        self.assertEqual(calls, [("config", "get", "FS_METHOD")])

    def test_refuses_without_its_policy(self):
        ok, calls = self._configure(policy=False)
        self.assertFalse(ok)
        self.assertEqual(calls, [])

    def test_nothing_to_do_without_the_plugin(self):
        ok, calls = self._configure(present=False)
        self.assertTrue(ok)
        self.assertEqual(calls, [])


class TestApplyCachePolicy(unittest.TestCase):

    def _apply(self, wp_rc=0):
        wp = mock.Mock(return_value=_done(code=wp_rc))
        sh = mock.Mock(return_value=_done())
        with mock.patch.object(multisite, "_wp", wp), \
             mock.patch.object(multisite, "_exec_www_data", sh):
            ok = multisite.apply_cache_policy(log_func=lambda _m: None)
        return ok, wp, sh

    def test_validates_then_clears_as_www_data(self):
        ok, wp, sh = self._apply()
        self.assertTrue(ok)
        self.assertEqual(wp.call_args.args, ("onionpress-cache-policy", "apply"))
        cmd = sh.call_args.args[0]
        self.assertEqual(cmd, "rm -f /var/www/html/wp-content/settings/cache-enabler/*.php"
                              " && rm -rf /var/www/html/wp-content/cache/cache-enabler")

    def test_leaves_the_files_when_the_check_fails(self):
        ok, wp, sh = self._apply(wp_rc=1)
        self.assertFalse(ok)
        sh.assert_not_called()

    def test_the_policy_plugin_provides_the_command(self):
        self.assertIn("WP_CLI::add_command( 'onionpress-cache-policy', 'onionpress_cache_policy_cli' )",
                      _read("app/Resources/plugins/onionpress-cache-policy.php"))

    def test_exec_www_data_is_www_data(self):
        with mock.patch.object(multisite.subprocess, "run", return_value=_done()) as run:
            multisite._exec_www_data("true")
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index("-u") + 1], "www-data")


# ─── the mu-plugins, in PHP ──────────────────────────────────────────

PREAMBLE = r"""<?php
define( 'ABSPATH', __DIR__ . '/' );
define( 'MINUTE_IN_SECONDS', 60 );
$GLOBALS['op_hooks'] = array();
function add_filter( $name, $cb, $prio = 10, $accepted = 1 ) {
    $GLOBALS['op_hooks'][ $name ][ $prio ][] = array( $cb, $accepted );
    return true;
}
function add_action( $name, $cb, $prio = 10, $accepted = 1 ) { return add_filter( $name, $cb, $prio, $accepted ); }
function remove_filter( $name, $cb, $prio = 10 ) {
    foreach ( $GLOBALS['op_hooks'][ $name ][ $prio ] ?? array() as $i => $h ) {
        if ( $h[0] === $cb ) { unset( $GLOBALS['op_hooks'][ $name ][ $prio ][ $i ] ); }
    }
    return true;
}
function apply_filters( $name, $value, ...$rest ) {
    $hooks = $GLOBALS['op_hooks'][ $name ] ?? array();
    ksort( $hooks );
    foreach ( $hooks as $callbacks ) {
        foreach ( $callbacks as $h ) {
            $value = call_user_func_array( $h[0], array_slice( array_merge( array( $value ), $rest ), 0, $h[1] ) );
        }
    }
    return $value;
}
function do_action( $name, ...$args ) {
    $hooks = $GLOBALS['op_hooks'][ $name ] ?? array();
    ksort( $hooks );
    foreach ( $hooks as $callbacks ) {
        foreach ( $callbacks as $h ) { call_user_func_array( $h[0], array_slice( $args, 0, $h[1] ) ); }
    }
}
class WP_Error {
    public $code;
    public function __construct( $code = '', $message = '' ) { $this->code = $code; }
}
$GLOBALS['op_state'] = array( 'logged_in' => false, 'is_404' => false, 'template' => null,
                              'meta' => array(), 'scanned' => array() );
function is_user_logged_in() { return $GLOBALS['op_state']['logged_in']; }
function is_404() { return $GLOBALS['op_state']['is_404']; }
function is_page() { return null !== $GLOBALS['op_state']['template']; }
function get_queried_object_id() { return 7; }
function get_page_template_slug( $id ) { return $GLOBALS['op_state']['template']; }
function home_url() { return 'http://' . ( $_SERVER['HTTP_HOST'] ?? 'localhost' ) . '/'; }
function get_post_meta( $id, $key, $single ) { return $GLOBALS['op_state']['meta'][ $id ][ $key ] ?? ''; }
function metadata_exists( $type, $id, $key ) { return in_array( $id, $GLOBALS['op_state']['scanned'], true ); }
function esc_url_raw( $url ) { return apply_filters( 'clean_url', $url, $url, 'db' ); }
"""


@unittest.skipUnless(PHP, "php not installed")
class _PhpCase(unittest.TestCase):
    PLUGIN = None
    EXTRA = ""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def php(self, body, setup=""):
        script = os.path.join(self.tmp, "case.php")
        with open(script, "w", encoding="utf-8") as f:
            f.write(PREAMBLE + setup + self.EXTRA
                    + f"require {json.dumps(self.PLUGIN)};\n$out = null;\n" + body
                    + "\necho json_encode( $out );\n")
        r = subprocess.run([PHP, "-n", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return json.loads(r.stdout)


class TestCachePolicyPhp(_PhpCase):
    PLUGIN = CACHE_POLICY

    def setUp(self):
        super().setUp()
        self.onion_file = os.path.join(self.tmp, "onion_address")
        with open(self.onion_file, "w") as f:
            f.write(ONION + "\n")
        self.settings_dir = os.path.join(self.tmp, "settings")
        os.mkdir(self.settings_dir)
        self.EXTRA = (f"define( 'ONIONPRESS_CACHE_ONION_FILE', {json.dumps(self.onion_file)} );\n"
                      f"define( 'CACHE_ENABLER_SETTINGS_DIR', {json.dumps(self.settings_dir)} );\n")

    def test_settings_for_a_new_site(self):
        out = self.php("$out = apply_filters( 'cache_enabler_settings_before_validation', array() );")
        self.assertEqual(out["cache_expires"], 1)
        self.assertEqual(out["cache_expiry_time"], 1)
        for key in ("clear_site_cache_on_saved_post", "clear_site_cache_on_saved_comment",
                    "clear_site_cache_on_saved_term", "clear_site_cache_on_changed_plugin"):
            self.assertEqual(out[key], 1, key)
        for key in ("mobile_cache", "convert_image_urls_to_webp", "minify_html", "compress_cache"):
            self.assertEqual(out[key], 0, key)
        self.assertEqual(out["excluded_query_strings"], "/./")
        self.assertEqual(out["excluded_cookies"], "/^(wordpress|wp-postpass|wp-settings|comment_author)/")

    def test_saved_settings_keep_their_choices_but_not_the_exclusions(self):
        out = self.php("$out = apply_filters( 'cache_enabler_settings_before_validation', array("
                       " 'cache_expires' => 0, 'cache_expiry_time' => 500, 'minify_html' => 1,"
                       " 'excluded_query_strings' => '', 'excluded_cookies' => '/^nothing$/',"
                       " 'excluded_page_paths' => '', 'version' => '1.8.17' ) );")
        self.assertEqual(out["minify_html"], 1)
        self.assertEqual(out["version"], "1.8.17")
        self.assertEqual(out["cache_expires"], 1)
        self.assertEqual(out["cache_expiry_time"], 24)
        self.assertEqual(out["excluded_query_strings"], "/./")
        self.assertNotIn("clear_site_cache_on_saved_post", out)  # not filled in
        out = self.php("$out = apply_filters( 'cache_enabler_settings_before_validation',"
                       " array( 'cache_expiry_time' => 0, 'minify_html' => 0 ) );")
        self.assertEqual(out["cache_expiry_time"], 1)

    def test_the_exclusions_hold_what_serving_must_not_answer(self):
        """Cache Enabler matches excluded_query_strings against the query
        string, excluded_cookies against each cookie name and
        excluded_page_paths against the path, before WordPress loads. And it
        keeps a regex only as a whole /.../ that preg_match accepts, after
        sanitize_text_field."""
        out = self.php(r"""
$e = onionpress_cache_enforced();
$m = function ( $re, $s ) { return 1 === preg_match( $re, $s ); };
$out = array();
foreach ( array( 'excluded_query_strings', 'excluded_cookies', 'excluded_page_paths' ) as $k ) {
    $out['valid'][ $k ] = 1 === preg_match( '/^\/.*\/$/', $e[ $k ] ) && false !== @preg_match( $e[ $k ], '' )
        && ! preg_match( '/[<>%\s]/', $e[ $k ] );
}
foreach ( array( 'op_login=abcdefghijklmnopqrstuvwxyz0123456789', 'utm_source=x&op_login=y',
                 'ref=1&p=3', 'onionpress_creation=a.html', 's=x', 'x' ) as $q ) {
    $out['query'][ $q ] = $m( $e['excluded_query_strings'], $q );
}
foreach ( array( 'wordpress_logged_in_abc', 'wordpress_sec_abc', 'wordpress_test_cookie',
                 'wp-postpass_abc', 'comment_author_abc', 'wp-settings-1', 'wp-settings-time-1',
                 'PHPSESSID', 'tor' ) as $c ) {
    $out['cookie'][ $c ] = $m( $e['excluded_cookies'], $c );
}
foreach ( array( '/wp-json/wp/v2/posts', '/alice/wp-json/', '/wp-admin/', '/wp-login.php',
                 '/onionpress-status', '/onionpress-status-json', '/', '/2026/10/hello/',
                 '/alice/', '/wp-jsonish/' ) as $p ) {
    $out['path'][ $p ] = $m( $e['excluded_page_paths'], $p );
}
""")
        self.assertTrue(all(out["valid"].values()), out["valid"])
        self.assertTrue(all(out["query"].values()), out["query"])
        self.assertEqual({c for c, hit in out["cookie"].items() if not hit}, {"PHPSESSID", "tor"})
        self.assertEqual({p for p, hit in out["path"].items() if not hit},
                         {"/", "/2026/10/hello/", "/alice/", "/wp-jsonish/"})

    def _may_store(self, host, headers=(), **state):
        setup = "".join(f"$GLOBALS['op_state'][{json.dumps(k)}] = {json.dumps(v)};\n"
                        for k, v in state.items())
        return self.php(f"$out = onionpress_cache_may_store( {json.dumps(host)}, "
                        f"json_decode( {json.dumps(json.dumps(list(headers)))} ) );",
                        setup=setup)

    def test_only_anonymous_pages_of_the_own_onion_address(self):
        self.assertTrue(self._may_store(ONION))
        self.assertTrue(self._may_store(ONION, ["Content-Type: text/html; charset=UTF-8",
                                                "Cache-Control: max-age=60"]))
        for host in (ONION + ":80", ONION.upper(), "localhost", "localhost:8080", "127.0.0.1",
                     "wordpress", OTHER_ONION, "onionpress.org", "", ONION + "."):
            with self.subTest(host=host):
                self.assertFalse(self._may_store(host))
        self.assertFalse(self._may_store(ONION, logged_in=True))
        self.assertFalse(self._may_store(ONION, is_404=True))
        for template in ("page-creations.php", "page-directory.php"):
            with self.subTest(template=template):
                self.assertFalse(self._may_store(ONION, template=template))
        self.assertTrue(self._may_store(ONION, template="page-follow.php"))
        for header in ("Set-Cookie: wordpress_logged_in_x=1", "set-cookie:PHPSESSID=2",
                       "Cache-Control: no-cache, must-revalidate, max-age=0, no-store, private",
                       "Cache-Control: private"):
            with self.subTest(header=header):
                self.assertFalse(self._may_store(ONION, [header]))

    def test_no_onion_address_no_caching(self):
        os.unlink(self.onion_file)
        self.assertFalse(self._may_store(ONION))
        with open(self.onion_file, "w") as f:
            f.write(ONION + "x\n")
        self.assertFalse(self._may_store(ONION))

    def test_the_bypass_filter(self):
        out = self.php("$_SERVER['HTTP_HOST'] = 'localhost:8080';"
                       "$out = array( apply_filters( 'cache_enabler_bypass_cache', false ),"
                       " apply_filters( 'cache_enabler_bypass_cache', true ) );")
        self.assertEqual(out, [True, True])
        out = self.php(f"$_SERVER['HTTP_HOST'] = {json.dumps(ONION)};"
                       "$out = array( apply_filters( 'cache_enabler_bypass_cache', false ),"
                       " apply_filters( 'cache_enabler_bypass_cache', true ) );")
        self.assertEqual(out, [False, True])

    DISK = r"""
class Cache_Enabler_Disk {
    public static $deleted = 0;
    public static function cache_iterator( $url ) { return esc_url_raw( $url ); }
    public static function delete_settings_file() { self::$deleted++; }
}
function somewhere_else( $url ) { return esc_url_raw( $url ); }
"""

    def test_clearing_goes_to_the_onion_address(self):
        out = self.php(r"""
$out = array(
    Cache_Enabler_Disk::cache_iterator( 'http://localhost:8080/alice/' ),
    Cache_Enabler_Disk::cache_iterator( 'https://localhost:8080/alice/2026/10/hello/?subpages=1' ),
    Cache_Enabler_Disk::cache_iterator( 'http://localhost' ),
    Cache_Enabler_Disk::cache_iterator( 'http://%s/alice/' ),
    somewhere_else( 'http://localhost:8080/alice/' ),
    apply_filters( 'clean_url', 'http://localhost:8080/', 'http://localhost:8080/', 'display' ),
);
""".replace("%s", ONION), setup=self.DISK)
        self.assertEqual(out, [
            f"http://{ONION}/alice/",
            f"http://{ONION}/alice/2026/10/hello/?subpages=1",
            f"http://{ONION}",
            f"http://{ONION}/alice/",
            "http://localhost:8080/alice/",
            "http://localhost:8080/",
        ])

    def test_clearing_untouched_without_cache_enabler_or_an_address(self):
        out = self.php("$out = esc_url_raw( 'http://localhost:8080/alice/' );")
        self.assertEqual(out, "http://localhost:8080/alice/")
        os.unlink(self.onion_file)
        out = self.php("$out = Cache_Enabler_Disk::cache_iterator( 'http://localhost:8080/' );",
                       setup=self.DISK)
        self.assertEqual(out, "http://localhost:8080/")

    def test_a_settings_change_drops_the_onion_settings_files(self):
        keep = ("localhost.php", "localhost.alice.php", OTHER_ONION + ".php", "x" + ONION + ".php")
        for name in keep + (ONION + ".php", ONION + ".alice.php"):
            open(os.path.join(self.settings_dir, name), "w").close()
        self.php("do_action( 'update_option_cache_enabler', array(), array(), 'cache_enabler' );")
        self.assertEqual(sorted(os.listdir(self.settings_dir)), sorted(keep))
        open(os.path.join(self.settings_dir, ONION + ".php"), "w").close()
        self.php("do_action( 'add_option_cache_enabler', 'cache_enabler', array() );")
        self.assertEqual(sorted(os.listdir(self.settings_dir)), sorted(keep))

    def test_settings_files_of_made_up_hosts_are_removed(self):
        # Cache Enabler names the file by the host as sent (port aside), so
        # each spelling of a name is a file of its own.
        for host, deleted in (("evil.example", 1), ("10.0.0.5:8080", 1), (ONION + ":1234", 0),
                              (ONION, 0), (ONION.upper(), 1), (ONION + ".", 1),
                              ("localhost:8080", 0), ("LOCALHOST:8080", 1), ("127.0.0.1", 0),
                              ("[::1]:8080", 0), ("wordpress", 0), ("", 0)):
            with self.subTest(host=host):
                out = self.php(f"$_SERVER['HTTP_HOST'] = {json.dumps(host)};"
                               "do_action( 'shutdown' ); $out = Cache_Enabler_Disk::$deleted;",
                               setup=self.DISK)
                self.assertEqual(out, deleted)


class TestLinkFixerPolicyPhp(_PhpCase):
    PLUGIN = LINK_FIXER_POLICY

    def setUp(self):
        super().setUp()
        src = _read("app/Resources/plugins/onionpress-tor-proxy.php")
        funcs = []
        for name in ("onionpress_is_local_host", "onionpress_remote_host_allowed"):
            m = re.search(r"^function " + name + r"\(.*?^}\n", src, re.S | re.M)
            funcs.append(m.group(0))
        self.tor_rules = "\n".join(funcs)
        self.EXTRA = self.tor_rules

    IA_HEADERS = "array( 'WP-Wayback-Link-Fixer' => '1.4.2' )"

    def _guard(self, url, body="null", headers=IA_HEADERS, pre="false"):
        out = self.php(f"$r = apply_filters( 'pre_http_request', {pre}, array( 'headers' => {headers},"
                       f" 'body' => {body} ), {json.dumps(url)} );"
                       "$out = $r instanceof WP_Error ? 'refused' : $r;")
        return out

    def test_archive_org_is_asked_about_clearnet_links_only(self):
        base = "https://archive.org/wayback/available/?url="
        check = "https://iabot-api.archive.org/livewebcheck?impersonate=1&url="
        for link in ("https://example.com/a?b=c&d=e", "http://www.torproject.org/",
                     "https://93.184.216.34/"):
            with self.subTest(link=link):
                self.assertEqual(self._guard(base + link), False)
                self.assertEqual(self._guard(check + link), False)
        for link in (f"http://{ONION}/", f"https://{OTHER_ONION}:8080/x", f"http://{ONION.upper()}/",
                     f"http://{ONION}./", "http://localhost:8080/", "http://wordpress/",
                     "http://onionpress-tor:9050/", "http://10.0.0.1/", "http://192.168.1.5/x",
                     "http://127.0.0.1/", "http://[::1]/", "http://printer.local/",
                     "http://intranet/", "not a url", ""):
            with self.subTest(link=link):
                self.assertEqual(self._guard(base + link), "refused")

    def test_save_page_now_posts_are_checked_too(self):
        save = "https://web.archive.org/save/"
        self.assertEqual(self._guard(save, body=f"array( 'url' => 'http://{ONION}/post' )"), "refused")
        self.assertEqual(self._guard(save, body=f"'url=http%3A%2F%2F{ONION}%2F'"), "refused")
        self.assertEqual(self._guard(save, body="array( 'url' => 'https://example.com/' )"), False)
        self.assertEqual(self._guard(save, body="array( 'url' => array( 'x' ) )"), "refused")

    def test_only_link_fixer_requests(self):
        url = f"https://archive.org/wayback/available/?url=http://{ONION}/"
        self.assertEqual(self._guard(url, headers="array()"), False)
        self.assertEqual(self._guard(url, headers="'wp-wayback-link-fixer: 1.4.2'"), "refused")
        self.assertEqual(self._guard(url, headers="array( 'wp-wayback-link-fixer' => 'x' )"), "refused")
        # Job status and system status name no link.
        self.assertEqual(self._guard("https://web-wp.archive.org/save/status/spn2-abc"), False)
        # An earlier answer stands.
        self.assertEqual(self._guard(url, pre="'cached'"), "cached")

    def test_without_the_tor_proxy_rules_nothing_is_asked(self):
        self.EXTRA = ""
        self.assertEqual(self._guard("https://archive.org/wayback/available/?url=https://example.com/"),
                         "refused")

    def test_verified_tls_and_bounded_timeouts(self):
        out = self.php(r"""
$f = function ( $args ) { return apply_filters( 'http_request_args', $args, 'https://web.archive.org/save/' ); };
$h = array( 'WP-Wayback-Link-Fixer' => '1.4.2' );
$out = array(
    $f( array( 'headers' => $h, 'sslverify' => false, 'timeout' => 1000 ) ),
    $f( array( 'headers' => $h, 'timeout' => 10 ) ),
    $f( array( 'headers' => $h, 'timeout' => 45 ) ),
    $f( array( 'headers' => $h, 'timeout' => 5000 ) ),
    $f( array( 'headers' => array(), 'sslverify' => false, 'timeout' => 5 ) ),
);
""")
        self.assertEqual([o.get("sslverify") for o in out], [True, True, True, True, False])
        self.assertEqual([o["timeout"] for o in out], [60, 30, 45, 60, 5])

    def test_the_scan_pace(self):
        out = self.php("$out = array( apply_filters( 'iawmlf_posts_per_batch', 10 ),"
                       " apply_filters( 'iawmlf_scan_posts_interval', 600 ) );")
        self.assertEqual(out, [5, 900])

    def test_an_import_is_left_to_the_scan(self):
        out = self.php(r"""
$GLOBALS['op_state']['meta'] = array( 1 => array( '_source_id' => 'tw:1' ), 3 => array( '_source_id' => 'md:3' ) );
$GLOBALS['op_state']['scanned'] = array( 3 );
$seen = array();
add_action( 'save_post', function ( $id ) use ( &$seen ) {
    $seen[ $id ] = apply_filters( 'pre_option_iawmlf_process_links', false );
}, 10 );
foreach ( array( 1, 2, 3 ) as $id ) { do_action( 'save_post', $id ); }
$out = array( $seen, apply_filters( 'pre_option_iawmlf_process_links', false ) );
""")
        # An imported post, first save: off for the Link Fixer (0); a post
        # of the user's own, or an import already scanned: as configured.
        self.assertEqual(out, [{"1": 0, "2": False, "3": False}, False])


if __name__ == "__main__":
    unittest.main()

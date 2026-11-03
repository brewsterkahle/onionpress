#!/usr/bin/env python3
"""cloudflared starts at the same point on both platforms (#352).

Through 2.5.4 the Linux launcher set CLOUDFLARE_TUNNEL_TOKEN="" and never
read the config, so a token saved on the Settings page sat there unused:
no cloudflared, no error. It now reads the token as the Mac launcher does.

The two launchers then started cloudflared at different points: the Mac at
the very end of every start, installed WordPress or not (its install page,
then an unprovisioned site, on the open internet); Linux right after Tor,
before provisioning, only once installed, and not again until a restart
after setup. Both now run one start_cloudflare_tunnel, held identical here:
only with a token and an installed WordPress, last in a start (after
provisioning and the bundled plugins) and at the end of every setup path;
otherwise the container is removed. Both take it down with the rest.

The behaviour tests run the launcher's own functions in bash with `docker`
stubbed as a shell function, so no daemon is ever contacted.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_config_one_reader import config_value_function  # noqa: E402

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LAUNCHER = os.path.join(PROJECT_ROOT, "linux", "onionpress")
MAC_LAUNCHER = os.path.join(PROJECT_ROOT, "app", "MacOS", "onionpress")
LAUNCHERS = {"linux": LAUNCHER, "mac": MAC_LAUNCHER}
COMPOSE_UP = ("docker compose -f docker-compose.yml -f docker-compose.cloudflare.yml"
              " up -d --no-deps cloudflared")
SETTINGS_PHP = os.path.join(
    PROJECT_ROOT, "app", "Resources", "plugins", "onionpress-settings.php")

# A token ending in `=` padding: `cut -d= -f2` used to drop it (#323).
TOKEN = "eyJhIjoiMTIzNDU2Nzg5MCIsInQiOiJhYmNkZWYiLCJzIjoiWFlaIn0="


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _function(text, name):
    start = text.index(f"\n{name}() {{")
    end = text.index("\n}\n", start)
    return text[start:end] + "\n}\n"


def _case(text, name):
    """A verb's body in the launcher's main case statement."""
    start = text.index(f"\n        {name})\n")
    end = text.index("\n            ;;\n", start)
    return text[start:end]


class TestLauncherWiring(unittest.TestCase):
    def setUp(self):
        self.text = _read(LAUNCHER)

    def test_token_is_read_from_the_config(self):
        body = _function(self.text, "read_cloudflare_token")
        self.assertIn("$(config_value CLOUDFLARE_TUNNEL_TOKEN)", body)
        self.assertNotRegex(self.text, r'(?m)^CLOUDFLARE_TUNNEL_TOKEN=""\s*$',
                            "the token must not be blanked at top level")
        self.assertRegex(self.text, r"(?m)^read_cloudflare_token$")

    def test_the_pages_settings_are_applied_in_their_own_process(self):
        """The page's Restart is handle-action's `apply-config` then
        `restart`, two launcher runs: the restart's top level reads the
        token the apply just wrote. Nothing applies settings in-process."""
        self.assertNotIn("apply_config_updates", self.text)
        self.assertRegex(self.text, r'\n        apply-config\)\n(?:            #.*\n)*'
                                    r'            op_py --data-dir "\$DATA_DIR" apply-config\n')

    def test_start_runs_the_tunnel_after_post_up(self):
        # start.post_up: Tor, the plugins, provisioning (tests/test_start_post_up.py).
        body = _function(self.text, "start_containers")
        self.assertLess(body.index('start_post_up "$wp_installed"'),
                        body.index('start_cloudflare_tunnel "$wp_installed"'))
        # The start's pull takes the Cloudflare file with a token
        # (start.start_core, #442.6; tests/test_start_core.py).
        self.assertIn("start-containers --until-core", body)

    def test_stop_and_uninstall_take_cloudflared_down(self):
        """Always with the override file: a token removed since the last
        start still leaves a running cloudflared on both networks."""
        body = _function(self.text, "stop_containers")
        self.assertIn('docker compose "${COMPOSE_WITH_CF[@]}" down', body)
        self.assertNotRegex(self.text, r"docker compose down\b")
        # The uninstall's `down -v` is uninstall._remove_linux's (#529),
        # with both compose files.
        src = _read("src/onionpress/uninstall.py")
        linux = src[src.index("def _remove_linux("):src.index("def run(")]
        self.assertIn('"docker-compose.cloudflare.yml")]', linux)
        self.assertIn('docker.compose(["down", "-v"], compose_files=files', linux)


class TestOneTimingOnBothPlatforms(unittest.TestCase):
    """Where each launcher starts cloudflared: the same places."""

    def setUp(self):
        self.texts = {name: _read(path) for name, path in LAUNCHERS.items()}

    def test_the_function_is_the_same_in_both_launchers(self):
        bodies = {_function(text, "start_cloudflare_tunnel")
                  for text in self.texts.values()}
        self.assertEqual(len(bodies), 1, "start_cloudflare_tunnel drifted")

    def test_only_the_function_starts_cloudflared(self):
        for name, text in self.texts.items():
            with self.subTest(launcher=name):
                fn = _function(text, "start_cloudflare_tunnel")
                rest = text.replace(fn, "")
                self.assertNotRegex(rest, r"up -d[^\n]*\bcloudflared\b")
                self.assertNotIn("docker stop onionpress-cloudflared", rest)

    def test_start_runs_it_last_after_provisioning(self):
        """The clearnet's first page comes from a provisioned site with the
        cache policy in place, as on the Mac before; and never before the
        install check, which the Mac start used to skip. Provisioning, the
        plugins and the image rebuild are onionpress.start's post_up
        (#442.7), which neither starts nor names cloudflared."""
        steps = {"mac": "start-containers \\", "linux": 'start_post_up "$wp_installed"'}
        for name, text in self.texts.items():
            with self.subTest(launcher=name):
                body = _function(text, "start_containers")
                call = body.index('start_cloudflare_tunnel "$wp_installed"')
                self.assertEqual(body.count("start_cloudflare_tunnel"), 1)
                self.assertLess(body.index(steps[name]), call)
                self.assertLess(body.index("local wp_installed=false"), call)
                self.assertEqual(body[call:].split(), ["start_cloudflare_tunnel",
                                                       '"$wp_installed"', "}"])
        mac = _function(self.texts["mac"], "start_containers")
        # 0 from start-containers is an installed WordPress; 3 is not yet.
        self.assertIn("0) wp_installed=true ;;", mac)
        with open(os.path.join(PROJECT_ROOT, "src", "onionpress", "start.py"),
                  encoding="utf-8") as f:
            text = f.read()
        # (Its pull step names the image's compose file and its image cleanup
        # the mirror's repository, ghcr.io/…/onionpress-cloudflared; neither
        # is the service or the container.)
        self.assertNotIn('"cloudflared"', text)
        self.assertNotRegex(text, r'(?<![/\w-])onionpress-cloudflared\b')

    def test_every_setup_path_starts_it(self):
        """A new site's tunnel needs no restart after setup."""
        for name, text in self.texts.items():
            with self.subTest(launcher=name, verb="provision-post-install"):
                body = _case(text, "provision-post-install")
                self.assertLess(body.index("provision-post-install \\"),
                                body.index("start_cloudflare_tunnel true"))
                self.assertLess(body.index("if wp_is_installed; then"),
                                body.index("start_cloudflare_tunnel true"))
            with self.subTest(launcher=name, verb="setup"):
                body = _case(text, "setup")
                self.assertIn("start_cloudflare_tunnel true", body)
        # `setup` installs through setup_logic.install_fresh_wordpress (#442),
        # which runs provision-post-install (above) and the plugins first.
        for name, text in self.texts.items():
            with self.subTest(launcher=name, verb="setup", after="run_setup"):
                body = _case(text, "setup")
                self.assertLess(body.index('run_setup "$@"'),
                                body.index("start_cloudflare_tunnel true"))

    def test_mac_provisioning_keeps_its_status(self):
        """setup_logic reads provision-post-install's exit status."""
        body = _case(self.texts["mac"], "provision-post-install")
        self.assertIn("|| ppi_rc=$?", body)
        self.assertTrue(body.rstrip().endswith('exit "$ppi_rc"'))

    def test_mac_stop_takes_cloudflared_down(self):
        body = _function(self.texts["mac"], "stop_containers")
        self.assertIn("docker compose -f docker-compose.yml "
                      "-f docker-compose.cloudflare.yml down", body)
        self.assertNotIn("CF_COMPOSE", self.texts["mac"])


class TestTunnelBehaviour(unittest.TestCase):
    """read_cloudflare_token and start_cloudflare_tunnel, run for real."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="op-cf-tunnel-")
        self.log = os.path.join(self.dir, "launcher.log")
        self.calls = os.path.join(self.dir, "docker-calls")
        text = _read(LAUNCHER)
        compose_line = re.search(r"(?m)^COMPOSE_WITH_CF=\(.*\)$", text).group(0)
        self.script = "\n".join([
            "set -e",
            f'DATA_DIR="{self.dir}"',
            f'DOCKER_DIR="{self.dir}"',
            f'LOG_FILE="{self.log}"',
            'log() { echo "$1" >> "$LOG_FILE"; }',
            # `docker container inspect` answers per $CF_EXISTS.
            'docker() {',
            f'    echo "docker $* TOKEN=$CLOUDFLARE_TUNNEL_TOKEN" >> "{self.calls}"',
            '    if [ "$1 $2" = "container inspect" ]; then [ "$CF_EXISTS" = yes ]; fi',
            '}',
            compose_line,
            config_value_function(),
            _function(text, "read_cloudflare_token"),
            _function(text, "start_cloudflare_tunnel"),
        ])

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _run(self, config, installed, exists="no"):
        with open(os.path.join(self.dir, "config"), "w") as f:
            f.write(config)
        script = self.script + (
            "\nread_cloudflare_token"
            f"\nCF_EXISTS={exists} start_cloudflare_tunnel {installed}\n")
        subprocess.run(["bash", "-c", script], check=True)
        calls = _read(self.calls) if os.path.exists(self.calls) else ""
        log = _read(self.log) if os.path.exists(self.log) else ""
        return calls, log

    def test_token_with_padding_starts_cloudflared(self):
        calls, log = self._run(
            f"ADDRESS_PREFIX=op2\nCLOUDFLARE_TUNNEL_TOKEN={TOKEN}\n", "true")
        self.assertIn(f"{COMPOSE_UP} TOKEN={TOKEN}\n", calls)
        self.assertNotIn(TOKEN, log, "the token must never reach the log")

    def test_not_before_wordpress_is_installed(self):
        calls, log = self._run(f"CLOUDFLARE_TUNNEL_TOKEN={TOKEN}\n", "false")
        self.assertNotIn(" up ", calls)
        self.assertNotIn("rm -f", calls)
        self.assertIn("waiting until WordPress is set up", log)

    def test_one_running_before_install_is_removed(self):
        """An older Mac start ran it before setup."""
        calls, _ = self._run(f"CLOUDFLARE_TUNNEL_TOKEN={TOKEN}\n", "false",
                             exists="yes")
        self.assertIn("docker rm -f onionpress-cloudflared", calls)
        self.assertNotIn(" up ", calls)

    def test_removed_token_removes_the_container(self):
        calls, _ = self._run("CLOUDFLARE_TUNNEL_TOKEN=\n", "true", exists="yes")
        self.assertIn("docker rm -f onionpress-cloudflared", calls)
        self.assertNotIn(" up ", calls)

    def test_no_token_no_container_does_nothing(self):
        calls, _ = self._run("ADDRESS_PREFIX=op2\n", "true")
        self.assertNotIn("rm -f", calls)
        self.assertNotIn(" up ", calls)


class TestSettingsPage(unittest.TestCase):
    def test_token_change_asks_for_a_restart(self):
        text = _read(SETTINGS_PHP)
        self.assertIn(
            "$needs_restart = isset( $updates['CLOUDFLARE_TUNNEL_TOKEN'] );", text)
        self.assertNotIn("does not take effect on Linux", text)
        self.assertNotIn("Do NOT install cloudflared on your Mac", text)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Linux runs the Cloudflare Tunnel the Settings page saves a token for (#352).

Through 2.5.4 the Linux launcher set CLOUDFLARE_TUNNEL_TOKEN="" and never
read the config, so a token saved on the Settings page sat there unused:
no cloudflared, no error. It now reads the token as the Mac launcher does,
starts cloudflared once WordPress is installed, removes it when the token
goes, and takes it down with the rest of the stack.

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


class TestLauncherWiring(unittest.TestCase):
    def setUp(self):
        self.text = _read(LAUNCHER)

    def test_token_is_read_from_the_config(self):
        body = _function(self.text, "read_cloudflare_token")
        self.assertIn("$(config_value CLOUDFLARE_TUNNEL_TOKEN)", body)
        self.assertNotRegex(self.text, r'(?m)^CLOUDFLARE_TUNNEL_TOKEN=""\s*$',
                            "the token must not be blanked at top level")
        self.assertRegex(self.text, r"(?m)^read_cloudflare_token$")

    def test_token_is_read_again_after_settings_updates(self):
        """The page's Restart applies the update and starts in one process."""
        body = _function(self.text, "apply_config_updates")
        self.assertLess(body.index("config-updates.json 2>>"),
                        body.index("read_cloudflare_token"))

    def test_start_runs_the_tunnel_after_tor(self):
        body = _function(self.text, "start_containers")
        self.assertLess(body.index("up -d tor onionheaven"),
                        body.index('start_cloudflare_tunnel "$wp_installed"'))
        self.assertIn('docker compose "${CF_COMPOSE[@]}" pull --policy missing',
                      body)

    def test_stop_and_uninstall_take_cloudflared_down(self):
        """Always with the override file: a token removed since the last
        start still leaves a running cloudflared on both networks."""
        body = _function(self.text, "stop_containers")
        self.assertIn('docker compose "${COMPOSE_WITH_CF[@]}" down', body)
        self.assertIn('docker compose "${COMPOSE_WITH_CF[@]}" down -v', self.text)
        self.assertNotRegex(self.text, r"docker compose down\b")

    def test_update_pulls_cloudflared_only_with_a_tunnel(self):
        body = _function(self.text, "update_images")
        self.assertLess(body.index('if [ -n "$CLOUDFLARE_TUNNEL_TOKEN" ]'),
                        body.index("cloudflare/cloudflared:"))


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
        self.assertIn(
            "docker compose -f docker-compose.yml -f docker-compose.cloudflare.yml"
            f" up -d cloudflared TOKEN={TOKEN}\n", calls)
        self.assertNotIn(TOKEN, log, "the token must never reach the log")

    def test_not_before_wordpress_is_installed(self):
        calls, log = self._run(f"CLOUDFLARE_TUNNEL_TOKEN={TOKEN}\n", "false")
        self.assertEqual(calls, "")
        self.assertIn("waiting until WordPress is set up", log)

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

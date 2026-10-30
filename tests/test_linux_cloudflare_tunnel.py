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
        # start.post_up: Tor, then the tunnel (tests/test_start_post_up.py).
        body = _function(self.text, "start_containers")
        self.assertIn('start_post_up "$wp_installed"', body)
        # The start's pull takes the Cloudflare file with a token
        # (start.start_core, #442.6; tests/test_start_core.py).
        self.assertIn("start_core_services || rc=$?", body)

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


# The tunnel's behaviour (start.cloudflare_tunnel since #442.7) is held in
# tests/test_start_post_up.py: TestCloudflareTunnel.


class TestSettingsPage(unittest.TestCase):
    def test_token_change_asks_for_a_restart(self):
        text = _read(SETTINGS_PHP)
        self.assertIn(
            "$needs_restart = isset( $updates['CLOUDFLARE_TUNNEL_TOKEN'] );", text)
        self.assertNotIn("does not take effect on Linux", text)
        self.assertNotIn("Do NOT install cloudflared on your Mac", text)


if __name__ == "__main__":
    unittest.main()

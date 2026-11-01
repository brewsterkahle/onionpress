#!/usr/bin/env python3
"""Linux write_status exports only allowlisted config into WordPress.

write_status used to copy every line of ~/.onionpress/config into the
WordPress container's /var/lib/onionpress/config-current.json, which PHP can
read — secrets such as CLOUDFLARE_TUNNEL_TOKEN included. The Mac app has
always filtered through config.SAFE_CONFIG_KEYS; the Linux launcher now asks
the same code (`onionpress.cli safe-config`) instead of parsing the file
itself.

The status writer (onionpress.status_writer, one for both platforms since
issue #523) writes config-current.json from config.redact_config; its test
runs it against a fake docker, so no daemon is ever contacted.
"""

import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(PROJECT_ROOT, "src")
sys.path.insert(0, SRC)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # test_status_writer's fake

from onionpress import cli  # noqa: E402
from onionpress.config import SAFE_CONFIG_KEYS  # noqa: E402

TOKEN = "eyJhIjoiY2xvdWRmbGFyZS10dW5uZWwtc2VjcmV0In0"

CONFIG = f"""\
ADDRESS_PREFIX=op2
START_ON_BOOT=no
# a comment
CLOUDFLARE_TUNNEL_TOKEN={TOKEN}
SOME_FUTURE_API_KEY=k3y
SHARE_ANALYTICS_WITH_ONIONHOME=yes
ONIONHEAVEN_PROPAGATION_DELAY=240
"""


def _read(rel_path):
    with open(os.path.join(PROJECT_ROOT, rel_path), "r", encoding="utf-8") as f:
        return f.read()


class TestSafeConfigCommand(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="op-safe-config-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _run(self, data_dir):
        buf = io.StringIO()
        # Read-only: none of OnionPressCLI's setup (config/secrets creation,
        # port probing) may run for it, every 30 s, beside a live stack.
        with mock.patch.object(cli, "OnionPressCLI", side_effect=AssertionError("setup ran")), \
                redirect_stdout(buf):
            rc = cli.main(["--data-dir", data_dir, "safe-config"])
        return rc, buf.getvalue()

    def test_prints_only_allowlisted_keys(self):
        with open(os.path.join(self.dir, "config"), "w") as f:
            f.write(CONFIG)
        rc, out = self._run(self.dir)
        self.assertEqual(rc, 0)
        self.assertNotIn(TOKEN, out)
        self.assertNotIn("k3y", out)
        self.assertEqual(json.loads(out), {
            "ADDRESS_PREFIX": "op2",
            "START_ON_BOOT": "no",
            "SHARE_ANALYTICS_WITH_ONIONHOME": "yes",
            "ONIONHEAVEN_PROPAGATION_DELAY": "240",
        })

    def test_missing_config_is_empty_and_not_created(self):
        missing = os.path.join(self.dir, "nowhere")
        rc, out = self._run(missing)
        self.assertEqual((rc, json.loads(out)), (0, {}))
        self.assertFalse(os.path.exists(missing))


class TestStatusWriterConfigCurrent(unittest.TestCase):
    """What the status writer hands WordPress as config-current.json."""

    def setUp(self):
        from test_status_writer import FakeDocker, StatusApp
        from onionpress.platform import OS, resolve_paths
        from onionpress import status_writer
        self.tmp = tempfile.mkdtemp(prefix="op-write-status-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(self.data)
        with open(os.path.join(self.data, "config"), "w") as f:
            f.write(CONFIG)
        self.docker = FakeDocker({
            "ps -a --filter label=com.docker.compose.project":
                "onionpress-tor\trunning\tdocker\nonionpress-wordpress\trunning\tdocker\n"})
        self.app = StatusApp(
            resolve_paths(data_dir=self.data, app_bundle=""), docker=self.docker,
            health=mock.Mock(**{"check_tor_bootstrap.return_value": (False, 0)}),
            version="0.0.0-test", log=lambda m: None, os_name=OS.LINUX)
        self.app.gather()
        self.write = status_writer.write

    def _config_current(self):
        self.write(self.app)
        self.assertIn("config-current.json", self.docker.volume,
                      "the writer wrote no config-current.json")
        return self.docker.volume["config-current.json"][0]

    def test_config_current_holds_only_allowlisted_keys(self):
        raw = self._config_current()
        self.assertNotIn(TOKEN, raw)
        self.assertNotIn("k3y", raw)
        exported = json.loads(raw)
        self.assertLessEqual(set(exported), SAFE_CONFIG_KEYS)
        self.assertEqual(exported["START_ON_BOOT"], "no")
        self.assertEqual(exported["ADDRESS_PREFIX"], "op2")
        self.assertEqual(self.app._last_status_payload["config"], exported)

    def test_an_unreadable_config_is_an_empty_object_never_raw(self):
        os.chmod(os.path.join(self.data, "config"), 0)
        self.addCleanup(os.chmod, os.path.join(self.data, "config"), 0o600)
        if os.access(os.path.join(self.data, "config"), os.R_OK):
            self.skipTest("root reads everything")
        self.assertEqual(json.loads(self._config_current()), {})


class TestSettingsPageRoundTrip(unittest.TestCase):
    """The settings page fills its form from config-current.json and posts
    every field back, so a field whose key is withheld there is reset on
    save, and a restart key it cannot see always looks changed."""

    PHP = "app/Resources/plugins/onionpress-settings.php"
    # Withheld on purpose: a secret, and a switch removed with Arti.
    WITHHELD = {"CLOUDFLARE_TUNNEL_TOKEN", "TOR_IMPL"}

    def setUp(self):
        self.php = _read(self.PHP)

    def test_every_shown_setting_is_exported(self):
        body = self.php[self.php.index("function onionpress_settings_fields()"):
                        self.php.index("function onionpress_settings_page()")]
        fields = set(re.findall(r"^        '([A-Z_]+)' => array\(", body, re.M))
        config_keys = set(re.findall(r"'config_key'\s*=>.*'([A-Z_]+)'\s*:\s*'([A-Z_]+)'", body)[0])
        self.assertIn("START_ON_BOOT", fields)
        missing = (fields | config_keys) - SAFE_CONFIG_KEYS - self.WITHHELD
        self.assertEqual(missing, set(),
                         "settings-page fields missing from SAFE_CONFIG_KEYS are "
                         "reset every time the page is saved")
        self.assertTrue(self.WITHHELD.isdisjoint(SAFE_CONFIG_KEYS))

    def test_restart_keys_are_all_exported(self):
        m = re.search(r"\$restart_keys = array\(([^)]*)\);", self.php)
        keys = set(re.findall(r"'([A-Z_]+)'", m.group(1)))
        self.assertTrue(keys)
        self.assertLessEqual(keys, SAFE_CONFIG_KEYS)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Linux write_status exports only allowlisted config into WordPress.

write_status used to copy every line of ~/.onionpress/config into the
WordPress container's /var/lib/onionpress/config-current.json, which PHP can
read — secrets such as CLOUDFLARE_TUNNEL_TOKEN included. The Mac app has
always filtered through config.SAFE_CONFIG_KEYS; the Linux launcher now asks
the same code (`onionpress.cli safe-config`) instead of parsing the file
itself.

The write_status test runs the launcher's own function in bash with `docker`
stubbed as a shell function, so no daemon is ever contacted.
"""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(PROJECT_ROOT, "src")
sys.path.insert(0, SRC)

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


def _function(text, name):
    # Ends at a column-0 `}` followed by a blank line: write_status embeds
    # `python3 -c` dict literals that close at column 0 too.
    start = text.index(f"\n{name}() {{")
    end = text.index("\n}\n\n", start)
    return text[start:end] + "\n}\n"


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


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class TestLinuxWriteStatus(unittest.TestCase):
    """Run linux/onionpress's write_status() and read back what it wrote
    into the (stubbed) WordPress volume."""

    DOCKER_STUB = r"""
docker() {
    # `docker exec -i onionpress-wordpress sh -c 'cat > /var/lib/onionpress/<f>'`
    # lands in $OUT/<f>; every other docker call fails, as with no daemon.
    if [ "$1" = exec ] && [ "$2" = -i ] && [ "$3" = onionpress-wordpress ] && [ "$4" = sh ]; then
        cat > "$OUT/${6##*/}"
        return 0
    fi
    return 1
}
log() { :; }
wp_is_installed() { return 1; }
"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="op-write-status-")
        self.data = os.path.join(self.tmp, "data")
        self.out = os.path.join(self.tmp, "volume")
        bin_dir = os.path.join(self.tmp, "bin")
        for d in (self.data, self.out, bin_dir):
            os.makedirs(d)
        # The launcher runs `python3`; make that this interpreter, which can
        # import the package (the system python3 may be too old for it).
        os.symlink(sys.executable, os.path.join(bin_dir, "python3"))
        with open(os.path.join(self.data, "config"), "w") as f:
            f.write(CONFIG)
        self.env = {
            "PATH": bin_dir + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": self.tmp,
            "DATA_DIR": self.data,
            "DOCKER_DIR": self.tmp,
            "LOG_FILE": os.path.join(self.tmp, "onionpress.log"),
            "ONIONPRESS_PYTHONPATH": SRC,
            "ONIONPRESS_VERSION": "0.0.0-test",
            "_IN_WRITE_STATUS": "1",
            "OUT": self.out,
            "DOCKER_HOST": "unix:///var/empty/no-docker-for-tests.sock",
        }

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_status(self, extra=""):
        launcher = _read("linux/onionpress")
        script = (_function(launcher, "op_py") + _function(launcher, "write_status")
                  + _function(launcher, "write_recent_logs")
                  + self.DOCKER_STUB + extra + "\nwrite_status\n")
        result = subprocess.run(["bash", "-c", script], env=self.env,
                                capture_output=True, text=True, timeout=120)
        path = os.path.join(self.out, "config-current.json")
        self.assertTrue(os.path.exists(path),
                        f"write_status wrote no config-current.json\n{result.stderr}")
        with open(path) as f:
            return f.read()

    def test_config_current_holds_only_allowlisted_keys(self):
        raw = self._write_status()
        self.assertNotIn(TOKEN, raw)
        self.assertNotIn("k3y", raw)
        exported = json.loads(raw)
        self.assertLessEqual(set(exported), SAFE_CONFIG_KEYS)
        self.assertEqual(exported["START_ON_BOOT"], "no")
        self.assertEqual(exported["ADDRESS_PREFIX"], "op2")

    def test_helper_failure_writes_empty_object_not_raw_config(self):
        raw = self._write_status(extra="op_py() { return 1; }\n")
        self.assertEqual(json.loads(raw), {})


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

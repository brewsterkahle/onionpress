#!/usr/bin/env python3
"""Linux decides OnionHeaven hub mode by address, as the Mac does (issue #345).

linux/onionpress's write_status set status.json's onionheaven.server_active
whenever a container named `onionheaven` was running, and every node runs
one (its heartbeat monitor idles off the hub): the status page said
"Mode: Hub (server)" on every Linux node and the Settings page showed the
hub's takeover tunables everywhere. The Mac daemon decides by the node's
onion address (onionheaven.is_onionheaven_instance); the Linux launcher now
asks the same rule through `onionpress is-onionheaven-hub <address>` and
queries the hub's registry only on the hub.

The write_status test runs the launcher's own function in bash with `docker`
stubbed, as tests/test_linux_safe_config.py does.
"""

import io
import json
import os
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

from onionpress import cli, onionheaven  # noqa: E402

HUB = onionheaven._KNOWN_ONIONHEAVEN_ADDRESS
NODE = "b" * 56 + ".onion"


def _read(rel_path):
    with open(os.path.join(PROJECT_ROOT, rel_path), "r", encoding="utf-8") as f:
        return f.read()


def _function(text, name):
    # Ends at a column-0 `}` followed by a blank line: write_status embeds
    # `python3 -c` dict literals that close at column 0 too.
    start = text.index(f"\n{name}() {{")
    end = text.index("\n}\n\n", start)
    return text[start:end] + "\n}\n"


class TestIsOnionheavenHubCommand(unittest.TestCase):
    """`onionpress is-onionheaven-hub <address>`: the Mac daemon's rule,
    without OnionPressCLI's setup (it runs on every status tick)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="op-is-hub-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _run(self, *argv):
        buf = io.StringIO()
        with mock.patch.object(cli, "OnionPressCLI", side_effect=AssertionError("setup ran")), \
                redirect_stdout(buf):
            return cli.main(["--data-dir", self.dir, "is-onionheaven-hub", *argv])

    def test_the_hub_address_is_the_hub(self):
        self.assertEqual(self._run(HUB), 0)
        self.assertTrue(onionheaven.is_onionheaven_instance(HUB))

    def test_any_other_address_is_a_client(self):
        for address in (NODE, "", "Generating...", HUB.upper(), HUB + "\n"):
            with self.subTest(address=address):
                self.assertEqual(self._run(address), 1)
        self.assertEqual(self._run(), 1)
        self.assertFalse(os.listdir(self.dir), "the command wrote into the data dir")


class TestBothPlatformsDecideByAddress(unittest.TestCase):
    def test_linux_launcher_delegates_to_the_cli(self):
        launcher = _read("linux/onionpress")
        self.assertEqual(
            _function(launcher, "is_onionheaven_hub"),
            '\nis_onionheaven_hub() {\n'
            '    op_py --data-dir "$DATA_DIR" is-onionheaven-hub "$1"\n}\n')
        write_status = _function(launcher, "write_status")
        self.assertIn('if is_onionheaven_hub "$onion_addr"; then', write_status)
        # The old rule: a running onionheaven container, which every node has.
        self.assertNotIn("grep -q '^onionheaven$'", write_status)
        self.assertNotIn("grep -q \"^onionheaven$\"", write_status)

    def test_mac_daemon_uses_the_same_rule(self):
        daemon = _read("src/onionpress/daemon.py")
        self.assertIn("self.is_onionheaven = onionheaven.is_onionheaven_instance(self.onion_address)",
                      daemon)
        self.assertIn("oh_server_active = bool(self.is_onionheaven)", daemon)


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class TestLinuxWriteStatusHubMode(unittest.TestCase):
    """Run linux/onionpress's write_status() with docker stubbed: a node
    serving the hub's address is the hub, any other address a client."""

    DOCKER_STUB = r"""
docker() {
    # The tor container's hostname file: the address the test chose.
    if [ "$1 $2 $3 $4 $5" = "compose exec -T tor cat" ]; then
        printf '%s\n' "$ONION_ADDR"
        return 0
    fi
    # Every node runs an onionheaven container.
    if [ "$1" = "ps" ]; then
        printf 'onionheaven\nonionpress-tor\nonionpress-wordpress\n'
        return 0
    fi
    # The hub's registry query: recorded, then answered.
    if [ "$1 $2 $3" = "exec onionheaven sqlite3" ]; then
        echo "registry-query" >> "$OUT/docker-calls"
        echo "3|2|1"
        return 0
    fi
    # wp_put's `docker exec -i -u www-data onionpress-wordpress sh -c <script>
    # sh /var/lib/onionpress/<f> <mode>` lands in $OUT/<f>; every other
    # docker call fails, as with no daemon.
    if [ "$1 $2 $3 $4 $5 $6 $7" = "exec -i -u www-data onionpress-wordpress sh -c" ]; then
        cat > "$OUT/${10##*/}"
        return 0
    fi
    return 1
}
log() { :; }
wp_is_installed() { return 1; }
wp_cli() { return 1; }
"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="op-hub-mode-")
        self.data = os.path.join(self.tmp, "data")
        self.out = os.path.join(self.tmp, "volume")
        bin_dir = os.path.join(self.tmp, "bin")
        for d in (self.data, self.out, bin_dir):
            os.makedirs(d)
        # The launcher runs `python3`; make that this interpreter, which can
        # import the package (the system python3 may be too old for it).
        os.symlink(sys.executable, os.path.join(bin_dir, "python3"))
        with open(os.path.join(self.data, "config"), "w") as f:
            f.write("ADDRESS_PREFIX=op2\nREGISTER_WITH_ONIONHEAVEN=yes\n")
        with open(os.path.join(self.data, "onionheaven-registration.json"), "w") as f:
            json.dump({"registered": True, "hub": HUB}, f)
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

    def _write_status(self, address):
        launcher = _read("linux/onionpress")
        script = (_function(launcher, "op_py") + _function(launcher, "is_onion_address")
                  + _function(launcher, "is_onionheaven_hub") + _function(launcher, "wp_put")
                  + _function(launcher, "write_status")
                  + _function(launcher, "write_recent_logs")
                  + self.DOCKER_STUB + "\nwrite_status\n")
        result = subprocess.run(["bash", "-c", script], env={**self.env, "ONION_ADDR": address},
                                capture_output=True, text=True, timeout=120)
        path = os.path.join(self.out, "status.json")
        self.assertTrue(os.path.exists(path),
                        f"write_status wrote no status.json\n{result.stderr}")
        with open(path) as f:
            status = json.load(f)
        calls = []
        calls_path = os.path.join(self.out, "docker-calls")
        if os.path.exists(calls_path):
            with open(calls_path) as f:
                calls = f.read().split()
        return status, calls

    def test_a_node_with_the_onionheaven_container_is_a_client(self):
        status, calls = self._write_status(NODE)
        self.assertEqual(status["onion_address"], NODE)
        oh = status["onionheaven"]
        self.assertFalse(oh["server_active"])
        # The client view the status page shows: the hub and the registration.
        self.assertEqual(oh["client_hub"], HUB)
        self.assertTrue(oh["client_registered"])
        self.assertTrue(oh["client_enabled"])
        self.assertEqual((oh["registered_count"], oh["online_count"], oh["taken_over_count"],
                          oh["takeover_containers"]), (0, 0, 0, 0))
        self.assertEqual(calls, [], "the registry was queried off the hub")

    def test_the_hub_address_is_the_hub(self):
        status, calls = self._write_status(HUB)
        oh = status["onionheaven"]
        self.assertTrue(oh["server_active"])
        self.assertEqual((oh["registered_count"], oh["online_count"], oh["taken_over_count"]),
                         (3, 2, 1))
        self.assertEqual(calls, ["registry-query"])

    def test_no_address_is_not_the_hub(self):
        status, calls = self._write_status("")
        self.assertEqual(status["onion_address"], "")
        self.assertFalse(status["onionheaven"]["server_active"])
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()

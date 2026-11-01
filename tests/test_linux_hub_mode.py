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

The status writer (onionpress.status_writer, one for both platforms since
issue #523) decides by address too; its test runs it against a fake docker.
"""

import io
import json
import os
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
        # The status is the writer's (#523): the rule by address there, and
        # never the old one (a running onionheaven container, which every
        # node has) in the launcher.
        writer = _read("src/onionpress/status_writer.py")
        self.assertIn("self.is_onionheaven = onionheaven.is_onionheaven_instance(addr)",
                      _read("src/onionpress/daemon.py"))
        self.assertNotIn("write-status", launcher)
        self.assertNotIn("oh_server_active", launcher)

    def test_mac_daemon_uses_the_same_rule(self):
        daemon = _read("src/onionpress/daemon.py")
        self.assertIn("self.is_onionheaven = onionheaven.is_onionheaven_instance(self.onion_address)",
                      daemon)
        self.assertIn("        status_writer.write(self)\n", daemon)
        self.assertIn("server_active=bool(app.is_onionheaven)", _read("src/onionpress/status_writer.py"))


class TestStatusWriterHubMode(unittest.TestCase):
    """The status writer (the daemon's pass, on both platforms since
    #407) with docker faked: a node serving the hub's address is
    the hub, any other address a client; the registry is asked only on
    the hub, through onionheaven.overview."""

    def setUp(self):
        from test_status_writer import FakeDocker, StatusApp
        from onionpress.platform import OS, resolve_paths
        from onionpress import status_writer
        self.StatusApp = StatusApp
        self.tmp = tempfile.mkdtemp(prefix="op-hub-mode-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(self.data)
        with open(os.path.join(self.data, "config"), "w") as f:
            f.write("ADDRESS_PREFIX=op2\nREGISTER_WITH_ONIONHEAVEN=yes\n")
        with open(os.path.join(self.data, "onionheaven-registration.json"), "w") as f:
            json.dump({"registered": True, "hub": HUB}, f)
        self.FakeDocker = FakeDocker
        self.paths = resolve_paths(data_dir=self.data, app_bundle="")
        self.OS = OS
        self.sw = status_writer

    def _write_status(self, address):
        docker = self.FakeDocker({
            "ps -a --filter label=com.docker.compose.project":
                "onionheaven\trunning\tdocker\nonionpress-tor\trunning\tdocker\n"
                "onionpress-wordpress\trunning\tdocker\n",
            "exec onionpress-tor cat /var/lib/tor/hidden_service/wordpress/hostname": address + "\n",
            "ps --format {{.Names}} --filter name=onionheaven-takeover": "",
        })
        app = self.StatusApp(
            self.paths, docker=docker, health=mock.Mock(**{"check_tor_bootstrap.return_value": (True, 100)}),
            version="0.0.0-test", log=lambda m: None, os_name=self.OS.LINUX)
        calls = []

        def registry(app_, args, timeout=15, input=None):
            calls.append("registry-query")
            return True, "3\t2\t1" if "COUNT" in args[-1] else ""

        app.gather()
        with mock.patch.object(onionheaven, "_run_docker", side_effect=registry):
            self.sw.write(app)
        return json.loads(docker.volume["status.json"][0]), calls

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
        # The counts and the sites (onionheaven.hub_counts / hub_sites), each
        # through the overview: never a query of the writer's own.
        self.assertEqual(set(calls), {"registry-query"})
        self.assertGreaterEqual(len(calls), 2)

    def test_no_address_is_not_the_hub(self):
        status, calls = self._write_status("")
        self.assertEqual(status["onion_address"], "")
        self.assertFalse(status["onionheaven"]["server_active"])
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()

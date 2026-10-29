#!/usr/bin/env python3
"""/__op_proxy.php answers the OnionPress app only.

src/onion-forward.php is copied into the WordPress container as
/__op_proxy.php: an HTTP fetcher for .onion URLs, through the site's own
Tor, for the menubar app's onion proxy (onion_proxy._fetch_via_php). It
used to admit any client address starting "172." -- but onion visitors
reach the same Apache from the onionpress-tor container (Tor -> socat ->
wordpress:80) and Cloudflare Tunnel visitors from cloudflared, all on
onionpress-network. Anyone on Tor could fetch other onion services through
the publisher's Tor.

Now a request needs the token install_php_proxy writes into the copy it
installs (new each app run; the bundled file holds a placeholder that never
matches) and must come from loopback or the container's default gateway,
which is where Docker's published port delivers the Mac's requests.

Nothing here talks to Docker: subprocess and http.client are mocked, and
the PHP runs under the php CLI against a temp copy and a fake route table.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(PROJECT_DIR, "src"))

from onionpress import onion_proxy, wp_files  # noqa: E402

PHP = shutil.which("php")
FORWARDER = os.path.join(PROJECT_DIR, "src", "onion-forward.php")
TOKEN = onion_proxy.PHP_PROXY_TOKEN
PLACEHOLDER = onion_proxy.PHP_PROXY_TOKEN_PLACEHOLDER

# /proc/net/route as the WordPress container sees it on onionpress-network:
# default route via 172.18.0.1 (hex in little-endian host order), and the
# bridge subnet itself.
ROUTE_TABLE = (
    "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
    "eth0\t00000000\t010012AC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
    "eth0\t000012AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
)
GATEWAY = "172.18.0.1"
TOR_CONTAINER = "172.18.0.3"


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class TestBundledSource(unittest.TestCase):

    SRC = _read(FORWARDER)

    def test_one_placeholder_for_the_installer(self):
        self.assertEqual(self.SRC.count(PLACEHOLDER), 1)
        self.assertIn(f"const OP_PROXY_TOKEN = '{PLACEHOLDER}';", self.SRC)

    def test_the_placeholder_can_never_pass_the_token_check(self):
        self.assertIn(r"preg_match('/^[A-Za-z0-9_-]{32,}$/D', OP_PROXY_TOKEN)", self.SRC)
        self.assertNotRegex(PLACEHOLDER, r"^[A-Za-z0-9_-]{32,}$")
        self.assertRegex(TOKEN, r"^[A-Za-z0-9_-]{32,}$")

    def test_no_address_prefix_admits_the_container_network(self):
        self.assertNotIn("str_starts_with($remote, '172.')", self.SRC)
        self.assertIn("hash_equals(OP_PROXY_TOKEN, $token)", self.SRC)

    def test_the_gate_comes_before_anything_else(self):
        gate = self.SRC.index("if (!op_proxy_caller_ok()) {")
        self.assertLess(gate, self.SRC.index("$_SERVER['HTTP_X_ONIONPRESS_URL']"))
        self.assertLess(gate, self.SRC.index("'status'"))
        self.assertLess(gate, self.SRC.index("curl_init("))


@unittest.skipUnless(PHP, "php not installed")
class TestForwarderGate(unittest.TestCase):
    """Run the forwarder under the php CLI. The status action answers
    {"ok":true} past the gate; refused requests print Forbidden."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.route = os.path.join(self.tmp, "route")
        with open(self.route, "w") as f:
            f.write(ROUTE_TABLE)

    def _copy(self, token=TOKEN, route=None):
        src = _read(FORWARDER).replace("'/proc/net/route'", repr(route or self.route))
        if token is not None:
            src = src.replace(PLACEHOLDER, token)
        path = os.path.join(self.tmp, "__op_proxy.php")
        with open(path, "w") as f:
            f.write(src)
        return path

    def _status(self, script, remote, token=TOKEN):
        env = {"PATH": os.environ.get("PATH", ""), "REMOTE_ADDR": remote,
               "HTTP_X_ONIONPRESS_ACTION": "status"}
        if token is not None:
            env["HTTP_X_ONIONPRESS_TOKEN"] = token
        r = subprocess.run([PHP, "-d", "disable_functions=curl_init", script],
                           env=env, capture_output=True, text=True, timeout=30)
        return r.stdout + r.stderr

    def test_the_app_from_the_gateway_or_loopback(self):
        script = self._copy()
        for remote in (GATEWAY, "::ffff:" + GATEWAY, "127.0.0.1", "::1"):
            with self.subTest(remote=remote):
                self.assertEqual(self._status(script, remote), '{"ok":true}')

    def test_onion_and_tunnel_visitors_are_refused(self):
        # The tor container, cloudflared, onionheaven, a takeover worker:
        # every one has its own address on the network, never the gateway.
        script = self._copy()
        for remote in (TOR_CONTAINER, "172.18.0.4", "172.17.0.1", "172.18.0.10",
                       "10.0.2.2", "192.168.1.20", ""):
            with self.subTest(remote=remote):
                self.assertEqual(self._status(script, remote), "Forbidden")

    def test_no_token_or_the_wrong_one_is_refused(self):
        script = self._copy()
        for token in (None, "", TOKEN[:-1], TOKEN + "x", PLACEHOLDER, TOKEN.upper()):
            with self.subTest(token=token):
                self.assertEqual(self._status(script, GATEWAY, token), "Forbidden")

    def test_the_bundled_file_refuses_everyone(self):
        script = self._copy(token=None)
        for token in (None, PLACEHOLDER, TOKEN):
            with self.subTest(token=token):
                self.assertEqual(self._status(script, "127.0.0.1", token), "Forbidden")

    def test_no_route_table_means_loopback_only(self):
        script = self._copy(route=os.path.join(self.tmp, "missing"))
        self.assertEqual(self._status(script, GATEWAY), "Forbidden")
        self.assertEqual(self._status(script, "127.0.0.1"), '{"ok":true}')

    def test_only_the_default_route_gives_the_gateway(self):
        with open(self.route, "w") as f:
            f.write(ROUTE_TABLE.splitlines(True)[0] + ROUTE_TABLE.splitlines(True)[2])
        script = self._copy()
        self.assertEqual(self._status(script, GATEWAY), "Forbidden")
        self.assertEqual(self._status(script, "172.18.0.0"), "Forbidden")


class TestInstall(unittest.TestCase):

    def _install(self, script_path, returncode=0):
        seen = {}

        def fake_run(argv, **kw):
            seen["argv"] = argv
            seen["content"] = kw.get("input", b"").decode("utf-8")
            return subprocess.CompletedProcess(argv, returncode, b"", b"boom")

        logs = []
        with mock.patch.object(wp_files.subprocess, "run", side_effect=fake_run) as run:
            ok = onion_proxy.install_php_proxy("/x/docker", {}, script_path, log_func=logs.append)
        return ok, seen, run, logs

    def test_installs_a_copy_with_this_runs_token(self):
        """Written into the container as www-data (wp_files.put_file): the
        token-bearing copy goes in on stdin and never lands on the Mac's
        disk."""
        ok, seen, _, _ = self._install(FORWARDER)
        self.assertTrue(ok)
        self.assertEqual(seen["argv"],
                         ["/x/docker"] + wp_files.put_file_args("/var/www/html/__op_proxy.php", "644"))
        self.assertEqual(seen["content"],
                         _read(FORWARDER).replace(PLACEHOLDER, TOKEN))
        self.assertNotIn(PLACEHOLDER, seen["content"])

    def test_a_failed_copy_is_reported(self):
        ok, _, _, logs = self._install(FORWARDER, returncode=1)
        self.assertFalse(ok)
        self.assertIn("Failed to install PHP proxy", logs[-1])

    def test_a_script_without_one_placeholder_is_not_installed(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        for content in ("<?php echo 1;", f"<?php '{PLACEHOLDER}'; '{PLACEHOLDER}';"):
            with self.subTest(content=content):
                path = os.path.join(tmp, "f.php")
                with open(path, "w") as f:
                    f.write(content)
                ok, _, run, logs = self._install(path)
                self.assertFalse(ok)
                run.assert_not_called()
                self.assertIn("token placeholder", logs[-1])


class TestCallersSendTheToken(unittest.TestCase):

    def _conn(self):
        conn = mock.MagicMock()
        resp = conn.getresponse.return_value
        resp.status = 200
        resp.read.return_value = b'{"ok": true}'
        resp.getheaders.return_value = [("Content-Type", "text/html")]
        return conn

    def test_fetch_via_php(self):
        conn = self._conn()
        h = onion_proxy.OnionProxyHandler.__new__(onion_proxy.OnionProxyHandler)
        h.server = types.SimpleNamespace(_wp_unreachable_since=None, log_func=None,
                                         wp_port=18080)
        with mock.patch.object(onion_proxy.http.client, "HTTPConnection",
                               return_value=conn) as cls:
            h._fetch_via_php("http://" + "a" * 56 + ".onion/")
        # The server's own port (a second user's +10000), not the module's.
        cls.assert_called_once_with("127.0.0.1", 18080, timeout=60)
        headers = conn.request.call_args.kwargs["headers"]
        self.assertEqual(headers["X-OnionPress-Token"], TOKEN)
        self.assertEqual(conn.request.call_args.args[1], onion_proxy.PHP_PROXY_PATH)

    def test_check_php_proxy(self):
        conn = self._conn()
        with mock.patch.object(onion_proxy.http.client, "HTTPConnection", return_value=conn):
            self.assertTrue(onion_proxy.check_php_proxy())
        headers = conn.request.call_args.kwargs["headers"]
        self.assertEqual(headers["X-OnionPress-Token"], TOKEN)

    def test_check_php_proxy_on_a_given_port(self):
        conn = self._conn()
        with mock.patch.object(onion_proxy.http.client, "HTTPConnection",
                               return_value=conn) as cls:
            self.assertTrue(onion_proxy.check_php_proxy(wp_port=28080))
        self.assertEqual(cls.call_args.args, ("127.0.0.1", 28080))


class TestServerPorts(unittest.TestCase):
    """The server reads its ports from what it was given, at each request
    (issue #476), so a port resync reaches a running proxy."""

    def _server(self, **kwargs):
        server = onion_proxy.ThreadingHTTPServer(
            ("127.0.0.1", 0), onion_proxy.OnionProxyHandler, **kwargs)
        self.addCleanup(server.server_close)
        return server

    def test_given_ports_are_read_live(self):
        from onionpress.ports import PortConfig
        holder = [PortConfig.from_offset(10000)]
        server = self._server(ports=lambda: holder[0])
        self.assertEqual(server.wp_port, 18080)
        holder[0] = PortConfig.from_offset(20000)
        self.assertEqual(server.wp_port, 28080)

    def test_without_ports_the_environment_default(self):
        self.assertEqual(self._server().wp_port, onion_proxy.PHP_PROXY_PORT)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Onion addresses are checked whole, not by "ends in .onion".

An address that ends up in a URL, a path, a command or a registry has to be
a well-formed v3 address and nothing else (key_manager.is_valid_onion_address
and the launchers' is_onion_address). `x.endswith(".onion")` and
`".onion" in x` accept "evil.example/" + 39 characters + ".onion": as the
hub address, that is a URL path on a clearnet host, and the heartbeat POSTs
the site's onion private key to it through a Tor exit. Covered here:

  - the hub address (ONIONHEAVEN_ADDRESS) on the Mac and in the Linux
    heartbeat: a malformed value falls back to the default hub, and the
    request builders refuse one that got through anyway;
  - the site's own content and healthcheck addresses, wherever they are
    read (Tor's hostname files, the launchers' caches);
  - hosts that only route a request (auto-login scope, the local onion
    proxy and its WordPress-side forwarder, /__op_proxy.php): parsed
    first, then checked;
  - WordPress readers that build a link or a redirect from an address
    (directory lookup, Wayback, settings status line, follow page);
  - the tor container: the watchdog's HSFETCH list (control-port commands,
    from a file the WordPress container can write) and OnionHome's own
    address;
  - a scan that keeps loose suffix tests out of the Python sources, bar the
    display-only and decoder sites listed in ALLOWED_SUFFIX_TESTS.

Nothing here talks to Docker or Tor: subprocess is mocked and the scripts'
paths point at temp dirs.
"""

import ast
import importlib.util
import io
import json
import os
import re
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

for fake in ("AppKit", "Foundation", "objc"):
    if fake not in sys.modules:
        sys.modules[fake] = types.ModuleType(fake)

from onionpress import (  # noqa: E402
    auto_login, key_manager, onion_auth, onion_proxy, onionheaven, setup_logic,
)

PHP = shutil.which("php")
HUB = onionheaven.DEFAULT_ONIONHEAVEN_ADDRESS
OTHER_HUB = "hub" + "b" * 53 + ".onion"
SITE = "op2" + "a" * 53 + ".onion"
HC = "vipwsi6bjeychxvdo3xjvojr2yvw7ggwagekemekmeccscryuhehp2id.onion"
# Each of these ends in ".onion" (or holds one), and none is an address.
LOOSE = [
    "evil.example/" + "a" * 39 + ".onion",
    "evil.example:80/" + "a" * 36 + ".onion",
    "evil.example#" + "a" * 43 + ".onion",
    "x\r\nSIGNAL HALT\r\n" + SITE,
    SITE + "\nevil.example",
    SITE.upper(),
    "www." + SITE,
    "x.onion",
    "a" * 62 + ".onion",
]


def _read(rel):
    with open(os.path.join(PROJECT_DIR, rel), encoding="utf-8") as f:
        return f.read()


def _load(rel, name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(PROJECT_DIR, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Recorder:
    """Stands in for subprocess.run: records argv, answers *reply*."""

    def __init__(self, reply=""):
        self.calls = []
        self.reply = reply

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        out = self.reply if kwargs.get("text") else self.reply.encode()
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")


class _FakeApp:
    def __init__(self, tmpdir, onion_address=SITE, healthcheck_address=HC):
        self.onion_address = onion_address
        self.healthcheck_address = healthcheck_address
        self.version = "test"
        self.is_onionheaven = False
        self.app_support = tmpdir
        self.colima_home = os.path.join(tmpdir, "colima")
        self.bin_dir = os.path.join(tmpdir, "bin")
        self.logs = []

    def log(self, msg):
        self.logs.append(msg)

    def read_config_value(self, key, default=""):
        # Opted in to OnionHeaven: these tests are about what goes out once
        # the user has said yes (tests/test_onionheaven_opt_out.py covers no
        # and unset).
        return "yes" if key == "REGISTER_WITH_ONIONHEAVEN" else default


def _patch_keys(test):
    """A key pair whose address is the content address, so the requests get
    as far as curl unless an address check stops them. Returns the address."""
    priv = os.urandom(64)
    pub = onion_auth.derive_public_key(priv)  # sign_expanded takes only a·B
    for name, value in (("extract_keys", (priv, pub)),
                        ("build_openssh_key", b"pem")):
        p = mock.patch.object(key_manager, name, return_value=value)
        p.start()
        test.addCleanup(p.stop)
    return onion_auth.derive_onion_address(pub)


# ─── Mac / shared package: the hub ──────────────────────────────────────


class TestConfiguredHubAddress(unittest.TestCase):

    def test_unset_is_the_default_hub(self):
        for value in ("", "  ", None):
            self.assertEqual(onionheaven.configured_hub_address(value), HUB)

    def test_a_well_formed_address_is_used(self):
        self.assertEqual(onionheaven.configured_hub_address(OTHER_HUB), OTHER_HUB)
        self.assertEqual(onionheaven.configured_hub_address(f" {OTHER_HUB} "), OTHER_HUB)

    def test_anything_else_falls_back_and_is_logged_by_key_name(self):
        for value in LOOSE:
            with self.subTest(value=value):
                logs = []
                self.assertEqual(
                    onionheaven.configured_hub_address(value, logs.append), HUB)
                self.assertEqual(len(logs), 1)
                self.assertIn("ONIONHEAVEN_ADDRESS", logs[0])
                self.assertNotIn("evil", logs[0])
                # A warning that names the hub the key goes to instead.
                self.assertIn("WARNING", logs[0])
                self.assertIn(HUB, logs[0])
                self.assertIn("REGISTER_WITH_ONIONHEAVEN=no", logs[0])
        onionheaven.configured_hub_address(OTHER_HUB)
        self.assertEqual(onionheaven.REJECTED_HUB_ADDRESS, "")

    def test_registration_warns_again_before_the_key_goes_out(self):
        self.addCleanup(setattr, onionheaven, "ONIONHEAVEN_ADDRESS",
                        onionheaven.ONIONHEAVEN_ADDRESS)
        self.addCleanup(setattr, onionheaven, "REJECTED_HUB_ADDRESS", "")
        with tempfile.TemporaryDirectory() as tmp:
            for value, warned in ((LOOSE[0], True), (OTHER_HUB, False), ("", False)):
                with self.subTest(value=value):
                    onionheaven.ONIONHEAVEN_ADDRESS = \
                        onionheaven.configured_hub_address(value)
                    app = _FakeApp(tmp)
                    client = onionheaven.OnionHeavenClient
                    with mock.patch.object(onionheaven, "_check_wordpress_healthy",
                                           return_value=True), \
                            mock.patch.object(client, "send_heartbeat"), \
                            mock.patch.object(onionheaven.time, "sleep"), \
                            mock.patch.object(client, "heartbeat_loop"):
                        onionheaven.register_with_onionheaven(app)
                    hits = [m for m in app.logs if "WARNING" in m and HUB in m]
                    self.assertEqual(bool(hits), warned, app.logs)
                    self.assertTrue(all("evil" not in m for m in app.logs))

    def test_menubar_reads_the_hub_through_it(self):
        # The hub address is read by the daemon (onionpress.daemon, #367),
        # at start and when `config set ONIONHEAVEN_ADDRESS` leaves the marker.
        src = _read("src/onionpress/daemon.py")
        self.assertEqual(src.count("onionheaven.ONIONHEAVEN_ADDRESS = onionheaven.configured_hub_address("), 2)
        self.assertNotIn("onionheaven.ONIONHEAVEN_ADDRESS = oh_addr", src)
        self.assertNotIn("onionheaven.ONIONHEAVEN_ADDRESS =", _read("src/menubar.py"))


class TestHubRequestsRefuseAMalformedHub(unittest.TestCase):
    """ONIONHEAVEN_ADDRESS is a module global any caller can set; every
    request to it is built by _hub_url, which checks it."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="oh-strict-test-")
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.site = _patch_keys(self)
        # A regression would retry with backoff; keep it from taking minutes.
        p = mock.patch.object(onionheaven.time, "sleep")
        p.start()
        self.addCleanup(p.stop)
        self.app = _FakeApp(self.tmpdir, onion_address=self.site)
        with open(os.path.join(self.tmpdir, "onionheaven-registration.json"), "w") as f:
            json.dump({"registered": True, "content_address": self.site}, f)

    def test_a_well_formed_hub_is_posted_to(self):
        rec = _Recorder('{"online": true}')
        with mock.patch.object(onionheaven, "ONIONHEAVEN_ADDRESS", OTHER_HUB), \
                mock.patch.object(onionheaven.subprocess, "run", rec):
            self.assertTrue(onionheaven._send_heartbeat(self.app))
        self.assertEqual(rec.calls[0][-1], f"http://{OTHER_HUB}:8083/online")

    def _assert_nothing_sent(self, fn):
        for hub in LOOSE:
            with self.subTest(hub=hub):
                rec = _Recorder('{"online": true}')
                with mock.patch.object(onionheaven, "ONIONHEAVEN_ADDRESS", hub), \
                        mock.patch.object(onionheaven.subprocess, "run", rec):
                    self.assertFalse(fn())
                self.assertEqual(rec.calls, [])

    def test_hub_url(self):
        with mock.patch.object(onionheaven, "ONIONHEAVEN_ADDRESS", OTHER_HUB):
            self.assertEqual(onionheaven._hub_url("online"),
                             f"http://{OTHER_HUB}:8083/online")
        for hub in LOOSE:
            with mock.patch.object(onionheaven, "ONIONHEAVEN_ADDRESS", hub):
                self.assertIsNone(onionheaven._hub_url("online"))

    def test_heartbeat(self):
        self._assert_nothing_sent(lambda: onionheaven._send_heartbeat(self.app))

    def test_notifications(self):
        self._assert_nothing_sent(
            lambda: onionheaven._send_onionheaven_notification(self.app, "online", "online"))
        self._assert_nothing_sent(
            lambda: onionheaven._send_onionheaven_notification(self.app, "offline", "offline"))

    def test_unregister(self):
        self._assert_nothing_sent(
            lambda: onionheaven.unregister_from_onionheaven(self.app) or False)

    def test_heartbeat_landed(self):
        self._assert_nothing_sent(lambda: onionheaven._heartbeat_landed(self.app, self.site))


class TestOwnAddressesMustBeWhole(unittest.TestCase):
    """The content and healthcheck addresses go into the signed payload,
    the /status/<address> URL and the hub's registry."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="oh-own-test-")
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        with open(os.path.join(self.tmpdir, "onionheaven-registration.json"), "w") as f:
            json.dump({"registered": True, "content_address": LOOSE[0]}, f)

    def test_nothing_is_sent_for_a_malformed_own_address(self):
        site = _patch_keys(self)
        with open(os.path.join(self.tmpdir, "onionheaven-registration.json"), "w") as f:
            json.dump({"registered": True, "content_address": site}, f)
        for field in ("onion_address", "healthcheck_address"):
            for value in LOOSE:
                with self.subTest(field=field, value=value):
                    app = _FakeApp(self.tmpdir, onion_address=site)
                    setattr(app, field, value)
                    rec = _Recorder('{"online": true}')
                    with mock.patch.object(onionheaven.subprocess, "run", rec):
                        self.assertFalse(onionheaven._send_heartbeat(app))
                        self.assertFalse(onionheaven._send_onionheaven_notification(
                            app, "offline", "offline"))
                    self.assertEqual(rec.calls, [])

    def test_unregister_takes_no_malformed_cached_address(self):
        app = _FakeApp(self.tmpdir, onion_address="Starting...")
        rec = _Recorder('{"unregistered": true}')
        with mock.patch.object(onionheaven.subprocess, "run", rec):
            onionheaven.unregister_from_onionheaven(app)
        self.assertEqual(rec.calls, [])
        self.assertIn("OnionHeaven: no content address for unregister", app.logs)


# ─── Linux heartbeat ────────────────────────────────────────────────────

_HB_HOME = tempfile.mkdtemp(prefix="op-hb-strict-test-")


def _load_linux_heartbeat():
    os.makedirs(os.path.join(_HB_HOME, ".onionpress"), exist_ok=True)
    saved = os.environ.get("HOME")
    os.environ["HOME"] = _HB_HOME
    sys.modules.setdefault("key_manager", key_manager)
    sys.modules.setdefault("onion_auth", onion_auth)
    try:
        return _load("linux/onionpress-heartbeat.py", "onionpress_heartbeat_strict_test")
    finally:
        if saved is None:
            del os.environ["HOME"]
        else:
            os.environ["HOME"] = saved


class TestLinuxHeartbeat(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.hb = _load_linux_heartbeat()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_HB_HOME, ignore_errors=True)

    def test_hub_from_config(self):
        hb = self.hb
        self.assertEqual(hb._hub_address({}), hb.DEFAULT_HUB)
        self.assertEqual(hb._hub_address({"ONIONHEAVEN_ADDRESS": ""}), hb.DEFAULT_HUB)
        self.assertEqual(hb._hub_address({"ONIONHEAVEN_ADDRESS": OTHER_HUB}), OTHER_HUB)
        for value in LOOSE:
            with self.subTest(value=value), self.assertLogs(hb.log, "WARNING") as cm:
                hb._bad_hub_value_logged = None
                self.assertEqual(hb._hub_address({"ONIONHEAVEN_ADDRESS": value}),
                                 hb.DEFAULT_HUB)
            self.assertTrue(all("evil" not in line for line in cm.output))
            self.assertTrue(any(hb.DEFAULT_HUB in line for line in cm.output))

    def test_register_warns_again_for_a_rejected_hub(self):
        hb = self.hb
        priv = os.urandom(64)
        pub = onion_auth.derive_public_key(priv)
        hb._bad_hub_value_logged = None
        for value, warned in ((LOOSE[0], True), (OTHER_HUB, False)):
            with self.subTest(value=value):
                with self.assertLogs(hb.log, "WARNING"):
                    hb._hub_address({"ONIONHEAVEN_ADDRESS": value})
                    hb.log.warning("marker")
                hub = hb._hub_address({"ONIONHEAVEN_ADDRESS": value})
                with self.assertLogs(hb.log, "DEBUG") as cm, \
                        mock.patch.object(hb, "read_config",
                                          return_value={"REGISTER_WITH_ONIONHEAVEN": "no"}), \
                        mock.patch.object(hb, "_is_onionheaven_server", return_value=False):
                    self.assertFalse(hb.register(hub, SITE, HC, priv, pub))
                hits = [l for l in cm.output if "WARNING" in l and hb.DEFAULT_HUB in l]
                self.assertEqual(bool(hits), warned, cm.output)

    def test_every_config_read_goes_through_it(self):
        src = _read("linux/onionpress-heartbeat.py")
        self.assertNotIn('config.get("ONIONHEAVEN_ADDRESS", DEFAULT_HUB)', src)
        self.assertEqual(src.count("= _hub_address(config)"), 3)

    def test_sign_and_post_refuses_a_malformed_hub(self):
        priv, pub = os.urandom(64), os.urandom(32)
        for hub in LOOSE:
            with self.subTest(hub=hub):
                rec = _Recorder('{"online": true}')
                with mock.patch.object(self.hb, "_hub_conn", None), \
                        mock.patch.object(self.hb.subprocess, "run", rec):
                    self.assertEqual(
                        self.hb.sign_and_post("online", hub, SITE, HC, priv, pub),
                        (False, None))
                self.assertEqual(rec.calls, [])

    def test_offline_once_takes_no_malformed_cached_address(self):
        hb = self.hb
        p = mock.patch.object(hb.key_manager, "extract_keys",
                              return_value=(os.urandom(64), os.urandom(32)))
        p.start()
        self.addCleanup(p.stop)
        state = os.path.join(_HB_HOME, "state.json")
        hc_cache = os.path.join(hb.DATA_DIR, "healthcheck-address")
        self.addCleanup(lambda: os.path.exists(hc_cache) and os.remove(hc_cache))
        for content, hc in ((LOOSE[0], HC), (SITE, LOOSE[0])):
            with self.subTest(content=content, hc=hc):
                with open(state, "w") as f:
                    json.dump({"content_address": content}, f)
                with open(hc_cache, "w") as f:
                    f.write(hc + "\n")
                rec = _Recorder("")   # the hostname files are empty too
                with mock.patch.object(hb, "STATE_PATH", state), \
                        mock.patch.object(hb, "read_config",
                                          return_value={"REGISTER_WITH_ONIONHEAVEN": "yes"}), \
                        mock.patch.object(hb, "sign_and_post",
                                          side_effect=AssertionError("posted")), \
                        mock.patch.object(hb.subprocess, "run", rec):
                    self.assertEqual(hb.offline_once(), 1)


# ─── Hosts that route a request ─────────────────────────────────────────


class TestAutoLoginScope(unittest.TestCase):

    def test_onion_scope_only_for_a_v3_host(self):
        self.assertEqual(auto_login.scope_for(f"http://{SITE}/alice/"), "onion")
        self.assertEqual(auto_login.scope_for(f"http://{SITE.upper()}/"), "onion")
        for url in ("http://x.onion/", f"http://www.{SITE}/",
                    "http://evil.example/" + "a" * 39 + ".onion",
                    f"http://evil.example/{SITE}", "http://onion/"):
            with self.subTest(url=url):
                self.assertIsNone(auto_login.scope_for(url))


def _proxy_handler(path, command="GET"):
    sent, fetched = [], []
    h = onion_proxy.OnionProxyHandler.__new__(onion_proxy.OnionProxyHandler)
    h.server = types.SimpleNamespace(cache=None, log_func=None, server_port=9077)
    h.path = path
    h.command = command
    h.headers = {"Host": "127.0.0.1:9077"}
    h.rfile = io.BytesIO()
    h.wfile = io.BytesIO()
    h.send_response = lambda *a: sent.append(("response",) + a)
    h.send_header = lambda *a: None
    h.end_headers = lambda: None
    h.send_error = lambda *a: sent.append(("error",) + a)

    def via_php(url, **kw):
        fetched.append(("tor", url))
        return 200, {}, b""

    h._fetch_via_php = via_php
    return h, sent, fetched


class TestOnionProxyRouting(unittest.TestCase):

    def test_onion_host(self):
        self.assertEqual(onion_proxy._onion_host(SITE), (SITE, True, True))
        self.assertEqual(onion_proxy._onion_host(SITE.upper() + "."), (SITE, True, True))
        self.assertEqual(onion_proxy._onion_host("www." + SITE), ("www." + SITE, True, True))
        self.assertEqual(onion_proxy._onion_host("example.com"), ("example.com", False, True))
        for host in ("x.onion", "evil.example?.onion", "a@" + SITE, "onion"):
            with self.subTest(host=host):
                _, is_onion, ok = onion_proxy._onion_host(host)
                self.assertTrue(is_onion)
                self.assertFalse(ok)

    def test_proxy_format_takes_only_a_v3_host(self):
        for path in ("/proxy/evil.example?.onion/x", "/proxy/x.onion/",
                     f"/proxy/evil.example@{SITE}/", f"/proxy/{SITE}:8080/"):
            with self.subTest(path=path):
                h, sent, fetched = _proxy_handler(path)
                h._handle_request()
                self.assertEqual(fetched, [])
                self.assertEqual(sent[0][:2], ("error", 400))
        h, sent, fetched = _proxy_handler(f"/proxy/{SITE}/a/b?c=1")
        h._handle_request()
        self.assertEqual(fetched, [("tor", f"http://{SITE}/a/b?c=1")])

    def test_proxy_format_needs_this_proxys_host(self):
        # Issue #465: a request for another Host (a name a page's DNS
        # points at 127.0.0.1) is refused before the path is looked at.
        for host in (None, "rebind.attacker.example:9077", "127.0.0.1:19077", "127.0.0.1"):
            with self.subTest(host=host):
                h, sent, fetched = _proxy_handler(f"/proxy/{SITE}/x")
                h.headers = {} if host is None else {"Host": host}
                h._handle_request()
                self.assertEqual(fetched, [])
                self.assertEqual(sent[0][:2], ("error", 403))
        for host in ("localhost:9077", "[::1]:9077"):
            with self.subTest(host=host):
                h, sent, fetched = _proxy_handler(f"/proxy/{SITE}/x")
                h.headers = {"Host": host}
                h._handle_request()
                self.assertEqual(fetched, [("tor", f"http://{SITE}/x")])
        # An absolute-form request's Host is its target.
        h, sent, fetched = _proxy_handler(f"http://{SITE}/x")
        h.headers = {"Host": SITE}
        h._handle_request()
        self.assertEqual(fetched, [("tor", f"http://{SITE}/x")])

    def test_an_onion_name_never_goes_out_clearnet(self):
        # A trailing dot or upper case used to read as a clearnet name.
        h, sent, fetched = _proxy_handler(f"http://{SITE.upper()}./x")
        h._handle_request()
        self.assertEqual(fetched, [("tor", f"http://{SITE}/x")])
        h, sent, fetched = _proxy_handler("http://x.onion./")
        h._handle_request()
        self.assertEqual(fetched, [])
        self.assertEqual(sent[0][:2], ("error", 400))
        # And a clearnet name never goes out at all: the proxy carries
        # .onion sites only (it used to fetch these directly, LAN and
        # loopback included).
        h, sent, fetched = _proxy_handler("http://example.com/x")
        h._handle_request()
        self.assertEqual(fetched, [])
        self.assertEqual(sent[0][:2], ("error", 403))

    def test_connect_refuses_every_target(self):
        for target in (f"{SITE}:443", f"{SITE.upper()}.:443", "x.onion:443",
                       "example.com:443", "127.0.0.1:8080"):
            with self.subTest(target=target):
                h, sent, _ = _proxy_handler(target, "CONNECT")
                with mock.patch.object(onion_proxy.socket, "create_connection",
                                       side_effect=OSError("connected")) as conn:
                    h.do_CONNECT()
                conn.assert_not_called()
                self.assertEqual(sent[0][:2], ("error", 403))


class TestPhpForwarder(unittest.TestCase):
    """src/onion-forward.php (served as /__op_proxy.php in WordPress). No
    PHP here, so the checks are on the source."""

    SRC = _read("src/onion-forward.php")

    def test_the_url_is_parsed_and_its_host_checked_whole(self):
        self.assertNotIn(r"preg_match('/\.onion(\/|$|:)/', $url)", self.SRC)
        self.assertIn("parse_url($url)", self.SRC)
        self.assertIn(r"'/^(?:[a-z0-9-]+\.)*[a-z2-7]{56}\.onion$/D', $host", self.SRC)
        self.assertIn("in_array($scheme, ['http', 'https'], true)", self.SRC)

    def test_curl_speaks_only_http(self):
        self.assertIn("CURLOPT_PROTOCOLS       => CURLPROTO_HTTP | CURLPROTO_HTTPS", self.SRC)
        self.assertIn("CURLOPT_REDIR_PROTOCOLS => CURLPROTO_HTTP | CURLPROTO_HTTPS", self.SRC)

    def test_the_host_rule_matches_the_proxy(self):
        php = re.search(r"'/\^(.*)\$/D', \$host", self.SRC).group(1)
        self.assertEqual(php, onion_proxy.ONION_HOST_RE.pattern)

    @unittest.skipUnless(PHP, "php not installed")
    def test_only_onion_urls_reach_curl(self):
        # curl_init is disabled: a URL the check lets through ends in "Call
        # to undefined function curl_init()", one it refuses in the 400
        # text, and nothing reaches the network or the disk. The old check
        # let the first three through, and PHP's curl reads a file:// URL
        # whatever proxy is set. The copy run is an installed one: the
        # bundled file refuses every caller (tests/test_op_proxy_access.py).
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        installed = os.path.join(tmp, "__op_proxy.php")
        with open(installed, "w") as f:
            f.write(self.SRC.replace(onion_proxy.PHP_PROXY_TOKEN_PLACEHOLDER,
                                     onion_proxy.PHP_PROXY_TOKEN))

        def run(url):
            r = subprocess.run(
                [PHP, "-d", "disable_functions=curl_init", installed],
                env={"PATH": os.environ.get("PATH", ""), "REMOTE_ADDR": "127.0.0.1",
                     "HTTP_X_ONIONPRESS_TOKEN": onion_proxy.PHP_PROXY_TOKEN,
                     "HTTP_X_ONIONPRESS_URL": url},
                capture_output=True, text=True, timeout=30)
            return r.stdout + r.stderr

        for url in ("file:///var/www/html/wp-config.php#.onion/",
                    "http://example.com/?.onion/",
                    "http://example.com/x.onion:80",
                    f"http://user@{SITE}/", f"gopher://{SITE}/",
                    "http://x.onion/", f"http://{SITE}.evil.example/", ""):
            with self.subTest(url=url):
                out = run(url)
                self.assertNotIn("curl_init", out)
                self.assertTrue("Only .onion URLs allowed" in out
                                or "Missing X-OnionPress-URL" in out, out)
        for url in (f"http://{SITE}/x?y=1", f"https://www.{SITE}:443/",
                    f"http://{SITE.upper()}./"):
            with self.subTest(url=url):
                self.assertIn("undefined function curl_init()", run(url))


# ─── Setup: the site URL ────────────────────────────────────────────────


class TestSetupSiteAddress(unittest.TestCase):

    def test_site_address(self):
        logs = []
        self.assertEqual(setup_logic._site_address(SITE, logs.append), SITE)
        self.assertEqual(setup_logic._site_address("localhost", logs.append), "localhost")
        self.assertEqual(setup_logic._site_address("", logs.append), "localhost")
        self.assertEqual(logs, [])
        for value in LOOSE:
            self.assertEqual(setup_logic._site_address(value, logs.append), "localhost")
        self.assertEqual(len(logs), len(LOOSE))

    def test_install_never_takes_a_malformed_address_for_the_site_url(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        rec = _Recorder()
        with mock.patch.object(setup_logic.subprocess, "run", rec):
            setup_logic.install_fresh_wordpress(
                site_title="T", onionname="alice", password="hunter22",
                onion_addr=LOOSE[0], launcher_bin="/x/onionpress", data_dir=tmp)
        urls = [a for argv in rec.calls for a in argv if "http://" in a]
        self.assertTrue(urls)
        self.assertTrue(all("evil" not in u for u in urls), urls)
        self.assertIn("--url=http://localhost", rec.calls[0])

    def test_read_onion_address(self):
        for content, want in ((SITE + "\n", SITE), (LOOSE[0] + "\n", "")):
            rec = _Recorder(content)
            with mock.patch.object(setup_logic.subprocess, "run", rec):
                self.assertEqual(setup_logic._read_onion_address(), want)


# ─── Tor container ──────────────────────────────────────────────────────


class TestTorWatchdogHsfetchList(unittest.TestCase):
    """discover_onion_addresses() feeds HSFETCH and GETINFO on the control
    port. It reads the shared onion_address file, which the WordPress
    container can write: a "\\r\\n" in it would add a command of its own."""

    def setUp(self):
        self.wd = _load("app/Resources/docker/tor/tor-watchdog.py", "tor_watchdog_strict_test")
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.wd.HS_BASE_DIR = os.path.join(self.tmp, "hs")
        self.wd.SHARED_ONION_ADDRESS_PATH = os.path.join(self.tmp, "onion_address")

    def _hostname(self, service, content):
        d = os.path.join(self.wd.HS_BASE_DIR, service)
        os.makedirs(d)
        with open(os.path.join(d, "hostname"), "w") as f:
            f.write(content)

    def test_only_whole_addresses(self):
        self._hostname("wordpress", SITE + "\n")
        self._hostname("healthcheck", HC + "\n")
        self._hostname("junk", "x.onion\n")
        with open(self.wd.SHARED_ONION_ADDRESS_PATH, "w") as f:
            f.write("x\r\nSETCONF SocksPort=0.0.0.0:9050\r\nGETINFO " + SITE + "\n")
        got = sorted(self.wd.discover_onion_addresses())
        self.assertEqual(got, sorted([SITE[:-6], HC[:-6]]))

    def test_the_shared_file_counts_when_well_formed(self):
        other = "oth" + "c" * 53 + ".onion"
        with open(self.wd.SHARED_ONION_ADDRESS_PATH, "w") as f:
            f.write(other + "\n")
        self.assertEqual(self.wd.discover_onion_addresses(), [other[:-6]])

    def test_the_path_is_the_shared_volume(self):
        self.assertEqual(
            _load("app/Resources/docker/tor/tor-watchdog.py", "tor_watchdog_path_test")
            .SHARED_ONION_ADDRESS_PATH,
            "/var/lib/onionpress/onion_address")


class TestOnionHomeOwnAddress(unittest.TestCase):

    def test_read_local_onion_address(self):
        names = _load("app/Resources/docker/tor/onionnames.py", "onionnames_strict_test")
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "hostname")
        for content, want in [(SITE + "\n", SITE)] + [(v, None) for v in LOOSE]:
            with self.subTest(content=content):
                with open(path, "w") as f:
                    f.write(content)
                self.assertEqual(names.read_local_onion_address(path), want)


# ─── Launchers ──────────────────────────────────────────────────────────

LAUNCHERS = ("app/MacOS/onionpress", "linux/onionpress")


def _bash_function(text, name):
    start = text.index(f"\n{name}() {{")
    end = text.index("\n}\n", start)
    return text[start:end + 3]


def _bash(script, *args, env=None):
    return subprocess.run(["bash", "-c", script, "bash", *args],
                          capture_output=True, text=True, timeout=10,
                          env={**os.environ, "LC_ALL": "en_US.UTF-8", **(env or {})})


class TestLaunchers(unittest.TestCase):

    def _functions(self, rel, *names):
        text = _read(rel)
        return "".join(_bash_function(text, n) for n in names)

    def test_vanity_key_dir_is_identical_in_both_launchers(self):
        bodies = [_bash_function(_read(p), "vanity_key_dir") for p in LAUNCHERS]
        self.assertEqual(bodies[0], bodies[1])

    def test_vanity_key_dir_takes_only_a_whole_address(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        keys = os.path.join(tmp, "shared", "vanity-keys")
        os.makedirs(os.path.join(keys, "aaa.onion"))
        os.makedirs(os.path.join(keys, "a,source=" + tmp + ",.onion"))
        os.makedirs(os.path.join(tmp, "elsewhere"))
        os.symlink(os.path.join(tmp, "elsewhere"),
                   os.path.join(keys, "b" * 56 + ".onion"))
        for rel in LAUNCHERS:
            fns = self._functions(rel, "is_onion_address", "vanity_key_dir")
            r = _bash(fns + 'DATA_DIR="$1"; vanity_key_dir', tmp)
            self.assertEqual((r.returncode, r.stdout), (1, ""), rel)
            os.makedirs(os.path.join(keys, SITE), exist_ok=True)
            r = _bash(fns + 'DATA_DIR="$1"; vanity_key_dir', tmp)
            self.assertEqual((r.returncode, r.stdout.strip()),
                             (0, os.path.join(keys, SITE)), rel)
            shutil.rmtree(os.path.join(keys, SITE))

    def test_no_launcher_picks_a_key_dir_by_suffix(self):
        for rel in LAUNCHERS:
            self.assertNotIn('find "$DATA_DIR/shared/vanity-keys" -maxdepth 1 -type d -name "*.onion"',
                             _read(rel), rel)

    def _with_fake_docker(self, rel, fn, content):
        fns = self._functions(rel, "is_onion_address", fn)
        script = ('docker() { printf "%s" "$FAKE_HOSTNAME"; }\n'
                  'DOCKER_DIR="$1"; DATA_DIR="$1"\n' + fns + fn + "\n")
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        r = _bash(script, tmp, env={"FAKE_HOSTNAME": content})
        cache = os.path.join(tmp, "healthcheck-address")
        return r, (open(cache).read() if os.path.exists(cache) else None)

    def test_get_onion_address(self):
        for rel in LAUNCHERS:
            r, _ = self._with_fake_docker(rel, "get_onion_address", SITE + "\n")
            self.assertEqual((r.returncode, r.stdout), (0, SITE + "\n"), rel)
            for value in LOOSE:
                with self.subTest(rel=rel, value=value):
                    r, _ = self._with_fake_docker(rel, "get_onion_address", value)
                    self.assertEqual((r.returncode, r.stdout), (1, "Generating...\n"))

    def test_get_healthcheck_address(self):
        for rel in LAUNCHERS:
            r, cache = self._with_fake_docker(rel, "get_healthcheck_address", HC + "\n")
            self.assertEqual((r.returncode, r.stdout, cache), (0, HC + "\n", HC + "\n"), rel)
            for value in LOOSE:
                with self.subTest(rel=rel, value=value):
                    r, cache = self._with_fake_docker(rel, "get_healthcheck_address", value)
                    self.assertEqual((r.returncode, r.stdout, cache), (1, "", None))

    def test_linux_status_carries_only_a_whole_address(self):
        self.assertIn('is_onion_address "$onion_addr" || onion_addr=""',
                      _bash_function(_read("linux/onionpress"), "write_status"))


# ─── WordPress side ─────────────────────────────────────────────────────

STRICT_PHP = r"preg_match( '/^[a-z2-7]{56}\.onion$/D', "


class TestWordPressConsumers(unittest.TestCase):

    @unittest.skipUnless(PHP, "php not installed")
    def test_the_php_rule_agrees_with_python(self):
        values = [SITE, HC] + LOOSE
        r = subprocess.run(
            [PHP, "-r", "foreach (json_decode(stream_get_contents(STDIN)) as $v) "
                        "{ echo " + STRICT_PHP + "$v ) ? '1' : '0'; }"],
            input=json.dumps(values), capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(
            r.stdout,
            "".join("1" if key_manager.is_valid_onion_address(v) else "0" for v in values))
        self.assertTrue(r.stdout.startswith("11"))

    def test_directory_lookup_answer(self):
        src = _read("app/Resources/plugins/onionpress-directory.php")
        body = src[src.index("function onionpress_directory_lookup("):]
        body = body[:body.index("\n}\n")]
        self.assertIn(STRICT_PHP + "$addr )", body)

    def test_wayback_address_file(self):
        src = _read("app/Resources/plugins/onionpress-wayback-archive.php")
        body = src[src.index("function onionpress_wayback_onion_addr("):]
        body = body[:body.index("\n}\n")]
        self.assertIn(STRICT_PHP + "$addr )", body)

    def test_settings_status_link(self):
        src = _read("app/Resources/plugins/onionpress-settings.php")
        self.assertNotIn("strpos( $onion_address, '.onion' )", src)
        self.assertIn(STRICT_PHP + "(string) $onion_address )", src)

    def test_follow_page_query_address(self):
        src = _read("app/Resources/themes/onionpress/page-follow.php")
        self.assertNotIn("sanitize_text_field($_GET['address'])", src)
        self.assertIn(r"preg_match('/^[a-z2-7]{56}\.onion$/D', $onion_addr)", src)


# ─── No loose suffix tests left ─────────────────────────────────────────

# (file, function): sites that may still test a suffix, and why.
ALLOWED_SUFFIX_TESTS = {
    # Display only: shortens an address for a menu title.
    ("src/menubar.py", "_short_onion"),
    ("src/menubar.py", "do_update"),
    ("linux/onionpress-tray", "_update_status_line"),
    # Equality with a fixed address follows.
    ("src/onionpress/onionheaven.py", "is_onionheaven_instance"),
    ("src/onionpress/onionheaven.py", "should_auto_prevent_sleep"),
    # Decoders: length, base32 (and checksum) checks follow; they return
    # key bytes, never the string.
    ("src/onionpress/key_manager.py", "_pubkey_from_hostname"),
    ("src/onionpress/onion_auth.py", "decode_onion_address"),
    ("app/Resources/docker/tor/onion_auth.py", "decode_onion_address"),
    # TLD detection, followed by ONION_HOST_RE.
    ("src/onionpress/onion_proxy.py", "_onion_host"),
}


def _sources():
    for root in ("src", "linux", os.path.join("app", "Resources", "docker", "tor"),
                 os.path.join("app", "MacOS")):
        for dirpath, dirnames, filenames in os.walk(os.path.join(PROJECT_DIR, root)):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in filenames:
                if name.endswith(".py"):
                    yield os.path.relpath(os.path.join(dirpath, name), PROJECT_DIR)
    yield "linux/onionpress-tray"


def _is_onion_suffix(node):
    return isinstance(node, ast.Constant) and node.value in (".onion", "onion")


class _SuffixTests(ast.NodeVisitor):
    def __init__(self):
        self.stack, self.found = [], []

    def _func(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = _func

    def visit_Call(self, node):
        f = node.func
        if (isinstance(f, ast.Attribute) and f.attr == "endswith"
                and node.args and _is_onion_suffix(node.args[0])):
            self.found.append((self.stack[-1] if self.stack else "<module>", node.lineno))
        self.generic_visit(node)

    def visit_Compare(self, node):
        # ".onion" in x, x[-6:] == ".onion"
        if (_is_onion_suffix(node.left) and isinstance(node.ops[0], (ast.In, ast.NotIn))) \
                or (isinstance(node.left, ast.Subscript)
                    and any(_is_onion_suffix(c) for c in node.comparators)):
            self.found.append((self.stack[-1] if self.stack else "<module>", node.lineno))
        self.generic_visit(node)


class TestNoLooseSuffixTests(unittest.TestCase):

    def test_scan(self):
        bad, seen = [], set()
        for rel in _sources():
            v = _SuffixTests()
            v.visit(ast.parse(_read(rel), rel))
            for func, line in v.found:
                if (rel, func) in ALLOWED_SUFFIX_TESTS:
                    seen.add((rel, func))
                else:
                    bad.append(f"{rel}:{line} in {func}()")
        self.assertEqual(bad, [], "use key_manager.is_valid_onion_address (or a "
                                  "fullmatch of the v3 rule) instead")
        # Keep the allowlist honest.
        self.assertEqual(seen, ALLOWED_SUFFIX_TESTS)


if __name__ == "__main__":
    unittest.main()

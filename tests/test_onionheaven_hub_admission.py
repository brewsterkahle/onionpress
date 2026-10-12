#!/usr/bin/env python3
"""Hub admission: only the OnionHeaven hub acts as one.

Port 8083 is published on every node's onion service, and every node runs
the onionheaven container (also the outside vantage point for the app's
reachability probe). Accepting registrations, storing keys, taking
addresses over and running takeover workers belong to the hub alone.

Pinned here, with no Docker and no Tor (subprocess.run is patched wherever
a docker call could otherwise happen):
  - the rule: the canonical hub address, or an exact "yes" opt-in;
  - the heartbeat monitor never opens the registry before admission;
  - web-server.py honours is_onionheaven only with its own signature, and
    the activation marker follows admission, never a registration;
  - the Linux client takes its claim from config, never the marker, and
    signs it under the label the hub checks;
  - the opt-in reaches both containers from every compose caller.
The HTTP behaviour is covered in test_onionheaven_integration.py.
"""

import hashlib
import importlib.util
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TESTS_DIR)
DOCKER_TOR_DIR = os.path.join(PROJECT_DIR, "app", "Resources", "docker", "tor")
sys.path.insert(0, os.path.join(PROJECT_DIR, "src"))
sys.path.insert(0, DOCKER_TOR_DIR)

import onionheaven_common as oc  # noqa: E402
from onionpress import key_manager, onion_auth  # noqa: E402
from onionpress import onionheaven as client_onionheaven  # noqa: E402

HUB = "oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion"
OTHER = "op2w5bhnczf4ryaxldryql7guydj2r52xhbj6o6kjscji5pruzwqyxad.onion"
HC = "vipwsi6bjeychxvdo3xjvojr2yvw7ggwagekemekmeccscryuhehp2id.onion"

OPT_IN = "ONIONHEAVEN_ACCEPT_REGISTRATIONS"


def _read(rel):
    with open(os.path.join(PROJECT_DIR, rel)) as fh:
        return fh.read()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _keypair():
    """(expanded_key_64, public_key_32, onion_address) for a random key."""
    h = bytearray(hashlib.sha512(os.urandom(32)).digest())
    h[0] &= 248
    h[31] &= 127
    h[31] |= 64
    a = int.from_bytes(bytes(h[:32]), "little")
    pub = onion_auth._encode_point(onion_auth._scalar_mult(a, onion_auth._B))
    return bytes(h), pub, onion_auth.derive_onion_address(pub)


ws = _load("web_server_hub_admission",
           os.path.join(DOCKER_TOR_DIR, "web-server.py"))
hbm = _load("onionheaven_heartbeat_hub_admission",
            os.path.join(DOCKER_TOR_DIR, "onionheaven-heartbeat.py"))

# linux/onionpress-heartbeat.py opens a log file under $HOME/.onionpress at
# import, and imports onion_auth / key_manager flat from /opt/onionpress.
_LINUX_HOME = tempfile.mkdtemp(prefix="op-hub-admission-")
os.makedirs(os.path.join(_LINUX_HOME, ".onionpress"))


def _load_linux_heartbeat():
    saved_home = os.environ.get("HOME")
    os.environ["HOME"] = _LINUX_HOME
    sys.modules.setdefault("key_manager", key_manager)
    sys.modules.setdefault("onion_auth", onion_auth)
    try:
        return _load("onionpress_heartbeat_hub_admission",
                     os.path.join(PROJECT_DIR, "linux", "onionpress-heartbeat.py"))
    finally:
        if saved_home is None:
            del os.environ["HOME"]
        else:
            os.environ["HOME"] = saved_home


linux_hb = _load_linux_heartbeat()


def _no_docker(*_a, **_kw):
    raise AssertionError("no docker call may happen in these tests")


class AdmissionRuleTest(unittest.TestCase):

    def test_canonical_hub_address_matches_the_client_constants(self):
        self.assertEqual(oc.ONIONHEAVEN_HUB_ADDRESS, HUB)
        self.assertEqual(client_onionheaven._KNOWN_ONIONHEAVEN_ADDRESS, HUB)
        self.assertEqual(linux_hb.DEFAULT_HUB, HUB)

    def test_only_the_hub_address_admits_without_opt_in(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(OPT_IN, None)
            self.assertTrue(oc.is_registration_hub(HUB))
            self.assertFalse(oc.is_registration_hub(OTHER))
            self.assertFalse(oc.is_registration_hub(None))
            self.assertFalse(oc.is_registration_hub(""))

    def test_only_an_exact_yes_opts_in(self):
        for value in ("yes", "YES", " yes "):
            with mock.patch.dict(os.environ, {OPT_IN: value}):
                self.assertTrue(oc.registrations_opted_in(), value)
                self.assertTrue(oc.is_registration_hub(OTHER), value)
                self.assertTrue(oc.is_registration_hub(None), value)
        for value in ("", "no", "1", "true", "y", "yes please"):
            with mock.patch.dict(os.environ, {OPT_IN: value}):
                self.assertFalse(oc.registrations_opted_in(), repr(value))
                self.assertFalse(oc.is_registration_hub(OTHER), repr(value))

    def test_web_server_uses_its_own_hostname(self):
        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(ws, "_HOSTNAME_PATH", "/nonexistent/hostname"):
            os.environ.pop(OPT_IN, None)
            for own, expected in ((HUB, True), (OTHER, False), (None, False)):
                with mock.patch.object(ws, "OWN_ONION_ADDRESS", own):
                    self.assertIs(ws._accepts_registrations(), expected, own)


class ReadTorContainerAddressTest(unittest.TestCase):

    def _run(self, **kw):
        result = subprocess.CompletedProcess(args=[], returncode=kw.get("rc", 0),
                                             stdout=kw.get("out", ""), stderr="")
        with mock.patch.object(oc.subprocess, "run", return_value=result) as run:
            addr = oc.read_tor_container_address()
        return addr, run

    def test_reads_the_hostname_from_the_tor_container(self):
        addr, run = self._run(out=HUB + "\n")
        self.assertEqual(addr, HUB)
        argv = run.call_args[0][0]
        self.assertEqual(argv[:3], ["docker", "exec", "onionpress-tor"])
        self.assertEqual(argv[-1], oc.OWN_HOSTNAME_PATH)

    def test_any_failure_is_unknown(self):
        self.assertIsNone(self._run(rc=1, out=HUB)[0])
        self.assertIsNone(self._run(out="not an onion address")[0])
        with mock.patch.object(oc.subprocess, "run", side_effect=OSError("no docker")):
            self.assertIsNone(oc.read_tor_container_address())


class _Stop(Exception):
    pass


class HeartbeatMonitorAdmissionTest(unittest.TestCase):

    def setUp(self):
        self.logs = []
        for p in (mock.patch.object(hbm, "log", self.logs.append),
                  mock.patch.object(hbm.subprocess, "run", side_effect=_no_docker),
                  mock.patch.dict(os.environ, {}, clear=False)):
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop(OPT_IN, None)

    def test_hub_is_admitted_at_once(self):
        sleep = mock.Mock(side_effect=_Stop)
        self.assertEqual(hbm.wait_for_hub_admission(lambda: HUB, sleep=sleep), HUB)
        sleep.assert_not_called()

    def test_waits_for_the_address_then_admits(self):
        answers = iter([None, None, HUB])
        sleeps = []
        own = hbm.wait_for_hub_admission(lambda: next(answers),
                                         sleep=sleeps.append, clock=lambda: 0)
        self.assertEqual(own, HUB)
        self.assertEqual(sleeps, [hbm.HUB_ADMISSION_RETRY] * 2)

    def test_opt_in_admits_any_node(self):
        os.environ[OPT_IN] = "yes"
        self.assertIsNone(hbm.wait_for_hub_admission(lambda: None, sleep=_no_docker))
        self.assertEqual(hbm.wait_for_hub_admission(lambda: OTHER, sleep=_no_docker), OTHER)

    def test_other_node_idles_and_keeps_its_log_fresh(self):
        now = [0.0]
        sleeps = []
        logged_at = []

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 10:
                raise _Stop
            now[0] += seconds

        def log(msg):
            self.assertIn("not an OnionHeaven hub", msg)
            logged_at.append(now[0])

        with mock.patch.object(hbm, "log", log), self.assertRaises(_Stop):
            hbm.wait_for_hub_admission(lambda: OTHER, sleep=sleep, clock=lambda: now[0])
        self.assertEqual(set(sleeps), {hbm.HUB_ADMISSION_RECHECK})
        # Throttled, yet never quiet for the 300 s after which the
        # entrypoint's watchdog would kill and restart the monitor.
        self.assertLess(len(logged_at), len(sleeps))
        gaps = [b - a for a, b in zip(logged_at, logged_at[1:])] + [now[0] - logged_at[-1]]
        self.assertLess(max(gaps), 300, logged_at)

    def test_main_never_opens_the_registry_before_admission(self):
        with mock.patch.object(hbm, "wait_for_hub_admission", side_effect=_Stop), \
                mock.patch.object(hbm, "db_connect",
                                  side_effect=AssertionError("registry opened")), \
                mock.patch.object(hbm.os.path, "isdir", return_value=True):
            with self.assertRaises(_Stop):
                hbm.main()

    def test_startup_drops_stored_peer_claims(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        oc.db_ensure_schema(conn)
        conn.execute(
            "INSERT INTO registry (content_address, healthcheck_address, "
            "registered_at, status, is_onionheaven) "
            "VALUES (?, ?, '2026-01-01T00:00:00Z', 'online', 1)", (OTHER, HC))
        conn.commit()
        with mock.patch.object(hbm, "cleanup_dead_workers"):
            hbm.startup_reconciliation(conn)
        self.assertEqual(
            conn.execute("SELECT is_onionheaven FROM registry").fetchone()[0], 0)


class PeerClaimTest(unittest.TestCase):
    """is_onionheaven counts at the hub only with its own signature."""

    def setUp(self):
        self.priv, self.pub, self.addr = _keypair()
        self.ts = onion_auth.make_timestamp()

    def _payload(self, **extra):
        data = {
            "content_address": self.addr,
            "healthcheck_address": HC,
            "timestamp": self.ts,
            "signature": onion_auth.sign_payload(
                self.priv, self.pub, "online", self.addr, HC, self.ts),
        }
        data.update(extra)
        return data

    def _sign(self, endpoint, priv=None, pub=None, hc=HC):
        return onion_auth.sign_payload(priv or self.priv, pub or self.pub,
                                       endpoint, self.addr, hc, self.ts)

    def test_no_claim(self):
        self.assertFalse(ws._signed_peer_claim(self._payload()))

    def test_bare_claim_is_ignored(self):
        self.assertFalse(ws._signed_peer_claim(self._payload(is_onionheaven=True)))

    def test_signed_claim_counts(self):
        data = self._payload(is_onionheaven=True,
                             onionheaven_peer_signature=self._sign(ws.PEER_CLAIM_ENDPOINT))
        self.assertTrue(ws._signed_peer_claim(data))

    def test_signature_for_anything_else_does_not(self):
        other_priv, other_pub, _ = _keypair()
        for sig in (self._sign("online"),
                    self._sign(ws.PEER_CLAIM_ENDPOINT, other_priv, other_pub),
                    self._sign(ws.PEER_CLAIM_ENDPOINT, hc=OTHER)):
            data = self._payload(is_onionheaven=True, onionheaven_peer_signature=sig)
            self.assertFalse(ws._signed_peer_claim(data))

    def test_peer_label_is_not_an_endpoint(self):
        self.assertNotIn(ws.PEER_CLAIM_ENDPOINT,
                         ("online", "register", "offline", "unregister", "logs"))


class ActivateMarkerTest(unittest.TestCase):
    """The marker follows hub admission; /online no longer writes it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="op-activate-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.marker = os.path.join(self.tmp, "activate")
        for p in (mock.patch.object(ws, "ACTIVATE_MARKER", self.marker),
                  mock.patch.object(ws, "_HOSTNAME_PATH", "/nonexistent/hostname"),
                  mock.patch.object(ws, "log"),
                  mock.patch.dict(os.environ, {}, clear=False)):
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop(OPT_IN, None)

    def test_written_on_the_hub(self):
        with mock.patch.object(ws, "OWN_ONION_ADDRESS", HUB):
            ws._sync_activate_marker()
        self.assertTrue(os.path.exists(self.marker))

    def test_stale_marker_removed_elsewhere(self):
        with open(self.marker, "w") as f:
            f.write("2026-01-01T00:00:00Z\n")
        with mock.patch.object(ws, "OWN_ONION_ADDRESS", OTHER):
            ws._sync_activate_marker()
        self.assertFalse(os.path.exists(self.marker))

    def test_waits_for_the_address(self):
        calls = []

        def sleep_then_learn(_seconds):
            calls.append(1)
            ws.OWN_ONION_ADDRESS = OTHER

        with open(self.marker, "w") as f:
            f.write("x\n")
        with mock.patch.object(ws, "OWN_ONION_ADDRESS", None), \
                mock.patch("time.sleep", sleep_then_learn):
            ws._sync_activate_marker(poll_seconds=0)
        self.assertEqual(len(calls), 1)
        self.assertFalse(os.path.exists(self.marker))

    def test_online_handler_does_not_write_it(self):
        src = _read("app/Resources/docker/tor/web-server.py")
        body = src[src.index("    def _handle_online(self):"):
                   src.index("    def _handle_offline(self):")]
        self.assertNotIn('"activate"', body)
        self.assertNotIn("ACTIVATE_MARKER", body)

    def test_hub_only_endpoints_refuse_before_parsing_the_body(self):
        src = _read("app/Resources/docker/tor/web-server.py")
        for name, first_action in (("_handle_online", "self._read_json()"),
                                   ("_handle_offline", "self._read_json()"),
                                   ("_handle_unregister", "self._read_json()"),
                                   ("_handle_reset_onionheaven", "_RESET_LOCK.acquire")):
            start = src.index(f"    def {name}(self):")
            body = src[start:src.index("\n    def ", start + 1)]
            with self.subTest(handler=name):
                self.assertLess(body.index("self._refuse_unless_hub()"),
                                body.index(first_action))


class LinuxClientClaimTest(unittest.TestCase):

    def setUp(self):
        self.priv, self.pub, self.addr = _keypair()
        self.tmp = tempfile.mkdtemp(prefix="op-linux-claim-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = os.path.join(self.tmp, "config")
        for p in (mock.patch.object(linux_hb, "CONFIG_PATH", self.config),
                  mock.patch.object(linux_hb, "docker_exec", side_effect=_no_docker)):
            p.start()
            self.addCleanup(p.stop)

    def _config(self, text):
        with open(self.config, "w") as f:
            f.write(text)

    def test_label_matches_the_hub(self):
        self.assertEqual(linux_hb.PEER_CLAIM_ENDPOINT, ws.PEER_CLAIM_ENDPOINT)

    def test_claim_comes_from_config_not_the_marker(self):
        self._config("REGISTER_WITH_ONIONHEAVEN=yes\n")
        self.assertFalse(linux_hb._is_onionheaven_server())
        self._config("ONIONHEAVEN_ACCEPT_REGISTRATIONS=yes\n")
        self.assertTrue(linux_hb._is_onionheaven_server())
        self._config("ONIONHEAVEN_ACCEPT_REGISTRATIONS=no\n")
        self.assertFalse(linux_hb._is_onionheaven_server())
        self.assertNotIn("onionheaven/activate", _read("linux/onionpress-heartbeat.py"))

    def test_claim_is_signed_and_accepted_by_the_hub(self):
        payload = linux_hb._build_signed_payload(
            "online", self.addr, HC, self.priv, self.pub,
            extra={"is_onionheaven": True})
        self.assertIn("onionheaven_peer_signature", payload)
        ok, err = onion_auth.verify_payload(
            self.addr, "online", HC, payload["timestamp"], payload["signature"])
        self.assertTrue(ok, err)
        self.assertTrue(ws._signed_peer_claim(payload))

    def test_no_claim_no_peer_signature(self):
        for extra in (None, {"is_onionheaven": False}, {"wordpress_healthy": True}):
            payload = linux_hb._build_signed_payload(
                "online", self.addr, HC, self.priv, self.pub, extra=extra)
            self.assertNotIn("onionheaven_peer_signature", payload)


class OptInPlumbingTest(unittest.TestCase):
    """Every compose caller hands the opt-in to both containers."""

    LINE = "- ONIONHEAVEN_ACCEPT_REGISTRATIONS=${ONIONHEAVEN_ACCEPT_REGISTRATIONS:-}"

    def _service(self, compose, name):
        start = compose.index(f"\n  {name}:\n")
        m = re.compile(r"\n  [a-z][a-z0-9_-]*:\n").search(compose, start + 1)
        return compose[start:m.start() if m else len(compose)]

    def test_compose_passes_it_to_tor_and_onionheaven(self):
        compose = _read("app/Resources/docker/docker-compose.yml")
        for name in ("tor", "onionheaven"):
            with self.subTest(service=name):
                self.assertIn(self.LINE, self._service(compose, name))
        self.assertNotIn(self.LINE, self._service(compose, "wordpress"))

    def test_launchers_export_it_from_config(self):
        for rel in ("app/MacOS/onionpress", "linux/onionpress"):
            text = _read(rel)
            with self.subTest(launcher=rel):
                self.assertIn('grep "^ONIONHEAVEN_ACCEPT_REGISTRATIONS="', text)
                self.assertIn("export ONIONHEAVEN_ACCEPT_REGISTRATIONS", text)

    def test_python_compose_env_sets_it(self):
        from onionpress import containers
        tmp = tempfile.mkdtemp(prefix="op-optin-env-")
        self.addCleanup(shutil.rmtree, tmp, True)
        config = os.path.join(tmp, "config")
        paths = types.SimpleNamespace(config_file=config, documents_dir=tmp)
        ports = types.SimpleNamespace(wp_port=8080, socks_port=9050,
                                      proxy_port=9077, offset=0)
        mgr = containers.ContainerManager(docker=None, paths=paths, port_config=ports)
        with mock.patch.object(mgr, "_secrets_env", return_value={}):
            self.assertEqual(mgr._build_env()[OPT_IN], "")
            with open(config, "w") as f:
                f.write("ONIONHEAVEN_ACCEPT_REGISTRATIONS=yes\n")
            self.assertEqual(mgr._build_env()[OPT_IN], "yes")

    def test_validators_flag_a_node_that_is_not_a_hub(self):
        for rel in ("app/MacOS/onionpress", "linux/onionpress"):
            with self.subTest(launcher=rel):
                self.assertIn("'\"accepting_registrations\": *false'", _read(rel))


if __name__ == "__main__":
    unittest.main(verbosity=2)

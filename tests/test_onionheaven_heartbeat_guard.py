#!/usr/bin/env python3
"""Unit test for the staleness guard in onionheaven._send_heartbeat.

The guard derives the v3 onion address from the live keystore's public key
and aborts if it doesn't match `app.onion_address`. Without this guard, a
heartbeat fired during a restore (between when write_private_key swaps the
keystore and when update_status next refreshes self.onion_address) would
ship (content_address=OLD, arti_key_pem=NEW) to OnionHeaven's /online,
which would clobber KEYS_DIR/OLD/ with NEW's bytes — irrecoverable loss
of the old address's takeover key.

We stub the two read sources (app.onion_address and key_manager.extract_keys)
plus the docker-exec call, then drive _send_heartbeat in three modes:
  A) onion_address matches the key    → proceeds, curl called once
  B) onion_address stale vs key       → guard fires, curl NOT called
  C) extract_keys raises              → exception path, curl NOT called
"""
import os
import secrets
import sys
import tempfile
import types
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(PROJECT_DIR, "src"))

# menubar.py (not imported here) pulls PyObjC; onionheaven.py doesn't, but
# stub these defensively in case the import graph shifts.
for fake in ("AppKit", "Foundation", "objc"):
    if fake not in sys.modules:
        sys.modules[fake] = types.ModuleType(fake)

from onionpress import key_manager, onion_auth, onionheaven  # noqa: E402


class _FakeApp:
    def __init__(self, onion_address, app_support):
        self.onion_address = onion_address
        self.healthcheck_address = (
            "vipwsi6bjeychxvdo3xjvojr2yvw7ggwagekemekmeccscryuhehp2id.onion"
        )
        self.version = "test"
        self.is_onionheaven = False
        self.app_support = app_support
        self.logs = []

    def log(self, msg):
        self.logs.append(msg)


class HeartbeatGuardTest(unittest.TestCase):
    def setUp(self):
        # Fabricate a key. derive_onion_address only uses the 32-byte public
        # key, so we don't need a real ed25519 expansion.
        self.priv_64 = secrets.token_bytes(64)
        self.pub_32 = secrets.token_bytes(32)
        self.matching_addr = onion_auth.derive_onion_address(self.pub_32)

        # Counter for stubbed curl invocations
        self.curl_calls = 0
        self.tmpdir = tempfile.mkdtemp(prefix="oh-guard-test-")

        # Save originals to restore on tearDown
        self._orig_run_docker_rc = onionheaven._run_docker_rc
        self._orig_extract_keys = key_manager.extract_keys
        self._orig_build = key_manager.build_openssh_key
        self._orig_wp_healthy = onionheaven._check_wordpress_healthy

        def fake_run_docker_rc(app, args, timeout=None, input=None):
            self.curl_calls += 1
            return 0, '{"online":true,"registered":true}'
        onionheaven._run_docker_rc = fake_run_docker_rc

        def fake_extract_keys():
            return (self.priv_64, self.pub_32)
        key_manager.extract_keys = fake_extract_keys

        def fake_build_openssh_key(priv, pub):
            return (b"-----BEGIN OPENSSH PRIVATE KEY-----\n"
                    b"stub\n"
                    b"-----END OPENSSH PRIVATE KEY-----\n")
        key_manager.build_openssh_key = fake_build_openssh_key

        onionheaven._check_wordpress_healthy = lambda app: True

    def tearDown(self):
        onionheaven._run_docker_rc = self._orig_run_docker_rc
        key_manager.extract_keys = self._orig_extract_keys
        key_manager.build_openssh_key = self._orig_build
        onionheaven._check_wordpress_healthy = self._orig_wp_healthy

    def test_matching_addr_proceeds(self):
        app = _FakeApp(self.matching_addr, self.tmpdir)
        onionheaven._send_heartbeat(app)
        self.assertEqual(self.curl_calls, 1,
                         f"matching addr should call curl once, "
                         f"got {self.curl_calls}. logs={app.logs}")
        self.assertFalse(any("ABORT heartbeat" in l for l in app.logs),
                         f"matching addr must not log ABORT. logs={app.logs}")

    def test_stale_addr_aborts(self):
        # An address we know cannot derive from a random 32-byte public key
        stale_addr = (
            "op2hecfkpduw2iou5aiul3tbg57tnkxqgwgq4uht4vyedanvbcv2wwqd.onion"
        )
        self.assertNotEqual(stale_addr, self.matching_addr,
                            "test setup collision: random key hashed to op2hecf")
        app = _FakeApp(stale_addr, self.tmpdir)
        onionheaven._send_heartbeat(app)
        self.assertEqual(self.curl_calls, 0,
                         f"stale addr must NOT invoke curl, "
                         f"got {self.curl_calls}. logs={app.logs}")
        abort_lines = [l for l in app.logs if "ABORT heartbeat" in l]
        self.assertEqual(len(abort_lines), 1,
                         f"stale addr should log exactly one ABORT. "
                         f"logs={app.logs}")
        line = abort_lines[0]
        self.assertIn(stale_addr, line,
                      f"ABORT should name the stale addr. line={line}")
        self.assertIn(self.matching_addr, line,
                      f"ABORT should name the derived addr. line={line}")

    def test_extract_keys_failure_aborts(self):
        def boom():
            raise RuntimeError("simulated extract_keys failure")
        key_manager.extract_keys = boom
        app = _FakeApp(self.matching_addr, self.tmpdir)
        onionheaven._send_heartbeat(app)
        self.assertEqual(self.curl_calls, 0,
                         f"extract_keys failure must NOT invoke curl, "
                         f"got {self.curl_calls}. logs={app.logs}")
        self.assertTrue(any("sign error" in l for l in app.logs),
                        f"expected 'sign error' in logs. logs={app.logs}")


if __name__ == "__main__":
    unittest.main()

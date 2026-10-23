"""Unit tests for the stress test orchestrator's signed payloads.

tests/stress/orchestrator/notifications.py signs /offline, /online and
/unregister payloads for every worker the WorkerInfoStore holds. The
store reads its rows out of a SQLite file in a Docker volume through one
`docker exec`; here a fake Docker answers that exec with the rows as
JSON, so nothing here needs Docker. The signatures are libsodium's
(onion_auth), so the signing tests need PyNaCl.
"""

import base64
import hashlib
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from onionpress.docker import DockerResult  # noqa: E402
from onionpress import onion_auth  # noqa: E402
from stress.orchestrator.config import StressConfig  # noqa: E402
from stress.orchestrator.metrics import WorkerInfoStore  # noqa: E402
from stress.orchestrator.notifications import (  # noqa: E402
    generate_signed_payloads, generate_unregister_payloads)

HAS_SODIUM = onion_auth.verification_available()


class FakeDocker:
    """Answers the store's one `docker exec` read with the rows given."""

    def __init__(self, rows):
        self.rows = rows
        self.execs = []

    def container_running(self, name):
        return True

    def exec(self, container, command, timeout=30, **kwargs):
        self.execs.append((container, command))
        return DockerResult(0, json.dumps(self.rows), "")


def _make_test_worker_info(workers: list[dict]) -> WorkerInfoStore:
    """A WorkerInfoStore loaded with the rows given."""
    store = WorkerInfoStore(FakeDocker(workers), StressConfig(total=len(workers) or 1))
    store.load_all(1)
    return store


def _generate_test_keys():
    """A deterministic expanded ed25519 key pair for signing."""
    seed = b"test-seed-for-stress-notifications-1234"
    h = hashlib.sha512(seed).digest()
    # Clamp the scalar (first 32 bytes) as Tor's expanded keys are clamped.
    expanded = bytearray(h)
    expanded[0] &= 248
    expanded[31] &= 127
    expanded[31] |= 64
    expanded_key = bytes(expanded)  # 64 bytes
    public_key = onion_auth.derive_public_key(expanded_key)
    return expanded_key, public_key


def _signing_workers(count=1, address=None):
    expanded_key, public_key = _generate_test_keys()
    addr = address or onion_auth.derive_onion_address(public_key)
    return addr, [{
        "global_index": i,
        "local_index": i,
        "content_address": addr,
        "healthcheck_address": addr,
        "registered": True,
        "privkey_b64": base64.b64encode(expanded_key).decode(),
        "pubkey_b64": base64.b64encode(public_key).decode(),
    } for i in range(count)]


@unittest.skipUnless(HAS_SODIUM, "PyNaCl is not installed")
class TestPayloadGeneration(unittest.TestCase):
    """Tests for generate_signed_payloads."""

    def test_generates_correct_count(self):
        _addr, workers = _signing_workers()
        store = _make_test_worker_info(workers)
        payloads = generate_signed_payloads(store, "offline", 0, 1)
        self.assertEqual(len(payloads), 1)

    def test_payload_has_required_fields(self):
        addr, workers = _signing_workers()
        store = _make_test_worker_info(workers)
        payloads = generate_signed_payloads(store, "offline", 0, 1)
        data = json.loads(payloads[0])
        self.assertIn("content_address", data)
        self.assertIn("healthcheck_address", data)
        self.assertIn("timestamp", data)
        self.assertIn("signature", data)
        self.assertEqual(data["content_address"], addr)

    def test_online_payload_carries_the_version(self):
        _addr, workers = _signing_workers()
        workers[0]["arti_key_pem"] = "PEM"
        store = _make_test_worker_info(workers)
        data = json.loads(generate_signed_payloads(store, "online", 0, 1, "stress-test-1")[0])
        self.assertEqual(data["version"], "stress-test-1")
        self.assertEqual(data["arti_key_pem"], "PEM")
        self.assertIs(data["wordpress_healthy"], True)

    def test_signature_verifies(self):
        _addr, workers = _signing_workers()
        store = _make_test_worker_info(workers)
        payloads = generate_signed_payloads(store, "offline", 0, 1)
        data = json.loads(payloads[0])
        ok, err = onion_auth.verify_payload(
            data["content_address"],
            "offline",
            data["healthcheck_address"],
            data["timestamp"],
            data["signature"],
        )
        self.assertTrue(ok, f"Signature verification failed: {err}")

    def test_range_filtering(self):
        _addr, workers = _signing_workers(count=5)
        store = _make_test_worker_info(workers)
        # Only sites 2-3
        payloads = generate_signed_payloads(store, "offline", 2, 2)
        self.assertEqual(len(payloads), 2)

    def test_skips_missing_keys(self):
        workers = [{
            "global_index": 0,
            "local_index": 0,
            "content_address": "test.onion",
            "healthcheck_address": "test.onion",
            "registered": True,
            # No privkey_b64 or pubkey_b64
        }]
        store = _make_test_worker_info(workers)
        payloads = generate_signed_payloads(store, "offline", 0, 1)
        self.assertEqual(len(payloads), 0)

    def test_unregister_payloads_dedup(self):
        _addr, workers = _signing_workers(count=2)  # the same address twice
        store = _make_test_worker_info(workers)
        payloads = generate_unregister_payloads(store)
        self.assertEqual(len(payloads), 1)  # deduped


class TestWorkerInfoStore(unittest.TestCase):
    """Tests for WorkerInfoStore (no signing, so no PyNaCl needed)."""

    def test_load_and_query(self):
        workers = [
            {"global_index": 0, "local_index": 0, "content_address": "a.onion",
             "healthcheck_address": "ha.onion", "registered": 1},
            {"global_index": 1, "local_index": 1, "content_address": "b.onion",
             "healthcheck_address": "hb.onion", "registered": 0},
        ]
        store = _make_test_worker_info(workers)
        self.assertEqual(store.get_worker(0)["content_address"], "a.onion")
        self.assertIs(store.get_worker(0)["registered"], True)
        self.assertEqual(store.get_worker(1)["content_address"], "b.onion")
        self.assertIs(store.get_worker(1)["registered"], False)
        self.assertIsNone(store.get_worker(2))

    def test_reads_through_one_exec_on_a_running_container(self):
        docker = FakeDocker([])
        store = WorkerInfoStore(docker, StressConfig(total=1))
        store.load_all(1)
        self.assertEqual(len(docker.execs), 1)
        container, command = docker.execs[0]
        self.assertEqual(container, "stress-worker-0")
        self.assertIn("/worker-data/worker-info.db", " ".join(command))

    def test_get_content_addrs(self):
        workers = [
            {"global_index": 0, "local_index": 0, "content_address": "a.onion",
             "healthcheck_address": "ha.onion", "registered": True},
            {"global_index": 1, "local_index": 1, "content_address": "b.onion",
             "healthcheck_address": "hb.onion", "registered": True},
        ]
        store = _make_test_worker_info(workers)
        addrs = store.get_content_addrs(0, 2)
        self.assertEqual(addrs, ["a.onion", "b.onion"])

    def test_get_hc_addrs(self):
        workers = [
            {"global_index": 0, "local_index": 0, "content_address": "a.onion",
             "healthcheck_address": "ha.onion", "registered": True},
        ]
        store = _make_test_worker_info(workers)
        addrs = store.get_hc_addrs(0, 1)
        self.assertEqual(addrs, ["ha.onion"])

    def test_total_registered(self):
        workers = [
            {"global_index": 0, "local_index": 0, "content_address": "a.onion",
             "healthcheck_address": "ha.onion", "registered": True},
            {"global_index": 1, "local_index": 1, "content_address": "b.onion",
             "healthcheck_address": "hb.onion", "registered": False},
            {"global_index": 2, "local_index": 2, "content_address": "c.onion",
             "healthcheck_address": "hc.onion", "registered": True},
        ]
        store = _make_test_worker_info(workers)
        self.assertEqual(store.total_registered(), 2)


if __name__ == "__main__":
    unittest.main()

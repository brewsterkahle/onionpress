"""Unit tests for stress test notification payload generation."""

import base64
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from stress.orchestrator.notifications import generate_signed_payloads, generate_unregister_payloads
from stress.orchestrator.metrics import WorkerInfoStore
from onionpress.onion_auth import verify_payload


def _make_test_worker_info(tmpdir: str, workers: list[dict]) -> WorkerInfoStore:
    """Create a WorkerInfoStore with test data."""
    path = os.path.join(tmpdir, "worker-0-info.json")
    with open(path, "w") as f:
        json.dump(workers, f)
    store = WorkerInfoStore(tmpdir, per_ctr=20)
    store.load_all(1)
    return store


def _generate_test_keys():
    """Generate a test ed25519 key pair for signing."""
    import hashlib
    # Use a deterministic seed for tests
    seed = b"test-seed-for-stress-notifications-1234"
    h = hashlib.sha512(seed).digest()
    # Clamp scalar (first 32 bytes)
    expanded = bytearray(h)
    expanded[0] &= 248
    expanded[31] &= 127
    expanded[31] |= 64
    expanded_key = bytes(expanded)  # 64 bytes

    # Derive public key
    from onion_auth import derive_public_key
    public_key = derive_public_key(expanded_key)

    return expanded_key, public_key


class TestPayloadGeneration:
    """Tests for generate_signed_payloads."""

    def test_generates_correct_count(self):
        expanded_key, public_key = _generate_test_keys()
        from onion_auth import derive_onion_address
        addr = derive_onion_address(public_key)

        workers = [{
            "global_index": 0,
            "local_index": 0,
            "content_address": addr,
            "healthcheck_address": addr,
            "registered": True,
            "privkey_b64": base64.b64encode(expanded_key).decode(),
            "pubkey_b64": base64.b64encode(public_key).decode(),
        }]

        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_test_worker_info(tmpdir, workers)
            payloads = generate_signed_payloads(store, "offline", 0, 1)
            assert len(payloads) == 1

    def test_payload_has_required_fields(self):
        expanded_key, public_key = _generate_test_keys()
        from onion_auth import derive_onion_address
        addr = derive_onion_address(public_key)

        workers = [{
            "global_index": 0,
            "local_index": 0,
            "content_address": addr,
            "healthcheck_address": addr,
            "registered": True,
            "privkey_b64": base64.b64encode(expanded_key).decode(),
            "pubkey_b64": base64.b64encode(public_key).decode(),
        }]

        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_test_worker_info(tmpdir, workers)
            payloads = generate_signed_payloads(store, "offline", 0, 1)
            data = json.loads(payloads[0])
            assert "content_address" in data
            assert "healthcheck_address" in data
            assert "timestamp" in data
            assert "signature" in data
            assert data["content_address"] == addr

    def test_signature_verifies(self):
        expanded_key, public_key = _generate_test_keys()
        from onion_auth import derive_onion_address
        addr = derive_onion_address(public_key)

        workers = [{
            "global_index": 0,
            "local_index": 0,
            "content_address": addr,
            "healthcheck_address": addr,
            "registered": True,
            "privkey_b64": base64.b64encode(expanded_key).decode(),
            "pubkey_b64": base64.b64encode(public_key).decode(),
        }]

        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_test_worker_info(tmpdir, workers)
            payloads = generate_signed_payloads(store, "offline", 0, 1)
            data = json.loads(payloads[0])
            ok, err = verify_payload(
                data["content_address"],
                "offline",
                data["healthcheck_address"],
                data["timestamp"],
                data["signature"],
            )
            assert ok, f"Signature verification failed: {err}"

    def test_range_filtering(self):
        expanded_key, public_key = _generate_test_keys()
        from onion_auth import derive_onion_address
        addr = derive_onion_address(public_key)

        workers = [
            {
                "global_index": i,
                "local_index": i,
                "content_address": addr,
                "healthcheck_address": addr,
                "registered": True,
                "privkey_b64": base64.b64encode(expanded_key).decode(),
                "pubkey_b64": base64.b64encode(public_key).decode(),
            }
            for i in range(5)
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_test_worker_info(tmpdir, workers)
            # Only sites 2-3
            payloads = generate_signed_payloads(store, "offline", 2, 2)
            assert len(payloads) == 2

    def test_skips_missing_keys(self):
        workers = [{
            "global_index": 0,
            "local_index": 0,
            "content_address": "test.onion",
            "healthcheck_address": "test.onion",
            "registered": True,
            # No privkey_b64 or pubkey_b64
        }]

        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_test_worker_info(tmpdir, workers)
            payloads = generate_signed_payloads(store, "offline", 0, 1)
            assert len(payloads) == 0

    def test_unregister_payloads_dedup(self):
        expanded_key, public_key = _generate_test_keys()
        from onion_auth import derive_onion_address
        addr = derive_onion_address(public_key)

        workers = [
            {
                "global_index": 0,
                "local_index": 0,
                "content_address": addr,
                "healthcheck_address": addr,
                "registered": True,
                "privkey_b64": base64.b64encode(expanded_key).decode(),
                "pubkey_b64": base64.b64encode(public_key).decode(),
            },
            {
                "global_index": 1,
                "local_index": 1,
                "content_address": addr,  # duplicate
                "healthcheck_address": addr,
                "registered": True,
                "privkey_b64": base64.b64encode(expanded_key).decode(),
                "pubkey_b64": base64.b64encode(public_key).decode(),
            },
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_test_worker_info(tmpdir, workers)
            payloads = generate_unregister_payloads(store)
            assert len(payloads) == 1  # deduped


class TestWorkerInfoStore:
    """Tests for WorkerInfoStore."""

    def test_load_and_query(self):
        workers = [
            {"global_index": 0, "local_index": 0, "content_address": "a.onion",
             "healthcheck_address": "ha.onion", "registered": True},
            {"global_index": 1, "local_index": 1, "content_address": "b.onion",
             "healthcheck_address": "hb.onion", "registered": False},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_test_worker_info(tmpdir, workers)
            assert store.get_worker(0)["content_address"] == "a.onion"
            assert store.get_worker(1)["content_address"] == "b.onion"
            assert store.get_worker(2) is None

    def test_get_content_addrs(self):
        workers = [
            {"global_index": 0, "local_index": 0, "content_address": "a.onion",
             "healthcheck_address": "ha.onion", "registered": True},
            {"global_index": 1, "local_index": 1, "content_address": "b.onion",
             "healthcheck_address": "hb.onion", "registered": True},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_test_worker_info(tmpdir, workers)
            addrs = store.get_content_addrs(0, 2)
            assert addrs == ["a.onion", "b.onion"]

    def test_get_hc_addrs(self):
        workers = [
            {"global_index": 0, "local_index": 0, "content_address": "a.onion",
             "healthcheck_address": "ha.onion", "registered": True},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_test_worker_info(tmpdir, workers)
            addrs = store.get_hc_addrs(0, 1)
            assert addrs == ["ha.onion"]

    def test_total_registered(self):
        workers = [
            {"global_index": 0, "local_index": 0, "content_address": "a.onion",
             "healthcheck_address": "ha.onion", "registered": True},
            {"global_index": 1, "local_index": 1, "content_address": "b.onion",
             "healthcheck_address": "hb.onion", "registered": False},
            {"global_index": 2, "local_index": 2, "content_address": "c.onion",
             "healthcheck_address": "hc.onion", "registered": True},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_test_worker_info(tmpdir, workers)
            assert store.total_registered() == 2

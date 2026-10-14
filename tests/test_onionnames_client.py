"""Tests for the Mac-side onionname helpers.

Covers:
  - onionpress.onionnames_client: validate_name, load_wordlist, suggest_name_local
  - onionpress.onionnames_registrar: signing + HTTP parsing with injected runner
    and key source (no real docker exec, no network).
"""

import hashlib
import json
import os
import sys
import unittest

# Make src/ importable (matches how the MenubarApp runs in dev).
_SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
sys.path.insert(0, _SRC_DIR)

from onionpress import onionnames_client  # noqa: E402
from onionpress import onionnames_registrar  # noqa: E402
from onionpress import onion_auth  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _expand_seed(seed_bytes):
    h = hashlib.sha512(seed_bytes).digest()
    a = bytearray(h[:32])
    a[0] &= 248
    a[31] &= 127
    a[31] |= 64
    prefix = h[32:]
    expanded = bytes(a) + prefix
    return expanded, onion_auth.derive_public_key(expanded)


# ---------------------------------------------------------------------------
# onionnames_client
# ---------------------------------------------------------------------------

class TestValidateName(unittest.TestCase):
    def test_ok_cases(self):
        for name in ("brewsterkahle", "BrewsterKahle", "a1-b2_c3",
                     "hello.world"):
            ok, _ = onionnames_client.validate_name(name)
            self.assertTrue(ok, f"{name!r} should be valid")

    def test_reject_cases(self):
        cases = [
            ("abcd", "too_short"),
            ("", "empty"),
            (None, "empty"),
            ("a" * 41, "too_long"),
            ("hello world", "invalid_chars"),
            ("brewster@archive", "invalid_chars"),
            (".alice", "invalid_chars"),
            ("alice.", "invalid_chars"),
            ("12345", "all_numeric"),
            # `$` alone would match before a trailing newline.
            ("alice\n", "invalid_chars"),
            ("12345\n", "invalid_chars"),
        ]
        for value, expected in cases:
            ok, reason = onionnames_client.validate_name(value)
            self.assertFalse(ok, f"{value!r} should be invalid")
            self.assertEqual(reason, expected, f"{value!r}: got {reason}")


class TestLocalSuggestion(unittest.TestCase):
    def test_suggest_returns_valid_name(self):
        name = onionnames_client.suggest_name_local("en")
        self.assertIsNotNone(name, "en wordlist should be discoverable")
        ok, _ = onionnames_client.validate_name(name)
        self.assertTrue(ok, f"suggestion {name!r} should pass validation")
        self.assertIn("-", name)

    def test_locale_mapping(self):
        name = onionnames_client.suggest_name_local("fr_FR")
        self.assertIsNotNone(name)

    def test_unknown_lang_falls_back_to_en(self):
        name = onionnames_client.suggest_name_local("xx_YY")
        self.assertIsNotNone(name)

    def test_suggest_names_local_returns_unique_list(self):
        names = onionnames_client.suggest_names_local("en", count=5)
        self.assertLessEqual(len(names), 5)
        self.assertEqual(len(names), len(set(n.lower() for n in names)))


# ---------------------------------------------------------------------------
# onionnames_registrar — behavior with injected runner + keys
# ---------------------------------------------------------------------------

class _FakeRunner:
    """Records curl invocations; returns scripted responses.

    scripted: list of (rc, body_string, http_status) tuples consumed in order.
    calls: (curl_args, timeout); inputs: what each call had on stdin.
    """
    def __init__(self, scripted):
        self.scripted = list(scripted)
        self.calls = []
        self.inputs = []

    def __call__(self, args, timeout, input=None):
        self.calls.append((list(args), timeout))
        self.inputs.append(input)
        if not self.scripted:
            raise AssertionError("FakeRunner: exhausted scripted responses")
        rc, body, status = self.scripted.pop(0)
        if rc != 0 or body is None:
            return rc, ""
        raw = f"{body}\n__HTTP_STATUS__:{status}"
        return rc, raw


def _fake_key_source():
    return _expand_seed(b"\x42" * 32)


class TestRegistrarGET(unittest.TestCase):

    def _make(self, scripted):
        runner = _FakeRunner(scripted)
        reg = onionnames_registrar.Registrar(
            runner=runner, key_source=_fake_key_source,
        )
        return reg, runner

    def test_suggest_happy_path(self):
        reg, _runner = self._make([
            (0, json.dumps({"onionname": "cheerful-penguin", "lang": "en"}), 200),
        ])
        result = reg.suggest("en")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.body["onionname"], "cheerful-penguin")

    def test_check_taken_returns_collision_body(self):
        reg, _runner = self._make([
            (0, json.dumps({
                "available": False,
                "reason": "taken",
                "suggestions": ["foo-1234", "foo-bright"],
            }), 200),
        ])
        result = reg.check("foo")
        # /check is always 200 even when taken — the body carries the info.
        self.assertEqual(result.status, "ok")
        self.assertFalse(result.body["available"])
        self.assertEqual(result.body["suggestions"], ["foo-1234", "foo-bright"])

    def test_lookup_404_returns_not_found(self):
        reg, _runner = self._make([
            (0, json.dumps({"error": "Not found"}), 404),
        ])
        result = reg.lookup("ghost")
        self.assertEqual(result.status, "not_found")
        self.assertEqual(result.http_status, 404)

    def test_network_timeout_is_unreachable(self):
        reg, _runner = self._make([(-1, None, 0)])
        result = reg.suggest("en")
        self.assertEqual(result.status, "unreachable")
        self.assertEqual(result.reason, "timeout")

    def test_connect_refused_mapped(self):
        reg, _runner = self._make([(7, None, 0)])
        result = reg.suggest("en")
        self.assertEqual(result.status, "unreachable")
        self.assertEqual(result.reason, "connect_refused")


class TestRegistrarPOST(unittest.TestCase):

    def _make(self, scripted):
        runner = _FakeRunner(scripted)
        reg = onionnames_registrar.Registrar(
            runner=runner, key_source=_fake_key_source,
        )
        return reg, runner

    def test_register_success_returns_url(self):
        expanded, pub = _expand_seed(b"\x42" * 32)
        addr = onion_auth.derive_onion_address(pub)
        url = f"http://{addr}/brewsterkahle"
        reg, runner = self._make([
            (0, json.dumps({
                "onionname": "brewsterkahle",
                "onionaddress": addr,
                "url": url,
            }), 201),
        ])
        result = reg.register("brewsterkahle", addr)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.body["url"], url)
        # Verify the request the runner saw carried a valid signature,
        # on curl's stdin rather than in argv (ps shows argv to every user).
        args, _ = runner.calls[0]
        self.assertIn("-X", args)
        self.assertIn("POST", args)
        data_idx = args.index("--data-binary")
        self.assertEqual(args[data_idx + 1], "@-")
        self.assertNotIn("--data", args)
        payload = json.loads(runner.inputs[0])
        for arg in args:
            self.assertNotIn(payload["signature"], arg)
        self.assertEqual(payload["onionname"], "brewsterkahle")
        self.assertEqual(payload["onionaddress"], addr)
        self.assertIn("signature", payload)
        self.assertIn("timestamp", payload)
        ok, _ = onion_auth.verify_name_payload(
            addr, "register", "brewsterkahle",
            payload["timestamp"], payload["signature"],
        )
        self.assertTrue(ok, "signature should verify against OnionHome")

    def test_register_collision_yields_suggestions(self):
        reg, _runner = self._make([
            (0, json.dumps({
                "error": "taken",
                "suggestions": ["foo-7321", "foo-bright", "foo-lantern"],
            }), 409),
        ])
        # Any fake address that passes ONION_RE validation client-side.
        _, pub = _expand_seed(b"\x42" * 32)
        addr = onion_auth.derive_onion_address(pub)
        result = reg.register("foo", addr)
        self.assertEqual(result.status, "collision")
        self.assertEqual(
            result.suggestions,
            ["foo-7321", "foo-bright", "foo-lantern"],
        )

    def test_register_forbidden_on_signature_rejection(self):
        reg, _runner = self._make([
            (0, json.dumps({"error": "Invalid signature"}), 403),
        ])
        _, pub = _expand_seed(b"\x42" * 32)
        addr = onion_auth.derive_onion_address(pub)
        result = reg.register("brewsterkahle", addr)
        self.assertEqual(result.status, "forbidden")
        self.assertEqual(result.reason, "Invalid signature")

    def test_register_key_failure_returns_error(self):
        # key_source raises — simulate a container that isn't ready yet.
        def broken_keys():
            raise RuntimeError("container not up")

        reg = onionnames_registrar.Registrar(
            runner=_FakeRunner([]),
            key_source=broken_keys,
        )
        _, pub = _expand_seed(b"\x42" * 32)
        addr = onion_auth.derive_onion_address(pub)
        result = reg.register("brewsterkahle", addr)
        self.assertEqual(result.status, "error")
        self.assertEqual(result.reason, "sign_failed")

    def test_release_signs_with_release_endpoint(self):
        _, pub = _expand_seed(b"\x42" * 32)
        addr = onion_auth.derive_onion_address(pub)
        reg, runner = self._make([
            (0, json.dumps({"released": True, "onionname": "foo"}), 200),
        ])
        result = reg.release("foo", addr)
        self.assertEqual(result.status, "ok")
        data_idx = runner.calls[0][0].index("--data-binary")
        self.assertEqual(runner.calls[0][0][data_idx + 1], "@-")
        payload = json.loads(runner.inputs[0])
        ok, _ = onion_auth.verify_name_payload(
            addr, "release", "foo",
            payload["timestamp"], payload["signature"],
        )
        self.assertTrue(ok)
        # A register-prefixed signature must NOT validate as a release.
        ok2, _ = onion_auth.verify_name_payload(
            addr, "register", "foo",
            payload["timestamp"], payload["signature"],
        )
        self.assertFalse(ok2)


class TestRegistrarResponseParsing(unittest.TestCase):
    def test_bad_json_is_error(self):
        runner = _FakeRunner([(0, "not json at all", 200)])
        reg = onionnames_registrar.Registrar(
            runner=runner, key_source=_fake_key_source,
        )
        result = reg.suggest("en")
        self.assertEqual(result.status, "error")
        self.assertEqual(result.reason, "bad_json")

    def test_missing_status_marker_is_error(self):
        # Runner returns a plain body without the marker we tell curl to add.
        class BareRunner:
            def __call__(self, args, timeout):
                return 0, json.dumps({"foo": 1})  # no marker
        reg = onionnames_registrar.Registrar(
            runner=BareRunner(), key_source=_fake_key_source,
        )
        result = reg.suggest("en")
        self.assertEqual(result.status, "error")
        self.assertEqual(result.reason, "missing_status_marker")


if __name__ == "__main__":
    unittest.main()

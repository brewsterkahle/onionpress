"""A damaged onion key never becomes a new onion address (issue #311).

A secret key file that was there but not 96 bytes made tor-watchdog.py
treat the key as missing, send ADD_ONION NEW:BEST and write the new
identity over the old one; the entrypoint only re-derived C Tor's files
from the delivered PEM when they were missing. These tests hold:

- key-convert.py check-ctor reports anything short of an exact copy;
- the entrypoint's key block brings a truncated (or foreign) secret key
  back to the PEM's address and moves the old files aside, never deleting
  them, and refuses to start when there is no usable key at all;
- the watchdog serves the delivered key when C Tor's copy is unusable,
  never sends NEW:BEST for a service with any trace of an address, never
  overwrites key files, and reuses a key it minted on the next DEL+ADD.

Nothing here touches a control port, Docker or a real key.
"""

import base64
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

_TOR_DIR = os.path.join(os.path.dirname(__file__), "..", "app", "Resources",
                        "docker", "tor")
_KEY_CONVERT = os.path.abspath(os.path.join(_TOR_DIR, "key-convert.py"))
_WATCHDOG = os.path.abspath(os.path.join(_TOR_DIR, "tor-watchdog.py"))
_ENTRYPOINT = os.path.abspath(os.path.join(_TOR_DIR, "entrypoint.sh"))
PEM_NAME = "ks_hs_id.ed25519_expanded_private"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


kc = _load(_KEY_CONVERT, "key_convert_under_test")


def _address(public_key):
    version = b"\x03"
    checksum = hashlib.sha3_256(b".onion checksum" + public_key + version).digest()[:2]
    return base64.b32encode(public_key + checksum + version).decode().lower() + ".onion"


def _make_key(seed):
    """A (expanded_key, public_key) pair. The format never checks that the
    two belong together, so fixed bytes are enough."""
    return bytes([seed]) * 64, bytes([seed + 1]) * 32


def _write_pem(path, expanded, public):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(kc.build_arti_pem(expanded, public))


def _write_ctor(d, expanded, public, hostname=True):
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "hs_ed25519_secret_key"), "wb") as f:
        f.write(kc.CTOR_SECRET_HEADER + expanded)
    with open(os.path.join(d, "hs_ed25519_public_key"), "wb") as f:
        f.write(kc.CTOR_PUBLIC_HEADER + public)
    if hostname:
        with open(os.path.join(d, "hostname"), "w") as f:
            f.write(_address(public) + "\n")


def _truncate(path, size):
    with open(path, "r+b") as f:
        f.truncate(size)


def _read(path, mode="rb"):
    with open(path, mode) as f:
        return f.read()


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.expanded, self.public = _make_key(7)
        self.address = _address(self.public)


class TestCheckCtor(_Tmp):
    def setUp(self):
        super().setUp()
        self.pem = os.path.join(self.tmp, "keys", "wordpress", PEM_NAME)
        self.ctor = os.path.join(self.tmp, "hs", "wordpress")
        _write_pem(self.pem, self.expanded, self.public)

    def _check(self):
        return kc.check_ctor_against_pem(self.pem, self.ctor)

    def test_exact_copy_passes(self):
        _write_ctor(self.ctor, self.expanded, self.public)
        self.assertEqual(self._check(), (True, self.address))

    def test_without_hostname_passes(self):
        _write_ctor(self.ctor, self.expanded, self.public, hostname=False)
        self.assertTrue(self._check()[0])

    def test_missing_secret(self):
        ok, reason = self._check()
        self.assertFalse(ok)
        self.assertIn("missing", reason)

    def test_truncated_secret(self):
        _write_ctor(self.ctor, self.expanded, self.public)
        _truncate(os.path.join(self.ctor, "hs_ed25519_secret_key"), 50)
        ok, reason = self._check()
        self.assertFalse(ok)
        self.assertIn("50 bytes", reason)

    def test_bad_header(self):
        _write_ctor(self.ctor, self.expanded, self.public)
        path = os.path.join(self.ctor, "hs_ed25519_secret_key")
        data = bytearray(_read(path))
        data[0] ^= 0xFF
        with open(path, "wb") as f:
            f.write(bytes(data))
        self.assertFalse(self._check()[0])

    def test_other_key(self):
        other_e, other_p = _make_key(40)
        _write_ctor(self.ctor, other_e, other_p)
        ok, reason = self._check()
        self.assertFalse(ok)
        self.assertIn("different key", reason)

    def test_damaged_public_key(self):
        _write_ctor(self.ctor, self.expanded, self.public)
        _truncate(os.path.join(self.ctor, "hs_ed25519_public_key"), 10)
        self.assertFalse(self._check()[0])

    def test_foreign_hostname(self):
        _write_ctor(self.ctor, self.expanded, self.public)
        with open(os.path.join(self.ctor, "hostname"), "w") as f:
            f.write(_address(_make_key(40)[1]) + "\n")
        self.assertFalse(self._check()[0])

    def test_linked_secret_is_not_followed(self):
        _write_ctor(self.ctor, self.expanded, self.public)
        real = os.path.join(self.tmp, "real-secret")
        os.replace(os.path.join(self.ctor, "hs_ed25519_secret_key"), real)
        os.symlink(real, os.path.join(self.ctor, "hs_ed25519_secret_key"))
        self.assertFalse(self._check()[0])

    def test_damaged_pem_raises(self):
        with open(self.pem, "w") as f:
            f.write("-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n")
        with self.assertRaises(ValueError):
            self._check()

    def test_cli_exit_codes(self):
        def run():
            return subprocess.run(
                ["python3", _KEY_CONVERT, "check-ctor", self.pem, self.ctor],
                capture_output=True, text=True).returncode
        self.assertEqual(run(), 3)  # missing
        _write_ctor(self.ctor, self.expanded, self.public)
        self.assertEqual(run(), 0)
        _truncate(os.path.join(self.ctor, "hs_ed25519_secret_key"), 95)
        self.assertEqual(run(), 3)
        with open(self.pem, "w") as f:
            f.write("not a key\n")
        self.assertEqual(run(), 4)

    def test_arti_to_ctor_writes_matching_hostname(self):
        kc.arti_to_ctor(self.pem, self.ctor)
        self.assertEqual(_read(os.path.join(self.ctor, "hostname"), "r").strip(),
                         self.address)
        self.assertEqual(self._check(), (True, self.address))
        self.assertEqual([n for n in os.listdir(self.ctor) if ".tmp-" in n], [])


def _entrypoint_key_block():
    """The entrypoint's key loop, as shell text."""
    src = _read(_ENTRYPOINT, "r")
    start = src.index("# Derive C Tor's key files from the delivered PEM")
    loop = src.index("for nickname in wordpress healthcheck; do", start)
    end = src.index("\ndone\n", loop) + len("\ndone\n")
    return src[start:end]


class TestEntrypointKeyBlock(_Tmp):
    """Runs the entrypoint's key block against a sandbox."""

    def setUp(self):
        super().setUp()
        self.tor = os.path.join(self.tmp, "var-lib-tor")
        self.keys = os.path.join(self.tmp, "keys")
        self.ctor = os.path.join(self.tor, "hidden_service", "wordpress")
        self.pem = os.path.join(self.keys, "wordpress", PEM_NAME)
        os.makedirs(os.path.join(self.tor, "hidden_service", "healthcheck"))
        os.makedirs(self.ctor)

    def _run(self):
        block = (_entrypoint_key_block()
                 .replace("/var/lib/tor", self.tor)
                 .replace("/key-convert.py", _KEY_CONVERT))
        script = f'KEYS_DIR="{self.keys}"\n{block}\necho BLOCK-DONE\n'
        return subprocess.run(["sh", "-c", script], capture_output=True, text=True)

    def _aside(self):
        root = os.path.join(self.tor, "key-set-aside")
        if not os.path.isdir(root):
            return []
        return [os.path.join(root, d) for d in os.listdir(root)]

    def test_truncated_secret_comes_back_with_the_original_address(self):
        _write_pem(self.pem, self.expanded, self.public)
        _write_ctor(self.ctor, self.expanded, self.public)
        secret = os.path.join(self.ctor, "hs_ed25519_secret_key")
        _truncate(secret, 50)
        r = self._run()
        self.assertIn("BLOCK-DONE", r.stdout, r.stderr)
        self.assertEqual(_read(secret), kc.CTOR_SECRET_HEADER + self.expanded)
        self.assertEqual(_read(os.path.join(self.ctor, "hostname"), "r").strip(),
                         self.address)
        # The damaged file is kept, not deleted.
        aside = self._aside()
        self.assertEqual(len(aside), 1)
        self.assertEqual(len(_read(os.path.join(aside[0], "hs_ed25519_secret_key"))), 50)
        self.assertIn("50 bytes", r.stdout)

    def test_foreign_key_is_replaced_by_the_delivered_one_and_kept(self):
        _write_pem(self.pem, self.expanded, self.public)
        other_e, other_p = _make_key(40)
        _write_ctor(self.ctor, other_e, other_p)
        r = self._run()
        self.assertIn("BLOCK-DONE", r.stdout, r.stderr)
        self.assertEqual(_read(os.path.join(self.ctor, "hostname"), "r").strip(),
                         self.address)
        aside = self._aside()
        self.assertEqual(len(aside), 1)
        self.assertEqual(_read(os.path.join(aside[0], "hs_ed25519_secret_key")),
                         kc.CTOR_SECRET_HEADER + other_e)

    def test_missing_secret_is_converted(self):
        _write_pem(self.pem, self.expanded, self.public)
        r = self._run()
        self.assertIn("BLOCK-DONE", r.stdout, r.stderr)
        self.assertTrue(kc.check_ctor_against_pem(self.pem, self.ctor)[0])
        self.assertEqual(self._aside(), [])

    def test_exact_copy_is_left_alone(self):
        _write_pem(self.pem, self.expanded, self.public)
        _write_ctor(self.ctor, self.expanded, self.public)
        before = os.stat(os.path.join(self.ctor, "hs_ed25519_secret_key"))
        r = self._run()
        self.assertIn("BLOCK-DONE", r.stdout, r.stderr)
        after = os.stat(os.path.join(self.ctor, "hs_ed25519_secret_key"))
        self.assertEqual(before.st_ino, after.st_ino)
        self.assertEqual(self._aside(), [])

    def test_no_pem_leaves_ctor_files_alone(self):
        _write_ctor(self.ctor, self.expanded, self.public)
        r = self._run()
        self.assertIn("BLOCK-DONE", r.stdout, r.stderr)
        self.assertEqual(self._aside(), [])

    def test_unreadable_pem_with_intact_ctor_starts(self):
        os.makedirs(os.path.dirname(self.pem))
        with open(self.pem, "w") as f:
            f.write("damaged\n")
        _write_ctor(self.ctor, self.expanded, self.public)
        r = self._run()
        self.assertIn("BLOCK-DONE", r.stdout, r.stderr)
        self.assertIn("WARNING", r.stderr)
        self.assertEqual(_read(os.path.join(self.ctor, "hs_ed25519_secret_key")),
                         kc.CTOR_SECRET_HEADER + self.expanded)

    def test_unreadable_pem_and_no_secret_refuses_to_start(self):
        os.makedirs(os.path.dirname(self.pem))
        with open(self.pem, "w") as f:
            f.write("damaged\n")
        r = self._run()
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("BLOCK-DONE", r.stdout)
        self.assertIn("refusing to start", r.stderr)


class _Recorder:
    """Stands in for send_cmd; NEW:BEST answers with a fresh key."""

    def __init__(self):
        self.commands = []

    def __call__(self, sock, cmd):
        self.commands.append(cmd)
        if "NEW:BEST" in cmd:
            e, p = _make_key(90)
            sid = _address(p)[:-len(".onion")]
            return (f"250-ServiceID={sid}\r\n"
                    f"250-PrivateKey=ED25519-V3:{base64.b64encode(e).decode()}\r\n"
                    "250 OK\r\n")
        m = re.search(r"ED25519-V3:(\S+)", cmd)
        return "250-ServiceID=x\r\n250 OK\r\n" if m else "250 OK\r\n"


class TestWatchdogNeverMints(_Tmp):
    def setUp(self):
        super().setUp()
        self.mod = _load(_WATCHDOG, "tor_watchdog_keys_under_test")
        self.logged = []
        self.mod.log = self.logged.append
        self.mod.HS_BASE_DIR = os.path.join(self.tmp, "hs")
        self.mod.KEYS_DIR = os.path.join(self.tmp, "keys")
        self.mod.SERVICES_JSON_PATH = os.path.join(self.tmp, "services.json")
        with open(self.mod.SERVICES_JSON_PATH, "w") as f:
            json.dump([{"name": "wordpress", "ports": ["80,127.0.0.1:8080"]}], f)
        self.ctor = os.path.join(self.mod.HS_BASE_DIR, "wordpress")
        self.pem = os.path.join(self.mod.KEYS_DIR, "wordpress", PEM_NAME)
        self.sent = _Recorder()
        self.mod.send_cmd = self.sent

    def _snapshot(self):
        out = {}
        for root, _dirs, files in os.walk(self.tmp):
            for n in files:
                p = os.path.join(root, n)
                out[p] = _read(p)
        return out

    def test_truncated_secret_with_pem_serves_the_original_address(self):
        _write_pem(self.pem, self.expanded, self.public)
        _write_ctor(self.ctor, self.expanded, self.public)
        _truncate(os.path.join(self.ctor, "hs_ed25519_secret_key"), 50)
        before = self._snapshot()
        svcs = self.mod.discover_services()
        self.assertEqual(svcs[0]["service_id"] + ".onion", self.address)
        self.assertEqual(svcs[0]["key_b64"], base64.b64encode(self.expanded).decode())
        self.mod.add_all_services(None, svcs)
        self.assertNotIn("NEW:BEST", " ".join(self.sent.commands))
        self.assertIn("ED25519-V3:" + svcs[0]["key_b64"], self.sent.commands[0])
        self.assertEqual(self._snapshot(), before, "no key file was written")

    def test_truncated_secret_without_pem_stays_down(self):
        _write_ctor(self.ctor, self.expanded, self.public)
        _truncate(os.path.join(self.ctor, "hs_ed25519_secret_key"), 50)
        before = self._snapshot()
        svcs = self.mod.discover_services()
        self.assertTrue(svcs[0]["refuse_new"])
        self.assertEqual(self.mod.add_all_services(None, svcs), (0, 0))
        self.mod.add_all_services(None, svcs)  # e.g. a wake
        self.assertEqual(self.sent.commands, [])
        self.assertEqual(self._snapshot(), before)
        self.assertTrue(any("REFUSING to mint" in m for m in self.logged))
        self.assertEqual(sum("SKIPPED" in m for m in self.logged), 1)

    def test_any_single_trace_blocks_new_best(self):
        for fname in ("hs_ed25519_secret_key", "hs_ed25519_public_key", "hostname"):
            with self.subTest(fname=fname):
                shutil.rmtree(self.ctor, ignore_errors=True)
                os.makedirs(self.ctor)
                with open(os.path.join(self.ctor, fname), "wb") as f:
                    f.write(b"x")
                self.sent.commands.clear()
                svcs = self.mod.discover_services()
                self.mod.add_all_services(None, svcs)
                self.assertNotIn("NEW:BEST", " ".join(self.sent.commands))

    def test_damaged_pem_and_no_ctor_key_stays_down(self):
        os.makedirs(os.path.dirname(self.pem))
        with open(self.pem, "w") as f:
            f.write("damaged\n")
        svcs = self.mod.discover_services()
        self.mod.add_all_services(None, svcs)
        self.assertEqual(self.sent.commands, [])

    def test_trace_appearing_after_discovery_blocks_new_best(self):
        svcs = self.mod.discover_services()
        self.assertFalse(svcs[0]["refuse_new"])
        _write_pem(self.pem, self.expanded, self.public)
        self.mod.add_all_services(None, svcs)
        self.assertEqual(self.sent.commands, [])

    def test_fresh_service_mints_once_and_keeps_that_key(self):
        svcs = self.mod.discover_services()
        self.mod.add_all_services(None, svcs)
        self.assertIn("NEW:BEST", self.sent.commands[0])
        new_id = svcs[0]["service_id"]
        e, p = _make_key(90)
        self.assertEqual(new_id + ".onion", _address(p))
        self.assertEqual(_read(os.path.join(self.ctor, "hs_ed25519_secret_key")),
                         kc.CTOR_SECRET_HEADER + e)
        self.assertEqual(_read(os.path.join(self.ctor, "hostname"), "r").strip(),
                         new_id + ".onion")
        # Sleep/wake or a republish: DEL+ADD must bring back the same address.
        self.mod.del_all_services(None, svcs)
        self.mod.add_all_services(None, svcs)
        self.assertEqual(sum("NEW:BEST" in c for c in self.sent.commands), 1)
        self.assertIn("ED25519-V3:" + base64.b64encode(e).decode(), self.sent.commands[-1])

    def test_save_generated_key_never_overwrites(self):
        os.makedirs(self.ctor)
        secret = os.path.join(self.ctor, "hs_ed25519_secret_key")
        with open(secret, "wb") as f:
            f.write(b"old")
        e, p = _make_key(90)
        ok = self.mod._save_generated_key(self.ctor, _address(p)[:-6],
                                          base64.b64encode(e).decode())
        self.assertFalse(ok)
        self.assertEqual(_read(secret), b"old")
        self.assertFalse(os.path.exists(os.path.join(self.ctor, "hostname")))


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""
OnionHeaven test helper — generates faux OnionPress identities and signed payloads.

Reuses crypto from tests/test_onionheaven_integration.py and src/onion_auth.py.

Usage:
    python3 tests/oh-test-helper.py generate
    python3 tests/oh-test-helper.py sign-online <identity.json> [--with-key]
    python3 tests/oh-test-helper.py sign-unregister <identity.json>
"""

import base64
import hashlib
import json
import os
import struct
import sys

# Allow importing from src/
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(PROJECT_DIR, "src"))

from onionpress import onion_auth


def make_test_keypair():
    """Generate an expanded key + public key + onion address for testing."""
    seed = os.urandom(32)
    h = hashlib.sha512(seed).digest()
    a_bytes = bytearray(h[:32])
    a_bytes[0] &= 248
    a_bytes[31] &= 127
    a_bytes[31] |= 64
    expanded = bytes(a_bytes) + h[32:]
    pub = onion_auth.derive_public_key(expanded)
    addr = onion_auth.derive_onion_address(pub)
    return expanded, pub, addr


def make_arti_pem(expanded_key, public_key):
    """Build a valid Arti-format OpenSSH PEM from expanded key + public key."""
    OPENSSH_MAGIC = b"openssh-key-v1\x00"
    KEY_TYPE = b"ed25519-expanded@spec.torproject.org"

    def pack(data):
        return struct.pack(">I", len(data)) + data

    pub_blob = pack(KEY_TYPE) + pack(public_key)
    check = struct.pack(">I", int.from_bytes(os.urandom(4), "big"))
    priv_blob = (check + check + pack(KEY_TYPE) + pack(public_key) +
                 pack(expanded_key) + pack(b""))
    pad_len = (8 - len(priv_blob) % 8) % 8
    priv_blob += bytes(range(1, pad_len + 1))
    binary = (OPENSSH_MAGIC + pack(b"none") + pack(b"none") + pack(b"") +
              struct.pack(">I", 1) + pack(pub_blob) + pack(priv_blob))
    b64 = base64.b64encode(binary).decode("ascii")
    lines = [b64[i:i + 70] for i in range(0, len(b64), 70)]
    pem = "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    pem += "\n".join(lines) + "\n"
    pem += "-----END OPENSSH PRIVATE KEY-----\n"
    return pem


def cmd_generate():
    """Generate a faux OnionPress identity and print as JSON."""
    expanded, pub, content_addr = make_test_keypair()
    _, _, healthcheck_addr = make_test_keypair()
    pem = make_arti_pem(expanded, pub)

    identity = {
        "content_address": content_addr,
        "healthcheck_address": healthcheck_addr,
        "expanded_key_b64": base64.b64encode(expanded).decode("ascii"),
        "public_key_b64": base64.b64encode(pub).decode("ascii"),
        "arti_key_pem_b64": base64.b64encode(pem.encode("utf-8")).decode("ascii"),
    }
    print(json.dumps(identity, indent=2))


def load_identity(path):
    """Load identity JSON from file."""
    with open(path) as f:
        return json.load(f)


def cmd_sign_online(identity_path, with_key=False):
    """Generate a signed /online heartbeat payload."""
    ident = load_identity(identity_path)
    expanded = base64.b64decode(ident["expanded_key_b64"])
    pub = base64.b64decode(ident["public_key_b64"])
    timestamp = onion_auth.make_timestamp()

    sig = onion_auth.sign_payload(
        expanded, pub, "online",
        ident["content_address"], ident["healthcheck_address"], timestamp
    )

    payload = {
        "content_address": ident["content_address"],
        "healthcheck_address": ident["healthcheck_address"],
        "wordpress_healthy": True,
        "version": "test-local-oh",
        "is_onionheaven": False,
        "timestamp": timestamp,
        "signature": sig,
    }

    if with_key:
        pem_bytes = base64.b64decode(ident["arti_key_pem_b64"])
        payload["arti_key_pem"] = base64.b64encode(pem_bytes).decode("ascii")

    print(json.dumps(payload, indent=2))


def cmd_sign_unregister(identity_path):
    """Generate a signed /unregister payload."""
    ident = load_identity(identity_path)
    expanded = base64.b64decode(ident["expanded_key_b64"])
    pub = base64.b64decode(ident["public_key_b64"])
    timestamp = onion_auth.make_timestamp()

    sig = onion_auth.sign_payload(
        expanded, pub, "unregister",
        ident["content_address"], ident["healthcheck_address"], timestamp
    )

    payload = {
        "content_address": ident["content_address"],
        "healthcheck_address": ident["healthcheck_address"],
        "timestamp": timestamp,
        "signature": sig,
    }

    print(json.dumps(payload, indent=2))


def main():
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "generate":
        cmd_generate()
    elif cmd == "sign-online":
        if len(sys.argv) < 3:
            print("Usage: oh-test-helper.py sign-online <identity.json> [--with-key]", file=sys.stderr)
            sys.exit(1)
        with_key = "--with-key" in sys.argv
        cmd_sign_online(sys.argv[2], with_key=with_key)
    elif cmd == "sign-unregister":
        if len(sys.argv) < 3:
            print("Usage: oh-test-helper.py sign-unregister <identity.json>", file=sys.stderr)
            sys.exit(1)
        cmd_sign_unregister(sys.argv[2])
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

"""
The one place an onion service key's OpenSSH PEM is read, written and
checked against its public key.

The delivered key (the onionpress-onion-keys volume's
ks_hs_id.ed25519_expanded_private, and OnionHeaven's wire format) is an
unencrypted OpenSSH private key of type ed25519-expanded@spec.torproject.org
holding a 64-byte expanded secret key and its 32-byte public key. The app
(key_manager), the tor image's key-convert.py, tor-watchdog.py and
web-server.py all go through here.

The v3 address of a public key is onion_auth.derive_onion_address; this
module derives nothing and computes no curve arithmetic: keypair_error asks
libsodium (PyNaCl).

This file is the source; app/Resources/docker/tor/onion_keys.py is a copy
the image is built from (build/sync-onion-keys.sh makes it,
tests/test_onion_keys.py holds the two equal). It imports only the standard
library so the image can use it as it is.
"""

import base64
import hmac
import os
import struct

OPENSSH_MAGIC = b"openssh-key-v1\x00"
KEY_TYPE = b"ed25519-expanded@spec.torproject.org"

PEM_BEGIN = "-----BEGIN OPENSSH PRIVATE KEY-----"
PEM_END = "-----END OPENSSH PRIVATE KEY-----"


class SodiumUnavailable(RuntimeError):
    """PyNaCl (python3-nacl, libsodium) is not installed."""


def pack_string(data):
    """Bytes as a uint32 big-endian length, then the bytes."""
    return struct.pack(">I", len(data)) + data


def unpack_string(buf, offset):
    """(bytes, next offset) of the length-prefixed string at offset."""
    if offset + 4 > len(buf):
        raise ValueError("Truncated length field")
    length = struct.unpack(">I", buf[offset:offset + 4])[0]
    offset += 4
    if offset + length > len(buf):
        raise ValueError(f"Truncated data: need {length} bytes at offset "
                         f"{offset}, have {len(buf) - offset}")
    return buf[offset:offset + length], offset + length


def parse_pem(pem_bytes, allow_embedded_public=False):
    """(expanded_key 64 bytes, public_key 32 bytes) of an OpenSSH PEM.

    The key as the takeover path hands it to Tor: the private section's.
    ValueError for anything else: not an unencrypted OpenSSH key, not one
    key, not this key type, mismatched check integers, wrong sizes.
    allow_embedded_public also takes a 96-byte secret (the expanded key
    with the public key appended, as plain OpenSSH ed25519 writes it) when
    its second half is the public key.
    """
    text = pem_bytes.decode("utf-8", errors="replace")
    body = []
    in_body = False
    for line in text.strip().splitlines():
        if line.startswith("-----BEGIN"):
            in_body = True
            continue
        if line.startswith("-----END"):
            break
        if in_body:
            body.append(line.strip())
    if not body:
        raise ValueError("No PEM body found in key file")

    raw = base64.b64decode("".join(body))
    if not raw.startswith(OPENSSH_MAGIC):
        raise ValueError("Not an OpenSSH private key (missing openssh-key-v1 magic)")
    offset = len(OPENSSH_MAGIC)

    cipher, offset = unpack_string(raw, offset)
    kdf, offset = unpack_string(raw, offset)
    _, offset = unpack_string(raw, offset)  # kdf options
    if cipher != b"none" or kdf != b"none":
        raise ValueError("Encrypted keys are not supported")

    if offset + 4 > len(raw):
        raise ValueError("Truncated key count")
    nkeys = struct.unpack(">I", raw[offset:offset + 4])[0]
    offset += 4
    if nkeys != 1:
        raise ValueError(f"Expected 1 key, got {nkeys}")

    _, offset = unpack_string(raw, offset)  # public blob; the private section has it
    private, _ = unpack_string(raw, offset)

    if len(private) < 8:
        raise ValueError("Truncated private section")
    if private[:4] != private[4:8]:
        raise ValueError("Check integers mismatch: key is corrupt or encrypted")
    p = 8
    key_type, p = unpack_string(private, p)
    if key_type != KEY_TYPE:
        raise ValueError(f"Unexpected key type: {key_type!r}")
    public_key, p = unpack_string(private, p)
    if len(public_key) != 32:
        raise ValueError(f"Public key is {len(public_key)} bytes, expected 32")
    secret, p = unpack_string(private, p)
    if len(secret) == 64:
        return secret, public_key
    if allow_embedded_public and len(secret) == 96:
        if secret[64:] != public_key:
            raise ValueError("Embedded public key in private blob does not match")
        return secret[:64], public_key
    raise ValueError(f"Expanded key is {len(secret)} bytes, expected 64")


def parse_pem_file(path, **kwargs):
    with open(path, "rb") as f:
        return parse_pem(f.read(), **kwargs)


def public_key_of_pem(pem_bytes):
    """The PEM's 32-byte public key, or None if the PEM cannot be read."""
    try:
        return parse_pem(pem_bytes, allow_embedded_public=True)[1]
    except Exception:
        return None


def build_pem(expanded_key, public_key):
    """The OpenSSH PEM (bytes) of an expanded key and its public key."""
    if len(expanded_key) != 64:
        raise ValueError(f"Private key must be 64 bytes, got {len(expanded_key)}")
    if len(public_key) != 32:
        raise ValueError(f"Public key must be 32 bytes, got {len(public_key)}")

    pub_blob = pack_string(KEY_TYPE) + pack_string(public_key)
    check = os.urandom(4)
    private = (check + check +
               pack_string(KEY_TYPE) + pack_string(public_key) +
               pack_string(expanded_key) + pack_string(b""))
    private += bytes(range(1, (8 - len(private) % 8) % 8 + 1))  # pad 1,2,3...

    binary = (OPENSSH_MAGIC +
              pack_string(b"none") + pack_string(b"none") + pack_string(b"") +
              struct.pack(">I", 1) +
              pack_string(pub_blob) + pack_string(private))
    b64 = base64.b64encode(binary).decode("ascii")
    lines = [b64[i:i + 70] for i in range(0, len(b64), 70)]
    return (PEM_BEGIN + "\n" + "\n".join(lines) + "\n" + PEM_END + "\n").encode("ascii")


def keypair_error(expanded_key, public_key):
    """Why expanded_key is not public_key's secret key, or None if it is.

    An expanded ed25519 key is a secret scalar a (its first 32 bytes) and a
    nonce prefix; the public key is a times the base point. The scalar is
    what Tor signs with and derives the onion address from, so a key whose
    scalar does not give public_key cannot serve that address. The point
    multiplication is libsodium's (PyNaCl), never computed here. Raises
    SodiumUnavailable without PyNaCl.
    """
    if len(expanded_key) != 64 or len(public_key) != 32:
        return "the key has the wrong length"
    scalar = expanded_key[:32]
    if scalar[31] & 0x80:
        # libsodium (and Tor's ref10) take the scalar mod 2^255: a set top
        # bit would check one scalar here and hand Tor another.
        return "the secret scalar is not below 2^255"
    try:
        from nacl import bindings
    except ImportError as e:
        raise SodiumUnavailable(str(e)) from e
    if not getattr(bindings, "has_crypto_scalarmult_ed25519", True):
        raise SodiumUnavailable("libsodium was built without ed25519 scalar multiplication")
    try:
        derived = bindings.crypto_scalarmult_ed25519_base_noclamp(scalar)
    except (RuntimeError, TypeError, ValueError):
        # libsodium refuses a zero scalar and one that gives the identity
        return "the secret scalar is zero or a multiple of the group order"
    if not hmac.compare_digest(derived, public_key):
        return "the secret scalar does not give the public key"
    return None

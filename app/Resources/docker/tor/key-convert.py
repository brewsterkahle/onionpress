#!/usr/bin/env python3
"""
Bidirectional key format conversion: Arti PEM <-> C Tor onion service keys.

Arti format:
  OpenSSH PEM with key type "ed25519-expanded@spec.torproject.org"
  Contains 64-byte expanded private key + 32-byte public key.

C Tor format:
  hs_ed25519_secret_key:  32-byte header + 64-byte expanded key
  hs_ed25519_public_key:  32-byte header + 32-byte public key
  Header: "== ed25519v1-{type}: type0 ==\x00\x00\x00"

Usage:
  key-convert.py arti-to-ctor <arti_pem_file> <output_dir>
  key-convert.py ctor-to-arti <ctor_secret_key_file> <output_pem_file>
  key-convert.py check-ctor <arti_pem_file> <ctor_dir>
      exit 0: C Tor's files in <ctor_dir> hold exactly the PEM's key
      exit 3: they are missing, damaged or hold another key (reason printed)
      exit 4: the PEM itself cannot be read (reason printed)
"""

import base64
import os
import stat
import struct
import sys

import onion_auth
import onion_keys
from onion_keys import SodiumUnavailable, keypair_error

# C Tor key file headers (exactly 32 bytes each)
CTOR_SECRET_HEADER = b"== ed25519v1-secret: type0 ==\x00\x00\x00"
CTOR_PUBLIC_HEADER = b"== ed25519v1-public: type0 ==\x00\x00\x00"


def parse_arti_pem(pem_path):
    """(expanded_key 64 bytes, public_key 32 bytes) of an Arti OpenSSH PEM file."""
    return onion_keys.parse_pem_file(pem_path)


def parse_arti_pem_bytes(pem_bytes):
    """parse_arti_pem for a PEM in memory: the private section's keys, the
    key ADD_ONION gets at takeover time.

    web-server.py reads a registration's PEM with this same function, so the
    key it checks is the key a takeover will use.
    """
    return onion_keys.parse_pem(pem_bytes)


def _write_atomic(path, data, mode=0o600):
    """Write key material to path in one step, mode 0600 (or mode).

    A new file next to it (O_EXCL, O_NOFOLLOW), synced, renamed over path,
    and the directory synced: a crash or a full disk leaves the old file or
    the new one, never a short one. A short hs_ed25519_secret_key is not
    converted again at the next start (the entrypoint converts only a
    missing one) and the watchdog then mints a new onion address.
    """
    directory, name = os.path.split(path)
    tmp = os.path.join(directory or ".", f".{name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                 | getattr(os, "O_CLOEXEC", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), mode)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dfd = os.open(directory or ".", os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def build_arti_pem(expanded_key, public_key):
    """The Arti-format OpenSSH PEM (str) of an expanded key + public key."""
    return onion_keys.build_pem(expanded_key, public_key).decode("ascii")


def arti_to_ctor(pem_path, output_dir):
    """Convert Arti PEM to C Tor hs_ed25519_secret_key + hs_ed25519_public_key."""
    expanded_key, public_key = parse_arti_pem(pem_path)

    os.makedirs(output_dir, exist_ok=True)

    # The public key first: the entrypoint converts again only while the
    # secret key is missing, so the secret key is the one written last
    # before the hostname.
    public_path = os.path.join(output_dir, "hs_ed25519_public_key")
    _write_atomic(public_path, CTOR_PUBLIC_HEADER + public_key)

    secret_path = os.path.join(output_dir, "hs_ed25519_secret_key")
    _write_atomic(secret_path, CTOR_SECRET_HEADER + expanded_key)

    # The hostname too: one left from another key would be published as
    # the service ID, and the host reads it to learn the address.
    hostname_path = os.path.join(output_dir, "hostname")
    _write_atomic(hostname_path,
                  (_onion_address(public_key) + ".onion\n").encode("ascii"),
                  mode=0o644)

    print(f"Wrote {secret_path} ({32 + 64} bytes)")
    print(f"Wrote {public_path} ({32 + 32} bytes)")
    print(f"Wrote {hostname_path}")


def _onion_address(public_key):
    """v3 onion address (no .onion suffix) of a 32-byte ed25519 public key."""
    return onion_auth.derive_onion_address(public_key)[:-len(".onion")]


def _read_small(path, limit=4096):
    """Read at most limit bytes of a regular file, never through a link."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise ValueError("not a regular file")
        return f.read(limit + 1)


def check_ctor_against_pem(pem_path, ctor_dir):
    """Does ctor_dir hold exactly the delivered PEM's key? Returns (ok, reason).

    The PEM in the onion-keys volume is the install's identity; C Tor's
    files are a derived copy. Issue #311: a secret key that was there but
    not 96 bytes made the watchdog mint a NEW address, because the
    entrypoint only re-derived the copy when it was missing. So anything
    short of an exact match (size, header, key bytes, public key, hostname)
    is reported here and the entrypoint re-derives the copy from the PEM.

    Raises ValueError/OSError if the PEM itself cannot be read: then there
    is nothing to compare against, and the caller decides.
    """
    expanded_key, public_key = parse_arti_pem(pem_path)
    address = _onion_address(public_key) + ".onion"

    secret_path = os.path.join(ctor_dir, "hs_ed25519_secret_key")
    try:
        secret = _read_small(secret_path)
    except FileNotFoundError:
        return False, "hs_ed25519_secret_key is missing"
    except (OSError, ValueError) as e:
        return False, f"hs_ed25519_secret_key is unreadable ({e})"
    if len(secret) != 96:
        return False, f"hs_ed25519_secret_key is {len(secret)} bytes (expected 96)"
    if secret[:32] != CTOR_SECRET_HEADER:
        return False, "hs_ed25519_secret_key has an invalid header"
    if secret[32:] != expanded_key:
        return False, "hs_ed25519_secret_key holds a different key than the delivered PEM"

    public_path = os.path.join(ctor_dir, "hs_ed25519_public_key")
    try:
        public = _read_small(public_path)
    except FileNotFoundError:
        return False, "hs_ed25519_public_key is missing"
    except (OSError, ValueError) as e:
        return False, f"hs_ed25519_public_key is unreadable ({e})"
    if public != CTOR_PUBLIC_HEADER + public_key:
        return False, "hs_ed25519_public_key does not match the delivered PEM"

    # hostname is optional (the watchdog derives it from the public key),
    # but one naming another address would be published as the service ID.
    hostname_path = os.path.join(ctor_dir, "hostname")
    try:
        hostname = _read_small(hostname_path).decode("ascii", "replace").strip()
    except FileNotFoundError:
        hostname = None
    except (OSError, ValueError) as e:
        return False, f"hostname is unreadable ({e})"
    if hostname is not None and hostname != address:
        return False, "hostname names another address than the delivered PEM"

    return True, address


def ctor_to_arti(secret_key_path, output_pem_path):
    """Convert C Tor hs_ed25519_secret_key to Arti PEM.

    Also reads hs_ed25519_public_key from the same directory if available,
    otherwise derives the public key from the expanded key's first 32 bytes.
    """
    with open(secret_key_path, "rb") as f:
        data = f.read()

    if len(data) != 96:
        raise ValueError(f"Secret key wrong size: {len(data)} (expected 96)")
    if data[:32] != CTOR_SECRET_HEADER:
        raise ValueError("Invalid C Tor secret key header")

    expanded_key = data[32:]

    # Try to read public key from same directory
    secret_dir = os.path.dirname(secret_key_path)
    public_path = os.path.join(secret_dir, "hs_ed25519_public_key")
    if os.path.exists(public_path):
        with open(public_path, "rb") as f:
            pub_data = f.read()
        if len(pub_data) == 64 and pub_data[:32] == CTOR_PUBLIC_HEADER:
            public_key = pub_data[32:]
        else:
            raise ValueError("Invalid C Tor public key file")
    else:
        # Derive public key — the first 32 bytes of expanded key are the
        # clamped scalar; we need to do scalar multiplication to get the
        # public point. For simplicity, require the public key file.
        raise ValueError(
            f"hs_ed25519_public_key not found in {secret_dir} — "
            "C Tor to Arti conversion requires both key files"
        )

    pem = build_arti_pem(expanded_key, public_key)

    _write_atomic(output_pem_path, pem.encode("ascii"))

    print(f"Wrote {output_pem_path}")


def pem_to_ed25519_base64(pem_path):
    """Extract raw 64-byte expanded ed25519 key from Arti PEM, output as base64.

    This is the format C Tor's ADD_ONION command expects:
        ADD_ONION ED25519-V3:<base64_key> ...
    """
    expanded_key, _ = parse_arti_pem(pem_path)
    print(base64.b64encode(expanded_key).decode("ascii"))


def pem_to_onion_address(pem_path):
    """Derive the v3 onion address (no .onion suffix) from an Arti PEM file.

    Used to validate that a key file on disk actually belongs to the address
    its directory name claims — guards against corrupted KEYS_DIR entries.

    v3 address format: base32(pubkey | checksum | version), lowercase, no padding.
        checksum = SHA3-256(b'.onion checksum' | pubkey | version)[:2]
        version  = b'\\x03'
    """
    _, public_key = parse_arti_pem(pem_path)
    print(_onion_address(public_key))


def main():
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "arti-to-ctor":
        if len(sys.argv) != 4:
            print("Usage: key-convert.py arti-to-ctor <pem_file> <output_dir>",
                  file=sys.stderr)
            sys.exit(1)
        arti_to_ctor(sys.argv[2], sys.argv[3])
    elif cmd == "ctor-to-arti":
        if len(sys.argv) != 4:
            print("Usage: key-convert.py ctor-to-arti <secret_key_file> <output_pem>",
                  file=sys.stderr)
            sys.exit(1)
        ctor_to_arti(sys.argv[2], sys.argv[3])
    elif cmd == "pem-to-ed25519-base64":
        if len(sys.argv) != 3:
            print("Usage: key-convert.py pem-to-ed25519-base64 <pem_file>",
                  file=sys.stderr)
            sys.exit(1)
        pem_to_ed25519_base64(sys.argv[2])
    elif cmd == "pem-to-onion-address":
        if len(sys.argv) != 3:
            print("Usage: key-convert.py pem-to-onion-address <pem_file>",
                  file=sys.stderr)
            sys.exit(1)
        pem_to_onion_address(sys.argv[2])
    elif cmd == "check-ctor":
        if len(sys.argv) != 4:
            print("Usage: key-convert.py check-ctor <pem_file> <ctor_dir>",
                  file=sys.stderr)
            sys.exit(1)
        try:
            ok, reason = check_ctor_against_pem(sys.argv[2], sys.argv[3])
        except (OSError, ValueError, struct.error) as e:
            print(f"delivered PEM is unreadable ({e})")
            sys.exit(4)
        print(reason)
        sys.exit(0 if ok else 3)
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

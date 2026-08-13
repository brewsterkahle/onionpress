#!/usr/bin/env python3
"""
Key Management for OnionPress
Extract and write Arti Ed25519 expanded private keys to/from the onionpress-tor container.
Keys are stored in OpenSSH format at the Arti keystore path.
"""

import base64
import os
import struct
import subprocess


def _backend_nickname():
    """Docker-network hostname / onion-service nickname for this install.

    Self-contained (no import from config.py) because this file is shipped
    as a flat script outside the onionpress package — app/MacOS/onionpress
    and linux/install.sh invoke it directly from Contents/Resources/scripts/
    or scripts/, without the rest of the package alongside it. Reads
    SITE_TYPE directly from ~/.onionpress/config; a missing file/key or any
    value other than "static" resolves to "wordpress" — today's only
    nickname, and the safe default for every existing install.
    """
    config_path = os.path.join(os.path.expanduser("~"), ".onionpress", "config")
    site_type = "wordpress"
    try:
        with open(config_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("SITE_TYPE="):
                    site_type = line.split("=", 1)[1].strip()
                    break
    except OSError:
        pass
    return "site" if site_type == "static" else "wordpress"


BACKEND_NICKNAME = _backend_nickname()

ARTI_KEYSTORE_PATH = f"/var/lib/arti/state/keystore/hss/{BACKEND_NICKNAME}/ks_hs_id.ed25519_expanded_private"
ARTI_KEY_TYPE = b"ed25519-expanded@spec.torproject.org"
CONTAINER = "onionpress-tor"

# C Tor onion service key files. In C Tor mode (TOR_IMPL=tor, which has been
# the default since 2026-03-16, commit 5d91cb3e) the Arti keystore doesn't
# exist at all — identity lives at these paths instead. Format: 32-byte
# ASCII header + raw ed25519 material.
CTOR_SECRET_PATH = f"/var/lib/tor/hidden_service/{BACKEND_NICKNAME}/hs_ed25519_secret_key"
CTOR_PUBLIC_PATH = f"/var/lib/tor/hidden_service/{BACKEND_NICKNAME}/hs_ed25519_public_key"
CTOR_HOSTNAME_PATH = f"/var/lib/tor/hidden_service/{BACKEND_NICKNAME}/hostname"
CTOR_SECRET_HEADER = b"== ed25519v1-secret: type0 ==\x00\x00\x00"
CTOR_PUBLIC_HEADER = b"== ed25519v1-public: type0 ==\x00\x00\x00"

OPENSSH_MAGIC = b"openssh-key-v1\x00"


def _pack_string(data):
    """Pack bytes as uint32 big-endian length + data."""
    return struct.pack(">I", len(data)) + data


def _unpack_string(buf, offset):
    """Unpack a uint32-length-prefixed string from buf at offset. Returns (data, new_offset)."""
    if offset + 4 > len(buf):
        raise ValueError("Truncated length field")
    length = struct.unpack(">I", buf[offset:offset + 4])[0]
    offset += 4
    if offset + length > len(buf):
        raise ValueError(f"Truncated data: need {length} bytes at offset {offset}, have {len(buf) - offset}")
    return buf[offset:offset + length], offset + length


def parse_openssh_key(data):
    """
    Parse an OpenSSH private key file (PEM format, unencrypted).
    Returns (private_key_64bytes, public_key_32bytes).
    """
    # Strip PEM headers and decode base64
    text = data.decode("utf-8", errors="replace")
    lines = text.strip().splitlines()
    b64_lines = []
    in_body = False
    for line in lines:
        if line.startswith("-----BEGIN"):
            in_body = True
            continue
        if line.startswith("-----END"):
            break
        if in_body:
            b64_lines.append(line.strip())
    if not b64_lines:
        raise ValueError("No PEM body found in key file")

    raw = base64.b64decode("".join(b64_lines))

    # Parse binary format
    if not raw.startswith(OPENSSH_MAGIC):
        raise ValueError("Missing openssh-key-v1 magic")
    offset = len(OPENSSH_MAGIC)

    cipher, offset = _unpack_string(raw, offset)
    kdf, offset = _unpack_string(raw, offset)
    kdf_options, offset = _unpack_string(raw, offset)

    if cipher != b"none" or kdf != b"none":
        raise ValueError("Encrypted keys are not supported")

    nkeys = struct.unpack(">I", raw[offset:offset + 4])[0]
    offset += 4
    if nkeys != 1:
        raise ValueError(f"Expected 1 key, got {nkeys}")

    # Skip public key blob (we'll get it from the private section)
    _pub_blob, offset = _unpack_string(raw, offset)

    # Parse private key blob
    priv_blob, offset = _unpack_string(raw, offset)
    p = 0

    check1 = struct.unpack(">I", priv_blob[p:p + 4])[0]
    p += 4
    check2 = struct.unpack(">I", priv_blob[p:p + 4])[0]
    p += 4
    if check1 != check2:
        raise ValueError("Check integers mismatch — key may be corrupt or encrypted")

    key_type, p = _unpack_string(priv_blob, p)
    if key_type != ARTI_KEY_TYPE:
        raise ValueError(f"Unexpected key type: {key_type!r}")

    public_key, p = _unpack_string(priv_blob, p)
    if len(public_key) != 32:
        raise ValueError(f"Public key is {len(public_key)} bytes, expected 32")

    secret_blob, p = _unpack_string(priv_blob, p)
    # Arti writes just the 64-byte expanded private key (no appended public key),
    # unlike standard OpenSSH ed25519 which concatenates private+public.
    if len(secret_blob) == 64:
        private_key = secret_blob
    elif len(secret_blob) == 96:
        private_key = secret_blob[:64]
        if secret_blob[64:] != public_key:
            raise ValueError("Embedded public key in private blob does not match")
    else:
        raise ValueError(f"Secret blob is {len(secret_blob)} bytes, expected 64 or 96")

    return private_key, public_key


def build_openssh_key(private_key, public_key):
    """
    Build an OpenSSH private key file (PEM format, unencrypted) for Arti.
    private_key: 64 bytes (expanded Ed25519)
    public_key: 32 bytes
    Returns bytes (PEM-encoded).
    """
    if len(private_key) != 64:
        raise ValueError(f"Private key must be 64 bytes, got {len(private_key)}")
    if len(public_key) != 32:
        raise ValueError(f"Public key must be 32 bytes, got {len(public_key)}")

    # Build public key blob
    pub_blob = _pack_string(ARTI_KEY_TYPE) + _pack_string(public_key)

    # Build private key blob
    import os
    check = struct.pack(">I", int.from_bytes(os.urandom(4), "big"))
    priv_blob = (
        check + check +  # checkint1 == checkint2
        _pack_string(ARTI_KEY_TYPE) +
        _pack_string(public_key) +
        _pack_string(private_key) +  # Arti uses just 64-byte expanded privkey
        _pack_string(b"")  # empty comment
    )
    # Pad to 8-byte boundary with 1,2,3,4,...
    pad_len = (8 - len(priv_blob) % 8) % 8
    priv_blob += bytes(range(1, pad_len + 1))

    # Assemble full binary
    binary = (
        OPENSSH_MAGIC +
        _pack_string(b"none") +       # cipher
        _pack_string(b"none") +       # kdf
        _pack_string(b"") +           # kdf options
        struct.pack(">I", 1) +        # nkeys
        _pack_string(pub_blob) +
        _pack_string(priv_blob)
    )

    # PEM-wrap
    b64 = base64.b64encode(binary).decode("ascii")
    lines = [b64[i:i + 70] for i in range(0, len(b64), 70)]
    pem = "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    pem += "\n".join(lines) + "\n"
    pem += "-----END OPENSSH PRIVATE KEY-----\n"
    return pem.encode("utf-8")


def _docker_cat(path):
    """docker exec cat <path>. Returns (ok, bytes-or-stderr)."""
    try:
        result = subprocess.run(
            ["docker", "exec", CONTAINER, "cat", path],
            capture_output=True,
            timeout=10,
        )
    except Exception as e:
        return False, str(e).encode()
    if result.returncode != 0:
        return False, result.stderr
    return True, result.stdout


def _parse_ctor_secret(data):
    """Parse a C Tor hs_ed25519_secret_key: 32-byte header + 64-byte expanded."""
    if len(data) != 96:
        raise Exception(
            f"C Tor secret key wrong size: {len(data)} (expected 96)"
        )
    if data[:32] != CTOR_SECRET_HEADER:
        raise Exception("C Tor secret key has bad header")
    return data[32:]


def _parse_ctor_public(data):
    """Parse a C Tor hs_ed25519_public_key: 32-byte header + 32-byte pubkey."""
    if len(data) != 64:
        raise Exception(
            f"C Tor public key wrong size: {len(data)} (expected 64)"
        )
    if data[:32] != CTOR_PUBLIC_HEADER:
        raise Exception("C Tor public key has bad header")
    return data[32:]


def _pubkey_from_hostname(hostname_bytes):
    """Recover the 32-byte ed25519 public key from a v3 .onion hostname.

    C Tor writes `hostname` but often doesn't write `hs_ed25519_public_key`
    (newer tor relies on the secret key + on-demand derivation, plus the
    hostname file as the canonical public artifact). An onion v3 address
    is base32(pubkey[32] || checksum[2] || version[1]) + ".onion" — the
    pubkey is the first 32 bytes of the decoded payload.
    """
    text = hostname_bytes.decode("utf-8", errors="replace").strip().lower()
    if not text.endswith(".onion"):
        raise Exception(f"hostname file is not a .onion address: {text!r}")
    b32 = text[:-len(".onion")].upper()
    if len(b32) != 56:
        raise Exception(
            f"onion address wrong length: {len(b32)} chars (expected 56)"
        )
    decoded = base64.b32decode(b32)
    if len(decoded) != 35:
        raise Exception(
            f"decoded onion address wrong size: {len(decoded)} (expected 35)"
        )
    return decoded[:32]


def extract_keys():
    """
    Extract both Ed25519 keys from the onionpress-tor container.
    Returns (private_key_64bytes, public_key_32bytes).

    Tries the Arti keystore first (single file, OpenSSH PEM). Falls back
    to C Tor's pair of raw header+payload files. The container picks Arti
    or C Tor at boot based on TOR_IMPL; this function handles both so
    registration works regardless. Only one layout is expected to exist
    at a time — if both are missing we surface the Arti error, which is
    the more informative of the two on a broken install.
    """
    arti_ok, arti_data = _docker_cat(ARTI_KEYSTORE_PATH)
    if arti_ok:
        try:
            return parse_openssh_key(arti_data)
        except Exception as e:
            raise Exception(f"Failed to parse Arti key: {e}")

    # Fall back to C Tor layout. The secret key is always present in C Tor
    # mode; the public key file is optional (newer tor omits it), so we
    # prefer hs_ed25519_public_key when present and otherwise recover the
    # pubkey from the hostname file (which is authoritative either way).
    sec_ok, sec_data = _docker_cat(CTOR_SECRET_PATH)
    if sec_ok:
        try:
            expanded = _parse_ctor_secret(sec_data)
        except Exception as e:
            raise Exception(f"Failed to parse C Tor secret key: {e}")
        pub_ok, pub_data = _docker_cat(CTOR_PUBLIC_PATH)
        if pub_ok:
            try:
                return expanded, _parse_ctor_public(pub_data)
            except Exception as e:
                raise Exception(f"Failed to parse C Tor public key: {e}")
        host_ok, host_data = _docker_cat(CTOR_HOSTNAME_PATH)
        if host_ok:
            try:
                return expanded, _pubkey_from_hostname(host_data)
            except Exception as e:
                raise Exception(
                    f"Failed to recover public key from hostname: {e}"
                )

    # Neither layout was readable. Report the Arti failure (it's what
    # callers used to see, and it's usually the clearer of the two error
    # messages — "No such file or directory" vs. the same plus whatever
    # the C Tor read produced).
    detail = arti_data.decode(errors="replace").strip() if isinstance(arti_data, bytes) else str(arti_data)
    raise Exception(f"Failed to extract keys: Could not read key file: {detail}")


def extract_private_key():
    """
    Extract the Ed25519 expanded private key from the Arti keystore.
    Returns the raw 64-byte private key.
    """
    private_key, _public_key = extract_keys()
    return private_key


def extract_public_key():
    """
    Extract the Ed25519 public key from the Arti keystore.
    Returns the raw 32-byte public key.
    """
    _private_key, public_key = extract_keys()
    return public_key


def write_private_key(private_key, public_key):
    """
    Write a new key pair to the Arti keystore in OpenSSH format.
    Deletes derived keys and restarts the tor container.
    This will change your onion address!
    """
    import tempfile
    import os

    try:
        pem_data = build_openssh_key(private_key, public_key)

        # Write to a temporary file with restricted permissions
        with tempfile.NamedTemporaryFile(mode="wb", delete=False) as temp_file:
            temp_path = temp_file.name
            os.chmod(temp_path, 0o600)
            temp_file.write(pem_data)

        try:
            # Ensure keystore directory exists
            result = subprocess.run(
                ["docker", "exec", CONTAINER, "mkdir", "-p",
                 f"/var/lib/arti/state/keystore/hss/{BACKEND_NICKNAME}"],
                capture_output=True,
                timeout=10
            )

            # Copy file to container
            result = subprocess.run(
                ["docker", "cp", temp_path,
                 f"{CONTAINER}:{ARTI_KEYSTORE_PATH}"],
                capture_output=True,
                timeout=10
            )
            if result.returncode != 0:
                raise Exception(f"Failed to copy key to container: {result.stderr.decode()}")

            # Set proper permissions inside container
            result = subprocess.run(
                ["docker", "exec", CONTAINER, "chmod", "600", ARTI_KEYSTORE_PATH],
                capture_output=True,
                timeout=10
            )
            if result.returncode != 0:
                raise Exception(f"Failed to set key permissions: {result.stderr.decode()}")

            # Delete derived keys so Arti regenerates them for the new identity
            keystore_dir = f"/var/lib/arti/state/keystore/hss/{BACKEND_NICKNAME}"
            for derived in ["ks_hss_blind_id", "ks_hss_desc_sign", "ks_hss_ipts"]:
                subprocess.run(
                    ["docker", "exec", CONTAINER, "sh", "-c",
                     f"rm -f {keystore_dir}/{derived}*"],
                    capture_output=True,
                    timeout=10
                )

            # Restart tor container to pick up the new key
            subprocess.run(
                ["docker", "restart", CONTAINER],
                capture_output=True,
                timeout=30
            )

            return True

        finally:
            # Securely delete temporary file (multi-pass overwrite)
            if os.path.exists(temp_path):
                file_len = len(pem_data)
                with open(temp_path, "wb") as f:
                    f.write(os.urandom(file_len))
                    f.flush()
                    os.fsync(f.fileno())
                with open(temp_path, "wb") as f:
                    f.write(b"\x00" * file_len)
                    f.flush()
                    os.fsync(f.fileno())
                with open(temp_path, "wb") as f:
                    f.write(b"\xff" * file_len)
                    f.flush()
                    os.fsync(f.fileno())
                os.unlink(temp_path)

    except Exception as e:
        raise Exception(f"Failed to write private key: {e}")


CTOR_HEADER_SECRET = b'== ed25519v1-secret: type0 ==\x00\x00\x00'  # 32 bytes
CTOR_HEADER_PUBLIC = b'== ed25519v1-public: type0 ==\x00\x00\x00'  # 32 bytes


def read_ctor_keys(key_dir):
    """Read C Tor format keys from directory. Returns (private_64, public_32)."""
    import os
    secret_path = os.path.join(key_dir, 'hs_ed25519_secret_key')
    public_path = os.path.join(key_dir, 'hs_ed25519_public_key')

    with open(secret_path, 'rb') as f:
        secret_data = f.read()
    if len(secret_data) < 64:
        raise ValueError(f"Secret key file too short: {len(secret_data)} bytes")
    private_key = secret_data[32:]  # skip 32-byte C Tor header

    with open(public_path, 'rb') as f:
        public_data = f.read()
    if len(public_data) < 32:
        raise ValueError(f"Public key file too short: {len(public_data)} bytes")
    public_key = public_data[32:]  # skip 32-byte C Tor header

    return private_key, public_key


def write_ctor_keys(key_dir, private_key, public_key):
    """Write C Tor format hs_ed25519_secret_key + hs_ed25519_public_key."""
    import os
    secret_path = os.path.join(key_dir, 'hs_ed25519_secret_key')
    with open(secret_path, 'wb') as f:
        f.write(CTOR_HEADER_SECRET + private_key)
    os.chmod(secret_path, 0o600)

    public_path = os.path.join(key_dir, 'hs_ed25519_public_key')
    with open(public_path, 'wb') as f:
        f.write(CTOR_HEADER_PUBLIC + public_key)
    os.chmod(public_path, 0o644)


def read_arti_key(key_dir):
    """Read Arti PEM from directory. Returns (private_64, public_32)."""
    import os
    pem_path = os.path.join(key_dir, 'ks_hs_id.ed25519_expanded_private')
    with open(pem_path, 'rb') as f:
        data = f.read()
    return parse_openssh_key(data)


def write_arti_key(key_dir, private_key, public_key):
    """Write ks_hs_id.ed25519_expanded_private (OpenSSH PEM)."""
    import os
    pem_data = build_openssh_key(private_key, public_key)
    output_path = os.path.join(key_dir, 'ks_hs_id.ed25519_expanded_private')
    with open(output_path, 'wb') as f:
        f.write(pem_data)
    os.chmod(output_path, 0o600)


def ensure_key_formats(key_dir):
    """Ensure both C Tor and Arti formats exist in key_dir.
    Derives whichever is missing from whichever is present.
    Returns True if any conversion was done."""
    import os
    has_ctor = (os.path.isfile(os.path.join(key_dir, 'hs_ed25519_secret_key'))
                and os.path.isfile(os.path.join(key_dir, 'hs_ed25519_public_key')))
    has_arti = os.path.isfile(os.path.join(key_dir, 'ks_hs_id.ed25519_expanded_private'))

    if has_ctor and has_arti:
        return False

    if has_ctor and not has_arti:
        private_key, public_key = read_ctor_keys(key_dir)
        write_arti_key(key_dir, private_key, public_key)
        return True

    if has_arti and not has_ctor:
        private_key, public_key = read_arti_key(key_dir)
        write_ctor_keys(key_dir, private_key, public_key)
        return True

    raise ValueError(f"No key files found in {key_dir}")


def decode_base32_key(key_b32):
    """Decode a base32-encoded ed25519 expanded secret key.
    Accepts 64 raw bytes or 96 bytes (with 32-byte C Tor header).
    Returns the 64-byte expanded private key."""
    key_bytes = base64.b32decode(key_b32, casefold=True)
    if len(key_bytes) == 96 and key_bytes[:29] == b'== ed25519v1-secret: type0 ==':
        key_bytes = key_bytes[32:]
    elif len(key_bytes) != 64:
        raise ValueError(f"Key must be exactly 64 bytes (or 96 with header), got {len(key_bytes)}")
    return key_bytes


def derive_public_key(private_key):
    """Derive ed25519 public key from expanded private key via scalar multiplication.
    Returns 32-byte public key."""
    p = 2**255 - 19
    def inv(x): return pow(x, p - 2, p)
    d = -121665 * inv(121666) % p

    def recover_x(y, sign):
        x2 = (y * y - 1) * inv(d * y * y + 1) % p
        x = pow(x2, (p + 3) // 8, p)
        if (x * x - x2) % p != 0:
            x = x * pow(2, (p - 1) // 4, p) % p
        if x % 2 != sign:
            x = p - x
        return x

    By = 4 * inv(5) % p
    Bx = recover_x(By, 0)
    B = (Bx, By, 1, Bx * By % p)

    def point_add(P, Q):
        x1, y1, z1, t1 = P
        x2, y2, z2, t2 = Q
        a = (y1 - x1) * (y2 - x2) % p
        b = (y1 + x1) * (y2 + x2) % p
        c = 2 * t1 * t2 * d % p
        dd = 2 * z1 * z2 % p
        e = b - a
        f = dd - c
        g = dd + c
        h = b + a
        return (e * f % p, g * h % p, f * g % p, e * h % p)

    def scalar_mult(s, P):
        Q = (0, 1, 1, 0)
        while s > 0:
            if s & 1:
                Q = point_add(Q, P)
            P = point_add(P, P)
            s >>= 1
        return Q

    def encode_point(P):
        x, y, z, _ = P
        zi = inv(z)
        x, y = x * zi % p, y * zi % p
        r = bytearray(y.to_bytes(32, 'little'))
        r[31] ^= (x & 1) << 7
        return bytes(r)

    import hashlib
    scalar = int.from_bytes(private_key[:32], 'little')
    return encode_point(scalar_mult(scalar, B))


def derive_onion_address(public_key):
    """Derive v3 .onion address from 32-byte public key."""
    import hashlib
    checksum = hashlib.sha3_256(b'.onion checksum' + public_key + b'\x03').digest()[:2]
    return base64.b32encode(public_key + checksum + b'\x03').decode().lower() + '.onion'


def import_key_b32(key_b32, output_dir):
    """Import a base32-encoded key: decode, derive address, write all key files.
    Creates output_dir/<address>/ with key files + hostname.
    Returns the .onion address."""
    import os

    private_key = decode_base32_key(key_b32)
    public_key = derive_public_key(private_key)
    address = derive_onion_address(public_key)

    addr_dir = os.path.join(output_dir, address)
    os.makedirs(addr_dir, exist_ok=True)

    write_arti_key(addr_dir, private_key, public_key)
    write_ctor_keys(addr_dir, private_key, public_key)

    hostname_path = os.path.join(addr_dir, 'hostname')
    with open(hostname_path, 'w') as f:
        f.write(address + '\n')
    os.chmod(hostname_path, 0o644)

    return address


if __name__ == "__main__":
    import sys

    def _usage():
        print("Usage:")
        print("  key_manager.py extract")
        print("  key_manager.py ensure-formats <key_dir>")
        print("  key_manager.py ctor-to-arti <key_dir>")
        print("  key_manager.py arti-to-ctor <key_dir>")
        print("  key_manager.py write-keys <hex_privkey> <hex_pubkey> <key_dir>")
        print("  key_manager.py import-key <base32_key> <output_dir>")
        print("  key_manager.py decode-key <base32_key>")
        sys.exit(1)

    if len(sys.argv) < 2:
        _usage()

    cmd = sys.argv[1]
    try:
        if cmd == "extract":
            key_bytes = extract_private_key()
            print(f"Successfully extracted {len(key_bytes)}-byte private key")
            pub_bytes = extract_public_key()
            print(f"Successfully extracted {len(pub_bytes)}-byte public key")

        elif cmd == "ensure-formats":
            if len(sys.argv) < 3:
                _usage()
            key_dir = sys.argv[2]
            if ensure_key_formats(key_dir):
                print(f"Converted key formats in {key_dir}")
            else:
                print(f"Both formats already present in {key_dir}")

        elif cmd == "ctor-to-arti":
            if len(sys.argv) < 3:
                _usage()
            key_dir = sys.argv[2]
            private_key, public_key = read_ctor_keys(key_dir)
            write_arti_key(key_dir, private_key, public_key)
            print(f"Wrote Arti PEM to {key_dir}")

        elif cmd == "arti-to-ctor":
            if len(sys.argv) < 3:
                _usage()
            key_dir = sys.argv[2]
            private_key, public_key = read_arti_key(key_dir)
            write_ctor_keys(key_dir, private_key, public_key)
            print(f"Wrote C Tor keys to {key_dir}")

        elif cmd == "write-keys":
            if len(sys.argv) < 5:
                _usage()
            import binascii
            private_key = binascii.unhexlify(sys.argv[2])
            public_key = binascii.unhexlify(sys.argv[3])
            key_dir = sys.argv[4]
            write_arti_key(key_dir, private_key, public_key)
            write_ctor_keys(key_dir, private_key, public_key)
            print(f"Wrote key files to {key_dir}")

        elif cmd == "import-key":
            if len(sys.argv) < 4:
                _usage()
            address = import_key_b32(sys.argv[2], sys.argv[3])
            print(address)

        elif cmd == "decode-key":
            if len(sys.argv) < 3:
                _usage()
            private_key = decode_base32_key(sys.argv[2])
            public_key = derive_public_key(private_key)
            address = derive_onion_address(public_key)
            print(address)

        else:
            _usage()

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

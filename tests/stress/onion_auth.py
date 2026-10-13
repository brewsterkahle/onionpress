"""
Ed25519 signatures for OnionHeaven API authentication.

Verification is libsodium's, through PyNaCl (nacl.signing.VerifyKey, which
is crypto_sign_open): never computed here. libsodium refuses S >= L, a
small-order or non-canonical public key or R, and compares R byte for
byte. Without PyNaCl, verify() raises VerificationUnavailable rather than
answer: a signature this module cannot check is never taken as good. Only
the hub verifies (web-server.py, in the tor image, which installs
python3-nacl); PyNaCl is imported when verify() first runs, so a client
that only signs never needs it here.

Signing is RFC 8032 Ed25519 in pure Python (stdlib), with a Tor expanded
private key: 64 bytes = scalar a (32, clamped) + nonce prefix (32).
Standard ed25519 derives these from SHA-512(seed); Tor (and the
onionpress-onion-keys volume's PEM) stores them directly, and an mkp224o
vanity key has no seed at all. libsodium and pyca/cryptography sign only
from a seed, so neither can sign with these keys as they are.

src/onionpress/onion_auth.py and app/Resources/docker/tor/onion_auth.py
(and tests/stress/onion_auth.py) are one file; tests/test_onion_auth.py
keeps them identical.
"""

import base64
import hashlib
import struct
from datetime import datetime, timezone

# ── Ed25519 curve constants ──

_p = 2**255 - 19  # field prime
_L = 2**252 + 27742317777372353535851937790883648493  # base point order
_d = -121665 * pow(121666, _p - 2, _p) % _p  # curve parameter
_I = pow(2, (_p - 1) // 4, _p)  # sqrt(-1) mod p


def _sha512(data):
    return hashlib.sha512(data).digest()


# ── Field / point arithmetic ──

def _inv(x):
    """Modular inverse mod p (Fermat)."""
    return pow(x, _p - 2, _p)


def _recover_x(y, sign):
    """Recover x coordinate from y and sign bit (RFC 8032 §5.1.3)."""
    y2 = y * y % _p
    x2 = (y2 - 1) * _inv(_d * y2 + 1) % _p
    if x2 == 0:
        if sign:
            raise ValueError("Invalid point")
        return 0
    x = pow(x2, (_p + 3) // 8, _p)
    if (x * x - x2) % _p != 0:
        x = x * _I % _p
    if (x * x - x2) % _p != 0:
        raise ValueError("No square root exists")
    if x & 1 != sign:
        x = _p - x
    return x


# Base point B (generator)
_By = 4 * _inv(5) % _p
_Bx = _recover_x(_By, 0)
_B = (_Bx, _By, 1, _Bx * _By % _p)  # extended coordinates (X, Y, Z, T)

# Identity (neutral element)
_ZERO = (0, 1, 1, 0)


def _point_add(P, Q):
    """Add two points in extended twisted Edwards coordinates (a=-1)."""
    X1, Y1, Z1, T1 = P
    X2, Y2, Z2, T2 = Q
    A = (Y1 - X1) * (Y2 - X2) % _p
    B_ = (Y1 + X1) * (Y2 + X2) % _p
    C = T1 * 2 * _d * T2 % _p
    D = Z1 * 2 * Z2 % _p
    E = (B_ - A) % _p
    F = (D - C) % _p
    G = (D + C) % _p
    H = (B_ + A) % _p
    return (E * F % _p, G * H % _p, F * G % _p, E * H % _p)


def _scalar_mult(s, P):
    """Scalar multiplication via double-and-add."""
    Q = _ZERO
    while s > 0:
        if s & 1:
            Q = _point_add(Q, P)
        P = _point_add(P, P)
        s >>= 1
    return Q


def _encode_point(P):
    """Encode point to 32 bytes (RFC 8032 §5.1.2)."""
    X, Y, Z, _ = P
    zi = _inv(Z)
    x = X * zi % _p
    y = Y * zi % _p
    enc = bytearray(y.to_bytes(32, 'little'))
    if x & 1:
        enc[31] |= 0x80
    return bytes(enc)


# ── Ed25519 sign / verify ──

def sign_expanded(expanded_key, public_key, message):
    """Sign with an Arti expanded private key.

    expanded_key: 64 bytes (scalar a ‖ nonce prefix)
    public_key:   32 bytes (encoded point A)
    message:      bytes

    Returns 64-byte signature (R ‖ S).
    """
    if len(expanded_key) != 64:
        raise ValueError(f"Expanded key must be 64 bytes, got {len(expanded_key)}")
    if len(public_key) != 32:
        raise ValueError(f"Public key must be 32 bytes, got {len(public_key)}")

    a = int.from_bytes(expanded_key[:32], 'little')
    prefix = expanded_key[32:]

    r = int.from_bytes(_sha512(prefix + message), 'little') % _L
    R = _scalar_mult(r, _B)
    R_enc = _encode_point(R)

    h = int.from_bytes(_sha512(R_enc + public_key + message), 'little') % _L
    S = (r + h * a) % _L

    return R_enc + S.to_bytes(32, 'little')


class VerificationUnavailable(RuntimeError):
    """PyNaCl (python3-nacl, libsodium) is not installed: no signature can
    be checked, so none may be accepted."""


def _verify_key_class():
    """(VerifyKey, CryptoError) from PyNaCl, or VerificationUnavailable."""
    try:
        from nacl.exceptions import CryptoError
        from nacl.signing import VerifyKey
    except ImportError as e:
        raise VerificationUnavailable(
            f"PyNaCl (python3-nacl) is not installed: {e}") from e
    return VerifyKey, CryptoError


def verification_available():
    """True if verify() can run (PyNaCl imports)."""
    try:
        _verify_key_class()
    except VerificationUnavailable:
        return False
    return True


def verify(public_key, message, signature):
    """True if signature is public_key's Ed25519 signature of message.

    public_key: 32 bytes
    message:    bytes
    signature:  64 bytes (R ‖ S)

    libsodium decides (crypto_sign_open via nacl.signing.VerifyKey): it
    refuses S >= L, a non-canonical or small-order public key or R, and
    any R other than the one it computes, so one message has one valid
    signature encoding per key. Anything malformed is False.

    Raises VerificationUnavailable without PyNaCl: this never returns True
    unless libsodium said so.
    """
    VerifyKey, CryptoError = _verify_key_class()
    if not all(isinstance(v, (bytes, bytearray, memoryview))
               for v in (public_key, message, signature)):
        return False
    public_key, message, signature = bytes(public_key), bytes(message), bytes(signature)
    if len(signature) != 64 or len(public_key) != 32:
        return False
    try:
        VerifyKey(public_key).verify(message, signature)
    except (CryptoError, ValueError, TypeError):
        return False
    return True


# ── Onion address encode / decode ──

_BASE32 = "abcdefghijklmnopqrstuvwxyz234567"


def _base32_encode(data):
    """RFC 4648 base32 encode (lowercase, no padding)."""
    bits = ""
    for b in data:
        bits += format(b, "08b")
    return "".join(_BASE32[int(bits[i:i + 5], 2)]
                   for i in range(0, len(bits) - 4, 5))


def derive_onion_address(public_key_32):
    """Derive a Tor v3 .onion address from a 32-byte public key."""
    checksum = hashlib.sha3_256(
        b".onion checksum" + public_key_32 + b"\x03"
    ).digest()[:2]
    return _base32_encode(public_key_32 + checksum + b"\x03") + ".onion"


def decode_onion_address(address):
    """Extract the 32-byte public key from a v3 .onion address."""
    host = address[:-6] if address.endswith('.onion') else address
    if len(host) != 56:
        raise ValueError(f"Invalid onion address length: {len(host)}")
    decoded = base64.b32decode(host.upper())
    if len(decoded) != 35:
        raise ValueError(f"Decoded length {len(decoded)}, expected 35")
    pubkey, checksum, version = decoded[:32], decoded[32:34], decoded[34]
    if version != 3:
        raise ValueError(f"Unsupported version: {version}")
    expected = hashlib.sha3_256(
        b".onion checksum" + pubkey + bytes([version])
    ).digest()[:2]
    if checksum != expected:
        raise ValueError("Bad checksum")
    return pubkey


# ── OnionHeaven API payload signing / verification ──

# Canonical message format: "{endpoint}|{content_address}|{healthcheck_address}|{timestamp}"
# healthcheck_address may be empty string.

# Timestamp tolerance for verification (seconds)
TIMESTAMP_TOLERANCE = 300  # 5 minutes


def _present(timestamp, signature_b64):
    """Both are non-empty strings (JSON can carry anything there)."""
    return (isinstance(timestamp, str) and isinstance(signature_b64, str)
            and bool(timestamp) and bool(signature_b64))


def sign_payload(expanded_key, public_key, endpoint, content_address,
                 healthcheck_address, timestamp):
    """Sign an OnionHeaven API payload.

    Returns base64-encoded signature string.
    """
    canonical = f"{endpoint}|{content_address}|{healthcheck_address}|{timestamp}"
    sig = sign_expanded(expanded_key, public_key, canonical.encode('utf-8'))
    return base64.b64encode(sig).decode('ascii')


def make_timestamp():
    """Generate a UTC timestamp for payload signing."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def verify_payload(content_address, endpoint, healthcheck_address,
                   timestamp, signature_b64):
    """Verify an OnionHeaven API payload signature.

    Extracts public key from content_address, checks timestamp freshness,
    and verifies the ed25519 signature (verify(): libsodium).

    Returns (ok, error_message). Raises VerificationUnavailable without
    PyNaCl (web-server.py answers 503): never (True, "") unchecked.
    """
    if not _present(timestamp, signature_b64):
        return False, "Missing timestamp or signature"

    # Timestamp freshness
    try:
        ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        delta = abs((datetime.now(timezone.utc) - ts).total_seconds())
        if delta > TIMESTAMP_TOLERANCE:
            return False, "Timestamp expired or too far in future"
    except (ValueError, TypeError):
        return False, "Invalid timestamp format"

    # Decode public key from content_address
    try:
        public_key = decode_onion_address(content_address)
    except (ValueError, TypeError, AttributeError) as e:
        return False, f"Cannot decode public key from content_address: {e}"

    # Decode and verify signature
    try:
        sig = base64.b64decode(signature_b64, validate=True)
    except Exception:
        return False, "Invalid signature base64"

    canonical = f"{endpoint}|{content_address}|{healthcheck_address}|{timestamp}"
    if not verify(public_key, canonical.encode('utf-8'), sig):
        return False, "Invalid signature"

    return True, ""


# ── OnionPress name-registry payload signing / verification ──
#
# Distinct namespace from the OnionHeaven payload so a signature from one
# system can never be replayed against the other. Canonical message:
#   "name:{endpoint}|{onionaddress}|{name_lower}|{timestamp}"

def sign_name_payload(expanded_key, public_key, endpoint, onionaddress,
                      name, timestamp):
    """Sign an onionname-registry payload. Returns base64 signature string."""
    canonical = f"name:{endpoint}|{onionaddress}|{name.lower()}|{timestamp}"
    sig = sign_expanded(expanded_key, public_key, canonical.encode('utf-8'))
    return base64.b64encode(sig).decode('ascii')


def verify_name_payload(onionaddress, endpoint, name, timestamp, signature_b64):
    """Verify an onionname-registry payload signature.

    Returns (ok, error_message). Raises VerificationUnavailable without
    PyNaCl, as verify_payload does.
    """
    if not _present(timestamp, signature_b64):
        return False, "Missing timestamp or signature"
    if not isinstance(name, str):
        return False, "Invalid name"

    try:
        ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        delta = abs((datetime.now(timezone.utc) - ts).total_seconds())
        if delta > TIMESTAMP_TOLERANCE:
            return False, "Timestamp expired or too far in future"
    except (ValueError, TypeError):
        return False, "Invalid timestamp format"

    try:
        public_key = decode_onion_address(onionaddress)
    except (ValueError, TypeError, AttributeError) as e:
        return False, f"Cannot decode public key from onionaddress: {e}"

    try:
        sig = base64.b64decode(signature_b64, validate=True)
    except Exception:
        return False, "Invalid signature base64"

    canonical = f"name:{endpoint}|{onionaddress}|{name.lower()}|{timestamp}"
    if not verify(public_key, canonical.encode('utf-8'), sig):
        return False, "Invalid signature"

    return True, ""

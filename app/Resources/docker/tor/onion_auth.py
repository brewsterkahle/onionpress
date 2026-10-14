"""
Ed25519 signatures for OnionHeaven API authentication.

Verification is libsodium's, through PyNaCl (nacl.signing.VerifyKey, which
is crypto_sign_open): never computed here. libsodium refuses S >= L, a
small-order or non-canonical public key or R, and compares R byte for
byte. Without PyNaCl, verify() raises VerificationUnavailable rather than
answer: a signature this module cannot check is never taken as good. Only
the hub verifies (web-server.py, in the tor image, which installs
python3-nacl).

Signing is RFC 8032 Ed25519 with a Tor expanded private key: 64 bytes =
scalar a (32, clamped) + nonce prefix (32). Standard ed25519 derives these
from SHA-512(seed); Tor (and the onionpress-onion-keys volume's PEM)
stores them directly, and an mkp224o vanity key has no seed at all.
libsodium and pyca/cryptography sign only from a seed, so sign_expanded
puts RFC 8032 §5.1.6 together from libsodium's own primitives (PyNaCl's
bindings, each constant-time), the way Tor signs with these keys:

    a = a mod L                       crypto_core_ed25519_scalar_reduce
    r = SHA-512(prefix || M) mod L    crypto_core_ed25519_scalar_reduce
    R = r·B                           crypto_scalarmult_ed25519_base_noclamp
    k = SHA-512(R || A || M) mod L    crypto_core_ed25519_scalar_reduce
    S = r + k·a mod L                 crypto_core_ed25519_scalar_mul, _add

with no curve or scalar arithmetic of its own. For a key that comes from a
seed, the signature is byte for byte libsodium's crypto_sign; for every
key it is what the pure-Python signer this replaced made, so the wire
format is unchanged (tests/test_onion_auth.py holds both). A must be a·B
(derive_public_key, libsodium's too): two signatures of one message under
two public keys would give a away, so sign_expanded refuses any other.

Without libsodium's ed25519 functions (no PyNaCl, or a minimal libsodium
build) signing and derive_public_key fall back to the pure-Python
arithmetic OnionPress signed with before, and log a warning once: a
heartbeat that cannot be signed lets the hub take the site over. That
arithmetic is variable-time (how long a signature takes depends on its
nonce). Nothing that ships should reach it: the Mac bundle
(build/py2app-extensions.sh verify) and the tor image (its Dockerfile) are
checked for the functions when they are built, the .deb depends on
python3-nacl, and linux/install.sh installs it.

src/onionpress/onion_auth.py and app/Resources/docker/tor/onion_auth.py
(and tests/stress/onion_auth.py) are one file; tests/test_onion_auth.py
keeps them identical.
"""

import base64
import hashlib
import hmac
import logging
from datetime import datetime, timezone

_log = logging.getLogger(__name__)


def _sha512(data):
    return hashlib.sha512(data).digest()


# ── libsodium (PyNaCl) ──

# What signing and derivation call (all in PyNaCl 1.5.0, the oldest the
# .deb takes).
_SODIUM_FUNCTIONS = (
    "crypto_core_ed25519_scalar_reduce",
    "crypto_core_ed25519_scalar_mul",
    "crypto_core_ed25519_scalar_add",
    "crypto_scalarmult_ed25519_base_noclamp",
)


def _sodium():
    """PyNaCl's bindings, when libsodium has its ed25519 scalar and point
    functions (a minimal build has not), else None.

    Looked up at every call: once python3-nacl is installed, the next
    signature uses it, without a restart."""
    try:
        from nacl import bindings
    except ImportError:
        return None
    if not (getattr(bindings, "has_crypto_core_ed25519", False)
            and getattr(bindings, "has_crypto_scalarmult_ed25519", False)):
        return None
    if not all(hasattr(bindings, name) for name in _SODIUM_FUNCTIONS):
        return None
    return bindings


def signing_uses_libsodium():
    """True if sign_expanded and derive_public_key run on libsodium, False
    if they would use the pure-Python fallback."""
    return _sodium() is not None


_fallback_logged = set()


def _fallback(what):
    """Log, once per process and task, that the fallback is in use."""
    if what in _fallback_logged:
        return
    _fallback_logged.add(what)
    _log.warning(
        "%s: libsodium's ed25519 functions are not available (install "
        "python3-nacl, PyNaCl); using OnionPress's pure-Python ed25519, "
        "which is not constant-time", what)


def _expanded_key_bytes(expanded_key):
    if len(expanded_key) != 64:
        raise ValueError(f"Expanded key must be 64 bytes, got {len(expanded_key)}")
    return bytes(expanded_key)


_NO_PUBLIC_KEY = ("The key has no public key: its secret scalar is zero "
                  "mod the group order")
_NOT_THE_KEYS = "The public key is not the expanded key's own (a·B)"


def _sodium_public_key(sodium, a):
    """a·B for a reduced scalar *a*: libsodium refuses a = 0 (ValueError)."""
    try:
        return sodium.crypto_scalarmult_ed25519_base_noclamp(a)
    except RuntimeError as e:  # nacl.exceptions.RuntimeError
        raise ValueError(_NO_PUBLIC_KEY) from e


def derive_public_key(expanded_key):
    """A = a·B, the 32-byte public key of a 64-byte expanded secret key.

    a (the first half) is taken mod L, as Tor takes it, then multiplied by
    libsodium (crypto_core_ed25519_scalar_reduce,
    crypto_scalarmult_ed25519_base_noclamp). ValueError for a scalar that
    is zero mod L: it has no public key.
    """
    expanded_key = _expanded_key_bytes(expanded_key)
    sodium = _sodium()
    if sodium is None:
        _fallback("Deriving a public key")
        return _python_public_key(expanded_key)
    a = sodium.crypto_core_ed25519_scalar_reduce(expanded_key[:32] + bytes(32))
    return _sodium_public_key(sodium, a)


def sign_expanded(expanded_key, public_key, message):
    """Sign with a Tor expanded private key (RFC 8032 §5.1.6, libsodium).

    expanded_key: 64 bytes (scalar a ‖ nonce prefix)
    public_key:   32 bytes (encoded point A); must be a·B
    message:      bytes

    Returns 64-byte signature (R ‖ S). ValueError for a wrong length, a
    public key that is not the expanded key's, or a scalar with none.
    """
    expanded_key = _expanded_key_bytes(expanded_key)
    if len(public_key) != 32:
        raise ValueError(f"Public key must be 32 bytes, got {len(public_key)}")
    public_key, message = bytes(public_key), bytes(message)

    sodium = _sodium()
    if sodium is None:
        _fallback("Signing")
        return _python_sign(expanded_key, public_key, message)

    reduce = sodium.crypto_core_ed25519_scalar_reduce
    a = reduce(expanded_key[:32] + bytes(32))
    if not hmac.compare_digest(_sodium_public_key(sodium, a), public_key):
        raise ValueError(_NOT_THE_KEYS)
    r = reduce(_sha512(expanded_key[32:] + message))
    # r = 0 (probability 2^-252) has no R; libsodium refuses it and this
    # raises rather than sign.
    R = sodium.crypto_scalarmult_ed25519_base_noclamp(r)
    k = reduce(_sha512(R + public_key + message))
    S = sodium.crypto_core_ed25519_scalar_add(
        r, sodium.crypto_core_ed25519_scalar_mul(k, a))
    return R + S


# ── Fallback without libsodium: pure-Python ed25519 ──
#
# What OnionPress signed with before libsodium did: RFC 8032 in Python
# integers. Double-and-add over the scalar's bits is variable-time. Only
# _sodium() returning None leads here (logged once, see _fallback).

_p = 2**255 - 19  # field prime
_L = 2**252 + 27742317777372353535851937790883648493  # base point order
_d = -121665 * pow(121666, _p - 2, _p) % _p  # curve parameter
_I = pow(2, (_p - 1) // 4, _p)  # sqrt(-1) mod p


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
    """Scalar multiplication via double-and-add (variable-time)."""
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


def _python_public_key(expanded_key):
    a = int.from_bytes(expanded_key[:32], 'little') % _L
    if a == 0:
        raise ValueError(_NO_PUBLIC_KEY)
    return _encode_point(_scalar_mult(a, _B))


def _python_sign(expanded_key, public_key, message):
    if _python_public_key(expanded_key) != public_key:
        raise ValueError(_NOT_THE_KEYS)
    a = int.from_bytes(expanded_key[:32], 'little')
    prefix = expanded_key[32:]

    r = int.from_bytes(_sha512(prefix + message), 'little') % _L
    R_enc = _encode_point(_scalar_mult(r, _B))

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

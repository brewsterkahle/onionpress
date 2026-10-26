# SPDX-License-Identifier: AGPL-3.0-or-later
"""A signature on a release's SHA256SUMS (issues #246 and #386, step 2).

Every release carries SHA256SUMS, and both updaters install only an asset
whose sha256 is its line there. That catches a swapped or damaged asset,
but the file itself comes from the same place as the assets: whoever can
write the GitHub release can rewrite both. So a release may also carry
SHA256SUMS.sig, an Ed25519 signature of the file's bytes by a key the
maintainer holds off GitHub, and the updaters check it against the public
keys embedded here, RELEASE_SIGNING_PUBLIC_KEYS.

Policy: while that tuple is empty (no key has been created yet), nothing
is checked and the updaters say so in their log; once a key is in it,
an update without a SHA256SUMS.sig, or with one that does not verify, is
refused. Several keys may be listed (rotation): a signature by any of
them counts, and the .sig names its key by id.

The .sig format (one text file, SIG_ASSET):

    onionpress-release-signature v1
    key: <key id: the first 16 hex of SHA-256 of the 32-byte public key>
    sig: <base64 of the 64-byte Ed25519 signature of SHA256SUMS' bytes>

libsodium through PyNaCl, as everything else here signs; nothing of our
own. build/release-key.py creates a key, signs and verifies from the
command line (build/release.sh signs when ONIONPRESS_RELEASE_SIGNING_KEY
names the secret key file); `onionpress release-signature verify` is the
Linux launcher's check.
"""

from __future__ import annotations

import base64
import hashlib
import os

try:
    from nacl.signing import SigningKey, VerifyKey
    from nacl.exceptions import BadSignatureError
except ImportError:          # pragma: no cover - the package requires PyNaCl
    SigningKey = VerifyKey = None
    BadSignatureError = Exception

# Hex Ed25519 public keys (64 hex each) whose signatures a release may
# carry. Empty until the maintainer runs `build/release-key.py create`
# and commits the public half here; from then on every update checks.
RELEASE_SIGNING_PUBLIC_KEYS: tuple[str, ...] = ()

SIG_ASSET = "SHA256SUMS.sig"
SIG_HEADER = "onionpress-release-signature v1"
NO_KEY_NOTE = ("no release signing key is embedded in this OnionPress, so SHA256SUMS "
               "is checked by its hashes only (build/release-key.py, issue #246)")


class SignatureError(Exception):
    """A .sig that cannot be read or does not verify."""


def key_id(public_key: bytes) -> str:
    return hashlib.sha256(public_key).hexdigest()[:16]


def public_keys() -> list[bytes]:
    return [bytes.fromhex(k) for k in RELEASE_SIGNING_PUBLIC_KEYS]


def create_keypair() -> tuple[bytes, bytes]:
    """(32-byte seed, 32-byte public key) of a fresh Ed25519 key."""
    if SigningKey is None:
        raise SignatureError("PyNaCl is not installed")
    sk = SigningKey.generate()
    return bytes(sk), bytes(sk.verify_key)


def sign(sums: bytes, seed: bytes) -> str:
    """The .sig text for *sums* under the key whose seed is *seed*."""
    if SigningKey is None:
        raise SignatureError("PyNaCl is not installed")
    if len(seed) != 32:
        raise SignatureError("a secret key is 32 bytes")
    sk = SigningKey(seed)
    sig = sk.sign(sums).signature
    return (f"{SIG_HEADER}\nkey: {key_id(bytes(sk.verify_key))}\n"
            f"sig: {base64.b64encode(sig).decode('ascii')}\n")


def parse_sig(text: str) -> tuple[str, bytes]:
    """(key id, signature bytes) of a .sig text, or SignatureError."""
    lines = [l.strip() for l in text.strip().splitlines()]
    if not lines or lines[0] != SIG_HEADER:
        raise SignatureError(f"not a {SIG_HEADER} file")
    fields = {}
    for line in lines[1:]:
        if ":" not in line:
            raise SignatureError("malformed line in the signature file")
        k, v = line.split(":", 1)
        fields[k.strip()] = v.strip()
    kid = fields.get("key", "")
    if len(kid) != 16 or any(c not in "0123456789abcdef" for c in kid):
        raise SignatureError("the signature names no key id")
    try:
        sig = base64.b64decode(fields.get("sig", ""), validate=True)
    except Exception:
        raise SignatureError("the signature is not base64")
    if len(sig) != 64:
        raise SignatureError("the signature is not 64 bytes")
    return kid, sig


def verify(sums: bytes, sig_text: str, keys: list[bytes] | None = None) -> str:
    """The id of the embedded key that signed *sums*, or SignatureError."""
    if VerifyKey is None:
        raise SignatureError("PyNaCl is not installed")
    keys = public_keys() if keys is None else keys
    kid, sig = parse_sig(sig_text)
    for pub in keys:
        if key_id(pub) != kid:
            continue
        try:
            VerifyKey(pub).verify(sums, sig)
        except BadSignatureError:
            raise SignatureError(f"the signature by key {kid} does not verify")
        return kid
    raise SignatureError(f"the signature's key {kid} is not one this OnionPress trusts")


def check_release_sums(sums: bytes, sig_text: str | None,
                       keys: list[bytes] | None = None) -> tuple[bool, str]:
    """(ok, note) for a release's SHA256SUMS and its .sig (None when the
    release carries none), under the policy in the module docstring."""
    keys = public_keys() if keys is None else keys
    if not keys:
        return True, NO_KEY_NOTE
    if sig_text is None:
        return False, (f"the release carries no {SIG_ASSET}; this OnionPress installs "
                       f"only a release whose SHA256SUMS is signed")
    try:
        kid = verify(sums, sig_text, keys)
    except SignatureError as e:
        return False, f"{SIG_ASSET} refused: {e}"
    return True, f"SHA256SUMS signed by release key {kid}"


def verify_files(sums_path: str, sig_path: str | None) -> int:
    """`onionpress release-signature verify`: 0 verified or unchecked (no
    key embedded; said on stdout), 1 refused, 2 a file could not be read."""
    try:
        with open(sums_path, "rb") as f:
            sums = f.read(1 << 20)
        sig_text = None
        if sig_path and os.path.exists(sig_path):
            with open(sig_path, "r", encoding="utf-8", errors="replace") as f:
                sig_text = f.read(4096)
    except OSError as e:
        print(f"release-signature: {e}")
        return 2
    ok, note = check_release_sums(sums, sig_text)
    print(note)
    return 0 if ok else 1

#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The release signing key (issues #246 and #386): create, sign, verify.

  build/release-key.py create <secret-key-file>
      Makes an Ed25519 key. The 32-byte seed goes to <secret-key-file> as
      64 hex, mode 0600, never into the repository; the public half is
      printed as the line to add to RELEASE_SIGNING_PUBLIC_KEYS in
      src/onionpress/release_signing.py (commit that, ship it, and every
      update from then on checks).
  build/release-key.py sign <SHA256SUMS> [--key <secret-key-file>]
      Writes <SHA256SUMS>.sig (the key from --key, else the file named by
      $ONIONPRESS_RELEASE_SIGNING_KEY). build/release.sh calls this.
  build/release-key.py verify <SHA256SUMS> <SHA256SUMS.sig> [--public-key <hex>]
      Checks the signature against the embedded keys (or the one given).

Keep the secret key off every machine that can write the GitHub release
and out of every CI secret: the point is that a compromised GitHub
account can publish a release but not sign one.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from onionpress import release_signing as rs  # noqa: E402


def _read_seed(path):
    with open(path, "r", encoding="utf-8") as f:
        text = f.read().strip()
    try:
        seed = bytes.fromhex(text)
    except ValueError:
        seed = b""
    if len(seed) != 32:
        sys.exit(f"{path}: not a 32-byte hex seed")
    return seed


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("secret_key_file")
    s = sub.add_parser("sign")
    s.add_argument("sums")
    s.add_argument("--key", default=os.environ.get("ONIONPRESS_RELEASE_SIGNING_KEY", ""))
    v = sub.add_parser("verify")
    v.add_argument("sums")
    v.add_argument("sig")
    v.add_argument("--public-key", default="")
    args = p.parse_args(argv)

    if args.cmd == "create":
        if os.path.exists(args.secret_key_file):
            sys.exit(f"{args.secret_key_file} exists; not overwriting a key")
        seed, pub = rs.create_keypair()
        fd = os.open(args.secret_key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(seed.hex() + "\n")
        print(f"secret key written to {args.secret_key_file} (keep it off GitHub and CI)")
        print(f"key id: {rs.key_id(pub)}")
        print("add to RELEASE_SIGNING_PUBLIC_KEYS in src/onionpress/release_signing.py:")
        print(f'    "{pub.hex()}",')
        return 0

    if args.cmd == "sign":
        if not args.key:
            sys.exit("no key: --key <file> or ONIONPRESS_RELEASE_SIGNING_KEY")
        seed = _read_seed(args.key)
        with open(args.sums, "rb") as f:
            sums = f.read()
        text = rs.sign(sums, seed)
        out = args.sums + ".sig"
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"{out}: {text.splitlines()[1]}")
        return 0

    with open(args.sums, "rb") as f:
        sums = f.read()
    with open(args.sig, "r", encoding="utf-8") as f:
        sig_text = f.read()
    keys = [bytes.fromhex(args.public_key)] if args.public_key else None
    if keys is None and not rs.public_keys():
        print("no public key embedded or given; nothing to verify against")
        return 2
    try:
        kid = rs.verify(sums, sig_text, keys)
    except rs.SignatureError as e:
        print(f"REFUSED: {e}")
        return 1
    print(f"verified: signed by key {kid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

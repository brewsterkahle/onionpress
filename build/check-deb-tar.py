#!/usr/bin/env python3
#
# Refuse a .deb member tarball that dpkg cannot unpack.
#
# Usage:
#   python3 -I build/check-deb-tar.py NAME TARFILE|-
#
# Reads a plain or gzip'd tar (`-` is stdin) and fails on any PAX header,
# extended ('x') or global ('g'). dpkg's own unpacker reads ustar and GNU
# tar only and stops with "unsupported PAX tar header type 'x'", while GNU
# tar, `dpkg-deb --contents` and lintian merely warn about the keywords and
# carry on. 2.7.0's Mac-built package had one on every member: bsdtar
# writes each file's com.apple.provenance attribute as a PAX header.
# build/check-deb.sh runs this on both of a package's tarballs, and
# build/build-linux.sh on the ones its macOS fallback writes.

import sys
import tarfile


def pax_members(fileobj):
    """(global headers, names of members carrying PAX headers)."""
    with tarfile.open(fileobj=fileobj, mode="r|*") as tar:
        names = [m.name for m in tar if m.pax_headers]
        return dict(tar.pax_headers), names


def main(argv):
    if len(argv) != 3:
        print("usage: check-deb-tar.py NAME TARFILE|-", file=sys.stderr)
        return 2
    label, path = argv[1], argv[2]
    try:
        if path == "-":
            global_headers, names = pax_members(sys.stdin.buffer)
        else:
            with open(path, "rb") as f:
                global_headers, names = pax_members(f)
    except (OSError, tarfile.TarError) as e:
        print(f"{label}: cannot read the tar: {e}", file=sys.stderr)
        return 1
    if not global_headers and not names:
        return 0
    keys = sorted(set(global_headers))
    print(f"{label}: PAX headers, which dpkg cannot unpack "
          f"({len(names)} members{', a global header' if global_headers else ''}):",
          file=sys.stderr)
    for name in names[:5]:
        print(f"    {name}", file=sys.stderr)
    if keys:
        print(f"    global keys: {', '.join(keys)}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))

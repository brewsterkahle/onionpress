#!/bin/bash
# Regenerate the Mac bundle's hash-pinned Python locks (issue #387):
#   build/requirements-macos.lock        from src/requirements.txt (runtime)
#   build/build-requirements-macos.lock  from build/build-requirements-macos.in
#                                        (pip, py2app, setuptools, wheel)
# build-dmg-simple.sh, rebuild-menubar.sh and py2app-extensions.sh install
# from them with pip's --require-hashes, so PyPI serves only bytes the
# locks name. Run this after changing either input or to take newer
# releases, review the diff, commit both locks. Needs uv and the network.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=3.14
uv pip compile src/requirements.txt --generate-hashes \
    --python-platform macos --python-version "$PY" -o build/requirements-macos.lock
uv pip compile build/build-requirements-macos.in --generate-hashes \
    --python-platform macos --python-version "$PY" -o build/build-requirements-macos.lock
echo "Locks written; review with git diff build/*.lock"

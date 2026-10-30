#!/bin/bash
# The tor image (and the stress image) are built from copies of the key
# modules in src/onionpress/, which is their one source:
#   onion_auth.py  -> app/Resources/docker/tor/, tests/stress/
#   onion_keys.py  -> app/Resources/docker/tor/
# Run this after editing either; tests/test_onion_keys.py and
# tests/test_onion_auth.py fail while a copy differs.
set -euo pipefail
cd "$(dirname "$0")/.."
cp src/onionpress/onion_auth.py app/Resources/docker/tor/onion_auth.py
cp src/onionpress/onion_auth.py tests/stress/onion_auth.py
cp src/onionpress/onion_keys.py app/Resources/docker/tor/onion_keys.py
echo "Synced onion_auth.py and onion_keys.py from src/onionpress/"

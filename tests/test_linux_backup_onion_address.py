#!/usr/bin/env python3
"""Linux create_backup takes only a whole onion address from Tor.

linux/onionpress's create_backup read the address from the tor
container's hostname file (a file that container writes) and put it in
metadata.json unchecked, and the settings page's backup named its file
after it. A restore refuses metadata whose address is not a whole v3
address (backup.validate_metadata), so a bad value made a backup that
could never be restored, discovered only then. create_backup now checks
it with is_onion_address before any work and fails with a clear error;
no address at all (tor down) stays empty, as before. The settings page
names the file after a whole address only, "site" otherwise.

The launcher's own functions run under bash with docker, db_client and
op_py stubbed as shell functions. No Docker.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LINUX = os.path.join(PROJECT_DIR, "linux", "onionpress")
SITE = "op2" + "a" * 53 + ".onion"
LOOSE = [
    "evil.example/" + "a" * 39 + ".onion",
    "../../" + "a" * 50 + ".onion",
    SITE + "evil",
    SITE.upper(),
    "www." + SITE,
    "x.onion",
    'a"b',
]

STUBS = r'''
log() { printf '%s\n' "$*" >> "$LOG_FILE"; }
python3() { "$PY" "$@"; }
verify_wp_admin_password() { cat >/dev/null; return 0; }
setup_db_passwords() { WORDPRESS_DB_PASSWORD=x; }
db_client() { echo "-- dump"; }
op_py() {
    case "$1" in
        backup-metadata) shift; PYTHONPATH="$SRC" "$PY" -c 'import sys
from onionpress.cli import main
sys.exit(main(sys.argv[1:]))' backup-metadata "$@" ;;
        pack-backup) cat >/dev/null; cp "$3/metadata.json" "$META_OUT"; : > "$5" ;;
        --data-dir) if [ "$3" = backup-metadata ]; then shift 2; op_py "$@"; else [ "$3" != using-local-images ]; fi ;;   # the predicate: published images
        *) return 0 ;;
    esac
}
docker() {
    printf '%s\n' "$*" >> "$CALLS"
    if [ "$1 $2 $3" = "exec onionpress-tor cat" ]; then
        printf '%s' "$FAKE_HOSTNAME"
        return 0
    fi
    # The key helpers' tor image is here; no key volume, so nothing is read.
    case "$1 $2" in
        "image inspect"|"volume ls") return 0 ;;
    esac
    return 1
}
'''


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _function(text, name):
    """A top-level bash function, to its last `}` line before the next one
    (create_backup's inline Python has a `}` line of its own)."""
    start = text.index(f"\n{name}() {{") + 1
    nxt = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\(\) \{", re.M).search(text, start + 1)
    chunk = text[start:nxt.start() if nxt else len(text)]
    return chunk[:chunk.rindex("\n}\n") + 3]


class TestSettingsPageFileName(unittest.TestCase):

    def test_named_after_a_whole_address_only(self):
        # The Settings page's backup is named by the shared handler
        # (onionpress.handle_action, issue #366): the served address only
        # when it is a whole v3 address (Host.served_address), else "site".
        text = _read(os.path.join(PROJECT_DIR, "src", "onionpress", "handle_action.py"))
        branch = text[text.index("def _create_backup(host):"):text.index("# -- restore --")]
        self.assertIn("addr = host.served_address()", branch)
        self.assertIn('onion_short = addr.replace(".onion", "")[:8] if addr else "site"', branch)
        served = text[text.index("    def served_address(self):"):text.index("    def containers(self):")]
        self.assertIn("return onion_hostname.read_served_address(self.docker)", served)
        self.assertNotIn("\n                create-backup)\n", _read(LINUX))


if __name__ == "__main__":
    unittest.main()

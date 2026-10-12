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
        pack-backup) cat >/dev/null; cp "$3/metadata.json" "$META_OUT"; : > "$5" ;;
        *) return 0 ;;
    esac
}
docker() {
    printf '%s\n' "$*" >> "$CALLS"
    if [ "$1 $2 $3" = "exec onionpress-tor cat" ]; then
        printf '%s' "$FAKE_HOSTNAME"
        return 0
    fi
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


class TestCreateBackupAddress(unittest.TestCase):

    def _run(self, hostname):
        text = _read(LINUX)
        script = (STUBS + _function(text, "is_onion_address") + _function(text, "create_backup")
                  + 'rc=0; create_backup pw "$OUT" || rc=$?; echo "rc=$rc"\n')
        with tempfile.TemporaryDirectory() as tmp:
            env = {"PATH": "/usr/bin:/bin", "HOME": tmp, "TMPDIR": tmp,
                   "PY": sys.executable, "DATA_DIR": os.path.join(tmp, "data"),
                   "LOG_FILE": os.path.join(tmp, "log"), "CALLS": os.path.join(tmp, "calls"),
                   "META_OUT": os.path.join(tmp, "metadata.json"),
                   "OUT": os.path.join(tmp, "backup.zip"), "FAKE_HOSTNAME": hostname}
            r = subprocess.run(["bash", "-c", script], env=env, capture_output=True,
                               text=True, timeout=60)
            log = _read(env["LOG_FILE"]) if os.path.exists(env["LOG_FILE"]) else ""
            calls = _read(env["CALLS"]).splitlines() if os.path.exists(env["CALLS"]) else []
            meta = (json.loads(_read(env["META_OUT"]))
                    if os.path.exists(env["META_OUT"]) else None)
        return r, log, calls, meta

    def test_a_whole_address_goes_into_the_metadata(self):
        for hostname in (SITE + "\n", SITE):
            with self.subTest(hostname=hostname):
                r, log, _, meta = self._run(hostname)
                self.assertEqual(r.stdout.strip(), "rc=0", r.stderr + log)
                self.assertEqual(meta["onion_address"], SITE)

    def test_no_address_stays_empty(self):
        r, log, _, meta = self._run("")
        self.assertEqual(r.stdout.strip(), "rc=0", r.stderr + log)
        self.assertEqual(meta["onion_address"], "")

    def test_anything_else_fails_before_any_work(self):
        for hostname in LOOSE:
            with self.subTest(hostname=hostname):
                r, log, calls, meta = self._run(hostname)
                self.assertEqual(r.stdout.strip(), "rc=1")
                self.assertIn("not a valid v3 .onion address; no backup was made", r.stderr)
                self.assertIn("ERROR: Backup: the tor container's onion address is not a valid v3 address", log)
                self.assertNotIn(hostname, log, "the bad value is not logged")
                self.assertIsNone(meta)
                self.assertEqual(len(calls), 1, calls)
                self.assertTrue(calls[0].startswith("exec onionpress-tor cat "), calls)


class TestSettingsPageFileName(unittest.TestCase):

    def test_named_after_a_whole_address_only(self):
        text = _read(LINUX)
        start = text.index("                create-backup)")
        branch = text[start:text.index("                restore-backup)", start)]
        self.assertNotIn("cut -c1-8", branch)
        self.assertIn('if is_onion_address "$bp_onion"; then', branch)
        self.assertIn('bp_onion="${bp_onion:0:8}"', branch)
        self.assertIn('bp_onion="site"', branch)
        self.assertLess(branch.index('bp_onion="site"'),
                        branch.index('local bp_filename="OnionPress-${bp_onion}-'))


if __name__ == "__main__":
    unittest.main()

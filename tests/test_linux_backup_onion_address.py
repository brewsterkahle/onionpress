#!/usr/bin/env python3
"""The Settings page's backup is named after a whole onion address only.

linux/onionpress's bash create_backup used to read the address from the
tor container's hostname file and put it in metadata.json unchecked; the
backup is backup.create_backup's on both platforms now (issue #406), which
derives the address from the key itself (tests/test_linux_backup_key_pem.py)
and never reads that file. The settings page names its file after the
served address only when it is a whole v3 address, "site" otherwise.
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

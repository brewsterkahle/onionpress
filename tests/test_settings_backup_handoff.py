#!/usr/bin/env python3
"""The Settings page's backup reaches the container as www-data's file,
written by www-data.

The backup is handed to the WordPress container at a predictable name in
/var/lib/onionpress, a folder www-data writes. It used to be `docker cp`'d
and then `chown www-data`'d as root: both follow a link www-data can plant
at that name, so the chown made any file in the container (an entrypoint
root runs, say) www-data's. Both platforms now write it with
`docker exec -u www-data`, which can reach only what www-data could anyway.

No docker: the Mac handler is AppKit-bound and checked as source; the Linux
branch runs for real with docker stubbed.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_backup_metadata_validation as mv  # noqa: E402


def _branch(text, head, end):
    start = text.index(head) + len(head)
    return text[start:text.index(end, start)]


class TestHandlerHandoff(unittest.TestCase):
    """The shared handler's create-backup (onionpress.handle_action, both
    platforms): the zip is written into the container as www-data, never
    docker cp + chown as root, and the local copy is removed whatever
    happens. tests/test_handle_action.py runs it; this reads the source."""

    def setUp(self):
        text = mv._read("src/onionpress/handle_action.py")
        self.body = text[text.index("def _create_backup(host):"):text.index("# -- restore --")]

    def test_written_as_www_data_never_chowned_by_root(self):
        code = "\n".join(line for line in self.body.splitlines()
                         if not line.lstrip().startswith("#"))
        self.assertIn('"exec", "-i", "-u", "www-data"', code)
        self.assertIn("""'umask 077; cat > "$1"'""", code)
        self.assertNotIn('"chown"', code)
        self.assertNotIn('"cp"', code)
        self.assertNotIn("docker cp", code)

    def test_the_local_copy_is_removed_whatever_happens(self):
        handoff = self.body[self.body.index('"-u", "www-data"'):]
        self.assertLess(handoff.index("finally:"), handoff.index("shutil.rmtree(work"))

    def test_the_launchers_hand_nothing_over_themselves(self):
        for rel in ("linux/onionpress", "app/MacOS/onionpress", "src/menubar.py"):
            with self.subTest(rel):
                self.assertNotIn('umask 077; cat > "$1"', mv._read(rel))


if __name__ == "__main__":
    unittest.main()

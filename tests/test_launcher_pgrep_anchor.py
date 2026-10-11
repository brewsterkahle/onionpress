#!/usr/bin/env python3
"""The launchers find the running MenubarApp with an anchored pgrep.

Incident (2026-10-11): launcher.sh decided "app already running" because
`pgrep -f "MenubarApp/Contents/MacOS/OnionPress"` matched a *shell* whose
command line quoted that path, so it touched .reopen and exited — leaving
no MenubarApp running at all. The same bare pattern in `onionpress quit`
would SIGTERM that shell instead of the app.

The real process's command line is exactly the executable path, with no
arguments, so the pattern is anchored: nothing before the path but the
path itself (no spaces), and nothing after it but a space or the end.
"""

import os
import platform
import re
import shutil
import subprocess
import time
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Keep in sync with app/MacOS/launcher.sh and app/MacOS/onionpress.
ANCHORED = r"^[^ ]*MenubarApp/Contents/MacOS/OnionPress( |$)"
BARE = "MenubarApp/Contents/MacOS/OnionPress"

REAL = "/Applications/OnionPress.app/Contents/Resources/MenubarApp/Contents/MacOS/OnionPress"


def _read(rel):
    with open(os.path.join(PROJECT_ROOT, rel), encoding="utf-8") as f:
        return f.read()


class TestScriptsUseAnchoredPattern(unittest.TestCase):
    SCRIPTS = ("app/MacOS/launcher.sh", "app/MacOS/onionpress")

    def test_anchored_pattern_present(self):
        for rel in self.SCRIPTS:
            self.assertIn(f"pgrep -u \"$(whoami)\" -f '{ANCHORED}'", _read(rel), rel)

    def test_bare_substring_pattern_gone(self):
        for rel in self.SCRIPTS:
            self.assertNotRegex(
                _read(rel), re.compile(r'pgrep[^\n]*-f\s+"' + re.escape(BARE) + '"'),
                f"{rel}: bare pgrep -f substring match is back")


class TestPatternSemantics(unittest.TestCase):
    """pgrep -f applies an extended regex to the full command line; Python's
    re agrees with ERE for this pattern."""

    def test_matches_the_real_process(self):
        self.assertRegex(REAL, ANCHORED)
        # Other install locations, and a hypothetical argument, still match.
        self.assertRegex("/Users/x/Apps/OnionPress.app/Contents/Resources/"
                         "MenubarApp/Contents/MacOS/OnionPress", ANCHORED)
        self.assertRegex(REAL + " -psn_0_12345", ANCHORED)

    def test_ignores_processes_that_merely_name_the_path(self):
        for cmdline in (
            f'zsh -c pgrep -fl "{BARE}" | head -1',
            f"bash -c kill $(pgrep -f {BARE})",
            f"tail -f {REAL}.log",
            f"grep -r {BARE} /Applications",
            f"vim {REAL}",
            f"/usr/bin/open {REAL}",
            REAL + ".bak",
        ):
            self.assertNotRegex(cmdline, ANCHORED, cmdline)
            # ...each of which the old bare pattern did match:
            self.assertIn(BARE, cmdline)


@unittest.skipUnless(platform.system() in ("Darwin", "Linux") and shutil.which("pgrep"),
                     "needs the system pgrep")
class TestLivePgrep(unittest.TestCase):
    """Run the real pgrep against a decoy process whose argv contains the
    path: the bare pattern finds it, the anchored one must not."""

    def test_decoy_is_not_matched(self):
        # sh -c with the path as $0: argv holds the substring, but the
        # command line starts with "sh". Two commands, so sh does not
        # exec the single `sleep` in place and drop its own argv.
        decoy = subprocess.Popen(["sh", "-c", "sleep 20; true", BARE])
        try:
            time.sleep(0.3)
            bare = subprocess.run(["pgrep", "-f", BARE],
                                  capture_output=True, text=True).stdout.split()
            anchored = subprocess.run(["pgrep", "-f", ANCHORED],
                                      capture_output=True, text=True).stdout.split()
            self.assertIn(str(decoy.pid), bare, "decoy should prove the bare match")
            self.assertNotIn(str(decoy.pid), anchored)
        finally:
            decoy.kill()
            decoy.wait()


if __name__ == "__main__":
    unittest.main()

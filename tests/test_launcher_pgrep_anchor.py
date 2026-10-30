#!/usr/bin/env python3
"""The launchers find the running MenubarApp with an anchored pgrep.

Incident (2026-10-11): launcher.sh decided "app already running" because
`pgrep -f "MenubarApp/Contents/MacOS/OnionPress"` matched a *shell* whose
command line quoted that path, so it touched .reopen and exited — leaving
no MenubarApp running at all. The same bare pattern in `onionpress quit`
would SIGTERM that shell instead of the app.

The real process's command line is exactly the executable path, with no
arguments, so the pattern is anchored: nothing before the path but the
path itself, and nothing after it but a space or the end.

Issue #372: launcher.sh's check matched ANY bundle's MenubarApp of this
user (`^[^ ]*MenubarApp/...`), so a second bundle (a build under test, with
its own HOME and data dir) never started while the installed one ran: it
logged "app already running", touched .reopen in the OTHER install's data
dir and exited. Both launchers now look for this bundle's own executable
path; launcher.sh quotes the path's ERE metacharacters first.
"""

import os
import platform
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Keep in sync with app/MacOS/launcher.sh and app/MacOS/onionpress.
ANCHORED = r"^[^ ]*MenubarApp/Contents/MacOS/OnionPress( |$)"
BARE = "MenubarApp/Contents/MacOS/OnionPress"
# launcher.sh (issue #372): this bundle's executable, quoted for an ERE.
LAUNCHER_SH_CHECK = (
    'MENUBAR_EXE="$MENUBAR_APP/Contents/MacOS/OnionPress"\n'
    "MENUBAR_EXE_RE=$(printf '%s' \"$MENUBAR_EXE\" | sed 's/[][*.^$()+?{}|\\\\]/\\\\&/g')\n"
    'if pgrep -u "$(whoami)" -f "^${MENUBAR_EXE_RE}( |$)" >/dev/null 2>&1; then\n'
)

REAL = "/Applications/OnionPress.app/Contents/Resources/MenubarApp/Contents/MacOS/OnionPress"


def _read(rel):
    with open(os.path.join(PROJECT_ROOT, rel), encoding="utf-8") as f:
        return f.read()


class TestScriptsUseAnchoredPattern(unittest.TestCase):
    SCRIPTS = ("app/MacOS/launcher.sh", "app/MacOS/onionpress")

    def test_anchored_pattern_present(self):
        for rel in self.SCRIPTS:
            text = _read(rel)
            if rel.endswith("/onionpress"):
                # The launcher's quit looks for THIS bundle's app (a test copy
                # beside the installed one must not be stopped), anchored
                # the same way (issue #367).
                self.assertIn('pgrep -u "$(whoami)" -f "^$RESOURCES_DIR/MenubarApp/Contents/MacOS/OnionPress( |$)"',
                              text, rel)
            else:
                self.assertIn(LAUNCHER_SH_CHECK, text, rel)

    def test_bare_substring_pattern_gone(self):
        for rel in self.SCRIPTS:
            self.assertNotRegex(
                _read(rel), re.compile(r'pgrep[^\n]*-f\s+"' + re.escape(BARE) + '"'),
                f"{rel}: bare pgrep -f substring match is back")

    def test_any_bundle_pattern_gone(self):
        """No launcher looks for any bundle's MenubarApp (issue #372)."""
        for rel in self.SCRIPTS:
            self.assertNotIn(ANCHORED, _read(rel), rel)


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



def _launcher_check_block():
    """launcher.sh's already-running check, from MENUBAR_EXE= to its fi."""
    text = _read("app/MacOS/launcher.sh")
    start = text.index('MENUBAR_EXE="$MENUBAR_APP/Contents/MacOS/OnionPress"')
    end = text.index("\nfi\n", start) + len("\nfi\n")
    return text[start:end]


PGREP_STUB = """#!/bin/bash
# pgrep -u USER -f PATTERN against the command lines in $OP_PROCS, one per
# line: exit 0 when one matches the ERE, 1 otherwise, as pgrep does.
user=""; pattern=""
while [ $# -gt 0 ]; do
    case "$1" in
        -u) user="$2"; shift 2 ;;
        -f) pattern="$2"; shift 2 ;;
        *) echo "unexpected pgrep argument: $1" >&2; exit 2 ;;
    esac
done
[ "$user" = "$(whoami)" ] || { echo "pgrep -u $user: not this user" >&2; exit 2; }
printf '%s\\n' "$pattern" > "$OP_PGREP_PATTERN"
grep -E -q -- "$pattern" "$OP_PROCS"
"""


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class TestLauncherChecksOnlyItsOwnBundle(unittest.TestCase):
    """launcher.sh's check, run under bash against a stub pgrep that
    answers from a list of command lines (issue #372)."""

    LIVE = "/Applications/OnionPress.app"
    TEST = "/Users/me/Builds/OnionPress (1)+.app"

    @staticmethod
    def _exe(bundle):
        return f"{bundle}/Contents/Resources/MenubarApp/Contents/MacOS/OnionPress"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="onionpress-launcher-check-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = os.path.join(self.tmp, "bin")
        os.mkdir(self.bin)
        stub = os.path.join(self.bin, "pgrep")
        with open(stub, "w") as f:
            f.write(PGREP_STUB)
        os.chmod(stub, 0o755)
        self.data_dir = os.path.join(self.tmp, "data")
        os.mkdir(self.data_dir)
        self.procs = os.path.join(self.tmp, "procs")
        self.pattern_file = os.path.join(self.tmp, "pattern")

    def _run(self, bundle, procs, *, real_pgrep=False):
        with open(self.procs, "w") as f:
            f.write("".join(p + "\n" for p in procs))
        script = (
            "set -e\n"
            f"MENUBAR_APP={shlex.quote(bundle + '/Contents/Resources/MenubarApp')}\n"
            f"DATA_DIR={shlex.quote(self.data_dir)}\n"
            f"LOG_FILE={shlex.quote(os.path.join(self.tmp, 'launcher.log'))}\n"
            'log() { echo "$1" >> "$LOG_FILE"; }\n'
            + _launcher_check_block()
            + 'echo "launched: $MENUBAR_EXE"\n'
        )
        env = dict(os.environ, OP_PROCS=self.procs, OP_PGREP_PATTERN=self.pattern_file)
        if not real_pgrep:
            env["PATH"] = self.bin + os.pathsep + os.environ.get("PATH", "")
        r = subprocess.run(["bash", "-c", script], env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        return r.stdout

    def _reopen_touched(self):
        return os.path.exists(os.path.join(self.data_dir, ".reopen"))

    def test_another_bundles_app_does_not_count(self):
        """The installed app runs; a bundle under test starts anyway and
        leaves the other install's .reopen alone."""
        out = self._run(self.TEST, [self._exe(self.LIVE)])
        self.assertEqual(out, f"launched: {self._exe(self.TEST)}\n")
        self.assertFalse(self._reopen_touched())

    def test_the_bundles_own_app_counts(self):
        for cmdline in (self._exe(self.TEST), self._exe(self.TEST) + " -psn_0_12345"):
            with self.subTest(cmdline=cmdline):
                out = self._run(self.TEST, [self._exe(self.LIVE), cmdline])
                self.assertEqual(out, "")
                self.assertTrue(self._reopen_touched())
                os.unlink(os.path.join(self.data_dir, ".reopen"))

    def test_the_installed_bundle_still_finds_itself(self):
        out = self._run(self.LIVE, [self._exe(self.LIVE)])
        self.assertEqual(out, "")
        self.assertTrue(self._reopen_touched())

    def test_processes_that_merely_name_the_path_do_not_count(self):
        exe = self._exe(self.TEST)
        out = self._run(self.TEST, [
            f"zsh -c pgrep -fl {exe}",
            f"tail -f {exe}.log",
            f"vim {exe}",
            exe + ".bak",
            # The metacharacters are quoted: "." matches no other character,
            # "(1)+" is not a group.
            exe.replace("OnionPress (1)+.app", "OnionPressX(1)+.app"),
            exe.replace("OnionPress (1)+.app", "OnionPress (1)+Xapp"),
            exe.replace("OnionPress (1)+.app", "OnionPress 1.app"),
            exe.replace("OnionPress (1)+.app", "OnionPress (11).app"),
        ])
        self.assertEqual(out, f"launched: {exe}\n")
        self.assertFalse(self._reopen_touched())

    def test_pattern_is_the_quoted_path(self):
        self._run(self.TEST, [])
        with open(self.pattern_file) as f:
            pattern = f.read().rstrip("\n")
        self.assertEqual(
            pattern,
            r"^/Users/me/Builds/OnionPress \(1\)\+\.app/Contents/Resources/"
            r"MenubarApp/Contents/MacOS/OnionPress( |$)")
        # ...which Python's ERE-compatible re reads the same way.
        self.assertRegex(self._exe(self.TEST), pattern)
        self.assertNotRegex(self._exe(self.LIVE), pattern)

    @unittest.skipUnless(platform.system() in ("Darwin", "Linux") and shutil.which("pgrep"),
                         "needs the system pgrep")
    def test_with_the_real_pgrep(self):
        """A process whose argv[0] is the bundle's executable path (as the
        MenubarApp's is) is found by the system pgrep; another bundle's is
        not. That other bundle is one nothing runs: /Applications'
        OnionPress may well be running on the Mac under test."""
        other = os.path.join(self.tmp, "Elsewhere", "OnionPress.app")
        decoy = subprocess.Popen([self._exe(self.TEST), "20"], executable="/bin/sleep")
        try:
            time.sleep(0.3)
            self.assertEqual(self._run(self.TEST, [], real_pgrep=True), "")
            self.assertTrue(self._reopen_touched())
            os.unlink(os.path.join(self.data_dir, ".reopen"))
            self.assertEqual(self._run(other, [], real_pgrep=True),
                             f"launched: {self._exe(other)}\n")
            self.assertFalse(self._reopen_touched())
        finally:
            decoy.kill()
            decoy.wait()


if __name__ == "__main__":
    unittest.main()

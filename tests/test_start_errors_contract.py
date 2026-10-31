#!/usr/bin/env python3
"""The START SUMMARY contract (issue #442.2, onionpress.start): a Python
start step appends the names of the steps it could not do to the file the
Mac launcher hands it (`--errors-file`), and the launcher's
note_start_errors reads them into START_ERRORS, which log_start_summary
prints. Linux passes no file and has no summary."""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

from onionpress.start import StartErrors  # noqa: E402

MAC = os.path.join(ROOT, "app", "MacOS", "onionpress")
LINUX = os.path.join(ROOT, "linux", "onionpress")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _function(text, name):
    start = text.index(f"\n{name}() {{")
    return text[start:text.index("\n}\n", start) + 3]


class TestStartErrors(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "errors")
        open(self.path, "w").close()

    def test_names_are_appended_as_they_happen(self):
        errors = StartErrors(self.path)
        errors.add("tor")
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "tor\n")
        errors.add("provision-post-install")
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "tor\nprovision-post-install\n")
        self.assertEqual(errors.names, ["tor", "provision-post-install"])
        self.assertTrue(errors)

    def test_only_the_bash_shape(self):
        for bad in ("", "Tor", "a b", "x;rm", "-tor", "tor-", "tor\nx"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                StartErrors(self.path).add(bad)

    def test_without_a_file_the_names_are_kept(self):
        errors = StartErrors()
        self.assertFalse(errors)
        errors.add("tor")
        self.assertEqual(errors.names, ["tor"])

    def test_a_link_at_the_name_is_not_followed(self):
        target = os.path.join(self.tmp, "target")
        open(target, "w").close()
        os.unlink(self.path)
        os.symlink(target, self.path)
        StartErrors(self.path).add("tor")
        with open(target, encoding="utf-8") as f:
            self.assertEqual(f.read(), "")

    def test_a_missing_file_is_not_an_error(self):
        os.unlink(self.path)
        errors = StartErrors(self.path)
        errors.add("tor")
        self.assertEqual(errors.names, ["tor"])
        self.assertFalse(os.path.exists(self.path))


class TestMacSummaryReadsTheFile(unittest.TestCase):
    """The Mac launcher's note_start_errors + log_start_summary under
    /bin/bash: a step's name written by the Python reaches the summary."""

    def _run(self, file_text):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        text = _read(MAC)
        errors = os.path.join(tmp, "errors")
        with open(errors, "w", encoding="utf-8") as f:
            f.write(file_text)
        script = (
            "set -e\n"
            f'LOG_FILE="{tmp}/log"\n'
            'log() { echo "$1" | tee -a "$LOG_FILE"; }\n'
            'START_ERRORS=" tor"\n'
            + _function(text, "start_errors_file")
            + _function(text, "note_start_errors")
            + _function(text, "log_start_summary")
            + f'f=$(start_errors_file); [ -f "$f" ]; rm -f "$f"\n'
            f'note_start_errors "{errors}"\n'
            'note_start_errors ""\n'
            'note_start_errors "/nonexistent/errors"\n'
            f'[ ! -e "{errors}" ] || echo "file left"\n'
            "log_start_summary\n")
        res = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True,
                             env={"PATH": "/usr/bin:/bin", "TMPDIR": tmp}, timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout

    def test_a_named_step_reaches_the_summary(self):
        out = self._run("wordpress-egress\nprovision-post-install")
        self.assertEqual(out.splitlines(), [
            "START SUMMARY: failed components: tor wordpress-egress provision-post-install"
            " — see the ERROR/WARNING lines above in " + re.search(r"in (\S+)$", out).group(1)])

    def test_only_names_of_the_bash_shape(self):
        out = self._run("ok-name\n$(touch x)\nTor\n\nwith space\n")
        self.assertIn("failed components: tor ok-name —", out)

    def test_nothing_failed(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        text = _read(MAC)
        script = (f'LOG_FILE="{tmp}/log"\nlog() {{ echo "$1"; }}\nSTART_ERRORS=""\n'
                  + _function(text, "note_start_errors") + _function(text, "log_start_summary")
                  + f': > "{tmp}/e"\nnote_start_errors "{tmp}/e"\nlog_start_summary\n')
        res = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True,
                             env={"PATH": "/usr/bin:/bin"}, timeout=30)
        self.assertEqual(res.stdout, "START SUMMARY: all components started\n")


class TestLinuxHasNoSummary(unittest.TestCase):
    def test_linux_passes_no_errors_file(self):
        text = _read(LINUX)
        self.assertNotIn("--errors-file", text)
        self.assertNotIn("log_start_summary", text)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""One rule for reading ~/.onionpress/config, and writes that are whole
(issue #404).

The launchers read config values with `grep | cut`, some `-f2` (a value
with `=` cut short), some `-f2-`, some `head -1`, one `tail -1`, while
`config.read_value` took the first line and `config.read_config` the last:
a duplicated key or a value holding `=` read differently per call site.
`config.write_value` truncated the file and wrote it again (a reader in
between saw it empty), and two writers could lose each other's change.

Now `config_value` (identical in linux/onionpress, app/MacOS/onionpress
and app/MacOS/launcher.sh) and config.read_value / read_config share one
rule: the first line naming the key wins, the value is everything after
its first `=`, blanks trimmed. No launcher reads the file with
`grep | cut` any more. Writes go to a temp file renamed over the config,
under an flock (`.config.lock`), keep the file's mode (0600 when new), and
leave one line per key they set.
"""

import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from onionpress import config  # noqa: E402

SCRIPTS = ("linux/onionpress", "app/MacOS/onionpress", "app/MacOS/launcher.sh")

CASES = {
    "plain": "VM_CPU=2\n",
    "with =": "CLOUDFLARE_TUNNEL_TOKEN=eyJh==\n",
    "duplicate": "VM_CPU=2\nVM_CPU=4\n",
    "blanks": "  VM_CPU =3\n  VM_CPU=  5  \n",
    "commented": "#VM_CPU=9\nVM_CPU=1\n",
    "empty value": "VM_CPU=\nVM_CPU=7\n",
    "missing": "OTHER=1\n",
    "prefix of another key": "VM_CPU_EXTRA=8\nVM_CPU=6\n",
    "no newline at end": "VM_CPU=12",
    "crlf": "VM_CPU=13\r\n",
}


def _read(rel):
    with open(os.path.join(PROJECT_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _function(text, name):
    m = re.search(r"^" + name + r"\(\) \{\n.*?^\}\n", text, re.S | re.M)
    if not m:
        raise AssertionError(f"no {name}()")
    return m.group(0)


def config_value_function():
    """The launchers' config_value, for harnesses that run launcher code."""
    return _function(_read(SCRIPTS[0]), "config_value")


class TestOneReader(unittest.TestCase):

    def test_the_function_is_the_same_in_every_script(self):
        bodies = {rel: _function(_read(rel), "config_value") for rel in SCRIPTS}
        self.assertEqual(len(set(bodies.values())), 1, "config_value drifted between launchers")

    def test_no_launcher_reads_the_config_with_grep_and_cut(self):
        for rel in SCRIPTS:
            text = _read(rel).replace(_function(_read(rel), "config_value"), "")
            for n, line in enumerate(text.split("\n"), 1):
                if line.lstrip().startswith("#"):
                    continue
                with self.subTest(script=rel, line=n):
                    self.assertNotRegex(line, r'"\$DATA_DIR/config".*\|\s*(cut|head|tail|awk|sed)\b')

    def _bash(self, key, content, default=None):
        with tempfile.TemporaryDirectory() as d:
            if content is not None:
                with open(os.path.join(d, "config"), "w", newline="") as f:
                    f.write(content)
            args = [key] + ([default] if default is not None else [])
            r = subprocess.run(
                ["/bin/bash", "-c", "set -e\n" + _function(_read(SCRIPTS[0]), "config_value")
                 + '\nv=$(config_value "$@")\nprintf "%s" "$v"', "bash", *args],
                env={"DATA_DIR": d, "PATH": "/usr/bin:/bin"}, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            return r.stdout

    def _python(self, key, content, default=""):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config")
            if content is not None:
                with open(path, "w", newline="") as f:
                    f.write(content)
            return config.read_value(path, key, default), config.read_config(path).get(key, default)

    def test_bash_and_python_agree(self):
        for name, content in CASES.items():
            with self.subTest(case=name):
                read_value, read_config = self._python("VM_CPU", content)
                self.assertEqual(read_value, read_config)
                self.assertEqual(self._bash("VM_CPU", content), read_value)
        self.assertEqual(self._python("CLOUDFLARE_TUNNEL_TOKEN", CASES["with ="])[0], "eyJh==")

    def test_the_rule(self):
        self.assertEqual(self._python("VM_CPU", CASES["duplicate"])[0], "2", "the first line wins")
        self.assertEqual(self._python("VM_CPU", CASES["blanks"])[0], "5")
        self.assertEqual(self._python("VM_CPU", CASES["empty value"])[0], "")

    def test_default_and_no_file(self):
        self.assertEqual(self._bash("VM_CPU", CASES["missing"], "dflt"), "dflt")
        self.assertEqual(self._bash("VM_CPU", None, "dflt"), "dflt")
        self.assertEqual(self._bash("VM_CPU", None), "")
        self.assertEqual(self._python("VM_CPU", None, "dflt")[0], "dflt")


class TestWholeWrites(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "config")

    def _text(self):
        with open(self.path) as f:
            return f.read()

    def test_a_write_leaves_one_line_per_key_where_the_first_was(self):
        with open(self.path, "w") as f:
            f.write("# c\nVM_CPU=2\nA=1\nVM_CPU=4\n")
        config.write_value(self.path, "VM_CPU", "8")
        self.assertEqual(self._text(), "# c\nVM_CPU=8\nA=1\n")
        config.write_config(self.path, {"B": "x=y", "A": "2"})
        self.assertEqual(self._text(), "# c\nVM_CPU=8\nA=2\nB=x=y\n")
        config.remove_value(self.path, "VM_CPU")
        self.assertEqual(self._text(), "# c\nA=2\nB=x=y\n")

    def test_a_file_without_a_final_newline(self):
        with open(self.path, "w") as f:
            f.write("A=1")
        config.write_value(self.path, "B", "2")
        self.assertEqual(self._text(), "A=1\nB=2\n")

    def test_modes(self):
        old = os.umask(0o022)
        try:
            config.write_value(self.path, "A", "1")
            self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
            os.chmod(self.path, 0o640)
            config.write_value(self.path, "A", "2")
            self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o640)
        finally:
            os.umask(old)

    def test_the_write_is_a_rename(self):
        config.write_value(self.path, "A", "1")
        before = os.stat(self.path).st_ino
        config.write_value(self.path, "A", "2")
        self.assertNotEqual(os.stat(self.path).st_ino, before, "rewritten in place")
        self.assertEqual(sorted(os.listdir(self._tmp.name)), [".config.lock", "config"])

    def test_concurrent_writers_lose_nothing(self):
        keys = [f"K{i}" for i in range(40)]

        def write(k):
            config.write_value(self.path, k, "v")
        threads = [threading.Thread(target=write, args=(k,)) for k in keys]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(set(config.read_config(self.path)), set(keys))

    def test_concurrent_change_notes_lose_nothing(self):
        d = self._tmp.name
        keys = [f"K{i}" for i in range(30)]
        threads = [threading.Thread(target=config.note_config_changed, args=(d, [k])) for k in keys]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(config.take_config_changes(d)), sorted(keys))
        self.assertEqual(config.take_config_changes(d), [])


if __name__ == "__main__":
    unittest.main()

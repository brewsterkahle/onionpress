#!/usr/bin/env python3
"""Every Python the launchers run is isolated from the current directory
(issue #470).

`python3 -m x` and `python3 -c '…'` put the current directory first on
sys.path (`-m` ahead of PYTHONPATH), and WordPress has My Creations
read-write: PHP there could write `socket.py` or `onionpress/__init__.py`
into the owner's publishing folder, and any `onionpress …` the owner ran
from a terminal in that folder executed it as the host user.

Linux: every `python3` in linux/onionpress runs with `-I` (no current or
script directory on sys.path, no PYTHON* environment, no user site; it
works on every python3 the .deb accepts, where `-P` and PYTHONSAFEPATH
need 3.11). `op_py` hands the package's path to the interpreter as an
argument (`OP_PY_BOOT`), since -I ignores PYTHONPATH, and keeps
PYTHONPATH in the environment for the Python the CLI starts itself.
Mac: `bundled_python` sets PYTHONSAFEPATH=1 (its Python is 3.14; -I
would ignore PYTHONHOME) and its one `-c` call says `-P` too.
"""

import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LINUX = "linux/onionpress"
MAC = "app/MacOS/onionpress"
MAC_SH = "app/MacOS/launcher.sh"
SRC = os.path.join(PROJECT_ROOT, "src")


def _read(rel):
    with open(os.path.join(PROJECT_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _code_lines(text):
    """(line number, line) for every line that is not a comment."""
    for n, line in enumerate(text.split("\n"), 1):
        if line.lstrip().startswith("#"):
            continue
        yield n, line


PYTHON_CALL = re.compile(r'(?<![\w/.-])python3?\b(?!["\'])')


class TestLinuxCensus(unittest.TestCase):

    def test_every_python3_is_isolated(self):
        """A `python3` that runs on the host carries -I as its first flag."""
        offenders = []
        for n, line in _code_lines(_read(LINUX)):
            if "docker exec" in line or "docker run" in line:
                continue  # the container's python, the container's cwd
            for m in PYTHON_CALL.finditer(line):
                rest = line[m.end():]
                if rest.startswith(" -I ") or rest.startswith(" -I\n"):
                    continue
                # A script run by path: sys.path[0] is the script's own
                # directory (/opt/onionpress/scripts, root's), not the cwd.
                if rest.startswith(' "$SCRIPTS_DIR/'):
                    continue
                # Not an invocation: `command -v python3`, a word in a
                # for-list of tool names, `python3>/dev/null`.
                if re.search(r'command -v python3|for tool in|python3\)|python3 is|python3\.', line):
                    continue
                offenders.append((n, line.strip()))
        self.assertEqual(offenders, [],
                         "python3 run without -I in linux/onionpress (issue #470)")

    def test_op_py_is_the_isolated_boot(self):
        text = _read(LINUX)
        boot = ("python3 -I -B -c 'import sys, runpy\n"
                'sys.path[:0] = sys.argv.pop(1).split(":")\n'
                'runpy.run_module("onionpress.cli", run_name="__main__", alter_sys=True)\''
                ' "$ONIONPRESS_PYTHONPATH"')
        # op_py, self-contained: the harnesses extract it alone. The start's
        # detached rebuild is the Python's own spawn since #442.7
        # (derivatives.isolated_cli_argv, tests/test_python_spawns_isolated.py).
        self.assertIn('    PYTHONPATH="$ONIONPRESS_PYTHONPATH" ' + boot + ' "$@"\n}\n', text)
        self.assertNotIn("OP_PY", text)
        self.assertNotIn("python3 -m onionpress", text)
        self.assertNotRegex(text, r'PYTHONPATH="\$ONIONPRESS_PYTHONPATH" python3 -c ')


class TestMacCensus(unittest.TestCase):

    def test_bundled_python_sets_safe_path(self):
        self.assertIn(
            'bundled_python() { PYTHONHOME="$_BUNDLED_PYTHONHOME" PYTHONSAFEPATH=1 '
            '"$_BUNDLED_PYTHON_BIN" "$@"; }', _read(MAC))

    def test_every_bundled_python_c_says_dash_p(self):
        """-m goes through the wrapper's PYTHONSAFEPATH; a -c says -P too."""
        for n, line in _code_lines(_read(MAC)):
            self.assertNotIn("bundled_python -c", line, f"{MAC}:{n}: -c without -P")
        self.assertIn("bundled_python -P -c", _read(MAC))

    def test_no_system_python_on_the_mac(self):
        """Modern Macs have no usable python3; nothing may call one."""
        for rel in (MAC, MAC_SH):
            for n, line in _code_lines(_read(rel)):
                if "docker exec" in line:
                    continue
                self.assertNotRegex(line, r'(?<![\w/_"])python3?\s', f"{rel}:{n}: {line.strip()}")


PLANTED = {
    "onionpress/__init__.py": 'raise SystemExit("PLANTED onionpress package ran")\n',
    "socket.py": 'raise SystemExit("PLANTED socket.py ran")\n',
    "runpy.py": 'raise SystemExit("PLANTED runpy.py ran")\n',
    "json.py": 'raise SystemExit("PLANTED json.py ran")\n',
}


def _op_py_definition():
    """ONIONPRESS_PYTHONPATH=, OP_PY_BOOT, OP_PY and op_py() from the launcher."""
    text = _read(LINUX)
    start = text.index('ONIONPRESS_PYTHONPATH="${ONIONPRESS_PYTHONPATH:-$INSTALL_DIR/lib}"')
    end = text.index("\n}\n", text.index("op_py() {", start)) + len("\n}\n")
    return text[start:end]


def _linux_port_probe():
    """The port probe's `python3 -I -c '…'` (its socket import)."""
    text = _read(LINUX)
    start = text.index("        if python3 -I -c '\nimport socket, sys")
    end = text.index("' \"$wp\"", start) + len("' \"$wp\"")
    return text[start:end].strip()


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class TestPlantedModulesAreNotImported(unittest.TestCase):
    """The launchers' Python, run from a folder holding planted modules."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="onionpress-pyisolated-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cwd = os.path.join(self.tmp, "My Creations")
        os.makedirs(os.path.join(self.cwd, "onionpress"))
        for rel, body in PLANTED.items():
            with open(os.path.join(self.cwd, rel), "w") as f:
                f.write(body)
        self.bin = os.path.join(self.tmp, "bin")
        os.mkdir(self.bin)
        os.symlink(sys.executable, os.path.join(self.bin, "python3"))

    def _bash(self, script, env=None):
        full_env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ.get("PATH", ""))
        full_env.pop("PYTHONPATH", None)
        full_env.pop("PYTHONSAFEPATH", None)
        full_env.update(env or {})
        return subprocess.run(["bash", "-c", script], cwd=self.cwd, env=full_env,
                              capture_output=True, text=True, timeout=120)

    def test_the_hole(self):
        """The old op_py ran the planted package; the old -c ran socket.py."""
        r = self._bash(f'PYTHONPATH={shlex.quote(SRC)} python3 -m onionpress.cli version')
        self.assertIn("PLANTED onionpress package ran", r.stderr)
        r = self._bash("python3 -c 'import socket'")
        self.assertIn("PLANTED socket.py ran", r.stderr)

    def test_op_py_runs_the_real_cli(self):
        script = (
            "INSTALL_DIR=/nonexistent\n"
            + _op_py_definition()
            + "op_py version\n"
        )
        r = self._bash(script, {"ONIONPRESS_PYTHONPATH": SRC})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("PLANTED", r.stderr + r.stdout)
        self.assertRegex(r.stdout, r"^OnionPress \d+\.\d+\.\d+")

    def test_op_py_keeps_pythonpath_for_the_clis_children(self):
        """-I ignores PYTHONPATH for op_py's own interpreter; the CLI's
        children (derivatives' `sys.executable -m onionpress.cli`) still
        find the package through the environment."""
        script = (
            "INSTALL_DIR=/nonexistent\n"
            + _op_py_definition()
            + "op_py_env() { PYTHONPATH=\"$ONIONPRESS_PYTHONPATH\" \"${OP_PY[@]}\" \"$@\"; }\n"
            # The boot's argv handling: argv[1] (the path) is consumed, the
            # rest reach the CLI as its arguments.
            "op_py version --json\n"
        )
        r = self._bash(script, {"ONIONPRESS_PYTHONPATH": SRC})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('"version"', r.stdout)

    def test_the_port_probe_imports_the_real_socket(self):
        script = "wp=1\n" + _linux_port_probe() + "; then echo free; else echo busy; fi\n"
        r = self._bash(script)
        self.assertNotIn("PLANTED", r.stderr + r.stdout)
        self.assertIn(r.stdout.strip(), ("free", "busy"), r.stderr)

    def test_the_mac_wrapper_drops_the_current_directory(self):
        """bundled_python as defined, with this interpreter as the bundle's
        (an empty PYTHONHOME is ignored by CPython)."""
        text = _read(MAC)
        line = next(l for l in text.split("\n") if l.startswith("bundled_python() {"))
        script = (
            f"_BUNDLED_PYTHON_BIN={shlex.quote(sys.executable)}\n"
            '_BUNDLED_PYTHONHOME=""\n'
            + line + "\n"
            + "bundled_python -P -c 'import socket, json, runpy; print(\"stdlib\")'\n"
            + "bundled_python -c 'import socket, json, runpy; print(\"stdlib again\")'\n"
        )
        r = self._bash(script)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "stdlib\nstdlib again\n")


if __name__ == "__main__":
    unittest.main()

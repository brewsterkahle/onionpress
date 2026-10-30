#!/usr/bin/env python3
"""Every Python the Python side starts is isolated from the current
directory (issue #491, #470's class).

`derivatives.spawn_regeneration` ran `sys.executable -m onionpress.cli`
after a restore: `-m` puts the caller's cwd first on sys.path, and the
owner may run `onionpress restore` from a terminal in My Creations, which
WordPress writes. `-I` is not inherited, and the Linux launcher leaves
PYTHONPATH in the environment for exactly this child.

Now every such spawn goes through `derivatives.isolated_cli_argv`: `-c`
with a boot that drops the cwd entry before importing anything that is not
built in and puts the package's own root first, which works on the .deb's
3.9 floor and keeps PYTHONHOME for the Mac's bundled Python. The detached
child also runs in the data dir, not the caller's folder.

The census reads src/ for `sys.executable` and `"-m"` argv lists; the
behavioural part runs the argv from a folder of planted modules. The
launchers' own spawns of the same command are held by
tests/test_launcher_python_isolated.py (OP_PY, PYTHONSAFEPATH).
"""

import ast
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(PROJECT_ROOT, "src")
sys.path.insert(0, SRC)

from onionpress import derivatives  # noqa: E402

# (file, enclosing function): sys.executable used, but nothing started.
NOT_A_SPAWN = {
    ("onionpress/derivatives.py", "isolated_cli_argv"): "the one isolated argv",
    ("onionpress/onionnames_client.py", "_candidate_wordlist_dirs"): "a path next to the interpreter",
    ("onionpress/daemon.py", "_log_version_info"): "logged",
}

PLANTED = {
    "onionpress/__init__.py": 'raise SystemExit("PLANTED onionpress package ran")\n',
    "runpy.py": 'raise SystemExit("PLANTED runpy.py ran")\n',
    "json.py": 'raise SystemExit("PLANTED json.py ran")\n',
    "argparse.py": 'raise SystemExit("PLANTED argparse.py ran")\n',
}


def _py_files():
    for root, _dirs, files in os.walk(SRC):
        for name in files:
            if name.endswith(".py"):
                path = os.path.join(root, name)
                yield os.path.relpath(path, SRC), path


def _enclosing(tree):
    """node -> name of the innermost function holding it."""
    owner = {}

    def walk(node, fn):
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
            owner[child] = name
            walk(child, name)
    walk(tree, None)
    return owner


def _is_python(node):
    """sys.executable, or a literal naming a python (`nsenter -m` is not one)."""
    if isinstance(node, ast.Attribute):
        return isinstance(node.value, ast.Name) and node.value.id == "sys" and node.attr == "executable"
    return isinstance(node, ast.Constant) and isinstance(node.value, str) \
        and os.path.basename(node.value).startswith("python")


class TestCensus(unittest.TestCase):

    def test_sys_executable_is_used_only_where_listed(self):
        found = set()
        for rel, path in _py_files():
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read(), path)
            owner = _enclosing(tree)
            for node in ast.walk(tree):
                if (isinstance(node, ast.Attribute) and node.attr == "executable"
                        and isinstance(node.value, ast.Name) and node.value.id == "sys"):
                    found.add((rel, owner.get(node)))
        unlisted = found - set(NOT_A_SPAWN)
        self.assertEqual(unlisted, set(), "a Python spawn that is not isolated_cli_argv (issue #491)")

    def test_no_dash_m_argv_in_src(self):
        offenders = []
        for rel, path in _py_files():
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read(), path)
            for node in ast.walk(tree):
                if isinstance(node, (ast.List, ast.Tuple)) and node.elts and _is_python(node.elts[0]) \
                        and any(isinstance(e, ast.Constant) and e.value == "-m" for e in node.elts):
                    offenders.append(f"{rel}:{node.lineno}")
        self.assertEqual(offenders, [], "`-m` puts the cwd first on sys.path")

    def test_the_dash_m_census_sees_the_old_spawn(self):
        old = ast.parse('[sys.executable, "-m", "onionpress.cli"]').body[0].value
        self.assertTrue(_is_python(old.elts[0]))
        nsenter = ast.parse('["nsenter", "-m"]').body[0].value
        self.assertFalse(_is_python(nsenter.elts[0]))

    def test_the_census_is_not_stale(self):
        """Every listed exception still exists, so the list cannot rot."""
        for rel, fn in NOT_A_SPAWN:
            with open(os.path.join(SRC, rel), encoding="utf-8") as f:
                self.assertIn(f"def {fn}(", f.read(), rel)


class TestSpawnRegeneration(unittest.TestCase):

    def test_spawns_the_isolated_argv_in_the_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = os.path.join(tmp, "data")
            os.makedirs(data)
            derivatives.schedule_regeneration(data)
            with mock.patch("onionpress.derivatives.subprocess.Popen") as popen:
                popen.return_value.pid = 1
                derivatives.spawn_regeneration(data)
        argv = popen.call_args.args[0]
        self.assertEqual(argv[:3], [sys.executable, "-c", derivatives._CLI_BOOT])
        self.assertNotIn("-m", argv)
        self.assertEqual(argv[-3:], ["--data-dir", data, "regenerate-derivatives"])
        self.assertEqual(popen.call_args.kwargs["cwd"], data)


class TestFromAPlantedFolder(unittest.TestCase):
    """The argv run from a folder of planted modules, the environment the
    Linux launcher leaves (PYTHONPATH set, no PYTHONSAFEPATH)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cwd = self._tmp.name
        for rel, body in PLANTED.items():
            path = os.path.join(self.cwd, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(body)
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("PYTHONSAFEPATH", "PYTHONSTARTUP")}
        self.env["PYTHONPATH"] = SRC

    def _run(self, argv):
        return subprocess.run(argv, cwd=self.cwd, env=self.env, capture_output=True,
                              text=True, timeout=60)

    def test_the_hole(self):
        r = self._run([sys.executable, "-m", "onionpress.cli", "--help"])
        self.assertIn("PLANTED", r.stderr)

    def test_the_isolated_argv_runs_the_real_cli(self):
        r = self._run(derivatives.isolated_cli_argv("--help"))
        self.assertNotIn("PLANTED", r.stderr + r.stdout)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("usage", r.stdout.lower())

    def test_without_pythonpath_too(self):
        del self.env["PYTHONPATH"]
        r = self._run(derivatives.isolated_cli_argv("--help"))
        self.assertNotIn("PLANTED", r.stderr + r.stdout)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_with_safe_path_set(self):
        """The Mac's environment: the cwd is already off, nothing else is."""
        self.env["PYTHONSAFEPATH"] = "1"
        r = self._run(derivatives.isolated_cli_argv("--help"))
        self.assertNotIn("PLANTED", r.stderr + r.stdout)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()

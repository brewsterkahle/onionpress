#!/usr/bin/env python3
"""Docker and wp-cli are run through one runner (issue #477).

docker.Docker (its run, exec, wp and compose) is the runner, and
wp_files.wp_cli / put_file / own / put_tree are the same commands for a
caller holding only a docker binary and environment; platform.docker_env
and find_docker_bin build those two. Every other docker argv a module
builds by hand is another place that chooses its own environment, binary,
user and timeout.

This census counts, in each Python module of the app, the argv lists that
start with a docker binary: the literal "docker", a name or attribute
called docker or *docker_bin*, or a call that finds the binary. The count
of each module is pinned in REVIEWED. A new hand-built docker argv fails
here: run it through Docker or wp_files instead. A module converted to
the runner fails too, until its number here is lowered, so the census
only ever shrinks.

No Docker: the sources are read with ast.
"""

import ast
import glob
import os
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The runners themselves.
RUNNERS = {
    "src/onionpress/docker.py",
    "src/onionpress/wp_files.py",
}

# Hand-built docker argv lists left in each module, as of issue #477.
REVIEWED = {
    "src/menubar.py": 2,
    "src/onionpress/analytics_sharing.py": 1,
    "src/onionpress/app_commands.py": 1,
    "src/onionpress/backup.py": 13,
    "src/onionpress/daemon.py": 3,
    "src/onionpress/db_import.py": 2,
    "src/onionpress/derivatives.py": 3,
    "src/onionpress/handle_action.py": 1,
    "src/onionpress/hub_backup.py": 6,
    "src/onionpress/key_manager.py": 5,
    "src/onionpress/launcher_ops.py": 3,
    "src/onionpress/multisite.py": 6,
    # Started by the browser, outside the app's package: it cannot import
    # the runner.
    "src/onionpress/native_messaging_host.py": 3,
    # read_served_address_with: the reader for a caller with only a binary
    # (issue #478).
    "src/onionpress/onion_hostname.py": 1,
    "src/onionpress/onionheaven.py": 3,
    "src/onionpress/onionnames_registrar.py": 1,
    "src/onionpress/ports.py": 1,
    "src/onionpress/scrub.py": 3,
    "src/onionpress/setup_logic.py": 1,
}

FINDERS = {"_docker_bin", "_find_docker", "find_docker_bin"}


def _is_docker_binary(node):
    if isinstance(node, ast.Constant):
        return node.value == "docker"
    if isinstance(node, ast.Name):
        return node.id == "docker" or "docker_bin" in node.id
    if isinstance(node, ast.Attribute):
        return node.attr == "docker" or "docker_bin" in node.attr
    if isinstance(node, ast.Call):
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        return name in FINDERS
    return False


def _sources():
    paths = sorted(glob.glob(os.path.join(PROJECT_ROOT, "src", "onionpress", "*.py")))
    paths.append(os.path.join(PROJECT_ROOT, "src", "menubar.py"))
    for path in paths:
        yield os.path.relpath(path, PROJECT_ROOT), path


def docker_argv_lines(path):
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    return sorted(node.lineno for node in ast.walk(tree)
                  if isinstance(node, ast.List) and node.elts
                  and _is_docker_binary(node.elts[0]))


class TestDockerRunnerCensus(unittest.TestCase):

    def test_no_new_hand_built_docker_argv(self):
        for rel, path in _sources():
            if rel in RUNNERS:
                continue
            lines = docker_argv_lines(path)
            with self.subTest(module=rel):
                expected = REVIEWED.get(rel, 0)
                self.assertLessEqual(
                    len(lines), expected,
                    f"{rel} builds a docker argv by hand at line(s) {lines}: "
                    "run it through docker.Docker or wp_files (issue #477)")
                self.assertEqual(
                    len(lines), expected,
                    f"{rel} has {len(lines)} hand-built docker argv(s) now: "
                    "lower its count in REVIEWED")

    def test_every_reviewed_module_exists(self):
        sources = {rel for rel, _ in _sources()}
        for rel in REVIEWED:
            with self.subTest(module=rel):
                self.assertIn(rel, sources)

    def test_the_census_sees_each_shape(self):
        tree = ast.parse(
            'a = ["docker", "ps"]\n'
            'b = [docker_bin, "ps"]\n'
            'c = [self._docker.docker_bin, "logs"]\n'
            'd = [_docker_bin(app)] + args\n'
            'e = ["dock", "ps"]\n'
            'f = [wp_files.WP_USER]\n')
        found = [n.lineno for n in ast.walk(tree)
                 if isinstance(n, ast.List) and n.elts and _is_docker_binary(n.elts[0])]
        self.assertEqual(sorted(found), [1, 2, 3, 4])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""docker.Docker is the one way Python runs docker (issue #477).

Every module that reaches a container takes an onionpress.docker.Docker
from its caller (the CLI's, the daemon's, the menubar's, built from
OnionPressPaths; a test's tests/fake_docker.FakeDocker) and runs through
its run, exec, wp(offline=…), compose, popen and run_to. The runner alone
knows the binary (platform.find_docker_bin) and the environment
(platform.docker_env, Colima's settings): a module that builds a
["docker", …] argv, finds the binary or threads a docker_bin / docker_env
of its own is another place that chooses its own daemon, user and
timeout, and the Linux unit's #503 socket rule or the daemon's
_setup_environment is then what makes it work.

This census reads every Python module of the app with ast and fails on:

- a "docker" string anywhere outside docker.py, unless (file, enclosing
  function) is in REVIEWED with its count: the strings that are a path
  or the binary's name, never a command;
- a parameter, name or attribute called docker_bin / docker_env (the
  old way of handing the binary and environment around), and a call to
  the binary finders, outside docker.py and platform.py.

The launchers' own docker calls (bash) are not this census's; the Linux
tray and heartbeat are the tests of their own. Modelled on
tests/test_container_capabilities.py.
"""

import ast
import glob
import os
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The runner: the only module that may name the docker binary.
RUNNER = "src/onionpress/docker.py"
# Finds the binary and builds the environment for the runner.
PLATFORM = "src/onionpress/platform.py"

# "docker" strings that are not a command, by (file, enclosing function):
# how many, and why each stays.
REVIEWED = {
    # The binary's name: in the bundle's bin/ and on PATH. The one finder,
    # which docker.Docker calls.
    (PLATFORM, "find_docker_bin"): 2,
    # The compose folder of the app bundle, Contents/Resources/docker.
    (PLATFORM, "resolve_paths"): 1,
    # The compose folder of a Linux install tree or a checkout
    # (<root>/docker, app/Resources/docker, $ONIONPRESS_INSTALL_DIR/docker).
    (PLATFORM, "linux_docker_dir"): 3,
    # The tor image's wordlists in a dev checkout:
    # app/Resources/docker/tor/wordlists.
    ("src/onionpress/onionnames_client.py", "_candidate_wordlist_dirs"): 1,
    # The first-time setup checks that the bundle ships its binaries
    # (docker, colima, limactl); it runs none of them.
    ("src/menubar.py", "_run_first_time_setup"): 1,
    # A cgroup path fragment: a process in Docker's cgroup is a
    # container's, not the host's cloudflared (issue #547).
    ("src/onionpress/host_cloudflared.py", "<module>"): 1,
}

# Identifiers that hand the binary or the environment around by hand.
HAND_THREADED = {"docker_bin", "_docker_bin", "docker_env", "_docker_env"}
FINDERS = {"find_docker_bin", "_find_docker"}


def _sources():
    paths = sorted(glob.glob(os.path.join(PROJECT_ROOT, "src", "onionpress", "*.py")))
    paths.append(os.path.join(PROJECT_ROOT, "src", "menubar.py"))
    for path in paths:
        yield os.path.relpath(path, PROJECT_ROOT), path


def _enclosing_functions(tree):
    """{node: name of the function it is in, else "<module>"}."""
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def function_of(node):
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node.name
        return "<module>"
    return function_of


def docker_strings(tree):
    """[(function, lineno)] of every "docker" string constant."""
    function_of = _enclosing_functions(tree)
    return sorted(((function_of(node), node.lineno) for node in ast.walk(tree)
                   if isinstance(node, ast.Constant) and node.value == "docker"),
                  key=lambda item: item[1])


def hand_threaded(tree):
    """[(what, lineno)] of every docker_bin / docker_env identifier and
    every call to a binary finder."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.arg) and node.arg in HAND_THREADED:
            found.append((f"parameter {node.arg}", node.lineno))
        elif isinstance(node, ast.Name) and node.id in HAND_THREADED:
            found.append((f"name {node.id}", node.lineno))
        elif isinstance(node, ast.Attribute) and node.attr in HAND_THREADED:
            found.append((f"attribute .{node.attr}", node.lineno))
        elif isinstance(node, ast.FunctionDef) and node.name in HAND_THREADED | FINDERS:
            found.append((f"def {node.name}", node.lineno))
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name in FINDERS:
                found.append((f"call {name}()", node.lineno))
    return sorted(found, key=lambda item: item[1])


def _parse(path):
    with open(path, encoding="utf-8") as f:
        return ast.parse(f.read())


class TestDockerRunnerCensus(unittest.TestCase):

    def test_no_docker_string_outside_the_runner(self):
        """A "docker" string is a command about to be run by hand, unless
        REVIEWED says what else it is."""
        seen = set()
        for rel, path in _sources():
            if rel == RUNNER:
                continue
            by_function = {}
            for function, lineno in docker_strings(_parse(path)):
                by_function.setdefault(function, []).append(lineno)
            for function, lines in by_function.items():
                seen.add((rel, function))
                with self.subTest(module=rel, function=function):
                    expected = REVIEWED.get((rel, function), 0)
                    self.assertLessEqual(
                        len(lines), expected,
                        f"{rel}:{function} names \"docker\" at line(s) {lines}: run it "
                        "through docker.Docker (issue #477), or, if it is a path or "
                        "the binary's name, list it in REVIEWED with why")
                    self.assertEqual(
                        len(lines), expected,
                        f"{rel}:{function} has {len(lines)} \"docker\" string(s) now: "
                        "lower its count in REVIEWED")
        for key in REVIEWED:
            with self.subTest(reviewed=key):
                self.assertIn(key, seen, f"{key} is in REVIEWED but names no \"docker\" "
                                         "string any more: remove it")

    def test_nothing_hands_the_binary_or_environment_around(self):
        """No docker_bin / docker_env parameter, variable or attribute, and
        no call to the binary finders, outside the runner and platform."""
        for rel, path in _sources():
            if rel in (RUNNER, PLATFORM):
                continue
            found = hand_threaded(_parse(path))
            with self.subTest(module=rel):
                self.assertEqual(
                    found, [],
                    f"{rel} hands docker's binary or environment around by hand: "
                    f"{found}. Take a Docker from the caller instead (issue #477)")

    def test_the_runner_and_platform_do_find_the_binary(self):
        """The census's exemptions are earned: the finder is platform's and
        the runner calls it and keeps the binary."""
        platform = [what for what, _ in hand_threaded(_parse(os.path.join(PROJECT_ROOT, PLATFORM)))]
        self.assertIn("def find_docker_bin", platform)
        runner = [what for what, _ in hand_threaded(_parse(os.path.join(PROJECT_ROOT, RUNNER)))]
        self.assertIn("call find_docker_bin()", runner)
        self.assertIn("attribute ._docker_bin", runner)

    def test_the_census_sees_each_shape(self):
        tree = ast.parse(
            'a = ["docker", "ps"]\n'
            'def f(docker_bin, env):\n'
            '    return [docker_bin, "ps"]\n'
            'c = [self._docker.docker_bin, "logs"]\n'
            'd = [_find_docker(app)] + args\n'
            'e = os.path.join(root, "docker")\n'
            'g = ["dock", "ps"]\n'
            'h = subprocess.run(find_docker_bin(bin_dir))\n')
        self.assertEqual(docker_strings(tree), [("<module>", 1), ("<module>", 6)])
        self.assertEqual(hand_threaded(tree), [
            ("parameter docker_bin", 2), ("name docker_bin", 3),
            ("attribute .docker_bin", 4), ("call _find_docker()", 5),
            ("call find_docker_bin()", 8)])



if __name__ == "__main__":
    unittest.main()

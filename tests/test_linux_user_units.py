#!/usr/bin/env python3
"""Linux services as systemd user units (issue #316).

- START_ON_BOOT enables/disables onionpress and onionpress-heartbeat as
  the user units both installers set up (system-scope systemctl never saw
  them).
- The settings watcher is a Type=oneshot unit; background work it starts
  (the derivative rebuild after a restore) goes to a transient user unit,
  not into the oneshot's cgroup, which systemd kills when it exits.

The shell is extracted from the launcher and run against stubs; nothing
here touches systemd or Docker.
"""

import os
import re
import subprocess
import tempfile
import time
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LAUNCHER = "linux/onionpress"


def _read(rel_path):
    with open(os.path.join(PROJECT_ROOT, rel_path), encoding="utf-8") as f:
        return f.read()


def _code_lines(text):
    return [l for l in text.splitlines() if not l.lstrip().startswith("#")]


def _bash_function(text, name):
    start = text.index(f"\n{name}() {{") + 1
    return text[start:text.index("\n}\n", start) + 3]


def _case_branch(text, label, next_label):
    start = text.index(f"\n        {label})\n")
    return text[start:text.index(f"\n        {next_label})\n", start)]


def _run_bash(script, env=None, **kw):
    full_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": tempfile.gettempdir()}
    full_env.update(env or {})
    return subprocess.run(["bash", "-c", script], env=full_env, capture_output=True,
                          text=True, timeout=60, stdin=subprocess.DEVNULL, **kw)


class TestStartOnBoot(unittest.TestCase):

    def setUp(self):
        self.launcher = _read(LAUNCHER)

    def test_start_on_boot_enables_the_user_units(self):
        body = _bash_function(self.launcher, "apply_config_updates")
        self.assertIn("systemctl --user enable onionpress.service onionpress-heartbeat.service", body)
        self.assertIn("systemctl --user disable onionpress.service onionpress-heartbeat.service", body)


class TestWatcherBackgroundWork(unittest.TestCase):

    def setUp(self):
        self.launcher = _read(LAUNCHER)
        self.fn = _bash_function(self.launcher, "run_detached")

    def test_handle_action_marks_the_watcher(self):
        handle = _case_branch(self.launcher, "handle-action", "reset")
        self.assertLess(handle.index("ONIONPRESS_IN_WATCHER=true"), handle.index("case \"$action\" in"))

    def test_derivative_rebuild_is_detached_through_it(self):
        body = _bash_function(self.launcher, "start_containers")
        self.assertRegex(body, r"\n\s*run_detached env PYTHONPATH=\"\$ONIONPRESS_PYTHONPATH\" "
                               r"python3 -m onionpress\.cli regenerate-derivatives\n")
        self.assertNotRegex(body, r"\(\s*op_py regenerate-derivatives")

    def _run(self, in_watcher):
        tmp = tempfile.mkdtemp()
        self.addCleanup(subprocess.run, ["rm", "-rf", tmp])
        bin_dir = os.path.join(tmp, "bin")
        os.makedirs(bin_dir)
        with open(os.path.join(bin_dir, "systemd-run"), "w") as f:
            f.write('#!/bin/sh\nfor a in "$@"; do echo "$a"; done > "$OUT/systemd-run"\n')
        os.chmod(os.path.join(bin_dir, "systemd-run"), 0o755)
        script = (self.fn + 'log() { :; }\n'
                  'ONIONPRESS_IN_WATCHER="$IN_WATCHER"\n'
                  'run_detached sh -c \'echo ran > "$OUT/ran"\'\n')
        r = _run_bash(script, env={
            "PATH": bin_dir + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
            "OUT": tmp, "IN_WATCHER": in_watcher, "LOG_FILE": os.path.join(tmp, "log"),
            "DOCKER_HOST": "unix:///run/user/1000/docker.sock",
            "MYSQL_ROOT_PASSWORD": "s3cret-not-for-argv",
        })
        self.assertEqual(r.returncode, 0, r.stderr)
        return tmp

    def test_from_the_watcher_it_goes_to_a_transient_user_unit(self):
        tmp = self._run("true")
        with open(os.path.join(tmp, "systemd-run")) as f:
            argv = f.read().splitlines()
        self.assertIn("--user", argv)
        self.assertIn("--collect", argv)
        self.assertIn("--setenv=DOCKER_HOST=unix:///run/user/1000/docker.sock", argv)
        self.assertEqual(argv[argv.index("--") + 1:], ["sh", "-c", 'echo ran > "$OUT/ran"'])
        self.assertFalse(any("s3cret" in a for a in argv))
        self.assertFalse(os.path.exists(os.path.join(tmp, "ran")))

    def test_elsewhere_it_runs_in_a_detached_subshell(self):
        tmp = self._run("")
        self.assertFalse(os.path.exists(os.path.join(tmp, "systemd-run")))
        for _ in range(100):
            if os.path.exists(os.path.join(tmp, "ran")):
                break
            time.sleep(0.05)
        self.assertTrue(os.path.exists(os.path.join(tmp, "ran")))


if __name__ == "__main__":
    unittest.main()

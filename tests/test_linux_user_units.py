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
        """config.apply_side_effects' Linux branch (run by `config set` and
        by handle_action.apply_config for the page's channel); the launcher
        has no copy of its own since 2026-10-31."""
        self.assertNotIn("apply_config_updates", self.launcher)
        self.assertNotIn("systemctl --user enable onionpress.service", self.launcher)
        config = _read("src/onionpress/config.py")
        body = config[config.index("def apply_side_effects("):]
        body = body[:body.index("\ndef ", 1)]
        self.assertIn('if "START_ON_BOOT" in keys and os_name == "linux":', body)
        self.assertIn('run(["systemctl", "--user", verb, "onionpress.service",\n'
                      '                     "onionpress-heartbeat.service"]', body)


class TestWatcherBackgroundWork(unittest.TestCase):

    def setUp(self):
        self.launcher = _read(LAUNCHER)

    def test_handle_action_marks_the_watcher(self):
        # The shared handler (onionpress.handle_action) runs the actions
        # through this launcher; the mark is inherited by those runs.
        handle = _case_branch(self.launcher, "handle-action", "settings-page-backup")
        self.assertRegex(handle, r'\n            ONIONPRESS_IN_WATCHER=true op_py --data-dir "\$DATA_DIR" '
                                 r'handle-action --launcher "\$INSTALL_DIR/onionpress" "\$\{@:2\}"\n')

    # The detached rebuild (a transient user unit from the watcher, a
    # session of its own elsewhere) is start.run_detached_cli since #442.7:
    # tests/test_start_post_up.py, TestDetached.


if __name__ == "__main__":
    unittest.main()

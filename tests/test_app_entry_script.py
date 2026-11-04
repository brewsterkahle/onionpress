#!/usr/bin/env python3
"""The app bundle's entry script starts the MenubarApp and the launcher's
VM start, nothing else (issue #545).

`app/MacOS/launcher.sh` is what OnionPress.app runs (launcher-wrapper.swift
execs it). It used to start Colima itself, beside `app/MacOS/onionpress`'s
own VM start, and the two had drifted: other default sizes, no VM
recovery, no fsck repair drop-in. It also carried the expired
~/Documents/OnionPress move and a third copy of `config_value`. Now it
starts the MenubarApp (gray icon), runs `onionpress start-vm` (the
launcher's `start_vm`, the one place Colima is started) and waits for the
MenubarApp, forwarding a quit to it.

The script runs here under /bin/bash in a scratch bundle: a stub
`onionpress` and MenubarApp record that they ran, and stub `sysctl`,
`uname`, `arch` and `pgrep` stand in for the Mac's.
"""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPT = os.path.join(PROJECT_ROOT, "app", "MacOS", "launcher.sh")
BASH = "/bin/bash"

STUBS = {
    # Apple Silicon, running natively: no Rosetta re-exec.
    "sysctl": 'echo "hw.optional.arm64: 1"\n',
    "uname": "echo arm64\n",
    "arch": 'shift\nexec "$@"\n',
    # No MenubarApp of this bundle is running.
    "pgrep": "exit 1\n",
}


def _executable(path, body):
    with open(path, "w", encoding="utf-8") as f:
        f.write("#!/bin/sh\n" + body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@unittest.skipUnless(os.path.exists(BASH), "needs /bin/bash")
class TestEntryScript(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="op-entry-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        macos = os.path.join(self.tmp, "OnionPress.app", "Contents", "MacOS")
        menubar = os.path.join(self.tmp, "OnionPress.app", "Contents", "Resources",
                               "MenubarApp", "Contents", "MacOS")
        bin_dir = os.path.join(self.tmp, "bin")
        for d in (macos, menubar, bin_dir):
            os.makedirs(d)
        self.script = os.path.join(macos, "launcher.sh")
        shutil.copy(SCRIPT, self.script)
        self.calls = os.path.join(self.tmp, "calls")
        open(self.calls, "w").close()
        _executable(os.path.join(macos, "onionpress"),
                    'echo "onionpress $*" >> "$OP_CALLS"\nexit "${STUB_RC_START_VM:-0}"\n')
        _executable(os.path.join(menubar, "OnionPress"),
                    'echo "menubar $*" >> "$OP_CALLS"\n')
        for name, body in STUBS.items():
            _executable(os.path.join(bin_dir, name), body)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.env = {"HOME": self.home, "OP_CALLS": self.calls,
                    "PATH": bin_dir + os.pathsep + "/usr/bin:/bin:/usr/sbin:/sbin"}

    def run_script(self, **env):
        r = subprocess.run([BASH, self.script], env={**self.env, **env},
                           capture_output=True, text=True, timeout=60)
        with open(self.calls, encoding="utf-8") as f:
            calls = f.read().splitlines()
        with open(os.path.join(self.home, ".onionpress", "launcher.log"), encoding="utf-8") as f:
            log = f.read()
        return r, calls, log

    def test_it_starts_the_menubar_then_the_launchers_vm_start(self):
        r, calls, log = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted(calls), ["menubar ", "onionpress start-vm"])
        self.assertLess(log.index("Menu bar app launched"), log.index("Setup complete"))
        self.assertIn("MenubarApp exited (rc=0), launcher done", log)
        self.assertEqual(os.stat(os.path.join(self.home, ".onionpress")).st_mode & 0o777, 0o700)

    def test_a_failed_vm_start_is_logged_and_the_menubar_keeps_running(self):
        r, calls, log = self.run_script(STUB_RC_START_VM="3")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("menubar ", calls)
        self.assertIn("ERROR: onionpress start-vm failed (rc=3)", log)
        self.assertNotIn("Setup complete", log)
        self.assertIn("MenubarApp exited (rc=0), launcher done", log)

    def test_the_documents_folder_move_is_gone(self):
        # The ~/Documents/OnionPress -> ~/OnionPress move expired on
        # 2026-08-01 (#239, #522): the script neither moves nor creates.
        old = os.path.join(self.home, "Documents", "OnionPress")
        os.makedirs(old)
        with open(os.path.join(old, "post.txt"), "w") as f:
            f.write("mine")
        r, _, _ = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(os.listdir(old), ["post.txt"])
        self.assertFalse(os.path.exists(os.path.join(self.home, "OnionPress")))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".onionpress", "config")))
        with open(SCRIPT, encoding="utf-8") as f:
            text = f.read()
        for word in ("_op_migrate_tree", "OLD_DOCUMENTS_DIR", "PATH_MIGRATION_2026_05"):
            self.assertNotIn(word, text)

    def test_it_keeps_the_rosetta_re_exec_and_the_quit_trap(self):
        with open(SCRIPT, encoding="utf-8") as f:
            text = f.read()
        self.assertIn('exec arch -arm64 "$0" "$@"', text)
        self.assertIn("trap 'kill -TERM $MENUBAR_PID 2>/dev/null; wait $MENUBAR_PID 2>/dev/null' "
                      "TERM INT HUP", text)


if __name__ == "__main__":
    unittest.main()

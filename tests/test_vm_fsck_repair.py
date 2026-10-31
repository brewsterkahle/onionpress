#!/usr/bin/env python3
"""The Colima guest's root filesystem must be able to repair itself at boot.

Root cause of the "Lima reports the VM Running, sshd and Docker never come
up" wedge (2026-10-11, live install): the guest's root ext4 had carried an
inconsistency for a month that the boot-time preen check cannot fix, so
systemd-fsck-root.service failed on every boot and its OnFailure= started
emergency.target. Whether sysinit, ssh and docker still came up was a race
that half the boots lost. The macOS launcher now gives the guest kernel
``fsck.repair=yes`` through a grub drop-in (systemd-fsck then repairs
instead of asking for a human), warns in onionpress.log when the current
boot's check failed, and keeps the journal of a boot that wedged after
recover_lima_vm has restarted the VM.

The bash harness runs the launcher's own functions against a stub
``limactl`` so the logic is exercised, not just read.
"""

import os
import subprocess
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER = "app/MacOS/onionpress"
DROP_IN = "/etc/default/grub.d/60-onionpress-fsck.cfg"


def _read(rel_path):
    with open(os.path.join(PROJECT_ROOT, rel_path), "r", encoding="utf-8") as f:
        return f.read()


def _function_body(text, name):
    start = text.index(f"\n{name}() {{")
    end = text.index("\n}\n", start)
    return text[start:end]


WEDGED_JOURNAL = """\
[    0.366962] colima systemd-fsck[126]: fsck failed with exit status 4.
[    0.367675] colima systemd[1]: systemd-fsck-root.service: Failed with result 'exit-code'.
[    0.383422] colima systemd[1]: Started emergency.service - Emergency Shell.
[    3.359852] colima systemd[1]: Startup finished in 106ms (kernel) + 3.253s (userspace) = 3.359s.
"""

HEALTHY_JOURNAL = """\
[    0.264708] colima systemd-fsck[136]: e2fsck 1.47.0 (5-Feb-2023)
[    0.286484] colima systemd-fsck[136]: cloudimg-rootfs: clean, 125773/12976128 files, 2300446/25951995 blocks
[    2.735555] colima systemd[1]: Reached target sysinit.target - System Initialization.
[    2.741772] colima systemd[1]: Listening on ssh.socket - OpenBSD Secure Shell server socket.
"""


class TestWiring(unittest.TestCase):
    """Static checks: the pieces are present and called from the right places."""

    def setUp(self):
        self.text = _read(LAUNCHER)

    def test_drop_in_sets_the_repair_flag_and_regenerates_grub(self):
        body = _function_body(self.text, "ensure_vm_fsck_repair")
        self.assertIn(DROP_IN, body)
        self.assertIn('fsck.repair=yes"', body)
        self.assertIn("update-grub", body)
        # Idempotent: the drop-in is only written when the flag is missing.
        self.assertIn('if ! grep -qs "fsck.repair=yes" "$f"; then', body)
        self.assertIn("systemctl is-failed systemd-fsck-root.service", body)

    def test_guest_script_is_valid_sh(self):
        body = _function_body(self.text, "ensure_vm_fsck_repair")
        start = body.index("<<'EOF'\n") + len("<<'EOF'\n")
        script = body[start:body.index("\nEOF\n", start)]
        subprocess.run(["sh", "-n"], input=script, text=True, check=True)
        # The drop-in line must reach the guest with a literal "$": the
        # outer heredoc is quoted, so only the sh -c layer unescapes it.
        self.assertIn('GRUB_CMDLINE_LINUX_DEFAULT=\\"\\$GRUB_CMDLINE_LINUX_DEFAULT fsck.repair=yes\\"', script)

    def test_start_looks_after_the_root_fs_once_docker_answers(self):
        body = _function_body(self.text, "start_containers")
        wait = body.index("if ! wait_for_docker_or_recover_vm 180 start; then")
        call = body.index("\n    ensure_vm_fsck_repair\n")
        self.assertLess(wait, call)
        # ...and before any volume question (those need Docker too).
        self.assertLess(call, body.index("start-containers"))

    def test_recovery_keeps_the_wedged_boots_journal(self):
        body = _function_body(self.text, "recover_lima_vm")
        restarted = body.index('log "$who: Lima VM restarted"')
        self.assertLess(restarted, body.index('save_previous_boot_journal "$who"'))
        journal = _function_body(self.text, "save_previous_boot_journal")
        self.assertIn("journalctl -b -1", journal)
        # Named like the other rotated logs: <type>-YYYY-MM-DD-NNN.log
        self.assertIn('vm-wedged-boot-$day-$(printf \'%03d\' "$n").log', journal)

    def test_linux_launcher_is_untouched(self):
        # Linux runs Docker natively: no VM, no grub, nothing to repair.
        linux = _read("linux/onionpress")
        for name in ("vm_shell_bounded", "ensure_vm_fsck_repair",
                     "save_previous_boot_journal"):
            self.assertNotIn(f"\n{name}() {{", linux)


class TestHarness(unittest.TestCase):
    """Runs the functions under bash with a stub limactl whose `shell`
    prints $STUB_OUTPUT (or hangs when STUB_HANG is set)."""

    FUNCTIONS = ("vm_shell_bounded", "ensure_vm_fsck_repair",
                 "save_previous_boot_journal")
    LIMACTL_STUB = """#!/bin/sh
printf 'limactl %s\\n' "$*" >> "$STUB_CALLS"
if [ "$1" = shell ]; then
    [ -n "$STUB_HANG" ] && exec /bin/sleep 30
    printf '%s' "$STUB_OUTPUT"
fi
exit 0
"""

    def _run(self, snippet, *, output="", hang=False, limactl=True, data_dir=True):
        text = _read(LAUNCHER)
        functions = "".join(_function_body(text, name) + "\n}\n"
                            for name in self.FUNCTIONS)
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = os.path.join(tmp, "bin")
            os.mkdir(bin_dir)
            if limactl:
                path = os.path.join(bin_dir, "limactl")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(self.LIMACTL_STUB)
                os.chmod(path, 0o755)
            calls = os.path.join(tmp, "calls")
            log = os.path.join(tmp, "log")
            for name in (calls, log):
                open(name, "w").close()
            script = (
                "set -e\n"
                f'LOG_FILE="{log}"; BIN_DIR="{bin_dir}"\n'
                + (f'DATA_DIR="{tmp}/data"\n' if data_dir else "")
                + 'log() { printf "%s\\n" "$*" >> "$LOG_FILE"; }\n'
                # The 60 s bound becomes 3 s: short enough for the hang test,
                # long enough for a stub to fork before the bound expires (a
                # no-op sleep, as the other harnesses use, kills it first).
                "sleep() { command sleep 0.05; }\n"
                + functions + snippet + "\n")
            env = {"PATH": bin_dir + os.pathsep + "/usr/bin:/bin",
                   "STUB_CALLS": calls, "STUB_OUTPUT": output,
                   "STUB_HANG": "1" if hang else ""}
            out = subprocess.run(["bash", "-c", script], env=env,
                                 capture_output=True, text=True, timeout=60)
            with open(calls, encoding="utf-8") as f:
                made = f.read().splitlines()
            with open(log, encoding="utf-8") as f:
                logged = f.read()
            saved = {}
            logs_dir = os.path.join(tmp, "data", "logs")
            if os.path.isdir(logs_dir):
                for name in sorted(os.listdir(logs_dir)):
                    with open(os.path.join(logs_dir, name), encoding="utf-8") as f:
                        saved[name] = f.read()
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip(), made, logged, saved

    ENSURE = "rc=0; ensure_vm_fsck_repair || rc=$?; echo rc=$rc"

    def test_installs_the_flag_and_warns_when_this_boot_failed_its_check(self):
        out, calls, logged, _ = self._run(
            self.ENSURE,
            output="fsck-root=failed repair-flag=installed cmdline=no\n"
                   "cloudimg-rootfs: UNEXPECTED INCONSISTENCY; RUN fsck MANUALLY.\n")
        self.assertEqual(out, "rc=0")
        # The stub logs "$*" and the guest script spans lines: count
        # invocations, not lines.
        self.assertEqual(sum(c.startswith("limactl ") for c in calls), 1, calls)
        self.assertTrue(calls[0].startswith("limactl shell colima -- sudo sh -c "), calls)
        script = "\n".join(calls)
        self.assertIn(DROP_IN, script)
        self.assertIn("update-grub", script)
        self.assertIn("added fsck.repair=yes to the guest kernel command line", logged)
        self.assertIn("WARNING: VM: the root filesystem check failed on this boot", logged)
        self.assertIn("RUN fsck MANUALLY", logged)
        self.assertIn("the next VM boot will repair it", logged)
        self.assertNotIn("needs manual attention", logged)

    def test_quiet_when_the_flag_is_present_and_the_check_passed(self):
        out, calls, logged, _ = self._run(
            self.ENSURE, output="fsck-root=active repair-flag=present cmdline=yes\n")
        self.assertEqual(out, "rc=0")
        self.assertEqual(sum(c.startswith("limactl ") for c in calls), 1, calls)
        self.assertEqual(logged, "")

    def test_escalates_when_the_repair_flag_was_active_and_fsck_still_failed(self):
        _, _, logged, _ = self._run(
            self.ENSURE, output="fsck-root=failed repair-flag=present cmdline=yes\n")
        self.assertIn("needs manual attention", logged)
        self.assertNotIn("the next VM boot will repair it", logged)

    def test_never_blocks_the_start(self):
        # A wedged sshd makes limactl shell hang: bounded, logged, rc 0.
        out, _, logged, _ = self._run(self.ENSURE, hang=True)
        self.assertEqual(out, "rc=0")
        self.assertIn("did not return within 60s", logged)
        # No bundled limactl (Linux, or a harness without one): nothing to do.
        out, calls, logged, _ = self._run(self.ENSURE, limactl=False)
        self.assertEqual((out, calls, logged), ("rc=0", [], ""))

    JOURNAL = "rc=0; save_previous_boot_journal start || rc=$?; echo rc=$rc"

    def test_saves_the_wedged_boots_journal_and_says_what_it_did(self):
        out, calls, logged, saved = self._run(self.JOURNAL, output=WEDGED_JOURNAL)
        self.assertEqual(out, "rc=0")
        self.assertEqual(calls, ["limactl shell colima -- sudo journalctl -b -1 "
                                 "--no-pager -o short-monotonic"])
        self.assertEqual(len(saved), 1)
        (name, body), = saved.items()
        self.assertRegex(name, r"^vm-wedged-boot-\d{4}-\d{2}-\d{2}-001\.log$")
        self.assertEqual(body, WEDGED_JOURNAL)
        self.assertIn("start: previous boot: fsck-root=failed emergency=yes "
                      "sysinit=no ssh=no — journal saved to ", logged)

    def test_a_healthy_previous_boot_reads_as_such_and_files_do_not_collide(self):
        _, _, logged, saved = self._run(
            self.JOURNAL + "\n" + self.JOURNAL, output=HEALTHY_JOURNAL)
        self.assertEqual(len(saved), 2, sorted(saved))
        self.assertTrue(any(n.endswith("-001.log") for n in saved))
        self.assertTrue(any(n.endswith("-002.log") for n in saved))
        self.assertIn("fsck-root=ok emergency=no sysinit=yes ssh=yes", logged)

    def test_an_unreadable_journal_leaves_no_file_behind(self):
        out, _, logged, saved = self._run(self.JOURNAL, output="")
        self.assertEqual(out, "rc=0")
        self.assertEqual(saved, {})
        self.assertIn("could not read the previous boot's journal", logged)
        # No DATA_DIR (the recovery harness in test_install_invariants):
        # skip silently rather than write next to /.
        out, calls, logged, _ = self._run(self.JOURNAL, output=WEDGED_JOURNAL,
                                          data_dir=False)
        self.assertEqual((out, calls, logged), ("rc=0", [], ""))


if __name__ == "__main__":
    unittest.main()

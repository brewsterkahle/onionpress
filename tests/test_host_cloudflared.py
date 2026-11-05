#!/usr/bin/env python3
"""A host cloudflared beside OnionPress's own (issue #547): the check is
the daemon's on both platforms (onionpress.host_cloudflared, the state's
`host_cloudflared`), the menubar and the tray show what the state says
(ui_client.host_cloudflared_notice). Until then the menubar ran its own
`pgrep` at launch and Linux had no check.

The app bundle's sharing with the admin group moved the same way: the
updater does it on the staged bundle before the swap, the daemon once per
start (updater.share_bundle_with_admins)."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
sys.path.insert(0, HERE)

from onionpress import host_cloudflared, ui_client, updater  # noqa: E402
from onionpress.platform import OS  # noqa: E402

from test_mac_daemon import _DaemonCase  # noqa: E402


def _done(rc, out=""):
    return subprocess.CompletedProcess([], rc, out, "")


class TestMac(unittest.TestCase):

    def test_pgrep_for_a_root_tunnel(self):
        run = mock.Mock(return_value=_done(0, "123\n"))
        self.assertIs(host_cloudflared.running(OS.MACOS, run=run), True)
        self.assertEqual(run.call_args[0][0],
                         ["pgrep", "-u", "root", "-f", "cloudflared.*tunnel.*run"])

    def test_none_found_and_a_failed_pgrep(self):
        self.assertIs(host_cloudflared.running(OS.MACOS, run=mock.Mock(return_value=_done(1))), False)
        # An error is not an answer.
        self.assertIsNone(host_cloudflared.running(OS.MACOS, run=mock.Mock(return_value=_done(3))))
        self.assertIsNone(host_cloudflared.running(
            OS.MACOS, run=mock.Mock(side_effect=OSError("no pgrep"))))


class TestLinux(unittest.TestCase):

    def setUp(self):
        self.proc = tempfile.mkdtemp(prefix="op-proc-")
        self.addCleanup(shutil.rmtree, self.proc, True)

    def add(self, pid, argv, uid=0, cgroup="0::/system.slice/cloudflared.service\n"):
        d = os.path.join(self.proc, str(pid))
        os.makedirs(d)
        with open(os.path.join(d, "cmdline"), "wb") as f:
            f.write(b"\0".join(a.encode() for a in argv) + b"\0")
        with open(os.path.join(d, "status"), "w") as f:
            f.write(f"Name:\tx\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
        with open(os.path.join(d, "cgroup"), "w") as f:
            f.write(cgroup)

    def running(self):
        return host_cloudflared.running(OS.LINUX, proc=self.proc)

    def test_a_root_service_on_the_host(self):
        self.add(10, ["/usr/bin/cloudflared", "--no-autoupdate", "tunnel", "run", "--token", "x"])
        self.assertIs(self.running(), True)

    def test_onionpress_own_container_is_not_the_host(self):
        # System Docker: the stack's cloudflared is a root process on the
        # host, in a container's cgroup.
        self.add(10, ["cloudflared", "tunnel", "--no-autoupdate", "run"],
                 cgroup="0::/system.slice/docker-0123abcd.scope\n")
        self.add(11, ["cloudflared", "tunnel", "run"],
                 cgroup="12:pids:/docker/0123abcd\n")
        self.assertIs(self.running(), False)

    def test_another_users_or_another_command(self):
        self.add(10, ["cloudflared", "tunnel", "run"], uid=1000)
        self.add(11, ["cloudflared", "access", "tcp"])
        self.add(12, ["vim", "notes"])
        self.assertIs(self.running(), False)

    def test_no_proc_is_no_answer(self):
        self.assertIsNone(host_cloudflared.running(OS.LINUX, proc=os.path.join(self.proc, "gone")))


class TestTheNotice(unittest.TestCase):

    def test_only_a_true_answer_is_shown(self):
        for state in (None, {}, {"host_cloudflared": None}, {"host_cloudflared": False},
                      {"host_cloudflared": "yes"}):
            with self.subTest(state=state):
                self.assertIsNone(ui_client.host_cloudflared_notice(state, "macos"))

    def test_each_platforms_fix(self):
        mac = ui_client.host_cloudflared_notice({"host_cloudflared": True}, "macos")
        linux = ui_client.host_cloudflared_notice({"host_cloudflared": True}, "linux")
        self.assertEqual(mac.title, linux.title)
        self.assertIn("brew uninstall cloudflared", mac.body)
        self.assertIn("this Mac", mac.body)
        self.assertNotIn("brew", linux.body)
        self.assertIn("systemctl disable --now cloudflared", linux.body)
        for n in (mac, linux):
            self.assertIn("502", n.body)


class TestTheDaemonAsks(_DaemonCase):

    def tunnel_on(self):
        with open(os.path.join(self.dir, "config"), "a") as f:
            f.write("CLOUDFLARE_TUNNEL_TOKEN=tok\n")
        self.d.cloudflare_tunnel_enabled = True

    def test_no_tunnel_no_question(self):
        with mock.patch.object(host_cloudflared, "running") as running:
            self.tick()
        running.assert_not_called()
        self.assertIsNone(self.state()["host_cloudflared"])

    def test_the_answer_lands_in_the_state_every_interval(self):
        self.tunnel_on()
        with mock.patch.object(host_cloudflared, "running", return_value=True) as running:
            self.tick()
            self.tick()
        self.assertEqual(running.call_count, 1, "asked once per HOST_CLOUDFLARED_INTERVAL")
        running.assert_called_with(self.OS_NAME)
        self.assertIs(self.state()["host_cloudflared"], True)
        with mock.patch.object(host_cloudflared, "running", return_value=None):
            self.d._check_host_cloudflared(now=self.d._host_cloudflared_checked_at + 301)
        self.assertIs(self.d.host_cloudflared, True, "no answer keeps the last one")
        with mock.patch.object(host_cloudflared, "running", return_value=False):
            self.d._check_host_cloudflared(now=self.d._host_cloudflared_checked_at + 301)
        self.assertIs(self.d.host_cloudflared, False)

    def test_the_tunnel_turned_off_clears_it(self):
        self.tunnel_on()
        with mock.patch.object(host_cloudflared, "running", return_value=True):
            self.tick()
        self.d.cloudflare_tunnel_enabled = False
        self.d._check_host_cloudflared()
        self.assertIsNone(self.d.host_cloudflared)


class TestTheDaemonOnLinux(TestTheDaemonAsks):
    OS_NAME = OS.LINUX


class TestShareBundle(unittest.TestCase):

    def _stat(self, mode=0o40755, uid=None):
        return mock.Mock(st_mode=mode, st_uid=os.getuid() if uid is None else uid)

    def share(self, path="/Applications/OnionPress.app", st=None, returncode=0,
              stat_error=None, admin=True):
        """(changed, argvs run, lines logged). The admin group is stubbed,
        so this runs on Linux too (the coverage job's platform)."""
        run = mock.Mock(return_value=_done(returncode))
        logged = []
        group = (mock.patch("grp.getgrnam", return_value=mock.Mock()) if admin
                 else mock.patch("grp.getgrnam", side_effect=KeyError("admin")))
        with mock.patch.object(updater.os.path, "realpath", side_effect=lambda p: p), \
                mock.patch.object(updater.os, "stat", return_value=st or self._stat(),
                                  side_effect=stat_error), group:
            changed = updater.share_bundle_with_admins(path, log=logged.append, run=run)
        return changed, [c[0][0] for c in run.call_args_list], logged

    def test_an_owned_bundle_in_applications(self):
        changed, calls, logged = self.share()
        self.assertTrue(changed)
        self.assertEqual(calls, [["chmod", "-R", "g+w", "/Applications/OnionPress.app"],
                                 ["chgrp", "-R", "admin", "/Applications/OnionPress.app"]])
        self.assertEqual(logged[-1], "Multi-user: app bundle permissions updated")
        # The staged copy beside it, before the swap.
        changed, _, _ = self.share("/Applications/.onionpress-staging-x/OnionPress.app")
        self.assertTrue(changed)

    def test_left_alone(self):
        for path, st in (("/Users/me/OnionPress.app", None),
                         ("/Applications/OnionPress.app", self._stat(mode=0o40775)),
                         ("/Applications/OnionPress.app", self._stat(uid=os.getuid() + 1))):
            with self.subTest(path=path, st=st):
                self.assertEqual(self.share(path, st), (False, [], []))

    def test_no_admin_group_changes_nothing(self):
        self.assertEqual(self.share(admin=False), (False, [], []))

    def test_a_failed_chmod_says_so(self):
        changed, calls, logged = self.share(returncode=1)
        self.assertFalse(changed)
        self.assertEqual(len(calls), 2)
        self.assertEqual(logged[-1], "Multi-user: could not update every file's permissions")

    def test_an_unreadable_bundle_says_so(self):
        changed, calls, logged = self.share(stat_error=OSError("gone"))
        self.assertFalse(changed)
        self.assertEqual(calls, [])
        self.assertEqual(logged, ["Multi-user: could not fix app bundle permissions: gone"])

    @unittest.skipUnless(sys.platform == "darwin", "stages with a Mac's xattr")
    def test_staging_shares_the_copy_before_it_is_live(self):
        src = tempfile.mkdtemp(prefix="op-src-")
        parent = tempfile.mkdtemp(prefix="op-apps-")
        self.addCleanup(shutil.rmtree, src, True)
        self.addCleanup(shutil.rmtree, parent, True)
        app = os.path.join(src, "OnionPress.app")
        os.makedirs(os.path.join(app, "Contents"))
        with mock.patch.object(updater, "share_bundle_with_admins") as share:
            staged = updater.stage_new_bundle(app, os.path.join(parent, "OnionPress.app"))
        share.assert_called_once_with(staged)

    def test_the_daemon_runs_it_once_per_start_on_a_mac(self):
        with open(os.path.join(os.path.dirname(HERE), "src", "onionpress", "daemon.py"),
                  encoding="utf-8") as f:
            src = f.read()
        run = src[src.index("    def run(self):"):src.index("    def _main_loop_once")]
        self.assertIn("if self.os_name != OS.LINUX:\n            self._share_app_bundle()", run)
        self.assertIn("updater.share_bundle_with_admins(self.paths.app_bundle", src)


if __name__ == "__main__":
    unittest.main()

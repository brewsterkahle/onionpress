#!/usr/bin/env python3
"""Linux's reset and uninstall run the CLI's (issue #529): cli.cmd_reset's
Linux tail (the launcher's start, the hub state's reset) and
onionpress.uninstall's Linux branch behind the check/confirm protocol
(the tray asks the questions, the terminal run does the work), where the
Linux launcher had a 71-line reset case and a 173-line uninstall case
with `read -p` questions of their own."""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
sys.path.insert(0, HERE)

from onionpress import cli, onionheaven, uninstall  # noqa: E402
from onionpress.docker import DockerResult  # noqa: E402
from launcher_harness import BASH, Launcher  # noqa: E402

LAUNCHER = "/opt/onionpress/onionpress"


class FakeDocker:
    def __init__(self, volumes="onionpress-onion-keys\nonionpress-db-data\n", running=True):
        self.calls = []
        self.volumes = volumes
        self.running = running
        self.docker_bin = "docker"

    def run(self, args, **_kw):
        self.calls.append("docker " + " ".join(args))
        if args[:2] == ["volume", "ls"]:
            return DockerResult(0, self.volumes, "")
        return DockerResult(0, "", "")

    def compose(self, args, compose_files=None, **_kw):
        files = " ".join(os.path.basename(f) for f in compose_files or [])
        self.calls.append(f"compose [{files}] " + " ".join(args))
        return DockerResult(0, "", "")

    def exec(self, container, command, **_kw):
        self.calls.append(f"exec {container} " + " ".join(command))
        return DockerResult(0, '{"reset": true, "removed": 2}\n', "")

    def container_running(self, name):
        return self.running


class TestRemoveLinux(unittest.TestCase):

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="op-uninstall-home-")
        self.addCleanup(shutil.rmtree, self.home, True)
        self.data = os.path.join(self.home, ".onionpress")
        os.makedirs(self.data)
        for rel in ("config/systemd/user/onionpress.service",
                    "config/systemd/user/onionpress-daemon.service",
                    "config/autostart/onionpress-tray.desktop",
                    "local/share/applications/onionpress.desktop",
                    "local/share/applications/onionpress-tray.desktop",
                    "local/share/applications/other.desktop",
                    "local/share/icons/hicolor/512x512/apps/onionpress.png",
                    "local/share/icons/hicolor/scalable/apps/onionpress-tray.svg",
                    "local/share/icons/hicolor/scalable/apps/other.svg"):
            path = os.path.join(self.home, "." + rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write("x")
        self.paths = mock.Mock(data_dir=self.data, docker_dir="/opt/onionpress/docker")
        self.logged = []
        self.shs = []

    def remove(self, docker=None, which=("gtk-update-icon-cache", "update-desktop-database")):
        docker = docker or FakeDocker()

        def sh(argv, capture=True, timeout=120):
            self.shs.append((" ".join(argv), capture))
            return mock.Mock(returncode=0)
        with mock.patch.object(uninstall, "_sh", sh), \
                mock.patch.object(uninstall.shutil, "which", lambda n: n if n in which else None), \
                mock.patch.object(uninstall.os.path, "expanduser", lambda p: p.replace("~", self.home)), \
                mock.patch("getpass.getuser", return_value="pat"):
            uninstall._remove_linux(self.paths, docker, self.logged.append)
        return docker

    def left(self):
        out = []
        for root, _d, files in os.walk(self.home):
            for f in files:
                out.append(os.path.relpath(os.path.join(root, f), self.home))
        return sorted(out)

    def test_the_order_and_everything_removed(self):
        docker = self.remove()
        lines = [l for l, _c in self.shs]
        self.assertEqual(lines[:7], [
            "systemctl --user stop onionpress", "systemctl --user stop onionpress-daemon",
            "systemctl --user stop onionpress-heartbeat", "systemctl --user stop onionpress-watcher.timer",
            "sudo systemctl stop onionpress", "sudo systemctl stop onionpress-heartbeat",
            "sudo systemctl stop onionpress-watcher.timer"])
        self.assertEqual(lines[7:9], ["pkill -u pat -f onionpress-tray",
                                      "pkill -u pat -f systemd-inhibit.*OnionPress"])
        self.assertEqual(docker.calls, [
            "compose [docker-compose.yml docker-compose.cloudflare.yml] down -v",
            "docker volume ls -q --filter name=onionpress-",
            "docker volume rm onionpress-onion-keys", "docker volume rm onionpress-db-data"])
        self.assertIn("systemctl --user daemon-reload", lines)
        self.assertIn("sudo systemctl disable onionpress-heartbeat", lines)
        self.assertIn("sudo rm -f /etc/systemd/system/onionpress.service "
                      "/etc/systemd/system/onionpress-heartbeat.service "
                      "/etc/systemd/system/onionpress-watcher.service "
                      "/etc/systemd/system/onionpress-watcher.timer", lines)
        self.assertIn("sudo systemctl daemon-reload", lines)
        self.assertIn("sudo rm -rf /opt/onionpress", lines)
        self.assertIn("sudo rm -f /usr/local/bin/onionpress /usr/local/bin/onionpress-tray", lines)
        self.assertEqual(lines[-2:], [f"gtk-update-icon-cache -t -f {self.home}/.local/share/icons/hicolor",
                                      f"update-desktop-database {self.home}/.local/share/applications"])
        self.assertEqual(self.left(), [".local/share/applications/other.desktop",
                                       ".local/share/icons/hicolor/scalable/apps/other.svg"])
        self.assertEqual(self.logged[-1], "Uninstall: data directory removed")

    def test_sudo_keeps_the_terminal_everything_else_is_captured(self):
        self.remove()
        for line, capture in self.shs:
            self.assertEqual(capture, not line.startswith("sudo "), line)

    def test_missing_cache_tools_are_skipped(self):
        self.remove(which=())
        self.assertFalse(any(l.startswith(("gtk-update", "update-desktop")) for l, _ in self.shs))

    def test_sh_swallows_a_missing_binary(self):
        self.assertIsNone(uninstall._sh(["/nonexistent/x"]))


class TestRunPicksThePlatform(unittest.TestCase):

    def test_linux_runs_the_linux_removal_after_the_common_head(self):
        data = tempfile.mkdtemp(prefix="op-uninstall-run-")
        self.addCleanup(shutil.rmtree, data, True)
        paths = mock.Mock(data_dir=data, docker_dir="")
        order = []
        with mock.patch.object(uninstall.op_daemon, "stop_agent", lambda *a, **k: order.append("daemon-stop")), \
                mock.patch.object(uninstall, "hubs_holding_key", return_value=[("a", "h")]), \
                mock.patch("onionpress.onionheaven.release_onionheaven",
                           side_effect=lambda *a, **k: order.append("release") or []), \
                mock.patch.object(uninstall, "_remove_linux", lambda *a: order.append("linux")), \
                mock.patch.object(uninstall.subprocess, "run",
                                  side_effect=AssertionError("the Mac's stop/colima ran")):
            remaining = uninstall.run(paths, FakeDocker(), lambda m: None, LAUNCHER, os_name="linux")
        self.assertEqual((order, remaining), (["daemon-stop", "release", "linux"], []))


class TestBackupOffer(unittest.TestCase):

    def setUp(self):
        self.out = io.StringIO()
        self.backups = tempfile.mkdtemp(prefix="op-backups-")
        self.addCleanup(shutil.rmtree, self.backups, True)
        self.runs = []

    def offer(self, answers, running=True, password="pw", rc=0):
        def run(argv, env=None, check=False):
            self.runs.append((argv, env))
            return mock.Mock(returncode=rc)
        with mock.patch("builtins.input", side_effect=answers), \
                mock.patch("onionpress.scrub._prompt_and_validate_wp_password", return_value=password), \
                mock.patch.object(cli.subprocess, "run", run):
            return cli.offer_backup_before_uninstall(FakeDocker(running=running), LAUNCHER,
                                                     lambda m: None, out=self.out,
                                                     backups_dir=self.backups)

    def test_a_backup_first(self):
        go_on, path = self.offer(["y"])
        self.assertTrue(go_on)
        self.assertTrue(path.startswith(os.path.join(self.backups, "OnionPress-backup-")))
        argv, env = self.runs[0]
        self.assertEqual(argv, [LAUNCHER, "backup", "-o", path])
        self.assertEqual(env["ONIONPRESS_BACKUP_PASSWORD"], "pw")
        self.assertIn("IMPORTANT: Copy this file somewhere safe", self.out.getvalue())

    def test_enter_means_yes_and_n_means_no(self):
        self.assertEqual(self.offer([""])[0], True)
        self.assertEqual(len(self.runs), 1)
        self.assertEqual(self.offer(["n"]), (True, ""))
        self.assertEqual(len(self.runs), 1)

    def test_a_bad_password_three_times_keeps_the_install(self):
        self.assertEqual(self.offer(["y"], password=None), (False, ""))
        self.assertEqual(self.runs, [])
        self.assertIn("Cancelled — install untouched.", self.out.getvalue())

    def test_a_failed_backup_keeps_the_install(self):
        self.assertEqual(self.offer(["y"], rc=1), (False, ""))
        self.assertIn("Backup failed. Aborting uninstall — install untouched.", self.out.getvalue())

    def test_containers_down_may_go_on_without_a_backup(self):
        self.assertEqual(self.offer(["y", "y"], running=False), (True, ""))
        self.assertEqual(self.offer(["y", ""], running=False), (False, ""))
        self.assertIn("Containers are not running — cannot create backup.", self.out.getvalue())
        self.assertEqual(self.runs, [])


class TestCmdUninstallOnLinux(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="op-uninstall-cmd-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.paths = mock.Mock(data_dir=self.dir, docker_dir="")
        self.out = io.StringIO()

    def cmd(self, *, tty, yes=False, offer=(True, ""), **kw):
        env = {"ONIONPRESS_YES": "true"} if yes else {}
        with mock.patch("onionpress.uninstall.check", return_value={"running": True, "hubs": 0}), \
                mock.patch("onionpress.uninstall.run", return_value=[]) as run, \
                mock.patch.object(cli, "offer_backup_before_uninstall", return_value=offer) as off, \
                mock.patch.object(sys, "stdin", mock.Mock(isatty=lambda: tty)), \
                mock.patch.dict(os.environ, env, clear=False), \
                contextlib.redirect_stdout(self.out):
            os.environ.pop("ONIONPRESS_YES", None) if not yes else None
            rc = cli.cmd_uninstall(self.paths, FakeDocker(), lambda m: None, LAUNCHER,
                                   os_name="linux", confirmed={"hub-holds-key", "uninstall"}, **kw)
        return rc, run, off

    def test_at_a_terminal_the_backup_is_offered_then_the_linux_removal_runs(self):
        rc, run, off = self.cmd(tty=True, offer=(True, "/b/x.zip"))
        self.assertEqual(rc, 0)
        off.assert_called_once()
        self.assertEqual(run.call_args.kwargs["os_name"], "linux")
        text = self.out.getvalue()
        self.assertIn("OnionPress has been uninstalled.\n", text)
        self.assertNotIn("Trash", text)
        self.assertIn("Your backup is at: /b/x.zip", text)
        self.assertIn("onionpress restore /b/x.zip", text)

    def test_a_declined_offer_keeps_the_install(self):
        rc, run, _off = self.cmd(tty=True, offer=(False, ""))
        self.assertEqual(rc, 1)
        run.assert_not_called()

    def test_no_offer_off_a_terminal_or_under_dash_y(self):
        for tty, yes in ((False, False), (True, True)):
            with self.subTest(tty=tty, yes=yes):
                rc, run, off = self.cmd(tty=tty, yes=yes)
                self.assertEqual(rc, 0)
                off.assert_not_called()
                run.assert_called_once()

    def test_json_carries_the_backup(self):
        rc, _run, _off = self.cmd(tty=True, offer=(True, "/b/x.zip"), json_output=True)
        self.assertEqual(rc, 0)
        import json
        self.assertEqual(json.loads(self.out.getvalue().strip().splitlines()[-1])["backup"], "/b/x.zip")


class TestResetHubState(unittest.TestCase):

    def test_runs_the_script_in_the_container_and_parses_the_answer(self):
        docker = FakeDocker()
        logged = []
        self.assertEqual(onionheaven.reset_hub_state(docker, logged.append), {"reset": True, "removed": 2})
        self.assertEqual(docker.calls, [f"exec onionheaven python3 -c {onionheaven.RESET_HUB_SCRIPT}"])
        self.assertTrue(logged[0].startswith("OnionHeaven reset: "))

    def test_a_failure_is_a_warning_and_none(self):
        docker = mock.Mock()
        docker.exec.return_value = DockerResult(1, "", "boom")
        logged = []
        self.assertIsNone(onionheaven.reset_hub_state(docker, logged.append))
        self.assertEqual(logged, ["WARNING: OnionHeaven reset failed (rc=1): boom"])
        docker.exec.side_effect = OSError("no docker")
        self.assertIsNone(onionheaven.reset_hub_state(docker, logged.append))


@unittest.skipUnless(os.path.exists(BASH), "needs /bin/bash")
class TestLinuxLauncherCases(unittest.TestCase):

    def setUp(self):
        self.l = Launcher(self, "linux", stub=("setup_db_passwords",))

    def dd(self):
        return f"--data-dir {self.l.data_dir}"

    def test_reset_is_the_clis_with_dash_y_as_yes(self):
        r = self.l.run("reset", "--check", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.l.py_calls(), [f"{self.dd()} reset --check --json"])
        r = self.l.run("reset", "-y")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.l.py_calls(), [f"{self.dd()} reset --yes"])

    def test_uninstall_is_the_clis_and_dash_y_confirms_through_the_environment(self):
        self.l.add_stubs('_op_rec_env() { _op_rec "confirmed ${ONIONPRESS_CONFIRMED:-unset}"; }\n'
                         'op_py() { _op_rec_env; _op_py "$@"; }')
        r = self.l.run("uninstall", "--check", "--json", "--confirmed", "uninstall")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.l.py_calls(), [f"{self.dd()} uninstall --check --json --confirmed uninstall"])
        self.assertIn("confirmed unset", self.l.calls())
        r = self.l.run("uninstall", "-y")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.l.py_calls(), [f"{self.dd()} uninstall"])
        self.assertIn("confirmed hub-holds-key uninstall", self.l.calls())

    def test_no_bash_questions_are_left(self):
        with open(os.path.join(os.path.dirname(HERE), "linux", "onionpress"), encoding="utf-8") as f:
            text = f.read()
        for gone in ("prompt_and_validate_wp_password", "verify_wp_admin_password() {",
                     "Create a backup before uninstalling", "USER_SYSTEMD_DIR", "Removing /opt/onionpress"):
            self.assertNotIn(gone, text, gone)


if __name__ == "__main__":
    unittest.main()

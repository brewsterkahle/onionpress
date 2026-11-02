#!/usr/bin/env python3
"""Ports and paths resolved in Python only (issue #405).

ports.resolve_at_start is the one rule for the ports: a preset
ONIONPRESS_PORT_OFFSET as it is, else the previous instance's port
waited for (.previous-port-offset), the running stack's published port,
else the bind probe. `onionpress launcher-env` prints its answer and the
paths KEY=VALUE per line, and each launcher's main() opens with
`launcher_environment`, which exports what it is given (an allowlist)
and, when the Python does not answer, the preset offset or 0 with an
ERROR in the log. The daemon's and OnionPressCLI's use of the rule is
tested beside them (TestResolvePorts, TestPortOffsetFromTheLauncher)."""

import contextlib
import io
import os
import shutil
import socket
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
sys.path.insert(0, HERE)

from onionpress import cli, ports  # noqa: E402
from onionpress.platform import OS, resolve_paths  # noqa: E402
from launcher_harness import BASH, Launcher  # noqa: E402


class _BindSocket:
    """socket.socket stand-in: *busy* ports refuse bind, a number of times
    each (None: always)."""

    def __init__(self, busy, bound):
        self.busy, self.bound = busy, bound

    def __call__(self, *a, **k):
        return self

    def bind(self, addr):
        self.bound.append(addr)
        left = self.busy.get(addr[1], 0)
        if left is None or left > 0:
            if left:
                self.busy[addr[1]] = left - 1
            raise OSError(48, "Address already in use")

    def close(self):
        pass


class _Case(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="op-ports-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.sentinel = os.path.join(self.dir, ports.PREVIOUS_OFFSET_SENTINEL)
        self.logged = []
        self.sleeps = []

    def write_sentinel(self, text, age=0):
        with open(self.sentinel, "w") as f:
            f.write(text)
        if age:
            old = time.time() - age
            os.utime(self.sentinel, (old, old))


class TestWaitForPreviousPort(_Case):

    def wait(self, busy=None):
        bound = []
        with mock.patch.object(ports.socket, "socket", _BindSocket(busy or {}, bound)):
            ports.wait_for_previous_port(self.dir, log=self.logged.append,
                                         sleep=self.sleeps.append)
        return bound

    def test_waits_for_the_previous_instance_s_port_then_removes_the_sentinel(self):
        self.write_sentinel("10000")
        bound = self.wait(busy={18080: 2})
        self.assertEqual(bound, [("127.0.0.1", 18080)] * 3)
        self.assertEqual(self.sleeps, [0.5, 0.5])
        self.assertEqual(self.logged, ["Waiting for the previous instance's port 18080 to free"])
        self.assertFalse(os.path.exists(self.sentinel))

    def test_a_free_port_is_not_waited_for(self):
        self.write_sentinel("0\n")
        self.assertEqual(self.wait(), [("127.0.0.1", 8080)])
        self.assertEqual(self.sleeps, [])
        self.assertEqual(self.logged, [])

    def test_gives_up_after_fifteen_seconds(self):
        self.write_sentinel("0")
        bound = self.wait(busy={8080: None})
        self.assertEqual(len(bound), ports.PREVIOUS_PORT_WAIT_TRIES)
        self.assertEqual(len(self.sleeps), ports.PREVIOUS_PORT_WAIT_TRIES)
        self.assertFalse(os.path.exists(self.sentinel))

    def test_an_old_sentinel_is_removed_without_waiting(self):
        self.write_sentinel("0", age=ports.SENTINEL_MAX_AGE + 540)
        self.assertEqual(self.wait(), [])
        self.assertFalse(os.path.exists(self.sentinel))

    def test_a_garbled_sentinel_stops_nothing(self):
        self.write_sentinel("ten thousand")
        self.assertEqual(self.wait(), [])

    def test_no_sentinel_is_nothing_to_do(self):
        self.assertEqual(self.wait(), [])
        self.assertEqual(self.sleeps, [])


class TestResolveAtStart(_Case):

    def resolve(self, preset, running=None, probe=None, busy=None):
        bound = []
        docker = mock.Mock()
        with mock.patch.object(ports, "get_running_wp_port", return_value=running) as read, \
                mock.patch.object(ports, "detect_port_offset",
                                  return_value=probe or ports.PortConfig.from_offset(0)) as det, \
                mock.patch.object(ports.socket, "socket", _BindSocket(busy or {}, bound)):
            pc = ports.resolve_at_start(self.dir, preset=preset, docker=docker,
                                        log=self.logged.append, sleep=self.sleeps.append)
        return pc, read, det, docker, bound

    def test_a_preset_offset_is_taken_as_it_is(self):
        self.write_sentinel("10000")
        pc, read, det, _d, bound = self.resolve("20000")
        self.assertEqual(pc, ports.PortConfig.from_offset(20000))
        read.assert_not_called()
        det.assert_not_called()
        self.assertEqual(bound, [])
        self.assertTrue(os.path.exists(self.sentinel), "a preset leaves the sentinel to the resolver")

    def test_zero_is_a_preset_too(self):
        pc, read, _det, _d, _b = self.resolve("0")
        self.assertEqual(pc.wp_port, 8080)
        read.assert_not_called()

    def test_without_a_preset_the_running_stack_s_port_decides(self):
        self.write_sentinel("0")
        pc, read, det, docker, bound = self.resolve(None, running=18080)
        self.assertEqual(pc, ports.PortConfig.from_offset(10000))
        read.assert_called_once_with(docker=docker)
        det.assert_not_called()
        self.assertEqual(bound, [("127.0.0.1", 8080)], "the sentinel was waited on first")
        self.assertFalse(os.path.exists(self.sentinel))

    def test_then_the_probe(self):
        pc, _read, det, _d, _b = self.resolve("junk", probe=ports.PortConfig.from_offset(30000))
        self.assertEqual(pc.offset, 30000)
        det.assert_called_once_with(log=self.logged.append)

    def test_a_unicode_digit_is_not_a_preset(self):
        _pc, read, _det, _d, _b = self.resolve("\u0663")
        read.assert_called_once()


class TestDetectPortOffsetLogs(unittest.TestCase):

    def test_each_bump_is_logged_in_the_mac_launcher_s_words(self):
        logged = []
        with mock.patch.object(ports.socket, "socket", _BindSocket({8080: None, 19050: None}, [])):
            pc = ports.detect_port_offset(log=logged.append)
        self.assertEqual(pc.offset, 20000)
        self.assertEqual(logged, ["Port 8080 in use, trying offset 10000",
                                  "Port 19050 in use, trying offset 20000"])

    def test_running_out_of_ports_is_an_error_and_offset_0(self):
        logged = []
        with mock.patch.object(ports.socket, "socket", _BindSocket({}, [])) as sock:
            sock.bind = lambda addr: (_ for _ in ()).throw(OSError(48, "busy"))
            pc = ports.detect_port_offset(log=logged.append)
        self.assertEqual(pc.offset, 0)
        self.assertEqual(logged[-1], "ERROR: No available ports (tried offsets 0-50000)")


class TestGetRunningWpPortThroughDocker(unittest.TestCase):
    """With a docker.Docker the question goes through its environment
    (DOCKER_HOST, the bundled binary); the bare `docker` on PATH only
    without one."""

    def test_asks_docker_port_and_reads_the_host_port(self):
        docker = mock.Mock()
        docker.run.return_value = mock.Mock(returncode=0, stdout="0.0.0.0:18080\n[::]:18080\n")
        self.assertEqual(ports.get_running_wp_port(docker=docker), 18080)
        docker.run.assert_called_once_with(["port", "onionpress-wordpress", "80"],
                                           timeout=10, quiet=True)

    def test_a_failed_or_odd_answer_is_none(self):
        docker = mock.Mock()
        docker.run.return_value = mock.Mock(returncode=1, stdout="")
        self.assertIsNone(ports.get_running_wp_port(docker=docker))
        docker.run.return_value = mock.Mock(returncode=0, stdout="127.0.0.1:80\n")
        self.assertIsNone(ports.get_running_wp_port(docker=docker))
        docker.run.side_effect = OSError("no docker")
        self.assertIsNone(ports.get_running_wp_port(docker=docker))


class TestLauncherEnvironment(unittest.TestCase):
    """cli.launcher_environment: what `launcher-env` prints, by platform."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="op-env-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.pc = ports.PortConfig.from_offset(10000)

    def paths(self, os_name):
        with mock.patch("onionpress.platform.detect_os", return_value=os_name), \
                mock.patch("onionpress.platform.linux_docker_dir", return_value="/opt/onionpress/docker"), \
                mock.patch("os.path.exists", return_value=os_name == OS.LINUX):
            return resolve_paths(data_dir=self.dir, app_bundle="")

    def test_the_mac_gets_the_ports_and_the_docker_dir_only(self):
        paths = self.paths(OS.MACOS)
        env = cli.launcher_environment(paths, self.pc, environ={"DOCKER_HOST": "unix:///x"},
                                       os_name="macos")
        self.assertEqual(env, {"ONIONPRESS_PORT_OFFSET": "10000", "ONIONPRESS_WP_PORT": "18080",
                               "ONIONPRESS_SOCKS_PORT": "19050", "ONIONPRESS_PROXY_PORT": "19077",
                               "DOCKER_DIR": ""})

    def test_linux_gets_the_rootless_socket_when_it_exists(self):
        paths = self.paths(OS.LINUX)
        env = cli.launcher_environment(paths, self.pc, environ={}, os_name="linux")
        sock = f"/run/user/{os.getuid()}/docker.sock"
        self.assertEqual(env["DOCKER_HOST"], f"unix://{sock}")
        self.assertEqual(env["DOCKER_SOCK"], sock)
        self.assertEqual(env["DOCKER_DIR"], "/opt/onionpress/docker")
        self.assertEqual(env["ONIONPRESS_WP_PORT"], "18080")

    def test_linux_keeps_a_preset_docker_host(self):
        paths = self.paths(OS.LINUX)
        env = cli.launcher_environment(paths, self.pc, environ={"DOCKER_HOST": "unix:///preset.sock"},
                                       os_name="linux")
        self.assertEqual(env["DOCKER_HOST"], "unix:///preset.sock")
        self.assertEqual(env["DOCKER_SOCK"], "/preset.sock")
        env = cli.launcher_environment(paths, self.pc, environ={"DOCKER_HOST": "tcp://h:2375"},
                                       os_name="linux")
        self.assertEqual(env["DOCKER_HOST"], "tcp://h:2375")
        self.assertEqual(env["DOCKER_SOCK"], paths.docker_socket)

    def test_every_key_printed_is_in_the_allowlist(self):
        paths = self.paths(OS.LINUX)
        env = cli.launcher_environment(paths, self.pc, environ={}, os_name="linux")
        self.assertEqual(set(env) - set(cli.LAUNCHER_ENV_KEYS), set())


class TestLauncherEnvCommand(unittest.TestCase):
    """`onionpress launcher-env`: the rule with the environment's preset,
    KEY=VALUE on stdout, log lines on stderr, never a traceback."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="op-env-cmd-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def run_cmd(self, env, pc=None, raises=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(cli, "Docker"), \
                mock.patch.object(cli, "detect_os", return_value=OS.MACOS), \
                mock.patch.object(ports, "resolve_at_start",
                                  return_value=pc, side_effect=raises) as resolve, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if "ONIONPRESS_PORT_OFFSET" not in env:
                os.environ.pop("ONIONPRESS_PORT_OFFSET", None)
            rc = cli.main(["--data-dir", self.dir, "launcher-env"])
        return rc, out.getvalue(), err.getvalue(), resolve

    def test_prints_the_ports_and_paths(self):
        rc, out, err, resolve = self.run_cmd({"ONIONPRESS_PORT_OFFSET": "10000"},
                                             pc=ports.PortConfig.from_offset(10000))
        self.assertEqual(rc, 0)
        self.assertEqual(resolve.call_args.kwargs["preset"], "10000")
        self.assertEqual(resolve.call_args.args[0], self.dir)
        lines = out.splitlines()
        self.assertEqual(lines[:4], ["ONIONPRESS_PORT_OFFSET=10000", "ONIONPRESS_WP_PORT=18080",
                                     "ONIONPRESS_SOCKS_PORT=19050", "ONIONPRESS_PROXY_PORT=19077"])
        self.assertTrue(lines[4].startswith("DOCKER_DIR="))
        self.assertEqual(len(lines), 5, "the Mac's DOCKER_HOST is the launcher's own")
        self.assertEqual(err, "")

    def test_a_failure_is_an_error_line_and_exit_1(self):
        rc, out, err, _r = self.run_cmd({}, raises=RuntimeError("no socket"))
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertRegex(err, r"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\] ERROR: the ports and paths "
                              r"could not be resolved: no socket\n$")

    def test_a_value_with_a_newline_is_not_printed(self):
        with mock.patch.object(cli, "launcher_environment",
                               return_value={"ONIONPRESS_WP_PORT": "8080", "DOCKER_DIR": "/a\n/b"}):
            rc, out, err, _r = self.run_cmd({}, pc=ports.PortConfig.from_offset(0))
        self.assertEqual(rc, 0)
        self.assertEqual(out, "ONIONPRESS_WP_PORT=8080\n")
        self.assertIn("ERROR: DOCKER_DIR holds a newline; not exported", err)

    def test_no_onionpress_cli_is_built(self):
        with mock.patch.object(cli, "OnionPressCLI") as MockCLI:
            self.run_cmd({}, pc=ports.PortConfig.from_offset(0))
        MockCLI.assert_not_called()


@unittest.skipUnless(os.path.exists(BASH), "needs /bin/bash")
class TestLauncherEnvironmentInBash(unittest.TestCase):
    """Each launcher's main() opens with launcher_environment: the CLI's
    answer exported (an allowlist, numbers checked), and without one the
    preset offset or 0 with an ERROR in the log."""

    RECORD = ('stop_containers() { _op_rec "env ${ONIONPRESS_PORT_OFFSET:-unset}'
              ' ${ONIONPRESS_WP_PORT:-unset} ${ONIONPRESS_SOCKS_PORT:-unset}'
              ' ${ONIONPRESS_PROXY_PORT:-unset} $DOCKER_DIR ${DOCKER_HOST:-unset}'
              ' ${DOCKER_SOCK:-unset}"; }')

    def launcher(self, platform):
        launcher = Launcher(self, platform, stub=("setup_db_passwords", "stop_onionheaven_farm",
                                                   "daemon_offline"))
        launcher.add_stubs(self.RECORD)
        return launcher

    def env_line(self, launcher):
        lines = [c for c in launcher.calls(env=True) if c.startswith("env ")]
        self.assertEqual(len(lines), 1, launcher.calls())
        return lines[0][4:].split(" ")

    def log(self, launcher):
        try:
            with open(os.path.join(launcher.data_dir, "onionpress.log")) as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def test_main_s_first_call_is_launcher_env_with_this_data_dir(self):
        for platform in ("mac", "linux"):
            with self.subTest(platform=platform):
                launcher = self.launcher(platform)
                r = launcher.run("stop")
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(launcher.py_calls(env=True)[0],
                                 f"--data-dir {launcher.data_dir} launcher-env")
                self.assertEqual(launcher.calls(env=True)[1], "py-env <none>")
                self.assertNotIn("launcher-env", " ".join(launcher.calls()))

    def test_the_answer_is_exported(self):
        for platform in ("mac", "linux"):
            with self.subTest(platform=platform):
                launcher = self.launcher(platform)
                answer = ("ONIONPRESS_PORT_OFFSET=20000\nONIONPRESS_WP_PORT=28080\n"
                          "ONIONPRESS_SOCKS_PORT=29050\nONIONPRESS_PROXY_PORT=29077\n"
                          f"DOCKER_DIR={launcher.tmp}\nDOCKER_HOST=unix:///run/x.sock\n"
                          "DOCKER_SOCK=/run/x.sock\nPATH=/evil\nHOME=/evil")
                r = launcher.run("stop", env={"STUB_LAUNCHER_ENV": answer, "ONIONPRESS_PORT_OFFSET": "0"})
                self.assertEqual(r.returncode, 0, r.stderr)
                got = self.env_line(launcher)
                self.assertEqual(got[:5], ["20000", "28080", "29050", "29077", launcher.tmp])
                if platform == "linux":
                    self.assertEqual(got[5:], ["unix:///run/x.sock", "/run/x.sock"])
                else:
                    # The Mac's Colima environment is the launcher's own.
                    self.assertEqual(got[5], f"unix://{launcher.data_dir}/colima/default/docker.sock")
                    self.assertEqual(got[6], "unset")
                self.assertNotIn("ERROR", self.log(launcher))

    def test_a_port_that_is_not_a_number_and_a_missing_folder_are_not_taken(self):
        launcher = self.launcher("linux")
        answer = ("ONIONPRESS_PORT_OFFSET=0\nONIONPRESS_WP_PORT=8080; rm -rf /\n"
                  "ONIONPRESS_SOCKS_PORT=9050\nONIONPRESS_PROXY_PORT=9077\n"
                  f"DOCKER_DIR={launcher.tmp}/missing\nDOCKER_HOST=\n")
        r = launcher.run("stop", env={"STUB_LAUNCHER_ENV": answer})
        self.assertEqual(r.returncode, 0, r.stderr)
        got = self.env_line(launcher)
        # No WordPress port in the answer: the fallback sets all four from the offset.
        self.assertEqual(got[:4], ["0", "8080", "9050", "9077"])
        self.assertEqual(got[4], launcher.docker_dir)
        self.assertEqual(got[5:], ["unset", "unset"])
        self.assertIn("ERROR: the ports could not be resolved (onionpress launcher-env gave no answer): using offset 0",
                      self.log(launcher))

    def test_without_an_answer_the_preset_offset_or_0_is_used(self):
        for platform, preset, want in (("mac", "10000", ["10000", "18080", "19050", "19077"]),
                                       ("linux", "junk", ["0", "8080", "9050", "9077"]),
                                       ("linux", "", ["0", "8080", "9050", "9077"])):
            with self.subTest(platform=platform, preset=preset):
                launcher = self.launcher(platform)
                r = launcher.run("stop", env={"STUB_LAUNCHER_ENV": "FAIL",
                                              "ONIONPRESS_PORT_OFFSET": preset})
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(self.env_line(launcher)[:4], want)
                self.assertIn(f"gave no answer): using offset {want[0]}", self.log(launcher))

    def test_the_sentinel_written_on_stop_carries_the_resolved_offset(self):
        launcher = self.launcher("linux")
        launcher.add_stubs('stop_containers() { echo "${ONIONPRESS_PORT_OFFSET:-0}" > "$DATA_DIR/.previous-port-offset"; }')
        answer = ("ONIONPRESS_PORT_OFFSET=10000\nONIONPRESS_WP_PORT=18080\n"
                  "ONIONPRESS_SOCKS_PORT=19050\nONIONPRESS_PROXY_PORT=19077\n")
        r = launcher.run("stop", env={"STUB_LAUNCHER_ENV": answer})
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(launcher.data_dir, ".previous-port-offset")) as f:
            self.assertEqual(f.read().strip(), "10000")


if __name__ == "__main__":
    unittest.main()

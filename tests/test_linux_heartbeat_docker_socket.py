#!/usr/bin/env python3
"""The Linux heartbeat's docker calls reach the daemon the rest of
OnionPress uses (issue #503): under the .deb's rootless Docker the unit,
run without the launcher's DOCKER_HOST, sent every `docker` to
/var/run/docker.sock, so there was no heartbeat and no republish."""

import importlib.util
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PROJECT_DIR, "src"))

# The script imports these as top-level modules from /opt/onionpress/scripts
# (never put src/onionpress itself on sys.path: its platform.py would shadow
# the standard library's).
from onionpress import key_manager, onion_auth  # noqa: E402
from onionpress import platform as op_platform  # noqa: E402

ROOTLESS = f"/run/user/{os.getuid()}/docker.sock"


def _load_heartbeat(home):
    sys.modules.setdefault("key_manager", key_manager)
    sys.modules.setdefault("onion_auth", onion_auth)
    with mock.patch.dict(os.environ, {"HOME": home}):
        spec = importlib.util.spec_from_file_location(
            "onionpress_heartbeat_docker_socket_test",
            os.path.join(PROJECT_DIR, "linux", "onionpress-heartbeat.py"))
        hb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hb)
    return hb


class TestDockerSocket(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="op-hb-socket-")
        os.makedirs(os.path.join(self.tmp, ".onionpress"))
        self.hb = _load_heartbeat(self.tmp)

    def _linux(self, rootless_present):
        real_exists = os.path.exists
        return (mock.patch.object(op_platform, "detect_os", return_value=op_platform.OS.LINUX),
                mock.patch("os.path.exists",
                           side_effect=lambda p: rootless_present if p == ROOTLESS
                           else real_exists(p)))

    def test_the_rootless_socket_when_it_exists_and_no_docker_host(self):
        env = {}
        a, b = self._linux(True)
        with a, b:
            self.assertEqual(self.hb.point_docker_at_the_daemon(env), f"unix://{ROOTLESS}")
        self.assertEqual(env, {"DOCKER_HOST": f"unix://{ROOTLESS}"})

    def test_a_preset_docker_host_is_kept(self):
        env = {"DOCKER_HOST": "unix:///elsewhere.sock"}
        a, b = self._linux(True)
        with a, b:
            self.assertEqual(self.hb.point_docker_at_the_daemon(env), "unix:///elsewhere.sock")
        self.assertEqual(env["DOCKER_HOST"], "unix:///elsewhere.sock")

    def test_system_docker_keeps_dockers_default(self):
        """No rootless socket and none of the system's either (a daemon
        that is not up yet): nothing is set, docker's own default applies."""
        env = {}
        real_exists = os.path.exists
        with mock.patch.object(op_platform, "detect_os", return_value=op_platform.OS.LINUX), \
             mock.patch("os.path.exists",
                        side_effect=lambda p: False if p in (ROOTLESS, "/var/run/docker.sock")
                        else real_exists(p)):
            self.assertEqual(self.hb.point_docker_at_the_daemon(env), "")
        self.assertEqual(env, {})

    def test_docker_exec_runs_against_that_socket(self):
        """The helper run for real: a fake `docker` on PATH reports the
        DOCKER_HOST it was given."""
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir)
        fake = os.path.join(bin_dir, "docker")
        with open(fake, "w") as f:
            f.write("#!/bin/sh\nprintf '%s' \"${DOCKER_HOST:-unset}\"\n")
        os.chmod(fake, os.stat(fake).st_mode | stat.S_IXUSR)
        env = {"PATH": bin_dir}
        a, b = self._linux(True)
        with mock.patch.dict(os.environ, env, clear=True), a, b:
            self.hb.point_docker_at_the_daemon()
            ok, out = self.hb.docker_exec("onionpress-tor", ["cat", "hostname"])
        self.assertTrue(ok)
        self.assertEqual(out, f"unix://{ROOTLESS}")
        # Without the step the fake saw no socket: what #503 found.
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(self.hb.docker_exec("onionpress-tor", ["x"])[1], "unset")

    def test_main_does_it_before_the_first_docker_call(self):
        with open(os.path.join(PROJECT_DIR, "linux", "onionpress-heartbeat.py")) as f:
            src = f.read()
        main = src[src.index("\ndef main():"):]
        self.assertLess(main.index("point_docker_at_the_daemon()"), main.index("wait_for_ready()"))

    def test_the_unit_sets_no_docker_host(self):
        """A system-Docker install has no rootless socket: the choice is the
        heartbeat's at run time, never the unit's."""
        with open(os.path.join(PROJECT_DIR, "linux", "onionpress-heartbeat.service")) as f:
            self.assertNotIn("DOCKER_HOST", f.read())


if __name__ == "__main__":
    unittest.main()

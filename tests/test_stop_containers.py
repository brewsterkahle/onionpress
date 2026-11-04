"""The stop: ContainerManager.stop and `onionpress stop-containers`, both
launchers' stop_containers (CLAUDE.md, "Cloudflare Tunnel Runs on Both
Platforms": stops always name the override file).

ContainerManager.stop named docker-compose.cloudflare.yml only while a
token was set, so a cloudflared started before its token was removed
outlived `cmd_stop`, the restore's teardown and a key import. The
launchers' bash stop always named it, and stopped the farm's workers by
hand; both are this one stop now."""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from onionpress import cli, ports  # noqa: E402
from onionpress.config import PortConfig  # noqa: E402
from onionpress.containers import ContainerManager  # noqa: E402
from onionpress.docker import DockerResult  # noqa: E402

from fake_docker import FakeDocker  # noqa: E402
from test_onionpress_containers import _make_paths  # noqa: E402

WORKERS = "onionheaven-takeover-1\nonionheaven-poll-12\nonionpress-tor\n" \
          "onionheaven-takeover-x\nmy-onionheaven-poll-3\nonionheaven\n"


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="op-stop-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.paths = _make_paths(self.tmp)
        self.cf_file = os.path.join(self.paths.docker_dir, "docker-compose.cloudflare.yml")
        with open(self.cf_file, "w") as f:
            f.write("services: {}\n")
        self.down_rc = 0

    def answer(self, argv, **kw):
        if argv[1:3] == ["ps", "-a"]:
            return DockerResult(0, WORKERS, "")
        if "down" in argv:
            return DockerResult(self.down_rc, "", "stub: down failed" if self.down_rc else "")
        return None

    def downs(self, docker):
        return [a for a in docker.argvs("compose") if "down" in a]


class TestContainerManagerStop(_Case):

    def _stop(self, token):
        if token is not None:
            with open(self.paths.config_file, "w") as f:
                f.write(f"CLOUDFLARE_TUNNEL_TOKEN={token}\n")
        docker = FakeDocker(self.answer)
        cm = ContainerManager(docker, self.paths, PortConfig.from_offset(0))
        return cm.stop(), docker

    def test_the_override_file_is_named_with_or_without_a_token(self):
        for token in (None, "", "tok"):
            with self.subTest(token=token):
                ok, docker = self._stop(token)
                self.assertTrue(ok)
                compose_yml = os.path.join(self.paths.docker_dir, "docker-compose.yml")
                self.assertEqual(self.downs(docker), [
                    ["compose", "-f", compose_yml, "-f", self.cf_file, "down"]])

    def test_the_farm_goes_first_and_only_its_workers(self):
        ok, docker = self._stop(None)
        self.assertTrue(ok)
        stopped = sorted(a[-1] for a in docker.argvs("stop"))
        self.assertEqual(stopped, ["onionheaven-poll-12", "onionheaven-takeover-1"])
        self.assertEqual(sorted(a[-1] for a in docker.argvs("rm")), stopped)
        for a in docker.argvs("stop"):
            self.assertEqual(a[:3], ["stop", "-t", "1"])
        flat = [c[1:] for c in docker.calls]
        self.assertLess(max(flat.index(a) for a in docker.argvs("rm")),
                        flat.index(self.downs(docker)[0]))

    def test_a_failed_down_is_false(self):
        self.down_rc = 1
        ok, _ = self._stop(None)
        self.assertFalse(ok)


class TestStopContainersCommand(_Case):
    """`onionpress stop-containers`, what both launchers' stop_containers
    run (tests/test_launcher_dispatch_{mac,linux}.py hold the launchers'
    side)."""

    def _main(self, offset="0"):
        docker = FakeDocker(self.answer)
        out = io.StringIO()
        env = {"ONIONPRESS_PORT_OFFSET": offset}
        with mock.patch.object(cli, "resolve_paths", return_value=self.paths), \
                mock.patch.object(cli, "Docker", return_value=docker), \
                mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out):
            rc = cli.main(["--data-dir", self.paths.data_dir, "stop-containers"])
        return rc, docker, out.getvalue()

    def test_a_stop(self):
        rc, docker, out = self._main(offset="10000")
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.downs(docker)), 1)
        self.assertIn(self.cf_file, self.downs(docker)[0])
        self.assertIn("Stopping OnionPress containers...", out)
        self.assertIn("Containers stopped", out)
        # The next start waits for this offset's port (ports.wait_for_previous_port).
        with open(os.path.join(self.paths.data_dir, ports.PREVIOUS_OFFSET_SENTINEL)) as f:
            self.assertEqual(f.read(), "10000")

    def test_a_failed_down_is_exit_1(self):
        self.down_rc = 3
        rc, _, out = self._main()
        self.assertEqual(rc, 1)
        self.assertIn("ERROR: stopping the containers failed", out)
        self.assertNotIn("Containers stopped", out)


if __name__ == "__main__":
    unittest.main()

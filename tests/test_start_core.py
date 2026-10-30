#!/usr/bin/env python3
"""The middle of a launcher's start (issue #442.6): start.start_core over
ContainerManager.start_core, which both launchers run through
`onionpress start-core`.

Held here: egress rules before anything starts (issue #144), then retiring
an older OnionPress's containers, then the Tor network's move, then the
core `compose up` (autoheal with it), 3 attempts 5 s apart; the start-time
pull is missing-only and never for a locally built stack; Linux pulls the
Cloudflare file with a tunnel and streams compose's output, the Mac sends
it to the log; WordPress's port is waited for, then `wp core is-installed`
15 times."""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

from onionpress import containers, start  # noqa: E402
from onionpress.config import PortConfig  # noqa: E402
from onionpress.docker import DockerResult  # noqa: E402
from onionpress.platform import resolve_paths  # noqa: E402

MAC_LAUNCHER = os.path.join(ROOT, "app", "MacOS", "onionpress")
LINUX_LAUNCHER = os.path.join(ROOT, "linux", "onionpress")


class FakeDocker:
    def __init__(self, up_fails=0, installed=True):
        self.up_fails, self.installed = up_fails, installed
        self.order, self.to = [], []
        self.env = {}

    def run(self, args, **_kw):
        args = [str(a) for a in args]
        if "is-installed" in args:
            self.order.append("is-installed")
            return DockerResult(0 if self.installed else 1, "", "")
        return DockerResult(0, "", "")

    def run_to(self, args, *, log_file=None, extra_env=None):
        self.to.append((args, log_file))
        if args[-len(containers.CORE_SERVICES) - 2:-len(containers.CORE_SERVICES)] == ["up", "-d"]:
            self.order.append("up")
            if self.up_fails:
                self.up_fails -= 1
                return 1
        return 0


class Base(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.data, True)
        self.docker_dir = os.path.join(self.data, "docker")
        os.makedirs(self.docker_dir)
        for name in ("docker-compose.yml", "docker-compose.cloudflare.yml"):
            open(os.path.join(self.docker_dir, name), "w").close()
        self.config = os.path.join(self.data, "config")
        open(self.config, "w").close()
        self.lines = []
        sleep = mock.patch("time.sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def manager(self, docker):
        import dataclasses
        paths = dataclasses.replace(resolve_paths(data_dir=self.data, app_bundle=""),
                                    docker_dir=self.docker_dir)
        m = containers.ContainerManager(docker, paths, PortConfig.from_offset(0),
                                        log_func=self.lines.append,
                                        egress=lambda: docker.order.append("own-egress"))
        m.retire_old_onionheaven_containers = lambda: docker.order.append("retire")
        m.migrate_tor_network = lambda: docker.order.append("migrate")
        m._build_env = lambda: {}
        return m


class TestStartCoreOrder(Base):

    def test_egress_then_retire_then_migrate_then_up(self):
        for launcher in (None, "mac", "linux"):
            with self.subTest(launcher=launcher):
                docker = FakeDocker()
                m = self.manager(docker)
                if launcher is None:
                    m.docker.compose = lambda args, **k: (docker.order.append("up"),
                                                          DockerResult(0, "", ""))[1]
                self.assertTrue(m.start_core(
                    launcher=launcher,
                    egress=None if launcher is None else lambda: docker.order.append("egress")))
                self.assertEqual(docker.order, [
                    "own-egress" if launcher is None else "egress", "retire", "migrate", "up"])

    def test_autoheal_comes_up_with_the_core(self):
        docker = FakeDocker()
        self.manager(docker).start_core(launcher="mac", egress=lambda: None)
        args, _ = docker.to[-1]
        self.assertEqual(args[-6:], ["up", "-d", "wordpress", "db", "onionheaven", "autoheal"])

    def test_retries_then_words_per_launcher(self):
        docker = FakeDocker(up_fails=2)
        self.assertTrue(self.manager(docker).start_core(launcher="mac", egress=lambda: None))
        self.assertEqual(docker.order.count("up"), 3)
        log = os.path.join(self.data, "onionpress.log")
        self.assertEqual(self.lines, [
            "WARNING: 'docker compose up -d wordpress db onionheaven autoheal' failed "
            f"(attempt 1/3). Check {log} for details.",
            "WARNING: 'docker compose up -d wordpress db onionheaven autoheal' failed "
            f"(attempt 2/3). Check {log} for details.",
            "WordPress, DB, and Tor client containers starting"])
        self.lines.clear()
        docker = FakeDocker(up_fails=3)
        self.assertFalse(self.manager(docker).start_core(launcher="linux", egress=lambda: None))
        self.assertEqual(self.lines[-2:], [
            "WARNING: docker compose up failed (attempt 3/3)",
            "ERROR: docker compose up failed after 3 attempts"])

    def test_output_goes_where_each_launcher_put_it(self):
        for launcher, where in (("mac", os.path.join(self.data, "onionpress.log")),
                                ("linux", None)):
            with self.subTest(launcher=launcher):
                docker = FakeDocker()
                self.manager(docker).start_core(launcher=launcher, egress=lambda: None)
                self.assertEqual(docker.to[-1][1], where)


class TestStartCoreStep(Base):

    def run_step(self, mac, docker, *, answers=True, config=""):
        with open(self.config, "w") as f:
            f.write(config)
        m = self.manager(docker)
        errors = start.StartErrors()
        with mock.patch.object(start, "wordpress_answers", return_value=answers) as wait, \
             mock.patch.object(start, "egress_step",
                               side_effect=lambda *a, **k: docker.order.append("egress")):
            got = start.start_core(m, mac=mac, wp_port=18080, errors=errors)
        return got, wait, errors

    def pulls(self, docker):
        return [a for a, _ in docker.to if "pull" in a]

    def test_pull_is_missing_only_before_anything_starts(self):
        docker = FakeDocker()
        self.assertEqual(self.run_step(True, docker)[0], (True, True))
        self.assertEqual(len(self.pulls(docker)), 1)
        self.assertEqual(self.pulls(docker)[0][-3:], ["pull", "--policy", "missing"])
        self.assertEqual(docker.order[:2], ["egress", "retire"])
        self.assertEqual(self.lines[0], "Pulling pre-built images (missing only)...")

    def test_never_for_a_local_stack(self):
        docker = FakeDocker()
        self.run_step(False, docker, config="ONIONPRESS_TOR_IMAGE=onionpress-tor:local\n")
        self.assertEqual(self.pulls(docker), [])
        self.assertEqual(self.lines[0], "Skipping image pull: running locally built images")

    def test_cloudflare_file_on_linux_only(self):
        for mac, with_cf in ((False, True), (True, False)):
            with self.subTest(mac=mac):
                docker = FakeDocker()
                self.run_step(mac, docker, config="CLOUDFLARE_TUNNEL_TOKEN=t\n")
                pull = self.pulls(docker)[0]
                self.assertEqual(any(a.endswith("docker-compose.cloudflare.yml") for a in pull),
                                 with_cf)

    def test_a_core_that_will_not_start_stops(self):
        docker = FakeDocker(up_fails=3)
        self.assertEqual(self.run_step(True, docker)[0], (False, False))
        self.assertNotIn("is-installed", docker.order)

    def test_not_installed_after_15_tries(self):
        docker = FakeDocker(installed=False)
        self.assertEqual(self.run_step(False, docker)[0], (True, False))
        self.assertEqual(docker.order.count("is-installed"), 15)

    def test_a_slow_wordpress_is_a_mac_warning(self):
        docker = FakeDocker()
        (ok, _), wait, _ = self.run_step(True, docker, answers=False)
        self.assertEqual(wait.call_count, 30)
        wait.assert_called_with(18080)
        self.assertIn("WARNING: WordPress did not answer on :18080 within 60s — continuing; "
                      "the install check below may misreport a slow database as a fresh site",
                      self.lines)
        self.lines.clear()
        self.run_step(False, FakeDocker(), answers=False)
        self.assertFalse([l for l in self.lines if "did not answer" in l])


class TestEgressStep(Base):

    def test_a_block_not_in_place_is_named_never_a_stop(self):
        from onionpress import wordpress_egress
        paths = resolve_paths(data_dir=self.data, app_bundle="")
        for result, named in ((wordpress_egress.Result(True, "x", "ok"), []),
                              (wordpress_egress.Result(False, "x", "no"), ["wordpress-egress"])):
            errors = start.StartErrors()
            with mock.patch.object(wordpress_egress, "ensure", return_value=result):
                start.egress_step(paths, object(), wp_port=8080, log=self.lines.append,
                                  errors=errors)
            self.assertEqual(errors.names, named)
        with mock.patch.object(wordpress_egress, "ensure", side_effect=OSError("gone")):
            start.egress_step(paths, object(), wp_port=8080, log=self.lines.append,
                              errors=None)
        self.assertIn("error: gone", self.lines[-1])


class TestLaunchers(unittest.TestCase):

    def _start(self, path):
        with open(path, encoding="utf-8") as f:
            text = f.read()
        i = text.index("\nstart_containers() {")
        return text, text[i:text.index("\n}\n", i)]

    def test_both_run_the_core_after_the_key_steps(self):
        for path in (MAC_LAUNCHER, LINUX_LAUNCHER):
            with self.subTest(path=os.path.relpath(path, ROOT)):
                text, body = self._start(path)
                self.assertLess(body.index("if ! seed_onion_key; then"),
                                body.index("start_core_services || rc=$?"))
                self.assertIn('*) return 1 ;;', body)
                fn = text[text.index("\nstart_core_services() {"):]
                fn = fn[:fn.index("\n}\n")]
                self.assertIn('start-core --docker-dir "$DOCKER_DIR"', fn.replace("\\\n        ", ""))
                self.assertIn('--wp-port "$ONIONPRESS_WP_PORT"', fn)

    def test_linux_binds_by_the_last_verdict(self):
        """Issue #464: compose reads WordPress's address from the
        environment the launcher hands the start."""
        text, _ = self._start(LINUX_LAUNCHER)
        fn = text[text.index("\nstart_core_services() {"):]
        self.assertIn('ONIONPRESS_BIND_HOST="$(wordpress_bind_host_for_start)" \\\n'
                      '        op_py --data-dir "$DATA_DIR" start-core', fn)


if __name__ == "__main__":
    unittest.main()

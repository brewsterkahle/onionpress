#!/usr/bin/env python3
"""OnionPress refuses too old a Docker or Compose with a message, not a
failed compose call (`onionpress check-docker`, src/onionpress/docker_versions.py).

The floors are tied to their evidence here: Compose 2.22.0 brought the
`pull --policy missing` both launchers run on every start; the Mac build's
pinned CLI and Compose must pass them; the Linux launcher's `start` asks
before it touches compose, and the .deb's postinst warns at install time.
"""
import io
import json
import os
import re
import sys
import unittest
from unittest import mock

PROJECT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_DIR, "src"))

from onionpress import docker_versions as dv  # noqa: E402
from onionpress.docker import DockerResult  # noqa: E402


def _read(rel):
    with open(os.path.join(PROJECT_DIR, rel), encoding="utf-8") as f:
        return f.read()


class FakeRun:
    """A docker.Docker for the probe: answers by argv, records calls; an
    Exception as the answer is raised (no binary, a timeout)."""

    def __init__(self, answers):
        self.answers = answers   # {"version --format {{.Server.Version}}": "29.1.3", ...}
        self.calls = []

    def run(self, args, timeout=30, quiet=False, **kw):
        key = " ".join(str(a) for a in args)
        self.calls.append(key)
        if key not in self.answers:
            return DockerResult(1, "", "error")
        answer = self.answers[key]
        if isinstance(answer, Exception):
            raise answer
        return DockerResult(0, answer, "")


SERVER = "version --format {{.Server.Version}}"
CLIENT = "version --format {{.Client.Version}}"
COMPOSE = "compose version --short"


class TestParse(unittest.TestCase):

    def test_versions_as_the_tools_print_them(self):
        for text, want in (("v2.40.2", (2, 40, 2)), ("29.1.3", (29, 1, 3)),
                           ("26.1.5+dfsg1", (26, 1, 5)), ("Docker Compose version v2.26.1\n", (2, 26, 1)),
                           ("20.10.24+dfsg1", (20, 10, 24)), ("2.22", (2, 22, 0))):
            with self.subTest(text):
                self.assertEqual(dv.parse_version(text), want)

    def test_nothing_is_none(self):
        for text in ("", None, "command not found", "docker: 'compose' is not a docker command"):
            with self.subTest(text):
                self.assertIsNone(dv.parse_version(text))

    def test_floors_print_short_and_findings_whole(self):
        self.assertEqual(dv.format_version(dv.MIN_ENGINE), "20.10")
        self.assertEqual(dv.format_version(dv.MIN_COMPOSE), "2.22")
        self.assertEqual(dv.format_version((29, 1, 3)), "29.1.3")
        self.assertEqual(dv.format_version((2, 40, 0)), "2.40.0")


class TestVerdict(unittest.TestCase):

    def test_new_enough(self):
        report = dv.check("29.1.3", "2.40.3")
        self.assertTrue(report.ok)
        self.assertEqual((report.engine, report.compose, report.engine_source), ("29.1.3", "2.40.3", "server"))
        self.assertEqual(dv.describe(report), ["Docker Engine 29.1.3, Docker Compose 2.40.3: OK "
                                               "(OnionPress needs Engine 20.10+ and Compose 2.22+)."])

    def test_the_floors_themselves_pass(self):
        self.assertTrue(dv.check("20.10.0", "2.22.0").ok)
        self.assertTrue(dv.check("20.10.24+dfsg1", "v2.26.1").ok, "Debian 12's engine, Debian 13's compose")

    def test_old_compose_says_what_it_found_and_how_to_upgrade(self):
        report = dv.check("29.1.3", "2.21.0")
        self.assertFalse(report.ok)
        self.assertEqual(len(report.problems), 1)
        self.assertIn("needs Docker Compose 2.22 or later; this machine has 2.21.0", report.problems[0])
        self.assertIn("docker-compose-v2", report.problems[0])
        self.assertIn("docker-compose-plugin", report.problems[0])

    def test_no_docker_compose_names_the_v1_package(self):
        report = dv.check("29.1.3", None)
        self.assertFalse(report.ok)
        self.assertIn("`docker compose` (Compose v2, a Docker CLI plugin) is not available", report.problems[0])
        self.assertIn("Compose v1", report.problems[0])
        self.assertEqual(report.compose, "")

    def test_old_engine(self):
        report = dv.check("19.03.15", "2.40.3")
        self.assertEqual(len(report.problems), 1)
        self.assertIn("needs Docker Engine 20.10 or later; this machine has 19.3.15", report.problems[0])

    def test_client_version_is_marked_as_such(self):
        report = dv.check("19.03.15", "2.40.3", engine_source="client")
        self.assertIn("the client's version; the daemon did not answer", report.problems[0])
        self.assertEqual(report.engine_source, "client")

    def test_both_wrong_lists_both(self):
        report = dv.check("19.03", "1.29.2")
        self.assertEqual(len(report.problems), 2)
        self.assertEqual(dv.describe(report), report.problems)

    def test_as_dict_carries_the_floors(self):
        d = dv.check("29.1.3", "2.40.3").as_dict()
        self.assertEqual((d["ok"], d["min_engine"], d["min_compose"]), (True, "20.10", "2.22"))


class TestProbeAndCommand(unittest.TestCase):

    def test_the_server_version_is_preferred(self):
        run = FakeRun({SERVER: "29.1.3\n", CLIENT: "29.1.3\n", COMPOSE: "v2.40.3\n"})
        report = dv.probe(run)
        self.assertTrue(report.ok)
        self.assertEqual(report.engine_source, "server")
        self.assertNotIn(CLIENT, run.calls)

    def test_without_a_daemon_the_clients_version_stands_in(self):
        run = FakeRun({CLIENT: "26.1.5+dfsg1\n", COMPOSE: "2.26.1\n"})
        report = dv.probe(run)
        self.assertTrue(report.ok)
        self.assertEqual((report.engine, report.engine_source), ("26.1.5", "client"))

    def test_no_docker_at_all_is_exit_2(self):
        out = io.StringIO()
        rc = dv.check_command(FakeRun({SERVER: OSError("no docker")}), json_output=True, out=out)
        self.assertEqual(rc, 2)
        result = json.loads(out.getvalue())
        self.assertFalse(result["ok"])
        self.assertEqual(result["engine"], "")
        self.assertEqual(len(result["problems"]), 2, "docker silent and no compose")

    def test_too_old_is_exit_1_with_the_message(self):
        out = io.StringIO()
        rc = dv.check_command(FakeRun({SERVER: "29.1.3", COMPOSE: "1.29.2"}), out=out)
        self.assertEqual(rc, 1)
        self.assertIn("needs Docker Compose 2.22 or later; this machine has 1.29.2", out.getvalue())

    def test_ok_is_exit_0_and_one_line(self):
        out = io.StringIO()
        rc = dv.check_command(FakeRun({SERVER: "29.1.3", COMPOSE: "2.40.3"}), out=out)
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue().count("\n"), 1)
        self.assertIn(": OK", out.getvalue())

    def test_json_from_the_cli(self):
        out = io.StringIO()
        rc = dv.check_command(FakeRun({SERVER: "29.1.3", COMPOSE: "2.40.3"}), json_output=True, out=out)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())["compose"], "2.40.3")


class TestTheFloorsAreEvidence(unittest.TestCase):

    def test_compose_floor_is_the_pull_policy_release(self):
        # `docker compose pull --policy` arrived in Compose v2.22.0; both
        # launchers run it on every start (CLAUDE.md, "Container Image
        # Pins"). A floor below it would let a start fail at that call.
        self.assertEqual(dv.MIN_COMPOSE, (2, 22, 0))
        # Both launchers' start runs it through start.start_core (#442.6).
        self.assertIn('["pull", "--policy", "missing"]', _read("src/onionpress/start.py"))

    def test_engine_floor_is_rootless_ga(self):
        self.assertEqual(dv.MIN_ENGINE, (20, 10, 0))

    def test_the_mac_build_pins_pass_the_floors(self):
        build = _read("build/build-dmg-simple.sh")
        cli = re.search(r'^DOCKER_VERSION="([^"]+)"', build, re.MULTILINE)
        compose = re.search(r'^DOCKER_COMPOSE_VERSION="([^"]+)"', build, re.MULTILINE)
        self.assertIsNotNone(cli)
        self.assertIsNotNone(compose)
        self.assertGreaterEqual(dv.parse_version(cli.group(1)), dv.MIN_ENGINE)
        self.assertGreaterEqual(dv.parse_version(compose.group(1)), dv.MIN_COMPOSE)

    def test_the_readme_names_the_floors(self):
        readme = _read("README.md")
        self.assertIn(f"Docker Engine {dv.format_version(dv.MIN_ENGINE)} or later", readme)
        self.assertIn(f"Docker Compose {dv.format_version(dv.MIN_COMPOSE)} or later", readme)
        self.assertIn("onionpress check-docker", readme)


class TestWhoAsks(unittest.TestCase):

    def test_the_linux_start_refuses_before_touching_compose(self):
        text = _read("linux/onionpress")
        start = text.index("        start)")
        check = text.index("op_py check-docker", start)
        first_compose = text.index("start_containers", start)
        self.assertLess(check, first_compose, "the check comes before the first compose call")
        self.assertIn('if ! _docker_check=$(op_py check-docker 2>&1); then', text)
        self.assertIn("exit 1", text[check:first_compose])
        self.assertNotIn("docker compose version >/dev/null", text[start:first_compose],
                         "the bare 'is compose there' probe is gone with it")

    def test_the_postinst_warns_on_exit_1_only_and_never_fails(self):
        from test_deb_upgrade_restarts import BUILD, _code, _heredoc
        postinst = _code(_heredoc(_read(BUILD), "POSTINST"))
        fn = re.search(r"warn_old_docker\(\) \{\n(.*?)\n\}", postinst, re.DOTALL)
        self.assertIsNotNone(fn)
        body = fn.group(1)
        self.assertIn("/opt/onionpress/lib check-docker --json", body)
        self.assertIn("sys.path[:0] = [sys.argv.pop(1)]", body,
                      "python3 -I ignores PYTHONPATH: the path goes in through argv")
        self.assertNotIn("PYTHONPATH=", body)
        self.assertIn('select(.ok == false and .engine != "")', body,
                      "only a verdict with docker answering is worth a warning")
        self.assertIn("return 0", body)
        self.assertLess(fn.start(), postinst.index("warn_old_docker\n"),
                        "defined before its first call (the upgrade path runs first)")
        self.assertEqual(postinst.count("    warn_old_docker\n"), 2, "first install and upgrade")

    def test_the_subcommand_is_dispatched_by_both_launchers(self):
        self.assertIn("check-docker|", _read("linux/onionpress"))
        self.assertIn("check-docker|", _read("app/MacOS/onionpress"))


if __name__ == "__main__":
    unittest.main()

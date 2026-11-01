#!/usr/bin/env python3
"""The wait after a start is onionpress.start.wait_for_services (issue
#529): both launchers' wait_for_services bash bodies (drifted: Linux wrote
the address on the not-installed early return, the Mac did not; the
healthcheck and address writes were in opposite orders) are one Python
function behind `onionpress wait-for-services`, against a fake docker."""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

from onionpress import start, wp_files  # noqa: E402
from onionpress.docker import DockerResult  # noqa: E402

SITE = "op2" + "a" * 53 + ".onion"
HC = "vipwsi6bjeychxvdo3xjvojr2yvw7ggwagekemekmeccscryuhehp2id.onion"


class FakeDocker:
    """Answers by argv prefix: {prefix: (rc, stdout)} or a callable giving
    the answer for the nth call of that prefix."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls = []
        self.docker_bin = "docker"
        self.env = {"DOCKER_HOST": "unix:///x"}
        self.counts = {}

    def _answer(self, args):
        line = " ".join(str(a) for a in args)
        self.calls.append(line)
        for prefix, answer in self.answers.items():
            if line.startswith(prefix):
                n = self.counts[prefix] = self.counts.get(prefix, 0) + 1
                rc, out = answer(n) if callable(answer) else answer
                return DockerResult(rc, out, "")
        return DockerResult(1, "", "no answer")

    def run(self, args, **_kw):
        return self._answer(args)

    def exec(self, container, command, **_kw):
        return self._answer(["exec", container, *command])

    def wp(self, *args, **_kw):
        return self._answer(["wp", *args])

    def compose(self, args, **_kw):
        return self._answer(["compose", *args])


class FakeManager:
    def __init__(self, docker, data_dir, bootstrapped=True):
        self.docker = docker
        self.paths = mock.Mock(data_dir=data_dir)
        self.bootstrapped = bootstrapped
        self.logged = []

    def _log(self, msg):
        self.logged.append(msg)

    def _is_tor_bootstrapped(self):
        return self.bootstrapped


class _Case(unittest.TestCase):

    def setUp(self):
        self.data = tempfile.mkdtemp(prefix="op-wait-")
        self.addCleanup(shutil.rmtree, self.data, True)
        self.sleeps = []
        self.puts = []

    def wait(self, docker, *, answers=(True,), installed=True, bootstrapped=True,
             errors=None, max_wait=120, put_rc=0):
        """*answers*: what localhost:8080 says per poll (the last repeats)."""
        manager = FakeManager(docker, self.data, bootstrapped)
        polls = list(answers)

        def http_get(url, timeout):
            self.assertEqual((url, timeout), ("http://localhost:18080/", 3))
            ok = polls.pop(0) if len(polls) > 1 else polls[0]
            if not ok:
                raise OSError("refused")
            return mock.Mock()

        def put_file(path, data, mode, **kw):
            self.puts.append((path, data, mode, kw))
            return mock.Mock(returncode=put_rc)

        docker.answers.setdefault("wp core is-installed", (0 if installed else 1, ""))
        with mock.patch.object(wp_files, "put_file", put_file):
            rc = start.wait_for_services(manager, wp_port=18080, errors=errors,
                                         sleep=self.sleeps.append, http_get=http_get,
                                         max_wait=max_wait)
        return rc, manager.logged


def _docker(hostname=SITE, hc=HC, reachable=0):
    return FakeDocker({
        "exec onionpress-tor cat /var/lib/tor/hidden_service/wordpress/hostname": (0, hostname + "\n"),
        "exec onionpress-tor cat /var/lib/tor/hidden_service/healthcheck/hostname": (0, hc + "\n"),
        "exec onionpress-tor wget -q -O /dev/null --timeout=5 http://wordpress:80/": (reachable, ""),
    })


class TestWaitForServices(_Case):

    def test_the_whole_path_when_everything_is_up(self):
        docker = _docker()
        rc, logged = self.wait(docker)
        self.assertEqual(rc, 0)
        self.assertEqual(logged, [
            "Waiting for services to be ready...",
            "WordPress is responding on localhost:18080",
            "Waiting for Tor to bootstrap and onion service to be reachable (est. 30-90s)...",
            f"Onion service ready: {SITE}",
            "All services are ready after 0s",
            "Onion address written to shared volume",
            f"Healthcheck address: {HC}",
        ])
        self.assertEqual(self.sleeps, [])
        self.assertEqual(self.puts, [("/var/lib/onionpress/onion_address", SITE + "\n", "644",
                                      {"docker_bin": "docker", "env": {"DOCKER_HOST": "unix:///x"}})])
        with open(os.path.join(self.data, "healthcheck-address")) as f:
            self.assertEqual(f.read(), HC + "\n")

    def test_a_site_not_installed_ends_the_wait_with_the_address_written(self):
        # The Linux body's early return (the Mac's wrote nothing then).
        rc, logged = self.wait(_docker(), installed=False)
        self.assertEqual(rc, 0)
        self.assertEqual(logged[-2:], ["WordPress not installed — setup required, skipping Tor wait",
                                       "Onion address written to shared volume"])
        self.assertEqual(len(self.puts), 1)
        self.assertFalse(os.path.exists(os.path.join(self.data, "healthcheck-address")))

    def test_not_installed_without_an_address_writes_nothing(self):
        rc, logged = self.wait(_docker(hostname=""), installed=False)
        self.assertEqual(rc, 0)
        self.assertEqual(logged[-1], "WordPress not installed — setup required, skipping Tor wait")
        self.assertEqual(self.puts, [])

    def test_wp_cli_that_cannot_be_asked_counts_as_not_installed(self):
        docker = _docker()
        docker.answers["wp core is-installed"] = (125, "")
        with mock.patch("onionpress.status_writer.wp_installed", return_value=None):
            rc, logged = self.wait(docker)
        self.assertIn("wp core is-installed could not be asked — treating as not installed", logged)
        self.assertEqual(logged[-1], "Onion address written to shared volume")

    def test_a_timeout_is_a_warning_and_the_summary_s_name_never_a_failure(self):
        errors = start.StartErrors()
        rc, logged = self.wait(_docker(reachable=1), errors=errors, max_wait=6)
        self.assertEqual(rc, 0)
        self.assertEqual(logged[-1], "WARNING: Services not fully ready after 6s (wp=true, onion=false)")
        self.assertEqual(errors.names, ["wait-for-services"])
        self.assertEqual(self.sleeps, [2, 2, 2])
        self.assertEqual(self.puts, [])

    def test_a_timeout_with_wordpress_silent(self):
        # The Tor side is checked on every poll, as the bash did, so it
        # can be ready before WordPress answers.
        rc, logged = self.wait(_docker(), answers=(False,), max_wait=4)
        self.assertEqual(logged[1], f"Onion service ready: {SITE}")
        self.assertEqual(logged[-1], "WARNING: Services not fully ready after 4s (wp=false, onion=true)")
        rc, logged = self.wait(_docker(hostname=""), answers=(False,), max_wait=4)
        self.assertEqual(logged[-1], "WARNING: Services not fully ready after 4s (wp=false, onion=false)")

    def test_wordpress_late_is_waited_for(self):
        rc, logged = self.wait(_docker(), answers=(False, False, True))
        self.assertEqual(logged[-3], "All services are ready after 4s")
        self.assertEqual(self.sleeps, [2, 2])

    def test_tor_not_bootstrapped_is_waited_for(self):
        docker = _docker()
        rc, logged = self.wait(docker, bootstrapped=False, max_wait=4)
        self.assertNotIn("exec onionpress-tor wget -q -O /dev/null --timeout=5 http://wordpress:80/",
                         docker.calls)
        self.assertEqual(logged[-1], "WARNING: Services not fully ready after 4s (wp=true, onion=false)")

    def test_a_failed_shared_write_is_a_warning(self):
        rc, logged = self.wait(_docker(), put_rc=1)
        self.assertIn("WARNING: Failed to write onion address to shared volume", logged)
        self.assertEqual(logged[-1], f"Healthcheck address: {HC}")

    def test_a_loose_healthcheck_hostname_is_neither_logged_nor_cached(self):
        rc, logged = self.wait(_docker(hc="not an address"))
        self.assertEqual(logged[-1], "Onion address written to shared volume")
        self.assertFalse(os.path.exists(os.path.join(self.data, "healthcheck-address")))

    def test_the_address_is_read_until_it_is_there(self):
        docker = _docker()
        docker.answers["exec onionpress-tor cat /var/lib/tor/hidden_service/wordpress/hostname"] = (
            lambda n: (1, "") if n == 1 else (0, SITE + "\n"))
        rc, logged = self.wait(docker)
        self.assertEqual(logged[-3], "All services are ready after 2s")
        reads = [c for c in docker.calls if c.endswith("wordpress/hostname")]
        self.assertEqual(len(reads), 3, "two polls, then the shared write's own read")


class TestHelpers(unittest.TestCase):

    def test_write_shared_onion_address_takes_only_a_whole_address(self):
        docker = _docker(hostname="x.onion")
        logged = []
        with mock.patch.object(wp_files, "put_file") as put:
            self.assertFalse(start.write_shared_onion_address(docker, logged.append))
        put.assert_not_called()

    def test_write_shared_onion_address_reports_a_docker_that_will_not_run(self):
        logged = []
        with mock.patch.object(wp_files, "put_file", side_effect=OSError("no docker")):
            self.assertFalse(start.write_shared_onion_address(_docker(), logged.append))
        self.assertEqual(logged, ["onion_address not written: no docker"])

    def test_healthcheck_address_reads_and_caches_a_whole_address_only(self):
        data = tempfile.mkdtemp(prefix="op-hc-")
        self.addCleanup(shutil.rmtree, data, True)
        self.assertEqual(start.healthcheck_address(_docker(), data), HC)
        with open(os.path.join(data, "healthcheck-address")) as f:
            self.assertEqual(f.read(), HC + "\n")
        os.remove(os.path.join(data, "healthcheck-address"))
        for loose in ("", HC + " x", "a" * 56 + ".onion\nb", HC.upper()):
            self.assertEqual(start.healthcheck_address(_docker(hc=loose), data), "", loose)
            self.assertFalse(os.path.exists(os.path.join(data, "healthcheck-address")))


class TestLaunchers(unittest.TestCase):
    """Both launchers' wait_for_services is the delegation; the bash
    helpers it used are gone."""

    ROOT = os.path.dirname(HERE)

    def _read(self, rel):
        with open(os.path.join(self.ROOT, rel), encoding="utf-8") as f:
            return f.read()

    def test_the_bash_bodies_are_delegations(self):
        for rel, runner in (("app/MacOS/onionpress", "bundled_python -m onionpress.cli"),
                            ("linux/onionpress", "op_py")):
            text = self._read(rel)
            i = text.index("\nwait_for_services() {")
            body = text[i:text.index("\n}\n", i)]
            with self.subTest(rel):
                self.assertIn(f'{runner} --data-dir "$DATA_DIR" wait-for-services', body)
                self.assertIn('--wp-port "$ONIONPRESS_WP_PORT"', body)
                self.assertIn('2>&1 | tee -a "$LOG_FILE"', body)
                self.assertTrue(body.rstrip().endswith("return 0"))
                for gone in ("get_healthcheck_address() {", "write_shared_onion_address() {",
                             "get_status() {", "ONION_ADDR"):
                    self.assertNotIn(gone, text, f"{rel}: {gone}")
                self.assertIn(f'{runner} --data-dir "$DATA_DIR" address 2>/dev/null', text)
                self.assertIn(f'{runner} --data-dir "$DATA_DIR" status\n', text)

    def test_the_mac_names_its_summary_file_linux_has_none(self):
        mac = self._read("app/MacOS/onionpress")
        i = mac.index("\nwait_for_services() {")
        body = mac[i:mac.index("\n}\n", i)]
        self.assertIn('${errors:+--errors-file "$errors"}', body)
        self.assertIn('note_start_errors "$errors"', body)
        linux = self._read("linux/onionpress")
        i = linux.index("\nwait_for_services() {")
        self.assertNotIn("--errors-file", linux[i:linux.index("\n}\n", i)])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""The Settings dialog's hub check is onionheaven.validate_hub_address
(issue #529): both launchers' _validate_oh_address bash bodies (the only
logic there with no Python counterpart: the :8083/status probe,
accepting_registrations, the port-80 fallback; the Mac checked the key
folder first for "self") are one function behind
`onionpress validate-oh-address`, against a fake docker."""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

from onionpress import cli, onionheaven  # noqa: E402
from onionpress.docker import DockerResult  # noqa: E402

VALID = "abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrstuvwx.onion"
OWN = "op2" + "a" * 53 + ".onion"
CURL = "curl -s --max-time 30 --socks5-hostname 127.0.0.1:9050"


class FakeDocker:
    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls = []

    def exec(self, container, command, **_kw):
        line = f"{container}: " + " ".join(command)
        self.calls.append(line)
        for prefix, (rc, out) in self.answers.items():
            if line.startswith(prefix):
                return DockerResult(rc, out, "")
        return DockerResult(1, "", "")


def _docker(status=None, status_rc=0, http_code=None, own=OWN):
    answers = {}
    if own is not None:
        answers["onionpress-tor: cat /var/lib/tor/hidden_service/wordpress/hostname"] = (0, own + "\n")
    if status is not None:
        answers[f"onionheaven: {CURL} http://{VALID}:8083/status"] = (status_rc, status)
    if http_code is not None:
        answers[f"onionheaven: {CURL} -o /dev/null -w %{{http_code}} http://{VALID}/"] = (0, http_code)
    return FakeDocker(answers)


class TestValidateHubAddress(unittest.TestCase):

    def setUp(self):
        self.data = tempfile.mkdtemp(prefix="op-oh-")
        self.addCleanup(shutil.rmtree, self.data, True)

    def check(self, docker, address=VALID):
        return onionheaven.validate_hub_address(address, docker, self.data)

    def test_only_a_whole_onion_address_gets_past_the_format_check(self):
        bad = ("evil.example.com/" + "a" * 39 + ".onion", "x" * 55 + "\n.onion",
               "abc\n" + VALID[4:], VALID.upper(), "", VALID + "/")
        self.assertEqual(len(bad[0]), 62)
        for addr in bad:
            with self.subTest(addr=addr):
                docker = _docker()
                answer = self.check(docker, addr)
                self.assertEqual((answer["valid"], answer["status"]), (False, "invalid_format"))
                self.assertEqual(docker.calls, [], "nothing is probed for a malformed address")

    def test_this_installs_own_address_is_refused(self):
        docker = _docker(own=VALID)
        answer = self.check(docker)
        self.assertEqual((answer["valid"], answer["status"]), (False, "self"))
        self.assertIn("your own onion address", answer["message"])
        self.assertEqual(len(docker.calls), 1, "no probe")

    def test_the_key_folder_says_self_before_the_served_address(self):
        # The Mac's check: an imported key not yet served.
        os.makedirs(os.path.join(self.data, "shared", "vanity-keys", VALID))
        docker = _docker(own=None)
        self.assertEqual(self.check(docker)["status"], "self")
        self.assertEqual(docker.calls, [])

    def test_a_hub(self):
        answer = self.check(_docker(status=json.dumps({"version": "2.6.1", "accepting_registrations": True})))
        self.assertEqual(answer, {"valid": True, "status": "hub", "message": "Valid OnionHeaven hub."})
        # An image from before the gate omits the field: it took them all.
        self.assertEqual(self.check(_docker(status=json.dumps({"version": "2.5.0"})))["status"], "hub")

    def test_a_node_that_refuses_registrations_is_a_site(self):
        answer = self.check(_docker(status=json.dumps({"version": "2.6.1", "accepting_registrations": False})))
        self.assertEqual((answer["valid"], answer["status"]), (True, "site"))
        self.assertIn("refuse registrations", answer["message"])

    def test_a_reachable_site_without_the_api(self):
        for code in ("200", "301", "302"):
            with self.subTest(code=code):
                docker = _docker(status="", status_rc=7, http_code=code)
                answer = self.check(docker)
                self.assertEqual((answer["valid"], answer["status"]), (True, "site"))
                self.assertIn("not running OnionHeaven yet", answer["message"])
                self.assertEqual(len(docker.calls), 3, docker.calls)

    def test_an_answer_that_is_not_json_or_has_no_version_falls_through(self):
        for status in ("<html>", json.dumps({"ok": True}), json.dumps([1])):
            with self.subTest(status=status):
                self.assertEqual(self.check(_docker(status=status, http_code="200"))["status"], "site")

    def test_unreachable(self):
        for code in ("000", "404", "500", ""):
            with self.subTest(code=code):
                answer = self.check(_docker(status="", status_rc=7, http_code=code))
                self.assertEqual((answer["valid"], answer["status"]), (True, "unreachable"))
                self.assertIn("Could not reach this address over Tor", answer["message"])

    def test_a_docker_that_will_not_run_is_unreachable_not_a_traceback(self):
        docker = mock.Mock()
        docker.exec.side_effect = OSError("no docker")
        answer = self.check(docker)
        self.assertEqual(answer["status"], "unreachable")

    def test_the_probes_run_in_the_onionheaven_container_over_its_tor(self):
        docker = _docker(status="", status_rc=7, http_code="000")
        self.check(docker)
        self.assertEqual(docker.calls[1:], [
            f"onionheaven: {CURL} http://{VALID}:8083/status",
            f"onionheaven: {CURL} -o /dev/null -w %{{http_code}} http://{VALID}/"])


class TestSubcommand(unittest.TestCase):

    def run_cmd(self, address, answer=None, raises=None):
        out = io.StringIO()
        data = tempfile.mkdtemp(prefix="op-oh-cmd-")
        self.addCleanup(shutil.rmtree, data, True)
        with mock.patch.object(cli, "Docker"), \
                mock.patch.object(onionheaven, "validate_hub_address",
                                  return_value=answer, side_effect=raises), \
                contextlib.redirect_stdout(out):
            rc = cli.main(["--data-dir", data, "validate-oh-address", address])
        return rc, out.getvalue()

    def test_the_json_is_one_compact_line_and_the_exit_says_valid(self):
        rc, out = self.run_cmd(VALID, {"valid": True, "status": "hub", "message": "Valid OnionHeaven hub."})
        self.assertEqual(rc, 0)
        self.assertEqual(out, '{"valid":true,"status":"hub","message":"Valid OnionHeaven hub."}\n')
        rc, out = self.run_cmd("x", {"valid": False, "status": "invalid_format", "message": "m"})
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out)["status"], "invalid_format")

    def test_a_failure_is_an_unreachable_answer_never_a_traceback(self):
        rc, out = self.run_cmd(VALID, raises=RuntimeError("boom"))
        self.assertEqual(rc, 0)
        answer = json.loads(out)
        self.assertEqual((answer["valid"], answer["status"]), (True, "unreachable"))
        self.assertIn("boom", answer["message"])


if __name__ == "__main__":
    unittest.main()

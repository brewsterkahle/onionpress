"""Tests for tests/stress/orchestrator/tor_control.py."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from onionpress.docker import DockerResult
from stress.orchestrator.tor_control import (
    TorControl, DirectTorControl, TorControlError, OnionService,
    create_tor_control,
)


def _ok(stdout="", stderr=""):
    return DockerResult(returncode=0, stdout=stdout, stderr=stderr)


def _fail(stderr="error", code=1):
    return DockerResult(returncode=code, stdout="", stderr=stderr)


class TestOnionService(unittest.TestCase):
    def test_address(self):
        svc = OnionService(service_id="op2abcdef")
        self.assertEqual(svc.address, "op2abcdef.onion")

    def test_private_key(self):
        svc = OnionService(service_id="abc", private_key="base64key==")
        self.assertEqual(svc.private_key, "base64key==")


class TestParseResponse(unittest.TestCase):
    def test_basic(self):
        lines = TorControl._parse_response("250 OK\r\n250 closing\r\n")
        self.assertEqual(lines, ["250 OK", "250 closing"])

    def test_empty(self):
        self.assertEqual(TorControl._parse_response(""), [])

    def test_mixed_newlines(self):
        lines = TorControl._parse_response("250 OK\n250 done\n")
        self.assertEqual(lines, ["250 OK", "250 done"])


class TestSendCommand(unittest.TestCase):
    def test_python_approach(self):
        docker = mock.Mock()
        docker.exec.return_value = _ok("250-version=0.4.8.0\r\n250 OK\r\n")
        tc = TorControl(docker, container="onionpress-tor")
        lines = tc.send_command("GETINFO version")
        self.assertIn("250-version=0.4.8.0", lines)

    def test_fallback_to_nc(self):
        docker = mock.Mock()
        # First call (python3) fails, second (nc) succeeds
        docker.exec.side_effect = [
            _fail("python3 not found"),
            _ok("250 OK\r\n"),
        ]
        tc = TorControl(docker, container="onionpress-tor")
        lines = tc.send_command("SIGNAL NEWNYM")
        self.assertEqual(docker.exec.call_count, 2)
        self.assertIn("250 OK", lines)

    def test_both_fail_raises(self):
        docker = mock.Mock()
        docker.exec.return_value = _fail("connection refused")
        tc = TorControl(docker, container="onionpress-tor")
        with self.assertRaises(TorControlError):
            tc.send_command("GETINFO version")


class TestGetBootstrapPhase(unittest.TestCase):
    def test_complete(self):
        docker = mock.Mock()
        docker.exec.return_value = _ok(
            '250-status/bootstrap-phase=NOTICE BOOTSTRAP PROGRESS=100 '
            'TAG=done SUMMARY="Done"\r\n250 OK\r\n'
        )
        tc = TorControl(docker)
        progress, summary = tc.get_bootstrap_phase()
        self.assertEqual(progress, 100)
        self.assertEqual(summary, "Done")

    def test_partial(self):
        docker = mock.Mock()
        docker.exec.return_value = _ok(
            '250-status/bootstrap-phase=NOTICE BOOTSTRAP PROGRESS=50 '
            'TAG=loading_descriptors SUMMARY="Loading relay descriptors"\r\n250 OK\r\n'
        )
        tc = TorControl(docker)
        progress, summary = tc.get_bootstrap_phase()
        self.assertEqual(progress, 50)
        self.assertIn("Loading", summary)

    def test_no_response(self):
        docker = mock.Mock()
        docker.exec.return_value = _ok("250 OK\r\n")
        tc = TorControl(docker)
        progress, summary = tc.get_bootstrap_phase()
        self.assertEqual(progress, 0)


class TestIsBootstrapped(unittest.TestCase):
    def test_true(self):
        docker = mock.Mock()
        docker.exec.return_value = _ok(
            '250-status/bootstrap-phase=NOTICE BOOTSTRAP PROGRESS=100 '
            'TAG=done SUMMARY="Done"\r\n250 OK\r\n'
        )
        tc = TorControl(docker)
        self.assertTrue(tc.is_bootstrapped())

    def test_false(self):
        docker = mock.Mock()
        docker.exec.return_value = _ok(
            '250-status/bootstrap-phase=NOTICE BOOTSTRAP PROGRESS=5 '
            'TAG=conn SUMMARY="Connecting"\r\n250 OK\r\n'
        )
        tc = TorControl(docker)
        self.assertFalse(tc.is_bootstrapped())

    def test_error_returns_false(self):
        docker = mock.Mock()
        docker.exec.return_value = _fail()
        tc = TorControl(docker)
        self.assertFalse(tc.is_bootstrapped())


class TestAddOnion(unittest.TestCase):
    def test_new_key(self):
        docker = mock.Mock()
        docker.exec.return_value = _ok(
            "250-ServiceID=op2abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrstuv\r\n"
            "250-PrivateKey=ED25519-V3:base64privatekey==\r\n"
            "250 OK\r\n"
        )
        tc = TorControl(docker)
        svc = tc.add_onion()
        self.assertEqual(
            svc.service_id,
            "op2abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrstuv",
        )
        self.assertEqual(svc.private_key, "base64privatekey==")

    def test_existing_key(self):
        docker = mock.Mock()
        docker.exec.return_value = _ok(
            "250-ServiceID=op2abc123\r\n250 OK\r\n"
        )
        tc = TorControl(docker)
        svc = tc.add_onion(key="mykey==", port_mapping="80,127.0.0.1:8082")
        self.assertEqual(svc.service_id, "op2abc123")
        # Verify the command included the key
        call_args = docker.exec.call_args[0]
        # The python script contains the command string
        self.assertIn("ED25519-V3:mykey==", str(call_args))

    def test_collision(self):
        docker = mock.Mock()
        docker.exec.return_value = _ok(
            "512 Onion address collision\r\n"
        )
        tc = TorControl(docker)
        with self.assertRaises(TorControlError) as ctx:
            tc.add_onion(key="somekey==")
        self.assertIn("collision", str(ctx.exception))

    def test_error_response(self):
        docker = mock.Mock()
        docker.exec.return_value = _ok("552 Unrecognized key type\r\n")
        tc = TorControl(docker)
        with self.assertRaises(TorControlError):
            tc.add_onion(key="badkey")

    def test_no_service_id(self):
        docker = mock.Mock()
        docker.exec.return_value = _ok("250 OK\r\n")
        tc = TorControl(docker)
        with self.assertRaises(TorControlError) as ctx:
            tc.add_onion()
        self.assertIn("no ServiceID", str(ctx.exception))


class TestDelOnion(unittest.TestCase):
    def test_success(self):
        docker = mock.Mock()
        docker.exec.return_value = _ok("250 OK\r\n")
        logs = []
        tc = TorControl(docker, log_func=logs.append)
        tc.del_onion("op2abc123")
        self.assertTrue(any("DEL_ONION" in l for l in logs))

    def test_strips_onion_suffix(self):
        docker = mock.Mock()
        docker.exec.return_value = _ok("250 OK\r\n")
        tc = TorControl(docker)
        tc.del_onion("op2abc123.onion")
        # Verify the command sent doesn't include .onion
        call_args = docker.exec.call_args[0]
        script_content = str(call_args)
        self.assertIn("DEL_ONION op2abc123", script_content)
        self.assertNotIn("DEL_ONION op2abc123.onion", script_content)

    def test_failure(self):
        docker = mock.Mock()
        docker.exec.return_value = _ok("552 Unknown onion address\r\n")
        tc = TorControl(docker)
        with self.assertRaises(TorControlError):
            tc.del_onion("nonexistent")


class TestSignalNewnym(unittest.TestCase):
    def test_success(self):
        docker = mock.Mock()
        docker.exec.return_value = _ok("250 OK\r\n")
        logs = []
        tc = TorControl(docker, log_func=logs.append)
        tc.signal_newnym()
        self.assertTrue(any("NEWNYM" in l for l in logs))

    def test_failure(self):
        docker = mock.Mock()
        docker.exec.return_value = _ok("552 Rate limited\r\n")
        tc = TorControl(docker)
        with self.assertRaises(TorControlError):
            tc.signal_newnym()


class TestHsfetch(unittest.TestCase):
    def test_basic(self):
        docker = mock.Mock()
        docker.exec.return_value = _ok("250 OK\r\n")
        tc = TorControl(docker)
        tc.hsfetch("op2abc123")
        # Should not raise

    def test_strips_suffix(self):
        docker = mock.Mock()
        docker.exec.return_value = _ok("250 OK\r\n")
        tc = TorControl(docker)
        tc.hsfetch("op2abc123.onion")
        call_args = str(docker.exec.call_args)
        self.assertIn("HSFETCH op2abc123", call_args)


class TestFlushDescriptorCache(unittest.TestCase):
    @mock.patch("stress.orchestrator.tor_control.time.sleep")
    def test_sends_newnym_then_hsfetch(self, mock_sleep):
        docker = mock.Mock()
        docker.exec.return_value = _ok("250 OK\r\n")
        tc = TorControl(docker)
        tc.flush_descriptor_cache(["sid1", "sid2.onion"])
        # Should have called exec 2 times: newnym (1 send_command), hsfetch batch (1 send_command)
        self.assertEqual(docker.exec.call_count, 2)
        mock_sleep.assert_called_once_with(3)


class TestGetDetachedServices(unittest.TestCase):
    def test_with_services(self):
        # Tor returns service IDs as 56-char base32 strings
        sid = "a" * 56
        docker = mock.Mock()
        docker.exec.return_value = _ok(
            f"250+onions/detached=\r\n{sid}\r\n.\r\n250 OK\r\n"
        )
        tc = TorControl(docker)
        services = tc.get_detached_services()
        self.assertEqual(services, [sid])

    def test_empty(self):
        docker = mock.Mock()
        docker.exec.return_value = _ok("250+onions/detached=\r\n.\r\n250 OK\r\n")
        tc = TorControl(docker)
        self.assertEqual(tc.get_detached_services(), [])


class TestGetVersion(unittest.TestCase):
    def test_version(self):
        docker = mock.Mock()
        docker.exec.return_value = _ok("250-version=0.4.8.12\r\n250 OK\r\n")
        tc = TorControl(docker)
        self.assertEqual(tc.get_version(), "0.4.8.12")

    def test_no_version(self):
        docker = mock.Mock()
        docker.exec.return_value = _ok("250 OK\r\n")
        tc = TorControl(docker)
        self.assertEqual(tc.get_version(), "")


class TestDirectTorControl(unittest.TestCase):
    def test_read_cookie_via_python(self):
        docker = mock.Mock()
        docker.exec.return_value = _ok("abcdef1234567890")
        dtc = DirectTorControl(docker, container="onionpress-tor")
        cookie = dtc._read_cookie()
        self.assertEqual(cookie, "abcdef1234567890")
        # Should cache
        cookie2 = dtc._read_cookie()
        self.assertEqual(cookie2, "abcdef1234567890")
        # Only one docker exec call (cached)
        self.assertEqual(docker.exec.call_count, 1)

    def test_invalidate_cookie(self):
        docker = mock.Mock()
        docker.exec.return_value = _ok("cookie1")
        dtc = DirectTorControl(docker)
        dtc._read_cookie()
        dtc.invalidate_cookie()
        self.assertIsNone(dtc._cookie_hex)

    def test_fallback_to_xxd(self):
        docker = mock.Mock()
        docker.exec.side_effect = [
            _fail("python3 not found"),  # python3 attempt
            _ok("abcdef"),               # xxd fallback
        ]
        dtc = DirectTorControl(docker)
        cookie = dtc._read_cookie()
        self.assertEqual(cookie, "abcdef")

    def test_cookie_read_failure_raises(self):
        docker = mock.Mock()
        docker.exec.return_value = _fail()
        dtc = DirectTorControl(docker)
        with self.assertRaises(TorControlError):
            dtc._read_cookie()

    @mock.patch("stress.orchestrator.tor_control.socket.create_connection")
    def test_direct_send_command(self, mock_conn):
        docker = mock.Mock()
        docker.exec.return_value = _ok("cookiehex")
        mock_sock = mock.Mock()
        mock_sock.recv.side_effect = [b"250 OK\r\n", b""]
        mock_conn.return_value = mock_sock

        dtc = DirectTorControl(docker, host="127.0.0.1", control_port=9051)
        lines = dtc.send_command("GETINFO version")
        self.assertIn("250 OK", lines)
        mock_sock.sendall.assert_called_once()

    @mock.patch("stress.orchestrator.tor_control.socket.create_connection", side_effect=OSError("refused"))
    def test_direct_falls_back_to_exec(self, mock_conn):
        docker = mock.Mock()
        # First exec: cookie read succeeds
        # Second exec (fallback send_command): returns result
        docker.exec.side_effect = [
            _ok("cookiehex"),                     # _read_cookie
            _ok("250 OK\r\n"),                    # fallback python3 send
        ]
        dtc = DirectTorControl(docker)
        lines = dtc.send_command("SIGNAL NEWNYM")
        self.assertIn("250 OK", lines)


class TestCreateTorControl(unittest.TestCase):
    @mock.patch("stress.orchestrator.tor_control.socket.create_connection")
    def test_direct_when_port_reachable(self, mock_conn):
        mock_conn.return_value = mock.Mock()
        docker = mock.Mock()
        tc = create_tor_control(docker, prefer_direct=True)
        self.assertIsInstance(tc, DirectTorControl)

    @mock.patch("stress.orchestrator.tor_control.socket.create_connection", side_effect=OSError("refused"))
    def test_exec_when_port_not_reachable(self, mock_conn):
        docker = mock.Mock()
        tc = create_tor_control(docker, prefer_direct=True)
        self.assertIsInstance(tc, TorControl)
        self.assertNotIsInstance(tc, DirectTorControl)

    def test_exec_when_prefer_direct_false(self):
        docker = mock.Mock()
        tc = create_tor_control(docker, prefer_direct=False)
        self.assertIsInstance(tc, TorControl)
        self.assertNotIsInstance(tc, DirectTorControl)


if __name__ == "__main__":
    unittest.main()

"""Tests for src/onionpress/cli.py."""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from onionpress.cli import main, OnionPressCLI, _make_log_func
from onionpress.onionnames_registrar import RegistrarResult


class TestMakeLogFunc(unittest.TestCase):
    def test_writes_to_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            path = f.name
        try:
            log = _make_log_func(path)
            log("test message")
            with open(path) as f:
                content = f.read()
            self.assertIn("test message", content)
        finally:
            os.unlink(path)

    def test_no_file(self):
        log = _make_log_func(None)
        # Should not crash
        log("test message")


class TestCLIArgParsing(unittest.TestCase):
    """Test that argparse handles commands correctly."""

    @mock.patch("onionpress.cli.OnionPressCLI")
    def test_version(self, MockCLI):
        with self.assertRaises(SystemExit) as ctx:
            main(["--version"])
        self.assertEqual(ctx.exception.code, 0)

    @mock.patch("onionpress.cli.OnionPressCLI")
    def test_status_command(self, MockCLI):
        instance = MockCLI.return_value
        instance.cmd_status.return_value = 0
        result = main(["status"])
        self.assertEqual(result, 0)
        instance.cmd_status.assert_called_once()

    @mock.patch("onionpress.cli.OnionPressCLI")
    def test_stop_command(self, MockCLI):
        instance = MockCLI.return_value
        instance.cmd_stop.return_value = 0
        result = main(["stop"])
        self.assertEqual(result, 0)

    @mock.patch("onionpress.cli.OnionPressCLI")
    def test_address_command(self, MockCLI):
        instance = MockCLI.return_value
        instance.cmd_address.return_value = 0
        result = main(["address"])
        self.assertEqual(result, 0)

    @mock.patch("onionpress.cli.OnionPressCLI")
    def test_backup_command(self, MockCLI):
        instance = MockCLI.return_value
        instance.cmd_backup.return_value = 0
        result = main(["backup", "mypass"])
        self.assertEqual(result, 0)
        # cli.py dispatches backup as cmd_backup(password, output, user) —
        # output and --user both default to None when omitted.
        instance.cmd_backup.assert_called_once_with("mypass", None, None)

    @mock.patch("onionpress.cli.OnionPressCLI")
    def test_backup_with_output(self, MockCLI):
        instance = MockCLI.return_value
        instance.cmd_backup.return_value = 0
        result = main(["backup", "mypass", "/tmp/backup.zip"])
        instance.cmd_backup.assert_called_once_with("mypass", "/tmp/backup.zip", None)

    @mock.patch("onionpress.cli.OnionPressCLI")
    def test_restore_command(self, MockCLI):
        instance = MockCLI.return_value
        instance.cmd_restore.return_value = 0
        result = main(["restore", "mypass", "/tmp/backup.zip"])
        instance.cmd_restore.assert_called_once_with("mypass", "/tmp/backup.zip")

    @mock.patch("onionpress.cli.OnionPressCLI")
    def test_reset_with_yes(self, MockCLI):
        instance = MockCLI.return_value
        instance.cmd_reset.return_value = 0
        result = main(["reset", "--yes"])
        instance.cmd_reset.assert_called_once_with(yes=True)

    @mock.patch("onionpress.cli.OnionPressCLI")
    def test_check_for_update_default(self, MockCLI):
        instance = MockCLI.return_value
        instance.cmd_check_for_update.return_value = 0
        result = main(["check-for-update"])
        self.assertEqual(result, 0)
        instance.cmd_check_for_update.assert_called_once_with(
            json_output=False, current=None)

    @mock.patch("onionpress.cli.OnionPressCLI")
    def test_check_for_update_json(self, MockCLI):
        instance = MockCLI.return_value
        instance.cmd_check_for_update.return_value = 0
        result = main(["check-for-update", "--json", "--current", "1.2.3"])
        self.assertEqual(result, 0)
        instance.cmd_check_for_update.assert_called_once_with(
            json_output=True, current="1.2.3")

    @mock.patch("onionpress.cli.OnionPressCLI")
    def test_default_is_start(self, MockCLI):
        instance = MockCLI.return_value
        instance.cmd_start.return_value = 0
        result = main([])
        instance.cmd_start.assert_called_once()

    @mock.patch("onionpress.cli.OnionPressCLI")
    def test_data_dir_override(self, MockCLI):
        instance = MockCLI.return_value
        instance.cmd_status.return_value = 0
        main(["--data-dir", "/tmp/test", "status"])
        MockCLI.assert_called_once_with(data_dir="/tmp/test")


class TestOnionnameArgParsing(unittest.TestCase):
    """main() routes `onionname <sub>` to the right cmd_onionname_* method."""

    @mock.patch("onionpress.cli.OnionPressCLI")
    def test_suggest(self, MockCLI):
        instance = MockCLI.return_value
        instance.cmd_onionname_suggest.return_value = 0
        self.assertEqual(main(["onionname", "suggest"]), 0)
        instance.cmd_onionname_suggest.assert_called_once_with()

    @mock.patch("onionpress.cli.OnionPressCLI")
    def test_check(self, MockCLI):
        instance = MockCLI.return_value
        instance.cmd_onionname_check.return_value = 0
        self.assertEqual(main(["onionname", "check", "brewsterkahle"]), 0)
        instance.cmd_onionname_check.assert_called_once_with("brewsterkahle")

    @mock.patch("onionpress.cli.OnionPressCLI")
    def test_register(self, MockCLI):
        instance = MockCLI.return_value
        instance.cmd_onionname_register.return_value = 0
        self.assertEqual(main(["onionname", "register", "brewsterkahle"]), 0)
        instance.cmd_onionname_register.assert_called_once_with("brewsterkahle")

    @mock.patch("onionpress.cli.OnionPressCLI")
    def test_bare_onionname_is_error(self, MockCLI):
        # No subcommand → help + non-zero (a driving app always passes one).
        self.assertEqual(main(["onionname"]), 1)


class TestOnionnameCommands(unittest.TestCase):
    """The JSON shapes an external app parses from stdout, with the
    Registrar mocked."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._patches = [
            mock.patch("onionpress.cli.detect_port_offset"),
            mock.patch("onionpress.cli.ensure_secrets"),
            mock.patch("onionpress.cli.Docker"),
        ]
        mock_ports, mock_secrets, MockDocker = (p.start() for p in self._patches)
        from onionpress.config import PortConfig, Secrets
        mock_ports.return_value = PortConfig(0, 8080, 9050, 9077)
        mock_secrets.return_value = Secrets("p1", "p2", "p3")
        MockDocker.return_value = mock.Mock()
        self.cli = OnionPressCLI(data_dir=self.tmpdir)
        # Silence stderr logging noise during the tests.
        self.cli.log = lambda *a, **k: None

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @staticmethod
    def _run(fn, *args):
        """Call a cmd_* fn, capturing the single JSON line it prints."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = fn(*args)
        out = buf.getvalue().strip()
        # Exactly one JSON line on stdout — the contract callers rely on.
        assert "\n" not in out, f"expected one line, got: {out!r}"
        return rc, json.loads(out)

    def _stub_registrar(self, **methods):
        stub = mock.Mock()
        for name, result in methods.items():
            getattr(stub, name).return_value = result
        return mock.patch.object(self.cli, "_registrar", return_value=stub), stub

    # ── suggest ──────────────────────────────────────────────────────────
    def test_suggest_from_registry(self):
        patch, _ = self._stub_registrar(
            suggest=RegistrarResult(status="ok", body={"onionname": "happy-otter"}))
        with patch:
            rc, out = self._run(self.cli.cmd_onionname_suggest)
        self.assertEqual(rc, 0)
        self.assertEqual(out, {"name": "happy-otter"})

    def test_suggest_falls_back_to_local_when_unreachable(self):
        patch, _ = self._stub_registrar(
            suggest=RegistrarResult(status="unreachable", reason="timeout"))
        with patch, mock.patch(
                "onionpress.onionnames_client.suggest_name_local",
                return_value="local-fallback"):
            rc, out = self._run(self.cli.cmd_onionname_suggest)
        self.assertEqual(out, {"name": "local-fallback"})

    # ── check ────────────────────────────────────────────────────────────
    def test_check_rejects_invalid_locally_without_registry(self):
        patch, stub = self._stub_registrar()
        with patch:
            rc, out = self._run(self.cli.cmd_onionname_check, "abc")  # too short
        self.assertEqual(out,
                         {"available": False, "reason": "too_short",
                          "suggestions": []})
        stub.check.assert_not_called()

    def test_check_available(self):
        patch, _ = self._stub_registrar(
            check=RegistrarResult(status="ok",
                                  body={"available": True, "reason": None}))
        with patch:
            rc, out = self._run(self.cli.cmd_onionname_check, "happy-otter")
        self.assertEqual(out,
                         {"available": True, "reason": "", "suggestions": []})

    def test_check_taken_with_suggestions(self):
        patch, _ = self._stub_registrar(
            check=RegistrarResult(
                status="ok",
                body={"available": False, "reason": "taken",
                      "suggestions": ["happy-otter2", "happy-otter3"]}))
        with patch:
            rc, out = self._run(self.cli.cmd_onionname_check, "happy-otter")
        self.assertEqual(out, {
            "available": False, "reason": "taken",
            "suggestions": ["happy-otter2", "happy-otter3"]})

    def test_check_registry_unreachable(self):
        patch, _ = self._stub_registrar(
            check=RegistrarResult(status="unreachable", reason="exec_failed"))
        with patch:
            rc, out = self._run(self.cli.cmd_onionname_check, "happy-otter")
        self.assertEqual(out, {
            "available": False, "reason": "exec_failed", "suggestions": []})

    # ── register ─────────────────────────────────────────────────────────
    def test_register_rejects_invalid_locally(self):
        patch, stub = self._stub_registrar()
        with patch:
            rc, out = self._run(self.cli.cmd_onionname_register, "12345")
        self.assertEqual(out,
                         {"ok": False, "error": "all_numeric",
                          "suggestions": []})
        stub.register.assert_not_called()

    def test_register_no_onion_address(self):
        patch, _ = self._stub_registrar(register=RegistrarResult(status="ok"))
        with patch, mock.patch.object(
                self.cli.containers, "get_onion_address", return_value=""):
            rc, out = self._run(self.cli.cmd_onionname_register, "happy-otter")
        self.assertEqual(out,
                         {"ok": False, "error": "no_onion_address",
                          "suggestions": []})

    def test_register_success_resolves_root_url(self):
        patch, _ = self._stub_registrar(register=RegistrarResult(status="ok"))
        with patch, mock.patch.object(
                self.cli.containers, "get_onion_address",
                return_value="op2abcdef.onion"):
            rc, out = self._run(self.cli.cmd_onionname_register, "happy-otter")
        self.assertEqual(out, {
            "ok": True, "name": "happy-otter",
            "address": "op2abcdef.onion",
            "url": "http://op2abcdef.onion/",
        })

    def test_register_collision_returns_suggestions(self):
        patch, _ = self._stub_registrar(
            register=RegistrarResult(status="collision", reason="taken",
                                     suggestions=["happy-otter2"]))
        with patch, mock.patch.object(
                self.cli.containers, "get_onion_address",
                return_value="op2abcdef.onion"):
            rc, out = self._run(self.cli.cmd_onionname_register, "happy-otter")
        self.assertEqual(out, {
            "ok": False, "error": "taken", "suggestions": ["happy-otter2"]})


class TestPIDLock(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @mock.patch("onionpress.cli.detect_port_offset")
    @mock.patch("onionpress.cli.ensure_secrets")
    @mock.patch("onionpress.cli.Docker")
    def test_pid_lock_lifecycle(self, MockDocker, mock_secrets, mock_ports):
        from onionpress.config import PortConfig, Secrets
        mock_ports.return_value = PortConfig(0, 8080, 9050, 9077)
        mock_secrets.return_value = Secrets("p1", "p2", "p3")
        MockDocker.return_value = mock.Mock()

        cli = OnionPressCLI(data_dir=self.tmpdir)

        # No lock initially
        self.assertFalse(cli._check_pid_lock())

        # Write lock
        cli._write_pid_lock()
        pid_file = os.path.join(self.tmpdir, "onionpress.pid")
        self.assertTrue(os.path.exists(pid_file))
        with open(pid_file) as f:
            self.assertEqual(int(f.read().strip()), os.getpid())

        # Lock detected (our own PID is running)
        self.assertTrue(cli._check_pid_lock())

        # Remove lock
        cli._remove_pid_lock()
        self.assertFalse(os.path.exists(pid_file))

    @mock.patch("onionpress.cli.detect_port_offset")
    @mock.patch("onionpress.cli.ensure_secrets")
    @mock.patch("onionpress.cli.Docker")
    def test_stale_pid_lock(self, MockDocker, mock_secrets, mock_ports):
        from onionpress.config import PortConfig, Secrets
        mock_ports.return_value = PortConfig(0, 8080, 9050, 9077)
        mock_secrets.return_value = Secrets("p1", "p2", "p3")
        MockDocker.return_value = mock.Mock()

        cli = OnionPressCLI(data_dir=self.tmpdir)
        pid_file = os.path.join(self.tmpdir, "onionpress.pid")

        # Write a stale PID (very unlikely to be a real process)
        with open(pid_file, "w") as f:
            f.write("999999999")

        # Should detect as stale and clean up
        self.assertFalse(cli._check_pid_lock())
        self.assertFalse(os.path.exists(pid_file))


if __name__ == "__main__":
    unittest.main()

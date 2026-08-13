"""Tests for src/onionpress/cli.py."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from onionpress.cli import main, OnionPressCLI, _make_log_func


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


class TestCmdProvisionStatic(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.docs_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.docs_dir, ignore_errors=True)

    @mock.patch("onionpress.cli.detect_port_offset")
    @mock.patch("onionpress.cli.ensure_secrets")
    @mock.patch("onionpress.cli.Docker")
    def test_creates_site_dir_and_persists_onionname(self, MockDocker, mock_secrets, mock_ports):
        from onionpress.config import PortConfig, Secrets
        import dataclasses
        mock_ports.return_value = PortConfig(0, 8080, 9050, 9077)
        mock_secrets.return_value = Secrets("p1", "p2", "p3")
        MockDocker.return_value = mock.Mock()
        cli = OnionPressCLI(data_dir=self.tmpdir)
        cli.paths = dataclasses.replace(cli.paths, documents_dir=self.docs_dir)

        self.assertEqual(cli.cmd_provision_static("alice"), 0)
        self.assertTrue(os.path.isfile(
            os.path.join(self.docs_dir, "Site", "index.html")))
        with open(os.path.join(self.tmpdir, "config")) as f:
            contents = f.read()
        self.assertIn("SITE_TYPE=static", contents)
        self.assertIn("ONIONNAME=alice", contents)


if __name__ == "__main__":
    unittest.main()

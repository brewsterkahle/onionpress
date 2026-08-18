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
    def test_publish_command(self, MockCLI):
        instance = MockCLI.return_value
        instance.cmd_publish.return_value = 0
        result = main(["publish", "/tmp/mysite"])
        self.assertEqual(result, 0)
        instance.cmd_publish.assert_called_once_with("/tmp/mysite")

    @mock.patch("onionpress.cli.OnionPressCLI")
    def test_follow_add_command(self, MockCLI):
        instance = MockCLI.return_value
        instance.cmd_follow_add.return_value = 0
        result = main(["follow", "add", "http://abc.onion/feed/", "--name", "ABC"])
        self.assertEqual(result, 0)
        instance.cmd_follow_add.assert_called_once_with("http://abc.onion/feed/", "ABC")

    @mock.patch("onionpress.cli.OnionPressCLI")
    def test_follow_add_command_no_name(self, MockCLI):
        instance = MockCLI.return_value
        instance.cmd_follow_add.return_value = 0
        main(["follow", "add", "http://abc.onion/feed/"])
        instance.cmd_follow_add.assert_called_once_with("http://abc.onion/feed/", None)

    @mock.patch("onionpress.cli.OnionPressCLI")
    def test_follow_remove_command(self, MockCLI):
        instance = MockCLI.return_value
        instance.cmd_follow_remove.return_value = 0
        result = main(["follow", "remove", "abc"])
        self.assertEqual(result, 0)
        instance.cmd_follow_remove.assert_called_once_with("abc")

    @mock.patch("onionpress.cli.OnionPressCLI")
    def test_follow_list_command(self, MockCLI):
        instance = MockCLI.return_value
        instance.cmd_follow_list.return_value = 0
        result = main(["follow", "list"])
        self.assertEqual(result, 0)
        instance.cmd_follow_list.assert_called_once()

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


class TestCmdBackupStaticSite(unittest.TestCase):
    """cmd_backup(): static installs skip verify_wp_admin entirely (no WP
    admin account to check) and back up under username="site"."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

    @mock.patch("onionpress.cli.detect_port_offset")
    @mock.patch("onionpress.cli.ensure_secrets")
    @mock.patch("onionpress.cli.Docker")
    def _make_cli(self, MockDocker, mock_secrets, mock_ports, site_type):
        from onionpress.config import PortConfig, Secrets, write_value
        mock_ports.return_value = PortConfig(0, 8080, 9050, 9077)
        mock_secrets.return_value = Secrets("p1", "p2", "p3")
        MockDocker.return_value = mock.Mock()
        cli = OnionPressCLI(data_dir=self.tmpdir)
        write_value(cli.paths.config_file, "SITE_TYPE", site_type)
        cli.containers = mock.Mock()
        cli.containers.get_onion_address.return_value = "abc.onion"
        return cli

    def test_static_skips_admin_verification(self):
        cli = self._make_cli(site_type="static")
        output = os.path.join(self.tmpdir, "out.zip")
        with mock.patch("onionpress.backup.verify_wp_admin") as m_verify, \
             mock.patch("onionpress.backup.create_backup") as m_create:
            result = cli.cmd_backup("mypassword", output)
        m_verify.assert_not_called()
        self.assertEqual(result, 0)
        self.assertEqual(m_create.call_args.kwargs["username"], "site")
        self.assertEqual(m_create.call_args.kwargs["site_type"], "static")

    def test_wordpress_still_requires_admin_verification(self):
        cli = self._make_cli(site_type="wordpress")
        output = os.path.join(self.tmpdir, "out.zip")
        with mock.patch("onionpress.backup.verify_wp_admin",
                         return_value=(False, "nope")) as m_verify, \
             mock.patch("onionpress.backup.get_admin_username",
                         return_value="alice"), \
             mock.patch("onionpress.backup.create_backup") as m_create:
            result = cli.cmd_backup("mypassword", output)
        m_verify.assert_called_once_with("alice", "mypassword")
        self.assertEqual(result, 1)
        m_create.assert_not_called()


class TestCmdPublish(unittest.TestCase):
    """cmd_publish: static-only, atomic rsync+swap into ~/OnionPress/Site/."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.docs_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.docs_dir, ignore_errors=True)

    @mock.patch("onionpress.cli.detect_port_offset")
    @mock.patch("onionpress.cli.ensure_secrets")
    @mock.patch("onionpress.cli.Docker")
    def _make_cli(self, MockDocker, mock_secrets, mock_ports, site_type="static"):
        from onionpress.config import PortConfig, Secrets, write_value
        import dataclasses
        mock_ports.return_value = PortConfig(0, 8080, 9050, 9077)
        mock_secrets.return_value = Secrets("p1", "p2", "p3")
        MockDocker.return_value = mock.Mock()
        cli = OnionPressCLI(data_dir=self.tmpdir)
        cli.paths = dataclasses.replace(cli.paths, documents_dir=self.docs_dir)
        write_value(cli.paths.config_file, "SITE_TYPE", site_type)
        return cli

    def _source_dir(self, files):
        src = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, src, ignore_errors=True)
        for name, content in files.items():
            path = os.path.join(src, name)
            os.makedirs(os.path.dirname(path) or src, exist_ok=True)
            with open(path, "w") as f:
                f.write(content)
        return src

    def test_refuses_when_not_static(self):
        cli = self._make_cli(site_type="wordpress")
        src = self._source_dir({"index.html": "hi"})
        self.assertEqual(cli.cmd_publish(src), 1)
        self.assertFalse(os.path.exists(os.path.join(self.docs_dir, "Site")))

    def test_refuses_missing_source_dir(self):
        cli = self._make_cli()
        self.assertEqual(cli.cmd_publish("/nonexistent/does/not/exist"), 1)

    def test_refuses_empty_source_dir(self):
        cli = self._make_cli()
        src = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, src, ignore_errors=True)
        self.assertEqual(cli.cmd_publish(src), 1)
        self.assertFalse(os.path.exists(os.path.join(self.docs_dir, "Site")))

    def test_publishes_into_site_dir(self):
        cli = self._make_cli()
        src = self._source_dir({"index.html": "<h1>hi</h1>", "sub/page.html": "sub"})
        self.assertEqual(cli.cmd_publish(src), 0)
        site_dir = os.path.join(self.docs_dir, "Site")
        with open(os.path.join(site_dir, "index.html")) as f:
            self.assertEqual(f.read(), "<h1>hi</h1>")
        with open(os.path.join(site_dir, "sub", "page.html")) as f:
            self.assertEqual(f.read(), "sub")
        # No leftover staging/previous dirs.
        self.assertFalse(os.path.exists(os.path.join(self.docs_dir, ".Site.staging")))
        self.assertFalse(os.path.exists(os.path.join(self.docs_dir, ".Site.previous")))

    def test_publish_preserves_site_dir_inode(self):
        # The running containers bind-mount Site/, which pins its inode —
        # a rename-and-recreate swap would leave nginx serving the orphaned
        # old directory (guaranteed 404s until container recreation). The
        # sync must happen IN PLACE.
        cli = self._make_cli()
        src1 = self._source_dir({"index.html": "v1"})
        self.assertEqual(cli.cmd_publish(src1), 0)
        site_dir = os.path.join(self.docs_dir, "Site")
        inode_before = os.stat(site_dir).st_ino

        src2 = self._source_dir({"index.html": "v2"})
        self.assertEqual(cli.cmd_publish(src2), 0)
        self.assertEqual(os.stat(site_dir).st_ino, inode_before,
                         "Site/ was replaced instead of synced in place — "
                         "this detaches the containers' bind mounts")

    def test_republish_removes_stale_files(self):
        cli = self._make_cli()
        src1 = self._source_dir({"old.html": "old"})
        self.assertEqual(cli.cmd_publish(src1), 0)
        site_dir = os.path.join(self.docs_dir, "Site")
        self.assertTrue(os.path.exists(os.path.join(site_dir, "old.html")))

        src2 = self._source_dir({"new.html": "new"})
        self.assertEqual(cli.cmd_publish(src2), 0)
        self.assertFalse(os.path.exists(os.path.join(site_dir, "old.html")))
        self.assertTrue(os.path.exists(os.path.join(site_dir, "new.html")))

    def test_republish_preserves_generated_follows_dir(self):
        cli = self._make_cli()
        src1 = self._source_dir({"index.html": "v1"})
        self.assertEqual(cli.cmd_publish(src1), 0)
        site_dir = os.path.join(self.docs_dir, "Site")
        follows_dir = os.path.join(site_dir, "follows")
        os.makedirs(follows_dir)
        with open(os.path.join(follows_dir, "index.html"), "w") as f:
            f.write("generated by follow-fetch")

        src2 = self._source_dir({"index.html": "v2"})
        self.assertEqual(cli.cmd_publish(src2), 0)
        with open(os.path.join(follows_dir, "index.html")) as f:
            self.assertEqual(f.read(), "generated by follow-fetch")


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

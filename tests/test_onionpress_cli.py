"""Tests for src/onionpress/cli.py."""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from onionpress.cli import (
    main, OnionPressCLI, _make_log_func, normalize_get_url,
    GET_DEFAULT_MAX_TIME, TOR_CONTAINER,
)
from onionpress.docker import DockerResult

ONION = "abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrstuvwxyz234567.onion"


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
        instance.cmd_backup.assert_called_once_with("mypass", None, None,
                                                    check=False, json_output=False)

    @mock.patch("onionpress.cli.OnionPressCLI")
    def test_backup_with_output(self, MockCLI):
        instance = MockCLI.return_value
        instance.cmd_backup.return_value = 0
        result = main(["backup", "mypass", "/tmp/backup.zip"])
        instance.cmd_backup.assert_called_once_with("mypass", "/tmp/backup.zip", None,
                                                    check=False, json_output=False)

    @mock.patch("onionpress.cli.OnionPressCLI")
    def test_restore_command(self, MockCLI):
        instance = MockCLI.return_value
        instance.cmd_restore.return_value = 0
        result = main(["restore", "mypass", "/tmp/backup.zip"])
        instance.cmd_restore.assert_called_once_with(
            "mypass", "/tmp/backup.zip", "", confirmed=frozenset(), json_output=False)

    @mock.patch("onionpress.cli.OnionPressCLI")
    def test_regenerate_derivatives_command(self, MockCLI):
        """The launchers spawn this on every start when a restore left the
        pending marker (#257)."""
        instance = MockCLI.return_value
        instance.cmd_regenerate_derivatives.return_value = 0
        self.assertEqual(main(["regenerate-derivatives"]), 0)
        instance.cmd_regenerate_derivatives.assert_called_once_with()

    @mock.patch("onionpress.cli.OnionPressCLI")
    def test_prune_image_derivatives_command(self, MockCLI):
        """The Linux launcher's bash create_backup calls this (#257)."""
        instance = MockCLI.return_value
        instance.cmd_prune_image_derivatives.return_value = 0
        self.assertEqual(
            main(["prune-image-derivatives", "--uploads-dir", "/tmp/stage/uploads"]), 0)
        instance.cmd_prune_image_derivatives.assert_called_once_with("/tmp/stage/uploads")

    @mock.patch("onionpress.cli.OnionPressCLI")
    def test_pack_backup_reads_the_password_from_stdin(self, MockCLI):
        """The Linux launcher's bash create_backup pipes the password in;
        it never appears in argv."""
        instance = MockCLI.return_value
        instance.cmd_pack_backup.return_value = 0
        with mock.patch("sys.stdin", io.StringIO("s3cret pass\n")):
            self.assertEqual(main(["pack-backup", "--staging", "/tmp/st",
                                   "--output", "/tmp/b.zip"]), 0)
        instance.cmd_pack_backup.assert_called_once_with(
            "/tmp/st", "/tmp/b.zip", "s3cret pass")

    @mock.patch("onionpress.cli.OnionPressCLI")
    def test_extract_backup_reads_the_password_from_stdin(self, MockCLI):
        instance = MockCLI.return_value
        instance.cmd_extract_backup.return_value = 0
        with mock.patch("sys.stdin", io.StringIO("s3cret pass")):
            self.assertEqual(main(["extract-backup", "--archive", "/tmp/b.zip",
                                   "--dest", "/tmp/out"]), 0)
        instance.cmd_extract_backup.assert_called_once_with(
            "/tmp/b.zip", "/tmp/out", "s3cret pass")

    @mock.patch("onionpress.cli.OnionPressCLI")
    def test_backup_helpers_refuse_an_empty_password(self, MockCLI):
        instance = MockCLI.return_value
        for argv in (["pack-backup", "--staging", "/s", "--output", "/o"],
                     ["extract-backup", "--archive", "/a", "--dest", "/d"]):
            with self.subTest(argv[0]), \
                    mock.patch("sys.stdin", io.StringIO("")), \
                    mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(main(argv), 1)
        instance.cmd_pack_backup.assert_not_called()
        instance.cmd_extract_backup.assert_not_called()

    @mock.patch("onionpress.cli.OnionPressCLI")
    def test_reset_with_yes(self, MockCLI):
        instance = MockCLI.return_value
        instance.cmd_reset.return_value = 0
        result = main(["reset", "--yes"])
        instance.cmd_reset.assert_called_once_with(
            yes=True, check=False, json_output=False, confirmed=frozenset())

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
    def test_get_command_defaults(self, MockCLI):
        instance = MockCLI.return_value
        instance.cmd_get.return_value = 0
        result = main(["get", ONION])
        self.assertEqual(result, 0)
        instance.cmd_get.assert_called_once_with(
            ONION, headers=False, max_time=GET_DEFAULT_MAX_TIME)

    @mock.patch("onionpress.cli.OnionPressCLI")
    def test_get_command_flags(self, MockCLI):
        instance = MockCLI.return_value
        instance.cmd_get.return_value = 7
        result = main(["get", "-i", "--max-time", "5", f"http://{ONION}/x"])
        self.assertEqual(result, 7)
        instance.cmd_get.assert_called_once_with(
            f"http://{ONION}/x", headers=True, max_time=5)

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


class TestNormalizeGetUrl(unittest.TestCase):
    """`onionpress get` (issue #70) takes a bare .onion or a full URL. What
    comes out must always be a URL argument for curl — never an option,
    never something with whitespace or control characters in it."""

    def test_bare_onion_host_gets_http(self):
        self.assertEqual(normalize_get_url(ONION), f"http://{ONION}")

    def test_bare_host_with_path_and_query(self):
        self.assertEqual(normalize_get_url("example.com/a/b?c=1"),
                         "http://example.com/a/b?c=1")

    def test_full_url_passes_through(self):
        for url in (f"http://{ONION}/", f"https://example.com:8443/x?y=1#z",
                    "http://127.0.0.1:8080/wp-json/"):
            self.assertEqual(normalize_get_url(url), url)

    def test_surrounding_whitespace_is_stripped(self):
        self.assertEqual(normalize_get_url(f"  {ONION}\n"), f"http://{ONION}")

    def test_rejects_malformed(self):
        bad = [
            "", "   ",
            "-K", "--config", "-o/tmp/x",          # would be curl options
            "ftp://example.com/", "file:///etc/passwd", "gopher://x",
            "http://", "https:///path",             # no host
            "http://exa mple.com/", "http://a/b c",  # whitespace
            "http://a\nb", "http://a\rb", "http://a\tb",
            "http://exa\x7fmple.com/", "http://h\x01st/",
            "http://[::1", "http://host:notaport/", "http://host:99999/",
            "http://host;rm -rf x/", "http://$(id)/", "http://a|b/",
            "http://-dash.onion/", "http://under_score.onion/",
            "http://café.onion/",
        ]
        for url in bad:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    normalize_get_url(url)


class TestCmdGet(unittest.TestCase):
    """cmd_get must run curl INSIDE the tor container over docker exec with
    an argv list (CLAUDE.md: SOCKS from the host does not work through
    Colima), and hand curl's exit code back unchanged."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _cli(self, result):
        from onionpress.config import PortConfig, Secrets
        with mock.patch("onionpress.cli.detect_port_offset",
                        return_value=PortConfig(0, 8080, 9050, 9077)), \
             mock.patch("onionpress.cli.ensure_secrets",
                        return_value=Secrets("p1", "p2", "p3")), \
             mock.patch("onionpress.cli.Docker") as MockDocker:
            MockDocker.return_value = mock.Mock()
            cli = OnionPressCLI(data_dir=self.tmpdir)
        cli.docker = mock.Mock()
        cli.docker.exec.return_value = result
        return cli

    def _run(self, cli, *args, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            rc = cli.cmd_get(*args, **kwargs)
        return rc, out.getvalue(), err.getvalue()

    def test_bare_onion_host_argv(self):
        cli = self._cli(DockerResult(0, "<html>hi</html>", ""))
        rc, out, err = self._run(cli, ONION)
        self.assertEqual(rc, 0)
        self.assertEqual(out, "<html>hi</html>")
        self.assertEqual(err, "")
        cli.docker.exec.assert_called_once_with(
            TOR_CONTAINER,
            ["curl", "-sS", "--max-time", str(GET_DEFAULT_MAX_TIME),
             "--socks5-hostname", "127.0.0.1:9050", f"http://{ONION}"],
            timeout=GET_DEFAULT_MAX_TIME + 15, quiet=True)
        self.assertEqual(TOR_CONTAINER, "onionpress-tor")

    def test_full_url_with_headers_and_max_time(self):
        cli = self._cli(DockerResult(0, "HTTP/1.1 200 OK\r\n\r\nbody", ""))
        rc, out, _ = self._run(cli, f"https://example.com/p?q=1",
                               headers=True, max_time=5)
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("HTTP/1.1 200 OK"))
        cli.docker.exec.assert_called_once_with(
            TOR_CONTAINER,
            ["curl", "-sS", "--max-time", "5",
             "--socks5-hostname", "127.0.0.1:9050", "--include",
             "https://example.com/p?q=1"],
            timeout=20, quiet=True)

    def test_curl_exit_code_and_stderr_pass_through(self):
        cli = self._cli(DockerResult(7, "", "curl: (7) Can't complete SOCKS5 connection"))
        rc, out, err = self._run(cli, ONION)
        self.assertEqual(rc, 7)
        self.assertEqual(out, "")
        self.assertIn("curl: (7)", err)

    def test_docker_timeout_is_124(self):
        cli = self._cli(DockerResult(-1, "", "Command timed out after 75s", timed_out=True))
        rc, _, err = self._run(cli, ONION)
        self.assertEqual(rc, 124)
        self.assertIn("timed out", err)

    def test_malformed_url_never_reaches_docker(self):
        cli = self._cli(DockerResult(0, "", ""))
        for url in ("-K", "ftp://x/", "http://exa mple.com/", ""):
            with self.subTest(url=url):
                rc, out, err = self._run(cli, url)
                self.assertEqual(rc, 2)
                self.assertEqual(out, "")
                self.assertIn("ERROR", err)
        cli.docker.exec.assert_not_called()

    def test_nonpositive_max_time_rejected(self):
        cli = self._cli(DockerResult(0, "", ""))
        rc, _, err = self._run(cli, ONION, max_time=0)
        self.assertEqual(rc, 2)
        self.assertIn("max-time", err)
        cli.docker.exec.assert_not_called()



PIN = "ghcr.io/brewsterkahle/onionpress-tor:latest@sha256:" + "a" * 64
V3_ONION = "op2" + "a" * 52 + "d.onion"


class TestCmdRestoreKeySeed(unittest.TestCase):
    """cmd_restore seeds onionpress-onion-keys with the restored key from the
    stack's pinned tor image, with no network; it refuses before changing
    anything when that image is not here, and a seed that fails stops the
    restore (and leaves no volume behind) instead of starting Tor, which
    would mint a new address under the restored site."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        from onionpress.config import PortConfig, Secrets
        with mock.patch("onionpress.cli.detect_port_offset",
                        return_value=PortConfig(0, 8080, 9050, 9077)), \
             mock.patch("onionpress.cli.ensure_secrets",
                        return_value=Secrets("p1", "p2", "p3")), \
             mock.patch("onionpress.cli.Docker"):
            self.cli = OnionPressCLI(data_dir=self.tmpdir)
        self.cli.log = mock.Mock()
        self.cli.docker = mock.Mock()
        self.calls = []
        self.seed_ok = True

        def run(args, timeout=30, **kw):
            self.calls.append(list(args))
            ok = self.seed_ok or args[0] != "run"
            return DockerResult(0 if ok else 1, "", "" if ok else "cp: no such file")
        self.cli.docker.run.side_effect = run
        self.cli.containers = mock.Mock()
        self.cli.containers.ensure_tor_image.return_value = PIN
        self.cli.containers.wait_for_tor.return_value = True
        self.marker = os.path.join(self.tmpdir, ".key-volume-migration-pending")
        open(self.marker, "w").close()

    def _restore(self):
        prepare = mock.patch(
            "onionpress.backup.prepare_install_from_backup",
            return_value=(os.path.join(self.tmpdir, "staging"), {"onion_address": V3_ONION}))
        # A backup with a key (V3_ONION's): one without keeps the key
        # volumes and seeds nothing (tests/test_settings_page_onion_key.py).
        with prepare as p, \
             mock.patch("onionpress.backup.read_backup_metadata", return_value={}), \
             mock.patch("onionpress.backup.staged_onion_key_address", return_value=V3_ONION), \
             mock.patch.object(OnionPressCLI, "cmd_import_backup_artifacts", return_value=0), \
             mock.patch("onionpress.derivatives.spawn_regeneration"):
            rc = self.cli.cmd_restore("pw", "/tmp/b.zip")
        return rc, p

    def test_refuses_without_the_tor_image_before_changing_anything(self):
        self.cli.containers.ensure_tor_image.return_value = None
        rc, prepare = self._restore()
        self.assertEqual(rc, 1)
        prepare.assert_not_called()
        self.assertEqual(self.calls, [])
        self.cli.containers.stop.assert_not_called()
        self.assertTrue(os.path.exists(self.marker))

    def test_the_seed_runs_the_pinned_image_without_a_network(self):
        rc, _ = self._restore()
        self.assertEqual(rc, 0)
        seed = next(c for c in self.calls if c[0] == "run")
        image_at = seed.index(PIN)
        options = seed[:image_at]
        pairs = set(zip(options, options[1:]))
        self.assertIn(("--network", "none"), pairs)
        self.assertIn(("--entrypoint", "sh"), pairs)
        self.assertIn(("--cap-drop", "ALL"), pairs)
        self.assertIn(("-v", "onionpress-onion-keys:/dest"), pairs)
        mount = options[options.index("--mount") + 1]
        self.assertTrue(mount.endswith(",target=/src,readonly"), mount)
        self.assertIn(os.path.join("vanity-keys", V3_ONION), mount)
        self.assertEqual(seed[image_at + 1], "-c")
        self.cli.containers.start_tor.assert_called_once()
        # The restored key replaces an unfinished key volume migration.
        self.assertFalse(os.path.exists(self.marker))

    def test_a_failed_seed_stops_the_restore_and_removes_the_volume(self):
        self.seed_ok = False
        rc, _ = self._restore()
        self.assertEqual(rc, 1)
        seed_at = next(i for i, c in enumerate(self.calls) if c[0] == "run")
        self.assertEqual(self.calls[seed_at + 1:], [["volume", "rm", "onionpress-onion-keys"]])
        self.cli.containers.start_core.assert_not_called()
        self.cli.containers.start_tor.assert_not_called()


class TestCmdStartWaitsForTheKeyVolume(unittest.TestCase):
    """`onionpress.cli start` has no key volume migration of its own: while
    one is due it starts nothing (starting tor would create an empty
    onionpress-onion-keys that every later start takes for the key)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, True)

    def test_refuses_before_starting_anything(self):
        from onionpress.config import PortConfig, Secrets
        with mock.patch("onionpress.cli.detect_port_offset",
                        return_value=PortConfig(0, 8080, 9050, 9077)), \
             mock.patch("onionpress.cli.ensure_secrets",
                        return_value=Secrets("p1", "p2", "p3")), \
             mock.patch("onionpress.cli.Docker"):
            cli = OnionPressCLI(data_dir=self.tmpdir)
        cli.log = mock.Mock()
        cli.containers = mock.Mock()
        cli.containers.key_volume_not_ready.return_value = "the key is still in onionpress-arti-state"
        with mock.patch("onionpress.cli.detect_os", return_value=None):
            rc = cli.cmd_start()
        self.assertEqual(rc, 1)
        cli.containers.pull_images.assert_not_called()
        cli.containers.start_core.assert_not_called()
        cli.containers.start_tor.assert_not_called()


if __name__ == "__main__":
    unittest.main()

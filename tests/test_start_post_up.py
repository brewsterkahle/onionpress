#!/usr/bin/env python3
"""The end of a launcher's start (issue #442.7): start.post_up, which both
launchers run through `onionpress start-post-up` once the core is up, and
its pieces: start.warn_if_socks_port_exposed,
start.configure_site_timezones, start.run_detached_cli.

These were bash until #442.7, tested by running the launchers' functions;
the same cases run against the Python here."""

import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

from onionpress import bundled_plugins, derivatives, multisite, start  # noqa: E402
from onionpress.docker import DockerResult  # noqa: E402

TOKEN = "eyJ" + "a" * 60


class FakeDocker:
    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls, self.inputs = [], []
        self.docker_bin = "docker"

    def run(self, args, input=None, **_kw):
        args = [str(a) for a in args]
        self.calls.append(" ".join(args))
        self.inputs.append(input)
        for prefix, (rc, out) in self.answers.items():
            if " ".join(args).startswith(prefix):
                return DockerResult(rc, out, "")
        return DockerResult(0, "", "")


class FakeManager:
    def __init__(self, data, docker, compose_rc=0):
        self.docker = docker
        self.compose = []
        self.compose_rc = compose_rc
        self.paths = mock.Mock(data_dir=data, config_file=os.path.join(data, "config"),
                               log_file=os.path.join(data, "onionpress.log"),
                               docker_dir="/opt/onionpress/docker")

    not_ready = None

    def key_volume_not_ready(self):
        return self.not_ready

    def run_compose_to(self, args, *, out, include_cloudflare=False):
        self.compose.append((args, out, include_cloudflare))
        return self.compose_rc


class Base(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.data, True)
        self.lines = []
        self.config("")

    def config(self, text):
        with open(os.path.join(self.data, "config"), "w") as f:
            f.write(text)


# The Cloudflare tunnel is the launchers' start_cloudflare_tunnel, last in
# a start after this step (tests/test_cloudflare_tunnel.py).


class TestSocksExposureWarning(Base):
    """Re-running install.sh leaves an older docker-compose.yml in place,
    and that one still publishes SOCKS on ONIONPRESS_BIND_HOST: after Tor
    starts, Docker's real mapping is asked and a wider one warned about."""

    def warn(self, rc, out):
        manager = FakeManager(self.data, FakeDocker({"port onionpress-tor": (rc, out)}))
        start.warn_if_socks_port_exposed(manager, self.lines.append)
        return self.lines

    def test_loopback_mapping_is_quiet(self):
        self.assertEqual(self.warn(0, "127.0.0.1:19050\n"), [])

    def test_wider_mapping_warns(self):
        out = self.warn(0, "0.0.0.0:9050\n[::]:9050\n")
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith("WARNING: Tor's SOCKS port is published beyond "
                                          "this machine (0.0.0.0:9050 [::]:9050): "
                                          "/opt/onionpress/docker/docker-compose.yml"))

    def test_no_container_is_quiet(self):
        self.assertEqual(self.warn(1, ""), [])


class TestDetached(Base):
    """Children of the start outlive it: a session of their own, or, from
    the Linux Settings-page watcher, a transient user unit (the watcher's
    stop would end them in its cgroup)."""

    def run_detached(self, in_watcher):
        bin_dir = os.path.join(self.data, "bin")
        os.makedirs(bin_dir, exist_ok=True)
        with open(os.path.join(bin_dir, "systemd-run"), "w") as f:
            f.write('#!/bin/sh\nfor a in "$@"; do echo "$a"; done > "$OUT/systemd-run"\n')
        os.chmod(os.path.join(bin_dir, "systemd-run"), 0o755)
        env = {"PATH": bin_dir + os.pathsep + "/usr/bin:/bin", "OUT": self.data,
               "ONIONPRESS_IN_WATCHER": in_watcher,
               "DOCKER_HOST": "unix:///run/user/1000/docker.sock",
               "MYSQL_ROOT_PASSWORD": "s3cret-not-for-argv"}
        command = ["sh", "-c", f'echo ran > "{self.data}/ran"']
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(derivatives, "isolated_cli_argv", return_value=command):
            start.run_detached_cli(["regenerate-derivatives"], data_dir=self.data,
                                   log_file=os.path.join(self.data, "log"))

    def test_from_the_watcher_it_goes_to_a_transient_user_unit(self):
        self.run_detached("true")
        with open(os.path.join(self.data, "systemd-run")) as f:
            argv = f.read().splitlines()
        self.assertIn("--user", argv)
        self.assertIn("--collect", argv)
        self.assertIn("--setenv=DOCKER_HOST=unix:///run/user/1000/docker.sock", argv)
        self.assertEqual(argv[argv.index("--") + 1:], ["sh", "-c", f'echo ran > "{self.data}/ran"'])
        self.assertFalse(any("s3cret" in a for a in argv))
        self.assertFalse(os.path.exists(os.path.join(self.data, "ran")))

    def test_elsewhere_it_runs_in_a_session_of_its_own(self):
        with mock.patch.object(start.subprocess, "Popen", wraps=start.subprocess.Popen) as popen:
            self.run_detached("")
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertFalse(os.path.exists(os.path.join(self.data, "systemd-run")))
        for _ in range(100):
            if os.path.exists(os.path.join(self.data, "ran")):
                break
            time.sleep(0.05)
        self.assertTrue(os.path.exists(os.path.join(self.data, "ran")))

    def test_the_command_is_the_isolated_one(self):
        with mock.patch.object(start.subprocess, "Popen") as popen:
            start.run_detached_cli(["ensure-archive-s3-keys"], data_dir=self.data,
                                   log_file=os.path.join(self.data, "log"))
        argv = popen.call_args.args[0]
        self.assertEqual(argv, derivatives.isolated_cli_argv(
            "--data-dir", self.data, "ensure-archive-s3-keys"))
        self.assertEqual(popen.call_args.kwargs["cwd"], self.data)


class TestPostUp(Base):

    def post_up(self, mac, installed=True, docker=None, steps=None, compose_rc=0):
        docker = docker or FakeDocker()
        manager = FakeManager(self.data, docker, compose_rc=compose_rc)
        order = []
        steps = steps or {}

        def step(argv, **_kw):
            order.append(" ".join(argv))
            return steps.get(argv[0], 0)
        errors = start.StartErrors()
        with mock.patch.object(start, "run_cli_step", side_effect=step), \
             mock.patch.object(start, "run_detached_cli",
                               side_effect=lambda argv, **k: order.append("detached " + argv[0])), \
             mock.patch.object(start, "configure_site_timezones",
                               side_effect=lambda *a: order.append("timezones")), \
             mock.patch.object(start, "warn_if_socks_port_exposed",
                               side_effect=lambda *a: order.append("socks")), \
             mock.patch.object(bundled_plugins, "install_bundled_plugins",
                               side_effect=lambda h: order.append("install-plugins")), \
             mock.patch.object(bundled_plugins, "setup_bundled_plugins",
                               side_effect=lambda h: order.append("setup-plugins")), \
             mock.patch.object(derivatives, "regeneration_pending", return_value=True):
            start.post_up(manager, mac=mac, installed=installed, errors=errors,
                          themes_dir="/t", plugins_dir="/p", scripts_dir="/s",
                          log=self.lines.append)
        return manager, order, errors

    def test_mac_order(self):
        manager, order, errors = self.post_up(True)
        self.assertEqual(manager.compose, [(["up", "-d", "tor", "onionheaven"], "log", False)])
        self.assertEqual(order, [
            "install-plugins", "deactivate-wp-statistics",
            "provision-post-install --themes-dir /t --plugins-dir /p", "setup-plugins",
            "detached regenerate-derivatives", "detached ensure-archive-s3-keys",
            "timezones"])
        self.assertEqual(errors.names, [])

    def test_linux_order(self):
        _, order, _ = self.post_up(False)
        self.assertEqual(order, [
            "socks", "install-plugins", "deactivate-wp-statistics",
            "provision-post-install --themes-dir /t --plugins-dir /p", "setup-plugins",
            "detached regenerate-derivatives"])

    def test_failed_steps_are_named_and_never_stop_it(self):
        for mac in (True, False):
            with self.subTest(mac=mac):
                self.lines.clear()
                _, order, errors = self.post_up(
                    mac, compose_rc=1,
                    steps={"deactivate-wp-statistics": 1, "provision-post-install": 2})
                self.assertIn("setup-plugins", order, "the start goes on")
                self.assertEqual(errors.names, ["tor", "deactivate-wp-statistics",
                                                "provision-post-install"] if mac
                                 else ["deactivate-wp-statistics", "provision-post-install"])
                self.assertIn("WARNING: provision-post-install failed (rc=2) — themes/plugins "
                              "may be missing; see " + os.path.join(self.data, "onionpress.log"),
                              self.lines)

    def test_never_tor_before_its_key_volume(self):
        with mock.patch.object(FakeManager, "not_ready", "the key is still in onionpress-arti-state"):
            manager, order, errors = self.post_up(True)
        self.assertEqual(manager.compose, [])
        self.assertIn("ERROR: not starting Tor: the key is still in onionpress-arti-state",
                      self.lines)
        self.assertEqual(errors.names, ["tor"])
        self.assertIn("setup-plugins", order)

    def test_no_derivative_rebuild_before_wordpress_is_installed(self):
        _, order, _ = self.post_up(True, installed=False)
        self.assertNotIn("detached regenerate-derivatives", order)
        self.assertIn("WordPress not yet installed — will be configured by setup window",
                      self.lines)

    def test_htaccess_is_the_one_body_written_as_www_data(self):
        docker = FakeDocker({"exec onionpress-wordpress test -s": (1, "")})
        self.post_up(True, docker=docker)
        i = next(i for i, c in enumerate(docker.calls) if c.startswith("exec -i -u www-data"))
        self.assertIn("/var/www/html/.htaccess 644", docker.calls[i])
        self.assertEqual(docker.inputs[i], multisite.HTACCESS_BODY)
        self.assertIn("✓ .htaccess restored", self.lines)

    def test_install_from_backup(self):
        staging = os.path.join(self.data, "staging")
        os.makedirs(staging)
        marker = os.path.join(self.data, start.INSTALL_FROM_BACKUP)
        with open(marker, "w") as f:
            f.write(staging + "\n")
        _, order, _ = self.post_up(True, steps={"import-backup-artifacts": 1})
        self.assertLess(order.index(f"import-backup-artifacts --staging {staging}"),
                        order.index("deactivate-wp-statistics"))
        self.assertTrue(os.path.exists(marker), "a failed import is retried")
        self.assertTrue(os.path.isdir(staging))
        self.post_up(True)
        self.assertFalse(os.path.exists(marker))
        self.assertFalse(os.path.exists(staging))
        self.assertIn("Install-from-backup: complete (staging cleaned up)", self.lines)

    def test_install_from_backup_is_the_macs(self):
        with open(os.path.join(self.data, start.INSTALL_FROM_BACKUP), "w") as f:
            f.write("/nowhere\n")
        _, order, _ = self.post_up(False)
        self.assertFalse([o for o in order if o.startswith("import-backup")])


class TestTimezones(Base):

    def test_only_empty_zones_are_set(self):
        docker = FakeDocker({
            "exec -u www-data -e WP_CLI_CACHE_DIR=/tmp/wp-cli-cache onionpress-wordpress "
            "wp site list": (0, "http://a/\nhttp://b/\n"),
            "exec -u www-data -e WP_CLI_CACHE_DIR=/tmp/wp-cli-cache onionpress-wordpress "
            "wp option get timezone_string --url=http://a/": (0, "Europe/Oslo\n"),
        })
        with mock.patch.object(start, "_host_timezone", return_value="America/Los_Angeles"):
            start.configure_site_timezones(docker, self.lines.append,
                                           os.path.join(self.data, "log"))
        updates = [c for c in docker.calls if "option update" in c]
        self.assertEqual([u.split("wp ", 1)[1] for u in updates], [
            "option update timezone_string America/Los_Angeles --url=http://b/",
            "option update gmt_offset  --url=http://b/"])
        self.assertEqual(self.lines, ["✓ Set timezone_string=America/Los_Angeles for http://b/"])

    def test_not_installed_or_no_zone(self):
        docker = FakeDocker({"exec -u www-data -e WP_CLI_CACHE_DIR=/tmp/wp-cli-cache "
                             "onionpress-wordpress wp core is-installed": (1, "")})
        start.configure_site_timezones(docker, self.lines.append, "/dev/null")
        self.assertEqual(len(docker.calls), 1)
        with mock.patch.object(start, "_host_timezone", return_value=""):
            start.configure_site_timezones(FakeDocker(), self.lines.append, "/dev/null")
        self.assertEqual(self.lines, ["Skipping timezone config: host TZ undetectable"])


class TestLaunchers(unittest.TestCase):

    def _read(self, rel):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            return f.read()

    def test_both_end_the_start_with_post_up(self):
        """The Mac's start-containers runs it; Linux's start runs
        start-post-up itself after its install-state steps (#442.8)."""
        text = self._read("linux/onionpress")
        i = text.index("\nstart_containers() {")
        body = text[i:text.index("\n}\n", i)]
        self.assertLess(body.index("start-containers --until-core"),
                        body.index('start_post_up "$wp_installed"'))
        self.assertIn("post_up(manager", self._read("src/onionpress/start.py"))

    def test_linux_keeps_its_host_steps_around_it(self):
        text = self._read("linux/onionpress")
        i = text.index("\nstart_containers() {")
        body = text[i:text.index("\n}\n", i)]
        post = body.index('start_post_up "$wp_installed"')
        for before in ("after_wordpress_installed", "hold_heartbeat_client_until_installed",
                       "note_if_lan_looks_local"):
            self.assertLess(body.index(before), post, before)
        self.assertLess(post, body.index("start_onionheaven_activation_watcher"))


if __name__ == "__main__":
    unittest.main()

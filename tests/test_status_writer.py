#!/usr/bin/env python3
"""One status writer for both platforms (issue #523, onionpress.status_writer).

The Mac daemon's status pass and Linux's `onionpress write-status`
(OneShotStatus, until the daemon runs on Linux, #407) build the same two
documents, daemon-state.json and status.json, through the same builders;
the fields Linux's bash gathered (the tray's) are each one function here,
tested against a fake docker; what reaches the WordPress volume is cut
to redact.WORDPRESS_STATUS_KEYS and the log excerpt scrubbed; the
registry is asked only on a hub, through onionheaven.overview; the
heartbeat's reachability.json is the one measure of reachability (no
probe of the writer's own).
"""

import ast
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from onionpress import cli, onionheaven, redact, status_writer as sw  # noqa: E402
from onionpress import launcher_ops, wp_files  # noqa: E402
from onionpress.config import SAFE_CONFIG_KEYS  # noqa: E402
from onionpress.daemon_client import read_state  # noqa: E402
from onionpress.docker import DockerResult  # noqa: E402
from onionpress.platform import OS, resolve_paths  # noqa: E402

ADDR = "op2ujzdegxdncf32epf3dhodzdocis2jhtlgmxgedn73u55xtplpft7v.onion"
HUB = onionheaven.DEFAULT_ONIONHEAVEN_ADDRESS
TOKEN = "eyJhIjoiY2xvdWRmbGFyZS10dW5uZWwtc2VjcmV0In0"
PS = ("onionpress-tor\trunning\tdocker\nonionpress-wordpress\trunning\tdocker\n"
      "onionpress-db\trunning\tdocker\nonionheaven\trunning\tdocker\n"
      "onionheaven-takeover-3\trunning\tdocker\n")
HOSTILE = "a[$(touch /tmp/pwned)]"


def _read(rel):
    with open(os.path.join(PROJECT_ROOT, rel), encoding="utf-8") as f:
        return f.read()


class FakeDocker:
    """Answers `docker <argv>` from a table of prefixes; records calls;
    keeps what wp_files.put_file_args writes, by file name."""

    docker_bin = "/x/docker"
    env = {"DOCKER_HOST": "unix:///nowhere"}

    def __init__(self, answers=None, raising=False):
        self.answers = dict(answers or {})
        self.calls = []
        self.volume = {}
        self.raising = raising

    def _answer(self, args):
        key = " ".join(str(a) for a in args)
        for prefix in sorted(self.answers, key=len, reverse=True):
            if key == prefix or key.startswith(prefix + " "):
                out = self.answers[prefix]
                if callable(out):
                    out = out(args)
                rc, text = out if isinstance(out, tuple) else (0, out)
                return DockerResult(returncode=rc, stdout=text, stderr="")
        return DockerResult(returncode=1, stdout="", stderr="no such answer")

    def run(self, args, timeout=30, check=False, input=None, extra_env=None, quiet=False):
        args = [str(a) for a in args]
        self.calls.append((args, input))
        if self.raising:
            raise OSError("no docker")
        if args[:3] == ["exec", "-i", "-u"] and args[-3] == "sh":
            self.volume[os.path.basename(args[-2])] = (input, args[-1])
            return DockerResult(returncode=0, stdout="", stderr="")
        return self._answer(args)

    def exec(self, container, command, timeout=30, check=False, quiet=False, user=None):
        return self.run(["exec"] + (["-u", user] if user else []) + [container, *command],
                        timeout=timeout, check=check, quiet=quiet)

    def wp(self, *args, timeout=30, check=False, quiet=False, input=None, offline=False, nice=False):
        return self.run(wp_files.wp_cli_args(*args, stdin=input is not None, offline=offline,
                                             nice=nice), timeout=timeout, input=input, quiet=quiet)

    def argv(self):
        return [" ".join(a) for a, _ in self.calls]


class _Sandbox(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="op-status-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.paths = resolve_paths(data_dir=self.dir, app_bundle="")
        with open(self.paths.config_file, "w") as f:
            f.write("REGISTER_WITH_ONIONHEAVEN=no\nONIONNAME=ivar\nSTART_ON_BOOT=no\n"
                    f"CLOUDFLARE_TUNNEL_TOKEN={TOKEN}\nSOME_FUTURE_API_KEY=k3y\n")
        self.logs = []
        # The seams: this test's platform is whichever it says.
        sw._BOOT_TIME.clear()
        self.addCleanup(sw._BOOT_TIME.clear)

    def log(self, msg):
        self.logs.append(msg)

    def docker(self, **extra):
        answers = {
            "ps -a --filter label=com.docker.compose.project": PS,
            "exec onionpress-tor cat /var/lib/tor/hidden_service/wordpress/hostname": ADDR + "\n",
            "inspect --format {{.State.StartedAt}} onionpress-tor": "2026-10-31T10:00:00.123456789Z\n",
            "exec -u www-data -e WP_CLI_CACHE_DIR=/tmp/wp-cli-cache onionpress-wordpress wp core is-installed": "",
            "exec -u www-data -e WP_CLI_CACHE_DIR=/tmp/wp-cli-cache onionpress-wordpress wp eval echo (string) get_site_option(\"onionpress_onboarded\", \"\");": "1\n",
            "exec -u www-data -e WP_CLI_CACHE_DIR=/tmp/wp-cli-cache onionpress-wordpress wp eval echo json_encode(onionpress_wayback_queue_totals());": '{"remaining": 2, "in_flight": 1}',
            "image inspect --format {{.Id}}": "sha256:abc\n",
            "image inspect": "",
            "run --rm --network none": "",
        }
        answers.update(extra)
        return FakeDocker(answers)

    def health(self, bootstrapped=(True, 100)):
        h = mock.Mock()
        h.check_tor_bootstrap.return_value = bootstrapped
        return h

    def reachability(self, reachable=True, address=ADDR, age=10, code="200", now=None):
        now = time.time() if now is None else now
        with open(os.path.join(self.dir, "reachability.json"), "w") as f:
            json.dump({"checked_at": now - age, "onion_address": address,
                       "reachable": reachable, "http_code": code}, f)

    def app(self, docker=None, health=None, os_name=OS.LINUX, now=None, version="9.9"):
        return sw.OneShotStatus(self.paths, docker=docker or self.docker(), health=health or self.health(),
                                version=version, log=self.log, os_name=os_name, now=now)

    def host_status(self):
        with open(os.path.join(self.dir, "status.json")) as f:
            return json.load(f)


# ---------------------------------------------------------------------------
# The fields Linux's bash gathered, one function each
# ---------------------------------------------------------------------------

class TestFields(_Sandbox):

    WP = "exec -u www-data -e WP_CLI_CACHE_DIR=/tmp/wp-cli-cache onionpress-wordpress wp "

    def test_wp_installed(self):
        self.assertTrue(sw.wp_installed(self.docker()))
        self.assertFalse(sw.wp_installed(self.docker(**{self.WP + "core is-installed": (1, "")})))
        self.assertIsNone(sw.wp_installed(FakeDocker(raising=True)))

    def test_onboarded_is_the_network_wide_option(self):
        d = self.docker()
        self.assertTrue(sw.onboarded(d))
        self.assertIn(self.WP + 'eval echo (string) get_site_option("onionpress_onboarded", "");',
                      d.argv())
        for value in ("", "0", "false", (1, "1")):
            with self.subTest(value=value):
                self.assertFalse(sw.onboarded(self.docker(**{
                    self.WP + 'eval echo (string) get_site_option("onionpress_onboarded", "");': value})))

    def test_wayback_queue_count(self):
        self.assertEqual(sw.wayback_queue_count(self.docker()), 3)
        key = self.WP + "eval echo json_encode(onionpress_wayback_queue_totals());"
        for value in ("not json", "[1]", (1, "{}"), '{"remaining": "x"}'):
            with self.subTest(value=value):
                self.assertEqual(sw.wayback_queue_count(self.docker(**{key: value})), 0)

    def test_mkp224o_is_probed_once_per_image(self):
        d = self.docker()
        self.assertTrue(sw.mkp224o_available(d, self.dir))
        probes = [a for a in d.argv() if a.startswith("run ")]
        self.assertEqual(len(probes), 1)
        self.assertIn("--entrypoint test " + launcher_ops.DEFAULT_TOR_IMAGE + " -x /usr/local/bin/mkp224o",
                      probes[0])
        self.assertIn("image inspect --format {{.Id}} " + launcher_ops.DEFAULT_TOR_IMAGE, d.argv())
        # The answer is kept by image id: the next pass runs no container.
        self.assertTrue(sw.mkp224o_available(d, self.dir))
        self.assertEqual(len([a for a in d.argv() if a.startswith("run ")]), 1)
        with open(os.path.join(self.dir, sw.MKP224O_PROBE_CACHE)) as f:
            self.assertEqual(json.load(f), {"image_id": "sha256:abc", "available": True})
        # A new image is probed again; one without mkp224o says so.
        d = self.docker(**{"image inspect --format {{.Id}}": "sha256:def\n", "run --rm --network none": (1, "")})
        self.assertFalse(sw.mkp224o_available(d, self.dir))
        self.assertEqual(len([a for a in d.argv() if a.startswith("run ")]), 1)
        self.assertFalse(sw.mkp224o_available(d, self.dir))
        self.assertEqual(len([a for a in d.argv() if a.startswith("run ")]), 1)
        # No image: no probe, nothing cached.
        os.remove(os.path.join(self.dir, sw.MKP224O_PROBE_CACHE))
        d = self.docker(**{"image inspect --format {{.Id}}": (1, "")})
        self.assertFalse(sw.mkp224o_available(d, self.dir))
        self.assertEqual([a for a in d.argv() if a.startswith("run ")], [])
        self.assertFalse(os.path.exists(os.path.join(self.dir, sw.MKP224O_PROBE_CACHE)))

    def test_the_probe_is_launcher_ops_with_the_docker_given(self):
        d = self.docker()
        self.assertTrue(launcher_ops.tor_image_has_mkp224o("img", docker=d))
        self.assertEqual(d.argv()[0], "image inspect img")
        self.assertTrue(d.argv()[1].startswith("run --rm --network none --cap-drop ALL "
                                               "--security-opt no-new-privileges:true "
                                               "--entrypoint test img -x /usr/local/bin/mkp224o"))

    def test_markers_under_the_data_dir(self):
        self.assertFalse(sw.vanity_in_progress(self.dir))
        self.assertIsNone(sw.wp_admin_password_path(self.dir))
        with open(os.path.join(self.dir, sw.VANITY_MARKER), "w"):
            pass
        with open(os.path.join(self.dir, sw.WP_ADMIN_PASSWORD_FILE), "w") as f:
            f.write("pw\n")
        self.assertTrue(sw.vanity_in_progress(self.dir))
        self.assertEqual(sw.wp_admin_password_path(self.dir),
                         os.path.join(self.dir, "wp-admin-password"))

    def test_serving_from_wayback(self):
        self.assertTrue(sw.serving_from_wayback("starting", True))
        self.assertTrue(sw.serving_from_wayback("stopped", True))
        self.assertFalse(sw.serving_from_wayback("running", True))
        self.assertFalse(sw.serving_from_wayback("starting", False))

    def test_tor_uptime_from_started_at(self):
        now = datetime(2026, 10, 31, 10, 5, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(sw.tor_uptime_seconds(self.docker(), now), 299)
        key = "inspect --format {{.State.StartedAt}} onionpress-tor"
        # Not started, no container, and a container's hostile answer
        # (issue #466: never evaluated, never anything but 0).
        for value in ("0001-01-01T00:00:00Z", (1, ""), HOSTILE, "", "2026-13-99T00:00:00Z"):
            with self.subTest(value=value):
                self.assertEqual(sw.tor_uptime_seconds(self.docker(**{key: value}), now), 0)
        self.assertFalse(os.path.exists("/tmp/pwned"))

    def test_the_heartbeats_verdict(self):
        now = 1_000_000.0
        self.reachability(now=now)
        self.assertEqual(sw.heartbeat_verdict(self.dir, ADDR, now), (True, "200"))
        self.reachability(now=now, reachable=False, code="000:rc=28")
        self.assertEqual(sw.heartbeat_verdict(self.dir, ADDR, now), (False, "000:rc=28"))
        # Stale, another address's, not a verdict, missing, garbage: nothing.
        self.reachability(now=now, age=sw.REACHABILITY_FRESH_SECONDS + 1)
        self.assertEqual(sw.heartbeat_verdict(self.dir, ADDR, now), (None, None))
        self.reachability(now=now, address="b" * 56 + ".onion")
        self.assertEqual(sw.heartbeat_verdict(self.dir, ADDR, now), (None, None))
        self.reachability(now=now, reachable=None)
        self.assertEqual(sw.heartbeat_verdict(self.dir, ADDR, now), (None, None))
        with open(os.path.join(self.dir, "reachability.json"), "w") as f:
            f.write("{nope")
        self.assertEqual(sw.heartbeat_verdict(self.dir, ADDR, now), (None, None))
        os.remove(os.path.join(self.dir, "reachability.json"))
        self.assertEqual(sw.heartbeat_verdict(self.dir, ADDR, now), (None, None))

    def test_list_containers_skips_the_workers(self):
        d = self.docker()
        self.assertEqual(sw.list_containers(d), [
            ("onionpress-tor", "running"), ("onionpress-wordpress", "running"),
            ("onionpress-db", "running"), ("onionheaven", "running")])
        self.assertIsNone(sw.list_containers(self.docker(**{"ps -a --filter label=com.docker.compose.project": (1, "")})))

    def test_platform_seams(self):
        self.assertEqual(sw.platform_name(OS.LINUX), "linux")
        self.assertEqual(sw.platform_name(OS.MACOS), "macos")
        now = 5_000_000.0
        with mock.patch("builtins.open", mock.mock_open(read_data="123.45 400.0\n")):
            self.assertAlmostEqual(sw.host_boot_time(OS.LINUX, now), now - 123.45)
        sw._BOOT_TIME.clear()
        with mock.patch.object(sw.subprocess, "run",
                               return_value=mock.Mock(stdout="{ sec = 4999000, usec = 1 } x")):
            self.assertEqual(sw.host_boot_time(OS.MACOS, now), 4999000)
        sw._BOOT_TIME.clear()
        with mock.patch.object(sw.subprocess, "run", side_effect=OSError("no sysctl")):
            self.assertEqual(sw.host_boot_time(OS.MACOS, now), now)


# ---------------------------------------------------------------------------
# The documents and what reaches the volume
# ---------------------------------------------------------------------------

class TestWrite(_Sandbox):

    def setUp(self):
        super().setUp()
        self.log_file = self.paths.log_file
        with open(self.log_file, "w") as f:
            f.write(f"[t] Wrote {self.dir}/config\n"
                    f"[t] cloudflared --token {TOKEN}\n"
                    "[t] Bound WordPress to 192.168.1.23:8080\n")
        with open(os.path.join(self.dir, "wp-admin-password"), "w") as f:
            f.write("not-read-by-this-test\n")

    def running_app(self, **docker):
        self.reachability()
        app = self.app(docker=self.docker(**docker))
        app.gather()
        return app

    def test_both_platforms_fields_on_both_platforms(self):
        app = self.running_app()
        sw.write(app)
        status = app._last_status_payload
        mac_fields = {"state", "version", "onion_address", "onionname", "tor_impl", "uptime_seconds",
                      "bootstrap_pct", "onion_reachable", "onion_http_code", "containers",
                      "updated_at", "platform", "load_avg", "host_uptime_seconds", "onionheaven",
                      "config"}
        linux_fields = {"wayback_queue_count", "wp_installed", "onboarded", "mkp224o_available",
                        "wp_admin_password_path", "vanity_in_progress", "serving_from_wayback",
                        "wp_port"}
        self.assertEqual(set(status), mac_fields | linux_fields)
        self.assertLessEqual(redact.WORDPRESS_STATUS_KEYS, set(status))
        self.assertEqual(status["state"], "running")
        self.assertEqual(status["platform"], "linux")
        self.assertEqual(status["onion_address"], ADDR)
        self.assertEqual(status["onionname"], "ivar")
        self.assertEqual((status["onion_reachable"], status["onion_http_code"]), (True, "200"))
        self.assertEqual(status["bootstrap_pct"], 100)
        self.assertEqual(status["containers"], {"onionpress-tor": "running", "onionpress-wordpress": "running",
                                                "onionpress-db": "running", "onionheaven": "running"})
        self.assertTrue(status["wp_installed"])
        self.assertTrue(status["onboarded"])
        self.assertEqual(status["wayback_queue_count"], 3)
        self.assertTrue(status["mkp224o_available"])
        self.assertEqual(status["wp_admin_password_path"], os.path.join(self.dir, "wp-admin-password"))
        self.assertFalse(status["vanity_in_progress"])
        self.assertFalse(status["serving_from_wayback"])
        self.assertEqual(status["wp_port"], 8080)
        self.assertGreater(status["uptime_seconds"], 0)
        self.assertEqual(status["config"]["ONIONNAME"], "ivar")
        self.assertLessEqual(set(status["config"]), SAFE_CONFIG_KEYS)
        oh = status["onionheaven"]
        self.assertFalse(oh["server_active"])
        self.assertIn("generated_at", oh)
        self.assertIn("client", oh)
        # The Mac's state file carries the same document.
        sw.publish(app)
        state = read_state(self.dir)
        self.assertEqual(state["status"], status)
        self.assertEqual(state["display_state"], "available")
        self.assertEqual(state["pid"], os.getpid())
        self.assertEqual(state["ports"]["wp"], 8080)
        self.assertEqual(state["onion_address"], ADDR)

    def test_the_volume_gets_the_redacted_status_and_scrubbed_logs(self):
        app = self.running_app()
        with mock.patch.dict(os.environ, {"HOME": self.dir}):
            sw.write(app)
        volume = app._docker.volume
        self.assertEqual(sorted(volume), ["config-current.json", "recent-logs.txt", "status.json", "version"])
        for name, (_text, mode) in volume.items():
            self.assertEqual(mode, "600", name)
        wp_status = json.loads(volume["status.json"][0])
        self.assertLessEqual(set(wp_status), redact.WORDPRESS_STATUS_KEYS)
        self.assertNotIn("wp_admin_password_path", wp_status)
        self.assertNotIn("config", wp_status)
        self.assertNotIn(self.dir, volume["status.json"][0])
        self.assertEqual(wp_status["platform"], "linux")
        self.assertEqual(volume["version"][0], "9.9")
        config = json.loads(volume["config-current.json"][0])
        self.assertLessEqual(set(config), SAFE_CONFIG_KEYS)
        self.assertEqual(config["START_ON_BOOT"], "no")
        self.assertNotIn(TOKEN, volume["config-current.json"][0])
        self.assertNotIn("k3y", volume["config-current.json"][0])
        logs = volume["recent-logs.txt"][0]
        self.assertIn("Wrote ~/config", logs)
        self.assertTrue(logs.endswith("\n") and not logs.endswith("\n\n"))
        for leaked in (self.dir, TOKEN, "192.168.1.23"):
            self.assertNotIn(leaked, logs)
        # The host copy keeps the full status, for this user only.
        host = self.host_status()
        self.assertEqual(host["wp_admin_password_path"], os.path.join(self.dir, "wp-admin-password"))
        self.assertEqual(os.stat(os.path.join(self.dir, "status.json")).st_mode & 0o777, 0o600)
        self.assertEqual([n for n in os.listdir(self.dir) if n.endswith(".tmp")], [])

    def test_a_log_that_cannot_be_scrubbed_is_a_placeholder_never_raw(self):
        app = self.running_app()
        with mock.patch.object(sw.redact, "wordpress_log_excerpt", side_effect=OSError("io")):
            sw.write(app)
        logs = app._docker.volume["recent-logs.txt"][0]
        self.assertIn("(Recent logs unavailable: could not scrub onionpress.log for display.)", logs)
        self.assertNotIn(TOKEN, logs)
        # No log file: nothing written, the old copy stays.
        os.remove(self.log_file)
        d = self.docker()
        sw.write_recent_logs(d, self.log_file, self.dir)
        self.assertEqual(d.calls, [])

    def test_not_running_writes_the_host_copy_only(self):
        app = self.app(docker=self.docker(**{"ps -a --filter label=com.docker.compose.project": ""}))
        app.gather()
        sw.write(app)
        self.assertEqual(app._docker.volume, {})
        host = self.host_status()
        self.assertEqual(host["state"], "stopped")
        self.assertEqual(host["containers"], {})
        self.assertFalse(host["wp_installed"])
        self.assertEqual(host["uptime_seconds"], 0)
        # Docker is asked for the containers and nothing else.
        self.assertEqual(app._docker.argv(), ["ps -a --filter label=com.docker.compose.project "
                                              "--format {{.Names}}\t{{.State}}\t{{.Label \"com.docker.compose.project\"}}"])
        self.assertEqual(app._last_status_payload, host)

    def test_a_partly_stopped_stack_is_stopped(self):
        app = self.app(docker=self.docker(**{
            "ps -a --filter label=com.docker.compose.project":
                "onionpress-tor\texited\tdocker\nonionpress-wordpress\trunning\tdocker\n"}))
        app.gather()
        self.assertFalse(app.is_running)
        self.assertEqual(sw.state_of(app), "stopped")

    def test_a_write_that_fails_is_logged_never_raised(self):
        app = self.running_app()
        app._docker.raising = True
        sw.write(app)
        self.assertTrue(any(m.startswith("status: ") for m in self.logs), self.logs)
        app = self.running_app()
        with mock.patch.object(sw, "write_json_atomic", side_effect=OSError("disk")):
            sw.write(app)
        self.assertTrue(any("could not write status.json" in m for m in self.logs), self.logs)
        self.assertIn("status.json", app._docker.volume)

    def test_the_registry_is_asked_only_on_the_hub_through_overview(self):
        hostname = "exec onionpress-tor cat /var/lib/tor/hidden_service/wordpress/hostname"
        with open(os.path.join(self.dir, "onionheaven-registration.json"), "w") as f:
            json.dump({"registered": True, "hub": HUB}, f)
        with open(self.paths.config_file, "w") as f:
            f.write("REGISTER_WITH_ONIONHEAVEN=yes\n")
        queries = []

        def fake_registry(app, args, timeout=15, input=None):
            queries.append(args)
            return True, "3\t2\t1" if "COUNT" in args[-1] else ""

        with mock.patch.object(onionheaven, "_run_docker", side_effect=fake_registry):
            app = self.running_app()
            sw.write(app)
            node = app._last_status_payload
            self.assertEqual(queries, [], "the registry was queried off the hub")
            app = self.running_app(**{hostname: HUB + "\n"})
            sw.write(app)
            hub = app._last_status_payload
        self.assertEqual(node["onion_address"], ADDR)
        oh = node["onionheaven"]
        self.assertFalse(oh["server_active"])
        self.assertEqual(oh["client_hub"], HUB)
        self.assertTrue(oh["client_registered"])
        self.assertTrue(oh["client_enabled"])
        self.assertEqual((oh["registered_count"], oh["online_count"], oh["taken_over_count"]), (0, 0, 0))
        self.assertTrue(hub["onionheaven"]["server_active"])
        self.assertTrue(queries, "the hub's registry was not asked")
        self.assertTrue(all(a[:3] == ["exec", "onionheaven", "sqlite3"] or a[0] == "ps" for a in queries),
                        queries)
        self.assertEqual((hub["onionheaven"]["registered_count"], hub["onionheaven"]["online_count"],
                          hub["onionheaven"]["taken_over_count"]), (3, 2, 1))
        self.assertIsNotNone(hub["onionheaven"]["hub"])
        # Registered with a hub and not reachable: visitors see Wayback.
        self.reachability(reachable=False)
        app = self.app()
        app.gather()
        sw.write(app)
        self.assertEqual(app._last_status_payload["state"], "starting")
        self.assertTrue(app._last_status_payload["serving_from_wayback"])

    def test_an_overview_that_raises_falls_back_to_the_client_half(self):
        app = self.running_app()
        with mock.patch.object(onionheaven, "hub_overview", side_effect=RuntimeError("boom")):
            sw.write(app)
        self.assertTrue(any("OnionHeaven: overview error: boom" in m for m in self.logs))
        self.assertIsNone(app._last_status_payload["onionheaven"]["hub"])
        self.assertIn("status.json", app._docker.volume)

    def test_only_a_whole_address_is_carried(self):
        for loose in ("Generating...", ADDR.upper(), ADDR[:-6], "evil.onion\n"):
            with self.subTest(loose=loose):
                app = self.running_app(**{
                    "exec onionpress-tor cat /var/lib/tor/hidden_service/wordpress/hostname": loose + "\n"})
                sw.write(app)
                self.assertEqual(app._last_status_payload["onion_address"], "")
                self.assertEqual(app._last_status_payload["state"], "starting")

    def test_a_mac_says_so(self):
        self.reachability()
        app = self.app(os_name=OS.MACOS)
        app.gather()
        with mock.patch.object(sw.subprocess, "run", return_value=mock.Mock(stdout="sec = 1")):
            sw.write(app)
        self.assertEqual(app._last_status_payload["platform"], "macos")


# ---------------------------------------------------------------------------
# The one-shot pass (Linux until #407)
# ---------------------------------------------------------------------------

class TestOneShot(_Sandbox):

    def test_states(self):
        self.reachability()
        app = self.app()
        app.gather()
        self.assertTrue(app.is_running and app.is_ready)
        self.assertEqual(sw.state_of(app, app.now), "running")
        self.assertEqual(app._last_bootstrap_pct, 100)
        # Bootstrapping: starting, the percentage shown.
        app = self.app(health=self.health((False, 45)))
        app.gather()
        self.assertEqual(sw.state_of(app, app.now), "starting")
        self.assertEqual(app._last_bootstrap_pct, 45)
        self.assertEqual(app._last_reachability, (None, None))
        # Bootstrapped, the heartbeat says unreachable: starting.
        self.reachability(reachable=False, code="000:rc=28")
        app = self.app()
        app.gather()
        self.assertEqual(sw.state_of(app, app.now), "starting")
        self.assertEqual(app._last_reachability, (False, "000:rc=28"))
        # No fresh verdict: nothing known, starting; never a probe of its own.
        self.reachability(age=1000)
        app = self.app()
        app.gather()
        self.assertEqual(sw.state_of(app, app.now), "starting")
        self.assertEqual(app._last_reachability, (None, None))
        self.assertFalse(any("curl" in a or ".onionpress-health" in a for a in app._docker.argv()))
        # No containers: stopped, the cached address shown.
        with open(os.path.join(self.dir, "onion_address"), "w") as f:
            f.write(ADDR)
        app = self.app(docker=self.docker(**{"ps -a --filter label=com.docker.compose.project": ""}))
        app.gather()
        self.assertEqual(sw.state_of(app, app.now), "stopped")
        self.assertEqual(app.onion_address, ADDR)

    def test_the_hub_by_address(self):
        app = self.app(docker=self.docker(**{
            "exec onionpress-tor cat /var/lib/tor/hidden_service/wordpress/hostname": HUB + "\n"}))
        app.gather()
        self.assertTrue(app.is_onionheaven)
        app = self.app()
        app.gather()
        self.assertFalse(app.is_onionheaven)

    def test_the_bash_write_statuss_log_lines(self):
        now = 1_000_000.0
        self.reachability(now=now)
        self.app(now=now).gather()
        self.assertEqual(self.logs, [f"Onion service is now reachable at http://{ADDR}/"])
        self.app(now=now + 30).gather()
        self.assertEqual(len(self.logs), 1, "the same state again says nothing")
        self.reachability(now=now + 60, reachable=False)
        self.app(now=now + 60).gather()
        self.assertEqual(self.logs[-1], "Onion service is no longer reachable (state: starting)")
        self.app(now=now + 90, docker=self.docker(**{"ps -a --filter label=com.docker.compose.project": ""})).gather()
        self.assertEqual(self.logs[-1], "OnionPress stopped")
        self.app(now=now + 120, docker=self.docker(**{"ps -a --filter label=com.docker.compose.project": ""})).gather()
        self.assertEqual(len(self.logs), 3)
        with open(os.path.join(self.dir, sw.LAST_STATE_FILE)) as f:
            self.assertEqual(f.read(), "stopped\n")

    def test_one_wedge_warning_after_ten_minutes_starting(self):
        now = 1_000_000.0
        self.reachability(now=now, reachable=False)
        self.app(now=now).gather()
        with open(os.path.join(self.dir, sw.YELLOW_SINCE_FILE)) as f:
            self.assertEqual(f.read().strip(), str(int(now)))
        self.reachability(now=now + 599, reachable=False)
        app = self.app(now=now + 599)
        app.gather()
        self.assertFalse(any("WEDGE" in m for m in self.logs))
        self.assertEqual(sw.state_of(app, app.now), "stuck", "the daemon's rule: 5 min yellow")
        self.reachability(now=now + 600, reachable=False)
        app = self.app(now=now + 600)
        app.gather()
        wedge = [m for m in self.logs if m.startswith("WEDGE WARNING")]
        self.assertEqual(wedge, [sw.wedge_warning(OS.LINUX)])
        self.assertIn("systemctl --user restart onionpress", wedge[0])
        self.assertTrue(app._wedge_warning_fired)
        self.assertTrue(os.path.exists(os.path.join(self.dir, sw.WEDGE_MARKER)))
        self.reachability(now=now + 700, reachable=False)
        self.app(now=now + 700).gather()
        self.assertEqual(len([m for m in self.logs if m.startswith("WEDGE WARNING")]), 1)
        # Reachable again: the memory is cleared.
        self.reachability(now=now + 800)
        app = self.app(now=now + 800)
        app.gather()
        self.assertIsNone(app._yellow_since)
        self.assertFalse(app._wedge_warning_fired)
        for name in (sw.YELLOW_SINCE_FILE, sw.WEDGE_MARKER):
            self.assertFalse(os.path.exists(os.path.join(self.dir, name)), name)
        self.assertIn("recovery: see the `probe verdict=`", sw.wedge_warning(OS.MACOS))

    def test_the_port_offset_is_the_launchers(self):
        with mock.patch.dict(os.environ, {"ONIONPRESS_PORT_OFFSET": "10000"}):
            app = self.app()
        self.assertEqual((app.wp_port, app.socks_port, app.proxy_port, app.port_offset),
                         (18080, 19050, 19077, 10000))
        with mock.patch.dict(os.environ, {"ONIONPRESS_PORT_OFFSET": "x"}):
            self.assertEqual(self.app().wp_port, 8080)

    def test_run_once_writes_both_documents_and_exits_0(self):
        self.reachability()
        d = self.docker()
        with mock.patch.dict(os.environ, {"ONIONPRESS_VERSION": "7.7"}):
            rc = sw.run_once(self.paths, self.log, docker=d, health=self.health(), os_name=OS.LINUX)
        self.assertEqual(rc, 0)
        state = read_state(self.dir)
        self.assertEqual(state["status"]["state"], "running")
        self.assertEqual(state["status"]["version"], "7.7")
        self.assertEqual(state["display_state"], "available")
        self.assertEqual(self.host_status()["version"], "7.7")
        self.assertEqual(d.volume["version"][0], "7.7")
        self.assertEqual(os.stat(os.path.join(self.dir, "daemon-state.json")).st_mode & 0o777, 0o600)

    def test_run_once_without_docker_still_writes_what_it_knows(self):
        rc = sw.run_once(self.paths, self.log, docker=FakeDocker(raising=True), health=self.health(),
                         os_name=OS.LINUX, version="1")
        self.assertEqual(rc, 0)
        self.assertTrue(any(m.startswith("status: could not read the stack's state") for m in self.logs))
        self.assertEqual(self.host_status()["state"], "stopped")
        self.assertEqual(read_state(self.dir)["status"]["state"], "stopped")

    def test_the_cli_fast_path(self):
        self.reachability()
        buf = io.StringIO()
        with mock.patch.object(cli, "OnionPressCLI", side_effect=AssertionError("setup ran")), \
                mock.patch("onionpress.docker.Docker", return_value=self.docker()), \
                mock.patch("onionpress.health.HealthChecker", return_value=self.health()), \
                mock.patch.object(sw, "detect_os", return_value=OS.LINUX), \
                mock.patch.dict(os.environ, {"ONIONPRESS_VERSION": "7.7"}), \
                mock.patch("sys.stdout", buf):
            rc = cli.main(["--data-dir", self.dir, "write-status"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.host_status()["state"], "running")
        self.assertRegex(buf.getvalue(), r"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\] Onion service is now reachable")
        self.assertEqual([n for n in os.listdir(self.dir) if n in ("secrets",)], [],
                         "the fast path ran OnionPressCLI's setup")


# ---------------------------------------------------------------------------
# One writer: the wiring
# ---------------------------------------------------------------------------

class TestWiring(unittest.TestCase):

    def test_the_daemon_is_the_writers(self):
        daemon = _read("src/onionpress/daemon.py")
        self.assertIn("        status_writer.write(self)\n", daemon)
        self.assertIn("                status_writer.publish(self)\n", daemon)
        self.assertIn("        return status_writer.state_document(self, now)\n", daemon)
        self.assertIn("        return status_writer.list_containers(self._docker)\n", daemon)
        self.assertIn("        return status_writer.wp_installed(self._docker)\n", daemon)
        for gone in ("def _host_boot_time", '"platform": "macos"', "ps --format {{.Names}}\\t{{.State}}",
                     "redact.status_for_wordpress", "wordpress_log_excerpt"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, daemon)

    def test_the_linux_launcher_delegates(self):
        launcher = _read("linux/onionpress")
        start = launcher.index("\nwrite_status() {")
        body = launcher[start:launcher.index("\n}\n", start)]
        self.assertTrue(body.endswith('\n    op_py --data-dir "$DATA_DIR" write-status 2>&1 | tee -a "$LOG_FILE"'), body)
        self.assertIn("_IN_WRITE_STATUS=1 apply_config_updates", body)
        self.assertNotIn("docker", body, "the bash is the delegation and the config guard")
        for gone in ("write_recent_logs", "linux-status", "sqlite3 /var/lib/onionheaven/registry.db",
                     "WEDGE WARNING", ".onionpress-health", "started_epoch", "wordpress-status",
                     "wordpress-logs", "reachability.json"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, launcher)
        self.assertNotIn("def write_recent_logs", _read("src/onionpress/handle_action.py"))
        self.assertFalse(os.path.exists(os.path.join(PROJECT_ROOT, "src", "onionpress", "linux_status.py")))

    def test_one_writer_of_the_shared_files(self):
        """status.json, recent-logs.txt, version and config-current.json
        reach the volume from status_writer and nowhere else."""
        sites = {}
        for root in ("src", "linux", "app/MacOS"):
            for dirpath, _dirs, files in os.walk(os.path.join(PROJECT_ROOT, root)):
                for name in files:
                    if name.endswith((".py", ".pyc")) and not name.endswith(".py"):
                        continue
                    path = os.path.join(dirpath, name)
                    try:
                        text = _read(os.path.relpath(path, PROJECT_ROOT))
                    except (OSError, UnicodeDecodeError):
                        continue
                    for shared in ("status.json", "recent-logs.txt", "config-current.json"):
                        if re.search(r'(_put\(docker, |/var/lib/onionpress/)"?' + re.escape(shared), text):
                            sites.setdefault(shared, []).append(os.path.relpath(path, PROJECT_ROOT))
        for shared, where in sites.items():
            with self.subTest(shared=shared):
                self.assertEqual(where, ["src/onionpress/status_writer.py"])
        writer = _read("src/onionpress/status_writer.py")
        self.assertIn("json.dumps(redact.status_for_wordpress(status), indent=2)", writer)
        self.assertIn("redact.wordpress_log_excerpt(log_file, data_dir)", writer)
        self.assertIn("onionheaven.status_block(overview, server_active=bool(app.is_onionheaven))", writer)
        self.assertIn("onionheaven.overview(app, hub=bool(app.is_running))", writer)
        self.assertNotIn("sqlite3", writer)
        self.assertNotIn("curl", writer)

    def test_the_tray_reads_the_daemons_state_file(self):
        tray = _read("linux/onionpress-tray")
        self.assertIn("STATE_FILE = daemon_client.state_path(DATA_DIR)", tray)
        self.assertIn("state = daemon_client.read_state(DATA_DIR)", tray)
        self.assertNotIn("status.json\")", tray)
        self.assertNotIn("json.load", tray)
        self.assertNotIn("STATUS_FILE", tray)
        tree = ast.parse(tray)
        opened = [n.lineno for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "open"
                  and n.args and isinstance(n.args[0], ast.Name) and n.args[0].id.endswith("FILE")]
        self.assertEqual(opened, [], "the tray opens a status file itself")

    def test_the_bundle_and_the_dispatch_know_it(self):
        self.assertIn("'onionpress.status_writer'", _read("setup.py"))
        self.assertNotIn("linux_status", _read("setup.py"))
        self.assertIn('("write-status",', _read("src/onionpress/cli.py"))
        self.assertNotIn("linux-status", _read("src/onionpress/cli.py"))


if __name__ == "__main__":
    unittest.main()

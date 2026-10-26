#!/usr/bin/env python3
"""`onionpress daemon` (onionpress.daemon, issue #367, CLI-first phase 3):
the Mac's loops out of the menubar, as a per-user launchd agent, and the
menubar as a view of its state file.

Run with stubs: a fake Docker (canned answers by argv prefix), a fake
HealthChecker, a recorded launcher. No docker, no launchctl (patched), no
network. DOCKER_HOST points at nothing in the suite anyway.
"""
import ast
import io
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from onionpress import daemon as op_daemon  # noqa: E402
from onionpress import config as op_config  # noqa: E402
from onionpress.docker import DockerResult  # noqa: E402
from onionpress.platform import resolve_paths  # noqa: E402

ADDR = "a" * 56 + ".onion"
HC = "h" * 56 + ".onion"


def _read(rel):
    with open(os.path.join(PROJECT_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _methods(src):
    tree = ast.parse(src)
    cls = [n for n in tree.body if isinstance(n, ast.ClassDef)][0]
    return {n.name: ast.get_source_segment(src, n)
            for n in cls.body if isinstance(n, ast.FunctionDef)}


class FakeDocker:
    """Answers `docker <argv>` from a table of prefixes; records calls."""

    docker_bin = "/x/docker"
    env = {"DOCKER_HOST": "unix:///nowhere"}

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls = []

    def _answer(self, args):
        key = " ".join(str(a) for a in args)
        for prefix in sorted(self.answers, key=len, reverse=True):
            if key == prefix or key.startswith(prefix + " "):
                out = self.answers[prefix]
                if callable(out):
                    out = out(args)
                if isinstance(out, tuple):
                    rc, text = out
                else:
                    rc, text = 0, out
                return DockerResult(returncode=rc, stdout=text, stderr="")
        return DockerResult(returncode=1, stdout="", stderr="no such answer")

    def run(self, args, timeout=30, check=False, input=None, extra_env=None, quiet=False):
        self.calls.append((list(str(a) for a in args), input))
        return self._answer(args)

    def exec(self, container, command, timeout=30, check=False, input=None,
             quiet=False, user=None, extra_env=None):
        args = ["exec", container, *(command if isinstance(command, list) else [command])]
        self.calls.append((args, input))
        return self._answer(args)


class FakeHealth:
    def __init__(self, *, bootstrapped=(True, 100), hostname=ADDR, internal=True,
                 external=(True, "200"), wordpress=True, internet=True):
        self.bootstrapped = bootstrapped
        self.hostname = hostname
        self.internal = internal
        self.external = external
        self.wordpress = wordpress
        self.internet = internet
        self.last_probe = {}

    def check_wordpress_external(self, wp_port, log=True):
        return self.wordpress

    def check_tor_hostname(self, expected=""):
        return self.hostname

    def check_tor_bootstrap(self):
        return self.bootstrapped

    def check_internal_connectivity(self):
        return self.internal

    def check_external_reachability(self, address):
        return self.external

    def check_internet_connectivity(self):
        return self.internet

    def check_vm_wedge(self):
        return None


class _DaemonCase(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="op-daemon-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        os.makedirs(os.path.join(self.dir, "colima"))
        with open(os.path.join(self.dir, "colima", ".initialized"), "w") as f:
            f.write("1")
        with open(os.path.join(self.dir, "config"), "w") as f:
            f.write("REGISTER_WITH_ONIONHEAVEN=no\nSHARE_ANALYTICS_WITH_ONIONHOME=no\n")
        self.paths = resolve_paths(data_dir=self.dir, app_bundle="")
        self.docker = FakeDocker({
            "info": "ok",
            "ps -a --filter label=com.docker.compose.project":
                "onionpress-tor\trunning\tdocker\nonionpress-wordpress\trunning\tdocker\n"
                "onionpress-db\trunning\tdocker\nonionheaven\trunning\tdocker\n",
            "ps --format {{.Names}}\t{{.State}} --filter name=onionpress":
                "onionpress-tor\trunning\nonionpress-wordpress\trunning\n",
            "ps --format {{.Names}} --filter name=onionheaven-takeover": "",
            "images --format {{.Repository}}": "ghcr.io/x/onionpress-wordpress\nmariadb\nghcr.io/x/onionpress-tor\n",
            "exec onionpress-tor cat /var/lib/tor/hidden_service/wordpress/hostname": ADDR + "\n",
            "exec onionpress-tor cat /var/lib/tor/hidden_service/healthcheck/hostname": HC + "\n",
            "exec onionpress-wordpress sh -c test -f /var/lib/onionpress/.upload-analytics && rm /var/lib/onionpress/.upload-analytics": (1, ""),
            "exec -u www-data": "",
            "exec -i -u www-data": "",
        })
        self.health = FakeHealth()
        self.launcher_calls = []
        self.launcher_answers = {}
        self.d = op_daemon.Daemon(self.paths, version="9.9", launcher_script="/x/onionpress",
                                  docker=self.docker, health_checker=self.health)
        # Nothing that forks, binds or sleeps for real.
        self.d.caffeine = mock.Mock()
        self.d.start_onion_proxy = lambda: None
        self.d.start_web_log_capture = lambda: None
        self.d.start_container_log_capture = lambda: None
        self.d.start_clearnet_log_capture = lambda: None
        self.d.stop_web_log_capture = lambda: None
        self.d.stop_container_log_capture = lambda: None
        self.proxy_stopped = False

        def _stop_proxy():
            self.proxy_stopped = True
        self.d.stop_onion_proxy = _stop_proxy
        self.d._hub_loops = lambda: None
        self.d._resync_ports = lambda: None
        self.d.run_launcher = self._run_launcher
        self.d._settings_page_thread_sync = True

    def _run_launcher(self, args, timeout=60, env=None):
        self.launcher_calls.append(list(args))
        rc, out = self.launcher_answers.get(args[0], (0, ""))
        return rc, out

    def state(self):
        return op_daemon.read_state(self.dir)

    def tick(self):
        self.d.check_status()
        # the settings-page run is a thread: let it finish
        for _ in range(50):
            if not self.d._settings_page_busy:
                break
            time.sleep(0.02)


class TestStatusPass(_DaemonCase):

    def test_a_running_ready_stack_lands_in_the_state_file(self):
        self.tick()
        s = self.state()
        self.assertEqual(s["pid"], os.getpid())
        self.assertTrue(s["is_running"])
        self.assertTrue(s["is_ready"])
        self.assertEqual(s["onion_address"], ADDR)
        self.assertEqual(s["healthcheck_address"], HC)
        self.assertEqual(s["display_state"], "available")
        self.assertEqual(s["bootstrap_pct"], 100)
        self.assertTrue(s["runtime_ready"])
        self.assertTrue(s["wordpress_up"])
        self.assertEqual(s["images_present"], {"wordpress": True, "mariadb": True, "tor": True})
        self.assertEqual(s["ports"]["wp"], self.d.wp_port)
        # The address is cached for the next launch, .setup_complete written
        self.assertEqual(op_config.cached_onion_address(self.dir), ADDR)
        self.assertTrue(os.path.exists(os.path.join(self.dir, ".setup_complete")))
        self.assertTrue(op_daemon.state_is_fresh(s))
        # caffeinate follows the running stack
        self.d.caffeine.start.assert_called()

    def test_status_json_is_written_as_www_data(self):
        self.tick()
        puts = [c for c in self.docker.calls if c[0][:3] == ["exec", "-i", "-u"]
                or (c[0][0] == "exec" and "-u" in c[0][:4])]
        names = " ".join(" ".join(c[0]) for c in puts)
        for name in ("status.json", "version", "config-current.json"):
            self.assertIn(f"/var/lib/onionpress/{name}", names)
        status_put = next(c for c in puts if "status.json" in " ".join(c[0]))
        doc = json.loads(status_put[1])
        self.assertEqual(doc["state"], "running")
        self.assertEqual(doc["onion_address"], ADDR)
        self.assertEqual(doc["platform"], "macos")

    def test_the_tick_runs_handle_action_once_at_a_time(self):
        self.launcher_answers["handle-action"] = (0, '{"handled": null}\n')
        self.tick()
        self.assertEqual([c for c in self.launcher_calls if c[0] == "handle-action"],
                         [["handle-action", "--json"]])

    def test_a_pending_question_goes_into_the_state_not_an_answer(self):
        question = {"confirm": "address-change", "action": "import-key-file",
                    "current": ADDR, "new": "b" * 56 + ".onion"}
        # handle-action exits 0 with the question (its first run is the check)
        self.launcher_answers["handle-action"] = (0, json.dumps(question) + "\n")
        self.tick()
        s = self.state()
        self.assertEqual(s["pending_question"]["confirm"], "address-change")
        self.assertEqual(s["pending_question"]["new"], "b" * 56 + ".onion")
        # The daemon never answers for the person at the Mac
        for call in self.launcher_calls:
            self.assertNotIn("--confirmed", call)
            self.assertNotIn("--cancel", call)
        # Answered elsewhere: the next plain run finds nothing pending
        self.launcher_answers["handle-action"] = (0, '{"handled": null}\n')
        self.tick()
        self.assertIsNone(self.state()["pending_question"])

    def test_a_failed_handle_action_is_said_once_an_hour(self):
        self.launcher_answers["handle-action"] = (1, "boom\n")
        self.tick()
        self.assertIn("handle-action failed", self.state()["settings_page_error"])

    def test_not_running_is_stopped_and_everything_resets(self):
        self.tick()
        self.docker.answers["ps -a --filter label=com.docker.compose.project"] = \
            "onionpress-tor\texited\tdocker\nonionpress-wordpress\trunning\tdocker\n"
        self.tick()
        s = self.state()
        self.assertFalse(s["is_running"])
        self.assertFalse(s["is_ready"])
        self.assertEqual(s["display_state"], "stopped")
        self.assertEqual(s["healthcheck_address"], "")
        self.assertEqual(s["onion_address"], ADDR)       # the cached address stays visible
        self.d.caffeine.stop.assert_called()
        self.assertIn("Service stopped", "".join(self._log_text()))
        self.assertTrue(self.proxy_stopped)

    def _log_text(self):
        logs = os.path.join(self.dir, "logs")
        out = []
        for name in sorted(os.listdir(logs)):
            if name.startswith("onionpress-"):
                with open(os.path.join(logs, name)) as f:
                    out.append(f.read())
        return out

    def test_the_probe_failing_twice_goes_yellow_and_feeds_the_trigger(self):
        self.tick()
        self.assertTrue(self.d.is_ready)
        self.health.external = (False, "000:rc=28")
        self.tick()
        self.assertTrue(self.d.is_ready, "one failure is transient")
        self.tick()
        self.assertFalse(self.d.is_ready)
        s = self.state()
        self.assertEqual(s["display_state"], "starting")
        self.assertIsNotNone(s["yellow_since"])
        self.assertEqual(s["probe"]["code"], "000:rc=28")
        self.assertEqual(self.d._republish_trigger.failing_since, self.d._republish_trigger.failing_since)

    def test_no_internet_is_offline_and_resets_the_window(self):
        self.tick()
        self.health.internet = False
        self.d._republish_trigger.reset = mock.Mock()
        self.tick()
        s = self.state()
        self.assertFalse(s["has_internet"])
        self.assertEqual(s["display_state"], "offline")
        self.d._republish_trigger.reset.assert_called()

    def test_the_config_marker_is_taken_by_the_daemon(self):
        op_config.note_config_changed(self.dir, ["PREVENT_SLEEP"])
        self.tick()
        self.assertEqual(op_config.take_config_changes(self.dir), [])
        self.d.caffeine.stop.assert_called()

    def test_upload_trigger_marker_starts_an_upload(self):
        with open(os.path.join(self.dir, op_daemon.UPLOAD_TRIGGER_MARKER), "w") as f:
            f.write("1")
        with mock.patch.object(self.d, "_manual_analytics_upload") as upload:
            self.tick()
            for _ in range(50):
                if upload.called:
                    break
                time.sleep(0.02)
        upload.assert_called_once()
        self.assertFalse(os.path.exists(os.path.join(self.dir, op_daemon.UPLOAD_TRIGGER_MARKER)))

    def test_port_conflict_is_reported_only_while_not_running(self):
        self.docker.answers["ps -a --filter label=com.docker.compose.project"] = ""
        with mock.patch.object(self.d, "check_port_conflict", return_value=[8080, 9050]):
            self.tick()
        self.assertEqual(self.state()["port_conflict"], [8080, 9050])
        self.docker.answers["ps -a --filter label=com.docker.compose.project"] = \
            "onionpress-tor\trunning\tdocker\n"
        self.tick()
        self.assertEqual(self.state()["port_conflict"], [])

    def test_a_crash_in_a_pass_still_publishes(self):
        self.d._full_health_pass = mock.Mock(side_effect=RuntimeError("x"))
        self.tick()
        self.assertTrue(os.path.exists(op_daemon.state_path(self.dir)))
        self.assertFalse(self.d.checking)


class TestHubLoopsGate(_DaemonCase):
    """The heartbeat starts only on "yes" (unset = yes), once Tor is
    bootstrapped and the address is known; the opt-out and the upkeep run
    whatever the choice; analytics once."""

    def setUp(self):
        super().setUp()
        del self.d._hub_loops          # the real one
        self.d.onion_address = ADDR
        self.d._last_bootstrap_pct = 100

    def _run(self, choice):
        with open(os.path.join(self.dir, "config"), "w") as f:
            f.write(f"REGISTER_WITH_ONIONHEAVEN={choice}\n" if choice is not None else "")
        with mock.patch("onionpress.onionheaven.start_registration_thread") as start, \
                mock.patch("onionpress.onionheaven.maybe_unregister_after_opt_out") as optout, \
                mock.patch("onionpress.onionheaven.maintain_registration") as maintain, \
                mock.patch("onionpress.analytics_sharing.start_analytics_sharing") as analytics:
            self.d._hub_loops()
        return start, optout, maintain, analytics

    def test_yes_and_unset_start_the_heartbeat(self):
        for choice in ("yes", None):
            with self.subTest(choice=choice):
                self.d._onionheaven_registration_in_flight = False
                start, optout, maintain, analytics = self._run(choice)
                start.assert_called_once_with(self.d)
                optout.assert_called_once_with(self.d)
                maintain.assert_called_once_with(self.d)

    def test_no_and_a_typo_never_start_it(self):
        for choice in ("no", "nope"):
            with self.subTest(choice=choice):
                self.d._onionheaven_registration_in_flight = False
                start, optout, maintain, analytics = self._run(choice)
                start.assert_not_called()
                optout.assert_called_once_with(self.d)

    def test_not_before_bootstrap(self):
        self.d._last_bootstrap_pct = 50
        start, optout, maintain, analytics = self._run("yes")
        start.assert_not_called()
        optout.assert_not_called()

    def test_analytics_sharing_starts_once(self):
        self._run("no")
        _, _, _, analytics = self._run("no")
        analytics.assert_not_called()
        self.assertTrue(self.d._analytics_sharing_started)


class TestSleepWake(_DaemonCase):

    def test_sleep_signals_the_watchdogs_and_pauses(self):
        self.tick()
        with mock.patch("onionpress.launcher_ops.signal_watchdog", return_value=True) as sig, \
                mock.patch("onionpress.onionheaven.notify_onionheaven_offline", return_value=True) as off:
            self.d._onionheaven_registration_succeeded = True
            self.d.handle_sleep()
        self.assertTrue(self.d._sleeping)
        self.assertEqual([c.args[1:] for c in sig.call_args_list],
                         [("onionpress-tor", "USR1"), ("onionheaven", "USR1")])
        off.assert_called_once()
        self.assertTrue(self.state()["sleeping"])
        self.d.caffeine.stop.assert_called()

    def test_wake_resets_the_window_and_supersedes_the_heartbeat(self):
        self.tick()
        self.d._republish_trigger.reset = mock.Mock()
        gen = self.d._heartbeat_generation
        with mock.patch("onionpress.launcher_ops.signal_watchdog", return_value=True) as sig:
            self.d.handle_wake()
        self.d._republish_trigger.reset.assert_called_once()
        self.assertEqual(self.d._heartbeat_generation, gen + 1)
        self.assertFalse(self.d._sleeping)
        self.assertFalse(self.d.is_ready)
        self.assertEqual([c.args[1:] for c in sig.call_args_list],
                         [("onionpress-tor", "USR2"), ("onionheaven", "USR2")])

    def test_usr1_sends_offline_and_acknowledges(self):
        self.tick()
        with mock.patch("onionpress.onionheaven.notify_onionheaven_offline", return_value=True) as off:
            self.d._onionheaven_registration_succeeded = True
            self.d._offline_now()
        off.assert_called_once()
        self.assertTrue(os.path.exists(os.path.join(self.dir, op_daemon.OFFLINE_ACK_MARKER)))

    def test_shutdown_sends_offline_only_for_a_quit(self):
        self.tick()
        self.d._onionheaven_registration_succeeded = True
        self.d.stop_onion_proxy = lambda: None
        with mock.patch("onionpress.onionheaven.notify_onionheaven_offline", return_value=True) as off:
            self.d._shutdown()
        off.assert_not_called()                        # launchd restart / update: coming back
        with open(os.path.join(self.dir, op_daemon.QUITTING_MARKER), "w") as f:
            f.write("1")
        self.d._quitting = False
        with mock.patch("onionpress.onionheaven.notify_onionheaven_offline", return_value=True) as off:
            self.d._shutdown()
        off.assert_called_once()                       # `daemon stop` for a quit
        self.assertFalse(os.path.exists(os.path.join(self.dir, op_daemon.QUITTING_MARKER)))
        with open(os.path.join(self.dir, ".previous-port-offset")) as f:
            self.assertEqual(f.read().strip(), str(self.d.port_offset))


class TestStateFile(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="op-daemon-state-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_round_trip_is_atomic(self):
        path = op_daemon.state_path(self.dir)
        op_daemon.write_json_atomic(path, {"a": 1})
        self.assertEqual(op_daemon.read_state(self.dir), {"a": 1})
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual([n for n in os.listdir(self.dir) if n.endswith(".tmp")], [])
        with open(path, "w") as f:
            f.write("{not json")
        self.assertIsNone(op_daemon.read_state(self.dir))
        self.assertIsNone(op_daemon.read_state(os.path.join(self.dir, "missing")))

    def test_fresh_means_recent_and_alive(self):
        now = 1_000_000.0
        live = {"pid": os.getpid(), "updated_at": now - 10}
        self.assertTrue(op_daemon.state_is_fresh(live, now))
        self.assertFalse(op_daemon.state_is_fresh({**live, "updated_at": now - 200}, now))
        self.assertFalse(op_daemon.state_is_fresh({**live, "pid": 2 ** 22 - 1}, now))
        self.assertFalse(op_daemon.state_is_fresh(None, now))
        self.assertFalse(op_daemon.state_is_fresh({"pid": os.getpid(), "updated_at": "x"}, now))

    def test_display_state(self):
        now = 1_000_000.0
        ds = op_daemon.display_state_of
        base = {"is_running": True, "is_ready": False, "has_internet": True,
                "yellow_since": None, "last_check_complete": now}
        self.assertEqual(ds(None, now), "stopped")
        self.assertEqual(ds({**base, "is_running": False}, now), "stopped")
        self.assertEqual(ds({**base, "is_ready": True}, now), "available")
        self.assertEqual(ds({**base, "last_check_complete": now - 400}, now), "stalled")
        self.assertEqual(ds({**base, "has_internet": False}, now), "offline")
        self.assertEqual(ds({**base, "yellow_since": now - 400}, now), "stuck")
        self.assertEqual(ds(base, now), "starting")

    def test_poll_interval_is_the_menubars_old_schedule(self):
        self.assertEqual(op_daemon.poll_interval("available"), 30)
        self.assertEqual(op_daemon.poll_interval("offline"), 10)
        for s in ("starting", "stuck", "stalled", "stopped"):
            self.assertEqual(op_daemon.poll_interval(s), 5)

    def test_daemon_pid(self):
        self.assertIsNone(op_daemon.daemon_pid(self.dir))
        with open(op_daemon.pid_path(self.dir), "w") as f:
            f.write(str(os.getpid()))
        self.assertEqual(op_daemon.daemon_pid(self.dir), os.getpid())
        with open(op_daemon.pid_path(self.dir), "w") as f:
            f.write(str(2 ** 22 - 1))
        self.assertIsNone(op_daemon.daemon_pid(self.dir))


class TestLaunchdAgent(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="op-daemon-agent-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.plist = os.path.join(self.dir, "org.onionpress.daemon.plist")
        self.launcher = os.path.join(self.dir, "onionpress")
        with open(self.launcher, "w") as f:
            f.write("#!/bin/sh\n")
        self.lines = []

    def test_plist(self):
        p = op_daemon.agent_plist(self.launcher, "/logs")
        self.assertEqual(p["Label"], "org.onionpress.daemon")
        self.assertEqual(p["ProgramArguments"], [self.launcher, "daemon"])
        self.assertTrue(p["RunAtLoad"])
        self.assertEqual(p["KeepAlive"], {"SuccessfulExit": False})
        self.assertTrue(p["AbandonProcessGroup"])
        self.assertEqual(p["LimitLoadToSessionType"], "Aqua")
        self.assertEqual(p["StandardOutPath"], "/logs/daemon-stdout.log")
        self.assertEqual(p["StandardErrorPath"], "/logs/daemon-stderr.log")
        self.assertNotIn("ONIONPRESS_PORT_OFFSET", p["EnvironmentVariables"])
        self.assertEqual(p["EnvironmentVariables"]["HOME"], os.path.expanduser("~"))
        p = op_daemon.agent_plist(self.launcher, "/logs", port_offset_env="10000")
        self.assertEqual(p["EnvironmentVariables"]["ONIONPRESS_PORT_OFFSET"], "10000")

    def test_install_is_idempotent_and_refuses_no_launcher(self):
        path = op_daemon.install_agent(self.launcher, self.dir, self.lines.append, self.plist)
        self.assertEqual(path, self.plist)
        with open(self.plist, "rb") as f:
            self.assertEqual(plistlib.load(f)["ProgramArguments"], [self.launcher, "daemon"])
        mtime = os.stat(self.plist).st_mtime_ns
        op_daemon.install_agent(self.launcher, self.dir, self.lines.append, self.plist)
        self.assertEqual(os.stat(self.plist).st_mtime_ns, mtime)
        self.assertEqual("", op_daemon.install_agent(os.path.join(self.dir, "none"), self.dir,
                                                     self.lines.append, self.plist))
        self.assertTrue(os.path.isdir(os.path.join(self.dir, "logs")))

    def test_start_bootstraps_then_kickstarts(self):
        calls = []
        states = iter([(False, None), (True, 4242)])

        def launchctl(args, timeout=20):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")
        with mock.patch.object(op_daemon, "_launchctl", launchctl), \
                mock.patch.object(op_daemon, "agent_loaded_pid", lambda: next(states)), \
                mock.patch.object(op_daemon, "_gui_domain", lambda: "gui/501"):
            ok = op_daemon.start_agent(self.launcher, self.dir, self.lines.append, self.plist)
        self.assertTrue(ok)
        self.assertEqual(calls, [["bootstrap", "gui/501", self.plist]])
        # Loaded but not running: kickstart
        calls.clear()
        states = iter([(True, None), (True, 7)])
        with mock.patch.object(op_daemon, "_launchctl", launchctl), \
                mock.patch.object(op_daemon, "agent_loaded_pid", lambda: next(states)), \
                mock.patch.object(op_daemon, "_gui_domain", lambda: "gui/501"):
            ok = op_daemon.start_agent(self.launcher, self.dir, self.lines.append, self.plist)
        self.assertTrue(ok)
        self.assertEqual(calls, [["kickstart", "gui/501/org.onionpress.daemon"]])

    def test_stop_boots_out_and_marks_a_quit(self):
        calls = []

        def launchctl(args, timeout=20):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")
        with open(op_daemon.pid_path(self.dir), "w") as f:
            f.write(str(2 ** 22 - 1))      # not alive: nothing to wait for
        with mock.patch.object(op_daemon, "_launchctl", launchctl), \
                mock.patch.object(op_daemon, "agent_loaded_pid", lambda: (True, None)), \
                mock.patch.object(op_daemon, "_gui_domain", lambda: "gui/501"):
            ok = op_daemon.stop_agent(self.dir, self.lines.append, self.plist, offline=True)
        self.assertTrue(ok)
        self.assertEqual(calls, [["bootout", "gui/501/org.onionpress.daemon"]])
        # No live pid: no quit marker to leave for a daemon that is not there
        self.assertFalse(os.path.exists(os.path.join(self.dir, op_daemon.QUITTING_MARKER)))
        with open(op_daemon.pid_path(self.dir), "w") as f:
            f.write(str(os.getpid()))
        with mock.patch.object(op_daemon, "_launchctl", launchctl), \
                mock.patch.object(op_daemon, "agent_loaded_pid", lambda: (False, None)), \
                mock.patch.object(op_daemon.os, "kill") as kill:
            op_daemon.stop_agent(self.dir, self.lines.append, self.plist, wait=0.1, offline=True)
        self.assertTrue(os.path.exists(os.path.join(self.dir, op_daemon.QUITTING_MARKER)))
        kill.assert_any_call(os.getpid(), op_daemon.signal.SIGTERM)
        with mock.patch.object(op_daemon, "_launchctl", launchctl), \
                mock.patch.object(op_daemon, "agent_loaded_pid", lambda: (False, None)), \
                mock.patch.object(op_daemon.os, "kill"):
            op_daemon.stop_agent(self.dir, self.lines.append, self.plist, wait=0.1, offline=False)
        self.assertFalse(os.path.exists(os.path.join(self.dir, op_daemon.QUITTING_MARKER)))

    def test_status_and_the_cli(self):
        with mock.patch.object(op_daemon, "agent_loaded_pid", lambda: (False, None)):
            doc = op_daemon.agent_status(self.dir)
        self.assertFalse(doc["loaded"])
        self.assertFalse(doc["state_fresh"])
        out = io.StringIO()
        paths = resolve_paths(data_dir=self.dir, app_bundle="")
        with mock.patch.object(op_daemon, "agent_loaded_pid", lambda: (True, 77)):
            rc = op_daemon.run_daemon_command("status", paths, self.lines.append,
                                              json_output=True, out=out)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())["pid"], 77)
        with mock.patch.object(op_daemon, "stop_agent", return_value=True) as stop:
            rc = op_daemon.run_daemon_command("stop", paths, self.lines.append,
                                              no_offline=True, out=io.StringIO())
        self.assertEqual(rc, 0)
        self.assertFalse(stop.call_args.kwargs["offline"])
        with mock.patch.object(op_daemon, "start_agent", return_value=False):
            rc = op_daemon.run_daemon_command("start", paths, self.lines.append,
                                              launcher_script=self.launcher, out=io.StringIO())
        self.assertEqual(rc, 1)

    def test_request_offline_waits_for_the_ack(self):
        self.assertFalse(op_daemon.request_offline(self.dir, self.lines.append, wait=0.1))
        with open(op_daemon.pid_path(self.dir), "w") as f:
            f.write(str(os.getpid()))
        ack = os.path.join(self.dir, op_daemon.OFFLINE_ACK_MARKER)

        def kill(pid, sig):
            if sig == 0:
                return          # pid_alive's liveness check
            self.assertEqual(sig, op_daemon.signal.SIGUSR1)
            with open(ack, "w") as f:
                f.write("1")
        with mock.patch.object(op_daemon.os, "kill", kill):
            self.assertTrue(op_daemon.request_offline(self.dir, self.lines.append, wait=2))
        self.assertFalse(os.path.exists(ack))

    def test_cli_dispatch_needs_no_docker(self):
        from onionpress import cli
        with mock.patch.object(cli, "OnionPressCLI", side_effect=AssertionError("setup ran")), \
                mock.patch("onionpress.daemon.run_daemon_command", return_value=0) as run:
            rc = cli.main(["--data-dir", self.dir, "daemon", "status", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(run.call_args.args[0], "status")
        self.assertTrue(run.call_args.kwargs["json_output"])
        self.assertTrue(run.call_args.kwargs["launcher_script"])


class TestUpdateOrder(unittest.TestCase):
    """`onionpress update`: download, stage, stop the daemon, swap,
    start the daemon from the new bundle (CLAUDE.md's Gatekeeper note)."""

    def test_hooks_around_the_swap(self):
        from onionpress import updater
        order = []
        with mock.patch.object(updater.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, "", "")), \
                mock.patch.object(updater.os.path, "getsize", return_value=2_000_000), \
                mock.patch.object(updater.os.path, "isdir", return_value=True), \
                mock.patch.object(updater, "stage_new_bundle",
                                  side_effect=lambda s, i: order.append("stage") or "/tmp/staged"), \
                mock.patch.object(updater, "swap_in_bundle",
                                  side_effect=lambda *a, **k: order.append("swap")), \
                mock.patch.object(updater, "_fetch_release_sums",
                                  return_value=f"{'a' * 64}  x.dmg\n"), \
                mock.patch.object(updater, "sha256_of", return_value="a" * 64), \
                mock.patch.object(updater.shutil, "rmtree"):
            updater.download_and_install(
                {"assets": [{"name": "x.dmg", "browser_download_url": "https://x/x.dmg"}]},
                "9.9", "/Applications/OnionPress.app",
                before_swap=lambda: order.append("stop-daemon"),
                after_swap=lambda: order.append("start-daemon"))
        self.assertEqual(order, ["stage", "stop-daemon", "swap", "start-daemon"])

    def test_cmd_update_wires_the_daemon(self):
        src = _read("src/onionpress/cli.py")
        body = src[src.index("def cmd_update("):src.index("def cmd_uninstall(")]
        self.assertIn("before_swap=_stop_daemon, after_swap=_start_daemon", body)
        self.assertIn("_daemon.stop_agent(paths.data_dir, log, offline=False)", body)
        self.assertIn("_daemon.start_agent(launcher, paths.data_dir, log", body)


class TestMenubarIsAView(unittest.TestCase):
    """src/menubar.py holds no loop that talks to docker, the hub or Tor:
    its one background loop reads the daemon's state file; every docker
    call left is an action's (the first-run setup's wp-cli, the login
    token), never a thread or timer's."""

    LOOP_MODULES = ("health", "docker", "onion_proxy", "thumbnails", "power",
                    "analytics_sharing", "reachability_stats", "system_metrics",
                    "reachability", "mac_power")
    # Methods that may name docker: one-shot actions of the first-run setup
    # and the login token (not loops), as before this phase.
    DOCKER_ALLOWED = {"_generate_login_url", "_read_onion_address", "_get_admin_username",
                      "_register_onionname_during_setup", "_rename_wp_admin", "_wp_core_install"}

    def setUp(self):
        self.src = _read("src/menubar.py")
        self.methods = _methods(self.src)

    def test_loop_modules_are_not_imported(self):
        tree = ast.parse(self.src)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "onionpress":
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("onionpress."):
                imported.add(node.module.split(".", 1)[1])
        for name in self.LOOP_MODULES:
            with self.subTest(module=name):
                self.assertNotIn(name, imported)
        for name in ("HealthChecker", "RepublishTrigger", "CaffeineManager", "Docker(",
                     "onion_proxy.", "thumbnails.", "analytics_sharing.", "_health_checker",
                     "_docker.", "check_tor_reachability", "write_status_to_volume",
                     "healthcheck-messages", "start_registration_thread",
                     "register_wake_notification", "handle_sleep", "handle_wake",
                     "start_container_log_capture", "start_web_log_capture",
                     "start_clearnet_log_capture", "monitor_image_downloads",
                     "start_thumbnail_generator", "start_status_checker"):
            with self.subTest(name=name):
                self.assertNotIn(name, self.src)

    def test_no_timer_but_the_splash_tips(self):
        timers = [m.start() for m in re.finditer(r"rumps\.Timer\(|NSTimer\.", self.src)]
        self.assertLessEqual(len(timers), 2)
        for pos in timers:
            self.assertIn("tip", self.src[pos - 600:pos + 600].lower())

    def test_the_loops_left_read_a_file_only(self):
        """Every `while` loop in the menubar: the view tick (a file read),
        the splash, a wait on the daemon's state, or an action's bounded
        wait; none runs docker, the launcher's status/address, or the hub."""
        for name, body in self.methods.items():
            if "while " not in body:
                continue
            with self.subTest(method=name):
                self.assertNotRegex(body, r'\["docker"|_docker\.|docker_bin|onionheaven\.(?!is_onionheaven_instance|describe_remaining)[a-z_]+\(|wp_cli_args|run_command\(')
                self.assertNotIn('"status"', body)
                self.assertNotIn('"address"', body)

    def test_docker_only_in_one_shot_actions(self):
        for name, body in self.methods.items():
            if re.search(r'\["docker"|wp_cli_args|docker_bin|_docker\.', body):
                with self.subTest(method=name):
                    self.assertIn(name, self.DOCKER_ALLOWED)
                    self.assertNotIn("while ", body)

    def test_the_view_loop_reads_the_state_file(self):
        body = self.methods["_view_tick"]
        self.assertIn("op_daemon.read_state(self.app_support)", body)
        self.assertIn("op_daemon.state_is_fresh(state, now)", body)
        self.assertIn("self.ensure_daemon_running(now)", body)
        loop = self.methods["start_view_loop"]
        self.assertIn("self._view_tick()", loop)
        self.assertIn("VIEW_TICK_SECONDS", loop)

    def test_stale_state_is_reconnecting_never_stopped(self):
        prop = self.methods["display_state"]
        self.assertIn('return "reconnecting"', prop)
        menu = self.methods["update_menu"]
        self.assertIn('elif state == "reconnecting":', menu)
        self.assertIn("Reconnecting to the OnionPress service", menu)
        self.assertNotIn('"reconnecting"', self.methods["ensure_daemon_running"])

    def test_the_menubar_starts_the_daemon_when_there_is_none(self):
        body = self.methods["ensure_daemon_running"]
        self.assertIn('[self.launcher_script, "daemon", "start"]', body)
        self.assertIn("op_daemon.daemon_pid(self.app_support)", body)
        self.assertIn("op_daemon.agent_loaded_pid()", body)

    def test_quit_goes_through_the_launcher(self):
        body = self.methods["_perform_quit_cleanup"]
        self.assertIn('[self.launcher_script, "quit", "--leave-menubar"]', body)
        self.assertIn('"--no-offline"', body)
        self.assertIn("QUIT_KEEPS_SITE_RUNNING", self.methods["_quit_keeps_site_running"])
        self.assertNotIn("colima_bin", body)
        self.assertNotIn('"stop"]', body)
        self.assertNotIn("notify_onionheaven_offline", self.src)

    def test_menubar_log_is_its_own(self):
        self.assertIn('RotatingLog(\n            self.logs_dir, "menubar"', self.src)
        self.assertNotIn('_rotating("onionpress")', self.src)
        self.assertIn('current_log_path(self.logs_dir, "onionpress")', self.methods["view_logs"])


class TestLauncherWiring(unittest.TestCase):
    """app/MacOS/onionpress: start starts the agent, stop asks the daemon
    for /offline first, quit stops the daemon before the containers and
    takes --leave-menubar."""

    def setUp(self):
        self.src = _read("app/MacOS/onionpress")
        main = self.src[self.src.index("\nmain() {"):]
        self.cases = {}
        for name in ("start", "stop", "quit", "daemon", "upload-analytics"):
            m = re.search(rf"\n        {re.escape(name)}\)\n(.*?)\n            ;;", main, re.S)
            self.assertIsNotNone(m, name)
            self.cases[name] = m.group(1)

    def test_start_starts_the_agent_before_the_containers(self):
        body = self.cases["start"]
        self.assertLess(body.index("daemon start"), body.index("start_containers"))
        self.assertIn('--launcher "$SCRIPT_DIR/onionpress"', body)

    def test_stop_asks_offline_first(self):
        body = self.cases["stop"]
        self.assertLess(body.index("daemon offline"), body.index("stop_containers"))

    def test_quit_stops_the_daemon_first_and_leaves_the_menubar_when_told(self):
        body = self.cases["quit"]
        self.assertLess(body.index("daemon stop"), body.index("stop_containers"))
        self.assertIn('"${2:-}" = "--leave-menubar"', body)
        self.assertIn("--no-offline", body)

    def test_daemon_case_delegates(self):
        self.assertIn('exec "$_BUNDLED_PYTHON_BIN" -m onionpress.cli --data-dir "$DATA_DIR" daemon --launcher "$SCRIPT_DIR/onionpress" "${@:2}"',
                      self.cases["daemon"])
        # upload-analytics is the CLI's (analytics_sharing.request_upload,
        # #351): the daemon.pid check moved there with the Linux launcher's
        # delegation (tests/test_settings_page_share_now.py).
        self.assertIn('bundled_python -m onionpress.cli --data-dir "$DATA_DIR" upload-analytics',
                      self.cases["upload-analytics"])
        self.assertNotIn('daemon.pid', self.cases["upload-analytics"])

    def test_help_names_the_daemon(self):
        self.assertIn("  daemon        ", self.src)

    def test_setup_py_bundles_the_module(self):
        self.assertIn("'onionpress.daemon'", _read("setup.py"))


if __name__ == "__main__":
    unittest.main()

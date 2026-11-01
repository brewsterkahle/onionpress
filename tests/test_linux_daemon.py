#!/usr/bin/env python3
"""`onionpress daemon` on Linux (issue #407): the one long-running process
on both platforms, as the onionpress-daemon.service user unit.

Until #407 Linux ran a heartbeat client of its own
(linux/onionpress-heartbeat.py, 2,200 lines re-implementing the daemon's
hub client), a bash status loop forked from the launcher's `start` and a
10 s watcher timer for the Settings page. Now the daemon of
tests/test_mac_daemon.py runs there too, and what differs is its
`os_name` seam: sleep and wake (power.LogindPowerHandler behind
mac_power.MacPowerHandler's interface), the sleep inhibitor, the Docker
environment (#503), runtime readiness, the hub gate until WordPress is
installed (#464), the rootless Docker namespace watch, and what only a
Mac has (thumbnails, the clearnet capture, the extension's proxy, the
start of Tor after setup).

The daemon runs against the Mac test's stubs (a fake Docker, a fake
HealthChecker, a recorded launcher); the client side (daemon_client's
systemd half) against a recorded systemctl; the packaging and the
launcher are read.
"""
import os
import re
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_mac_daemon import _DaemonCase, ADDR  # noqa: E402

from onionpress import analytics_sharing, daemon as op_daemon  # noqa: E402
from onionpress import daemon_client, health, mac_power, power  # noqa: E402
from onionpress.platform import OS, resolve_paths  # noqa: E402


def _read(rel):
    with open(os.path.join(PROJECT_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _code(text):
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def _bash_function(text, name):
    start = text.index(f"\n{name}() {{") + 1
    return text[start:text.index("\n}\n", start) + 3]


def _case(text, label):
    main = text[text.index("\nmain() {"):]
    m = re.search(rf"\n        {re.escape(label)}\)\n(.*?)\n            ;;", main, re.S)
    if m is None:
        raise AssertionError(f"no {label}) case in the Linux launcher's main()")
    return m.group(1)


class _LinuxDaemonCase(_DaemonCase):
    OS_NAME = OS.LINUX


# ---------------------------------------------------------------------------
# The seams
# ---------------------------------------------------------------------------

class TestPlatformSeams(_LinuxDaemonCase):

    def test_the_inhibitor_is_systemd_inhibit_here_and_caffeinate_on_a_mac(self):
        d = op_daemon.Daemon(self.paths, version="9.9", docker=self.docker,
                             health_checker=self.health, os_name=OS.LINUX)
        self.assertIsInstance(d.caffeine, power.SystemdInhibitor)
        d = op_daemon.Daemon(self.paths, version="9.9", docker=self.docker,
                             health_checker=self.health, os_name=OS.MACOS)
        self.assertIsInstance(d.caffeine, power.CaffeineManager)

    def test_the_power_handler_by_platform(self):
        self.assertIs(self.d._power_handler_class(), power.LogindPowerHandler)
        self.d.os_name = OS.MACOS
        self.assertIs(self.d._power_handler_class(), mac_power.MacPowerHandler)

    def test_runtime_ready_needs_no_colima_marker(self):
        os.remove(os.path.join(self.dir, "colima", ".initialized"))
        self.assertTrue(self.d._runtime_ready())
        self.d.os_name = OS.MACOS
        self.assertFalse(self.d._runtime_ready())

    def test_the_environment_is_the_rootless_socket(self):
        """Issue #503: the unit runs without the launcher's DOCKER_HOST;
        the daemon exports resolve_paths' socket when it exists, keeps a
        preset one, and leaves docker's default when there is none."""
        sock = os.path.join(self.dir, "docker.sock")
        self.d.docker_socket = sock
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DOCKER_HOST", None)
            self.d._setup_environment()
            self.assertNotIn("DOCKER_HOST", os.environ)
            open(sock, "w").close()
            self.d._setup_environment()
            self.assertEqual(os.environ["DOCKER_HOST"], f"unix://{sock}")
            os.environ["DOCKER_HOST"] = "unix:///preset.sock"
            self.d._setup_environment()
            self.assertEqual(os.environ["DOCKER_HOST"], "unix:///preset.sock")
            # Nothing of Colima's.
            self.assertNotIn("COLIMA_HOME", {k for k in os.environ if k == "COLIMA_HOME"
                                             and os.environ[k] == self.d.colima_home})

    def test_the_unit_sets_no_docker_host(self):
        unit = _read("linux/onionpress-daemon.service")
        self.assertNotIn("DOCKER_HOST", unit)
        self.assertIn("ExecStart=/opt/onionpress/onionpress daemon\n", unit)
        self.assertIn("Before=onionpress.service\n", unit)
        self.assertIn("Restart=always\n", unit)
        self.assertIn("WantedBy=default.target", unit)

    def test_status_says_linux(self):
        self.tick()
        self.assertEqual(self.state()["status"]["platform"], "linux")

    def test_the_internet_check_is_the_routing_table(self):
        d = tempfile.mkdtemp(prefix="op-route-")
        self.addCleanup(shutil.rmtree, d, True)
        route, route6 = os.path.join(d, "route"), os.path.join(d, "ipv6_route")
        with open(route, "w") as f:
            f.write("Iface\tDestination\tGateway\nwlan0\t00000000\t0100A8C0\n")
        self.assertTrue(health.linux_has_default_route(route, route6))
        with open(route, "w") as f:
            f.write("Iface\tDestination\tGateway\nwlan0\t0000A8C0\t00000000\n")
        with open(route6, "w") as f:
            f.write("00000000000000000000000000000000 00 ... eth0\n")
        self.assertTrue(health.linux_has_default_route(route, route6))
        with open(route6, "w") as f:
            f.write("fe800000000000000000000000000000 40 ... eth0\n")
        self.assertFalse(health.linux_has_default_route(route, route6))
        # An unreadable table never reads as offline.
        self.assertTrue(health.linux_has_default_route(os.path.join(d, "none"), route6))
        with mock.patch("onionpress.platform.detect_os", return_value=OS.LINUX), \
                mock.patch("onionpress.health.linux_has_default_route", return_value=False) as r:
            self.assertFalse(health.HealthChecker.check_internet_connectivity())
        r.assert_called_once()

    def test_only_a_mac_has_thumbnails_the_proxy_and_the_clearnet_capture(self):
        d = op_daemon.Daemon(self.paths, version="9.9", docker=self.docker,
                             health_checker=self.health, os_name=OS.LINUX)
        with mock.patch("onionpress.daemon.threading.Thread") as thread:
            d.start_thumbnail_generator()
            d.start_onion_proxy()
        thread.assert_not_called()
        self.assertIsNone(d.proxy_server)
        self.assertTrue(d._thumbnail_generator_started)
        with mock.patch("onionpress.daemon.subprocess.run") as run:
            d.start_clearnet_log_capture()
        run.assert_not_called()
        # The status pass starts no clearnet capture either.
        started = []
        self.d.start_clearnet_log_capture = lambda: started.append(1)
        self.tick()
        self.assertFalse(self.d._clearnet_capture_started)
        self.assertEqual(started, [])

    def test_no_start_tor_after_setup_the_linux_start_brings_tor_up(self):
        """The Mac's first run starts Tor through the launcher once
        WordPress is installed; Linux's start brought it up already, so
        the daemon only tracks the install (the hub gate)."""
        self.docker.answers["wp core is-installed"] = (1, "")
        self.d.proxy_server = object()
        self.d._check_wordpress_installed()
        self.assertIs(self.d._wp_installed, False)
        self.docker.answers["wp core is-installed"] = (0, "")
        self.d._check_wordpress_installed()
        self.assertIs(self.d._wp_installed, True)
        self.assertEqual([c for c in self.launcher_calls if c and c[0] == "start-tor"], [])

    def test_the_rootless_namespace_watch_reapplies_the_egress_rules(self):
        runs = []
        self.d.run_cli = lambda args, timeout=60: runs.append(list(args)) or (0, "")
        with mock.patch.object(self.d, "_read_rootless_netns", return_value="net:[1]"):
            self.d._watch_rootless_netns()          # the baseline
            self.d._watch_rootless_netns()          # unchanged
        self.assertEqual(runs, [])
        with mock.patch.object(self.d, "_read_rootless_netns", return_value="net:[2]"), \
                mock.patch("onionpress.daemon.threading.Thread",
                           side_effect=lambda target, **kw: mock.Mock(start=target)):
            self.d._watch_rootless_netns()
            self.d._watch_rootless_netns()          # once per namespace
        self.assertEqual(runs, [["wordpress-egress", "--wp-port", str(self.d.wp_port)]])
        with mock.patch.object(self.d, "_read_rootless_netns", return_value=""):
            self.d._watch_rootless_netns()          # system Docker: nothing
        self.assertEqual(len(runs), 1)
        self.d.os_name = OS.MACOS
        with mock.patch.object(self.d, "_read_rootless_netns", return_value="net:[3]"):
            self.d._watch_rootless_netns()
        self.assertEqual(len(runs), 1)

    def test_reading_the_namespace(self):
        runtime = os.path.join(self.dir, "run")
        os.makedirs(os.path.join(runtime, "dockerd-rootless"))
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": runtime}):
            self.assertEqual(self.d._read_rootless_netns(), "")
            with open(os.path.join(runtime, "dockerd-rootless", "child_pid"), "w") as f:
                f.write("x\n")
            self.assertEqual(self.d._read_rootless_netns(), "")
            with open(os.path.join(runtime, "dockerd-rootless", "child_pid"), "w") as f:
                f.write(f"{os.getpid()}\n")
            ns = self.d._read_rootless_netns()
            self.assertTrue(ns == "" or ns.startswith("net:"), ns)

    def test_the_libsodium_warning_at_start(self):
        logged = []
        self.d.log = logged.append
        with mock.patch("onionpress.daemon.onion_auth.signing_uses_libsodium", return_value=False):
            self.d._log_version_info()
        self.assertTrue(any("signed without libsodium" in m and "python3-nacl" in m for m in logged))
        logged.clear()
        with mock.patch("onionpress.daemon.onion_auth.signing_uses_libsodium", return_value=True), \
                mock.patch.dict(os.environ, {"INVOCATION_ID": "abc"}):
            self.d._log_version_info()
        self.assertFalse(any("libsodium" in m for m in logged))
        self.assertTrue(any("Running under systemd (onionpress-daemon" in m for m in logged))


class TestHubGate(_LinuxDaemonCase):
    """Issue #464: nothing registers the address with the hub before
    WordPress is installed. On Linux Tor runs before setup, so the
    daemon's hub loops wait for `wp core is-installed` (until #407 a
    runtime mask of the heartbeat unit, held by the launcher)."""

    def setUp(self):
        super().setUp()
        del self.d._hub_loops          # the real one
        self.d.hub_client = mock.Mock()
        self.d._last_bootstrap_pct = 100
        self.d.is_ready = True
        self.d.onion_address = ADDR

    def _installed(self, yes):
        self.docker.answers["wp core is-installed"] = (0 if yes else 1, "")

    def test_closed_until_installed_then_open(self):
        self._installed(False)
        self.d._check_wordpress_installed()
        self.assertFalse(self.d.hub_registration_allowed())
        self.d._hub_loops()
        self.assertEqual(self.d.hub_client.tick.call_args.args[0], False)
        self._installed(True)
        self.d._check_wordpress_installed()
        self.assertTrue(self.d.hub_registration_allowed())
        self.d._hub_loops()
        self.assertEqual(self.d.hub_client.tick.call_args.args[0], True)

    def test_said_once_in_the_log(self):
        logged = []
        self.d.log = logged.append
        self._installed(False)
        self.d._check_wordpress_installed()
        self.d._check_wordpress_installed()
        self.assertEqual([m for m in logged if "no hub registration" in m],
                         ["WordPress is not installed: no hub registration until setup has run (issue #464)"])
        self._installed(True)
        self.d._check_wordpress_installed()
        self.assertIn("WordPress is installed: hub registration allowed", logged)

    def test_a_mac_has_no_gate(self):
        self.d.os_name = OS.MACOS
        self.assertTrue(self.d.hub_registration_allowed())

    def test_the_status_pass_decides_before_the_hub_loops(self):
        daemon = _read("src/onionpress/daemon.py")
        body = daemon[daemon.index("    def check_status(self):"):]
        self.assertLess(body.index("self._check_wordpress_installed()"), body.index("self._hub_loops()"))
        self.assertIn("self.hub_client.tick(tor_bootstrapped and self.hub_registration_allowed(),", daemon)

    def test_the_launcher_holds_nothing(self):
        launcher = _read("linux/onionpress")
        for gone in ("hold_heartbeat_client_until_installed", "heartbeat_client_held",
                     "release_heartbeat_client_hold", "HEARTBEAT_UNIT", "mask --runtime",
                     "stop_heartbeat_client", "start_heartbeat_client"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, launcher)
        after = _bash_function(launcher, "after_wordpress_installed")
        self.assertEqual(_code(after).strip(),
                         "after_wordpress_installed() {\n    bind_wordpress_port_for_install_state true\n}")


# ---------------------------------------------------------------------------
# Sleep and wake
# ---------------------------------------------------------------------------

class _Inhibit:
    """A fake dbus: the Inhibit() call hands out a pipe's read end."""

    def __init__(self):
        self.fds = []

    def Interface(self, obj, iface):
        return self

    def Inhibit(self, what, who, why, mode):
        assert (what, mode) == ("sleep", "delay"), (what, mode)
        r, w = os.pipe()
        os.close(w)
        self.fds.append(r)
        return mock.Mock(take=lambda: r)


class TestLogindPowerHandler(unittest.TestCase):

    def _handler(self):
        self.events = []
        h = power.LogindPowerHandler(
            on_will_sleep=lambda: self.events.append("sleep"),
            on_has_powered_on=lambda: self.events.append("wake"),
            log=self.events.append)
        h._dbus = _Inhibit()
        h._login_obj = object()
        return h

    def test_the_interface_is_the_macs(self):
        for cls in (power.LogindPowerHandler, mac_power.MacPowerHandler):
            with self.subTest(cls=cls.__name__):
                for name in ("register", "run_loop_once", "stop", "OFFLINE_MAX_TIME"):
                    self.assertTrue(hasattr(cls, name), name)
        # logind's InhibitDelayMaxSec is 5 s by default: the /offline
        # takes less, the watchdog signals before it the rest.
        self.assertLessEqual(power.LogindPowerHandler.OFFLINE_MAX_TIME, 3)
        self.assertEqual(mac_power.MacPowerHandler.OFFLINE_MAX_TIME, 5)
        h = power.LogindPowerHandler(lambda: None, lambda: None, lambda m: None)
        self.assertFalse(h.run_loop_once(1.0))

    def test_without_dbus_register_says_so(self):
        logged = []
        h = power.LogindPowerHandler(lambda: None, lambda: None, logged.append)
        with mock.patch.dict(sys.modules, {"dbus": None, "dbus.mainloop.glib": None}):
            self.assertFalse(h.register())
        self.assertTrue(any("not importable" in m for m in logged), logged)

    def test_sleep_runs_the_callback_inside_the_window_then_releases(self):
        h = self._handler()
        self.assertTrue(h._acquire())
        fd = h._inhibitor_fd
        h._on_prepare_for_sleep(True)
        self.assertEqual([e for e in self.events if e in ("sleep", "wake")], ["sleep"])
        self.assertIsNone(h._inhibitor_fd)
        with self.assertRaises(OSError):
            os.fstat(fd)                            # released: the fd is closed
        self.assertTrue(any("released" in m for m in self.events))

    def test_a_raising_callback_still_releases(self):
        h = self._handler()
        h._on_will_sleep = mock.Mock(side_effect=RuntimeError("boom"))
        h._acquire()
        h._on_prepare_for_sleep(True)
        self.assertIsNone(h._inhibitor_fd)
        self.assertTrue(any("on_will_sleep raised: boom" in m for m in self.events))

    def test_wake_takes_the_inhibitor_again_then_runs_the_callback(self):
        h = self._handler()
        order = []
        h._on_has_powered_on = lambda: order.append(("wake", h._inhibitor_fd is not None))
        h._on_prepare_for_sleep(False)
        self.assertEqual(order, [("wake", True)])
        h.stop()
        self.assertIsNone(h._inhibitor_fd)


class TestSleepWakeOnLinux(_LinuxDaemonCase):
    """The one sleep/wake policy (handle_sleep / handle_wake) runs under
    the logind handler: /offline within its budget, USR1/USR2 to the
    watchdogs, the republish window reset on wake (test-enforced on both
    platforms: tests/test_mac_daemon.TestSleepWake has the Mac)."""

    def test_the_offline_fits_loginds_delay(self):
        self.tick()
        self.d._power = power.LogindPowerHandler(lambda: None, lambda: None, lambda m: None)
        with mock.patch("onionpress.launcher_ops.signal_watchdog", return_value=True), \
                mock.patch.object(self.d.hub_client, "notify_offline", return_value=True) as off:
            self.d.hub_client.registration_succeeded = True
            self.d.handle_sleep()
        off.assert_called_once_with(max_time=power.LogindPowerHandler.OFFLINE_MAX_TIME)
        self.assertTrue(self.d._sleeping)
        self.d.caffeine.stop.assert_called()

    def test_wake_resets_the_republish_window(self):
        self.tick()
        self.d._recovery.trigger.reset = mock.Mock()
        with mock.patch("onionpress.launcher_ops.signal_watchdog", return_value=True) as sig:
            self.d.handle_wake()
        self.d._recovery.trigger.reset.assert_called_once()
        self.assertEqual([c.args[1:] for c in sig.call_args_list],
                         [("onionpress-tor", "USR2"), ("onionheaven", "USR2")])
        self.d.caffeine.start.assert_called()

    def test_the_handlers_callbacks_are_the_daemons(self):
        registered = {}

        class Handler:
            OFFLINE_MAX_TIME = 3

            def __init__(self, on_will_sleep, on_has_powered_on, log):
                registered.update(sleep=on_will_sleep, wake=on_has_powered_on)

            def register(self):
                return True

            def run_loop_once(self, seconds):
                return False

            def stop(self):
                registered["stopped"] = True
        with mock.patch.object(self.d, "_power_handler_class", return_value=Handler):
            self.d._register_power_events()
        self.assertIsInstance(self.d._power, Handler)
        with mock.patch.object(self.d, "handle_sleep") as hs, \
                mock.patch.object(self.d, "handle_wake") as hw:
            registered["sleep"]()
            registered["wake"]()
        hs.assert_called_once()
        hw.assert_called_once()
        self.d._quitting = True
        with mock.patch.object(self.d, "handle_sleep") as hs:
            registered["sleep"]()
        hs.assert_not_called()
        self.d._quitting = False
        self.d.hub_client.registration_succeeded = False
        self.d._shutdown()
        self.assertTrue(registered.get("stopped"))

    def test_the_main_loop_waits_when_the_handler_has_its_own_thread(self):
        self.d._power = power.LogindPowerHandler(lambda: None, lambda: None, lambda m: None)
        with mock.patch.object(self.d._stop, "wait") as wait:
            self.d._main_loop_once(0.5)
        wait.assert_called_once_with(0.5)


# ---------------------------------------------------------------------------
# The client side: daemon_client's systemd half
# ---------------------------------------------------------------------------

class _Systemctl:
    """`systemctl --user` with a unit's show answer and a call record."""

    def __init__(self, load="loaded", active="active", pid=0):
        self.load, self.active, self.pid = load, active, pid
        self.calls = []
        self.fail = set()

    def __call__(self, args, timeout=60):
        self.calls.append(list(args))
        if args[0] == "show":
            return mock.Mock(returncode=0, stdout=f"LoadState={self.load}\nActiveState={self.active}\n"
                                                  f"MainPID={self.pid}\n", stderr="")
        if args[0] in self.fail:
            return mock.Mock(returncode=1, stdout="", stderr="refused")
        if args[0] == "start":
            self.pid = self.pid or 4242
            self.active = "active"
        if args[0] == "stop":
            self.pid, self.active = 0, "inactive"
        return mock.Mock(returncode=0, stdout="", stderr="")


class TestOnLinux(unittest.TestCase):

    def test_on_linux_reads_uname(self):
        with mock.patch.object(daemon_client.os, "uname", return_value=mock.Mock(sysname="Linux")):
            self.assertTrue(daemon_client._on_linux())
        with mock.patch.object(daemon_client.os, "uname", return_value=mock.Mock(sysname="Darwin")):
            self.assertFalse(daemon_client._on_linux())
        # The client module stays standard-library only (the menubar's
        # import closure): the platform is read from os, not detect_os.
        src = _read("src/onionpress/daemon_client.py")
        self.assertNotIn("from .platform", src)
        self.assertNotIn("detect_os", src)


class TestSystemdSide(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="op-dc-linux-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.logs = []
        p = mock.patch.object(daemon_client, "_on_linux", return_value=True)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(daemon_client, "pid_alive", return_value=False)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(daemon_client.time, "sleep")
        p.start()
        self.addCleanup(p.stop)

    def _ctl(self, **kw):
        ctl = _Systemctl(**kw)
        p = mock.patch.object(daemon_client, "_systemctl", ctl)
        p.start()
        self.addCleanup(p.stop)
        return ctl

    def test_the_unit_is_per_data_dir(self):
        self.assertEqual(daemon_client.systemd_unit(), "onionpress-daemon.service")
        with mock.patch.object(daemon_client, "default_data_dir", return_value=self.dir):
            self.assertEqual(daemon_client.systemd_unit(self.dir), "onionpress-daemon.service")
        other = daemon_client.systemd_unit(os.path.join(self.dir, "other"))
        self.assertRegex(other, r"^onionpress-daemon\.[0-9a-f]{8}\.service$")
        self.assertEqual(daemon_client.SYSTEMD_UNIT, "onionpress-daemon")
        unit = _read("linux/onionpress-daemon.service")
        self.assertTrue(os.path.exists(os.path.join(PROJECT_ROOT, "linux", "onionpress-daemon.service")))
        self.assertIn("onionpress daemon", unit)

    def test_loaded_pid_from_show(self):
        ctl = self._ctl(pid=77)
        self.assertEqual(daemon_client.agent_loaded_pid(self.dir), (True, 77))
        ctl.pid = 0
        self.assertEqual(daemon_client.agent_loaded_pid(self.dir), (True, None))
        ctl.load = "not-found"
        self.assertEqual(daemon_client.agent_loaded_pid(self.dir), (False, None))
        self.assertTrue(all(c[0] == "show" and c[-1].startswith("onionpress-daemon") for c in ctl.calls))

    def test_start_is_no_block_and_waits_for_the_pid(self):
        ctl = self._ctl()
        self.assertTrue(daemon_client.start_agent("/opt/onionpress/onionpress", self.dir, self.logs.append))
        self.assertIn(["start", "--no-block", daemon_client.systemd_unit(self.dir)], ctl.calls)
        self.assertTrue(any("started under systemd" in m for m in self.logs))
        # Already running: nothing asked.
        ctl.calls.clear()
        self.assertTrue(daemon_client.start_agent("/x", self.dir, self.logs.append))
        self.assertEqual([c[0] for c in ctl.calls], ["show"])

    def test_no_unit_file_is_said_not_started(self):
        ctl = self._ctl(load="not-found")
        self.assertFalse(daemon_client.start_agent("/x", self.dir, self.logs.append))
        self.assertFalse([c for c in ctl.calls if c[0] == "start"])
        self.assertTrue(any("no systemd user unit" in m and "onionpress daemon" in m for m in self.logs))
        ctl = self._ctl()
        ctl.fail.add("start")
        self.assertFalse(daemon_client.start_agent("/x", self.dir, self.logs.append))
        self.assertTrue(any("systemctl --user start" in m and "refused" in m for m in self.logs))

    def test_stop_stops_the_unit_and_marks_a_quit(self):
        ctl = self._ctl(pid=77)
        with open(daemon_client.pid_path(self.dir), "w") as f:
            f.write("77")
        with mock.patch.object(daemon_client, "pid_alive", side_effect=lambda p: ctl.pid == int(p)):
            self.assertTrue(daemon_client.stop_agent(self.dir, self.logs.append))
        self.assertIn(["stop", daemon_client.systemd_unit(self.dir)], ctl.calls)
        self.assertTrue(os.path.exists(os.path.join(self.dir, daemon_client.QUITTING_MARKER)))
        # --no-offline (an update): no marker.
        ctl.pid = 77
        with mock.patch.object(daemon_client, "pid_alive", side_effect=lambda p: ctl.pid == int(p)):
            daemon_client.stop_agent(self.dir, self.logs.append, offline=False)
        self.assertFalse(os.path.exists(os.path.join(self.dir, daemon_client.QUITTING_MARKER)))

    def test_a_terminals_daemon_is_signalled(self):
        self._ctl(pid=0)
        with open(daemon_client.pid_path(self.dir), "w") as f:
            f.write(str(os.getpid()))
        alive = [True]
        with mock.patch.object(daemon_client, "pid_alive", side_effect=lambda p: alive[0]), \
                mock.patch.object(daemon_client.os, "kill",
                                  side_effect=lambda p, s: alive.__setitem__(0, False)) as kill:
            self.assertTrue(daemon_client.stop_agent(self.dir, self.logs.append))
        kill.assert_called_once_with(os.getpid(), daemon_client.signal.SIGTERM)

    def test_status_names_the_unit(self):
        self._ctl(pid=77)
        doc = daemon_client.agent_status(self.dir)
        self.assertEqual((doc["loaded"], doc["pid"], doc["unit"], doc["label"]),
                         (True, 77, daemon_client.systemd_unit(self.dir), daemon_client.systemd_unit(self.dir)))
        self.assertNotIn("plist", doc)

    def test_remove_only_stops(self):
        ctl = self._ctl(pid=77)
        daemon_client.remove_agent(self.logs.append, data_dir=self.dir)
        self.assertEqual([c[0] for c in ctl.calls], ["show", "stop"])

    def test_the_macs_side_is_untouched_off_linux(self):
        with mock.patch.object(daemon_client, "_on_linux", return_value=False), \
                mock.patch.object(daemon_client, "_launchctl",
                                  return_value=mock.Mock(returncode=1, stdout="", stderr="")) as lc, \
                mock.patch.object(daemon_client, "_systemctl") as sc:
            self.assertEqual(daemon_client.agent_loaded_pid(self.dir), (False, None))
        lc.assert_called_once()
        sc.assert_not_called()

    def test_share_now_asks_the_pid_file_on_linux_too(self):
        self.assertFalse(analytics_sharing.daemon_running(self.dir, "linux"))
        with open(os.path.join(self.dir, daemon_client.PID_FILENAME), "w") as f:
            f.write(f"{os.getpid()}\n")
        self.assertTrue(analytics_sharing.daemon_running(self.dir, "linux"))
        src = _read("src/onionpress/analytics_sharing.py")
        self.assertNotIn("systemctl", src[src.index("def daemon_running("):])


# ---------------------------------------------------------------------------
# The launcher, the units and the packaging
# ---------------------------------------------------------------------------

class TestLauncher(unittest.TestCase):

    def setUp(self):
        self.launcher = _read("linux/onionpress")

    def test_no_heartbeat_no_status_loop_no_watcher_no_sleep_hook(self):
        code = _code(self.launcher)
        for gone in ("onionpress-heartbeat.py", "heartbeat_client", "_status_poll_loop",
                     "start_container_log_capture", "stop_container_log_capture",
                     "write_status", "write-status", "sleep-pre", "sleep-post",
                     "rootless_netns()", "systemd-notify", "ONIONPRESS_IN_WATCHER=true op_py --data-dir \"$DATA_DIR\" write"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, code)
        # The retired unit names appear only where an old install's are
        # stopped, disabled and removed (uninstall).
        for line in code.splitlines():
            if "onionpress-heartbeat" in line or "onionpress-watcher" in line:
                self.assertRegex(line, r"systemctl (--user )?(stop|disable)|rm -f|/etc/systemd/system/|\$USER_SYSTEMD_DIR", line)
        self.assertFalse(os.path.exists(os.path.join(PROJECT_ROOT, "linux", "onionpress-heartbeat.py")))
        self.assertFalse(os.path.exists(os.path.join(PROJECT_ROOT, "linux", "onionpress-sleep-hook")))
        self.assertFalse(os.path.exists(os.path.join(PROJECT_ROOT, "linux", "onionpress-watcher.timer")))
        self.assertFalse(os.path.exists(os.path.join(PROJECT_ROOT, "src", "onionpress", "heartbeat_unit.py")))

    def test_start_starts_the_daemon_after_the_stack(self):
        start = _code(_case(self.launcher, "start"))
        self.assertLess(start.index("wait_for_services"), start.index("start_daemon"))
        fn = _bash_function(self.launcher, "start_daemon")
        self.assertIn('op_py --data-dir "$DATA_DIR" daemon start --launcher "$INSTALL_DIR/onionpress"', fn)
        self.assertIn("WARNING: the OnionPress daemon could not be started", fn)
        # The oneshot returns: no loop keeps the process alive.
        self.assertNotIn("wait\n", start)

    def test_stop_and_restart_ask_the_daemon_offline_first(self):
        for label in ("stop", "restart"):
            with self.subTest(label):
                body = _code(_case(self.launcher, label))
                self.assertLess(body.index("daemon_offline"), body.index("stop_containers"))
        restart = _code(_case(self.launcher, "restart"))
        self.assertLess(restart.index("start_containers"), restart.index("start_daemon"))
        fn = _bash_function(self.launcher, "daemon_offline")
        self.assertIn('op_py --data-dir "$DATA_DIR" daemon offline', fn)
        update = _code(_bash_function(self.launcher, "update_finish"))
        self.assertLess(update.index("daemon_offline"), update.index("daemon stop --no-offline"))
        self.assertLess(update.index("daemon stop --no-offline"), update.index("stop_containers"))
        self.assertLess(update.index("start_containers"), update.index("start_daemon"))

    def test_the_release_function_is_the_macs(self):
        linux = _bash_function(self.launcher, "onionheaven_release")
        mac = _bash_function(_read("app/MacOS/onionpress"), "onionheaven_release")
        norm = lambda t: _code(t).replace("op_py ", "bundled_python -m onionpress.cli ")
        self.assertEqual(norm(linux), norm(mac))

    def test_handle_action_still_marks_its_cgroup(self):
        # The daemon's unit ends with the daemon (an update restarts it):
        # what must outlive a handle-action run goes through systemd-run
        # (start.run_detached_cli), which ONIONPRESS_IN_WATCHER tells.
        handle = _case(self.launcher, "handle-action")
        self.assertIn('ONIONPRESS_IN_WATCHER=true op_py --data-dir "$DATA_DIR" handle-action', handle)

    def test_uninstall_stops_the_daemon_and_the_old_units(self):
        body = _code(_case(self.launcher, "uninstall"))
        self.assertLess(body.index("daemon stop --no-offline"), body.index("onionheaven_release uninstall"))
        for unit in ("onionpress-daemon", "onionpress-heartbeat", "onionpress-watcher.timer"):
            self.assertIn(f"systemctl --user stop {unit}", body)
            self.assertIn(f"systemctl --user disable {unit}", body)
        self.assertIn('"$USER_SYSTEMD_DIR/onionpress-daemon.service"', body)

    def test_start_on_boot_enables_the_daemon_unit(self):
        config = _read("src/onionpress/config.py")
        body = config[config.index("def apply_side_effects("):]
        body = body[:body.index("\ndef ", 1)]
        self.assertIn('run(["systemctl", "--user", verb, "onionpress.service",\n'
                      '                     "onionpress-daemon.service"]', body)
        self.assertNotIn("systemctl --user enable onionpress.service", self.launcher)


class TestUnitsAndPackaging(unittest.TestCase):

    def test_the_site_unit_is_a_oneshot_that_stays_active(self):
        unit = _code(_read("linux/onionpress.service"))
        self.assertIn("Type=oneshot\n", unit)
        self.assertIn("RemainAfterExit=yes\n", unit)
        self.assertIn("ExecStart=/opt/onionpress/onionpress start\n", unit)
        self.assertIn("ExecStop=/opt/onionpress/onionpress stop\n", unit)
        self.assertNotIn("notify", unit.lower())

    def test_the_install_tree_and_the_deb_ship_the_daemon_unit_only(self):
        tree = _read("linux/install-tree.sh")
        self.assertRegex(tree, r"(?m)^file\s+linux/onionpress-daemon\.service\s+onionpress-daemon\.service\s+0644$")
        for gone in ("onionpress-heartbeat", "onionpress-watcher", "sleep-hook"):
            self.assertNotIn(gone, tree)
        check = _read("build/check-deb.sh")
        self.assertIn("./usr/lib/systemd/user/onionpress-daemon.service", check)
        for gone in ("onionpress-heartbeat", "onionpress-watcher", "system-sleep"):
            self.assertNotIn(gone, check)
        build = _read("build/build-linux.sh")
        self.assertIn('cp "$PROJECT_DIR/linux/onionpress-daemon.service" "$DEB_ROOT/usr/lib/systemd/user/"', build)
        self.assertNotIn("onionpress-sleep-hook", build)
        self.assertNotIn("system-sleep", _code(build))
        self.assertIn("python3-dbus", build[build.index("Depends:"):build.index("Suggests:")])

    def test_the_postinst_enables_the_daemon_and_retires_the_old_units(self):
        build = _code(_read("build/build-linux.sh"))
        self.assertIn("systemctl --global enable onionpress.service onionpress-daemon.service", build)
        self.assertIn("systemctl --global disable onionpress-heartbeat.service onionpress-watcher.timer", build)
        self.assertRegex(build, r"user_systemctl enable --now --no-block \\\n\s+onionpress\.service \\\n"
                                r"\s+onionpress-daemon\.service \\\n")
        self.assertNotIn("lift_heartbeat_holds", build)
        self.assertNotIn("masked-runtime", build)

    def test_install_sh_installs_the_daemon_unit_and_removes_the_old_ones(self):
        script = _code(_read("linux/install.sh"))
        self.assertIn('cp "$INSTALL_DIR/onionpress-daemon.service" "$USER_SYSTEMD_DIR/onionpress-daemon.service"', script)
        self.assertIn("systemctl --user enable onionpress-daemon", script)
        self.assertIn("systemctl --user --no-block restart onionpress-daemon", script)
        self.assertIn('rm -f "$USER_SYSTEMD_DIR/onionpress-heartbeat.service"', script)
        self.assertIn("$SUDO rm -f /usr/lib/systemd/system-sleep/onionpress", script)
        self.assertNotIn("install -D", script[script.index("system-sleep") - 400:script.index("system-sleep")])
        self.assertNotIn("python3-socks", script)
        self.assertIn("python3-dbus python3-gi", script)

    def test_nothing_names_the_retired_pieces(self):
        """No code of the heartbeat's era is left (docstrings may say
        what was): the unit paused around a restore, the one-shot status
        pass and its reachability.json, the Linux recovery loop."""
        for rel in ("src/onionpress/cli.py", "src/onionpress/handle_action.py",
                    "src/onionpress/status_writer.py", "src/onionpress/reachability.py",
                    "src/onionpress/analytics_sharing.py", "src/onionpress/daemon.py",
                    "setup.py", "linux/onionpress-tray", "linux/onionpress"):
            text = _code(_read(rel))
            with self.subTest(rel=rel):
                for gone in ("heartbeat_unit.", "import heartbeat_unit",
                             'reachability.json"', "ReachabilityLoop(", "OneShotStatus(",
                             '"write-status"', "write-status)", "run_once(paths",
                             "is-active onionpress-heartbeat"):
                    self.assertNotIn(gone, text, gone)


if __name__ == "__main__":
    unittest.main()

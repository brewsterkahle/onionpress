#!/usr/bin/env python3
"""`onionpress daemon` (onionpress.daemon): the paths test_mac_daemon.py's
status passes do not reach — the process's own life (run, signals, the
start-up environment, the multi-user port offset), the browser
extension's proxy, the onionname retry, the manual analytics upload, the
container log captures, the clearnet capture, the thumbnails and the VM
wedge probe.

What is held, beyond the lines:

- run() installs TERM/INT/USR1/USR2, sets the environment and the ports
  before the first state write, answers USR1 with /offline and USR2 with
  a wake, and on its way out leaves .previous-port-offset and removes its
  pid file;
- _setup_environment points Colima at this install (COLIMA_HOME made
  before any colima call, #370), Docker at this install's socket and a
  private DOCKER_CONFIG, never rewriting the user's docker config;
- _resolve_ports_at_start (CLAUDE.md, Multi-User Support) waits for the
  previous instance's WordPress port by a bind test, takes the resolved
  offset, falls back to ONIONPRESS_PORT_OFFSET's when detection fails,
  and publishes the ports to the environment, onion_proxy and the state
  file the menubar reads;
- the proxy binds 127.0.0.1 only, on this user's proxy port, and reads
  the daemon's live ports;
- a collision during the onionname retry is put to the menubar
  (onionname_collision), a success saved through `config set`;
- the captures follow `docker ps` (takeover workers included), resume
  with --since, end when their container goes; clearnet lines are
  written once each;
- the thumbnailer never retries a file qlmanage failed on until it
  changes, and stops at a .thumbs that is not a real folder;
- the wedge probe runs at most every five minutes and warns once per
  episode.

Everything that would fork, bind, sleep or reach docker is a stand-in
(FakeDocker / FakeHealth from test_mac_daemon.py, patched subprocess,
socket and threads).
"""

import io
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

import onionpress  # noqa: E402
from onionpress import config as op_config  # noqa: E402
from onionpress import daemon as op_daemon  # noqa: E402
from onionpress import onion_proxy  # noqa: E402
from onionpress import ports as op_ports  # noqa: E402
from onionpress import thumbnails  # noqa: E402
from onionpress.health import WedgeSignals  # noqa: E402
from tests.test_mac_daemon import ADDR, _DaemonCase  # noqa: E402

Daemon = op_daemon.Daemon


class _Lines:
    """A RotatingLog stand-in."""

    def __init__(self):
        self.lines = []

    def write(self, line):
        self.lines.append(line)

    def current_path(self):
        return "/dev/null"


class _PathsCase(_DaemonCase):

    def setUp(self):
        super().setUp()
        self.logged = []
        self.d._log_func = self.logged.append
        # the proxy's module globals and the environment are restored
        for name in ("PROXY_PORT", "PHP_PROXY_PORT"):
            p = mock.patch.object(onion_proxy, name, getattr(onion_proxy, name))
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.dict(os.environ)
        p.start()
        self.addCleanup(p.stop)

    def saw(self, text):
        return any(text in line for line in self.logged)


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------

class TestRunDaemonCommand(unittest.TestCase):

    def setUp(self):
        self.paths = types.SimpleNamespace(data_dir="/d")
        self.log = mock.Mock()
        self.status = {"pid": 77, "loaded": True, "state_fresh": True,
                       "display_state": "available"}
        for name, value in (("agent_status", mock.Mock(return_value=self.status)),
                            ("start_agent", mock.Mock(return_value=True)),
                            ("stop_agent", mock.Mock(return_value=True)),
                            ("request_offline", mock.Mock(return_value=True))):
            p = mock.patch.object(op_daemon, name, value)
            setattr(self, name, p.start())
            self.addCleanup(p.stop)

    def cmd(self, sub, **kw):
        out = io.StringIO()
        rc = op_daemon.run_daemon_command(sub, self.paths, self.log, out=out, **kw)
        return rc, out.getvalue()

    def test_serve_is_the_default(self):
        with mock.patch.object(op_daemon, "Daemon") as D:
            self.assertEqual(self.cmd(None, version="1.2", launcher_script="/l")[0], 0)
        D.assert_called_once_with(self.paths, version="1.2", launcher_script="/l")
        D.return_value.run.assert_called_once()

    def test_start_passes_the_preset_offset(self):
        with mock.patch.dict(os.environ, {"ONIONPRESS_PORT_OFFSET": "10000"}):
            rc, out = self.cmd("start", launcher_script="/l", json_output=True)
        self.assertEqual(rc, 0)
        self.start_agent.assert_called_once_with("/l", "/d", self.log, port_offset="10000")
        self.assertIn('"ok": true', out.replace(" ", " "))

    def test_a_failed_start_says_so(self):
        self.start_agent.return_value = False
        rc, out = self.cmd("start")
        self.assertEqual(rc, 1)
        self.assertIn("could not be started", out)

    def test_stop_and_no_offline(self):
        self.assertEqual(self.cmd("stop", no_offline=True)[0], 0)
        self.stop_agent.assert_called_once_with("/d", self.log, offline=False)
        self.stop_agent.return_value = False
        rc, out = self.cmd("stop", json_output=True)
        self.assertEqual(rc, 1)
        self.assertIn('"ok": false', out)

    def test_status_text_and_json(self):
        rc, out = self.cmd("status")
        self.assertEqual(rc, 0)
        self.assertIn("running (pid 77)", out)
        self.assertIn("agent loaded", out)
        self.assertIn("(available)", out)
        self.status.update(pid=None, loaded=False, state_fresh=False, display_state="")
        out = self.cmd("status")[1]
        self.assertIn("not running", out)
        self.assertIn("stale or missing", out)
        self.assertIn('"loaded": false', self.cmd("status", json_output=True)[1])

    def test_offline(self):
        self.assertEqual(self.cmd("offline", json_output=True), (0, '{"ok": true}\n'))
        self.request_offline.return_value = False
        self.assertEqual(self.cmd("offline")[0], 1)

    def test_unknown(self):
        with mock.patch.object(sys, "stderr", io.StringIO()) as err:
            self.assertEqual(self.cmd("dance")[0], 2)
        self.assertIn("Unknown daemon command: dance", err.getvalue())


# ---------------------------------------------------------------------------
# run(), the signals and the main loop
# ---------------------------------------------------------------------------

class TestRun(_PathsCase):

    def test_run_wires_signals_answers_them_and_cleans_up(self):
        d = self.d
        order = []
        d._setup_environment = lambda: order.append("env")
        d._resolve_ports_at_start = lambda: order.append("ports")
        real_publish = d.publish_state

        def publish():
            order.append("publish")
            real_publish()
        d.publish_state = publish
        d._register_power_events = lambda: order.append("power")
        d._status_loop = lambda: order.append("status-loop")
        d.start_thumbnail_generator = lambda: order.append("thumbs")
        d._offline_now = mock.Mock()
        d.handle_wake = mock.Mock()
        handlers = {}
        pid_seen = []
        turns = iter([lambda: d._on_usr1(signal.SIGUSR1, None),
                      lambda: d._on_usr2(signal.SIGUSR2, None),
                      lambda: d._on_term(signal.SIGTERM, None)])

        def turn(seconds):
            pid_seen.append(os.path.exists(op_daemon.pid_path(self.dir)))
            next(turns)()
        d._main_loop_once = turn
        with mock.patch.object(op_daemon.signal, "signal",
                               side_effect=lambda s, h: handlers.__setitem__(s, h)):
            d.run()
        self.assertEqual(handlers, {signal.SIGTERM: d._on_term, signal.SIGINT: d._on_term,
                                    signal.SIGUSR1: d._on_usr1, signal.SIGUSR2: d._on_usr2})
        self.assertEqual(order[:4], ["env", "ports", "publish", "power"])
        self.assertIn("thumbs", order)
        d._offline_now.assert_called_once()
        d.handle_wake.assert_called_once()
        self.assertTrue(pid_seen[0])
        # _shutdown: pid file gone, the offset left for the next daemon
        self.assertFalse(os.path.exists(op_daemon.pid_path(self.dir)))
        with open(os.path.join(self.dir, ".previous-port-offset")) as f:
            self.assertEqual(f.read(), str(d.port_offset))
        self.assertTrue(self.proxy_stopped)
        d.caffeine.stop.assert_called()
        self.assertTrue(self.saw("OnionPress daemon stopped"))
        self.assertTrue(self.saw("Received signal"))

    def test_an_unwritable_pid_file_is_logged(self):
        self.d.app_support = os.path.join(self.dir, "missing", "deeper")
        self.d._write_pid()
        self.assertTrue(self.saw("could not write the pid file"))

    def test_someone_elses_pid_file_is_left(self):
        with open(op_daemon.pid_path(self.dir), "w") as f:
            f.write("1")
        self.d._remove_pid()
        self.assertTrue(os.path.exists(op_daemon.pid_path(self.dir)))

    def test_status_loop_survives_an_error_and_sleeps_by_state(self):
        d = self.d
        calls = []

        def check():
            calls.append(1)
            raise RuntimeError("boom")
        d.check_status = check
        waits = []

        def wait(t):
            waits.append(t)
            if len(waits) == 1:
                d._sleeping = True
            else:
                d._stop.set()
            return d._stop.is_set()
        d._stop.wait = wait
        d._status_loop()
        self.assertEqual(len(calls), 1)
        self.assertTrue(self.saw("ERROR in the status loop: boom"))
        self.assertEqual(waits[0], op_daemon.poll_interval(d.display_state))
        self.assertEqual(waits[1], 30)       # asleep: no passes

    def test_main_loop_once_without_iokit_waits(self):
        self.d._power = None
        self.d._stop.set()
        t = time.monotonic()
        self.d._main_loop_once(5)
        self.assertLess(time.monotonic() - t, 1)

    def test_main_loop_once_runs_the_cfrunloop(self):
        # The handler turns the run loop (MacPowerHandler.run_loop_once);
        # True means the turn was taken and the daemon does not wait.
        self.d._power = types.SimpleNamespace(run_loop_once=mock.Mock(return_value=True))
        self.d._stop.wait = mock.Mock()
        self.d._main_loop_once(1.0)
        self.d._power.run_loop_once.assert_called_once_with(1.0)
        self.d._stop.wait.assert_not_called()

    def power(self, register):
        class Handler:
            def __init__(self, on_will_sleep, on_has_powered_on, log):
                Handler.hooks = (on_will_sleep, on_has_powered_on)

            def register(self):
                if isinstance(register, Exception):
                    raise register
                return register
        self.d._power = None
        with mock.patch.object(self.d, "_power_handler_class", return_value=Handler):
            self.d._register_power_events()
        return Handler

    def test_the_handler_is_the_platforms(self):
        from onionpress.mac_power import MacPowerHandler
        from onionpress.power import LogindPowerHandler
        self.assertIs(self.d._power_handler_class(), MacPowerHandler)
        self.d.os_name = op_daemon.OS.LINUX
        self.assertIs(self.d._power_handler_class(), LogindPowerHandler)

    def test_power_events_registered(self):
        H = self.power(True)
        self.assertIsNotNone(self.d._power)
        self.assertTrue(self.saw("Registered for sleep and wake events (Handler)"))
        # the hooks do nothing while quitting
        self.d.handle_sleep = mock.Mock()
        self.d.handle_wake = mock.Mock()
        self.d._quitting = True
        H.hooks[0]()
        H.hooks[1]()
        self.d.handle_sleep.assert_not_called()
        self.d._quitting = False
        H.hooks[0]()
        H.hooks[1]()
        self.d.handle_sleep.assert_called_once()
        self.d.handle_wake.assert_called_once()

    def test_power_events_unavailable(self):
        self.power(False)
        self.assertIsNone(self.d._power)
        self.assertTrue(self.saw("clock gap only"))
        self.power(RuntimeError("no IOKit"))
        self.assertTrue(self.saw("power handler init failed: no IOKit"))

    def test_shutdown_sends_offline_only_when_quitting(self):
        d = self.d
        d.hub_client = mock.Mock(registration_succeeded=True)
        d._shutdown()
        d.hub_client.notify_offline.assert_not_called()
        open(os.path.join(self.dir, op_daemon.QUITTING_MARKER), "w").close()
        d.hub_client.notify_offline.side_effect = OSError("tor down")
        d._shutdown()
        d.hub_client.notify_offline.assert_called_once()
        self.assertFalse(os.path.exists(os.path.join(self.dir, op_daemon.QUITTING_MARKER)))

    def test_offline_now_acknowledges_even_when_the_hub_fails(self):
        d = self.d
        d.hub_client = mock.Mock(registration_succeeded=True)
        d.hub_client.notify_offline.side_effect = RuntimeError("nope")
        d._offline_now()
        self.assertTrue(os.path.exists(os.path.join(self.dir, op_daemon.OFFLINE_ACK_MARKER)))
        self.assertTrue(self.saw("/offline before stop failed: nope"))
        d.hub_client.notify_offline.side_effect = None
        d.hub_client.notify_offline.return_value = False
        d._offline_now()
        self.assertTrue(self.saw("/offline before stop MISSED"))

    def test_version_info_is_logged(self):
        self.d._had_cached_address = True
        self.d._log_version_info()
        self.assertTrue(self.saw("Version: 9.9"))
        self.assertTrue(self.saw(f"Data dir: {self.dir}"))


# ---------------------------------------------------------------------------
# Start-up: the environment and the ports
# ---------------------------------------------------------------------------

class TestSetupEnvironment(_PathsCase):

    def setUp(self):
        super().setUp()
        self.home = os.path.join(self.dir, "home")
        os.makedirs(self.home)
        os.environ["HOME"] = self.home
        self.bin = os.path.join(self.dir, "bin")
        os.makedirs(self.bin)
        self.d.bin_dir = self.bin
        self.d.colima_home = os.path.join(self.dir, "fresh-colima")
        self.cfg = os.path.join(self.dir, "docker-config")

    def test_this_install_s_colima_and_docker(self):
        os.environ["PATH"] = "/usr/bin"
        with open(os.path.join(self.bin, "docker-compose"), "w") as f:
            f.write("#!/bin/sh\n")
        self.d._setup_environment()
        self.assertEqual(os.environ["PATH"], f"{self.bin}:/usr/bin")
        self.assertEqual(os.environ["COLIMA_HOME"], self.d.colima_home)
        self.assertEqual(os.environ["LIMA_HOME"], os.path.join(self.d.colima_home, "_lima"))
        self.assertTrue(os.path.isdir(os.environ["LIMA_HOME"]))   # #370
        self.assertEqual(os.environ["LIMA_INSTANCE"], "onionpress")
        self.assertEqual(os.environ["DOCKER_HOST"], f"unix://{self.d.docker_socket}")
        self.assertEqual(os.environ["DOCKER_CONFIG"], self.cfg)
        with open(os.path.join(self.cfg, "config.json")) as f:
            self.assertIn('"currentContext": "colima"', f.read())
        link = os.path.join(self.cfg, "cli-plugins", "docker-compose")
        self.assertEqual(os.readlink(link), os.path.join(self.bin, "docker-compose"))

    def test_an_existing_config_and_plugin_are_kept(self):
        os.makedirs(os.path.join(self.cfg, "cli-plugins"))
        with open(os.path.join(self.cfg, "config.json"), "w") as f:
            f.write('{"mine": 1}')
        with open(os.path.join(self.cfg, "cli-plugins", "docker-compose"), "w") as f:
            f.write("mine")
        with open(os.path.join(self.bin, "docker-compose"), "w") as f:
            f.write("bundled")
        self.d._setup_environment()
        with open(os.path.join(self.cfg, "config.json")) as f:
            self.assertEqual(f.read(), '{"mine": 1}')
        self.assertFalse(os.path.islink(os.path.join(self.cfg, "cli-plugins", "docker-compose")))

    def test_without_a_bundled_compose_the_users_is_linked(self):
        user_plugins = os.path.join(self.home, ".docker", "cli-plugins")
        os.makedirs(user_plugins)
        target = os.path.join(self.dir, "brew-compose")
        open(target, "w").close()
        os.symlink(target, os.path.join(user_plugins, "docker-compose"))
        self.d._setup_environment()
        self.assertEqual(os.readlink(os.path.join(self.cfg, "cli-plugins", "docker-compose")),
                         os.path.join(user_plugins, "docker-compose"))

    def test_no_bin_dir_leaves_path(self):
        self.d.bin_dir = ""
        os.environ["PATH"] = "/usr/bin"
        self.d._setup_environment()
        self.assertEqual(os.environ["PATH"], "/usr/bin")
        self.assertFalse(os.path.exists(os.path.join(self.cfg, "cli-plugins", "docker-compose")))


class _BindSocket:
    """socket.socket stand-in: *busy* ports refuse bind, a number of times
    each (None: always)."""

    def __init__(self, busy, bound):
        self.busy, self.bound = busy, bound

    def __call__(self, *a, **k):
        return self

    def settimeout(self, t):
        pass

    def bind(self, addr):
        self.bound.append(addr)
        assert addr[0] == "127.0.0.1"
        left = self.busy.get(addr[1], 0)
        if left is None or left > 0:
            if left:
                self.busy[addr[1]] = left - 1
            raise OSError(48, "Address already in use")

    def close(self):
        pass


class TestResolvePorts(_PathsCase):

    def setUp(self):
        super().setUp()
        self.d.bin_dir = "/b"
        self.runs = []
        p = mock.patch("subprocess.run", side_effect=lambda argv, **kw: self.runs.append(argv))
        p.start()
        self.addCleanup(p.stop)
        self.sleeps = []
        p = mock.patch.object(op_daemon.time, "sleep", self.sleeps.append)
        p.start()
        self.addCleanup(p.stop)
        self.sentinel = os.path.join(self.dir, ".previous-port-offset")

    def resolve(self, pc=None, raises=None, busy=None):
        bound = []
        resolver = mock.Mock(return_value=pc, side_effect=raises)
        with mock.patch.object(op_ports, "resolve_port_offset", resolver), \
                mock.patch("socket.socket", _BindSocket(busy or {}, bound)):
            self.d._resolve_ports_at_start()
        return bound

    def test_the_resolved_offset_is_published_everywhere(self):
        self.resolve(op_config.PortConfig.from_offset(10000))
        self.assertEqual(self.d.port_offset, 10000)
        self.assertEqual(self.d.wp_port, 18080)
        self.assertEqual(os.environ["ONIONPRESS_PORT_OFFSET"], "10000")
        self.assertEqual(os.environ["ONIONPRESS_WP_PORT"], "18080")
        self.assertEqual(onion_proxy.PROXY_PORT, self.d.proxy_port)
        self.assertEqual(onion_proxy.PHP_PROXY_PORT, 18080)
        self.d.publish_state()
        ports = self.state()["ports"]
        self.assertEqual(ports, {"offset": 10000, "wp": 18080,
                                 "socks": self.d.socks_port, "proxy": self.d.proxy_port})
        self.assertTrue(self.saw("Ports: offset 10000 (WordPress 18080"))

    def test_failed_detection_keeps_the_environment_s_offset(self):
        self.d._apply_ports(op_config.PortConfig.from_offset(20000))
        self.resolve(raises=RuntimeError("no socket"))
        self.assertEqual(self.d.port_offset, 20000)
        self.assertTrue(self.saw("port detection failed (no socket)"))

    def test_a_running_colima_is_never_stopped(self):
        # With this daemon's own pid file just written and no other daemon
        # alive, a running VM is a crash restart's or a terminal start's
        # live site: its port is read, nothing is run against it.
        self.resolve(op_config.PortConfig.from_offset(0))
        self.assertEqual(self.runs, [])

    def test_waits_for_the_previous_instance_s_port(self):
        with open(self.sentinel, "w") as f:
            f.write("10000")
        bound = self.resolve(op_config.PortConfig.from_offset(10000), busy={18080: 2})
        self.assertEqual(bound, [("127.0.0.1", 18080)] * 3)
        self.assertEqual(self.sleeps, [0.5, 0.5])
        self.assertFalse(os.path.exists(self.sentinel))

    def test_gives_up_waiting_after_fifteen_seconds(self):
        with open(self.sentinel, "w") as f:
            f.write("0")
        bound = self.resolve(op_config.PortConfig.from_offset(10000), busy={8080: None})
        self.assertEqual(len(bound), 30)
        self.assertEqual(self.d.port_offset, 10000)

    def test_an_old_sentinel_is_removed_without_waiting(self):
        with open(self.sentinel, "w") as f:
            f.write("0")
        old = time.time() - 600
        os.utime(self.sentinel, (old, old))
        bound = self.resolve(op_config.PortConfig.from_offset(0))
        self.assertEqual(bound, [])
        self.assertFalse(os.path.exists(self.sentinel))

    def test_a_garbled_sentinel_does_not_stop_the_start(self):
        with open(self.sentinel, "w") as f:
            f.write("ten thousand")
        self.resolve(op_config.PortConfig.from_offset(0))
        self.assertEqual(self.d.port_offset, 0)

    def test_resync_follows_the_running_stack(self):
        self.d._apply_ports(op_config.PortConfig.from_offset(0))
        with mock.patch.object(op_daemon.launcher_ops, "get_running_wp_port", return_value=None):
            Daemon._resync_ports(self.d)
        self.assertEqual(self.d.wp_port, 8080)
        with mock.patch.object(op_daemon.launcher_ops, "get_running_wp_port", return_value=18080):
            Daemon._resync_ports(self.d)
        self.assertEqual(self.d.port_offset, 10000)
        self.assertEqual(os.environ["ONIONPRESS_WP_PORT"], "18080")
        self.assertTrue(self.saw("Port resync: was 8080, running stack is on 18080"))

    def test_port_conflict_lists_the_bound_ports(self):
        self.d._apply_ports(op_config.PortConfig.from_offset(0))
        with mock.patch("socket.socket", _BindSocket({8080: None, self.d.proxy_port: None}, [])):
            self.assertEqual(self.d.check_port_conflict(), [8080, self.d.proxy_port])


# ---------------------------------------------------------------------------
# The extension's proxy
# ---------------------------------------------------------------------------

class _FakeServer:
    instances = []

    def __init__(self, addr, handler, ports=None):
        self.addr, self.handler, self.ports = addr, handler, ports
        self.shutdown_called = False
        _FakeServer.instances.append(self)

    def serve_forever(self):
        self.state_while_serving = _FakeServer.state_reader()

    def shutdown(self):
        self.shutdown_called = True


class TestOnionProxy(_PathsCase):

    def setUp(self):
        super().setUp()
        _FakeServer.instances = []
        _FakeServer.state_reader = self.state
        self.d.resources_dir = os.path.join(self.dir, "Resources")
        os.makedirs(self.d.resources_dir)
        self.d.onion_address = ADDR
        self.install = mock.Mock()
        for name, value in (("ThreadingHTTPServer", _FakeServer),
                            ("install_php_proxy", self.install)):
            p = mock.patch.object(onion_proxy, name, value)
            p.start()
            self.addCleanup(p.stop)

    def start(self):
        Daemon.start_onion_proxy(self.d)
        if self.d.proxy_thread is not None:
            self.d.proxy_thread.join(5)

    def test_binds_localhost_on_this_user_s_port(self):
        self.d._apply_ports(op_config.PortConfig.from_offset(10000))
        self.start()
        (srv,) = _FakeServer.instances
        self.assertEqual(srv.addr, ("127.0.0.1", self.d.proxy_port))
        self.assertIs(srv.handler, onion_proxy.OnionProxyHandler)
        self.assertIs(srv.ports(), self.d.ports)   # live, not a snapshot
        self.assertEqual(srv.onion_address, ADDR)
        self.assertEqual(srv.data_dir, self.dir)
        self.assertEqual(srv.launcher_script, "/x/onionpress")
        self.assertEqual(srv.docker_bin, self.docker.docker_bin)
        self.assertTrue(srv.state_while_serving["proxy_running"])
        self.install.assert_not_called()   # no onion-forward.php shipped

    def test_the_php_forwarder_is_installed_when_shipped(self):
        php = os.path.join(self.d.resources_dir, "onion-forward.php")
        open(php, "w").close()
        self.start()
        self.install.assert_called_once_with(self.docker.docker_bin, self.docker.env, php,
                                             log_func=self.d.log)

    def test_not_started_twice_or_while_quitting(self):
        self.d.proxy_server = object()
        self.start()
        self.d.proxy_server = None
        self.d._quitting = True
        self.start()
        self.assertEqual(_FakeServer.instances, [])

    def test_a_bind_failure_is_logged(self):
        with mock.patch.object(onion_proxy, "ThreadingHTTPServer",
                               side_effect=OSError("Address already in use")):
            self.start()
        self.assertIsNone(self.d.proxy_server)
        self.assertTrue(self.saw("Onion proxy failed to start: Address already in use"))

    def test_stop(self):
        srv = _FakeServer(("127.0.0.1", 1), None)
        self.d.proxy_server = srv
        Daemon.stop_onion_proxy(self.d)
        self.assertTrue(srv.shutdown_called)
        self.assertIsNone(self.d.proxy_server)
        bad = mock.Mock()
        bad.shutdown.side_effect = RuntimeError("stuck")
        self.d.proxy_server = bad
        Daemon.stop_onion_proxy(self.d)
        self.assertIsNone(self.d.proxy_server)
        self.assertTrue(self.saw("Error stopping onion proxy: stuck"))
        Daemon.stop_onion_proxy(self.d)   # nothing running: nothing to do


# ---------------------------------------------------------------------------
# The onionname retry
# ---------------------------------------------------------------------------

class TestOnionnameRetry(_PathsCase):

    def setUp(self):
        super().setUp()
        with open(os.path.join(self.dir, "config"), "a") as f:
            f.write("ONIONNAME=mysite\n")
        self.d._read_served_address = lambda: ADDR
        self.d._onionname_retry_in_flight = True

    def retry(self, result=None, raises=None):
        from onionpress import onionnames_registrar
        reg = mock.Mock()
        reg.register.return_value = result
        if raises:
            reg.register.side_effect = raises
        with mock.patch.object(onionnames_registrar, "Registrar", return_value=reg) as R:
            self.d._retry_pending_onionname()
        self.assertFalse(self.d._onionname_retry_in_flight)
        return R, reg

    def config_sets(self):
        return [c[2:4] for c in self.launcher_calls if c[:2] == ["config", "set"]]

    def test_success_is_saved_through_config_set(self):
        from onionpress.onionnames_registrar import RegistrarResult
        R, reg = self.retry(RegistrarResult(status="ok"))
        R.assert_called_once_with(docker_bin=self.docker.docker_bin, log=self.d.log)
        reg.register.assert_called_once_with("mysite", ADDR)
        self.assertEqual(self.config_sets(), [["ONIONNAME", "mysite"],
                                              ["ONIONNAME_ADDRESS", ADDR],
                                              ["ONIONNAME_REGISTERED", "yes"]])
        self.assertTrue(self.d._onionname_retry_giveup)
        self.assertIsNone(self.state()["onionname_collision"])

    def test_a_collision_goes_to_the_menubar(self):
        from onionpress.onionnames_registrar import RegistrarResult
        self.retry(RegistrarResult(status="collision", suggestions=["mysite2", "mysite-blog"]))
        self.assertEqual(self.config_sets(), [])
        self.assertTrue(self.d._onionname_retry_giveup)
        self.assertEqual(self.state()["onionname_collision"],
                         {"name": "mysite", "suggestions": ["mysite2", "mysite-blog"]})

    def test_an_unreachable_registrar_tries_again_later(self):
        from onionpress.onionnames_registrar import RegistrarResult
        self.retry(RegistrarResult(status="unreachable"))
        self.assertFalse(self.d._onionname_retry_giveup)
        self.assertTrue(self.saw("retry deferred (unreachable)"))

    def test_an_error_is_logged_and_clears_the_flag(self):
        self.retry(raises=RuntimeError("socat died"))
        self.assertTrue(self.saw("retry thread error: socat died"))

    def test_nothing_without_a_name_or_an_address(self):
        self.d._read_served_address = lambda: ""
        R, _ = self.retry()
        R.assert_not_called()
        with open(os.path.join(self.dir, "config"), "w") as f:
            f.write("ONIONNAME=\n")
        R, _ = self.retry()
        R.assert_not_called()

    def test_hub_loops_start_the_retry_once(self):
        d = self.d
        d.is_ready = True
        d.onion_address = ADDR
        d._onionheaven_checked = True
        d._analytics_sharing_started = True
        d._onionname_retry_in_flight = False
        d.hub_client = mock.Mock()
        started = []
        with mock.patch.object(op_daemon.threading, "Thread") as T:
            T.return_value.start.side_effect = lambda: started.append(1)
            Daemon._hub_loops(d)
            Daemon._hub_loops(d)       # in flight: not again
        self.assertEqual(len(started), 1)
        self.assertEqual(T.call_args.kwargs["target"], d._retry_pending_onionname)
        d.hub_client.tick.assert_called_with(True, True)


# ---------------------------------------------------------------------------
# The manual analytics upload
# ---------------------------------------------------------------------------

class TestManualAnalyticsUpload(_PathsCase):

    def upload(self, result=None, raises=None):
        from onionpress import analytics_sharing
        with mock.patch.object(analytics_sharing, "upload_cycle",
                               return_value=result, side_effect=raises) as up:
            self.d._manual_analytics_upload()
        return up

    def test_offline_queues_it(self):
        self.d.is_ready = True
        self.health.internet = False
        up = self.upload()
        up.assert_not_called()
        self.assertTrue(self.d._pending_manual_upload)
        self.health.internet = True
        self.d.is_ready = False
        self.d._pending_manual_upload = False
        self.upload().assert_not_called()
        self.assertTrue(self.d._pending_manual_upload)

    def test_each_outcome_has_its_own_line(self):
        self.d.is_ready = True
        cases = [
            ({"status": "ok", "wanted": 3, "uploaded": 3}, "Analytics upload complete: 3/3"),
            ({"status": "ok", "wanted": 3, "uploaded": 1}, "Analytics upload partial: 1/3"),
            ({"status": "manifest_failed"}, "could not reach OnionHome"),
            ({"status": "none_wanted"}, "already has everything"),
            ({"status": "no_files"}, "no logs to share yet"),
            ({"status": "sign_error"}, "manifest signing error"),
            ({"status": "no_onion"}, "onion address not yet available"),
            ({"status": "weird"}, "finished (status=weird)"),
            (None, "finished (status=unknown)"),
        ]
        for result, line in cases:
            with self.subTest(result=result):
                up = self.upload(result)
                up.assert_called_once_with(self.d, include_active=True)
                self.assertTrue(self.saw(line), self.logged[-1])

    def test_an_error_is_logged(self):
        self.d.is_ready = True
        self.upload(raises=RuntimeError("tor"))
        self.assertTrue(self.saw("Analytics upload error: tor"))


# ---------------------------------------------------------------------------
# Log captures
# ---------------------------------------------------------------------------

class _Proc:
    def __init__(self, lines, wait_raises=False):
        self.stdout = iter(lines)
        self.wait_raises = wait_raises
        self.terminated = self.killed = False

    def wait(self, timeout=None):
        if self.wait_raises:
            raise subprocess.TimeoutExpired("docker", timeout)
        return 0

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class TestContainerCapture(_PathsCase):

    def setUp(self):
        super().setUp()
        self.d._capture_shutdown = threading.Event()
        self.d._takeover_logs = {}

    def test_start_runs_one_supervisor(self):
        d = self.d
        d._capture_supervisor_thread = None
        with mock.patch.object(op_daemon.threading, "Thread") as T:
            Daemon.start_container_log_capture(d)
            Daemon.start_container_log_capture(d)
        self.assertEqual(T.call_count, 1)
        self.assertEqual(T.call_args.kwargs["target"], d._capture_supervisor_loop)

    def test_supervisor_captures_what_docker_ps_shows(self):
        d = self.d
        launched = []
        d._docker_ps_names = lambda b, e: {"onionpress-tor", "onionpress-db",
                                           "onionheaven-takeover-3", "someone-else"}
        alive = mock.Mock()
        alive.is_alive.return_value = True
        d._container_log_processes["onionpress-db"] = {"thread": alive}
        d._launch_capture_worker = lambda name, log, b, e: launched.append((name, log))
        d._capture_shutdown.wait = lambda t: d._capture_shutdown.set()
        d._capture_supervisor_loop()
        names = dict(launched)
        self.assertEqual(set(names), {"onionpress-tor", "onionheaven-takeover-3"})
        self.assertIs(names["onionpress-tor"], d._tor_log)
        self.assertIs(d._takeover_logs["onionheaven-takeover-3"],
                      names["onionheaven-takeover-3"])

    def test_supervisor_survives_an_error(self):
        d = self.d
        d._docker_ps_names = mock.Mock(side_effect=RuntimeError("ps"))
        d._capture_shutdown.wait = lambda t: d._capture_shutdown.set()
        d._capture_supervisor_loop()
        self.assertTrue(self.saw("Capture supervisor: ps"))

    def test_launch_worker_registers_the_thread(self):
        with mock.patch.object(op_daemon.threading, "Thread") as T:
            self.d._launch_capture_worker("onionpress-tor", self.d._tor_log, "/x/docker", {})
        entry = self.d._container_log_processes["onionpress-tor"]
        self.assertIs(entry["thread"], T.return_value)
        T.return_value.start.assert_called_once()

    def test_worker_tails_then_resumes_since_and_ends_with_the_container(self):
        d = self.d
        log = _Lines()
        d._container_log_processes["onionpress-tor"] = {"thread": None, "proc": None,
                                                        "last_ts": None}
        argvs = []
        procs = iter([_Proc(["a\n", "b\n"]), _Proc(["c\n"], wait_raises=True)])

        def popen(cmd, **kw):
            argvs.append(cmd)
            return next(procs)
        ps = iter([{"onionpress-tor"}, set()])
        d._docker_ps_names = lambda b, e: next(ps)
        with mock.patch.object(op_daemon.subprocess, "Popen", popen), \
                mock.patch.object(op_daemon.time, "sleep"):
            d._capture_worker("onionpress-tor", log, "/x/docker", {"E": "1"})
        self.assertEqual(log.lines, ["a\n", "b\n", "c\n"])
        self.assertEqual(argvs[0], ["/x/docker", "logs", "-f", "--tail", "100", "onionpress-tor"])
        self.assertEqual(argvs[1][:4], ["/x/docker", "logs", "-f", "--since"])
        self.assertNotIn("onionpress-tor", d._container_log_processes)

    def test_worker_retries_a_failed_popen_until_shutdown(self):
        d = self.d
        d._container_log_processes["onionpress-tor"] = {"last_ts": None}
        sleeps = []

        def sleep(s):
            sleeps.append(s)
            d._capture_shutdown.set()
        with mock.patch.object(op_daemon.subprocess, "Popen", side_effect=OSError("no docker")), \
                mock.patch.object(op_daemon.time, "sleep", sleep):
            d._capture_worker("onionpress-tor", _Lines(), "/x/docker", {})
        self.assertEqual(sleeps, [10])

    def test_worker_ends_when_its_entry_is_gone(self):
        with mock.patch.object(op_daemon.subprocess, "Popen") as P:
            self.d._capture_worker("gone", _Lines(), "/x/docker", {})
        P.assert_not_called()

    def test_docker_ps_names(self):
        def run(stdout="", rc=0, raises=None):
            m = mock.Mock(return_value=subprocess.CompletedProcess([], rc, stdout=stdout),
                          side_effect=raises)
            with mock.patch.object(op_daemon.subprocess, "run", m):
                return self.d._docker_ps_names("/x/docker", {})
        self.assertEqual(run("onionpress-tor\n onionpress-db \n\n"),
                         {"onionpress-tor", "onionpress-db"})
        self.assertEqual(run("onionpress-tor\n", rc=1), set())
        self.assertEqual(run(raises=subprocess.TimeoutExpired("docker", 10)), set())

    def test_stop_ends_every_capture(self):
        d = self.d
        ok, stuck = _Proc([]), _Proc([], wait_raises=True)
        thread = mock.Mock()
        d._container_log_processes.update({
            "onionpress-tor": {"proc": ok, "thread": thread},
            "onionpress-db": {"proc": stuck, "thread": None},
            "starting": {"proc": None, "thread": None},
        })
        d._capture_supervisor_thread = object()
        Daemon.stop_container_log_capture(d)
        self.assertTrue(d._capture_shutdown.is_set())
        self.assertTrue(ok.terminated and not ok.killed)
        self.assertTrue(stuck.killed)
        thread.join.assert_called_once_with(timeout=3)
        self.assertEqual(d._container_log_processes, {})
        self.assertIsNone(d._capture_supervisor_thread)

    def test_stop_before_any_start(self):
        d = self.d
        del d._capture_shutdown
        Daemon.stop_container_log_capture(d)
        self.assertEqual(d._container_log_processes, {})


class TestWebLogCapture(_PathsCase):

    def test_access_and_visitor_logs(self):
        d = self.d
        d._wp_access_log, d._wp_visitors_log = _Lines(), _Lines()
        proc = _Proc(['1.2.3.4 "GET /" "Firefox"\n', '127.0.0.1 "GET /" "OnionPress-HealthCheck"\n'])
        with mock.patch.object(op_daemon.subprocess, "Popen", return_value=proc) as P:
            Daemon.start_web_log_capture(d)
            d.web_log_thread.join(5)
            Daemon.start_web_log_capture(d)      # already running
        self.assertEqual(P.call_count, 1)
        self.assertEqual(P.call_args.args[0],
                         ["/x/docker", "logs", "-f", "--tail", "100", "onionpress-wordpress"])
        self.assertEqual(len(d._wp_access_log.lines), 2)
        self.assertEqual(len(d._wp_visitors_log.lines), 1)
        Daemon.stop_web_log_capture(d)
        self.assertTrue(proc.terminated)
        self.assertIsNone(d.web_log_process)

    def test_a_failed_start_and_a_stuck_stop(self):
        d = self.d
        with mock.patch.object(op_daemon.subprocess, "Popen", side_effect=OSError("x")):
            Daemon.start_web_log_capture(d)
        self.assertIsNone(d.web_log_process)
        self.assertTrue(self.saw("Error starting web log capture: x"))
        stuck = _Proc([], wait_raises=True)
        d.web_log_process = stuck
        d.web_log_thread = None
        Daemon.stop_web_log_capture(d)
        self.assertTrue(stuck.killed)


class TestClearnetCapture(_PathsCase):

    def test_new_clearnet_lines_once_each(self):
        d = self.d
        d.bin_dir = "/b"
        d._clearnet_log = _Lines()
        outputs = iter([
            (0, "x\n[1] CLEARNET a\n[2] other\n[3] CLEARNET b\n"),
            (1, ""),
            (0, "[1] CLEARNET a\n[3] CLEARNET b\n[4] CLEARNET c\n"),
        ])
        calls = []

        def run(argv, **kw):
            calls.append((argv, kw))
            rc, out = next(outputs, (0, ""))
            return subprocess.CompletedProcess(argv, rc, stdout=out)
        waits = []

        def wait(t):
            waits.append(t)
            if len(waits) >= 4:
                d._stop.set()
            return d._stop.is_set()
        d._stop.wait = wait
        with mock.patch.object(op_daemon.subprocess, "run", run):
            Daemon.start_clearnet_log_capture(d)
        self.assertEqual(d._clearnet_log.lines,
                         ["[1] CLEARNET a\n", "[3] CLEARNET b\n", "[4] CLEARNET c\n"])
        argv, kw = calls[0]
        self.assertEqual(argv, ["/b/limactl", "shell", "colima", "--", "sh", "-c", "sudo dmesg"])
        self.assertEqual(kw["env"]["COLIMA_HOME"], d.colima_home)
        self.assertEqual(kw["env"]["LIMA_HOME"], os.path.join(d.colima_home, "_lima"))
        self.assertEqual(waits, [60] * 4)

    def test_a_hung_limactl_is_survived(self):
        d = self.d
        d.bin_dir = ""
        d._stop.wait = lambda t: d._stop.set()
        with mock.patch.object(op_daemon.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("limactl", 15)) as run:
            Daemon.start_clearnet_log_capture(d)
        self.assertEqual(run.call_args.args[0][0], "limactl")


# ---------------------------------------------------------------------------
# Thumbnails
# ---------------------------------------------------------------------------

class _StopAfter:
    """An Event stand-in whose wait() ends the loop after *n* waits."""

    def __init__(self, n):
        self.n, self.waits, self._set = n, [], False

    def wait(self, t):
        self.waits.append(t)
        if len(self.waits) >= self.n:
            self._set = True
        return self._set

    def is_set(self):
        return self._set

    def set(self):
        self._set = True


class TestThumbnails(_PathsCase):

    def setUp(self):
        super().setUp()
        self.home = os.path.join(self.dir, "home")
        os.environ["HOME"] = self.home
        self.creations = os.path.join(self.home, "OnionPress", "Creations", "My Creations")
        os.makedirs(self.creations)
        self.src = os.path.join(self.creations, "talk.pdf")
        open(self.src, "w").close()

    def generator(self):
        with mock.patch.object(op_daemon.threading, "Thread") as T:
            Daemon.start_thumbnail_generator(self.d)
            Daemon.start_thumbnail_generator(self.d)      # once only
        self.assertEqual(T.call_count, 1)
        return T.call_args.kwargs["target"]

    def run_passes(self, passes, *, render=True, ql_raises=None, dir_ok=True, install_ok=True):
        target = self.generator()
        self.d._stop = _StopAfter(passes + 1)
        installs, ql = [], []

        def run(argv, **kw):
            ql.append(argv)
            if ql_raises:
                raise ql_raises
            out_dir, src = argv[-2], argv[-1]
            if render:
                with open(os.path.join(out_dir, os.path.basename(src) + ".png"), "wb") as f:
                    f.write(b"png")
            return subprocess.CompletedProcess(argv, 0)

        def install(creations, rel, rendered):
            installs.append((creations, rel, os.path.basename(rendered)))
            return install_ok
        with mock.patch.object(thumbnails, "thumbs_dir_ok", return_value=dir_ok), \
                mock.patch.object(thumbnails, "scan",
                                  return_value=([(self.src, "talk.pdf", 1.0)], ["old.png"], True)), \
                mock.patch.object(thumbnails, "prune", return_value=1) as prune, \
                mock.patch.object(thumbnails, "source_ok", return_value=True), \
                mock.patch.object(thumbnails, "install", install), \
                mock.patch.object(op_daemon.subprocess, "run", run):
            target()
        self.prune = prune
        return installs, ql

    def test_rendered_and_installed(self):
        installs, ql = self.run_passes(1)
        self.assertEqual(ql[0][:5], ["qlmanage", "-t", "-s", "400", "-o"])
        self.assertEqual(ql[0][-1], self.src)
        self.assertEqual(installs, [(self.creations, "talk.pdf", "talk.pdf.png")])
        self.prune.assert_called_with(self.creations, ["old.png"])
        self.assertTrue(self.saw("Removed 1 thumbnail(s)"))
        self.assertEqual(self.d._stop.waits, [10, 60])

    def test_a_failed_render_is_not_retried_until_the_file_changes(self):
        installs, ql = self.run_passes(3, render=False)
        self.assertEqual(len(ql), 1)
        self.assertEqual(installs, [])

    def test_a_qlmanage_timeout_is_not_retried(self):
        _, ql = self.run_passes(2, ql_raises=subprocess.TimeoutExpired("qlmanage", 30))
        self.assertEqual(len(ql), 1)

    def test_a_refused_install_is_not_retried(self):
        installs, ql = self.run_passes(2, install_ok=False)
        self.assertEqual(len(ql), 1)
        self.assertEqual(len(installs), 1)

    def test_a_linked_thumbs_folder_stops_it_with_one_warning(self):
        installs, ql = self.run_passes(3, dir_ok=False)
        self.assertEqual((installs, ql), ([], []))
        self.assertEqual(sum("not a real folder" in l for l in self.logged), 1)

    def test_stopped_before_the_first_pass(self):
        target = self.generator()
        self.d._stop = _StopAfter(1)
        with mock.patch.object(thumbnails, "scan") as scan:
            target()
        scan.assert_not_called()

    def test_an_error_is_logged_and_the_loop_goes_on(self):
        target = self.generator()
        self.d._stop = _StopAfter(2)
        with mock.patch.object(thumbnails, "thumbs_dir_ok", side_effect=OSError("EIO")):
            target()
        self.assertTrue(self.saw("Thumbnail generation error: EIO"))


# ---------------------------------------------------------------------------
# The VM wedge probe
# ---------------------------------------------------------------------------

class TestWedgeProbe(_PathsCase):

    def probe(self, signals, at):
        self.health.check_vm_wedge = lambda: signals
        with mock.patch.object(op_daemon.time, "time", return_value=at):
            self.d._probe_vm_wedge()

    def wedge_lines(self):
        return [l for l in self.logged if l.startswith("WEDGE")]

    def test_one_warning_per_episode_every_five_minutes_at_most(self):
        hot = WedgeSignals(loadavg_1min=25.0, wp_health_status="healthy", wp_failing_streak=0)
        self.probe(hot, 1000)
        self.assertEqual(self.wedge_lines(),
                         ["WEDGE alarm: loadavg1=25.00 wp_health=healthy wp_failing_streak=0"])
        self.probe(hot, 1100)          # within five minutes: not probed
        self.probe(hot, 1400)          # same episode: no second line
        self.assertEqual(len(self.wedge_lines()), 1)
        calm = WedgeSignals(loadavg_1min=0.5, wp_health_status="healthy", wp_failing_streak=0)
        self.probe(calm, 1800)         # recovered: the episode ends
        self.probe(hot, 2200)
        self.assertEqual(len(self.wedge_lines()), 2)

    def test_tiers(self):
        warn = WedgeSignals(loadavg_1min=6.0, wp_health_status="healthy", wp_failing_streak=0)
        self.probe(warn, 1000)
        self.assertTrue(self.wedge_lines()[-1].startswith("WEDGE warn: loadavg1=6.00"))
        streak = WedgeSignals(loadavg_1min=None, wp_health_status="unhealthy", wp_failing_streak=4)
        self.probe(streak, 2000)
        self.assertEqual(self.wedge_lines()[-1],
                         "WEDGE alarm: loadavg1=? wp_health=unhealthy wp_failing_streak=4")
        short = WedgeSignals(loadavg_1min=None, wp_health_status="unhealthy", wp_failing_streak=1)
        self.probe(short, 3000)
        self.assertIsNone(self.d._wedge_last_episode)

    def test_no_signals_is_no_episode(self):
        self.probe(None, 1000)
        self.assertEqual(self.wedge_lines(), [])
        self.assertEqual(self.d._wedge_probe_next, 1300)


if __name__ == "__main__":
    unittest.main()

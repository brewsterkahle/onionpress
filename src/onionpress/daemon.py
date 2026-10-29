"""`onionpress daemon`: the Mac's long-running loops, out of the menubar
(issue #367, CLI-first phase 3; the Mac counterpart of
linux/onionpress-heartbeat.py).

Until this module the menubar process (src/menubar.py) was both the view
and the service: the status loop with the dual probe and the
RepublishTrigger, the OnionHeaven heartbeat and registration upkeep, the
Settings page's handle-action tick, the log
captures, caffeinate, the My Creations thumbnails and the browser
extension's HTTP proxy all ran as threads inside the rumps app. They run
here now, in one per-user launchd agent
(~/Library/LaunchAgents/org.onionpress.daemon.plist, KeepAlive), and the
menubar reads what this process writes:

  ~/.onionpress/daemon-state.json   what the menu shows (address, state,
                                    ports, the pending question),
                                    rewritten atomically on every change
  ~/.onionpress/daemon.pid          this process
  status.json in the volume         as before, for the WordPress pages
  ~/.onionpress/onion_address       the cached address, as before

and asks for the few things the view decides, through files it writes:

  .onionname-retry                  a new onionname was chosen after a
                                    collision; register it now
  .reopen                           is the menubar's own (it opens the
                                    browser), the daemon leaves it alone

and signals: USR1 is "offline now" (a `stop`: /offline to the hub while
the tor container is still up; answered with .daemon-offline-ack), USR2
"online now" (wake), TERM a graceful end (/offline, the proxy, captures
and caffeinate stopped, exit 0: launchd's KeepAlive restarts only an
unsuccessful exit). Sleep and wake come from IOKit on the main thread
(onionpress.mac_power), as they did in the menubar.

The question a Settings-page request stops at (a key import, a restore of
another address: onionpress.handle_action's pending-action/) is not
answered here: it goes into the state file and the menubar asks the person
at the Mac, then runs `handle-action --confirmed … | --cancel` itself.

Everything that talks to the hub is onionpress.onionheaven's: the daemon
holds one OnionHeavenClient (hub_client, issue #479), which owns the
session's registration state and reads this object as its host, so the
Mac and the CLI's HubContext share one implementation and one lock
(_hub_lock). The dual probe is
onionpress.health's; the #266 recovery step on its outcome (the
RepublishTrigger, the request to the watchdog, the wake and offline
resets) is onionpress.reachability's RecoveryWindow, the one Linux's
ReachabilityLoop runs too.
"""

import json
import os
import plistlib
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone

from . import config as op_config
from . import confirm as op_confirm
from . import key_manager
from . import launcher_ops
from . import onion_proxy
from . import onionheaven
from . import redact
from . import thumbnails
from . import wp_files
from .docker import Docker
from .health import (
    HealthChecker,
    WEDGE_LOAD_WARN,
    WEDGE_LOAD_ALARM,
    WEDGE_FAILING_STREAK_ALARM,
    decode_curl_reason,
)
from .log_rotation import RotatingLog
from .power import CaffeineManager
from .reachability import RecoveryWindow
from .reachability_stats import ReachabilityStats
from .system_metrics import host_metrics, container_metrics

# The view's side (the state file's reader, the pid, the launchd agent)
# is onionpress.daemon_client's, which imports none of the loops' modules
# (issue #475); re-exported here, where the CLI, the uninstaller and the
# tests have always found it.
from .daemon_client import (  # noqa: F401
    AGENT_LABEL,
    OFFLINE_ACK_MARKER,
    ONIONNAME_RETRY_MARKER,
    PID_FILENAME,
    QUITTING_MARKER,
    STALE_AFTER_SECONDS,
    STALLED_AFTER_SECONDS,
    STATE_FILENAME,
    _gui_domain,
    _launchctl,
    agent_loaded_pid,
    agent_plist,
    agent_plist_path,
    agent_status,
    daemon_pid,
    display_state_of,
    install_agent,
    pid_alive,
    pid_path,
    read_state,
    remove_agent,
    request_offline,
    start_agent,
    state_is_fresh,
    state_path,
    stop_agent,
)

UPLOAD_TRIGGER_MARKER = ".upload-analytics"

PLACEHOLDER_ADDRESSES = ("Starting...", "Not running", "Generating address...")


def _utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# The state file (written here; read by the menubar through daemon_client)
# ---------------------------------------------------------------------------

def write_json_atomic(path, doc):
    """Write *doc* to *path* through a temp file and a rename, so a reader
    never sees a half-written file. mode 0600: the state names the
    address and the pending question's facts."""
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def poll_interval(display_state):
    """Seconds between status passes (the menubar's old adaptive schedule):
    30 s when the site is up, 10 s offline, 5 s while starting or stuck."""
    if display_state == "available":
        return 30
    if display_state == "offline":
        return 10
    return 5


def run_daemon_command(sub, paths, log, *, launcher_script="", json_output=False,
                       version="", out=None, no_offline=False):
    """`onionpress daemon [serve|start|stop|status|offline]`."""
    out = out or sys.stdout
    sub = sub or "serve"
    if sub == "serve":
        Daemon(paths, version=version, launcher_script=launcher_script).run()
        return 0
    if sub == "start":
        ok = start_agent(launcher_script, paths.data_dir, log,
                         port_offset=os.environ.get("ONIONPRESS_PORT_OFFSET"))
        if json_output:
            print(op_confirm.dumps({"ok": ok, **agent_status(paths.data_dir)}), file=out)
        elif not ok:
            print("The OnionPress daemon could not be started (see the log).", file=out)
        return 0 if ok else 1
    if sub == "stop":
        ok = stop_agent(paths.data_dir, log, offline=not no_offline)
        if json_output:
            print(op_confirm.dumps({"ok": ok, **agent_status(paths.data_dir)}), file=out)
        return 0 if ok else 1
    if sub == "status":
        doc = agent_status(paths.data_dir)
        if json_output:
            print(op_confirm.dumps(doc), file=out)
        else:
            running = f"running (pid {doc['pid']})" if doc.get("pid") else "not running"
            print(f"OnionPress daemon: {running}; agent "
                  f"{'loaded' if doc['loaded'] else 'not loaded'}; state "
                  f"{'fresh' if doc['state_fresh'] else 'stale or missing'}"
                  + (f" ({doc.get('display_state')})" if doc.get("display_state") else ""),
                  file=out)
        return 0
    if sub == "offline":
        ok = request_offline(paths.data_dir, log)
        if json_output:
            print(op_confirm.dumps({"ok": ok}), file=out)
        return 0 if ok else 1
    print(f"Unknown daemon command: {sub}", file=sys.stderr)
    return 2


# ---------------------------------------------------------------------------
# The daemon
# ---------------------------------------------------------------------------

class Daemon:
    """The loops. One instance per process; `run()` blocks until TERM."""

    is_onionheaven = False

    def __init__(self, paths, version="", launcher_script="", log_func=None,
                 docker=None, health_checker=None, data_dir=None):
        self.paths = paths
        self.app_support = data_dir or paths.data_dir
        self.config_file = paths.config_file
        self.bin_dir = paths.bin_dir
        self.colima_home = paths.colima_home
        self.docker_socket = paths.docker_socket
        self.launcher_script = launcher_script or (
            os.path.join(paths.app_bundle, "Contents", "MacOS", "onionpress")
            if paths.app_bundle else "")
        self.resources_dir = (os.path.join(paths.app_bundle, "Contents", "Resources")
                              if paths.app_bundle else "")
        self.version = version or self._read_bundle_version() or "unknown"
        os.makedirs(self.app_support, exist_ok=True)
        logs_dir = os.path.join(self.app_support, "logs")

        # Visitor-facing logs get IP pseudonymization on rotation
        # (redact.IP_SCRUB_TYPES, shared with the upload-time scrub).
        def _rotating(log_type):
            scrub = redact.make_scrub_fn(self.app_support,
                                         scrub_ips=redact.scrubs_ips(log_type))
            return RotatingLog(logs_dir, log_type, scrub_fn=scrub)

        self._onionpress_log = _rotating("onionpress")
        self._wp_access_log = _rotating("wordpress-access")
        self._wp_visitors_log = _rotating("wordpress-visitors")
        self._tor_log = _rotating("container-tor")
        self._onionheaven_log = _rotating("container-onionheaven")
        self._wp_errors_log = _rotating("container-wordpress")
        self._db_log = _rotating("container-db")
        self._cloudflared_log = _rotating("container-cloudflared")
        self._tor_client_log = _rotating("container-tor-client")
        self._clearnet_log = _rotating("clearnet")
        self._clearnet_last_offset = 0
        self._container_log_processes = {}
        self._log_func = log_func
        self.log_file = self._onionpress_log.current_path()

        self._docker = docker or Docker(paths, log_func=self.log)
        self._health_checker = health_checker or HealthChecker(self._docker, log_func=self.log)
        self._wedge_probe_next = 0.0
        self._wedge_last_episode = None

        self._stop = threading.Event()
        self._lock = threading.Lock()          # publish_state
        self._checking_lock = threading.Lock()
        self.checking = False
        self._settings_page_lock = threading.Lock()
        self._settings_page_busy = False
        self._settings_page_failed_at = 0.0
        self._settings_page_error = ""
        self._pending_question = None
        self.startup_time = time.time()

        # Ports: resolved in run() (the running stack's, else the first
        # free offset). Until then the environment's or the default.
        pc = op_config.PortConfig.from_offset(
            int(os.environ.get("ONIONPRESS_PORT_OFFSET", "0") or 0))
        self.port_offset = pc.offset
        self.wp_port = pc.wp_port
        self.socks_port = pc.socks_port
        self.proxy_port = pc.proxy_port
        self.runtime_ready = False
        self.port_conflict = []

        # The address cached by the last run, for an instant menu.
        self._had_cached_address = False
        self.onion_address = "Starting..."
        cached = op_config.cached_onion_address(self.app_support)
        if cached:
            self.onion_address = cached
            self._had_cached_address = True
        self.is_onionheaven = onionheaven.is_onionheaven_instance(self.onion_address)
        self.healthcheck_address = None
        self.is_running = False
        self.is_ready = False
        self._sleeping = False
        self.web_log_process = None
        self.web_log_thread = None
        self.last_status_logged = None
        self.caffeine = CaffeineManager(self.app_support, self.log, self.read_config_value)
        self.proxy_server = None
        self.proxy_thread = None
        self._wp_installed = None
        self._has_internet = True
        self._last_bootstrap_pct = 0
        self._bootstrap_stall_count = 0
        self._yellow_since = None
        self._last_check_complete_ts = time.time()
        self._was_ready = False
        self._tor_internally_ready = False
        self._wordpress_confirmed = False
        self._onionheaven_overview = None   # onionheaven.overview(), from check_status
        self._onionheaven_checked = False
        self._pending_manual_upload = False
        self._onionname_retry_in_flight = False
        self._onionname_retry_giveup = False
        self._onionname_collision = None
        self.cloudflare_tunnel_enabled = bool(self.read_config_value("CLOUDFLARE_TUNNEL_TOKEN"))
        self._quitting = False
        self._stopping = False
        self._consecutive_fail_count = 0
        self._wedge_warning_fired = False
        self._last_not_running = None
        self._s3_keys_reconcile_started = False
        self._analytics_sharing_started = False
        self._clearnet_capture_started = False
        self._capture_supervisor_thread = None
        self._images_present = {"wordpress": False, "mariadb": False, "tor": False}
        self._last_status_payload = None

        self._reachability_stats = ReachabilityStats()
        # The #266 recovery step (onionpress.reachability, shared with
        # Linux). Late-bound, so a replaced log or config reader is seen.
        self._recovery = RecoveryWindow(
            self._docker, lambda m: self.log(m),
            read_config_value=lambda k, d: self.read_config_value(k, d))
        self._last_probe_code = ""
        self._last_probe_why = ""
        self._last_probe_ms = 0
        self._last_reachability = (None, None)
        self._last_snapshot_ts = time.time()
        self._snapshot_interval_seconds = 12 * 3600

        onionheaven.ONIONHEAVEN_ADDRESS = onionheaven.configured_hub_address(
            self.read_config_value("ONIONHEAVEN_ADDRESS", ""), self.log)
        # The OnionHeaven client: registration, heartbeat, /offline, the
        # opt-out and the record's upkeep. Its hooks are late-bound, so a
        # replaced health checker or docker is seen.
        self.hub_client = onionheaven.OnionHeavenClient(
            self,
            sleeping=lambda: self._sleeping,
            wordpress_healthy=lambda: self._health_checker.check_wordpress_local(),
            docker=lambda: self._docker,
            drives_own_snapshot=True)

    # -- small helpers -----------------------------------------------------

    def _read_bundle_version(self):
        try:
            with open(os.path.join(self.paths.app_bundle, "Contents", "Info.plist"), "rb") as f:
                return plistlib.load(f).get("CFBundleShortVersionString", "")
        except (OSError, ValueError, AttributeError, TypeError):
            return ""

    def log(self, message):
        """Timestamped line in onionpress-YYYY-MM-DD-NNN.log (this process
        owns that log; the menubar writes menubar-*.log)."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] {message}\n"
        if self._log_func is not None:
            self._log_func(message)
        try:
            self._onionpress_log.write(line)
        except Exception:
            pass

    def read_config_value(self, key, default=""):
        return op_config.read_value(self.config_file, key, default)

    _read_config_value = read_config_value

    def check_internet_connectivity(self):
        return self._health_checker.check_internet_connectivity()

    def run_launcher(self, args, timeout=60, env=None):
        """`<launcher> <args>`: the returncode and stdout (stripped), or
        (None, None) when it could not run. stdin is /dev/null."""
        if not self.launcher_script:
            return None, None
        try:
            r = subprocess.run(
                [self.launcher_script, *args], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout,
                stdin=subprocess.DEVNULL, env={**os.environ, **(env or {})})
        except (OSError, subprocess.SubprocessError) as e:
            self.log(f"Error running onionpress {' '.join(args)}: {e}")
            return None, None
        return r.returncode, (r.stdout or "").strip()

    def _marker(self, name):
        return os.path.join(self.app_support, name)

    def _take_marker(self, name):
        """True once if the marker file exists (and remove it)."""
        path = self._marker(name)
        if not os.path.exists(path):
            return False
        try:
            os.remove(path)
        except OSError:
            pass
        return True

    @property
    def display_state(self):
        return display_state_of(self._state_fields(), time.time())

    @property
    def local_url(self):
        return f"http://localhost:{self.wp_port}"

    def _address_known(self):
        return bool(self.onion_address) and self.onion_address not in PLACEHOLDER_ADDRESSES

    # -- the state file ----------------------------------------------------

    def _state_fields(self):
        return {
            "is_running": self.is_running,
            "is_ready": self.is_ready,
            "has_internet": self._has_internet,
            "yellow_since": self._yellow_since,
            "last_check_complete": self._last_check_complete_ts,
        }

    def state_document(self, now=None):
        now = time.time() if now is None else now
        fields = self._state_fields()
        return {
            **fields,
            "pid": os.getpid(),
            "version": self.version,
            "updated_at": now,
            "started_at": self.startup_time,
            "display_state": display_state_of(fields, now),
            "runtime_ready": self.runtime_ready,
            "port_conflict": list(self.port_conflict),
            "ports": {"offset": self.port_offset, "wp": self.wp_port,
                      "socks": self.socks_port, "proxy": self.proxy_port},
            "onion_address": self.onion_address if self._address_known() else "",
            "address_placeholder": "" if self._address_known() else self.onion_address,
            "healthcheck_address": self.healthcheck_address or "",
            "is_onionheaven": bool(self.is_onionheaven),
            "bootstrap_pct": 100 if self.is_ready else self._last_bootstrap_pct,
            "cloudflare_tunnel_enabled": self.cloudflare_tunnel_enabled,
            "wp_installed": self._wp_installed,
            "wordpress_up": self._wordpress_confirmed,
            "images_present": dict(self._images_present),
            "proxy_running": self.proxy_server is not None,
            "sleeping": self._sleeping,
            "onionheaven": self._onionheaven_overview,
            "pending_question": self._pending_question,
            "onionname_collision": self._onionname_collision,
            "settings_page_error": self._settings_page_error,
            "wedge_warning": self._wedge_warning_fired,
            "probe": {"code": self._last_probe_code, "why": self._last_probe_why,
                      "ms": self._last_probe_ms,
                      "reachable": self._last_reachability[0]},
        }

    def publish_state(self):
        """Rewrite daemon-state.json (where the menubar's update_menu was)."""
        try:
            with self._lock:
                write_json_atomic(state_path(self.app_support), self.state_document())
        except Exception as e:
            self.log(f"daemon: could not write the state file: {e}")

    # The menubar's update_menu() calls, kept by name so the moved code
    # reads as it did.
    update_menu = publish_state


    # -- run -----------------------------------------------------------------

    def run(self):
        """Blocks until TERM. The main thread serves IOKit's sleep/wake
        callbacks (a CFRunLoop, run a second at a time so Python signal
        handlers still run) and the threads do the work."""
        self._write_pid()
        signal.signal(signal.SIGTERM, self._on_term)
        signal.signal(signal.SIGINT, self._on_term)
        signal.signal(signal.SIGUSR1, self._on_usr1)
        signal.signal(signal.SIGUSR2, self._on_usr2)
        self._pending_offline = False
        self._pending_online = False

        self.log("=" * 60)
        self.log(f"=== OnionPress daemon starting (v{self.version}, pid {os.getpid()}) ===")
        self.log("=" * 60)
        self._log_version_info()
        self._setup_environment()
        self._resolve_ports_at_start()
        self.publish_state()

        self._power = None
        self._register_power_events()

        threading.Thread(target=self._status_loop, daemon=True, name="status").start()
        self.start_thumbnail_generator()

        try:
            while not self._stop.is_set():
                self._main_loop_once(1.0)
                if self._pending_offline:
                    self._pending_offline = False
                    self._offline_now()
                if self._pending_online:
                    self._pending_online = False
                    self.handle_wake()
        finally:
            self._shutdown()

    def _main_loop_once(self, seconds):
        """One turn of the main run loop: IOKit's callbacks fire here."""
        try:
            from . import mac_power
            if self._power is not None and mac_power.run_loop_once(seconds):
                return
        except Exception:
            pass
        self._stop.wait(seconds)

    def _register_power_events(self):
        """Sleep: /offline and DEL_ONION inside IOKit's delay-inhibitor
        window (onionpress.mac_power), wake: USR2 to the watchdogs. Without
        IOKit (not a Mac) the clock-gap check in check_status is what
        notices a suspend."""
        try:
            from .mac_power import MacPowerHandler
            handler = MacPowerHandler(
                on_will_sleep=lambda: None if self._quitting else self.handle_sleep(),
                on_has_powered_on=lambda: None if self._quitting else self.handle_wake(),
                log=self.log)
            if handler.register():
                self._power = handler
                self.log("Registered for sleep via IOKit IORegisterForSystemPower (delay-inhibitor active)")
            else:
                self.log("WARNING: no IOKit sleep registration; sleep is noticed by the clock gap only")
        except Exception as e:
            self.log(f"WARNING: MacPowerHandler init failed: {e}")

    def _write_pid(self):
        try:
            with open(pid_path(self.app_support), "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
        except OSError as e:
            self.log(f"daemon: could not write the pid file: {e}")

    def _remove_pid(self):
        try:
            with open(pid_path(self.app_support), encoding="utf-8") as f:
                if int(f.read().strip()) == os.getpid():
                    os.remove(pid_path(self.app_support))
        except (OSError, ValueError):
            pass

    def _on_term(self, signum, frame):
        self.log(f"Received signal {signum}: stopping")
        self._stop.set()

    def _on_usr1(self, signum, frame):
        self._pending_offline = True

    def _on_usr2(self, signum, frame):
        self._pending_online = True

    def _shutdown(self):
        """TERM: the hub hears /offline while the tor container is still
        up (a `quit` stops the containers after the daemon; a plain
        restart by launchd or an update comes right back, so only when
        the stack is going down too: `daemon stop` writes
        .daemon-quitting), the proxy, the captures and caffeinate end,
        the pid file goes."""
        self._quitting = True
        # The next daemon waits for this one's WordPress port to free before
        # detecting its offset (a Colima forward can outlast the process).
        try:
            with open(self._marker(".previous-port-offset"), "w") as f:
                f.write(str(self.port_offset))
        except OSError:
            pass
        try:
            if (self.hub_client.registration_succeeded and not self.is_onionheaven
                    and self._take_marker(QUITTING_MARKER)):
                try:
                    self.hub_client.notify_offline()
                except Exception:
                    pass
        except Exception:
            pass
        self.stop_onion_proxy()
        self.stop_web_log_capture()
        self.stop_container_log_capture()
        self.caffeine.stop()
        self._remove_pid()
        self.log("OnionPress daemon stopped")

    def _offline_now(self):
        """USR1 (`onionpress daemon offline`, from the launcher's `stop`):
        tell the hub /offline now, as the menubar's Stop did before
        stopping the containers, and acknowledge."""
        try:
            if self.hub_client.registration_succeeded and not self.is_onionheaven:
                try:
                    ok = bool(self.hub_client.notify_offline())
                    self.log(f"daemon: /offline before stop {'landed' if ok else 'MISSED'}")
                except Exception as e:
                    self.log(f"daemon: /offline before stop failed: {e}")
        finally:
            try:
                with open(self._marker(OFFLINE_ACK_MARKER), "w") as f:
                    f.write(str(os.getpid()))
            except OSError:
                pass

    # -- start-up ------------------------------------------------------------

    def _setup_environment(self):
        """What the menubar's __init__ exported for its children: the
        bundle's bin first on PATH, this install's Colima and Docker."""
        docker_config_dir = os.path.join(self.app_support, "docker-config")
        if self.bin_dir:
            os.environ["PATH"] = f"{self.bin_dir}:{os.environ.get('PATH', '')}"
        os.environ["COLIMA_HOME"] = self.colima_home
        os.environ["LIMA_HOME"] = os.path.join(self.colima_home, "_lima")
        # Colima honours COLIMA_HOME only when the directory exists (else it
        # uses ~/.colima, socket forward included): make it before the
        # first colima call (stop_stale_colima), on a fresh install too (#370).
        os.makedirs(os.environ["LIMA_HOME"], exist_ok=True)
        os.environ["LIMA_INSTANCE"] = "onionpress"
        os.environ["DOCKER_HOST"] = f"unix://{self.docker_socket}"
        os.environ["DOCKER_CONFIG"] = docker_config_dir
        try:
            os.makedirs(docker_config_dir, exist_ok=True)
            docker_config_file = os.path.join(docker_config_dir, "config.json")
            if not os.path.exists(docker_config_file):
                with open(docker_config_file, "w") as f:
                    f.write('{\n\t"auths": {},\n\t"currentContext": "colima"\n}\n')
            cli_plugins_dir = os.path.join(docker_config_dir, "cli-plugins")
            os.makedirs(cli_plugins_dir, exist_ok=True)
            compose_plugin_dest = os.path.join(cli_plugins_dir, "docker-compose")
            bundled_compose = os.path.join(self.bin_dir, "docker-compose") if self.bin_dir else ""
            system_compose = os.path.expanduser("~/.docker/cli-plugins/docker-compose")
            if bundled_compose and os.path.isfile(bundled_compose) and not os.path.exists(compose_plugin_dest):
                os.symlink(bundled_compose, compose_plugin_dest)
            elif os.path.islink(system_compose) and not os.path.exists(compose_plugin_dest):
                os.symlink(system_compose, compose_plugin_dest)
        except OSError:
            pass

    def _resolve_ports_at_start(self):
        """Multi-user port offset (CLAUDE.md): stop an orphaned Colima of a
        previous crash, wait for our previous instance's port to free
        (.previous-port-offset), then read the running stack's published
        port or allocate by bind-probing. The result goes into the
        environment (the launcher honours ONIONPRESS_PORT_OFFSET) and the
        state file (the menubar reads its ports from there)."""
        colima_bin = os.path.join(self.bin_dir, "colima") if self.bin_dir else "colima"
        try:
            op_config.stop_stale_colima(colima_bin, self.colima_home,
                                        pid_path(self.app_support), log_func=self.log)
        except Exception as e:
            self.log(f"daemon: stale Colima check failed: {e}")
        prev_offset_sentinel = self._marker(".previous-port-offset")
        try:
            if os.path.exists(prev_offset_sentinel):
                if time.time() - os.path.getmtime(prev_offset_sentinel) < 60:
                    with open(prev_offset_sentinel) as f:
                        prev_offset = int(f.read().strip())
                    desired_port = 8080 + prev_offset
                    import socket
                    for _ in range(30):
                        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        try:
                            s.bind(("127.0.0.1", desired_port))
                            s.close()
                            break
                        except OSError:
                            s.close()
                            time.sleep(0.5)
                os.remove(prev_offset_sentinel)
        except (OSError, ValueError):
            pass
        try:
            pc = op_config.resolve_port_offset()
        except Exception as e:
            self.log(f"daemon: port detection failed ({e}); using the environment's offset")
            pc = op_config.PortConfig.from_offset(self.port_offset)
        self._apply_ports(pc)
        self.log(f"Ports: offset {self.port_offset} (WordPress {self.wp_port}, "
                 f"SOCKS {self.socks_port}, proxy {self.proxy_port})")

    def _apply_ports(self, pc):
        self.port_offset = pc.offset
        self.wp_port = pc.wp_port
        self.socks_port = pc.socks_port
        self.proxy_port = pc.proxy_port
        os.environ["ONIONPRESS_PORT_OFFSET"] = str(pc.offset)
        os.environ["ONIONPRESS_WP_PORT"] = str(self.wp_port)
        os.environ["ONIONPRESS_SOCKS_PORT"] = str(self.socks_port)
        os.environ["ONIONPRESS_PROXY_PORT"] = str(self.proxy_port)
        onion_proxy.PROXY_PORT = self.proxy_port
        onion_proxy.PHP_PROXY_PORT = self.wp_port

    def _resync_ports(self):
        """After the stack came up: the port Docker actually published,
        which can differ from the start-time snapshot (a stop/start that
        raced another process's allocation)."""
        running_port = launcher_ops.get_running_wp_port()
        if running_port is None or running_port == self.wp_port:
            return
        pc = op_config.PortConfig.from_offset(running_port - 8080)
        self.log(f"Port resync: was {self.wp_port}, running stack is on {running_port}")
        self._apply_ports(pc)

    def check_port_conflict(self):
        """Ports bound by something else while our containers are not up:
        [ports], reported in the state for the menubar's dialog."""
        import socket
        in_use = []
        for port in (self.wp_port, self.socks_port, self.proxy_port):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(1)
                s.bind(("127.0.0.1", port))
                s.close()
            except OSError:
                in_use.append(port)
        return in_use

    def _log_version_info(self):
        try:
            self.log(f"Version: {self.version}")
            self.log(f"Bundle: {self.paths.app_bundle or '(none)'}")
            self.log(f"Launcher: {self.launcher_script or '(none)'}")
            self.log(f"Python: {sys.version.split()[0]} ({sys.executable})")
            self.log(f"Data dir: {self.app_support}")
            if self._had_cached_address:
                self.log(f"Onion address: {self.onion_address}")
            if os.environ.get("ONIONPRESS_DAEMON_LAUNCHD"):
                self.log("Running under launchd (" + AGENT_LABEL + ")")
        except Exception:
            pass

    # -- the status loop (the menubar's start_status_checker) ----------------

    def _status_loop(self):
        while not self._stop.is_set():
            if self._sleeping:
                self._stop.wait(30)
                continue
            try:
                self.check_status()
            except Exception as e:
                self.log(f"ERROR in the status loop: {e}")
            self._stop.wait(poll_interval(self.display_state))

    def _runtime_ready(self):
        """Docker answers (the launcher may still be initializing Colima)."""
        if not os.path.exists(os.path.join(self.colima_home, ".initialized")):
            return False
        r = self._docker.run(["info"], timeout=5, quiet=True)
        return r.ok

    def _containers(self):
        """[(name, state)] of this install's containers, from `docker ps
        -a` by compose project label: cheaper than the launcher's `status`
        (bash + compose + jq per tick, the way the menubar did it)."""
        r = self._docker.run(
            ["ps", "-a", "--filter", "label=com.docker.compose.project",
             "--format", "{{.Names}}\t{{.State}}\t{{.Label \"com.docker.compose.project\"}}"],
            timeout=10, quiet=True)
        if not r.ok:
            return None
        out = []
        for line in r.output.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and (parts[0].startswith("onionpress-") or parts[0] in (
                    "onionheaven", "onionheaven-farm", "onionpress-autoheal",
                    "onionpress-autoheal-proxy") or parts[0].startswith("onionheaven-")):
                if parts[0].startswith("onionheaven-takeover") or parts[0].startswith("onionheaven-poll"):
                    continue        # workers come and go; not the stack's health
                out.append((parts[0], parts[1]))
        return out

    def _check_images_present(self):
        """Which of the stack's images are here (the first-run setup
        window's "Downloading images" step reads it from the state)."""
        r = self._docker.run(["images", "--format", "{{.Repository}}"], timeout=5, quiet=True)
        if not r.ok:
            return
        repos = r.output.split()
        for name in self._images_present:
            if not self._images_present[name] and any(name in repo for repo in repos):
                self._images_present[name] = True
                self.log(f"Image downloaded: {name}")

    def _read_served_address(self):
        r = self._docker.run(["exec", "onionpress-tor", "cat", wp_files.TOR_HOSTNAME_FILE],
                             timeout=10, quiet=True)
        addr = r.output.strip() if r.ok else ""
        return addr if key_manager.is_valid_onion_address(addr) else ""

    def check_status(self):
        """One pass: containers, address, internet, WordPress, the dual
        probe and the republish trigger, the hub's loops, the captures,
        caffeinate, the proxy, status.json and the Settings page's
        requests. The menubar's check_status, with its menu updates
        turned into state writes."""
        if self._quitting:
            return
        with self._checking_lock:
            if self.checking:
                return
            self.checking = True
        try:
            if (not self._wedge_warning_fired and self._yellow_since
                    and (time.time() - self._yellow_since) > 600):
                self.log(
                    "WEDGE WARNING: onion service unreachable for 10+ min. "
                    "Automatic recovery: see the `probe verdict=` and "
                    "`descriptor republish` lines (REPUBLISH_ON_UNREACHABLE="
                    f"{'yes' if self._recovery.republish_enabled() else 'no'}).")
                self._wedge_warning_fired = True

            if not self.runtime_ready:
                self.runtime_ready = self._runtime_ready()
                if self.runtime_ready:
                    self.log("Container runtime is ready")

            # The Share Now button / `onionpress upload-analytics`
            trigger_found = self._take_marker(UPLOAD_TRIGGER_MARKER)
            if not trigger_found and self.is_running:
                r = self._docker.exec(
                    "onionpress-wordpress",
                    ["sh", "-c", "test -f /var/lib/onionpress/.upload-analytics && rm /var/lib/onionpress/.upload-analytics"],
                    timeout=5, quiet=True, user=wp_files.WP_USER)
                if r.ok:
                    trigger_found = True
            if trigger_found:
                self.log("Upload-analytics trigger detected")
                threading.Thread(target=self._manual_analytics_upload, daemon=True).start()

            # A new onionname after a collision
            if self._take_marker(ONIONNAME_RETRY_MARKER):
                self._onionname_retry_giveup = False
                self._onionname_collision = None

            # Settings changed by `onionpress config set` (the CLI leaves
            # the keys in .config-changed)
            self.apply_config_changes(op_config.take_config_changes(self.app_support))

            if self.runtime_ready and not all(self._images_present.values()):
                self._check_images_present()

            containers = self._containers() if self.runtime_ready else None
            not_running = []
            if containers:
                self.is_running = all(state.lower() == "running" for _, state in containers)
                not_running = sorted(f"{name} ({state})" for name, state in containers
                                     if state.lower() != "running")
            else:
                self.is_running = False
            if not_running != self._last_not_running:
                if not_running:
                    self.log("Containers not running: " + ", ".join(not_running))
                self._last_not_running = not_running

            was_running = self._last_pass_running
            self._last_pass_running = self.is_running
            if self.is_running and not was_running:
                # The stack just came up (a start or restart): the port it
                # really published, and onionheaven's HSFETCH timer armed
                # for the cold start (tor-watchdog arms its own).
                self._resync_ports()
                if self._signal_watchdog("onionheaven", "USR2"):
                    self.log("Sent USR2 (start) to onionheaven watchdog")

            if self.is_running:
                self._probe_vm_wedge()

            if self.is_running:
                addr = self._read_served_address()
                if addr:
                    self.onion_address = addr
                    self.is_onionheaven = onionheaven.is_onionheaven_instance(addr)
                    try:
                        with open(self._marker("onion_address"), "w") as f:
                            f.write(addr)
                    except OSError:
                        pass
                else:
                    self.onion_address = "Generating address..."

                had_internet = self._has_internet
                self._has_internet = self.check_internet_connectivity()
                if not self._has_internet and had_internet:
                    self.log("Internet connectivity lost")
                elif self._has_internet and not had_internet:
                    self.log("Internet connectivity restored")
                    self.publish_state()

                if not self._has_internet:
                    if self.is_ready:
                        self.log("Going offline — no internet connection")
                    self.is_ready = False
                    self._last_reachability = (None, None)
                    self._recovery.note_offline()
                    if self._yellow_since is None:
                        self._yellow_since = time.time()
                else:
                    self._full_health_pass()

                if self.web_log_process is None:
                    threading.Thread(target=self.start_web_log_capture, daemon=True).start()
                if not self._container_log_processes:
                    threading.Thread(target=self.start_container_log_capture, daemon=True).start()
                if not self._clearnet_capture_started:
                    self._clearnet_capture_started = True
                    threading.Thread(target=self.start_clearnet_log_capture, daemon=True).start()

                self.caffeine.start()

                if self.proxy_server is None:
                    self.start_onion_proxy()
                else:
                    self.proxy_server.onion_address = self.onion_address
                    self.proxy_server.healthcheck_address = self.healthcheck_address
                    self.proxy_server.tor_ready = self.is_ready

                if self.healthcheck_address is None and self._tor_internally_ready:
                    self.read_healthcheck_address()

                if self.is_ready and self._pending_manual_upload:
                    self._pending_manual_upload = False
                    self.log("Analytics upload: running queued Share Now request")
                    threading.Thread(target=self._manual_analytics_upload, daemon=True).start()

                self.write_status_to_volume()
                self.poll_settings_page()
                self._hub_loops()
                self._check_wordpress_installed()
            else:
                if was_running or self.is_ready:
                    self.log("Service stopped")
                    self.last_status_logged = None
                if not self.onion_address or self.onion_address in ("Starting...", "Generating address..."):
                    self.onion_address = "Not running"
                self.is_ready = False
                self._last_reachability = (None, None)
                self._recovery.note_offline()
                self._wp_installed = None
                self._was_ready = False
                self._last_bootstrap_pct = 0
                self._bootstrap_stall_count = 0
                self._yellow_since = None
                self._consecutive_fail_count = 0
                self._wedge_warning_fired = False
                self.healthcheck_address = None
                self._onionheaven_checked = False
                self.hub_client.reset_session()
                self._tor_internally_ready = False
                if self.web_log_process is not None:
                    self.stop_web_log_capture()
                    self.stop_container_log_capture()
                self.caffeine.stop()
                # The extension's proxy serves only while the stack runs (it
                # would otherwise hold its own port against the conflict
                # check below and answer for a stopped site).
                self.stop_onion_proxy()
                if self.runtime_ready:
                    self.port_conflict = self.check_port_conflict()
                    if self.port_conflict and not self._port_conflict_logged:
                        self.log("Port conflict: ports "
                                 f"{', '.join(map(str, self.port_conflict))} already in use "
                                 "by another process")
                    self._port_conflict_logged = bool(self.port_conflict)
            if self.is_running:
                self.port_conflict = []
                self._port_conflict_logged = False

            self._maybe_emit_snapshot()
        except Exception as e:
            self.log(f"ERROR in check_status: {e}")
            import traceback
            self.log(traceback.format_exc())
        finally:
            self._last_check_complete_ts = time.time()
            self.checking = False
            self.publish_state()

    _last_pass_running = False
    _port_conflict_logged = False

    def _full_health_pass(self):
        """Internet is there: WordPress, then the dual probe and the #266
        recovery step (RecoveryWindow, shared with Linux)."""
        current_status = (self.is_running, self.onion_address)
        should_log = (current_status != self.last_status_logged) or not self.is_ready
        if not self._wordpress_confirmed:
            wordpress_ready = self._health_checker.check_wordpress_external(self.wp_port, log=should_log)
            if wordpress_ready:
                self._wordpress_confirmed = True
        else:
            wordpress_ready = True
        tor_reachable = self.check_tor_reachability(log_result=should_log)
        self._reachability_stats.record_probe(tor_reachable, self._last_probe_code, self._last_probe_ms)
        self._recovery.step(self._tor_internally_ready, tor_reachable,
                            self._last_probe_code, self._last_probe_why)

        previous_ready = self.is_ready
        ready_now = wordpress_ready and tor_reachable
        if ready_now and not previous_ready:
            _now = time.time()
            if self._reachability_stats.current_yellow_start_ts is not None:
                _ydur = int(_now - self._reachability_stats.current_yellow_start_ts)
                self._reachability_stats.exit_yellow(_now)
                self.log(f"reachability: yellow → purple (http={self._last_probe_code}, "
                         f"{self._last_probe_ms}ms, yellow lasted {_ydur}s)")
            elif not self._was_ready:
                self._reachability_stats.reset_session(_now)
            self.is_ready = True
            self._was_ready = True
            self.hub_client.reset_reclaim()
            self._bootstrap_stall_count = 0
            self._yellow_since = None
            self._consecutive_fail_count = 0
            self._wedge_warning_fired = False
            elapsed = int(time.time() - self.startup_time)
            self.log(f"✓ System fully operational (launched in {elapsed}s)")
            self.last_status_logged = current_status
            try:
                setup_marker = self._marker(".setup_complete")
                if not os.path.exists(setup_marker):
                    with open(setup_marker, "w") as f:
                        f.write("1")
            except OSError:
                pass
            self.cloudflare_tunnel_enabled = bool(self.read_config_value("CLOUDFLARE_TUNNEL_TOKEN"))
            if not self._s3_keys_reconcile_started:
                self._s3_keys_reconcile_started = True
                threading.Thread(target=self._reconcile_archive_s3_keys, daemon=True).start()
            self.publish_state()
        elif ready_now:
            self.is_ready = True
            self._bootstrap_stall_count = 0
            self._yellow_since = None
            self._consecutive_fail_count = 0
            self._wedge_warning_fired = False
            self.last_status_logged = current_status
        elif previous_ready and not ready_now:
            if self._stopping or self._quitting:
                self.is_ready = False
                self._consecutive_fail_count = 0
            else:
                self._consecutive_fail_count += 1
                if self._consecutive_fail_count >= 2:
                    self.is_ready = False
                    _trip_ts = time.time()
                    self._yellow_since = _trip_ts
                    self._bootstrap_stall_count = 0
                    self.hub_client.heartbeat_succeeded = False
                    self.startup_time = _trip_ts
                    self._reachability_stats.enter_yellow(_trip_ts)
                    self.log(f"reachability: purple → yellow (http={self._last_probe_code}, "
                             f"{self._last_probe_ms}ms, after 2 fails)")
        else:
            pct = self._parse_bootstrap_percentage()
            if pct > self._last_bootstrap_pct:
                self._last_bootstrap_pct = pct
                self._bootstrap_stall_count = 0
            else:
                self._bootstrap_stall_count += 1
            if self._yellow_since is None:
                self._yellow_since = time.time()

    def _hub_loops(self):
        """OnionHeaven: hub mode once, the heartbeat once Tor is
        bootstrapped, the opt-out and upkeep, analytics, the onionname
        retry (the menubar's second half of check_status)."""
        if self.is_ready and not self._onionheaven_checked:
            self._onionheaven_checked = True
            if self.is_onionheaven:
                self.log("OnionHeaven mode activated (heartbeat monitor runs in onionheaven container)")
            if onionheaven.should_auto_prevent_sleep(self.onion_address):
                sleep_marker = self._marker(".onionheaven_sleep_set")
                if not os.path.exists(sleep_marker):
                    self.write_config_value("PREVENT_SLEEP", "never")
                    try:
                        with open(sleep_marker, "w") as f:
                            f.write("1")
                    except OSError:
                        pass
                    self.log("Auto-set PREVENT_SLEEP=never for always-awake machine (first detection)")
                self.caffeine.stop()
                self.caffeine.start()

        tor_bootstrapped = self._last_bootstrap_pct >= 100 or self.is_ready
        # The heartbeat on "yes" (or unset), the opt-out's /unregister on
        # "no", and the record's upkeep: the client's, re-read every cycle.
        self.hub_client.tick(tor_bootstrapped, self._address_known())

        if tor_bootstrapped and self._address_known() and not self._analytics_sharing_started:
            self._analytics_sharing_started = True
            from . import analytics_sharing
            analytics_sharing.start_analytics_sharing(self)

        if (tor_bootstrapped and self._address_known() and not self.is_onionheaven
                and not self._onionname_retry_in_flight and not self._onionname_retry_giveup):
            pending = self.read_config_value("ONIONNAME", "")
            registered = self.read_config_value("ONIONNAME_REGISTERED", "no") == "yes"
            if pending and not registered:
                self._onionname_retry_in_flight = True
                threading.Thread(target=self._retry_pending_onionname, daemon=True,
                                 name="onionname-retry").start()

    def _check_wordpress_installed(self):
        """First run: once WordPress is installed, start Tor (the launcher's
        start-tor)."""
        if self._wp_installed is True or self.proxy_server is None:
            return
        wp_installed = self.check_wp_installed()
        if wp_installed:
            was_waiting = self._wp_installed is False
            self._wp_installed = True
            if was_waiting:
                self.log("Setup complete — starting Tor")
                threading.Thread(target=lambda: self.run_launcher(["start-tor"], timeout=600),
                                 daemon=True).start()
        elif wp_installed is False:
            self._wp_installed = False

    def check_wp_installed(self):
        try:
            r = self._docker.run(wp_files.wp_cli_args("core", "is-installed"), timeout=10, quiet=True)
            return r.ok
        except Exception:
            return None

    # -- the probe (the menubar's check_tor_reachability) ----------------

    def check_tor_reachability(self, log_result=True):
        self._tor_internally_ready = False
        self._last_probe_code = ""
        self._last_probe_ms = 0
        self._last_probe_why = ""
        self._last_reachability = (None, None)
        if not self._address_known():
            self._last_probe_code = "no_address"
            return False
        try:
            if log_result:
                self.log(f"Checking Tor onion service status for: {self.onion_address}")
            hostname = self._health_checker.check_tor_hostname(self.onion_address)
            if not hostname:
                if log_result:
                    self.log("✗ Onion service hostname file not found")
                self._last_probe_code = "hostname_missing"
                return False
            if hostname != self.onion_address:
                if log_result:
                    self.log(f"✗ Hostname mismatch: {hostname} != {self.onion_address}")
                self._last_probe_code = "hostname_mismatch"
                return False
            bootstrapped, pct = self._health_checker.check_tor_bootstrap()
            if not bootstrapped:
                if log_result:
                    self.log(f"✗ onionpress-tor not fully bootstrapped yet ({pct}%)")
                self._last_probe_code = f"bootstrap_{pct}"
                return False
            if not self._health_checker.check_internal_connectivity():
                if log_result:
                    self.log("✗ WordPress not reachable from Tor container")
                self._last_probe_code = "internal_wp"
                return False
            self._tor_internally_ready = True
            _probe_start = time.monotonic()
            reachable, http_code = self._health_checker.check_external_reachability(self.onion_address)
            self._last_probe_ms = int((time.monotonic() - _probe_start) * 1000)
            self._last_probe_code = http_code or ""
            self._last_probe_why = (
                "" if reachable else
                str(getattr(self._health_checker, "last_probe", {}).get("why") or ""))
            self._last_reachability = (reachable, http_code or None)
            if not reachable:
                if log_result:
                    if http_code == "takeover":
                        self.log("✗ Onion service flagged OnionHeaven takeover (X-OnionHeaven-Takeover header set)")
                    elif http_code == "302":
                        self.log("✗ Onion service returning 302 (OnionHeaven takeover active)")
                    elif http_code.startswith("000"):
                        self.log(f"✗ Our onion service not yet reachable through Tor network ({decode_curl_reason(http_code)})")
                    else:
                        self.log(f"✗ Onion service returned HTTP {http_code}")
                return False
            if log_result:
                self.log(f"✓ Onion service verified: {self.onion_address}")
            return True
        except Exception as e:
            if log_result:
                self.log(f"✗ Tor status check failed: {e}")
            if not self._last_probe_code:
                self._last_probe_code = "exception"
            return False

    def _parse_bootstrap_percentage(self):
        try:
            _bootstrapped, pct = self._health_checker.check_tor_bootstrap()
            return pct
        except Exception:
            return 0

    def _probe_vm_wedge(self):
        """One WARN per wedge episode, probed every 5 minutes."""
        now = time.time()
        if now < self._wedge_probe_next:
            return
        self._wedge_probe_next = now + 300
        signals = self._health_checker.check_vm_wedge()
        if signals is None:
            return
        tier = None
        if signals.loadavg_1min is not None and signals.loadavg_1min >= WEDGE_LOAD_ALARM:
            tier = "alarm"
        elif signals.loadavg_1min is not None and signals.loadavg_1min >= WEDGE_LOAD_WARN:
            tier = "warn"
        elif (signals.wp_health_status == "unhealthy"
              and signals.wp_failing_streak is not None
              and signals.wp_failing_streak >= WEDGE_FAILING_STREAK_ALARM):
            tier = "alarm"
        if tier is None:
            self._wedge_last_episode = None
            return
        episode = (tier, signals.wp_health_status)
        if episode == self._wedge_last_episode:
            return
        self._wedge_last_episode = episode
        load_str = f"{signals.loadavg_1min:.2f}" if signals.loadavg_1min is not None else "?"
        self.log(f"WEDGE {tier}: loadavg1={load_str} wp_health={signals.wp_health_status} "
                 f"wp_failing_streak={signals.wp_failing_streak}")

    def _signal_watchdog(self, container, sig):
        return launcher_ops.signal_watchdog(self._docker, container, sig)

    def _maybe_emit_snapshot(self, force=False, fast=False):
        now = time.time()
        if not force and now - self._last_snapshot_ts < self._snapshot_interval_seconds:
            return
        try:
            host = host_metrics()
            ctn = None
            if not fast and self._docker is not None:
                ctn = container_metrics(self._docker)
            self.log(self._reachability_stats.format_snapshot(host, ctn, now))
        except Exception as e:
            self.log(f"snapshot emit failed: {e}")
        finally:
            self._last_snapshot_ts = now

    # -- sleep / wake (the menubar's handle_sleep / handle_wake) ----------

    def _record_suspend_offline(self, ok, elapsed_ms):
        self.log(f"SUSPEND-RACE: /offline {'landed' if ok else 'MISSED'} in {elapsed_ms}ms")

    def handle_sleep(self):
        """Inside IOKit's delay-inhibitor: snapshot, USR1 to the watchdogs
        (DEL_ONION), /offline to the hub (5 s), caffeinate off."""
        self.log("System going to sleep")
        self._maybe_emit_snapshot(force=True, fast=True)
        self._sleeping = True
        self.hub_client.heartbeat_succeeded = False
        if not self.is_onionheaven:
            for container in ("onionpress-tor", "onionheaven"):
                if self._signal_watchdog(container, "USR1"):
                    self.log(f"Sent USR1 (sleep) to {container} watchdog")
                else:
                    self.log(f"Failed to send USR1 to {container} watchdog")
            if self.is_ready and self.hub_client.registration_succeeded:
                t0 = time.monotonic()
                ok = False
                try:
                    ok = bool(self.hub_client.notify_offline(max_time=5))
                except Exception:
                    ok = False
                self._record_suspend_offline(ok, int((time.monotonic() - t0) * 1000))
            self.caffeine.stop()
        self.publish_state()

    def handle_wake(self):
        """Back from sleep: snapshot and fresh counters, the republish
        window reset (a pre-sleep failure must not look like 10 min of
        unreachability), the heartbeat loop superseded, USR2 to the
        watchdogs (ADD_ONION)."""
        self.log("System wake detected — marking Tor as reconnecting")
        self._maybe_emit_snapshot(force=True)
        self._reachability_stats.reset_session(time.time())
        self._recovery.note_wake()
        self._sleeping = False
        self.startup_time = time.time()
        self.caffeine.start()
        self._onionheaven_checked = False
        self.hub_client.wake()
        self._wordpress_confirmed = False
        if self.is_ready:
            self.is_ready = False
            self._last_bootstrap_pct = 0
            self._bootstrap_stall_count = 0
            self._yellow_since = time.time()
        for container in ("onionpress-tor", "onionheaven"):
            if self._signal_watchdog(container, "USR2"):
                self.log(f"Sent USR2 (wake) to {container} watchdog")
            else:
                self.log(f"Failed to send USR2 to {container} watchdog")
        self.publish_state()

    # -- the hub's messages ---------------------------------------------

    def read_healthcheck_address(self):
        try:
            hc_file = self._marker("healthcheck-address")
            if os.path.exists(hc_file):
                with open(hc_file) as f:
                    addr = f.read().strip()
                if key_manager.is_valid_onion_address(addr):
                    self.healthcheck_address = addr
                    self.log(f"Healthcheck address: {addr}")
                    return
            r = self._docker.run(["exec", "onionpress-tor", "cat",
                                  "/var/lib/tor/hidden_service/healthcheck/hostname"],
                                 timeout=10, quiet=True)
            if r.ok:
                addr = r.output.strip()
                if key_manager.is_valid_onion_address(addr):
                    self.healthcheck_address = addr
                    try:
                        with open(hc_file, "w") as f:
                            f.write(addr)
                    except OSError:
                        pass
                    self.log(f"Healthcheck address: {addr}")
        except Exception as e:
            self.log(f"Failed to read healthcheck address: {e}")

    # -- analytics -------------------------------------------------------

    def _manual_analytics_upload(self):
        from . import analytics_sharing
        if not self.check_internet_connectivity() or not self.is_ready:
            self._pending_manual_upload = True
            self.log("Analytics upload queued — will run when online")
            return
        try:
            result = analytics_sharing.upload_cycle(self, include_active=True) or {}
            status = result.get("status", "unknown")
            if status == "ok":
                w, u = result.get("wanted", 0), result.get("uploaded", 0)
                self.log(f"Analytics upload {'complete' if u == w else 'partial'}: {u}/{w} file(s)")
            elif status == "manifest_failed":
                self.log("Analytics upload failed: could not reach OnionHome "
                         "(Tor circuit not ready or hub unreachable)")
            elif status == "none_wanted":
                self.log("Analytics upload: OnionHome already has everything")
            elif status == "no_files":
                self.log("Analytics upload: no logs to share yet")
            elif status == "sign_error":
                self.log("Analytics upload failed: manifest signing error")
            elif status == "no_onion":
                self.log("Analytics upload failed: onion address not yet available")
            else:
                self.log(f"Analytics upload finished (status={status})")
        except Exception as e:
            self.log(f"Analytics upload error: {e}")

    def _reconcile_archive_s3_keys(self):
        """Once per run when the site is up: archive.org keys for the
        Wayback sweep (`onionpress ensure-archive-s3-keys`)."""
        try:
            rc, out = self.run_launcher(["ensure-archive-s3-keys"], timeout=300)
            if rc is None:
                return
            if rc != 0:
                self.log(f"archive.org keys: ensure-archive-s3-keys exited {rc}")
            elif out:
                self.log(f"archive.org keys: {out.splitlines()[-1][:200]}")
        except Exception as e:
            self.log(f"archive.org keys: {e}")

    # -- settings ----------------------------------------------------------

    def write_config_value(self, key, value):
        """`onionpress config set` through the launcher (the one writer of
        the config file); the marker it leaves is taken at the next tick."""
        rc, out = self.run_launcher(["config", "set", key, value, "--json"], timeout=120)
        if rc != 0:
            self.log(f"config set {key} refused (rc {rc}): {out[-200:] if out else ''}")
            return False
        self.apply_config_changes(op_config.take_config_changes(self.app_support))
        return True

    def apply_config_changes(self, keys):
        """After `onionpress config set` (by anyone: the marker): caffeinate
        follows PREVENT_SLEEP, an analytics upload starts when sharing was
        turned on, the tunnel flag, status.json."""
        if not keys:
            return
        self.log(f"Settings changed: {', '.join(keys)}")
        try:
            if "PREVENT_SLEEP" in keys:
                self.caffeine.stop()
                self.caffeine.start()
            if ("SHARE_ANALYTICS_WITH_ONIONHOME" in keys
                    and self.read_config_value("SHARE_ANALYTICS_WITH_ONIONHOME") == "yes"):
                from .analytics_sharing import trigger_upload
                trigger_upload()
            if "CLOUDFLARE_TUNNEL_TOKEN" in keys:
                self.cloudflare_tunnel_enabled = bool(self.read_config_value("CLOUDFLARE_TUNNEL_TOKEN"))
            if "ONIONHEAVEN_ADDRESS" in keys:
                onionheaven.ONIONHEAVEN_ADDRESS = onionheaven.configured_hub_address(
                    self.read_config_value("ONIONHEAVEN_ADDRESS", ""), self.log)
            if self.is_running:
                self.write_status_to_volume()
            self.publish_state()
        except Exception as e:
            self.log(f"Applying settings change: {e}")

    # -- status.json (the menubar's write_status_to_volume) ----------------

    def write_status_to_volume(self):
        """status.json, version, config-current.json and recent-logs.txt in
        the shared volume, written as www-data (wp_files), for the
        WordPress pages."""
        try:
            state = display_state_of(self._state_fields(), time.time())
            state = {"available": "running", "stalled": "starting"}.get(state, state)
            containers = {}
            r = self._docker.run(["ps", "--format", "{{.Names}}\t{{.State}}", "--filter", "name=onionpress"],
                                 timeout=5, quiet=True)
            if r.ok:
                for line in r.output.splitlines():
                    parts = line.split("\t", 1)
                    if len(parts) == 2:
                        containers[parts[0]] = parts[1]
            uptime_seconds = int(time.time() - self.startup_time) if self.is_running else 0
            bootstrap_pct = 100 if self.is_ready else self._last_bootstrap_pct
            onion_reachable, onion_http_code = self._last_reachability
            # Both OnionHeaven roles, for the OnionHeaven screen (the Mac
            # dialog reads it from the state file, the WordPress page from
            # status.json): the registry is asked only on a hub.
            try:
                oh_overview = onionheaven.overview(self)
            except Exception as e:
                self.log(f"OnionHeaven: overview error: {e}")
                oh_overview = onionheaven.overview(self, hub=False)
            self._onionheaven_overview = oh_overview
            # status.json's server_active stays the Mac's rule: the node
            # serving the hub's address (tests/test_linux_hub_mode.py).
            oh_server_active = bool(self.is_onionheaven)
            oh_stats = onionheaven.status_block(oh_overview, server_active=oh_server_active)
            onion_addr = self.onion_address if key_manager.is_valid_onion_address(self.onion_address) else ""
            load_avg = list(os.getloadavg())
            host_uptime = int(time.time() - self._host_boot_time())
            safe_config = op_config.redact_config(op_config.read_config(self.config_file))
            status = {
                "state": state,
                "version": self.version,
                "onion_address": onion_addr,
                "onionname": self.read_config_value("ONIONNAME", ""),
                "tor_impl": "tor",
                "uptime_seconds": uptime_seconds,
                "bootstrap_pct": bootstrap_pct,
                "onion_reachable": onion_reachable,
                "onion_http_code": onion_http_code,
                "containers": containers,
                "updated_at": _utc_now(),
                "platform": "macos",
                "load_avg": load_avg,
                "host_uptime_seconds": host_uptime,
                "onionheaven": oh_stats,
                "config": safe_config,
            }
            self._last_status_payload = status
            status_json = json.dumps(redact.status_for_wordpress(status), indent=2)
            self._docker.run(wp_files.put_file_args("/var/lib/onionpress/status.json"),
                             input=status_json, timeout=5, quiet=True)
            self._docker.run(wp_files.put_file_args("/var/lib/onionpress/version"),
                             input=self.version, timeout=5, quiet=True)
            self._docker.run(wp_files.put_file_args("/var/lib/onionpress/config-current.json"),
                             input=json.dumps(safe_config, indent=2), timeout=5, quiet=True)
            log_file = os.path.join(self.app_support, "onionpress.log")
            try:
                recent = redact.wordpress_log_excerpt(log_file, self.app_support)
                self._docker.run(wp_files.put_file_args("/var/lib/onionpress/recent-logs.txt"),
                                 input=recent, timeout=5, quiet=True)
            except (OSError, IOError):
                pass
        except Exception:
            pass

    _boot_time = None

    def _host_boot_time(self):
        if self._boot_time is None:
            try:
                r = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True,
                                   text=True, timeout=2)
                import re
                m = re.search(r"sec\s*=\s*(\d+)", r.stdout)
                self._boot_time = int(m.group(1)) if m else time.time()
            except Exception:
                self._boot_time = time.time()
        return self._boot_time

    # -- the Settings page's requests (handle-action) ---------------------

    def poll_settings_page(self):
        """`onionpress handle-action --json` once per tick, one run at a
        time (a restore or a start can take minutes). A question it stops
        at goes into the state for the menubar to ask."""
        with self._settings_page_lock:
            if self._settings_page_busy:
                return
            self._settings_page_busy = True
        threading.Thread(target=self._settings_page_run, daemon=True).start()

    def _settings_page_run(self):
        try:
            result = self._handle_action([])
            if result is None:
                return
            if result.get("confirm") or result.get("pending"):
                question = dict(result)
                question["confirm"] = question.get("confirm") or question.get("pending")
                if question != self._pending_question:
                    self._pending_question = question
                    self.log(f"Settings page: {question.get('action')} waits for the person "
                             f"at this Mac ({question['confirm']})")
                    self.publish_state()
                return
            if self._pending_question is not None:
                self._pending_question = None
                self.publish_state()
            if result.get("handled") or result.get("config"):
                self.apply_config_changes(op_config.take_config_changes(self.app_support))
                self.write_status_to_volume()
        except Exception as e:
            self.log(f"Settings page: {e}")
        finally:
            self._settings_page_busy = False

    def _handle_action(self, args):
        """One `onionpress handle-action --json` run; its last-line JSON, or
        None when it could not be run or answered (said in the log and
        the state, once an hour)."""
        rc, out = self.run_launcher(["handle-action", "--json", *args], timeout=None)
        if rc is None:
            self._settings_page_failed("could not run onionpress handle-action")
            return None
        result = op_confirm.last_json_line(out or "")
        if rc != 0 or result is None:
            self._settings_page_failed(f"onionpress handle-action failed (rc {rc}): {(out or '')[-400:]}")
            return None
        if self._settings_page_error:
            self._settings_page_error = ""
        return result

    def _settings_page_failed(self, message):
        self.log(f"Settings page: {message}")
        now = time.time()
        if now - self._settings_page_failed_at >= 3600:
            self._settings_page_failed_at = now
            self._settings_page_error = message
            self.publish_state()

    # -- onionname retry (the menubar's _retry_pending_onionname) --------

    def _save_onionname(self, name, onionaddress, registered):
        self.write_config_value("ONIONNAME", name)
        self.write_config_value("ONIONNAME_ADDRESS", onionaddress or "")
        self.write_config_value("ONIONNAME_REGISTERED", "yes" if registered else "no")

    def _retry_pending_onionname(self):
        """An onionname that did not register during setup: register it
        now that Tor is up. A collision is reported in the state
        (onionname_collision); the menubar asks for a new name, saves it
        with `config set ONIONNAME` and writes .onionname-retry."""
        try:
            name = self.read_config_value("ONIONNAME", "")
            if not name:
                return
            onionaddress = self._read_served_address()
            if not onionaddress:
                return
            try:
                from .onionnames_registrar import Registrar
            except Exception as e:
                self.log(f"onionname: retry cannot import registrar: {e}")
                return
            reg = Registrar(docker_bin=self._docker.docker_bin, log=self.log)
            result = reg.register(name, onionaddress)
            if result.ok:
                self._save_onionname(name, onionaddress, registered=True)
                self.log(f"onionname: retry succeeded for '{name}'")
                self._onionname_retry_giveup = True
                self._onionname_collision = None
            elif result.status == "collision":
                self.log(f"onionname: retry collision for '{name}' — asking for another name")
                self._onionname_collision = {"name": name,
                                             "suggestions": list(result.suggestions or [])}
                self._onionname_retry_giveup = True
            else:
                self.log(f"onionname: retry deferred ({result.status})")
        except Exception as e:
            self.log(f"onionname: retry thread error: {e}")
        finally:
            self._onionname_retry_in_flight = False
            self.publish_state()

    # -- the extension's proxy ------------------------------------------

    def start_onion_proxy(self):
        if self.proxy_server is not None or self._quitting:
            return
        docker_bin = self._docker.docker_bin
        docker_env = self._docker.env
        php_script = ""
        for candidate in (os.path.join(self.resources_dir, "MenubarApp", "Contents", "Resources", "onion-forward.php"),
                          os.path.join(self.resources_dir, "onion-forward.php")):
            if candidate and os.path.exists(candidate):
                php_script = candidate
                break
        if php_script:
            onion_proxy.install_php_proxy(docker_bin, docker_env, php_script, log_func=self.log)

        def run_proxy():
            try:
                server = onion_proxy.ThreadingHTTPServer(("127.0.0.1", self.proxy_port),
                                                         onion_proxy.OnionProxyHandler)
                server.docker_bin = docker_bin
                server.docker_env = docker_env
                server.onion_address = self.onion_address
                server.healthcheck_address = self.healthcheck_address
                server.version = self.version
                server.data_dir = self.app_support
                server.log_func = self.log
                server.launcher_script = self.launcher_script
                self.proxy_server = server
                self.log(f"Onion proxy listening on http://127.0.0.1:{self.proxy_port}")
                self.publish_state()
                server.serve_forever()
            except Exception as e:
                self.log(f"Onion proxy failed to start: {e}")
                self.proxy_server = None

        self.proxy_thread = threading.Thread(target=run_proxy, daemon=True)
        self.proxy_thread.start()

    def stop_onion_proxy(self):
        if self.proxy_server is not None:
            try:
                self.proxy_server.shutdown()
                self.log("Onion proxy stopped")
            except Exception as e:
                self.log(f"Error stopping onion proxy: {e}")
            finally:
                self.proxy_server = None
                self.proxy_thread = None

    # -- log captures -------------------------------------------------------

    def _web_log_reader_thread(self, process):
        try:
            for line in process.stdout:
                self._wp_access_log.write(line)
                if "OnionPress-HealthCheck" not in line:
                    self._wp_visitors_log.write(line)
        except Exception:
            pass

    def start_web_log_capture(self):
        if self.web_log_process is not None:
            return
        try:
            self.web_log_process = subprocess.Popen(
                [self._docker.docker_bin, "logs", "-f", "--tail", "100", "onionpress-wordpress"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace", env=self._docker.env)
            self.web_log_thread = threading.Thread(
                target=self._web_log_reader_thread, args=(self.web_log_process,), daemon=True)
            self.web_log_thread.start()
            self.log(f"Started web log capture to {self._wp_access_log.current_path()}")
        except Exception as e:
            self.log(f"Error starting web log capture: {e}")
            self.web_log_process = None

    def stop_web_log_capture(self):
        if self.web_log_process is not None:
            try:
                self.web_log_process.terminate()
                self.web_log_process.wait(timeout=5)
            except Exception:
                try:
                    self.web_log_process.kill()
                except Exception:
                    pass
            self.web_log_process = None
            if self.web_log_thread:
                self.web_log_thread.join(timeout=3)
                self.web_log_thread = None

    def _capture_specs(self):
        return {
            "onionpress-tor": self._tor_log,
            "onionheaven": self._onionheaven_log,
            "onionpress-wordpress": self._wp_errors_log,
            "onionpress-db": self._db_log,
            "onionpress-cloudflared": self._cloudflared_log,
            "onionpress-tor-client": self._tor_client_log,
        }

    def start_container_log_capture(self):
        if self._capture_supervisor_thread is not None:
            return
        self._takeover_logs = {}
        self._capture_shutdown = threading.Event()
        self._capture_supervisor_thread = threading.Thread(
            target=self._capture_supervisor_loop, daemon=True, name="container-capture-supervisor")
        self._capture_supervisor_thread.start()

    def _capture_supervisor_loop(self):
        docker_bin = self._docker.docker_bin
        docker_env = self._docker.env
        logs_dir = os.path.join(self.app_support, "logs")
        while not self._capture_shutdown.is_set():
            try:
                running = self._docker_ps_names(docker_bin, docker_env)
                desired = {}
                specs = self._capture_specs()
                for name in running:
                    if name in specs:
                        desired[name] = specs[name]
                    elif name.startswith("onionheaven-takeover"):
                        rot = self._takeover_logs.get(name)
                        if rot is None:
                            rot = RotatingLog(logs_dir, f"container-{name}",
                                              scrub_fn=redact.make_scrub_fn(self.app_support, scrub_ips=False))
                            self._takeover_logs[name] = rot
                        desired[name] = rot
                for container_name, rotating_log in desired.items():
                    entry = self._container_log_processes.get(container_name)
                    if entry is not None and entry["thread"].is_alive():
                        continue
                    self._launch_capture_worker(container_name, rotating_log, docker_bin, docker_env)
            except Exception as e:
                self.log(f"Capture supervisor: {e}")
            self._capture_shutdown.wait(60)

    def _docker_ps_names(self, docker_bin, docker_env):
        try:
            result = subprocess.run([docker_bin, "ps", "--format", "{{.Names}}"],
                                    capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", timeout=10, env=docker_env)
        except Exception:
            return set()
        if result.returncode != 0:
            return set()
        return {n.strip() for n in result.stdout.split() if n.strip()}

    def _launch_capture_worker(self, container_name, rotating_log, docker_bin, docker_env):
        entry = {"thread": None, "proc": None, "last_ts": None}
        self._container_log_processes[container_name] = entry
        worker = threading.Thread(target=self._capture_worker,
                                  args=(container_name, rotating_log, docker_bin, docker_env),
                                  daemon=True, name=f"capture-{container_name}")
        entry["thread"] = worker
        worker.start()

    def _capture_worker(self, container_name, rotating_log, docker_bin, docker_env):
        while not self._capture_shutdown.is_set():
            entry = self._container_log_processes.get(container_name)
            if entry is None:
                return
            last_ts = entry.get("last_ts")
            cmd = [docker_bin, "logs", "-f"]
            cmd += ["--since", last_ts] if last_ts else ["--tail", "100"]
            cmd.append(container_name)
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, encoding="utf-8", errors="replace", env=docker_env)
            except Exception:
                time.sleep(10)
                continue
            entry["proc"] = proc
            try:
                for line in proc.stdout:
                    rotating_log.write(line)
                    entry["last_ts"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            if container_name not in self._docker_ps_names(docker_bin, docker_env):
                self._container_log_processes.pop(container_name, None)
                return
            time.sleep(5)

    def start_clearnet_log_capture(self):
        """The VM's dmesg CLEARNET lines (the egress rules' LOG targets)."""
        limactl = os.path.join(self.bin_dir, "limactl") if self.bin_dir else "limactl"
        lima_env = os.environ.copy()
        lima_env["COLIMA_HOME"] = self.colima_home
        lima_env["LIMA_HOME"] = os.path.join(self.colima_home, "_lima")
        while not self._stop.is_set():
            try:
                result = subprocess.run([limactl, "shell", "colima", "--", "sh", "-c", "sudo dmesg"],
                                        capture_output=True, text=True, encoding="utf-8",
                                        errors="replace", timeout=15, env=lima_env)
                if result.returncode == 0:
                    all_lines = [l for l in result.stdout.splitlines() if "CLEARNET" in l]
                    if len(all_lines) > self._clearnet_last_offset:
                        for line in all_lines[self._clearnet_last_offset:]:
                            self._clearnet_log.write(line + "\n")
                        self._clearnet_last_offset = len(all_lines)
            except Exception:
                pass
            self._stop.wait(60)

    def stop_container_log_capture(self):
        if hasattr(self, "_capture_shutdown"):
            self._capture_shutdown.set()
        for name, entry in list(self._container_log_processes.items()):
            proc = entry.get("proc") if isinstance(entry, dict) else None
            thread = entry.get("thread") if isinstance(entry, dict) else None
            if proc is None:
                continue
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            if thread is not None:
                thread.join(timeout=3)
        self._container_log_processes.clear()
        self._capture_supervisor_thread = None

    # -- thumbnails -------------------------------------------------------

    def start_thumbnail_generator(self):
        """QuickLook thumbnails for My Creations (onionpress.thumbnails
        decides which), one pass a minute; a file qlmanage failed on is
        not retried until it changes."""
        if getattr(self, "_thumbnail_generator_started", False):
            return
        self._thumbnail_generator_started = True
        creations_dir = os.path.expanduser("~/OnionPress/Creations/My Creations")

        def generator():
            failed = {}
            warned_unsafe = False
            if self._stop.wait(10):
                return
            while not self._stop.is_set():
                try:
                    if os.path.isdir(creations_dir):
                        if not thumbnails.thumbs_dir_ok(creations_dir):
                            if not warned_unsafe:
                                self.log("My Creations/.thumbs is not a real folder "
                                         "(a link?); no thumbnails until it is removed")
                            warned_unsafe = True
                            self._stop.wait(60)
                            continue
                        warned_unsafe = False
                        stale, orphans, _complete = thumbnails.scan(creations_dir)
                        removed = thumbnails.prune(creations_dir, orphans)
                        if removed:
                            self.log(f"Removed {removed} thumbnail(s) of deleted Creations files")
                        for src, rel, mtime in stale:
                            if failed.get(src) == mtime:
                                continue
                            # Rendered into a private folder, then copied in
                            # through no-follow fds (onionpress.thumbnails, #460).
                            if not thumbnails.source_ok(creations_dir, src):
                                continue
                            with tempfile.TemporaryDirectory(prefix="onionpress-thumb-") as tmp:
                                try:
                                    subprocess.run(["qlmanage", "-t", "-s", "400", "-o",
                                                    tmp, src],
                                                   capture_output=True, timeout=30)
                                except subprocess.TimeoutExpired:
                                    failed[src] = mtime
                                    continue
                                ql_output = os.path.join(tmp, os.path.basename(src) + ".png")
                                if (os.path.isfile(ql_output)
                                        and not os.path.islink(ql_output)
                                        and thumbnails.install(creations_dir, rel, ql_output)):
                                    failed.pop(src, None)
                                else:
                                    failed[src] = mtime
                except Exception as e:
                    self.log(f"Thumbnail generation error: {e}")
                self._stop.wait(60)

        threading.Thread(target=generator, daemon=True, name="thumbnails").start()

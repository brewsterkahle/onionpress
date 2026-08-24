#!/usr/bin/env python3
"""OnionPress Linux background service.

Runs as the systemd user service (replaces the bash linux/onionpress script).
Uses the same src/onionpress/ package as the Mac menubar — ContainerManager,
HealthChecker, HealthMonitor — so bug fixes and improvements land on both
platforms automatically.

Lifecycle:
  1. start_containers()   — pull images, docker compose up
  2. wait_for_ready()     — wait for WordPress + Tor bootstrap
  3. _poll_loop()         — write status.json every 30s; tray reads it
  4. SIGTERM → graceful shutdown

First-run WordPress provisioning (plugin install, WP-CLI setup, multisite
config) is still handled by the provisioning entrypoint baked into the
WordPress container image. This service just starts the stack and monitors it.

Usage (systemd):
  ExecStart=/opt/onionpress/onionpress-service.py
  or: python3 /opt/onionpress/onionpress-service.py

Subcommands (thin wrappers used by the 'onionpress' CLI):
  python3 onionpress-service.py stop
  python3 onionpress-service.py restart
  python3 onionpress-service.py status
  python3 onionpress-service.py address
"""

import dataclasses
import fcntl
import json
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Optional

# ─── 0. Package path (same pattern as onionpress-tray) ──────────────────────

INSTALL_DIR = os.environ.get("ONIONPRESS_INSTALL_DIR", "/opt/onionpress")
_LIB_DIR = os.path.join(INSTALL_DIR, "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

try:
    from onionpress.containers import ContainerManager
    from onionpress.health import (
        HealthChecker, HealthMonitor, HealthResult, ServiceState,
        POLL_READY_SECONDS, POLL_STARTING_SECONDS,
    )
    from onionpress.docker import Docker
    from onionpress.platform import resolve_paths, OS, detect_os
    from onionpress.config import (
        ensure_config, ensure_secrets, read_value, resolve_port_offset,
    )
    from onionpress import launcher_ops, system_metrics
    from onionpress.power import SystemdInhibitor
except ImportError as e:
    print(f"ERROR: onionpress package not found in {_LIB_DIR}: {e}", file=sys.stderr)
    sys.exit(1)

# ─── 1. Paths + logging ─────────────────────────────────────────────────────

DATA_DIR = os.path.join(os.path.expanduser("~"), ".onionpress")
DOCKER_DIR = os.path.join(INSTALL_DIR, "docker")
LOG_FILE = os.path.join(DATA_DIR, "onionpress.log")
PID_FILE = os.path.join(DATA_DIR, "onionpress.pid")
STATUS_FILE = os.path.join(DATA_DIR, "status.json")
VERSION_FILE = os.path.join(INSTALL_DIR, "VERSION")

os.makedirs(DATA_DIR, exist_ok=True)

_handler = logging.handlers.RotatingFileHandler(
    LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3,
)
_handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s",
                                        datefmt="%Y-%m-%d %H:%M:%S"))
_stdout = logging.StreamHandler(sys.stdout)
_stdout.setFormatter(logging.Formatter("[%(asctime)s] %(message)s",
                                       datefmt="%Y-%m-%d %H:%M:%S"))
logging.basicConfig(level=logging.INFO, handlers=[_handler, _stdout])
log = logging.getLogger(__name__).info


def _version() -> str:
    try:
        return open(VERSION_FILE).read().strip()
    except OSError:
        return "unknown"


# ─── 2. Docker + manager setup ──────────────────────────────────────────────

def _make_docker() -> Docker:
    uid = os.getuid()
    rootless = f"/run/user/{uid}/docker.sock"
    sock = rootless if os.path.exists(rootless) else "/var/run/docker.sock"
    return Docker(socket_path=sock, log_func=log)


def _make_manager(docker: Docker) -> ContainerManager:
    paths = resolve_paths(data_dir=DATA_DIR)
    # resolve_paths() leaves docker_dir="" when there's no app bundle.
    # On Linux the docker configs live under INSTALL_DIR.
    paths = dataclasses.replace(paths, docker_dir=DOCKER_DIR)
    port_cfg = resolve_port_offset()
    return ContainerManager(docker, paths, port_cfg, log_func=log)


# ─── 3. Status.json writer ──────────────────────────────────────────────────

_start_time = time.time()


def _onionheaven_stats(docker: Docker) -> dict:
    """Query the OnionHeaven container for hub stats. Best-effort."""
    result = docker.exec(
        "onionheaven",
        ["sqlite3", "/var/lib/onionpress/onionheaven/registry.db",
         "SELECT COUNT(*), SUM(status='online'), SUM(status='taken-over') "
         "FROM registry"],
        timeout=5, quiet=True,
    )
    registered, online, taken_over = 0, 0, 0
    if result.ok and result.output.strip():
        parts = result.output.strip().split("|")
        try:
            registered = int(parts[0] or 0)
            online = int(parts[1] or 0)
            taken_over = int(parts[2] or 0)
        except (ValueError, IndexError):
            pass

    workers = docker.run(
        ["ps", "--format", "{{.Names}}"], timeout=5, quiet=True,
    )
    takeover_containers = 0
    if workers.ok:
        takeover_containers = sum(
            1 for n in workers.output.splitlines()
            if n.strip().startswith("onionheaven-takeover-")
        )

    # Is the client registered? Read from shared volume written by heartbeat.
    client_registered = False
    reg_result = docker.exec(
        "onionpress-wordpress",
        ["sh", "-c",
         "cat /var/lib/onionpress/onionheaven/client_registered 2>/dev/null"],
        timeout=5, quiet=True,
    )
    if reg_result.ok and reg_result.output.strip() in ("1", "true", "yes"):
        client_registered = True

    hub = read_value(
        os.path.join(DATA_DIR, "config"),
        "ONIONHEAVEN_ADDRESS", "",
    )
    enabled = read_value(
        os.path.join(DATA_DIR, "config"),
        "REGISTER_WITH_ONIONHEAVEN", "no",
    )

    return {
        "server_active": docker.container_running("onionheaven"),
        "registered_count": registered,
        "online_count": online,
        "taken_over_count": taken_over,
        "takeover_containers": takeover_containers,
        "client_registered": client_registered,
        "client_hub": hub,
        "client_enabled": enabled == "yes",
    }


def write_status(
    *,
    docker: Docker,
    manager: ContainerManager,
    health_result: Optional[HealthResult],
    service_state: ServiceState,
    onion_address: str,
) -> None:
    """Write ~/.onionpress/status.json in the format the tray expects."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    uptime = int(time.time() - _start_time)

    # Container states
    containers: dict[str, str] = {}
    ps = docker.run(
        ["ps", "-a",
         "--filter", "name=onionpress-",
         "--filter", "name=onionheaven",
         "--format", "{{.Names}}\t{{.State}}"],
        timeout=10, quiet=True,
    )
    if ps.ok:
        for line in ps.output.splitlines():
            parts = line.strip().split("\t", 1)
            if len(parts) == 2:
                containers[parts[0]] = parts[1]

    bootstrap_pct = health_result.bootstrap_pct if health_result else 0

    # External reachability: HealthResult.tor_externally_reachable
    # is itself None until Check 5 actually runs (full_check gates it on
    # tor_internally_ready + onion_address) — mirror it straight through
    # rather than re-deriving the gate here, so this can never fall out of
    # sync with what actually decides "did we run the check".
    onion_reachable = health_result.tor_externally_reachable if health_result else None
    onion_http_code = health_result.external_http_code if health_result else None

    # Map ServiceState → status.json "state"
    state_str = {
        ServiceState.AVAILABLE: "running",
        ServiceState.STARTING: "starting",
        ServiceState.STOPPED: "stopped",
        ServiceState.OFFLINE: "starting",
        ServiceState.STUCK: "starting",
    }.get(service_state, "starting")

    # WordPress install / onboarding status
    wp_installed = manager.wp_is_installed()

    onboarded = False
    if wp_installed:
        opt = docker.exec(
            "onionpress-wordpress",
            ["wp", "--allow-root", "option", "get", "onionpress_onboarded"],
            timeout=10, quiet=True,
        )
        onboarded = opt.ok and opt.output.strip() in ("1", "true", "yes")

    # Wayback queue depth (items waiting to be archived)
    wayback_queue = 0
    wq = docker.exec(
        "onionpress-wordpress",
        ["wp", "--allow-root", "eval",
         "echo count(get_posts(['post_type'=>'any','post_status'=>'any',"
         "'meta_key'=>'_op_wayback_pending','posts_per_page'=>-1]));"],
        timeout=10, quiet=True,
    )
    if wq.ok:
        try:
            wayback_queue = int(wq.output.strip())
        except ValueError:
            pass

    # mkp224o availability
    mkp224o = launcher_ops.tor_image_has_mkp224o()

    # Vanity generation in progress
    vanity_in_progress = os.path.exists(os.path.join(DATA_DIR, ".vanity-running"))

    # Admin password path
    pw_path = os.path.join(DATA_DIR, "wp-admin-password")
    wp_admin_password_path = pw_path if os.path.exists(pw_path) else None

    # OnionHeaven stats (best-effort)
    oh = _onionheaven_stats(docker)

    # serving_from_wayback: not running locally but registered with hub
    serving_from_wayback = (state_str != "running") and oh["client_registered"]

    # System metrics (load average, host uptime)
    metrics = system_metrics.host_metrics()
    load_avg = metrics.get("load_avg", [])
    host_uptime = metrics.get("host_uptime_seconds", 0)

    status = {
        "state": state_str,
        "version": _version(),
        "onion_address": onion_address,
        "uptime_seconds": uptime,
        "bootstrap_pct": bootstrap_pct,
        "onion_reachable": onion_reachable,
        "onion_http_code": onion_http_code,
        "containers": containers,
        "wayback_queue_count": wayback_queue,
        "updated_at": now,
        "platform": "linux",
        "load_avg": load_avg,
        "host_uptime_seconds": host_uptime,
        "onionheaven": oh,
        "wp_installed": wp_installed,
        "onboarded": onboarded,
        "mkp224o_available": mkp224o,
        "wp_admin_password_path": wp_admin_password_path,
        "vanity_in_progress": vanity_in_progress,
        "serving_from_wayback": serving_from_wayback,
        "wp_port": manager.port_config.wp_port,
    }

    payload = json.dumps(status, indent=2)

    # Atomic write so the tray never reads a partial file
    tmp = STATUS_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(payload)
    os.replace(tmp, STATUS_FILE)

    # Mirror into the WordPress container for the status/settings page
    docker.run(
        ["exec", "-i", "onionpress-wordpress",
         "sh", "-c", "cat > /var/lib/onionpress/status.json"],
        input=payload,
        timeout=10, quiet=True,
    )


# ─── 4. Main service loop ────────────────────────────────────────────────────

_stop_event = threading.Event()

# Sleep/wake (mirrors Mac handle_sleep/handle_wake in src/menubar.py).
# _sleeping_event is set while the system is suspended so the poll loop
# throttles and skips Docker exec calls that would just time out.
# _wake_event is used as a "kick" so the poll loop runs an immediate
# iteration on wake (and on shutdown) rather than sleeping out the
# remaining poll interval.
_sleeping_event = threading.Event()
_wake_event = threading.Event()
_dbus_proc: Optional[subprocess.Popen] = None

# Sleep inhibitor (systemd-inhibit). Created in _start() and accessed by
# the sleep/wake handlers — released before suspend so the system can
# actually sleep, re-acquired on wake. Mirrors Mac's CaffeineManager.
_inhibitor: Optional["SystemdInhibitor"] = None


def _read_config_value(key: str, default: str) -> str:
    """Read a single value from ~/.onionpress/config."""
    return read_value(os.path.join(DATA_DIR, "config"), key, default)


def _handle_sleep(docker: Docker) -> None:
    """DEL_ONION via watchdogs so the OnionHeaven hub can take over without
    competing descriptors — same as src/menubar.py handle_sleep()."""
    log("System going to sleep — DEL_ONION via watchdogs")
    _sleeping_event.set()
    _wake_event.clear()
    for container in ("onionpress-tor", "onionheaven"):
        if launcher_ops.signal_watchdog(docker, container, "USR1"):
            log(f"Sent USR1 (sleep) to {container} watchdog")
    # Release the sleep inhibitor so the kernel can actually suspend.
    if _inhibitor is not None:
        _inhibitor.stop()


def _handle_wake(docker: Docker) -> None:
    """ADD_ONION via watchdogs and kick the poll loop into an immediate
    iteration — same as src/menubar.py handle_wake()."""
    log("System wake — ADD_ONION via watchdogs")
    _sleeping_event.clear()
    for container in ("onionpress-tor", "onionheaven"):
        if launcher_ops.signal_watchdog(docker, container, "USR2"):
            log(f"Sent USR2 (wake) to {container} watchdog")
    # Re-acquire the inhibitor (no-op if PREVENT_SLEEP=normal).
    if _inhibitor is not None:
        _inhibitor.start()
    # Kick the poll loop so it doesn't sit out the remaining interval.
    _wake_event.set()


def _start_sleep_wake_monitor(docker: Docker) -> None:
    """Watch system D-Bus for org.freedesktop.login1 PrepareForSleep signals.

    Uses `dbus-monitor` (always present on systemd distros) rather than
    pulling in python-dbus. PrepareForSleep fires with `true` immediately
    before suspend and `false` immediately after resume.
    """
    def _watch() -> None:
        global _dbus_proc
        try:
            _dbus_proc = subprocess.Popen(
                ["dbus-monitor", "--system",
                 "type='signal',interface='org.freedesktop.login1.Manager',"
                 "member='PrepareForSleep'"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1,
            )
        except FileNotFoundError:
            log("dbus-monitor not available — sleep/wake handling disabled")
            return

        try:
            in_signal = False
            for line in _dbus_proc.stdout:
                if _stop_event.is_set():
                    break
                line = line.strip()
                if line.startswith("signal ") and "PrepareForSleep" in line:
                    in_signal = True
                    continue
                if in_signal and line.startswith("boolean "):
                    going_to_sleep = line.endswith("true")
                    in_signal = False
                    try:
                        if going_to_sleep:
                            _handle_sleep(docker)
                        else:
                            _handle_wake(docker)
                    except Exception as e:
                        log(f"sleep/wake handler error: {e}")
        finally:
            if _dbus_proc:
                try:
                    _dbus_proc.terminate()
                except Exception:
                    pass

    threading.Thread(target=_watch, daemon=True, name="sleep-wake").start()


def _poll_loop(docker: Docker, manager: ContainerManager) -> None:
    """Background thread: health-check + write status.json on each interval.

    Starts immediately after containers are launched — same as the Mac menubar
    check_status() thread — so the tray shows Tor bootstrap percentage and WP
    provisioning progress rather than a static icon until everything is ready.
    """
    checker = HealthChecker(docker, log_func=log)
    monitor = HealthMonitor(log_func=log)
    onion_address = ""
    service_state = ServiceState.STARTING
    _shared_volume_written = False
    _was_sleeping = False

    while not _stop_event.is_set():
        # Detect sleep→wake transition: re-verify WordPress on the next poll
        # (mirrors src/menubar.py handle_wake setting _wordpress_confirmed=False).
        # The wedge warning is intentionally NOT reset here — it's sticky across
        # sleep and only clears when the service becomes fully ready.
        is_sleeping_now = _sleeping_event.is_set()
        if _was_sleeping and not is_sleeping_now:
            monitor.state.wordpress_confirmed = False
            log("Wake transition — will re-verify WordPress on next poll")
        _was_sleeping = is_sleeping_now

        # While the system is suspended, skip the docker exec calls (they
        # just time out) and write a lightweight status snapshot every 30s
        # so the tray sees "asleep" rather than stale data.
        if is_sleeping_now:
            try:
                write_status(
                    docker=docker,
                    manager=manager,
                    health_result=None,
                    service_state=ServiceState.OFFLINE,
                    onion_address=onion_address,
                )
            except Exception as e:
                log(f"Status poll (sleeping) error: {e}")
            # Wait up to 30s, but break out immediately on wake or shutdown.
            _wake_event.wait(timeout=30)
            _wake_event.clear()
            continue

        try:
            hr = checker.full_check(
                expected_address=onion_address,
                wordpress_confirmed=monitor.state.wordpress_confirmed,
            )

            # When we first learn the onion address, copy it to the shared
            # volume so WP plugins (domain-map, etc.) can pick it up.
            if hr.onion_address and hr.onion_address != onion_address:
                onion_address = hr.onion_address
                log(f"Onion address: {onion_address}")

            if onion_address and not _shared_volume_written:
                r = docker.exec(
                    "onionpress-tor",
                    ["sh", "-c",
                     "cp /var/lib/tor/hidden_service/wordpress/hostname "
                     "/var/lib/onionpress/onion_address"],
                    timeout=10, quiet=True,
                )
                if r.ok:
                    _shared_volume_written = True

            service_state = monitor.evaluate(hr, is_running=True)

            # One-shot wedge hint after 10+ min yellow — mirrors src/menubar.py.
            # On Linux the recovery is restarting the user service or the Docker
            # daemon, not killing Colima.
            if monitor.should_emit_wedge_warning():
                log(
                    "WEDGE WARNING: WordPress unreachable for 10+ min. "
                    "Try restarting the OnionPress service: "
                    "`systemctl --user restart onionpress` "
                    "(or the system equivalent). If still wedged, the Docker "
                    "daemon may be stuck — restart it with "
                    "`systemctl --user restart docker` (rootless) or "
                    "`sudo systemctl restart docker` (system Docker)."
                )

            if monitor.should_restart_tor(checker.tor_container_unhealthy()):
                log("Auto-restarting Tor container")
                docker.run(["restart", "onionpress-tor"], timeout=30)

            write_status(
                docker=docker,
                manager=manager,
                health_result=hr,
                service_state=service_state,
                onion_address=onion_address,
            )

        except Exception as e:
            log(f"Status poll error: {e}")

        interval = monitor.poll_interval(service_state)
        # Use _wake_event as a "kick" — set on system wake and on shutdown
        # so the loop runs an immediate iteration rather than sitting out
        # the remaining interval.
        _wake_event.wait(timeout=interval)
        _wake_event.clear()


def _start(manager: ContainerManager, docker: Docker) -> None:
    """Full startup sequence."""
    global _inhibitor

    # PID lock
    pid_fd = open(PID_FILE, "w")
    try:
        fcntl.flock(pid_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        existing = open(PID_FILE).read().strip()
        print(f"OnionPress is already running (PID {existing})", file=sys.stderr)
        sys.exit(1)
    pid_fd.write(str(os.getpid()))
    pid_fd.flush()

    def _cleanup(*_):
        _stop_event.set()
        # Kick the poll loop out of its wait so shutdown is prompt.
        _wake_event.set()
        # Release the sleep inhibitor so it doesn't outlive the service.
        if _inhibitor is not None:
            _inhibitor.stop()
        # Terminate the dbus-monitor subprocess if it's running.
        if _dbus_proc is not None:
            try:
                _dbus_proc.terminate()
            except Exception:
                pass
        try:
            os.unlink(PID_FILE)
        except OSError:
            pass

    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT, _cleanup)

    log(f"OnionPress service starting (v{_version()})")

    ensure_config(manager.paths)

    # Pull images (best-effort; containers may already be present)
    manager.pull_images()

    # Start core services, then Tor
    if not manager.start_core():
        log("ERROR: failed to start core containers — aborting")
        _cleanup()
        sys.exit(1)

    if not manager.start_tor():
        log("WARNING: Tor failed to start — will retry via health loop")

    # Start polling immediately — same as Mac menubar. The poll loop writes
    # status.json on every tick so the tray shows Tor bootstrap progress,
    # WP provisioning, etc. rather than a blank icon until everything is ready.
    _poll_loop_thread = threading.Thread(
        target=_poll_loop, args=(docker, manager), daemon=True,
    )
    _poll_loop_thread.start()

    # Watch for system sleep/wake so we DEL_ONION before suspend (lets the
    # OnionHeaven hub take over without competing descriptors) and ADD_ONION
    # immediately on resume. Mirrors src/menubar.py handle_sleep/handle_wake.
    _start_sleep_wake_monitor(docker)

    # Optional sleep inhibitor (no-op when PREVENT_SLEEP=normal). Mirrors
    # the Mac CaffeineManager started in src/menubar.py:1873.
    _inhibitor = SystemdInhibitor(DATA_DIR, log, _read_config_value)
    _inhibitor.start()

    # Notify systemd that the service is up (containers are starting)
    notify = os.environ.get("NOTIFY_SOCKET")
    if notify:
        subprocess.run(
            ["systemd-notify", "--ready"],
            env={**os.environ, "NOTIFY_SOCKET": notify},
            check=False,
        )

    log("OnionPress is running!")

    # Block until SIGTERM
    _stop_event.wait()
    log("Shutting down")
    manager.stop()
    pid_fd.close()


# ─── 5. CLI subcommands ─────────────────────────────────────────────────────

def _cmd_stop(manager: ContainerManager) -> None:
    manager.stop()
    log("Stopped")


def _cmd_restart(manager: ContainerManager, docker: Docker) -> None:
    manager.stop()
    time.sleep(2)
    _start(manager, docker)


def _cmd_status() -> None:
    try:
        data = json.loads(open(STATUS_FILE).read())
        state = data.get("state", "unknown")
        addr = data.get("onion_address", "")
        pct = data.get("bootstrap_pct", 0)
        print(f"State:   {state}")
        if addr:
            print(f"Address: {addr}")
        if pct < 100:
            print(f"Tor:     {pct}% bootstrapped")
    except OSError:
        print("OnionPress is not running (no status file)")
        sys.exit(1)


def _cmd_address() -> None:
    try:
        data = json.loads(open(STATUS_FILE).read())
        addr = data.get("onion_address", "")
        if addr:
            print(addr)
        else:
            print("Generating...")
            sys.exit(1)
    except OSError:
        print("OnionPress is not running")
        sys.exit(1)


# ─── 6. Entry point ─────────────────────────────────────────────────────────

def main() -> None:
    docker = _make_docker()
    manager = _make_manager(docker)

    cmd = sys.argv[1] if len(sys.argv) > 1 else "start"

    if cmd == "start":
        _start(manager, docker)
    elif cmd == "stop":
        _cmd_stop(manager)
    elif cmd == "restart":
        _cmd_restart(manager, docker)
    elif cmd == "status":
        _cmd_status()
    elif cmd == "address":
        _cmd_address()
    else:
        print(f"Usage: {sys.argv[0]} [start|stop|restart|status|address]",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

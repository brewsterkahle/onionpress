"""Health checks and restart decisions for OnionPress.

Extracts the health monitoring logic from menubar.py into a testable module.
The MenubarApp (or CLI) creates a HealthMonitor and calls check() periodically.
"""

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from .docker import Docker


# Curl exit-code → human reason map. When an external reachability probe
# fails before getting an HTTP response, check_status formats the result
# as "000rc=N" so the rc travels with the http_code through the same
# logging path. Centralising the dict here means the Mac MenubarApp and
# any future Linux health-check path log the same wording.
CURL_RC_REASONS = {
    "1": "protocol error (descriptor not yet available)",
    "6": "DNS resolution failed",
    "7": "connection refused",
    "28": "timeout (30s)",
    "35": "TLS handshake failed",
    "52": "empty reply",
    "56": "connection reset",
    "97": "SOCKS handshake failed (descriptor not yet available)",
}


def decode_curl_reason(http_code: str) -> str:
    """Decode a 'rc=N' suffix on a 000-style http_code into a human reason.

    Returns a short phrase suitable for log output, falling back to
    "curl rc=N" for unknown codes or "unknown" if no rc is present.
    """
    rc = http_code.split("rc=")[1] if "rc=" in http_code else ""
    return CURL_RC_REASONS.get(rc, f"curl rc={rc}" if rc else "unknown")


class ServiceState(Enum):
    """Overall OnionPress service state."""
    STOPPED = "stopped"
    STARTING = "starting"
    AVAILABLE = "available"
    OFFLINE = "offline"
    STUCK = "stuck"


@dataclass
class HealthResult:
    """Result of a single health check cycle."""
    wp_healthy: bool = False
    tor_bootstrapped: bool = False
    tor_internally_ready: bool = False  # Checks 1-4 passed
    # Check 5 (dual-probe) is tri-state: None until it actually runs — the
    # gate below is the ONLY place that assigns it. Do not default this to
    # False: an unrun check and a check that came back negative must stay
    # distinguishable all the way out to status.json (see write_status()
    # in linux/onionpress-service.py and menubar.py's write_status_to_volume).
    tor_externally_reachable: Optional[bool] = None
    onion_address: str = ""
    bootstrap_pct: int = 0
    external_http_code: Optional[str] = None  # HTTP status from external reachability check
    errors: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        """Service is fully operational."""
        return self.wp_healthy and self.tor_externally_reachable is True


@dataclass
class WedgeSignals:
    """VM/FUSE wedge probe result.

    Produced by HealthChecker.check_vm_wedge(). The wedge pattern
    (fuse_lock_inode pile-up in the WP container's apache workers,
    kernel-suppressed after one dmesg report) is invisible to the
    existing health checks, so we surface it as two cheap signals the
    analytics uploader can see: VM-wide load average, and the WP
    container's docker-level health status.
    """
    loadavg_1min: Optional[float] = None
    wp_health_status: Optional[str] = None     # "healthy" | "unhealthy" | "starting"
    wp_failing_streak: Optional[int] = None


# Thresholds for deciding when to log (observational only — no actions).
WEDGE_LOAD_WARN = 5.0        # VM 1-min load above this is already unusual
WEDGE_LOAD_ALARM = 20.0      # Above this = real pile-up, worth an alarm
WEDGE_FAILING_STREAK_ALARM = 3   # WP health-check failed 3+ times in a row


# Patterns in Tor logs indicating the container is sick and restart will help.
SICK_PATTERNS = [
    "No usable guards",
    "Too many preemptive onion service circuits failed",
    "Rejected 60/60 as down",
    "Could not connect rendezvous circuit",
]

# Patterns indicating Tor is healthy (just waiting for propagation).
HEALTHY_PATTERNS = [
    "Sufficiently bootstrapped",
]


class HealthChecker:
    """Stateless health check operations.

    Each method performs a single check and returns a result.
    No state tracking — that's HealthMonitor's job.
    """

    def __init__(
        self,
        docker: Docker,
        log_func: Callable[[str], None] | None = None,
    ):
        self.docker = docker
        self.log_func = log_func

    def _log(self, msg: str) -> None:
        if self.log_func:
            self.log_func(msg)

    def check_wordpress_local(self, wp_port: int = 80) -> bool:
        """Check if WordPress responds locally inside the container."""
        result = self.docker.exec(
            "onionpress-wordpress",
            ["curl", "-sf", "--max-time", "3", f"http://localhost:{wp_port}/"],
            timeout=10,
        )
        if result.ok:
            # Check for database errors
            if "Error establishing a database connection" in result.stdout:
                return False
            return True
        return False

    def check_wordpress_external(self, wp_port: int, log: bool = True) -> bool:
        """Check WordPress responds via the host port mapping.

        Exercises the Mac → Colima → Docker → WordPress path; complements
        check_wordpress_local which only tests inside the container. Uses
        curl instead of urllib to avoid macOS "local network" TCC prompts.
        """
        if log:
            self._log(f"Checking local access: http://localhost:{wp_port}")
        try:
            result = subprocess.run(
                ["curl", "-s", "--max-time", "3",
                 "-H", "User-Agent: OnionPress-HealthCheck",
                 f"http://localhost:{wp_port}"],
                capture_output=True,
                text=True, encoding='utf-8', errors='replace',
                timeout=5,
            )
        except Exception as e:
            if log:
                self._log(f"✗ Local access: Connection failed ({e})")
            return False

        if result.returncode != 0:
            if log:
                self._log(f"✗ Local access: Connection failed (curl exit code {result.returncode})")
            return False

        content = result.stdout
        if ("Error establishing a database connection" in content
                or "Database connection error" in content):
            if log:
                self._log("✗ Local access: Database connection error")
            return False

        if log:
            self._log("✓ Local access: WordPress responding")
        return True

    def check_tor_bootstrap(self) -> tuple[bool, int]:
        """Check Tor bootstrap status via control port (with log fallback).

        Returns:
            (bootstrapped, percentage) — bootstrapped is True if 100%.
        """
        # Primary: query control port directly (reliable, not affected by log rotation)
        result = self.docker.exec(
            "onionpress-tor",
            ["sh", "-c",
             'printf "AUTHENTICATE $(cat /var/lib/tor/control_auth_cookie '
             '| od -A n -t x1 | tr -d \' \\n\')\\r\\n'
             'GETINFO status/bootstrap-phase\\r\\nQUIT\\r\\n"'
             ' | nc -w 5 127.0.0.1 9051'],
            timeout=15,
        )
        if result.ok and "PROGRESS=" in result.output:
            for part in result.output.split():
                if part.startswith("PROGRESS="):
                    try:
                        pct = int(part.split("=")[1])
                        if pct < 100:
                            self._log(f"Checking Tor bootstrap status... {pct}%")
                        return pct >= 100, pct
                    except ValueError:
                        pass

        # Fallback: parse container logs (for Arti or if control port unavailable)
        result = self.docker.run(
            ["logs", "--tail", "100", "onionpress-tor"],
            timeout=15,
        )
        if not result.ok:
            self._log("Checking Tor bootstrap status... failed")
            return False, 0

        output = result.stdout
        pct = 0

        for m in re.finditer(r"PROGRESS=(\d+)", output):
            p = int(m.group(1))
            if p > pct:
                pct = p

        if "Sufficiently bootstrapped" in output:
            pct = max(pct, 100)
        if "Bootstrapped 100%" in output:
            pct = 100

        if pct < 100:
            self._log(f"Checking Tor bootstrap status... {pct}%")
        return pct >= 100, pct

    def check_tor_hostname(self, expected_address: str = "") -> str:
        """Read the onion hostname from the Tor container.

        Returns the address, or "" if not available.
        """
        result = self.docker.exec(
            "onionpress-tor",
            ["cat", "/var/lib/tor/hidden_service/wordpress/hostname"],
            timeout=10,
        )
        addr = result.output.strip() if result.ok else ""
        if expected_address and addr and addr != expected_address:
            self._log(f"WARNING: hostname mismatch: expected={expected_address}, got={addr}")
        return addr

    def check_internal_connectivity(self) -> bool:
        """Check if WordPress is reachable from inside the Tor container."""
        result = self.docker.exec(
            "onionpress-tor",
            ["curl", "-sf", "--max-time", "15",
             "-H", "User-Agent: OnionPress-HealthCheck",
             "http://wordpress:80/"],
            timeout=20,
        )
        return result.ok

    # Well-known onion used to probe whether onionheaven's tor is itself
    # healthy. If onionheaven can reach the OnionHeaven hub it clearly
    # has working circuits; if it can't reach anything, its "our onion
    # is unreachable" verdict is meaningless. Value matches the hub
    # address in MEMORY.md / onionheaven code.
    _ONIONHEAVEN_HUB_ADDRESS = (
        "oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion"
    )
    # Cache onionheaven-tor-healthy result this long, since we only need
    # it on self✓/external✗ disagreement and don't want to re-probe the
    # hub on every health cycle.
    _ONIONHEAVEN_HEALTH_TTL_SECONDS = 60.0

    def _probe_onion_via(self, container: str, onion_address: str) -> tuple[bool, str]:
        """Curl an onion address through `container`'s local SOCKS (127.0.0.1:9050).

        Returns (reachable, http_code). 200/301 from the real site counts
        as reachable; anything else (including 302 from the OnionHeaven
        takeover redirector) does not. Also inspects response headers
        for `X-OnionHeaven-Takeover: 1` — if present, we're talking to
        the hub's takeover replica regardless of status code, and we
        return reachable=False with the sentinel http_code "takeover"
        so the caller can log + account for it cleanly. Curl transport
        failure returns "000:rc=<exit>".
        """
        if not onion_address:
            return False, ""
        # -D - dumps response headers to stdout; -o /dev/null suppresses
        # the body; -w appends "---<code>" at the very end so we can
        # split headers from status. The delimiter must be something
        # that can't appear in an RFC-compliant HTTP header.
        result = self.docker.exec(
            container,
            [
                "curl", "-s", "--max-time", "30",
                "--socks5-hostname", "127.0.0.1:9050",
                "-o", "/dev/null",
                "-D", "-",
                "-w", "\n---STATUS---%{http_code}",
                "-H", "User-Agent: OnionPress-HealthCheck",
                f"http://{onion_address}/",
            ],
            timeout=45,
        )
        if not result.ok:
            return False, f"000:rc={result.returncode}"
        raw = result.output or ""
        headers_part, _, tail = raw.rpartition("---STATUS---")
        http_code = tail.strip() if headers_part else raw.strip()
        # Case-insensitive header match; curl echoes headers verbatim.
        if headers_part:
            for line in headers_part.splitlines():
                name, _, value = line.partition(":")
                if name.strip().lower() == "x-onionheaven-takeover" and value.strip() == "1":
                    return False, "takeover"
        return http_code in ("200", "301"), http_code

    def check_self_reachability(self, onion_address: str) -> tuple[bool, str]:
        """Probe our onion through onionpress-tor's own SOCKS.

        Confirmed by timing (15s cold, 1.7s warm) that C Tor routes
        self-connections through real circuits rather than a local
        shortcut — so this is a genuine Tor-network probe, just using
        the same tor instance that also hosts the service.
        """
        return self._probe_onion_via("onionpress-tor", onion_address)

    def check_onionheaven_tor_healthy(self) -> bool:
        """Is onionheaven's tor able to reach any onion at all?

        Used to disambiguate self✓/external✗ disagreement in
        check_external_reachability. Cached for 60s since onionheaven
        health changes on a container-lifetime scale, not a single
        health-cycle scale.
        """
        now = time.time()
        cached = getattr(self, "_onionheaven_health_cache", None)
        if cached and (now - cached[0]) < self._ONIONHEAVEN_HEALTH_TTL_SECONDS:
            return cached[1]
        ok, _ = self._probe_onion_via("onionheaven", self._ONIONHEAVEN_HUB_ADDRESS)
        self._onionheaven_health_cache = (now, ok)
        return ok

    def check_external_reachability(self, onion_address: str) -> tuple[bool, str]:
        """Check if our onion service is reachable through the Tor network.

        Dual-probe with disambiguation:
          - Probe via onionheaven (independent tor, separate circuits).
            If it succeeds, we're reachable — done. This is the hot path
            and costs exactly one curl.
          - If it fails, probe via onionpress-tor (our own tor — self-
            connection via real circuits). If that fails too, we're
            genuinely down.
          - If self✓ and external✗, disambiguate: is onionheaven's tor
            itself working? If it can't reach the OnionHeaven hub
            either, the external verdict is untrustworthy — report
            reachable with code "degraded:<ext>". Otherwise report
            unreachable (likely a descriptor-publish issue, which is
            invisible to our own tor but real for outside visitors).

        Returns (reachable, http_code). Only 200/301 from at least one
        probe counts as reachable; 302 from external means OnionHeaven
        takeover (still unreachable from our POV).
        """
        if not onion_address:
            return False, ""

        ext_ok, ext_code = self._probe_onion_via("onionheaven", onion_address)
        if ext_ok:
            return True, ext_code

        self_ok, self_code = self._probe_onion_via("onionpress-tor", onion_address)
        if not self_ok:
            # Both failed — genuinely unreachable. Prefer ext_code since
            # existing callers know how to format onionheaven failures.
            return False, ext_code

        # Disagreement: self says yes, external says no. Is the external
        # probe trustworthy?
        if not self.check_onionheaven_tor_healthy():
            self._log(
                f"Reachability: onionheaven tor is itself unhealthy — "
                f"trusting self-probe (ext={ext_code})"
            )
            return True, f"degraded:ext={ext_code}"

        # External probe is healthy and still can't reach us. That's a
        # real problem for outside visitors (likely descriptor-publish
        # failure) even though our own tor can rendezvous to itself.
        self._log(
            f"Reachability: onionheaven healthy but can't reach us "
            f"(ext={ext_code}, self={self_code}) — likely descriptor issue"
        )
        return False, ext_code

    def check_vm_wedge(self) -> Optional["WedgeSignals"]:
        """Probe for VM/FUSE wedge conditions (onionpress.org Apr 2026 incident).

        Two signals read via paths that don't touch the FUSE mount:
          - /proc/loadavg from the tor container (tor's root fs is a docker
            named volume, never on sshfs — stays responsive even when
            apache in the WP container is piled up in fuse_lock_inode).
          - docker inspect health status for the WP container (API-level;
            doesn't exec into it — exec on a wedged container would itself
            queue on the same inode lock).

        Never execs into onionpress-wordpress — during a real wedge that
        exec would block forever and the health loop would hang.

        Returns WedgeSignals on success, None if the probe itself failed
        (e.g. containers not up). Caller logs only when thresholds trip
        so steady-state polling stays quiet.
        """
        # Load average from tor container's /proc (VM-global, one number).
        loadavg_1min = None
        r = self.docker.exec(
            "onionpress-tor",
            ["cat", "/proc/loadavg"],
            timeout=5,
            quiet=True,
        )
        if r.ok and r.output:
            try:
                loadavg_1min = float(r.output.split()[0])
            except (ValueError, IndexError):
                pass

        # WP container docker-level health via inspect (no exec).
        wp_health_status = None
        wp_failing_streak = None
        r = self.docker.run(
            ["inspect", "--format", "{{json .State.Health}}", "onionpress-wordpress"],
            timeout=5,
            quiet=True,
        )
        if r.ok and r.output.strip() and r.output.strip() != "null":
            try:
                h = json.loads(r.output)
                wp_health_status = h.get("Status")
                wp_failing_streak = h.get("FailingStreak")
            except (ValueError, TypeError):
                pass

        if loadavg_1min is None and wp_health_status is None:
            return None

        return WedgeSignals(
            loadavg_1min=loadavg_1min,
            wp_health_status=wp_health_status,
            wp_failing_streak=wp_failing_streak,
        )

    @staticmethod
    def check_internet_connectivity() -> bool:
        """Check if the host has network access without triggering TCC.

        Scans `ifconfig` output for any non-loopback interface with an
        IPv4 address. Does NOT call SCNetworkReachability (which fires
        the macOS Local Network permission prompt on Sequoia+, even
        though the call makes no traffic). On any failure, assumes
        connected rather than block the app.
        """
        try:
            result = subprocess.run(
                ["ifconfig"],
                capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=3,
            )
        except Exception:
            return True

        if result.returncode != 0:
            return True

        current_iface = None
        for line in result.stdout.splitlines():
            if line and not line[0].isspace():
                current_iface = line.split(":", 1)[0]
            elif current_iface and current_iface != "lo0" and "\tinet " in line:
                return True
        return False

    def tor_container_unhealthy(self) -> bool:
        """Check Tor container logs for signs of sickness.

        Returns True if restart is likely to help.
        """
        self._log("Checking Tor container health...")
        result = self.docker.run(
            ["logs", "--tail", "50", "onionpress-tor"],
            timeout=10,
        )
        if not result.ok:
            return True  # Can't read logs — assume unhealthy

        output = result.stdout

        # Never restart a tor that recently bootstrapped — it just needs
        # time for descriptor propagation (10-60s).  Restarting resets the
        # descriptor upload and creates a self-defeating restart loop.
        if "Bootstrapped 100%" in output or "Sufficiently bootstrapped" in output:
            self._log("Tor bootstrapped — waiting for descriptor propagation")
            return False

        for pattern in SICK_PATTERNS:
            if pattern in output:
                return True

        for pattern in HEALTHY_PATTERNS:
            if pattern in output:
                return False  # Healthy, just waiting

        # No recognizable patterns — don't restart without clear signals.
        # Aggressive auto-restart disrupted stress tests; default to waiting.
        return False

    def full_check(self, expected_address: str = "",
                   wordpress_confirmed: bool = False) -> HealthResult:
        """Run all five health checks.

        Args:
            expected_address: Onion address to compare against the
                hostname file (warns on mismatch).
            wordpress_confirmed: If True, skip the local WordPress
                probe — WP responds reliably once it has come up
                inside Docker (Mac menubar's `_wordpress_confirmed`
                optimization). Caller should track this via
                `HealthMonitor.state.wordpress_confirmed` and reset
                it on system wake.

        Returns a HealthResult with all fields populated.
        """
        hr = HealthResult()

        # Check 1: WordPress local health. Once confirmed once, skip
        # subsequent probes — WP doesn't flap inside the container, and
        # the docker exec adds ~100ms per poll cycle for no information.
        if wordpress_confirmed:
            hr.wp_healthy = True
        else:
            hr.wp_healthy = self.check_wordpress_local()

        # Check 2: Tor bootstrap
        hr.tor_bootstrapped, hr.bootstrap_pct = self.check_tor_bootstrap()

        # Check 3: Hostname
        hr.onion_address = self.check_tor_hostname(expected_address)
        if not hr.onion_address:
            hr.errors.append("No onion address found")

        # Check 4: Internal connectivity (Tor → WordPress)
        if hr.wp_healthy and hr.tor_bootstrapped:
            internal = self.check_internal_connectivity()
            hr.tor_internally_ready = internal
            if not internal:
                hr.errors.append("WordPress not reachable from Tor container")

        # Check 5: External reachability (via onionheaven's independent Tor)
        if hr.tor_internally_ready and hr.onion_address:
            reachable, http_code = self.check_external_reachability(hr.onion_address)
            hr.tor_externally_reachable = reachable
            hr.external_http_code = http_code
            if not reachable:
                if http_code in ("000", ""):
                    hr.errors.append("Onion service not yet reachable through Tor network")
                else:
                    hr.errors.append(f"Onion service returned HTTP {http_code}")

        return hr


# -- Thresholds (from menubar.py) --

YELLOW_TO_STUCK_SECONDS = 300      # 5 min in yellow → display "stuck"
YELLOW_TO_RESTART_SECONDS = 120    # 2 min in yellow → eligible for restart
YELLOW_TO_WEDGE_WARNING_SECONDS = 600   # 10 min in yellow → emit wedge warning
RESTART_COOLDOWN_SECONDS = 300     # 5 min between auto-restarts
RECLAIM_RETRY_SECONDS = 60         # Retry OnionHeaven reclaim every 60s
POLL_READY_SECONDS = 30            # Poll interval when ready
POLL_OFFLINE_SECONDS = 10          # Poll interval when offline
POLL_STARTING_SECONDS = 5          # Poll interval when starting/stuck


@dataclass
class HealthState:
    """Mutable state tracked across health check cycles."""
    yellow_since: float | None = None  # Timestamp when entered yellow
    was_ready: bool = False            # Were we ever ready this session?
    wordpress_confirmed: bool = False  # WP responded at least once
    last_bootstrap_pct: int = 0
    bootstrap_stall_count: int = 0
    last_auto_restart: float = 0
    has_internet: bool = True
    tor_internally_ready: bool = False
    # OnionHeaven reclaim
    reclaim_succeeded: bool = False
    reclaim_in_flight: bool = False
    reclaim_last_attempt: float = 0
    # Wedge warning: one-shot per yellow episode. Reset when ready.
    wedge_warning_fired: bool = False


class HealthMonitor:
    """Stateful health monitor that tracks changes across check cycles.

    Call evaluate() with a HealthResult to get the current state and
    any restart decisions.
    """

    def __init__(self, log_func: Callable[[str], None] | None = None):
        self.state = HealthState()
        self.log_func = log_func

    def _log(self, msg: str) -> None:
        if self.log_func:
            self.log_func(msg)

    def evaluate(self, result: HealthResult, is_running: bool = True) -> ServiceState:
        """Evaluate a health check result and update state.

        Args:
            result: The latest HealthResult from HealthChecker.full_check().
            is_running: Whether the container stack is supposed to be running.

        Returns:
            The current ServiceState.
        """
        now = time.time()

        if not is_running:
            return ServiceState.STOPPED

        # Track WordPress confirmation
        if result.wp_healthy:
            self.state.wordpress_confirmed = True

        # Track internal readiness
        self.state.tor_internally_ready = result.tor_internally_ready

        if result.ready:
            # Fully operational
            if not self.state.was_ready:
                self._log("Service is now available")
            self.state.was_ready = True
            self.state.yellow_since = None
            self.state.bootstrap_stall_count = 0
            self.state.reclaim_succeeded = False
            self.state.reclaim_in_flight = False
            # Re-arm the wedge warning so the next yellow episode can fire it.
            self.state.wedge_warning_fired = False
            return ServiceState.AVAILABLE

        if self.state.was_ready and not result.ready:
            # Was ready, now degraded
            if self.state.yellow_since is None:
                self.state.yellow_since = now
                self.state.bootstrap_stall_count = 0
                self.state.last_auto_restart = 0  # Allow immediate restart
                self._log("Service became unreachable — reconnecting")

        # Track bootstrap progress
        if result.bootstrap_pct > self.state.last_bootstrap_pct:
            self.state.bootstrap_stall_count = 0
        elif result.bootstrap_pct == self.state.last_bootstrap_pct and result.bootstrap_pct > 0:
            self.state.bootstrap_stall_count += 1
        self.state.last_bootstrap_pct = result.bootstrap_pct

        if self.state.yellow_since is None:
            self.state.yellow_since = now

        # Determine display state
        if not self.state.has_internet:
            return ServiceState.OFFLINE

        yellow_duration = now - self.state.yellow_since if self.state.yellow_since else 0
        if yellow_duration > YELLOW_TO_STUCK_SECONDS:
            return ServiceState.STUCK

        return ServiceState.STARTING

    def should_restart_tor(self, tor_unhealthy: bool) -> bool:
        """Decide whether to auto-restart the Tor container.

        Args:
            tor_unhealthy: Result of HealthChecker.tor_container_unhealthy().

        Returns:
            True if Tor should be restarted.
        """
        now = time.time()

        if self.state.yellow_since is None:
            return False

        yellow_duration = now - self.state.yellow_since
        cooldown = now - self.state.last_auto_restart

        if (
            yellow_duration > YELLOW_TO_RESTART_SECONDS
            and cooldown > RESTART_COOLDOWN_SECONDS
            and tor_unhealthy
        ):
            self.state.last_auto_restart = now
            self._log("Tor container unhealthy after 2min — restarting")
            return True

        return False

    def should_emit_wedge_warning(self) -> bool:
        """Return True exactly once per yellow episode after 10+ min in yellow.

        Mirrors the Mac menubar's one-shot wedge hint (src/menubar.py
        check_status). The caller logs a platform-appropriate recovery
        message — on Linux that's `systemctl --user restart onionpress`
        or restarting Docker; on Mac it's killing Colima.

        Resets when the service becomes ready again (in evaluate()).
        """
        if self.state.wedge_warning_fired:
            return False
        if self.state.yellow_since is None:
            return False
        if (time.time() - self.state.yellow_since) <= YELLOW_TO_WEDGE_WARNING_SECONDS:
            return False
        self.state.wedge_warning_fired = True
        return True

    def should_reclaim(self) -> bool:
        """Decide whether to send OnionHeaven /online reclaim.

        Returns True if internally ready but self-check failing,
        and enough time has passed since last attempt.
        """
        now = time.time()

        if not self.state.tor_internally_ready:
            return False
        if self.state.reclaim_succeeded:
            return False
        if self.state.reclaim_in_flight:
            return False
        if (now - self.state.reclaim_last_attempt) < RECLAIM_RETRY_SECONDS:
            return False

        self.state.reclaim_in_flight = True
        self.state.reclaim_last_attempt = now
        return True

    def poll_interval(self, state: ServiceState) -> int:
        """Return the recommended poll interval in seconds."""
        if state == ServiceState.AVAILABLE:
            return POLL_READY_SECONDS
        if state == ServiceState.OFFLINE:
            return POLL_OFFLINE_SECONDS
        return POLL_STARTING_SECONDS

"""Health checks and restart decisions for OnionPress.

Extracts the health monitoring logic from menubar.py into a testable module.
The MenubarApp (or CLI) creates a HealthMonitor and calls check() periodically.
"""

import json
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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


# ---------------------------------------------------------------------------
# Tor-network probe (issue #266 dual probe)
# ---------------------------------------------------------------------------

# The probe fetches a static file that Apache serves straight from the
# WordPress image (app/Resources/docker/wordpress/onionpress-health.conf),
# never touching PHP. Fetching "/" ran WordPress, so a slow site could
# exhaust the curl budget on a perfectly healthy Tor path and register as
# rc=28 — the same code as dead intro points. Images older than the conf
# answer 404 on this path; the probe then falls back to "/" and remembers
# that for PROBE_STATIC_RECHECK_SECONDS.
PROBE_PATH = "/.onionpress-health"
PROBE_PATH_FALLBACK = "/"
PROBE_STATIC_RECHECK_SECONDS = 3600
# The static file carries this marker so a 200 from something that is not
# our Apache (a replica without the takeover header, a captive page) is
# not mistaken for the site being up.
PROBE_BODY_MARKER = "onionpress-health"
# First attempt: generous, a cold rendezvous can take ~15 s. The isolated
# retry after a timeout builds fresh circuits and only needs to prove the
# site answers at all, so it gets a shorter budget — it runs on the
# failure path, where every second delays the verdict.
PROBE_TIMEOUT_SECONDS = 30
PROBE_RETRY_TIMEOUT_SECONDS = 20
# Healthy probes log one structured `probe` line at most this often;
# unhealthy ones log every time (see HealthChecker._log_probe).
PROBE_LOG_HEALTHY_INTERVAL_SECONDS = 300

# Onions used to decide whether onionheaven's tor is itself able to reach
# anything, when it says our site is unreachable but our own tor disagrees.
# The hub is tried first; the others only when it does not answer, so
# third-party onions see traffic only while the hub is down. Any HTTP
# answer from any one of them proves the client has working circuits.
# Addresses verified 2026-10-11 on the owners' own pages:
#   torproject.org  — support.torproject.org/onionservices (v3 example)
#   duckduckgo.com  — duckduckgo.com/duckduckgo-help-pages/privacy/no-tracking
ONIONHEAVEN_HUB_ADDRESS = (
    "oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion"
)
WELL_KNOWN_ONIONS = (
    ("torproject", "2gzyxa5ihm7nsggfxnu52rck2vv4rvmdlkiu3zzui5du4xyclen53wid.onion"),
    ("duckduckgo", "duckduckgogg42xjoc72x3sjasowoarfbgcmvfimaftt6twagswzczad.onion"),
)


@dataclass
class ProbeResult:
    """One curl through a container's Tor SOCKS port.

    `ok` and `code` are what the old (reachable, http_code) tuple carried;
    iterating a ProbeResult yields exactly those two so existing callers
    keep working. The timings split the round trip where curl can see
    the seam: with socks5h, Tor answers the SOCKS CONNECT only once the
    stream is attached, i.e. after descriptor fetch, introduction and
    rendezvous — that is `tor_ms` (curl's time_pretransfer). The server's
    own time to first byte is `app_ms`. -1 means curl never got that far
    or the output carried no timings.
    """
    ok: bool
    code: str
    container: str = ""
    path: str = ""
    isolated: bool = False
    tor_ms: int = -1
    app_ms: int = -1
    total_ms: int = -1

    def __iter__(self):
        return iter((self.ok, self.code))


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

    def _control_getinfo(self, container: str, key: str) -> str:
        """GETINFO `key` on `container`'s control port; "" if it did not answer.

        Cookie auth over nc, the same way the bash launchers do it. Both
        tor containers (onionpress-tor, onionheaven) run the same image
        with ControlPort 9051 and CookieAuthentication 1.
        """
        result = self.docker.exec(
            container,
            ["sh", "-c",
             'printf "AUTHENTICATE $(cat /var/lib/tor/control_auth_cookie '
             '| od -A n -t x1 | tr -d \' \\n\')\\r\\n'
             f'GETINFO {key}\\r\\nQUIT\\r\\n"'
             ' | nc -w 5 127.0.0.1 9051'],
            timeout=15,
        )
        if not result.ok:
            return ""
        return result.output or ""

    def check_tor_bootstrap(self) -> tuple[bool, int]:
        """Check Tor bootstrap status via control port (with log fallback).

        Returns:
            (bootstrapped, percentage) — bootstrapped is True if 100%.
        """
        # Primary: query control port directly (reliable, not affected by log rotation)
        answer = self._control_getinfo("onionpress-tor", "status/bootstrap-phase")
        if "PROGRESS=" in answer:
            for part in answer.split():
                if part.startswith("PROGRESS="):
                    try:
                        pct = int(part.split("=")[1])
                        if pct < 100:
                            self._log(f"Checking Tor bootstrap status... {pct}%")
                        self._control_port_fallback_logged = False
                        return pct >= 100, pct
                    except ValueError:
                        pass

        # Fallback: parse container logs if the control port is unavailable.
        # Say so once: sleep/wake DEL_ONION/ADD_ONION, the watchdog's stall
        # recovery and OnionHeaven all depend on that port, so a silent
        # fallback here hides an outage of theirs.
        if not getattr(self, "_control_port_fallback_logged", False):
            self._log("Tor control port did not answer — reading bootstrap state from docker logs instead")
            self._control_port_fallback_logged = True
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

    # Cache onionheaven-tor-healthy result this long, since we only need
    # it on self✓/external✗ disagreement and don't want to re-probe the
    # hub on every health cycle.
    _ONIONHEAVEN_HEALTH_TTL_SECONDS = 60.0
    # Kept as class attributes for callers/tests that reach for them here.
    _ONIONHEAVEN_HUB_ADDRESS = ONIONHEAVEN_HUB_ADDRESS

    # ----- probe transport ------------------------------------------------

    def _probe_path(self) -> str:
        """The URL path to probe: the static file, or "/" while the
        WordPress image is known not to serve it."""
        until = getattr(self, "_static_probe_unsupported_until", 0.0)
        if time.time() < until:
            return PROBE_PATH_FALLBACK
        return PROBE_PATH

    def _curl_onion(
        self, container: str, onion_address: str, isolate: str | None = None,
        path: str = PROBE_PATH, timeout: int = PROBE_TIMEOUT_SECONDS,
    ) -> ProbeResult:
        """One curl through `container`'s SOCKS; see _probe_onion_via.

        `isolate` is a SOCKS username; any value forces Tor to serve the
        request on circuits of its own (stream isolation by auth).
        """
        # -D - dumps response headers to stdout; the body follows when we
        # ask for it (-o -), and -w appends "---STATUS---<code> <timings>"
        # at the very end. The delimiter must be something that can't
        # appear in an RFC-compliant HTTP header or in the health file.
        argv = [
            "curl", "-s", "--max-time", str(timeout),
            "--socks5-hostname", "127.0.0.1:9050",
        ]
        if isolate:
            argv += ["--proxy-user", f"{isolate}:x"]
        # The static health file is a few bytes and carries the identity
        # marker, so fetch it; "/" is a whole WordPress page we never read.
        argv += ["-o", "-" if path != PROBE_PATH_FALLBACK else "/dev/null"]
        argv += [
            "-D", "-",
            "-w", ("\n---STATUS---%{http_code} %{time_pretransfer} "
                   "%{time_starttransfer} %{time_total}"),
            "-H", "User-Agent: OnionPress-HealthCheck",
            f"http://{onion_address}{path}",
        ]
        pr = ProbeResult(ok=False, code="", container=container, path=path,
                         isolated=bool(isolate))
        result = self.docker.exec(container, argv, timeout=timeout + 15)
        if not result.ok:
            pr.code = f"000:rc={result.returncode}"
            return pr
        raw = result.output or ""
        headers_part, _, tail = raw.rpartition("---STATUS---")
        fields = tail.split() if headers_part else [raw.strip()]
        pr.code = fields[0] if fields else ""
        if len(fields) >= 4:
            try:
                pre, start, total = (float(fields[1]), float(fields[2]),
                                     float(fields[3]))
                pr.tor_ms = round(pre * 1000)
                pr.app_ms = max(round((start - pre) * 1000), 0)
                pr.total_ms = round(total * 1000)
            except ValueError:
                pass
        body = ""
        if headers_part:
            # Case-insensitive header match; curl echoes headers verbatim.
            head, sep, body = headers_part.partition("\r\n\r\n")
            if not sep:
                head, _, body = headers_part.partition("\n\n")
            for line in head.splitlines():
                name, _, value = line.partition(":")
                if (name.strip().lower() == "x-onionheaven-takeover"
                        and value.strip() == "1"):
                    pr.code = "takeover"
                    return pr
        pr.ok = pr.code in ("200", "301")
        # A real 200 for the static file that lacks the marker did not come
        # from our Apache. Only a full response (headers present) can be
        # judged; legacy "200"-only output has no body to look at.
        if (pr.ok and pr.code == "200" and path == PROBE_PATH and headers_part
                and PROBE_BODY_MARKER not in body):
            pr.ok = False
            pr.code = "foreign"
        return pr

    def _curl_site(
        self, container: str, onion_address: str, isolate: str | None = None,
        timeout: int = PROBE_TIMEOUT_SECONDS,
    ) -> ProbeResult:
        """_curl_onion against our own site, with the static-path fallback.

        A 404 on the static path means a WordPress image older than
        onionpress-health.conf: probe "/" instead, now and for the next
        hour, so an app update never outruns the image it is paired with.
        """
        path = self._probe_path()
        pr = self._curl_onion(container, onion_address, isolate, path, timeout)
        if path == PROBE_PATH and pr.code == "404":
            self._static_probe_unsupported_until = (
                time.time() + PROBE_STATIC_RECHECK_SECONDS)
            self._log(
                f"Reachability: {PROBE_PATH} is 404 via {container} (WordPress "
                f"image predates the health file) — probing / instead for "
                f"{PROBE_STATIC_RECHECK_SECONDS // 60} min"
            )
            pr = self._curl_onion(container, onion_address, isolate,
                                  PROBE_PATH_FALLBACK, timeout)
        return pr

    def _probe_onion_via(self, container: str, onion_address: str) -> ProbeResult:
        """Curl our onion address through `container`'s local SOCKS (127.0.0.1:9050).

        Returns a ProbeResult (iterable as (reachable, http_code)). 200/301
        from the real site counts as reachable; anything else (including
        302 from the OnionHeaven takeover redirector) does not. Also
        inspects response headers for `X-OnionHeaven-Takeover: 1` — if
        present, we're talking to the hub's takeover replica regardless
        of status code, and we return reachable=False with the sentinel
        http_code "takeover" so the caller can log + account for it
        cleanly. Curl transport failure returns "000:rc=<exit>".

        A curl timeout (rc=28) is retried once on fresh circuits. Tor
        reuses an open rendezvous circuit to the same service for new
        streams, and a rendezvous that joined but never passes data
        (seen 2026-10-11 on onionpress-tor right after a cold start:
        HSCR_JOINED, every stream on it timed out for minutes while a
        fresh visitor got a 301) poisons every later probe until Tor
        gives up on it. Per-probe SOCKS auth credentials put the retry
        in its own isolation group (IsolateSOCKSAuth is on by default),
        so it has to build its own circuits. Only the retry is isolated:
        the healthy path keeps the cheap warm-circuit reuse.
        """
        if not onion_address:
            return ProbeResult(ok=False, code="", container=container)
        pr = self._curl_site(container, onion_address)
        if pr.code == "000:rc=28":
            pr2 = self._curl_site(container, onion_address,
                                  isolate=self._isolation_nonce(),
                                  timeout=PROBE_RETRY_TIMEOUT_SECONDS)
            if pr2.ok:
                self._log(
                    f"Reachability: probe via {container} timed out but "
                    f"succeeded on fresh circuits — stale rendezvous circuit"
                )
            return pr2
        return pr

    @staticmethod
    def _isolation_nonce() -> str:
        return f"onionpress-probe-{int(time.time() * 1000)}-{threading.get_ident()}"

    def check_self_reachability(self, onion_address: str) -> ProbeResult:
        """Probe our onion through onionpress-tor's own SOCKS.

        Confirmed by timing (15s cold, 1.7s warm) that C Tor routes
        self-connections through real circuits rather than a local
        shortcut — so this is a genuine Tor-network probe, just using
        the same tor instance that also hosts the service.
        """
        return self._probe_onion_via("onionpress-tor", onion_address)

    # ----- is the external vantage point itself working? ------------------

    def check_onionheaven_tor_healthy(self) -> bool:
        """Is onionheaven's tor able to reach any onion at all?

        Used to disambiguate self✓/external✗ disagreement in
        check_external_reachability. Cached for 60s since onionheaven
        health changes on a container-lifetime scale, not a single
        health-cycle scale.

        Three rungs, cheapest first:
          1. `GETINFO status/circuit-established` on onionheaven's control
             port. A 0 is a definite no — no curl can succeed — and costs
             no Tor traffic. Anything else is inconclusive.
          2. The OnionHeaven hub.
          3. The WELL_KNOWN_ONIONS, one at a time, only if the hub did not
             answer. Before this the hub was the single oracle: with the
             hub down, every external failure was downgraded to
             "degraded" and reported as reachable, so wedge detection
             switched itself off fleet-wide, silently.
        Any HTTP answer at all (even a 503) proves the circuits work.
        """
        now = time.time()
        cached = getattr(self, "_onionheaven_health_cache", None)
        if cached and (now - cached[0]) < self._ONIONHEAVEN_HEALTH_TTL_SECONDS:
            return cached[1]
        ok = self._onionheaven_can_reach_anything()
        self._onionheaven_health_cache = (now, ok)
        return ok

    def _onionheaven_can_reach_anything(self) -> bool:
        answer = self._control_getinfo("onionheaven", "status/circuit-established")
        if "circuit-established=0" in answer:
            self._log("Reachability: onionheaven tor has no established circuits")
            return False
        for name, onion in (("hub", ONIONHEAVEN_HUB_ADDRESS),) + WELL_KNOWN_ONIONS:
            pr = self._curl_onion("onionheaven", onion, path=PROBE_PATH_FALLBACK)
            reached = bool(pr.code) and not pr.code.startswith("000") \
                and pr.code != "takeover"
            if reached:
                if name != "hub":
                    self._log(
                        f"Reachability: onionheaven tor cannot reach the "
                        f"OnionHeaven hub but reached {name} ({pr.code}) — "
                        f"treating its verdicts as trustworthy"
                    )
                return True
        return False

    # ----- the dual probe ---------------------------------------------------

    def check_external_reachability(self, onion_address: str) -> tuple[bool, str]:
        """Check if our onion service is reachable through the Tor network.

        Dual-probe with disambiguation:
          - Probe via onionheaven (independent tor, separate circuits).
            If it succeeds, we're reachable — done. This is the hot path
            and costs exactly one curl.
          - If it fails, the rest runs concurrently rather than one
            30 s curl after another: the isolated retry of the external
            probe (only after a timeout), the self-probe via
            onionpress-tor (our own tor — self-connection via real
            circuits, with its own retry), and the "is onionheaven's tor
            itself working" check. Serially this path took up to ~150 s
            inside a 30 s poll loop; now it is bounded by the slowest leg.
          - If the external retry succeeds, we're reachable (stale
            rendezvous circuit, logged).
          - If the self-probe fails too, we're genuinely down.
          - If self✓ and external✗, disambiguate: is onionheaven's tor
            itself working? If it can't reach any onion either, the
            external verdict is untrustworthy — report reachable with
            code "degraded:<ext>". Otherwise report unreachable (likely
            a descriptor-publish issue, which is invisible to our own
            tor but real for outside visitors).

        Every call records its legs in `self.last_probe` and logs one
        structured `probe ...` line (see _log_probe).

        Returns (reachable, http_code). Only 200/301 from at least one
        probe counts as reachable; 302 from external means OnionHeaven
        takeover (still unreachable from our POV).
        """
        if not onion_address:
            return False, ""

        ext = self._curl_site("onionheaven", onion_address)
        if ext.ok:
            self._log_probe("reachable", ext=ext)
            return True, ext.code

        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="probe") as pool:
            f_retry = None
            if ext.code == "000:rc=28":
                f_retry = pool.submit(
                    self._curl_site, "onionheaven", onion_address,
                    self._isolation_nonce(), PROBE_RETRY_TIMEOUT_SECONDS)
            f_self = pool.submit(self._probe_onion_via, "onionpress-tor", onion_address)
            f_hub = pool.submit(self.check_onionheaven_tor_healthy)
            ext_retry = f_retry.result() if f_retry else None
            self_pr = f_self.result()
            hub_ok = f_hub.result()

        if ext_retry is not None:
            if ext_retry.ok:
                self._log(
                    "Reachability: probe via onionheaven timed out but "
                    "succeeded on fresh circuits — stale rendezvous circuit"
                )
                self._log_probe("reachable", ext=ext, ext_retry=ext_retry,
                                self_pr=self_pr, hub_ok=hub_ok)
                return True, ext_retry.code
            ext = ext_retry

        if not self_pr.ok:
            # Both failed — genuinely unreachable. Prefer ext_code since
            # existing callers know how to format onionheaven failures.
            self._log_probe("unreachable", ext=ext, ext_retry=ext_retry,
                            self_pr=self_pr, hub_ok=hub_ok)
            return False, ext.code

        # Disagreement: self says yes, external says no. Is the external
        # probe trustworthy?
        if not hub_ok:
            self._log(
                f"Reachability: onionheaven tor is itself unhealthy — "
                f"trusting self-probe (ext={ext.code})"
            )
            self._log_probe("degraded", ext=ext, ext_retry=ext_retry,
                            self_pr=self_pr, hub_ok=hub_ok)
            return True, f"degraded:ext={ext.code}"

        # External probe is healthy and still can't reach us. That's a
        # real problem for outside visitors (likely descriptor-publish
        # failure) even though our own tor can rendezvous to itself.
        self._log(
            f"Reachability: onionheaven healthy but can't reach us "
            f"(ext={ext.code}, self={self_pr.code}) — likely descriptor issue"
        )
        self._log_probe("unreachable", ext=ext, ext_retry=ext_retry,
                        self_pr=self_pr, hub_ok=hub_ok)
        return False, ext.code

    def _log_probe(
        self, verdict: str, ext: ProbeResult,
        ext_retry: ProbeResult | None = None,
        self_pr: ProbeResult | None = None,
        hub_ok: bool | None = None,
    ) -> None:
        """One key=value line per probe, for fleet log mining.

        The #266 analysis was regexes over free-text lines; this gives it
        a stable record: verdict, every leg's code, and the Tor-path vs
        application split of the latency (tor_ms = until the SOCKS
        stream attached, app_ms = server time to first byte). Healthy
        probes are rate-limited to one line per
        PROBE_LOG_HEALTHY_INTERVAL_SECONDS so the ready-state log stays
        quiet; anything else logs every cycle.
        """
        now = time.time()
        record = {"verdict": verdict, "path": ext.path or PROBE_PATH}
        legs = [("ext", ext), ("ext_retry", ext_retry), ("self", self_pr)]
        for name, pr in legs:
            if pr is None:
                continue
            record[name] = pr.code or "-"
            if pr.tor_ms >= 0:
                record[f"{name}_tor_ms"] = pr.tor_ms
                record[f"{name}_app_ms"] = pr.app_ms
            elif pr.total_ms >= 0:
                record[f"{name}_ms"] = pr.total_ms
        if hub_ok is not None:
            record["hub"] = "ok" if hub_ok else "down"
        self.last_probe = record
        if verdict == "reachable":
            last = getattr(self, "_last_healthy_probe_log", 0.0)
            if now - last < PROBE_LOG_HEALTHY_INTERVAL_SECONDS:
                return
            self._last_healthy_probe_log = now
        self._log("probe " + " ".join(f"{k}={v}" for k, v in record.items()))

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
# Outcome-verified descriptor republish (issue #266, plan item 1). The
# app's dual probe is the only component that measures real reachability
# through the Tor network; when it fails continuously for this long while
# Tor is internally healthy, the HSDirs are holding a descriptor whose intro
# points no longer answer for us (seen 2026-10-11: INTRODUCE_ACK nack
# reason 1 on every intro point, service throttled from re-uploading for
# another hour). Fleet data in #266: healthy propagation clears in < 5 min
# (p50 2.5 min), so 10 min never fires on a benign publish.
REPUBLISH_AFTER_UNREACHABLE_SECONDS = 600
# At most one request per cooldown so a genuinely dead Tor network can't
# turn into DEL_ONION/ADD_ONION churn. The watchdog enforces its own
# cooldown too; this one just keeps the host side quiet.
REPUBLISH_COOLDOWN_SECONDS = 900
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


class RepublishTrigger:
    """Decide when to ask the tor-watchdog for a forced descriptor republish.

    Pure policy, no I/O: feed it one external-probe outcome per health
    cycle via observe() and it returns True on the cycle where a request
    should be sent. Shared by the Mac menubar and the Linux service so
    both platforms fire on the same evidence.

    A cycle counts towards the failure window only when all of these hold:
      - Tor is internally ready (bootstrapped, hostname present, WordPress
        reachable from the tor container) — otherwise the problem isn't
        descriptor publication and a republish can't help;
      - the dual probe said unreachable;
      - the code is in the wedge family: "000", "000:rc=28" (dead intro
        circuits), "000:rc=97" (descriptor missing/unusable). Takeover
        codes ("takeover", "302") are excluded on purpose: during an
        OnionHeaven takeover the heartbeat's /online is the reclaim path,
        and republishing would just fight the hub's replica every cooldown.
    Any other outcome resets the window.
    """

    def __init__(
        self,
        failing_for: float = REPUBLISH_AFTER_UNREACHABLE_SECONDS,
        cooldown: float = REPUBLISH_COOLDOWN_SECONDS,
    ):
        self.failing_for = failing_for
        self.cooldown = cooldown
        self.failing_since: float | None = None
        self.last_requested: float | None = None
        self.last_code: str = ""

    @staticmethod
    def is_wedge_code(http_code: str | None) -> bool:
        """True for the probe outcomes a republish can repair."""
        return str(http_code or "").startswith("000")

    def observe(
        self,
        now: float,
        internally_ready: bool,
        reachable: bool | None,
        http_code: str | None,
    ) -> bool:
        """Record one probe outcome. Returns True when a request is due.

        The caller decides whether to send the request or only log that
        it would have (REPUBLISH_ON_UNREACHABLE=no); either way the
        cooldown starts, so log-only mode reports the same cadence the
        live mode would act on.
        """
        wedged = (
            bool(internally_ready)
            and reachable is False
            and self.is_wedge_code(http_code)
        )
        if not wedged:
            self.failing_since = None
            return False
        self.last_code = str(http_code or "")
        if self.failing_since is None:
            self.failing_since = now
            return False
        if (now - self.failing_since) < self.failing_for:
            return False
        if (self.last_requested is not None
                and (now - self.last_requested) < self.cooldown):
            return False
        self.last_requested = now
        return True

    def reset(self) -> None:
        """Forget the current failure window (not the cooldown).

        For cycles where no probe ran at all — no internet, stack stopped —
        so an offline gap never counts towards the window.
        """
        self.failing_since = None

    def failing_seconds(self, now: float) -> int:
        """How long the current wedge window has lasted (0 if none)."""
        if self.failing_since is None:
            return 0
        return int(now - self.failing_since)


class HealthMonitor:
    """Stateful health monitor that tracks changes across check cycles.

    Call evaluate() with a HealthResult to get the current state and
    any restart decisions.
    """

    def __init__(self, log_func: Callable[[str], None] | None = None):
        self.state = HealthState()
        self.log_func = log_func
        self.republish = RepublishTrigger()

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

    def should_request_republish(self, result: HealthResult) -> bool:
        """Feed this cycle's dual-probe outcome to the republish trigger.

        Returns True on the cycle where the caller should ask the tor
        container's watchdog for a forced DEL_ONION/ADD_ONION republish
        (see launcher_ops.request_watchdog_republish). Mirrors what the
        Mac menubar does inline in check_status() with the same
        RepublishTrigger.
        """
        return self.republish.observe(
            time.time(),
            result.tor_internally_ready,
            result.tor_externally_reachable,
            result.external_http_code,
        )

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

"""Tests for src/onionpress/health.py."""

import os
import re
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from onionpress.docker import DockerResult
from onionpress.health import (
    HealthChecker, HealthResult, HealthMonitor, HealthState,
    ServiceState, SICK_PATTERNS, HEALTHY_PATTERNS, WedgeSignals,
    YELLOW_TO_STUCK_SECONDS, YELLOW_TO_RESTART_SECONDS,
    RESTART_COOLDOWN_SECONDS, RECLAIM_RETRY_SECONDS,
    POLL_READY_SECONDS, POLL_STARTING_SECONDS, POLL_OFFLINE_SECONDS,
    WEDGE_LOAD_WARN, WEDGE_LOAD_ALARM, WEDGE_FAILING_STREAK_ALARM,
    REPUBLISH_AFTER_UNREACHABLE_SECONDS, REPUBLISH_COOLDOWN_SECONDS,
    RepublishTrigger, decode_curl_reason,
    PROBE_PATH, PROBE_PATH_FALLBACK, PROBE_STATIC_RECHECK_SECONDS,
    PROBE_TIMEOUT_SECONDS, PROBE_RETRY_TIMEOUT_SECONDS,
    PROBE_LOG_HEALTHY_INTERVAL_SECONDS, ONIONHEAVEN_HUB_ADDRESS,
    WELL_KNOWN_ONIONS, REPUBLISH_AFTER_UNREACHABLE_FAST_SECONDS,
    REPUBLISH_FAST_CLASSES, REPUBLISH_MIN_FAILURE_RATIO,
    REPUBLISH_RECOVERY_STREAK, WEDGE_CLASSIFY_TTL_SECONDS,
)


def _ok(stdout="", stderr=""):
    return DockerResult(returncode=0, stdout=stdout, stderr=stderr)


def _fail(stderr="error", code=1):
    return DockerResult(returncode=code, stdout="", stderr=stderr)


class TestHealthResult(unittest.TestCase):
    def test_ready_when_all_checks_pass(self):
        hr = HealthResult(wp_healthy=True, tor_externally_reachable=True)
        self.assertTrue(hr.ready)

    def test_not_ready_missing_wp(self):
        hr = HealthResult(wp_healthy=False, tor_externally_reachable=True)
        self.assertFalse(hr.ready)

    def test_not_ready_missing_tor(self):
        hr = HealthResult(wp_healthy=True, tor_externally_reachable=False)
        self.assertFalse(hr.ready)

    def test_defaults(self):
        hr = HealthResult()
        self.assertFalse(hr.ready)
        self.assertEqual(hr.errors, [])

    def test_unknown_reachability_defaults_to_none_not_false(self):
        # Reachability is tri-state: a HealthResult that never ran Check 5
        # must be distinguishable from one that ran it and got a negative
        # answer — status.json (and any consumer of onion_reachable) treats
        # None as "unknown", never as "confirmed unreachable".
        hr = HealthResult()
        self.assertIsNone(hr.tor_externally_reachable)
        self.assertIsNone(hr.external_http_code)
        self.assertFalse(hr.ready)  # None must not satisfy `ready`

    def test_not_ready_when_reachability_unknown(self):
        hr = HealthResult(wp_healthy=True, tor_externally_reachable=None)
        self.assertFalse(hr.ready)


class TestCheckWordpressLocal(unittest.TestCase):
    def test_healthy(self):
        docker = mock.Mock()
        docker.exec.return_value = _ok("<html>WordPress</html>")
        hc = HealthChecker(docker)
        self.assertTrue(hc.check_wordpress_local())

    def test_database_error(self):
        docker = mock.Mock()
        docker.exec.return_value = _ok("Error establishing a database connection")
        hc = HealthChecker(docker)
        self.assertFalse(hc.check_wordpress_local())

    def test_unreachable(self):
        docker = mock.Mock()
        docker.exec.return_value = _fail()
        hc = HealthChecker(docker)
        self.assertFalse(hc.check_wordpress_local())


class TestCheckWordpressExternal(unittest.TestCase):
    """check_wordpress_external shells out to host curl — patch subprocess.run."""

    def _result(self, returncode=0, stdout=""):
        r = mock.Mock()
        r.returncode = returncode
        r.stdout = stdout
        return r

    def test_healthy(self):
        hc = HealthChecker(mock.Mock())
        with mock.patch("onionpress.health.subprocess.run",
                        return_value=self._result(0, "<html>WordPress</html>")):
            self.assertTrue(hc.check_wordpress_external(8080, log=False))

    def test_database_error_in_body(self):
        """A 200 OK with a DB-error body is NOT healthy — this is the onionheaven bug fix."""
        hc = HealthChecker(mock.Mock())
        with mock.patch("onionpress.health.subprocess.run",
                        return_value=self._result(0, "Error establishing a database connection")):
            self.assertFalse(hc.check_wordpress_external(8080, log=False))

    def test_alternate_database_error_in_body(self):
        hc = HealthChecker(mock.Mock())
        with mock.patch("onionpress.health.subprocess.run",
                        return_value=self._result(0, "Database connection error")):
            self.assertFalse(hc.check_wordpress_external(8080, log=False))

    def test_curl_exit_code_nonzero(self):
        hc = HealthChecker(mock.Mock())
        with mock.patch("onionpress.health.subprocess.run",
                        return_value=self._result(7, "")):
            self.assertFalse(hc.check_wordpress_external(8080, log=False))

    def test_subprocess_exception(self):
        hc = HealthChecker(mock.Mock())
        with mock.patch("onionpress.health.subprocess.run",
                        side_effect=OSError("boom")):
            self.assertFalse(hc.check_wordpress_external(8080, log=False))

    def test_logs_on_success(self):
        logs = []
        hc = HealthChecker(mock.Mock(), log_func=logs.append)
        with mock.patch("onionpress.health.subprocess.run",
                        return_value=self._result(0, "<html>")):
            hc.check_wordpress_external(8080, log=True)
        self.assertTrue(any("Checking local access" in l for l in logs))
        self.assertTrue(any("WordPress responding" in l for l in logs))

    def test_silent_when_log_false(self):
        logs = []
        hc = HealthChecker(mock.Mock(), log_func=logs.append)
        with mock.patch("onionpress.health.subprocess.run",
                        return_value=self._result(0, "<html>")):
            hc.check_wordpress_external(8080, log=False)
        self.assertEqual(logs, [])


class TestCheckTorBootstrap(unittest.TestCase):
    # check_tor_bootstrap first tries the control-port via docker.exec; if
    # that doesn't return a parseable PROGRESS line it falls back to
    # docker.run logs. These tests exercise the log fallback, so exec is
    # stubbed to fail.

    def test_100_percent(self):
        docker = mock.Mock()
        docker.exec.return_value = _fail()
        docker.run.return_value = _ok("Bootstrapped 100% (done): Done")
        hc = HealthChecker(docker)
        bootstrapped, pct = hc.check_tor_bootstrap()
        self.assertTrue(bootstrapped)
        self.assertEqual(pct, 100)

    def test_partial(self):
        docker = mock.Mock()
        docker.exec.return_value = _fail()
        docker.run.return_value = _ok("PROGRESS=50 TAG=loading")
        hc = HealthChecker(docker)
        bootstrapped, pct = hc.check_tor_bootstrap()
        self.assertFalse(bootstrapped)
        self.assertEqual(pct, 50)

    def test_no_logs(self):
        docker = mock.Mock()
        docker.exec.return_value = _fail()
        docker.run.return_value = _fail()
        hc = HealthChecker(docker)
        bootstrapped, pct = hc.check_tor_bootstrap()
        self.assertFalse(bootstrapped)
        self.assertEqual(pct, 0)

    def test_control_port_primary(self):
        """Control-port probe short-circuits the log-based fallback."""
        docker = mock.Mock()
        docker.exec.return_value = _ok("250-status/bootstrap-phase=NOTICE BOOTSTRAP PROGRESS=100 TAG=done")
        docker.run.side_effect = AssertionError("should not fall through to logs")
        hc = HealthChecker(docker)
        bootstrapped, pct = hc.check_tor_bootstrap()
        self.assertTrue(bootstrapped)
        self.assertEqual(pct, 100)


HOSTNAME = "op2" + "a" * 53 + ".onion"


class TestCheckTorHostname(unittest.TestCase):
    def test_returns_address(self):
        docker = mock.Mock()
        docker.exec.return_value = _ok(HOSTNAME + "\n")
        hc = HealthChecker(docker)
        self.assertEqual(hc.check_tor_hostname(), HOSTNAME)

    def test_mismatch_logs_warning(self):
        docker = mock.Mock()
        different = "dif" + "b" * 53 + ".onion"
        docker.exec.return_value = _ok(different + "\n")
        logs = []
        hc = HealthChecker(docker, log_func=logs.append)
        addr = hc.check_tor_hostname(expected_address=HOSTNAME)
        self.assertEqual(addr, different)
        self.assertTrue(any("mismatch" in l for l in logs))

    def test_anything_but_a_whole_address_reads_as_none(self):
        # The address goes into probe URLs and control-port commands.
        for content in ("evil.example/" + "a" * 39 + ".onion",
                        "op2abc.onion",
                        HOSTNAME.upper(),
                        "x\r\nSIGNAL HALT\r\n" + HOSTNAME):
            with self.subTest(content=content):
                docker = mock.Mock()
                docker.exec.return_value = _ok(content + "\n")
                logs = []
                hc = HealthChecker(docker, log_func=logs.append)
                self.assertEqual(hc.check_tor_hostname(), "")
                self.assertTrue(any("valid onion address" in l for l in logs))


class TestCheckInternalConnectivity(unittest.TestCase):
    def test_reachable(self):
        docker = mock.Mock()
        docker.exec.return_value = _ok()
        hc = HealthChecker(docker)
        self.assertTrue(hc.check_internal_connectivity())

    def test_unreachable(self):
        docker = mock.Mock()
        docker.exec.return_value = _fail()
        hc = HealthChecker(docker)
        self.assertFalse(hc.check_internal_connectivity())


class TestCheckInternetConnectivity(unittest.TestCase):
    """Interface-scan based check — no TCC-gated API calls (the Mac's;
    Linux reads the routing table: tests/test_linux_daemon.py)."""

    def setUp(self):
        from onionpress.platform import OS
        p = mock.patch("onionpress.platform.detect_os", return_value=OS.MACOS)
        p.start()
        self.addCleanup(p.stop)

    def _run(self, stdout, returncode=0):
        return mock.Mock(returncode=returncode, stdout=stdout)

    def test_real_en0_has_internet(self):
        ifconfig = (
            "lo0: flags=8049<UP,LOOPBACK> mtu 16384\n"
            "\tinet 127.0.0.1 netmask 0xff000000\n"
            "en0: flags=8863<UP,BROADCAST> mtu 1500\n"
            "\tinet 192.168.1.42 netmask 0xffffff00\n"
        )
        with mock.patch("onionpress.health.subprocess.run",
                        return_value=self._run(ifconfig)):
            self.assertTrue(HealthChecker.check_internet_connectivity())

    def test_only_loopback_means_offline(self):
        ifconfig = (
            "lo0: flags=8049<UP,LOOPBACK> mtu 16384\n"
            "\tinet 127.0.0.1 netmask 0xff000000\n"
            "en0: flags=8863<UP,BROADCAST> mtu 1500\n"
            "\tether aa:bb:cc:dd:ee:ff\n"
        )
        with mock.patch("onionpress.health.subprocess.run",
                        return_value=self._run(ifconfig)):
            self.assertFalse(HealthChecker.check_internet_connectivity())

    def test_ifconfig_failure_assumes_connected(self):
        """Don't block the app if ifconfig can't run for some reason."""
        with mock.patch("onionpress.health.subprocess.run",
                        return_value=self._run("", returncode=1)):
            self.assertTrue(HealthChecker.check_internet_connectivity())

    def test_subprocess_exception_assumes_connected(self):
        with mock.patch("onionpress.health.subprocess.run",
                        side_effect=OSError("boom")):
            self.assertTrue(HealthChecker.check_internet_connectivity())


def _resp(code, body="onionpress-health ok", takeover=False, timings="0.900 1.100 1.200"):
    """A full curl response as _curl_onion sees it: headers, body, -w tail."""
    headers = f"HTTP/1.1 {code} X\r\nContent-Type: text/plain\r\n"
    if takeover:
        headers += "X-OnionHeaven-Takeover: 1\r\n"
    return _ok(f"{headers}\r\n{body}\n---STATUS---{code} {timings}")


def _dispatch(table):
    """docker.exec side_effect keyed on what the probe asked for.

    The failure path of check_external_reachability runs its legs
    concurrently, so a positional side_effect list is racy. Keys are
    (container, "ctl") for a control-port GETINFO, else
    (container, "<host><path>[:iso]") for a curl, ":iso" when the call
    carries --proxy-user. A list value is consumed one call at a time.
    """
    calls = []

    def side_effect(container, argv, timeout=30, **kw):
        if argv[0] == "sh":
            # Specific first ("ctl:GETINFO hs/client/desc/id", "ctl:HSFETCH",
            # ...), then the generic ("ctl") answer for everything else.
            m = re.search(r"(HSFETCH|GETINFO) (\S+?)\\r", argv[2])
            cmd = ""
            if m and m.group(1) == "HSFETCH":
                cmd = "HSFETCH"                   # drop the address
            elif m:
                cmd = f"GETINFO {m.group(2)}"
                if cmd.startswith("GETINFO hs/"):
                    cmd = cmd.rsplit("/", 1)[0]   # drop the address
            specific = (container, "ctl:" + cmd) if cmd else None
            key = specific if specific in table else (container, "ctl")
            if specific and specific not in table and key not in table:
                key = specific
        else:
            url = argv[-1]
            kind = url.split("//", 1)[1] + (":iso" if "--proxy-user" in argv else "")
            key = (container, kind)
        calls.append((key, list(argv)))
        if key not in table:
            raise AssertionError(f"unexpected docker.exec {key}")
        val = table[key]
        if isinstance(val, list):
            return val.pop(0)
        return val

    side_effect.calls = calls
    return side_effect


SITE = "op2abc.onion"
STATIC = f"{SITE}{PROBE_PATH}"
ROOT = f"{SITE}/"
HUB = f"{ONIONHEAVEN_HUB_ADDRESS}/"
TORPROJECT = f"{WELL_KNOWN_ONIONS[0][1]}/"
DUCKDUCKGO = f"{WELL_KNOWN_ONIONS[1][1]}/"
CIRCUITS_UP = _ok("250-status/circuit-established=1\r\n250 OK")
CIRCUITS_DOWN = _ok("250-status/circuit-established=0\r\n250 OK")

OURS_DESC = _ok(
    "250+hs/service/desc/id/op2abc=\r\nhs-descriptor 3\r\ndescriptor-lifetime 180\r\n"
    "descriptor-signing-key-cert\r\n-----BEGIN ED25519 CERT-----\r\nAQgABvkAAAAA\r\nBBBB\r\n"
    "-----END ED25519 CERT-----\r\nrevision-counter 5575074206\r\nsuperencrypted\r\n.\r\n250 OK")


def _client_desc(cert="AQgABvkAAAAABBBB", rev=5575074206):
    return _ok(
        "250+hs/client/desc/id/op2abc=\r\nhs-descriptor 3\r\ndescriptor-lifetime 180\r\n"
        f"descriptor-signing-key-cert\r\n-----BEGIN ED25519 CERT-----\r\n{cert}\r\n"
        f"-----END ED25519 CERT-----\r\nrevision-counter {rev}\r\nsuperencrypted\r\n.\r\n250 OK")


FETCH_RECEIVED = _ok(
    "250 OK\r\n250 OK\r\n250 OK\r\n"
    "650 HS_DESC REQUESTED op2abc NO_AUTH $8416B5F0~Relay05 uqayzFnb HSDIR_INDEX=A\r\n"
    "650 HS_DESC RECEIVED op2abc NO_AUTH $8416B5F0~Relay05 uqayzFnb")
FETCH_NOT_FOUND = _ok(
    "250 OK\r\n250 OK\r\n250 OK\r\n"
    "650 HS_DESC REQUESTED op2abc NO_AUTH $8416B5F0~Relay05 uqayzFnb HSDIR_INDEX=A\r\n"
    "650 HS_DESC FAILED op2abc NO_AUTH $8416B5F0~Relay05 uqayzFnb REASON=NOT_FOUND\r\n"
    "650 HS_DESC REQUESTED op2abc NO_AUTH $C0FFEE~Relay06 uqayzFnb HSDIR_INDEX=B\r\n"
    "650 HS_DESC FAILED op2abc NO_AUTH $C0FFEE~Relay06 uqayzFnb REASON=NOT_FOUND")



# What the classifier asks on every wedge-code failure path; merged into
# dual-probe tests so its control-port sessions have answers.
CLASSIFY_TABLE = {
    ("onionpress-tor", "ctl:GETINFO hs/service/desc/id"): OURS_DESC,
    ("onionheaven", "ctl:HSFETCH"): FETCH_RECEIVED,
    ("onionheaven", "ctl:GETINFO hs/client/desc/id"): _client_desc(),
}



class TestCheckExternalReachability(unittest.TestCase):
    def test_reachable_external_succeeds_fast_path(self):
        # Hot path: onionheaven returns 200, we short-circuit without probing self.
        docker = mock.Mock()
        docker.exec.return_value = _resp("200")
        hc = HealthChecker(docker)
        reachable, code = hc.check_external_reachability(SITE)
        self.assertTrue(reachable)
        self.assertEqual(code, "200")
        # Exactly one docker.exec — we didn't waste probes.
        self.assertEqual(docker.exec.call_count, 1)

    def test_unreachable_both_fail(self):
        # External fails, self fails → genuinely unreachable.
        docker = mock.Mock()
        docker.exec.side_effect = _dispatch({
            ("onionheaven", STATIC): _fail(code=97),
            ("onionpress-tor", STATIC): _fail(code=97),
            ("onionheaven", "ctl"): CIRCUITS_UP,
            ("onionheaven", HUB): _resp("200", body="hub"),
            **CLASSIFY_TABLE,
        })
        hc = HealthChecker(docker)
        reachable, code = hc.check_external_reachability(SITE)
        self.assertFalse(reachable)
        self.assertEqual(code, "000:rc=97")

    def test_reachable_self_ok_onionheaven_sick(self):
        # Today's yellow-state bug: onionheaven's tor stuck, our tor fine.
        # External probe fails, self probe succeeds, onionheaven can't
        # reach the hub or any well-known onion either → trust self,
        # report reachable.
        docker = mock.Mock()
        docker.exec.side_effect = _dispatch({
            ("onionheaven", STATIC): _ok("000"),   # curl "ok" but HTTP 000
            ("onionpress-tor", STATIC): _resp("301"),
            ("onionheaven", "ctl"): _fail(),
            ("onionheaven", HUB): _fail(),
            ("onionheaven", TORPROJECT): _fail(),
            ("onionheaven", DUCKDUCKGO): _fail(),
            **CLASSIFY_TABLE,
        })
        hc = HealthChecker(docker)
        reachable, code = hc.check_external_reachability(SITE)
        self.assertTrue(reachable)
        self.assertTrue(code.startswith("degraded:"))

    def test_unreachable_self_ok_onionheaven_healthy(self):
        # Descriptor-publish failure case: our tor reaches its own HS,
        # onionheaven's tor is working fine (can reach the hub), yet
        # onionheaven can't reach us → we're dark to outside visitors.
        docker = mock.Mock()
        docker.exec.side_effect = _dispatch({
            ("onionheaven", STATIC): _ok("000"),
            ("onionpress-tor", STATIC): _resp("200"),
            ("onionheaven", "ctl"): CIRCUITS_UP,
            ("onionheaven", HUB): _resp("200", body="hub"),
            **CLASSIFY_TABLE,
        })
        hc = HealthChecker(docker)
        reachable, _code = hc.check_external_reachability(SITE)
        self.assertFalse(reachable)

    def test_failure_path_runs_legs_concurrently_with_short_retry(self):
        # ext times out → the isolated external retry, the self-probe and
        # the onionheaven health check all run at once; the retry gets the
        # shorter budget. Serially this was up to ~150 s per cycle.
        docker = mock.Mock()
        table = {
            ("onionheaven", STATIC): _fail(code=28),
            ("onionheaven", STATIC + ":iso"): _resp("200"),
            ("onionpress-tor", STATIC): _resp("301"),
            ("onionheaven", "ctl"): CIRCUITS_UP,
            ("onionheaven", HUB): _resp("200", body="hub"),
            **CLASSIFY_TABLE,
        }
        docker.exec.side_effect = _dispatch(table)
        hc = HealthChecker(docker)
        reachable, code = hc.check_external_reachability(SITE)
        self.assertTrue(reachable)
        self.assertEqual(code, "200")
        keys = [k for k, _ in docker.exec.side_effect.calls]
        self.assertIn(("onionheaven", STATIC + ":iso"), keys)
        self.assertIn(("onionpress-tor", STATIC), keys)
        self.assertIn(("onionheaven", "ctl"), keys)
        first = next(a for k, a in docker.exec.side_effect.calls if k == ("onionheaven", STATIC))
        retry = next(a for k, a in docker.exec.side_effect.calls if k == ("onionheaven", STATIC + ":iso"))
        self.assertEqual(first[first.index("--max-time") + 1], str(PROBE_TIMEOUT_SECONDS))
        self.assertEqual(retry[retry.index("--max-time") + 1], str(PROBE_RETRY_TIMEOUT_SECONDS))
        self.assertEqual(hc.last_probe["ext_retry"], "200")

    def test_timeout_is_retried_once_on_isolated_circuits(self):
        # 2026-10-11: a joined-but-dead rendezvous circuit made every
        # probe through onionpress-tor time out for minutes while fresh
        # circuits reached the site at once. rc=28 → one retry with a
        # per-probe SOCKS username so Tor can't reuse the stale circuit.
        docker = mock.Mock()
        docker.exec.side_effect = [
            _fail(code=28),  # plain probe: curl timeout
            _resp("301"),    # isolated retry: success
        ]
        hc = HealthChecker(docker)
        reachable, code = hc._probe_onion_via("onionpress-tor", SITE)
        self.assertTrue(reachable)
        self.assertEqual(code, "301")
        self.assertEqual(docker.exec.call_count, 2)
        first = docker.exec.call_args_list[0].args[1]
        retry = docker.exec.call_args_list[1].args[1]
        self.assertNotIn("--proxy-user", first)
        self.assertIn("--proxy-user", retry)
        user = retry[retry.index("--proxy-user") + 1]
        self.assertTrue(user.startswith("onionpress-probe-"), user)
        self.assertTrue(user.endswith(":x"), user)

    def test_timeout_twice_is_reported_as_timeout(self):
        docker = mock.Mock()
        docker.exec.side_effect = [_fail(code=28), _fail(code=28)]
        hc = HealthChecker(docker)
        reachable, code = hc._probe_onion_via("onionpress-tor", SITE)
        self.assertFalse(reachable)
        self.assertEqual(code, "000:rc=28")
        self.assertEqual(docker.exec.call_count, 2)

    def test_socks_failure_is_not_retried(self):
        # rc=97 is Tor refusing the SOCKS connect (descriptor missing or
        # unusable); fresh circuits can't change that — same client cache.
        docker = mock.Mock()
        docker.exec.side_effect = [_fail(code=97)]
        hc = HealthChecker(docker)
        reachable, code = hc._probe_onion_via("onionpress-tor", SITE)
        self.assertFalse(reachable)
        self.assertEqual(code, "000:rc=97")
        self.assertEqual(docker.exec.call_count, 1)

    def test_empty_address(self):
        docker = mock.Mock()
        hc = HealthChecker(docker)
        reachable, code = hc.check_external_reachability("")
        self.assertFalse(reachable)
        docker.exec.assert_not_called()

    def test_onionheaven_health_is_cached(self):
        # Caching: two disagreement cycles within 60s should run the
        # onionheaven health check exactly once.
        docker = mock.Mock()
        docker.exec.side_effect = _dispatch({
            ("onionheaven", STATIC): _ok("000"),
            ("onionpress-tor", STATIC): _resp("301"),
            ("onionheaven", "ctl"): _fail(),
            ("onionheaven", HUB): _fail(),
            ("onionheaven", TORPROJECT): _fail(),
            ("onionheaven", DUCKDUCKGO): _fail(),
            **CLASSIFY_TABLE,
        })
        hc = HealthChecker(docker)
        r1, _ = hc.check_external_reachability(SITE)
        r2, _ = hc.check_external_reachability(SITE)
        self.assertTrue(r1)
        self.assertTrue(r2)
        keys = [k for k, _ in docker.exec.side_effect.calls]
        self.assertEqual(keys.count(("onionheaven", HUB)), 1)
        self.assertEqual(keys.count(("onionheaven", "ctl")), 1)
        self.assertEqual(keys.count(("onionheaven", STATIC)), 2)


class TestProbeTransport(unittest.TestCase):
    """_curl_onion: the static health target, timings, identity, fallback."""

    def test_probes_the_static_file_and_splits_the_latency(self):
        docker = mock.Mock()
        docker.exec.return_value = _resp("200", timings="0.900 1.100 1.200")
        hc = HealthChecker(docker)
        pr = hc._curl_onion("onionheaven", SITE)
        self.assertTrue(pr.ok)
        self.assertEqual(pr.code, "200")
        self.assertEqual((pr.tor_ms, pr.app_ms, pr.total_ms), (900, 200, 1200))
        argv = docker.exec.call_args.args[1]
        self.assertEqual(argv[-1], f"http://{STATIC}")
        # The static file is fetched (it carries the marker); "/" never is.
        self.assertEqual(argv[argv.index("-o") + 1], "-")
        self.assertEqual(argv[argv.index("--max-time") + 1], "30")
        # Tuple unpacking still works for older callers.
        ok, code = pr
        self.assertEqual((ok, code), (True, "200"))

    def test_legacy_bare_status_output_still_parses(self):
        docker = mock.Mock()
        docker.exec.return_value = _ok("200")
        hc = HealthChecker(docker)
        pr = hc._curl_onion("onionheaven", SITE)
        self.assertTrue(pr.ok)
        self.assertEqual((pr.tor_ms, pr.app_ms), (-1, -1))

    def test_static_file_without_marker_is_not_our_site(self):
        docker = mock.Mock()
        docker.exec.return_value = _resp("200", body="<html>Welcome to the captive portal</html>")
        hc = HealthChecker(docker)
        pr = hc._curl_onion("onionheaven", SITE)
        self.assertFalse(pr.ok)
        self.assertEqual(pr.code, "foreign")
        # ...and "foreign" is not a wedge code: no republish for it.
        self.assertFalse(RepublishTrigger.is_wedge_code("foreign"))

    def test_takeover_header_wins_on_the_static_path(self):
        docker = mock.Mock()
        docker.exec.return_value = _resp("302", body="", takeover=True)
        hc = HealthChecker(docker)
        pr = hc._curl_onion("onionheaven", SITE)
        self.assertFalse(pr.ok)
        self.assertEqual(pr.code, "takeover")

    def test_404_falls_back_to_root_and_remembers(self):
        # An older WordPress image has no /.onionpress-health: the probe
        # must keep working against it, now and for the next hour.
        docker = mock.Mock()
        docker.exec.side_effect = _dispatch({
            ("onionheaven", STATIC): _resp("404", body="Not Found"),
            ("onionheaven", ROOT): _resp("301", body=""),
        })
        logs = []
        hc = HealthChecker(docker, log_func=logs.append)
        pr = hc._curl_site("onionheaven", SITE)
        self.assertTrue(pr.ok)
        self.assertEqual(pr.code, "301")
        self.assertEqual(pr.path, PROBE_PATH_FALLBACK)
        root_argv = next(a for k, a in docker.exec.side_effect.calls if k == ("onionheaven", ROOT))
        self.assertEqual(root_argv[root_argv.index("-o") + 1], "/dev/null")
        self.assertTrue(any("404" in m and "/.onionpress-health" in m for m in logs), logs)
        # Second probe goes straight to "/".
        hc._curl_site("onionheaven", SITE)
        keys = [k for k, _ in docker.exec.side_effect.calls]
        self.assertEqual(keys, [("onionheaven", STATIC), ("onionheaven", ROOT),
                                ("onionheaven", ROOT)])
        self.assertEqual(hc._probe_path(), PROBE_PATH_FALLBACK)
        # ...until the recheck window passes.
        with mock.patch("onionpress.health.time.time",
                        return_value=time.time() + PROBE_STATIC_RECHECK_SECONDS + 1):
            self.assertEqual(hc._probe_path(), PROBE_PATH)


class TestOnionheavenTorHealthy(unittest.TestCase):
    """The external vantage point must be checked against more than one oracle."""

    def test_no_circuits_is_a_definite_no_without_any_curl(self):
        docker = mock.Mock()
        docker.exec.side_effect = _dispatch({("onionheaven", "ctl"): CIRCUITS_DOWN})
        hc = HealthChecker(docker)
        self.assertFalse(hc.check_onionheaven_tor_healthy())
        self.assertEqual(docker.exec.call_count, 1)

    def test_hub_down_but_well_known_onion_answers(self):
        # Before: hub down ⇒ every external failure downgraded to
        # "degraded" ⇒ wedge detection silently off fleet-wide.
        docker = mock.Mock()
        docker.exec.side_effect = _dispatch({
            ("onionheaven", "ctl"): CIRCUITS_UP,
            ("onionheaven", HUB): _fail(code=28),
            ("onionheaven", TORPROJECT): _resp("200", body="Tor Project"),
        })
        logs = []
        hc = HealthChecker(docker, log_func=logs.append)
        self.assertTrue(hc.check_onionheaven_tor_healthy())
        keys = [k for k, _ in docker.exec.side_effect.calls]
        self.assertNotIn(("onionheaven", DUCKDUCKGO), keys)  # stopped at first answer
        self.assertTrue(any("torproject" in m for m in logs), logs)

    def test_third_party_onions_see_no_traffic_while_the_hub_answers(self):
        docker = mock.Mock()
        docker.exec.side_effect = _dispatch({
            ("onionheaven", "ctl"): CIRCUITS_UP,
            ("onionheaven", HUB): _resp("503", body="maintenance"),
        })
        hc = HealthChecker(docker)
        # Any HTTP answer proves the circuits work, even a 503.
        self.assertTrue(hc.check_onionheaven_tor_healthy())
        self.assertEqual(docker.exec.call_count, 2)

    def test_nothing_answers(self):
        docker = mock.Mock()
        docker.exec.side_effect = _dispatch({
            ("onionheaven", "ctl"): _ok("250 OK"),   # no circuit-established line
            ("onionheaven", HUB): _fail(code=28),
            ("onionheaven", TORPROJECT): _ok("000"),
            ("onionheaven", DUCKDUCKGO): _fail(code=97),
        })
        hc = HealthChecker(docker)
        self.assertFalse(hc.check_onionheaven_tor_healthy())
        self.assertEqual(docker.exec.call_count, 4)


class TestProbeLogLine(unittest.TestCase):
    """One structured `probe` line per cycle, for fleet mining."""

    def test_unreachable_logs_every_leg(self):
        docker = mock.Mock()
        docker.exec.side_effect = _dispatch({
            ("onionheaven", STATIC): _fail(code=28),
            ("onionheaven", STATIC + ":iso"): _fail(code=28),
            ("onionpress-tor", STATIC): _resp("301", timings="1.500 1.680 1.700"),
            ("onionheaven", "ctl"): CIRCUITS_UP,
            ("onionheaven", HUB): _resp("200", body="hub"),
            **CLASSIFY_TABLE,
        })
        logs = []
        hc = HealthChecker(docker, log_func=logs.append)
        reachable, code = hc.check_external_reachability(SITE)
        self.assertFalse(reachable)
        line = next(m for m in logs if m.startswith("probe "))
        fields = dict(kv.split("=", 1) for kv in line[len("probe "):].split())
        self.assertEqual(fields["verdict"], "unreachable")
        self.assertEqual(fields["ext"], "000:rc=28")
        self.assertEqual(fields["ext_retry"], "000:rc=28")
        self.assertEqual(fields["self"], "301")
        self.assertEqual(fields["self_tor_ms"], "1500")
        self.assertEqual(fields["self_app_ms"], "180")
        self.assertEqual(fields["hub"], "ok")
        self.assertEqual(fields["path"], PROBE_PATH)
        self.assertEqual(hc.last_probe["verdict"], "unreachable")

    def test_healthy_probes_are_rate_limited(self):
        docker = mock.Mock()
        docker.exec.return_value = _resp("200")
        logs = []
        hc = HealthChecker(docker, log_func=logs.append)
        with mock.patch("onionpress.health.time.time", return_value=1000.0):
            hc.check_external_reachability(SITE)
            hc.check_external_reachability(SITE)
        with mock.patch("onionpress.health.time.time",
                        return_value=1000.0 + PROBE_LOG_HEALTHY_INTERVAL_SECONDS + 1):
            hc.check_external_reachability(SITE)
        probe_lines = [m for m in logs if m.startswith("probe ")]
        self.assertEqual(len(probe_lines), 2)
        self.assertIn("verdict=reachable", probe_lines[0])
        self.assertIn("ext_tor_ms=900", probe_lines[0])
        # The record is still refreshed on every call, logged or not.
        self.assertEqual(hc.last_probe["ext"], "200")


class TestTorContainerUnhealthy(unittest.TestCase):
    def test_sick_patterns(self):
        for pattern in SICK_PATTERNS:
            docker = mock.Mock()
            docker.run.return_value = _ok(f"some log\n{pattern}\nmore log")
            hc = HealthChecker(docker)
            self.assertTrue(hc.tor_container_unhealthy(), f"Should be unhealthy for: {pattern}")

    def test_healthy_pattern(self):
        docker = mock.Mock()
        docker.run.return_value = _ok("Sufficiently bootstrapped to build circuits")
        hc = HealthChecker(docker)
        self.assertFalse(hc.tor_container_unhealthy())

    def test_no_patterns_returns_healthy(self):
        docker = mock.Mock()
        docker.run.return_value = _ok("some random log output")
        hc = HealthChecker(docker)
        # No clear signals → don't restart (conservative approach)
        self.assertFalse(hc.tor_container_unhealthy())

    def test_log_failure_returns_unhealthy(self):
        docker = mock.Mock()
        docker.run.return_value = _fail()
        hc = HealthChecker(docker)
        self.assertTrue(hc.tor_container_unhealthy())


class TestFullCheck(unittest.TestCase):
    def test_all_healthy(self):
        docker = mock.Mock()
        docker.exec.side_effect = [
            _ok("<html>"),           # check_wordpress_local
            _fail(),                 # check_tor_bootstrap control-port probe → falls back to docker.run
            _ok(HOSTNAME + "\n"),   # check_tor_hostname
            _ok(),                   # check_internal_connectivity
            _ok("200"),              # check_external_reachability
        ]
        docker.run.return_value = _ok("Bootstrapped 100% (done)")
        hc = HealthChecker(docker)
        hr = hc.full_check()
        self.assertTrue(hr.ready)
        self.assertTrue(hr.wp_healthy)
        self.assertTrue(hr.tor_bootstrapped)
        self.assertTrue(hr.tor_internally_ready)
        self.assertTrue(hr.tor_externally_reachable)
        self.assertEqual(hr.onion_address, HOSTNAME)

    def test_wp_down_skips_later_checks(self):
        docker = mock.Mock()
        docker.exec.side_effect = [
            _fail(),               # check_wordpress_local
            _fail(),               # check_tor_bootstrap control-port probe
            _ok(HOSTNAME + "\n"), # check_tor_hostname
        ]
        docker.run.return_value = _ok("Bootstrapped 100%")
        hc = HealthChecker(docker)
        hr = hc.full_check()
        self.assertFalse(hr.ready)
        self.assertFalse(hr.tor_internally_ready)
        # Check 5 never ran (gated on tor_internally_ready) — the
        # regression this guards is write_status() reading this as a
        # confirmed-unreachable `false` instead of "never asked".
        self.assertIsNone(hr.tor_externally_reachable)
        self.assertIsNone(hr.external_http_code)

    def test_missing_onion_address_skips_check_5_leaves_reachability_unknown(self):
        # Checks 1-4 can all pass with no onion_address yet (e.g. hostname
        # file present but check_tor_hostname returned "" for some other
        # reason) — full_check's Check 5 gate requires BOTH
        # tor_internally_ready and onion_address, so this exercises the
        # other half of that gate.
        docker = mock.Mock()
        docker.exec.side_effect = [
            _ok("<html>"),   # check_wordpress_local
            _fail(),         # check_tor_bootstrap control-port probe
            _ok(""),         # check_tor_hostname → empty address
            _ok(),           # check_internal_connectivity
        ]
        docker.run.return_value = _ok("Bootstrapped 100% (done)")
        hc = HealthChecker(docker)
        hr = hc.full_check()
        self.assertTrue(hr.tor_internally_ready)
        self.assertEqual(hr.onion_address, "")
        self.assertIsNone(hr.tor_externally_reachable)
        self.assertIsNone(hr.external_http_code)
        self.assertFalse(hr.ready)


class TestHealthMonitorEvaluate(unittest.TestCase):
    def test_stopped(self):
        hm = HealthMonitor()
        state = hm.evaluate(HealthResult(), is_running=False)
        self.assertEqual(state, ServiceState.STOPPED)

    def test_available(self):
        hm = HealthMonitor()
        hr = HealthResult(wp_healthy=True, tor_externally_reachable=True)
        state = hm.evaluate(hr)
        self.assertEqual(state, ServiceState.AVAILABLE)
        self.assertTrue(hm.state.was_ready)
        self.assertIsNone(hm.state.yellow_since)

    def test_starting(self):
        hm = HealthMonitor()
        hr = HealthResult(wp_healthy=True, tor_externally_reachable=False, bootstrap_pct=50)
        state = hm.evaluate(hr)
        self.assertEqual(state, ServiceState.STARTING)
        self.assertIsNotNone(hm.state.yellow_since)

    def test_degraded_from_ready(self):
        hm = HealthMonitor()
        # First: available
        hm.evaluate(HealthResult(wp_healthy=True, tor_externally_reachable=True))
        # Then: degraded
        state = hm.evaluate(HealthResult(wp_healthy=True, tor_externally_reachable=False))
        self.assertEqual(state, ServiceState.STARTING)
        self.assertIsNotNone(hm.state.yellow_since)

    def test_stuck_after_timeout(self):
        hm = HealthMonitor()
        hm.state.yellow_since = time.time() - YELLOW_TO_STUCK_SECONDS - 1
        hr = HealthResult(wp_healthy=True, tor_externally_reachable=False, bootstrap_pct=50)
        state = hm.evaluate(hr)
        self.assertEqual(state, ServiceState.STUCK)

    def test_offline(self):
        hm = HealthMonitor()
        hm.state.has_internet = False
        hm.state.yellow_since = time.time()
        hr = HealthResult()
        state = hm.evaluate(hr)
        self.assertEqual(state, ServiceState.OFFLINE)

    def test_wordpress_confirmed_persists(self):
        hm = HealthMonitor()
        hm.evaluate(HealthResult(wp_healthy=True))
        self.assertTrue(hm.state.wordpress_confirmed)
        hm.evaluate(HealthResult(wp_healthy=False))
        self.assertTrue(hm.state.wordpress_confirmed)  # still True

    def test_bootstrap_stall_tracking(self):
        hm = HealthMonitor()
        hm.evaluate(HealthResult(bootstrap_pct=50))
        self.assertEqual(hm.state.last_bootstrap_pct, 50)
        self.assertEqual(hm.state.bootstrap_stall_count, 0)
        # Same percentage → stall
        hm.evaluate(HealthResult(bootstrap_pct=50))
        self.assertEqual(hm.state.bootstrap_stall_count, 1)
        # Progress → reset
        hm.evaluate(HealthResult(bootstrap_pct=75))
        self.assertEqual(hm.state.bootstrap_stall_count, 0)


class TestShouldRestartTor(unittest.TestCase):
    def test_no_restart_when_not_yellow(self):
        hm = HealthMonitor()
        self.assertFalse(hm.should_restart_tor(tor_unhealthy=True))

    def test_restart_after_thresholds(self):
        hm = HealthMonitor()
        hm.state.yellow_since = time.time() - YELLOW_TO_RESTART_SECONDS - 1
        hm.state.last_auto_restart = time.time() - RESTART_COOLDOWN_SECONDS - 1
        self.assertTrue(hm.should_restart_tor(tor_unhealthy=True))
        # Should update last_auto_restart
        self.assertGreater(hm.state.last_auto_restart, 0)

    def test_no_restart_during_cooldown(self):
        hm = HealthMonitor()
        hm.state.yellow_since = time.time() - YELLOW_TO_RESTART_SECONDS - 1
        hm.state.last_auto_restart = time.time()  # Just restarted
        self.assertFalse(hm.should_restart_tor(tor_unhealthy=True))

    def test_no_restart_if_healthy(self):
        hm = HealthMonitor()
        hm.state.yellow_since = time.time() - YELLOW_TO_RESTART_SECONDS - 1
        hm.state.last_auto_restart = time.time() - RESTART_COOLDOWN_SECONDS - 1
        self.assertFalse(hm.should_restart_tor(tor_unhealthy=False))


class TestShouldReclaim(unittest.TestCase):
    def test_reclaim_when_internally_ready(self):
        hm = HealthMonitor()
        hm.state.tor_internally_ready = True
        hm.state.reclaim_last_attempt = 0
        self.assertTrue(hm.should_reclaim())
        self.assertTrue(hm.state.reclaim_in_flight)

    def test_no_reclaim_not_internally_ready(self):
        hm = HealthMonitor()
        hm.state.tor_internally_ready = False
        self.assertFalse(hm.should_reclaim())

    def test_no_reclaim_already_succeeded(self):
        hm = HealthMonitor()
        hm.state.tor_internally_ready = True
        hm.state.reclaim_succeeded = True
        self.assertFalse(hm.should_reclaim())

    def test_no_reclaim_in_flight(self):
        hm = HealthMonitor()
        hm.state.tor_internally_ready = True
        hm.state.reclaim_in_flight = True
        self.assertFalse(hm.should_reclaim())

    def test_no_reclaim_too_soon(self):
        hm = HealthMonitor()
        hm.state.tor_internally_ready = True
        hm.state.reclaim_last_attempt = time.time()
        self.assertFalse(hm.should_reclaim())


class TestPollInterval(unittest.TestCase):
    def test_ready(self):
        hm = HealthMonitor()
        self.assertEqual(hm.poll_interval(ServiceState.AVAILABLE), POLL_READY_SECONDS)

    def test_offline(self):
        hm = HealthMonitor()
        self.assertEqual(hm.poll_interval(ServiceState.OFFLINE), POLL_OFFLINE_SECONDS)

    def test_starting(self):
        hm = HealthMonitor()
        self.assertEqual(hm.poll_interval(ServiceState.STARTING), POLL_STARTING_SECONDS)

    def test_stuck(self):
        hm = HealthMonitor()
        self.assertEqual(hm.poll_interval(ServiceState.STUCK), POLL_STARTING_SECONDS)


class TestCheckVMWedge(unittest.TestCase):
    """Validates the wedge probe against the Apr 2026 onionpress.org snapshot.

    Ground truth (captured live from the hung VM):
      - /proc/loadavg on tor container: "150.14 150.07 149.73 1/455 1832876"
      - docker inspect wordpress State.Health: Status="unhealthy", FailingStreak=32

    These cases ensure the probe would have detected that incident and
    that a healthy machine stays silent.
    """

    def _make_checker(self, loadavg_output, inspect_output):
        docker = mock.MagicMock()
        # `exec("onionpress-tor", ["cat", "/proc/loadavg"], ...)`
        docker.exec.return_value = _ok(stdout=loadavg_output) if loadavg_output else _fail()
        # `run(["inspect", ...])`
        docker.run.return_value = _ok(stdout=inspect_output) if inspect_output else _fail()
        return HealthChecker(docker)

    def test_wedged_machine_returns_alarm_signals(self):
        # Snapshot: load=150.14, wp unhealthy, streak=32
        checker = self._make_checker(
            "150.14 150.07 149.73 1/455 1832876\n",
            '{"Status":"unhealthy","FailingStreak":32,"Log":[]}',
        )
        s = checker.check_vm_wedge()
        self.assertAlmostEqual(s.loadavg_1min, 150.14, places=2)
        self.assertEqual(s.wp_health_status, "unhealthy")
        self.assertEqual(s.wp_failing_streak, 32)
        # Both thresholds should classify this as "alarm"
        self.assertGreaterEqual(s.loadavg_1min, WEDGE_LOAD_ALARM)
        self.assertGreaterEqual(s.wp_failing_streak, WEDGE_FAILING_STREAK_ALARM)

    def test_healthy_machine_is_below_thresholds(self):
        checker = self._make_checker(
            "0.10 0.05 0.01 1/200 12345\n",
            '{"Status":"healthy","FailingStreak":0,"Log":[]}',
        )
        s = checker.check_vm_wedge()
        self.assertLess(s.loadavg_1min, WEDGE_LOAD_WARN)
        self.assertEqual(s.wp_health_status, "healthy")
        self.assertEqual(s.wp_failing_streak, 0)

    def test_no_health_block_handled(self):
        # A container without a HEALTHCHECK directive returns "null"
        checker = self._make_checker("0.50 0.40 0.30 1/200 12345\n", "null")
        s = checker.check_vm_wedge()
        self.assertAlmostEqual(s.loadavg_1min, 0.50, places=2)
        self.assertIsNone(s.wp_health_status)
        self.assertIsNone(s.wp_failing_streak)

    def test_both_probes_fail_returns_none(self):
        docker = mock.MagicMock()
        docker.exec.return_value = _fail()
        docker.run.return_value = _fail()
        checker = HealthChecker(docker)
        self.assertIsNone(checker.check_vm_wedge())

    def test_malformed_loadavg_doesnt_crash(self):
        checker = self._make_checker(
            "garbage\n",
            '{"Status":"healthy","FailingStreak":0,"Log":[]}',
        )
        s = checker.check_vm_wedge()
        self.assertIsNone(s.loadavg_1min)
        self.assertEqual(s.wp_health_status, "healthy")

    def test_malformed_json_doesnt_crash(self):
        checker = self._make_checker("0.20 0.15 0.10 1/200 12345\n", "not json")
        s = checker.check_vm_wedge()
        self.assertAlmostEqual(s.loadavg_1min, 0.20, places=2)
        self.assertIsNone(s.wp_health_status)

    def test_probe_never_execs_into_wordpress(self):
        # Critical invariant: docker exec into a wedged WP container would
        # itself hang on fuse_lock_inode. The probe must only use
        # exec(onionpress-tor) and run(inspect).
        docker = mock.MagicMock()
        docker.exec.return_value = _ok(stdout="1.0 1.0 1.0 1/200 12345\n")
        docker.run.return_value = _ok(stdout='{"Status":"healthy","FailingStreak":0}')
        checker = HealthChecker(docker)
        checker.check_vm_wedge()
        for call in docker.exec.call_args_list:
            self.assertNotEqual(
                call.args[0], "onionpress-wordpress",
                "wedge probe must never exec into wordpress (would hang)",
            )


class TestDecodeCurlReason(unittest.TestCase):
    """decode_curl_reason maps '000rc=N' http_codes to human strings."""

    def test_known_codes(self):
        self.assertEqual(decode_curl_reason("000rc=6"),
                         "DNS resolution failed")
        self.assertEqual(decode_curl_reason("000rc=28"), "timeout (30s)")
        self.assertEqual(decode_curl_reason("000rc=97"),
                         "SOCKS handshake failed (descriptor not yet available)")

    def test_unknown_rc_falls_back_to_raw(self):
        self.assertEqual(decode_curl_reason("000rc=999"), "curl rc=999")

    def test_no_rc_suffix(self):
        # "000" with no rc= means curl gave us nothing to decode.
        self.assertEqual(decode_curl_reason("000"), "unknown")

    def test_empty_rc(self):
        # Pathological "rc=" with no number — treat as unknown rather
        # than crashing or returning a confusing "curl rc=".
        self.assertEqual(decode_curl_reason("000rc="), "unknown")


class TestRepublishTrigger(unittest.TestCase):
    """Issue #266 plan item 1: the outcome-verified republish trigger.

    Pure policy over (internally_ready, reachable, http_code) per cycle.
    Time is passed in, so these run instantly.
    """

    def _wedge(self, t, trig, code="000:rc=97", ready=True):
        return trig.observe(t, ready, False, code)

    def test_thresholds_match_issue_266_plan(self):
        # 10 min of continuous failure, at most one request per 15 min.
        self.assertEqual(REPUBLISH_AFTER_UNREACHABLE_SECONDS, 600)
        self.assertEqual(REPUBLISH_COOLDOWN_SECONDS, 900)

    def test_never_fires_before_the_failure_window(self):
        trig = RepublishTrigger(failing_for=600, cooldown=900)
        for t in range(0, 600, 30):
            self.assertFalse(self._wedge(t, trig), f"fired early at t={t}")

    def test_fires_once_window_elapsed_then_respects_cooldown(self):
        trig = RepublishTrigger(failing_for=600, cooldown=900)
        self._wedge(0, trig)
        self.assertTrue(self._wedge(600, trig))
        # Still failing: no second request inside the cooldown...
        for t in range(630, 1500, 30):
            self.assertFalse(self._wedge(t, trig), f"re-fired at t={t}")
        # ...but one more once it expires.
        self.assertTrue(self._wedge(1500, trig))

    def test_a_single_success_no_longer_resets_but_a_streak_does(self):
        # Review item 2: a descriptor with two of three intro points dead
        # fails ~2/3 of visitors and never fails continuously. One lucky
        # probe is not a recovery; REPUBLISH_RECOVERY_STREAK of them is.
        trig = RepublishTrigger(failing_for=600, cooldown=900)
        self._wedge(0, trig)
        self._wedge(300, trig)
        self.assertFalse(trig.observe(330, True, True, "301"))
        self.assertEqual(trig.failing_since, 0)          # window survives
        self._wedge(360, trig)
        self.assertTrue(self._wedge(600, trig))          # 4/5 failed, 600 s old
        trig2 = RepublishTrigger(failing_for=600, cooldown=900)
        self._wedge(0, trig2)
        for t in range(30, 30 * (REPUBLISH_RECOVERY_STREAK + 1), 30):
            self.assertFalse(trig2.observe(t, True, True, "301"))
        self.assertIsNone(trig2.failing_since)          # recovered
        self._wedge(400, trig2)
        self.assertFalse(self._wedge(900, trig2))        # new window, 500 s
        self.assertTrue(self._wedge(1000, trig2))

    def test_partial_failure_fires(self):
        # Alternating fail/pass every 30 s: the old rule reset on every
        # pass and never fired. Half the probes failing for 10 min is a
        # wedge for half the visitors.
        trig = RepublishTrigger(failing_for=600, cooldown=900)
        fired = []
        for i, t in enumerate(range(0, 601, 30)):
            if i % 2 == 0:
                fired.append((t, self._wedge(t, trig)))
            else:
                self.assertFalse(trig.observe(t, True, True, "301"))
        self.assertEqual([t for t, f in fired if f], [600])
        self.assertGreaterEqual(trig.failure_ratio(), REPUBLISH_MIN_FAILURE_RATIO)

    def test_below_the_failure_ratio_never_fires(self):
        trig = RepublishTrigger(failing_for=600, cooldown=900)
        for i, t in enumerate(range(0, 1201, 30)):
            if i % 3 == 0:
                self.assertFalse(self._wedge(t, trig), f"fired at t={t}")
            else:
                self.assertFalse(trig.observe(t, True, True, "301"))
        # Two passes then one fail: the recovery streak (5) is never met,
        # so the window persists, but 1/3 failed is below the ratio.
        self.assertLess(trig.failure_ratio(), REPUBLISH_MIN_FAILURE_RATIO)

    def test_fast_window_for_proven_publication_failures(self):
        # Review item 1: when every wedge sample says the HSDirs lack our
        # current descriptor, 5 min is enough — the benign propagation
        # cluster is shorter and the watchdog guards a fresh upload.
        for cls in REPUBLISH_FAST_CLASSES:
            trig = RepublishTrigger(failing_for=600, cooldown=900, fast_for=300)
            for t in range(0, 300, 30):
                self.assertFalse(trig.observe(t, True, False, "000:rc=97", cls), cls)
            self.assertTrue(trig.observe(300, True, False, "000:rc=97", cls), cls)
            self.assertEqual(trig.required_window(), 300)
        # Ambiguous classes keep the full window...
        for cls in ("desc-current", "desc-other", "fetch-error", "unknown", "", None):
            trig = RepublishTrigger(failing_for=600, cooldown=900, fast_for=300)
            for t in range(0, 600, 30):
                self.assertFalse(trig.observe(t, True, False, "000:rc=28", cls), cls)
            self.assertTrue(trig.observe(600, True, False, "000:rc=28", cls), cls)
        # ...and one ambiguous sample in the window disqualifies the fast path.
        trig = RepublishTrigger(failing_for=600, cooldown=900, fast_for=300)
        trig.observe(0, True, False, "000:rc=97", "desc-missing")
        trig.observe(30, True, False, "000:rc=28", "unknown")
        for t in range(60, 600, 30):
            self.assertFalse(trig.observe(t, True, False, "000:rc=97", "desc-missing"))
        self.assertEqual(trig.required_window(), 600)
        self.assertTrue(trig.observe(600, True, False, "000:rc=97", "desc-missing"))

    def test_describe_summarises_the_window(self):
        trig = RepublishTrigger(failing_for=600, cooldown=900)
        trig.observe(0, True, False, "000:rc=97", "desc-stale")
        trig.observe(30, True, True, "301")
        trig.observe(60, True, False, "000:rc=97", "desc-stale")
        self.assertEqual(trig.describe(90),
                         "unreachable 90s, 2/3 probes failed, http=000:rc=97, why=desc-stale")

    def test_requires_internal_readiness(self):
        # Hostname missing / not bootstrapped / WP down from tor: a
        # republish can't help, and the watchdog's own stall logic owns
        # those. Never counts towards the window.
        trig = RepublishTrigger(failing_for=600, cooldown=900)
        for t in range(0, 1200, 30):
            self.assertFalse(trig.observe(t, False, False, "000:rc=97"))
        self.assertIsNone(trig.failing_since)

    def test_only_the_wedge_code_family_counts(self):
        # rc=28 (dead intro circuits) and rc=97 (descriptor missing or
        # unusable) are the two families from the fleet data; a bare
        # "000" is a curl that got no HTTP status at all.
        for code in ("000", "000:rc=28", "000:rc=97", "000:rc=7"):
            trig = RepublishTrigger(failing_for=600, cooldown=900)
            self._wedge(0, trig, code=code)
            self.assertTrue(self._wedge(600, trig, code=code), code)
        # Takeover is the heartbeat's job; HTTP errors are WordPress's.
        for code in ("takeover", "302", "500", "503", "degraded:ext=000:rc=28", ""):
            trig = RepublishTrigger(failing_for=600, cooldown=900)
            self._wedge(0, trig, code=code)
            self.assertFalse(self._wedge(600, trig, code=code), code)
            self.assertIsNone(trig.failing_since, code)

    def test_unknown_reachability_does_not_count(self):
        # None = Check 5 never ran (e.g. no internet); not a verdict.
        trig = RepublishTrigger(failing_for=600, cooldown=900)
        self.assertFalse(trig.observe(0, True, None, None))
        self.assertFalse(trig.observe(600, True, None, None))
        self.assertIsNone(trig.failing_since)

    def test_reset_forgets_the_window_but_not_the_cooldown(self):
        trig = RepublishTrigger(failing_for=600, cooldown=900)
        self._wedge(0, trig)
        self.assertTrue(self._wedge(600, trig))
        trig.reset()                       # e.g. internet dropped
        self.assertIsNone(trig.failing_since)
        self._wedge(700, trig)             # back online, still wedged
        self.assertFalse(self._wedge(1300, trig))  # window ok, cooldown not
        self.assertTrue(self._wedge(1500, trig))

    def test_failing_seconds_reports_the_window(self):
        trig = RepublishTrigger(failing_for=600, cooldown=900)
        self.assertEqual(trig.failing_seconds(100), 0)
        self._wedge(100, trig)
        self.assertEqual(trig.failing_seconds(400), 300)
        trig.observe(401, True, True, "200")
        self.assertEqual(trig.failing_seconds(500), 400)   # one pass: still open
        trig.reset()
        self.assertEqual(trig.failing_seconds(500), 0)

    def test_thresholds_for_the_fast_window(self):
        self.assertEqual(REPUBLISH_AFTER_UNREACHABLE_FAST_SECONDS, 300)
        self.assertEqual(REPUBLISH_FAST_CLASSES, ("desc-missing", "desc-stale"))
        self.assertEqual(REPUBLISH_MIN_FAILURE_RATIO, 0.5)
        self.assertEqual(REPUBLISH_RECOVERY_STREAK, 5)


class TestRecoveryWindowRepublish(unittest.TestCase):
    """Both platforms' entry point (issue #480): reachability.RecoveryWindow
    feeds each cycle's outcome to its RepublishTrigger."""

    @staticmethod
    def _window():
        from onionpress.reachability import RecoveryWindow
        return RecoveryWindow(mock.Mock(), lambda m: None, lambda k, d: d)

    def test_window_owns_a_trigger_and_feeds_it_the_health_result(self):
        window = self._window()
        self.assertIsInstance(window.trigger, RepublishTrigger)
        hr = HealthResult(
            wp_healthy=True, tor_bootstrapped=True, bootstrap_pct=100,
            onion_address="op2abc.onion", tor_internally_ready=True,
            tor_externally_reachable=False, external_http_code="000:rc=28",
        )
        base = 1_000_000.0
        with mock.patch("onionpress.reachability.time.time", return_value=base):
            self.assertFalse(window.decide_result(hr))
        with mock.patch("onionpress.reachability.time.time", return_value=base + 599):
            self.assertFalse(window.decide_result(hr))
        with mock.patch("onionpress.reachability.time.time", return_value=base + 600):
            self.assertTrue(window.decide_result(hr))
        with mock.patch("onionpress.reachability.time.time", return_value=base + 601):
            self.assertFalse(window.decide_result(hr))

    def test_window_passes_the_wedge_class_to_the_trigger(self):
        window = self._window()
        hr = HealthResult(tor_internally_ready=True, tor_externally_reachable=False,
                          external_http_code="000:rc=97", wedge_class="desc-missing")
        with mock.patch("onionpress.reachability.time.time", return_value=0.0):
            window.decide_result(hr)
        self.assertEqual(window.trigger.last_class, "desc-missing")
        self.assertEqual(window.trigger.required_window(),
                         REPUBLISH_AFTER_UNREACHABLE_FAST_SECONDS)
        with mock.patch("onionpress.reachability.time.time", return_value=300.0):
            self.assertTrue(window.decide_result(hr))

    def test_a_healthy_result_resets(self):
        window = self._window()
        bad = HealthResult(tor_internally_ready=True,
                           tor_externally_reachable=False,
                           external_http_code="000:rc=97")
        good = HealthResult(wp_healthy=True, tor_internally_ready=True,
                            tor_externally_reachable=True,
                            external_http_code="301")
        with mock.patch("onionpress.reachability.time.time", return_value=0.0):
            window.decide_result(bad)
        with mock.patch("onionpress.reachability.time.time", return_value=300.0):
            self.assertFalse(window.decide_result(good))
        # Rate-based window: a pass is a sample, not a reset; the window
        # only ends after REPUBLISH_RECOVERY_STREAK passes in a row.
        self.assertEqual(window.trigger.failing_since, 0.0)
        for t in range(330, 330 + 30 * REPUBLISH_RECOVERY_STREAK, 30):
            with mock.patch("onionpress.reachability.time.time", return_value=float(t)):
                window.decide_result(good)
        self.assertIsNone(window.trigger.failing_since)


class TestClassifyWedge(unittest.TestCase):
    """Review item 1: ask both tors what they hold instead of guessing from curl."""

    def _hc(self, fetch, theirs, ours=OURS_DESC):
        docker = mock.Mock()
        table = {
            ("onionpress-tor", "ctl:GETINFO hs/service/desc/id"): ours,
            ("onionheaven", "ctl:HSFETCH"): fetch,
        }
        if theirs is not None:
            table[("onionheaven", "ctl:GETINFO hs/client/desc/id")] = theirs
        docker.exec.side_effect = _dispatch(table)
        return HealthChecker(docker), docker

    def test_descriptor_missing(self):
        hc, docker = self._hc(FETCH_NOT_FOUND, None)
        r = hc.classify_wedge("op2abc.onion")
        self.assertEqual(r["why"], "desc-missing")
        self.assertEqual(r["fetch"], "not-found")
        self.assertEqual(r["ours_rev"], 5575074206)
        self.assertIsNone(r["theirs_rev"])
        # No client-cache read when nothing was received.
        keys = [k for k, _ in docker.exec.side_effect.calls]
        self.assertNotIn(("onionheaven", "ctl:GETINFO hs/client/desc/id"), keys)

    def test_descriptor_stale(self):
        hc, _ = self._hc(FETCH_RECEIVED, _client_desc(rev=5575074000))
        r = hc.classify_wedge("op2abc.onion")
        self.assertEqual(r["why"], "desc-stale")
        self.assertEqual((r["ours_rev"], r["theirs_rev"]), (5575074206, 5575074000))

    def test_descriptor_current_means_intro_points(self):
        hc, _ = self._hc(FETCH_RECEIVED, _client_desc())
        self.assertEqual(hc.classify_wedge("op2abc.onion")["why"], "desc-current")

    def test_other_lineage_is_ambiguous(self):
        # A different signing cert: time-period skew or the incarnation
        # before the last DEL+ADD. Revision counters are not comparable
        # across lineages, so this must not become desc-stale.
        hc, _ = self._hc(FETCH_RECEIVED, _client_desc(cert="ZZZZOTHER", rev=1))
        self.assertEqual(hc.classify_wedge("op2abc.onion")["why"], "desc-other")

    def test_fetch_errors_are_not_evidence(self):
        for events, outcome in (
            (_ok("250 OK\r\n650 HS_DESC FAILED op2abc NO_AUTH $X y REASON=QUERY_NO_HSDIR"),
             "query-no-hsdir"),
            (_ok("250 OK\r\n250 OK\r\n250 OK"), "no-events"),
            (_fail(), "no-events"),
        ):
            hc, _ = self._hc(events, None)
            r = hc.classify_wedge("op2abc.onion")
            self.assertEqual(r["why"], "fetch-error", outcome)
            self.assertEqual(r["fetch"], outcome)

    def test_unknown_when_our_tor_does_not_answer(self):
        hc, _ = self._hc(FETCH_RECEIVED, _client_desc(), ours=_fail())
        r = hc.classify_wedge("op2abc.onion")
        self.assertEqual(r["why"], "unknown")
        self.assertIsNone(r["ours_rev"])

    def test_cached_per_address(self):
        hc, docker = self._hc(FETCH_NOT_FOUND, None)
        with mock.patch("onionpress.health.time.time", return_value=1000.0):
            hc.classify_wedge("op2abc.onion")
            hc.classify_wedge("op2abc.onion")
        n = docker.exec.call_count
        with mock.patch("onionpress.health.time.time",
                        return_value=1000.0 + WEDGE_CLASSIFY_TTL_SECONDS + 1):
            hc.classify_wedge("op2abc.onion")
        self.assertEqual(docker.exec.call_count, 2 * n)

    def test_fetch_session_waits_for_events(self):
        hc, docker = self._hc(FETCH_NOT_FOUND, None)
        hc.classify_wedge("op2abc.onion")
        argv = next(a for k, a in docker.exec.side_effect.calls if k == ("onionheaven", "ctl:HSFETCH"))
        self.assertIn("SETEVENTS HS_DESC\\r\\nHSFETCH op2abc\\r\\n", argv[2])
        self.assertIn("; sleep 20; printf \"QUIT", argv[2])

    def test_failure_path_classifies_and_logs_why(self):
        docker = mock.Mock()
        docker.exec.side_effect = _dispatch({
            ("onionheaven", STATIC): _fail(code=97),
            ("onionpress-tor", STATIC): _resp("301"),
            ("onionheaven", "ctl:GETINFO status/circuit-established"): CIRCUITS_UP,
            ("onionheaven", HUB): _resp("200", body="hub"),
            ("onionpress-tor", "ctl:GETINFO hs/service/desc/id"): OURS_DESC,
            ("onionheaven", "ctl:HSFETCH"): FETCH_NOT_FOUND,
        })
        logs = []
        hc = HealthChecker(docker, log_func=logs.append)
        reachable, code = hc.check_external_reachability(SITE)
        self.assertFalse(reachable)
        self.assertEqual(hc.last_probe["why"], "desc-missing")
        line = next(m for m in logs if m.startswith("probe "))
        self.assertIn("why=desc-missing", line)
        self.assertIn("fetch=not-found", line)
        self.assertIn("ours_rev=5575074206", line)
        # full_check carries it to the trigger.
        hr = HealthResult(tor_internally_ready=True)
        with mock.patch.object(hc, "check_wordpress_local", return_value=True), \
             mock.patch.object(hc, "check_tor_bootstrap", return_value=(True, 100)), \
             mock.patch.object(hc, "check_tor_hostname", return_value=SITE), \
             mock.patch.object(hc, "check_internal_connectivity", return_value=True), \
             mock.patch.object(hc, "check_external_reachability", return_value=(False, "000:rc=97")):
            hr = hc.full_check()
        self.assertEqual(hr.wedge_class, "desc-missing")

    def test_no_classification_for_non_wedge_codes(self):
        # Takeover/HTTP errors are not the HSDirs' doing: no HSFETCH.
        docker = mock.Mock()
        docker.exec.side_effect = _dispatch({
            ("onionheaven", STATIC): _resp("302", body="", takeover=True),
            ("onionpress-tor", STATIC): _resp("301"),
            ("onionheaven", "ctl"): CIRCUITS_UP,
            ("onionheaven", HUB): _resp("200", body="hub"),
        })
        hc = HealthChecker(docker)
        reachable, code = hc.check_external_reachability(SITE)
        self.assertEqual(code, "takeover")
        keys = [k for k, _ in docker.exec.side_effect.calls]
        self.assertNotIn(("onionheaven", "ctl:HSFETCH"), keys)
        self.assertNotIn("why", hc.last_probe)


class TestWakeResetsRepublishWindow(unittest.TestCase):
    """Issue #266: a pre-sleep failure window must not survive a wake.

    RepublishTrigger counts wall-clock time. A probe that failed just
    before a long sleep would otherwise make the first failing probe
    after wake look like 10+ min of unreachability and fire DEL+ADD on
    top of the watchdog's own wake ADD. Both platforms reset the window
    in their wake handler; this pins that in the source since neither
    handler is importable without a desktop session.
    """

    ROOT = os.path.join(os.path.dirname(__file__), "..")

    def _body(self, path, start_marker, end_marker):
        src = open(os.path.join(self.ROOT, path), encoding="utf-8").read()
        start = src.index(start_marker)
        return src[start:src.index(end_marker, start)]

    def test_mac_handle_wake_resets_the_trigger(self):
        # The Mac's wake handler is the daemon's (onionpress.daemon, #367).
        body = self._body("src/onionpress/daemon.py", "    def handle_wake(self):",
                          "    # -- the hub's messages")
        self.assertIn("self._recovery.note_wake()", body)
        menubar = open(os.path.join(self.ROOT, "src/menubar.py"), encoding="utf-8").read()
        self.assertNotIn("def handle_wake(", menubar)

    def test_linux_wake_is_the_daemons_too(self):
        # One wake handler on both platforms since #407 (the daemon's
        # handle_wake, run by power.LogindPowerHandler on Linux); the
        # Linux ReachabilityLoop with its own reset is gone.
        self.assertNotIn("class ReachabilityLoop", open(os.path.join(
            self.ROOT, "src/onionpress/reachability.py"), encoding="utf-8").read())
        daemon = open(os.path.join(self.ROOT, "src/onionpress/daemon.py"), encoding="utf-8").read()
        self.assertIn("on_has_powered_on=lambda: None if self._quitting else self.handle_wake()", daemon)
        self.assertIn("from .power import LogindPowerHandler", daemon)

    def test_the_shared_wake_reset_resets_the_window(self):
        from onionpress.reachability import RecoveryWindow
        body = self._body("src/onionpress/reachability.py",
                          "    def note_wake(self) -> None:\n        \"\"\"Back from suspend. Issue #266",
                          "    def note_offline(self)")
        self.assertIn("self.trigger.reset()", body)
        window = RecoveryWindow(mock.Mock(), lambda m: None, lambda k, d: d)
        window.decide(True, False, "000:rc=28")
        self.assertIsNotNone(window.trigger.failing_since)
        window.note_wake()
        self.assertIsNone(window.trigger.failing_since)

    def test_the_logind_handler_runs_the_wake_callback(self):
        src = open(os.path.join(self.ROOT, "src/onionpress/power.py"), encoding="utf-8").read()
        body = src[src.index("    def _on_prepare_for_sleep(self, going_to_sleep) -> None:"):]
        wake = body[body.index("self._acquire()"):]
        self.assertIn("self._on_has_powered_on()", wake)


if __name__ == "__main__":
    unittest.main()

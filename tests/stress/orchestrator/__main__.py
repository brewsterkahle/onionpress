"""CLI entry point + main test flow.

Usage:
    python3 -m tests.stress.orchestrator --total 5
    python3 -m tests.stress.orchestrator --cleanup
    python3 tests/stress/dashboard.py            # standalone dashboard
"""

import atexit
import os
import signal
import subprocess
import sys
import time

# Add src to path for onionpress imports — must be before any .orchestrator imports
# that reference onionpress at module level (e.g. containers.py)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))

from .config import StressConfig
from .phases import StressLogger, fmt_duration
from .containers import WorkerManager, PollClientManager
from .metrics import WorkerInfoStore, Dashboard, init_worker_db_volume
from .notifications import (
    generate_signed_payloads, send_notifications, send_notifications_via_workers,
    flush_client_descriptor_cache,
)
from .verification import (
    wait_for_bootstrap, wait_for_takeover, wait_for_recovery,
    run_verify_worker, verify_redirects,
)
from .cleanup import cleanup_stress_test, run_cleanup, run_cleanup_stale

from onionpress.docker import Docker
from onionpress.platform import resolve_paths

DEFAULT_ONIONHEAVEN_ADDR = "oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion"


def _create_docker(config: StressConfig) -> Docker:
    """Create a Docker wrapper with OnionPress paths."""
    paths = resolve_paths(data_dir=config.data_dir)
    return Docker(paths)


def _detect_onionheaven_addr(config: StressConfig, logger: StressLogger):
    """Auto-detect OnionHeaven address if not specified."""
    if config.onionheaven_addr:
        logger.log(f"OnionHeaven address (user-specified): {config.onionheaven_addr}")
        return
    config.onionheaven_addr = DEFAULT_ONIONHEAVEN_ADDR
    logger.log(f"OnionHeaven address: {config.onionheaven_addr}")


def _preflight(config: StressConfig, docker: Docker, logger: StressLogger):
    """Run preflight checks."""
    os.makedirs(config.output_dir, exist_ok=True)
    logger.log("Preflight checks...")

    result = docker.run(["info"], timeout=15)
    if not result.ok:
        logger.log("ERROR: Cannot reach Docker")
        sys.exit(1)

    for ctr in ["onionpress-tor", "onionpress-wordpress"]:
        if not docker.container_running(ctr):
            logger.log(f"ERROR: Container {ctr} is not running")
            sys.exit(1)

    _detect_onionheaven_addr(config, logger)

    # Get local onion address for logging
    result = docker.exec("onionpress-tor",
        "cat /var/lib/tor/hidden_service/wordpress/hostname", timeout=10)
    local_addr = result.output.strip() if result.ok else ""
    if local_addr:
        logger.log(f"  Local address: {local_addr}")

    # Verify we can reach OnionHeaven API over Tor
    if not docker.container_running("onionpress-tor"):
        logger.log("ERROR: onionpress-tor is not running (needed for Tor SOCKS)")
        sys.exit(1)
    logger.log(f"  Checking OnionHeaven API at {config.onionheaven_addr}:8083/status ...")
    result = docker.exec("onionpress-tor",
        f'curl -s -w "\\n%{{http_code}}" --socks5-hostname "preflight:x@127.0.0.1:9050" --max-time 30 '
        f'"http://{config.onionheaven_addr}:8083/status"',
        timeout=35)
    if result.ok and result.output.strip():
        lines = result.output.strip().rsplit("\n", 1)
        body = lines[0] if len(lines) > 1 else ""
        code = lines[-1].strip()
    else:
        body, code = "", "000"
    if code != "200" or '"total"' not in body:
        logger.log(f"ERROR: OnionHeaven API returned HTTP {code} (expected 200)")
        logger.log(f"  Response: {body[:200]}")
        sys.exit(1)
    logger.log(f"  OnionHeaven API OK (HTTP {code}): {body[:100]}")

    logger.log("Preflight OK")


def _check_previous_artifacts(docker: Docker, config: StressConfig, logger: StressLogger):
    """Check for and optionally clean leftover stress test containers."""
    result = docker.run(["ps", "-a", "--format", "{{.Names}}"], timeout=10)
    stale_containers = [
        c for c in (result.output.strip().splitlines() if result.ok else [])
        if c.startswith("stress-worker-") or c.startswith("stress-poll-client-")
    ]

    if not stale_containers:
        return

    print()
    print(f"Found {len(stale_containers)} stress container(s) from a previous run")
    print()

    if sys.stdin.isatty():
        answer = input("Clean up before starting? [Y/n] ").strip()
    else:
        answer = "y"

    if answer.lower().startswith("n"):
        logger.log("Keeping previous artifacts")
        return

    logger.log("Cleaning previous artifacts...")
    for ctr in stale_containers:
        docker.run(["rm", "-f", ctr], timeout=10)
    logger.log(f"  Removed {len(stale_containers)} stress containers")
    print()


def _open_phase_log_window(phase_log: str):
    """Open a Terminal.app window tailing the phase log."""
    try:
        abs_path = os.path.abspath(phase_log)
        subprocess.Popen([
            "osascript", "-e",
            f'tell application "Terminal" to do script "tail -f \'{abs_path}\'"',
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def run_worker(config: StressConfig):
    """Main stress test flow."""
    docker = _create_docker(config)
    logger = StressLogger(config.output_dir)

    _preflight(config, docker, logger)
    _check_previous_artifacts(docker, config, logger)

    # Create timestamped run directory
    run_ts = time.strftime("%Y%m%d-%H%M%S")
    run_dir = os.path.join(config.output_dir, f"run-{run_ts}")
    os.makedirs(run_dir, exist_ok=True)
    config.output_dir = run_dir
    # Update latest symlink
    latest_link = os.path.join(os.path.dirname(run_dir), "latest")
    try:
        os.unlink(latest_link)
    except OSError:
        pass
    os.symlink(f"run-{run_ts}", latest_link)

    logger.output_dir = config.output_dir
    logger.setup(
        os.path.join(run_dir, "stress-test.log"),
        os.path.join(run_dir, "phase.log"),
    )

    workers = WorkerManager(config, docker, logger)
    workers.detect_images()

    dashboard = Dashboard(config, docker, logger)
    oh_version = dashboard.get_onionheaven_version()
    logger.write_phase_header(config, oh_version)
    _open_phase_log_window(logger.phase_log)

    logger.log("=== OnionHeaven Stress Test (C Tor) ===")
    logger.log(f"OnionHeaven: {config.onionheaven_addr}")
    logger.log(f"Sites: {config.total} total ({config.healthy} stay healthy, {config.failing} will fail)")
    logger.log(f"Stress containers: {config.num_containers} x {config.per_ctr} sites/container")
    if config.batch_size > 0:
        logger.log(f"Container batch size: {config.batch_size}")
    logger.log(f"Output: {config.output_dir}")
    print()

    # Store for worker info (reads from shared SQLite DB via docker exec)
    store = WorkerInfoStore(docker, config)

    # Cleanup on exit
    def _cleanup():
        logger.log("Cleaning up before exit...")
        cleanup_stress_test(docker, config, logger, store)

    atexit.register(_cleanup)

    def _sigint(sig, frame):
        logger.log("Interrupted...")
        sys.exit(130)
    signal.signal(signal.SIGINT, _sigint)
    signal.signal(signal.SIGTERM, _sigint)

    try:
        _run_phases(config, docker, logger, workers, store, dashboard)
    except Exception as e:
        import traceback
        logger.log(f"ERROR: {e}")
        logger.log(traceback.format_exc())
        raise
    finally:
        # Cleanup (deregister atexit handler, clean explicitly)
        atexit.unregister(_cleanup)
        cleanup_stress_test(docker, config, logger, store)
        print()

    logger.log("=== Stress test complete ===")
    logger.log(f"Results saved to: {config.output_dir}/metrics.jsonl")


def _run_phases(config, docker, logger, workers, store, dashboard):
    """Execute all test phases. Separated so errors are caught and logged."""
    run_start = time.time()
    results = {}

    # Create shared Docker volume + DB before starting containers
    init_worker_db_volume(docker, config)
    logger.log(f"Created worker-info volume: {config.db_volume}")

    # Phase 1: Start containers
    logger.phase_start("1", f"Starting {config.num_containers} containers ({config.total} sites) (est. <1m)")
    logger.log(f"Phase 1: Starting {config.num_containers} site containers + {config.num_poll_clients} polling clients...")
    workers.start_all()

    poll_mgr = PollClientManager(config, docker, logger, workers.stress_image, workers.network)
    poll_mgr.start_all()
    logger.phase_result("1", f"Started {config.num_containers} stress + {config.num_poll_clients} poll containers")
    print()

    # Phase 2: Bootstrap
    logger.phase_start("2", "Waiting for sites to bootstrap and register over Tor (est. 2m)")
    logger.log("Phase 2: Waiting for sites to bootstrap and register over Tor...")
    r = wait_for_bootstrap(docker, config, logger, store)
    results["phase2"] = r.message
    logger.phase_result("2", r.message)

    # Load full worker data from the shared DB
    store.refresh()
    total_registered = store.total_registered()
    logger.log(f"Total registered sites: {total_registered}")
    print()

    # HSFETCH
    flush_client_descriptor_cache(docker, config, logger, store, 0, config.total)

    # Phase 3: Healthy
    hc_type = "content" if config.no_healthcheck else "healthcheck"
    logger.phase_start("3", f"Waiting for all {config.total} sites to be reachable from polling clients (est. 1m)")
    logger.log(f"Phase 3: Waiting for {config.total} {hc_type} addresses to be reachable from polling clients (timeout: {config.healthy_timeout}s)...")
    r = run_verify_worker(docker, config, logger, store,
                          "200", config.total, config.healthy_timeout, 0, config.total, hc_type)
    if r.success:
        logger.log("Phase 3: all sites reachable")
    else:
        logger.log("WARNING: Not all sites became healthy, continuing anyway...")
    results["phase3"] = r.message
    logger.phase_result("3", r.message)
    print()

    if config.failing > 0:
        fail_start = config.fail_start

        # ═══════════════════════════════════════════════════════════════
        # Scenario A: Graceful offline/online
        # ═══════════════════════════════════════════════════════════════
        logger.phase_start("A", "GRACEFUL OFFLINE/ONLINE (with /offline and /online notifications)")

        # A.1: Takeover
        logger.phase_start("A.1", f"Graceful takeover: /offline + disable {config.failing} sites (est. 1-2m)")
        scenario_ts = time.time()
        logger.log(f"Phase A.1: Graceful offline — disabling responders + sending /offline for {config.failing} sites...")
        workers.disable_workers(fail_start, config.failing)

        send_notifications_via_workers(docker, logger, config, "offline", fail_start, config.failing)
        flush_client_descriptor_cache(docker, config, logger, store, fail_start, config.failing)

        logger.log("Phase A.1: Waiting for takeovers...")
        r = wait_for_takeover(docker, config, logger, store, dashboard,
                              config.failing, config.takeover_timeout)
        takeover_elapsed = int(time.time() - scenario_ts)
        results["a1"] = f"{r.message} ({fmt_duration(takeover_elapsed)} e2e)"
        logger.phase_result("A.1", f"Takeover: {results['a1']}")
        print()

        # A.1v: Verify redirects
        logger.phase_start("A.1v", "Double-check taken-over addresses redirect (302) to Wayback Machine (est. <1m)")
        sample = max(5, config.failing // 4)  # 25% of failing, min 5
        r = verify_redirects(docker, config, logger, store, "A.1v", sample)
        results["a1v"] = r.message
        logger.phase_result("A.1v", r.message)
        print()

        # A.2: Recovery
        logger.phase_start("A.2", f"Graceful recovery: re-enable + /online for {config.failing} sites (est. 1-2m)")
        scenario_ts = time.time()
        logger.log("Phase A.2: Graceful recovery — re-enabling responders + sending /online...")
        workers.enable_workers(fail_start, config.failing, silent=False)

        send_notifications_via_workers(docker, logger, config, "online",
                                      fail_start, config.failing,
                                      stress_version=config.stress_version)
        flush_client_descriptor_cache(docker, config, logger, store, fail_start, config.failing)

        logger.log("Phase A.2: Waiting for recovery...")
        r = wait_for_recovery(docker, config, logger, store, dashboard,
                              config.failing, config.recovery_timeout)
        recovery_elapsed = int(time.time() - scenario_ts)
        results["a2"] = f"{r.message} ({fmt_duration(recovery_elapsed)} e2e)"
        logger.phase_result("A.2", f"Recovery: {results['a2']}")
        print()

        results["a_sum"] = f"takeover {fmt_duration(takeover_elapsed)}, recovery {fmt_duration(recovery_elapsed)}"
        logger.phase_result("A", f"Graceful: {results['a_sum']}")
        print()

        # ═══════════════════════════════════════════════════════════════
        # Scenario B: Silent crash + recovery (heartbeat-only)
        # ═══════════════════════════════════════════════════════════════
        logger.phase_start("B", "SILENT CRASH/RECOVERY (no notifications, heartbeat-only detection)")

        # B.1: Takeover
        logger.phase_start("B.1", f"Silent crash: disable {config.failing} sites (no /offline), wait for heartbeat (est. 9m)")
        scenario_ts = time.time()
        logger.log(f"Phase B.1: Silent crash — disabling responders for {config.failing} sites (no /offline)...")
        workers.disable_workers(fail_start, config.failing)
        flush_client_descriptor_cache(docker, config, logger, store, fail_start, config.failing)

        logger.log("Phase B.1: Waiting for heartbeat-detected takeovers (takeovers can start at 180s)...")
        r = wait_for_takeover(docker, config, logger, store, dashboard,
                              config.failing, config.takeover_timeout)
        takeover_elapsed = int(time.time() - scenario_ts)
        results["b1"] = f"{r.message} ({fmt_duration(takeover_elapsed)} e2e)"
        logger.phase_result("B.1", f"Takeover: {results['b1']}")
        print()

        # B.1v: Verify redirects
        logger.phase_start("B.1v", "Double-check taken-over addresses redirect (302) (est. <1m)")
        sample = max(5, config.failing // 4)  # 25% of failing, min 5
        r = verify_redirects(docker, config, logger, store, "B.1v", sample)
        results["b1v"] = r.message
        logger.phase_result("B.1v", r.message)
        print()

        results["b_sum"] = f"takeover {fmt_duration(takeover_elapsed)}"
        logger.phase_result("B", f"Silent: {results['b_sum']}")
        print()

    else:
        logger.log("No failing sites configured — skipping failure/recovery test")
        print()

    # Summary
    logger.write_summary(config, results, run_start)

    logger.phase_start("done", "Final metrics and cleanup")
    logger.log("=== Final metrics ===")
    dashboard.print_dashboard()
    logger.phase_result("done", "Stress test complete")
    print()


def main():
    config = StressConfig.from_args()

    if config.cleanup_stale:
        docker = _create_docker(config)
        logger = StressLogger(config.output_dir)
        result = docker.run(["info"], timeout=15)
        if not result.ok:
            print("ERROR: Cannot reach Docker")
            sys.exit(1)
        _detect_onionheaven_addr(config, logger)
        run_cleanup_stale(docker, config, logger)
        sys.exit(0)

    if config.cleanup:
        docker = _create_docker(config)
        logger = StressLogger(config.output_dir)
        result = docker.run(["info"], timeout=15)
        if not result.ok:
            print("ERROR: Cannot reach Docker")
            sys.exit(1)
        _detect_onionheaven_addr(config, logger)
        run_cleanup(docker, config, logger)
        sys.exit(0)

    if config.mode == "worker":
        run_worker(config)
    else:
        print(f"Unknown mode: {config.mode}")
        sys.exit(1)


if __name__ == "__main__":
    main()

"""Container + registry cleanup."""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from .config import StressConfig
from .phases import StressLogger
from .metrics import WorkerInfoStore
from .notifications import generate_unregister_payloads

from onionpress.docker import Docker


def cleanup_stress_test(
    docker: Docker,
    config: StressConfig,
    logger: StressLogger,
    store: WorkerInfoStore,
):
    """Remove all stress containers and unregister entries."""
    logger.log("Cleaning up stress test artifacts...")

    # Remove worker containers in parallel
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = []
        for idx in range(config.num_containers):
            futures.append(executor.submit(
                docker.run, ["rm", "-f", config.container_name(idx)], 10,
            ))
        # Also catch extras
        result = docker.run(
            ["ps", "-a", "--format", "{{.Names}}"],
            timeout=10,
        )
        if result.ok:
            for ctr in result.output.strip().splitlines():
                if ctr.startswith("stress-worker-"):
                    futures.append(executor.submit(
                        docker.run, ["rm", "-f", ctr], 10,
                    ))
        for f in as_completed(futures):
            f.result()

    # Remove polling clients
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = []
        for i in range(config.num_poll_clients):
            futures.append(executor.submit(
                docker.run, ["rm", "-f", config.poll_client_name(i)], 10,
            ))
        for f in as_completed(futures):
            f.result()

    logger.log("  Removed stress + polling containers")

    # Remove the shared worker-info Docker volume
    docker.run(["volume", "rm", "-f", config.db_volume], timeout=10)
    logger.log(f"  Removed volume: {config.db_volume}")

    # Clean stress test entries and refresh workers via /reset-onionheaven.
    # Always call over Tor to reach the OnionHeaven that was actually used.
    result = docker.exec("onionpress-tor",
        f'curl -s --socks5-hostname "reset:x@127.0.0.1:9050" --max-time 120 '
        f'-X POST "http://{config.onionheaven_addr}:8083/reset-onionheaven"',
        timeout=130)
    if result.ok and "error" not in (result.output or "").lower():
        logger.log(f"  OnionHeaven reset: {result.output.strip()}")
    else:
        logger.log(f"  OnionHeaven reset failed, falling back to individual unregister")
        payloads = generate_unregister_payloads(store)
        count = 0
        for payload in payloads:
            docker.exec("onionpress-tor",
                f'curl -s --socks5-hostname "unreg{count}:x@127.0.0.1:9050" --max-time 30 '
                f'-X POST "http://{config.onionheaven_addr}:8083/unregister" '
                f'-H "Content-Type: application/json" '
                f"-d '{payload}'",
                timeout=35)
            count += 1
        logger.log(f"  Unregistered {count} entries")
    logger.log("Cleanup complete")


def run_cleanup(
    docker: Docker,
    config: StressConfig,
    logger: StressLogger,
):
    """Full cleanup mode — remove all stress artifacts."""
    logger.log("=== OnionHeaven Stress Test Cleanup ===")

    # Remove all stress containers
    result = docker.run(["ps", "-a", "--format", "{{.Names}}"], timeout=10)
    if result.ok:
        for ctr in result.output.strip().splitlines():
            if ctr.startswith("stress-worker-") or ctr.startswith("stress-poll-client-"):
                docker.run(["rm", "-f", ctr], timeout=10)
                logger.log(f"Removed container: {ctr}")

    docker.run(["rm", "-f", "stress-worker-tor"], timeout=10)

    # Build unregister payloads from all info files
    store = WorkerInfoStore(docker, config)
    store.load_from_globs()
    payloads = generate_unregister_payloads(store)

    logger.log(f"Found {len(payloads)} stress-test entries to clean up")
    if not payloads:
        logger.log("Nothing to clean up")
        return

    # Unregister in parallel
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {}
        for i, payload in enumerate(payloads):
            fut = executor.submit(
                docker.exec, "onionpress-tor",
                f'curl -s --socks5-hostname "cleanup{i}:x@127.0.0.1:9050" --max-time 30 '
                f'-X POST "http://{config.onionheaven_addr}:8083/unregister" '
                f'-H "Content-Type: application/json" '
                f"-d '{payload}'",
                35,
            )
            futures[fut] = i

        succeeded = 0
        for fut in as_completed(futures):
            if fut.result().ok:
                succeeded += 1

    logger.log(f"Unregistered {succeeded}/{len(payloads)} entries (parallel)")
    logger.log(f"Cleanup complete: {len(payloads)} stress-test entries removed")


def run_cleanup_stale(
    docker: Docker,
    config: StressConfig,
    logger: StressLogger,
):
    """Remove only stale stress tests (no activity in stale_hours)."""
    db_path = "/var/lib/onionheaven/registry.db"
    logger.log(f"=== OnionHeaven Stale Stress Test Cleanup (>{config.stale_hours}h inactive) ===")

    if not docker.container_running("onionheaven"):
        logger.log("ERROR: --cleanup-stale requires the onionheaven container (need DB access)")
        logger.log("NOTE:  Stale stress-test entries are auto-cleaned by the heartbeat monitor after 2h.")
        return

    # Find stale versions
    result = docker.exec("onionheaven",
        f'sqlite3 {db_path} '
        f'"SELECT version, COUNT(*), MAX(last_healthy) FROM registry '
        f"WHERE unregistered_at IS NULL "
        f"AND version LIKE 'stress-test%' "
        f"AND last_healthy < datetime('now', '-{config.stale_hours} hours') "
        f'GROUP BY version;"',
        timeout=10)

    if not result.ok or not result.output.strip():
        logger.log(f"No stale stress tests found (all have activity within {config.stale_hours}h)")

        # Show active stress tests
        result = docker.exec("onionheaven",
            f'sqlite3 {db_path} '
            f'"SELECT version, COUNT(*), MAX(last_healthy) FROM registry '
            f"WHERE unregistered_at IS NULL AND version LIKE 'stress-test%' "
            f'GROUP BY version;"',
            timeout=10)
        if result.ok and result.output.strip():
            logger.log("Active stress tests:")
            for line in result.output.strip().splitlines():
                parts = line.split("|")
                if len(parts) >= 3:
                    logger.log(f"  {parts[0]}: {parts[1]} entries, last activity {parts[2]}")
        return

    logger.log("Stale stress tests to clean up:")
    total_cleaned = 0
    for line in result.output.strip().splitlines():
        parts = line.split("|")
        if len(parts) < 3:
            continue
        ver, cnt, last = parts[0], parts[1], parts[2]
        logger.log(f"  {ver}: {cnt} entries, last activity {last}")

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        docker.exec("onionheaven",
            f'sqlite3 {db_path} '
            f"\"UPDATE registry SET unregistered_at = '{now}', "
            f"unregistered_reason = 'stale-cleanup', "
            f"status = 'unregistered' "
            f"WHERE version = '{ver}' "
            f"AND unregistered_at IS NULL "
            f"AND last_healthy < datetime('now', '-{config.stale_hours} hours');\"",
            timeout=10)
        logger.log(f"  Cleaned up {cnt} entries from {ver}")
        try:
            total_cleaned += int(cnt)
        except ValueError:
            pass

    # Remove idle local containers
    result = docker.run(
        ["ps", "-a", "--format", "{{.Names}}"],
        timeout=10,
    )
    if result.ok:
        removed = 0
        for ctr in result.output.strip().splitlines():
            if not ctr.startswith("stress-worker-"):
                continue
            has_info = docker.exec(ctr,
                "test -f /worker-info.db && echo yes || echo no", timeout=10)
            has_bootstrap = docker.exec(ctr,
                "ps aux 2>/dev/null | grep -c '[w]orker-bootstrap'", timeout=10)
            has_heartbeat = docker.exec(ctr,
                "ps aux 2>/dev/null | grep -c '[h]eartbeat_loop'", timeout=10)

            info_val = has_info.output.strip() if has_info.ok else "unknown"
            boot_val = has_bootstrap.output.strip() if has_bootstrap.ok else "0"
            hb_val = has_heartbeat.output.strip() if has_heartbeat.ok else "0"

            if info_val == "yes" and boot_val == "0" and hb_val == "0":
                docker.run(["rm", "-f", ctr], timeout=10)
                removed += 1
                logger.log(f"  Removed idle container: {ctr}")
            else:
                logger.log(f"  Keeping active container: {ctr} (info={info_val} bootstrap={boot_val} heartbeat={hb_val})")

        if removed > 0:
            logger.log(f"Removed {removed} idle containers")

    logger.log(f"Stale cleanup complete: {total_cleaned} entries cleaned")

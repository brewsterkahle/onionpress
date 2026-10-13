"""OnionHeaven API notifications — /offline, /online, /unregister.

Replaces ~400 lines of nearly-identical bash (notify_offline/notify_online).
Generates signed payloads via direct onion_auth calls (no subprocess).
"""

import base64
import json
import os
import sys

from .config import StressConfig
from .phases import StressLogger
from .metrics import WorkerInfoStore

from onionpress.docker import Docker

# Import onion_auth from the onionpress package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))
from onionpress.onion_auth import sign_payload, make_timestamp


def generate_signed_payloads(
    store: WorkerInfoStore,
    endpoint: str,
    start: int,
    count: int,
    stress_version: str = "",
) -> list[str]:
    """Generate signed JSON payloads for a range of sites.

    Args:
        store: WorkerInfoStore with loaded worker info.
        endpoint: API endpoint name ('offline', 'online', 'unregister').
        start: Global index of first site.
        count: Number of sites.
        stress_version: Version string (required for 'online' payloads).

    Returns:
        List of JSON strings, one per site.
    """
    payloads = []
    for i in range(start, start + count):
        worker = store.get_worker(i)
        if not worker:
            continue
        ca = worker.get("content_address", "")
        ha = worker.get("healthcheck_address", "")
        pk = worker.get("privkey_b64", "")
        pub = worker.get("pubkey_b64", "")
        if not (ca and ha and pk and pub):
            continue

        privkey = base64.b64decode(pk)
        pubkey = base64.b64decode(pub)
        ts = make_timestamp()
        sig = sign_payload(privkey, pubkey, endpoint, ca, ha, ts)
        payload = {
            "content_address": ca,
            "healthcheck_address": ha,
            "timestamp": ts,
            "signature": sig,
        }
        # /online requires arti_key_pem and version
        if endpoint == "online":
            payload["arti_key_pem"] = worker.get("arti_key_pem", "")
            payload["version"] = stress_version
            payload["wordpress_healthy"] = True
        payloads.append(json.dumps(payload))
    return payloads


def generate_unregister_payloads(store: WorkerInfoStore) -> list[str]:
    """Generate signed /unregister payloads for ALL sites in the store."""
    payloads = []
    seen = set()
    for worker in store.all_workers():
        ca = worker.get("content_address", "")
        ha = worker.get("healthcheck_address", "")
        pk = worker.get("privkey_b64", "")
        pub = worker.get("pubkey_b64", "")
        if not (ca and pk and pub) or ca in seen:
            continue
        seen.add(ca)
        privkey = base64.b64decode(pk)
        pubkey = base64.b64decode(pub)
        ts = make_timestamp()
        sig = sign_payload(privkey, pubkey, "unregister", ca, ha, ts)
        payloads.append(json.dumps({
            "content_address": ca,
            "healthcheck_address": ha,
            "timestamp": ts,
            "signature": sig,
        }))
    return payloads


def _send_one_notification(
    docker: Docker,
    config: StressConfig,
    endpoint: str,
    payload: str,
    index: int,
    max_attempts: int = 3,
) -> tuple[bool, str]:
    """Send a single notification with retries. Returns (success, detail)."""
    tag = endpoint[:3]
    code = "000"
    body = ""
    for attempt in range(1, max_attempts + 1):
        socks_tag = f"{tag}{index}r{attempt}"
        # Capture both HTTP code and response body
        result = docker.exec(
            "onionpress-tor",
            [
                "curl", "-s", "-w", "\n%{http_code}",
                "--socks5-hostname", f"{socks_tag}:x@127.0.0.1:9050",
                "--max-time", "30",
                "-X", "POST",
                f"http://{config.onionheaven_addr}:8083/{endpoint}",
                "-H", "Content-Type: application/json",
                "-d", payload,
            ],
            timeout=45,
        )
        if result.ok and result.output.strip():
            lines = result.output.strip().rsplit("\n", 1)
            body = lines[0] if len(lines) > 1 else ""
            code = lines[-1].strip()
        else:
            code = "000"
            body = result.stderr.strip()[:100] if result.stderr else ""

        if code == "200":
            return True, f"HTTP 200 (attempt {attempt})"
        if attempt < max_attempts:
            import time
            time.sleep(5)

    # Include the error response body so we know why it failed
    detail = f"HTTP {code} after {max_attempts} attempts"
    if body:
        detail += f": {body[:150]}"
    return False, detail


def send_notifications(
    docker: Docker,
    logger: StressLogger,
    config: StressConfig,
    endpoint: str,
    payloads: list[str],
    max_parallel: int = 5,
) -> int:
    """Send notification payloads to OnionHeaven API over Tor.

    Uses ThreadPoolExecutor for parallelism with per-payload logging.

    Returns:
        Number of successfully sent notifications.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if not payloads:
        logger.log(f"WARNING: No payloads generated for /{endpoint}")
        return 0

    logger.log(f"  Generated {len(payloads)} /{endpoint} payload(s)")

    notified = 0
    failed_details = []

    with ThreadPoolExecutor(max_workers=max_parallel) as executor:
        futures = {}
        for i, payload in enumerate(payloads):
            # Extract content_address for logging
            try:
                addr = json.loads(payload).get("content_address", "?")
            except (json.JSONDecodeError, ValueError):
                addr = "?"
            fut = executor.submit(
                _send_one_notification, docker, config, endpoint, payload, i,
            )
            futures[fut] = (i, addr)

        for fut in as_completed(futures):
            idx, addr = futures[fut]
            try:
                success, detail = fut.result()
                if success:
                    notified += 1
                    logger.log(f"  /{endpoint} {addr[:20]}... → {detail}")
                else:
                    failed_details.append((addr, detail))
                    logger.log(f"  /{endpoint} FAILED {addr[:20]}... → {detail}")
            except Exception as e:
                failed_details.append((addr, str(e)))
                logger.log(f"  /{endpoint} ERROR {addr[:20]}... → {e}")
            logger.progress_dot()

    logger.progress_end(f"{notified}/{len(payloads)}")
    logger.log(f"Sent /{endpoint} for {notified}/{len(payloads)} sites")
    if failed_details:
        logger.log(f"WARNING: {len(failed_details)} /{endpoint} notification(s) failed")

    logger.log_json(
        f'"event":"{endpoint}_notify","count":{len(payloads)},"notified":{notified}'
    )
    return notified


def send_notifications_via_workers(
    docker: Docker,
    logger: StressLogger,
    config: StressConfig,
    endpoint: str,
    start: int,
    count: int,
    stress_version: str = "",
) -> int:
    """Send notifications through the worker containers' own Tor circuits.

    Instead of routing all notifications through onionpress-tor,
    tells each worker container to sign and send its own /offline or /online
    payloads via its already-warm Tor SOCKS proxy.

    Returns:
        Number of successfully sent notifications.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if count == 0:
        return 0

    # Group workers by container
    per_ctr_map: dict[int, list[int]] = {}  # ctr_idx -> [local_indices]
    for i in range(start, start + count):
        ctr_idx = i // config.per_ctr
        local_idx = i % config.per_ctr
        per_ctr_map.setdefault(ctr_idx, []).append(local_idx)

    logger.log(f"  Sending /{endpoint} via {len(per_ctr_map)} worker container(s) ({count} sites)...")

    notified = 0
    failed = 0

    def _notify_container(ctr_idx: int, local_indices: list[int]) -> tuple[int, int]:
        """Send notifications for a batch of workers in one container."""
        ctr_name = config.container_name(ctr_idx)
        result = docker.exec(ctr_name, [
            "curl", "-s", "-X", "POST", "http://127.0.0.1:9000/notify",
            "-H", "Content-Type: application/json",
            "-d", json.dumps({
                "workers": local_indices,
                "endpoint": endpoint,
                "onionheaven_addr": config.onionheaven_addr,
                "stress_version": stress_version,
            }),
        ], timeout=60 + len(local_indices) * 35)

        ok_count = 0
        fail_count = 0
        if result.ok and result.output.strip():
            try:
                data = json.loads(result.output)
                for widx, r in data.get("results", {}).items():
                    if isinstance(r, dict) and not r.get("error"):
                        ok_count += 1
                    else:
                        err = r.get("error", r) if isinstance(r, dict) else r
                        logger.log(f"  /{endpoint} FAILED {ctr_name} local={widx}: {str(err)[:200]}")
                        fail_count += 1
            except (json.JSONDecodeError, ValueError):
                logger.log(f"  /{endpoint} FAILED {ctr_name}: bad response: {result.output[:200]}")
                fail_count = len(local_indices)
        else:
            logger.log(f"  /{endpoint} FAILED {ctr_name}: no response")
            fail_count = len(local_indices)
        return ok_count, fail_count

    with ThreadPoolExecutor(max_workers=len(per_ctr_map)) as executor:
        futures = {
            executor.submit(_notify_container, ctr_idx, indices): ctr_idx
            for ctr_idx, indices in per_ctr_map.items()
        }
        for fut in as_completed(futures):
            ctr_idx = futures[fut]
            try:
                ok, fail = fut.result()
                notified += ok
                failed += fail
            except Exception as e:
                ctr_name = config.container_name(ctr_idx)
                logger.log(f"  /{endpoint} ERROR on {ctr_name}: {e}")
                failed += len(per_ctr_map.get(ctr_idx, []))
            logger.progress_dot()

    logger.progress_end(f"{notified}/{count}")
    logger.log(f"Sent /{endpoint} for {notified}/{count} sites via worker containers")
    if failed > 0:
        logger.log(f"WARNING: {failed} /{endpoint} notification(s) failed")

    logger.log_json(
        f'"event":"{endpoint}_notify","count":{count},"notified":{notified}'
    )
    return notified


def flush_client_descriptor_cache(
    docker: Docker,
    config: StressConfig,
    logger: StressLogger,
    store: WorkerInfoStore,
    start: int,
    count: int,
):
    """Flush descriptor cache on all poll clients via NEWNYM + HSFETCH.

    Uses TorControl (tor_control.py) instead of xxd+nc shell pipes.
    """
    from .tor_control import TorControl

    addrs = store.get_content_addrs(start, count)
    if not addrs:
        return

    sids = [a.replace(".onion", "") for a in addrs]
    logger.log(f"HSFETCH for {len(sids)} addresses across {config.num_poll_clients} poll clients...")

    for ci in range(config.num_poll_clients):
        cname = config.poll_client_name(ci)
        try:
            tc = TorControl(docker, container=cname)
            tc.flush_descriptor_cache(sids)
        except Exception as e:
            logger.log(f"  WARNING: flush failed on {cname}: {e}")

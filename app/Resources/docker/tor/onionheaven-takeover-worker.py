#!/usr/bin/env python3
"""
OnionHeaven Takeover Worker — runs inside onionheaven-takeover-N containers.

Watches the registry DB for rows assigned to this container with
takeover_pending or release_pending flags set. Executes the actual
tor-manager takeover/release commands locally (this container has its
own Tor instance with a control port).

Each takeover container has its own Tor guard pool, preventing the
circuit exhaustion cascade that occurs when a single Tor daemon handles
too many onion services.

Startup:
  1. Register self in takeover_containers table
  2. Reconcile stale assignments (release any services from previous run)

Main loop (every 2s):
  - Process takeover_pending rows assigned to this container
  - Process release_pending rows assigned to this container
  - Heartbeat every 30s
"""

import os
import subprocess
import sys
import time
from datetime import datetime, timezone

from onionheaven_common import (
    db_connect, db_commit_with_retry, db_ensure_schema, log,
    _takeover_local, _release_local,
    TOR_MANAGER, ONIONHEAVEN_DATA_DIR,
)

CONTAINER_NAME = os.environ.get("CONTAINER_NAME", "unknown")
MAX_SERVICES = int(os.environ.get("MAX_TAKEOVER_SERVICES", "10"))
LOOP_INTERVAL = 2  # seconds between DB checks
HEARTBEAT_INTERVAL = 30  # seconds between heartbeats


def register_self(conn):
    """Register this container in takeover_containers table."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO takeover_containers (container_name, max_services, active_services, last_heartbeat, status) "
        "VALUES (?, ?, 0, ?, 'active') "
        "ON CONFLICT(container_name) DO UPDATE SET "
        "max_services = excluded.max_services, active_services = 0, "
        "last_heartbeat = excluded.last_heartbeat, status = 'active'",
        (CONTAINER_NAME, MAX_SERVICES, now)
    )
    db_commit_with_retry(conn)
    log(f"takeover-worker: registered as {CONTAINER_NAME} (max {MAX_SERVICES} services)")


def startup_reconciliation(conn):
    """Re-add taken-over services after container restart.

    Container restart wipes ephemeral ADD_ONION services. Re-execute
    takeovers for entries still assigned to us so they start serving again
    immediately.
    """
    # Get the list of addresses to re-add (one-time snapshot, not a while loop)
    stale = conn.execute(
        "SELECT DISTINCT content_address FROM registry "
        "WHERE takeover_container = ? AND status = 'taken-over'",
        (CONTAINER_NAME,)
    ).fetchall()
    count = 0
    for row in stale:
        addr = row[0]
        ok = _takeover_local(addr)
        if ok:
            conn.execute(
                "UPDATE registry SET takeover_pending = NULL "
                "WHERE content_address = ? AND takeover_container = ?",
                (addr, CONTAINER_NAME)
            )
            db_commit_with_retry(conn)
            count += 1
            log(f"  re-added takeover for {addr}")
        time.sleep(5)

    if count > 0:
        log(f"takeover-worker: re-added {count} taken-over service(s) after restart")
    log("takeover-worker: reconciliation complete")



def process_takeovers(conn):
    """Process pending takeover requests one at a time with a pause between each.

    Each ADD_ONION is instant and independent — the pause gives Tor time to
    publish each descriptor before starting the next.
    """
    count = 0
    while True:
        # Fetch one pending row at a time — re-queries each iteration so we
        # never act on stale data (e.g., a row released by /online mid-batch).
        row = conn.execute(
            "SELECT content_address, healthcheck_address FROM registry "
            "WHERE takeover_container = ? AND takeover_pending IS NOT NULL "
            "AND status = 'taken-over' LIMIT 1",
            (CONTAINER_NAME,)
        ).fetchone()

        if not row:
            break

        ca = row["content_address"]
        ha = row["healthcheck_address"]
        log(f"takeover-worker: executing takeover for {ca}")
        ok = _takeover_local(ca)

        if ok:
            # Clear pending flag — takeover succeeded
            conn.execute(
                "UPDATE registry SET takeover_pending = NULL "
                "WHERE content_address = ? AND healthcheck_address = ?",
                (ca, ha)
            )
            db_commit_with_retry(conn)
            count += 1
            # Pause between services to let Tor publish each descriptor
            time.sleep(5)
        else:
            # Leave takeover_pending set — will retry next cycle
            log(f"takeover-worker: will retry {ca} next cycle")
            break  # stop this batch, retry after LOOP_INTERVAL

    if count > 0:
        update_active_count(conn)
        log(f"takeover-worker: processed {count} takeover(s)")
    return count


def process_releases(conn):
    """Process pending release requests."""
    count = 0
    while True:
        # Fetch one pending release at a time — fresh query each iteration.
        row = conn.execute(
            "SELECT content_address, healthcheck_address FROM registry "
            "WHERE takeover_container = ? AND release_pending IS NOT NULL LIMIT 1",
            (CONTAINER_NAME,)
        ).fetchone()

        if not row:
            break

        ca = row["content_address"]
        ha = row["healthcheck_address"]
        log(f"takeover-worker: executing release for {ca}")
        ok = _release_local(ca)

        if ok:
            # Clear pending and assignment flags
            conn.execute(
                "UPDATE registry SET release_pending = NULL "
                "WHERE content_address = ? AND healthcheck_address = ?",
                (ca, ha)
            )
            db_commit_with_retry(conn)
            count += 1
        else:
            log(f"takeover-worker: will retry release for {ca} next cycle")
            break

    if count > 0:
        update_active_count(conn)
        log(f"takeover-worker: processed {count} release(s)")
    return count


def _check_healthcheck(healthcheck_address):
    """Check if a healthcheck .onion address is reachable via local Tor SOCKS."""
    try:
        result = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             "--socks5-hostname", "127.0.0.1:9050",
             "--max-time", os.environ.get("ONIONHEAVEN_CURL_TIMEOUT", "8"),
             f"http://{healthcheck_address}/"],
            capture_output=True, text=True, timeout=15
        )
        http_code = result.stdout.strip()
        return result.returncode == 0 and http_code in ("200", "301")
    except Exception:
        return False


def process_audits(conn):
    """Process pending audit requests — one per cycle to avoid blocking takeovers."""
    row = conn.execute(
        "SELECT content_address, healthcheck_address FROM registry "
        "WHERE takeover_container = ? AND audit_pending IS NOT NULL LIMIT 1",
        (CONTAINER_NAME,)
    ).fetchone()

    if not row:
        return 0

    ca = row["content_address"]
    ha = row["healthcheck_address"]
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if _check_healthcheck(ha):
        # False positive — site is actually alive
        log(f"takeover-worker: FALSE POSITIVE: {ha} (content: {ca}) responded — releasing")
        conn.execute(
            "UPDATE registry SET audit_result = 'false_positive', audit_at = ?, "
            "audit_pending = NULL "
            "WHERE content_address = ? AND healthcheck_address = ?",
            (now_str, ca, ha)
        )
        db_commit_with_retry(conn)
        # Release via local tor-manager and update DB
        ok = _release_local(ca)
        if ok:
            conn.execute(
                "UPDATE registry SET status = 'online', last_released = ?, "
                "takeover_pending = NULL, release_pending = NULL "
                "WHERE content_address = ? AND healthcheck_address = ?",
                (now_str, ca, ha)
            )
            # Decrement assigned_count
            conn.execute(
                "UPDATE takeover_containers SET assigned_count = MAX(0, assigned_count - 1) "
                "WHERE container_name = ?",
                (CONTAINER_NAME,)
            )
            db_commit_with_retry(conn)
    else:
        # Confirmed dead — clear audit_pending
        conn.execute(
            "UPDATE registry SET audit_result = 'confirmed_dead', audit_at = ?, "
            "audit_pending = NULL "
            "WHERE content_address = ? AND healthcheck_address = ?",
            (now_str, ca, ha)
        )
        db_commit_with_retry(conn)

    return 1


def update_active_count(conn):
    """Update active_services count for this container."""
    count = conn.execute(
        "SELECT COUNT(DISTINCT content_address) FROM registry "
        "WHERE takeover_container = ? AND status = 'taken-over'",
        (CONTAINER_NAME,)
    ).fetchone()[0]
    conn.execute(
        "UPDATE takeover_containers SET active_services = ? WHERE container_name = ?",
        (count, CONTAINER_NAME)
    )
    db_commit_with_retry(conn)


def heartbeat(conn):
    """Update heartbeat timestamp."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "UPDATE takeover_containers SET last_heartbeat = ? WHERE container_name = ?",
        (now, CONTAINER_NAME)
    )
    db_commit_with_retry(conn)


def wait_for_db():
    """Wait for the shared DB directory to exist."""
    for _ in range(30):
        if os.path.isdir(ONIONHEAVEN_DATA_DIR):
            return True
        time.sleep(2)
    log("WARNING: data dir not found after 60s, creating it")
    os.makedirs(ONIONHEAVEN_DATA_DIR, exist_ok=True)
    return True


def wait_for_tor():
    """Wait for C Tor's control port to be ready (bootstrapped)."""
    log("takeover-worker: waiting for Tor to bootstrap...")
    for attempt in range(60):
        try:
            result = subprocess.run(
                ["sh", "-c",
                 'cookie=$(xxd -p /var/lib/tor/control_auth_cookie 2>/dev/null | tr -d "\\n"); '
                 '[ -n "$cookie" ] && printf "AUTHENTICATE %s\\r\\nGETINFO status/bootstrap-phase\\r\\nQUIT\\r\\n" '
                 '"$cookie" | nc -w 2 127.0.0.1 9051'],
                capture_output=True, text=True, timeout=10,
            )
            if "PROGRESS=100" in result.stdout:
                log("takeover-worker: Tor bootstrapped and ready")
                return True
        except Exception:
            pass
        time.sleep(2)
    log("WARNING: Tor not bootstrapped after 120s, proceeding anyway")
    return False


def main():
    log(f"takeover-worker starting: {CONTAINER_NAME}")

    wait_for_db()
    wait_for_tor()

    conn = db_connect()
    db_ensure_schema(conn)
    register_self(conn)
    startup_reconciliation(conn)
    conn.close()

    log(f"takeover-worker ready: {CONTAINER_NAME}")

    last_heartbeat = time.monotonic()

    while True:
        try:
            conn = db_connect()

            takeovers = process_takeovers(conn)
            releases = process_releases(conn)
            audits = process_audits(conn)

            # Periodic heartbeat
            now = time.monotonic()
            if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                heartbeat(conn)
                update_active_count(conn)
                last_heartbeat = now

            conn.close()
            time.sleep(LOOP_INTERVAL)

        except Exception as e:
            log(f"takeover-worker error: {e}")
            try:
                conn.close()
            except Exception:
                pass
            time.sleep(10)


if __name__ == "__main__":
    main()

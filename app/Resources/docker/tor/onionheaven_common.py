"""
OnionHeaven shared module — constants, DB schema, takeover/release functions.

Imported by both web-server.py and onionheaven-heartbeat.py to ensure
consistent schema and decision logic.

Takeover/release engine: the heartbeat is the single orchestrator. It picks a
bootstrapped worker container with the fewest addresses and executes takeover/
release directly via `docker exec`. No polling loops, no DB-mediated IPC.
Workers are just Tor instances — they receive commands, not requests.
"""

import json
import os
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ONIONHEAVEN_DATA_DIR = "/var/lib/onionpress/onionheaven"
DB_PATH = os.path.join(ONIONHEAVEN_DATA_DIR, "registry.db")
KEYS_DIR = os.path.join(ONIONHEAVEN_DATA_DIR, "keys")
TOR_MANAGER = "/onionheaven-tor-manager.sh"

# How long to wait after the last successful healthcheck before considering
# a node stale enough for takeover. Override via env for testing.
PROPAGATION_DELAY = int(os.environ.get("TOR_PROPAGATION_DELAY", "180"))

# How many missed heartbeats before considering takeover.
# With 60s heartbeat interval and 180s PROPAGATION_DELAY, this is ~3 missed beats.
CONSECUTIVE_FAILS_THRESHOLD = int(os.environ.get("ONIONHEAVEN_CONSECUTIVE_FAILS", "3"))

# Extra grace period (seconds) for OnionHeaven peers before takeover.
# Peers run OnionHeaven themselves, so a restart is expected to take longer.
# Default 30 minutes — a peer that's been down 30+ minutes is likely truly dead.
ONIONHEAVEN_PEER_GRACE = int(os.environ.get("ONIONHEAVEN_PEER_GRACE", "1800"))

# Container identity — set by entrypoint for takeover workers
CONTAINER_NAME = os.environ.get("CONTAINER_NAME", "")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

import collections
import threading

# Per-address log ring buffer — last 20 entries per address
_addr_logs = {}
_addr_logs_lock = threading.Lock()
_ADDR_LOG_MAX = 10


def log(msg):
    """Log to stderr with timestamp (captured by docker logs).

    Uses local time (not UTC) so timestamps match the host-side stress test
    logs and operator's clock. The container inherits /etc/localtime from
    the Colima VM, which mirrors the Mac's timezone.
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sys.stderr.write(f"[{ts}] OnionHeaven: {msg}\n")
    sys.stderr.flush()


def addr_log(address, msg):
    """Log a message and store it in the per-address ring buffer.

    Shows up in docker logs AND in /status/<address> responses.
    """
    log(msg)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{ts}] {msg}"
    with _addr_logs_lock:
        if address not in _addr_logs:
            _addr_logs[address] = collections.deque(maxlen=_ADDR_LOG_MAX)
        _addr_logs[address].append(entry)


def get_addr_logs(address):
    """Return recent log entries for an address."""
    with _addr_logs_lock:
        return list(_addr_logs.get(address, []))


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------

def db_connect():
    """Open OnionHeaven SQLite database with WAL mode."""
    os.makedirs(ONIONHEAVEN_DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def db_commit_with_retry(conn, max_retries=5, base_delay=0.5):
    """Commit with retry on 'database is locked' errors.

    The busy_timeout PRAGMA handles most contention, but under extreme load
    (800+ concurrent heartbeats) the timeout can still be exceeded.
    """
    for attempt in range(max_retries):
        try:
            conn.commit()
            return
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e) and attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                log(f"DB locked on commit, retry {attempt + 1}/{max_retries} in {delay:.1f}s")
                time.sleep(delay)
            else:
                raise


def db_ensure_schema(conn):
    """Create the registry table with the new schema.

    Starting fresh — no migration from the old fail_count/takeover_active schema.
    If the old table exists, drop it and recreate.
    """
    # Check if table exists with old schema
    cols = []
    try:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(registry)").fetchall()]
    except sqlite3.Error:
        pass

    if cols and ("fail_count" in cols or "takeover_active" in cols or "last_contact" in cols
                 or "last_polled" in cols):
        # Old schema detected — drop and recreate
        log("Old schema detected — dropping and recreating registry table")
        conn.execute("DROP TABLE registry")
        cols = []

    if not cols:
        conn.execute("""CREATE TABLE IF NOT EXISTS registry (
            content_address     TEXT NOT NULL,
            healthcheck_address TEXT NOT NULL,
            key_hash            TEXT,
            registered_at       TEXT NOT NULL,
            unregistered_at     TEXT,
            unregistered_reason TEXT,
            version             TEXT DEFAULT 'unknown',
            status              TEXT DEFAULT 'online',
            last_checked        TEXT,
            last_healthy        TEXT,
            last_released       TEXT,
            last_taken_over     TEXT,
            last_redirect       TEXT,
            takeover_container  TEXT,
            takeover_pending    TEXT,
            release_pending     TEXT,
            consecutive_fails   INTEGER DEFAULT 0,
            wordpress_healthy   INTEGER,
            wordpress_checked_at TEXT,
            audit_result        TEXT,
            audit_at            TEXT,
            audit_pending       TEXT,
            is_onionheaven      INTEGER DEFAULT 0,
            PRIMARY KEY (content_address, healthcheck_address)
        )""")
        db_commit_with_retry(conn)
    else:
        # Table exists — add any missing columns
        for col in [
            "unregistered_at", "unregistered_reason",
            "last_checked", "last_healthy", "last_released",
            "last_taken_over", "last_redirect",
            "takeover_container", "takeover_pending", "release_pending",
            "wordpress_checked_at", "audit_result", "audit_at", "audit_pending",
        ]:
            if col not in cols:
                conn.execute(f"ALTER TABLE registry ADD COLUMN {col} TEXT")
        if "consecutive_fails" not in cols:
            conn.execute("ALTER TABLE registry ADD COLUMN consecutive_fails INTEGER DEFAULT 0")
        if "wordpress_healthy" not in cols:
            conn.execute("ALTER TABLE registry ADD COLUMN wordpress_healthy INTEGER")
        if "is_onionheaven" not in cols:
            conn.execute("ALTER TABLE registry ADD COLUMN is_onionheaven INTEGER DEFAULT 0")
        db_commit_with_retry(conn)

    # Drop deprecated tables from old polling architecture
    conn.execute("DROP TABLE IF EXISTS poll_containers")
    conn.execute("DROP TABLE IF EXISTS farm_scale_requests")

    # Migrate takeover_containers to new schema if needed
    tc_cols = []
    try:
        tc_cols = [row[1] for row in conn.execute("PRAGMA table_info(takeover_containers)").fetchall()]
    except sqlite3.Error:
        pass

    if tc_cols and "bootstrapped" not in tc_cols:
        # Old schema — drop and recreate
        conn.execute("DROP TABLE takeover_containers")
        tc_cols = []

    if not tc_cols:
        conn.execute("""CREATE TABLE IF NOT EXISTS takeover_containers (
            container_name  TEXT PRIMARY KEY,
            max_services    INTEGER DEFAULT 50,
            bootstrapped    INTEGER DEFAULT 0,
            assigned_count  INTEGER DEFAULT 0,
            active_services INTEGER DEFAULT 0,
            last_heartbeat  TEXT,
            status          TEXT DEFAULT 'active',
            created_at      TEXT
        )""")
    else:
        # Add columns expected by takeover-worker (dropped in eafa80c refactor)
        if "active_services" not in tc_cols:
            conn.execute("ALTER TABLE takeover_containers ADD COLUMN active_services INTEGER DEFAULT 0")
        if "last_heartbeat" not in tc_cols:
            conn.execute("ALTER TABLE takeover_containers ADD COLUMN last_heartbeat TEXT")
        if "status" not in tc_cols:
            conn.execute("ALTER TABLE takeover_containers ADD COLUMN status TEXT DEFAULT 'active'")

    db_commit_with_retry(conn)


# ---------------------------------------------------------------------------
# Farm mode helpers
# ---------------------------------------------------------------------------

# Docker image for takeover workers (same as main tor image)
TAKEOVER_IMAGE = os.environ.get("ONIONHEAVEN_IMAGE",
                                "ghcr.io/brewsterkahle/onionpress-tor:latest")
MAX_SERVICES_PER_WORKER = int(os.environ.get("ONIONHEAVEN_MAX_SERVICES", "50"))

# Next index for naming new takeover containers
_next_worker_index = 0


def is_farm_mode():
    """True if we can manage takeover containers.

    True when ONIONHEAVEN=1 (heartbeat container) or when the docker
    socket is available (onionpress-tor server container).
    """
    if os.environ.get("ONIONHEAVEN") == "1":
        return True
    return os.path.exists("/var/run/docker.sock")


def _init_worker_index(conn):
    """Set _next_worker_index based on existing containers in DB."""
    global _next_worker_index
    try:
        rows = conn.execute(
            "SELECT container_name FROM takeover_containers"
        ).fetchall()
        max_idx = -1
        for row in rows:
            name = row["container_name"]
            # parse "onionheaven-takeover-N"
            try:
                idx = int(name.rsplit("-", 1)[-1])
                if idx > max_idx:
                    max_idx = idx
            except (ValueError, IndexError):
                pass
        _next_worker_index = max_idx + 1
    except sqlite3.OperationalError:
        _next_worker_index = 0


def _pick_worker(conn):
    """Pick the bootstrapped worker with the fewest addresses.

    Returns container_name or None if no bootstrapped workers exist.
    """
    try:
        row = conn.execute(
            "SELECT container_name FROM takeover_containers "
            "WHERE bootstrapped = 1 ORDER BY assigned_count ASC LIMIT 1"
        ).fetchone()
        return row["container_name"] if row else None
    except sqlite3.OperationalError:
        return None



def _ensure_capacity(conn):
    """If all bootstrapped workers are at max_services and fewer than 2
    containers are currently building, kick off 2 more.

    Called after each takeover assignment.
    """
    try:
        # Any bootstrapped worker under max_services? If so, no need to build.
        under_cap = conn.execute(
            "SELECT COUNT(*) FROM takeover_containers "
            "WHERE bootstrapped = 1 AND assigned_count < ?",
            (MAX_SERVICES_PER_WORKER,)
        ).fetchone()[0]
        if under_cap > 0:
            return

        # How many are currently building?
        building = conn.execute(
            "SELECT COUNT(*) FROM takeover_containers WHERE bootstrapped = 0"
        ).fetchone()[0]
        if building >= 2:
            return

        # Build 2 more
        to_build = 2 - building
        for _ in range(to_build):
            _spawn_worker(conn)
    except sqlite3.OperationalError:
        pass


def _spawn_worker(conn):
    """Start a new takeover worker container via docker run."""
    global _next_worker_index
    idx = _next_worker_index
    _next_worker_index += 1
    name = f"onionheaven-takeover-{idx}"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Remove any existing container with this name
    subprocess.run(["docker", "rm", "-f", name],
                   capture_output=True, timeout=15)

    tz = os.environ.get("TZ", "UTC")

    try:
        result = subprocess.run(
            ["docker", "run", "-d",
             "--name", name,
             "--network", "onionpress-network",
             "--ulimit", "nofile=10000:10000",
             "-e", f"TZ={tz}",
             "-e", "TAKEOVER_WORKER=1",
             "-e", f"CONTAINER_NAME={name}",
             "-e", f"MAX_TAKEOVER_SERVICES={MAX_SERVICES_PER_WORKER}",
             "-v", "onionpress-persistent-data:/var/lib/onionpress",
             "--restart", "unless-stopped",
             TAKEOVER_IMAGE],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            # Insert into DB as not-yet-bootstrapped
            conn.execute(
                "INSERT OR REPLACE INTO takeover_containers "
                "(container_name, max_services, bootstrapped, assigned_count, created_at) "
                "VALUES (?, ?, 0, 0, ?)",
                (name, MAX_SERVICES_PER_WORKER, now)
            )
            db_commit_with_retry(conn)
            log(f"Spawned takeover worker {name} (bootstrapping...)")
        else:
            log(f"WARNING: Failed to spawn {name}: {result.stderr.strip()}")
            _next_worker_index -= 1  # reuse index
    except Exception as e:
        log(f"ERROR spawning {name}: {e}")
        _next_worker_index -= 1


def _check_worker_process(container_name):
    """Check if the takeover-worker Python process is alive inside the container.

    Returns True if the process is running, False otherwise.
    Checks for the specific takeover-worker script, not just any python3.
    """
    try:
        result = subprocess.run(
            ["docker", "exec", container_name, "sh", "-c",
             "ps aux | grep '[o]nionheaven-takeover-worker\\.py' | grep -v grep"],
            capture_output=True, text=True, timeout=10
        )
        return result.returncode == 0 and result.stdout.strip() != ""
    except Exception:
        return False


def check_worker_bootstrap(conn):
    """Check worker health — bootstrap status and process liveness.

    Called each heartbeat cycle. For unbootstrapped workers: checks Tor
    bootstrap and queue manager. For all workers: verifies the takeover-worker
    process is alive and warns loudly if not.
    """
    now = datetime.now(timezone.utc)
    now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        rows = conn.execute(
            "SELECT container_name, bootstrapped, created_at, last_heartbeat FROM takeover_containers"
        ).fetchall()
    except sqlite3.OperationalError:
        return

    for row in rows:
        name = row["container_name"]
        bootstrapped = row["bootstrapped"]
        created_at = row["created_at"]

        # --- Check unbootstrapped workers ---
        if not bootstrapped:
            # Warn if stuck unbootstrapped for >2 minutes
            if created_at:
                try:
                    ct = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    age = (now - ct).total_seconds()
                    if age > 120:
                        log(f"WARNING: worker {name} has been unbootstrapped for {int(age)}s — "
                            f"check container logs: docker logs {name}")
                except (ValueError, TypeError):
                    pass

            try:
                result = subprocess.run(
                    ["docker", "exec", name, "sh", "-c",
                     "COOKIE=$(xxd -p -c 64 /var/lib/tor/control_auth_cookie 2>/dev/null) && "
                     "printf 'AUTHENTICATE %s\\r\\nGETINFO status/bootstrap-phase\\r\\nQUIT\\r\\n' "
                     "\"$COOKIE\" | nc -q 1 127.0.0.1 9051 2>/dev/null || true"],
                    capture_output=True, text=True, timeout=10
                )
                if "PROGRESS=100" in result.stdout:
                    # Also verify queue manager is accepting commands
                    qm_result = subprocess.run(
                        ["docker", "exec", name,
                         "python3", "/onionheaven-queue-manager.py", "status"],
                        capture_output=True, text=True, timeout=10
                    )
                    if qm_result.returncode != 0 or not qm_result.stdout.strip():
                        continue
                    try:
                        qm_status = json.loads(qm_result.stdout.strip())
                        if "error" in qm_status:
                            continue
                    except (json.JSONDecodeError, ValueError):
                        continue

                    conn.execute(
                        "UPDATE takeover_containers SET bootstrapped = 1 "
                        "WHERE container_name = ?",
                        (name,)
                    )
                    db_commit_with_retry(conn)
                    log(f"Takeover worker {name} is bootstrapped and ready (Tor + queue manager)")
            except Exception:
                pass
            continue

        # --- Check bootstrapped workers: is the worker process alive? ---
        if not _check_worker_process(name):
            log(f"ERROR: worker {name} container is running but takeover-worker process is DEAD — "
                f"takeovers/audits will not be processed. Check logs: docker logs {name}")
            # Check if it has been dead for a while (no heartbeat update)
            last_hb = row["last_heartbeat"]
            if last_hb:
                try:
                    lh = datetime.fromisoformat(last_hb.replace("Z", "+00:00"))
                    dead_for = (now - lh).total_seconds()
                    if dead_for > 300:
                        log(f"ERROR: worker {name} has had no heartbeat for {int(dead_for)}s — "
                            f"consider restarting: docker restart {name}")
                except (ValueError, TypeError):
                    pass


def cleanup_dead_workers(conn):
    """Remove DB entries for workers whose containers no longer exist.

    Called each heartbeat cycle.
    """
    try:
        rows = conn.execute(
            "SELECT container_name FROM takeover_containers"
        ).fetchall()
    except sqlite3.OperationalError:
        return

    for row in rows:
        name = row["container_name"]
        try:
            result = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Running}}", name],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0 or "true" not in result.stdout.lower():
                # Container dead — clear assignments and remove
                conn.execute(
                    "UPDATE registry SET takeover_container = NULL "
                    "WHERE takeover_container = ? AND status = 'taken-over'",
                    (name,)
                )
                conn.execute(
                    "DELETE FROM takeover_containers WHERE container_name = ?",
                    (name,)
                )
                db_commit_with_retry(conn)
                log(f"Cleaned up dead worker {name}")
        except Exception:
            pass


def _exec_takeover(container_name, content_address):
    """Queue takeover on a worker via the queue manager.

    The queue manager rate-limits ADD_ONION calls and monitors descriptor
    propagation. Returns True if the address was queued successfully.
    """
    log(f"Queuing takeover of {content_address} on {container_name}")
    try:
        result = subprocess.run(
            ["docker", "exec", container_name,
             "python3", "/onionheaven-queue-manager.py", "takeover", content_address],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0 and result.stdout.strip():
            try:
                resp = json.loads(result.stdout.strip())
                status = resp.get("status", "")
                if status in ("queued", "already_queued", "already_active", "in_flight"):
                    addr_log(content_address, f"Takeover queued: {content_address} on {container_name} ({status})")
                    return True
                elif "error" in resp:
                    log(f"Takeover queue failed for {content_address} on {container_name}: "
                        f"{resp['error']}")
                    return False
                else:
                    log(f"Takeover unexpected response for {content_address} on {container_name}: "
                        f"{resp}")
                    return False
            except (json.JSONDecodeError, ValueError):
                log(f"Takeover unparseable response for {content_address} on {container_name}: "
                    f"{result.stdout.strip()[:200]}")
                return False
        else:
            log(f"Takeover queue failed for {content_address} on {container_name}: "
                f"exit={result.returncode} stderr={result.stderr.strip()[:200]}")
            return False
    except Exception as e:
        log(f"Takeover error for {content_address} on {container_name}: {e}")
        return False


def _exec_release(container_name, content_address):
    """Release on a worker via the queue manager.

    Uses the worker's own Tor (GETINFO onions/{detached,current} via the
    queue-manager's `has` command) as the source of truth, because the
    queue-manager's reported release status alone is unreliable: a
    `release_warning` means DEL_ONION explicitly failed, and a `not_found`
    can mask orphans the queue-manager forgot about (e.g. after a daemon
    restart cleared its in-memory active set while Tor kept the detached
    service). Both produced returncode 0 under the previous returncode-only
    contract, which left orphaned takeover services serving redirects long
    after the registry said "released".

    Returns True on verified success, False otherwise.
    """
    addr_log(content_address, f"Releasing {content_address} via {container_name}")

    try:
        result = subprocess.run(
            ["docker", "exec", container_name,
             "python3", "/onionheaven-queue-manager.py", "release", content_address],
            capture_output=True, text=True, timeout=30
        )
    except Exception as e:
        log(f"Release error for {content_address} on {container_name}: {e}")
        return False

    if result.returncode != 0:
        log(f"Release subprocess failed for {content_address} on {container_name}: "
            f"rc={result.returncode} stderr={result.stderr.strip()[:200]}")
        return False

    reported = None
    try:
        reported = json.loads(result.stdout.strip() or "{}").get("status")
    except (json.JSONDecodeError, AttributeError):
        log(f"Release: non-JSON response from {container_name} for "
            f"{content_address}: {result.stdout.strip()[:200]}")

    has_after = _has_onion_on_worker(container_name, content_address)
    if has_after is True:
        log(f"Release FAILED-VERIFY: {content_address} still active on "
            f"{container_name} (queue-manager reported={reported})")
        return False
    if has_after is False:
        addr_log(content_address,
                 f"Release complete: {content_address} on {container_name} "
                 f"(reported={reported})")
        return True

    # Verify probe itself failed (e.g. worker still on an older image with
    # no `has` command). Fall back to the queue-manager's reported status,
    # but treat release_warning as the failure it is.
    if reported in ("released", "not_found", "removed_from_queue"):
        addr_log(content_address,
                 f"Release complete: {content_address} on {container_name} "
                 f"(reported={reported}, verify probe unavailable)")
        return True
    log(f"Release failed for {content_address} on {container_name}: "
        f"reported={reported}, verify probe unavailable")
    return False


def _has_onion_on_worker(container_name, content_address):
    """Independently probe whether a worker's Tor still has the onion.

    Returns True/False on success, or None if the probe itself fails
    (e.g. docker exec error, worker on older image without `has`).
    """
    try:
        result = subprocess.run(
            ["docker", "exec", container_name,
             "python3", "/onionheaven-queue-manager.py", "has", content_address],
            capture_output=True, text=True, timeout=10
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout.strip() or "{}")
    except (json.JSONDecodeError, AttributeError):
        return None
    if not isinstance(data, dict) or "error" in data or "has_onion" not in data:
        return None
    return bool(data["has_onion"])


def get_queue_status(container_name):
    """Get queue status from a worker's queue manager.

    Returns dict with queued/in_flight/active counts, or None on error.
    """
    try:
        result = subprocess.run(
            ["docker", "exec", container_name,
             "python3", "/onionheaven-queue-manager.py", "status"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return json.loads(result.stdout.strip())
    except Exception:
        pass
    return None


def get_addr_serving_status(conn, content_address):
    """Get the serving_status for a content_address.

    Returns one of: registered, queued-to-activate, activating, active, failed, unknown.
    """
    row = conn.execute(
        "SELECT status, takeover_container FROM registry "
        "WHERE content_address = ? AND unregistered_at IS NULL LIMIT 1",
        (content_address,)
    ).fetchone()

    if not row:
        return "unknown"

    if row["status"] == "online":
        return "registered"

    if row["status"] != "taken-over":
        return "unknown"

    container = row["takeover_container"]
    if not container:
        return "queued-to-activate"  # taken-over but no worker assigned yet

    # Ask the worker's queue manager
    try:
        result = subprocess.run(
            ["docker", "exec", container,
             "python3", "/onionheaven-queue-manager.py", "addr_state", content_address],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            resp = json.loads(result.stdout.strip())
            state = resp.get("serving_status")
            if state:
                return state
    except Exception:
        pass

    return "activating"  # fallback — assigned but can't reach worker


# ---------------------------------------------------------------------------
# Takeover function
# ---------------------------------------------------------------------------

def takeover_function(conn, content_address, healthcheck_address, force=False):
    """Handle takeover for a single registry row.

    1. Mark the row status='taken-over' and record last_taken_over.
    2. Check guards:
       - No OTHER row for same content_address already has status='taken-over'
       - No other row for content_address has status='online'
       - last_healthy is stale (now - last_healthy > PROPAGATION_DELAY) OR force=True
    3. Pick the bootstrapped worker with fewest addresses → docker exec takeover.
    4. If all workers are at max_services and <2 building, spawn 2 more.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    row = conn.execute(
        "SELECT * FROM registry WHERE content_address = ? AND healthcheck_address = ?",
        (content_address, healthcheck_address)
    ).fetchone()

    if not row:
        log(f"ERROR: takeover_function called for non-existent row: "
            f"{content_address} / {healthcheck_address}")
        return

    if row["status"] != "online" and not force:
        return

    # Mark this row as taken-over
    conn.execute(
        "UPDATE registry SET status = 'taken-over', last_taken_over = ? "
        "WHERE content_address = ? AND healthcheck_address = ?",
        (now, content_address, healthcheck_address)
    )
    db_commit_with_retry(conn)
    log(f"Marked {healthcheck_address} as taken-over for {content_address}")

    # Guards: should we actually start serving the onion service?
    already_serving = conn.execute(
        "SELECT COUNT(*) FROM registry "
        "WHERE content_address = ? AND healthcheck_address != ? AND status = 'taken-over'",
        (content_address, healthcheck_address)
    ).fetchone()[0] > 0

    if already_serving:
        log(f"Skipping takeover for {content_address} — another row already taken-over")
        return

    no_other_online = conn.execute(
        "SELECT COUNT(*) FROM registry "
        "WHERE content_address = ? AND healthcheck_address != ? AND status = 'online'",
        (content_address, healthcheck_address)
    ).fetchone()[0] == 0

    if not no_other_online:
        log(f"Skipping takeover for {content_address} — other row(s) still online")
        return

    # Check propagation delay
    last_healthy_stale = True
    if row["last_healthy"] and not force:
        try:
            lh = datetime.fromisoformat(row["last_healthy"].replace("Z", "+00:00"))
            now_dt = datetime.now(timezone.utc)
            elapsed = (now_dt - lh).total_seconds()
            last_healthy_stale = elapsed > PROPAGATION_DELAY
        except (ValueError, TypeError):
            last_healthy_stale = True

    if not last_healthy_stale:
        log(f"Skipping takeover for {content_address} — last_healthy not yet stale")
        return

    # All guards pass — execute takeover on a worker.
    # Prefer the worker this address was previously on (stored in
    # takeover_container even after release) to avoid orphaned ADD_ONION
    # services when takeover/release cycles bounce between workers.
    if is_farm_mode():
        preferred = row["takeover_container"] if row["takeover_container"] else None
        if preferred:
            # Verify the preferred worker is still bootstrapped
            ok = conn.execute(
                "SELECT 1 FROM takeover_containers WHERE container_name = ? AND bootstrapped = 1",
                (preferred,)
            ).fetchone()
            worker = preferred if ok else _pick_worker(conn)
        else:
            worker = _pick_worker(conn)
        if worker:
            success = _exec_takeover(worker, content_address)
            if success:
                conn.execute(
                    "UPDATE registry SET takeover_container = ?, takeover_pending = NULL "
                    "WHERE content_address = ? AND healthcheck_address = ?",
                    (worker, content_address, healthcheck_address)
                )
                conn.execute(
                    "UPDATE takeover_containers SET assigned_count = assigned_count + 1 "
                    "WHERE container_name = ?",
                    (worker,)
                )
                db_commit_with_retry(conn)
            # Check if we need more capacity
            _ensure_capacity(conn)
            return
        else:
            # No bootstrapped workers — spawn 2 if not already building
            _ensure_capacity(conn)
            addr_log(content_address, f"Takeover deferred for {content_address} — no bootstrapped workers yet")
            return

    # Legacy fallback — only safe inside takeover worker containers.
    if os.environ.get("TAKEOVER_WORKER") == "1":
        _takeover_local(content_address)
    else:
        log(f"ERROR: takeover_function fell through to legacy mode for "
            f"{content_address} — not farm mode and not a takeover worker.")


def _takeover_local(content_address):
    """Execute takeover via local tor-manager (takeover worker containers only).

    ADD_ONION via the control port — no config reload involved.
    """
    log(f"Taking over {content_address} via tor-manager (local)")
    try:
        result = subprocess.run(
            [TOR_MANAGER, "takeover", content_address],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            log(f"Takeover complete for {content_address}")
            return True
        else:
            log(f"Takeover failed for {content_address}: {result.stderr.strip()}")
            return False
    except Exception as e:
        log(f"Takeover error for {content_address}: {e}")
        return False


# ---------------------------------------------------------------------------
# Release function
# ---------------------------------------------------------------------------

def release_function(conn, content_address, healthcheck_address):
    """Handle release for a single registry row.

    Owns the status change: checks if taken-over, marks online, does DEL_ONION,
    decrements assigned_count. If already online, returns early — prevents
    double-release and double-decrement.
    """
    row = conn.execute(
        "SELECT * FROM registry WHERE content_address = ? AND healthcheck_address = ?",
        (content_address, healthcheck_address)
    ).fetchone()

    if not row:
        return

    if row["status"] != "taken-over":
        return

    takeover_container = row["takeover_container"] if "takeover_container" in row.keys() else None
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Mark this row as online — release_function owns this status change.
    # Keep takeover_container so the next takeover reuses the same worker,
    # preventing orphaned ADD_ONION services on other workers.
    conn.execute(
        "UPDATE registry SET status = 'online', last_released = ?, "
        "takeover_pending = NULL, release_pending = NULL, "
        "audit_pending = NULL, audit_result = NULL, audit_at = NULL "
        "WHERE content_address = ? AND healthcheck_address = ?",
        (now, content_address, healthcheck_address)
    )
    db_commit_with_retry(conn)
    addr_log(content_address, f"Released {content_address} (was on {takeover_container or 'none'})")

    # Execute DEL_ONION on the tracked worker and decrement assigned_count
    if takeover_container:
        success = _exec_release(takeover_container, content_address)
        if success:
            conn.execute(
                "UPDATE takeover_containers SET assigned_count = MAX(0, assigned_count - 1) "
                "WHERE container_name = ?",
                (takeover_container,)
            )
            db_commit_with_retry(conn)

    # Safety sweep: DEL_ONION from ALL other takeover containers.
    # A takeover can be queued on multiple workers (e.g. during rapid
    # takeover/release cycles), but the DB only tracks one takeover_container.
    # Without this sweep, orphaned services on other workers keep publishing
    # stale descriptors indefinitely.
    all_workers = conn.execute(
        "SELECT container_name FROM takeover_containers"
    ).fetchall()
    sweep_problems = []
    for w in all_workers:
        wname = w["container_name"] if isinstance(w, dict) else w[0]
        if wname == takeover_container:
            continue  # already handled above
        if not _exec_release(wname, content_address):
            sweep_problems.append(wname)
    if sweep_problems:
        # These workers reported a release-time failure or still had the
        # onion on the verify probe — surface them so the address-level
        # log shows the silent-orphan case rather than hiding it.
        addr_log(content_address,
                 f"Safety sweep problems for {content_address} on: "
                 f"{', '.join(sweep_problems)} — may still serve redirects")

    if not takeover_container:
        # Legacy fallback — only inside takeover worker containers
        if os.environ.get("TAKEOVER_WORKER") == "1":
            _release_local(content_address)


def _release_local(content_address):
    """Execute release via local tor-manager (takeover worker containers only).

    DEL_ONION via the control port — no config reload involved.
    """
    log(f"Releasing {content_address} via tor-manager (local)")
    try:
        result = subprocess.run(
            [TOR_MANAGER, "release", content_address],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            log(f"Release complete for {content_address}")
            return True
        else:
            log(f"Release failed for {content_address}: {result.stderr.strip()}")
            return False
    except Exception as e:
        log(f"Release error for {content_address}: {e}")
        return False


# ---------------------------------------------------------------------------
# Unregister function
# ---------------------------------------------------------------------------

def unregister_entry(conn, content_address, healthcheck_address, reason="unregistered"):
    """Fully unregister an entry: release from worker, mark unregistered, delete keys.

    This is the single path for all cleanup — called by /unregister handler
    and by implicit unregistration (superseded by new healthcheck, stale
    stress test cleanup, etc.).
    """
    import shutil

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Release from worker if taken-over (DEL_ONION + decrement)
    release_function(conn, content_address, healthcheck_address)

    # Mark as unregistered
    conn.execute(
        "UPDATE registry SET unregistered_at = ?, "
        "unregistered_reason = ?, status = 'unregistered' "
        "WHERE content_address = ? AND healthcheck_address = ?",
        (now, reason, content_address, healthcheck_address)
    )
    db_commit_with_retry(conn)

    # Delete key directory if no other active rows use this content_address
    other_active = conn.execute(
        "SELECT COUNT(*) FROM registry WHERE content_address = ? "
        "AND unregistered_at IS NULL",
        (content_address,)
    ).fetchone()[0]

    if other_active == 0:
        key_dir = os.path.join(KEYS_DIR, content_address)
        if os.path.isdir(key_dir):
            shutil.rmtree(key_dir, ignore_errors=True)
            log(f"Deleted keys for {content_address}")


# ---------------------------------------------------------------------------
# Stress test cleanup — remove only stress test entries, preserve real ones
# ---------------------------------------------------------------------------

def cleanup_stress_tests(conn=None):
    """Remove all stress test entries and release their takeovers.

    Only touches registry rows where version LIKE 'stress-test%'.
    Real entries and their keys are never deleted. Takeover workers are
    removed only if they have no remaining (real) assignments.

    Returns a dict summarising what was cleaned up.
    """
    import shutil

    own_conn = conn is None
    if own_conn:
        conn = db_connect()
        db_ensure_schema(conn)

    stats = {
        "registry_deleted": 0,
        "released": 0,
        "keys_deleted": 0,
        "workers_removed": 0,
    }

    # 1. Find all stress test entries
    stress_rows = conn.execute(
        "SELECT content_address, healthcheck_address, status, takeover_container "
        "FROM registry WHERE version LIKE 'stress-test%'"
    ).fetchall()

    stats["registry_deleted"] = len(stress_rows)
    if not stress_rows:
        log("cleanup_stress_tests: no stress test entries found")
        if own_conn:
            conn.close()
        return stats

    # 2. Release taken-over entries from their workers
    for row in stress_rows:
        if row["status"] == "taken-over" and row["takeover_container"]:
            container = row["takeover_container"]
            ca = row["content_address"]
            try:
                _exec_release(container, ca)
                stats["released"] += 1
            except Exception as e:
                log(f"cleanup_stress_tests: release failed for {ca} on {container}: {e}")
            # Decrement assigned_count
            conn.execute(
                "UPDATE takeover_containers SET assigned_count = MAX(0, assigned_count - 1) "
                "WHERE container_name = ?",
                (container,)
            )

    # 3. Delete stress test rows from registry
    conn.execute("DELETE FROM registry WHERE version LIKE 'stress-test%'")
    db_commit_with_retry(conn)
    log(f"cleanup_stress_tests: deleted {stats['registry_deleted']} registry rows")

    # 4. Delete keys that belong ONLY to stress test content addresses.
    #    A key is safe to delete only if no real entry uses the same content_address.
    stress_addrs = set(row["content_address"] for row in stress_rows)
    for ca in stress_addrs:
        real_refs = conn.execute(
            "SELECT COUNT(*) FROM registry WHERE content_address = ? "
            "AND version NOT LIKE 'stress-test%'",
            (ca,)
        ).fetchone()[0]
        if real_refs == 0:
            key_dir = os.path.join(KEYS_DIR, ca)
            if os.path.isdir(key_dir):
                shutil.rmtree(key_dir, ignore_errors=True)
                stats["keys_deleted"] += 1

    if stats["keys_deleted"]:
        log(f"cleanup_stress_tests: deleted {stats['keys_deleted']} key directories")

    # 5. Remove takeover workers that are now empty (assigned_count = 0).
    #    Also discover orphaned workers not in the DB.
    to_remove = set()
    try:
        empty_workers = conn.execute(
            "SELECT container_name FROM takeover_containers WHERE assigned_count <= 0"
        ).fetchall()
        for w in empty_workers:
            to_remove.add(w["container_name"])
            conn.execute(
                "DELETE FROM takeover_containers WHERE container_name = ?",
                (w["container_name"],)
            )
        db_commit_with_retry(conn)
    except sqlite3.OperationalError:
        pass

    # Also discover orphaned takeover containers not tracked in DB
    try:
        db_workers = set()
        try:
            rows = conn.execute("SELECT container_name FROM takeover_containers").fetchall()
            db_workers = set(r["container_name"] for r in rows)
        except sqlite3.OperationalError:
            pass

        result = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            for name in result.stdout.strip().splitlines():
                if name.startswith("onionheaven-takeover-") and name not in db_workers:
                    to_remove.add(name)
    except Exception:
        pass

    # Remove all in parallel
    if to_remove:
        def _rm(name):
            subprocess.run(["docker", "rm", "-f", name],
                           capture_output=True, timeout=15)
            return name
        with ThreadPoolExecutor(max_workers=len(to_remove)) as pool:
            for _ in as_completed({pool.submit(_rm, n): n for n in to_remove}):
                stats["workers_removed"] += 1
        log(f"cleanup_stress_tests: removed {stats['workers_removed']} workers")

    if own_conn:
        conn.close()

    log(f"cleanup_stress_tests complete: {stats}")
    return stats


# ---------------------------------------------------------------------------
# Reset OnionHeaven — clean stress tests + refresh workers with current code
# ---------------------------------------------------------------------------

# Files to copy from the onionheaven container into takeover workers.
# These are the Python/shell files that workers execute.
_WORKER_CODE_FILES = [
    "/onionheaven_common.py",
    "/onionheaven-queue-manager.py",
    "/onionheaven-takeover-worker.py",
    "/onionheaven-tor-manager.sh",
    "/onionheaven-redirect.sh",
    "/onion_auth.py",
    "/key-convert.py",
]


def _patch_worker_code(container_name):
    """Copy current code files from this container into a takeover worker.

    Uses docker cp via the host socket. The onionheaven container is the
    source of truth — it has the latest code (either from the image or from
    docker cp during development).
    """
    # We're running inside the onionheaven container, so we copy from local
    # filesystem into the target container via `docker cp`.
    failed = []
    for fpath in _WORKER_CODE_FILES:
        if not os.path.isfile(fpath):
            continue
        try:
            result = subprocess.run(
                ["docker", "cp", fpath, f"{container_name}:{fpath}"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                failed.append(fpath)
        except Exception:
            failed.append(fpath)

    if failed:
        log(f"_patch_worker_code({container_name}): failed to copy: {failed}")
    else:
        log(f"_patch_worker_code({container_name}): patched {len(_WORKER_CODE_FILES)} files")


def _rotate_logs():
    """Rotate OnionHeaven log files: rename current logs with a timestamp,
    delete logs older than 7 days.

    Covers heartbeat.log and queue-manager-*.log on the shared volume.
    """
    import glob as _glob

    log_dir = ONIONHEAVEN_DATA_DIR
    now_str = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    rotated = 0
    deleted = 0

    # Rotate current logs
    patterns = [
        os.path.join(log_dir, "heartbeat.log"),
        os.path.join(log_dir, "queue-manager-*.log"),
    ]
    for pattern in patterns:
        for path in _glob.glob(pattern):
            # Don't rotate already-rotated files (they have a date suffix)
            basename = os.path.basename(path)
            if basename.count(".") > 1:
                continue
            name, ext = os.path.splitext(path)
            rotated_path = f"{name}.{now_str}{ext}"
            try:
                os.rename(path, rotated_path)
                rotated += 1
            except OSError:
                pass

    # Delete old rotated logs (older than 7 days)
    cutoff = time.time() - (7 * 86400)
    old_patterns = [
        os.path.join(log_dir, "heartbeat.*.log"),
        os.path.join(log_dir, "queue-manager-*.*.log"),
    ]
    for pattern in old_patterns:
        for path in _glob.glob(pattern):
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    deleted += 1
            except OSError:
                pass

    if rotated or deleted:
        log(f"_rotate_logs: rotated {rotated}, deleted {deleted} old logs")


def reset_onionheaven(conn=None):
    """Clean stress tests and refresh all takeover workers with current code.

    Steps:
      1. Tear down ALL old takeover workers and clear assignments FIRST
         (prevents heartbeat from assigning to doomed workers during cleanup)
      2. Remove all stress test entries (registry rows, keys, takeovers)
      3. Collect real taken-over entries that need to be migrated
      4. Spawn fresh workers, patch them with current code, wait for bootstrap
      5. Re-assign real taken-over entries to the new workers

    Returns a dict summarising what happened.
    """
    global _next_worker_index

    own_conn = conn is None
    if own_conn:
        conn = db_connect()
        db_ensure_schema(conn)

    stats = {
        "stress_cleaned": 0,
        "real_migrated": 0,
        "old_workers_removed": 0,
        "new_workers_spawned": 0,
        "duplicate_heartbeats_killed": 0,
    }

    # Step 0: always restart the heartbeat so it picks up updated code.
    # Also eliminates any duplicate heartbeat processes.
    try:
        subprocess.run(
            ["docker", "exec", "onionheaven", "pkill", "-f", "onionheaven-heartbeat"],
            capture_output=True, timeout=10
        )
        time.sleep(2)
        subprocess.run(
            ["docker", "exec", "-d", "onionheaven", "sh", "-c",
             "python3 /onionheaven-heartbeat.py "
             "2>>/var/lib/onionpress/onionheaven/heartbeat.log"],
            capture_output=True, timeout=10
        )
        log("reset_onionheaven: restarted heartbeat process")
    except Exception as e:
        log(f"reset_onionheaven: heartbeat restart error: {e}")

    # Step 0.5: rotate log files so each test run has a clean slate.
    _rotate_logs()

    # Step 1: tear down ALL workers and clear assignments FIRST.
    # This must happen before cleanup_stress_tests so the heartbeat can't
    # assign new takeovers to workers that are about to be removed.

    # Clear ALL takeover_container assignments so heartbeat knows nothing
    # is assigned (prevents it from skipping unassigned entries).
    conn.execute(
        "UPDATE registry SET takeover_container = NULL "
        "WHERE takeover_container IS NOT NULL"
    )
    conn.execute("DELETE FROM takeover_containers")
    db_commit_with_retry(conn)

    # Remove all takeover containers (DB-tracked + orphaned)
    old_workers = set()
    try:
        result = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            for name in result.stdout.strip().splitlines():
                if name.startswith("onionheaven-takeover-"):
                    old_workers.add(name)
    except Exception:
        pass

    if old_workers:
        def _rm(name):
            subprocess.run(["docker", "rm", "-f", name],
                           capture_output=True, timeout=15)
            return name
        with ThreadPoolExecutor(max_workers=len(old_workers)) as pool:
            for name in as_completed(
                {pool.submit(_rm, n): n for n in old_workers}
            ):
                stats["old_workers_removed"] += 1
        log(f"reset_onionheaven: removed {stats['old_workers_removed']} workers")

    _next_worker_index = 0

    # Step 2: clean stress tests (safe now — no workers to race with)
    cleanup_stats = cleanup_stress_tests(conn)
    stats["stress_cleaned"] = cleanup_stats["registry_deleted"]

    # Step 3: collect real taken-over entries that need migration
    real_takeovers = conn.execute(
        "SELECT content_address, healthcheck_address, takeover_container "
        "FROM registry WHERE status = 'taken-over' AND unregistered_at IS NULL "
        "AND (version IS NULL OR version NOT LIKE 'stress-test%')"
    ).fetchall()

    # If no real takeovers to migrate, we're done — heartbeat will spawn
    # workers on demand when new takeovers happen.
    if not real_takeovers:
        log(f"reset_onionheaven: no real takeovers to migrate")
        if own_conn:
            conn.close()
        log(f"reset_onionheaven complete: {stats}")
        return stats

    # Step 4: spawn fresh workers, patch code, wait for bootstrap
    # Figure out how many workers we need
    needed = (len(real_takeovers) + MAX_SERVICES_PER_WORKER - 1) // MAX_SERVICES_PER_WORKER
    needed = max(needed, 1)
    # Spawn in pairs (as _ensure_capacity does)
    to_spawn = needed + (needed % 2)

    new_worker_names = []
    for _ in range(to_spawn):
        idx = _next_worker_index
        _spawn_worker(conn)
        name = f"onionheaven-takeover-{idx}"
        # Check if it was actually created
        row = conn.execute(
            "SELECT container_name FROM takeover_containers WHERE container_name = ?",
            (name,)
        ).fetchone()
        if row:
            new_worker_names.append(name)
            stats["new_workers_spawned"] += 1

    # Patch code into new workers
    for name in new_worker_names:
        _patch_worker_code(name)

    # Wait for at least one worker to bootstrap (up to 90s)
    log(f"reset_onionheaven: waiting for {len(new_worker_names)} workers to bootstrap...")
    ready_worker = None
    for _ in range(18):  # 18 * 5s = 90s
        check_worker_bootstrap(conn)
        ready_worker = _pick_worker(conn)
        if ready_worker:
            break
        time.sleep(5)

    if not ready_worker:
        log("reset_onionheaven: WARNING — no workers bootstrapped in 90s, "
            "real takeovers will be re-assigned by heartbeat later")
        if own_conn:
            conn.close()
        log(f"reset_onionheaven complete: {stats}")
        return stats

    # Step 5: re-assign real takeovers to new workers
    for row in real_takeovers:
        ca = row["content_address"]
        ha = row["healthcheck_address"]
        worker = _pick_worker(conn)
        if not worker:
            log(f"reset_onionheaven: no worker available for {ca}, deferring to heartbeat")
            break
        if _exec_takeover(worker, ca):
            conn.execute(
                "UPDATE registry SET takeover_container = ? "
                "WHERE content_address = ? AND healthcheck_address = ?",
                (worker, ca, ha)
            )
            conn.execute(
                "UPDATE takeover_containers SET assigned_count = assigned_count + 1 "
                "WHERE container_name = ?",
                (worker,)
            )
            stats["real_migrated"] += 1
    db_commit_with_retry(conn)

    if own_conn:
        conn.close()

    log(f"reset_onionheaven complete: {stats}")
    return stats

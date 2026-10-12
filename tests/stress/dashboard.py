#!/usr/bin/env python3
"""OnionHeaven live dashboard — standalone registry monitor.

Queries the registry via docker exec (local mode). Shows current values
and deltas since the last refresh.

Usage:
    python3 tests/stress/dashboard.py                          # local (Pi)
    python3 tests/stress/dashboard.py --interval 5             # faster refresh
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime


# ---------------------------------------------------------------------------
# Docker helpers
# ---------------------------------------------------------------------------

def docker_exec(container, cmd, timeout=10):
    """Run a command inside a container, return stdout or None on failure."""
    try:
        result = subprocess.run(
            ["docker", "exec", container, "sh", "-c", cmd],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, Exception):
        pass
    return None


def docker_run(args, timeout=10):
    """Run a docker command, return stdout or None on failure."""
    try:
        result = subprocess.run(
            ["docker"] + args,
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, Exception):
        pass
    return None


# ---------------------------------------------------------------------------
# Local queries (docker exec + sqlite3)
# ---------------------------------------------------------------------------

DB = "/var/lib/onionheaven/registry.db"


def query_registry():
    sql = (
        "SELECT "
        "  SUM(CASE WHEN status='online' AND unregistered_at IS NULL THEN 1 ELSE 0 END), "
        "  SUM(CASE WHEN status='taken-over' AND unregistered_at IS NULL THEN 1 ELSE 0 END), "
        "  SUM(CASE WHEN unregistered_at IS NOT NULL THEN 1 ELSE 0 END), "
        "  SUM(CASE WHEN unregistered_at IS NULL THEN 1 ELSE 0 END) "
        "FROM registry;"
    )
    out = docker_exec("onionheaven", f"sqlite3 {DB} \"{sql}\"")
    if not out:
        return None
    parts = out.split("|")
    if len(parts) < 4:
        return None
    return {
        "online": int(parts[0] or 0),
        "taken_over": int(parts[1] or 0),
        "unregistered": int(parts[2] or 0),
        "active": int(parts[3] or 0),
    }


def query_versions():
    sql = (
        "SELECT version, COUNT(*) FROM registry "
        "WHERE unregistered_at IS NULL GROUP BY version ORDER BY COUNT(*) DESC LIMIT 5;"
    )
    out = docker_exec("onionheaven", f"sqlite3 {DB} \"{sql}\"")
    if not out:
        return []
    versions = []
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) == 2:
            versions.append((parts[0], int(parts[1])))
    return versions


def query_workers():
    sql = (
        "SELECT container_name, assigned_count, bootstrapped, max_services "
        "FROM takeover_containers ORDER BY container_name;"
    )
    out = docker_exec("onionheaven", f"sqlite3 {DB} \"{sql}\"")
    if not out:
        return []
    workers = []
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) >= 4:
            workers.append({
                "name": parts[0],
                "assigned": int(parts[1]),
                "bootstrapped": bool(int(parts[2])),
                "max": int(parts[3]),
            })
    return workers


def query_db_assigned(container_name):
    """Count how many registry rows are assigned to this worker."""
    sql = (
        f"SELECT COUNT(*) FROM registry "
        f"WHERE takeover_container='{container_name}' "
        f"AND status='taken-over' AND unregistered_at IS NULL;"
    )
    out = docker_exec("onionheaven", f"sqlite3 {DB} \"{sql}\"")
    if out is None:
        return None
    try:
        return int(out.strip())
    except ValueError:
        return None


def query_queue_status(container_name):
    out = docker_exec(container_name,
                      "python3 /onionheaven-queue-manager.py status", timeout=5)
    if not out:
        return None
    try:
        return json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return None


def query_tor_detached(container_name):
    """Count actual detached onion services in a worker's Tor."""
    script = (
        "import socket, binascii; "
        "cookie = open('/var/lib/tor/control_auth_cookie','rb').read(); "
        "s = socket.socket(); s.settimeout(10); s.connect(('127.0.0.1',9051)); "
        "s.send(('AUTHENTICATE ' + binascii.hexlify(cookie).decode() + chr(13) + chr(10)).encode()); "
        "s.recv(256); "
        "s.send(b'GETINFO onions/detached' + bytes([13,10])); "
        "data = b''; "
        "exec('while True:\\n chunk = s.recv(8192)\\n if not chunk: break\\n data += chunk\\n if b\"250 OK\" in data: break'); "
        "s.close(); "
        "print(sum(1 for l in data.decode().strip().split(chr(10)) "
        "if l.strip() and not l.startswith('250') and l.strip() != '.'))"
    )
    try:
        result = subprocess.run(
            ["docker", "exec", container_name, "python3", "-c", script],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            return int(result.stdout.strip())
    except (subprocess.TimeoutExpired, ValueError, Exception):
        pass
    return None


def get_heartbeat_info():
    out = docker_exec("onionheaven",
                      "tail -1 /var/lib/onionheaven/heartbeat.log")
    if not out:
        return "?", "?"
    try:
        ts_str = out.split("]")[0].lstrip("[").strip()
        ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        age = (datetime.now() - ts).total_seconds()
        msg = out.split("OnionHeaven: ", 1)[-1] if "OnionHeaven: " in out else ""
        return f"{age:.0f}s ago", msg
    except (ValueError, IndexError):
        return "?", ""


def count_containers(prefix):
    out = docker_run(["ps", "--filter", f"name={prefix}", "--format", "{{.Names}}"])
    if not out:
        return 0
    return len(out.splitlines())


def get_container_mem(name):
    out = docker_run(["stats", "--no-stream", "--format", "{{.MemUsage}}", name])
    if not out:
        return "?"
    return out.split("/")[0].strip()


# ---------------------------------------------------------------------------
# Delta tracking
# ---------------------------------------------------------------------------

_prev = {}  # previous values for delta calculation


def delta(key, value):
    """Return formatted delta string like (+3) or (-2), empty if no change."""
    prev = _prev.get(key)
    if prev is None or prev == value:
        return ""
    diff = value - prev
    if diff > 0:
        return f" \033[32m(+{diff})\033[0m"
    else:
        return f" \033[31m({diff})\033[0m"


def save(key, value):
    """Save value for next delta calculation."""
    _prev[key] = value


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def clear_screen():
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


def c(color, text):
    return f"\033[{color}m{text}\033[0m"


def print_dashboard(iteration):
    now = datetime.now().astimezone().strftime("%H:%M:%S %Z")

    reg = query_registry()
    if not reg:
        print(f"[{now}] #{iteration} | ERROR: cannot query registry")
        return

    workers = query_workers()
    versions = query_versions()
    hb_age, hb_msg = get_heartbeat_info()

    # Get OnionHeaven address (first 20 chars)
    oh_addr = docker_exec("onionpress-tor", "cat /var/lib/tor/hidden_service/wordpress/hostname 2>/dev/null") or "unknown"
    oh_short = oh_addr[:20] + "…" if len(oh_addr) > 20 else oh_addr

    # Header
    print(f"[{now}] OnionHeaven Dashboard  #{iteration}  {oh_short}  (heartbeat: {hb_age})")
    print(f"{'─' * 72}")

    # Registry with deltas
    online = reg["online"]
    taken = reg["taken_over"]
    active = reg["active"]
    unreg = reg["unregistered"]

    unreg_str = f"  |  {unreg} unreg{delta('unreg', unreg)}" if unreg else ""
    print(f"  Registry:    {active} active{delta('active', active)}  |  "
          f"{c(32, f'{online} online')}{delta('online', online)}  |  "
          f"{c(33, f'{taken} taken-over')}{delta('taken', taken)}"
          f"{unreg_str}")
    save('active', active)
    save('online', online)
    save('taken', taken)
    save('unreg', unreg)

    # Versions
    if versions:
        ver_parts = [f"{v}: {n}" for v, n in versions]
        print(f"  Versions:    {', '.join(ver_parts)}")

    # Last heartbeat message
    if hb_msg:
        if len(hb_msg) > 60:
            hb_msg = hb_msg[:57] + "..."
        print(f"  Last pass:   {hb_msg}")

    # Workers with deltas
    if workers:
        print(f"  Workers:")
        for w in workers:
            name = w["name"]
            boot = c(32, "✓") if w["bootstrapped"] else c(31, "⏳")
            assigned = w["assigned"]
            bar_len = 20
            fill = int(bar_len * assigned / max(w["max"], 1))
            bar = "█" * fill + "░" * (bar_len - fill)

            line = f"    {name:30s}  {boot}  [{bar}] {assigned}/{w['max']}"

            # DB assigned count for this worker
            db_assigned = query_db_assigned(name)

            # Queue details + Tor reality check
            qs = query_queue_status(name)
            tor_count = query_tor_detached(name)

            q_parts = []

            # DB vs queue manager vs Tor
            if db_assigned is not None:
                q_parts.append(f"db={db_assigned}")
            if qs:
                act = qs.get("active", 0)
                queued = qs.get("queued", 0)
                fly = qs.get("in_flight", 0)
                failed = qs.get("failed", 0)

                q_parts.append(f"qm={act}")

                if tor_count is not None:
                    if act != tor_count:
                        q_parts.append(c(31, f"tor={tor_count}"))
                    else:
                        q_parts.append(f"tor={tor_count}")

                q_parts.append(f"q={queued}")
                q_parts.append(f"fly={fly}")
                if failed:
                    q_parts.append(c(31, f"fail={failed}"))
            elif tor_count is not None:
                q_parts.append(f"tor={tor_count}")

            if q_parts:
                line += f"  ({', '.join(q_parts)})"

            print(line)
    else:
        print(f"  Workers:     none")

    # Containers + memory
    stress = count_containers("stress-worker-")
    poll = count_containers("stress-poll-client-")
    takeover = count_containers("onionheaven-takeover-")
    oh_mem = get_container_mem("onionheaven")

    ctr_parts = [f"{takeover} takeover{delta('ctr_takeover', takeover)}"]
    save('ctr_takeover', takeover)
    if stress:
        ctr_parts.append(f"{stress} stress")
    if poll:
        ctr_parts.append(f"{poll} poll")
    print(f"  Containers:  {', '.join(ctr_parts)}  |  OH mem: {oh_mem}")

    print(f"{'─' * 72}")


def main():
    parser = argparse.ArgumentParser(description="OnionHeaven live dashboard")
    parser.add_argument("--interval", "-i", type=int, default=15,
                        help="Refresh interval in seconds (default: 15)")
    parser.add_argument("--once", action="store_true",
                        help="Print once and exit")
    args = parser.parse_args()

    iteration = 0
    try:
        while True:
            iteration += 1
            if not args.once:
                clear_screen()
            print_dashboard(iteration)
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nDashboard stopped.")


if __name__ == "__main__":
    main()

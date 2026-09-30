#!/usr/bin/env python3
"""
Worker bootstrap: creates onion services via C Tor's control port, registers with OnionHeaven over Tor.

Runs inside each worker container after C Tor starts. Each worker self-registers
with OnionHeaven just like a real OnionPress instance would — over Tor, using the
container's own Tor SOCKS proxy.

Usage:
    python3 worker-bootstrap.py <onionheaven_addr> <container_idx> <num_workers> [base_port]
"""

import base64
import json
import os
import random
import struct
import subprocess
import sys
import threading
import time

ONIONHEAVEN_ADDR = sys.argv[1]
CONTAINER_IDX = int(sys.argv[2])
NUM_WORKERS = int(sys.argv[3])
BASE_PORT = int(sys.argv[4]) if len(sys.argv) > 4 else 9100
# per_ctr = uniform container size for global_index calculation
# (last container may have fewer workers, but global indices must be consistent)
PER_CTR = int(sys.argv[5]) if len(sys.argv) > 5 else NUM_WORKERS

NO_HEALTHCHECK = os.environ.get("NO_HEALTHCHECK", "false").lower() == "true"
CTOR_HS_BASE = "/var/lib/tor/hidden_service"


CONTROL_PORT = 9051
MAX_IN_FLIGHT = int(os.environ.get("MAX_IN_FLIGHT", "5"))


def ctor_control(cmd):
    """Send a command to C Tor's control port. Returns the full response."""
    result = subprocess.run(
        ["sh", "-c",
         f'cookie=$(xxd -p /var/lib/tor/control_auth_cookie | tr -d "\\n"); '
         f'printf "AUTHENTICATE %s\\r\\n{cmd}\\r\\nQUIT\\r\\n" "$cookie" | '
         f'nc -w 15 127.0.0.1 {CONTROL_PORT}'],
        capture_output=True, text=True, timeout=30,
    )
    return result.stdout


def _read_control_cookie():
    """Read the Tor control auth cookie as hex string."""
    with open("/var/lib/tor/control_auth_cookie", "rb") as f:
        return f.read().hex()


class HsDescMonitor:
    """Monitor HS_DESC UPLOADED events from C Tor control port.

    Runs a background thread that listens for 650 HS_DESC events and
    tracks which service_ids have had their descriptors uploaded.
    """

    def __init__(self):
        self.uploaded = set()  # service_ids with HS_DESC UPLOADED
        self.lock = threading.Lock()
        self._sock = None
        self._running = False

    def start(self):
        """Connect to control port, subscribe to HS_DESC events, start listener."""
        import socket as _socket
        self._sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        self._sock.connect(("127.0.0.1", CONTROL_PORT))
        self._sock.settimeout(5.0)

        cookie = _read_control_cookie()
        self._sock.sendall(f"AUTHENTICATE {cookie}\r\n".encode())
        resp = self._recv()
        if "250 OK" not in resp:
            raise RuntimeError(f"HS_DESC monitor auth failed: {resp}")

        self._sock.sendall(b"SETEVENTS HS_DESC\r\n")
        resp = self._recv()
        if "250 OK" not in resp:
            raise RuntimeError(f"SETEVENTS failed: {resp}")

        self._running = True
        t = threading.Thread(target=self._listen, daemon=True)
        t.start()

    def _recv(self):
        data = b""
        while True:
            try:
                chunk = self._sock.recv(4096)
                if not chunk:
                    break
                data += chunk
                text = data.decode("utf-8", errors="replace")
                for line in text.split("\r\n"):
                    if line and len(line) >= 4 and line[:3].isdigit() and line[3] == " ":
                        return text
            except Exception:
                break
        return data.decode("utf-8", errors="replace")

    def _listen(self):
        buf = b""
        while self._running:
            try:
                chunk = self._sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\r\n" in buf:
                    line, buf = buf.split(b"\r\n", 1)
                    text = line.decode("utf-8", errors="replace")
                    if "650 HS_DESC UPLOADED" in text:
                        parts = text.split()
                        if len(parts) >= 4:
                            with self.lock:
                                self.uploaded.add(parts[3])
            except Exception:
                continue

    def is_uploaded(self, service_id):
        with self.lock:
            return service_id in self.uploaded

    def stop(self):
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass


def ctor_add_onion(port, label=""):
    """Create an ephemeral onion service via ADD_ONION NEW:ED25519-V3.

    Returns (address, privkey_b64) or (None, None) on failure.
    The address includes .onion suffix. The privkey is the raw base64
    key that can be used with ADD_ONION ED25519-V3:<key> to re-add.
    Retries up to 3 times on failure.
    """
    for attempt in range(3):
        t0 = time.time()
        response = ctor_control(f"ADD_ONION NEW:ED25519-V3 Flags=Detach Port=80,127.0.0.1:{port}")
        elapsed = time.time() - t0
        service_id = None
        privkey_b64 = None
        for line in response.splitlines():
            if line.startswith("250-ServiceID="):
                service_id = line.split("=", 1)[1].strip()
            elif line.startswith("250-PrivateKey=ED25519-V3:"):
                privkey_b64 = line.split("ED25519-V3:", 1)[1].strip()
        if service_id and privkey_b64:
            print(f"  {label}ADD_ONION port={port} → {service_id}.onion ({elapsed:.1f}s)", flush=True)
            return f"{service_id}.onion", privkey_b64
        # Log the failure with the raw response
        resp_preview = response.replace('\n', ' ').strip()[:120] if response else "(empty)"
        print(f"  {label}ADD_ONION port={port} FAILED attempt {attempt+1}/3 ({elapsed:.1f}s): {resp_preview}", flush=True)
        time.sleep(2)
    return None, None


def register_with_onionheaven(content_addr, hc_addr, privkey, pubkey, pem_b64, worker_id=0):
    """Register with OnionHeaven over Tor (via this container's SOCKS proxy).

    Tries 3 times with 10s gaps. If all fail, the heartbeat loop (every 60s)
    will keep retrying via /online automatically.
    """
    from onion_auth import sign_payload, make_timestamp
    timestamp = make_timestamp()
    signature = sign_payload(privkey, pubkey, "online", content_addr, hc_addr, timestamp)
    payload = json.dumps({
        "content_address": content_addr,
        "healthcheck_address": hc_addr,
        "arti_key_pem": pem_b64,
        "version": os.environ.get("STRESS_VERSION", "stress-test"),
        "timestamp": timestamp,
        "signature": signature,
    })

    for attempt in range(3):
        try:
            result = subprocess.run(
                [
                    "curl", "-s", "-X", "POST",
                    "--socks5-hostname", f"w{worker_id}:x@127.0.0.1:9050",
                    "-H", "Content-Type: application/json",
                    "-d", payload,
                    "--max-time", "30",
                    f"http://{ONIONHEAVEN_ADDR}:8083/online",
                ],
                capture_output=True, text=True, timeout=45,
            )
            try:
                resp = json.loads(result.stdout)
                if resp.get("registered"):
                    return result.stdout
            except (json.JSONDecodeError, ValueError):
                pass
        except Exception:
            pass

        if attempt < 2:
            time.sleep(10)

    # Failed — heartbeat loop will retry
    try:
        return result.stdout
    except Exception:
        return '{"error": "registration failed, heartbeat will retry"}'


def wait_for_socks():
    """Wait for Tor's SOCKS proxy to be ready before attempting registration."""
    print("Waiting for Tor SOCKS proxy to be ready...", flush=True)
    for attempt in range(120):  # up to 4 minutes
        try:
            result = subprocess.run(
                ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                 "--socks5-hostname", "127.0.0.1:9050",
                 "--max-time", "10",
                 f"http://{ONIONHEAVEN_ADDR}/"],
                capture_output=True, text=True, timeout=15,
            )
            # Any response (even error) means SOCKS is working and Tor is connected
            if result.returncode == 0:
                print("SOCKS proxy ready", flush=True)
                return True
        except Exception:
            pass
        time.sleep(2)
    print("WARNING: SOCKS proxy not ready after 4 minutes", flush=True)
    return False


def _upsert_worker(w):
    """Upsert a single worker row into the shared SQLite DB."""
    import sqlite3
    conn = sqlite3.connect("/worker-data/worker-info.db", timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""CREATE TABLE IF NOT EXISTS workers (
        global_index INTEGER PRIMARY KEY, local_index INTEGER NOT NULL,
        container INTEGER NOT NULL, content_address TEXT, healthcheck_address TEXT,
        content_port INTEGER, hc_port INTEGER, registered INTEGER NOT NULL DEFAULT 0,
        privkey_b64 TEXT, pubkey_b64 TEXT, ctor_key_b64 TEXT DEFAULT '',
        arti_key_pem TEXT DEFAULT '', error TEXT);""")
    conn.execute(
        """INSERT OR REPLACE INTO workers
           (global_index, local_index, container, content_address, healthcheck_address,
            content_port, hc_port, registered, privkey_b64, pubkey_b64, ctor_key_b64,
            arti_key_pem, error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            w.get("global_index"),
            w.get("local_index"),
            w.get("container"),
            w.get("content_address"),
            w.get("healthcheck_address"),
            w.get("content_port"),
            w.get("hc_port"),
            1 if w.get("registered") else 0,
            w.get("privkey_b64"),
            w.get("pubkey_b64"),
            w.get("ctor_key_b64", ""),
            w.get("arti_key_pem", w.get("_pem_b64", "")),
            w.get("error"),
        ),
    )
    conn.commit()
    conn.close()


def main_ctor_ramped():
    """C Tor bootstrap with ramped ADD_ONION — max MAX_IN_FLIGHT concurrent.

    Creates onion services with rate-limited ADD_ONION. Monitors HS_DESC
    UPLOADED events; when a service's descriptor is uploaded, immediately
    registers it with OnionHeaven in a background thread and fills the
    next ADD_ONION slot from the queue.
    """
    from concurrent.futures import ThreadPoolExecutor

    wait_for_socks()

    print(f"Ramped bootstrap: {NUM_WORKERS} workers, max {MAX_IN_FLIGHT} in-flight", flush=True)

    # Start HS_DESC monitor
    monitor = HsDescMonitor()
    try:
        monitor.start()
        print("HS_DESC monitor connected", flush=True)
    except Exception as e:
        print(f"WARNING: HS_DESC monitor failed ({e}), falling back to timed ramp", flush=True)
        monitor = None

    workers = [None] * NUM_WORKERS
    queued = list(range(NUM_WORKERS))  # worker indices to process
    in_flight = {}  # service_id -> (worker_index, timestamp)
    completed = []  # worker indices done (ADD_ONION + descriptor uploaded)

    heartbeat_started = set()  # track which workers have heartbeat threads

    def start_heartbeat(w):
        """Start heartbeat thread for a worker. First heartbeat is the registration."""
        idx = w["local_index"]
        if idx not in heartbeat_started:
            heartbeat_started.add(idx)
            t = threading.Thread(
                target=_site_heartbeat_thread,
                args=(w, workers, 60, 0),
                daemon=True,
            )
            t.start()

    def fill_slots():
        """Move workers from queue to in-flight up to MAX_IN_FLIGHT."""
        while queued and len(in_flight) < MAX_IN_FLIGHT:
            i = queued.pop(0)
            global_idx = CONTAINER_IDX * PER_CTR + i
            cp = BASE_PORT + i * 2
            hp = BASE_PORT + i * 2 + 1

            # Create content onion service
            content_addr, content_key_b64 = ctor_add_onion(cp, label=f"[worker {i}] content ")
            if not content_addr:
                print(f"[worker {i}] ADD_ONION failed, skipping", flush=True)
                workers[i] = {
                    "global_index": global_idx, "local_index": i,
                    "container": CONTAINER_IDX, "registered": False,
                    "error": "add_onion_failed",
                }
                _upsert_worker(workers[i])
                continue

            # Create healthcheck onion service
            if NO_HEALTHCHECK:
                hc_addr = content_addr.replace(content_addr[:8], "hc" + content_addr[2:8])
            else:
                hc_addr, _ = ctor_add_onion(hp, label=f"[worker {i}] hc ")
                if not hc_addr:
                    print(f"[worker {i}] ADD_ONION hc failed, skipping", flush=True)
                    workers[i] = {
                        "global_index": global_idx, "local_index": i,
                        "container": CONTAINER_IDX, "registered": False,
                        "error": "add_onion_hc_failed",
                    }
                    _upsert_worker(workers[i])
                    continue

            # Derive keys for registration
            raw_key = base64.b64decode(content_key_b64)
            import onion_auth
            a_bytes = raw_key[:32]
            a = int.from_bytes(a_bytes, 'little')
            A = onion_auth._scalar_mult(a, onion_auth._B)
            pubkey = onion_auth._encode_point(A)
            privkey = raw_key

            # Build PEM for registration
            key_dir = f"/tmp/ctor_keys_{CONTAINER_IDX}_{i}"
            os.makedirs(key_dir, exist_ok=True)
            with open(f"{key_dir}/hs_ed25519_secret_key", "wb") as f:
                f.write(b"== ed25519v1-secret: type0 ==\x00\x00\x00" + raw_key)
            with open(f"{key_dir}/hs_ed25519_public_key", "wb") as f:
                f.write(b"== ed25519v1-public: type0 ==\x00\x00\x00" + pubkey)
            pem_path = f"/tmp/w{CONTAINER_IDX}_{i}_content.pem"
            subprocess.run(
                ["python3", "/key-convert.py", "ctor-to-arti",
                 f"{key_dir}/hs_ed25519_secret_key", pem_path],
                capture_output=True, text=True, timeout=10,
            )
            with open(pem_path, "rb") as f:
                pem_b64 = base64.b64encode(f.read()).decode()

            service_id = content_addr.replace(".onion", "")
            in_flight[service_id] = (i, time.time())

            workers[i] = {
                "global_index": global_idx, "local_index": i,
                "container": CONTAINER_IDX,
                "content_address": content_addr, "healthcheck_address": hc_addr,
                "content_port": cp, "hc_port": hp,
                "registered": False,
                "privkey_b64": base64.b64encode(privkey).decode(),
                "pubkey_b64": base64.b64encode(pubkey).decode(),
                "ctor_key_b64": content_key_b64,
                "_pem_b64": pem_b64,
            }
            _upsert_worker(workers[i])

            slots = f"{len(in_flight)}/{MAX_IN_FLIGHT} slots, {len(queued)} queued"
            print(f"[worker {i}] in-flight ({slots})", flush=True)

    # Initial fill
    fill_slots()

    # Wait for descriptors to upload, register immediately, fill new slots
    while in_flight or queued:
        time.sleep(1)

        promoted = []
        for sid, (idx, ts) in list(in_flight.items()):
            uploaded = monitor.is_uploaded(sid) if monitor else (time.time() - ts > 15)
            if uploaded:
                promoted.append((sid, idx))
            elif time.time() - ts > 120:
                print(f"[worker {idx}] descriptor timeout after 120s, continuing", flush=True)
                promoted.append((sid, idx))

        for sid, idx in promoted:
            del in_flight[sid]
            completed.append(idx)
            print(f"[worker {idx}] descriptor ready, starting heartbeat ({len(completed)}/{NUM_WORKERS} done)", flush=True)
            # First heartbeat will register via /online
            start_heartbeat(workers[idx])

        if promoted:
            fill_slots()

    if monitor:
        monitor.stop()

    all_with_addresses = [w for w in workers if w and w.get("content_address")]
    print(f"Bootstrap complete: {len(all_with_addresses)}/{NUM_WORKERS} have addresses, "
          f"{len(heartbeat_started)} heartbeating (first heartbeat = registration)", flush=True)

    # Start heartbeat threads for any workers that don't have one yet
    # (e.g. those that failed registration but have addresses).
    remaining = [w for w in all_with_addresses if w["local_index"] not in heartbeat_started]
    if remaining:
        n = len(remaining)
        stagger = 60 / max(n, 1)
        for i, w in enumerate(remaining):
            heartbeat_started.add(w["local_index"])
            t = threading.Thread(
                target=_site_heartbeat_thread,
                args=(w, workers, 60, i * stagger),
                daemon=True,
            )
            t.start()
        print(f"Started {n} additional heartbeat threads for unregistered workers", flush=True)

    # Keep main thread alive, periodically log summary
    if all_with_addresses:
        HEARTBEAT_INTERVAL = 60
        while True:
            time.sleep(HEARTBEAT_INTERVAL)
            enabled = sum(1 for s in all_with_addresses if is_worker_enabled(s))
            print(f"Heartbeat: {enabled}/{len(all_with_addresses)} enabled", flush=True)


def main():
    main_ctor_ramped()


def send_heartbeat(worker):
    """Send a single /online heartbeat for a worker."""
    from onion_auth import sign_payload, make_timestamp

    privkey = base64.b64decode(worker["privkey_b64"])
    pubkey = base64.b64decode(worker["pubkey_b64"])
    ca = worker["content_address"]
    ha = worker["healthcheck_address"]

    timestamp = make_timestamp()
    signature = sign_payload(privkey, pubkey, "online", ca, ha, timestamp)
    payload_dict = {
        "content_address": ca,
        "healthcheck_address": ha,
        "timestamp": timestamp,
        "signature": signature,
        "wordpress_healthy": True,
    }
    # Include PEM key so unregistered workers can register via heartbeat
    pem_b64 = worker.get("_pem_b64", "")
    if pem_b64:
        payload_dict["arti_key_pem"] = pem_b64
        payload_dict["version"] = os.environ.get("STRESS_VERSION", "stress-test")
    payload = json.dumps(payload_dict)

    # Use unique SOCKS credentials per worker for circuit isolation
    worker_id = worker.get("global_index", worker.get("local_index", 0))
    try:
        result = subprocess.run(
            [
                "curl", "-s", "-X", "POST",
                "--socks5-hostname", f"w{worker_id}:x@127.0.0.1:9050",
                "-H", "Content-Type: application/json",
                "-d", payload,
                "--max-time", "30",
                f"http://{ONIONHEAVEN_ADDR}:8083/online",
            ],
            capture_output=True, text=True, timeout=45,
        )
        return result.stdout
    except Exception as e:
        return f'{{"error": "{e}"}}'


def is_worker_enabled(worker):
    """Check if this worker's HTTP responder is still enabled (not disabled by stress test)."""
    cp = worker["content_port"]
    try:
        result = subprocess.run(
            ["curl", "-s", "--max-time", "2", f"http://127.0.0.1:{cp}/"],
            capture_output=True, text=True, timeout=5,
        )
        # If we get a response, the worker is enabled
        return result.returncode == 0 and result.stdout.strip() != ""
    except Exception:
        return False


def _site_heartbeat_thread(site, all_sites, interval, initial_delay):
    """Independent heartbeat thread for a single site.

    Each site sends its own heartbeat every `interval` seconds,
    offset by `initial_delay` so heartbeats are evenly spread.
    If /online succeeds and site wasn't registered, update the DB.
    """
    time.sleep(initial_delay)

    while True:
        if not is_worker_enabled(site):
            time.sleep(interval)
            continue

        result = send_heartbeat(site)
        try:
            resp = json.loads(result)
            if resp.get("online"):
                if not site.get("registered"):
                    site["registered"] = True
                    print(f"  site {site['local_index']}: registered via heartbeat", flush=True)
                    _upsert_worker(site)
            else:
                print(f"  heartbeat rejected for site {site['local_index']}: {result[:100]}", flush=True)
        except Exception:
            pass

        time.sleep(interval)


if __name__ == "__main__":
    main()

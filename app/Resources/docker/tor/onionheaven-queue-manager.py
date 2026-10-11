#!/usr/bin/env python3
"""
OnionHeaven Queue Manager — rate-limited ADD_ONION pipeline.

Runs inside takeover worker containers. Uses two Tor control port
connections: one for commands (ADD_ONION/DEL_ONION), one for events
(HS_DESC UPLOADED). Rate-limits ADD_ONION calls to avoid overwhelming
Tor's circuit builder.

Interface:
  docker exec <worker> python3 /onionheaven-queue-manager.py takeover <addr>
  docker exec <worker> python3 /onionheaven-queue-manager.py release <addr>
  docker exec <worker> python3 /onionheaven-queue-manager.py status
"""

import json
import os
import socket
import subprocess
import sys
import time
import threading
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAX_IN_FLIGHT = int(os.environ.get("ONIONHEAVEN_MAX_IN_FLIGHT", "5"))
CONTROL_HOST = "127.0.0.1"
CONTROL_PORT = 9051
COOKIE_PATH = "/var/lib/tor/control_auth_cookie"
KEYS_DIR = "/var/lib/onionpress/onionheaven/keys"
KEY_CONVERT = "/key-convert.py"
REDIRECT_PORT = 8082
CONTAINER_NAME = os.environ.get("CONTAINER_NAME", "unknown")
SOCK_PATH = "/tmp/queue-manager.sock"


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sys.stderr.write(f"[{ts}] queue-manager: {msg}\n")
    sys.stderr.flush()


def _read_cookie():
    with open(COOKIE_PATH, "rb") as f:
        return f.read().hex()


# ---------------------------------------------------------------------------
# Tor control port — command connection (synchronous)
# ---------------------------------------------------------------------------

class TorCommandConn:
    """Synchronous control port connection for ADD_ONION / DEL_ONION."""

    def __init__(self):
        self.sock = None

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((CONTROL_HOST, CONTROL_PORT))
        self.sock.settimeout(10.0)
        cookie = _read_cookie()
        self._send(f"AUTHENTICATE {cookie}")
        resp = self._read_response()
        if "250 OK" not in resp:
            raise RuntimeError(f"Command conn auth failed: {resp}")
        log("Command connection established")

    def _send(self, cmd):
        self.sock.sendall(f"{cmd}\r\n".encode())

    def _read_response(self):
        """Read a complete synchronous response (ends with 'NNN SP' line)."""
        data = b""
        while True:
            try:
                chunk = self.sock.recv(4096)
                if not chunk:
                    break
                data += chunk
                text = data.decode("utf-8", errors="replace")
                for line in text.split("\r\n"):
                    if line and len(line) >= 4 and line[:3].isdigit() and line[3] == " ":
                        return text
            except socket.timeout:
                break
        return data.decode("utf-8", errors="replace")

    def add_onion(self, content_address, key_b64):
        """Send ADD_ONION. Returns (success, service_id_or_error).

        Defense-in-depth: if Tor's ServiceID doesn't match content_address,
        the key on disk derives to a different onion than the directory
        name claims. Roll back the ADD and refuse — otherwise Tor ends up
        serving the wrong content under content_address's takeover slot,
        and the heartbeat RECONCILE loop runs forever cleaning up an entry
        the queue manager keeps re-creating. Catches residue from the
        pre-542cb61b /online key-write bug and any future class of bug
        that lets a wrong key onto disk.
        """
        cmd = (f"ADD_ONION ED25519-V3:{key_b64} Flags=Detach "
               f"Port=80,127.0.0.1:{REDIRECT_PORT}")
        self._send(cmd)
        resp = self._read_response()

        expected_sid = content_address.replace(".onion", "")
        if "250-ServiceID=" in resp:
            # Extract service ID from response
            for line in resp.split("\r\n"):
                if line.startswith("250-ServiceID="):
                    sid = line.split("=", 1)[1]
                    if sid != expected_sid:
                        # Wrong key on disk. Roll back the ADD so we don't
                        # leak a service into Tor under the wrong slot.
                        self._send(f"DEL_ONION {sid}")
                        self._read_response()
                        return False, f"key_mismatch:expected={expected_sid} actual={sid}"
                    return True, sid
            return True, expected_sid

        if "Onion address collision" in resp:
            # Tor says the key already maps to a registered service. That's
            # only "fine" if Tor is actually serving it — otherwise the entry
            # is leaked inside hs_service_map (Tor 0.4.x bug: invisible to
            # GETINFO onions/{detached,current} and unreachable via DEL_ONION,
            # so it can't be cleared without restarting Tor). Verify before
            # claiming success to avoid an infinite retry loop where the
            # caller thinks the ADD succeeded but no descriptor ever publishes.
            if self.has_onion(content_address):
                return True, content_address.replace(".onion", "")
            return False, "stuck_collision"

        # Failure
        return False, resp.strip()

    def del_onion(self, content_address):
        """Send DEL_ONION. Returns (success, response)."""
        service_id = content_address.replace(".onion", "")
        self._send(f"DEL_ONION {service_id}")
        resp = self._read_response()
        success = "250 OK" in resp
        return success, resp.strip()

    def has_onion(self, content_address):
        """Check if Tor still has this onion service (detached or current)."""
        service_id = content_address.replace(".onion", "")
        for kind in ("onions/detached", "onions/current"):
            self._send(f"GETINFO {kind}")
            resp = self._read_response()
            if service_id in resp:
                return True
        return False

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Tor control port — event connection (async HS_DESC monitoring)
# ---------------------------------------------------------------------------

class TorEventConn:
    """Async control port connection that monitors HS_DESC events."""

    def __init__(self):
        self.sock = None
        self.uploaded = set()  # service_ids with HS_DESC UPLOADED
        self.failed = set()    # service_ids with HS_DESC FAILED
        self.lock = threading.Lock()
        self._running = False
        self._thread = None

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((CONTROL_HOST, CONTROL_PORT))
        self.sock.settimeout(5.0)
        cookie = _read_cookie()
        self._send(f"AUTHENTICATE {cookie}")
        resp = self._read_response()
        if "250 OK" not in resp:
            raise RuntimeError(f"Event conn auth failed: {resp}")

        self._send("SETEVENTS HS_DESC")
        resp = self._read_response()
        if "250 OK" not in resp:
            raise RuntimeError(f"SETEVENTS failed: {resp}")

        self._running = True
        self._thread = threading.Thread(target=self._event_loop, daemon=True)
        self._thread.start()
        log("Event connection established, listening for HS_DESC events")

    def _send(self, cmd):
        self.sock.sendall(f"{cmd}\r\n".encode())

    def _read_response(self):
        data = b""
        while True:
            try:
                chunk = self.sock.recv(4096)
                if not chunk:
                    break
                data += chunk
                text = data.decode("utf-8", errors="replace")
                for line in text.split("\r\n"):
                    if line and len(line) >= 4 and line[:3].isdigit() and line[3] == " ":
                        return text
            except socket.timeout:
                break
        return data.decode("utf-8", errors="replace")

    def _event_loop(self):
        self.sock.settimeout(1.0)
        buf = b""
        while self._running:
            try:
                chunk = self.sock.recv(4096)
                if not chunk:
                    log("Event connection closed")
                    self._running = False
                    break
                buf += chunk
                while b"\r\n" in buf:
                    line, buf = buf.split(b"\r\n", 1)
                    self._handle_line(line.decode("utf-8", errors="replace"))
            except socket.timeout:
                continue
            except Exception as e:
                log(f"Event loop error: {e}")
                time.sleep(1)

    def _handle_line(self, line):
        # 650 HS_DESC UPLOADED <service_id> <auth_type> <hs_dir>
        if "650 HS_DESC UPLOADED" in line:
            parts = line.split()
            if len(parts) >= 4:
                sid = parts[3]
                with self.lock:
                    self.uploaded.add(sid)
                    self.failed.discard(sid)
                log(f"HS_DESC UPLOADED: {sid[:20]}...")
        # 650 HS_DESC FAILED <service_id> ...
        elif "650 HS_DESC FAILED" in line:
            parts = line.split()
            if len(parts) >= 4:
                sid = parts[3]
                with self.lock:
                    self.failed.add(sid)
                log(f"HS_DESC FAILED: {sid[:20]}...")

    def is_uploaded(self, content_address):
        sid = content_address.replace(".onion", "")
        with self.lock:
            return sid in self.uploaded

    def is_failed(self, content_address):
        sid = content_address.replace(".onion", "")
        with self.lock:
            return sid in self.failed

    def clear(self, content_address):
        sid = content_address.replace(".onion", "")
        with self.lock:
            self.uploaded.discard(sid)
            self.failed.discard(sid)

    def close(self):
        self._running = False
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Queue manager
# ---------------------------------------------------------------------------

class QueueManager:
    """Rate-limited ADD_ONION pipeline with stuck circuit recovery."""

    # Timeout before declaring in-flight addresses stuck
    STUCK_TIMEOUT = 180  # 3 minutes
    # Backoff multiplier for repeated recovery attempts
    MAX_BACKOFF = 720  # 12 minutes max

    def __init__(self, cmd_conn, evt_conn):
        self.cmd = cmd_conn
        self.evt = evt_conn
        self.queued = []       # addresses waiting for a slot
        self.in_flight = {}    # addr -> timestamp (ADD_ONION done, waiting)
        self.active = set()    # descriptors uploaded, service reachable
        self.failed = set()    # ADD_ONION failed
        self.lock = threading.Lock()
        self._ever_active = False          # has any address ever gone active?
        self._recovery_count = 0           # consecutive recovery attempts
        self._last_recovery_time = 0.0     # monotonic time of last recovery

    def takeover(self, content_address):
        """Add an address to the takeover queue.

        If in-memory state says active/in_flight but Tor doesn't actually have
        the service (e.g. DEL_ONION'd via tor-manager out-of-band, or a
        pre-#232 takeover_function race left the row marked taken-over without
        ADD_ONION ever firing), the stale entry is evicted so the address re-queues.
        """
        with self.lock:
            if content_address in self.active or content_address in self.in_flight:
                if self.cmd.has_onion(content_address):
                    if content_address in self.active:
                        return {"status": "already_active", "address": content_address}
                    return {"status": "in_flight", "address": content_address}
                log(f"RECONCILE: {content_address[:20]}... in memory but not in Tor — re-queuing")
                self.active.discard(content_address)
                self.in_flight.pop(content_address, None)
                self.failed.discard(content_address)
                self.evt.clear(content_address)
                # fall through to enqueue
            if content_address in self.queued:
                return {"status": "already_queued", "address": content_address}
            self.queued.append(content_address)
        self._process_queue()
        return {"status": "queued", "address": content_address}

    def reset(self):
        """Clear all state and DEL_ONION everything in Tor.

        Called when the DB is cleaned externally and the worker needs to
        start fresh without being restarted.
        """
        with self.lock:
            all_addrs = list(self.active) + list(self.in_flight.keys())
            self.queued.clear()
            self.in_flight.clear()
            self.active.clear()
            self.failed.clear()
            self._ever_active = False
            self._recovery_count = 0
            self._last_recovery_time = 0.0

        # DEL_ONION everything currently in Tor
        del_count = 0
        for addr in all_addrs:
            success, _ = self.cmd.del_onion(addr)
            if success:
                del_count += 1
            self.evt.clear(addr)

        log(f"RESET: cleared {len(all_addrs)} addresses from memory, "
            f"DEL_ONION'd {del_count} from Tor")
        return {
            "status": "reset",
            "cleared": len(all_addrs),
            "del_onion": del_count,
        }

    def release(self, content_address):
        """Release an address (immediate DEL_ONION)."""
        with self.lock:
            if content_address in self.queued:
                self.queued.remove(content_address)
                return {"status": "removed_from_queue", "address": content_address}
            was_active = content_address in self.active
            was_in_flight = content_address in self.in_flight
            self.active.discard(content_address)
            self.in_flight.pop(content_address, None)
            self.failed.discard(content_address)

        if was_active or was_in_flight:
            success, resp = self.cmd.del_onion(content_address)
            self.evt.clear(content_address)
            self._process_queue()
            if success:
                return {"status": "released", "address": content_address}
            else:
                return {"status": "release_warning", "address": content_address,
                        "detail": resp}

        # Not in our in-memory state — check if Tor still has it (orphan
        # from a previous process lifetime with Flags=Detach)
        if self.cmd.has_onion(content_address):
            log(f"Found orphaned detached service for {content_address}, removing")
            success, resp = self.cmd.del_onion(content_address)
            self.evt.clear(content_address)
            if success:
                return {"status": "released", "address": content_address}
            else:
                return {"status": "release_warning", "address": content_address,
                        "detail": resp}

        return {"status": "not_found", "address": content_address}

    def _process_queue(self):
        """Move addresses from queued to in_flight if slots available."""
        with self.lock:
            while self.queued and len(self.in_flight) < MAX_IN_FLIGHT:
                addr = self.queued.pop(0)
                key_b64 = self._get_key(addr)
                if not key_b64:
                    log(f"ERROR: no key for {addr}, skipping")
                    self.failed.add(addr)
                    continue

                success, result = self.cmd.add_onion(addr, key_b64)
                if success:
                    self.in_flight[addr] = time.time()
                    log(f"ADD_ONION OK: {addr[:20]}... "
                        f"({len(self.in_flight)}/{MAX_IN_FLIGHT} slots, "
                        f"{len(self.queued)} queued)")
                else:
                    log(f"ADD_ONION FAILED: {addr[:20]}... — {result}")
                    self.failed.add(addr)

    def check_uploads(self):
        """Move in-flight addresses to active if descriptors uploaded.

        Also detects stuck in-flight addresses and triggers recovery:
        - If 0 ever went active and in-flight stuck > STUCK_TIMEOUT: restart Tor
          (bad guard selection — need fresh guards)
        - If some active but current batch stuck > STUCK_TIMEOUT: SIGNAL NEWNYM
          (circuits congested — get new circuits without losing active services)
        """
        promoted = []
        with self.lock:
            for addr in list(self.in_flight):
                if self.evt.is_uploaded(addr):
                    self.active.add(addr)
                    del self.in_flight[addr]
                    promoted.append(addr)
                elif self.evt.is_failed(addr):
                    elapsed = time.time() - self.in_flight[addr]
                    if elapsed > 300:
                        log(f"HS_DESC FAILED after {elapsed:.0f}s, giving up: "
                            f"{addr[:20]}...")
                        self.failed.add(addr)
                        del self.in_flight[addr]

        if promoted:
            self._ever_active = True
            self._recovery_count = 0  # reset backoff on success
            for addr in promoted:
                log(f"ACTIVE: {addr[:20]}... (descriptors uploaded)")
            self._process_queue()

        # Check for stuck in-flight addresses
        self._check_stuck()

    def _check_stuck(self):
        """Move stuck in-flight addresses to active and open slots.

        After STUCK_TIMEOUT, stop waiting for HS_DESC UPLOADED — the
        ADD_ONION succeeded so Tor has the service, descriptors will
        propagate eventually. Don't block the queue waiting for confirmation.
        """
        now = time.time()
        with self.lock:
            if not self.in_flight:
                return

            promoted = []
            for addr, ts in list(self.in_flight.items()):
                if now - ts > self.STUCK_TIMEOUT:
                    promoted.append(addr)

            for addr in promoted:
                self.active.add(addr)
                del self.in_flight[addr]

        if promoted:
            self._ever_active = True
            for addr in promoted:
                log(f"STUCK→ACTIVE: {addr[:20]}... (no HS_DESC after {self.STUCK_TIMEOUT}s, "
                    f"promoting anyway)")
            self._process_queue()

    def _restart_tor(self):
        """Restart Tor with fresh guard selection. Re-queues all services."""
        self._last_recovery_time = time.time()
        self._recovery_count += 1

        # Collect everything to re-queue
        with self.lock:
            to_requeue = list(self.in_flight.keys()) + list(self.active)
            self.in_flight.clear()
            self.active.clear()

        # Close control connections (Tor is going away)
        self.cmd.close()
        self.evt.close()

        # Delete state and restart Tor
        try:
            subprocess.run(["rm", "-rf", "/var/lib/tor/state"],
                           capture_output=True, timeout=5)
            # Find and kill Tor process
            result = subprocess.run(["pgrep", "-f", "tor -f"],
                                    capture_output=True, text=True, timeout=5)
            if result.stdout.strip():
                pid = result.stdout.strip().split()[0]
                subprocess.run(["kill", pid], capture_output=True, timeout=5)
                log(f"RECOVERY: killed Tor PID {pid}, waiting for restart...")
                # The entrypoint's wait loop will detect Tor died and exit,
                # but we need Tor to come back. Since the container has
                # restart=unless-stopped, it will restart. But we're inside
                # the container — we need to start Tor ourselves.
                time.sleep(2)
                subprocess.Popen(
                    ["su", "-s", "/bin/sh", "debian-tor", "-c", "tor -f /etc/tor/torrc"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                log("RECOVERY: Tor restarting with fresh guards...")
        except Exception as e:
            log(f"RECOVERY: error restarting Tor: {e}")

        # Wait for Tor to bootstrap
        for _ in range(60):
            time.sleep(2)
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.connect((CONTROL_HOST, CONTROL_PORT))
                s.close()
                break
            except ConnectionRefusedError:
                continue
        else:
            log("RECOVERY: Tor control port not available after 120s")
            return

        # Reconnect control connections
        try:
            self.cmd.connect()
            self.evt.connect()
            self.evt.uploaded.clear()
            self.evt.failed.clear()
            log("RECOVERY: control connections re-established")
        except Exception as e:
            log(f"RECOVERY: failed to reconnect: {e}")
            return

        # Re-queue everything
        with self.lock:
            for addr in to_requeue:
                if addr not in self.queued:
                    self.queued.append(addr)
        log(f"RECOVERY: re-queued {len(to_requeue)} addresses after Tor restart")
        self._process_queue()

    def _signal_newnym(self):
        """Send SIGNAL NEWNYM to get new circuits without killing services."""
        self._last_recovery_time = time.time()
        self._recovery_count += 1
        try:
            self.cmd._send("SIGNAL NEWNYM")
            resp = self.cmd._read_response()
            if "250 OK" in resp:
                log("RECOVERY: SIGNAL NEWNYM sent — waiting for new circuits")
            else:
                log(f"RECOVERY: SIGNAL NEWNYM response: {resp.strip()}")
        except Exception as e:
            log(f"RECOVERY: NEWNYM error: {e}")

    def status(self):
        with self.lock:
            oldest_in_flight = None
            if self.in_flight:
                oldest = min(self.in_flight.values())
                oldest_in_flight = int(time.time() - oldest)
            return {
                "container": CONTAINER_NAME,
                "queued": len(self.queued),
                "in_flight": len(self.in_flight),
                "active": len(self.active),
                "failed": len(self.failed),
                "max_in_flight": MAX_IN_FLIGHT,
                "recovery_count": self._recovery_count,
                "oldest_in_flight_secs": oldest_in_flight,
            }

    def addr_state(self, content_address):
        """Return the serving_status for a single address."""
        with self.lock:
            if content_address in self.active:
                return "active"
            if content_address in self.in_flight:
                return "activating"
            if content_address in self.queued:
                return "queued-to-activate"
            if content_address in self.failed:
                return "failed"
        return None  # not in this worker

    def _get_key(self, content_address):
        """Extract ed25519 key as base64 for ADD_ONION."""
        key_file = os.path.join(KEYS_DIR, content_address,
                                "ks_hs_id.ed25519_expanded_private")
        if not os.path.isfile(key_file):
            return None
        try:
            result = subprocess.run(
                ["python3", KEY_CONVERT, "pem-to-ed25519-base64", key_file],
                capture_output=True, text=True, timeout=10
            )
            return result.stdout.strip() if result.returncode == 0 else None
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Command interface via Unix socket
# ---------------------------------------------------------------------------

def run_daemon():
    """Run as daemon — listen for commands on Unix socket."""
    log("Waiting for Tor control port...")
    for _ in range(60):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect((CONTROL_HOST, CONTROL_PORT))
            s.close()
            break
        except ConnectionRefusedError:
            time.sleep(2)
    else:
        log("ERROR: Tor control port not available after 120s")
        sys.exit(1)

    # Two separate connections: commands and events
    cmd_conn = TorCommandConn()
    cmd_conn.connect()
    evt_conn = TorEventConn()
    evt_conn.connect()
    qm = QueueManager(cmd_conn, evt_conn)

    # Remove stale socket
    try:
        os.unlink(SOCK_PATH)
    except FileNotFoundError:
        pass

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCK_PATH)
    os.chmod(SOCK_PATH, 0o666)
    server.listen(64)
    server.settimeout(2.0)

    log(f"Queue manager daemon started (max_in_flight={MAX_IN_FLIGHT})")

    while True:
        # Check for uploaded descriptors
        qm.check_uploads()

        # Accept commands
        try:
            conn, _ = server.accept()
            conn.settimeout(5.0)
            data = conn.recv(4096).decode().strip()
            parts = data.split(None, 1)
            cmd = parts[0] if parts else ""
            arg = parts[1] if len(parts) > 1 else ""

            if cmd == "takeover" and arg:
                result = qm.takeover(arg)
            elif cmd == "release" and arg:
                result = qm.release(arg)
            elif cmd == "status":
                result = qm.status()
            elif cmd == "reset":
                result = qm.reset()
            elif cmd == "addr_state" and arg:
                state = qm.addr_state(arg)
                result = {"address": arg, "serving_status": state}
            elif cmd == "has" and arg:
                result = {"address": arg, "has_onion": qm.cmd.has_onion(arg)}
            else:
                result = {"error": f"unknown command: {data}"}

            conn.sendall(json.dumps(result).encode() + b"\n")
            conn.close()
        except socket.timeout:
            continue
        except Exception as e:
            log(f"Socket error: {e}")


def send_command(cmd):
    """Send a command to the running daemon and print response."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(SOCK_PATH)
        s.sendall(cmd.encode() + b"\n")
        s.settimeout(30.0)
        resp = s.recv(4096).decode().strip()
        s.close()
        if resp:
            print(resp)
        else:
            print(json.dumps({"error": "empty response from queue manager"}))
            sys.exit(1)
    except socket.timeout:
        print(json.dumps({"error": "queue manager timed out"}))
        sys.exit(1)
    except ConnectionRefusedError:
        print(json.dumps({"error": "queue manager not running"}))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({"error": f"queue manager error: {e}"}))
        sys.exit(1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: queue-manager.py daemon | takeover <addr> | release <addr> | has <addr> | status | reset")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "daemon":
        run_daemon()
    elif cmd == "takeover" and len(sys.argv) >= 3:
        send_command(f"takeover {sys.argv[2]}")
    elif cmd == "release" and len(sys.argv) >= 3:
        send_command(f"release {sys.argv[2]}")
    elif cmd == "has" and len(sys.argv) >= 3:
        send_command(f"has {sys.argv[2]}")
    elif cmd == "status":
        send_command("status")
    elif cmd == "reset":
        send_command("reset")
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)

#!/usr/bin/env python3
"""
OnionHeaven farm controller — the one Tor-side process with the Docker socket.

Runs in the `onionheaven-farm` container (compose: FARM_CONTROLLER=1, no
network, every capability dropped). The `onionheaven` container's heartbeat
used to mount the Docker socket itself to create and drive the takeover
workers; the socket is root on the Docker host (the Colima VM on macOS, the
user's own account under rootless Docker on Linux), so a compromised
heartbeat could `docker run -v $HOME:/x`. Now the heartbeat talks to this
process over a unix socket on a volume only the two containers mount, and
this process knows a handful of verbs with validated arguments:

  spawn       {name}            start a takeover worker: a fixed `docker run`
                                of the stack's pinned image with a fixed
                                network, volume, ulimit, restart policy and
                                hardening. Nothing about the container's
                                shape comes from the request.
  remove      {name}            `docker rm -f` a worker
  list                          the names of the worker containers
  inspect     {name}            whether a worker exists and runs, and its
                                mounts (JSON, as `docker inspect` prints it)
  exec        {name, verb[, address]}
                                one of EXEC_VERBS inside a worker: the
                                queue manager's takeover/release/has/
                                addr_state/status, Tor's bootstrap phase or
                                detached onions, or a worker-process
                                liveness check. The command line is this
                                file's; the request chooses only the verb
                                and a v3 onion address.
  tor-address                   the site's onion address, read from a fixed
                                file in onionpress-tor (the hub admission
                                check in onionheaven, which does not mount
                                the Tor state volume)

Names must match a takeover worker's (`onionheaven-takeover-N`, or the
older `onionheaven-poll-N` for remove/inspect), addresses the v3 pattern;
unknown fields, ops and verbs are refused, and every refusal is logged.
Spawn is capped at MAX_WORKERS containers so a compromised heartbeat
cannot fill the Docker host with Tor instances either.

Protocol: one request per connection, one JSON object on one line
(at most MAX_REQUEST bytes), one JSON object back, connection closed. The
socket is created 0600 by root; the heartbeat runs as root too. A request
never reaches `docker` as anything but a validated name, verb or address
placed into an argv this file builds. Output from workers is returned to
the heartbeat capped at MAX_OUTPUT bytes; the heartbeat parses it as it
always did.

The client is onionheaven_common.farm_request / farm_exec.
docs/DOCKER-SOCKET.md, "The farm controller".
"""

import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time

SOCKET_DIR = "/run/onionheaven-farm"
SOCKET_PATH = SOCKET_DIR + "/farm.sock"
DOCKER_SOCKET = "/var/run/docker.sock"

# The hub data volume and its mount point: the only volume a worker gets
# (onionheaven_common says the same; a test keeps the constants equal).
HUB_DATA_VOLUME = "onionpress-onionheaven-data"
ONIONHEAVEN_DATA_DIR = "/var/lib/onionheaven"

TOR_CONTAINER = "onionpress-tor"
OWN_HOSTNAME_PATH = "/var/lib/tor/hidden_service/wordpress/hostname"
QUEUE_MANAGER = "/onionheaven-queue-manager.py"

# Workers this controller starts. `poll` is the name older versions gave
# them; those may still be removed and inspected, never exec'd into.
TAKEOVER_WORKER_RE = re.compile(r"onionheaven-takeover-[0-9]{1,9}")
FARM_WORKER_RE = re.compile(r"onionheaven-(takeover|poll)-[0-9]{1,9}")
ONION_ADDRESS_RE = re.compile(r"[a-z2-7]{56}\.onion")
IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")

MAX_REQUEST = 4096          # bytes of request
MAX_OUTPUT = 65536          # bytes of a worker's stdout or stderr returned
MAX_CONCURRENT = 8          # requests handled at once; the rest wait
REQUEST_TIMEOUT = 10        # seconds to receive a request / send a reply
# Spawn refuses beyond this many worker containers (any state). The hub
# scales by two workers at a time and each holds MAX_TAKEOVER_SERVICES
# addresses, so this is thousands of taken-over sites.
MAX_WORKERS = int(os.environ.get("ONIONHEAVEN_MAX_WORKERS", "64"))
# What each worker is told to hold at most (onionheaven_common's
# MAX_SERVICES_PER_WORKER reads the same variable with the same default).
MAX_SERVICES_PER_WORKER = int(os.environ.get("ONIONHEAVEN_MAX_SERVICES", "50"))

# --- exec verbs: argv run inside a worker, whether an address is taken,
# and the docker timeout. The strings are what onionheaven_common and the
# heartbeat used to pass to `docker exec` themselves.
_BOOTSTRAP_PHASE_SH = (
    "COOKIE=$(xxd -p -c 64 /var/lib/tor/control_auth_cookie 2>/dev/null) && "
    "printf 'AUTHENTICATE %s\\r\\nGETINFO status/bootstrap-phase\\r\\nQUIT\\r\\n' "
    "\"$COOKIE\" | nc -q 1 127.0.0.1 9051 2>/dev/null || true")
_ONIONS_DETACHED_PY = (
    "import socket, binascii\n"
    "cookie = open('/var/lib/tor/control_auth_cookie','rb').read()\n"
    "s = socket.socket()\n"
    "s.settimeout(10)\n"
    "s.connect(('127.0.0.1',9051))\n"
    "s.send(('AUTHENTICATE ' + binascii.hexlify(cookie).decode() + '\\r\\n').encode())\n"
    "s.recv(256)\n"
    "s.send(b'GETINFO onions/detached\\r\\n')\n"
    "data = b''\n"
    "while True:\n"
    "    chunk = s.recv(8192)\n"
    "    if not chunk: break\n"
    "    data += chunk\n"
    "    if b'250 OK' in data: break\n"
    "s.close()\n"
    "for l in data.decode().strip().split('\\n'):\n"
    "    l = l.strip()\n"
    "    if l and not l.startswith('250') and l != '.':\n"
    "        print(l + '.onion')\n")
_WORKER_ALIVE_SH = "ps aux | grep '[o]nionheaven-takeover-worker\\.py' | grep -v grep"

EXEC_VERBS = {
    # verb: (argv inside the worker, takes an address, timeout)
    "takeover": (["python3", QUEUE_MANAGER, "takeover"], True, 30),
    "release": (["python3", QUEUE_MANAGER, "release"], True, 30),
    "has": (["python3", QUEUE_MANAGER, "has"], True, 10),
    "addr_state": (["python3", QUEUE_MANAGER, "addr_state"], True, 10),
    "status": (["python3", QUEUE_MANAGER, "status"], False, 10),
    "bootstrap-phase": (["sh", "-c", _BOOTSTRAP_PHASE_SH], False, 10),
    "onions-detached": (["python3", "-c", _ONIONS_DETACHED_PY], False, 15),
    "alive": (["sh", "-c", _WORKER_ALIVE_SH], False, 10),
}

# Fields each op takes: name -> (pattern or set of values, required).
OPS = {
    "spawn": {"name": (TAKEOVER_WORKER_RE, True)},
    "remove": {"name": (FARM_WORKER_RE, True)},
    "list": {},
    "inspect": {"name": (FARM_WORKER_RE, True)},
    "exec": {"name": (TAKEOVER_WORKER_RE, True),
             "verb": (frozenset(EXEC_VERBS), True),
             "address": (ONION_ADDRESS_RE, False)},
    "tor-address": {},
}


def log(msg):
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    sys.stderr.write(f"[{ts}] farm: {msg}\n")
    sys.stderr.flush()


def _printable(value, limit=80):
    """A request value for a log line: printable ASCII, bounded."""
    text = value if isinstance(value, str) else repr(value)
    text = "".join(c if 0x20 <= ord(c) < 0x7f else "?" for c in text)
    return text[:limit] + ("…" if len(text) > limit else "")


# ---------------------------------------------------------------------------
# docker
# ---------------------------------------------------------------------------

def docker(argv, timeout):
    """Run a docker CLI command; returns a CompletedProcess, or one with
    returncode None when it timed out or could not run."""
    try:
        return subprocess.run(["docker"] + argv, capture_output=True,
                              text=True, errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, None, "", "timeout")
    except OSError as e:
        return subprocess.CompletedProcess(argv, None, "", str(e))


_own_image = None


def takeover_image():
    """The image takeover workers run: the one this container runs.

    docker-compose.yml passes ONIONHEAVEN_IMAGE with the same reference as
    this service's own `image:` (the digest pin from build/image-pins.env,
    or a local build). Without it, ask Docker which image this container
    was started from. Never a bare tag: that names whatever is tagged
    locally (possibly stale) or whatever the registry serves today. A
    digest literal here would be one image behind, since it ships inside
    the image it names. None when neither is available.
    """
    image = os.environ.get("ONIONHEAVEN_IMAGE", "").strip()
    if image:
        return image
    global _own_image
    if _own_image is None:
        result = docker(["inspect", "--format", "{{.Image}}",
                         os.environ.get("HOSTNAME", "")], timeout=15)
        ref = (result.stdout or "").strip()
        if result.returncode == 0 and IMAGE_ID_RE.fullmatch(ref):
            _own_image = ref
    return _own_image


def worker_run_argv(name, image, tz="UTC"):
    """The `docker run` for a takeover worker. Every flag is fixed here;
    the request contributed the name only (validated). The same shape as
    every compose service (docker-compose.yml's header): no capability but
    what the worker uses, no raw packets on onionpress-network, and no
    setuid way back to root. The entrypoint drops to debian-tor with su as
    root (SETGID, SETUID), which no-new-privileges leaves alone; the
    watchdog, the queue manager and the tor-manager read the control cookie
    in debian-tor's 0700 /var/lib/tor (DAC_OVERRIDE).
    tests/test_container_capabilities.py holds the set."""
    return [
        "run", "-d",
        "--name", name,
        "--network", "onionpress-network",
        "--ulimit", "nofile=10000:10000",
        "--log-opt", "max-size=10m",
        "--log-opt", "max-file=3",
        "--cap-drop", "ALL",
        "--cap-add", "DAC_OVERRIDE",
        "--cap-add", "SETGID",
        "--cap-add", "SETUID",
        "--security-opt", "no-new-privileges:true",
        "-e", f"TZ={tz}",
        "-e", "TAKEOVER_WORKER=1",
        "-e", f"CONTAINER_NAME={name}",
        "-e", f"MAX_TAKEOVER_SERVICES={MAX_SERVICES_PER_WORKER}",
        # The hub data volume only: workers face Tor and need nothing from
        # onionpress-persistent-data (WordPress's shared volume).
        "-v", f"{HUB_DATA_VOLUME}:{ONIONHEAVEN_DATA_DIR}",
        "--restart", "unless-stopped",
        image,
    ]


def worker_names():
    """Names of every worker container Docker knows, any state; None when
    Docker could not be asked."""
    result = docker(["ps", "-a", "--format", "{{.Names}}"], timeout=15)
    if result.returncode != 0:
        return None
    return sorted(n for n in result.stdout.split() if FARM_WORKER_RE.fullmatch(n))


# ---------------------------------------------------------------------------
# ops
# ---------------------------------------------------------------------------

def op_spawn(req):
    name = req["name"]
    image = takeover_image()
    if not image:
        return {"ok": False, "error": "no worker image: ONIONHEAVEN_IMAGE is "
                                      "unset and this container's own image "
                                      "could not be determined"}
    names = worker_names()
    if names is None:
        return {"ok": False, "error": "docker ps failed"}
    if name not in names and len(names) >= MAX_WORKERS:
        return {"ok": False, "error": f"{len(names)} worker containers exist; "
                                      f"the limit is {MAX_WORKERS}"}
    docker(["rm", "-f", name], timeout=15)
    result = docker(worker_run_argv(name, image, os.environ.get("TZ", "UTC")),
                    timeout=30)
    if result.returncode != 0:
        return {"ok": False, "error": "docker run failed: "
                                      + _printable(result.stderr.strip(), 300)}
    return {"ok": True, "name": name}


def op_remove(req):
    result = docker(["rm", "-f", req["name"]], timeout=30)
    return {"ok": True, "removed": result.returncode == 0,
            "stderr": result.stderr[-MAX_OUTPUT:]}


def op_list(req):
    names = worker_names()
    if names is None:
        return {"ok": False, "error": "docker ps failed"}
    return {"ok": True, "names": names}


def op_inspect(req):
    result = docker(["inspect", "--format", "{{.State.Running}} {{json .Mounts}}",
                     req["name"]], timeout=10)
    if result.returncode != 0:
        return {"ok": True, "exists": False}
    state, _, mounts = result.stdout.strip().partition(" ")
    return {"ok": True, "exists": True, "running": state.lower() == "true",
            "mounts": mounts[:MAX_OUTPUT]}


def op_exec(req):
    inner, takes_address, timeout = EXEC_VERBS[req["verb"]]
    argv = ["exec", req["name"]] + inner + ([req["address"]] if takes_address else [])
    result = docker(argv, timeout=timeout)
    if result.returncode is None:
        return {"ok": False, "error": "docker exec: " + result.stderr}
    return {"ok": True, "returncode": result.returncode,
            "stdout": result.stdout[:MAX_OUTPUT],
            "stderr": result.stderr[-MAX_OUTPUT:]}


def op_tor_address(req):
    result = docker(["exec", TOR_CONTAINER, "cat", OWN_HOSTNAME_PATH], timeout=15)
    if result.returncode != 0:
        return {"ok": True, "address": None}
    addr = result.stdout.strip()
    return {"ok": True, "address": addr if ONION_ADDRESS_RE.fullmatch(addr) else None}


HANDLERS = {
    "spawn": op_spawn,
    "remove": op_remove,
    "list": op_list,
    "inspect": op_inspect,
    "exec": op_exec,
    "tor-address": op_tor_address,
}


def validate(req):
    """None when the request is well-formed, else why not."""
    if not isinstance(req, dict):
        return "request is not an object"
    op = req.get("op")
    if not isinstance(op, str) or op not in OPS:
        return f"unknown op {_printable(op)}"
    fields = OPS[op]
    for key in req:
        if key != "op" and key not in fields:
            return f"{op}: unexpected field {_printable(key)}"
    for key, (allowed, required) in fields.items():
        value = req.get(key)
        if value is None:
            if required:
                return f"{op}: {key} is required"
            continue
        if not isinstance(value, str):
            return f"{op}: {key} is not a string"
        if isinstance(allowed, frozenset):
            if value not in allowed:
                return f"{op}: unknown {key} {_printable(value)}"
        elif not allowed.fullmatch(value):
            return f"{op}: bad {key} {_printable(value)}"
    if op == "exec":
        takes_address = EXEC_VERBS[req["verb"]][1]
        if takes_address and not req.get("address"):
            return f"exec: {req['verb']} needs an address"
        if not takes_address and req.get("address") is not None:
            return f"exec: {req['verb']} takes no address"
    return None


def handle(req):
    """The reply for one request (a parsed JSON value)."""
    problem = validate(req)
    if problem:
        log(f"refused: {problem}")
        return {"ok": False, "error": problem}
    reply = HANDLERS[req["op"]](req)
    what = " ".join(_printable(req[k]) for k in ("op", "name", "verb") if k in req)
    outcome = ("ok" if reply.get("ok") else "error: " + _printable(reply.get("error"), 200))
    if "returncode" in reply:
        outcome += f" rc={reply['returncode']}"
    log(f"{what} -> {outcome}")
    return reply


# ---------------------------------------------------------------------------
# server
# ---------------------------------------------------------------------------

def _read_request(conn):
    """One line of JSON, at most MAX_REQUEST bytes. Raises ValueError."""
    conn.settimeout(REQUEST_TIMEOUT)
    data = b""
    while b"\n" not in data:
        chunk = conn.recv(MAX_REQUEST + 1 - len(data))
        if not chunk:
            break
        data += chunk
        if len(data) > MAX_REQUEST:
            raise ValueError(f"request longer than {MAX_REQUEST} bytes")
    line = data.split(b"\n", 1)[0]
    return json.loads(line.decode("utf-8"))


def serve_connection(conn, slots):
    with conn:
        with slots:
            try:
                req = _read_request(conn)
            except (ValueError, UnicodeDecodeError, OSError) as e:
                log(f"refused: unreadable request ({_printable(str(e), 120)})")
                reply = {"ok": False, "error": "unreadable request"}
            else:
                try:
                    reply = handle(req)
                except Exception as e:  # never die on one request
                    log(f"error handling request: {e!r}")
                    reply = {"ok": False, "error": "internal error"}
            try:
                conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))
            except OSError:
                pass
            _discard_unread(conn)


def _discard_unread(conn):
    """Drop what the client sent past the request (an oversized one), without
    waiting for more: on Linux, closing a socket with unread input resets
    the connection, and the client loses the reply it was sent."""
    try:
        conn.setblocking(False)
        for _ in range(16):
            if not conn.recv(MAX_REQUEST):
                break
    except OSError:
        pass


def listen():
    os.makedirs(SOCKET_DIR, mode=0o700, exist_ok=True)
    try:
        os.unlink(SOCKET_PATH)
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old_umask = os.umask(0o177)
    try:
        server.bind(SOCKET_PATH)
    finally:
        os.umask(old_umask)
    server.listen(64)
    return server


def main():
    if not os.path.exists(DOCKER_SOCKET):
        log(f"ERROR: {DOCKER_SOCKET} is not mounted; nothing to control")
    server = listen()
    slots = threading.BoundedSemaphore(MAX_CONCURRENT)

    def stop(signum, frame):
        log("stopping")
        try:
            os.unlink(SOCKET_PATH)
        except OSError:
            pass
        os._exit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    log(f"listening on {SOCKET_PATH} (image {takeover_image() or 'unknown'}, "
        f"up to {MAX_WORKERS} workers)")
    while True:
        try:
            conn, _ = server.accept()
        except OSError as e:
            log(f"accept failed: {e}")
            time.sleep(1)
            continue
        threading.Thread(target=serve_connection, args=(conn, slots),
                         daemon=True).start()


if __name__ == "__main__":
    main()

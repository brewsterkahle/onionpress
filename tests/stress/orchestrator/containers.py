"""Container lifecycle — worker and poll client management.

Replaces start_worker_container, start_all_workers, start_poll_clients,
disable_workers, enable_workers, enable_workers_silent from bash.
"""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import StressConfig
from .phases import StressLogger, run_parallel

# Type alias for the Docker wrapper
from onionpress.docker import Docker


class WorkerManager:
    """Manage stress worker containers."""

    def __init__(self, config: StressConfig, docker: Docker, logger: StressLogger):
        self.config = config
        self.docker = docker
        self.logger = logger
        self.network: str = ""
        self.stress_image: str = ""
        self.tor_image: str = ""

    def detect_network(self):
        """Get the Docker network used by onionpress-tor."""
        result = self.docker.run([
            "inspect", "onionpress-tor",
            "--format", "{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}",
        ])
        self.network = result.output.strip()
        if not self.network:
            raise RuntimeError("Could not determine OnionPress Docker network")

    def detect_images(self):
        """Detect container images to use."""
        result = self.docker.run([
            "inspect", "--format", "{{.Config.Image}}", "onionpress-tor",
        ])
        self.tor_image = result.output.strip()
        if not self.tor_image:
            raise RuntimeError("Cannot determine image from onionpress-tor container")

        self.stress_image = "ghcr.io/brewsterkahle/onionpress-stress-worker:latest"
        result = self.docker.run(["image", "inspect", self.stress_image])
        if not result.ok:
            # Try building locally
            dockerfile_dir = os.path.join(self.config.script_dir, "stress")
            self.docker.run(["build", "-t", self.stress_image, dockerfile_dir], timeout=120)
            result = self.docker.run(["image", "inspect", self.stress_image])
            if not result.ok:
                self.logger.log(f"  WARNING: Stress worker image not available, falling back to {self.tor_image}")
                self.stress_image = self.tor_image

    def start_worker(self, idx: int):
        """Start a single worker container."""
        cfg = self.config
        workers_in_ctr = cfg.workers_in_container(idx)
        ctr_name = cfg.container_name(idx)

        self.logger.log(f"  Starting container {ctr_name} ({workers_in_ctr} sites)...")
        self.docker.run(["rm", "-f", ctr_name], timeout=10)

        # Generate config files
        self._generate_torrc(idx)

        # Start container with shared worker-info volume
        self.docker.run([
            "run", "-d",
            "--name", ctr_name,
            "--network", self.network,
            "--ulimit", "nofile=10000:10000",
            "-v", f"{cfg.db_volume}:/worker-data",
            "--entrypoint", "sh",
            self.stress_image,
            "-c", "sleep infinity",
        ], timeout=30)

        # Copy scripts into container
        stress_dir = os.path.join(cfg.script_dir, "stress")
        src_dir = os.path.join(cfg.script_dir, "..", "src")
        for src, dst in [
            (os.path.join(stress_dir, "worker-server.py"), "/worker-server.py"),
            (os.path.join(stress_dir, "worker-bootstrap.py"), "/worker-bootstrap.py"),
            (os.path.join(src_dir, "onionpress", "onion_auth.py"), "/onion_auth.py"),
            (os.path.join(stress_dir, "tor-watchdog.py"), "/tor-watchdog.py"),
        ]:
            self.docker.run(["cp", src, f"{ctr_name}:{dst}"], timeout=10)

        torrc_path = os.path.join(cfg.output_dir, f"{ctr_name}-torrc")
        self.docker.run(["cp", torrc_path, f"{ctr_name}:/etc/tor/torrc"], timeout=10)

        # Generate and copy startup script
        startup = self._generate_startup(idx, workers_in_ctr)
        startup_path = os.path.join(cfg.output_dir, f"{ctr_name}-start.sh")
        with open(startup_path, "w") as f:
            f.write(startup)
        os.chmod(startup_path, 0o755)
        self.docker.run(["cp", startup_path, f"{ctr_name}:/start.sh"], timeout=10)

        # Launch startup script in background inside container
        # (must not block — start.sh waits for Tor to exit)
        self.docker.exec(ctr_name, "sh /start.sh </dev/null >/dev/null 2>&1 &", timeout=10)

    def start_all(self):
        """Start all worker containers, optionally in batches."""
        cfg = self.config
        self.detect_network()
        self.logger.log(f"Docker network: {self.network}")

        for idx in range(cfg.num_containers):
            self.start_worker(idx)

            if cfg.batch_size > 0 and (idx + 1) % cfg.batch_size == 0 and idx + 1 < cfg.num_containers:
                self.logger.log(f"  Batch of {cfg.batch_size} containers started, waiting 30s before next batch...")
                time.sleep(30)

        self.logger.log(f"Started {cfg.num_containers} stress containers")

    def disable_workers(self, fail_start: int, fail_count: int):
        """Disable HTTP responders + Tor services for a range of sites.

        Runs up to 5 in parallel with progress dots.
        """
        cfg = self.config
        self.logger.log(f"Disabling responders for sites {fail_start}..{fail_start + fail_count - 1}...")

        def _disable_one(i: int) -> bool:
            ctr_idx = i // cfg.per_ctr
            local_idx = i % cfg.per_ctr
            ctr_name = cfg.container_name(ctr_idx)
            cp = cfg.base_port + local_idx * 2
            hp = cfg.base_port + local_idx * 2 + 1

            # Disable HTTP ports
            self.docker.exec(ctr_name, [
                "curl", "-s", "-X", "POST", "http://127.0.0.1:9000/disable",
                "-H", "Content-Type: application/json",
                "-d", json.dumps({"ports": [cp, hp]}),
            ], timeout=10)

            result = self.docker.exec(ctr_name, [
                "curl", "-s", "-X", "POST", "http://127.0.0.1:9000/del_onion",
                "-H", "Content-Type: application/json",
                "-d", json.dumps({"workers": [local_idx]}),
            ], timeout=30)
            resp = result.output.strip()[:300]
            self.logger.log(f"  DEL_ONION site {i} ({ctr_name} local={local_idx}): {resp}")
            if "false-ok" in resp or "fail" in resp.lower() or "error" in resp.lower() or not result.ok:
                self.logger.log(f"WARNING: DEL_ONION issue for site {i}")
                if not result.ok:
                    return False

            return True

        succeeded, del_failures = run_parallel(
            list(range(fail_start, fail_start + fail_count)),
            _disable_one, self.logger,
        )

        if del_failures > 0:
            self.logger.log(f"WARNING: {del_failures}/{fail_count} DEL_ONION calls failed")
        self.logger.log(f"Disabled {fail_count} sites (HTTP responders + C Tor DEL_ONION)")

    def enable_workers(self, start: int, count: int, silent: bool = False):
        """Re-enable workers. If silent=True, skip re-registration and /online.

        Runs up to 5 workers in parallel (like gnu-parallel -j5) with progress dots.
        """
        cfg = self.config
        action = "no /online" if silent else "re-registering"
        self.logger.log(f"Re-enabling responders for sites {start}..{start + count - 1} ({action})...")

        def _enable_one(i: int) -> bool:
            """Enable a single worker. Returns False if ADD_ONION failed."""
            ctr_idx = i // cfg.per_ctr
            local_idx = i % cfg.per_ctr
            ctr_name = cfg.container_name(ctr_idx)
            cp = cfg.base_port + local_idx * 2
            hp = cfg.base_port + local_idx * 2 + 1

            # Timeout 150s: ADD_ONION + 60s HS_DESC wait + possible DEL+ADD retry + 60s wait
            result = self.docker.exec(ctr_name, [
                "curl", "-s", "-X", "POST", "http://127.0.0.1:9000/add_onion",
                "-H", "Content-Type: application/json",
                "-d", json.dumps({"workers": [local_idx]}),
            ], timeout=150)
            resp = result.output.strip()[:300]
            self.logger.log(f"  ADD_ONION site {i} ({ctr_name} local={local_idx}): {resp}")
            if "fail" in resp.lower() or "error" in resp.lower() or not result.ok:
                self.logger.log(f"WARNING: ADD_ONION issue for site {i}")
                if not result.ok:
                    return False

            # Re-enable HTTP responders
            self.docker.exec(ctr_name, [
                "curl", "-s", "-X", "POST", "http://127.0.0.1:9000/enable",
                "-H", "Content-Type: application/json",
                "-d", json.dumps({"ports": [cp, hp]}),
            ], timeout=10)

            # Re-register over Tor (unless silent)
            if not silent:
                self._reregister_worker(ctr_idx, local_idx, ctr_name)

            return True

        done_count, add_failures = run_parallel(
            list(range(start, start + count)),
            _enable_one, self.logger,
        )

        if add_failures > 0:
            self.logger.log(f"WARNING: {add_failures}/{count} ADD_ONION calls failed")
        suffix = ", no notifications sent" if silent else " + re-registrations over Tor"
        self.logger.log(f"Re-enabled {count} sites (C Tor ADD_ONION{suffix})")

    def _reregister_worker(self, ctr_idx: int, local_idx: int, ctr_name: str):
        """Re-register a single worker with OnionHeaven over Tor.

        Runs a compact Python script inside the container that:
        1. Reads worker info + PEM key
        2. Signs a registration payload using onion_auth
        3. Sends /online via curl over the container's SOCKS proxy
        """
        cfg = self.config
        pem_path = f"/tmp/w{ctr_idx}_{local_idx}_content.pem"

        # Compact script — runs inside container where onion_auth.py and keys live
        script = (
            "import sqlite3,json,subprocess,sys,time,os,base64;"
            "from onion_auth import sign_payload,make_timestamp;"
            "conn=sqlite3.connect('/worker-data/worker-info.db',timeout=10);"
            "conn.row_factory=sqlite3.Row;"
            f"row=conn.execute('SELECT * FROM workers WHERE container=? AND local_index=?',({ctr_idx},{local_idx})).fetchone();"
            "conn.close();"
            "exec('sys.exit(0)') if not row or not row['content_address'] else None;"
            "w=dict(row);"
            f"time.sleep({local_idx});"
            f"pem_b64=base64.b64encode(open('{pem_path}','rb').read()).decode() "
            f"if os.path.exists('{pem_path}') else w.get('arti_key_pem','');"
            "ts=make_timestamp();"
            "sig=sign_payload(base64.b64decode(w['privkey_b64']),"
            "base64.b64decode(w['pubkey_b64']),'register',"
            "w['content_address'],w['healthcheck_address'],ts);"
            "subprocess.run(['curl','-s','-X','POST',"
            f"'--socks5-hostname','w{ctr_idx}_{local_idx}:x@127.0.0.1:9050',"
            "'-H','Content-Type: application/json','-d',"
            "json.dumps({'content_address':w['content_address'],"
            "'healthcheck_address':w['healthcheck_address'],"
            f"'arti_key_pem':pem_b64,'version':'{cfg.stress_version}',"
            "'timestamp':ts,'signature':sig}),"
            f"'--max-time','60','http://{cfg.onionheaven_addr}:8083/online'],"
            "capture_output=True,timeout=75)"
        )
        self.docker.exec(ctr_name, ["python3", "-c", script], timeout=120)

    def _generate_torrc(self, idx: int):
        """Generate torrc for a C Tor worker container."""
        torrc = """SocksPort 127.0.0.1:9050
ControlPort 127.0.0.1:9051
CookieAuthentication 1
DataDirectory /var/lib/tor
Log notice stdout
"""
        path = os.path.join(self.config.output_dir, f"{self.config.container_name(idx)}-torrc")
        with open(path, "w") as f:
            f.write(torrc)

    def _generate_startup(self, idx: int, workers_in_ctr: int) -> str:
        """Generate the startup script for a worker container."""
        cfg = self.config
        env_prefix = f'STRESS_VERSION="{cfg.stress_version}" NO_HEALTHCHECK="{cfg.no_healthcheck}"'

        return f"""#!/bin/sh
# No set -e — individual failures should not kill the container

if ! python3 --version >/dev/null 2>&1; then
    apt-get update -qq && apt-get install -y -qq python3 curl >/dev/null 2>&1
fi

mkdir -p /var/lib/tor
chown -R debian-tor:debian-tor /var/lib/tor 2>/dev/null || true
chmod 700 /var/lib/tor

python3 /worker-server.py {cfg.base_port} {workers_in_ctr} {idx} &

MAX_TOR_RETRIES=3
TOR_ATTEMPT=0
while [ "$TOR_ATTEMPT" -lt "$MAX_TOR_RETRIES" ]; do
    TOR_ATTEMPT=$((TOR_ATTEMPT + 1))
    rm -f /var/lib/tor/state /var/lib/tor/lock
    su -s /bin/sh debian-tor -c "tor -f /etc/tor/torrc" &
    TOR_PID=$!
    python3 /tor-watchdog.py 120 &
    WATCHDOG_PID=$!
    wait $WATCHDOG_PID
    WATCHDOG_EXIT=$?
    if [ "$WATCHDOG_EXIT" -eq 0 ]; then
        break
    fi
    echo "tor-watchdog: retry $TOR_ATTEMPT/$MAX_TOR_RETRIES" >&2
    wait $TOR_PID 2>/dev/null
done

{env_prefix} python3 -u /worker-bootstrap.py "{cfg.onionheaven_addr}" {idx} {workers_in_ctr} {cfg.base_port} {cfg.per_ctr} > /bootstrap.log 2>&1 &

wait $TOR_PID
"""


class PollClientManager:
    """Manage poll client containers for reachability verification."""

    def __init__(self, config: StressConfig, docker: Docker, logger: StressLogger,
                 stress_image: str, network: str):
        self.config = config
        self.docker = docker
        self.logger = logger
        self.stress_image = stress_image
        self.network = network

    def start_all(self):
        """Start all poll client containers and wait for bootstrap."""
        cfg = self.config
        self.logger.log(f"Starting {cfg.num_poll_clients} polling clients...")

        for i in range(cfg.num_poll_clients):
            name = cfg.poll_client_name(i)
            self.docker.run(["rm", "-f", name], timeout=10)
            self.docker.run([
                "run", "-d",
                "--name", name,
                "--network", self.network,
                "--ulimit", "nofile=10000:10000",
                "--entrypoint", "sh",
                self.stress_image,
                "-c", """
                    mkdir -p /var/lib/tor
                    chown -R debian-tor:debian-tor /var/lib/tor 2>/dev/null || true
                    chmod 700 /var/lib/tor
                    cat > /etc/tor/torrc << EOF
SocksPort 0.0.0.0:9050
ControlPort 127.0.0.1:9051
CookieAuthentication 1
DataDirectory /var/lib/tor
Log notice stdout
EOF
                    su -s /bin/sh debian-tor -c 'tor -f /etc/tor/torrc'
                """,
            ], timeout=30)

        # Wait for bootstrap
        self.logger.log(f"Waiting for {cfg.num_poll_clients} polling clients to bootstrap...")
        prev_ready = 0
        for attempt in range(30):
            ready = 0
            for i in range(cfg.num_poll_clients):
                result = self.docker.exec(
                    cfg.poll_client_name(i),
                    ["curl", "-s", "-o", "/dev/null",
                     "--socks5-hostname", "127.0.0.1:9050",
                     "--max-time", "5", "http://example.com/"],
                    timeout=15,
                )
                if result.ok:
                    ready += 1

            if ready > prev_ready:
                self.logger.progress_dot(ready - prev_ready)
                prev_ready = ready

            if ready >= cfg.num_poll_clients:
                self.logger.progress_end(f"{cfg.num_poll_clients}/{cfg.num_poll_clients}")
                self.logger.log(f"  All {cfg.num_poll_clients} polling clients ready ({(attempt + 1) * 10}s)")
                break
            time.sleep(10)
        else:
            self.logger.progress_end(f"{prev_ready}/{cfg.num_poll_clients}")
            self.logger.log(f"WARNING: Not all polling clients bootstrapped ({prev_ready}/{cfg.num_poll_clients})")

        # Copy verify-worker.py
        stress_dir = os.path.join(cfg.script_dir, "stress")
        for i in range(cfg.num_poll_clients):
            self.docker.run([
                "cp",
                os.path.join(stress_dir, "verify-worker.py"),
                f"{cfg.poll_client_name(i)}:/verify-worker.py",
            ], timeout=10)
        self.logger.log("  Poll clients ready (verify-worker.py updated from repo)")

    def stop_all(self):
        """Remove all poll client containers in parallel."""
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = []
            for i in range(self.config.num_poll_clients):
                futures.append(executor.submit(
                    self.docker.run, ["rm", "-f", self.config.poll_client_name(i)], 10,
                ))
            for f in as_completed(futures):
                f.result()  # propagate exceptions

#!/usr/bin/env python3
"""
OnionPress Native Messaging Host

Communicates with the OnionPress browser extension using Chrome's
native messaging protocol (4-byte length-prefixed JSON over stdin/stdout).

Provides:
- Proxy port and service status
- User's .onion address
- Writes ~/.onionpress/extension-connected marker file on connection
"""

import json
import os
import struct
import subprocess
import sys
import time

APP_SUPPORT = os.path.expanduser("~/.onionpress")
PROXY_PORT = int(os.environ.get("ONIONPRESS_PROXY_PORT", 9077))


def read_message():
    """Read a native messaging message from stdin."""
    raw_length = sys.stdin.buffer.read(4)
    if not raw_length or len(raw_length) < 4:
        return None
    length = struct.unpack('@I', raw_length)[0]
    if length > 1024 * 1024:  # 1 MB limit
        return None
    data = sys.stdin.buffer.read(length)
    if len(data) < length:
        return None
    return json.loads(data.decode('utf-8'))


def send_message(msg):
    """Send a native messaging message to stdout."""
    data = json.dumps(msg).encode('utf-8')
    sys.stdout.buffer.write(struct.pack('@I', len(data)))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def _backend_nickname():
    """Docker-network hostname / onion-service nickname for this install.

    Self-contained (no relative import) — this file runs standalone as a
    subprocess invoked directly by the browser, not as part of the
    onionpress package. Mirrors key_manager.py's _backend_nickname().
    """
    config_path = os.path.join(os.path.expanduser("~"), ".onionpress", "config")
    site_type = "wordpress"
    try:
        with open(config_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("SITE_TYPE="):
                    site_type = line.split("=", 1)[1].strip()
                    break
    except OSError:
        pass
    return "site" if site_type == "static" else "wordpress"


def get_onion_address():
    """Read the user's .onion address from the Tor container."""
    try:
        # Try reading from the hostname file via docker
        docker_bin = _find_docker()
        env = _docker_env()
        nickname = _backend_nickname()
        result = subprocess.run(
            [docker_bin, "exec", "onionpress-tor",
             "cat", f"/var/lib/tor/hidden_service/{nickname}/hostname"],
            capture_output=True, text=True, timeout=5, env=env
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def is_service_running():
    """Check if OnionPress containers are running."""
    try:
        docker_bin = _find_docker()
        env = _docker_env()
        result = subprocess.run(
            [docker_bin, "ps", "--filter", "name=onionpress-tor",
             "--format", "{{.State}}"],
            capture_output=True, text=True, timeout=5, env=env
        )
        return result.returncode == 0 and "running" in result.stdout.lower()
    except Exception:
        return False


def write_extension_marker():
    """Write timestamp to ~/.onionpress/extension-connected."""
    marker_path = os.path.join(APP_SUPPORT, "extension-connected")
    try:
        os.makedirs(APP_SUPPORT, exist_ok=True)
        with open(marker_path, 'w') as f:
            f.write(str(int(time.time())))
    except Exception:
        pass


def _find_app_bundle():
    """Walk up from this file to find the enclosing .app bundle."""
    current = os.path.dirname(os.path.abspath(__file__))
    for _ in range(10):
        if current.endswith(".app") and os.path.isdir(os.path.join(current, "Contents", "MacOS")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


def _find_docker():
    """Find the docker binary."""
    # Prefer the bundled docker in the app bundle
    bundle = _find_app_bundle()
    if bundle:
        app_docker = os.path.join(bundle, "Contents", "Resources", "bin", "docker")
        if os.path.exists(app_docker):
            return app_docker
    # Fall back to PATH
    return "docker"


def _docker_env():
    """Return environment dict for docker commands."""
    env = os.environ.copy()
    colima_home = os.path.join(APP_SUPPORT, "colima")
    env["DOCKER_HOST"] = f"unix://{colima_home}/default/docker.sock"
    env["DOCKER_CONFIG"] = os.path.join(APP_SUPPORT, "docker-config")
    return env


def handle_message(msg):
    """Handle an incoming message and return a response."""
    msg_type = msg.get("type", "")

    if msg_type == "ping":
        return {"status": "ok"}

    if msg_type == "get_config":
        running = is_service_running()
        address = get_onion_address() if running else None
        return {
            "proxy_port": PROXY_PORT,
            "onion_address": address,
            "running": running,
        }

    return {"error": f"Unknown message type: {msg_type}"}


def main():
    """Main loop: read messages from stdin, send responses to stdout."""
    # Write marker on connection
    write_extension_marker()

    while True:
        msg = read_message()
        if msg is None:
            # stdin closed — browser terminated the host
            break
        response = handle_message(msg)
        send_message(response)
        # Update marker on each message
        write_extension_marker()


if __name__ == "__main__":
    main()

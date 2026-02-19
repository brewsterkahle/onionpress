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
PROXY_PORT = 9077


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


def get_onion_address():
    """Read the user's .onion address from the Tor container."""
    try:
        # Try reading from the hostname file via docker
        docker_bin = _find_docker()
        env = _docker_env()
        result = subprocess.run(
            [docker_bin, "exec", "onionpress-tor",
             "cat", "/var/lib/tor/hidden_service/wordpress/hostname"],
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


def _find_docker():
    """Find the docker binary."""
    # Prefer the bundled docker in OnionPress.app
    app_docker = "/Applications/OnionPress.app/Contents/Resources/bin/docker"
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

#!/usr/bin/env python3
"""
OnionPress Native Messaging Host

Communicates with the OnionPress browser extension using Chrome's
native messaging protocol (4-byte length-prefixed JSON over stdin/stdout).

Provides:
- Proxy port and service status
- User's .onion address
- Writes ~/.onionpress/extension-connected marker file on each message
"""

import json
import os
import struct
import subprocess
import sys
import time

APP_SUPPORT = os.path.expanduser("~/.onionpress")
DEFAULT_PROXY_PORT = 9077
DEFAULT_WP_PORT = 8080

# Same list as onionpress.browser.ALLOWED_BROWSERS (test-enforced): this
# script is started by the browser, outside the app's Python package.
ALLOWED_BROWSERS = {"Firefox", "Google Chrome", "Brave Browser", "Microsoft Edge", "Safari"}


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


def get_proxy_port():
    """The host port this user's onion proxy listens on.

    The browser starts this script, so it does not get the app's
    ONIONPRESS_PROXY_PORT. A second macOS user's ports are offset by +10000
    (19077, ...; CLAUDE.md "Multi-User Support"), so read the offset from
    what Docker published for our own WordPress container, the way
    launcher_ops.get_running_wp_port does, and fall back to 9077.
    """
    env_port = os.environ.get("ONIONPRESS_PROXY_PORT", "")
    if env_port.isdigit():
        return int(env_port)
    try:
        result = subprocess.run(
            [_find_docker(), "port", "onionpress-wordpress", "80"],
            capture_output=True, text=True, timeout=5, env=_docker_env()
        )
    except Exception:
        return DEFAULT_PROXY_PORT
    return proxy_port_from_docker_port(result.stdout) if result.returncode == 0 \
        else DEFAULT_PROXY_PORT


def proxy_port_from_docker_port(output):
    """Proxy port for `docker port onionpress-wordpress 80` output
    ("127.0.0.1:18080" -> 19077). Offsets are multiples of 10000; anything
    else is not a mapping OnionPress made, so the default is returned."""
    first = output.strip().splitlines()[0] if output.strip() else ""
    port_str = first.rsplit(":", 1)[-1]
    if not port_str.isdigit():
        return DEFAULT_PROXY_PORT
    offset = int(port_str) - DEFAULT_WP_PORT
    if offset < 0 or offset % 10000 or DEFAULT_PROXY_PORT + offset > 65535:
        return DEFAULT_PROXY_PORT
    return DEFAULT_PROXY_PORT + offset


def write_extension_marker(browser):
    """Record in ~/.onionpress/extension-connected that *browser*'s extension
    is connected now. The same JSON object onionpress.browser's
    write_extension_marker writes and extension_connected_recently reads
    (issue #318: a bare integer here was never detected). *browser* is what
    the extension says it runs in; only a known name is written."""
    if browser not in ALLOWED_BROWSERS:
        return False
    marker_path = os.path.join(APP_SUPPORT, "extension-connected")
    tmp = f"{marker_path}.{os.getpid()}.tmp"
    try:
        os.makedirs(APP_SUPPORT, exist_ok=True)
        with open(tmp, 'w') as f:
            f.write(json.dumps({"timestamp": int(time.time()), "browser": browser}))
        os.replace(tmp, marker_path)
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


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
            "proxy_port": get_proxy_port(),
            "onion_address": address,
            "running": running,
        }

    return {"error": f"Unknown message type: {msg_type}"}


def main():
    """Main loop: read messages from stdin, send responses to stdout."""
    while True:
        msg = read_message()
        if msg is None:
            # stdin closed — browser terminated the host
            break
        if not isinstance(msg, dict):
            continue
        response = handle_message(msg)
        send_message(response)
        # Every message names the browser the extension runs in
        write_extension_marker(msg.get("browser"))


if __name__ == "__main__":
    main()

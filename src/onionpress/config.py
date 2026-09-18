"""Configuration, secrets, and port detection for OnionPress.

Handles:
- Reading/writing the shell-style config file (~/.onionpress/config)
- Generating and loading database secrets (~/.onionpress/secrets)
- Detecting available port offsets for multi-user support
- Validating onion address prefixes
"""

import logging
import os
import re
import secrets
import socket
import subprocess
from dataclasses import dataclass

from .platform import OnionPressPaths

# Default config values matching config-template.txt
DEFAULTS = {
    "ADDRESS_PREFIX": "op2",
    "INSTALL_IA_PLUGIN": "yes",
    "UPDATE_ON_LAUNCH": "yes",
    "LAUNCH_ON_LOGIN": "yes",
    "PREVENT_SLEEP": "normal",
    "VM_MEMORY": "1",
    "VM_CPU": "2",
    "CLOUDFLARE_TUNNEL_TOKEN": "",
    "REGISTER_WITH_ONIONHEAVEN": "yes",
    "TOR_IMPL": "tor",
    "TOR_BRIDGE_LINES": "",
    "TOR_CLIENT_TRANSPORT_PLUGIN": "",
    "TOR_UPSTREAM_PROXY": "",
    "ONIONHEAVEN_ADDRESS": "",
    "ONIONHEAVEN_MAX_SERVICES": "10",
    "SHARE_ANALYTICS_WITH_ONIONHOME": "no",
    "ONIONHOME_ADDRESS": "op2homeiwjb4fdqnfkj5kbokvcee45zpk2pwgvpz5rrkanp5qqwxzbyd.onion",
}

# Config keys safe to share off-machine — surfaced in the local WordPress
# status page AND uploaded to OnionHome inside status.json. This is an
# ALLOWLIST, not a denylist: any key not listed here is withheld, so a
# future credential-bearing key can't leak by simply being added to config.
# CLOUDFLARE_TUNNEL_TOKEN is the notable exclusion — it's a secret.
SAFE_CONFIG_KEYS = frozenset({
    "TOR_IMPL", "ADDRESS_PREFIX",
    "VM_MEMORY", "VM_CPU", "VM_DISK",
    "INSTALL_IA_PLUGIN", "UPDATE_ON_LAUNCH", "LAUNCH_ON_LOGIN", "PREVENT_SLEEP",
    "REGISTER_WITH_ONIONHEAVEN", "ONIONHEAVEN_ADDRESS", "ONIONHEAVEN_MAX_SERVICES",
    "SHARE_ANALYTICS_WITH_ONIONHOME", "ONIONHOME_ADDRESS",
    "ONIONNAME", "ONIONNAME_REGISTERED",
})


def redact_config(config: dict) -> dict:
    """Return only the allowlisted, non-secret keys from a config dict.

    Used wherever config leaves its trust boundary — the status.json written
    into the WordPress container and the copy uploaded to OnionHome. Allowlist
    semantics mean a newly-added key (which could be a token/secret) is
    withheld until explicitly added to SAFE_CONFIG_KEYS.
    """
    return {k: v for k, v in config.items() if k in SAFE_CONFIG_KEYS}


def read_config(path: str) -> dict:
    """Read all key=value pairs from a config file.

    Skips comments (#) and blank lines. Values are NOT unquoted —
    the config format uses bare values (KEY=value), not shell quoting.
    """
    result = {}
    if not os.path.exists(path):
        return result
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, value = line.split("=", 1)
                result[key.strip()] = value.strip()
    return result


def read_value(path: str, key: str, default: str = "") -> str:
    """Read a single value from the config file."""
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1]
    except OSError:
        pass
    return default


def write_value(path: str, key: str, value: str) -> None:
    """Write a single key=value to the config file, updating in place or appending."""
    lines = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}\n"
            found = True
            break

    if not found:
        lines.append(f"{key}={value}\n")

    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def write_config(path: str, values: dict) -> None:
    """Write multiple key=value pairs, updating existing or appending new."""
    for key, value in values.items():
        write_value(path, key, value)


def ensure_config(paths: OnionPressPaths) -> None:
    """Create config file from template or defaults if it doesn't exist."""
    if os.path.exists(paths.config_file):
        return

    # Try to copy template
    if paths.app_bundle:
        template = os.path.join(
            paths.app_bundle, "Contents", "Resources", "config-template.txt"
        )
        if os.path.exists(template):
            import shutil
            shutil.copy2(template, paths.config_file)
            return

    # Write minimal defaults
    with open(paths.config_file, "w", encoding="utf-8") as f:
        for key, value in DEFAULTS.items():
            if value:  # skip empty defaults
                f.write(f"{key}={value}\n")


# -- Address prefix validation --

def validate_address_prefix(prefix: str) -> tuple[bool, str, str]:
    """Validate an onion address prefix.

    Returns:
        (valid, error_message, suggestion) tuple.
        suggestion is a corrected prefix (or "" if no fix possible).
    """
    if not prefix:
        return (True, "", "")

    # Build suggested fix: lowercase, strip invalid chars, truncate to 5
    suggested = re.sub(r"[^a-z2-7]", "", prefix.lower())[:5]

    if len(prefix) > 5 and re.match(r"^[a-z2-7]+$", prefix):
        return (
            False,
            f'Address prefix "{prefix}" is too long and would take '
            f"hours or days to generate ({len(prefix)} characters).\n\n"
            f"Maximum length is 5 characters.",
            suggested,
        )

    if not re.match(r"^[a-z2-7]+$", prefix):
        has_upper = any(c.isupper() for c in prefix)
        has_digits_089 = any(c in "0189" for c in prefix)

        msg = f'Address prefix "{prefix}" contains invalid characters.\n\n'
        msg += "Onion addresses use base32 encoding:\n"
        msg += "  Allowed letters:  a-z\n"
        msg += "  Allowed numbers:  2, 3, 4, 5, 6, 7\n"
        msg += "  NOT allowed:  0, 1, 8, 9\n"

        if has_upper:
            msg += "\nUppercase letters will be lowercased."
        if has_digits_089:
            bad_digits = sorted(set(c for c in prefix if c in "0189"))
            msg += (
                f"\nDigits {', '.join(bad_digits)} are not valid in base32 "
                "and will be removed."
            )

        return (False, msg, suggested)

    return (True, "", prefix)


# -- Secrets --

@dataclass
class Secrets:
    """Database secrets for docker compose."""
    wordpress_db_password: str
    mysql_password: str
    mysql_root_password: str

    def as_env(self) -> dict:
        """Return as environment variables for docker compose."""
        return {
            "WORDPRESS_DB_PASSWORD": self.wordpress_db_password,
            "MYSQL_PASSWORD": self.mysql_password,
            "MYSQL_ROOT_PASSWORD": self.mysql_root_password,
        }


def _generate_password(length: int = 32) -> str:
    """Generate a random alphanumeric password."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def load_secrets(path: str) -> Secrets:
    """Load secrets from a shell-style secrets file.

    Format: KEY='value' (single-quoted) or KEY=value (bare).
    """
    values = {}
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                # Strip single quotes
                if val.startswith("'") and val.endswith("'"):
                    val = val[1:-1]
                values[key] = val

    return Secrets(
        wordpress_db_password=values.get("WORDPRESS_DB_PASSWORD", ""),
        mysql_password=values.get("MYSQL_PASSWORD", ""),
        mysql_root_password=values.get("MYSQL_ROOT_PASSWORD", ""),
    )


def ensure_secrets(path: str) -> Secrets:
    """Generate secrets file if missing, then load and return Secrets.

    Sets file permissions to 600 (owner-only read/write).
    """
    if not os.path.exists(path):
        wp_pass = _generate_password()
        root_pass = _generate_password()

        # Create with restricted permissions
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("# Database passwords - auto-generated\n")
            f.write("# DO NOT SHARE THESE PASSWORDS\n")
            f.write(f"WORDPRESS_DB_PASSWORD='{wp_pass}'\n")
            f.write(f"MYSQL_PASSWORD='{wp_pass}'\n")
            f.write(f"MYSQL_ROOT_PASSWORD='{root_pass}'\n")

    return load_secrets(path)


# -- Port detection --

@dataclass
class PortConfig:
    """Port configuration for multi-user support."""
    offset: int
    wp_port: int
    socks_port: int
    proxy_port: int


def stop_stale_colima(colima_bin: str, colima_home: str, pid_file: str) -> None:
    """Stop an orphaned Colima VM left over from a crash or force-quit.

    If our Colima VM is running but the MenubarApp PID file is stale (or
    missing), the VM is orphaned and holding ports.  Stop it so the next
    launch gets port 8080 instead of needlessly offsetting.
    """
    log = logging.getLogger("onionpress")

    # If a live MenubarApp already owns these ports, leave them alone
    if os.path.exists(pid_file):
        try:
            with open(pid_file) as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, 0)  # raises if not alive
            return  # another instance is running — not stale
        except (ProcessLookupError, ValueError, OSError):
            pass  # stale PID file — fall through

    # Check if the onionpress Colima VM is running
    env = os.environ.copy()
    env["COLIMA_HOME"] = colima_home
    env["LIMA_HOME"] = os.path.join(colima_home, "_lima")
    env["LIMA_INSTANCE"] = "onionpress"

    try:
        result = subprocess.run(
            [colima_bin, "list", "--json"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=10, env=env,
        )
        if result.returncode != 0 or "Running" not in result.stdout:
            return  # VM not running — nothing to clean up
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return

    log.warning("Found orphaned Colima VM from a previous crash — stopping it")
    try:
        subprocess.run(
            [colima_bin, "stop"],
            capture_output=True, timeout=60, env=env,
        )
        log.warning("Orphaned Colima VM stopped successfully")
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning(f"Failed to stop orphaned Colima VM: {e}")


def detect_port_offset() -> PortConfig:
    """Detect available port offset for multi-user / port-conflict support.

    Probes all three host-exposed ports (WP 8080, SOCKS 9050, PROXY 9077);
    if ANY is in use, bumps the offset by 10000 and retries. Uses real
    socket binding so it detects ports bound by other users or system
    services (e.g. a system tor on 9050 from torbrowser-launcher's apt
    deps) — not just other OnionPress instances. lsof would only show
    the current user's processes.
    """
    offset = 0
    while True:
        ports = (8080 + offset, 9050 + offset, 9077 + offset)
        if max(ports) > 65535:
            offset = 0  # fall back to default
            break
        all_free = True
        for p in ports:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            except OSError:
                # Can't even create a socket — give up
                offset = 0
                return PortConfig(
                    offset=offset,
                    wp_port=8080 + offset,
                    socks_port=9050 + offset,
                    proxy_port=9077 + offset,
                )
            try:
                s.bind(("127.0.0.1", p))
            except OSError:
                all_free = False
                s.close()
                break
            s.close()
        if all_free:
            break
        offset += 10000

    return PortConfig(
        offset=offset,
        wp_port=8080 + offset,
        socks_port=9050 + offset,
        proxy_port=9077 + offset,
    )

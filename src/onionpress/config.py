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

from . import launcher_ops
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
    # On by default; empty reads as "yes" too, only "no" turns it off
    # (see onionheaven.registration_choice).
    "REGISTER_WITH_ONIONHEAVEN": "yes",
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
# The settings page posts back every field it shows, so a field whose key
# is withheld here is reset on save: START_ON_BOOT (Linux's LAUNCH_ON_LOGIN)
# and the hub's takeover tunables are listed for that reason.
SAFE_CONFIG_KEYS = frozenset({
    "ADDRESS_PREFIX",
    "VM_MEMORY", "VM_CPU", "VM_DISK",
    "INSTALL_IA_PLUGIN", "UPDATE_ON_LAUNCH", "LAUNCH_ON_LOGIN", "START_ON_BOOT",
    "PREVENT_SLEEP",
    "REGISTER_WITH_ONIONHEAVEN", "ONIONHEAVEN_ADDRESS", "ONIONHEAVEN_MAX_SERVICES",
    "ONIONHEAVEN_PROPAGATION_DELAY", "ONIONHEAVEN_HUB_THRESHOLD",
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


# -- Settings page updates --
#
# The WordPress Settings page (onionpress-settings.php) asks for config
# changes by writing config-updates.json into the onionpress-data volume —
# which anything running inside WordPress can write too, so the host treats
# that file as untrusted input. Only the keys below are accepted, each value
# must pass its check, and everything else is dropped and logged by name.
#
# Never add: the image overrides (ONIONPRESS_*_IMAGE run with Docker access
# and the onion key), ONIONHEAVEN_ADDRESS (the hub the heartbeat sends the
# onion private key to), ONIONHEAVEN_ACCEPT_REGISTRATIONS, ADDRESS_PREFIX
# (read-only on the page), ports, paths, or secrets other than the tunnel
# token. The page shows ADDRESS_PREFIX and ONIONHEAVEN_ADDRESS read-only.

def _one_of(*allowed):
    return lambda v: v in allowed


def _int_between(low, high):
    return lambda v: bool(re.fullmatch(r"[0-9]{1,6}", v)) and low <= int(v) <= high


# A Cloudflare Tunnel token is base64 (of a small JSON object). The page
# checks the same pattern before it asks for a change.
CLOUDFLARE_TOKEN_PATTERN = r"[A-Za-z0-9+/_-]{32,2048}={0,2}"

SETTINGS_PAGE_KEYS = {
    "UPDATE_ON_LAUNCH": _one_of("yes", "no"),
    "LAUNCH_ON_LOGIN": _one_of("yes", "no"),      # macOS
    "START_ON_BOOT": _one_of("yes", "no"),        # Linux
    "PREVENT_SLEEP": _one_of("normal", "on-battery", "never"),
    "VM_MEMORY": _int_between(1, 64),
    "REGISTER_WITH_ONIONHEAVEN": _one_of("", "yes", "no"),
    "SHARE_ANALYTICS_WITH_ONIONHOME": _one_of("yes", "no"),
    # Write-only on the page; "" is its explicit "remove token".
    "CLOUDFLARE_TUNNEL_TOKEN":
        lambda v: v == "" or bool(re.fullmatch(CLOUDFLARE_TOKEN_PATTERN, v)),
    # Takeover tunables, shown only on an active OnionHeaven hub.
    "ONIONHEAVEN_MAX_SERVICES": _int_between(1, 500),
    "ONIONHEAVEN_PROPAGATION_DELAY": _int_between(60, 3600),
    "ONIONHEAVEN_HUB_THRESHOLD": _int_between(1, 1000),
}

# Keys whose values never appear in a log.
SECRET_CONFIG_KEYS = frozenset({"CLOUDFLARE_TUNNEL_TOKEN"})

# Rejected key names are logged, and they come from WordPress: cap how many.
_MAX_REJECTED_NAMES = 20


def loggable_value(key: str, value: str) -> str:
    """*value* as it may appear in a log: a secret becomes <set> or <empty>."""
    if key in SECRET_CONFIG_KEYS:
        return "<set>" if value else "<empty>"
    return value


def describe_change(key: str, old: str, new: str) -> str:
    """One log line for a config change, without secret values."""
    if key in SECRET_CONFIG_KEYS:
        if not new:
            return f"{key}: removed"
        return f"{key}: {'replaced' if old else 'set'}"
    return f"{key}: {old!r} → {new!r}"


def _loggable_key(key) -> str:
    """A WordPress-supplied key name, safe to put in a log line."""
    if isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", key):
        return key
    return re.sub(r"[^A-Za-z0-9_.-]", "?", str(key)[:64]) or "?"


@dataclass
class SettingsUpdate:
    """What apply_settings_updates did with a config-updates.json payload."""
    accepted: dict  # every allowed key/value, changed or not
    changes: list   # (key, old, new) for each value it wrote
    rejected: list  # log-safe names of the keys it dropped (capped)


def filter_settings_updates(updates) -> tuple[dict, list]:
    """Split a Settings page payload into (accepted, rejected key names).

    A key is accepted only if it is in SETTINGS_PAGE_KEYS and its value is
    a string with no control characters (a newline would add a config line)
    that passes the key's check.
    """
    if not isinstance(updates, dict):
        return {}, ["<payload is not an object>"]
    accepted, rejected = {}, []
    for key, value in updates.items():
        check = SETTINGS_PAGE_KEYS.get(key)
        if (check is None or not isinstance(value, str)
                or re.search(r"[\x00-\x1f\x7f\x85\u2028\u2029]", value)
                or not check(value)):
            rejected.append(_loggable_key(key))
            continue
        accepted[key] = value
    if len(rejected) > _MAX_REJECTED_NAMES:
        extra = len(rejected) - _MAX_REJECTED_NAMES
        rejected = rejected[:_MAX_REJECTED_NAMES] + [f"(+{extra} more)"]
    return accepted, rejected


def apply_settings_updates(path: str, updates) -> SettingsUpdate:
    """Write the acceptable part of a Settings page payload to the config."""
    accepted, rejected = filter_settings_updates(updates)
    current = read_config(path)
    result = SettingsUpdate(accepted=accepted, changes=[], rejected=rejected)
    for key, value in accepted.items():
        old = current.get(key, "")
        if old != value:
            write_value(path, key, value)
            result.changes.append((key, old, value))
    return result


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

# THE rules for an onion address prefix. Every entry point must use
# validate_address_prefix() rather than re-deriving these, because they did
# not used to agree: the macOS launcher capped at 5 while the Linux path
# (cli.py and launcher_ops.py) accepted 2-6 and checked no character set at
# all. The same ~/.onionpress/config therefore behaved differently per
# platform — a 6-character prefix was silently downgraded to "op2" on macOS
# and accepted on Linux, and a prefix containing 0, 1, 8 or 9 (not base32, so
# no address can ever match it) was handed straight to mkp224o, which would
# search forever.
ADDRESS_PREFIX_MIN = 2
ADDRESS_PREFIX_MAX = 5
ADDRESS_PREFIX_CHARS = "a-z2-7"


def validate_address_prefix(prefix: str) -> tuple[bool, str, str]:
    """Validate an onion address prefix.

    An empty prefix is valid and means "use the default" — callers substitute
    DEFAULTS["ADDRESS_PREFIX"].

    Returns:
        (valid, error_message, suggestion) tuple.
        suggestion is a corrected prefix (or "" if no fix possible).
    """
    if not prefix:
        return (True, "", "")

    # Build suggested fix: lowercase, strip invalid chars, truncate to max
    suggested = re.sub(
        f"[^{ADDRESS_PREFIX_CHARS}]", "", prefix.lower())[:ADDRESS_PREFIX_MAX]

    if len(prefix) > ADDRESS_PREFIX_MAX and re.fullmatch(f"[{ADDRESS_PREFIX_CHARS}]+", prefix):
        return (
            False,
            f'Address prefix "{prefix}" is too long and would take '
            f"hours or days to generate ({len(prefix)} characters).\n\n"
            f"Maximum length is {ADDRESS_PREFIX_MAX} characters.",
            suggested,
        )

    if not re.fullmatch(r"[a-z2-7]+", prefix):
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

    if len(prefix) < ADDRESS_PREFIX_MIN:
        return (
            False,
            f'Address prefix "{prefix}" is too short.\n\n'
            f"Minimum length is {ADDRESS_PREFIX_MIN} characters.",
            "",
        )

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

    @classmethod
    def from_offset(cls, offset: int) -> "PortConfig":
        """Build the port triple from an offset — the one place 8080/9050/9077
        get added to it, so the three ports can't drift apart across callers."""
        return cls(
            offset=offset,
            wp_port=8080 + offset,
            socks_port=9050 + offset,
            proxy_port=9077 + offset,
        )


def stop_stale_colima(colima_bin: str, colima_home: str, pid_file: str,
                      log_func=None) -> None:
    """Stop an orphaned Colima VM left over from a crash or force-quit.

    If our Colima VM is running but the MenubarApp PID file is stale (or
    missing), the VM is orphaned and holding ports.  Stop it so the next
    launch gets port 8080 instead of needlessly offsetting.

    log_func receives the progress lines; the MenubarApp passes its own
    logger. Without one they go to the "onionpress" logging logger, which
    has no handler in the app, so they only ever reached stderr.
    """
    _logger = logging.getLogger("onionpress")

    def warn(msg: str) -> None:
        if log_func:
            log_func(msg)
        else:
            _logger.warning(msg)

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

    warn("Found orphaned Colima VM from a previous crash — stopping it (up to 60s)")
    try:
        subprocess.run(
            [colima_bin, "stop"],
            capture_output=True, timeout=60, env=env,
        )
        warn("Orphaned Colima VM stopped successfully")
    except (subprocess.TimeoutExpired, OSError) as e:
        warn(f"Failed to stop orphaned Colima VM: {e}")


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
        pc = PortConfig.from_offset(offset)
        if max(pc.wp_port, pc.socks_port, pc.proxy_port) > 65535:
            return PortConfig.from_offset(0)  # fall back to default
        all_free = True
        for p in (pc.wp_port, pc.socks_port, pc.proxy_port):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            except OSError:
                # Can't even create a socket — give up
                return PortConfig.from_offset(0)
            try:
                s.bind(("127.0.0.1", p))
            except OSError:
                all_free = False
                s.close()
                break
            s.close()
        if all_free:
            return pc
        offset += 10000


def resolve_port_offset() -> PortConfig:
    """Return the port offset our own stack is actually using.

    detect_port_offset() allocates: it binds candidate ports and returns
    the first free one, which is correct before anything is running. Once
    our WordPress container is up, its ports are bound (by the container
    itself), so a fresh bind test just reports "in use" without saying
    which port that is — including the mundane case where the caller is
    asking about the very stack it's part of.

    Read the running container's published port first (authoritative —
    Docker already made this mapping) and only fall back to allocation
    when nothing is running yet. Callers that need the multi-user
    allocation behavior with no risk of misreading a foreign container
    (e.g. a first-launch check before anything of ours exists) should call
    detect_port_offset() directly instead.
    """
    running_port = launcher_ops.get_running_wp_port()
    if running_port is not None:
        return PortConfig.from_offset(running_port - 8080)
    return detect_port_offset()

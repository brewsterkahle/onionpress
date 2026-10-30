"""Configuration for OnionPress.

Handles:
- Reading/writing the shell-style config file (~/.onionpress/config)
- The Settings page allowlist and `onionpress config` (CLI_CONFIG_KEYS)
- Validating onion address prefixes

Database secrets live in secrets_file.py and port selection in ports.py
(issue #482); their names are re-exported here so existing `config.X`
callers keep working.
"""

import contextlib
import fcntl
import os
import re
import stat
import sys
from dataclasses import dataclass, field

from .platform import OnionPressPaths
from .ports import (  # noqa: F401  (re-exported, #482)
    PortConfig, detect_port_offset, get_running_wp_port, resolve_port_offset,
)
from .secrets_file import (  # noqa: F401  (re-exported, #482)
    Secrets, _generate_password, ensure_secrets, load_secrets,
)


# Default config values matching config-template.txt
DEFAULTS = {
    "ADDRESS_PREFIX": "op2",
    "INSTALL_IA_PLUGIN": "yes",
    # Images are pinned by digest and a launch pulls only the pins a
    # release moved; yes re-pulls every image at every launch, which
    # contacts the registry each time (issue #36).
    "UPDATE_ON_LAUNCH": "no",
    "LAUNCH_ON_LOGIN": "yes",
    "PREVENT_SLEEP": "normal",
    "VM_MEMORY": "1",
    "VM_CPU": "2",
    "CLOUDFLARE_TUNNEL_TOKEN": "",
    # On by default; empty reads as "yes" too, only "no" turns it off
    # (see onionheaven.registration_choice).
    "REGISTER_WITH_ONIONHEAVEN": "yes",
    "ONIONHEAVEN_ADDRESS": "",
    # The containers' own default (onionheaven_common.MAX_SERVICES_PER_WORKER,
    # onionheaven-farm.py, docker-compose.yml): one number everywhere.
    "ONIONHEAVEN_MAX_SERVICES": "50",
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
    "ONIONHEAVEN_PROPAGATION_DELAY",
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

class _Check:
    """The check for one config key's value: callable (True when *value*
    is acceptable), with the words a refusal uses (`describe`)."""

    def __init__(self, fn, describe):
        self._fn = fn
        self.describe = describe

    def __call__(self, value):
        return bool(self._fn(value))


def _one_of(*allowed):
    words = " or ".join(repr(a) if a else "empty" for a in allowed)
    return _Check(lambda v: v in allowed, words)


def _int_between(low, high):
    return _Check(
        lambda v: bool(re.fullmatch(r"[0-9]{1,6}", v)) and low <= int(v) <= high,
        f"a whole number from {low} to {high}")


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
    "CLOUDFLARE_TUNNEL_TOKEN": _Check(
        lambda v: v == "" or bool(re.fullmatch(CLOUDFLARE_TOKEN_PATTERN, v)),
        "a Cloudflare Tunnel token (base64), or empty to remove it"),
    # Takeover tunables, shown only on an active OnionHeaven hub. Both
    # launchers and ContainerManager hand them to compose (issue #353):
    # ONIONHEAVEN_PROPAGATION_DELAY as TOR_PROPAGATION_DELAY to tor and
    # onionheaven, ONIONHEAVEN_MAX_SERVICES under its own name to onionheaven
    # and onionheaven-farm. A new tunable needs a reader, or
    # tests/test_settings_page_keys_are_read.py fails.
    "ONIONHEAVEN_MAX_SERVICES": _int_between(1, 500),
    "ONIONHEAVEN_PROPAGATION_DELAY": _int_between(60, 3600),
}

# Keys the page may clear but never set or replace by itself (#463): a
# tunnel token puts the site on the clear web through a tunnel whose owner
# sees this host's IP address and chooses what the tunnel reaches. ""
# (remove) is applied; a new value is held for the person at the host (the
# Mac's "clearnet-tunnel" dialog, handle_action) or refused (Linux).
PAGE_HELD_KEYS = frozenset({"CLOUDFLARE_TUNNEL_TOKEN"})

# Keys whose values never appear in a log.
SECRET_CONFIG_KEYS = frozenset({"CLOUDFLARE_TUNNEL_TOKEN"})

# A value may not hold one of these: a newline would add a config line.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f\x85\u2028\u2029]")


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
    held: dict = field(default_factory=dict)  # PAGE_HELD_KEYS values not written


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
                or _CONTROL_CHARS.search(value)
                or not check(value)):
            rejected.append(_loggable_key(key))
            continue
        accepted[key] = value
    if len(rejected) > _MAX_REJECTED_NAMES:
        extra = len(rejected) - _MAX_REJECTED_NAMES
        rejected = rejected[:_MAX_REJECTED_NAMES] + [f"(+{extra} more)"]
    return accepted, rejected


def apply_settings_updates(path: str, updates) -> SettingsUpdate:
    """Write the acceptable part of a Settings page payload to the config.
    A new value for a PAGE_HELD_KEYS key is not written: it comes back in
    ``held`` (and not in ``accepted``)."""
    accepted, rejected = filter_settings_updates(updates)
    current = read_config(path)
    held = {key: value for key, value in accepted.items()
            if key in PAGE_HELD_KEYS and value and value != current.get(key, "")}
    accepted = {k: v for k, v in accepted.items() if k not in held}
    result = SettingsUpdate(accepted=accepted, changes=[], rejected=rejected, held=held)
    for key, value in accepted.items():
        old = current.get(key, "")
        if old != value:
            write_value(path, key, value)
            result.changes.append((key, old, value))
    return result


# One rule for reading the config file, here and in the launchers'
# `config_value` (issue #404; tests/test_config_one_reader.py holds them
# together): a line is KEY's when, stripped, it starts with `KEY=`; the
# first such line wins (the one write_value sets; #323 read the token so);
# the value is everything after its first `=`, stripped. Comments (#) and blank lines are skipped. Values are NOT
# unquoted: the format is bare KEY=value, not shell quoting.
def _config_line(line: str):
    """(key, value) of a config line, or None for a comment or blank."""
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    key, value = line.split("=", 1)
    return key, value.strip()


def _read_lines(path: str) -> list:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.readlines()
    except FileNotFoundError:
        return []


def read_config(path: str) -> dict:
    """Every KEY=value of the config file, by the rule above."""
    result = {}
    try:
        lines = _read_lines(path)
    except OSError:
        return result
    for line in lines:
        parsed = _config_line(line)
        if parsed:
            result.setdefault(parsed[0], parsed[1])
    return result


def read_value(path: str, key: str, default: str = "") -> str:
    """One value of the config file, by the rule above."""
    return read_config(path).get(key, default)


@contextlib.contextmanager
def _config_lock(path: str):
    """An flock on `.config.lock` next to *path* around a read-modify-write
    (issue #404): two writers no longer lose each other's change. Going on
    without it when the lock file cannot be used, as before it existed."""
    fd = None
    try:
        fd = os.open(os.path.join(os.path.dirname(os.path.abspath(path)), CONFIG_LOCK),
                     os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError:
        if fd is not None:
            os.close(fd)
        fd = None
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)


def _replace_file(path: str, text: str) -> None:
    """Write *path* whole by a temp file and a rename (issue #404): a reader
    sees the old file or the new one, never a truncated one. A file this
    creates is 0600 (issue #397: it may hold CLOUDFLARE_TUNNEL_TOKEN),
    whatever the umask; an existing file keeps its mode."""
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        mode = 0o600
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fchmod(f.fileno(), mode)
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _with_values(lines: list, values: dict) -> list:
    """*lines* with each KEY's first line set to KEY=value and its later
    lines (duplicates a hand edit left) dropped; a new KEY is appended."""
    out, done = [], set()
    for line in lines:
        parsed = _config_line(line)
        if parsed and parsed[0] in values:
            if parsed[0] not in done:
                out.append(f"{parsed[0]}={values[parsed[0]]}\n")
                done.add(parsed[0])
            continue
        out.append(line)
    if out and not out[-1].endswith("\n"):
        out[-1] += "\n"
    out += [f"{k}={v}\n" for k, v in values.items() if k not in done]
    return out


def write_value(path: str, key: str, value: str) -> None:
    """Set KEY=value in the config file: one line for KEY, where it was."""
    write_config(path, {key: value})


def remove_value(path: str, key: str) -> None:
    """Remove every KEY= line from the config file (comments stay)."""
    with _config_lock(path):
        lines = _read_lines(path)
        kept = [line for line in lines if (_config_line(line) or ("",))[0] != key]
        if len(kept) != len(lines):
            _replace_file(path, "".join(kept))


def write_config(path: str, values: dict) -> None:
    """Set several KEY=value pairs in one locked rewrite."""
    if not values:
        return
    with _config_lock(path):
        _replace_file(path, "".join(_with_values(_read_lines(path), dict(values))))


def ensure_config(paths: OnionPressPaths) -> None:
    """Create config file from template or defaults if it doesn't exist.

    The file is 0600 (issue #397): it may hold CLOUDFLARE_TUNNEL_TOKEN,
    and the template is a 0644 bundle resource.
    """
    if os.path.exists(paths.config_file):
        return

    # Try to copy template
    if paths.app_bundle:
        template = os.path.join(
            paths.app_bundle, "Contents", "Resources", "config-template.txt"
        )
        if os.path.exists(template):
            import shutil
            shutil.copyfile(template, paths.config_file)
            os.chmod(paths.config_file, 0o600)
            return

    # Write minimal defaults
    fd = os.open(paths.config_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for key, value in DEFAULTS.items():
            if value:  # skip empty defaults
                f.write(f"{key}={value}\n")
    os.chmod(paths.config_file, 0o600)


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


# -- The command line: `onionpress config get|set|unset|list` (issue #365) --
#
# Every setting a person may change is reachable through `config set`, on
# both platforms, with the check its value gets everywhere else; the Mac
# menubar's Settings dialog and the Linux tray are callers of it, and the
# Settings page's channel (config-updates.json, SETTINGS_PAGE_KEYS above)
# stays what it is. A new setting gets its `config set` entry here first
# (its key, its check, its restart note); a UI only calls the command.
#
# Hand-edit only, refused by `config set`: the image overrides, ports,
# paths and the bind host (every ONIONPRESS_* key: they change what runs
# and where it listens) and ONIONHEAVEN_ACCEPT_REGISTRATIONS (a hub). The
# Settings page allowlist decided the same.

_ONION_ADDRESS_RE = re.compile(r"[a-z2-7]{56}\.onion")  # key_manager.ONION_ADDRESS_RE


def _onion_address_or_empty():
    return _Check(lambda v: v == "" or _ONION_ADDRESS_RE.fullmatch(v) is not None,
                  "a 56-character .onion address, or empty for the default")


def _address_prefix():
    return _Check(lambda v: validate_address_prefix(v)[0],
                  f"{ADDRESS_PREFIX_MIN} to {ADDRESS_PREFIX_MAX} characters "
                  f"a-z and 2-7, or empty for the default")


# Recorded by OnionPress at setup (the owner's login, the onionname's
# address, whether it is registered): settable so the apps write them
# through `config set` too, but not a setting a person normally changes.
RECORDED_KEYS = {
    "ONIONNAME": _Check(lambda v: v == "" or re.fullmatch(r"[A-Za-z0-9 _.@-]{1,60}", v) is not None,
                        "a WordPress login (letters, digits, space, _ . @ -)"),
    "ONIONNAME_ADDRESS": _onion_address_or_empty(),
    "ONIONNAME_REGISTERED": _one_of("yes", "no"),
}

CLI_CONFIG_KEYS = {
    **SETTINGS_PAGE_KEYS,
    "ADDRESS_PREFIX": _address_prefix(),
    "VM_CPU": _int_between(1, 64),
    "VM_DISK": _int_between(10, 2000),
    "INSTALL_IA_PLUGIN": _one_of("yes", "no"),
    "INSTALL_CACHE_ENABLER": _one_of("yes", "no"),
    "REPUBLISH_ON_UNREACHABLE": _one_of("yes", "no"),
    # macOS: Quit in the menu closes only the menu (the daemon keeps the
    # site, heartbeat and probe running) instead of taking the site down.
    "QUIT_KEEPS_SITE_RUNNING": _one_of("yes", "no"),
    # The format check of the Mac launcher's validate-oh-address
    # (key_manager.is_valid_onion_address). Whether the address answers as
    # a hub is the Settings dialog's question, asked before it saves.
    "ONIONHEAVEN_ADDRESS": _onion_address_or_empty(),
    "ONIONHOME_ADDRESS": _onion_address_or_empty(),
    **RECORDED_KEYS,
}

# What applies when a key has no line, for the keys whose default lives in
# the code that reads them rather than in DEFAULTS (config-template.txt).
CODE_DEFAULTS = {
    "START_ON_BOOT": "yes",                      # linux/onionpress's first config
    "VM_DISK": "20",                             # colima.py
    "INSTALL_CACHE_ENABLER": "yes",              # plugin_wanted in the launchers
    "REPUBLISH_ON_UNREACHABLE": "yes",           # daemon.py, reachability.py
    "QUIT_KEEPS_SITE_RUNNING": "no",             # menubar.py quit_app
    "ONIONHEAVEN_ADDRESS":                       # onionheaven.DEFAULT_ONIONHEAVEN_ADDRESS
        "oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion",
    "ONIONHEAVEN_PROPAGATION_DELAY": "180",      # onionheaven_common.PROPAGATION_DELAY
    "ONIONNAME_REGISTERED": "no",
}


def default_value(key: str) -> str:
    """The value in effect for *key* without a config line ("" if none)."""
    return DEFAULTS.get(key) or CODE_DEFAULTS.get(key, "")


HAND_EDIT_ONLY = frozenset({"ONIONHEAVEN_ACCEPT_REGISTRATIONS"})
HAND_EDIT_ONLY_PREFIX = "ONIONPRESS_"

# Settings a running OnionPress reads once, at start: a change takes effect
# at the next restart. Everything else is read from the config on its next
# use (the apps apply LAUNCH_ON_LOGIN/START_ON_BOOT and PREVENT_SLEEP at
# once, see apply_side_effects). Shared by the CLI and the Mac Settings
# dialog.
NEEDS_RESTART = frozenset({
    "ADDRESS_PREFIX", "VM_MEMORY", "VM_CPU",
    "CLOUDFLARE_TUNNEL_TOKEN",
    # Read at startup only (onionheaven.ONIONHEAVEN_ADDRESS). After the
    # restart the site registers with the new hub and the old one is asked
    # to delete its copy of the key (onionheaven.maintain_registration).
    "ONIONHEAVEN_ADDRESS",
    # The plugins are installed and activated by the start sequence.
    "INSTALL_IA_PLUGIN", "INSTALL_CACHE_ENABLER",
    # Handed to the tor and onionheaven containers' environment by compose.
    "ONIONHEAVEN_MAX_SERVICES", "ONIONHEAVEN_PROPAGATION_DELAY",
})

# Read when the VM is created, never again: the diffdisk cap is immutable.
VM_CREATION_ONLY = frozenset({"VM_DISK"})

# One line per key for `config list --help`.
CONFIG_KEY_HELP = {
    "ADDRESS_PREFIX": "prefix a new onion address is generated with (restart; the old address stops working)",
    "UPDATE_ON_LAUNCH": "re-pull every container image at each launch (default no: images are pinned, a launch fetches only what a release moved)",
    "LAUNCH_ON_LOGIN": "macOS: start OnionPress at login",
    "START_ON_BOOT": "Linux: start OnionPress at boot",
    "PREVENT_SLEEP": "normal (sleep as usual), on-battery (awake on AC) or never",
    "VM_MEMORY": "container VM memory in GB (restart)",
    "VM_CPU": "container VM CPUs (restart)",
    "VM_DISK": "container VM disk cap in GB, applied when the VM is first created",
    "INSTALL_IA_PLUGIN": "install the Wayback Machine Link Fixer plugin (restart)",
    "INSTALL_CACHE_ENABLER": "install the Cache Enabler plugin (restart)",
    "REPUBLISH_ON_UNREACHABLE": "ask Tor for fresh introduction points when the site is unreachable",
    "QUIT_KEEPS_SITE_RUNNING": "macOS: Quit closes only the menu bar app; the site keeps running (default no: Quit takes the site down)",
    "REGISTER_WITH_ONIONHEAVEN": "keep the site reachable through OnionHeaven (see: onionpress onionheaven)",
    "ONIONHEAVEN_ADDRESS": "the OnionHeaven hub's .onion address (restart)",
    "ONIONHEAVEN_MAX_SERVICES": "hub: addresses one takeover worker serves (restart)",
    "ONIONHEAVEN_PROPAGATION_DELAY": "hub: seconds after a site's last good healthcheck before its takeover (restart)",
    "SHARE_ANALYTICS_WITH_ONIONHOME": "upload scrubbed logs to OnionHome for debugging",
    "ONIONHOME_ADDRESS": "the OnionHome hub's .onion address",
    "CLOUDFLARE_TUNNEL_TOKEN": "Cloudflare Tunnel token (restart); set with `config set CLOUDFLARE_TUNNEL_TOKEN -` and the token on stdin",
    "ONIONNAME": "recorded at setup: the owner's WordPress login",
    "ONIONNAME_ADDRESS": "recorded at setup: the onionname's address",
    "ONIONNAME_REGISTERED": "recorded at setup: whether the onionname is registered",
}

def is_hand_edit_only(key: str) -> bool:
    return key in HAND_EDIT_ONLY or key.startswith(HAND_EDIT_ONLY_PREFIX)


def check_config_value(key: str, value) -> str:
    """Why `config set KEY VALUE` must refuse, or "" when it may go ahead."""
    if is_hand_edit_only(key):
        return (f"{key} can only be changed by editing the config file "
                "(image overrides, ports, paths and hub admission are not settings)")
    check = CLI_CONFIG_KEYS.get(key)
    if check is None:
        return f"{key} is not a setting (see: onionpress config list)"
    if not isinstance(value, str) or _CONTROL_CHARS.search(value):
        return f"{key}: the value may not contain control characters"
    if not check(value):
        return f"{key} must be {check.describe}"
    return ""


@dataclass
class ConfigChange:
    """What one `config set`/`unset` did."""
    key: str
    old: str
    new: str
    was_set: bool   # the key had a line before
    changed: bool   # the file was written

    def describe(self) -> str:
        if not self.changed:
            return f"{self.key}: unchanged ({loggable_value(self.key, self.new)})"
        if not self.was_set and self.key not in SECRET_CONFIG_KEYS:
            # The line was absent: its default applied (CODE_DEFAULTS for
            # the keys whose default lives in the code that reads them).
            before = default_value(self.key)
            return (f"{self.key}: {before!r} (unset) → {self.new!r}" if before
                    else f"{self.key}: unset → {self.new!r}")
        return describe_change(self.key, self.old, self.new)


def set_config_value(path: str, key: str, value: str) -> ConfigChange:
    """Write KEY=VALUE after check_config_value passed (the caller's job)."""
    current = read_config(path)
    old = current.get(key, "")
    changed = key not in current or old != value
    if changed:
        write_value(path, key, value)
    return ConfigChange(key, old, value, key in current, changed)


def set_checked_value(data_dir: str, key: str, value: str, log) -> ConfigChange | None:
    """`config set KEY VALUE` for host code that runs in-process (setup,
    the CLI's own commands): the key's check, the write, the log line and
    the change marker, as run_config_command does them. A UI process runs
    the subcommand instead. None, logged, when the check refuses."""
    reason = check_config_value(key, value)
    if reason:
        log(f"WARNING: config set {key} refused: {reason}")
        return None
    change = set_config_value(os.path.join(data_dir, "config"), key, value)
    log(f"config set: {change.describe()}")
    if change.changed:
        note_config_changed(data_dir, [key])
    return change


def unset_config_value(path: str, key: str) -> ConfigChange:
    """Remove KEY's line: the default applies again."""
    current = read_config(path)
    old = current.get(key, "")
    if key in current:
        remove_value(path, key)
    return ConfigChange(key, old, "", key in current, key in current)


def effective_config(path: str) -> dict:
    """Every CLI key with the value in effect: the file's, else DEFAULTS'
    (else ""). Secrets as loggable_value shows them, so the result can go
    anywhere a log line can."""
    current = read_config(path)
    out = {}
    for key in CLI_CONFIG_KEYS:
        value = current.get(key, default_value(key))
        out[key] = loggable_value(key, value)
    return out


def hand_edit_keys_present(path: str) -> list:
    """The hand-edit-only keys the file holds, by name."""
    return sorted(k for k in read_config(path) if is_hand_edit_only(k))


# A running Mac menubar caches some settings (PREVENT_SLEEP for caffeinate,
# the tunnel token for its menu) and does side effects of its own
# (status.json into the volume). `config set` from a terminal reaches it
# the way `.upload-analytics` does: a marker file in the data directory
# that its status loop reads and removes (take_config_changes), listing
# the keys that changed.
CONFIG_CHANGED_MARKER = ".config-changed"
# The flock writers of the config file and of the marker take (#404).
CONFIG_LOCK = ".config.lock"


def note_config_changed(data_dir: str, keys) -> None:
    """Add *keys* to the marker (kept if a reader has not taken it yet).
    Writers hold the config lock, so two never lose each other's keys."""
    keys = [k for k in keys if re.fullmatch(r"[A-Z][A-Z0-9_]*", k)]
    if not keys:
        return
    path = os.path.join(data_dir, CONFIG_CHANGED_MARKER)
    with _config_lock(path):
        present = take_config_changes(data_dir)
        for k in keys:
            if k not in present:
                present.append(k)
        try:
            _replace_file(path, "\n".join(present) + "\n")
        except OSError:
            pass


def take_config_changes(data_dir: str) -> list:
    """The keys the marker names, removing it; [] without one. The marker
    is renamed away before it is read, so a writer's new marker is either
    taken whole or left for the next reader."""
    path = os.path.join(data_dir, CONFIG_CHANGED_MARKER)
    taken = f"{path}.{os.getpid()}.taken"
    try:
        os.rename(path, taken)
    except OSError:
        return []
    try:
        with open(taken, "r", encoding="utf-8", errors="replace") as f:
            names = f.read().split()
    except OSError:
        names = []
    try:
        os.remove(taken)
    except OSError:
        pass
    return [n for n in names if re.fullmatch(r"[A-Z][A-Z0-9_]*", n)]


def restart_notes(keys) -> list:
    """What to tell the person after *keys* changed: a restart, or that the
    VM keeps its size."""
    notes = []
    restart = sorted(k for k in keys if k in NEEDS_RESTART)
    if restart:
        notes.append("Restart OnionPress for the change to take effect: "
                     + ", ".join(restart))
    for k in sorted(keys):
        if k in VM_CREATION_ONLY:
            notes.append(f"{k} applies when the container VM is first created; "
                         "an existing VM keeps its size.")
    return notes


def apply_side_effects(keys, config_path: str, os_name: str, log,
                       run=None, app_bundle: str = "", launchagent_path: str = "") -> list:
    """What the apps do right after a setting changes, done by the CLI
    (menubar.poll_config_updates / settings_ui and Linux's
    apply_config_updates do the same for the Settings page's channel):
    LAUNCH_ON_LOGIN installs or removes the Mac login item,
    START_ON_BOOT enables or disables the user units on Linux.
    PREVENT_SLEEP is the running menubar's (it owns caffeinate; the marker
    tells it) and the Linux daemon re-reads it. Returns a line per effect
    for the terminal; failures are lines too, never an exception."""
    import subprocess as _sp
    run = run or _sp.run
    lines = []
    values = read_config(config_path)
    if "LAUNCH_ON_LOGIN" in keys and os_name == "macos":
        from . import login_item
        want = values.get("LAUNCH_ON_LOGIN", DEFAULTS["LAUNCH_ON_LOGIN"]) == "yes"
        kwargs = {"path": launchagent_path} if launchagent_path else {}
        if want:
            ok = login_item.install(app_bundle, log, **kwargs)
            lines.append("Login item installed." if ok
                         else "WARNING: could not install the login item (see the log).")
        else:
            ok = login_item.remove(log, run=run, **kwargs)
            lines.append("Login item removed." if ok
                         else "WARNING: could not remove the login item (see the log).")
    if "START_ON_BOOT" in keys and os_name == "linux":
        want = values.get("START_ON_BOOT", "yes") == "yes"
        verb = "enable" if want else "disable"
        try:
            r = run(["systemctl", "--user", verb, "onionpress.service",
                     "onionpress-heartbeat.service"],
                    capture_output=True, text=True, timeout=60)
            ok = r.returncode == 0
        except (OSError, _sp.SubprocessError) as e:
            log(f"systemctl --user {verb}: {e}")
            ok = False
        if ok:
            log(f"{'Enabled' if want else 'Disabled'} start on boot")
            lines.append(f"Start on boot {'enabled' if want else 'disabled'}.")
        else:
            log(f"WARNING: Failed to {verb} start on boot")
            lines.append(f"WARNING: systemctl --user {verb} failed (see the log).")
    return lines


def run_config_command(args, data_dir: str, log, *, stdin=None, out=None,
                       err=None, os_name: str = "", app_bundle: str = "") -> int:
    """`onionpress config get|set|unset|list` (the CLI's config subcommand).

    *args* is the argparse namespace: config_command, key, value, json.
    Only the config file, the marker and the side effects above are
    touched: no Docker, no port probing. Exit 0 done, 1 not set / failed,
    2 refused (bad key or value, a secret on argv)."""
    import json as _json
    out = out or sys.stdout
    err = err or sys.stderr
    stdin = stdin or sys.stdin
    path = os.path.join(data_dir, "config")
    sub = args.config_command

    if sub == "list":
        values = effective_config(path)
        others = hand_edit_keys_present(path)
        if args.json:
            doc = {"settings": values, "hand_edit_only": others}
            print(_json.dumps(doc, indent=2, sort_keys=True), file=out)
            return 0
        current = read_config(path)
        for key in sorted(values):
            suffix = "" if key in current else (
                "  (default)" if default_value(key) else "  (unset)")
            print(f"{key}={values[key]}{suffix}", file=out)
        if others:
            print(f"# hand-edit only, in the file: {', '.join(others)}", file=out)
        return 0

    key = args.key
    json_output = bool(getattr(args, "json", False))
    if sub == "get":
        current = read_config(path)
        if key in current:
            print(loggable_value(key, current[key]), file=out)
            return 0
        if default_value(key):
            print(loggable_value(key, default_value(key)), file=out)
            return 0
        print(f"{key} is not set", file=err)
        return 1

    if sub == "unset":
        if key not in CLI_CONFIG_KEYS:
            print(f"ERROR: {check_config_value(key, '')}", file=err)
            return 2
        change = unset_config_value(path, key)
    else:  # set
        from . import confirm as _confirm
        value = args.value
        if value == "-":
            value = stdin.read()
            if value.endswith("\n"):
                value = value[:-1]
        elif key in SECRET_CONFIG_KEYS and value != "":
            return _confirm.answer_error(
                f"{key} is a secret: pass - and the value on stdin "
                f"(printf '%s' \"$TOKEN\" | onionpress config set {key} -)",
                json_output, out, err, code=2)
        reason = check_config_value(key, value)
        if reason:
            return _confirm.answer_error(reason, json_output, out, err, code=2)
        # A prefix the live address does not start with changes nothing
        # about that address (the prefix is used when a new one is made:
        # a reset, a new install), so it is asked about first.
        try:
            prefix_change_question(key, value, data_dir, _confirm.given(args))
        except _confirm.NeedsConfirmation as e:
            # --check answers the question (exit 4); a run without the
            # confirmation refuses (exit 3), naming it.
            if getattr(args, "check", False):
                return _confirm.answer_confirm(e, json_output, out, err)
            return _confirm.answer_refused_question(e, json_output, out, err)
        if getattr(args, "check", False):
            return _confirm.answer_ok(json_output, out, text=f"OK: {key} may be set.", key=key)
        change = set_config_value(path, key, value)

    log(f"config {sub}: {change.describe()}")
    if json_output:
        from . import confirm as _confirm
        lines = []
        if change.changed:
            note_config_changed(data_dir, [key])
            lines = apply_side_effects([key], path, os_name, log, app_bundle=app_bundle)
        for line in lines:
            log(line)
        print(_confirm.dumps({"ok": True, "key": key, "changed": change.changed,
                              "described": change.describe(), "side_effects": lines,
                              "restart": restart_notes([key])}), file=out)
        return 0
    print(change.describe(), file=out)
    if not change.changed:
        return 0
    note_config_changed(data_dir, [key])
    for line in apply_side_effects([key], path, os_name, log, app_bundle=app_bundle):
        print(line, file=out)
    for line in restart_notes([key]):
        print(line, file=out)
    return 0


def cached_onion_address(data_dir: str) -> str:
    """The address this install serves, from the launcher's cache file
    (`onion_address` in the data directory; written at every start), or
    "" (not running yet, a new install). No Docker here."""
    try:
        with open(os.path.join(data_dir, "onion_address"), encoding="utf-8") as f:
            addr = f.read().strip()
    except OSError:
        return ""
    return addr if _ONION_ADDRESS_RE.fullmatch(addr) else ""


def prefix_change_question(key: str, value: str, data_dir: str, confirmed) -> None:
    """`config set ADDRESS_PREFIX`: raise NeedsConfirmation("prefix-change",
    current, new, address) when the install's address does not start with
    the new prefix, or is not known (no cache file: not started yet, or
    not installed yet; "address" is "" then), and that was not confirmed
    (onionpress.confirm). The live address keeps its prefix whatever the
    setting says; the setting is read when a new address is made. Setting
    the prefix the file already has asks nothing."""
    if key != "ADDRESS_PREFIX":
        return
    from . import confirm as _confirm
    current = read_value(os.path.join(data_dir, "config"), key, "")
    if value == current:
        return
    addr = cached_onion_address(data_dir)
    new = value or DEFAULTS["ADDRESS_PREFIX"]
    if addr and addr.startswith(new):
        return
    _confirm.require("prefix-change", confirmed,
                     current=current or DEFAULTS["ADDRESS_PREFIX"], new=new, address=addr)

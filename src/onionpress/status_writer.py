# SPDX-License-Identifier: AGPL-3.0-or-later
#
# OnionPress: Copyright (C) 2026 Internet Archive and OnionPress contributors
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""One status writer for both platforms (issue #523).

Every view of a running OnionPress reads one of two documents, both
written here from one pass of the daemon's checks:

- ``daemon-state.json`` (:func:`state_document`): the daemon's own state,
  what the Mac menubar and the Linux tray read through
  ``daemon_client.read_state``, with the status document under
  ``"status"``.
- ``status.json`` (:func:`status_document`): what the WordPress status
  and Settings pages show. It goes into the shared volume as www-data
  cut to ``redact.WORDPRESS_STATUS_KEYS`` (:func:`write_volume`, with
  ``version``, ``config-current.json`` and the scrubbed
  ``recent-logs.txt``), and in full, host paths included, as
  ``~/.onionpress/status.json`` (:func:`write_host_copy`) for the
  analytics snapshot and the launcher's port detection.

The daemon (onionpress.daemon, on both platforms since issue #407) is
the one app: its ``write_status_to_volume`` is :func:`write` and its
``publish_state`` is :func:`publish`; nothing else writes these
documents (until #407 Linux's ``onionpress write-status`` built them
from one pass over the stack for the launcher's 30 s tick). What
differs between the platforms is a ``detect_os()`` seam here: the
platform's name in the documents and where the host's boot time is
read.

The fields Linux's bash gathered beside the daemon's (the tray's
``wp_installed``, ``onboarded``, ``wayback_queue_count``,
``mkp224o_available``, ``vanity_in_progress``, ``serving_from_wayback``,
``wp_admin_password_path``, ``wp_port``) are each one function here, one
docker call or one file, gathered on both platforms (:func:`gather_fields`).
"""

import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone

from . import config as op_config
from . import key_manager
from . import launcher_ops
from . import onion_hostname
from . import onionheaven
from . import redact
from . import wp_files
from .daemon_client import display_state_of, state_path
from .platform import OS, detect_os

VOLUME_DIR = "/var/lib/onionpress"
HOST_STATUS_FILENAME = "status.json"
LOG_FILENAME = "onionpress.log"

# Markers under the data dir that the status reports.
VANITY_MARKER = ".vanity-running"
WP_ADMIN_PASSWORD_FILE = "wp-admin-password"
# The mkp224o probe's answer, by the tor image's id: one `docker run` per
# image, not one per status pass.
MKP224O_PROBE_CACHE = ".mkp224o-probe"

# What the daemon shows for an address it does not have yet.
PLACEHOLDER_ADDRESSES = ("Starting...", "Not running", "Generating address...")

# status.json's `state` from the daemon's display state (daemon_client).
DISPLAY_TO_STATE = {"available": "running", "stalled": "starting"}

# The containers this install runs beside its onionpress-* ones; the
# hub's takeover and poll workers come and go, so they are not the
# stack's health.
STACK_NAMES = ("onionheaven", "onionheaven-farm", "onionpress-autoheal",
               "onionpress-autoheal-proxy")

_RFC3339 = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?Z")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_json_atomic(path, doc):
    """Write *doc* to *path* through a temp file and a rename, so a reader
    never sees a half-written file. mode 0600: the state names the
    address and the pending question's facts."""
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def platform_name(os_name=None):
    """status.json's `platform`: "macos" or "linux"."""
    os_name = os_name or detect_os()
    return "linux" if os_name == OS.LINUX else "macos"


def address_known(address):
    return bool(address) and address not in PLACEHOLDER_ADDRESSES


def load_average():
    try:
        return list(os.getloadavg())
    except OSError:
        return []


_BOOT_TIME = {}


def host_boot_time(os_name=None, now=None):
    """When the host booted (epoch seconds): `sysctl kern.boottime` on a
    Mac, /proc/uptime on Linux; now when neither answers. Read once per
    process."""
    os_name = os_name or detect_os()
    if os_name in _BOOT_TIME:
        return _BOOT_TIME[os_name]
    now = time.time() if now is None else now
    boot = now
    try:
        if os_name == OS.LINUX:
            with open("/proc/uptime", encoding="utf-8") as f:
                boot = now - float(f.read().split()[0])
        else:
            r = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True,
                               text=True, timeout=2)
            m = re.search(r"sec\s*=\s*(\d+)", r.stdout)
            if m:
                boot = int(m.group(1))
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        boot = now
    _BOOT_TIME[os_name] = boot
    return boot


# ---------------------------------------------------------------------------
# The stack, as docker tells it
# ---------------------------------------------------------------------------

def list_containers(docker):
    """[(name, state)] of this install's containers, from `docker ps -a`
    by compose project label (cheaper than the launcher's `status`: bash +
    compose + jq per tick); None when docker did not answer."""
    r = docker.run(
        ["ps", "-a", "--filter", "label=com.docker.compose.project",
         "--format", "{{.Names}}\t{{.State}}\t{{.Label \"com.docker.compose.project\"}}"],
        timeout=10, quiet=True)
    if not r.ok:
        return None
    out = []
    for line in r.output.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        name = parts[0]
        if name.startswith("onionheaven-takeover") or name.startswith("onionheaven-poll"):
            continue
        if name.startswith("onionpress-") or name in STACK_NAMES or name.startswith("onionheaven-"):
            out.append((name, parts[1]))
    return out


def tor_uptime_seconds(docker, now=None):
    """Seconds since the tor container started (`docker inspect`'s
    StartedAt), 0 when it is not running or docker did not answer."""
    now = time.time() if now is None else now
    r = docker.run(["inspect", "--format", "{{.State.StartedAt}}", "onionpress-tor"],
                   timeout=5, quiet=True)
    if not r.ok:
        return 0
    m = _RFC3339.fullmatch(r.output.strip())
    if not m or m.group(1).startswith("0001-01-01"):
        return 0
    try:
        started = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return 0
    epoch = started.timestamp() + (int(m.group(2)[:6].ljust(6, "0")) / 1e6 if m.group(2) else 0)
    return max(0, int(now - epoch))


def wp_installed(docker):
    """True once `wp core is-installed` answers yes, False when it
    answers no, None when it could not be asked."""
    try:
        return docker.wp("core", "is-installed", timeout=10, quiet=True).ok
    except Exception:
        return None


def onboarded(docker):
    """The onboarding mu-plugin's network-wide marker (get_site_option,
    where install_fresh_wordpress writes it: a per-blog option would
    diverge after the multisite conversion)."""
    r = docker.wp("eval", 'echo (string) get_site_option("onionpress_onboarded", "");',
                  timeout=15, quiet=True)
    if not r.ok:
        return False
    value = r.output.strip()
    return bool(value) and value not in ("0", "false")


def wayback_queue_count(docker):
    """Posts not yet archived (waiting plus in flight), from the Wayback
    plugin's own totals."""
    r = docker.wp("eval", "echo json_encode(onionpress_wayback_queue_totals());",
                  timeout=15, quiet=True)
    if not r.ok:
        return 0
    try:
        data = json.loads(r.output)
        return int(data.get("remaining", 0)) + int(data.get("in_flight", 0))
    except (ValueError, TypeError, AttributeError):
        return 0


def mkp224o_available(docker, data_dir, image=None):
    """Whether the tor image is here and bundles mkp224o (the setup
    dialogs offer a vanity address on it, `onionpress generate-vanity`
    checks the same image: launcher_ops.DEFAULT_TOR_IMAGE). The probe is
    a `docker run`, so its answer is kept by image id in the data dir."""
    image = image or launcher_ops.DEFAULT_TOR_IMAGE
    r = docker.run(["image", "inspect", "--format", "{{.Id}}", image], timeout=15, quiet=True)
    if not r.ok:
        return False
    image_id = r.output.strip()
    cache = os.path.join(data_dir, MKP224O_PROBE_CACHE)
    try:
        with open(cache, encoding="utf-8") as f:
            known = json.load(f)
        if (isinstance(known, dict) and known.get("image_id") == image_id
                and isinstance(known.get("available"), bool)):
            return known["available"]
    except (OSError, ValueError):
        pass
    available = launcher_ops.tor_image_has_mkp224o(image, docker=docker)
    try:
        write_json_atomic(cache, {"image_id": image_id, "available": available})
    except OSError:
        pass
    return available


def vanity_in_progress(data_dir):
    return os.path.isfile(os.path.join(data_dir, VANITY_MARKER))


def wp_admin_password_path(data_dir):
    """The first-run admin password file's path when it exists (the tray
    shows it; never copied into the volume: redact.status_for_wordpress)."""
    path = os.path.join(data_dir, WP_ADMIN_PASSWORD_FILE)
    return path if os.path.isfile(path) else None


def serving_from_wayback(state, client_registered):
    """Visitors see the hub's Wayback copy: the site is not running and
    the client is registered with a hub (a more precise signal will come
    from the hub's heartbeat answer)."""
    return state != "running" and bool(client_registered)


def gather_fields(app, now=None):
    """The fields Linux's bash gathered beside the daemon's, from *app*'s
    docker and data dir: one call each, docker asked only while the stack
    runs."""
    data_dir = app.app_support
    fields = {
        "wp_installed": False, "onboarded": False, "wayback_queue_count": 0,
        "mkp224o_available": False, "uptime_seconds": 0,
        "vanity_in_progress": vanity_in_progress(data_dir),
        "wp_admin_password_path": wp_admin_password_path(data_dir),
    }
    if not app.is_running:
        return fields
    docker = app._docker
    known = app._wp_installed
    installed = known if isinstance(known, bool) else wp_installed(docker)
    fields["wp_installed"] = bool(installed)
    if installed:
        fields["onboarded"] = onboarded(docker)
        fields["wayback_queue_count"] = wayback_queue_count(docker)
    fields["mkp224o_available"] = mkp224o_available(docker, data_dir)
    fields["uptime_seconds"] = tor_uptime_seconds(docker, now)
    return fields


def state_of(app, now=None):
    """status.json's `state` for *app*: the daemon's display state, with
    "available" said as "running" and "stalled" as "starting"."""
    display = display_state_of(app._state_fields(), now)
    return DISPLAY_TO_STATE.get(display, display)


def status_document(app, now=None):
    """status.json's content from *app*'s last pass (`gather_fields` and
    the OnionHeaven overview already taken): the Mac's fields and
    Linux's, on both platforms."""
    now = time.time() if now is None else now
    os_name = getattr(app, "os_name", None) or detect_os()
    state = state_of(app, now)
    f = app._status_fields or {}
    overview = app._onionheaven_overview or onionheaven.overview(app, hub=False)
    # server_active stays the Mac's rule: the node serving the hub's
    # address (tests/test_linux_hub_mode.py), never a running container.
    oh = onionheaven.status_block(overview, server_active=bool(app.is_onionheaven))
    addr = app.onion_address if key_manager.is_valid_onion_address(app.onion_address) else ""
    onion_reachable, onion_http_code = app._last_reachability
    return {
        "state": state,
        "version": app.version,
        "onion_address": addr,
        "onionname": app.read_config_value("ONIONNAME", ""),
        "tor_impl": "tor",
        "uptime_seconds": int(f.get("uptime_seconds", 0)),
        "bootstrap_pct": 100 if app.is_ready else app._last_bootstrap_pct,
        "onion_reachable": onion_reachable,
        "onion_http_code": onion_http_code,
        "containers": dict(app._containers_seen or {}),
        "wayback_queue_count": int(f.get("wayback_queue_count", 0)),
        "updated_at": utc_now(),
        "platform": platform_name(os_name),
        "load_avg": load_average(),
        "host_uptime_seconds": max(0, int(now - host_boot_time(os_name, now))),
        "onionheaven": oh,
        "config": op_config.redact_config(op_config.read_config(app.config_file)),
        "wp_installed": bool(f.get("wp_installed")),
        "onboarded": bool(f.get("onboarded")),
        "mkp224o_available": bool(f.get("mkp224o_available")),
        "wp_admin_password_path": f.get("wp_admin_password_path") or None,
        "vanity_in_progress": bool(f.get("vanity_in_progress")),
        "serving_from_wayback": serving_from_wayback(state, oh["client_registered"]),
        "wp_port": app.wp_port,
    }


def state_document(app, now=None):
    """daemon-state.json's content: the daemon's state as the views read
    it (daemon_client), with the status document under "status"."""
    now = time.time() if now is None else now
    fields = app._state_fields()
    known = address_known(app.onion_address)
    return {
        **fields,
        "pid": os.getpid(),
        "version": app.version,
        "updated_at": now,
        "started_at": app.startup_time,
        "display_state": display_state_of(fields, now),
        "runtime_ready": app.runtime_ready,
        "port_conflict": list(app.port_conflict),
        "ports": {"offset": app.port_offset, "wp": app.wp_port,
                  "socks": app.socks_port, "proxy": app.proxy_port},
        "onion_address": app.onion_address if known else "",
        "address_placeholder": "" if known else app.onion_address,
        "healthcheck_address": app.healthcheck_address or "",
        "is_onionheaven": bool(app.is_onionheaven),
        "bootstrap_pct": 100 if app.is_ready else app._last_bootstrap_pct,
        "cloudflare_tunnel_enabled": app.cloudflare_tunnel_enabled,
        "wp_installed": app._wp_installed,
        "wordpress_up": app._wordpress_confirmed,
        "images_present": dict(app._images_present),
        "proxy_running": app.proxy_server is not None,
        "sleeping": app._sleeping,
        "onionheaven": app._onionheaven_overview,
        "pending_question": app._pending_question,
        "onionname_collision": app._onionname_collision,
        "settings_page_error": app._settings_page_error,
        "wedge_warning": app._wedge_warning_fired,
        "probe": {"code": app._last_probe_code, "why": app._last_probe_why,
                  "ms": app._last_probe_ms,
                  "reachable": app._last_reachability[0]},
        "status": app._last_status_payload,
    }


# ---------------------------------------------------------------------------
# Writing them
# ---------------------------------------------------------------------------

def _put(docker, name, text):
    """*text* into the shared volume as www-data (wp_files), 0600."""
    return docker.run(wp_files.put_file_args(f"{VOLUME_DIR}/{name}"),
                      input=text, timeout=5, quiet=True)


def write_recent_logs(docker, log_file, data_dir):
    """recent-logs.txt for the status page's Recent Logs: the end of the
    log, scrubbed first (redact.wordpress_log_excerpt: no home path or
    user name, credentials or non-loopback IPs) and never copied raw: a
    placeholder goes instead when the scrub fails. The one writer of it."""
    if not os.path.isfile(log_file):
        return
    try:
        text = redact.wordpress_log_excerpt(log_file, data_dir)
    except OSError:
        text = (f"(Recent logs unavailable: could not scrub "
                f"{os.path.basename(log_file)} for display.)")
    _put(docker, "recent-logs.txt", text.rstrip("\n") + "\n")


def write_volume(app, status):
    """status.json (redact.status_for_wordpress: the fields the pages show,
    never a host path), version, config-current.json (the allowlisted
    config: the file is readable by PHP) and recent-logs.txt, as
    www-data."""
    docker = app._docker
    _put(docker, "status.json", json.dumps(redact.status_for_wordpress(status), indent=2))
    _put(docker, "version", app.version)
    _put(docker, "config-current.json", json.dumps(status["config"], indent=2))
    log_file = getattr(app, "log_file", None) or os.path.join(app.app_support, LOG_FILENAME)
    write_recent_logs(docker, log_file, app.app_support)


def write_host_copy(data_dir, status):
    """~/.onionpress/status.json: the full status (the heartbeat's
    analytics snapshot, the Linux launcher's port detection)."""
    write_json_atomic(os.path.join(data_dir, HOST_STATUS_FILENAME), status)


def write(app, now=None):
    """The status document from *app*'s pass: the OnionHeaven overview
    (the registry asked only on a hub, through onionheaven.overview), the
    gathered fields, the host copy, and, while the stack runs, the
    volume's four files. Never raises: a status pass that cannot write
    is said in the log."""
    now = time.time() if now is None else now
    try:
        try:
            overview = onionheaven.overview(app, hub=bool(app.is_running))
        except Exception as e:
            app.log(f"OnionHeaven: overview error: {e}")
            overview = onionheaven.overview(app, hub=False)
        app._onionheaven_overview = overview
        app._status_fields = gather_fields(app, now)
        status = status_document(app, now)
        app._last_status_payload = status
        try:
            write_host_copy(app.app_support, status)
        except OSError as e:
            app.log(f"status: could not write {HOST_STATUS_FILENAME}: {e}")
        if app.is_running:
            write_volume(app, status)
    except Exception as e:
        app.log(f"status: {e}")


def publish(app, now=None):
    """daemon-state.json, atomically."""
    write_json_atomic(state_path(app.app_support), state_document(app, now))

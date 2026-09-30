"""Opt-in analytics log sharing with OnionHome.

Background thread that periodically uploads completed (rolled) log files
to the OnionHome hub for remote debugging.  Only runs when the user has
set ``SHARE_ANALYTICS_WITH_ONIONHOME=yes`` in their config.
"""

import base64
import gzip
import hashlib
import io
import json
import os
import platform
import re
import subprocess
import threading
import time

# Permissive "safe filename" check used when the server asks for a file
# back. Log naming can evolve on the client side faster than both ends
# can be redeployed in lockstep, so we only enforce that the name can't
# escape its storage directory. OnionHome can tighten this on its side
# if a rogue instance starts spamming weird names.
_LOG_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")


_upload_now = threading.Event()


def trigger_upload():
    """Wake the sharing loop to upload immediately."""
    _upload_now.set()


def start_analytics_sharing(app):
    """Start the analytics sharing daemon thread.

    Called once from menubar.py after services are running.
    """
    thread = threading.Thread(target=_sharing_loop, args=(app,), daemon=True)
    thread.start()
    return thread


def _sharing_loop(app):
    """Sleep until this instance's designated hour, upload, repeat daily.

    If the instance was offline and missed its window, uploads on the
    next wake-up after a short delay (so Tor has time to reconnect).
    """
    from datetime import datetime, timezone, timedelta

    upload_hour = _pick_upload_hour(app)
    last_upload_date = None
    # Transient-failure retry state. Reset when the date changes so a
    # 24-hour outage doesn't permanently poison the retry counter.
    failed_attempts_today = 0
    failed_attempts_date = None
    MAX_RETRIES_PER_DAY = 5
    # Progressive backoff between failed retries (seconds): 60s, 3m,
    # 10m, 30m. With MAX_RETRIES_PER_DAY=5 (initial + 4 retries) and
    # the 60s initial-wake buffer, total elapsed before giving up is
    # ~45 min, which covers "Tor bootstrap is slow this morning"
    # without hammering OnionHome when it's genuinely down.
    RETRY_DELAYS = (60, 180, 600, 1800)

    while True:
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")

        # Reset retry counter on date rollover.
        if failed_attempts_date != today:
            failed_attempts_today = 0
            failed_attempts_date = today

        if last_upload_date == today:
            # Already uploaded today — sleep until tomorrow's window
            target = now.replace(hour=upload_hour, minute=0, second=0, microsecond=0)
            target += timedelta(days=1)
            _upload_now.wait((target - now).total_seconds())
        elif failed_attempts_today >= MAX_RETRIES_PER_DAY:
            # Exhausted today's retry budget — sleep until tomorrow.
            target = now.replace(hour=upload_hour, minute=0, second=0, microsecond=0)
            target += timedelta(days=1)
            _upload_now.wait((target - now).total_seconds())
        elif now.hour >= upload_hour:
            # Hit/missed the window; retry with progressive backoff so
            # a transient Tor hiccup right after startup doesn't lose
            # the day's upload but we also don't hammer OnionHome.
            if failed_attempts_today == 0:
                # First attempt this window — short buffer after wake.
                _upload_now.wait(60)
            else:
                delay = RETRY_DELAYS[min(failed_attempts_today - 1,
                                         len(RETRY_DELAYS) - 1)]
                _upload_now.wait(delay)
        else:
            # Haven't hit our window yet today — sleep until it
            target = now.replace(hour=upload_hour, minute=0, second=0, microsecond=0)
            _upload_now.wait((target - now).total_seconds())

        _upload_now.clear()

        if getattr(app, "_sleeping", False):
            continue

        try:
            enabled = app.read_config_value(
                "SHARE_ANALYTICS_WITH_ONIONHOME", "no"
            ).lower()
        except Exception:
            continue
        if enabled != "yes":
            continue

        # Precondition gate: only need internet, not our own onion to
        # be reachable. Uploading goes to OnionHome over Tor, which
        # works as soon as the Tor client has circuits — well before
        # our onion's descriptor has propagated. Gating on is_ready
        # used to cost us the first 1–5 min of every boot. A
        # "no internet" isn't a failure worth burning a retry slot —
        # come back later. If OnionHome itself is unreachable, the
        # manifest POST below returns "manifest_failed" and the outer
        # loop applies its own backoff.
        try:
            online = app.check_internet_connectivity()
        except Exception:
            online = True  # conservative: let the upload try
        if not online:
            app.log("Analytics sharing: no internet, will retry")
            _upload_now.wait(60)
            continue

        try:
            # Include today's active logs too — otherwise a day-1 install
            # ships only launcher.log (the only file globbed unconditionally)
            # because no other log has rolled yet. OnionHome re-requests when
            # the offered size grows, so subsequent days supersede cleanly.
            result = _do_upload_cycle(app, include_active=True) or {}
            status = result.get("status", "unknown")
            if status == "manifest_failed":
                # Likely transient: Tor circuit not ready yet, or OnionHome
                # briefly unreachable. Don't burn the day — bump the retry
                # counter and let the outer loop wait per RETRY_DELAYS.
                # Give up only after MAX_RETRIES_PER_DAY attempts.
                failed_attempts_today += 1
                app.log(
                    f"Analytics sharing: manifest failed "
                    f"(retry {failed_attempts_today}/{MAX_RETRIES_PER_DAY})"
                )
            else:
                last_upload_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        except Exception as e:
            app.log(f"Analytics sharing error: {e}")


def _pick_upload_hour(app):
    """Derive a stable hour (0-23) from the onion address for upload jitter."""
    addr = getattr(app, "onion_address", "") or ""
    if addr:
        h = hashlib.sha256(addr.encode()).digest()
        return h[0] % 24
    # Fallback: random hour per process
    import random
    return random.randint(0, 23)


def _do_upload_cycle(app, include_active=False):
    """Collect completed logs, send manifest, upload wanted files.

    When *include_active* is True (manual upload), current/active log files
    are included alongside completed ones.  OnionHome re-requests a file if
    the offered size is larger than what it already has.

    Returns a dict with "status" (one of: "no_files", "no_onion",
    "sign_error", "manifest_failed", "none_wanted", "ok") plus "wanted"
    and "uploaded" counts when "ok". Callers decide how to surface the
    outcome — this function only logs per-file events, not the verdict.
    """
    # Collect completed files from all rotating logs
    all_files = []
    log_instances = [
        getattr(app, "_onionpress_log", None),
        getattr(app, "_wp_access_log", None),
        getattr(app, "_wp_visitors_log", None),
        getattr(app, "_tor_log", None),
        getattr(app, "_onionheaven_log", None),
        # WordPress container stderr — PHP error_log() output from
        # plugins. Primary source for Wayback archive-state telemetry.
        getattr(app, "_wp_errors_log", None),
        getattr(app, "_clearnet_log", None),
    ]

    import glob as _glob
    logs_dir = os.path.join(getattr(app, "app_support", ""), "logs")
    for log_inst in log_instances:
        if log_inst is not None:
            all_files.extend(log_inst.completed_files())
            if include_active:
                # Include the current active file too
                path = log_inst.current_path()
                if os.path.exists(path):
                    try:
                        size = os.path.getsize(path)
                        if size > 0:
                            name = os.path.basename(path)
                            # Avoid duplicates
                            if not any(f["name"] == name for f in all_files):
                                all_files.append({"name": name, "size": size, "path": path})
                    except OSError:
                        pass

    # Scan logs dir for any other container-* rotating logs whose RotatingLog
    # instance isn't held as an attribute on `app` (e.g. container-db,
    # container-cloudflared, and takeover-worker logs that get spun up
    # dynamically). Without this, those files exist on disk but never ship.
    container_patterns = ["container-*.log.gz"]
    if include_active:
        container_patterns.append("container-*.log")
    for pattern in container_patterns:
        for p in sorted(_glob.glob(os.path.join(logs_dir, pattern))):
            name = os.path.basename(p)
            if any(f["name"] == name for f in all_files):
                continue
            try:
                size = os.path.getsize(p)
                if size > 0:
                    all_files.append({"name": name, "size": size, "path": p})
            except OSError:
                pass

    # Include launcher rotating logs (written by shell script, not RotatingLog)
    import glob as _glob_launcher
    for p in sorted(_glob_launcher.glob(os.path.join(logs_dir, "launcher-*.log"))):
        name = os.path.basename(p)
        try:
            size = os.path.getsize(p)
            if size > 0 and not any(f["name"] == name for f in all_files):
                all_files.append({"name": name, "size": size, "path": p})
        except OSError:
            pass

    # Backward compat: also include launcher.log if it's a real file (not symlink)
    launcher_log = os.path.join(getattr(app, "app_support", ""), "launcher.log")
    if os.path.exists(launcher_log) and not os.path.islink(launcher_log):
        try:
            size = os.path.getsize(launcher_log)
            if size > 0:
                all_files.append({
                    "name": "launcher.log",
                    "size": size,
                    "path": launcher_log,
                })
        except OSError:
            pass

    # Machine-status snapshot — gzip the latest status payload (machine stats
    # + allowlisted config, assembled by write_status_to_volume) and offer it
    # alongside the logs. A dated filename means OnionHome sees a new file and
    # requests it each day (the server stores any safe-named file as-is).
    status_payload = getattr(app, "_last_status_payload", None)
    if status_payload:
        try:
            from datetime import datetime as _dt, timezone as _tz
            status_name = f"status-{_dt.now(_tz.utc).strftime('%Y-%m-%d')}.json.gz"
            status_path = os.path.join(logs_dir, status_name)
            with gzip.open(status_path, "wb") as _gz:
                _gz.write(json.dumps(status_payload, indent=2).encode("utf-8"))
            ssize = os.path.getsize(status_path)
            if ssize > 0 and not any(f["name"] == status_name for f in all_files):
                all_files.append({"name": status_name, "size": ssize, "path": status_path})
        except Exception as _e:
            app.log(f"Analytics: status snapshot write failed: {_e}")

    if not all_files:
        return {"status": "no_files"}

    # Gzip any uncompressed .log entry in memory so the wire transfer is
    # ~10–20x smaller. Rolled .log.gz files on disk pass through as-is.
    # Active logs are still being written, so we compress a point-in-time
    # snapshot — subsequent writes will ship in the next day's cycle when
    # the file has either rolled (on-disk .log.gz) or grown enough that
    # OnionHome re-requests the larger snapshot under the same name.
    compressed_bytes = {}  # advertised name -> gzipped bytes
    transformed = []
    for entry in all_files:
        name = entry["name"]
        path = entry["path"]
        if name.endswith(".gz"):
            transformed.append(entry)
            continue
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        if not raw:
            continue
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6) as gz:
            gz.write(raw)
        data = buf.getvalue()
        gz_name = name + ".gz"
        compressed_bytes[gz_name] = data
        transformed.append({"name": gz_name, "size": len(data), "path": path})
    all_files = transformed

    all_files.sort(key=lambda f: f["name"], reverse=True)  # newest first by date in name

    if not all_files:
        return {"status": "no_files"}

    content_addr = getattr(app, "onion_address", None)
    hc_addr = getattr(app, "healthcheck_address", None)
    if not content_addr or not hc_addr:
        return {"status": "no_onion"}

    # Read OnionHome address from config
    onionhome_addr = app.read_config_value(
        "ONIONHOME_ADDRESS",
        "op2homeiwjb4fdqnfkj5kbokvcee45zpk2pwgvpz5rrkanp5qqwxzbyd.onion",
    )
    if not onionhome_addr:
        return {"status": "no_onion"}

    # Sign the manifest
    try:
        from onionpress import key_manager
        from onionpress import onion_auth

        secret_key_bytes, public_key_raw = key_manager.extract_keys()
        timestamp = onion_auth.make_timestamp()
        signature = onion_auth.sign_payload(
            secret_key_bytes, public_key_raw,
            "logs", content_addr, hc_addr, timestamp,
        )
    except Exception as e:
        app.log(f"Analytics sharing: sign error: {e}")
        return {"status": "sign_error"}

    manifest = {
        "content_address": content_addr,
        "healthcheck_address": hc_addr,
        "version": getattr(app, "version", "unknown"),
        "tor_impl": "tor",  # the only implementation since 2026-09-24; kept for the OnionHome schema
        "os_version": platform.mac_ver()[0] or "unknown",
        "files": [{"name": f["name"], "size": f["size"]} for f in all_files],
        "timestamp": timestamp,
        "signature": signature,
    }

    # Build a name→path map for quick lookup
    file_map = {f["name"]: f["path"] for f in all_files}

    api_port = 8083
    base_url = f"http://{onionhome_addr}:{api_port}"

    # Step 1: POST manifest
    wanted = _post_json(app, f"{base_url}/logs/manifest", manifest)
    if wanted is None:
        return {"status": "manifest_failed"}
    wanted_names = wanted.get("wanted", [])
    if not wanted_names:
        return {"status": "none_wanted"}

    app.log(f"Analytics sharing: OnionHome wants {len(wanted_names)} file(s)")

    # Step 2: Upload each wanted file
    uploaded_count = 0
    from onionpress import onion_auth as _oa

    for name in wanted_names:
        if not _LOG_NAME_RE.match(name):
            continue
        path = file_map.get(name)
        if not path or not os.path.exists(path):
            continue

        cached = compressed_bytes.get(name)
        if cached is not None:
            content_b64 = base64.b64encode(cached).decode("ascii")
        else:
            try:
                with open(path, "rb") as f:
                    content_b64 = base64.b64encode(f.read()).decode("ascii")
            except OSError:
                continue

        ts2 = _oa.make_timestamp()
        sig2 = _oa.sign_payload(
            secret_key_bytes, public_key_raw,
            "logs", content_addr, hc_addr, ts2,
        )
        upload_payload = {
            "content_address": content_addr,
            "healthcheck_address": hc_addr,
            "file_name": name,
            "file_content": content_b64,
            "timestamp": ts2,
            "signature": sig2,
        }
        resp = _post_json(app, f"{base_url}/logs/upload", upload_payload)
        if not resp or not resp.get("stored"):
            # Retry once after 10s
            time.sleep(10)
            resp = _post_json(app, f"{base_url}/logs/upload", upload_payload)
        if resp and resp.get("stored"):
            app.log(f"Analytics sharing: uploaded {name}")
            uploaded_count += 1
            # Advance the per-type shipped watermark so RotatingLog's
            # total-size enforcement won't prematurely rotate this
            # roll (or any earlier one) out of existence when the
            # next outage extends beyond retention.
            #
            # Skip watermark updates for live-compressed uploads (those
            # whose .log.gz name was synthesized in memory from an
            # actively-written .log file). The disk name is still .log,
            # which sorts strictly below the advertised .log.gz — marking
            # shipped here would make retention treat the still-growing
            # active file as deletable, and would also mask the fact that
            # the eventual rolled .log.gz on disk holds more bytes than
            # what we shipped. The next cycle, once the file has rolled,
            # picks it up off disk and advances the watermark properly.
            if name in compressed_bytes:
                continue
            try:
                from onionpress import log_rotation
                logs_dir = os.path.join(app.app_support, "logs")
                log_type = log_rotation.extract_log_type(name)
                if log_type is not None:
                    log_rotation.mark_shipped(logs_dir, log_type, name)
            except Exception:
                pass

    return {"status": "ok", "wanted": len(wanted_names), "uploaded": uploaded_count}


def _post_json(app, url, payload_dict):
    """POST JSON to *url* via docker exec curl through Tor SOCKS.

    Uses stdin piping to avoid ARG_MAX limits on large payloads.
    Returns parsed JSON response or None on failure.
    """
    # Import docker helpers from onionheaven (same pattern)
    from onionpress import onionheaven

    docker_bin = onionheaven._docker_bin(app)
    docker_env = onionheaven._docker_env(app)

    payload_json = json.dumps(payload_dict)

    cmd = [
        docker_bin, "exec", "-i", "onionpress-tor",
        "curl", "-s", "-X", "POST",
        "--socks5-hostname", "127.0.0.1:9050",
        "-H", "Content-Type: application/json",
        "-d", "@-",  # read payload from stdin
        "--max-time", "120",
        url,
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            env=docker_env,
        )
        stdout, stderr = proc.communicate(input=payload_json, timeout=150)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        app.log("Analytics sharing: curl timed out")
        return None
    except Exception as e:
        app.log(f"Analytics sharing: curl error: {e}")
        return None

    if proc.returncode != 0:
        app.log(f"Analytics sharing: curl failed rc={proc.returncode}")
        return None

    try:
        return json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        app.log(f"Analytics sharing: bad JSON response: {stdout[:200]}")
        return None

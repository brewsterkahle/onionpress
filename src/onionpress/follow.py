"""Follow-list management for OnionPress static-site installs.

Read/write helpers for the follows.json store that app/Resources/docker/tor/
follow-fetch.py (running inside the tor container) periodically fetches and
regenerates a "follows" page from. Imported by cli.py's `follow` subcommands.

The store lives inside the tor container at /var/lib/onionpress/follow/
follows.json — a Docker named volume (onionpress-data), not a host path —
because that's the same volume follow-fetch.py already writes to, and
adding it there means no new bind-mount is needed for this feature. So
every read/write here goes through `docker exec`, mirroring how key_manager.py
and backup.py already operate on other container-internal state.
"""

import json
import re
import subprocess
import urllib.parse
from datetime import datetime, timezone

FOLLOWS_JSON = "/var/lib/onionpress/follow/follows.json"
FOLLOW_DIR = "/var/lib/onionpress/follow"
CONTAINER = "onionpress-tor"


def _read_follows(docker_bin="docker"):
    result = subprocess.run(
        [docker_bin, "exec", CONTAINER, "cat", FOLLOWS_JSON],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode == 0:
        try:
            data = json.loads(result.stdout)
            if isinstance(data, dict) and isinstance(data.get("follows"), list):
                return data
        except ValueError:
            pass
    return {"schema": 1, "follows": []}


def _write_follows(data, docker_bin="docker"):
    """Write follows.json atomically: a plain `cat > follows.json` leaves a
    window where follow-fetch.py's own read (running concurrently inside
    the same container, mid-fetch-cycle) could see a truncated/partial
    file. Write to a tmp file in the same directory and rename over the
    target — `mv` within one filesystem is atomic, so any concurrent
    reader always sees either the old or the new complete content, never
    a partial write.
    """
    subprocess.run(
        [docker_bin, "exec", CONTAINER, "mkdir", "-p", FOLLOW_DIR],
        capture_output=True, timeout=10,
    )
    payload = json.dumps(data, indent=2)
    tmp_path = f"{FOLLOWS_JSON}.tmp"
    result = subprocess.run(
        [docker_bin, "exec", "-i", CONTAINER, "sh", "-c",
         f"cat > {tmp_path} && mv {tmp_path} {FOLLOWS_JSON}"],
        input=payload, capture_output=True, text=True, timeout=10,
    )
    return result.returncode == 0


def _slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "follow"


def _unique_key(base, existing_keys):
    if base not in existing_keys:
        return base
    n = 2
    while f"{base}-{n}" in existing_keys:
        n += 1
    return f"{base}-{n}"


def list_follows(docker_bin="docker"):
    """Return the list of follow entries (each a dict)."""
    return _read_follows(docker_bin=docker_bin)["follows"]


def add_follow(feed_url, display_name=None, docker_bin="docker"):
    """Add a new follow. Returns (ok, message_or_key)."""
    parsed = urllib.parse.urlparse(feed_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False, f"Not a valid feed URL: {feed_url!r}"

    data = _read_follows(docker_bin=docker_bin)
    if any(f["feed_url"] == feed_url for f in data["follows"]):
        return False, "Already following that feed."

    name = display_name or parsed.netloc
    existing_keys = {f["key"] for f in data["follows"]}
    key = _unique_key(_slugify(name), existing_keys)

    data["follows"].append({
        "key": key,
        "feed_url": feed_url,
        "display_name": name,
        "added_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_fetch_at": None,
        "last_fetch_ok": None,
        "last_error": None,
        "items": [],
    })
    if not _write_follows(data, docker_bin=docker_bin):
        return False, "Could not save — is OnionPress running?"
    return True, key


def remove_follow(key, docker_bin="docker"):
    """Remove a follow by key. Returns (ok, message)."""
    data = _read_follows(docker_bin=docker_bin)
    before = len(data["follows"])
    data["follows"] = [f for f in data["follows"] if f["key"] != key]
    if len(data["follows"]) == before:
        return False, f"No follow with key {key!r}"
    if not _write_follows(data, docker_bin=docker_bin):
        return False, "Could not save — is OnionPress running?"
    return True, f"Removed {key!r}"

#!/usr/bin/env python3
"""
OnionPress Follow — static-site edition.

Content-agnostic re-implementation of the "follow" feature that, for
WordPress installs, lives entirely inside app/Resources/plugins/onionpress-blogroll.php
(storage in WP options, fetch via WP's fetch_feed()/SimplePie, rendered by a
WP theme template). Static installs have none of that, so this is a from-
scratch daemon: periodically fetches each followed site's RSS/Atom feed over
Tor and writes both the raw state (follows.json, for `onionpress follow
list`/management) and a generated HTML page visitors can see.

Runs as a long-lived process inside the tor container (started by
entrypoint.sh when ONIONPRESS_SITE_TYPE=static), separate from
wayback-static.py — different timer (hourly, vs. Wayback's continuous
tight loop), different failure mode (a slow/dead remote feed shouldn't
starve Wayback submissions), so a stuck fetch here can't block that.

The follows list itself is managed by `onionpress follow add/remove/list`
(src/onionpress/follow.py, host-side) via `docker exec` into this same
container — see FOLLOWS_JSON below for why (it lives in a Docker named
volume, not a host-visible path).
"""

import json
import os
import subprocess
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html import escape

FETCH_INTERVAL_SEC = 3600
ITEMS_PER_FEED = 10
FETCH_TIMEOUT_SEC = 30
SOCKS_PROXY = "onionheaven:9050"  # see wayback-static.py for the rationale

FOLLOW_DIR = "/var/lib/onionpress/follow"
FOLLOWS_JSON = os.path.join(FOLLOW_DIR, "follows.json")
SITE_FOLLOWS_DIR = "/var/lib/onionpress-site/follows"

_ATOM_NS = "{http://www.w3.org/2005/Atom}"


def log(msg):
    print(f"[follow-fetch] {msg}", file=sys.stderr, flush=True)


# ─────────────────────────────── state store ──────────────────────────────

def read_follows():
    try:
        with open(FOLLOWS_JSON) as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("follows"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"schema": 1, "follows": []}


def write_follows(data):
    os.makedirs(FOLLOW_DIR, exist_ok=True)
    tmp = FOLLOWS_JSON + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, FOLLOWS_JSON)


# ─────────────────────────────── feed fetch ────────────────────────────────

def _fetch_via_tor(url, timeout=FETCH_TIMEOUT_SEC):
    try:
        result = subprocess.run(
            ["curl", "-sL", "--max-time", str(timeout),
             "--socks5-hostname", SOCKS_PROXY,
             "-H", "User-Agent: OnionPress-Follow/1",
             url],
            capture_output=True, text=True, timeout=timeout + 10,
        )
    except subprocess.TimeoutExpired:
        return None, "timeout"
    if result.returncode != 0 or not result.stdout:
        return None, f"fetch failed (curl exit {result.returncode})"
    return result.stdout, None


def _text(el):
    return (el.text or "").strip() if el is not None else ""


def parse_feed(body):
    """Parse RSS 2.0 or Atom XML into a list of {title, url, published_at}
    dicts, most recent first, capped at ITEMS_PER_FEED. Returns [] on any
    parse failure — a malformed feed just yields no items, not a crash."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []

    items = []
    if root.tag == "rss" or root.find("channel") is not None:
        for item in root.findall(".//item")[:ITEMS_PER_FEED]:
            items.append({
                "title": _text(item.find("title")) or "(untitled)",
                "url": _text(item.find("link")),
                "published_at": _text(item.find("pubDate")),
            })
    elif root.tag == f"{_ATOM_NS}feed" or root.tag == "feed":
        ns = _ATOM_NS if root.tag.startswith("{") else ""
        for entry in root.findall(f".//{ns}entry")[:ITEMS_PER_FEED]:
            link_el = entry.find(f"{ns}link")
            link = link_el.get("href", "") if link_el is not None else ""
            published = (_text(entry.find(f"{ns}published"))
                         or _text(entry.find(f"{ns}updated")))
            items.append({
                "title": _text(entry.find(f"{ns}title")) or "(untitled)",
                "url": link,
                "published_at": published,
            })
    return items


def fetch_one(entry):
    """Fetch + parse a single follow entry. Returns an updated copy of
    entry (never raises — network/parse failures are recorded as
    last_error, not exceptions, so one bad feed can't kill the cycle)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    updated = dict(entry)
    body, err = _fetch_via_tor(entry["feed_url"])
    updated["last_fetch_at"] = now
    if err:
        updated["last_fetch_ok"] = False
        updated["last_error"] = err
        log(f"fetch failed for {entry.get('display_name', entry['feed_url'])}: {err}")
        return updated
    items = parse_feed(body)
    updated["last_fetch_ok"] = True
    updated["last_error"] = None
    updated["items"] = items
    log(f"fetched {entry.get('display_name', entry['feed_url'])}: {len(items)} item(s)")
    return updated


def fetch_cycle():
    data = read_follows()
    if not data["follows"]:
        return data
    data["follows"] = [fetch_one(entry) for entry in data["follows"]]
    write_follows(data)
    return data


# ─────────────────────────── generated follows page ────────────────────────

def generate_follows_page(data):
    """Write a plain HTML page listing followed sites + their recent
    items — the static-mode equivalent of WordPress's page-follow.php
    theme template, since there's no PHP here to render it dynamically."""
    follows = data.get("follows", [])
    parts = [
        "<!doctype html>",
        '<html><head><meta charset="utf-8"><title>Follows</title></head><body>',
        "<h1>Sites I follow</h1>",
    ]
    if not follows:
        parts.append("<p>Not following anyone yet.</p>")
    for entry in follows:
        name = escape(entry.get("display_name") or entry.get("feed_url", ""))
        parts.append(f"<h2>{name}</h2>")
        if not entry.get("last_fetch_ok", True) and entry.get("last_error"):
            parts.append(f'<p><em>Last fetch failed: {escape(entry["last_error"])}</em></p>')
        items = entry.get("items") or []
        if items:
            parts.append("<ul>")
            for item in items:
                title = escape(item.get("title", ""))
                url = escape(item.get("url", ""), quote=True)
                date = escape(item.get("published_at", ""))
                if url:
                    parts.append(f'<li><a href="{url}">{title}</a> <small>{date}</small></li>')
                else:
                    parts.append(f"<li>{title} <small>{date}</small></li>")
            parts.append("</ul>")
    parts.append("</body></html>")

    os.makedirs(SITE_FOLLOWS_DIR, exist_ok=True)
    tmp = os.path.join(SITE_FOLLOWS_DIR, "index.html.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))
    os.replace(tmp, os.path.join(SITE_FOLLOWS_DIR, "index.html"))


def main():
    log("starting")
    while True:
        try:
            data = fetch_cycle()
            generate_follows_page(data)
        except Exception as e:
            log(f"cycle error: {e}")
        time.sleep(FETCH_INTERVAL_SEC)


if __name__ == "__main__":
    main()

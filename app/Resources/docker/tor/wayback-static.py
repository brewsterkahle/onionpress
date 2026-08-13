#!/usr/bin/env python3
"""
OnionPress Wayback Archive — static-site edition.

Content-agnostic counterpart to app/Resources/plugins/onionpress-wayback-archive.php
(the WordPress plugin). That plugin archives WP posts/home/feed via wp-cron,
using WP postmeta as its state store. This script does the same job for a
static site with no PHP/database at all: it discovers pages by parsing the
site's own sitemap.xml (falling back to a same-origin crawl), submits them to
the Internet Archive's Save Page Now (SPN2) API, and tracks state in a local
SQLite database.

Runs as a long-lived process inside the tor container (started by
entrypoint.sh when ONIONPRESS_SITE_TYPE=static) — there's no wp-cron
equivalent needed since this process's own loop is the scheduler.

Tunables, API shapes, and the two-phase poll-then-submit sweep algorithm are
ported directly from the PHP plugin, which get these values from extensive
production tuning (see that file's header comments for the "why"). Notably:
outbound requests route through onionheaven's SOCKS (not this container's own
127.0.0.1:9050) so SPN traffic bursts don't compete with this container's own
job of serving inbound onion traffic and the heartbeat.
"""

import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser

# ─────────────────────────── tunables (ported from the PHP plugin) ────────

SUBMIT_BATCH_MAX = 40       # max new submissions per sweep tick
CONCURRENT_MAX = 5          # max concurrent in-flight curl calls
STATUS_BATCH_MAX = 20       # max job_ids per /save/status POST
STALE_PENDING_SEC = 300     # clear job_id if pending this long — SPN lost it
SWEEP_BUDGET_SEC = 45       # wall-clock cap per tick
YOUNG_JOB_SKIP_SEC = 15     # don't poll a job younger than this

LOOP_IDLE_SLEEP = 30        # between iterations when work was done
LOOP_NOWORK_SLEEP = 90      # when an iteration found nothing to submit

BACKOFF_NO_SLOTS = 20       # SPN says available=0
BACKOFF_UNREACHABLE = 120   # our own onion not responding
BACKOFF_SPN_DOWN = 60       # /save/status/user call itself failed

CRAWL_MAX_PAGES = int(os.environ.get("ONIONPRESS_WAYBACK_CRAWL_MAX_PAGES", "500"))
CRAWL_MAX_DEPTH = int(os.environ.get("ONIONPRESS_WAYBACK_CRAWL_MAX_DEPTH", "5"))
DISCOVERY_INTERVAL_SEC = 300  # re-discover pages this often

SPN_HOST = "web.archivep75mbjunhxc6x4j5mwjmomyxb573v42baldlqu56ruil2oiad.onion"
ARCHIVE_LOGIN_HOST = "archivep75mbjunhxc6x4j5mwjmomyxb573v42baldlqu56ruil2oiad.onion"
# Shared OnionPress archive.org service account — same credentials already
# used by src/onionpress/multisite.py's ensure_archive_s3_keys() for the
# WordPress path. Not a per-user secret; every OnionPress install uses this
# account to obtain SPN2 submission credentials.
ARCHIVE_LOGIN_EMAIL = "onionpress@internetarchive.eu"
ARCHIVE_LOGIN_PASS = "aat:aep7"

SOCKS_PROXY = "onionheaven:9050"  # deliberately NOT this container's own 9050

BACKEND_HOST = os.environ.get("ONIONPRESS_BACKEND_HOST", "site")
STATE_DIR = "/var/lib/onionpress/wayback-static"
DB_PATH = os.path.join(STATE_DIR, "state.db")
S3_KEYS_PATH = os.path.join(STATE_DIR, "archive-s3-keys.json")
BACKOFF_PATH = os.path.join(STATE_DIR, "backoff-until")
HOSTNAME_PATH = f"/var/lib/tor/hidden_service/{BACKEND_HOST}/hostname"


def log(msg):
    print(f"[wayback-static] {msg}", file=sys.stderr, flush=True)


# ─────────────────────────────── state store ──────────────────────────────

def db_connect():
    os.makedirs(STATE_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pages (
            url TEXT PRIMARY KEY,
            archived_at INTEGER,
            snapshot_ts TEXT,
            job_id TEXT,
            submitted_at INTEGER,
            last_error_ext TEXT,
            last_error_at INTEGER,
            last_seen_in_discovery_at INTEGER
        )
    """)
    conn.commit()
    return conn


def upsert_seen(conn, urls, now):
    """Record freshly discovered URLs, inserting new ones and touching
    last_seen_in_discovery_at for existing ones (does not disturb
    archived_at/job_id state for already-known URLs)."""
    with conn:
        conn.executemany(
            "INSERT INTO pages (url, last_seen_in_discovery_at) VALUES (?, ?) "
            "ON CONFLICT(url) DO UPDATE SET last_seen_in_discovery_at = excluded.last_seen_in_discovery_at",
            [(u, now) for u in urls],
        )


def in_flight_jobs(conn):
    """Return {job_id: (url, submitted_at)} for pages with an outstanding job_id."""
    rows = conn.execute(
        "SELECT url, job_id, submitted_at FROM pages "
        "WHERE job_id IS NOT NULL AND job_id != '' AND archived_at IS NULL"
    ).fetchall()
    return {jid: (url, submitted_at or 0) for url, jid, submitted_at in rows}


def urls_needing_submit(conn, budget):
    rows = conn.execute(
        "SELECT url FROM pages WHERE archived_at IS NULL "
        "AND (job_id IS NULL OR job_id = '') "
        "ORDER BY last_seen_in_discovery_at DESC LIMIT ?",
        (budget,),
    ).fetchall()
    return [r[0] for r in rows]


def mark_submitted(conn, url, job_id, now):
    with conn:
        conn.execute(
            "UPDATE pages SET job_id = ?, submitted_at = ? WHERE url = ?",
            (job_id, now, url),
        )


def mark_success(conn, url, snapshot_ts, now):
    with conn:
        conn.execute(
            "UPDATE pages SET archived_at = ?, snapshot_ts = ?, job_id = '', "
            "submitted_at = NULL, last_error_ext = '', last_error_at = NULL WHERE url = ?",
            (now, snapshot_ts, url),
        )


def mark_error(conn, url, ext, now):
    with conn:
        conn.execute(
            "UPDATE pages SET job_id = '', submitted_at = NULL, "
            "last_error_ext = ?, last_error_at = ? WHERE url = ?",
            (ext, now, url),
        )


def clear_job(conn, url):
    with conn:
        conn.execute(
            "UPDATE pages SET job_id = '', submitted_at = NULL WHERE url = ?",
            (url,),
        )


# ───────────────────────────── page discovery ─────────────────────────────

class _LinkExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        for name, value in attrs:
            if name == "href" and value:
                self.links.append(value)


def _fetch_local(path, timeout=10):
    """GET a path from our own content backend over the docker network
    (same-host, no Tor needed for this leg)."""
    try:
        result = subprocess.run(
            ["curl", "-s", "--max-time", str(timeout),
             f"http://{BACKEND_HOST}:80{path}"],
            capture_output=True, text=True, timeout=timeout + 5,
        )
        if result.returncode == 0:
            return result.stdout
    except subprocess.TimeoutExpired:
        pass
    return None


def discover_via_sitemap():
    body = _fetch_local("/sitemap.xml")
    if not body:
        return None
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return None
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    urls = []
    for loc in root.findall(".//sm:url/sm:loc", ns) or root.findall(".//loc"):
        text = (loc.text or "").strip()
        if not text:
            continue
        path = urllib.parse.urlparse(text).path or "/"
        urls.append(path)
    return urls or None


def discover_via_crawl():
    """Same-origin breadth-first crawl starting at '/', capped by
    CRAWL_MAX_PAGES / CRAWL_MAX_DEPTH. No sitemap.xml — common for a
    hand-written site or an SSG that doesn't emit one."""
    seen = set()
    queue = [("/", 0)]
    pages = []
    while queue and len(pages) < CRAWL_MAX_PAGES:
        path, depth = queue.pop(0)
        if path in seen:
            continue
        seen.add(path)
        body = _fetch_local(path)
        if body is None:
            continue
        pages.append(path)
        if depth >= CRAWL_MAX_DEPTH:
            continue
        parser = _LinkExtractor()
        try:
            parser.feed(body)
        except Exception:
            continue
        for href in parser.links:
            if href.startswith(("http://", "https://", "mailto:", "javascript:", "#")):
                # Only same-origin absolute links are worth following;
                # cheaply detect same-origin by checking the path-only form.
                parsed = urllib.parse.urlparse(href)
                if parsed.netloc:
                    continue
                href = parsed.path or "/"
            href = href.split("#")[0].split("?")[0]
            if href and href.startswith("/") and href not in seen:
                queue.append((href, depth + 1))
    return pages


def discover_pages():
    pages = discover_via_sitemap()
    if pages:
        log(f"discovered {len(pages)} pages via sitemap.xml")
        return pages
    pages = discover_via_crawl()
    log(f"discovered {len(pages)} pages via crawl (no sitemap.xml)")
    return pages


# ─────────────────────────────── HTTP via Tor ──────────────────────────────

def _curl(args, timeout):
    try:
        result = subprocess.run(
            ["curl", "-s", "--max-time", str(timeout),
             "--socks5-hostname", SOCKS_PROXY,
             "-w", "\n---CODE---%{http_code}"] + args,
            capture_output=True, text=True, timeout=timeout + 10,
        )
    except subprocess.TimeoutExpired:
        return 0, ""
    if result.returncode != 0:
        return 0, ""
    body, _, code = result.stdout.rpartition("---CODE---")
    try:
        return int(code.strip()), body
    except ValueError:
        return 0, ""


def _curl_many(jobs, timeout):
    """Run [(key, args)] concurrently (capped at CONCURRENT_MAX), return
    {key: (code, body)}."""
    results = {}
    with ThreadPoolExecutor(max_workers=CONCURRENT_MAX) as pool:
        futures = {pool.submit(_curl, args, timeout): key for key, args in jobs}
        for future in futures:
            key = futures[future]
            results[key] = future.result()
    return results


def read_onion_address():
    try:
        with open(HOSTNAME_PATH) as f:
            return f.read().strip()
    except OSError:
        return ""


def self_reachable(onion):
    code, _ = _curl(["-I", "-o", "/dev/null", f"http://{onion}/"], timeout=20)
    return code in (200, 301)


def get_auth_header():
    """Return the 'LOW access:secret' auth header, fetching + caching
    archive.org S3 keys on first use (or if the cache is missing)."""
    try:
        with open(S3_KEYS_PATH) as f:
            keys = json.load(f)
        if keys.get("access") and keys.get("secret"):
            return f"LOW {keys['access']}:{keys['secret']}"
    except (OSError, ValueError):
        pass

    log("fetching archive.org S3 keys (first run)...")
    code, body = _curl(
        ["-k", "-X", "POST",
         "-d", f"email={ARCHIVE_LOGIN_EMAIL}&password={ARCHIVE_LOGIN_PASS}",
         f"https://{ARCHIVE_LOGIN_HOST}/services/xauthn/?op=login"],
        timeout=60,
    )
    if code != 200 or not body:
        log(f"archive.org login failed (code={code})")
        return ""
    try:
        data = json.loads(body)
    except ValueError:
        log("archive.org login returned non-JSON")
        return ""
    s3 = (data.get("values") or {}).get("s3") or {}
    access, secret = s3.get("access", ""), s3.get("secret", "")
    if not access or not secret:
        log("archive.org login succeeded but S3 keys were empty")
        return ""
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(S3_KEYS_PATH, "w") as f:
        json.dump({"access": access, "secret": secret}, f)
    log("archive.org S3 keys cached")
    return f"LOW {access}:{secret}"


def user_status(auth):
    code, body = _curl(
        ["-H", "Accept: application/json", "-H", f"Authorization: {auth}",
         f"https://{SPN_HOST}/save/status/user?t={int(time.time())}"],
        timeout=20,
    )
    if code != 200 or not body:
        return None
    try:
        data = json.loads(body)
        return data if isinstance(data, dict) else None
    except ValueError:
        return None


def submit_parallel(urls, auth):
    """urls: {key: full_url}. Returns {key: job_id | 'RATE_LIMITED' | ''}."""
    jobs = []
    for key, url in urls.items():
        args = [
            "-X", "POST",
            "-d", urllib.parse.urlencode({
                "url": url, "skip_first_archive": 1, "js_behavior_timeout": 0,
            }),
            "-H", "Accept: application/json", "-H", f"Authorization: {auth}",
            f"https://{SPN_HOST}/save",
        ]
        jobs.append((key, args))
    raw = _curl_many(jobs, timeout=40)
    results = {}
    for key, (code, body) in raw.items():
        if code == 429:
            results[key] = "RATE_LIMITED"
            continue
        if code < 200 or code >= 400 or not body:
            results[key] = ""
            continue
        try:
            data = json.loads(body)
            results[key] = str(data.get("job_id", "")) if isinstance(data, dict) else ""
        except ValueError:
            results[key] = ""
    return results


def poll_parallel(job_ids, auth):
    """Returns a flat list of SPN status dicts."""
    if not job_ids:
        return []
    headers = ["-H", "Accept: application/json"]
    if auth:
        headers += ["-H", f"Authorization: {auth}"]
    chunks = [job_ids[i:i + STATUS_BATCH_MAX] for i in range(0, len(job_ids), STATUS_BATCH_MAX)]
    jobs = [
        (i, ["-X", "POST", "-d", urllib.parse.urlencode({"job_ids": ",".join(chunk)})]
             + headers + [f"https://{SPN_HOST}/save/status"])
        for i, chunk in enumerate(chunks)
    ]
    raw = _curl_many(jobs, timeout=40)
    all_results = []
    for _, (code, body) in raw.items():
        if code != 200 or not body:
            continue
        try:
            data = json.loads(body)
        except ValueError:
            continue
        if isinstance(data, list):
            all_results.extend(item for item in data if isinstance(item, dict))
    return all_results


def cdx_lookup_parallel(urls):
    """urls: {key: full_url}. Returns {key: timestamp | ''}."""
    if not urls:
        return {}
    jobs = []
    for key, url in urls.items():
        no_scheme = url.split("://", 1)[-1]
        endpoint = (f"https://{SPN_HOST}/cdx/search/cdx?"
                    f"url={urllib.parse.quote(no_scheme)}&output=json&limit=-1")
        jobs.append((key, ["-H", "Accept: application/json", endpoint]))
    raw = _curl_many(jobs, timeout=25)
    results = {key: "" for key in urls}
    for key, (code, body) in raw.items():
        if code != 200 or not body:
            continue
        try:
            data = json.loads(body)
        except ValueError:
            continue
        if isinstance(data, list) and len(data) >= 2:
            last = data[-1]
            if isinstance(last, list) and len(last) > 1 and last[1]:
                results[key] = str(last[1])
    return results


# ────────────────────────────── backoff gate ───────────────────────────────

def backoff_until():
    try:
        with open(BACKOFF_PATH) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def set_backoff(seconds):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(BACKOFF_PATH, "w") as f:
        f.write(str(int(time.time()) + seconds))


# ─────────────────────────────── sweep logic ───────────────────────────────

def sweep_iteration(conn):
    now = int(time.time())

    if backoff_until() > now:
        return False  # did no work

    onion = read_onion_address()
    if not onion:
        log("sweep skipped: onion address not ready")
        return False

    auth = get_auth_header()
    if not auth:
        set_backoff(BACKOFF_SPN_DOWN)
        return False

    if not self_reachable(onion):
        log("sweep paused: self not reachable through Tor yet")
        set_backoff(BACKOFF_UNREACHABLE)
        return False

    user = user_status(auth)
    available = SUBMIT_BATCH_MAX if user is None else int(user.get("available", 0))
    if user is not None and available <= 0:
        log(f"sweep paused: available=0 processing={user.get('processing', '?')}")
        set_backoff(BACKOFF_NO_SLOTS)
        return False

    did_work = False

    # ---- Step A: poll outstanding jobs ----
    in_flight = in_flight_jobs(conn)
    ripe = [jid for jid, (_, submitted_at) in in_flight.items()
            if submitted_at and (now - submitted_at) >= YOUNG_JOB_SKIP_SEC]
    results = poll_parallel(ripe, auth)

    cdx_check = {}  # job_id -> url
    cdx_ext = {}
    for res in results:
        jid = str(res.get("job_id", ""))
        if jid not in in_flight:
            continue
        url, submitted_at = in_flight[jid]
        status = res.get("status", "")
        if status == "success":
            mark_success(conn, url, str(res.get("timestamp", "")), now)
            did_work = True
            log(f"archived {url} ts={res.get('timestamp', '')} "
                f"dur={res.get('duration_sec', '')}")
        elif status == "error":
            cdx_check[jid] = url
            cdx_ext[jid] = str(res.get("status_ext", "error"))
        else:
            age = now - submitted_at if submitted_at else None
            if age is None or age > STALE_PENDING_SEC:
                clear_job(conn, url)
                log(f"stale-pending {url}, clearing for resubmit")

    if cdx_check:
        # Cap the rescue burst; anything beyond the first 5 is recorded
        # as an error now and gets another shot at CDX rescue next tick.
        items = list(cdx_check.items())
        do_now, defer = dict(items[:5]), dict(items[5:])
        for jid, url in defer.items():
            mark_error(conn, url, cdx_ext[jid], now)
        if do_now:
            cdx = cdx_lookup_parallel({jid: url for jid, url in do_now.items()})
            for jid, url in do_now.items():
                ts = cdx.get(jid, "")
                if ts:
                    mark_success(conn, url, ts, now)
                    log(f"CDX rescued {url} ts={ts} (SPN said {cdx_ext[jid]})")
                else:
                    mark_error(conn, url, cdx_ext[jid], now)
        did_work = True

    # ---- Step B: submit fresh work up to available slots ----
    budget = max(0, min(SUBMIT_BATCH_MAX, available))
    if budget > 0:
        candidates = urls_needing_submit(conn, budget)
        if candidates:
            to_submit = {path: f"http://{onion}{path}" for path in candidates}
            submitted = submit_parallel(to_submit, auth)
            rate_limited = False
            for path, job_id in submitted.items():
                if job_id == "RATE_LIMITED":
                    rate_limited = True
                    continue
                if job_id:
                    mark_submitted(conn, path, job_id, now)
                    did_work = True
            if rate_limited:
                set_backoff(BACKOFF_NO_SLOTS)

    return did_work


def main():
    conn = db_connect()
    last_discovery = 0.0
    log(f"starting (backend={BACKEND_HOST})")
    while True:
        now = time.monotonic()
        if now - last_discovery >= DISCOVERY_INTERVAL_SEC:
            try:
                pages = discover_pages()
                upsert_seen(conn, pages, int(time.time()))
            except Exception as e:
                log(f"discovery error: {e}")
            last_discovery = now

        try:
            did_work = sweep_iteration(conn)
        except Exception as e:
            log(f"sweep error: {e}")
            did_work = False

        time.sleep(LOOP_IDLE_SLEEP if did_work else LOOP_NOWORK_SLEEP)


if __name__ == "__main__":
    main()

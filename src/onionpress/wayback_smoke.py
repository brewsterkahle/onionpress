"""End-to-end smoke test for the Wayback sweeper.

Publishes a throwaway post, then repeatedly triggers the sweep cron and
inspects the post's `_op_wayback_*` meta to watch its state transitions:
no meta → job_id set (submit) → archived_at set (success) or failed_at
set (given up).

Exit 0 = post reached archived_at within the poll window.
Exit 1 = state machine stalled, gave up, or timed out.
"""

import json
import subprocess
import time
from typing import Callable


_WORDPRESS_CONTAINER = "onionpress-wordpress"

# Max time (seconds) to wait for the sweeper to reach archived_at.
# Includes submit + SPN crawl + poll + Tor flake buffer.
_TEST_TIMEOUT_SEC = 360


def _docker_exec(args: list, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "exec", _WORDPRESS_CONTAINER] + args,
        capture_output=True, text=True, encoding='utf-8',
        errors='replace', **kwargs,
    )


def _wp(args: list, url: str = None, **kwargs) -> subprocess.CompletedProcess:
    cmd = ["wp"] + args + ["--path=/var/www/html", "--allow-root"]
    if url:
        cmd.append("--url=" + url)
    return _docker_exec(cmd, **kwargs)


def _get_post_wayback_state(post_id: str, site_url: str) -> dict:
    """Read _op_wayback_* meta for a post. Returns empty strings for unset."""
    keys = [
        "_op_wayback_archived_at",
        "_op_wayback_snapshot_ts",
        "_op_wayback_job_id",
        "_op_wayback_submitted_at",
        "_op_wayback_last_error_at",
        "_op_wayback_last_error_ext",
    ]
    state = {}
    for key in keys:
        r = _wp(["post", "meta", "get", post_id, key, "--format=json"],
                url=site_url, timeout=15)
        state[key] = r.stdout.strip() if r.returncode == 0 else ""
    return state


def _pick_test_site() -> dict:
    r = _wp(["site", "list", "--fields=blog_id,path,url", "--format=json"],
            timeout=15)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(f"wp site list failed: {r.stderr or r.stdout}")
    sites = json.loads(r.stdout)
    subsites = [s for s in sites if s.get("path") != "/"]
    return subsites[0] if subsites else sites[0]


def smoke_test_wayback(log_func: Callable[[str], None]) -> int:
    try:
        site = _pick_test_site()
    except Exception as e:
        log_func(f"FAIL: {e}")
        return 1
    site_url = site["url"].rstrip("/") + "/"
    log_func(f"Publishing test post on {site_url} (blog_id={site.get('blog_id')})")

    marker = f"onionpress-smoke-{int(time.time())}"
    r = _wp(
        ["post", "create",
         "--post_title=Wayback smoke test " + marker,
         "--post_content=Wayback archiving smoke test. Safe to delete.",
         "--post_status=publish",
         "--porcelain"],
        url=site_url, timeout=30,
    )
    if r.returncode != 0 or not r.stdout.strip().isdigit():
        log_func(f"FAIL: publish failed: {r.stderr or r.stdout}")
        return 1
    post_id = r.stdout.strip()
    log_func(f"Published post ID {post_id}")

    try:
        deadline = time.monotonic() + _TEST_TIMEOUT_SEC
        last_summary = ""
        tick = 0
        while time.monotonic() < deadline:
            tick += 1
            # Force a sweep tick instead of waiting for the 5-minute cron.
            _wp(["cron", "event", "run", "onionpress_wayback_sweep"],
                timeout=120)
            state = _get_post_wayback_state(post_id, site_url)
            summary = (f"tick={tick} "
                       f"archived_at={state['_op_wayback_archived_at'] or '-'} "
                       f"job_id={state['_op_wayback_job_id'] or '-'} "
                       f"submitted_at={state['_op_wayback_submitted_at'] or '-'} "
                       f"last_error={state['_op_wayback_last_error_ext'] or '-'}")
            if summary != last_summary:
                log_func("  " + summary)
                last_summary = summary

            if state["_op_wayback_archived_at"]:
                ts = state["_op_wayback_snapshot_ts"] or "?"
                log_func(f"PASS: post archived (snapshot_ts={ts})")
                return 0
            # The v4 sweep never gives up on a post: an SPN error is recorded
            # in _op_wayback_last_error_* and the URL is resubmitted next
            # tick, so an error here is progress to report, not a verdict.
            time.sleep(8)  # brief pause between sweep ticks

        log_func(f"FAIL: timed out after {_TEST_TIMEOUT_SEC}s. Last state: {last_summary}")
        return 1
    finally:
        cleanup = _wp(["post", "delete", post_id, "--force"],
                      url=site_url, timeout=30)
        if cleanup.returncode == 0:
            log_func(f"Deleted test post ID {post_id}")
        else:
            log_func(f"WARNING: cleanup failed: {cleanup.stderr or cleanup.stdout}")

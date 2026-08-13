"""Tests for app/Resources/docker/tor/wayback-static.py — the static-site
counterpart to the WordPress Wayback Machine plugin.

Covers page discovery (sitemap.xml + crawl fallback), the SQLite state
store, and the sweep_iteration orchestration (poll-then-submit, CDX rescue,
backoff gating) with the network layer (_curl/_curl_many) mocked out.
"""

import importlib.util
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

_TOR_DIR = os.path.join(
    os.path.dirname(__file__), "..", "app", "Resources", "docker", "tor"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "wayback_static", os.path.join(_TOR_DIR, "wayback-static.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wb = _load_module()


class TestDiscoverViaSitemap(unittest.TestCase):
    def test_parses_urlset(self):
        body = (
            '<?xml version="1.0"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            '<url><loc>http://abc.onion/</loc></url>'
            '<url><loc>http://abc.onion/about/</loc></url>'
            '</urlset>'
        )
        with mock.patch.object(wb, "_fetch_local", return_value=body):
            urls = wb.discover_via_sitemap()
        self.assertEqual(sorted(urls), ["/", "/about/"])

    def test_missing_sitemap_returns_none(self):
        with mock.patch.object(wb, "_fetch_local", return_value=None):
            self.assertIsNone(wb.discover_via_sitemap())

    def test_malformed_xml_returns_none(self):
        with mock.patch.object(wb, "_fetch_local", return_value="<not valid xml"):
            self.assertIsNone(wb.discover_via_sitemap())

    def test_empty_urlset_returns_none(self):
        body = '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></urlset>'
        with mock.patch.object(wb, "_fetch_local", return_value=body):
            self.assertIsNone(wb.discover_via_sitemap())


class TestDiscoverViaCrawl(unittest.TestCase):
    def test_follows_same_origin_links(self):
        pages = {
            "/": '<a href="/about/">About</a><a href="/blog/">Blog</a>',
            "/about/": '<a href="/">Home</a>',
            "/blog/": '<a href="https://external.example/">external</a>'
                      '<a href="mailto:a@b.com">mail</a>'
                      '<a href="/blog/post-1/">Post 1</a>',
            "/blog/post-1/": "",
        }

        def fetch(path, timeout=10):
            return pages.get(path)

        with mock.patch.object(wb, "_fetch_local", side_effect=fetch):
            found = wb.discover_via_crawl()
        self.assertEqual(set(found), set(pages.keys()))

    def test_respects_max_pages_cap(self):
        def fetch(path, timeout=10):
            n = int(path.strip("/") or 0)
            return f'<a href="/{n + 1}/">next</a>'

        with mock.patch.object(wb, "_fetch_local", side_effect=fetch), \
             mock.patch.object(wb, "CRAWL_MAX_PAGES", 5), \
             mock.patch.object(wb, "CRAWL_MAX_DEPTH", 100):
            found = wb.discover_via_crawl()
        self.assertEqual(len(found), 5)

    def test_unreachable_root_returns_empty(self):
        with mock.patch.object(wb, "_fetch_local", return_value=None):
            self.assertEqual(wb.discover_via_crawl(), [])


class _StateStoreTestCase(unittest.TestCase):
    """Base class: points wb's STATE_DIR/DB_PATH at a temp dir per test."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self._orig_state_dir = wb.STATE_DIR
        self._orig_db_path = wb.DB_PATH
        wb.STATE_DIR = self.tmpdir
        wb.DB_PATH = os.path.join(self.tmpdir, "state.db")
        self.conn = wb.db_connect()

    def _cleanup(self):
        self.conn.close()
        wb.STATE_DIR = self._orig_state_dir
        wb.DB_PATH = self._orig_db_path
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)


class TestStateStore(_StateStoreTestCase):
    def test_upsert_seen_inserts_new_urls(self):
        wb.upsert_seen(self.conn, ["/a", "/b"], now=100)
        rows = self.conn.execute("SELECT url FROM pages ORDER BY url").fetchall()
        self.assertEqual([r[0] for r in rows], ["/a", "/b"])

    def test_upsert_seen_does_not_disturb_existing_state(self):
        wb.upsert_seen(self.conn, ["/a"], now=100)
        wb.mark_submitted(self.conn, "/a", "job123", now=101)
        wb.upsert_seen(self.conn, ["/a"], now=200)
        row = self.conn.execute(
            "SELECT job_id, last_seen_in_discovery_at FROM pages WHERE url = '/a'"
        ).fetchone()
        self.assertEqual(row[0], "job123")
        self.assertEqual(row[1], 200)

    def test_urls_needing_submit_excludes_archived_and_in_flight(self):
        wb.upsert_seen(self.conn, ["/a", "/b", "/c"], now=100)
        wb.mark_success(self.conn, "/a", "20260101000000", now=101)
        wb.mark_submitted(self.conn, "/b", "job1", now=101)
        needing = wb.urls_needing_submit(self.conn, budget=10)
        self.assertEqual(needing, ["/c"])

    def test_mark_success_clears_job_fields(self):
        wb.upsert_seen(self.conn, ["/a"], now=100)
        wb.mark_submitted(self.conn, "/a", "job1", now=101)
        wb.mark_success(self.conn, "/a", "20260101000000", now=102)
        row = self.conn.execute(
            "SELECT archived_at, snapshot_ts, job_id FROM pages WHERE url = '/a'"
        ).fetchone()
        self.assertEqual(row, (102, "20260101000000", ""))

    def test_in_flight_jobs(self):
        wb.upsert_seen(self.conn, ["/a", "/b"], now=100)
        wb.mark_submitted(self.conn, "/a", "job1", now=101)
        flight = wb.in_flight_jobs(self.conn)
        self.assertEqual(flight, {"job1": ("/a", 101)})

    def test_clear_job_makes_url_eligible_again(self):
        wb.upsert_seen(self.conn, ["/a"], now=100)
        wb.mark_submitted(self.conn, "/a", "job1", now=101)
        wb.clear_job(self.conn, "/a")
        self.assertEqual(wb.urls_needing_submit(self.conn, 10), ["/a"])


class TestSweepIteration(_StateStoreTestCase):
    """sweep_iteration: the gate → poll → submit orchestration."""

    def setUp(self):
        super().setUp()
        patches = [
            mock.patch.object(wb, "backoff_until", return_value=0),
            mock.patch.object(wb, "set_backoff"),
            mock.patch.object(wb, "read_onion_address", return_value="abc.onion"),
            mock.patch.object(wb, "get_auth_header", return_value="LOW a:s"),
            mock.patch.object(wb, "self_reachable", return_value=True),
            mock.patch.object(wb, "user_status", return_value={"available": 40}),
        ]
        self.mocks = {p.attribute: p.start() for p in patches}
        for p in patches:
            self.addCleanup(p.stop)

    def test_backoff_gate_short_circuits(self):
        wb.upsert_seen(self.conn, ["/a"], now=int(time.time()))
        with mock.patch.object(wb, "backoff_until", return_value=int(time.time()) + 100):
            did_work = wb.sweep_iteration(self.conn)
        self.assertFalse(did_work)
        # Nothing submitted — url still needs submit.
        self.assertEqual(wb.urls_needing_submit(self.conn, 10), ["/a"])

    def test_no_slots_backs_off_without_submitting(self):
        wb.upsert_seen(self.conn, ["/a"], now=int(time.time()))
        with mock.patch.object(wb, "user_status", return_value={"available": 0, "processing": 5}), \
             mock.patch.object(wb, "set_backoff") as m_backoff:
            did_work = wb.sweep_iteration(self.conn)
        self.assertFalse(did_work)
        m_backoff.assert_called_once_with(wb.BACKOFF_NO_SLOTS)

    def test_unreachable_self_backs_off(self):
        with mock.patch.object(wb, "self_reachable", return_value=False), \
             mock.patch.object(wb, "set_backoff") as m_backoff:
            did_work = wb.sweep_iteration(self.conn)
        self.assertFalse(did_work)
        m_backoff.assert_called_once_with(wb.BACKOFF_UNREACHABLE)

    def test_submits_fresh_urls_up_to_budget(self):
        wb.upsert_seen(self.conn, ["/a", "/b"], now=int(time.time()))
        with mock.patch.object(wb, "submit_parallel",
                                return_value={"/a": "job-a", "/b": "job-b"}) as m_submit:
            did_work = wb.sweep_iteration(self.conn)
        self.assertTrue(did_work)
        m_submit.assert_called_once()
        submitted_urls = m_submit.call_args[0][0]
        self.assertEqual(submitted_urls, {"/a": "http://abc.onion/a", "/b": "http://abc.onion/b"})
        self.assertEqual(wb.in_flight_jobs(self.conn),
                          {"job-a": ("/a", mock.ANY), "job-b": ("/b", mock.ANY)})

    def test_poll_success_marks_archived(self):
        now = int(time.time())
        wb.upsert_seen(self.conn, ["/a"], now=now)
        wb.mark_submitted(self.conn, "/a", "job-a", now=now - 100)
        with mock.patch.object(wb, "poll_parallel", return_value=[
            {"job_id": "job-a", "status": "success", "timestamp": "20260101000000"},
        ]), mock.patch.object(wb, "submit_parallel", return_value={}):
            did_work = wb.sweep_iteration(self.conn)
        self.assertTrue(did_work)
        row = self.conn.execute(
            "SELECT archived_at, snapshot_ts FROM pages WHERE url = '/a'").fetchone()
        self.assertIsNotNone(row[0])
        self.assertEqual(row[1], "20260101000000")

    def test_poll_error_falls_back_to_cdx_rescue(self):
        now = int(time.time())
        wb.upsert_seen(self.conn, ["/a"], now=now)
        wb.mark_submitted(self.conn, "/a", "job-a", now=now - 100)
        with mock.patch.object(wb, "poll_parallel", return_value=[
            {"job_id": "job-a", "status": "error", "status_ext": "error:no-captures"},
        ]), mock.patch.object(wb, "cdx_lookup_parallel",
                               return_value={"job-a": "20260102000000"}) as m_cdx, \
             mock.patch.object(wb, "submit_parallel", return_value={}):
            did_work = wb.sweep_iteration(self.conn)
        self.assertTrue(did_work)
        m_cdx.assert_called_once()
        row = self.conn.execute(
            "SELECT archived_at, snapshot_ts FROM pages WHERE url = '/a'").fetchone()
        self.assertEqual(row[1], "20260102000000")

    def test_poll_error_with_no_cdx_hit_records_error(self):
        now = int(time.time())
        wb.upsert_seen(self.conn, ["/a"], now=now)
        wb.mark_submitted(self.conn, "/a", "job-a", now=now - 100)
        with mock.patch.object(wb, "poll_parallel", return_value=[
            {"job_id": "job-a", "status": "error", "status_ext": "error:no-captures"},
        ]), mock.patch.object(wb, "cdx_lookup_parallel", return_value={"job-a": ""}), \
             mock.patch.object(wb, "submit_parallel", return_value={}):
            wb.sweep_iteration(self.conn)
        row = self.conn.execute(
            "SELECT job_id, last_error_ext FROM pages WHERE url = '/a'").fetchone()
        self.assertEqual(row, ("", "error:no-captures"))

    def test_stale_pending_job_cleared_for_resubmit(self):
        now = int(time.time())
        wb.upsert_seen(self.conn, ["/a"], now=now)
        wb.mark_submitted(self.conn, "/a", "job-a", now=now - wb.STALE_PENDING_SEC - 10)
        with mock.patch.object(wb, "poll_parallel", return_value=[
            {"job_id": "job-a", "status": "pending"},
        ]), mock.patch.object(wb, "submit_parallel", return_value={}):
            wb.sweep_iteration(self.conn)
        row = self.conn.execute("SELECT job_id FROM pages WHERE url = '/a'").fetchone()
        self.assertEqual(row[0], "")

    def test_young_jobs_are_not_polled(self):
        now = int(time.time())
        wb.upsert_seen(self.conn, ["/a"], now=now)
        wb.mark_submitted(self.conn, "/a", "job-a", now=now)  # just submitted
        with mock.patch.object(wb, "poll_parallel") as m_poll, \
             mock.patch.object(wb, "submit_parallel", return_value={}):
            wb.sweep_iteration(self.conn)
        m_poll.assert_called_once_with([], "LOW a:s")


if __name__ == "__main__":
    unittest.main()

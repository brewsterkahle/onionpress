"""Tests for app/Resources/docker/tor/follow-fetch.py — the static-site
counterpart to the WordPress follow/blogroll plugin.

Covers RSS/Atom feed parsing, the fetch-one/fetch-cycle orchestration
(network mocked out), and generated-page rendering.
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from unittest import mock

_TOR_DIR = os.path.join(
    os.path.dirname(__file__), "..", "app", "Resources", "docker", "tor"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "follow_fetch", os.path.join(_TOR_DIR, "follow-fetch.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ff = _load_module()

_RSS_FEED = """<?xml version="1.0"?>
<rss version="2.0"><channel>
<title>Example Blog</title>
<item><title>Post One</title><link>http://abc.onion/post-1/</link><pubDate>Mon, 01 Jan 2026 00:00:00 GMT</pubDate></item>
<item><title>Post Two</title><link>http://abc.onion/post-2/</link><pubDate>Tue, 02 Jan 2026 00:00:00 GMT</pubDate></item>
</channel></rss>
"""

_ATOM_FEED = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>Example Blog</title>
<entry><title>Entry One</title><link href="http://abc.onion/entry-1/"/><updated>2026-01-01T00:00:00Z</updated></entry>
</feed>
"""


class TestParseFeed(unittest.TestCase):
    def test_parses_rss(self):
        items = ff.parse_feed(_RSS_FEED)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["title"], "Post One")
        self.assertEqual(items[0]["url"], "http://abc.onion/post-1/")
        self.assertEqual(items[0]["published_at"], "Mon, 01 Jan 2026 00:00:00 GMT")

    def test_parses_atom(self):
        items = ff.parse_feed(_ATOM_FEED)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Entry One")
        self.assertEqual(items[0]["url"], "http://abc.onion/entry-1/")
        self.assertEqual(items[0]["published_at"], "2026-01-01T00:00:00Z")

    def test_malformed_xml_returns_empty(self):
        self.assertEqual(ff.parse_feed("<not valid"), [])

    def test_caps_at_items_per_feed(self):
        items_xml = "".join(
            f"<item><title>Post {i}</title><link>http://x.onion/{i}/</link></item>"
            for i in range(30)
        )
        body = f"<rss><channel>{items_xml}</channel></rss>"
        with mock.patch.object(ff, "ITEMS_PER_FEED", 5):
            items = ff.parse_feed(body)
        self.assertEqual(len(items), 5)

    def test_missing_title_falls_back(self):
        body = "<rss><channel><item><link>http://x.onion/</link></item></channel></rss>"
        items = ff.parse_feed(body)
        self.assertEqual(items[0]["title"], "(untitled)")


class TestFetchOne(unittest.TestCase):
    def test_success_updates_entry(self):
        entry = {"key": "a", "feed_url": "http://a.onion/feed/", "display_name": "A"}
        with mock.patch.object(ff, "_fetch_via_tor", return_value=(_RSS_FEED, None)):
            updated = ff.fetch_one(entry)
        self.assertTrue(updated["last_fetch_ok"])
        self.assertIsNone(updated["last_error"])
        self.assertEqual(len(updated["items"]), 2)
        self.assertIsNotNone(updated["last_fetch_at"])

    def test_failure_records_error_and_keeps_key(self):
        entry = {"key": "a", "feed_url": "http://a.onion/feed/", "display_name": "A"}
        with mock.patch.object(ff, "_fetch_via_tor", return_value=(None, "timeout")):
            updated = ff.fetch_one(entry)
        self.assertFalse(updated["last_fetch_ok"])
        self.assertEqual(updated["last_error"], "timeout")
        self.assertEqual(updated["key"], "a")

    def test_never_raises_on_bad_feed_body(self):
        entry = {"key": "a", "feed_url": "http://a.onion/feed/", "display_name": "A"}
        with mock.patch.object(ff, "_fetch_via_tor", return_value=("<garbage", None)):
            updated = ff.fetch_one(entry)  # must not raise
        self.assertTrue(updated["last_fetch_ok"])
        self.assertEqual(updated["items"], [])


class TestFetchCycle(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self._orig_dir = ff.FOLLOW_DIR
        self._orig_json = ff.FOLLOWS_JSON
        ff.FOLLOW_DIR = self.tmpdir
        ff.FOLLOWS_JSON = os.path.join(self.tmpdir, "follows.json")

    def _cleanup(self):
        ff.FOLLOW_DIR = self._orig_dir
        ff.FOLLOWS_JSON = self._orig_json
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_empty_follows_list_is_a_noop(self):
        ff.write_follows({"schema": 1, "follows": []})
        with mock.patch.object(ff, "fetch_one") as m_fetch:
            ff.fetch_cycle()
        m_fetch.assert_not_called()

    def test_fetches_each_follow_and_persists(self):
        ff.write_follows({"schema": 1, "follows": [
            {"key": "a", "feed_url": "http://a.onion/feed/", "display_name": "A"},
            {"key": "b", "feed_url": "http://b.onion/feed/", "display_name": "B"},
        ]})
        with mock.patch.object(ff, "_fetch_via_tor", return_value=(_RSS_FEED, None)):
            data = ff.fetch_cycle()
        self.assertEqual(len(data["follows"]), 2)
        for entry in data["follows"]:
            self.assertTrue(entry["last_fetch_ok"])
        reloaded = ff.read_follows()
        self.assertEqual(len(reloaded["follows"]), 2)


class TestGenerateFollowsPage(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self._orig = ff.SITE_FOLLOWS_DIR
        ff.SITE_FOLLOWS_DIR = self.tmpdir

    def _cleanup(self):
        ff.SITE_FOLLOWS_DIR = self._orig
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_writes_index_html(self):
        data = {"follows": [
            {"display_name": "A Blog", "last_fetch_ok": True,
             "items": [{"title": "Hi", "url": "http://a.onion/hi/", "published_at": "2026-01-01"}]},
        ]}
        ff.generate_follows_page(data)
        with open(os.path.join(self.tmpdir, "index.html")) as f:
            html = f.read()
        self.assertIn("A Blog", html)
        self.assertIn("http://a.onion/hi/", html)
        self.assertIn("Hi", html)

    def test_empty_follows_shows_placeholder(self):
        ff.generate_follows_page({"follows": []})
        with open(os.path.join(self.tmpdir, "index.html")) as f:
            html = f.read()
        self.assertIn("Not following anyone yet", html)

    def test_escapes_untrusted_content(self):
        data = {"follows": [
            {"display_name": "<script>alert(1)</script>", "last_fetch_ok": True, "items": []},
        ]}
        ff.generate_follows_page(data)
        with open(os.path.join(self.tmpdir, "index.html")) as f:
            html = f.read()
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_no_leftover_tmp_file(self):
        ff.generate_follows_page({"follows": []})
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, "index.html.tmp")))


if __name__ == "__main__":
    unittest.main()

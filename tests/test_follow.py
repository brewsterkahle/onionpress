"""Tests for src/onionpress/follow.py — follow-list management.

All reads/writes shell out to `docker exec onionpress-tor` (the store lives
inside a Docker named volume, not a host path — see follow.py's module
docstring), so every test mocks subprocess.run.
"""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from onionpress import follow


def _ok(stdout=""):
    return mock.Mock(returncode=0, stdout=stdout, stderr="")


def _fail(stderr="error"):
    return mock.Mock(returncode=1, stdout="", stderr=stderr)


class TestReadWriteFollows(unittest.TestCase):
    def test_read_missing_returns_empty_schema(self):
        with mock.patch("onionpress.follow.subprocess.run", return_value=_fail()):
            data = follow._read_follows()
        self.assertEqual(data, {"schema": 1, "follows": []})

    def test_read_malformed_json_returns_empty_schema(self):
        with mock.patch("onionpress.follow.subprocess.run", return_value=_ok("not json")):
            data = follow._read_follows()
        self.assertEqual(data, {"schema": 1, "follows": []})

    def test_read_valid_json(self):
        payload = json.dumps({"schema": 1, "follows": [{"key": "a"}]})
        with mock.patch("onionpress.follow.subprocess.run", return_value=_ok(payload)):
            data = follow._read_follows()
        self.assertEqual(data["follows"], [{"key": "a"}])

    def test_write_pipes_json_via_docker_exec(self):
        with mock.patch("onionpress.follow.subprocess.run", return_value=_ok()) as m_run:
            ok = follow._write_follows({"schema": 1, "follows": []})
        self.assertTrue(ok)
        # Second call is the `sh -c "cat > ..."` write; check the piped input.
        write_call = m_run.call_args_list[-1]
        self.assertEqual(write_call.kwargs["input"], json.dumps({"schema": 1, "follows": []}, indent=2))

    def test_write_failure_returns_false(self):
        with mock.patch("onionpress.follow.subprocess.run", return_value=_fail()):
            ok = follow._write_follows({"schema": 1, "follows": []})
        self.assertFalse(ok)


class TestSlugify(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(follow._slugify("Some Blog!"), "some-blog")

    def test_empty_falls_back(self):
        self.assertEqual(follow._slugify("***"), "follow")

    def test_unique_key_appends_suffix_on_collision(self):
        self.assertEqual(follow._unique_key("blog", {"blog"}), "blog-2")
        self.assertEqual(follow._unique_key("blog", {"blog", "blog-2"}), "blog-3")
        self.assertEqual(follow._unique_key("blog", set()), "blog")


class TestAddFollow(unittest.TestCase):
    def test_rejects_invalid_url(self):
        ok, msg = follow.add_follow("not-a-url")
        self.assertFalse(ok)
        self.assertIn("valid feed URL", msg)

    def test_adds_new_follow(self):
        with mock.patch("onionpress.follow._read_follows",
                         return_value={"schema": 1, "follows": []}), \
             mock.patch("onionpress.follow._write_follows", return_value=True) as m_write:
            ok, key = follow.add_follow("http://example.onion/feed/", display_name="Example")
        self.assertTrue(ok)
        self.assertEqual(key, "example")
        written = m_write.call_args[0][0]
        self.assertEqual(len(written["follows"]), 1)
        entry = written["follows"][0]
        self.assertEqual(entry["feed_url"], "http://example.onion/feed/")
        self.assertEqual(entry["display_name"], "Example")
        self.assertEqual(entry["items"], [])

    def test_default_name_is_hostname(self):
        with mock.patch("onionpress.follow._read_follows",
                         return_value={"schema": 1, "follows": []}), \
             mock.patch("onionpress.follow._write_follows", return_value=True) as m_write:
            follow.add_follow("http://abc123.onion/feed/")
        entry = m_write.call_args[0][0]["follows"][0]
        self.assertEqual(entry["display_name"], "abc123.onion")

    def test_rejects_duplicate_feed_url(self):
        existing = {"schema": 1, "follows": [
            {"key": "a", "feed_url": "http://abc.onion/feed/"},
        ]}
        with mock.patch("onionpress.follow._read_follows", return_value=existing):
            ok, msg = follow.add_follow("http://abc.onion/feed/")
        self.assertFalse(ok)
        self.assertIn("Already following", msg)

    def test_deduplicates_slug_on_collision(self):
        existing = {"schema": 1, "follows": [
            {"key": "example", "feed_url": "http://example.onion/feed/"},
        ]}
        with mock.patch("onionpress.follow._read_follows", return_value=existing), \
             mock.patch("onionpress.follow._write_follows", return_value=True) as m_write:
            ok, key = follow.add_follow("http://example.onion/other-feed/", display_name="Example")
        self.assertTrue(ok)
        self.assertEqual(key, "example-2")

    def test_write_failure_propagates(self):
        with mock.patch("onionpress.follow._read_follows",
                         return_value={"schema": 1, "follows": []}), \
             mock.patch("onionpress.follow._write_follows", return_value=False):
            ok, msg = follow.add_follow("http://abc.onion/feed/")
        self.assertFalse(ok)
        self.assertIn("running", msg)


class TestRemoveFollow(unittest.TestCase):
    def test_removes_existing_key(self):
        existing = {"schema": 1, "follows": [
            {"key": "a", "feed_url": "http://a.onion/feed/"},
            {"key": "b", "feed_url": "http://b.onion/feed/"},
        ]}
        with mock.patch("onionpress.follow._read_follows", return_value=existing), \
             mock.patch("onionpress.follow._write_follows", return_value=True) as m_write:
            ok, msg = follow.remove_follow("a")
        self.assertTrue(ok)
        remaining = [f["key"] for f in m_write.call_args[0][0]["follows"]]
        self.assertEqual(remaining, ["b"])

    def test_unknown_key_fails(self):
        with mock.patch("onionpress.follow._read_follows",
                         return_value={"schema": 1, "follows": []}):
            ok, msg = follow.remove_follow("nope")
        self.assertFalse(ok)
        self.assertIn("No follow", msg)


class TestListFollows(unittest.TestCase):
    def test_returns_follows_list(self):
        with mock.patch("onionpress.follow._read_follows",
                         return_value={"schema": 1, "follows": [{"key": "a"}]}):
            self.assertEqual(follow.list_follows(), [{"key": "a"}])


if __name__ == "__main__":
    unittest.main()

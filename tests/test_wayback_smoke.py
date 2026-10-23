#!/usr/bin/env python3
"""wayback_smoke (src/onionpress/wayback_smoke.py): `onionpress
smoke-test-wayback` publishes a throwaway post, drives the Wayback sweep
cron until the post's `_op_wayback_*` meta says archived, and deletes the
post. wp-cli is faked here (subprocess.run), so nothing reaches Docker;
the census in test_wp_cli_as_www_data.py holds the real calls to
www-data."""

import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from onionpress import wayback_smoke as ws  # noqa: E402

SITES = '[{"blog_id": 1, "path": "/", "url": "http://x.onion/"},' \
        ' {"blog_id": 2, "path": "/me/", "url": "http://x.onion/me/"}]'


def _completed(stdout="", rc=0, stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


class FakeWp:
    """Answers each wp-cli argv; records them; the meta advances per sweep."""

    def __init__(self, meta_by_tick, publish="42\n"):
        self.meta_by_tick = meta_by_tick
        self.publish = publish
        self.calls = []
        self.ticks = 0

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        wp = argv[argv.index("wp") + 1:]
        if wp[:2] == ["site", "list"]:
            return _completed(SITES)
        if wp[:2] == ["post", "create"]:
            return _completed(self.publish)
        if wp[:3] == ["cron", "event", "run"]:
            self.ticks += 1
            return _completed("")
        if wp[:3] == ["post", "meta", "get"]:
            meta = self.meta_by_tick[min(self.ticks, len(self.meta_by_tick)) - 1]
            return _completed(meta.get(wp[4], ""))
        if wp[:2] == ["post", "delete"]:
            return _completed("Success\n")
        raise AssertionError(wp)


class TestWpArgv(unittest.TestCase):
    def test_runs_wp_cli_as_www_data_with_the_path_and_url(self):
        with mock.patch.object(subprocess, "run", return_value=_completed("")) as run:
            ws._wp(["site", "list"], url="http://x.onion/me/", timeout=15)
        argv = run.call_args.args[0]
        self.assertEqual(argv[:2], ["docker", "exec"])
        self.assertIn("-u", argv)
        self.assertEqual(argv[argv.index("-u") + 1], "www-data")
        self.assertNotIn("--allow-root", argv)
        self.assertEqual(argv[-4:], ["site", "list", "--path=/var/www/html", "--url=http://x.onion/me/"])
        self.assertEqual(run.call_args.kwargs["timeout"], 15)


class TestPickSite(unittest.TestCase):
    def test_prefers_a_subsite(self):
        with mock.patch.object(subprocess, "run", return_value=_completed(SITES)):
            self.assertEqual(ws._pick_test_site()["blog_id"], 2)

    def test_the_main_site_when_there_is_no_other(self):
        with mock.patch.object(subprocess, "run",
                               return_value=_completed('[{"blog_id": 1, "path": "/", "url": "u"}]')):
            self.assertEqual(ws._pick_test_site()["blog_id"], 1)

    def test_a_failed_listing_raises(self):
        with mock.patch.object(subprocess, "run", return_value=_completed("", rc=1, stderr="boom")):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                ws._pick_test_site()


class TestMetaState(unittest.TestCase):
    def test_unset_keys_are_empty_strings(self):
        def run(argv, **kw):
            key = argv[argv.index("get") + 2]
            if key == "_op_wayback_job_id":
                return _completed('"spn2-abc"\n')
            return _completed("", rc=1)
        with mock.patch.object(subprocess, "run", side_effect=run):
            state = ws._get_post_wayback_state("42", "http://x.onion/me/")
        self.assertEqual(state["_op_wayback_job_id"], '"spn2-abc"')
        self.assertEqual(state["_op_wayback_archived_at"], "")
        self.assertEqual(len(state), 6)


class TestSmokeTest(unittest.TestCase):
    def setUp(self):
        self.log = []
        p = mock.patch.object(ws.time, "sleep")
        p.start()
        self.addCleanup(p.stop)

    def test_passes_once_archived_and_deletes_the_post(self):
        wp = FakeWp([
            {},
            {"_op_wayback_job_id": "j1", "_op_wayback_submitted_at": "1"},
            {"_op_wayback_job_id": "j1", "_op_wayback_archived_at": "2", "_op_wayback_snapshot_ts": "20260101"},
        ])
        with mock.patch.object(subprocess, "run", side_effect=wp):
            self.assertEqual(ws.smoke_test_wayback(self.log.append), 0)
        self.assertEqual(wp.ticks, 3)
        self.assertTrue(any(m.startswith("PASS: post archived (snapshot_ts=20260101)") for m in self.log))
        self.assertEqual(wp.calls[-1][-6:-3], ["post", "delete", "42"])
        self.assertIn("--force", wp.calls[-1])
        self.assertIn("Deleted test post ID 42", self.log)
        # The post was created on the subsite, published, by id.
        create = next(c for c in wp.calls if "create" in c)
        self.assertIn("--post_status=publish", create)
        self.assertIn("--url=http://x.onion/me/", create)

    def test_a_timeout_fails_and_still_deletes(self):
        wp = FakeWp([{"_op_wayback_last_error_ext": "SPN 429"}])
        clock = iter([0.0, 1.0, 1000.0])
        with mock.patch.object(subprocess, "run", side_effect=wp), \
                mock.patch.object(ws.time, "monotonic", side_effect=clock):
            self.assertEqual(ws.smoke_test_wayback(self.log.append), 1)
        self.assertTrue(any(m.startswith("FAIL: timed out") and "SPN 429" in m for m in self.log))
        self.assertEqual(wp.calls[-1][-6:-3], ["post", "delete", "42"])

    def test_a_failed_publish_is_a_failure_with_no_post_to_delete(self):
        wp = FakeWp([{}], publish="Error: nope\n")
        with mock.patch.object(subprocess, "run", side_effect=wp):
            self.assertEqual(ws.smoke_test_wayback(self.log.append), 1)
        self.assertTrue(any(m.startswith("FAIL: publish failed") for m in self.log))
        self.assertFalse(any("delete" in c for c in wp.calls))

    def test_no_site_is_a_failure(self):
        with mock.patch.object(subprocess, "run", return_value=_completed("", rc=1, stderr="down")):
            self.assertEqual(ws.smoke_test_wayback(self.log.append), 1)
        self.assertEqual(self.log, ["FAIL: wp site list failed: down"])


if __name__ == "__main__":
    unittest.main()

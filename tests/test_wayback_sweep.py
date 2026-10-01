#!/usr/bin/env python3
"""Integration tests for the Wayback archive plugin's sweep engine.

These drive the live plugin inside the onionpress-wordpress container
via `wp eval`, using the mock filter hooks we added to short-circuit
every network-touching function (user_status, submit, poll, cdx,
self_reachable) — no real Tor/SPN traffic — and the onion address.

Coverage focus: behaviors that are easy to break during refactors.
  1. Queue totals aggregate across the subsites the sweep works on,
     leaving excluded ones out.
  2. CDX rescue: SPN flips success->error, CDX still has a capture;
     the post must end up archived via the CDX timestamp, not errored.
  3. Young-job skip: a job submitted in the last 15s must NOT be
     polled (wastes a Tor round-trip on a guaranteed "pending").
  4. Submit path: a fresh post with no job_id gets one, with a
     matching submitted_at, on a successful submit.
  5. Lock mutex: a fresh lock blocks a second sweep invocation.
  6. Exclusion: the sweep never visits an excluded subsite, and
     publishing on one schedules no sweep.

Sandbox safety: everything runs on a dedicated subsite
(`op-wayback-test`) that each class creates afresh, excluded from the
sweep, and deletes when it finishes. Earlier versions took the first
non-root subsite — on a live install, the owner's real blog — and
published and force-deleted posts there and deleted its live sweep lock
and back-off. The lock and back-off this suite touches are the
sandbox's own options. The onion address comes from a mock, because the
target must not have one: wp_integration.py refuses a container with
/var/lib/onionpress/onion_address.

Opt-in: skipped unless ONIONPRESS_INTEGRATION_TESTS=1, and refused on a
live install — see wp_integration.py. Needs the plugin from this
checkout in mu-plugins/; a plugin without the sweep exclusion is
refused before the sandbox is created.
"""

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wp_integration  # noqa: E402

_wp = wp_integration.wp
_eval = wp_integration.wp_eval

_TEST_SUBSITE_SLUG = "op-wayback-test"

# Stands in for the onion address file the target must not have.
_MOCK_ONION = "wayback-test-sandbox.onion"


class _WaybackSandbox(wp_integration.SandboxTestCase):
    SANDBOX_SLUG = _TEST_SUBSITE_SLUG
    SANDBOX_TITLE = "Wayback Sweep Test Sandbox"
    # The exclusion is what keeps the sandbox out of the live sweep.
    REQUIRED_PHP_FUNCTIONS = ("onionpress_wayback_site_excluded",
                              "onionpress_wayback_sites")


class TestWaybackQueueTotals(_WaybackSandbox):
    """Queue totals aggregate across the subsites the sweep works on."""

    def test_totals_structure(self):
        """Totals come back as expected, with the remaining invariant holding."""
        out = _eval("echo json_encode(onionpress_wayback_queue_totals());",
                    self.url)
        totals = json.loads(out)
        for k in ("archived", "in_flight", "remaining", "total"):
            self.assertIn(k, totals, f"missing key: {k}")
            self.assertIsInstance(totals[k], int)
        # remaining = max(0, total - archived - in_flight).
        self.assertEqual(
            totals["remaining"],
            max(0, totals["total"] - totals["archived"] - totals["in_flight"]),
        )

    def test_excluded_subsite_is_left_out(self):
        """The sandbox's posts don't count while it is excluded. Seen as
        included — through the filter, in this process only — they add
        exactly this subsite's count to the aggregate."""
        _wp(["post", "create", "--post_type=post", "--post_status=publish",
             "--post_title=wayback-test-totals", "--porcelain"],
            url=self.url, timeout=15)
        php = """
        global $wpdb;
        $mine = (int) $wpdb->get_var(
            "SELECT COUNT(*) FROM $wpdb->posts WHERE post_status='publish' "
            . "AND post_type IN ('post','page')"
        );
        $bid = get_current_blog_id();
        $excluded = onionpress_wayback_queue_totals();
        add_filter('onionpress_wayback_site_excluded',
                   function($ex, $id) use ($bid) { return $id === $bid ? false : $ex; }, 10, 2);
        $included = onionpress_wayback_queue_totals();
        echo json_encode(array(
            'mine'     => $mine,
            'excluded' => $excluded['total'],
            'included' => $included['total'],
        ));
        """
        out = json.loads(_eval(php, self.url))
        self.assertGreater(out["mine"], 0)
        self.assertEqual(out["included"] - out["excluded"], out["mine"],
            f"aggregate should gain exactly this subsite's posts: {out}")


class TestWaybackSandboxExclusion(_WaybackSandbox):
    """An excluded subsite is out of the sweep's reach — never visited,
    never scheduling a sweep of its own. This is what keeps a sandbox
    out of the public Wayback Machine on a stack that could submit."""

    def test_sweep_never_visits_it(self):
        php = """
        $swept = array_map(function($s) { return (int) $s->blog_id; },
                           onionpress_wayback_sites());
        echo json_encode(array(
            'excluded' => onionpress_wayback_site_excluded(get_current_blog_id()),
            'swept'    => in_array(get_current_blog_id(), $swept, true),
            'main'     => in_array((int) get_main_site_id(), $swept, true),
        ));
        """
        out = json.loads(_eval(php, self.url))
        self.assertTrue(out["excluded"], "the sandbox was created excluded")
        self.assertFalse(out["swept"], "the sweep must skip an excluded subsite")
        self.assertTrue(out["main"], "exclusion must not reach the network root")

    def test_publishing_schedules_no_sweep(self):
        """save_post and wp_insert_comment leave the subsite's cron
        without a sweep, so it never starts a daemon of its own."""
        php = """
        $pid = wp_insert_post(array(
            'post_type'=>'post','post_status'=>'publish',
            'post_title'=>'wayback-test-schedule','post_content'=>'<p>x</p>',
            'meta_input'=>array(
                '_source_id'=>'mastodon:wbsched-' . time(),
                '_op_wayback_archived_at'=>'2026-04-01 12:00:00',
            ),
        ));
        wp_insert_comment(array(
            'comment_post_ID'=>$pid,
            'comment_content'=>'<p>thread reply</p>',
            'comment_approved'=>1,
        ));
        echo wp_next_scheduled('onionpress_wayback_sweep') ? 'scheduled' : 'none';
        """
        self.assertEqual(_eval(php, self.url), "none")


class TestWaybackSweepIteration(_WaybackSandbox):
    """Sweep iteration behavior with mocked network functions."""

    def setUp(self):
        _wp(["option", "delete", "op_wayback_backoff_until"],
            url=self.url, timeout=15)
        r = _wp(["post", "create", "--post_type=post", "--post_status=publish",
                 "--post_title=wayback-test-" + self._testMethodName,
                 "--porcelain"], url=self.url, timeout=15)
        self.post_id = int(r.stdout.strip())
        self.addCleanup(self._cleanup_post)

    def _cleanup_post(self):
        _wp(["post", "delete", str(self.post_id), "--force"],
            url=self.url, timeout=15)

    def _set_meta(self, key, value):
        _wp(["post", "meta", "update", str(self.post_id), key, str(value)],
            url=self.url, timeout=15)

    def _get_meta(self, key):
        r = _wp(["post", "meta", "get", str(self.post_id), key],
                url=self.url, timeout=15)
        return r.stdout.strip()

    def _common_mocks(self, available=40):
        """Short-circuit the onion address, reachability + user_status so
        the iteration reaches the poll/submit phases."""
        return f"""
        add_filter('onionpress_wayback_onion_addr_mock',
                   function() {{ return '{_MOCK_ONION}'; }});
        add_filter('onionpress_wayback_self_reachable_mock',
                   function() {{ return true; }});
        add_filter('onionpress_wayback_user_status_mock',
                   function() {{ return array('available' => {available}, 'processing' => 0); }});
        """

    def test_cdx_rescues_spn_error(self):
        """SPN flips success->error; CDX still has capture -> post archived via CDX."""
        self._set_meta("_op_wayback_job_id", "jid-cdx-test")
        self._set_meta("_op_wayback_submitted_at", str(int(time.time()) - 120))

        php = self._common_mocks() + """
        add_filter('onionpress_wayback_poll_parallel_mock',
                   function($_, $job_ids) {
            return array(array(
                'job_id'     => 'jid-cdx-test',
                'status'     => 'error',
                'status_ext' => 'error:no-captures',
            ));
        }, 10, 2);
        add_filter('onionpress_wayback_cdx_lookup_parallel_mock',
                   function($_, $urls) {
            $out = array();
            foreach ($urls as $k => $v) { $out[$k] = '20260101120000'; }
            return $out;
        }, 10, 2);
        add_filter('onionpress_wayback_submit_parallel_mock',
                   function($_, $urls) {
            return array_fill_keys(array_keys($urls), '');
        }, 10, 2);
        onionpress_wayback_sweep_iteration();
        echo 'ok';
        """
        _eval(php, self.url)
        self.assertNotEqual("", self._get_meta("_op_wayback_archived_at"),
            "post should be archived via CDX rescue")
        self.assertEqual("20260101120000", self._get_meta("_op_wayback_snapshot_ts"),
            "snapshot_ts should come from the CDX timestamp")
        self.assertEqual("", self._get_meta("_op_wayback_job_id"),
            "job_id should be cleared after success")

    def test_young_job_is_not_polled(self):
        """A job submitted < YOUNG_JOB_SKIP_SEC ago MUST NOT be polled."""
        self._set_meta("_op_wayback_job_id", "jid-young-test")
        self._set_meta("_op_wayback_submitted_at", str(int(time.time())))

        php = self._common_mocks() + """
        delete_option('op_test_wb_poll_called_with');
        add_filter('onionpress_wayback_poll_parallel_mock',
                   function($_, $job_ids) {
            update_option('op_test_wb_poll_called_with',
                          implode(',', $job_ids), false);
            return array();
        }, 10, 2);
        add_filter('onionpress_wayback_submit_parallel_mock',
                   function($_, $urls) {
            return array_fill_keys(array_keys($urls), '');
        }, 10, 2);
        onionpress_wayback_sweep_iteration();
        echo (string) get_option('op_test_wb_poll_called_with', '(unset)');
        """
        out = _eval(php, self.url)
        self.assertNotIn("jid-young-test", out,
            f"young job should not be polled; poll got: {out}")

    def test_submit_assigns_job_id(self):
        """A fresh post (no job_id) gets one on a successful submit."""
        php = self._common_mocks() + f"""
        add_filter('onionpress_wayback_poll_parallel_mock',
                   function($_, $job_ids) {{ return array(); }}, 10, 2);
        add_filter('onionpress_wayback_submit_parallel_mock',
                   function($_, $urls) {{
            $out = array();
            foreach ($urls as $k => $v) {{
                $out[$k] = ($k === 'post:{self.post_id}')
                    ? 'jid-submit-test'
                    : '';
            }}
            return $out;
        }}, 10, 2);
        onionpress_wayback_sweep_iteration();
        echo 'ok';
        """
        _eval(php, self.url)
        self.assertEqual("jid-submit-test", self._get_meta("_op_wayback_job_id"),
            "post should have received the mocked job_id")
        submitted_at = self._get_meta("_op_wayback_submitted_at")
        self.assertNotEqual("", submitted_at, "submitted_at should be set")
        self.assertGreater(int(submitted_at), int(time.time()) - 60)


class TestWaybackSweepLock(_WaybackSandbox):
    """Token-lock mutex semantics for the sweep entry point."""

    def setUp(self):
        _wp(["option", "delete", "op_wayback_sweep_lock"],
            url=self.url, timeout=15)

    def tearDown(self):
        _wp(["option", "delete", "op_wayback_sweep_lock"],
            url=self.url, timeout=15)

    def test_fresh_lock_blocks_second_invocation(self):
        """A fresh lock (< STALE threshold) rejects a new sweep."""
        php_seed = """
        update_option('op_wayback_sweep_lock',
                      'otherTok:' . time(), false);
        echo 'seeded';
        """
        _eval(php_seed, self.url)
        php = """
        add_filter('onionpress_wayback_self_reachable_mock',
                   function() { return true; });
        add_filter('onionpress_wayback_user_status_mock',
                   function() { return array('available' => 0); });
        onionpress_wayback_sweep();
        echo (string) get_option('op_wayback_sweep_lock', '(empty)');
        """
        out = _eval(php, self.url)
        self.assertTrue(out.startswith("otherTok:"),
            f"lock should still belong to otherTok: {out}")


class TestWaybackCommentResnapshot(_WaybackSandbox):
    """`wp_insert_comment` triggers exactly one re-archive of the parent
    post — and only for imported posts that already have a snapshot.
    Caps the social-importer-threading SPN cost at one extra snapshot
    per parent (instead of one per comment)."""

    def _make_imported_post(self, archived=True):
        """Insert a publish-state imported post with the wayback metadata
        we'd expect after a successful capture (or empty if archived=False)."""
        archived_at = "2026-04-01 12:00:00" if archived else ""
        snapshot_ts = "20260401120000" if archived else ""
        pid = int(_eval(f"""
        $pid = wp_insert_post(array(
            'post_type'=>'post','post_status'=>'publish',
            'post_title'=>'imported parent','post_content'=>'<p>parent</p>',
            'meta_input'=>array(
                '_source_id'=>'mastodon:wbresnap-{int(time.time()*1000)}',
                '_op_wayback_archived_at'=>'{archived_at}',
                '_op_wayback_snapshot_ts'=>'{snapshot_ts}',
            ),
        ));
        echo (int)$pid;
        """, self.url))
        self.addCleanup(_eval, f"wp_delete_post({pid}, true);", self.url)
        return pid

    def _add_comment(self, post_id):
        cid = int(_eval(f"""
        $cid = wp_insert_comment(array(
            'comment_post_ID'=>{post_id},
            'comment_author'=>'me',
            'comment_content'=>'<p>thread reply</p>',
            'comment_approved'=>1,
        ));
        echo (int)$cid;
        """, self.url))
        return cid

    def _meta(self, post_id, key):
        return _eval(
            f"echo (string) get_post_meta({post_id}, '{key}', true);",
            self.url,
        )

    def test_first_comment_clears_snapshot_and_marks_resnapshot_done(self):
        pid = self._make_imported_post(archived=True)
        self.assertEqual(self._meta(pid, "_op_wayback_archived_at"),
                         "2026-04-01 12:00:00")
        self.assertEqual(self._meta(pid, "_op_wayback_resnapshot_done"), "")
        self._add_comment(pid)
        # Snapshot fields cleared → post will re-enter the queue.
        self.assertEqual(self._meta(pid, "_op_wayback_archived_at"), "")
        self.assertEqual(self._meta(pid, "_op_wayback_snapshot_ts"), "")
        self.assertEqual(self._meta(pid, "_op_wayback_resnapshot_done"), "1")

    def test_second_comment_is_noop(self):
        """Once flagged, further comments don't re-trigger — caps total
        comment-driven re-archives at one per parent."""
        pid = self._make_imported_post(archived=True)
        self._add_comment(pid)
        # Manually re-archive it (simulate the sweep completing).
        _eval(f"""
        update_post_meta({pid}, '_op_wayback_archived_at', '2026-04-02 00:00:00');
        update_post_meta({pid}, '_op_wayback_snapshot_ts', '20260402000000');
        """, self.url)
        # Adding a second comment should NOT clear the new snapshot.
        self._add_comment(pid)
        self.assertEqual(self._meta(pid, "_op_wayback_archived_at"),
                         "2026-04-02 00:00:00")

    def test_unarchived_post_is_skipped(self):
        """A post without a prior snapshot has nothing to invalidate —
        save_post will queue it through the normal path. The hook
        should not flip resnapshot_done in that case."""
        pid = self._make_imported_post(archived=False)
        self._add_comment(pid)
        self.assertEqual(self._meta(pid, "_op_wayback_resnapshot_done"), "")

    def test_original_post_is_skipped(self):
        """Posts without _source_id are 'original' — re-archive is
        already handled by save_post on actual edits, not by this hook."""
        pid = int(_eval(f"""
        $pid = wp_insert_post(array(
            'post_type'=>'post','post_status'=>'publish',
            'post_title'=>'original','post_content'=>'<p>original</p>',
            'meta_input'=>array(
                '_op_wayback_archived_at'=>'2026-04-01 12:00:00',
                '_op_wayback_snapshot_ts'=>'20260401120000',
            ),
        ));
        echo (int)$pid;
        """, self.url))
        self.addCleanup(_eval, f"wp_delete_post({pid}, true);", self.url)
        self._add_comment(pid)
        # No re-archive triggered — original posts go through save_post.
        self.assertEqual(self._meta(pid, "_op_wayback_archived_at"),
                         "2026-04-01 12:00:00")
        self.assertEqual(self._meta(pid, "_op_wayback_resnapshot_done"), "")


if __name__ == "__main__":
    unittest.main()

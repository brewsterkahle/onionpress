#!/usr/bin/env python3
"""Integration tests for the Wayback archive plugin's sweep engine.

These drive the live plugin inside the onionpress-wordpress container
via `wp eval`, using the mock filter hooks we added to short-circuit
every network-touching function (user_status, submit, poll, cdx,
self_reachable) — no real Tor/SPN traffic.

Coverage focus: behaviors that are easy to break during refactors.
  1. Queue totals aggregate across every subsite in the network.
  2. Young-job skip: a job submitted in the last 15s must NOT be
     polled (wastes a Tor round-trip on a guaranteed "pending").
  3. Submit path: a fresh post with no job_id gets one, with a
     matching submitted_at, on a successful submit.
  4. Lock mutex: a fresh lock blocks a second sweep invocation.

The rest guard the ways this engine has actually wedged in production,
all of which shared one shape — it kept logging a healthy sweep while
archiving nothing:
  5. A job SPN has FORGOTTEN (absent from /save/status, not "pending"
     or "error") must be cleared, or that URL deadlocks permanently.
  6. A job whose status batch never came back must NOT be cleared —
     failing to ask is not the same as being told it is gone.
  7. CDX rescue + the $answered guard, in one iteration: an SPN "error"
     must be verified against CDX before being written off, and must
     not be swept up as forgotten on the way there.
  8. Coverage bookkeeping in poll_parallel itself, driven through the
     curl seam rather than mocked past: only a batch that came back as
     a JSON list of statuses counts as an answer about its job_ids.
  9. The daemon must recycle on a timer and hand its lock back. It ran
     70 hours in one PHP request, serving option reads from a cache
     that predated the fix being applied to the database.

Prerequisites (skips the suite if any fails):
  - Docker running
  - `onionpress-wordpress` container up with the wayback plugin
    present in mu-plugins/
  - At least one subsite to target
"""

import json
import shutil
import subprocess
import time
import unittest

_WP = "onionpress-wordpress"


def _docker_exec(args, **kwargs):
    return subprocess.run(
        ["docker", "exec", _WP] + args,
        capture_output=True, text=True, encoding='utf-8',
        errors='replace', **kwargs,
    )


def _wp(args, url=None, **kwargs):
    cmd = ["wp"] + args + ["--path=/var/www/html", "--allow-root"]
    if url:
        cmd.append("--url=" + url)
    return _docker_exec(cmd, **kwargs)


def _docker_available():
    if not shutil.which("docker"):
        return False
    r = subprocess.run(
        ["docker", "inspect", _WP, "--format={{.State.Running}}"],
        capture_output=True, text=True, timeout=10,
    )
    return r.returncode == 0 and "true" in r.stdout


def _pick_site():
    r = _wp(["site", "list", "--fields=blog_id,path,url", "--format=json"],
            timeout=15)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    sites = json.loads(r.stdout)
    sub = [s for s in sites if s.get("path") != "/"]
    return sub[0] if sub else (sites[0] if sites else None)


def _eval(php, url):
    """Run PHP inside WP, return stdout (stripped)."""
    r = _wp(["eval", php], url=url, timeout=90)
    return r.stdout.strip()


@unittest.skipUnless(_docker_available(), "requires running onionpress-wordpress container")
class TestWaybackQueueTotals(unittest.TestCase):
    """Queue totals aggregate correctly across every subsite."""

    @classmethod
    def setUpClass(cls):
        s = _pick_site()
        if s is None:
            raise unittest.SkipTest("no site available")
        cls.url = s["url"].rstrip("/") + "/"

    def test_totals_structure_and_aggregate(self):
        """Totals come back as expected, with the remaining invariant holding."""
        php = """
        $t = onionpress_wayback_queue_totals();
        echo json_encode($t);
        """
        out = _eval(php, self.url)
        totals = json.loads(out)
        for k in ("archived", "in_flight", "remaining", "total"):
            self.assertIn(k, totals, f"missing key: {k}")
            self.assertIsInstance(totals[k], int)
        # remaining = max(0, total - archived - in_flight).
        self.assertEqual(
            totals["remaining"],
            max(0, totals["total"] - totals["archived"] - totals["in_flight"]),
        )
        # Aggregated total must be >= this subsite alone.
        php_one = """
        global $wpdb;
        echo (int) $wpdb->get_var(
            "SELECT COUNT(*) FROM $wpdb->posts WHERE post_status='publish' "
            . "AND post_type IN ('post','page')"
        );
        """
        one = int(_eval(php_one, self.url))
        self.assertGreaterEqual(totals["total"], one,
            f"aggregated total {totals['total']} < this subsite's {one}")


@unittest.skipUnless(_docker_available(), "requires running onionpress-wordpress container")
class TestWaybackSweepIteration(unittest.TestCase):
    """Sweep iteration behavior with mocked network functions."""

    @classmethod
    def setUpClass(cls):
        s = _pick_site()
        if s is None:
            raise unittest.SkipTest("no site available")
        cls.url = s["url"].rstrip("/") + "/"

    def setUp(self):
        _wp(["option", "delete", "op_wayback_backoff_until"],
            url=self.url, timeout=15)
        self._save_sitewide_state()
        r = _wp(["post", "create", "--post_type=post", "--post_status=publish",
                 "--post_title=wayback-test-" + self._testMethodName,
                 "--porcelain"], url=self.url, timeout=15)
        self.post_id = int(r.stdout.strip())
        self.addCleanup(self._cleanup_post)

    def _save_sitewide_state(self):
        """Snapshot the home/feed capture options and restore them after.

        These tests run against a LIVE site, and the home/feed captures are
        real archive records kept in wp_options rather than post meta. A
        sweep iteration writes them, and one test seeds them outright — so
        without this, running the suite marks the site's home page
        unarchived and the next real sweep spends an SPN slot re-capturing
        it. The post each test creates is disposable; these options are not.
        """
        php = """
        echo base64_encode(json_encode(array(
            'op_wayback_home_state' => get_option('op_wayback_home_state', null),
            'op_wayback_feed_state' => get_option('op_wayback_feed_state', null),
        )));
        """
        saved = _eval(php, self.url)
        self.addCleanup(self._restore_sitewide_state, saved)

    def _restore_sitewide_state(self, saved):
        php = """
        $s = json_decode(base64_decode('%s'), true);
        if (!is_array($s)) { echo 'no-snapshot'; return; }
        foreach ($s as $opt => $val) {
            if ($val === null) { delete_option($opt); }
            else { update_option($opt, $val, false); }
        }
        echo 'restored';
        """ % saved
        _eval(php, self.url)

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
        """Short-circuit reachability + user_status so the iteration
        reaches the poll/submit phases."""
        return f"""
        add_filter('onionpress_wayback_self_reachable_mock',
                   function() {{ return true; }});
        add_filter('onionpress_wayback_user_status_mock',
                   function() {{ return array('available' => {available}, 'processing' => 0); }});
        """

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

    def test_job_spn_has_forgotten_is_cleared(self):
        """SPN has a behavior its API doesn't document: a job_id it has
        entirely forgotten comes back ABSENT from /save/status rather than
        as 'pending' or 'error'. Every finalize branch keys off a returned
        status dict, so such a job matched nothing, kept its job_id, and
        was skipped by the submit step forever. This site's home and feed
        sat that way for five days, archiving nothing, while every sweep
        logged a healthy avail=40.
        """
        self._set_meta("_op_wayback_job_id", "jid-forgotten-test")
        self._set_meta("_op_wayback_submitted_at", str(int(time.time()) - 4000))

        php = self._common_mocks() + """
        add_filter('onionpress_wayback_poll_parallel_mock',
                   function($_, $job_ids) { return array(); }, 10, 2);
        add_filter('onionpress_wayback_submit_parallel_mock',
                   function($_, $urls) {
            return array_fill_keys(array_keys($urls), '');
        }, 10, 2);
        onionpress_wayback_sweep_iteration();
        echo 'ok';
        """
        _eval(php, self.url)
        # The submit mock returns '' for everything, so nothing re-flights
        # and the outcome is exactly "cleared" — assert that, not merely
        # "changed". Both halves of the write matter: a surviving
        # submitted_at with no job_id would make the record look freshly
        # submitted to the age checks.
        self.assertEqual("", self._get_meta("_op_wayback_job_id"),
            "a job_id SPN no longer knows must not survive the sweep — "
            "keeping it deadlocks this URL permanently")
        self.assertEqual("", self._get_meta("_op_wayback_submitted_at"),
            "submitted_at must be cleared alongside the job_id")

    def test_cdx_rescue_and_the_answered_guard(self):
        """Two behaviours that only separate when they run in one iteration.

        1. CDX rescue: SPN flips success->error while the capture is still
           in CDX, so an errored job must be verified against CDX before
           being written off, and archived from the CDX timestamp.
        2. The $answered guard: the forgotten-sweep runs BEFORE that rescue,
           so a job SPN did answer for must be excluded from it. The guard
           is what protects the errored records the over-budget CDX path
           deliberately leaves in flight — without it the next sweep clears
           exactly those job_ids and the deferral means nothing.

        Asserting on the returned counters, not just the final meta, is
        the point. Both paths end up writing the same archived_at and
        snapshot_ts, so a state-only assertion passes with the guard
        deleted: the forgotten-sweep clears the job_id, then the rescue —
        holding its own pre-loop snapshot of the errored records —
        overwrites the result. `forgotten` is the only observable that
        tells the two apart.
        """
        self._set_meta("_op_wayback_job_id", "jid-answered-test")
        self._set_meta("_op_wayback_submitted_at", str(int(time.time()) - 4000))

        # A second in-flight record SPN says nothing about, so the same
        # iteration exercises both branches.
        php = self._common_mocks() + """
        update_option('op_wayback_home_state',
                      array('job_id' => 'jid-silent-test',
                            'submitted_at' => time() - 4000), false);
        add_filter('onionpress_wayback_poll_parallel_mock',
                   function($_, $job_ids) {
            return array(array(
                'job_id'     => 'jid-answered-test',
                'status'     => 'error',
                'status_ext' => 'error:no-captures',
            ));
        }, 10, 2);
        add_filter('onionpress_wayback_cdx_lookup_parallel_mock',
                   function($_, $urls) {
            $out = array();
            foreach ($urls as $k => $v) { $out[$k] = '20260202120000'; }
            return $out;
        }, 10, 2);
        add_filter('onionpress_wayback_submit_parallel_mock',
                   function($_, $urls) {
            return array_fill_keys(array_keys($urls), '');
        }, 10, 2);
        $stats = onionpress_wayback_sweep_iteration();
        $home = get_option('op_wayback_home_state', array());
        $stats['home_job'] = $home['job_id'] ?? '';
        echo json_encode($stats);
        """
        stats = json.loads(_eval(php, self.url))

        # Exactly one job was forgotten — the silent one. Two would mean
        # the answered job was swept up as well.
        self.assertEqual(1, stats["forgotten"],
            "only the job SPN stayed silent about may count as forgotten; "
            f"got {stats['forgotten']} — the $answered guard is not holding")
        self.assertEqual(1, stats["cdx"],
            f"the answered job should have been rescued via CDX; got {stats}")
        # ...and the rescue wrote a real record, from the CDX timestamp.
        self.assertEqual("20260202120000", self._get_meta("_op_wayback_snapshot_ts"),
            "snapshot_ts should come from the CDX timestamp")
        self.assertNotEqual("", self._get_meta("_op_wayback_archived_at"),
            "the answered job should end up archived via CDX")
        self.assertEqual("", self._get_meta("_op_wayback_job_id"),
            "job_id should be cleared once the capture is recorded")
        # The job SPN stayed silent about is the one that gets cleared.
        self.assertEqual("", stats["home_job"],
            f"the unanswered job should have been cleared; got: {stats}")

    def test_job_whose_batch_never_answered_is_not_cleared(self):
        """poll_parallel chunks job_ids 20 at a time and silently drops any
        batch that fails — a non-200 or unparseable body contributes nothing
        to the return value and leaves no trace. Age alone cannot tell that
        apart from amnesia, so one 40s Tor timeout would reclassify a whole
        batch of old jobs as forgotten and resubmit all of them.

        Coverage tracking is the fix: only jobs whose batch actually came
        back count as answered-about. Here the job is old enough to clear
        but its batch never returned, so it must be left alone.
        """
        self._set_meta("_op_wayback_job_id", "jid-lost-batch-test")
        self._set_meta("_op_wayback_submitted_at", str(int(time.time()) - 4000))

        php = self._common_mocks() + """
        add_filter('onionpress_wayback_poll_parallel_mock',
                   function($_, $job_ids) { return array(); }, 10, 2);
        // No batch came back, so nothing is covered.
        add_filter('onionpress_wayback_poll_covered_mock',
                   function($_, $job_ids) { return array(); }, 10, 2);
        add_filter('onionpress_wayback_submit_parallel_mock',
                   function($_, $urls) {
            return array_fill_keys(array_keys($urls), '');
        }, 10, 2);
        onionpress_wayback_sweep_iteration();
        echo 'ok';
        """
        _eval(php, self.url)
        self.assertEqual("jid-lost-batch-test", self._get_meta("_op_wayback_job_id"),
            "a job whose status batch never came back must keep its job_id — "
            "we did not learn that SPN forgot it, only that we failed to ask")
        self.assertNotEqual("", self._get_meta("_op_wayback_submitted_at"),
            "submitted_at must survive alongside the job_id — clearing it "
            "alone would make the record read as a zombie on the next sweep "
            "and get it cleared there instead")

    def test_recently_submitted_job_survives_an_empty_poll(self):
        """A guard against over-clearing, not a regression test — it passes
        against the pre-fix plugin too, which cleared nothing at all.

        poll_parallel returns [] both when SPN forgot the jobs and when the
        request itself failed. Coverage tracking now separates those, but
        the age gate is the second line of defence: a job younger than the
        threshold at which a *pending* job would be resubmitted must
        survive a blank response, or one Tor blip resubmits everything.
        """
        self._set_meta("_op_wayback_job_id", "jid-fresh-test")
        self._set_meta("_op_wayback_submitted_at", str(int(time.time()) - 30))

        php = self._common_mocks() + """
        add_filter('onionpress_wayback_poll_parallel_mock',
                   function($_, $job_ids) { return array(); }, 10, 2);
        add_filter('onionpress_wayback_submit_parallel_mock',
                   function($_, $urls) {
            return array_fill_keys(array_keys($urls), '');
        }, 10, 2);
        onionpress_wayback_sweep_iteration();
        echo 'ok';
        """
        _eval(php, self.url)
        self.assertEqual("jid-fresh-test", self._get_meta("_op_wayback_job_id"),
            "a young job must survive an empty poll — it is far more likely "
            "the poll failed than that SPN forgot a job submitted seconds ago")

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


@unittest.skipUnless(_docker_available(), "requires running onionpress-wordpress container")
class TestWaybackPollCoverage(unittest.TestCase):
    """poll_parallel's own response handling, driven through the curl seam.

    Every sweep test above mocks poll_parallel wholesale, which leaves its
    body — the chunking, the chunk/result alignment, the HTTP-200 gate and
    the coverage bookkeeping — with no test at all. That body is where a
    misread response turns into "SPN forgot these 20 jobs", so it is the
    last place that should be untested. The default of the wholesale mock
    also happens to be the optimistic case (everything covered), i.e. the
    opposite of the real function's failure mode, so a test passing there
    says nothing about production.
    """

    @classmethod
    def setUpClass(cls):
        s = _pick_site()
        if s is None:
            raise unittest.SkipTest("no site available")
        cls.url = s["url"].rstrip("/") + "/"

    def _poll(self, responses_php):
        """Poll 25 ids (j0..j24) => two chunks of 20 and 5, one parallel
        group. $responses_php returns the mocked curl_multi result."""
        php = """
        add_filter('onionpress_wayback_curl_multi_mock', function($_, $setups) {
            %s
        }, 10, 2);
        $ids = array();
        for ($i = 0; $i < 25; $i++) { $ids[] = 'j' . $i; }
        $covered = null;
        $res = onionpress_wayback_poll_parallel($ids, $covered);
        echo json_encode(array(
            'results' => $res,
            'covered' => array_keys($covered),
        ));
        """ % responses_php
        return json.loads(_eval(php, self.url))

    def test_only_the_batch_that_answered_counts_as_covered(self):
        """One chunk answers, the other times out. Coverage must follow the
        chunk boundary exactly: $parallel_group[$i] has to line up with the
        keys curl_multi returns, or the coverage map describes the wrong
        jobs and the forgotten-sweep clears a batch nobody asked about."""
        out = self._poll("""
            return array(
                0 => array('code' => 200,
                           'body' => '[{"job_id":"j3","status":"pending"}]'),
                1 => array('code' => 0, 'body' => ''),   // Tor timeout
            );
        """)
        self.assertEqual(
            ["j%d" % i for i in range(20)], sorted(out["covered"], key=lambda s: int(s[1:])),
            "exactly the 20 ids of the chunk that answered must be covered")
        self.assertNotIn("j20", out["covered"],
            "an id from the chunk that timed out must NOT be marked covered — "
            "that is what makes one 40s timeout resubmit a whole batch")
        self.assertEqual(1, len(out["results"]))
        self.assertEqual("j3", out["results"][0]["job_id"])

    def test_a_200_that_is_not_a_status_list_covers_nothing(self):
        """SPN answers 200 with a JSON *object* — a rate-limit or auth
        envelope, {"message": ...}. That decodes to a PHP array and used to
        pass the is_array() gate, marking all 20 ids in the batch covered on
        a response carrying no statuses whatsoever. The forgotten-sweep then
        reads it as "SPN answered and mentioned none of them" and resubmits
        the lot: the exact over-clearing the coverage tracking exists to
        prevent, reached through a narrower door."""
        out = self._poll("""
            return array(
                0 => array('code' => 200,
                           'body' => '{"message":"You have reached the limit '
                                     . 'of active sessions"}'),
                1 => array('code' => 200, 'body' => '[]'),
            );
        """)
        self.assertNotIn("j0", out["covered"],
            "a 200 carrying an object, not a list of statuses, is not an "
            "answer about any job in the batch")
        # The empty-list chunk IS a real answer: SPN said "none of these
        # five are known to me", which is what the forgotten path acts on.
        self.assertEqual(["j20", "j21", "j22", "j23", "j24"], sorted(out["covered"]),
            "an empty JSON list is a valid answer and must count as covered")
        self.assertEqual([], out["results"])


@unittest.skipUnless(_docker_available(), "requires running onionpress-wordpress container")
class TestWaybackSweepLock(unittest.TestCase):
    """Token-lock mutex semantics for the sweep entry point."""

    @classmethod
    def setUpClass(cls):
        s = _pick_site()
        if s is None:
            raise unittest.SkipTest("no site available")
        cls.url = s["url"].rstrip("/") + "/"

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

    def test_daemon_recycles_and_hands_the_lock_back(self):
        """The daemon must not run forever. It used to: OP_WB_LOOP_MAX_SEC
        appeared in two comments but was never defined, so the loop was
        `while (true)` with only a drained-queue exit. WordPress caches
        options per REQUEST and the daemon is one request, so a process
        alive for 70 hours kept reading job_ids that had been deleted from
        the database — and since a non-empty job_id is what marks the queue
        as having work, the stale read sustained the loop that sustained
        the stale read. Five days, nothing archived.

        What matters is the handoff, not just the exit: the lock must be
        released, or the queue stalls for LOCK_STALE_SEC on every recycle.
        """
        # The filter doubles as the sentinel. `echo 'ran'` after the sweep
        # proves nothing — the mutex can reject the invocation and return
        # immediately, and the echo still fires. Counting entries into the
        # loop body is the only way to observe that the daemon actually
        # started, which is exactly what the second half asserts.
        preamble = """
        add_filter('onionpress_wayback_self_reachable_mock',
                   function() { return true; });
        add_filter('onionpress_wayback_user_status_mock',
                   function() { return array('available' => 0); });
        // Cap of 0 => recycle on the very first iteration, before any
        // network-touching work, and (see below) without a real loopback.
        add_filter('onionpress_wayback_loop_max_sec', function() {
            update_option('op_test_loop_entered',
                          1 + (int) get_option('op_test_loop_entered', 0), false);
            return 0;
        });
        // Belt and braces on the loopback the recycle would otherwise fire.
        // site_url() resolves inside this container, so an unguarded POST
        // to wp-cron.php would start a genuine unmocked daemon out of a
        // unit test — real Tor, real SPN submissions, holding the lock for
        // OP_WB_LOOP_MAX_SEC and breaking every test that follows.
        add_filter('pre_http_request', function($pre, $args, $url) {
            update_option('op_test_http_attempts',
                          1 + (int) get_option('op_test_http_attempts', 0), false);
            return new WP_Error('blocked-by-test', 'no outbound HTTP in tests');
        }, 10, 3);
        """
        php = """
        delete_option('op_test_loop_entered');
        delete_option('op_test_http_attempts');
        """ + preamble + """
        onionpress_wayback_sweep();
        echo json_encode(array(
            'lock'     => (string) get_option('op_wayback_sweep_lock', '(gone)'),
            'entered'  => (int) get_option('op_test_loop_entered', 0),
            'attempts' => (int) get_option('op_test_http_attempts', 0),
        ));
        """
        first = json.loads(_eval(php, self.url))
        self.addCleanup(lambda: _wp(
            ["option", "delete", "op_test_loop_entered"], url=self.url, timeout=15))
        self.addCleanup(lambda: _wp(
            ["option", "delete", "op_test_http_attempts"], url=self.url, timeout=15))

        self.assertEqual(1, first["entered"], "the daemon should have run its loop once")
        self.assertEqual("(gone)", first["lock"],
            "a recycling daemon must delete its lock so the successor can "
            f"claim it immediately rather than waiting it out; got: {first['lock']}")
        # A cap of 0 means every successor recycles on its own first
        # iteration, so firing the handoff there is a restart loop, not a
        # handoff — the production path is gated on a non-zero lifetime.
        self.assertEqual(0, first["attempts"],
            "a zero-lifetime recycle must not fire the loopback")

        # And the successor really can claim it — the property that makes
        # the recycle a handoff instead of a stall. Asserted by the loop
        # entry counter reaching 2, not by the sweep call returning.
        php2 = preamble + """
        onionpress_wayback_sweep();
        echo (int) get_option('op_test_loop_entered', 0);
        """
        self.assertEqual("2", _eval(php2, self.url).strip(),
            "a fresh sweep must be able to start straight after a recycle — "
            "it must reach the loop body, not bounce off a surviving lock")


@unittest.skipUnless(_docker_available(), "requires running onionpress-wordpress container")
class TestWaybackCommentResnapshot(unittest.TestCase):
    """`wp_insert_comment` triggers exactly one re-archive of the parent
    post — and only for imported posts that already have a snapshot.
    Caps the social-importer-threading SPN cost at one extra snapshot
    per parent (instead of one per comment)."""

    @classmethod
    def setUpClass(cls):
        s = _pick_site()
        if s is None:
            raise unittest.SkipTest("no site available")
        cls.url = s["url"].rstrip("/") + "/"

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


@unittest.skipUnless(_docker_available(), "requires running onionpress-wordpress container")
class TestWaybackKickAndInvalidate(unittest.TestCase):
    """`onionpress_wayback_kick_now()` / `_invalidate_sitewide()` are the
    shared mechanism behind every "archive right now" trigger: the
    save_post hook, the admin "kick" button, and (via the static receiver)
    a static-site publish. Covers the mechanism itself, not each caller."""

    @classmethod
    def setUpClass(cls):
        s = _pick_site()
        if s is None:
            raise unittest.SkipTest("no site available")
        cls.url = s["url"].rstrip("/") + "/"

    def setUp(self):
        _eval("delete_option('op_wayback_home_state'); "
              "delete_option('op_wayback_feed_state'); "
              "update_option('op_wayback_backoff_until', time() + 999, false); "
              "wp_clear_scheduled_hook('onionpress_wayback_sweep');",
              self.url)

    def test_kick_now_clears_backoff_and_schedules_sweep(self):
        _eval("onionpress_wayback_kick_now();", self.url)
        backoff = _eval("echo (string) get_option('op_wayback_backoff_until', '');",
                         self.url)
        self.assertEqual(backoff, "", "backoff option should be deleted")
        scheduled = _eval(
            "echo wp_next_scheduled('onionpress_wayback_sweep') ? '1' : '0';",
            self.url)
        self.assertEqual(scheduled, "1", "sweep should be scheduled for immediate run")

    def test_invalidate_sitewide_clears_home_and_feed_when_idle(self):
        _eval("update_option('op_wayback_home_state', array('archived_at'=>'x'), false); "
              "update_option('op_wayback_feed_state', array('archived_at'=>'x'), false);",
              self.url)
        _eval("onionpress_wayback_invalidate_sitewide();", self.url)
        home = _eval("echo (string) get_option('op_wayback_home_state', '');", self.url)
        feed = _eval("echo (string) get_option('op_wayback_feed_state', '');", self.url)
        self.assertEqual(home, "")
        self.assertEqual(feed, "")

    def test_invalidate_sitewide_skips_home_with_job_in_flight(self):
        """A capture already submitted must not be wiped — the in-flight
        SPN job will render the current content anyway; clearing the
        option here would just burn a duplicate submission on the next
        sweep. Mirrors the reasoning in save_post's own comment."""
        _eval("update_option('op_wayback_home_state', "
              "array('job_id'=>'abc123'), false);", self.url)
        _eval("onionpress_wayback_invalidate_sitewide();", self.url)
        home = _eval("echo (string) get_option('op_wayback_home_state', '');", self.url)
        self.assertNotEqual(home, "", "home state with a job in flight must survive")


if __name__ == "__main__":
    unittest.main()

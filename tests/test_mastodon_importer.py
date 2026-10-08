#!/usr/bin/env python3
"""Integration tests for the Mastodon importer plugin.

These drive the live plugin inside the onionpress-wordpress container
via `wp eval`, using the `onionpress_mastodon_fetch_statuses_mock`
filter hook to inject canned API responses — no real Mastodon server
is contacted.

Sandbox safety: tests run against a dedicated subsite (slug
`op-mastodon-test`) that each class creates afresh and deletes when it
finishes. Earlier versions of this file picked the first non-root
subsite, which silently clobbered real user blogs' routing options (see
TestSandboxGuard below).

Coverage focus: regressions we've hit in the field.
  1. Short page (count < PER_PAGE) MUST NOT mark backfill done — the
     bug that stopped a user's backfill at 647/1417.
  2. Truly empty page DOES mark backfill done.
  3. Cycle guard: max_id cursor not advancing for 3 rounds → error,
     no infinite loop.
  4. Dedupe: importing the same source_id twice creates one post.
  5. Reply/boost filters: include_replies/include_boosts correctly
     skip items when disabled.
  6. Token-lock mutex: fresh lock blocks a second daemon; stale lock
     lets a new daemon take over.
  7. Handle/server drift detection (the test-pollution bug that
     poisoned a user's brewsterkahle subsite).
  8. Sandbox guard refuses to run against a subsite with a real
     handle configured.

Opt-in: skipped unless ONIONPRESS_INTEGRATION_TESTS=1, and refused on a
live install — see wp_integration.py. Needs a multisite install with the
plugin from this checkout in mu-plugins/.
"""

import json
import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wp_integration  # noqa: E402

_wp = wp_integration.wp
_eval = wp_integration.wp_eval


_SAFE_TEST_ACCOUNT_ID = "1"
_SAFE_TEST_SERVER     = "example.test"
_SAFE_TEST_HANDLE     = "test@example.test"
_TEST_SUBSITE_SLUG    = "op-mastodon-test"

# Every option key the suite ever writes. Reset between tests so a
# crashed test can't bleed state into the next, and so a fresh checkout
# isn't poisoned by leftovers from a previous run.
_TOUCHED_OPTIONS = (
    "onionpress_social_mastodon_handle",
    "onionpress_social_mastodon_server",
    "onionpress_social_mastodon_username",
    "onionpress_social_mastodon_account_id",
    "onionpress_social_mastodon_oldest_id",
    "onionpress_social_mastodon_oldest_owner",
    "onionpress_social_mastodon_newest_id",
    "onionpress_social_mastodon_newest_owner",
    "onionpress_social_mastodon_total_statuses",
    "onionpress_social_mastodon_last_sync",
    "onionpress_social_mastodon_last_note",
    "onionpress_social_mastodon_daemon_lock",
    "onionpress_social_mastodon_opts",
    "onionpress_mastodon_threads_v1_migrated",
    "op_test_mastodon_mock_idx",
    "op_test_mastodon_mock_pages",
    "op_test_mastodon_status_mocks",
)


class _MastodonSandbox(wp_integration.SandboxTestCase):
    """Every class runs on the dedicated sandbox subsite.

    Earlier versions of this file picked the first non-root subsite —
    fine on a fresh CI install, catastrophic on a real machine where
    that subsite had a configured handle. The test would silently
    overwrite _server / _account_id with sandbox values, and the
    user's Sync would then poll example.test forever. Use a slug
    that's obviously test-only so the same can never happen again.
    """
    SANDBOX_SLUG = _TEST_SUBSITE_SLUG
    SANDBOX_TITLE = "Mastodon Importer Test Sandbox"


def _assert_test_sandbox(url):
    """Refuse to run if any of _server, _account_id, or _handle look
    like real configuration. Belt-and-suspenders against the test ever
    being pointed at a real subsite via a future code change.

    The handle check is the load-bearing one: _server and _account_id
    are written by setUp before this guard would notice them, but
    _handle is only written via the live save path or by a real user.
    A non-empty, non-sandbox handle means we're on someone's blog.
    """
    pairs = (
        ("onionpress_social_mastodon_server",     _SAFE_TEST_SERVER),
        ("onionpress_social_mastodon_account_id", _SAFE_TEST_ACCOUNT_ID),
        ("onionpress_social_mastodon_handle",     _SAFE_TEST_HANDLE),
    )
    for opt, sandbox_value in pairs:
        r = _wp(["option", "get", opt], url=url, timeout=10)
        actual = (r.stdout or "").strip()
        if actual and actual != sandbox_value:
            raise RuntimeError(
                f"Refusing to run Mastodon tests against {url!r}: "
                f"{opt} is {actual!r} (looks real, not sandbox). "
                f"This subsite should never be the test target — only "
                f"{_TEST_SUBSITE_SLUG!r} should be."
            )


def _clear_all_options(url):
    """Wipe every option the suite touches. Run in addCleanup so a
    crashed test can't leave routing state for the next one."""
    for opt in _TOUCHED_OPTIONS:
        _wp(["option", "delete", opt], url=url, timeout=10)
    _wp(["transient", "delete", "onionpress_social_mastodon_lock"],
        url=url, timeout=10)


def _cleanup_test_posts(url):
    """Delete any posts whose content is '<p>hi</p>' (our canned test
    status content) AND which have a _source_id. Tests that import
    statuses create real WP posts; without this they'd accumulate
    forever on the live site."""
    _eval("""
    global $wpdb;
    $ids = $wpdb->get_col(
      "SELECT p.ID FROM {$wpdb->posts} p
       JOIN {$wpdb->postmeta} m ON m.post_id=p.ID AND m.meta_key='_source_id'
       WHERE p.post_content='<p>hi</p>' AND p.post_status='publish'"
    );
    foreach ($ids as $id) { wp_delete_post((int)$id, true); }
    echo count($ids);
    """, url)


def _status(id_str, created="2026-04-23T00:00:00.000Z",
            in_reply_to_id=None, in_reply_to_account_id=None,
            reblog=None, content="<p>hi</p>"):
    """Build a fake Mastodon status object, shape matching the API."""
    return {
        "id": id_str,
        "created_at": created,
        "in_reply_to_id": in_reply_to_id,
        "in_reply_to_account_id": in_reply_to_account_id,
        "reblog": reblog,
        "content": content,
        "account": {"id": "1", "display_name": "test", "username": "test",
                    "url": "https://example.test/@test", "avatar_static": ""},
        "media_attachments": [],
        "tags": [],
    }


def _delete_test_comments(url):
    """Delete any orphan comments left by test runs. Most are killed
    when their post is deleted (wp_delete_post true), but a defensive
    sweep catches any stragglers — match by content '<p>hi</p>' on a
    comment that has a mastodon: _source_id."""
    _eval("""
    global $wpdb;
    $ids = $wpdb->get_col(
      "SELECT c.comment_ID FROM {$wpdb->comments} c
       JOIN {$wpdb->commentmeta} m ON m.comment_id=c.comment_ID
        AND m.meta_key='_source_id' AND m.meta_value LIKE 'mastodon:%'
       WHERE c.comment_content='<p>hi</p>'"
    );
    foreach ($ids as $id) { wp_delete_comment((int)$id, true); }
    echo count($ids);
    """, url)


class TestMastodonImporterBackfill(_MastodonSandbox):
    """Backfill pagination semantics."""

    def setUp(self):
        _assert_test_sandbox(self.url)
        # Fresh cursors + minimal config so the sync has a valid target.
        _wp(["option", "update", "onionpress_social_mastodon_server",
             _SAFE_TEST_SERVER], url=self.url, timeout=15)
        _wp(["option", "update", "onionpress_social_mastodon_account_id",
             _SAFE_TEST_ACCOUNT_ID], url=self.url, timeout=15)
        _wp(["option", "delete", "onionpress_social_mastodon_oldest_id"],
            url=self.url, timeout=15)
        _wp(["option", "delete", "onionpress_social_mastodon_newest_id"],
            url=self.url, timeout=15)
        _wp(["option", "delete", "onionpress_social_mastodon_daemon_lock"],
            url=self.url, timeout=15)
        _wp(["transient", "delete", "onionpress_social_mastodon_lock"],
            url=self.url, timeout=15)
        # sync_one_tick imports whatever the mock returns into real
        # WP posts — clean those up after every test. Wipe options too
        # so a crashed test can't leak state into the next one.
        self.addCleanup(_cleanup_test_posts, self.url)
        self.addCleanup(_clear_all_options, self.url)

    def _register_mock(self, pages_json):
        """Install a PHP filter that returns pre-canned pages in order,
        then empties. Pages are drained one per call. We stash the
        pages JSON into a WP option so the filter closure can
        json_decode it at fire time — avoids PHP/JSON brace collisions
        in string interpolation."""
        pages_escaped = pages_json.replace("'", "\\'")
        return f"""
        delete_option('op_test_mastodon_mock_idx');
        update_option('op_test_mastodon_mock_pages', '{pages_escaped}', false);
        add_filter('onionpress_mastodon_fetch_statuses_mock', function($_, $params) {{
            $pages = json_decode((string) get_option('op_test_mastodon_mock_pages', '[]'), true);
            $idx = (int) get_option('op_test_mastodon_mock_idx', 0);
            update_option('op_test_mastodon_mock_idx', $idx + 1, false);
            return is_array($pages) && isset($pages[$idx]) ? $pages[$idx] : array();
        }}, 10, 2);
        """

    def test_short_page_does_not_mark_done(self):
        """A page with count < PER_PAGE (40) must NOT mark backfill done."""
        # 20-entry page (short), then empty. After processing both,
        # oldest_id should be 'done' only because of the empty page —
        # and the 20 items from the short page must have been iterated.
        page = [_status(str(1000 - i)) for i in range(20)]
        mock = json.dumps([page, []])
        php = self._register_mock(mock) + """
        $r = onionpress_mastodon_sync_one_tick('example.test', '1');
        echo 'done=' . ($r['done'] ? '1' : '0') . ';pages=' . $r['stats']['pages'];
        """
        out = _eval(php, self.url)
        # Expect 2 pages processed (short + empty) and done=1 only
        # because of the EMPTY page.
        self.assertIn("done=1", out)
        self.assertIn("pages=2", out)
        # Regression: before the fix, we'd see pages=1 (short page
        # incorrectly treated as done).

    def test_empty_page_marks_done(self):
        """An immediately-empty page marks backfill done in one step."""
        mock = json.dumps([[]])
        php = self._register_mock(mock) + """
        $r = onionpress_mastodon_sync_one_tick('example.test', '1');
        echo 'done=' . ($r['done'] ? '1' : '0') . ';pages=' . $r['stats']['pages'];
        """
        out = _eval(php, self.url)
        self.assertIn("done=1", out)
        self.assertIn("pages=1", out)

    def test_cycle_guard_stops_after_3_no_progress_rounds(self):
        """Server stuck returning same last-id → error after 3 rounds."""
        # Return the SAME single-item page over and over. max_id cursor
        # won't advance (import_status sees dupe → skipped doesn't
        # change oldest_id... actually oldest_id IS set from the status
        # id regardless of import outcome). So same id means same
        # before_id after: triggers cycle guard.
        stuck_page = [_status("42")]
        mock = json.dumps([stuck_page] * 10)
        php = self._register_mock(mock) + """
        $r = onionpress_mastodon_sync_one_tick('example.test', '1');
        echo 'done=' . ($r['done'] ? '1' : '0')
          . ';pages=' . $r['stats']['pages']
          . ';errs=' . count($r['errors']);
        """
        out = _eval(php, self.url)
        # Should NOT be done (no empty page seen), and errors should
        # have the "cursor stuck" entry after 3 no-progress rounds.
        self.assertIn("done=0", out)
        self.assertIn("errs=1", out)


class TestMastodonImporterStatus(_MastodonSandbox):
    """Per-status filter logic (include_replies / include_boosts / dedupe)."""

    def setUp(self):
        _assert_test_sandbox(self.url)
        self.addCleanup(_cleanup_test_posts, self.url)
        self.addCleanup(_clear_all_options, self.url)

    def _import(self, status, opts):
        # Pass as JSON strings then json_decode inside PHP — direct
        # string interpolation would collide with PHP's {} block syntax.
        s = json.dumps(status).replace("'", "\\'")
        o = json.dumps(opts).replace("'", "\\'")
        php = f"""
        $s = json_decode('{s}', true);
        $o = json_decode('{o}', true);
        $r = onionpress_mastodon_import_status($s, $o);
        echo $r;
        """
        return _eval(php, self.url)

    def test_dedupe(self):
        """Same source_id imported twice → one post created."""
        sid = "mastodon:test-" + uuid.uuid4().hex
        status = _status(sid)
        first = self._import(status, {"include_boosts": False, "include_replies": False})
        second = self._import(status, {"include_boosts": False, "include_replies": False})
        self.assertEqual(first, "imported")
        self.assertEqual(second, "skipped")

    def test_reply_skipped_when_disabled(self):
        status = _status("r-" + uuid.uuid4().hex, in_reply_to_id="99")
        r = self._import(status, {"include_boosts": False, "include_replies": False})
        self.assertEqual(r, "skipped")

    def test_reply_imported_when_enabled(self):
        status = _status("r-" + uuid.uuid4().hex, in_reply_to_id="99")
        r = self._import(status, {"include_boosts": False, "include_replies": True})
        self.assertEqual(r, "imported")

    def test_boost_skipped_when_disabled(self):
        inner = _status("b-inner-" + uuid.uuid4().hex)
        boost = _status("b-outer-" + uuid.uuid4().hex, reblog=inner)
        r = self._import(boost, {"include_boosts": False, "include_replies": False})
        self.assertEqual(r, "skipped")


class TestMastodonDaemonLock(_MastodonSandbox):
    """Token-lock mutex semantics for the daemon entry point."""

    def setUp(self):
        _assert_test_sandbox(self.url)
        _wp(["option", "update", "onionpress_social_mastodon_server",
             _SAFE_TEST_SERVER], url=self.url, timeout=15)
        _wp(["option", "update", "onionpress_social_mastodon_account_id",
             _SAFE_TEST_ACCOUNT_ID], url=self.url, timeout=15)
        _wp(["option", "delete", "onionpress_social_mastodon_daemon_lock"],
            url=self.url, timeout=15)
        self.addCleanup(_clear_all_options, self.url)

    def test_fresh_lock_blocks_second_run(self):
        """A fresh lock < STALE threshold rejects a new run."""
        # Seed a fresh lock that belongs to a fake other token.
        php_seed = """
        update_option('onionpress_social_mastodon_daemon_lock',
                      'otherTok:' . time(), false);
        echo 'seeded';
        """
        _eval(php_seed, self.url)
        # Now call the entry. from_admin=true so we get a notice back.
        # Should return a "already running" warning and NOT change the lock.
        php = """
        add_filter('onionpress_mastodon_fetch_statuses_mock',
                   function($_, $params){ return []; }, 10, 2);
        $r = onionpress_mastodon_run_sync_tick(true);
        $lock = get_option('onionpress_social_mastodon_daemon_lock');
        echo 'level=' . ($r['level'] ?? '?') . ';lock=' . $lock;
        """
        out = _eval(php, self.url)
        self.assertIn("level=warning", out)
        self.assertTrue(out.startswith("level=warning;lock=otherTok:"),
                        f"lock should not have changed owner: {out}")

    def test_stale_lock_is_taken_over(self):
        """A lock older than STALE_SEC is replaced by the new daemon."""
        # Seed a stale lock (heartbeat 9999s old).
        php_seed = """
        update_option('onionpress_social_mastodon_daemon_lock',
                      'deadTok:' . (time() - 9999), false);
        echo 'seeded';
        """
        _eval(php_seed, self.url)
        php = """
        // Mock: return empty so the daemon exits immediately after
        // one iteration. We only care about lock acquisition semantics.
        add_filter('onionpress_mastodon_fetch_statuses_mock',
                   function($_, $params){ return []; }, 10, 2);
        onionpress_mastodon_run_sync_tick(true);
        $lock = (string) get_option('onionpress_social_mastodon_daemon_lock', '(empty)');
        echo 'lock=' . $lock;
        """
        out = _eval(php, self.url)
        # After a clean run, the lock should be deleted (finally {}).
        # If any cleanup path leaves it, confirm it's at least not
        # the stale 'deadTok' value — a new token took over.
        self.assertNotIn("deadTok", out)


class TestMastodonThreading(_MastodonSandbox):
    """Self-replies should fold into the parent post's comment thread,
    not flood the category archive as fragmentary top-level posts."""

    def setUp(self):
        _assert_test_sandbox(self.url)
        # Threading branches on account_id (self-reply detection) — pin
        # to the sandbox value so the test status objects' fake
        # in_reply_to_account_id="1" matches.
        _wp(["option", "update", "onionpress_social_mastodon_account_id",
             _SAFE_TEST_ACCOUNT_ID], url=self.url, timeout=15)
        # Migration option must be cleared between tests so each
        # migration test sees a clean run.
        _wp(["option", "delete", "onionpress_mastodon_threads_v1_migrated"],
            url=self.url, timeout=15)
        self.addCleanup(_cleanup_test_posts, self.url)
        self.addCleanup(_delete_test_comments, self.url)
        self.addCleanup(_clear_all_options, self.url)

    def _import(self, status, opts=None):
        if opts is None:
            opts = {"include_boosts": False, "include_replies": True}
        s = json.dumps(status).replace("'", "\\'")
        o = json.dumps(opts).replace("'", "\\'")
        return _eval(f"""
        $s = json_decode('{s}', true);
        $o = json_decode('{o}', true);
        echo onionpress_mastodon_import_status($s, $o);
        """, self.url)

    def _comments_for(self, source_id):
        """Return list of (comment_id, comment_parent) for a given source_id."""
        return _eval(f"""
        $cs = get_comments(array(
            'meta_key' => '_source_id',
            'meta_value' => 'mastodon:{source_id}',
            'orderby' => 'comment_ID',
        ));
        $out = array();
        foreach ($cs as $c) {{
            $out[] = $c->comment_ID . ':' . $c->comment_parent;
        }}
        echo implode(',', $out);
        """, self.url)

    def _post_count(self, source_id):
        return _eval(f"""
        $ps = get_posts(array(
            'post_type' => 'post',
            'meta_key' => '_source_id',
            'meta_value' => 'mastodon:{source_id}',
            'post_status' => 'any',
            'posts_per_page' => -1,
            'fields' => 'ids',
        ));
        echo count($ps);
        """, self.url)

    def _pending_count(self):
        return int(_eval("""
        $ps = get_posts(array(
            'post_type' => 'post',
            'meta_key' => '_pending_reattach',
            'meta_value' => '1',
            'post_status' => 'any',
            'posts_per_page' => -1,
            'fields' => 'ids',
        ));
        echo count($ps);
        """, self.url))

    def test_self_reply_with_present_parent_becomes_comment(self):
        """The parent toot is imported, then a self-reply to it. The
        reply should land as a comment on the parent's post, NOT a
        new top-level post."""
        parent_id = "p-" + uuid.uuid4().hex
        reply_id  = "r-" + uuid.uuid4().hex
        self.assertEqual(self._import(_status(parent_id)), "imported")
        reply = _status(reply_id, in_reply_to_id=parent_id, in_reply_to_account_id="1")
        self.assertEqual(self._import(reply), "imported")
        # Parent post exists; reply has NO post.
        self.assertEqual(self._post_count(parent_id), "1")
        self.assertEqual(self._post_count(reply_id), "0")
        # Reply has one comment, comment_parent=0 (top-level under post).
        comments = self._comments_for(reply_id)
        self.assertNotEqual(comments, "", "reply should have one comment row")
        cid, cparent = comments.split(":")
        self.assertEqual(cparent, "0")

    def test_self_reply_without_parent_marks_pending(self):
        """A self-reply whose parent isn't here yet (typical during
        backward backfill) lands as a top-level post flagged
        _pending_reattach=1, awaiting the end-of-tick sweep."""
        reply_id = "r-" + uuid.uuid4().hex
        reply = _status(reply_id, in_reply_to_id="9999",
                        in_reply_to_account_id="1")
        self.assertEqual(self._import(reply), "imported")
        self.assertEqual(self._post_count(reply_id), "1")
        self.assertEqual(self._pending_count(), 1)

    def test_reattach_sweep_converts_pending_after_parent_arrives(self):
        """Backfill order: reply imported first as pending post; parent
        arrives later; the sweep then converts the pending post into a
        comment on the parent and deletes the placeholder."""
        parent_id = "p-" + uuid.uuid4().hex
        reply_id  = "r-" + uuid.uuid4().hex
        # Reply arrives first → pending.
        self.assertEqual(
            self._import(_status(reply_id, in_reply_to_id=parent_id,
                                 in_reply_to_account_id="1")),
            "imported",
        )
        self.assertEqual(self._pending_count(), 1)
        # Parent arrives.
        self.assertEqual(self._import(_status(parent_id)), "imported")
        # Sweep.
        n = int(_eval("echo onionpress_mastodon_reattach_pending();", self.url))
        self.assertEqual(n, 1)
        # Pending post is gone, comment exists on parent.
        self.assertEqual(self._pending_count(), 0)
        self.assertEqual(self._post_count(reply_id), "0")
        self.assertNotEqual(self._comments_for(reply_id), "")

    def test_reply_with_unfetchable_parent_falls_back_to_pending(self):
        """A reply whose parent can't be fetched (no API mock here, and
        the live API would 404 in a unit-test container) must still
        import — as a top-level post flagged _pending_reattach=1, the
        same fallback used when self-reply backfill outpaces parents.
        Threading kicks in later if the parent ever shows up."""
        reply_id = "r-" + uuid.uuid4().hex
        reply = _status(reply_id, in_reply_to_id="external-toot",
                        in_reply_to_account_id="999")
        self.assertEqual(self._import(reply), "imported")
        self.assertEqual(self._post_count(reply_id), "1")
        self.assertEqual(self._pending_count(), 1,
                         "context fetch failed → flagged for later reattach")
        self.assertEqual(self._comments_for(reply_id), "",
                         "still top-level (not yet threaded under any parent)")

    def test_self_reply_dedup_on_re_import(self):
        """Re-importing the same self-reply must not create a duplicate
        comment (idempotency on _source_id commentmeta)."""
        parent_id = "p-" + uuid.uuid4().hex
        reply_id  = "r-" + uuid.uuid4().hex
        self._import(_status(parent_id))
        reply = _status(reply_id, in_reply_to_id=parent_id, in_reply_to_account_id="1")
        self.assertEqual(self._import(reply), "imported")
        self.assertEqual(self._import(reply), "skipped")
        # Exactly one comment row.
        self.assertEqual(len(self._comments_for(reply_id).split(",")), 1)

    def test_nested_self_reply_chain(self):
        """A → B (self-reply to A) → C (self-reply to B). C's comment
        should be parented to B's comment (not the post root) so the
        thread renders as a tree."""
        a = "a-" + uuid.uuid4().hex
        b = "b-" + uuid.uuid4().hex
        c = "c-" + uuid.uuid4().hex
        self._import(_status(a))
        self._import(_status(b, in_reply_to_id=a, in_reply_to_account_id="1"))
        self._import(_status(c, in_reply_to_id=b, in_reply_to_account_id="1"))
        b_comments = self._comments_for(b)
        c_comments = self._comments_for(c)
        self.assertNotEqual(b_comments, "")
        self.assertNotEqual(c_comments, "")
        b_id, b_parent = b_comments.split(":")
        c_id, c_parent = c_comments.split(":")
        self.assertEqual(b_parent, "0",
                         "B's comment hangs off the post (no parent comment)")
        self.assertEqual(c_parent, b_id,
                         "C's comment must nest under B's comment")

    def test_migration_converts_existing_self_reply_posts(self):
        """A site that imported self-replies as top-level posts before
        threading was wired up. Migration converts them to comments
        and sets the migrated flag."""
        parent_id = "mig-p-" + uuid.uuid4().hex
        reply_id  = "mig-r-" + uuid.uuid4().hex
        # Seed parent the normal way.
        self._import(_status(parent_id))
        # Manually insert a legacy-shaped self-reply post (no
        # _pending_reattach flag — simulating a pre-threading import).
        raw_status = _status(reply_id, in_reply_to_id=parent_id,
                             in_reply_to_account_id="1")
        raw_json = json.dumps(raw_status).replace("'", "\\'")
        legacy_pid = int(_eval(f"""
        $pid = wp_insert_post(array(
            'post_type' => 'post',
            'post_status' => 'publish',
            'post_title' => 'legacy reply',
            'post_content' => '<p>hi</p>',
            'meta_input' => array(
                '_source_id' => 'mastodon:{reply_id}',
                '_is_reply' => '1',
                '_reply_to_id' => '{parent_id}',
                '_raw' => '{raw_json}',
            ),
        ));
        echo (int) $pid;
        """, self.url))
        self.assertGreater(legacy_pid, 0)
        # Run migration.
        n = int(_eval(
            "echo onionpress_mastodon_migrate_self_replies_to_comments();",
            self.url,
        ))
        self.assertEqual(n, 1)
        # Legacy post deleted, comment exists, migration flag set.
        self.assertEqual(self._post_count(reply_id), "0")
        self.assertNotEqual(self._comments_for(reply_id), "")
        self.assertEqual(
            _eval("echo get_option('onionpress_mastodon_threads_v1_migrated', '');",
                  self.url),
            "yes",
        )
        # Second call is a no-op (flag short-circuit).
        n2 = int(_eval(
            "echo onionpress_mastodon_migrate_self_replies_to_comments();",
            self.url,
        ))
        self.assertEqual(n2, 0)

    def test_migration_handles_legacy_raw_without_account_id(self):
        """Old imports stored `_raw` without `in_reply_to_account_id`.
        The migration must still convert them — the importer only ever
        pulls our own account's statuses, so a parent post existing in
        our DB IS proof the reply is part of our own thread."""
        # Reset migration flag (setUp clears it, but be explicit).
        _wp(["option", "delete", "onionpress_mastodon_threads_v1_migrated"],
            url=self.url, timeout=15)
        parent_id = "old-p-" + uuid.uuid4().hex
        reply_id  = "old-r-" + uuid.uuid4().hex
        self._import(_status(parent_id))
        # Legacy-shaped raw: no in_reply_to_account_id field at all.
        legacy_raw = {
            "id": reply_id,
            "created_at": "2022-11-15T12:00:00.000Z",
            "in_reply_to_id": None,  # also legacy: nulled even though we know it's a reply
            "content": "<p>hi</p>",
            "account": {"id": "1", "display_name": "test", "username": "test",
                        "url": "https://example.test/@test", "avatar_static": ""},
            "media_attachments": [],
            "tags": [],
        }
        raw_json = json.dumps(legacy_raw).replace("'", "\\'")
        legacy_pid = int(_eval(f"""
        $pid = wp_insert_post(array(
            'post_type' => 'post',
            'post_status' => 'publish',
            'post_title' => 'legacy-old-shape reply',
            'post_content' => '<p>hi</p>',
            'meta_input' => array(
                '_source_id' => 'mastodon:{reply_id}',
                '_is_reply' => '1',
                '_reply_to_id' => '{parent_id}',
                '_raw' => '{raw_json}',
            ),
        ));
        echo (int) $pid;
        """, self.url))
        self.assertGreater(legacy_pid, 0)
        n = int(_eval(
            "echo onionpress_mastodon_migrate_self_replies_to_comments();",
            self.url,
        ))
        self.assertEqual(n, 1, "must convert even when _raw lacks in_reply_to_account_id")
        self.assertEqual(self._post_count(reply_id), "0")
        self.assertNotEqual(self._comments_for(reply_id), "")

    def test_migration_handles_broken_raw_json(self):
        """Real-world case: older imports stored `_raw` with HTML attribute
        quotes left unescaped (`<a href=\"…\">` inside a JSON string),
        making `_raw` un-decodeable. The migration must still convert these
        posts using post_content + postmeta directly."""
        _wp(["option", "delete", "onionpress_mastodon_threads_v1_migrated"],
            url=self.url, timeout=15)
        # Set the username option so the comment author isn't 'me'.
        _wp(["option", "update", "onionpress_social_mastodon_username",
             "test"], url=self.url, timeout=15)
        parent_id = "br-p-" + uuid.uuid4().hex
        reply_id  = "br-r-" + uuid.uuid4().hex
        self._import(_status(parent_id))
        # Inject a legacy reply post with deliberately-broken _raw —
        # unescaped quotes inside the content field that break json_decode.
        broken_raw = '{"id":"' + reply_id + '","content":"<a href="https://x">link</a>"}'
        broken_escaped = broken_raw.replace("'", "\\'").replace("\\", "\\\\").replace('"', '\\"')
        legacy_pid = int(_eval(f"""
        $pid = wp_insert_post(array(
            'post_type' => 'post', 'post_status' => 'publish',
            'post_title' => 'broken raw reply', 'post_content' => '<p>hi</p>',
            'meta_input' => array(
                '_source_id'   => 'mastodon:{reply_id}',
                '_is_reply'    => '1',
                '_reply_to_id' => '{parent_id}',
                '_source_url'  => 'https://example.test/@test/{reply_id}',
                '_raw'         => "{broken_escaped}",
            ),
        ));
        echo (int) $pid;
        """, self.url))
        self.assertGreater(legacy_pid, 0)
        # Sanity: confirm _raw really is un-decodeable.
        decode_test = _eval(f"""
        $r = (string) get_post_meta({legacy_pid}, '_raw', true);
        $j = json_decode($r, true);
        echo is_array($j) ? 'decodable' : 'broken';
        """, self.url)
        self.assertEqual(decode_test, "broken",
                         "test setup must produce un-decodeable _raw")
        # Migration converts despite broken _raw.
        n = int(_eval(
            "echo onionpress_mastodon_migrate_self_replies_to_comments();",
            self.url,
        ))
        self.assertEqual(n, 1)
        self.assertEqual(self._post_count(reply_id), "0")
        self.assertNotEqual(self._comments_for(reply_id), "")

    def test_migration_skips_explicit_other_account(self):
        """If `_raw` HAS in_reply_to_account_id and it's NOT us, the
        post is a reply to someone else and must NOT be converted to
        a comment under one of our own posts."""
        _wp(["option", "delete", "onionpress_mastodon_threads_v1_migrated"],
            url=self.url, timeout=15)
        parent_id = "p-" + uuid.uuid4().hex
        reply_id  = "r-" + uuid.uuid4().hex
        self._import(_status(parent_id))
        other_status = _status(reply_id, in_reply_to_id=parent_id,
                               in_reply_to_account_id="999")  # not us
        raw_json = json.dumps(other_status).replace("'", "\\'")
        legacy_pid = int(_eval(f"""
        $pid = wp_insert_post(array(
            'post_type' => 'post', 'post_status' => 'publish',
            'post_title' => 't', 'post_content' => '<p>hi</p>',
            'meta_input' => array(
                '_source_id' => 'mastodon:{reply_id}',
                '_is_reply' => '1',
                '_reply_to_id' => '{parent_id}',
                '_raw' => '{raw_json}',
            ),
        ));
        echo (int) $pid;
        """, self.url))
        self.assertGreater(legacy_pid, 0)
        n = int(_eval(
            "echo onionpress_mastodon_migrate_self_replies_to_comments();",
            self.url,
        ))
        # Other-account reply should NOT have been converted.
        self.assertEqual(self._post_count(reply_id), "1")
        self.assertEqual(self._comments_for(reply_id), "")


class TestMastodonContextFetch(_MastodonSandbox):
    """When we reply to someone else's toot, the parent (and ancestor
    chain back to one of our own toots, or up to the depth cap) is
    fetched from Mastodon and imported as context — so the
    conversation reads in full instead of as a fragmentary reply."""

    def setUp(self):
        _assert_test_sandbox(self.url)
        _wp(["option", "update", "onionpress_social_mastodon_account_id",
             _SAFE_TEST_ACCOUNT_ID], url=self.url, timeout=15)
        _wp(["option", "update", "onionpress_social_mastodon_server",
             _SAFE_TEST_SERVER], url=self.url, timeout=15)
        self.addCleanup(_cleanup_test_posts, self.url)
        self.addCleanup(_delete_test_comments, self.url)
        self.addCleanup(_clear_all_options, self.url)

    def _register_status_mock(self, statuses_by_id):
        """Install a PHP filter that returns a specific status for any
        single-status fetch. Tests stash the dict in a wp_option so the
        closure can read it without PHP/JSON brace-escaping pain."""
        by_id_json = json.dumps(statuses_by_id).replace("'", "\\'")
        return f"""
        update_option('op_test_mastodon_status_mocks', '{by_id_json}', false);
        add_filter('onionpress_mastodon_fetch_status_mock', function($_, $sid) {{
            $by_id = json_decode((string) get_option('op_test_mastodon_status_mocks', '{{}}'), true);
            return is_array($by_id) && isset($by_id[$sid]) ? $by_id[$sid] : null;
        }}, 10, 2);
        """

    def _import(self, status, opts=None, mock_setup=""):
        if opts is None:
            opts = {"include_boosts": False, "include_replies": True}
        s = json.dumps(status).replace("'", "\\'")
        o = json.dumps(opts).replace("'", "\\'")
        php = mock_setup + f"""
        $s = json_decode('{s}', true);
        $o = json_decode('{o}', true);
        echo onionpress_mastodon_import_status($s, $o);
        """
        return _eval(php, self.url)

    def _meta(self, source_id, key):
        return _eval(f"""
        $ps = get_posts(array(
            'post_type' => 'post',
            'meta_key' => '_source_id',
            'meta_value' => 'mastodon:{source_id}',
            'post_status' => 'any',
            'posts_per_page' => 1,
            'fields' => 'ids',
        ));
        if (empty($ps)) {{ echo '(no post)'; return; }}
        echo (string) get_post_meta((int)$ps[0], '{key}', true);
        """, self.url)

    def _comment_post_id(self, source_id):
        """Return the comment_post_ID of the comment with this source_id,
        or '0' if no such comment."""
        return _eval(f"""
        $cs = get_comments(array(
            'meta_key' => '_source_id',
            'meta_value' => 'mastodon:{source_id}',
            'number' => 1,
        ));
        echo empty($cs) ? '0' : (int) $cs[0]->comment_post_ID;
        """, self.url)

    def _other_account_status(self, id_str, in_reply_to_id=None,
                              in_reply_to_account_id=None,
                              foreign_acct_id="999"):
        """Build a status from a NON-test account id (foreign user)."""
        s = _status(id_str, in_reply_to_id=in_reply_to_id,
                    in_reply_to_account_id=in_reply_to_account_id)
        s["account"]["id"] = foreign_acct_id
        s["account"]["username"] = "egonw"
        s["account"]["display_name"] = "Egon"
        return s

    def test_external_reply_fetches_parent_as_context(self):
        """We reply to @egonw's toot. The parent isn't in our DB.
        Importer fetches @egonw's toot via the API mock, marks it
        _is_context=1, and threads our reply under it."""
        ours_id   = "ours-" + uuid.uuid4().hex
        egonw_id  = "egonw-" + uuid.uuid4().hex
        # @egonw's toot — what gets fetched from Mastodon when we reply.
        egonw_status = self._other_account_status(egonw_id)
        mock = self._register_status_mock({egonw_id: egonw_status})
        # Now import OUR reply to @egonw's toot.
        our_reply = _status(ours_id, in_reply_to_id=egonw_id,
                            in_reply_to_account_id="999")  # not us
        result = self._import(our_reply, mock_setup=mock)
        self.assertEqual(result, "imported")
        # @egonw's parent toot is now in our DB as a top-level post.
        self.assertEqual(self._meta(egonw_id, "_source_id"),
                         f"mastodon:{egonw_id}")
        # Marked as context.
        self.assertEqual(self._meta(egonw_id, "_is_context"), "1")
        # Our reply landed as a comment on egonw's post.
        self.assertNotEqual(self._comment_post_id(ours_id), "0")
        self.assertEqual(self._meta(ours_id, "_source_id"), "(no post)",
                         "our reply should be a comment, not a top-level post")

    def test_external_reply_with_self_grandparent_threads_under_us(self):
        """The egonw scenario from the field: our reply → @egonw's
        reply → our original. Walking up the ancestor chain finds our
        original, imports @egonw's middle hop as context (folded as a
        comment under our original), and threads our reply as a deeper
        nested comment. Whole conversation hangs off our toot."""
        our_root_id  = "root-" + uuid.uuid4().hex   # already in DB
        egonw_id     = "egonw-" + uuid.uuid4().hex  # to be fetched
        our_reply_id = "rep-"   + uuid.uuid4().hex
        # Seed our original (no mock needed).
        self.assertEqual(self._import(_status(our_root_id)), "imported")
        # @egonw replies to our root — fetched on demand.
        egonw_status = self._other_account_status(egonw_id, in_reply_to_id=our_root_id,
                                                  in_reply_to_account_id="1")
        mock = self._register_status_mock({egonw_id: egonw_status})
        # Our reply to @egonw.
        our_reply = _status(our_reply_id, in_reply_to_id=egonw_id,
                            in_reply_to_account_id="999")
        self.assertEqual(self._import(our_reply, mock_setup=mock), "imported")
        # @egonw's middle hop became a comment on our root.
        egonw_post_id = self._comment_post_id(egonw_id)
        self.assertNotEqual(egonw_post_id, "0",
                            "@egonw's reply should thread under our root")
        # And our reply nests under it (same root post).
        self.assertEqual(self._comment_post_id(our_reply_id), egonw_post_id,
                         "our reply threads under the same root post as @egonw's middle hop")

    def test_failed_parent_fetch_falls_back_to_pending(self):
        """If the API mock returns nothing for the parent (e.g. parent
        deleted on the source server), the reply lands as a top-level
        post with _pending_reattach=1 — same fallback as before."""
        reply_id = "r-" + uuid.uuid4().hex
        # No mock entries → fetch returns null.
        mock = self._register_status_mock({})
        our_reply = _status(reply_id, in_reply_to_id="9999",
                            in_reply_to_account_id="999")
        self.assertEqual(self._import(our_reply, mock_setup=mock), "imported")
        # Top-level, pending_reattach.
        self.assertEqual(self._meta(reply_id, "_pending_reattach"), "1")

    def test_context_flag_not_set_for_our_own_toots(self):
        """When walking ancestors, a toot whose account.id matches
        ours is imported normally (not flagged _is_context). Used by
        the egonw-with-self-grandparent path to avoid mis-marking our
        own ancestors."""
        ours_id   = "ours-" + uuid.uuid4().hex
        # ours_id has account.id="1" (the test sandbox account).
        ours_status = _status(ours_id)
        mock = self._register_status_mock({ours_id: ours_status})
        # Import via ensure (simulates ancestor walk).
        _eval(mock + f"""
        $opts = array('include_replies' => true);
        echo onionpress_mastodon_ensure_imported_with_ancestry('{ours_id}', $opts);
        """, self.url)
        self.assertEqual(self._meta(ours_id, "_is_context"), "0",
                         "our own toot should not be flagged _is_context")

    def test_depth_cap_stops_unbounded_chain(self):
        """A pathological chain (each toot replies to another) must
        stop fetching at CONTEXT_DEPTH_MAX. Build a chain of 20 →
        only the first ~6 should land in our DB."""
        ids = ["d-" + uuid.uuid4().hex + "-" + str(i) for i in range(20)]
        # Each toot replies to the previous one, all foreign.
        statuses = {}
        for i, sid in enumerate(ids):
            parent = ids[i - 1] if i > 0 else None
            statuses[sid] = self._other_account_status(
                sid, in_reply_to_id=parent, in_reply_to_account_id="999"
            )
        mock = self._register_status_mock(statuses)
        # Trigger ancestor walk starting from the LAST (deepest) toot.
        _eval(mock + f"""
        $opts = array('include_replies' => true);
        echo onionpress_mastodon_ensure_imported_with_ancestry('{ids[-1]}', $opts);
        """, self.url)
        # Count how many of the chain made it in.
        in_db = int(_eval(f"""
        $ids = {json.dumps(ids)};
        $count = 0;
        foreach ($ids as $sid) {{
            $ps = get_posts(array(
                'post_type' => 'post',
                'meta_key' => '_source_id',
                'meta_value' => 'mastodon:' . $sid,
                'posts_per_page' => 1,
                'fields' => 'ids',
            ));
            if (!empty($ps)) $count++;
        }}
        echo $count;
        """, self.url))
        # Should be at most CONTEXT_DEPTH_MAX+1 (the starting status counts as depth 0).
        self.assertLessEqual(in_db, 7,
                             f"depth cap should bound chain — got {in_db}")
        self.assertGreater(in_db, 0,
                           "at least the starting status should be in DB")

    def test_backfill_context_threads_existing_replies(self):
        """An existing reply post with parent missing from our DB.
        The backfill sweep finds it, fetches the parent via API, and
        re-threads the reply as a comment."""
        our_root_id  = "br-root-" + uuid.uuid4().hex
        foreign_id   = "br-fp-"   + uuid.uuid4().hex
        our_reply_id = "br-rep-"  + uuid.uuid4().hex
        # Seed our root.
        self._import(_status(our_root_id))
        # Pre-seed our reply as a legacy top-level post (simulating
        # the state of brewsterkahle's existing data — _is_reply=1
        # but parent not in DB).
        legacy_pid = int(_eval(f"""
        $pid = wp_insert_post(array(
            'post_type' => 'post',
            'post_status' => 'publish',
            'post_title' => 'legacy reply',
            'post_content' => '<p>hi</p>',
            'meta_input' => array(
                '_source_id'   => 'mastodon:{our_reply_id}',
                '_is_reply'    => '1',
                '_reply_to_id' => '{foreign_id}',
                '_source_url'  => 'https://example.test/x',
            ),
        ));
        echo (int) $pid;
        """, self.url))
        self.assertGreater(legacy_pid, 0)
        # Foreign middle-hop available via API mock; replies to our root.
        foreign_status = self._other_account_status(
            foreign_id, in_reply_to_id=our_root_id, in_reply_to_account_id="1"
        )
        mock = self._register_status_mock({foreign_id: foreign_status})
        # Run backfill_context.
        n = int(_eval(mock + "echo onionpress_mastodon_backfill_context();", self.url))
        self.assertGreaterEqual(n, 1)
        # Original legacy post is gone, our reply is now a comment.
        self.assertEqual(self._meta(our_reply_id, "_source_id"), "(no post)")
        self.assertNotEqual(self._comment_post_id(our_reply_id), "0")


class TestHandleServerHelper(_MastodonSandbox):
    """`onionpress_mastodon_handle_server()` parses the server out of a stored
    handle. The admin UI uses it to flag drift between the visible handle and
    the routing options — the symptom that bit a user's brewsterkahle subsite
    after tests poisoned _server / _account_id."""

    def _call(self, handle):
        h = json.dumps(handle).replace("'", "\\'")
        return _eval(
            f"echo onionpress_mastodon_handle_server(json_decode('{h}', true));",
            self.url,
        )

    def test_parses_bare_handle(self):
        self.assertEqual(self._call("user@example.test"), "example.test")

    def test_parses_leading_at(self):
        self.assertEqual(self._call("@user@example.test"), "example.test")

    def test_lowercases(self):
        self.assertEqual(self._call("User@Mastodon.Archive.Org"), "mastodon.archive.org")

    def test_empty_returns_empty(self):
        self.assertEqual(self._call(""), "")

    def test_unparseable_returns_empty(self):
        # No @ separator → no server portion to extract.
        self.assertEqual(self._call("just-a-name"), "")


class TestSandboxGuard(_MastodonSandbox):
    """The sandbox guard is what prevents tests from poisoning a real subsite.
    It must reject any of _server / _account_id / _handle that look real, even
    if the others are sandbox values — the bug we're fixing was exactly this:
    _handle was real, _server and _account_id were sandbox, and the old guard
    only checked the latter two."""

    def setUp(self):
        # We poke options directly to simulate a poisoned subsite, then
        # verify the guard refuses. Make sure cleanup runs even if a test
        # fails mid-way through writing.
        self.addCleanup(_clear_all_options, self.url)

    def test_real_handle_blocks_run(self):
        _wp(["option", "update", "onionpress_social_mastodon_handle",
             "brewster@mastodon.archive.org"], url=self.url, timeout=15)
        with self.assertRaises(RuntimeError) as ctx:
            _assert_test_sandbox(self.url)
        self.assertIn("onionpress_social_mastodon_handle", str(ctx.exception))

    def test_real_server_blocks_run(self):
        _wp(["option", "update", "onionpress_social_mastodon_server",
             "mastodon.archive.org"], url=self.url, timeout=15)
        with self.assertRaises(RuntimeError):
            _assert_test_sandbox(self.url)

    def test_real_account_id_blocks_run(self):
        # Real Mastodon IDs are large numbers; "9999999" is plausibly real
        # whereas "1" is the sandbox value.
        _wp(["option", "update", "onionpress_social_mastodon_account_id",
             "9999999"], url=self.url, timeout=15)
        with self.assertRaises(RuntimeError):
            _assert_test_sandbox(self.url)

    def test_sandbox_values_pass(self):
        _wp(["option", "update", "onionpress_social_mastodon_handle",
             _SAFE_TEST_HANDLE], url=self.url, timeout=15)
        _wp(["option", "update", "onionpress_social_mastodon_server",
             _SAFE_TEST_SERVER], url=self.url, timeout=15)
        _wp(["option", "update", "onionpress_social_mastodon_account_id",
             _SAFE_TEST_ACCOUNT_ID], url=self.url, timeout=15)
        # Should not raise.
        _assert_test_sandbox(self.url)

    def test_all_empty_passes(self):
        # Pristine subsite with no Mastodon config should be allowed.
        _assert_test_sandbox(self.url)


if __name__ == "__main__":
    unittest.main()

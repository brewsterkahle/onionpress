#!/usr/bin/env python3
"""Integration tests for the Twitter / X importer plugin.

Drive the live plugin inside the onionpress-wordpress container via
`wp eval`. Twitter is a one-shot ZIP import (no live API), so tests
exercise import_tweet() directly with synthetic tweet objects rather
than mocking HTTP — same shape the archive parser would hand the
import function. Threading is what's exercised most: self-replies
fold into the parent post's comment thread, and the migration
re-threads pre-threading installs.

Sandbox safety: a dedicated test subsite (`op-twitter-test`) is
created afresh for each class and deleted when it finishes. Tests
refuse to run if any other Twitter options on the subsite look like
real config — same load-bearing guard as the Mastodon and Bluesky test
files.

Opt-in: skipped unless ONIONPRESS_INTEGRATION_TESTS=1, and refused on a
live install — see wp_integration.py.
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


_SAFE_TEST_HANDLE  = "test_twitter_user"
_SAFE_TEST_USER_ID = "12345"
_TEST_SUBSITE_SLUG = "op-twitter-test"

_TOUCHED_OPTIONS = (
    "onionpress_social_twitter_handle",
    "onionpress_social_twitter_self_user_id",
    "onionpress_twitter_threads_v1_migrated",
)


class _TwitterSandbox(wp_integration.SandboxTestCase):
    SANDBOX_SLUG = _TEST_SUBSITE_SLUG
    SANDBOX_TITLE = "Twitter Importer Test Sandbox"


def _assert_test_sandbox(url):
    """Refuse to run if _handle / _self_user_id look real."""
    pairs = (
        ("onionpress_social_twitter_handle",       _SAFE_TEST_HANDLE),
        ("onionpress_social_twitter_self_user_id", _SAFE_TEST_USER_ID),
    )
    for opt, sandbox_value in pairs:
        r = _wp(["option", "get", opt], url=url, timeout=10)
        actual = (r.stdout or "").strip()
        if actual and actual != sandbox_value:
            raise RuntimeError(
                f"Refusing to run Twitter tests against {url!r}: "
                f"{opt} is {actual!r} (looks real, not sandbox)."
            )


def _clear_all_options(url):
    for opt in _TOUCHED_OPTIONS:
        _wp(["option", "delete", opt], url=url, timeout=10)


def _cleanup_test_posts(url):
    """Delete tweet posts created by tests — match by content '<p>hi</p>'
    on a post with a twitter: _source_id."""
    _eval("""
    global $wpdb;
    $ids = $wpdb->get_col(
      "SELECT p.ID FROM {$wpdb->posts} p
       JOIN {$wpdb->postmeta} m ON m.post_id=p.ID AND m.meta_key='_source_id'
       WHERE p.post_content LIKE '%hi%' AND m.meta_value LIKE 'twitter:%'"
    );
    foreach ($ids as $id) { wp_delete_post((int)$id, true); }
    echo count($ids);
    """, url)


def _delete_test_comments(url):
    _eval("""
    global $wpdb;
    $ids = $wpdb->get_col(
      "SELECT c.comment_ID FROM {$wpdb->comments} c
       JOIN {$wpdb->commentmeta} m ON m.comment_id=c.comment_ID
        AND m.meta_key='_source_id' AND m.meta_value LIKE 'twitter:%'"
    );
    foreach ($ids as $id) { wp_delete_comment((int)$id, true); }
    echo count($ids);
    """, url)


def _tweet(id_str, created="Wed Apr 23 00:00:00 +0000 2026",
           in_reply_to_status_id=None, in_reply_to_user_id=None,
           text="hi"):
    """Build a tweet shaped like the archive's tweets.js entries."""
    t = {
        "id_str": id_str,
        "id": id_str,
        "created_at": created,
        "full_text": text,
        "entities": {"hashtags": [], "user_mentions": [], "urls": []},
    }
    if in_reply_to_status_id is not None:
        t["in_reply_to_status_id_str"] = str(in_reply_to_status_id)
        t["in_reply_to_status_id"] = str(in_reply_to_status_id)
    if in_reply_to_user_id is not None:
        t["in_reply_to_user_id_str"] = str(in_reply_to_user_id)
        t["in_reply_to_user_id"] = str(in_reply_to_user_id)
    return t


class TestTwitterThreading(_TwitterSandbox):
    """Self-replies thread as comments on the parent post."""

    def setUp(self):
        _assert_test_sandbox(self.url)
        _wp(["option", "update", "onionpress_social_twitter_self_user_id",
             _SAFE_TEST_USER_ID], url=self.url, timeout=15)
        _wp(["option", "update", "onionpress_social_twitter_handle",
             _SAFE_TEST_HANDLE], url=self.url, timeout=15)
        _wp(["option", "delete", "onionpress_twitter_threads_v1_migrated"],
            url=self.url, timeout=15)
        self.addCleanup(_cleanup_test_posts, self.url)
        self.addCleanup(_delete_test_comments, self.url)
        self.addCleanup(_clear_all_options, self.url)

    def _import(self, tweet, opts=None):
        if opts is None:
            opts = {"include_rts": False, "include_replies": True,
                    "self_user_id": _SAFE_TEST_USER_ID, "media_dir": ""}
        t = json.dumps(tweet).replace("'", "\\'")
        o = json.dumps(opts).replace("'", "\\'")
        return _eval(f"""
        $t = json_decode('{t}', true);
        $o = json_decode('{o}', true);
        echo onionpress_twitter_import_tweet($t, $o);
        """, self.url)

    def _post_count(self, tid):
        return _eval(f"""
        $ps = get_posts(array(
            'post_type' => 'post',
            'meta_key' => '_source_id',
            'meta_value' => 'twitter:{tid}',
            'post_status' => 'any',
            'posts_per_page' => -1,
            'fields' => 'ids',
        ));
        echo count($ps);
        """, self.url)

    def _comment_post_id(self, tid):
        return _eval(f"""
        $cs = get_comments(array(
            'meta_key' => '_source_id',
            'meta_value' => 'twitter:{tid}',
            'number' => 1,
        ));
        echo empty($cs) ? '0' : (int) $cs[0]->comment_post_ID;
        """, self.url)

    def _comment_parent(self, tid):
        return _eval(f"""
        $cs = get_comments(array(
            'meta_key' => '_source_id',
            'meta_value' => 'twitter:{tid}',
            'number' => 1,
        ));
        echo empty($cs) ? '0' : (int) $cs[0]->comment_parent;
        """, self.url)

    def test_self_reply_with_present_parent_becomes_comment(self):
        parent = "p-" + uuid.uuid4().hex
        reply  = "r-" + uuid.uuid4().hex
        self.assertEqual(self._import(_tweet(parent)), "imported")
        self.assertEqual(
            self._import(_tweet(reply, in_reply_to_status_id=parent,
                                in_reply_to_user_id=_SAFE_TEST_USER_ID)),
            "imported",
        )
        self.assertEqual(self._post_count(parent), "1")
        self.assertEqual(self._post_count(reply), "0")
        self.assertNotEqual(self._comment_post_id(reply), "0")

    def test_self_reply_without_parent_stays_top_level(self):
        """No pending/reattach in Twitter — sort-oldest-first means
        parent should always be present. If somehow not, fall through
        to top-level so import doesn't lose the tweet."""
        reply = "r-" + uuid.uuid4().hex
        self.assertEqual(
            self._import(_tweet(reply, in_reply_to_status_id="9999",
                                in_reply_to_user_id=_SAFE_TEST_USER_ID)),
            "imported",
        )
        self.assertEqual(self._post_count(reply), "1")

    def test_reply_to_other_user_stays_top_level(self):
        """A reply to someone else's tweet has no parent in the archive
        (we only ingest the user's own tweets) — keep as top-level
        post, gated by include_replies."""
        reply = "r-" + uuid.uuid4().hex
        self.assertEqual(
            self._import(_tweet(reply, in_reply_to_status_id="9999",
                                in_reply_to_user_id="999")),
            "imported",
        )
        self.assertEqual(self._post_count(reply), "1")
        self.assertEqual(self._comment_post_id(reply), "0")

    def test_self_reply_dedup_on_re_import(self):
        parent = "p-" + uuid.uuid4().hex
        reply  = "r-" + uuid.uuid4().hex
        self._import(_tweet(parent))
        r1 = _tweet(reply, in_reply_to_status_id=parent,
                    in_reply_to_user_id=_SAFE_TEST_USER_ID)
        self.assertEqual(self._import(r1), "imported")
        self.assertEqual(self._import(r1), "skipped")

    def test_nested_self_reply_chain(self):
        a = "a-" + uuid.uuid4().hex
        b = "b-" + uuid.uuid4().hex
        c = "c-" + uuid.uuid4().hex
        self._import(_tweet(a))
        self._import(_tweet(b, in_reply_to_status_id=a,
                            in_reply_to_user_id=_SAFE_TEST_USER_ID))
        self._import(_tweet(c, in_reply_to_status_id=b,
                            in_reply_to_user_id=_SAFE_TEST_USER_ID))
        b_post = self._comment_post_id(b)
        c_post = self._comment_post_id(c)
        self.assertEqual(b_post, c_post,
                         "B and C should share the same root post (A)")
        # B hangs directly off the post; C nests under B.
        self.assertEqual(self._comment_parent(b), "0")
        b_cid = _eval(f"""
        $cs = get_comments(array('meta_key'=>'_source_id','meta_value'=>'twitter:{b}','number'=>1));
        echo empty($cs) ? '0' : (int) $cs[0]->comment_ID;
        """, self.url)
        self.assertEqual(self._comment_parent(c), b_cid)

    def test_migration_converts_existing_self_reply_posts(self):
        parent = "p-" + uuid.uuid4().hex
        reply  = "r-" + uuid.uuid4().hex
        self._import(_tweet(parent))
        # Pre-seed reply as a legacy top-level post.
        raw = json.dumps(_tweet(reply, in_reply_to_status_id=parent,
                                in_reply_to_user_id=_SAFE_TEST_USER_ID))
        raw_escaped = raw.replace("'", "\\'")
        legacy_pid = int(_eval(f"""
        $pid = wp_insert_post(array(
            'post_type'=>'post','post_status'=>'publish',
            'post_title'=>'legacy reply','post_content'=>'<p>hi</p>',
            'meta_input'=>array(
                '_source_id'=>'twitter:{reply}',
                '_is_reply'=>'1',
                '_reply_to_id'=>'{parent}',
                '_source_url'=>'https://twitter.com/i/status/{reply}',
                '_raw'=>'{raw_escaped}',
            ),
        ));
        echo (int) $pid;
        """, self.url))
        self.assertGreater(legacy_pid, 0)
        n = int(_eval(
            "echo onionpress_twitter_migrate_replies_to_comments();",
            self.url,
        ))
        self.assertEqual(n, 1)
        self.assertEqual(self._post_count(reply), "0")
        self.assertNotEqual(self._comment_post_id(reply), "0")

    def test_migration_skips_explicit_other_account(self):
        """When _raw clearly says in_reply_to_user_id != self, skip."""
        parent = "p-" + uuid.uuid4().hex
        reply  = "r-" + uuid.uuid4().hex
        self._import(_tweet(parent))
        raw = json.dumps(_tweet(reply, in_reply_to_status_id=parent,
                                in_reply_to_user_id="999"))
        raw_escaped = raw.replace("'", "\\'")
        legacy_pid = int(_eval(f"""
        $pid = wp_insert_post(array(
            'post_type'=>'post','post_status'=>'publish',
            'post_title'=>'t','post_content'=>'<p>hi</p>',
            'meta_input'=>array(
                '_source_id'=>'twitter:{reply}',
                '_is_reply'=>'1',
                '_reply_to_id'=>'{parent}',
                '_source_url'=>'https://twitter.com/i/status/{reply}',
                '_raw'=>'{raw_escaped}',
            ),
        ));
        echo (int) $pid;
        """, self.url))
        self.assertGreater(legacy_pid, 0)
        _eval("echo onionpress_twitter_migrate_replies_to_comments();", self.url)
        self.assertEqual(self._post_count(reply), "1",
                         "other-account reply should NOT be converted")
        self.assertEqual(self._comment_post_id(reply), "0")

    def test_migration_handles_missing_raw(self):
        """Older imports may have no usable _raw. Without it, fall back
        to 'parent in DB ⇒ self-reply' since the importer only ingests
        the user's own archive."""
        parent = "p-" + uuid.uuid4().hex
        reply  = "r-" + uuid.uuid4().hex
        self._import(_tweet(parent))
        legacy_pid = int(_eval(f"""
        $pid = wp_insert_post(array(
            'post_type'=>'post','post_status'=>'publish',
            'post_title'=>'t','post_content'=>'<p>hi</p>',
            'meta_input'=>array(
                '_source_id'=>'twitter:{reply}',
                '_is_reply'=>'1',
                '_reply_to_id'=>'{parent}',
                '_source_url'=>'https://twitter.com/i/status/{reply}',
            ),
        ));
        echo (int) $pid;
        """, self.url))
        self.assertGreater(legacy_pid, 0)
        n = int(_eval(
            "echo onionpress_twitter_migrate_replies_to_comments();",
            self.url,
        ))
        self.assertEqual(n, 1)
        self.assertEqual(self._post_count(reply), "0")


if __name__ == "__main__":
    unittest.main()

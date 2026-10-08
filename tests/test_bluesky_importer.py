#!/usr/bin/env python3
"""Integration tests for the Bluesky importer plugin.

Drive the live plugin inside the onionpress-wordpress container via
`wp eval`, using the `onionpress_bluesky_*_mock` filter hooks to
inject canned API responses — no real Bluesky or Tor traffic.

Coverage focus:
  1. Cursor-based pagination: missing cursor marks backfill done.
     (No "short-page" trap — absence is the only terminator.)
  2. Cycle guard: same cursor returned 3 rounds in a row → error,
     no infinite loop.
  3. Dedupe by AT-URI.
  4. Repost filter (reason.$type = reasonRepost).
  5. Reply filter + self-reply always imported.
  6. Quote-post rendering (inline blockquote with quoted author + text).
  7. Quote-post unavailable states (viewNotFound / viewBlocked /
     viewDetached) render as muted one-liners, not errors.
  8. Handle → DID resolution stored at save time.
  9. Token-lock mutex: fresh lock blocks a second daemon; stale lock
     is taken over.

Opt-in and sandboxed: skipped unless ONIONPRESS_INTEGRATION_TESTS=1,
refused on a live install, and every class runs on a fresh
`op-bluesky-test` subsite that is deleted afterwards — see
wp_integration.py. Needs the plugin from this checkout in mu-plugins/.
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


_SAFE_TEST_DID    = "did:plc:testactor"
_SAFE_TEST_HANDLE = "testuser.bsky.social"
_TEST_SUBSITE_SLUG = "op-bluesky-test"

_TOUCHED_OPTIONS = (
    "onionpress_social_bluesky_handle",
    "onionpress_social_bluesky_did",
    "onionpress_social_bluesky_display_name",
    "onionpress_social_bluesky_newest_uri",
    "onionpress_social_bluesky_newest_owner",
    "onionpress_social_bluesky_backfill_cursor",
    "onionpress_social_bluesky_cursor_owner",
    "onionpress_social_bluesky_total_posts",
    "onionpress_social_bluesky_last_sync",
    "onionpress_social_bluesky_last_note",
    "onionpress_social_bluesky_daemon_lock",
    "onionpress_social_bluesky_opts",
    "onionpress_bluesky_threads_v1_migrated",
    "op_test_bluesky_mock_idx",
    "op_test_bluesky_mock_pages",
    "op_test_bluesky_post_mocks",
)


class _BlueskySandbox(wp_integration.SandboxTestCase):
    """Every class runs on the dedicated sandbox subsite. Earlier this
    file used _pick_site() — fine on a CI checkout, but on a real machine
    it picked whatever subsite happened to come first and silently
    overwrote its DID. Same trap as the Mastodon side."""
    SANDBOX_SLUG = _TEST_SUBSITE_SLUG
    SANDBOX_TITLE = "Bluesky Importer Test Sandbox"


def _assert_test_sandbox(url):
    """Refuse to run if any of _did / _handle look real. The handle
    check is load-bearing: _did is written by setUp before any guard
    notices, but _handle is only set by the live save flow or a real
    user. A non-empty, non-sandbox handle means we're on someone's blog."""
    pairs = (
        ("onionpress_social_bluesky_did",    _SAFE_TEST_DID),
        ("onionpress_social_bluesky_handle", _SAFE_TEST_HANDLE),
    )
    for opt, sandbox_value in pairs:
        r = _wp(["option", "get", opt], url=url, timeout=10)
        actual = (r.stdout or "").strip()
        if actual and actual != sandbox_value:
            raise RuntimeError(
                f"Refusing to run Bluesky tests against {url!r}: "
                f"{opt} is {actual!r} (looks real, not sandbox). "
                f"Only the dedicated test subsite "
                f"({_TEST_SUBSITE_SLUG!r}) should be the target."
            )


def _clear_all_options(url):
    """Wipe every option key the suite touches so a crashed test can't
    leak state into the next."""
    for opt in _TOUCHED_OPTIONS:
        _wp(["option", "delete", opt], url=url, timeout=10)
    _wp(["transient", "delete", "onionpress_social_bluesky_lock"],
        url=url, timeout=10)


def _delete_test_comments(url):
    """Defensive sweep for orphan comments left after a test crashes
    before its parent post is deleted."""
    _eval("""
    global $wpdb;
    $ids = $wpdb->get_col(
      "SELECT c.comment_ID FROM {$wpdb->comments} c
       JOIN {$wpdb->commentmeta} m ON m.comment_id=c.comment_ID
        AND m.meta_key='_source_id' AND m.meta_value LIKE 'bluesky:%'"
    );
    foreach ($ids as $id) { wp_delete_comment((int)$id, true); }
    echo count($ids);
    """, url)


def _cleanup_test_posts(url):
    """Delete any posts created by our test fixture. Matches by the
    test-only DID `did:plc:testactor` embedded in the _source_id —
    real Bluesky DIDs are cryptographically generated base32 strings,
    so this never collides with real imports even if the _op_test_marker
    mechanism gets skipped (e.g. a test crashes before addCleanup fires)."""
    _eval("""
    global $wpdb;
    $ids = $wpdb->get_col(
      "SELECT p.ID FROM {$wpdb->posts} p
       JOIN {$wpdb->postmeta} m ON m.post_id=p.ID AND m.meta_key='_source_id'
       WHERE (m.meta_value LIKE 'bluesky:at://did:plc:testactor/%'
              OR m.meta_value LIKE 'bluesky:repost:%did:plc:testactor/%')"
    );
    foreach ($ids as $id) { wp_delete_post((int)$id, true); }
    echo count($ids);
    """, url)


def _at_uri(rkey=None):
    """Build a deterministic AT-URI for tests."""
    return f"at://did:plc:testactor/app.bsky.feed.post/{rkey or uuid.uuid4().hex}"


def _feed_item(uri=None, text="hello from test", created="2026-04-23T00:00:00Z",
               reply_parent_uri=None, is_repost=False, embed=None,
               author_handle="testuser.bsky.social", author_did="did:plc:testactor"):
    """Build a minimal getAuthorFeed feed-view item."""
    uri = uri or _at_uri()
    record = {
        "$type":     "app.bsky.feed.post",
        "text":      text,
        "createdAt": created,
    }
    if reply_parent_uri:
        record["reply"] = {
            "parent": {"uri": reply_parent_uri, "cid": "bafy"},
            "root":   {"uri": reply_parent_uri, "cid": "bafy"},
        }
    item = {
        "post": {
            "uri":    uri,
            "cid":    "bafy",
            "author": {"did": author_did, "handle": author_handle, "displayName": "Test"},
            "record": record,
        }
    }
    if embed is not None:
        item["post"]["embed"] = embed
    if is_repost:
        item["reason"] = {
            "$type":     "app.bsky.feed.defs#reasonRepost",
            "by":        {"did": "did:plc:testactor", "handle": author_handle},
            "indexedAt": created,
        }
    return item


class TestBlueskyBackfill(_BlueskySandbox):
    """Backfill pagination + cursor semantics."""

    def setUp(self):
        _assert_test_sandbox(self.url)
        _wp(["option", "update", "onionpress_social_bluesky_did",
             "did:plc:testactor"], url=self.url, timeout=15)
        _wp(["option", "delete", "onionpress_social_bluesky_newest_uri"],
            url=self.url, timeout=15)
        _wp(["option", "delete", "onionpress_social_bluesky_backfill_cursor"],
            url=self.url, timeout=15)
        _wp(["option", "delete", "onionpress_social_bluesky_daemon_lock"],
            url=self.url, timeout=15)
        _wp(["transient", "delete", "onionpress_social_bluesky_lock"],
            url=self.url, timeout=15)
        # Default opts to include replies, exclude reposts (our project default)
        _wp(["option", "update", "onionpress_social_bluesky_opts",
             json.dumps({"include_replies": True, "include_reposts": False}),
             "--format=json"], url=self.url, timeout=15)
        self.addCleanup(_cleanup_test_posts, self.url)
        self.addCleanup(_delete_test_comments, self.url)
        self.addCleanup(_clear_all_options, self.url)

    def _register_mock(self, pages_json):
        """Install a PHP filter that returns canned responses in order.
        Each page is a full getAuthorFeed response (`{feed: [...],
        cursor?: '...'}`). The absence of `cursor` signals end."""
        pages_escaped = pages_json.replace("'", "\\'")
        return f"""
        delete_option('op_test_bluesky_mock_idx');
        update_option('op_test_bluesky_mock_pages', '{pages_escaped}', false);
        add_filter('onionpress_bluesky_fetch_feed_mock', function($_, $params) {{
            $pages = json_decode((string) get_option('op_test_bluesky_mock_pages', '[]'), true);
            $idx = (int) get_option('op_test_bluesky_mock_idx', 0);
            update_option('op_test_bluesky_mock_idx', $idx + 1, false);
            return is_array($pages) && isset($pages[$idx]) ? $pages[$idx] : array('feed' => array());
        }}, 10, 2);
        """

    def test_missing_cursor_marks_backfill_done(self):
        """A response with no `cursor` field marks backfill as done —
        the opposite of mastodon's empty-page rule but cleaner."""
        page = {"feed": [_feed_item(_at_uri())]}  # no cursor!
        mock = json.dumps([page])
        php = self._register_mock(mock) + """
        $r = onionpress_bluesky_sync_one_tick('did:plc:testactor');
        echo 'done=' . ($r['done'] ? '1' : '0') . ';imported=' . $r['stats']['imported'];
        """
        out = _eval(php, self.url)
        self.assertIn("done=1", out)
        self.assertIn("imported=1", out)

    def test_cursor_advances_across_pages(self):
        """With a cursor, backfill continues. Absent cursor on second
        page terminates."""
        page1 = {"feed": [_feed_item(_at_uri())], "cursor": "CURSOR_PAGE2"}
        page2 = {"feed": [_feed_item(_at_uri())]}  # no cursor = done
        mock = json.dumps([page1, page2])
        php = self._register_mock(mock) + """
        $r = onionpress_bluesky_sync_one_tick('did:plc:testactor');
        echo 'done=' . ($r['done'] ? '1' : '0')
          . ';pages=' . $r['stats']['pages']
          . ';imported=' . $r['stats']['imported'];
        """
        out = _eval(php, self.url)
        self.assertIn("done=1", out)
        self.assertIn("pages=2", out)
        self.assertIn("imported=2", out)

    def test_cycle_guard_stops_after_3_no_progress_rounds(self):
        """If the API returns the same non-empty cursor 3 times, we
        error out rather than spinning forever."""
        stuck = {"feed": [_feed_item(_at_uri())], "cursor": "STUCK"}
        # Stuck cursor → we feed it back → API returns same cursor → loop
        mock = json.dumps([stuck] * 10)
        php = self._register_mock(mock) + """
        $r = onionpress_bluesky_sync_one_tick('did:plc:testactor');
        echo 'done=' . ($r['done'] ? '1' : '0')
          . ';errs=' . count($r['errors']);
        """
        out = _eval(php, self.url)
        self.assertIn("done=0", out)
        self.assertIn("errs=1", out)


class TestBlueskyImportFilters(_BlueskySandbox):
    """Per-item filter logic (replies/reposts) + dedupe."""

    def setUp(self):
        _assert_test_sandbox(self.url)
        _wp(["option", "update", "onionpress_social_bluesky_did",
             "did:plc:testactor"], url=self.url, timeout=15)
        self.addCleanup(_cleanup_test_posts, self.url)
        self.addCleanup(_delete_test_comments, self.url)
        self.addCleanup(_clear_all_options, self.url)

    def _import(self, item, opts):
        s = json.dumps(item).replace("'", "\\'")
        o = json.dumps(opts).replace("'", "\\'")
        php = f"""
        $item = json_decode('{s}', true);
        $opts = json_decode('{o}', true);
        echo onionpress_bluesky_import_post($item, $opts);
        """
        return _eval(php, self.url)

    def test_dedupe(self):
        """Same AT-URI imported twice → one post created."""
        uri = _at_uri()
        item = _feed_item(uri)
        first  = self._import(item, {"include_replies": True})
        second = self._import(item, {"include_replies": True})
        self.assertEqual(first, "imported")
        self.assertEqual(second, "skipped")

    def test_reply_skipped_when_disabled(self):
        """Reply to someone else skipped when include_replies=off."""
        item = _feed_item(_at_uri(), reply_parent_uri="at://did:plc:otheruser/app.bsky.feed.post/theirs")
        r = self._import(item, {"include_replies": False})
        self.assertEqual(r, "skipped")

    def test_reply_imported_when_enabled(self):
        item = _feed_item(_at_uri(), reply_parent_uri="at://did:plc:otheruser/app.bsky.feed.post/theirs")
        r = self._import(item, {"include_replies": True})
        self.assertEqual(r, "imported")

    def test_self_reply_always_imported(self):
        """Replies to OUR OWN posts (thread continuations) are always
        imported, regardless of include_replies."""
        parent = "at://did:plc:testactor/app.bsky.feed.post/own-parent"
        item = _feed_item(_at_uri(), reply_parent_uri=parent)
        r = self._import(item, {"include_replies": False})
        self.assertEqual(r, "imported",
            "self-reply should always be imported as a thread continuation")

    def test_repost_skipped_when_disabled(self):
        item = _feed_item(_at_uri(), is_repost=True)
        r = self._import(item, {"include_reposts": False})
        self.assertEqual(r, "skipped")

    def test_repost_imported_when_enabled(self):
        item = _feed_item(_at_uri(), is_repost=True)
        r = self._import(item, {"include_reposts": True})
        self.assertEqual(r, "imported")


class TestBlueskyQuotePostRendering(_BlueskySandbox):
    """Quote-post rendering: inline blockquote with quoted author +
    text; muted one-liner for unavailable states."""

    def setUp(self):
        _assert_test_sandbox(self.url)
        self.addCleanup(_cleanup_test_posts, self.url)
        self.addCleanup(_delete_test_comments, self.url)
        self.addCleanup(_clear_all_options, self.url)

    def _render_embed(self, embed):
        e = json.dumps(embed).replace("'", "\\'")
        php = f"""
        $embed = json_decode('{e}', true);
        echo onionpress_bluesky_render_embed($embed);
        """
        return _eval(php, self.url)

    def test_quote_renders_as_blockquote(self):
        embed = {
            "$type": "app.bsky.embed.record#view",
            "record": {
                "$type":  "app.bsky.embed.record#viewRecord",
                "uri":    "at://did:plc:quoted/app.bsky.feed.post/abc",
                "author": {"handle": "quoted.bsky.social", "did": "did:plc:quoted"},
                "value":  {"text": "original wisdom", "createdAt": "2026-04-20T00:00:00Z"},
            },
        }
        out = self._render_embed(embed)
        self.assertIn("blockquote", out)
        self.assertIn("op-bluesky-quote", out)
        self.assertIn("@quoted.bsky.social", out)
        self.assertIn("original wisdom", out)
        self.assertIn("View on Bluesky", out)

    def test_quote_not_found_renders_muted_line(self):
        embed = {
            "$type":  "app.bsky.embed.record#view",
            "record": {"$type": "app.bsky.embed.record#viewNotFound",
                       "uri":   "at://did:plc:gone/app.bsky.feed.post/zzz"},
        }
        out = self._render_embed(embed)
        self.assertIn("Quoted post deleted", out)
        self.assertIn("op-bluesky-quote--unavailable", out)

    def test_quote_blocked_renders_muted_line(self):
        embed = {
            "$type":  "app.bsky.embed.record#view",
            "record": {"$type": "app.bsky.embed.record#viewBlocked"},
        }
        out = self._render_embed(embed)
        self.assertIn("Quoted post unavailable (blocked)", out)

    def test_quote_detached_renders_muted_line(self):
        embed = {
            "$type":  "app.bsky.embed.record#view",
            "record": {"$type": "app.bsky.embed.record#viewDetached"},
        }
        out = self._render_embed(embed)
        self.assertIn("Quoted post unavailable (quote removed)", out)

    def test_quoted_post_media_appears_as_placeholder(self):
        """Quoted post with nested images should render [image]
        placeholder, NOT try to sideload them."""
        embed = {
            "$type": "app.bsky.embed.record#view",
            "record": {
                "$type":  "app.bsky.embed.record#viewRecord",
                "uri":    "at://did:plc:quoted/app.bsky.feed.post/abc",
                "author": {"handle": "quoted.bsky.social"},
                "value":  {"text": "see pic"},
                "embeds": [{"$type": "app.bsky.embed.images#view",
                            "images": [{"fullsize": "https://cdn.bsky.app/x.jpg"}]}],
            },
        }
        out = self._render_embed(embed)
        self.assertIn("[image]", out)
        # The quoted-post's image URL must NOT appear as an <img> tag —
        # we preserve only a placeholder, not the media.
        self.assertNotIn("cdn.bsky.app/x.jpg", out)


class TestBlueskyHandleResolution(_BlueskySandbox):
    """Handle → DID resolution at save time."""

    def setUp(self):
        _assert_test_sandbox(self.url)
        _wp(["option", "delete", "onionpress_social_bluesky_handle"],
            url=self.url, timeout=15)
        _wp(["option", "delete", "onionpress_social_bluesky_did"],
            url=self.url, timeout=15)

    def test_resolve_handle_stores_did(self):
        """A successful resolve stores the DID for downstream lookups."""
        php = """
        add_filter('onionpress_bluesky_resolve_handle_mock',
                   function($_, $h) { return 'did:plc:resolved'; }, 10, 2);
        $r = onionpress_bluesky_resolve_handle('brewster.archive.org');
        echo is_wp_error($r) ? 'ERR:' . $r->get_error_message() : $r;
        """
        out = _eval(php, self.url)
        self.assertEqual(out, "did:plc:resolved")

    def test_resolve_handle_error_returns_wp_error(self):
        """A failing resolve returns WP_Error for the save handler to
        surface as an admin notice."""
        php = """
        add_filter('onionpress_bluesky_resolve_handle_mock',
                   function($_, $h) { return new WP_Error('not_found', 'nope'); }, 10, 2);
        $r = onionpress_bluesky_resolve_handle('nosuch.bsky.social');
        echo is_wp_error($r) ? 'WPERR:' . $r->get_error_message() : 'OK:' . $r;
        """
        out = _eval(php, self.url)
        self.assertTrue(out.startswith("WPERR:"),
            f"expected WP_Error; got: {out}")


class TestBlueskyDaemonLock(_BlueskySandbox):
    """Token-lock mutex semantics for the daemon entry point."""

    def setUp(self):
        _assert_test_sandbox(self.url)
        _wp(["option", "update", "onionpress_social_bluesky_did",
             "did:plc:testactor"], url=self.url, timeout=15)
        _wp(["option", "delete", "onionpress_social_bluesky_daemon_lock"],
            url=self.url, timeout=15)

    def test_fresh_lock_blocks_second_run(self):
        php_seed = """
        update_option('onionpress_social_bluesky_daemon_lock',
                      'otherTok:' . time(), false);
        """
        _eval(php_seed, self.url)
        php = """
        add_filter('onionpress_bluesky_fetch_feed_mock',
                   function($_, $p){ return array('feed' => array()); }, 10, 2);
        $r = onionpress_bluesky_run_sync_tick(true);
        $lock = get_option('onionpress_social_bluesky_daemon_lock');
        echo 'level=' . ($r['level'] ?? '?') . ';lock=' . $lock;
        """
        out = _eval(php, self.url)
        self.assertIn("level=warning", out)
        self.assertTrue(out.startswith("level=warning;lock=otherTok:"),
            f"lock should not have changed owner: {out}")

    def test_stale_lock_is_taken_over(self):
        php_seed = """
        update_option('onionpress_social_bluesky_daemon_lock',
                      'deadTok:' . (time() - 9999), false);
        """
        _eval(php_seed, self.url)
        php = """
        add_filter('onionpress_bluesky_fetch_feed_mock',
                   function($_, $p){ return array('feed' => array()); }, 10, 2);
        onionpress_bluesky_run_sync_tick(true);
        $lock = (string) get_option('onionpress_social_bluesky_daemon_lock', '(empty)');
        echo 'lock=' . $lock;
        """
        out = _eval(php, self.url)
        self.assertNotIn("deadTok", out)


class TestBlueskyThreading(_BlueskySandbox):
    """Self- and external-replies fold into the parent post's comment
    thread. External replies trigger a context fetch via getPosts so
    the conversation reads in full instead of as a fragmentary reply."""

    def setUp(self):
        _assert_test_sandbox(self.url)
        _wp(["option", "update", "onionpress_social_bluesky_did",
             _SAFE_TEST_DID], url=self.url, timeout=15)
        _wp(["option", "delete", "onionpress_bluesky_threads_v1_migrated"],
            url=self.url, timeout=15)
        self.addCleanup(_cleanup_test_posts, self.url)
        self.addCleanup(_delete_test_comments, self.url)
        self.addCleanup(_clear_all_options, self.url)

    def _register_post_mock(self, posts_by_uri):
        """Install a filter that returns canned items for getPosts
        single-URI fetches. Each value should be a feed-item-shaped
        array `{post: PostView}` (NOT a getPosts envelope) — the
        production wrapper unpacks it before calling the filter."""
        by_uri_json = json.dumps(posts_by_uri).replace("'", "\\'")
        return f"""
        update_option('op_test_bluesky_post_mocks', '{by_uri_json}', false);
        add_filter('onionpress_bluesky_fetch_post_mock', function($_, $uri) {{
            $by = json_decode((string) get_option('op_test_bluesky_post_mocks', '{{}}'), true);
            return is_array($by) && isset($by[$uri]) ? $by[$uri] : null;
        }}, 10, 2);
        """

    def _import(self, item, opts=None, mock_setup=""):
        if opts is None:
            opts = {"include_replies": True, "include_reposts": False}
        i = json.dumps(item).replace("'", "\\'")
        o = json.dumps(opts).replace("'", "\\'")
        return _eval(mock_setup + f"""
        $i = json_decode('{i}', true);
        $o = json_decode('{o}', true);
        echo onionpress_bluesky_import_post($i, $o);
        """, self.url)

    def _post_count(self, uri):
        return _eval(f"""
        $ps = get_posts(array(
            'post_type' => 'post',
            'meta_key' => '_source_id',
            'meta_value' => 'bluesky:' . '{uri}',
            'post_status' => 'any',
            'posts_per_page' => -1,
            'fields' => 'ids',
        ));
        echo count($ps);
        """, self.url)

    def _comment_post_id(self, uri):
        return _eval(f"""
        $cs = get_comments(array(
            'meta_key' => '_source_id',
            'meta_value' => 'bluesky:' . '{uri}',
            'number' => 1,
        ));
        echo empty($cs) ? '0' : (int) $cs[0]->comment_post_ID;
        """, self.url)

    def _meta(self, uri, key):
        return _eval(f"""
        $ps = get_posts(array(
            'post_type' => 'post',
            'meta_key' => '_source_id',
            'meta_value' => 'bluesky:' . '{uri}',
            'post_status' => 'any',
            'posts_per_page' => 1,
            'fields' => 'ids',
        ));
        if (empty($ps)) {{ echo '(no post)'; return; }}
        echo (string) get_post_meta((int)$ps[0], '{key}', true);
        """, self.url)

    def _foreign_item(self, uri, reply_parent_uri=None,
                       author_did="did:plc:foreigner",
                       author_handle="egonw.bsky.social"):
        return _feed_item(uri=uri, reply_parent_uri=reply_parent_uri,
                          author_did=author_did, author_handle=author_handle)

    def test_self_reply_with_present_parent_becomes_comment(self):
        parent_uri = _at_uri()
        reply_uri  = _at_uri()
        self.assertEqual(self._import(_feed_item(uri=parent_uri)), "imported")
        self.assertEqual(
            self._import(_feed_item(uri=reply_uri, reply_parent_uri=parent_uri)),
            "imported",
        )
        self.assertEqual(self._post_count(parent_uri), "1")
        self.assertEqual(self._post_count(reply_uri), "0")
        self.assertNotEqual(self._comment_post_id(reply_uri), "0")

    def test_self_reply_without_parent_marks_pending(self):
        reply_uri = _at_uri()
        self.assertEqual(
            self._import(_feed_item(uri=reply_uri,
                                    reply_parent_uri=_at_uri()),
                         mock_setup=self._register_post_mock({})),
            "imported",
        )
        self.assertEqual(self._meta(reply_uri, "_pending_reattach"), "1")

    def test_reattach_sweep_threads_pending(self):
        parent_uri = _at_uri()
        reply_uri  = _at_uri()
        self.assertEqual(
            self._import(_feed_item(uri=reply_uri,
                                    reply_parent_uri=parent_uri),
                         mock_setup=self._register_post_mock({})),
            "imported",
        )
        self._import(_feed_item(uri=parent_uri))
        n = int(_eval("echo onionpress_bluesky_reattach_pending();", self.url))
        self.assertGreaterEqual(n, 1)
        self.assertEqual(self._post_count(reply_uri), "0")
        self.assertNotEqual(self._comment_post_id(reply_uri), "0")

    def test_external_reply_fetches_parent_as_context(self):
        egonw_uri = "at://did:plc:foreigner/app.bsky.feed.post/abc"
        ours_uri  = _at_uri()
        egonw_item = self._foreign_item(egonw_uri)
        mock = self._register_post_mock({egonw_uri: egonw_item})
        our_reply = _feed_item(uri=ours_uri, reply_parent_uri=egonw_uri)
        self.assertEqual(self._import(our_reply, mock_setup=mock), "imported")
        self.assertEqual(self._meta(egonw_uri, "_is_context"), "1")
        self.assertNotEqual(self._comment_post_id(ours_uri), "0")
        self.assertEqual(self._post_count(ours_uri), "0")

    def test_external_reply_with_self_grandparent_threads_under_us(self):
        our_root = _at_uri()
        egonw    = "at://did:plc:foreigner/app.bsky.feed.post/mid"
        our_rep  = _at_uri()
        self._import(_feed_item(uri=our_root))
        egonw_item = self._foreign_item(egonw, reply_parent_uri=our_root)
        mock = self._register_post_mock({egonw: egonw_item})
        self._import(_feed_item(uri=our_rep, reply_parent_uri=egonw),
                     mock_setup=mock)
        egonw_post_id = self._comment_post_id(egonw)
        self.assertNotEqual(egonw_post_id, "0")
        self.assertEqual(self._comment_post_id(our_rep), egonw_post_id)

    def test_depth_cap_stops_unbounded_chain(self):
        uris = [f"at://did:plc:foreigner/app.bsky.feed.post/d{i}" for i in range(20)]
        posts = {}
        for i, u in enumerate(uris):
            parent = uris[i - 1] if i > 0 else None
            posts[u] = self._foreign_item(u, reply_parent_uri=parent)
        mock = self._register_post_mock(posts)
        _eval(mock + f"""
        $opts = array('include_replies' => true);
        echo onionpress_bluesky_ensure_imported_with_ancestry('{uris[-1]}', $opts);
        """, self.url)
        in_db = sum(int(self._post_count(u)) for u in uris)
        self.assertLessEqual(in_db, 7,
                             f"depth cap should bound chain — got {in_db}")
        self.assertGreater(in_db, 0)

    def test_backfill_context_threads_existing_replies(self):
        our_root = _at_uri()
        foreign  = "at://did:plc:foreigner/app.bsky.feed.post/back"
        our_rep  = _at_uri()
        self._import(_feed_item(uri=our_root))
        legacy_pid = int(_eval(f"""
        $pid = wp_insert_post(array(
            'post_type' => 'post',
            'post_status' => 'publish',
            'post_title' => 'legacy reply',
            'post_content' => '<p>hi</p>',
            'meta_input' => array(
                '_source_id'   => 'bluesky:{our_rep}',
                '_is_reply'    => '1',
                '_reply_to_id' => '{foreign}',
                '_source_url'  => 'https://bsky.app/x',
            ),
        ));
        echo (int) $pid;
        """, self.url))
        self.assertGreater(legacy_pid, 0)
        foreign_item = self._foreign_item(foreign, reply_parent_uri=our_root)
        mock = self._register_post_mock({foreign: foreign_item})
        n = int(_eval(mock + "echo onionpress_bluesky_backfill_context();", self.url))
        self.assertGreaterEqual(n, 1)
        self.assertEqual(self._post_count(our_rep), "0")
        self.assertNotEqual(self._comment_post_id(our_rep), "0")

    def test_migration_converts_existing_reply_posts(self):
        parent_uri = _at_uri()
        reply_uri  = _at_uri()
        self._import(_feed_item(uri=parent_uri))
        legacy_pid = int(_eval(f"""
        $pid = wp_insert_post(array(
            'post_type' => 'post', 'post_status' => 'publish',
            'post_title' => 't', 'post_content' => '<p>hi</p>',
            'meta_input' => array(
                '_source_id'   => 'bluesky:{reply_uri}',
                '_is_reply'    => '1',
                '_reply_to_id' => '{parent_uri}',
                '_source_url'  => 'https://bsky.app/y',
            ),
        ));
        echo (int) $pid;
        """, self.url))
        self.assertGreater(legacy_pid, 0)
        n = int(_eval(
            "echo onionpress_bluesky_migrate_replies_to_comments();",
            self.url,
        ))
        self.assertEqual(n, 1)
        self.assertEqual(self._post_count(reply_uri), "0")
        self.assertNotEqual(self._comment_post_id(reply_uri), "0")
        self.assertEqual(
            _eval("echo get_option('onionpress_bluesky_threads_v1_migrated', '');",
                  self.url),
            "yes",
        )


if __name__ == "__main__":
    unittest.main()

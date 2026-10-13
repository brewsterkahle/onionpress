#!/usr/bin/env python3
"""The Following aggregate is built in the background, under a lock and a
real budget (issue #314).

The sidebar is on every page and calls onionpress_get_aggregated_feed_items().
On an expired cache that used to fetch every followed feed in turn, over Tor,
inside the visitor's page render: discovery HEADs and fetch_feed() each
raised to at least 60 s by onionpress-tor-proxy, a 60 s budget checked only
between follows, no lock (every concurrent visitor ran the same rebuild), a
backoff counted from the last success (a feed down for a day was retried on
every rebuild, first in line), and a backed-off follow's posts dropped from
the blogroll.

Now a page view only reads: the cached aggregate, or each follow's last good
items while wp-cron runs onionpress_blogroll_refresh(). That holds a lock row
(INSERT IGNORE), fetches least recently tried follows first, backs off from
the last attempt, keeps a failing follow's items, and cuts every request to
what is left of its budget. Follow requests keep their own timeouts
('onionpress_exact_timeout', honoured by onionpress-tor-proxy), so the follow
form's 8 s probe is 8 s. The wp-cron loopback goes to http://localhost, so a
refresh asked for by a Tor visitor actually runs.

php is preinstalled on ubuntu-latest, so the PHP half runs in CI; locally it
is skipped when php is missing.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PLUGINS = os.path.join(ROOT, "app", "Resources", "plugins")
BLOGROLL = os.path.join(PLUGINS, "onionpress-blogroll.php")
TOR_PROXY = os.path.join(PLUGINS, "onionpress-tor-proxy.php")
SETTINGS = os.path.join(PLUGINS, "onionpress-settings.php")
THEME = os.path.join(ROOT, "app", "Resources", "themes", "onionpress")
PHP = shutil.which("php")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _function(src, name):
    m = re.search(r"^( *)function " + re.escape(name) + r"\(", src, re.M)
    assert m, f"no function {name}"
    end = src.index("\n" + m.group(1) + "}\n", m.end())
    return src[m.start():end + len(m.group(1)) + 2]


def _code(src):
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(line for line in src.splitlines()
                     if not line.lstrip().startswith("//"))


class TestSource(unittest.TestCase):

    def setUp(self):
        self.blogroll = _read(BLOGROLL)

    def test_page_views_never_fetch(self):
        serve = _function(self.blogroll, "onionpress_get_aggregated_feed_items")
        self.assertIn("onionpress_blogroll_schedule_refresh();", serve)
        for call in ("fetch_feed(", "onionpress_discover_feed_url(", "remote_",
                     "onionpress_blogroll_refresh(", "onionpress_blogroll_fetch_due("):
            with self.subTest(call=call):
                self.assertNotIn(call, serve)
        # The theme reaches the feeds only through it.
        for name in ("sidebar.php", "page-blogroll.php"):
            src = _code(_read(os.path.join(THEME, name)))
            for call in ("fetch_feed(", "onionpress_blogroll_refresh(", "wp_remote_"):
                self.assertNotIn(call, src)

    def test_refresh_is_wp_cron_only(self):
        code = _code(self.blogroll)
        self.assertIn("add_action( 'onionpress_blogroll_refresh', 'onionpress_blogroll_refresh' );", code)
        self.assertEqual(len(re.findall(r"(?<![\w'])onionpress_blogroll_refresh\(", code)), 1,
                         "onionpress_blogroll_refresh() is called directly")
        self.assertEqual(len(re.findall(r"(?<![\w'])onionpress_blogroll_fetch_due\(", code)), 2)

    def test_no_global_simplepie_lifetime(self):
        self.assertNotIn("add_filter( 'wp_feed_cache_transient_lifetime'", _code(self.blogroll))
        fetch = _function(self.blogroll, "onionpress_follow_fetch_feed")
        self.assertIn("$feed->enable_cache( false );", fetch)
        self.assertIn("$feed->set_timeout( (int) $timeout );", fetch)
        self.assertIn("remove_action( 'wp_feed_options', $options );", fetch.split("} finally {")[1])

    def test_follow_requests_opt_out_of_the_floor(self):
        args = _function(self.blogroll, "onionpress_follow_guard_args")
        allowed = args.index("if ( onionpress_follow_url_allowed( $url ) ) {")
        self.assertGreater(args.index("$args['onionpress_exact_timeout'] = true;"), allowed)
        proxy = _read(TOR_PROXY)
        hook = proxy[proxy.index("add_action( 'http_api_curl'"):]
        hook = hook[:hook.index("}, 10, 3 );")]
        self.assertIn("if ( ! empty( $parsed_args['onionpress_exact_timeout'] ) && $timeout > 0 ) {", hook)
        # The connect timeout is set before, and stays.
        self.assertLess(hook.index("CURLOPT_CONNECTTIMEOUT, 45"),
                        hook.index("onionpress_exact_timeout"))

    def test_settings_probes_go_through_the_follow_wrappers(self):
        # Their 'timeout' => 8 is exact only because they are follow requests.
        settings = _code(_read(SETTINGS))
        self.assertEqual(settings.count("onionpress_follow_remote_get( $probe_url,"), 2)
        self.assertEqual(settings.count("onionpress_discover_feed_url( $addr, "), 2)


HARNESS = r"""<?php
define( 'ABSPATH', __DIR__ . '/' );
define( 'HOUR_IN_SECONDS', 3600 );
define( 'MINUTE_IN_SECONDS', 60 );

$GLOBALS['op_hooks'] = array();
$GLOBALS['opts']     = array();
$GLOBALS['trans']    = array();
$GLOBALS['cron']     = array();
$GLOBALS['log']      = array();
$GLOBALS['net']      = array();
$GLOBALS['curl']     = array();

function add_filter( $name, $cb, $prio = 10, $accepted = 1 ) {
    $GLOBALS['op_hooks'][ $name ][ $prio ][] = array( $cb, $accepted );
    return true;
}
function add_action( $name, $cb, $prio = 10, $accepted = 1 ) { return add_filter( $name, $cb, $prio, $accepted ); }
function remove_filter( $name, $cb, $prio = 10 ) {
    if ( empty( $GLOBALS['op_hooks'][ $name ][ $prio ] ) ) { return false; }
    foreach ( $GLOBALS['op_hooks'][ $name ][ $prio ] as $i => $h ) {
        if ( $h[0] === $cb ) { unset( $GLOBALS['op_hooks'][ $name ][ $prio ][ $i ] ); return true; }
    }
    return false;
}
function remove_action( $name, $cb, $prio = 10 ) { return remove_filter( $name, $cb, $prio ); }
function apply_filters( $name, $value, ...$rest ) {
    if ( empty( $GLOBALS['op_hooks'][ $name ] ) ) { return $value; }
    $hooks = $GLOBALS['op_hooks'][ $name ];
    ksort( $hooks );
    foreach ( $hooks as $callbacks ) {
        foreach ( $callbacks as $h ) {
            $value = call_user_func_array( $h[0], array_slice( array_merge( array( $value ), $rest ), 0, $h[1] ) );
        }
    }
    return $value;
}
function do_action_ref_array( $name, $args ) {
    if ( empty( $GLOBALS['op_hooks'][ $name ] ) ) { return; }
    $hooks = $GLOBALS['op_hooks'][ $name ];
    ksort( $hooks );
    foreach ( $hooks as $callbacks ) {
        foreach ( $callbacks as $h ) { call_user_func_array( $h[0], array_slice( $args, 0, $h[1] ) ); }
    }
}
function do_action( $name, ...$args ) { do_action_ref_array( $name, $args ); }

function get_option( $name, $default = false ) {
    return array_key_exists( $name, $GLOBALS['opts'] ) ? $GLOBALS['opts'][ $name ] : $default;
}
function update_option( $name, $value, $autoload = null ) {
    $old = get_option( $name );
    $GLOBALS['opts'][ $name ] = $value;
    $GLOBALS['log'][] = array( 'update_option', $name );
    do_action( 'update_option_' . $name, $old, $value, $name );
    return true;
}
function get_transient( $name ) {
    return array_key_exists( $name, $GLOBALS['trans'] ) ? $GLOBALS['trans'][ $name ]['value'] : false;
}
function set_transient( $name, $value, $ttl = 0 ) {
    $GLOBALS['trans'][ $name ] = array( 'value' => $value, 'ttl' => $ttl );
    return true;
}
function delete_transient( $name ) {
    do_action( 'delete_transient_' . $name, $name );
    unset( $GLOBALS['trans'][ $name ] );
    return true;
}
function wp_next_scheduled( $hook ) {
    foreach ( $GLOBALS['cron'] as $e ) { if ( $e['hook'] === $hook ) { return $e['at']; } }
    return false;
}
function wp_schedule_single_event( $at, $hook ) {
    $GLOBALS['cron'][] = array( 'hook' => $hook, 'at' => $at );
    return true;
}
function wp_generate_password( $n = 12 ) { return substr( bin2hex( random_bytes( $n ) ), 0, $n ); }
function wp_parse_url( $url, $c = -1 ) { return parse_url( $url, $c ); }
function wp_strip_all_tags( $s ) { return strip_tags( $s ); }
function wp_trim_words( $s ) { return $s; }
function is_wp_error( $x ) { return $x instanceof WP_Error; }
class WP_Error {
    public $code;
    public function __construct( $code = '', $message = '' ) { $this->code = $code; }
    public function get_error_code() { return $this->code; }
}

/** The options table, for the lock's three statements. */
class OpWpdb {
    public $options = 'wp_options';
    public $rows_affected = 0;
    public function prepare( $sql, ...$args ) { return array( $sql, $args ); }
    public function query( $q ) {
        list( $sql, $args ) = $q;
        $this->rows_affected = 0;
        if ( 0 === strpos( $sql, 'INSERT IGNORE INTO wp_options' ) ) {
            if ( ! array_key_exists( $args[0], $GLOBALS['opts'] ) ) {
                $GLOBALS['opts'][ $args[0] ] = $args[1];
                $this->rows_affected = 1;
            }
        } elseif ( 0 === strpos( $sql, 'DELETE FROM wp_options WHERE option_name = %s AND option_value = %s' ) ) {
            if ( array_key_exists( $args[0], $GLOBALS['opts'] ) && $GLOBALS['opts'][ $args[0] ] === $args[1] ) {
                unset( $GLOBALS['opts'][ $args[0] ] );
                $this->rows_affected = 1;
            }
        } else {
            throw new Exception( 'unexpected query: ' . $sql );
        }
        return $this->rows_affected;
    }
    public function get_var( $q ) {
        list( $sql, $args ) = $q;
        if ( 0 !== strpos( $sql, 'SELECT option_value FROM wp_options WHERE option_name = %s' ) ) {
            throw new Exception( 'unexpected query: ' . $sql );
        }
        return array_key_exists( $args[0], $GLOBALS['opts'] ) ? $GLOBALS['opts'][ $args[0] ] : null;
    }
}
$GLOBALS['wpdb'] = new OpWpdb();

/** Network: each request is recorded with the args the filters leave it. */
function op_net( $kind, $url, $args ) {
    $args = apply_filters( 'http_request_args', $args, $url );
    $pre  = apply_filters( 'pre_http_request', false, $args, $url );
    $GLOBALS['net'][] = array( 'kind' => $kind, 'url' => $url, 'timeout' => $args['timeout'] ?? null,
                               'exact' => ! empty( $args['onionpress_exact_timeout'] ),
                               'refused' => is_wp_error( $pre ) );
    if ( isset( $GLOBALS['on_request'][ $url ] ) ) {
        call_user_func( $GLOBALS['on_request'][ $url ] );
    }
    return $pre;
}
function wp_remote_head( $url, $args = array() ) {
    $pre = op_net( 'head', $url, $args );
    if ( $pre ) { return $pre; }
    $ok = ! empty( $GLOBALS['up'][ $url ] );
    return $ok ? array( 'response' => array( 'code' => 200 ) ) : new WP_Error( 'http_request_failed' );
}
function wp_remote_get( $url, $args = array() ) { return wp_remote_head( $url, $args ); }
function wp_remote_retrieve_response_code( $r ) { return $r['response']['code']; }

class OpFeedItem {
    public $d;
    public function __construct( $d ) { $this->d = $d; }
    public function get_date( $f ) { return $this->d['date']; }
    public function get_title() { return $this->d['title']; }
    public function get_permalink() { return 'http://example.com/' . $this->d['title']; }
    public function get_author() { return null; }
    public function get_description() { return '<p>' . $this->d['title'] . '</p>'; }
}
class OpFeed {
    public $url; public $timeout = 10; public $cache = true;
    public function set_timeout( $t ) { $this->timeout = $t; }
    public function enable_cache( $on ) { $this->cache = $on; }
    public function get_title() { return 'Title of ' . $this->url; }
    public function get_items( $start, $n ) {
        return array_map( function ( $d ) { return new OpFeedItem( $d ); },
                          array_slice( $GLOBALS['up'][ $this->url ], $start, $n ) );
    }
}
function fetch_feed( $url ) {
    $feed = new OpFeed();
    $feed->url = $url;
    do_action_ref_array( 'wp_feed_options', array( &$feed, $url ) );
    $pre = op_net( 'feed', $url, array( 'timeout' => $feed->timeout, 'reject_unsafe_urls' => true ) );
    $n = count( $GLOBALS['net'] );
    $GLOBALS['net'][ $n - 1 ]['cache'] = $feed->cache;
    if ( $pre ) { return $pre; }
    if ( ! is_array( $GLOBALS['up'][ $url ] ?? null ) ) { return new WP_Error( 'simplepie-error' ); }
    return $feed;
}

function curl_setopt( $h, $opt, $value ) { $GLOBALS['curl'][ $opt ] = $value; return true; }
function fsockopen( ...$a ) { return fopen( 'php://memory', 'r' ); }

require %(tor_proxy)s;
require %(blogroll)s;

$in = json_decode( file_get_contents( 'php://stdin' ), true );
$GLOBALS['opts'] = $in['opts'] ?? array();
foreach ( $GLOBALS['opts'] as $k => $v ) {
    if ( is_string( $v ) && preg_match( '/^@now([+-]\d+)$/', $v, $m ) ) { $GLOBALS['opts'][ $k ] = time() + (int) $m[1]; }
}
array_walk_recursive( $GLOBALS['opts'], function ( &$v ) {
    if ( is_string( $v ) && preg_match( '/^@now([+-]\d+)$/', $v, $m ) ) { $v = time() + (int) $m[1]; }
} );
if ( isset( $in['opts']['onionpress_blogroll_lock'] ) ) {
    $GLOBALS['opts']['onionpress_blogroll_lock'] = 'other:' . ( time() + (int) $in['opts']['onionpress_blogroll_lock'] );
}
$GLOBALS['trans'] = $in['trans'] ?? array();
$GLOBALS['up']    = $in['up'] ?? array();
$GLOBALS['on_request'] = array();
foreach ( $in['unfollow_during'] ?? array() as $url => $key ) {
    $GLOBALS['on_request'][ $url ] = function () use ( $key ) {
        $GLOBALS['opts']['onionpress_following'] = array_values( array_diff( $GLOBALS['opts']['onionpress_following'], array( $key ) ) );
    };
}

$result = null;
$t0 = time();
foreach ( $in['steps'] as $step ) {
    switch ( $step[0] ) {
        case 'serve':     $result = onionpress_get_aggregated_feed_items( $step[1] ); break;
        case 'cron':      do_action( 'onionpress_blogroll_refresh' ); break;
        case 'fetch_due': onionpress_follow_deadline( microtime( true ) + $step[1] );
                          $result = onionpress_blogroll_fetch_due();
                          onionpress_follow_deadline( 0 ); break;
        case 'delete':    delete_transient( 'onionpress_blogroll_items' ); break;
        case 'guard':     onionpress_follow_deadline( $step[2] ? microtime( true ) + $step[2] : 0 );
                          $result = onionpress_follow_guarded( function () use ( $step ) {
                              return apply_filters( 'http_request_args', array( 'timeout' => $step[1] ), 'http://example.com/feed/' );
                          } );
                          onionpress_follow_deadline( 0 ); break;
        case 'curl':      $GLOBALS['curl'] = array();
                          do_action_ref_array( 'http_api_curl', array( null, $step[1], $step[2] ) );
                          $result = array( 'timeout' => $GLOBALS['curl'][ CURLOPT_TIMEOUT ] ?? null,
                                           'connect' => $GLOBALS['curl'][ CURLOPT_CONNECTTIMEOUT ] ?? null ); break;
        case 'cron_url':  $r = apply_filters( 'cron_request', array( 'url' => $step[1], 'key' => 'k', 'args' => array() ), 'k' );
                          $result = $r['url']; break;
    }
}
$cron = array_map( function ( $e ) use ( $t0 ) { return array( $e['hook'], $e['at'] - $t0 ); }, $GLOBALS['cron'] );
$opts = $GLOBALS['opts'];
echo json_encode( array( 'result' => $result, 'opts' => $opts, 'trans' => $GLOBALS['trans'],
                         'cron' => $cron, 'net' => $GLOBALS['net'], 'now' => $t0 ) );
"""

A = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.onion"
B = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.onion"
C = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccc.onion"
FEED_A = f"http://{A}/feed/"
FEED_B = f"http://{B}/feed/"
FEED_C = f"http://{C}/feed/"


@unittest.skipUnless(PHP, "php not installed")
class TestUnderPhp(unittest.TestCase):

    def _run(self, steps, **state):
        tmp = tempfile.mkdtemp(prefix="onionpress-blogroll-refresh-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        harness = os.path.join(tmp, "harness.php")
        with open(harness, "w") as f:
            f.write(HARNESS.replace("%(tor_proxy)s", json.dumps(TOR_PROXY))
                    .replace("%(blogroll)s", json.dumps(BLOGROLL)))
        proc = subprocess.run(
            [PHP, "-d", "error_reporting=E_ALL", "-d", "display_errors=stderr",
             "-d", "disable_functions=curl_setopt,fsockopen", harness],
            input=json.dumps(dict(state, steps=steps)), capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertEqual(proc.stderr, "")
        return json.loads(proc.stdout)

    @staticmethod
    def _follows(*keys, feeds=True, **extra):
        opts = {"onionpress_following": list(keys)}
        if feeds:
            opts["onionpress_following_feeds"] = {k: f"http://{k}/feed/" for k in keys}
        opts.update(extra)
        return opts

    def test_page_view_serves_stored_items_and_only_schedules(self):
        items = {A: [{"title": "a1", "date": 100}], B: [{"title": "b1", "date": 300}],
                 C: [{"title": "gone", "date": 999}]}
        out = self._run([["serve", 5], ["serve", 5]],
                        opts=self._follows(A, B, onionpress_following_items=items),
                        up={FEED_A: [{"title": "x", "date": 1}]})
        self.assertEqual(out["net"], [])
        self.assertEqual([i["title"] for i in out["result"]], ["b1", "a1"])  # C unfollowed
        self.assertEqual(out["cron"], [["onionpress_blogroll_refresh", 0]])

    def test_fresh_aggregate_is_served_as_is(self):
        out = self._run([["serve", 1]], opts=self._follows(A),
                        trans={"onionpress_blogroll_items": {"value": [{"title": "t"}, {"title": "u"}], "ttl": 1}})
        self.assertEqual(out["result"], [{"title": "t"}])
        self.assertEqual(out["cron"], [])
        self.assertEqual(out["net"], [])

    def test_clearing_the_aggregate_schedules_a_refresh(self):
        out = self._run([["delete"]], opts=self._follows(A),
                        trans={"onionpress_blogroll_items": {"value": [], "ttl": 1}})
        self.assertEqual(out["cron"], [["onionpress_blogroll_refresh", 0]])

    def test_refresh_fetches_stores_and_unlocks(self):
        out = self._run([["cron"]], opts=self._follows(A, B, feeds=False),
                        up={FEED_A: [{"title": "a1", "date": 100}],
                            f"http://{B}/feed/": [{"title": "b1", "date": 200}]})
        opts = out["opts"]
        self.assertNotIn("onionpress_blogroll_lock", opts)
        self.assertEqual(out["trans"]["onionpress_blogroll_items"]["ttl"], 3600)
        self.assertEqual([i["title"] for i in out["trans"]["onionpress_blogroll_items"]["value"]], ["b1", "a1"])
        self.assertEqual(opts["onionpress_following_feeds"], {A: FEED_A, B: FEED_B})
        self.assertEqual(opts["onionpress_following_stats"][A]["fail_count"], 0)
        for n in out["net"]:
            self.assertTrue(n["exact"], n)
            self.assertFalse(n["refused"], n)
            if n["kind"] == "feed":
                self.assertEqual(n["timeout"], 60)
                self.assertFalse(n["cache"])
            else:
                self.assertEqual(n["timeout"], 30)
        # A fresh aggregate: a second run does nothing.
        again = self._run([["cron"]], opts=opts, trans=out["trans"], up={FEED_A: []})
        self.assertEqual(again["net"], [])

    def test_one_refresh_at_a_time(self):
        out = self._run([["cron"]], opts=self._follows(A, onionpress_blogroll_lock=200),
                        up={FEED_A: [{"title": "a1", "date": 1}]})
        self.assertEqual(out["net"], [])
        self.assertEqual(out["cron"], [["onionpress_blogroll_refresh", 60]])
        self.assertTrue(out["opts"]["onionpress_blogroll_lock"].startswith("other:"))

    def test_an_expired_lock_is_taken_over(self):
        out = self._run([["cron"]], opts=self._follows(A, onionpress_blogroll_lock=-5),
                        up={FEED_A: [{"title": "a1", "date": 1}]})
        self.assertEqual([n["url"] for n in out["net"]], [FEED_A])
        self.assertNotIn("onionpress_blogroll_lock", out["opts"])

    def test_backoff_counts_from_the_last_attempt(self):
        stats = {
            # Down for days, tried an hour ago: 4 h backoff, not due.
            A: {"fail_count": 3, "last_success": "@now-900000", "last_attempt": "@now-3600"},
            # One failure two hours ago: due.
            B: {"fail_count": 1, "last_attempt": "@now-7200"},
            # Stats from before last_attempt: counts from last_success.
            C: {"fail_count": 9, "last_success": "@now-90000"},
        }
        out = self._run([["cron"]], opts=self._follows(A, B, C, onionpress_following_stats=stats),
                        up={FEED_B: [], FEED_C: []})
        self.assertEqual(sorted(n["url"] for n in out["net"]), [FEED_B, FEED_C])
        new = out["opts"]["onionpress_following_stats"]
        self.assertEqual(new[A]["fail_count"], 3)
        self.assertEqual(new[B]["fail_count"], 0)
        self.assertGreaterEqual(new[C]["last_attempt"], out["now"])

    def test_a_failing_follow_keeps_its_items(self):
        items = {A: [{"title": "kept", "date": 5, "source_addr": A}]}
        out = self._run([["cron"]], opts=self._follows(A, onionpress_following_items=items), up={})
        opts = out["opts"]
        self.assertEqual(opts["onionpress_following_items"], items)
        self.assertEqual(opts["onionpress_following_stats"][A]["fail_count"], 1)
        self.assertGreaterEqual(opts["onionpress_following_stats"][A]["last_attempt"], out["now"])
        self.assertNotIn("last_success", opts["onionpress_following_stats"][A])
        self.assertEqual([i["title"] for i in out["trans"]["onionpress_blogroll_items"]["value"]], ["kept"])

    def test_least_recently_tried_first(self):
        stats = {A: {"fail_count": 0, "last_attempt": 300}, B: {"fail_count": 0, "last_attempt": 100}}
        out = self._run([["cron"]], opts=self._follows(A, B, C, onionpress_following_stats=stats),
                        up={FEED_A: [], FEED_B: [], FEED_C: []})
        self.assertEqual([n["url"] for n in out["net"]], [FEED_C, FEED_B, FEED_A])

    def test_no_follow_starts_without_budget(self):
        out = self._run([["fetch_due", 10]], opts=self._follows(A), up={FEED_A: []})
        self.assertFalse(out["result"])
        self.assertEqual(out["net"], [])

    def test_unfollow_during_a_refresh_is_not_undone(self):
        out = self._run([["cron"]], opts=self._follows(A, B),
                        up={FEED_A: [{"title": "a1", "date": 1}], FEED_B: [{"title": "b1", "date": 2}]},
                        unfollow_during={FEED_A: B})
        opts = out["opts"]
        self.assertEqual(opts["onionpress_following"], [A])
        self.assertNotIn(B, opts["onionpress_following_stats"])
        self.assertNotIn(B, opts["onionpress_following_items"])
        self.assertNotIn(B, opts.get("onionpress_following_titles", {}))

    def test_refused_url_follow_loses_feed_title_and_items(self):
        bad = "http://onionpress-tor:8083/api"
        opts = {"onionpress_following": [bad],
                "onionpress_following_feeds": {bad: bad},
                "onionpress_following_titles": {bad: "hub"},
                "onionpress_following_items": {bad: [{"title": "x", "date": 1}]}}
        out = self._run([["cron"]], opts=opts)
        self.assertEqual(out["net"], [])
        for option in ("onionpress_following_feeds", "onionpress_following_titles",
                       "onionpress_following_items"):
            self.assertEqual(out["opts"][option], [], option)

    def test_guard_cuts_requests_to_the_budget(self):
        self.assertEqual(self._run([["guard", 8, 0]])["result"]["timeout"], 8)
        out = self._run([["guard", 60, 5]])["result"]
        self.assertTrue(out["onionpress_exact_timeout"])
        self.assertLessEqual(out["timeout"], 5)
        self.assertGreater(out["timeout"], 3)

    def test_tor_proxy_floor_and_exact_timeouts(self):
        url = f"http://{A}/feed/"
        self.assertEqual(self._run([["curl", {"timeout": 8}, url]])["result"],
                         {"timeout": 60, "connect": 45})
        self.assertEqual(self._run([["curl", {"timeout": 120}, url]])["result"],
                         {"timeout": 120, "connect": 45})
        self.assertEqual(self._run([["curl", {"timeout": 8, "onionpress_exact_timeout": True}, url]])["result"],
                         {"timeout": 8, "connect": 45})
        self.assertEqual(self._run([["curl", {"timeout": 0.4, "onionpress_exact_timeout": True}, url]])["result"],
                         {"timeout": 1, "connect": 45})

    def test_cron_loopback_stays_in_the_container(self):
        for url, want in (
            (f"http://{A}/alice/wp-cron.php?doing_wp_cron=1.5", "http://localhost/alice/wp-cron.php?doing_wp_cron=1.5"),
            ("https://example.org/wp-cron.php?doing_wp_cron=2", "http://localhost/wp-cron.php?doing_wp_cron=2"),
            ("http://localhost:8080/wp-cron.php?doing_wp_cron=3", "http://localhost/wp-cron.php?doing_wp_cron=3"),
            ("http://localhost/wp-cron.php?doing_wp_cron=4", "http://localhost/wp-cron.php?doing_wp_cron=4"),
        ):
            with self.subTest(url=url):
                self.assertEqual(self._run([["cron_url", url]])["result"], want)


if __name__ == "__main__":
    unittest.main()

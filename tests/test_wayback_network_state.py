#!/usr/bin/env python3
"""The Wayback sweep's archive.org keys, lock, back-off and schedule are
the network's, not a blog's (issue #322).

One sweep walks every site of the multisite. Through 2.5.3:
  1. The Settings page saved the archive.org keys in the current site's
     options while the sweep read only the main site's, so a pair the owner
     entered on their own /<onionname>/ site was never used.
  2. The sweep's lock and back-off were each blog's own options and every
     blog scheduled the sweep in its own cron, so each blog could run a
     whole-network sweep beside the others, and a back-off set on one did
     not hold the rest.
  3. First-run setup logged in to clearnet archive.org while the per-launch
     reconcile used archive.org's onion (two copies of the login).

Most of these read the sources; TestUnderPhp runs both plugins against
stubbed WordPress functions where php is installed (CI's ubuntu-latest has
it). The integration suite (test_wayback_sweep.py) exercises the lock
inside a live WordPress.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WAYBACK = "app/Resources/plugins/onionpress-wayback-archive.php"
SETTINGS = "app/Resources/plugins/onionpress-settings.php"


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _function(src, name):
    start = src.index(f"function {name}(")
    return src[start:src.index("\n}\n", start)]


def _hook(src, marker):
    """The add_action( 'init' ... ) block that contains *marker*."""
    blocks = re.split(r"^(?=add_action\(|add_filter\(|function )", src, flags=re.M)
    found = [b for b in blocks if marker in b]
    if len(found) != 1:
        raise AssertionError(f"{len(found)} blocks contain {marker!r}")
    return found[0]


class TestArchiveKeysAreTheNetworks(unittest.TestCase):

    def setUp(self):
        self.settings = _read(SETTINGS)
        self.wayback = _read(WAYBACK)

    def test_settings_page_reads_and_writes_only_the_main_sites_keys(self):
        # Every per-site option call on the keys goes through the helpers.
        for call in ("get_option( 'onionpress_archive_s3",
                     "update_option( 'onionpress_archive_s3",
                     "delete_option( 'onionpress_archive_s3"):
            with self.subTest(call=call):
                self.assertNotIn(call, self.settings)
        read = _function(self.settings, "onionpress_archive_keys")
        save = _function(self.settings, "onionpress_archive_keys_save")
        for body in (read, save):
            self.assertIn("$main = function_exists( 'get_main_site_id' ) ? get_main_site_id() : 1;", body)
        self.assertEqual(read.count("get_blog_option( $main, 'onionpress_archive_s3_"), 2)
        self.assertEqual(save.count("update_blog_option( $main, 'onionpress_archive_s3_"), 2)

    def test_sweep_reads_the_main_sites_keys(self):
        fn = _function(self.wayback, "onionpress_wayback_auth_header")
        self.assertEqual(fn.count("get_blog_option( $main, 'onionpress_archive_s3_"), 2)
        self.assertIn("get_main_site_id()", fn)

    def test_subsite_keys_are_moved_once(self):
        fn = _function(self.settings, "onionpress_archive_keys_move_to_main_site")
        self.assertIn("add_action( 'init', 'onionpress_archive_keys_move_to_main_site' );",
                      self.settings)
        # Main site only, once per network, then every subsite's copy goes.
        self.assertLess(fn.index("! is_main_site()"), fn.index("get_sites("))
        self.assertLess(fn.index("get_site_option( 'onionpress_archive_keys_moved' ) === 'yes'"),
                        fn.index("get_sites("))
        self.assertIn("onionpress_archive_keys_save( $access, $secret );", fn)
        self.assertIn("delete_blog_option( $bid, 'onionpress_archive_s3_access' );", fn)
        self.assertIn("delete_blog_option( $bid, 'onionpress_archive_s3_secret' );", fn)
        self.assertIn("update_site_option( 'onionpress_archive_keys_moved', 'yes' );", fn)

    def test_blank_fields_keep_the_keys(self):
        """The account and password fields are always empty. Clearing on
        blank wiped the network's keys whenever the owner saved any other
        setting; removing them takes the checkbox."""
        clear = self.settings.index("onionpress_archive_keys_save( '', '' );")
        branch = self.settings.rfind("elseif", 0, clear)
        self.assertIn("! empty( $_POST['op_ia_credentials_remove'] )",
                      self.settings[branch:clear])
        self.assertIn('name="op_ia_credentials_remove"', self.settings)


class TestSweepStateIsTheNetworks(unittest.TestCase):

    def setUp(self):
        self.php = _read(WAYBACK)

    def test_lock_and_backoff_only_through_the_network_helpers(self):
        for name in ("OP_WB_NET_LOCK", "OP_WB_OPT_BACKOFF_UNTIL", "'op_wayback_sweep_lock'",
                     "'op_wayback_backoff_until'"):
            for fn in ("get_option(", "update_option(", "delete_option("):
                with self.subTest(name=name, fn=fn):
                    self.assertIsNone(re.search(re.escape(fn) + r"\s*" + re.escape(name), self.php))
        get = _function(self.php, "onionpress_wayback_net_get")
        self.assertIn("get_site_option( $key, $default )", get)
        # A long-running sweep must see another process's takeover.
        self.assertIn("wp_cache_delete( $network . ':' . $key, 'site-options' );", get)
        self.assertIn("update_site_option(", _function(self.php, "onionpress_wayback_net_set"))
        self.assertIn("delete_site_option(", _function(self.php, "onionpress_wayback_net_delete"))
        self.assertIn("define( 'OP_WB_NET_LOCK',          'op_wayback_sweep_lock' );", self.php)

    def test_net_key_filter_is_used_for_every_access(self):
        for fn in ("onionpress_wayback_net_get", "onionpress_wayback_net_set",
                   "onionpress_wayback_net_delete"):
            with self.subTest(fn=fn):
                self.assertIn("onionpress_wayback_net_key( $key )", _function(self.php, fn))

    def test_sweep_is_scheduled_on_the_main_site_only(self):
        init = _hook(self.php, "wp_schedule_event( time(), 'onionpress_wayback_watchdog'")
        guard = init.index("if ( is_multisite() && ! is_main_site() ) {")
        self.assertLess(guard, init.index("wp_schedule_event("))
        self.assertIn("wp_clear_scheduled_hook( 'onionpress_wayback_sweep' );",
                      init[guard:init.index("return;", guard)])

    def test_immediate_sweeps_go_to_the_main_sites_cron(self):
        # One wp_schedule_single_event, inside the helper that switches to
        # the main site.
        self.assertEqual(self.php.count("wp_schedule_single_event("), 1)
        fn = _function(self.php, "onionpress_wayback_schedule_now")
        self.assertIn("switch_to_blog( get_main_site_id() );", fn)
        self.assertIn("wp_schedule_single_event( time(), 'onionpress_wayback_sweep' );", fn)

    def test_publishing_leaves_the_network_backoff_alone(self):
        for marker in ("add_action( 'save_post'", "add_action( 'wp_insert_comment'"):
            hook = _hook(self.php, marker)
            with self.subTest(hook=marker):
                self.assertIn("onionpress_wayback_schedule_now();", hook)
                self.assertNotIn("OP_WB_OPT_BACKOFF_UNTIL", hook)

    def test_kick_runs_the_main_sites_cron(self):
        hook = _hook(self.php, "add_action( 'admin_post_onionpress_wayback_action'")
        self.assertIn("get_site_url( $main, 'wp-cron.php?doing_wp_cron='", hook)
        self.assertNotIn("site_url( 'wp-cron.php", hook.replace("get_site_url( $main, 'wp-cron.php", ""))

    def test_old_per_blog_state_is_retired_once(self):
        fn = _function(self.php, "onionpress_wayback_retire_blog_state")
        self.assertIn("delete_blog_option( (int) $bid, OP_WB_NET_LOCK );", fn)
        self.assertIn("delete_blog_option( (int) $bid, OP_WB_OPT_BACKOFF_UNTIL );", fn)
        self.assertIn("update_site_option( 'op_wayback_blog_state_retired', 'yes' );", fn)


PHP = shutil.which("php")

HARNESS = r"""<?php
define( 'ABSPATH', '/nonexistent/' );
$GLOBALS['blogs'] = array(
    1 => array( 'onionpress_archive_s3_access' => 'MAIN-A', 'onionpress_archive_s3_secret' => 'MAIN-S',
                'op_wayback_sweep_lock' => 'old:1', 'op_wayback_backoff_until' => 9 ),
    2 => array(),
    3 => array( 'onionpress_archive_s3_access' => 'SUB-A', 'onionpress_archive_s3_secret' => 'SUB-S',
                'op_wayback_backoff_until' => 9 ),
    4 => array( 'onionpress_archive_s3_access' => 'LATER-A', 'onionpress_archive_s3_secret' => 'LATER-S' ),
);
$GLOBALS['site'] = array();
$GLOBALS['cur'] = 3;
$GLOBALS['stack'] = array();
$GLOBALS['sched'] = array();
function add_action() {}
function add_filter() {}
function apply_filters( $n, $v ) { return $v; }
function is_multisite() { return true; }
function get_main_site_id() { return 1; }
function is_main_site() { return $GLOBALS['cur'] === 1; }
function wp_doing_cron() { return true; }
function is_admin() { return false; }
function get_current_network_id() { return 1; }
function wp_cache_delete() { return true; }
function get_blog_option( $b, $k, $d = false ) { return $GLOBALS['blogs'][ $b ][ $k ] ?? $d; }
function update_blog_option( $b, $k, $v ) { $GLOBALS['blogs'][ $b ][ $k ] = $v; return true; }
function delete_blog_option( $b, $k ) { unset( $GLOBALS['blogs'][ $b ][ $k ] ); return true; }
function get_site_option( $k, $d = false ) { return $GLOBALS['site'][ $k ] ?? $d; }
function update_site_option( $k, $v ) { $GLOBALS['site'][ $k ] = $v; return true; }
function delete_site_option( $k ) { unset( $GLOBALS['site'][ $k ] ); return true; }
function get_sites( $a ) { return array( '1', '2', '3', '4' ); }
function switch_to_blog( $b ) { $GLOBALS['stack'][] = $GLOBALS['cur']; $GLOBALS['cur'] = $b; }
function restore_current_blog() { $GLOBALS['cur'] = array_pop( $GLOBALS['stack'] ); }
function wp_schedule_single_event( $t, $h ) { $GLOBALS['sched'][] = $GLOBALS['cur']; return true; }
require %(settings)s;
require %(wayback)s;

$out = array();
onionpress_archive_keys_move_to_main_site();          // on subsite 3: not here
$out['moved_from_subsite'] = $GLOBALS['site'];
$GLOBALS['cur'] = 1;
onionpress_archive_keys_move_to_main_site();
onionpress_archive_keys_move_to_main_site();          // once
$out['after_move'] = $GLOBALS['blogs'];
$out['auth'] = onionpress_wayback_auth_header();
$GLOBALS['cur'] = 2;
onionpress_archive_keys_save( 'NEW-A', 'NEW-S' );     // saved on a subsite
$out['saved'] = onionpress_archive_keys();
$out['auth_after_save'] = onionpress_wayback_auth_header();
$out['subsite_2'] = $GLOBALS['blogs'][2];
onionpress_wayback_schedule_now();
$out['scheduled_on'] = $GLOBALS['sched'];
$out['cur_after_schedule'] = $GLOBALS['cur'];
onionpress_wayback_net_set( OP_WB_OPT_BACKOFF_UNTIL, 123 );
$GLOBALS['cur'] = 4;
$out['backoff_from_4'] = onionpress_wayback_net_get( OP_WB_OPT_BACKOFF_UNTIL, 0 );
$GLOBALS['cur'] = 1;
onionpress_wayback_retire_blog_state();
$out['blogs_after_retire'] = $GLOBALS['blogs'];
$out['site'] = $GLOBALS['site'];
echo json_encode( $out );
"""


@unittest.skipUnless(PHP, "php not installed")
class TestUnderPhp(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.mkdtemp(prefix="onionpress-wayback-network-")
        try:
            harness = os.path.join(tmp, "harness.php")
            with open(harness, "w") as f:
                f.write(HARNESS % {"settings": json.dumps(os.path.join(ROOT, SETTINGS)),
                                   "wayback": json.dumps(os.path.join(ROOT, WAYBACK))})
            proc = subprocess.run(
                [PHP, "-d", "error_reporting=E_ALL", "-d", "display_errors=stderr",
                 "-d", "error_log=/dev/null", harness],
                capture_output=True, text=True, timeout=60)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        assert proc.returncode == 0 and proc.stderr == "", proc.stderr
        cls.out = json.loads(proc.stdout)

    def test_keys_move_only_on_the_main_site_and_once(self):
        self.assertEqual(self.out["moved_from_subsite"], [])
        blogs = self.out["after_move"]
        # The lowest subsite's pair wins over the main site's; every
        # subsite copy is gone.
        self.assertEqual(blogs["1"]["onionpress_archive_s3_access"], "SUB-A")
        self.assertEqual(blogs["1"]["onionpress_archive_s3_secret"], "SUB-S")
        for bid in ("3", "4"):
            self.assertNotIn("onionpress_archive_s3_access", blogs[bid])
            self.assertNotIn("onionpress_archive_s3_secret", blogs[bid])
        self.assertEqual(self.out["auth"], "LOW SUB-A:SUB-S")
        self.assertEqual(self.out["site"]["onionpress_archive_keys_moved"], "yes")

    def test_keys_saved_on_a_subsite_reach_the_sweep(self):
        self.assertEqual(self.out["saved"], {"access": "NEW-A", "secret": "NEW-S"})
        self.assertEqual(self.out["auth_after_save"], "LOW NEW-A:NEW-S")
        self.assertEqual(self.out["subsite_2"], [])

    def test_schedule_from_a_subsite_lands_on_the_main_site(self):
        self.assertEqual(self.out["scheduled_on"], [1])
        self.assertEqual(self.out["cur_after_schedule"], 2)

    def test_backoff_is_shared_and_old_state_retired(self):
        self.assertEqual(self.out["backoff_from_4"], 123)
        self.assertEqual(self.out["site"]["op_wayback_backoff_until"], 123)
        for bid, opts in self.out["blogs_after_retire"].items():
            if isinstance(opts, dict):
                with self.subTest(blog=bid):
                    self.assertNotIn("op_wayback_sweep_lock", opts)
                    self.assertNotIn("op_wayback_backoff_until", opts)
        self.assertEqual(self.out["site"]["op_wayback_blog_state_retired"], "yes")


if __name__ == "__main__":
    unittest.main()

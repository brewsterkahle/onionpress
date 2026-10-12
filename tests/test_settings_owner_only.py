#!/usr/bin/env python3
"""What reaches the host or every site is the network super admin's.

OnionPress runs WordPress as a multisite: the owner's site, plus a subsite
at /<onionname>/ for each additional user, who administers it and so holds
manage_options there. The OnionPress Settings page was registered on every
site with manage_options, and its handlers (config-updates.json, start/stop/
restart/update, reachability, backup with its download, restore, onion key
import, analytics upload) checked only manage_options. Any subsite
administrator could download a backup of every site's database and the
onion service key, restore over the whole node, or replace its address.

Now those handlers, the REST route and the AJAX endpoints require
onionpress_settings_owner_allowed() (logged in and is_super_admin(), the
owner: setup's multisite-install/-convert makes the installing admin super
admin; on a single site it means an administrator). A subsite administrator
still gets the OnionPress menu, showing only the site's Following list,
which lives in that site's own options.

There is no PHP in the test environment, so these read the source.
"""

import os
import re
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

SETTINGS = "app/Resources/plugins/onionpress-settings.php"

HOST = "/var/lib/onionpress/"
OWNER_GATE = "if ( ! onionpress_settings_owner_allowed() )"


def _read(rel_path):
    with open(os.path.join(PROJECT_ROOT, rel_path), "r", encoding="utf-8") as f:
        return f.read()


def _between(text, start_marker, end_marker):
    start = text.index(start_marker)
    return text[start:text.index(end_marker, start + len(start_marker))]


def _blocks(src):
    """The file's top-level statements: each column-0 add_action(,
    add_filter( or function, up to the next one. Column-0 docblocks and //
    lines are dropped first so a comment never lands in the block before
    the one it describes."""
    src = re.sub(r"^/\*\*.*?\*/\n", "", src, flags=re.M | re.S)
    src = re.sub(r"^//[^\n]*\n", "", src, flags=re.M)
    starts = [m.start() for m in
              re.finditer(r"^(?:add_action\(|add_filter\(|function )", src, re.M)]
    return [src[s:e] for s, e in zip(starts, starts[1:] + [len(src)])]


def _block(src, marker):
    found = [b for b in _blocks(src) if marker in b]
    if len(found) != 1:
        raise AssertionError(f"{len(found)} blocks contain {marker!r}")
    return found[0]


class TestSettingsPageHandlers(unittest.TestCase):

    def setUp(self):
        self.php = _read(SETTINGS)

    def test_owner_is_the_super_admin(self):
        fn = _between(self.php, "function onionpress_settings_owner_allowed()", "\n}\n")
        self.assertIn("return is_user_logged_in() && is_super_admin();", fn)

    def test_every_block_that_reaches_the_host_checks_the_owner_first(self):
        """Every hook and function that names the shared volume asks
        onionpress_settings_owner_allowed() before it. The one exception
        reads status.json's platform for the field list and is only called
        from gated code (next test)."""
        checked = 0
        for block in _blocks(self.php):
            if HOST not in block:
                continue
            if block.startswith("function onionpress_settings_fields()"):
                continue
            head = block.split("\n", 1)[0]
            with self.subTest(block=head):
                self.assertIn("onionpress_settings_owner_allowed()", block)
                self.assertLess(block.index("onionpress_settings_owner_allowed()"),
                                block.index(HOST))
            checked += 1
        # action handler, REST route, config save, download, poll, update
        # check, and the page itself
        self.assertEqual(checked, 7)

    def test_field_list_is_read_only_after_the_gate(self):
        calls = [b for b in _blocks(self.php)
                 if "onionpress_settings_fields()" in b
                 and not b.startswith("function onionpress_settings_fields()")]
        self.assertEqual(len(calls), 2)
        for block in calls:
            with self.subTest(block=block.split("\n", 1)[0]):
                self.assertLess(block.index(OWNER_GATE),
                                block.index("onionpress_settings_fields()"))

    def test_action_handler_gates_before_any_action(self):
        body = _block(self.php, "wp_verify_nonce( $_POST['onionpress_action_nonce']")
        gate = body.index(OWNER_GATE)
        self.assertLess(body.index("wp_verify_nonce( $_POST['onionpress_action_nonce'], 'onionpress_action' )"), gate)
        for act in ("'create-backup'", "'restore-backup'", "'import-key-file'",
                    "requested-action", "backup-password", "restore-upload.zip",
                    "import-key-data"):
            with self.subTest(action=act):
                self.assertLess(gate, body.index(act))
        self.assertIn("return;", body[gate:gate + 80])

    def test_config_save_gates_before_writing(self):
        body = _block(self.php, "wp_verify_nonce( $_POST['onionpress_settings_nonce']")
        gate = body.index(OWNER_GATE)
        self.assertLess(body.index("'onionpress_settings_save'"), gate)
        for write in ("config-updates.json", "update_option( 'onionpress_archive_s3_access'"):
            with self.subTest(write=write):
                self.assertLess(gate, body.index(write))

    def test_rest_route_is_the_owners(self):
        body = _block(self.php, "register_rest_route( 'onionpress/v1', '/upload-analytics'")
        self.assertIn(
            "'permission_callback' => function () { return onionpress_settings_owner_allowed(); },",
            body)
        self.assertEqual(body.count("register_rest_route("), 1)
        self.assertEqual(self.php.count("register_rest_route("), 1)

    def test_every_ajax_endpoint_is_the_owners(self):
        names = re.findall(r"add_action\( 'wp_ajax_(\w+)'", self.php)
        self.assertEqual(sorted(names), ["onionpress_check_update",
                                         "onionpress_download_backup",
                                         "onionpress_poll_action"])
        self.assertNotIn("wp_ajax_nopriv_", self.php)
        for name in names:
            body = _block(self.php, f"'wp_ajax_{name}'")
            with self.subTest(endpoint=name):
                # The very first statement, after any comment lines.
                first = re.sub(r"^\s*(?://[^\n]*\n\s*)*", "", body[body.index("{") + 1:])
                self.assertTrue(first.startswith(OWNER_GATE), body[:300])
                self.assertLess(body.index(OWNER_GATE), body.index(HOST))

    def test_backup_download_refuses_with_403_before_the_nonce(self):
        body = _block(self.php, "'wp_ajax_onionpress_download_backup'")
        gate = body.index(OWNER_GATE)
        self.assertLess(gate, body.index("check_ajax_referer( 'onionpress_download_backup' )"))
        self.assertLess(gate, body.index("readfile( $filepath )"))
        self.assertIn("'response' => 403", body[gate:gate + 150])

    def test_manage_options_remains_only_where_it_is_per_site(self):
        # The page's entry check (then the owner dispatch below) and the
        # Following handler. Anything new on manage_options gets reviewed.
        self.assertEqual(self.php.count("current_user_can( 'manage_options' )"), 2)
        follow = _block(self.php, "wp_verify_nonce( $_POST['onionpress_follow_nonce']")
        self.assertIn("if ( ! current_user_can( 'manage_options' ) )", follow)
        self.assertNotIn(HOST, follow)
        self.assertNotIn("onionpress_settings_owner_allowed", follow)

    def test_larger_upload_limits_only_for_the_owner(self):
        limit = _block(self.php, "add_filter( 'upload_size_limit'")
        self.assertIn("&& onionpress_settings_owner_allowed()", limit)
        ini = _block(self.php, "@ini_set( 'upload_max_filesize', '512M' )")
        self.assertLess(ini.index("onionpress_settings_owner_allowed()"),
                        ini.index("@ini_set("))


class TestSettingsPageMenu(unittest.TestCase):

    def setUp(self):
        self.php = _read(SETTINGS)
        self.page = _between(self.php, "function onionpress_settings_page() {", "\n}\n")

    def test_menu_is_on_every_site_for_its_administrators(self):
        menu = _block(self.php, "add_action( 'admin_menu'")
        self.assertIn("'manage_options',\n        'onionpress-settings',\n        'onionpress_settings_page',", menu)
        network = _block(self.php, "add_action( 'network_admin_menu'")
        self.assertIn("'manage_network_options',", network)

    def test_page_dispatches_subsite_admins_before_reading_the_host(self):
        gate = self.page.index(OWNER_GATE)
        self.assertLess(self.page.index("if ( ! current_user_can( 'manage_options' ) )"), gate)
        branch = self.page[gate:self.page.index("}", gate)]
        self.assertIn("onionpress_settings_site_page();", branch)
        self.assertIn("return;", branch)
        self.assertLess(gate, self.page.index(HOST))
        self.assertLess(gate, self.page.index("get_option( 'onionpress_archive_s3_access'"))

    def test_owner_page_keeps_following(self):
        self.assertIn("<?php onionpress_settings_render_following(); ?>", self.page)

    def test_subsite_page_is_following_only(self):
        site = _between(self.php, "function onionpress_settings_site_page() {", "\n}\n")
        following = _between(self.php, "function onionpress_settings_render_following() {", "\n}\n")
        self.assertIn("onionpress_settings_render_following();", site)
        for fn_name, body in (("site_page", site), ("render_following", following)):
            for host_thing in (HOST, "onionpress_action", "onionpress_settings_save",
                               "onionpress_download_backup", "onionpress_check_update",
                               "onionpress_poll_action", "upload-analytics",
                               "onionpress_archive_s3", "onionpress-status"):
                with self.subTest(fn=fn_name, thing=host_thing):
                    self.assertNotIn(host_thing, body)
        # The Following form and its handler's nonce are what it carries.
        self.assertIn("wp_nonce_field( 'onionpress_follow_save', 'onionpress_follow_nonce' )", following)
        self.assertIn("get_option( 'onionpress_following'", following)


class TestSharedVolumeCensus(unittest.TestCase):
    """Outside the two owner-gated pages, plugins and the theme only read the
    public onion address and version from the shared volume, or remove
    files from an older layout. The files that make the host act
    (requested-action, config-updates.json, backup and restore files, key
    import, the analytics trigger) are named nowhere else. A new reference
    fails here so it gets the same review."""

    GATED = {"onionpress-settings.php", "onionpress-status.php"}
    # The auto-login plugin's token handling is reviewed on its own.
    SKIP = {"onionpress-auto-login.php"}
    READ_OR_LEGACY = {"onion_address", "version", "hit-counter.txt",
                      "wayback-queue.json", "wayback-archived.json"}

    def test_only_the_gated_pages_name_host_action_files(self):
        roots = [os.path.join(PROJECT_ROOT, "app/Resources/plugins"),
                 os.path.join(PROJECT_ROOT, "app/Resources/themes/onionpress")]
        seen = set()
        for root in roots:
            for dirpath, _dirs, files in os.walk(root):
                for name in files:
                    if not name.endswith(".php") or name in self.GATED | self.SKIP:
                        continue
                    with open(os.path.join(dirpath, name), encoding="utf-8") as f:
                        names = set(re.findall(r"/var/lib/onionpress/([A-Za-z0-9._-]+)", f.read()))
                    with self.subTest(file=name):
                        self.assertLessEqual(names, self.READ_OR_LEGACY)
                    seen |= names
        self.assertIn("onion_address", seen)


if __name__ == "__main__":
    unittest.main()

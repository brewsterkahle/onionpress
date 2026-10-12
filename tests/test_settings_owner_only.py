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

The same rule now covers the other places a subsite administrator or author
reached shared state: the onboarding wizard (network-wide
onionpress_onboarded), the Wayback page (the sweep archives every site with
the owner's archive.org account), the blog importer's WXR snapshots and the
My Creations upload (both write into the owner's ~/OnionPress/Creations).

There is no PHP in the test environment, so these read the source.
"""

import os
import re
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

SETTINGS = "app/Resources/plugins/onionpress-settings.php"
ONBOARDING = "app/Resources/plugins/onionpress-onboarding.php"
WAYBACK = "app/Resources/plugins/onionpress-wayback-archive.php"
SOCIAL = "app/Resources/plugins/onionpress-social-archive.php"
BLOG = "app/Resources/plugins/onionpress-social-archive-blog.php"
CREATIONS = "app/Resources/plugins/onionpress-creations/onionpress-creations.php"
THEME_CREATIONS = "app/Resources/themes/onionpress/page-creations.php"

HOST = "/var/lib/onionpress/"
OWNER_GATE = "if ( ! onionpress_settings_owner_allowed() )"


def _read(rel_path):
    with open(os.path.join(PROJECT_ROOT, rel_path), "r", encoding="utf-8") as f:
        return f.read()


def _between(text, start_marker, end_marker):
    start = text.index(start_marker)
    return text[start:text.index(end_marker, start + len(start_marker))]


def _body(text, signature):
    """A function's body: after *signature* (which ends with its opening
    brace) up to the closing brace at the signature's own indentation."""
    start = text.index(signature) + len(signature)
    line_start = text.rindex("\n", 0, text.index(signature)) + 1
    indent = text[line_start:text.index(signature)]
    return text[start:text.index("\n" + indent + "}\n", start)]


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


class TestOnboardingIsTheOwners(unittest.TestCase):

    def setUp(self):
        self.php = _read(ONBOARDING)

    def test_page_registered_only_for_the_super_admin(self):
        fn = _between(self.php, "function onionpress_onboarding_register_page() {", "\n}\n")
        self.assertLess(fn.index("if ( ! is_super_admin() )"), fn.index("add_submenu_page("))
        self.assertIn("return;", fn[:fn.index("add_submenu_page(")])

    def test_wizard_refuses_before_changing_anything(self):
        fn = _between(self.php, "function onionpress_onboarding_render() {", "    // Render\n")
        gate = fn.index("if ( ! is_super_admin() )")
        self.assertIn("'response' => 403", fn[gate:gate + 200])
        for change in ("onionpress_onboarding_finish()", "update_option(",
                       "wp_set_password(", "wp_set_auth_cookie("):
            with self.subTest(change=change):
                self.assertLess(gate, fn.index(change))

    def test_skip_link_carries_a_nonce(self):
        fn = _between(self.php, "function onionpress_onboarding_render() {", "\n}\n")
        skip = fn[fn.index("if ( isset( $_GET['op_skip'] ) )"):]
        self.assertLess(skip.index("check_admin_referer( 'onionpress-onboarding-skip' )"),
                        skip.index("onionpress_onboarding_finish()"))
        self.assertIn("wp_nonce_url( admin_url( 'admin.php?page=onionpress-onboarding&op_skip=1' ), "
                      "'onionpress-onboarding-skip' )", self.php)


class TestWaybackIsTheOwners(unittest.TestCase):

    def setUp(self):
        self.php = _read(WAYBACK)

    def test_no_manage_options_gate_left(self):
        self.assertNotIn("current_user_can( 'manage_options' )", self.php)

    def test_menu_registered_only_for_the_super_admin(self):
        menu = _block(self.php, "'onionpress_wayback_admin_page'")
        gate = menu.index("if ( ! is_super_admin() )")
        self.assertLess(gate, menu.index("add_menu_page("))
        self.assertLess(gate, menu.index("add_submenu_page("))

    def test_actions_refuse_before_touching_the_sweep(self):
        body = _block(self.php, "add_action( 'admin_post_onionpress_wayback_action'")
        gate = body.index("if ( ! is_super_admin() )")
        self.assertIn("wp_die( 'Forbidden', 403 );", body[gate:gate + 80])
        for change in ("check_admin_referer(", "delete_option(", "wp_schedule_single_event(",
                       "wp_remote_post("):
            with self.subTest(change=change):
                self.assertLess(gate, body.index(change))

    def test_page_and_notice_are_the_owners(self):
        page = _body(self.php, "function onionpress_wayback_admin_page() {")
        self.assertTrue(page.lstrip().startswith("if ( ! is_super_admin() )"))
        notice = _block(self.php, "Wayback archiving is not running.")
        self.assertTrue(notice.split("{", 1)[1].lstrip().startswith("if ( ! is_super_admin() )"))

    def test_social_dashboard_links_the_page_only_for_the_owner(self):
        php = _read(SOCIAL)
        link = php.index('<a href="<?php echo esc_url( $wb_details ); ?>"')
        self.assertIn("<?php if ( is_super_admin() ) : ?>", php[link - 200:link])


class TestBlogSnapshotsAreTheOwners(unittest.TestCase):

    def setUp(self):
        self.php = _read(BLOG)

    def test_allowed_is_the_super_admin(self):
        fn = _between(self.php, "function onionpress_blog_snapshots_allowed() {", "\n}\n")
        self.assertIn("return is_super_admin();", fn)

    def test_upload_and_delete_go_through_it(self):
        page = _between(self.php, "function onionpress_blog_import_page() {", "\n    $url ")
        for handler in ("onionpress_blog_handle_wxr_upload()", "onionpress_blog_handle_wxr_delete()"):
            with self.subTest(handler=handler):
                at = page.index(handler)
                self.assertIn("$notice = onionpress_blog_snapshots_allowed()\n                ? ",
                              page[at - 80:at])
        self.assertEqual(self.php.count("onionpress_blog_handle_wxr_upload()"), 2)  # def + call
        self.assertEqual(self.php.count("onionpress_blog_handle_wxr_delete()"), 2)

    def test_download_and_listing_are_the_owners(self):
        dl = _between(self.php, "function onionpress_blog_serve_wxr_download() {", "\n}\n")
        self.assertLess(dl.index("! onionpress_blog_snapshots_allowed()"), dl.index("readfile("))
        listing = _body(self.php, "function onionpress_blog_render_snapshots() {")
        self.assertTrue(listing.lstrip().startswith("if ( ! onionpress_blog_snapshots_allowed() )"))

    def test_snapshot_form_shown_only_to_the_owner(self):
        page = _between(self.php, "function onionpress_blog_import_page() {", "\n}\n")
        gate = page.index("<?php if ( ! onionpress_blog_snapshots_allowed() ) : ?>")
        other = page.index("<?php else : ?>", gate)
        self.assertLess(other, page.index("name=\"onionpress_blog_upload_wxr\""))
        self.assertLess(other, page.index("<?php onionpress_blog_render_snapshots(); ?>"))


class TestCreationsUploadIsTheOwners(unittest.TestCase):

    def setUp(self):
        self.php = _read(CREATIONS)

    def test_can_upload_is_the_super_admin(self):
        fn = _between(self.php, "public static function can_upload() {", "\n    }\n")
        self.assertIn("return is_user_logged_in() && is_super_admin();", fn)

    def test_upload_handler_checks_it_before_writing(self):
        start = self.php.index("public function handle_upload()")
        body = self.php[start:self.php.index("public function ", start + 1)]
        self.assertNotIn("upload_files", body)
        self.assertLess(body.index("if (!self::can_upload())"), body.index("move_uploaded_file("))

    def test_theme_shows_the_form_only_to_the_owner(self):
        tpl = _read(THEME_CREATIONS)
        self.assertNotIn("upload_files", tpl)
        form = tpl.index('id="creations-upload-form"')
        self.assertIn("OnionPress_Creations::can_upload()", tpl[form - 300:form])


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

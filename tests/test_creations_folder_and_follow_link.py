#!/usr/bin/env python3
"""My Creations folder names with quotes, the upload's return, and the
admin bar's Follow link (issue #325).

  - WordPress adds backslashes to request data, so `?folder=Bob's photos`
    arrived as `Bob\\'s photos` and matched no folder; an upload into it
    went nowhere. Both the template's `folder` and the upload's
    `creations_upload_folder` are wp_unslash()ed before use.
  - The upload form posts back to the My Creations page, so the referer
    equals the request URI and wp_get_referer() returns false: every upload
    landed on the home page. handle_upload() now redirects to the page's
    own permalink, keeping the folder.
  - The Follow node in the admin bar links to the OnionPress Settings page,
    registered with manage_options; it is added only for users who hold it.

There is no PHP in the test environment, so these read the source.
"""

import os
import re
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CREATIONS = os.path.join(PROJECT_ROOT, "app", "Resources", "plugins",
                         "onionpress-creations", "onionpress-creations.php")
THEME_CREATIONS = os.path.join(PROJECT_ROOT, "app", "Resources", "themes",
                               "onionpress", "page-creations.php")
LOGIN_FIX = os.path.join(PROJECT_ROOT, "app", "Resources", "plugins",
                         "onionpress-login-fix.php")
SETTINGS = os.path.join(PROJECT_ROOT, "app", "Resources", "plugins",
                        "onionpress-settings.php")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _php_function(src, name):
    m = re.search(r"function " + re.escape(name) + r"\s*\([^)]*\)\s*\{", src)
    assert m, f"function {name} not found"
    depth, i = 0, m.end() - 1
    while True:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[m.end():i]
        i += 1


class TestFolderNamesAreUnslashed(unittest.TestCase):
    def test_template_folder(self):
        src = _read(THEME_CREATIONS)
        self.assertIn("sanitize_text_field(wp_unslash($_GET['folder']))", src)
        self.assertNotRegex(src, r"sanitize_text_field\(\$_GET\[")

    def test_upload_folder(self):
        body = _php_function(_read(CREATIONS), "handle_upload")
        self.assertIn(
            "sanitize_text_field(wp_unslash($_POST['creations_upload_folder']))",
            body)

    def test_no_request_value_is_used_slashed(self):
        for path in (CREATIONS, THEME_CREATIONS):
            src = _read(path)
            for m in re.finditer(r"sanitize_text_field\(\s*\$_(GET|POST|REQUEST)\[", src):
                self.fail(f"{os.path.basename(path)}: {m.group(0)} without wp_unslash")


class TestUploadReturnsToTheFolder(unittest.TestCase):
    def setUp(self):
        self.body = _php_function(_read(CREATIONS), "handle_upload")

    def test_does_not_rely_on_the_referer(self):
        self.assertNotIn("wp_get_referer(", self.body.replace("Not wp_get_referer():", ""))

    def test_redirects_to_the_page_keeping_the_folder(self):
        self.assertIn("get_permalink($page_id)", self.body)
        self.assertIn("add_query_arg('folder', urlencode($subfolder), $back)",
                      self.body)
        self.assertIn("wp_safe_redirect($back);", self.body)
        # The template builds its folder links the same way.
        self.assertIn("add_query_arg('folder', urlencode(",
                      _read(THEME_CREATIONS))


class TestFollowLinkOnlyForThoseWhoCanOpenIt(unittest.TestCase):
    def test_settings_page_capability(self):
        # The page the link opens; if this changes, change the gate below.
        m = re.search(r"add_menu_page\(.*?'(\w+)',\s*'onionpress-settings'",
                      _read(SETTINGS), re.S)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "manage_options")

    def test_node_is_gated(self):
        src = _read(LOGIN_FIX)
        hook = src.index("add_action( 'admin_bar_menu'")
        gate = src.index("if ( ! current_user_can( 'manage_options' ) )", hook)
        node = src.index("'id'     => 'onionpress-follow'", hook)
        self.assertLess(gate, node)
        self.assertIn("admin.php?page=onionpress-settings", src[node:])


if __name__ == "__main__":
    unittest.main()

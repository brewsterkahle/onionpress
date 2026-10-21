#!/usr/bin/env python3
"""The OnionPress theme renders a post's replies itself.

Without a comments.php, WordPress fell back to its own theme-compat
template and logged a deprecation on every single-post view. The theme's
template must not show avatars: onionpress-avatar.php serves one only for
the site's own users, and for every other commenter (imported Mastodon and
Bluesky replies included) get_avatar() points the visitor's browser at
Gravatar, a clearnet request on every page view.
"""

import os
import re
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
THEME = os.path.join(ROOT, "app", "Resources", "themes", "onionpress")


def _read(name):
    with open(os.path.join(THEME, name), encoding="utf-8") as f:
        return f.read()


def _code(src):
    """PHP source without its docblocks and comment lines."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(line for line in src.splitlines()
                     if not line.lstrip().startswith(("//", "#", "*")))


class TestThemeCommentsTemplate(unittest.TestCase):

    def setUp(self):
        self.code = _code(_read("comments.php"))

    def test_single_post_uses_it(self):
        self.assertIn("comments_template()", _read("single.php"))

    def test_password_protected_posts_keep_their_replies(self):
        guard = self.code.index("post_password_required()")
        self.assertLess(guard, self.code.index("wp_list_comments("))
        self.assertIn("return;", self.code[guard:guard + 80])

    def test_anchor_the_archive_links_to(self):
        # index.php's thread preview links to get_permalink() . '#comments'.
        self.assertIn("'#comments'", _read("index.php"))
        self.assertRegex(self.code, r'<section id="comments"')

    def test_no_avatars(self):
        m = re.search(r"wp_list_comments\(array\((.*?)\)\);", self.code, re.S)
        self.assertIsNotNone(m, "comments.php must list replies with wp_list_comments")
        self.assertRegex(m.group(1), r"'avatar_size'\s*=>\s*0\b")
        self.assertNotIn("get_avatar", self.code)

    def test_reply_script_only_where_replies_thread(self):
        functions = _code(_read("functions.php"))
        self.assertRegex(
            functions,
            r"if \(is_singular\(\) && comments_open\(\) && get_option\('thread_comments'\)\) \{\s*"
            r"wp_enqueue_script\('comment-reply'\);",
        )


if __name__ == "__main__":
    unittest.main()

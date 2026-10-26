"""Issue #403: imported bodies go through wp_kses_post() at ingest.

Mastodon stored the instance's `content` verbatim and the blog importer
ran a regex sanitizer that missed entity-encoded `jav&#x61;script:`
hrefs, `<img/onerror=...>` without whitespace, <base> and <meta refresh>.
What protected the site was that both importers run under wp-cron, where
no user is set and WordPress's save-time kses is on; the_content never
runs kses. Each foreign HTML field is now passed through wp_kses_post()
explicitly, where it is read, and the regex sanitizer is gone.

Bluesky, Twitter and Reddit build their HTML from escaped text
(esc_html on the body, esc_url/esc_attr on what they add) and store no
foreign HTML; a census keeps it so.
"""

import os
import re
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGINS = os.path.join(PROJECT_ROOT, "app/Resources/plugins")


def _read(name):
    with open(os.path.join(PLUGINS, name), encoding="utf-8") as f:
        return f.read()


def _function(text, name):
    start = text.index(f"function {name}(")
    return text[start:text.index("\n}\n", start)]


class TestMastodon(unittest.TestCase):

    def setUp(self):
        self.text = _read("onionpress-social-archive-mastodon.php")
        self.render = _function(self.text, "onionpress_mastodon_render_content")

    def test_both_content_fields_are_filtered_where_read(self):
        self.assertIn("$inner = wp_kses_post( (string) ( $status['reblog']['content'] ?? '' ) );",
                      self.render)
        self.assertIn("$html = wp_kses_post( (string) ( $status['content'] ?? '' ) );",
                      self.render)
        # No other read of a content field.
        self.assertEqual(self.render.count("['content']"), 2)

    def test_nothing_else_reads_a_status_body(self):
        rest = self.text.replace(self.render, "")
        for field in ("['content']", "['reblog']['content']"):
            with self.subTest(field=field):
                self.assertNotIn(f"$status{field}", rest)


class TestBlog(unittest.TestCase):

    def setUp(self):
        self.text = _read("onionpress-social-archive-blog.php")
        self.render = _function(self.text, "onionpress_blog_render_content")

    def test_the_body_is_filtered_where_read(self):
        self.assertIn("$html = wp_kses_post( (string) ( $post['content']['rendered'] ?? '' ) );",
                      self.render)

    def test_the_regex_sanitizer_is_gone(self):
        self.assertNotIn("strip_dangerous_html", self.text)
        self.assertNotIn("preg_replace( '#<(script", self.text)

    def test_nothing_else_reads_the_rendered_body(self):
        rest = self.text.replace(self.render, "")
        self.assertNotIn("['content']['rendered']", rest)

    def test_both_insert_and_update_use_the_renderer(self):
        for fn in ("onionpress_blog_import_post", "onionpress_blog_update_post"):
            with self.subTest(function=fn):
                self.assertIn("onionpress_blog_render_content( $post )", _function(self.text, fn))


class TestOtherImportersStoreNoForeignHtml(unittest.TestCase):
    """The census: a post_content built by these importers is made of
    esc_html()'d text and markup of their own."""

    def test_bluesky_text_is_escaped(self):
        text = _read("onionpress-social-archive-bluesky.php")
        fn = _function(text, "onionpress_bluesky_render_text_with_facets")
        self.assertIn("esc_html( $text )", fn)
        self.assertNotIn("['record']['html']", text)

    def test_twitter_text_is_escaped(self):
        text = _read("onionpress-social-archive-twitter.php")
        fn = _function(text, "onionpress_twitter_render_content")
        self.assertIn("esc_html(", fn)

    def test_reddit_markdown_is_escaped_first(self):
        text = _read("onionpress-social-archive-reddit.php")
        self.assertRegex(text, r"\$text = esc_html\( \$text \);")

    def test_no_importer_stores_a_rendered_html_field_unfiltered(self):
        for name in sorted(os.listdir(PLUGINS)):
            if not name.startswith("onionpress-social-archive-") or not name.endswith(".php"):
                continue
            text = _read(name)
            for m in re.finditer(r"\(string\) \( \$\w+(\[[^\]]+\])*\['(content|html|rendered)'\][^;]*;",
                                 text):
                line = text[text.rfind("\n", 0, m.start()) + 1:m.end()]
                # The function the read is in: filtered there, or cut to
                # plain text there (titles, excerpts).
                fn_start = text.rfind("\nfunction ", 0, m.start())
                fn = text[fn_start:text.index("\n}\n", m.end())]
                with self.subTest(file=name, line=line.strip()):
                    self.assertTrue("wp_kses_post(" in line or "wp_strip_all_tags(" in fn,
                                    "a foreign HTML field stored without wp_kses_post()")


if __name__ == "__main__":
    unittest.main()

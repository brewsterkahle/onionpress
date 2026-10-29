"""Issue #403: imported bodies go through wp_kses_post() at ingest.

Mastodon stored the instance's `content` verbatim and the blog importer
ran a regex sanitizer that missed entity-encoded `jav&#x61;script:`
hrefs, `<img/onerror=...>` without whitespace, <base> and <meta refresh>.
What protected the site was that both importers run under wp-cron, where
no user is set and WordPress's save-time kses is on; the_content never
runs kses. Each foreign HTML field is now passed through wp_kses_post()
explicitly, where it is read, and the regex sanitizer is gone.

Comments (#462): wp_insert_comment() runs no comment filter, so every
importer comment goes through onionpress_social_insert_comment(), which
cleans author, author URL and content before they are stored.

Bluesky, Twitter and Reddit build their HTML from escaped text
(esc_html on the body, esc_url/esc_attr on what they add) and store no
foreign HTML; a census keeps it so.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
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
        # Escaped before anything adds markup, not just somewhere (#462).
        text = _read("onionpress-social-archive-twitter.php")
        fn = _function(text, "onionpress_twitter_render_content")
        body = fn[fn.index("{") + 1:].lstrip()
        while body.startswith("//"):
            body = body[body.index("\n") + 1:].lstrip()
        self.assertTrue(body.startswith("$text = esc_html( html_entity_decode("), body[:80])

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


class TestImporterCommentsAreSanitized(unittest.TestCase):
    """wp_insert_comment() runs no comment filter and comment_text is not
    kses'd on output; every importer comment goes through one helper that
    cleans the author, its URL and the content (#462)."""

    HELPER = "onionpress_social_insert_comment"

    def test_the_helper_cleans_every_stored_field(self):
        fn = _function(_read("onionpress-social-archive.php"), self.HELPER)
        for line in (
                "$args['comment_author']     = sanitize_text_field(",
                "$args['comment_author_url'] = esc_url_raw(",
                "$args['comment_content']    = wp_kses_post(",
                "return wp_insert_comment( $args );"):
            with self.subTest(line=line):
                self.assertIn(line, fn)

    def test_no_plugin_inserts_a_comment_any_other_way(self):
        for name in sorted(os.listdir(PLUGINS)):
            if not name.endswith(".php"):
                continue
            text = _read(name)
            if name == "onionpress-social-archive.php":
                text = text.replace(_function(text, self.HELPER), "")
            text = re.sub(r"/\*.*?\*/|//[^\n]*", "", text, flags=re.S)
            with self.subTest(file=name):
                self.assertIsNone(re.search(r"\b(wp_insert_comment|wp_new_comment)\s*\(", text))

    def test_every_importer_comment_uses_the_helper(self):
        for name, count in (("onionpress-social-archive-twitter.php", 2),
                            ("onionpress-social-archive-bluesky.php", 2),
                            ("onionpress-social-archive-mastodon.php", 2)):
            with self.subTest(file=name):
                self.assertEqual(_read(name).count(f"{self.HELPER}( array("), count)


@unittest.skipUnless(shutil.which("php"), "php not installed")
class TestTwitterRendererRuns(unittest.TestCase):
    """onionpress_twitter_render_content under php, with WordPress's
    escapers stubbed by htmlspecialchars."""

    def _render(self, tweet):
        fn = _function(_read("onionpress-social-archive-twitter.php"),
                       "onionpress_twitter_render_content")
        script = ("<?php\n"
                  "function esc_html($s) { return htmlspecialchars($s, ENT_QUOTES, 'UTF-8'); }\n"
                  "function esc_url($s) { return htmlspecialchars($s, ENT_QUOTES, 'UTF-8'); }\n"
                  + fn + "\n}\n"
                  "echo onionpress_twitter_render_content(json_decode(stream_get_contents(STDIN), true));\n")
        with tempfile.NamedTemporaryFile("w", suffix=".php", delete=False) as f:
            f.write(script)
        self.addCleanup(os.remove, f.name)
        return subprocess.run(["php", f.name], input=json.dumps(tweet), text=True,
                              capture_output=True, check=True).stdout

    def test_markup_in_the_text_is_escaped(self):
        out = self._render({"full_text": "<img src=x onerror=alert(1)> &lt;b&gt;hi&lt;/b&gt;"})
        self.assertNotIn("<img", out)
        self.assertNotIn("<b>", out)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", out)
        self.assertIn("&lt;b&gt;hi&lt;/b&gt;", out)

    def test_links_and_mentions_still_render(self):
        out = self._render({
            "full_text": "hi @Alice see https://t.co/abc & more\nbye",
            "entities": {
                "urls": [{"url": "https://t.co/abc", "expanded_url": "https://example.org/x",
                          "display_url": "example.org/x"}],
                "user_mentions": [{"screen_name": "Alice"}]}})
        self.assertIn('<a href="https://example.org/x" rel="nofollow noopener">example.org/x</a>', out)
        self.assertIn('<a href="https://twitter.com/Alice" rel="nofollow noopener">@Alice</a>', out)
        self.assertIn("&amp; more<br />", out)


if __name__ == "__main__":
    unittest.main()

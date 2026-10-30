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

Titles and excerpts (#472): the_title() and the_excerpt() run no kses
either, and the save-time title_save_pre kses that stripped a Reddit
thread's `<img onerror>` title under wp-cron is off when the owner
(unfiltered_html) clicks Sync now or uploads a ZIP. Every foreign title
and excerpt (Reddit thread titles fetched with raw_json=1 and cached,
the "Re: " titles, the blog importer's title.rendered and
excerpt.rendered, the previews cut from bodies) is now
onionpress_social_plain_text(): sanitize_text_field(wp_strip_all_tags())
and never html_entity_decode() afterwards, which turned `&lt;script&gt;`
back into markup. A census holds every post_title / post_excerpt to it.
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


class TestTitlesAndExcerptsArePlainText(unittest.TestCase):
    """Issue #472: every foreign title and excerpt is plain text at
    ingest, through one helper, and nothing decodes entities after
    the tags are stripped."""

    HELPER = "onionpress_social_plain_text"
    RENDERERS = re.compile(r"onionpress_\w+_render_(?:content|title|excerpt)\(")

    def _importers(self):
        for name in sorted(os.listdir(PLUGINS)):
            if name.startswith("onionpress-social-archive-") and name.endswith(".php"):
                yield name, _read(name)

    def test_the_helper_strips_then_sanitizes_and_never_decodes(self):
        fn = _function(_read("onionpress-social-archive.php"), self.HELPER)
        self.assertIn("return sanitize_text_field( wp_strip_all_tags( (string) $text ) );", fn)
        self.assertNotIn("html_entity_decode", fn)

    def test_no_importer_decodes_entities_after_stripping_tags(self):
        for name, text in self._importers():
            with self.subTest(file=name):
                self.assertNotIn("html_entity_decode( wp_strip_all_tags(", text)
                # The one decode left is the Twitter body's, before esc_html.
                for m in re.finditer(r"html_entity_decode\(", text):
                    line = text[text.rfind("\n", 0, m.start()) + 1:m.end()]
                    self.assertIn("esc_html( html_entity_decode(", line, line.strip())

    def test_every_post_title_and_excerpt_comes_from_the_helper(self):
        """The census: a 'post_title' / 'post_excerpt' value is either an
        attachment's sanitize_title(), or built in a function that calls
        the helper itself or a renderer whose body calls it."""
        seen = 0
        for name, text in self._importers():
            for m in re.finditer(r"'post_(?:title|excerpt)'\s*=>\s*([^\n]+)", text):
                seen += 1
                value = m.group(1)
                fn_start = text.rfind("\nfunction ", 0, m.start())
                fn = text[fn_start:text.index("\n}\n", m.end())]
                with self.subTest(file=name, value=value.strip()):
                    if "sanitize_title(" in value:
                        continue
                    if f"{self.HELPER}(" in fn:
                        continue
                    renderers = [r[:-1] for r in self.RENDERERS.findall(fn)]
                    self.assertTrue(renderers, "no plain-text source in the function")
                    for renderer in renderers:
                        self.assertIn(f"{self.HELPER}(", _function(text, renderer),
                                      f"{renderer} does not make plain text")
        self.assertGreaterEqual(seen, 14)

    def test_reddit_thread_titles_are_plain_text_when_fetched_cached_and_applied(self):
        text = _read("onionpress-social-archive-reddit.php")
        fetch = _function(text, "onionpress_reddit_fetch_thread_titles")
        self.assertIn("$out[ $id ] = onionpress_social_plain_text( $child['data']['title'] ?? '' );", fetch)
        self.assertNotIn("(string) ( $child['data']['title']", fetch)
        cache = _function(text, "onionpress_reddit_get_title_cache")
        self.assertIn("$c[ $id ] = onionpress_social_plain_text( $title );", cache)
        resolve = _function(text, "onionpress_reddit_resolve_titles_batch")
        self.assertIn("$title = onionpress_social_plain_text( $cache[ $link_id ] );", resolve)
        self.assertIn("'post_title' => 'Re: ' . $title", resolve)

    def test_reddit_csv_and_api_titles_are_plain_text(self):
        text = _read("onionpress-social-archive-reddit.php")
        submission = _function(text, "onionpress_reddit_import_submission")
        self.assertIn("$title     = onionpress_social_plain_text( $row['title'] ?? '' );", submission)
        self.assertIn("$title = wp_trim_words( onionpress_social_plain_text( $content ), 10, '…' );",
                      submission)
        comment = _function(text, "onionpress_reddit_import_comment")
        self.assertIn("$title = wp_trim_words( onionpress_social_plain_text( $body_md ), 10, '…' );",
                      comment)
        insert = _function(text, "onionpress_reddit_insert_post")
        self.assertIn("$title   = onionpress_social_plain_text( $title );", insert)
        self.assertIn("$excerpt = wp_trim_words( onionpress_social_plain_text( $content ), 35, '…' );",
                      insert)
        # No raw title read anywhere else.
        rest = text.replace(submission, "").replace(_function(text, "onionpress_reddit_api_post_to_row"), "")
        self.assertNotIn("$row['title']", rest)

    def test_blog_title_and_excerpt_renderers(self):
        text = _read("onionpress-social-archive-blog.php")
        self.assertIn("return onionpress_social_plain_text( $post['title']['rendered'] ?? '' );",
                      _function(text, "onionpress_blog_render_title"))
        self.assertIn("$excerpt = onionpress_social_plain_text( $post['excerpt']['rendered'] ?? '' );",
                      _function(text, "onionpress_blog_render_excerpt"))
        self.assertIn("$preview = onionpress_social_plain_text( $html );",
                      _function(text, "onionpress_blog_render_content"))
        rest = text
        for fn in ("onionpress_blog_render_title", "onionpress_blog_render_excerpt"):
            rest = rest.replace(_function(text, fn), "")
        for field in ("['title']['rendered']", "['excerpt']['rendered']"):
            self.assertNotIn(field, rest)

    def test_body_previews_are_plain_text(self):
        for name, fn, line in (
                ("onionpress-social-archive-twitter.php", "onionpress_twitter_import_tweet",
                 "$preview_text = onionpress_social_plain_text( $content );"),
                ("onionpress-social-archive-bluesky.php", "onionpress_bluesky_render_content",
                 "$preview = onionpress_social_plain_text( $text );"),
                ("onionpress-social-archive-mastodon.php", "onionpress_mastodon_render_content",
                 "$preview = onionpress_social_plain_text( $html );")):
            with self.subTest(file=name):
                self.assertIn(line, _function(_read(name), fn))


# WordPress's own wp_strip_all_tags() and sanitize_text_field() (the
# _sanitize_text_fields() body with keep_newlines false; the UTF-8 check
# left out), so the PHP half runs what the site runs.
WP_TEXT_STUBS = r"""
function esc_html( $s ) { return htmlspecialchars( $s, ENT_QUOTES, 'UTF-8', false ); }
function wp_strip_all_tags( $text, $remove_breaks = false ) {
    $text = preg_replace( '@<(script|style)[^>]*?>.*?</\\1>@si', '', $text );
    $text = strip_tags( $text );
    if ( $remove_breaks ) { $text = preg_replace( '/[\r\n\t ]+/', ' ', $text ); }
    return trim( $text );
}
function wp_pre_kses_less_than( $content ) {
    return preg_replace_callback( '%<[^>]*?((?=<)|>|$)%', function ( $m ) {
        if ( false === strpos( $m[0], '>' ) ) { return esc_html( $m[0] ); }
        return $m[0];
    }, $content );
}
function sanitize_text_field( $str ) {
    if ( is_object( $str ) || is_array( $str ) ) { return ''; }
    $filtered = (string) $str;
    if ( str_contains( $filtered, '<' ) ) {
        $filtered = wp_pre_kses_less_than( $filtered );
        $filtered = wp_strip_all_tags( $filtered, false );
        $filtered = str_replace( "<\n", "&lt;\n", $filtered );
    }
    $filtered = preg_replace( '/[\r\n\t ]+/', ' ', $filtered );
    $filtered = trim( $filtered );
    $filtered = preg_replace( '/[\x00-\x08\x0B\x0C\x0E-\x1F]/', '', $filtered );
    $found = false;
    while ( preg_match( '/%[a-f0-9]{2}/i', $filtered, $match ) ) {
        $filtered = str_replace( $match[0], '', $filtered );
        $found = true;
    }
    if ( $found ) { $filtered = trim( preg_replace( '/ +/', ' ', $filtered ) ); }
    return $filtered;
}
function wp_trim_words( $text, $num_words = 55, $more = '…' ) {
    $text  = wp_strip_all_tags( $text );
    $words = preg_split( "/[\n\r\t ]+/", $text, $num_words + 1, PREG_SPLIT_NO_EMPTY );
    if ( count( $words ) > $num_words ) { array_pop( $words ); return implode( ' ', $words ) . $more; }
    return implode( ' ', $words );
}
function wp_kses_post( $s ) { return $s; }
function get_option( $k, $d = array() ) { return $GLOBALS['opt']; }
function apply_filters( $tag, $value ) { return $value; }
function is_wp_error( $x ) { return false; }
function onionpress_reddit_api_get( $url ) { return $GLOBALS['api']; }
const ONIONPRESS_REDDIT_API_HOST = 'www.reddit.com';
const ONIONPRESS_REDDIT_TITLE_CACHE_OPT = 'x';
"""

FOREIGN_TITLES = {
    "entity": "&lt;script&gt;alert(1)&lt;/script&gt; Hello",
    "tag": "<img src=x onerror=alert(1)>Hello",
    "less_than": "1 < 2 and Don&#8217;t <b>stop</b>",
}


@unittest.skipUnless(shutil.which("php"), "php not installed")
class TestPlainTextTitlesRun(unittest.TestCase):
    """The helper and the title paths under php, with WordPress's own
    text functions: no `<` survives, an entity-encoded tag stays text."""

    def _run(self, functions, body):
        script = "<?php\n" + WP_TEXT_STUBS
        for name, fn in functions:
            script += _function(_read(name), fn) + "\n}\n"
        script += body
        with tempfile.NamedTemporaryFile("w", suffix=".php", delete=False) as f:
            f.write(script)
        self.addCleanup(os.remove, f.name)
        proc = subprocess.run(["php", f.name], input=json.dumps(FOREIGN_TITLES), text=True,
                              capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        return json.loads(proc.stdout)

    def _check(self, out):
        for key, title in out.items():
            with self.subTest(title=key):
                self.assertNotIn("<", title)
                self.assertNotIn(">", title)
                self.assertIn("Hello" if key != "less_than" else "stop", title)
        self.assertIn("&lt;script&gt;", out["entity"])
        self.assertIn("Don&#8217;t", out["less_than"])

    def test_the_helper(self):
        out = self._run(
            [("onionpress-social-archive.php", "onionpress_social_plain_text")],
            "$in = json_decode(stream_get_contents(STDIN), true);\n"
            "echo json_encode(array_map('onionpress_social_plain_text', $in));\n")
        self._check(out)

    def test_blog_title_excerpt_and_preview(self):
        out = self._run(
            [("onionpress-social-archive.php", "onionpress_social_plain_text"),
             ("onionpress-social-archive-blog.php", "onionpress_blog_render_title"),
             ("onionpress-social-archive-blog.php", "onionpress_blog_render_excerpt"),
             ("onionpress-social-archive-blog.php", "onionpress_blog_render_content")],
            "$in = json_decode(stream_get_contents(STDIN), true);\n"
            "$out = array();\n"
            "foreach ($in as $k => $t) {\n"
            "  $post = array('title' => array('rendered' => $t), 'excerpt' => array('rendered' => $t),"
            " 'content' => array('rendered' => '<p>' . $t . '</p>'));\n"
            "  $out[$k] = onionpress_blog_render_title($post);\n"
            "  $out['excerpt_' . $k] = onionpress_blog_render_excerpt($post, '');\n"
            "  list($html, $preview) = onionpress_blog_render_content($post);\n"
            "  $out['preview_' . $k] = $preview;\n"
            "}\n"
            "echo json_encode($out);\n")
        self._check({k: v for k, v in out.items() if k in FOREIGN_TITLES})
        for k, v in out.items():
            with self.subTest(field=k):
                self.assertNotIn("<", v)

    def test_reddit_titles_fetched_and_cached(self):
        out = self._run(
            [("onionpress-social-archive.php", "onionpress_social_plain_text"),
             ("onionpress-social-archive-reddit.php", "onionpress_reddit_fetch_thread_titles"),
             ("onionpress-social-archive-reddit.php", "onionpress_reddit_get_title_cache")],
            "$in = json_decode(stream_get_contents(STDIN), true);\n"
            "$kids = array();\n"
            "foreach ($in as $k => $t) { $kids[] = array('data' => array('name' => 't3_' . $k, 'title' => $t)); }\n"
            "$GLOBALS['api'] = array('code' => 200, 'json' => array('data' => array('children' => $kids)));\n"
            "$fetched = onionpress_reddit_fetch_thread_titles(array_keys($in));\n"
            "$GLOBALS['opt'] = $in;  // a cache written before #472\n"
            "$cached = onionpress_reddit_get_title_cache();\n"
            "$out = array();\n"
            "foreach ($in as $k => $t) { $out[$k] = $fetched['t3_' . $k]; $out['cached_' . $k] = $cached[$k]; }\n"
            "echo json_encode($out);\n")
        self._check({k: v for k, v in out.items() if not k.startswith("cached_")})
        self._check({k[7:]: v for k, v in out.items() if k.startswith("cached_")})


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""My Creations publishes what its pages list, never hidden files.

The Creations folder is the host's ~/OnionPress/Creations/My Creations. A
publisher can keep a .git/ there (a static-site checkout), and Finder drops
.DS_Store files into it. The page listing skipped dotfiles, but:

  - `?onionpress_creation=<path>` served any file inside the folder,
    .git/config and .git/logs/HEAD (author names and emails) included;
  - get_files() descended into hidden folders, so .git/HEAD and friends
    showed up on the Creations page and the home page's recent creations;
  - `?folder=.git` browsed a hidden folder, and the upload form accepted
    one as its target;
  - Apache itself served /wp-content/creations/.git/config, since the
    folder sits under DocumentRoot.

Now no component of a served, browsed or upload path may start with '.',
checked on the request and again on the realpath()ed result (a symlink
cannot reach one either). The one exception is the generated thumbnail,
`.thumbs/<listed path>.png`, which the pages link to themselves. The
multisite .htaccess answers 403 for any hidden component of a direct URL.

php is preinstalled on ubuntu-24.04, so the PHP half runs in CI; locally
it is skipped when php is missing.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PLUGIN = os.path.join(PROJECT_ROOT, "app", "Resources", "plugins",
                      "onionpress-creations", "onionpress-creations.php")
MENUBAR = os.path.join(PROJECT_ROOT, "src", "menubar.py")
PHP = shutil.which("php")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _php_function(src, name):
    """Body of PHP function *name* (from its `{` to the matching `}`)."""
    m = re.search(r"function " + re.escape(name) + r"\s*\([^)]*\)\s*\{", src)
    assert m, f"function {name} not found in the plugin"
    depth, i = 0, m.end() - 1
    while True:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[m.end():i]
        i += 1


PUBLIC = ["a.png", "sub/a.png", "sub/deeper/My File.pdf", "x.tar.gz"]
HIDDEN = [".git/config", ".git/logs/HEAD", ".DS_Store", "sub/.DS_Store",
          "sub/.git/HEAD", "./a.png", "../a.png", "sub/../a.png", "",
          "a//b.png", "/a.png", "a.png/", ".thumbs/x", "a\0.png"]
THUMBS = [".thumbs/a.png.png", ".thumbs/sub/a.mov.png"]
HIDDEN_THUMBS = [".thumbs/.git/x.png.png", ".thumbs/sub/.hidden.png.png",
                 ".thumbs/.png", ".thumbs/a.png.txt"]


@unittest.skipUnless(PHP, "php not installed")
class TestPathRuleUnderPhp(unittest.TestCase):

    def _ask(self, method, paths):
        tmp = tempfile.mkdtemp(prefix="onionpress-creations-test-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        harness = textwrap.dedent(f"""\
            <?php
            define('ABSPATH', __DIR__);
            function add_action(...$a) {{}}
            function add_filter(...$a) {{}}
            function register_activation_hook(...$a) {{}}
            require {json.dumps(PLUGIN)};
            $out = [];
            foreach (json_decode(file_get_contents('php://stdin')) as $p) {{
                $out[] = OnionPress_Creations::{method}($p);
            }}
            echo json_encode($out);
        """)
        path = os.path.join(tmp, "harness.php")
        with open(path, "w") as f:
            f.write(harness)
        proc = subprocess.run(
            [PHP, "-d", "error_reporting=E_ALL", "-d", "display_errors=stderr",
             path],
            input=json.dumps(paths), capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("Warning", proc.stderr, proc.stderr)
        return dict(zip(paths, json.loads(proc.stdout)))

    def test_public_path(self):
        got = self._ask("is_public_path", PUBLIC + HIDDEN + THUMBS)
        for p in PUBLIC:
            self.assertTrue(got[p], p)
        for p in HIDDEN + THUMBS:
            self.assertFalse(got[p], p)

    def test_servable_path_allows_only_public_thumbnails(self):
        got = self._ask("is_servable_path",
                        PUBLIC + HIDDEN + THUMBS + HIDDEN_THUMBS)
        for p in PUBLIC + THUMBS:
            self.assertTrue(got[p], p)
        for p in HIDDEN + HIDDEN_THUMBS:
            self.assertFalse(got[p], p)


class TestEveryEntryPointChecks(unittest.TestCase):
    """The rule is applied to the request and to the realpath() result."""

    def setUp(self):
        self.src = _read(PLUGIN)

    def test_serve_file(self):
        body = _php_function(self.src, "serve_file")
        self.assertIn("wp_unslash($_GET['onionpress_creation'])", body)
        self.assertIn("self::is_servable_path($requested)", body)
        self.assertIn("self::is_servable_path($resolved_rel)", body)
        self.assertLess(body.index("is_servable_path($requested)"),
                        body.index("realpath("))
        self.assertLess(body.index("is_servable_path($resolved_rel)"),
                        body.index("self::send_bytes("))

    def test_folder_browsing_and_upload_target(self):
        for fn, resolved in (("get_folder_contents", "$browse_rel"),
                             ("handle_upload", "$target_rel")):
            body = _php_function(self.src, fn)
            self.assertIn("self::is_public_path($subfolder)", body, fn)
            self.assertIn(f"self::is_public_path({resolved})", body, fn)

    def test_get_files_does_not_descend_into_hidden_folders(self):
        body = _php_function(self.src, "scan_tree")
        self.assertIn("RecursiveCallbackFilterIterator", body)
        self.assertIn("getFilename()[0] !== '.'", body)

    def test_thumbnail_urls_match_the_exception(self):
        self.assertIn("const THUMBS_PREFIX = '.thumbs/';", self.src)
        links = re.findall(r"urlencode\('(\.thumbs/)' \. \$rel_path \. '\.png'\)",
                           self.src)
        # One place makes them (listing_entry), for both listings.
        self.assertEqual(links, [".thumbs/"],
                         "thumbnail links moved; keep is_servable_path in step")


class TestDirectUrlRefusesHiddenPaths(unittest.TestCase):
    """The folder is also under DocumentRoot, so Apache would serve
    /wp-content/creations/.git/config itself. The multisite .htaccess
    (rewritten at every start; its copies are kept in sync by
    test_multisite) answers 403 for any hidden component there."""

    def setUp(self):
        import sys
        sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))
        from onionpress import multisite
        self.body = multisite.HTACCESS_BODY
        m = re.search(
            r"^RewriteCond %\{REQUEST_URI\} (/wp-content/creations/\S+) \[NC\]\n"
            r"RewriteRule \^ - \[F\]$", self.body, re.M)
        self.assertIsNotNone(m, "hidden-path deny rule missing from HTACCESS_BODY")
        self.cond = re.compile(m.group(1), re.I)
        self.rule_at = m.start()

    def test_denies_hidden_components(self):
        for path in ("/wp-content/creations/.git/config",
                     "/wp-content/creations/.git/logs/HEAD",
                     "/wp-content/creations/.DS_Store",
                     "/wp-content/creations/sub/.DS_Store",
                     "/wp-content/creations/sub/.git/HEAD",
                     "/wp-content/creations/.thumbs/a.png.png",
                     "/alice/wp-content/creations/.git/config",
                     "/WP-CONTENT/CREATIONS/.git/config"):
            self.assertTrue(self.cond.search(path), path)

    def test_spares_public_files_and_everything_else(self):
        for path in ("/wp-content/creations/a.png",
                     "/wp-content/creations/my.file.tar.gz",
                     "/wp-content/creations/sub/x.y.pdf",
                     "/alice/wp-content/creations/sub/a.png",
                     "/.well-known/x", "/wp-content/uploads/.htaccess-free"):
            self.assertFalse(self.cond.search(path), path)

    def test_runs_before_the_existing_file_passthrough(self):
        self.assertLess(self.rule_at,
                        self.body.index("RewriteCond %{REQUEST_FILENAME} -f"))


class TestThumbnailGeneratorSkipsHiddenFolders(unittest.TestCase):
    def test_walk_prunes_dot_directories(self):
        self.assertIn("dirs[:] = [d for d in dirs if not d.startswith('.')]",
                      _read(MENUBAR))


if __name__ == "__main__":
    unittest.main()

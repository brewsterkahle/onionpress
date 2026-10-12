"""Nothing in wp-content/uploads runs as code.

Uploads sit under DocumentRoot, where the base image hands every *.php to
PHP. WordPress's media upload refuses PHP by MIME type, but importers,
plugins and restored backups write here as well, and a .php that reaches
the folder by any of them was one request away from running as www-data.
Two layers, the same as My Creations (tests/test_creations_no_php.py):

  1. the multisite .htaccess, rewritten at every start, answers 403 for
     server-executable types under /wp-content/uploads/ on every site path
     (multisite.HTACCESS_BODY) — this reaches installs with the app;
  2. the WordPress image turns PHP off for the folder and denies the same
     types (onionpress-uploads.conf) — this reaches installs with the next
     image publish.

Unlike Creations, .htaccess files stay allowed under uploads: plugins keep
private folders there behind a "deny" .htaccess.

And at the source: the social-archive importers, which move files into
uploads themselves, keep only types wp_check_filetype() accepts.
"""

import os
import re
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

from onionpress import derivatives, multisite  # noqa: E402

WP_DIR = os.path.join(ROOT, "app", "Resources", "docker", "wordpress")
CONF = os.path.join(WP_DIR, "onionpress-uploads.conf")
CREATIONS_CONF = os.path.join(WP_DIR, "onionpress-creations.conf")
DOCKERFILE = os.path.join(WP_DIR, "Dockerfile")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _conf_code(path=CONF):
    return "\n".join(line for line in _read(path).splitlines()
                     if not line.lstrip().startswith("#"))


def _block_conds(folder):
    """(uri, ext) regexes of the .htaccess deny block for *folder*."""
    m = re.search(
        r"^RewriteCond %\{REQUEST_URI\} (/wp-content/" + folder + r"/) \[NC\]\n"
        r"RewriteCond %\{REQUEST_URI\} (\S+) \[NC\]\n"
        r"RewriteRule \^ - \[F\]$",
        multisite.HTACCESS_BODY, re.M,
    )
    assert m, f"no {folder} deny block in multisite.HTACCESS_BODY"
    return m.group(1), m.group(2)


def _conf_exts(path):
    m = re.search(r'<FilesMatch "\(\?i\)\\\.\((.+)\)\$">', _read(path))
    assert m, f"no extension FilesMatch in {path}"
    return m.group(1)


class TestHtaccessRule(unittest.TestCase):
    def test_same_extensions_as_creations(self):
        self.assertEqual(_block_conds("uploads")[1], _block_conds("creations")[1])

    def test_rule_matches_every_site_path_into_uploads(self):
        uri_cond, ext_cond = _block_conds("uploads")
        uri, ext = re.compile(uri_cond, re.I), re.compile(ext_cond, re.I)

        def denied(path):
            return bool(uri.search(path) and ext.search(path))

        for path in ("/wp-content/uploads/x.php",
                     "/wp-content/uploads/2026/10/X.PHP",
                     "/alice/wp-content/uploads/2026/10/x.php",
                     "/wp-content/uploads/sites/2/2026/10/x.phtml",
                     "/wp-content/uploads/x.php/extra.jpg",
                     "/wp-content/uploads/x.php5",
                     "/wp-content/uploads/x.phar"):
            self.assertTrue(denied(path), path)
        # Media, and PHP everywhere else, keep working.
        for path in ("/wp-content/uploads/2026/10/pic.jpg",
                     "/wp-content/uploads/sites/2/2026/10/clip.mp4",
                     "/wp-content/uploads/shell.php_.jpg",
                     "/wp-admin/upload.php",
                     "/wp-admin/async-upload.php",
                     "/wp-content/plugins/akismet/akismet.php",
                     "/wp-content/themes/onionpress/functions.php",
                     "/index.php", "/wp-login.php"):
            self.assertFalse(denied(path), path)

    def test_rule_runs_before_the_existing_file_passthrough(self):
        body = multisite.HTACCESS_BODY
        deny = body.index("RewriteCond %{REQUEST_URI} /wp-content/uploads/ [NC]")
        self.assertLess(deny, body.index("RewriteCond %{REQUEST_FILENAME} -f"))
        self.assertLess(deny, body.index("# BEGIN WordPress Multisite"))


class TestImageConf(unittest.TestCase):
    def test_conf_guards_the_uploads_folder(self):
        self.assertIn(f"<Directory {derivatives.CONTAINER_UPLOADS}>", _read(CONF))
        self.assertIn(f"/{os.path.relpath(derivatives.CONTAINER_UPLOADS, '/var/www/html')}/",
                      _block_conds("uploads")[0])

    def test_same_extensions_as_creations(self):
        self.assertEqual(_conf_exts(CONF), _conf_exts(CREATIONS_CONF))

    def test_conf_turns_php_off(self):
        self.assertIn("php_admin_flag engine off", _conf_code())

    def test_conf_leaves_htaccess_files_working(self):
        # Plugins keep private folders under uploads behind a "deny"
        # .htaccess; AllowOverride None would ignore it and serve them.
        self.assertNotRegex(_conf_code(), r"(?m)^\s*AllowOverride\b")
        self.assertNotRegex(_conf_code(), r"(?m)^\s*Options\b")

    def test_conf_is_enabled_and_checked_in_the_image(self):
        dockerfile = _read(DOCKERFILE)
        self.assertIn("COPY onionpress-uploads.conf /etc/apache2/conf-available/",
                      dockerfile)
        enconf = re.search(r"^RUN a2enconf (.+)$", dockerfile, re.M)
        self.assertIsNotNone(enconf)
        self.assertIn("onionpress-uploads", enconf.group(1).split())
        self.assertLess(dockerfile.index("COPY onionpress-uploads.conf"),
                        dockerfile.index("RUN apache2ctl configtest"))


PLUGINS_DIR = os.path.join(ROOT, "app", "Resources", "plugins")


def _php_function(src, name):
    """Body of PHP function *name* (from its `{` to the matching `}`)."""
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


class TestImportersWriteOnlyMedia(unittest.TestCase):
    """The importers put files into uploads with rename()/copy(), not
    wp_handle_sideload(), so WordPress's type check never ran: a blog or
    Mastodon server whose media URL ended in .php, or an archive holding
    `<tweet id>-x.php`, put a .php in uploads under a name the post then
    published. Each now keeps only a type wp_check_filetype() accepts."""

    def _plugin(self, name):
        return _read(os.path.join(PLUGINS_DIR, name))

    def test_remote_media_extension_must_be_a_wordpress_media_type(self):
        for plugin, func in (
                ("onionpress-social-archive-blog.php", "onionpress_blog_sideload_url"),
                ("onionpress-social-archive-mastodon.php", "onionpress_mastodon_sideload_media")):
            with self.subTest(func=func):
                body = _php_function(self._plugin(plugin), func)
                check = "! wp_check_filetype( 'x.' . $ext )['ext']"
                self.assertIn(check, body)
                self.assertLess(body.index(check), body.index("$basename"))
                self.assertLess(body.index(check), body.index("rename("))

    def test_archive_media_name_must_be_a_wordpress_media_type(self):
        body = _php_function(self._plugin("onionpress-social-archive-twitter.php"),
                             "onionpress_twitter_sideload_media")
        check = "if ( ! wp_check_filetype( $f )['ext'] ) continue;"
        self.assertIn(check, body)
        self.assertLess(body.index(check), body.index("copy( $src, $dest_path )"))

    def test_no_other_plugin_writes_into_uploads_directly(self):
        # A new writer must be reviewed (and added above), or go through
        # media_handle_sideload() / wp_handle_upload(), which check types.
        writers = set()
        for dirpath, _, filenames in os.walk(PLUGINS_DIR):
            for name in filenames:
                if name.endswith(".php") and "wp_upload_dir(" in _read(os.path.join(dirpath, name)):
                    writers.add(name)
        self.assertEqual(writers, {"onionpress-social-archive-blog.php",
                                   "onionpress-social-archive-mastodon.php",
                                   "onionpress-social-archive-twitter.php"})


if __name__ == "__main__":
    unittest.main()

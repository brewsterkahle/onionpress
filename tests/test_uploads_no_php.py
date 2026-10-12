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


if __name__ == "__main__":
    unittest.main()

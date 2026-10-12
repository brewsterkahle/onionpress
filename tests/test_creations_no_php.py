"""Nothing in My Creations runs as code.

My Creations is the host folder ~/OnionPress/Creations/My Creations,
bind-mounted under WordPress's DocumentRoot at wp-content/creations, where
the base image hands every *.php to PHP. Three layers keep a .php there
from running as www-data:

  1. the upload form refuses server-executable types
     (BLOCKED_EXT_RE in onionpress-creations.php);
  2. the multisite .htaccess, rewritten at every start, answers 403 for
     them (multisite.HTACCESS_BODY) — this reaches installs with the app;
  3. the WordPress image turns PHP off for the folder, ignores .htaccess
     files in it and denies the same types (onionpress-creations.conf) —
     this reaches installs with the next image publish.

The three carry one extension list; these checks keep it one list, and
keep each layer pointed at the folder the compose file actually mounts.
"""

import os
import re
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

from onionpress import multisite  # noqa: E402

WP_DIR = os.path.join(ROOT, "app", "Resources", "docker", "wordpress")
CONF = os.path.join(WP_DIR, "onionpress-creations.conf")
DOCKERFILE = os.path.join(WP_DIR, "Dockerfile")
COMPOSE = os.path.join(ROOT, "app", "Resources", "docker", "docker-compose.yml")
PLUGIN = os.path.join(ROOT, "app", "Resources", "plugins",
                      "onionpress-creations", "onionpress-creations.php")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _conf_code():
    return "\n".join(line for line in _read(CONF).splitlines()
                     if not line.lstrip().startswith("#"))


def _htaccess_creations_conds():
    """The two RewriteConds of the My Creations block, as (uri, ext) regexes."""
    m = re.search(
        r"^RewriteCond %\{REQUEST_URI\} (\S+) \[NC\]\n"
        r"RewriteCond %\{REQUEST_URI\} (\S+) \[NC\]\n"
        r"RewriteRule \^ - \[F\]$",
        multisite.HTACCESS_BODY, re.M,
    )
    assert m, "no My Creations deny block in multisite.HTACCESS_BODY"
    return m.group(1), m.group(2)


def _ext_lists():
    """The extension alternation as each layer spells it."""
    _, ext_cond = _htaccess_creations_conds()
    htaccess = re.fullmatch(r"\\\.\((.+)\)\(/\|\$\)", ext_cond)
    conf = re.search(r'<FilesMatch "\(\?i\)\\\.\((.+)\)\$">', _read(CONF))
    plugin = re.search(r"const BLOCKED_EXT_RE = '/\\\.\((.+)\)\$/i';", _read(PLUGIN))
    assert htaccess and conf and plugin, "an extension list moved; update this test"
    return {
        "multisite.HTACCESS_BODY": htaccess.group(1),
        "onionpress-creations.conf": conf.group(1),
        "onionpress-creations.php": plugin.group(1),
    }


class TestOneExtensionList(unittest.TestCase):
    def test_all_layers_carry_the_same_list(self):
        lists = _ext_lists()
        self.assertEqual(len(set(lists.values())), 1, lists)

    def test_list_covers_what_php_can_run_and_spares_media(self):
        ext = re.compile(r"\.(" + _ext_lists()["onionpress-creations.php"] + r")$", re.I)
        for name in ("x.php", "X.PHP", "x.php5", "x.php8", "x.phtml", "x.pht",
                     "x.phar", "x.phps", "x.cgi", "x.shtml"):
            self.assertTrue(ext.search(name), name)
        # sanitize_file_name() turns shell.php.jpg into shell.php_.jpg.
        for name in ("x.png", "x.html", "x.txt", "x.pdf", "x.mp4", "x.phpx",
                     "shell.php_.jpg", "x.py", "x.sh"):
            self.assertFalse(ext.search(name), name)


class TestHtaccessRule(unittest.TestCase):
    def test_rule_matches_every_site_path_into_the_folder(self):
        uri_cond, ext_cond = _htaccess_creations_conds()
        uri, ext = re.compile(uri_cond, re.I), re.compile(ext_cond, re.I)

        def denied(path):
            return bool(uri.search(path) and ext.search(path))

        # Multisite subdirectory sites reach the shared folder under their
        # own prefix; PATH_INFO after the script name still runs the script.
        for path in ("/wp-content/creations/x.php",
                     "/alice/wp-content/creations/x.php",
                     "/wp-content/creations/sub/X.PHP",
                     "/wp-content/creations/x.php/extra",
                     "/wp-content/creations/x.phtml"):
            self.assertTrue(denied(path), path)
        # The Creations page serves files via /?onionpress_creation=, and
        # PHP everywhere else must keep working.
        for path in ("/wp-content/creations/pic.png",
                     "/", "/index.php", "/alice/wp-admin/admin.php",
                     "/wp-content/plugins/foo/foo.php",
                     "/wp-content/uploads/2026/10/x.png"):
            self.assertFalse(denied(path), path)

    def test_rule_runs_before_the_existing_file_passthrough(self):
        body = multisite.HTACCESS_BODY
        deny = body.index("RewriteRule ^ - [F]")
        self.assertLess(deny, body.index("RewriteCond %{REQUEST_FILENAME} -f"))
        self.assertLess(deny, body.index("# BEGIN WordPress Multisite"))


class TestImageConf(unittest.TestCase):
    def test_conf_guards_the_folder_compose_mounts(self):
        mount = re.search(r"/Creations/My Creations:(\S+?):rw", _read(COMPOSE))
        self.assertIsNotNone(mount, "Creations bind mount moved in docker-compose.yml")
        target = mount.group(1)
        self.assertIn(f"<Directory {target}>", _read(CONF))
        self.assertIn(f"const CREATIONS_DIR = '{target}';", _read(PLUGIN))
        self.assertIn(f"/{os.path.relpath(target, '/var/www/html')}/",
                      _htaccess_creations_conds()[0])

    def test_conf_turns_php_and_overrides_off(self):
        code = _conf_code()
        self.assertIn("php_admin_flag engine off", code)
        self.assertRegex(code, r"(?m)^\s*AllowOverride None\s*$")
        self.assertRegex(code, r'(?m)^\s*<FilesMatch "\^\\\.">\s*$')

    def test_conf_keeps_a_symlink_option(self):
        # Options None also forbids the root .htaccess's RewriteRules inside
        # the folder, and Apache answers 403 for every file in it (AH00670).
        opts = re.search(r"(?m)^\s*Options\s+(.+)$", _conf_code())
        self.assertIsNotNone(opts)
        self.assertTrue(
            {"SymLinksIfOwnerMatch", "FollowSymLinks", "+SymLinksIfOwnerMatch",
             "+FollowSymLinks"} & set(opts.group(1).split()),
            opts.group(0))

    def test_conf_is_enabled_and_checked_in_the_image(self):
        dockerfile = _read(DOCKERFILE)
        self.assertIn("COPY onionpress-creations.conf /etc/apache2/conf-available/",
                      dockerfile)
        enconf = re.search(r"^RUN a2enconf (.+)$", dockerfile, re.M)
        self.assertIsNotNone(enconf)
        self.assertIn("onionpress-creations", enconf.group(1).split())
        self.assertRegex(dockerfile, r"(?m)^RUN apache2ctl configtest")


class TestUploadRefusesBlockedTypes(unittest.TestCase):
    def test_check_runs_before_the_file_is_moved(self):
        src = _read(PLUGIN)
        start = src.index("public function handle_upload()")
        end = src.index("public function ", start + 1)
        body = src[start:end]
        self.assertIn("preg_match(self::BLOCKED_EXT_RE, $name)", body)
        self.assertLess(body.index("preg_match(self::BLOCKED_EXT_RE, $name)"),
                        body.index("move_uploaded_file("))
        self.assertIn("wp_die(", body)


if __name__ == "__main__":
    unittest.main()

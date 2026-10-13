#!/usr/bin/env python3
"""The blog importer's WXR snapshots stay on the owner's computer, off the web.

A WXR export carries drafts, private and password-protected posts and the
authors' email addresses. Through 2.5.2 the importer stored each upload in
wp-content/creations/blog-archives/<host>/, inside My Creations, the folder
OnionPress publishes: the Creations page (and the home page's recent
creations) listed the files, ?onionpress_creation= served them, and Apache
served /wp-content/creations/blog-archives/<host>/*.xml.gz directly.

Now they live in the hidden .blog-archives/ next to it: still in the owner's
~/OnionPress/Creations/My Creations/, where they survive reinstalls, and
refused by every layer that publishes that folder (the Creations plugin's
path rule, the multisite .htaccess, the WordPress image's conf). The
importer moves the old folder there on the owner's next wp-admin request;
until it has, the same layers refuse the old name too.

php is preinstalled on ubuntu-latest, so the PHP half runs in CI; locally
it is skipped when php is missing.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

from onionpress import multisite  # noqa: E402

PLUGINS = os.path.join(ROOT, "app", "Resources", "plugins")
BLOG = os.path.join(PLUGINS, "onionpress-social-archive-blog.php")
CREATIONS = os.path.join(PLUGINS, "onionpress-creations", "onionpress-creations.php")
CONF = os.path.join(ROOT, "app", "Resources", "docker", "wordpress",
                    "onionpress-creations.conf")
COMPOSE = os.path.join(ROOT, "app", "Resources", "docker", "docker-compose.yml")
PHP = shutil.which("php")

SNAPSHOT = "example.org/2026-10-12T09-30-00Z.xml.gz"


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _const(src, name):
    m = re.search(r"^const " + name + r" = '([^']+)';", src, re.M)
    assert m, f"{name} not found"
    return m.group(1)


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


def _mount_target():
    """Where docker-compose.yml mounts ~/OnionPress/Creations/My Creations."""
    m = re.search(r"/Creations/My Creations:(\S+?):rw", _read(COMPOSE))
    assert m, "Creations bind mount moved in docker-compose.yml"
    return m.group(1)


def _htaccess_deny(prefix):
    """The single-condition 403 rule whose URI pattern starts with *prefix*."""
    for m in re.finditer(r"^RewriteCond %\{REQUEST_URI\} (\S+) \[NC\]\n"
                         r"RewriteRule \^ - \[F\]$", multisite.HTACCESS_BODY, re.M):
        if m.group(1).startswith(prefix):
            return re.compile(m.group(1), re.I), m.start()
    raise AssertionError(f"no 403 rule for {prefix} in HTACCESS_BODY")


def _conf_denials():
    """The regexes of the conf's <DirectoryMatch> sections that deny all."""
    found = re.findall(r'^<DirectoryMatch "([^"]+)">\n\s*Require all denied\n'
                       r"</DirectoryMatch>$", _read(CONF), re.M)
    assert found, "no denying <DirectoryMatch> in onionpress-creations.conf"
    return [re.compile(rx) for rx in found]


def _conf_denies(path):
    """Whether a denying <DirectoryMatch> covers *path* (a container path)
    or any folder it is in, whichever string Apache matches it against."""
    dirs, d = [path], path
    while d.count("/") > 1:
        d = d.rsplit("/", 1)[0]
        dirs += [d, d + "/"]
    return any(rx.search(p) for rx in _conf_denials() for p in dirs)


class TestSnapshotsLiveInAHiddenFolder(unittest.TestCase):

    def setUp(self):
        self.target = _mount_target()
        self.root = _const(_read(BLOG), "ONIONPRESS_BLOG_SNAPSHOTS_ROOT")
        self.rel = os.path.relpath(self.root, self.target)

    def test_root_is_a_hidden_folder_on_the_creations_mount(self):
        # On the mount, so the owner keeps the files on their computer...
        self.assertTrue(self.root.startswith(self.target + "/"), self.root)
        # ...in a hidden folder, which nothing that publishes My Creations
        # lists or serves.
        self.assertEqual(self.rel, ".blog-archives")

    def test_the_htaccess_refuses_it(self):
        rule, _ = _htaccess_deny("/wp-content/creations/(")
        for site in ("", "/alice"):
            for path in (f"/{self.rel}/{SNAPSHOT}", f"/{self.rel}/", f"/{self.rel}"):
                url = f"{site}/wp-content/creations{path}"
                self.assertTrue(rule.search(url), url)

    def test_the_image_conf_refuses_files_inside_hidden_folders(self):
        # <FilesMatch "^\."> sees only a file's own name, which for a
        # snapshot is 2026-...Z.xml.gz.
        for path in (f"{self.root}/{SNAPSHOT}",
                     f"{self.target}/.git/config",
                     f"{self.target}/sub/.git/logs/HEAD",
                     f"{self.target}/.thumbs/a.png.png"):
            self.assertTrue(_conf_denies(path), path)
        for path in (f"{self.target}/a.png",
                     f"{self.target}/my.file.tar.gz",
                     f"{self.target}/sub/x.y.pdf",
                     f"{self.target}/blog/2026.xml.gz",
                     "/var/www/html/.well-known/x",
                     "/var/www/html/wp-content/uploads/2026/10/a.png"):
            self.assertFalse(_conf_denies(path), path)

    def test_no_text_points_at_the_published_folder(self):
        # Paths the owner reads (plugin header, page text) name the hidden
        # folder; the old one appears only in the could-not-move warning.
        src = _read(BLOG)
        header = src[:src.index("*/")]
        self.assertIn("~/OnionPress/Creations/My Creations/.blog-archives/", header)
        self.assertIn("<code>~/OnionPress/Creations/My Creations/.blog-archives/&lt;host&gt;/</code>",
                      src)
        self.assertEqual(src.count("My Creations/blog-archives"), 1)
        self.assertIn("My Creations/blog-archives",
                      _php_function(src, "onionpress_blog_render_snapshots"))


class TestTheOldFolderStaysUnpublished(unittest.TestCase):

    LEGACY_URLS = ("/wp-content/creations/blog-archives/" + SNAPSHOT,
                   "/wp-content/creations/blog-archives/",
                   "/wp-content/creations/blog-archives",
                   "/alice/wp-content/creations/blog-archives/" + SNAPSHOT,
                   "/wp-content/creations/BLOG-Archives/" + SNAPSHOT)
    LOOKALIKES = ("/wp-content/creations/blog-archives-2020/a.png",
                  "/wp-content/creations/blog-archives.pdf",
                  "/wp-content/creations/sub/blog-archives/a.png",
                  "/wp-content/uploads/blog-archives/a.png",
                  "/blog-archives/")

    def setUp(self):
        self.target = _mount_target()
        self.blog = _read(BLOG)
        self.creations = _read(CREATIONS)
        self.legacy = _const(self.blog, "ONIONPRESS_BLOG_SNAPSHOTS_LEGACY_ROOT")

    def test_every_layer_names_the_folder_older_versions_wrote(self):
        self.assertEqual(self.legacy, self.target + "/blog-archives")
        self.assertIn("const LEGACY_SNAPSHOTS_DIR = 'blog-archives';", self.creations)

    def test_the_htaccess_refuses_it(self):
        rule, at = _htaccess_deny("/wp-content/creations/blog-archives")
        for url in self.LEGACY_URLS:
            self.assertTrue(rule.search(url), url)
        for url in self.LOOKALIKES:
            self.assertFalse(rule.search(url), url)
        self.assertLess(at, multisite.HTACCESS_BODY.index("RewriteCond %{REQUEST_FILENAME} -f"))

    def test_the_image_conf_refuses_it(self):
        for url in self.LEGACY_URLS:
            path = "/var/www/html" + url.replace("/alice", "", 1)
            self.assertTrue(_conf_denies(path.rstrip("/") or "/"), path)
        for url in self.LOOKALIKES:
            path = "/var/www/html" + url
            self.assertFalse(_conf_denies(path), path)

    def test_the_creations_path_rule_refuses_it(self):
        body = _php_function(self.creations, "is_public_path")
        self.assertIn("strcasecmp($parts[0], self::LEGACY_SNAPSHOTS_DIR) === 0", body)
        self.assertLess(body.index("LEGACY_SNAPSHOTS_DIR"), body.index("return true;"))

    def test_both_listings_go_through_the_path_rule(self):
        # The scans the listings cache (scan_folder, scan_tree).
        folder = _php_function(self.creations, "scan_folder")
        loop = folder[folder.index("foreach ($entries as $entry)"):]
        self.assertLess(loop.index("if (!self::is_public_path($rel_path))"),
                        loop.index("$full_path = "))
        files = _php_function(self.creations, "scan_tree")
        callback = files[files.index("RecursiveCallbackFilterIterator"):
                         files.index("RecursiveIteratorIterator::LEAVES_ONLY")]
        self.assertIn("self::is_public_path(substr($file_info->getPathname(), "
                      "strlen($creations_dir) + 1))", callback)

    def test_a_list_cached_by_the_old_plugin_is_not_reused(self):
        self.assertNotIn("const FILES_CACHE_KEY = 'op_creations_files';", self.creations)


class TestTheMove(unittest.TestCase):

    def setUp(self):
        self.src = _read(BLOG)

    def test_runs_on_the_owners_admin_requests(self):
        hooks = [b for b in re.findall(r"^add_action\( 'admin_init', function \(\) \{\n(.*?)^\} \);$",
                                       self.src, re.M | re.S)
                 if "onionpress_blog_snapshots_migrate(" in b]
        self.assertEqual(len(hooks), 1)
        self.assertEqual(hooks[0].split(),
                         "if ( onionpress_blog_snapshots_allowed() ) { "
                         "onionpress_blog_snapshots_migrate( false ); }".split())

    def test_snapshot_page_and_handlers_move_first(self):
        d = _php_function(self.src, "onionpress_blog_snapshots_dir_for_host")
        self.assertLess(d.index("onionpress_blog_snapshots_migrate( true );"), d.index("mkdir("))
        r = _php_function(self.src, "onionpress_blog_render_snapshots")
        self.assertLess(r.index("onionpress_blog_snapshots_allowed()"),
                        r.index("onionpress_blog_snapshots_migrate( true )"))
        self.assertLess(r.index("if ( ! onionpress_blog_snapshots_migrate( true ) )"),
                        r.index("scandir("))
        self.assertIn("notice-warning", r)

    def test_one_mover_at_a_time_and_a_failure_backs_off(self):
        m = _php_function(self.src, "onionpress_blog_snapshots_migrate")
        self.assertIn("flock( $lock, $wait ? LOCK_EX : LOCK_EX | LOCK_NB )", m)
        self.assertIn("if ( ! $wait && get_site_transient( ONIONPRESS_BLOG_SNAPSHOTS_MOVE_FAILED ) )", m)
        self.assertIn("onionpress_blog_snapshots_move_tree( $old, ONIONPRESS_BLOG_SNAPSHOTS_ROOT )", m)
        self.assertIn("set_site_transient( ONIONPRESS_BLOG_SNAPSHOTS_MOVE_FAILED", m)

    def test_never_deletes_a_file_nor_follows_a_link(self):
        t = _php_function(self.src, "onionpress_blog_snapshots_move_tree")
        self.assertNotIn("unlink(", t)
        self.assertNotIn("copy(", t)
        self.assertIn("if ( is_link( $old ) || ! is_dir( $old ) ) {\n        return $old;", t)
        self.assertIn("is_dir( $from ) && ! is_link( $from ) && is_dir( $to ) && ! is_link( $to )", t)
        # Every rename either fills an empty slot or picks a free name.
        self.assertEqual(t.count("rename("), 2)
        self.assertIn("! file_exists( $new ) && ! is_link( $new ) && @rename( $old, $new )", t)
        self.assertIn("@rename( $from, $new . '/' . $name )", t)
        self.assertIn("onionpress_blog_snapshots_free_name( $new, $e )", t)
        # The only deletion: a folder rmdir() finds empty.
        self.assertIn("if ( $left === '' && ! @rmdir( $old ) ) {", t)


@unittest.skipUnless(PHP, "php not installed")
class TestTheMoveUnderPhp(unittest.TestCase):
    """onionpress_blog_snapshots_move_tree() on a scratch directory."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="onionpress-blog-snapshots-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.old = os.path.join(self.tmp, "creations", "blog-archives")
        self.new = os.path.join(self.tmp, "creations", ".blog-archives")
        os.makedirs(os.path.join(self.tmp, "creations"))

    def _write(self, path, body):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(body)

    def _tree(self, top):
        out = {}
        for dirpath, dirs, files in os.walk(top):
            for name in files + [d for d in dirs if os.path.islink(os.path.join(dirpath, d))]:
                p = os.path.join(dirpath, name)
                rel = os.path.relpath(p, top)
                out[rel] = ("-> " + os.readlink(p)) if os.path.islink(p) else open(p).read()
        return out

    def _move(self):
        harness = os.path.join(self.tmp, "harness.php")
        with open(harness, "w") as f:
            f.write(textwrap.dedent(f"""\
                <?php
                define('ABSPATH', __DIR__);
                define('MINUTE_IN_SECONDS', 60);
                define('HOUR_IN_SECONDS', 3600);
                function add_action(...$a) {{}}
                function add_filter(...$a) {{}}
                require {json.dumps(BLOG)};
                $a = json_decode(file_get_contents('php://stdin'));
                echo json_encode(onionpress_blog_snapshots_move_tree($a[0], $a[1]));
            """))
        proc = subprocess.run(
            [PHP, "-d", "error_reporting=E_ALL", "-d", "display_errors=stderr", harness],
            input=json.dumps([self.old, self.new]), capture_output=True, text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("Warning", proc.stderr, proc.stderr)
        return json.loads(proc.stdout)

    def test_nothing_to_move(self):
        self.assertEqual(self._move(), "")
        self.assertFalse(os.path.exists(self.new))

    def test_moves_the_whole_folder(self):
        self._write(os.path.join(self.old, SNAPSHOT), "A")
        self._write(os.path.join(self.old, "other.net", "x.xml.gz"), "B")
        self.assertEqual(self._move(), "")
        self.assertFalse(os.path.lexists(self.old))
        self.assertEqual(self._tree(self.new),
                         {SNAPSHOT: "A", "other.net/x.xml.gz": "B"})
        self.assertEqual(self._move(), "")  # idempotent

    def test_merges_without_overwriting(self):
        self._write(os.path.join(self.old, SNAPSHOT), "old")
        self._write(os.path.join(self.old, "example.org", "b.xml.gz"), "old-b")
        self._write(os.path.join(self.old, "other.net", "c.xml.gz"), "old-c")
        self._write(os.path.join(self.old, ".DS_Store"), "ds")
        self._write(os.path.join(self.new, SNAPSHOT), "new")
        self.assertEqual(self._move(), "")
        self.assertFalse(os.path.lexists(self.old))
        self.assertEqual(self._tree(self.new), {
            SNAPSHOT: "new",
            SNAPSHOT.replace("Z.xml.gz", "Z-1.xml.gz"): "old",
            "example.org/b.xml.gz": "old-b",
            "other.net/c.xml.gz": "old-c",
            ".DS_Store": "ds",
        })

    def test_moves_a_link_and_never_what_it_points_at(self):
        outside = os.path.join(self.tmp, "outside")
        self._write(os.path.join(outside, "keep.txt"), "K")
        self._write(os.path.join(self.new, "x"), "X")
        os.makedirs(self.old)
        os.symlink(outside, os.path.join(self.old, "link"))
        self.assertEqual(self._move(), "")
        self.assertEqual(self._tree(outside), {"keep.txt": "K"})
        self.assertEqual(os.readlink(os.path.join(self.new, "link")), outside)

    def test_a_linked_old_folder_is_not_merged(self):
        outside = os.path.join(self.tmp, "outside")
        self._write(os.path.join(outside, "keep.txt"), "K")
        self._write(os.path.join(self.new, "x"), "X")
        os.symlink(outside, self.old)
        self.assertEqual(self._move(), self.old)
        self.assertEqual(self._tree(outside), {"keep.txt": "K"})
        self.assertEqual(self._tree(self.new), {"x": "X"})

    def test_reports_what_it_could_not_move(self):
        self._write(os.path.join(self.old, SNAPSHOT), "A")
        self._write(self.new, "a file where the folder should be")
        self.assertEqual(self._move(), self.old)
        self.assertEqual(self._tree(self.old), {SNAPSHOT: "A"})


@unittest.skipUnless(PHP, "php not installed")
class TestCreationsPathRuleUnderPhp(unittest.TestCase):

    def _ask(self, method, paths):
        tmp = tempfile.mkdtemp(prefix="onionpress-creations-test-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        harness = os.path.join(tmp, "harness.php")
        with open(harness, "w") as f:
            f.write(textwrap.dedent(f"""\
                <?php
                define('ABSPATH', __DIR__);
                function add_action(...$a) {{}}
                function add_filter(...$a) {{}}
                function register_activation_hook(...$a) {{}}
                require {json.dumps(CREATIONS)};
                $out = [];
                foreach (json_decode(file_get_contents('php://stdin')) as $p) {{
                    $out[] = OnionPress_Creations::{method}($p);
                }}
                echo json_encode($out);
            """))
        proc = subprocess.run(
            [PHP, "-d", "error_reporting=E_ALL", "-d", "display_errors=stderr", harness],
            input=json.dumps(paths), capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return dict(zip(paths, json.loads(proc.stdout)))

    def test_snapshot_folders_are_not_public(self):
        refused = [".blog-archives/" + SNAPSHOT, ".blog-archives",
                   "blog-archives/" + SNAPSHOT, "blog-archives", "Blog-Archives/x",
                   "BLOG-ARCHIVES", ".thumbs/blog-archives/a.png.png"]
        public = ["blog-archives-2020/a.png", "blog-archives.pdf",
                  "sub/blog-archives/a.png", "blog/a.png"]
        for method in ("is_public_path", "is_servable_path"):
            got = self._ask(method, refused + public)
            for p in refused:
                self.assertFalse(got[p], (method, p))
            for p in public:
                self.assertTrue(got[p], (method, p))


if __name__ == "__main__":
    unittest.main()

"""Issue #395: a WordPress core package is checked before it is installed.

onionpress-core-update.php's `download` fetched whatever `packages.full`
URL the update offer named (only the file name was checked), and the
only payload check before `status=ok` was size over 1 MiB plus a PK
magic; the audit then ran `wp core update <zip>`, and the md5 check
against api.wordpress.org came afterwards, report-only. Now the package
may come only from https://downloads.wordpress.org, the checksum list
for the package's version is fetched first (op_fetch_checksums, shared
with `verify`), and op_verify_core_zip holds every member of the zip to
it before the helper answers ok.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
import zipfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
WP_DIR = os.path.join(ROOT, "app", "Resources", "docker", "wordpress")
HELPER = os.path.join(WP_DIR, "onionpress-core-update.php")
AUDIT = os.path.join(WP_DIR, "onionpress-security-audit.sh")
DOCKERFILE = os.path.join(WP_DIR, "Dockerfile")

PHP = shutil.which("php")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _function(src, name):
    m = re.search(r"^function " + re.escape(name) + r"\(", src, re.M)
    assert m, f"no function {name}"
    end = src.index("\n}\n", m.end())
    return src[m.start():end + 3]


def _php_has_zip():
    if not PHP:
        return False
    r = subprocess.run([PHP, "-r", 'exit(class_exists("ZipArchive") ? 0 : 1);'],
                       capture_output=True)
    return r.returncode == 0


class TestWiring(unittest.TestCase):

    def setUp(self):
        self.src = _read(HELPER)
        start = self.src.index("if ( 'download' === $mode ) {")
        self.download = self.src[start:self.src.index("/* ---", start)]

    def test_the_package_host_is_checked_before_the_fetch(self):
        self.assertLess(self.download.index("op_package_url_allowed( $url )"),
                        self.download.index("wp_remote_get("))
        fn = _function(self.src, "op_package_url_allowed")
        self.assertIn("'https' !== strtolower( $p['scheme'] )", fn)
        self.assertIn("'downloads.wordpress.org' === strtolower( $p['host'] )", fn)

    def test_the_zip_is_verified_before_ok(self):
        ok = self.download.index("op_out( 'status', 'ok' )")
        self.assertLess(self.download.index("op_fetch_checksums( $version )"), ok)
        self.assertLess(self.download.index("op_verify_core_zip( $dest, $sums )"), ok)
        # A refused package is removed, not left for anyone to install.
        refused = self.download.index("op_verify_core_zip( $dest, $sums )")
        self.assertIn("@unlink( $dest )", self.download[refused:ok])

    def test_the_version_comes_from_the_name_and_must_match_the_caller(self):
        self.assertIn("wordpress-([0-9.]+)(-[a-z-]+)?\\.zip", self.download)
        self.assertIn("$wanted !== $version", self.download)

    def test_verify_mode_shares_the_fetch(self):
        start = self.src.index("if ( 'verify' === $mode ) {")
        verify = self.src[start:]
        self.assertIn("op_fetch_checksums( $version )", verify)
        self.assertEqual(self.src.count("api.wordpress.org/core/checksums"), 1)

    def test_the_audit_passes_the_target(self):
        self.assertIn('helper download "$package" "$target"', _read(AUDIT))

    def test_the_image_checks_for_ziparchive(self):
        self.assertIn('class_exists("ZipArchive")', _read(DOCKERFILE))


@unittest.skipUnless(PHP, "php not installed")
class TestPackageUrl(unittest.TestCase):

    def test_only_https_downloads_wordpress_org(self):
        php = _function(_read(HELPER), "op_package_url_allowed") + """
        $out = array();
        foreach (json_decode(file_get_contents('php://stdin'), true) as $u) {
            $out[$u] = op_package_url_allowed($u);
        }
        echo json_encode($out);
        """
        urls = {
            "https://downloads.wordpress.org/release/wordpress-7.0.3.zip": True,
            "https://DOWNLOADS.wordpress.org/release/wordpress-7.0.3.zip": True,
            "http://downloads.wordpress.org/release/wordpress-7.0.3.zip": False,
            "https://wordpress.org/wordpress-7.0.3.zip": False,
            "https://downloads.wordpress.org.evil.example/wordpress-7.0.3.zip": False,
            "https://evil.example/downloads.wordpress.org/wordpress-7.0.3.zip": False,
            "https://user@downloads.wordpress.org/release/wordpress-7.0.3.zip": False,
            "ftp://downloads.wordpress.org/wordpress-7.0.3.zip": False,
            "": False,
            "not a url": False,
        }
        r = subprocess.run([PHP, "-r", php], input=json.dumps(list(urls)),
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), urls)


@unittest.skipUnless(_php_has_zip(), "php with the zip extension not installed")
class TestZipVerification(unittest.TestCase):
    """op_verify_core_zip on packages built here: the release, a changed
    file, a missing core file, an extra file in wp-includes, and extra
    files under wp-content (left alone, as WordPress's own verify does)."""

    FILES = {
        "index.php": b"<?php // index\n",
        "wp-includes/version.php": b"<?php $wp_version = '7.0.3';\n",
        "wp-admin/admin.php": b"<?php // admin\n",
        "wp-content/plugins/hello.php": b"<?php // hello\n",
    }

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="op-core-zip-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sums = {rel: hashlib.md5(data).hexdigest() for rel, data in self.FILES.items()}

    def _zip(self, files, name="pkg.zip"):
        path = os.path.join(self.tmp, name)
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("wordpress/", "")
            for rel, data in files.items():
                z.writestr("wordpress/" + rel, data)
        return path

    def _verify(self, zip_path, sums):
        php = _function(_read(HELPER), "op_verify_core_zip") + """
        $in = json_decode(file_get_contents('php://stdin'), true);
        echo json_encode(op_verify_core_zip($in['zip'], $in['sums']));
        """
        r = subprocess.run([PHP, "-r", php], input=json.dumps({"zip": zip_path, "sums": sums}),
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_the_release_passes(self):
        self.assertEqual(self._verify(self._zip(self.FILES), self.sums), "")

    def test_a_changed_file_is_refused(self):
        files = dict(self.FILES, **{"wp-includes/version.php": b"<?php system($_GET['c']);\n"})
        self.assertIn("mismatch: wp-includes/version.php", self._verify(self._zip(files), self.sums))

    def test_a_missing_core_file_is_refused(self):
        files = {k: v for k, v in self.FILES.items() if k != "wp-admin/admin.php"}
        self.assertIn("missing: wp-admin/admin.php", self._verify(self._zip(files), self.sums))

    def test_an_extra_core_file_is_refused(self):
        files = dict(self.FILES, **{"wp-includes/evil.php": b"<?php\n"})
        self.assertIn("not in the release: wp-includes/evil.php",
                      self._verify(self._zip(files), self.sums))

    def test_a_member_outside_wordpress_is_refused(self):
        path = os.path.join(self.tmp, "stray.zip")
        with zipfile.ZipFile(path, "w") as z:
            for rel, data in self.FILES.items():
                z.writestr("wordpress/" + rel, data)
            z.writestr("../etc/passwd", b"x")
        self.assertIn("outside wordpress/", self._verify(path, self.sums))

    def test_extra_files_under_wp_content_pass_and_listed_ones_must_match(self):
        files = dict(self.FILES, **{"wp-content/themes/x/style.css": b"body{}"})
        self.assertEqual(self._verify(self._zip(files), self.sums), "")
        files = dict(self.FILES, **{"wp-content/plugins/hello.php": b"<?php // changed\n"})
        self.assertIn("mismatch: wp-content/plugins/hello.php",
                      self._verify(self._zip(files), self.sums))
        # A listed wp-content file the package lacks is not a refusal.
        files = {k: v for k, v in self.FILES.items() if k != "wp-content/plugins/hello.php"}
        self.assertEqual(self._verify(self._zip(files), self.sums), "")


if __name__ == "__main__":
    unittest.main()

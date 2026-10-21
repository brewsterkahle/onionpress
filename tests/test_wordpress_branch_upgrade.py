#!/usr/bin/env python3
"""Existing sites move to the WordPress branch the image was tested on.

A site's WordPress lives in its volume: the base image copies its files in
only on a first start, and onionpress-security-audit.sh took same-branch
releases only. So a new base moved no existing site to a new branch: in
October 2026 new installs got 7.1.3 while every older site stayed on 7.0.x.

The image now names its branch (WORDPRESS_BRANCH in the Dockerfile, checked
against the base's WordPress at build time) and the audit passes it to the
core-update helper, which offers the newest release of the site's own branch
or of the image's when that is newer. Never past it, never down.

php is preinstalled on ubuntu-24.04, so the selection test runs in CI;
locally it is skipped when php is missing.
"""

import json
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
WP_DIR = os.path.join(ROOT, "app", "Resources", "docker", "wordpress")
DOCKERFILE = os.path.join(WP_DIR, "Dockerfile")
AUDIT = os.path.join(WP_DIR, "onionpress-security-audit.sh")
HELPER = os.path.join(WP_DIR, "onionpress-core-update.php")
BRANCH_FILE = "/usr/local/share/onionpress/wordpress-branch"
PHP = shutil.which("php")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _code(text):
    return "\n".join(line for line in text.splitlines()
                     if not line.lstrip().startswith("#"))


def _function(src, name):
    """A top-level PHP function, from its signature to its closing brace."""
    m = re.search(r"^function " + re.escape(name) + r"\(", src, re.M)
    assert m, f"no function {name}"
    end = src.index("\n}\n", m.end())
    return src[m.start():end + 3]


class TestImageNamesItsBranch(unittest.TestCase):

    def test_branch_arg_checked_against_the_base(self):
        code = _code(_read(DOCKERFILE))
        self.assertRegex(code, r"(?m)^ARG WORDPRESS_BRANCH=\d+\.\d+$")
        self.assertIn("/usr/src/wordpress/wp-includes/version.php", code,
                      "The build must read the base image's own WordPress.")
        self.assertRegex(code, r'\[ "\$branch" != "\$WORDPRESS_BRANCH" \][^\n]*\n[^\n]*exit 1',
                         "A base of another branch must fail the build.")
        self.assertIn(BRANCH_FILE, code)
        self.assertGreater(code.index("ARG WORDPRESS_BRANCH"), code.index("FROM "),
                           "An ARG before FROM is not visible to RUN.")

    def test_audit_passes_the_branch_to_the_helper(self):
        code = _code(_read(AUDIT))
        self.assertIn(f"IMAGE_WP_BRANCH=$(cat {BRANCH_FILE}", code)
        self.assertIn('helper check "$IMAGE_WP_BRANCH"', code)
        self.assertNotRegex(code, r"helper check\s*\)",
                            "A bare `helper check` keeps sites on their branch.")


@unittest.skipUnless(PHP, "php not installed")
class TestUpdateSelection(unittest.TestCase):

    def pick(self, current, image_branch, offers):
        src = _read(HELPER)
        php = _function(src, "op_pick_core_update") + """
$in = json_decode( stream_get_contents( STDIN ) );
echo json_encode( op_pick_core_update( $in->current, $in->image, $in->offers ) );
"""
        r = subprocess.run([PHP, "-r", php], input=json.dumps(
            {"current": current, "image": image_branch, "offers": offers}),
            capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return tuple(json.loads(r.stdout))

    @staticmethod
    def offer(version, response="autoupdate"):
        return {"version": version, "response": response,
                "packages": {"full": f"https://downloads.wordpress.org/release/wordpress-{version}.zip"}}

    def setUp(self):
        # What api.wordpress.org offered a 7.0.7 site on 2026-10-21.
        self.OFFERS = [self.offer("7.1.3", "upgrade"), self.offer("7.1.3"),
                       self.offer("7.0.7"), self.offer("6.9.10"), self.offer("6.8.11")]

    def test_moves_up_to_the_image_branch(self):
        self.assertEqual(self.pick("7.0.7", "7.1", self.OFFERS)[0], "7.1.3")
        self.assertEqual(self.pick("7.0.2", "7.1", self.OFFERS)[0], "7.1.3")

    def test_without_the_image_branch_stays_on_its_own(self):
        offers = self.OFFERS + [self.offer("7.0.8")]
        self.assertEqual(self.pick("7.0.7", "", offers)[0], "7.0.8")
        self.assertEqual(self.pick("7.0.7", "", self.OFFERS), ("", ""))

    def test_never_past_the_image_branch(self):
        offers = self.OFFERS + [self.offer("7.2.0", "upgrade"), self.offer("8.0", "upgrade")]
        self.assertEqual(self.pick("7.0.7", "7.1", offers)[0], "7.1.3")
        self.assertEqual(self.pick("7.1.3", "7.1", offers), ("", ""))

    def test_an_older_image_never_moves_a_site_down(self):
        self.assertEqual(self.pick("7.1.3", "7.0", self.OFFERS), ("", ""))
        offers = self.OFFERS + [self.offer("7.1.4")]
        self.assertEqual(self.pick("7.1.3", "7.0", offers)[0], "7.1.4")

    def test_branch_prefix_is_whole(self):
        # 7.1 is not 7.10, nor 7.1 a prefix of 7.10.0.
        offers = [self.offer("7.10.0", "upgrade")]
        self.assertEqual(self.pick("7.0.7", "7.1", offers), ("", ""))
        self.assertEqual(self.pick("7.1.3", "", offers), ("", ""))

    def test_malformed_image_branch_is_ignored(self):
        offers = self.OFFERS + [self.offer("8.0.1", "upgrade")]
        for bad in ("8", "8.0.1", "8.0\n", "../8.0", "8.x"):
            with self.subTest(bad=bad):
                self.assertEqual(self.pick("7.0.7", bad, offers), ("", ""))

    def test_package_is_the_full_zip(self):
        target, package = self.pick("7.0.7", "7.1", self.OFFERS)
        self.assertEqual(package, "https://downloads.wordpress.org/release/wordpress-7.1.3.zip")
        no_zip = [{"version": "7.1.3", "response": "upgrade", "packages": {}}]
        self.assertEqual(self.pick("7.0.7", "7.1", no_zip), ("", ""))


if __name__ == "__main__":
    unittest.main()

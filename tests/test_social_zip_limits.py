"""Issue #473: an uploaded social-importer ZIP is held to limits before
anything is extracted.

The Twitter and Reddit importers took a ZIP from any site administrator
and ran ZipArchive::extractTo on every member with an allowed extension:
no cap on the member count, a member's size, the total or the
compression ratio, so a zip bomb filled the WordPress container's /tmp
(the VM's disk on a Mac). Both now go through
onionpress_social_zip_extract() in onionpress-social-archive.php, the
shared plugin: before a byte is written the archive may list at most
ONIONPRESS_SOCIAL_ZIP_MAX_MEMBERS members, none over
ONIONPRESS_SOCIAL_ZIP_MAX_MEMBER_BYTES, adding up to at most the larger of
ONIONPRESS_SOCIAL_ZIP_MIN_UNPACK_BUDGET and ONIONPRESS_SOCIAL_ZIP_MAX_EXPANSION
times the upload's own size (backup.check_archive_size's rule), the
wanted members must fit the disk with ONIONPRESS_SOCIAL_ZIP_FREE_RESERVE
to spare, and a symlink, a special file, an absolute or `..` name or a
duplicate refuses the archive whole. Only the members an importer reads
are extracted (by name pattern), each written by the plugin and held to
its listed size, never by extractTo.

The PHP half builds archives with zipfile and runs the plugin's
functions under php (CI's runner has one; locally it is skipped).
"""

import base64
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import unittest
import zipfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGINS = os.path.join(PROJECT_ROOT, "app/Resources/plugins")
SHARED = "onionpress-social-archive.php"
TWITTER = "onionpress-social-archive-twitter.php"
REDDIT = "onionpress-social-archive-reddit.php"

LIMITS = {
    "ONIONPRESS_SOCIAL_ZIP_MAX_MEMBERS": "100000",
    "ONIONPRESS_SOCIAL_ZIP_MAX_MEMBER_BYTES": "2147483648",
    "ONIONPRESS_SOCIAL_ZIP_MIN_UNPACK_BUDGET": "1073741824",
    "ONIONPRESS_SOCIAL_ZIP_MAX_EXPANSION": "20",
    "ONIONPRESS_SOCIAL_ZIP_FREE_RESERVE": "536870912",
}


def _read(name):
    with open(os.path.join(PLUGINS, name), encoding="utf-8") as f:
        return f.read()


def _function(text, name):
    start = text.index(f"function {name}(")
    return text[start:text.index("\n}\n", start)] + "\n}\n"


def _code(text):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(line for line in text.splitlines()
                     if not line.lstrip().startswith("//"))


class TestSource(unittest.TestCase):

    def setUp(self):
        self.shared = _read(SHARED)

    def test_the_limits_are_named_constants_with_these_values(self):
        for name, value in LIMITS.items():
            with self.subTest(constant=name):
                self.assertRegex(self.shared, re.compile(rf"^const {name}\s*=\s*{value};", re.M))
        limits = _function(self.shared, "onionpress_social_zip_limits")
        for name in LIMITS:
            self.assertIn(name + ",", limits)

    def test_nothing_in_the_plugins_calls_extractTo(self):
        for name in sorted(os.listdir(PLUGINS)):
            if name.endswith(".php"):
                with self.subTest(file=name):
                    self.assertNotIn("extractTo", _code(_read(name)))

    def test_both_importers_extract_through_the_shared_function(self):
        for name, fn, const in ((TWITTER, "onionpress_twitter_extract_safely", "ONIONPRESS_TWITTER_ZIP_MEMBERS"),
                                (REDDIT, "onionpress_reddit_extract_safely", "ONIONPRESS_REDDIT_ZIP_MEMBERS")):
            text = _read(name)
            with self.subTest(file=name):
                body = _code(_function(text, fn))
                self.assertIn(f"return onionpress_social_zip_extract( $zip_path, $target_dir, {const} );", body)
                self.assertNotIn("ZipArchive", body)
                self.assertRegex(text, re.compile(rf"^const {const} = '#\^.*\$#D';$", re.M))
                self.assertEqual(text.count("ZipArchive"), 0)

    def test_both_callers_show_the_refusal_and_remove_the_workdir(self):
        for name, fn, rm in ((TWITTER, "onionpress_twitter_handle_upload", "onionpress_rrmdir"),
                             (REDDIT, "onionpress_reddit_handle_upload", "onionpress_reddit_rrmdir")):
            text = _function(_read(name), fn)
            with self.subTest(file=name):
                self.assertIn("@chmod( $workdir, 0700 );", text)
                self.assertIn("if ( is_wp_error( $extracted ) ) {\n        " + rm + "( $workdir );", text)
                self.assertIn("esc_html( $extracted->get_error_message() )", text)

    def test_every_check_runs_before_the_first_write(self):
        extract = _function(self.shared, "onionpress_social_zip_extract")
        check = extract.index("onionpress_social_zip_check(")
        self.assertLess(check, extract.index("getStream("))
        self.assertLess(check, extract.index("fopen("))
        check_fn = _function(self.shared, "onionpress_social_zip_check")
        for call in ("getExternalAttributesIndex(", "onionpress_social_zip_name_problem(",
                     "0120000", "max_member_bytes", "min_unpack_budget", "max_expansion",
                     "disk_free_space(", "free_reserve"):
            with self.subTest(check=call):
                self.assertIn(call, check_fn)
        # A member is held to its listed size as it is written.
        self.assertIn("if ( $got > $size ) {", extract)
        self.assertIn("@unlink( $path );", extract)

    def test_the_member_patterns_name_only_what_the_importers_read(self):
        twitter = re.search(r"^const ONIONPRESS_TWITTER_ZIP_MEMBERS = '#(.*)#D';$", _read(TWITTER), re.M).group(1)
        reddit = re.search(r"^const ONIONPRESS_REDDIT_ZIP_MEMBERS = '#(.*)#D';$", _read(REDDIT), re.M).group(1)
        twitter = re.compile(twitter)
        reddit = re.compile(reddit)
        for name in ("data/tweets.js", "data/tweets-part12.js", "data/account.js",
                     "data/tweets_media/123-AbC_d.jpg", "data/tweet_media/9-x.MP4",
                     "twitter-2024-01-01/data/tweets.js"):
            self.assertTrue(twitter.match(name), name)
        for name in ("data/manifest.js", "data/direct-messages.js", "assets/app.js",
                     "Your archive.html", "data/tweets_media/evil.php", "data/tweets_media/1-a.jpg.php",
                     "a/b/data/tweets.js", "data/tweets_media/sub/1-a.jpg", "data/tweets.js/x"):
            self.assertFalse(twitter.match(name), name)
        for name in ("posts.csv", "comments.csv", "export_123/comments.csv"):
            self.assertTrue(reddit.match(name), name)
        for name in ("metadata.json", "posts.csv.php", "a/b/posts.csv", "x.csv"):
            self.assertFalse(reddit.match(name), name)


HARNESS_HEAD = r"""<?php
class WP_Error {
    private $msg;
    function __construct( $code, $msg ) { $this->msg = $msg; }
    function get_error_message() { return $this->msg; }
}
function is_wp_error( $x ) { return $x instanceof WP_Error; }
function wp_mkdir_p( $d ) { return is_dir( $d ) || mkdir( $d, 0777, true ); }
function number_format_i18n( $n ) { return number_format( $n ); }
"""

HARNESS_TAIL = r"""
$req = json_decode( file_get_contents( $argv[1] ), true );
$pattern = $req['which'] === 'twitter' ? ONIONPRESS_TWITTER_ZIP_MEMBERS : ONIONPRESS_REDDIT_ZIP_MEMBERS;
if ( $req['limits'] === null ) {
    $r = $req['which'] === 'twitter'
        ? onionpress_twitter_extract_safely( $req['zip'], $req['target'] )
        : onionpress_reddit_extract_safely( $req['zip'], $req['target'] );
} else {
    $r = onionpress_social_zip_extract( $req['zip'], $req['target'], $pattern, $req['limits'] );
}
// List what was extracted (with the small files' contents), then remove
// it: php may run as another user (a container), whose files the test
// could not remove.
$files    = array();
$contents = array();
$it = new RecursiveIteratorIterator(
    new RecursiveDirectoryIterator( $req['target'], FilesystemIterator::SKIP_DOTS ),
    RecursiveIteratorIterator::CHILD_FIRST );
foreach ( $it as $f ) {
    if ( $f->isFile() ) {
        $rel = substr( $f->getPathname(), strlen( $req['target'] ) + 1 );
        $files[ $rel ] = $f->getSize();
        if ( $f->getSize() < 1024 ) {
            $contents[ $rel ] = base64_encode( file_get_contents( $f->getPathname() ) );
        }
        unlink( $f->getPathname() );
    } else {
        rmdir( $f->getPathname() );
    }
}
ksort( $files );
echo json_encode( array(
    'ok'       => $r === true,
    'error'    => is_wp_error( $r ) ? $r->get_error_message() : null,
    'files'    => (object) $files,
    'contents' => (object) $contents,
) );
"""

SMALL = {"max_members": 5, "max_member_bytes": 100, "min_unpack_budget": 1 << 20,
         "max_expansion": 20, "free_reserve": 0}


def _member(zf, name, data=b"x", mode=None, method=zipfile.ZIP_DEFLATED):
    info = zipfile.ZipInfo(name)
    info.compress_type = method
    if mode is not None:
        info.create_system = 3
        info.external_attr = mode << 16
    zf.writestr(info, data)


@unittest.skipUnless(shutil.which("php"), "php not installed")
class TestExtractionRuns(unittest.TestCase):
    """The plugin's checks and extractor under php, on archives built
    here."""

    @classmethod
    def setUpClass(cls):
        shared = _read(SHARED)
        script = HARNESS_HEAD
        for name in LIMITS:
            script += re.search(rf"^const {name}\s*=.*;", shared, re.M).group(0) + "\n"
        for fn in ("onionpress_social_zip_limits", "onionpress_social_zip_name_problem",
                   "onionpress_social_zip_size_text", "onionpress_social_zip_check",
                   "onionpress_social_zip_extract"):
            script += _function(shared, fn)
        for name, fn, const in ((TWITTER, "onionpress_twitter_extract_safely", "ONIONPRESS_TWITTER_ZIP_MEMBERS"),
                                (REDDIT, "onionpress_reddit_extract_safely", "ONIONPRESS_REDDIT_ZIP_MEMBERS")):
            text = _read(name)
            script += re.search(rf"^const {const} = .*;$", text, re.M).group(0) + "\n"
            script += _function(text, fn)
        script += HARNESS_TAIL
        cls.dir = tempfile.mkdtemp(prefix="onionpress-zip-limits-")
        cls.script = os.path.join(cls.dir, "harness.php")
        with open(cls.script, "w", encoding="utf-8") as f:
            f.write(script)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def _zip(self, build):
        path = tempfile.mktemp(suffix=".zip", dir=self.dir)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            build(zf)
        return path

    def _run(self, zip_path, which="reddit", limits=SMALL):
        target = tempfile.mkdtemp(prefix="target-", dir=self.dir)
        req = os.path.join(self.dir, "req.json")
        with open(req, "w") as f:
            json.dump({"zip": zip_path, "target": target, "which": which, "limits": limits}, f)
        proc = subprocess.run(["php", self.script, req], text=True, capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        out = json.loads(proc.stdout)
        out["contents"] = {k: base64.b64decode(v) for k, v in out["contents"].items()}
        return out

    def _refused(self, out, words):
        self.assertFalse(out["ok"], out)
        self.assertIn(words, out["error"])
        self.assertEqual(out["files"], {}, "something was extracted")

    # ── accepted ─────────────────────────────────────────────────────

    def test_a_twitter_archive_yields_only_the_members_the_importer_reads(self):
        def build(zf):
            zf.writestr("data/tweets.js", "window.YTD.tweets.part0 = []")
            zf.writestr("data/tweets-part1.js", "window.YTD.tweets.part1 = []")
            zf.writestr("data/account.js", "window.YTD.account.part0 = []")
            zf.writestr("data/tweets_media/123-AbC.jpg", b"\xff\xd8jpg")
            zf.writestr("data/tweets_media/123-AbC.php", "<?php evil();")
            zf.writestr("data/manifest.js", "window.YTD.manifest = {}")
            zf.writestr("data/direct-messages.js", "secret")
            zf.writestr("assets/app.js", "js")
            zf.writestr("Your archive.html", "<html>")
        out = self._run(self._zip(build), which="twitter", limits=None)
        self.assertTrue(out["ok"], out)
        self.assertEqual(sorted(out["files"]), ["data/account.js", "data/tweets-part1.js",
                                                "data/tweets.js", "data/tweets_media/123-AbC.jpg"])
        self.assertEqual(out["contents"]["data/tweets_media/123-AbC.jpg"], b"\xff\xd8jpg")

    def test_a_nested_twitter_archive_is_found_one_folder_down(self):
        out = self._run(self._zip(lambda zf: zf.writestr("twitter-2024/data/tweets.js", "[]")),
                        which="twitter", limits=None)
        self.assertTrue(out["ok"], out)
        self.assertEqual(list(out["files"]), ["twitter-2024/data/tweets.js"])

    def test_a_reddit_export_yields_its_two_csvs(self):
        def build(zf):
            zf.writestr("export/posts.csv", "id,title\n1,hi\n")
            zf.writestr("export/comments.csv", "id,body\n")
            zf.writestr("export/metadata.json", "{}")
            zf.writestr("export/README.txt", "...")
        out = self._run(self._zip(build), limits=None)
        self.assertTrue(out["ok"], out)
        self.assertEqual(sorted(out["files"]), ["export/comments.csv", "export/posts.csv"])
        self.assertEqual(out["contents"]["export/posts.csv"], b"id,title\n1,hi\n")

    def test_the_real_limits_accept_an_ordinary_export(self):
        out = self._run(self._zip(lambda zf: zf.writestr("posts.csv", "id\n" * 1000)), limits=None)
        self.assertTrue(out["ok"], out)

    # ── refused before anything is written ───────────────────────────

    def test_too_many_members(self):
        def build(zf):
            for i in range(6):
                zf.writestr(f"f{i}.txt", "x")
        self._refused(self._run(self._zip(build)), "more than 5 files")

    def test_a_member_over_the_per_member_cap_even_when_not_wanted(self):
        def build(zf):
            zf.writestr("posts.csv", "id\n")
            zf.writestr("big.bin", b"\0" * 200)
        self._refused(self._run(self._zip(build)), "larger than 0.0 MB")

    def test_total_over_the_unpack_budget(self):
        limits = dict(SMALL, max_member_bytes=1 << 30)
        out = self._run(self._zip(lambda zf: zf.writestr("posts.csv", b"\0" * (2 << 20))), limits=limits)
        self._refused(out, "would unpack to 2.0 MB")

    def test_total_over_the_expansion_of_the_upload(self):
        # 64 KiB of zeros deflates to ~100 bytes: over 20x its own size
        # once the minimum budget is below it.
        limits = dict(SMALL, max_member_bytes=1 << 30, min_unpack_budget=1024)
        out = self._run(self._zip(lambda zf: zf.writestr("posts.csv", b"\0" * (64 << 10))), limits=limits)
        self._refused(out, "would unpack to 0.1 MB, more than an upload of 0.0 MB")

    def test_not_enough_free_space(self):
        limits = dict(SMALL, free_reserve=1 << 60)
        out = self._run(self._zip(lambda zf: zf.writestr("posts.csv", "id\n")), limits=limits)
        self._refused(out, "Not enough free space")

    def test_a_dotdot_name_refuses_the_archive(self):
        def build(zf):
            zf.writestr("posts.csv", "id\n")
            zf.writestr("../escape.csv", "id\n")
        self._refused(self._run(self._zip(build)), '".." path component')

    def test_an_absolute_name_refuses_the_archive(self):
        def build(zf):
            zf.writestr("posts.csv", "id\n")
            _member(zf, "/tmp/escape.csv", b"id\n")
        self._refused(self._run(self._zip(build)), "absolute path")

    def test_a_backslash_name_refuses_the_archive(self):
        self._refused(self._run(self._zip(lambda zf: _member(zf, "a\\posts.csv", b"id\n"))),
                      "backslash")

    def test_a_symlink_refuses_the_archive(self):
        def build(zf):
            zf.writestr("posts.csv", "id\n")
            _member(zf, "comments.csv", b"/etc/passwd", mode=0o120777)
        self._refused(self._run(self._zip(build)), "symbolic link")

    def test_a_special_file_refuses_the_archive(self):
        self._refused(self._run(self._zip(lambda zf: _member(zf, "posts.csv", b"", mode=0o010644))),
                      "special file")

    def test_a_wanted_name_listed_twice_refuses_the_archive(self):
        def build(zf):
            _member(zf, "posts.csv", b"id\n1\n")
            _member(zf, "posts.csv", b"id\n2\n")
        self._refused(self._run(self._zip(build)), "twice")

    def test_a_folder_entry_counts_for_nothing(self):
        def build(zf):
            zf.writestr("export/", "")
            zf.writestr("export/posts.csv", "id\n")
        out = self._run(self._zip(build))
        self.assertTrue(out["ok"], out)
        self.assertEqual(list(out["files"]), ["export/posts.csv"])

    def test_not_a_zip(self):
        path = os.path.join(self.dir, "not.zip")
        with open(path, "wb") as f:
            f.write(b"PK\x03\x04 but no")
        self._refused(self._run(path, limits=None), "not a ZIP archive")

    # ── held to the listed size while written ────────────────────────

    def test_a_member_inflating_past_its_listed_size_is_refused_and_removed(self):
        path = self._zip(lambda zf: zf.writestr("posts.csv", b"\0" * (1 << 20)))
        with open(path, "rb") as f:
            data = bytearray(f.read())
        # The central directory's uncompressed size, at offset 24 of the
        # entry, says 100 bytes; the deflate stream still inflates to 1 MiB.
        at = data.index(b"PK\x01\x02")
        struct.pack_into("<I", data, at + 24, 100)
        with open(path, "wb") as f:
            f.write(data)
        with zipfile.ZipFile(path) as zf:
            self.assertEqual(zf.getinfo("posts.csv").file_size, 100)
        out = self._run(path, limits=dict(SMALL, max_member_bytes=1 << 30))
        self.assertFalse(out["ok"], out)
        self.assertEqual(out["files"], {}, "the oversize member was left on disk")


if __name__ == "__main__":
    unittest.main()

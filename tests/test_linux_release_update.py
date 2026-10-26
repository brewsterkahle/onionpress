#!/usr/bin/env python3
"""Linux `onionpress update` installs a released package, checked; the
Settings page can no longer start an install.

`onionpress update` installed whatever a git checkout of main held (the
user's own, or root's /opt/onionpress-src, cloned from GitHub), as root,
with nothing checked beyond TLS. And the Settings page's "Update Now"
wrote requested-action `update`, which handle-action answered by running
`onionpress update` (sudo, as root): anything running in WordPress can
write that file.

Now (linux/onionpress, update_cmd): the onionpress.deb of a GitHub release
(the latest, or --version), installed with `sudo apt-get install` only once
its sha256 is the one the release's SHA256SUMS gives for it, its package
is onionpress of the tag's version, and that version is newer than the
installed one (--force for a reinstall or downgrade). A draft is refused,
and a pre-release without --pre. `--check` reports and installs nothing;
the checkout path is `--from-checkout`. handle-action `update` only writes
what the check found to update-result.json. (build/release.sh publishes
SHA256SUMS: tests/test_release_sha256sums.py.)

The launcher's functions run for real here, with curl, sudo, apt-get and
dpkg-deb stubbed (the curl stub serves a fake release from a folder).
Nothing reaches the network, sudo, apt or Docker.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TESTS_DIR)
LINUX = "linux/onionpress"
API = "https://api.github.com/repos/brewsterkahle/onionpress/releases"
DOWNLOAD = "https://github.com/brewsterkahle/onionpress/releases/download"


def _read(rel):
    with open(os.path.join(PROJECT_DIR, rel), encoding="utf-8") as f:
        return f.read()


def _shell_function(text, name):
    m = re.search(r"^" + name + r"\(\)\s*\{.*?^\}", text, re.DOTALL | re.MULTILINE)
    if m is None:
        raise AssertionError(f"{name}() not found")
    return m.group(0)


def _code(text):
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def _write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb" if isinstance(text, bytes) else "w") as f:
        f.write(text)
    if mode is not None:
        os.chmod(path, mode)


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


CURL_STUB = r'''#!/bin/bash
echo "curl $*" >> "$CALLS"
out=""; url=""
while [ $# -gt 0 ]; do
    case "$1" in
        -o) out="$2"; shift ;;
        -H|--max-time|--max-filesize|--connect-timeout|--proto|--proto-redir) shift ;;
        -*) ;;
        *) url="$1" ;;
    esac
    shift
done
case "$url" in
    https://api.github.com/repos/brewsterkahle/onionpress/releases/latest)
        f="$FAKE/api/latest.json" ;;
    https://api.github.com/repos/brewsterkahle/onionpress/releases/tags/*)
        f="$FAKE/api/tags/${url##*/}.json" ;;
    "https://api.github.com/repos/brewsterkahle/onionpress/releases?per_page=30")
        f="$FAKE/api/list.json" ;;
    https://github.com/brewsterkahle/onionpress/releases/download/*)
        f="$FAKE/download/${url#https://github.com/brewsterkahle/onionpress/releases/download/}" ;;
    *) echo "curl stub: unexpected URL $url" >&2; exit 6 ;;
esac
if [ ! -f "$f" ]; then
    echo "curl: (22) The requested URL returned error: 404" >&2
    exit 22
fi
if [ -n "$out" ]; then cat "$f" > "$out"; else cat "$f"; fi
'''

SUDO_STUB = r'''#!/bin/bash
echo "sudo $*" >> "$CALLS"
if [ "$1" = "apt-get" ]; then
    for a in "$@"; do
        case "$a" in *.deb) cp "$a" "$FAKE/installed.deb" ;; esac
    done
fi
exit "${SUDO_RC:-0}"
'''

APT_STUB = r'''#!/bin/bash
echo "apt-get $*" >> "$CALLS"
exit 1
'''

# The fake packages are text: "Package: ...\nVersion: ...\n" and a payload.
DPKG_DEB_STUB = r'''#!/bin/bash
echo "dpkg-deb $*" >> "$CALLS"
[ "$1" = "--field" ] || exit 2
sed -n "s/^$3: //p" "$2" | head -n 1
'''

PRELUDE = r'''
log() { echo "[log] $*" >> "$LOG_FILE"; }
update_finish() { echo "update_finish" >> "$CALLS"; }
update_from_checkout() { echo "update_from_checkout" >> "$CALLS"; }
wp_put() { echo "wp_put $*" >> "$CALLS"; cat > "$WP_PUT_OUT"; }
'''

FUNCTIONS = ("op_py", "is_release_version", "version_newer", "release_curl", "sha256_of",
             "update_find_release", "update_fetch_release", "update_from_release",
             "update_settings_page_check", "update_settings_page_put_result",
             "update_usage", "update_cmd")


def _launcher_script(call):
    text = _read(LINUX)
    constants = "\n".join(re.findall(r"^RELEASE_[A-Z_]+=.*$", text, re.MULTILINE))
    body = "\n".join(_shell_function(text, name) for name in FUNCTIONS)
    return ("set -e\n" + constants + "\n" + PRELUDE + body + "\n"
            'installed_version() { printf "%s\\n" "${CURRENT:-}"; }\n' + call + "\n")


class _Harness(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="op-release-update-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fake = os.path.join(self.tmp, "fake")
        self.bin = os.path.join(self.tmp, "bin")
        self.tmpdir = os.path.join(self.tmp, "tmpdir")
        os.makedirs(os.path.join(self.fake, "api", "tags"))
        os.makedirs(self.tmpdir)
        for name, text in (("curl", CURL_STUB), ("sudo", SUDO_STUB),
                           ("apt-get", APT_STUB), ("dpkg-deb", DPKG_DEB_STUB)):
            _write(os.path.join(self.bin, name), text, 0o755)
        self.calls_file = os.path.join(self.tmp, "calls")
        open(self.calls_file, "w").close()
        self.log_file = os.path.join(self.tmp, "log")
        self.wp_put_out = os.path.join(self.tmp, "wp-put-out")
        self.data_dir = os.path.join(self.tmp, "data")
        os.makedirs(self.data_dir)
        self.listed = []

    # A release: its API answer (as latest, by tag, and in the list) and
    # its downloads.
    def release(self, version="2.6.0", *, deb_version=None, package="onionpress",
                assets=("onionpress.dmg", "onionpress.deb", "SHA256SUMS"),
                upload=("onionpress.deb", "SHA256SUMS"), sums=None, served_deb=None,
                draft=False, prerelease=False, latest=True, tag=None, fields=None):
        tag = tag or f"v{version}"
        deb = (f"Package: {package}\nVersion: {deb_version or version}\n"
               f"payload of {tag}\n").encode()
        if sums is None:
            sums = (f"{_sha256(b'the dmg')}  onionpress.dmg\n"
                    f"{_sha256(deb)}  onionpress.deb\n")
        data = {"tag_name": tag, "draft": draft, "prerelease": prerelease,
                "assets": [{"name": n, "browser_download_url": f"{DOWNLOAD}/{tag}/{n}"}
                           for n in assets]}
        if fields is not None:
            data = fields(data)
        api = os.path.join(self.fake, "api")
        if latest:
            _write(os.path.join(api, "latest.json"), json.dumps(data))
        _write(os.path.join(api, "tags", f"{tag}.json"), json.dumps(data))
        self.listed.insert(0, data)
        _write(os.path.join(api, "list.json"), json.dumps(self.listed))
        if "onionpress.deb" in upload:
            _write(os.path.join(self.fake, "download", tag, "onionpress.deb"),
                   served_deb if served_deb is not None else deb)
        if "SHA256SUMS" in upload:
            _write(os.path.join(self.fake, "download", tag, "SHA256SUMS"), sums)
        return deb

    def run_launcher(self, call, *args, current="2.5.3", install_dir="/opt/onionpress", **env):
        r = subprocess.run(
            ["bash", "-c", _launcher_script(call), "bash", *args],
            capture_output=True, text=True, timeout=60,
            env={"PATH": self.bin + os.pathsep + os.environ["PATH"],
                 "HOME": self.tmp, "TMPDIR": self.tmpdir, "LOG_FILE": self.log_file,
                 "CALLS": self.calls_file, "FAKE": self.fake, "CURRENT": current,
                 "INSTALL_DIR": install_dir, "WP_PUT_OUT": self.wp_put_out,
                 "DATA_DIR": self.data_dir,
                 # op_py (the SHA256SUMS.sig check, issue #246) runs the
                 # checkout's package under the system python3.
                 "ONIONPRESS_PYTHONPATH": os.path.join(PROJECT_DIR, "src"), **env})
        return r

    def update(self, *args, **kw):
        return self.run_launcher('update_cmd "$@"', *args, **kw)

    @property
    def calls(self):
        with open(self.calls_file) as f:
            return f.read().splitlines()

    def downloads(self):
        return [c for c in self.calls if c.startswith("curl ") and DOWNLOAD in c]

    def installs(self):
        return [c for c in self.calls if c.startswith(("sudo", "apt-get"))]

    def assertNothingInstalled(self, r):
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.installs(), [])
        self.assertNotIn("update_finish", self.calls)
        self.assertIn("Nothing was installed", r.stdout)
        self.assertEqual(os.listdir(self.tmpdir), [])


class TestReleaseUpdate(_Harness):

    def test_a_good_release_installs(self):
        deb = self.release("2.6.0")
        r = self.update()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        calls = self.calls
        self.assertIn(f"{API}/latest", calls[0])
        self.assertEqual([c.split()[-1] for c in self.downloads()],
                         [f"{DOWNLOAD}/v2.6.0/SHA256SUMS", f"{DOWNLOAD}/v2.6.0/SHA256SUMS.sig",
                          f"{DOWNLOAD}/v2.6.0/onionpress.deb"])
        installs = self.installs()
        self.assertEqual(len(installs), 1, calls)
        self.assertRegex(installs[0], r"^sudo apt-get install -y --no-remove "
                         + re.escape(self.tmpdir) + r"/onionpress-update\.[^/ ]+/onionpress\.deb$")
        with open(os.path.join(self.fake, "installed.deb"), "rb") as f:
            self.assertEqual(f.read(), deb)
        self.assertEqual(calls[-1], "update_finish")
        self.assertIn("matches the sha256", r.stdout)
        # The download folder is gone, and was this user's alone.
        self.assertEqual(os.listdir(self.tmpdir), [])

    def test_every_request_is_https_only(self):
        self.release("2.6.0")
        self.assertEqual(self.update().returncode, 0)
        for c in (c for c in self.calls if c.startswith("curl ")):
            with self.subTest(c):
                self.assertIn("--fail", c)
                self.assertIn("--proto =https --proto-redir =https", c)
        body = _shell_function(_read(LINUX), "release_curl")
        self.assertIn("--proto '=https' --proto-redir '=https'", body)

    def test_a_hash_mismatch_is_refused(self):
        self.release("2.6.0", served_deb=b"Package: onionpress\nVersion: 2.6.0\nsomething else\n")
        r = self.update()
        self.assertNothingInstalled(r)
        self.assertIn("does not match its SHA256SUMS", r.stdout)

    def test_sha256sums_must_name_the_deb_once(self):
        cases = {
            "no line": f"{_sha256(b'the dmg')}  onionpress.dmg\n",
            "two lines": None,
            "another name": None,
            "short hash": "abc  onionpress.deb\n",
        }
        for label, sums in cases.items():
            with self.subTest(label):
                self.setUp()
                deb = (b"Package: onionpress\nVersion: 2.6.0\npayload of v2.6.0\n")
                if label == "two lines":
                    sums = f"{_sha256(deb)}  onionpress.deb\n{_sha256(b'x')}  onionpress.deb\n"
                elif label == "another name":
                    sums = f"{_sha256(deb)}  ./onionpress.deb\n"
                self.release("2.6.0", sums=sums)
                r = self.update()
                self.assertNothingInstalled(r)
                self.assertIn("does not give one sha256 for onionpress.deb", r.stdout)

    def test_a_release_without_sha256sums_is_refused(self):
        self.release("2.6.0", assets=("onionpress.dmg", "onionpress.deb"), upload=("onionpress.deb",))
        r = self.update()
        self.assertNothingInstalled(r)
        self.assertIn("publishes no SHA256SUMS", r.stdout)
        self.assertEqual(self.downloads(), [])

    def test_a_listed_but_missing_asset_is_refused(self):
        for missing in ("SHA256SUMS", "onionpress.deb"):
            with self.subTest(missing):
                self.setUp()
                self.release("2.6.0", upload=[n for n in ("onionpress.deb", "SHA256SUMS")
                                              if n != missing])
                r = self.update()
                self.assertNothingInstalled(r)
                self.assertIn(f"could not download {missing}", r.stdout)

    def test_a_release_without_a_deb_is_refused(self):
        self.release("2.6.0", assets=("onionpress.dmg", "SHA256SUMS"))
        r = self.update()
        self.assertNothingInstalled(r)
        self.assertIn("has no onionpress.deb", r.stdout)
        self.assertEqual(self.downloads(), [])

    def test_the_package_must_be_the_tags_onionpress(self):
        for kw, why in (({"deb_version": "2.5.9"}, "is version 2.5.9, not 2.6.0"),
                        ({"package": "something-else"}, "is not the onionpress package")):
            with self.subTest(why):
                self.setUp()
                self.release("2.6.0", **kw)
                r = self.update()
                self.assertNothingInstalled(r)
                self.assertIn(why, r.stdout)

    def test_an_older_or_the_same_release_is_not_installed(self):
        for current, version in (("2.6.0", "2.6.0"), ("2.7.0", "2.6.0"),
                                 ("2.10.0", "2.9.9"), ("3.0", "2.99.99")):
            with self.subTest(current=current, release=version):
                self.setUp()
                self.release(version)
                r = self.update(current=current)
                self.assertEqual(r.returncode, 0, r.stdout)
                self.assertIn("up to date", r.stdout)
                self.assertEqual(self.downloads(), [])
                self.assertEqual(self.installs(), [])
                self.assertNotIn("update_finish", self.calls)

    def test_a_newer_release_by_number_not_by_text(self):
        self.release("2.10.0")
        r = self.update(current="2.9.9")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(len(self.installs()), 1)

    def test_force_reinstalls_or_downgrades(self):
        self.release("2.6.0")
        r = self.update("--force", current="2.7.0")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertRegex(self.installs()[0], r"^sudo apt-get install -y --no-remove "
                         r"--reinstall --allow-downgrades /.*/onionpress\.deb$")
        self.assertEqual(self.calls[-1], "update_finish")

    def test_a_chosen_version(self):
        self.release("2.7.0")
        self.release("2.6.0", latest=False)
        r = self.update("--version", "2.6.0", "--force", current="2.7.0")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn(f"{API}/tags/v2.6.0", self.calls[0])
        self.assertIn(f"{DOWNLOAD}/v2.6.0/onionpress.deb", self.downloads()[-1])

    def test_a_version_argument_must_be_a_version(self):
        for bad in ("2.6.0;id", "../../latest", "v2.6.0-rc1", "latest", "2.6.0 ", "2"):
            with self.subTest(bad):
                self.setUp()
                self.release("2.6.0")
                r = self.update("--version", bad)
                self.assertNothingInstalled(r)
                self.assertIn("is not a release version", r.stdout)
                self.assertEqual(self.calls, [])

    def test_an_answer_for_another_tag_is_refused(self):
        self.release("2.7.0", latest=False)
        os.rename(os.path.join(self.fake, "api", "tags", "v2.7.0.json"),
                  os.path.join(self.fake, "api", "tags", "v2.6.0.json"))
        r = self.update("--version", "2.6.0")
        self.assertNothingInstalled(r)
        self.assertIn("answered with v2.7.0, not v2.6.0", r.stdout)

    def test_a_bad_tag_is_refused(self):
        self.release("2.6.0", fields=lambda d: {**d, "tag_name": "v2.6.0\nVersion: 9"})
        r = self.update()
        self.assertNothingInstalled(r)
        self.assertIn("is not a release version", r.stdout)

    def test_a_draft_is_refused(self):
        self.release("2.6.0", draft=True)
        r = self.update("--version", "2.6.0", "--pre")
        self.assertNothingInstalled(r)
        self.assertIn("is a draft", r.stdout)

    def test_a_prerelease_needs_pre(self):
        self.release("2.6.0", prerelease=True)
        r = self.update("--version", "2.6.0")
        self.assertNothingInstalled(r)
        self.assertIn("pre-release; add --pre", r.stdout)
        self.setUp()
        self.release("2.6.0", prerelease=True)
        r = self.update("--version", "2.6.0", "--pre")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(len(self.installs()), 1)

    def test_pre_without_a_version_takes_the_newest_non_draft(self):
        self.release("2.6.0")
        self.release("2.7.0", prerelease=True, latest=False)
        self.release("2.8.0", draft=True, latest=False)
        r = self.update("--pre")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn(f"{API}?per_page=30", self.calls[0])
        self.assertIn(f"{DOWNLOAD}/v2.7.0/onionpress.deb", self.downloads()[-1])

    def test_missing_flags_count_as_set(self):
        def strip(data):
            del data["draft"], data["prerelease"]
            return data
        self.release("2.6.0", fields=strip)
        r = self.update()
        self.assertNothingInstalled(r)
        self.assertIn("is a draft", r.stdout)

    def test_github_not_reached(self):
        r = self.update()
        self.assertNothingInstalled(r)
        self.assertIn("could not look up the latest release", r.stdout)
        _write(os.path.join(self.fake, "api", "latest.json"), "<html>rate limited</html>")
        r = self.update()
        self.assertNothingInstalled(r)
        self.assertIn("did not describe a release", r.stdout)

    def test_check_installs_nothing(self):
        self.release("2.6.0")
        r = self.update("--check")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("Installed: 2.5.3", r.stdout)
        self.assertIn("Release:   v2.6.0", r.stdout)
        self.assertIn("Run 'onionpress update' to install it.", r.stdout)
        self.assertEqual(self.downloads(), [])
        self.assertEqual(self.installs(), [])
        self.assertNotIn("update_finish", self.calls)

    def test_check_says_when_update_cannot_install_it(self):
        self.release("2.6.0", assets=("onionpress.dmg", "onionpress.deb"))
        r = self.update("--check")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("cannot install it: release v2.6.0 publishes no SHA256SUMS", r.stdout)
        self.assertEqual(self.installs(), [])

    def test_another_install_dir_is_refused(self):
        self.release("2.6.0")
        r = self.update(install_dir=os.path.join(self.tmp, "elsewhere"))
        self.assertNothingInstalled(r)
        self.assertIn("the release package installs /opt/onionpress", r.stdout)
        self.assertEqual(self.downloads(), [])

    def test_a_failed_apt_get_does_not_restart(self):
        self.release("2.6.0")
        r = self.update(SUDO_RC="100")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(len(self.installs()), 1)
        self.assertNotIn("update_finish", self.calls)
        self.assertIn("installing the package failed", r.stdout)
        self.assertEqual(os.listdir(self.tmpdir), [])

    def test_from_checkout_only_when_asked(self):
        r = self.update("--from-checkout")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.calls, ["update_from_checkout"])
        for extra in ("--check", "--force", "--pre"):
            with self.subTest(extra):
                self.setUp()
                r = self.update("--from-checkout", extra)
                self.assertNotEqual(r.returncode, 0)
                self.assertEqual(self.calls, [])

    def test_unknown_options_are_refused(self):
        for args in (("--yes",), ("--version",), ("now",)):
            with self.subTest(args):
                self.setUp()
                r = self.update(*args)
                self.assertNotEqual(r.returncode, 0)
                self.assertEqual(self.calls, [])

    def test_the_launcher_wires_it_up(self):
        text = _read(LINUX)
        start = text.index("\n        update)\n")
        branch = text[start:text.index("\n            ;;\n", start)]
        self.assertIn('update_cmd "${@:2}"', _code(branch))
        body = _code(_shell_function(text, "update_from_release"))
        # The package is checked before apt-get sees it, and installed from
        # the folder it was checked in.
        self.assertLess(body.index("update_fetch_release"), body.index("sudo apt-get install"))
        self.assertIn('sudo apt-get install "${apt_opts[@]}" "$UPDATE_TMP/$RELEASE_DEB"', body)
        self.assertNotIn("dpkg -i", _code(text))
        self.assertNotIn("install-tree.sh", body)
        # One repository for releases, on Linux and in the Mac updater.
        self.assertIn('RELEASE_REPO="brewsterkahle/onionpress"', text)
        self.assertIn("repos/brewsterkahle/onionpress/releases/latest",
                      _read("src/onionpress/updater.py"))


class TestSettingsPageOnlyChecks(_Harness):
    """handle-action `update` (whatever WordPress asks for) never installs."""

    def check(self, **kw):
        open(self.calls_file, "w").close()
        r = self.run_launcher("update_settings_page_check", **kw)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("wp_put /var/lib/onionpress/update-result.json", self.calls)
        self.assertEqual(self.downloads(), [])
        self.assertEqual(self.installs(), [])
        self.assertNotIn("update_finish", self.calls)
        self.assertNotIn("update_from_checkout", self.calls)
        with open(self.wp_put_out) as f:
            return json.load(f)

    def test_an_update_is_reported_with_how_to_install_it(self):
        self.release("2.6.0")
        result = self.check()
        self.assertIs(result["success"], True)
        self.assertEqual((result["current"], result["latest"]), ("2.5.3", "2.6.0"))
        self.assertIs(result["update_available"], True)
        self.assertIs(result["installable"], True)
        self.assertIn("run `onionpress update` on this machine", result["message"])

    def test_up_to_date(self):
        self.release("2.6.0")
        result = self.check(current="2.6.0")
        self.assertIs(result["update_available"], False)
        self.assertIn("up to date", result["message"])

    def test_a_release_update_cannot_install(self):
        self.release("2.6.0", assets=("onionpress.dmg", "onionpress.deb"))
        result = self.check()
        self.assertIs(result["update_available"], True)
        self.assertIs(result["installable"], False)
        self.assertIn("publishes no SHA256SUMS", result["message"])

    def test_a_failed_check(self):
        result = self.check()
        self.assertIs(result["success"], False)
        self.assertIn("could not look up the latest release", result["error"])

    def test_the_requests_id_is_echoed(self):
        # Issue #356: handle-action exports the page's request id; the
        # result carries it, fresh or from the cache.
        self.release("2.6.0")
        result = self.check(ONIONPRESS_REQUEST_ID="Req_1-a")
        self.assertEqual(result["request_id"], "Req_1-a")
        self.assertIs(result["update_available"], True)
        cached = self.check(ONIONPRESS_REQUEST_ID="Req_2")
        self.assertEqual(cached["request_id"], "Req_2")
        self.assertFalse([c for c in self.calls if c.startswith("curl ")])
        with open(os.path.join(self.data_dir, "update-check.json")) as f:
            self.assertNotIn("request_id", json.load(f))

    def test_without_an_id_or_with_a_bad_one_none_is_echoed(self):
        self.release("2.6.0")
        self.assertNotIn("request_id", self.check())
        for bad in ("a b", "x" * 65, "../x", "a;b"):
            with self.subTest(bad=bad):
                self.assertNotIn("request_id", self.check(ONIONPRESS_REQUEST_ID=bad))

    def test_one_github_lookup_per_five_minutes(self):
        self.release("2.6.0")
        first = self.check()
        self.assertEqual(len([c for c in self.calls if c.startswith("curl ")]), 1)
        # Asked again at once: the same answer, without asking GitHub.
        self.assertEqual(self.check(), first)
        self.assertFalse([c for c in self.calls if c.startswith("curl ")])
        # Six minutes later, GitHub again.
        cache = os.path.join(self.data_dir, "update-check.json")
        old = os.stat(cache).st_mtime - 360
        os.utime(cache, (old, old))
        self.check()
        self.assertEqual(len([c for c in self.calls if c.startswith("curl ")]), 1)

    def test_handle_action_update_only_checks(self):
        text = _read(LINUX)
        # The shared handler (onionpress.handle_action) runs the launcher's
        # settings-page-update-check on Linux, which only reports.
        handler = _read("src/onionpress/handle_action.py")
        update = handler[handler.index("def _update(host):"):handler.index("# -- recent logs")]
        self.assertIn('if host.os_name != "macos":', update)
        self.assertIn('host.run_launcher(["settings-page-update-check"]', update)
        self.assertLess(update.index('["settings-page-update-check"]'), update.index("return"))
        start = text.index("\n        settings-page-update-check)\n")
        branch = _code(text[start:text.index("\n            ;;\n", start)])
        self.assertIn("update_settings_page_check", branch)
        for word in ('"$0"', "sudo", "apt-get", "install-tree", "update_cmd",
                     "update_from", "update_finish", "update_fetch_release"):
            with self.subTest(word):
                self.assertNotIn(word, branch)
        body = _code(_shell_function(text, "update_settings_page_check"))
        for word in ("sudo", "apt-get", "update_fetch_release", "update_from",
                     "update_finish", "update_cmd", '"$0"'):
            with self.subTest(word):
                self.assertNotIn(word, body)

    def test_the_page_says_so_on_linux(self):
        php = _read("app/Resources/plugins/onionpress-settings.php")
        self.assertIn("$update_checks_only = is_array( $status_now ) && isset( $status_now['platform'] )",
                      php)
        self.assertIn("'update' => $update_checks_only ? 'Checking for a new release of' : 'Updating'",
                      php)
        self.assertIn("var updateChecksOnly = <?php echo ( 'linux' === $current_platform )", php)
        linux = php[php.index("if (data.update && data.latest && updateChecksOnly) {"):]
        linux = linux[:linux.index("} else if (data.update && data.latest) {")]
        self.assertIn("<code>onionpress update</code> on this machine", linux)
        self.assertNotIn("Update Now", linux)


if __name__ == "__main__":
    unittest.main()

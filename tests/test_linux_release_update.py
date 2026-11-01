#!/usr/bin/env python3
"""Linux `onionpress update` installs a released package, checked; the
Settings page can no longer start an install; one updater on both
platforms.

`onionpress update` installed whatever a git checkout of main held (the
user's own, or root's /opt/onionpress-src, cloned from GitHub), as root,
with nothing checked beyond TLS. And the Settings page's "Update Now"
wrote requested-action `update`, which handle-action answered by running
`onionpress update` (sudo, as root): anything running in WordPress can
write that file.

Since 2.5.4: the onionpress.deb of a GitHub release (the latest, or
--version), installed with `sudo apt-get install` only once its sha256 is
the one the release's SHA256SUMS gives for it, its package is onionpress
of the tag's version, and that version is newer than the installed one
(--force for a reinstall or downgrade). A draft is refused, and a
pre-release without --pre. `--check` reports and installs nothing; the
checkout path is `--from-checkout`. handle-action `update` only writes
what the check found to update-result.json. (build/release.sh publishes
SHA256SUMS: tests/test_release_sha256sums.py.)

Since 2.6.2 the client is onionpress.cli's on both platforms
(cli.cmd_update -> updater.find_release / check_release / fetch_deb; the
Mac's version of the same rules used to lack --version, --pre and the
draft check, and linux/onionpress carried a second client in bash). The
Linux launcher keeps the install leaves the Python calls:
`update-install-deb <path> [--force]` (sudo apt-get, then update_finish)
and `update-from-checkout`.

cmd_update runs for real here, with curl, dpkg-deb, sudo and apt-get
stubbed on PATH (the curl stub serves a fake release from a folder) and a
stub launcher whose update-install-deb is the real bash function with
update_finish recorded. Nothing reaches the network, sudo, apt or Docker.
"""
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(PROJECT_DIR, "src"))

from onionpress import cli, confirm, handle_action, updater  # noqa: E402
from onionpress.platform import resolve_paths  # noqa: E402

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
        -H|--max-time|--max-filesize|--connect-timeout|--proto|--proto-redir|--cacert) shift ;;
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

# The stub launcher: update-install-deb is linux/onionpress's own
# function (apt-get for real, against the sudo stub), update_finish and
# log recorded; update-from-checkout is recorded only.
LAUNCHER_PRELUDE = r'''
log() { echo "[log] $*" >> "$LOG_FILE"; }
update_finish() { echo "update_finish" >> "$CALLS"; }
'''
LAUNCHER_MAIN = r'''
echo "launcher $*" >> "$CALLS"
case "$1" in
    update-install-deb) update_install_deb "${@:2}" ;;
    update-from-checkout) exit "${CHECKOUT_RC:-0}" ;;
    *) echo "launcher stub: unexpected verb $1" >&2; exit 9 ;;
esac
'''


def _launcher_script():
    text = _read(LINUX)
    return ("#!/bin/bash\nset -e\n" + LAUNCHER_PRELUDE
            + _shell_function(text, "update_install_deb") + "\n" + LAUNCHER_MAIN)


class _Harness(unittest.TestCase):
    """A fake GitHub, the stubs on PATH, a data dir and an install dir of
    the test's own; cmd_update run in-process as on Linux."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="op-update-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = os.path.join(self.tmp, "bin")
        self.fake = os.path.join(self.tmp, "fake")
        self.tmpdir = os.path.join(self.tmp, "tmpdir")
        os.makedirs(os.path.join(self.fake, "api", "tags"))
        os.makedirs(self.tmpdir)
        for name, text in (("curl", CURL_STUB), ("sudo", SUDO_STUB),
                           ("apt-get", APT_STUB), ("dpkg-deb", DPKG_DEB_STUB)):
            _write(os.path.join(self.bin, name), text, 0o755)
        self.launcher = os.path.join(self.tmp, "launcher")
        _write(self.launcher, _launcher_script(), 0o755)
        self.calls_file = os.path.join(self.tmp, "calls")
        open(self.calls_file, "w").close()
        self.log_file = os.path.join(self.tmp, "log")
        self.data_dir = os.path.join(self.tmp, "data")
        os.makedirs(self.data_dir)
        self.install_dir = os.path.join(self.tmp, "opt", "onionpress")
        os.makedirs(self.install_dir)
        self.listed = []
        self.logged = []
        env = mock.patch.dict(os.environ, {
            "PATH": self.bin + os.pathsep + os.environ["PATH"], "HOME": self.tmp,
            "TMPDIR": self.tmpdir, "LOG_FILE": self.log_file, "CALLS": self.calls_file,
            "FAKE": self.fake, "ONIONPRESS_INSTALL_DIR": self.install_dir})
        env.start()
        self.addCleanup(env.stop)
        for key in ("ONIONPRESS_YES", "ONIONPRESS_APP_PATH", "ONIONPRESS_LAUNCHER", "SUDO_RC"):
            os.environ.pop(key, None)
        # The package installs /opt/onionpress; here, the test's own tree.
        p = mock.patch.object(updater, "RELEASE_INSTALL_DIR", self.install_dir)
        p.start()
        self.addCleanup(p.stop)
        # tempfile caches its choice: the download must land in ours.
        self._old_tempdir = tempfile.tempdir
        tempfile.tempdir = self.tmpdir
        self.addCleanup(setattr, tempfile, "tempdir", self._old_tempdir)
        self.paths = resolve_paths(data_dir=self.data_dir, app_bundle="")

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
                "published_at": "2026-10-01", "body": f"Notes of {tag}",
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

    def update(self, *, current="2.5.3", yes=True, tty=False, **kw):
        """cmd_update as on Linux; (rc, stdout, stderr)."""
        with open(os.path.join(self.install_dir, "VERSION"), "w") as f:
            f.write(current + "\n")
        out, err = io.StringIO(), io.StringIO()
        stdin = mock.Mock(isatty=lambda: True) if tty else io.StringIO("")
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(sys, "stdin", stdin):
            rc = cli.cmd_update(self.paths, self.logged.append, yes=yes, launcher=self.launcher,
                                os_name="linux", **kw)
        return rc, out.getvalue(), err.getvalue()

    @property
    def calls(self):
        with open(self.calls_file) as f:
            return f.read().splitlines()

    def downloads(self):
        return [c for c in self.calls if c.startswith("curl ") and DOWNLOAD in c]

    def installs(self):
        return [c for c in self.calls if c.startswith(("sudo", "apt-get"))]

    def assertNothingInstalled(self, result):
        rc, out, err = result
        self.assertNotEqual(rc, 0, out)
        self.assertEqual(self.installs(), [])
        self.assertNotIn("update_finish", self.calls)
        self.assertIn("Nothing was installed", err)
        self.assertEqual(os.listdir(self.tmpdir), [])


class TestReleaseUpdate(_Harness):

    def test_a_good_release_installs(self):
        deb = self.release("2.6.0")
        rc, out, err = self.update()
        self.assertEqual(rc, 0, out + err)
        calls = self.calls
        self.assertIn(f"{API}/latest", calls[0])
        self.assertEqual([c.split()[-1] for c in self.downloads()],
                         [f"{DOWNLOAD}/v2.6.0/SHA256SUMS", f"{DOWNLOAD}/v2.6.0/onionpress.deb"])
        installs = self.installs()
        self.assertEqual(len(installs), 1, calls)
        self.assertRegex(installs[0], r"^sudo apt-get install -y --no-remove "
                         + re.escape(self.tmpdir) + r"/onionpress-update\.[^/ ]+/onionpress\.deb$")
        with open(os.path.join(self.fake, "installed.deb"), "rb") as f:
            self.assertEqual(f.read(), deb)
        self.assertEqual(calls[-1], "update_finish")
        # The package is checked before the launcher's leaf sees it.
        self.assertLess(calls.index([c for c in calls if c.startswith("dpkg-deb")][0]),
                        calls.index([c for c in calls if c.startswith("launcher ")][0]))
        self.assertIn("matches the sha256", out)
        self.assertIn("Installed OnionPress 2.6.0.", out)
        self.assertTrue(any("matches its SHA256SUMS (sha256 " in m for m in self.logged))
        # The download folder is gone, and was this user's alone.
        self.assertEqual(os.listdir(self.tmpdir), [])

    def test_the_json_answer(self):
        self.release("2.6.0")
        rc, out, _ = self.update(json_output=True)
        self.assertEqual(rc, 0, out)
        self.assertEqual(confirm.last_json_line(out),
                         {"ok": True, "version": "2.6.0", "current": "2.5.3"})

    def test_every_request_is_https_only(self):
        self.release("2.6.0")
        self.assertEqual(self.update()[0], 0)
        curls = [c for c in self.calls if c.startswith("curl ")]
        self.assertEqual(len(curls), 3)
        for c in curls:
            with self.subTest(c):
                self.assertIn("--fail", c)
                self.assertIn("--proto =https --proto-redir =https --tlsv1.2", c)
                self.assertIn("User-Agent: onionpress", c)
        self.assertIn("--max-filesize 65536", curls[1])
        self.assertIn("--max-filesize 268435456", curls[2])

    def test_a_hash_mismatch_is_refused(self):
        self.release("2.6.0", served_deb=b"Package: onionpress\nVersion: 2.6.0\nsomething else\n")
        r = self.update()
        self.assertNothingInstalled(r)
        self.assertIn("does not match its SHA256SUMS", r[2])

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
                self.assertIn("does not give one sha256 for onionpress.deb", r[2])

    def test_a_release_without_sha256sums_is_refused(self):
        self.release("2.6.0", assets=("onionpress.dmg", "onionpress.deb"), upload=("onionpress.deb",))
        r = self.update()
        self.assertNothingInstalled(r)
        self.assertIn("publishes no SHA256SUMS", r[2])
        self.assertEqual(self.downloads(), [])

    def test_a_listed_but_missing_asset_is_refused(self):
        for missing in ("SHA256SUMS", "onionpress.deb"):
            with self.subTest(missing):
                self.setUp()
                self.release("2.6.0", upload=[n for n in ("onionpress.deb", "SHA256SUMS")
                                              if n != missing])
                r = self.update()
                self.assertNothingInstalled(r)
                self.assertIn(f"could not download {missing}", r[2])

    def test_a_release_without_a_deb_is_refused(self):
        self.release("2.6.0", assets=("onionpress.dmg", "SHA256SUMS"))
        r = self.update()
        self.assertNothingInstalled(r)
        self.assertIn("has no onionpress.deb", r[2])
        self.assertEqual(self.downloads(), [])

    def test_the_package_must_be_the_tags_onionpress(self):
        for kw, why in (({"deb_version": "2.5.9"}, "is version 2.5.9, not 2.6.0"),
                        ({"package": "something-else"}, "is not the onionpress package")):
            with self.subTest(why):
                self.setUp()
                self.release("2.6.0", **kw)
                r = self.update()
                self.assertNothingInstalled(r)
                self.assertIn(why, r[2])

    def test_an_older_or_the_same_release_is_not_installed(self):
        for current, version in (("2.6.0", "2.6.0"), ("2.7.0", "2.6.0"),
                                 ("2.10.0", "2.9.9"), ("3.0", "2.99.99")):
            with self.subTest(current=current, release=version):
                self.setUp()
                self.release(version)
                rc, out, _ = self.update(current=current)
                self.assertEqual(rc, 0, out)
                self.assertIn("up to date", out)
                self.assertIn(f"onionpress update --version {version} --force", out)
                self.assertEqual(self.downloads(), [])
                self.assertEqual(self.installs(), [])
                self.assertNotIn("update_finish", self.calls)

    def test_a_newer_release_by_number_not_by_text(self):
        self.release("2.10.0")
        rc, out, _ = self.update(current="2.9.9")
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.installs()), 1)

    def test_an_unreadable_installed_version_takes_any_release(self):
        self.release("2.6.0")
        os.rename(self.install_dir, self.install_dir + "-gone")
        os.makedirs(self.install_dir)
        with mock.patch.object(updater, "__version__", "not a version"):
            rc, out, _ = self.update(current="unknown")
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.installs()), 1)

    def test_force_reinstalls_or_downgrades(self):
        self.release("2.6.0")
        rc, out, _ = self.update(force=True, current="2.7.0")
        self.assertEqual(rc, 0, out)
        self.assertRegex(self.installs()[0], r"^sudo apt-get install -y --no-remove "
                         r"--reinstall --allow-downgrades /.*/onionpress\.deb$")
        self.assertEqual(self.calls[-1], "update_finish")

    def test_a_chosen_version(self):
        self.release("2.7.0")
        self.release("2.6.0", latest=False)
        rc, out, _ = self.update(version="2.6.0", force=True, current="2.7.0")
        self.assertEqual(rc, 0, out)
        self.assertIn(f"{API}/tags/v2.6.0", self.calls[0])
        self.assertIn(f"{DOWNLOAD}/v2.6.0/onionpress.deb", self.downloads()[-1])

    def test_a_version_argument_must_be_a_version(self):
        for bad in ("2.6.0;id", "../../latest", "v2.6.0-rc1", "latest", "2.6.0 ", "2", "2.6.0\n"):
            with self.subTest(bad):
                self.setUp()
                self.release("2.6.0")
                r = self.update(version=bad)
                self.assertNothingInstalled(r)
                self.assertIn("is not a release version", r[2])
                self.assertEqual(self.calls, [])

    def test_an_answer_for_another_tag_is_refused(self):
        self.release("2.7.0", latest=False)
        os.rename(os.path.join(self.fake, "api", "tags", "v2.7.0.json"),
                  os.path.join(self.fake, "api", "tags", "v2.6.0.json"))
        r = self.update(version="2.6.0")
        self.assertNothingInstalled(r)
        self.assertIn("answered with v2.7.0, not v2.6.0", r[2])

    def test_a_bad_tag_is_refused(self):
        self.release("2.6.0", fields=lambda d: {**d, "tag_name": "v2.6.0\nVersion: 9"})
        r = self.update()
        self.assertNothingInstalled(r)
        self.assertIn("is not a release version", r[2])
        self.assertNotIn("\n", r[2].split("tag")[1].split("is not")[0])

    def test_a_draft_is_refused(self):
        self.release("2.6.0", draft=True)
        r = self.update(version="2.6.0", pre=True)
        self.assertNothingInstalled(r)
        self.assertIn("is a draft", r[2])

    def test_a_prerelease_needs_pre(self):
        self.release("2.6.0", prerelease=True)
        r = self.update(version="2.6.0")
        self.assertNothingInstalled(r)
        self.assertIn("pre-release; add --pre", r[2])
        self.setUp()
        self.release("2.6.0", prerelease=True)
        rc, out, _ = self.update(version="2.6.0", pre=True)
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.installs()), 1)

    def test_pre_without_a_version_takes_the_newest_non_draft(self):
        self.release("2.6.0")
        self.release("2.7.0", prerelease=True, latest=False)
        self.release("2.8.0", draft=True, latest=False)
        rc, out, _ = self.update(pre=True)
        self.assertEqual(rc, 0, out)
        self.assertIn(f"{API}?per_page=30", self.calls[0])
        self.assertIn(f"{DOWNLOAD}/v2.7.0/onionpress.deb", self.downloads()[-1])

    def test_missing_flags_count_as_set(self):
        def strip(data):
            del data["draft"], data["prerelease"]
            return data
        self.release("2.6.0", fields=strip)
        r = self.update()
        self.assertNothingInstalled(r)
        self.assertIn("is a draft", r[2])

    def test_github_not_reached(self):
        r = self.update()
        self.assertNothingInstalled(r)
        self.assertIn("could not look up the latest release", r[2])
        self.assertIn("curl exit 22", r[2])
        _write(os.path.join(self.fake, "api", "latest.json"), "<html>rate limited</html>")
        r = self.update()
        self.assertNothingInstalled(r)
        self.assertIn("did not describe a release", r[2])
        r = self.update(version="2.6.0")
        self.assertNothingInstalled(r)
        self.assertIn("could not find release v2.6.0 on GitHub", r[2])

    def test_check_installs_nothing(self):
        self.release("2.6.0")
        rc, out, _ = self.update(check=True)
        self.assertEqual(rc, 0, out)
        self.assertIn("Installed: 2.5.3", out)
        self.assertIn("Update available: 2.6.0", out)
        self.assertIn("Run 'onionpress update' to install it.", out)
        self.assertEqual(self.downloads(), [])
        self.assertEqual(self.installs(), [])
        self.assertNotIn("update_finish", self.calls)
        rc, out, _ = self.update(check=True, json_output=True)
        doc = json.loads(out)
        self.assertEqual((doc["current"], doc["latest"], doc["update_available"],
                          doc["installable"], doc["install_problem"], doc["error"]),
                         ("2.5.3", "2.6.0", True, True, None, None))
        self.assertEqual(doc["release_url"],
                         "https://github.com/brewsterkahle/onionpress/releases/tag/v2.6.0")

    def test_check_says_when_update_cannot_install_it(self):
        self.release("2.6.0", assets=("onionpress.dmg", "onionpress.deb"))
        rc, out, _ = self.update(check=True)
        self.assertEqual(rc, 0, out)
        self.assertIn("cannot install it: release v2.6.0 publishes no SHA256SUMS", out)
        self.assertEqual(self.installs(), [])
        rc, out, _ = self.update(check=True, json_output=True)
        doc = json.loads(out)
        self.assertEqual((doc["update_available"], doc["installable"]), (True, False))
        self.assertIn("publishes no SHA256SUMS", doc["install_problem"])

    def test_check_up_to_date_and_failed(self):
        self.release("2.5.3")
        rc, out, _ = self.update(check=True, json_output=True)
        doc = json.loads(out)
        self.assertEqual((doc["update_available"], doc["installable"], doc["latest"]),
                         (False, False, "2.5.3"))
        self.assertIsNone(doc["error"])
        os.remove(os.path.join(self.fake, "api", "latest.json"))
        rc, out, _ = self.update(check=True, json_output=True)
        doc = json.loads(out)
        self.assertEqual(rc, 0)
        self.assertIn("could not look up the latest release", doc["error"])
        self.assertEqual((doc["update_available"], doc["latest"]), (False, None))

    def test_another_install_dir_is_refused(self):
        self.release("2.6.0")
        with mock.patch.object(updater, "RELEASE_INSTALL_DIR", "/opt/onionpress"):
            r = self.update()
        self.assertNothingInstalled(r)
        self.assertIn("the release package installs /opt/onionpress", r[2])
        self.assertEqual(self.downloads(), [])

    def test_a_missing_tool_is_refused_before_any_download(self):
        self.release("2.6.0")
        with mock.patch("shutil.which",
                        side_effect=lambda t: None if t == "dpkg-deb" else f"/usr/bin/{t}"):
            r = self.update()
        self.assertNothingInstalled(r)
        self.assertIn("dpkg-deb is not installed", r[2])
        self.assertEqual(self.downloads(), [])

    def test_a_failed_apt_get_does_not_restart(self):
        self.release("2.6.0")
        os.environ["SUDO_RC"] = "100"
        rc, out, err = self.update()
        self.assertNotEqual(rc, 0)
        self.assertEqual(len(self.installs()), 1)
        self.assertNotIn("update_finish", self.calls)
        self.assertIn("installing the package failed", err)
        self.assertEqual(os.listdir(self.tmpdir), [])

    def test_no_temporary_folder_no_download(self):
        self.release("2.6.0")
        with mock.patch.object(tempfile, "mkdtemp", side_effect=OSError("full")):
            r = self.update()
        self.assertNothingInstalled(r)
        self.assertIn("could not make a temporary folder", r[2])

    def test_without_yes_a_terminal_is_asked_and_a_pipe_refused(self):
        self.release("2.6.0")
        rc, out, err = self.update(yes=False)
        self.assertEqual(rc, 2)
        self.assertIn("re-run with --yes", err)
        self.assertEqual(self.downloads(), [])
        with mock.patch("builtins.input", return_value="n"):
            rc, out, _ = self.update(yes=False, tty=True)
        self.assertEqual((rc, "Aborted." in out), (0, True))
        self.assertEqual(self.downloads(), [])
        with mock.patch("builtins.input", return_value="y"):
            rc, out, _ = self.update(yes=False, tty=True)
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.installs()), 1)

    def test_from_checkout_only_when_asked(self):
        rc, out, _ = self.update(from_checkout=True, yes=False)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.calls, ["launcher update-from-checkout"])
        for extra in ({"check": True}, {"force": True}, {"pre": True}, {"version": "2.6.0"},
                      {"yes": True}):
            with self.subTest(extra):
                self.setUp()
                rc, _, err = self.update(from_checkout=True, **{"yes": False, **extra})
                self.assertEqual(rc, 2)
                self.assertIn(cli.FROM_CHECKOUT_ALONE_MSG, err)
                self.assertEqual(self.calls, [])
        self.setUp()
        os.environ["CHECKOUT_RC"] = "3"
        rc, _, err = self.update(from_checkout=True, yes=False)
        self.assertEqual(rc, 3)
        self.assertIn("installing from the checkout failed (exit 3)", err)
        self.setUp()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.cmd_update(self.paths, self.logged.append, from_checkout=True, os_name="macos")
        self.assertEqual(rc, 2)
        self.assertIn(cli.FROM_CHECKOUT_LINUX_ONLY_MSG, err.getvalue())

    def test_the_launcher_wires_it_up(self):
        text = _read(LINUX)
        start = text.index("\n        update)\n")
        branch = _code(text[start:text.index("\n            ;;\n", start)])
        self.assertIn('op_py --data-dir "$DATA_DIR" update --launcher "$INSTALL_DIR/onionpress" '
                      '"${update_args[@]}"', branch)
        self.assertIn('[ "$ONIONPRESS_YES" != "true" ] || update_args+=(--yes)', branch)
        for case, fn in (("update-install-deb", 'update_install_deb "${@:2}"'),
                         ("update-from-checkout", "update_from_checkout")):
            start = text.index(f"\n        {case})\n")
            self.assertIn(fn, _code(text[start:text.index("\n            ;;\n", start)]))
        body = _code(_shell_function(text, "update_install_deb"))
        self.assertIn('sudo apt-get install "${apt_opts[@]}" "$deb"', body)
        self.assertLess(body.index("sudo apt-get install"), body.index("update_finish"))
        self.assertNotIn("dpkg -i", _code(text))
        self.assertNotIn("install-tree.sh", body)
        self.assertNotIn("curl", body)
        # The bash client is gone: one lookup, one comparator, one
        # SHA256SUMS parser, in updater.py.
        for gone in ("update_find_release", "update_fetch_release", "update_from_release",
                     "update_settings_page_check", "update_cmd()", "release_curl",
                     "is_release_version", "version_newer", "settings-page-update-check"):
            with self.subTest(gone):
                self.assertNotIn(gone, _code(text))
        self.assertEqual(updater.RELEASE_REPO, "brewsterkahle/onionpress")
        self.assertEqual(updater.RELEASE_API, API)
        self.assertEqual(updater.RELEASE_DOWNLOAD, DOWNLOAD)
        # Every docker compose pull in the launcher is missing-only.
        for line in text.splitlines():
            if "docker compose" in line and " pull" in line and not line.lstrip().startswith("#"):
                self.assertIn("--policy missing", line)


class TestUpdaterRules(unittest.TestCase):
    """updater.py's pieces on their own."""

    def test_release_versions(self):
        for ok in ("2.5.3", "2.6", "10.0.0.1", "0.1"):
            self.assertTrue(updater.is_release_version(ok), ok)
        for bad in ("v2.5.3", "2", "2.5.3-rc1", "2.5.3\n", " 2.5.3", "2..3", "a.b", "",
                    None, "２.5.3", "2.5.3.4.5"):
            self.assertFalse(updater.is_release_version(bad), repr(bad))

    def test_version_newer(self):
        self.assertTrue(updater.version_newer("2.10.0", "2.9.9"))
        self.assertTrue(updater.version_newer("3.0", "2.99.99"))
        self.assertTrue(updater.version_newer("2.6.1", "2.6"))
        self.assertFalse(updater.version_newer("2.6", "2.6.0"))
        self.assertFalse(updater.version_newer("2.6.0", "2.6.0"))
        self.assertFalse(updater.version_newer("2.6.0", "2.7.0"))

    def test_install_problem_per_platform(self):
        r = updater.Release("v1.0.0", "1.0.0", {}, ["OnionPress-1.0.0.dmg", "onionpress.deb",
                                                   "SHA256SUMS"])
        self.assertIsNone(r.install_problem("linux"))
        self.assertIsNone(r.install_problem("macos"))
        self.assertEqual(r.dmg_name(), "OnionPress-1.0.0.dmg")
        self.assertEqual(r.download_url("SHA256SUMS"),
                         f"{DOWNLOAD}/v1.0.0/SHA256SUMS")
        r = updater.Release("v1.0.0", "1.0.0", {}, ["SHA256SUMS"])
        self.assertIn("has no onionpress.deb", r.install_problem("linux"))
        self.assertIn("has no .dmg", r.install_problem("macos"))
        r = updater.Release("v1.0.0", "1.0.0", {}, ["a.dmg", "onionpress.deb"])
        self.assertIn("so its package cannot be checked", r.install_problem("linux"))
        self.assertIn("so its disk image cannot be checked", r.install_problem("macos"))
        r = updater.Release("v1.0.0", "1.0.0", {}, ["a.dmg", "b.dmg", "SHA256SUMS"])
        self.assertIn("has no .dmg", r.install_problem("macos"))

    def test_installed_version_sources(self):
        tmp = tempfile.mkdtemp(prefix="op-ver-")
        self.addCleanup(shutil.rmtree, tmp, True)
        # Linux: the install tree's VERSION, else the package's own.
        with open(os.path.join(tmp, "VERSION"), "w") as f:
            f.write("2.4.0\n2.4.1\n")
        self.assertEqual(updater.installed_version("linux", install_dir=tmp), "2.4.0")
        with mock.patch.dict(os.environ, {"ONIONPRESS_INSTALL_DIR": tmp}):
            self.assertEqual(updater.installed_version("linux"), "2.4.0")
        self.assertEqual(updater.installed_version("linux", install_dir=os.path.join(tmp, "no")),
                         updater.__version__)
        # A Mac: the bundle's Info.plist, else the package's own.
        import plistlib
        app = os.path.join(tmp, "OnionPress.app")
        os.makedirs(os.path.join(app, "Contents"))
        with open(os.path.join(app, "Contents", "Info.plist"), "wb") as f:
            plistlib.dump({"CFBundleShortVersionString": "2.3.0"}, f)
        self.assertEqual(updater.installed_version("macos", app_path=app), "2.3.0")
        self.assertEqual(updater.installed_version("macos", app_path=os.path.join(tmp, "x")),
                         updater.__version__)
        self.assertEqual(updater.installed_version("macos"), updater.__version__)

    def test_curl_that_cannot_run_is_an_error_not_a_crash(self):
        with mock.patch.object(updater.subprocess, "run", side_effect=OSError("no curl")):
            c = updater.check_release("1.0.0", log=lambda m: None)
        self.assertIn("could not look up the latest release", c.error)
        self.assertIn("curl could not run: no curl", c.error)
        self.assertIsNone(c.release)
        with mock.patch.object(updater.subprocess, "run", side_effect=OSError("no dpkg-deb")):
            self.assertEqual(updater._deb_field("/x.deb", "Package"), "")
        with mock.patch.object(updater.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 2, "", "")):
            self.assertEqual(updater._deb_field("/x.deb", "Package"), "")
        with mock.patch.object(updater.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, " onionpress \n", "")):
            self.assertEqual(updater._deb_field("/x.deb", "Package"), "onionpress")


class TestSettingsPageOnlyChecks(unittest.TestCase):
    """handle-action `update` (whatever WordPress asks for) never installs,
    on either platform: it runs `update --check --json` and writes what it
    found. The flows (the cache, the request id, the messages) are in
    tests/test_handle_action.py's TestUpdate."""

    def test_handle_action_update_only_checks(self):
        handler = _read("src/onionpress/handle_action.py")
        update = handler[handler.index("def _update(host):"):handler.index("# -- recent logs")]
        self.assertIn('host.run_launcher(["update", "--check", "--json"]', update)
        for word in ('"--yes"', "settings-page-update-check", "apt-get", "sudo", "install_deb"):
            with self.subTest(word):
                self.assertNotIn(word, update)
        self.assertIn("run `onionpress update` on this machine", handle_action.LINUX_UPDATE_AVAILABLE_MSG)
        self.assertNotIn("settings-page-update-check", _read(LINUX))

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

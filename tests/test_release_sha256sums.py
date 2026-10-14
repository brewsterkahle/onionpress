#!/usr/bin/env python3
"""build/release.sh publishes SHA256SUMS with every release, and does not
finish a release without it.

Linux's `onionpress update` installs a release's onionpress.deb only when
its sha256 is the one the release's SHA256SUMS gives
(tests/test_linux_release_update.py), so a release without that file, or
with a stale one, cannot be installed that way. release.sh writes
"<sha256>  <name>" for onionpress.deb and onionpress.dmg (on Linux, the
release's own .dmg, downloaded), uploads it with them, then downloads
SHA256SUMS and the .deb back and checks them. A new release is created as a
draft and published only after that check.

release.sh runs for real in a scratch project, under /bin/bash (macOS's is
3.2), with gh and uname stubbed and the two build scripts replaced by ones
that write small files. Nothing reaches GitHub.
"""
import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TESTS_DIR)


def _write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb" if isinstance(text, bytes) else "w") as f:
        f.write(text)
    if mode is not None:
        os.chmod(path, mode)


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


GH_STUB = r'''#!/bin/bash
echo "gh $*" >> "$CALLS"
S="$GH_STATE"
[ "$1" = "release" ] || exit 2
cmd="$2"; tag="$3"; shift 3
case "$cmd" in
    view)
        [ -f "$S/exists" ] || exit 1
        case "$*" in
            *"--json assets"*) ls "$S/assets" ;;
            *"--json isDraft"*) if [ -f "$S/draft" ]; then echo true; else echo false; fi ;;
        esac
        ;;
    create)
        touch "$S/exists"
        mkdir -p "$S/assets"
        for a in "$@"; do
            case "$a" in
                --draft) touch "$S/draft" ;;
                -*) ;;
                *) n=$(basename "$a")
                   if [ -f "$a" ] && [ "${DROP_UPLOAD:-}" != "$n" ]; then cp "$a" "$S/assets/$n"; fi ;;
            esac
        done
        ;;
    upload)
        for a in "$@"; do
            case "$a" in
                -*) ;;
                *) n=$(basename "$a")
                   if [ "${DROP_UPLOAD:-}" != "$n" ]; then cp "$a" "$S/assets/$n"; fi ;;
            esac
        done
        ;;
    download)
        dir=""; pats=""
        while [ $# -gt 0 ]; do
            case "$1" in
                --pattern|-p) pats="$pats $2"; shift ;;
                --dir|-D) dir="$2"; shift ;;
            esac
            shift
        done
        for p in $pats; do
            [ -f "$S/assets/$p" ] || exit 1
            cp "$S/assets/$p" "$dir/$p"
            if [ "${TAMPER:-}" = "$p" ]; then echo tampered >> "$dir/$p"; fi
        done
        ;;
    edit)
        case "$*" in *"--draft=false"*) rm -f "$S/draft" ;; esac
        ;;
    *) exit 2 ;;
esac
'''

UNAME_STUB = '#!/bin/sh\necho "$FAKE_OS"\n'


class TestReleaseScript(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="op-release-sh-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.proj = os.path.join(self.tmp, "proj")
        build = os.path.join(self.proj, "build")
        os.makedirs(build)
        shutil.copy(os.path.join(PROJECT_DIR, "build", "release.sh"),
                    os.path.join(build, "release.sh"))
        _write(os.path.join(self.proj, "src", "menubar.py"), '        self.version = "9.8.7"\n')
        _write(os.path.join(build, "build-linux.sh"),
               '#!/bin/bash\nprintf "the deb built %s" "$BUILD_ID" > "$(dirname "$0")/onionpress.deb"\n',
               0o755)
        _write(os.path.join(build, "build-dmg-simple.sh"),
               '#!/bin/bash\nprintf "the dmg built %s" "$BUILD_ID" > "$(dirname "$0")/onionpress.dmg"\n',
               0o755)
        self.bin = os.path.join(self.tmp, "bin")
        _write(os.path.join(self.bin, "gh"), GH_STUB, 0o755)
        _write(os.path.join(self.bin, "uname"), UNAME_STUB, 0o755)
        self.state = os.path.join(self.tmp, "gh-state")
        os.makedirs(os.path.join(self.state, "assets"))
        self.calls_file = os.path.join(self.tmp, "calls")
        open(self.calls_file, "w").close()
        self.tmpdir = os.path.join(self.tmp, "tmpdir")
        os.makedirs(self.tmpdir)

    def existing_release(self, **assets):
        open(os.path.join(self.state, "exists"), "w").close()
        for name, data in assets.items():
            _write(os.path.join(self.state, "assets", name.replace("_", ".")), data)

    def run_release(self, *args, os_name="Darwin", **env):
        # /bin/bash: macOS's is 3.2, which release.sh must keep working on.
        return subprocess.run(
            ["/bin/bash", os.path.join(self.proj, "build", "release.sh"), *args],
            capture_output=True, text=True, timeout=60,
            env={"PATH": self.bin + os.pathsep + os.environ["PATH"], "HOME": self.tmp,
                 "TMPDIR": self.tmpdir, "CALLS": self.calls_file, "GH_STATE": self.state,
                 "FAKE_OS": os_name, "BUILD_ID": "1", **env})

    @property
    def calls(self):
        with open(self.calls_file) as f:
            return f.read().splitlines()

    def asset(self, name):
        with open(os.path.join(self.state, "assets", name), "rb") as f:
            return f.read()

    def is_draft(self):
        return os.path.exists(os.path.join(self.state, "draft"))

    def test_a_mac_release_publishes_sha256sums(self):
        r = self.run_release()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(sorted(os.listdir(os.path.join(self.state, "assets"))),
                         ["SHA256SUMS", "onionpress.deb", "onionpress.dmg"])
        self.assertEqual(self.asset("SHA256SUMS").decode(),
                         f"{_sha256(b'the deb built 1')}  onionpress.deb\n"
                         f"{_sha256(b'the dmg built 1')}  onionpress.dmg\n")
        with open(os.path.join(self.proj, "build", "SHA256SUMS"), "rb") as f:
            self.assertEqual(f.read(), self.asset("SHA256SUMS"))
        calls = self.calls
        create = next(i for i, c in enumerate(calls) if c.startswith("gh release create"))
        self.assertIn("--draft", calls[create])
        self.assertIn("/build/SHA256SUMS", calls[create])
        check = next(i for i, c in enumerate(calls)
                     if c.startswith("gh release download") and "SHA256SUMS" in c)
        publish = calls.index("gh release edit v9.8.7 --draft=false")
        self.assertLess(create, check)
        self.assertLess(check, publish)
        self.assertFalse(self.is_draft())
        self.assertEqual(os.listdir(self.tmpdir), [])

    def test_draft_stays_a_draft(self):
        r = self.run_release("--draft")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(self.is_draft())
        self.assertFalse([c for c in self.calls if c.startswith("gh release edit")])
        self.assertIn("Draft release v9.8.7 ready", r.stdout)

    def test_no_release_is_finished_without_sha256sums(self):
        # A new release: created, but left a draft.
        r = self.run_release(DROP_UPLOAD="SHA256SUMS")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("release v9.8.7 has no SHA256SUMS", r.stderr)
        self.assertTrue(self.is_draft())
        self.assertFalse([c for c in self.calls if c.startswith("gh release edit")])
        # An existing one: the script fails and says so.
        self.setUp()
        self.existing_release(onionpress_dmg=b"old dmg", onionpress_deb=b"old deb")
        r = self.run_release(DROP_UPLOAD="SHA256SUMS")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("release v9.8.7 has no SHA256SUMS", r.stderr)

    def test_a_published_package_that_does_not_match_is_refused(self):
        r = self.run_release(TAMPER="onionpress.deb")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("does not match SHA256SUMS", r.stderr)
        self.assertTrue(self.is_draft(), "a release that fails the check stays a draft")
        self.assertFalse([c for c in self.calls if c.startswith("gh release edit")])

    def test_stale_sha256sums_are_refused(self):
        self.existing_release(onionpress_dmg=b"old dmg", onionpress_deb=b"old deb",
                              SHA256SUMS=b"stale\n")
        r = self.run_release(DROP_UPLOAD="SHA256SUMS")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("SHA256SUMS published with v9.8.7 is not the one written here", r.stderr)

    def test_linux_attaches_the_deb_with_new_sums(self):
        self.existing_release(onionpress_dmg=b"the released dmg", onionpress_deb=b"old deb",
                              SHA256SUMS=b"old sums\n")
        r = self.run_release(os_name="Linux", BUILD_ID="2")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.asset("onionpress.deb"), b"the deb built 2")
        self.assertEqual(self.asset("SHA256SUMS").decode(),
                         f"{_sha256(b'the deb built 2')}  onionpress.deb\n"
                         f"{_sha256(b'the released dmg')}  onionpress.dmg\n")
        self.assertFalse([c for c in self.calls if c.startswith("gh release create")])
        self.assertEqual(self.asset("onionpress.dmg"), b"the released dmg")

    def test_linux_never_creates_a_release(self):
        r = self.run_release(os_name="Linux")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse([c for c in self.calls if c.startswith(("gh release create",
                                                                 "gh release upload"))])


if __name__ == "__main__":
    unittest.main()

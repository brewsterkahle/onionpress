"""Issue #387: everything the Mac bundle is built from is pinned by hash.

build-dmg-simple.sh fetched Colima, Lima, the Docker CLI and Compose by
version with no checksum, so two builds of one commit could differ and a
compromised upstream release flowed into the DMG with no record; the
binary cache made a release whatever was downloaded first on that Mac;
mkp224o fell back to a clone of master; libsodium's version came from
the build host's Homebrew; and the Python side floated (`>=` floors, an
unpinned py2app, wheels re-downloaded from PyPI).

Now every download goes through fetch_checked against a sha256 written
in the script, the cache keys carry those hashes, mkp224o is held to the
tag's commit with no fallback, libsodium is a pinned version and hash,
and every pip install or download takes its packages from the two locks
under build/ with --require-hashes.
"""

import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DMG = "build/build-dmg-simple.sh"
EXT = "build/py2app-extensions.sh"
REBUILD = "build/rebuild-menubar.sh"
RUNTIME_LOCK = "build/requirements-macos.lock"
BUILD_LOCK = "build/build-requirements-macos.lock"
DMG_BG_LOCK = "build/dmg-background-requirements-macos.lock"
SHA = r"[0-9a-f]{64}"


def _read(rel):
    with open(os.path.join(PROJECT_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _code(text):
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


class TestDownloads(unittest.TestCase):

    def setUp(self):
        self.code = _code(_read(DMG))

    def test_every_download_is_a_checked_fetch(self):
        # No bare curl that writes a file: license_file's is the only
        # other curl, and it fetches license texts, which are read as text.
        for m in re.finditer(r"(?m)^\s*curl [^\n]*-o ", self.code):
            line = self.code[self.code.rfind("\n", 0, m.start()) + 1:self.code.find("\n", m.end())]
            with self.subTest(line=line.strip()):
                self.assertIn('"$dest" "$url"', line, "a download outside fetch_checked / license_file")
        self.assertEqual(len(re.findall(r"(?m)^\s*fetch_checked ", self.code)), 9)

    def test_the_nine_hashes_are_pinned(self):
        for var in ("COLIMA_SHA256_X86_64", "COLIMA_SHA256_ARM64", "LIMA_SHA256_X86_64",
                    "LIMA_SHA256_ARM64", "DOCKER_SHA256_X86_64", "DOCKER_SHA256_ARM64",
                    "COMPOSE_SHA256_X86_64", "COMPOSE_SHA256_ARM64", "SODIUM_SHA256"):
            with self.subTest(var=var):
                self.assertRegex(self.code, rf'(?m)^{var}="{SHA}"$')
                self.assertIn(f'fetch_checked "${var}"', self.code)

    def test_cache_keys_carry_the_hashes(self):
        for key, var in (("COLIMA_CACHE_KEY", "COLIMA_SHA256_ARM64"),
                         ("LIMA_CACHE_KEY", "LIMA_SHA256_ARM64"),
                         ("DOCKER_CACHE_KEY", "DOCKER_SHA256_ARM64"),
                         ("COMPOSE_CACHE_KEY", "COMPOSE_SHA256_ARM64"),
                         ("MKP_CACHE_KEY", "SODIUM_SHA256")):
            with self.subTest(key=key):
                m = re.search(rf'(?m)^{key}="([^"]+)"$', self.code)
                self.assertIsNotNone(m)
                self.assertIn("${" + var + ":0:12}", m.group(1))
        self.assertIn("${MKP224O_COMMIT:0:12}", self.code)
        # Every cache_get/cache_put names a key variable, never a bare version.
        for m in re.finditer(r"cache_(?:get|put) \"([^\"]+)\"", self.code):
            with self.subTest(call=m.group(0)):
                self.assertRegex(m.group(1), r"^\$[A-Z_]+_CACHE_KEY(\.libsodium-version)?$")

    def test_mkp224o_is_the_tags_commit_and_never_master(self):
        self.assertRegex(self.code, r'(?m)^MKP224O_COMMIT="[0-9a-f]{40}"$')
        self.assertEqual(self.code.count("git clone"), 1)
        self.assertIn('git clone --branch "${MKP224O_VERSION}" --depth 1', self.code)
        self.assertNotRegex(self.code, r"git clone https://github.com/cathugger/mkp224o.git \"\$TEMP_BIN_DIR/mkp224o-src\" 2>/dev/null\n")
        self.assertIn('[ "$_mkp_head" != "$MKP224O_COMMIT" ]', self.code)
        # The checks happen outside the subshell whose failures are warnings.
        self.assertLess(self.code.index('[ "$_mkp_head" != "$MKP224O_COMMIT" ]'),
                        self.code.index("    (\n    set -e\n"))
        self.assertLess(self.code.index('fetch_checked "$SODIUM_SHA256"'),
                        self.code.index("    (\n    set -e\n"))

    def test_libsodium_is_pinned_not_homebrews(self):
        self.assertRegex(self.code, r'(?m)^SODIUM_VERSION="\d+\.\d+\.\d+"$')
        self.assertNotIn("pkg-config --modversion", self.code)
        self.assertNotIn("1.0.20-stable", self.code)


class TestFetchCheckedUnderBash(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="op-fetch-checked-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        src = _read(DMG)
        start = src.index("fetch_checked() {")
        self.fn = src[start:src.index("\n}\n", start) + 3]

    def _fake_curl(self, body):
        path = os.path.join(self.bin, "curl")
        with open(path, "w") as f:
            f.write('#!/bin/bash\nout=""\nwhile [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done\n'
                    f'printf %s "{body}" > "$out"\n')
        os.chmod(path, stat.S_IRWXU)

    def _run(self, want):
        dest = os.path.join(self.tmp, "file")
        r = subprocess.run(["bash", "-c", self.fn + f'\nfetch_checked "{want}" "{dest}" https://x/y\n'],
                           capture_output=True, text=True, timeout=30,
                           env={"PATH": self.bin + ":/usr/bin:/bin"})
        return r, dest

    def test_a_matching_download_is_kept(self):
        import hashlib
        self._fake_curl("hello")
        r, dest = self._run(hashlib.sha256(b"hello").hexdigest())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(dest))

    def test_a_mismatch_ends_the_build_and_removes_the_file(self):
        self._fake_curl("hello")
        r, dest = self._run("0" * 64)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not the pinned", r.stderr)
        self.assertFalse(os.path.exists(dest))


class TestPythonLocks(unittest.TestCase):

    def test_every_pip_install_or_download_requires_hashes(self):
        for rel in (DMG, EXT, REBUILD):
            code = _code(_read(rel)).replace("\\\n", " ")       # continuation lines joined
            for m in re.finditer(r"pip\" (?:install|download) [^\n]*", code):
                with self.subTest(file=rel, call=m.group(0)[:60]):
                    self.assertIn("--require-hashes", m.group(0))
            for m in re.finditer(r"-m pip install [^\n]*", code):
                with self.subTest(file=rel, call=m.group(0)[:60]):
                    self.assertIn("--require-hashes", m.group(0))
            self.assertNotIn("install --upgrade pip", code)
            self.assertNotIn("install py2app", code)
            self.assertNotIn("'setuptools<81'", code)

    def test_the_locks_pin_every_requirement_with_hashes(self):
        runtime = _read(RUNTIME_LOCK)
        wanted = [re.split(r"[<>=!~ ]", l, 1)[0].lower() for l in _read("src/requirements.txt").splitlines()
                  if l.strip() and not l.startswith("#")]
        for name in wanted:
            with self.subTest(package=name):
                self.assertRegex(runtime, rf"(?m)^{re.escape(name)}==\d")
        build = _read(BUILD_LOCK)
        for name in ("pip", "py2app", "setuptools", "wheel"):
            with self.subTest(package=name):
                self.assertRegex(build, rf"(?m)^{name}==\d")
        m = re.search(r"(?m)^setuptools==(\d+)", build)
        self.assertLess(int(m.group(1)), 81, "setuptools 81 removed distutils.spawn's dry_run (py2app #557)")
        self.assertRegex(_read(DMG_BG_LOCK), r"(?m)^pillow==\d")
        for rel in (RUNTIME_LOCK, BUILD_LOCK, DMG_BG_LOCK):
            text = _read(rel)
            pins = re.findall(r"(?m)^([a-z0-9_.-]+)==", text)
            for pkg in pins:
                with self.subTest(lock=rel, package=pkg):
                    block = text[text.index(f"\n{pkg}==") + 1:]
                    block = block[:block.find("\n\n") if "\n\n" in block else len(block)]
                    self.assertIn("--hash=sha256:", block)

    def test_the_refresh_script_writes_both_locks(self):
        text = _read("build/refresh-python-locks.sh")
        self.assertIn("--generate-hashes", text)
        self.assertIn("-o build/requirements-macos.lock", text)
        self.assertIn("-o build/build-requirements-macos.lock", text)
        self.assertIn("-o build/dmg-background-requirements-macos.lock", text)
        self.assertIn("src/requirements.txt", text)

    def test_the_build_venv_gets_no_package_outside_the_app_and_its_tools(self):
        # Pillow sat in the build lock, so it was installed into the venv
        # before py2app, and make_universal looked for its x86_64 wheel in
        # the runtime lock, which does not carry it: the first Mac build of
        # this change failed there. Pillow is installed only in the DMG
        # background fallback, after py2app, from a lock of its own.
        build = _read(BUILD_LOCK)
        self.assertNotRegex(build, r"(?mi)^pillow==")
        dmg = _code(_read(DMG))
        install = dmg.index("dmg-background-requirements-macos.lock")
        self.assertGreater(install, dmg.index("setup.py py2app"))
        self.assertIn("create-dmg-background.py", dmg[install:])

    def test_make_universal_refuses_a_package_the_runtime_lock_lacks(self):
        src = _read(EXT)
        body = src[src.index("make_universal() {"):]
        guard = body.index('if [ -z "$(lock_block "$dist")" ]; then')
        self.assertLess(guard, body.index("pip\" download"))
        self.assertIn("return 1", body[guard:body.index("pip\" download")])

    def test_lock_block_prints_one_packages_lines(self):
        src = _read(EXT)
        fn = src[src.index("lock_block() {"):src.index("\n}\n", src.index("lock_block() {")) + 3]
        r = subprocess.run(["bash", "-c", f'LOCK="{os.path.join(PROJECT_ROOT, RUNTIME_LOCK)}"\n' + fn
                            + "\nlock_block PyNaCl\n"], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.assertRegex(lines[0], r"^pynacl==\d")
        self.assertTrue(all(l.strip().startswith("--hash=sha256:") for l in lines[1:]), lines)
        self.assertFalse(lines[-1].rstrip().endswith("\\"), "the block must end a requirement")
        self.assertNotIn("# via", r.stdout)
        self.assertGreater(len(lines), 2)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""The .dmg must not ship bytecode caches from the developer's checkout.

build/build-dmg-simple.sh assembles OnionPress.app with `cp -R` from
app/Resources/ and src/onionpress/. `make test-unit` leaves __pycache__/ in
both — tests/test_onionnames.py and tests/test_onionheaven_integration.py
import from app/Resources/docker/tor — so a DMG built right after the tests
carried 31 .pyc files a clean-tree build did not: 24 under the py2app
bundle's lib/python3.14/onionpress/__pycache__/, 5 under docker/tor/__pycache__/
and 2 under docker/tor/wordlists/__pycache__/. The .deb had the same leak and
build-linux.sh strips it; these checks hold the .dmg to the same standard.

Like the rest of the build-script tests these are text checks, plus one that
runs the real strip function against a throwaway tree.
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPT = "build/build-dmg-simple.sh"


def _read(rel_path):
    with open(os.path.join(PROJECT_ROOT, rel_path), "r", encoding="utf-8") as f:
        return f.read()


def _code(rel_path):
    """Shell source with comment lines removed.

    The script explains the trap in prose right next to the command that
    avoids it, so a scan of the raw text would match the explanation.
    """
    return "\n".join(
        line for line in _read(rel_path).splitlines()
        if not line.lstrip().startswith("#")
    )


def _strip_pycache_definition(text):
    match = re.search(r"^strip_pycache\(\) \{\n.*?^\}", text, re.S | re.M)
    return match.group(0) if match else None


class TestDmgAssemblyStripsBytecode(unittest.TestCase):

    def setUp(self):
        self.code = _code(SCRIPT)

    def _index(self, needle):
        self.assertIn(
            needle, self.code,
            f"Could not find {needle!r} in {SCRIPT} — has it been "
            "restructured? Update this test.",
        )
        return self.code.index(needle)

    def test_defines_strip_pycache(self):
        func = _strip_pycache_definition(self.code)
        self.assertIsNotNone(
            func, f"{SCRIPT} must define strip_pycache() — the .deb build "
            "strips __pycache__ from every copied tree and the .dmg must too.",
        )
        self.assertRegex(func, r"-type d -name __pycache__",
                         "strip_pycache must remove __pycache__ directories.")
        self.assertRegex(func, r"-name '\*\.pyc'",
                         "strip_pycache must also remove stray *.pyc files.")

    def test_strips_the_resource_trees_right_after_copying_them(self):
        """docker/, plugins/, themes/ and scripts/ are copied with cp -R, which
        takes __pycache__ along. docker/tor/ is where the test suite leaves
        its bytecode.
        """
        copies = [
            self._index(f'cp -R "$PROJECT_DIR/app/Resources/{tree}" '
                        f'"$APP_PATH/Contents/Resources/{tree}"')
            for tree in ("docker", "plugins", "themes", "scripts")
        ]
        strip = self._index('strip_pycache "$APP_PATH/Contents/Resources"')
        self.assertGreater(
            strip, max(copies),
            "strip_pycache must run on Contents/Resources after all four "
            "resource trees have been copied in.",
        )
        self.assertLess(
            strip, self._index('echo "OnionPress.app assembled from app/ source"'),
            "The resource strip belongs in the assembly step, before the "
            "script moves on to the container-runtime binaries.",
        )

    def test_strips_the_package_copy_before_py2app_runs(self):
        """py2app copies src/onionpress from the build venv's site-packages
        into the bundle verbatim — this is where 24 of the 31 files came from.
        Stripping the copy is what keeps them out of py2app's input.
        """
        copy = self._index('cp -r "$SCRIPTS_DIR/onionpress" "$SITE_PACKAGES/"')
        strip = self._index('strip_pycache "$SITE_PACKAGES/onionpress"')
        py2app = self._index("setup.py py2app")
        self.assertLess(copy, strip,
                        "strip_pycache must run on the site-packages copy of "
                        "the package, after it is copied in.")
        self.assertLess(strip, py2app,
                        "...and before py2app builds from it.")

    def test_sweeps_the_assembled_bundle_before_signing(self):
        """Belt and braces: whatever future step copies bytecode in, none may
        remain when the bundle is sealed. It has to run before codesign, or
        the sweep would invalidate the signature it is meant to protect.
        """
        sweep = self._index('find "$APP_PATH" -type d -name __pycache__')
        installed = self._index(
            'mv "$MENUBAR_BUILD_DIR/dist/OnionPress.app" "$MENUBAR_APP_DIR"')
        signing = self._index('codesign -f -s - --deep "$APP_PATH"')
        self.assertLess(installed, sweep,
                        "The bundle-wide __pycache__ sweep must run after the "
                        "py2app MenubarApp has been installed into the bundle.")
        self.assertLess(sweep, signing,
                        "The bundle-wide __pycache__ sweep must run before the "
                        "bundle is signed.")

    def test_never_deletes_pyc_bundle_wide(self):
        """py2app byte-compiles site.py into the MenubarApp's
        Contents/Resources/site.pyc on purpose — it is the only .pyc a clean
        DMG contains. A `find $APP_PATH -name '*.pyc' -delete` would remove
        it, so the only *.pyc deletion allowed is the scoped one inside
        strip_pycache.
        """
        func = _strip_pycache_definition(self.code)
        self.assertIsNotNone(func)
        start = self.code.index(func)
        end = start + len(func)
        for match in re.finditer(r"\*\.pyc", self.code):
            self.assertTrue(
                start <= match.start() < end,
                f"'*.pyc' is matched outside strip_pycache at offset "
                f"{match.start()} — py2app ships site.pyc deliberately; only "
                f"__pycache__ directories may be swept bundle-wide.",
            )


class TestStripPycacheFunction(unittest.TestCase):
    """Run the function as the script defines it, not a re-implementation."""

    @unittest.skipUnless(shutil.which("bash") and shutil.which("find"),
                         "needs bash and find")
    def test_removes_caches_and_stray_pyc_and_nothing_else(self):
        func = _strip_pycache_definition(_read(SCRIPT))
        self.assertIsNotNone(func, "strip_pycache() not found in the script")

        removed = (
            "docker/tor/__pycache__/onionnames.cpython-314.pyc",
            "docker/tor/wordlists/__pycache__/en.cpython-314.pyc",
            "docker/tor/wordlists/__pycache__/__init__.cpython-314.pyc",
            "docker/tor/legacy.pyc",
            "onionpress/__pycache__/backup.cpython-314.pyc",
        )
        kept = (
            "docker/tor/onionnames.py",
            "docker/tor/wordlists/__init__.py",
            "docker/tor/wordlists/en.py",
            "docker/docker-compose.yml",
            "themes/onionpress/style.css",
            "plugins/notes-about-__pycache__.txt",
            "onionpress/backup.py",
        )
        with tempfile.TemporaryDirectory() as tmp:
            for rel in removed + kept:
                path = os.path.join(tmp, rel)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as f:
                    f.write(b"x")

            result = subprocess.run(
                ["bash", "-c", func + '\nstrip_pycache "$1"', "strip_pycache", tmp],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(
                0, result.returncode,
                f"strip_pycache failed:\n{result.stdout}\n{result.stderr}",
            )
            self.assertEqual("", result.stderr,
                             "strip_pycache must run clean — the build runs "
                             "under set -e and the .deb's variant had to "
                             "swallow find's errors.")

            for rel in removed:
                with self.subTest(removed=rel):
                    self.assertFalse(os.path.exists(os.path.join(tmp, rel)),
                                     f"{rel} should have been removed")
            for rel in kept:
                with self.subTest(kept=rel):
                    self.assertTrue(os.path.exists(os.path.join(tmp, rel)),
                                    f"{rel} should have been left alone")
            for dirpath, dirnames, _ in os.walk(tmp):
                self.assertNotIn("__pycache__", dirnames,
                                 f"a __pycache__ directory survived under {dirpath}")


class TestBuildDocsRecordTheFix(unittest.TestCase):

    def test_proven_table_dmg_row_mentions_bytecode(self):
        """docs/BUILDING.md's "What has been proven" table already treats
        stale .pyc as a defect for the .deb and the tor image; the .dmg row
        must say the same so the next reader knows it was checked.
        """
        doc = _read("docs/BUILDING.md")
        row = re.search(r"^\| `\.dmg` \|(.*)\|$", doc, re.M)
        self.assertIsNotNone(row, "No `.dmg` row in docs/BUILDING.md's table.")
        self.assertIn(".pyc", row.group(1),
                      "The .dmg row must record that the DMG ships no stale "
                      ".pyc, as the .deb row does.")


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""The committed binaries that used to have no build recipe.

build/onionpress-firefox.xpi, app/Resources/AppIcon.icns, the three menubar
PNGs and build/dmg-assets/* were all committed with nothing in the repo that
could produce them. These tests guard the recipes that now exist, and the few
sharp edges that make each one easy to break silently.
"""

import json
import os
import platform
import shutil
import subprocess
import tempfile
import unittest
import zipfile

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _read(rel_path):
    with open(os.path.join(PROJECT_ROOT, rel_path), "r", encoding="utf-8") as f:
        return f.read()


def _code(rel_path):
    """Shell source with comment lines removed.

    These scripts document the traps they avoid, so the prose contains the
    exact strings some of these checks scan for. An unstripped scan matches
    the warning instead of the command.
    """
    return "\n".join(
        line for line in _read(rel_path).splitlines()
        if not line.lstrip().startswith("#")
    )


class TestIconRecipe(unittest.TestCase):

    SCRIPT = "build/make-icons.sh"

    def test_script_exists_and_is_executable(self):
        path = os.path.join(PROJECT_ROOT, self.SCRIPT)
        self.assertTrue(os.path.exists(path), f"{self.SCRIPT} is missing.")
        self.assertTrue(os.access(path, os.X_OK))

    def test_squashes_rather_than_fits_the_master(self):
        """`sips -z H W` ignores aspect ratio, and that is load-bearing: the
        master is 992x1072 and is deliberately squashed to a square 1024x1024.
        "Fixing" it to `sips -Z 1024` yields 948x1024 and silently changes
        every icon layer and the .icns.
        """
        script = _code(self.SCRIPT)
        self.assertIn(
            "sips -z 1024 1024", script,
            "make-icons.sh must use `sips -z` (exact size), not `sips -Z` "
            "(fit within), for the app icon master.",
        )
        self.assertNotIn("sips -Z", script)

    def test_does_not_round_trip_the_existing_icns(self):
        """Exporting the committed .icns with `iconutil --convert iconset` and
        re-converting is lossy: ic04/ic05 are stored as raw ARGB and get
        re-encoded, yielding a file of identical length that differs in two
        bytes. The iconset must be built from the PNG master.
        """
        script = _code(self.SCRIPT)
        self.assertNotIn(
            "--convert iconset", script,
            "make-icons.sh must build the iconset from app-icon.png, not by "
            "exporting the existing .icns (that round-trip is lossy).",
        )

    @unittest.skipUnless(platform.system() == "Darwin", "needs macOS sips/iconutil")
    @unittest.skipUnless(shutil.which("sips") and shutil.which("iconutil"),
                         "sips/iconutil not available")
    def test_regenerates_the_committed_icons_byte_for_byte(self):
        """The reason to trust this script. --verify rebuilds into a temp dir
        and diffs; it writes nothing.
        """
        result = subprocess.run(
            ["bash", os.path.join(PROJECT_ROOT, self.SCRIPT), "--verify", "--icns"],
            capture_output=True, text=True, cwd=PROJECT_ROOT, timeout=180,
        )
        self.assertEqual(
            0, result.returncode,
            f"make-icons.sh --verify failed:\n{result.stdout}\n{result.stderr}",
        )
        for name in ("app-icon.png", "AppIcon.icns"):
            with self.subTest(name=name):
                self.assertRegex(
                    result.stdout, rf"IDENTICAL\s+app/Resources/{name}",
                    f"{name} no longer regenerates byte-identically:\n"
                    f"{result.stdout}",
                )

    def test_is_honest_about_the_menubar_icons(self):
        """They were made with ImageMagick and the recipe is reconstructed
        from the committed files' embedded metadata, not verified. The script
        must say so rather than implying the same guarantee as the .icns.
        """
        script = _read(self.SCRIPT)
        self.assertIn("Rec709Luma", script,
                      "ImageMagick 7's -colorspace Gray is linear and does "
                      "not match the committed file; Rec709Luma does.")
        self.assertIn("ImageMagick", script)


class TestExtensionRecipe(unittest.TestCase):

    SCRIPT = "build/build-extension.sh"

    def test_script_exists_and_is_executable(self):
        path = os.path.join(PROJECT_ROOT, self.SCRIPT)
        self.assertTrue(os.path.exists(path), f"{self.SCRIPT} is missing.")
        self.assertTrue(os.access(path, os.X_OK))

    def test_firefox_manifest_is_the_current_one(self):
        """There are two Firefox manifests and they are not equivalent.
        extension/manifest.firefox.json is stale: it re-requests webRequest,
        webRequestBlocking, webNavigation and <all_urls>, and has no
        data_collection_permissions, which AMO requires from Firefox 142.
        Building from it would ship an AMO-rejectable package with broader
        permissions than the extension needs.
        """
        current = json.loads(_read("extension-firefox/manifest.json"))
        gecko = current["browser_specific_settings"]["gecko"]
        self.assertIn(
            "data_collection_permissions", gecko,
            "extension-firefox/manifest.json must declare "
            "data_collection_permissions — AMO requires it from Firefox 142.",
        )
        for permission in ("webRequest", "webRequestBlocking", "webNavigation",
                           "<all_urls>"):
            with self.subTest(permission=permission):
                self.assertNotIn(
                    permission, current["permissions"],
                    f"The current Firefox manifest must not request "
                    f"{permission}.",
                )

    def test_build_script_uses_the_overlay_not_the_stale_manifest(self):
        script = _read(self.SCRIPT)
        self.assertIn("extension-firefox", script)
        self.assertIn(
            'rm -f "$stage/manifest.json" "$stage/manifest.firefox.json"', script,
            "The Firefox build must drop both of extension/'s manifests "
            "before overlaying extension-firefox/'s.",
        )

    def test_chrome_package_excludes_the_firefox_manifest(self):
        script = _read(self.SCRIPT)
        self.assertIn('rm -f "$stage/manifest.firefox.json"', script)

    def test_archives_are_reproducible(self):
        """Fixed timestamps, sorted entries and -X (no uid/gid/extra fields),
        so two builds of the same sources match byte for byte.
        """
        script = _read(self.SCRIPT)
        self.assertIn("zip -q -X", script)
        self.assertIn("sort", script)
        self.assertIn("FIXED_DATE", script)

    @unittest.skipUnless(shutil.which("zip") and shutil.which("unzip"),
                         "zip/unzip not available")
    def test_builds_a_wellformed_firefox_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            result = subprocess.run(
                ["bash", os.path.join(PROJECT_ROOT, self.SCRIPT), "firefox"],
                capture_output=True, text=True, cwd=PROJECT_ROOT, env=env,
                timeout=120,
            )
            self.assertEqual(
                0, result.returncode,
                f"build-extension.sh failed:\n{result.stdout}\n{result.stderr}",
            )
            xpi = os.path.join(PROJECT_ROOT, "build/dist/onionpress-firefox.xpi")
            self.assertTrue(os.path.exists(xpi), "no .xpi was produced")

            with zipfile.ZipFile(xpi) as archive:
                names = set(archive.namelist())
                manifest = json.loads(archive.read("manifest.json"))

            # Everything the manifest points at must be in the package. A
            # missing icon or popup installs fine and then misbehaves.
            self.assertIn("offline.js", names,
                          "offline.js must ship — extension-firefox/ "
                          "externalised the inline script to satisfy the "
                          "extension CSP.")
            self.assertNotIn("content.js", names,
                             "content.js is not referenced by the Firefox "
                             "manifest and should not ship.")
            self.assertNotIn("manifest.firefox.json", names)
            self.assertIn(
                "data_collection_permissions",
                manifest["browser_specific_settings"]["gecko"],
            )
            _ = tmp  # temp dir kept for symmetry; the script writes to build/dist


class TestRepoTrackingOfExtensionSources(unittest.TestCase):

    def test_extension_firefox_is_not_gitignored(self):
        """.gitignore listed `extension-firefox/` while four of its files were
        tracked. Ignore rules do not apply to already-tracked files, so the
        entry was inert for those four — but any NEW file added there would
        have been silently untracked, and that directory holds the current
        Firefox manifest.
        """
        self.assertNotIn(
            "extension-firefox/", _read(".gitignore"),
            "extension-firefox/ holds the current Firefox extension sources "
            "and must not be gitignored.",
        )

    def test_generated_extension_output_is_gitignored(self):
        self.assertIn(
            "build/dist/", _read(".gitignore"),
            "build/dist/ is generated by build/build-extension.sh and should "
            "not be committed.",
        )


class TestDmgAssets(unittest.TestCase):

    def test_ds_store_keeps_its_dotless_name(self):
        """The repo ignores `.DS_Store`. Renaming this capture to `.DS_Store`
        makes git ignore it silently, and the next DMG build loses all of its
        window styling with no error.
        """
        self.assertTrue(
            os.path.exists(os.path.join(PROJECT_ROOT, "build/dmg-assets/DS_Store")),
            "build/dmg-assets/DS_Store is missing or was renamed with a "
            "leading dot, which .gitignore would swallow.",
        )

    def test_provenance_is_documented(self):
        """It cannot be regenerated by a script — it is a Finder capture that
        embeds volume metadata and a build-machine path.
        """
        readme = _read("build/dmg-assets/README.md")
        self.assertIn("capture", readme.lower())
        self.assertIn("create-dmg-background.py", readme)


if __name__ == "__main__":
    unittest.main()

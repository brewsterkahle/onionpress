"""OnionPress is AGPL-3.0-or-later, and says so everywhere it ships.

A license audit (2026-10-14) found every dependency compatible with the
AGPL and six gaps in OnionPress's own declarations: the AppStream metainfo
still said MIT (the first commit's license, 2026-01-31), nothing named the
copyright holder, the WordPress plugin headers said "AGPL-3.0" or nothing,
the entry scripts carried no notice, the Mac bundle shipped Colima, Lima,
the Docker CLI, Compose, mkp224o and libsodium without their license texts,
and the .deb had no copyright file. These tests keep each one closed.

The DMG build's license staging and notices writer run for real here,
with a curl stub standing in for GitHub, and so does build/python-notices.py,
which adds what py2app bundles into the MenubarApp, against a fake build venv
and bundle.
"""
import importlib.util
import os
import plistlib
import re
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

SPDX = "AGPL-3.0-or-later"
LICENSE_URI = "https://www.gnu.org/licenses/agpl-3.0.html"
COPYRIGHT_LINE = "Copyright (C) 2026 Internet Archive and OnionPress contributors"

# The short header every entry script carries, as the GNU AGPL's "How to
# Apply These Terms" asks, with the SPDX identifier in front.
HEADER = f"""SPDX-License-Identifier: {SPDX}

OnionPress: {COPYRIGHT_LINE}

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""

ENTRY_SCRIPTS = (
    "src/menubar.py",
    "src/onionpress/__init__.py",
    "app/MacOS/onionpress",
    "app/MacOS/launcher.sh",
    "linux/onionpress",
    "linux/onionpress-tray",
    "app/Resources/docker/tor/entrypoint.sh",
    "app/Resources/docker/wordpress/onionpress-entrypoint.sh",
)


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


class TestLicenseDeclarations(unittest.TestCase):

    def test_appstream_metainfo_says_agpl(self):
        text = _read("linux/onionpress.metainfo.xml")
        self.assertIn(f"<project_license>{SPDX}</project_license>", text)
        self.assertNotIn("MIT", text)

    def test_license_file_names_the_holder_above_the_fsf_text(self):
        text = _read("LICENSE")
        self.assertTrue(text.startswith(f"OnionPress: {COPYRIGHT_LINE}\n"), text[:120])
        self.assertIn(f"SPDX-License-Identifier: {SPDX}", text[:400])
        # The FSF's text, whole, follows the short header.
        self.assertIn("                    GNU AFFERO GENERAL PUBLIC LICENSE\n"
                      "                       Version 3, 19 November 2007\n", text)
        self.assertIn("How to Apply These Terms to Your New Programs", text)
        self.assertTrue(text.rstrip().endswith("<https://www.gnu.org/licenses/>."))

    def test_readme_license_section(self):
        text = _read("README.md")
        section = text[text.index("## License"):text.index("## Credits")]
        self.assertIn(COPYRIGHT_LINE, section)
        self.assertIn(f"`{SPDX}`", section)
        self.assertIn("[LICENSE](LICENSE)", section)
        self.assertIn("THIRD-PARTY-NOTICES.txt", section)
        self.assertIn("/usr/share/doc/onionpress/copyright", section)
        self.assertNotIn("AGPL 3 License", text)
        self.assertNotIn("(AGPL 3)", text)

    def test_mac_bundle_plists_carry_the_copyright(self):
        with open(os.path.join(ROOT, "app/Info.plist"), "rb") as f:
            outer = plistlib.load(f)
        self.assertIn("NSHumanReadableCopyright", outer)
        self.assertIn("2026 Internet Archive and OnionPress contributors",
                      outer["NSHumanReadableCopyright"])
        self.assertIn("AGPL", outer["NSHumanReadableCopyright"])
        setup = _read("setup.py")
        m = re.search(r"^COPYRIGHT = \((?:\s*'[^']*'\s*)+\)", setup, re.M)
        self.assertIsNotNone(m, "setup.py must define COPYRIGHT")
        inner = "".join(re.findall(r"'([^']*)'", m.group(0)))
        self.assertEqual(inner, outer["NSHumanReadableCopyright"],
                         "the MenubarApp's plist and app/Info.plist must say the same")
        self.assertIn("'NSHumanReadableCopyright': COPYRIGHT,", setup)
        self.assertIn(f"license='{SPDX}'", setup)

    def test_about_boxes_say_or_later(self):
        self.assertIn('ABOUT_LICENSE = "License: AGPL v3 or later"', _read("src/onionpress/ui_client.py"))
        self.assertIn('ui_client.about_text(self.version, "your Mac")', _read("src/menubar.py"))
        # GTK's AGPL_3_0 is "version 3 or later"; AGPL_3_0_ONLY is the other.
        self.assertIn("dlg.set_license_type(Gtk.License.AGPL_3_0)", _read("linux/onionpress-tray"))


class TestEntryScriptHeaders(unittest.TestCase):
    """Every entry point carries the AGPL short header, verbatim, right
    after its shebang (or at the top of a module without one)."""

    def test_header_on_every_entry_script(self):
        for rel in ENTRY_SCRIPTS:
            with self.subTest(rel):
                lines = _read(rel).split("\n")
                if lines[0].startswith("#!"):
                    lines = lines[1:]
                comment = []
                for line in lines:
                    if not line.startswith("#"):
                        break
                    comment.append(line[2:] if line.startswith("# ") else line[1:])
                self.assertEqual("\n".join(comment) + "\n", HEADER, rel)

    def test_header_is_the_gnu_text(self):
        # The three paragraphs the AGPL's "How to Apply These Terms" gives,
        # unchanged, so a reader sees the standard notice.
        fsf = _read("LICENSE")
        start = fsf.index("    This program is free software: you can redistribute it")
        end = fsf.index("Also add information on how to contact you")
        paragraphs = "\n".join(l[4:] if l.startswith("    ") else l
                               for l in fsf[start:end].rstrip().split("\n"))
        self.assertIn(paragraphs, HEADER)


class TestPluginAndThemeHeaders(unittest.TestCase):
    """Every WordPress plugin header and the theme's name the license as
    WordPress asks: License and License URI."""

    def _plugin_files(self):
        found = []
        for d, _, files in os.walk(os.path.join(ROOT, "app/Resources/plugins")):
            for f in files:
                if f.endswith(".php"):
                    found.append(os.path.relpath(os.path.join(d, f), ROOT))
        return sorted(found)

    @staticmethod
    def _first_docblock(text):
        start = text.find("/**")
        if start == -1:
            return ""
        return text[start:text.find("*/", start)]

    def test_every_plugin_header(self):
        with_header = []
        for rel in self._plugin_files():
            block = self._first_docblock(_read(rel))
            if "Plugin Name:" not in block:
                continue
            with_header.append(rel)
            with self.subTest(rel):
                self.assertRegex(block, r"\n \* License:\s+" + re.escape(SPDX) + r"\n")
                self.assertRegex(block, r"\n \* License URI:\s+" + re.escape(LICENSE_URI) + r"\n")
        self.assertGreaterEqual(len(with_header), 28, with_header)
        for rel in ("app/Resources/plugins/onionpress-tor-proxy.php",
                    "app/Resources/plugins/onionpress-hit-counter/onionpress-hit-counter.php",
                    "app/Resources/plugins/onionpress-creations/onionpress-creations.php"):
            self.assertIn(rel, with_header)

    def test_theme_header(self):
        text = _read("app/Resources/themes/onionpress/style.css")
        head = text[:text.index("*/")]
        self.assertIn(f"\nLicense: {SPDX}\n", head)
        self.assertIn(f"\nLicense URI: {LICENSE_URI}\n", head)

    def test_no_bare_agpl_3_0_anywhere_shipped(self):
        for d, _, files in os.walk(os.path.join(ROOT, "app/Resources")):
            for f in files:
                if f.endswith((".php", ".css", ".md")):
                    rel = os.path.relpath(os.path.join(d, f), ROOT)
                    with self.subTest(rel):
                        self.assertNotRegex(_read(rel), r"AGPL-3\.0(?!-or-later)")


class TestNounProjectIconGone(unittest.TestCase):

    def test_no_reference_and_no_file(self):
        self.assertFalse(os.path.exists(os.path.join(ROOT, "assets/branding/noun-computer-5963091.svg")))
        r = subprocess.run(["git", "grep", "-l", "noun-computer", "--", ".",
                            ":!tests/test_license_declarations.py"], cwd=ROOT,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 1, r.stdout)  # 1: no match
        self.assertNotIn("noun-computer", _read("setup.py"))


class TestDebCopyright(unittest.TestCase):

    def test_dep5_copyright_file(self):
        text = _read("linux/copyright")
        self.assertTrue(text.startswith(
            "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n"))
        self.assertIn("Upstream-Name: OnionPress\n", text)
        self.assertIn("Source: https://github.com/brewsterkahle/onionpress\n", text)
        self.assertIn("\nFiles: *\nCopyright: 2026 Internet Archive and OnionPress contributors\n"
                      f"License: {SPDX}\n", text)
        self.assertIn(f"\nLicense: {SPDX}\n This program is free software", text)
        self.assertIn('"/usr/share/common-licenses/AGPL-3"', text)
        for line in text.split("\n"):
            # A continuation line starts with a space, a paragraph break is " .".
            self.assertFalse(line.startswith("\t"), repr(line))

    def test_deb_build_installs_it(self):
        script = _read("build/build-linux.sh")
        self.assertIn('mkdir -p "$DEB_ROOT/usr/share/doc/onionpress"\n'
                      'install -m 0644 "$PROJECT_DIR/linux/copyright" \\\n'
                      '    "$DEB_ROOT/usr/share/doc/onionpress/copyright"\n', script)


def _shell_function(text, name):
    m = re.search(r"^" + name + r"\(\) \{.*?^\}", text, re.DOTALL | re.MULTILINE)
    if m is None:
        raise AssertionError(f"{name}() not found")
    return m.group(0)


CURL_STUB = r"""#!/bin/sh
# curl stand-in: logs the URL, then writes a text naming it to -o.
# CURL_FAIL=ALL refuses every URL (exit 22, nothing written);
# CURL_EMPTY=1 writes an empty file and reports success.
out=""; url=""
while [ $# -gt 0 ]; do
    case "$1" in
        -o|--max-time|--retry) out_or_n="$2"; [ "$1" = -o ] && out="$2"; shift 2 ;;
        -*) shift ;;
        *) url="$1"; shift ;;
    esac
done
echo "$url" >> "$CURL_LOG"
[ "${CURL_FAIL:-}" = ALL ] && exit 22
if [ "${CURL_EMPTY:-}" = 1 ]; then : > "$out"; else printf 'text of %s\n' "$url" > "$out"; fi
"""


class TestThirdPartyNotices(unittest.TestCase):
    """build-dmg-simple.sh stages each bundled project's license text and
    writes Contents/Resources/THIRD-PARTY-NOTICES.txt from them. The staging
    functions, the calls and the writer run here, extracted from the script,
    against a curl stub."""

    @classmethod
    def setUpClass(cls):
        cls.script = _read("build/build-dmg-simple.sh")
        s = cls.script
        cls.versions = dict(re.findall(r'^(\w+_VERSION)="([^"]+)"', s, re.M))
        version_lines = re.findall(
            r'^(?:COLIMA|LIMA|DOCKER|DOCKER_COMPOSE|MKP224O)_VERSION=.*$', s, re.M)
        calls = re.findall(r"^license_file (?:[^\n]*\\\n)*[^\n]*$", s, re.M)
        sodium = re.search(r'^SODIUM_VERSION=\$\(cat .*\n.*$', s, re.M).group(0)
        writer = s[s.index('NOTICES_FILE="'):s.index("# Clean up temp directory")]
        cls.harness = "\n".join(version_lines + [
            _shell_function(s, "license_file"), _shell_function(s, "notice_section"),
            sodium] + calls + [writer])
        cls.n_calls = len(calls)

    def _run(self, tmp, env_extra=None, archives=(), keep_cache=True):
        proj = os.path.join(tmp, "proj")
        bins = os.path.join(tmp, "bin")
        app = os.path.join(tmp, "app")
        stub = os.path.join(tmp, "stub")
        for d in (proj, bins, os.path.join(app, "Contents", "Resources"), stub):
            os.makedirs(d, exist_ok=True)
        if not keep_cache:
            subprocess.run(["rm", "-rf", os.path.join(proj, "build")], check=True)
        with open(os.path.join(stub, "curl"), "w") as f:
            f.write(CURL_STUB)
        os.chmod(os.path.join(stub, "curl"), stat.S_IRWXU)
        for rel, text in archives:
            path = os.path.join(bins, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(text)
        with open(os.path.join(bins, "libsodium-version"), "w") as f:
            f.write("1.0.20\n")
        log = os.path.join(tmp, "curl.log")
        open(log, "w").close()
        env = {"PATH": stub + ":/usr/bin:/bin", "CURL_LOG": log,
               "PROJECT_DIR": proj, "TEMP_BIN_DIR": bins, "APP_PATH": app}
        env.update(env_extra or {})
        prelude = ('set -e\n'
                   'LICENSE_CACHE_DIR="$PROJECT_DIR/build/.cache/licenses"\n'
                   'NOTICES_STAGE="$TEMP_BIN_DIR/licenses"\n'
                   'mkdir -p "$LICENSE_CACHE_DIR" "$NOTICES_STAGE"\n')
        r = subprocess.run(["bash", "-c", prelude + self.harness], env=env,
                           capture_output=True, text=True, timeout=120)
        with open(log) as f:
            urls = f.read().split()
        notices = os.path.join(app, "Contents", "Resources", "THIRD-PARTY-NOTICES.txt")
        text = None
        if os.path.exists(notices):
            with open(notices) as f:
                text = f.read()
        return r, urls, text, os.path.join(proj, "build", ".cache", "licenses")

    def expected_urls(self):
        v = self.versions
        return {
            f"https://raw.githubusercontent.com/abiosoft/colima/{v['COLIMA_VERSION']}/LICENSE",
            f"https://raw.githubusercontent.com/lima-vm/lima/v{v['LIMA_VERSION']}/LICENSE",
            f"https://raw.githubusercontent.com/lima-vm/lima/v{v['LIMA_VERSION']}/NOTICE",
            f"https://raw.githubusercontent.com/docker/cli/v{v['DOCKER_VERSION']}/LICENSE",
            f"https://raw.githubusercontent.com/docker/cli/v{v['DOCKER_VERSION']}/NOTICE",
            f"https://raw.githubusercontent.com/docker/compose/{v['DOCKER_COMPOSE_VERSION']}/LICENSE",
            f"https://raw.githubusercontent.com/docker/compose/{v['DOCKER_COMPOSE_VERSION']}/NOTICE",
            f"https://raw.githubusercontent.com/cathugger/mkp224o/{v['MKP224O_VERSION']}/COPYING.txt",
            "https://raw.githubusercontent.com/jedisct1/libsodium/1.0.20-RELEASE/LICENSE",
        }

    def test_fetches_every_text_from_the_pinned_tags_and_writes_the_file(self):
        self.assertEqual(self.n_calls, 9)
        with tempfile.TemporaryDirectory() as tmp:
            r, urls, text, cache = self._run(tmp)
            self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
            self.assertEqual(set(urls), self.expected_urls())
            self.assertEqual(len(urls), 9, "each text fetched once")
            self.assertIsNotNone(text)
            self.assertEqual(text.count("\nLicense: "), 6)
            for heading in (f"\nColima {self.versions['COLIMA_VERSION']}\nLicense: MIT\n",
                            f"\nLima v{self.versions['LIMA_VERSION']}\nLicense: Apache-2.0\n",
                            f"\nDocker CLI v{self.versions['DOCKER_VERSION']}\nLicense: Apache-2.0\n",
                            f"\nDocker Compose {self.versions['DOCKER_COMPOSE_VERSION']}\nLicense: Apache-2.0\n",
                            f"\nmkp224o {self.versions['MKP224O_VERSION']}\nLicense: CC0-1.0\n",
                            "\nlibsodium 1.0.20\nLicense: ISC\n"):
                self.assertIn(heading, text)
            for url in self.expected_urls():
                self.assertIn(f"text of {url}\n", text, "each fetched text, verbatim")
            self.assertIn("--- NOTICE (Docker CLI) ---", text)
            self.assertIn("--- COPYING.txt (mkp224o) ---", text)
            self.assertIn(COPYRIGHT_LINE, text)
            self.assertIn("Contents/Resources/bin/mkp224o", text)
            # Cached by project and version for the next build.
            self.assertEqual(sorted(os.listdir(cache)), sorted([
                f"colima-{self.versions['COLIMA_VERSION']}-LICENSE",
                f"lima-{self.versions['LIMA_VERSION']}-LICENSE",
                f"lima-{self.versions['LIMA_VERSION']}-NOTICE",
                f"docker-cli-{self.versions['DOCKER_VERSION']}-LICENSE",
                f"docker-cli-{self.versions['DOCKER_VERSION']}-NOTICE",
                f"docker-compose-{self.versions['DOCKER_COMPOSE_VERSION']}-LICENSE",
                f"docker-compose-{self.versions['DOCKER_COMPOSE_VERSION']}-NOTICE",
                f"mkp224o-{self.versions['MKP224O_VERSION']}-COPYING.txt",
                "libsodium-1.0.20-LICENSE"]))

    def test_archives_win_then_the_cache_and_no_network_is_needed(self):
        archives = [
            ("lima-arm64/share/doc/lima/LICENSE", "lima license from the tarball\n"),
            ("mkp224o-src/COPYING.txt", "mkp224o CC0 from the clone\n"),
            ("libsodium-src/LICENSE", "libsodium ISC from the tarball\n"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            r, urls, text, _ = self._run(tmp)   # fills the cache
            self.assertEqual(r.returncode, 0, r.stderr)
            r, urls, text, _ = self._run(tmp, {"CURL_FAIL": "ALL"}, archives)
            self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
            self.assertEqual(urls, [], "nothing fetched: archives and cache")
            for _, body in archives:
                self.assertIn(body, text)
            self.assertNotIn("text of https://raw.githubusercontent.com/lima-vm/lima/v"
                             f"{self.versions['LIMA_VERSION']}/LICENSE", text)
            self.assertEqual(text.count("\nLicense: "), 6)

    def test_a_missing_or_empty_text_fails_the_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            r, urls, text, _ = self._run(tmp, {"CURL_FAIL": "ALL"})
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("ERROR: could not fetch", r.stderr)
            self.assertIsNone(text)
        with tempfile.TemporaryDirectory() as tmp:
            r, urls, text, _ = self._run(tmp, {"CURL_EMPTY": "1"})
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("is empty", r.stderr)
            self.assertIsNone(text)

    def test_script_wiring(self):
        s = self.script
        self.assertIn('NOTICES_FILE="$APP_PATH/Contents/Resources/THIRD-PARTY-NOTICES.txt"', s)
        # Written while the staged texts still exist, before the temp dir goes.
        self.assertLess(s.index('} > "$NOTICES_FILE"'), s.index('rm -rf "$TEMP_BIN_DIR"'))
        # Lima's LICENSE rides in the cached bundle from now on.
        self.assertIn("        lima-arm64/share/lima \\\n        lima-arm64/share/doc/lima/LICENSE)", s)
        # The libsodium version is recorded beside the mkp224o binary.
        self.assertIn('cache_put "$MKP_CACHE_KEY.libsodium-version"', s)
        self.assertIn('cache_get "$MKP_CACHE_KEY.libsodium-version"', s)
        self.assertIn("LICENSE_CACHE_DIR=\"$PROJECT_DIR/build/.cache/licenses\"", s)


def _python_notices_module():
    spec = importlib.util.spec_from_file_location(
        "python_notices", os.path.join(ROOT, "build", "python-notices.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PYOBJC_CORE_LICENSE_URL = ("https://raw.githubusercontent.com/ronaldoussoren/pyobjc/"
                           "v12.2.2/pyobjc-core/License.txt")

# Python wrapper standing in for the build venv's python3: runs the real
# interpreter on the script, pointed at the fake tree.
VENV_PYTHON_STUB = """#!/bin/sh
exec "$REAL_PYTHON" "$@" --site-packages "$FAKE_SITE" --stdlib "$FAKE_STDLIB" \\
    --python-version 3.14.0
"""


class TestPythonNotices(unittest.TestCase):
    """build/python-notices.py appends the MenubarApp's Python side to the
    notices file: the interpreter and every installed distribution with a
    module in the py2app bundle, each with its license texts. Run here
    against a fake build venv (the metadata layouts pip really produces),
    a fake bundle and a curl stub."""

    @classmethod
    def setUpClass(cls):
        cls.script = _read("build/build-dmg-simple.sh")
        cls.module = _python_notices_module()

    # -- the fake tree ---------------------------------------------------

    @staticmethod
    def _dist_info(site, dirname, metadata, top_level=None, files=(), record=()):
        d = os.path.join(site, dirname)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "METADATA"), "w") as f:
            f.write("Metadata-Version: 2.4\n" + metadata + "\nSummary: fake\n\nbody\n")
        if top_level is not None:
            with open(os.path.join(d, "top_level.txt"), "w") as f:
                f.write("\n".join(top_level) + "\n")
        with open(os.path.join(d, "RECORD"), "w") as f:
            for rel in list(record) + [f"{dirname}/METADATA", f"{dirname}/RECORD"]:
                f.write(f"{rel},,\n")
                # Distribution.files (3.12+) lists only what is on disk.
                path = os.path.join(site, rel)
                if not rel.startswith("..") and not os.path.exists(path):
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    open(path, "w").close()
        for rel, text in files:
            path = os.path.join(d, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(text)
        return d

    def _fake_tree(self, tmp):
        site = os.path.join(tmp, "venv", "lib", "python3.14", "site-packages")
        stdlib = os.path.join(tmp, "stdlib")
        bundle = os.path.join(tmp, "MenubarApp")
        os.makedirs(site)
        os.makedirs(stdlib)
        with open(os.path.join(stdlib, "LICENSE.txt"), "w") as f:
            f.write("A. HISTORY OF THE SOFTWARE\n(the PSF license text)\n")
        # PEP 639 layout, two texts (PyNaCl 1.6.2's: its own and libsodium's).
        self._dist_info(site, "pynacl-1.6.2.dist-info",
                        "Name: PyNaCl\nVersion: 1.6.2\nLicense: Apache-2.0\n"
                        "Classifier: License :: OSI Approved :: Apache Software License\n"
                        "License-File: LICENSE\nLicense-File: licenses/LICENSE.libsodium.txt",
                        ["_sodium", "nacl"],
                        [("licenses/LICENSE", "Apache License\nVersion 2.0 (PyNaCl's text)\n"),
                         ("licenses/licenses/LICENSE.libsodium.txt", "ISC License (libsodium's text)\n")])
        self._dist_info(site, "cffi-2.1.1.dist-info",
                        "Name: cffi\nVersion: 2.1.1\nLicense-Expression: MIT-0\nLicense-File: LICENSE",
                        ["_cffi_backend", "cffi"],
                        [("licenses/LICENSE", "MIT No Attribution (cffi's text)\n")])
        # The older layout: the text beside METADATA; "BSD License" with a
        # three-clause text (rumps).
        self._dist_info(site, "rumps-0.4.0.dist-info",
                        "Name: rumps\nVersion: 0.4.0\nLicense: BSD License\n"
                        "Classifier: License :: OSI Approved :: BSD License\nLicense-File: LICENSE",
                        ["rumps"],
                        [("LICENSE", "Copyright (c) 2020, Jared Suttles.\n"
                                     "1. Redistributions of source code must retain...\n"
                                     "2. Redistributions in binary form must reproduce...\n"
                                     "3. Neither the name of rumps nor...\n")])
        # No text in the wheel at all (pyobjc-core): fetched from the tag.
        self._dist_info(site, "pyobjc_core-12.2.2.dist-info",
                        "Name: pyobjc-core\nVersion: 12.2.2\nLicense: MIT",
                        ["PyObjCTest", "PyObjCTools", "objc"],
                        [("include/pyobjc-api.h", "/* not a license */\n")])
        self._dist_info(site, "pyobjc_framework_cocoa-12.2.2.dist-info",
                        "Name: pyobjc-framework-Cocoa\nVersion: 12.2.2\nLicense: MIT\n"
                        "License-File: LICENSE.txt",
                        ["AppKit", "Cocoa", "CoreFoundation", "Foundation", "PyObjCTest", "PyObjCTools"],
                        [("licenses/LICENSE.txt", "(This is the MIT license) pyobjc's text\n")])
        # No License-File fields, no top_level.txt, a classifier only:
        # the RECORD and the files beside METADATA have to do.
        self._dist_info(site, "oldstyle-1.0.dist-info",
                        "Name: oldstyle\nVersion: 1.0\n"
                        "Classifier: License :: OSI Approved :: ISC License (ISCL)",
                        None,
                        [("COPYING", "oldstyle's ISC text\n"), ("NOTICE", "oldstyle's notice\n")],
                        ["oldstyle.py", "oldstyle-1.0.dist-info/COPYING"])
        # Build-only tools: installed in the venv, no module in the bundle.
        self._dist_info(site, "py2app-0.28.10.dist-info",
                        "Name: py2app\nVersion: 0.28.10\nLicense: MIT or PSF License\n"
                        "License-File: LICENSE.txt",
                        ["py2app"], [("licenses/LICENSE.txt", "py2app's text\n")])
        self._dist_info(site, "pip-25.0.dist-info",
                        "Name: pip\nVersion: 25.0\nLicense-Expression: MIT\nLicense-File: LICENSE.txt",
                        None, [("licenses/LICENSE.txt", "pip's text\n")],
                        ["pip/__init__.py", "../../bin/pip"])
        # The py2app bundle: packages as directories, extensions in
        # lib-dynload, the rest in the zip.
        lib = os.path.join(bundle, "Contents", "Resources", "lib", "python3.14")
        for d in ("nacl", "objc", "AppKit", "rumps", "lib-dynload", "config-3.14-darwin", "__pycache__"):
            os.makedirs(os.path.join(lib, d))
        for rel in ("nacl/__init__.pyc", "nacl/_sodium.abi3.so", "objc/__init__.pyc",
                    "AppKit/__init__.pyc", "rumps/__init__.pyc",
                    "lib-dynload/_cffi_backend.cpython-314-darwin.so",
                    "lib-dynload/_ssl.cpython-314-darwin.so", "config-3.14-darwin/Makefile"):
            open(os.path.join(lib, rel), "w").close()
        with zipfile.ZipFile(os.path.join(lib, "site-packages.zip"), "w") as z:
            for member in ("Cocoa/__init__.pyc", "Foundation/__init__.pyc",
                           "CoreFoundation/__init__.pyc", "PyObjCTools/AppHelper.pyc",
                           "json/__init__.pyc", "email/mime/text.pyc", "oldstyle.pyc",
                           "onionpress/__init__.pyc", "menubar.pyc",
                           "_sysconfigdata__darwin_darwin.pyc"):
                z.writestr(member, b"")
        stub = os.path.join(tmp, "stub")
        os.makedirs(stub)
        with open(os.path.join(stub, "curl"), "w") as f:
            f.write(CURL_STUB)
        os.chmod(os.path.join(stub, "curl"), stat.S_IRWXU)
        return {"site": site, "stdlib": stdlib, "bundle": bundle, "stub": stub,
                "cache": os.path.join(tmp, "cache"), "log": os.path.join(tmp, "curl.log"),
                "requirements": os.path.join(ROOT, "src", "requirements.txt")}

    def _run(self, tree, env_extra=None):
        open(tree["log"], "w").close()
        env = {"PATH": tree["stub"] + ":/usr/bin:/bin", "CURL_LOG": tree["log"]}
        env.update(env_extra or {})
        r = subprocess.run([sys.executable, os.path.join(ROOT, "build", "python-notices.py"),
                            "--bundle", tree["bundle"], "--where", "Contents/Resources/MenubarApp",
                            "--requirements", tree["requirements"],
                            "--license-cache", tree["cache"],
                            "--site-packages", tree["site"], "--stdlib", tree["stdlib"],
                            "--python-version", "3.14.0"],
                           env=env, capture_output=True, timeout=120)
        with open(tree["log"]) as f:
            urls = f.read().split()
        return r, urls, r.stdout.decode("utf-8")

    # -- the harvest ---------------------------------------------------------

    def test_lists_python_and_every_bundled_distribution_with_its_texts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = self._fake_tree(tmp)
            r, urls, text = self._run(tree)
            self.assertEqual(r.returncode, 0, r.stderr.decode())
            self.assertTrue(text.startswith("=" * 70 + "\nThe menubar app: Contents/Resources/MenubarApp\n"))
            self.assertEqual(text.count("\nLicense: "), 7)
            headings = [
                "\nPython 3.14.0\nLicense: PSF-2.0\n"
                "Source: https://www.python.org/downloads/release/python-3140/\n"
                "In this bundle: Contents/Resources/MenubarApp/Contents/Frameworks/Python.framework "
                "(the interpreter) and Contents/Resources/MenubarApp/Contents/Resources/lib/python3.14 "
                "(its standard library)\n",
                "\ncffi 2.1.1\nLicense: MIT-0\nSource: https://pypi.org/project/cffi/2.1.1/\n"
                "In this bundle: Contents/Resources/MenubarApp/Contents/Resources/lib/python3.14/"
                "lib-dynload/_cffi_backend.cpython-314-darwin.so\n",
                "\noldstyle 1.0\nLicense: ISC\nSource: https://pypi.org/project/oldstyle/1.0/\n"
                "In this bundle: Contents/Resources/MenubarApp/Contents/Resources/lib/python3.14/"
                "site-packages.zip (oldstyle)\n",
                "\nPyNaCl 1.6.2\nLicense: Apache-2.0\nSource: https://pypi.org/project/PyNaCl/1.6.2/\n"
                "In this bundle: Contents/Resources/MenubarApp/Contents/Resources/lib/python3.14/nacl\n",
                "\npyobjc-core 12.2.2\nLicense: MIT\nSource: https://pypi.org/project/pyobjc-core/12.2.2/\n"
                "In this bundle: Contents/Resources/MenubarApp/Contents/Resources/lib/python3.14/objc, "
                "Contents/Resources/MenubarApp/Contents/Resources/lib/python3.14/site-packages.zip "
                "(PyObjCTools)\n",
                "\npyobjc-framework-Cocoa 12.2.2\nLicense: MIT\n"
                "Source: https://pypi.org/project/pyobjc-framework-Cocoa/12.2.2/\n"
                "In this bundle: Contents/Resources/MenubarApp/Contents/Resources/lib/python3.14/AppKit, "
                "Contents/Resources/MenubarApp/Contents/Resources/lib/python3.14/site-packages.zip "
                "(Cocoa, CoreFoundation, Foundation, PyObjCTools)\n",
                "\nrumps 0.4.0\nLicense: BSD-3-Clause\nSource: https://pypi.org/project/rumps/0.4.0/\n"
                "In this bundle: Contents/Resources/MenubarApp/Contents/Resources/lib/python3.14/rumps\n",
            ]
            positions = []
            for heading in headings:
                self.assertIn(heading, text)
                positions.append(text.index(heading))
            self.assertEqual(positions, sorted(positions), "Python first, then by name")
            # Every text, verbatim, under its own label.
            for label_and_text in (
                    "--- LICENSE.txt (Python) ---\n\nA. HISTORY OF THE SOFTWARE\n(the PSF license text)\n",
                    "--- LICENSE (PyNaCl) ---\n\nApache License\nVersion 2.0 (PyNaCl's text)\n",
                    "--- licenses/LICENSE.libsodium.txt (PyNaCl) ---\n\nISC License (libsodium's text)\n",
                    "--- LICENSE (cffi) ---\n\nMIT No Attribution (cffi's text)\n",
                    "--- LICENSE (rumps) ---\n\nCopyright (c) 2020, Jared Suttles.\n",
                    "--- LICENSE.txt (pyobjc-framework-Cocoa) ---\n\n(This is the MIT license) pyobjc's text\n",
                    "--- COPYING (oldstyle) ---\n\noldstyle's ISC text\n",
                    "--- NOTICE (oldstyle) ---\n\noldstyle's notice\n",
                    f"--- License.txt (pyobjc-core) ---\n\ntext of {PYOBJC_CORE_LICENSE_URL}\n"):
                self.assertIn(label_and_text, text)
            # pyobjc-core's text came from the tag, once, and is cached.
            self.assertEqual(urls, [PYOBJC_CORE_LICENSE_URL])
            self.assertEqual(os.listdir(tree["cache"]), ["pyobjc-core-12.2.2-License.txt"])
            # The build tools are not in the bundle and stay out.
            for absent in ("\npy2app ", "\npip ", "py2app's text", "pip's text", "not a license"):
                self.assertNotIn(absent, text)
            log = r.stderr.decode()
            self.assertIn("not in the bundle (left out): pip 25.0, py2app 0.28.10", log)
            self.assertIn("rumps 0.4.0 (BSD-3-Clause: LICENSE)", log)
            self.assertNotIn("NOTE:", log, "every bundled module is accounted for")

    def test_the_cache_serves_a_second_build_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = self._fake_tree(tmp)
            r, urls, first = self._run(tree)
            self.assertEqual(r.returncode, 0, r.stderr.decode())
            r, urls, second = self._run(tree, {"CURL_FAIL": "ALL"})
            self.assertEqual(r.returncode, 0, r.stderr.decode())
            self.assertEqual(urls, [])
            self.assertEqual(first, second)

    def test_a_distribution_without_a_text_fails_the_build(self):
        cases = {
            "declared but absent": lambda t: os.remove(
                os.path.join(t["site"], "cffi-2.1.1.dist-info", "licenses", "LICENSE")),
            "nothing to find": lambda t: [os.remove(os.path.join(
                t["site"], "oldstyle-1.0.dist-info", n)) for n in ("COPYING", "NOTICE")],
        }
        for case, break_it in cases.items():
            with self.subTest(case), tempfile.TemporaryDirectory() as tmp:
                tree = self._fake_tree(tmp)
                break_it(tree)
                r, urls, text = self._run(tree)
                self.assertNotEqual(r.returncode, 0)
                self.assertEqual(text, "", "nothing partial")
                self.assertIn("ERROR: ", r.stderr.decode())
        with self.subTest("fetch refused"), tempfile.TemporaryDirectory() as tmp:
            tree = self._fake_tree(tmp)
            r, urls, text = self._run(tree, {"CURL_FAIL": "ALL"})
            self.assertNotEqual(r.returncode, 0)
            self.assertIn(f"ERROR: could not fetch {PYOBJC_CORE_LICENSE_URL}", r.stderr.decode())
            self.assertEqual(text, "")
        with self.subTest("fetched empty"), tempfile.TemporaryDirectory() as tmp:
            tree = self._fake_tree(tmp)
            r, urls, text = self._run(tree, {"CURL_EMPTY": "1"})
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("ERROR: License.txt of pyobjc-core 12.2.2 is empty", r.stderr.decode())
            self.assertEqual(text, "")
            self.assertFalse(os.path.exists(os.path.join(tree["cache"], "pyobjc-core-12.2.2-License.txt")))

    def test_an_undeclared_license_or_missing_interpreter_text_fails(self):
        with self.subTest("no license declared"), tempfile.TemporaryDirectory() as tmp:
            tree = self._fake_tree(tmp)
            meta = os.path.join(tree["site"], "oldstyle-1.0.dist-info", "METADATA")
            with open(meta) as f:
                text = f.read()
            with open(meta, "w") as f:
                f.write(text.replace("Classifier: License :: OSI Approved :: ISC License (ISCL)\n", ""))
            r, urls, out = self._run(tree)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("ERROR: oldstyle 1.0 declares no license", r.stderr.decode())
            self.assertEqual(out, "")
        with self.subTest("no LICENSE.txt"), tempfile.TemporaryDirectory() as tmp:
            tree = self._fake_tree(tmp)
            os.remove(os.path.join(tree["stdlib"], "LICENSE.txt"))
            r, urls, out = self._run(tree)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("ERROR: no LICENSE.txt in the interpreter's standard library", r.stderr.decode())
            self.assertEqual(out, "")

    def test_a_requirement_missing_from_the_bundle_fails(self):
        # The check that decides what is bundled must find what
        # src/requirements.txt names; a bundle without rumps is wrong.
        with tempfile.TemporaryDirectory() as tmp:
            tree = self._fake_tree(tmp)
            subprocess.run(["rm", "-rf", os.path.join(
                tree["bundle"], "Contents", "Resources", "lib", "python3.14", "rumps")], check=True)
            r, urls, out = self._run(tree)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("ERROR: not found in the bundle, though requirements.txt names it: rumps",
                          r.stderr.decode())
            self.assertEqual(out, "")
        with tempfile.TemporaryDirectory() as tmp:
            tree = self._fake_tree(tmp)
            subprocess.run(["rm", "-rf", os.path.join(
                tree["bundle"], "Contents", "Resources", "lib")], check=True)
            r, urls, out = self._run(tree)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("ERROR: nothing under", r.stderr.decode())

    def test_requirements_file_is_what_the_check_reads(self):
        names = self.module.requirement_names(os.path.join(ROOT, "src", "requirements.txt"))
        self.assertEqual(names, {"rumps", "pyobjc-framework-cocoa", "pynacl"})

    def test_format_matches_the_shell_notice_section(self):
        files = [("LICENSE", b"the license\nno final newline"), ("NOTICE", b"the notice\n")]
        expected = self.module.format_section("Some Project", "v1.2", "Apache-2.0",
                                              "https://example.org/src", "somewhere/in/the/bundle", files)
        with tempfile.TemporaryDirectory() as tmp:
            for label, data in files:
                with open(os.path.join(tmp, f"proj-{label}"), "wb") as f:
                    f.write(data)
            r = subprocess.run(["bash", "-c", _shell_function(self.script, "notice_section") +
                                '\nnotice_section "Some Project" "v1.2" "Apache-2.0" '
                                '"https://example.org/src" "somewhere/in/the/bundle" proj LICENSE NOTICE'],
                               env={"PATH": "/usr/bin:/bin", "NOTICES_STAGE": tmp},
                               capture_output=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, expected)

    # -- the build script ------------------------------------------------------

    def _shell_block(self):
        s = self.script
        start = s.index("# ─── Third-party notices: the MenubarApp's Python side")
        end = s.index('echo "  $(basename "$NOTICES_FILE"): $NOTICES_COUNT projects"\n', start)
        return s[start:end] + 'echo "  $(basename "$NOTICES_FILE"): $NOTICES_COUNT projects"\n'

    def test_build_script_wiring(self):
        s = self.script
        block = self._shell_block()
        self.assertIn('"$MENUBAR_BUILD_DIR/venv/bin/python3" "$BUILD_DIR/python-notices.py"', block)
        for arg in ('--bundle "$MENUBAR_APP_DIR"', '--where "Contents/Resources/MenubarApp"',
                    '--requirements "$SCRIPTS_DIR/requirements.txt"',
                    '--license-cache "$LICENSE_CACHE_DIR"', '> "$PY_NOTICES"',
                    'cat "$PY_NOTICES" >> "$NOTICES_FILE"'):
            self.assertIn(arg, block)
        # After the MenubarApp is in place and checked, before the bundle is
        # signed and before the build venv goes.
        at = s.index(block)
        self.assertGreater(at, s.index('mv "$MENUBAR_BUILD_DIR/dist/OnionPress.app" "$MENUBAR_APP_DIR"'))
        self.assertGreater(at, s.index('echo "  Version verified: $BUILT_VERSION"'))
        self.assertGreater(at, s.index('rm -rf "$TEMP_BIN_DIR"'))
        self.assertLess(at, s.index('echo "Signing application bundle..."'))
        self.assertLess(at, s.index('rm -rf "$MENUBAR_BUILD_DIR"'))
        # The file's opening paragraph says the Python side follows.
        self.assertIn("in the menubar app under\nContents/Resources/MenubarApp, the Python interpreter "
                      "and the Python packages\nlisted after them.", s)

    def _run_block(self, tmp, tree, python_stub=VENV_PYTHON_STUB, requirements=None):
        venv_bin = os.path.join(tmp, "venv", "bin")
        os.makedirs(venv_bin, exist_ok=True)
        with open(os.path.join(venv_bin, "python3"), "w") as f:
            f.write(python_stub)
        os.chmod(os.path.join(venv_bin, "python3"), stat.S_IRWXU)
        scripts = os.path.join(tmp, "src")
        os.makedirs(scripts, exist_ok=True)
        with open(os.path.join(scripts, "requirements.txt"), "w") as f:
            f.write(requirements if requirements is not None else _read("src/requirements.txt"))
        notices = os.path.join(tmp, "THIRD-PARTY-NOTICES.txt")
        with open(notices, "w") as f:
            f.write("the binaries' part\n" + "License: X\n" * 6)
        open(tree["log"], "w").close()
        env = {"PATH": tree["stub"] + ":/usr/bin:/bin", "CURL_LOG": tree["log"],
               "REAL_PYTHON": sys.executable, "FAKE_SITE": tree["site"], "FAKE_STDLIB": tree["stdlib"],
               "MENUBAR_BUILD_DIR": tmp, "BUILD_DIR": os.path.join(ROOT, "build"),
               "MENUBAR_APP_DIR": tree["bundle"], "SCRIPTS_DIR": scripts,
               "LICENSE_CACHE_DIR": tree["cache"], "NOTICES_FILE": notices}
        r = subprocess.run(["bash", "-c", "set -e\n" + self._shell_block()], env=env,
                           capture_output=True, text=True, timeout=120)
        with open(notices) as f:
            text = f.read()
        return r, text

    def test_shell_block_appends_the_python_side_or_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = self._fake_tree(tmp)
            r, text = self._run_block(tmp, tree)
            self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
            self.assertTrue(text.startswith("the binaries' part\n" + "License: X\n" * 6 + "=" * 70))
            self.assertEqual(text.count("\nLicense: "), 6 + 7)
            self.assertIn("THIRD-PARTY-NOTICES.txt: 13 projects", r.stdout)
            self.assertIn("\nrumps 0.4.0\nLicense: BSD-3-Clause\n", text)
        # The script failing leaves the file as it was and stops the build.
        with tempfile.TemporaryDirectory() as tmp:
            tree = self._fake_tree(tmp)
            r, text = self._run_block(tmp, tree, python_stub="#!/bin/sh\necho partial\nexit 1\n")
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("ERROR: the MenubarApp's Python packages could not be added", r.stderr)
            self.assertEqual(text, "the binaries' part\n" + "License: X\n" * 6)
        # Fewer entries than the binaries, Python and requirements.txt: stop.
        with tempfile.TemporaryDirectory() as tmp:
            tree = self._fake_tree(tmp)
            r, text = self._run_block(tmp, tree, python_stub="#!/bin/sh\necho 'License: only one'\n",
                                      requirements="rumps\npyobjc-framework-Cocoa\nPyNaCl\n")
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("holds 7 entries, fewer than the 10 it must", r.stderr)


if __name__ == "__main__":
    unittest.main()

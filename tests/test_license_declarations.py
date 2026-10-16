"""OnionPress is AGPL-3.0-or-later, and says so everywhere it ships.

A license audit (2026-10-14) found every dependency compatible with the
AGPL and six gaps in OnionPress's own declarations: the AppStream metainfo
still said MIT (the first commit's license, 2026-01-31), nothing named the
copyright holder, the WordPress plugin headers said "AGPL-3.0" or nothing,
the entry scripts carried no notice, the Mac bundle shipped Colima, Lima,
the Docker CLI, Compose, mkp224o and libsodium without their license texts,
and the .deb had no copyright file. These tests keep each one closed.

The DMG build's license staging and notices writer run for real here,
with a curl stub standing in for GitHub.
"""
import os
import plistlib
import re
import stat
import subprocess
import tempfile
import unittest

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
    "linux/onionpress-heartbeat.py",
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
        self.assertIn('License: AGPL v3 or later"""', _read("src/menubar.py"))
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
        r = subprocess.run(["git", "grep", "-l", "noun-computer"], cwd=ROOT,
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
        text = open(notices).read() if os.path.exists(notices) else None
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


if __name__ == "__main__":
    unittest.main()

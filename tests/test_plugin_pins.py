#!/usr/bin/env python3
"""install_plugin.sh installs pinned plugin releases only.

It fetched https://downloads.wordpress.org/plugin/<slug>.zip, whatever
release that was that day, printed the zip's SHA256 and unpacked it into
the web root, where its PHP runs. Now each slug has one release and the
SHA256 of its zip (plugin_pin), the versioned URL is fetched (still over
Tor, from inside the WordPress container), and a zip that does not match,
or a slug with no pin, installs nothing. build/pin-wp-plugin.sh prints a
new pin; docs/BUILDING.md, "Bumping a pinned input", says how.
"""

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPT = "app/Resources/scripts/install_plugin.sh"
PLUGINS = ("cache-enabler", "internet-archive-wayback-machine-link-fixer")


def _read(rel):
    with open(os.path.join(PROJECT_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _pins(text):
    body = text[text.index("\nplugin_pin() {"):]
    body = body[:body.index("\n}\n")]
    return dict(re.findall(r'^\s+([a-z0-9-]+)\)\n\s+echo "([^"]*)" ;;$', body, re.M))


class TestPins(unittest.TestCase):

    def test_every_installed_plugin_has_a_well_formed_pin(self):
        pins = _pins(_read(SCRIPT))
        self.assertEqual(set(pins), set(PLUGINS))
        for slug, pin in pins.items():
            with self.subTest(slug):
                version, _, sha = pin.partition(" ")
                self.assertRegex(version, r"^\d+(\.\d+)+$")
                # Every shipped plugin carries its SHA256 (install_plugin.sh
                # refuses one without it); build/pin-wp-plugin.sh prints it.
                self.assertRegex(sha, r"^[0-9a-f]{64}$")

    def test_launchers_install_only_pinned_slugs(self):
        pins = _pins(_read(SCRIPT))
        for rel in ("app/MacOS/onionpress", "linux/onionpress"):
            for slug in re.findall(r'install_plugin\.sh" "([^"]+)"', _read(rel)):
                with self.subTest(f"{rel}: {slug}"):
                    self.assertIn(slug, pins)


class TestInstallPluginRuns(unittest.TestCase):
    """install_plugin.sh against a docker stub whose `curl` hands over a
    zip made here, with a copy of the script pinned to that zip's hash."""

    STUB = r'''
import json, os, shutil, sys
box, args = os.environ["BOX"], sys.argv[1:]
with open(os.environ["CALLS"], "a") as f:
    f.write(json.dumps(args) + "\n")
if args[0] == "exec":
    rest = args[2:]
    if rest == ["true"]:
        sys.exit(0)
    if rest[:2] == ["mktemp", "-d"]:
        os.makedirs(box + "/tmp/onionpress-plugin-abc123")
        print("/tmp/onionpress-plugin-abc123")
        sys.exit(0)
    if rest[0] == "curl":
        shutil.copy(os.environ["ZIP"], box + rest[rest.index("-o") + 1])
        sys.exit(0)
    if rest[:2] == ["rm", "-rf"]:
        sys.exit(0)
if args[0] == "cp":
    shutil.copy(box + args[1].split(":", 1)[1], args[2])
    sys.exit(0)
sys.exit(1)
'''

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.zip = os.path.join(self.tmp, "release.zip")
        with zipfile.ZipFile(self.zip, "w") as z:
            z.writestr("demo-plugin/demo-plugin.php", "<?php // demo\n")
        with open(self.zip, "rb") as f:
            self.sha = hashlib.sha256(f.read()).hexdigest()
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir)
        stub = os.path.join(self.tmp, "stub.py")
        with open(stub, "w") as f:
            f.write(self.STUB)
        with open(os.path.join(bin_dir, "docker"), "w") as f:
            f.write(f'#!/bin/sh\nexec "{sys.executable}" "{stub}" "$@"\n')
        os.chmod(os.path.join(bin_dir, "docker"), 0o755)
        self.calls = os.path.join(self.tmp, "calls")
        self.env = dict(os.environ, PATH=bin_dir + os.pathsep + os.environ["PATH"],
                        BOX=os.path.join(self.tmp, "box"), CALLS=self.calls, ZIP=self.zip)
        os.makedirs(self.env["BOX"])

    def _run(self, pin):
        text = _read(SCRIPT)
        body = text[text.index("\nplugin_pin() {"):]
        body = body[:body.index("\n}\n") + 3]
        pinned = text.replace(body, '\nplugin_pin() {\n    case "$1" in\n'
                              f'        demo-plugin)\n            echo "{pin}" ;;\n'
                              '        *) return 1 ;;\n    esac\n}\n')
        script = os.path.join(self.tmp, "install_plugin.sh")
        with open(script, "w") as f:
            f.write(pinned)
        dest = os.path.join(self.tmp, "dest")
        os.makedirs(dest, exist_ok=True)
        r = subprocess.run(["bash", script, "demo-plugin", dest], env=self.env,
                           capture_output=True, text=True, timeout=60)
        calls = []
        if os.path.exists(self.calls):
            import json
            with open(self.calls) as f:
                calls = [json.loads(line) for line in f]
        return r, dest, calls

    def test_a_matching_zip_is_unpacked_from_the_versioned_url(self):
        r, dest, calls = self._run(f"2.0.1 {self.sha}")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.isfile(os.path.join(dest, "demo-plugin", "demo-plugin.php")))
        [curl] = [c for c in calls if c[2:3] == ["curl"]]
        self.assertEqual(curl[-1], "https://downloads.wordpress.org/plugin/demo-plugin.2.0.1.zip")
        # Over Tor, as before.
        self.assertIn("--socks5-hostname", curl)

    def test_a_different_zip_is_refused_and_nothing_unpacked(self):
        r, dest, _ = self._run("2.0.1 " + "0" * 64)
        self.assertEqual(r.returncode, 1)
        self.assertIn("does not match its pinned SHA256", r.stdout)
        self.assertEqual(os.listdir(dest), [])

    def test_no_sha256_means_no_download(self):
        for pin in ("2.0.1 ", "2.0.1 not-a-hash"):
            with self.subTest(pin):
                r, dest, calls = self._run(pin)
                self.assertEqual(r.returncode, 1)
                self.assertIn("no SHA256 pinned", r.stdout)
                self.assertEqual(calls, [])
                self.assertEqual(os.listdir(dest), [])

    def test_an_unknown_slug_is_refused(self):
        dest = os.path.join(self.tmp, "dest")
        os.makedirs(dest)
        r = subprocess.run(["bash", os.path.join(PROJECT_ROOT, SCRIPT), "evil-plugin", dest],
                           env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 1)
        self.assertIn("not a plugin OnionPress installs", r.stdout)
        self.assertFalse(os.path.exists(self.calls))


class TestPinHelper(unittest.TestCase):
    def test_prints_the_line_install_plugin_reads(self):
        sh = _read("build/pin-wp-plugin.sh")
        self.assertIn('url="https://downloads.wordpress.org/plugin/${SLUG}.${VERSION}.zip"', sh)
        self.assertIn("printf '        %s)\\n            echo \"%s %s\" ;;\\n'", sh)
        self.assertTrue(os.access(os.path.join(PROJECT_ROOT, "build/pin-wp-plugin.sh"),
                                  os.X_OK))


if __name__ == "__main__":
    unittest.main()

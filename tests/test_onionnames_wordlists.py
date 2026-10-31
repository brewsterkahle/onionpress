#!/usr/bin/env python3
"""The onionname wordlists are found from the app bundle too (issue #507).

`onionpress setup` in the bundle runs through `bundled_python -m
onionpress.cli`: no RESOURCEPATH, not frozen, so find_wordlist_dir() saw
none of the bundle's MenubarApp/Contents/Resources/wordlists/ and the
terminal setup suggested no username. The package lives under that
Resources folder (lib/python3.x/onionpress/) and the interpreter beside
it (MacOS/python): either names the folder.
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from onionpress import onionnames_client  # noqa: E402


class TestBundleWordlists(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="op-wordlists-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, True))
        self.res = os.path.join(self.tmp, "MenubarApp", "Contents", "Resources")
        self.pkg = os.path.join(self.res, "lib", "python3.14", "onionpress")
        os.makedirs(self.pkg)
        os.makedirs(os.path.join(self.res, "wordlists"))
        with open(os.path.join(self.res, "wordlists", "en.py"), "w") as f:
            f.write("WORDS = ['a']\n")

    def test_found_from_the_packages_resources_folder(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("RESOURCEPATH", "ONIONNAME_WORDLISTS_DIR")}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(onionnames_client, "__file__",
                               os.path.join(self.pkg, "onionnames_client.py")), \
             mock.patch.object(sys, "executable", "/usr/bin/python3"):
            self.assertEqual(os.path.realpath(onionnames_client.find_wordlist_dir()),
                             os.path.realpath(os.path.join(self.res, "wordlists")))

    def test_found_beside_the_bundled_interpreter(self):
        macos = os.path.join(self.tmp, "MenubarApp", "Contents", "MacOS")
        os.makedirs(macos)
        env = {k: v for k, v in os.environ.items()
               if k not in ("RESOURCEPATH", "ONIONNAME_WORDLISTS_DIR")}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(onionnames_client, "__file__",
                               os.path.join(self.tmp, "elsewhere", "onionnames_client.py")), \
             mock.patch.object(sys, "executable", os.path.join(macos, "python")):
            self.assertEqual(os.path.realpath(onionnames_client.find_wordlist_dir()),
                             os.path.realpath(os.path.join(self.res, "wordlists")))

    def test_the_bundle_ships_them_where_the_lookup_looks(self):
        # py2app's resources list (setup.py) puts wordlists/ in Resources.
        with open(os.path.join(os.path.dirname(__file__), "..", "setup.py")) as f:
            self.assertIn("wordlists", f.read())


if __name__ == "__main__":
    unittest.main()

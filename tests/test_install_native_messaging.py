#!/usr/bin/env python3
"""install_native_messaging (src/onionpress/install_native_messaging.py):
the menubar registers the browser extension's native messaging host
manifest for Chrome, Brave and Firefox at first launch. The browser
folders are redirected into a temporary directory here."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from onionpress import install_native_messaging as inm  # noqa: E402


class TestManifests(unittest.TestCase):
    def test_chrome_without_a_store_id_allows_any_extension(self):
        m = inm._chrome_manifest("/x/host.py")
        self.assertEqual(m["name"], inm.HOST_NAME)
        self.assertEqual(m["path"], "/x/host.py")
        self.assertEqual(m["type"], "stdio")
        self.assertEqual(m["allowed_origins"], ["chrome-extension://*/"])

    def test_chrome_with_a_store_id(self):
        m = inm._chrome_manifest("/x/host.py", "abcdefghijklmnop")
        self.assertEqual(m["allowed_origins"], ["chrome-extension://abcdefghijklmnop/"])

    def test_firefox(self):
        m = inm._firefox_manifest("/x/host.py")
        self.assertEqual(m["allowed_extensions"], [f"{inm.EXTENSION_ID}@onionpress"])
        self.assertNotIn("allowed_origins", m)


class TestHostScriptPath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_inside_a_bundle(self):
        bundle = os.path.join(self.tmp, "OnionPress.app")
        pkg = os.path.join(bundle, "Contents", "Resources", "lib", "python3.14", "onionpress")
        os.makedirs(os.path.join(bundle, "Contents", "MacOS"))
        os.makedirs(pkg)
        host = os.path.join(bundle, "Contents", "Resources", "native-messaging-host.py")
        open(host, "w").close()
        with mock.patch.object(inm, "__file__", os.path.join(pkg, "install_native_messaging.py")):
            self.assertEqual(inm._find_app_bundle(), bundle)
            self.assertEqual(inm._host_script_path(), host)

    def test_a_bundle_without_the_host_falls_back_to_the_package(self):
        bundle = os.path.join(self.tmp, "OnionPress.app")
        pkg = os.path.join(bundle, "Contents", "Resources", "onionpress")
        os.makedirs(os.path.join(bundle, "Contents", "MacOS"))
        os.makedirs(pkg)
        with mock.patch.object(inm, "__file__", os.path.join(pkg, "install_native_messaging.py")):
            self.assertEqual(inm._host_script_path(),
                             os.path.join(pkg, "native_messaging_host.py"))

    def test_outside_a_bundle(self):
        with mock.patch.object(inm, "__file__", os.path.join(self.tmp, "src", "x.py")):
            self.assertIsNone(inm._find_app_bundle())


class TestInstallUninstall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.dirs = {b: os.path.join(self.tmp, b, "NativeMessagingHosts")
                     for b in ("chrome", "brave", "firefox")}
        self.host = os.path.join(self.tmp, "native-messaging-host.py")
        with open(self.host, "w") as f:
            f.write("#!/usr/bin/env python3\n")
        os.chmod(self.host, 0o600)
        self.log = []
        patches = [mock.patch.object(inm, "BROWSER_DIRS", self.dirs),
                   mock.patch.object(inm, "_host_script_path", return_value=self.host)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_install_writes_every_manifest_and_makes_the_host_executable(self):
        installed = inm.install(log_func=self.log.append)
        self.assertEqual(installed, ["chrome", "brave", "firefox"])
        self.assertTrue(os.stat(self.host).st_mode & 0o111)
        for browser, d in self.dirs.items():
            with open(os.path.join(d, f"{inm.HOST_NAME}.json")) as f:
                manifest = json.load(f)
            self.assertEqual(manifest["path"], self.host)
            if browser == "firefox":
                self.assertIn("allowed_extensions", manifest)
            else:
                self.assertEqual(manifest["allowed_origins"], ["chrome-extension://*/"])
        self.assertEqual(len(self.log), 3)

    def test_install_with_a_chrome_id(self):
        inm.install(chrome_extension_id="abc")
        with open(os.path.join(self.dirs["brave"], f"{inm.HOST_NAME}.json")) as f:
            self.assertEqual(json.load(f)["allowed_origins"], ["chrome-extension://abc/"])

    def test_a_folder_that_cannot_be_made_is_skipped_and_logged(self):
        blocker = os.path.join(self.tmp, "chrome")
        open(blocker, "w").close()  # a file where the folder must go
        installed = inm.install(log_func=self.log.append)
        self.assertEqual(installed, ["brave", "firefox"])
        self.assertTrue(any("Failed to install native messaging for chrome" in m for m in self.log))

    def test_uninstall_removes_what_install_wrote(self):
        inm.install()
        inm.uninstall(log_func=self.log.append)
        for d in self.dirs.values():
            self.assertFalse(os.path.exists(os.path.join(d, f"{inm.HOST_NAME}.json")))
        self.assertEqual(len(self.log), 3)
        inm.uninstall(log_func=self.log.append)  # nothing there: silent
        self.assertEqual(len(self.log), 3)


if __name__ == "__main__":
    unittest.main()

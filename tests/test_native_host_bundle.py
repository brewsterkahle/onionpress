#!/usr/bin/env python3
"""The browser extension's native messaging host runs from a built app
(issue #318).

The manifests install_native_messaging writes name the host by path, and the
browser runs that path with the user's environment. In a py2app build the
package's native_messaging_host.py sits in the MenubarApp's zip as a
`#!/usr/bin/env python3` script, and a Mac ships no usable python3, so the
host never started and the extension fell back to the first user's ports.
The bundle's host is app/MacOS/native-messaging-host, a wrapper that runs the
module on the MenubarApp's Python. Held here: the build copies it to the
path the manifests name, that path resolves inside a bundle laid out as the
build lays it out, and the wrapper runs the module the way the launcher's
bundled_python runs the CLI."""

import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

from onionpress import install_native_messaging as inm  # noqa: E402

WRAPPER = os.path.join(ROOT, "app", "MacOS", "native-messaging-host")
NAME = os.path.basename(WRAPPER)


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _fake_bundle(tmp, python_stub):
    """OnionPress.app as build-dmg-simple.sh lays it out: the wrapper in
    Contents/MacOS, the MenubarApp (py2app's output) under Resources with
    its Python at Contents/MacOS/python and the package in its lib/."""
    bundle = os.path.join(tmp, "Apps", "OnionPress.app")
    macos = os.path.join(bundle, *inm.BUNDLE_HOST[:-1])
    menubar = os.path.join(bundle, "Contents", "Resources", "MenubarApp")
    pkg = os.path.join(menubar, "Contents", "Resources", "lib", "python314.zip", "onionpress")
    for d in (macos, os.path.join(menubar, "Contents", "MacOS"), pkg):
        os.makedirs(d)
    shutil.copy2(WRAPPER, os.path.join(macos, NAME))
    python = os.path.join(menubar, "Contents", "MacOS", "python")
    with open(python, "w") as f:
        f.write(python_stub)
    os.chmod(python, 0o755)
    return bundle, pkg


class TestTheBuildShipsTheHost(unittest.TestCase):

    def test_the_wrapper_is_where_the_manifests_point(self):
        self.assertEqual(inm.BUNDLE_HOST, ("Contents", "MacOS", NAME))

    def test_the_wrapper_is_executable_in_git(self):
        mode = subprocess.run(["git", "ls-files", "-s", "app/MacOS/" + NAME], cwd=ROOT,
                              capture_output=True, text=True).stdout.split()[:1]
        if mode:  # a checkout without git metadata: the file mode is enough
            self.assertEqual(mode, ["100755"])
        self.assertTrue(os.access(WRAPPER, os.X_OK))

    def test_the_dmg_build_copies_it_and_makes_it_executable(self):
        build = _read("build/build-dmg-simple.sh")
        self.assertIn(f'cp "$PROJECT_DIR/app/MacOS/{NAME}" "$APP_PATH/Contents/MacOS/"', build)
        chmod = re.search(r'^chmod \+x ((?:[^\n]*\\\n)*[^\n]*)$', build, re.M)
        self.assertIsNotNone(chmod)
        self.assertIn(f'"$APP_PATH/Contents/MacOS/{NAME}"', chmod.group(1))

    def test_the_menubar_rebuild_installs_it(self):
        self.assertIn(f'install -m 755 "$PROJECT_DIR/app/MacOS/{NAME}" "$APP_PATH/Contents/MacOS/"',
                      _read("build/rebuild-menubar.sh"))

    def test_the_module_is_in_the_py2app_build(self):
        self.assertIn("'onionpress.native_messaging_host'", _read("setup.py"))

    def test_it_runs_the_launchers_bundled_python(self):
        launcher = _read("app/MacOS/onionpress")
        wrapper = _read("app/MacOS/" + NAME)
        python = re.search(r'^_BUNDLED_PYTHON_BIN="\$RESOURCES_DIR/([^"]+)"$', launcher, re.M).group(1)
        home = re.search(r'^_BUNDLED_PYTHONHOME="\$RESOURCES_DIR/([^"]+)"$', launcher, re.M).group(1)
        self.assertIn(f'exec "$RESOURCES_DIR/{python}" -m onionpress.native_messaging_host', wrapper)
        self.assertIn(f'PYTHONHOME="$RESOURCES_DIR/{home}" PYTHONSAFEPATH=1', wrapper)


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class TestTheManifestPathRunsInABundle(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.elsewhere = os.path.join(self.tmp, "cwd")
        os.makedirs(self.elsewhere)

    def _install(self, bundle, pkg):
        dirs = {b: os.path.join(self.tmp, "nmh", b) for b in ("chrome", "brave", "firefox")}
        with mock.patch.object(inm, "__file__", os.path.join(pkg, "install_native_messaging.pyc")), \
                mock.patch.object(inm, "BROWSER_DIRS", dirs):
            self.assertEqual(inm._find_app_bundle(), bundle)
            self.assertEqual(inm.install(), ["chrome", "brave", "firefox"])
        paths = set()
        for d in dirs.values():
            with open(os.path.join(d, f"{inm.HOST_NAME}.json")) as f:
                paths.add(json.load(f)["path"])
        self.assertEqual(len(paths), 1)
        return paths.pop()

    def test_the_manifests_path_is_the_bundles_wrapper(self):
        bundle, pkg = _fake_bundle(self.tmp, "#!/bin/sh\nexit 0\n")
        path = self._install(bundle, pkg)
        self.assertEqual(path, os.path.join(bundle, "Contents", "MacOS", NAME))
        self.assertTrue(os.path.isabs(path))
        self.assertTrue(os.access(path, os.X_OK))

    def test_the_wrapper_runs_the_module_on_the_bundles_python(self):
        record = os.path.join(self.tmp, "record")
        bundle, pkg = _fake_bundle(self.tmp, (
            "#!/bin/bash\n"
            f'{{ printf "%s\\n" "PYTHONHOME=$PYTHONHOME" "PYTHONSAFEPATH=$PYTHONSAFEPATH" "$@"; }} > "{record}"\n'))
        path = self._install(bundle, pkg)
        # The browser's own arguments (Chrome: the caller's origin) and cwd.
        subprocess.run([path, "chrome-extension://abc/"], cwd=self.elsewhere,
                       stdin=subprocess.DEVNULL, check=True, timeout=30)
        with open(record) as f:
            self.assertEqual(f.read().splitlines(), [
                "PYTHONHOME=" + os.path.join(bundle, "Contents", "Resources", "MenubarApp",
                                             "Contents", "Resources"),
                "PYTHONSAFEPATH=1",
                "-m", "onionpress.native_messaging_host"])

    def test_the_host_answers_the_extension_through_the_wrapper(self):
        # The stub "python" is this interpreter over the source tree, so the
        # module the wrapper names really runs and speaks the protocol.
        bundle, pkg = _fake_bundle(self.tmp, (
            "#!/bin/bash\nunset PYTHONHOME\n"
            f'PYTHONPATH="{os.path.join(ROOT, "src")}" exec "{sys.executable}" "$@"\n'))
        path = self._install(bundle, pkg)
        body = json.dumps({"type": "ping"}).encode()
        out = subprocess.run([path], cwd=self.elsewhere, input=struct.pack("@I", len(body)) + body,
                             capture_output=True, timeout=30,
                             env=dict(os.environ, HOME=self.tmp)).stdout
        (length,) = struct.unpack("@I", out[:4])
        self.assertEqual(json.loads(out[4:4 + length]), {"status": "ok"})


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""The auto-updater never writes into the installed bundle's path.

The 2.5.2 → 2.5.3 update failed on 2026-10-13 with "fcopyfile failed:
Input/output error": updater.py renamed /Applications/OnionPress.app aside
and cp'd the new bundle to that path; the running app, whose bin_dir is that
absolute path, ran a docker health probe out of the half-copied bundle; and
macOS 27's Gatekeeper answered by revoking the copy's access to the file cp
had open, then refusing every further write into the bundle. The copy now
goes to a hidden sibling folder and two renames swap it in, so the path the
app executes from is always a whole bundle.

These tests run download_and_install against fake curl/hdiutil/cp/xattr on
PATH and check where the bytes go: on the happy path, when cp fails, and
when the second rename fails.

No docker.
"""

import os
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from onionpress import updater  # noqa: E402


FAKE_CURL = """#!/bin/bash
# The updater's download: write 1.1 MB to the -o target (it refuses < 1 MB).
out=""
while [ $# -gt 0 ]; do
  if [ "$1" = "-o" ]; then out="$2"; shift; fi
  shift
done
head -c 1100000 /dev/zero > "$out"
"""

FAKE_HDIUTIL = """#!/bin/bash
# attach <dmg> -mountpoint <dir> ...: put the fixture bundle at the mount
# point. detach: nothing.
if [ "$1" = "attach" ]; then
  mp=""
  while [ $# -gt 0 ]; do
    if [ "$1" = "-mountpoint" ]; then mp="$2"; shift; fi
    shift
  done
  /bin/cp -R "$FAKE_DMG_APP" "$mp/OnionPress.app"
fi
exit 0
"""

FAKE_CP = """#!/bin/bash
# Record the copy's destination and whether the installed bundle was still
# whole at that moment, then do the copy (or fail, leaving a partial tree).
dst="${@: -1}"
if [ -f "$INSTALL_PATH/Contents/Resources/old-marker" ]; then whole=yes; else whole=no; fi
printf '%s\\t%s\\n' "$dst" "$whole" >> "$CP_LOG"
if [ -n "$FAKE_CP_FAIL" ]; then
  mkdir -p "$dst/Contents/Resources"
  echo partial > "$dst/Contents/Resources/partial"
  echo "cp: $dst/Contents/Resources/x: fcopyfile failed: Input/output error" >&2
  exit 1
fi
exec /bin/cp "$@"
"""

FAKE_XATTR = """#!/bin/bash
printf '%s\\n' "${@: -1}" >> "$XATTR_LOG"
"""


class StagedSwapTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="updater-test-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.apps = os.path.join(self.root, "Applications")
        self.install_path = os.path.join(self.apps, "OnionPress.app")
        os.makedirs(os.path.join(self.install_path, "Contents", "Resources"))
        with open(os.path.join(self.install_path, "Contents", "Resources",
                               "old-marker"), "w") as f:
            f.write("old\n")

        self.dmg_app = os.path.join(self.root, "dmg", "OnionPress.app")
        os.makedirs(os.path.join(self.dmg_app, "Contents", "Resources", "bin"))
        with open(os.path.join(self.dmg_app, "Contents", "Resources", "bin",
                               "docker"), "w") as f:
            f.write("new\n")

        fake_bin = os.path.join(self.root, "bin")
        os.makedirs(fake_bin)
        for name, body in (("curl", FAKE_CURL), ("hdiutil", FAKE_HDIUTIL),
                           ("cp", FAKE_CP), ("xattr", FAKE_XATTR)):
            path = os.path.join(fake_bin, name)
            with open(path, "w") as f:
                f.write(body)
            os.chmod(path, stat.S_IRWXU)

        tmp = os.path.join(self.root, "tmp")
        os.makedirs(tmp)
        self.cp_log = os.path.join(self.root, "cp.log")
        self.xattr_log = os.path.join(self.root, "xattr.log")
        env = {
            "PATH": fake_bin + ":/usr/bin:/bin",
            "TMPDIR": tmp,
            "FAKE_DMG_APP": self.dmg_app,
            "INSTALL_PATH": self.install_path,
            "CP_LOG": self.cp_log,
            "XATTR_LOG": self.xattr_log,
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        # tempfile caches its choice of temp dir; the download must land in
        # ours so a failed run leaves nothing behind.
        self._old_tempdir = tempfile.tempdir
        tempfile.tempdir = tmp
        self.addCleanup(setattr, tempfile, "tempdir", self._old_tempdir)

        self.release = {"assets": [{"name": "onionpress.dmg",
                                    "browser_download_url": "https://x/y.dmg"}]}
        self.logged = []

    def _install(self):
        return updater.download_and_install(
            self.release, "9.9.9", self.install_path, log=self.logged.append)

    def _cp_calls(self):
        with open(self.cp_log) as f:
            return [line.rstrip("\n").split("\t") for line in f]

    def _apps_entries(self):
        return sorted(os.listdir(self.apps))

    def test_copy_goes_to_a_hidden_sibling_and_is_swapped_in(self):
        self.assertEqual(self._install(), "9.9.9")

        (dst, whole), = self._cp_calls()
        self.assertNotEqual(dst, self.install_path)
        self.assertFalse(dst.startswith(self.install_path + os.sep),
                         "copied into the installed bundle's path")
        staging = os.path.dirname(dst)
        self.assertEqual(os.path.dirname(staging), self.apps,
                         "staging folder is not beside the installed bundle")
        self.assertTrue(os.path.basename(staging).startswith(
            updater.STAGING_PREFIX))
        self.assertEqual(whole, "yes",
                         "the installed bundle was renamed away before the copy")

        with open(self.xattr_log) as f:
            self.assertEqual(f.read().split(), [dst],
                             "quarantine stripped somewhere but the staged copy")

        with open(os.path.join(self.install_path, "Contents", "Resources",
                               "bin", "docker")) as f:
            self.assertEqual(f.read(), "new\n")
        self.assertFalse(os.path.exists(os.path.join(
            self.install_path, "Contents", "Resources", "old-marker")))
        self.assertEqual(self._apps_entries(), ["OnionPress.app"])
        self.assertEqual(os.listdir(os.path.join(self.root, "tmp")), [],
                         "download or mount point left behind")

    def test_stale_backup_and_staging_folders_are_cleared(self):
        for stale in ("OnionPress.app.bak-123", updater.STAGING_PREFIX + "old"):
            os.makedirs(os.path.join(self.apps, stale, "Contents"))
        self._install()
        self.assertEqual(self._apps_entries(), ["OnionPress.app"])

    def test_failed_copy_leaves_the_installed_bundle_alone(self):
        os.environ["FAKE_CP_FAIL"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_CP_FAIL", None)
        before = os.stat(self.install_path).st_ino

        with self.assertRaisesRegex(RuntimeError, "Copy failed.*Input/output"):
            self._install()

        self.assertEqual(os.stat(self.install_path).st_ino, before)
        with open(os.path.join(self.install_path, "Contents", "Resources",
                               "old-marker")) as f:
            self.assertEqual(f.read(), "old\n")
        self.assertEqual(self._apps_entries(), ["OnionPress.app"],
                         "partial copy or backup left beside the bundle")
        self.assertEqual(os.listdir(os.path.join(self.root, "tmp")), [])

    def test_failed_swap_puts_the_old_bundle_back(self):
        real_rename = os.rename
        calls = []

        def rename(src, dst, *args, **kwargs):
            calls.append((src, dst))
            if len(calls) == 2:
                raise OSError(1, "Operation not permitted", dst)
            return real_rename(src, dst, *args, **kwargs)

        with mock.patch.object(updater.os, "rename", rename):
            with self.assertRaises(OSError):
                self._install()

        self.assertEqual(calls[0][0], self.install_path)
        self.assertEqual(calls[1][1], self.install_path)
        with open(os.path.join(self.install_path, "Contents", "Resources",
                               "old-marker")) as f:
            self.assertEqual(f.read(), "old\n")
        self.assertEqual(self._apps_entries(), ["OnionPress.app"])
        self.assertIn("Auto-update: swap failed, restoring backup", self.logged)


if __name__ == "__main__":
    unittest.main()

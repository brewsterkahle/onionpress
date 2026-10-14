#!/usr/bin/env python3
"""Build and CI properties that broke silently (issue #326).

1. The .deb's files are root's. dpkg-deb records the staging tree's real
   owner unless told otherwise, so the 2.5.3 .deb listed every file as the
   builder's user and group. Both paths — dpkg-deb, and the manual ar/tar
   assembly used where dpkg-deb is missing (macOS) — must say root.

2. A release's Mac app is universal. Without python.org's universal2 Python,
   build-dmg-simple.sh falls back to uv's single-arch interpreter, and 2.5.2
   and 2.5.3 shipped an Apple-Silicon-only MenubarApp because nothing
   noticed. release.sh now makes the build refuse that fallback and scans
   the built bundle with lipo.

3. The x86_64 libsodium that mkp224o links is built with an optimisation
   level: its configure, like mkp224o's, picks none once CFLAGS is given.

4. CI runs on a pinned Ubuntu, not ubuntu-latest (Ubuntu 26 from
   2026-10-19), and on action majors that run on Node 24.

Static checks: the scripts need macOS or a release to run for real.
"""

import glob
import os
import re
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

LINUX_BUILD = "build/build-linux.sh"
DMG_BUILD = "build/build-dmg-simple.sh"
RELEASE = "build/release.sh"

# The first major of each action this repo uses whose action.yml says
# `using: node24` (checked against each repository's action.yml).
NODE24_MAJOR = {
    "actions/checkout": 5,
    "actions/setup-python": 6,
    "docker/login-action": 4,
    "docker/setup-buildx-action": 4,
    "docker/build-push-action": 7,
    "crazy-max/ghaction-github-runtime": 4,
    "actions/upload-artifact": 6,
    "actions/download-artifact": 7,
    # A composite action: v4 runs actions/attest v4 (node24), v3 and
    # earlier node20 versions of it.
    "actions/attest-build-provenance": 4,
}


def _read(rel_path):
    with open(os.path.join(PROJECT_ROOT, rel_path), "r", encoding="utf-8") as f:
        return f.read()


def _code(text):
    """Shell or YAML text without comment lines, continuations joined."""
    lines = [l for l in text.splitlines() if not l.lstrip().startswith("#")]
    return "\n".join(lines).replace("\\\n", " ")


class DebIsRootOwned(unittest.TestCase):
    def setUp(self):
        self.code = _code(_read(LINUX_BUILD))

    def test_dpkg_deb_uses_root_owner_group(self):
        calls = [l for l in self.code.splitlines()
                 if re.search(r"\bdpkg-deb\b.*--build", l)]
        self.assertTrue(calls, f"{LINUX_BUILD}: no dpkg-deb --build call found")
        for line in calls:
            self.assertIn("--root-owner-group", line,
                          "dpkg-deb without --root-owner-group records the "
                          "builder's uid/gid on every file in the package.")

    def test_manual_assembly_tars_as_root(self):
        tars = [l for l in self.code.splitlines()
                if re.search(r"\btar\b[^|]*-c\w*f\b", l)]
        self.assertEqual(len(tars), 2,
                         f"{LINUX_BUILD}: expected the control and data tar "
                         f"calls, found {tars}")
        for line in tars:
            self.assertIn('"${ROOT_OWNED[@]}"', line)
        self.assertIn("--owner=root:0 --group=root:0", self.code)  # GNU tar
        self.assertIn("--uid 0 --gid 0 --uname root --gname root", self.code)  # bsdtar


class ReleaseAppIsUniversal(unittest.TestCase):
    def test_release_builds_the_dmg_as_a_release_build(self):
        calls = [l for l in _code(_read(RELEASE)).splitlines()
                 if "build-dmg-simple.sh" in l]
        self.assertTrue(calls)
        for line in calls:
            self.assertIn("ONIONPRESS_RELEASE_BUILD=1", line)

    def test_release_scans_the_bundle_with_lipo_before_uploading(self):
        code = _code(_read(RELEASE))
        scan = code.find("lipo -archs")
        upload = min(i for i in (code.find("gh release upload \"$TAG\" \"$DMG_PATH\""),
                                 code.find("gh release create")) if i >= 0)
        self.assertGreater(scan, 0, f"{RELEASE}: no lipo -archs check")
        self.assertLess(scan, upload,
                        "The universal check must run before anything is uploaded.")
        self.assertIn("MenubarApp/Contents/MacOS/python", code)
        self.assertRegex(code, r'\*" arm64 "\*.*\*" x86_64 "\*')

    def test_dmg_build_refuses_uv_python_for_a_release(self):
        code = _code(_read(DMG_BUILD))
        guard = code.find('"${ONIONPRESS_RELEASE_BUILD:-}" = "1"')
        uv = code.find("command -v uv")
        self.assertGreater(guard, 0, f"{DMG_BUILD}: no ONIONPRESS_RELEASE_BUILD guard")
        self.assertLess(guard, uv,
                        "The release guard must come before the uv fallback.")
        self.assertIn("exit 1", code[guard:uv])


class IntelLibsodiumIsOptimised(unittest.TestCase):
    def test_libsodium_cross_build_passes_an_optimisation_level(self):
        lines = [l for l in _code(_read(DMG_BUILD)).splitlines()
                 if "./configure" in l and "--prefix=" in l]
        self.assertTrue(lines, f"{DMG_BUILD}: libsodium configure call not found")
        for line in lines:
            cflags = re.search(r'CFLAGS="([^"]*)"', line)
            self.assertIsNotNone(cflags)
            self.assertRegex(cflags.group(1), r"(^|\s)-O[123s]\b",
                             "libsodium's configure adds no -O once CFLAGS is set.")


class WorkflowRunnersAndActions(unittest.TestCase):
    def setUp(self):
        self.workflows = {
            os.path.relpath(p, PROJECT_ROOT): _code(open(p, encoding="utf-8").read())
            for p in glob.glob(os.path.join(PROJECT_ROOT, ".github/workflows/*.yml"))
        }
        self.assertTrue(self.workflows)

    def test_no_job_runs_on_a_moving_runner_label(self):
        for name, code in self.workflows.items():
            for runner in re.findall(r"runs-on:[ \t]*(\S+)", code):
                with self.subTest(workflow=name, runner=runner):
                    self.assertNotIn("latest", runner,
                                     "Pin the Ubuntu version; -latest moved to "
                                     "Ubuntu 26 on 2026-10-19 without a review.")

    def test_actions_run_on_node_24(self):
        for name, code in self.workflows.items():
            for action, major in re.findall(r"uses:[ \t]*([\w.-]+/[\w.-]+)@v(\d+)", code):
                with self.subTest(workflow=name, action=action):
                    self.assertIn(action, NODE24_MAJOR,
                                  "New action: add its first node24 major here.")
                    self.assertGreaterEqual(int(major), NODE24_MAJOR[action],
                                            f"{action}@v{major} runs on Node 20.")


if __name__ == "__main__":
    unittest.main()

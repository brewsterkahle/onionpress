#!/usr/bin/env python3
"""The .deb carries Docker's rootless scripts for a docker.io that ships none
(issue #486).

Ubuntu 24.04's docker.io 29.1.3 installs no dockerd-rootless-setuptool.sh
and no dockerd-rootless.sh (its contrib holds check-config.sh and
nuke-graph-directory.sh), so the postinst's first install fell back to the
docker group without a word. linux/docker-rootless/ now holds Docker's own
two scripts, unchanged, which the package installs to
/opt/onionpress/docker-rootless/ as the last place the postinst looks.

Pinned here by sha256 (linux/docker-rootless/README.md names the commit):
a refresh changes the pins on purpose; nothing else does. The scripts are
Apache-2.0, declared in linux/copyright. Nothing here runs them.
"""
import hashlib
import os
import re
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
FOLDER = "linux/docker-rootless"
UPSTREAM_COMMIT = "27f3e40d39730e2e29dbcdaee3bd3ddcd3516c60"
PINS = {
    "dockerd-rootless.sh":
        "200203633806081a401e60aefdf68a8fa73fc7dc80aa854c52a69d47710a3488",
    "dockerd-rootless-setuptool.sh":
        "1c9f0dc93ebb3d75255254ec760d26a912affba7f329ab8abffe8e25eb0b3f94",
}
PACKAGED_DIR = "/opt/onionpress/docker-rootless"


def _read(rel, mode="r"):
    with open(os.path.join(ROOT, rel), mode, **({} if "b" in mode else {"encoding": "utf-8"})) as f:
        return f.read()


class TestTheScripts(unittest.TestCase):

    def test_bytes_are_the_pinned_upstream_ones(self):
        for name, digest in PINS.items():
            with self.subTest(name):
                self.assertEqual(hashlib.sha256(_read(f"{FOLDER}/{name}", "rb")).hexdigest(),
                                 digest, f"{FOLDER}/{name} changed; see its README before re-pinning")

    def test_nothing_else_is_in_the_folder(self):
        self.assertEqual(sorted(os.listdir(os.path.join(ROOT, FOLDER))),
                         sorted([*PINS, "README.md"]))

    def test_they_are_dockers_sh_scripts(self):
        for name in PINS:
            with self.subTest(name):
                text = _read(f"{FOLDER}/{name}")
                self.assertTrue(text.startswith("#!/bin/sh\n"), name)
                self.assertIn(name, text.splitlines()[1], "Docker's own header names the script")
        self.assertIn("rootlesskit", _read(f"{FOLDER}/dockerd-rootless.sh"))
        self.assertIn('DOCKERD_ROOTLESS_SH="dockerd-rootless.sh"',
                      _read(f"{FOLDER}/dockerd-rootless-setuptool.sh"))

    def test_the_readme_names_the_commit_and_the_hashes(self):
        text = _read(f"{FOLDER}/README.md")
        self.assertIn(UPSTREAM_COMMIT, text)
        for name, digest in PINS.items():
            with self.subTest(name):
                self.assertIn(digest, text)
        self.assertIn("Apache-2.0", text)


class TestTheyShip(unittest.TestCase):

    def test_install_tree_lists_them_executable(self):
        manifest = _read("linux/install-tree.sh")
        for name in PINS:
            with self.subTest(name):
                self.assertRegex(
                    manifest,
                    re.compile(rf"^file\s+{re.escape(FOLDER)}/{re.escape(name)}\s+"
                               rf"docker-rootless/{re.escape(name)}\s+0755\s*$", re.MULTILINE),
                    f"linux/install-tree.sh: {name} as docker-rootless/{name}, mode 0755")

    def test_check_deb_requires_them(self):
        check = _read("build/check-deb.sh")
        for name in PINS:
            with self.subTest(name):
                self.assertIn(f"    .{PACKAGED_DIR}/{name}\n", check)

    def test_the_postinst_looks_there_last(self):
        build = _read("build/build-linux.sh")
        self.assertIn(f'PACKAGED_ROOTLESS_DIR="${{ONIONPRESS_DOCKER_ROOTLESS_DIR:-{PACKAGED_DIR}}}"', build)
        fn = re.search(r"rootless_setuptool_dir\(\) \{\n(.*?)\n\}", build, re.DOTALL)
        self.assertIsNotNone(fn)
        body = fn.group(1)
        self.assertLess(body.index("command -v dockerd-rootless-setuptool.sh"),
                        body.index('"$DOCKER_IO_CONTRIB" "$PACKAGED_ROOTLESS_DIR"'),
                        "PATH, then docker.io's contrib, then the package's copy")

    def test_depends_has_what_the_wrapper_runs(self):
        m = re.search(r"^Depends: (.*)$", _read("build/build-linux.sh"), re.MULTILINE)
        self.assertIsNotNone(m)
        depends = [d.strip() for d in m.group(1).split(",")]
        for need in ("rootlesskit | docker-ce-rootless-extras", "slirp4netns", "uidmap", "procps"):
            with self.subTest(need):
                self.assertIn(need, depends)


class TestTheLicenseIsDeclared(unittest.TestCase):

    def test_copyright_file_has_an_apache_stanza_for_the_folder(self):
        text = _read("linux/copyright")
        self.assertIn(f"\nFiles: {FOLDER}/*\nCopyright: 2013-2026 Docker, Inc.\nLicense: Apache-2.0\n", text)
        self.assertIn("\nLicense: Apache-2.0\n Licensed under the Apache License, Version 2.0", text)
        self.assertIn('"/usr/share/common-licenses/Apache-2.0"', text)
        self.assertLess(text.index("\nFiles: *\n"), text.index(f"\nFiles: {FOLDER}/*\n"),
                        "DEP-5: the later, more specific stanza wins")
        self.assertNotIn("bundles\n nothing from another project", text)


if __name__ == "__main__":
    unittest.main()

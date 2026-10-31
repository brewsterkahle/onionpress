"""The third-party images are pinned to our own ghcr.io copies (issue #501).

build/image-mirrors.txt is the one table: which pin, which upstream, which
package. build/refresh-image-digests.sh writes the mirror's name into the
pins, .github/workflows/mirror-images.yml makes the copy by digest and reads
it back, and the release adds a :<version> tag. These tests hold the three
together; tests/test_image_pins.py holds the consumers to the pins.
"""
import os
import re
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAMESPACE = "ghcr.io/brewsterkahle"


def _read(rel):
    with open(os.path.join(PROJECT_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _table():
    rows = {}
    for line in _read("build/image-mirrors.txt").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            key, upstream, package = line.split()
            rows[key] = (upstream, package)
    return rows


def _pins():
    return dict(line.split("=", 1) for line in _read("build/image-pins.env").splitlines()
                if "=" in line and not line.startswith("#"))


class TestMirrorTable(unittest.TestCase):
    def test_covers_exactly_the_refresh_scripts_third_party_keys(self):
        script = _read("build/refresh-image-digests.sh")
        keys = re.search(r"^THIRD_PARTY_KEYS=\((.*?)\)$", script, re.M).group(1).split()
        self.assertEqual(sorted(_table()), sorted(keys))

    def test_every_third_party_pin_names_its_mirror_by_digest(self):
        pins = _pins()
        for key, (_, package) in _table().items():
            with self.subTest(key=key):
                self.assertRegex(
                    pins[key],
                    r"^" + re.escape(f"{NAMESPACE}/{package}") + r":[\w][\w.-]*@sha256:[0-9a-f]{64}$",
                    f"{key} must pin our mirror, not the upstream: a publisher "
                    "deleting an old digest would stop a first run (issue #501).")

    def test_packages_are_distinct_and_never_the_projects_own(self):
        packages = [p for _, p in _table().values()]
        self.assertEqual(len(set(packages)), len(packages))
        for p in packages:
            self.assertNotIn(p, ("onionpress-tor", "onionpress-wordpress",
                                 "onionpress-stress-worker"))


class TestMirrorWorkflow(unittest.TestCase):
    def setUp(self):
        self.text = _read(".github/workflows/mirror-images.yml")

    def test_runs_when_the_pins_change(self):
        self.assertRegex(self.text, r"(?s)push:\s+paths:.*?- build/image-pins\.env")

    def test_copies_by_digest_from_the_upstream_and_reads_it_back(self):
        self.assertIn('imagetools create -t "$mirror:$tag" "$upstream@$digest"', self.text)
        self.assertIn('imagetools inspect "$mirror:$tag"', self.text)
        self.assertIn('if [ "$got" != "$digest" ]', self.text)

    def test_reads_the_same_table_and_pins(self):
        self.assertIn("build/image-mirrors.txt", self.text)
        self.assertIn("build/image-pins.env", self.text)

    def test_write_permission_only_on_packages(self):
        self.assertRegex(self.text, r"permissions:\s+contents: read\s+packages: write")
        self.assertNotIn("secrets.", self.text.replace("secrets.GITHUB_TOKEN", ""))

    def test_actions_are_pinned_to_a_commit(self):
        for m in re.finditer(r"(?m)^\s*- uses: (\S+)(.*)$", self.text):
            with self.subTest(action=m.group(1)):
                self.assertRegex(m.group(1), r"@[0-9a-f]{40}$")
                self.assertRegex(m.group(2), r"# v\d")

    def test_release_tags_the_mirrors_from_the_same_table(self):
        text = _read(".github/workflows/docker-publish.yml")
        self.assertIn("build/image-mirrors.txt", text)


if __name__ == "__main__":
    unittest.main()

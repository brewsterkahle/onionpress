"""Images change only with a release (decided reviewing #499).

With every image pinned by digest, the opt-in refresh could fetch nothing
newer than the pins: UPDATE_ON_LAUNCH ("Update Docker on Launch", the
launchers' update_images), `onionpress update-images` and the image pass
of the menubar's Check for Updates re-pulled the same digests, and the
last one told the owner "All container images are up to date", as if
images updated apart from OnionPress. All three are gone: a start pulls
what is missing (the pins a new release moved, start.start_core) and
removes the images those pins superseded (start.prune_superseded_images,
tests/test_start_post_up.py). Issue #36 was the refresh's cost; #501
is what a first run does when a pin is gone from its registry.

An old config's UPDATE_ON_LAUNCH line is ignored; `config set` refuses
the key like any other unknown one.
"""

import os
import re
import subprocess
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(PROJECT_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _shipped_files():
    out = subprocess.run(["git", "ls-files", "src", "app", "linux", "build"],
                         cwd=PROJECT_ROOT, capture_output=True, text=True)
    if out.returncode != 0:
        raise unittest.SkipTest("not a git checkout")
    return [p for p in out.stdout.splitlines()
            if os.path.isfile(os.path.join(PROJECT_ROOT, p))]


class TestNoImageRefresh(unittest.TestCase):

    def test_nothing_shipped_names_it(self):
        found = []
        for path in _shipped_files():
            try:
                text = _read(path)
            except UnicodeDecodeError:
                continue
            for word in ("UPDATE_ON_LAUNCH", "update_images", "update-images",
                         "update_docker_images", "updateOnLaunch"):
                if word in text:
                    found.append(f"{path}: {word}")
        self.assertEqual(found, [])

    def test_the_cli_has_no_update_images(self):
        import sys
        sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))
        from onionpress import config
        self.assertNotIn("UPDATE_ON_LAUNCH", config.CLI_CONFIG_KEYS)
        self.assertNotIn("UPDATE_ON_LAUNCH", config.DEFAULTS)
        self.assertNotIn('("update-images"', _read("src/onionpress/cli.py"))

    def test_check_for_updates_checks_the_release_only(self):
        menubar = _read("src/menubar.py")
        body = menubar[menubar.index("    def check_for_updates(self, _):"):]
        body = body[:body.index("\n    def ", 10)]
        self.assertIn('run_json("update", "--check", "--json"', body)
        self.assertNotRegex(body, r"(?i)container images|docker")

    def test_the_start_pulls_what_is_missing_only(self):
        start = _read("src/onionpress/start.py")
        self.assertEqual(re.findall(r'\["pull"[^\]]*\]', start),
                         ['["pull", "--policy", "missing"]'])


if __name__ == "__main__":
    unittest.main()

"""Issue #36: with UPDATE_ON_LAUNCH=yes a Mac launch refreshes the images
once, in the launcher, not twice.

The menubar ran `onionpress update-images` at launch when the config said
yes, and then the launcher's `start` ran its own update_images() pass:
two serialized registry contacts before any container started. The
menubar's pass is gone; the launcher's is the one, and the plain start
still pulls only what is missing (`--policy missing`).
"""

import os
import re
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(PROJECT_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _code(text):
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


class TestOnePullPass(unittest.TestCase):

    def test_the_menubar_reads_update_on_launch_nowhere(self):
        code = _code(_read("src/menubar.py"))
        self.assertNotIn("UPDATE_ON_LAUNCH", code)
        # update_docker_images is the Check for Updates... flow's only.
        calls = [m.start() for m in re.finditer(r"self\.update_docker_images\(", code)]
        self.assertEqual(len(calls), 1, "one caller: _check_docker_updates_async")
        i = code.index("def _check_docker_updates_async")
        self.assertLess(i, calls[0])
        self.assertLess(calls[0], code.index("def ", i + 10))

    def test_the_launcher_refreshes_once_per_start(self):
        code = _code(_read("app/MacOS/onionpress"))
        start = code.index("start_containers() {")
        body = code[start:code.index("\n}\n", start)]
        self.assertEqual(len(re.findall(r"(?m)^\s+update_images$", body)), 1)
        # The missing-only pull is start-core's (start.start_core, #442.6).
        self.assertLess(body.index("update_images"), body.index("start-containers"))
        fn = code[code.index("update_images() {"):]
        fn = fn[:fn.index("\n}\n")]
        self.assertIn("$(config_value UPDATE_ON_LAUNCH)", fn)
        self.assertIn('if [ "$update_enabled" != "yes" ]; then', fn)


if __name__ == "__main__":
    unittest.main()

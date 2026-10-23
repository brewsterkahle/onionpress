#!/usr/bin/env python3
"""The unit tests' coverage gate (`make coverage`, CI's coverage steps):
.coveragerc measures what ships, build/coverage-floors.sh holds the total
and the per-file floors, and CI runs the same three steps as the Makefile."""

import os
import re
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _read(rel):
    with open(os.path.join(PROJECT_ROOT, rel), encoding="utf-8") as f:
        return f.read()


class TestCoverageConfig(unittest.TestCase):

    def test_coveragerc_measures_what_ships_with_branches(self):
        rc = _read(".coveragerc")
        self.assertIn("branch = True", rc)
        for src in ("src/onionpress", "linux", "app/Resources/docker/tor"):
            self.assertRegex(rc, rf"(?m)^\s+{re.escape(src)}\s*$")
        for omitted in ("tests/*", "tests/stress/*", "setup.py"):
            self.assertRegex(rc, rf"(?m)^\s+{re.escape(omitted)}\s*$")

    def test_floors(self):
        script = _read("build/coverage-floors.sh")
        self.assertIn("TOTAL_FLOOR=60", script)
        floors = dict(re.findall(r"(?m)^(src/onionpress/\S+\.py) (\d+)$", script))
        self.assertEqual(floors, {"src/onionpress/cli.py": "60",
                                  "src/onionpress/handle_action.py": "85",
                                  "src/onionpress/confirm.py": "90",
                                  "src/onionpress/config.py": "85"})
        for path in floors:
            self.assertTrue(os.path.exists(os.path.join(PROJECT_ROOT, path)), path)
        # macOS /bin/bash is 3.2: no associative arrays.
        self.assertNotIn("declare -A", script)

    def test_make_coverage_runs_the_three_steps(self):
        make = _read("Makefile")
        target = make[make.index("\ncoverage:"):make.index("\n# ─── Housekeeping")]
        self.assertIn("coverage run -m unittest discover tests -p 'test_*.py'", target)
        self.assertIn("coverage report --skip-empty", target)
        self.assertIn("./build/coverage-floors.sh", target)
        self.assertIn("--with 'pynacl>=1.5.0'", target)


class TestWorkflow(unittest.TestCase):

    def setUp(self):
        self.text = _read(".github/workflows/test.yml")

    def test_linux_job_measures_and_holds_the_floors(self):
        self.assertIn('pip install "pynacl>=1.5.0" coverage', self.text)
        run = self.text.index("coverage run --branch")
        self.assertIn("--source=src/onionpress,linux,app/Resources/docker/tor", self.text)
        self.assertLess(run, self.text.index("coverage report --skip-empty"))
        self.assertLess(self.text.index("coverage report --skip-empty"),
                        self.text.index("./build/coverage-floors.sh"))
        self.assertIn("python -m coverage xml", self.text)
        self.assertIn("python -m coverage html", self.text)
        self.assertIn("actions/upload-artifact@", self.text)
        # The PyNaCl 1.5.0 pass stays.
        self.assertIn('pip install "pynacl==1.5.0"', self.text)


if __name__ == "__main__":
    unittest.main()

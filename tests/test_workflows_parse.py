"""Every GitHub Actions workflow file must parse as YAML.

An unquoted `: ` in a one-line `run:` made .github/workflows/test.yml
invalid on 2026-10-23: GitHub reported "workflow file issue" and ran
no job, so 26 commits landed on main untested. The other workflow
tests read the files as text and never parsed them; this one does.
PyYAML comes with `make test-unit` (uv `--with pyyaml`) and the CI
jobs' pip installs; without it the test skips.
"""
import glob
import os
import unittest

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS = sorted(glob.glob(os.path.join(REPO, ".github", "workflows", "*.yml")))


@unittest.skipUnless(yaml, "PyYAML not installed")
class TestWorkflowsParse(unittest.TestCase):

    def test_there_are_workflows(self):
        self.assertTrue(WORKFLOWS)

    def test_every_workflow_parses_and_has_jobs(self):
        for path in WORKFLOWS:
            with self.subTest(workflow=os.path.basename(path)):
                with open(path, encoding="utf-8") as f:
                    doc = yaml.safe_load(f)
                self.assertIsInstance(doc, dict, path)
                # PyYAML reads the bare key `on` as the boolean True.
                self.assertTrue("on" in doc or True in doc, f"{path}: no `on:`")
                self.assertIsInstance(doc.get("jobs"), dict, f"{path}: no jobs")
                for name, job in doc["jobs"].items():
                    self.assertTrue(
                        "runs-on" in job or "uses" in job,
                        f"{path}: job {name} has no runs-on",
                    )

    def test_no_run_line_with_a_bare_colon_space(self):
        # The shape that broke test.yml: a one-line `run:` whose command
        # holds `: ` (a mapping separator to YAML). Block scalars
        # (`run: |`) and quoted strings are fine.
        for path in WORKFLOWS:
            with open(path, encoding="utf-8") as f:
                for lineno, line in enumerate(f, 1):
                    stripped = line.strip()
                    if not stripped.startswith("run: ") or stripped in ("run: |", "run: >"):
                        continue
                    body = stripped[len("run: "):]
                    if body[:1] in ("'", '"', "|", ">"):
                        continue
                    self.assertNotIn(
                        ": ", body,
                        f"{os.path.basename(path)}:{lineno}: one-line run with ': ' — use `run: |`",
                    )

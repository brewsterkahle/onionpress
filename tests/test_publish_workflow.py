#!/usr/bin/env python3
"""Static invariants for .github/workflows/docker-publish.yml.

The workflow publishes the container images every install pulls, and it only
runs on a release or a manual dispatch, so a property broken today is found
on release day. Three of them matter enough to pin here:

1. It runs anywhere. Every job is on a GitHub-hosted runner, and the image
   namespace is the owner of the repository the run belongs to, so a fork
   runs it unchanged and publishes under its own account. Until September
   2026 the arm64 half ran on the maintainer's own Mac as a self-hosted
   runner: every image release depended on that one machine being online,
   and nobody else could produce the arm64 images at all.

2. Its images are named through two variables. IMAGE_NAMESPACE (the GHCR
   account) and IMAGE_PREFIX ("dev-" on the development branch, "" on main)
   are each declared once and every image reference goes through both, so
   changing one line really changes every image. The prefix keeps a run
   from the development branch from ever overwriting the onionpress-*
   images shipped installs pull. A plain `git merge development` into main
   would carry the prefix along and make the next release publish dev
   images while the app kept pulling the old digests — so when CI runs for
   main the prefix has to be empty, and when it runs for development it
   has to be "dev-".

3. The stress worker extends the tor image this run built. Its Dockerfile
   takes the base as ARG TOR_IMAGE, defaulting to the published image; a
   run that does not pass it builds the worker on somebody else's tor
   image, which on the development branch or in a fork is a different
   build entirely.

No PyYAML: CI installs nothing beyond CPython, so the workflow is read with
regexes, like tests/test_image_pins.py does for docker-compose.yml.
"""

import os
import re
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

WORKFLOW = ".github/workflows/docker-publish.yml"
IMAGES = ("stress-worker", "tor", "wordpress")
ARCHES = ("amd64", "arm64")

# Every image reference must start with this: registry, then the two
# variables, then the fixed image name.
ROUTED = "${{ env.REGISTRY }}/${{ env.IMAGE_NAMESPACE }}/${{ env.IMAGE_PREFIX }}onionpress-"

# The base the stress worker must be told to extend.
TOR_BASE = "TOR_IMAGE=" + ROUTED + "tor:latest"

# What each branch's copy of the workflow must publish under.
EXPECTED_PREFIX = {
    "main": "",
    "development": "dev-",
}


def _read(rel_path):
    with open(os.path.join(PROJECT_ROOT, rel_path), "r", encoding="utf-8") as f:
        return f.read()


def _code(text):
    """Workflow text with `#` comment lines removed.

    The header explains the traps by naming the bare image names and the old
    self-hosted runner, so scanning the raw text would match the explanation
    instead of the instruction.
    """
    return "\n".join(
        line for line in text.splitlines()
        if not line.lstrip().startswith("#")
    )


def _env_value(code, key):
    """Value of the top-level `  KEY:` entry under env:, or None if absent.

    Accepts the bare, "double-quoted" and 'single-quoted' spellings, and an
    empty value written either as `KEY:` or `KEY: ""`.
    """
    m = re.search(r"^  " + re.escape(key) + r":[ \t]*(.*?)[ \t]*$", code, re.MULTILINE)
    if not m:
        return None
    value = m.group(1)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def _ci_branch():
    """The branch this CI run is for: the PR's base, else the pushed ref.

    GitHub sets GITHUB_BASE_REF only for pull_request events, where
    GITHUB_REF_NAME is the synthetic "<n>/merge" ref. Empty outside CI.
    """
    return os.environ.get("GITHUB_BASE_REF") or os.environ.get("GITHUB_REF_NAME") or ""


class PublishWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.text = _read(WORKFLOW)
        self.code = _code(self.text)

    # ── 1. Runs anywhere ──────────────────────────────────────────────────

    def test_every_job_runs_on_a_github_hosted_runner(self):
        runners = re.findall(r"^\s*runs-on:[ \t]*(.+?)[ \t]*$", self.code, re.MULTILINE)
        self.assertTrue(runners, f"{WORKFLOW}: no runs-on lines found")
        offenders = [r for r in runners if not re.fullmatch(r"ubuntu-[a-z0-9.]+(-arm)?", r)]
        self.assertEqual(
            offenders, [],
            f"{WORKFLOW}: jobs that do not run on a GitHub-hosted Ubuntu runner: "
            f"{offenders}. A self-hosted runner makes every release depend on "
            f"one machine being online, and a fork cannot run the workflow at all.")

    def test_namespace_is_the_repository_owner_by_default(self):
        value = _env_value(self.code, "IMAGE_NAMESPACE")
        self.assertIsNotNone(value, f"{WORKFLOW}: no top-level `IMAGE_NAMESPACE:` under env:")
        self.assertIn(
            "github.repository_owner", value,
            f"{WORKFLOW}: IMAGE_NAMESPACE is {value!r}; it must default to "
            f"github.repository_owner so a fork publishes under its own account.")

    def test_no_hardcoded_account_in_any_image_reference(self):
        hardcoded = [line.strip() for line in self.code.splitlines()
                     if re.search(r"ghcr\.io/[a-z0-9-]+/|/brewsterkahle/", line)]
        self.assertEqual(
            hardcoded, [],
            f"{WORKFLOW}: image references with a literal account name, which "
            f"a fork's token cannot push to:\n  " + "\n  ".join(hardcoded))

    # ── 2. Named through two variables ────────────────────────────────────

    def test_namespace_and_prefix_are_declared_once(self):
        for key in ("IMAGE_NAMESPACE", "IMAGE_PREFIX"):
            declarations = re.findall(r"^  " + key + r":", self.code, re.MULTILINE)
            self.assertEqual(
                len(declarations), 1,
                f"{WORKFLOW}: expected exactly one top-level `{key}:` under env:, "
                f"found {len(declarations)}")

    def test_every_image_reference_goes_through_both_variables(self):
        bypassing = [line.strip() for line in self.code.splitlines()
                     if re.search(r"onionpress-(tor|wordpress|stress-worker)", line)
                     and ROUTED not in line]
        self.assertEqual(
            bypassing, [],
            f"{WORKFLOW}: image references that bypass IMAGE_NAMESPACE or "
            f"IMAGE_PREFIX, so the one-line switches would not rename them:\n  "
            + "\n  ".join(bypassing))
        routed = re.findall(re.escape(ROUTED) + r"([a-z-]+)(?=[:\"\s])", self.code)
        self.assertEqual(
            tuple(sorted(set(routed))), IMAGES,
            f"{WORKFLOW}: images routed through the variables are "
            f"{sorted(set(routed))}, expected {list(IMAGES)}")

    def test_both_architectures_are_built_for_every_image(self):
        for image in IMAGES:
            for arch in ARCHES:
                tag = f"{ROUTED}{image}:build-{arch}"
                self.assertIn(
                    tag, self.code,
                    f"{WORKFLOW}: no job pushes {tag}; the merge step needs both "
                    f"halves and shipped installs run on both architectures.")

    def test_prefix_matches_the_branch_ci_runs_for(self):
        branch = _ci_branch()
        if branch not in EXPECTED_PREFIX:
            self.skipTest(
                f"CI is running for {branch!r}, not main or development"
                if branch else "not running in CI (GITHUB_REF_NAME unset)")
        expected = EXPECTED_PREFIX[branch]
        actual = _env_value(self.code, "IMAGE_PREFIX")
        self.assertEqual(
            actual, expected,
            f"{WORKFLOW} sets IMAGE_PREFIX to {actual!r}, but this run is for "
            f"the {branch} branch, which must publish with prefix "
            f"{expected!r}. "
            + ("A merge of development into main carries \"dev-\" along: "
               "set IMAGE_PREFIX to \"\" in the merge commit."
               if branch == "main" else
               "The development branch must never publish over the release "
               "images: set IMAGE_PREFIX back to \"dev-\"."))

    # ── 3. The stress worker extends this run's tor image ─────────────────

    def test_stress_worker_extends_the_tor_image_this_run_built(self):
        contexts = len(re.findall(r"^\s*context:[ \t]*tests/stress[ \t]*$", self.code, re.MULTILINE))
        self.assertEqual(
            contexts, len(ARCHES),
            f"{WORKFLOW}: expected {len(ARCHES)} stress-worker build steps "
            f"(context: tests/stress), found {contexts}")
        self.assertEqual(
            self.code.count(TOR_BASE), len(ARCHES),
            f"{WORKFLOW}: every stress-worker build must pass the build-arg "
            f"{TOR_BASE!r}; found {self.code.count(TOR_BASE)} of {len(ARCHES)}. "
            f"Without it tests/stress/Dockerfile falls back to its ARG default, "
            f"the published production tor image.")


if __name__ == "__main__":
    unittest.main()

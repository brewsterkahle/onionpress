#!/usr/bin/env python3
"""Static invariants for .github/workflows/docker-publish.yml.

The workflow publishes the container images every install pulls, and it only
runs on a release or a manual dispatch, so a property broken today is found
on release day. Four of them matter enough to pin here:

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

4. A release builds nothing (issue #327). Releases pin digests from an
   earlier "Run workflow" run; publishing the release used to rebuild every
   image and move :latest and :<version> off the pinned ones, so a tag
   named an image no release had tested (2.5.2, 2.5.3). Every build job now
   runs on workflow_dispatch only, and on a release the tag-release job
   points both tags at build/image-pins.env's digests and reads them back.
   Its script is run here against a stub docker.

No PyYAML: CI installs nothing beyond CPython, so the workflow is read with
regexes, like tests/test_image_pins.py does for docker-compose.yml.
"""

import os
import re
import shutil
import subprocess
import tempfile
import textwrap
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


def _jobs(code):
    """{job id: its text} for every job under `jobs:`."""
    body = code.split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([A-Za-z0-9_-]+):[ \t]*$", body, flags=re.MULTILINE)
    return dict(zip(parts[1::2], parts[2::2]))


def _run_block(job_text, step_name):
    """The `run: |` script of the step named step_name, dedented."""
    m = re.search(
        r"^\s*- name: " + re.escape(step_name) + r"[ \t]*\n\s*run: \|[ \t]*\n"
        r"((?:[ \t]*\n|[ \t]{10,}.*\n)+)",
        job_text + "\n", re.MULTILINE)
    return textwrap.dedent(m.group(1)) if m else None


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


# ── 4. A release builds nothing; it tags the pinned digests ──────────────

RELEASE_JOB = "tag-release"
RELEASE_STEP = "Point :latest and :<version> at the pinned digests"
ON_DISPATCH = "if: github.event_name == 'workflow_dispatch'"
ON_RELEASE = "if: github.event_name == 'release'"

# Stands in for `docker` in the tag-release script. `imagetools create`
# records which source each tag now names; `imagetools inspect` answers with
# buildx's text format, Digest taken from the source's @sha256:. STUB_SKEW
# makes inspect report a different digest, as a create that rewrote the
# index would.
STUB_DOCKER = r"""#!/bin/sh
echo "$*" >> "$STUB_DIR/calls"
[ "$1 $2" = "buildx imagetools" ] || exit 2
case "$3" in
  create)
    shift 3; tags=""
    while [ "$1" = "-t" ]; do tags="$tags $2"; shift 2; done
    for t in $tags; do printf '%s %s\n' "$t" "${1##*@}" >> "$STUB_DIR/tags"; done ;;
  inspect)
    d=$(awk -v t="$4" '$1 == t {d = $2} END {print d}' "$STUB_DIR/tags")
    [ -n "$d" ] || exit 1
    [ -z "$STUB_SKEW" ] || d="sha256:$(printf '%064d' 7)"
    printf 'Name:      %s\nMediaType: application/vnd.oci.image.index.v1+json\nDigest:    %s\n' "$4" "$d" ;;
  *) exit 2 ;;
esac
"""

SUBST = {
    "${{ env.IMAGE_VERSION }}": "v9.9.9",
    "${{ env.REGISTRY }}": "ghcr.io",
    "${{ env.IMAGE_NAMESPACE }}": "someone",
    "${{ env.IMAGE_PREFIX }}": "",
}


class ReleaseTagsPinnedDigestsTest(unittest.TestCase):
    def setUp(self):
        self.code = _code(_read(WORKFLOW))
        self.jobs = _jobs(self.code)

    def test_release_trigger_still_runs_the_workflow(self):
        # Dropping the trigger was the other option in #327; this one keeps
        # :<version> appearing at release time, so the job must be reachable.
        self.assertRegex(self.code, r"(?m)^  release:\n    types: \[published\]")
        self.assertIn(RELEASE_JOB, self.jobs, f"{WORKFLOW}: no {RELEASE_JOB} job")

    def test_every_build_job_runs_on_dispatch_only(self):
        offenders = [name for name, text in self.jobs.items()
                     if name != RELEASE_JOB and ON_DISPATCH not in text]
        self.assertEqual(
            offenders, [],
            f"{WORKFLOW}: jobs not gated with `{ON_DISPATCH}`: {offenders}. "
            f"On a release they would rebuild the images and move :latest and "
            f":<version> off the digests the release pinned (issue #327).")

    def test_release_job_builds_nothing(self):
        text = self.jobs[RELEASE_JOB]
        self.assertIn(ON_RELEASE, text)
        self.assertNotIn("build-push-action", text)
        self.assertNotIn(":build-", text,
                         f"{RELEASE_JOB} must tag the pinned digests, not this "
                         f"run's or an earlier run's :build-<arch> images")
        self.assertIn("build/image-pins.env", text)

    def _run(self, pins=None, skew=False):
        script = _run_block(self.jobs[RELEASE_JOB], RELEASE_STEP)
        self.assertIsNotNone(script, f"{RELEASE_JOB}: no step named {RELEASE_STEP!r}")
        for k, v in SUBST.items():
            script = script.replace(k, v)
        self.assertNotIn("${{", script, "unsubstituted expression in the script")
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "bin"))
            os.makedirs(os.path.join(tmp, "build"))
            stub = os.path.join(tmp, "bin", "docker")
            with open(stub, "w") as f:
                f.write(STUB_DOCKER)
            os.chmod(stub, 0o755)
            shutil.copy(os.path.join(PROJECT_ROOT, "build/image-mirrors.txt"),
                        os.path.join(tmp, "build", "image-mirrors.txt"))
            pins_path = os.path.join(tmp, "build", "image-pins.env")
            if pins is None:
                shutil.copy(os.path.join(PROJECT_ROOT, "build/image-pins.env"), pins_path)
            else:
                with open(pins_path, "w") as f:
                    f.write(pins)
            env = dict(os.environ, STUB_DIR=tmp,
                       PATH=os.path.join(tmp, "bin") + os.pathsep + os.environ["PATH"])
            if skew:
                env["STUB_SKEW"] = "1"
            # How Actions runs a `run:` step: bash -e -o pipefail.
            result = subprocess.run(
                ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
                cwd=tmp, env=env, capture_output=True, text=True, timeout=60)
            calls = ""
            if os.path.exists(os.path.join(tmp, "calls")):
                with open(os.path.join(tmp, "calls")) as f:
                    calls = f.read()
        return result, calls

    def _pins(self):
        pins = {}
        for line in _read("build/image-pins.env").splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                pins[k] = v
        return pins

    def test_tags_point_at_the_pinned_digests(self):
        if not shutil.which("bash"):
            self.skipTest("bash not available")
        result, calls = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        pins = self._pins()
        for key, image in (("ONIONPRESS_TOR_IMAGE", "onionpress-tor"),
                           ("ONIONPRESS_WORDPRESS_IMAGE", "onionpress-wordpress")):
            target = f"ghcr.io/someone/{image}"
            self.assertIn(
                f"buildx imagetools create -t {target}:latest -t {target}:9.9.9 {pins[key]}",
                calls)
            for tag in ("latest", "9.9.9"):
                self.assertIn(f"{target}:{tag} -> {pins[key].split('@', 1)[1]}",
                              result.stdout)

    def test_mirrored_images_get_the_version_tag_only(self):
        """Issue #501: each third-party mirror is tagged :<version> from its
        pin, and never :latest (that would move a tag nobody pinned)."""
        if not shutil.which("bash"):
            self.skipTest("bash not available")
        result, calls = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        pins = self._pins()
        for key, package in (("MARIADB_IMAGE", "onionpress-mariadb"),
                             ("AUTOHEAL_IMAGE", "onionpress-autoheal"),
                             ("CLOUDFLARED_IMAGE", "onionpress-cloudflared"),
                             ("SOCKET_PROXY_IMAGE", "onionpress-socket-proxy")):
            target = f"ghcr.io/someone/{package}"
            self.assertIn(
                f"buildx imagetools create -t {target}:9.9.9 {pins[key]}", calls)
            self.assertNotIn(f"{target}:latest", calls)

    def test_fails_when_a_tag_does_not_read_back_as_the_pin(self):
        if not shutil.which("bash"):
            self.skipTest("bash not available")
        result, _ = self._run(skew=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("but the release pins", result.stdout)

    def test_fails_without_a_digest_pin(self):
        if not shutil.which("bash"):
            self.skipTest("bash not available")
        result, calls = self._run(pins="ONIONPRESS_TOR_IMAGE=ghcr.io/x/onionpress-tor:latest\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("imagetools create", calls)


if __name__ == "__main__":
    unittest.main()

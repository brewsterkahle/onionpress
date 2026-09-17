#!/usr/bin/env python3
"""Static invariants for the container build contexts and build/build-images.sh.

The images used to exist only as GHCR artifacts built by
.github/workflows/docker-publish.yml, which runs on release and whose arm64
half runs on a self-hosted Mac. build/build-images.sh is the local path.
These checks guard the parts of it that fail quietly rather than loudly.
"""

import os
import re
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

CONTEXTS = (
    "app/Resources/docker/tor",
    "app/Resources/docker/wordpress",
    "tests/stress",
)


def _read(rel_path):
    with open(os.path.join(PROJECT_ROOT, rel_path), "r", encoding="utf-8") as f:
        return f.read()


def _code(rel_path):
    """File contents with `#` comment lines removed.

    These scripts and Makefile targets document the traps they avoid, so the
    prose contains the exact strings some checks scan for. Scanning the raw
    text matches the explanation instead of the instruction.
    """
    return "\n".join(
        line for line in _read(rel_path).splitlines()
        if not line.lstrip().startswith("#")
    )


class TestDockerIgnore(unittest.TestCase):
    """Docker does not read .gitignore. __pycache__/ is gitignored, so it is
    invisible in `git status` yet fully present in the build context — 544K of
    the tor context's 964K. Worse, `COPY wordlists /wordlists` baked those
    .pyc files into the published image, so that layer's cache key depended on
    whether the developer had run the test suite. Two of them had no .py
    source left in the repo (follow-fetch, wayback-static): deleted modules
    shipping as bytecode.

    tests/test_onionnames.py and tests/test_onionheaven_integration.py put
    app/Resources/docker/tor on sys.path and import from it, so these
    directories come back after every test run. An exclude is the only fix.
    """

    def test_every_context_has_a_dockerignore(self):
        for context in CONTEXTS:
            with self.subTest(context=context):
                path = os.path.join(PROJECT_ROOT, context, ".dockerignore")
                self.assertTrue(
                    os.path.exists(path),
                    f"{context}/.dockerignore is missing. Without it "
                    "__pycache__ is shipped to the daemon and, for the tor "
                    "context, baked into the image.",
                )

    def test_dockerignore_excludes_bytecode(self):
        for context in CONTEXTS:
            text = _read(os.path.join(context, ".dockerignore"))
            with self.subTest(context=context):
                self.assertIn("__pycache__/", text)
                self.assertIn("*.py[cod]", text)

    def test_dockerignore_is_not_an_allowlist(self):
        """The tor Dockerfile COPYs 18 individually named files. An
        exclude-everything-then-allowlist .dockerignore that misses one fails
        late with a confusing 'file not found' at COPY time, so these must
        stay targeted excludes.
        """
        for context in CONTEXTS:
            lines = [
                line.strip()
                for line in _read(os.path.join(context, ".dockerignore")).splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
            with self.subTest(context=context):
                self.assertNotIn(
                    "*", lines,
                    f"{context}/.dockerignore must not exclude everything — "
                    "the Dockerfiles COPY individually named files and an "
                    "allowlist that misses one fails at COPY time.",
                )


class TestDebPackagingStripsBytecode(unittest.TestCase):
    """build-linux.sh copies app/Resources/docker verbatim into the .deb. It
    already stripped __pycache__ from the lib/onionpress copy but not from
    this one, so 18 stale .pyc files shipped to every Linux user.
    """

    def test_collect_files_strips_pycache_from_docker_tree(self):
        script = _read("build/build-linux.sh")
        match = re.search(
            r'cp -r "\$PROJECT_DIR/app/Resources/docker" "\$dest/docker"\n(.*?)\n\n',
            script, re.S,
        )
        self.assertIsNotNone(
            match,
            "Could not find the docker-tree copy in build-linux.sh's "
            "collect_files — has it been restructured? Update this test.",
        )
        self.assertIn(
            "__pycache__", match.group(1),
            "build-linux.sh must strip __pycache__ from the copied docker "
            "tree, the way it already does for lib/onionpress.",
        )


class TestStressWorkerBaseIsParameterized(unittest.TestCase):
    """tests/stress FROMs the tor image. A literal made `build-images.sh all`
    silently extend whatever :latest was published instead of the tor image
    just built locally, so local stress runs tested someone else's build.
    """

    def test_dockerfile_takes_a_tor_image_arg(self):
        text = _read("tests/stress/Dockerfile")
        self.assertRegex(
            text, r"ARG TOR_IMAGE=",
            "tests/stress/Dockerfile must declare ARG TOR_IMAGE.",
        )
        self.assertRegex(
            text, r"FROM \$\{TOR_IMAGE\}",
            "tests/stress/Dockerfile must FROM ${TOR_IMAGE}.",
        )

    def test_build_script_chains_stress_worker_off_local_tor(self):
        script = _read("build/build-images.sh")
        self.assertIn(
            "--build-arg", script,
            "build-images.sh must pass TOR_IMAGE through to the stress "
            "worker build so it chains off the local tor image.",
        )
        self.assertIn("TOR_IMAGE=", script)


class TestBuildImagesScript(unittest.TestCase):

    def test_covers_every_context(self):
        script = _read("build/build-images.sh")
        for context in CONTEXTS:
            with self.subTest(context=context):
                self.assertIn(
                    context, script,
                    f"build-images.sh must know how to build {context}.",
                )

    def test_shadow_tags_the_ghcr_name_by_default(self):
        """Both launchers gate vanity-address generation on a tag-only
        `docker image inspect ghcr.io/...onionpress-tor:latest`. A local build
        tagged only onionpress-tor:dev fails that check and the install
        silently falls back to a random .onion — the v2.4.101 regression.
        """
        script = _read("build/build-images.sh")
        self.assertIn(
            "SHADOW_TAG=1", script,
            "build-images.sh must shadow-tag the GHCR name by default so the "
            "launchers' vanity-generation presence check keeps passing.",
        )
        self.assertIn("--no-shadow-tag", script)

    def test_refuses_multiplatform_without_push(self):
        """Docker cannot --load a multi-platform build. Catching it up front
        matters more than usual here: the failure would otherwise land at the
        end of a multi-hour emulated arti compile.
        """
        script = _read("build/build-images.sh")
        self.assertIn("PLATFORM_COUNT", script)
        self.assertRegex(
            script, r'PLATFORM_COUNT" -gt 1 \] && \[ "\$PUSH" = "0"',
            "build-images.sh must refuse multi-platform builds that are not "
            "pushed, before starting the build.",
        )

    def test_sets_provenance_explicitly(self):
        """docker/build-push-action attaches provenance attestations by
        default and raw `docker buildx build` does not. Commit 419b53ec had to
        move from `docker manifest` to `buildx imagetools` over exactly that
        mismatch, so the script must not inherit either default.
        """
        script = _read("build/build-images.sh")
        self.assertIn(
            "--provenance", script,
            "build-images.sh must pass --provenance explicitly.",
        )


class TestValidationWorkflow(unittest.TestCase):
    """The CI job that keeps the local build path from rotting. Several of its
    properties are security-relevant rather than merely nice.
    """

    WORKFLOW = ".github/workflows/build-images.yml"

    def test_workflow_exists(self):
        self.assertTrue(
            os.path.exists(os.path.join(PROJECT_ROOT, self.WORKFLOW)),
            f"{self.WORKFLOW} is missing — without it, a change that breaks "
            "the Dockerfiles is only discovered on release day.",
        )

    def test_never_runs_on_the_self_hosted_mac(self):
        """This triggers on pull_request, so a fork PR runs contributor code.
        Routing that to [self-hosted, macOS, ARM64] would execute it on the
        maintainer's machine.

        Checks the `runs-on:` values rather than the raw text — the word
        appears in this workflow's own comments explaining why it is absent.
        """
        runners = re.findall(r"^\s*runs-on:\s*(.+)$", _read(self.WORKFLOW), re.M)
        self.assertTrue(
            runners,
            "Could not find any runs-on: in the validation workflow — has it "
            "been restructured? Update this test.",
        )
        for runner in runners:
            with self.subTest(runner=runner):
                self.assertNotIn(
                    "self-hosted", runner,
                    "The PR validation workflow must never use the "
                    "self-hosted runner — it runs untrusted fork code.",
                )

    def test_never_pushes(self):
        text = _read(self.WORKFLOW)
        self.assertNotIn(
            "--push", text,
            "The PR validation workflow must be build-only.",
        )
        self.assertNotIn(
            "packages: write", text,
            "The PR validation workflow must not request package write "
            "permission.",
        )

    def test_does_not_evict_the_release_cache(self):
        """A PR may read the release scope but must write to its own, or
        docker-publish.yml's arti cache gets churned by every PR.
        """
        text = _read(self.WORKFLOW)
        cache_to = re.findall(r"--cache-to\s+\"([^\"]+)\"", text)
        self.assertTrue(
            cache_to,
            "Could not find --cache-to in the validation workflow — has it "
            "been restructured? Update this test.",
        )
        for spec in cache_to:
            with self.subTest(spec=spec):
                self.assertIn(
                    "scope=pr-", spec,
                    "PR builds must write to a PR-scoped cache, not the "
                    "release scope docker-publish.yml depends on.",
                )

    def test_exports_the_gha_cache_runtime(self):
        """`type=gha` needs ACTIONS_RUNTIME_TOKEN / ACTIONS_RESULTS_URL in the
        environment. docker/build-push-action injects them; a plain `run:`
        step does not, and without them --cache-from silently no-ops — every
        PR would pay for a cold arti compile.
        """
        text = _read(self.WORKFLOW)
        self.assertIn(
            "ghaction-github-runtime", text,
            "The validation workflow calls build-images.sh from a `run:` "
            "step, so it must export the GitHub Actions runtime for the gha "
            "cache backend to work.",
        )

    def test_unit_test_workflow_stays_unfiltered(self):
        """Adding a paths: filter to test.yml's pull_request trigger would
        gate the unit tests too, so a Python-only PR would report no test run
        and the required check would never appear. That is why image builds
        live in their own workflow.
        """
        text = _read(".github/workflows/test.yml")
        match = re.search(r"pull_request:\s*\n(\s+)branches:", text)
        self.assertIsNotNone(
            match,
            "Could not find test.yml's pull_request trigger — has it been "
            "restructured? Update this test.",
        )
        self.assertNotIn(
            "paths:", text,
            "test.yml must not gain a paths: filter — it would silently skip "
            "the unit tests on PRs that do not touch the filtered paths.",
        )


if __name__ == "__main__":
    unittest.main()


class TestBuildEntryPoints(unittest.TestCase):
    """Every build script should be reachable from `make`, and `make help`
    should list it — the help block is a hardcoded echo, so it drifts silently.
    """

    TARGETS = {
        "images": "build/build-images.sh",
        "dev-up": "build/dev-up.sh",
        "dmg": "build/build-dmg-simple.sh",
        "deb": "build/build-linux.sh",
        "extension": "build/build-extension.sh",
        "icons": "build/make-icons.sh",
        "doctor": "build/doctor.sh",
    }

    def test_every_script_has_a_make_target(self):
        makefile = _read("Makefile")
        for target, script in self.TARGETS.items():
            with self.subTest(target=target):
                self.assertTrue(
                    os.path.exists(os.path.join(PROJECT_ROOT, script)),
                    f"{script} is missing.",
                )
                self.assertRegex(
                    makefile, rf"(?m)^{re.escape(target)}:",
                    f"Makefile must define a `{target}` target for {script}.",
                )

    def test_help_lists_every_target(self):
        makefile = _read("Makefile")
        help_block = makefile.split("help:", 1)[1].split("\n\n# ", 1)[0]
        for target in self.TARGETS:
            with self.subTest(target=target):
                self.assertIn(
                    f"make {target}", help_block,
                    f"`make help` must mention `make {target}` — the help "
                    "block is a hardcoded echo and drifts silently.",
                )

    def test_phony_covers_every_target(self):
        makefile = _read("Makefile")
        phony = re.search(r"\.PHONY:(.*?)(?=\n[a-z])", makefile, re.S)
        self.assertIsNotNone(phony, "Could not find .PHONY — update this test.")
        declared = set(phony.group(1).replace("\\", " ").split())
        for target in self.TARGETS:
            with self.subTest(target=target):
                self.assertIn(target, declared)

    def test_dead_dmg_script_is_gone(self):
        """build/build-dmg.sh was reachable as the default-looking `make build`
        and was actively damaging: it packaged a bundle at the never-produced
        lowercase path onionpress.app — which resolves to the real
        OnionPress.app on case-insensitive APFS — ran `lipo -thin arm64` over
        its binaries, destroying the universal-binary invariant that
        validate-bundle.sh and test-bundle.sh enforce, and then failed under
        `set -e` on a background image that does not exist, leaving the damaged
        bundle behind.
        """
        self.assertFalse(
            os.path.exists(os.path.join(PROJECT_ROOT, "build/build-dmg.sh")),
            "build/build-dmg.sh thins universal binaries to arm64-only and "
            "should not exist.",
        )
        self.assertNotIn(
            "build-dmg.sh", _code("Makefile").replace("build-dmg-simple.sh", ""),
            "No make target may invoke build/build-dmg.sh.",
        )

    def test_make_test_checks_pin_consistency(self):
        self.assertIn(
            "refresh-image-digests.sh --check", _read("Makefile"),
            "`make test` should catch image-pin drift before a build.",
        )

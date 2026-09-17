#!/usr/bin/env python3
"""Static invariants for container-image pins.

build/image-pins.env is the single source of truth for which
ghcr.io/brewsterkahle/onionpress-* images the stack runs. Five consumers
embed those literals so a bare `docker compose up` works with no environment
set, and build/refresh-image-digests.sh rewrites all of them together.

This file exists because "rewrites them together" was previously only a
convention. v2.4.110 (commit 94ce1a36, message "refresh image digests")
updated docker-compose.yml alone, so for that whole release macOS compose ran
tor@sha256:1f98ac29… while the Linux launcher, vanity-key generation
(launcher_ops.DEFAULT_TOR_IMAGE) and the OnionHeaven farm workers
(containers.ONIONHEAVEN_IMAGE) all still ran tor@sha256:ecab8ad6…. One
install, two different Tor builds, and an "up to date" log line about an
image the stack never started. These tests turn that from a thing you must
remember into a thing you cannot commit.

Guarded here:
  1. Every consumer's embedded literal equals build/image-pins.env.
  2. The sites that are deliberately NOT pinned stay that way — a blanket
     "pin everything" sweep would break the vanity-generation presence check
     and the in-container takeover default.
  3. The env-var override that lets a locally built stack take over
     (build/build-images.sh) is wired up in all five consumers.

No PyYAML: CI installs nothing beyond CPython, so docker-compose.yml is read
with a regex. Note that the `onionheaven` service wraps its pin in
${ONIONHEAVEN_IMAGE:-${ONIONPRESS_TOR_IMAGE:-…}}, so a pattern anchored to
"image: ghcr.io" would miss it.
"""

import os
import re
import subprocess
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

PINS_FILE = "build/image-pins.env"

# Every file build/refresh-image-digests.sh rewrites. Keep in sync with that
# script's CONSUMERS list — test_refresh_script_covers_same_consumers below
# asserts they match, so this list cannot silently fall behind.
CONSUMERS = (
    "app/Resources/docker/docker-compose.yml",
    "app/MacOS/onionpress",
    "linux/onionpress",
    "src/onionpress/containers.py",
    "src/onionpress/launcher_ops.py",
)

TOR_REPO = "ghcr.io/brewsterkahle/onionpress-tor:latest"
WP_REPO = "ghcr.io/brewsterkahle/onionpress-wordpress:latest"

# A reference in "pin context": a quoted string, a ${VAR:-…} default, or end
# of line. Mirrors the lookahead in build/refresh-image-digests.sh, which
# deliberately skips bare shell occurrences.
_PINNED = r'{repo}@sha256:(?P<digest>[a-f0-9]{{64}})(?=["}}\n])'


def _read(rel_path):
    with open(os.path.join(PROJECT_ROOT, rel_path), "r", encoding="utf-8") as f:
        return f.read()


def _load_pins():
    pins = {}
    for line in _read(PINS_FILE).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        pins[key.strip()] = value.strip()
    return pins


def _pinned_digests(text, repo):
    """Every digest this file pins `repo` to, in pin context."""
    return [m.group("digest")
            for m in re.finditer(_PINNED.format(repo=re.escape(repo)), text)]


class TestPinsFile(unittest.TestCase):
    """build/image-pins.env itself is well-formed."""

    def test_declares_both_images(self):
        pins = _load_pins()
        for key in ("ONIONPRESS_TOR_IMAGE", "ONIONPRESS_WORDPRESS_IMAGE"):
            self.assertIn(
                key, pins,
                f"{PINS_FILE} must declare {key} — has it been renamed? "
                "Update this test and build/refresh-image-digests.sh.",
            )

    def test_pins_are_digests_not_bare_tags(self):
        """A bare `:latest` here would silently un-pin the whole stack: the
        consumers would still resolve, so nothing would fail loudly, but
        `the v2.4.x tor image` would stop meaning one set of bytes.
        """
        for key, value in _load_pins().items():
            with self.subTest(key=key):
                self.assertRegex(
                    value, r"@sha256:[a-f0-9]{64}$",
                    f"{key} in {PINS_FILE} must be pinned to a sha256 digest, "
                    f"got {value!r}. These must be multi-arch INDEX digests "
                    "(`docker buildx imagetools inspect <tag>`), never a "
                    "single-platform manifest digest — the latter resolves on "
                    "one architecture and 404s on the other.",
                )


class TestConsumersMatchPins(unittest.TestCase):
    """The core invariant: no consumer may drift from build/image-pins.env."""

    def setUp(self):
        pins = _load_pins()
        self.tor = pins["ONIONPRESS_TOR_IMAGE"]
        self.wp = pins["ONIONPRESS_WORDPRESS_IMAGE"]
        self.tor_digest = self.tor.split("@", 1)[1].split(":", 1)[1]
        self.wp_digest = self.wp.split("@", 1)[1].split(":", 1)[1]

    def test_every_consumer_exists(self):
        for path in CONSUMERS:
            with self.subTest(path=path):
                self.assertTrue(
                    os.path.exists(os.path.join(PROJECT_ROOT, path)),
                    f"{path} is listed as an image-pin consumer but does not "
                    "exist — has it been renamed? Update this test and "
                    "build/refresh-image-digests.sh.",
                )

    def test_tor_pins_agree_everywhere(self):
        seen = {}
        for path in CONSUMERS:
            for digest in _pinned_digests(_read(path), TOR_REPO):
                seen.setdefault(digest, []).append(path)
        self.assertTrue(
            seen,
            f"No pinned {TOR_REPO} reference found in any consumer — the pin "
            "context regex or the consumer list is stale. Update this test.",
        )
        self.assertEqual(
            list(seen), [self.tor_digest],
            "Tor image pins have drifted from build/image-pins.env "
            f"(expected sha256:{self.tor_digest}). Found: "
            + "; ".join(f"sha256:{d} in {', '.join(f)}" for d, f in seen.items())
            + ". Fix with: build/refresh-image-digests.sh --propagate",
        )

    def test_wordpress_pins_agree_everywhere(self):
        seen = {}
        for path in CONSUMERS:
            for digest in _pinned_digests(_read(path), WP_REPO):
                seen.setdefault(digest, []).append(path)
        self.assertTrue(
            seen,
            f"No pinned {WP_REPO} reference found in any consumer — the pin "
            "context regex or the consumer list is stale. Update this test.",
        )
        self.assertEqual(
            list(seen), [self.wp_digest],
            "WordPress image pins have drifted from build/image-pins.env "
            f"(expected sha256:{self.wp_digest}). Found: "
            + "; ".join(f"sha256:{d} in {', '.join(f)}" for d, f in seen.items())
            + ". Fix with: build/refresh-image-digests.sh --propagate",
        )

    def test_both_launchers_pin_the_same_images(self):
        """The macOS launcher pulled bare `:latest` while the Linux launcher
        pinned by digest, so the two platforms warmed different layers from
        the same release. Both now pin; keep it that way.
        """
        for path in ("app/MacOS/onionpress", "linux/onionpress"):
            text = _read(path)
            with self.subTest(path=path):
                self.assertIn(
                    self.tor_digest, text,
                    f"{path} must pull the pinned tor image, not a bare tag.",
                )
                self.assertIn(
                    self.wp_digest, text,
                    f"{path} must pull the pinned wordpress image, not a bare tag.",
                )


class TestRefreshScriptOwnsEveryConsumer(unittest.TestCase):
    """The rewriter and this test must agree on what the consumers are, or
    a file can be added to one and silently skipped by the other.
    """

    def test_refresh_script_covers_same_consumers(self):
        script = _read("build/refresh-image-digests.sh")
        match = re.search(r"CONSUMERS = \[(.*?)\]", script, re.S)
        self.assertIsNotNone(
            match,
            "Could not find the CONSUMERS list in "
            "build/refresh-image-digests.sh — has it been restructured? "
            "Update this test.",
        )
        listed = set(re.findall(r'"([^"]+)"', match.group(1)))
        self.assertEqual(
            listed, set(CONSUMERS),
            "build/refresh-image-digests.sh and tests/test_image_pins.py "
            "disagree about which files embed image pins. A file in only one "
            "list is a file that drifts without anything noticing.",
        )

    def test_check_mode_passes_on_the_working_tree(self):
        """End-to-end: run the real script. --check fails if --propagate
        would change any consumer, so this covers the whole rewrite path
        rather than the literals alone.

        This exists because a purely textual assertion missed a real bug.
        Giving linux/onionpress's `docker image inspect` an
        ONIONPRESS_TOR_IMAGE override changed it from
            docker image inspect ghcr.io/…:latest >/dev/null
        to
            docker image inspect "${ONIONPRESS_TOR_IMAGE:-ghcr.io/…:latest}"
        which moved it INTO the rewriter's pin context. The text still looked
        right, and a test that only read the text still passed, but the next
        --propagate would have digest-pinned the presence check and silently
        disabled vanity-address generation for locally built images.
        """
        result = subprocess.run(
            ["bash", os.path.join(PROJECT_ROOT, "build/refresh-image-digests.sh"),
             "--check"],
            capture_output=True, text=True, cwd=PROJECT_ROOT, timeout=60,
        )
        self.assertEqual(
            0, result.returncode,
            "build/refresh-image-digests.sh --check failed:\n"
            f"{result.stdout}\n{result.stderr}",
        )

    def test_rewriter_never_touches_presence_checks(self):
        """`docker image inspect` lines must be excluded by what they do, not
        by how they happen to be punctuated.
        """
        script = _read("build/refresh-image-digests.sh")
        self.assertIn(
            'if "image inspect" in line:', script,
            "The rewriter must explicitly skip `docker image inspect` lines — "
            "relying on the pin-context lookahead alone already failed once.",
        )

    def test_refresh_script_offers_check_mode(self):
        """`--check` is what makes drift a build failure rather than a code
        review question; docs/BUILDING.md and `make doctor` both rely on it.
        """
        script = _read("build/refresh-image-digests.sh")
        self.assertIn(
            "--check", script,
            "build/refresh-image-digests.sh must keep its --check mode.",
        )


class TestDeliberatelyUnpinned(unittest.TestCase):
    """Not every image reference is meant to be pinned. A blanket sweep over
    `grep -r ghcr.io` breaks each of these in a way that is quiet at build
    time and painful at runtime.
    """

    def test_vanity_presence_check_stays_tag_only(self):
        """linux/onionpress decides whether vanity-address generation is
        available with `docker image inspect <tag>`. Pinning that to a digest
        makes a locally built image fail the check, and the install silently
        falls back to a random .onion — the v2.4.101 regression. A local build
        tags `:latest` precisely so this keeps working.
        """
        text = _read("linux/onionpress")
        match = re.search(r"docker image inspect \"?([^\"\n]+)\"?", text)
        self.assertIsNotNone(
            match,
            "Could not find the mkp224o presence check in linux/onionpress — "
            "has it moved or been renamed? Update this test.",
        )
        expr = match.group(1)
        self.assertNotIn(
            "@sha256:", expr,
            "The presence check must not be digest-pinned: a locally built "
            f"image would fail it. Got {expr!r}.",
        )
        # It follows ONIONPRESS_TOR_IMAGE so a local build is recognised, and
        # falls back to the bare tag — which build/build-images.sh also
        # shadow-tags, so the default keeps working either way.
        self.assertIn("${ONIONPRESS_TOR_IMAGE:-", expr)
        self.assertIn(TOR_REPO, expr)

    def test_in_container_takeover_default_stays_tag_only(self):
        """onionheaven_common.py runs INSIDE the tor container, where the
        right image is whichever one is already running. A digest baked in
        here would outlive the image that contains it.
        """
        text = _read("app/Resources/docker/tor/onionheaven_common.py")
        self.assertIn(
            f'"{TOR_REPO}"', text,
            "onionheaven_common.TAKEOVER_IMAGE must default to the plain tag.",
        )
        self.assertNotIn(
            "onionpress-tor:latest@sha256:", text,
            "onionheaven_common.py must not embed a digest — it runs inside "
            "the very image it would be pinning.",
        )

    def test_mariadb_and_autoheal_stay_floating(self):
        """Deliberate: we rely on upstream shipping security patches without
        us tracking digests. Documented in docs/BUILDING.md.
        """
        compose = _read("app/Resources/docker/docker-compose.yml")
        self.assertIn("image: mariadb:latest", compose)
        self.assertIn("image: willfarrell/autoheal:latest", compose)


class TestLocalImageOverride(unittest.TestCase):
    """build/build-images.sh hands a developer a locally built stack purely by
    exporting these variables. If a consumer stops honouring them it does not
    fail — it quietly keeps running the published image, and the developer
    tests someone else's build while believing they tested their own.
    """

    def test_compose_services_honour_overrides(self):
        compose = _read("app/Resources/docker/docker-compose.yml")
        self.assertIn(
            "${ONIONPRESS_TOR_IMAGE:-", compose,
            "docker-compose.yml's tor service must accept ONIONPRESS_TOR_IMAGE.",
        )
        self.assertIn(
            "${ONIONPRESS_WORDPRESS_IMAGE:-", compose,
            "docker-compose.yml's wordpress service must accept "
            "ONIONPRESS_WORDPRESS_IMAGE.",
        )
        self.assertIn(
            "${ONIONHEAVEN_IMAGE:-${ONIONPRESS_TOR_IMAGE:-", compose,
            "The onionheaven service must fall back through "
            "ONIONHEAVEN_IMAGE -> ONIONPRESS_TOR_IMAGE -> pin, so a local "
            "build replaces it too.",
        )

    def test_launchers_honour_overrides(self):
        for path in ("app/MacOS/onionpress", "linux/onionpress"):
            text = _read(path)
            with self.subTest(path=path):
                self.assertIn("${ONIONPRESS_TOR_IMAGE:-", text)
                self.assertIn("${ONIONPRESS_WORDPRESS_IMAGE:-", text)

    def test_python_modules_honour_overrides(self):
        """Checked as source text rather than by importing and mutating
        os.environ: both constants are evaluated at import time, and
        tests/test_onionheaven_*.py import these modules at module scope, so
        an env mutation here would leak across discovery order.
        """
        containers = _read("src/onionpress/containers.py")
        self.assertRegex(
            containers,
            r'os\.environ\.get\("ONIONHEAVEN_IMAGE"\)',
            "containers.ONIONHEAVEN_IMAGE must prefer the ONIONHEAVEN_IMAGE "
            "env var, matching docker-compose.yml's resolution order.",
        )
        self.assertRegex(
            containers,
            r'os\.environ\.get\("ONIONPRESS_TOR_IMAGE"\)',
            "containers.ONIONHEAVEN_IMAGE must fall back to "
            "ONIONPRESS_TOR_IMAGE before the pin.",
        )

        launcher_ops = _read("src/onionpress/launcher_ops.py")
        self.assertRegex(
            launcher_ops,
            r'os\.environ\.get\(\s*\n?\s*"ONIONPRESS_TOR_IMAGE"',
            "launcher_ops.DEFAULT_TOR_IMAGE must honour ONIONPRESS_TOR_IMAGE "
            "so vanity-key generation uses the same image as the rest of a "
            "locally built stack.",
        )


if __name__ == "__main__":
    unittest.main()

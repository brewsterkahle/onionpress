#!/usr/bin/env python3
"""Every external input to the container images is pinned and verified.

Before this, none of the 17 external inputs across the three Dockerfiles was
pinned: `FROM rust:latest`, `cargo install arti` with no --version,
`MKP224O_COMMIT=master`, `FROM wordpress:latest`, `COPY --from=docker:cli`,
and an unverified wp-cli.phar from a gh-pages branch. Two builds of the same
commit could ship a different compiler, a different arti and a different
mkp224o — and because CI builds amd64 and arm64 on separate runners at
different times, the two halves of one published manifest could disagree.

These are static checks on the Dockerfiles. They cannot prove an image builds;
they prove the inputs are named immutably and that the two supply-chain
assertions (the Tor apt key fingerprint, the wp-cli checksum) are still wired.
"""

import os
import re
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

TOR_DOCKERFILE = "app/Resources/docker/tor/Dockerfile"
WP_DOCKERFILE = "app/Resources/docker/wordpress/Dockerfile"
STRESS_DOCKERFILE = "tests/stress/Dockerfile"


def _read(rel_path):
    with open(os.path.join(PROJECT_ROOT, rel_path), "r", encoding="utf-8") as f:
        return f.read()


def _strip_comments(text):
    """Dockerfile text with comment lines removed.

    Needed because these Dockerfiles document the bugs they fixed, so the
    prose contains the very strings some of these checks scan for — an
    unstripped scan matches the explanation instead of the instruction.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def _arg_defaults(text):
    """{ARG name: default} for every `ARG NAME=default` in the file."""
    return dict(re.findall(r"^ARG\s+([A-Za-z_][A-Za-z0-9_]*)=(.+)$", text, re.M))


def _from_refs(text):
    """Every FROM's image reference, with `${ARG}` resolved from ARG defaults.

    Returns (resolved_ref, stage_alias_or_None) tuples. References that name an
    earlier stage are returned as-is so callers can filter them out.
    """
    args = _arg_defaults(text)
    out = []
    for match in re.finditer(r"^FROM\s+(\S+)(?:\s+AS\s+(\S+))?\s*$", text, re.M | re.I):
        ref, alias = match.group(1), match.group(2)
        resolved = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}",
                          lambda m: args.get(m.group(1), m.group(0)), ref)
        out.append((resolved, alias))
    return out


class TestBaseImagesArePinned(unittest.TestCase):

    def _assert_all_from_pinned(self, dockerfile):
        refs = _from_refs(_read(dockerfile))
        self.assertTrue(
            refs, f"No FROM found in {dockerfile} — has it been renamed?")
        stages = {alias for _, alias in refs if alias}
        for ref, _ in refs:
            if ref in stages:
                continue  # references an earlier stage in this file
            with self.subTest(dockerfile=dockerfile, ref=ref):
                self.assertRegex(
                    ref, r"@sha256:[a-f0-9]{64}$",
                    f"{dockerfile} has an unpinned base image: {ref}. Pin it "
                    "to a multi-arch INDEX digest — "
                    "`docker buildx imagetools inspect <tag>` — not a "
                    "per-platform manifest digest, which resolves on amd64 "
                    "and 404s on the arm64 builder.",
                )

    def test_tor_dockerfile_bases_are_pinned(self):
        self._assert_all_from_pinned(TOR_DOCKERFILE)

    def test_wordpress_dockerfile_bases_are_pinned(self):
        self._assert_all_from_pinned(WP_DOCKERFILE)

    def test_docker_cli_is_a_named_pinned_stage(self):
        """`COPY --from=docker:cli` was a base-image dependency hiding outside
        any FROM line, easy to miss when enumerating inputs. The binary is
        effectively privileged: this container has the docker socket mounted
        and uses it to create takeover containers.
        """
        text = _read(TOR_DOCKERFILE)
        self.assertNotRegex(
            text, r"COPY --from=docker:cli",
            "The docker CLI must come from a pinned, named stage, not a bare "
            "`--from=docker:cli` image reference.",
        )
        self.assertRegex(
            text, r"COPY --from=docker-cli\s",
            "Expected `COPY --from=docker-cli` — has the stage been renamed?",
        )

    def test_stress_worker_base_is_an_arg(self):
        """tests/stress FROMs the image CI just built, so a static digest is
        stale by construction; an ARG is the correct pin mechanism there.
        """
        text = _read(STRESS_DOCKERFILE)
        self.assertRegex(text, r"ARG TOR_IMAGE=")
        self.assertRegex(text, r"FROM \$\{TOR_IMAGE\}")


class TestArtiIsPinned(unittest.TestCase):
    """`cargo install arti --locked` without --version is not pinned: --locked
    applies the published crate's Cargo.lock to its dependency graph, but the
    arti version itself is still resolved to newest-at-build-time.
    """

    def test_cargo_install_has_an_explicit_version(self):
        text = _read(TOR_DOCKERFILE)
        self.assertRegex(
            text, r"ARG ARTI_VERSION=\d+\.\d+\.\d+",
            "The tor Dockerfile must declare ARG ARTI_VERSION=X.Y.Z.",
        )
        self.assertRegex(
            text, r"cargo install arti .*--version",
            "`cargo install arti` must pass --version; --locked alone pins "
            "the dependency graph, not arti itself.",
        )

    def test_keeps_the_features_the_app_needs(self):
        text = _read(TOR_DOCKERFILE)
        self.assertIn("onion-service-service", text)
        self.assertIn("static-sqlite", text)


class TestMkp224oIsPinned(unittest.TestCase):
    """MKP224O_COMMIT defaulted to `master` with a standing TODO in the file.
    mkp224o mints users' vanity onion addresses — an unpinned build of it is
    an unpinned key generator.
    """

    def test_commit_is_a_full_sha(self):
        args = _arg_defaults(_read(TOR_DOCKERFILE))
        commit = args.get("MKP224O_COMMIT", "")
        self.assertRegex(
            commit, r"^[0-9a-f]{40}$",
            "MKP224O_COMMIT must be a full 40-character commit SHA, not a "
            f"branch or tag. Got {commit!r}.",
        )

    def test_resolved_commit_is_asserted_after_clone(self):
        """A tag is a movable pointer, so cloning by tag is not a pin on its
        own. Fetching the SHA directly is not reliable either — that needs the
        server to allow arbitrary SHA1-in-want. Clone by tag, then assert.
        """
        text = _read(TOR_DOCKERFILE)
        self.assertIn(
            "git rev-parse HEAD", text,
            "The mkp224o build must resolve the cloned commit...",
        )
        self.assertRegex(
            text, r'\$actual" != "\$\{MKP224O_COMMIT\}"',
            "...and compare it against MKP224O_COMMIT, failing the build on "
            "mismatch.",
        )

    def test_matches_the_macos_build(self):
        """build-dmg-simple.sh cross-compiles the same mkp224o release for the
        macOS host. Two different versions minting addresses for the same
        project is a difference nobody would notice until the outputs differed.
        """
        args = _arg_defaults(_read(TOR_DOCKERFILE))
        dockerfile_version = args.get("MKP224O_VERSION", "")
        match = re.search(r'MKP224O_VERSION="([^"]+)"', _read("build/build-dmg-simple.sh"))
        self.assertIsNotNone(
            match,
            "Could not find MKP224O_VERSION in build-dmg-simple.sh — renamed? "
            "Update this test.",
        )
        self.assertEqual(
            dockerfile_version, match.group(1),
            "The tor Dockerfile and build-dmg-simple.sh must build the same "
            "mkp224o release.",
        )


class TestWpCliIsVerified(unittest.TestCase):
    """wp-cli.phar came from the gh-pages branch of wp-cli/builds: a moving
    target, no checksum, and no `-f` — so an HTTP error page was written to
    /usr/local/bin/wp and chmod +x'd, failing much later inside
    onionpress-multisite-init.sh instead of at build time.
    """

    def test_pinned_to_a_release_not_a_branch(self):
        text = _read(WP_DOCKERFILE)
        self.assertNotIn(
            "wp-cli/builds/gh-pages", text,
            "wp-cli must come from a tagged release asset, not the moving "
            "gh-pages branch.",
        )
        self.assertRegex(text, r"ARG WP_CLI_VERSION=\d+\.\d+\.\d+")

    def test_checksum_is_verified_before_chmod(self):
        text = _read(WP_DOCKERFILE)
        args = _arg_defaults(text)
        self.assertRegex(
            args.get("WP_CLI_SHA256", ""), r"^[a-f0-9]{64}$",
            "WP_CLI_SHA256 must be a full sha256.",
        )
        self.assertIn(
            "sha256sum -c -", text,
            "The wp-cli download must be checksum-verified.",
        )
        checksum_at = text.index("sha256sum -c -")
        chmod_at = text.index("chmod +x /usr/local/bin/wp")
        self.assertLess(
            checksum_at, chmod_at,
            "The checksum must be verified BEFORE the phar is made "
            "executable.",
        )


class TestTorAptKeyIsVerified(unittest.TestCase):
    """The signing key was fetched over HTTPS from a fingerprint-named URL with
    nothing comparing the fetched key's actual fingerprint to that name. A
    substituted key at that URL would have been installed and trusted.
    """

    def test_fingerprint_is_asserted(self):
        text = _read(TOR_DOCKERFILE)
        self.assertIn(
            "gpg --show-keys", text,
            "The Tor apt key's fingerprint must be re-derived from the "
            "fetched bytes with `gpg --show-keys` and compared.",
        )
        self.assertIn(
            "Tor apt signing key fingerprint mismatch", text,
            "A fingerprint mismatch must fail the build with a clear message.",
        )

    def test_fingerprint_is_not_overridable(self):
        """As an ARG it could be overridden with --build-arg, letting a
        builder point the fetch and the assertion at the same substituted key
        — a self-certifying check. It must be ENV or a literal.
        """
        text = _read(TOR_DOCKERFILE)
        # re.M: these anchors are per-line within the Dockerfile, not
        # whole-string.
        self.assertIsNone(
            re.search(r"^ARG\s+TOR_APT_KEY_FPR=", text, re.M),
            "TOR_APT_KEY_FPR must not be an ARG — --build-arg would let the "
            "fetch and the assertion be pointed at the same attacker key.",
        )
        self.assertIsNotNone(
            re.search(r"^ENV TOR_APT_KEY_FPR=[0-9A-F]{40}$", text, re.M),
            "TOR_APT_KEY_FPR must be an ENV with a full 40-hex fingerprint.",
        )

    def test_tor_package_is_deliberately_not_version_pinned(self):
        """deb.torproject.org removes superseded versions from the mirror and
        publishes no snapshot service, so an apt version pin becomes
        `E: Version '…' was not found` within weeks. The base-image digest is
        the right granularity, and the identifier users actually consume is
        the published image digest in build/image-pins.env.
        """
        text = _strip_comments(_read(TOR_DOCKERFILE))
        self.assertNotRegex(
            text, r"install[^\n]*\btor=\d",
            "The tor apt package must not be version-pinned — see the comment "
            "in the Dockerfile.",
        )


class TestNoSilentlyFailingDownloads(unittest.TestCase):
    """`curl` without -f exits 0 on an HTTP error and writes the error body to
    the output file. Both network fetches in these images had that bug.
    """

    def test_all_curl_downloads_use_fail_flag(self):
        for dockerfile in (TOR_DOCKERFILE, WP_DOCKERFILE):
            body = _strip_comments(_read(dockerfile))
            for match in re.finditer(r"curl\s+(-[A-Za-z]+)", body):
                flags = match.group(1)
                with self.subTest(dockerfile=dockerfile, flags=flags):
                    self.assertIn(
                        "f", flags,
                        f"{dockerfile}: `curl {flags}` lacks -f, so an HTTP "
                        "error page is written to the output file and the "
                        "build continues.",
                    )


if __name__ == "__main__":
    unittest.main()

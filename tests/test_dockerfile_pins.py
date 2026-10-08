#!/usr/bin/env python3
"""Every external input to the container images is pinned and verified.

Before this, none of the 17 external inputs across the three Dockerfiles was
pinned: `FROM rust:latest`, `cargo install arti` with no --version,
`MKP224O_COMMIT=master`, `FROM wordpress:latest`, `COPY --from=docker:cli`,
and an unverified wp-cli.phar from a gh-pages branch. Two builds of the same
commit could ship a different compiler, a different arti and a different
mkp224o — and because CI builds amd64 and arm64 on separate runners at
different times, the two halves of one published manifest could disagree.

Since Onimages 0.3.0 (2026-09-24) the tor image is built ON the Tor Project's
own C Tor image from containers.torproject.org. The apt-key verification this
file used to check therefore happens in the Tor Project's build, not ours;
what this file asserts instead is that the base is that image, pinned by
digest. Arti was removed the same day (no control interface for onion
services); the image must not grow it back by accident.

One input is pinned by compiler flag rather than by version: mkp224o's
configure.ac appends `-march=native`, so pinning its commit still left the
binary varying with the builder's CPU — and a binary built on a newer CI host
SIGILLs on an older user CPU, which nothing above it treats as fatal: the site
just carries on with a random address.

These are static checks on the Dockerfiles. They cannot prove an image builds;
they prove the inputs are named immutably and that the build-time assertions
(mkp224o commit, mkp224o CFLAGS, wp-cli checksum) are still wired.
"""

import os
import re
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

TOR_DOCKERFILE = "app/Resources/docker/tor/Dockerfile"
WP_DOCKERFILE = "app/Resources/docker/wordpress/Dockerfile"
STRESS_DOCKERFILE = "tests/stress/Dockerfile"
DMG_BUILD = "build/build-dmg-simple.sh"

# The Tor Project's onion-service images (the Onimages project), the only
# place Tor may come from.
ONIMAGES = "containers.torproject.org/tpo/onion-services/onimages"
ONIMAGES_TOR = ONIMAGES + "/tor"


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


def _expand_shell_vars(text, names):
    """Substitute `$NAME` / `${NAME}` for the named variables' literal values.

    Only the names asked for, and only one level. Checks that read a flag at
    its use site otherwise pass on the *name* while the value is empty — which
    is exactly how a revert would look.
    """
    for name in names:
        match = re.search(r'^\s*%s="([^"]*)"\s*$' % re.escape(name), text, re.M)
        if match is None:
            continue
        value = match.group(1)
        text = re.sub(r"\$\{%s\}|\$%s\b" % (re.escape(name), re.escape(name)),
                      value.replace("\\", "\\\\"), text)
    return text


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
    # Tolerates flags (`FROM --platform=$BUILDPLATFORM ...`) and trailing
    # comments. The previous pattern anchored at `\s*$` right after the
    # optional `AS`, so ANY such line failed to match and was silently never
    # checked — an unpinned `FROM --platform=... rust:latest` passed every
    # base-image test.
    for line in text.splitlines():
        match = re.match(
            r"^FROM\s+(?:--\S+\s+)*(\S+)(?:\s+AS\s+(\S+))?\s*(?:#.*)?$",
            line, re.I,
        )
        if not match:
            if re.match(r"^FROM\s", line, re.I):
                raise AssertionError(
                    f"Could not parse FROM line, so it would be silently "
                    f"skipped: {line!r}. Fix _from_refs in this test.")
            continue
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

    def test_tor_runtime_is_the_tor_projects_image(self):
        """The LAST FROM is the runtime base. It must be the Tor Project's own
        C Tor image (Onimages), not a plain Debian with Tor installed on top:
        that is where Tor, the deb.torproject.org apt source and its verified
        keyring now come from.
        """
        refs = [ref for ref, _ in _from_refs(_read(TOR_DOCKERFILE))]
        self.assertRegex(
            refs[-1], r"^" + re.escape(ONIMAGES_TOR) + r":\S+@sha256:",
            f"The tor image's runtime base must be {ONIMAGES_TOR}:<tag>@<digest>, "
            f"got {refs[-1]!r}.",
        )

    def test_tor_runtime_resets_user_to_root(self):
        """The Onimages base ends with USER debian-tor for its own ENTRYPOINT.
        /entrypoint.sh has to start as root — it chowns the state volumes,
        writes /etc/tor/torrc and drops privileges itself with su. Without
        `USER root` after the final FROM, every chown fails and Tor never
        starts, and nothing static short of this test notices.
        """
        text = _strip_comments(_read(TOR_DOCKERFILE))
        last_from = max(m.start() for m in re.finditer(r"^FROM\s", text, re.M))
        tail = text[last_from:]
        self.assertRegex(
            tail, re.compile(r"^USER root\s*$", re.M),
            "The runtime stage must `USER root` right after its FROM.",
        )
        user_root_at = tail.index("USER root")
        first_run_at = re.search(r"^RUN\s", tail, re.M).start()
        self.assertLess(
            user_root_at, first_run_at,
            "`USER root` must come before the first RUN of the runtime stage.",
        )
        self.assertNotRegex(
            tail[user_root_at:], re.compile(r"^USER\s+(?!root\b)", re.M),
            "Nothing after `USER root` may switch the image's user again — "
            "the entrypoint expects to start as root.",
        )

    def test_tor_runtime_clears_the_inherited_cmd(self):
        """The base's CMD is a list of tor flags (--RunAsDaemon 0
        --HiddenServiceDir …). Setting a new ENTRYPOINT does reset it, but say
        so explicitly so a future edit cannot hand those flags to
        /entrypoint.sh as arguments.
        """
        text = _strip_comments(_read(TOR_DOCKERFILE))
        self.assertRegex(
            text, re.compile(r'^ENTRYPOINT \["/entrypoint\.sh"\]\s*$', re.M))
        self.assertRegex(
            text, re.compile(r"^CMD \[\]\s*$", re.M),
            "The runtime stage must end with an explicit empty `CMD []`.",
        )

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


class TestMkp224oBaselineIsPinned(unittest.TestCase):
    """Pinning MKP224O_COMMIT pinned the source; the binary was still unpinned.

    mkp224o's configure.ac appends `-march=native` whenever the compiler takes
    it, so a bare `./configure` encoded the builder's exact CPU. That made this
    the last input to the tor image that still varied after everything else was
    pinned — and, worse, a binary built on a newer CI host dies with SIGILL on
    an older user CPU. mkp224o mints the vanity address, and
    generate_vanity_address in app/MacOS/onionpress treats any failure as
    "carry on with a random address", so that crash is silent and permanent.

    Every check here strips comments first: the Dockerfile explains all of this
    in prose that contains `-march=native` verbatim, so an unstripped scan
    would match the explanation and pass while the instruction was gone.
    """

    def _mkp224o_run(self):
        """The mkp224o build RUN as one line: comments dropped, continuations
        joined. Joining matters because the flags, the arch switch and the
        post-configure assertion are on separate physical lines of one command.
        """
        text = _strip_comments(_read(TOR_DOCKERFILE)).replace("\\\n", " ")
        for line in text.splitlines():
            if "mkp224o.git" in line:
                return line
        self.fail("Could not find the mkp224o build RUN in " + TOR_DOCKERFILE +
                  " — renamed? Update this test rather than passing vacuously.")

    def test_configure_is_given_explicit_cflags(self):
        run = self._mkp224o_run()
        self.assertNotRegex(
            run, r"&&\s*\./configure\s+&&",
            "`./configure` must not be run bare — with CFLAGS unset it appends "
            "-march=native and ties the binary to the build machine's CPU.",
        )
        self.assertRegex(
            run, r'CFLAGS="[^"]*"\s+\./configure',
            "The mkp224o build must pass CFLAGS to ./configure.",
        )

    def test_baseline_is_chosen_from_targetarch(self):
        """A single hardcoded -march cannot be right for both published
        manifests, and TARGETARCH is what BuildKit hands each build.
        """
        text = _strip_comments(_read(TOR_DOCKERFILE))
        self.assertIsNotNone(
            re.search(r"^ARG TARGETARCH\s*$", text, re.M),
            "ARG TARGETARCH must be declared in the mkp224o stage for BuildKit "
            "to populate it.",
        )
        arms = dict(re.findall(r'(amd64|arm64)\)\s*march="([^"]+)"',
                               self._mkp224o_run()))
        self.assertEqual(
            set(arms), {"amd64", "arm64"},
            "Both published architectures need a baseline. Got: " + repr(arms),
        )
        for arch, march in arms.items():
            with self.subTest(arch=arch):
                self.assertNotIn(
                    "native", march,
                    "The whole point is not to compile for the builder's CPU.",
                )

    def test_unknown_arch_fails_the_build(self):
        """Both fallbacks are silent: guessing an ISA SIGILLs at run time on
        the user's machine, and dropping -march lets configure put `native`
        back. Refusing to build is the only loud option.
        """
        run = self._mkp224o_run()
        match = re.search(r"\*\)(.*?)esac", run)
        self.assertIsNotNone(
            match, "Expected a `*)` default arm in the TARGETARCH case.")
        self.assertIn(
            "exit 1", match.group(1),
            "An unrecognised TARGETARCH must fail the build, not fall back to "
            "a guessed baseline or to no -march at all.",
        )

    def test_cflags_carry_the_optimisation_flags_too(self):
        """configure.ac applies its own `-O3 -march=native -fomit-frame-pointer`
        only when CFLAGS arrived unset — it compares CFLAGS across AC_PROG_CC.
        Passing CFLAGS to set -march therefore ALSO drops -O3, and nothing
        warns: configure succeeds, make succeeds, and the miner runs several
        times slower while minting perfectly correct addresses.
        """
        match = re.search(r'CFLAGS="([^"]*)"\s+\./configure',
                          self._mkp224o_run())
        self.assertIsNotNone(match, "No CFLAGS passed to ./configure.")
        cflags = match.group(1)
        self.assertIn(
            "-O3", cflags,
            "CFLAGS must carry -O3: setting CFLAGS at all makes configure skip "
            "the block that would otherwise supply it.",
        )
        self.assertIn("-fomit-frame-pointer", cflags)

    def test_the_flags_are_asserted_after_configure(self):
        """Static checks in this file cannot see what configure did with the
        flags. The build itself has to, because both ways of losing them —
        dropped -O3, restored -march=native — produce a working binary.
        """
        run = self._mkp224o_run()
        self.assertRegex(
            run,
            r'grep -q -- "\^CFLAGS=[^"]*-O3 -march=\$\{march\}'
            r'[^"]*" GNUmakefile',
            "The build must grep the generated GNUmakefile to confirm the "
            "flags it passed actually landed — a dropped -O3 is otherwise "
            "invisible.",
        )
        self.assertRegex(
            run, r'grep -q -- "-march=native" GNUmakefile',
            "The check must also fail if -march=native came back — that is the "
            "regression this pin exists to prevent.",
        )
        self.assertIn(
            "exit 1", run.split("GNUmakefile")[-1],
            "A failed flags check must fail the build.",
        )

    def test_macos_build_passes_the_optimisation_flags(self):
        """build-dmg-simple.sh hit the same configure quirk from the other
        side: it has always passed CFLAGS (to drive the universal
        cross-compile), so every shipped macOS mkp224o was built unoptimised.
        It needs no -march — `-arch arm64`/`-arch x86_64` already fix the ISA —
        but it does need -O3.
        """
        # Line continuations joined so one configure invocation is one line.
        # The `--prefix=` exclusion drops the libsodium cross-build, which the
        # same section performs: libsodium is a dependency, not the miner.
        # worker_batch.inc.h calls randombytes() once per thread and
        # sodium_memzero() once at the end, so no libsodium code runs inside
        # the per-key loop and its optimisation level does not affect mining.
        body = _expand_shell_vars(
            _strip_comments(_read(DMG_BUILD)).replace("\\\n", " "), ["MKP_OPT"])
        invocations = [
            line for line in body.splitlines()
            if "./configure" in line and "--host=" in line
            and "--prefix=" not in line
        ]
        self.assertTrue(
            invocations,
            "Could not find the mkp224o cross-compile configure calls in " +
            DMG_BUILD + " — renamed? Update this test rather than passing "
            "vacuously.",
        )
        for line in invocations:
            match = re.search(r'CFLAGS="([^"]*)"', line)
            with self.subTest(configure=line[:60]):
                self.assertIsNotNone(
                    match, "mkp224o configure call with no CFLAGS: " + line[:80])
                self.assertIn(
                    "-O3", match.group(1),
                    "Passing CFLAGS without -O3 silently disables optimisation "
                    "entirely; configure only supplies -O3 when CFLAGS is unset.",
                )

    def test_macos_binary_cache_key_tracks_the_flags(self):
        """The cache is keyed by mkp224o version, and these flags do not change
        the version. Without the key changing, a developer with a warm cache
        keeps shipping the binary built before this fix.
        """
        # MKP224O_VERSION is deliberately left unexpanded: the point is that
        # the key carries something *besides* the version, since these flags
        # change the binary without changing the version.
        text = _expand_shell_vars(_strip_comments(_read(DMG_BUILD)),
                                  ["MKP_CACHE_KEY"])
        keys = {k for k in re.findall(r'cache_(?:get|put) "([^"]+)" ', text)
                if "mkp224o" in k}
        self.assertTrue(keys, "No mkp224o cache key found in " + DMG_BUILD)
        for key in keys:
            with self.subTest(key=key):
                self.assertNotEqual(
                    key, "mkp224o-${MKP224O_VERSION}-universal",
                    "The pre-fix cache key is still in use, so a warm cache "
                    "serves the unoptimised binary built before this fix.",
                )
                self.assertIn(
                    "O3", key,
                    "The cache key must move when the optimisation flags move; "
                    "today it says so by name.",
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


class TestTorComesFromTheOfficialImage(unittest.TestCase):
    """Tor is whatever the pinned Onimages base carries. The Tor Project's own
    build sets up deb.torproject.org and verifies the archive key's
    fingerprint; this Dockerfile must not do either again, and must not
    reinstall tor on top — that would silently move the Tor version off the
    one the base digest names to whatever the mirror serves on build day.
    """

    def test_no_second_apt_source_or_key_fetch(self):
        code = _strip_comments(_read(TOR_DOCKERFILE))
        self.assertNotIn(
            "deb.torproject.org", code,
            "The Onimages base already configures deb.torproject.org with a "
            "verified keyring. A second copy here would be an unverified "
            "duplicate of that work.",
        )
        for needle in ("gpg --dearmor", "tor-archive-keyring", "TOR_APT_KEY_FPR"):
            self.assertNotIn(
                needle, code,
                f"{needle!r}: the apt-key handling moved upstream into the "
                "Tor Project's image build and must not come back here.",
            )

    def test_no_arti(self):
        """Arti was removed on 2026-09-24: it hosts a site acceptably but has
        no control interface, and sleep/wake, the watchdog's recovery and the
        OnionHeaven takeover pipeline are all built on the control port. Its
        key file format stays (OnionHeaven's wire format, delivered through
        the onionpress-onion-keys volume); the daemon must not come back unnoticed.
        """
        code = _strip_comments(_read(TOR_DOCKERFILE))
        self.assertNotIn(
            ONIMAGES + "/arti", code,
            "The tor Dockerfile must not pull the Tor Project's arti image.",
        )
        self.assertNotRegex(
            code, r"(?m)^COPY [^\n]*\barti\b",
            "No arti binary or config may be copied into the image.",
        )
        self.assertNotRegex(
            code, r"useradd[^\n]*\barti\b",
            "No arti user: nothing in the image runs as it.",
        )

    def test_tor_is_not_reinstalled(self):
        code = _strip_comments(_read(TOR_DOCKERFILE)).replace("\\\n", " ")
        for match in re.finditer(r"apt-get install\s+([^\n&]*)", code):
            packages = set(re.sub(r"\s-[-\w]+", " ", match.group(1)).split())
            with self.subTest(packages=sorted(packages)):
                self.assertNotIn(
                    "tor", packages,
                    "`apt-get install tor` in a derived stage upgrades Tor to "
                    "the mirror's current version and unpins it from the base "
                    "digest. Tor comes from the base image.",
                )
                self.assertFalse(
                    [p for p in packages if p.startswith("tor=")],
                    "The tor apt package must not be version-pinned either — "
                    "deb.torproject.org drops superseded versions within weeks.",
                )


class TestNoSilentlyFailingDownloads(unittest.TestCase):
    """`curl` without -f exits 0 on an HTTP error and writes the error body to
    the output file. Both network fetches in these images had that bug. The
    tor Dockerfile's fetch (the apt key) moved upstream, so today only the
    wordpress image downloads anything; the tor file is still scanned in case
    a download comes back.
    """

    def test_all_curl_downloads_use_fail_flag(self):
        for dockerfile in (TOR_DOCKERFILE, WP_DOCKERFILE):
            # Join line continuations first, then scan the WHOLE invocation.
            # Looking only at the first short-flag cluster missed
            # `curl --silent -o ...` entirely (no cluster to match, so the
            # loop body never ran and the test passed vacuously) and falsely
            # failed the correct `curl -sSL --fail ...`.
            body = _strip_comments(_read(dockerfile)).replace("\\\n", " ")
            # Anchored to a command position (start of line, after RUN, or
            # after &&/||/;/|). Without that, the bare word `curl` inside the
            # apt-get package list matched and the rest of the package names
            # were scanned as if they were curl flags.
            invocations = re.findall(
                r"(?:^|RUN\s+|&&\s*|\|\|\s*|;\s*|\|\s*)curl\s+(.*?)(?=\s+&&|\s*$)",
                body, re.M,
            )
            if dockerfile == WP_DOCKERFILE:
                self.assertTrue(
                    invocations,
                    f"No curl invocation found in {dockerfile} — renamed or "
                    "removed? Update this test rather than passing vacuously.",
                )
            for args in invocations:
                with self.subTest(dockerfile=dockerfile, args=args[:60]):
                    short = re.findall(r"(?:^|\s)-([A-Za-z]+)", args)
                    has_fail = "--fail" in args or any("f" in c for c in short)
                    self.assertTrue(
                        has_fail,
                        f"{dockerfile}: `curl {args[:70]}` lacks -f/--fail, so "
                        "an HTTP error page is written to the output file and "
                        "the build continues.",
                    )


if __name__ == "__main__":
    unittest.main()

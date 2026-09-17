# Building OnionPress locally

Every artifact OnionPress ships can be rebuilt on a developer's own machine.
This document is the map: what each component is, which command rebuilds it,
which host OS that command needs, and what it reaches out to the network for.

**"Local" here means buildable on your machine, not hermetic.** The builds
still fetch from upstream package sources — Debian, crates.io, Docker Hub,
the Tor Project apt repo. Vendoring those is out of scope and would not be
realistic for a WordPress + Tor stack. What *is* in scope is that nothing
requires access to the project's CI, its registry credentials, or its
self-hosted runner, and that the inputs are pinned so your build and the
published build are comparable.

Some steps genuinely require a particular host OS — the `.dmg` needs macOS
because it uses `swiftc`, `lipo`, `codesign`, `hdiutil` and `PlistBuddy`.
Those are called out per component.

## Contents

- [Container image pins](#container-image-pins)
- [Running a locally built stack](#running-a-locally-built-stack)
- [Building the container images](#building-the-container-images)

<!-- Sections for input pinning, generated assets and the unified entry
     points are added by the later phases of this work. -->

---

## Container image pins

`build/image-pins.env` is the single source of truth for which
`ghcr.io/brewsterkahle/onionpress-*` images the stack runs:

```
ONIONPRESS_TOR_IMAGE=ghcr.io/brewsterkahle/onionpress-tor:latest@sha256:…
ONIONPRESS_WORDPRESS_IMAGE=ghcr.io/brewsterkahle/onionpress-wordpress:latest@sha256:…
```

Five files embed those literals, so a bare `docker compose up` with no
environment set still works:

| Consumer | What it pins |
|---|---|
| `app/Resources/docker/docker-compose.yml` | the `tor`, `wordpress` and `onionheaven` services |
| `app/MacOS/onionpress` | `update_images()`, the OnionHeaven takeover worker |
| `linux/onionpress` | `update_images()` |
| `src/onionpress/containers.py` | `ONIONHEAVEN_IMAGE` — farm workers |
| `src/onionpress/launcher_ops.py` | `DEFAULT_TOR_IMAGE` — vanity-key generation |

`build/refresh-image-digests.sh` is the only thing that writes any of them.

### Why this is one file and not five

It used to be five independent copies kept in sync by remembering to run the
refresh script. v2.4.110 (commit `94ce1a36`, whose message says "refresh image
digests") updated `docker-compose.yml` alone. From that release until this
change, a single install ran **two different Tor builds**: compose started
`tor@sha256:1f98ac29…` while the Linux launcher, vanity-key generation and the
OnionHeaven farm workers all still used `tor@sha256:ecab8ad6…`. The Linux
launcher also pre-pulled the stale digest and logged `✓ up to date` about an
image the stack never started.

`tests/test_image_pins.py` now fails on any drift and names the offending
file, so this is no longer something you have to remember.

### Refreshing the pins at release time

```bash
docker pull ghcr.io/brewsterkahle/onionpress-tor:latest
docker pull ghcr.io/brewsterkahle/onionpress-wordpress:latest
build/refresh-image-digests.sh
git diff
```

Other modes, none of which need a Docker daemon:

```bash
build/refresh-image-digests.sh --check                   # CI / pre-commit: drift is an error
build/refresh-image-digests.sh --propagate               # repair: re-apply image-pins.env everywhere
build/refresh-image-digests.sh --tor sha256:… --wordpress sha256:…   # supply digests explicitly
```

Pins must be **multi-arch index digests** — what
`docker buildx imagetools inspect <tag>` reports for the tag. A
single-platform manifest digest resolves on one architecture and 404s on the
other, which on this project means "works on the maintainer's Mac, breaks on
every Intel machine".

### Why some images stay floating

`mariadb:latest` and `willfarrell/autoheal:latest` are deliberately **not**
pinned. We rely on their upstream registries shipping security patches
without us tracking digests, and neither is built by this project.

Three more references are deliberately left as bare tags, and
`tests/test_image_pins.py` asserts they stay that way — a blanket
"pin everything" sweep breaks each of them quietly at build time and
painfully at runtime:

- **`linux/onionpress`'s `docker image inspect …onionpress-tor:latest`.** This
  is how the launcher decides whether vanity-address generation is available.
  Pinned to a digest, a locally built image fails the check and the install
  silently falls back to a random `.onion` — the v2.4.101 regression.
- **`onionheaven_common.TAKEOVER_IMAGE`.** It runs *inside* the tor container,
  where the correct image is whichever one is already running. A digest baked
  in there would outlive the image containing it.
- **`tests/stress/Dockerfile`.** It `FROM`s the image CI just built, so any
  static digest is stale by construction.

---

## Running a locally built stack

Every consumer reads the pins as an *override with the pin as default*, so
exporting two variables repoints the entire stack — compose services, both
launchers, farm workers and vanity-key generation — at your own images:

```bash
export ONIONPRESS_TOR_IMAGE=onionpress-tor:dev
export ONIONPRESS_WORDPRESS_IMAGE=onionpress-wordpress:dev
```

Resolution order for the `onionheaven` service is
`ONIONHEAVEN_IMAGE` → `ONIONPRESS_TOR_IMAGE` → the pin, matching
`docker-compose.yml` and `containers.py` exactly so the menubar path and the
compose path can never disagree.

Verify interpolation without a daemon:

```bash
cd app/Resources/docker && docker compose config | grep image:
```

---

## Building the container images

```bash
build/build-images.sh              # tor + wordpress, native arch
build/build-images.sh tor          # just one
build/build-images.sh all          # adds the stress-test worker
build/build-images.sh --help       # every flag
```

| Image | Context | Cold build time |
|---|---|---|
| `onionpress-tor:dev` | `app/Resources/docker/tor` | **tens of minutes** — compiles arti from source |
| `onionpress-wordpress:dev` | `app/Resources/docker/wordpress` | seconds |
| `onionpress-stress-worker:dev` | `tests/stress` | seconds, chains off your local tor image |

Needs `docker` with `buildx`, and nothing else. These are Linux images, so
unlike the `.dmg` there is no host-OS requirement.

### Shadow tags, and why a local build also tags the GHCR name

Both launchers decide whether vanity-address generation is available with a
deliberately tag-only check:

```bash
docker image inspect ghcr.io/brewsterkahle/onionpress-tor:latest
```

A local build tagged only `onionpress-tor:dev` fails that check, and the
install silently falls back to a random `.onion` instead of an `op2…` vanity
address — the v2.4.101 regression. So a local build **also** tags the GHCR
name, pointing at your local image ID. It shadows the published image on your
machine until you `docker pull` again. `--no-shadow-tag` opts out.

### Architectures

By default you build for your host's native platform. Multi-arch needs
`--push`, because Docker cannot load a multi-platform result into the local
image store — a tag there resolves to exactly one manifest. The script refuses
that combination up front rather than failing at the end of a long build.

Cross-building the **tor** image is a QEMU-emulated Rust compile and takes
hours. CI avoids it entirely: `docker-publish.yml` builds amd64 on a
GitHub-hosted runner and arm64 on a self-hosted Apple Silicon Mac, then merges
the two with `docker buildx imagetools create`. For local work, build natively
for whatever you are on — that is what you run anyway.

A consequence worth knowing: because the two halves are built at different
times from unpinned upstreams, the amd64 and arm64 sides of a published
manifest can legitimately contain different Debian, Tor and arti versions.
Pinning the inputs is what makes them agree — see the next section.

### Build contexts are now filtered

Docker does not read `.gitignore`, so `__pycache__/` was invisible in
`git status` yet fully present in the build context — 544K of the tor
context's 964K. `COPY wordlists /wordlists` baked those `.pyc` files into the
published image, making that layer's cache key depend on whether the developer
had run the test suite. Two of them (`follow-fetch`, `wayback-static`) had no
`.py` source left in the repo at all.

Each context now has a `.dockerignore`. The same leak went into the Linux
`.deb` — 18 stale `.pyc` files — and `build/build-linux.sh` now strips them
too.

These directories come back after every test run, because
`tests/test_onionnames.py` and `tests/test_onionheaven_integration.py` put
`app/Resources/docker/tor` on `sys.path` and import from it. An exclude is the
fix; deleting them is not.

### CI coverage

`.github/workflows/build-images.yml` runs `build/build-images.sh` on any PR
touching a build context, so the local path cannot rot between releases. It is
amd64-only, never pushes, never touches the self-hosted Mac (it runs fork PRs),
and writes to a PR-scoped build cache so it cannot evict the release cache.

**`docker-publish.yml` was deliberately not rewired to call this script.** The
two CI paths differ in ways a shared script would have to absorb carefully —
the `type=gha` cache backend needs Actions runtime env vars that
`build-push-action` injects and a plain `run:` step does not; the self-hosted
runner reuses a long-lived builder while the hosted one gets a fresh empty one
each run; and the two paths have different default provenance behaviour, a
mismatch that already forced commit `419b53ec`. `build-images.sh` exposes every
flag that migration needs (`--platform`, `--push`, `--registry`, `--cache-from`,
`--cache-to`, `--provenance`, `--pull`) and the validation workflow exercises
them, but changing the release publisher is a supply-chain change that
[CONTRIBUTING.md](../CONTRIBUTING.md) reserves for the maintainer and that
cannot be tested without cutting a release.

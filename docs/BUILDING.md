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

<!-- Sections for the image builds, input pinning, generated assets and the
     unified entry points are added by the later phases of this work. -->

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

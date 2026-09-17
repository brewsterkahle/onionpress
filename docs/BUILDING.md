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
- [Pinned inputs](#pinned-inputs)
- [Running the stack you just built](#running-the-stack-you-just-built)
- [Generated assets](#generated-assets)

<!-- The unified entry points section is added by the last phase of this work. -->

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

---

## Pinned inputs

Every external input to the images is pinned to an immutable identifier. The
pins live as `ARG` defaults in the Dockerfiles themselves, next to what they
control — deliberately not in a separate manifest, because the repo already
has two pin surfaces (`build/bump-version.sh` for the app version,
`build/refresh-image-digests.sh` for the published image digests) and a third
would be one more thing to forget.

| Input | Where | Pinned as |
|---|---|---|
| Rust toolchain (arti builder) | tor | `ARG RUST_IMAGE` — tag + index digest |
| Debian (mkp224o builder + runtime) | tor | `ARG DEBIAN_IMAGE` — tag + index digest |
| Docker CLI | tor | `ARG DOCKER_CLI_IMAGE` — named stage, tag + digest |
| arti crate | tor | `ARG ARTI_VERSION` + `cargo install --version` |
| mkp224o | tor | `ARG MKP224O_VERSION` + `ARG MKP224O_COMMIT`, asserted after clone |
| Tor apt signing key | tor | `ENV TOR_APT_KEY_FPR`, fingerprint asserted |
| WordPress base | wordpress | `ARG WORDPRESS_IMAGE` — tag + index digest |
| wp-cli | wordpress | `ARG WP_CLI_VERSION` + `ARG WP_CLI_SHA256`, verified |
| tor image (stress worker) | tests/stress | `ARG TOR_IMAGE`, supplied by the builder |

`tests/test_dockerfile_pins.py` fails if any base image loses its digest, if
`cargo install arti` loses `--version`, if `MKP224O_COMMIT` stops being a full
SHA, if either supply-chain assertion is removed, or if a `curl` loses `-f`.

### The two supply-chain fixes

**wp-cli was an unverified download from a moving branch.** The old line
fetched `wp-cli.phar` from the `gh-pages` branch of `wp-cli/builds` with no
checksum and no `curl -f` — so an HTTP error page was written to
`/usr/local/bin/wp` and `chmod +x`'d. The failure then surfaced far away,
inside `onionpress-multisite-init.sh` or `onionpress-security-audit.sh`,
rather than at build time. It is now a tagged release asset whose published
sha256 is verified *before* the file is made executable.

**The Tor apt signing key was fetched but never checked.** The URL is *named*
after a fingerprint, which is not verification: nothing compared the fetched
key's actual fingerprint to that name, so a substituted key at that URL would
have been installed and trusted. `gpg --show-keys` now re-derives the
fingerprint from the fetched bytes and the build fails on mismatch.

`TOR_APT_KEY_FPR` is an `ENV`, not an `ARG`, and that distinction is
load-bearing: an `ARG` can be overridden with `--build-arg`, which would let a
builder point *both* the fetch and the assertion at the same substituted key —
a self-certifying check that proves nothing.

### What is deliberately not pinned

**The `tor` apt package.** `deb.torproject.org` removes superseded versions
from its mirror and publishes no snapshot service, so an apt version pin
becomes `E: Version '…' was not found` within weeks. The base-image digest is
the right granularity for the Debian package set, and the identifier users
actually consume is the published image digest in `build/image-pins.env`. The
signing-key assertion is what makes this safe.

**WordPress core, in practice.** The base image is pinned, but that does not
freeze WordPress for users: `onionpress-security-audit.sh` runs on every
container start and applies pending core security releases over Tor. The pin
fixes what this image is *built* on; the running site patches itself.

### Bumping a pinned input

Resolve the new **multi-arch index digest** — not a per-platform manifest
digest, which resolves on amd64 and 404s on the arm64 builder:

```bash
docker buildx imagetools inspect rust:1.99-trixie
```

Without a daemon, the registry API works too:

```bash
TOKEN=$(curl -s "https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/rust:pull" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
curl -sI -H "Authorization: Bearer $TOKEN" \
     -H "Accept: application/vnd.oci.image.index.v1+json" \
     https://registry-1.docker.io/v2/library/rust/manifests/1.99-trixie \
  | grep -i docker-content-digest
```

Then edit the `ARG` default and rebuild. Two coupling rules:

- **`ARTI_VERSION` and `RUST_IMAGE` move together.** arti requires a minimum
  Rust version; bumping one alone fails about twenty minutes into a
  release-mode compile.
- **`MKP224O_VERSION` must match `build/build-dmg-simple.sh`.** That script
  cross-compiles the same mkp224o release as a universal macOS binary. Two
  different versions minting vanity addresses for the same project is a
  difference nobody notices until the outputs differ. A test enforces this.

Do **not** switch the Rust builder to a `-slim` variant. arti's default
features resolve `default` → `default-runtime` → `native-tls` → `openssl-sys`,
which needs `pkg-config` and `libssl-dev`; `rust:slim-trixie` ships only
`ca-certificates`, `gcc` and `libc6-dev`. The full variant is
`FROM buildpack-deps:trixie` and has them. The size difference is irrelevant —
it is a builder stage, discarded once the arti binary is copied out.

---

## Running the stack you just built

```bash
build/build-images.sh     # produce onionpress-{tor,wordpress}:dev
build/dev-up.sh           # start the stack on them
build/dev-up.sh --logs
build/dev-up.sh --down
```

### Why building was only half the job

The running app used to pull over whatever you built. Three separate paths did
it, and the important one was not the opt-in one:

| Path | Gated by `UPDATE_ON_LAUNCH`? |
|---|---|
| `update_images()` in both launchers | yes |
| `docker compose pull` on every start | **no** — ran unconditionally |
| `docker compose up --pull always tor` (`start-tor`) | **no** — force-pulls |
| `docker compose pull` in `onionpress update` (Linux) | **no** |
| `update_docker_images()` in `src/menubar.py` | separate path again |

So you could build an image, launch the app, and silently test someone else's
build. Nothing failed; the stack just was not running your code.

All of them now check `using_local_images()` — true when
`ONIONPRESS_TOR_IMAGE` or `ONIONPRESS_WORDPRESS_IMAGE` points at a reference
that is *not* on `ghcr.io`. A `ghcr.io` reference is a deliberate pin, not a
local build, so pulls keep working normally for everyone else.

The predicate exists in three places — `app/MacOS/onionpress`,
`linux/onionpress` and `onionpress.containers.using_local_images()` — because
the bash CLI, the Linux launcher and the macOS menubar are parallel
implementations. `tests/test_local_image_mode.py` checks all three agree and
that no pull escapes the guard.

### Pointing an installed app at local images

Add to `~/.onionpress/config` (the keys are in the shipped config template,
commented out):

```
ONIONPRESS_TOR_IMAGE=onionpress-tor:dev
ONIONPRESS_WORDPRESS_IMAGE=onionpress-wordpress:dev
```

Both launchers read these at startup and export them, so compose, the pull
gating and vanity-key generation all follow.

### One stack per machine

`docker-compose.yml` hardcodes `container_name:` and `volumes: name:`, so a
"dev" stack is not isolated from an installed OnionPress — it takes the same
containers and volumes over. `build/dev-up.sh` refuses to start when an
OnionPress stack is already running rather than fighting it; `--force`
overrides.

For the same reason `--down` never passes `-v`. Those volumes hold the
database, the WordPress content and the onion service keys.

---

## Generated assets

Four families of binary were committed with nothing in the repo that could
produce them. Each now has a recipe, and the recipes differ in how much they
guarantee — that difference is the point of this section.

| Artifact | Command | Guarantee |
|---|---|---|
| `app/Resources/app-icon.png` | `build/make-icons.sh` | **byte-identical** |
| `app/Resources/AppIcon.icns` | `build/make-icons.sh` | **byte-identical** |
| `app/Resources/menubar-icon-*.png` | `build/make-icons.sh` (needs ImageMagick) | equivalent, reconstructed |
| `build/dist/onionpress-firefox.xpi` | `build/build-extension.sh` | reproducible across machines |
| `build/dist/onionpress-chrome.zip` | `build/build-extension.sh` | reproducible across machines |
| `build/dmg-assets/dmg-background.png` | `build/create-dmg-background.py` | equivalent, not identical |
| `build/dmg-assets/DS_Store` | — | a capture, not a build |

```bash
build/make-icons.sh --verify    # rebuild to a temp dir and diff; writes nothing
```

### App icons

macOS only (`sips`, `iconutil`). Two sharp edges, both guarded by tests:

- **`sips -z`, not `sips -Z`.** The master is 992x1072 and is deliberately
  squashed to a square 1024x1024. "Fixing" that to `-Z 1024` yields 948x1024
  and silently changes every icon layer and the `.icns`.
- **Never build the iconset by exporting the existing `.icns`.**
  `iconutil --convert iconset` and back is lossy: the `ic04`/`ic05` layers are
  raw ARGB and get re-encoded, producing a file of identical length that
  differs in two bytes. Build from the PNG master.

The three menubar PNGs are the exception. They were made with **ImageMagick**,
not `sips` — their embedded `tEXt` chunks say so, and
`exif:PixelXDimension 761` pins the source to
`assets/branding/icon-menubar.png`. The recipe is reconstructed from that
metadata and from the pixel relationships between the files; it produces
equivalent icons, not identical bytes, and it has not been run against the
committed files. Use `-grayscale Rec709Luma`, not `-colorspace Gray`:
ImageMagick 7's `-colorspace Gray` is gamma-aware and does not match.

### Browser extensions

```bash
build/build-extension.sh          # both
build/build-extension.sh firefox  # one
```

There are **two Firefox manifests in the tree and they are not equivalent**:

| | `extension/manifest.firefox.json` | `extension-firefox/manifest.json` |
|---|---|---|
| Version | 1.0.0, min Firefox 109 | 1.1.0, min Firefox 142 |
| Permissions | + `webRequest`, `webRequestBlocking`, `webNavigation`, `<all_urls>` | narrow |
| Content script | yes | no |
| `data_collection_permissions` | **absent** | present |

The build uses `extension-firefox/`. Building from `manifest.firefox.json`
would re-request four permissions the extension no longer needs and produce a
package addons.mozilla.org rejects — AMO requires
`data_collection_permissions` from Firefox 142.

`extension-firefox/` holds only the files that differ (`manifest.json`,
`offline.html`, `offline.js`); icons, popup and background come from
`extension/`. `offline.html` is deliberately different: `extension/`'s uses an
inline `<script>`, which violates the extension CSP, and `extension-firefox/`
externalises it to `offline.js`. The build fails if the manifest references a
file that is not in the package.

`.gitignore` used to list `extension-firefox/` while four of its files were
tracked. Ignore rules do not apply to already-tracked files, so the entry was
inert for those four — but any *new* file added there would have been silently
untracked, in the directory holding the current manifest. That entry is gone.

**The committed `build/onionpress-firefox.xpi` is not what this script
produces, and cannot be.** Its inputs no longer exist: its `background.js`
matches neither current source (it is an older revision), and its
`manifest.json` appears in **no commit in this repository**. It is v1.0.0
where the sources are v1.1.0. Nothing in the repo references it. It is left in
place rather than deleted, because whether to replace a distributable that may
already be published is the maintainer's call — but it should not be treated
as the output of any build.

### DMG window assets

See [`build/dmg-assets/README.md`](../build/dmg-assets/README.md).
`dmg-background.png` is regenerable from branding sources (needs Pillow) but
only equivalently — it depends on the installed Pillow's resampling and on
system font rasterisation.

`DS_Store` cannot be regenerated by a script at all. It is a Finder capture
encoding volume creation dates, file IDs and an Alias blob, and the committed
one embeds a build machine's home directory path
(`/Users/brewster/tmp/onionpress/…`), which therefore ships inside every
published DMG. Recapturing on another machine substitutes that machine's path
rather than removing it. Its filename has **no leading dot** on purpose: the
repo ignores `.DS_Store`, so renaming it makes git drop it silently and the
next DMG loses all window styling with no error.

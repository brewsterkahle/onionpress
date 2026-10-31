# Building OnionPress locally

Every artifact OnionPress ships can be rebuilt on a developer's own machine.
This document is the map and the reasoning: what each component is, which
command rebuilds it, which host OS that command needs, what it reaches out to
the network for — and why each of those is the way it is.

**If you just want to build something, start with
[HOW-TO-BUILD.md](HOW-TO-BUILD.md)** — requirements, install commands and
steps, without the history.

**"Local" here means buildable on your machine, not hermetic.** The builds
still fetch from upstream package sources — Debian, Docker Hub, the Tor
Project's container registry and apt repo. Vendoring those is out of scope and would not be
realistic for a WordPress + Tor stack. What *is* in scope is that nothing
requires access to the project's CI, its registry credentials, or anyone's
self-hosted runner, and that the inputs are pinned so your build and the
published build are comparable. The CI holds to the same rule: the publish
workflow runs entirely on GitHub-hosted runners and publishes to the
namespace of whichever repository runs it, so a fork produces the full set
of images without editing it — see [CI coverage](#ci-coverage).

Some steps genuinely require a particular host OS — the `.dmg` needs macOS
because it uses `swiftc`, `lipo`, `codesign`, `diskutil image` and `PlistBuddy`.
Those are called out per component.

## Contents

- [Container image pins](#container-image-pins)
- [Running a locally built stack](#running-a-locally-built-stack)
- [Building the container images](#building-the-container-images)
- [Pinned inputs](#pinned-inputs)
- [Running the stack you just built](#running-the-stack-you-just-built)
- [Integration tests](#integration-tests)
- [Generated assets](#generated-assets)
- [Quick reference](#quick-reference)


## Quick reference

```bash
make doctor        # which build tools you have, and what each missing one costs
```

| You want | Command | Host OS | Reaches out to |
|---|---|---|---|
| tor + wordpress images | `make images` | any | containers.torproject.org, Docker Hub, Debian, deb.torproject.org, GitHub |
| run the stack on them | `make dev-up` | any | — |
| macOS installer (`.dmg`) | `make dmg` | **macOS** | GitHub releases, Docker, python.org/PyPI, libsodium |
| Linux package (`.deb`) | `make deb` | any | — |
| browser extensions | `make extension` | any | — |
| app icons | `make icons` | **macOS** | — |
| unit tests | `make test-unit` | any | PyPI (PyNaCl, via uv) |
| unit tests with coverage | `make coverage` | any | PyPI (PyNaCl, coverage, via uv); floors in `build/coverage-floors.sh` |
| integration tests | see [Integration tests](#integration-tests) | any — **never a machine that runs OnionPress** | — |
| source layout + pin check | `make test` | any | — |

`make deb` and `make extension` need no network at all. The `.dmg` is
macOS-only because it uses `swiftc`, `lipo`, `codesign`, `diskutil image` and
`PlistBuddy`; note that `.deb` is *not* Linux-only — `build/build-linux.sh`
hand-assembles the `ar` archive in Python when `dpkg-deb` is absent. Either
way the package records root as the owner of every file (dpkg installs the
owner it finds in `data.tar`) and, on macOS, `COPYFILE_DISABLE` keeps bsdtar
from adding an AppleDouble `._` member per file. `build/check-deb.sh` (Linux:
it reads the package with `dpkg-deb`; `--lintian` runs lintian too) checks a
built package for exactly the things the Mac fallback got wrong before
October 2026; CI runs it on every build (see [CI coverage](#ci-coverage)).

### What has been proven by actually running it

Every row above has been executed on a developer machine, not just reasoned
about. So you know what you are trusting:

| Artifact | Proven |
|---|---|
| `.deb` | builds on macOS via the pure-Python `ar` fallback; ships 0 stale `.pyc` (was 18). Rehearsed in a `debian:trixie` container with the real `dpkg-deb` and lintian 2.122 (2026-10-14): `build/check-deb.sh --lintian` passes; the v2.5.3 release's package, built on the Mac before the fixes, had all 430 members owned by uid 502 and 215 AppleDouble `._` files, a `Description` line starting with `.onion`, a folded `Depends` and an unlisted conffile |
| `.dmg` | full path — pinned binary downloads, libsodium + mkp224o cross-compile (universal), the bundled binaries' license texts into `Contents/Resources/THIRD-PARTY-NOTICES.txt` (out of the release archives or from the pinned tags, cached under `build/.cache/licenses/`; a missing text fails the build), py2app, then the interpreter's and the bundled Python packages' texts into the same file (`build/python-notices.py`, from the build venv's `*.dist-info`; run against the real wheels' metadata from Linux so far, not yet inside a `make dmg`), signing, `diskutil image` (and the `hdiutil` fallback, forced with `DMG_TOOL=hdiutil`). 161 MB, version verified; ships 0 stale `.pyc` (was 31 when built right after `make test-unit`). **Dev-grade**: with `uv` the bundled Python is arm64-only; release-grade needs the python.org universal2 3.14 installer |
| `onionpress-wordpress` image | builds with the classic builder; wp-cli 2.12.0 with the pinned sha256 in the image; a wrong `WP_CLI_SHA256` fails at `sha256sum -c` **before** `chmod +x`; a wrong base digest fails at `FROM` |
| `onionpress-tor` image | builds on the Tor Project's Onimages `tor:trixie` image in under a minute once it is pulled (isolated Colima VM, classic builder — nothing but mkp224o is compiled); baked in: Tor 0.4.9.13, Docker 29.8.1, mkp224o v1.7.0 against the base's libsodium; image user root, `CMD []`; the entrypoint bootstraps Tor in onion-service, SOCKS-only and takeover-worker modes and converts a delivered PEM key to C Tor's files; the stress worker chains off it; 0 `.pyc` under `/wordlists`; a wrong `MKP224O_COMMIT` fails at the post-clone assert |
| `AppIcon.icns`, `app-icon.png` | byte-identical to the committed files |
| menubar PNGs | `running`, `starting` pixel-identical; `stopped` within 2/255 (see [Generated assets](#generated-assets)) |
| extensions | byte-reproducible across runs |
| integration tests | all four suites (81 tests) pass against a disposable stack set up as in [What to run them against](#what-to-run-them-against) — image entrypoint, the minute's wait, no tor — on GitHub-hosted runners in a fork (`ivar/onionpress`, run 36817968776), in about 9 min, leaving only the root site behind; the suites as they were before the guard left three sandbox subsites (run 36817057114). With `/var/lib/onionpress/onion_address` in the container, or one extra subsite, the guard refused before creating anything |
| `linux-package.yml` | ran in a fork (`ivar/onionpress`, run 38631665798, 2026-10-14, `workflow_dispatch`): the `.deb` built with `dpkg-deb` 1.22 and passed `check-deb.sh --lintian` (lintian 2.117) in 38 s; `install.sh` ran twice on `ubuntu-latest` in 7 min 15 s — rootless Docker set up by the installer itself, images pulled, the onion service reachable after the first run, the second run's tree identical to the checkout with no nested folder, all three user units enabled, WordPress answering on the launcher's port (18080: the runner already has a listener on 8080, so the launcher took the next offset, which is also why the installer's own 8080 wait reported no answer). The release job was skipped, as it must be outside a release |
| `docker-publish.yml` | ran end to end in a fork (`ivar/onionpress`, run 36064530531, 2026-09-24, on the C Tor-only image) on GitHub-hosted runners only — amd64 on `ubuntu-latest`, arm64 on `ubuntu-24.04-arm`. All three images published under the fork's own namespace as OCI indexes carrying `linux/amd64` and `linux/arm64`; the stress worker's `FROM` resolved to the tor index the same run had merged seconds earlier. 2 min 12 s end to end; tor 58 s (amd64) / 55 s (arm64). The previous run on the arti-compiling image (35479901478, 2026-09-20) took 8 min 50 s |

**The disk-image step now uses `diskutil image`.** On macOS 27 the
`hdiutil create`, `hdiutil attach` and `hdiutil convert` calls the DMG build
used to make each printed a deprecation warning pointing at `diskutil image`
(and `hdiutil(1)` now tables every verb's replacement, `detach` → `diskutil
eject` included). `build/build-dmg-simple.sh` was ported: `create from
--format RAW --volumeName OnionPress` for the read-write image, `diskutil
image attach`, `diskutil eject`, and `create from --format UDZO` for the
compressed result. `diskutil image` needs macOS 26 or later; on older hosts
the script falls back to the hdiutil verbs by itself, and `DMG_TOOL=hdiutil`
forces that path so it stays testable on a current macOS, where hdiutil still
works.

The two paths were compared by building both ways on macOS 27 and mounting
the results side by side:

- both mount at `/Volumes/OnionPress` as APFS (hdiutil defaults to APFS on
  macOS 27 as well) with `OnionPress.app`, the `Applications` symlink and
  `.background/dmg-background.png`;
- the app's file contents and permissions are identical (`diff -r`, plus a
  digest over every entry's mode and name), and `codesign --verify --deep
  --strict` passes on both;
- `.DS_Store` is byte-identical to the committed capture, and its background
  alias — resolved the way Finder resolves it, alias record → bookmark → URL —
  lands on `/Volumes/OnionPress/.background/dmg-background.png` on both
  volumes, so the window styling survives;
- both are `UDZO` with a CRC32 master checksum, and `hdiutil attach` still
  mounts the diskutil-built image — which matters because the in-app updater
  (`src/onionpress/updater.py`) mounts downloaded DMGs with `hdiutil attach`
  and must keep working on macOS 13–25, where `diskutil image` does not exist;
- the one difference: `diskutil image` exposes no zlib level, so its image is
  ~1.3 MB (0.8%) larger than hdiutil's `-imagekey zlib-level=9` output. The
  read-write image also has no `-size`: diskutil sizes it itself and leaves
  ~5% free (18 MB on a 380 MB payload), ample for the 10 KB `.DS_Store`
  written after mounting.

**Building the tor image without disturbing a running OnionPress.** Its VM
is your live site, and at 1 GB it is sized for running the stack. The tor
build is light now that Tor comes from the Tor Project's image (apt plus a
short mkp224o compile), but build beside the live VM, not in it:
run a second, isolated Colima instance under its own home; nothing under
`~/.onionpress` is touched, and deleting the directory reclaims everything:

```bash
export PATH="/Applications/OnionPress.app/Contents/Resources/bin:$PATH"
export COLIMA_HOME="$HOME/.colima-build"
export LIMA_HOME="$COLIMA_HOME/_lima"
export DOCKER_CONFIG="$COLIMA_HOME/docker-config"
export DOCKER_HOST="unix://$COLIMA_HOME/default/docker.sock"
colima start --cpu 6 --memory 8 --disk 20 --ssh-config=false  # first time: downloads a ~200 MB VM image
build/build-images.sh tor                                      # under a minute once the base image is pulled
colima stop                                                    # frees the RAM; the layer cache stays
```

`--ssh-config=false` stops Colima adding an `Include` line for this VM to your
`~/.ssh/config`, which deleting the directory would leave behind.

---

## Container image pins

`build/image-pins.env` is the single source of truth for which
images the stack runs — the two this project builds and the four
third-party ones:

```
ONIONPRESS_TOR_IMAGE=ghcr.io/brewsterkahle/onionpress-tor:latest@sha256:…
ONIONPRESS_WORDPRESS_IMAGE=ghcr.io/brewsterkahle/onionpress-wordpress:latest@sha256:…
MARIADB_IMAGE=mariadb:13.0.2@sha256:…
AUTOHEAL_IMAGE=willfarrell/autoheal:latest@sha256:…
CLOUDFLARED_IMAGE=cloudflare/cloudflared:2026.10.0@sha256:…
SOCKET_PROXY_IMAGE=ghcr.io/wollomatic/socket-proxy:1.13.1@sha256:…
```

Six files embed those literals, so a bare `docker compose up` with no
environment set still works:

| Consumer | What it pins |
|---|---|
| `app/Resources/docker/docker-compose.yml` | the `tor`, `wordpress` and `onionheaven` services, the `ONIONHEAVEN_IMAGE` the last one hands its takeover workers, and `db`, `autoheal`, `autoheal-proxy` |
| `app/Resources/docker/docker-compose.cloudflare.yml` | `cloudflared` |
| `app/MacOS/onionpress` | `KEY_HELPER_IMAGE`, the tor image its one-shot key helpers run |
| `linux/onionpress` | `KEY_HELPER_IMAGE`, the tor image its one-shot key helpers run |
| `src/onionpress/containers.py` | `ONIONHEAVEN_IMAGE` — farm workers |
| `src/onionpress/launcher_ops.py` | `DEFAULT_TOR_IMAGE` — vanity-key generation |

`build/refresh-image-digests.sh` is the only thing that writes any of them.

The Mac bundle's own inputs are pinned by hash too (issue #387): the
`*_SHA256_*` constants at the top of `build/build-dmg-simple.sh` are what
each download must hash to (`fetch_checked` refuses anything else), and the
Python packages come from `build/requirements-macos.lock` and
`build/build-requirements-macos.lock` with pip's `--require-hashes`
(Pillow, for the DMG background fallback only, from
`build/dmg-background-requirements-macos.lock`, installed after py2app so
it never sits in the venv py2app bundles).
`build/refresh-python-locks.sh` regenerates the locks (uv, network); a new
Colima, Lima, Docker CLI, Compose, libsodium or mkp224o version means
putting its published sum (or `shasum -a 256` of a fresh download, for the
two that publish none) and, for mkp224o, the tag's commit into the script.

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
build/refresh-image-digests.sh     # pins what GHCR serves as :latest right now
git diff
```

The digests come from the registry (`build/base-image-digest.sh`, no Docker
daemon needed). Until issue #327 they came from `docker images --digests`,
i.e. whatever the release machine had last pulled, and that pinned stale
images during the 2.5.3 release.

That same run also re-resolves the four third-party tags over the registry
API (`build/base-image-digest.sh`, no daemon): Docker Hub rebuilds a version
tag such as `mariadb:13.0.2` for base-OS patches, and this is how those reach
a release. Moving one to a new tag is an argument:

```bash
build/refresh-image-digests.sh --mariadb mariadb:13.0.3
build/refresh-image-digests.sh --cloudflared cloudflare/cloudflared:2026.11.0
build/refresh-image-digests.sh --socket-proxy ghcr.io/wollomatic/socket-proxy:1.14.0
build/refresh-image-digests.sh --autoheal willfarrell/autoheal:latest   # it publishes no version tags
```

Other modes (only `--local` needs a Docker daemon):

```bash
build/refresh-image-digests.sh --check                   # CI / pre-commit: drift is an error
build/refresh-image-digests.sh --propagate               # repair: re-apply image-pins.env everywhere
build/refresh-image-digests.sh --tor sha256:… --wordpress sha256:…   # supply the ghcr.io digests explicitly
build/refresh-image-digests.sh --local                   # this machine's pulled ghcr.io images (needs a daemon)
```

Publishing the GitHub release does **not** rebuild the images. On
`release: published`, `docker-publish.yml` only points `:latest` and
`:<version>` at the digests in the release's `build/image-pins.env`, then
checks that both tags resolve to exactly those digests. Images are built only
from "Run workflow". Until #327 a release rebuilt everything and moved both
tags off the images it pinned and tested (2.5.2 and 2.5.3 both shipped that
way).

Pins must be **multi-arch index digests** — what
`docker buildx imagetools inspect <tag>` and `build/base-image-digest.sh`
report for the tag. A single-platform manifest digest resolves on one
architecture and 404s on the other, which on this project means "works on
the maintainer's Mac, breaks on every Intel machine". The script refuses
one for any image (with `--local`, the two ghcr.io digests come from
`docker images --digests`, which is the index for a multi-arch GHCR tag).

### The third-party images are pinned too

`mariadb`, `willfarrell/autoheal`, `cloudflare/cloudflared` and the socket
proxy used to float on `:latest` (through 2.5.3), on the theory that upstream
would ship security patches without us tracking digests. What that bought in
practice: a registry could swap any of them under every install, and every
macOS launch pulled them over clearnet — DNS and registry traffic naming the
images, each time, for nothing new ([issue #36](https://github.com/brewsterkahle/onionpress/issues/36)).
Now a patch reaches users the same way a tor or wordpress change does: a
release moves the pin, and the next launch's `--policy missing` pull fetches
it. Re-pin them before every release (the refresh does it).

**mariadb pins move forward only.** The data directory follows whatever
version has run on it (`MARIADB_AUTO_UPGRADE=1`), and an older server
refuses a newer directory. The refresh script rejects a lower version. Until
this pin existed every launch pulled `mariadb:latest`, so the first pin was
what `:latest` resolved to that day; a release must never pin lower than
`:latest` was at the previous one.

autoheal no longer holds the Docker socket. It reaches Docker only through
`autoheal-proxy` (`ghcr.io/wollomatic/socket-proxy`), the container that does
hold the socket — pinned from `build/image-pins.env` like the rest. The
proxy passes only the two calls autoheal's script makes; if a new autoheal
calls anything else, the proxy refuses it and logs a WARN line
(`docker logs onionpress-autoheal-proxy`). See `docs/DOCKER-SOCKET.md`.
autoheal also runs with every capability dropped: it reaches the proxy's
0600 socket as its owner, uid 0. A new autoheal that ran as another user,
or needed a capability, would fail to reach the proxy (see
`docker logs onionpress-autoheal`).

The other container with the socket is `onionheaven-farm`, the OnionHeaven
farm controller: the tor image's own `onionheaven-farm.py`, so it is pinned
with the tor image. It starts and drives the takeover workers for the
`onionheaven` container, which has no socket; `docker logs onionheaven-farm`
shows every request and refusal. See `docs/DOCKER-SOCKET.md`.

Two more references are deliberately left as bare tags, and one carries no
image reference at all; `tests/test_image_pins.py` asserts they stay that
way — a blanket "pin everything" sweep breaks each of them quietly at build
time and painfully at runtime:

- **`linux/onionpress`'s `docker image inspect …onionpress-tor:latest`.** This
  is how the launcher decides whether vanity-address generation is available.
  Pinned to a digest, a locally built image fails the check and the install
  silently falls back to a random `.onion` — the v2.4.101 regression.
- **`onionheaven_common.takeover_image()`.** It runs *inside* the tor
  container, where the correct image is whichever one is already running. A
  digest baked in there would be one image behind, since it ships inside the
  image it names, and a bare tag names whatever is tagged locally or whatever
  the registry serves that day. So `docker-compose.yml` passes the
  `onionheaven` service's own image reference in as `ONIONHEAVEN_IMAGE`, and
  without it the code asks Docker for its own container's image ID; with
  neither it spawns no worker.
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
| `onionpress-tor:dev` | `app/Resources/docker/tor` | about a minute, plus a one-time ~260 MB pull of the Tor Project base image; only mkp224o is compiled |
| `onionpress-wordpress:dev` | `app/Resources/docker/wordpress` | seconds |
| `onionpress-stress-worker:dev` | `tests/stress` | seconds, chains off your local tor image |

Needs `docker`, and nothing else. These are Linux images, so unlike the
`.dmg` there is no host-OS requirement.

`buildx` is optional for a plain local build — the script falls back to the
classic builder without it. That matters on macOS: OnionPress bundles its own
docker CLI at `Contents/Resources/bin/docker` with **no buildx plugin** (the
launcher links only `docker-compose` into `cli-plugins`), so requiring buildx
would lock you out of the toolchain the app itself ships. `--platform`,
`--push` and `--cache-*` are buildx-only and the script says so if you ask for
them without it.

To build against the app's own Colima VM:

```bash
export PATH="/Applications/OnionPress.app/Contents/Resources/bin:$PATH"
export COLIMA_HOME="$HOME/.onionpress/colima"
export LIMA_HOME="$COLIMA_HOME/_lima"
export DOCKER_CONFIG="$HOME/.onionpress/docker-config"
export DOCKER_HOST="unix://$COLIMA_HOME/default/docker.sock"
colima start --ssh-config=false
```

That VM is sized for *running* the stack (1 GB RAM by default) and it is your
live site. The build is light now that nothing but mkp224o is compiled here,
but prefer the isolated instance described above all the same.

### Shadow tags, and why a local build also tags the GHCR name

The **Linux** launcher decides whether vanity-address generation is available
with a deliberately tag-only check:

```bash
docker image inspect ghcr.io/brewsterkahle/onionpress-tor:latest
```

A local build tagged only `onionpress-tor:dev` fails that check, and the
install silently falls back to a random `.onion` instead of an `op2…` vanity
address — the v2.4.101 regression. So a local build **also** tags the GHCR
name, pointing at your local image ID. It shadows the published image on your
machine until you `docker pull` again. `--no-shadow-tag` opts out.

macOS never consults Docker for this. It runs the bundled native
`$BIN_DIR/mkp224o` from inside the `.app`, and falls back to a random address
only if that binary is missing — which is why `build-dmg-simple.sh` aborts the
DMG rather than warning when the mkp224o cross-compile fails. The shadow tag
is a Linux concern.

### Architectures

By default you build for your host's native platform. Multi-arch needs
`--push`, because Docker cannot load a multi-platform result into the local
image store — a tag there resolves to exactly one manifest. The script refuses
that combination up front rather than failing at the end of a long build.

Cross-building the **tor** image runs its apt steps and the mkp224o compile
under QEMU — hours back when it also compiled arti, still slow. CI avoids it
entirely: `docker-publish.yml` builds amd64 on
`ubuntu-24.04` and arm64 on `ubuntu-24.04-arm` — both GitHub-hosted, both
free for public repositories — then merges the two with
`docker buildx imagetools create`. Until September 2026 the arm64 half ran on
the maintainer's own Mac as a self-hosted runner, so every image release
depended on that one machine being online and nobody else could produce the
arm64 images at all. For local work, build natively for whatever you are on —
that is what you run anyway.

A consequence worth knowing: because the two halves are built at different
times, the amd64 and arm64 sides of a published manifest could contain
different Debian and Tor versions if the inputs were not pinned. Pinning them
is what makes the halves agree — see the next section.

### Build contexts are now filtered

Docker does not read `.gitignore`, so `__pycache__/` was invisible in
`git status` yet fully present in the build context — 544K of the tor
context's 964K. `COPY wordlists /wordlists` baked those `.pyc` files into the
published image, making that layer's cache key depend on whether the developer
had run the test suite. Two of them (`follow-fetch`, `wayback-static`) had no
`.py` source left in the repo at all.

Each context now has a `.dockerignore`. The same leak went into the Linux
`.deb` — 18 stale `.pyc` files — and the Linux tree is now staged by
`linux/install-tree.sh` (the .deb's, install.sh's and
`onionpress update --from-checkout`'s one file list), which strips them. It
also went into the `.dmg`: `build/build-dmg-simple.sh` copies
`app/Resources/` and `src/onionpress/` with `cp -R`, so a DMG built right
after `make test-unit` carried 31 `.pyc` files a clean-tree build did not (24
under the py2app bundle's `onionpress/__pycache__/`, 7 under `docker/tor/`).
The script now strips `__pycache__/` and stray `*.pyc` from every tree it
copies out of the checkout, and sweeps `__pycache__/` from the assembled bundle
once more before signing. It deliberately does *not* sweep `*.pyc` bundle-wide:
py2app byte-compiles `site.py` into the MenubarApp's `Contents/Resources/` on
purpose. `tests/test_dmg_pycache.py` guards all of it.

These directories come back after every test run, because
`tests/test_onionnames.py` and `tests/test_onionheaven_integration.py` put
`app/Resources/docker/tor` on `sys.path` and import from it. An exclude is the
fix; deleting them is not.

### CI coverage

`.github/workflows/build-images.yml` runs `build/build-images.sh` on any PR
touching a build context, so the local path cannot rot between releases. It is
amd64-only, never pushes, runs fork PRs only on a disposable GitHub-hosted
runner with a read-only token, and writes to a PR-scoped build cache so it
cannot evict the release cache.

`.github/workflows/docker-publish.yml` is the release publisher, and it needs
nothing outside GitHub. Every job runs on a GitHub-hosted runner, and the
image namespace comes from `github.repository_owner`, so a fork runs the same
file and publishes `ghcr.io/<you>/onionpress-*` — "Run workflow" in the fork's
Actions tab does it. The stress worker's base is passed as the `TOR_IMAGE`
build-arg, so the worker extends the tor image the run itself just merged
rather than the published one. `tests/test_publish_workflow.py` fails the
build if a job moves off a hosted runner, an image reference bypasses the
namespace or prefix variables, an account name is hardcoded, or the stress
worker stops receiving its base. GHCR creates a package private on first
push; make it public in the package settings if anything must pull it
anonymously.

`.github/workflows/linux-package.yml` builds the `.deb` on `ubuntu-24.04`
with the real `dpkg-deb` on every push and pull request, runs
`build/check-deb.sh --lintian` on it (root ownership, executable plugin
scripts, the `python3-nacl (>= 1.5.0)` dependency, no AppleDouble litter,
lintian errors beyond an accepted list) and keeps it as a run artifact. On a
*published* release it attaches the package to that release — `onionpress.deb`
over the Mac-built copy `build/release.sh` uploaded, plus
`onionpress_<version>_all.deb` — with a build-provenance attestation from
`actions/attest-build-provenance`, so anyone can check a download. It also
rewrites the `onionpress.deb` line of the release's `SHA256SUMS` (keeping the
`.dmg`'s) and reads both back, since Linux's `onionpress update` installs only
a `.deb` that matches it:

```bash
gh attestation verify onionpress.deb --repo brewsterkahle/onionpress
```

The Mac still creates the release and uploads its own `.deb` first: "Latest"
must never lack a `.deb` (the README links
`releases/latest/download/onionpress.deb`), and a CI failure must not leave
it that way. The deb job also refuses a release whose tag is not
`v<src/menubar.py version>`. When a pull request touches `linux/`, a second
job runs `linux/install.sh` twice on the runner, like a user re-running the
README's `curl | bash` line to update, and checks that the second run
replaced the first (no `docker/docker` or `lib/onionpress/onionpress`, the
nested trees of issue #316 that `linux/install-tree.sh` ended), that the
installed trees match the checkout, that the user units are enabled and that WordPress answers on
port 8080. Nothing in the workflow names this repository or a secret; the
attestation signs with the run's OIDC identity and the upload goes to the
repository the run belongs to, so a fork's release gets its own attested
`.deb`. Only the release job, which never runs for a pull request, has write
access. `tests/test_linux_package_workflow.py` enforces all of this.

**`docker-publish.yml` was deliberately not rewired to call `build-images.sh`.**
The two CI paths differ in ways a shared script would have to absorb
carefully — the `type=gha` cache backend needs Actions runtime env vars that
`build-push-action` injects and a plain `run:` step does not, and the two
paths have different default provenance behaviour, a mismatch that already
forced commit `419b53ec`. `build-images.sh` exposes every flag that migration
needs (`--platform`, `--push`, `--registry`, `--cache-from`, `--cache-to`,
`--provenance`, `--pull`) and the validation workflow exercises them, but
changing what the canonical images contain is a supply-chain change that
[CONTRIBUTING.md](../CONTRIBUTING.md) reserves for the maintainer. A fork can
now exercise such a change end to end before proposing it.

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
| Tor Project C Tor image (runtime base, and the mkp224o builder) | tor | `ARG TOR_IMAGE` — Onimages `tor:trixie`, tag + index digest |
| Docker CLI | tor | `ARG DOCKER_CLI_IMAGE` — named stage, tag + digest |
| mkp224o | tor | `ARG MKP224O_VERSION` + `ARG MKP224O_COMMIT`, asserted after clone |
| mkp224o ISA baseline | tor | `CFLAGS="-O3 -march=… -fomit-frame-pointer"` per `TARGETARCH`, asserted after configure |
| Tor apt signing key | tor | verified inside the Tor Project's own image build (fingerprint `A3C4…DD89`); this repo pins the resulting image by digest instead |
| WordPress base | wordpress | `ARG WORDPRESS_IMAGE` — tag + index digest |
| wp-cli | wordpress | `ARG WP_CLI_VERSION` + `ARG WP_CLI_SHA256`, verified |
| tor image (stress worker) | tests/stress | `ARG TOR_IMAGE`, supplied by the builder |

`tests/test_dockerfile_pins.py` fails if any base image loses its digest, if
the runtime base stops being the Tor Project's image, if an arti image, binary
or user creeps back in, if `USER root` or `CMD []` go missing from the runtime
stage, if `MKP224O_COMMIT` stops being a full SHA, if the mkp224o CFLAGS stop
naming a fixed `-march` or stop carrying `-O3`, if the wp-cli checksum is
removed, or if a `curl` loses `-f`.

### The Tor Project's images

The tor image is built **on** the Tor Project's own onion-service container
image — the [Onimages](https://gitlab.torproject.org/tpo/onion-services/onimages/)
project, published at `containers.torproject.org/tpo/onion-services/onimages/`
and documented at
[onionservices.torproject.org](https://onionservices.torproject.org/apps/base/containers/).
**`tor:trixie`** is the runtime base: Debian 13 from TPA's own base image,
`apt-get upgrade`d at their build, with Tor from deb.torproject.org (0.4.9.x —
Debian's own 0.4.8.16 has false-positive "compression bomb" warnings that
break onion services after ~20h). Their build fetches the archive key and
checks its fingerprint before trusting it, and leaves the apt source and
keyring configured. It ends in `USER debian-tor` and
`ENTRYPOINT ["/usr/bin/tor"]`, so the Dockerfile switches back to root (the
entrypoint drops privileges itself) and clears the inherited `CMD`. Its
numeric uids differ from the previous image's; the entrypoint's per-start
`chown -R /var/lib/tor` is what makes that safe for existing volumes.

Onimages also publishes an `arti:trixie` image, and until 2026-09-24 this
image shipped arti beside C Tor as a switchable implementation. It was
removed that day: arti hosts a site acceptably — 20/20 services reachable in
a 2.6.0 test, after the usual "Too many preemptive onion service circuits
failed" churn — but it has no control interface, and sleep/wake
DEL_ONION/ADD_ONION, the watchdog's stall recovery and the OnionHeaven
takeover pipeline are all built on the control port. What remains is its
key file format: the launchers deliver the onion service key as an OpenSSH
PEM in the `onionpress-onion-keys` volume (`onionpress-arti-state` until
2026-09-25; both launchers migrate an old volume once, before first-run
detection, and refuse to start if the copy fails), OnionHeaven exchanges
keys as `arti_key_pem`, and the entrypoint converts the PEM to C Tor's key
files. `tests/arti-descriptor-test.sh` is the standalone repro to re-run if
that decision is ever revisited.

Upstream rebuilds these **daily** and the tags move; a digest is the only
thing that names one specific build. The Tor sysadmins' [registry
notes](https://gitlab.torproject.org/tpo/tpa/team/-/wikis/service/gitlab)
say untagged manifests are not purged, but in practice they are: the digest
pinned on 2026-09-25 returned 404 by 2026-09-30, failing the v2.5.0 image
publish. Expect the pin to go stale within days, so a tor image build from
an older commit will fail until it is re-pinned. The fix is to bump the pin
(`build/base-image-digest.sh
containers.torproject.org/tpo/onion-services/onimages/tor:trixie`), not to
drop it. Shipped images are unaffected: they are self-contained in GHCR.

Before Onimages 0.3.0 (2026-09-24) the images were amd64-only, which on Apple
Silicon meant QEMU emulation for the whole Tor stack; that is why this image
used to build everything itself. 0.3.0 added arm64.
Upstream labels the images experimental; the trixie variants use only Tor
Project package sources.

### The pinned input that isn't a version: mkp224o's `-march`

Pinning the mkp224o commit pinned the *source*. The **binary** still varied,
because mkp224o's `configure.ac` appends `-march=native` whenever the compiler
accepts it — so the published image encoded whichever CPU the builder happened
to have. That is worse than a reproducibility wart. Both halves are built on
GitHub-hosted runners, whose CPU model changes under us without notice, and
`native` on the amd64 one is at least Haswell — so a binary using instructions
the user's CPU lacks dies with SIGILL. mkp224o is what mints the vanity
address, and nothing above it treats a failure as fatal:
`generate_vanity_in_container` in `src/onionpress/launcher_ops.py` logs the
exit status and returns `None`, and the site carries on with a random address.
So the crash costs the user their `op2…` identity and reports nothing. (The
macOS launcher runs the bundled binary rather than this one, and
`generate_vanity_address` there says the same thing in a comment.)

Each arch now gets a fixed baseline, chosen from `TARGETARCH`:

| `TARGETARCH` | `-march` | Floor |
|---|---|---|
| `amd64` | `x86-64-v2` | Nehalem (2008) and later |
| `arm64` | `armv8-a` | every arm64 machine |
| anything else | — | build fails, deliberately |

An unrecognised `TARGETARCH` fails the build rather than falling back,
because both fallbacks are silent: guessing an ISA SIGILLs at run time, and
omitting `-march` lets `configure` put `native` back.

**What the baseline costs**, measured against mkp224o v1.7.0's default donna
backend:

- **arm64: nothing at all.** `-march=native` and `-march=armv8-a` compile to
  *byte-identical* binaries. donna's hot loop is 64x64→128 scalar multiplies,
  which are base-ISA `mul`/`umulh`; there is no wider instruction available to
  give up.
- **amd64: real, and accepted.** `x86-64-v2` and plain `x86-64` are
  equivalent for this code (10727 vs 10729 instructions in `worker.o`), so the
  floor could drop further for free if some ancient CPU ever needs it. The
  step that costs something is v3: at `-march=x86-64-v3` the compiler emits
  900 `mulx` and uses AVX2, about 9% fewer instructions. We give that up on
  purpose: the longest prefix any caller accepts is 6 characters
  (`generate_vanity_in_container`; the macOS launcher caps its own at 5), so
  the entire search is ~1.07e9 keys at millions of keys/sec per core across
  every core — tens of seconds. Tens of seconds are worth less than an old
  machine silently never getting a vanity address at all.

The amd64 figures are instruction counts, not timings: there is no x86
hardware in this project's local toolchain, and timings under emulation would
measure the emulator. The arm64 figures are wall-clock throughput on an Apple
M1 Pro.

### `-O3` is load-bearing, and its absence is silent

`configure.ac` applies its own `-O3 -march=native -fomit-frame-pointer` **only
when `CFLAGS` arrived unset** — it compares `CFLAGS` before and after
`AC_PROG_CC`. Pass any `CFLAGS` and that whole block is skipped, so a build
that sets `-march` by hand and stops there gets **no optimisation at all**.
Nothing warns about it: configure succeeds, `make` succeeds, and mkp224o runs
and mints correct addresses, several times slower. On an Apple M1 Pro, one
thread (keys/sec):

| backend | no `-O3` | with `-O3` |
|---|---|---|
| donna (Dockerfile default) | 1.02M | 6.01M |
| ref10 (`build-dmg-simple.sh`) | 1.04M | 2.37M |

`build/build-dmg-simple.sh` was in exactly that state — it has always passed
`CFLAGS` to drive the universal cross-compile, so every shipped macOS
`mkp224o` was unoptimised. It now passes `-O3 -fomit-frame-pointer` too, and
its binary cache key carries the flags so a warm cache cannot serve the old
slow binary. The Dockerfile greps the generated `GNUmakefile` after
`./configure` for the same reason: this failure leaves no other trace.

macOS needs no `-march`: `-arch arm64` / `-arch x86_64` already fix the ISA,
and the universal binary has to run on every supported Mac.

That table also shows something this change deliberately does *not* touch:
`build-dmg-simple.sh` passes `--enable-ref10`, and on ARM ref10 is 2.5x
slower than the donna backend the container build uses. Changing which
ed25519 implementation mints macOS addresses is a larger decision than
restoring an optimisation flag, and it cannot be validated for the x86_64
half of the universal binary from an Apple Silicon host.

### PyNaCl in the MenubarApp: the same trap, upstream

Backups are encrypted with PyNaCl (libsodium; `src/onionpress/backup_crypto.py`).
PyNaCl's macOS wheels on PyPI are built with `CFLAGS="-arch arm64 -arch
x86_64"` and no `-O`, and libsodium's `configure` likewise adds an
optimisation level only when `CFLAGS` arrives unset. On an Apple Silicon Mac,
PyNaCl 1.5.0 and 1.6.2 wheels against the same release built with `-O2`:

| | PyPI wheel | built with `-O2` |
|---|---|---|
| Argon2id, the backup's parameters (64 MiB) | 450 ms | 67 ms |
| XChaCha20-Poly1305 secretstream | 50 MiB/s | 470 MiB/s |
| backup / restore of a 514 MB tree | 12.7 s / 12.3 s | 4.9 s / 4.6 s |

`build/py2app-extensions.sh prepare` therefore rebuilds PyNaCl from source in
the py2app venv (`-O2`, both `-arch` flags under a universal2 interpreter,
`--no-cache-dir` because pip's wheel cache is not keyed by `CFLAGS`; the
build runs libsodium's own test suite). It also lipo-merges cffi's
`_cffi_backend`, which PyNaCl loads and which cffi publishes as per-arch
wheels only — without that a universal2 bundle would fail to make or restore
a backup on an Intel Mac. `verify` then has the bundled interpreter derive a
key and encrypt with it, natively and as x86_64 under Rosetta, and prints the
speed. It also fails the build unless OnionHeaven request signing runs on
libsodium's ed25519 core functions (`has_crypto_core_ed25519`; a minimal
libsodium build lacks them and `onion_auth` would fall back on pure Python)
and signs byte for byte as libsodium's `crypto_sign`. On Linux, Debian's
and Ubuntu's `python3-nacl` link the system `libsodium23`, which has them.

### The third-party notices file

`Contents/Resources/THIRD-PARTY-NOTICES.txt` is written in two halves, both
by `build/build-dmg-simple.sh`. The first, before py2app runs, is the six
downloaded binaries (Colima, Lima, the Docker CLI, Compose, mkp224o and the
libsodium linked into it): `license_file` takes each text out of the release
archive, else `build/.cache/licenses/`, else the project's repository at the
pinned tag, and `notice_section` prints one entry per project. The second,
once the MenubarApp is built and checked, is `build/python-notices.py`, run
under the build venv's interpreter: the interpreter itself (the standard
library's `LICENSE.txt`, PSF-2.0) and every installed distribution with a
module in the bundle — a package directory under
`Contents/Resources/lib/python3.X/`, a module in `lib-dynload/` or a member
of `site-packages.zip` — in the same entry format, with the texts out of its
`*.dist-info` (the `License-File` entries, under `licenses/` in a new wheel
or beside `METADATA` in an older one, else any `LICENSE*`/`COPYING*`/
`NOTICE*` there). pip's build-only tools (pip, py2app, macholib, modulegraph,
altgraph) have no module in the bundle and are left out, by name in the log.

What stops the build: a bundled distribution without a text, one that
declares no license (no `License-Expression`, `License` field or license
classifier), no `LICENSE.txt` beside the standard library, a package
`src/requirements.txt` names that the bundle check does not find (the check
is wrong, or py2app dropped it), and the script's output is appended whole
or not at all. A wheel that ships no text at all gets it from the project's
repository at the installed version's tag through the script's
`LICENSE_URLS` table and the same cache as the binaries; pyobjc-core's wheel
is the one case today (its `License.txt` is in the pyobjc repository at
`v<version>/pyobjc-core/`). A new dependency whose wheel carries no text
needs a row there. The log also prints a `NOTE:` naming any bundled
top-level module that belongs to no installed distribution, the standard
library or OnionPress — look at it when it appears. `build/rebuild-menubar.sh`
does not rewrite the file; only `make dmg` does.
`tests/test_license_declarations.py` runs both halves against a curl stub,
the Python one against a fake build venv and bundle.

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
have been installed and trusted. This repo first fixed that with its own
`gpg --show-keys` assertion. Since the move onto the Onimages base the fetch
happens in the Tor Project's own image build (their `get-tor-debian-key`
helper checks the same fingerprint), and this Dockerfile no longer touches the
key at all — a test fails if a second apt source or key fetch comes back. What
this repo verifies instead is the identity of the resulting image, its digest.

### What is deliberately not pinned

**The `tor` apt package, by version.** Tor's version is fixed by the
`TOR_IMAGE` digest: the runtime stage neither reinstalls nor upgrades the
package, and a test enforces that (an `apt-get install tor` in a derived stage
would quietly move to whatever the mirror serves on build day). An apt version
pin on top would only rot — `deb.torproject.org` removes superseded versions
from its mirror and publishes no snapshot service, so it becomes
`E: Version '…' was not found` within weeks. The identifier users actually
consume remains the published image digest in `build/image-pins.env`.

**WordPress core, in practice.** The base image is pinned, but that does not
freeze WordPress for users: `onionpress-security-audit.sh` runs on every
container start and applies pending core security releases over Tor. The pin
fixes what this image is *built* on; the running site patches itself. A site's WordPress lives in its volume (the base image copies its files in
only on a first start), so the audit also moves a site on an older branch up
to `WORDPRESS_BRANCH`, the branch the image is built on (next section).

### Bumping a pinned input

Resolve the new **multi-arch index digest** — not a per-platform manifest
digest, which resolves on amd64 and 404s on the arm64 builder. No daemon
needed; the script speaks the standard registry token flow, so it works for
Docker Hub and for `containers.torproject.org` alike, and refuses a tag that
resolves to a single-platform manifest:

```bash
build/base-image-digest.sh containers.torproject.org/tpo/onion-services/onimages/tor:trixie
build/base-image-digest.sh docker:29.8.1-cli
build/base-image-digest.sh wordpress:latest
```

(`docker buildx imagetools inspect <tag>` reports the same digest when you
have buildx.) Then edit the `ARG` default and rebuild. The build prints
`tor --version` so the reviewer of a `TOR_IMAGE` bump sees what it brought.
Two coupling rules:

- **`MKP224O_VERSION` must match `build/build-dmg-simple.sh`.** That script
  cross-compiles the same mkp224o release as a universal macOS binary. Two
  different versions minting vanity addresses for the same project is a
  difference nobody notices until the outputs differ. A test enforces this.
- **Bumping `MKP224O_COMMIT` means re-reading `configure.ac`.** The `-march`
  and `-O3` handling described above is upstream behaviour this build works
  around, not an interface. The Dockerfile asserts the flags it wanted
  actually landed in the generated `GNUmakefile`, so a change upstream fails
  the build loudly instead of quietly shipping an unoptimised miner.

#### Moving to a new WordPress branch

`WORDPRESS_BRANCH` in `app/Resources/docker/wordpress/Dockerfile` names the
WordPress branch (`7.1`) the image is built on, and the build fails when the
base image carries another. Every existing site follows it at its next start:
`onionpress-security-audit.sh` passes it to `onionpress-core-update.php
check`, which offers the newest release of the site's own branch or, when
newer, of this one (over Tor, like a security release; then `wp core
update-db --network` and the checksum audit). Never past it and never down
(`tests/test_wordpress_branch_upgrade.py`).

So re-pinning `WORDPRESS_IMAGE` to a base on a new branch is a WordPress
upgrade for every user. Before raising `WORDPRESS_BRANCH`, start a site
on the old image in a throwaway VM (never the live one; see "Integration
tests"), provision it as setup does, switch it to the new image, and check
that the audit logs `core updated to … and DB migrated`, then: every public
page and every wp-admin page of the main site, the user's subsite and the
network admin load without PHP errors in the debug log; the block editor
saves; Cache Enabler stores and serves onion-host pages only and clears on
edit; the Link Fixer records links and refuses onion/local ones; and the
docker-backed integration suites pass against it. Read the release's
dev notes for changes to anything the mu-plugins hook (multisite bootstrap,
`sunrise.php`, `WP_Http`, `clean_url`, REST permissions).

#### WordPress.org plugins (Cache Enabler, IA Link Fixer)

`app/Resources/scripts/install_plugin.sh` installs one release of each
plugin it knows (`plugin_pin()`: version and the SHA256 of that release's
zip) and unpacks nothing whose zip does not match. A slug with no pin, or a
pin with no SHA256, installs nothing. To move a plugin to a new release,
read its changelog, then:

```bash
build/pin-wp-plugin.sh cache-enabler 1.8.17
```

and paste the line it prints into `plugin_pin()`. The script fetches the
same versioned URL the launcher fetches over Tor
(`downloads.wordpress.org/plugin/<slug>.<version>.zip`). WordPress.org
builds those zips itself; if it ever rebuilds one, the install refuses it
until the pin is renewed (`tests/test_plugin_pins.py` checks the pins'
form and runs the check). An installed copy of any other version is
replaced at the next start.

Both plugins are held in check by mu-plugins that lean on their internals,
so a new release is also read against those:

- `onionpress-cache-policy.php` (Cache Enabler): the
  `cache_enabler_settings_before_validation` and `cache_enabler_bypass_cache`
  filters, the per-host settings files in `wp-content/settings/cache-enabler/`
  and `Cache_Enabler_Disk::delete_settings_file()`, and clearing going
  through `Cache_Enabler_Disk::cache_iterator()` → `esc_url_raw()` (how
  clearing reaches the onion address). If clearing moves, posts published
  from localhost show on the onion site only when the hour is up.
- `onionpress-link-fixer-policy.php` (IA Link Fixer): every request through
  the WordPress HTTP API with a `WP-Wayback-Link-Fixer` header and the link
  in a `url` parameter or POST field, the `iawmlf_posts_per_batch` and
  `iawmlf_scan_posts_interval` filters, the `iawmlf_process_links` option and
  `iawmlf_links` post meta, and the importers' `_source_id` post meta.

`tests/test_bundled_plugins.py` runs both mu-plugins against those shapes.

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

Since then the start-time pull on both platforms is `docker compose pull
--policy missing` and `start-tor` has no pull policy at all: with every image
pinned by digest, a launch fetches exactly the pins a new release moved and
nothing else, and never touches the registry otherwise. The opt-in refresh
(`UPDATE_ON_LAUNCH`'s `update_images()`, `onionpress update-images`, the
image pass of the menubar's Check for Updates) is gone: with every image
pinned it could only re-pull the same digests. Images change with a release;
Linux's `onionpress update` pulls the new release's pins, and each start
removes the images a moved pin superseded (`start.prune_superseded_images`).

All of them now check `using_local_images()` — true when
`ONIONPRESS_TOR_IMAGE` or `ONIONPRESS_WORDPRESS_IMAGE` points at a reference
that is *not* on `ghcr.io`. A `ghcr.io` reference is a deliberate pin, not a
local build, so pulls keep working normally for everyone else.

The predicate is `onionpress.containers.using_local_images()`; the bash
launchers' `using_local_images()` ask it through `onionpress using-local-images`
(exit 0 = local images), and the macOS menubar calls it in-process.
`tests/test_local_image_mode.py` runs the bash delegation and checks that no
pull escapes the guard.

### Pointing an installed app at local images

Add to `~/.onionpress/config` (the keys are in the shipped config template,
commented out):

```
ONIONPRESS_TOR_IMAGE=onionpress-tor:dev
ONIONPRESS_WORDPRESS_IMAGE=onionpress-wordpress:dev
```

Both launchers read these at startup and export them, so compose, the pull
gating and vanity-key generation all follow.

The macOS menubar app reads the same file directly, via
`onionpress.containers.image_override()`. That is necessary rather than
redundant: the MenubarApp *spawns* the launcher, so the launcher's exports can
never reach it, and it has its own `docker compose pull` on the
"Check for Updates" path. Without reading the config itself it would pull over
your local image on every launch and then report the images as up to date.

### One stack per machine

`docker-compose.yml` hardcodes `container_name:` and `volumes: name:`, so a
"dev" stack is not isolated from an installed OnionPress — it takes the same
containers and volumes over. `build/dev-up.sh` refuses to start when an
OnionPress stack is already running rather than fighting it; `--force`
overrides.

For the same reason `--down` never passes `-v`. Those volumes hold the
database, the WordPress content and the onion service keys.

---

## Integration tests

```bash
ONIONPRESS_INTEGRATION_TESTS=1 python3 -m unittest \
    tests.test_bluesky_importer tests.test_mastodon_importer \
    tests.test_twitter_importer tests.test_wayback_sweep
```

These four suites drive the real mu-plugins inside `onionpress-wordpress`
through `docker exec … wp`. They create subsites, publish and delete posts,
and rewrite options in whichever container the host `docker` CLI reaches — on
a machine that also runs OnionPress, the live site. On 2026-09-30 a plain
`python -m unittest discover tests` on such a Mac created three sandbox
subsites served at the live onion address, published and force-deleted posts
on the owner's real subsite, deleted its Wayback lock and back-off, and the
live Wayback sweep then submitted the sandboxes to the public Wayback Machine.

So `tests/wp_integration.py` now stands between them and Docker:

- **Opt-in.** Without `ONIONPRESS_INTEGRATION_TESTS=1` every one of them is
  skipped and Docker is never called. `make test-unit` and CI stay fast and
  safe on any machine.
- **Liveness guard.** Opted in, the target is checked once per run, and the
  suites *error* — they do not skip — if any of these hold:

  | Signal | What it means |
  |---|---|
  | `ONIONNAME_REGISTERED=yes` in `~/.onionpress/config` | this machine registered a public onion name |
  | a non-empty `~/.onionpress/onion_address` | the menubar app here has published an onion service |
  | `/var/lib/onionpress/onion_address` in the container | the launcher published this WordPress as an onion service; it is also the file the Wayback sweep needs before it can submit anything |
  | a subsite other than the root and the test sandboxes | setup creates `/<onionname>/` for the owner of every real install |
  | `onionpress_root_site=yes` on the root site | a branded install (onionpress.org, OnionHeaven) whose root site is public |

  A probe that cannot answer — no running container, WordPress not installed
  or not multisite — refuses too. The first two signals describe the machine,
  not the container, so a machine that runs OnionPress is refused even when
  `docker` points at another daemon. That is deliberate.
- **Sandboxes.** Each test class works in its own subsite (`op-bluesky-test`,
  `op-mastodon-test`, `op-twitter-test` or `op-wayback-test`), created
  non-public and with `op_wayback_exclude=yes` set as part of creating it,
  and deleted when the class finishes. The Wayback plugin never visits,
  counts or schedules a sweep from a subsite with that option.

`tests/test_wp_integration_gate.py` pins all of this without Docker.

### What to run them against

A Docker daemon that has never run OnionPress — a CI runner or a VM — with a
WordPress multisite carrying the mu-plugins from this checkout, and **no tor
container**, so nothing ever writes the onion address file. Bringing up
`wordpress` alone from `app/Resources/docker/docker-compose.yml` starts it
and its database. Give it a minute: the image runs a one-shot multisite
conversion 15 s after start, and it must find WordPress not yet installed,
or it races the conversion below. Then `wp core install
--url=http://localhost` inside the container, and
`onionpress.multisite.provision_post_install()` with this checkout's
`app/Resources/themes` and `app/Resources/plugins`, turn it into an
OnionPress network.

Never run that compose file on a machine with an OnionPress install:
`container_name:` and the volume names are hardcoded (see
[One stack per machine](#one-stack-per-machine)), so it would take the live
site's containers over.

---

## Generated assets

Four families of binary were committed with nothing in the repo that could
produce them. Each now has a recipe, and the recipes differ in how much they
guarantee — that difference is the point of this section.

| Artifact | Command | Guarantee |
|---|---|---|
| `app/Resources/app-icon.png` | `build/make-icons.sh` | **byte-identical** |
| `app/Resources/AppIcon.icns` | `build/make-icons.sh` | **byte-identical** |
| `app/Resources/menubar-icon-*.png` | `build/make-icons.sh` (needs ImageMagick) | **pixel-identical** (`running`, `starting`); `stopped` within 2/255 |
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

The three menubar PNGs were made with **ImageMagick**, not `sips` — their
embedded `tEXt` chunks say so, and `exif:PixelXDimension 761` pins the source
to `assets/branding/icon-menubar.png`. ImageMagick stamps the save time into
every PNG it writes, so a fresh render can never be byte-identical to a
committed file even when every pixel matches; `--verify` compares **pixels**
for these three and bytes for the `.icns` chain.

Measured, with ImageMagick installed:

| Icon | Recipe | `--verify` |
|---|---|---|
| `running` | `-resize 75x88` | pixel-identical |
| `stopped` | `-grayscale Rec709Luma` | ~2 pixels differ by 2/255 — version rounding. `-colorspace Gray` is linear-light and far off. |
| `starting` | flood fill from the four corners, 15% fuzz | pixel-identical |

The `starting` recipe was first written as `-transparent gray` and that was
**wrong**: the backdrop is `#E0E0E0`, not `#808080`, so it removed nothing.
Even `-transparent '#E0E0E0'` with fuzz reaches only 610 of the committed 617
transparent pixels, because the backdrop has ±1 noise per channel — the
original was a *connectivity*-based fill from the edges, which a corner flood
fill reproduces exactly. Outputs are `-strip`'d so two runs agree byte for
byte; regenerating drops the timestamps the committed files carry.

### Browser extensions

```bash
build/build-extension.sh          # both
build/build-extension.sh firefox  # one
```

Firefox is built from `extension/` overlaid with `extension-firefox/`, which
holds only what Firefox needs to differ: `manifest.json` (v1.2.0, min Firefox
142, narrow permissions, `data_collection_permissions`, which
addons.mozilla.org requires from Firefox 142). **Every file in
`extension-firefox/` wins**, so a copy of a shared file there would silently
keep a fix to `extension/`'s out of the Firefox package: `background.js`,
`offline.html` and `offline.js` are shared from `extension/` (issue #318
removed the overlay's copies; a fix to `extension/background.js` alone used to
miss the Firefox package, and `extension/`'s `offline.html` carried an inline
`<script>` the extension CSP refuses). A test fails if any of them comes back
to the overlay. The build fails if the manifest references a file that is not
in the package.

How the extension reaches Tor (#318): `.onion` requests go to the menubar's
HTTP proxy (`onion_proxy.py`, `127.0.0.1:9077`, +10000 per extra macOS user),
never to Tor's SOCKS port, which does not work through Colima's port
forwarding. The proxy port comes from the native messaging host
(`native_messaging_host.get_proxy_port`, read from Docker's published
WordPress port). The extension polls `/status` every 5 s with
`X-OnionPress-Browser`, which writes `~/.onionpress/extension-connected`
(`browser.write_extension_marker`; the native host writes the same JSON).
`https://` `.onion` URLs are not proxied (the proxy refuses CONNECT to an
onion). A `.onion` page load while OnionPress is down opens `offline.html`;
one whose onion service does not answer gets the proxy's own page. Both offer
a retry and a link to the Wayback Machine's copy.

`.gitignore` used to list `extension-firefox/` while four of its files were
tracked. Ignore rules do not apply to already-tracked files, so the entry was
inert for those four — but any *new* file added there would have been silently
untracked, in the directory holding the current manifest. That entry is gone.

Removed in issue #321: a committed `build/onionpress-firefox.xpi` (v1.0.0,
built from a `background.js` revision and a `manifest.json` no commit
holds, so nothing could rebuild it), the stale
`extension/manifest.firefox.json` (v1.0.0, min Firefox 109, `webRequest`,
`webRequestBlocking`, `webNavigation`, `<all_urls>`, a content script, no
`data_collection_permissions`) and the unused `extension/content.js`.
`tests/test_generated_assets.py` keeps them out.

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

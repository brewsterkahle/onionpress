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
because it uses `swiftc`, `lipo`, `codesign`, `hdiutil` and `PlistBuddy`.
Those are called out per component.

## Contents

- [Container image pins](#container-image-pins)
- [Running a locally built stack](#running-a-locally-built-stack)
- [Building the container images](#building-the-container-images)
- [Pinned inputs](#pinned-inputs)
- [Running the stack you just built](#running-the-stack-you-just-built)
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
| unit tests | `make test-unit` | any | — |
| source layout + pin check | `make test` | any | — |

`make deb` and `make extension` need no network at all. The `.dmg` is
macOS-only because it uses `swiftc`, `lipo`, `codesign`, `hdiutil` and
`PlistBuddy`; note that `.deb` is *not* Linux-only — `build/build-linux.sh`
hand-assembles the `ar` archive in Python when `dpkg-deb` is absent.

### What has been proven by actually running it

Every row above has been executed on a developer machine, not just reasoned
about. So you know what you are trusting:

| Artifact | Proven |
|---|---|
| `.deb` | builds on macOS via the pure-Python `ar` fallback; ships 0 stale `.pyc` (was 18) |
| `.dmg` | full path — pinned binary downloads, libsodium + mkp224o cross-compile (universal), py2app, signing, `hdiutil`. 160 MB, version verified. **Dev-grade**: with `uv` the bundled Python is arm64-only; release-grade needs the python.org universal2 3.14 installer |
| `onionpress-wordpress` image | builds with the classic builder; wp-cli 2.12.0 with the pinned sha256 in the image; a wrong `WP_CLI_SHA256` fails at `sha256sum -c` **before** `chmod +x`; a wrong base digest fails at `FROM` |
| `onionpress-tor` image | builds on the Tor Project's Onimages `tor:trixie` image in under a minute once it is pulled (isolated Colima VM, classic builder — nothing but mkp224o is compiled); baked in: Tor 0.4.9.13, Docker 29.8.1, mkp224o v1.7.0 against the base's libsodium; image user root, `CMD []`; the entrypoint bootstraps Tor in onion-service, SOCKS-only and takeover-worker modes and converts a delivered PEM key to C Tor's files; the stress worker chains off it; 0 `.pyc` under `/wordlists`; a wrong `MKP224O_COMMIT` fails at the post-clone assert |
| `AppIcon.icns`, `app-icon.png` | byte-identical to the committed files |
| menubar PNGs | `running`, `starting` pixel-identical; `stopped` within 2/255 (see [Generated assets](#generated-assets)) |
| extensions | byte-reproducible across runs |
| `docker-publish.yml` | ran end to end in a fork (`ivar/onionpress`, run 36064530531, 2026-09-24, on the C Tor-only image) on GitHub-hosted runners only — amd64 on `ubuntu-latest`, arm64 on `ubuntu-24.04-arm`. All three images published under the fork's own namespace as OCI indexes carrying `linux/amd64` and `linux/arm64`; the stress worker's `FROM` resolved to the tor index the same run had merged seconds earlier. 2 min 12 s end to end; tor 58 s (amd64) / 55 s (arm64). The previous run on the arti-compiling image (35479901478, 2026-09-20) took 8 min 50 s |

One forward-looking note from the DMG log: on macOS 27 `hdiutil create`,
`hdiutil attach` and `hdiutil convert` each print a deprecation warning
pointing at `diskutil image …`. They still work today; a future macOS may
remove them, and `build/build-dmg-simple.sh` uses all three.

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
colima start --cpu 6 --memory 8 --disk 20      # first time: downloads a ~200 MB VM image
build/build-images.sh tor                      # under a minute once the base image is pulled
colima stop                                    # frees the RAM; the layer cache stays
```

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
colima start
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
`ubuntu-latest` and arm64 on `ubuntu-24.04-arm` — both GitHub-hosted, both
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
`.deb` — 18 stale `.pyc` files — and `build/build-linux.sh` now strips them
too.

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
| Tor apt signing key | tor | verified inside the Tor Project's own image build (fingerprint `A3C4…DD89`); this repo pins the resulting image by digest instead |
| WordPress base | wordpress | `ARG WORDPRESS_IMAGE` — tag + index digest |
| wp-cli | wordpress | `ARG WP_CLI_VERSION` + `ARG WP_CLI_SHA256`, verified |
| tor image (stress worker) | tests/stress | `ARG TOR_IMAGE`, supplied by the builder |

`tests/test_dockerfile_pins.py` fails if any base image loses its digest, if
the runtime base stops being the Tor Project's image, if an arti image, binary
or user creeps back in, if `USER root` or `CMD []` go missing from the runtime
stage, if `MKP224O_COMMIT` stops being a full SHA, if the wp-cli checksum is
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
say untagged manifests are deliberately *not* purged (a Saturday cron only
collects unreferenced layers), so a pinned digest stays pullable after the
tag has moved on — with the stated caveat that purging is a policy they could
adopt if the registry ran out of space. If a pinned digest ever 404s, the fix
is to bump it, not to drop the pin.

Before Onimages 0.3.0 (2026-09-24) the images were amd64-only, which on Apple
Silicon meant QEMU emulation for the whole Tor stack; that is why this image
used to build everything itself. 0.3.0 added arm64.
Upstream labels the images experimental; the trixie variants use only Tor
Project package sources.

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
fixes what this image is *built* on; the running site patches itself.

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
One coupling rule:

- **`MKP224O_VERSION` must match `build/build-dmg-simple.sh`.** That script
  cross-compiles the same mkp224o release as a universal macOS binary. Two
  different versions minting vanity addresses for the same project is a
  difference nobody notices until the outputs differ. A test enforces this.

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

`extension-firefox/` is overlaid on top of `extension/`, so **every file it
contains wins** — `manifest.json`, `offline.html`, `offline.js` *and*
`background.js`. Only the icons and the popup come from `extension/`. The two
`background.js` files happen to be byte-identical today, which is exactly what
would make a future divergence silent: fix a bug in `extension/background.js`
alone and the Firefox package will not contain it.

`offline.html` is deliberately different: `extension/`'s uses an inline
`<script>`, which violates the extension CSP, and `extension-firefox/`
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

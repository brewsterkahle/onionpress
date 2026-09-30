# How to build OnionPress

Everything OnionPress ships can be built on your own machine — the macOS
installer, the Linux package, the container images, the browser extensions and
the icons. Nothing needs the project's CI, its registry credentials or anyone's
self-hosted runner — and the CI itself runs in a fork, on GitHub's own runners,
publishing under your account (see [§6](#6-cutting-a-release-maintainers)).

This is the practical guide: what to install, what to run, how to check the
result. The *why* behind each step — pins, digests, the history of what went
wrong before — is in [BUILDING.md](BUILDING.md).

```bash
make doctor        # tells you which tools below you have and which you lack
```

## Contents

1. [What you can build](#1-what-you-can-build)
2. [System requirements](#2-system-requirements)
3. [Software requirements](#3-software-requirements)
4. [Quick start](#4-quick-start)
5. [Building each artifact](#5-building-each-artifact)
6. [Cutting a release](#6-cutting-a-release-maintainers)
7. [Troubleshooting](#7-troubleshooting)
8. [Verified configuration](#8-verified-configuration)

---

## 1. What you can build

| Artifact | Command | Host OS | Measured time |
|---|---|---|---|
| Container images (tor, wordpress) | `make images` | any with Docker | tor under 1 min once its Tor Project base image is pulled; wordpress ≈ 1 min |
| Run the stack on those images | `make dev-up` | any with Docker | seconds |
| macOS installer `onionpress.dmg` | `make dmg` | **macOS** | ≈ 4 min (first run downloads ≈ 200 MB) |
| Linux package `onionpress.deb` | `make deb` | any | seconds |
| Chrome + Firefox extensions | `make extension` | any | seconds |
| App icons (`.icns`, menubar PNGs) | `make icons` | **macOS** | seconds |
| Unit tests | `make test-unit` | any | ≈ 30 s |

Times are from the [verified configuration](#8-verified-configuration) below
— an Apple Silicon Mac. The tor image compiles nothing but mkp224o (Tor
comes from the Tor Project's image), so only that small compile scales with
cores; running the build under CPU emulation (e.g. building arm64 on an
Intel host) still multiplies every step.

---

## 2. System requirements

### Operating system

| You want | You need |
|---|---|
| `.dmg`, icons | macOS 13 or later with the Xcode Command Line Tools. The launcher is compiled with a macOS 13 deployment target and the bundle is universal (arm64 + x86_64) regardless of the host. Verified on Apple Silicon; an Intel host should work but has not been exercised. |
| Container images, dev stack | Any OS with a Docker daemon: macOS (Docker Desktop, Colima, or the Colima that OnionPress itself bundles), Linux, or WSL2 (untested). |
| `.deb` | Any OS. `dpkg-deb` is used when present; otherwise the archive is assembled by a pure-Python fallback, which is how it builds on macOS. |
| Extensions, tests | Any OS. |

### Hardware

- **RAM for the tor image**: nothing heavy any more — Tor comes from the Tor
  Project's image and only mkp224o is compiled. The verified build ran in an
  8 GB isolated Colima instance that had been sized for the old arti compile;
  far less would do. Still build *beside*, not inside, OnionPress's own 1 GB
  VM — it is your live site; see
  [Building the tor image beside a running OnionPress](#building-the-tor-image-beside-a-running-onionpress).
- **Disk**: about 2 GB for a DMG build (the assembled `OnionPress.app` is
  ≈ 370 MB, the DMG 160 MB, the download cache 290 MB); about 5 GB for the
  image builds and their base layers.
- **CPU**: anything works; the tor build scales with cores.

### Network

Builds are local but not hermetic — they fetch pinned inputs from upstream:

| Build | Reaches |
|---|---|
| `make images` | containers.torproject.org (the Tor Project's tor image), Docker Hub (wordpress, docker CLI base images), Debian and deb.torproject.org apt repos, GitHub (mkp224o, wp-cli) |
| `make dmg` | GitHub releases (Colima, Lima, Docker Compose), download.docker.com, python.org/PyPI, libsodium.org |
| `make deb`, `make extension`, `make icons` | nothing |

Every remote input is pinned — by digest, version, commit or checksum — so
what you fetch is what the published build fetched. Details in
[BUILDING.md → Pinned inputs](BUILDING.md#pinned-inputs).

---

## 3. Software requirements

### Everything

| Tool | Notes |
|---|---|
| `git` | |
| `python3` **3.10 or newer** (3.14 recommended) | Runs the build scripts and tests. **Stock macOS `/usr/bin/python3` is a 3.9 shim and will not work** — use Homebrew's, `uv`'s, or python.org's. |
| `bash` | The macOS system bash (3.2) is fine; the scripts are written for it. |
| `curl` | |

### Per artifact

| Artifact | Required | Optional |
|---|---|---|
| Container images | `docker` | `docker buildx` — only for `--platform` (multi-arch) and `--push`. A plain local build falls back to the classic builder without it. |
| Dev stack | `docker` with Compose v2 (`docker compose`) | |
| `.dmg` | Xcode Command Line Tools (`swiftc`, `lipo`, `codesign`, `hdiutil`, `PlistBuddy`); Homebrew; `pkg-config`; **Python 3.14** — see below | `gh` (authenticated) — raises GitHub download speed from ≈ 30 KB/s to several MB/s |
| `.deb` | `python3` | `dpkg-deb` (used when present) |
| Extensions | `zip`, `python3` | |
| Icons | `sips`, `iconutil` (ship with macOS) | ImageMagick — needed only for the three menubar PNGs |
| Tests | `python3` ≥ 3.10 | `uv` — `make test-unit` uses it to pin 3.14 |
| Release | `gh`, authenticated | |

`build/build-dmg-simple.sh` installs `libsodium`, `autoconf` and `automake`
itself via Homebrew if they are missing (for the mkp224o cross-compile).

### Python 3.14 for the DMG: two grades

The `.dmg` embeds a Python interpreter via py2app, and the interpreter it finds
decides what the installer can run on:

| Interpreter | Result | When |
|---|---|---|
| **python.org universal2 3.14** at `/Library/Frameworks/Python.framework/Versions/3.14` | **Release-grade** — runs on Intel and Apple Silicon Macs | Cutting a release. Installer needs an admin password. |
| **`uv`-managed 3.14** | **Dev-grade** — bundled Python is single-arch (arm64 on Apple Silicon) | Verifying the build path, local testing. No admin needed. |
| stock `/usr/bin/python3` | refused — the script hard-fails rather than ship an app that crashes on launch | — |

### Install commands

**macOS**

```bash
xcode-select --install                       # swiftc, lipo, codesign, hdiutil, PlistBuddy
brew install uv pkg-config                   # dev-grade DMG
brew install imagemagick                     # menubar icons only (pulls ~17 formulae)
brew install gh && gh auth login             # optional: fast downloads; required for releases
# release-grade DMG: install "macOS 64-bit universal2 installer" for 3.14 from python.org
```

Docker on macOS: Docker Desktop, `brew install colima docker`, or reuse the
Colima that OnionPress bundles (see [§5](#building-the-tor-image-beside-a-running-onionpress)).

**Debian / Ubuntu**

```bash
sudo apt-get install git python3 zip curl docker.io docker-compose-v2
# multi-arch / push builds also want the buildx plugin (docker-buildx or docker-buildx-plugin)
```

The `.dmg` and icons cannot be built on Linux.

---

## 4. Quick start

```bash
git clone https://github.com/brewsterkahle/onionpress.git
cd onionpress
make doctor                 # what is missing, and what each missing tool costs you
make test-unit              # or: python3 -m unittest discover tests -p 'test_*.py'
make deb                    # → build/onionpress.deb          (seconds, no network)
make extension              # → build/dist/*.xpi, *.zip       (seconds, no network)
make images                 # → onionpress-tor:dev, onionpress-wordpress:dev
make dev-up                 # run the stack on them; http://localhost:8080
```

`make test` (no `-unit`) is a fast sanity check of the source layout that also
verifies the image pins have not drifted.

---

## 5. Building each artifact

### Container images

```bash
make images                              # tor + wordpress for this host's architecture
build/build-images.sh tor                # just one
build/build-images.sh all                # adds the stress-test worker
build/build-images.sh --help             # every flag
```

Produces `onionpress-tor:dev` and `onionpress-wordpress:dev`, and — by default
— also tags the tor image as `ghcr.io/brewsterkahle/onionpress-tor:latest` so
the Linux launcher's tag-only vanity-address check keeps passing
(`--no-shadow-tag` opts out).

`docker buildx` is optional. Without it the script uses the classic builder,
which is what the verified builds used; `--platform` and `--push` need buildx
and the script says so if you ask for them without it. Multi-arch requires
`--push` (Docker cannot load a multi-platform result locally):

```bash
make images-multiarch REGISTRY=ghcr.io/you
```

**Verify** what got baked in:

```bash
docker run --rm --entrypoint sh onionpress-tor:dev -c 'tor --version; docker --version; mkp224o -V'
docker run --rm --entrypoint sh onionpress-wordpress:dev -c 'wp --info --allow-root | grep -i version; sha256sum /usr/local/bin/wp'
```

Expected (as pinned in the Dockerfiles): Tor 0.4.9.13 from the pinned Tor
Project image, Docker 29.8.1, mkp224o v1.7.0; wp-cli 2.12.0 with the sha256
declared in `app/Resources/docker/wordpress/Dockerfile`.

#### Building the tor image beside a running OnionPress

OnionPress's own Colima VM is your live site. Do not build in it or resize
it. Run a second, isolated Colima instance under its own home using the
binaries the app bundles; nothing under `~/.onionpress` is touched, and
deleting the directory reclaims everything:

```bash
export PATH="/Applications/OnionPress.app/Contents/Resources/bin:$PATH"
export COLIMA_HOME="$HOME/.colima-build"
export LIMA_HOME="$COLIMA_HOME/_lima"
export DOCKER_CONFIG="$COLIMA_HOME/docker-config"
export DOCKER_HOST="unix://$COLIMA_HOME/default/docker.sock"

colima start --cpu 6 --memory 8 --disk 20    # first time: downloads a ~200 MB VM image
build/build-images.sh tor                    # under a minute once the base image is pulled
colima stop                                  # frees the RAM; the layer cache stays
# rm -rf ~/.colima-build                     # when you want the disk back
```

The bundled CLI has no buildx plugin, which is fine — the script falls back.

### Running the stack you built

```bash
make dev-up            # verifies the images exist, starts the stack
build/dev-up.sh --logs
make dev-down          # stops it; volumes are kept — they hold your site and onion keys
```

`docker-compose.yml` hardcodes container and volume names, so there is exactly
one OnionPress stack per Docker daemon. `dev-up.sh` refuses to start over a
running OnionPress rather than take its containers over. To point an
*installed* OnionPress.app at your images instead, add to `~/.onionpress/config`:

```
ONIONPRESS_TOR_IMAGE=onionpress-tor:dev
ONIONPRESS_WORDPRESS_IMAGE=onionpress-wordpress:dev
```

Both launchers and the menubar read this, and every image pull is skipped
while it points at a non-`ghcr.io` reference — otherwise the app would pull
the published image over yours on the next launch.

### macOS installer (`.dmg`)

```bash
make dmg               # → build/onionpress.dmg; also assembles OnionPress.app/ in the repo root
```

What it does: assembles the bundle from `app/`, compiles the Swift launcher
for both architectures, downloads pinned Colima / Lima / Docker / Compose
binaries and `lipo`s them universal (cached under `build/.cache/`), builds
libsodium and mkp224o for both architectures, runs py2app, ad-hoc signs
everything, and creates the DMG with the pre-baked Finder window styling from
`build/dmg-assets/`.

**Verify:**

```bash
/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" OnionPress.app/Contents/Resources/MenubarApp/Contents/Info.plist
lipo -archs OnionPress.app/Contents/Resources/bin/mkp224o                       # want: x86_64 arm64
lipo -archs OnionPress.app/Contents/Resources/MenubarApp/Contents/MacOS/python  # release: x86_64 arm64; dev (uv): arm64
open build/onionpress.dmg
```

The version must match `src/menubar.py`'s `self.version` — the script aborts
if it does not.

**Install for testing** (replaces whatever is in `/Applications`):

```bash
make install
```

**Iterating on `src/` only**: `build/rebuild-menubar.sh` rebuilds just the
py2app MenubarApp and writes it straight into `/Applications/OnionPress.app`.
Editing `src/` alone never changes the running app — the bundle holds compiled
`.pyc` files.

### Linux package (`.deb`)

```bash
make deb               # → build/onionpress.deb
```

Architecture `all`: scripts, compose files and the Python package; Docker
pulls the right image variant at runtime. Builds on macOS too.

**Verify:** `dpkg-deb -I build/onionpress.deb` on Linux, or list its contents
anywhere:

```bash
python3 -c "
import sys;d=open('build/onionpress.deb','rb').read();i=8
while i<len(d):
    n=d[i:i+16].decode().strip();s=int(d[i+48:i+58]);i+=60;print(n,s);i+=s+(s%2)"
```

### Browser extensions

```bash
make extension         # → build/dist/onionpress-chrome.zip, build/dist/onionpress-firefox.xpi
```

Byte-reproducible: sorted entries, fixed timestamps, no uid/gid. Firefox is
built from `extension/` overlaid with `extension-firefox/` — the latter holds
the current manifest (v1.1.0, Firefox ≥ 142, `data_collection_permissions`).
Do not build from `extension/manifest.firefox.json`; it is stale and would
request permissions the extension no longer needs.

Load unpacked for testing: Chrome `chrome://extensions` → Developer mode →
Load unpacked → `extension/`; Firefox `about:debugging` → Load Temporary Add-on.

### App icons

```bash
make icons                          # regenerates app-icon.png, AppIcon.icns, menubar-icon-*.png
build/make-icons.sh --verify        # rebuilds to a temp dir and compares; writes nothing
```

`app-icon.png` and `AppIcon.icns` regenerate **byte-identically**. The three
menubar PNGs need ImageMagick and are compared by **pixels** (ImageMagick
stamps the save time into every PNG, so bytes can never match): `running` and
`starting` are pixel-identical, `stopped` differs by 2/255 on about two
pixels.

### Tests

```bash
make test-unit                                      # pins Python 3.14 via uv
python3 -m unittest discover tests -p 'test_*.py'   # any Python ≥ 3.10
```

Standard-library `unittest` only — CI installs nothing else. A handful of
tests skip when a host tool is absent (sips/iconutil, ImageMagick, zip).

---

## 6. Cutting a release (maintainers)

```bash
build/bump-version.sh X.Y.Z             # updates every version location
docker pull ghcr.io/brewsterkahle/onionpress-tor:latest
docker pull ghcr.io/brewsterkahle/onionpress-wordpress:latest
build/refresh-image-digests.sh          # rewrites build/image-pins.env and every consumer
git commit -am "Bump version to X.Y.Z; refresh image digests"
build/release.sh                        # builds .dmg + .deb, creates the GitHub release with both
```

Requirements beyond §3: the **python.org universal2 Python 3.14** (the `.dmg`
must run on Intel), and `gh` authenticated. `release.sh` refuses to create a
release from Linux, because a `.dmg`-less "Latest" would 404 the README's
download link.

The container images are published by `.github/workflows/docker-publish.yml`
on release, or by hand from the Actions tab ("Run workflow"). It runs entirely
on GitHub-hosted runners — amd64 on `ubuntu-latest`, arm64 on
`ubuntu-24.04-arm` — and publishes to the namespace of the repository it runs
in, so it works unchanged in a fork:

```bash
gh workflow run docker-publish.yml --ref <branch> -R <you>/onionpress   # → ghcr.io/<you>/onionpress-*
gh run watch -R <you>/onionpress
```

Runs from the `development` branch prefix every image with `dev-`. A package
is private the first time GHCR sees it; make it public in the package settings
if installs must pull it anonymously. The workflow was deliberately **not**
rewired to call `build/build-images.sh`; see
[BUILDING.md → CI coverage](BUILDING.md#ci-coverage) for what a migration must
absorb. Never hand-edit an image digest — run `build/refresh-image-digests.sh`;
`make test` fails on drift.

---

## 7. Troubleshooting

**`docker: command not found` on macOS, but OnionPress runs fine.** The app
bundles its own Docker CLI inside `OnionPress.app`; nothing puts it on your
PATH. Either install Docker, or export the app's environment (the block in
[§5](#building-the-tor-image-beside-a-running-onionpress)). Note the app's
socket is `~/.onionpress/colima/default/docker.sock`, not `/var/run/docker.sock`.

**`'docker buildx' is unavailable, but you asked for: --platform`.** buildx is
needed only for multi-arch and push. Drop the flag for a local build, or
`brew install docker-buildx` / `apt-get install docker-buildx-plugin` and link
it into `~/.docker/cli-plugins`.

**Tor image build dies or crawls inside OnionPress's own VM.** That VM has 1 GB
of RAM. Use the isolated instance in §5.

**`ERROR: image pins have drifted`.** A consumer no longer matches
`build/image-pins.env`. `build/refresh-image-digests.sh --propagate` re-applies
the pins file everywhere; needs no Docker daemon.

**`py2app failed — retrying with setuptools<81...`** Expected. setuptools 81
removed an API py2app 0.28.9 still uses; the script retries automatically.

**`hdiutil: WARNING: ... is deprecated. Please use 'diskutil image ...'`**
Warnings only, on macOS 27. The DMG still builds.

**`ERROR: no Python 3.14 found` while building the DMG.** Install `uv`
(dev-grade) or the python.org universal2 installer (release-grade). The
script deliberately refuses stock `/usr/bin/python3`.

**`ERROR: mkp224o not available — refusing to build a DMG without it.`**
The cross-compile failed. Usually a missing `pkg-config` or Homebrew;
`make doctor` shows which. Without mkp224o every fresh install would silently
get a random `.onion` instead of a vanity address, so this is a hard stop.

**GitHub downloads at 30 KB/s during the DMG build.** Anonymous release
downloads are throttled. `gh auth login` — the script picks up the token
automatically.

**`stress-worker extends …onionpress-tor:latest, which is not present locally`.**
Build the tor image first (`build/build-images.sh tor` or `all`); otherwise
Docker would pull and extend the published image instead of yours.

**`make build` used to run a different script.** `build/build-dmg.sh` was
removed — it thinned universal binaries to arm64-only. `make build` and
`make build-simple` are now aliases for `make dmg`.

---

## 8. Verified configuration

Every command in this document was run, and its output checked, on:

| | |
|---|---|
| Host | macOS 27.0, Apple Silicon (10 cores, 32 GB) |
| Xcode CLT | Swift 6.4 |
| Python | 3.14.7 (Homebrew) for scripts and tests; `uv` 0.12.15 for the dev-grade DMG |
| ImageMagick | 7.1.2 |
| Docker | client 27.5.1 / server 27.4.0 (Colima 0.8.1), Compose 2.40.2 — classic builder, no buildx |
| tor image | under a minute after pulling the Tor Project base image, isolated Colima 6 CPU / 8 GB |
| DMG | 3 min 43 s, dev-grade |
| `docker-publish.yml` | 2 min 12 s end to end in a fork (run 36064530531, 2026-09-24), GitHub-hosted runners only (`ubuntu-latest`, `ubuntu-24.04-arm`), all three images amd64 + arm64; was 8 min 50 s when the tor image still compiled arti |

Not exercised: an Intel Mac host, Windows/WSL2, the buildx code path, a
release-grade DMG (needs the python.org interpreter), Linux native `dpkg-deb`
for the `.deb`. What *has* been proven, artifact by artifact, is tabulated in
[BUILDING.md → What has been proven by actually running it](BUILDING.md#what-has-been-proven-by-actually-running-it).

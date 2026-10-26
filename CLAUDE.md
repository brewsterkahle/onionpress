# OnionPress Project Memory

## Meta
- **This file (`CLAUDE.md`) is the project memory.** Store all new memories and notes here so they travel with the repo.
- Run `/mission` at any point to evaluate a change against OnionPress's three author commitments: safety, ease of publishing, and content durability.

## Naming Rules (IMPORTANT)
- The project is called **OnionPress** (one word, capital O and P). Never "Onion.Press", "onion.press", or "onion-press".
- Data directory: `~/.onionpress/` (not `~/.onion.press/`)
- GitHub repo: `brewsterkahle/onionpress`
- Use **"onion service"** (not "hidden service") in all user-facing text. Tor Project deprecated "hidden service" terminology.
  - Exception: file paths like `/var/lib/tor/hidden_service/` and Docker image names like `goldy/tor-hidden-service` cannot change — those are external identifiers.
- When writing new code, docs, issues, or UI text, always use "OnionPress" and "onion service".

## Key Architecture
- macOS menubar app (py2app built from `src/menubar.py`): a view of the
  daemon's state file; `onionpress daemon` (`src/onionpress/daemon.py`,
  a launchd agent) runs every loop (see "The Mac Daemon")
- Launcher shell script at `app/MacOS/onionpress` (assembled into `OnionPress.app/Contents/MacOS/` at build time)
- Docker containers (tor, wordpress, mariadb) run inside Colima VM
- Logs at `~/.onionpress/onionpress.log` and `~/.onionpress/launcher.log`
- User-visible content at `~/OnionPress/` (backups, Creations)

## Repo Layout
- `src/menubar.py` — py2app entry point (the only flat module; everything else lives in the package)
- `src/onionpress/` — shared Python package (all non-entry-point code)
- `app/` — macOS .app bundle source (assembled into `OnionPress.app/` at build time)
  - `app/Info.plist` — canonical version source #2
  - `app/MacOS/` — launcher scripts, Swift wrapper source
  - `app/Resources/` — docker configs, plugins, icons, templates
- `OnionPress.app/` — **gitignored build output**, assembled by `build/build-dmg-simple.sh`

## Why py2app
- Modern Macs do NOT ship a usable Python — `/usr/bin/python3` is just a shim that prompts to install Xcode CLI Tools
- Apple removed Python 2 in macOS 12.3 and has no commitment to shipping Python 3 long-term
- py2app bundles the Python interpreter + all dependencies into a self-contained .app so the user never needs to know Python is involved
- This is essential for a consumer app — cannot ask non-technical users to install Xcode Command Line Tools

## Build & Release Process
- MenubarApp built with py2app via `setup.py` (extracted from `build/build-dmg-simple.sh` lines 228-276)
- **After editing ANY `src/` file that the MenubarApp uses, you MUST rebuild the MenubarApp** via `build/rebuild-menubar.sh`. The py2app bundle contains compiled `.pyc` files — editing `src/` alone does NOT update the running app.
- **py2app entry point**: `MenubarApp/Contents/Resources/menubar.py` is the ONLY copy that matters at runtime — py2app `exec()`s it from `__boot__.py`. The copies in `lib/python3.14/` and `scripts/` are unused build artifacts. When hot-patching the installed app without a full rebuild, only update `Contents/Resources/menubar.py` (and its `__pycache__/` pyc).
  - `src/menubar.py` — main app (entry point)
  - `src/onionpress/` — shared package: backup, key_manager, setup_window, onion_proxy, onion_auth, onionheaven, updater, install_native_messaging, native_messaging_host, power, health, docker, colima, platform, config, containers, ui_helpers, settings_ui, browser, log_rotation, analytics_sharing, onionnames_*
  - `setup.py` — py2app config. Adding a new submodule to `onionpress` just means one new line in the `includes` list — no build-script change needed. Build scripts only copy the whole `onionpress` package into site-packages.
- **Release via GitHub releases only** (`gh release create`). Do NOT upload to Internet Archive.
- **Every release goes on `brewsterkahle/onionpress`, never a fork, and
  `build/release.sh` enforces it.** In a checkout whose `origin` is a fork
  (`ivar/onionpress`, upstream as the `brewster` remote), `gh` defaults to
  the fork: v2.5.4 was first published on `ivar/onionpress`, tagged at the
  fork's stale `main`; the Mac updater, Linux `onionpress update` and the
  README all read brewsterkahle's releases, so no user saw it, and the
  fork's CI ran the stale tag's workflows. release.sh passes
  `-R "$RELEASE_REPO"` (`brewsterkahle/onionpress`; `ONIONPRESS_RELEASE_REPO`
  only for testing) to every `gh release` call, and before creating a
  release, before anything is built, refuses unless HEAD is the tip of that
  repo's `main` (`gh api repos/…/commits/main`) and any existing tag of that
  name points there; it creates the tag with `--target <that sha>`. Check
  out upstream `main` (`git fetch brewster && git checkout brewster/main`)
  to release. Hand-run `gh release` commands need `-R brewsterkahle/onionpress`
  too. `tests/test_release_sha256sums.py`.
- **Cut releases with `build/release.sh`** — it builds BOTH artifacts and uploads both to one release, so the Linux `.deb` can't be forgotten. (It was: v2.4.101–v2.4.106 shipped only the `.dmg`, which 404'd the README's `releases/latest/download/onionpress.deb` link — that link resolves to the *Latest* release's asset of that name.) Bump + commit first, then run it. Cross-platform rule it enforces: the `.dmg` can only be built on macOS, so on **macOS** it builds `.dmg`+`.deb` and creates/updates the release with both; on **Linux** it builds only the `.deb` and *refuses to create* a release (a `.dmg`-less "Latest" would break the Mac link) — it only attaches the `.deb` to an existing release that already carries the matching `.dmg`. Always run the Mac side first.
- **Every release carries `SHA256SUMS`** (`<sha256>  onionpress.deb` / `onionpress.dmg`, sha256sum format), written by `build/release.sh` (on Linux it downloads the release's `.dmg` to hash it). A new release is created as a draft; the script downloads `SHA256SUMS` and the `.deb` back, checks them, and only then publishes. It fails instead of finishing a release without them (`tests/test_release_sha256sums.py`). Linux's `onionpress update` installs **only** a release's `onionpress.deb` whose sha256 is its `SHA256SUMS` line, whose package is `onionpress` of the tag's version, and that is newer than the installed one (`--force` reinstalls/downgrades; drafts never; pre-releases only with `--pre`; `--version X.Y.Z`; `--check` reports only), with `sudo apt-get install -y --no-remove`; a git checkout only with `--from-checkout` (developers). So a release without `SHA256SUMS` cannot be installed that way. The Settings page's update button on Linux only checks: handle-action `update` writes `update-result.json` (one GitHub lookup per 5 min) and never installs, since anything in WordPress can write `requested-action` (`tests/test_linux_release_update.py`). Signing `SHA256SUMS` with a pinned key is the planned next step.
- **A release's `SHA256SUMS` may be signed** (issues #246 and #386 step 2,
  branch `sec-246-signed-sums`, not merged): `src/onionpress/release_signing.py`
  holds `RELEASE_SIGNING_PUBLIC_KEYS` (Ed25519, hex; empty until the
  maintainer runs `build/release-key.py create <file>` and commits the
  public half), the `.sig` format (`SIG_ASSET`, one text file: header,
  key id, base64 signature; PyNaCl) and the policy `check_release_sums`:
  no key embedded, nothing enforced and the updaters log `NO_KEY_NOTE`;
  a key embedded, a release without `SHA256SUMS.sig` or with one that
  does not verify is refused. `build/release.sh` signs `SHA256SUMS` when
  `ONIONPRESS_RELEASE_SIGNING_KEY` names the secret key file (kept off
  GitHub and CI) and uploads the `.sig`, else warns. The Mac updater
  fetches the `.sig` (`_fetch_release_sig`, the fourth curl) and checks
  before the hash; Linux `update_fetch_release` downloads it (a failed
  download is a missing signature) and runs `onionpress
  release-signature verify <sums> <sig>` (0 verified or unchecked, 1
  refused, 2 unreadable) before the `.deb`. Open: CI's linux-package
  workflow rewrites the `.deb` line of `SHA256SUMS` after publication,
  which breaks a signature made on the Mac; either CI stops rewriting
  (the Mac-built `.deb` stays) or signs with a key in a GitHub secret
  (the threat this guards against). `tests/test_release_signing.py`.
- **The update check's curl names macOS's CA bundle only where it exists**
  (issue #355, `updater._curl_tls_args`): `--cacert /etc/ssl/cert.pem` was
  passed unconditionally, and Debian/Ubuntu/Raspberry Pi OS keep theirs at
  `/etc/ssl/certs/ca-certificates.crt`, so on Linux curl exited 77 before
  connecting. `check-for-update --json` (and `update --check --json`)
  always exits 0 and says `error` in its JSON: every consumer reads that
  field (the tray's `update_check_outcome`, the menubar, handle-action) and
  shows "Update check failed", never "Up to date".
  `tests/test_update_check_ca_bundle.py`.
- **What goes into `/opt/onionpress` is one list: `linux/install-tree.sh`.** The .deb (`collect_files` stages with it), `linux/install.sh` and `onionpress update --from-checkout` (both `install-tree.sh install`: staged beside the install dir, renamed into place, the old tree removed only after) all use it; a new file for Linux goes in its list, nowhere else. It refuses a missing source, a link or a special file before copying anything. `update --from-checkout` installs from the invoking user's own checkout (`$HOME/tmp/onionpress`, `$HOME/onionpress`) or root's `/opt/onionpress-src` (cloned from GitHub when missing), never another user's home; the checkout's files and the folders above it must be that user's or root's and not world-writable, it must be clean, and it only fast-forwards (no `reset --hard`). Plain `onionpress update` installs a release's checked `.deb` instead (previous bullet). `tests/test_linux_install_tree.py`.
- **`linux/install.sh` removes only what it made** (issue #346): the clone
  goes in `CLONE_DIR`, never `TMPDIR` (the user's temp folder, which the
  old `rm -rf "$TMPDIR"` deleted on a run from a checkout), removed by its
  `trap cleanup EXIT` and once installed. Never `rm -rf` a folder named by
  the environment. `tests/test_install_sh_clone_cleanup.py`.
- **`linux/install.sh` installs a release and parses whole** (issue #388):
  not run from a checkout, it clones the newest `v<x>.<y>.<z>` tag
  (`release_ref_to_install`: `git ls-remote --tags`, `sort -V`; nothing
  printed and the install stops when none can be read;
  `ONIONPRESS_INSTALL_REF=<ref>` for a developer's branch), never HEAD of
  main, so a push to main changes nothing new installs run as root and a
  fresh install's version is a release's. The body is `main() { ... }`
  with `main "$@"` last, so a `curl | bash` cut part-way executes
  nothing. The system Docker is disabled only when `docker ps -q` is
  empty (the .deb postinst's check), else left running beside the
  rootless one; Docker's apt key is checked against the published
  fingerprint (`DOCKER_APT_KEY_FPR`, `docker_apt_key_is_dockers`, gnupg
  among the prerequisites) before the repository is added; the
  postinst's fallback `usermod -aG docker` says the group is
  root-equivalent. Installing the release's checked `.deb` instead of a
  clone (the `update_find_release` path) is the next step and still
  open. `tests/test_install_sh_release.py` (the functions under bash
  with fake git and gpg; a truncated script runs nothing).
- **The .deb's postinst restarts the site on an upgrade** (issue #357):
  with `$2` set it runs `systemctl --user --machine=<user>@.host
  daemon-reload` and `try-restart onionpress.service
  onionpress-heartbeat.service` (the heartbeat's `After=` makes systemd
  stop it first, so /offline goes out, and start it last), then replaces
  the tray through `spawn_tray` (the first install's starter too). A
  failure prints the commands to run and never fails the install; prerm
  stays a no-op on upgrade. `onionpress update` restarts once more after
  apt-get returns (serial, never beside it). Before, the old units, daemon
  and tray kept running the old code until a reboot.
  `tests/test_deb_upgrade_restarts.py`.
- **Everything the Mac bundle is built from is pinned by hash** (issue
  #387, branch `sec-387-build-hashes`, not yet run on a Mac): every
  third-party download in `build-dmg-simple.sh` goes through
  `fetch_checked <sha256> <dest> <url>` against the `*_SHA256_*`
  constants at the top (Colima's from its release's `*.sha256sum`
  files, Lima's from its `SHA256SUMS`, Compose's from `checksums.txt`;
  the Docker CLI tarballs and the libsodium tarball hashed from a
  download, since neither publishes sums), and the binary cache's keys
  carry those hashes, so a cache made from other bytes is a miss.
  mkp224o is cloned at its tag and held to `MKP224O_COMMIT` with no
  fallback to master; libsodium is `SODIUM_VERSION` + `SODIUM_SHA256`,
  fetched outside the mkp224o subshell so a bad tarball ends the build.
  The Python side installs with `--require-hashes` from
  `build/requirements-macos.lock` (from `src/requirements.txt`) and
  `build/build-requirements-macos.lock` (pip, py2app, setuptools<81,
  wheel; from `build/build-requirements-macos.in`), all written by
  `build/refresh-python-locks.sh` (uv); `py2app-extensions.sh` takes
  PyNaCl's sdist and cffi's per-arch wheels from the runtime lock
  (`lock_block`) and refuses a single-arch extension of a package that
  lock lacks. Pillow (DMG background fallback) has its own
  `build/dmg-background-requirements-macos.lock`, installed only after
  py2app: in the build lock it sat in the py2app venv and broke the
  universal2 merge. Bumping a version means putting its new hash in the
  script and re-running the refresh. `tests/test_dmg_build_pins.py`.
- **A release's Mac app must be universal** (issue #326): 2.5.2 and 2.5.3 shipped an Apple-Silicon-only MenubarApp because `build-dmg-simple.sh` fell back to uv's single-arch Python without python.org's universal2 3.14. `release.sh` sets `ONIONPRESS_RELEASE_BUILD=1`, which makes the build refuse that fallback, then fails if any Mach-O in `OnionPress.app` lacks arm64 or x86_64 (`lipo -archs`). The `.deb`'s files are root's (`dpkg-deb --root-owner-group`; the manual path's tar too). CI runs on pinned `ubuntu-24.04` and Node 24 action majors. `tests/test_build_release_hygiene.py`.
- **CI builds and attests the `.deb` on every published release**
  (`.github/workflows/linux-package.yml`): `build/build-linux.sh` with the
  real `dpkg-deb` on `ubuntu-24.04`, `build/check-deb.sh --lintian`, then
  `gh release upload --clobber` over the Mac-built `onionpress.deb` (plus
  `onionpress_<version>_all.deb`) with an `actions/attest-build-provenance`
  attestation (`gh attestation verify onionpress.deb --repo brewsterkahle/onionpress`),
  and rewrites the release's `SHA256SUMS` line for `onionpress.deb` (the
  `.dmg`'s kept) and reads both back: `onionpress update` installs only a
  `.deb` matching its `SHA256SUMS` line.
  The Mac keeps uploading its own `.deb` first so "Latest" is never without
  one if CI fails. The Mac fallback in `build-linux.sh` must keep recording
  root ownership and `COPYFILE_DISABLE=1`: v2.5.3's package had every file
  owned by uid 502 and 215 AppleDouble `._` members. Run `build/check-deb.sh`
  (Linux) on any package before handing it to anyone. The release job
  refuses a tag that is not `v<src/menubar.py version>`.
  `tests/test_linux_package_workflow.py` locks the workflow's permissions
  split (write only in the release job) and the check script's requirements.
  On PRs touching `linux/` the workflow runs `linux/install.sh` twice on a
  fresh runner and checks the second run replaced the first (the nested
  `docker/docker`, `lib/onionpress/onionpress` of issue #316 that
  `install-tree.sh` ended).
- **Every workflow action is pinned to a commit SHA** (issue #389):
  `uses: owner/action@<40-hex> # vX.Y.Z`, the version comment being what
  the SHA was resolved from. A floating major tag is whatever its
  maintainer (or whoever holds their account) points it at, and the
  release job holds `contents: write`. `.github/dependabot.yml` bumps the
  SHA and the comment together, weekly, grouped. Bump by hand with
  `gh api repos/<owner>/<action>/git/ref/tags/<tag>` (dereference an
  annotated tag through `git/tags/<sha>`).
  `tests/test_build_release_hygiene.py` refuses an unpinned `uses:`.
- **Version bumping**: run `build/bump-version.sh X.Y.Z` — it updates all version locations automatically. The 2 canonical sources are `src/menubar.py` (`self.version`) and `app/Info.plist` (`CFBundleShortVersionString`). Derived locations (`src/onionpress/__init__.py`, `setup.py` which reads menubar.py dynamically, MenubarApp plist) are updated by the bump script or at build time. The quit log in menubar.py uses `self.version` dynamically. Docker containers get the version via `ONIONPRESS_VERSION` env var from the launcher script (which reads Info.plist).
- **py2app vs setuptools 81+ incompatibility** — setuptools 81 (released 2026-02-06) removed `dry_run` from `distutils.spawn()`, which py2app 0.28.9 passed. The build lock pins `setuptools<81` (from `build/build-requirements-macos.in`), and the build scripts' "py2app failed" retry reinstalls from that lock. py2app 0.28.10 (in the lock) no longer passes `dry_run`, so the pin is probably obsolete, but no build has tried setuptools 81+; it is kept on purpose until one does. Re-check it whenever the locks are refreshed: the steps to drop it are in the `.in` file's comment. Upstream: https://github.com/ronaldoussoren/py2app/issues/557

## Local Build Path
- **`docs/BUILDING.md` is the map** — every component, its command, its host-OS
  requirement and its network dependencies. `make doctor` reports missing tools.
- `build/build-images.sh` builds the tor/wordpress images locally (`make images`).
  `build/dev-up.sh` runs the stack on them (`make dev-up`).
- **Every `docker pull` is gated on `using_local_images()`** — one rule,
  `onionpress.containers.using_local_images`; both launchers' bash
  `using_local_images()` is a delegation to `onionpress.cli
  using-local-images` (Phase 4, #120), the menubar and `update-images`
  call it in-process. `tests/test_local_image_mode.py` runs the bash
  delegation and checks no pull escapes the gate.
- **The tor image is built on the Tor Project's own Onimages C Tor image**
  (`containers.torproject.org/tpo/onion-services/onimages/tor:trixie`,
  amd64 + arm64 since Onimages 0.3.0, 2026-09-24). Tor and the verified
  deb.torproject.org keyring come from it, so the Dockerfile no longer sets
  up that apt source or key. A tor image build is under a minute. The base
  ends in `USER debian-tor`; the Dockerfile must `USER root` again (the
  entrypoint drops privileges itself) and `CMD []` (a test enforces both).
  The base's numeric uids differ from the old image's; the entrypoint's
  per-start `chown -R /var/lib/tor` is what absorbs that — keep it.
- **C Tor is the only Tor implementation** (Arti removed 2026-09-24). Arti
  hosts a site acceptably (20/20 in a 2.6.0 test, after heavy circuit churn)
  but has no control interface, and sleep/wake DEL_ONION/ADD_ONION, the
  watchdog's stall recovery and the OnionHeaven takeover pipeline are built on
  the control port. `TOR_IMPL` is gone; a test fails if it comes back. What
  stays is the key format: the onion service key is delivered to the
  container as an OpenSSH PEM at `<name>/ks_hs_id.ed25519_expanded_private`
  in the `onionpress-onion-keys` volume (mounted at `/var/lib/onionpress-keys`),
  OnionHeaven exchanges keys as `arti_key_pem`, and `key-convert.py` /
  `key_manager.py` convert between that PEM and C Tor's key files. That
  volume is every install's identity and, on macOS, the only route by which
  the key reaches the container. It was `onionpress-arti-state` (layout
  `state/keystore/hss/<name>/`) until 2026-09-25; both launchers'
  `migrate_key_volume()` run `ContainerManager.migrate_key_volume`
  (`onionpress migrate-key-volume`, Phase 4 #120; the bash is the one-line
  delegation, exit status handed through with PIPESTATUS), which copies an
  old volume to the new name once, before first-run detection, and refuses
  to start if the copy fails — the first-run check keys off the new
  volume's existence, so skipping the migration would mint a new address.
  Keep the old name in the wipe lists (`.import-key-pending`, restore) so a
  replaced identity cannot be migrated back.
- **The key volume migration fails loudly, never into an empty copy.** It
  runs from the stack's pinned tor image (`containers.tor_image`, which
  `ContainerManager.ensure_tor_image` pulls by its pin when missing, never
  for a locally built stack; the launchers' remaining helpers use the same
  pin as `KEY_HELPER_IMAGE` / `ensure_key_helper_image`), `--network none`,
  `--entrypoint sh`, DAC_OVERRIDE only: the copy is root's, 0700/0600. The
  script is `containers.KEY_VOLUME_MIGRATION_SCRIPT`. It walks down to
  `state/keystore/hss` one listed folder at a time, so a folder it cannot
  list stops it (unreadable is not absent: it used to pass for a missing
  keystore and leave an empty copy the next start took for the key),
  refuses links and anything but folders and files, and checks every name
  and every file's bytes in the copy. `$DATA_DIR/.key-volume-migration-pending`
  is written before the copy and removed once it is verified: a start that
  finds it beside both volumes removes the unverified new volume and
  copies again; with the old volume gone it keeps the new one and drops
  the marker. Every identity wipe (key import, `.import-key-pending`, the
  Linux Settings page's generate-vanity and import-key-file, restores
  through `containers.forget_key_volume_migration`) drops the marker, or
  an old volume that survived the wipe would be copied back over the new
  key.
  Nothing starts tor before it: `compose up … tor` creates an empty
  onionpress-onion-keys. `start_containers`, the Mac `start-tor`
  subcommand and both launchers' `setup` run `if ! migrate_key_volume`
  first (test-enforced); `onionpress.cli start` and
  `ContainerManager.start_tor` do not run it and refuse while one is
  due (`key_volume_not_ready`: the old volume there and the new one absent
  or marked, or `docker volume ls` unanswered).
  `tests/test_install_invariants.TestKeyVolumeMigrationInPython` runs the
  Python against a fake docker (every case the bash harness ran until the
  body moved, plus the marker's timing and an unwritable marker);
  `TestMigrationScriptLocally` runs the script under sh. A failed first-run key
  seed removes the volume it made and stops the start, as does
  `cli.cmd_restore`'s, so Tor never starts without the key it was given.
- **All Dockerfile inputs are pinned** as `ARG` defaults in the Dockerfiles
  themselves (images by multi-arch index digest, mkp224o by commit, wp-cli by
  sha256). `MKP224O_VERSION` must match `build/build-dmg-simple.sh`.
  `build/base-image-digest.sh <ref>` resolves a tag to its multi-arch index
  digest on any registry, no daemon needed; it refuses single-platform
  manifests. containers.torproject.org does NOT keep old digests
  (a 2026-09-25 pin was 404 by 2026-09-30, failing the v2.5.0 publish):
  re-pin the Onimages base right before every image publish.
- **`build/build-dmg.sh` was deleted** — it thinned universal binaries to
  arm64-only and damaged real bundles via a lowercase-path match on APFS.
  `make build` now runs `build-dmg-simple.sh`.
- Generated assets: `build/make-icons.sh` (icns is byte-exact; menubar PNGs need
  ImageMagick), `build/build-extension.sh` (Firefox = `extension/` overlaid
  with `extension-firefox/`; keep only files Firefox needs to differ there,
  since an overlay file silently replaces `extension/`'s).
- **`.github/workflows/docker-publish.yml` needs nothing outside GitHub**: amd64
  on `ubuntu-24.04`, arm64 on `ubuntu-24.04-arm` (no self-hosted runner), and
  `IMAGE_NAMESPACE` defaults to the repo owner so a fork's "Run workflow"
  publishes `ghcr.io/<you>/…`. The stress worker gets its base via the
  `TOR_IMAGE` build-arg. `tests/test_publish_workflow.py` enforces all of it.
  On `development`, `IMAGE_PREFIX: dev-` names the images `dev-onionpress-*`;
  reset it to `""` when merging into `main` (the test fails the PR otherwise).

## Container Image Pins
- **`build/image-pins.env` is the single source of truth** for every image
  the stack runs: the two GHCR images and the four third-party ones
  (mariadb, autoheal, cloudflared, the autoheal socket proxy). Six files
  embed the literals (docker-compose.yml, docker-compose.cloudflare.yml,
  app/MacOS/onionpress, linux/onionpress, src/onionpress/containers.py,
  src/onionpress/launcher_ops.py).
- **Never hand-edit a digest.** Run `build/refresh-image-digests.sh` (or
  `--propagate` to re-apply the pins file without a Docker daemon).
  `tests/test_image_pins.py` fails the build on drift — this exists because
  v2.4.110 refreshed only docker-compose.yml and shipped two different Tor
  builds inside one install.
- Pins must be **multi-arch index digests** (`docker buildx imagetools inspect`),
  never a single-platform manifest digest. The third-party ones are resolved
  by `build/base-image-digest.sh` too, which refuses anything else; a plain
  refresh re-resolves their tags, `--mariadb <ref>` etc. move one to a new
  tag. **mariadb pins move forward only** (the datadir follows the newest
  server that ran on it; the script rejects a lower version).
- **The third-party images floated on `:latest` through 2.5.3** while the
  Mac launcher ran an unconditional `docker compose pull` on every start
  (issue #36). Both launchers now pull `--policy missing` at start and
  `start-tor` has no `--pull always`: a launch fetches only a pin a new
  release moved. The opt-in refresh paths (`update_images()`, Linux
  `onionpress update`, the menubar's Check for Updates) still pull
  unconditionally. `tests/test_image_pins.py` enforces all of it.
- **A launch refreshes the images once** (issue #36): `UPDATE_ON_LAUNCH=yes`
  is read by the Mac launcher's `update_images()` (in `start_containers`,
  before the `--policy missing` pull), nowhere in the menubar; the
  menubar's own `update-images` pass at launch (a second registry
  contact before any container started) is gone. `update_docker_images`
  is called by Check for Updates... only. What remains of the issue is
  the first launch's fetch of the images themselves, which no setting
  can avoid. `tests/test_launch_pulls_once.py`.
- **The refresh resolves digests from the registry** (`build/base-image-digest.sh`
  on `:latest`); the local `docker images` cache only with `--local` (issue
  #327: the cache pinned stale images in 2.5.3).
- **A release builds no images** (issue #327). Images are built only by
  "Run workflow"; on `release: published` the `tag-release` job points
  `:latest` and `:<version>` at the release's `build/image-pins.env` digests
  and reads them back. Before, a release rebuilt and moved both tags off the
  pinned images (2.5.2, 2.5.3). `tests/test_publish_workflow.py`.
- Exporting `ONIONPRESS_TOR_IMAGE` / `ONIONPRESS_WORDPRESS_IMAGE` repoints the
  whole stack at locally built images. See `docs/BUILDING.md`.

## A Damaged Key Never Becomes a New Address (issue #311)
- The delivered PEM (`<name>/ks_hs_id.ed25519_expanded_private`, onion-keys
  volume) is the identity; C Tor's `hs_ed25519_*` + `hostname` are a copy.
  Every start the entrypoint runs `key-convert.py check-ctor` and re-derives
  the copy unless it matches exactly (size, header, key, public key,
  hostname); old files go to `/var/lib/tor/key-set-aside/`, never deleted.
  No usable PEM and no secret key: refuse to start.
- tor-watchdog.py sends `ADD_ONION NEW:BEST` only when there is no trace of
  an address (no PEM, secret key, public key or hostname, even damaged),
  re-checked at ADD time. An unusable C Tor key falls back to the PEM;
  without one the service stays down with a loud log line. A minted key is
  kept in memory for the next DEL+ADD (it used to mint again on every
  wake/republish) and saved with O_EXCL, never over an existing file.
  `tests/test_onion_key_never_reminted.py`. Tor image change: needs a publish.

## Onion Service Recovery (issue #266)
- **The app's dual probe is the only measure of real reachability.** When it fails
  continuously for 10 min with Tor internally healthy (`RepublishTrigger` in
  `src/onionpress/health.py`, used by the Mac daemon `src/onionpress/daemon.py`
  (`check_status`, every status pass; the menubar shows its verdict) and, on
  Linux, by `src/onionpress/reachability.py` in the heartbeat daemon), the app writes a
  request file inside the tor container
  (`launcher_ops.request_watchdog_republish`) and `tor-watchdog.py` answers with
  DEL_ONION + ADD_ONION — fresh intro points, immediate descriptor upload. Without it,
  C Tor keeps a dead descriptor on the HSDirs until its next scheduled upload (60–120 min).
- **Linux does not run this loop yet** (issue #310): `linux/onionpress-service.py`
  carries the same probe, trigger and wake reset, but neither `install.sh` nor
  `build-linux.sh` installs it and no systemd unit runs it.
- Rate limits: one request per 15 min on the host, one forced republish per 15 min in
  the watchdog, never within 5 min of an HS_DESC UPLOADED event. Takeover codes never
  trigger it (the heartbeat's /online is the reclaim path).
- `REPUBLISH_ON_UNREACHABLE=no` in `~/.onionpress/config` makes it log-only.
- **On Linux the loop runs in `onionpress-heartbeat.py`** (issue #310), on every
  node including the hub and whatever the OnionHeaven opt-in says: until then it
  lived in `linux/onionpress-service.py`, which no installer shipped and no unit
  ran (deleted). The loop writes `~/.onionpress/reachability.json`; the bash
  launcher's `write_status` uses that verdict while it is fresh instead of its
  own probe through Tor. Wake resets come from the SleepInhibitor's
  PrepareForSleep(false) and, without D-Bus, from a wall-clock vs monotonic gap.
  `tests/test_linux_recovery_loop.py` also requires every Python entry point in
  `linux/` to be installed by both `install.sh` and `build-linux.sh`.
- **The dual probe fetches `/.onionpress-health`**, a static file Apache serves from
  the WordPress image (`onionpress-health.conf`, no PHP), and checks the body for
  `onionpress-health`; `PROBE_PATH`/`PROBE_BODY_MARKER` in `health.py` and the Alias
  in the conf are test-locked together. Older images answer 404 and the probe falls
  back to `/` for an hour, so the app may ship before the image, never the reverse
  constraint. The failure path runs the isolated retry, self-probe and onionheaven
  health check concurrently (~50 s worst case, was ~150 s); onionheaven's health is
  judged by `status/circuit-established`, then the hub, then two well-known onions
  (torproject, duckduckgo — only contacted while the hub is down). Each cycle logs
  one `probe verdict=… ext=… self=… hub=…` line with `*_tor_ms`/`*_app_ms` (Tor path
  vs server time to first byte); healthy cycles log it at most every 5 min.
- **The republish window is rate-based, not continuous** (`RepublishTrigger`):
  a request is due when ≥ 50% of the probes in the last 10 min failed with a wedge
  code, the window (opened by the first failure) is ≥ 10 min old, and the current
  probe failed; 5 passes in a row end it. A single pass no longer resets it — a
  descriptor with two of three intro points dead fails ~2/3 of visitors and never
  fails continuously, so the old rule never fired for it.
- **On the failure path the app asks both tors why** (`HealthChecker.classify_wedge`,
  cached 2 min): `GETINFO hs/service/desc/id` on onionpress-tor vs an `HSFETCH` +
  `hs/client/desc/id` on onionheaven. Classes: `desc-missing` (every HSDir FAILED
  NOT_FOUND), `desc-stale` (same signing cert, lower revision — publication failed),
  `desc-current` (HSDirs serve exactly ours → intro points dead), `desc-other`
  (different lineage: period skew or pre-DEL+ADD incarnation), `fetch-error`,
  `unknown`. Revision counters are only comparable within one signing-cert lineage
  (v3 OPE per period), hence the cert check. `desc-missing`/`desc-stale` get a 5-min
  window instead of 10 (`REPUBLISH_FAST_CLASSES`); the watchdog's 5-min-after-upload
  guard caps a false fire during benign propagation. The class rides in the `probe`
  log line (`why=… fetch=… ours_rev=… theirs_rev=…`), `HealthResult.wedge_class`,
  and the reason string sent to the watchdog.
- **Both wake handlers reset the republish window** (`_republish_trigger.reset()` in
  the Mac daemon's `handle_wake`, `monitor.republish.reset()` on Linux, test-enforced): the trigger uses wall
  clock, so a failure seen just before sleep would otherwise fire DEL+ADD on the
  first probe after wake, on top of the watchdog's own wake ADD.
- The request path is a literal in both `launcher_ops.py` and `tor-watchdog.py`; a test
  fails if they drift. The watchdog ships in the tor image, so this needs an image
  publish + `build/refresh-image-digests.sh` to reach users.
- **The watchdog is supervised (issue #313).** Every entrypoint mode starts it
  through `supervise_watchdog` (never a bare `python3 /tor-watchdog.py &`):
  each exit is logged, restarts back off 1 s → 60 s, and a watchdog whose
  `/run/tor-watchdog/alive` is 300 s old is killed (a clock gap in the
  supervisor's own loop, i.e. a suspended VM, skips that check). The
  supervisor is a function in entrypoint.sh, not a script named
  `*tor-watchdog*`: the hosts signal `pgrep -f '[t]or-watchdog'`, and USR1
  kills a shell. A restarted watchdog keeps the host's sleep state
  (`/run/tor-watchdog/sleeping`, written on USR1, removed on USR2) and a
  pending republish request; the supervisor clears the run dir once per
  container start. A signal sent while the watchdog is down is still lost.
  `request_watchdog_republish` returns `requested` / `no-watchdog` (file
  written, but no `python3 /tor-watchdog.py` process or alive file > 120 s
  old) / `failed`. `tests/test_tor_watchdog_supervised.py`.

## License Declarations
- OnionPress is **AGPL-3.0-or-later** (that SPDX form everywhere, never a
  bare "AGPL-3.0"); the copyright line is "Copyright (C) 2026 Internet
  Archive and OnionPress contributors" (the first commit, 2026-01-31, was
  version 1.0.0 under MIT; the metainfo said MIT until 2026-10-15). It is
  at the top of LICENSE (above the FSF text, which stays whole), in
  README's License section, as `NSHumanReadableCopyright` in app/Info.plist
  and setup.py (the same string), and in `linux/copyright` (DEP-5, the
  .deb's `/usr/share/doc/onionpress/copyright`).
- The entry points carry the GNU short notice after the shebang (SPDX line,
  copyright, the three paragraphs as LICENSE's "How to Apply" words them):
  src/menubar.py, src/onionpress/__init__.py, both Mac launcher scripts,
  linux/onionpress, onionpress-tray, onionpress-heartbeat.py, and the tor
  and wordpress image entrypoints. A new entry script gets the same block.
- Every WordPress plugin header and the theme's style.css say
  `License: AGPL-3.0-or-later` + `License URI:
  https://www.gnu.org/licenses/agpl-3.0.html`; a new plugin needs both.
- **The DMG ships the bundled binaries' licenses**:
  `Contents/Resources/THIRD-PARTY-NOTICES.txt`, written by
  build/build-dmg-simple.sh from Colima (MIT), Lima (Apache-2.0 + NOTICE),
  the Docker CLI and Compose (Apache-2.0 + NOTICE), mkp224o (CC0, its
  COPYING.txt) and the libsodium linked into it (ISC). `license_file` takes
  each text from the release archive when that carries it, else from
  `build/.cache/licenses/`, else from the project's repository at the pinned
  tag; a missing or empty text fails the build. Bumping one of those
  versions needs nothing else, but a project that renames its license file
  does. The .deb bundles nothing third-party (images are pulled, the rest
  is in Depends). `tests/test_license_declarations.py` runs the staging and
  the writer against a curl stub.
- **And the MenubarApp's Python side**: once py2app has built it, the DMG
  build runs `build/python-notices.py` under the build venv's interpreter
  and appends, in the same entry format, the interpreter (the standard
  library's LICENSE.txt, PSF-2.0) and every installed distribution with a
  module in the bundle (a package dir under `Contents/Resources/lib/python3.X/`,
  a module in `lib-dynload/`, a member of `site-packages.zip`), with the
  texts out of its `*.dist-info` (`License-File` entries, else
  LICENSE*/COPYING*/NOTICE* there); pip's build-only tools (pip, py2app,
  macholib, modulegraph, altgraph) have no module in the bundle and are
  left out by name in the log. A bundled distribution without a text or a
  declared license, no LICENSE.txt beside the standard library, or a
  `src/requirements.txt` package the bundle check does not find fails the
  build; the output is appended whole or not at all. pyobjc-core's wheel
  ships no text: `LICENSE_URLS` in the script fetches `pyobjc-core/License.txt`
  from the pyobjc repository at the version's tag into `build/.cache/licenses/`;
  a new dependency whose wheel carries none needs a row there. PyNaCl's
  dist-info carries libsodium's ISC too (`licenses/LICENSE.libsodium.txt`),
  so that comes along. The log's `NOTE:` line names bundled modules
  belonging to no distribution, the standard library or OnionPress. Run
  against the real wheels' metadata from Linux (2026-10-15), not yet inside
  a `make dmg`; `build/rebuild-menubar.sh` does not refresh the file. The
  tests run the script and the build script's block against a fake build
  venv and bundle.

## Test suite
- `make test-unit` runs `tests/` with `unittest discover` (uv, Python 3.14,
  PyNaCl); **there is no pytest runner**, so every test file must hold
  `unittest.TestCase` classes: a plain class with bare asserts is
  collected by nothing (the stress orchestrator's two files were, until
  2026-10-23). On a Mac that runs OnionPress set
  `DOCKER_HOST=unix:///var/empty/no-docker-for-tests.sock`: the
  docker-backed tests then skip instead of reaching the live site (CI's
  macOS job does the same; it runs on pull requests only, the Linux job
  is the required check).
- `make coverage` is the same run under `coverage` (`.coveragerc`:
  branches; src/onionpress, linux and the tor image's scripts; tests,
  tests/stress and setup.py left out), then `build/coverage-floors.sh`:
  total 63 %, `cli.py` 97, `app_commands.py` 94, `handle_action.py` 95,
  `confirm.py` 98, `config.py` 85, each a point or two under the measured
  value. Raise a floor when the tests catch up; never lower one to pass.
  CI runs the three steps on the Linux job and uploads `coverage.xml` +
  the HTML report. `tests/test_coverage_gate.py` holds the wiring.
- **Every `onionpress` command handler has a test of its own**:
  `tests/test_cli_dispatch.py` is the census (main() reaches each handler
  with the parsed arguments); `tests/test_cli_commands.py` runs the
  handlers in `cli.py` against stubs (OnionPressCLI built with Docker,
  the port probe and secrets patched, each module patched where the
  handler imports it: update, uninstall and reset at a terminal, the
  lifecycle commands, restore's four hub-data fates and its JSON answer,
  the staging helpers, the hub recovery key, update-images,
  generate-vanity, the Settings page's commands, the password and
  recovery-key readers); `tests/test_cli_edges.py` covers the paths
  app_commands, confirm and handle_action leave out, reusing the
  harnesses of `test_cli_app_commands.py` and `test_handle_action.py`.
  A new handler or branch gets its case there; coverage of `cli.py`
  (99 % on 2026-10-25) is held by the floors above.
- **A new `onionpress` subcommand needs a row in
  `tests/test_cli_dispatch.py`**: the census walks the parser
  `cli.main()` builds (nested subparsers too) and fails on an
  `add_parser` without a row saying which handler it reaches with which
  parsed arguments and what `main` returns (stubs only; the fast paths
  are checked never to construct `OnionPressCLI`). A grouping subcommand
  goes in `PARENTS`.

## Security
- **Database passwords are randomly generated per install** — never use defaults or hardcoded passwords. The `ensure_secrets` function generates unique passwords with `openssl rand` on first run, saved to `~/.onionpress/secrets`.
- Do not commit or log database passwords.

## WordPress Branch Moves With the Image
- A site's WordPress core lives in its volume; the base image's copy is used
  only on a first start. `WORDPRESS_BRANCH` in the wordpress Dockerfile (build
  fails if the base carries another branch, written to
  `/usr/local/share/onionpress/wordpress-branch`) is the branch the security
  audit moves older sites up to at start, over Tor; same-branch releases as
  before; never past it, never down (`op_pick_core_update`). Raising it means
  testing the mu-plugins and pinned plugins on the new branch first
  (docs/BUILDING.md, "Moving to a new WordPress branch").
  `tests/test_wordpress_branch_upgrade.py`. Needs an image publish.

## A Core Package Is Checked Before It Is Installed (issue #395)
- `onionpress-core-update.php`'s `download` fetches a package only from
  `https://downloads.wordpress.org` (`op_package_url_allowed`; the offer
  names the URL), takes the version from the package name (and the
  audit's `$target`, which must agree), fetches WordPress.org's md5
  list for it over Tor (`op_fetch_checksums`, shared with `verify`) and
  holds every member of the zip to it (`op_verify_core_zip`, ZipArchive:
  a listed file's md5 must match, a member the list lacks is refused
  unless under `wp-content/`, a listed file outside `wp-content/` must
  be there, nothing outside `wordpress/`) before it answers ok; a
  refused package is removed. Before, size + `PK` was the whole check
  and the md5 comparison ran after `wp core update`, report-only. The
  image build checks ZipArchive is there (without it every update is
  refused). Needs an image publish. `tests/test_core_update_verified.py`
  (the PHP halves under a php with the zip extension; CI's runner has
  one, `php:8.3-cli` does not).

## Settings Page Config Writes Are Allowlisted
- The WordPress Settings page asks for config changes through
  `config-updates.json` in the onionpress-data volume, which anything running
  in WordPress can write. Both hosts apply it only through
  `config.apply_settings_updates`, run as `onionpress apply-config` (Mac:
  `handle_action.apply_config`, which `onionpress handle-action` runs on
  every status tick; Linux: bash `apply_config_updates` → `onionpress.cli
  apply-settings-updates`): keys in `SETTINGS_PAGE_KEYS`, each value
  checked (yes/no enums, integer ranges, the token's base64 charset, no
  control characters). Everything else is dropped and logged by key name
  only. The menubar applies nothing itself: the CLI leaves the accepted
  keys in `.config-changed` for it (issue #366).
- **The page's requests (`requested-action` and the files beside it) are
  handled by `onionpress handle-action` on both platforms**
  (`src/onionpress/handle_action.py`, issue #366): Linux's
  onionpress-watcher.timer runs it every 10 s, the Mac daemon's status
  loop (`daemon.poll_settings_page`) on every tick. The verbs are fixed (`ACTIONS`:
  start, restart, stop, check-reachability, import-key-file, create-backup,
  restore-backup, refresh-status, update); every action runs through the
  launcher (`Host.run_launcher`: start, restart, stop, apply-config,
  write-status, import-key, settings-page-backup, settings-page-restore,
  settings-page-update-check, update), so the Linux launcher keeps its bash
  bodies behind those internal cases and the Mac launcher delegates them to
  `onionpress.cli` (`cmd_settings_page_backup`, `cmd_settings_page_restore`).
  Linux refuses import-key-file and a restore of another address; `update`
  only checks on **both** platforms (through 2.5.6 the Mac ran `update
  --yes` from the page's request: an unattended download and install of
  the latest GitHub release, whose DMG nothing verifies beyond TLS, on the
  say-so of anything running in WordPress; now `update --check --json`
  and `MAC_UPDATE_AVAILABLE_MSG` pointing to Check for Updates..., which
  asks the person at the Mac; the `relaunch` state key is gone with it);
  on the Mac the first two stop at `{"confirm": "address-change"}`
  (kept in `~/.onionpress/pending-action/`, 0700, dropped after 10 minutes
  without an answer) until the menubar asks and runs `handle-action
  --confirmed address-change` or `--cancel` (the daemon puts the question
  in its state file, `pending_question`, and never answers it itself).
  `tests/test_handle_action.py`
  runs it against a stub launcher and a stub docker.
- **A result answers one request** (issue #356): the page writes an id
  (`wp_generate_password(16)`) to `request-id` before `requested-action`
  and unlinks the action's own result file; `handle()` reads the id with
  the request, keeps it only as `[A-Za-z0-9_-]{1,64}`
  (`checked_request_id`, the bad one dropped and never logged), removes
  the request, the id and the action's result file (`RESULT_FILES`)
  before the action runs, and `Host.write_result` adds it as
  `request_id` to every result of that request (a pending Mac question
  keeps it in `pending-action/request.json` for its answer; Linux's
  `settings-page-update-check` gets it as `ONIONPRESS_REQUEST_ID` and
  `update_settings_page_put_result` echoes it, from the 5-min cache too).
  The page's poll (`onionpress_action_result_is_this_requests`, the
  service result too) takes a result carrying the request's id; one
  carrying another id is an earlier request's; one without any (a host
  from before the id) only when its mtime is not older than the request;
  a page that sent no id gets the old rule. Before, the first poll took
  the last run's file for this run's and stopped.
  `tests/test_settings_page_results.py` (the PHP rule under php where
  installed), `TestRequestId` in `test_handle_action.py`.
- **The page can make the host do only so much** (issue #396): `Host.read_shared`
  reads a request file through `head -c` up to `READ_LIMITS[name]` (else
  `READ_LIMIT`, 64 KiB; the action and id 256 bytes, passwords 4 KiB) and
  drops, by name in the log, a file over its limit, never a cut-short
  value; each action runs at most once per `MIN_INTERVAL[action]` seconds
  (stop, start, restart and the long ones 60, check-reachability 30,
  refresh-status free), the last times in `$DATA_DIR/.handle-action-times.json`
  (`throttle`; a request inside the interval gets a failed result naming
  the wait); and the request's text reaches the log only once it is in
  `ACTIONS`, an unknown one as `unknown action dropped (N bytes)`, since
  the log goes back to WordPress as recent-logs.txt and out with
  analytics sharing. `TestBoundedReads`, `TestThrottle` in
  `tests/test_handle_action.py` (its `_Case` zeroes the intervals).
- **The owner's GETs with a side effect carry a nonce** (issue #402):
  `wp_ajax_onionpress_poll_action` (unlinks service-result.json once
  read) and `wp_ajax_onionpress_check_update` (a request through Tor, a
  transient) `check_ajax_referer` their own action right after the owner
  check, the page's script sending `_wpnonce`; `/onionpress-status-json`
  writes `requested-action=refresh-status` only with the status page's
  nonce (`onionpress_status_refresh_allowed`, `wp_verify_nonce` on
  `_wpnonce`) and never over a request already there (it clobbered a
  pending owner action), and every reply carries `refresh_nonce` so a
  page left open keeps a fresh one. Without the nonce the data still
  comes back; only the write is skipped. A new `wp_ajax_` handler in the
  settings plugin needs a `check_ajax_referer`
  (`tests/test_settings_page_nonces.py`, a census).
- **The page's Stop and Restart are the launcher's** (issue #354): on
  Linux they reach its `stop)`/`restart)` cases, which stop the heartbeat
  client before the containers (so it sends /offline to the hub) and start
  it again after a restart. After the page's Stop the `onionpress.service`
  user unit is still active (its `start` process keeps its status loop and
  the PID lock), so `onionpress start` is refused and `systemctl --user
  start onionpress` does nothing: the page's hint says `onionpress
  restart` (it named `sudo systemctl start onionpress`, a system unit
  current installs lack). `tests/test_linux_settings_page_stop.py`.
- **Share Now is taken by the host's long-running process on both
  platforms** (issue #351): the page's REST endpoint touches
  `/var/lib/onionpress/.upload-analytics` (www-data's), `onionpress
  upload-analytics` touches `~/.onionpress/.upload-analytics`; the Mac
  daemon's status loop and Linux's heartbeat client
  (`_poll_upload_trigger`, every cycle of both its loops, the volume file
  removed as www-data) take both and wake the upload loop
  (`analytics_sharing.trigger_upload`), which uploads only on
  `SHARE_ANALYTICS_WITH_ONIONHOME=yes`; with it off the request is logged
  and nothing is sent. Until then nothing on Linux read the file while
  the page said "queued". Both launchers' `upload-analytics` is
  `analytics_sharing.request_upload` (the CLI subcommand), which refuses
  while sharing is off or nothing runs to take the request (Mac:
  `daemon.pid` alive; Linux: the heartbeat unit active); the page answers
  "off" instead of "queued" when config-current.json says so. The marker
  name and path are literals in `analytics_sharing`, `daemon.py`, the
  heartbeat and the page; `tests/test_settings_page_share_now.py` ties
  them.
- Never add image overrides, `ONIONHEAVEN_ADDRESS` (the heartbeat sends the
  onion key to it), `ONIONHEAVEN_ACCEPT_REGISTRATIONS`, `ADDRESS_PREFIX`,
  ports, paths or secrets other than `CLOUDFLARE_TUNNEL_TOKEN`. The page
  shows the prefix and hub address read-only; the hub address changes in the
  Mac Settings dialog or `~/.onionpress/config`.
- The token field is write-only: blank keeps the saved token, a checkbox
  removes it. Log config changes through `describe_change` /
  `loggable_value` so `SECRET_CONFIG_KEYS` values never reach a log.
  `tests/test_settings_page_updates.py` locks the page's fields, choices,
  ranges and token pattern to the allowlist.
- **Every page key reaches a reader** (issue #353,
  `tests/test_settings_page_keys_are_read.py`: a census row per key in
  `SETTINGS_PAGE_KEYS`, the host file that reads it or, for the hub's
  takeover tunables, the compose mapping and the image constant). Both
  launchers and `ContainerManager._build_env` export
  `ONIONHEAVEN_MAX_SERVICES` and `ONIONHEAVEN_PROPAGATION_DELAY` from the
  config (the Mac side never did; `_build_env` falls back to the default
  for a value the check refuses); compose hands the delay to tor and
  onionheaven as `TOR_PROPAGATION_DELAY` (web-server.py reads it too; tor
  never got it) and the cap to onionheaven and onionheaven-farm under its
  own name (it went to onionheaven as `ONIONHEAVEN_TAKEOVER_SCALE_THRESHOLD`,
  which nothing read). One default, 50 and 180, in config, compose, the
  image and the page's placeholder (`DEFAULTS` said 10). Both are in
  `NEEDS_RESTART`. `ONIONHEAVEN_HUB_THRESHOLD` is gone: nothing ever read
  it.
- **`onionpress config get|set|unset|list [--json]` is the one writer of
  `~/.onionpress/config` for the apps** (issue #365, CLI-first phase 1;
  plan: https://claude.ai/artifact/JgQYk8MQmW3ZixVYj4MS78). Its key set is
  `config.CLI_CONFIG_KEYS`: `SETTINGS_PAGE_KEYS` (the same check objects)
  plus `ADDRESS_PREFIX`, `VM_CPU`, `VM_DISK`, `INSTALL_IA_PLUGIN`,
  `INSTALL_CACHE_ENABLER`, `REPUBLISH_ON_UNREACHABLE`, `ONIONHEAVEN_ADDRESS`
  and `ONIONHOME_ADDRESS` (the `.onion` format check of
  `validate-oh-address`; whether it answers as a hub stays the Settings
  dialog's question) and the three `ONIONNAME*` values the apps record at
  setup (`RECORDED_KEYS`). Hand-edit only, refused: every `ONIONPRESS_*`
  key (image overrides, ports, paths, bind host) and
  `ONIONHEAVEN_ACCEPT_REGISTRATIONS`. **A new setting gets its `config set`
  entry first** (key, check, `CONFIG_KEY_HELP`, `NEEDS_RESTART` or
  `CODE_DEFAULTS` as needed); a UI only calls the command, and
  `tests/test_cli_config.py` fails on a key in `DEFAULTS` or the template
  that is neither settable nor hand-edit only.
- A secret is taken only as `config set KEY -` with the value on stdin
  (`tests/test_secrets_off_argv.py`); `get`/`list` show it as
  `<set>`/`<empty>`. A change is logged through `describe_change`, the key
  goes into `~/.onionpress/.config-changed` for the running Mac daemon
  (its `check_status` takes it with `take_config_changes` →
  `Daemon.apply_config_changes`: caffeinate, the analytics upload, the
  tunnel flag, the hub address, status.json; the menubar applies nothing), and
  `config.apply_side_effects` installs or removes the Mac login item
  (`onionpress.login_item`, shared with the menubar) or runs
  `systemctl --user enable|disable` on Linux. The keys in `NEEDS_RESTART`
  are printed as needing a restart (the Settings dialog shares the set).
- Callers: `menubar.write_config_value` runs the launcher's `config set`
  (so does the Settings dialog, through its one `write_config` callback);
  the Linux tray's setup write runs `onionpress config set`; both bash
  launchers' `config)` and `onionheaven)` cases are one-line delegations to
  `onionpress.cli` (Linux's `onionheaven_cmd` too; its bash body moved to
  `onionheaven.run_onionheaven_command`). `onionpress onionheaven
  enable|disable|status [--json]` writes the choice through the same path
  and reads the registration record, no network. The Settings page's
  `config-updates.json` channel and `SETTINGS_PAGE_KEYS` are unchanged.
- Nor does the token reach WordPress in a backup. The page's backup is
  handed to WordPress, with a password from there, so it is
  `create_backup(include_hub_data=False, include_host_secrets=False)`
  (Linux `--without-hub-data --without-host-secrets`): its config is cut to
  `SAFE_CONFIG_KEYS` (`backup.wordpress_safe_config`, what status.json
  shows), Linux's healthcheck key is left out, and `metadata.json` gets
  `excludes_host_secrets` + `secret_config_left_out` (names). The site's
  onion key stays on Linux; the Mac leaves it out too (next section). App
  and CLI backups keep the whole config. No restore takes the token from any backup
  (`RESTORABLE_CONFIG`): this install's stays, and `restore_backup_config`
  tells an owner without one to enter it again.
  `tests/test_backup_host_secrets_off_wordpress.py` keeps the census.

## WordPress Host Controls Are the Owner's
- OnionPress is a multisite: each additional user administers their own
  subsite, so `manage_options` is a per-site capability anyone can hold.
  Never gate on it anything that reaches the host (the request, config,
  backup, restore and key files in `/var/lib/onionpress/`) or every site.
  Use `onionpress_settings_owner_allowed()` (logged in + `is_super_admin()`:
  the owner, whom multisite-install/-convert makes super admin; on a single
  site, an administrator), checked in every handler, AJAX endpoint and REST
  `permission_callback`, not just the menu.
- The OnionPress menu stays on every site for its administrators; a subsite
  administrator's page shows only Following, which lives in that site's
  options.
- Same rule (`is_super_admin()`) for the other shared state: the onboarding
  wizard (network-wide `onionpress_onboarded`), the Wayback page and its
  actions (the sweep archives every site with the owner's archive.org
  account), the blog importer's WXR snapshots and the My Creations upload
  (both write into the owner's `~/OnionPress/Creations`; `upload_files` is
  per-site too). The social importers stay per-site on `manage_options`.
- `tests/test_settings_owner_only.py` enforces it, including a census: only
  the owner-gated settings and status plugins name host action files in
  `/var/lib/onionpress/`.
- The backup download (`wp_ajax_onionpress_download_backup`) sends and then
  deletes only `OnionPress-<≤8 chars>-<YYYY-MM-DD-HH-MM>.zip`, a regular
  file directly in `/var/lib/onionpress/` (basename, strict pattern, no
  symlink, realpath check): that folder also holds `backup-password` and the
  config. Changing the create-backup filename in `handle_action.py`
  (both platforms) means changing that pattern; `tests/test_backup_download.py`
  ties them together.
- **A key import never loses the current key.** Every one (`onionpress
  import-key`, and the Mac page's import through `handle-action`) goes
  through the launcher's `replace_onion_key`: the key is checked
  (`key_manager.check_expanded_secret_key`: 64 bytes, bit 255 clear,
  nonzero mod L; the public key is libsodium's, `derive_public_key`)
  and staged, a copy of every key in use goes to
  `~/.onionpress/replaced-keys/<address>-<time>/` (host only, 0700; no copy,
  no import), and only then are the hubs released, OnionPress stopped,
  the staged folder swapped in as `vanity-keys/` and `.import-key-pending`
  set (the start removes the old volumes). The page used to remove the
  volumes and vanity-keys/ before decoding the key: garbage minted a new
  address. On a Mac the page's import needs the person at the Mac:
  `handle-action` runs `import-key - --check --json` (the key on stdin; a
  refused key never reaches a dialog), stops at `{"confirm":
  "address-change", "current", "new"}`, the menubar shows a native dialog
  with both addresses (Cancel is the default; no answer in 5 minutes is
  Cancel), and only Replace runs `handle-action --confirmed address-change`,
  which runs `import-key - --confirmed address-change` (the older
  `ONIONPRESS_IMPORT_KEY_CONFIRMED=yes` still counts; the launcher's
  terminal question would read the key's end of input and cancel). Linux
  has no one to ask: the handler refuses the page's import-key-file
  (pointing to `onionpress import-key`), and generate-vanity is gone from
  the page and the handler. `tests/test_onion_key_replacement.py`.
- **Code in WordPress never reads or replaces the site's own onion key
  without the owner at the host** (decided 2026-10-14). The Mac page's
  backup leaves it out (`create_backup(include_onion_key=False)`: no
  `tor-keys/`, metadata `excludes_onion_key`, `ONION_KEY_LEFT_OUT_NOTE` as
  the result's `note` and in README.txt; every `create_backup` call says
  which, test-enforced). Linux's page backup keeps it for now (headless
  installs back up there). A backup without a key restores onto the
  install's own address on every path, never minting one:
  `staged_onion_key_address` is '' (and refuses a backup whose metadata says
  no key but which holds one: dialogs word themselves on the flag);
  `restore_from_backup` writes no key, `prepare_install_from_backup` seeds
  nothing (`keep_installed_address`: the served key's address, else
  `NO_KEY_TO_KEEP_MSG`; the welcome screen refuses such a backup up
  front), `cmd_restore` keeps the key volumes and releases no hub, `setup
  --from-backup` reimports only when `SETUP_BACKUP_KEY_RESTORED=yes`, and
  Linux `restore_backup` reports the kept address. A page restore of a
  backup of another address than the served one (`served_onion_address`):
  on a Mac, `restore_from_backup(confirm_address_change=)` in
  `cli.cmd_settings_page_restore` raises the "address-change" question
  (exit 4), `handle-action` keeps the upload and password in
  `pending-action/`, and the menubar asks the key import's dialog
  (`_ask_to_replace_address`: Cancel default, no answer in 5 minutes is
  Cancel) before anything is touched; on Linux refused
  (`ADDRESS_CHANGE_REFUSED_MSG`, `restore_backup --from-settings-page`,
  pointing to `onionpress restore <file>`). Same-address restores go
  ahead. On an OnionHeaven hub or OnionHome, or a node that cannot say,
  the page restores nothing on either platform
  (`settings_page_restore_node_refusal`, the backup check's fail-closed
  logic; Linux `settings-page-restore-node-check`), before the upload is
  opened. `tests/test_settings_page_onion_key.py`.

## The Mac Daemon (issue #367, CLI-first phase 3)
- **`onionpress daemon` (`src/onionpress/daemon.py`) runs every loop the
  menubar used to run in-process**, as the Mac counterpart of
  `linux/onionpress-heartbeat.py`: `check_status` (containers from `docker
  ps -a` by compose label and the tor container's hostname, not the
  launcher's `status`/`address`; internet; WordPress; the dual probe with
  `RepublishTrigger` and `classify_wedge`; status.json, version,
  config-current.json and recent-logs.txt to the volume as www-data), the
  OnionHeaven heartbeat and upkeep (`onionheaven.*` with the daemon as the
  `app`, under `_HUB_LOCK`), the handle-action tick (`poll_settings_page`,
  one run at a time), the image monitor, the three log
  captures (`RotatingLog`), caffeinate (`onionpress.power`), the My
  Creations thumbnails, the browser extension's HTTP proxy, port-offset
  detection at start, and sleep/wake through IOKit
  (`mac_power.MacPowerHandler`; the main thread runs the CFRunLoop a
  second at a time, `mac_power.run_loop_once`, so Python's signal
  handlers still run). The menubar (`src/menubar.py`) is a view: menu,
  dialogs, notifications, the browser items, the welcome and setup
  windows, and starting the daemon when there is none. `tests/test_mac_daemon.py` (the daemon against stub
  docker/health/launcher; the menubar census: no loop that names docker,
  the hub or Tor, no timer but the splash tips).
- **launchd**: `~/Library/LaunchAgents/org.onionpress.daemon.plist`
  (`daemon.agent_plist`): `<bundle>/Contents/MacOS/onionpress daemon`,
  `RunAtLoad`, `KeepAlive {SuccessfulExit: false}` (a crash or `kill -9`
  is restarted after 10 s; the daemon exits 0 on TERM so `daemon stop`
  and a `quit` stay down), `AbandonProcessGroup` (an `update` the
  daemon's own tick started must outlive the daemon it stops), stdout and
  stderr to `~/.onionpress/logs/daemon-std{out,err}.log`, one agent per
  macOS user. `onionpress daemon start|stop|status|offline [--json]` are
  the CLI (`daemon.start_agent`: install the plist, bootstrap or
  kickstart; `stop_agent`: bootout and wait; `request_offline`: USR1 and
  wait for `.daemon-offline-ack`); the launcher's `start` runs `daemon
  start` before the containers, `stop` runs `daemon offline` before
  `stop_containers` (the hub hears /offline while the tor container is
  up, as the menubar's Stop did), `quit` runs `daemon stop` first
  (`.daemon-quitting` tells the daemon to send /offline on its way out;
  `--no-offline` for an update relaunch) and takes `--leave-menubar`
  (the menubar's own Quit). The menubar starts the agent when neither
  `daemon.pid` nor `launchctl print` shows a process
  (`ensure_daemon_running`, at most every 30 s).
- **Files**: `~/.onionpress/daemon-state.json` (atomic rename, 0600:
  pid, version, ports, runtime_ready, is_running/is_ready/has_internet/
  yellow_since/last_check_complete (the `display_state` inputs, shared
  as `daemon.display_state_of`), address, healthcheck address, bootstrap
  %, images_present, wordpress_up, wp_installed, proxy_running,
  `pending_question`, `onionname_collision`,
  `settings_page_error`, the probe's last code), `daemon.pid`,
  `onion_address` (the cache, as before), `.setup_complete`. The menubar
  reads the state every 2 s (`_view_tick`); a state older than 90 s or
  from a dead pid is "Reconnecting to the OnionPress service..." (icon
  starting), never "Stopped". Markers the menubar writes for the daemon:
  `.onionname-retry`; `.upload-analytics`
  and `.config-changed` are taken by the daemon's tick; `.reopen` stays
  the menubar's. The Settings page's question the handler stops at goes
  into `pending_question`; the menubar asks and runs `handle-action
  --confirmed <what>` / `--cancel` itself.
- **`.setup_complete` is written by every setup, not only the daemon**
  (issue #374): the menubar shows the welcome screen until it exists, and
  the daemon writes it only once the site is fully operational, so a node
  set up headlessly with `onionpress setup` (or stopped before the site
  was reachable) got the welcome screen at its first menubar launch.
  `setup_logic.mark_setup_complete` (`SETUP_COMPLETE_MARKER`) writes it
  at the end of a completed `install_fresh_wordpress` (the setup window,
  the Linux paths), the Mac launcher's `setup` touches it after
  `setup_wordpress_cli` (both branches) and for an already installed site
  (the repair for a node from before: run `onionpress setup`), and the
  daemon's write stays. `tests/test_setup_complete_marker.py` runs the
  launcher's case under bash and keeps the four literals equal.
- **Logs**: the daemon owns `logs/onionpress-*.log` (and the capture
  logs); the menubar writes `logs/menubar-*.log`; View Logs opens the
  daemon's, the launcher's flat `onionpress.log` and its own
  (`log_rotation.current_log_path` finds another process's active file
  without writing it). Two processes never write one RotatingLog.
- **Decisions**: Quit keeps today's meaning (the site goes down: daemon,
  containers, VM) unless `QUIT_KEEPS_SITE_RUNNING=yes` (`config set`;
  then Quit closes only the menubar and the daemon keeps the site, the
  heartbeat and the probe running until `onionpress quit`). `onionpress
  update` stops the daemon once the new bundle is staged, swaps, and
  starts it from the new bundle (`updater.download_and_install`'s
  `before_swap`/`after_swap`), before the menubar relaunches: the daemon
  executes from the bundle being replaced (the Gatekeeper note above).
  `onionpress uninstall` stops the daemon before the /unregister and the
  stop, and removes the agent's plist with the login item.
- **After editing `src/onionpress/daemon.py`, rebuild the MenubarApp**:
  the daemon runs the bundle's interpreter and package, like every
  `onionpress.cli` command. A daemon started from a bundle at another
  path keeps that path in its plist (`install_agent` rewrites it when it
  differs).

## The App's Functions as Subcommands (issue #382)
- The rows #350 left after its four phases live in
  `src/onionpress/app_commands.py`, one implementation for both platforms,
  none constructing `OnionPressCLI` (its setup creates config and secrets
  and probes ports): `version [--json]`; `address [--url] [--copy]` (the
  served address from the tor container, else the daemon's `onion_address`
  cache; the URL is the menu's Copy Onion Address, `/<ONIONNAME>/` and no
  token; the clipboard through pbcopy / wl-copy / xclip / xsel);
  `check-reachability [--json]` (`reachability_test`, the one probe through
  the `onionheaven` Tor client, which handle-action's check-reachability
  runs too; exit 0/1/2); `logs --app|--launcher|--web [-n N] [-f]` (the Mac
  daemon's `logs/onionpress-*.log`, the flat `onionpress.log`, the
  `wordpress-visitors` capture or, without it, `docker logs
  onionpress-wordpress` with the `OnionPress-HealthCheck` lines dropped as
  the daemon drops them; bare `logs` stays the compose follow);
  `open site|admin|settings|onion` (the browser gets
  `launcher_ops.login_target`'s file:// page, never the token; the port from
  `$ONIONPRESS_WP_PORT`, else `config.resolve_port_offset`; `onion` prefers
  Tor Browser, on a Mac `open -a` of `launcher_ops.TOR_BROWSER_APPS`);
  `onionname show|set NAME [--json]` (checked by `setup_logic.validate_onionname`,
  registered with OnionHome under the served address when the site is up,
  then recorded through `config.set_config_value`; a taken name is refused
  with its suggestions, exit 2, nothing recorded; one not registered now is
  recorded `ONIONNAME_REGISTERED=no` and on a Mac `.onionname-retry` has the
  daemon retry it). `import-key --file
  <hs_ed25519_secret_key>` is bash in both launchers over `key_manager.py
  encode-key-file` (96-byte C Tor file or the bare 64-byte key, checked
  before anything happens); Linux's `import-key` takes `-` now too. Both
  launchers delegate `address`, `logs` with flags and the new names to
  `onionpress.cli`. `tests/test_cli_app_commands.py`; rows in
  `tests/test_cli_dispatch.py`.

## Check/Confirm Protocol
- Every destructive command speaks it (`src/onionpress/confirm.py`, issue
  #366): a UI runs `<cmd> --check --json` and the CLI, changing nothing,
  answers `{"ok": true, ...facts}`, `{"confirm": "<what>", ...facts}` (exit
  4) or `{"error": ...}`; the UI shows its own dialog from the facts
  (Cancel the default, no answer in 5 minutes is Cancel: those semantics
  live in the UI, `menubar._ask_cli_question`), then runs the command again
  with `--confirmed <what>` for every question it asked (or all of them in
  `$ONIONPRESS_CONFIRMED`). Without that the CLI refuses past the check
  (exit 3) when not at a terminal; at a terminal it asks itself, as the
  launchers always have. `{"ask": "password", ...}` (exit 5) means the
  command needs a secret first (a restore's password), which goes in the
  environment or on stdin, never on argv.
- The questions (`confirm.QUESTIONS`, a test holds the menubar's dialogs to
  the list): `address-change` (import-key, settings-page-restore), `
  sealed-hub-data-left-out` (`restore` of a hub's sealed backup without
  its recovery key), `hub-holds-key` (`uninstall`, `reset` while a hub may
  hold the key and OnionPress is not running to ask it), `prefix-change`
  (`config set ADDRESS_PREFIX` to a prefix the live address lacks),
  `uninstall` and `reset`. `handle-action` has no `--check`: its first run
  is the check, and `--confirmed`/`--cancel` answers the pending question.
- A new destructive command or question: `confirm.add_arguments` on its
  parser, `confirm.require(what, confirmed, **facts)` at the decision,
  `answer_*` for the output, the name in `QUESTIONS`, a dialog in
  `_ask_cli_question`, and a flow in `tests/test_cli_confirm_protocol.py`.
- **The Linux tray is a client too** (issue #375, `linux/onionpress-tray`
  section 3b, GTK-free so a stub launcher tests it): Restore… runs `restore
  <zip> --check --json` (`plan_restore`: the `{"ask": "password"}` prompt
  worded with `backup_user`, the facts dialog with address, date, user,
  version and `excludes_onion_key`, the recovery-key dialog for
  `sealed-hub-data-left-out` — the typed key is checked by another
  `--check` with it in `ONIONPRESS_HUB_RECOVERY_KEY`, "Restore Without" is
  `--confirmed sealed-hub-data-left-out`, any other question is cancelled)
  and then the real run with the `RestorePlan`'s args and environment.
  Backup… runs `backup --check --json` (`check_backup`; the Linux
  launcher's `backup)` case hands `--check`, `--json` and `--user` to
  `onionpress.cli backup --check` with the password in the environment,
  the backup itself staying bash's `create_backup`, now at the check's
  `output` path, the CLI's file name). The flow runs in a worker thread,
  each dialog marshalled to the GTK loop (`_on_main_thread`), Cancel the
  default and no answer in 5 minutes Cancel (`_run_dialog`); `Launcher.run`
  gives the launcher no terminal (stdin /dev/null) and takes `extra_env`
  for the key. `tests/test_linux_tray_protocol.py`.

## Blog Snapshots Stay Off the Web
- My Creations is published, so nothing private may sit there under a name
  that does not start with `.`. The blog importer's WXR snapshots (drafts,
  private and password-protected posts, author emails) live in the hidden
  `~/OnionPress/Creations/My Creations/.blog-archives/<host>/`: on the
  owner's disk, downloaded through the importer page's owner-only
  admin-post handler. Every publishing layer refuses hidden paths:
  `OnionPress_Creations::is_public_path` (both listings, `?onionpress_creation=`,
  the upload target), the multisite `.htaccess`, and the image's
  `onionpress-creations.conf` (`<FilesMatch "^\.">` sees only a file's own
  name, so a `<DirectoryMatch>` covers files inside hidden folders).
- Through 2.5.2 they went to the published `blog-archives/`.
  `onionpress_blog_snapshots_migrate()` moves it into `.blog-archives/` on
  the owner's admin requests: one rename, else a merge that never
  overwrites, never deletes a file and never descends a symlink. Until it
  is gone, the top-level `blog-archives` is refused by the same layers
  (`LEGACY_SNAPSHOTS_DIR`, its own `.htaccess` block, a second
  `<DirectoryMatch>`), and the importer page says what could not be moved.
  `tests/test_blog_snapshots_private.py`.

## My Creations Listings, Serving and Uploads (issue #319)
- Both listings (`get_folder_contents`, `get_files`) go through
  `cached_listing()`: a transient per folder, rebuilt when old (5 min per
  folder, 10 min for the whole-tree home page list), when the folder's or
  its `.thumbs/` folder's mtime changes, or when an upload raises the
  network-wide `op_creations_generation` site option. While one request
  rebuilds, others get the previous listing (the 2026-04-19 outage was
  piled-up scans of the host mount). Cached entries hold no URLs:
  `listing_entry()` adds them per request, because the domain map makes
  `home_url()` the request's host.
- `serve_file` sends ETag/Last-Modified, answers 304, one byte range with
  206 (416, If-Range honoured; several ranges get the whole file) and
  streams in 1 MiB pieces. `tests/test_creations_listing_and_serving.py`.
- Uploads: `max_file_uploads = 200` in the WordPress image's
  `onionpress-uploads.ini` (needs an image publish); the form checks the
  limits before sending and posts `creations_upload_count`, and
  `handle_upload` reports refused, failed, unmoved and dropped files.
- **What the folder serves is sandboxed** (issue #399): both paths, the
  page's `serve_file` and the image's `onionpress-creations.conf` for
  the raw `/wp-content/creations/...` path, send
  `X-Content-Type-Options: nosniff`, and `Content-Security-Policy:
  sandbox` for a document a browser could run script from
  (`OnionPress_Creations::SANDBOXED_EXTENSIONS`: HTML, XHTML, SHTML,
  MHTML, SVG, XML, XSL, by extension or by type; the conf's `FilesMatch`
  lists the same extensions, test-tied). A saved web page or an SVG
  dropped into My Creations opened at localhost:8080 ran script with
  the owner's cookies. The sandbox has no `allow-scripts`: a published
  page keeps its markup and loses its script. Needs an image publish for
  the raw path. `tests/test_creations_sandboxed_serving.py`.
- The Mac thumbnail generator starts at launch; `onionpress.thumbnails`
  decides what to make and removes thumbnails of files that are gone
  (never when part of the folder could not be read).

## Follow Requests Stay Off the Container Network
- Every site administrator types follow targets, and onionpress-tor-proxy
  connects to `onionpress_is_local_host()` names (containers, localhost)
  directly instead of through Tor. So every Following request (probe,
  discovery, `fetch_feed`) goes through `onionpress_follow_remote_get/
  _head/_fetch_feed` in onionpress-blogroll.php: the URL must pass
  `onionpress_follow_url_allowed()` (http(s), no userinfo/whitespace/
  backslash, host = 2+ labels with an alphabetic TLD, not localhost or a
  local-network name, not local per the tor proxy; no DNS lookup), and
  `onionpress_follow_guarded()` holds every further request and redirect to
  the same rule. Discovery runs only for a v3 `.onion`. The guard also sets
  `reject_unsafe_urls` false for accepted URLs: WordPress's
  `wp_http_validate_url()` (which `fetch_feed` asks for) resolves the host
  with `gethostbyname()` in the container, a clearnet DNS query.
- Never call `wp_remote_*`/`fetch_feed` directly for a follow;
  `tests/test_follow_requests.py` fails if one appears.

## Imported Bodies Are Filtered at Ingest (issue #403)
- The Mastodon importer stored the instance's `content` verbatim and
  the blog importer ran a regex sanitizer that missed entity-encoded
  `jav&#x61;script:` hrefs, `<img/onerror=...>` without whitespace,
  `<base>` and `<meta refresh>`; the_content never runs kses, and what
  protected the site was that both run under wp-cron, where save-time
  kses happens to apply. Each foreign HTML field now goes through
  `wp_kses_post()` where it is read (`onionpress_mastodon_render_content`,
  both the status and the boosted status; `onionpress_blog_render_content`,
  which insert and update share), and the regex sanitizer is gone.
  Bluesky, Twitter and Reddit build their HTML from `esc_html`'d text.
  `tests/test_importers_kses_at_ingest.py` keeps a census: a
  `(string) ( $x['content'|'html'|'rendered'] ...)` read in an importer
  is filtered on its line or cut to plain text in its function.

## The Blogroll Refreshes in wp-cron, Never in a Page View (issue #314)
- `onionpress_get_aggregated_feed_items()` (sidebar, every page) only reads:
  the `onionpress_blogroll_items` transient, else each current follow's
  last good items (`onionpress_following_items`), and schedules
  `onionpress_blogroll_refresh`. Deleting that transient (any follow
  change) schedules one too. Never fetch from the serve path.
- The refresh holds a lock row (`INSERT IGNORE`, expiring value), a
  180 s budget every follow request is cut to (`onionpress_follow_deadline`
  in the follow guard), least recently tried follows first, backoff from
  `last_attempt`, a failing follow keeps its items, and it re-reads the
  options before writing (only still-followed keys).
- Follow requests set `onionpress_exact_timeout`; onionpress-tor-proxy then
  keeps their timeout instead of the 60 s floor (so the follow form's 8 s
  probe is 8 s). Follow fetches turn SimplePie's cache off; there is no
  global `wp_feed_cache_transient_lifetime` override.
- onionpress-tor-proxy's `cron_request` filter sends the wp-cron loopback to
  `http://localhost/<path>` (WP_SITEURL is the visitor's host, so a Tor
  view's loopback went out through Tor and never arrived).
  `tests/test_blogroll_refresh.py` (PHP harness, runs where php is installed).

## WordPress Resolves No Name Outside Docker
- Tor resolves every name WordPress fetches (socks5h), so a DNS lookup in
  the wordpress container is only ever a leak: Docker's resolver forwards
  non-container names to the Docker host's resolvers (the Colima VM's on
  macOS), on current Docker from the host's own namespace, so the iptables
  clearnet log may never see it. WordPress made one per `wp_safe_remote_*`
  request (oEmbed, pingbacks, enclosures, `fetch_feed` and the dashboard
  feeds, `download_url`, link previews) via `wp_http_validate_url()`'s
  `gethostbyname()`.
- onionpress-tor-proxy: `onionpress_tor_proxy_request_args` (last
  `http_request_args`) turns `reject_unsafe_urls` off for every non-local
  host and marks it `onionpress_check_url`; `onionpress_tor_proxy_check_url`
  (pre_http_request) applies `onionpress_remote_url_is_safe()`, WordPress's
  rules by shape (no IP literal in private ranges unless
  `http_request_host_is_external`, 2+ labels with an alphabetic TLD, no
  local-network names, ports per `http_allowed_safe_ports`), never a lookup.
  `.onion` and update hosts skip the check as before; local hosts keep
  WordPress's own (Docker answers for them). A redirect must stay on its
  request's route (local→local or Tor→public shape): the proxy choice is
  made from the first URL, so local→outside would connect directly.
- docker-compose.yml: `dns: 127.0.0.1` for wordpress and db. Docker's
  resolver still answers for containers and forwards anything else to the
  container's own loopback, where nothing listens: lookups fail closed
  (wp-cli's own HTTP client, plugin code). Keep them on a user-defined
  network (onionpress-wordpress-network): the loopback upstream means the
  container's own loopback only behind the embedded resolver. Not an
  `internal: true` network: those publish no ports and give WordPress no
  default route (no gateway for auto-login), and it needs 8080 on the host.
- wp-cli's own HTTP client (not WP_Http, so not Tor) now fails at the
  lookup: `wp core update <zip>` exits non-zero after a completed update
  (its checksum-list fetch), so onionpress-security-audit.sh judges the
  update by `wp core version`. Raw `curl_init` in plugins must set a socks5h
  proxy or talk only to a container. `tests/test_no_clearnet_dns.py` (PHP
  harness with the lookup functions disabled; plugin census).
- **Nor any connection (issue #144).** wordpress and db are on
  onionpress-wordpress-network only, bridge `onionpress-wp0` (fixed name);
  tor, onionheaven and cloudflared join it too (wordpress:80, SOCKS 9050,
  8083), takeover workers never. On the Docker host the ONIONPRESS-WP chain
  (DOCKER-USER for FORWARD, a jump in INPUT) refuses, and logs as
  `CLEARNET-blocked:`, every packet from that bridge that leaves it or is
  for the host, except replies (`RELATED,ESTABLISHED`): the published port,
  LAN visitors (own address, not the gateway) and docker-proxy still work.
  `onionpress/wordpress_egress.py` holds the script and puts it in place
  before every start's first `compose up` (`ContainerManager.start_core`;
  both launchers' `block_wordpress_egress` is `onionpress wordpress-egress
  … 2>&1 | tee -a "$LOG_FILE"; return 0`, Phase 4 #120: the CLI logs the
  verdict in the launchers' format, the Mac reads its status out of
  PIPESTATUS for the START SUMMARY): Colima VM via
  `limactl shell`, plus a docker.service drop-in (`ExecStartPre=-`) so it is
  there before Docker restarts containers after a VM boot; rootless Linux
  via `nsenter -U --preserve-credentials -n -m` into RootlessKit (the
  status loop re-applies when the namespace changes); system Docker via a
  one-shot `--privileged --pid host --network none` helper (pinned tor
  image) running the host's iptables, plus the `.deb`'s docker.service
  vendor drop-in (`linux/onionpress-wordpress-egress.conf`, the script cut
  from RULES_SCRIPT at build time). Best effort: a failure is a WARNING
  (Mac: `wordpress-egress` in START SUMMARY), never a stopped start. The
  network's name must sort after `onionpress-network` (Docker gives a
  two-network container the lexically first default route) and stay
  IPv4-only. It replaced the Mac launcher's per-address LOG rules
  (`install_clearnet_logging`), which the script removes.
  `tests/test_wordpress_egress.py` (fake iptables).
- **The Tor side reaches only the internet.** onionpress-network's bridge
  is fixed too, `onionpress-tor0`; the same script's ONIONPRESS-TOR chain
  (DOCKER-USER + INPUT) sends the host itself (`addrtype LOCAL`) and
  10/8, 100.64/10, 127/8, 169.254/16, 172.16/12, 192.168/16, 224/4, 240/4
  (fc00::/7, fe80::/10, ff00::/8) to ONIONPRESS-TOR-REFUSE (logged
  `CLEARNET-local-blocked:`), after replies and DNS (udp/tcp 53: Docker's
  resolver asks a LAN or on-host server from the container's namespace).
  Not 198.18/15 (fake-IP proxies). Before, tor, onionheaven, cloudflared
  and takeover workers reached the LAN, the host's services and, through
  192.168.5.2 (Lima maps it to the Mac's 127.0.0.1), everything on the
  Mac's loopback. Docker cannot rename a bridge:
  `ContainerManager.migrate_tor_network` (`start_core`, and both launchers'
  `migrate_tor_network` through `onionpress migrate-tor-network`) removes an old onionpress-network
  before the first `compose up` (onionheaven first, then OnionPress's
  containers on it, by exact name, workers only as
  `onionheaven-{poll,takeover}-<digits>`; anything else is only
  disconnected, never removed). Compose 2.40
  would recreate it itself, but fails the start while a takeover worker
  is attached. A Cloudflare tunnel whose ingress names a LAN service
  stops reaching it.
- **No container reaches WordPress's published port.** Docker skips its
  DNAT for anything from one of its bridges, so docker-proxy handed any
  container's `<host address>:<port>` to WordPress as the gateway (Linux
  0.0.0.0 bind; on macOS through the Mac's loopback and Lima's forward).
  INPUT `! -i lo -p tcp --dport $ONIONPRESS_WP_PORT -j ONIONPRESS-WP`
  closes it for every bridge, other projects' too; loopback and DNAT'd LAN
  visitors are untouched. The port is the script's 6th argument
  (`--wp-port`, both launchers pass `$ONIONPRESS_WP_PORT`; a changed port
  leaves no rule behind; the VM drop-in carries it, the `.deb`'s cannot and
  leaves the rule as it is). Under rootless Docker's default (RootlessKit
  "builtin") port driver every client of the port, LAN included, arrives
  as the gateway anyway (measured); the Linux launcher says so at start
  (`note_if_lan_looks_local`) unless the bind is 127.0.0.1; the token is
  the remaining check there.

## The Docker Socket Stays Out of onionheaven
- Only two compose services mount `/var/run/docker.sock`, both with
  `network_mode: none`, `cap_drop: [ALL]`, `read_only: true` and the mount
  `:ro`:
  `autoheal-proxy` (path allowlist for autoheal's two calls) and
  `onionheaven-farm`, the OnionHeaven farm controller
  (`app/Resources/docker/tor/onionheaven-farm.py`, entrypoint mode
  `FARM_CONTROLLER=1`). The `onionheaven` container's heartbeat used to
  mount the socket read-write: it parses output from Tor-facing workers, so
  that was `docker run -v $HOME:/x` away. A socket-proxy allowlist would not
  have helped (`POST /containers/create` with any `Binds` is a path it must
  allow) and `:ro` on the mount is not a mitigation either (HTTP over the
  socket).
- The heartbeat reaches the controller over `/run/onionheaven-farm/farm.sock`
  on the `onionpress-onionheaven-farm` volume (only those two services mount
  it) through `onionheaven_common.farm_request/farm_exec/farm_list/
  farm_inspect/farm_remove`; `farm_available()` replaced `docker_available()`.
  The controller knows six verbs (`spawn`, `remove`, `list`, `inspect`,
  `exec` with eight fixed commands, `tor-address`); names must match
  `onionheaven-takeover-N`, addresses the v3 pattern, and nothing in a
  request chooses an image, a mount, a capability or a command line. The
  worker's `docker run` shape and the image (compose's `ONIONHEAVEN_IMAGE`,
  now passed to the controller, not to `onionheaven`) are the controller's.
  Spawn is capped at `ONIONHEAVEN_MAX_WORKERS` (64).
- **Never add a `docker` call to a tor-image script other than
  onionheaven-farm.py**, and never a new verb that takes a command line or
  a path; `tests/test_onionheaven_farm.py` holds the census, the validation
  table and the protocol test. `reset_onionheaven` no longer `docker cp`s
  code into workers (they run the pinned image) and restarts the heartbeat
  as a local process.
- Ships as compose + tor image together (docs/DOCKER-SOCKET.md, "The farm
  controller"): with either half old, a hub takes nothing over until the
  other arrives; nothing breaks otherwise. Verified 2026-10-14 in a
  throwaway Colima VM against the published 2.5.3 tor image with the new
  scripts bind-mounted (spawn, every exec verb, remove, refusals, SIGTERM,
  read-only rootfs), not yet on a live hub: on the first hub start after
  the upgrade, check `docker logs onionheaven-farm` for "listening on" and
  the first `spawn … -> ok`.

## A Plain Node's Address Carries No Hub API (issue #393)
- The tor entrypoint maps port 8083 (web-server.py: the hub API,
  OnionHome's name and analytics endpoints) onto the node's onion
  address only when `onionheaven_common.hub_api_exposed()` says so: a
  hub (`is_registration_hub`: the opt-in or the canonical address) or
  OnionHome (`ONIONHOME_ADDRESS`, now in onionheaven_common; web-server
  imports it). The hostname is derived from the key before the
  onion-services.json is written, so an existing address is known; a
  node still to mint one is no hub. web-server.py still listens inside
  the container network (the directory plugin and name sync on
  OnionHome call `onionpress-tor:8083`). Before, every node ran the
  1,900-line API as root on its own address beside the site key and the
  control cookie. The healthcheck and redirect listeners run `socat -T
  30 ... fork,max-children=64` (an idle connection is closed, a client
  cannot hold thousands of root shells; the Dockerfile checks socat
  knows the option), and the healthcheck keeps its three WordPress
  probes' answers for `CACHE_TTL` (60 s) in `/run/healthcheck/wordpress-facts`
  (written whole and renamed; junk or an unwritable dir just probes
  again). The launchers' hub-address check probes `<addr>:8083/status`
  and, on a plain node, now falls through to its port-80 probe
  ("reachable but not running OnionHeaven"). Still open in the issue:
  running web-server.py, the healthcheck and the redirect as a
  dedicated uid (root's key files, the registry and the farm socket all
  assume root). Ships in the tor image. `tests/test_tor_node_surface.py`.

## Containers Drop Every Capability
- Every compose service (both compose files), every takeover worker
  (`worker_run_argv` in onionheaven-farm.py and the unused
  `start_farm_worker`; the Mac launcher starts none) and every one-shot
  `docker run` in the launchers, cli.py and launcher_ops.py has
  `cap_drop: [ALL]` / `--cap-drop ALL`, adds back only the capabilities
  `tests/test_container_capabilities.py` lists for it, and has
  `no-new-privileges:true`. The test keeps a census of every `docker run`
  in shipped code (a new one fails until it is listed with its set) and
  never allows NET_RAW, NET_ADMIN, SYS_ADMIN, SYS_PTRACE, SYS_MODULE,
  DAC_READ_SEARCH (open_by_handle_at, the "shocker" escape: root reads
  other users' files with DAC_OVERRIDE instead), ALL or privileged. Write
  compose lists block style; the test reads them line by line.
- The sets (found on the 2.5.3 images by starting each with nothing and
  adding one at a time; docker-compose.yml says what needs which): tor
  CHOWN DAC_OVERRIDE FOWNER SETGID SETUID; wordpress the same plus KILL
  (Apache's graceful restart signals its www-data children) and
  NET_BIND_SERVICE (only Docker < 20.10 lacks ip_unprivileged_port_start=0);
  db CHOWN DAC_OVERRIDE SETGID SETUID; onionheaven and takeover workers
  DAC_OVERRIDE SETGID SETUID (their /var/lib/tor is the image's, so the
  entrypoint's chmod of it logs "Operation not permitted" harmlessly);
  autoheal, autoheal-proxy, onionheaven-farm (the farm controller: the
  docker CLI over the socket, nothing else), cloudflared nothing. Helpers: reads of key
  files, mkp224o, key seeds and `ContainerManager.migrate_key_volume` (its
  copy is root's) DAC_OVERRIDE; the Linux tor-state seeds CHOWN DAC_OVERRIDE FOWNER (they
  chown to debian-tor); the mkp224o probe nothing. `docker exec` runs with
  the container's set, so a root exec into tor or wordpress that touches
  debian-tor's or www-data's files relies on that DAC_OVERRIDE. Change a
  set only after the same live test.
- Every one-shot helper joins no network (`--network none`) and overrides
  the image's entrypoint; only the takeover workers join one and keep
  theirs. The launchers' helpers (key volume migration, key seeds, Linux
  create_backup's key reads) run `KEY_HELPER_IMAGE`,
  `${ONIONPRESS_TOR_IMAGE:-<tor pin>}` (the pin equal to
  build/image-pins.env's, set after the config is read), and the Python
  ones the pinned tor image (`ContainerManager.ensure_tor_image`,
  `launcher_ops.DEFAULT_TOR_IMAGE`): never a floating tag such as `alpine`,
  which a registry or mirror could swap for an image that copies the key.
  Sources are mounted read-only. Linux create_backup reads keys through
  `key_volume_cat` (volume read-only, `--entrypoint cat`; it runs nothing
  when the volume does not exist, since `docker run -v` would create an
  empty one that passes for an installed or migrated key) and refuses to
  back up without the image. The census in
  `tests/test_container_capabilities.py` holds all of it; a test harness
  running Linux create_backup loads the key helpers from the launcher
  (`test_backup_metadata_validation._linux_key_helpers`) and has its docker
  stub answer `image inspect` and `volume ls`.
- Auto-login's "local" scope and `/__op_proxy.php` admit loopback and the
  gateway of WordPress's network (onionpress-wordpress-network) by source
  address, which is why no container may hold NET_RAW (packet forging on
  that network). The one privileged container is the #144 egress helper on
  Linux system Docker (`wordpress_egress.helper_argv`: no network, no
  mounts, `--rm`), held to that shape by the same test.
- no-new-privileges blocks setuid binaries, not root calling setuid(): the
  tor image starts as root and drops with `su` (from root only), MariaDB
  uses gosu, Apache drops its own children. Never make a non-root process
  call su or sudo in these images; the test keeps a census of su callers,
  and of the tor image's signal senders (none may signal another user's
  process without KILL back).
- Nothing in the images uses raw sockets (no ping, arping, SOCK_RAW); a
  healthcheck or tool that needs one has to get past the same test.

## The Data Dir Is the User's Alone (issue #397)
- Both launchers `chmod 700 "$DATA_DIR"` right after `mkdir -p`, on every
  start (an install made under a looser umask is fixed too), and every
  creation of `config` makes it 0600 (the Mac's template copy, both
  heredoc fallbacks, install.sh, `config.ensure_config`): it may hold
  `CLOUDFLARE_TUNNEL_TOKEN`, and the template is a 0644 bundle resource.
  Every `os.makedirs(... data_dir ...)` in `src/` passes `mode=0o700`
  (a census in `tests/test_data_dir_permissions.py`, which also runs
  the launchers' two blocks under bash). An existing config's mode is
  left as the owner set it. Still open in the issue: the mkp224o key
  stays in `shared/vanity-keys/` (the one folder mounted into the VM)
  after it is seeded into the onion-keys volume; the seed helper can
  read it only from there, and restores, imports and hub detection
  keep the folder, so moving it is a design change of its own.

## Colima VM Sandboxing
- The VM has two narrow mounts: `~/.onionpress/shared:w` and `~/OnionPress:w`
- This limits blast radius if a container is compromised — attacker can only see vanity keys and user's published content (which is public anyway)
- `~/.onionpress/` stays as-is for app state (no spaces in path — required by Colima/Lima/Docker socket paths)
- `~/OnionPress/` holds user-visible backups and Creations
- All other container data uses Docker named volumes (which live inside the VM)
- **Do not move app state to `~/Library/Application Support/`** — the space in the path breaks Colima/Docker socket paths (104-char Unix socket limit + space handling issues)
- **Diffdisk cap is 20 GiB on new installs** (issue #230) via `--disk` to the first `colima start` in `app/MacOS/launcher.sh` and `src/onionpress/colima.py`. The cap is set at VM creation and is immutable — existing installs keep their original 100 GiB cap. Dangling Docker images auto-prune after each successful image pull (`update_docker_images()` in `src/menubar.py` and the bash launchers), keeping real usage well under 5 GiB in practice. The cap is configurable via `VM_DISK=N` in `~/.onionpress/config` *before* first launch; when the OnionHeaven hub vanity key is present at first start, the cap auto-bumps to 100 GiB for takeover-container headroom (hub is the edge case; normal nodes never need more than 20).
- **Do not add additional `--mount` flags without considering security implications**
- **`$COLIMA_HOME` must exist before anything runs Colima** (issue #370).
  Colima honours `COLIMA_HOME` only when the directory exists; otherwise it
  uses `~/.colima`, including for the Docker socket forward it writes into
  the instance's lima.yaml (`hostSocket:`). Through 2.5.6 nothing created
  it before the first `colima start`, so a fresh install's VM forwarded
  the socket to `~/.colima/default/docker.sock` while the launcher waited
  at its own path. Both launchers `mkdir -p "$LIMA_HOME"` where they export
  the variables; the menubar and `Colima._run` do the same. A Running VM
  whose lima.yaml forwards the socket outside `$COLIMA_HOME` is restarted
  through Colima with `colima_start_with_vm_settings` (the first start's
  flags: a bare `colima start` there mounts the whole home folder), from
  `wait_for_docker_or_recover_vm` and the restart case; the Lima-level
  restart cannot change the forward. `tests/test_colima_home_before_first_start.py`.

## OnionHeaven Hub Data Volume
- The hub's registry and every registrant's onion key live on
  `onionpress-onionheaven-data` at `/var/lib/onionheaven`, mounted by
  onionpress-tor, onionheaven and the takeover workers only — **never
  wordpress**, and never under `/var/lib/onionpress` (that is
  onionpress-persistent-data, which WordPress mounts read-write; a mount
  point inside a directory WordPress can write is not a boundary). Workers
  mount only this volume. Host code reaches it via `docker exec`/`cp` on
  `onionheaven` or `onionpress-tor`. `tests/test_onionheaven_data_volume.py`.
- It used to be `onionheaven/` on onionpress-persistent-data. The tor image's
  entrypoint (`onionheaven-data-migrate.py`, onionpress-tor and onionheaven)
  moves it on start: copy, verify, sync, put in place, marker
  `.migrated-from-persistent-data`, then remove each old file only if it is
  unchanged (the registry's files all or none). Only between mount points
  (both `/var/lib/onionheaven` and `/var/lib/onionpress`).
- WordPress owns the old location's folder, so: only `SCHEMA` moves
  (registry, `keys/<v3>.onion/` key files + hostname, logs, flags), walked
  by directory fd and never deeper; nothing sparse or over its `LIMITS`.
  **SQLite never opens a path there** (a planted `-shm` link would have
  root write through it): db + WAL are copied by fd with the WAL header
  unchanged around the copy (one state even with an older writer still
  running), then `Connection.backup` makes one file on the volume. Copies
  are root's, 0700/0600. A WAL/journal goes into place before its db.
- Later arrivals go to `legacy-leftover-<time>/` (allowlist only, within a
  free-space reserve; the rest there is removed; a registry written after
  the move logs a loud downgrade warning). Kept copies drop keys the volume
  holds identically, lose an address's key on unregister
  (`purge_kept_keys`), and are removed after 30 days.
- Older containers must not race the move: every start removes each
  takeover/poll worker, `onionpress-tor` and `onionheaven` that does not
  mount the volume before `compose up`
  (`ContainerManager.retire_old_onionheaven_containers`: `start_core`, and
  both launchers through `onionpress retire-old-onionheaven-containers`), the farm
  stops in parallel (`stop -t 1`), and the heartbeat retires old workers the
  registry does not list too.
- Fails closed only if the first move fails with a real key (non-empty key
  file in `keys/<v3>.onion/`) at stake; a start without keys at stake marks
  the volume (else the next start would shelve live data). Never after the
  marker: WordPress can write the old location.
  `tests/test_onionheaven_data_move_hardening.py`.
- Backups hold hub data when the volume has a key dir or a registry row
  (`backup.hub_data_state`, not the long-dead `master-key.json`): registry
  via `sqlite3 .backup` + `keys/`, nothing else. **An error is never a
  "no"**: `HUB_DATA_STATE_SH` says `none` only for no registry file, an
  empty one or one without a `registry` table or rows; `keys/` or a
  registry it cannot read (sqlite3 failing or missing, locked past its
  10 s busy timeout) prints nothing, so `hub_data_state()` is None.
  OnionHome's check is tri-state the same way (`onionhome_registry_present`,
  now also create_backup's step 4b, which used `test -f`: a docker error
  read as "not OnionHome"). The owner's backup then goes ahead
  without that data and says so: a WARNING in the log, metadata
  `onionheaven_data_unchecked` / `onionhome_registry_unchecked`, and
  `unchecked_notes` with the finished backup (Mac message, `onionpress
  backup`; Linux create_backup prints the hub's on stderr, its words
  test-locked to `HUB_DATA_UNCHECKED_NOTE`;
  `tests/test_backup_hub_data_off_wordpress.py`). **Sealed to the hub's
  recovery key** (issue #60 O7, `src/onionpress/hub_backup.py`) when
  `~/.onionpress/hub-backup-recovery.pub` exists: read into memory
  (`read_hub_data`, never a file in the clear), one tar, PyNaCl `SealedBox`
  (crypto_box_seal) to the public half, stored as `onionheaven.sealed` in
  the format-2 archive (no format bump; metadata `onionheaven_data:
  "sealed"`, `onionheaven_sealed_format: 1`, `onionheaven_recovery_key_id`:
  16 hex of SHA-256 of the public half). No key: plain `onionheaven/` as
  before (`stage_hub_data`) with `HUB_DATA_UNSEALED_WARNING` every time
  (`stage_hub_backup`; Linux `onionpress.cli stage-hub-backup`). A key file
  that is there but unreadable fails the backup. The private half is
  printed once by `onionpress hub-recovery-key create` (`create|show|check|
  restore`, both launchers pass `--data-dir "$DATA_DIR"`), held offline by
  the operator, and read only from `ONIONPRESS_HUB_RECOVERY_KEY` (popped in
  `cli.main` before anything else), stdin or a prompt; never argv, never
  stored. The pub file is never under `shared/` (VM mount) or in config
  (Settings page). Restores: the Mac's Restore… prompts
  (`_ask_hub_recovery_key`), `onionpress restore` takes the env var or a
  tty prompt, checked by key id before anything is restored and opened in
  memory right after extraction (`prepare_install_from_backup(after_extract=)`);
  without it everything else is restored and the hub data named as left
  out (`sealed_skipped_message`); `hub-recovery-key restore <backup>`
  restores it later (the welcome screen's restore never asks). 2.5.3
  restores such a backup's site and silently ignores the member.
- Every hub-data restore, sealed or plain, is checked first
  (`hub_backup.check`): a key must be a v3 address's own (PEM parses,
  derives to the name, scalar < 2^255 and scalar·B = public key, as
  `key-convert.py`'s `keypair_error`; a test holds them to the same
  verdicts) and is written as a fresh PEM + our own hostname via `docker cp
  -` of an in-memory tar; the registry goes through `REGISTRY_RESTORE_SH`
  in onionheaven (integrity check, only `registry`/`takeover_containers`
  and their autoindexes, rows with v3 addresses only, then `.restore`).
  Sizes bounded (`MAX_ARCHIVE_BYTES` etc.). `tests/test_hub_backup_sealed.py`.
- **Never in a backup that goes through WordPress.** The Settings page's
  backup is handed to the WordPress container for download, and its
  password is the admin password WordPress saw in plain text; its restore
  takes an upload and a password from there too. So the page's backup is
  `create_backup(include_hub_data=False)` (Linux `create_backup
  --without-hub-data`), and on a hub or on OnionHome it is **refused**
  before anything is copied (`settings_page_backup_refusal`:
  `SettingsPageBackupRefused`, Linux return 3 + `BACKUP_REFUSAL`, the page
  shows the message): it carries the node's own onion service key, and code
  in WordPress could run a rival hub collecting registrants' keys (or a
  rival OnionHome). Detection: own address is the canonical hub's or
  OnionHome's, `ONIONHEAVEN_ACCEPT_REGISTRATIONS=yes`, `hub_data_state() ==
  "hub"`, or OnionHome's registry present; never `server_active` (true on
  every Linux node). **Fails closed**: no own address, `hub_data_state()`
  not `"none"` (onionheaven did not answer or could not tell) or no yes/no
  from `onionhome_registry_present()` (`sh -c` printing yes/no: a failed
  exec and a missing file both exit 1) is refused too
  (`UNVERIFIED_BACKUP_REFUSED_MSG`, same paths, rc 3 on Linux; Linux
  create_backup uses its words, test-locked, whenever
  `settings-page-backup-check` fails without exit 3 and a reason); the
  owner's backups never ask. Every other `create_backup` caller passes
  `include_hub_data=True` (owner's disk). The page's restore
  (`restore_from_backup(through_wordpress=True)`, Linux `restore_backup
  --from-settings-page`) restores nothing at all on a hub or OnionHome, or
  a node that cannot say (`settings_page_restore_node_refusal`, before the
  upload is opened: it would replace the node's own key and site), and
  refuses, before touching anything, a backup with
  any entry in `onionheaven/keys/` or an `onionheaven.sealed`
  (`settings_page_restore_refusal`; refused whole, since a hub restored
  without its registrants' keys looks restored) and never writes the volume
  (`hub_volumes=False`: a registry alone, as every Linux backup through
  2.5.2 carries, is left out). `tests/test_backup_hub_data_off_wordpress.py`
  keeps a census of the hand-offs to WordPress and the callers.
- Compose and tor image ship together (see docs/DOCKER-SOCKET.md, "The
  OnionHeaven hub data"). **Never downgrade a hub across this release**;
  on the hub follow the procedure there (count, tar the old dir, stop
  fully, update, check the log line and the counts).

## Registration Caps (issue #391; on main since #414, the numbers still open)
- Minting a key is instant, so web-server.py bounds what a free
  registration can take: `registration_room` (after the signature, before
  the key is looked at) refuses a new (content, healthcheck) pair for an
  address at `MAX_ROWS_PER_ADDRESS` (4) with 429 and a new address on a
  hub at `MAX_REGISTRANTS` (5000) with 503 "hub full"; an existing pair
  always has room, so the registered keep heartbeating. `analytics_room`
  refuses a new analytics pair past `ANALYTICS_MAX_PAIRS_PER_ADDRESS` (4)
  per address or `ANALYTICS_MAX_INSTANCES` (10000) in all (manifest and
  upload, before the directory is made). `onionnames.register_name`
  refuses the `MAX_NAMES_PER_ADDRESS` (10) + 1th name of an address as
  `too_many` (429 from `local_register`); the migration script goes
  through `validate_name` only, so a legacy registry is not cut. Tests
  that build big registries lift the cap (`MAX_NAMES_PER_ADDRESS`
  patched). Env: `ONIONHEAVEN_MAX_ROWS_PER_ADDRESS`,
  `ONIONHEAVEN_MAX_REGISTRANTS`, `ONIONHOME_ANALYTICS_MAX_PAIRS_PER_ADDRESS`,
  `ONIONHOME_ANALYTICS_MAX_INSTANCES`, `ONIONHOME_MAX_NAMES_PER_ADDRESS`.
  The numbers, a per-address `/online` interval and whether a row must
  pass an audit before it earns a worker slot are still the issue's
  decision (the code reached main on #414's branch, not by one).
  `tests/test_hub_registration_caps.py`.

## Registry Names Reach Docker Only as Workers'
- The registry is data anything mounting the hub volume can write as root
  (onionpress-tor faces Tor; every takeover worker). The heartbeat holds the
  Docker socket and takes container names from it (`takeover_containers`,
  `registry.takeover_container`): a planted `onionpress-wordpress` row got
  `docker rm -f` from `cleanup_dead_workers`. So in the tor image's code a
  name reaches a docker call only through `worker_name_ok`/`is_worker_name`
  (`onionheaven-(takeover|poll)-N`, whole), an address only through
  `onion_address_ok`/`is_onion_address`; `drop_unnamed_worker_rows` (start
  of `cleanup_dead_workers`) deletes such rows and unassigns their
  addresses. The queue manager refuses non-addresses itself
  (`handle_command`), and key directories are removed only for an onion
  address (`_key_dir`: ".." or an absolute path removed other trees).
- `tests/test_hub_registry_names.py` keeps a census of every docker argv in
  the image's Python: each variable is guarded in its function or listed in
  `REVIEWED` with why it is not registry data. A new docker call fails it
  until it is looked at.

## Stored Registrant Keys Are Checked and Written in One Step
- `/online`'s signature covers endpoint|content|healthcheck|timestamp, not
  `arti_key_pem`. The PEM must prove itself: web-server.py's
  `private_key_problem` reads the private section with key-convert.py's
  `parse_arti_pem_bytes` (what the takeover path uses), requires its public
  key to be the one that derives to the address, and its scalar times the
  base point to be that key (`keypair_error`: libsodium via PyNaCl,
  `crypto_scalarmult_ed25519_base_noclamp`; scalar bit 255 refused). 400
  otherwise; the stored key is untouched. The tor image installs
  `python3-nacl` (Debian) and checks it at build; without it the scalar is
  not checked (logged once) rather than refusing every heartbeat.
- Key files are never rewritten in place: `write_key_file` (hub keys and
  hostname; skips identical bytes), `key-convert.py _write_atomic` (node key
  at start, secret key written last), the watchdog's NEW:BEST save. Temp
  file O_EXCL|O_NOFOLLOW 0600, fsync, rename, fsync dir.
  `tests/test_hub_stored_keys.py`. All of this ships in the tor image.

## Request Signatures Are Made and Checked by libsodium
- `onion_auth.verify` is PyNaCl's `VerifyKey(pub).verify` (libsodium's
  crypto_sign_open): S < L, no small-order or non-canonical key or R. The
  pure-Python verifier it replaced took R = identity, S = 0 as a signature
  of anything for a small-order key, so anyone could sign for
  `aeaaaa…hmjqd.onion`.
- Signing (`sign_expanded`, Tor's expanded key; an mkp224o key has no seed,
  and libsodium and pyca/cryptography sign only from one) is RFC 8032
  §5.1.6 composed from libsodium's constant-time primitives, as Tor signs
  with these keys (Ivar, 2026-10-14): `crypto_core_ed25519_scalar_reduce`,
  `crypto_scalarmult_ed25519_base_noclamp`, `_scalar_mul`, `_scalar_add`;
  no arithmetic of our own. It refuses a public key other than a·B (two
  signatures of one message under two keys give a away). The public key
  (`onion_auth.derive_public_key`, `key_manager.derive_public_key`) is
  libsodium's the same way, the scalar taken mod L as Tor takes it. The
  tests hold the bytes to libsodium's `crypto_sign` for seeded keys, the
  RFC 8032 vectors, and the pure-Python signer it replaced for every key
  (the wire format did not change).
- Without libsodium's ed25519 functions (no PyNaCl, or a minimal build)
  signing and derivation fall back on the old pure-Python arithmetic
  (variable-time) and log a warning once, rather than stop: an unsigned
  heartbeat lets the hub take the site over, and format-1 restores must
  work without PyNaCl. Nothing shipped should get there:
  `build/py2app-extensions.sh verify` and the tor Dockerfile fail the build
  without `has_crypto_core_ed25519` or with signing off libsodium, the
  .deb depends on python3-nacl, install.sh checks the functions, and the
  Linux heartbeat logs a warning at start. `onionpress update` from a
  checkout installs no apt package, so a source install from before 2.5.3
  can lack python3-nacl.
- The only verifier is the hub's web-server.py: `_verify_signature`
  (/online, /offline, /unregister), `_signed_peer_claim`, the OnionHome
  name and log endpoints. `tests/test_onion_auth.py` keeps that census and
  keeps the three copies of onion_auth.py (src, tor image, tests/stress)
  identical.
- **A verified request is accepted once** (issue #400): a signature is
  good for `TIMESTAMP_TOLERANCE` (5 min) either side of the hub's clock,
  and a captured `/offline` replayed inside that window forced a takeover
  of a live site. `_verify_signature` now, after `verify_payload` says
  yes, runs `onionheaven_common.note_signed_request` (the `signed_requests`
  table, `db_ensure_schema`): per content address the latest accepted
  timestamp and the signatures accepted at that second (clients stamp
  to the second and may send an `/online` and the `/offline` of a
  suspend in one; Ed25519 is deterministic, so a replay is
  byte-identical), refused as 403 when older or already seen, in one
  `BEGIN IMMEDIATE` so two copies cannot both pass. A forgery never
  moves the mark (the registry is opened only after the check); a
  registry that cannot be opened refuses. A client clock set back costs
  at most the tolerance's worth of refused requests (`REJECT-REPLAY` in
  the log). The peer claim and the OnionHome name endpoints have their
  own namespaces and are not marked. A hub restore keeps only
  `registry` and `takeover_containers`; an empty table admits what the
  tolerance admits. Ships in the tor image. `ReplayTest` in
  `tests/test_hub_signature_verification.py`; a harness that posts one
  message twice in a second needs distinct timestamps
  (`test_hub_stored_keys._online`).
- Without PyNaCl verify raises `VerificationUnavailable`, never says yes:
  do_POST answers 503 and nothing is done; a peer claim earns nothing. No
  /online then gets through, so the heartbeat monitor takes over no site
  for a missed heartbeat (`stale_takeovers_allowed`) and gives every online
  site a fresh grace period once verification is back. A recorded /offline
  is still acted on (it was verified when recorded). The tor image's build
  imports `nacl.signing`. `tests/test_hub_signature_verification.py`.

## Inside a Takeover Worker
- The queue manager's command socket is `/run/onionheaven-queue-manager.sock`,
  0600 from bind() (`bind_command_socket`, umask 0177). The root daemon loads
  any registrant's key into Tor on `takeover`; Tor runs there as debian-tor,
  and the old `/tmp/queue-manager.sock` was 0666. Every client is root
  (`docker exec` into a worker, image USER root, no `-u`); keep it so.
- The post-takeover audit fetches `healthcheck_address` only if it is one v3
  onion address (`onion_address_ok`); anything else is recorded as
  `invalid_healthcheck_address`: never fetched, never a release.
  `tests/test_takeover_worker_hardening.py`. Both ship in the tor image.

## OnionHome Name Registry Volume
- OnionHome's `onionnames.db` (onionname → onion address) lives on
  `onionpress-onionnames-data` at `/var/lib/onionnames`, mounted by
  onionpress-tor only (web-server.py is its only reader and writer) —
  **never wordpress**, never under `/var/lib/onionpress`. WordPress looks
  names up through `/api/name/lookup`. Host code (backup/restore) reaches it
  via `docker cp` on onionpress-tor and copies only `onionnames.db` and its
  `-wal` (`backup.ONIONHOME_REGISTRY_FILES`); the archive folder is still
  `onionhome/`. `tests/test_onionhome_registry_volume.py`.
- **A restore never puts the backup's file on the volume** (anyone can make
  a backup, and a Settings-page download can be swapped in WordPress):
  `backup.restore_name_registry` copies it into a private `mktemp -d` in
  onionpress-tor, runs the image's own `onionhome-registry-migrate.py
  --legacy-parent <tmp>/backup --dest <tmp>/rebuilt --no-mount-check` on it
  (the move's checks, below), and puts the rebuilt registry in place of the
  live one through SQLite's backup API (live -wal never applied to it). The
  name count before/after goes in the log. What the image refuses leaves the
  live registry alone and logs an ERROR; the rest of the restore goes on.
  Only install-from-backup restores it: `restore_container_artifacts` in
  the launcher's start hook (Tor up), and `onionpress restore`/the app's
  Restore..., which import before Tor starts and so call
  `restore_staged_name_registry` after `start_tor`. The 2.5.3 image has the
  script; keep its flags and marker JSON (`moved`, `names`, `left_out`)
  stable. `tests/test_onionhome_registry_restore.py` runs the real script.
- The Settings page's backups keep `onionhome/` (no secret in it, and an
  app restore of a backup without it would serve an empty, claimable
  registry). The page's restore refuses a backup carrying
  `onionhome/onionnames.db` (`settings_page_restore_refusal`) and never
  writes the volume (`hub_volumes=False`): that upload is WordPress's to
  make, and the registry left WordPress's reach so code there could not
  repoint names. On OnionHome itself the page restores nothing
  (`settings_page_restore_node_refusal`). Restore OnionHome from the app or
  `onionpress restore`.
  `tests/test_backup_hub_data_off_wordpress.py`.
- It used to be `onionhome/` on onionpress-persistent-data (www-data owns
  that folder). `onionhome-registry-migrate.py` (onionpress-tor's
  entrypoint, before web-server.py) reads only the db/-wal/-journal there,
  by O_NOFOLLOW descriptor, size-capped, into a private copy, and builds a
  new registry from the rows that pass onionnames.py's checks; SQLite never
  opens the old path. Kept separate from onionheaven-data-migrate.py on
  purpose. web-server.py serves no name (503) until the move's marker is on
  the volume (`onionnames.registry_in_place`), so the container never fails
  to start over the registry, and an old compose file or a failed move never
  serves an empty, claimable registry. A file with SQLite's header that is
  too large or sparse to read (db, -wal or -journal) is held — no marker,
  names at 503, retried every start, one ERROR line — never abandoned as
  "not a registry"; "sparse" means holes (SEEK_HOLE beyond SPARSE_SLACK),
  not few allocated blocks, which compressing filesystems report for dense
  files. Needs a tor image publish; on
  OnionHome compare the name count before and after, and never downgrade it
  afterwards (docs/DOCKER-SOCKET.md, "The OnionHome name registry").

## No Message Channel From the Hub (removed 2026-10-25)
- The healthcheck onion service (tor image, `healthcheck-server.sh`) is
  GET-only. Through 2.5.6 a POST to it stored any body under
  `/var/lib/tor/healthcheck-messages/`, and the Mac daemon, the menubar,
  the Linux tray and `onionpress onionheaven alerts` showed each file's
  `type` and `message` in a native "OnionHeaven Alerts" dialog. Nothing
  checked who sent it, nothing in OnionHeaven ever sent one, and the hub
  hands out healthcheck addresses on `/status/<address>`: a phishing
  channel to the owner's desktop for anyone. Both ends are gone (the
  handler, `onionheaven.read_hub_messages`/`remove_hub_messages`,
  `app_commands.alerts_command`, the `alerts` subcommand, the daemon's
  poll and `.dismiss-onionheaven-alerts`, the menubar item, the tray's
  poll). The GET's `"messages":[]` stays for an older hub's reader. A hub
  that wants to tell an owner something needs a signed channel designed
  for it. The image side needs a tor image publish; until then an old
  image still stores POSTs that nothing reads.
  `tests/test_install_invariants` (the handler under sh),
  `tests/test_mac_daemon` (the menubar census names `healthcheck-messages`).

## The Directory Answers Visitors Only on OnionHome (issue #394)
- `onionpress-directory.php` is a mu-plugin on every node, and its two
  request hooks (`/follow?name=` on parse_request, the `/NAME[/REST]`
  dispatch of a 404 on template_redirect) used to run for any request
  whose Host header was OnionHome's or onionpress.org: the Host is the
  visitor's to choose, so every node looked names up through Tor (30 s
  in an Apache worker, no cache) and redirected.
  `onionpress_directory_request_path()` now returns null unless
  `onionpress_directory_is_onionhome_node()` (the node's own served
  address, from the shared volume's `onion_address`, is
  `ONIONPRESS_ONIONHOME_ADDRESS`), before the Host check; the visitor
  handlers go through `onionpress_directory_lookup_for_visitor` (a miss
  is not asked again for 60 s, a transient). The Settings page's own
  `onionpress_directory_lookup()` calls (the owner's Following form, on
  every node) are untouched. `ONIONPRESS_ONION_ADDRESS_FILE` lets a test
  point the address read elsewhere. onionpress-status-hint.php (a 302
  to the hub's fixed address) and onionpress-user-path.php
  (`/<onionname>/` on every install, by design) stay on every node.
  `tests/test_onionname_redirect.py` (the gate under php where installed).

## OnionHeaven Is On by Default
- **`REGISTER_WITH_ONIONHEAVEN`: `yes` or unset/empty registers, `no` opts
  out** (and unregisters, below); any other value (a typo) does neither, so
  a mistyped `no` never registers. Nothing asks: the startup prompts (Mac
  `prompt_enable_onionheaven`, Linux tray `_maybe_prompt_onionheaven`) were
  removed 2026-10-21 at Ivar's request, reversing the 2026-10-11 opt-in
  decision. Fresh configs say `yes` (template, Linux launcher, both
  installers, `config.DEFAULTS`); the Mac launcher's inline fallback leaves
  it out, which reads as yes. Installs left undecided by 2.5.x start
  registering at the upgrade, with no prompt. The heartbeat ships the onion
  private key to the hub: the README, config template, Settings dialog and
  page, `onionpress onionheaven status` and install.sh's banner say so and
  how to turn it off. `onionheaven.registration_choice()` is the Mac
  normalizer, the daemon's `_registration_choice()` the Linux one; every
  gate is `== "yes"`, never `!= "no"`. The Settings dialog shows unset as
  checked; the page offers only Enabled/Disabled.
  `tests/test_onionheaven_opt_in.py` enforces it.
- Linux CLI: `onionpress onionheaven enable|disable|status`. Both platforms
  re-read the choice before every request that carries the key (every
  `/online` does: the hub refuses one without `arti_key_pem`), so enabling
  and disabling need no restart: the Mac heartbeat loop ends at its next
  beat and `_send_heartbeat` refuses under `_HUB_LOCK`; the Linux daemon
  stops in the next cycle. Nothing goes out unless `yes` (or unset): no `/online`, no
  `/offline` (it makes the hub take over at once), on sleep, stop or quit.
- **An explicit `no` takes the key back**: when the registration record
  (`~/.onionpress/onionheaven-registration.json`, "registered": the hub may
  hold the key) says so, one signed `/unregister` for the whole address (no
  healthcheck_address: every row, so the hub deletes the key dir) goes to
  the hub the record names, then the record is cleared. Mac:
  `maybe_unregister_after_opt_out` from the daemon's check_status (also at
  startup with `no`), ordered after any heartbeat in flight by `_HUB_LOCK`; Linux:
  `_maybe_unregister_after_opt_out` in the daemon, which used to send
  `/offline` on disable (a takeover of a running site, key kept). Retried
  every 15 min while the hub does not answer; "Entry not found" counts as
  done. An unrecognized value sends nothing, not even this.
  `tests/test_onionheaven_opt_out.py`.
- **Every end of a hub's hold on a key takes the key back, whatever the
  choice** (the user ends it explicitly): uninstall, reset (and scrub's), a
  key import, a restore or `setup --from-backup` of another address send
  the same signed `/unregister` before the key goes, via
  `onionheaven.release_registration` (menubar: uninstall, and the Settings
  page's restore through `restore_from_backup(before_key_change=)`; all else
  via `onionpress.cli onionheaven-unregister` — the launchers'
  `onionheaven_release`, `cmd_reset`, `cmd_restore`). Only the key in the tor
  container can sign: if OnionPress is not running or the hub does not
  answer, the launchers ask before going on, and the pair stays in the
  record's `left_on_hubs` (a reset keeps the record) so `onionpress
  onionheaven status`, the uninstall and restore dialogs and the log say what
  remains on which hub. Linux stops the heartbeat client first
  (`stop_heartbeat_client`, user and system unit): it holds the key in
  memory. A hub change: Mac `maintain_registration` from the daemon's
  check_status (`ONIONHEAVEN_ADDRESS` is in `_NEEDS_RESTART`), Linux the daemon; an old hub
  that does not answer stays in `left_on_hubs`, retried every 15 min. The
  daemon also notices a key replaced under it and unregisters the old one
  with the copy in its memory before it exits. Between processes,
  `_hub_lock` (`_HUB_LOCK` + flock on `~/.onionpress/onionheaven-hub.lock`)
  and the record's 10-min `released` hold keep the Mac daemon's heartbeat
  from putting a released key back before the launcher removes it;
  `onionpress uninstall` stops the daemon first (`uninstall.run`).
  `tests/test_onionheaven_lifecycle.py`.
- **Hub mode is decided by address on both platforms** (issue #345):
  status.json's `onionheaven.server_active` (the status page's "Mode",
  the Settings page's takeover tunables) is
  `onionheaven.is_onionheaven_instance(<served address>)`, the Mac
  daemon's rule; the Linux launcher's `write_status` asks it through
  `is_onionheaven_hub` → `onionpress is-onionheaven-hub <address>` and
  queries the hub's registry only when it says yes. It used to be "a
  container named `onionheaven` is running", which every node has, so
  every Linux node called itself the hub. `ONIONHEAVEN_ACCEPT_REGISTRATIONS=yes`
  on another address is not counted, as on the Mac.
  `tests/test_linux_hub_mode.py`.

## Backups Carry Original Images Only (issue #257)
- `create_backup` prunes WordPress's regenerable derivatives (`-WxH` sizes,
  `-scaled`) from the staged `wp-content/uploads` — about half of a media-heavy
  site's uploads. The rule lives in `src/onionpress/derivatives.py` and is the
  one WordPress uses: a file is a derivative only if its base image sits in the
  same directory; a true original that merely ends in `-123x456.jpg` is kept.
  Video, audio, PDFs and everything else are never touched. Linux's inline bash
  `create_backup` calls `onionpress.cli prune-image-derivatives` so both
  platforms share the classification.
- **The two halves must ship together.** A restore writes
  `~/.onionpress/.regenerate-derivatives-pending`; every launcher start
  (both launchers' `regenerate_derivatives_pending()`, a delegation to
  `onionpress regenerate-derivatives-pending` over
  `derivatives.regeneration_pending`, and the CLI `start`/`restore` paths) then runs
  `onionpress.cli regenerate-derivatives` detached in the background:
  `nice`d `wp media regenerate --only-missing --yes` per site. `--only-missing`
  makes it resumable, so the marker is cleared only when a sweep completes.
- The archive also carries `wp-content/uploads/.onionpress-regenerate-derivatives`,
  which a plain `docker cp` of wp-content delivers into the container: an older
  OnionPress restoring a new backup still leaves the signal behind, and the
  `onionpress-image-fallback.php` mu-plugin keys off that file to serve the
  original for any size not yet on disk (heavier images, never broken ones).
- `metadata.json` gains `excludes_image_derivatives: true`. Old backups restore
  as before (the sweep finds nothing to do).

## Backup/Restore Time Limits Grow With the Site (issue #317)
- No fixed limit on a copy: each gets a floor plus its size at
  `wp_files.COPY_BYTES_PER_S` (`wp_files.sized_timeout`): the dump (from
  BACKUP_STATS' db_bytes, streamed to its file by `_run_db_client_to_file`,
  never held in memory), its import (sql size, at the slower
  `_DB_IMPORT_BYTES_PER_S`), wp-content out (`stage_wp_content`) and in
  (`put_tree`, default sized from the tree), zip, unzip, and the Settings
  page's upload/download copies. A process past its limit is killed.
- The UIs that start the launcher's `backup`/`restore` wait for it (no
  timeout: killing the launcher stopped only the shell, its docker and
  Python steps ran on) and notify on failure (tray `_notify_failed`).
  `tests/test_backup_timeouts.py`.
- The Mac Settings page's restore is NOT the menu's: it stays the in-place
  `restore_from_backup(through_wordpress=True, before_key_change=,
  confirm_address_change=)`, now in `cli.cmd_settings_page_restore`
  (`onionpress settings-page-restore`, which `handle-action` runs), then a
  restart. The launcher's `restore` (install-from-backup, the menu's
  Restore... and `onionpress restore`) has none of that path's checks
  (hub/OnionHome refusals, address-change dialog, no hub volumes written),
  so a backup and password uploaded through WordPress must never reach it;
  #341's switch to it was left out for that reason (fae542dd). Linux's
  Settings-page restore is still bash's in-place `restore_backup
  --from-settings-page`, behind the launcher's `settings-page-restore` case.
- **The menubar performs no operation itself** (issue #366, CLI-first
  phase 2): Backup..., Restore..., Check for Updates..., Uninstall, the
  welcome screen's restore and the Settings page's requests run
  `onionpress backup|restore|update|update-images|uninstall|prepare-restore|
  handle-action` through the launcher (`_run_launcher_json`: the answer is
  the JSON object the CLI prints last, secrets go in the environment, stdin
  is /dev/null) and show what comes back; it keeps the dialogs, the progress
  window (the CLI's log lines streamed in) and the notifications, and a
  failed run is a dialog with the CLI's message, never only a log line.
  `src/menubar.py` imports none of `backup`, `hub_backup`, `updater`; it
  uses `key_manager` only to validate an address and `wp_files` only for
  the first-run setup's wp-cli calls (`tests/test_menubar_actions.py`). A new
  menu action gets its subcommand first. Nor does it run a loop (phase 3,
  "The Mac Daemon" below).
- `wp_files.own(recursive=True)` (put_tree's first step, so every restore)
  never enters a folder named `creations` (My Creations' bind mount), and
  follows no link (find -execdir chown -h), like onionpress-web-root-owner.sh.

## Backup Encryption (backup format 2)
- New backups are an ordinary, unencrypted `.zip` holding `README.txt` and
  `onionpress-backup.enc`: the same zip of the same members, encrypted with
  **libsodium through PyNaCl, using its constructions as they are** —
  Argon2id (`crypto_pwhash`, INTERACTIVE preset: opslimit 2, 64 MiB) for the
  key, `crypto_secretstream_xchacha20poly1305` in 1 MiB messages with the
  header as additional data. **Maintainer rule: crypto comes from
  established libraries, never constructions of our own** (an earlier,
  never-shipped format 2 built scrypt + SHAKE256 + HMAC from the standard
  library and was replaced). The layout is in
  `src/onionpress/backup_crypto.py`'s docstring; `tests/fixtures/backup-format-2.zip`
  (sha256-pinned, never regenerate) and an independent decoder in
  `tests/test_backup_manager.py` hold it in place. Changing the bytes means
  a new format.
- KDF parameters are INTERACTIVE because every backup must restore on a
  1-GB Raspberry Pi beside the running site; a reader refuses (as "newer")
  headers asking for more than opslimit 4 / memlimit 128 MiB before running
  Argon2id. A first message that fails to decrypt is reported as a wrong
  password (damage to the header or first message reads the same).
- **PyNaCl is a dependency everywhere**: `src/requirements.txt` and setup.py
  (`nacl` package, `_cffi_backend` include) on Mac; `python3-nacl (>= 1.5.0)`
  in the .deb and `linux/install.sh`; `pip install pynacl` in CI (plus a
  pass against 1.5.0, the Debian/Ubuntu version). `backup_crypto` imports it
  guarded: without it, format 2 raises `SodiumUnavailableError` naming
  python3-nacl, and format-1 restores and everything else still work.
  `tests/test_backup_manager.py` checks all of this wiring.
- **PyPI's macOS PyNaCl wheels carry an unoptimised libsodium** (~7x slower:
  450 ms vs 67 ms per key derivation). `build/py2app-extensions.sh prepare`
  rebuilds PyNaCl from source with `-O2` and lipo-merges cffi's per-arch
  `_cffi_backend` for universal2 builds; `verify` runs the bundle's
  interpreter (also as x86_64 under Rosetta) and prints the speed. Both build
  scripts call it. Run the unit tests in a venv with PyNaCl (`make test-unit`
  does this via uv); without it ~60 backup tests error.
- **Format 1 (ZipCrypto zips from `zip -P`, everything through 2.5.2) must
  restore forever.** `backup.extract_archive` picks the decoder by whether
  the zip holds `onionpress-backup.enc`. Never write `zip -P` again; the
  tests fail if a launcher does.
- `metadata.json` carries `backup_format: 2` (absent = 1); a higher number,
  or unknown version/kdf/cipher/reserved bytes in the container header, is refused as
  "made by a newer version of OnionPress".
- The bash launchers go through `onionpress.cli pack-backup` / `extract-backup`
  (password on stdin); Linux's inline create_backup no longer zips itself.
- OnionPress 2.5.2 and older cannot restore a format-2 backup (they report it
  as not a valid OnionPress backup); the README inside the zip says so.
- A hub's backup may carry one more member inside the encrypted zip,
  `onionheaven.sealed` (its registry and registrants' keys sealed to the
  hub's recovery key: "OnionHeaven Hub Data Volume" above). It is an
  ordinary member: the container format, its bytes and the fixture are
  unchanged, and the README says the hub data is sealed.

## Restores Extract Plain Files Only
- A backup file is its maker's, password or not. `backup.extract_archive`
  (every restore path; the launchers reach it through `extract-backup`) runs
  `validate_archive_members` on the member list before either extractor
  runs — format 1's outer zip (readable without the password), format 2's
  decrypted inner zip — and refuses symlinks, device/FIFO/socket entries,
  absolute or `..` names, NULs, empty names, duplicates and file/folder
  clashes, header and Unicode-path-field names alike. `unzip` recreates
  symlink entries, and `docker cp "$staging/wp-content/." …` resolves a link
  in its source path: one `wp-content -> ~` member published the home folder.
- `check_staged_tree` (lstat walk) re-checks the extracted tree, and
  `restore_container_artifacts` runs it again before its first docker cp.
- Real backups hold no links: `zip -r` without `-y` stores a link's target
  as a plain file. pack_backup must never store links (`zip -y`), or its
  backups stop restoring. `tests/test_backup_archive_members.py`.
- Zip bombs: `check_archive_size` refuses, before extracting, an archive
  listing more than 500,000 members or more data than the larger of 8 GiB
  and 50x its own size. A listed size is not enforced by Apple's `unzip`
  (it wrote 8 MiB for a member listing 1 KiB, exit 0), so format 2's inner
  zip is extracted by zipfile only (holds each member to its listed size and
  CRC, never makes a link; faster than unzip here). Format 1 keeps
  `unzip -P` (zipfile's ZipCrypto is ~2 MB/s) under `ulimit -f` at the
  largest listed member, with a watchdog on the disk's free space; the
  extracted tree may hold no more than the archive lists. metadata.json
  reads stop at 1 MiB. `tests/test_backup_extract_limits.py`.
- Restored keys are checked before anything uses them: the site's key
  files must parse and agree (`key_manager.staged_key_address`), and so
  must Linux's healthcheck key, whose hostname must name exactly its
  address (`backup.staged_healthcheck_address`: tor-watchdog.py puts that
  service id into DEL_ONION on the control port). Only its key files are
  copied and the hostname is written from the key; the Settings page's
  restore takes no healthcheck key at all.
  `tests/test_backup_healthcheck_key_restore.py`. The watchdog checks too
  (tor image): a service id comes only from one whole v3 address, else the
  hostname is rewritten from the public key
  (`tests/test_tor_watchdog_service_ids.py`).
- **A restored dump, and the DB password re-set after it, run only SQL.**
  The MariaDB client honours `source`/`\.`, `\!`/`system`, `tee` and `pager`
  inside a sourced file — a root shell in onionpress-db, reachable from
  WordPress through the Settings page's restore — and `LOAD DATA LOCAL
  INFILE` reads the exec'd process's inherited `MYSQL_ROOT_PASSWORD` out of
  `/proc`. So the one import, `backup.db_import` (what
  `restore_container_artifacts` runs; the Linux launcher's bash `db_import`
  is a one-line delegation to `onionpress db-import`, the password on
  stdin's first line, never argv; the Mac launcher has none, its restores
  go through `restore_container_artifacts`), streams the dump on
  stdin after the password line — never `docker cp`'d to a fixed
  `/tmp/wordpress.sql` and `source`d — through a client run
  `--sandbox --binary-mode --local-infile=0`, and refuses a client too old
  for `--sandbox` rather than fall back. mariadb-dump's own output still
  imports (its first line is the sandbox directive). The password re-set
  (`ALTER USER … IDENTIFIED BY`) no longer splices a wp-config.php value
  (WordPress's to write, so `x'; \! cmd` gave a root shell on any restore):
  the password is the host secrets file's (`_restore_db_password`, what the
  bash launchers already use), validated by `_reject_unsafe_db_value` (no
  quote/backslash/backtick/control char; DB_NAME/DB_USER also no leading
  dash, checked in `_get_db_credentials` for every caller), and the root
  client runs `--binary-mode --sandbox` too. Needs no image publish.
  `tests/test_db_import_sandbox.py`, `tests/test_secrets_off_argv.py`.

## Backups Stage Plain Files Only
- What create_backup copies out of a container (wp-content; a hub's
  onionheaven/ and onionhome/) may hold links planted as www-data, and
  `zip -r` stores the file a link points to: `uploads/x -> ~/.ssh/id_ed25519`
  went into a backup whose password the Settings page lets the site choose.
  The derivative pruning walked a linked uploads/ and the marker was written
  through one.
- Mac: the container's `tar -cf - --hard-dereference` stream is unpacked by
  `backup.unpack_plain_tar` (regular files and folders only, O_NOFOLLOW) —
  never a host `tar -x`. Each staged tree (and docker cp'd onionheaven/,
  onionhome/) then goes through `scrub_staged_tree` (lstat walk: removes
  symlinks, devices/FIFOs/sockets and hard links to files outside the tree,
  logs each by printable name) before pruning or the marker touch it.
  Linux: `onionpress.cli scrub-staging` right after the wp-content docker cp.
  `pack_backup` scrubs the whole staging again before zip, on both
  platforms. `tests/test_backup_staging_links.py`.
- The Settings page's restore docker-cp's `restore-upload.zip` out of the
  container (a link stays a link) into a mkdtemp folder of its own and
  refuses anything but a regular file before reading it
  (`backup.check_uploaded_backup`; Linux: `[ -L ] || [ ! -f ]`).
  `tests/test_settings_restore_upload.py`.
- The Settings page's finished backup is written into the container by
  `docker exec -i -u www-data … sh -c 'umask 077; cat > "$1"'`, never by
  root's `docker cp` + `chown`: both followed a link www-data can plant at
  the predictable name, and the chown handed any container file to
  www-data. `tests/test_settings_backup_handoff.py`.

## Root Never Writes Through www-data's Names
- www-data owns /var/lib/onionpress (onionpress-data, also mounted in the
  tor container) and the web root, and can plant a link at any name there.
  Root in a container follows it: `tee`/`cat >`, `docker cp` to the name or
  through a linked folder, `chown` of a path, a trailing-slash `chown -R`.
  So everything the host hands the WordPress container goes in as www-data
  (`onionpress.wp_files`): `put_file_args`/`put_file` (temp file + rename,
  so a planted link is replaced, never followed; status/results mode 600,
  web-root files 644), `put_tree` (a tar of plain files unpacked by
  www-data: theme, plugins, the restore's wp-content), `exec_args` for the
  reads and removals of what the page writes. The bash launchers use
  `wp_put` (the same script, test-enforced) and Linux `wp_exec`; their
  restores and plugin installs go through `onionpress.cli put-wp-tree`.
- Root's only remaining chown there is `wp_files.own_args`: `cd -P` into the
  folder, check `pwd -P`, then `chown -h`/`-R` of a bare name. Never
  `chown … /var/www/…`, never a trailing slash.
- wp-content is read for a backup as www-data too (`backup.wp_content_tar_argv`,
  Linux `onionpress.cli stage-wp-content`): root's `tar -C`/`docker cp`
  followed a wp-content link to any folder into a backup the site gets back.
- The onion address is read from the tor container and written by www-data,
  never `cp`'d by root in the tor container (it followed a link to the key).
  tor-watchdog.py reads it with O_NOFOLLOW and takes only one v3 address:
  it goes into control-port commands, where a CR/LF would add one.
- The image's security audit (root, every start) builds its report in a
  private temp file and publishes it with `rm -f` + `dd conv=excl`;
  quarantine is /var/lib/onionpress-quarantine (root, 700).
- `tests/test_wordpress_writes_as_www_data.py` runs the scripts against a
  sandbox and scans the sources for new root writes. The hub's onionheaven/
  and onionhome/ folders in the shared volume are not covered yet.

## wp-cli Runs as www-data
- wp-cli loads WordPress: wp-config.php, mu-plugins, plugins, themes, all
  www-data's to write. Every call ran as root (`--allow-root`), so any PHP
  execution in WordPress became root in the container at the next call.
  Host Python: `wp_files.wp_cli_args` (`docker exec -u www-data -e
  WP_CLI_CACHE_DIR=/tmp/wp-cli-cache … wp`) or `Docker.wp`; the launchers:
  `wp_cli` / `wp_cli_in` (stdin); activate_plugin.sh the same; the image's
  scripts: `onionpress-wp` (setsid + runuser to www-data). **Never pass
  `--allow-root`**: without it wp-cli refuses root, so a regression fails
  instead of running WordPress as root. `tests/test_wp_cli_as_www_data.py`
  scans everything that ships for `--allow-root` and for "wp" argv lists
  outside wp_files, and keeps the copies equal.
- **The host's provisioning wp-cli calls make no outbound request**
  (issue #373): `wp_files.wp_cli_args(offline=True)` adds `-e
  ONIONPRESS_WP_OFFLINE=1`, and the onionpress-tor-proxy mu-plugin's first
  `pre_http_request` filter (`onionpress_tor_proxy_offline`, priority 1)
  answers every request to a host outside Docker with a WP_Error before
  the SOCKS probe; its later filters hand a set `$pre` on. `multisite._wp`
  passes it by default (provision-post-install, setup-bundled-plugins'
  steps, deactivate-wp-statistics): wp-cli loads WordPress, `wp theme
  list` runs `wp_update_themes()` through Tor, and with Tor down each
  such request waited for the proxy's timeout until the 60 s call expired
  and START SUMMARY said provision-post-install failed with the site up.
  Callers that download (`setup_logic`'s `wp language core install`, the
  daemon's `Docker.wp`, the bash `wp_cli`) are unchanged. The mu-plugin is
  the app's (`MU_PLUGINS`, put in place by provision-post-install before
  its first update-checking call), not the image's: no image publish.
  `tests/test_wp_cli_offline.py` locks the name to the PHP literal and
  runs the plugin under php where it is installed (CI).
- What wp-cli prints is www-data's to choose. Root acts on none of it but
  the audit's downloaded core zip, removed only when the name is
  `/tmp/onionpress-wordpress-<version>.zip`.
- `onionpress-web-root-owner.sh` (entrypoint, every start, before
  multisite-init and the audit) gives www-data what root's wp-cli left in
  /var/www/html (core files from the audit's root `wp core update`,
  wp-content/upgrade, Cache Enabler's files): www-data's core update fails
  on them otherwise. `find -execdir chown -h`, never following a link;
  every folder named `creations` is pruned (the My Creations bind mount:
  a mount point cannot be renamed), and /proc/self/mountinfo must show
  every mount below the web root to be such a folder (Docker follows a link
  at the mount path when it starts) and nothing mounted over the web root,
  or nothing is changed.
- App and WordPress image ship together: an old image's scripts still run
  wp-cli as root, an old app's calls too.
- install_plugin.sh installs pinned releases only (`plugin_pin`: version +
  the zip's SHA256; versioned URL, still over Tor); no pin, no install.
  `build/pin-wp-plugin.sh <slug> <version>` prints a pin.
  `tests/test_plugin_pins.py`.

## Cache Enabler and the Link Fixer Are Active
- Until this release neither was ever activated: activate_plugin.sh forced
  DOCKER_HOST to the Colima socket (nothing on Linux), used timeout(1)
  (none on macOS) and, earlier, `docker exec -T`; the .deb shipped neither
  script. It now runs in the launcher's environment, `--network` or
  `--each-site`, exit 2 when WordPress is not installed.
- Both launchers, identical functions (test-enforced): `install_ia_plugin`/
  `install_cache_enabler` → `install_wp_org_plugin` (pinned release; any
  other installed version is replaced) before provision-post-install (which
  installs the policy mu-plugins), then `setup_bundled_plugins`:
  `configure-cache-enabler` (FS_METHOD=direct; refuses without
  onionpress-cache-policy.php) → `activate_plugin.sh … --network` →
  `apply-cache-policy` (`wp onionpress-cache-policy apply` re-validates every
  site, then settings files and stored pages are removed as www-data); and
  `configure-ia-plugin` (per site, before activation, once per
  `IA_CONFIG_VERSION`; refuses without onionpress-link-fixer-policy.php) →
  `activate_plugin.sh … --each-site`. Every start, and once more via the
  `setup-bundled-plugins` subcommand that `install_fresh_wordpress` starts
  in the background after the user's subsite exists. `INSTALL_IA_PLUGIN` /
  `INSTALL_CACHE_ENABLER=no` skip a plugin (an active one stays active).
- Why network vs per site: Cache Enabler's drop-in and page cache serve the
  whole network and the mu-plugin fills in every site's settings (new sites
  too); the Link Fixer creates its link table only in the site it is
  activated on (network activation left the user's subsite failing on save).
- **Cache policy** (onionpress-cache-policy.php): pages are stored only for
  Host == this install's onion address exactly (no port), anonymous, no
  Set-Cookie or no-cache/private header, not is_404, not the My Creations
  or Directory templates. Serving happens before WordPress loads and checks
  only the settings file's exclusions, so every site gets, whatever is
  saved: any query string, `wordpress*`/`wp-postpass`/`wp-settings`/
  `comment_author` cookies, wp-admin/wp-json/wp-login.php/onionpress-status
  paths, expiry 1–24 h. Cache Enabler clears the home URL's host, which the
  domain map makes the request's (localhost for the owner): a `clean_url`
  filter rewrites it to the onion address, only inside
  `Cache_Enabler_Disk::cache_iterator`. A settings change drops the onion
  address's settings files; settings files of any host but the onion,
  localhost, 127.0.0.1, [::1] and wordpress are removed at shutdown (Tor
  visitors choose the Host header).
- **Link Fixer policy** (onionpress-link-fixer-policy.php): its requests
  (WP-Wayback-Link-Fixer header) never name an onion, local or private link
  (refused before the HTTP call; its own exclusions do not stop the
  lookups), always verify TLS (its Save Page Now POST turns it off), get
  30–60 s; the existing-posts scan does 5 posts per 15 min, and an imported
  post (`_source_id`) is left to that scan on its first save.
- Re-pinning either plugin means re-reading these against its internals
  (docs/BUILDING.md). `tests/test_bundled_plugins.py`.

## The Hit Counter Has One Writer (issue #398)
- `onionpress-hit-counter.php` is the plugin folder's only PHP: anything
  else under wp-content/plugins is reachable directly, and
  `standalone-counter.php` (SHORTINIT, a raw UPDATE of the main site's
  wp_options for anyone) was a second, unauthenticated writer. The
  increment checks the nonce it mints for its script
  (`check_ajax_referer('onionpress_counter_nonce', 'nonce', false)`) and
  on failure answers the current count without counting, so a page
  served from the cache past the nonce's life still shows a counter.
  The legacy `/var/lib/onionpress/hit-counter.txt` migration is gone
  (nothing wrote it since the option). `tests/test_hit_counter.py`.

## The Wayback Sweep Is the Network's (issue #322)
- One sweep walks every site, so its state is the network's, never a blog's:
  - **archive.org keys** live in the main site's options. Setup, the
    reconcile and Linux's installer write them with plain `wp option update`
    (no `--url`); the Settings page saves them there from any site
    (`onionpress_archive_keys`/`_save`). Blank account/password fields keep
    the keys; removing them takes the `op_ia_credentials_remove` checkbox.
    `onionpress_archive_keys_move_to_main_site` moved a pair saved on a
    subsite through 2.5.3 once (it wins over the main site's).
  - **lock and back-off** are network options, only through
    `onionpress_wayback_net_get/_set/_delete` (the get drops the object
    cache, so a long-running sweep sees a takeover). save_post and comments
    no longer clear the back-off.
  - **schedule**: only the main site's cron runs the sweep; subsites clear
    theirs on init, and `onionpress_wayback_schedule_now` puts immediate
    sweeps on the main site's cron (the tor healthcheck loads it every
    minute).
- **One archive.org login**: `multisite.fetch_archive_s3_keys` (archive.org's
  onion via onionheaven's Tor, form and cookie on stdin) serves setup
  (`onion_proxy._fetch_archive_s3_keys`) and `ensure_archive_s3_keys`.
  Linux's bash `fetch_archive_s3_keys` uses the same onion.
- `tests/test_wayback_network_state.py`; test_wayback_sweep.py points the
  lock and back-off at test keys through the `onionpress_wayback_net_key`
  filter so it never touches a live sweep's.

## The Mac Update Installs Only a DMG Its Release Vouches For (issue #386)
- `updater.download_and_install` downloads the release's `SHA256SUMS`
  (`_fetch_release_sums`, the third curl through `_curl_tls_args`, 64 KiB
  cap) and mounts the DMG only when `sha256_of` it equals the one line
  `published_sha256` finds for the asset's name (sha256sum format, as
  `build/release.sh` writes it and Linux's `update_find_release` reads
  it); a release without the file, a file without exactly one line for
  the DMG, or a mismatch stops before `hdiutil attach`. Same trust root
  as before (TLS to GitHub) but a swapped or damaged asset is caught, as
  on Linux. Still open in the issue: signing `SHA256SUMS` with a key the
  app carries (with #246, the .deb side) and Developer ID + notarization
  with `codesign --verify` on the staged bundle, which need a key the
  maintainer holds. `DmgChecksumTest` in `tests/test_updater_staged_swap.py`.

## Auto-Update Swaps Bundles by Rename
- `updater.download_and_install` copies the new bundle from the mounted DMG
  into a hidden sibling folder (`/Applications/.onionpress-update-*`,
  `stage_new_bundle`), strips quarantine there, then renames the old bundle
  to `.bak-<pid>` and the staged one into place (`swap_in_bundle`). **Never
  write into `/Applications/OnionPress.app` while the app runs**: the
  MenubarApp's `bin_dir` is that absolute path, so after the old bundle is
  renamed away its next docker call (health probes every few seconds)
  executes out of whatever sits at that path, and macOS 26/27's Gatekeeper
  then revokes the writing process's access to the file it has open
  (`fcopyfile failed: Input/output error`, `fchmod failed: No such file or
  directory`, then `Operation not permitted` for every further write). That
  is how 2.5.2 → 2.5.3 failed on 2026-10-13 (the 2.5.1 → 2.5.2 copy, same
  code, just won the race). Reproduced 3/3 with an ad-hoc-signed test app;
  the staged swap passed 150 concurrent execs. `tests/test_updater_staged_swap.py`.
- The fix only helps once an app carrying it is installed: the *running*
  app's updater does the update, so the first update after a release with
  this change still runs the old code.

## Multi-User Support (v2.4.11+)
- Multiple macOS users can run OnionPress simultaneously from the same `/Applications/OnionPress.app`
- Each user gets their own `~/.onionpress/` data dir, Colima VM, and Docker containers
- **Port offsets**: second user auto-detects port 8080 is taken and offsets all ports by +10000 (18080/19050/19077), third user by +20000, etc. Max ~5 users.
- **Detection uses socket bind test**, not `lsof` — `lsof` only sees the current user's processes and cannot detect ports bound by other users
- **Port detection happens in the daemon at its start** (`daemon._resolve_ports_at_start`: `config.resolve_port_offset`, the running stack's published port first, else the bind probe), after `stop_stale_colima` and the `.previous-port-offset` wait that used to be the MenubarApp's `__init__`. The result goes into the daemon's environment (its launcher calls inherit `ONIONPRESS_PORT_OFFSET`) and the state file's `ports`; the menubar takes its ports from there (the environment's or the last state's until the first state lands) and exports them before it runs the launcher
- **Module-level constants in `onionpress.onion_proxy`** (`PROXY_PORT`, `PHP_PROXY_PORT`) are set at import time. The daemon updates these globals after detecting the offset (`_apply_ports`); the proxy is its
- The `onionpress` shell script also has detection as a fallback (for standalone use, and for the launchd agent, which starts with no offset in its environment), but respects a pre-set `ONIONPRESS_PORT_OFFSET` env var
- **`LSMultipleInstancesProhibited` must NOT be in any Info.plist** — macOS enforces it across ALL users sharing the same app bundle, not just per-user
- **`pgrep` in the launcher must use `-u $(whoami)`** to restrict to the current user's processes
- **PID lock file** (`~/.onionpress/onionpress.pid`) prevents the same user from double-launching; cleaned up via `trap` on EXIT/INT/TERM/HUP. The daemon's is `daemon.pid`; the agent is per user (`gui/<uid>`), so each user's daemon has its own offset
- **launcher.sh's already-running check matches THIS bundle's MenubarApp**
  (issue #372): `pgrep -u "$(whoami)" -f "^${MENUBAR_EXE_RE}( |$)"`, the
  bundle's own `Contents/Resources/MenubarApp/Contents/MacOS/OnionPress`
  with its ERE metacharacters quoted by sed. The old `^[^ ]*MenubarApp/…`
  matched any bundle's, so a second bundle of the same user (a build
  under test with its own HOME) never started beside the installed one
  and touched `.reopen` in the other install's data dir. `onionpress quit`
  already looked for `$RESOURCES_DIR`'s. The launchd agent's label is
  still one per user, so two bundles of one user share the daemon.
  `tests/test_launcher_pgrep_anchor.py` runs the block under bash.
- **Container-internal ports are NOT offset** — Docker networking (`onionpress-tor:9050`, `wordpress:80`) is isolated per-VM. Only host-side port mappings change.
- `OnionPress.app/` is gitignored — it is assembled at build time from `app/` source. Never commit build output.

## Browser Extension (issue #318)
- The extension (`extension/`, shared by both packages; `extension-firefox/`
  holds only the Firefox manifest, test-enforced) routes `.onion` to the
  Mac daemon's HTTP proxy (`onion_proxy.py`, `127.0.0.1:<proxy_port>`,
  started by `daemon.start_onion_proxy` while the stack runs), never
  to Tor's SOCKS port (see the Colima gotcha below). The port comes from the
  native messaging host (`get_proxy_port`, from Docker's published WordPress
  port: +10000 per extra macOS user); 9077 without it.
- `~/.onionpress/extension-connected` is JSON `{timestamp, browser}` from
  both writers (`browser.write_extension_marker`; the native host's own
  copy, test-tied). The extension polls `/status` every 5 s (not logged).
- **The proxy carries `.onion` only and answers no other site's page**
  (2026-10-25). Any web page in any browser on the Mac can send requests
  to 127.0.0.1, and `/status` answered them with the served onion address
  under `Access-Control-Allow-Origin: *`: a clearnet site could learn
  which onion site its visitor publishes. Now `/status` and `/proxy/`
  refuse a request made for another site's page
  (`OnionProxyHandler._from_a_web_page`: an `Origin` that is not the
  extension's or the proxy's own, a `Sec-Fetch-Site` other than
  `same-origin`/`none` (`same-site` is localhost:8080's page), a foreign
  `Referer`; the extension's fetch, the setup page, the address bar and
  curl pass), `/status` echoes only an extension `Origin` and sends
  `nosniff`, and every non-onion target is refused: no clearnet fetch
  (`_fetch_direct` is gone), no `CONNECT` relay (it tunnelled to any
  host:port for any local client, the LAN and the Mac's loopback
  included). The extension routes only `.onion` here, so nothing shipped
  used either. `tests/test_browser_extension.TestProxy`,
  `tests/test_strict_onion_addresses.TestOnionProxyRouting`.
- Fallback: `.onion` while OnionPress is down → `offline.html`; onion
  service not answering → the proxy's `ONION_UNREACHABLE_PAGE`. Both only
  *link* the Wayback Machine (clearnet, said on the page), never redirect.
- Not live-tested on a Mac yet. The native host is registered at a `.py`
  path run by `/usr/bin/env python3`: in a py2app build that file sits in a
  zip and the system python3 is a shim, so the native host probably never
  starts on a real install (then the port falls back to 9077).
  `tests/test_browser_extension.py`.

## Cloudflare Tunnel Runs on Both Platforms (issue #352)
- A `CLOUDFLARE_TUNNEL_TOKEN` in `~/.onionpress/config` adds
  `docker-compose.cloudflare.yml` and starts `onionpress-cloudflared`.
  Both launchers read it with `cut -d= -f2-` (padding, #323); Linux in
  `read_cloudflare_token`, again after `apply_config_updates` (the page's
  Restart applies and starts in one process). Through 2.5.4 Linux never
  read it.
- Linux starts cloudflared only once WordPress is installed (its install
  page would be on the open internet), removes the container when the
  token goes (`docker rm -f`, so the old token leaves its config), and
  every `down` takes `COMPOSE_WITH_CF` so a tunnel outlives no token.
  A token set or removed on the Settings page shows Linux's restart prompt.
- The ingress must be `http://wordpress:80`: the ONIONPRESS-TOR chain
  refuses the LAN and the host. `tests/test_linux_cloudflare_tunnel.py`.

## Colima Networking Gotcha
- **SOCKS proxy (port 9050) does NOT work through Colima VM port forwarding** — connections are accepted then immediately closed
- **For ANY communication over Tor from the Mac, always use `docker exec` into the tor container** — this is reliable
  - Do NOT use `curl --socks5-hostname 127.0.0.1:9050` from the Mac host — it will fail
- This applies to future mirror system communication (health checks, challenge-response, etc.)
- **`wget` inside the tor container CANNOT fetch external .onion addresses** — it doesn't support SOCKS proxies, so it can't resolve .onion through Tor. `wget` only works for internal container-to-container requests (e.g., `wget http://wordpress:80/`).
- **To fetch external .onion addresses, use `socat` with SOCKS4A** inside the tor container:
  - `printf "GET / HTTP/1.1\r\nHost: <address>.onion\r\nConnection: close\r\n\r\n" | socat -t 10 - SOCKS4A:127.0.0.1:<address>.onion:80,socksport=9050`
  - Or install `curl` in the tor container and use `curl --socks5-hostname 127.0.0.1:9050`
- WordPress container has `curl`
- Test onion service path (internal): `docker exec onionpress-tor wget -q -O /dev/null http://wordpress:80/`

## The OnionHeaven Overview (one view of both roles)
- **`onionheaven.overview(app)` says which hub keeps this site reachable
  and whom this install keeps reachable**: `client` (the choice, the hub,
  `hub_known_as` from `KNOWN_HOSTS`: the Internet Archive's OnionHeaven
  and OnionHome; the record's `registered_at` / `last_heartbeat_at`; the
  served and health-check addresses; `left_on_hubs` with `key_here`, whether
  the key that could ask for the copy's deletion is still here), `hub`
  (`active` = `ONIONHEAVEN_ACCEPT_REGISTRATIONS=yes` or the canonical
  address; counts; `sites` from the registry capped at `HUB_SITES_LIMIT`;
  the takeover settings) and `known_hosts`. `status_block()` is status.json's
  `onionheaven` object: the flat keys onionpress-status.php always read (the
  platform's `server_active`: the Mac's stays `bool(self.is_onionheaven)`,
  `tests/test_linux_hub_mode.py` pins the line) plus the nested view.
- **Who carries it**: the Mac daemon computes it in `check_status` (the
  registry is asked only on a hub) and publishes it in daemon-state.json
  (`"onionheaven"`) and status.json; Linux's `write_status` merges `op_py
  onionheaven overview --json` into the block; `onionpress onionheaven
  overview [--json]` prints it. `_mark_registered` (Mac) and
  `_record_registered` (Linux) write `registered_at` (kept across beats)
  and `last_heartbeat_at` (every beat). A UI reads the overview; it never
  asks docker itself.
- **Registry rows are checked before they are shown** (`hub_sites`: a v3
  address, a status from a fixed set, short printable timestamps and
  versions): the registry is data anything on the hub volume can write.
  `tests/test_onionheaven_overview.py`.

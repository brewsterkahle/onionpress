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
- macOS menubar app (py2app built from `src/menubar.py`)
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
  - `src/onionpress/` — shared package: backup, key_manager, setup_window, onion_proxy, onion_auth, onionheaven, updater, install_native_messaging, native_messaging_host, power, health, docker, tor, colima, platform, config, containers, ui_helpers, settings_ui, browser, log_rotation, analytics_sharing, onionnames_*
  - `setup.py` — py2app config. Adding a new submodule to `onionpress` just means one new line in the `includes` list — no build-script change needed. Build scripts only copy the whole `onionpress` package into site-packages.
- **Release via GitHub releases only** (`gh release create`). Do NOT upload to Internet Archive.
- **Cut releases with `build/release.sh`** — it builds BOTH artifacts and uploads both to one release, so the Linux `.deb` can't be forgotten. (It was: v2.4.101–v2.4.106 shipped only the `.dmg`, which 404'd the README's `releases/latest/download/onionpress.deb` link — that link resolves to the *Latest* release's asset of that name.) Bump + commit first, then run it. Cross-platform rule it enforces: the `.dmg` can only be built on macOS, so on **macOS** it builds `.dmg`+`.deb` and creates/updates the release with both; on **Linux** it builds only the `.deb` and *refuses to create* a release (a `.dmg`-less "Latest" would break the Mac link) — it only attaches the `.deb` to an existing release that already carries the matching `.dmg`. Always run the Mac side first.
- **Version bumping**: run `build/bump-version.sh X.Y.Z` — it updates all version locations automatically. The 2 canonical sources are `src/menubar.py` (`self.version`) and `app/Info.plist` (`CFBundleShortVersionString`). Derived locations (`src/onionpress/__init__.py`, `setup.py` which reads menubar.py dynamically, MenubarApp plist) are updated by the bump script or at build time. The quit log in menubar.py uses `self.version` dynamically. Docker containers get the version via `ONIONPRESS_VERSION` env var from the launcher script (which reads Info.plist).
- **py2app vs setuptools 81+ incompatibility** — setuptools 81 (released 2026-02-06) removed `dry_run` from `distutils.spawn()`, which py2app 0.28.9 still uses. The build script (`build/build-dmg-simple.sh`) handles this automatically: it tries the build first, and falls back to `setuptools<81` only if py2app fails. Once py2app ships a fix, the fallback stops being needed. Track upstream: https://github.com/ronaldoussoren/py2app/issues/557

## Local Build Path
- **`docs/BUILDING.md` is the map** — every component, its command, its host-OS
  requirement and its network dependencies. `make doctor` reports missing tools.
- `build/build-images.sh` builds the tor/wordpress images locally (`make images`).
  `build/dev-up.sh` runs the stack on them (`make dev-up`).
- **Every `docker pull` is gated on `using_local_images()`** — implemented three
  times (app/MacOS/onionpress, linux/onionpress, onionpress.containers). Change
  all three together; a test enforces they agree.
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
  `state/keystore/hss/<name>/`) until 2026-09-25; both launchers carry an
  identical `migrate_key_volume()` (test-enforced) that copies an old volume
  to the new name once, before first-run detection, and refuses to start if
  the copy fails — the first-run check keys off the new volume's existence,
  so skipping the migration would mint a new address. Keep the old name in
  the wipe lists (`.import-key-pending`, restore) so a replaced identity
  cannot be migrated back.
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
  ImageMagick), `build/build-extension.sh` (builds Firefox from
  `extension-firefox/`, NOT the stale `extension/manifest.firefox.json`).
- **`.github/workflows/docker-publish.yml` needs nothing outside GitHub**: amd64
  on `ubuntu-latest`, arm64 on `ubuntu-24.04-arm` (no self-hosted runner), and
  `IMAGE_NAMESPACE` defaults to the repo owner so a fork's "Run workflow"
  publishes `ghcr.io/<you>/…`. The stress worker gets its base via the
  `TOR_IMAGE` build-arg. `tests/test_publish_workflow.py` enforces all of it.
  On `development`, `IMAGE_PREFIX: dev-` names the images `dev-onionpress-*`;
  reset it to `""` when merging into `main` (the test fails the PR otherwise).

## Container Image Pins
- **`build/image-pins.env` is the single source of truth** for the GHCR image
  digests. Five files embed the literals (docker-compose.yml, app/MacOS/onionpress,
  linux/onionpress, src/onionpress/containers.py, src/onionpress/launcher_ops.py).
- **Never hand-edit a digest.** Run `build/refresh-image-digests.sh` (or
  `--propagate` to re-apply the pins file without a Docker daemon).
  `tests/test_image_pins.py` fails the build on drift — this exists because
  v2.4.110 refreshed only docker-compose.yml and shipped two different Tor
  builds inside one install.
- Pins must be **multi-arch index digests** (`docker buildx imagetools inspect`),
  never a single-platform manifest digest.
- Exporting `ONIONPRESS_TOR_IMAGE` / `ONIONPRESS_WORDPRESS_IMAGE` repoints the
  whole stack at locally built images. See `docs/BUILDING.md`.

## Onion Service Recovery (issue #266)
- **The app's dual probe is the only measure of real reachability.** When it fails
  continuously for 10 min with Tor internally healthy (`RepublishTrigger` in
  `src/onionpress/health.py`, used by `src/menubar.py` and `linux/onionpress-service.py`),
  the app writes a request file inside the tor container
  (`launcher_ops.request_watchdog_republish`) and `tor-watchdog.py` answers with
  DEL_ONION + ADD_ONION — fresh intro points, immediate descriptor upload. Without it,
  C Tor keeps a dead descriptor on the HSDirs until its next scheduled upload (60–120 min).
- Rate limits: one request per 15 min on the host, one forced republish per 15 min in
  the watchdog, never within 5 min of an HS_DESC UPLOADED event. Takeover codes never
  trigger it (the heartbeat's /online is the reclaim path).
- `REPUBLISH_ON_UNREACHABLE=no` in `~/.onionpress/config` makes it log-only.
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
- **Both wake handlers reset the republish window** (`_republish_trigger.reset()` on
  Mac, `monitor.republish.reset()` on Linux, test-enforced): the trigger uses wall
  clock, so a failure seen just before sleep would otherwise fire DEL+ADD on the
  first probe after wake, on top of the watchdog's own wake ADD.
- The request path is a literal in both `launcher_ops.py` and `tor-watchdog.py`; a test
  fails if they drift. The watchdog ships in the tor image, so this needs an image
  publish + `build/refresh-image-digests.sh` to reach users.

## Security
- **Database passwords are randomly generated per install** — never use defaults or hardcoded passwords. The `ensure_secrets` function generates unique passwords with `openssl rand` on first run, saved to `~/.onionpress/secrets`.
- Do not commit or log database passwords.

## Settings Page Config Writes Are Allowlisted
- The WordPress Settings page asks for config changes through
  `config-updates.json` in the onionpress-data volume, which anything running
  in WordPress can write. Both hosts apply it only through
  `config.apply_settings_updates` (Mac `poll_config_updates`; Linux
  `apply_config_updates` → `onionpress.cli apply-settings-updates`): keys in
  `SETTINGS_PAGE_KEYS`, each value checked (yes/no enums, integer ranges,
  the token's base64 charset, no control characters). Everything else is
  dropped and logged by key name only.
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
- `tests/test_settings_owner_only.py` enforces it, including a census: only
  the owner-gated settings and status plugins name host action files in
  `/var/lib/onionpress/`.

## Colima VM Sandboxing
- The VM has two narrow mounts: `~/.onionpress/shared:w` and `~/OnionPress:w`
- This limits blast radius if a container is compromised — attacker can only see vanity keys and user's published content (which is public anyway)
- `~/.onionpress/` stays as-is for app state (no spaces in path — required by Colima/Lima/Docker socket paths)
- `~/OnionPress/` holds user-visible backups and Creations
- All other container data uses Docker named volumes (which live inside the VM)
- **Do not move app state to `~/Library/Application Support/`** — the space in the path breaks Colima/Docker socket paths (104-char Unix socket limit + space handling issues)
- **Diffdisk cap is 20 GiB on new installs** (issue #230) via `--disk` to the first `colima start` in `app/MacOS/launcher.sh` and `src/onionpress/colima.py`. The cap is set at VM creation and is immutable — existing installs keep their original 100 GiB cap. Dangling Docker images auto-prune after each successful image pull (`update_docker_images()` in `src/menubar.py` and the bash launchers), keeping real usage well under 5 GiB in practice. The cap is configurable via `VM_DISK=N` in `~/.onionpress/config` *before* first launch; when the OnionHeaven hub vanity key is present at first start, the cap auto-bumps to 100 GiB for takeover-container headroom (hub is the edge case; normal nodes never need more than 20).
- **Do not add additional `--mount` flags without considering security implications**

## OnionHeaven Opt-In
- **`REGISTER_WITH_ONIONHEAVEN` is tri-state and opt-in**: `yes` registers,
  `no` never registers and never asks, unset/empty = undecided → no
  registration, and the UI prompts at startup once the site is online
  (Mac: `prompt_enable_onionheaven` in `src/menubar.py`; Linux tray:
  `_maybe_prompt_onionheaven`). "Not Now" leaves it unset (asked again next
  launch); "Don't Ask Again" writes `no`. Only an explicit `yes` may ever
  register — the heartbeat ships the onion private key to the hub, so a
  missing key or typo must never opt a user in. `onionheaven.registration_choice()`
  is the one normalizer; all three launch paths and the Linux heartbeat daemon
  use `== "yes"`, never `!= "no"`. `tests/test_onionheaven_opt_in.py` enforces it.
- Installs whose config carries `REGISTER_WITH_ONIONHEAVEN=yes` keep
  registering: Linux (`install.sh` wrote it) and Macs whose config was seeded
  from `config-template.txt`. Mac configs without the line (older installs,
  or ones the launcher's inline fallback created; Ivar's live install is one)
  used to register via the `yes` default and are now treated as undecided,
  so they get the startup prompt at next launch. **Decided 2026-10-11
  (Ivar): no heartbeat without an explicit yes, and no automatic migration.**
  Such an install goes silent at the upgrade, and the hub takes its address
  over within ~10 minutes of the last heartbeat, serving the Wayback copy
  until the user clicks Enable (one click re-registers and reclaims it).
  Seen on the live install: prompt 1 s after "System fully operational",
  takeover header 12 minutes later. Do not "fix" this by writing `yes`
  for existing installs; it was rejected as an opt-in without consent.
- Linux CLI: `onionpress onionheaven enable|disable|status`. The heartbeat
  daemon re-reads the config every cycle, so enabling needs no restart on
  either platform.

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
  (`regenerate_derivatives_pending()` in both launchers, test-enforced
  identical, and the CLI `start`/`restore` paths) then runs
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

## Backup Encryption (backup format 2)
- New backups are an ordinary, unencrypted `.zip` holding `README.txt` and
  `onionpress-backup.enc`: scrypt (N=2^17, r=8, p=1) → SHAKE256 keystream +
  HMAC-SHA256 per 1 MiB record, wrapped around the same zip of the same
  members. Python standard library only — no new dependency in the py2app
  bundle or on Linux. The layout is in `src/onionpress/backup_crypto.py`'s
  docstring, pinned by a known-answer vector and an independent decoder in
  `tests/test_backup_manager.py`; changing the bytes means a new format.
- **Format 1 (ZipCrypto zips from `zip -P`, everything through 2.5.2) must
  restore forever.** `backup.extract_archive` picks the decoder by whether
  the zip holds `onionpress-backup.enc`. Never write `zip -P` again; the
  tests fail if a launcher does.
- `metadata.json` carries `backup_format: 2` (absent = 1); a higher number,
  or unknown version/kdf/suite bytes in the container header, is refused as
  "made by a newer version of OnionPress".
- The bash launchers go through `onionpress.cli pack-backup` / `extract-backup`
  (password on stdin); Linux's inline create_backup no longer zips itself.
- OnionPress 2.5.2 and older cannot restore a format-2 backup (they report it
  as not a valid OnionPress backup); the README inside the zip says so.

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

## Multi-User Support (v2.4.11+)
- Multiple macOS users can run OnionPress simultaneously from the same `/Applications/OnionPress.app`
- Each user gets their own `~/.onionpress/` data dir, Colima VM, and Docker containers
- **Port offsets**: second user auto-detects port 8080 is taken and offsets all ports by +10000 (18080/19050/19077), third user by +20000, etc. Max ~5 users.
- **Detection uses socket bind test**, not `lsof` — `lsof` only sees the current user's processes and cannot detect ports bound by other users
- **Port detection must happen in the MenubarApp's `__init__`** (Python socket bind), not in the shell scripts — the MenubarApp launches first and needs the correct ports before the `onionpress` script runs
- **Module-level constants in `onionpress.onion_proxy`** (`PROXY_PORT`, `PHP_PROXY_PORT`) are set at import time. The MenubarApp must update these globals after detecting the offset: `onion_proxy.PROXY_PORT = self.proxy_port`
- The `onionpress` shell script also has detection as a fallback (for standalone use), but respects pre-set `ONIONPRESS_PORT_OFFSET` env var from the MenubarApp
- **`LSMultipleInstancesProhibited` must NOT be in any Info.plist** — macOS enforces it across ALL users sharing the same app bundle, not just per-user
- **`pgrep` in the launcher must use `-u $(whoami)`** to restrict to the current user's processes
- **PID lock file** (`~/.onionpress/onionpress.pid`) prevents the same user from double-launching; cleaned up via `trap` on EXIT/INT/TERM/HUP
- **Container-internal ports are NOT offset** — Docker networking (`onionpress-tor:9050`, `wordpress:80`) is isolated per-VM. Only host-side port mappings change.
- `OnionPress.app/` is gitignored — it is assembled at build time from `app/` source. Never commit build output.

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

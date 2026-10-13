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
  config. Changing the create-backup filename in `src/menubar.py` or
  `linux/onionpress` means changing that pattern; `tests/test_backup_download.py`
  ties them together.

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
  before every start's first `compose up` (both launchers'
  `block_wordpress_egress`, `ContainerManager.start_core`): Colima VM via
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

## Containers Drop Every Capability
- Every compose service (both compose files), every takeover worker
  (`_spawn_worker` in onionheaven_common.py, the unused `start_farm_worker`
  and the Mac launcher's `_start_takeover_worker`) and every one-shot
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
  autoheal, autoheal-proxy, cloudflared nothing. Helpers: reads of key
  files, mkp224o and key seeds DAC_OVERRIDE; migrate_key_volume and the
  Linux tor-state seeds CHOWN DAC_OVERRIDE FOWNER; the mkp224o probe
  nothing. `docker exec` runs with the container's set, so a root exec
  into tor or wordpress that touches debian-tor's or www-data's files
  relies on that DAC_OVERRIDE. Change a set only after the same live test.
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

## Colima VM Sandboxing
- The VM has two narrow mounts: `~/.onionpress/shared:w` and `~/OnionPress:w`
- This limits blast radius if a container is compromised — attacker can only see vanity keys and user's published content (which is public anyway)
- `~/.onionpress/` stays as-is for app state (no spaces in path — required by Colima/Lima/Docker socket paths)
- `~/OnionPress/` holds user-visible backups and Creations
- All other container data uses Docker named volumes (which live inside the VM)
- **Do not move app state to `~/Library/Application Support/`** — the space in the path breaks Colima/Docker socket paths (104-char Unix socket limit + space handling issues)
- **Diffdisk cap is 20 GiB on new installs** (issue #230) via `--disk` to the first `colima start` in `app/MacOS/launcher.sh` and `src/onionpress/colima.py`. The cap is set at VM creation and is immutable — existing installs keep their original 100 GiB cap. Dangling Docker images auto-prune after each successful image pull (`update_docker_images()` in `src/menubar.py` and the bash launchers), keeping real usage well under 5 GiB in practice. The cap is configurable via `VM_DISK=N` in `~/.onionpress/config` *before* first launch; when the OnionHeaven hub vanity key is present at first start, the cap auto-bumps to 100 GiB for takeover-container headroom (hub is the edge case; normal nodes never need more than 20).
- **Do not add additional `--mount` flags without considering security implications**

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
  mount the volume before `compose up` (`retire_old_onionheaven_containers`,
  identical in both launchers; `ContainerManager.start_core`), the farm
  stops in parallel (`stop -t 1`), and the heartbeat retires old workers the
  registry does not list too.
- Fails closed only if the first move fails with a real key (non-empty key
  file in `keys/<v3>.onion/`) at stake; a start without keys at stake marks
  the volume (else the next start would shelve live data). Never after the
  marker: WordPress can write the old location.
  `tests/test_onionheaven_data_move_hardening.py`.
- Backups hold hub data when the volume has a key dir or a registry row
  (`backup.hub_data_state`, not the long-dead `master-key.json`): registry
  via `sqlite3 .backup` + `keys/`, nothing else (`stage_hub_data`; Linux via
  `onionpress.cli stage-hub-data`). Restores go through `restore_hub_data`
  (SQLite `.restore`, never `docker cp` over the live db).
- **Never in a backup that goes through WordPress.** The Settings page's
  backup is handed to the WordPress container for download, and its
  password is the admin password WordPress saw in plain text; its restore
  takes an upload and a password from there too. So the page's backup is
  `create_backup(include_hub_data=False)` (Linux `create_backup
  --without-hub-data`): hub data left out and logged, `metadata.json` gets
  `excludes_onionheaven_data: true` + `excludes_onionheaven_data_reason:
  "settings-page"`, the page shows `HUB_DATA_LEFT_OUT_NOTE`. Every other
  `create_backup` caller passes `include_hub_data=True` (owner's disk).
  The page's restore (`restore_from_backup(through_wordpress=True)`, Linux
  `restore_backup --from-settings-page`) refuses, before touching anything,
  a backup with any entry in `onionheaven/keys/`
  (`settings_page_restore_refusal`; refused whole, since a hub restored
  without its registrants' keys looks restored) and never writes the volume
  (`hub_volumes=False`: a registry alone, as every Linux backup through
  2.5.2 carries, is left out). `tests/test_backup_hub_data_off_wordpress.py`
  keeps a census of the hand-offs to WordPress and the callers.
- Compose and tor image ship together (see docs/DOCKER-SOCKET.md, "The
  OnionHeaven hub data"). **Never downgrade a hub across this release**;
  on the hub follow the procedure there (count, tar the old dir, stop
  fully, update, check the log line and the counts).

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

## Request Signatures Are Checked by libsodium
- `onion_auth.verify` is PyNaCl's `VerifyKey(pub).verify` (libsodium's
  crypto_sign_open): S < L, no small-order or non-canonical key or R. The
  pure-Python verifier it replaced took R = identity, S = 0 as a signature
  of anything for a small-order key, so anyone could sign for
  `aeaaaa…hmjqd.onion`. Signing is still the pure-Python expanded-key
  signer (libsodium and pyca/cryptography sign only from a seed; an
  mkp224o key has none); changing that is an open decision for Ivar.
- The only verifier is the hub's web-server.py: `_verify_signature`
  (/online, /offline, /unregister), `_signed_peer_claim`, the OnionHome
  name and log endpoints. `tests/test_onion_auth.py` keeps that census and
  keeps the three copies of onion_auth.py (src, tor image, tests/stress)
  identical.
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
- The Settings page's backups keep `onionhome/` (no secret in it, and an
  app restore of a backup without it would serve an empty, claimable
  registry). The page's restore refuses a backup carrying
  `onionhome/onionnames.db` (`settings_page_restore_refusal`) and never
  writes the volume (`hub_volumes=False`): that upload is WordPress's to
  make, and the registry left WordPress's reach so code there could not
  repoint names. Restore OnionHome from the app or `onionpress restore`.
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

## OnionHeaven Opt-In
- **`REGISTER_WITH_ONIONHEAVEN` is tri-state and opt-in**: `yes` registers,
  `no` never registers and never asks (and unregisters, below), unset/empty
  = undecided → no registration, and the UI prompts at startup once the site is online
  (Mac: `prompt_enable_onionheaven` in `src/menubar.py`; Linux tray:
  `_maybe_prompt_onionheaven`). "Not Now" leaves it unset (asked again next
  launch); "Don't Ask Again" writes `no`. Only an explicit `yes` may ever
  register — the heartbeat ships the onion private key to the hub, so a
  missing key or typo must never opt a user in. `onionheaven.registration_choice()`
  is the Mac normalizer, the daemon's `_registration_choice()` the Linux one
  (exactly yes/no); every gate is `== "yes"`, never `!= "no"`.
  `tests/test_onionheaven_opt_in.py` enforces it.
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
- Linux CLI: `onionpress onionheaven enable|disable|status`. Both platforms
  re-read the choice before every request that carries the key (every
  `/online` does: the hub refuses one without `arti_key_pem`), so enabling
  and disabling need no restart: the Mac heartbeat loop ends at its next
  beat and `_send_heartbeat` refuses under `_HUB_LOCK`; the Linux daemon
  stops in the next cycle. Nothing goes out unless `yes`: no `/online`, no
  `/offline` (it makes the hub take over at once), on sleep, stop or quit.
- **An explicit `no` takes the key back**: when the registration record
  (`~/.onionpress/onionheaven-registration.json`, "registered": the hub may
  hold the key) says so, one signed `/unregister` for the whole address (no
  healthcheck_address: every row, so the hub deletes the key dir) goes to
  the hub the record names, then the record is cleared. Mac:
  `maybe_unregister_after_opt_out` from check_status (also at startup with
  `no`), ordered after any heartbeat in flight by `_HUB_LOCK`; Linux:
  `_maybe_unregister_after_opt_out` in the daemon, which used to send
  `/offline` on disable (a takeover of a running site, key kept). Retried
  every 15 min while the hub does not answer; "Entry not found" counts as
  done. Unset sends nothing, not even this (the 2026-10-11 decision above).
  `tests/test_onionheaven_opt_out.py`.

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

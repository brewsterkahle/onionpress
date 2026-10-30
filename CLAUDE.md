# OnionPress Project Memory

## Meta
- **This file holds rules a change must respect**, each with the test that
  holds it. Not history: what broke, in which release and why goes in the
  commit message or PR. `docs/DESIGN-NOTES.md` has the story behind every
  rule here, under the same heading (a 2026-10-26 snapshot, not maintained).
- A new rule: a bullet or two under the section it belongs to, ending in its
  test. If a rule needs a paragraph of explanation, put that in docs/.
- Run `/mission` to evaluate a change against OnionPress's three author
  commitments: safety, ease of publishing, and content durability.

## Naming Rules (IMPORTANT)
- **OnionPress** (one word, capital O and P), never "Onion.Press",
  "onion.press" or "onion-press". Data dir `~/.onionpress/`. Repo
  `brewsterkahle/onionpress`.
- **"onion service"**, never "hidden service", in all user-facing text
  (external identifiers like `/var/lib/tor/hidden_service/` excepted).

## Key Architecture
- macOS menubar app (py2app, `src/menubar.py`) is a view of the daemon's
  state file; `onionpress daemon` (`src/onionpress/daemon.py`, a launchd
  agent) runs every loop ("The Mac Daemon").
- Launcher `app/MacOS/onionpress` (Mac) and `linux/onionpress` (Linux);
  Docker containers (tor, wordpress, mariadb, onionheaven…) run inside a
  Colima VM on macOS.
- Logs in `~/.onionpress/` (`logs/`, `onionpress.log`, `launcher.log`);
  user-visible content in `~/OnionPress/` (backups, Creations).

## Repo Layout
- `src/menubar.py` — py2app entry point, the only flat module.
- `src/onionpress/` — the shared package (everything else). A new submodule
  needs one line in `setup.py`'s `includes`.
- `app/` — the .app bundle source (`Info.plist`, `MacOS/`, `Resources/`:
  docker configs, plugins, icons, templates). `linux/` — the Linux side.
- `OnionPress.app/` — **gitignored build output**; never commit it.
- py2app because Macs ship no usable Python; users must never need Xcode
  tools.

## Build & Release Process
- **After editing any `src/` file the app uses, rebuild the MenubarApp**
  (`build/rebuild-menubar.sh`): the bundle holds compiled `.pyc`s, and the
  daemon and every `onionpress.cli` command run the bundle's package.
  Hot-patching: only `MenubarApp/Contents/Resources/menubar.py` (and its
  pyc) is the live entry point.
- **Releases: `build/release.sh` only, on `brewsterkahle/onionpress`, never
  a fork.** It passes `-R`, refuses unless HEAD is upstream `main`'s tip,
  builds both `.dmg` and `.deb` (Mac side first; Linux only attaches a
  `.deb` to a release that has the `.dmg`), writes `SHA256SUMS`, publishes
  from draft only after reading it back. Hand-run `gh release` needs
  `-R brewsterkahle/onionpress`. No Internet Archive uploads.
  `tests/test_release_sha256sums.py`.
- **Every release adds its notes to `RELEASE_HISTORY.md`** before it is cut:
  a `## X.Y.Z — YYYY-MM-DD` section at the top, saying what changed for the
  people who run OnionPress (what to do, what to know; hub/OnionHome steps
  under their own heading). Write it in the bump PR. `release.sh` puts the
  section above GitHub's generated PR list and refuses a version without
  one; the tree's version must have one. `TestReleaseHistory` in
  `tests/test_release_sha256sums.py`.
- `onionpress update` (Linux) installs only a release `.deb` matching its
  `SHA256SUMS` line, of the tag's version, newer than installed; the
  Settings page's update only checks (anything in WordPress can request
  it). `tests/test_linux_release_update.py`.
- The Mac update mounts a DMG only if it matches the release's `SHA256SUMS`.
  `DmgChecksumTest` in `tests/test_updater_staged_swap.py`.
- Update-check curl passes `--cacert /etc/ssl/cert.pem` only where it
  exists; `--json` always exits 0 and reports `error`, which UIs show as
  failure. `tests/test_update_check_ca_bundle.py`.
- **What goes into `/opt/onionpress` is one list: `linux/install-tree.sh`**
  (the .deb, `install.sh`, `update --from-checkout`). It refuses links and
  special files; `--from-checkout` takes only a clean, non-world-writable
  checkout owned by the user or root, fast-forward only.
  `tests/test_linux_install_tree.py`.
- `linux/install.sh`: never `rm -rf` a folder named by the environment
  (`tests/test_install_sh_clone_cleanup.py`); installs the newest release
  tag, not main's HEAD; body is `main() {…}; main "$@"` so a cut-off
  `curl | bash` runs nothing; Docker's apt key is checked by fingerprint.
  `tests/test_install_sh_release.py`.
- The .deb postinst restarts the units and tray on upgrade and never fails
  the install (`tests/test_deb_upgrade_restarts.py`); a first install sets
  up rootless Docker, judging success by `docker info`, using
  `user_systemctl` (not `--machine`) and `--no-block`
  (`tests/test_deb_first_install_docker.py`).
- The rootless setup tool is looked for on PATH, then in docker.io's
  contrib, then in the package's own copy of Docker's two scripts
  (`linux/docker-rootless/`, pinned by commit and sha256, Apache-2.0 in
  `linux/copyright`; Ubuntu's docker.io ships none). A RootlessKit without
  `--detach-netns` (Ubuntu 24.04's 1.1.1) gets the user's
  `docker.service.d/onionpress-rootlesskit.conf` drop-in turning it off. A
  skipped or failed attempt says why at the terminal and in
  `~/.onionpress/docker-rootless-setup.log`. **No Compose v1 in
  `Depends`**: every Compose alternative is v2 (`docker-compose-plugin |
  docker-compose-v2 | docker-compose (>= 2)`), and the launcher's start
  checks `docker compose version`. **The .deb Recommends nothing**:
  torbrowser-launcher is a Suggests, since as a Recommends it brought the
  system `tor` on to 9050 (Ubuntu installs Recommends by default) and the
  site moved to 18080. `tests/test_docker_rootless_vendored.py`,
  `tests/test_deb_first_install_docker.py`.
- **Docker Engine 20.10+ and Compose 2.22+ are the floors**
  (`docker_versions.py`, `onionpress check-docker`): the Linux launcher's
  `start` refuses older ones with what was found and how to upgrade, the
  postinst warns at install time (never fails). Moving a floor means new
  evidence in that module's docstring. `tests/test_docker_versions.py`.
- **Every Mac build input is pinned by hash**: downloads through
  `fetch_checked` against the `*_SHA256_*` constants; Python from the
  `build/*-macos.lock` files with `--require-hashes`
  (`build/refresh-python-locks.sh`); mkp224o at `MKP224O_COMMIT`. Bumping
  a version means its new hash. `tests/test_dmg_build_pins.py`.
- **A release's Mac app is universal** (`ONIONPRESS_RELEASE_BUILD=1`
  refuses single-arch Python; every Mach-O must have arm64 + x86_64). The
  `.deb`'s files are root's, with no AppleDouble `._` members (run
  `build/check-deb.sh` on any package before handing it out).
  `tests/test_build_release_hygiene.py`.
- CI rebuilds, lintian-checks and attests the `.deb` on every published
  release and rewrites its `SHA256SUMS` line; write permission only in the
  release job. `tests/test_linux_package_workflow.py`.
- **Every workflow action is pinned to a commit SHA** with a `# vX.Y.Z`
  comment; dependabot bumps both. `tests/test_build_release_hygiene.py`.
- **Version bump: `build/bump-version.sh X.Y.Z`.** Canonical sources:
  `src/menubar.py` (`self.version`) and `app/Info.plist`.
- The build lock keeps `setuptools<81` (py2app #557) until a build has
  tried 81+; see the `.in` file's comment before dropping it.

## Local Build Path
- **`docs/BUILDING.md` is the map**; `make doctor` reports missing tools.
  `make images` / `make dev-up` build and run local images.
- Every `docker pull` is gated on `onionpress.containers.using_local_images`
  (the launchers delegate to it). `tests/test_local_image_mode.py`.
- The tor image is built on Onimages' C Tor image; its Dockerfile must
  `USER root` and `CMD []`, and the entrypoint's per-start
  `chown -R /var/lib/tor` stays. **C Tor only**: `TOR_IMPL` must not come
  back (the control port is load-bearing).
- **The key volume is every install's identity**: `onionpress-onion-keys`,
  PEM at `<name>/ks_hs_id.ed25519_expanded_private`.
  `migrate_key_volume` (from the old `onionpress-arti-state`) runs before
  anything starts tor and before first-run detection, fails loudly rather
  than copy into an empty volume, and is resumable via
  `.key-volume-migration-pending`. Every identity wipe drops that marker
  and keeps the old volume name in its wipe list. `cli start` /
  `ContainerManager.start_tor` refuse while a migration is due. A failed
  first-run key seed removes its volume and stops.
  `tests/test_install_invariants.py`.
- **All Dockerfile inputs are pinned** as `ARG` defaults (images by
  multi-arch index digest, mkp224o by commit, wp-cli by sha256);
  `MKP224O_VERSION` matches `build/build-dmg-simple.sh`. Re-pin the
  Onimages base right before every image publish (its registry drops old
  digests). `build/base-image-digest.sh` resolves tags.
- `make build` runs `build-dmg-simple.sh` (`build-dmg.sh` is deleted and
  stays deleted). Firefox's extension = `extension/` overlaid with
  `extension-firefox/`; keep only differing files there.
- `docker-publish.yml` needs nothing outside GitHub and publishes to the
  repo owner's namespace. On `development` `IMAGE_PREFIX: dev-`; reset to
  `""` when merging to main. `tests/test_publish_workflow.py`.

## Container Image Pins
- **`build/image-pins.env` is the single source of truth** for every image
  the stack runs. **Never hand-edit a digest**: `build/refresh-image-digests.sh`
  (`--propagate` without a daemon; resolves from the registry, `--local`
  only on request). Pins are multi-arch index digests; **mariadb moves
  forward only**. `tests/test_image_pins.py`.
- Starts pull `--policy missing` only (once per launch, in the launcher's
  `update_images()`); unconditional pulls only on opt-in refresh paths.
  `tests/test_launch_pulls_once.py`, `tests/test_image_pins.py`.
- **A release builds no images**: it only points `:latest`/`:<version>` at
  the pinned digests. `tests/test_publish_workflow.py`.
- `ONIONPRESS_TOR_IMAGE` / `ONIONPRESS_WORDPRESS_IMAGE` repoint the stack at
  local images.
- **Image pins move with the launcher**; changes to files in the tor or
  wordpress image reach users only after an image publish + re-pin.

## A Damaged Key Never Becomes a New Address (issue #311)
- The delivered PEM is the identity; C Tor's files are a copy re-derived at
  every start unless they match exactly (old files set aside, never
  deleted). No usable PEM and no secret key: refuse to start.
- The watchdog mints (`ADD_ONION NEW:BEST`) only with no trace of an
  address at all, keeps a minted key in memory, saves it O_EXCL.
  `tests/test_onion_key_never_reminted.py`.

## Onion Service Recovery (issue #266)
- The app's dual probe (`health.py`, fetching `/.onionpress-health`) is the
  only measure of reachability. `RepublishTrigger` (rate-based: ≥ 50 % of
  10 min failing; 5 min for `desc-missing`/`desc-stale`) asks the watchdog
  for DEL_ONION + ADD_ONION via `launcher_ops.request_watchdog_republish`.
  Mac: the daemon's `check_status`; Linux: `onionpress-heartbeat.py`
  (writes `reachability.json`, which `write_status` prefers).
- Rate limits: 15 min host, 15 min watchdog, never within 5 min of an
  upload. `REPUBLISH_ON_UNREACHABLE=no` makes it log-only. Both wake
  handlers reset the window (test-enforced).
- Locked together by tests: `PROBE_PATH`/`PROBE_BODY_MARKER` and the
  image's `onionpress-health.conf`; the request path literal in
  `launcher_ops.py` and `tor-watchdog.py`. Every Python entry point in
  `linux/` is installed by both `install.sh` and `build-linux.sh`.
  `tests/test_linux_recovery_loop.py`.
- The watchdog is started only through entrypoint.sh's `supervise_watchdog`
  (a function, not a `*tor-watchdog*` script: hosts signal
  `pgrep -f '[t]or-watchdog'`). `tests/test_tor_watchdog_supervised.py`.

## License Declarations
- **AGPL-3.0-or-later** (that SPDX form), "Copyright (C) 2026 Internet
  Archive and OnionPress contributors": LICENSE, README, Info.plist,
  setup.py, `linux/copyright`. Entry scripts carry the GNU short notice;
  plugin headers and the theme's style.css carry `License:` + `License URI:`.
- The DMG's `THIRD-PARTY-NOTICES.txt` covers bundled binaries and the
  bundle's Python distributions (`build/python-notices.py`); a missing text
  fails the build. A new dependency whose wheel has no license text needs a
  `LICENSE_URLS` row. `tests/test_license_declarations.py`.

## Test suite
- `make test-unit` (`unittest discover`, uv, Python 3.14, PyNaCl). **No
  pytest**: every test file holds `unittest.TestCase` classes.
- **On a Mac running OnionPress, set
  `DOCKER_HOST=unix:///var/empty/no-docker-for-tests.sock`** so
  docker-backed tests skip instead of touching the live site.
- `make coverage` enforces floors (`build/coverage-floors.sh`); raise a
  floor when tests catch up, never lower one to pass.
  `tests/test_coverage_gate.py`.
- **The launchers' `main` is run, not just read**: `tests/launcher_harness.py`
  runs a copy of each launcher with docker, the CLI and named helpers
  stubbed. A launcher verb's options, exit status and the CLI argv it
  produces go in `tests/test_launcher_dispatch_{mac,linux}.py`; the tray
  runs only verbs the Linux `main` has (same file).
- **A new `onionpress` subcommand needs a row in `tests/test_cli_dispatch.py`**
  (the census fails otherwise; grouping commands go in `PARENTS`), and its
  handler a case in `tests/test_cli_commands.py` / `test_cli_edges.py`.
- The e2e suite's platforms are OS families, `macos` and `linux`
  (`e2e/harness/platforms.py`); the distro is a node fact. Linux-host gates
  use `HostFacts.debian_based` (`ID` or `ID_LIKE`), never `distro ==
  "debian"`; a `linux-vm` guest is one of `LINUX_GUESTS` (Debian 13 the
  default). `tests/test_e2e_harness.py`.

## Security
- Database passwords are random per install (`ensure_secrets`,
  `~/.onionpress/secrets`); never defaults, never committed or logged.
- **Crypto comes from established libraries (libsodium via PyNaCl), never
  constructions of our own.**
- Secrets never go on argv: stdin or the environment.
  `tests/test_secrets_off_argv.py`.

## WordPress Branch Moves With the Image
- `WORDPRESS_BRANCH` in the wordpress Dockerfile is the branch the audit
  moves sites up to; never past it, never down. Raise it only after testing
  mu-plugins and pinned plugins on it (docs/BUILDING.md).
  `tests/test_wordpress_branch_upgrade.py`.

## A Core Package Is Checked Before It Is Installed (issue #395)
- Core packages come only from `https://downloads.wordpress.org` and every
  zip member is held to WordPress.org's md5 list before install; a refused
  package is removed. `tests/test_core_update_verified.py`.

## Settings Page Config Writes Are Allowlisted
- Anything in WordPress can write the request files in
  `/var/lib/onionpress/`. Config changes go only through
  `config.apply_settings_updates` (`SETTINGS_PAGE_KEYS`, every value
  checked; the rest dropped, logged by key name only).
  `tests/test_settings_page_updates.py`.
- **Never add to `SETTINGS_PAGE_KEYS`** image overrides,
  `ONIONHEAVEN_ADDRESS`, `ONIONHEAVEN_ACCEPT_REGISTRATIONS`,
  `ADDRESS_PREFIX`, ports, paths or secrets other than
  `CLOUDFLARE_TUNNEL_TOKEN` (write-only field). Log changes via
  `describe_change`/`loggable_value`. Every page key reaches a reader
  (`tests/test_settings_page_keys_are_read.py`).
- **The page never sets `CLOUDFLARE_TUNNEL_TOKEN` unattended** (#463; its
  tunnel's owner sees the host's IP): `PAGE_HELD_KEYS` values are held,
  asked on a Mac ("clearnet-tunnel"), refused on Linux; removal is
  applied. `TestTunnelToken` in `tests/test_handle_action.py`.
- **Page requests are handled only by `onionpress handle-action`**
  (`handle_action.py`, both platforms): fixed verbs (`ACTIONS`), each run
  through the launcher; `update` only checks on both platforms; on a Mac an
  address change stops for the person at the Mac (`pending-action/`).
  `tests/test_handle_action.py`.
- Every result carries the request's id (`checked_request_id`);
  `tests/test_settings_page_results.py`. Reads are size-capped
  (`READ_LIMITS`), actions throttled (`MIN_INTERVAL`), unknown request text
  never logged (`TestBoundedReads`, `TestThrottle`).
- Every `wp_ajax_` handler with a side effect `check_ajax_referer`s
  (`tests/test_settings_page_nonces.py`, a census).
- Page Stop/Restart go through the launcher's cases (Linux stops the
  heartbeat first). `tests/test_linux_settings_page_stop.py`. Share Now is
  taken by the long-running process and refused while sharing is off.
  `tests/test_settings_page_share_now.py`.
- **`onionpress config get|set|unset|list` is the apps' one writer of
  `~/.onionpress/config`** (`config.CLI_CONFIG_KEYS`). A new setting gets
  its `config set` entry first (check, `CONFIG_KEY_HELP`, `NEEDS_RESTART` /
  `CODE_DEFAULTS`); a UI only calls the command. `ONIONPRESS_*` and
  `ONIONHEAVEN_ACCEPT_REGISTRATIONS` are hand-edit only. Secrets only as
  `config set KEY -`. `tests/test_cli_config.py`.
- **One rule reads the config** (#404): the first `KEY=` line wins, the
  value is all after its first `=`, trimmed: `config.read_value` /
  `read_config` and the launchers' `config_value` (identical in all three
  scripts; never `grep | cut`). Writes are a temp file + rename under
  `.config.lock`, one line per key. `tests/test_config_one_reader.py`.
- The page's backup leaves out host secrets (`include_host_secrets=False`,
  config cut to `SAFE_CONFIG_KEYS`); no restore takes the token from a
  backup. `tests/test_backup_host_secrets_off_wordpress.py`.

## WordPress Host Controls Are the Owner's
- `manage_options` is per-site in the multisite: **gate everything that
  reaches the host or every site on `onionpress_settings_owner_allowed()`**
  (super admin), in every handler, AJAX endpoint and REST
  `permission_callback`. Same for onboarding, Wayback, blog snapshots and
  My Creations upload; social importers stay per-site.
  `tests/test_settings_owner_only.py`.
- The backup download sends/deletes only an
  `OnionPress-<≤8>-<YYYY-MM-DD-HH-MM>.zip` regular file; changing the name
  in `handle_action.py` means changing the pattern.
  `tests/test_backup_download.py`.
- **A key import never loses the current key**: every import goes through
  `replace_onion_key` (check, stage, copy old keys to
  `~/.onionpress/replaced-keys/`, then release hubs and swap). On a Mac the
  page's import needs the person at the Mac (native dialog, Cancel default,
  5 min = Cancel); Linux refuses it from the page.
  `tests/test_onion_key_replacement.py`.
- **Code in WordPress never reads or replaces the site's onion key without
  the owner at the host**: the Mac page's backup excludes it
  (`include_onion_key=False`; every `create_backup` call says which); a
  keyless backup never mints an address on any path; a page restore of
  another address asks (Mac) or is refused (Linux); a hub/OnionHome node,
  or one that cannot say, restores nothing from the page.
  `tests/test_settings_page_onion_key.py`.

## The Mac Daemon (issue #367, CLI-first phase 3)
- **`onionpress daemon` runs every loop; the menubar is a view** (menu,
  dialogs, notifications, setup windows, starting the daemon). The menubar
  runs no loop naming docker, the hub or Tor, no timer but the splash tips.
  `tests/test_mac_daemon.py`.
- **The menubar performs no operation itself**: every action is an
  `onionpress …` subcommand run through the launcher; it imports none of
  `backup`, `hub_backup`, `updater`. A new menu action gets its subcommand
  first. `tests/test_menubar_actions.py`.
- launchd agent `org.onionpress.daemon` (one per user, `KeepAlive` on
  failure only, `AbandonProcessGroup`); `onionpress daemon
  start|stop|status|offline`. The launcher's `stop` runs `daemon offline`
  before stopping containers; `quit` runs `daemon stop` first.
- State in `~/.onionpress/daemon-state.json` (atomic, 0600); a state older
  than 90 s or from a dead pid shows "Reconnecting", never "Stopped". Two
  processes never write one RotatingLog.
- `.setup_complete` is written by every completed setup path
  (`setup_logic.mark_setup_complete`). `tests/test_setup_complete_marker.py`.
- Quit takes the site down unless `QUIT_KEEPS_SITE_RUNNING=yes`. Update and
  uninstall stop the daemon first.
- **The daemon's start never stops Colima**: with no daemon alive a running
  VM is a crash restart's or a terminal start's live site; its port is read
  (`resolve_port_offset`). `TestResolvePorts` in
  `tests/test_mac_daemon_paths.py`.

## The App's Functions as Subcommands (issue #382)
- `version`, `address`, `check-reachability`, `logs`, `open`, `onionname`
  live in `src/onionpress/app_commands.py`, shared by both platforms, never
  constructing `OnionPressCLI`. The browser never gets the login token.
  `tests/test_cli_app_commands.py`.

## Check/Confirm Protocol
- **Every destructive command speaks it** (`confirm.py`): `--check --json`
  changes nothing and answers ok / `{"confirm": …}` (exit 4) / error;
  the UI asks (Cancel default, 5 min = Cancel) and reruns with
  `--confirmed <what>`. Off a terminal it refuses without confirmation
  (exit 3). `{"ask": "password"}` (exit 5): secret via env or stdin.
- A new question: `confirm.add_arguments`, `confirm.require`, the name in
  `QUESTIONS`, a dialog in `_ask_cli_question`, a flow in
  `tests/test_cli_confirm_protocol.py`. The Linux tray is a client too
  (`tests/test_linux_tray_protocol.py`).

## Blog Snapshots Stay Off the Web
- My Creations is published: nothing private there under a name not
  starting with `.`. WXR snapshots live in `.blog-archives/`; every
  publishing layer (`is_public_path`, `.htaccess`, `onionpress-creations.conf`
  with `<DirectoryMatch>`) refuses hidden paths and the legacy
  `blog-archives`. `tests/test_blog_snapshots_private.py`.

## My Creations Listings, Serving and Uploads (issue #319)
- Listings go through `cached_listing()` (stale-while-rebuild); cached
  entries hold no URLs. `serve_file` handles ETag/304/ranges and streams.
  `tests/test_creations_listing_and_serving.py`.
- Script-capable documents (`SANDBOXED_EXTENSIONS`, matched by the conf's
  `FilesMatch`) are served with `Content-Security-Policy: sandbox`
  (no `allow-scripts`) and `nosniff`, on both paths.
  `tests/test_creations_sandboxed_serving.py`.
- Thumbnails are never removed when part of a folder could not be read.
- **Host code that walks the Creations mount follows no link** (#460):
  `.thumbs` must be a real folder, linked sources are skipped, removals and
  writes go through `O_NOFOLLOW` folder fds (`thumbnails.prune`/`install`),
  qlmanage renders into a private temp folder.
  `tests/test_creations_thumbnails.py`.

## Follow Requests Stay Off the Container Network
- **Never call `wp_remote_*`/`fetch_feed` directly for a follow**: use
  `onionpress_follow_remote_get/_head/_fetch_feed` (URL shape check, no DNS
  lookup, redirects held to the same rule). `tests/test_follow_requests.py`.

## Imported Bodies Are Filtered at Ingest (issue #403)
- Every foreign HTML field goes through `wp_kses_post()` where it is read.
  `tests/test_importers_kses_at_ingest.py` (census).
- **Every importer comment goes through `onionpress_social_insert_comment`**
  (#462; `wp_insert_comment` runs no comment filter): author, URL and
  content are cleaned before storing. Tweet text is escaped first.
  Same test.
- **Every foreign title and excerpt is `onionpress_social_plain_text()`**
  (#472, `onionpress-social-archive.php`:
  `sanitize_text_field( wp_strip_all_tags( … ) )`, never
  `html_entity_decode` afterwards, which turned `&lt;script&gt;` back
  into markup): the_title() and the_excerpt() run no kses, and the
  save-time `title_save_pre` kses that stripped a Reddit thread's
  `<img onerror>` title under wp-cron is off for the owner's own Sync
  now or ZIP upload (unfiltered_html). Reddit thread titles are cleaned
  when fetched (raw_json=1), when the cache is read and when applied;
  CSV/API titles and every `onionpress_reddit_insert_post`; the blog
  importer's `title.rendered` / `excerpt.rendered`; the previews cut
  from bodies (Twitter, Bluesky, Mastodon, blog). The census holds every
  `post_title` / `post_excerpt` to the helper (an attachment's
  `sanitize_title` aside); a new importer's titles go through it.

## Importer ZIP Uploads Are Held to Limits (issue #473)
- The Twitter and Reddit importers take a ZIP from any site administrator
  and used `extractTo` on every member with an allowed extension: no cap
  on count, size, total or ratio. Both now unpack through
  `onionpress_social_zip_extract( $zip_path, $target_dir, $pattern )`
  (`onionpress-social-archive.php`), modelled on
  `backup.check_archive_size`: before a byte is written the archive may
  list at most `ONIONPRESS_SOCIAL_ZIP_MAX_MEMBERS` (100,000) members,
  none over `ONIONPRESS_SOCIAL_ZIP_MAX_MEMBER_BYTES` (2 GiB), adding up
  to at most the larger of `ONIONPRESS_SOCIAL_ZIP_MIN_UNPACK_BUDGET`
  (1 GiB) and `ONIONPRESS_SOCIAL_ZIP_MAX_EXPANSION` (20) times the
  upload's own size, the wanted members must fit the disk with
  `ONIONPRESS_SOCIAL_ZIP_FREE_RESERVE` (512 MiB) to spare, and a
  symlink, special file, absolute, `..`, backslash or control-character
  name or a wanted name listed twice refuses the archive whole. Only the
  members an importer reads are extracted, by name pattern
  (`ONIONPRESS_TWITTER_ZIP_MEMBERS`: `data/tweets*.js`, `data/account.js`,
  the tweet media files; `ONIONPRESS_REDDIT_ZIP_MEMBERS`: `posts.csv`,
  `comments.csv`; each at the root or one folder down), each written by
  the plugin from `getStream` and held to its listed size (a member that
  inflates past it fails the extraction and what was written is removed).
  The workdir is 0700; a refusal is shown with its reason. Nothing in
  the plugins may call `extractTo` again. `tests/test_social_zip_limits.py`
  (the limits, the patterns, and archives built with zipfile run under
  php where installed).

## The Blogroll Refreshes in wp-cron, Never in a Page View (issue #314)
- The serve path only reads cached items and schedules
  `onionpress_blogroll_refresh`; **never fetch from it**. The refresh is
  locked, time-budgeted, backs off and keeps a failing follow's items.
  wp-cron's loopback goes to `http://localhost/`.
  `tests/test_blogroll_refresh.py`.

## WordPress Resolves No Name Outside Docker
- A DNS lookup in the wordpress container is a leak. onionpress-tor-proxy
  checks URLs by shape (`onionpress_remote_url_is_safe()`), never by lookup;
  redirects stay on their route. `dns: 127.0.0.1` for wordpress and db, on a
  user-defined (not `internal`) network. Raw `curl_init` in plugins must
  use a socks5h proxy or talk only to a container.
  `tests/test_no_clearnet_dns.py`.
- **Nor any connection (issue #144)**: `wordpress_egress.py`'s
  ONIONPRESS-WP chain refuses everything from bridge `onionpress-wp0`
  except replies; ONIONPRESS-TOR keeps `onionpress-tor0` off the host and
  private ranges (DNS excepted); INPUT closes WordPress's published port to
  every bridge. Applied before every start's first `compose up`; best
  effort (a WARNING, never a stopped start). The wordpress network's name
  sorts after `onionpress-network` and stays IPv4-only.
  `tests/test_wordpress_egress.py`.
- `migrate_tor_network` removes an old onionpress-network before the first
  `compose up`, removing only known containers by exact name.

## The Docker Socket Stays Out of onionheaven
- Only `autoheal-proxy` and `onionheaven-farm` mount the socket (`:ro`,
  `network_mode: none`, `cap_drop: [ALL]`, `read_only`).
- **Never add a `docker` call to a tor-image script other than
  onionheaven-farm.py**, and never a farm verb that takes a command line, a
  path, an image, a mount or a capability. `tests/test_onionheaven_farm.py`.

## A Plain Node's Address Carries No Hub API (issue #393)
- Port 8083 (web-server.py) is mapped onto the onion address only when
  `hub_api_exposed()` (hub or OnionHome). socat listeners are
  `-T 30 … max-children=64`. `tests/test_tor_node_surface.py`.
- **do_POST checks every body's length before a handler runs** (digits
  only, the path's limit, no chunked); bodies are read under a deadline,
  at most `MAX_CONNECTIONS` connections (#461). Handlers read only via
  `_read_json` and check `is False`. `tests/test_hub_request_limits.py`.

## Containers Drop Every Capability
- **Every compose service, takeover worker and one-shot `docker run` has
  `cap_drop: ALL`, `no-new-privileges:true`, and adds back only the set
  `tests/test_container_capabilities.py` lists for it.** A new `docker run`
  fails the census until listed. Never NET_RAW, NET_ADMIN, SYS_ADMIN,
  SYS_PTRACE, SYS_MODULE, DAC_READ_SEARCH, ALL or privileged (the one
  exception: the Linux system-Docker egress helper, held to its shape).
  Write compose lists block style. Change a set only after a live test.
- One-shot helpers: `--network none`, entrypoint overridden, sources
  mounted read-only, the **pinned** tor image (`KEY_HELPER_IMAGE`,
  `ensure_tor_image`), never a floating tag. Never `docker run -v` a volume
  that may not exist (it creates an empty one that passes for a key).
- Never make a non-root process call su or sudo in the images; nothing uses
  raw sockets.

## The Data Dir Is the User's Alone (issue #397)
- `$DATA_DIR` is 0700 on every start; every creation of `config` is 0600;
  every `os.makedirs` of the data dir passes `mode=0o700`.
  `tests/test_data_dir_permissions.py`.
- **The .deb postinst and install.sh make it as the user, never as root**
  (issue #471): the same `plain_path` / `data_dir_setup` /
  `first_install_files` in both (test-held identical), run through
  `runuser -u "$REAL_USER" --` (postinst) or `run_as_user` (install.sh);
  `install -d -m 700`, secrets and config under `umask 077` + `set -C`
  (0600 from the first byte, a link at the name fails the write), a link
  or a non-folder/non-file at `~/.onionpress`, `secrets` or `config`
  refused with a message (the launcher makes what is missing), existing
  files never rewritten; the root `chown -R` fixup is gone. The Docker
  bootstrap's `docker-rootless-setup.log` is opened by the user's shell
  (`as_user_logging_to`, #490): root opens nothing under the data dir.
  `tests/test_deb_postinst_data_dir.py`.

## Container Output Never Reaches Bash Arithmetic (issue #466)
- Inside `$(( ))`, `(( ))`, `[[ a -gt b ]]`, `let` and `declare -i` bash
  evaluates a variable's VALUE as an expression, and `a[$(cmd)]` in it
  runs cmd (bash 3.2 and 5). The Mac launcher's `detect_container_runtime`
  put the tor container's MemTotal (`docker exec … awk`) straight into
  `$(( ))`: root in that container chose what the next `onionpress start`
  ran as the Mac user. Every number read out of a container, or derived
  from docker's answer, is now held to `[[ "$x" =~ ^[0123456789]{1,N}$ ]]
  || x=0` before arithmetic sees it (`_vm_mem_kb`, `_vm_cpus`; Linux's
  `started_epoch`). `[ a -gt b ]` (the test builtin) does not evaluate
  expressions, but write the guard anyway: the census in
  `tests/test_launcher_container_arithmetic.py` follows a value assigned
  from `docker`/`wp_cli`/`wp_exec` output (transitively) into any
  arithmetic context in the three launcher scripts and fails on one
  without that guard line between assignment and use; it also runs the
  guarded blocks under bash with `a[$(>file)]` as the container's answer.

## The Launchers' Python Ignores the Current Directory (issue #470)
- `python3 -m x` and `python3 -c` put the cwd first on sys.path (`-m`
  ahead of PYTHONPATH), and WordPress has My Creations read-write: a
  planted `socket.py` or `onionpress/__init__.py` there ran as the host
  user when the owner ran `onionpress …` from that folder. Linux: every
  `python3` in `linux/onionpress` runs `-I` (no cwd or script dir on
  sys.path, no PYTHON* environment, no user site; `-P`/PYTHONSAFEPATH
  need 3.11, which the .deb's `python3` does not promise). `op_py` is
  `python3 -I -B -c '<boot>' "$ONIONPRESS_PYTHONPATH" "$@"`: the boot
  puts the argument on sys.path and `runpy.run_module("onionpress.cli")`;
  `-B` keeps `PYTHONDONTWRITEBYTECODE`'s effect; PYTHONPATH stays in the
  environment for the Python the CLI starts itself (derivatives'
  `sys.executable -m onionpress.cli`, still un-isolated: open). `op_py`
  is self-contained (the harnesses extract it alone); `OP_PY` is the same
  argv for `run_detached`. A script run by path (`"$SCRIPTS_DIR/key_manager.py"`)
  gets its own root-owned dir on sys.path, not the cwd, and stays as is
  (the key-replacement test holds the two launchers' bodies equal).
  Mac: `bundled_python` sets `PYTHONSAFEPATH=1` (never `-I`: it would
  ignore PYTHONHOME) and its one `-c` says `-P` too. No `cd /`: nothing
  in either launcher depends on the cwd, and relative backup paths would
  break. `tests/test_launcher_python_isolated.py` (census; op_py and the
  snippets run from a folder of planted modules).

## WordPress's Installer Is the Owner's Alone (issue #464)
- Until WordPress is installed, `wp-admin/install.php` takes the first
  visitor's choice of admin (user 1, super admin after the multisite
  conversion); the .deb starts the units at once, Linux binds WordPress's
  port to every interface by default, Tor serves the site as soon as the
  key exists, and a setup that found WordPress installed adopted it.
- **Only a WordPress OnionPress installed is adopted.** Every install
  writes `~/.onionpress/.wordpress-installed-by-onionpress`
  (`setup_logic.INSTALLED_BY_ONIONPRESS_MARKER`: user 1's `user_registered`
  as WordPress records it, else `unknown`): `install_fresh_wordpress`
  (after the password is set) and both launchers' bash `setup`
  (`note_wordpress_installed_by_onionpress`, test-held identical, right
  after `wp core multisite-install`). `provision_existing_wordpress`
  refuses an installed WordPress whose marker is missing or does not
  match the live install (`FOREIGN_INSTALL_MSG`, False; the Linux tray's
  legacy branch gets the same answer) unless `adopt_existing`:
  `onionpress provision --adopt-existing`. A restored site's stamp
  differs from the marker: `provision` is not for restored sites.
  `tests/test_setup_adopts_only_its_own_install.py`.
- **Linux binds WordPress's port to 127.0.0.1 until WordPress is
  installed.** `start_containers` composes with
  `ONIONPRESS_BIND_HOST="$(wordpress_bind_host_for_start)"` (the config
  value only once a start has seen WordPress installed:
  `$DATA_DIR/.wordpress-installed`, a cache verified every start), then,
  with `wp core is-installed`'s answer, `bind_wordpress_port_for_install_state`
  records the verdict and recreates the wordpress container when its
  mapping (`docker port`) is not the one the state calls for: once per
  install, right after setup. Not installed: `hold_heartbeat_client_until_installed`
  masks the heartbeat user unit at runtime (`systemctl --user mask
  --runtime`, gone at reboot; `start_heartbeat_client` respects it), so
  nothing registers the address with the hub before the site exists.
  Every setup path ends in `after_wordpress_installed` (port moved,
  hold released): the `provision-post-install` subcommand that
  `install_fresh_wordpress` runs (the tray, `provision`), the `setup`
  case, `start_containers` for an installed site. Only the wordpress
  service's port reads `ONIONPRESS_BIND_HOST` (test-held), so the
  per-command override reaches nothing else. The .deb postinst's
  upgrade `try-restart onionpress-heartbeat` fails while the unit is
  held (it prints the commands and never fails the install).
  `tests/test_linux_wordpress_port_until_installed.py` (the functions
  under bash against stub docker/systemctl).
- **The image denies `install.php` and `setup-config.php` to every
  source but the owner's**: `onionpress-install-page.conf` (`<Files ~>`,
  `Require ip 127.0.0.0/8 ::1` or `%{REMOTE_ADDR} == osenv('ONIONPRESS_WP_GATEWAY')`,
  the container's default gateway, where the published port's requests
  arrive from on both platforms: auto-login's "local" rule). Tor visitors
  arrive from the tor container, cloudflared's from its own address. The
  entrypoint's `gateway_from_route` (the plugin's /proc/net/route parse,
  every field checked before the arithmetic) exports the variable before
  Apache starts; none means loopback only. Verified against Apache 2.4.58
  (`TestUnderApache` runs where apache2 is installed). The rule stays
  after setup. **Needs a WordPress image publish**; until then an old
  image still serves the installer to Tor, which the Linux bind and the
  adopt refusal do not cover. No `DISALLOW_FILE_MODS` (it would break the
  owner's plugin installs). `tests/test_wordpress_installer_local_only.py`.

## Colima VM Sandboxing
- The VM has exactly two mounts: `~/.onionpress/shared:w` and
  `~/OnionPress:w`. **No new `--mount` without a security review.** All
  other container data is in named volumes.
- **App state stays in `~/.onionpress/`**, never `~/Library/Application
  Support/` (spaces and the 104-char socket limit break Colima/Docker).
- New VMs get a 20 GiB disk (`VM_DISK`, 100 for a hub; fixed at creation).
- **`$COLIMA_HOME` must exist before anything runs Colima** (else Colima
  silently uses `~/.colima`). `tests/test_colima_home_before_first_start.py`.

## OnionHeaven Hub Data Volume
- Registry and registrants' keys live on `onionpress-onionheaven-data`
  (`/var/lib/onionheaven`), mounted by onionpress-tor, onionheaven and
  workers only — **never wordpress, never under `/var/lib/onionpress`**.
  `tests/test_onionheaven_data_volume.py`.
- The legacy move (`onionheaven-data-migrate.py`) reads WordPress-owned
  paths by fd only; **SQLite never opens a path there**; fails closed only
  with a real key at stake, never after the marker; older containers are
  retired before `compose up`. `tests/test_onionheaven_data_move_hardening.py`.
- **An error is never a "no"**: `hub_data_state()` / `onionhome_registry_present()`
  are tri-state; an unchecked backup says so in log, metadata and notes.
- Hub backups are sealed to the recovery key when
  `hub-backup-recovery.pub` exists (`hub_backup.py`, PyNaCl `SealedBox`);
  the private half is never stored, never on argv, never under `shared/` or
  in config. Every hub-data restore is checked first (`hub_backup.check`).
  `tests/test_hub_backup_sealed.py`.
- **Hub data never goes through WordPress**: the page's backup is
  `include_hub_data=False` and refused outright on a hub/OnionHome or a
  node that cannot say (fail closed); the page's restore refuses backups
  with hub keys or sealed data and never writes hub volumes.
  `tests/test_backup_hub_data_off_wordpress.py`.
- Compose and tor image ship together; never downgrade a hub across this
  change (docs/DOCKER-SOCKET.md).

## Registration Caps (issue #391)
- web-server.py bounds free registrations (`registration_room`,
  `analytics_room`, `MAX_NAMES_PER_ADDRESS`); existing pairs always have
  room. The numbers are still the issue's decision.
  `tests/test_hub_registration_caps.py`.
- **Dead rows are bounded too** (#492): `unregister_entry` keeps the
  newest `MAX_DEAD_ROWS_PER_ADDRESS` (4, at least 1, so #467's 404 still
  knows the address) and `MAX_DEAD_ROWS` (`MAX_REGISTRANTS` × 4) hub-wide
  (`prune_dead_rows`); `/status`'s `unregistered` counts kept rows.
  `DeadRowsTest` in the same file.

## The Hub Bounds What a Free Key Leaves and Tells (issues #467, #468, #469)
- #467: `/offline`, `/unregister` and the signed status query answer 404
  for an address the registry has no row for, before a replay mark or an
  address log line is written (`_verify_signature(..., registered_only=True)`:
  after `verify_payload`, before `note_signed_request`; only `/online`,
  bounded by `registration_room`, may leave anything). Every free-text
  body field goes through `onionheaven_common.short_text` (64 printable
  characters: the registry's `version`, the address log lines, the
  analytics metadata and audit). `_addr_logs` is an LRU of
  `_ADDR_LOGS_MAX_ADDRESSES` (8192, above `MAX_REGISTRANTS`), and
  `note_signed_request` deletes marks older than the tolerance on every
  insert (`SIGNED_REQUEST_MARK_TTL`, an index on `last_timestamp`).
  `UnknownAddressTest` and the pruning test in
  `tests/test_hub_signature_verification.py`, `BoundedGrowthTest` in
  `tests/test_hub_registration_caps.py`, the stored version in
  `tests/test_hub_stored_keys.py`.
- #468: `/logs/manifest` and `/logs/upload` take a v3 `healthcheck_address`
  only (with "" the files landed in `<content>/` itself, outside the
  per-instance quota; the body has to be read to see it), and
  `analytics_pair_room` holds the pair's folder, sidecars included, to
  `ANALYTICS_PER_INSTANCE_QUOTA` before every write (507). `analytics_disk_full`
  is the one reader of `shutil.disk_usage`, on `ANALYTICS_DIR` (a bind
  mount of the host's folder; "/" was the container's disk). The cleanup
  trims stray files under `<content>/` as a group of their own;
  `MAX_MANIFEST_FILES` bounds a manifest. No replay marks on `/logs/*`:
  the client re-sends the same signed upload when an ACK is lost (same
  second, so the same signature), and a replay re-stores identical bytes
  within the quota. `AnalyticsQuotaTest` in `tests/test_hub_registration_caps.py`.
- #469 (decided conservatively, 2026-10-29): `GET /status/<address>`
  answers `{address, registered, status}` for a content address only (a
  healthcheck address is 404: it names nothing on its own); the detail
  (healthcheck address, version, every time, audit, `recent_logs`) answers
  `POST /status/<address>` signed by the address's own key under
  `STATUS_ENDPOINT` ("status"), replay-marked like `/offline`, 404 when
  never registered; the aggregate `/status` keeps its counts and lost
  `takeover_workers` (names, per-worker queues; nothing read it).
  Consumers: the Mac daemon's `onionheaven._heartbeat_landed` (did a
  timed-out `/online` land?) is the one reader of the detail and signs
  its query (`onionheaven.STATUS_ENDPOINT`, test-tied); the launchers'
  hub-address check and the heartbeats read only the aggregate; linux/,
  the plugins and tests/stress read neither form. A hub without this
  release still answers the old GET. `tests/test_hub_status_privacy.py`
  (server, client and a consumer census).

## Registry Names Reach Docker Only as Workers'
- Registry data is attacker-writable: a name reaches docker only through
  `worker_name_ok`/`is_worker_name`, an address only through
  `onion_address_ok`. `tests/test_hub_registry_names.py` (census; a new
  docker argv must be guarded or listed in `REVIEWED`).

## Stored Registrant Keys Are Checked and Written in One Step
- `/online`'s PEM must derive to the address (`private_key_problem`,
  `keypair_error` via libsodium); else 400, stored key untouched.
- Key files are never rewritten in place: temp O_EXCL|O_NOFOLLOW 0600,
  fsync, rename, fsync dir. `tests/test_hub_stored_keys.py`.

## Request Signatures Are Made and Checked by libsodium
- Verify is PyNaCl's `VerifyKey.verify`; without PyNaCl it raises
  `VerificationUnavailable` (503), never says yes. Signing composes
  libsodium primitives only (no arithmetic of our own); the pure-Python
  fallback warns and must never be reached in shipped builds.
- The only verifier is web-server.py; `src/onionpress/onion_auth.py` is the
  source of the image's and stress image's copies (`build/sync-onion-keys.sh`).
  `tests/test_onion_auth.py`.
- **A v3 address is derived only by `onion_auth.derive_onion_address`; an
  onion key's OpenSSH PEM is read, built and checked only by
  `onion_keys`** (source in `src/onionpress/`, copied into the tor image by
  the same script). No second parser or derivation in web-server.py,
  key-convert.py, tor-watchdog.py or key_manager.py. `tests/test_onion_keys.py`.
- A verified request is accepted once (`note_signed_request`, replay →
  403). `tests/test_hub_signature_verification.py`.

## Inside a Takeover Worker
- The queue manager socket is 0600 from bind(); every client is root.
  `healthcheck_address` is fetched only if it is one v3 address.
  `tests/test_takeover_worker_hardening.py`.

## OnionHome Name Registry Volume
- `onionnames.db` lives on `onionpress-onionnames-data`, mounted by
  onionpress-tor only — never wordpress. `tests/test_onionhome_registry_volume.py`.
- **A restore never puts the backup's file on the volume**: it is rebuilt
  by the image's `onionhome-registry-migrate.py` and swapped in via
  SQLite's backup API; keep that script's flags and marker JSON stable.
  `tests/test_onionhome_registry_restore.py`.
- web-server.py serves no name (503) until the move's marker exists; an
  unreadable registry is held, never abandoned. Never downgrade OnionHome
  across this change.

## No Message Channel From the Hub (removed 2026-10-25)
- The healthcheck service is GET-only and nothing shows hub messages to the
  owner. A future hub→owner channel must be signed and designed for it.
  `tests/test_install_invariants.py`, `tests/test_mac_daemon.py`.

## The Directory Answers Visitors Only on OnionHome (issue #394)
- The directory's visitor hooks run only when the node's own address is
  OnionHome's (never on the Host header alone); misses cached 60 s.
  `tests/test_onionname_redirect.py`.

## OnionHeaven Is On by Default
- **`REGISTER_WITH_ONIONHEAVEN`: `yes` or unset registers, `no` opts out,
  anything else does neither.** Every gate is `== "yes"` after
  normalizing, never `!= "no"`. Nothing prompts. User-facing text says the
  heartbeat ships the private key to the hub and how to turn it off.
  `tests/test_onionheaven_opt_in.py`.
- The choice is re-read before every request carrying the key; nothing
  goes out unless registering. An explicit `no` sends one signed
  `/unregister`, retried every 15 min. `tests/test_onionheaven_opt_out.py`.
- The Linux heartbeat holds the same hub lock as the Mac client
  (`onionheaven-hub.lock` next to the record) around every /online,
  /unregister and record write (#404). `tests/test_linux_heartbeat_hub_lock.py`.
- **Every end of a hub's hold on a key takes it back** (uninstall, reset,
  key import, restore of another address, hub change) via
  `release_registration` / `onionheaven-unregister`; what could not be
  released stays in `left_on_hubs` and is shown. `_hub_lock` orders it
  against heartbeats. `tests/test_onionheaven_lifecycle.py`.
- Hub mode is decided by address (`is_onionheaven_instance`), never by a
  running container. `tests/test_linux_hub_mode.py`.

## Backups Carry Original Images Only (issue #257)
- Backups prune WordPress derivatives by `src/onionpress/derivatives.py`'s
  rule (shared with Linux via `prune-image-derivatives`); restores
  regenerate them in the background (`--only-missing`, marker cleared
  only when complete) and the `onionpress-image-fallback.php` mu-plugin
  serves originals meanwhile. The two halves ship together.

## Backup/Restore Time Limits Grow With the Site (issue #317)
- No fixed copy limit: floor + size at `wp_files.COPY_BYTES_PER_S`
  (`sized_timeout`); dumps stream to file. UIs wait for the launcher's
  backup/restore without a timeout. `tests/test_backup_timeouts.py`.
- **The Settings page's restore is never the launcher's `restore`**: it
  stays the in-place `restore_from_backup(through_wordpress=True, …)`
  (`cmd_settings_page_restore`), which carries the hub/OnionHome refusals
  and address-change check; a WordPress-uploaded backup must never reach
  install-from-backup.
- `wp_files.own(recursive=True)` never enters `creations` and follows no
  link.

## Backup Encryption (backup format 2)
- Format 2: plain `.zip` with `README.txt` + `onionpress-backup.enc`
  (Argon2id INTERACTIVE + secretstream XChaCha20-Poly1305 via PyNaCl, as
  they are). Layout in `backup_crypto.py`'s docstring;
  `tests/fixtures/backup-format-2.zip` is sha256-pinned — **never
  regenerate it**; changing the bytes means a new format.
  `tests/test_backup_manager.py`.
- KDF must stay restorable on a 1 GB Raspberry Pi; readers refuse headers
  above opslimit 4 / 128 MiB as "newer".
- PyNaCl is a dependency everywhere (Mac bundle rebuilt with `-O2` by
  `build/py2app-extensions.sh`; `python3-nacl (>= 1.5.0)` on Linux),
  imported guarded so format 1 works without it.
- **Format 1 (`zip -P`) must restore forever; never write `zip -P` again.**
- Launchers go through `onionpress.cli pack-backup` / `extract-backup`
  (password on stdin).

## Restores Extract Plain Files Only
- `validate_archive_members` runs before any extractor (no links, devices,
  absolute/`..`/NUL/empty/duplicate names); `check_staged_tree` rechecks
  before the first docker cp. `pack_backup` never stores links (`zip -y`).
  `tests/test_backup_archive_members.py`.
- Zip-bomb limits (`check_archive_size`); format 2's inner zip is extracted
  by zipfile only; format 1's `unzip -P` under `ulimit -f` and a disk
  watchdog. `tests/test_backup_extract_limits.py`.
- A restored site key is checked before use; the hostname is written from
  the key. `tests/test_tor_watchdog_service_ids.py`.
- **The healthcheck key is in no backup on either platform**: a start makes
  a new one and the heartbeat tells the hub; a restore takes none and
  removes a key an older restore staged.
  `tests/test_healthcheck_key_never_backed_up.py`.
- **A restored dump runs only SQL**: `backup.db_import` streams it on stdin
  to a client run `--sandbox --binary-mode --local-infile=0` (refuse a
  client without `--sandbox`); the DB password re-set uses the host
  secrets file, never a wp-config.php value, checked by
  `_reject_unsafe_db_value`. `tests/test_db_import_sandbox.py`.

## Backups Stage Plain Files Only
- What comes out of a container may hold planted links: unpack with
  `backup.unpack_plain_tar` (never a host `tar -x`), then
  `scrub_staged_tree`; `pack_backup` scrubs again.
  `tests/test_backup_staging_links.py`.
- The page's upload is copied to a private temp dir and must be a regular
  file (`check_uploaded_backup`; `tests/test_settings_restore_upload.py`);
  the finished page backup is written in as www-data, never root's
  `docker cp` + `chown` (`tests/test_settings_backup_handoff.py`).

## Root Never Writes Through www-data's Names
- **Everything the host hands the WordPress container goes in as www-data**
  via `onionpress.wp_files` (`put_file`, `put_tree`, `exec_args`; bash
  `wp_put`). Root's only chown is `wp_files.own_args` — never `chown …
  /var/www/…`, never a trailing slash. wp-content is read for backups as
  www-data. The onion address is written by www-data and read by the
  watchdog O_NOFOLLOW, one v3 address only.
  `tests/test_wordpress_writes_as_www_data.py`.

## wp-cli Runs as www-data
- **Never pass `--allow-root`.** Host Python: `wp_files.wp_cli_args` /
  `Docker.wp`; launchers `wp_cli` / `wp_cli_in`; image scripts
  `onionpress-wp`. Root acts on nothing wp-cli prints (except the audit's
  core zip at its fixed name). `tests/test_wp_cli_as_www_data.py`.
- Provisioning calls run offline (`wp_cli_args(offline=True)`,
  `ONIONPRESS_WP_OFFLINE=1`). `tests/test_wp_cli_offline.py`.
- `onionpress-web-root-owner.sh` follows no link, prunes `creations`, and
  changes nothing if mounts look wrong.
- Plugins install only from pins (`plugin_pin`, `build/pin-wp-plugin.sh`).
  `tests/test_plugin_pins.py`. App and WordPress image ship together.

## Cache Enabler and the Link Fixer Are Active
- Both launchers install pinned releases, configure (refusing without
  their policy mu-plugin) and activate: Cache Enabler `--network`, Link
  Fixer `--each-site`. `INSTALL_IA_PLUGIN` / `INSTALL_CACHE_ENABLER=no`
  skip one.
- Cache policy: store only anonymous pages for Host == the onion address;
  serve-time exclusions are forced on every site; stray hosts' settings
  files are removed. Link Fixer policy: never names an onion/local/private
  link, always verifies TLS.
- **Re-pinning either plugin means re-reading these against its internals**
  (docs/BUILDING.md). `tests/test_bundled_plugins.py`.

## The Hit Counter Has One Writer (issue #398)
- `onionpress-hit-counter.php` is the plugin folder's only PHP; the
  increment checks its nonce. `tests/test_hit_counter.py`.

## The Wayback Sweep Is the Network's (issue #322)
- archive.org keys live in the main site's options; lock and back-off are
  network options via `onionpress_wayback_net_*` only; only the main site's
  cron runs the sweep; one archive.org login
  (`multisite.fetch_archive_s3_keys`, over Tor).
  `tests/test_wayback_network_state.py` (tests use the
  `onionpress_wayback_net_key` filter, never live keys).

## Auto-Update Swaps Bundles by Rename
- **Never write into `/Applications/OnionPress.app` while the app runs**:
  stage the new bundle in a hidden sibling, then rename old away and new in
  (`stage_new_bundle`, `swap_in_bundle`). Writing in place makes macOS
  Gatekeeper revoke the copier's access mid-update.
  `tests/test_updater_staged_swap.py`.
- The running app's updater performs the update, so an updater fix takes
  effect one release later.

## Multi-User Support (v2.4.11+)
- Several macOS users can run one `/Applications/OnionPress.app`, each with
  their own data dir, VM and containers. Extra users offset host ports by
  +10000 each (18080/19050/19077…); container-internal ports never.
- **Detect ports with a socket bind test, never `lsof`** (it sees only the
  current user's processes). The daemon resolves the offset at start
  (`_resolve_ports_at_start`), sets `onion_proxy`'s module globals
  (`_apply_ports`) and publishes `ports`; the launcher's fallback respects a
  preset `ONIONPRESS_PORT_OFFSET`.
- **No `LSMultipleInstancesProhibited`** in any Info.plist (enforced across
  users). Launcher `pgrep` uses `-u "$(whoami)"` and matches this bundle's
  MenubarApp only. `tests/test_launcher_pgrep_anchor.py`.

## Browser Extension (issue #318)
- `.onion` routes to the daemon's HTTP proxy (`onion_proxy.py`), never to
  Tor's SOCKS port. `extension-firefox/` holds only the Firefox manifest.
- **The proxy carries `.onion` only and answers no other site's page**:
  `_from_a_web_page` refuses foreign `Origin`/`Sec-Fetch-Site`/`Referer`;
  no clearnet fetch, no `CONNECT`. `tests/test_browser_extension.py`,
  `tests/test_strict_onion_addresses.py`.
- **And only a request whose `Host` names it** (issue #465, `own_hosts`:
  `127.0.0.1`/`localhost`/`[::1]` with its port; a bare name only on port
  80): a page at a name its DNS points at 127.0.0.1 (rebinding) sends
  `Sec-Fetch-Site: same-origin` and no `Origin`, what the setup page's
  fetch sends. Checked first on every handler, OPTIONS and POST included;
  only an absolute-form request (`GET http://x.onion/`) names its target
  in `Host`. 403 without echoing the request.
- **A `/proxy/` page is sandboxed off the proxy's origin**: every response
  under `/proxy/` carries `PROXIED_PAGE_CSP` (`sandbox allow-forms
  allow-scripts allow-popups allow-modals allow-downloads`, never
  `allow-same-origin`, added in `end_headers`), so a visited onion site's
  script is no longer same-origin with `/status` or `/setup`. Its own
  requests then arrive as `cross-site` with `Origin: null` or none, what
  another site's page sends, so `_rewrite_onion_links` writes
  `/proxy/<PROXY_PAGE_TOKEN>/<host>/...` (a per-run secret only a page the
  proxy served knows) and a tokened request skips `_from_a_web_page`; the
  plain `/proxy/<host>/` form (tools, the address bar) keeps the check.
  `_setup_post_same_origin` also needs `Sec-Fetch-Site: same-origin` when
  it is sent. Absolute-form responses get no CSP: that page is the
  onion's own origin.
- Fallback pages only link the Wayback Machine, never redirect. Not
  live-tested on a Mac; the native host likely does not start from a
  py2app build.

## Cloudflare Tunnel Runs on Both Platforms (issue #352)
- `CLOUDFLARE_TUNNEL_TOKEN` adds `docker-compose.cloudflare.yml`. Linux
  starts cloudflared only once WordPress is installed and removes it when
  the token goes. Ingress must be `http://wordpress:80`.
  `tests/test_linux_cloudflare_tunnel.py`.

## Colima Networking Gotcha
- **SOCKS (9050) does not work through Colima port forwarding.** For any Tor
  traffic from the Mac, `docker exec` into the tor container; never
  `curl --socks5-hostname 127.0.0.1:9050` from the host.
- `wget` in the tor container cannot reach external `.onion` (no SOCKS).
  Use `socat` with SOCKS4A:
  `printf "GET / HTTP/1.1\r\nHost: <addr>.onion\r\nConnection: close\r\n\r\n" | socat -t 10 - SOCKS4A:127.0.0.1:<addr>.onion:80,socksport=9050`.
  The WordPress container has `curl`.
- Internal path check: `docker exec onionpress-tor wget -q -O /dev/null http://wordpress:80/`.

## The OnionHeaven Overview (one view of both roles)
- `onionheaven.overview(app)` is the one view of client and hub roles; UIs
  read it (status.json, daemon-state.json, `onionpress onionheaven
  overview`) and never ask docker themselves. Registry rows are validated
  before display (`hub_sites`). `tests/test_onionheaven_overview.py`.

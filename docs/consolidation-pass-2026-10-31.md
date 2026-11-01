# Consolidation pass, 2026-10-31: what still runs outside `onionpress.cli`

A code read of `main` @ 378c687d (after v2.6.1, #503–#509 and #519).
Nothing here was live-tested. Line numbers are at that commit.

**Tracking issue: #530** holds the sequence and how to continue; each
ranked section below names its issue.

The rule this pass checks: **the CLI is the one implementation; every
other surface (the two bash launchers, the menubar, the Linux tray, the
daemon, the Linux heartbeat, the WordPress plugins) calls it.** Phase 4
(#120) and phase 4b (#442) are on main: the start, the setup, the key
seed, the pending key import, the key volume migration, the egress block,
the bundled plugins and `db_import` are Python with one-line bash
delegations. This pass is about what those left.

## The shape of what is left

| Surface | Size | Still its own implementation |
|---|---|---|
| `linux/onionpress` | 4214 lines bash | backup 299, restore 252, `write_status` 347, `update_*` 510, `import_key` 139, `replace_onion_key` 105, `wait_for_services` 80, `apply_config_updates` 52, `uninstall` case 173, `reset` case 71 |
| `app/MacOS/onionpress` | 2716 lines bash | `import_key` 197, `replace_onion_key` 105, `scrub` case 152, `quit` case 129, `wait_for_services` 79, `_validate_oh_address` 58; the Colima bootstrap (~405, stays for now, #405) |
| `linux/onionpress-heartbeat.py` | 2206 lines | ≈1,300 lines re-implement `onionheaven.OnionHeavenClient`, `daemon.Daemon`, `docker.Docker`, `config` (#407) |
| `src/menubar.py` | 3409 lines | first-run WordPress install, onionname registration, an admin rename by raw SQL, the login-token mint, bundle chmod, native-messaging install |
| `linux/onionpress-tray` | 2247 lines | first-run install in-process, Tor Browser launch (150 lines), a direct `python -m onionpress.cli` call, a direct `systemctl --user stop` |
| `onionpress-settings.php` | | its own GitHub release lookup and version compare over Tor, its own archive.org login, a copy of the config allowlist and of `NEEDS_RESTART` (drifted: 5 keys vs 10) |

Three facts frame the ranking:

- **Neither launcher's default case reaches the CLI.** An unknown verb
  prints usage and exits 1 on both. So the daemon's
  `_reconcile_archive_s3_keys` (`daemon.py:1368`), which runs the launcher
  with `ensure-archive-s3-keys`, has failed on every Mac run since it was
  written: the verb is a CLI subcommand but no launcher case. The start's
  detached run (`start.py:821`) is what actually does the work.
- **The same function pairs that tests hold identical are what freezes
  the bash.** Sixteen tests assert a Mac body equals a Linux body
  (`test_backup_password_handoff`, `test_cloudflare_tunnel`,
  `test_onion_key_replacement`, `test_strict_onion_addresses` …). Each
  move retires one for a Python unit test, as phase 4's slices did.
- **Linux has three long-running drivers for what the Mac does in one
  process**: the heartbeat unit, the bash `_status_poll_loop` forked from
  `start`, and the 10 s `onionpress-watcher.timer`. The Mac daemon
  (`daemon.py`) imports nothing from AppKit; what keeps it off Linux is a
  handful of `detect_os()` seams listed under item 2.

## Ranked plan

Ordered by bash or duplicate Python removed, weighted by how far the two
copies have already drifted. Sizes are lines removed, approximate.

### 1. Linux backup and restore run the CLI's (issue #406) — ~680 lines

Issue: #406.

`linux/onionpress` `create_backup` (1740–2038), `key_volume_cat`,
`restore_backup` (2040–2291), and the `backup`, `settings-page-backup`,
`settings-page-restore` cases re-implement `backup.create_backup`,
`backup.restore_from_backup(through_wordpress=…)` and
`cli.cmd_settings_page_backup/restore`, the Mac's path. The drift is
real: the key is read by `key_volume_cat` + `ctor-to-arti` on Linux
(#519) and `key_manager.extract_private_key` on the Mac; the address
prefix rule has three variants (Mac caps at 5, Linux floors at 3 with no
cap, the restore keeps the old length); the refusal strings are held
equal by test instead of being one string. Seam: `detect_os()` hooks for
`stop_heartbeat_client`/`start_heartbeat_client` around the restore, and
the `--check --json` argv parse the two `restore` cases repeat only to
refuse the legacy positional password (the CLI's argparse already does
that; keep `read_backup_password`).

### 2. `onionpress daemon` runs on Linux; the heartbeat goes (issue #407) — ~1,300 lines

Issue: #407.

`daemon.Daemon` + `onionheaven.OnionHeavenClient` + `reachability` +
`analytics_sharing` already are the implementation; the heartbeat's
`_hub_lock` is byte-identical to `onionheaven._hub_lock`, its
`register`/`heartbeat`/`unregister`/`send_offline`/`_record_*`/
`_maybe_unregister_after_opt_out` are a second copy, and its
`AnalyticsHost` fakes the daemon's attributes to reuse the upload. The
seams, each one `detect_os()` branch:

- power: `mac_power.MacPowerHandler`'s constructor is the interface; the
  heartbeat's `SleepInhibitor` (logind) becomes `power.LogindPowerHandler`
  with the same `register()`/`run_loop_once()`; `power.SystemdInhibitor`
  (written for this, used by nothing today) replaces `CaffeineManager`.
- `_runtime_ready` (Colima's `.initialized` → `docker info`),
  `_setup_environment` (→ `platform.resolve_paths`' Linux socket),
  `_host_boot_time` (`sysctl kern.boottime` → `/proc/uptime`),
  `health.check_internet_connectivity` (`ifconfig` → `/proc/net/route`,
  the heartbeat's `_linux_has_default_route`).
- skip thumbnails, the clearnet dmesg capture and the onion proxy on
  Linux; the `platform: "macos"` literal at `daemon.py:1465`.
- `start-tor` (the one launcher verb the daemon runs that Linux lacks)
  becomes `ContainerManager.start_tor` called directly.
- `HubConnection` (230 lines, a persistent SOCKS socket for the fast
  `/offline` inside logind's delay) either becomes an optional transport
  in `onionheaven.py` or is dropped for docker-exec curl (~1.7 s, inside
  the 5 s delay).

Then `onionpress-heartbeat.service` is `ExecStart=… onionpress daemon`,
the bash `_status_poll_loop` and the watcher timer go (the daemon's
`poll_settings_page` replaces the timer), and Linux gains what it lacks
today: the onionname retry after a collision, the 12 h metrics snapshot
(`_emit_heartbeat_snapshot` is unreachable on Linux), log capture with
the IP scrub and all ten log types, `PREVENT_SLEEP` having any effect.
`offline_once`/`--offline-once` (no caller), `linux/onionpress-sleep-hook`
and the `sleep-pre|sleep-post` verbs (a no-op chain since v2.4.99) go
with it. Prerequisite: item 3, since the daemon's `_setup_environment`
is what makes the bare-`"docker"` modules work on the Mac.

### 3. One status writer — ~455 lines

Issue: #523.

Linux `write_status` (2295–2646; about 25 process spawns per 30 s tick,
eight inline `python3 -c`, an inline sqlite query of the hub registry
beside a call to `onionheaven overview --json` that already reads it, a
third reachability probe when `reachability.json` is stale, its own
WEDGE WARNING) + `write_recent_logs` + `linux_status.py` duplicate
`Daemon.write_status_to_volume` + `state_document`. One Python writer
produces both `daemon-state.json` and the volume's `status.json`; the
Linux-only fields (`wp_port`, `onboarded`, `wayback_queue_count`,
`mkp224o_available`, `vanity_in_progress`, `wp_admin_password_path`,
`serving_from_wayback`) are each one call the daemon has a wrapper for.
The tray then reads `daemon-state.json` through `daemon_client.read_state`
as the menubar does; `refresh-status` becomes identical on both
platforms. Can ship before item 2 as an `onionpress write-status` fast
path over the daemon's code.

### 4. Key import and replacement run the CLI's — ~590 lines

Issue: #524.

`import_key` (Mac 197, Linux 139) and `replace_onion_key` (105 each) are
orchestration living only in bash over `key_manager.encode_key_file`,
`import_key_b32`, `keep_current_keys`; the swap steps
(`.import-key-pending`, the vanity-keys folder, `ADDRESS_PREFIX`,
`ONIONNAME`) exist a second time in Python in `backup.restore_from_backup`
(~1367–1455). The Mac speaks the check/confirm protocol
(`--check --json`, `--confirmed`, `ONIONPRESS_CONFIRMED`); Linux prompts
with `read`. Target: `onionpress import-key` on `confirm.py` sharing the
swap with the restore, one prefix rule (`config.validate_address_prefix`),
and the Mac's `$SCRIPTS_DIR/key_manager.py` script copy
(`build-dmg-simple.sh:78`) retired.

### 5. One updater — ~440 lines

Issue: #525.

`updater.py` (`_parse_version`, `_curl_tls_args` with the tested
`--cacert` rule, `check_for_update`, `published_sha256`, `sha256_of`) and
`cli.cmd_update --check --json` already serve the Mac and, through the
tray's direct call, Linux. `linux/onionpress`'s `update_*` family (510
lines, 2788–3369) is a second GitHub client (`release_curl` has no
`--cacert` rule; `update_find_release` has `--version X`, `--pre` and the
draft/prerelease checks the Python lacks), a second SHA256SUMS parser, a
second version comparator, and `update_settings_page_check` builds
`update-result.json` with two inline `python3 -c`. `cli._run_update`
refuses on Linux and defers to the bash. Keep as the Linux leaf only the
apt-get install of the checked `.deb` (`update_from_release`'s second
half), `update_finish`, and the `--from-checkout` path. Fix in passing:
`update_finish` runs `docker compose pull` without `--policy missing`
(CLAUDE.md: starts pull `--policy missing` only).

### 6. The two UIs become widget-only — ~600 lines across both

Issue: #526.

`menubar._run_launcher_json`/`_launcher_failure` and the tray's
`Launcher.run`/`launcher_answer`/`launcher_failure` are one algorithm
twice; the backup and restore flows (the tray already has a toolkit-free
`plan_restore(path, ui)` tested against a stub launcher; the menubar has
the same state machine inline plus a post-restore summary the tray never
shows); the update-check classification; the confirm-question loop (the
menubar answers all seven `confirm.QUESTIONS`, the tray one). A
`src/onionpress/ui_client.py` (stdlib + `confirm` + `launcher_ops`, so
the menubar's docker-free import closure holds) with `run_launcher_json`,
`launcher_failure`, `confirm_loop`, `QUESTION_TEXTS`, `plan_backup`,
`plan_restore`, `restore_summary`, `update_outcome`; the toolkits keep
NSAlert and Gtk. `test_menubar_actions` and `test_linux_tray_protocol`
collapse onto one protocol test.

### 7. First-run install through `onionpress setup` on both platforms

Issue: #527.

`menubar._wp_core_install` (1687–1737) and the tray's
`SetupDialog._provision` (1432–1471) call `setup_logic.install_fresh_wordpress`
in-process (docker, markers, two launcher Popens). `onionpress setup
--user --title --launcher` with the password in `SETUP_PASSWORD_ENV`
exists (`setup_from_terminal`). Gaps: no `--language` (the setup window
offers nine), no `--json`, and setup does not register the onionname.
With it: `menubar._register_onionname_during_setup` (1589–1663, a
`Registrar` run from the view) becomes `onionpress onionname set NAME
--json` (exists; answers `taken` + suggestions); the admin rename by
f-string SQL (`_rename_wp_admin`, 1664–1686) becomes a `--rename-admin`
flag on it; Linux, which never registers the onionname at setup today,
gets the same path. Empties `DOCKER_ALLOWED` in `test_mac_daemon` and the
`ALLOWED` list in `test_menubar_actions`.

### 8. The Settings page stops doing the host's work — PHP + ~120 bash lines

Issue: #528.

- `wp_ajax_onionpress_check_update` (`settings.php:1148–1184`) fetches
  `api.github.com` over Tor on every Settings page load and
  `version_compare`s; handle-action's `update` then looks it up again.
  Host writes `update-result.json` on its own cadence; PHP reads.
- `config-updates.json`: handle-action applies it on the Mac only
  (`handle_action.py:372`, `host.os_name == "macos"`); Linux applies it
  from bash `apply_config_updates` inside `write_status`, with its own
  `systemctl` copy of `config.apply_side_effects`' `START_ON_BOOT`
  branch (which is already Linux-aware). Drop the gate, delete the bash,
  make the `apply-config` case `op_py apply-config` as the Mac's is.
- Share Now: `settings.php:257–296` touches `.upload-analytics`; the
  daemon and the heartbeat each `docker exec … rm` it per cycle. A
  handle-action verb over `analytics_sharing.request_upload` (which
  leaves the host-side marker both already take) removes the per-cycle
  exec into the wordpress container.
- `onionpress_fetch_ia_s3_keys` (`settings.php:67–107`) logs into
  archive.org from inside WordPress; `multisite.fetch_archive_s3_keys` is
  "the one archive.org login" per CLAUDE.md. A decision, not a mechanical
  fold: the PHP path keeps the password inside WordPress.
- The PHP copies of `SETTINGS_PAGE_KEYS` ranges, the token regex,
  `NEEDS_RESTART` (5 keys in PHP, 10 in `config.py`), `RESULT_FILES`
  (twice in PHP), `REQUEST_ID_RE` (three copies incl. inline Python in
  bash), the backup filename regex: either the host writes a generated
  `onionpress-settings-schema.json` to the volume, or a census test holds
  each PHP literal to its Python source as `test_backup_download` does
  for one.

### 9. Ports and paths resolved in Python only (issue #405, the half that is not Colima) — ~200 lines

Issue: #405.

The port offset is found in six places in three languages:
`ports.detect_port_offset`/`resolve_port_offset`/`get_running_wp_port`,
Mac bash 363–402 (probes 8080 only), Linux bash 253–334 (all three ports,
plus a bash copy of `daemon._resolve_ports_at_start`'s
`.previous-port-offset` wait), `daemon._resolve_ports_at_start`, the
heartbeat's `_detect_host_socks_port`, `OnionPressCLI`'s probe (#507).
One `ports.resolve_at_start(data_dir)`; each launcher calls it once at
the top. `platform.resolve_paths` becomes what the launchers export
`DATA_DIR`/`DOCKER_DIR`/`DOCKER_HOST` from. `config_value` stays the one
intentional bash copy (#404, test-held).

### 10. `docker.Docker` everywhere (issue #477) — prerequisite for 2

Issue: #477.

Still true: `Docker` is used by `cli`, `containers`, `daemon`, `health`;
`multisite` (37 lines plumbing `docker_bin`), `setup_logic` (25),
`derivatives`, `wp_files`, `auto_login`, `onion_proxy`, `scrub` thread
`docker_bin`/`env` by hand; `onionheaven._run_docker*` builds its own
from an `app` object with a Mac-shaped `colima_home` fallback (reused by
`analytics_sharing`); `key_manager._docker_cat` (the key read every hub
request depends on), `ports.get_running_wp_port`, `launcher_ops`,
`native_messaging_host` use bare `"docker"` with the ambient
`DOCKER_HOST`. No stated reason in `multisite`/`setup_logic`; they predate
`docker.py`. `Docker` already exposes `.docker_bin`/`.env`/`.wp()`, so the
swap is mechanical, module by module, with a census that fails on a new
`"docker"` argv literal outside `docker.py`.

### 11. The rest of the launchers' shared bash — ~600 lines

Issue: #529.

Each a small slice in the phase 4 shape:

- `wait_for_services` ×2 (drifted: Linux writes the address on the
  not-installed early return, the Mac doesn't; hc/address order swapped)
  + `get_healthcheck_address`/`write_shared_onion_address`/`wp_is_installed`/
  `get_status` → `onionpress wait-for-services` over
  `containers.wait_for_wordpress`/`wait_for_tor`, `onion_hostname`,
  `wp_files.put_file`. `wp_is_installed` has three Python copies already
  (`containers`, `multisite`, `bundled_plugins`): pick one.
- `_validate_oh_address` ×2 (110; the only real logic with no Python
  counterpart: the hub probe on `:8083/status`, `accepting_registrations`,
  the port-80 fallback) → `onionheaven.validate_hub_address` + a
  `validate-oh-address` subcommand, or `config set ONIONHEAVEN_ADDRESS
  --check --json` with two new `confirm.QUESTIONS`.
- Mac `scrub` case (152, the reset-in-place scrub) → `scrub.run_scrub`
  with a Mac phase set (Linux already delegates).
- Mac `start-tor` case → `ContainerManager.start_tor`.
- `setup_db_passwords` ×2 (79, Linux lacks the Mac's truncated-file
  guard) → `secrets_file.ensure_secrets`; bash keeps only the `export`.
- Linux `reset` (71) and `uninstall` (173) cases → `cli.cmd_reset` with
  the Linux tail, `uninstall.py` with a Linux branch; brings the
  check/confirm protocol to Linux (today `read -p`).
- Mac `help` (89 hand-written lines) vs Linux `usage` (6): generate from
  the CLI's argparse.

## Quick wins (an afternoon, no design)

Issue: #522.

- `daemon.py:1368`: run `ensure-archive-s3-keys` through the CLI, or
  delete `_reconcile_archive_s3_keys` (the start already does it).
- Make each launcher's `*)` case `… -m onionpress.cli --data-dir
  "$DATA_DIR" "$@"` so every CLI subcommand is reachable through the
  launcher and an orphaned verb cannot recur; the dispatch census already
  lists every subcommand.
- Delete: `_op_migrate_tree` and its call blocks in both launchers
  (`REMOVE-AFTER 2026-08-01` is past, ~90 lines); `get_onion_address`
  in both launchers (no callers); the Mac's `ensure_key_helper_image`
  (no callers); `power.SystemdInhibitor` (until item 2 wires it) or wire
  it; `heartbeat.offline_once` (no caller but a test); `menubar.run_command`
  (no callers); `wayback-queue.json`/`wayback-archived.json` unlinks.
- `linux/onionpress-tray:2019`: `update --check --json` through the
  launcher instead of `sys.executable -m onionpress.cli`; `_on_quit`
  through a Linux `quit` verb instead of a direct `systemctl --user stop`;
  `uninstall --check --json` before opening the terminal so
  `hub-holds-key` is asked; make the package import hard and delete the
  seven degraded fallbacks (the .deb installs the package beside the
  tray).
- `handle_action.py:372`: drop the `macos` gate (item 8, second bullet).
- `update_finish`: `docker compose pull --policy missing`.
- `onionpress-security-audit.sh` writes `security-report.txt` that
  nothing reads; surface it in status or stop writing it.
- `daemon.py:525` docstring promises a clock-gap sleep check in
  `check_status` that does not exist; `ReachabilityLoop._slept_since_last_cycle`
  is the implementation to lift.

## Smaller duplications noted, not ranked

- Key-file parsing exists four times: `key_manager.read_ctor_keys`,
  `key-convert.py:187`, `tor-watchdog.py:219`, `onionnames.py:571`; the
  image's are stricter and older (#519's missing-public-file case). One
  `read_ctor_keys` in a synced module (`build/sync-onion-keys.sh`).
- `key_manager.derive_public_key` + `_python_public_key` beside
  `onion_auth.derive_public_key` + `_python_public_key`: two derivations
  with two pure-Python fallbacks (CLAUDE.md: one derivation).
- The onionname validator three times (`onionnames.py`,
  `onionnames_client.py`, `setup_logic.py` "so it holds even when that
  module is missing"); the wordlist suggestion twice.
- The hub request body shape is built in three clients (Mac Python,
  Linux heartbeat, the stress worker) against a server that `data.get`s
  eight fields; no shared schema.
- `/var/lib/onionpress/onion_address` has three writers (Mac bash, Linux
  bash, `multisite.py:534`) and no refresh after start.
- `recent-logs.txt` has two Linux writers 10 s and 30 s apart
  (`handle_action.write_recent_logs` on the quiet tick, bash
  `write_recent_logs`).
- `onionpress-multisite-init.sh` (179) vs `multisite.ensure_multisite`
  (the Python lacks the siteurl flip, the half-converted unwind and the
  auto-update constants; `setup_logic.py:682` says "until ported");
  `scripts/install_plugin.sh` (161) and `activate_plugin.sh` (91) are
  host-side bash that `bundled_plugins.py` wraps, the pins in a shell
  `case`.
- `heartbeat.read_config` is a fourth config reader, not test-held,
  with a different `strip()`.

## Rules worth adding to CLAUDE.md once the work lands

- A launcher verb is a CLI subcommand; the launcher's default case
  delegates. (Test: the dispatch census's subcommand set ⊇ every verb the
  daemon, menubar, tray and `handle_action` run through a launcher.)
- A UI (menubar, tray, setup window, Settings page) performs no
  operation: it calls a subcommand and renders the answer. The menubar
  has this rule; extend it to the tray and the page, with the tray's
  import closure test matching `test_mac_daemon`'s.
- `onionpress daemon` is the one long-running process on both platforms;
  the Linux unit runs it.
- No bash body may exist on one platform where the other delegates to
  the CLI (today: `update`, `backup`, `restore`, `apply-config`, `reset`,
  `uninstall`, `write-status`, `scrub`).

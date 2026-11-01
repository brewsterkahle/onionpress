# Phase 4 (CLI-first): the bash launchers become dispatchers — resume note

Phase 4 of the CLI-first architecture (issue
https://github.com/brewsterkahle/onionpress/issues/120, as re-scoped by its
latest comment) moves the bodies of the two bash launchers
(`app/MacOS/onionpress`, macOS /bin/bash 3.2; `linux/onionpress`) into
`onionpress.cli` (`src/onionpress/cli.py`) one test-enforced identical
function pair at a time, until the launchers are one-line delegations and
can go away. Each slice: the Python keeps the bash body's log lines and
exit behaviour, both bash bodies become a one-line delegation (the bash
function name and its callers stay), the identical-pair test is retired for
a unit test of the Python with a fake docker, the `docker run` census and
the dispatch census get their rows, and CLAUDE.md stops naming the bash
function as the implementation.

## How to resume

```
git fetch origin phase4 && git checkout phase4     # ivar/onionpress, fixed name
DOCKER_HOST=unix:///var/empty/no-docker-for-tests.sock make test-unit   # ~86 skips
DOCKER_HOST=unix:///var/empty/no-docker-for-tests.sock make coverage    # floors below
```

Coverage floors (`build/coverage-floors.sh`): total 60 %, `cli.py` 69,
`handle_action.py` 85, `confirm.py` 90, `config.py` 85. Never lower one.
After every commit: `git push origin HEAD:phase4` (never squash or
force-push). No PR until the slices through 7 are in; the branch is the
hand-over. Never touch `~/.onionpress` or `/Applications/OnionPress.app`.
Run one suite at a time: `test_onionpress_colima` (`/tmp/colima_test_args*`)
and `test_backup_manager`'s staging-cleanup scans fail when two runs
overlap on one machine (`make test-unit` beside `make coverage`, 2026-10-24).
Each full run is about 5 minutes here.

Delegation shape: Mac `bundled_python -m onionpress.cli --data-dir
"$DATA_DIR" <subcommand> "$@"` (the bundle's interpreter; `/usr/bin/python3`
is a shim), Linux `op_py --data-dir "$DATA_DIR" <subcommand> "$@"`. A new
subcommand needs a `Row` in `tests/test_cli_dispatch.py`; one that runs
before the stack is up is a fast path in `cli.main` (no `OnionPressCLI`:
its setup creates config and secrets and probes ports), like
`wordpress-egress`. Bash-harness tests that cut a function out of a launcher
and run it under `bash` with a `docker(){}` stub need the delegation's
runner defined too (`bundled_python`/`op_py` → `sys.executable` with
`PYTHONPATH=src`), or a one-line bash stub of the delegated function when
the harness is about something else.

## Slice order and status

| # | pair | status |
|---|------|--------|
| 1 | `using_local_images` | done, 80aae349 |
| 2 | `regenerate_derivatives_pending` | done, 76bee60e |
| 3 | `retire_old_onionheaven_containers` | done, c5bb487a |
| 4 | `migrate_tor_network` | done, c8d644e6 |
| 5 | `migrate_key_volume` | done, 487a3e06 |
| 6 | `db_import` | done, 14d3ff94 |
| 7 | `block_wordpress_egress` | done, 7c22e46a |
| 8 | Linux `update_*` (the release client; the apt-get and checkout leaves stay bash) | done, 2026-10-31 (`cli.cmd_update` + `updater.py` on both platforms) |

Out of scope for now: `start_containers`, `setup`, the Colima bootstrap.

Later slices in the same shape, outside Phase 4's table: `write_status`
(issue #523, consolidation stage 2): `onionpress write-status` is a fast
path over `onionpress.status_writer` (the daemon's status pass as one
run, `OneShotStatus`, until the daemon runs on Linux, #407); the bash
body (347 lines, `write_recent_logs` and `linux_status.py` with it) is
the delegation plus its `apply_config_updates` guard; its three bash
harnesses (`test_wordpress_host_data`, `test_linux_safe_config`,
`test_linux_hub_mode`) became tests of the writer against a fake docker
(`tests/test_status_writer.py` has the field-by-field ones).

## Invariants each moved function must keep (CLAUDE.md has the sections)

- `using_local_images`: every `docker pull` / `docker compose pull` /
  `--pull always` stays gated on it (`tests/test_local_image_mode.py`
  `TestEveryPullIsGuarded`); a `ghcr.io/` reference is a published image,
  anything else in `ONIONPRESS_TOR_IMAGE` / `ONIONPRESS_WORDPRESS_IMAGE`
  (environment, else `~/.onionpress/config`) is a local build; the config
  value is never `eval`ed into commands.
- `regenerate_derivatives_pending`: true for the host marker
  `$DATA_DIR/.regenerate-derivatives-pending` or the container marker
  `/var/www/html/wp-content/uploads/.onionpress-regenerate-derivatives`
  (`derivatives.HOST_MARKER` / `CONTAINER_MARKER`); the kick-off in
  `start_containers` stays guarded on it, detached, after
  provision-post-install.
- `retire_old_onionheaven_containers`: before the first `compose up`, every
  `onionpress-tor`, `onionheaven`, `onionheaven-(poll|takeover)-<n>` that
  does not mount `onionpress-onionheaven-data:/var/lib/onionheaven` is
  removed, all at once; an inspect that fails keeps the container; never
  `onionheaven-farm` or `onionpress-wordpress`.
- `migrate_tor_network`: an `onionpress-network` whose bridge is not
  `onionpress-tor0` is emptied and removed once, after retiring and before
  `compose up`: `onionheaven` first, then OnionPress's own names by exact
  match (compose services, `onionheaven-(poll|takeover)-<digits>`), anything
  else only disconnected with a WARNING; a network that will not go is a
  WARNING, never a stopped start.
- `migrate_key_volume`: fails loudly, never into an empty copy: the pinned
  tor image (`containers.tor_image`, the launchers' `KEY_HELPER_IMAGE`;
  never pulled for a local stack), `--network none`,
  `--cap-drop ALL --cap-add DAC_OVERRIDE`, `--entrypoint sh`; walks down to
  `state/keystore/hss` one listed folder at a time (unreadable is not
  absent), refuses links and anything but folders and files, verifies every
  name and every file's bytes; `.key-volume-migration-pending` written
  before the copy and removed after it is verified; nothing starts tor
  while one is due.
- `db_import`: the dump streams on stdin after the password line into a
  client run `--sandbox --binary-mode --local-infile=0`; refuses a client
  too old for `--sandbox`; the password never on argv.
- `block_wordpress_egress`: `wordpress_egress.ensure` before the first
  `compose up`; a failure is a WARNING, never a stopped start.
- Everywhere: every `docker run` has `--cap-drop ALL` and a row in
  `tests/test_container_capabilities.py`; root never writes through
  www-data's names (`onionpress.wp_files`); wp-cli as www-data, never
  `--allow-root`; secrets never on argv.

## Slice notes

- Slice 1 (`using_local_images`): the `docker pull` gates stay in bash
  (`if using_local_images; then`); only the predicate's body moved. The
  bash function is `… --data-dir "$DATA_DIR" using-local-images`, a fast
  path in `cli.main` over `containers.using_local_images(paths.config_file)`
  (exit 0 local, 1 published). The launchers' config→environment export
  loop for the two image variables stays (compose reads them); its
  injection-safety test stays too. Bash harnesses: `test_install_invariants`
  defines `bundled_python`/`op_py` on `sys.executable`;
  `test_backup_metadata_validation._linux_key_helpers` emits a
  `using_local_images() { return 1; }` stub; `test_linux_backup_onion_address`'s
  `op_py` stub answers the subcommand; `test_backup_password_verify` runs
  the real delegation (it already had `op_py` and a `python3` wrapper).
  Also added in passing: the `daemon` subcommand's rows in
  `tests/test_cli_dispatch.py` (PR #376 and PR #377 landed separately, so
  the census was failing on main's merge of both).

- Slice 2 (`regenerate_derivatives_pending`): `regenerate-derivatives-pending`
  is a fast path over `derivatives.regeneration_pending(paths.data_dir,
  Docker(paths).docker_bin)`, the same check the sweep
  (`regenerate-derivatives`) runs, so the gate and the sweep cannot disagree;
  exit 0 = pending. The bash body is the delegation with `2>>"$LOG_FILE"`.
  Note for harness tests: `resolve_paths()` finds `/Applications/OnionPress.app`
  on a Mac that has it, so `Docker(paths).docker_bin` is the bundle's docker
  there, not a stub on PATH; a bash-level test can only exercise the host
  marker (and sets `DOCKER_HOST` at nothing so the container leg fails
  fast), and the docker leg is tested in Python with an explicit stub binary
  (`test_install_invariants.TestDerivativeRegenerationWiring`).

- Slice 3 (`retire_old_onionheaven_containers`): the fast path builds the
  manager with `cli._start_step_manager(paths)` (`PortConfig.from_offset`
  of `$ONIONPRESS_PORT_OFFSET`, never the bind probe; `_launcher_log_func`,
  `[ts] msg` on stdout) and always exits 0. The bash body is
  `… 2>&1 | tee -a "$LOG_FILE"`: the lines reach the log and the terminal
  as `log` sent them, and the pipe's status is tee's, so a Python failure
  is in the log but never stops the start (the bash body never did
  either). `test_onionheaven_data_move_hardening.TestLaunchers` keeps its
  bash harness for `stop_onionheaven_farm`; the retirement is tested in
  Python with a fake docker (removed set, inspect failure keeps, never the
  farm controller, all at once).

- Slice 4 (`migrate_tor_network`): the same fast path shape as slice 3
  (`cli.main` handles both names in one branch). The bash `MOVE_STUB`/`_move`
  harness tests went; `TestTorNetworkMove` (Python, fake docker) is the
  coverage, with the delegation check and the start_containers ordering
  check (`retire` before `migrate` before `compose up`) kept.

- Slice 5 (`migrate_key_volume`): `ContainerManager.migrate_key_volume`
  (with `volume_state`, the launchers' `docker_volume_state` in Python, and
  the sh copy as the module constant `KEY_VOLUME_MIGRATION_SCRIPT`), the
  fast path `migrate-key-volume` over `_start_step_manager`, exit 0/1 from
  its bool. Unlike slices 3 and 4 the status must reach the callers' `if !
  migrate_key_volume`, so the bash body is `… 2>&1 | tee -a "$LOG_FILE"`
  followed by `return "${PIPESTATUS[0]}"` (bash 3.2 has PIPESTATUS; under
  `set -e` a failing non-last pipeline member does not exit). The helper's
  own output, which bash appended to the log file, is logged line by line
  (`_log_lines`), so it reaches the terminal too. `ensure_tor_image` was
  already the Python `ensure_key_helper_image` (it only got the bash's
  "Pulling … for the onion service key helpers" words); the bash
  `ensure_key_helper_image` and `docker_volume_state` stay for the key
  seeds and Linux's `key_volume_cat`. Tests: `TestKeyVolumeMigrationInPython`
  (fake docker; every case the bash harness ran, plus the second `volume ls`
  unanswered, the marker's timing around the copy, a failed `volume create`,
  an unwritable data dir, and the CLI's exit status) and
  `TestMigrationScriptLocally` now reads the constant. The bash harness
  (`TestLauncherVolumeChecksUnderDockerFailure`) keeps two end-to-end runs
  of the delegation (status through PIPESTATUS, lines in the log) that skip
  where `Docker()` would take a bundle's docker over the PATH stub (a Mac
  with the app installed), and its stdout checks read the last line, since
  the tee'd lines now precede the `echo $rc`. The `docker run` census lost
  the two bash rows and gained containers.py's (its floor is 12 sites
  now). Pre-existing on this Linux machine, unrelated and identical at the
  previous HEAD: 10 failures in `test_onionheaven_data_move_hardening` /
  `test_onionheaven_data_volume` (the later-arrivals/sparse cases), and 2
  in `test_wp_cli_as_www_data.TestWebRootOwnerGivesBack` (a relative
  `./bin` in the shell's PATH makes `find -execdir` refuse); CI is clean.

- Slice 6 (`db_import`): `backup.db_import(sql_path, password, *, user,
  name)` is the one implementation: `restore_container_artifacts` calls it
  (the Mac's and the CLI's restores, the Settings page's), and
  `onionpress db-import --sql-file <dump>` is the fast path over it (no
  `OnionPressCLI`; the password is stdin's first line, never argv or the
  environment; exit 0, or one `ERROR: …` line on stderr and exit 1 for an
  empty password, a client too old for `--sandbox`, a failed import or a
  timeout). The Linux bash body is `printf '%s\n' "$1" | op_py --data-dir
  "$DATA_DIR" db-import --sql-file "$2"`, so `restore_backup`'s
  `db_import "$db_pass" "$sql_file" 2>>"$LOG_FILE"` is unchanged and the
  pipeline's status is the Python's. Two deltas from the bash: its "too
  old" line went through `log` (terminal + log file), now it is stderr
  only, which the caller's `2>>"$LOG_FILE"` lands in the log before the
  caller's own `log "ERROR: Database import failed"`; and the dump is read
  into memory (`subprocess.run(input=password + b'\n' + dump)`, as the
  Mac's restore always did) where bash `cat`ed it — a streaming Popen
  would be the better shape but every restore test records
  `backup.subprocess.run`'s `input` (test_secrets_off_argv,
  test_onionhome_registry_restore, test_db_import_sandbox), so it was
  left for a slice of its own. `db_import` refuses a password that is not
  one line (empty, LF or CR: the rest would be read as SQL). Tests:
  `test_db_import_sandbox` has `TestDbImport` (mocked `subprocess.run`:
  probe first, flags, argv shape, stdin bytes, the timeout equal to
  `sized_timeout(len(dump), _DB_IMPORT_FLOOR_S, _DB_IMPORT_BYTES_PER_S)`,
  old client refused before anything is sent, failed import's words, bad
  passwords), `TestDbImportSubcommand` (`cli.main` with stdin, never
  `OnionPressCLI`) and `TestLinuxDelegationEndToEnd` (the bash function
  cut out, `op_py` on `sys.executable`, a `docker` stub on PATH recording
  argv and stdin: the stub sees the password line then the dump, the
  function's status is the Python's). `test_backup_timeouts
  .test_the_import_is_sized` reads `db_import`'s body now. The bash
  harnesses in test_backup_hub_data_off_wordpress and
  test_settings_page_onion_key stub `db_import() { … }` themselves, so
  they did not change. No `docker run` here (exec only), so no
  capabilities row.

- Slice 7 (`block_wordpress_egress`): moved, not recorded as done as it
  stood — the two verdict lines were the last bash words of the body, and
  the move took well under an hour. Both launchers were already the
  delegation to `wordpress-egress` (`wordpress_egress.ensure`, a fast path
  in `cli.main`; the Mac passes `--bin-dir "$BIN_DIR"`, both `--wp-port
  "$ONIONPRESS_WP_PORT"`); what moved is the `$(…) || rc=$?` capture and
  the two `log` lines, now `cli._log_wordpress_egress_verdict` over
  `_launcher_log_func` (`[ts] msg` on stdout), worded per platform as each
  bash body worded them (`detect_os()`: the Mac's `✓ … in place: <line>`
  and `WARNING: … this Mac's own services — see <paths.log_file>`, which
  is the launcher's `$LOG_FILE`; Linux's `… this machine and its LAN.`).
  `--check` (nobody's launcher call) still prints the bare status line.
  The bash `${out:-rc=$rc}` fallback became a `try/except` around
  `Docker()` + `ensure` in the fast path: whatever goes wrong is the
  WARNING's words, never a traceback and never a start stopped. The bash
  bodies are the slice 5 shape: `… 2>&1 | tee -a "$LOG_FILE"`, then on the
  Mac `[ "${PIPESTATUS[0]}" -eq 0 ] || START_ERRORS="$START_ERRORS
  wordpress-egress"` (the START SUMMARY's list is bash state the Python
  cannot set), and `return 0` on both (the launchers run under `set -e`;
  a failure is a WARNING, never a stopped start). Linux's status-loop
  re-apply (`rootless_netns` changed) calls the same function and needs
  nothing new. One delta from the bash: the Python's stderr used to go to
  the log file only (`2>>"$LOG_FILE"`); now it is tee'd to the terminal
  too, as slices 3–5 do. Tests (`tests/test_wordpress_egress.py`):
  `TestLaunchers.test_the_helpers_call_the_shared_cli_and_never_stop_the_start`
  now holds the delegation shape (tee, `return 0`, no `log` call or
  WARNING words in the body, the Mac's PIPESTATUS/START_ERRORS line,
  none on Linux); `test_the_cli_logs_each_launchers_own_verdict_line`
  (both platforms' four lines, `cli.detect_os` patched — the helper must
  use `cli`'s module-level `detect_os`, a local re-import dodged the
  patch), `test_check_prints_the_bare_status_line`,
  `test_whatever_goes_wrong_is_the_warning_never_a_traceback`
  (`cli.Docker` raising); and `TestLauncherDelegationEndToEnd` runs each
  launcher's function cut out under bash with `bundled_python`/`op_py` on
  `sys.executable` and a `docker` stub on PATH (`info` answers
  `["name=seccomp"]`, `run` prints `ok`: the CLI's system-Docker path, so
  skipped on a Mac; the Linux launcher's case also skipped where
  `Docker()` would take a bundle's docker over the stub, like slice 5's
  harness — the Mac launcher's `--bin-dir` points `Docker()` at the stub's
  folder, so that case always runs): the one verdict line is on stdout
  and in `$LOG_FILE`, the function returns 0 both ways, and only the Mac
  with `docker info` failing has ` wordpress-egress` in `START_ERRORS`.
  The dispatch census rows were already right (the row's Mock has
  `line`); the `docker run` census has no new site (the helper's
  `docker run` was the Python's already). CLAUDE.md's "Nor any
  connection (issue #144)" bullet now says the bash function is the
  delegation.

## Next step

The slice table is complete: slices 1–7 are on `phase4`, every launcher
body in it a one-line (or, where a status must come back, two-line)
delegation to `onionpress.cli`, each with a Python unit test against a
fake docker, its dispatch-census row and, where it runs a container, its
`docker run` census row. The branch is the hand-over: ready for a PR
against `main` of `brewsterkahle/onionpress` once Ivar wants one (none
opened). Before opening it, run both suites once more at the tip (one at
a time) and note the pre-existing local failures above (10 in
`test_onionheaven_data_move_hardening` / `test_onionheaven_data_volume`,
2 in `test_wp_cli_as_www_data.TestWebRootOwnerGivesBack`; CI is clean).

Still out of scope, as decided at the start: `start_containers`, `setup`
and the Colima bootstrap stay bash. Each is a different kind of move (a
long orchestration with START SUMMARY state, the first-run flow with its
prompts, and the VM's lifecycle, which the menubar's `onionpress.colima`
already covers in part), so each would want its own plan: the same slice
rules apply (log lines and exit behaviour kept, a fake-docker unit test,
the census rows, CLAUDE.md), with `START_ERRORS` the first thing to
design — slice 7 left it in bash because the Python cannot set it, and
`start_containers` is where it is read.

Notes carried over from slice 7 that a later slice may want: the
`try/except` around a fast path's work is the right way to keep a bash
`${out:-rc=$rc}` fallback (the launchers' terminal and log never see a
traceback); and a helper in `cli.py` that asks `detect_os()` must use the
module-level import, so the dispatch census's `os_name` and a test's
`mock.patch.object(cli, "detect_os", …)` reach it.

## Phase 4b (#442): start_containers and setup

Issue https://github.com/brewsterkahle/onionpress/issues/442 moves the
two pieces phase 4 left in bash, one sub-issue per slice (#443–#453), on
branch `cli-first-start-setup-442`. Same rules as the slices above. The
Colima bootstrap and the Linux install/systemd code stay bash.

### The start traces (#443)

`tests/test_start_traces.py` runs each launcher's `start_containers`
(`tests/start_trace.py`: `launcher_harness.Launcher`, so the real top
level, with `main` replaced by one `start_containers` call under `set -e`)
against a stateful fake docker (`tests/start_trace_docker.py`: volumes,
in-use and stuck volumes, compose failures by count, WordPress installed,
plugins, the published port) and records, in order: every `log` line,
docker call (compose files by base name; `[BIND=…]` on Linux's `compose
up`), Python step and stubbed function; then the detached steps, the
status, START_ERRORS (Mac), the markers left and the volumes.
`bundled_python`/`op_py` are `tests/start_trace_runner.py`: the start
steps already in Python run for real in it (OS forced to the launcher's,
no app bundle, `time.sleep` a no-op, `wordpress_egress.ensure` answering
"ok"), the WordPress-side ones (`provision-post-install`, `configure-*`,
…) are recorded as `py …`. When a step moves into the Python start, the
runner patches the same seam (`start.run_cli_step`,
`start.run_detached_cli`, `start.generate_vanity_on_host`,
`start.configure_site_timezones`) so the recorded line stays the same.
Fixtures: `tests/fixtures/start-traces/<platform>/<scenario>.trace`;
`OP_UPDATE_START_TRACES=1` rewrites them, and a slice that changes one
says why in its commit. About 100 s for the 55 runs.

### The START SUMMARY contract (#444)

In `src/onionpress/start.py`'s docstring: a Python start step appends the
names of the steps it could not do to `--errors-file` (one per line,
`^[a-z0-9]+(-[a-z0-9]+)*$`, `StartErrors`), the Mac reads them into
START_ERRORS with `note_start_errors` (a name of any other shape is
dropped) and removes the file; the exit status only says stop / carry on.
`wordpress-egress --errors-file` exits 0 for a block not in place and
writes `wordpress-egress` (without the flag its status is as before: the
Linux status loop and `--check` read it). Linux gets no START SUMMARY:
its launcher passes no file, and the WARNING/ERROR lines are the record.
`tests/test_start_errors_contract.py` runs the Mac's reader and summary
under /bin/bash.

### The bundled plugins (#445)

`onionpress.bundled_plugins`: `install_bundled_plugins` (the Link Fixer,
then Cache Enabler, each unless `INSTALL_*=no`: `install_plugin.sh
--pinned-version`, the installed header's `Version:` read as www-data, the
download into a temp folder, `put-wp-tree`) and `setup_bundled_plugins`
(`wp core is-installed`, then per wanted and present plugin
`configure-*` → `activate_plugin.sh … --network|--each-site` →
`apply-cache-policy`), with the bash's log words. The two scripts stay
(they hold the pins and the activation scopes); the onionpress steps run
through `start.run_cli_step` (a subprocess of this interpreter, isolated
as `op_py` / `bundled_python` are, output appended to the log), the
scripts in docker's environment. Fast paths `install-bundled-plugins` and
`setup-bundled-plugins` (`--scripts-dir`), always exit 0; the bash
functions are `… 2>&1 | tee -a "$LOG_FILE"; return 0`, and the six helpers
(`plugin_wanted` … `install_cache_enabler`) are gone. Delta: put-wp-tree's
stdout now goes to the log, not the terminal. Traces unchanged.

### The pending key import (#446)

`ContainerManager.apply_pending_key_import` (`containers.IMPORT_KEY_PENDING`,
`TOR_STATE_VOLUME`, `OLD_TOR_KEYS_VOLUME`): the bash block, step for step,
including the `docker rm -f` with no ids when `ps` finds none; docker's own
output goes to the log file only (`_to_log_file`, the bash `>> "$LOG_FILE"
2>&1`), so the trace and the terminal stay as they were. The "—" / "--"
of the two launchers' lines follows `platform.detect_os()`. Fast path
`apply-pending-key-import` (exit 1 stops the start); the bash function is
the slice-5 shape (`tee`, `return "${PIPESTATUS[0]}"`) and
`start_containers` runs it every start (it returns at once without the
marker). The marker is only a regular file (`isfile`, as `[ -f ]`).
`tests/test_install_invariants.py`'s bash harness now runs the CLI
through `tests/cli_on_stub_docker.py` (no app bundle, so `Docker()` takes
the stub on PATH, never an installed OnionPress.app's docker) with
`DOCKER_HOST` at the no-docker socket and HOME at its scratch folder: the
two migration end-to-end tests no longer skip on a Mac with the app.
Traces unchanged.

### The first-run key seed (#447)

`start.KeySeed(manager, mac=…, bin_dir=…)`, fast path `seed-onion-key`
(`--bin-dir` on the Mac, for mkp224o), bash `seed_onion_key` in the
slice-5 shape. `volume_state(KEY_VOLUME)` 2 stops, 1 is a first run.
Platform differences, as settled:
- Generation: the Mac's `generate_vanity_address` is
  `start.generate_vanity_on_host` (bundled mkp224o on the host, its
  ERROR lines, the quarantine strip, stale empty folders removed, then
  `start.ensure_key_formats` = `key_manager.ensure_key_formats`
  in-process); Linux still runs `generate-vanity` in the tor image
  (`run_cli_step`). The bash generator is gone.
- Prefix: the Mac uses `config.validate_address_prefix` and logs its first
  line (`ERROR: ADDRESS_PREFIX 'x' is invalid: …`) where the bash copy
  said "too long (6 chars, max 5)"; the copy and its tests are gone
  (`test_address_prefix.TestFirstRunUsesTheValidator`). Linux dropped its
  `2 <= len <= 6` gate: generate-vanity validates.
- Linux keeps re-syncing an imported key every start, the C Tor seed into
  `onionpress-tor-state` and its format sync (in-process now, where the
  bash ran `key_manager.py`); the Mac keeps neither (the entrypoint
  re-derives C Tor's files from the PEM, #311). A failed `volume create` on
  a Linux first run now stops the start, as the Mac's always did.
- One seed: `containers.run_key_seed(docker, …)` (`KEY_SEED_SCRIPT`) for
  both starts and `cmd_restore`; `run_tor_state_seed` (`TOR_STATE_SEED_SCRIPT`,
  byte for byte the bash's) for Linux. The `docker run` census lost the
  three bash rows and the restore's and gained these two.
Trace deltas, on purpose: the Mac bad-prefix line, and Linux's
`bin python3 …key_manager.py ensure-formats` became `fn ensure_key_formats`.
Two fixtures (`plugins-off`, `update-on-launch`) were re-recorded against
the bash first: the scenario's config had not applied (fe3636fc).

### The core services (#448)

`start.start_core(manager, mac=…, wp_port=…, errors=…)`, fast path
`start-core` (`--docker-dir`, `--bin-dir`, `--wp-port`, `--errors-file`;
exit 0 installed, 3 not, 1 stop), bash `start_core_services` + a `case`
on its status in `start_containers`. It pulls `--policy missing` (gated on
`using_local_images`; Linux with the Cloudflare file under a token), then
`ContainerManager.start_core(launcher=…, egress=…)`: the launcher's words,
compose output to the log (Mac) or streamed to stdout (Linux, through the
new `Docker.run_to`, so the docker-argv census did not grow), the egress
step `start.egress_step` (the launcher's verdict, `wordpress-egress` in
START_ERRORS on the Mac), retire, the network move, `compose up` × 3;
then the host-side `curl` wait for the port (60 s, the Mac's WARNING) and
`wp core is-installed` × 15. (`update_images`, UPDATE_ON_LAUNCH's refresh,
first stayed bash here; it is gone since, see "No image refresh" below.)
Linux's WordPress address (#464) reaches compose as before, in the
environment: `ONIONPRESS_BIND_HOST="$(wordpress_bind_host_for_start)" op_py
… start-core` (no Python code names the variable;
`test_linux_bind_scope`). Gone: the Mac's `block_wordpress_egress`, both
launchers' `retire_old_onionheaven_containers` and `migrate_tor_network`
(Linux keeps `block_wordpress_egress` for its status loop). The ordering
tests live in `tests/test_start_core.py`. Traces unchanged (the harness
now has empty compose files in its docker folder, as an install has).

### After the core is up (#449)

`start.post_up(manager, mac=…, installed=…, errors=…, themes_dir=…,
plugins_dir=…, scripts_dir=…)`, fast path `start-post-up`
(`--wordpress-installed yes|no`, always 0), bash `start_post_up
"$wp_installed"`. In the bash order: Mac `.htaccess` repair (written as
www-data from `multisite.HTACCESS_BODY`, which the heredoc matched byte for
byte; the heredoc is gone), Tor (`tor` in START_ERRORS on the Mac), Linux's
SOCKS check, `install_bundled_plugins`, the Mac's
install-from-backup import, `deactivate-wp-statistics` and
`provision-post-install` (`run_cli_step`; output to the log on the Mac, to
the terminal on Linux), `setup_bundled_plugins`, the staging cleanup, the
detached `regenerate-derivatives` (`run_detached_cli`: a session of its
own, or `systemd-run --user` from the Settings-page watcher) and the Mac's
detached `ensure-archive-s3-keys`, `configure_site_timezones`. Settled:
- Tor now starts only when `key_volume_not_ready()` says nothing
  (as `ContainerManager.start_tor`): one more `volume ls` in every trace.
- Linux's `deactivate-wp-statistics` / `provision-post-install` failures
  are WARNINGs: under `set -e` the bare bash calls ended the whole start
  (the `steps-fail` trace had `rc=1`). Both platforms carry on now.
- Linux's install-state steps (`after_wordpress_installed`, the port and
  heartbeat hold, #464) stay bash, before the call: they name
  `ONIONPRESS_BIND_HOST`, which no Python may (`test_linux_bind_scope`).
  `note_if_lan_looks_local` stays bash too, now before Tor; the OnionHeaven
  activation watcher (a bash background loop) after the call: the Linux
  traces moved its line to the end.
- The Cloudflare tunnel is not in `post_up`. Upstream's #500 (merged
  2026-10-30) made it one bash `start_cloudflare_tunnel`, test-held
  identical in both launchers (`tests/test_cloudflare_tunnel.py`): only
  with a token and an installed WordPress, `up -d --no-deps cloudflared`
  with both compose files, last in a start and at the end of every setup
  path (the `provision-post-install` and `setup` cases); otherwise the
  container is removed. Each launcher's `start_containers` calls it after
  the Python start with the `wp_installed` its exit status gave (the Mac:
  0 installed, 3 not), so the Python names neither `cloudflared` nor the
  container. The branch's interim `start.cloudflare_tunnel` (each
  platform's old behaviour) went with that merge; the Mac traces lost
  their `docker stop onionpress-cloudflared` for the shared function's
  `container inspect`, and the token scenarios were re-recorded.
Gone: the Mac's `configure_site_timezones` and `.htaccess` heredoc, both
launchers' `regenerate_derivatives_pending` and `install_bundled_plugins`,
Linux's `start_cloudflare_tunnel`, `warn_if_socks_port_exposed`,
`run_detached` and `OP_PY`. Their behavioural tests moved to
`tests/test_start_post_up.py`.

### One delegation, one start path (#450)

`start.start_containers` (fast path `start-containers`: exit 1 stop, 0
installed, 3 not) runs `apply_pending_key_import` → `migrate_key_volume`
→ `KeySeed` → `start_core` → `post_up`. The Mac's `start_containers` is
the Docker wait, `ensure_vm_fsck_repair` and that one
call (errors file → START_ERRORS); Linux's is the wait,
`start-containers --until-core` (with `ONIONPRESS_BIND_HOST` in its
environment), its install-state steps and LAN note, `start-post-up`, and
the activation watcher. Settled:
- Linux cannot be one call: the install-state steps name
  `ONIONPRESS_BIND_HOST` (no Python may, `test_linux_bind_scope`) and
  must run between the core and Tor (the heartbeat hold before Tor).
- The thinner Python start (`OnionPressCLI.cmd_start` / `cmd_restart`: no
  migration, seed or provisioning) is gone; #405 removed it upstream, and
  this branch's interim launcher wrappers went with the merge.
- The intermediate subcommands (`apply-pending-key-import`,
  `seed-onion-key`, `start-core`) and their bash functions are gone; the
  Python functions keep their unit tests (`test_pending_key_import`,
  `test_first_run_key_seed`, `test_start_core`), the order is
  `tests/test_start_sequence.py`.

### The setup half (#451–#453)

`onionpress setup` (`cli._run_setup`, no OnionPressCLI) runs
`setup_logic.setup_from_terminal(data_dir, mac, launcher_bin, docker,
manager, user, title, from_backup, ia_account, log)`; exit `SETUP_DONE` 0,
`SETUP_FAILED` 1, `SETUP_ALREADY_INSTALLED` 2, `SETUP_RESTART` 10. Both
launchers' `setup)` case: `setup_db_passwords`, `run_setup "$@"` (moves
`--pass`, `--ia-password` and `--from-backup`'s password into
`ONIONPRESS_SETUP_PASSWORD` / `ONIONPRESS_IA_PASSWORD` /
`ONIONPRESS_BACKUP_PASSWORD`; the CLI pops them once read), then on 10
`stop_containers; start_containers; wait_for_services` (Linux also
`start_heartbeat_client`), Linux's `write_status`, the closing lines.
- #451 `stage_setup_from_backup`: `backup.extract_backup` into a 0700
  temp folder (always removed), `backup.staged_backup_address` (the key's
  address; refused before anything is copied), `restore_backup_config`,
  the key files into `vanity-keys/<address>/`. No key: this install keeps
  its own. Not `prepare_install_from_backup`: that stages a full import
  only the Mac's start performs.
- #452 `ask_setup_answers` (username = an onionname, password ≥ 6 chars
  typed twice through getpass, title defaulting to `default_site_title()`)
  and `install_fresh_wordpress`, so the terminal installs as the setup
  window does (single-site install, then the launcher's
  `provision-post-install` converts to multisite and, on Linux, moves the
  port and releases the heartbeat hold; the user's subsite; the bundled
  plugins; the shared archive.org keys in the background). Linux's
  `--ia-account` login is `multisite.fetch_archive_s3_keys` + `wp option
  update` on stdin (`save_archive_login`).
- #453 decided: both platforms now provision, set up the plugins and
  fetch the archive.org keys at setup (install_fresh_wordpress does); both
  check "WordPress running" with `wp --info` (Linux's hint keeps its
  `systemctl` line); both write `.setup_complete` for an installed site.
  `--from-backup` installs with `admin` / the backup's password, then
  (with a key) asks the hubs to release the current one and flags the
  import; the launcher restarts either way. Without a backup, Tor starts
  if it is not running, behind `migrate_key_volume` and
  `key_volume_not_ready`.
Gone from both launchers: `setup_wordpress_cli` / `_interactive`,
`note_wordpress_installed_by_onionpress`, `wp_set_user_password`,
`wp_cli_in`, Linux's `fetch_archive_s3_keys`. Deltas: `--user` must be a
valid onionname and the password 6+ characters (the GUI's rules);
`--email` is accepted and ignored (install_fresh_wordpress uses
admin@onionpress.local).

### No image refresh; the start prunes superseded images (review of #499)

Decided in review: images change only with a release. With every image
pinned by digest, `UPDATE_ON_LAUNCH` (both launchers' `update_images`, the
Mac's `restart` too), `onionpress update-images` and the image pass of the
menubar's Check for Updates could only re-pull the same digests, and the
last one told the owner images were "up to date" as if they updated apart
from OnionPress. All gone: the key, its UI (Mac settings window,
`settings.html`, the WordPress Settings page), defaults and validators,
the verb, the subcommand, `ContainerManager.pull_images*`. An old config
line is ignored. The launchers keep one pin each (`KEY_HELPER_IMAGE`), so
both stay refresher consumers; the third-party pins live in the compose
files only. `tests/test_no_image_refresh.py` replaces
`test_launch_pulls_once.py`; the settings tests that used the key as a
sample yes/no key use `SHARE_ANALYTICS_WITH_ONIONHOME`.

`start.prune_superseded_images`, last in `post_up` (both platforms), took
over #230's prune: for each image `compose config --images` lists (the
Cloudflare file included), once `image inspect` finds the pinned digest,
`image ls <repo>` and a plain `image rm` of each other untagged image.
Untagged only: OnionPress pulls by digest, so a user's tagged image on a
shared Linux Docker is never touched; no global `image prune`. The
`update-on-launch` trace scenario became `images-superseded`; every other
trace gained the one `config --images` line. What a first run does when a
pin has left its registry is #501.

Upstream #502 (merged in) moved the four third-party pins to mirrors under
ghcr.io/brewsterkahle with the same digests. The prune also cleans each
mirror's former repository (`start.FORMER_REPOSITORIES`, test-held equal
to `build/image-mirrors.txt`): an upgraded install's Docker Hub copies of
older versions go, while the same digest under the old name is the pinned
image itself and stays. The launchers' conflicting hunks were #502's new
pins in `update_images`, which this branch removed.


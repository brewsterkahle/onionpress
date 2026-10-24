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

Out of scope for now: `start_containers`, `setup`, the Colima bootstrap.

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

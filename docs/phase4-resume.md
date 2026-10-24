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
| 6 | `db_import` | todo |
| 7 | `block_wordpress_egress` | todo |

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

## Next step

Slice 6: `db_import` (`linux/onionpress` line 639 at this commit; the Mac
launcher has no bash body: its restores go through
`backup.restore_container_artifacts`, whose import is the same client
flags). `tests/test_db_import_sandbox.py` holds the bash function and the
Python to `--sandbox --binary-mode --local-infile=0`, the dump on stdin
after the password line, and the `--sandbox` capability check; the Linux
`restore_backup` calls it as `db_import "$db_pass" "$sql_file"`. The move:
a Python entry (probably a `db-import` subcommand taking the password on
stdin's first line, or in the environment, never argv:
`tests/test_secrets_off_argv.py`) that `backup.restore_container_artifacts`
and the Linux launcher share, the bash body a delegation that keeps the
`"$db_pass" "$sql_file"` call shape (or feeds the file on stdin), the
sandbox test rewritten against the one Python implementation, and the
dispatch row. The timeout must stay sized to the dump
(`wp_files.sized_timeout`, issue #317). Then slice 7
(`block_wordpress_egress`), already a delegation to `wordpress-egress`
in both launchers (`tests/test_wordpress_egress.TestLaunchers`): check
whether anything bash is left to move, and if not record it as done.

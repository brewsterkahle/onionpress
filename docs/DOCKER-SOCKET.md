# The Docker socket

Access to the Docker socket is root on the Docker host: the Colima VM on
macOS, the user's own account under rootless Docker on Linux. So the socket
goes only where it is needed, never into a container that serves Tor
traffic, and `tests/test_docker_socket_scope.py` fails if that changes.

## Who has it

| Container | Socket | Uses Docker for | Faces |
|---|---|---|---|
| `onionpress-tor` | **no** | — (see below) | Tor: the site, the healthcheck service, the OnionHeaven API on 8083 |
| `onionheaven` | yes | the heartbeat: `docker run`/`rm` takeover workers, `docker exec` their queue managers and Tor control ports, `docker inspect` them; `reset_onionheaven` adds `ps`, `cp` and exec into itself | no onion service; outbound Tor; 8082/9050 on `onionpress-network` |
| `onionheaven-takeover-N` | no | — | Tor (the redirect for taken-over addresses) |
| `autoheal` | **no** | restarts unhealthy `autoheal=true` containers, via `autoheal-proxy` | nothing (`network_mode: none`) |
| `autoheal-proxy` | yes, read-only mount | passes `GET /containers/json` and `POST /containers/<id>/restart`, refuses the rest | nothing (`network_mode: none`), a unix socket on the `onionpress-autoheal-proxy` volume |

Every Docker call `onionpress-tor` used to make came from `web-server.py`
through `onionheaven_common`, and all of it was OnionHeaven hub work. On a
node nobody registers with, none of it ever ran.

## How onionpress-tor works without it

`onionheaven_common.docker_available()` is false there, and the shared code
records the work in the registry (on the `onionpress-onionheaven-data`
volume, which both containers mount; see "The OnionHeaven hub data" below)
for a process that has Docker:

- **`/offline`** — `takeover_function` runs its guards, then sets
  `takeover_requested` instead of picking a worker. The heartbeat's next pass
  (every 15–20 s, plus the pass itself) sees it (`takeover_requested_now`: no `/online` since)
  and calls `takeover_function(force=True)` itself, once. If no worker is
  ready, the staleness check takes over later — as before, when a deferred
  `/offline` takeover also waited for it.
- **`/online`, `/unregister`** — `release_function` marks the row online and
  sets `release_pending` in the same UPDATE. The worker holding the address
  already polls for that every 2 s (`process_releases` in
  `onionheaven-takeover-worker.py`) and runs the `DEL_ONION`. The heartbeat's
  RECONCILE pass removes the address from any other worker (the old "safety
  sweep"), and `fix_assigned_count_drift` corrects the `assigned_count` this
  path does not count down. One case is slower than before: an address still
  waiting in the worker's queue manager (a takeover burst) is added after the
  release, and RECONCILE removes it on the next pass.
- **`/status`, `/status/<addr>`** — no per-worker queue counts, and
  `serving_status` reads `activating` for an assigned takeover. Neither spawns
  a `docker` per request any more.
- **`/reset-onionheaven`** — answers 503. `onionpress reset-onionheaven`
  (Linux) runs `reset_onionheaven()` in the `onionheaven` container instead.

With an older compose file that still mounts the socket into `onionpress-tor`,
the same code takes the old synchronous path.

**Release rule:** this is compose plus tor-image code. Publish the tor image
built from the change and refresh the pins before releasing the app and the
`.deb`. The new compose with an old image still works but degrades on a hub:
releases wait for RECONCILE (two per worker per pass), `/offline` waits for
the 180 s staleness check, and `assigned_count` drifts upward.

## The OnionHeaven hub data

The hub's registry (`registry.db`) and the onion service key of every site
registered with it (`keys/<address>/`) live on their own volume,
`onionpress-onionheaven-data`, mounted at `/var/lib/onionheaven`:

| Container | `onionpress-onionheaven-data` | `onionpress-persistent-data` |
|---|---|---|
| `onionpress-wordpress` | **no** | yes (status, settings-page actions) |
| `onionpress-tor` | yes (registration API) | yes |
| `onionheaven` | yes (heartbeat, takeovers) | yes |
| `onionheaven-takeover-N` | yes (keys for `ADD_ONION`, registry) | **no** |

Both used to be `onionheaven/` on `onionpress-persistent-data`, which the
WordPress container mounts read-write, so file permissions inside that
container were all that kept the site's PHP away from every registrant's key.
The new mount point is outside `/var/lib/onionpress` on purpose: a volume
mounted inside a directory WordPress can write is only as separate as that
directory entry. Host-side code reaches the data through the `onionheaven`
or `onionpress-tor` container, never `onionpress-wordpress`.
`tests/test_onionheaven_data_volume.py` enforces all of this.

**Moving existing data.** In `onionpress-tor` and `onionheaven` the
entrypoint runs `onionheaven-data-migrate.py` before anything opens the
registry, on every start; a lock on the volume makes the second container
wait for the first. Nothing moves unless both `/var/lib/onionheaven` and
`/var/lib/onionpress` are mount points.

- Only what older versions wrote there moves: `registry.db` (with its
  `-wal` or `-journal`), `keys/<address>.onion/` with its key files and
  `hostname`, and the hub's logs and flag files. WordPress owns the folder
  the old directory is in and can replace it, so the directory is walked
  through directory descriptors, never following a link and never deeper
  than that layout. Nothing else there (other names, deeper trees, special
  files, a file over its size limit or with holes in it) is ever copied.
- The registry is never opened where it is: root's SQLite would write
  through a link planted beside it as its `-shm`. The database and its
  write-ahead log are copied by descriptor, with the log's header unchanged
  around the copy, so they are one state even while an older container
  still writes to them, and SQLite makes one database file of that copy on
  the volume (`Connection.backup`). A registry that is no sound SQLite
  database moves as it is.
- Everything copied is root's, folders `0700`, files `0600`. The copy is
  verified and synced to disk, put in place (a log or journal before its
  database), `.migrated-from-persistent-data` is written, and only then is
  each old file removed that still has the bytes copied: the registry's
  files all together or none. A file that changed stays at the old
  location, and the next run (the other container's, seconds later, or the
  next start's) keeps it aside.
- What was already on the volume before that first move is kept in
  `.pre-migration-<time>/`. What turns up at the old location later is kept
  in `legacy-leftover-<time>/` and not used, if it fits beside a reserve of
  free space; everything else there is removed. A registry written there
  after the move gets a loud warning in the log: an older OnionPress used
  the old location since.
- Kept copies hold registrants' private keys, so they do not stay: a key
  the volume holds with the same bytes is dropped from them, unregistering
  an address drops its key from them (`purge_kept_keys`), and each copy is
  removed 30 days after it was made.
- If the first move fails while registrants' keys are at stake (a non-empty
  key file in `keys/<address>.onion/`), the container exits without
  removing anything and Docker restarts it. With no keys there it starts
  anyway and marks the volume, so a later start never sets aside what the
  hub writes there meanwhile. Once the data in use has moved it always
  starts: WordPress can write to the old location, and must not be able to
  keep the hub from starting.

Containers an older version started (takeover workers, and an
`onionpress-tor` or `onionheaven` from the older image) mount the old volume
and would go on using the old location. Before compose starts anything,
every start (`retire_old_onionheaven_containers` in both launchers,
`ContainerManager.start_core`) removes each of them that does not mount
`onionpress-onionheaven-data`; compose recreates the two services, and the
farm, when stopped, goes all at once with a second's grace. The heartbeat
removes any it finds later (`cleanup_dead_workers`, whether the registry
lists them or not) and reassigns their addresses to workers it starts with
the new volume.

**What the socket is aimed at.** The heartbeat reads the names of its
workers back from the registry, and `onionpress-tor` and every takeover
worker can write the registry as root. So a name from there gets a docker
call (exec, inspect, cp, rm) only if it is a worker's name, whole
(`onionheaven-takeover-N`, or `onionheaven-poll-N` from an older version),
and an address handed to a worker only if it is one v3 onion address; a
row naming anything else is dropped on the next pass and its addresses are
reassigned. `tests/test_hub_registry_names.py` lists every docker call in
the image and where its arguments come from.

**Backups.** A backup holds a hub's data when the volume has any (a key
directory, or a row in the registry; `backup.hub_data_state`): the registry
as `sqlite3 .backup` copies it, one consistent state while the hub writes,
and `keys/`, read out of `onionheaven` as plain files, nothing else from the
volume (`stage_hub_data`; Linux runs it as `onionpress.cli stage-hub-data`).
A restore puts the registry back through SQLite's `.restore`, never as a
copy over the database in use, whose `-wal` SQLite would apply to it, and
the keys as root's (`restore_hub_data`, all three restore paths). Until this
release Mac backups held no hub data at all (they waited for a
`master-key.json` nothing writes any more).

None of it goes through WordPress. The Settings page's backup is handed to
the WordPress container for download, under the admin password WordPress
saw: it leaves the hub data out (`create_backup(include_hub_data=False)`,
Linux `create_backup --without-hub-data`), records
`excludes_onionheaven_data` in `metadata.json` and tells the owner. A hub is
backed up from the app (Backup…) or with `onionpress backup`. The Settings
page's restore refuses a backup holding registrants' keys before touching
anything, and never writes this volume; restore such a backup from the app
or with `onionpress restore`.

**Release rule:** compose plus tor-image code again. Publish the tor image,
refresh the pins, then release the app and the `.deb`. A new compose file with
an old image moves nothing (the old image keeps using the old location), but
the hub's status counts and the backups' hub data come up empty until the
image arrives. An old compose file with a new image must never ship: the
data stays put, but the hub would run on an empty registry in the container's
writable layer.

**Never downgrade a hub across this release.** An older image reads and
writes the old location again, which the move has emptied: it runs on an
empty registry, and what it records there is kept aside, not used, by the
next start of a newer one (with the warning above). Registrations made in
between then exist only in that `legacy-leftover-<time>/registry.db`.

On the hub itself:

1. Before updating, count what there is: `docker exec onionheaven sqlite3
   /var/lib/onionpress/onionheaven/registry.db 'SELECT COUNT(*) FROM
   registry WHERE unregistered_at IS NULL'` and `docker exec onionheaven sh
   -c 'ls /var/lib/onionpress/onionheaven/keys | wc -l'`. Keep a copy of the
   old directory too, since a backup made by an older OnionPress does not
   hold it: `docker exec onionheaven tar -C /var/lib/onionpress -cf -
   onionheaven > onionheaven-before-update.tar`. It holds every registrant's
   private key: keep it encrypted and offline, and delete it once the
   counts below agree.
2. Stop OnionPress completely, which also removes the takeover workers;
   `docker ps -a` then lists no `onionheaven-takeover-*` or `onionpress-tor`.
3. Update, start, and look for the "moved the OnionHeaven data" line in the
   logs of `onionheaven` (or `onionpress-tor`, whichever started first),
   and for no `WARNING` or `ERROR` line from `onionheaven-data-migrate`.
4. Count again on the new location (`/var/lib/onionheaven/registry.db`,
   `/var/lib/onionheaven/keys`) and compare.

## The OnionHome name registry

OnionHome's onionname registry (`onionnames.db`: which onionname points at
which onion address, for every lookup and every follow by name) lives on its
own volume too, `onionpress-onionnames-data`, mounted at `/var/lib/onionnames`
in one container:

| Container | `onionpress-onionnames-data` |
|---|---|
| `onionpress-tor` | yes (`web-server.py`'s `/api/name/*`, its only reader and writer) |
| `onionpress-wordpress` | **no** (the directory plugin looks names up through `/api/name/lookup`) |
| `onionheaven`, `onionheaven-takeover-N` | no |

It used to be `onionhome/` on `onionpress-persistent-data`, which WordPress
mounts read-write (and `fix_onionpress_permissions` handed that directory to
www-data): code running in WordPress on OnionHome could point any name at an
onion address of its choosing, or delete the registry. Host-side code reaches
it through `onionpress-tor`; backup and restore copy `onionnames.db` and its
`-wal`, nothing else, and the archive still calls the folder `onionhome/`.
`tests/test_onionhome_registry_volume.py` enforces all of this. The Settings
page's backups keep the registry (nothing in it is secret), but its restore
refuses a backup carrying it and never writes this volume: code running in
WordPress can make that upload, and would point names wherever it liked.
OnionHome is restored from the app or with `onionpress restore`.

**Moving existing data.** `onionpress-tor`'s entrypoint runs
`onionhome-registry-migrate.py` before `web-server.py` starts, on every node
(anywhere but OnionHome there is nothing to move, and the volume is just
marked). Everything at the old location may be WordPress's work, so it reads
it as untrusted:

- Nothing there is walked: only `onionnames.db`, `-wal` and `-journal`,
  through `O_NOFOLLOW` descriptors below the old directory (itself opened
  without following a link), each a regular file of at most 256 MiB that is
  not sparse; the database must start with SQLite's header.
- SQLite never opens a path there. The files are copied into a private,
  root-only folder on the new volume until the copy is stable (no legitimate
  writer is left: the old `onionpress-tor` is the container being replaced).
- The new registry is a new database with `onionnames.py`'s schema holding
  only rows that pass its checks (name, v3 address, timestamps), read from
  the private copy under SQLite's defensive settings after `quick_check`. It
  is renamed into place after any stale `-wal`, `-journal` or `-shm` on the
  volume is removed; the private copy is always deleted, nothing is kept
  aside, and the copied files leave the old location if still unchanged.
- The marker (`.migrated-from-persistent-data`) is written whenever the
  container is to start on the new volume: after a move, with no registry at
  the old location (or only something that is not one: no `onionnames`
  table), or when the volume already holds one (a restore, or an interrupted
  run). Only when both `/var/lib/onionnames` and `/var/lib/onionpress` are
  mount points. Once it exists the old location is never read again.
- Every start makes the volume `root:root`, 0700, and its files 0600.

`web-server.py` serves no name until that marker is on the volume
(`onionnames.registry_in_place`): without it `/var/lib/onionnames` is not the
volume, or a registry still waits at the old location, and one opened there
would start empty, every name free for anyone to claim. `/api/name/*`
answers 503 then. So a registry that cannot be moved does not stop the
container: the site stays up, names answer 503, nothing is moved or removed,
no marker is written, and the next start tries again. That covers a registry
that keeps changing, one SQLite finds damaged, and a file carrying SQLite's
header (`onionnames.db`, or its `-wal`/`-journal`) that is too large or too
sparse to read safely: such a file is held, never taken for "not a
registry", because abandoning a real one would serve an empty registry with
every name free to claim. Each hold logs one ERROR line from
onionhome-registry-migrate. Nothing WordPress plants can keep the container
from starting.

When the ERROR appears:
1. Preferred: restore a backup made before the update. The restore puts the
   registry on the new volume; the next start finds it there, marks the
   volume and serves it. (Backups taken during the hold contain no registry.)
2. No backup, and the held file is the real registry: copy the old
   directory out (`docker cp onionpress-tor:/var/lib/onionpress/onionhome
   ./held`), inspect only the copy with `sqlite3 -safe` and `PRAGMA
   trusted_schema=OFF` (`PRAGMA integrity_check; SELECT COUNT(*) FROM
   onionnames;`), compact it (`PRAGMA wal_checkpoint(TRUNCATE); VACUUM;`),
   copy the compacted `onionnames.db` back over the old one, delete the old
   `-wal`, `-shm` and `-journal`, and restart onionpress-tor. Check that the
   "moved … N names, … K invalid rows left out" line gives N+K equal to the
   count you took.
3. Only if the file holds no names worth keeping: move the old directory
   aside (`docker exec onionpress-tor mv /var/lib/onionpress/onionhome
   /var/lib/onionpress/onionhome.aside`) and restart; the volume is marked
   and starts empty, every name free to claim. The old in-container
path `/var/lib/onionhome/onionnames.db` is no longer moved by
`web-server.py` itself; every container recreate wiped it long ago.

**Release rule:** compose plus tor image, as above: publish the tor image,
refresh the pins, then release the app and the `.deb`. A new compose file
with an old image moves nothing (the old image keeps the old location), but
the new app's backups leave the registry out, and a restore writes it where
the old image does not look (and the later move would take that restored
copy over the registry in use); so never release this compose file with pins
to an older image. A new image with an old compose file serves no names
(503) rather than an empty registry. On OnionHome itself: count the names
first (`docker exec onionpress-tor sqlite3
/var/lib/onionpress/onionhome/onionnames.db 'SELECT COUNT(*) FROM
onionnames'`), update, start, and look for "moved the OnionHome name registry
to its own volume: N names, …, K invalid rows left out" in `docker logs
onionpress-tor`: N plus K is the count from before, and the same query on
`/var/lib/onionnames/onionnames.db` gives N. Never downgrade OnionHome after
that: an older image would serve the old location, now empty.

## Next: the heartbeat's socket

What remains is the `onionheaven` container. It hosts no onion service, but
it runs Tor, a shell HTTP responder on 8082 and a SOCKS port reachable from
`onionpress-network`, and parses output from workers that do face Tor. A path
allowlist is not enough here: container create and exec bodies (privileged,
binds, devices, user, command) are what matter, and a path-filtering proxy
such as the one in front of autoheal cannot inspect them.

1. **A farm controller.** A small new service, same image with its own entrypoint
   mode, holding the socket, with `network_mode: none`, listening on a unix
   socket on a dedicated volume that only it and `onionheaven` mount. Fixed
   verbs with validated arguments: `spawn N`, `remove N`, `running N`,
   `list`, and `exec N <verb> [address]` for a fixed set of commands (queue
   manager `takeover`/`release`/`has`/`status`/`addr_state`, Tor bootstrap
   phase, `onions/detached`, worker liveness). `N` is an integer, the address
   must match the v3 pattern, and the worker's shape (image, network, volume,
   ulimit, restart policy, no extra privileges) is built in, not passed.
   The image should be the stack's pinned one passed in from compose, as
   `onionheaven` already receives it (`ONIONHEAVEN_IMAGE`).
   `onionheaven_common`'s `docker` calls become calls to this client, and
   `onionheaven` loses the socket. Compose and image again ship together.
2. **Workers without `docker exec`.** Optional, and better: give each worker's
   queue manager a control socket on a volume shared only with `onionheaven`
   (never `onionpress-persistent-data`, which `wordpress` also mounts). Then the
   controller only spawns, inspects and removes, and `exec` disappears.
3. **Reset without `docker cp`.** `_patch_worker_code` and the heartbeat
   restart via `docker exec onionheaven` are development conveniences;
   recreate workers from the current image and signal the heartbeat inside its
   own container instead.
4. **Smaller surfaces.** Bind `onionheaven`'s 8082 redirect and SOCKS port to
   loopback or drop them (takeovers are served by the workers), move workers
   to a network of their own, and build the tor image without the docker CLI
   where it is not used.
5. **Normal nodes.** Every node accepts registrations by design, so every node
   runs `onionheaven`. If registration becomes hub-only, the controller and
   `onionheaven` could move behind a compose profile that only a hub enables,
   and a normal install would run nothing with the socket except
   `autoheal-proxy`.

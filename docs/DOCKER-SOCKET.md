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
wait for the first. It copies the old directory, verifies the copy and that
the source did not change meanwhile, puts it in place, writes
`.migrated-from-persistent-data`, and only then removes each old file that
still has the bytes it copied. What was already on the volume before that
first move is kept in `.pre-migration-<time>/`; anything that turns up at the
old location later is kept in `legacy-leftover-<time>/` and not used.
Symlinks at the old location are never followed. Nothing moves unless
`/var/lib/onionheaven` is a mount point. If the first move fails while
registrants' keys are at stake, the container exits without removing
anything and Docker restarts it; with no keys there, or once the data in use
has moved, it starts anyway and tries again next time (WordPress can write
to the old location, and must not be able to keep the hub from starting). Takeover workers started before the move mount the old volume;
the heartbeat removes them (`cleanup_dead_workers`) and reassigns their
addresses to workers it starts with the new one.

**Release rule:** compose plus tor-image code again. Publish the tor image,
refresh the pins, then release the app and the `.deb`. A new compose file with
an old image moves nothing (the old image keeps using the old location), but
the hub's status counts and the Linux backup's hub data come up empty until
the image arrives. An old compose file with a new image must never ship: the
data stays put, but the hub would run on an empty registry in the container's
writable layer. On the hub itself, stop OnionPress completely (that also
removes the takeover workers), update, start, and check the logs of
`onionheaven` (or `onionpress-tor`, whichever started first) for the "moved
the OnionHeaven data" line, and the registry count against the one from
before.

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

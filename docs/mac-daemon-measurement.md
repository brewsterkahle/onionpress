# What the post-#369 menubar cost idle (go/no-go for #367)

Measured 2026-10-23 on a throwaway node (HOME under /tmp, port offset
36000, its own Colima VM, `OnionPress.app` built from PR #369's head
6cca947e) before any of this branch's changes. The node had WordPress
installed, Tor bootstrapped, the OnionHeaven heartbeat registered and the
dual probe answering `reachable` (purple), so the status loop ran on its
slowest schedule (one pass every 30 s). Nothing clicked, no browser
attached. The live install in `~/.onionpress` was not involved.

Method: the bundle's `bin/docker` was replaced by a shim that logs each
call with its process ancestry and execs the real binary; `top -l 61 -s 10
-stats pid,command,cpu,csw,th,sysbsd,sysmach,mem -pid <menubar>` and
`ps -o cputime` over the same 10 minutes.

| Idle 10 min, purple                         | Menubar process (pid 61169) |
|---------------------------------------------|-----------------------------|
| CPU time                                    | 1.46 s (0.24 % of one core) |
| `top` %CPU samples                          | 0.0 – 0.2 %                 |
| context switches                            | 9 238 (15.3 /s)             |
| BSD syscalls / Mach calls                   | 38 /s / 26 /s               |
| threads                                     | 16                          |
| RSS                                         | 69 MB                       |
| docker CLI invocations in its process tree  | 291 (29 /min)               |
|   of which direct children                  | 237                         |
|   via the launcher's `status` / `address`   | 36 `compose ps` + `compose exec` |

The docker calls by kind: 82 `exec onionpress-tor` (hostname, bootstrap,
control-port checks, the message poll), 81 `exec -i` (status.json, version,
config-current.json, recent-logs.txt written as www-data: four per pass),
36 `exec -u www-data` (handle-action's reads, the upload trigger), 28 `ps`,
18 `exec onionheaven` (the probe through the second Tor), 18 `compose ps`
and 18 `compose exec` (the launcher's `status` and `address`, one bash +
compose + jq process each: 0.18 s CPU per `status` call), 9 `exec
onionpress-wordpress`, 1 `inspect`.

So the menubar process itself is quiet (a quarter of a percent of a core,
~15 wake-ups a second), but it spawns a docker CLI every two seconds and
two launcher shells a minute, whose own CPU (roughly 36 × 0.18 s ≈ 6.5 s
per 10 min for the launcher calls alone) exceeds the process's. In yellow
(the 5-second schedule) all of that is six times higher.

Verdict: proceed, as asked, and take the easy win on the way: the daemon
reads `docker ps` and the tor container's hostname directly instead of
running the launcher's `status` and `address` (two shells, a compose and a
jq per pass), and the view process reads a file.

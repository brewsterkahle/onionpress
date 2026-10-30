# End-to-end tests (design)

Status: design agreed in outline, 2026-10-24 (decisions in §9). Nothing here is built yet.

The unit suite (`make test-unit`, ~150 files) checks code and text: censuses,
pins, fake iptables, PHP harnesses. What it cannot say is that a fresh
install on a real Mac or Linux box comes up, serves its site over Tor, keeps
its address, backs up and restores, and that the hardening holds on real
containers. Today that evidence comes from hand-run QA (2.5.3's on
2026-10-14 found twelve problems the unit suite had passed). This suite
makes that QA repeatable.

It is slow (hours for the whole thing) and runs on demand. It is **not**
part of releasing: releases come too often for a run of hours, so none is
required, cited or awaited (§9). That is revisited once development slows.

## 1. Ground rules

1. **Never the live install.** Every node is throwaway: a scratch `HOME` with
   its own Colima VM on a Mac, a Lima VM on Linux. The harness refuses to
   start if `DOCKER_HOST` (or the default socket) reaches the user's own
   `~/.onionpress/colima`, if any target answers with the live onion
   address, or if a scratch path resolves under the real `$HOME`. The
   existing `tests/wp_integration.py` liveness guard is reused, not
   reimplemented.
2. **Nothing reaches a public OnionPress service.** OnionHeaven is on by
   default, so a throwaway node that starts without a config would ship its
   key to the canonical hub, and any node that sets a name registers it with
   the real OnionHome. The harness writes every node's
   `~/.onionpress/config` *before* its first start:
   `REGISTER_WITH_ONIONHEAVEN=no`, or `ONIONHEAVEN_ADDRESS=<test hub>`;
   `ONIONHOME_ADDRESS=<test home>`; `SHARE_ANALYTICS_WITH_ONIONHOME=no`
   unless under test. A **tripwire** greps every node's logs and the tor
   container's control-port events at teardown for the canonical hub and
   OnionHome addresses and fails the run if either appears.
3. **No writes to third parties** outside the opt-in external tier (§6 T4):
   no Wayback submissions, no archive.org logins, no Cloudflare tunnels, no
   GitHub releases. Read-only GitHub API calls are allowed.
4. **Tests assert behaviour from outside.** Prefer what a user or visitor
   sees (HTTP over Tor, the launcher's exit code and output, files in
   `~/OnionPress/`) over reading internals. Internals (docker inspect,
   iptables counters, log lines) are used for the security properties,
   where the outside view is "nothing happened".
5. **Every run leaves evidence**: per-node log bundle (launcher, app,
   container logs, `docker inspect`, iptables-save), JUnit XML, and a
   one-page HTML/Markdown summary. Teardown always collects before it
   destroys.

## 2. Topology adapts to the platforms at hand

No run needs both a Mac and a Linux machine. The harness looks at what the
host can provide, builds the topology from that, and says in the report
which platforms were tested and which were not (§3.1).

### Node kinds

A node is one throwaway OnionPress install with its own Docker daemon (the
container names are fixed, so two installs cannot share one daemon).

| Kind | Provided by | How several fit on one host |
|---|---|---|
| `mac` | a macOS host: the build under test (`OnionPress.app` from `make dmg`, or the checkout's launcher) | one scratch `HOME` each (`/tmp/ope2e/mac-N`: short, for the 104-char socket limit), its own Colima VM, `COLIMA_HOME` created before the first start (#370), `ONIONPRESS_PORT_OFFSET` 31000, 41000, … |
| `linux-vm` | Lima on a Mac (vz); Lima + QEMU + KVM on a Linux host | one VM each, no home mount, only the share dir mounted; Debian 13 by default, Ubuntu 24.04 LTS with `--guest ubuntu-24.04` (§9) |
| `linux-user` | a Linux host without nested virtualisation (a CI runner, a Pi) | one throwaway local user each with its own rootless Docker daemon — how two installs coexist on Linux anyway |
| `linux-host` | the Linux host itself, when it is a disposable Debian-based machine | one only |

The Linux target is **Debian-based** (§9): the `.deb`, `install.sh`,
rootless Docker and the systemd user units are the same on Debian, Ubuntu,
Raspberry Pi OS or Mint. The platform is `linux` (the report's column);
which distro ran is a fact of the node (`Debian GNU/Linux 13`,
`Ubuntu 24.04.5 LTS`), so a test marked `platform("linux")` runs on every
guest. A VM guest is Debian 13 unless `--guest ubuntu-24.04` is passed.
`linux-user` and `linux-host` nodes need the host itself to be Debian-based
(`/etc/os-release` `ID=debian`, or `ID_LIKE` containing `debian`); a Fedora
or Arch host is used only to run VMs, and the probe says so rather than
testing another family.

Within the family, the flavour is a node attribute, not a kind: the two
ways OnionPress gets Docker there, and the arch (whatever the host or VM
runs):

| Flavour | What it is | Default |
|---|---|---|
| `deb` | `apt install ./onionpress.deb` on a fresh system: `Depends: docker.io \| docker-ce` pulls in Debian's docker.io, and the postinst sets it up rootless when it can (else the docker group); the report says which | yes |
| `install-sh` | `linux/install.sh`: Docker CE from Docker's repository, rootless, system daemon disabled | with `--linux-flavours all` |

What each host can offer:

| Host | Node kinds available |
|---|---|
| macOS | `mac`, and `linux-vm` when Lima is installed |
| Linux with KVM (`limactl`, `/dev/kvm` read-write, `qemu-system-<arch>`, UEFI firmware) | `linux-vm`; `linux-user` and `linux-host` if Debian-based (and disposable) |
| Linux without KVM (most CI) | `linux-user`, `linux-host` if Debian-based and disposable |

macOS cannot be virtualised on non-Apple hardware, so a Linux-only run
never tests the Mac app; it says so instead of failing.

### Roles

Tests ask for **roles**, not machines: `node` (the install under test),
`peer` (a second site, for following and cross-install restore), `hub`
(`ONIONHEAVEN_ACCEPT_REGISTRATIONS=yes`, with a hub recovery key; other
nodes point `ONIONHEAVEN_ADDRESS` at it), `home` (acting as OnionHome;
needs §5.1, until then images rebuilt with `_ONIONHOME_ADDRESS` patched,
as the 2.5.3 QA did). Any node kind can play any role. The harness fills
the roles from what is available, preferring a mix of platforms when it
has one (e.g. a Mac node registering with a Linux hub), and records the
assignment in the report.

The **visitor** is not a node: HTTP over Tor from inside any node's tor
container (`curl --socks5-hostname` or the socat recipe in CLAUDE.md),
never the Mac host's SOCKS port (Colima gotcha).

A tier declares the roles it needs; the harness brings up only those, and
`--max-nodes` caps how many it creates (each Mac node is a VM: ~2 GB RAM).
A test whose roles cannot be filled is skipped with that reason.

## 3. Harness

```
e2e/                       top-level, so `unittest discover tests` never finds it
  conftest.py              topology fixtures, safety guard, evidence collection
  platforms.py             probe the host: which node kinds it can provide
  nodes/
    base.py                Node: run(cmd), launcher(subcmd), docker(…), wp(…),
                           http_local(path), http_onion(addr, path, via=),
                           config_set(k, v), logs(), wait_until(pred, timeout),
                           facts() (os, version, arch, docker flavour, install method)
    mac.py                 MacNode: scratch HOME, Colima, app bundle / launcher
    linux_vm.py            LinuxVMNode in a Lima VM (--guest debian-13 | ubuntu-24.04)
    linux_user.py          LinuxNode as a throwaway user with rootless Docker
    linux_host.py          LinuxNode on a disposable host (CI runner)
  tor.py                   descriptor/HS_DESC helpers over the control port,
                           reachability with retries and timing
  browser.py               Playwright (WordPress admin pages, extension)
  macui.py                 menubar + native dialogs via accessibility (thin)
  fixtures/                legacy backups, WXR, social exports, malicious zips
  test_t0_stack.py … test_t4_external.py
  run.sh                   entry point: tier/node selection, keep-on-failure
```

- **pytest**, not unittest: session/module fixtures (install once, run many
  checks), markers per tier and node, `-k`, `--junitxml`, `-x`/rerun. It is
  an e2e-only dependency, fetched by `uv run --with pytest --with playwright`
  like `make test-unit` fetches PyNaCl. Make target: `make e2e TIER=t1`.
- **Reuse the product's own helpers** for anything that encodes a rule
  (`wp_files.wp_cli_args`, `backup` validators, `onion_auth` for signed
  requests, `oh-test-helper.py` for faux heartbeats) so a test cannot pass
  by doing it differently from the app.
- **Waits are explicit and budgeted**: `wait_until(pred, timeout, every)`
  logs elapsed time; every Tor-dependent wait records its duration in the
  report, so slow-but-green is visible over time.
- **Keep on failure**: `--keep` leaves the failing topology up for
  inspection; the default tears it down. Every run starts by removing anything
  under `/tmp/ope2e` and Lima instances named `ope2e-*` older than a day.
- **Platform marks**: every test is `@platform("any")` (runs once per
  available platform: a backup/restore test runs on the Mac node and on a
  Linux node if both exist), `@platform("mac")` (menubar, updater swap,
  Colima VM events) or `@platform("linux")` (systemd units, tray,
  `onionpress update`, sleep hook; every Debian-based guest, never one
  distro). Platform-neutral behaviour is written
  once against `Node`; only the setup differs.
- **Stateful ordering**: a module is one scenario on one long-lived node
  (e.g. "install → back up → reset → restore → address unchanged"); tests
  inside it are ordered steps, and a failed step skips the rest of that
  module with a clear reason rather than cascading errors.

### 3.1 What a report says

The report opens with what was tested on, before any result:

```
OnionPress e2e — build 2.5.6 (onionpress.dmg sha256 …, onionpress.deb sha256 …)
Host: macOS 27.0 arm64 · Tor: public network
Tested:     macOS 27.0 arm64 (mac, Colima 0.9)  ·  Linux: Debian 13 arm64 (linux-vm, deb: docker.io)
Not tested: Linux amd64 · Ubuntu 24.04 guest · install-sh flavour (rootless Docker CE)
Roles: node=mac-1  peer=linux-vm-1  hub=linux-vm-1  home=linux-vm-2
```

Then one matrix, a row per test and a column per platform tested:
`pass`, `fail`, `skip (reason)`, or `—` (does not apply: a Linux-only test
in the macOS column). A platform that was not available gets **no column**
and is listed under "Not tested", so a run on one OS can never read as
covering the other. The same facts go into the JUnit XML (as properties
and per-platform test ids, e.g. `t1_backup::roundtrip[mac]`) and into a
`results.json` that later runs can be compared against.

A run passes when every test that ran passed; it says nothing about
platforms it did not have. Comparing several runs (a Mac run and a CI
Linux run of the same build) can be done by merging their `results.json`
into one matrix (`e2e/merge-results.py`).

## 4. Tor network: public vs private

| | Public Tor | Private network (chutney-style) |
|---|---|---|
| Realism | the real thing (HSDirs, intro points, circuit failures) | close, not identical |
| Speed | descriptor publish 1–5 min, reachability flaky | seconds |
| Offline / CI | needs egress to Tor | fully offline |
| Product change | none | torrc override hook (`TestingTorNetwork`, `DirAuthority`) in the tor entrypoint, test-only |

Decided: **public Tor** (§9), with generous retries and timings recorded.
A private network stays a later option if flakiness or duration becomes
the main cost; it is also the only way to test the republish trigger
deterministically (kill intro points on demand), so T1's republish test
checks the request/response path, not a real descriptor outage.

## 5. Product changes the suite needs

Small, test-motivated, each with its own unit test:

1. **OnionHome address from config in the tor image.** `web-server.py`
   hardcodes `_ONIONHOME_ADDRESS`; `config.py` already has
   `ONIONHOME_ADDRESS`. Pass it through compose/env so a test node forwards
   name registrations to the test home on published images.
2. **Update feed override.** `updater.py` hardcodes
   `brewsterkahle/onionpress`; Linux already has `RELEASE_REPO`. A
   `ONIONPRESS_RELEASE_REPO` (or a local URL) lets the updater tests install
   from a fixture release (fork release or a local HTTP server serving a
   DMG/.deb + `SHA256SUMS`). Must not be settable from the Settings page.
3. **Wayback endpoint override** (filter in the mu-plugin) pointing at a
   mock container, so the sweep can be run end to end without submitting
   to the public Wayback Machine.
4. **The setup window's provisioning without the window.** Both launchers'
   `onionpress setup --user … --pass …` install WordPress headlessly (T0
   uses it), but the Mac setup window also creates the owner's subsite and
   onion name (`setup_logic.install_fresh_wordpress`); a CLI path to that
   would let T1 test the Mac first run as a user gets it.
5. **A daemon launchd label per data directory.** The Mac daemon's agent is
   `org.onionpress.daemon` in the user's gui domain, whatever `$HOME` says.
   A test node in a scratch `HOME` and the live install of the same macOS
   user would take over each other's agent (a test teardown's `bootout`
   would stop the live heartbeat; the hub takes the address over ~10 min
   later). Until the label carries the data directory, the harness refuses
   to start a Mac node while that label is loaded for any program outside
   its scratch root (`harness/safety.py`).
6. Optional time knobs for slow paths (republish window, takeover delay),
   env-only, logged loudly when set. `ONIONHEAVEN_HEARTBEAT_INTERVAL`
   already exists.

## 6. Tiers

Durations are estimates on an M-series Mac with public Tor.

Roles below are filled per §2; tests are per available platform unless
marked *(Mac)* or *(Linux)*.

### T0 — stack smoke (~20 min per platform; role: node)

- Fresh install (DMG mounted and copied on Mac; `.deb` on Linux), first
  start, WordPress set up, admin password works.
- Site served locally (`http_local /`) and over Tor from the visitor
  (`/.onionpress-health` body marker, then `/`).
- `onionpress status`/`address` agree with what Tor serves.
- Stop, start, restart: same address, site back, no new key minted
  (`replaced-keys/` empty, key files unchanged).
- Tripwire clean.

### T1 — single-node features (~1.5 h per platform)

**Keys and identity**
- Vanity prefix at first start (short prefix, timed).
- `import-key`: valid key → new address served, old key copied to
  `replaced-keys/`; garbage, small-order and bit-255 keys refused with the
  old address still served.
- Damaged C Tor key files (truncate `hs_ed25519_secret_key`, wrong
  `hostname`) → re-derived from the PEM, old files in `key-set-aside/`,
  same address (#311).
- Key volume migration: seed an `onionpress-arti-state` volume in the old
  layout, start → copied, verified, same address; an unreadable folder in
  it → start refused, no empty volume.

**Backup and restore**
- App/CLI backup → `reset` → restore: same address, posts, media, users,
  subsites; derivatives pruned in the archive and regenerated after
  restore (`.regenerate-derivatives-pending` cleared).
- Restore onto another node: address moves; with a backup without a key
  (Mac Settings-page backup) the target keeps its own address.
- Format-1 fixture (a real 2.5.2 ZipCrypto backup) restores.
- Wrong password, truncated archive, newer-format header: refused with the
  right message, nothing changed.
- Malicious archives from `e2e/fixtures/`: symlink member
  (`wp-content -> ~`), `..` names, zip bomb, a dump with `\! touch /pwned`
  and `LOAD DATA LOCAL INFILE` → each refused or neutralised; assert the
  marker file does not exist anywhere.
- Settings-page backup: no onion key (Mac), no Cloudflare token, download
  only by the owner, file removed after download.
- Large site (generated: 5 GB uploads, 200 MB DB): backup and restore finish
  within their sized timeouts (#317).

**WordPress features** (Playwright against the local port, plus Tor for
visitor views)
- Onboarding wizard, auto-login (local scope only; refused from the Tor
  side), domain map (onion Host vs localhost), root redirect, user path.
- Multisite: second user gets a subsite; their OnionPress menu shows only
  Following; every host-reaching handler answers them with refusal (the
  owner-only census, exercised live).
- My Creations: upload 200 files, listing caches and refreshes, ETag/304,
  Range 206/416, hidden `.blog-archives/` never served (listing, direct
  URL, `?onionpress_creation=`), thumbnails generated (Mac).
- Following: node A follows node B over Tor; blogroll refreshes in wp-cron
  only (page views make no outbound request: count tor-proxy requests);
  local/LAN follow targets refused.
- Cache Enabler: anonymous onion view cached, logged-in/localhost/query
  string not; Link Fixer active per site, never requests an onion/local
  link (mock Wayback, §5.3).
- Importers from fixtures: WXR blog import (snapshot lands in the hidden
  folder), Bluesky, Mastodon, Twitter, Reddit exports.
- Directory, hit counter, avatar, favicon, status page, offline publish,
  name sync (against test-home).
- Security audit runs on start, core update path, xmlrpc blocked, PHP
  hardening ini in effect.

**Security properties on live containers**
- WordPress resolves nothing outside Docker (`getent hosts example.com`
  fails in wordpress and db; container names resolve).
- WordPress egress: TCP/UDP to an internet IP and to the host refused,
  `CLEARNET-blocked:` logged; published port works from the host and the
  LAN; no container reaches the published port.
- Tor side: tor container cannot reach the host, RFC1918, the Lima
  `192.168.5.2` alias; can reach the internet and Tor.
- Every container's `CapAdd`/`CapDrop`/`SecurityOpt` equals the census in
  `tests/test_container_capabilities.py` (import it, don't copy it); no
  `docker.sock` mount outside autoheal-proxy and onionheaven-farm.
- Config writes: drop a `config-updates.json` with an allowlisted change
  and a forbidden one (`ONIONHEAVEN_ADDRESS`, an image override) → first
  applied, second dropped and logged by name only.
- Planted links: as www-data, link names the host writes (status file,
  backup download name, uploads) to root-owned targets; run the operations;
  targets unchanged, nothing followed. wp-cli calls run as www-data (no
  `--allow-root` process seen during a start).

**Onion service health**
- Watchdog supervision: kill the watchdog → restarted with backoff, sleep
  state preserved; stop updating `alive` → killed and restarted.
- Republish request: `request_watchdog_republish` → DEL_ONION/ADD_ONION
  seen, HS_DESC UPLOADED within budget, address unchanged; rate limits
  hold (second request within 15 min ignored).
- Sleep/wake: `sleep-pre`/`sleep-post` (Linux), USR1/USR2 (Mac) → service
  removed and re-added, republish window reset, site reachable after wake.

### T2 — federation (~2–3 h; roles: node, hub, home — any platforms)

- Registration: node starts with `ONIONHEAVEN_ADDRESS=<test hub>` →
  `/online` signed, hub stores a key that derives to the node's address.
- Takeover: stop the node's heartbeat (stop OnionPress) → after the stale
  window the hub's takeover worker serves the node's address; visitor sees
  the takeover page; node starts → hub releases, node serves again.
- Opt-out: `REGISTER_WITH_ONIONHEAVEN=no` while running → one
  `/unregister`, hub's key dir gone, record cleared; a typo value sends
  nothing at all.
- Key leaves the hub on uninstall, reset, key import, restore of another
  address; with the hub down the pair is kept in `left_on_hubs` and retried.
- Hub change: point at a second hub → old one released.
- Hub protections live: forged `/online` (wrong signature, small-order
  key, PEM not matching the address) → 400/refused, stored key untouched;
  planted registry row naming `onionpress-wordpress` → not removed.
- Farm controller: spawn/exec/remove work, out-of-shape requests refused,
  `onionheaven` has no socket.
- Hub backup sealed to the recovery key; restore with and without the key;
  Settings-page backup and restore refused on the hub and on test-home.
- OnionHome: register a name, look it up from another node, name sync
  plugin updates; registry restore through the image's migrate script;
  analytics sharing on/off.
- Hub data migration: hub started on N-1 with data on
  onionpress-persistent-data → upgraded, counts equal, marker present.

### T3 — lifecycle (several hours; mostly unattended)

- **Upgrade from the previous release**: install N-1 from its published
  DMG/.deb, create content, upgrade to the candidate (Mac: the updater with
  §5.2; Linux: `onionpress update` from a fixture release), assert address,
  content, config, hub registration unchanged. Repeat from N-2 when a
  migration lands (key volume, hub data, tor network rename).
- *(Mac)* updater: staged copy and rename swap while health probes run (the
  2.5.2 → 2.5.3 failure), quarantine stripped, old bundle kept as `.bak-`.
- *(Linux)* `onionpress update`: refuses a `.deb` not matching `SHA256SUMS`, a
  wrong package/version, a draft; `--check`, `--version`, `--force`;
  Settings-page update button only checks.
- VM/host events: Colima VM restart (egress rules back before containers),
  hard VM stop (the ext4/fsck path, #309), Linux reboot (units come back).
- Soak: 6–12 h with the reachability loop on; report probe verdicts, any
  republish fired and why.
- Reset, scrub, uninstall: nothing left (volumes, VM, launch agents,
  native-messaging manifests, `~/.onionpress` per the documented rules),
  hub released first.
- *(Mac)* Multi-user on one Mac: two scratch HOMEs at once → second gets the
  +10000 offset (socket bind detection), both serve.

### T4 — external (opt-in, by hand, with dedicated test accounts)

- Wayback sweep against the real Wayback Machine from a test archive.org
  account, on a sandbox subsite.
- archive.org S3 key fetch over its onion.
- Cloudflare tunnel with a throwaway token.
- Never: the canonical OnionHeaven hub or OnionHome.

### UI coverage

- **WordPress admin**: Playwright (Chromium) for every page the tiers name.
- **Mac menubar and dialogs**: since #369 the menu actions are launcher
  subprocesses, so behaviour is covered at the launcher; `macui.py` checks
  only that the app launches, the menu has its items in each state, Copy
  Address puts the address on the pasteboard, and the Settings dialog
  writes config (accessibility API; needs a granted Terminal/agent).
  Native confirm dialogs (key import, address change) are answered through
  `confirm.py`'s protocol where possible, accessibility otherwise.
- **Linux tray**: launches, menu present (xvfb); actions via `handle-action`.
- **Browser extension**: Playwright persistent Chromium context with the
  unpacked extension: `.onion` routed through the menubar proxy, offline
  page when stopped, unreachable page for a dead onion, native host
  reports the offset port (currently suspected broken; this test is how
  we'll know). Firefox via web-ext later. Tor Browser: not automated.

## 7. Where it runs

Anywhere one of the §2 hosts is, with whatever it offers:

- **A Mac alone** covers macOS and, with Lima, Linux arm64 (the 2.5.3 QA
  ran this way, beside a live install, through scratch-HOME isolation).
  GitHub's macOS runners cannot run Colima VMs, so macOS is covered only
  from a local or self-hosted Mac.
- **A Linux machine alone** covers Linux: Debian or Ubuntu VMs with KVM
  on any distribution (the Ubuntu dev box, §10), throwaway users on a
  Debian-based host without. A Mac-only test shows as not run, never as
  passed.
- **GitHub's Linux runners** are Ubuntu, so they would run Debian VMs
  (where the runner exposes KVM) for amd64 and arm64, by
  `workflow_dispatch` only. Optional; nothing depends on it.

There is no scheduled run and no dedicated hardware for one yet (§9): runs
happen when someone starts one, on the machine in front of them. The
harness should stay cheap to start there (`--probe`, one command, a
report at the end) so that remains practical. Results are kept in
`e2e-results/<date>-<host>/` (git-ignored) and can be merged.

A run takes `--tier`, `--platforms` (default: everything the host offers;
`--platforms mac` to stay on one), `--max-nodes`, and
`--build <dmg/deb path or release tag>`, so the same suite tests a local
build, a candidate, or a published release. `e2e/run.sh --probe` prints
what this host could test without starting anything.

## 8. Order of work

1. ~~Harness skeleton: host probe, safety guard + tripwire, Mac and
   `linux-vm` nodes, the platform-aware report, evidence collection, T0.~~
   Built (§10). Next in this step: `linux-user`/`linux-host` nodes.
2. §5.1 (OnionHome override) and pre-written configs, so T2 needs no image
   patching.
3. T1 backup/restore, keys, and security properties (highest bug yield in
   past QA).
4. T1 WordPress features with Playwright.
5. T2 federation.
6. §5.2 and T3 upgrade/updater.
7. Extension, macui, private Tor network, soak.

## 9. Decisions (2026-10-24)

1. **Public Tor.** No private Tor network for now (§4).
2. **No scheduled runs yet.** Regular (nightly) testing is wanted, but there
   is no hardware for it; runs are on demand (§7). Revisit when a machine
   is available.
3. **Debian-based is the Linux target** (widened from "Debian only" on
   2026-10-29, #486). Debian 13 is the default guest and the reference;
   Ubuntu 24.04 LTS is a second `linux-vm` guest (`--guest`), and a
   Debian-based host counts as a Debian host for `linux-user`/`linux-host`.
   Both Docker flavours available, `deb` by default (§2). Recommendations
   below.
4. **Releases need no e2e run.** The release cadence is too high for a
   multi-hour suite. Stricter release requirements come once development
   slows and the product matures.

### Linux recommendations (open for discussion)

- **Debian 13 on both arm64 and amd64.** arm64 is what a Mac's Lima VMs
  run and what a Raspberry Pi runs; amd64 is most servers and CI. The
  same package, but the images, PyNaCl and mkp224o differ per arch.
- **Both Docker flavours, eventually.** They are the two real ways a
  Debian install gets Docker today, and they behave differently in ways
  the hardening depends on: egress rules go through RootlessKit's
  namespace under rootless and through the privileged helper + the `.deb`'s
  docker.service drop-in under system Docker; LAN clients look like the
  gateway under rootless. Default to `deb` (what `apt install` of the
  package gives), add `install-sh` when T1's security tests land.
- **Raspberry Pi OS later, if Pi users appear.** It is Debian-based, and
  the backup format was sized for a 1-GB Pi; Debian 13 arm64 in a VM
  with 1 GB of RAM covers most of that before real hardware.
- **Ubuntu as a second guest, not a second target.** Debian 13 stays the
  default and the one a plain `make e2e` runs; `--guest ubuntu-24.04`
  exercises the package on the distro most servers run. What differs there
  is what the distro ships (docker.io's version, Python 3.12, apt sources),
  not the harness. The `.deb`'s `Depends` are satisfied by Ubuntu 24.04's
  `main` and `universe` (both enabled in its cloud image): `docker.io`,
  `uidmap`, `python3-nacl` 1.5.0.

## 10. Running it

```bash
make e2e-probe                  # what this host could test; starts nothing
make dmg                        # a Mac build to test (or pass one, below)
make e2e                        # T0 on every platform the host offers
e2e/run.sh --tier t0 --platforms linux
e2e/run.sh --guest ubuntu-24.04 # the Linux VM runs Ubuntu 24.04 LTS instead of Debian 13
e2e/run.sh --release latest     # a published release's .dmg/.deb (checked against SHA256SUMS)
e2e/run.sh --mac-app /Applications/OnionPress.app --deb build/onionpress.deb
e2e/run.sh --keep -x            # stop at the first failure, leave the nodes up
e2e/merge-results.py e2e-results/A/results.json e2e-results/B/results.json
```

Builds: the Mac node copies `--mac-app`/`--mac-dmg`/the release's DMG,
else the checkout's `OnionPress.app` or `build/onionpress.dmg`; without
one, macOS is reported as not tested. A Linux node installs `--deb`/the
release's `.deb`, else builds one inside the VM from the checkout's
tracked files as they are on disk. Lima comes from `PATH`, `--limactl`,
or the `limactl` bundled in an OnionPress.app (the build under test, or
the one in /Applications, used only as a tool).

Each run writes `e2e-results/<date>-<host>/`: `report.md`, `results.json`,
`junit.xml`, `harness.log`, and per node `nodes/<name>/` (the harness log
of every command, the data dir's logs and config, every container's log
and `docker inspect`). Nodes live under `/tmp/ope2e` (`--scratch-root`),
which the harness claims with a marker file; the next run removes
whatever a killed or `--keep` run left there (VMs included).

Code: `e2e/harness/` (probe, safety, nodes, report, session; no pytest
in it, so `tests/test_e2e_harness.py` checks it in the unit suite),
`e2e/conftest.py` (pytest wiring), `e2e/test_t<N>_*.py` (tiers).

### Linux host setup

`make doctor` lists the four host tools the Linux VM node needs, and
`make e2e-probe` says whether `linux-vm` is offered. On a Debian-based
host (the Ubuntu 24.04 dev box, 2026-10-29):

1. **QEMU and its UEFI firmware**: `sudo apt-get install qemu-system-x86
   ovmf` (arm64: `qemu-system-arm qemu-efi-aarch64`). Lima's QEMU driver
   boots x86_64 guests from `/usr/share/OVMF/OVMF_CODE_4M.fd` (or the other
   paths `platforms.UEFI_FIRMWARE_PATHS` lists) and fails after the image
   download without one.
2. **KVM**: `/dev/kvm` must be readable and writable by you. A console
   login usually has it already (logind grants the seat's user an ACL);
   an SSH session or a service does not, so for those
   `sudo usermod -aG kvm "$USER"` and log in again. Without `/dev/kvm`
   the probe does not offer `linux-vm`: QEMU without KVM is far too slow
   for a Docker stack.
3. **Lima** has no Debian or Ubuntu package. Install a pinned release
   tarball from `lima-vm/lima` after checking its `SHA256SUMS`
   (the Mac app bundles Lima 2.x the same way):

   ```bash
   v=2.2.1; a=$(uname -m)
   cd "$(mktemp -d)"
   curl -fsSLO "https://github.com/lima-vm/lima/releases/download/v$v/lima-$v-Linux-$a.tar.gz"
   curl -fsSLO "https://github.com/lima-vm/lima/releases/download/v$v/SHA256SUMS"
   sha256sum -c --ignore-missing SHA256SUMS      # must print: lima-2.2.1-Linux-x86_64.tar.gz: OK
   tar xzf "lima-$v-Linux-$a.tar.gz" -C ~/.local  # bin/limactl, share/lima/templates
   ```

   `~/.local/bin` is usually on `PATH`; else pass `--limactl ~/.local/bin/limactl`
   to `e2e/run.sh`. Lima 2.2.1's `SHA256SUMS` (2026-10-03) says
   `b391ac7fcac2b2a5a1628107756463188ded4d18d3104a40a66a8bb6fec01bcb` for
   the Linux x86_64 tarball.
4. **Room for the VM.** The scratch root defaults to `/tmp/ope2e`; the VM's
   disk and Lima's state live below it and grow to several GiB. The
   harness refuses to start a VM with less than 10 GiB free there. On a
   host whose `/` is small, pass a root on another filesystem, outside your
   home and at most 40 characters (`sudo install -d -o "$USER" /home/ope2e`,
   then `E2E_ARGS="--scratch-root /home/ope2e"`); `/dev/shm/ope2e` works
   for one run from RAM on a 32 GB machine. Lima's image cache
   (`~/.cache/lima`, a few hundred MB per guest) is in your home as usual.

Then:

```bash
make e2e-probe                                   # linux-vm available
make e2e                                         # T0 on Debian 13 amd64
make e2e E2E_ARGS="--guest ubuntu-24.04"         # T0 on Ubuntu 24.04 amd64
```

Hosts this has run on: see the "Tested:" line in §3.1 and the entries
below.

- 2026-10-25: macOS 27.0 arm64 (mac), Debian 13 arm64 (linux-vm from a
  Mac). T0.
- Ubuntu 24.04 x86_64 host (Lima 2.2.1 + QEMU 8.2 + KVM): set up on
  2026-10-29; the first T0 run on it (Debian 13 amd64, then
  `--guest ubuntu-24.04`) is still to be recorded here.

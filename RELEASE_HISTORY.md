# OnionPress Release History

What changed in each release, newest first, written for the people who run
OnionPress. Every release adds its section here before it is cut:
`build/release.sh` puts that section at the top of the GitHub release's notes,
above GitHub's generated list of pull requests, and refuses a version that
has none.

Each section's heading is `## X.Y.Z — YYYY-MM-DD`. Full lists of changes are
on each [GitHub release](https://github.com/brewsterkahle/onionpress/releases).

## 2.6.0 — 2026-10-26

A large security round and a new way the Mac app runs.

### Highlights
- **On a Mac, OnionPress now runs as a background service** (#367). The
  status checks, the OnionHeaven heartbeat, Tor recovery and the Settings
  page's requests run in `onionpress daemon`, a per-user launchd agent; the
  menu bar icon only shows its state. If the icon says "Reconnecting to the
  OnionPress service...", the service is restarting; it does not mean your
  site stopped. Quit still takes the site down, unless you set
  `onionpress config set QUIT_KEEPS_SITE_RUNNING yes`.
- **Everything the app does is an `onionpress` command** (#365, #366, #382):
  `onionpress config get|set|unset|list`, `onionpress onionheaven
  enable|disable|status|overview`, `address`, `version`, `logs`, `open`,
  `check-reachability`, `onionname`, `backup`, `restore`. The Mac menu and
  the Linux tray call the same commands, and destructive ones ask before
  they act.
- **Updates are checked before they are installed.** A Mac update mounts
  the new DMG only if it matches the release's published `SHA256SUMS`, and
  WordPress core updates come only from downloads.wordpress.org and must
  match WordPress.org's checksums (#395).
- **The Settings page can no longer install an update on a Mac**; it reports
  a newer release and points you to Check for Updates… in the menu, which
  asks you at the Mac.

### Security fixes
- The local proxy the browser extension uses carries `.onion` only: no
  other website can read your onion address from it or use it to reach your
  network.
- The healthcheck service's message channel is gone: nothing a stranger
  sends to your healthcheck address is shown to you as a message.
- Your `~/.onionpress` folder is readable only by you, and its config file
  only by you, on every path that creates them (#397).
- Settings page requests are size-limited and throttled (#396); actions
  with a side effect need a fresh nonce (#402).
- Imported posts (blog, Reddit and other importers) are filtered as they
  come in (#403); the hit counter checks its nonce (#398); My Creations
  serves documents that could run scripts in a sandbox (#399).
- A plain node's onion address no longer exposes the hub API (#393).
- Every input of the Mac build, every GitHub Actions step and every image
  the stack runs is pinned by hash.

### Also in this release
- **Fresh images:** new tor and WordPress images (still WordPress 7.1 on
  PHP 8.3.36), and re-pinned MariaDB 13.0.2 and autoheal.
- **A launch downloads only what a release moved.** `UPDATE_ON_LAUNCH` now
  defaults to `no` everywhere; launches no longer contact the image
  registry for nothing (#36).
- **Linux:** the `.deb` finishes its own setup on Debian 13 with rootless
  Docker; upgrades restart the site (#357); `install.sh` installs the
  newest release, not the development branch; the healthcheck key is left
  out of backups, as on the Mac; Share Now and the tray's Backup… and
  Restore… work like the Mac's (#351, #375).
- **Mac:** a first start on a new account no longer puts Colima's files in
  `~/.colima` (#370); a headless `onionpress setup` no longer shows the
  welcome screen afterwards (#374).
- **Docs:** the README says what an OnionHeaven hub can do with your key
  and how to opt out.

### Worth knowing
- Mac users: this update installs the background service. After updating,
  the first launch starts it; you do not need to do anything.
- If you set `UPDATE_ON_LAUNCH=yes` yourself, it is kept.
- Backups still use format 2; older backups still restore.

### OnionHeaven hub and OnionHome operators
- Free registrations are capped: 4 registrations per address, 5,000
  addresses per hub, 10 names per address on OnionHome (#391). Existing
  registrations always keep their place. Each cap can be changed with an
  `ONIONHEAVEN_MAX_*` / `ONIONHOME_*` environment variable.
- A signed request is accepted only once (#400); the hub API answers only on
  a hub's or OnionHome's own address (#393).
- As before: take a backup and note the registry and name counts before
  updating, compare them afterwards, and never downgrade.

## 2.5.6 — 2026-10-21

- **Existing sites move to WordPress 7.1**, the version the WordPress image
  now ships (#362).
- **OnionHeaven is on by default, with no startup prompt** (#361). The
  heartbeat sends your onion service's private key to the hub so it can keep
  your site up while your computer is off. To opt out, set
  `REGISTER_WITH_ONIONHEAVEN=no` in `~/.onionpress/config`, untick it in
  Settings, or run `onionpress onionheaven disable` on Linux; an explicit
  `no` also asks the hub to delete the key it holds. Installs that never
  answered the 2.5.x prompt start registering with this update.
- **License:** OnionPress is AGPL-3.0-or-later, now declared everywhere it
  ships, with third-party notices (#360).
- **Theme:** posts show their replies (comments.php, #363).
- **Mac Settings dialog** is wider, so fields show their values (#364).
- **Linux:** the Cloudflare Tunnel runs once the Settings page saves a token
  (#352, #359).

## 2.5.5 — 2026-10-21

- **Security fix:** the WordPress image is built on PHP 8.3.36, which fixes
  the "PHP2Shell" vulnerability. The image build refuses a PHP without the
  fix. Please update.
- The tor image's Onimages base is re-pinned (the previous digest was gone
  from the registry).

## 2.5.4 — 2026-10-15

Continues the hardening of 2.5.3 and fixes a round of reliability issues.

### Highlights
- **WordPress talks to the outside world only through Tor.** The WordPress
  and database containers can no longer reach your LAN, your computer's own
  services or the internet directly, and make no DNS lookups outside Docker
  (#144). The Tor side reaches only the internet, never your local network.
- **Your onion address is never lost by accident.**
  - A damaged key file no longer turns into a new address (#311).
  - Importing a key keeps a copy of the old one in
    `~/.onionpress/replaced-keys/`. On a Mac, a key imported from the
    Settings page must be confirmed at the Mac.
  - Restoring a backup of a different address from the Settings page asks at
    the Mac first (Linux refuses and points to `onionpress restore`). A
    backup without a key restores onto the address you already have.
- **Every container drops all Linux capabilities** and adds back only what it
  needs, with `no-new-privileges` on everything.
- **Third-party images are pinned** (MariaDB, autoheal, cloudflared, the
  socket proxy). A launch downloads only an image a release has moved (#36).
- **Tor recovers by itself.** The watchdog that republishes your onion
  service is supervised and restarted if it stops (#313). On Linux the
  reachability check and automatic republish now run (#310).
- **Mac auto-update swaps the app by rename**, fixing the "Input/output
  error" that broke the 2.5.2 → 2.5.3 update.
- **Linux `onionpress update` installs real releases**: the release's `.deb`,
  checked against the published `SHA256SUMS`, only if newer.

### Also in this release
- **My Creations:** faster listings, resumable downloads and video seeking,
  up to 200 files per upload with a report of anything refused, Mac
  thumbnails from launch (#319, #325).
- **Backups and restores** get time limits that grow with the site; a failed
  Mac restore says why (#317, #323).
- **Following:** the blogroll refreshes in the background (#314), default
  follows are kept, and OnionHome names stay attached (#315).
- **Wayback:** one archive.org login and one sweep for the whole network,
  over the archive.org onion (#322).
- **Settings page backups** leave out the Mac's onion key and the Cloudflare
  token. Backups from the menu or `onionpress backup` keep everything.
- **Cloudflare Tunnel** tokens keep their `=` padding (#323).
- **Linux:** autoheal starts with the other containers (#324), onionname
  errors name the real limits, `START_ON_BOOT` uses user units, and the
  `.deb`'s files are owned by root (#316, #326).
- **Browser extension** routes `.onion` through OnionPress's local proxy
  instead of Tor's SOCKS port (#318).
- **The Mac app is universal** again (Apple Silicon and Intel) (#326).

### Worth knowing
- Linux users on 2.5.3: install this `.deb` by hand
  (`sudo apt install ./onionpress.deb`); from 2.5.4 on, `onionpress update`
  can do it.
- Mac users: the safer update code takes effect from the *next* update. If
  the 2.5.3 → 2.5.4 auto-update fails, download `onionpress.dmg` and drag it
  to Applications.
- A Cloudflare Tunnel ingress rule that points at a LAN service stops
  working.
- Rootless Docker on Linux: LAN visitors to the local WordPress port look
  like the machine itself; the launcher says so at start.
- Backups still use the 2.5.3 format.

### OnionHeaven hub and OnionHome operators
- The Docker socket moved out of `onionheaven` into a new `onionheaven-farm`
  controller with no network and no capabilities. After the first start,
  check `docker logs onionheaven-farm` for "listening on" and the first
  `spawn … -> ok`.
- Hub backups can be sealed to an offline recovery key
  (`onionpress hub-recovery-key create`).
- The Settings page refuses to back up or restore a hub or OnionHome.
- Registrants' keys and request signatures are checked with libsodium.
- Take a backup and note the registry and name counts before updating,
  compare them afterwards, and never downgrade. See `docs/DOCKER-SOCKET.md`.

## 2.5.3 — 2026-10-13

A security hardening release: backups, the local WordPress site, its
containers, the Tor setup and the OnionHeaven/OnionHome services.

### Worth knowing
- **New backup format**, encrypted with libsodium. OnionPress 2.5.2 and
  earlier can't restore it, so update every machine you might restore on.
  Old backups still restore.
- **Linux:** install from the `.deb`, which now depends on `python3-nacl`
  (`onionpress update` can't deliver this release, #316). Tor's SOCKS port
  is local-only. `ONIONPRESS_BIND_HOST` now takes effect.
- **Restore** no longer brings back the OnionHeaven or analytics choices, the
  Cloudflare token or advanced settings.
- **On sites with more than one user,** only the site owner gets
  OnionPress's settings, backup, restore and service controls;
  `/onionpress-status` is owner-only.
- **Page caching (Cache Enabler) and Wayback link fixing (IA Link Fixer) are
  on.** Opt out with `INSTALL_CACHE_ENABLER=no` / `INSTALL_IA_PLUGIN=no`.
- **Following** accepts `.onion` and public web addresses only.
- Blog importer snapshots move to a hidden folder; PHP-like files are refused
  in My Creations and uploads; the Cloudflare token field is write-only.

### OnionHeaven hub and OnionHome operators
Both move their data onto dedicated Docker volumes on first start. Take a
backup and note the registry or name counts first, check them afterwards,
and never downgrade. See `docs/DOCKER-SOCKET.md`.

## 2.5.2 — 2026-10-12

- **OnionHeaven registration became opt-in** with a startup prompt (#297;
  reversed in 2.5.6).
- **The onion service republishes itself** when the app's probe finds it
  unreachable for 10 minutes while Tor looks healthy (#300), with a better
  probe: a static health page, faster failure checks, a reset after wake and
  a classifier for why it failed (#308).
- **Backups store original images only** and regenerate the other sizes on
  restore, about half the size for media-heavy sites (#306).
- **OnionPress's Colima stays out of your own `~/.docker` and
  `~/.ssh/config`**, and never adopts another Colima VM (#285, #287, #279).
- **A wedged VM is recovered** from the start path (#286); every component
  failure at boot shows in the logs (#292).
- Hub API requires signatures from onion callers (#302); takeover guards run
  before a site is marked taken over (#296).
- Writing: "New Post" in the admin bar and an onboarding first post (#305);
  a progress indicator when following by name and tips on the launch splash
  (#303).
- `onionpress get <url>` fetches a page through Tor (#307).
- Fixes: wrong backup passwords are reported (#288), vanity key import keeps
  the prefix length (#301), launcher lifecycle and auto-login redirect fixes
  (#273, thanks @guoliu), Linux install and analytics fixes (#294, #295).
- The DMG is built with `diskutil image` and carries no `__pycache__`
  (#290, #291).

## 2.5.1 — 2026-10-01

- The launchers wait for Docker before checking the onion key volume, and
  refuse to start if they cannot tell, instead of risking a new address.
- `build/base-image-digest.sh` retries transient registry errors.

## 2.5.0 — 2026-09-30

- **The tor image is built on the Tor Project's official Onimages C Tor
  image**, and Arti is removed: C Tor is the only Tor implementation (#283).
- The onion key volume is renamed (`onionpress-arti-state` →
  `onionpress-onion-keys`); the launcher copies it over once, on first start,
  and refuses to start if the copy fails, so the address is kept.

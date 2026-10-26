# OnionPress Release History

What changed in each release, newest first, written for the people who run
OnionPress. Every release adds its section here before it is cut:
`build/release.sh` puts that section at the top of the GitHub release's notes,
above GitHub's generated list of pull requests, and refuses a version that
has none.

Each section's heading is `## X.Y.Z — YYYY-MM-DD`. Full lists of changes are
on each [GitHub release](https://github.com/brewsterkahle/onionpress/releases).

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

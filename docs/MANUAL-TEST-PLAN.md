# Manual test plan (2026-11-03, against upstream main `f2c746a9`)

What to run by hand before the next release, on both platforms. It covers
what the automated suites cannot: `make test-unit` stubs docker and the
OS, and the e2e suite (`e2e/`) runs only T0 and the first slice of T1.
The rest of `docs/E2E-TESTING.md` §6 is design only.

This is the hand-run half of the testing plan; `docs/E2E-TESTING.md` §8
is the automated half. The table under "Where the suites stop" says which
sections here §8 will take over. When an e2e module lands, move what it
checks into "Now automated" and drop it from its section.

**Now automated (don't repeat these by hand):**
- **T0** (`e2e/test_t0_stack.py`, on both platforms):
  - fresh install, first start, headless `onionpress setup`, admin login
  - the site served locally and over Tor
  - `status` and `address` agree with what Tor serves
  - stop/start and `onionpress restart` keep the address and the key
  - `install.php` / `setup-config.php` refused from the tor container
    before setup and over Tor after it (#464)
  - no hub registration, and the onionname not registered with
    OnionHeaven off
- **T1(a)** (`e2e/test_t1_a_hardening.py`), read-only checks on the live
  containers:
  - WordPress and MariaDB resolve no outside name
  - WordPress reaches neither the internet nor the host's published port,
    and the tor container reaches no host service
  - every container's capabilities match the census
  - only autoheal-proxy and onionheaven-farm mount the Docker socket
  - `~/.onionpress` is 0700, and `config` and `secrets` are 0600

**e2e runs on this plan's commit (2026-11-03, Ubuntu 24.04 guest, rootless
Docker, `.deb` built from `f2c746a9`, T0 + T1):**
- First run: 14 passed, 1 failed (4 skipped after it). The health file
  over Tor answered in 5 s, then `GET /` over Tor timed out after 90 s and
  never reached Apache. WordPress was mid multisite-init and security
  audit at that moment.
- Rerun: 19 of 19 passed.
- So this was most likely a Tor flake. T0 now retries a page over Tor
  while no answer comes back (`harness.scenario.tor_page`), and reports a
  fetch that needed it. By hand, still watch the **first page load over
  Tor in the first minute after setup** (§2).
- Not run here: macOS (the e2e Mac node needs a Mac host) and the Debian
  13 guest (it passed at 18:34 today on an earlier commit).

**Where the suites stop:**
- The e2e nodes run the **published, pinned images**. A change inside the
  tor or wordpress image is untested end to end until it is published
  (see §14).
- The e2e nodes use only the CLI, never a GUI.
- There is no T1(b)+, T2 or T3 yet: keys, backup/restore, federation,
  upgrades and host events are all still manual.

What `docs/E2E-TESTING.md` §8 plans to automate, against this plan:

| Section here | Planned e2e | What stays manual after it lands |
|---|---|---|
| §12a Settings page | T1 (b), the page's requests | the page in a browser, the notices, the test archive.org login |
| §9 onionnames | T1 (c), onionnames | registration against a test OnionHome |
| §6 key import, §14 damaged keys | T1 (d), keys | the native dialogs, the Settings page import |
| §5 backup and restore | T1 (e), backup and restore | Mac ↔ Linux, the UIs' dialogs, the page's restore |
| §12 WordPress side | step 4, Playwright | Tor Browser, judging the pages by eye |
| §9 OnionHeaven | step 5, T2 federation | the probe never asking the hub while opted out (#544) |
| §1 upgrade, §8 update | step 6, T3 | the Mac's Check for Updates dialog |
| §13 extension, §11 soak | step 7 | real sleep/wake, reboot, VM hard stop |
| §2–§4 setup window, tray, menubar | none (macui and the tray launch only) | all of it |
| §7 uninstall, reset, scrub | T3 lists them | the tray's terminal and sudo |
| §10 multiple users | T3, two scratch HOMEs | fast user switching |

Since 2.6.1, about 30 PRs moved most of the launchers' bash into Python
(#406, #407, #405, #477, #523, #524, #525, #526, #529). Each one's notes
in `docs/phase4-resume.md` end with "Not live-tested". That makes this
round mostly a check that the moved code still does the same thing on a
real machine.

**What automation misses, and so what this plan is about:**
1. **Upgrades from a released build.** Units retired, labels kept, state
   carried over. Unit tests only see the new tree.
2. **Real OS events.** Sleep/wake, reboot, logout, a VM hard stop, a
   killed daemon.
3. **Real UIs.** The menubar, the tray, native dialogs, a terminal opened
   by the tray, sudo prompts, the setup window, the WordPress Settings
   page.
4. **Timing.** The Linux daemon's 30 s cadence for page requests,
   "Reconnecting" vs "Stopped", Tor descriptor delays.
5. **Data crossing machines.** A backup made on one platform, restored on
   the other.
6. **Human judgment.** Wording, whether a message says what to do next,
   whether a dialog defaults to Cancel.
7. **Unpublished image changes.** Covered in §14.

Changes since the first version of this plan, with the section that now
tests each by hand:
- #527: first-run setup through `onionpress setup --json` from both UIs,
  and one `open` path. See §2.
- #528: the Settings page stops doing the host's work, and the
  archive.org login is the host's. See §12a.
- #554, #555: onionnames go with OnionHeaven. See §9.
- #557: `ui_client.status_view` for both UIs, and the tray log's
  redaction. See §3 and §4.
- #551, #558: dead launcher bash removed, and the stop is the CLI's
  `stop-containers`. See §7.
- #559: the tor image rework. See §14.

## Ground rules

- [ ] Before you start, back up any live site on the test machines
      (`onionpress backup -o ~/safe/`). Keep the password somewhere apart
      from the backup.
- [ ] Keep `REGISTER_WITH_ONIONHEAVEN=no`, except in §9, and there use a
      **test hub only**, never the canonical OnionHeaven or OnionHome.
- [ ] Machines: one Mac (Apple silicon; an Intel Mac too if you have one,
      since releases are universal), Ubuntu 24.04 (rootless Docker), and
      Debian 13. A Lima VM is fine for Linux
      (`docs/E2E-TESTING.md` §10).
- [ ] Artifacts: build them the release's way. On the Mac, `make build`
      with `ONIONPRESS_RELEASE_BUILD=1`. On Linux, the `.deb`, checked with
      `build/check-deb.sh`. Also have the published 2.6.1 `.dmg` and `.deb`
      for §1.
- [ ] For every test, note the version, the platform and pass/fail. Keep
      `~/.onionpress/onionpress.log`, `launcher.log` and `logs/` from
      every failure.

Priority: **P1** = must pass before release. **P2** = should. **P3** = if
there's time.

---

## 1. Upgrade from 2.6.1 (P1, both platforms)

This is the riskiest step and none of the suites exercise it (T3 is
unwritten).

**Linux (one Ubuntu 24.04 machine, one Debian 13):**
- [ ] Install the 2.6.1 `.deb`, finish setup and make a post with an
      image. Note the address and `systemctl --user list-units 'onionpress*'`.
- [ ] Upgrade with `sudo apt install ./onionpress_<new>.deb`. Pass when:
  - the install does not fail, and the postinst reports what is running
  - `onionpress-heartbeat.service` and `onionpress-watcher.timer` are gone,
    with no leftover runtime mask (`systemctl --user is-enabled` /
    `ls $XDG_RUNTIME_DIR/systemd/user/`)
  - `onionpress-daemon.service` is active, `onionpress.service` is active
    (exited)
  - the address, the post and the image are unchanged, the site is
    reachable over Tor, and `~/.onionpress/replaced-keys/` is empty
  - the tray restarted and shows Running
- [ ] Also upgrade through `onionpress update` from 2.6.1. That uses the
      **old** updater, since an updater fix lands one release later. Then
      run `onionpress update --check` on the new version.

**Mac:**
- [ ] Install 2.6.1, set up, make content. Update through the menu's
      Check for Updates… (this needs a test release, or skip it and
      replace the app by hand while it is quit). Pass when:
  - `launchctl print gui/$(id -u)/org.onionpress.daemon` shows the same
    label, now run by the new bundle (#505 keeps the default label)
  - the menubar goes from "Reconnecting" to Running and never shows
    "Stopped"
  - the address and content are unchanged
  - Gatekeeper raised no prompt, and the old bundle was kept as `.bak-`

## 2. Fresh install and first run (P1)

T0 covers the headless path. This is the GUI path and the first-run details.

**Mac:**
- [ ] New macOS user account. Open the DMG, drag the app over, launch it.
      Pass when: `~/.colima` is not created; the setup window completes;
      the menubar states go Starting → Running.
- [ ] Vanity prefix (2–3 characters) during setup: it finishes, and the
      address starts with the prefix.
- [ ] Open the site from the menu (auto-login works locally). Copy Address
      puts the address on the clipboard.
- [ ] **#527: the setup window now runs `onionpress setup --json`.**
  - The progress shows while it runs, and a non-English language choice
    gives a WordPress in that language.
  - Trigger a failure (stop Colima mid-setup). The window shows the
    error, and setting up again works.
  - Choose an onionname. With OnionHeaven off, the window says the name
    wasn't registered and why (`ONIONNAME_OFF_NOTE`).
  - `onionname set --rename-admin` (the collision dialog's path, with a
    test OnionHome) renames the admin, and you can log in with the new
    name.
- [ ] **#527: Open Site / Settings / Onion now go through the file://
      redirect page on the Mac.** Check them in Safari, in Firefox, and
      with Tor Browser as the browser choice. Each logs in and never
      shows the token in the address bar or the history.

**Linux (both distros):**
- [ ] `sudo apt install ./onionpress.deb` on a clean machine, with no
      manual step. Rootless Docker is set up. `onionpress check-docker`
      passes. `docker-rootless-setup.log` is owned by the user.
- [ ] Before WordPress is installed, `ss -ltnp` shows WordPress's port on
      `127.0.0.1` only. Opening `http://<LAN-IP>:<port>/wp-admin/install.php`
      from another machine fails (#464). e2e checks only the tor side, not
      the LAN bind.
- [ ] Complete setup **from the tray** (#527: now `onionpress setup
      --json`, or `provision --json` for the legacy branch). The port then
      moves to the configured bind. The tray shows the result, and the
      language choice is honored.
- [ ] Right after setup finishes, open the site in Tor Browser within the
      first minute and reload a few times. Pages load. (One e2e run timed
      out here; see the top of this plan.)
- [ ] Tray → Open Onion with Tor Browser **not running** (the cold
      launch). It starts, then opens the site logged in, or shows the URL
      to paste.
- [ ] #519: run `docker images` before generating a vanity address. The
      first run pulls the tor image *before* `generate-vanity`. Then a
      backup holds a key (no `onion_key_unreadable` in its metadata).
- [ ] #543: on Ubuntu 24.04's Compose, Cache Enabler and Link Fixer are
      active after the first start (`wp plugin list --network` in the
      wordpress container, as www-data).
- [ ] `install.sh` (`curl | bash`) on a third clean machine installs the
      newest release tag.

## 3. The Linux daemon (#407, P1)

This is new in this release, and none of it has been run on a real machine.

- [ ] `onionpress daemon status` and the tray agree with
      `~/.onionpress/daemon-state.json`.
- [ ] `kill -9` the daemon. systemd restarts it (`Restart=always`). The
      tray shows "Reconnecting" in between, never "Stopped". Only one
      daemon process runs afterwards.
- [ ] **#557: the tray's view now comes from `ui_client.status_view`.**
      Stop the daemon unit for more than 90 s
      (`systemctl --user stop onionpress-daemon`):
  - the tray shows "Reconnecting", not the last state it saw
  - the actions that view doesn't enable are greyed out, as in the
    menubar
  - with the site offline, the tray doesn't show "Starting…"
- [ ] **Suspend/resume** (`systemctl suspend`, a real laptop if possible).
      After waking, the log shows `handle_wake`, the recovery window is
      reset, and the site is reachable over Tor within a few minutes.
      Also the offline-before-sleep path: the daemon sends `/offline`
      within logind's delay.
- [ ] Reboot. The units come back by themselves (with lingering, or after
      login, whichever is documented) and the site comes back.
- [ ] Settings page requests now run on a 30 s cadence (the old watcher
      used 10 s). Click Share Now, change a setting, run Check for
      Updates. Each answers within about 30 s, and the page's feedback
      doesn't look broken while it waits.
- [ ] `onionpress stop`: the log shows `daemon offline` before the
      containers stop. `onionpress restart` does the same.
- [ ] Rootless `DOCKER_HOST`: run `onionpress launcher-env` and
      `systemctl --user show onionpress-daemon -p Environment`. The daemon
      reaches the same socket as the launcher (#503). With system Docker
      (a Debian install with the user in the `docker` group), it still
      works without `DOCKER_HOST`.

## 4. The menubar and the tray on one client (#526, P1)

Click every item, in each state (Running, Stopped, Starting). The cases
below are the ones that go through `ui_client`'s check/confirm loop.

- [ ] Every confirm dialog: **Cancel is the default**. Pressing Enter
      cancels. Leaving it for 5 min cancels. The wording comes from
      `ui_client.question_text` and is clear.
- [ ] Backup…: asks for the password twice and never shows it in `ps`
      (`ps -eo args | grep -i backup` while it runs). The finished dialog
      names the file and any `unchecked_notes`.
- [ ] Restore…: choose a backup with another address. The address-change
      question comes up, and Cancel leaves everything alone. With a wrong
      password you get a clear error and the site stays up.
- [ ] Check for Updates…: "up to date" and "error" (go offline) read as
      different outcomes, not the same message.
- [ ] Settings dialog (Mac) / Settings (tray): a hub address typo is
      refused (`validate-oh-address`, now through `Launcher.run_json`),
      and a good one is saved. On the Mac, `VM_MEMORY` / `VM_CPU` outside
      `config set`'s bounds are refused with a readable message.
- [ ] Backup…: the dialog pre-fills the onionname. An empty username is
      filled in by the CLI and the backup completes (#557: the menubar's
      own docker lookup is gone).
- [ ] Tray log redaction (#557): after an hour of use, plus one forced
      roll, `~/.onionpress/tray.log*` shows `~` instead of the home path,
      contains no token or password, and the rolled files are 0600.
- [ ] Quit (Mac): the site goes down. With `QUIT_KEEPS_SITE_RUNNING=yes`
      it stays up and the daemon keeps running.

## 5. Backup and restore (#406, #508, #509, P1)

- [ ] **Mac → Linux and Linux → Mac**: a CLI backup made on one, then
      `onionpress restore` on the other. Pass when the address moves, and
      posts, media, users and subsites come through. Image derivatives are
      regenerated (`.regenerate-derivatives-pending` is cleared later), and
      originals are served in the meantime.
- [ ] Run `onionpress restore ../backup.zip` from an unrelated folder
      (`cd /tmp`) with a relative path (#508).
- [ ] `onionpress setup --from-backup` on a fresh install restores the
      backup's site and address, and doesn't produce an empty WordPress
      (#509).
- [ ] Interrupt a restore after its teardown (kill it, or give it a
      corrupt archive whose damage only shows late). Tor comes back, and
      the next `onionpress start` imports the kept `.install-from-backup`.
- [ ] Settings page backup: download it, check that the file is removed
      afterwards, and that there is no onion key and no Cloudflare token
      in it.
- [ ] Settings page restore of the same address works on both platforms.
      **On Linux, watch the key path**: the page restore writes the key
      the Mac's way (docker cp + tor restart). This is flagged as never
      live-tested.
- [ ] A Settings page restore of another address: the Mac asks at the Mac,
      Linux refuses with the documented message.
- [ ] A format-1 backup from 2.5.x restores (keep one around for this).
- [ ] Passing a password on argv (`onionpress backup secret`) is refused
      with exit 2 and a helpful message.

## 6. Key import (#524, P1)

- [ ] `onionpress import-key <pem>` on both platforms. You get the
      address-change question, the new address is served, the old key
      shows up in `replaced-keys/`, and the daemon keeps running (no stop
      or restart).
- [ ] A garbage file and a key for the current address are refused, and
      the old address is still served.
- [ ] Mac Settings page import: a native dialog appears with Cancel as the
      default; waiting 5 min cancels. On Linux the page's import is
      refused.
- [ ] Import while Docker is down (Mac: quit Colima). The next start
      finishes it (`.import-key-pending`).

## 7. Uninstall, reset, scrub (#529, P2)

- [ ] Linux tray → Uninstall: it opens a terminal, offers a backup first,
      asks you to type `uninstall`, then asks for sudo. Afterwards nothing
      is left: the units, the tray autostart, the containers and volumes,
      `/opt/onionpress`, the symlinks, the desktop entries and
      `~/.onionpress`.
- [ ] Run the same from the CLI with `-y`. No backup is offered under
      `ONIONPRESS_YES=true`.
- [ ] `onionpress reset` (both platforms): you get a new address, the
      start runs, the site comes up, and the hub state is reset.
- [ ] Mac `onionpress scrub`: the reset-in-place cycle finishes and the
      menubar recovers.
- [ ] Mac uninstall: the VM, the launch agents, the native-messaging
      manifests and the data dir go, as documented.
- [ ] **#558: the stop is now `onionpress stop-containers` on both
      platforms.**
  - With a Cloudflare token set, run `config unset
    CLOUDFLARE_TUNNEL_TOKEN`, then `onionpress stop`. cloudflared is gone
    too; it used to outlive the stop.
  - On a test hub with takeover workers running, a stop removes the
    `onionheaven-(poll|takeover)-N` containers, then the stack.
  - `.previous-port-offset` is written on both platforms, and the next
    start keeps the port.
  - Quit from the menubar and Stop from the tray take the site down the
    same way.

## 8. Update paths (#525, P2)

- [ ] Linux: `onionpress update --check`, `--check --json` (exit 0 even
      offline, with `error` set), `--version <older> --force` (a
      downgrade with a warning), and `--pre`. Off a terminal it needs
      `--yes`.
- [ ] Linux: tamper with a downloaded `.deb`, or point at a release whose
      `SHA256SUMS` doesn't match. It is refused and nothing is installed.
- [ ] The Settings page update button only checks, on both platforms, and
      the answer is cached for 5 min (click twice and check that the log
      shows one GitHub lookup).
- [ ] Mac: `onionpress update --check` from the terminal matches what the
      menu says.

## 9. OnionHeaven with a test hub (#544, #407, P2)

- [ ] Set `REGISTER_WITH_ONIONHEAVEN=no` and turn on a network capture
      (`tcpdump` on the tor container's bridge, or the hub's log). The
      reachability probe's failure path never contacts the hub (#544).
- [ ] Set `onionpress onionheaven enable` with the test hub. On Linux the
      first `/online` comes from the daemon, **only after WordPress is
      installed** (try enabling it before setup). `onionpress onionheaven
      overview` shows it registered.
- [ ] Run `onionpress onionheaven disable`. One `/unregister` is sent and
      the hub drops the key.
- [ ] Stop the node for longer than the stale window. The hub's takeover
      page is served, and when the node starts the hub releases it.
- [ ] Uninstall or reset while registered: the hub is released first.
      With the hub down, `left_on_hubs` is shown.
- [ ] **#554, #555: onionnames go with OnionHeaven.** Use a test OnionHome
      only (`ONIONHOME_ADDRESS` set to it). e2e checks only the off case at
      setup.
  - Off: add a second WordPress user and wait past the name-sync
    plugin's first daily reconcile (a minute after load). The test
    OnionHome sees no registration.
  - Turn OnionHeaven on. The daemon's retry (`onionname set --json`)
    registers the recorded name, and new users register.
  - Turn it off again. Names already registered stay, and nothing new is
    sent.
  - The Settings page's OnionHeaven text and `config set` help say that
    turning it off also stops onionname registration.

## 10. Ports, multiple users, data dirs (#405, #505, P2)

- [ ] Mac, two users logged in at once (fast user switching). The second
      user gets the +10000 ports, both sites serve, and each user's menu
      controls only its own site.
- [ ] Mac: an install with a non-default data dir gets its own launchd
      label (`org.onionpress.daemon.<hex>`) and doesn't touch the default
      one's daemon.
- [ ] Restart while the stack is running: the port stays the same and
      doesn't move to the next offset (#507).

## 11. Recovery and host events (P2)

- [ ] Mac sleep/wake (lid closed for 10+ minutes). On wake the site is
      reachable again and the log shows no republish storm.
- [ ] Mac: hard-stop the VM (`colima stop --force`, or kill the qemu/vz
      process), then start from the menu. The fsck path runs and the site
      comes up.
- [ ] Kill the tor container's watchdog. It is restarted.
- [ ] Block Tor for 15+ minutes (pull the network). `RepublishTrigger`
      fires once, within its rate limits, and the address doesn't change.
- [ ] Soak: leave both platforms running overnight. Next morning, check
      the logs for repeated errors, the probe verdicts and any republish,
      and check that `daemon-state.json` is fresh.

## 12. The WordPress side, by eye (P2)

The Playwright coverage planned for T1 doesn't exist yet. Click through
these:

- [ ] The onboarding wizard, the OnionPress Settings page (the status
      fields: address, version, recent logs, hub overview), the dashboard
      widgets.
- [ ] A second user's subsite: their OnionPress menu shows only Following,
      and host actions refuse them.
- [ ] My Creations: upload a folder, view the listing, open an `.html`
      file (sandboxed: its scripts don't run), check thumbnails on the Mac.
- [ ] Following: follow another test node over Tor, and the blogroll
      fills after wp-cron runs.
- [ ] Importers: one WXR and one Twitter ZIP from a test export.
- [ ] The visitor's view in **Tor Browser**: the theme, images, the
      comment form, and the hit counter.

## 12a. The Settings page asks the host (#528, P1)

Most of this moved from PHP to `handle-action`. Unit tests hold the
contract, but not the page's behavior in a browser.

- [ ] The release line comes from status.json's `release`. Block
      WordPress from GitHub (it is blocked anyway) and check the page
      still says whether there is a newer release. Check for update
      refreshes it within about 30 s on Linux.
- [ ] Share Now is now `handle-action upload-analytics`. Click it with
      sharing on (against a test OnionHome) and with it off (refused,
      with a message). No `docker exec` into wordpress shows up in the
      daemon log each cycle.
- [ ] **archive.org login on the host:** enter a **test** archive.org
      account on the Settings page.
  - The keys are saved and the Wayback sweep can use them.
  - While it runs, the request file in `/var/lib/onionpress/` is 0600
    and is gone once the login starts.
  - Neither the account nor the password appears in `onionpress.log`,
    the daemon log or `archive-login-result.json`.
  - A wrong password gives a readable reason with the password cut out.
  - The verb is offered only by a host that lists it (`page_actions`):
    with a 2.6.1 host the page doesn't write the file.
- [ ] Save a setting and look at the form before the host applies it.
      The unapplied value shows on top. Afterwards it shows the applied
      one, and keys that need a restart say so.
- [ ] The status page writes no requested-action. The security audit's
      report isn't published anywhere.

## 13. External and rarely used (P3)

- [ ] A Cloudflare tunnel with a throwaway token, on both platforms. It
      starts at the end of setup with no restart, and removing the token
      removes the container.
- [ ] The browser extension (Chrome and Firefox) routes `.onion` through
      the daemon's proxy and shows the offline page when stopped. It is
      suspected broken in py2app builds; record what actually happens.
- [ ] Wayback sweep and the archive.org S3 key fetch with a test account.

## 14. The tor image rework, on local images (#559, P1 before an image publish)

e2e runs the published pins, so none of this has been run. Build local
images (`make images`, then `make dev-up`, or set `ONIONPRESS_TOR_IMAGE`
for a node) and run on one plain node and one **test** hub.

- [ ] Plain node: the healthcheck (now web-server.py's handler on
      127.0.0.1:8081) answers 200, and the daemon's dual probe still
      reports reachable. Nothing on port 8083 is mapped onto the address.
- [ ] Watchdog over the new `tor_control.py`:
  - kill it, and it is restarted
  - a republish request gives DEL_ONION/ADD_ONION and HS_DESC UPLOADED
    with the same address
  - sleep/wake (the USR1/USR2 path) still removes and re-adds the
    service
- [ ] Damaged C Tor key files (truncate `hs_ed25519_secret_key`, edit
      `hostname`). They are re-derived from the PEM by
      `onion_keys.build_ctor`, the old files are set aside, and the
      address is the same.
- [ ] A backup holds the PEM, which `key_manager` now reads through
      `parse_ctor_secret`. A restore of it on another node serves the same
      address.
- [ ] Hub: a node registers; then stop the node. The takeover now goes
      through the worker's queue manager, since `onionheaven-tor-manager.sh`
      is gone. The takeover page is served at the node's address, and it
      is released when the node returns.
- [ ] Hub: the OnionHeaven redirect (now `onionheaven-redirect.py`) works,
      and an idle connection is dropped after about 30 s.
- [ ] OnionHome on a test node: onionnames resolve (`onionnames.py` now
      uses the shared parser).

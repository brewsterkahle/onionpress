# OnionPress Project Memory

## Meta
- **This file (`CLAUDE.md`) is the project memory.** Store all new memories and notes here so they travel with the repo.

## Naming Rules (IMPORTANT)
- The project is called **OnionPress** (one word, capital O and P). Never "Onion.Press", "onion.press", or "onion-press".
- Data directory: `~/.onionpress/` (not `~/.onion.press/`)
- GitHub repo: `brewsterkahle/onionpress`
- Use **"onion service"** (not "hidden service") in all user-facing text. Tor Project deprecated "hidden service" terminology.
  - Exception: file paths like `/var/lib/tor/hidden_service/` and Docker image names like `goldy/tor-hidden-service` cannot change — those are external identifiers.
- When writing new code, docs, issues, or UI text, always use "OnionPress" and "onion service".

## Key Architecture
- macOS menubar app (py2app built from `src/menubar.py`)
- Launcher shell script at `app/MacOS/onionpress` (assembled into `OnionPress.app/Contents/MacOS/` at build time)
- Docker containers (tor, wordpress, mariadb) run inside Colima VM
- Logs at `~/.onionpress/onionpress.log` and `~/.onionpress/launcher.log`
- User-visible content at `~/OnionPress/` (backups, Creations)

## Repo Layout
- `src/menubar.py` — py2app entry point (the only flat module; everything else lives in the package)
- `src/onionpress/` — shared Python package (all non-entry-point code)
- `app/` — macOS .app bundle source (assembled into `OnionPress.app/` at build time)
  - `app/Info.plist` — canonical version source #2
  - `app/MacOS/` — launcher scripts, Swift wrapper source
  - `app/Resources/` — docker configs, plugins, icons, templates
- `OnionPress.app/` — **gitignored build output**, assembled by `build/build-dmg-simple.sh`

## Why py2app
- Modern Macs do NOT ship a usable Python — `/usr/bin/python3` is just a shim that prompts to install Xcode CLI Tools
- Apple removed Python 2 in macOS 12.3 and has no commitment to shipping Python 3 long-term
- py2app bundles the Python interpreter + all dependencies into a self-contained .app so the user never needs to know Python is involved
- This is essential for a consumer app — cannot ask non-technical users to install Xcode Command Line Tools

## Build & Release Process
- MenubarApp built with py2app via `setup.py` (extracted from `build/build-dmg-simple.sh` lines 228-276)
- **After editing ANY `src/` file that the MenubarApp uses, you MUST rebuild the MenubarApp** via `build/rebuild-menubar.sh`. The py2app bundle contains compiled `.pyc` files — editing `src/` alone does NOT update the running app.
- **py2app entry point**: `MenubarApp/Contents/Resources/menubar.py` is the ONLY copy that matters at runtime — py2app `exec()`s it from `__boot__.py`. The copies in `lib/python3.14/` and `scripts/` are unused build artifacts. When hot-patching the installed app without a full rebuild, only update `Contents/Resources/menubar.py` (and its `__pycache__/` pyc).
  - `src/menubar.py` — main app (entry point)
  - `src/onionpress/` — shared package: backup, key_manager, setup_window, onion_proxy, onion_auth, onionheaven, updater, install_native_messaging, native_messaging_host, power, health, docker, tor, colima, platform, config, containers, ui_helpers, settings_ui, browser, log_rotation, analytics_sharing, onionnames_*
  - `setup.py` — py2app config. Adding a new submodule to `onionpress` just means one new line in the `includes` list — no build-script change needed. Build scripts only copy the whole `onionpress` package into site-packages.
- **Release via GitHub releases only** (`gh release create`). Do NOT upload to Internet Archive.
- **Cut releases with `build/release.sh`** — it builds BOTH artifacts and uploads both to one release, so the Linux `.deb` can't be forgotten. (It was: v2.4.101–v2.4.106 shipped only the `.dmg`, which 404'd the README's `releases/latest/download/onionpress.deb` link — that link resolves to the *Latest* release's asset of that name.) Bump + commit first, then run it. Cross-platform rule it enforces: the `.dmg` can only be built on macOS, so on **macOS** it builds `.dmg`+`.deb` and creates/updates the release with both; on **Linux** it builds only the `.deb` and *refuses to create* a release (a `.dmg`-less "Latest" would break the Mac link) — it only attaches the `.deb` to an existing release that already carries the matching `.dmg`. Always run the Mac side first.
- **Version bumping**: run `build/bump-version.sh X.Y.Z` — it updates all version locations automatically. The 2 canonical sources are `src/menubar.py` (`self.version`) and `app/Info.plist` (`CFBundleShortVersionString`). Derived locations (`src/onionpress/__init__.py`, `setup.py` which reads menubar.py dynamically, MenubarApp plist) are updated by the bump script or at build time. The quit log in menubar.py uses `self.version` dynamically. Docker containers get the version via `ONIONPRESS_VERSION` env var from the launcher script (which reads Info.plist).
- **py2app vs setuptools 81+ incompatibility** — setuptools 81 (released 2026-02-06) removed `dry_run` from `distutils.spawn()`, which py2app 0.28.9 still uses. The build script (`build/build-dmg-simple.sh`) handles this automatically: it tries the build first, and falls back to `setuptools<81` only if py2app fails. Once py2app ships a fix, the fallback stops being needed. Track upstream: https://github.com/ronaldoussoren/py2app/issues/557

## Security
- **Database passwords are randomly generated per install** — never use defaults or hardcoded passwords. The `ensure_secrets` function generates unique passwords with `openssl rand` on first run, saved to `~/.onionpress/secrets`.
- Do not commit or log database passwords.

## Colima VM Sandboxing
- The VM has two narrow mounts: `~/.onionpress/shared:w` and `~/OnionPress:w`
- This limits blast radius if a container is compromised — attacker can only see vanity keys and user's published content (which is public anyway)
- `~/.onionpress/` stays as-is for app state (no spaces in path — required by Colima/Lima/Docker socket paths)
- `~/OnionPress/` holds user-visible backups and Creations
- All other container data uses Docker named volumes (which live inside the VM)
- **Do not move app state to `~/Library/Application Support/`** — the space in the path breaks Colima/Docker socket paths (104-char Unix socket limit + space handling issues)
- **Diffdisk cap is 20 GiB on new installs** (issue #230) via `--disk` to the first `colima start` in `app/MacOS/launcher.sh` and `src/onionpress/colima.py`. The cap is set at VM creation and is immutable — existing installs keep their original 100 GiB cap. Dangling Docker images auto-prune after each successful image pull (`update_docker_images()` in `src/menubar.py` and the bash launchers), keeping real usage well under 5 GiB in practice. The cap is configurable via `VM_DISK=N` in `~/.onionpress/config` *before* first launch; when the OnionHeaven hub vanity key is present at first start, the cap auto-bumps to 100 GiB for takeover-container headroom (hub is the edge case; normal nodes never need more than 20).
- **Do not add additional `--mount` flags without considering security implications**

## Multi-User Support (v2.4.11+)
- Multiple macOS users can run OnionPress simultaneously from the same `/Applications/OnionPress.app`
- Each user gets their own `~/.onionpress/` data dir, Colima VM, and Docker containers
- **Port offsets**: second user auto-detects port 8080 is taken and offsets all ports by +10000 (18080/19050/19077), third user by +20000, etc. Max ~5 users.
- **Detection uses socket bind test**, not `lsof` — `lsof` only sees the current user's processes and cannot detect ports bound by other users
- **Port detection must happen in the MenubarApp's `__init__`** (Python socket bind), not in the shell scripts — the MenubarApp launches first and needs the correct ports before the `onionpress` script runs
- **Module-level constants in `onionpress.onion_proxy`** (`PROXY_PORT`, `PHP_PROXY_PORT`) are set at import time. The MenubarApp must update these globals after detecting the offset: `onion_proxy.PROXY_PORT = self.proxy_port`
- The `onionpress` shell script also has detection as a fallback (for standalone use), but respects pre-set `ONIONPRESS_PORT_OFFSET` env var from the MenubarApp
- **`LSMultipleInstancesProhibited` must NOT be in any Info.plist** — macOS enforces it across ALL users sharing the same app bundle, not just per-user
- **`pgrep` in the launcher must use `-u $(whoami)`** to restrict to the current user's processes
- **PID lock file** (`~/.onionpress/onionpress.pid`) prevents the same user from double-launching; cleaned up via `trap` on EXIT/INT/TERM/HUP
- **Container-internal ports are NOT offset** — Docker networking (`onionpress-tor:9050`, `wordpress:80`) is isolated per-VM. Only host-side port mappings change.
- `OnionPress.app/` is gitignored — it is assembled at build time from `app/` source. Never commit build output.

## Colima Networking Gotcha
- **SOCKS proxy (port 9050) does NOT work through Colima VM port forwarding** — connections are accepted then immediately closed
- **For ANY communication over Tor from the Mac, always use `docker exec` into the tor container** — this is reliable
  - Do NOT use `curl --socks5-hostname 127.0.0.1:9050` from the Mac host — it will fail
- This applies to future mirror system communication (health checks, challenge-response, etc.)
- **`wget` inside the tor container CANNOT fetch external .onion addresses** — it doesn't support SOCKS proxies, so it can't resolve .onion via Arti. `wget` only works for internal container-to-container requests (e.g., `wget http://wordpress:80/`).
- **To fetch external .onion addresses, use `socat` with SOCKS4A** inside the tor container:
  - `printf "GET / HTTP/1.1\r\nHost: <address>.onion\r\nConnection: close\r\n\r\n" | socat -t 10 - SOCKS4A:127.0.0.1:<address>.onion:80,socksport=9050`
  - Or install `curl` in the tor container and use `curl --socks5-hostname 127.0.0.1:9050`
- WordPress container has `curl`
- Test onion service path (internal): `docker exec onionpress-tor wget -q -O /dev/null http://wordpress:80/`

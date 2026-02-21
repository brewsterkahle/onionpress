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
- Launcher shell script at `OnionPress.app/Contents/MacOS/onionpress`
- Docker containers (tor, wordpress, mariadb) run inside Colima VM
- Logs at `~/.onionpress/onionpress.log` and `~/.onionpress/launcher.log`

## Why py2app
- Modern Macs do NOT ship a usable Python — `/usr/bin/python3` is just a shim that prompts to install Xcode CLI Tools
- Apple removed Python 2 in macOS 12.3 and has no commitment to shipping Python 3 long-term
- py2app bundles the Python interpreter + all dependencies into a self-contained .app so the user never needs to know Python is involved
- This is essential for a consumer app — cannot ask non-technical users to install Xcode Command Line Tools

## Build & Release Process
- MenubarApp built with py2app via `setup.py` (extracted from `build/build-dmg-simple.sh` lines 228-276)
- Must copy `key_manager.py` and `bip39_words.py` to venv site-packages before build
- After editing `src/menubar.py`, rebuild binary and replace `OnionPress.app/Contents/Resources/MenubarApp/`
- **Release via GitHub releases only** (`gh release create`). Do NOT upload to Internet Archive.
- Version must be bumped in **all 5 locations** (Finder shows the MenubarApp plist version in Get Info):
  1. `src/menubar.py` — `self.version = "X.Y.Z"` (~line 529)
  2. `src/menubar.py` — `self.log("QUIT BUTTON CLICKED - vX.Y.Z RUNNING")` (~line 3446)
  3. `setup.py` — `CFBundleVersion` and `CFBundleShortVersionString` (2 values, same line area)
  4. `OnionPress.app/Contents/Info.plist` — `CFBundleShortVersionString`
  5. `OnionPress.app/Contents/Resources/MenubarApp/Contents/Info.plist` — `CFBundleShortVersionString` AND `CFBundleVersion` (py2app build artifact; the build script overwrites this, but it must also be updated manually for non-rebuild releases)
- **py2app vs setuptools 81+ incompatibility** — setuptools 81 (released 2026-02-06) removed `dry_run` from `distutils.spawn()`, which py2app 0.28.9 still uses. The build script (`build/build-dmg-simple.sh`) handles this automatically: it tries the build first, and falls back to `setuptools<81` only if py2app fails. Once py2app ships a fix, the fallback stops being needed. Track upstream: https://github.com/ronaldoussoren/py2app/issues/557

## Security
- **Database passwords are randomly generated per install** — never use defaults or hardcoded passwords. The `ensure_secrets` function generates unique passwords with `openssl rand` on first run, saved to `~/.onionpress/secrets`.
- Do not commit or log database passwords.

## Colima VM Sandboxing
- The VM is restricted to only mount `~/.onionpress/` (via `--mount "$DATA_DIR:w"`), NOT the full home directory
- This limits blast radius if a container (e.g., WordPress) is compromised — attacker cannot read `~/Documents`, `~/.ssh/`, etc.
- The only host bind mount needed is for vanity key import (`~/.onionpress/shared/vanity-keys/`)
- All other container data uses Docker named volumes (which live inside the VM)
- **Do not add additional `--mount` flags without considering security implications**

## Colima Networking Gotcha
- **SOCKS proxy (port 9050) does NOT work through Colima VM port forwarding** — connections are accepted then immediately closed
- **For ANY communication over Tor from the Mac, always use `docker exec` into the tor container** — this is reliable
  - Example: `docker exec onionpress-tor wget -q -O - http://some-address.onion/`
  - Do NOT use `curl --socks5-hostname 127.0.0.1:9050` from the Mac host — it will fail
- This applies to future mirror system communication (health checks, challenge-response, etc.)
- `wget` is available in the tor container (Alpine-based), `curl` is not
- WordPress container has `curl`
- Test onion service path: `docker exec onionpress-tor wget -q -O /dev/null http://wordpress:80/`

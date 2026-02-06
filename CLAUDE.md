# Onion.Press Project Memory

## Meta
- **This file (`CLAUDE.md`) is the project memory.** Store all new memories and notes here so they travel with the repo.

## Key Architecture
- macOS menubar app (py2app built from `src/menubar.py`)
- Launcher shell script at `Onion.Press.app/Contents/MacOS/onion.press`
- Docker containers (tor, wordpress, mariadb) run inside Colima VM
- Logs at `~/.onion.press/onion.press.log` and `~/.onion.press/launcher.log`

## Why py2app
- Modern Macs do NOT ship a usable Python — `/usr/bin/python3` is just a shim that prompts to install Xcode CLI Tools
- Apple removed Python 2 in macOS 12.3 and has no commitment to shipping Python 3 long-term
- py2app bundles the Python interpreter + all dependencies into a self-contained .app so the user never needs to know Python is involved
- This is essential for a consumer app — cannot ask non-technical users to install Xcode Command Line Tools

## Build & Release Process
- MenubarApp built with py2app via `setup.py` (extracted from `build/build-dmg-simple.sh` lines 228-276)
- Must copy `key_manager.py` and `bip39_words.py` to venv site-packages before build
- After editing `src/menubar.py`, rebuild binary and replace `Onion.Press.app/Contents/Resources/MenubarApp/`
- **Release via GitHub releases only** (`gh release create`). Do NOT upload to Internet Archive.
- Version must be bumped in: `src/menubar.py` (2 places), `setup.py`, `Onion.Press.app/Contents/Info.plist`

## Colima Networking Gotcha
- **SOCKS proxy (port 9050) does NOT work through Colima VM port forwarding** — connections are accepted then immediately closed
- **For ANY communication over Tor from the Mac, always use `docker exec` into the tor container** — this is reliable
  - Example: `docker exec onionpress-tor wget -q -O - http://some-address.onion/`
  - Do NOT use `curl --socks5-hostname 127.0.0.1:9050` from the Mac host — it will fail
- This applies to future mirror system communication (health checks, challenge-response, etc.)
- `wget` is available in the tor container (Alpine-based), `curl` is not
- WordPress container has `curl`
- Test onion service path: `docker exec onionpress-tor wget -q -O /dev/null http://wordpress:80/`

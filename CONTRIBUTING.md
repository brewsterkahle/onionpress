# Contributing to OnionPress

Thanks for your interest in contributing. OnionPress is a single-maintainer
project today, transitioning to take outside contributions. This document
covers what you need to know.

## What OnionPress is

A consumer-friendly way to publish a WordPress site as a Tor onion service.
On macOS it ships as a menubar app; on Linux as a `.deb` / AppImage with a
systemd service. WordPress, MariaDB, and Tor run as Docker containers
(inside Colima on macOS, native Docker on Linux). The host-side wrapper
handles startup, port detection, key management, backups, and updates.

## Repo layout

| Path | What lives there |
| --- | --- |
| `src/menubar.py` | The macOS menubar app (`py2app` entry point). |
| `src/onionpress/` | Shared Python package — CLI, updater, backup, OnionHeaven, key management, etc. |
| `app/` | macOS `.app` bundle source (assembled into `OnionPress.app/` at build time). |
| `linux/` | Linux launcher script and systemd units. |
| `app/Resources/plugins/` | WordPress plugins (mu-plugins) shipped with the app. |
| `app/Resources/docker/` | Docker Compose file and per-container configs. |
| `tests/` | Python unit tests (pytest / unittest). |
| `build/` | Build scripts (DMG, .deb, AppImage, version bumping). |

## Naming and terminology

- The project is **OnionPress** (one word, capital O and P). Never "Onion.Press"
  or "onion.press".
- Use **"onion service"**, not "hidden service" — the Tor Project deprecated
  that term. (The exception is unavoidable external identifiers like the
  `goldy/tor-hidden-service` Docker image name.)
- Data directory is `~/.onionpress/`. User-visible content lives in
  `~/Documents/OnionPress/`.

## Setting up a dev environment (macOS)

1. Install Python 3.14 from python.org (or homebrew). `py2app` needs the
   universal2 build, which the python.org installer provides.
2. `git clone` the repo and `cd` into it.
3. Run the existing tests to confirm your environment works:

   ```
   python3 -m unittest tests.test_onionpress_cli
   ```

4. To rebuild the menubar app after editing `src/`:

   ```
   build/rebuild-menubar.sh
   ```

   This takes a couple of minutes. After running, **quit and relaunch
   `OnionPress.app`** — `src/` edits don't take effect in the running
   process, only in a fresh launch.

5. To launch from a clean state:

   ```
   /Applications/OnionPress.app/Contents/MacOS/onionpress-cli quit
   open /Applications/OnionPress.app
   ```

## Setting up a dev environment (Linux)

The Linux build is scripts-only (no compiled binaries). `build/build-linux.sh`
produces a `.deb` and an AppImage. Test the launcher directly:

```
linux/onionpress start
linux/onionpress logs
linux/onionpress stop
```

Containers run via native Docker, no Colima.

## Running tests

Unit tests live in `tests/`. Run with either runner:

```
python3 -m unittest tests.test_onionpress_cli       # builtin
pytest tests/                                        # if installed
```

CI runs the same tests on every PR; broken tests block merge.

## Code style

- **Default to no comments.** The codebase generally avoids comments that
  describe what code does. Reserve comments for the *why* — non-obvious
  constraints, workarounds for specific bugs, or invariants that aren't
  visible from the code.
- **Don't add error handling for impossible cases.** Trust internal calls
  and framework guarantees. Only validate at system boundaries (user
  input, external APIs, container output).
- **`subprocess.run` reading `.stdout`/`.stderr` MUST use
  `text=True, encoding='utf-8', errors='replace'`** — without this,
  non-ASCII characters from Tor logs (✓, —) cause crashes. Calls that
  only check `.returncode` and discard output are fine without it.
- **Database passwords are randomly generated per-install.** Never use
  defaults or hardcoded passwords. Don't commit or log passwords.
- **No clearnet requests from Docker containers.** Anything reaching
  outside the local machine must go through Tor SOCKS (`socks5h://onionpress-tor:9050`
  inside containers, the WordPress container's `onionpress_curl_tor()`
  helper for PHP). Direct curl to `https://archive.org` etc. is a leak
  bug, not a convenience.

## Making a change

1. Open an issue first for anything beyond a small fix, so we can
   discuss approach before you spend time. Pre-existing tagged
   "good first issue" items already have agreed scope.
2. Fork the repo and create a branch off `main`.
3. Make your change. Keep PRs focused — one logical change per PR.
4. Update or add tests where it makes sense.
5. Commit with a descriptive message. Follow the existing style:
   short subject (under 70 chars), then a blank line, then a body
   explaining the *why*. Reference issues with `(#NNN)`.
6. Open a PR against `main`. The PR template will ask you to describe
   the change, link the issue, and explain how you tested it.
7. CI will run the unit tests. If they fail, fix them before review.

## Things that stay with the maintainer

These aren't open to contributors, at least for now:

- **Cutting releases.** Release tagging, DMG signing/notarization, and
  upload to GitHub Releases require credentials only the maintainer has.
- **`.github/workflows/docker-publish.yml`** — controls what
  `ghcr.io/brewsterkahle/onionpress-*` images contain. Supply chain;
  changes go through extra review.
- **The OnionHeaven hub registration protocol.** Changes affect every
  running install; coordinate with the maintainer before touching it.

You're welcome to propose changes to all of these via PR — merge stays
with the maintainer.

## Build pipeline gotchas

- **`py2app` vs `setuptools` 81+** — setuptools 81 (released 2026-02-06)
  removed `dry_run` from `distutils.spawn()`, which py2app 0.28.9 still
  uses. `build/build-dmg-simple.sh` handles this with a fallback to
  `setuptools<81`. If you hit a similar issue locally, check that.
- **Bundled Python.** Modern macOS doesn't ship a usable Python.
  py2app embeds the interpreter. Anywhere shell code runs Python, it
  must use the bundled binary at
  `OnionPress.app/Contents/Resources/MenubarApp/Contents/MacOS/python`,
  never the system `python3`.
- **Two `Info.plist` files.** `OnionPress.app/Contents/Info.plist`
  (the parent) and `OnionPress.app/Contents/Resources/MenubarApp/Contents/Info.plist`
  (py2app's) must agree. `build/rebuild-menubar.sh` syncs them; if you
  bypass it, expect the WP page to show a stale version.

## Reporting bugs

For non-security bugs, open an issue with the
[bug report template](.github/ISSUE_TEMPLATE/bug_report.md). Include
your OS version, OnionPress version (visible at the top of WP admin
or via `onionpress-cli status`), and the relevant log lines from
`~/.onionpress/onionpress.log`.

For security issues, see [SECURITY.md](SECURITY.md) — please report
privately, not in a public issue.

## Asking questions

GitHub Discussions or issues with the "question" label are fine.
Real-time chat doesn't exist yet.

## License

By contributing, you agree your contribution is licensed under the
same terms as the project (see [LICENSE](LICENSE)).

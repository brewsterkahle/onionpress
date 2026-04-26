# Security

OnionPress is software that helps people publish on Tor. Bugs that could
expose a publisher's identity, leak content out of Tor, or weaken the
isolation between containers are taken seriously.

## Reporting a vulnerability

**Please report privately.** Don't open a public GitHub issue for security
bugs.

Two channels, in order of preference:

1. **GitHub Security Advisory** — open a private advisory at
   <https://github.com/brewsterkahle/onionpress/security/advisories/new>.
   This is the preferred channel; it's encrypted, only visible to the
   maintainer, and has a clean disclosure-coordination workflow.

2. **Email** — `brewster@archive.org`. Mention "OnionPress security" in the
   subject. PGP is available on request.

## What we'll do

- Acknowledge your report within **3 business days**.
- Investigate and reply with an initial assessment within **7 days**.
- Coordinate disclosure with you. Most fixes ship in the next regular
  release; if a bug is being actively exploited, we'll cut an emergency
  release.
- Credit you in the release notes if you'd like that (or keep your report
  anonymous if you'd prefer).

## What's in scope

- The macOS menubar app (`src/menubar.py`, `src/onionpress/`,
  `app/MacOS/`).
- The Linux launcher (`linux/`).
- WordPress mu-plugins shipped with OnionPress (`app/Resources/plugins/`).
- The Docker container configurations
  (`app/Resources/docker/{tor,wordpress}/`).
- The OnionHeaven hub protocol and our heartbeat client.
- The auto-update flow (DMG download, signature checking, install).

In particular, please report:

- Anything that could leak an onion service publisher's IP or real-world
  identity.
- Clearnet requests from inside containers that should go via Tor (the
  "no clearnet" rule — `socks5h://onionpress-tor:9050` inside containers,
  `onionpress_curl_tor()` for PHP).
- Persistent or stored XSS in the OnionPress-shipped WordPress plugins.
- Privilege escalation between containers, or container → host escapes
  via our shared mounts (`~/.onionpress/shared`, `~/Documents/OnionPress`).
- Authentication bypass in the WP admin auto-login flow or the
  OnionHeaven registration protocol.
- Tampering attacks against the auto-update path (replace the DMG between
  download and install, etc.).

## What's out of scope

- WordPress core / third-party plugin vulnerabilities. Report those to
  the WordPress security team or the relevant plugin author. We track
  upstream advisories and ship updated images, but the bugs themselves
  aren't ours to fix.
- Tor Project software (Arti, C Tor, the bundled `tor` binary). Report
  those to Tor.
- Docker, Colima, or Lima vulnerabilities. Same — upstream reports.
- Issues that require physical access to the user's Mac, or that
  require the attacker to already have root on the user's machine.
- Theoretical timing attacks against `tor` itself (Tor's threat model
  documents what it does and doesn't defend against).

## Threat model

Quick summary of who we try to defend against:

- **Network adversaries** observing traffic between the publisher's
  machine and the rest of the internet. Tor handles this; OnionPress
  must not bypass Tor.
- **Visitors** to the onion site. They shouldn't be able to deanonymize
  the publisher, escape to the host, or escalate from a low-privilege
  WP account to admin.
- **Adversaries with physical access** to the publisher's Mac during
  short absences. This is mostly out of scope (full-disk encryption is
  the user's responsibility), but we shouldn't make it easier — e.g.
  no plaintext credentials in `~/.onionpress/` that aren't already
  loaded by Docker for the running containers.

What we **do not** defend against:

- Running OnionPress on a compromised machine. If the OS is owned, so
  is OnionPress.
- Users who deliberately weaken security (e.g. exporting their onion
  service private key, sharing their database password).
- Side-channel attacks that have only theoretical proofs of concept;
  we'll triage but probably won't ship a fix until they're practical.

## Responsible disclosure

Please give us a reasonable window to fix and ship before public
disclosure. The default is **90 days from the date we acknowledge your
report**. We'll work with you on the timeline if a fix needs more time
or if there's reason to disclose sooner.

<div align="center">
  <img src="assets/branding/logo.png" alt="OnionPress logo" width="400">

  **Your Decentralized Social Blog Site**

  **[Website](https://onionpress.org)** · **[Download](https://github.com/brewsterkahle/onionpress/releases/latest)**
</div>

# OnionPress

**Run your own website from your computer. Just Works. Free, forever.**

OnionPress turns your Mac or Linux computer into a web server running WordPress, accessible via the Tor network. Your site gets its own permanent .onion address that no one can take away from you. It's backed up automatically by the Internet Archive's Wayback Machine.

**WordPress + Tor + Wayback Machine.** You write. Tor delivers. Wayback remembers.

## Features

- 📝 **Full WordPress**: Any theme, any plugin, a dashboard you already know
- 🧅 **Permanent .onion Address**: Your own address on the Tor network — no domain registrar, no DNS, uncensorable
- 🏛️ **Wayback Machine Integration**: Automatically archived and served when you're offline
- 🏠 **Works Behind Firewalls**: Home, school, work — Tor punches through NAT and firewalls
- 🔐 **Private by Default**: No analytics, no tracking, no ads. End-to-end encrypted without certificates
- 🐳 **Sandboxed**: Docker containers inside a VM for isolation and security
- 📱 **Mac Menu Bar App**: Purple onion icon with one-click controls
- 🐧 **Linux Support**: CLI + systemd for servers and Raspberry Pi

## Requirements

- **Mac**: macOS 13.0 (Ventura) or later
- **Linux**: Docker and Docker Compose
- Internet connection

## Installation

### Mac

1. Download [`onionpress.dmg`](https://github.com/brewsterkahle/onionpress/releases/latest/download/onionpress.dmg)
2. Drag `OnionPress.app` to Applications and launch
3. First launch takes 3-5 minutes (one-time setup)

### Linux

One command installs everything:

```bash
curl -sSL https://raw.githubusercontent.com/brewsterkahle/onionpress/main/linux/install.sh | bash
```

Or download the [`.deb` package](https://github.com/brewsterkahle/onionpress/releases/latest/download/onionpress.deb):

```bash
wget https://github.com/brewsterkahle/onionpress/releases/latest/download/onionpress.deb
sudo apt-get install ./onionpress.deb
sudo systemctl start onionpress
```

After install: `onionpress status`, `onionpress address`, `onionpress logs`.

### macOS Security Warning

On first launch, macOS will show a security warning (normal for open-source software). Go to **System Settings → Privacy & Security**, scroll down, and click **"Open Anyway"**.

## Usage

### Menu Bar Controls

On Mac, OnionPress appears in your menu bar:
- 🟣 **Purple** = running and available
- 🟡 **Yellow** = starting or reconnecting
- ⚪ **Gray** = stopped

## Viewing Your Site

Your site is viewable on any Tor-enabled browser:

- **[Tor Browser](https://www.torproject.org/download/)** — Windows, Mac, Linux (the gold standard)
- **[Brave Browser](https://brave.com/)** — Windows, Mac, Linux, Android (built-in Tor window)
- **[Onion Browser](https://apps.apple.com/app/onion-browser/id519296448)** — iPhone & iPad
- **[Tor Browser for Android](https://play.google.com/store/apps/details?id=org.torproject.torbrowser)** — Android

## Architecture

OnionPress is built on [WordPress](https://wordpress.org/), [Tor](https://www.torproject.org/), and the Internet Archive's [Wayback Machine](https://web.archive.org/).

All data is stored in:
- `~/.onionpress/` — Application state, logs, config
- `~/OnionPress/` — Backups and My Creations
- Docker volumes for WordPress content, database, and Tor keys

## Disk usage

OnionPress runs Docker inside a Colima VM on macOS. The VM's disk image
lives at `~/.onionpress/colima/_lima/colima/diffdisk` and is a **sparse
file** — the operating system only allocates real disk for blocks that
have been written to.

To see real vs. apparent size:

```
$ du -sh ~/.onionpress/colima/_lima/colima/diffdisk     # real allocation
3.2G    diffdisk

$ ls -lh ~/.onionpress/colima/_lima/colima/diffdisk     # apparent (= cap)
-rw-------  1 you  staff   20G May 19 13:50 diffdisk
```

Finder shows the **apparent** size, which is the configured cap. New
installs are capped at **20 GiB**; existing installs created before
that change keep their original 100 GiB cap (the cap is set when the
VM is first created and is not changed by upgrades). To override
before first launch, set `VM_DISK=N` (in GiB) in
`~/.onionpress/config`. OnionHeaven hub installs auto-bump to 100 GiB
to accommodate takeover containers at scale.

Real disk usage stays small: after months of typical use, expect 3–7 GB
for the full `~/.onionpress/` tree. The menubar's "Check for Updates"
flow automatically prunes dangling Docker image versions after each
successful image pull, so old releases don't accumulate on disk.

If you want to apply the smaller 20 GiB cap to an existing install:

```
onionpress backup          # IMPORTANT: back up first — data loss otherwise
/Applications/OnionPress.app/Contents/MacOS/onionpress quit
colima delete onionpress   # destroys the VM and its diffdisk
open /Applications/OnionPress.app  # next launch creates a fresh 20 GiB VM
onionpress restore <backup-path>
```

This is a manual procedure because shrinking the diffdisk on a running
install requires recreating the VM. Most users will never need to do
this — sparse files mean the existing 100 GiB cap is a Finder display
quirk, not actual disk consumption.

## Building from Source

Everything OnionPress ships — including the container images — can be rebuilt
on your own machine. Nothing requires access to the project's CI, its registry
credentials, or its self-hosted build runner.

```bash
make doctor        # which build tools you have, and what each missing one costs

make images        # the tor + wordpress container images   (any OS)
make dev-up        # run the stack on the images you just built
make dmg           # the macOS installer                    (macOS only)
make deb           # the Linux package                      (any OS)
make extension     # the Chrome + Firefox extensions        (any OS)
```

See **[docs/BUILDING.md](docs/BUILDING.md)** for what each component needs,
which inputs are pinned and how to bump them, and how to point an installed
OnionPress at images you built yourself.

## Uninstalling

- **Mac**: Click **Uninstall** from the menu bar app. It will remove all data and quit.
- **Linux**: Run `onionpress uninstall`

## License

AGPL 3 License - See LICENSE file for details

## Credits

A [Decentralized Web](http://brewster.kahle.org/2015/08/11/locking-the-web-open-a-call-for-a-distributed-web-2/) project.

Free and Open Source (AGPL 3). [Donate to the Tor Project](https://donate.torproject.org/) · [Donate to the Internet Archive](https://archive.org/donate/)

## Support

For issues, questions, or contributions, visit the [GitHub repository](https://github.com/brewsterkahle/onionpress) or the [product page](https://onionpress.org).

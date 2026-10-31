#!/bin/bash

# OnionPress Installer for Raspberry Pi / Linux
# Usage: curl -sSL https://raw.githubusercontent.com/brewsterkahle/onionpress/main/linux/install.sh | bash
#
# Or clone the repo and run: bash linux/install.sh
#
# Installs Docker in rootless mode for security — container compromise
# cannot escalate to host root.

set -e

# Everything is inside main(), run by the last line (issue #388): bash
# parses the whole function before it runs any of it, so a `curl | bash`
# whose connection drops part-way executes nothing rather than a prefix.
main() {

INSTALL_DIR="/opt/onionpress"
REPO_URL="https://github.com/brewsterkahle/onionpress"
# The fingerprint of Docker's apt signing key (docs.docker.com, "Install
# using the apt repository"): the key is fetched over TLS and checked
# against this before apt is pointed at the repository (issue #388).
DOCKER_APT_KEY_FPR="9DC858229FC7DD38854AE2D88D81803C0EBFCD88"

# Resolve real user (not root when run via sudo)
if [ -n "$SUDO_USER" ]; then
    REAL_USER="$SUDO_USER"
    REAL_HOME=$(getent passwd "$SUDO_USER" | cut -d: -f6)
    REAL_UID=$(id -u "$SUDO_USER")
else
    REAL_USER="$(whoami)"
    REAL_HOME="$HOME"
    REAL_UID=$(id -u)
fi
DATA_DIR="$REAL_HOME/.onionpress"
USER_SYSTEMD_DIR="$REAL_HOME/.config/systemd/user"

# What this run leaves behind on exit, whether it finishes or is
# interrupted: the folder it clones the repo into when not run from a
# checkout (nothing when it is), and the journal tail it starts while the
# site comes up. Only these two, by variables of this script's own: never
# a folder named by the environment. Through 2.5.6 the clone was kept in
# TMPDIR, the standard temp-folder variable (libpam-tmpdir sets it to
# /tmp/user/<uid>; people set it in their profile), and `rm -rf "$TMPDIR"`
# ran whether or not anything had been cloned (issue #346).
CLONE_DIR=""
JOURNAL_PID=""
cleanup() {
    if [ -n "$JOURNAL_PID" ]; then
        kill "$JOURNAL_PID" 2>/dev/null || true
    fi
    if [ -n "$CLONE_DIR" ]; then
        rm -rf -- "$CLONE_DIR"
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo ""
echo "  OnionPress Installer for Linux"
echo "  ==============================="
echo ""

# ─── Checks ──────────────────────────────────────────────────────────

# Check architecture
ARCH=$(uname -m)
case "$ARCH" in
    aarch64|arm64)
        echo "  Architecture: ARM64 (Raspberry Pi / Apple Silicon)"
        ;;
    x86_64)
        echo "  Architecture: x86_64"
        ;;
    armv7l)
        echo "  Architecture: ARM32 (may be slow, 64-bit OS recommended)"
        ;;
    *)
        echo "ERROR: Unsupported architecture: $ARCH"
        exit 1
        ;;
esac

# Check for root/sudo
if [ "$EUID" -ne 0 ]; then
    if ! command -v sudo >/dev/null 2>&1; then
        echo "ERROR: This script must be run as root or with sudo available."
        exit 1
    fi
    SUDO="sudo"
else
    SUDO=""
fi

# Refuse to run as root itself (not via sudo). Docker is set up in rootless
# mode for the user who will run OnionPress, and dockerd-rootless-setuptool.sh
# refuses to install for uid 0 — so a root login would get as far as
# installing Docker and then abort (issue #265). Fail before touching anything.
if [ "$REAL_UID" -eq 0 ]; then
    echo "ERROR: Do not run the installer as root."
    echo "  OnionPress runs Docker in rootless mode, which needs a normal user"
    echo "  account with sudo. Create one, switch to it, and rerun the installer:"
    echo ""
    echo "    adduser onionpress && usermod -aG sudo onionpress"
    echo "    su - onionpress"
    echo "    curl -sSL https://raw.githubusercontent.com/brewsterkahle/onionpress/main/linux/install.sh | bash"
    exit 1
fi

# ─── Install Docker (rootless) ──────────────────────────────────────

# True when the armored key file $1 has exactly Docker's published
# fingerprint (DOCKER_APT_KEY_FPR), read with gpg without importing it.
docker_apt_key_is_dockers() {
    local fpr
    fpr=$(gpg --batch --with-colons --import-options show-only --import "$1" 2>/dev/null \
          | awk -F: '$1 == "fpr" { print $10; exit }') || fpr=""
    [ -n "$fpr" ] && [ "$fpr" = "$DOCKER_APT_KEY_FPR" ]
}

# The ref to install when not run from a checkout (issue #388): the newest
# release tag (v<major>.<minor>.<patch>), never HEAD of main, so a push to
# main does not change what every new install runs as root, and a fresh
# install's version is a release's (what `onionpress update` compares
# against). ONIONPRESS_INSTALL_REF names another ref (a developer's
# branch). Fails with nothing printed when no release tag can be read.
release_ref_to_install() {
    local tag
    if [ -n "${ONIONPRESS_INSTALL_REF:-}" ]; then
        printf '%s\n' "$ONIONPRESS_INSTALL_REF"
        return 0
    fi
    tag=$(git ls-remote --tags --refs "$REPO_URL" 'refs/tags/v*' 2>/dev/null \
          | sed 's#.*refs/tags/##' \
          | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' \
          | sort -V | tail -n 1) || tag=""
    [ -n "$tag" ] || return 1
    printf '%s\n' "$tag"
}

# Helper to run a command as the real user (not root)
run_as_user() {
    if [ "$REAL_USER" = "$(whoami)" ]; then
        "$@"
    else
        sudo -u "$REAL_USER" "$@"
    fi
}

# Check if rootless Docker is already set up for this user
_rootless_sock="/run/user/$REAL_UID/docker.sock"
_docker_is_rootless=false
if [ -S "$_rootless_sock" ]; then
    if run_as_user env DOCKER_HOST="unix://$_rootless_sock" docker info >/dev/null 2>&1; then
        _docker_is_rootless=true
    fi
fi

if [ "$_docker_is_rootless" = "true" ]; then
    echo "  Docker: already installed, rootless ($(run_as_user docker --version | cut -d' ' -f3 | tr -d ','))"
else
    echo "  Installing Docker (rootless mode)..."

    # Install prerequisites for rootless Docker (gnupg: to read the apt
    # key's fingerprint below)
    $SUDO apt-get update -qq
    $SUDO apt-get install -y -qq uidmap dbus-user-session curl ca-certificates gnupg

    # Install Docker engine packages (needed for rootless setup tool)
    if ! command -v dockerd >/dev/null 2>&1; then
        # Add Docker's official GPG key and repository. The key is what
        # apt trusts for every Docker package from here on: its
        # fingerprint must be the published one, or nothing is added.
        $SUDO install -m 0755 -d /etc/apt/keyrings
        $SUDO curl -fsSL https://download.docker.com/linux/$(. /etc/os-release && echo "$ID")/gpg \
            -o /etc/apt/keyrings/docker.asc
        if ! docker_apt_key_is_dockers /etc/apt/keyrings/docker.asc; then
            $SUDO rm -f /etc/apt/keyrings/docker.asc
            echo "ERROR: the Docker apt key fetched from download.docker.com does not have"
            echo "       the published fingerprint $DOCKER_APT_KEY_FPR; not adding the repository."
            exit 1
        fi
        $SUDO chmod a+r /etc/apt/keyrings/docker.asc

        echo \
          "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
          https://download.docker.com/linux/$(. /etc/os-release && echo "$ID") \
          $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
          $SUDO tee /etc/apt/sources.list.d/docker.list > /dev/null

        $SUDO apt-get update -qq
        $SUDO apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin docker-ce-rootless-extras
    fi

    # Disable the system-wide (rootful) Docker daemon — we use rootless
    # instead — unless it is running someone's containers (issue #388; the
    # .deb's postinst makes the same check). Then it stays, and the
    # rootless setup below runs beside it (--force).
    if $SUDO docker ps -q 2>/dev/null | grep -q .; then
        echo "  The system Docker daemon runs containers: leaving it enabled."
        echo "  OnionPress uses its own rootless Docker beside it."
    else
        $SUDO systemctl disable --now docker.service docker.socket 2>/dev/null || true
        # Remove the socket file if it lingers after stopping (the rootless setup
        # tool refuses to proceed while /var/run/docker.sock exists)
        $SUDO rm -f /var/run/docker.sock
    fi

    # Enable lingering so the user's systemd services run at boot without login
    $SUDO loginctl enable-linger "$REAL_USER"

    # Set up rootless Docker as the real user
    # XDG_RUNTIME_DIR must be set for the setup tool
    run_as_user env XDG_RUNTIME_DIR="/run/user/$REAL_UID" \
        dockerd-rootless-setuptool.sh install --force

    # Start the user's rootless Docker daemon
    run_as_user env XDG_RUNTIME_DIR="/run/user/$REAL_UID" \
        systemctl --user start docker.service
    run_as_user env XDG_RUNTIME_DIR="/run/user/$REAL_UID" \
        systemctl --user enable docker.service

    echo "  Docker installed (rootless mode)"
fi

# Check docker compose plugin
if run_as_user docker compose version >/dev/null 2>&1; then
    echo "  Docker Compose: available"
else
    echo "  Installing Docker Compose plugin..."
    $SUDO apt-get update -qq
    $SUDO apt-get install -y -qq docker-compose-plugin
    echo "  Docker Compose plugin installed"
fi

# Ensure jq is available (used by status command)
if ! command -v jq >/dev/null 2>&1; then
    echo "  Installing jq..."
    $SUDO apt-get update -qq
    $SUDO apt-get install -y -qq jq
fi

# Ensure python3 is available
if ! command -v python3 >/dev/null 2>&1; then
    echo "  Installing python3..."
    $SUDO apt-get update -qq
    $SUDO apt-get install -y -qq python3
fi

# PyNaCl (libsodium) encrypts and decrypts backups (backup format 2) and
# signs OnionHeaven requests (its ed25519 core functions; without them the
# heartbeat signs in pure Python, not constant-time). The .deb depends on
# python3-nacl; a source install needs it just the same.
if ! python3 -c "import nacl.pwhash.argon2id; from nacl.bindings import crypto_secretstream_xchacha20poly1305_init_push, has_crypto_core_ed25519 as c, has_crypto_scalarmult_ed25519 as s; raise SystemExit(not (c and s))" >/dev/null 2>&1; then
    echo "  Installing python3-nacl (backup encryption, request signing)..."
    $SUDO apt-get update -qq
    $SUDO apt-get install -y -qq python3-nacl
fi

# Heartbeat daemon takes a systemd-logind delay inhibitor so /offline lands
# before suspend (NetworkManager tears down WiFi before any system-sleep
# hook can fire — only the DBus PrepareForSleep path beats it). Requires
# python3-dbus + python3-gi (GLib). Both are usually pre-installed on
# desktop images; ensure them here for headless boxes too.
if ! python3 -c "import dbus; from gi.repository import GLib" >/dev/null 2>&1; then
    echo "  Installing python3-dbus + python3-gi (for sleep-inhibitor)..."
    $SUDO apt-get install -y -qq python3-dbus python3-gi \
        || echo "  (python3-dbus/gi install failed — sleep-inhibitor will be skipped, heartbeat-timeout fallback still works)"
fi

# python3-socks lets the inhibitor POST /offline direct from the host
# through Tor's SOCKS5 port, skipping docker exec startup (~200ms).
# That's the margin we need to win the race against NetworkManager's
# WiFi teardown on suspend.
if ! python3 -c "import socks" >/dev/null 2>&1; then
    echo "  Installing python3-socks (for fast suspend /offline)..."
    $SUDO apt-get install -y -qq python3-socks \
        || echo "  (python3-socks install failed — sleep-inhibitor will fall back to docker exec)"
fi

# Ensure unzip and zip are available (needed for plugin installs and backups)
if ! command -v unzip >/dev/null 2>&1 || ! command -v zip >/dev/null 2>&1; then
    echo "  Installing zip/unzip..."
    $SUDO apt-get update -qq
    $SUDO apt-get install -y -qq zip unzip
fi

# Install torbrowser-launcher (Tor Project's verifier wrapper) so users can
# test their .onion site in real Tor Browser. Skip on headless boxes where
# no GUI is present — GTK deps would waste disk on a server/Pi that won't
# use them. xdg-open existence is a decent proxy for "has a desktop env".
if command -v xdg-open >/dev/null 2>&1 && ! command -v torbrowser-launcher >/dev/null 2>&1; then
    echo "  Installing torbrowser-launcher (for testing your .onion site)..."
    $SUDO apt-get install -y -qq torbrowser-launcher \
        || echo "  (torbrowser-launcher install failed — you can install it later: sudo apt install torbrowser-launcher)"
fi

# ─── Stop existing services (if reinstalling) ────────────────────────

# Stop user services (rootless)
if run_as_user env XDG_RUNTIME_DIR="/run/user/$REAL_UID" \
    systemctl --user is-active --quiet onionpress 2>/dev/null; then
    echo "  Stopping existing OnionPress service..."
    run_as_user env XDG_RUNTIME_DIR="/run/user/$REAL_UID" \
        systemctl --user stop onionpress-heartbeat 2>/dev/null || true
    run_as_user env XDG_RUNTIME_DIR="/run/user/$REAL_UID" \
        systemctl --user stop onionpress-watcher.timer 2>/dev/null || true
    run_as_user env XDG_RUNTIME_DIR="/run/user/$REAL_UID" \
        systemctl --user stop onionpress 2>/dev/null || true
fi

# Also stop old system-level services from previous installs
if $SUDO systemctl is-active --quiet onionpress 2>/dev/null; then
    echo "  Stopping old system-level OnionPress service..."
    $SUDO systemctl stop onionpress-heartbeat 2>/dev/null || true
    $SUDO systemctl stop onionpress-watcher.timer 2>/dev/null || true
    $SUDO systemctl stop onionpress 2>/dev/null || true
    $SUDO systemctl disable onionpress onionpress-heartbeat onionpress-watcher.timer 2>/dev/null || true
    $SUDO rm -f /etc/systemd/system/onionpress.service \
                /etc/systemd/system/onionpress-heartbeat.service \
                /etc/systemd/system/onionpress-watcher.service \
                /etc/systemd/system/onionpress-watcher.timer
    $SUDO systemctl daemon-reload
    echo "  Migrated from system services to user services"
fi

# ─── Install OnionPress files ────────────────────────────────────────

echo ""
echo "  Installing OnionPress to $INSTALL_DIR..."

$SUDO mkdir -p "$(dirname "$INSTALL_DIR")"

# Determine source: if we're in the repo, use local files; otherwise clone
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/onionpress" ] && [ -d "$SCRIPT_DIR/../src/onionpress" ]; then
    # Running from cloned repo. (We previously gated on OnionPress.app/ but
    # that's macOS-only build output and gitignored — it never exists on a
    # Linux dev clone, so the check fell through to a fresh GitHub clone.)
    REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
    echo "  Source: local repo at $REPO_DIR"
else
    # Download the latest release from GitHub (never HEAD of main).
    if ! INSTALL_REF=$(release_ref_to_install); then
        echo "ERROR: could not read the latest release tag from $REPO_URL."
        echo "       Check the network, or set ONIONPRESS_INSTALL_REF=<tag> and rerun."
        exit 1
    fi
    echo "  Downloading $INSTALL_REF from GitHub..."
    CLONE_DIR=$(mktemp -d)
    git clone --depth 1 --branch "$INSTALL_REF" "$REPO_URL" "$CLONE_DIR/onionpress"
    REPO_DIR="$CLONE_DIR/onionpress"
fi

# Verify source directory exists
if [ ! -d "$REPO_DIR/app/Resources/docker" ]; then
    echo "ERROR: Docker resources not found at $REPO_DIR/app/Resources/docker"
    echo "       Make sure you're running from the full OnionPress repo."
    exit 1
fi

# Install the files: the one list in linux/install-tree.sh (the .deb's and
# `onionpress update --from-checkout`'s too), staged beside $INSTALL_DIR and
# renamed into place, so a reinstall replaces the whole tree. The `cp -r`
# of each folder this replaces copied into docker/docker/ (and plugins/,
# themes/...) when the folder was already there, and the old compose file
# stayed in use.
$SUDO bash "$REPO_DIR/linux/install-tree.sh" install "$REPO_DIR" "$INSTALL_DIR"

# Create /usr/local/bin symlinks NOW, before the tray is started below —
# the tray's SetupDialog shells out to `/usr/local/bin/onionpress
# provision-post-install` to install the theme + convert to multisite,
# and a quick user can complete Setup before we'd otherwise reach the
# symlink step at the bottom of this script. Missing symlink = silent
# provision-post-install failure = WP comes up themeless and single-site.
$SUDO mkdir -p /usr/local/bin
$SUDO ln -sf "$INSTALL_DIR/onionpress" /usr/local/bin/onionpress
$SUDO ln -sf "$INSTALL_DIR/onionpress-tray" /usr/local/bin/onionpress-tray

# Bring up the tray immediately — with the binary, shared lib, and icons in
# place it can show a gray "stopped" icon and a working "View Logs" menu
# while the rest of install (Docker pulls, container startup, WordPress
# configuration) is still running. Headless boxes silently skip this; the
# late dashboard-open block below also no longer relaunches the tray.
if { [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; }; then
    pkill -u "$REAL_USER" -f onionpress-tray 2>/dev/null || true
    sleep 0.3
    run_as_user "$INSTALL_DIR/onionpress-tray" >/dev/null 2>&1 &
    echo "  Tray icon launched — use it to view logs during install."
fi

# system-sleep hook so the suspend/resume cycle notifies the hub +
# DEL_ONION/ADD_ONION. Without this, a laptop close-lid leaves a stale
# descriptor on the DHT for minutes until missed heartbeats finally
# trigger the hub takeover. See linux/onionpress-sleep-hook for details.
# -D creates /usr/lib/systemd/system-sleep/, which does not exist on a
# fresh Debian 13 (issue #264).
$SUDO install -D -m 0755 "$REPO_DIR/linux/onionpress-sleep-hook" \
    /usr/lib/systemd/system-sleep/onionpress

# LAN bind: the compose file publishes WordPress on
# ${ONIONPRESS_BIND_HOST:-127.0.0.1}, and the Linux launcher exports 0.0.0.0
# unless ~/.onionpress/config sets ONIONPRESS_BIND_HOST, so a headless Pi
# can be reached from another device on the LAN. Tor's SOCKS port is fixed
# to 127.0.0.1. No sed rewrite of the compose file, here or anywhere.

VERSION=$(cat "$INSTALL_DIR/VERSION")
echo "  OnionPress $VERSION installed to $INSTALL_DIR"

# Remove the clone, if this run made one (the trap at the top removes it
# when a run is interrupted before this point).
if [ -n "$CLONE_DIR" ]; then
    rm -rf -- "$CLONE_DIR"
    CLONE_DIR=""
fi

# ─── Create data directory & secrets ─────────────────────────────────

echo ""
echo "  Setting up data directory..."

# ~/.onionpress, secrets and config are the user's alone (issues #397,
# #471): made as the user, never by root, so a link planted at one of
# those names leads root nowhere; the folder is 0700 and both files are
# 0600 from their first byte (umask 077; `set -C` makes `>` create only,
# so a name that holds a link, dangling or not, fails instead of being
# followed). A name holding a link, or anything but a folder / regular
# file, is left alone with a message: the launcher makes what is missing
# at the first start. An existing secrets or config is never rewritten
# (the running database's passwords are in it).
plain_path() {   # 0: $1 is absent, or a $2 (-d / -f) that is no link
    [ ! -L "$1" ] && { [ ! -e "$1" ] || [ "$2" "$1" ]; }
}
data_dir_setup() {
    umask 077
    local d
    for d in "$DATA_DIR" "$DATA_DIR/shared" "$DATA_DIR/shared/vanity-keys"; do
        if ! plain_path "$d" -d; then
            echo "  $d is a link or not a folder; left alone" >&2
            return 1
        fi
    done
    install -d -m 700 "$DATA_DIR"
    install -d "$DATA_DIR/shared" "$DATA_DIR/shared/vanity-keys"
}
first_install_files() {
    umask 077
    set -C
    local f
    if [ -L "$DATA_DIR" ] || [ ! -d "$DATA_DIR" ]; then
        echo "  $DATA_DIR is a link or not a folder; secrets and config not written" >&2
        return 1
    fi
    for f in "$DATA_DIR/secrets" "$DATA_DIR/config"; do
        if ! plain_path "$f" -f; then
            echo "  $f is a link or not a regular file; left alone" >&2
            return 1
        fi
    done
    if [ ! -f "$DATA_DIR/secrets" ]; then
        WP_PASS=$(LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 32)
        ROOT_PASS=$(LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 32)
        cat > "$DATA_DIR/secrets" <<EOF
# Database passwords - generated on $(date)
# DO NOT SHARE THESE PASSWORDS
WORDPRESS_DB_PASSWORD='$WP_PASS'
MYSQL_PASSWORD='$WP_PASS'
MYSQL_ROOT_PASSWORD='$ROOT_PASS'
EOF
        chmod 600 "$DATA_DIR/secrets"
        echo "  Database passwords generated"
    else
        echo "  Existing secrets preserved"
    fi
    if [ ! -f "$DATA_DIR/config" ]; then
        cat > "$DATA_DIR/config" <<EOF
ADDRESS_PREFIX=op2
INSTALL_IA_PLUGIN=yes
START_ON_BOOT=yes
# OnionHeaven is on by default: yes (or unset) = on, no = off.
# Turn off with: onionpress onionheaven disable
REGISTER_WITH_ONIONHEAVEN=yes
ONIONHEAVEN_ADDRESS=oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion
# WordPress's local web port (normally 8080) is open to other devices on
# your network, so a headless machine can be set up from a phone or laptop.
# Uncomment to keep it on this machine only. Tor's SOCKS port is always
# this machine only.
#ONIONPRESS_BIND_HOST=127.0.0.1
EOF
        # Already 0600 from the umask; said again where the file is made.
        chmod 600 "$DATA_DIR/config"
        echo "  Default config created"
    fi
}
# The shell that runs one of the functions above as the user: `set -e`,
# the functions, DATA_DIR, the call. The caller runs it under that user.
data_dir_script() {
    printf 'set -e; %s; DATA_DIR=%q; %s' \
        "$(declare -f plain_path data_dir_setup first_install_files)" "$DATA_DIR" "$1"
}
# Through 2.5.6 this ran as root with SUDO_USER set: the folder, secrets
# and config were made by root and chown -R'd after (issue #471).
if ! run_as_user bash -c "$(data_dir_script data_dir_setup)"; then
    echo "  Could not set up $DATA_DIR for $REAL_USER; onionpress start makes it." >&2
elif ! run_as_user bash -c "$(data_dir_script first_install_files)"; then
    echo "  Could not write secrets and config in $DATA_DIR; onionpress start makes them." >&2
fi

# ─── Install systemd user services ──────────────────────────────────

echo ""
echo "  Installing systemd user services..."

# Create user systemd directory
if [ -n "$SUDO_USER" ]; then
    install -d -o "$SUDO_USER" -g "$SUDO_USER" "$USER_SYSTEMD_DIR"
else
    mkdir -p "$USER_SYSTEMD_DIR"
fi

# Copy service files to user systemd directory (from install dir, not repo — repo may be cleaned up)
cp "$INSTALL_DIR/onionpress.service" "$USER_SYSTEMD_DIR/onionpress.service"
cp "$INSTALL_DIR/onionpress-heartbeat.service" "$USER_SYSTEMD_DIR/onionpress-heartbeat.service"
cp "$INSTALL_DIR/onionpress-watcher.service" "$USER_SYSTEMD_DIR/onionpress-watcher.service"
cp "$INSTALL_DIR/onionpress-watcher.timer" "$USER_SYSTEMD_DIR/onionpress-watcher.timer"

# Ensure owned by user
if [ -n "$SUDO_USER" ]; then
    chown "$SUDO_USER:$SUDO_USER" "$USER_SYSTEMD_DIR"/onionpress*
fi

# Enable lingering (ensures user services start at boot, even without login)
$SUDO loginctl enable-linger "$REAL_USER"

# Reload and enable user services
run_as_user env XDG_RUNTIME_DIR="/run/user/$REAL_UID" \
    systemctl --user daemon-reload
run_as_user env XDG_RUNTIME_DIR="/run/user/$REAL_UID" \
    systemctl --user enable onionpress
run_as_user env XDG_RUNTIME_DIR="/run/user/$REAL_UID" \
    systemctl --user enable --now onionpress-watcher.timer
run_as_user env XDG_RUNTIME_DIR="/run/user/$REAL_UID" \
    systemctl --user enable onionpress-heartbeat
echo "  Systemd user services installed and enabled (starts on boot)"

# ─── Start OnionPress ─────────────────────────────────────────────────

echo ""
echo "  Starting OnionPress (this may take a few minutes on first run)..."
echo "  Docker will pull container images for WordPress, MariaDB, and Tor."
echo ""

local_ip=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")

# Start the journal tail BEFORE systemctl restart so it captures the early
# "Pulling" lines. journalctl --since=now misses anything emitted before it
# launches, and the launcher's tee'd pull output starts immediately.
echo "  ───── live progress (image pulls take 2-3 min on first run) ─────"
run_as_user env XDG_RUNTIME_DIR="/run/user/$REAL_UID" \
    journalctl --user -u onionpress -f --no-hostname --no-pager --since=now &
JOURNAL_PID=$!   # cleanup (the EXIT trap at the top) kills it

# Use restart (not start) so a stale service from a previous install is replaced.
# --no-block: don't wait for Type=notify READY=1. Without --no-block, this
# call would synchronously block for the full 2-3 min pull/bootstrap window
# — starving the journal tail above of any audience until the work is done.
run_as_user env XDG_RUNTIME_DIR="/run/user/$REAL_UID" \
    systemctl --user --no-block restart onionpress

# Heartbeat client waits for containers anyway; --no-block keeps it from
# stalling install.sh in lockstep with onionpress.service.
run_as_user env XDG_RUNTIME_DIR="/run/user/$REAL_UID" \
    systemctl --user --no-block restart onionpress-heartbeat

# Wait for WordPress container to respond. This is the real "are we up"
# signal — `systemctl is-active` races against Type=notify when the
# restart is non-blocking (returns "activating" until READY=1).
# First run on a Pi can take 2-3 minutes (image pulls + DB bootstrap).
wp_ready=false
wp_wait=0
while [ $wp_wait -lt 180 ]; do
    if curl -s --max-time 3 "http://localhost:8080" >/dev/null 2>&1; then
        wp_ready=true
        break
    fi
    sleep 3
    wp_wait=$((wp_wait + 3))
done

# Stop tailing the journal before we print the success message.
kill $JOURNAL_PID 2>/dev/null || true
JOURNAL_PID=""
echo "  ─────────────────────────────────────────────────────────────────"

if [ "$wp_ready" = "true" ]; then
    # Try to get the onion address
    onion_addr=$("$INSTALL_DIR/onionpress" address 2>/dev/null) || true

    echo ""
    echo "  ══════════════════════════════════════════════════════════"
    echo "  OnionPress is running."
    echo "  ══════════════════════════════════════════════════════════"
    echo ""
    echo "  NEXT — set up your site:"
    echo ""
    echo "    • On a desktop: open the OnionPress tray icon and click"
    echo "      Setup… to pick your site title, username, and password."
    echo "    • From SSH or a terminal: run  onionpress setup"
    echo ""
    echo "    The wizard installs WordPress with your chosen credentials,"
    echo "    so there's no auto-generated password to copy down."
    echo ""
    echo "  Your public .onion address (visit from Tor Browser):"
    if [ "$onion_addr" != "Generating..." ] && [ -n "$onion_addr" ]; then
        echo "       http://${onion_addr}"
        echo "       (allow 5-10 min for the descriptor to propagate)"
    else
        echo "       still publishing — run 'onionpress address' to check"
    fi
    echo ""
    echo "  ──────────────────────────────────────────────────────────"
    echo "  OnionHeaven keeps your site online when this machine is off"
    echo ""
    echo "  OnionHeaven is on: a copy of your onion private key goes to the"
    echo "  hub, run by the Internet Archive, so it can serve your address"
    echo "  while this machine is asleep or powered down. To opt out (one"
    echo "  command, no restart needed; the hub deletes its copy of the key):"
    echo ""
    echo "      onionpress onionheaven disable"
    echo ""
    echo "  What it does and what is shared:  onionpress onionheaven status"
    echo ""
    echo "  ──────────────────────────────────────────────────────────"
    echo "  Useful commands:"
    echo "    onionpress status     - is it running?"
    echo "    onionpress address    - show .onion address"
    echo "    onionpress onionheaven enable|disable|status"
    echo "    onionpress logs       - stream live container logs"
    echo "    systemctl --user restart onionpress   - restart"
    echo "    systemctl --user stop onionpress      - stop"
    echo ""
    echo "  Log file: $DATA_DIR/onionpress.log"
    echo ""
else
    echo ""
    echo "  WordPress did not respond within 3 minutes."
    echo "  OnionPress may still be starting (slow connection or first"
    echo "  install on low-end hardware). Check progress with:"
    echo "    journalctl --user -u onionpress -f"
    echo "    onionpress status"
    echo ""
fi

# CLI symlinks are created earlier (right after copying the binaries),
# before the tray is started, so SetupDialog's provision-post-install
# subprocess hop resolves even when a user clicks Setup fast.

# ─── Desktop entry (per-user) ────────────────────────────────────────
# Puts OnionPress in the user's app drawer so they can launch the
# dashboard from Activities like any other app (Signal, Firefox, etc.)
# rather than memorizing a localhost URL. Per-user — no sudo, no /usr.

DESKTOP_APPS_DIR="$REAL_HOME/.local/share/applications"
DESKTOP_HICOLOR_DIR="$REAL_HOME/.local/share/icons/hicolor"
# Ship the same app-icon.png at every dock-relevant size so GNOME's
# StatusNotifier / dash-to-dock pick up the rainbow gradient instead of
# falling back to the generic gear icon (the fallback fires whenever the
# theme search misses the size the shell asks for). gtk-update-icon-cache
# below picks these up. We install identical copies and let GTK scale —
# avoids an ImageMagick dep on minimal Pi installs.
DESKTOP_ICON_SIZES="48 64 128 256 512"

if [ -n "$SUDO_USER" ]; then
    install -d -o "$SUDO_USER" -g "$SUDO_USER" "$DESKTOP_APPS_DIR"
    for _size in $DESKTOP_ICON_SIZES; do
        install -d -o "$SUDO_USER" -g "$SUDO_USER" \
            "$DESKTOP_HICOLOR_DIR/${_size}x${_size}/apps"
    done
else
    mkdir -p "$DESKTOP_APPS_DIR"
    for _size in $DESKTOP_ICON_SIZES; do
        mkdir -p "$DESKTOP_HICOLOR_DIR/${_size}x${_size}/apps"
    done
fi

cat > "$DESKTOP_APPS_DIR/onionpress.desktop" <<DESKTOP_EOF
[Desktop Entry]
Type=Application
Name=OnionPress
GenericName=Decentralized Blog
Comment=Open your OnionPress dashboard
Exec=onionpress dashboard
Icon=onionpress
Categories=Network;Publishing;
Keywords=blog;wordpress;tor;onion;
StartupNotify=false
DESKTOP_EOF

if [ -f "$INSTALL_DIR/app-icon.png" ]; then
    # Resize to each target dimension. Plain `cp` of the 1024x1024
    # source into a "48x48" directory leaves the file at native size,
    # and GNOME's panel renders it at native dimensions inside the
    # ~22px cell — zoomed and clipped. Prefer ImageMagick `convert`
    # (pre-installed on most desktops); fall back to Python+PIL; fall
    # back further to GdkPixbuf via python3-gi (the tray's runtime dep
    # is already python3-gi, so this is guaranteed available); last
    # resort, copy unscaled and accept the rendering bug rather than
    # leaving the icon directories empty.
    _resize_icon() {
        local src="$1" dst="$2" size="$3"
        if command -v convert >/dev/null 2>&1; then
            convert "$src" -resize "${size}x${size}" "$dst" 2>/dev/null && return 0
        fi
        if python3 -c 'from PIL import Image' 2>/dev/null; then
            python3 -c "
from PIL import Image
img = Image.open('$src')
img.thumbnail(($size, $size), Image.LANCZOS)
img.save('$dst', 'PNG')
" 2>/dev/null && return 0
        fi
        if python3 -c 'import gi; gi.require_version(\"GdkPixbuf\", \"2.0\"); from gi.repository import GdkPixbuf' 2>/dev/null; then
            python3 -c "
import gi
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import GdkPixbuf
pix = GdkPixbuf.Pixbuf.new_from_file_at_size('$src', $size, $size)
pix.savev('$dst', 'png', [], [])
" 2>/dev/null && return 0
        fi
        cp "$src" "$dst"
    }
    for _size in $DESKTOP_ICON_SIZES; do
        _resize_icon "$INSTALL_DIR/app-icon.png" \
            "$DESKTOP_HICOLOR_DIR/${_size}x${_size}/apps/onionpress.png" \
            "$_size"
    done
fi

if [ -n "$SUDO_USER" ]; then
    chown "$SUDO_USER:$SUDO_USER" "$DESKTOP_APPS_DIR/onionpress.desktop" 2>/dev/null || true
    for _size in $DESKTOP_ICON_SIZES; do
        chown "$SUDO_USER:$SUDO_USER" \
            "$DESKTOP_HICOLOR_DIR/${_size}x${_size}/apps/onionpress.png" 2>/dev/null || true
    done
fi

# ─── Tray autostart (per-user) ───────────────────────────────────────
# Installs the tray so it starts on every login, and launches it now
# if a display is available. Headless boxes skip this silently.
AUTOSTART_DIR="$REAL_HOME/.config/autostart"
if [ -n "$SUDO_USER" ]; then
    install -d -o "$SUDO_USER" -g "$SUDO_USER" "$AUTOSTART_DIR"
else
    mkdir -p "$AUTOSTART_DIR"
fi

cat > "$AUTOSTART_DIR/onionpress-tray.desktop" <<TRAY_EOF
[Desktop Entry]
Type=Application
Name=OnionPress
GenericName=OnionPress tray
Comment=Status icon for your OnionPress site
Exec=$INSTALL_DIR/onionpress-tray
Icon=onionpress
Categories=Network;
StartupNotify=false
NoDisplay=true
StartupWMClass=onionpress-tray
X-GNOME-Autostart-Phase=Applications
OnlyShowIn=GNOME;KDE;XFCE;MATE;Cinnamon;Unity;LXQt;LXDE;Budgie;Pantheon;
TRAY_EOF

if [ -n "$SUDO_USER" ]; then
    chown "$SUDO_USER:$SUDO_USER" "$AUTOSTART_DIR/onionpress-tray.desktop" 2>/dev/null || true
fi

# Tray status icons (tray-running/starting/stopped) — installed into the
# standard user hicolor theme so GNOME Shell finds them by name at the
# exact requested size (typically 24px) without a custom IconThemePath.
# A custom IconThemePath causes GNOME to render icons at native pixel size
# rather than scaling to the panel slot, which clips a 22px icon in a 24px
# slot. Providing exact sizes avoids any scaling artefact.
TRAY_ICON_SIZES="16 22 24 32"
for _size in $TRAY_ICON_SIZES; do
    if [ -n "$SUDO_USER" ]; then
        install -d -o "$SUDO_USER" -g "$SUDO_USER" \
            "$DESKTOP_HICOLOR_DIR/${_size}x${_size}/apps"
    else
        mkdir -p "$DESKTOP_HICOLOR_DIR/${_size}x${_size}/apps"
    fi
    for _name in tray-running tray-starting tray-stopped; do
        _src="$INSTALL_DIR/assets/hicolor/${_size}x${_size}/apps/${_name}.png"
        _dst="$DESKTOP_HICOLOR_DIR/${_size}x${_size}/apps/${_name}.png"
        if [ -f "$_src" ]; then
            if [ -n "$SUDO_USER" ]; then
                install -o "$SUDO_USER" -g "$SUDO_USER" "$_src" "$_dst"
            else
                cp "$_src" "$_dst"
            fi
        fi
    done
done

# Refresh GNOME's caches so the icon shows up without requiring a logout.
# Best-effort — these tools aren't installed on every distro.
if command -v update-desktop-database >/dev/null 2>&1; then
    run_as_user update-desktop-database "$DESKTOP_APPS_DIR" >/dev/null 2>&1 || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    run_as_user gtk-update-icon-cache -t -f "$REAL_HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true
fi

# On a GUI desktop: open the browser to wp-admin. The tray itself was
# launched right after the file copies so the user could open View Logs
# during the long Docker/WP setup; we don't relaunch it here.
if { [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; }; then
    if [ "$wp_ready" = "true" ] && command -v xdg-open >/dev/null 2>&1; then
        run_as_user "$INSTALL_DIR/onionpress" dashboard >/dev/null 2>&1 &
    fi
fi

}

main "$@"

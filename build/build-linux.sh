#!/bin/bash

# Build OnionPress .deb for Linux
# Usage: bash build/build-linux.sh
#
# Output:
#   build/onionpress.deb  (version is inside the package via the Version:
#                          control field — what the homepage links to via
#                          releases/latest/download/)

set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="$PROJECT_DIR/build"
STAGE_DIR=$(mktemp -d)

# ─── Detect version ────────────────────────────────────────────────────

VERSION=$(grep 'self\.version *= *"' "$PROJECT_DIR/src/menubar.py" | head -1 | sed 's/.*"\(.*\)".*/\1/')
if [ -z "$VERSION" ]; then
    echo "ERROR: Could not detect version from src/menubar.py"
    exit 1
fi
echo "Building OnionPress v$VERSION for Linux"

# Architecture: "all" because the package contains only scripts,
# Docker Compose files, and Python — no compiled binaries.
# Docker pulls the correct container images for the platform at runtime.
DEB_ARCH="all"
echo "Architecture: all (scripts only, works on any platform)"

# ─── Collect source files ──────────────────────────────────────────────
# These are the files that get installed to /opt/onionpress: the one list
# in linux/install-tree.sh, which install.sh and `onionpress update
# --from-checkout` install from too (a test keeps all three on it).

collect_files() {
    bash "$PROJECT_DIR/linux/install-tree.sh" stage "$PROJECT_DIR" "$1"
}

# ─── Build .deb ─────────────────────────────────────────────────────────

echo ""
echo "Building .deb package..."

# Staging dir name doesn't influence the output filename — `dpkg-deb --build`
# writes wherever we point it, and the package's own version lives in the
# control file's `Version:` field, not the filename.
DEB_NAME="onionpress_${VERSION}_${DEB_ARCH}"
DEB_ROOT="$STAGE_DIR/deb/$DEB_NAME"
DEB_OUT="$BUILD_DIR/onionpress.deb"

# Install files
collect_files "$DEB_ROOT/opt/onionpress"

# Systemd user services. The unit files use user-unit idioms
# (WantedBy=default.target, XDG_RUNTIME_DIR=/run/user/%U) — they belong
# under /usr/lib/systemd/user/, not /lib/systemd/system/. The previous
# layout enabled them as system units, which is a no-op for
# default.target and meant services never actually started at boot.
mkdir -p "$DEB_ROOT/usr/lib/systemd/user"
cp "$PROJECT_DIR/linux/onionpress.service" "$DEB_ROOT/usr/lib/systemd/user/"
cp "$PROJECT_DIR/linux/onionpress-heartbeat.service" "$DEB_ROOT/usr/lib/systemd/user/"
cp "$PROJECT_DIR/linux/onionpress-watcher.service" "$DEB_ROOT/usr/lib/systemd/user/"
cp "$PROJECT_DIR/linux/onionpress-watcher.timer" "$DEB_ROOT/usr/lib/systemd/user/"

# system-sleep hook — system-scope (not user-scope), runs as root, fires
# around every suspend/hibernate. Notifies the hub + DEL_ONION before
# suspend, re-publishes + sends immediate /online after resume.
mkdir -p "$DEB_ROOT/usr/lib/systemd/system-sleep"
install -m 0755 "$PROJECT_DIR/linux/onionpress-sleep-hook" \
    "$DEB_ROOT/usr/lib/systemd/system-sleep/onionpress"

# System Docker's drop-in: the WordPress network's egress rules before
# dockerd restarts any container at boot (see the file). A vendor drop-in,
# so it goes with the package; nothing else in docker.service changes.
mkdir -p "$DEB_ROOT/usr/lib/systemd/system/docker.service.d"
install -m 0644 "$PROJECT_DIR/linux/onionpress-wordpress-egress.conf" \
    "$DEB_ROOT/usr/lib/systemd/system/docker.service.d/onionpress-wordpress-egress.conf"

# CLI symlinks
mkdir -p "$DEB_ROOT/usr/local/bin"
ln -s /opt/onionpress/onionpress "$DEB_ROOT/usr/local/bin/onionpress"
ln -s /opt/onionpress/onionpress-tray "$DEB_ROOT/usr/local/bin/onionpress-tray"

# Autostart entry — spawns the tray icon at login for users in a graphical
# session. OnlyShowIn= covers the Ubuntu flavours that have a tray; on a
# headless system the entry sits dormant.
mkdir -p "$DEB_ROOT/etc/xdg/autostart"
cp "$PROJECT_DIR/linux/onionpress-tray.desktop" \
    "$DEB_ROOT/etc/xdg/autostart/onionpress-tray.desktop"

# AppStream metadata — drives GNOME Software presence (name, summary,
# screenshots, release notes, "Open Application" button).
mkdir -p "$DEB_ROOT/usr/share/metainfo"
cp "$PROJECT_DIR/linux/onionpress.metainfo.xml" \
    "$DEB_ROOT/usr/share/metainfo/org.onionpress.OnionPress.metainfo.xml"

# Desktop entry — gives GUI users (Ubuntu Desktop, KDE) an icon in their
# app menu that opens http://localhost:8080. xdg-open routes to the
# default browser, which is the right thing on every modern Linux desktop.
mkdir -p "$DEB_ROOT/usr/share/applications"
cat > "$DEB_ROOT/usr/share/applications/onionpress.desktop" <<EOF
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
EOF

# Icon for the desktop entry.
if [ -f "$PROJECT_DIR/app/Resources/app-icon.png" ]; then
    mkdir -p "$DEB_ROOT/usr/share/icons/hicolor/512x512/apps"
    cp "$PROJECT_DIR/app/Resources/app-icon.png" "$DEB_ROOT/usr/share/icons/hicolor/512x512/apps/onionpress.png"
fi

# DEBIAN control
# python3-nacl (PyNaCl) encrypts backups (backup format 2,
# src/onionpress/backup_crypto.py) and signs OnionHeaven requests
# (onion_auth.sign_expanded, on libsodium's ed25519 core functions, which
# Debian's libsodium23 has). 1.5.0 is what Debian 12 and 13, Ubuntu
# 22.04 and 24.04 and Raspberry Pi OS Bookworm ship, and what CI tests
# against besides the latest; it has every binding OnionPress uses.
# Depends is one line: a binary package's control file may not fold it
# (lintian: multiline-field). No Description line may start with a dot
# after the space: " .onion address" read as a control statement, and the
# only valid one is " ." alone (lintian: description-contains-invalid-
# control-statement). build/check-deb.sh runs lintian on what this makes.
mkdir -p "$DEB_ROOT/DEBIAN"
cat > "$DEB_ROOT/DEBIAN/control" <<EOF
Package: onionpress
Version: $VERSION
Section: web
Priority: optional
Architecture: $DEB_ARCH
Depends: docker.io | docker-ce, docker-compose-plugin | docker-compose, uidmap, dbus-user-session, fuse-overlayfs, slirp4netns, jq, python3, python3-nacl (>= 1.5.0), zip, unzip, python3-gi, gir1.2-gtk-3.0, gir1.2-ayatanaappindicator3-0.1, gir1.2-notify-0.7, xdg-utils
Recommends: torbrowser-launcher
Maintainer: Brewster Kahle <brewster@archive.org>
Homepage: https://onionpress.org
Description: Your Decentralized Social Blog Site
 OnionPress turns your computer into a web server running WordPress,
 accessible via the Tor network. Your site gets its own permanent onion
 address, backed up by the Internet Archive's Wayback Machine.
 .
 Built on WordPress + Tor + Wayback Machine.
EOF

# The autostart entry lives in /etc, so dpkg must treat it as a
# configuration file: a user's edit survives an upgrade instead of being
# overwritten (Debian Policy 10.7; lintian: file-in-etc-not-marked-as-conffile).
cat > "$DEB_ROOT/DEBIAN/conffiles" <<EOF
/etc/xdg/autostart/onionpress-tray.desktop
EOF

# Post-install: data dir + secrets (first install only), Docker
# bootstrap (rootless preferred, docker-group fallback), linger,
# enable + start user units. Goal is "self-contained for casual
# users": after `sudo apt install ./onionpress.deb` on a clean Ubuntu,
# the site comes up and the user just needs a URL.
#
# Gating ($1=configure, $2 empty = fresh install; $2 set = upgrade from
# that version). First-install logic must NOT re-run on upgrade or it
# clobbers user-edited config and re-generates secrets that don't match
# the running DB.
cat > "$DEB_ROOT/DEBIAN/postinst" <<'POSTINST'
#!/bin/bash
set -e

# Detect the human user who invoked the install. We try every known
# invocation channel because there are several:
#   - sudo apt install …               → SUDO_USER (headless SSH, terminal)
#   - GNOME Software (PackageKit)      → PACKAGEKIT_CALLER_UID
#   - KDE Discover / pkexec            → PKEXEC_UID
#   - dpkg -i from a TTY               → logname
#   - last resort                      → first non-root user on the box
detect_real_user() {
    local u
    if [ -n "$SUDO_USER" ] && [ "$SUDO_USER" != "root" ]; then
        echo "$SUDO_USER"; return
    fi
    if [ -n "$PACKAGEKIT_CALLER_UID" ]; then
        u=$(getent passwd "$PACKAGEKIT_CALLER_UID" 2>/dev/null | cut -d: -f1)
        [ -n "$u" ] && [ "$u" != "root" ] && { echo "$u"; return; }
    fi
    if [ -n "$PKEXEC_UID" ]; then
        u=$(getent passwd "$PKEXEC_UID" 2>/dev/null | cut -d: -f1)
        [ -n "$u" ] && [ "$u" != "root" ] && { echo "$u"; return; }
    fi
    u=$(logname 2>/dev/null || true)
    [ -n "$u" ] && [ "$u" != "root" ] && { echo "$u"; return; }
    # Last resort: pick the first regular user (UID >= 1000, < 65534).
    u=$(getent passwd | awk -F: '$3 >= 1000 && $3 < 65534 { print $1; exit }')
    [ -n "$u" ] && { echo "$u"; return; }
    echo "root"
}
REAL_USER=$(detect_real_user)
REAL_HOME=$(getent passwd "$REAL_USER" 2>/dev/null | cut -d: -f6)
[ -z "$REAL_HOME" ] && REAL_HOME="/root"
DATA_DIR="$REAL_HOME/.onionpress"

# Idempotent: data dir creation runs on every postinst trigger.
if [ "$REAL_USER" != "root" ]; then
    install -d -o "$REAL_USER" -g "$REAL_USER" "$DATA_DIR"
    install -d -o "$REAL_USER" -g "$REAL_USER" "$DATA_DIR/shared"
    install -d -o "$REAL_USER" -g "$REAL_USER" "$DATA_DIR/shared/vanity-keys"
else
    mkdir -p "$DATA_DIR/shared/vanity-keys"
fi

# First-install-only: random secrets + default config.
if [ "$1" = "configure" ] && [ -z "$2" ]; then
    if [ ! -f "$DATA_DIR/secrets" ]; then
        WP_PASS=$(LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 32)
        ROOT_PASS=$(LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 32)
        cat > "$DATA_DIR/secrets" <<EOF
# Database passwords - generated on $(date)
WORDPRESS_DB_PASSWORD='$WP_PASS'
MYSQL_PASSWORD='$WP_PASS'
MYSQL_ROOT_PASSWORD='$ROOT_PASS'
EOF
        chmod 600 "$DATA_DIR/secrets"
    fi

    if [ ! -f "$DATA_DIR/config" ]; then
        cat > "$DATA_DIR/config" <<EOF
ADDRESS_PREFIX=op2
INSTALL_IA_PLUGIN=yes
UPDATE_ON_LAUNCH=no
START_ON_BOOT=yes
# OnionHeaven is opt-in: unset = not decided (you will be asked), yes = on, no = off.
# Enable with: onionpress onionheaven enable
REGISTER_WITH_ONIONHEAVEN=
ONIONHEAVEN_ADDRESS=oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion
# WordPress's local web port (normally 8080) is open to other devices on
# your network, so a headless machine can be set up from a phone or laptop.
# Uncomment to keep it on this machine only. Tor's SOCKS port is always
# this machine only.
#ONIONPRESS_BIND_HOST=127.0.0.1
EOF
    fi
fi

# Ownership fixup is idempotent.
if [ "$REAL_USER" != "root" ]; then
    chown -R "$REAL_USER:$REAL_USER" "$DATA_DIR"
fi

# Reload the user-unit catalog and enable units for any user on next
# login. --global respects per-user disable preferences.
systemctl --global enable onionpress.service onionpress-heartbeat.service onionpress-watcher.timer 2>/dev/null || true

# The system manager reads docker.service's new drop-in (the WordPress
# network's egress rules at boot) from its next start; nothing restarts now.
systemctl daemon-reload 2>/dev/null || true

# Self-contained bootstrap. Only runs on first install (not upgrade) and
# only when we have a real target user. Two-tier strategy:
#   1. Preferred: rootless Docker (user-level daemon at
#      /run/user/$UID/docker.sock). No docker group, no logout — same
#      session can immediately use Docker. Requires
#      dockerd-rootless-setuptool.sh (ships with docker.io on Debian 12 /
#      Ubuntu 22.04+) and uidmap/dbus-user-session/fuse-overlayfs/
#      slirp4netns (declared in Depends:). Refused if system Docker is
#      already serving other containers.
#   2. Fallback: docker group + daemon-reexec, the legacy flow. Still
#      tries to avoid logout via daemon-reexec; if that fails the user
#      gets the "Log out and back in" message.
if [ "$1" = "configure" ] && [ -z "$2" ] && [ "$REAL_USER" != "root" ]; then
    bootstrap_ok=1
    rootless_ok=0
    REAL_UID=$(id -u "$REAL_USER")
    _rootless_sock="/run/user/$REAL_UID/docker.sock"

    # 1. Lingering so the user systemd manager runs at boot without a
    #    login. Needed for both rootless and group paths — rootless
    #    docker.service is a user unit, and our own onionpress.service
    #    is also user-scoped.
    if ! loginctl show-user "$REAL_USER" 2>/dev/null | grep -q 'Linger=yes'; then
        loginctl enable-linger "$REAL_USER" 2>/dev/null || bootstrap_ok=0
    fi

    # 2. Already-rootless detection (e.g. earlier install.sh run, or
    #    .deb reinstall after dpkg -r). Probing `docker info` with
    #    DOCKER_HOST pointing at the socket confirms the daemon is
    #    actually responsive, not just that the file exists.
    if [ -S "$_rootless_sock" ] && \
       runuser -u "$REAL_USER" -- env DOCKER_HOST="unix://$_rootless_sock" \
           docker info >/dev/null 2>&1; then
        rootless_ok=1
    fi

    # 3. Try fresh rootless setup. Skip if the setup tool isn't on PATH
    #    (older Docker releases) or if system Docker is actively serving
    #    other containers (we'd be ripping the rug out from under another
    #    user's workloads). `docker ps -q` is empty on a fresh install
    #    where apt just brought docker.service up.
    if [ "$rootless_ok" = "0" ] && \
       [ "$bootstrap_ok" = "1" ] && \
       command -v dockerd-rootless-setuptool.sh >/dev/null 2>&1; then

        system_busy=0
        if docker ps -q 2>/dev/null | grep -q .; then
            system_busy=1
        fi

        if [ "$system_busy" = "0" ]; then
            # The rootless setup tool refuses to proceed while
            # /var/run/docker.sock exists, so stop + remove it.
            systemctl disable --now docker.service docker.socket 2>/dev/null || true
            rm -f /var/run/docker.sock

            if runuser -u "$REAL_USER" -- env XDG_RUNTIME_DIR="/run/user/$REAL_UID" \
                   dockerd-rootless-setuptool.sh install --force >/dev/null 2>&1 && \
               runuser -u "$REAL_USER" -- env XDG_RUNTIME_DIR="/run/user/$REAL_UID" \
                   systemctl --user enable --now docker.service >/dev/null 2>&1; then
                rootless_ok=1
            fi
        fi
    fi

    # 4. Fallback: docker group flow if rootless didn't take. apt's
    #    docker.io postinst already created the group; we add the user.
    if [ "$rootless_ok" = "0" ]; then
        if ! id -nG "$REAL_USER" 2>/dev/null | grep -qw docker; then
            if usermod -aG docker "$REAL_USER" 2>/dev/null; then
                echo "  Added $REAL_USER to the docker group."
            else
                bootstrap_ok=0
            fi
        fi

        # Reload the user's systemd manager so it picks up the new
        # docker-group membership. Without this, services launched from
        # the existing user-manager instance inherit the old group set
        # and can't reach /var/run/docker.sock.
        # `systemctl --user --machine=USER@.host` requires systemd 248+
        # (Ubuntu 22.04, Debian 12, Raspberry Pi OS Bookworm — all fine).
        if [ "$bootstrap_ok" = "1" ]; then
            systemctl --user --machine="${REAL_USER}@.host" daemon-reexec 2>/dev/null || bootstrap_ok=0
        fi
    fi

    # 5. Enable + start the OnionPress services.
    if [ "$bootstrap_ok" = "1" ]; then
        systemctl --user --machine="${REAL_USER}@.host" daemon-reload 2>/dev/null || true
        systemctl --user --machine="${REAL_USER}@.host" enable --now \
            onionpress.service \
            onionpress-heartbeat.service \
            onionpress-watcher.timer \
            2>/dev/null || bootstrap_ok=0
    fi

    VERSION_INSTALLED=$(cat /opt/onionpress/VERSION 2>/dev/null || echo "?")
    if [ "$bootstrap_ok" = "1" ]; then
        if [ "$rootless_ok" = "1" ]; then
            DOCKER_MODE_LINE="Docker mode: rootless (no logout required)."
        else
            DOCKER_MODE_LINE="Docker mode: system daemon (docker group)."
        fi
        cat <<EOF

OnionPress v$VERSION_INSTALLED installed and starting in the background.
$DOCKER_MODE_LINE
First start pulls ~500 MB of container images (Tor, WordPress, MariaDB)
and may take 1-3 minutes on a fast connection, longer on a Pi.

When ready:
    Local:   http://localhost:8080
    .onion:  onionpress address
    Status:  onionpress status
    Logs:    onionpress logs

EOF
    else
        # Something in the bootstrap path failed — print clear manual
        # instructions so the user can still get to a working install.
        cat <<EOF

OnionPress v$VERSION_INSTALLED installed. One manual step needed to finish:

    sudo usermod -aG docker $REAL_USER
    loginctl enable-linger $REAL_USER
    # Log out and back in (so docker-group membership takes effect)
    systemctl --user enable --now onionpress

Then:
    Local:   http://localhost:8080
    .onion:  onionpress address
    Status:  onionpress status

EOF
    fi

    # Spawn the tray for the user's active graphical session so the icon
    # appears without making them log out and back in. Best-effort — if
    # there's no graphical session, this is a no-op. The autostart entry
    # in /etc/xdg/autostart/ handles all subsequent logins.
    if loginctl show-user "$REAL_USER" 2>/dev/null | grep -q 'Display='; then
        if ! pgrep -u "$REAL_USER" -f '/onionpress-tray$' >/dev/null 2>&1; then
            (runuser -u "$REAL_USER" -- bash -lc \
                'setsid /usr/local/bin/onionpress-tray >/dev/null 2>&1 < /dev/null &' \
              || su - "$REAL_USER" -c \
                'setsid /usr/local/bin/onionpress-tray >/dev/null 2>&1 < /dev/null &') 2>/dev/null || true
        fi
    fi

    # Warn about a stale install.sh-style install if we detect one in the
    # user's home. Only one curl|bash installer is known in the field
    # (op2pie) — leave the cleanup to that user rather than auto-migrate.
    if [ -f "$REAL_HOME/.config/systemd/user/onionpress.service" ]; then
        cat <<EOF
NOTE: Detected an earlier install.sh-installed copy of onionpress in
      $REAL_HOME/.config/systemd/user/. Run the following once to clean
      it up (the .deb's units in /usr/lib/systemd/user/ take over):

    systemctl --user stop onionpress onionpress-heartbeat onionpress-watcher.timer 2>/dev/null
    systemctl --user disable onionpress onionpress-heartbeat onionpress-watcher.timer 2>/dev/null
    rm -f ~/.config/systemd/user/onionpress*.service ~/.config/systemd/user/onionpress*.timer
    systemctl --user daemon-reload

EOF
    fi
fi
POSTINST
chmod 755 "$DEB_ROOT/DEBIAN/postinst"

# Pre-remove: only act on actual removal, not upgrade.
cat > "$DEB_ROOT/DEBIAN/prerm" <<'PRERM'
#!/bin/bash
set -e

case "$1" in
    remove)
        # Globally disable so the units don't relink on next login.
        systemctl --global disable onionpress.service onionpress-heartbeat.service onionpress-watcher.timer 2>/dev/null || true

        # Kill any running tray across all users. Without this, the
        # in-memory tray keeps painting its indicator icon and hitting
        # ENOENT on every poll after /opt/onionpress is removed, until
        # the user logs out. root can pkill by command line regardless
        # of which session owns the process.
        pkill -f /opt/onionpress/onionpress-tray 2>/dev/null || true
        # Sleep briefly so the StatusNotifier D-Bus name drops before
        # the next step (the data.tar.gz removal). Otherwise some
        # shells redraw the stale icon for a few extra seconds.
        sleep 1
        # Also stop any user-session OnionPress launcher / heartbeat
        # that's actively running — same rationale, root knows the
        # binary path.
        pkill -f /opt/onionpress/onionpress 2>/dev/null || true
        ;;
    upgrade|deconfigure|failed-upgrade)
        # No-op — preserve running state across upgrade.
        ;;
esac
PRERM
chmod 755 "$DEB_ROOT/DEBIAN/prerm"

# Post-remove: clean up postinst-generated files on purge.
cat > "$DEB_ROOT/DEBIAN/postrm" <<'POSTRM'
#!/bin/bash
set -e

case "$1" in
    remove)
        # docker.service's drop-in is gone with the package. The rules it
        # put in place stay until the next boot; they name only the
        # WordPress network's bridge, which goes when its containers do.
        systemctl daemon-reload 2>/dev/null || true
        ;;
    purge)
        # Don't auto-remove ~/.onionpress/ — it holds the onion key and
        # DB passwords. Tell the user what's still there.
        cat <<EOF
onionpress purged. User data preserved at:
    ~/.onionpress/   (config, secrets, onion key, logs)
    ~/OnionPress/    (Creations, backups)
Remove manually if you no longer need them.
EOF
        ;;
esac
POSTRM
chmod 755 "$DEB_ROOT/DEBIAN/postrm"

# Build the .deb
#
# Every file in the package is root's. Without --root-owner-group dpkg-deb
# records the staging tree's real owner, so the 2.5.3 .deb listed its files
# as the builder's user and group (dpkg then installs them that way). The
# manual path's tar gets the same: GNU tar spells it --owner/--group, BSD tar
# (macOS, where dpkg-deb is usually missing) --uid/--gid/--uname/--gname.
if command -v dpkg-deb >/dev/null 2>&1; then
    dpkg-deb --root-owner-group --build "$DEB_ROOT" "$DEB_OUT"
else
    # dpkg-deb not available (e.g. building on macOS) — assemble manually.
    # A .deb is an ar archive containing: debian-binary, control.tar.gz, data.tar.gz
    echo "  dpkg-deb not available, assembling .deb manually..."

    DEB_TMP="$STAGE_DIR/deb-parts"
    mkdir -p "$DEB_TMP"

    if tar --version 2>/dev/null | grep -q "GNU tar"; then
        ROOT_OWNED=(--owner=root:0 --group=root:0)
    else
        ROOT_OWNED=(--uid 0 --gid 0 --uname root --gname root)
    fi

    # debian-binary
    echo "2.0" > "$DEB_TMP/debian-binary"

    # macOS's bsdtar stores extended attributes as AppleDouble `._name`
    # members unless told not to: the v2.5.3 .deb was 430 members, 215 of
    # them `._` files, every one installed into /opt/onionpress.
    export COPYFILE_DISABLE=1

    # control.tar.gz
    (cd "$DEB_ROOT/DEBIAN" && tar "${ROOT_OWNED[@]}" -czf "$DEB_TMP/control.tar.gz" .)

    # data.tar.gz — everything except DEBIAN/
    (cd "$DEB_ROOT" && tar "${ROOT_OWNED[@]}" -czf "$DEB_TMP/data.tar.gz" --exclude='./DEBIAN' .)

    # Assemble with ar (BSD ar on macOS needs 'r' not 'rcs')
    # But macOS ar adds Mach-O warnings, so use a simple concatenation approach
    # that matches the Debian ar format: "!<arch>\n" header + members
    python3 -c "
import struct, os, time

def ar_header(name, size):
    name = name.ljust(16)
    mtime = str(int(time.time())).ljust(12)
    uid = '0'.ljust(6)
    gid = '0'.ljust(6)
    mode = '100644'.ljust(8)
    fsize = str(size).ljust(10)
    return f'{name}{mtime}{uid}{gid}{mode}{fsize}\x60\n'.encode()

parts_dir = '$DEB_TMP'
out_path = '$DEB_OUT'

with open(out_path, 'wb') as out:
    out.write(b'!<arch>\n')
    for fname in ['debian-binary', 'control.tar.gz', 'data.tar.gz']:
        fpath = os.path.join(parts_dir, fname)
        fsize = os.path.getsize(fpath)
        out.write(ar_header(fname, fsize))
        with open(fpath, 'rb') as f:
            out.write(f.read())
        # ar members must be 2-byte aligned
        if fsize % 2 != 0:
            out.write(b'\n')

print(f'  .deb assembled: {out_path}')
"
fi

DEB_SIZE=$(du -h "$DEB_OUT" | cut -f1)
echo "  .deb created: $DEB_OUT ($DEB_SIZE, version $VERSION via control file)"

# ─── Clean up ───────────────────────────────────────────────────────────

rm -rf "$STAGE_DIR"

echo ""
echo "✅ Linux package built: $DEB_OUT"
echo ""
echo "Install:     sudo apt install ./build/onionpress.deb"

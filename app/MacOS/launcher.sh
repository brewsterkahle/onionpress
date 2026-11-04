#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# OnionPress: Copyright (C) 2026 Internet Archive and OnionPress contributors
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

# The app bundle's entry point (launcher-wrapper.swift execs it): starts the
# MenubarApp, then has the launcher bring the VM up (`onionpress start-vm`,
# issue #545) while the menu bar icon shows gray. Everything else, the VM's
# creation and recovery included, is app/MacOS/onionpress's and the
# daemon's; this script starts no Colima of its own.

set -e

# Ensure we run natively on Apple Silicon (not under Rosetta).
# macOS LaunchServices may start shell-script app executables under Rosetta
# if it cached a previous version that contained x86_64-only binaries.
# Universal binaries inherit the parent's architecture, so we must re-exec.
if sysctl hw.optional.arm64 2>/dev/null | grep -q ": 1" && [ "$(uname -m)" = "x86_64" ]; then
    exec arch -arm64 "$0" "$@"
fi

# Get directories
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
APP_DIR="$(dirname "$SCRIPT_DIR")"
RESOURCES_DIR="$APP_DIR/Resources"
MENUBAR_APP="$RESOURCES_DIR/MenubarApp"
DATA_DIR="$HOME/.onionpress"
# Named here only for the notes below; this script runs no Colima.
COLIMA_HOME="$DATA_DIR/colima"

mkdir -p -m 700 "$DATA_DIR"

# Log file — daily rotation matching RotatingLog format (launcher-YYYY-MM-DD-001.log)
LOGS_DIR="$DATA_DIR/logs"
mkdir -p "$LOGS_DIR"
_log_date=$(date -u '+%Y-%m-%d')
_log_seq=1
while [ -f "$LOGS_DIR/launcher-${_log_date}-$(printf '%03d' $_log_seq).log" ] && \
      [ "$(wc -c < "$LOGS_DIR/launcher-${_log_date}-$(printf '%03d' $_log_seq).log")" -gt 5242880 ]; do
    _log_seq=$((_log_seq + 1))
done
LOG_FILE="$LOGS_DIR/launcher-${_log_date}-$(printf '%03d' $_log_seq).log"

# Backward compat symlink
ln -sf "$LOG_FILE" "$DATA_DIR/launcher.log"

# Function to log messages
log() {
    # Roll to new file if date changed
    _now_date=$(date -u '+%Y-%m-%d')
    if [ "$_now_date" != "$_log_date" ]; then
        _log_date="$_now_date"
        _log_seq=1
        LOG_FILE="$LOGS_DIR/launcher-${_log_date}-$(printf '%03d' $_log_seq).log"
        ln -sf "$LOG_FILE" "$DATA_DIR/launcher.log"
    fi
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

log "Starting onionpress launcher..."

# Releases through 2.5.1 ran Colima from here without DOCKER_CONFIG, leaving a
# context in the user's Docker config aimed at our VM, made current. That
# config is theirs to change; only say so.
USER_DOCKER_CONFIG="${DOCKER_CONFIG:-$HOME/.docker}"
if grep -qsF "unix://$COLIMA_HOME/default/docker.sock" "$USER_DOCKER_CONFIG"/contexts/meta/*/meta.json; then
    log "NOTE: $USER_DOCKER_CONFIG has a Docker context pointing at OnionPress's VM, left by OnionPress <= 2.5.1 — OnionPress leaves it alone; see 'docker context ls'"
fi

# Colima's start prepends "Include $COLIMA_HOME/ssh_config" to ~/.ssh/config
# (creating it) unless passed --ssh-config=false, as the launcher's every
# start is. While our VM runs that file holds a "Host colima" that collides
# with a Colima of the user's own. Releases through 2.5.1 added the line;
# again, only say so.
if grep -qsxF "Include $COLIMA_HOME/ssh_config" "$HOME/.ssh/config"; then
    log "NOTE: ~/.ssh/config has 'Include $COLIMA_HOME/ssh_config' (a 'Host colima' for OnionPress's VM), left by OnionPress <= 2.5.1 — OnionPress leaves it alone; delete that line to drop it"
fi

# The MenubarApp runs as the hardware's architecture (sysctl, not the
# process's: a shell script may still be under Rosetta here).
if sysctl hw.optional.arm64 2>/dev/null | grep -q ": 1"; then
    HOST_ARCH="arm64"
    log "Detected Apple Silicon (ARM64)"
elif [ "$(uname -m)" = "x86_64" ]; then
    HOST_ARCH="x86_64"
    log "Detected Intel (x86_64)"
else
    HOST_ARCH=$(uname -m)
    log "ERROR: Unsupported architecture: $HOST_ARCH"
    echo "ERROR: Unsupported architecture: $HOST_ARCH" >&2
    exit 1
fi

# Check if standalone menubar app exists
if [ ! -f "$MENUBAR_APP/Contents/MacOS/OnionPress" ]; then
    log "ERROR: Standalone menubar app not found at $MENUBAR_APP/Contents/MacOS/OnionPress"
    echo "ERROR: Application bundle is corrupted. Please reinstall OnionPress." >&2
    exit 1
fi

# Check if THIS bundle's menubar app is already running.
# Anchored: the real process's command line is exactly the executable
# path, with no arguments. A bare substring match also hit any process
# that merely mentioned the path in its arguments (a shell, grep, tail,
# editor...), and then this block exited without starting the app at all
# (seen 2026-10-11: a test script's pgrep -f quoting the same path).
# This bundle's own path, not any bundle's MenubarApp (issue #372): a
# second bundle (a build under test, its own HOME and data dir) never
# started while the installed one ran, and touched .reopen in the other
# install's data dir instead. pgrep -f takes an ERE, so the path's
# metacharacters are quoted ("OnionPress (1).app", the "." of ".app").
MENUBAR_EXE="$MENUBAR_APP/Contents/MacOS/OnionPress"
MENUBAR_EXE_RE=$(printf '%s' "$MENUBAR_EXE" | sed 's/[][*.^$()+?{}|\\]/\\&/g')
if pgrep -u "$(whoami)" -f "^${MENUBAR_EXE_RE}( |$)" >/dev/null 2>&1; then
    echo "" >> "$LOG_FILE"
    echo "============================================================" >> "$LOG_FILE"
    log "=== Launcher restarted (app already running) ==="
    echo "============================================================" >> "$LOG_FILE"
    # Signal the running instance to open browser
    touch "$DATA_DIR/.reopen"
    exit 0
fi

# Launch menubar app immediately so icon appears fast (gray = starting)
log "Launching menu bar application..."
export PYTHONDONTWRITEBYTECODE=1
arch -"$HOST_ARCH" "$MENUBAR_APP/Contents/MacOS/OnionPress" >> "$LOG_FILE" 2>&1 &
MENUBAR_PID=$!
log "Menu bar app launched (PID: $MENUBAR_PID)"

# The VM, through the launcher's one VM start (issue #545): the MenubarApp
# waits for the daemon's runtime_ready (Colima's .initialized, then
# `docker info`) before it runs `onionpress start` or shows a first run's
# welcome screen, so the VM is brought up here, while the icon is gray,
# rather than left to that start. start-vm creates the VM on a first run
# (refusing macOS < 13) and starts a stopped one; its own lines go to
# ~/.onionpress/onionpress.log. Under set -e a failure is caught on the
# same line: the MenubarApp keeps running and says what it is waiting for.
vm_rc=0
"$SCRIPT_DIR/onionpress" start-vm >> "$LOG_FILE" 2>&1 || vm_rc=$?
if [ "$vm_rc" -eq 0 ]; then
    log "Setup complete"
else
    log "ERROR: onionpress start-vm failed (rc=$vm_rc) — see $DATA_DIR/onionpress.log; the MenubarApp will keep waiting for Docker"
fi

# Stay alive so macOS can send us Apple Event quit (osascript -e 'quit app').
# Forward SIGTERM to the MenubarApp so it runs its full cleanup.
trap 'kill -TERM $MENUBAR_PID 2>/dev/null; wait $MENUBAR_PID 2>/dev/null' TERM INT HUP
menubar_rc=0
wait $MENUBAR_PID 2>/dev/null || menubar_rc=$?
log "MenubarApp exited (rc=$menubar_rc), launcher done"

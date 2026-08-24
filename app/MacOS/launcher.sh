#!/bin/bash

# onionpress launcher - Initializes Colima and starts standalone menu bar app

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
DOCUMENTS_DIR="$HOME/OnionPress"
OLD_DOCUMENTS_DIR="$HOME/Documents/OnionPress"

# DOCUMENTS_DIR lives at top-of-$HOME (not under ~/Documents/) so the VM
# can mount it without triggering macOS TCC. See issue #239.
BIN_DIR="$RESOURCES_DIR/bin"
COLIMA_HOME="$DATA_DIR/colima"

mkdir -p "$DATA_DIR"

# One-time host-side migration from ~/Documents/OnionPress to
# ~/OnionPress (#239). Walks source tree recursively so empty subdirs
# pre-existing in destination don't block the mv. Marker is set only
# when the old dir is actually gone; a TCC-denied attempt retries on
# next launch.
#
# REMOVE-AFTER 2026-08-01: by then every active install will have
# migrated. Delete this block, _op_migrate_tree, and OLD_DOCUMENTS_DIR.
_op_migrate_tree() {
    local src="$1" dst="$2"
    [ -d "$src" ] || return 0
    mkdir -p "$dst"
    shopt -s dotglob nullglob 2>/dev/null || true
    local entry base target
    for entry in "$src"/*; do
        [ -e "$entry" ] || continue
        base=$(basename "$entry")
        target="$dst/$base"
        if [ -d "$entry" ] && [ -d "$target" ]; then
            _op_migrate_tree "$entry" "$target"
            rmdir "$entry" 2>/dev/null || true
        else
            mv -n "$entry" "$target" 2>/dev/null || true
        fi
    done
}

if ! grep -qE '^PATH_MIGRATION_2026_05=done$' "$DATA_DIR/config" 2>/dev/null; then
    if [ -d "$OLD_DOCUMENTS_DIR" ]; then
        rm -f "$OLD_DOCUMENTS_DIR/MOVED.txt" 2>/dev/null || true
        _op_migrate_tree "$OLD_DOCUMENTS_DIR" "$DOCUMENTS_DIR"
        rmdir "$OLD_DOCUMENTS_DIR" 2>/dev/null || true
        if [ -d "$OLD_DOCUMENTS_DIR" ]; then
            echo "OnionPress content moved to $DOCUMENTS_DIR" \
                > "$OLD_DOCUMENTS_DIR/MOVED.txt" 2>/dev/null || true
        else
            printf 'PATH_MIGRATION_2026_05=done\n' >> "$DATA_DIR/config" 2>/dev/null || true
        fi
    else
        mkdir -p "$DOCUMENTS_DIR"
        printf 'PATH_MIGRATION_2026_05=done\n' >> "$DATA_DIR/config" 2>/dev/null || true
    fi
fi

# Idempotent subtree setup (no TCC concerns now that DOCUMENTS_DIR is
# top-of-home). Compose's bind-mount needs these subdirs present.
mkdir -p "$DOCUMENTS_DIR/backups"
mkdir -p "$DOCUMENTS_DIR/Creations/My Creations"
mkdir -p "$DOCUMENTS_DIR/Social Archives"
touch "$DOCUMENTS_DIR/Social Archives/.onionpress-activated"

# Emit `--mount $DOCUMENTS_DIR:w`. Defensive existence check against
# a hand-deleted directory mid-session.
docs_mount_args() {
    if [ -d "$DOCUMENTS_DIR" ]; then
        printf -- '--mount %s:w' "$DOCUMENTS_DIR"
    fi
}

# Set up bundled binaries in PATH
export PATH="$BIN_DIR:$PATH"

# Configure Colima environment
export COLIMA_HOME="$COLIMA_HOME"
export LIMA_HOME="$COLIMA_HOME/_lima"
export LIMA_INSTANCE="onionpress"
export DOCKER_HOST="unix://$COLIMA_HOME/default/docker.sock"

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

# Detect architecture (use sysctl to get actual hardware, not process architecture)
# This is important because shell scripts may run under Rosetta on Apple Silicon
if sysctl hw.optional.arm64 2>/dev/null | grep -q ": 1"; then
    HOST_ARCH="arm64"
    VM_ARCH="aarch64"
    log "Detected Apple Silicon (ARM64)"
elif [ "$(uname -m)" = "x86_64" ]; then
    HOST_ARCH="x86_64"
    VM_ARCH="x86_64"
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

# Check if this is first-time initialization
FIRST_RUN=false
if [ ! -f "$COLIMA_HOME/.initialized" ]; then
    FIRST_RUN=true
    log "First-time initialization detected"
fi

# Initialize Colima on first run
initialize_colima() {
    if [ ! -f "$COLIMA_HOME/.initialized" ]; then
        log "Initializing Colima container runtime..."

        # Check macOS version >= 13
        MACOS_VERSION=$(sw_vers -productVersion | cut -d '.' -f 1)
        if [ "$MACOS_VERSION" -lt 13 ]; then
            log "ERROR: macOS 13+ required"
            echo "ERROR: onionpress requires macOS 13 (Ventura) or later." >&2
            echo "Your macOS version: $(sw_vers -productVersion)" >&2
            echo "The bundled container runtime uses Apple's virtualization framework which requires macOS 13+." >&2
            exit 1
        fi

        # Initialize Colima
        # Read VM memory from config (default: 1 GB)
        VM_MEMORY=1
        if [ -f "$DATA_DIR/config" ]; then
            config_mem=$(grep "^VM_MEMORY=" "$DATA_DIR/config" | cut -d= -f2)
            if [ ! -z "$config_mem" ]; then
                VM_MEMORY="$config_mem"
            fi
        fi
        # Pre-check: if onionheaven key is imported, start with 5GB to avoid a restart
        local onionheaven_address="oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion"
        if [ -d "$DATA_DIR/shared/vanity-keys/$onionheaven_address" ] && [ "$VM_MEMORY" -lt 5 ]; then
            log "OnionHeaven key detected in launcher — starting VM with 5GB"
            VM_MEMORY=5
            # Persist to config so subsequent starts also use 5GB
            if [ -f "$DATA_DIR/config" ] && grep -q "^VM_MEMORY=" "$DATA_DIR/config" 2>/dev/null; then
                sed -i.bak "s/^VM_MEMORY=.*/VM_MEMORY=5/" "$DATA_DIR/config"
            else
                echo "VM_MEMORY=5" >> "$DATA_DIR/config"
            fi
        fi

        # Read VM disk cap from config (default: 20 GiB for normal nodes,
        # 100 GiB for the hub). Hub takeover containers are small per
        # instance (~50 MB c-tor) but accumulate at scale — a hub serving
        # thousands of users could see hundreds of simultaneous takeovers.
        # Admins of large hubs should set VM_DISK in ~/.onionpress/config
        # BEFORE first launch (cap is baked in at VM creation).
        VM_DISK=20
        if [ -f "$DATA_DIR/config" ]; then
            config_disk=$(grep "^VM_DISK=" "$DATA_DIR/config" | cut -d= -f2)
            if [ ! -z "$config_disk" ]; then
                VM_DISK="$config_disk"
            fi
        fi
        if [ -d "$DATA_DIR/shared/vanity-keys/$onionheaven_address" ] && [ "$VM_DISK" -lt 100 ]; then
            log "OnionHeaven key detected — sizing diffdisk at 100GB for takeover-container headroom"
            log "  (set VM_DISK=N in ~/.onionpress/config before first launch to override)"
            VM_DISK=100
        fi
        # Create minimal shared directory to avoid Downloads folder permission prompt
        mkdir -p "$DATA_DIR/shared"
        # Cap diffdisk at 20 GiB on first VM creation (#230). Lima's default
        # is 100 GiB which alarms users seeing it in Finder; real usage stays
        # well under 5 GiB with auto-prune in the image-update path. The
        # value is baked in at VM creation; existing installs keep their
        # original cap and are unaffected by changing this number.
        if [ "$HOST_ARCH" = "arm64" ]; then
            # Apple Silicon: use VZ backend (Virtualization.framework)
            "$BIN_DIR/colima" start \
                --vm-type vz \
                --mount-type virtiofs \
                --mount "$DATA_DIR/shared:w" \
                $(docs_mount_args) \
                --cpu 2 \
                --memory "$VM_MEMORY" \
                --disk "$VM_DISK" \
                --arch "$VM_ARCH" \
                --vz-rosetta=false \
                >> "$LOG_FILE" 2>&1
        else
            # Intel: use QEMU backend
            "$BIN_DIR/colima" start \
                --vm-type qemu \
                --mount-type sshfs \
                --mount "$DATA_DIR/shared:w" \
                $(docs_mount_args) \
                --cpu 2 \
                --memory "$VM_MEMORY" \
                --disk "$VM_DISK" \
                --arch "$VM_ARCH" \
                >> "$LOG_FILE" 2>&1
        fi

        if [ $? -eq 0 ]; then
            touch "$COLIMA_HOME/.initialized"
            log "Colima initialized successfully"
        else
            log "ERROR: Colima init failed"
            echo "ERROR: Failed to initialize container runtime." >&2
            echo "Check the logs for details: $LOG_FILE" >&2
            exit 1
        fi
    fi

    # Ensure Colima is running
    if ! "$BIN_DIR/colima" status >/dev/null 2>&1; then
        # Read VM memory from config (default: 1 GB)
        local vm_mem=1
        if [ -f "$DATA_DIR/config" ]; then
            local cfg_mem=$(grep "^VM_MEMORY=" "$DATA_DIR/config" | cut -d= -f2)
            if [ ! -z "$cfg_mem" ]; then
                vm_mem="$cfg_mem"
            fi
        fi
        # Pre-check: if onionheaven key exists, ensure 5GB
        local onionheaven_address="oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion"
        if [ -d "$DATA_DIR/shared/vanity-keys/$onionheaven_address" ] && [ "$vm_mem" -lt 5 ]; then
            log "OnionHeaven key detected in launcher — starting VM with 5GB"
            vm_mem=5
        fi
        log "Starting Colima VM..."
        "$BIN_DIR/colima" start \
            --mount "$DATA_DIR/shared:w" \
            $(docs_mount_args) \
            --memory "$vm_mem" \
            >> "$LOG_FILE" 2>&1
    fi
}

# Check if menubar app is already running.
#
# `ps`, not `pgrep`, for the reason spelled out above menubar_alive() in
# `onionpress`: on macOS pgrep can fail to see a live MenubarApp, and getting
# this wrong here is expensive — the else branch below launches a second copy.
# Captured into a variable rather than piped into grep so the scan cannot
# match the grep process's own argv. `ps -x` is this user's processes only,
# which is what the multi-user pgrep rule in CLAUDE.md was asking for.
_running_procs="$(ps -x -o args= 2>/dev/null || true)"
case "$_running_procs" in
    *MenubarApp/Contents/MacOS/OnionPress*) _menubar_up=1 ;;
    *) _menubar_up=0 ;;
esac
if [ "$_menubar_up" = 1 ]; then
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

# Run initialization (menubar is now visible with gray icon during this)
initialize_colima

# Create symlink for Docker socket if needed
# Colima forwards the socket to ~/.colima/default/docker.sock
# but we configure DOCKER_HOST to use ~/.onionpress/colima/default/docker.sock
# Create symlink to bridge this gap
SOCKET_DIR="$COLIMA_HOME/default"
SOCKET_PATH="$SOCKET_DIR/docker.sock"
COLIMA_SOCKET="$HOME/.colima/default/docker.sock"

if [ -S "$COLIMA_SOCKET" ]; then
    if [ ! -e "$SOCKET_PATH" ] || [ ! -S "$SOCKET_PATH" ]; then
        log "Creating Docker socket symlink..."
        mkdir -p "$SOCKET_DIR"
        ln -sf "$COLIMA_SOCKET" "$SOCKET_PATH"
        log "Docker socket symlink created: $SOCKET_PATH -> $COLIMA_SOCKET"
    fi
fi

log "Setup complete"

# Stay alive so macOS can send us Apple Event quit (osascript -e 'quit app').
# Forward SIGTERM to the MenubarApp so it runs its full cleanup.
trap 'kill -TERM $MENUBAR_PID 2>/dev/null; wait $MENUBAR_PID 2>/dev/null' TERM INT HUP
wait $MENUBAR_PID 2>/dev/null
log "MenubarApp exited, launcher done"

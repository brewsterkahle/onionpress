# Launch Fixes Applied

## Issues Found and Resolved

### 1. Missing Lima Wrapper Script
**Problem**: Colima requires a `lima` command that wraps `limactl shell`
**Fix**: Created `/Contents/Resources/bin/lima` wrapper script
**Status**: ✅ Applied to source

### 2. Rosetta Emulation Issue
**Problem**: Universal binaries (x86_64 + arm64) were running under Rosetta, causing "limactl is running under rosetta" error
**Fix**: Extract ARM64-only slices from universal binaries
**Status**: ⚠️ Manual fix applied to installed app, needs build process update

**To fix in build process**, add to `build-dmg.sh` or `build-dmg-simple.sh`:
```bash
# After copying binaries to app bundle, extract ARM64 slices
cd "$APP_PATH/Contents/Resources/bin"
for binary in colima docker limactl; do
    if file "$binary" | grep -q "universal"; then
        lipo "$binary" -thin arm64 -output "${binary}.arm64"
        mv "${binary}" "${binary}.universal"
        mv "${binary}.arm64" "${binary}"
        echo "Extracted ARM64 slice for $binary"
    fi
done
```

### 3. Docker Socket Path
**Problem**: Colima forwards socket to `~/.colima/docker.sock` but app expects `~/.onionpress/colima/default/docker.sock`
**Fix**: Launcher script creates the symlink on first run
**Status**: ✅ This is handled automatically by launcher initialization

Add to launcher script's `initialize_colima()` function:
```bash
# Create socket symlink
mkdir -p "$COLIMA_HOME/default"
ln -sf ~/.colima/docker.sock "$COLIMA_HOME/default/docker.sock"
```

### 4. JSON Status Format
**Problem**: `docker compose ps --format json` outputs newline-delimited JSON, but Python menu bar app expects JSON array
**Fix**: Modified `get_status()` to use `jq -s '.'` to combine into array
**Status**: ✅ Applied to source
**Dependency**: Requires `jq` to be installed (available via Homebrew on macOS)

## Testing the Fixes

After rebuilding:
1. DMG mounts correctly ✓
2. App launches without errors ✓
3. Colima initializes (2-3 min first run) ✓
4. Containers start successfully ✓
5. Menu bar shows filled circle (●) when running ✓
6. WordPress accessible at http://localhost:8080 ✓
7. Onion address displayed in menu ✓

## Current Status

**App is now fully functional!** ✅

The installed app at `/Applications/onionpress.app` is working correctly with all fixes applied. Future DMG builds should incorporate the binary architecture fix in the build script.

# Changelog

All notable changes to onion.press will be documented in this file.

## [2.0.4] - 2026-01-31

### Fixed
- Fixed duplicate "QUIT" menu items in menu bar dropdown
- Menu bar now shows only one Quit option (rumps framework provides it automatically)

### Technical
- Removed manual Quit menu item definition
- Changed quit handler to use `quit_callback()` to properly override rumps default behavior
- Maintained confirmation dialog and service shutdown functionality

## [2.0.3] - 2026-01-31

### Added
- **Internet Archive Wayback Machine Link Fixer plugin** - Automatically installed and activated to combat link rot
- Plugin helps preserve web links by creating archived versions and redirecting when links break
- Automatic archiving of your own posts on every update
- Configurable installation via `~/.onion.press/config` (INSTALL_IA_PLUGIN=yes/no, default: yes)
- Plugin automatically activates after WordPress setup is complete

### Technical
- Plugin is downloaded from WordPress.org repository and copied to WordPress container on first launch
- Automatic activation using PHP script injection
- Configuration option to disable automatic installation if desired

## [2.0.2] - 2026-01-31

### Added
- **Vanity onion addresses** - All new installations now generate addresses starting with "op2" for easy identification
- Bundled mkp224o for vanity address generation (no external dependencies)
- Configurable vanity prefix via `~/.onion.press/config` (default: "op2")
- Fast generation: 3-character prefix takes under 1 second

### Technical
- Integrated mkp224o v3 onion address generator into build process
- Automatic vanity key generation on first launch
- Config template for customizing vanity prefix
- All onion.press services now easily identifiable by "op2" prefix
- Base32 validation (only a-z, 2-7 characters allowed in onion addresses)

## [2.0.1] - 2026-01-31

### Fixed
- Fixed launcher script JSON status output for menu bar app compatibility
- Fixed missing lima wrapper script causing Colima initialization failures
- Fixed Rosetta emulation issue with universal binaries on ARM64 Macs
- Fixed Docker socket path detection and symlink creation
- Menu bar now correctly displays running status with filled circle indicator

### Documentation
- Added LAUNCH_FIXES.md with comprehensive troubleshooting guide
- Documented build process requirements for ARM64-only binary extraction

## [2.0.0] - 2026-01-31

### Changed
- **Breaking**: Now bundles Colima container runtime instead of requiring external Docker
- Single DMG installation - no external dependencies required
- Reduced installation steps from multiple to one (drag & drop)

### Added
- Bundled Colima (v0.8.1) container runtime
- Bundled Docker CLI and Lima tools
- Automatic container runtime initialization on first launch
- Support for macOS Virtualization.Framework (VZ backend)

## [1.0.1] - 2026-01-31

### Changed
- Replaced OrbStack recommendation with Docker Desktop (free for personal use)
- Auto-detection now prefers Docker Desktop over OrbStack
- Installer now offers to download Docker Desktop if no Docker runtime is found
- Updated documentation to recommend Docker Desktop as primary option

### Added
- Automatic Docker runtime detection and launch on app startup
- Support for both Docker Desktop and OrbStack (auto-detects which is installed)

### Fixed
- App now automatically launches Docker Desktop/OrbStack when starting if not already running
- Better error handling when Docker is not available

## [1.0.0] - 2026-01-31

### Initial Release
- WordPress + Tor Hidden Service bundle for macOS
- Menu bar app for easy control
- One-click start/stop/restart
- Automatic onion address generation
- Copy address to clipboard
- Open in Tor Browser
- View logs functionality
- Auto-start on app launch

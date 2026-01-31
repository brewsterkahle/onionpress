# Changelog

All notable changes to onion.press will be documented in this file.

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

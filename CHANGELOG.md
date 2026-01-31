# Changelog

All notable changes to onion.press will be documented in this file.

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

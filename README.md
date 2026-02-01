<div align="center">
  <img src="logo.png" alt="onion.press logo" width="400">
</div>

# onion.press

**Easy-to-install WordPress with Tor Hidden Service for macOS**

> **Current Version: 2.0.4** - Now with vanity onion addresses!

onion.press is a macOS application that bundles WordPress with a Tor hidden service, allowing you to run a WordPress blog accessible only through the Tor network.

## Features

- 🧅 **Tor Hidden Service**: Your WordPress site is automatically configured as a Tor onion service
- ✨ **Vanity Onion Addresses**: All installations generate addresses starting with "op2" for easy identification
- 📚 **Internet Archive Integration**: Automatically installs the [Wayback Machine Link Fixer](https://wordpress.org/plugins/internet-archive-wayback-machine-link-fixer/) plugin to combat link rot
- 🐳 **Container-Based**: Uses Docker containers for easy management and isolation
- 📱 **Menu Bar App**: Simple menu bar interface to control your site
- 🚀 **One-Click Install**: Download the DMG, drag to Applications, and launch
- 🔒 **Privacy-First**: Your site is only accessible through Tor

## Requirements

- macOS 13.0 (Ventura) or later
- Python 3 (pre-installed on macOS 12.3+, or install from [python.org](https://www.python.org/downloads/))
- Internet connection for first-time setup

**No external Docker installation required!** All container runtime components are bundled.

## Installation

1. Download the latest `onion.press.dmg` from the [releases page](https://github.com/brewsterkahle/onion.press/releases)
2. Open the DMG and drag `onion.press.app` to your Applications folder
3. Launch onion.press from Applications
4. On first launch:
   - The app will generate your vanity onion address (starting with "op2") - takes < 1 second
   - The app will initialize its bundled container runtime (Colima) - takes ~2-3 minutes
   - It will download WordPress, MariaDB, and Tor container images (~1GB)
   - Total setup: 3-5 minutes depending on your internet connection
   - Subsequent launches are instant

## Usage

### Menu Bar Controls

Once installed, onion.press appears in your menu bar with an onion icon (🧅):

- **Copy Onion Address**: Copy your .onion URL to clipboard
- **Open in Tor Browser**: Launch Tor Browser with your site (requires Tor Browser to be installed)
- **Start/Stop/Restart**: Control the WordPress service
- **View Logs**: Open logs in Console.app for troubleshooting

### Accessing Your Site

1. Your onion address is displayed in the menu bar dropdown (starts with "op2" for easy identification)
2. Install [Tor Browser](https://www.torproject.org/download/) to access .onion sites
3. Copy your onion address and paste it into Tor Browser
4. Complete the WordPress setup wizard

**Vanity Address Configuration**: You can customize the prefix in `~/.onion.press/config` before first launch. See the config file for details on generation times for different prefix lengths.

### Internet Archive Wayback Machine Link Fixer

onion.press automatically installs and activates the [Internet Archive Wayback Machine Link Fixer plugin](https://wordpress.org/plugins/internet-archive-wayback-machine-link-fixer/), which helps combat link rot by:

- Automatically scanning your posts for outbound links
- Creating archived versions in the Wayback Machine
- Redirecting to archived versions when links break
- Archiving your own posts on every update

**The plugin is enabled by default.** To disable automatic installation, edit `~/.onion.press/config` before first launch:
```bash
INSTALL_IA_PLUGIN=no
```

For increased daily link processing, you can add your free Archive.org API credentials in the plugin settings after setup.

### Local Testing

For testing purposes, your WordPress site is also available at:
- http://localhost:8080 (only accessible from your Mac)

## Architecture

onion.press uses:
- **WordPress**: Latest official WordPress container
- **MariaDB**: Latest MariaDB for the database
- **Tor**: Hidden service container that exposes WordPress as an onion service
- **mkp224o**: Vanity onion address generator (generates addresses with custom prefixes)
- **Colima**: Bundled container runtime using Apple's virtualization framework
- **Lima**: VM management layer (bundled)
- **Docker CLI**: Container management tools (bundled)

All data is stored in:
- `~/.onion.press/` - Application data, logs, config, and Colima VM
- Docker volumes for WordPress, database, and Tor keys

## Building from Source

To build the DMG installer:

```bash
cd onion.press
./build/build-dmg.sh
```

This will create `onion.press.dmg` in the `build/` directory.

## Troubleshooting

### "Python 3 not found"
Install Python 3 from [python.org](https://www.python.org/downloads/) or via Homebrew:
```bash
brew install python3
```

### "macOS version too old"
onion.press requires macOS 13 (Ventura) or later for Apple's native virtualization framework.

### Containers won't start
Check the logs via the menu bar app or run:
```bash
tail -f ~/.onion.press/onion.press.log
tail -f ~/.onion.press/colima/colima.log
```

### Onion address not generating
Wait 30-60 seconds for Tor to generate your onion address. Check logs if it takes longer.

### Reset everything
If you encounter persistent issues:
```bash
# Stop the app first, then:
rm -rf ~/.onion.press
# Relaunch onion.press
```

## Security Notes

- Change the default WordPress admin password immediately after installation
- The default database passwords are set in `docker-compose.yml` - consider changing them
- Your site is only accessible via Tor by default (port 8080 is localhost-only for testing)
- Keep WordPress and plugins updated regularly

## Uninstalling

1. Stop the service from the menu bar app
2. Quit onion.press
3. Move `onion.press.app` to Trash
4. Remove data directory: `rm -rf ~/.onion.press`
5. Remove Docker volumes:
   ```bash
   docker volume rm onionpress-tor-keys onionpress-wordpress-data onionpress-db-data
   ```

## License

MIT License - See LICENSE file for details

## Credits

Built with:
- [WordPress](https://wordpress.org/) - Open source content management system
- [Tor Project](https://www.torproject.org/) - Anonymous communication network
- [Colima](https://github.com/abiosoft/colima) - Container runtime for macOS
- [Lima](https://github.com/lima-vm/lima) - Linux virtual machines for macOS
- [mkp224o](https://github.com/cathugger/mkp224o) - Vanity onion address generator
- [rumps](https://github.com/jaredks/rumps) - Python library for macOS menu bar apps

## Support

For issues, questions, or contributions, please visit the GitHub repository.

# onion.press

**Easy-to-install WordPress with Tor Hidden Service for macOS**

onion.press is a macOS application that bundles WordPress with a Tor hidden service, allowing you to run a WordPress blog accessible only through the Tor network.

## Features

- 🧅 **Tor Hidden Service**: Your WordPress site is automatically configured as a Tor onion service
- 🐳 **Container-Based**: Uses Docker containers for easy management and isolation
- 📱 **Menu Bar App**: Simple menu bar interface to control your site
- 🚀 **One-Click Install**: Download the DMG, drag to Applications, and launch
- 🔒 **Privacy-First**: Your site is only accessible through Tor

## Requirements

- macOS 11.0 (Big Sur) or later
- Python 3 (pre-installed on macOS 12.3+, or install from [python.org](https://www.python.org/downloads/))
- Container runtime (OrbStack recommended - will auto-install if needed)

## Installation

1. Download the latest `onion.press.dmg` from the releases page
2. Open the DMG and drag `onion.press.app` to your Applications folder
3. Launch onion.press from Applications
4. On first launch:
   - If OrbStack/Docker is not installed, you'll be prompted to install OrbStack
   - The app will download WordPress, MariaDB, and Tor container images (~1GB)
   - This takes 3-5 minutes depending on your internet connection

## Usage

### Menu Bar Controls

Once installed, onion.press appears in your menu bar with an onion icon (🧅):

- **Copy Onion Address**: Copy your .onion URL to clipboard
- **Open in Tor Browser**: Launch Tor Browser with your site (requires Tor Browser to be installed)
- **Start/Stop/Restart**: Control the WordPress service
- **View Logs**: Open logs in Console.app for troubleshooting

### Accessing Your Site

1. Your onion address is displayed in the menu bar dropdown
2. Install [Tor Browser](https://www.torproject.org/download/) to access .onion sites
3. Copy your onion address and paste it into Tor Browser
4. Complete the WordPress setup wizard

### Local Testing

For testing purposes, your WordPress site is also available at:
- http://localhost:8080 (only accessible from your Mac)

## Architecture

onion.press uses:
- **WordPress**: Latest official WordPress container
- **MariaDB**: Latest MariaDB for the database
- **Tor**: Hidden service container that exposes WordPress as an onion service
- **OrbStack/Docker**: Container runtime (lightweight alternative to Docker Desktop)

All data is stored in Docker volumes, persisted at:
- `~/.onion.press/` - Application data and logs

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

### "Container runtime not found"
The app should prompt to install OrbStack automatically. If not, you can:
- Install [OrbStack](https://orbstack.dev/) manually
- Or install [Docker Desktop](https://www.docker.com/products/docker-desktop/)

### Containers won't start
Check the logs via the menu bar app or run:
```bash
tail -f ~/.onion.press/onion.press.log
```

### Onion address not generating
Wait 30-60 seconds for Tor to generate your onion address. Check logs if it takes longer.

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
- [WordPress](https://wordpress.org/)
- [Tor Project](https://www.torproject.org/)
- [OrbStack](https://orbstack.dev/)
- [rumps](https://github.com/jaredks/rumps) - Python library for macOS menu bar apps

## Support

For issues, questions, or contributions, please visit the GitHub repository.

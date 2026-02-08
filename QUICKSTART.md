# onionpress Quick Start Guide

> **Version 2.0.2** - Now with bundled container runtime and vanity onion addresses!

## Installation

1. Download `onionpress.dmg` from the [releases page](https://github.com/brewsterkahle/onionpress/releases)
2. Open the DMG and drag `onionpress.app` to Applications
3. Launch from Applications - that's it!

**No external Docker installation needed!** Everything is bundled.

## Usage

onionpress includes both a menu bar app and command-line tool.

### Menu Bar App (Recommended)

Simply launch `onionpress.app` from Applications. The onion icon will appear in your menu bar.

### Command Line

Alternatively, use the command-line tool. Open Terminal and run:

```bash
/Applications/onionpress.app/Contents/MacOS/onionpress start
```

This will:
1. Generate your vanity onion address starting with "op2" (< 1 second)
2. Initialize the bundled container runtime (Colima) - first launch only (~2-3 minutes)
3. Download WordPress, MariaDB, and Tor containers (~1GB, one-time)
4. Start your WordPress site with your new onion address

## Quick Commands

```bash
# Start the service
/Applications/onionpress.app/Contents/MacOS/onionpress start

# Get your onion address (starts with "op2")
/Applications/onionpress.app/Contents/MacOS/onionpress address

# Check status
/Applications/onionpress.app/Contents/MacOS/onionpress status

# View logs
/Applications/onionpress.app/Contents/MacOS/onionpress logs

# Stop the service
/Applications/onionpress.app/Contents/MacOS/onionpress stop
```

## Optional: Create an Alias

Add this to your `~/.zshrc` or `~/.bashrc`:

```bash
alias onion-press="/Applications/onionpress.app/Contents/MacOS/onionpress"
```

Then you can just run:
```bash
onion-press start
onion-press address
```

## Accessing Your WordPress Site

### Via Tor (Recommended)

1. Install [Tor Browser](https://www.torproject.org/download/)
2. Get your onion address (will start with "op2"):
   ```bash
   /Applications/onionpress.app/Contents/MacOS/onionpress address
   ```
   Or click the menu bar icon and copy it from there.
3. Open Tor Browser and visit: `http://[your-address].onion`
4. Complete WordPress setup

### Local Testing (Optional)

Your site is also available at `http://localhost:8080` for testing (only accessible from your Mac).

### Vanity Address Customization

Want a different prefix? Edit `~/.onionpress/config` before first launch:
```bash
VANITY_PREFIX=blog  # or any base32 string (a-z, 2-7)
```
Note: Longer prefixes take exponentially longer to generate (see config file for estimates).

## First-Time Setup

After starting onionpress and accessing your site:

1. Choose your language
2. Create an admin account
3. Set your site title
4. Start blogging!

## Troubleshooting

### First launch takes a while
The first launch initializes the bundled container runtime (Colima) which takes 2-3 minutes. Subsequent launches are instant.

### Can't see onion address
Wait 30-60 seconds after starting for Tor to initialize, then run:
```bash
/Applications/onionpress.app/Contents/MacOS/onionpress address
```
Or check the menu bar dropdown.

### Containers won't start
View detailed logs:
```bash
/Applications/onionpress.app/Contents/MacOS/onionpress logs
```

Or check the log file:
```bash
tail -f ~/.onionpress/onionpress.log
```

### Port 8080 already in use
Stop other services using port 8080, or edit the docker-compose.yml to use a different port.

### Reset everything
If you encounter persistent issues:
```bash
# Stop the app, then:
rm -rf ~/.onionpress
# Relaunch onionpress
```

## Uninstalling

1. Stop the service:
   ```bash
   /Applications/onionpress.app/Contents/MacOS/onion-press-cli stop
   ```

2. Remove the app:
   ```bash
   rm -rf /Applications/onionpress.app
   ```

3. Remove data (optional):
   ```bash
   rm -rf ~/.onionpress
   docker volume rm onionpress-tor-keys onionpress-wordpress-data onionpress-db-data
   ```

## Security Notes

- Change the default WordPress admin password immediately
- Keep WordPress and plugins updated
- Your site is only accessible via Tor by default (except localhost:8080 for testing)
- Consider changing database passwords in the docker-compose.yml file

## Getting Help

- Check logs: `/Applications/onionpress.app/Contents/MacOS/onionpress logs`
- View documentation: See README.md
- Report issues: [GitHub issues page](https://github.com/brewsterkahle/onionpress/issues)

## What's New in v2.0.2

- **Vanity onion addresses**: All new installations generate addresses starting with "op2"
- **Instant generation**: Vanity address creation takes < 1 second
- **Configurable prefix**: Customize your onion address prefix before first launch
- **Bundled runtime**: No external Docker/OrbStack needed - everything is included

Enjoy your private WordPress site! 🧅

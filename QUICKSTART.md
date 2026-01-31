# onion.press Quick Start Guide

## Installation

1. Download `onion.press.dmg`
2. Open the DMG and drag `onion.press.app` to Applications
3. That's it!

## Usage

onion.press includes a command-line tool. Open Terminal and run:

```bash
/Applications/onion.press.app/Contents/MacOS/onion-press-cli start
```

This will:
1. Check if Docker/OrbStack is installed
2. Offer to install OrbStack if needed (~100MB download)
3. Download WordPress, MariaDB, and Tor containers (~1GB, one-time)
4. Start your WordPress site
5. Generate your .onion address

## Quick Commands

```bash
# Start the service
/Applications/onion.press.app/Contents/MacOS/onion-press-cli start

# Get your onion address
/Applications/onion.press.app/Contents/MacOS/onion-press-cli address

# Check status
/Applications/onion.press.app/Contents/MacOS/onion-press-cli status

# View logs
/Applications/onion.press.app/Contents/MacOS/onion-press-cli logs

# Stop the service
/Applications/onion.press.app/Contents/MacOS/onion-press-cli stop
```

## Optional: Create an Alias

Add this to your `~/.zshrc` or `~/.bashrc`:

```bash
alias onion-press="/Applications/onion.press.app/Contents/MacOS/onion-press-cli"
```

Then you can just run:
```bash
onion-press start
onion-press address
```

## Accessing Your WordPress Site

### Via Tor (Recommended)

1. Install [Tor Browser](https://www.torproject.org/download/)
2. Get your onion address:
   ```bash
   /Applications/onion.press.app/Contents/MacOS/onion-press-cli address
   ```
3. Open Tor Browser and visit: `http://[your-address].onion`
4. Complete WordPress setup

### Local Testing (Optional)

Your site is also available at `http://localhost:8080` for testing (only accessible from your Mac).

## First-Time Setup

After starting onion.press and accessing your site:

1. Choose your language
2. Create an admin account
3. Set your site title
4. Start blogging!

## Troubleshooting

### "Docker not found"
onion.press will prompt you to install OrbStack. Click "Install OrbStack" and wait for it to complete.

### Can't see onion address
Wait 30-60 seconds for Tor to generate your address, then run:
```bash
/Applications/onion.press.app/Contents/MacOS/onion-press-cli address
```

### Containers won't start
Check if Docker/OrbStack is running:
```bash
docker ps
```

View logs:
```bash
/Applications/onion.press.app/Contents/MacOS/onion-press-cli logs
```

### Port 8080 already in use
Stop other services using port 8080, or edit the docker-compose.yml to use a different port.

## Uninstalling

1. Stop the service:
   ```bash
   /Applications/onion.press.app/Contents/MacOS/onion-press-cli stop
   ```

2. Remove the app:
   ```bash
   rm -rf /Applications/onion.press.app
   ```

3. Remove data (optional):
   ```bash
   rm -rf ~/.onion.press
   docker volume rm onionpress-tor-keys onionpress-wordpress-data onionpress-db-data
   ```

## Security Notes

- Change the default WordPress admin password immediately
- Keep WordPress and plugins updated
- Your site is only accessible via Tor by default (except localhost:8080 for testing)
- Consider changing database passwords in the docker-compose.yml file

## Getting Help

- Check logs: `/Applications/onion.press.app/Contents/MacOS/onion-press-cli logs`
- View documentation: See README.md
- Report issues: GitHub issues page

Enjoy your private WordPress site! 🧅

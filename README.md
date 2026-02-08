<div align="center">
  <img src="logo.png" alt="onionpress logo" width="400">

  **[Product Page](https://brewsterkahle.github.io/onionpress/)**
</div>

# OnionPress

**Easy and free self-hosted web server for macOS**

onionpress is a macOS application that bundles WordPress with a Tor onion service, giving you an easy and free self-hosted web server accessible even when you are on a private network.

⚠️ This is **not** a securely-anonymous publishing tool-- maybe someday, but not yet.

## Features

- 💻 **Easy and Free Self-Hosted**: Run your own web server without monthly hosting fees or technical complexity
- 🧅 **Tor Onion Service**: Your WordPress site is automatically configured as a Tor onion service (requires website visitors to use Tor or Brave browsers)
- 🔐 **End-to-End Encrypted**: Built-in encryption without needing HTTPS certificates or SSL setup
- 🌐 **No DNS Registration Needed**: Your .onion address works immediately - no domain registration, no DNS configuration
- 🏠 **Works Behind Firewalls**: Runs on home, school, or work networks even behind firewalls or NAT - no port forwarding required
- ✨ **Vanity Onion Addresses**: All installations generate addresses starting with "op2" for easy identification
- 📚 **Internet Archive Integration**: Automatically installs the [Wayback Machine Link Fixer](https://wordpress.org/plugins/internet-archive-wayback-machine-link-fixer/) plugin to combat link rot by default
- 🐳 **Container-Based**: Uses Docker containers for easy management and isolation
- 📱 **Menu Bar App**: Simple menu bar interface to control your site
- 🚀 **One-Click Install**: Download the DMG, drag to Applications, and launch
- 🌐 **Tor-Only Access**: Your site is only accessible through Tor (e.g. Tor and Brave Browsers)

## Requirements

- macOS 13.0 (Ventura) or later
- Internet connection

## Installation

1. Download the latest `onionpress.dmg` from the [releases page](https://github.com/brewsterkahle/onionpress/releases)
2. Open the DMG and drag `OnionPress.app` to your Applications folder
3. Launch OnionPress from Applications
4. On first launch:
   - The app will generate your onion address (starting with "op2") - takes < 1 second
   - The app will initialize its bundled container runtime (Colima) - takes ~2-3 minutes
   - It will download WordPress, MariaDB, and Tor container images (~1GB)
   - Total 1-time setup: 3-5 minutes depending on your internet connection
   - Launching the site is about a minute.

### macOS Security Warning

Since this app is not code-signed with an Apple Developer certificate, macOS on first launch. This is normal for open-source software.

**Method 1 - System Settings (Recommended):**
1. Open the app when in your Applications folder - you'll see a security warning.  Hit Done.
2. Open **System Settings** → **Privacy & Security**
3. Scroll down and click **"Open Anyway"** next to the OnionPress warning
4. Click **"Open Anyway"** in the confirmation dialog, and enter your computer's password

**Method 2 - Right-Click:**
1. Right-click (or Control-click) on the OnionPress app in you Application folder
2. Select **"Open"**
3. Click **"Open"** in the dialog

**Method 3 - Terminal (Advanced):**

If you're comfortable with the terminal, you can remove the quarantine flag:
```bash
# After moving to Applications folder
xattr -cr /Applications/OnionPress.app
```
This removes macOS's quarantine attribute and allows the app to launch without warnings.

## Usage

### Menu Bar Controls

Once installed, onionpress appears in your menu bar with an onion icon (🧅):

- **Copy Onion Address**: Copy your .onion URL to clipboard
- **Open in Tor Browser**: Launch Tor Browser with your site (requires Tor Browser to be installed)
- **Start/Stop**: Control the WordPress service
- **View Logs**: Open logs in Console.app for troubleshooting
- **Settings**: Open configuration file for customization
- **Export Private Key**: Backup your onion address as 47 BIP39 mnemonic words (like Bitcoin seed phrases)
- **Import Private Key**: Restore your onion address from a mnemonic backup
- **Check for Updates**: Check for new app versions and update WordPress, MariaDB, and Tor container images

### Keeping Your Site Updated

**Manual Updates** (Recommended):
Click "Check for Updates..." in the menu to:
1. Check for new OnionPress app versions
2. Download updated WordPress, MariaDB, and Tor container images
3. Apply security patches and new features

**Automatic Updates** (Optional):
Enable automatic Docker image updates on launch by editing `~/.onionpress/config`:
```bash
UPDATE_ON_LAUNCH=yes
```

When enabled, onionpress will check for and download updated container images each time you launch the app. This ensures you have the latest security patches without manual intervention.

**Note**: After updating container images, restart the service from the menu bar to apply updates.

### Launch on Login

Have your WordPress site start automatically when you log in to macOS by editing `~/.onionpress/config`:
```bash
LAUNCH_ON_LOGIN=yes
```

When enabled:
- OnionPress automatically launches when you log in
- Your WordPress site starts automatically in the background
- The menu bar app appears and shows your status

The app automatically syncs this setting with macOS login items. You can also manage this in **System Settings → General → Login Items**.

**Default**: Disabled (manual launch required)

### Accessing Your Site

1. Your onion address is displayed in the menu bar dropdown (starts with "op2" for easy identification)
2. Install [Tor Browser](https://www.torproject.org/download/) to access .onion sites
3. Copy your onion address and paste it into Tor Browser
4. Complete the WordPress setup wizard

**Vanity Address Configuration**: You can customize the prefix in `~/.onionpress/config` before first launch. See the config file for details on generation times for different prefix lengths.

### Private Key Backup & Restore

Your onion address is derived from a private key. You can back up and restore this key to:
- Migrate your onion address to a new machine
- Recover your address after reinstalling
- Keep a secure backup of your identity

**To backup your key:**
1. Click "Export Private Key" in the menu bar
2. You'll receive 47 BIP39 mnemonic words (like Bitcoin seed phrases)
3. Write these words down and store them securely
4. Anyone with these words can restore your exact onion address

**To restore a key:**
1. Click "Import Private Key" in the menu bar
2. Paste your 47 mnemonic words
3. Your onion address will be restored

⚠️ **Security Note**: Keep your mnemonic words private and secure. Anyone with these words can impersonate your onion address.

### Internet Archive Wayback Machine Link Fixer

OnionPress automatically installs and activates the [Internet Archive Wayback Machine Link Fixer plugin](https://wordpress.org/plugins/internet-archive-wayback-machine-link-fixer/), which helps combat link rot by:

- Automatically scanning your posts for outbound links
- Creating archived versions in the Wayback Machine
- Redirecting to archived versions when links break
- Archiving your own posts on every update

**The plugin is enabled by default.** To disable automatic installation, edit `~/.onionpress/config` before first launch:
```bash
INSTALL_IA_PLUGIN=no
```

For increased daily link processing, you can add your free Archive.org API credentials in the plugin settings after setup.

### Recommended WordPress Plugins for Tor Onion Services

These plugins are optimized for the Tor network's slower speeds and privacy-focused audience:

#### Performance & Optimization (Essential for Tor)

- **[WP Super Cache](https://wordpress.org/plugins/wp-super-cache/)** or **[W3 Total Cache](https://wordpress.org/plugins/w3-total-cache/)** - Critical for caching to improve response times over Tor's slower connections
- **[Autoptimize](https://wordpress.org/plugins/autoptimize/)** - Minifies and concatenates CSS/JavaScript to reduce HTTP requests and data transfer
- **[EWWW Image Optimizer](https://wordpress.org/plugins/ewww-image-optimizer/)** - Compresses images locally without cloud dependencies
- **[Lazy Load](https://wordpress.org/plugins/rocket-lazy-load/)** - Only loads images when scrolling, reducing initial page load time

#### Privacy & Self-Hosted Alternatives

- **[Simple Local Avatars](https://wordpress.org/plugins/simple-local-avatars/)** - Replaces Gravatar with local avatars (no external service calls)
- **[Koko Analytics](https://wordpress.org/plugins/koko-analytics/)** - Privacy-friendly, cookieless analytics (self-hosted, GDPR-compliant)
- **[Simple Location](https://wordpress.org/plugins/simple-location/)** - Uses OpenStreetMap instead of Google Maps
- **[ActivityPub](https://wordpress.org/plugins/activitypub/)** - Connect your WordPress site to the Fediverse for decentralized social networking

#### Security & Anti-Spam

- **[WP Cerber Security](https://wordpress.org/plugins/wp-cerber/)** or **[Wordfence Security](https://wordpress.org/plugins/wordfence/)** - Rate limiting and login protection
- **[CleanTalk](https://wordpress.org/plugins/cleantalk-spam-protect/)** - Effective spam protection that works well with Tor users
- **[Math Captcha](https://wordpress.org/plugins/wp-math-captcha/)** - Self-hosted CAPTCHA alternative (avoid Google reCAPTCHA which blocks many Tor users)
- **[Disable Comments](https://wordpress.org/plugins/disable-comments/)** - Reduces spam attack surface if comments aren't needed

#### Content Security

- **[HTTP Headers](https://wordpress.org/plugins/http-headers/)** - Add security headers and control referrer policies
- **[Content Security Policy Manager](https://wordpress.org/plugins/content-security-policy-manager/)** - Prevents loading of external resources for better security

**Installation tip**: Install these plugins through the WordPress admin interface after completing initial setup. Focus on performance plugins first to optimize for Tor's network characteristics.

### Local Testing

For testing purposes, your WordPress site is also available at:
- http://localhost:8080 (only accessible from your Mac)

## Architecture

onionpress uses:
- **WordPress**: Latest official WordPress container
- **MariaDB**: Latest MariaDB for the database
- **Tor**: Hidden service container that exposes WordPress as an onion service
- **mkp224o**: Vanity onion address generator (generates addresses with custom prefixes)
- **Colima**: Bundled container runtime using Apple's virtualization framework
- **Lima**: VM management layer (bundled)
- **Docker CLI**: Container management tools (bundled)

All data is stored in:
- `~/.onionpress/` - Application data, logs, config, and Colima VM
- Docker volumes for WordPress, database, and Tor keys

## Building from Source

To build the DMG installer:

```bash
cd onionpress
./build/build-dmg.sh
```

This will create `onionpress.dmg` in the `build/` directory.

## Troubleshooting

### "macOS version too old"
OnionPress requires macOS 13 (Ventura) or later for Apple's native virtualization framework.

### Containers won't start
Check the logs via the menu bar app or run:
```bash
tail -f ~/.onionpress/onionpress.log
tail -f ~/.onionpress/colima/colima.log
```

### Onion address not generating
Wait 30-60 seconds for Tor to generate your onion address. Check logs if it takes longer.

## Security Notes

- Change the default WordPress admin password immediately after installation
- Your site is only accessible via Tor by default (port 8080 is localhost-only for testing)
- Keep WordPress and plugins updated regularly

## Uninstalling

1. Click Uninstall from the menu bar app 
2. Quit OnionPress
3. Move `OnionPress.app` to Trash
   or
1. Quit OnionPress
2. Move `OnionPress.app` to Trash
3. Remove data directory: `rm -rf ~/.onionpress`
4. Remove Docker volumes:
   ```bash
   docker volume rm onionpress-tor-keys onionpress-wordpress-data onionpress-db-data
   ```
5. Reboot

## License

AGPL 3 License - See LICENSE file for details

## Credits

A Decentralized Web project

Built with:
- [WordPress](https://wordpress.org/) - Open source content management system
- [Tor Project](https://www.torproject.org/) - Anonymous communication network
- [Colima](https://github.com/abiosoft/colima) - Container runtime for macOS
- [Lima](https://github.com/lima-vm/lima) - Linux virtual machines for macOS
- [mkp224o](https://github.com/cathugger/mkp224o) - Vanity onion address generator
- [rumps](https://github.com/jaredks/rumps) - Python library for macOS menu bar apps

## Support

For issues, questions, or contributions, please visit the GitHub repository.

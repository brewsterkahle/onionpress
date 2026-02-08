# OnionPress Hit Counter Plugin

Retro-style animated hit counter with persistent storage that survives reboots and upgrades.

## Features

- 🎯 **Persistent Storage**: Counter data stored in `/var/lib/onionpress/` - survives reboots, restarts, and upgrades
- 🎨 **Animated Odometer**: Classic mechanical odometer animation when count changes
- 🎪 **Multiple Styles**: Choose from odometer, digital, or classic styles
- 📊 **Auto-Increment**: Automatically increments on each page view
- 💾 **Exportable**: Counter data included in site backups

## Installation

### Via Docker (Recommended)

The plugin is pre-installed with OnionPress. Simply activate it in WordPress:

1. Go to **Plugins → Installed Plugins**
2. Find "OnionPress Hit Counter"
3. Click **Activate**

### Manual Installation

1. Copy the `onionpress-hit-counter` folder to `/var/www/html/wp-content/plugins/`
2. Ensure `/var/lib/onionpress/` directory exists and is writable by www-data
3. Activate the plugin in WordPress admin

## Usage

### Basic Shortcode

Add this shortcode to any page or post:

```
[hit_counter]
```

### With Options

```
[hit_counter style="odometer" digits="6" auto_increment="true"]
```

**Parameters:**

- `style` - Display style: `odometer` (default), `digital`, or `classic`
- `digits` - Number of digits to display (default: 6)
- `auto_increment` - Auto-increment on page load: `true` (default) or `false`

### Examples

**Classic odometer (green glow):**
```
[hit_counter style="odometer"]
```

**Digital red display:**
```
[hit_counter style="digital"]
```

**Simple classic counter:**
```
[hit_counter style="classic"]
```

**Custom digit count:**
```
[hit_counter digits="8"]
```

## Adding to Product Website

To add the hit counter to the OnionPress product website homepage:

1. **Option A: WordPress Page**
   - Create a WordPress page
   - Add the shortcode: `[hit_counter]`
   - Set as front page in Settings → Reading

2. **Option B: Theme Template**
   - Edit your theme's template file
   - Add: `<?php echo do_shortcode('[hit_counter]'); ?>`

3. **Option C: Widget**
   - Go to Appearance → Widgets
   - Add a "Shortcode" widget
   - Insert: `[hit_counter]`

## Persistent Storage

The counter value is stored in:
```
/var/lib/onionpress/hit-counter.txt
```

This location is mounted as a Docker volume (`onionpress-persistent-data`) ensuring the count persists across:
- Container restarts
- Computer reboots
- Software upgrades
- WordPress reinstalls

## Export/Import

The counter data is automatically included when exporting your site using the OnionPress export script:

```bash
cd product-website/scripts
./export-site.sh
```

The counter will be restored when importing to a new instance:

```bash
./import-site.sh
```

## Technical Details

### File Structure

```
onionpress-hit-counter/
├── onionpress-hit-counter.php  # Main plugin file
├── assets/
│   ├── hit-counter.js          # Odometer animation
│   └── hit-counter.css         # Retro styling
└── README.md                   # This file
```

### How It Works

1. **On Page Load**: JavaScript calls WordPress AJAX endpoint
2. **Server Side**: PHP increments counter in flat file
3. **Response**: Returns new count to JavaScript
4. **Animation**: Odometer animates to new value
5. **One Per Load**: Only increments once per page load (prevents refresh spam)

### Storage Location

The plugin stores data in `/var/lib/onionpress/` instead of the WordPress database because:
- WordPress database can be reset during upgrades
- Flat file storage is simple and reliable
- Same persistence level as Tor keys
- Easy to backup and restore

## Troubleshooting

### Counter doesn't increment

1. Check if `/var/lib/onionpress/` exists and is writable:
   ```bash
   docker exec onionpress-wordpress ls -la /var/lib/onionpress/
   ```

2. Check file permissions:
   ```bash
   docker exec onionpress-wordpress ls -la /var/lib/onionpress/hit-counter.txt
   ```

3. Ensure www-data owns the directory:
   ```bash
   docker exec onionpress-wordpress chown -R www-data:www-data /var/lib/onionpress/
   ```

### Counter resets after reboot

Ensure the Docker volume is properly configured in `docker-compose.yml`:

```yaml
volumes:
  onionpress-data:
    name: onionpress-persistent-data
```

And mounted in the wordpress service:

```yaml
volumes:
  - onionpress-data:/var/lib/onionpress
```

### Animation doesn't work

1. Check browser console for JavaScript errors
2. Ensure jQuery is loaded
3. Clear browser cache

## Future Enhancements

- [ ] Mirror site aggregation (sync counts from backup sites)
- [ ] More odometer styles (vintage, neon, LCD)
- [ ] Unique visitor tracking (vs total hits)
- [ ] Configurable increment rules
- [ ] Admin dashboard with statistics

## License

AGPL-3.0 - Same as OnionPress

## Credits

Built for OnionPress - bringing back the retro web! 💜

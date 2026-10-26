# OnionPress Hit Counter Plugin

Retro-style animated hit counter with persistent storage that survives reboots and upgrades.

## Features

- 🎯 **Persistent Storage**: the count is a WordPress option, so it is in every backup and survives reboots, restarts and upgrades
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
2. Activate the plugin in WordPress admin

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

The count is the `onionpress_hit_counter` option in the main site's
`wp_options` table (not autoloaded), so it persists across:
- Container restarts
- Computer reboots
- Software upgrades

and is part of every OnionPress backup, like the rest of the database.

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

1. **On Page Load**: JavaScript posts to the WordPress AJAX endpoint
   (`increment_counter`) with the nonce the page was given
2. **Server Side**: PHP checks the nonce and increments the option; a
   request without a valid nonce (a page cached past the nonce's life)
   gets the current count and counts nothing
3. **Response**: Returns the count to JavaScript
4. **Animation**: Odometer animates to the new value
5. **One Per Load**: Only increments once per page load (prevents refresh spam)

There is no other writer: the `standalone-counter.php` endpoint that
bypassed WordPress (SHORTINIT, a raw UPDATE of `wp_options` for anyone)
was removed in 1.2.0 (issue #398).

## Troubleshooting

### Counter doesn't increment

1. Check the browser console: the increment is a POST to
   `admin-ajax.php?action=increment_counter`
2. A page served from the page cache for longer than a day carries an
   expired nonce; the counter then shows but does not move until the
   cached page is refreshed
3. Read the stored count:
   ```bash
   docker exec -u www-data onionpress-wordpress wp option get onionpress_hit_counter
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

AGPL-3.0-or-later - Same as OnionPress

## Credits

Built for OnionPress - bringing back the retro web! 💜

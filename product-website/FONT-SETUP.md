# Self-Hosted Font Setup for OnionPress Product Website

This guide explains how to download and install retro fonts locally in WordPress for privacy.

## Why Self-Host Fonts?

**Privacy**: Loading fonts from Google Fonts or other CDNs leaks visitor data (IP addresses, pages visited, etc.)

**Tor Compatibility**: External font requests may be blocked or slow on Tor

**Self-Contained**: Everything served from your .onion address

## Fonts Used

1. **Press Start 2P** - Pixel/arcade font for headings and buttons
2. **VT323** - Retro terminal font for code blocks and counters

Both are open source and free to use!

## Step-by-Step Installation

### Option 1: Download Fonts from Google Fonts

1. **Download Press Start 2P**:
   - Visit: https://fonts.google.com/specimen/Press+Start+2P
   - Click "Download family"
   - Extract the ZIP file
   - Find `PressStart2P-Regular.ttf`

2. **Download VT323**:
   - Visit: https://fonts.google.com/specimen/VT323
   - Click "Download family"
   - Extract the ZIP file
   - Find `VT323-Regular.ttf`

### Option 2: Download from GitHub

Both fonts are available on GitHub:

```bash
# Press Start 2P
curl -L https://github.com/google/fonts/raw/main/ofl/pressstart2p/PressStart2P-Regular.ttf -o PressStart2P-Regular.ttf

# VT323
curl -L https://github.com/google/fonts/raw/main/ofl/vt323/VT323-Regular.ttf -o VT323-Regular.ttf
```

### Step 3: Convert to Web Formats

For best browser compatibility, convert TTF to WOFF2 and WOFF formats.

**Online Converters** (easiest):
- https://cloudconvert.com/ttf-to-woff2
- https://cloudconvert.com/ttf-to-woff
- https://transfonter.org/ (converts to multiple formats at once)

**Or use command-line tools**:

```bash
# Install woff2 tools (macOS with Homebrew)
brew install woff2

# Convert TTF to WOFF2
woff2_compress PressStart2P-Regular.ttf
woff2_compress VT323-Regular.ttf

# For WOFF, use an online converter or fontforge
```

You should have these files:
- `PressStart2P-Regular.woff2`
- `PressStart2P-Regular.woff`
- `VT323-Regular.woff2`
- `VT323-Regular.woff`

### Step 4: Upload to WordPress

1. **Create fonts directory via Docker**:
   ```bash
   export DOCKER_HOST="unix://$HOME/.onionpress/colima/default/docker.sock"
   docker exec onionpress-wordpress mkdir -p /var/www/html/wp-content/uploads/fonts
   ```

2. **Copy font files to WordPress**:
   ```bash
   # Assuming your font files are in ~/Downloads/fonts/
   docker cp ~/Downloads/fonts/PressStart2P-Regular.woff2 onionpress-wordpress:/var/www/html/wp-content/uploads/fonts/
   docker cp ~/Downloads/fonts/PressStart2P-Regular.woff onionpress-wordpress:/var/www/html/wp-content/uploads/fonts/
   docker cp ~/Downloads/fonts/VT323-Regular.woff2 onionpress-wordpress:/var/www/html/wp-content/uploads/fonts/
   docker cp ~/Downloads/fonts/VT323-Regular.woff onionpress-wordpress:/var/www/html/wp-content/uploads/fonts/
   ```

3. **Fix permissions**:
   ```bash
   docker exec onionpress-wordpress chown -R www-data:www-data /var/www/html/wp-content/uploads/fonts/
   ```

4. **Verify files are uploaded**:
   ```bash
   docker exec onionpress-wordpress ls -la /var/www/html/wp-content/uploads/fonts/
   ```

   You should see:
   ```
   PressStart2P-Regular.woff
   PressStart2P-Regular.woff2
   VT323-Regular.woff
   VT323-Regular.woff2
   ```

### Step 5: Test the Fonts

1. Go to your site: http://localhost:8080
2. Open browser DevTools (F12)
3. Go to Network tab
4. Reload the page
5. Look for font file requests - they should load from `/wp-content/uploads/fonts/`
6. Check Console for any font-related errors

### Step 6: Export to Git

Once fonts are working:

```bash
cd product-website/scripts
./export-site.sh
```

This will include the fonts in `content/uploads/fonts/` and they'll be in git!

## Troubleshooting

### Fonts not loading?

**Check the file paths in CSS**:
The CSS expects fonts at: `/wp-content/uploads/fonts/`

**Check browser DevTools**:
- Network tab shows 404 for fonts? Path is wrong
- CORS errors? Shouldn't happen on same domain

**Check file permissions**:
```bash
docker exec onionpress-wordpress ls -la /var/www/html/wp-content/uploads/fonts/
```
Files should be owned by `www-data:www-data`

### Fonts look wrong?

**Clear browser cache**: Hard refresh with Cmd+Shift+R

**Check font-display**: The CSS uses `font-display: swap` which shows fallback fonts until custom fonts load

### File size concerns?

WOFF2 files are small:
- Press Start 2P: ~17 KB
- VT323: ~14 KB

Total: ~31 KB for both fonts (much smaller than most images!)

## Alternative: Use WordPress Media Library

You can also upload fonts via WordPress admin:

1. Go to Media → Add New
2. Upload the WOFF2 and WOFF files
3. Note the URLs WordPress assigns
4. Update the `@font-face` declarations in CSS with those URLs

This is easier but uses random WordPress-generated URLs.

## Font Licenses

Both fonts are open source:

- **Press Start 2P**: SIL Open Font License 1.1
- **VT323**: SIL Open Font License 1.1

You can use them freely, including for commercial projects.

License files are included when you download from Google Fonts.

## When Importing to New Instance

The `import-site.sh` script automatically copies fonts from:
- `product-website/content/uploads/fonts/` → WordPress container

So once you export with fonts included, they'll work on any new instance!

## Additional Retro Fonts (Optional)

Want more retro fonts? Try these (all open source):

- **Pixel Operator** - Clean pixel font
- **04b_03** - Tiny pixel font
- **Commodore 64 Pixelized** - Classic C64 style
- **IBM VGA** - DOS/VGA terminal font
- **Fixedsys Excelsior** - Windows 3.1 system font

Download from:
- https://www.dafont.com (filter by "bitmap" or "pixel")
- https://int10h.org/oldschool-pc-fonts/ (authentic DOS/VGA fonts)

## Summary

✅ Download fonts (TTF)
✅ Convert to WOFF2/WOFF
✅ Upload to `/wp-content/uploads/fonts/`
✅ CSS already configured to use them
✅ Export to git
✅ Privacy maintained!

Your retro website now has authentic pixel fonts, all self-hosted! 🎮✨

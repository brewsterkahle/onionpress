# Quick Start: Product Website Management

## First Time: Building the Product Website

If you want to build the retro product website from scratch:

1. **Install fonts** (see `FONT-SETUP.md`):
   - Download Press Start 2P and VT323
   - Upload to WordPress at `/wp-content/uploads/fonts/`

2. **Follow the build guide** (see `SETUP-GUIDE.md`):
   - Add custom CSS
   - Create pages with provided content
   - Set up navigation

3. **Export to git**:
   ```bash
   cd product-website/scripts
   ./export-site.sh
   git add product-website/
   git commit -m "Initial product website build"
   git push
   ```

## Deploy to a New Instance

If you want to load the product website on a fresh OnionPress installation:

```bash
# 1. Clone the repo
git clone https://github.com/brewsterkahle/onionpress.git
cd onionpress

# 2. Start OnionPress (use the app or run the launcher)

# 3. Wait for it to fully start (check menubar icon is purple)

# 4. Import the website
cd product-website/scripts
./import-site.sh

# 5. Access your site
# Onion: http://[your-onion-address].onion
# Local: http://localhost:8080
```

## Regular Workflow

### Making Updates

1. Edit content through WordPress admin at http://localhost:8080/wp-admin
2. Preview changes
3. When satisfied, export:
   ```bash
   cd product-website/scripts
   ./export-site.sh
   ```
4. Commit changes:
   ```bash
   git add product-website/
   git commit -m "Update homepage hero section"
   git push
   ```

### Pulling Updates from Others

```bash
git pull
cd product-website/scripts
./import-site.sh
```

## What Gets Saved

✅ **All your content:**
- Posts and pages
- Media files (images, PDFs)
- Custom themes
- Plugins
- WordPress settings
- Users and roles

✅ **Portable:**
- Works on any OnionPress instance
- URLs automatically updated to new onion address

❌ **Not saved:**
- WordPress core (comes from Docker image)
- Tor keys (generated per instance)

## Need Help?

See the full [README.md](README.md) for detailed documentation.

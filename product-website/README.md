# OnionPress Product Website

This directory contains the official OnionPress product website - the WordPress site that showcases and documents the OnionPress software.

## Directory Structure

```
product-website/
├── content/
│   ├── themes/          # WordPress themes
│   ├── plugins/         # WordPress plugins
│   └── uploads/         # Media files (images, documents)
├── database/
│   └── product-website.sql  # Database export with URL placeholders
├── scripts/
│   ├── export-site.sh   # Export running site to git
│   └── import-site.sh   # Import from git to running instance
└── README.md            # This file
```

## Quick Start

### Export Current Website to Git

Save the current state of your WordPress site to this repository:

```bash
cd product-website/scripts
./export-site.sh
```

Then commit and push:

```bash
git add product-website/
git commit -m "Update product website"
git push
```

### Import Website to Fresh Instance

Load the product website from git into a running OnionPress instance:

```bash
cd product-website/scripts
./import-site.sh
```

**Note:** This will replace all existing WordPress content. The script will ask for confirmation.

## How It Works

### URL Placeholders

The database export uses placeholders for portability across different instances:

- `{{SITE_URL}}` - Replaced with the actual onion address
- `{{ONION_ADDRESS}}` - Replaced with the .onion hostname

This allows the same export to work on any OnionPress instance with different onion addresses.

### What Gets Exported

- **Themes** - All installed themes (including custom themes)
- **Plugins** - All installed plugins
- **Uploads** - All media files (images, PDFs, etc.)
- **Database** - Complete WordPress database (posts, pages, settings, users, etc.)

### What Doesn't Get Exported

- WordPress core files (these are in the Docker image)
- Docker containers or configuration
- Tor onion service keys (each instance generates its own)

## Workflow

### Regular Updates

1. Make changes to the product website through WordPress admin
2. Export: `./scripts/export-site.sh`
3. Review: `git diff`
4. Commit: `git commit -am "Update homepage copy"`
5. Push: `git push`

### Setting Up New Instance

1. Install and start OnionPress
2. Clone the repository
3. Import: `cd product-website/scripts && ./import-site.sh`
4. Access at your new onion address

### Collaboration

Multiple people can work on the product website:

1. Person A makes changes and exports
2. Person A commits and pushes to git
3. Person B pulls from git
4. Person B imports to their instance
5. Person B makes additional changes
6. Repeat

## Best Practices

### Before Exporting

- Test all pages and links work correctly
- Check that images display properly
- Verify plugins are activated
- Ensure content is production-ready

### Commit Messages

Use descriptive commit messages:

```bash
git commit -m "Add new feature comparison page"
git commit -m "Update screenshot images to v2.2.33"
git commit -m "Fix typo in installation instructions"
```

### Binary Files

Media files (images, PDFs) are stored in git. To keep the repository size manageable:

- Optimize images before uploading to WordPress
- Use appropriate formats (WebP, compressed JPGs)
- Avoid storing very large files

## Troubleshooting

### Export fails: "WordPress container is not running"

Start OnionPress before exporting:
1. Open the OnionPress application
2. Wait for it to start (purple icon in menubar)
3. Run export script again

### Import fails: "Database export not found"

Make sure you've pulled the latest from git:

```bash
git pull
```

### Import replaces all content

Yes, that's intentional. The import script does a complete database import, which replaces everything. Always export your current content first if you want to save it.

### URLs still show old onion address

Run these commands in the WordPress container:

```bash
docker exec onionpress-wordpress wp search-replace 'old-address.onion' 'new-address.onion' --allow-root
docker exec onionpress-wordpress wp cache flush --allow-root
```

## Technical Details

### Database Handling

The export script:
1. Uses `wp db export` to create a SQL dump
2. Replaces all URLs with placeholders using `sed`
3. Stores the portable SQL file in git

The import script:
1. Gets the current onion address from the Tor container
2. Replaces placeholders with actual URLs
3. Imports the modified SQL into the database
4. Fixes file permissions
5. Flushes caches

### File Permissions

After import, all wp-content files are owned by `www-data:www-data` (the web server user) to ensure WordPress can read and write files properly.

## Future Enhancements

- Automated backup script (export on schedule)
- Pre-commit hooks to validate exports
- Diff tool to preview changes before import
- Support for WordPress multisite

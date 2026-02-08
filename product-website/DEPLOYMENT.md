# Quick Deployment Guide

Deploy the OnionPress product website to a fresh installation.

## Prerequisites

1. OnionPress installed and running (purple icon in menubar)
2. Git repository cloned with latest product website files

## Deploy in 3 Commands

```bash
# 1. Clone or pull latest
git clone https://github.com/brewsterkahle/onionpress.git
cd onionpress

# 2. Make sure OnionPress is running (wait for purple icon)

# 3. Deploy the product website
cd product-website/scripts
./import-static-site.sh
```

That's it! Your product website is now live.

## What Gets Installed

✅ Retro-styled product homepage (static HTML)  
✅ OnionPress logo  
✅ Working visitor hit counter  
✅ All styling and features from preview  

## Access Your Site

- **Local**: http://localhost:8080
- **Tor**: http://[your-onion-address].onion (shown after import)

## WordPress Admin

WordPress is still available at:
- http://localhost:8080/wp-admin

The static homepage takes priority, but WordPress admin and all its features remain accessible.

## Reverting to WordPress

To restore WordPress as the homepage:

```bash
docker exec onionpress-wordpress mv /var/www/html/index.php.bak /var/www/html/index.php
```

## Troubleshooting

**"WordPress container is not running"**
- Make sure OnionPress app is running
- Wait for the purple icon in menubar (service is ready)

**"static-homepage.html not found"**
- Run `git pull` to get latest files
- Ensure you're in the onionpress repository directory

**Logo or counter not working**
- The script will warn if files are missing
- Check that all files exist in `product-website/content/`
- Re-run the import script

## Updating the Site

To update the product website design:

1. Make changes to `static-homepage.html`
2. Test locally
3. Commit and push to git
4. On target instance: `git pull && ./import-static-site.sh`

## Multiple Instances

You can deploy the same product website to multiple OnionPress instances. Each will:
- Have its own unique .onion address
- Have its own visitor counter (starts at 42)
- Display the same product website design

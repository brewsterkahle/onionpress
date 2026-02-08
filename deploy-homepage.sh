#!/bin/bash
# Deploy the product homepage and all assets to the running OnionPress instance
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SOURCE="$SCRIPT_DIR/product-website/static-homepage.html"
CONTAINER="onionpress-wordpress"

if [ ! -f "$SOURCE" ]; then
    echo "Error: $SOURCE not found"
    exit 1
fi

if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
    echo "Error: $CONTAINER container is not running"
    exit 1
fi

# Deploy the homepage
docker cp "$SOURCE" "$CONTAINER":/var/www/html/index.html

# Deploy static assets
docker cp "$SCRIPT_DIR/product-website/content/uploads/logo.webp" "$CONTAINER":/var/www/html/logo.webp
docker cp "$SCRIPT_DIR/docs/construction.gif" "$CONTAINER":/var/www/html/construction.gif
docker cp "$SCRIPT_DIR/assets/branding/icon-face-transparent.png" "$CONTAINER":/var/www/html/icon-face-transparent.png

# Deploy hit counter plugin
docker cp "$SCRIPT_DIR/wordpress-plugins/onionpress-hit-counter" "$CONTAINER":/var/www/html/wp-content/plugins/onionpress-hit-counter

# Fix ownership
docker exec "$CONTAINER" chown -R www-data:www-data \
    /var/www/html/index.html \
    /var/www/html/logo.webp \
    /var/www/html/construction.gif \
    /var/www/html/icon-face-transparent.png \
    /var/www/html/wp-content/plugins/onionpress-hit-counter

# Ensure index.html is served before index.php via .htaccess (survives container restarts)
docker exec "$CONTAINER" bash -c '
    if ! grep -q "BEGIN OnionPress" /var/www/html/.htaccess 2>/dev/null; then
        sed -i "/# BEGIN WordPress/i # BEGIN OnionPress\nDirectoryIndex index.html index.php\n# END OnionPress\n" /var/www/html/.htaccess
    fi
'

echo "Deployed homepage, assets, and hit counter plugin to live site"

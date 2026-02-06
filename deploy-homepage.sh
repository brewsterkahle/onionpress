#!/bin/bash
# Deploy the product homepage from git to the running Onion.Press instance
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SOURCE="$SCRIPT_DIR/product-website/static-homepage.html"

if [ ! -f "$SOURCE" ]; then
    echo "Error: $SOURCE not found"
    exit 1
fi

if ! docker inspect onionpress-wordpress >/dev/null 2>&1; then
    echo "Error: onionpress-wordpress container is not running"
    exit 1
fi

docker cp "$SOURCE" onionpress-wordpress:/var/www/html/index.html
docker exec onionpress-wordpress chown www-data:www-data /var/www/html/index.html
docker exec onionpress-wordpress chmod 644 /var/www/html/index.html
echo "Deployed static-homepage.html to live site"

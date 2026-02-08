#!/bin/bash
# Import static product website to a running OnionPress instance
# This deploys the retro product website as a static HTML page

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRODUCT_WEBSITE_DIR="$(dirname "$SCRIPT_DIR")"

# Docker environment
export DOCKER_HOST="unix://$HOME/.onionpress/colima/default/docker.sock"
export DOCKER_CONFIG="$HOME/.onionpress/docker-config"

echo "=== Importing OnionPress Product Website ==="
echo

# Check if WordPress container is running
if ! docker ps --format '{{.Names}}' | grep -q '^onionpress-wordpress$'; then
    echo "Error: WordPress container is not running"
    echo "Please start OnionPress first and wait for it to be ready (purple icon)"
    exit 1
fi

# Check if static homepage exists
if [ ! -f "$PRODUCT_WEBSITE_DIR/static-homepage.html" ]; then
    echo "Error: static-homepage.html not found"
    echo "Please ensure you've pulled the latest from git"
    exit 1
fi

echo "This will replace the default WordPress installation with the product website."
echo "The WordPress admin will still be accessible at /wp-admin"
echo
read -p "Continue? (y/N) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Cancelled"
    exit 0
fi

echo
echo "Step 1: Backing up WordPress index.php..."
docker exec onionpress-wordpress bash -c 'if [ -f /var/www/html/index.php ]; then mv /var/www/html/index.php /var/www/html/index.php.bak; fi' 2>/dev/null || true

echo "Step 2: Installing static homepage..."
docker cp "$PRODUCT_WEBSITE_DIR/static-homepage.html" onionpress-wordpress:/var/www/html/index.html
docker exec onionpress-wordpress chown www-data:www-data /var/www/html/index.html

echo "Step 3: Installing logo..."
if [ -f "$PRODUCT_WEBSITE_DIR/content/uploads/logo.webp" ]; then
    docker cp "$PRODUCT_WEBSITE_DIR/content/uploads/logo.webp" onionpress-wordpress:/var/www/html/logo.webp
    docker exec onionpress-wordpress chown www-data:www-data /var/www/html/logo.webp
else
    echo "  Warning: logo.webp not found, skipping"
fi

echo "Step 4: Installing hit counter plugin..."
if [ -d "$PRODUCT_WEBSITE_DIR/content/plugins/onionpress-hit-counter" ]; then
    docker cp "$PRODUCT_WEBSITE_DIR/content/plugins/onionpress-hit-counter" onionpress-wordpress:/var/www/html/wp-content/plugins/
    docker exec onionpress-wordpress chown -R www-data:www-data /var/www/html/wp-content/plugins/onionpress-hit-counter
else
    echo "  Warning: hit counter plugin not found, skipping"
fi

echo "Step 5: Checking hit counter..."
# Check if counter already exists in container (preserve existing counter on re-deploy)
if docker exec onionpress-wordpress test -f /var/lib/onionpress/hit-counter.txt 2>/dev/null; then
    CURRENT_COUNT=$(docker exec onionpress-wordpress cat /var/lib/onionpress/hit-counter.txt 2>/dev/null || echo "0")
    echo "  Preserving existing counter: $CURRENT_COUNT"
else
    # Only initialize counter on first deploy
    if [ -f "$PRODUCT_WEBSITE_DIR/content/onionpress-data/hit-counter.txt" ]; then
        echo "  Initializing from saved counter..."
        docker cp "$PRODUCT_WEBSITE_DIR/content/onionpress-data/hit-counter.txt" onionpress-wordpress:/var/lib/onionpress/hit-counter.txt
        docker exec onionpress-wordpress chown www-data:www-data /var/lib/onionpress/hit-counter.txt
        docker exec onionpress-wordpress chmod 644 /var/lib/onionpress/hit-counter.txt
    else
        echo "  Initializing with default count of 42..."
        docker exec onionpress-wordpress sh -c 'echo "42" > /var/lib/onionpress/hit-counter.txt && chown www-data:www-data /var/lib/onionpress/hit-counter.txt && chmod 644 /var/lib/onionpress/hit-counter.txt'
    fi
fi

echo
echo "=== Import Complete ==="
echo
echo "Your product website is now live!"
echo
echo "View it at:"
echo "  - Local: http://localhost:8080"

# Try to get onion address
ONION_ADDRESS=$(docker exec onionpress-tor cat /var/lib/tor/hidden_service/wordpress/hostname 2>/dev/null | tr -d '\n' || echo "")
if [ -n "$ONION_ADDRESS" ]; then
    echo "  - Onion: http://$ONION_ADDRESS"
fi

echo
echo "Note: WordPress admin is still accessible at:"
echo "  http://localhost:8080/wp-admin"
echo
echo "To restore WordPress as the main site:"
echo "  docker exec onionpress-wordpress mv /var/www/html/index.php.bak /var/www/html/index.php"
echo

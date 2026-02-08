#!/bin/bash
# Import OnionPress product website from git repository
# This loads the complete WordPress site into a running instance

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRODUCT_WEBSITE_DIR="$(dirname "$SCRIPT_DIR")"
CONTENT_DIR="$PRODUCT_WEBSITE_DIR/content"
DATABASE_DIR="$PRODUCT_WEBSITE_DIR/database"

# Docker environment
export DOCKER_HOST="unix://$HOME/.onionpress/colima/default/docker.sock"
export DOCKER_CONFIG="$HOME/.onionpress/docker-config"

echo "=== Importing OnionPress Product Website ==="
echo

# Check if WordPress is running
if ! docker ps --format '{{.Names}}' | grep -q '^onionpress-wordpress$'; then
    echo "Error: WordPress container is not running"
    echo "Please start OnionPress first"
    exit 1
fi

# Check if database export exists
if [ ! -f "$DATABASE_DIR/product-website.sql" ]; then
    echo "Error: Database export not found at $DATABASE_DIR/product-website.sql"
    exit 1
fi

# Get current onion address
echo "Getting current onion address..."
ONION_ADDRESS=$(docker exec onionpress-tor cat /var/lib/tor/hidden_service/hostname 2>/dev/null | tr -d '\n')
if [ -z "$ONION_ADDRESS" ]; then
    echo "Error: Could not retrieve onion address"
    exit 1
fi
echo "Onion address: $ONION_ADDRESS"

# Create temporary SQL file with current URLs
echo "Preparing database import..."
TEMP_SQL="/tmp/product-website-import-$$.sql"
cp "$DATABASE_DIR/product-website.sql" "$TEMP_SQL"

# Replace placeholders with actual URLs
sed -i.bak "s|{{SITE_URL}}|http://${ONION_ADDRESS}|g" "$TEMP_SQL"
sed -i.bak "s|{{ONION_ADDRESS}}|${ONION_ADDRESS}|g" "$TEMP_SQL"
rm "${TEMP_SQL}.bak"

# Import content directories
echo
echo "Importing themes..."
if [ -d "$CONTENT_DIR/themes" ] && [ "$(ls -A "$CONTENT_DIR/themes" 2>/dev/null)" ]; then
    docker cp "$CONTENT_DIR/themes/." onionpress-wordpress:/var/www/html/wp-content/themes/
    echo "  ✓ Themes imported"
else
    echo "  (no themes to import)"
fi

echo "Importing plugins..."
if [ -d "$CONTENT_DIR/plugins" ] && [ "$(ls -A "$CONTENT_DIR/plugins" 2>/dev/null)" ]; then
    docker cp "$CONTENT_DIR/plugins/." onionpress-wordpress:/var/www/html/wp-content/plugins/
    echo "  ✓ Plugins imported"
else
    echo "  (no plugins to import)"
fi

echo "Importing uploads (media files)..."
if [ -d "$CONTENT_DIR/uploads" ] && [ "$(ls -A "$CONTENT_DIR/uploads" 2>/dev/null)" ]; then
    docker cp "$CONTENT_DIR/uploads/." onionpress-wordpress:/var/www/html/wp-content/uploads/
    echo "  ✓ Uploads imported"
else
    echo "  (no uploads to import)"
fi

echo "Importing hit counter data..."
if [ -d "$CONTENT_DIR/onionpress-data" ]; then
    # Ensure directory exists in container
    docker exec onionpress-wordpress mkdir -p /var/lib/onionpress

    # Copy hit counter data if it exists
    if [ -f "$CONTENT_DIR/onionpress-data/hit-counter.txt" ]; then
        docker cp "$CONTENT_DIR/onionpress-data/hit-counter.txt" onionpress-wordpress:/var/lib/onionpress/hit-counter.txt
        docker exec onionpress-wordpress chown www-data:www-data /var/lib/onionpress/hit-counter.txt
        docker exec onionpress-wordpress chmod 644 /var/lib/onionpress/hit-counter.txt
        echo "  ✓ Hit counter imported"
    else
        echo "  (no hit counter data to import)"
    fi
else
    echo "  (no persistent data to import)"
fi

# Import database
echo
echo "Importing database..."
echo "  WARNING: This will replace all existing content!"
read -p "Continue? (yes/no): " confirm
if [ "$confirm" != "yes" ]; then
    echo "Import cancelled"
    rm "$TEMP_SQL"
    exit 0
fi

# Copy SQL file to container
docker cp "$TEMP_SQL" onionpress-wordpress:/tmp/import.sql

# Import the database
docker exec onionpress-wordpress wp db import /tmp/import.sql --allow-root

# Fix file permissions
echo "Fixing file permissions..."
docker exec onionpress-wordpress chown -R www-data:www-data /var/www/html/wp-content/

# Flush WordPress cache
echo "Flushing WordPress cache..."
docker exec onionpress-wordpress wp cache flush --allow-root 2>/dev/null || true

# Update permalink structure
echo "Updating permalink structure..."
docker exec onionpress-wordpress wp rewrite flush --allow-root 2>/dev/null || true

# Clean up
rm "$TEMP_SQL"
docker exec onionpress-wordpress rm /tmp/import.sql 2>/dev/null || true

echo
echo "=== Import Complete ==="
echo
echo "Product website is now live at:"
echo "  Onion: http://$ONION_ADDRESS"
echo "  Local: http://localhost:8080"
echo
echo "WordPress admin:"
echo "  URL: http://localhost:8080/wp-admin"
echo "  (use credentials from original site)"

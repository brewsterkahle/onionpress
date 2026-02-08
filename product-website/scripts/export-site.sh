#!/bin/bash
# Export OnionPress product website to git repository
# This captures the complete WordPress site for version control

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRODUCT_WEBSITE_DIR="$(dirname "$SCRIPT_DIR")"
CONTENT_DIR="$PRODUCT_WEBSITE_DIR/content"
DATABASE_DIR="$PRODUCT_WEBSITE_DIR/database"

# Docker environment
export DOCKER_HOST="unix://$HOME/.onionpress/colima/default/docker.sock"
export DOCKER_CONFIG="$HOME/.onionpress/docker-config"

echo "=== Exporting OnionPress Product Website ==="
echo

# Check if WordPress is running
if ! docker ps --format '{{.Names}}' | grep -q '^onionpress-wordpress$'; then
    echo "Error: WordPress container is not running"
    echo "Please start OnionPress first"
    exit 1
fi

# Clean existing content
echo "Cleaning previous export..."
rm -rf "$CONTENT_DIR/themes" "$CONTENT_DIR/plugins" "$CONTENT_DIR/uploads"
mkdir -p "$CONTENT_DIR/themes" "$CONTENT_DIR/plugins" "$CONTENT_DIR/uploads"

# Export wp-content directories
echo "Exporting themes..."
docker cp onionpress-wordpress:/var/www/html/wp-content/themes/. "$CONTENT_DIR/themes/" 2>/dev/null || echo "  (no custom themes)"

echo "Exporting plugins..."
docker cp onionpress-wordpress:/var/www/html/wp-content/plugins/. "$CONTENT_DIR/plugins/" 2>/dev/null || echo "  (no custom plugins)"

echo "Exporting uploads (media files)..."
docker cp onionpress-wordpress:/var/www/html/wp-content/uploads/. "$CONTENT_DIR/uploads/" 2>/dev/null || echo "  (no uploads)"

# Export hit counter data
echo "Exporting hit counter data..."
mkdir -p "$CONTENT_DIR/onionpress-data"
docker cp onionpress-wordpress:/var/lib/onionpress/hit-counter.txt "$CONTENT_DIR/onionpress-data/hit-counter.txt" 2>/dev/null || echo "  (no hit counter data yet)"

# Export database
echo "Exporting database..."
docker exec onionpress-wordpress wp db export /tmp/product-website.sql \
    --add-drop-table \
    --allow-root 2>/dev/null

docker cp onionpress-wordpress:/tmp/product-website.sql "$DATABASE_DIR/product-website.sql"

# Get the current onion address
ONION_ADDRESS=$(docker exec onionpress-tor cat /var/lib/tor/hidden_service/hostname 2>/dev/null | tr -d '\n' || echo "unknown")

# Replace URLs with placeholders for portability
echo "Replacing site URLs with placeholders..."
sed -i.bak "s|http://${ONION_ADDRESS}|{{SITE_URL}}|g" "$DATABASE_DIR/product-website.sql"
sed -i.bak "s|http://localhost:8080|{{SITE_URL}}|g" "$DATABASE_DIR/product-website.sql"
sed -i.bak "s|${ONION_ADDRESS}|{{ONION_ADDRESS}}|g" "$DATABASE_DIR/product-website.sql"
rm "$DATABASE_DIR/product-website.sql.bak"

# Create export metadata
cat > "$PRODUCT_WEBSITE_DIR/export-info.txt" <<EOF
Export Date: $(date)
WordPress Version: $(docker exec onionpress-wordpress wp core version --allow-root 2>/dev/null || echo "unknown")
Source Onion Address: $ONION_ADDRESS
Exported By: $(whoami)
EOF

echo
echo "=== Export Complete ==="
echo "Location: $PRODUCT_WEBSITE_DIR"
echo
echo "Files exported:"
echo "  - content/themes/     (WordPress themes)"
echo "  - content/plugins/    (WordPress plugins)"
echo "  - content/uploads/    (Media files)"
echo "  - content/onionpress-data/ (Hit counter and persistent data)"
echo "  - database/product-website.sql (Database with URL placeholders)"
echo
echo "Next steps:"
echo "1. Review changes: git status"
echo "2. Add files: git add product-website/"
echo "3. Commit: git commit -m 'Update product website'"
echo "4. Push: git push"

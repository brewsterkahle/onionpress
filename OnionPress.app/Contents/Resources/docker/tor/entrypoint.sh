#!/bin/sh
# Fix ownership on mounted volume (may be root-owned from previous goldy image)
chown -R tor:tor /var/lib/tor/hidden_service
chmod 700 /var/lib/tor/hidden_service

# Ensure healthcheck hidden service directory exists with correct permissions
mkdir -p /var/lib/tor/hidden_service/healthcheck
chown -R tor:tor /var/lib/tor/hidden_service/healthcheck
chmod 700 /var/lib/tor/hidden_service/healthcheck

# Start healthcheck HTTP server in background (port 8081)
/healthcheck-server.sh &

# Drop privileges and run tor
exec su-exec tor tor

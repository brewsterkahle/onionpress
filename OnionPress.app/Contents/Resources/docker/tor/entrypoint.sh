#!/bin/sh
# Fix ownership on mounted volume (may be root-owned from previous goldy image)
chown -R tor:tor /var/lib/tor/hidden_service
chmod 700 /var/lib/tor/hidden_service
# Drop privileges and run tor
exec su-exec tor tor

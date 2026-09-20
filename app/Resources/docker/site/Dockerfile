# OnionPress static-site backend — a minimal, tool-agnostic file server for
# "bring your own static site" installs (Hugo, Jekyll, Next.js export, plain
# HTML, ...). Unlike the wordpress image, this one doesn't care what
# produced the files it serves.
FROM nginx:alpine

# curl for the HEALTHCHECK below and for wait_for_site_ready()'s
# `docker exec` readiness probe (src/onionpress/containers.py).
RUN apk add --no-cache curl

COPY nginx.conf /etc/nginx/nginx.conf

HEALTHCHECK --interval=60s --timeout=15s --retries=15 --start-period=30s \
    CMD curl -sf --max-time 10 http://localhost/ || exit 1

EXPOSE 80

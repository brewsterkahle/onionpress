# Stress test worker/poll client — extends OnionPress tor image.
# Pre-installed: python3, curl, tor, netcat-openbsd, xxd, stress scripts.
# Base image already has python3 — no apt-get needed.

# Base image is a build ARG, not a literal: CI passes the manifest it just
# merged, and build/build-images.sh passes the tor image it just built
# locally, so `build-images.sh stress-worker` chains off your own work
# instead of silently extending whatever :latest is published.
ARG TOR_IMAGE=ghcr.io/brewsterkahle/onionpress-tor:latest
FROM ${TOR_IMAGE}

COPY worker-server.py /worker-server.py
COPY worker-bootstrap.py /worker-bootstrap.py
COPY verify-worker.py /verify-worker.py
COPY tor-watchdog.py /tor-watchdog.py
COPY onion_auth.py /onion_auth.py

ENTRYPOINT ["sh", "-c", "sleep infinity"]

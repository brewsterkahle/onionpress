# OnionPress tor image.
#
# Built on the Tor Project's own onion-service container image — the Onimages
# project (https://gitlab.torproject.org/tpo/onion-services/onimages/), published
# at containers.torproject.org and rebuilt daily. The runtime base is their
# C Tor image for Debian trixie: it carries Tor from deb.torproject.org,
# installed at THEIR build with the archive key's fingerprint verified, so this
# file no longer sets up that apt source or fetches and checks the key itself.
#
# C Tor is the only Tor implementation in this image. Arti was removed on
# 2026-09-24: it hosts a site acceptably (20/20 services reachable in a
# 2.6.0 test, after the usual "Too many preemptive onion service circuits
# failed" churn), but it has no control interface, and everything that makes
# OnionPress robust — sleep/wake DEL_ONION/ADD_ONION, the watchdog's stall
# recovery, the OnionHeaven takeover pipeline — is built on the control port.
# What remains of arti is its key file format: the launchers deliver the
# onion service key as an OpenSSH PEM (ks_hs_id.ed25519_expanded_private) in
# the onionpress-onion-keys volume, OnionHeaven exchanges keys in that
# format, and /key-convert.py turns it into C Tor's key files.
#
# Until Onimages 0.3.0 (2026-09-24) the official images were amd64-only,
# which on Apple Silicon meant QEMU emulation for the whole Tor stack — the
# reason this image used to build everything itself. 0.3.0 added arm64.
#
# Every external input below is pinned to an immutable identifier: images by
# multi-arch index digest, mkp224o by tag with its commit SHA asserted after
# clone. Before this, `FROM rust:latest` + `cargo install arti` (no
# --version) + `MKP224O_COMMIT=master` meant two builds of the same commit
# could ship different compilers, a different arti and a different mkp224o —
# and because CI builds amd64 and arm64 on separate runners at different
# times, the two halves of a single published manifest could disagree.
#
# Digests must be the multi-arch INDEX digest (what
# `docker buildx imagetools inspect <tag>` or build/base-image-digest.sh
# reports), not a per-platform manifest digest — the latter resolves on amd64
# and 404s on the arm64 runner. The Onimages tags themselves move every day;
# only the digest names one specific build. containers.torproject.org keeps
# untagged manifests (the Tor sysadmins' cron only collects unreferenced
# layers), so a pinned digest stays pullable after the tag moves on. See
# docs/BUILDING.md, "Bumping a pinned input".
#
# ARGs used in FROM lines must be declared before the first FROM (global
# scope) and re-declared inside any stage that needs them.

ARG TOR_IMAGE=containers.torproject.org/tpo/onion-services/onimages/tor:trixie@sha256:636dc5d7ea28675f8bd4d9a1fe561a371f94e54250dad73ecc4825194283df6a
ARG DOCKER_CLI_IMAGE=docker:29.8.1-cli@sha256:9f36dfce2d1fd053d700a4eca00c358df79bf7d8cb69d4a9e8d9981af18834ea

# Stage 1: Build mkp224o for vanity .onion address generation. Lives in this
# container instead of the host so a single Architecture: all .deb works on
# both amd64 and arm64 — Docker selects the right manifest variant at pull
# time.
#
# Built ON the tor base image rather than on a separate Debian pin, so the
# binary is compiled against exactly the glibc and libsodium the runtime stage
# has, and so the image has one Debian to keep in step instead of two.
#
# Cloned by tag, then the resolved commit is ASSERTED against the expected
# SHA. A tag is a movable pointer, so the tag alone is not a pin; fetching the
# SHA directly is not reliable either, because that needs the server to allow
# arbitrary SHA1-in-want. Clone-by-tag plus assert gives a readable reference
# and a real pin. Keep MKP224O_COMMIT in step with build/build-dmg-simple.sh's
# MKP224O_VERSION, which cross-compiles the same release for the macOS host.
FROM ${TOR_IMAGE} AS mkp224o-builder
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libc6-dev make autoconf automake libsodium-dev pkg-config \
    git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ARG MKP224O_VERSION=v1.7.0
ARG MKP224O_COMMIT=3e7aaf8a3a9cbbed56872afeef88c7a9d538d51a
RUN git clone --depth 1 --branch "${MKP224O_VERSION}" \
        https://github.com/cathugger/mkp224o.git /build/mkp224o \
    && cd /build/mkp224o \
    && actual="$(git rev-parse HEAD)" \
    && if [ "$actual" != "${MKP224O_COMMIT}" ]; then \
         echo "ERROR: mkp224o ${MKP224O_VERSION} resolved to $actual," >&2; \
         echo "       expected ${MKP224O_COMMIT}. The tag moved, or the" >&2; \
         echo "       remote is not what we think it is. Refusing to build." >&2; \
         exit 1; \
       fi \
    && ./autogen.sh \
    && ./configure \
    && make \
    && strip mkp224o

# The docker CLI, as a named stage so it is pinned in one place with the rest.
# It is effectively privileged: this container has /var/run/docker.sock mounted
# and uses this binary to create OnionHeaven takeover containers.
FROM ${DOCKER_CLI_IMAGE} AS docker-cli

# Stage 2: Runtime — the Tor Project's C Tor image for Debian trixie.
#
# What it gives us: Debian trixie (their tpo/tpa/base-images/debian, upgraded
# at their build), Tor from deb.torproject.org (0.4.9.x — Debian's own
# 0.4.8.16 has false-positive "compression bomb" warnings that break onion
# services after ~20h), the debian-tor user, and the deb.torproject.org apt
# source left configured with its verified keyring.
#
# Tor itself is deliberately NOT reinstalled or version-pinned here. The
# version is fixed by the base-image digest above; an `apt-get install tor`
# in this stage would silently upgrade it to whatever deb.torproject.org
# serves on build day, and a version pin becomes `E: Version '…' was not
# found` within weeks because that mirror drops superseded packages.
FROM ${TOR_IMAGE}

# The base switches to USER debian-tor for its own ENTRYPOINT (/usr/bin/tor).
# /entrypoint.sh has to start as root: it fixes ownership on the state
# volume, writes /etc/tor/torrc, and drops privileges itself with su to
# debian-tor. Without this line every chown in the entrypoint fails and Tor
# never starts.
#
# Numeric ids differ from the previous, debian:trixie-slim-based image
# (debian-tor was 100:101 there and is 101:102 here). Existing tor-state
# volumes carry the old ids. That is fine only because the entrypoint runs
# `chown -R` on /var/lib/tor at every start — keep that, and never chown a
# volume by number from the host side and expect it to stay right.
USER root

# The base sets WORKDIR /etc/tor. Every path in the scripts is absolute, but
# a relative path must not quietly resolve inside Tor's config directory.
WORKDIR /

# ca-certificates and wget are already in the base; they are listed anyway so
# this image's own requirements do not depend on upstream's incidental
# package choices. sqlite3 is the OnionHeaven registry's CLI.
RUN apt-get update && apt-get install -y --no-install-recommends \
    socat openssl ca-certificates sqlite3 python3 curl wget procps netcat-openbsd xxd \
    libsodium23 \
    && rm -rf /var/lib/apt/lists/*
# Docker CLI for heartbeat to manage takeover containers via docker exec/run
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=mkp224o-builder /build/mkp224o/mkp224o /usr/local/bin/mkp224o
# Record what the pinned base shipped, in the build log where a digest bump
# is reviewed.
RUN tor --version | head -n 1

# No CACHEBUST ARG: it sat above only the COPY block, whose cache Docker
# already keys on file content, and below the apt/cargo/git stages that
# actually drifted. It was never passed as --build-arg by anything, so it was
# a hand-edited string that invalidated the layers least in need of it. Now
# that the drifting stages are pinned, bump the pin instead.
COPY entrypoint.sh /entrypoint.sh
COPY healthcheck-server.sh /healthcheck-server.sh
COPY onionheaven-redirect.sh /onionheaven-redirect.sh
COPY onionheaven-tor-manager.sh /onionheaven-tor-manager.sh
COPY onion_auth.py /onion_auth.py
COPY onionheaven_common.py /onionheaven_common.py
COPY onionheaven-heartbeat.py /onionheaven-heartbeat.py
COPY web-server.py /web-server.py
COPY onionheaven-takeover-worker.py /onionheaven-takeover-worker.py
COPY onionheaven-queue-manager.py /onionheaven-queue-manager.py
COPY key-convert.py /key-convert.py
COPY torrc.template /etc/tor/torrc.template
COPY tor-watchdog.py /tor-watchdog.py
COPY onionnames.py /onionnames.py
COPY wordlists /wordlists
RUN chmod +x /entrypoint.sh /healthcheck-server.sh /onionheaven-redirect.sh /onionheaven-tor-manager.sh /onionheaven-heartbeat.py /web-server.py /onionheaven-takeover-worker.py /onionheaven-queue-manager.py /key-convert.py /tor-watchdog.py
ENTRYPOINT ["/entrypoint.sh"]
# A new ENTRYPOINT resets an inherited CMD, but say so: the base's CMD is a
# list of tor flags (--RunAsDaemon 0 --HiddenServiceDir …) that would
# otherwise arrive as /entrypoint.sh's arguments.
CMD []

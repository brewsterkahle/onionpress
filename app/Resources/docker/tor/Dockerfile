# OnionPress tor image.
#
# Every external input below is pinned to an immutable identifier: base images
# by multi-arch index digest, the arti crate by version, mkp224o by tag with
# its commit SHA asserted after clone. Before this, `FROM rust:latest` +
# `cargo install arti` (no --version) + `MKP224O_COMMIT=master` meant two
# builds of the same commit could ship different compilers, a different arti
# and a different mkp224o — and because CI builds amd64 and arm64 on separate
# runners at different times, the two halves of a single published manifest
# could disagree. "The v2.4.110 tor image" had no single Tor version.
#
# Digests must be the multi-arch INDEX digest (what
# `docker buildx imagetools inspect <tag>` reports), not a per-platform
# manifest digest — the latter resolves on amd64 and 404s on the arm64 runner.
# See docs/BUILDING.md, "Bumping a pinned input".
#
# ARGs used in FROM lines must be declared before the first FROM (global
# scope) and re-declared inside any stage that needs them.

ARG RUST_IMAGE=rust:1.98.1-trixie@sha256:462a9af3c54fb4718850d3c602fc0e54452c20b1c12a4e4080fdb001d4b9acbf
ARG DEBIAN_IMAGE=debian:trixie-slim@sha256:d7e12182ce18b85b93007c1dedf31f2d29e01ccf3182cc4017c709b6259bc132
ARG DOCKER_CLI_IMAGE=docker:29.8.1-cli@sha256:9f36dfce2d1fd053d700a4eca00c358df79bf7d8cb69d4a9e8d9981af18834ea

# Stage 1: Compile Arti from source — builds native arch (arm64 on Apple Silicon)
# The official Tor Project image is amd64-only, which forces qemu emulation on
# ARM Macs. Building from source gives us a native binary instead.
#
# NOT the -slim rust variant: arti's default features resolve
# default -> default-runtime -> native-tls -> openssl-sys, which needs
# pkg-config and libssl-dev. rust:slim-trixie ships only ca-certificates, gcc
# and libc6-dev, so -slim fails to compile. The full variant is FROM
# buildpack-deps:trixie and has them. Size is irrelevant here — this is a
# builder stage, discarded once /usr/local/cargo/bin/arti is copied out.
#
# ARTI_VERSION and RUST_IMAGE move together: arti requires a minimum Rust, so
# bumping one without the other fails ~20 minutes into a release-mode compile.
FROM ${RUST_IMAGE} AS builder
ARG ARTI_VERSION=2.6.0
RUN cargo install arti --locked --version "${ARTI_VERSION}" \
      --features onion-service-service,static-sqlite

# Stage 2: Build mkp224o for vanity .onion address generation. Lives in this
# container instead of the host so a single Architecture: all .deb works on
# both amd64 and arm64 — Docker selects the right manifest variant at pull
# time.
#
# Cloned by tag, then the resolved commit is ASSERTED against the expected
# SHA. A tag is a movable pointer, so the tag alone is not a pin; fetching the
# SHA directly is not reliable either, because that needs the server to allow
# arbitrary SHA1-in-want. Clone-by-tag plus assert gives a readable reference
# and a real pin. Keep MKP224O_COMMIT in step with build/build-dmg-simple.sh's
# MKP224O_VERSION, which cross-compiles the same release for the macOS host.
FROM ${DEBIAN_IMAGE} AS mkp224o-builder
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

# Stage 3: Runtime — Debian stable (trixie). Use Tor Project's own repo for
# latest stable Tor (0.4.9.x) instead of Debian's older 0.4.8.16 which has
# false-positive "compression bomb" warnings that break onion services after ~20h.
#
# The `tor` package is deliberately NOT version-pinned. deb.torproject.org
# removes superseded versions from the mirror, so a version pin becomes
# `E: Version '…' was not found` within weeks, and the repo publishes no
# snapshot service to pin against. The immutable identifier that actually
# protects users is the published image digest in build/image-pins.env; the
# base-image digest above is the right granularity for the Debian packages.
# What matters for supply chain is that the signing key is verified — see below.
FROM ${DEBIAN_IMAGE}

# TOR_APT_KEY_FPR is intentionally ENV, not ARG: an ARG can be overridden with
# --build-arg, which would let a builder point the fetch and the assertion at
# the same substituted key, making the check self-certifying and useless.
ENV TOR_APT_KEY_FPR=A3C4F0F979CAA22CDBA8F512EE8CBC9E886DDD89

# `curl -f` matters here. Without it curl exits 0 on an HTTP error and writes
# the error page to the output file; the build only failed today because
# `gpg --dearmor` happens to choke on HTML, which is luck, not a check.
#
# The key URL being *named* after a fingerprint is not verification either.
# gpg --show-keys re-derives the fingerprint from the fetched bytes and we
# compare it, so a substituted key at that URL fails the build.
#
# It compares EVERY primary key in the file, not just the first. `--dearmor`
# decodes the whole file into the keyring, and apt's `signed-by=` trusts any
# key in there — so checking only the first fingerprint would let an attacker
# APPEND a second key after the genuine one and have it trusted while the
# assertion still passed. The awk prints the fpr record that follows each
# `pub` (primary keys only, skipping subkey fprs), and the comparison demands
# that list be exactly the one expected fingerprint.
RUN apt-get update && apt-get install -y --no-install-recommends \
    socat openssl ca-certificates sqlite3 python3 curl wget procps netcat-openbsd xxd gpg \
    libsodium23 \
    && curl -fsSL "https://deb.torproject.org/torproject.org/${TOR_APT_KEY_FPR}.asc" \
       -o /tmp/tor-archive-keyring.asc \
    && gpg --show-keys --with-colons /tmp/tor-archive-keyring.asc \
       | awk -F: '$1=="pub"{p=1;next} $1=="fpr" && p {print $10; p=0}' \
       > /tmp/tor-key-fprs \
    && if [ "$(cat /tmp/tor-key-fprs)" != "${TOR_APT_KEY_FPR}" ]; then \
         echo "ERROR: Tor apt signing key does not match expectations." >&2; \
         echo "       expected exactly one primary key:" >&2; \
         echo "         ${TOR_APT_KEY_FPR}" >&2; \
         echo "       got:" >&2; \
         sed 's/^/         /' /tmp/tor-key-fprs >&2; \
         exit 1; \
       fi \
    && gpg --dearmor -o /usr/share/keyrings/tor-archive-keyring.gpg /tmp/tor-archive-keyring.asc \
    && rm -f /tmp/tor-archive-keyring.asc /tmp/tor-key-fprs \
    && echo 'deb [signed-by=/usr/share/keyrings/tor-archive-keyring.gpg] https://deb.torproject.org/torproject.org trixie main' \
       > /etc/apt/sources.list.d/tor.list \
    && apt-get update && apt-get install -y --no-install-recommends tor \
    && rm -rf /var/lib/apt/lists/*
# Docker CLI for heartbeat to manage takeover containers via docker exec/run
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
RUN useradd -r -m -s /bin/false arti
COPY --from=builder /usr/local/cargo/bin/arti /usr/local/bin/arti
COPY --from=mkp224o-builder /build/mkp224o/mkp224o /usr/local/bin/mkp224o

# No CACHEBUST ARG: it sat above only the COPY block, whose cache Docker
# already keys on file content, and below the apt/cargo/git stages that
# actually drifted. It was never passed as --build-arg by anything, so it was
# a hand-edited string that invalidated the layers least in need of it. Now
# that the drifting stages are pinned, bump the pin instead.
COPY arti.toml /etc/arti/arti.toml
COPY arti-polling.toml /etc/arti/arti-polling.toml
COPY arti-onionheaven.toml /etc/arti/arti-onionheaven.toml
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

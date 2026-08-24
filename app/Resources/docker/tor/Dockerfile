# Stage 1: Compile Arti from source — builds native arch (arm64 on Apple Silicon)
# The official Tor Project image is amd64-only, which forces qemu emulation on
# ARM Macs. Building from source gives us a native binary instead.
FROM rust:latest AS builder
RUN cargo install arti --locked --features onion-service-service,static-sqlite

# Stage 2: Build mkp224o for vanity .onion address generation. Lives in this
# container instead of the host so a single Architecture: all .deb works on
# both amd64 and arm64 — Docker selects the right manifest variant at pull
# time. TODO before release: pin MKP224O_COMMIT to a vetted commit hash.
FROM debian:trixie-slim AS mkp224o-builder
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libc6-dev make autoconf automake libsodium-dev pkg-config \
    git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ARG MKP224O_COMMIT=master
RUN git clone --depth 1 https://github.com/cathugger/mkp224o.git /build/mkp224o \
    && cd /build/mkp224o \
    && if [ "$MKP224O_COMMIT" != "master" ]; then \
         git fetch --depth 1 origin "$MKP224O_COMMIT" && git checkout "$MKP224O_COMMIT"; \
       fi \
    && ./autogen.sh \
    && ./configure \
    && make \
    && strip mkp224o

# Stage 3: Runtime — Debian stable (trixie). Use Tor Project's own repo for
# latest stable Tor (0.4.9.x) instead of Debian's older 0.4.8.16 which has
# false-positive "compression bomb" warnings that break onion services after ~20h.
FROM debian:trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    socat openssl ca-certificates sqlite3 python3 curl wget procps netcat-openbsd xxd gpg \
    libsodium23 \
    && curl -s https://deb.torproject.org/torproject.org/A3C4F0F979CAA22CDBA8F512EE8CBC9E886DDD89.asc \
       | gpg --dearmor -o /usr/share/keyrings/tor-archive-keyring.gpg \
    && echo 'deb [signed-by=/usr/share/keyrings/tor-archive-keyring.gpg] https://deb.torproject.org/torproject.org trixie main' \
       > /etc/apt/sources.list.d/tor.list \
    && apt-get update && apt-get install -y --no-install-recommends \
       tor obfs4proxy snowflake-client \
    && rm -rf /var/lib/apt/lists/*
# Docker CLI for heartbeat to manage takeover containers via docker exec/run
COPY --from=docker:cli /usr/local/bin/docker /usr/local/bin/docker
RUN useradd -r -m -s /bin/false arti
COPY --from=builder /usr/local/cargo/bin/arti /usr/local/bin/arti
COPY --from=mkp224o-builder /build/mkp224o/mkp224o /usr/local/bin/mkp224o

# Cache-bust: changing this value forces rebuild of all COPY layers below
ARG CACHEBUST=2026-04-13a
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

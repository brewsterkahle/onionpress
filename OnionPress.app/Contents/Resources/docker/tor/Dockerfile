FROM alpine:latest
RUN apk add --no-cache tor wget su-exec socat openssl
COPY torrc /etc/tor/torrc
COPY entrypoint.sh /entrypoint.sh
COPY healthcheck-server.sh /healthcheck-server.sh
COPY cellar-redirect.sh /cellar-redirect.sh
COPY cellar-tor-manager.sh /cellar-tor-manager.sh
RUN chmod +x /entrypoint.sh /healthcheck-server.sh /cellar-redirect.sh /cellar-tor-manager.sh
ENTRYPOINT ["/entrypoint.sh"]

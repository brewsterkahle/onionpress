FROM alpine:latest
RUN apk add --no-cache tor wget su-exec socat
COPY torrc /etc/tor/torrc
COPY entrypoint.sh /entrypoint.sh
COPY healthcheck-server.sh /healthcheck-server.sh
RUN chmod +x /entrypoint.sh /healthcheck-server.sh
ENTRYPOINT ["/entrypoint.sh"]

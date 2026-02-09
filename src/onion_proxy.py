#!/usr/bin/env python3
"""
OnionPress Local Proxy Server

A local HTTP proxy on localhost:9077 that fetches .onion content via
`docker exec onionpress-wordpress curl --socks5-hostname onionpress-tor:9050`.
The WordPress container has curl and can reach Tor's SOCKS proxy over the
Docker network. This allows any browser with the OnionPress extension to
browse .onion sites transparently.

URL scheme: http://localhost:9077/proxy/{onion-host}/{path}
Status endpoint: http://localhost:9077/status
"""

import os
import re
import subprocess
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import quote, unquote

PROXY_PORT = 9077
ONION_PATTERN = re.compile(r'^[a-z0-9.-]+\.onion$')
# Match .onion URLs in HTML for rewriting
ONION_URL_RE = re.compile(
    r'(https?://)((?:[a-z0-9-]+\.)*[a-z2-7]{56}\.onion)((?:/[^\s"\'<>]*)?)',
    re.IGNORECASE
)


class OnionProxyHandler(BaseHTTPRequestHandler):
    """Handles proxy requests to .onion addresses via docker exec."""

    # Suppress per-request log lines from BaseHTTPRequestHandler
    def log_message(self, format, *args):
        if self.server.log_func:
            self.server.log_func(f"Proxy: {format % args}")

    def do_GET(self):
        self._handle_request()

    def do_POST(self):
        self._handle_request()

    def do_HEAD(self):
        self._handle_request(head_only=True)

    def _handle_request(self, head_only=False):
        # Status endpoint
        if self.path == '/status':
            self._handle_status()
            return

        # Parse /proxy/{onion-host}/{path}
        if not self.path.startswith('/proxy/'):
            self.send_error(404, "Use /proxy/{onion-host}/{path} or /status")
            return

        parts = self.path[len('/proxy/'):].split('/', 1)
        onion_host = parts[0].lower()
        onion_path = '/' + parts[1] if len(parts) > 1 else '/'

        # Validate .onion host
        if not ONION_PATTERN.match(onion_host):
            self.send_error(400, "Only .onion addresses are allowed")
            return

        # Build the target URL
        target_url = f"http://{onion_host}{onion_path}"

        # Read POST body if present
        post_data = None
        if self.command == 'POST':
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length > 0:
                post_data = self.rfile.read(content_length)

        # Fetch via docker exec
        try:
            stdout, stderr, returncode = self._docker_curl(
                target_url, post_data=post_data, head_only=head_only
            )
        except Exception as e:
            self.send_error(502, f"Failed to fetch from Tor: {e}")
            return

        if returncode != 0:
            error_msg = stderr if stderr else f"curl exit code {returncode}"
            # curl exit code 22 = HTTP error (4xx/5xx) - still has output with -D -
            if returncode != 22 or not stdout:
                self.send_error(502, f"Tor fetch failed: {error_msg}")
                return

        # Parse response: wget --save-headers puts HTTP headers then \r\n\r\n then body
        self._send_proxied_response(stdout, onion_host, head_only)

    def _docker_curl(self, url, post_data=None, head_only=False):
        """Fetch a URL via curl in the WordPress container through Tor's SOCKS proxy.

        Uses: docker exec onionpress-wordpress curl --socks5-hostname onionpress-tor:9050
        This works because both containers share the Docker network, and curl
        in the WordPress container can reach Tor's SOCKS port directly.
        """
        docker_bin = self.server.docker_bin
        docker_env = self.server.docker_env

        cmd = [docker_bin, "exec"]

        # For POST with data, pipe through stdin
        if post_data is not None:
            cmd += ["-i"]

        cmd += [
            "onionpress-wordpress",
            "curl", "-sD", "-", "-L",
            "--socks5-hostname", "onionpress-tor:9050",
            "--max-time", "30",
        ]

        if head_only:
            cmd += ["-I"]

        if post_data is not None:
            content_type = self.headers.get('Content-Type', 'application/x-www-form-urlencoded')
            cmd += [
                "-H", f"Content-Type: {content_type}",
                "--data-binary", "@-",
            ]

        cmd.append(url)

        result = subprocess.run(
            cmd,
            capture_output=True,
            input=post_data if post_data is not None else None,
            timeout=60,
            env=docker_env
        )
        return result.stdout, result.stderr.decode('utf-8', errors='replace'), result.returncode

    def _send_proxied_response(self, raw_output, onion_host, head_only=False):
        """Parse curl -D - output and send to client.

        When curl follows redirects (-L -D -), the output contains headers
        from EVERY response in the chain. We need to find the LAST set of
        headers (the final response) and use those.
        """
        # With -L -D -, curl outputs: headers1\r\n\r\nheaders2\r\n\r\nbody
        # Each redirect adds another header block. Find the last HTTP/ line
        # that starts a new response, then parse from there.
        # Strategy: split on \r\n\r\n, find the last chunk that starts with HTTP/
        sep = b'\r\n\r\n'
        chunks = raw_output.split(sep)
        if len(chunks) < 2:
            # Try \n\n
            sep = b'\n\n'
            chunks = raw_output.split(sep)

        if len(chunks) < 2:
            # No headers found, send as raw body
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.end_headers()
            if not head_only:
                self.wfile.write(raw_output)
            return

        # Find the last chunk that looks like HTTP headers (starts with HTTP/)
        last_header_idx = 0
        for i, chunk in enumerate(chunks[:-1]):  # exclude last chunk (body)
            if chunk.lstrip().startswith(b'HTTP/'):
                last_header_idx = i

        header_bytes = chunks[last_header_idx]
        # Everything after the last header block is body
        body = sep.join(chunks[last_header_idx + 1:])

        # Parse status line and headers
        header_text = header_bytes.decode('utf-8', errors='replace')
        header_lines = header_text.split('\r\n') if '\r\n' in header_text else header_text.split('\n')

        status_code = 200
        if header_lines and header_lines[0].startswith('HTTP/'):
            try:
                status_code = int(header_lines[0].split()[1])
            except (IndexError, ValueError):
                pass
            header_lines = header_lines[1:]

        # Collect headers
        content_type = 'application/octet-stream'
        skip_headers = {'transfer-encoding', 'connection', 'keep-alive', 'content-length', 'location'}
        response_headers = []
        for line in header_lines:
            if ':' in line:
                name, value = line.split(':', 1)
                name_lower = name.strip().lower()
                if name_lower in skip_headers:
                    continue
                if name_lower == 'content-type':
                    content_type = value.strip()
                response_headers.append((name.strip(), value.strip()))

        # Rewrite URLs in HTML so resources load through the proxy
        is_html = 'text/html' in content_type
        if is_html and body:
            body = self._rewrite_onion_links(body, onion_host)

        # Send response
        self.send_response(status_code)
        for name, value in response_headers:
            self.send_header(name, value)
        self.send_header('Content-Length', str(len(body)))
        # Allow extension to read response
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()

        if not head_only:
            self.wfile.write(body)

    def _rewrite_onion_links(self, body_bytes, onion_host):
        """Rewrite URLs in HTML so resources load through the proxy.

        Two rewrites:
        1. Absolute .onion URLs  → proxy URLs
        2. Root-relative URLs (/path) → /proxy/{onion_host}/path
        """
        try:
            text = body_bytes.decode('utf-8', errors='replace')
        except Exception:
            return body_bytes

        proxy_prefix = f"/proxy/{onion_host}"

        # 1. Rewrite absolute .onion URLs
        def replace_onion_url(match):
            _scheme = match.group(1)
            host = match.group(2)
            path = match.group(3) or ''
            return f"{proxy_prefix.rsplit(onion_host, 1)[0]}{host}{path}" if host != onion_host else f"{proxy_prefix}{path}"

        def replace_abs_onion(match):
            _scheme = match.group(1)
            host = match.group(2)
            path = match.group(3) or ''
            return f"/proxy/{host}{path}"

        text = ONION_URL_RE.sub(replace_abs_onion, text)

        # 2. Rewrite root-relative URLs in src, href, action, srcset attributes
        # Match: src="/path", href="/path", action="/path"
        # But NOT src="//..." (protocol-relative) or src="http..." (absolute)
        ROOT_REL_RE = re.compile(
            r'((?:src|href|action|srcset)\s*=\s*["\'])(/(?!/)[^"\']*)',
            re.IGNORECASE
        )
        text = ROOT_REL_RE.sub(rf'\1{proxy_prefix}\2', text)

        # Also rewrite url(/path) in inline CSS
        CSS_URL_RE = re.compile(
            r'(url\(\s*["\']?)(/(?!/)[^"\')\s]+)',
            re.IGNORECASE
        )
        text = CSS_URL_RE.sub(rf'\1{proxy_prefix}\2', text)

        return text.encode('utf-8')

    def _handle_status(self):
        """Return proxy status as JSON."""
        info = {
            "running": True,
            "proxy_port": self.server.server_port,
            "onion_address": self.server.onion_address,
            "version": self.server.version,
        }
        body = json.dumps(info).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """HTTP server that handles each request in a new thread."""
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *args, **kwargs):
        self.onion_address = None
        self.version = "unknown"
        self.docker_bin = "docker"
        self.docker_env = None
        self.log_func = None
        super().__init__(*args, **kwargs)


def start_proxy(docker_bin, docker_env, onion_address=None, version="unknown",
                log_func=None, port=PROXY_PORT):
    """Start the proxy server in the current thread (blocking).

    Call this from a daemon thread. Returns the server instance.
    """
    server = ThreadingHTTPServer(("127.0.0.1", port), OnionProxyHandler)
    server.docker_bin = docker_bin
    server.docker_env = docker_env
    server.onion_address = onion_address
    server.version = version
    server.log_func = log_func

    if log_func:
        log_func(f"Onion proxy listening on http://127.0.0.1:{port}")
    server.serve_forever()
    return server


def stop_proxy(server):
    """Stop the proxy server."""
    if server:
        server.shutdown()

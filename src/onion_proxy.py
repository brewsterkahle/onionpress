#!/usr/bin/env python3
"""
OnionPress Local Proxy Server

A local HTTP proxy on localhost:9077 that fetches .onion content through
the OnionPress Tor container via `docker exec`. This allows any browser
with the OnionPress extension to browse .onion sites transparently.

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
ONION_PATTERN = re.compile(r'^[a-z2-7]{56}\.onion$')
# Match .onion URLs in HTML for rewriting
ONION_URL_RE = re.compile(
    r'(https?://)([a-z2-7]{56}\.onion)((?:/[^\s"\'<>]*)?)',
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
            stdout, stderr, returncode = self._docker_wget(
                target_url, post_data=post_data, head_only=head_only
            )
        except Exception as e:
            self.send_error(502, f"Failed to fetch from Tor: {e}")
            return

        if returncode != 0:
            error_msg = stderr if stderr else f"wget exit code {returncode}"
            # wget exit code 8 = server error (4xx/5xx) - still has output with --save-headers
            if returncode != 8 or not stdout:
                self.send_error(502, f"Tor fetch failed: {error_msg}")
                return

        # Parse response: wget --save-headers puts HTTP headers then \r\n\r\n then body
        self._send_proxied_response(stdout, onion_host, head_only)

    def _docker_wget(self, url, post_data=None, head_only=False):
        """Fetch a URL via docker exec into the tor container."""
        docker_bin = self.server.docker_bin
        docker_env = self.server.docker_env

        cmd = [docker_bin, "exec"]

        # For POST with data, pipe through stdin
        if post_data is not None:
            cmd += ["-i"]

        cmd += [
            "onionpress-tor",
            "wget", "--save-headers", "-q", "-O", "-",
            "--timeout=30",
            "--tries=1",
        ]

        if head_only:
            cmd += ["--spider"]

        if post_data is not None:
            content_type = self.headers.get('Content-Type', 'application/x-www-form-urlencoded')
            cmd += [
                f"--header=Content-Type: {content_type}",
                f"--post-data={post_data.decode('utf-8', errors='replace')}",
            ]

        cmd.append(url)

        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=60,
            env=docker_env
        )
        return result.stdout, result.stderr.decode('utf-8', errors='replace'), result.returncode

    def _send_proxied_response(self, raw_output, onion_host, head_only=False):
        """Parse wget --save-headers output and send to client."""
        # Split headers from body at first \r\n\r\n
        header_end = raw_output.find(b'\r\n\r\n')
        if header_end == -1:
            # Try \n\n as fallback
            header_end = raw_output.find(b'\n\n')
            if header_end == -1:
                # No headers found, send as raw body
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.end_headers()
                if not head_only:
                    self.wfile.write(raw_output)
                return
            sep_len = 2
        else:
            sep_len = 4

        header_bytes = raw_output[:header_end]
        body = raw_output[header_end + sep_len:]

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
        skip_headers = {'transfer-encoding', 'connection', 'keep-alive', 'content-length'}
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

        # Rewrite .onion URLs in HTML
        is_html = 'text/html' in content_type
        if is_html and body:
            body = self._rewrite_onion_links(body)

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

    def _rewrite_onion_links(self, body_bytes):
        """Rewrite .onion URLs in HTML to go through the proxy."""
        try:
            text = body_bytes.decode('utf-8', errors='replace')
        except Exception:
            return body_bytes

        proxy_base = f"http://localhost:{self.server.server_port}/proxy"

        def replace_onion_url(match):
            _scheme = match.group(1)
            host = match.group(2)
            path = match.group(3) or ''
            return f"{proxy_base}/{host}{path}"

        text = ONION_URL_RE.sub(replace_onion_url, text)
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

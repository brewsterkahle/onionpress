#!/usr/bin/env python3
"""
OnionPress Local Proxy Server

A local HTTP forward proxy on localhost:9077 that fetches .onion content
through a persistent PHP proxy running inside the WordPress container.

The browser extension configures the browser to use this as an HTTP proxy
for .onion domains. The browser sends standard proxy requests:
    GET http://xyz.onion/path HTTP/1.1
and the address bar keeps showing the real .onion URL.

Flow: Browser → http proxy localhost:9077
                → localhost:8080/__op_proxy.php (PHP inside WordPress)
                → socks5h://onionpress-tor:9050
                → .onion site

Also supports direct access: http://localhost:9077/proxy/{host}/{path}
Status endpoint: http://localhost:9077/status
"""

import os
import re
import subprocess
import json
import threading
import http.client
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse

PROXY_PORT = 9077
PHP_PROXY_PORT = 8080  # WordPress container's mapped port
PHP_PROXY_PATH = "/__op_proxy.php"
ONION_PATTERN = re.compile(r'^[a-z0-9.-]+\.onion$')
# Match https .onion URLs in HTML for downgrading to http
HTTPS_ONION_RE = re.compile(
    r'https://((?:[a-z0-9-]+\.)*[a-z0-9]{16,56}\.onion)',
    re.IGNORECASE
)


def install_php_proxy(docker_bin, docker_env, php_script_path, log_func=None):
    """Copy the PHP proxy script into the WordPress container."""
    try:
        result = subprocess.run(
            [docker_bin, "cp", php_script_path,
             "onionpress-wordpress:/var/www/html/__op_proxy.php"],
            capture_output=True, text=True, timeout=10, env=docker_env
        )
        if result.returncode == 0:
            if log_func:
                log_func("Installed PHP proxy in WordPress container")
            return True
        else:
            if log_func:
                log_func(f"Failed to install PHP proxy: {result.stderr}")
            return False
    except Exception as e:
        if log_func:
            log_func(f"Failed to install PHP proxy: {e}")
        return False


def check_php_proxy(log_func=None):
    """Verify the PHP proxy is responding."""
    try:
        conn = http.client.HTTPConnection("127.0.0.1", PHP_PROXY_PORT, timeout=5)
        conn.request("GET", PHP_PROXY_PATH,
                     headers={"X-OnionPress-Action": "status"})
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        if resp.status == 200:
            data = json.loads(body)
            if data.get("ok"):
                if log_func:
                    log_func("PHP proxy is responding")
                return True
        if log_func:
            log_func(f"PHP proxy check failed: HTTP {resp.status}")
        return False
    except Exception as e:
        if log_func:
            log_func(f"PHP proxy not reachable: {e}")
        return False


class OnionProxyHandler(BaseHTTPRequestHandler):
    """Handles proxy requests to .onion addresses via the PHP proxy."""

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

        # Determine if this is a standard HTTP proxy request or /proxy/ format
        is_forward_proxy = False
        if self.path.startswith('http://'):
            # Standard HTTP forward proxy: GET http://host.onion/path
            is_forward_proxy = True
            parsed = urlparse(self.path)
            onion_host = (parsed.hostname or '').lower()
            onion_path = parsed.path or '/'
            if parsed.query:
                onion_path += '?' + parsed.query
        elif self.path.startswith('/proxy/'):
            # Direct access format: /proxy/{onion-host}/{path}
            parts = self.path[len('/proxy/'):].split('/', 1)
            onion_host = parts[0].lower()
            onion_path = '/' + parts[1] if len(parts) > 1 else '/'
        else:
            self.send_error(404, "Use /proxy/{onion-host}/{path} or /status")
            return

        # Validate .onion host
        if not ONION_PATTERN.match(onion_host):
            self.send_error(400, "Only .onion addresses are allowed")
            return

        # Build the target URL
        target_url = f"http://{onion_host}{onion_path}"

        # Read POST body if present
        post_data = None
        content_type = None
        if self.command == 'POST':
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length > 0:
                post_data = self.rfile.read(content_length)
            content_type = self.headers.get('Content-Type', 'application/x-www-form-urlencoded')

        # Fetch via PHP proxy
        try:
            status, resp_headers, body = self._fetch_via_php(
                target_url, post_data=post_data, content_type=content_type,
                head_only=head_only
            )
        except Exception as e:
            self.send_error(502, f"Failed to fetch from Tor: {e}")
            return

        # Determine content type for link rewriting
        resp_content_type = resp_headers.get('content-type', 'application/octet-stream')

        # Rewrite URLs in HTML responses
        if 'text/html' in resp_content_type and body:
            if is_forward_proxy:
                # Forward proxy mode: just downgrade https→http for .onion URLs
                # (the browser's proxy config handles routing automatically)
                body = self._downgrade_https_onion(body)
            else:
                # /proxy/ format: full rewrite of .onion URLs and root-relative paths
                body = self._rewrite_onion_links(body, onion_host)

        # Send response
        self.send_response(status)
        # Forward selected headers
        forward_headers = {'content-type', 'cache-control', 'etag',
                           'last-modified', 'content-disposition'}
        for name, value in resp_headers.items():
            if name.lower() in forward_headers:
                self.send_header(name, value)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()

        if not head_only:
            self.wfile.write(body)

    def _fetch_via_php(self, url, post_data=None, content_type=None, head_only=False):
        """Fetch a URL through the PHP proxy in the WordPress container.

        Makes an HTTP request to localhost:8080/__op_proxy.php with the
        target URL in the X-OnionPress-URL header. No docker exec needed.
        """
        headers = {"X-OnionPress-URL": url}
        if content_type:
            headers["Content-Type"] = content_type

        method = "HEAD" if head_only else ("POST" if post_data else "GET")

        conn = http.client.HTTPConnection("127.0.0.1", PHP_PROXY_PORT, timeout=60)
        conn.request(method, PHP_PROXY_PATH, body=post_data, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        status = resp.status
        conn.close()

        return status, resp_headers, body

    def _downgrade_https_onion(self, body_bytes):
        """Downgrade https .onion URLs to http in HTML.

        In forward proxy mode, the browser handles routing automatically.
        We just need to ensure .onion links use http (not https) so the
        browser sends them as proxy requests rather than CONNECT tunnels.
        """
        try:
            text = body_bytes.decode('utf-8', errors='replace')
        except Exception:
            return body_bytes
        text = HTTPS_ONION_RE.sub(r'http://\1', text)
        return text.encode('utf-8')

    def _rewrite_onion_links(self, body_bytes, onion_host):
        """Rewrite URLs in HTML for /proxy/ format access.

        Two rewrites:
        1. Absolute .onion URLs  → /proxy/{host}/path
        2. Root-relative URLs (/path) → /proxy/{onion_host}/path
        """
        try:
            text = body_bytes.decode('utf-8', errors='replace')
        except Exception:
            return body_bytes

        proxy_prefix = f"/proxy/{onion_host}"

        # 1. Rewrite absolute .onion URLs (both http and https)
        ONION_URL_RE = re.compile(
            r'(https?://)((?:[a-z0-9-]+\.)*[a-z0-9]{16,56}\.onion)((?:/[^\s"\'<>]*)?)',
            re.IGNORECASE
        )

        def replace_abs_onion(match):
            host = match.group(2)
            path = match.group(3) or ''
            return f"/proxy/{host}{path}"

        text = ONION_URL_RE.sub(replace_abs_onion, text)

        # 2. Rewrite root-relative URLs in src, href, action, srcset attributes
        # But NOT already-rewritten /proxy/ URLs or protocol-relative //
        ROOT_REL_RE = re.compile(
            r'((?:src|href|action|srcset)\s*=\s*["\'])(/(?!proxy/|/)[^"\']*)',
            re.IGNORECASE
        )
        text = ROOT_REL_RE.sub(rf'\1{proxy_prefix}\2', text)

        # Also rewrite url(/path) in inline CSS
        CSS_URL_RE = re.compile(
            r'(url\(\s*["\']?)(/(?!proxy/|/)[^"\')\s]+)',
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


def stop_proxy(server):
    """Stop the proxy server."""
    if server:
        server.shutdown()

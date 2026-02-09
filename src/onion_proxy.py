#!/usr/bin/env python3
"""
OnionPress Local Proxy Server

A local HTTP forward proxy on localhost:9077 that routes:
  - .onion URLs through Tor (via PHP proxy in WordPress container)
  - Clearnet URLs directly over the internet

The browser extension routes ALL traffic through this proxy so that
.onion SPAs that reference clearnet resources work seamlessly.
HTTPS clearnet is handled via CONNECT tunneling.

Status endpoint: http://localhost:9077/status
"""

import os
import re
import socket
import select
import subprocess
import json
import threading
import time
import http.client
from collections import OrderedDict
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

# Cache settings
CACHE_MAX_BYTES = 100 * 1024 * 1024  # 100 MB
CACHE_MAX_ENTRIES = 5000


def _cache_ttl(content_type, cache_control):
    """Determine cache TTL in seconds based on response headers."""
    if cache_control:
        cc = cache_control.lower()
        if 'no-store' in cc or 'private' in cc:
            return 0
        if 'max-age=' in cc:
            try:
                age = int(cc.split('max-age=')[1].split(',')[0].strip())
                return min(age, 3600)
            except (ValueError, IndexError):
                pass

    ct = (content_type or '').lower()
    if any(t in ct for t in ['image/', 'font/', 'woff', 'application/javascript',
                              'text/javascript', 'text/css', 'application/wasm']):
        return 600
    if 'svg' in ct:
        return 600
    if 'text/html' in ct:
        return 30
    if 'json' in ct:
        return 60
    return 120


class ProxyCache:
    """Thread-safe in-memory LRU cache for proxy responses."""

    def __init__(self, max_bytes=CACHE_MAX_BYTES, max_entries=CACHE_MAX_ENTRIES):
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._cache = OrderedDict()
        self._size = 0
        self.hits = 0
        self.misses = 0

    def get(self, url):
        with self._lock:
            entry = self._cache.get(url)
            if entry is None:
                self.misses += 1
                return None
            if time.time() >= entry[3]:
                self._remove(url)
                self.misses += 1
                return None
            self._cache.move_to_end(url)
            self.hits += 1
            return entry[0], entry[1], entry[2]

    def put(self, url, status, headers, body, ttl):
        if ttl <= 0:
            return
        size = len(body)
        if size > self.max_bytes // 10:
            return
        with self._lock:
            if url in self._cache:
                self._remove(url)
            while (self._size + size > self.max_bytes or
                   len(self._cache) >= self.max_entries):
                if not self._cache:
                    break
                self._remove(next(iter(self._cache)))
            self._cache[url] = (status, headers, body, time.time() + ttl)
            self._size += size

    def _remove(self, url):
        entry = self._cache.pop(url, None)
        if entry:
            self._size -= len(entry[2])

    def stats(self):
        with self._lock:
            return {
                "entries": len(self._cache),
                "size_mb": round(self._size / (1024 * 1024), 1),
                "hits": self.hits,
                "misses": self.misses,
            }


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
    """HTTP forward proxy: .onion via Tor, clearnet direct."""

    def log_message(self, format, *args):
        if self.server.log_func:
            self.server.log_func(f"Proxy: {format % args}")

    def do_GET(self):
        self._handle_request()

    def do_POST(self):
        self._handle_request()

    def do_HEAD(self):
        self._handle_request(head_only=True)

    def do_CONNECT(self):
        """Handle HTTPS tunneling for clearnet URLs."""
        try:
            host, port_str = self.path.rsplit(':', 1)
            port = int(port_str)
        except ValueError:
            self.send_error(400, "Bad CONNECT target")
            return

        # .onion HTTPS should be downgraded to HTTP by the extension
        if host.endswith('.onion'):
            self.send_error(502, "Use http:// for .onion sites")
            return

        # Connect directly to clearnet target
        try:
            remote = socket.create_connection((host, port), timeout=10)
        except Exception as e:
            self.send_error(502, f"Cannot connect to {host}:{port}: {e}")
            return

        self.send_response(200, 'Connection established')
        self.end_headers()

        # Relay data between browser and remote server
        conns = [self.connection, remote]
        try:
            while True:
                readable, _, errored = select.select(conns, [], conns, 60)
                if errored:
                    break
                if not readable:
                    break  # timeout
                for sock in readable:
                    data = sock.recv(65536)
                    if not data:
                        remote.close()
                        return
                    if sock is self.connection:
                        remote.sendall(data)
                    else:
                        self.connection.sendall(data)
        except Exception:
            pass
        finally:
            remote.close()

    def _handle_request(self, head_only=False):
        # Status endpoint
        if self.path == '/status':
            self._handle_status()
            return

        # Determine if this is a standard proxy request or /proxy/ format
        is_forward_proxy = False
        if self.path.startswith('http://'):
            is_forward_proxy = True
            parsed = urlparse(self.path)
            target_host = (parsed.hostname or '').lower()
            target_path = parsed.path or '/'
            if parsed.query:
                target_path += '?' + parsed.query
            target_port = parsed.port
        elif self.path.startswith('/proxy/'):
            parts = self.path[len('/proxy/'):].split('/', 1)
            target_host = parts[0].lower()
            target_path = '/' + parts[1] if len(parts) > 1 else '/'
            target_port = None
        else:
            self.send_error(404, "Use /proxy/{host}/{path} or /status")
            return

        is_onion = target_host.endswith('.onion')

        # For /proxy/ format, only allow .onion
        if not is_forward_proxy and not is_onion:
            self.send_error(400, "Only .onion addresses allowed in /proxy/ format")
            return

        # Build the target URL
        if target_port and target_port != 80:
            target_url = f"http://{target_host}:{target_port}{target_path}"
        else:
            target_url = f"http://{target_host}{target_path}"

        # Read POST body if present
        post_data = None
        content_type = None
        if self.command == 'POST':
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length > 0:
                post_data = self.rfile.read(content_length)
            content_type = self.headers.get('Content-Type', 'application/x-www-form-urlencoded')

        # Check cache for GET requests
        cache = self.server.cache
        if self.command == 'GET' and cache:
            cached = cache.get(target_url)
            if cached:
                status, resp_headers, body = cached
                self._send_response(status, resp_headers, body, head_only)
                return

        # Fetch: .onion via Tor, clearnet directly
        try:
            if is_onion:
                status, resp_headers, body = self._fetch_via_php(
                    target_url, post_data=post_data, content_type=content_type,
                    head_only=head_only
                )
            else:
                status, resp_headers, body = self._fetch_direct(
                    target_url, target_host, target_port,
                    target_path, post_data=post_data,
                    content_type=content_type, head_only=head_only
                )
        except Exception as e:
            self.send_error(502, f"Fetch failed: {e}")
            return

        # Determine content type for link rewriting
        resp_content_type = resp_headers.get('content-type', 'application/octet-stream')

        # Rewrite URLs in HTML responses
        if is_onion and 'text/html' in resp_content_type and body:
            if is_forward_proxy:
                body = self._downgrade_https_onion(body)
            else:
                body = self._rewrite_onion_links(body, target_host)

        # Cache successful GET responses
        if self.command == 'GET' and cache and 200 <= status < 400:
            ttl = _cache_ttl(resp_content_type,
                             resp_headers.get('cache-control', ''))
            cache.put(target_url, status, resp_headers, body, ttl)

        self._send_response(status, resp_headers, body, head_only)

    def _send_response(self, status, resp_headers, body, head_only=False):
        """Send an HTTP response to the client."""
        self.send_response(status)
        forward_headers = {'content-type', 'cache-control', 'etag',
                           'last-modified', 'content-disposition',
                           'content-encoding', 'vary'}
        for name, value in resp_headers.items():
            if name.lower() in forward_headers:
                self.send_header(name, value)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _fetch_via_php(self, url, post_data=None, content_type=None, head_only=False):
        """Fetch a .onion URL through the PHP proxy (via Tor SOCKS)."""
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

    def _fetch_direct(self, url, host, port, path,
                      post_data=None, content_type=None, head_only=False):
        """Fetch a clearnet URL directly (no Tor)."""
        headers = {"Host": host}
        if content_type:
            headers["Content-Type"] = content_type
        # Forward Accept headers from the browser
        for hdr in ('Accept', 'Accept-Language', 'Accept-Encoding'):
            val = self.headers.get(hdr)
            if val:
                headers[hdr] = val

        method = "HEAD" if head_only else ("POST" if post_data else "GET")

        conn = http.client.HTTPConnection(host, port or 80, timeout=30)
        conn.request(method, path, body=post_data, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        status = resp.status
        conn.close()

        # Follow redirects (up to 5)
        redirects = 0
        while status in (301, 302, 303, 307, 308) and redirects < 5:
            location = resp_headers.get('location', '')
            if not location:
                break
            parsed = urlparse(location)
            if parsed.scheme == 'https':
                # Can't follow HTTPS redirect in direct fetch; return redirect to browser
                break
            rhost = parsed.hostname or host
            rport = parsed.port or 80
            rpath = parsed.path or '/'
            if parsed.query:
                rpath += '?' + parsed.query
            conn = http.client.HTTPConnection(rhost, rport, timeout=30)
            conn.request("GET", rpath, headers={"Host": rhost})
            resp = conn.getresponse()
            body = resp.read()
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            status = resp.status
            conn.close()
            redirects += 1

        return status, resp_headers, body

    def _downgrade_https_onion(self, body_bytes):
        """Downgrade https .onion URLs to http in HTML."""
        try:
            text = body_bytes.decode('utf-8', errors='replace')
        except Exception:
            return body_bytes
        text = HTTPS_ONION_RE.sub(r'http://\1', text)
        return text.encode('utf-8')

    def _rewrite_onion_links(self, body_bytes, onion_host):
        """Rewrite URLs in HTML for /proxy/ format access."""
        try:
            text = body_bytes.decode('utf-8', errors='replace')
        except Exception:
            return body_bytes

        proxy_prefix = f"/proxy/{onion_host}"

        ONION_URL_RE = re.compile(
            r'(https?://)((?:[a-z0-9-]+\.)*[a-z0-9]{16,56}\.onion)((?:/[^\s"\'<>]*)?)',
            re.IGNORECASE
        )

        def replace_abs_onion(match):
            host = match.group(2)
            path = match.group(3) or ''
            return f"/proxy/{host}{path}"

        text = ONION_URL_RE.sub(replace_abs_onion, text)

        ROOT_REL_RE = re.compile(
            r'((?:src|href|action|srcset)\s*=\s*["\'])(/(?!proxy/|/)[^"\']*)',
            re.IGNORECASE
        )
        text = ROOT_REL_RE.sub(rf'\1{proxy_prefix}\2', text)

        CSS_URL_RE = re.compile(
            r'(url\(\s*["\']?)(/(?!proxy/|/)[^"\')\s]+)',
            re.IGNORECASE
        )
        text = CSS_URL_RE.sub(rf'\1{proxy_prefix}\2', text)

        return text.encode('utf-8')

    def _handle_status(self):
        """Return proxy status as JSON."""
        cache_stats = self.server.cache.stats() if self.server.cache else {}
        info = {
            "running": True,
            "proxy_port": self.server.server_port,
            "onion_address": self.server.onion_address,
            "version": self.server.version,
            "cache": cache_stats,
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
        self.cache = ProxyCache()
        super().__init__(*args, **kwargs)


def stop_proxy(server):
    """Stop the proxy server."""
    if server:
        server.shutdown()

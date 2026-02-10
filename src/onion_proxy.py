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
import secrets
import socket
import select
import string
import subprocess
import json
import threading
import time
import http.client
from collections import OrderedDict
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs

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

# Setup page HTML (WordPress-style first-run configuration)
SETUP_PAGE_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OnionPress &rsaquo; Setup</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #f0f0f1; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Oxygen-Sans, Ubuntu, Cantarell, "Helvetica Neue", sans-serif; font-size: 14px; color: #3c434a; }
  .setup-container { max-width: 580px; margin: 40px auto; padding: 0 20px; }
  .logo { text-align: center; margin-bottom: 20px; }
  .logo h1 { font-size: 28px; color: #1d2327; }
  .logo h1 span { color: #7b4e9e; }
  .setup-box { background: #fff; border: 1px solid #c3c4c7; border-radius: 4px; padding: 26px 24px; box-shadow: 0 1px 3px rgba(0,0,0,.04); }
  .setup-box h2 { font-size: 1.3em; margin-bottom: 1em; color: #1d2327; }
  .form-table { width: 100%; }
  .form-table th { text-align: left; padding: 12px 0 6px; font-weight: 600; vertical-align: top; }
  .form-table td { padding: 4px 0 12px; }
  .form-table input[type="text"],
  .form-table input[type="email"],
  .form-table input[type="password"] { width: 100%; padding: 6px 10px; font-size: 14px; border: 1px solid #8c8f94; border-radius: 4px; background: #fff; line-height: 1.6; }
  .form-table input:focus { border-color: #7b4e9e; box-shadow: 0 0 0 1px #7b4e9e; outline: none; }
  .password-group { position: relative; }
  .password-group input { padding-right: 70px; }
  .toggle-password { position: absolute; right: 6px; top: 50%; transform: translateY(-50%); background: #f0f0f1; border: 1px solid #8c8f94; border-radius: 3px; padding: 2px 8px; font-size: 12px; cursor: pointer; color: #3c434a; }
  .toggle-password:hover { background: #e0e0e1; }
  .description { font-size: 13px; color: #646970; margin-top: 4px; }
  .strength-indicator { height: 4px; border-radius: 2px; margin-top: 6px; background: #ddd; }
  .strength-bar { height: 100%; border-radius: 2px; transition: width 0.3s; }
  .strength-strong { background: #00a32a; width: 100%; }
  .strength-medium { background: #dba617; width: 66%; }
  .strength-weak { background: #d63638; width: 33%; }
  .theme-choice { margin: 8px 0; }
  .theme-choice label { display: block; padding: 10px 12px; border: 2px solid #dcdcde; border-radius: 4px; margin-bottom: 8px; cursor: pointer; }
  .theme-choice label:hover { border-color: #7b4e9e; }
  .theme-choice input[type="radio"] { margin-right: 8px; }
  .theme-choice input:checked + span { font-weight: 600; }
  .theme-choice .theme-desc { font-size: 12px; color: #646970; margin-left: 22px; display: block; }
  .submit-row { margin-top: 20px; text-align: center; }
  .submit-btn { background: #7b4e9e; color: #fff; border: none; padding: 10px 36px; font-size: 15px; border-radius: 4px; cursor: pointer; font-weight: 600; }
  .submit-btn:hover { background: #6a3f8d; }
  .submit-btn:active { background: #5a3280; }
  p.note { text-align: center; margin-top: 16px; font-size: 13px; color: #646970; }
</style>
</head>
<body>
<div class="setup-container">
  <div class="logo">
    <h1><span>&#x1F9C5;</span> OnionPress</h1>
  </div>
  <div class="setup-box">
    <h2>Welcome to OnionPress!</h2>
    <p style="margin-bottom:16px;">Set up your WordPress admin account below. Your site will only be accessible through Tor after this step.</p>
    <form method="post" action="/setup" id="setup-form">
      <table class="form-table">
        <tr>
          <th><label for="blog_title">Site Title</label></th>
          <td><input type="text" name="blog_title" id="blog_title" value="My OnionPress Site" /></td>
        </tr>
        <tr>
          <th><label for="user_name">Username</label></th>
          <td>
            <input type="text" name="user_name" id="user_name" value="admin" autocomplete="username" />
            <p class="description">Usernames cannot be changed later.</p>
          </td>
        </tr>
        <tr>
          <th><label for="admin_password">Password</label></th>
          <td>
            <div class="password-group">
              <input type="password" name="admin_password" id="admin_password" value="{{GENERATED_PASSWORD}}" autocomplete="new-password" />
              <button type="button" class="toggle-password" onclick="togglePassword()">Show</button>
            </div>
            <div class="strength-indicator"><div class="strength-bar strength-strong" id="strength-bar"></div></div>
            <p class="description">Save this password somewhere safe.</p>
          </td>
        </tr>
        <tr>
          <th><label for="admin_email">Your Email</label></th>
          <td>
            <input type="email" name="admin_email" id="admin_email" value="admin@example.com" />
            <p class="description">Used for admin notifications only.</p>
          </td>
        </tr>
        <tr>
          <th>Site Type</th>
          <td>
            <div class="theme-choice">
              <label><input type="radio" name="theme_choice" value="blog" checked /><span>Simple Blog</span><span class="theme-desc">Clean, minimal blog layout</span></label>
              <label><input type="radio" name="theme_choice" value="standard" /><span>Full WordPress</span><span class="theme-desc">Standard WordPress with all features</span></label>
            </div>
          </td>
        </tr>
      </table>
      <div class="submit-row">
        <button type="submit" class="submit-btn" id="submit-btn">Install OnionPress</button>
      </div>
    </form>
  </div>
  <p class="note">This page is only accessible from your local machine.</p>
</div>
<script>
function togglePassword() {
  var inp = document.getElementById('admin_password');
  var btn = document.querySelector('.toggle-password');
  if (inp.type === 'password') { inp.type = 'text'; btn.textContent = 'Hide'; }
  else { inp.type = 'password'; btn.textContent = 'Show'; }
}
function checkStrength() {
  var pw = document.getElementById('admin_password').value;
  var bar = document.getElementById('strength-bar');
  bar.className = 'strength-bar';
  if (pw.length >= 12 && /[A-Z]/.test(pw) && /[0-9]/.test(pw) && /[^A-Za-z0-9]/.test(pw)) {
    bar.classList.add('strength-strong');
  } else if (pw.length >= 8) {
    bar.classList.add('strength-medium');
  } else {
    bar.classList.add('strength-weak');
  }
}
document.getElementById('admin_password').addEventListener('input', checkStrength);
document.getElementById('setup-form').addEventListener('submit', function() {
  var btn = document.getElementById('submit-btn');
  btn.disabled = true;
  btn.textContent = 'Installing...';
});
</script>
</body>
</html>'''

SETUP_SUCCESS_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OnionPress &rsaquo; Setup Complete</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #f0f0f1; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Oxygen-Sans, Ubuntu, Cantarell, "Helvetica Neue", sans-serif; font-size: 14px; color: #3c434a; }
  .setup-container { max-width: 580px; margin: 40px auto; padding: 0 20px; text-align: center; }
  .logo h1 { font-size: 28px; color: #1d2327; margin-bottom: 20px; }
  .logo h1 span { color: #7b4e9e; }
  .setup-box { background: #fff; border: 1px solid #c3c4c7; border-radius: 4px; padding: 40px 24px; box-shadow: 0 1px 3px rgba(0,0,0,.04); }
  .success-icon { font-size: 48px; margin-bottom: 16px; }
  h2 { color: #00a32a; margin-bottom: 12px; }
  p { margin-bottom: 12px; line-height: 1.6; }
  .spinner { display: inline-block; width: 20px; height: 20px; border: 3px solid #ddd; border-top-color: #7b4e9e; border-radius: 50%; animation: spin 1s linear infinite; vertical-align: middle; margin-right: 8px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .status { color: #646970; margin-top: 20px; font-size: 13px; }
</style>
</head>
<body>
<div class="setup-container">
  <div class="logo">
    <h1><span>&#x1F9C5;</span> OnionPress</h1>
  </div>
  <div class="setup-box">
    <div class="success-icon">&#x2705;</div>
    <h2>WordPress Configured!</h2>
    <p>Your admin account has been created.</p>
    <p><span class="spinner"></span>Starting Tor onion service&hellip;</p>
    <p class="status">Your .onion address will appear in the menu bar when ready.<br>This page will close automatically.</p>
  </div>
</div>
</body>
</html>'''


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

        # Setup page (first-run WordPress configuration)
        if self.path.startswith('/setup'):
            if self.command == 'POST':
                self._handle_setup_post()
            else:
                self._handle_setup_get()
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

    def _handle_setup_get(self):
        """Serve the WordPress setup form (first-run only)."""
        # Check if WordPress is already installed
        try:
            result = subprocess.run(
                [self.server.docker_bin, "exec", "onionpress-wordpress",
                 "wp", "core", "is-installed", "--allow-root"],
                env=self.server.docker_env,
                capture_output=True, timeout=10
            )
            if result.returncode == 0:
                body = b'<html><head><meta http-equiv="refresh" content="0;url=/status"></head><body>WordPress is already configured.</body></html>'
                self.send_response(302)
                self.send_header('Location', '/status')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        except Exception:
            pass

        # Generate a strong random password
        chars = string.ascii_letters + string.digits + '!@#$%^&*'
        generated_password = ''.join(secrets.choice(chars) for _ in range(24))

        html = SETUP_PAGE_HTML.replace('{{GENERATED_PASSWORD}}', generated_password)
        body = html.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_setup_post(self):
        """Process the WordPress setup form submission."""
        # Read POST body
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length == 0:
            self.send_error(400, "No form data")
            return

        post_data = self.rfile.read(content_length)
        params = parse_qs(post_data.decode('utf-8'))

        title = params.get('blog_title', ['My OnionPress Site'])[0]
        username = params.get('user_name', [''])[0]
        password = params.get('admin_password', [''])[0]
        email = params.get('admin_email', ['admin@example.com'])[0]
        # theme_choice captured for future use
        # theme = params.get('theme_choice', ['standard'])[0]

        if not username or not password:
            body = b'<html><body><h1>Error</h1><p>Username and password are required.</p><p><a href="/setup">Go back</a></p></body></html>'
            self.send_response(400)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # Run wp core install
        try:
            cmd = [
                self.server.docker_bin, "exec", "onionpress-wordpress",
                "wp", "core", "install",
                "--url=http://localhost",
                f"--title={title}",
                f"--admin_user={username}",
                f"--admin_password={password}",
                f"--admin_email={email}",
                "--skip-email",
                "--allow-root"
            ]
            result = subprocess.run(
                cmd, env=self.server.docker_env,
                capture_output=True, text=True, timeout=30
            )

            if result.returncode == 0:
                if self.server.log_func:
                    self.server.log_func(f"WordPress installed successfully via setup page")
                body = SETUP_SUCCESS_HTML.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                if self.server.log_func:
                    self.server.log_func(f"wp core install failed: {result.stderr}")
                error_msg = result.stderr.strip() or "Unknown error"
                body = f'<html><body><h1>Setup Failed</h1><p>{error_msg}</p><p><a href="/setup">Try again</a></p></body></html>'.encode('utf-8')
                self.send_response(500)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        except Exception as e:
            if self.server.log_func:
                self.server.log_func(f"Setup error: {e}")
            body = f'<html><body><h1>Setup Error</h1><p>{e}</p><p><a href="/setup">Try again</a></p></body></html>'.encode('utf-8')
            self.send_response(500)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def _handle_status(self):
        """Return proxy status as JSON."""
        # Write extension-connected marker when polled by the browser extension
        if self.server.data_dir:
            try:
                browser = self.headers.get('X-OnionPress-Browser', '')
                if browser:
                    marker = os.path.join(self.server.data_dir, "extension-connected")
                    data = json.dumps({"timestamp": int(time.time()), "browser": browser})
                    with open(marker, 'w') as f:
                        f.write(data)
            except Exception:
                pass

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
        self.data_dir = None
        self.launcher_script = None
        self.cache = ProxyCache()
        super().__init__(*args, **kwargs)


def stop_proxy(server):
    """Stop the proxy server."""
    if server:
        server.shutdown()

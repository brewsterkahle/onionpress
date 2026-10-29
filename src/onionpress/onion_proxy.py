#!/usr/bin/env python3
"""
OnionPress Local Proxy Server

A local HTTP server on localhost:9077 (+10000 per additional macOS user):
  - an HTTP forward proxy for .onion URLs only, through Tor (via the PHP
    proxy in the WordPress container). The browser extension routes .onion
    URLs through it (an HTTP proxy on the Mac itself: Tor's SOCKS port does
    not work through Colima's port forwarding, issue #318); it routes
    nothing else here, and the proxy refuses every other target: no
    clearnet fetch, no CONNECT relay, nothing on the LAN or this Mac's
    loopback (it used to relay all of those for any local client).
    /proxy/{host}/{path} does the same for a tool that cannot use a proxy.
  - /status: whether OnionPress is running, and its onion address (the
    extension polls it).
  - /setup: the first-run WordPress setup page.

Any web page in any browser on this Mac can send requests to 127.0.0.1.
/status and /proxy/ answer the extension (its Origin), the proxy's own
pages, the address bar and command-line tools, and refuse a request
another site's page made (_from_a_web_page): /status named the onion
address this Mac serves with Access-Control-Allow-Origin: *, which tied
the person's clearnet browsing to their onion site, and /proxy/ let a
page send the person's Tor to any onion URL.

A request's Host must name this proxy too (own_hosts, issue #465): a
clear-web page whose name's DNS is pointed at 127.0.0.1 (DNS rebinding)
reaches this port with its own name as Host, and its fetches carry what
the proxy's own page's carry. Only an absolute-form request (GET
http://x.onion/ HTTP/1.1, the extension's proxy setting) names its target
there. And a /proxy/ page is sandboxed (PROXIED_PAGE_CSP): a visited onion
site's script was otherwise same-origin with /status and /setup. The links
the proxy rewrites into such a page carry PROXY_PAGE_TOKEN, which is how
the page's own requests are told from another site's.
"""

import hmac
import os
import re
import secrets
import socket
import string
import subprocess
import json
import threading
import time
import http.client
from collections import OrderedDict
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from html import escape as html_escape
from urllib.parse import urlparse, parse_qs

PROXY_PORT = int(os.environ.get("ONIONPRESS_PROXY_PORT", 9077))
PHP_PROXY_PORT = int(os.environ.get("ONIONPRESS_WP_PORT", 8080))
PHP_PROXY_PATH = "/__op_proxy.php"
# /__op_proxy.php answers only requests that carry this token. Onion and
# Cloudflare Tunnel visitors reach the same WordPress, so the forwarder
# cannot tell this app from them by address alone (src/onion-forward.php).
# install_php_proxy writes the token into the copy it installs, in place of
# PHP_PROXY_TOKEN_PLACEHOLDER; new for each run of the app, kept in memory.
PHP_PROXY_TOKEN = secrets.token_urlsafe(32)
PHP_PROXY_TOKEN_PLACEHOLDER = "__ONIONPRESS_PROXY_TOKEN__"
# How onion-forward.php's body starts when Tor could not fetch the URL.
PHP_PROXY_FETCH_FAILED = b"Tor fetch failed"
# /proxy/ serves any onion site's page at this proxy's own origin, so every
# response under it carries a CSP sandbox without allow-same-origin: the
# browser gives the page an opaque origin, and its script is no longer
# same-origin with /status (the onion address) or /setup. The site's
# scripts, forms, links, popups and downloads keep working.
PROXIED_PAGE_CSP = ("sandbox allow-forms allow-scripts allow-popups "
                    "allow-modals allow-downloads")
# A sandboxed page's own requests (its images, scripts, links, forms and
# fetches) all arrive as Sec-Fetch-Site: cross-site with Origin null or
# none, exactly what another site's page sends, so _from_a_web_page cannot
# tell them apart. The links the proxy rewrites into a proxied page carry
# this token instead (/proxy/<token>/<host>/...): a page the proxy served
# knows it, a page from elsewhere does not. New for each run of the app.
PROXY_PAGE_TOKEN = secrets.token_urlsafe(16)
WAYBACK_PREFIX = "https://web.archive.org/web/"

# Shown for a page load whose onion service did not answer (_send_onion_unreachable).
ONION_UNREACHABLE_PAGE = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Onion site not reachable</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f0f0f1; color: #3c434a; margin: 0; padding: 40px 20px; }
  .box { max-width: 560px; margin: 0 auto; background: #fff; border: 1px solid #c3c4c7; border-radius: 4px; padding: 24px; }
  h1 { font-size: 20px; color: #1d2327; margin-top: 0; }
  code { word-break: break-all; }
  a.btn { display: inline-block; margin: 8px 8px 0 0; padding: 8px 18px; border-radius: 4px; background: #7b4e9e; color: #fff; text-decoration: none; }
  a.btn.secondary { background: #dcdcde; color: #1d2327; }
  .note { font-size: 13px; color: #646970; }
</style>
</head>
<body>
<div class="box">
  <h1>This onion site did not answer</h1>
  <p>OnionPress asked Tor for <code>{{URL}}</code>, but the onion service could not be reached. It may be offline, or Tor may still be finding a route to it.</p>
  <p><a class="btn" href="{{URL}}">Try again</a><a class="btn secondary" href="{{WAYBACK_URL}}">Archived copy in the Wayback Machine</a></p>
  <p class="note">The Wayback Machine link opens web.archive.org over the regular internet, not through Tor, so archive.org will see which address you asked for.</p>
</div>
</body>
</html>
'''
# A v3 onion host, optionally under subdomain labels (Tor serves
# www.<address>.onion as <address>.onion). Matched with fullmatch against a
# lowercased host without a trailing dot (see _onion_host).
ONION_HOST_RE = re.compile(r'(?:[a-z0-9-]+\.)*[a-z2-7]{56}\.onion')
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
  .error-banner { background: #fcf0f1; border: 1px solid #d63638; border-radius: 4px; padding: 12px 16px; margin-bottom: 16px; color: #d63638; font-size: 14px; }
  .error-banner a { color: #d63638; font-weight: 600; }
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
    {{ERROR_BANNER}}
    <form method="post" action="/setup" id="setup-form">
      <table class="form-table">
        <tr>
          <th><label for="blog_title">Site Title</label></th>
          <td><input type="text" name="blog_title" id="blog_title" value="My OnionPress Site" /></td>
        </tr>
        <tr>
          <th><label for="user_name">Username</label></th>
          <td>
            <input type="text" name="user_name" id="user_name" value="{{SUGGESTED_USERNAME}}" autocomplete="username" />
            <p class="description">Also your site's address (/username/). Usernames cannot be changed later.</p>
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
        <button type="submit" class="submit-btn" id="submit-btn">Configure OnionPress</button>
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
    <p id="tor-status"><span class="spinner"></span>Starting Tor onion service&hellip;</p>
    <p class="status">Your .onion address will appear in the menu bar when ready, which can be used in a Tor-capable browser (Tor Browser or Brave Browser in Tor mode or a browser running the OnionPress extension).</p>
  </div>
</div>
<script>
(function() {
  var poll = setInterval(function() {
    fetch('/status').then(function(r) { return r.json(); }).then(function(d) {
      if (d.tor_ready) {
        clearInterval(poll);
        document.getElementById('tor-status').innerHTML = '&#x2705; Tor onion service started!';
      }
    }).catch(function() {});
  }, 3000);
})();
</script>
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
    """Copy the PHP proxy script into the WordPress container, with
    PHP_PROXY_TOKEN written into it.

    Written as www-data (wp_files): www-data owns the web root, and root's
    docker cp followed a link planted at __op_proxy.php to any file in the
    container.
    """
    from onionpress import wp_files
    try:
        with open(php_script_path, encoding="utf-8") as f:
            script = f.read()
        if script.count(PHP_PROXY_TOKEN_PLACEHOLDER) != 1:
            if log_func:
                log_func(f"Failed to install PHP proxy: {php_script_path} "
                         "has no single token placeholder")
            return False
        result = wp_files.put_file(
            "/var/www/html/__op_proxy.php",
            script.replace(PHP_PROXY_TOKEN_PLACEHOLDER, PHP_PROXY_TOKEN), "644",
            docker_bin=docker_bin, env=docker_env, timeout=10)
        if result.returncode == 0:
            if log_func:
                log_func("Installed PHP proxy in WordPress container")
            return True
        else:
            if log_func:
                log_func("Failed to install PHP proxy: "
                         + result.stderr.decode("utf-8", "replace"))
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
                     headers={"X-OnionPress-Action": "status",
                              "X-OnionPress-Token": PHP_PROXY_TOKEN})
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


def _script_json(value):
    """*value* as a JavaScript literal safe inside an inline <script>:
    json.dumps leaves '<', '>' and '&' alone, so a "</script>" in the value
    would end the script element."""
    return (json.dumps(value).replace('<', '\\u003c').replace('>', '\\u003e')
            .replace('&', '\\u0026'))


def own_hosts(port):
    """The Host values that name this proxy: its loopback names with its
    port (a bare name only when the port is 80, which it never is). A
    request for any other Host was made for a name whose DNS a page
    pointed at 127.0.0.1, and is refused."""
    names = ("127.0.0.1", "localhost", "[::1]")
    own = {f"{name}:{port}" for name in names}
    if port == 80:
        own.update(names)
    return own


def _onion_host(host):
    """Classify a request's target host: (host, is_onion, ok).

    *host* comes back lowercased and without a trailing dot, which names the
    same host: "<address>.onion." must not pass for a clearnet name and go
    out through the system resolver. Everything in the .onion TLD is onion
    traffic; it is *ok* only as a v3 address (ONION_HOST_RE), and anything
    else there is refused rather than fetched, neither through Tor (a
    "/proxy/evil.example?.onion" target) nor directly.
    """
    host = (host or "").lower().rstrip(".")
    is_onion = host == "onion" or host.endswith(".onion")
    ok = not is_onion or ONION_HOST_RE.fullmatch(host) is not None
    return host, is_onion, ok


class OnionProxyHandler(BaseHTTPRequestHandler):
    """HTTP forward proxy: .onion via Tor, clearnet direct."""

    def log_message(self, format, *args):
        if self.server.log_func:
            self.server.log_func(f"Proxy: {format % args}")

    def log_request(self, code='-', size='-'):
        # The browser extension polls /status every few seconds (it is how
        # the menubar knows the extension is there); a line for each would
        # bury everything else in onionpress.log.
        if self.path == '/status' and getattr(code, 'value', code) == 200:
            return
        super().log_request(code, size)

    def do_GET(self):
        self._handle_request()

    def do_POST(self):
        self._handle_request()

    def do_HEAD(self):
        self._handle_request(head_only=True)

    def _get_cors_origin(self):
        """Return the CORS origin header value if the request is from a browser extension."""
        origin = self.headers.get('Origin', '')
        if origin.startswith('moz-extension://') or origin.startswith('chrome-extension://'):
            return origin
        return None

    def _own_origins(self):
        port = getattr(self.server, 'server_port', None)
        return tuple(sorted("http://" + host for host in own_hosts(port)))

    def _host_is_own(self):
        """True when the request's Host names this proxy (own_hosts). A
        page at a name its DNS rebinds to 127.0.0.1 reaches this port with
        that name as Host, and its same-origin fetches carry no Origin and
        Sec-Fetch-Site: same-origin, what _from_a_web_page takes for the
        proxy's own page."""
        host = self.headers.get('Host')
        if host is None:
            return False
        port = getattr(self.server, 'server_port', None)
        return host.strip().lower() in own_hosts(port)

    def _refuse_host(self):
        # Nothing of the request goes into the body or the log.
        self.send_error(403, "Not this proxy's host")

    def end_headers(self):
        # Every response under /proxy/ (a page, a subresource, a cached
        # copy, the unreachable page, an error) is a document at this
        # proxy's origin when a browser renders it: sandbox it.
        if getattr(self, 'path', '').startswith('/proxy/'):
            self.send_header('Content-Security-Policy', PROXIED_PAGE_CSP)
        super().end_headers()

    def _from_a_web_page(self):
        """True for a request a browser made on behalf of another site's
        page. Browsers add Origin to cross-origin fetches and Sec-Fetch-Site
        to every request for 127.0.0.1 (a potentially trustworthy origin),
        which no page can remove or forge. The extension's own fetches carry
        its Origin, the proxy's own pages are same-origin, an address-bar
        visit is Sec-Fetch-Site: none, and a command-line tool sends none
        of these. localhost:<wp port> is same-site with localhost:<this>,
        so same-site counts as another site's page."""
        if self._get_cors_origin():
            return False
        own = self._own_origins()
        origin = self.headers.get('Origin')
        if origin is not None:
            return origin not in own
        site = self.headers.get('Sec-Fetch-Site')
        if site is not None:
            return site not in ('same-origin', 'none')
        referer = self.headers.get('Referer')
        if referer:
            return not referer.startswith(tuple(o + '/' for o in own))
        return False

    def _refuse_web_page(self):
        self.send_error(403, "Not for web pages")

    def do_OPTIONS(self):
        """Handle CORS preflight requests (browser extensions only)."""
        if not self._host_is_own():
            self._refuse_host()
            return
        self.send_response(204)
        cors_origin = self._get_cors_origin()
        if cors_origin:
            self.send_header('Access-Control-Allow-Origin', cors_origin)
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, HEAD, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-OnionPress-Browser')
            self.send_header('Access-Control-Max-Age', '86400')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_CONNECT(self):
        """No tunnelling: the extension sends .onion over http:// only, and
        a CONNECT relay to any host:port was an open proxy for every local
        client (the LAN, this Mac's loopback, the internet)."""
        self.send_error(403, "This proxy carries .onion sites only (http://)")

    def _handle_request(self, head_only=False):
        # An absolute-form request names its target in Host; every other
        # request must name this proxy there, whatever it asks for.
        if not self.path.startswith('http://') and not self._host_is_own():
            self._refuse_host()
            return

        # Status endpoint
        if self.path == '/status':
            if self._from_a_web_page():
                self._refuse_web_page()
                return
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
            rest = self.path[len('/proxy/'):]
            token, slash, tail = rest.partition('/')
            if slash and hmac.compare_digest(token.encode('latin-1'),
                                             PROXY_PAGE_TOKEN.encode('ascii')):
                # A link the proxy rewrote into a page it served
                # (_rewrite_onion_links): the sandboxed page's own request.
                rest = tail
            elif self._from_a_web_page():
                self._refuse_web_page()
                return
            parts = rest.split('/', 1)
            target_host = parts[0].lower()
            target_path = '/' + parts[1] if len(parts) > 1 else '/'
            target_port = None
        else:
            self.send_error(404, "Use /proxy/{host}/{path} or /status")
            return

        target_host, is_onion, onion_ok = _onion_host(target_host)
        if not onion_ok:
            self.send_error(400, "Not a valid v3 .onion address")
            return

        # .onion only, in both forms: the extension routes nothing else here,
        # and a clearnet fetch reached the LAN and this Mac's loopback.
        if not is_onion:
            if is_forward_proxy:
                self.send_error(403, "This proxy carries .onion sites only")
            else:
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

        # Fetch through Tor
        try:
            status, resp_headers, body = self._fetch_via_php(
                target_url, post_data=post_data, content_type=content_type,
                head_only=head_only
            )
        except Exception as e:
            if is_onion and self._wants_page():
                self._send_onion_unreachable(target_url, head_only)
                return
            self.send_error(502, f"Fetch failed: {e}")
            return

        # Tor could not reach the onion service (onion-forward.php's own
        # 502, not one the site sent): a page with a way to the archived copy.
        if (is_onion and status == 502 and body.startswith(PHP_PROXY_FETCH_FAILED)
                and self._wants_page()):
            self._send_onion_unreachable(target_url, head_only)
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
        cors_origin = self._get_cors_origin()
        if cors_origin:
            self.send_header('Access-Control-Allow-Origin', cors_origin)
        self.send_header('Referrer-Policy', 'no-referrer')
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _wants_page(self):
        """True for a request a person is looking at (a page load), not a
        script, image or form post: those keep the plain 502."""
        return (self.command in ('GET', 'HEAD')
                and 'text/html' in self.headers.get('Accept', ''))

    def _send_onion_unreachable(self, url, head_only=False):
        """502 with a page saying the onion service did not answer, offering
        a retry and the Wayback Machine's copy (issue #99). The archive link
        is only a link: following it is the visitor's choice, and it goes to
        web.archive.org over the regular internet, which the page says."""
        body = ONION_UNREACHABLE_PAGE.replace(
            '{{URL}}', html_escape(url, quote=True)).replace(
            '{{WAYBACK_URL}}', html_escape(WAYBACK_PREFIX + url, quote=True)
        ).encode('utf-8')
        self._send_response(502, {'content-type': 'text/html; charset=utf-8',
                                  'cache-control': 'no-store'},
                            body, head_only)

    def _fetch_via_php(self, url, post_data=None, content_type=None, head_only=False):
        """Fetch a .onion URL through the PHP proxy (via Tor SOCKS)."""
        headers = {"X-OnionPress-URL": url, "X-OnionPress-Token": PHP_PROXY_TOKEN}
        if content_type:
            headers["Content-Type"] = content_type

        method = "HEAD" if head_only else ("POST" if post_data else "GET")

        try:
            conn = http.client.HTTPConnection("127.0.0.1", PHP_PROXY_PORT, timeout=60)
            conn.request(method, PHP_PROXY_PATH, body=post_data, headers=headers)
            resp = conn.getresponse()
            body = resp.read()
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            status = resp.status
            conn.close()
        except OSError as e:
            # Log once on first failure, then every 5 minutes while down.
            # OSError covers ConnectionRefusedError, timeout, etc. — all signs
            # that the Lima SSH port forward for localhost:PHP_PROXY_PORT has dropped.
            srv = self.server
            now = time.time()
            if srv._wp_unreachable_since is None:
                srv._wp_unreachable_since = now
                srv._wp_unreachable_last_log = now
                if srv.log_func:
                    srv.log_func(
                        "WARNING: WordPress unreachable on localhost:%d — "
                        "Lima port forward may be down (%s)", PHP_PROXY_PORT, e)
            elif now - srv._wp_unreachable_last_log >= 300:
                srv._wp_unreachable_last_log = now
                elapsed = int(now - srv._wp_unreachable_since)
                if srv.log_func:
                    srv.log_func(
                        "WARNING: WordPress still unreachable on localhost:%d — "
                        "%ds elapsed (%s)", PHP_PROXY_PORT, elapsed, e)
            raise

        # Recovery: log once when WordPress comes back after a failure.
        if self.server._wp_unreachable_since is not None:
            if self.server.log_func:
                elapsed = int(time.time() - self.server._wp_unreachable_since)
                self.server.log_func(
                    "WordPress reachable again on localhost:%d after %ds down",
                    PHP_PROXY_PORT, elapsed)
            self.server._wp_unreachable_since = None
            self.server._wp_unreachable_last_log = None

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

        # The rewritten links carry the token: a sandboxed page's requests
        # cannot otherwise be told from another site's (PROXY_PAGE_TOKEN).
        proxy_prefix = f"/proxy/{PROXY_PAGE_TOKEN}/{onion_host}"

        ONION_URL_RE = re.compile(
            r'(https?://)((?:[a-z0-9-]+\.)*[a-z0-9]{16,56}\.onion)((?:/[^\s"\'<>]*)?)',
            re.IGNORECASE
        )

        def replace_abs_onion(match):
            host = match.group(2)
            path = match.group(3) or ''
            return f"/proxy/{PROXY_PAGE_TOKEN}/{host}{path}"

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
            from onionpress import wp_files
            result = subprocess.run(
                [self.server.docker_bin] + wp_files.wp_cli_args("core", "is-installed"),
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

        # The alphabet includes '&': escape it for the value="..." attribute.
        import html as html_mod
        html = SETUP_PAGE_HTML.replace('{{GENERATED_PASSWORD}}',
                                       html_mod.escape(generated_password))
        # The user name is the onionname: setup_logic suggests one, as the
        # setup window does ("admin", the form's old default, without one).
        from onionpress import setup_logic
        html = html.replace('{{SUGGESTED_USERNAME}}',
                            html_mod.escape(setup_logic.suggest_onionname() or 'admin'))
        html = html.replace('{{ERROR_BANNER}}', '')
        body = html.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_setup_form_with_error(self, params, error_msg):
        """Re-render the setup form with an error banner and previously entered values."""
        import html as html_mod

        banner = (
            '<div class="error-banner">'
            + html_mod.escape(error_msg)
            + '</div>'
        )

        # Preserve previously entered values. The password goes into a
        # value="..." attribute: escape it.
        admin_password = html_mod.escape(params.get('admin_password', [''])[0])

        page = SETUP_PAGE_HTML.replace('{{GENERATED_PASSWORD}}', admin_password)
        page = page.replace('{{SUGGESTED_USERNAME}}', '')
        page = page.replace('{{ERROR_BANNER}}', banner)

        # Also preserve other field values via JavaScript
        blog_title = params.get('blog_title', ['My OnionPress Site'])[0]
        user_name = params.get('user_name', [''])[0]
        theme = params.get('theme_choice', ['blog'])[0]

        # Inject script to restore form values
        restore_script = (
            '<script>'
            f'document.getElementById("blog_title").value={_script_json(blog_title)};'
            f'document.getElementById("user_name").value={_script_json(user_name)};'
            f'var radios=document.querySelectorAll("input[name=theme_choice]");'
            f'radios.forEach(function(r){{r.checked=r.value==={_script_json(theme)}}});'
            '</script>'
        )
        page = page.replace('</body>', restore_script + '</body>')

        body = page.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _setup_post_same_origin(self):
        """False for a POST a browser sent from any page but the setup page
        this proxy served: browsers send Origin with every POST, so any web
        page could otherwise submit the setup form to 127.0.0.1, and a
        proxied onion page (/proxy/, sandboxed: Origin null, every request
        cross-site) is not the setup page either. A client that sends no
        Origin is not a browser."""
        origin = self.headers.get('Origin')
        if origin is None:
            return True
        if origin not in self._own_origins():
            return False
        site = self.headers.get('Sec-Fetch-Site')
        return site is None or site == 'same-origin'

    def _wp_installed(self):
        """True if WordPress is installed, None if that can't be told."""
        from onionpress import wp_files
        try:
            result = subprocess.run(
                [self.server.docker_bin] + wp_files.wp_cli_args("core", "is-installed"),
                env=self.server.docker_env, capture_output=True, timeout=10)
        except (subprocess.SubprocessError, OSError):
            return None
        return result.returncode == 0

    def _send_setup_result(self, status, body):
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_setup_post(self):
        """Process the WordPress setup form submission.

        The install itself is setup_logic.install_fresh_wordpress, the one
        install path every setup front end shares; this handles the form,
        refuses what must not reach it, and reports the result."""
        if not self._setup_post_same_origin():
            self.send_error(403, "Cross-origin setup request refused")
            return
        # First run only. On an installed site the password step would set
        # the password of whichever existing user the form names.
        installed = self._wp_installed()
        if installed is not False:
            if self.server.log_func:
                self.server.log_func(
                    "Setup form refused: WordPress is already installed"
                    if installed else
                    "Setup form refused: could not check whether WordPress is installed")
            self.send_error(409 if installed else 503,
                            "WordPress is already configured" if installed
                            else "Could not check whether WordPress is installed")
            return

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
        # theme_choice captured for future use
        # theme = params.get('theme_choice', ['standard'])[0]

        import html as html_mod
        if not username or not password:
            self._send_setup_result(400, b'<html><body><h1>Error</h1><p>Username and password are required.</p><p><a href="/setup">Go back</a></p></body></html>')
            return

        # Validate setup inputs. The user name is the onionname (the admin
        # login and the site's /<name>/ path), held to setup_logic's rule;
        # the rest must carry no control characters.
        import re
        from onionpress import setup_logic
        _ctrl_re = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
        errors = []
        name_ok, reason = setup_logic.validate_onionname(username)
        if not name_ok:
            errors.append("Username: " + setup_logic.onionname_error_message(reason) + ".")
        if _ctrl_re.search(password):
            errors.append("Password must not contain control characters.")
        if _ctrl_re.search(title) or title.startswith('-'):
            errors.append("Site title must not start with a dash or contain control characters.")
        if len(title) > 200:
            errors.append("One or more fields are too long.")
        if errors:
            msg = '<br>'.join(html_mod.escape(e) for e in errors)
            self._send_setup_result(400, f'<html><body><h1>Error</h1><p>{msg}</p><p><a href="/setup">Go back</a></p></body></html>'.encode())
            return

        launcher = getattr(self.server, 'launcher_script', None)
        if not launcher:
            # Without the launcher the install would skip the multisite
            # conversion, mu-plugins and theme (provision-post-install).
            if self.server.log_func:
                self.server.log_func("Setup form refused: no launcher to provision the site")
            self.send_error(503, "OnionPress launcher not available")
            return

        # Every step's message goes to the OnionPress log; the errors are
        # kept for the failure page.
        errors_logged = []

        def log(msg):
            if self.server.log_func:
                self.server.log_func(msg)
            if msg.startswith("ERROR"):
                errors_logged.append(msg)

        try:
            ok = setup_logic.install_fresh_wordpress(
                site_title=title,
                onionname=username,
                password=password,
                onion_addr=self.server.onion_address or "localhost",
                docker_bin=self.server.docker_bin,
                docker_env=self.server.docker_env,
                launcher_bin=launcher,
                log_func=log,
                data_dir=self.server.data_dir,
            )
        except Exception as e:
            # A subprocess error's message quotes its whole argv; name the
            # error instead.
            detail = (type(e).__name__ if isinstance(e, subprocess.SubprocessError)
                      else str(e))
            log(f"Setup error: {detail}")
            self._send_setup_result(500, f'<html><body><h1>Setup Error</h1><p>{html_mod.escape(detail)}</p><p><a href="/setup">Try again</a></p></body></html>'.encode('utf-8'))
            return

        if ok:
            log("WordPress installed via setup page")
            self._send_setup_result(200, SETUP_SUCCESS_HTML.encode('utf-8'))
        elif self._wp_installed():
            # Installed, but the admin password was not set.
            self._send_setup_result(500, b'<html><body><h1>Setup Incomplete</h1>'
                                    b'<p>WordPress was installed, but your admin password '
                                    b'could not be set. See the OnionPress log.</p>'
                                    b'</body></html>')
        else:
            # wp-cli's stderr quotes the submitted title and user name back:
            # escape it, or the form is a script injection.
            error_msg = html_mod.escape("\n".join(errors_logged) or "Unknown error")
            self._send_setup_result(500, f'<html><body><h1>Setup Failed</h1><p>{error_msg}</p><p><a href="/setup">Try again</a></p></body></html>'.encode('utf-8'))

    def _handle_status(self):
        """Return proxy status as JSON."""
        # Write extension-connected marker when polled by the browser extension
        browser = self.headers.get('X-OnionPress-Browser', '')
        if self.server.data_dir and browser:
            from onionpress import browser as op_browser
            op_browser.write_extension_marker(self.server.data_dir, browser)

        cache_stats = self.server.cache.stats() if self.server.cache else {}
        info = {
            "running": True,
            "proxy_port": self.server.server_port,
            "onion_address": self.server.onion_address,
            "healthcheck_address": getattr(self.server, 'healthcheck_address', None),
            "tor_ready": getattr(self.server, 'tor_ready', False),
            "version": self.server.version,
            "cache": cache_stats,
        }
        body = json.dumps(info).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        # The extension reads this cross-origin, so it gets its own Origin
        # back; never "*": the onion address names the site this Mac serves.
        cors_origin = self._get_cors_origin()
        if cors_origin:
            self.send_header('Access-Control-Allow-Origin', cors_origin)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """HTTP server that handles each request in a new thread."""
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *args, **kwargs):
        self.onion_address = None
        self.healthcheck_address = None
        self.tor_ready = False
        self.version = "unknown"
        self.docker_bin = "docker"
        self.docker_env = None
        self.log_func = None
        self.data_dir = None
        self.launcher_script = None
        self.cache = ProxyCache()
        self._wp_unreachable_since = None   # time.time() of first failure, or None
        self._wp_unreachable_last_log = None  # time.time() of last warning log
        super().__init__(*args, **kwargs)


def stop_proxy(server):
    """Stop the proxy server."""
    if server:
        server.shutdown()

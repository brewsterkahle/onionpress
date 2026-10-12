"""Privacy scrubbing for OnionPress logs.

Runs at log rotation time (see ``log_rotation.RotatingLog``): when a
file rolls off, its bytes pass through :func:`redact_bytes` before
gzip-compression, so the stored ``.log.gz`` on disk — and the bytes
later offered to OnionHome — contain no visitor IPs and no sensitive
URL parameters or request headers.

Runs again at upload time (see ``analytics_sharing._do_upload_cycle``)
for every file that leaves the machine without having been rolled by a
RotatingLog: the Mac's active logs and launcher logs, and every Linux
log, which are plain files nothing scrubs on disk. :func:`scrub_for_upload`
applies the same rules in one pass.

What gets scrubbed
==================
* Query-string values for well-known auth/session parameters
  (``?nonce=``, ``?token=``, ``?key=``, ``?password=``, ``?_wpnonce=``,
  …).  The parameter name is kept; the value becomes ``<redacted>``.
* HTTP request headers that commonly carry credentials
  (``Cookie``, ``Set-Cookie``, ``Authorization``, ``X-Auth-Token``,
  ``X-Api-Key``).  Replaced with ``<header>: <redacted>``.
* IPv4 and IPv6 addresses.  Replaced with a deterministic
  IPv6-in-``fd00::/8`` (RFC 4193 ULA) pseudonym computed from a
  32-byte random salt that rotates daily.  Within a day the same
  real IP always maps to the same pseudonym, so unique-visitor
  counts still work on the receiving end; across days the salt
  changes and the pseudonym does too, so long-term correlation is
  broken.

The salt is stored under ``<app_support>/ip-salts/salt-YYYY-MM-DD``
with mode 0600 and is never shipped: only files beneath
``<app_support>/logs/`` are offered in the analytics manifest.
Salts older than 30 days are garbage-collected on each access.
"""

import hashlib
import ipaddress
import os
import re
import secrets
from datetime import datetime, timezone


# Query-string parameter names whose values are sensitive. Scrubbed
# case-insensitively so ``?Token=`` and ``?TOKEN=`` are both caught.
_SENSITIVE_PARAMS = (
    "key", "nonce", "_wpnonce", "token", "access_token", "refresh_token",
    "id_token", "api_key", "apikey", "secret", "password", "passwd",
    "pass", "pwd", "auth", "authorization", "session", "sessionid",
    "sid", "csrf", "hmac", "signature", "hash",
)
_PARAM_RE = re.compile(
    r"([?&])(" + "|".join(re.escape(p) for p in _SENSITIVE_PARAMS)
    + r")=[^&\s\"'<>]*",
    re.IGNORECASE,
)

# Request-header lines carrying credentials.
_HEADER_RE = re.compile(
    r"\b(Cookie|Set-Cookie|Authorization|Proxy-Authorization|"
    r"X-Auth-Token|X-Api-Key):\s*[^\r\n]*",
    re.IGNORECASE,
)

# IPv4: four dotted octets, bounded so software version strings like
# ``1.2.3.4-rc5`` or ``192.168.1.0/24`` don't partially match.
_IPV4_RE = re.compile(r"(?<![\w.\-/])(\d{1,3}(?:\.\d{1,3}){3})(?![\w.\-/])")

# IPv6: candidate tokens with at least two colons between hex groups.
# Intentionally loose — anything it matches is validated via
# ``ipaddress.ip_address`` in :func:`_replace_ip`; non-IPv6 tokens are
# passed through unchanged. This handles both full form
# (``2001:db8:0:0:0:0:0:1``) and compressed form (``2001:db8::1``).
_IPV6_RE = re.compile(
    r"(?<![\w:])("
    r"(?:[0-9a-fA-F]{1,4}:){1,7}:?(?:[0-9a-fA-F]{1,4})?"
    r"(?:(?::[0-9a-fA-F]{1,4}){0,7})"
    r"|::(?:[0-9a-fA-F]{1,4}:){0,7}[0-9a-fA-F]{1,4}"
    r")(?![\w:])"
)

SALT_DIR = "ip-salts"
_SALT_SIZE = 32
_SALT_MAX_AGE_DAYS = 30

# Visitor-facing logs get IP pseudonymization; internal/outbound logs
# keep real addresses because those IPs are destinations, not humans,
# and scrubbing them destroys debugging value with no privacy benefit.
# All logs get URL query-param and credential-header scrubbing regardless.
IP_SCRUB_TYPES = frozenset({
    "wordpress-access", "wordpress-visitors", "container-wordpress",
})


def scrubs_ips(log_name):
    """True if *log_name* — a log type, or a file name that starts with
    one (``container-wordpress-2026-10-12-001.log``,
    ``container-wordpress.log``) — is a visitor-facing log."""
    return any(
        log_name == t or log_name.startswith((t + "-", t + "."))
        for t in IP_SCRUB_TYPES
    )


def _salt_dir_path(app_support):
    return os.path.join(app_support, SALT_DIR)


def get_daily_salt(app_support, day=None):
    """Return the 32-byte pseudonymization salt for *day* (UTC).

    Creates a fresh random salt on first use of each day. The salt is
    written with mode 0600 to ``<app_support>/ip-salts/salt-YYYY-MM-DD``
    and is explicitly outside the logs directory so it is never part
    of the analytics offer.
    """
    if day is None:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    salt_dir = _salt_dir_path(app_support)
    os.makedirs(salt_dir, mode=0o700, exist_ok=True)
    path = os.path.join(salt_dir, f"salt-{day}")
    try:
        with open(path, "rb") as f:
            data = f.read()
        if len(data) == _SALT_SIZE:
            return data
    except FileNotFoundError:
        pass
    salt = secrets.token_bytes(_SALT_SIZE)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, salt)
        finally:
            os.close(fd)
    except FileExistsError:
        # Lost a race — read whatever the winner wrote.
        with open(path, "rb") as f:
            salt = f.read()
    _gc_old_salts(salt_dir)
    return salt


def _gc_old_salts(salt_dir):
    cutoff = datetime.now(timezone.utc).timestamp() - _SALT_MAX_AGE_DAYS * 86400
    try:
        names = os.listdir(salt_dir)
    except OSError:
        return
    for name in names:
        p = os.path.join(salt_dir, name)
        try:
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
        except OSError:
            pass


def pseudonymize_ip(ip_str, salt):
    """Map *ip_str* to a stable IPv6 pseudonym under *salt*.

    The output is always in ``fd00::/8`` (RFC 4193 Unique Local
    Addresses) so it's visibly synthetic and cannot collide with a
    real public address.
    """
    digest = hashlib.sha256(salt + ip_str.encode("ascii")).digest()
    return str(ipaddress.IPv6Address(bytes([0xfd]) + digest[:15]))


def _replace_ip(match, salt):
    ip_text = match.group(1)
    try:
        ipaddress.ip_address(ip_text)
    except ValueError:
        return match.group(0)
    return pseudonymize_ip(ip_text, salt)


def redact_text(text, salt, scrub_ips=True):
    """Return *text* scrubbed of sensitive values.

    URL query params and credential headers are always stripped. IP
    addresses are only pseudonymized when *scrub_ips* is True — this
    should be enabled for visitor/access/PHP-error logs (where IPs
    identify individual humans) and disabled for logs whose IPs are
    destinations (clearnet, Tor status) since pseudonymizing those
    destroys debugging value with no privacy gain.
    """
    text = _PARAM_RE.sub(r"\1\2=<redacted>", text)
    text = _HEADER_RE.sub(lambda m: f"{m.group(1)}: <redacted>", text)
    if scrub_ips:
        text = _IPV4_RE.sub(lambda m: _replace_ip(m, salt), text)
        text = _IPV6_RE.sub(lambda m: _replace_ip(m, salt), text)
    return text


def redact_bytes(raw, salt, scrub_ips=True):
    """Decode *raw* as UTF-8 (with replacement), scrub, encode back."""
    return redact_text(
        raw.decode("utf-8", errors="replace"), salt, scrub_ips=scrub_ips,
    ).encode("utf-8")


def make_scrub_fn(app_support, scrub_ips=True):
    """Return a callback suitable for ``RotatingLog(scrub_fn=...)``.

    The callback fetches today's salt at the moment of rotation (once
    per day, cached via the file on disk) and scrubs the rolled file's
    bytes before gzip compression.
    """
    def _scrub(raw_bytes):
        salt = get_daily_salt(app_support) if scrub_ips else b""
        return redact_bytes(raw_bytes, salt, scrub_ips=scrub_ips)
    return _scrub


def scrub_for_upload(raw, app_support, log_name, home=None):
    """Scrub a log's bytes on their way to OnionHome.

    Applies what a RotatingLog applies over a file's life — the home
    directory becomes ``~`` (at write time there) and :func:`redact_bytes`
    (at rotation there) — with IPs pseudonymized only for visitor-facing
    logs (:func:`scrubs_ips` on *log_name*).
    """
    if home is None:
        home = os.path.expanduser("~")
    text = raw.decode("utf-8", errors="replace")
    if home and home != os.sep:
        text = text.replace(home, "~")
    scrub_ips = scrubs_ips(log_name)
    salt = get_daily_salt(app_support) if scrub_ips else b""
    return redact_text(text, salt, scrub_ips=scrub_ips).encode("utf-8")

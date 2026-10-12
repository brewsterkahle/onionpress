"""One-time admin auto-login links that never sit on a command line.

The menubar (Mac), the tray and `onionpress dashboard` (Linux) open the
user's site already logged in as its administrator: they store a random
token in WordPress for TOKEN_TTL seconds and open the site with
`?op_login=<token>`, which the onionpress-auto-login.php mu-plugin trades
once for an admin session. Whoever presents the token first in that window
is the admin, so it must never appear in a process's argv: `ps` (macOS) and
/proc/<pid>/cmdline (Linux) show every user's command lines, and several
users can share one Mac.

- mint() hands the token to a fixed `wp eval` PHP (MINT_PHP) on stdin.
- On a Mac the URL reaches the browser through LaunchServices, called from
  inside the menubar process (browser.open_url), not through `open`.
- On Linux a browser takes URLs only on its command line, so it is given
  the path of a 0600 page that redirects to the URL (write_redirect_page).
- A token is bound to where it is redeemed: a "local" token only over
  localhost and never through Tor, an "onion" token only on a .onion host
  (the mu-plugin checks).
"""

import html
import os
import secrets
import stat
import subprocess
import tempfile
import time
from typing import Callable, Optional
from urllib.parse import urlsplit

from . import key_manager

# The mu-plugin honours a token this many seconds after it is minted. MINT_PHP
# spells the number out (it is fixed code); a test keeps the two equal.
TOKEN_TTL = 120

SCOPES = ("local", "onion")
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")

# Reads "<token>\n<scope>" from stdin and stores the token network-wide (a
# site transient, so a subsite URL finds it too) for the first administrator.
# The token never appears in this code or in wp-cli's arguments. No single
# quotes and no "$" inside double-quoted strings, so it could sit in a shell
# function's single quotes like setup_logic.WP_SET_PASSWORD_PHP.
MINT_PHP = r'''
$in = explode("\n", file_get_contents("php://stdin"));
$token = isset($in[0]) ? trim($in[0]) : "";
$scope = isset($in[1]) ? trim($in[1]) : "";
if (!preg_match("/^[A-Za-z0-9_-]{32,64}\z/", $token) || !in_array($scope, array("local", "onion"), true)) { fwrite(STDERR, "Error: malformed token or scope\n"); exit(1); }
$uid = 0;
foreach (get_users(array("role" => "administrator", "orderby" => "ID", "order" => "ASC", "number" => 1, "fields" => "ID")) as $id) { $uid = (int) $id; }
if (!$uid && is_multisite()) { foreach (get_super_admins() as $login) { $u = get_user_by("login", $login); if ($u && (!$uid || $u->ID < $uid)) { $uid = (int) $u->ID; } } }
if (!$uid) { fwrite(STDERR, "Error: no administrator to log in as\n"); exit(1); }
set_site_transient("op_login_" . $token, array("uid" => $uid, "scope" => $scope), 120);
echo "Success: login token stored\n";
'''


def scope_for(url: str) -> Optional[str]:
    """The scope a token for *url* gets: "local" for a localhost URL, "onion"
    for one whose host is a v3 onion address, None for anything else (no
    token is minted). The mu-plugin redeems an "onion" token only on such a
    host, so a looser test here would only mint tokens no one can use."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return None
    if host in LOCAL_HOSTS:
        return "local"
    if key_manager.is_valid_onion_address(host):
        return "onion"
    return None


def new_token() -> str:
    """43 URL-safe characters (256 random bits)."""
    return secrets.token_urlsafe(32)


def add_token(url: str, token: str) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}op_login={token}"


def mint(scope: str, *, docker_bin: str = "docker", env=None,
         container: str = "onionpress-wordpress", timeout: int = 15,
         log_func: Optional[Callable[[str], None]] = None) -> Optional[str]:
    """Store a fresh token for *scope* in WordPress; return it, or None.

    The token goes to MINT_PHP on stdin (`docker exec -i`): nothing that
    names it is ever an argument. Failures are logged without the argv.
    """
    if scope not in SCOPES:
        raise ValueError(f"unknown auto-login scope {scope!r}")
    token = new_token()
    try:
        result = subprocess.run(
            [docker_bin, "exec", "-i", container,
             "wp", "eval", MINT_PHP, "--allow-root"],
            input=f"{token}\n{scope}\n",
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, env=env,
        )
    except (subprocess.SubprocessError, OSError) as e:
        if log_func:
            log_func(f"Auto-login token error: {type(e).__name__}")
        return None
    if result.returncode != 0:
        if log_func:
            log_func(f"Auto-login token failed: {(result.stderr or '').strip()[-200:]}")
        return None
    return token


def login_url(base_url: str, **mint_kwargs) -> Optional[str]:
    """*base_url* with a freshly minted token, or None when no token could
    be minted (the caller opens *base_url* without auto-login)."""
    scope = scope_for(base_url)
    if scope is None:
        return None
    token = mint(scope, **mint_kwargs)
    return add_token(base_url, token) if token else None


# ─── Linux: the redirect page ────────────────────────────────────────────

# Under a non-hidden top-level directory of $HOME: snap browsers (Ubuntu's
# Firefox and Chromium) can read nothing in /tmp, $XDG_RUNTIME_DIR or a
# hidden top-level directory. Not mounted into any container (only
# Creations/My Creations is).
REDIRECT_DIR = os.path.join(os.path.expanduser("~"), "OnionPress", ".auto-login")
REDIRECT_PREFIX = "login-"

_PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="referrer" content="no-referrer">
<meta http-equiv="refresh" content="0;url={url}">
<title>OnionPress</title></head>
<body><p><a href="{url}">Continue to your site</a></p></body></html>
"""


def _private_dir(directory: str) -> None:
    """Create *directory* 0700, or make sure an existing one is a real
    directory of ours and 0700 (not a symlink planted by someone else)."""
    os.makedirs(directory, mode=0o700, exist_ok=True)
    st = os.lstat(directory)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise OSError(f"{directory} is not a directory owned by this user")
    if stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(directory, 0o700)


def sweep_redirect_pages(directory: str = REDIRECT_DIR,
                         max_age: float = 2 * TOKEN_TTL) -> None:
    """Delete pages a crashed or killed caller left behind."""
    try:
        names = os.listdir(directory)
    except OSError:
        return
    cutoff = time.time() - max_age
    for name in names:
        if not (name.startswith(REDIRECT_PREFIX) and name.endswith(".html")):
            continue
        path = os.path.join(directory, name)
        try:
            if os.lstat(path).st_mtime < cutoff:
                os.unlink(path)
        except OSError:
            pass


def write_redirect_page(url: str, directory: str = REDIRECT_DIR) -> str:
    """Write a page that sends the browser on to *url*; return its path.

    The file is 0600 in a 0700 directory and its name is random, never the
    token: the path is what goes on the browser's command line.
    """
    _private_dir(directory)
    sweep_redirect_pages(directory)
    fd, path = tempfile.mkstemp(prefix=REDIRECT_PREFIX, suffix=".html",
                                dir=directory)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(_PAGE.format(url=html.escape(url, quote=True)))
    return path


def remove_later(path: str, delay: float = TOKEN_TTL) -> None:
    """Delete *path* after *delay* seconds, from a detached process so it
    happens even if the caller exits first (`onionpress dashboard`)."""
    try:
        subprocess.Popen(
            ["sh", "-c", 'sleep "$1"; rm -f -- "$2"', "sh", str(int(delay)), path],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except OSError:
        pass  # sweep_redirect_pages removes it next time

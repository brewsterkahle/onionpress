"""Shared first-run setup logic for OnionPress.

Used by:
  - linux/onionpress-tray (GTK setup dialog)
  - src/onionpress/setup_window.py / menubar.py (macOS AppKit dialog)

Keeps step definitions, name utilities, and provisioning in one place
so both platforms stay in sync without copy-paste.
"""

import os
import re
import subprocess
from typing import Callable, Optional

from onionpress import key_manager, onion_hostname, wp_files

try:
    from onionpress import onionnames_client as _onc
except ImportError:
    _onc = None

# ─── Branded installs ────────────────────────────────────────────────────────

# These onion addresses are OnionPress-operated sites (the directory hub and
# the main landing site). They keep blog_id=1 as the canonical public surface
# rather than creating a personal subsite at /<onionname>/.
_BRANDED_ONIONS: frozenset[str] = frozenset({
    "op2homeiwjb4fdqnfkj5kbokvcee45zpk2pwgvpz5rrkanp5qqwxzbyd.onion",  # onionpress.org
    "oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion",  # OnionHeaven
})


def _site_address(onion_addr, log) -> str:
    """*onion_addr* if it is a whole, well-formed onion address, else
    "localhost" (what callers pass when Tor has not written one yet).

    The address becomes WordPress's site URL (wp core install --url) and
    the admin's user_url, and callers read it raw from Tor's hostname file
    or from status.json.
    """
    if onion_addr == "localhost" or key_manager.is_valid_onion_address(onion_addr):
        return onion_addr
    if onion_addr:
        log("WARNING: the onion address is not a well-formed v3 address; "
            "using localhost for the site URL")
    return "localhost"


# ─── Name utilities ──────────────────────────────────────────────────────────

def suggest_onionname(lang: str = "en_US") -> Optional[str]:
    """Return a random adjective-noun onionname suggestion, or None."""
    if _onc is None:
        return None
    try:
        return _onc.suggest_name_local(lang) or _onc.suggest_name_local("en_US")
    except Exception:
        return None


# The onionname charset/length rule, kept here as well as in
# onionnames_client so it holds even when that module is missing or raises.
# The name becomes a wp-cli argument, a URL path and a line in
# ~/.onionpress/config, so nothing outside this set may get through.
_ONIONNAME_MIN_LEN = 5
_ONIONNAME_MAX_LEN = 40
_ONIONNAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*[A-Za-z0-9]")
_ONIONNAME_ALL_NUMERIC_RE = re.compile(r"[0-9]+")


def _local_onionname_rule(name) -> tuple[bool, str]:
    if not isinstance(name, str) or not name:
        return False, "empty"
    if len(name) < _ONIONNAME_MIN_LEN:
        return False, "too_short"
    if len(name) > _ONIONNAME_MAX_LEN:
        return False, "too_long"
    if not _ONIONNAME_RE.fullmatch(name):
        return False, "invalid_chars"
    if _ONIONNAME_ALL_NUMERIC_RE.fullmatch(name):
        return False, "all_numeric"
    return True, ""


def validate_onionname(name: str) -> tuple[bool, str]:
    """Validate an onionname.

    Returns (ok, reason). reason is "" on success, or one of:
      "too_short", "too_long", "invalid_chars", "all_numeric", "empty"

    Fails closed: the local charset/length rule always applies, and the
    onionnames_client check runs on top of it when that module is present.
    """
    ok, reason = _local_onionname_rule(name)
    if not ok or _onc is None:
        return ok, reason
    try:
        ok, reason = _onc.validate_name(name)
        return ok, (reason or "")
    except Exception:
        # The local rule above already passed.
        return True, ""


def onionname_error_message(reason: str) -> str:
    """The text a setup front end shows for validate_onionname's *reason*.

    Built from the limits the rule enforces, so the message cannot drift
    from them (the Linux tray and `onionpress setup` once said 2 and 63
    while the rule said 5 and 40).
    """
    return {
        "empty": "Cannot be empty",
        "too_short": f"Too short (min {_ONIONNAME_MIN_LEN} characters)",
        "too_long": f"Too long (max {_ONIONNAME_MAX_LEN} characters)",
        "invalid_chars": "Letters, digits, '.', '_' or '-' only; "
                         "start and end with a letter or digit",
        "all_numeric": "Cannot be all numbers",
    }.get(reason, "Invalid username")


def _refuse_invalid_onionname(onionname, log) -> bool:
    """True (after logging why) when *onionname* must not be installed.

    Every setup front end validates before it gets here; this keeps a
    name that slipped past one of them out of WordPress and the config.
    """
    ok, reason = validate_onionname(onionname)
    if not ok:
        log(f"ERROR: onionname {onionname!r} is invalid ({reason}); "
            "not installing")
    return not ok


def default_site_title() -> str:
    """Generate a site title from the OS username."""
    try:
        import pwd
        entry = pwd.getpwuid(os.getuid())
        full = entry.pw_gecos.split(",")[0].strip() if entry.pw_gecos else ""
        if not full:
            full = entry.pw_name
        if full:
            initials = "".join(w[0].upper() for w in full.split() if w)
            if initials:
                return f"{initials} OnionPress"
    except Exception:
        pass
    try:
        import subprocess
        result = subprocess.run(["id", "-F"], capture_output=True, text=True, timeout=5)
        full = result.stdout.strip()
        if full:
            initials = "".join(w[0].upper() for w in full.split() if w)
            if initials:
                return f"{initials} OnionPress"
    except Exception:
        pass
    return "My OnionPress Site"


# ─── First post ──────────────────────────────────────────────────────────────

# WordPress seeds every new site with a "Hello world!" post (ID 1). A brand
# new OnionPress site replaces it with this onboarding post (#193): it tells
# the author what the site is for and where to start, and it is the only
# thing a visitor sees until the first real post. Block markup so it opens
# cleanly in the block editor; the author is free to edit or delete it.
#
# WP_DEFAULT_POST_TITLE is the exact string core writes
# (wp_install_defaults → __('Hello world!')); install_welcome_post only
# touches post 1 while that is still its title.
WP_DEFAULT_POST_TITLE = "Hello world!"
WP_DEFAULT_COMMENT_AUTHOR = "A WordPress Commenter"

WELCOME_POST_TITLE = "Welcome to your OnionPress site"
WELCOME_POST_SLUG = "welcome-to-onionpress"
WELCOME_POST_CONTENT = """\
<!-- wp:paragraph -->
<p>This site is yours. It runs on your own computer as a Tor onion service, so no company can close your account, bury your writing, or decide who gets to read it. There is no feed here and no algorithm, just you and the people who choose to visit.</p>
<!-- /wp:paragraph -->

<!-- wp:heading -->
<h2 class="wp-block-heading">What you can do here</h2>
<!-- /wp:heading -->

<!-- wp:list -->
<ul class="wp-block-list"><!-- wp:list-item -->
<li><strong>Write.</strong> Click <strong>New Post</strong> in the bar at the top and start typing. The moment you press Publish, it is live at your .onion address.</li>
<!-- /wp:list-item -->

<!-- wp:list-item -->
<li><strong>Share it.</strong> In the OnionPress menubar, choose <strong>Open in Tor Browser</strong> to see your site the way visitors do, and copy your address or onionname to pass along.</li>
<!-- /wp:list-item -->

<!-- wp:list-item -->
<li><strong>Follow and be followed.</strong> Other OnionPress sites can follow you by onionname, and you can follow them from the <strong>Following</strong> section of your settings. Sites link to sites, with no platform in between.</li>
<!-- /wp:list-item -->

<!-- wp:list-item -->
<li><strong>Keep what you make.</strong> Drop files into <strong>My Creations</strong> in your OnionPress folder to publish them here, and use <strong>Backup</strong> in the menubar for an encrypted copy of your whole site that you can restore anywhere.</li>
<!-- /wp:list-item --></ul>
<!-- /wp:list -->

<!-- wp:heading -->
<h2 class="wp-block-heading">Make it yours</h2>
<!-- /wp:heading -->

<!-- wp:paragraph -->
<p>This post is only a starting point. Edit it, delete it, or leave it up as a reminder of why you set this up: to publish freely, keep what you write, and own it.</p>
<!-- /wp:paragraph -->
"""


def install_welcome_post(
    *,
    blog_url: str,
    docker_bin: str = "docker",
    docker_env: dict | None = None,
    log_func: Callable[[str], None] | None = None,
) -> bool:
    """Replace WordPress's stock "Hello world!" post on a freshly created
    site with the OnionPress onboarding post (#193).

    Safe to call on any site: post 1 is only rewritten while its title is
    still the WordPress default, so a site the author has already touched,
    a restored backup, or an upgraded install is never altered. Returns
    True when the welcome post is in place (or post 1 was left alone on
    purpose), False when the replacement itself failed.
    """
    def log(msg: str) -> None:
        if log_func:
            log_func(msg)

    def wp(*args, timeout: int = 30) -> subprocess.CompletedProcess:
        return wp_files.wp_cli(*args, docker_bin=docker_bin, env=docker_env,
                               timeout=timeout)

    url_arg = f"--url={blog_url}"
    r = wp("post", "get", "1", "--field=post_title", url_arg)
    title = (r.stdout or "").strip()
    if r.returncode != 0 or title != WP_DEFAULT_POST_TITLE:
        log(f"Post 1 is not the stock '{WP_DEFAULT_POST_TITLE}' post "
            f"(got {title!r}) — leaving it alone")
        return True

    r = wp("post", "update", "1",
           f"--post_title={WELCOME_POST_TITLE}",
           f"--post_name={WELCOME_POST_SLUG}",
           f"--post_content={WELCOME_POST_CONTENT}",
           "--post_status=publish",
           url_arg)
    if r.returncode != 0:
        log(f"WARNING: welcome post update failed: {r.stderr.strip()[:200]}")
        return False
    log(f"Welcome post installed in place of '{WP_DEFAULT_POST_TITLE}'")

    # Core also seeds a sample comment on post 1. A fake comment under the
    # welcome post is confusing, so drop it — but only the stock one.
    r = wp("comment", "get", "1", "--field=comment_author", url_arg)
    if r.returncode == 0 and (r.stdout or "").strip() == WP_DEFAULT_COMMENT_AUTHOR:
        wp("comment", "delete", "1", "--force", url_arg)
        log("Stock sample comment removed")
    return True


# ─── Admin password ──────────────────────────────────────────────────────────

# The admin password never rides a host command line: `ps` shows every
# user's argv (several users can run OnionPress on one Mac), and a subprocess
# error (TimeoutExpired, CalledProcessError) quotes the whole argv, so it
# would reach the log too. WordPress is installed without --admin_password
# (wp-cli then generates one, printed only on the stdout nobody keeps) and
# the user's password is set by this fixed PHP, which reads
# "<login>\n<password>" from stdin (`docker exec -i`).
#
# trim(), as wp_install() and wp_authenticate() do. wp_install() flags the
# generated password with default_password_nag; the user chose this one.
# A new password ends the user's sessions, which is fine at setup.
#
# No single quotes in it: the bash launchers' wp_set_user_password carries
# the same code in single quotes (test-enforced). No "$" inside its PHP
# double-quoted strings either, which would interpolate.
WP_SET_PASSWORD_PHP = r'''
$in = explode("\n", file_get_contents("php://stdin"), 2);
$u = get_user_by("login", trim($in[0]));
$pw = isset($in[1]) ? trim($in[1]) : "";
if (!$u || $pw === "") { fwrite(STDERR, "Error: no such user, or an empty password\n"); exit(1); }
wp_set_password($pw, $u->ID);
delete_user_meta($u->ID, "default_password_nag");
echo "Success: password set for ", $u->user_login, "\n";
'''


def _run_wp(docker_bin, args, *, input=None, env=None, timeout=60):
    """Run wp-cli in onionpress-wordpress as www-data (wp_files.wp_cli_args);
    with *input*, `docker exec -i` and *input* on stdin.

    A timeout comes back as a failed CompletedProcess (returncode 124) whose
    stderr names the wp command, not the argv: a TimeoutExpired's message
    quotes the whole command line, and callers log what they catch.
    """
    try:
        return wp_files.wp_cli(*args, docker_bin=docker_bin, env=env,
                               input=input, timeout=timeout)
    except subprocess.TimeoutExpired:
        name = " ".join(a for a in list(args)[:2] if a.isalpha())
        return subprocess.CompletedProcess(
            [docker_bin] + wp_files.wp_cli_args(*args, stdin=input is not None),
            124, "", f"wp {name} timed out after {timeout}s")


def set_wp_user_password(login, password, *, docker_bin="docker", env=None,
                         timeout=60):
    """Set WordPress user *login*'s password. The password reaches PHP on
    stdin only (WP_SET_PASSWORD_PHP); it is never part of any argv or code.
    Returns the CompletedProcess; returncode 0 on success."""
    return _run_wp(docker_bin, ["eval", WP_SET_PASSWORD_PHP],
                   input=f"{login}\n{password}", env=env, timeout=timeout)


# ─── Provisioning ────────────────────────────────────────────────────────────

def provision_primary_subsite(
    *,
    onionname: str,
    site_title: str,
    onion_addr: str = "localhost",
    docker_bin: str = "docker",
    docker_env: dict | None = None,
    log_func: Callable[[str], None] | None = None,
) -> bool:
    """Create the primary subsite at /<onionname>/ and configure it.

    Shared between Mac and Linux. Called after WordPress is installed
    (either via wp core install on Mac or wp core multisite-install on
    Linux). Idempotent: if the subsite already exists the create step
    is a no-op.

    Steps:
      1. Branded installs: set onionpress_root_site=yes, return early
      2. wp site create --slug=<onionname>
      3. wp user add-role <onionname> administrator on the subsite
      4. Resolve blog_id and set primary_blog usermeta
      5. wp theme activate onionpress on the subsite
      6. wp option update limit_login_show_top_bar_menu_item 0
      7. install_welcome_post — swap "Hello world!" for the onboarding post

    Returns True (best-effort — individual failures are logged but do
    not block the wizard).
    """
    def log(msg: str) -> None:
        if log_func:
            log_func(msg)

    def wp(*args, timeout: int = 30,
           stdin: str | None = None) -> subprocess.CompletedProcess:
        return wp_files.wp_cli(*args, docker_bin=docker_bin, env=docker_env,
                               input=stdin, timeout=timeout)

    onion_addr = _site_address(onion_addr, log)

    # Branded installs (OnionHeaven hub, onionpress.org) keep blog_id=1
    # as the canonical public surface — no personal subsite.
    if onion_addr in _BRANDED_ONIONS:
        log(f"Branded install ({onion_addr}): keeping blog_id=1 as canonical site")
        wp("option", "update", "onionpress_root_site", "yes",
           "--url=http://localhost/")
        return True

    # 1. Create the subsite at /<onionname>/
    log(f"Creating subsite /{onionname}/")
    r = wp("site", "create",
           f"--slug={onionname}",
           f"--title={site_title or onionname}",
           "--email=admin@onionpress.local",
           "--url=http://localhost/",
           timeout=60)
    if r.returncode != 0:
        stderr = (r.stderr or "")[-300:]
        if "already exists" in stderr.lower():
            log(f"Subsite /{onionname}/ already exists — ok")
        else:
            log(f"WARNING: wp site create failed: {stderr.strip()[:200]}")
            return True  # best-effort; don't block the wizard

    # 2. Grant administrator role on the subsite
    wp("user", "add-role", onionname, "administrator",
       f"--url=http://localhost/{onionname}/")
    log(f"Administrator role set on /{onionname}/")

    # 3. Set user_url to the per-user page (nice-to-have; skipped if no address yet)
    if onion_addr and onion_addr != "localhost":
        wp("user", "update", onionname,
           f"--user_url=http://{onion_addr}/{onionname}/")

    # 4. Resolve blog_id and point primary_blog at the subsite so that
    #    wp-admin's "+ New", "My Sites", and redirect defaults all go to
    #    /<onionname>/wp-admin/ instead of the network root. The name
    #    reaches PHP on stdin; the code itself never changes.
    r = wp("eval",
           "echo get_blog_id_from_url('localhost',"
           " '/' . trim(file_get_contents('php://stdin')) . '/');",
           stdin=onionname)
    lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
    blog_id = lines[-1] if lines else ""
    if blog_id.isdigit() and int(blog_id) > 0:
        wp("user", "meta", "update", onionname, "primary_blog", blog_id)
        log(f"primary_blog set to blog_id={blog_id}")
    else:
        log(f"WARNING: could not resolve blog_id for /{onionname}/ "
            f"(got: {blog_id!r}) — primary_blog not updated")

    # 5. Activate OnionPress theme on the subsite. wp site create assigns
    #    WP_DEFAULT_THEME (twentytwentyfive); without this the subsite is
    #    unthemed and root-redirect.php bouncing / → /<onionname>/ means
    #    the user never sees OnionPress at all.
    wp("theme", "activate", "onionpress",
       f"--url=http://localhost/{onionname}/")
    log(f"OnionPress theme activated on /{onionname}/")

    # 6. Hide LLAR's top-bar badge on the subsite (Tor-only sites see
    #    mostly bot traffic; the badge is noise rather than signal).
    wp("option", "update", "limit_login_show_top_bar_menu_item", "0",
       f"--url=http://localhost/{onionname}/")

    # 7. First post. `wp site create` seeded the subsite with WordPress's
    #    "Hello world!"; a new author should instead find a post that says
    #    what this site is for and where to start writing (#193). Guarded
    #    on the stock title, so a re-run on an existing subsite is a no-op.
    install_welcome_post(
        blog_url=f"http://localhost/{onionname}/",
        docker_bin=docker_bin,
        docker_env=docker_env,
        log_func=log_func,
    )

    log(f"Subsite /{onionname}/ ready")
    return True


def start_bundled_plugin_setup(launcher_bin: str, log: Callable[[str], None]) -> bool:
    """Start `<launcher_bin> setup-bundled-plugins` in the background: the
    launcher configures and activates Cache Enabler and the Link Fixer
    (setup_bundled_plugins). Its start skipped them, WordPress not being
    installed then, and the Link Fixer is activated site by site, so this
    runs once setup has made the user's site. A minute of wp-cli calls
    (more on a Raspberry Pi) the user need not wait for; the launcher logs
    it, and its next start does it otherwise."""
    try:
        subprocess.Popen(
            [launcher_bin, "setup-bundled-plugins"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except OSError as e:
        log(f"WARNING: plugin setup not started ({e}); it runs at the next start")
        return False
    log("Cache Enabler and Link Fixer setup started in background")
    return True


# The Mac menubar shows the welcome screen until this file exists in the
# data dir (issue #374). Every setup that installs WordPress writes it when
# it completes: this function (the Mac setup window, the Linux paths), the
# Mac launcher's `onionpress setup` (bash: touch), and the Mac daemon once
# more when the site is fully operational. The name is a literal in
# src/menubar.py, src/onionpress/daemon.py and app/MacOS/onionpress;
# tests/test_setup_complete_marker.py keeps them equal.
SETUP_COMPLETE_MARKER = ".setup_complete"


def mark_setup_complete(data_dir: str, log: Callable[[str], None] | None = None) -> bool:
    """Write the setup-complete marker in *data_dir*. False, logged, when
    it cannot be written (the setup itself has succeeded by then)."""
    path = os.path.join(data_dir, SETUP_COMPLETE_MARKER)
    try:
        os.makedirs(data_dir, mode=0o700, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("1")
    except OSError as e:
        if log:
            log(f"WARNING: could not write {path}: {e}")
        return False
    return True


# Who installed this WordPress (issue #464). Until WordPress is installed,
# its own installer (wp-admin/install.php) takes the first visitor's choice
# of admin account, and a setup that found WordPress installed used to
# adopt it: rename and re-password the existing admin, keeping whatever
# else that visitor added. Every OnionPress install writes this marker
# beside .setup_complete, holding user 1's registration time as WordPress
# records it; a setup that finds WordPress installed provisions it only
# when the marker matches the live install (or the operator says
# --adopt-existing). The bash setups (`onionpress setup` in both
# launchers) write the same file the same way; a test keeps them equal.
INSTALLED_BY_ONIONPRESS_MARKER = ".wordpress-installed-by-onionpress"
INSTALLED_BY_UNKNOWN = "unknown"
# user_registered as WordPress stores it; the launchers' bash check is the
# same shape (note_wordpress_installed_by_onionpress).
_USER_REGISTERED_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}$")
FOREIGN_INSTALL_MSG = (
    "ERROR: this WordPress was not installed by OnionPress (no "
    f"{INSTALLED_BY_ONIONPRESS_MARKER} marker for it): someone may have "
    "reached WordPress's own installer before setup ran. Check its users "
    "and plugins, or reset and set up again. To keep it anyway, run "
    "`onionpress provision --adopt-existing`."
)


def _wp_user_1_registered(docker_bin: str, docker_env: dict | None = None) -> str:
    """User 1's `user_registered` as WordPress stores it ('' when it
    cannot be read)."""
    r = _run_wp(docker_bin, ("user", "get", "1", "--field=user_registered"),
                env=docker_env, timeout=30)
    if r.returncode != 0:
        return ""
    value = (r.stdout or "").split("\n", 1)[0].strip()
    return value if _USER_REGISTERED_RE.fullmatch(value) else ""


def mark_wordpress_installed_by_onionpress(
    data_dir: str, docker_bin: str = "docker",
    log: Callable[[str], None] | None = None,
    docker_env: dict | None = None,
) -> bool:
    """Record that OnionPress installed the WordPress now in the container.
    False, logged, when the marker cannot be written."""
    stamp = _wp_user_1_registered(docker_bin, docker_env) or INSTALLED_BY_UNKNOWN
    path = os.path.join(data_dir, INSTALLED_BY_ONIONPRESS_MARKER)
    try:
        os.makedirs(data_dir, mode=0o700, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(stamp + "\n")
    except OSError as e:
        if log:
            log(f"WARNING: could not write {path}: {e}")
        return False
    return True


def wordpress_installed_by_onionpress(data_dir: str, docker_bin: str = "docker") -> bool:
    """True when the marker says OnionPress installed the WordPress in the
    container: its stamp is user 1's registration time (or 'unknown', a
    stamp the install could not read), never a stale marker of an earlier
    install."""
    path = os.path.join(data_dir, INSTALLED_BY_ONIONPRESS_MARKER)
    try:
        with open(path, encoding="utf-8") as f:
            stamp = f.read(256).strip()
    except OSError:
        return False
    if stamp == INSTALLED_BY_UNKNOWN:
        return True
    if not _USER_REGISTERED_RE.fullmatch(stamp):
        return False
    live = _wp_user_1_registered(docker_bin)
    return bool(live) and live == stamp


def install_fresh_wordpress(
    *,
    site_title: str,
    onionname: str,
    password: str,
    language: str = "en_US",
    onion_addr: str = "localhost",
    docker_bin: str = "docker",
    docker_env: dict | None = None,
    launcher_bin: str | None = None,
    log_func: Callable[[str], None] | None = None,
    data_dir: str | None = None,
) -> bool:
    """Install WordPress from scratch with the user's chosen credentials.

    The cross-platform replacement for the old "bash auto-bootstraps with a
    random password, wizard then renames the user" dance on Linux: now the
    GTK SetupDialog, Mac SetupWindow, and `onionpress setup` SSH path all
    funnel through this single function with the user-typed creds, as
    does the browser extension proxy's /setup form (onion_proxy).

    *docker_env* is the environment docker runs with (None: this
    process's); the launcher hop and the background archive.org key
    fetch use this process's environment.

    Steps:
      1. wp core install (admin_user=onionname), then set the password on
         stdin (set_wp_user_password) — never on the command line
      2. wp user update — set user_url to per-user subsite path
      3. Set blogname (site title) explicitly — wp core install accepts it
         but some hardened WP images sandbox the option write.
      4. Set language (best-effort).
      5. Invoke `<launcher_bin> provision-post-install` — runs multisite
         conversion, sunrise.php + mu-plugins, theme, permission fixes.
         The bash subcommand stays bash for now; planned Python port in
         src/onionpress/multisite.py as a follow-up.
      6. provision_primary_subsite() — per-user subsite at /<onionname>/,
         then start_bundled_plugin_setup() (Cache Enabler, Link Fixer).
      7. Mark onboarded; persist onionname to ~/.onionpress/config.

    Returns True on success (best-effort — individual sub-steps may log
    warnings without failing the overall install), False when wp core
    install or setting the admin password failed. An onionname that fails
    validate_onionname() is refused up front with False.
    """
    def log(msg: str) -> None:
        if log_func:
            log_func(msg)

    def wp(*args, timeout: int = 60) -> subprocess.CompletedProcess:
        return _run_wp(docker_bin, args, env=docker_env, timeout=timeout)

    if _refuse_invalid_onionname(onionname, log):
        return False
    onion_addr = _site_address(onion_addr, log)

    log(f"Installing WordPress: title={site_title!r} onionname={onionname!r}")

    # 1. wp core install — fails if already installed, which is fine here
    # because callers (SetupDialog, CLI setup) branch on wp_installed
    # before choosing this path. No --admin_password: wp-cli generates one
    # (and prints it on stdout, which is dropped), and the user's password
    # goes in on stdin right after.
    r = wp(
        "core", "install",
        f"--url=http://{onion_addr}",
        f"--title={site_title}",
        f"--admin_user={onionname}",
        "--admin_email=admin@onionpress.local",
        "--skip-email",
    )
    if r.returncode != 0:
        log(f"ERROR: wp core install failed: {r.stderr.strip()[:200]}")
        return False
    log("WordPress installed")

    # The rest of setup still runs if this fails (the menubar's auto-login
    # reaches wp-admin without a password), but the result is False so the
    # caller reports it.
    password_set = True
    r = set_wp_user_password(onionname, password, docker_bin=docker_bin,
                             env=docker_env)
    if r.returncode != 0:
        password_set = False
        log(f"ERROR: setting the admin password failed: "
            f"{r.stderr.strip()[:200]}")
    else:
        log("Admin password set")

    if data_dir is None:
        data_dir = os.path.join(os.path.expanduser("~"), ".onionpress")
    # OnionPress installed this one (issue #464): what a later setup
    # that finds WordPress installed checks before adopting it.
    mark_wordpress_installed_by_onionpress(data_dir, docker_bin, log,
                                           docker_env=docker_env)

    # 2. Per-user subsite URL on the user's profile — matches Mac UX where
    # the "Website" link on profile.php points somewhere useful.
    r = wp(
        "user", "update", onionname,
        f"--user_url=http://{onion_addr}/{onionname}/",
        "--skip-email",
    )
    if r.returncode != 0:
        log(f"WARNING: user_url update failed: {r.stderr.strip()[:100]}")

    # 3. Site title — wp core install sets it, but re-asserting is cheap
    # and survives images that mangle the option during install.
    wp("option", "update", "blogname", site_title)

    # 4. Language (best-effort)
    if language and language != "en_US":
        r = wp("language", "core", "install", language)
        if r.returncode == 0:
            wp("option", "update", "WPLANG", language)
            log(f"Language set: {language}")
        else:
            log(f"Language install skipped: {r.stderr.strip()[:80]}")

    # 5. Post-install steps via the platform's bash launcher. Stays a
    # subprocess hop until the bash multisite-init is ported to Python.
    if launcher_bin is None:
        log("WARNING: launcher_bin not provided — skipping post-install steps")
    else:
        try:
            r = subprocess.run(
                [launcher_bin, "provision-post-install"],
                capture_output=True, text=True, timeout=180,
            )
            if r.returncode == 0:
                log("Post-install (multisite, mu-plugins, theme) complete")
            else:
                log(f"WARNING: provision-post-install rc={r.returncode}: "
                    f"{(r.stderr or '').strip()[:200]}")
        except subprocess.TimeoutExpired:
            log("WARNING: provision-post-install timed out")
        except Exception as e:
            log(f"WARNING: provision-post-install failed: {e}")

    # 6. Per-user subsite (shared with the legacy provision_existing path)
    provision_primary_subsite(
        onionname=onionname,
        site_title=site_title,
        onion_addr=onion_addr,
        docker_bin=docker_bin,
        docker_env=docker_env,
        log_func=log_func,
    )

    # 6b. Cache Enabler and the Link Fixer, now that every site exists.
    if launcher_bin is not None:
        start_bundled_plugin_setup(launcher_bin, log)

    # 7. Mark onboarded + persist onionname.
    # `wp option update` writes wp_options (per-blog). The
    # onionpress-onboarding mu-plugin reads via get_site_option(), which
    # on multisite goes to wp_sitemeta (network-wide). Set via
    # update_site_option() so the right table is hit on both single-site
    # and multisite — single-site falls back to update_option in core.
    wp("eval", "update_site_option('onionpress_onboarded', time());")
    log("Marked as onboarded")

    if data_dir is None:
        data_dir = os.path.join(os.path.expanduser("~"), ".onionpress")
    _write_config(data_dir, "ONIONNAME", onionname, log)
    if password_set:
        mark_setup_complete(data_dir, log)

    # 8. Fetch archive.org S3 keys in the background.
    # Tor-routed login can take 10-30s, so we don't block setup
    # completion on it. The wp_option onionpress_archive_s3_access will
    # appear once it lands — the Wayback sweep plugin polls for it.
    # Without this, Mac SetupWindow and Linux tray SetupDialog never
    # fetched the keys (only the bash `onionpress setup` subcommand
    # did, via its explicit `op_py ensure-archive-s3-keys` call at
    # linux/onionpress:2351). The scrub verify's Wayback warning was
    # the symptom.
    def _fetch_s3_keys():
        try:
            from . import multisite
            multisite.ensure_archive_s3_keys(
                docker_bin=docker_bin, log_func=log_func)
        except subprocess.SubprocessError as e:
            # Not {e}: a subprocess error's message quotes the argv.
            log(f"WARNING: archive.org S3 key fetch failed: {type(e).__name__}")
        except Exception as e:
            log(f"WARNING: archive.org S3 key fetch failed: {e}")

    import threading
    threading.Thread(target=_fetch_s3_keys, daemon=True).start()
    log("Archive.org S3 key fetch started in background")

    return password_set


def provision_existing_wordpress(
    *,
    site_title: str,
    onionname: str,
    password: str,
    language: str = "en_US",
    onion_addr: str = "localhost",
    docker_bin: str = "docker",
    log_func: Callable[[str], None] | None = None,
    data_dir: str | None = None,
    adopt_existing: bool = False,
) -> bool:
    """Apply setup choices to an already-installed WordPress and provision
    the primary subsite.

    Used on Linux where the container auto-installs WordPress on first
    boot with a bootstrap 'admin' account. The wizard collects the
    user's preferences and this function applies them via wp-cli, then
    calls provision_primary_subsite() so Linux sites get the same
    /<onionname>/ URL structure as Mac sites.

    Steps:
      1. Set site title (blogname option)
      2. Rename the bootstrap 'admin' account to the user's onionname
      3. Set the password
      4. Set the site language
      5. Create subsite at /<onionname>/, set role, primary_blog, theme
      6. Mark onboarded
      7. Persist onionname to ~/.onionpress/config

    Returns True on success (best-effort — individual failures are
    logged but do not cause False so the wizard is never stuck). An
    onionname that fails validate_onionname() is refused up front with
    False.
    """
    def log(msg: str) -> None:
        if log_func:
            log_func(msg)

    def wp(*args, timeout: int = 30) -> subprocess.CompletedProcess:
        return _run_wp(docker_bin, args, timeout=timeout)

    if _refuse_invalid_onionname(onionname, log):
        return False

    if data_dir is None:
        data_dir = os.path.join(os.path.expanduser("~"), ".onionpress")
    # Only a WordPress OnionPress installed is adopted (issue #464): one
    # installed through wp-admin/install.php is whoever got there first's.
    if not adopt_existing and not wordpress_installed_by_onionpress(data_dir, docker_bin):
        log(FOREIGN_INSTALL_MSG)
        return False
    if adopt_existing:
        log("Adopting the installed WordPress as the operator asked (--adopt-existing)")
        mark_wordpress_installed_by_onionpress(data_dir, docker_bin, log)

    log(f"Provisioning WordPress: title={site_title!r} onionname={onionname!r}")

    # 1. Site title
    r = wp("option", "update", "blogname", site_title)
    if r.returncode == 0:
        log(f"Site title set: {site_title}")
    else:
        log(f"WARNING: blogname update failed: {r.stderr.strip()[:100]}")

    # 2. Rename bootstrap admin to chosen onionname
    r = wp("user", "update", "admin",
           f"--user_login={onionname}",
           f"--display_name={onionname}",
           "--skip-email")
    if r.returncode == 0:
        log(f"Username set: {onionname}")
    else:
        log(f"Username update: {r.stderr.strip()[:100]} (may already be set)")

    # 3. Password — on stdin, never as --user_pass (see WP_SET_PASSWORD_PHP)
    r = set_wp_user_password(onionname, password, docker_bin=docker_bin)
    if r.returncode == 0:
        log("Password updated")
    else:
        r2 = set_wp_user_password("admin", password, docker_bin=docker_bin)
        if r2.returncode == 0:
            log("Password updated (via admin fallback)")
        else:
            log(f"WARNING: password update failed: {r.stderr.strip()[:100]}")

    # 4. Language
    if language and language != "en_US":
        r = wp("language", "core", "install", language)
        if r.returncode == 0:
            wp("option", "update", "WPLANG", language)
            log(f"Language set: {language}")
        else:
            log(f"Language install skipped: {r.stderr.strip()[:80]}")

    # 5. Subsite at /<onionname>/ — same structure as Mac installs
    provision_primary_subsite(
        onionname=onionname,
        site_title=site_title,
        onion_addr=onion_addr,
        docker_bin=docker_bin,
        log_func=log_func,
    )

    # 6. Mark onboarded — service's write_status() picks this up within 30s.
    # Use update_site_option so the network-scoped onboarding mu-plugin
    # (which reads via get_site_option) sees the marker on multisite.
    wp("eval", "update_site_option('onionpress_onboarded', time());")
    log("Marked as onboarded")

    # 7. Persist onionname to config so wp-cli calls and tray can reuse it
    if data_dir is None:
        data_dir = os.path.join(os.path.expanduser("~"), ".onionpress")
    _write_config(data_dir, "ONIONNAME", onionname, log)
    mark_setup_complete(data_dir, log)

    return True


def provision_interactive(data_dir: str | None = None, *,
                          adopt_existing: bool = False) -> bool:
    """Interactive terminal setup for headless / SSH installs.

    Called by `onionpress provision` on systems where WordPress has already
    been auto-installed by the container (systemd path).  Prompts for site
    title, username, and password, then delegates to
    provision_existing_wordpress(), which adopts an installed WordPress
    only when OnionPress installed it or *adopt_existing* says to
    (`onionpress provision --adopt-existing`, issue #464).
    """
    import getpass

    print()
    print("  OnionPress First-Run Setup")
    print("  ==========================")
    print()
    print("  WordPress is running. Choose a site title, username, and password.")
    print()

    default_title = default_site_title()
    raw = input(f"  Site title [{default_title}]: ").strip()
    title = raw if raw else default_title

    suggestion = suggest_onionname() or ""
    while True:
        prompt = f"  Username [{suggestion}]: " if suggestion else "  Username: "
        onionname = input(prompt).strip() or suggestion
        if not onionname:
            print("  Username cannot be empty.")
            continue
        ok, reason = validate_onionname(onionname)
        if ok:
            break
        print(f"  Invalid username: {onionname_error_message(reason)}")

    while True:
        pw = getpass.getpass("  Password: ")
        if len(pw) < 6:
            print("  Password must be at least 6 characters.")
            continue
        pw2 = getpass.getpass("  Confirm password: ")
        if pw != pw2:
            print("  Passwords do not match. Try again.")
            continue
        break

    print()
    print("  Applying settings…")
    log_fn = lambda msg: print(f"  {msg}")
    if _wp_is_installed():
        # Legacy install path — WP was already configured by a previous
        # auto-bootstrap (older Linux installs). Rename + change password.
        ok = provision_existing_wordpress(
            site_title=title,
            onionname=onionname,
            password=pw,
            data_dir=data_dir,
            log_func=log_fn,
            adopt_existing=adopt_existing,
        )
    else:
        # Fresh install path — same as the GTK SetupDialog and Mac
        # SetupWindow now use. Pick up the live onion address so wp
        # core install's --url matches reality.
        onion_addr = _read_onion_address() or "localhost"
        # Prefer the real binary at /opt/onionpress/onionpress over the
        # /usr/local/bin symlink, which install.sh creates late enough
        # that a quick Setup run can miss it. Honor an explicit override.
        launcher_bin = os.environ.get("ONIONPRESS_LAUNCHER_BIN") or next(
            (p for p in (
                "/opt/onionpress/onionpress",
                "/usr/local/bin/onionpress",
            ) if os.path.exists(p)),
            "/usr/local/bin/onionpress",
        )
        ok = install_fresh_wordpress(
            site_title=title,
            onionname=onionname,
            password=pw,
            onion_addr=onion_addr,
            launcher_bin=launcher_bin,
            data_dir=data_dir,
            log_func=log_fn,
        )
    if ok:
        print()
        print("  Setup complete!")
    return ok


def _wp_is_installed() -> bool:
    """Cheap wp-cli probe for the SSH/CLI setup path."""
    try:
        return wp_files.wp_cli("core", "is-installed",
                               timeout=15).returncode == 0
    except Exception:
        return False


def _read_onion_address() -> str:
    try:
        return onion_hostname.read_served_address_with("docker")
    except Exception:
        return ""


def _write_config(data_dir: str, key: str, value: str,
                  log: Callable[[str], None]) -> None:
    """Record KEY=VALUE through config's checked writer (issue #474):
    the key's check, a 0600 file if it is new, the log line and the
    change marker, as `onionpress config set` does them."""
    from onionpress import config as op_config
    try:
        op_config.set_checked_value(data_dir, key, value, log)
    except OSError as e:
        log(f"WARNING: could not write {key} to the config: {e}")


# ---- `onionpress setup`: the launchers' terminal setup (issue #442) ----
#
# The launchers' `setup)` case runs `onionpress setup` (cli._run_setup):
# setup_from_terminal below decides everything and says what the launcher
# has to do after it (SETUP_* exit codes): restart the stack for a backup's
# key, or nothing. Passwords reach it in the environment (the launcher
# moves `--pass`, `--ia-password` and `--from-backup`'s password there),
# never on argv.

SETUP_DONE, SETUP_FAILED, SETUP_ALREADY_INSTALLED, SETUP_RESTART = 0, 1, 2, 10
SETUP_PASSWORD_ENV = "ONIONPRESS_SETUP_PASSWORD"
IA_PASSWORD_ENV = "ONIONPRESS_IA_PASSWORD"
_KEY_FILES = ("hs_ed25519_secret_key", "hs_ed25519_public_key",
              "ks_hs_id.ed25519_expanded_private")


def stage_setup_from_backup(zip_path: str, password: str, data_dir: str,
                            log: Callable[[str], None],
                            say: Callable[[str], None]) -> tuple[bool, str]:
    """`setup --from-backup`'s host side (#442.9): extract the backup
    (either format) into a private temp folder, check it and take the
    address from its key (backup.staged_backup_address, never from
    metadata.json), merge its allowlisted settings into this install's
    config, and put its key in shared/vanity-keys/<address>/ for the next
    start (which the setup flags as a key import). (ok, address): address
    "" when the backup holds no key, and then this install keeps its own; a
    backup without a key never mints one. Refused before anything is copied
    when the backup does not check out. The temp folder always goes."""
    import shutil
    import tempfile
    from onionpress import backup
    staging = tempfile.mkdtemp(prefix="onionpress-setup-")
    try:
        os.chmod(staging, 0o700)
        say("  Extracting config from backup...")
        try:
            backup.extract_backup(zip_path, password, log, staging=staging)
        except Exception as e:
            log(f"Restore: could not extract the backup: {e}")
            say("ERROR: Failed to extract backup (wrong password, corrupt zip or not a "
                "valid OnionPress backup)")
            return False, ""
        try:
            address = backup.staged_backup_address(staging, log)
        except Exception as e:
            log(f"Restore: refusing the backup: {str(e)[:300]}")
            address = None
        if address is None or (address and not key_manager.is_valid_onion_address(address)):
            say("ERROR: Not a valid OnionPress backup (invalid metadata or onion service key)")
            return False, ""
        config_file = os.path.join(data_dir, "config")
        try:
            backup.restore_backup_config(staging, config_file, log, address)
            log("Setup: restored settings from backup")
            say("  Settings restored from backup.")
        except Exception as e:
            log(f"Restore: could not restore settings: {str(e)[:300]}")
        keys = os.path.join(staging, "tor-keys")
        has_key = any(os.path.isfile(os.path.join(keys, n)) for n in _KEY_FILES[0::2])
        if address and has_key and os.path.isfile(os.path.join(staging, "metadata.json")):
            vanity = os.path.join(data_dir, "shared", "vanity-keys")
            shutil.rmtree(vanity, ignore_errors=True)
            folder = os.path.join(vanity, address)
            os.makedirs(folder, mode=0o700)
            for name in _KEY_FILES:
                if os.path.isfile(os.path.join(keys, name)):
                    shutil.copyfile(os.path.join(keys, name), os.path.join(folder, name))
            with open(os.path.join(folder, "hostname"), "w", encoding="utf-8") as f:
                f.write(address + "\n")
            log(f"Setup: restored Tor keys for {address} from backup")
            say(f"  Tor keys restored from backup ({address}).")
            return True, address
        log("Setup: the backup holds no onion service key; this install keeps its own")
        say("  This backup holds no onion service key: this install keeps its own address.")
        return True, ""
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def ask_setup_answers(*, user: str = "", password: str = "", title: str = "",
                      input_func=input, getpass_func=None,
                      say: Callable[[str], None] = print) -> tuple[str, str, str]:
    """The terminal's answers for install_fresh_wordpress (#442.10): the
    username (an onionname, validate_onionname), the password (6 or more
    characters, typed twice, never echoed) and the site title, each asked
    only when not given. Raises EOFError when there is no terminal to ask."""
    import getpass
    getpass_func = getpass_func or getpass.getpass
    if not (user and password):
        say("")
        say("  =======================================")
        say("  OnionPress First-Time Setup")
        say("  =======================================")
        say("")
        say("  Create your WordPress admin account.")
        say("  You'll use these credentials to log in at /wp-admin.")
        say("")
    suggestion = "" if user else (suggest_onionname() or "")
    while True:
        if not user:
            prompt = f"  Username [{suggestion}]: " if suggestion else "  Username: "
            user = input_func(prompt).strip() or suggestion
        ok, reason = validate_onionname(user) if user else (False, "empty")
        if ok:
            break
        say("  Username cannot be empty." if not user
            else f"  Invalid username: {onionname_error_message(reason)}")
        user = ""
    while len(password) < 6:
        if password:
            say("  Password must be at least 6 characters.")
        password = getpass_func("  Admin password: ")
        if len(password) < 6:
            continue
        if getpass_func("  Confirm password: ") != password:
            say("  Passwords do not match. Try again.")
            password = ""
    if not title:
        default = default_site_title()
        title = input_func(f"  Site title [{default}]: ").strip() or default
    return user, password, title


def save_archive_login(email: str, password: str, *, docker_bin: str, docker_env: dict | None,
                       log: Callable[[str], None]) -> bool:
    """Log in to archive.org with the owner's account (over Tor,
    multisite.fetch_archive_s3_keys) and keep its S3 keys in WordPress,
    each value on `wp option update`'s stdin."""
    from onionpress import multisite
    keys = multisite.fetch_archive_s3_keys(email, password, docker_bin=docker_bin,
                                           env=docker_env, log_func=log)
    if not keys:
        return False
    for option, value in (("onionpress_archive_s3_access", keys["access"]),
                          ("onionpress_archive_s3_secret", keys["secret"])):
        if _run_wp(docker_bin, ("option", "update", option), input=value,
                   env=docker_env).returncode != 0:
            return False
    log("Archive.org S3 keys saved (from login)")
    return True


def setup_from_terminal(*, data_dir: str, mac: bool, launcher_bin: str, docker,
                        manager, user: str = "", title: str = "", from_backup: str = "",
                        ia_account: str = "", log: Callable[[str], None],
                        say: Callable[[str], None] = print, input_func=input,
                        getpass_func=None) -> int:
    """`onionpress setup` (#442.11): WordPress installed the way the setup
    window installs it (install_fresh_wordpress), with the answers given
    or asked at the terminal, or with a backup's settings and key
    (stage_setup_from_backup) under temporary credentials the restore
    replaces. One of the SETUP_* codes: SETUP_RESTART when the launcher
    must restart the stack (a backup's setup; with its key flagged as an
    import first, after any hub holding the current key was asked to
    delete it)."""
    from onionpress.containers import IMPORT_KEY_PENDING
    docker_bin, docker_env = docker.docker_bin, docker.env

    def wp(*args):
        return _run_wp(docker_bin, args, env=docker_env, timeout=30)

    if wp("core", "is-installed").returncode == 0:
        say("WordPress is already installed.")
        # A site is set up: no welcome screen for it (issue #374; a node
        # set up headlessly before the marker existed gets it here).
        mark_setup_complete(data_dir, log)
        return SETUP_ALREADY_INSTALLED
    if wp("--info").returncode != 0:
        say("WordPress container is not running. Start OnionPress first."
            + ("" if mac else ": systemctl --user start onionpress"))
        return SETUP_FAILED

    password = os.environ.pop(SETUP_PASSWORD_ENV, "")
    ia_password = os.environ.pop(IA_PASSWORD_ENV, "")
    address = ""
    if from_backup:
        from onionpress.launcher_ops import BACKUP_PASSWORD_ENV
        backup_password = os.environ.pop(BACKUP_PASSWORD_ENV, "")
        if not backup_password or not os.path.isfile(from_backup):
            say("Usage: onionpress setup --from-backup <password> <backup.zip>")
            return SETUP_FAILED
        ok, address = stage_setup_from_backup(from_backup, backup_password, data_dir, log, say)
        if not ok:
            return SETUP_FAILED
        # The restore replaces the database, and these credentials with it.
        user, password, title = "admin", backup_password, title or "OnionPress"
        say("  Installing WordPress with temporary credentials (restore will overwrite)...")
    else:
        try:
            user, password, title = ask_setup_answers(
                user=user, password=password, title=title, input_func=input_func,
                getpass_func=getpass_func, say=say)
        except EOFError:
            say("ERROR: no terminal to ask on; give --user and --title (and the "
                "password with --pass)")
            return SETUP_FAILED

    say("")
    say("  Installing WordPress...")
    if not install_fresh_wordpress(
            site_title=title, onionname=user, password=password,
            onion_addr=(onion_hostname.read_served_address_with(docker_bin, env=docker_env)
                        or "localhost"),
            docker_bin=docker_bin, docker_env=docker_env, launcher_bin=launcher_bin,
            log_func=log, data_dir=data_dir):
        say(f"  ERROR: WordPress installation failed. Check {os.path.join(data_dir, 'onionpress.log')} "
            "for details.")
        return SETUP_FAILED
    say("  WordPress installed successfully!")

    if ia_account and ia_password:
        say("  Logging in to Archive.org (over Tor)...")
        if save_archive_login(ia_account, ia_password, docker_bin=docker_bin,
                              docker_env=docker_env, log=log):
            say("  Archive.org credentials saved.")
        else:
            say("  WARNING: Archive.org login failed. You can set credentials later in Settings.")

    if from_backup:
        if address:
            # The backup's key replaces the one Tor serves now: hubs that
            # may hold a copy of the current one are asked to delete it
            # first (not the backup's own address, which stays).
            from onionpress.onionheaven import release_onionheaven
            try:
                release_onionheaven("restore", keep_address=address, data_dir=data_dir)
            except Exception as e:
                log(f"Setup: could not ask the OnionHeaven hub to delete the old key ({e}); "
                    "continuing")
            log("Setup --from-backup: flagging key reimport...")
            with open(os.path.join(data_dir, IMPORT_KEY_PENDING), "w", encoding="utf-8"):
                pass
        return SETUP_RESTART

    # Tor, if it is not running, its key volume in place first (it would
    # otherwise create an empty one the next start takes for the key).
    if not docker.container_running("onionpress-tor"):
        say("  Starting Tor...")
        why = None if manager.migrate_key_volume() else "the migration failed"
        why = why or manager.key_volume_not_ready()
        if why:
            say("  Tor not started: the onion service key volume is not in place (see "
                f"{os.path.join(data_dir, 'onionpress.log')}).")
            return SETUP_FAILED
        manager.run_compose_to(["up", "-d", "tor"] + ([] if mac else ["onionheaven"]),
                               out="log")
    return SETUP_DONE

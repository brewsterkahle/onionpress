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

from onionpress import key_manager

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
        return subprocess.run(
            [docker_bin, "exec", "onionpress-wordpress",
             "wp", "--allow-root"] + list(args),
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout,
        )

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
    """Run wp-cli in onionpress-wordpress; with *input*, `docker exec -i`
    and *input* on stdin.

    A timeout comes back as a failed CompletedProcess (returncode 124) whose
    stderr names the wp command, not the argv: a TimeoutExpired's message
    quotes the whole command line, and callers log what they catch.
    """
    argv = ([docker_bin, "exec"] + (["-i"] if input is not None else [])
            + ["onionpress-wordpress", "wp", "--allow-root"] + list(args))
    try:
        return subprocess.run(
            argv, input=input, env=env,
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        name = " ".join(a for a in list(args)[:2] if a.isalpha())
        return subprocess.CompletedProcess(
            argv, 124, "", f"wp {name} timed out after {timeout}s")


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
        return subprocess.run(
            [docker_bin, "exec"] + (["-i"] if stdin is not None else [])
            + ["onionpress-wordpress", "wp", "--allow-root"] + list(args),
            input=stdin,
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout,
        )

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
        log_func=log_func,
    )

    log(f"Subsite /{onionname}/ ready")
    return True


def install_fresh_wordpress(
    *,
    site_title: str,
    onionname: str,
    password: str,
    language: str = "en_US",
    onion_addr: str = "localhost",
    docker_bin: str = "docker",
    launcher_bin: str | None = None,
    log_func: Callable[[str], None] | None = None,
    data_dir: str | None = None,
) -> bool:
    """Install WordPress from scratch with the user's chosen credentials.

    The cross-platform replacement for the old "bash auto-bootstraps with a
    random password, wizard then renames the user" dance on Linux: now the
    GTK SetupDialog, Mac SetupWindow, and `onionpress setup` SSH path all
    funnel through this single function with the user-typed creds.

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
      6. provision_primary_subsite() — per-user subsite at /<onionname>/.
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
        return _run_wp(docker_bin, args, timeout=timeout)

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
    r = set_wp_user_password(onionname, password, docker_bin=docker_bin)
    if r.returncode != 0:
        password_set = False
        log(f"ERROR: setting the admin password failed: "
            f"{r.stderr.strip()[:200]}")
    else:
        log("Admin password set")

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
        log_func=log_func,
    )

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
    _write_config(data_dir, "ONIONNAME", onionname)
    log(f"Onionname saved to config: {onionname}")

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
    _write_config(data_dir, "ONIONNAME", onionname)
    log(f"Onionname saved to config: {onionname}")

    return True


def provision_interactive(data_dir: str | None = None) -> bool:
    """Interactive terminal setup for headless / SSH installs.

    Called by `onionpress provision` on systems where WordPress has already
    been auto-installed by the container (systemd path).  Prompts for site
    title, username, and password, then delegates to
    provision_existing_wordpress().
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
        msgs: dict[str, str] = {
            "too_short": "Too short (min 2 characters)",
            "too_long": "Too long (max 63 characters)",
            "invalid_chars": "Letters and numbers only",
            "all_numeric": "Cannot be all numbers",
            "empty": "Cannot be empty",
        }
        print(f"  Invalid username: {msgs.get(reason, reason)}")

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
        r = subprocess.run(
            ["docker", "exec", "onionpress-wordpress",
             "wp", "--allow-root", "core", "is-installed"],
            capture_output=True, text=True, timeout=15,
        )
        return r.returncode == 0
    except Exception:
        return False


def _read_onion_address() -> str:
    try:
        r = subprocess.run(
            ["docker", "exec", "onionpress-tor",
             "cat", "/var/lib/tor/hidden_service/wordpress/hostname"],
            capture_output=True, text=True, timeout=10,
        )
        addr = r.stdout.strip()
        return addr if key_manager.is_valid_onion_address(addr) else ""
    except Exception:
        return ""


def _write_config(data_dir: str, key: str, value: str) -> None:
    config_path = os.path.join(data_dir, "config")
    try:
        lines = []
        found = False
        if os.path.exists(config_path):
            with open(config_path) as f:
                for line in f:
                    if line.startswith(f"{key}="):
                        lines.append(f"{key}={value}\n")
                        found = True
                    else:
                        lines.append(line)
        if not found:
            lines.append(f"{key}={value}\n")
        with open(config_path, "w") as f:
            f.writelines(lines)
    except OSError:
        pass

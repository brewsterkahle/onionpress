"""Shared first-run setup logic for OnionPress.

Used by:
  - linux/onionpress-tray (GTK setup dialog)
  - src/onionpress/setup_window.py / menubar.py (macOS AppKit dialog)

Keeps step definitions, name utilities, and provisioning in one place
so both platforms stay in sync without copy-paste.
"""

import os
import subprocess
from typing import Callable, Optional

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


# ─── Name utilities ──────────────────────────────────────────────────────────

def suggest_onionname(lang: str = "en_US") -> Optional[str]:
    """Return a random adjective-noun onionname suggestion, or None."""
    if _onc is None:
        return None
    try:
        return _onc.suggest_name_local(lang) or _onc.suggest_name_local("en_US")
    except Exception:
        return None


def validate_onionname(name: str) -> tuple[bool, str]:
    """Validate an onionname.

    Returns (ok, reason). reason is "" on success, or one of:
      "too_short", "too_long", "invalid_chars", "all_numeric", "empty"
    """
    if not name:
        return False, "empty"
    if _onc is None:
        # No validator available — accept anything non-empty
        return True, ""
    try:
        ok, reason = _onc.validate_name(name)
        return ok, (reason or "")
    except Exception:
        return True, ""


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

    Returns True (best-effort — individual failures are logged but do
    not block the wizard).
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
    #    /<onionname>/wp-admin/ instead of the network root.
    r = wp("eval", f'echo get_blog_id_from_url("localhost", "/{onionname}/");')
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

    log(f"Subsite /{onionname}/ ready")
    return True


_PLACEHOLDER_INDEX_HTML = """\
<!doctype html>
<html>
<head><meta charset="utf-8"><title>OnionPress</title></head>
<body>
<h1>Your site is live, but empty</h1>
<p>Publish your own static site (Hugo, Jekyll, a Next.js export, or plain
HTML — anything that produces static files) with:</p>
<pre>onionpress publish &lt;directory&gt;</pre>
</body>
</html>
"""


def provision_static_site(
    *,
    onionname: str,
    documents_dir: str | None = None,
    log_func: Callable[[str], None] | None = None,
    data_dir: str | None = None,
) -> bool:
    """Set up a fresh static-site install: content dir + persisted onionname.

    The static-mode counterpart to install_fresh_wordpress() /
    provision_existing_wordpress() — no wp-cli, no database, no site
    title/admin-password concept. Just: make sure the published-content
    directory exists (with a friendly placeholder so the site isn't blank
    before the first `onionpress publish`), and persist the chosen
    onionname the same way WordPress installs do (actual network
    registration is a separate, later, interactive action — see
    onionnames_registrar.Registrar — identical to how WP installs work).

    Returns True on success.
    """
    def log(msg: str) -> None:
        if log_func:
            log_func(msg)

    if documents_dir is None:
        from .platform import default_documents_dir
        documents_dir = default_documents_dir()

    site_dir = os.path.join(documents_dir, "Site")
    try:
        os.makedirs(site_dir, exist_ok=True)
        if not os.listdir(site_dir):
            with open(os.path.join(site_dir, "index.html"), "w", encoding="utf-8") as f:
                f.write(_PLACEHOLDER_INDEX_HTML)
            log(f"Created placeholder site at {site_dir}")
    except OSError as e:
        log(f"ERROR: could not create {site_dir}: {e}")
        return False

    if data_dir is None:
        data_dir = os.path.join(os.path.expanduser("~"), ".onionpress")
    _write_config(data_dir, "SITE_TYPE", "static")
    _write_config(data_dir, "ONIONNAME", onionname)
    log(f"Onionname saved to config: {onionname}")

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
      1. wp core install (admin_user=onionname, admin_password=password)
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
    warnings without failing the overall install).
    """
    def log(msg: str) -> None:
        if log_func:
            log_func(msg)

    def wp(*args, timeout: int = 60, **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(
            [docker_bin, "exec", "onionpress-wordpress",
             "wp", "--allow-root"] + list(args),
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout, **kwargs,
        )

    log(f"Installing WordPress: title={site_title!r} onionname={onionname!r}")

    # 1. wp core install — fails if already installed, which is fine here
    # because callers (SetupDialog, CLI setup) branch on wp_installed
    # before choosing this path.
    r = wp(
        "core", "install",
        f"--url=http://{onion_addr}",
        f"--title={site_title}",
        f"--admin_user={onionname}",
        f"--admin_password={password}",
        "--admin_email=admin@onionpress.local",
        "--skip-email",
    )
    if r.returncode != 0:
        log(f"ERROR: wp core install failed: {r.stderr.strip()[:200]}")
        return False
    log("WordPress installed")

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
        except Exception as e:
            log(f"WARNING: archive.org S3 key fetch failed: {e}")

    import threading
    threading.Thread(target=_fetch_s3_keys, daemon=True).start()
    log("Archive.org S3 key fetch started in background")

    return True


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
    logged but do not cause False so the wizard is never stuck).
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

    # 3. Password
    r = wp("user", "update", onionname,
           f"--user_pass={password}",
           "--skip-email")
    if r.returncode == 0:
        log("Password updated")
    else:
        r2 = wp("user", "update", "admin",
                 f"--user_pass={password}",
                 "--skip-email")
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

    site_type = "wordpress"
    raw = input("  Publish with WordPress, or bring your own static site "
                 "(Hugo, Jekyll, plain HTML, ...)? [wordpress]: ").strip().lower()
    if raw in ("static", "s"):
        site_type = "static"

    if site_type == "static":
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

        print()
        print("  Setting up your static site…")
        log_fn = lambda msg: print(f"  {msg}")
        ok = provision_static_site(
            onionname=onionname, data_dir=data_dir, log_func=log_fn,
        )
        if ok:
            print()
            print("  Setup complete! Publish your site with:")
            print("    onionpress publish <directory>")
        return ok

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
        return r.stdout.strip()
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

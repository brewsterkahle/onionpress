"""The app's remaining functions as `onionpress` subcommands (issue #382).

What the Mac menubar, the Linux tray and the WordPress Settings page offer
and the command line did not: `version`, `address --url/--copy`,
`check-reachability`, `logs --app/--launcher/--web`, `open
site|admin|settings|onion` and `onionname show|set`.
One implementation for both platforms; both launchers hand the subcommand
to onionpress.cli, which calls in here. None of these constructs
OnionPressCLI (its setup creates config and secrets and probes ports):
each takes the resolved paths and a Docker wrapper.

Secrets stay off argv: an auto-login token reaches the browser through
launcher_ops.login_target's file:// page, never a command line; the
clipboard gets the shareable URL, which carries no token.
"""

import collections
import datetime
import json
import os
import shutil
import subprocess
import sys

from . import __version__
from . import config as _config
from . import key_manager, launcher_ops, onion_hostname
from .log_rotation import current_log_path

TOR_CONTAINER = "onionpress-tor"
WORDPRESS_CONTAINER = "onionpress-wordpress"
ONIONHEAVEN_CONTAINER = "onionheaven"

# The daemon's cache of the served address (the menubar's too).
ADDRESS_CACHE = "onion_address"
# daemon.ONIONNAME_RETRY_MARKER (a test keeps them equal): what the Mac
# daemon takes on its next tick.
ONIONNAME_RETRY_MARKER = ".onionname-retry"
# Said when an onionname is recorded but not registered because
# OnionHeaven (or OnionHome) is off on this install.
ONIONNAME_OFF_NOTE = ("Not registered with OnionHome: onionnames are registered only while "
                      "OnionHeaven is on (REGISTER_WITH_ONIONHEAVEN), and it is off on "
                      "this install.")

# The daemon's web log capture skips the app's own probes the same way.
HEALTHCHECK_LOG_MARK = "OnionPress-HealthCheck"

LOG_KINDS = ("app", "launcher", "web")
OPEN_TARGETS = ("site", "admin", "settings", "onion")

NO_ADDRESS_MSG = "No onion address available (is OnionPress running?)"


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------

def version_command(os_name, *, json_output=False, out=None):
    """`onionpress version [--json]`: the About box's number."""
    out = out or sys.stdout
    if json_output:
        print(json.dumps({"version": __version__, "platform": os_name}), file=out)
    else:
        print(f"OnionPress {__version__}", file=out)
    return 0


# ---------------------------------------------------------------------------
# the address
# ---------------------------------------------------------------------------

def served_address(docker, data_dir=""):
    """The onion address the tor container serves, else the daemon's
    cached one, else '' (never anything but a whole v3 address)."""
    try:
        addr = onion_hostname.read_served_address(docker)
    except Exception:
        addr = ""
    if addr:
        return addr
    if data_dir:
        try:
            with open(os.path.join(data_dir, ADDRESS_CACHE), encoding="utf-8") as f:
                addr = f.read().strip()
        except OSError:
            addr = ""
        if key_manager.is_valid_onion_address(addr):
            return addr
    return ""


def onionname_of(config_file):
    return _config.read_value(config_file, "ONIONNAME", "").strip()


def shareable_url(address, onionname=""):
    """The site's URL as the menu's Copy Onion Address gives it: with the
    /<onionname>/ subsite path, no login token."""
    return f"http://{address}/{onionname}/" if onionname else f"http://{address}/"


def clipboard_tools(os_name):
    if os_name == "macos":
        return [["pbcopy"]]
    return [["wl-copy"], ["xclip", "-selection", "clipboard"],
            ["xsel", "--clipboard", "--input"]]


def copy_to_clipboard(text, os_name):
    """Put *text* on the clipboard with the first tool found; returns the
    tool's name. RuntimeError when there is none or it fails."""
    for argv in clipboard_tools(os_name):
        if shutil.which(argv[0]):
            r = subprocess.run(argv, input=text.encode("utf-8"), capture_output=True,
                               timeout=15)
            if r.returncode != 0:
                raise RuntimeError(f"{argv[0]} failed: "
                                   f"{r.stderr.decode('utf-8', 'replace').strip()}")
            return argv[0]
    wanted = " or ".join(a[0] for a in clipboard_tools(os_name))
    raise RuntimeError(f"no clipboard tool found (install {wanted})")


def address_command(paths, docker, os_name, *, url=False, copy=False, out=None):
    """`onionpress address [--url] [--copy]`."""
    out = out or sys.stdout
    addr = served_address(docker, paths.data_dir)
    if not addr:
        print(NO_ADDRESS_MSG, file=sys.stderr)
        return 1
    text = shareable_url(addr, onionname_of(paths.config_file)) if (url or copy) else addr
    print(text, file=out)
    if copy:
        try:
            tool = copy_to_clipboard(text, os_name)
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            print(f"Not copied: {e}", file=sys.stderr)
            return 1
        print(f"Copied to the clipboard ({tool}).", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# check-reachability: the Settings page's test
# ---------------------------------------------------------------------------

def reachability_test(docker, address, max_time=60):
    """Fetch http://<address>/ through the independent `onionheaven` Tor
    client (not the tor container that hosts the service, whose own
    circuits say nothing about what visitors see). The page's test and
    handle-action's check-reachability share this; the result dict is
    what reachability-result.json holds."""
    r = docker.run(
        ["exec", ONIONHEAVEN_CONTAINER, "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
         "--max-time", str(max_time), "--socks5-hostname", "127.0.0.1:9050",
         f"http://{address}/"],
        timeout=max_time + 15, quiet=True)
    code = r.output.strip() if r.ok else ""
    code = code if code.isdigit() else "000"
    return {"reachable": 200 <= int(code) < 500, "http_code": code,
            "address": address, "tested_at": _now_iso()}


def check_reachability_command(paths, docker, *, json_output=False, out=None):
    """`onionpress check-reachability [--json]`: exit 0 reachable, 1 not,
    2 no address to test."""
    out = out or sys.stdout
    addr = served_address(docker, paths.data_dir)
    if not addr:
        result = {"reachable": False, "error": "No onion address found",
                  "tested_at": _now_iso()}
        rc = 2
    else:
        result = reachability_test(docker, addr)
        rc = 0 if result["reachable"] else 1
    if json_output:
        print(json.dumps(result), file=out)
    elif rc == 2:
        print(NO_ADDRESS_MSG, file=out)
    elif rc == 0:
        print(f"Reachable: http://{addr}/ answered HTTP {result['http_code']} through "
              f"the onionheaven Tor client.", file=out)
    else:
        print(f"Not reachable: http://{addr}/ gave HTTP {result['http_code']} through "
              f"the onionheaven Tor client (000: no answer).", file=out)
    return rc


# ---------------------------------------------------------------------------
# logs --app | --launcher | --web
# ---------------------------------------------------------------------------

def log_file_for(paths, kind, os_name):
    """The file a log kind is read from, or None (web usage without the
    daemon's capture: read from the container instead)."""
    logs_dir = os.path.join(paths.data_dir, "logs")
    if kind == "launcher":
        return paths.log_file
    if kind == "app":
        # The daemon's rotating log on both platforms (issue #407); the
        # flat onionpress.log (the launcher's) until the daemon has one.
        path = current_log_path(logs_dir, "onionpress")
        if path and os.path.exists(path):
            return path
        return paths.log_file
    if kind == "web":
        path = current_log_path(logs_dir, "wordpress-visitors")
        return path if path and os.path.exists(path) else None
    raise ValueError(f"unknown log kind: {kind}")


def tail_lines(path, lines):
    """The last *lines* of a text file (all of it for lines <= 0)."""
    with open(path, encoding="utf-8", errors="replace") as f:
        if lines <= 0:
            return list(f)
        return list(collections.deque(f, maxlen=lines))


def _follow_file(path, lines):
    try:
        return subprocess.call(["tail", "-n", str(max(lines, 0)), "-F", path])
    except KeyboardInterrupt:
        return 0


def is_visitor_line(line):
    return HEALTHCHECK_LOG_MARK not in line


def _web_log_from_container(docker, lines, follow, out):
    """The WordPress container's access log, the app's own probes left
    out, as the daemon's wordpress-visitors capture has it."""
    if follow:
        try:
            proc = docker.popen(
                ["logs", "-f", "--tail", str(max(lines, 0)), WORDPRESS_CONTAINER],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"Could not read the web usage log: {e}", file=sys.stderr)
            return 1
        try:
            for line in proc.stdout:
                if is_visitor_line(line):
                    out.write(line)
                    out.flush()
        except KeyboardInterrupt:
            pass
        finally:
            proc.terminate()
        return 0
    args = ["logs", WORDPRESS_CONTAINER]
    if lines > 0:
        args[1:1] = ["--tail", str(lines)]
    r = docker.run(args, timeout=60, quiet=True)
    if not r.ok:
        print("No web usage log: the WordPress container is not running.", file=sys.stderr)
        return 1
    for line in (r.stdout + r.stderr).splitlines(keepends=True):
        if is_visitor_line(line):
            out.write(line)
    return 0


def logs_command(paths, docker, os_name, kind, *, lines=200, follow=False, out=None):
    """`onionpress logs --app|--launcher|--web [-n N] [-f]`."""
    out = out or sys.stdout
    path = log_file_for(paths, kind, os_name)
    if path is None:
        return _web_log_from_container(docker, lines, follow, out)
    if not os.path.exists(path):
        print(f"No {kind} log yet ({path}).", file=sys.stderr)
        return 1
    if follow:
        return _follow_file(path, lines)
    out.writelines(tail_lines(path, lines))
    return 0


# ---------------------------------------------------------------------------
# open site | admin | settings | onion
# ---------------------------------------------------------------------------

def wordpress_port(docker):
    """The published WordPress port: what the launcher exported, else the
    running stack's (config.resolve_port_offset, through *docker*)."""
    value = os.environ.get("ONIONPRESS_WP_PORT", "")
    if value.isdigit():
        return int(value)
    return _config.resolve_port_offset(docker).wp_port


def open_target(what, paths, docker):
    """(url, prefer_tor) for `open <what>`: the local site, wp-admin, the
    Settings page, or the onion address in a Tor-capable browser. The
    /<onionname>/ path takes the owner to their own subsite."""
    name = onionname_of(paths.config_file)
    if what == "onion":
        addr = served_address(docker, paths.data_dir)
        if not addr:
            raise RuntimeError(NO_ADDRESS_MSG)
        return shareable_url(addr, name), True
    base = f"http://localhost:{wordpress_port(docker)}"
    if what == "site":
        return (f"{base}/{name}/" if name else f"{base}/"), False
    if what == "admin":
        return f"{base}/wp-admin/", False
    if what == "settings":
        return f"{base}/wp-admin/admin.php?page=onionpress-settings", False
    raise ValueError(f"open: what is {what!r}; one of {', '.join(OPEN_TARGETS)}")


def open_command(what, paths, docker, *, json_output=False, out=None):
    """`onionpress open site|admin|settings|onion [--json]`, logged in as
    the owner: the browser gets launcher_ops.login_target's file:// page
    (the token never on a command line); the onion address goes to a
    Tor-capable browser through launcher_ops.open_onion, the one
    implementation the menubar and the tray call (issue #527). With
    --json the answer is one JSON object (open_onion's for `onion`)."""
    out = out or sys.stdout

    def say(message):
        print(message, file=sys.stderr if json_output else out, flush=True)

    url = ""
    try:
        url, prefer_tor = open_target(what, paths, docker)
        if prefer_tor:
            answer = launcher_ops.open_onion(url, docker=docker, data_dir=paths.data_dir,
                                             say=say)
        else:
            launcher_ops.open_in_browser(launcher_ops.login_target(url, docker=docker))
            answer = {"ok": True, "browser": "default"}
    except RuntimeError as e:
        answer = {"error": str(e)}
    if json_output:
        print(json.dumps({"url": url, **answer}), file=out)
    elif answer.get("error"):
        print(f"ERROR: {answer['error']}", file=sys.stderr)
    elif answer.get("paste_url"):
        print("Tor Browser is already running: paste this into a new tab (it logs you "
              f"in once, for two minutes):\n{answer['paste_url']}", file=out)
    elif answer.get("launching"):
        print("Tor Browser is starting already: open the site once it is up.", file=out)
    else:
        print(f"Opening {url}", file=out)
    return 1 if answer.get("error") else 0


# ---------------------------------------------------------------------------
# onionname show | set
# ---------------------------------------------------------------------------

def onionname_record(config_file):
    name = onionname_of(config_file)
    address = _config.read_value(config_file, "ONIONNAME_ADDRESS", "").strip()
    registered = _config.read_value(config_file, "ONIONNAME_REGISTERED", "").strip() == "yes"
    return {"name": name, "address": address, "registered": registered,
            "url": shareable_url(address, name) if name and address else ""}


def onionname_show(paths, *, json_output=False, out=None):
    """`onionpress onionname show [--json]`."""
    out = out or sys.stdout
    record = onionname_record(paths.config_file)
    if json_output:
        print(json.dumps(record), file=out)
        return 0
    if not record["name"]:
        print("No onionname is set (onionpress onionname set NAME).", file=out)
        return 0
    print(f"onionname: {record['name']}", file=out)
    print(f"address: {record['address'] or '(not recorded)'}", file=out)
    print(f"registered: {'yes' if record['registered'] else 'no'}", file=out)
    if record["url"]:
        print(f"url: {record['url']}", file=out)
    return 0


def _save_onionname(paths, log, name, address, registered):
    for key, value in (("ONIONNAME", name), ("ONIONNAME_ADDRESS", address),
                       ("ONIONNAME_REGISTERED", "yes" if registered else "no")):
        why = _config.check_config_value(key, value)
        if why:
            raise ValueError(why)
    for key, value in (("ONIONNAME", name), ("ONIONNAME_ADDRESS", address),
                       ("ONIONNAME_REGISTERED", "yes" if registered else "no")):
        log(_config.set_config_value(paths.config_file, key, value).describe())


class OnionnameOutcome(collections.namedtuple(
        "OnionnameOutcome", "status name suggestions note error")):
    """What register_onionname did: status "registered" (OnionHome took
    it for the served address), "recorded" (kept unregistered: the site
    is not running, OnionHome did not answer, or *register* was False),
    "taken" (OnionHome has it for another address: nothing recorded),
    "invalid" (*error* says why: nothing recorded) or "unsaved" (the
    config could not be written: *error*)."""


def register_onionname(name, paths, docker, os_name, log, *, registrar=None,
                       register=True):
    """Check *name*, register it with OnionHome under the served address
    when there is one, and record it in the config (ONIONNAME,
    ONIONNAME_ADDRESS, ONIONNAME_REGISTERED) unless it is taken. One
    implementation for `onionname set` and the setup (issue #527). A name
    left unregistered is retried by the daemon (.onionname-retry on a Mac).
    *register* False records it unregistered without asking OnionHome."""
    from . import setup_logic
    name = (name or "").strip()
    ok, reason = setup_logic.validate_onionname(name)
    if not ok:
        return OnionnameOutcome("invalid", name, [], "",
                                setup_logic.onionname_error_message(reason))

    from .onionnames_registrar import configured_onionhome
    onionhome = configured_onionhome(paths.config_file, log) if register else ""
    address = served_address(docker, paths.data_dir) if onionhome else ""
    result = None
    if address:
        if registrar is None:
            from .onionnames_registrar import Registrar
            registrar = Registrar(docker, log=log, onionhome_address=onionhome)
        try:
            result = registrar.register(name, address)
        except Exception as e:  # the registrar reports, never raises; belt and braces
            log(f"onionname: registering {name!r} failed: {e}")
            result = None

    if result is not None and result.status == "collision":
        log(f"onionname: {name!r} is taken; not recorded")
        return OnionnameOutcome("taken", name, list(result.suggestions or []), "", "")

    registered = result is not None and result.ok
    try:
        _save_onionname(paths, log, name, address if registered else "", registered)
    except (ValueError, OSError) as e:
        return OnionnameOutcome("unsaved", name, [], "", str(e))
    if registered:
        log(f"onionname: {name!r} registered for {address}")
        return OnionnameOutcome("registered", name, [], "", "")
    if register and not onionhome:
        # OnionHeaven off, or ONIONHOME_ADDRESS empty: no OnionHome, so no
        # retry either (onionnames_registrar.configured_onionhome).
        log(f"onionname: {name!r} recorded; not registered: OnionHeaven or OnionHome is off")
        return OnionnameOutcome("recorded", name, [], ONIONNAME_OFF_NOTE, "")
    if os_name == "macos":
        try:
            with open(os.path.join(paths.data_dir, ONIONNAME_RETRY_MARKER), "w") as f:
                f.write(str(datetime.datetime.now(datetime.timezone.utc).timestamp()))
        except OSError:
            pass
        note = ("Not registered yet: OnionPress registers it once the site is running "
                "(start OnionPress)." if not address else
                "Not registered yet (OnionHome did not answer): OnionPress keeps trying.")
    else:
        note = ("Not registered yet: run this command again while OnionPress is running."
                if not address else
                "Not registered yet (OnionHome did not answer): run this command again later.")
    if result is not None:
        log(f"onionname: {name!r} recorded, registration deferred ({result.status})")
    elif not register:
        log(f"onionname: {name!r} recorded, registration left for later")
    else:
        log(f"onionname: {name!r} recorded, OnionPress not running")
    return OnionnameOutcome("recorded", name, [], note, "")


def onionname_set(name, paths, docker, os_name, log, *, json_output=False, out=None,
                  registrar=None, rename_admin=False):
    """`onionpress onionname set NAME [--rename-admin] [--json]`: the
    menubar's pick-another-name dialog (register_onionname). A name
    OnionHome says is taken is not recorded (exit 2, with its
    suggestions). With *rename_admin* (after a collision, issue #527) the
    WordPress admin whose login is the old onionname is renamed to NAME
    (setup_logic.rename_wp_user); a rename that failed is exit 1, the
    name recorded all the same."""
    from . import setup_logic
    out = out or sys.stdout
    old_name = onionname_of(paths.config_file)
    outcome = register_onionname(name, paths, docker, os_name, log, registrar=registrar)
    if outcome.status == "unsaved":
        print(f"ERROR: {outcome.error}", file=sys.stderr)
        return 2
    if outcome.status == "invalid":
        if json_output:
            print(json.dumps({"error": outcome.error, "name": outcome.name}), file=out)
        else:
            print(f"ERROR: {outcome.error}", file=out)
        return 2
    if outcome.status == "taken":
        if json_output:
            print(json.dumps({"error": "taken", "name": outcome.name,
                              "suggestions": outcome.suggestions}), file=out)
        else:
            print(f"The onionname '{outcome.name}' is already taken.", file=out)
            if outcome.suggestions:
                print("Suggestions: " + ", ".join(outcome.suggestions), file=out)
        return 2

    renamed = None
    if rename_admin and old_name and old_name != outcome.name:
        renamed = setup_logic.rename_wp_user(old_name, outcome.name, docker=docker, log=log)
    record = onionname_record(paths.config_file)
    rename_error = ("" if renamed is not False else
                    f"The WordPress admin is still '{old_name}': renaming it failed "
                    "(see the OnionPress log)")
    if json_output:
        answer = {"ok": not rename_error, **record, "note": outcome.note}
        if renamed is not None:
            answer["admin_renamed"] = renamed
        if rename_error:
            answer["error"] = rename_error
        print(json.dumps(answer), file=out)
    else:
        if outcome.status == "registered":
            print(f"Registered '{outcome.name}': {record['url']}", file=out)
        else:
            print(f"Recorded '{outcome.name}'. {outcome.note}", file=out)
        if renamed:
            print(f"The WordPress admin is now '{outcome.name}'.", file=out)
        elif rename_error:
            print(f"ERROR: {rename_error}", file=sys.stderr)
    return 1 if rename_error else 0

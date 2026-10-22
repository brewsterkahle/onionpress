"""`onionpress handle-action`: the WordPress Settings page's requests,
handled once for both platforms (issue #366, CLI-first phase 2).

The page asks the host for something by writing files into the shared
volume (/var/lib/onionpress/, www-data's): `requested-action` names one of
ACTIONS, and `backup-password`, `restore-password`, `restore-upload.zip`
and `import-key-data` carry what the action needs. On Linux
onionpress-watcher.timer runs `onionpress handle-action` every 10 s; on
the Mac the menubar's status loop runs it. Anything running in WordPress
can write those files, so the verbs are fixed, every result goes back to
the page scrubbed, and nothing here changes the site's onion address
without the person at the host: on Linux a key import is refused and a
restore of another address is refused (nobody is there to ask); on the
Mac the handler stops at the question and the menubar asks it in a native
dialog (the check/confirm protocol, onionpress.confirm).

What the actions do runs through the launcher (`Host.run_launcher`):
start, restart, stop, apply-config, write-status (Linux), import-key,
settings-page-backup, settings-page-restore, update. The Linux launcher
keeps its bash bodies behind those subcommands and the Mac launcher
delegates them to onionpress.cli; this module is what both share.

A question in flight (a Mac key import or restore of another address) is
kept in $DATA_DIR/pending-action/ (0700, no container mounts it): the
request, and the key or the uploaded zip and its password, which the page
has already handed over and which the confirmed run needs again. The next
`handle-action --confirmed address-change` resumes it; `--cancel`, or no
answer within PENDING_MAX_AGE, writes the page a "not confirmed" result
and drops it. While one is pending no other request is handled.
"""

import datetime
import json
import os
import shutil
import subprocess
import tempfile
import time

from . import confirm, redact, wp_files
from .key_manager import is_valid_onion_address
from .launcher_ops import BACKUP_PASSWORD_ENV

SHARED = wp_files.SHARED_DIR
REQUEST_FILE = "requested-action"

ACTIONS = ("start", "restart", "stop", "check-reachability", "import-key-file",
           "create-backup", "restore-backup", "refresh-status", "update")

PENDING_DIR = "pending-action"
PENDING_MAX_AGE = 10 * 60      # the dialog gives up after 5 minutes

# Where the launchers live when handle-action is not told (--launcher).
LINUX_LAUNCHER = "/opt/onionpress/onionpress"

# The time a start or restart gets to bring every container up before the
# result is written (the page keeps polling status.json after that).
WAIT_RUNNING_S = 60

IMPORT_REFUSED_LINUX_MSG = (
    "Not imported: a key import replaces this site's onion address, which the "
    "Settings page never does on Linux. On the computer running OnionPress, "
    "run: onionpress import-key")
IMPORT_NOT_CONFIRMED_MSG = (
    "Not replaced: the person at the Mac running OnionPress has to click "
    "Replace in the dialog OnionPress shows there.")
NO_PASSWORD_MSG = "No password provided"
NO_KEY_MSG = "No key data found"
MISSING_UPLOAD_MSG = "Missing password or backup file"
BACKUP_FAILED_MSG = "Backup failed — check logs"
RESTORE_FAILED_MSG = "Restore failed — wrong password or invalid backup"
HANDOFF_FAILED_MSG = "Could not hand the backup to the Settings page"
UPDATE_CHECK_FAILED_MSG = "Could not check for updates"


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_launcher(paths, os_name):
    """The launcher script of this install: the bundle's on a Mac, the
    .deb's on Linux (both overridable with $ONIONPRESS_LAUNCHER)."""
    env = os.environ.get("ONIONPRESS_LAUNCHER")
    if env:
        return env
    if os_name == "macos" and paths.app_bundle:
        return os.path.join(paths.app_bundle, "Contents", "MacOS", "onionpress")
    return LINUX_LAUNCHER


class Host:
    """What the handler needs from the machine it runs on: the launcher,
    the shared volume (read and written as www-data), the tor container's
    address and the containers' states."""

    def __init__(self, *, launcher, paths, docker, log, os_name, version=""):
        self.launcher = launcher
        self.paths = paths
        self.docker = docker
        self.log = log
        self.os_name = os_name
        self.version = version

    # -- the launcher --

    def run_launcher(self, args, *, env=None, input=None, timeout=None):
        """`<launcher> <args>`; *env* is merged into this process's
        environment (a password goes there, never on argv)."""
        full_env = {**os.environ, **(env or {})}
        # No terminal for the launcher: stdin is /dev/null unless *input*
        # is given (a key), so nothing in it waits on a prompt.
        return subprocess.run(
            [self.launcher, *args], input=input, env=full_env,
            **({} if input is not None else {"stdin": subprocess.DEVNULL}),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout)

    # -- the shared volume, as www-data --

    def read_shared(self, name, timeout=5):
        r = self.docker.run(wp_files.exec_args("cat", f"{SHARED}/{name}"),
                            timeout=timeout, quiet=True)
        return r.output if r.ok else ""

    def shared_size(self, name):
        r = self.docker.run(wp_files.exec_args("stat", "-c", "%s", f"{SHARED}/{name}"),
                            timeout=5, quiet=True)
        text = r.output.strip()
        return int(text) if r.ok and text.isdigit() else 0

    def remove_shared(self, *names):
        self.docker.run(wp_files.exec_args("rm", "-f", *(f"{SHARED}/{n}" for n in names)),
                        timeout=5, quiet=True)

    def put_shared(self, name, data, mode="600"):
        """Written as www-data (wp_files.put_file_args): root's tee or cp
        followed a link www-data can plant at the name."""
        return self.docker.run(wp_files.put_file_args(f"{SHARED}/{name}", mode),
                               timeout=10, input=data, quiet=True)

    def write_result(self, filename, data):
        """A result JSON for the page. Error strings come from exceptions
        and launcher output and can name host paths, so every string is
        scrubbed first."""
        try:
            data = redact.scrub_json_for_wordpress(data, self.paths.data_dir)
            self.put_shared(filename, json.dumps(data))
        except Exception as e:      # the page polls; nothing else to do
            self.log(f"Settings page: could not write {filename}: {e}")

    # -- the containers --

    def served_address(self):
        """The onion address the tor container serves, or ''."""
        r = self.docker.run(["exec", "onionpress-tor", "cat", wp_files.TOR_HOSTNAME_FILE],
                            timeout=10, quiet=True)
        addr = r.output.strip() if r.ok else ""
        return addr if is_valid_onion_address(addr) else ""

    def containers(self):
        """[(name, status)] of the onionpress containers."""
        r = self.docker.run(["ps", "--filter", "name=onionpress", "--format",
                             "{{.Names}}\t{{.Status}}"], timeout=10, quiet=True)
        if not r.ok:
            return []
        out = []
        for line in r.output.splitlines():
            parts = line.split("\t", 1)
            if len(parts) == 2:
                out.append((parts[0], parts[1]))
        return out

    def all_running(self):
        found = self.containers()
        return bool(found) and all(status.startswith("Up") for _, status in found)

    def wait_running(self, timeout=WAIT_RUNNING_S, interval=2):
        deadline = time.monotonic() + timeout
        while True:
            if self.all_running():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(interval)

    def containers_info(self):
        return ", ".join(f"{name.replace('onionpress-', '')}: {status}"
                         for name, status in self.containers())


# ---------------------------------------------------------------------------
# The pending question
# ---------------------------------------------------------------------------

def _pending_dir(host):
    return os.path.join(host.paths.data_dir, PENDING_DIR)


def _pending_load(host):
    path = os.path.join(_pending_dir(host), "request.json")
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("confirm") not in confirm.QUESTIONS:
        return None
    return doc


def _pending_clear(host):
    shutil.rmtree(_pending_dir(host), ignore_errors=True)


def _write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data.encode("utf-8") if isinstance(data, str) else data)


def _pending_save(host, action, question, facts, *, key=None, zip_path=None,
                  password=None):
    """Keep the question and what the confirmed run needs again."""
    _pending_clear(host)
    d = _pending_dir(host)
    os.makedirs(d, mode=0o700)
    os.chmod(d, 0o700)
    if key is not None:
        _write_private(os.path.join(d, "key"), key + "\n")
    if password is not None:
        _write_private(os.path.join(d, "password"), password)
    if zip_path is not None:
        shutil.move(zip_path, os.path.join(d, "restore-upload.zip"))
    doc = {"action": action, "confirm": question,
           "facts": {k: v for k, v in facts.items() if k != "confirm"},
           "asked_at": time.time()}
    _write_private(os.path.join(d, "request.json"), json.dumps(doc))


def _pending_read(host, name):
    try:
        with open(os.path.join(_pending_dir(host), name), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _finish_not_confirmed(host, pending, why):
    """The page's answer when the question was not answered Yes."""
    action = pending.get("action")
    host.log(f"Settings page: {action} not confirmed at this Mac ({why})")
    if action == "import-key-file":
        host.write_result("import-result.json", {
            "success": False, "error": IMPORT_NOT_CONFIRMED_MSG,
            "imported_at": now_iso()})
    elif action == "restore-backup":
        from .backup import ADDRESS_CHANGE_NOT_CONFIRMED_MSG
        host.write_result("restore-result.json", {
            "success": False, "error": ADDRESS_CHANGE_NOT_CONFIRMED_MSG,
            "restored_at": now_iso()})
    _pending_clear(host)


# ---------------------------------------------------------------------------
# The handler
# ---------------------------------------------------------------------------

def handle(host, confirmed=frozenset(), cancel=False):
    """One run of `onionpress handle-action`. Returns what the caller (the
    menubar) needs to know: {"handled": <action or None>}, with
    {"confirm": <question>, ...facts} when the Mac must ask, {"pending":
    <question>} while one is unanswered, {"relaunch": true} after the Mac
    installed an update."""
    pending = _pending_load(host)
    if cancel:
        if pending:
            _finish_not_confirmed(host, pending, "cancelled")
            return {"handled": pending.get("action"), "cancelled": pending["confirm"]}
        return {"handled": None}
    if pending:
        if pending["confirm"] in confirmed:
            return _resume(host, pending, confirmed)
        if time.time() - float(pending.get("asked_at") or 0) > PENDING_MAX_AGE:
            _finish_not_confirmed(host, pending, "no answer")
        else:
            return {"handled": None, "pending": pending["confirm"],
                    "action": pending.get("action"), **pending.get("facts", {})}

    # The page's saved settings (config-updates.json), every run on a Mac
    # (Linux's launcher applies them in write_status and before a start):
    # the menubar's poll_config_updates until issue #366. The accepted
    # keys come back so the menubar applies what it owes at once.
    applied = apply_config(host) if host.os_name == "macos" else []
    extra = {"config": applied} if applied else {}

    action = host.read_shared(REQUEST_FILE).strip()
    if not action:
        if host.os_name == "linux":
            write_recent_logs(host)
        return {"handled": None, **extra}
    host.remove_shared(REQUEST_FILE, "service-result.json")
    if action != "refresh-status":
        host.log(f"Settings page: handling action '{action}'")

    handlers = {
        "start": lambda: _start(host, "start"),
        "restart": lambda: _start(host, "restart"),
        "stop": lambda: _stop(host),
        "check-reachability": lambda: _check_reachability(host),
        "import-key-file": lambda: _import_key_file(host, confirmed),
        "create-backup": lambda: _create_backup(host),
        "restore-backup": lambda: _restore_backup(host, confirmed),
        "refresh-status": lambda: _refresh_status(host),
        "update": lambda: _update(host),
    }
    fn = handlers.get(action)
    if fn is None:
        host.log(f"Settings page: unknown action '{action}'")
        return {"handled": None, "unknown": action, **extra}
    try:
        extra.update(fn() or {})
    except Exception as e:
        host.log(f"Settings page: action '{action}' failed: {e}")
        extra["error"] = str(e)
    return {"handled": action, **extra}


def _resume(host, pending, confirmed):
    """The confirmed run of a pending question."""
    action = pending.get("action")
    host.log(f"Settings page: {action} confirmed at this Mac")
    try:
        if action == "import-key-file":
            key = _pending_read(host, "key").strip()
            _pending_clear(host)
            return {"handled": action, **(_import_key(host, key, confirmed) or {})}
        if action == "restore-backup":
            password = _pending_read(host, "password")
            zip_path = os.path.join(_pending_dir(host), "restore-upload.zip")
            work = tempfile.mkdtemp(prefix="onionpress-restore-upload-")
            local_zip = os.path.join(work, "restore-upload.zip")
            try:
                shutil.move(zip_path, local_zip)
            except OSError:
                local_zip = ""
            _pending_clear(host)
            if not password or not local_zip:
                shutil.rmtree(work, ignore_errors=True)
                host.write_result("restore-result.json", {
                    "success": False, "error": MISSING_UPLOAD_MSG,
                    "restored_at": now_iso()})
                return {"handled": action}
            return {"handled": action,
                    **(_settings_page_restore(host, local_zip, password, confirmed, work) or {})}
    except Exception as e:
        host.log(f"Settings page: action '{action}' failed: {e}")
        return {"handled": action, "error": str(e)}
    _pending_clear(host)
    return {"handled": None}


# -- start / restart / stop --

def _write_status(host):
    """status.json for the page: the Linux launcher writes it; on the Mac
    the menubar does, after this run returns."""
    if host.os_name == "linux":
        host.run_launcher(["write-status"], timeout=300)


def _start(host, action):
    # The page's Restart also applies its saved settings (Linux has always
    # done so; the config file is what the start reads).
    try:
        host.run_launcher(["apply-config"], timeout=300)
    except (OSError, subprocess.SubprocessError) as e:
        host.log(f"Settings page: apply-config before {action}: {e}")
    host.put_shared("service-pending", f"{action}\n")
    host.log(f"Settings page: {action}ing OnionPress...")
    r = host.run_launcher([action])
    host.wait_running()
    _write_status(host)
    verb = "Restarted" if action == "restart" else "Started"
    ok = r.returncode == 0
    host.write_result("service-result.json", {
        "success": ok, "action": action,
        "message": (f"{verb} {host.containers_info()}" if ok
                    else f"{action} failed (rc={r.returncode}); see the OnionPress log"),
        "completed_at": now_iso()})
    host.remove_shared("service-pending")


def _stop(host):
    host.log("Settings page: stopping OnionPress...")
    r = host.run_launcher(["stop"], timeout=600)
    host.write_result("service-result.json", {
        "success": r.returncode == 0, "action": "stop",
        "message": "OnionPress stopped" if r.returncode == 0 else "stop failed; see the OnionPress log",
        "completed_at": now_iso()})


def _refresh_status(host):
    _write_status(host)


# -- reachability --

def _check_reachability(host):
    host.log("Settings page: running reachability test...")
    addr = host.served_address()
    if not addr:
        host.write_result("reachability-result.json", {
            "reachable": False, "error": "No onion address found", "tested_at": now_iso()})
        host.log("Settings page: reachability test: no onion address")
        return
    r = host.docker.run(
        ["exec", "onionheaven", "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
         "--max-time", "60", "--socks5-hostname", "127.0.0.1:9050", f"http://{addr}/"],
        timeout=75, quiet=True)
    code = r.output.strip() if r.ok else ""
    code = code if code.isdigit() else "000"
    reachable = 200 <= int(code) < 500
    host.write_result("reachability-result.json", {
        "reachable": reachable, "http_code": code, "address": addr, "tested_at": now_iso()})
    host.log(f"Settings page: reachability test -> HTTP {code} (reachable={reachable})")


# -- key import --

def _import_key_file(host, confirmed):
    key = host.read_shared("import-key-data").strip()
    host.remove_shared("import-key-data")
    if host.os_name != "macos":
        # No one at this host to ask (the Mac asks the person at the Mac):
        # keys are imported there, with onionpress import-key, which asks
        # first and keeps a copy of the current key.
        host.write_result("import-result.json", {
            "success": False, "error": IMPORT_REFUSED_LINUX_MSG, "imported_at": now_iso()})
        host.log("Settings page: key import refused: import a key on this computer "
                 "with onionpress import-key")
        return
    if not key:
        host.write_result("import-result.json", {
            "success": False, "error": NO_KEY_MSG, "imported_at": now_iso()})
        return
    return _import_key(host, key, confirmed)


def _import_key(host, key, confirmed):
    """The Mac's import: the launcher checks the key and says whether the
    address changes; the person at the Mac is asked; only then is it
    imported, and OnionPress restarted. The key goes on stdin ("-"):
    argv shows in ps to every user."""
    # "a refused key never reaches the dialog": import-key --check decodes
    # and checks it before answering.
    r = host.run_launcher(["import-key", "-", "--check", "--json"],
                          input=key + "\n", timeout=300)
    ans = confirm.last_json_line(r.stdout) or {}
    if (r.returncode in (confirm.EXIT_CONFIRM, confirm.EXIT_REFUSED)
            and ans.get("confirm") == "address-change"):
        if "address-change" not in confirmed:
            _pending_save(host, "import-key-file", "address-change", ans, key=key)
            host.log(f"Settings page: key import of {ans.get('new')} needs the "
                     f"person at this Mac")
            return ans
    elif r.returncode != 0:
        error = ans.get("error") or r.stderr.strip() or r.stdout.strip() or "Import failed"
        host.log(f"Settings page: key import refused: {error}")
        host.write_result("import-result.json", {
            "success": False, "error": error, "imported_at": now_iso()})
        return
    else:
        # The same key as the one installed: nothing to do.
        host.write_result("import-result.json", {
            "success": True, "address": ans.get("address", ""), "imported_at": now_iso()})
        return
    r = host.run_launcher(["import-key", "-", "--confirmed", "address-change"],
                          input=key + "\n", timeout=300)
    if r.returncode != 0:
        error = r.stderr.strip() or r.stdout.strip() or "Import failed"
        host.log(f"Settings page: key import failed: {error}")
        host.write_result("import-result.json", {
            "success": False, "error": error, "imported_at": now_iso()})
        return
    # Restart to pick up the imported key
    host.run_launcher(["restart"])
    host.wait_running(120)
    _write_status(host)
    addr = host.served_address() or ans.get("new", "")
    host.log(f"Settings page: key imported, address: {addr}")
    host.write_result("import-result.json", {
        "success": True, "address": addr, "imported_at": now_iso()})


# -- backup --

def _create_backup(host):
    host.log("Settings page: creating backup...")
    password = host.read_shared("backup-password").strip()
    host.remove_shared("backup-password")
    if not password:
        host.write_result("backup-result.json", {
            "success": False, "error": NO_PASSWORD_MSG, "created_at": now_iso()})
        host.log("Settings page: backup failed: no password")
        return
    # The address the live key derives to names the file (its first 8
    # characters), only from a whole address.
    addr = host.served_address()
    onion_short = addr.replace(".onion", "")[:8] if addr else "site"
    filename = f"OnionPress-{onion_short}-{time.strftime('%Y-%m-%d-%H-%M')}.zip"
    # A folder of our own, not a guessable name in /tmp.
    work = tempfile.mkdtemp(prefix="onionpress-settings-backup-")
    path = os.path.join(work, filename)
    try:
        # Handed to WordPress below, and the password came from there:
        # refused on an OnionHeaven hub or OnionHome, or a node that could
        # not be checked (exit 3, the reason on stdout), and never a hub's
        # registry or registrants' keys, the Cloudflare tunnel token or
        # any other config WordPress is not shown; on a Mac the site's own
        # onion service key stays out too (settings-page-backup).
        r = host.run_launcher(["settings-page-backup", path],
                              env={BACKUP_PASSWORD_ENV: password})
        ans = confirm.last_json_line(r.stdout) or {}
        if r.returncode == confirm.EXIT_REFUSED:
            refusal = ans.get("error") or (r.stdout.strip().splitlines() or [BACKUP_FAILED_MSG])[-1]
            host.write_result("backup-result.json", {
                "success": False, "error": refusal, "created_at": now_iso()})
            host.log("Settings page: backup refused: back this node up with onionpress backup")
            return
        if r.returncode != 0 or not os.path.isfile(path):
            host.write_result("backup-result.json", {
                "success": False, "error": ans.get("error") or BACKUP_FAILED_MSG,
                "created_at": now_iso()})
            host.log(f"Settings page: backup failed (rc={r.returncode})")
            return
        # Hand the zip to the WordPress container for download, written as
        # www-data (the page's own user). Not docker cp + chown as root:
        # both follow a link www-data can plant at this predictable name,
        # and the chown made any file in the container its own. The time
        # allowed grows with the backup (#317).
        with open(path, "rb") as backup_file:
            size = os.fstat(backup_file.fileno()).st_size
            handed = subprocess.run(
                [host.docker.docker_bin, "exec", "-i", "-u", "www-data",
                 "onionpress-wordpress", "sh", "-c", 'umask 077; cat > "$1"', "sh",
                 f"{SHARED}/{filename}"],
                stdin=backup_file, capture_output=True, env=host.docker.env,
                timeout=wp_files.sized_timeout(size, 300))
        if handed.returncode != 0:
            raise RuntimeError(HANDOFF_FAILED_MSG)
        result = {"success": True, "filename": filename, "created_at": now_iso()}
        if ans.get("note"):
            # Shown under the download link (the Mac: the key is not in it).
            result["note"] = ans["note"]
        host.write_result("backup-result.json", result)
        host.log(f"Settings page: backup created: {filename}")
    except Exception as e:
        host.write_result("backup-result.json", {
            "success": False, "error": str(e), "created_at": now_iso()})
        host.log(f"Settings page: backup failed: {e}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


# -- restore --

def _restore_backup(host, confirmed):
    from .backup import check_uploaded_backup
    host.log("Settings page: restoring from backup...")
    password = host.read_shared("restore-password").strip()
    host.remove_shared("restore-password")
    upload = f"{SHARED}/restore-upload.zip"
    # A folder of our own, so nothing stale or planted sits at the copy's
    # path. docker cp copies a symbolic link as a link: one planted at
    # restore-upload.zip would point at a host file, so the copy must be a
    # regular file (check_uploaded_backup) before it is read. The copy's
    # time limit grows with the upload (#317).
    work = tempfile.mkdtemp(prefix="onionpress-restore-upload-")
    local_zip = os.path.join(work, "restore-upload.zip")
    size = host.shared_size("restore-upload.zip")
    r = host.docker.run(["cp", f"onionpress-wordpress:{upload}", local_zip],
                        timeout=wp_files.sized_timeout(size, 60), quiet=True)
    if not r.ok:
        host.log(f"Settings page: could not copy the uploaded backup (docker cp rc={r.returncode})")
    host.remove_shared("restore-upload.zip")
    if not password or not os.path.lexists(local_zip):
        shutil.rmtree(work, ignore_errors=True)
        host.write_result("restore-result.json", {
            "success": False, "error": MISSING_UPLOAD_MSG, "restored_at": now_iso()})
        host.log("Settings page: restore failed: missing password or file")
        return
    try:
        check_uploaded_backup(local_zip)
    except ValueError as e:
        shutil.rmtree(work, ignore_errors=True)
        host.write_result("restore-result.json", {
            "success": False, "error": str(e), "restored_at": now_iso()})
        host.log(f"Settings page: restore refused: {e}")
        return
    return _settings_page_restore(host, local_zip, password, confirmed, work)


def _settings_page_restore(host, local_zip, password, confirmed, work):
    """The in-place restore of the page's upload (the launcher's
    settings-page-restore: backup.restore_from_backup with
    through_wordpress=True on a Mac, restore_backup --from-settings-page
    on Linux), never the
    install-from-backup `restore`. The file and its password came through
    WordPress: refused on an OnionHeaven hub or OnionHome (or a node that
    could not be checked), and for a backup holding a hub's keys or
    OnionHome's name registry; neither hub volume is written. A backup of
    another address replaces this one only if the person at the Mac says
    so (Linux refuses); one without a key keeps it."""
    args = ["settings-page-restore", local_zip, "--json"]
    if "address-change" in confirmed:
        args += ["--confirmed", "address-change"]
    try:
        r = host.run_launcher(args, env={BACKUP_PASSWORD_ENV: password})
        ans = confirm.last_json_line(r.stdout) or {}
        if (r.returncode in (confirm.EXIT_CONFIRM, confirm.EXIT_REFUSED)
                and ans.get("confirm") == "address-change"):
            ans = {k: v for k, v in ans.items() if k != "error"}
            _pending_save(host, "restore-backup", "address-change", ans,
                          zip_path=local_zip, password=password)
            host.log(f"Settings page: restore of {ans.get('new')} needs the person at this Mac")
            return ans
        if r.returncode != 0:
            if r.returncode == confirm.EXIT_REFUSED:
                error = ans.get("error") or (r.stdout.strip().splitlines() or [RESTORE_FAILED_MSG])[-1]
                host.log("Settings page: restore refused: the backup must be restored "
                         "from the app or the CLI")
            else:
                error = ans.get("error") or RESTORE_FAILED_MSG
                host.log(f"Settings page: restore failed (rc={r.returncode})")
            host.write_result("restore-result.json", {
                "success": False, "error": error, "restored_at": now_iso()})
            return
        # Restart to pick up restored keys
        host.run_launcher(["restart"])
        host.wait_running(120)
        _write_status(host)
        addr = host.served_address()
        host.write_result("restore-result.json", {
            "success": True, "address": addr, "restored_at": now_iso()})
        host.log(f"Settings page: restore complete, address: {addr}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


# -- update --

def _update(host):
    if host.os_name != "macos":
        # Report only: anything running in WordPress can ask for this. The
        # page learns whether a release is available and that `onionpress
        # update` on this machine installs it (the launcher writes
        # update-result.json, one GitHub lookup per 5 minutes).
        host.log("Settings page: update check requested")
        host.run_launcher(["settings-page-update-check"], timeout=300)
        return
    host.log("Settings page: install update requested")
    r = host.run_launcher(["update", "--check", "--json"], timeout=300)
    ans = confirm.last_json_line(r.stdout) or {}
    if r.returncode != 0 or ans.get("error") or "update_available" not in ans:
        error = ans.get("error") or r.stderr.strip() or f"rc={r.returncode}"
        host.write_result("update-result.json", {
            "success": False, "error": f"{UPDATE_CHECK_FAILED_MSG}: {error}",
            "current": ans.get("current"), "latest": ans.get("latest"),
            "update_available": False, "completed_at": now_iso()})
        return
    if not ans.get("update_available"):
        current = ans.get("current") or host.version
        host.write_result("update-result.json", {
            "success": True, "update_available": False,
            "message": f"OnionPress {current} is up to date.",
            "current": current, "latest": ans.get("latest") or current,
            "completed_at": now_iso()})
        return
    latest = ans.get("latest", "")
    r = host.run_launcher(["update", "--yes", "--json"])
    ans = confirm.last_json_line(r.stdout) or {}
    if r.returncode != 0:
        error = ans.get("error") or r.stderr.strip() or r.stdout.strip() or "Update failed"
        host.log(f"Settings page: update install failed: {error}")
        host.write_result("update-result.json", {
            "success": False, "error": error, "completed_at": now_iso()})
        return
    host.write_result("update-result.json", {
        "success": True, "version": latest,
        "message": f"Installed v{latest}. OnionPress is restarting to apply the update.",
        "completed_at": now_iso()})
    host.log(f"Settings page: installed v{latest}, relaunching")
    # Give the polling page a moment to see the result before the
    # WordPress container goes down for the relaunch (the menubar's).
    time.sleep(2)
    return {"relaunch": True, "version": latest}


# -- recent logs (Linux's quiet tick) --

def write_recent_logs(host):
    """recent-logs.txt for the status page: the launcher's log, scrubbed
    (home path, user names) by redact.wordpress_log_excerpt."""
    log_file = host.paths.log_file
    if not os.path.isfile(log_file):
        return
    try:
        text = redact.wordpress_log_excerpt(log_file, host.paths.data_dir)
    except OSError:
        text = (f"(Recent logs unavailable: could not scrub "
                f"{os.path.basename(log_file)} for display.)")
    host.put_shared("recent-logs.txt", text + "\n")


# ---------------------------------------------------------------------------
# `onionpress apply-config` (the Mac: Linux's launcher has its bash twin)
# ---------------------------------------------------------------------------

def apply_config(host, out=None):
    """config-updates.json from the Settings page, applied through the
    allowlist (config.apply_settings_updates: only SETTINGS_PAGE_KEYS with
    valid values; the rest dropped and logged by name, secrets never
    logged), then removed. The accepted keys go into the .config-changed
    marker for the running menubar (caffeinate, the analytics upload, the
    tunnel flag, status.json) and through config.apply_side_effects (the
    login item). Prints `changed|unchanged|rejected KEY` lines to *out*
    (nothing without one). Returns the accepted keys ([] when there was
    nothing to apply; None when the file is not JSON yet)."""
    from . import config as op_config
    text = host.read_shared("config-updates.json").strip()
    if not text or text == "{}":
        return []
    try:
        updates = json.loads(text)
    except ValueError:
        # Left for the next run: the page may still be writing it.
        host.log("Settings page: config-updates.json is not valid JSON (left for the next run)")
        return None
    applied = op_config.apply_settings_updates(host.paths.config_file, updates)
    if applied.rejected:
        host.log("Settings page: ignored (not a setting it may change, or an "
                 f"invalid value): {', '.join(applied.rejected)}")
    if applied.changes:
        host.log(f"Settings page: applying {len(applied.changes)} config update(s)")
    for key, old, new in applied.changes:
        host.log(f"  {op_config.describe_change(key, old, new)}")
    host.remove_shared("config-updates.json")
    changed = {key for key, _o, _n in applied.changes}
    if out is not None:
        for key in applied.accepted:
            print(f"{'changed' if key in changed else 'unchanged'} {key}", file=out)
        for key in applied.rejected:
            print(f"rejected {key}", file=out)
    if applied.accepted:
        op_config.note_config_changed(host.paths.data_dir, list(applied.accepted))
        for line in op_config.apply_side_effects(
                list(applied.accepted), host.paths.config_file, host.os_name, host.log,
                app_bundle=host.paths.app_bundle):
            host.log(line)
    return list(applied.accepted)

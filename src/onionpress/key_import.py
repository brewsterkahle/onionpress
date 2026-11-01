"""`onionpress import-key`: replace this site's onion service key, in an
order that never loses the current one (issue #524; the launchers'
import_key / replace_onion_key / set_address_prefix_for, which were two
bash copies).

  1. check: the key is decoded and checked (key_manager: one Tor would
     not serve as the address derived is refused) before anything is
     touched; the staged files are read back as that address;
  2. ask: replacing an address needs the `address-change` confirmation
     (onionpress.confirm), by --confirmed, at a terminal, or not at all;
  3. keep: a copy of every key this install may be using (what the tor
     container holds, and vanity-keys/) goes to <data>/replaced-keys/
     (key_manager.keep_current_keys). If no copy can be made, or nothing
     could be read while a key volume exists (or Docker cannot say),
     nothing is replaced;
  4. only then the OnionHeaven hubs are asked to delete the old key
     (onionheaven.release_onionheaven) and OnionPress is stopped;
  5. the staged folder becomes vanity-keys/ and `.import-key-pending` is
     set: the next start removes the old key volumes and installs the new
     key; ADDRESS_PREFIX follows the new address.

The Settings page never imports a key on Linux (handle_action refuses);
on a Mac it asks the person at the Mac, then runs this with
`--confirmed address-change`.
"""

import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field

from . import config, confirm as _confirm, heartbeat_unit, key_manager, onion_hostname
from .containers import IMPORT_KEY_PENDING, forget_key_volume_migration
from .onionheaven import _RETRYABLE

STAGING_PREFIX = ".key-import-"
# Every place a key lives: the volume, and the two it replaced.
KEY_VOLUMES = ("onionpress-onion-keys", "onionpress-arti-state",
               "onionpress-tor-state")
# The two whose presence means "there is an address to replace".
ADDRESS_VOLUMES = ("onionpress-onion-keys", "onionpress-arti-state")

HELP_TEXT = """\
Usage: onionpress import-key <base32-secret-key> [--check] [--json]
                             [--confirmed address-change]
       onionpress import-key --file <hs_ed25519_secret_key> [...]
       onionpress import-key - [...]        (the key on stdin)

Import a pre-generated ed25519 key for your onion service.

The base32 key should encode the 64-byte expanded ed25519 secret key.
OnionPress will derive the .onion address automatically.

HOW TO GET THE KEY FROM mkp224o OUTPUT:

  OnionPress stores vanity keys in ~/.onionpress/shared/vanity-keys/.
  Each address has its own directory containing hs_ed25519_secret_key.
  Pass that file with --file, or encode it to base32 yourself.

  OnionPress accepts both the full file (96 bytes with header) and the
  64-byte key alone.

EXAMPLE:

  onionpress import-key --file ~/.onionpress/shared/vanity-keys/<address>.onion/hs_ed25519_secret_key

WHAT THIS COMMAND DOES:

  1. Decodes the key and checks it is a usable 64-byte expanded
     ed25519 key; nothing is changed for a key that is refused
  2. Derives the .onion address from it
  3. Keeps a copy of the current key in ~/.onionpress/replaced-keys/
     <address>-<time>/ (import that key again to go back)
  4. Writes key files to ~/.onionpress/shared/vanity-keys/<address>/
     - ks_hs_id.ed25519_expanded_private  (OpenSSH PEM, the format the container takes)
     - hostname                           (the .onion address)
  5. On next start, OnionPress will use this key

NOTE: If an onion address already exists, you will be asked to
confirm the replacement. OnionPress will handle stopping and
removing the old keys automatically.
"""


@dataclass
class Replacement:
    """What replace_key did. status: "replaced", "unchanged" (already this
    site's address: nothing done), "failed" (nothing replaced, error says
    why; stopped says whether OnionPress had been stopped by then)."""
    status: str
    address: str = ""
    kept: list = field(default_factory=list)
    stopped: bool = False
    error: str = ""


def read_key_file(path):
    """--file: the base32 of a key file, or ValueError."""
    return key_manager.encode_key_file(path)


def decoded_address(key_b32):
    """The address *key_b32* derives to (ValueError for a key that is
    refused)."""
    private_key = key_manager.decode_base32_key(key_b32)
    return key_manager.derive_onion_address(
        key_manager.check_expanded_secret_key(private_key))


def installed_address(data_dir):
    """The address in vanity-keys/ (the first hostname file), '' for
    none: what `find vanity-keys -name hostname | head -1` read."""
    base = os.path.join(data_dir, "shared", "vanity-keys")
    for root, dirs, files in os.walk(base):
        dirs.sort()
        if "hostname" in files:
            try:
                with open(os.path.join(root, "hostname"), encoding="utf-8") as f:
                    return f.readline().strip()
            except (OSError, UnicodeDecodeError):
                return ""
    return ""


def key_volume_exists(containers):
    """Whether Docker has a key volume (either name: an install that never
    started on this version has not migrated yet). An unanswered question
    reads as no, as `docker volume ls | grep` did; replace_key then
    refuses when no key could be kept."""
    return any(containers.volume_state(name) == 0 for name in ADDRESS_VOLUMES)


prefix_for_address = config.prefix_for_address


follow_address_prefix = config.follow_address_prefix


# ---- The replacement ----

def _error_line(text):
    line = (text or "").strip()[:400].splitlines()[0:1]
    line = line[0] if line else ""
    return line[len("Error: "):] if line.startswith("Error: ") else line


def replace_key(key_b32, *, data_dir, docker, containers, log, release,
                hub_held_ok, linux, stdout=None):
    """Replace this site's key (steps 3-5 above, with the check and
    staging first). *release*(reason) asks the hubs and returns what may
    remain on one; *hub_held_ok*(remaining) says whether to go on with a
    hub still holding a key (the user's answer). Returns a Replacement."""
    shared = os.path.join(data_dir, "shared")
    os.makedirs(shared, exist_ok=True)
    try:
        staging = tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=data_dir)
    except OSError:
        return Replacement("failed", error=f"Could not make a staging folder in {data_dir}")
    staged_keys = os.path.join(staging, "vanity-keys")

    def fail(error, **kw):
        shutil.rmtree(staging, ignore_errors=True)
        return Replacement("failed", error=error, **kw)

    # 1. Check and stage.
    try:
        os.mkdir(staged_keys)
        address = key_manager.import_key_b32(key_b32, staged_keys)
    except Exception as e:
        error = _error_line(str(e)) or "The key could not be read"
        log(f"Key import refused: {error}")
        return fail(error)
    try:
        staged = key_manager.staged_key_address(os.path.join(staged_keys, address))
    except Exception as e:
        log(f"staged-address: {e}")
        staged = ""
    if not key_manager.is_valid_onion_address(address) or staged != address:
        error = "The imported key did not read back as one onion address"
        log(f"Key import refused: {error}")
        return fail(error)

    # Already the address Tor serves (or, with Tor not answering, the one
    # in vanity-keys/): nothing to replace.
    from .start import vanity_key_dir
    vdir = vanity_key_dir(data_dir)
    served_address = onion_hostname.read_served_address(docker)
    if served_address == address or (
            not served_address and vdir and os.path.basename(vdir) == address):
        shutil.rmtree(staging, ignore_errors=True)
        log(f"Key import: {address} is already this site's address")
        return Replacement("unchanged", address=address)

    # 2. Keep a copy of the current key.
    root = os.path.join(data_dir, key_manager.REPLACED_KEYS_DIR)
    try:
        kept = key_manager.keep_current_keys(root, [vdir] if vdir else [])
    except Exception as e:
        log(f"keep-current: {e}")
        log("Key import refused: keeping the current key failed")
        return fail(f"A copy of the current onion service key could not be kept "
                    f"(see {_log_file(data_dir)}); nothing was changed.", address=address)
    if kept:
        log("Kept a copy of the current onion service key: " + " ".join(kept))
    else:
        # Nothing could be read. Only Docker's confirmed "no such volume"
        # for every place a key lives means there is no key to keep.
        for vol in KEY_VOLUMES:
            state = containers.volume_state(vol)
            if state != 1:
                log(f"Key import refused: no copy of the current key (volume {vol}: state {state})")
                return fail("The current onion service key could not be read, so no "
                            "copy of it could be kept. Start OnionPress and try "
                            "again; nothing was changed.", address=address)
        log("Key import: no current key to keep (no key volume)")

    # 3. Only now: the hubs, then OnionPress.
    if linux:
        heartbeat_unit.stop(log)
    try:
        remaining = release("key-import")
    except Exception as e:
        log(f"onionheaven release failed: {e}")
        remaining = [{"why": "unanswered"}]
    if not hub_held_ok(remaining):
        if linux:
            heartbeat_unit.start(log)
        return fail("Cancelled: an OnionHeaven hub may still hold the current key",
                    address=address, kept=kept)
    try:
        containers.stop()
    except Exception as e:
        log(f"Stop warning: {e}")

    # 4. The staged key becomes vanity-keys/; the next start does the rest.
    vanity = os.path.join(shared, "vanity-keys")
    aside = os.path.join(staging, "replaced-vanity-keys")
    if os.path.lexists(vanity):
        try:
            os.rename(vanity, aside)
        except OSError:
            shutil.rmtree(staged_keys, ignore_errors=True)
            error = "Could not move the old vanity-keys folder aside; the key was not replaced"
            log(f"ERROR: {error} (staging folder {staging})")
            return Replacement("failed", address=address, kept=kept, stopped=True, error=error)
    try:
        os.rename(staged_keys, vanity)
    except OSError:
        try:
            os.rename(aside, vanity)
        except OSError:
            pass
        error = "Could not put the new key in place; the key was not replaced"
        log(f"ERROR: {error} (staging folder {staging})")
        return Replacement("failed", address=address, kept=kept, stopped=True, error=error)
    with open(os.path.join(data_dir, IMPORT_KEY_PENDING), "a"):
        pass
    # A replaced identity ends any unfinished move of the old key into its
    # new volume: the next start must not copy it back.
    forget_key_volume_migration(data_dir)
    shutil.rmtree(staging, ignore_errors=True)

    # 5.
    follow_address_prefix(os.path.join(data_dir, "config"), address, log)
    log(f"Imported key for address: {address}")
    return Replacement("replaced", address=address, kept=kept, stopped=True)


def _log_file(data_dir):
    return os.path.join(data_dir, "onionpress.log")


def run(args, *, paths, docker, containers, log, release, linux,
        interactive=None, environ=None, out=None, err=None, stdin=None):
    """The subcommand. *args* has key (base32, "-" for stdin, or None), file,
    check, json and confirmed (onionpress.confirm.add_arguments).

    Exit 0 done / nothing to do, 1 failed or cancelled, 3 refused for want
    of a confirmation, 4 --check found a question (confirm protocol)."""
    out = out or sys.stdout
    err = err or sys.stderr
    environ = os.environ if environ is None else environ
    stdin = stdin or sys.stdin
    if interactive is None:
        interactive = stdin is not None and stdin.isatty()
    json_output = args.json
    data_dir = paths.data_dir
    confirmed = set(_confirm.given(args, environ))
    if environ.get("ONIONPRESS_IMPORT_KEY_CONFIRMED") == "yes":
        confirmed.add("address-change")

    key_b32 = args.key or ""
    if args.file:
        try:
            key_b32 = read_key_file(args.file)
        except Exception as e:
            return _confirm.answer_error(_error_line(str(e)), json_output, out, err)
    if key_b32 == "-":
        key_b32 = (stdin.readline() or "").rstrip("\r\n")
    key_b32 = key_b32.strip()
    os.makedirs(data_dir, mode=0o700, exist_ok=True)
    if not key_b32:
        print(HELP_TEXT, file=out)
        return 0

    try:
        address = decoded_address(key_b32)
    except Exception as e:
        return _confirm.answer_error(_error_line(str(e)), json_output, out, err)
    if not key_manager.is_valid_onion_address(address):
        return _confirm.answer_error("the key did not decode to an onion address",
                                     json_output, out, err)

    current = installed_address(data_dir)
    if current == address:
        if json_output:
            print(_confirm.dumps({"ok": True, "address": address, "unchanged": True}), file=out)
        else:
            print(f"Key already installed for: {address}", file=out)
            print("Nothing to do.", file=out)
        return 0

    if key_volume_exists(containers):
        if args.check:
            if "address-change" in confirmed:
                return _confirm.answer_ok(json_output, out, address=address)
            return _confirm.answer_confirm(
                _confirm.NeedsConfirmation("address-change", current=current, new=address),
                json_output, out, err)
        print("An onion address key already exists.", file=out)
        print("", file=out)
        if current:
            print(f"Current address: {current}", file=out)
            print("", file=out)
        print("Importing a new key will REPLACE your current onion address.", file=out)
        print(f"A copy of the current key is kept in {data_dir}/replaced-keys/.", file=out)
        print("", file=out)
        if "address-change" in confirmed:
            print("Confirmed at this Mac." if not linux else "Confirmed.", file=out)
        elif interactive:
            if not _yes("Continue? [y/N] ", stdin, out):
                print("Cancelled.", file=out)
                return 1
        else:
            print("Cancelled.", file=out)
            print("Not imported: this replaces the site's onion address. Run import-key "
                  "--check --json to see the question, then again with --confirmed "
                  "address-change.", file=err)
            return _confirm.EXIT_REFUSED
    elif args.check:
        return _confirm.answer_ok(json_output, out, address=address)

    def hub_held_ok(remaining):
        # 0 and 3 (nothing, or nothing the user could still take back): go
        # on. A hub the user could still take the key back from: ask.
        if not any(i.get("why") in _RETRYABLE for i in remaining):
            return True
        if environ.get("ONIONPRESS_YES") == "true" or "hub-holds-key" in confirmed:
            return True
        if not interactive:
            return False
        return _yes("Continue anyway? The hub keeps the key listed above. [y/N] ", stdin, out)

    result = replace_key(key_b32, data_dir=data_dir, docker=docker,
                         containers=containers, log=log, release=release,
                         hub_held_ok=hub_held_ok, linux=linux)
    if result.status == "unchanged":
        print(f"Key already installed for: {address}", file=out)
        print("Nothing to do.", file=out)
        return 0
    if result.status == "failed":
        _confirm.answer_error(result.error, json_output, out, err)
        if result.stopped:
            print("OnionPress was stopped; " + (
                "start it again with 'onionpress restart'." if linux
                else "launch it again."), file=err)
        return 1

    print(f"Imported key for: {address}", file=out)
    print(f"Key files written to: {data_dir}/shared/vanity-keys/{address}", file=out)
    if result.kept:
        print("The replaced key is kept in:", file=out)
        for path in result.kept:
            print(f"  {path}", file=out)
    print("", file=out)
    if linux:
        print("Run 'onionpress restart' to use this address (it starts the "
              "heartbeat client again).", file=out)
    else:
        print("Launch OnionPress to use this address.", file=out)
    if json_output:
        print(_confirm.dumps({"ok": True, "address": address}), file=out)
    return 0


def _yes(prompt, stdin, out):
    out.write(prompt)
    out.flush()
    try:
        answer = stdin.readline().strip()
    except (OSError, EOFError):
        answer = ""
    return answer in ("y", "Y")

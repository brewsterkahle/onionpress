"""
An OnionHeaven hub's data in backups: sealed to an offline recovery key, and
checked before it is restored.

A hub's backup holds its registry and every registrant's onion service key
(registry.db and keys/<address>/ks_hs_id.ed25519_expanded_private on the
onionpress-onionheaven-data volume). A registrant's key is its address:
whoever holds it can serve anything there, for good. A backup's password is
the WordPress administrator's, which WordPress sees in plain text, so on a
hub it is not enough. The hub data is sealed to a recovery key whose
private half the hub's operator keeps offline (a password manager, paper),
never on the hub:

  - The public half is <data dir>/hub-backup-recovery.pub (~/.onionpress,
    mode 0600): outside ~/.onionpress/shared, the one folder of it the VM
    mounts, and not in config, which the Settings page can write through
    config-updates.json. `onionpress hub-recovery-key create` makes the
    pair and prints the private half once.
  - A backup puts the registry and the keys into one tar in memory (never
    into files on the host), seals it with libsodium's crypto_box_seal
    (PyNaCl's SealedBox: X25519 and XSalsa20-Poly1305 under a fresh
    ephemeral key) to the public half, and stores the result as
    onionheaven.sealed, one member of the backup's format-2 archive.
    metadata.json says so (onionheaven_data "sealed", the key id). Making a
    backup needs no secret.
  - A restore needs the private half. Without it, or with another key (told
    apart by the key id and by the seal itself, before anything is
    written), everything else is restored and the hub data is left out:
    registrants that are online send their keys again within a minute;
    sites that are offline cannot be taken over until they come back.
    Nobody loses an address: the hub never hands a key back.

Library constructions only, used as they come (PyNaCl). The key id is the
first 16 hex digits of the SHA-256 of the public key: a name that matches a
key to the backups sealed to it, not a security property.

Whatever is restored onto the hub data volume, sealed or not, is checked
first (check): a backup file is whoever made it's, and one made by code
running in WordPress, under a password WordPress knows, could carry
anything. Each keys/<name> must be a v3 onion address whose key parses, has
that address and a secret scalar that gives its public key (as the hub's
own check does, key-convert.py's keypair_error); what is written is a fresh
PEM of that key pair and a hostname of our own, never the file's bytes. The
registry is checked inside the onionheaven container before SQLite restores
it (REGISTRY_RESTORE_SH).

The second half of this module moves the hub data between the onionheaven
container and a backup: staging and sealing it for create_backup (and the
Linux launcher's), checking and writing it back for a restore.
"""

import base64
import hashlib
import hmac
import io
import os
import re
import stat
import subprocess
import tarfile
import tempfile
import threading
import zipfile
import zlib

from onionpress import backup_crypto, key_manager
from onionpress.backup_archive import (MISMATCH_MSG, _STAGE_BUFSIZE,
                                       _STAGE_COPY_FLOOR_S, _check_backup_format,
                                       _member_path, _parse_metadata,
                                       _stage_container_tar, check_archive_size,
                                       scrub_staged_tree, validate_archive_members)

try:
    from nacl import bindings as _sodium
    from nacl import exceptions as _nacl_errors
    from nacl.public import PrivateKey as _PrivateKey
    from nacl.public import PublicKey as _PublicKey
    from nacl.public import SealedBox as _SealedBox
    _SODIUM_IMPORT_ERROR = None
except ImportError as e:
    # As in backup_crypto: everything but sealing and unsealing works
    # without PyNaCl, and those say what is missing.
    _sodium = _nacl_errors = _PrivateKey = _PublicKey = _SealedBox = None
    _SODIUM_IMPORT_ERROR = e.with_traceback(None)


# The public half, in the OnionPress data directory (never shared/, which
# the VM mounts, and never config, which the Settings page can write).
PUBLIC_KEY_FILE = "hub-backup-recovery.pub"

# The sealed hub data: one member at the top of the backup's archive.
SEALED_MEMBER = "onionheaven.sealed"
# metadata.json's onionheaven_sealed_format: what the sealed tar holds.
SEALED_FORMAT = 1

# How a restore gets the private half without argv (readable by every
# local user): the environment, which the launchers pass on; removed from
# the process's environment by whoever reads it.
RECOVERY_KEY_ENV = "ONIONPRESS_HUB_RECOVERY_KEY"

PRIVATE_PREFIX = "onionpress-hub-recovery-key-1-"
PUBLIC_PREFIX = "onionpress-hub-recovery-public-1-"

KEY_FILE = "ks_hs_id.ed25519_expanded_private"
REGISTRY_FILE = "registry.db"
SQLITE_MAGIC = b"SQLite format 3\x00"

# Bounds. A registrant's PEM is about 400 bytes; a registry row a few
# hundred. A tar entry costs at least 1 KiB, so the archive bound admits
# about 100,000 registrants: far more than any hub has.
MAX_ARCHIVE_BYTES = 128 << 20
MAX_REGISTRY_BYTES = 96 << 20
MAX_KEY_FILE_BYTES = 8 << 10
MAX_KEYS = 200_000
# crypto_box_SEALBYTES: the ephemeral public key and the MAC.
SEAL_OVERHEAD = 48
MAX_SEALED_BYTES = MAX_ARCHIVE_BYTES + SEAL_OVERHEAD

_LOG_LIMIT = 20

SODIUM_MISSING_MSG = (
    "A hub's recovery key needs PyNaCl (libsodium for Python), which this "
    "OnionPress cannot load ({error}). On Debian, Ubuntu or Raspberry Pi "
    "OS, install it with: sudo apt install python3-nacl")
NOT_A_KEY_MSG = (
    "That is not an OnionPress hub recovery key: it starts with "
    f"{PRIVATE_PREFIX} followed by 52 letters and digits.")
PUBLIC_NOT_PRIVATE_MSG = (
    "That is the public half of a recovery key. A restore needs the private "
    f"half, which starts with {PRIVATE_PREFIX}.")
EXISTS_MSG = (
    "A hub backup recovery key is already set up here (key id {key_id}), "
    "and the backups made since are sealed to it. Replacing it does not "
    "change those backups: keep its private half for them. To replace it "
    "anyway, run the command again with --force.")
BAD_PUBLIC_FILE_MSG = (
    "{path} is not a hub backup recovery key (public half). Backups of this "
    "hub stop until it is fixed: remove the file and set the key up again "
    "with onionpress hub-recovery-key create.")
OTHER_KEY_MSG = (
    "That recovery key (key id {given}) is not the one this backup's hub "
    "data was sealed to (key id {wanted}).")
DOES_NOT_OPEN_MSG = (
    "The recovery key does not open this backup's hub data: the backup is "
    "damaged, or the data was not sealed to this key.")
NEWER_SEAL_MSG = (
    "This backup's hub data was sealed by a newer version of OnionPress. "
    "Update OnionPress to restore it.")


class RecoveryKeyError(ValueError):
    """Not a recovery key, or a recovery key file that cannot be used."""


class RecoveryKeyExistsError(RecoveryKeyError):
    """create refused: a public half is already set up."""


class WrongRecoveryKeyError(RecoveryKeyError):
    """The recovery key does not open this backup's hub data."""


class HubDataError(ValueError):
    """Hub data that is too large or not what a hub backup holds."""


class SodiumUnavailableError(RuntimeError):
    """PyNaCl (libsodium) cannot be imported."""


def _require_sodium():
    if _SealedBox is None:
        raise SodiumUnavailableError(
            SODIUM_MISSING_MSG.format(error=_SODIUM_IMPORT_ERROR))


# ─── The recovery key ───────────────────────────────────────────────────

def _b32(raw):
    return base64.b32encode(raw).decode("ascii").rstrip("=").lower()


def _unb32(text):
    return base64.b32decode(text.upper() + "=" * (-len(text) % 8))


_KEY_BODY_RE = re.compile(r"[a-z2-7]{52}")


def _parse(text, prefix):
    """The 32 bytes after *prefix* in *text* (whitespace anywhere ignored,
    case ignored), or None."""
    compact = "".join(text.split()).lower()
    if not compact.startswith(prefix):
        return None
    body = compact[len(prefix):]
    if not _KEY_BODY_RE.fullmatch(body):
        return None
    raw = _unb32(body)
    return raw if len(raw) == 32 else None


def format_private_key(private_raw):
    return PRIVATE_PREFIX + _b32(private_raw)


def format_public_key(public_raw):
    return PUBLIC_PREFIX + _b32(public_raw)


def parse_private_key(text):
    """The 32-byte private half from what the operator typed or pasted;
    RecoveryKeyError if it is not one."""
    if not isinstance(text, str) or not text.strip():
        raise RecoveryKeyError(NOT_A_KEY_MSG)
    raw = _parse(text, PRIVATE_PREFIX)
    if raw is None:
        if _parse(text, PUBLIC_PREFIX) is not None:
            raise RecoveryKeyError(PUBLIC_NOT_PRIVATE_MSG)
        raise RecoveryKeyError(NOT_A_KEY_MSG)
    return raw


def public_key_of(private_raw):
    _require_sodium()
    return _PrivateKey(private_raw).public_key.encode()


def key_id(public_raw):
    """A short name for a recovery key: 16 hex digits of the SHA-256 of
    its public half."""
    return hashlib.sha256(public_raw).hexdigest()[:16]


_KEY_ID_RE = re.compile(r"[0-9a-f]{16}")


def is_key_id(value):
    return isinstance(value, str) and _KEY_ID_RE.fullmatch(value) is not None


def public_key_path(data_dir):
    return os.path.join(data_dir, PUBLIC_KEY_FILE)


def _public_key_file_text(public_raw):
    return (f"# OnionPress hub backup recovery key, public half (key id "
            f"{key_id(public_raw)}).\n"
            f"# Backups of this hub's registry and registrants' keys are "
            f"sealed to it.\n"
            f"# The private half is not on this computer.\n"
            f"{format_public_key(public_raw)}\n")


def load_public_key(data_dir):
    """The public half set up on this machine, or None if there is none.

    Read without following a link, from a regular file of at most 4 KiB;
    anything else there raises RecoveryKeyError (a backup must not quietly
    go unsealed because the file was damaged)."""
    path = public_key_path(data_dir)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                     | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        return None
    except OSError as e:
        raise RecoveryKeyError(BAD_PUBLIC_FILE_MSG.format(path=path)) from e
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        data = f.read(4097) if stat.S_ISREG(st.st_mode) else b""
    lines = [line.strip() for line in data.decode("ascii", "replace").splitlines()
             if line.strip() and not line.strip().startswith("#")]
    raw = _parse(lines[0], PUBLIC_PREFIX) if len(lines) == 1 and len(data) <= 4096 else None
    if raw is None:
        raise RecoveryKeyError(BAD_PUBLIC_FILE_MSG.format(path=path))
    return raw


def create_recovery_key(data_dir, *, force=False):
    """Make a recovery key pair: write the public half to
    public_key_path(data_dir), mode 0600, and return (the private half as
    text, key id). The private half is not kept anywhere: the caller shows
    it once. Refuses (RecoveryKeyExistsError) when a public half is there
    already, unless *force*."""
    _require_sodium()
    path = public_key_path(data_dir)
    if not force and os.path.lexists(path):
        try:
            existing = key_id(load_public_key(data_dir))
        except RecoveryKeyError:
            existing = "unreadable"
        raise RecoveryKeyExistsError(EXISTS_MSG.format(key_id=existing))
    private = _PrivateKey.generate()
    public_raw = private.public_key.encode()
    os.makedirs(data_dir, mode=0o700, exist_ok=True)
    # A new file beside it, renamed over it: a link planted at the name is
    # replaced, never followed.
    tmp = os.path.join(data_dir, f".{PUBLIC_KEY_FILE}.{os.urandom(6).hex()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                 | getattr(os, "O_CLOEXEC", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(_public_key_file_text(public_raw))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return format_private_key(private.encode()), key_id(public_raw)


def check_key_for_backup(text, metadata):
    """The private half in *text*, if it is the key *metadata*'s hub data
    was sealed to; RecoveryKeyError (WrongRecoveryKeyError for another
    key) otherwise. Only the key ids are compared: the seal is checked when
    it is opened (unseal)."""
    private_raw = parse_private_key(text)
    given = key_id(public_key_of(private_raw))
    wanted = metadata.get("onionheaven_recovery_key_id") if isinstance(metadata, dict) else None
    if is_key_id(wanted) and given != wanted:
        raise WrongRecoveryKeyError(OTHER_KEY_MSG.format(given=given, wanted=wanted))
    return private_raw


def check_sealed_format(metadata):
    """NEWER_SEAL_MSG (RecoveryKeyError) for hub data sealed in a layout
    this OnionPress does not know."""
    fmt = metadata.get("onionheaven_sealed_format", SEALED_FORMAT)
    if fmt != SEALED_FORMAT:
        raise RecoveryKeyError(NEWER_SEAL_MSG)


# ─── Sealing ────────────────────────────────────────────────────────────

def seal(plaintext, public_raw):
    """crypto_box_seal of *plaintext* to the recovery key's public half."""
    _require_sodium()
    if len(plaintext) > MAX_ARCHIVE_BYTES:
        raise HubDataError("The hub data is too large to back up "
                           f"({len(plaintext)} bytes).")
    return bytes(_SealedBox(_PublicKey(public_raw)).encrypt(plaintext))


def unseal(blob, private_raw):
    """crypto_box_seal_open; WrongRecoveryKeyError when the key does not
    open it (or it was altered: the two cannot be told apart)."""
    _require_sodium()
    if not SEAL_OVERHEAD <= len(blob) <= MAX_SEALED_BYTES:
        raise WrongRecoveryKeyError(DOES_NOT_OPEN_MSG)
    try:
        return _SealedBox(_PrivateKey(private_raw)).decrypt(blob)
    except (_nacl_errors.CryptoError, TypeError, ValueError):
        raise WrongRecoveryKeyError(DOES_NOT_OPEN_MSG) from None


# ─── The hub data, in memory ────────────────────────────────────────────

class HubData:
    """A hub's registry (bytes, or None) and its registrants' keys
    ({address: PEM bytes})."""

    def __init__(self, registry=None, keys=None):
        self.registry = registry
        self.keys = dict(keys or {})

    def __bool__(self):
        return self.registry is not None or bool(self.keys)


def _label(name):
    """A name from a container or a backup, for a log line: printable
    characters only, cut to 80."""
    text = "".join(ch for ch in str(name) if ch.isprintable())
    return repr(text if len(text) <= 80 else text[:80] + "…")


def log_left_out(log_func, where, left_out):
    """Log (name, why) pairs: the first _LOG_LIMIT by name, then a count."""
    for name, why in left_out[:_LOG_LIMIT]:
        log_func(f"{where}: left {_label(name)} out ({why})")
    if len(left_out) > _LOG_LIMIT:
        log_func(f"{where}: left {len(left_out) - _LOG_LIMIT} more entries out")


def _plain_name(name):
    """A tar member's name as 'a/b', or None unless it is a plain relative
    name."""
    if "\x00" in name or name.startswith("/"):
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        return None
    return "/".join(parts)


def read_tar(fileobj, *, stream=False, where="Hub data"):
    """Read a hub data tar (HUB_DATA_TAR_SH's, or one pack() made) from
    *fileobj*; return (HubData, [(name, why) left out]).

    Kept: registry.db and keys/<v3 address>/ks_hs_id.ed25519_expanded_private,
    regular files within their bounds. A key directory's hostname is not
    kept (a restore writes its own). Links, devices and every other name
    are left out. Raises HubDataError past MAX_ARCHIVE_BYTES or MAX_KEYS:
    a backup must not quietly lose keys.
    """
    hub = HubData()
    left_out = []
    total = 0
    mode = "r|" if stream else "r:"
    try:
        with tarfile.open(fileobj=fileobj, mode=mode, bufsize=64 << 10) as tf:
            for member in tf:
                name = _plain_name(member.name)
                if member.isdir() and name is not None:
                    continue
                if name is None or not member.isreg():
                    left_out.append((member.name, "not a plain file"))
                    continue
                parts = name.split("/")
                if name == REGISTRY_FILE:
                    if member.size > MAX_REGISTRY_BYTES:
                        raise HubDataError("The hub's registry is too large to "
                                           f"back up or restore ({member.size} bytes).")
                    if hub.registry is not None:
                        left_out.append((name, "a second registry"))
                        continue
                    data = tf.extractfile(member).read(member.size)
                    total += len(data)
                    hub.registry = data
                elif len(parts) == 3 and parts[0] == "keys" and parts[2] == KEY_FILE:
                    if not key_manager.is_valid_onion_address(parts[1]):
                        left_out.append((name, "not an onion address"))
                        continue
                    if member.size > MAX_KEY_FILE_BYTES:
                        left_out.append((name, "too large for a key"))
                        continue
                    if parts[1] in hub.keys:
                        left_out.append((name, "a second key for the address"))
                        continue
                    if len(hub.keys) >= MAX_KEYS:
                        raise HubDataError("The hub data holds too many keys.")
                    data = tf.extractfile(member).read(member.size)
                    total += len(data)
                    hub.keys[parts[1]] = data
                elif len(parts) == 3 and parts[0] == "keys" and parts[2] == "hostname":
                    continue
                else:
                    left_out.append((name, "not part of a hub's registry or keys"))
                    continue
                if total > MAX_ARCHIVE_BYTES:
                    raise HubDataError("The hub data is too large to back up or restore.")
    except tarfile.TarError as e:
        raise HubDataError(f"{where}: not a readable archive ({e})") from e
    return hub, left_out


def pack(hub):
    """The hub data as one tar, in memory: registry.db and
    keys/<address>/ks_hs_id.ed25519_expanded_private, root's, 0600."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        def add(name, data):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o600
            tf.addfile(info, io.BytesIO(data))
        if hub.registry is not None:
            add(REGISTRY_FILE, hub.registry)
        for address in sorted(hub.keys):
            add(f"keys/{address}/{KEY_FILE}", hub.keys[address])
    data = buf.getvalue()
    if len(data) > MAX_ARCHIVE_BYTES:
        raise HubDataError("The hub data is too large to back up "
                           f"({len(data)} bytes).")
    return data


def unpack(data, log_func):
    """HubData from a tar pack() made (an unsealed onionheaven.sealed)."""
    hub, left_out = read_tar(io.BytesIO(data), where="Restore: the sealed hub data")
    log_left_out(log_func, "Restore: the sealed hub data", left_out)
    return hub


def read_regular(path, limit):
    """(bytes, None) for a regular file of at most *limit* bytes at *path*
    (no link followed); (None, why) otherwise; (None, None) if absent."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                     | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        return None, None
    except OSError:
        return None, "not a plain file"
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            return None, "not a plain file"
        data = f.read(limit + 1)
    if len(data) > limit:
        return None, "too large"
    return data, None


def load_dir(src, log_func):
    """HubData from a backup's plain onionheaven/ folder (*src*): what
    backups without a recovery key, and every backup before them, hold.
    Bounded like read_tar; nothing is followed."""
    hub = HubData()
    left_out = []
    data, why = read_regular(os.path.join(src, REGISTRY_FILE), MAX_REGISTRY_BYTES)
    if why:
        left_out.append((REGISTRY_FILE, why))
    hub.registry = data
    keys = os.path.join(src, "keys")
    if os.path.isdir(keys) and not os.path.islink(keys):
        for name in sorted(os.listdir(keys)):
            path = os.path.join(keys, name)
            if not stat.S_ISDIR(os.lstat(path).st_mode):
                left_out.append((f"keys/{name}", "not a folder"))
                continue
            if not key_manager.is_valid_onion_address(name):
                left_out.append((f"keys/{name}", "not an onion address"))
                continue
            if len(hub.keys) >= MAX_KEYS:
                raise HubDataError("The backup's hub data holds too many keys.")
            pem, why = read_regular(os.path.join(path, KEY_FILE), MAX_KEY_FILE_BYTES)
            if pem is None:
                left_out.append((f"keys/{name}", why or "no key file"))
                continue
            hub.keys[name] = pem
    log_left_out(log_func, "Restore: the backup's hub data", left_out)
    return hub


# ─── Checking what a backup holds ───────────────────────────────────────

def keypair_problem(expanded_key, public_key):
    """Why *expanded_key* is not *public_key*'s secret key, or None.

    The rule of the hub's own check (key-convert.py's keypair_error, which
    a test holds this to): the secret scalar (the first 32 bytes) must be
    below 2^255 and times the base point give the public key, computed by
    key_manager.derive_public_key: libsodium (PyNaCl), or without it (a
    Linux system without python3-nacl restoring an old backup) its
    pure-Python fallback.
    """
    if len(expanded_key) != 64 or len(public_key) != 32:
        return "the key has the wrong length"
    if expanded_key[31] & 0x80:
        return "the secret scalar is not below 2^255"
    try:
        derived = key_manager.derive_public_key(expanded_key)
    except ValueError:
        return "the secret scalar is zero or a multiple of the group order"
    if not hmac.compare_digest(derived, public_key):
        return "the secret scalar does not give the public key"
    return None


def registrant_key_problem(address, pem):
    """Why *pem* is not a usable key for the registrant *address*, or None."""
    if not key_manager.is_valid_onion_address(address):
        return "the folder name is not a v3 onion address"
    if len(pem) > MAX_KEY_FILE_BYTES:
        return "the key file is too large"
    try:
        private_key, public_key = key_manager.parse_openssh_key(pem)
    except Exception:
        return "the key file cannot be read"
    if key_manager.derive_onion_address(public_key) != address:
        return "the key belongs to another address"
    return keypair_problem(private_key, public_key)


def check(hub, log_func, where="Restore"):
    """The part of *hub* that may go onto the hub data volume.

    Each key must be its address's (registrant_key_problem); it comes back
    as a fresh PEM of the checked key pair, so none of the backup's own
    bytes are written. Anything else is left out and logged. The registry
    comes back as it is if it starts like an SQLite database: it is
    checked in the container before it is restored
    (REGISTRY_RESTORE_SH)."""
    good = {}
    left_out = []
    for address in sorted(hub.keys):
        problem = registrant_key_problem(address, hub.keys[address])
        if problem:
            left_out.append((f"keys/{address}", problem))
            continue
        private_key, public_key = key_manager.parse_openssh_key(hub.keys[address])
        good[address] = key_manager.build_openssh_key(private_key, public_key)
    registry = hub.registry
    if registry is not None and not registry.startswith(SQLITE_MAGIC):
        left_out.append((REGISTRY_FILE, "not an SQLite database"))
        registry = None
    log_left_out(log_func, f"{where}: the hub data", left_out)
    if hub.keys:
        log_func(f"{where}: {len(good)} of {len(hub.keys)} registrants' keys "
                 f"passed the checks")
    return HubData(registry, good)


# ─── The hub data volume, in and out of backups ─────────────────────────
# create_backup (and the Linux launcher's, through onionpress.cli) stages
# or seals the hub data with these; restores check it (check, above) and
# write it back. backup re-exports every name here, as before.


def _default_data_dir():
    """The default OnionPress data directory, resolved at call time (as
    backup._default_data_dir)."""
    return os.path.expanduser("~/.onionpress")


# The OnionHeaven hub's data: its own volume, reached through the
# onionheaven container (onionheaven_common.ONIONHEAVEN_DATA_DIR in the tor
# image). A backup keeps the registry and the registrants' keys in
# onionheaven/, and nothing else from there: not the logs, nor the copies
# the move to the volume keeps aside (keys of addresses since unregistered).
ONIONHEAVEN_DATA_DIR = "/var/lib/onionheaven"

# Prints "hub" if the volume holds hub data (a key directory, or a row in
# the registry), else "none". Never makes a registry.db (sqlite3 would).
# "none" only when that is known: no keys/ (or an empty one) and no
# registry file, an empty one, or one without a registry table or rows.
# Anything it cannot read (keys/, a registry sqlite3 fails on: corrupt,
# locked past the busy timeout, no sqlite3) prints nothing and exits 1, so
# hub_data_state() is None, never a "none" the Settings page's backup check
# would take for "not a hub". The busy timeout (SQLite's, in ms) lets a hub
# that is writing its registry answer; both queries fit in hub_data_state's
# 30 s.
HUB_DATA_STATE_SH = f"""cd {ONIONHEAVEN_DATA_DIR} || exit 1
if [ -e keys ] || [ -L keys ]; then
    k=$(ls -A keys) || exit 1
    if [ -n "$k" ]; then echo hub; exit 0; fi
fi
if [ -s registry.db ]; then
    q() {{ sqlite3 -cmd '.timeout 10000' registry.db "$1"; }}
    t=$(q "SELECT 1 FROM sqlite_master WHERE type = 'table' AND lower(name) = 'registry'") || exit 1
    if [ -n "$t" ]; then
        r=$(q 'SELECT 1 FROM registry LIMIT 1') || exit 1
        if [ -n "$r" ]; then echo hub; exit 0; fi
    fi
fi
echo none
"""


# A hub's backup without a recovery key set up: the hub data goes into the
# backup as before, under its password only, and the operator hears about it
# every time (the log; the Mac's finished-backup message and `onionpress
# backup` print HUB_DATA_UNSEALED_NOTE).
HUB_DATA_UNSEALED_WARNING = (
    "Backup: WARNING: this OnionHeaven hub has no backup recovery key, so its "
    "registry and every registrant's onion service key go into this backup "
    "protected only by the backup's password. Set one up with: onionpress "
    "hub-recovery-key create")
HUB_DATA_UNSEALED_NOTE = (
    "This hub's registrants' onion service keys are in this backup protected "
    "only by its password. Set up a recovery key so that they are sealed to "
    "a key you keep off this machine: in Terminal, run "
    "/Applications/OnionPress.app/Contents/MacOS/onionpress hub-recovery-key "
    "create (on Linux: onionpress hub-recovery-key create).")


# Logged once a restore has put the hub data in place (the menubar's restore
# summary looks for it in the CLI's output).
HUB_DATA_RESTORED_LOG = "Restore: OnionHeaven registry and keys restored"
HUB_DATA_SEALED_SKIPPED_MSG = (
    "Restore: this backup's OnionHeaven registry and registrants' keys are "
    "sealed to recovery key {key_id}, and this restore did not have that "
    "key: they were not restored. "
    "Registrants that are online send their keys again within a minute; "
    "sites that are offline cannot be taken over until they come back. To "
    "restore them later, with the recovery key: onionpress hub-recovery-key "
    "restore <backup file>")
# The Mac's restore summary, when they did not come back.
HUB_DATA_NOT_RESTORED_NOTE = (
    "OnionHeaven registry and registrants' keys: not restored (they are sealed "
    "to the hub's recovery key; see the log). Registrants that are online send "
    "their keys again within a minute; sites that are offline cannot be taken "
    "over until they come back. To restore them later, with the key: in "
    "Terminal, /Applications/OnionPress.app/Contents/MacOS/onionpress "
    "hub-recovery-key restore <backup file>")

# A tar of the registry, as sqlite3's .backup copies it (one consistent
# state while the hub writes; a copy of the file could miss its -wal or catch
# a checkpoint half done), and of keys/.
HUB_DATA_TAR_SH = f"""set -e
d=$(mktemp -d)
trap 'rm -rf "$d"' EXIT
cd {ONIONHEAVEN_DATA_DIR}
set --
if [ -s registry.db ]; then
    sqlite3 registry.db ".backup '$d/registry.db'"
    set -- -C "$d" registry.db
fi
if [ -d keys ]; then
    set -- "$@" -C {ONIONHEAVEN_DATA_DIR} keys
fi
[ "$#" -gt 0 ]
tar -cf - "$@"
"""

# Restores a registry from stdin onto the hub data volume, in the onionheaven
# container, only after checking it there: SQLite's integrity check, nothing
# in it but the hub's two tables and their primary-key indexes (no trigger,
# view or other table, which the hub's own writes could set off), a registry
# table with its address columns, and only rows whose addresses are v3 onion
# addresses (others are deleted from the copy and counted). Then SQLite's
# .restore puts it in place under the database's locks; never a copy over
# the file in use, whose -wal SQLite would apply to it. Prints "restored
# dropped=<n>", or "refused: <why>" and exits 3. The host bounds the input
# (MAX_REGISTRY_BYTES).
_V3_SQL = ("length({col}) = 62 AND substr({col}, 57) = '.onion' "
           "AND substr({col}, 1, 56) NOT GLOB '*[^a-z2-7]*'")
REGISTRY_RESTORE_SH = f"""umask 077
t=$(mktemp) || exit 1
trap 'rm -f "$t" "$t-journal" "$t-wal" "$t-shm"' EXIT
cat > "$t" || exit 1
q() {{ sqlite3 -init /dev/null -batch -bail "$t" "$1" 2>/dev/null; }}
[ "$(q 'PRAGMA integrity_check')" = ok ] || {{ echo "refused: it fails SQLite's integrity check"; exit 3; }}
n=$(q "SELECT count(*) FROM sqlite_master WHERE NOT ((type = 'table' AND name IN ('registry', 'takeover_containers')) OR (type = 'index' AND sql IS NULL AND tbl_name IN ('registry', 'takeover_containers')))")
[ "$n" = 0 ] || {{ echo "refused: it holds more than the hub's tables"; exit 3; }}
c=$(q "SELECT count(*) FROM pragma_table_info('registry') WHERE name IN ('content_address', 'healthcheck_address')")
[ "$c" = 2 ] || {{ echo "refused: it has no registry table"; exit 3; }}
d=$(q "DELETE FROM registry WHERE NOT (coalesce({_V3_SQL.format(col='content_address')}, 0) AND coalesce({_V3_SQL.format(col='healthcheck_address')}, 0)); SELECT changes();") || {{ echo "refused: its rows cannot be checked"; exit 3; }}
sqlite3 -init /dev/null -batch -bail {ONIONHEAVEN_DATA_DIR}/registry.db ".restore '$t'" || exit 1
echo "restored dropped=$d"
"""


def hub_data_state():
    """"hub" if the OnionHeaven volume holds a hub's data (registrants'
    keys, or a registry with a row), "none" if it is known not to, None if
    the onionheaven container did not answer or could not tell (a registry
    sqlite3 could not read: HUB_DATA_STATE_SH)."""
    try:
        r = subprocess.run(['docker', 'exec', 'onionheaven', 'sh', '-c', HUB_DATA_STATE_SH],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    state = r.stdout.strip() if r.returncode == 0 else ""
    return state if state in ("hub", "none") else None


def stage_hub_data(dest, log_func):
    """Copy the hub's registry (a consistent copy, HUB_DATA_TAR_SH) and
    keys/ out of the onionheaven container into *dest*, as plain files and
    folders (unpack_plain_tar), then scrub_staged_tree."""
    os.makedirs(dest, exist_ok=True)
    _stage_container_tar(['docker', 'exec', 'onionheaven', 'sh', '-c', HUB_DATA_TAR_SH],
                         dest, log_func, 'onionheaven')
    scrub_staged_tree(dest, log_func, 'onionheaven')
    keys = os.path.join(dest, 'keys')
    count = len(os.listdir(keys)) if os.path.isdir(keys) else 0
    log_func(f"Backup: OnionHeaven registry and {count} registrants' key directories copied")


def read_hub_data(log_func):
    """The hub's registry (a consistent copy, HUB_DATA_TAR_SH) and its
    registrants' keys, read out of the onionheaven container into memory
    (read_tar: plain files of the expected names only, bounded).
    For a sealed backup: none of it is written to a file in the clear."""
    proc = subprocess.Popen(['docker', 'exec', 'onionheaven', 'sh', '-c', HUB_DATA_TAR_SH],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)
    killer = threading.Timer(_STAGE_COPY_FLOOR_S, proc.kill)
    killer.daemon = True
    killer.start()
    try:
        hub, left_out = read_tar(proc.stdout, stream=True,
                                            where="Backup: the hub data")
        # Drain the tar's end padding so the container never blocks on a
        # full pipe.
        while proc.stdout.read(_STAGE_BUFSIZE):
            pass
    except BaseException:
        proc.kill()
        raise
    finally:
        proc.stdout.close()
        rc = proc.wait()
        killer.cancel()
    if rc != 0:
        raise Exception(f"onionheaven tar failed: docker={rc}")
    log_left_out(log_func, "Backup: the hub data", left_out)
    return hub


def stage_hub_backup(staging, log_func, *, data_dir=None):
    """Put this hub's registry and registrants' keys into the staged backup
    *staging*; return the metadata.json fields that say how.

    With a recovery key set up (load_public_key): read into
    memory, packed into one tar, sealed to the key and written as
    onionheaven.sealed. Without one: a plain onionheaven/ folder
    (stage_hub_data) under the backup's password only, as before, with a
    loud warning every time. A recovery key file that is there but cannot
    be read fails the backup (RecoveryKeyError) rather than
    leave the keys unsealed. create_backup's, on both platforms."""
    _data_dir = data_dir if data_dir is not None else _default_data_dir()
    public_raw = load_public_key(_data_dir)
    if public_raw is None:
        log_func(HUB_DATA_UNSEALED_WARNING)
        stage_hub_data(os.path.join(staging, 'onionheaven'), log_func)
        return {'onionheaven_data': 'unsealed'}
    hub = read_hub_data(log_func)
    sealed = seal(pack(hub), public_raw)
    fd = os.open(os.path.join(staging, SEALED_MEMBER),
                 os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(sealed)
    kid = key_id(public_raw)
    log_func(f"Backup: the OnionHeaven registry and {len(hub.keys)} registrants' "
             f"keys sealed to recovery key {kid}")
    return {'onionheaven_data': 'sealed',
            'onionheaven_sealed_format': SEALED_FORMAT,
            'onionheaven_recovery_key_id': kid}


def _keys_tar(keys):
    """A tar of checked registrants' keys for `docker cp -` into keys/:
    <address>/ (0700), its PEM and a hostname of our own (0600)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w', format=tarfile.PAX_FORMAT) as tf:
        for address in sorted(keys):
            info = tarfile.TarInfo(address)
            info.type = tarfile.DIRTYPE
            info.mode = 0o700
            tf.addfile(info)
            for name, data in ((KEY_FILE, keys[address]),
                               ('hostname', (address + '\n').encode('ascii'))):
                info = tarfile.TarInfo(f"{address}/{name}")
                info.size = len(data)
                info.mode = 0o600
                tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def write_hub_data(hub, log_func):
    """Put checked hub data (check) onto the hub data volume,
    through the onionheaven container; nothing passes through a host file.

    Keys go in as root's, by `docker cp -` of a tar made here: one
    directory per address, the PEM and the hostname. The registry goes in
    through REGISTRY_RESTORE_SH, which checks it in the container and
    restores it with SQLite's .restore, under its locks; a registry it
    refuses is left out and logged, and the keys stay restored.
    HUB_DATA_RESTORED_LOG is logged only when something was written.
    """
    wrote = False
    if hub.keys:
        subprocess.run(['docker', 'exec', 'onionheaven', 'mkdir', '-p',
                        f'{ONIONHEAVEN_DATA_DIR}/keys'],
                       capture_output=True, timeout=30, check=True)
        subprocess.run(['docker', 'cp', '-', f'onionheaven:{ONIONHEAVEN_DATA_DIR}/keys/'],
                       input=_keys_tar(hub.keys), capture_output=True, timeout=120,
                       check=True)
        log_func(f"Restore: {len(hub.keys)} registrants' keys restored")
        wrote = True
    if hub.registry is not None:
        r = subprocess.run(['docker', 'exec', '-i', 'onionheaven', 'sh', '-c',
                            REGISTRY_RESTORE_SH],
                           input=hub.registry, capture_output=True, timeout=120)
        lines = r.stdout.decode('utf-8', errors='replace').strip().splitlines()
        last = ''.join(ch for ch in (lines[-1] if lines else '') if ch.isprintable())[:200]
        if r.returncode == 3:
            log_func(f"Restore: the backup's OnionHeaven registry was not restored: "
                     f"{last.removeprefix('refused: ')}")
        elif r.returncode != 0 or not last.startswith('restored'):
            raise Exception("SQLite could not restore the OnionHeaven registry")
        else:
            wrote = True
            dropped = last.rpartition('dropped=')[2]
            if dropped not in ('', '0'):
                log_func(f"Restore: left {dropped} registry rows out (an address "
                         f"that is not a v3 onion address)")
    if wrote:
        log_func(HUB_DATA_RESTORED_LOG)
    else:
        log_func("Restore: nothing of the backup's OnionHeaven data passed the "
                 "checks; the hub data volume was not written")


def restore_hub_data(src, log_func):
    """Put a backup's plain OnionHeaven registry and keys (*src*, its
    onionheaven/ folder) onto the hub data volume: read within bounds
    (load_dir), checked (check: each key must be its
    address's) and written (write_hub_data). Nothing else from the archive
    goes there (an older backup's master-key files, logs, the move's kept
    copies, a hostname of the backup's)."""
    write_hub_data(check(load_dir(src, log_func), log_func),
                   log_func)


def open_sealed_hub_data(staging, metadata, private_raw, log_func):
    """The hub data sealed in an extracted backup (*staging*), opened with
    the recovery key's private half; unchecked (restore_container_artifacts
    checks it before writing). Raises RecoveryKeyError when the
    key does not open it or it was sealed by a newer OnionPress, and
    HubDataError when it is not a hub data archive: before
    anything is written."""
    check_sealed_format(metadata)
    blob, why = read_regular(
        os.path.join(staging, SEALED_MEMBER), MAX_SEALED_BYTES)
    if blob is None:
        raise HubDataError(
            f"The backup's sealed hub data cannot be read ({why or 'missing'}).")
    return unpack(unseal(blob, private_raw), log_func)


def read_sealed_hub_member(zip_path, password):
    """(metadata, onionheaven.sealed's bytes) from a format-2 backup file,
    for restoring a hub's sealed data on its own (`onionpress
    hub-recovery-key restore`): the inner archive is decrypted to a temp
    file, checked like any restore's (validate_archive_members,
    check_archive_size), and only that member read, within its bound.
    Raises ValueError for a backup without sealed hub data."""
    if not backup_crypto.is_encrypted_backup(zip_path):
        raise ValueError("This backup holds no sealed hub data.")
    fd, inner = tempfile.mkstemp(prefix='onionpress-hub-restore-', suffix='.zip')
    os.close(fd)
    try:
        metadata = _check_backup_format(_parse_metadata(
            backup_crypto.decrypt_archive(zip_path, password, inner)))
        try:
            with zipfile.ZipFile(inner) as zf:
                validate_archive_members(zf)
                check_archive_size(zf, os.path.getsize(zip_path))
                names = [n for n in zf.namelist()
                         if _member_path(n) == SEALED_MEMBER]
                if not names:
                    raise ValueError("This backup holds no sealed hub data.")
                if zf.getinfo(names[0]).file_size > MAX_SEALED_BYTES:
                    raise ValueError("This backup's sealed hub data is too large.")
                return metadata, zf.read(names[0])
        except (zipfile.BadZipFile, zlib.error, EOFError) as e:
            raise ValueError(MISMATCH_MSG) from e
    finally:
        os.unlink(inner)


def sealed_skipped_message(metadata):
    """HUB_DATA_SEALED_SKIPPED_MSG for *metadata*'s recovery key."""
    kid = metadata.get('onionheaven_recovery_key_id') if isinstance(metadata, dict) else None
    return HUB_DATA_SEALED_SKIPPED_MSG.format(
        key_id=kid if is_key_id(kid) else "unknown")


def _hub_readme_text(metadata):
    """What README.txt adds for a hub's backup: how its hub data is kept."""
    if metadata.get('onionheaven_data') == 'sealed':
        kid = metadata.get('onionheaven_recovery_key_id')
        return (
            "\n"
            "This is a backup of an OnionHeaven hub. Its registry and its\n"
            "registrants' onion service keys are sealed separately, in\n"
            f"{SEALED_MEMBER} inside the encrypted data (libsodium\n"
            "crypto_box_seal), to the hub's backup recovery key, key id\n"
            f"{kid if is_key_id(kid) else 'unknown'}. The hub's operator "
            "keeps its private half off the hub.\n"
            "Restoring them needs that key; everything else restores with the\n"
            "password alone. OnionPress versions from before sealed hub data\n"
            "restore the site from this backup and leave the hub data out.\n")
    if metadata.get('onionheaven_data') == 'unsealed':
        return (
            "\n"
            "This is a backup of an OnionHeaven hub. Its registry and its\n"
            "registrants' onion service keys are protected by the password\n"
            "only: no backup recovery key was set up on the hub.\n")
    return ""

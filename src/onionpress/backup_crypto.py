"""
Encrypted backup container (backup format 2).

Backups up to format 1 were zip files encrypted with `zip -P`, the legacy
PKWARE cipher ("ZipCrypto"). ZipCrypto has a long-published known-plaintext
attack, which matters here: a backup holds the onion service private key
and the WordPress database, and backups are made to leave the machine. Format
2 keeps the same zip of the same members, unencrypted, and encrypts it with
libsodium (through PyNaCl), using the library's own password hashing and
file-encryption constructions as they are, with nothing composed here:

  - key: Argon2id v1.3 (libsodium crypto_pwhash, ALG_ARGON2ID13) of the
    UTF-8 password and a fresh 16-byte random salt, 32 bytes. New backups
    use libsodium's INTERACTIVE preset, opslimit 2 and memlimit 64 MiB,
    because every backup, from any platform, must restore on a Raspberry
    Pi: 64 MiB fits beside the running site on a 1-GB device, where
    MODERATE's 256 MiB would compete with MariaDB, and Argon2 runs there
    without SIMD (libsodium has SIMD code for x86 only). Measured on Apple
    Silicon with an optimised libsodium: ~70 ms for INTERACTIVE, ~450 ms
    for MODERATE.
  - encryption: crypto_secretstream_xchacha20poly1305, libsodium's
    construction for encrypting a file as a sequence of messages. Every
    message is authenticated (Poly1305) under one key and an evolving
    nonce, so a message that was altered, dropped, reordered or taken from
    another backup does not decrypt, and the last message carries
    TAG_FINAL, so a file cut short is detected as well. A message's
    plaintext is released only after its tag verifies.

The backup file is still an ordinary, unencrypted .zip, so file pickers,
browsers and Archive Utility handle it as before. It holds README.txt (what
the file is, which OnionPress made it) and onionpress-backup.enc:

  header       72 bytes
      magic         16  b"OnionPressBackup"
      version        1  1
      kdf            1  2 = Argon2id v1.3 (crypto_pwhash_ALG_ARGON2ID13)
      cipher         1  1 = crypto_secretstream_xchacha20poly1305
      reserved       1  0
      opslimit       4  Argon2id opslimit, big-endian
      memlimit       8  Argon2id memlimit in bytes, big-endian
      salt          16  crypto_pwhash_SALTBYTES
      stream header 24  from crypto_secretstream_xchacha20poly1305_init_push
  messages, each:
      length         4  big-endian, the length of the ciphertext that
                          follows: its plaintext plus 17 (ABYTES)
      ciphertext        crypto_secretstream_xchacha20poly1305_push(plaintext,
                          additional data = the 72 header bytes, tag)

  key = crypto_pwhash(32, password, salt, opslimit, memlimit,
                      crypto_pwhash_ALG_ARGON2ID13)

Message 0 is the backup's metadata.json, tagged TAG_PUSH (the end of the
metadata), so a restore dialog can check the password and show what the
backup holds after a single message. Messages 1 to n are the zip of the
backup's members (the same tree format 1 zipped) in 1 MiB pieces, each
tagged TAG_MESSAGE except the last, which is tagged TAG_FINAL and may be
empty; the file ends right after it. Readers accept messages of up to
16 MiB of plaintext.

Message 0 failing to decrypt means a wrong password (a 128-bit Poly1305 tag
lets no wrong password through, where ZipCrypto checks a single byte); a
damaged header or first message reads the same way, as the two cannot be
told apart. After that, a message that does not decrypt, an unexpected tag,
a missing final message or bytes after it mean the file was damaged or
altered.

The Argon2id parameters are read before the password can be checked, so a
reader bounds them before running it: opslimit at most 4 and memlimit at
most 128 MiB (twice the shipped values each), so a crafted file can neither
exhaust a 1-GB device's memory nor stall a restore for long. A header asking
for more is refused as coming from a newer OnionPress; one asking for less
than libsodium's minimum is damaged.
"""

import os
import tempfile
import zipfile
from contextlib import contextmanager

try:
    from nacl import bindings as _sodium
    from nacl import exceptions as _nacl_errors
    from nacl.pwhash import argon2id as _argon2id
    _SODIUM_IMPORT_ERROR = None
except ImportError as e:
    # The Linux system python3 without python3-nacl, or a broken bundle.
    # Everything else in OnionPress (format-1 restores included) works
    # without it; format-2 backups say what is missing.
    _sodium = _nacl_errors = _argon2id = None
    _SODIUM_IMPORT_ERROR = e.with_traceback(None)


# metadata.json's 'backup_format'. Format 1 (the ZipCrypto zip) predates the
# field, so a backup without it is format 1.
BACKUP_FORMAT = 2

PAYLOAD_NAME = 'onionpress-backup.enc'
README_NAME = 'README.txt'

MAGIC = b'OnionPressBackup'
VERSION = 1
KDF_ARGON2ID13 = 2          # libsodium's crypto_pwhash_ALG_ARGON2ID13
CIPHER_SECRETSTREAM = 1     # crypto_secretstream_xchacha20poly1305

# What new backups are written with: libsodium's Argon2id INTERACTIVE
# preset (crypto_pwhash_argon2id_OPSLIMIT/MEMLIMIT_INTERACTIVE), and 1 MiB
# messages. A reader takes the KDF parameters from the header.
OPSLIMIT = 2
MEMLIMIT = 64 * 1024 * 1024
CHUNK_SIZE = 1 << 20

# Bounds on what a header may ask for (see the docstring). The minimums are
# libsodium's own for Argon2id.
_OPSLIMIT_MIN = 1
_MEMLIMIT_MIN = 8 * 1024
_OPSLIMIT_MAX = 4
_MEMLIMIT_MAX = 128 * 1024 * 1024
_MESSAGE_MAX = 16 * 1024 * 1024

_HEADER_LEN = 72
_SALT_LEN = 16
_KEY_LEN = 32
_ABYTES = 17
_LENGTH_LEN = 4

WRONG_PASSWORD_MSG = "Incorrect password for this backup."
DAMAGED_MSG = ("This backup file is damaged or was changed after it was "
               "made, so it cannot be restored.")
NEWER_MSG = ("This backup was made by a newer version of OnionPress. "
             "Update OnionPress to restore it.")
SODIUM_MISSING_MSG = (
    "Encrypted backups need PyNaCl (libsodium for Python), which this "
    "OnionPress cannot load ({error}). On Debian, Ubuntu or Raspberry Pi "
    "OS, install it with: sudo apt install python3-nacl")


class WrongPasswordError(ValueError):
    """The password does not open this backup."""


class DamagedBackupError(ValueError):
    """The backup failed its integrity check: truncated, corrupted or altered."""


class NewerBackupError(ValueError):
    """The backup uses a format this OnionPress does not know."""


class SodiumUnavailableError(RuntimeError):
    """PyNaCl (libsodium) cannot be imported, so format 2 is unavailable."""


def _require_sodium():
    if _sodium is None:
        raise SodiumUnavailableError(
            SODIUM_MISSING_MSG.format(error=_SODIUM_IMPORT_ERROR))
    return _sodium


def _derive_key(password, salt, opslimit, memlimit):
    """Argon2id the password into the 32-byte secretstream key."""
    return _argon2id.kdf(_KEY_LEN, password.encode('utf-8'), salt,
                         opslimit=opslimit, memlimit=memlimit)


class EncryptedWriter:
    """Write onionpress-backup.enc to the binary stream *dst*.

    The header and the metadata message go out at construction; archive
    bytes passed to write() go out in CHUNK_SIZE messages. close() writes the
    final message, so a writer abandoned before close() (say, because `zip`
    failed) never leaves a file that would pass as complete.
    """

    def __init__(self, dst, password, metadata_bytes):
        sodium = _require_sodium()
        if len(metadata_bytes) > CHUNK_SIZE:
            raise ValueError("metadata.json is larger than one message")
        opslimit, memlimit = OPSLIMIT, MEMLIMIT
        salt = os.urandom(_SALT_LEN)
        key = _derive_key(password, salt, opslimit, memlimit)
        self._state = sodium.crypto_secretstream_xchacha20poly1305_state()
        stream_header = sodium.crypto_secretstream_xchacha20poly1305_init_push(
            self._state, key)
        self._header = (MAGIC
                        + bytes([VERSION, KDF_ARGON2ID13, CIPHER_SECRETSTREAM, 0])
                        + opslimit.to_bytes(4, 'big')
                        + memlimit.to_bytes(8, 'big')
                        + salt + stream_header)
        self._dst = dst
        dst.write(self._header)
        self._push(metadata_bytes,
                   sodium.crypto_secretstream_xchacha20poly1305_TAG_PUSH)
        self._buf = bytearray()

    def write(self, data):
        self._buf += data
        # Keep at least one byte back: the final message must be written by
        # close(), even when the archive is an exact multiple of the chunk.
        while len(self._buf) > CHUNK_SIZE:
            self._push(bytes(self._buf[:CHUNK_SIZE]),
                       _sodium.crypto_secretstream_xchacha20poly1305_TAG_MESSAGE)
            del self._buf[:CHUNK_SIZE]
        return len(data)

    def close(self):
        if self._buf is None:
            return
        self._push(bytes(self._buf),
                   _sodium.crypto_secretstream_xchacha20poly1305_TAG_FINAL)
        self._buf = None

    def _push(self, plaintext, tag):
        ciphertext = _sodium.crypto_secretstream_xchacha20poly1305_push(
            self._state, plaintext, self._header, tag)
        self._dst.write(len(ciphertext).to_bytes(_LENGTH_LEN, 'big'))
        self._dst.write(ciphertext)


def _read_exact(src, n):
    """Read exactly *n* bytes, fewer only at end of stream."""
    parts = []
    while n > 0:
        part = src.read(n)
        if not part:
            break
        parts.append(part)
        n -= len(part)
    return b''.join(parts)


class _Reader:
    """Check the header of an onionpress-backup.enc stream and derive its
    key, then hand out its messages in order, each verified by libsodium
    before its plaintext is returned."""

    def __init__(self, src, password):
        sodium = _require_sodium()
        header = _read_exact(src, _HEADER_LEN)
        if len(header) < _HEADER_LEN or header[:16] != MAGIC:
            raise DamagedBackupError(DAMAGED_MSG)
        if tuple(header[16:20]) != (VERSION, KDF_ARGON2ID13,
                                    CIPHER_SECRETSTREAM, 0):
            raise NewerBackupError(NEWER_MSG)
        opslimit = int.from_bytes(header[20:24], 'big')
        memlimit = int.from_bytes(header[24:32], 'big')
        if opslimit < _OPSLIMIT_MIN or memlimit < _MEMLIMIT_MIN:
            raise DamagedBackupError(DAMAGED_MSG)
        if opslimit > _OPSLIMIT_MAX or memlimit > _MEMLIMIT_MAX:
            raise NewerBackupError(NEWER_MSG)
        salt, stream_header = header[32:48], header[48:72]
        key = _derive_key(password, salt, opslimit, memlimit)
        self._state = sodium.crypto_secretstream_xchacha20poly1305_state()
        sodium.crypto_secretstream_xchacha20poly1305_init_pull(
            self._state, stream_header, key)
        self._src = src
        self._header = header
        self._first = True

    def read_message(self):
        """Return (plaintext, tag) of the next message."""
        length = _read_exact(self._src, _LENGTH_LEN)
        if len(length) < _LENGTH_LEN:
            raise DamagedBackupError(DAMAGED_MSG)
        length = int.from_bytes(length, 'big')
        if not _ABYTES <= length <= _MESSAGE_MAX + _ABYTES:
            raise DamagedBackupError(DAMAGED_MSG)
        ciphertext = _read_exact(self._src, length)
        if len(ciphertext) < length:
            raise DamagedBackupError(DAMAGED_MSG)
        try:
            plaintext, tag = _sodium.crypto_secretstream_xchacha20poly1305_pull(
                self._state, ciphertext, self._header)
        except _nacl_errors.CryptoError:
            if self._first:
                raise WrongPasswordError(WRONG_PASSWORD_MSG) from None
            raise DamagedBackupError(DAMAGED_MSG) from None
        self._first = False
        return plaintext, tag

    def read_metadata(self):
        data, tag = self.read_message()
        if tag != _sodium.crypto_secretstream_xchacha20poly1305_TAG_PUSH:
            raise DamagedBackupError(DAMAGED_MSG)
        return data


def is_encrypted_backup(zip_path):
    """True if *zip_path* is a format-2 backup (a zip holding the payload).
    False for a format-1 backup and for anything that is not a zip."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            return PAYLOAD_NAME in zf.namelist()
    except (zipfile.BadZipFile, OSError):
        return False


def read_metadata(zip_path, password):
    """Check the password and return the backup's metadata.json bytes,
    reading only the header and the first message."""
    try:
        with zipfile.ZipFile(zip_path) as zf, zf.open(PAYLOAD_NAME) as payload:
            return _Reader(payload, password).read_metadata()
    except zipfile.BadZipFile as e:
        raise DamagedBackupError(DAMAGED_MSG) from e


def decrypt_archive(zip_path, password, out_path):
    """Decrypt the backup's archive (the inner zip) into *out_path*; return
    the metadata.json bytes. Raises WrongPasswordError before writing
    anything, and DamagedBackupError if any message fails its check or the
    final one is missing — out_path then holds only a verified prefix, and
    the caller must discard it."""
    sodium = _require_sodium()
    tag_message = sodium.crypto_secretstream_xchacha20poly1305_TAG_MESSAGE
    tag_final = sodium.crypto_secretstream_xchacha20poly1305_TAG_FINAL
    try:
        with zipfile.ZipFile(zip_path) as zf, zf.open(PAYLOAD_NAME) as payload:
            reader = _Reader(payload, password)
            metadata = reader.read_metadata()
            with open(out_path, 'wb') as out:
                while True:
                    data, tag = reader.read_message()
                    if tag not in (tag_message, tag_final):
                        raise DamagedBackupError(DAMAGED_MSG)
                    out.write(data)
                    if tag == tag_final:
                        break
            if payload.read(1):
                raise DamagedBackupError(DAMAGED_MSG)
            return metadata
    except zipfile.BadZipFile as e:
        # zipfile's own CRC check on the payload member; libsodium normally
        # catches the damage first.
        raise DamagedBackupError(DAMAGED_MSG) from e


@contextmanager
def create(output_path, password, metadata_bytes, readme):
    """Write a format-2 backup to *output_path*; yields the EncryptedWriter
    that takes the archive bytes.

    The file is built beside *output_path* under a temporary name, owner
    read/write only, and renamed into place only when the with-block
    completes; if it raises, the partial file is removed and nothing
    appears at *output_path*.
    """
    _require_sodium()
    out_dir = os.path.dirname(os.path.abspath(output_path))
    fd, tmp_path = tempfile.mkstemp(dir=out_dir, prefix='.onionpress-backup-',
                                    suffix='.partial')
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_STORED) as outer:
            outer.writestr(README_NAME, readme)
            # force_zip64: the payload's size is unknown up front and may
            # pass 4 GiB.
            with outer.open(PAYLOAD_NAME, 'w', force_zip64=True) as member:
                writer = EncryptedWriter(member, password, metadata_bytes)
                yield writer
                writer.close()
        os.replace(tmp_path, output_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def readme_text(onionpress_version):
    """README.txt for the outer zip — the one part anyone can read."""
    return (
        "OnionPress backup\n"
        "=================\n"
        "\n"
        "This zip is an OnionPress site backup: the onion service key, the\n"
        "WordPress database and wp-content, encrypted with the password\n"
        "chosen when the backup was made. The encrypted data is\n"
        f"{PAYLOAD_NAME}.\n"
        "\n"
        f"Made by OnionPress {onionpress_version or 'unknown'}.\n"
        "To restore it, use that version or a newer one: Restore... in the\n"
        "OnionPress menu, \"Restore from backup...\" on the welcome screen, or\n"
        "`onionpress restore` on the command line. Versions from before\n"
        f"backup format {BACKUP_FORMAT} cannot read it and call it not a "
        "valid backup.\n"
        "\n"
        f"Format: OnionPress backup format {BACKUP_FORMAT} (libsodium: "
        "Argon2id,\n"
        "XChaCha20-Poly1305 secretstream), specified in\n"
        "src/onionpress/backup_crypto.py at\n"
        "https://github.com/brewsterkahle/onionpress\n"
    )

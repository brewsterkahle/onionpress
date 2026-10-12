"""
Encrypted backup container (backup format 2).

Backups up to format 1 were zip files encrypted with `zip -P`, the legacy
PKWARE cipher ("ZipCrypto"). ZipCrypto has a long-published known-plaintext
attack, which matters here: a backup holds the onion service private key
and the WordPress database, and backups are made to leave the machine. Format
2 keeps the same zip of the same members, unencrypted, and wraps it in
password-based authenticated encryption built only from the Python standard
library, so neither the py2app bundle nor the Linux system python3 needs a
new dependency:

  - key derivation: scrypt (RFC 7914) of the UTF-8 password with a fresh
    16-byte random salt, N=2^17, r=8, p=1 (128 MiB, ~0.3 s), split into a
    32-byte encryption key and a 32-byte MAC key;
  - encryption: XOR with a SHAKE256 (FIPS 202) keystream, a fresh one per
    record: SHAKE256(enc_key || nonce || stream || index). SHAKE256 with a
    fixed-length secret key prefix is a PRF; ML-KEM (FIPS 203) uses it as
    its PRF the same way;
  - authentication: HMAC-SHA256 over every record (encrypt-then-MAC), with
    the record's stream, index and last-record flag inside the MAC, so
    reordered, dropped, truncated or spliced records all fail (the STREAM
    construction). A record's plaintext is released only after its tag
    verifies.

The backup file is still an ordinary, unencrypted .zip, so file pickers,
browsers and Archive Utility handle it as before. It holds README.txt (what
the file is, which OnionPress made it) and onionpress-backup.enc:

  header       56 bytes
      magic        16  b"OnionPressBackup"
      version       1  1
      kdf           1  1 = scrypt
      log2(N)       1  scrypt cost
      r             1  scrypt block size
      p             1  scrypt parallelism
      suite         1  1 = SHAKE256 keystream + HMAC-SHA256
      log2(chunk)   1  record size; 20 = 1 MiB
      reserved      1  0
      salt         16
      nonce        16
  header tag   32 bytes  HMAC-SHA256(mac_key, "OPBK-header" || header)
  records, each:
      stream        1  0 = metadata.json, 1 = archive
      flags         1  bit 0 = last record of its stream
      length        4  big-endian, at most the chunk size
      ciphertext    length bytes: plaintext XOR the first `length` bytes of
                      SHAKE256(enc_key || nonce || stream || index)
      tag          32  HMAC-SHA256(mac_key, "OPBK-record" || header tag ||
                         stream || flags || length || index || ciphertext)
  index is the record's 0-based position within its stream, as 8 bytes
  big-endian. Stream 0 is a single record holding the backup's
  metadata.json, so a restore dialog can check the password and show what
  the backup holds without decrypting the archive. Stream 1 is the zip of
  the backup's members (the same tree format 1 zipped) in chunk-size
  records; the file ends right after its last record.

A header tag that does not verify means a wrong password (a 256-bit tag lets
no wrong password through, where ZipCrypto checks a single byte); a record
tag that does not verify after that means the file was damaged or altered.
"""

import hashlib
import hmac
import os
import tempfile
import zipfile
from contextlib import contextmanager


# metadata.json's 'backup_format'. Format 1 (the ZipCrypto zip) predates the
# field, so a backup without it is format 1.
BACKUP_FORMAT = 2

PAYLOAD_NAME = 'onionpress-backup.enc'
README_NAME = 'README.txt'

MAGIC = b'OnionPressBackup'
VERSION = 1
KDF_SCRYPT = 1
SUITE_SHAKE256_HMAC_SHA256 = 1
STREAM_METADATA = 0
STREAM_ARCHIVE = 1
FLAG_LAST = 0x01

# What new backups are written with. A reader takes the values from the
# header, so these can be raised later without touching old backups.
SCRYPT_LOG2_N = 17
SCRYPT_R = 8
SCRYPT_P = 1
CHUNK_LOG2 = 20

# The header is only trusted once its tag verifies, and the tag can only be
# checked after scrypt has run on the header's parameters — so bound what a
# header may ask for (memory, time, record size) before running it.
_MAX_SCRYPT_BYTES = 1 << 30
_MAX_SCRYPT_P = 16
_MIN_CHUNK_LOG2 = 10
_MAX_CHUNK_LOG2 = 24

_HEADER_LEN = 56
_TAG_LEN = 32
_RECORD_HEAD_LEN = 6
_HEADER_LABEL = b'OPBK-header'
_RECORD_LABEL = b'OPBK-record'

WRONG_PASSWORD_MSG = "Incorrect password for this backup."
DAMAGED_MSG = ("This backup file is damaged or was changed after it was "
               "made, so it cannot be restored.")
NEWER_MSG = ("This backup was made by a newer version of OnionPress. "
             "Update OnionPress to restore it.")

# Salt and nonce source; tests pin it to produce a known-answer vector.
_random_bytes = os.urandom


class WrongPasswordError(ValueError):
    """The password does not open this backup."""


class DamagedBackupError(ValueError):
    """The backup failed its integrity check: truncated, corrupted or altered."""


class NewerBackupError(ValueError):
    """The backup uses a format this OnionPress does not know."""


def _derive_keys(password, salt, log2_n, r, p):
    """scrypt the password into (enc_key, mac_key)."""
    if not hasattr(hashlib, 'scrypt'):
        # hashlib.scrypt needs OpenSSL 1.1+; every Python OnionPress ships
        # with or supports has it, but say so plainly if one doesn't.
        raise RuntimeError("This Python has no hashlib.scrypt (OpenSSL 1.1 "
                           "or newer is needed for encrypted backups)")
    n = 1 << log2_n
    # OpenSSL refuses to run unless maxmem covers its V and B arrays,
    # 128*r*(N+2) + 128*r*p bytes; the 1 MiB is slack.
    key = hashlib.scrypt(password.encode('utf-8'), salt=salt, n=n, r=r, p=p,
                         maxmem=128 * r * (n + 2 + p) + (1 << 20), dklen=64)
    return key[:32], key[32:]


def _crypt(enc_key, nonce, stream, index, data):
    """XOR *data* with its record's keystream (encrypts and decrypts)."""
    if not data:
        return b''
    pad = hashlib.shake_256(
        enc_key + nonce + bytes([stream]) + index.to_bytes(8, 'big')
    ).digest(len(data))
    # Whole-buffer XOR through int: C speed, no per-byte Python loop.
    return (int.from_bytes(data, 'little') ^ int.from_bytes(pad, 'little')
            ).to_bytes(len(data), 'little')


def _record_tag(mac_key, header_tag, head, index, ciphertext):
    h = hmac.new(mac_key, _RECORD_LABEL, hashlib.sha256)
    h.update(header_tag)
    h.update(head)
    h.update(index.to_bytes(8, 'big'))
    h.update(ciphertext)
    return h.digest()


class EncryptedWriter:
    """Write onionpress-backup.enc to the binary stream *dst*.

    The header and the metadata record go out at construction; archive bytes
    passed to write() go out in chunk-size records. close() writes the last
    record, so a writer abandoned before close() (say, because `zip` failed)
    never leaves a file that would pass as complete.
    """

    def __init__(self, dst, password, metadata_bytes):
        self._dst = dst
        self._chunk = 1 << CHUNK_LOG2
        if len(metadata_bytes) > self._chunk:
            raise ValueError("metadata.json is larger than one record")
        salt = _random_bytes(16)
        self._nonce = _random_bytes(16)
        header = (MAGIC
                  + bytes([VERSION, KDF_SCRYPT, SCRYPT_LOG2_N, SCRYPT_R,
                           SCRYPT_P, SUITE_SHAKE256_HMAC_SHA256, CHUNK_LOG2, 0])
                  + salt + self._nonce)
        self._enc_key, self._mac_key = _derive_keys(
            password, salt, SCRYPT_LOG2_N, SCRYPT_R, SCRYPT_P)
        self._header_tag = hmac.new(
            self._mac_key, _HEADER_LABEL + header, hashlib.sha256).digest()
        dst.write(header + self._header_tag)
        self._emit(STREAM_METADATA, 0, metadata_bytes, last=True)
        self._buf = bytearray()
        self._index = 0

    def write(self, data):
        self._buf += data
        # Keep at least one byte back: the final record must be written by
        # close(), even when the archive is an exact multiple of the chunk.
        while len(self._buf) > self._chunk:
            self._emit(STREAM_ARCHIVE, self._index,
                       bytes(self._buf[:self._chunk]), last=False)
            del self._buf[:self._chunk]
            self._index += 1
        return len(data)

    def close(self):
        if self._buf is None:
            return
        self._emit(STREAM_ARCHIVE, self._index, bytes(self._buf), last=True)
        self._buf = None

    def _emit(self, stream, index, plaintext, last):
        ciphertext = _crypt(self._enc_key, self._nonce, stream, index, plaintext)
        head = (bytes([stream, FLAG_LAST if last else 0])
                + len(ciphertext).to_bytes(4, 'big'))
        self._dst.write(head)
        self._dst.write(ciphertext)
        self._dst.write(_record_tag(self._mac_key, self._header_tag, head,
                                    index, ciphertext))


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
    """Verify the header of an onionpress-backup.enc stream, then hand out
    its records in order, each checked before its plaintext is returned."""

    def __init__(self, src, password):
        self._src = src
        header = _read_exact(src, _HEADER_LEN)
        if len(header) < _HEADER_LEN or header[:16] != MAGIC:
            raise DamagedBackupError(DAMAGED_MSG)
        version, kdf, log2_n, r, p, suite, chunk_log2, reserved = header[16:24]
        if (version != VERSION or kdf != KDF_SCRYPT
                or suite != SUITE_SHAKE256_HMAC_SHA256 or reserved != 0):
            raise NewerBackupError(NEWER_MSG)
        if not (1 <= log2_n and 1 <= r and 1 <= p <= _MAX_SCRYPT_P
                and 128 * r * (1 << log2_n) <= _MAX_SCRYPT_BYTES
                and _MIN_CHUNK_LOG2 <= chunk_log2 <= _MAX_CHUNK_LOG2):
            raise DamagedBackupError(DAMAGED_MSG)
        salt, self._nonce = header[24:40], header[40:56]
        self._chunk = 1 << chunk_log2
        tag = _read_exact(src, _TAG_LEN)
        if len(tag) < _TAG_LEN:
            raise DamagedBackupError(DAMAGED_MSG)
        self._enc_key, self._mac_key = _derive_keys(password, salt, log2_n, r, p)
        expected = hmac.new(self._mac_key, _HEADER_LABEL + header,
                            hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected):
            raise WrongPasswordError(WRONG_PASSWORD_MSG)
        self._header_tag = tag

    def read_record(self, stream, index):
        """Return (plaintext, is_last) of the next record, which must be
        record *index* of *stream*."""
        head = _read_exact(self._src, _RECORD_HEAD_LEN)
        if len(head) < _RECORD_HEAD_LEN:
            raise DamagedBackupError(DAMAGED_MSG)
        length = int.from_bytes(head[2:6], 'big')
        if head[0] != stream or head[1] & ~FLAG_LAST or length > self._chunk:
            raise DamagedBackupError(DAMAGED_MSG)
        body = _read_exact(self._src, length + _TAG_LEN)
        if len(body) < length + _TAG_LEN:
            raise DamagedBackupError(DAMAGED_MSG)
        ciphertext, tag = body[:length], body[length:]
        expected = _record_tag(self._mac_key, self._header_tag, head, index,
                               ciphertext)
        if not hmac.compare_digest(tag, expected):
            raise DamagedBackupError(DAMAGED_MSG)
        plaintext = _crypt(self._enc_key, self._nonce, stream, index, ciphertext)
        return plaintext, bool(head[1] & FLAG_LAST)

    def read_metadata(self):
        data, last = self.read_record(STREAM_METADATA, 0)
        if not last:
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
    reading only the header and the metadata record."""
    try:
        with zipfile.ZipFile(zip_path) as zf, zf.open(PAYLOAD_NAME) as payload:
            return _Reader(payload, password).read_metadata()
    except zipfile.BadZipFile as e:
        raise DamagedBackupError(DAMAGED_MSG) from e


def decrypt_archive(zip_path, password, out_path):
    """Decrypt the backup's archive (the inner zip) into *out_path*; return
    the metadata.json bytes. Raises WrongPasswordError before writing
    anything, and DamagedBackupError if any record fails its check — out_path
    then holds only a verified prefix, and the caller must discard it."""
    try:
        with zipfile.ZipFile(zip_path) as zf, zf.open(PAYLOAD_NAME) as payload:
            reader = _Reader(payload, password)
            metadata = reader.read_metadata()
            with open(out_path, 'wb') as out:
                index = 0
                while True:
                    data, last = reader.read_record(STREAM_ARCHIVE, index)
                    out.write(data)
                    if last:
                        break
                    index += 1
            if payload.read(1):
                raise DamagedBackupError(DAMAGED_MSG)
            return metadata
    except zipfile.BadZipFile as e:
        # zipfile's own CRC check on the payload member; the record tags
        # normally catch the damage first.
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
        f"Format: OnionPress backup format {BACKUP_FORMAT} (scrypt, SHAKE256, "
        "HMAC-SHA256),\n"
        "specified in src/onionpress/backup_crypto.py at\n"
        "https://github.com/brewsterkahle/onionpress\n"
    )

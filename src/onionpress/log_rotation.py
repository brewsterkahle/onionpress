"""Rotating log files for OnionPress.

Handles daily rotation with size limits, home-directory scrubbing,
total disk usage caps for the logs directory, and gzip compression
of rolled (closed) files so older history takes ~10–20x less disk
and bandwidth during analytics upload.
"""

import glob
import gzip
import json
import os
import re
import shutil
import threading
from datetime import datetime, timezone


# --- Shipped-watermark state (module-level, shared across instances) ---
#
# analytics_sharing.py records the latest successfully-uploaded filename
# per log-type here. _enforce_total_size uses it to avoid deleting
# files we haven't managed to ship yet, so a week-long OnionHome outage
# doesn't silently cost us log history. When the hard ceiling is reached
# we still delete unshipped files (oldest first) — data loss eventually
# beats disk exhaustion — but the soft cap only trims shipped files.

_SHIPPED_LOCK = threading.Lock()
_SHIPPED_FILE = ".shipped.json"


def _shipped_state_path(base_dir):
    return os.path.join(base_dir, _SHIPPED_FILE)


def read_shipped(base_dir):
    """Return ``{log_type: latest-shipped-filename}`` or ``{}``."""
    try:
        with open(_shipped_state_path(base_dir), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}
    except (OSError, ValueError):
        pass
    return {}


def mark_shipped(base_dir, log_type, name):
    """Move the watermark for *log_type* forward to *name* (max-semantics).

    Names sort correctly under plain string comparison because the
    rotation scheme embeds ``YYYY-MM-DD-NNN`` in every filename and
    ``.log < .log.gz`` (the ``.log`` precursor is the partial upload
    of the same roll).
    """
    with _SHIPPED_LOCK:
        data = read_shipped(base_dir)
        if name > data.get(log_type, ""):
            data[log_type] = name
            tmp = _shipped_state_path(base_dir) + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                os.replace(tmp, _shipped_state_path(base_dir))
            except OSError:
                pass


_FILENAME_RE = re.compile(
    r"^(?P<type>.+?)-\d{4}-\d{2}-\d{2}-\d+\.log(?:\.gz)?$"
)


def extract_log_type(filename):
    """Return the ``<log_type>`` prefix of a rotated filename, or ``None``.

    ``onionpress-2026-04-21-001.log`` → ``onionpress``
    ``container-onionheaven-takeover-5-2026-04-21-001.log.gz`` →
    ``container-onionheaven-takeover-5``
    ``launcher.log`` → ``launcher``
    """
    if filename in ("launcher.log", "launcher.log.gz"):
        return "launcher"
    m = _FILENAME_RE.fullmatch(filename)
    return m.group("type") if m else None


def current_log_path(base_dir, log_type):
    """The newest `{log_type}-YYYY-MM-DD-NNN.log` under *base_dir*, or ""
    (another process's active log, read only: the menubar opens the
    daemon's onionpress log in View Logs without writing to it)."""
    try:
        names = [n for n in os.listdir(base_dir)
                 if n.startswith(f"{log_type}-") and n.endswith(".log")
                 and extract_log_type(n) == log_type]
    except OSError:
        return ""
    if not names:
        return ""
    return os.path.join(base_dir, sorted(names)[-1])


class RotatingLog:
    """Thread-safe rotating log writer.

    Files are named ``{log_type}-YYYY-MM-DD-NNN.log`` under *base_dir*.
    A new file is started when the UTC date changes or the current file
    exceeds *max_size* bytes.  After each roll the total size of
    *base_dir* is checked and oldest files are pruned to stay under
    *max_total_size*.
    """

    def __init__(self, base_dir, log_type, max_size=5_242_880,
                 max_total_size=104_857_600, scrub_fn=None):
        self._base_dir = base_dir
        self._log_type = log_type
        self._max_size = max_size
        self._max_total_size = max_total_size
        # Optional callable(bytes) -> bytes run on each rolled file
        # before it is gzip-compressed. Used by analytics-sharing to
        # strip sensitive content (visitor IPs, auth tokens, …) from
        # the rolled file so only the active log ever contains raw
        # data locally. See onionpress.redact.make_scrub_fn.
        self._scrub_fn = scrub_fn

        # Pre-compute the home directory string to scrub
        self._home = os.path.expanduser("~")

        self._lock = threading.Lock()

        os.makedirs(base_dir, exist_ok=True)

        # Determine today's date and pick up any existing sequence
        self._current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._seq = self._find_next_seq(self._current_date)
        self._path = self._make_path(self._current_date, self._seq)

    # -- public API --------------------------------------------------------

    def write(self, message):
        """Append *message* to the current log file (thread-safe).

        Scrubs the user's home directory path before writing.
        Rolls to a new file if the UTC date changed or size exceeded.
        """
        message = message.replace(self._home, "~")

        prev_path = None  # if we rolled, gzip-compress the just-closed file

        with self._lock:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            rolled = False

            if today != self._current_date:
                prev_path = self._path
                self._current_date = today
                self._seq = self._find_next_seq(today)
                self._path = self._make_path(today, self._seq)
                rolled = True
            elif os.path.exists(self._path):
                try:
                    if os.path.getsize(self._path) >= self._max_size:
                        prev_path = self._path
                        self._seq += 1
                        self._path = self._make_path(self._current_date, self._seq)
                        rolled = True
                except OSError:
                    pass

        # Write outside the lock — O_APPEND is atomic on POSIX
        try:
            fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(message)
        except Exception:
            pass

        if rolled:
            # Compress the file we just rolled off of, in a background
            # thread so we don't block the write path. Enforcement runs
            # after so size accounting includes the compressed artifact.
            if prev_path and os.path.exists(prev_path) and not prev_path.endswith(".gz"):
                threading.Thread(
                    target=self._gzip_file, args=(prev_path,), daemon=True,
                ).start()
            self._enforce_total_size()

    def current_path(self):
        """Return the path of the currently active log file."""
        return self._path

    def completed_files(self):
        """Return metadata for all files that are NOT the current active file.

        Each entry is ``{"name": str, "size": int, "path": str}``.
        Includes both plain ``.log`` files (still-compressing or pre-gzip
        era) and ``.log.gz`` files (rolled + compressed).
        """
        current = os.path.basename(self._path)
        result = []
        patterns = [
            os.path.join(self._base_dir, f"{self._log_type}-*.log"),
            os.path.join(self._base_dir, f"{self._log_type}-*.log.gz"),
        ]
        seen = set()
        for pattern in patterns:
            for p in sorted(glob.glob(pattern)):
                name = os.path.basename(p)
                if name == current or name in seen:
                    continue
                seen.add(name)
                try:
                    size = os.path.getsize(p)
                except OSError:
                    continue
                result.append({"name": name, "size": size, "path": p})
        return result

    # -- internals ---------------------------------------------------------

    def _make_path(self, date_str, seq):
        return os.path.join(self._base_dir, f"{self._log_type}-{date_str}-{seq:03d}.log")

    def _find_next_seq(self, date_str):
        """Find the highest existing sequence number for *date_str*, or start at 1.

        Considers both ``.log`` (still-active or not-yet-compressed) and
        ``.log.gz`` (rolled + compressed) files so we don't reuse a seq
        that's been compressed.
        """
        patterns = [
            os.path.join(self._base_dir, f"{self._log_type}-{date_str}-*.log"),
            os.path.join(self._base_dir, f"{self._log_type}-{date_str}-*.log.gz"),
        ]
        matches = []
        for pattern in patterns:
            matches.extend(glob.glob(pattern))
        if not matches:
            return 1
        best = 0
        for m in matches:
            base = os.path.basename(m)
            # "<type>-YYYY-MM-DD-NNN.log" or "<type>-YYYY-MM-DD-NNN.log.gz"
            # Strip .gz then .log, then take the last -<NNN> segment.
            stem = base
            if stem.endswith(".gz"):
                stem = stem[:-3]
            if stem.endswith(".log"):
                stem = stem[:-4]
            parts = stem.rsplit("-", 1)
            if len(parts) == 2:
                try:
                    seq = int(parts[1])
                    if seq > best:
                        best = seq
                except ValueError:
                    pass
        return best if best > 0 else 1

    def _gzip_file(self, path):
        """Optionally scrub, then gzip *path* in place.

        Runs in a background thread off the write path. Safe to call
        after a rotation because the original file is no longer being
        appended to. When *scrub_fn* is configured, the file's bytes
        are passed through it before compression, so privacy-relevant
        content (IPs, session tokens, …) never lives on disk in the
        rolled file and never leaves this machine in the analytics
        offer.
        """
        gz_path = path + ".gz"
        try:
            if self._scrub_fn is not None:
                with open(path, "rb") as src:
                    raw = src.read()
                try:
                    scrubbed = self._scrub_fn(raw)
                except Exception:
                    # Fail-open: a scrubbing bug must never cost us the
                    # log history. Worst case is the rolled file keeps
                    # its raw content until the next manual audit.
                    scrubbed = raw
                with gzip.open(gz_path, "wb", compresslevel=6) as dst:
                    dst.write(scrubbed)
            else:
                with open(path, "rb") as src, gzip.open(gz_path, "wb", compresslevel=6) as dst:
                    shutil.copyfileobj(src, dst, length=1 << 16)
            os.chmod(gz_path, 0o600)
            os.remove(path)
        except OSError:
            # If compression or delete failed, leave the .log in place
            # and clean up any partial .gz — next rotation retries.
            try:
                os.remove(gz_path)
            except OSError:
                pass

    def _enforce_total_size(self):
        """Delete oldest log files across all types until under the cap.

        Two-phase policy so unshipped history survives OnionHome outages:

        * **Soft cap** (``max_total_size``): only files whose names are
          ``<=`` the per-type shipped watermark are eligible. A week-
          long upload outage therefore keeps its full log history
          rather than silently rotating the data out of existence.
        * **Hard ceiling** (5× ``max_total_size``): if the soft phase
          can't get under the cap because most files are unshipped,
          we fall back to deleting oldest-first regardless of upload
          state. Data loss eventually beats disk exhaustion.

        Skips any file modified in the last 60 seconds to protect
        concurrently-active write targets of sibling RotatingLog
        instances sharing this directory.
        """
        import time as _time
        now = _time.time()

        try:
            all_logs = sorted(
                glob.glob(os.path.join(self._base_dir, "*.log"))
                + glob.glob(os.path.join(self._base_dir, "*.log.gz")),
                key=lambda p: os.path.getmtime(p),
            )
        except OSError:
            return

        total = 0
        sizes = {}
        for p in all_logs:
            try:
                sizes[p] = os.path.getsize(p)
                total += sizes[p]
            except OSError:
                sizes[p] = 0

        watermarks = read_shipped(self._base_dir)

        def is_shipped(path):
            name = os.path.basename(path)
            log_type = extract_log_type(name)
            if log_type is None:
                # Unknown naming → treat as shipped so cleanup can reclaim it.
                return True
            return name <= watermarks.get(log_type, "")

        def protected(path):
            try:
                return now - os.path.getmtime(path) < 60
            except OSError:
                return False

        # Phase 1: soft cap over shipped-only pool.
        if total > self._max_total_size:
            for p in all_logs:
                if total <= self._max_total_size:
                    break
                if protected(p) or not is_shipped(p):
                    continue
                try:
                    os.remove(p)
                    total -= sizes.get(p, 0)
                except OSError:
                    pass

        # Phase 2: hard ceiling across everything (including unshipped).
        hard_ceiling = self._max_total_size * 5
        if total > hard_ceiling:
            for p in all_logs:
                if total <= hard_ceiling:
                    break
                if protected(p) or not os.path.exists(p):
                    continue
                try:
                    os.remove(p)
                    total -= sizes.get(p, 0)
                except OSError:
                    pass

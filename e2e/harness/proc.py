"""Running commands, waiting for conditions, and the per-node harness log.

Every command a node runs goes through `run`, which writes the argv, the
exit code, the time it took and the tail of its output to the node's
harness log. Values passed in `secrets` are replaced by `***` in that log
(passwords go to commands on stdin, but a test may still name one).
"""

import os
import subprocess
import threading
import time
from dataclasses import dataclass, field

OUTPUT_TAIL = 4000


@dataclass
class Result:
    argv: list
    rc: int
    out: str
    err: str
    seconds: float

    @property
    def ok(self):
        return self.rc == 0

    def describe(self):
        tail = (self.err or self.out).strip()[-600:]
        return f"`{' '.join(self.argv)}` exited {self.rc} after {self.seconds:.0f}s: {tail}"


class CommandFailed(Exception):
    def __init__(self, result):
        super().__init__(result.describe())
        self.result = result


class NodeLog:
    """Append-only text log for one node (or the harness itself)."""

    def __init__(self, path, secrets=()):
        self.path = path
        self.secrets = [s for s in secrets if s]
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def add_secret(self, value):
        if value:
            self.secrets.append(value)

    def redact(self, text):
        for s in self.secrets:
            text = text.replace(s, "***")
        return text

    def write(self, text):
        stamp = time.strftime("%H:%M:%S")
        with self._lock, open(self.path, "a", encoding="utf-8") as f:
            for line in self.redact(text).splitlines() or [""]:
                f.write(f"{stamp} {line}\n")


def run(argv, *, env=None, input=None, timeout=600, cwd=None, log=None, check=False):
    """Run argv (never through a shell) and return a Result.

    A timeout is a Result with rc 124, not an exception, so callers treat
    it like any other failure."""
    argv = [str(a) for a in argv]
    started = time.monotonic()
    if log:
        log.write(f"$ {' '.join(argv)}")
    try:
        p = subprocess.run(argv, env=env, input=input, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, cwd=cwd)
        result = Result(argv, p.returncode, p.stdout, p.stderr, time.monotonic() - started)
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = e.stderr.decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        result = Result(argv, 124, out, err + f"\n(timed out after {timeout}s)",
                        time.monotonic() - started)
    except OSError as e:
        result = Result(argv, 127, "", str(e), time.monotonic() - started)
    if log:
        log.write(f"  -> rc {result.rc} in {result.seconds:.1f}s")
        for name, text in (("out", result.out), ("err", result.err)):
            if text.strip():
                log.write(f"  [{name}] " + text.strip()[-OUTPUT_TAIL:].replace("\n", "\n  "))
    if check and not result.ok:
        raise CommandFailed(result)
    return result


class WaitTimeout(Exception):
    pass


@dataclass
class Timing:
    label: str
    seconds: float
    ok: bool


@dataclass
class Timings:
    """How long each wait took, kept for the report: slow-but-green
    Tor reachability is worth seeing over time."""
    entries: list = field(default_factory=list)

    def add(self, label, seconds, ok):
        self.entries.append(Timing(label, round(seconds, 1), ok))


def wait_until(predicate, *, timeout, every=5.0, label, timings=None, log=None):
    """Call predicate() until it returns something truthy; return that.

    Exceptions from predicate count as "not yet" (the last one is named in
    the WaitTimeout). The wait's duration goes into timings."""
    started = time.monotonic()
    last_error = None
    while True:
        try:
            value = predicate()
            if value:
                elapsed = time.monotonic() - started
                if timings is not None:
                    timings.add(label, elapsed, True)
                if log:
                    log.write(f"wait '{label}': ok after {elapsed:.0f}s")
                return value
        except Exception as e:  # noqa: BLE001 - any failure is "not yet"
            last_error = e
        elapsed = time.monotonic() - started
        if elapsed >= timeout:
            if timings is not None:
                timings.add(label, elapsed, False)
            why = f" (last error: {last_error})" if last_error else ""
            if log:
                log.write(f"wait '{label}': gave up after {elapsed:.0f}s{why}")
            raise WaitTimeout(f"{label}: not within {timeout:.0f}s{why}")
        time.sleep(every)

"""The Python CLI as the start traces see it (tests/test_start_traces.py).

The trace harness points the launchers' `bundled_python` / `op_py` here:
`python -I start_trace_runner.py [-m onionpress.cli] [--data-dir D] SUB …`.

The launchers' start steps that are already Python, and the ones each
later slice of issue #442 moves, run for real, in this process, against
the fake docker (tests/start_trace_docker.py) on PATH, with:

  - this machine's OS replaced by the launcher's ($OP_TRACE_OS), so a
    fixture recorded on a Mac matches one recorded on Linux;
  - no app bundle (no bundled docker, compose run from the docker folder);
  - time.sleep a no-op;
  - wordpress_egress.ensure answering "ok" (or failing with
    $OP_EGRESS_FAIL set): its own tests are in test_wordpress_egress.py;
  - every launcher-format log line recorded as `log MSG`.

The steps that work on WordPress itself (provision-post-install, the
plugins' configure-*, …) are recorded as `py SUB ARGS`, never run; their
exit status is $STUB_RC_PY_<SUB> (0 by default). The ones a start only
ever runs detached are recorded as `detached py SUB`. `generate-vanity`
writes a key folder when $FAKE_VANITY names an address, else fails.

Python start code that calls one of those steps in-process goes through
the seams patched in `install_seams` and records the same lines, so a
step moved from bash to Python keeps its trace.
"""

import os
import sys

TMP = os.environ.get("OP_TMP", "")

# Run for real: the launchers' start steps in Python.
REAL = {
    "migrate-key-volume", "retire-old-onionheaven-containers",
    "migrate-tor-network", "using-local-images",
    "regenerate-derivatives-pending", "wordpress-egress",
    "apply-pending-key-import", "start-containers", "setup-bundled-plugins",
    "start-core", "start-post-up", "seed-onion-key",
}
# Only ever started in the background by a start.
DETACHED = {"ensure-archive-s3-keys", "regenerate-derivatives"}


def norm(text):
    return text.replace(TMP, "$TMP") if TMP else text


def record(line):
    with open(os.environ["OP_CALLS"], "a", encoding="utf-8") as f:
        f.write(norm(line) + "\n")


def split(argv):
    """(argv for cli.main, the subcommand and its arguments, without
    --data-dir)."""
    if argv[:2] == ["-m", "onionpress.cli"]:
        argv = argv[2:]
    shown, i = [], 0
    while i < len(argv):
        if argv[i] == "--data-dir":
            i += 2
            continue
        shown.append(argv[i])
        i += 1
    return argv, shown


def fake_step(shown):
    """A recorded step: its line and exit status."""
    sub = shown[0] if shown else ""
    if sub in DETACHED:
        record("detached py " + " ".join(shown))
        return 0
    record("py " + " ".join(shown))
    if sub == "generate-vanity":
        return write_vanity_key()
    return int(os.environ.get("STUB_RC_PY_" + sub.upper().replace("-", "_"), "0"))


def write_vanity_key(data_dir=None):
    """What a successful generation leaves: the key folder, named by
    $FAKE_VANITY. 1 when the scenario says generation fails."""
    address = os.environ.get("FAKE_VANITY", "")
    if not address:
        return 1
    data_dir = data_dir or os.environ["OP_DATA_DIR"]
    folder = os.path.join(data_dir, "shared", "vanity-keys", address)
    os.makedirs(folder, exist_ok=True)
    for name, body in (("hostname", address + "\n"),
                       ("ks_hs_id.ed25519_expanded_private", "PEM\n"),
                       ("hs_ed25519_secret_key", "secret\n"),
                       ("hs_ed25519_public_key", "public\n")):
        with open(os.path.join(folder, name), "w", encoding="utf-8") as f:
            f.write(body)
    return 0


def install_seams():
    """Patch the Python side as the module docstring says."""
    import time

    from onionpress import cli, platform, wordpress_egress
    from onionpress.platform import OS

    time.sleep = lambda _s: None
    wanted = OS.MACOS if os.environ.get("OP_TRACE_OS") == "mac" else OS.LINUX
    original = platform.detect_os
    for name, module in list(sys.modules.items()):
        if name.startswith("onionpress") and getattr(module, "detect_os", None) is original:
            module.detect_os = lambda: wanted
    platform.find_app_bundle = lambda: None

    class Egress:
        ok = not os.environ.get("OP_EGRESS_FAIL")
        how = "test"
        detail = "ok" if ok else "error: refused"

        def line(self):
            return f"{self.detail} ({self.how})"

    wordpress_egress.ensure = lambda *a, **k: Egress()

    def launcher_log_func():
        def log(msg):
            record("log " + msg)
            print(msg, flush=True)
        return log
    cli._launcher_log_func = launcher_log_func

    try:
        from onionpress import start
    except ImportError:
        return
    # The steps a Python start runs in-process or detached: recorded as
    # the bash recorded them.
    start.run_cli_step = lambda argv, **_k: fake_step(split(list(argv))[1])
    start.run_detached_cli = lambda argv, **_k: (
        record("detached py " + " ".join(split(list(argv))[1])))
    if hasattr(start, "generate_vanity_on_host"):
        def generate(prefix, data_dir, *a, **k):
            record(f"fn generate_vanity_address {prefix}")
            if write_vanity_key(data_dir):
                return None
            return os.path.join(data_dir, "shared", "vanity-keys",
                                os.environ["FAKE_VANITY"])
        start.generate_vanity_on_host = generate
    for name in ("configure_site_timezones",):
        if hasattr(start, name):
            setattr(start, name, lambda *a, _n=name, **k: record(f"fn {_n}"))


def main(argv):
    full, shown = split(argv)
    if not shown or shown[0] not in REAL:
        return fake_step(shown)
    sys.path.insert(0, os.environ["OP_SRC"])
    from onionpress import cli
    install_seams()
    try:
        return cli.main(full)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

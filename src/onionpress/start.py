"""The launchers' start, in Python (issue #442, CLI-first).

Each launcher's `start_containers` waits for Docker (the Mac's Colima VM
and its recovery stay bash) and then runs `onionpress start-containers`,
the steps here, in the order the bash bodies ran them.

The START SUMMARY contract (#442.2)
-----------------------------------
The Mac launcher ends a start with one line naming the components that
failed (`log_start_summary`, from the bash variable START_ERRORS). A Python
start step names what it could not do by appending one name per line to
the file given as `--errors-file`; the bash reads the names back into
START_ERRORS after the call (`note_start_errors`) and removes the file.
The exit status says only "stop the start" (non-zero) or "carry on" (0):
a best-effort step that failed is a name in the file, never a non-zero
exit. Names are lowercase words joined by hyphens, as the bash used
(`tor`, `provision-post-install`, `wordpress-egress`); anything else in
the file is ignored by the reader.

Linux has no START SUMMARY: its launcher passes no `--errors-file`, and
the failed steps' WARNING / ERROR lines in the log are the record.
"""

import os
import re
import shutil
import subprocess
import sys

ERROR_NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


class StartErrors:
    """The names of the start steps that failed, for the START SUMMARY:
    appended to *path* (the launcher's `--errors-file`) as they happen, so
    a step that fails before a later one dies is still named. Without a
    path the names are only kept here (Linux, tests)."""

    def __init__(self, path: str | None = None):
        self.path = path
        self.names: list[str] = []

    def add(self, name: str) -> None:
        if not ERROR_NAME.match(name):
            raise ValueError(f"not a start step name: {name!r}")
        self.names.append(name)
        if not self.path:
            return
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
        except OSError:
            return  # the launcher's temp file is gone: the log still says it
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(name + "\n")

    def __bool__(self) -> bool:
        return bool(self.names)


# ---- running other onionpress steps ----
#
# The bash start ran the WordPress-side steps (provision-post-install, the
# plugins' configure-*, ...) as separate `onionpress …` processes, their
# output appended to the log and their failure only an exit status; the
# Python start does the same, with this interpreter. Isolated like the
# launchers' own runners (issue #470): on Linux `-I` with the package's
# folder handed over as an argument (op_py's boot), on the Mac the
# bundle's Python, which needs PYTHONHOME, with PYTHONSAFEPATH instead.

_BOOT = ("import sys, runpy\n"
         "sys.path[:0] = sys.argv.pop(1).split(':')\n"
         "runpy.run_module('onionpress.cli', run_name='__main__', alter_sys=True)")


def _package_parent() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def cli_command(args: list[str]) -> tuple[list[str], dict]:
    """(argv, environment) that run `onionpress ARGS` with this
    interpreter, isolated as the launchers' runners are."""
    from .platform import OS, detect_os
    env = os.environ.copy()
    if detect_os() == OS.MACOS:
        env["PYTHONSAFEPATH"] = "1"
        env["PYTHONPATH"] = os.pathsep.join(
            p for p in (_package_parent(), env.get("PYTHONPATH", "")) if p)
        return [sys.executable, "-m", "onionpress.cli", *args], env
    return [sys.executable, "-I", "-B", "-c", _BOOT, _package_parent(), *args], env


def _log_handle(log_file: str | None):
    if not log_file:
        return None
    return open(log_file, "a", encoding="utf-8")


def run_cli_step(argv: list[str], *, data_dir: str, log_file: str | None = None) -> int:
    """Run `onionpress --data-dir DATA_DIR ARGV` to the end: its exit
    status (127 when it could not be started). With *log_file* its output
    is appended there, as the bash's `>> "$LOG_FILE" 2>&1` did; without, it
    goes where this process's does."""
    command, env = cli_command(["--data-dir", data_dir, *argv])
    out = _log_handle(log_file)
    try:
        return subprocess.run(command, env=env, stdin=subprocess.DEVNULL, stdout=out,
                              stderr=subprocess.STDOUT if out else None).returncode
    except OSError:
        return 127
    finally:
        if out:
            out.close()


def run_detached_cli(argv: list[str], *, data_dir: str, log_file: str) -> None:
    """Start `onionpress --data-dir DATA_DIR ARGV` in the background, its
    output appended to *log_file*, so that it outlives this start and the
    launcher's EXIT trap (which kills its own jobs): a session of its own.
    Started by the Linux Settings-page watcher (ONIONPRESS_IN_WATCHER),
    it goes out of the watcher's cgroup through systemd-run, as the bash
    run_detached did, or the watcher's stop would end it."""
    command, env = cli_command(["--data-dir", data_dir, *argv])
    if os.environ.get("ONIONPRESS_IN_WATCHER") == "true" and shutil.which("systemd-run"):
        setenv = ["--setenv=PYTHONDONTWRITEBYTECODE=1"]
        if os.environ.get("DOCKER_HOST"):
            setenv.append(f"--setenv=DOCKER_HOST={os.environ['DOCKER_HOST']}")
        try:
            if subprocess.run(
                    ["systemd-run", "--user", "--quiet", "--collect", "--no-block",
                     f"--property=StandardOutput=append:{log_file}",
                     f"--property=StandardError=append:{log_file}",
                     *setenv, "--", *command],
                    stdin=subprocess.DEVNULL, capture_output=True).returncode == 0:
                return
        except OSError:
            pass
    out = _log_handle(log_file)
    try:
        subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=out,
                         stderr=subprocess.STDOUT if out else None,
                         start_new_session=True)
    except OSError:
        pass
    finally:
        if out:
            out.close()

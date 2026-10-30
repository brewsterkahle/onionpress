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

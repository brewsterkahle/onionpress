"""`onionpress` as a bash harness's launcher runner, never on a real docker.

`python cli_on_stub_docker.py [-m onionpress.cli] ARGS…` runs
onionpress.cli.main(ARGS) from this checkout's src/ with no app bundle
found, so Docker() runs `docker` from PATH (the harness's stub), never an
installed OnionPress.app's docker against whatever socket that reaches.
For the harnesses that cut launcher functions out and run them under
bash (their `bundled_python` / `op_py`).
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

from onionpress import cli, platform  # noqa: E402

platform.find_app_bundle = lambda: None

if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv[:2] == ["-m", "onionpress.cli"]:
        argv = argv[2:]
    sys.exit(cli.main(argv))

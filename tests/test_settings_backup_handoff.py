#!/usr/bin/env python3
"""The Settings page's backup reaches the container as www-data's file,
written by www-data.

The backup is handed to the WordPress container at a predictable name in
/var/lib/onionpress, a folder www-data writes. It used to be `docker cp`'d
and then `chown www-data`'d as root: both follow a link www-data can plant
at that name, so the chown made any file in the container (an entrypoint
root runs, say) www-data's. Both platforms now write it with
`docker exec -u www-data`, which can reach only what www-data could anyway.

No docker: the Mac handler is AppKit-bound and checked as source; the Linux
branch runs for real with docker stubbed.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_backup_metadata_validation as mv  # noqa: E402


def _branch(text, head, end):
    start = text.index(head) + len(head)
    return text[start:text.index(end, start)]


class TestMenubarHandoff(unittest.TestCase):

    def setUp(self):
        self.body = _branch(mv._read("src/menubar.py"),
                            'elif action == "create-backup":',
                            'elif action == "restore-backup":')

    def test_written_as_www_data_never_chowned_by_root(self):
        self.assertIn('"docker", "exec", "-i", "-u", "www-data"', self.body)
        self.assertIn("""'umask 077; cat > "$1"'""", self.body)
        self.assertNotIn('"chown"', self.body)
        self.assertNotIn('"docker", "cp"', self.body)

    def test_the_local_copy_is_removed_whatever_happens(self):
        handoff = self.body[self.body.index('"-u", "www-data"'):]
        self.assertLess(handoff.index("finally:"), handoff.index("os.remove(tmp_path)"))


class TestLinuxHandoff(unittest.TestCase):

    PRELUDE = r'''
log() { echo "[log] $1" >> "$LOG_FILE"; }
setup_db_passwords() { :; }
create_backup() { printf 'backup bytes' > "$2"; }
docker() {
    echo "docker $*" >> "$CALLS"
    case "$*" in
        "exec onionpress-wordpress cat /var/lib/onionpress/backup-password") echo pw ;;
        "exec -i -u www-data onionpress-wordpress sh -c "*)
            # Run the script the launcher hands over, against a sandbox path.
            shift 7; sh -c "$1" sh "$SANDBOX$3" ;;
        "exec -i onionpress-wordpress sh -c "*) cat > "$RESULT" ;;
        "exec onionpress-tor "*) echo "$ADDR" ;;
    esac
    return 0
}
'''

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        text = mv._read("linux/onionpress")
        self.branch = _branch(text, "                create-backup)\n",
                              "\n                    ;;\n")

    def test_written_as_www_data_never_chowned_by_root(self):
        code = "\n".join(line for line in self.branch.splitlines()
                         if not line.lstrip().startswith("#"))
        self.assertNotIn("chown", code)
        self.assertNotRegex(code, r"docker cp [^\n]*onionpress-wordpress:")
        self.assertIn("docker exec -i -u www-data onionpress-wordpress", code)

    def test_the_backup_arrives_whole_and_owner_only(self):
        sandbox = os.path.join(self.tmp, "container")
        os.makedirs(os.path.join(sandbox, "var", "lib", "onionpress"))
        calls = os.path.join(self.tmp, "calls")
        env = {
            "PATH": os.environ["PATH"], "HOME": os.path.join(self.tmp, "home"),
            "TMPDIR": self.tmp, "LOG_FILE": os.path.join(self.tmp, "log"),
            "CALLS": calls, "RESULT": os.path.join(self.tmp, "result"),
            "SANDBOX": sandbox, "ADDR": mv.ADDR,
            "DOCKER_HOST": "unix:///var/empty/no-docker-for-tests.sock",
        }
        script = self.PRELUDE + "run_branch() {\n" + self.branch + "\n}\nrun_branch\n"
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                           timeout=60, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        landed = os.listdir(os.path.join(sandbox, "var", "lib", "onionpress"))
        self.assertEqual(len(landed), 1, landed)
        path = os.path.join(sandbox, "var", "lib", "onionpress", landed[0])
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"backup bytes")
        self.assertEqual(os.stat(path).st_mode & 0o077, 0)
        with open(os.path.join(self.tmp, "result")) as f:
            self.assertIn('"success":true', f.read())
        # Staged in a folder of its own under TMPDIR, and removed.
        self.assertFalse([n for n in os.listdir(self.tmp)
                          if n.startswith("onionpress-settings-backup-")])
        with open(calls) as f:
            self.assertNotIn(" /tmp/OnionPress-", f.read())


if __name__ == "__main__":
    unittest.main()

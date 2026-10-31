#!/usr/bin/env python3
"""wp-cli runs as www-data, never as root.

wp-cli loads WordPress: wp-config.php, the mu-plugins, plugins and themes,
every one of them a file www-data can write. The host's calls (`docker exec
onionpress-wordpress wp … --allow-root`, from the menubar, both launchers,
the Linux service and the CLI) and the image's own scripts (multisite-init,
the security audit) all ran it as root, so any code execution in WordPress,
a plugin bug say, became root in the container at the next call; root
there reaches the shared volumes and, under rootless Docker, the host
user's own files.

Now the host goes through onionpress.wp_files.wp_cli_args (Python), wp_cli
and wp_cli_in (the launchers) and activate_plugin.sh's copy of the same
command; the image's scripts go through onionpress-wp (runuser to
www-data). Nothing passes --allow-root, so wp-cli itself refuses any call
that still ends up as root. Root's remaining job, giving www-data back what
root's wp-cli left in the web root, is onionpress-web-root-owner.sh: file
operations, no WordPress, and never inside My Creations.
"""

import ast
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(PROJECT_ROOT, "src")
sys.path.insert(0, SRC)

from onionpress import wp_files  # noqa: E402
from onionpress.docker import Docker  # noqa: E402

LAUNCHERS = ("app/MacOS/onionpress", "linux/onionpress")
IMAGE = "app/Resources/docker/wordpress/"
WP_HELPER = IMAGE + "onionpress-wp"
OWNER_SCRIPT = IMAGE + "onionpress-web-root-owner.sh"
AUDIT = IMAGE + "onionpress-security-audit.sh"
MULTISITE_INIT = IMAGE + "onionpress-multisite-init.sh"
AS_WWW_DATA = ["-u", "www-data", "-e", "WP_CLI_CACHE_DIR=/tmp/wp-cli-cache"]


def _read(rel):
    with open(os.path.join(PROJECT_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _code_lines(text, comment=("#",)):
    """(number, line) for each line that is not a comment."""
    return [(n, line) for n, line in enumerate(text.splitlines(), 1)
            if not line.lstrip().startswith(comment)]


def _function(text, name):
    """Source of a top-level bash function, closing brace included."""
    start = text.index(f"\n{name}() {{") + 1
    return text[start:text.index("\n}\n", start) + 3]


def _shipped_files():
    """Every text file that ships (app, launchers, package, build scripts)."""
    out = []
    for top in ("app", "linux", "src", "build"):
        for root, dirs, files in os.walk(os.path.join(PROJECT_ROOT, top)):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", "node_modules")]
            for name in files:
                path = os.path.join(root, name)
                try:
                    with open(path, encoding="utf-8") as f:
                        text = f.read()
                except (UnicodeDecodeError, OSError):
                    continue  # images, icons, binaries
                out.append((os.path.relpath(path, PROJECT_ROOT), text))
    return out


def _is_python(rel, text):
    return rel.endswith(".py") or text.startswith(("#!/usr/bin/env python",
                                                   "#!/usr/bin/python"))


def _docstrings(tree):
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                ids.add(id(body[0].value))
    return ids


class TestWpCliArgs(unittest.TestCase):

    def test_as_www_data_and_never_allow_root(self):
        self.assertEqual(
            wp_files.wp_cli_args("option", "get", "siteurl"),
            ["exec"] + AS_WWW_DATA + ["onionpress-wordpress", "wp", "option",
                                      "get", "siteurl"])
        self.assertEqual(wp_files.wp_cli_args("eval", "x", stdin=True)[:2],
                         ["exec", "-i"])
        self.assertEqual(
            wp_files.wp_cli_args("media", "regenerate", nice=True, container="c"),
            ["exec"] + AS_WWW_DATA + ["c", "nice", "-n", "10", "wp", "media",
                                      "regenerate"])
        for argv in (wp_files.wp_cli_args(), wp_files.wp_cli_args(stdin=True)):
            self.assertNotIn("--allow-root", argv)
            self.assertEqual(argv[argv.index("-u") + 1], wp_files.WP_USER)

    def test_docker_wp(self):
        d = Docker.__new__(Docker)
        d.run = mock.Mock(return_value="r")
        self.assertEqual(d.wp("core", "is-installed", timeout=15), "r")
        self.assertEqual(d.run.call_args.args[0],
                         wp_files.wp_cli_args("core", "is-installed"))
        self.assertEqual(d.run.call_args.kwargs["timeout"], 15)
        d.wp("option", "update", "k", input="v")
        self.assertEqual(d.run.call_args.args[0],
                         wp_files.wp_cli_args("option", "update", "k", stdin=True))
        self.assertEqual(d.run.call_args.kwargs["input"], "v")


class TestTheCopiesAgree(unittest.TestCase):
    """The launchers, activate_plugin.sh and the image's onionpress-wp carry
    the same command as wp_files.wp_cli_args."""

    def test_launchers_wp_cli(self):
        plain = "docker " + " ".join(wp_files.wp_cli_args()) + ' "$@"'
        piped = "docker " + " ".join(wp_files.wp_cli_args(stdin=True)) + ' "$@"'
        for rel in LAUNCHERS:
            text = _read(rel)
            with self.subTest(rel):
                self.assertEqual(_function(text, "wp_cli"),
                                 "wp_cli() {\n    " + plain + "\n}\n")
                # wp_cli_in went with the bash setup (#442.10): what reads
                # stdin is the Python's (wp_files.wp_cli_args(stdin=True)).
                self.assertNotIn("\nwp_cli_in() {", text)
                self.assertTrue(piped)

    def test_activate_plugin_script(self):
        sh = _read("app/Resources/scripts/activate_plugin.sh")
        args = wp_files.wp_cli_args(container='"$CONTAINER_NAME"')
        self.assertIn('WP_CLI=("$DOCKER_BIN" ' + " ".join(args) + ")", sh)
        self.assertNotIn(" -T ", sh)  # docker exec has no -T: it never ran

    def test_image_helper(self):
        sh = _read(WP_HELPER)
        cache = f"WP_CLI_CACHE_DIR={wp_files.WP_CLI_CACHE_DIR}"
        self.assertIn(f'exec setsid -w runuser -u www-data -- env {cache} wp "$@"', sh)
        self.assertIn(f'exec env {cache} wp "$@"', sh)
        self.assertTrue(os.access(os.path.join(PROJECT_ROOT, WP_HELPER), os.X_OK))


class TestNothingRunsWpCliAsRoot(unittest.TestCase):

    def test_no_allow_root_anywhere_it_ships(self):
        """wp-cli refuses root without --allow-root; nothing may pass it."""
        for rel, text in _shipped_files():
            if "--allow-root" not in text:
                continue
            with self.subTest(rel):
                if _is_python(rel, text):
                    tree = ast.parse(text)
                    docs = _docstrings(tree)
                    for node in ast.walk(tree):
                        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                                and id(node) not in docs):
                            self.assertNotIn("--allow-root", node.value,
                                             f"{rel}:{node.lineno}")
                else:
                    for n, line in _code_lines(text, ("#", "*", "//")):
                        self.assertNotIn("--allow-root", line, f"{rel}:{n}")

    def test_python_builds_wp_argv_only_in_wp_files(self):
        """A list holding the bare word "wp" is a wp-cli argv; only
        wp_cli_args may build one."""
        for rel, text in _shipped_files():
            if not _is_python(rel, text) or rel == "src/onionpress/wp_files.py":
                continue
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, (ast.List, ast.Tuple)) and any(
                        isinstance(e, ast.Constant) and e.value == "wp"
                        for e in node.elts):
                    self.fail(f"{rel}:{node.lineno}: build it with wp_files.wp_cli_args")

    def test_no_shell_runs_wp_in_the_container_as_root(self):
        scripts = list(LAUNCHERS) + [
            "app/Resources/scripts/activate_plugin.sh",
            "app/Resources/scripts/install_plugin.sh"]
        pattern = re.compile(
            r"\bexec\b(?![^\n]*-u www-data)[^\n]*\s(?:\S*wordpress|\"\$CONTAINER(?:_NAME)?\")"
            r"\s+(?:nice -n \d+ )?wp(?:\s|$)")
        for rel in scripts:
            for n, line in _code_lines(_read(rel)):
                with self.subTest(f"{rel}:{n}"):
                    self.assertNotRegex(line, pattern)

    def test_image_scripts_run_wp_through_the_helper(self):
        bare_wp = re.compile(r"(?:^|[\s;&|(`\"'])wp\s")
        for rel in (AUDIT, MULTISITE_INIT, IMAGE + "onionpress-entrypoint.sh"):
            for n, line in _code_lines(_read(rel)):
                if "-u www-data" in line:
                    continue  # a command the report tells the user to run
                with self.subTest(f"{rel}:{n}"):
                    self.assertNotRegex(line, bare_wp)
        self.assertIn('\nWP="onionpress-wp --path=$WPROOT"\n', _read(AUDIT))
        self.assertIn("onionpress-wp core multisite-convert", _read(MULTISITE_INIT))

    def test_dockerfile_installs_and_checks_the_helper(self):
        df = _read(IMAGE + "Dockerfile")
        self.assertIn("COPY onionpress-wp /usr/local/bin/", df)
        self.assertIn("onionpress-wp --info", df)
        self.assertIn("runuser -u www-data -- id -un", df)
        self.assertIn("COPY onionpress-web-root-owner.sh /usr/local/bin/", df)
        # The helper is in place before anything that runs wp-cli.
        self.assertLess(df.index("COPY onionpress-wp "),
                        df.index("COPY onionpress-multisite-init.sh"))

    def test_entrypoint_order(self):
        ep = _read(IMAGE + "onionpress-entrypoint.sh")
        owner = ep.index("/usr/local/bin/onionpress-web-root-owner.sh")
        self.assertLess(owner, ep.index("/usr/local/bin/onionpress-multisite-init.sh"))
        self.assertLess(owner, ep.index("/usr/local/bin/onionpress-security-audit.sh"))
        self.assertLess(owner, ep.index("exec docker-entrypoint.sh"))


class _Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.calls = os.path.join(self.tmp, "calls")

    def _stub(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body)
        os.chmod(path, 0o755)

    def _recorded(self):
        if not os.path.exists(self.calls):
            return []
        with open(self.calls) as f:
            return f.read().splitlines()

    def _env(self):
        return dict(os.environ, PATH=self.bin + os.pathsep + os.environ["PATH"])


class TestImageHelperRuns(_Sandbox):
    """onionpress-wp, run here with stubs for id, setsid and wp."""

    def _run(self, uid):
        self._stub("id", f'echo {uid}\n')
        self._stub("setsid", f'printf "%s\\n" "$@" > {self.calls!r}\n')
        self._stub("wp", f'{{ echo "cache=$WP_CLI_CACHE_DIR"; printf "%s\\n" "$@"; }} > {self.calls!r}\n')
        return subprocess.run(["sh", os.path.join(PROJECT_ROOT, WP_HELPER),
                               "eval", "echo 'a b';", "--path=/var/www/html"],
                              env=self._env(), capture_output=True, text=True,
                              timeout=30)

    def test_root_drops_to_www_data(self):
        r = self._run(0)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._recorded(), [
            "-w", "runuser", "-u", "www-data", "--",
            "env", "WP_CLI_CACHE_DIR=/tmp/wp-cli-cache",
            "wp", "eval", "echo 'a b';", "--path=/var/www/html"])

    def test_www_data_runs_it_directly(self):
        r = self._run(33)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._recorded(), [
            "cache=/tmp/wp-cli-cache", "eval", "echo 'a b';", "--path=/var/www/html"])


class TestAuditTakesOnlyItsOwnZip(unittest.TestCase):
    """The download helper runs as www-data now, so the path it prints is
    www-data's choice; root removes it afterwards (rm -f)."""

    def _accepted(self, path):
        line = next(l for l in _read(AUDIT).splitlines() if "=~ ^/tmp/onionpress-wordpress-" in l)
        cond = re.search(r"\[\[ .* \]\]", line).group(0)
        r = subprocess.run(["bash", "-c", f'zip="$1"; {cond}', "bash", path],
                           capture_output=True, timeout=30)
        return r.returncode == 0

    def test_only_the_helpers_own_name(self):
        self.assertTrue(self._accepted("/tmp/onionpress-wordpress-7.0.3.zip"))
        self.assertTrue(self._accepted("/tmp/onionpress-wordpress-7.0.3-no-content.zip"))
        for bad in ("/etc/passwd", "/usr/local/bin/docker-entrypoint.sh",
                    "/tmp/onionpress-wordpress-7.0.3.zip/../../etc/x.zip",
                    "/tmp/onionpress-wordpress-7.0.3.zip\n/etc/passwd",
                    "/var/www/html/wp-config.php", "", "/tmp/onionpress-wordpress-.zip"):
            with self.subTest(bad):
                self.assertFalse(self._accepted(bad))


MOUNTINFO_OK = """\
1 0 0:1 / / rw - overlay overlay rw
2 1 0:2 / /proc rw - proc proc rw
3 1 0:3 / /dev rw - tmpfs tmpfs rw
4 1 8:1 /var/lib/docker/containers/x/resolv.conf /etc/resolv.conf rw - ext4 /dev/vda1 rw
5 1 8:1 /var/lib/docker/volumes/onionpress_wordpress-data/_data /var/www/html rw - ext4 /dev/vda1 rw
6 1 8:1 /var/lib/docker/volumes/onionpress_onionpress-data/_data /var/lib/onionpress rw - ext4 /dev/vda1 rw
7 5 0:40 /Users/me/OnionPress/Creations/My\\040Creations /var/www/html/wp-content/creations rw - virtiofs mount0 rw
"""


class TestWebRootOwnerMounts(_Sandbox):
    """mounts_ok: the web root is one mount, and every mount below it is a
    folder named "creations" (it cannot be renamed) or lies inside one."""

    def _ok(self, mountinfo):
        text = _read(OWNER_SCRIPT)
        fn = text[text.index("\nmounts_ok() {") + 1:]
        fn = fn[:fn.index("\n}\n") + 3]
        path = os.path.join(self.tmp, "mountinfo")
        with open(path, "w") as f:
            f.write(mountinfo)
        r = subprocess.run(["bash", "-c", fn + "mounts_ok"], capture_output=True,
                           env=dict(os.environ, WPROOT="/var/www/html", MOUNTINFO=path),
                           timeout=30)
        return r.returncode == 0

    def test_the_compose_layout_passes(self):
        self.assertTrue(self._ok(MOUNTINFO_OK))
        no_creations = "\n".join(MOUNTINFO_OK.splitlines()[:-1]) + "\n"
        self.assertTrue(self._ok(no_creations))
        nested = MOUNTINFO_OK + "8 7 0:41 / /var/www/html/wp-content/creations/usb rw - vfat x rw\n"
        self.assertTrue(self._ok(nested))
        moved = MOUNTINFO_OK.replace("/var/www/html/wp-content/creations",
                                     "/var/www/html/wp-includes/creations")
        self.assertTrue(self._ok(moved))  # still named creations: pruned there

    def test_anything_else_refuses(self):
        cases = {
            # wp-content/creations was a link when Docker mounted: the mount
            # sits at the link's target, under a name www-data chose.
            "creations followed through a link":
                MOUNTINFO_OK.replace("/var/www/html/wp-content/creations",
                                     "/var/www/html/wp-includes/js"),
            "inside a plain folder named creations":
                MOUNTINFO_OK.replace("/var/www/html/wp-content/creations",
                                     "/var/www/html/creations/x"),
            "mounted over the web root":
                MOUNTINFO_OK + "8 5 0:40 /Users/me/OnionPress /var/www/html rw - virtiofs m rw\n",
            "mounted above the web root":
                MOUNTINFO_OK + "8 1 0:40 /Users/me/OnionPress /var/www rw - virtiofs m rw\n",
            "no web root mount": MOUNTINFO_OK.replace(
                " /var/www/html rw", " /srv rw"),
        }
        for name, info in cases.items():
            with self.subTest(name):
                self.assertFalse(self._ok(info))

    def test_without_mountinfo_nothing_is_touched(self):
        """Run whole: its paths are fixed, and outside a container (no
        /proc/self/mountinfo here, or no /var/www/html mount on Linux) it
        must change nothing."""
        self._stub("chown", f'echo chown >> {self.calls!r}\n')
        self._stub("find", f'echo find >> {self.calls!r}\n')
        r = subprocess.run(["bash", os.path.join(PROJECT_ROOT, OWNER_SCRIPT)],
                           env=self._env(), capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no owner changed", r.stdout)
        self.assertEqual(self._recorded(), [])


class TestWebRootOwnerGivesBack(_Sandbox):
    """give_back against a sandbox web root, with a chown that records
    where it ran and what it was told, and this user standing for root."""

    def setUp(self):
        super().setUp()
        self._stub("chown", f'echo "$(pwd -P) $*" >> {self.calls!r}\n')
        self.root = os.path.join(self.tmp, "html")
        for rel in ("wp-includes/version.php", "wp-content/plugins/p/p.php",
                    "wp-content/creations/mine.jpg",
                    "wp-content/moved/creations/also-mine.jpg",
                    "outside/secret"):
            path = os.path.join(self.root if not rel.startswith("outside") else self.tmp, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "w").close()
        os.symlink(os.path.join(self.tmp, "outside"),
                   os.path.join(self.root, "wp-content", "link"))

    def _give_back(self):
        text = _read(OWNER_SCRIPT)
        fn = text[text.index("\ngive_back() {") + 1:]
        fn = fn[:fn.index("\n}\n") + 3]
        r = subprocess.run(["bash", "-c", fn + "give_back"], capture_output=True, text=True,
                           env=dict(self._env(), WPROOT=self.root, ROOT_UID=str(os.getuid())),
                           timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = set()
        for line in self._recorded():
            where, rest = line.split(" -h www-data:www-data -- ")
            # `-execdir … {} +` may hand one chown several names (GNU find
            # does, BSD find often doesn't); the fixture's names hold no
            # spaces.
            for name in rest.split(" "):
                out.add((os.path.relpath(where, self.tmp), name.removeprefix("./")))
        return out

    def test_root_files_given_back_in_place(self):
        # (The web root itself is left out here: for a starting point, GNU
        # find's -execdir runs in its parent, BSD find's in the cwd.)
        got = self._give_back()
        for expected in (("html", "wp-includes"),
                         ("html/wp-includes", "version.php"),
                         ("html/wp-content/plugins/p", "p.php"),
                         ("html/wp-content", "link"),          # the link itself (-h)
                         ("html/wp-content", "moved")):
            self.assertIn(expected, got)

    def test_never_my_creations_or_through_a_link(self):
        got = self._give_back()
        for where, name in got:
            with self.subTest((where, name)):
                self.assertNotIn("creations", where.split(os.sep))
                self.assertNotEqual(name, "creations")
                self.assertFalse(where.startswith("outside"))
                self.assertNotEqual(name, "secret")

    def test_the_find_line(self):
        text = _read(OWNER_SCRIPT)
        self.assertIn('find "$WPROOT" -xdev -name creations -prune -o -user "$ROOT_UID" -print \\\n'
                      '        -execdir chown -h www-data:www-data -- {} +', text)
        self.assertIn("\nROOT_UID=0\n", text)
        self.assertIn("\nWPROOT=/var/www/html\n", text)


if __name__ == "__main__":
    unittest.main()

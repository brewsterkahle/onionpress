#!/usr/bin/env python3
"""What the host hands to the WordPress container goes in as www-data.

www-data (any PHP code execution in WordPress) owns /var/lib/onionpress and
the web root, and can put a symbolic link at any name there. Root in the
container followed such links: status and result files were written with
`tee`/`cat >` as root (a link at backup-result.json to the container's
entrypoint, plus an error message carrying text of the planter's choosing,
is root code execution at the next start); mu-plugins, sunrise.php, the
theme and plugins went in by root's docker cp and then a chown that handed
the link's target to www-data; the restore's `chown -R …/wp-content/`
resolved a wp-content link through its trailing slash; the tor container's
root `cp` of the onion address followed a link in the shared volume to any
file there; and the security audit truncated and rewrote its report through
whatever sat at that name.

Now every such write runs as www-data (onionpress.wp_files and the
launchers' wp_put), and root's remaining chowns follow no link. These tests
run the scripts for real against a sandbox (docker stubbed) and keep the
sources from growing a root write into www-data's folders again.
"""

import ast
import importlib.util
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(PROJECT_ROOT, "src")
sys.path.insert(0, SRC)

from onionpress import multisite, wp_files  # noqa: E402
from onionpress.docker import Docker  # noqa: E402

ADDR = "op2" + "a" * 53 + ".onion"
LAUNCHERS = ("app/MacOS/onionpress", "linux/onionpress")


def _read(rel):
    with open(os.path.join(PROJECT_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _code_lines(text):
    """(number, line) for each line that is not a comment."""
    return [(n, line) for n, line in enumerate(text.splitlines(), 1)
            if not line.lstrip().startswith("#")]


class _Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _file(self, rel, data=b"", mode=0o644):
        path = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        os.chmod(path, mode)
        return path

    def _put(self, path, data, mode="600"):
        return subprocess.run(["sh", "-c", wp_files.PUT_FILE_SCRIPT, "sh", path, mode],
                              input=data, capture_output=True, timeout=30)


class TestPutFileScript(_Sandbox):
    """wp_files.PUT_FILE_SCRIPT, run here as the container runs it."""

    def test_writes_the_content_with_the_mode(self):
        dest = os.path.join(self.tmp, "status.json")
        r = self._put(dest, b'{"state": "running"}\n')
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(dest, "rb") as f:
            self.assertEqual(f.read(), b'{"state": "running"}\n')
        self.assertEqual(stat.S_IMODE(os.lstat(dest).st_mode), 0o600)
        self.assertEqual(self._put(dest, b"x", "644").returncode, 0)
        self.assertEqual(stat.S_IMODE(os.lstat(dest).st_mode), 0o644)
        self.assertEqual(os.listdir(self.tmp), ["status.json"])

    def test_a_planted_link_is_replaced_never_followed(self):
        target = self._file("outside/docker-entrypoint.sh", b"#!/bin/sh\nexec real\n", 0o755)
        dest = os.path.join(self.tmp, "volume", "backup-result.json")
        os.makedirs(os.path.dirname(dest))
        os.symlink(target, dest)
        r = self._put(dest, b'{"error": "evil\\n"}')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.islink(dest))
        with open(dest, "rb") as f:
            self.assertEqual(f.read(), b'{"error": "evil\\n"}')
        with open(target, "rb") as f:
            self.assertEqual(f.read(), b"#!/bin/sh\nexec real\n")
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o755)

    def test_replaces_a_file_it_could_not_open_for_writing(self):
        """Older versions left root's files there, which www-data cannot
        overwrite in place; a rename over them works in its own folder."""
        dest = self._file("volume/version", b"2.5.2\n", 0o444)
        r = self._put(dest, b"2.5.3\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(dest, "rb") as f:
            self.assertEqual(f.read(), b"2.5.3\n")

    def test_a_failure_leaves_no_temporary_file(self):
        r = self._put(os.path.join(self.tmp, "missing", "status.json"), b"x")
        self.assertNotEqual(r.returncode, 0)
        dest = self._file("volume/status.json", b"old")
        r = self._put(dest, b"new", "not-a-mode")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(os.listdir(os.path.dirname(dest)), ["status.json"])
        with open(dest, "rb") as f:
            self.assertEqual(f.read(), b"old")

    def test_put_file_args_run_it_as_www_data(self):
        self.assertEqual(
            wp_files.put_file_args("/var/lib/onionpress/status.json"),
            ["exec", "-i", "-u", "www-data", "onionpress-wordpress", "sh", "-c",
             wp_files.PUT_FILE_SCRIPT, "sh", "/var/lib/onionpress/status.json", "600"])
        self.assertEqual(wp_files.exec_args("cat", "/var/lib/onionpress/requested-action"),
                         ["exec", "-u", "www-data", "onionpress-wordpress",
                          "cat", "/var/lib/onionpress/requested-action"])


class TestOwnScript(_Sandbox):
    """wp_files.OWN_SCRIPT, root's one job on www-data's paths, run with a
    chown that records where it ran and what it was told."""

    def setUp(self):
        super().setUp()
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir)
        self.calls = os.path.join(self.tmp, "chown-calls")
        with open(os.path.join(bin_dir, "chown"), "w") as f:
            f.write(f'#!/bin/sh\necho "$(pwd -P) $*" >> {self.calls!r}\n')
        os.chmod(os.path.join(bin_dir, "chown"), 0o755)
        # Absolute entries only: find -execdir refuses a PATH with a
        # relative one.
        path = [p for p in os.environ["PATH"].split(os.pathsep) if os.path.isabs(p)]
        self.env = dict(os.environ, PATH=os.pathsep.join([bin_dir] + path))

    def _own(self, path, recursive):
        argv = wp_files.own_args(path, recursive)
        self.assertEqual(argv[:5], ["exec", "onionpress-wordpress", "sh", "-c",
                                    wp_files.OWN_SCRIPT])
        return subprocess.run(["sh", "-c", *argv[4:]], env=self.env,
                              capture_output=True, timeout=30)

    def _chowns(self):
        if not os.path.exists(self.calls):
            return []
        with open(self.calls) as f:
            return f.read().splitlines()

    def _given(self):
        """(folder relative to the sandbox, name) for every name chown got."""
        out = set()
        for line in self._chowns():
            where, rest = line.split(" -h www-data:www-data -- ")
            # `-execdir … {} +` may hand one chown several names; the
            # fixture's names hold no spaces.
            for name in rest.split(" "):
                out.add((os.path.relpath(where, self.tmp), name.removeprefix("./")))
        return out

    def test_chowns_the_bare_name_from_inside_the_folder(self):
        wp = os.path.join(self.tmp, "html", "wp-content")
        os.makedirs(os.path.join(wp, "uploads", "2026"))
        self.assertEqual(self._own(os.path.join(wp, "mu-plugins"), False).returncode, 0)
        self.assertEqual(self._chowns(), [f"{wp} -h www-data:www-data -- mu-plugins"])
        self.assertEqual(self._own(os.path.join(wp, "uploads"), True).returncode, 0)
        self.assertEqual(self._given(), {
            ("html/wp-content", "mu-plugins"),
            ("html/wp-content", "uploads"),
            ("html/wp-content/uploads", "2026"),
        })

    def test_recursive_never_enters_my_creations_or_a_link(self):
        """My Creations is the user's folder, bind-mounted at
        wp-content/creations: the restore's chown -R of wp-content handed
        it to www-data (under rootless Docker, a subordinate uid), #317."""
        wp = os.path.join(self.tmp, "html", "wp-content")
        for rel in ("uploads/a.jpg", "creations/mine.jpg",
                    "moved/creations/also-mine.jpg"):
            os.makedirs(os.path.dirname(os.path.join(wp, rel)), exist_ok=True)
            open(os.path.join(wp, rel), "w").close()
        os.makedirs(os.path.join(self.tmp, "outside"))
        open(os.path.join(self.tmp, "outside", "secret"), "w").close()
        os.symlink(os.path.join(self.tmp, "outside"), os.path.join(wp, "link"))
        self.assertEqual(self._own(wp, True).returncode, 0)
        got = self._given()
        for expected in (("html", "wp-content"),
                         ("html/wp-content", "uploads"),
                         ("html/wp-content/uploads", "a.jpg"),
                         ("html/wp-content", "moved"),
                         ("html/wp-content", "link")):   # the link itself (-h)
            self.assertIn(expected, got)
        for where, name in got:
            with self.subTest((where, name)):
                self.assertNotIn("creations", where.split(os.sep))
                self.assertNotEqual(name, "creations")
                self.assertNotIn("outside", where.split(os.sep))
                self.assertNotEqual(name, "secret")

    def test_a_link_among_the_folders_stops_it(self):
        """wp-content swapped for a link: root would chown elsewhere."""
        os.makedirs(os.path.join(self.tmp, "elsewhere", "uploads"))
        os.makedirs(os.path.join(self.tmp, "html"))
        os.symlink(os.path.join(self.tmp, "elsewhere"),
                   os.path.join(self.tmp, "html", "wp-content"))
        r = self._own(os.path.join(self.tmp, "html", "wp-content", "uploads"), True)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self._chowns(), [])

    def test_only_canonical_paths(self):
        for bad in ("relative/uploads", "/var/www/html/wp-content/",
                    "/var/www/html/../etc/passwd", "/var/www/html/wp-content/..",
                    "/var/www//html/x", "/"):
            with self.subTest(bad):
                with self.assertRaises(ValueError):
                    wp_files.own_args(bad)


class TestPutTree(_Sandbox):
    """wp_files.put_tree against a docker stub that runs the container
    side's script in a sandbox."""

    def setUp(self):
        super().setUp()
        self.box = os.path.join(self.tmp, "container")
        os.makedirs(os.path.join(self.box, "var/www/html/wp-content/themes"))
        self.calls = os.path.join(self.tmp, "docker-calls")
        self.docker = os.path.join(self.tmp, "docker")
        with open(self.docker, "w") as f:
            f.write(f"""#!/bin/bash
echo "$*" >> {self.calls!r}
if [ "$1 $2 $3 $4 $5 $6 $7" = "exec -i -u www-data onionpress-wordpress sh -c" ]; then
    exec sh -c "$8" sh {self.box!r}"${{10}}"
fi
[ "$1 $2 $3 $4" = "exec onionpress-wordpress sh -c" ] && exit 0
exit 1
""")
        os.chmod(self.docker, 0o755)
        self.src = os.path.join(self.tmp, "theme-src")
        self._file("theme-src/style.css", b"body {}")
        self._file("theme-src/inc/functions.php", b"<?php // new", 0o755)

    def _tree(self, rel):
        root = os.path.join(self.box, rel)
        out = {}
        for d, dirs, files in os.walk(root):
            for name in files:
                p = os.path.join(d, name)
                with open(p, "rb") as f:
                    out[os.path.relpath(p, root)] = f.read()
        return out

    def test_replaces_the_folder_whole(self):
        dest = "/var/www/html/wp-content/themes/onionpress"
        self._file("container" + dest + "/old.php", b"<?php // stale")
        r = wp_files.put_tree(self.src, dest, docker_bin=self.docker, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._tree(dest[1:]), {"style.css": b"body {}",
                                                "inc/functions.php": b"<?php // new"})
        self.assertEqual(os.listdir(os.path.dirname(os.path.join(self.box, dest[1:]))),
                         ["onionpress"])
        self.assertTrue(os.stat(os.path.join(self.box, dest[1:], "inc/functions.php"))
                        .st_mode & stat.S_IXUSR)
        with open(self.calls) as f:
            calls = f.read().splitlines()
        # Root first gives www-data what older versions left root's there.
        self.assertTrue(calls[0].startswith("exec onionpress-wordpress sh -c "), calls)
        self.assertTrue(calls[0].endswith(
            " sh /var/www/html/wp-content/themes onionpress -R"), calls)

    def test_merge_keeps_what_is_there(self):
        dest = "/var/www/html/wp-content"
        self._file("container" + dest + "/uploads/kept.jpg", b"jpg")
        r = wp_files.put_tree(self.src, dest, merge=True, docker_bin=self.docker,
                              timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        tree = self._tree(dest[1:])
        self.assertEqual(tree["uploads/kept.jpg"], b"jpg")
        self.assertEqual(tree["style.css"], b"body {}")

    def test_the_launchers_cli_helper(self):
        """`onionpress.cli put-wp-tree`: the bash launchers' restore and
        plugin installs."""
        from onionpress import cli
        os.makedirs(os.path.join(self.box, "var/www/html/wp-content/plugins"))
        os.rename(self.docker, os.path.join(self.tmp, "bin-docker"))
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir)
        os.rename(os.path.join(self.tmp, "bin-docker"), os.path.join(bin_dir, "docker"))
        dest = "/var/www/html/wp-content/plugins/cache-enabler"
        with mock.patch.dict(os.environ, {"PATH": bin_dir + os.pathsep + os.environ["PATH"]}):
            rc = cli.main(["put-wp-tree", "--src", self.src, "--dest", dest])
            self.assertEqual(rc, 0)
            self.assertEqual(self._tree(dest[1:])["style.css"], b"body {}")
            os.symlink("/etc/passwd", os.path.join(self.src, "passwd.css"))
            with mock.patch("sys.stderr"):
                rc = cli.main(["put-wp-tree", "--src", self.src, "--dest", dest])
            self.assertEqual(rc, 1)

    def test_a_link_in_the_source_is_refused_and_nothing_replaced(self):
        dest = "/var/www/html/wp-content/themes/onionpress"
        self._file("container" + dest + "/old.php", b"<?php // stale")
        os.symlink("/etc/passwd", os.path.join(self.src, "passwd.css"))
        with self.assertRaises(ValueError):
            wp_files.put_tree(self.src, dest, docker_bin=self.docker, timeout=60)
        self.assertEqual(self._tree(dest[1:]), {"old.php": b"<?php // stale"})
        self.assertEqual(os.listdir(os.path.dirname(os.path.join(self.box, dest[1:]))),
                         ["onionpress"])


class TestLaunchersShareThePutScript(unittest.TestCase):
    def test_wp_put_runs_wp_files_script_as_www_data(self):
        for path in LAUNCHERS:
            text = _read(path)
            start = text.index("\nwp_put() {\n")
            body = text[start:text.index("\n}\n", start)]
            with self.subTest(path):
                self.assertIn("docker exec -i -u www-data onionpress-wordpress", body)
                m = re.search(r"sh -c '([^']*)' \\\n\s*sh \"\$1\" \"\$\{2:-600\}\"", body)
                self.assertIsNotNone(m, body)
                self.assertEqual(m.group(1), wp_files.PUT_FILE_SCRIPT)

    def test_linux_wp_exec_is_www_data(self):
        text = _read("linux/onionpress")
        start = text.index("\nwp_exec() {\n")
        self.assertIn('docker exec -u www-data onionpress-wordpress "$@"',
                      text[start:text.index("\n}\n", start)])


class TestNoRootWritesIntoWwwDataFolders(unittest.TestCase):
    """The sources, scanned: nothing writes, chowns or copies into the
    WordPress container's www-data folders as root again."""

    PY = ("src/menubar.py", "linux/onionpress-heartbeat.py",
          "src/onionpress/reachability.py",
          "src/onionpress/multisite.py", "src/onionpress/onion_proxy.py",
          "src/onionpress/derivatives.py", "src/onionpress/backup.py",
          "src/onionpress/wp_files.py", "src/onionpress/setup_logic.py")
    def _sources(self):
        return {p: _read(p) for p in self.PY + LAUNCHERS}

    def test_no_tee_or_redirect_into_the_container_as_root(self):
        for path, text in self._sources().items():
            for n, line in _code_lines(text):
                if "onionpress-wordpress" not in line or "-u www-data" in line:
                    continue
                with self.subTest(f"{path}:{n}"):
                    self.assertNotRegex(line, r"\btee\b|cat >|echo [^|\n]*> */var")

    def test_no_docker_cp_into_the_wordpress_container(self):
        for path, text in self._sources().items():
            for n, line in _code_lines(text):
                with self.subTest(f"{path}:{n}"):
                    # bash: `docker cp <host path> onionpress-wordpress:…`
                    self.assertNotRegex(line, r"docker cp (?!\"?onionpress-wordpress:)\S+ \"?onionpress-wordpress:")
            # Python: a "onionpress-wordpress:<path>" argument is only ever a
            # source (restore-upload.zip).
            for m in re.finditer(r"""["']onionpress-wordpress:(/[^"']*)["']""", text):
                with self.subTest(f"{path}: {m.group(1)}"):
                    self.assertEqual(m.group(1), "/var/lib/onionpress/restore-upload.zip",
                                     m.group(0))

    def test_no_root_chown_of_a_web_root_path(self):
        """Every chown there goes through wp_files.own_args (cd -P, a bare
        name, -h or -R). A path argument is followed through a link among
        its folders, a trailing slash through the last one too."""
        for path, text in self._sources().items():
            for n, line in _code_lines(text):
                with self.subTest(f"{path}:{n}"):
                    self.assertNotRegex(line, r"chown\b[^\n]*www-data:www-data[^\n]*/var/www")
                    self.assertNotRegex(line, r"chown[^\n]*/['\"\s]*$")

    def test_launchers_touch_www_data_folders_as_www_data(self):
        pattern = re.compile(
            r"docker (?:compose )?exec\b(?![^\n]*-u www-data)[^\n]*"
            r"(?:onionpress-wordpress|\bwordpress\b)[^\n]*(?:/var/lib/onionpress|/var/www)")
        for path in LAUNCHERS:
            for n, line in _code_lines(_read(path)):
                if re.search(r"\btest -[fs]\b", line):
                    continue  # read-only checks
                with self.subTest(f"{path}:{n}"):
                    self.assertNotRegex(line, pattern)

    def test_python_touches_the_shared_folder_as_www_data(self):
        """Every call naming a /var/lib/onionpress path in the WordPress
        container runs as www-data (wp_files, user=, -u www-data)."""
        for path in self.PY:
            text = _read(path)
            for node in ast.walk(ast.parse(text)):
                if not isinstance(node, ast.Call):
                    continue
                names = [c.value for c in ast.walk(node)
                         if isinstance(c, ast.Constant) and isinstance(c.value, str)
                         and "/var/lib/onionpress/" in c.value
                         # docker cp's source: copied out as a link, if one
                         # (check_uploaded_backup refuses it).
                         and c.value != "onionpress-wordpress:/var/lib/onionpress/restore-upload.zip"]
                if not names:
                    continue
                seg = ast.get_source_segment(text, node)
                if "onionpress-wordpress" not in seg and "wp_files." not in seg:
                    continue  # another container (the hub's onionheaven)
                with self.subTest(f"{path}:{node.lineno}"):
                    self.assertTrue(
                        "wp_files." in seg or "WP_USER" in seg
                        or '"-u", "www-data"' in seg or "own_args" in seg, seg)

    def test_the_onion_address_is_never_copied_by_root(self):
        for path, text in self._sources().items():
            with self.subTest(path):
                self.assertNotIn("cp /var/lib/tor/hidden_service/wordpress/hostname", text)


class TestSharedOnionAddress(unittest.TestCase):
    def _run(self, hostname_out):
        calls = []

        def fake_run(argv, **kw):
            calls.append((list(argv), kw.get("input")))
            out = hostname_out if argv[1:3] == ["exec", "onionpress-tor"] else ""
            return subprocess.CompletedProcess(argv, 0, out, "")
        with mock.patch.object(multisite.subprocess, "run", fake_run):
            ok = multisite.write_shared_onion_address()
        return ok, calls

    def test_read_from_tor_written_by_www_data(self):
        ok, calls = self._run(ADDR + "\n")
        self.assertTrue(ok)
        self.assertEqual(calls[0][0], ["docker", "exec", "onionpress-tor", "cat",
                                       "/var/lib/tor/hidden_service/wordpress/hostname"])
        self.assertEqual(calls[1], (["docker"] + wp_files.put_file_args(
            "/var/lib/onionpress/onion_address", "644"), ADDR + "\n"))

    def test_anything_but_one_address_is_not_written(self):
        for out in ("", "Generating...", ADDR + "\nSIGNAL HALT\n", ADDR.upper()):
            with self.subTest(out):
                ok, calls = self._run(out)
                self.assertFalse(ok)
                self.assertEqual(len(calls), 1)


class TestDockerExecUser(unittest.TestCase):
    def test_user_goes_before_the_container(self):
        d = Docker.__new__(Docker)
        d.run = mock.Mock(return_value="r")
        d.exec("onionpress-wordpress", ["cat", "/x"], user="www-data")
        self.assertEqual(d.run.call_args.args[0],
                         ["exec", "-u", "www-data", "onionpress-wordpress", "cat", "/x"])
        d.exec("onionpress-wordpress", "rm -f /x")
        self.assertEqual(d.run.call_args.args[0],
                         ["exec", "onionpress-wordpress", "sh", "-c", "rm -f /x"])


class TestSecurityAuditReport(_Sandbox):
    """onionpress-security-audit.sh runs as root in the WordPress container
    at every start and wrote its report through whatever www-data had put
    at /var/lib/onionpress/security-report.txt."""

    SCRIPT = "app/Resources/docker/wordpress/onionpress-security-audit.sh"

    def setUp(self):
        super().setUp()
        self.text = _read(self.SCRIPT)

    def test_nothing_writes_the_published_name_directly(self):
        code = "\n".join(line for _, line in _code_lines(self.text))
        self.assertNotRegex(code, r'>>? *"\$REPORT"')
        self.assertIn('rm -f "$REPORT"', code)
        self.assertIn('dd if="$report_tmp" of="$REPORT" conv=excl', code)
        self.assertIn("trap publish_report EXIT", code)

    def test_quarantine_is_out_of_www_data_reach(self):
        m = re.search(r"^QUARANTINE=(\S+)$", self.text, re.M)
        self.assertFalse(m.group(1).startswith("/var/lib/onionpress/"), m.group(1))
        self.assertIn('mkdir -p -m 700 "$QUARANTINE"', self.text)
        body = self.text[self.text.index("\nquarantine() {"):]
        body = body[:body.index("\n}\n")]
        self.assertIn('cd -P -- "$dir"', body)
        self.assertIn('[ "$(pwd -P)" = "$dir" ]', body)

    def test_publish_replaces_a_planted_link(self):
        """Run publish_report with a dd that has GNU's conv=excl (O_EXCL),
        which this Mac's dd lacks."""
        fn = self.text[self.text.index("\npublish_report() {"):]
        fn = fn[:fn.index("\n}\n") + 3]
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir)
        with open(os.path.join(bin_dir, "dd"), "w") as f:
            f.write(f"""#!{sys.executable}
import os, sys
a = dict(x.split("=", 1) for x in sys.argv[1:])
assert a["conv"] == "excl"
fd = os.open(a["of"], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
with open(a["if"], "rb") as src:
    os.write(fd, src.read())
""")
        os.chmod(os.path.join(bin_dir, "dd"), 0o755)
        target = self._file("outside/docker-entrypoint.sh", b"#!/bin/sh\n", 0o755)
        report = os.path.join(self.tmp, "volume", "security-report.txt")
        os.makedirs(os.path.dirname(report))
        os.symlink(target, report)
        tmp_report = self._file("report-tmp", b"No indicators of compromise found.\n")
        r = subprocess.run(
            ["bash", "-c", "warn() { echo \"$*\" >&2; }\n" + fn + "publish_report\n"],
            env=dict(os.environ, PATH=bin_dir + os.pathsep + os.environ["PATH"],
                     REPORT=report, report_tmp=tmp_report),
            capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.islink(report))
        with open(report, "rb") as f:
            self.assertEqual(f.read(), b"No indicators of compromise found.\n")
        with open(target, "rb") as f:
            self.assertEqual(f.read(), b"#!/bin/sh\n")
        self.assertFalse(os.path.exists(tmp_report))


class TestWatchdogSharedAddress(_Sandbox):
    """tor-watchdog.py sends the shared volume's onion_address to the
    control port (GETINFO, HSFETCH): www-data writes that file."""

    def setUp(self):
        super().setUp()
        path = os.path.join(PROJECT_ROOT, "app", "Resources", "docker", "tor",
                            "tor-watchdog.py")
        spec = importlib.util.spec_from_file_location("tor_watchdog_shared_addr", path)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.mod.HS_BASE_DIR = os.path.join(self.tmp, "hs")
        self.path = os.path.join(self.tmp, "volume", "onion_address")
        os.makedirs(os.path.dirname(self.path))
        self.mod.SHARED_ONION_ADDRESS_PATH = self.path

    def _found(self):
        return self.mod.discover_onion_addresses()

    def test_one_address_is_used(self):
        self._file("volume/onion_address", (ADDR + "\n").encode())
        self.assertEqual(self._found(), [ADDR[:-len(".onion")]])

    def test_a_control_command_never_rides_along(self):
        for content in (ADDR + "\r\nSIGNAL HALT\r\nx.onion",
                        "x\r\nSETCONF ControlPort=0.0.0.0:9051\r\n" + ADDR,
                        ADDR.upper(), "a" * 56 + ".onion.onion", ""):
            with self.subTest(content):
                with open(self.path, "w") as f:
                    f.write(content)
                self.assertEqual(self._found(), [])

    def test_a_link_or_fifo_is_not_read(self):
        secret = self._file("hs-secret/hostname", (ADDR + "\n").encode())
        os.symlink(secret, self.path)
        self.assertEqual(self._found(), [])
        os.unlink(self.path)
        os.mkfifo(self.path)
        self.assertEqual(self._found(), [])  # and does not hang


class TestPluginDownloadFolder(unittest.TestCase):
    def test_the_zip_lands_in_a_folder_of_roots_own(self):
        sh = _read("app/Resources/scripts/install_plugin.sh")
        self.assertNotIn("/tmp/onionpress-plugin-$$", sh)
        self.assertIn('docker exec "$CONTAINER" mktemp -d /tmp/onionpress-plugin-XXXXXX', sh)
        self.assertIn('REMOTE_ZIP="${REMOTE_DIR}/plugin.zip"', sh)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""The .deb's postinst and install.sh make ~/.onionpress as the user (issue #471).

Through 2.5.6 the postinst (a heredoc in build/build-linux.sh) ran, as
root, `install -d` without `-m 700` (a 0755 data dir), `cat >
"$DATA_DIR/secrets"` under umask 022 and only then `chmod 600` (a window
in which any local user could open the passwords and keep the fd), `cat
> "$DATA_DIR/config"` with no chmod at all, and `[ ! -f ... ]` checks that
follow links: a dangling link planted at ~/.onionpress, secrets or config
made root create a file of fixed content wherever it pointed. install.sh
run under sudo did the same, then `chown -R`'d the lot to the user.

Now both scripts carry the same three functions (`plain_path`,
`data_dir_setup`, `first_install_files`, held identical here) and run
them as the user, the postinst through `runuser -u "$REAL_USER" --`,
install.sh through its `run_as_user`: root writes nothing into the home
folder. The folder is `install -d -m 700`; secrets and config are
written under `umask 077` with `set -C` (`>` creates only, so a link at
the name, dangling or not, fails instead of being followed), so both are
0600 from their first byte; a name holding a link, or anything but a
folder / regular file, is refused with a message and left alone; an
existing secrets or config is never rewritten. The ownership fixup is
gone with the root writes it existed for.

The static part reads both scripts; the behavioural part runs the
functions under bash against a temp HOME with a 022 umask, and the whole
generated postinst with the first-install harness's stubs and the real
`install`.
"""
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_deb_upgrade_restarts import BUILD, _code, _heredoc, _read  # noqa: E402
import test_deb_first_install_docker as first_install  # noqa: E402

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
INSTALLER = "linux/install.sh"
FUNCTIONS = ("plain_path", "data_dir_setup", "first_install_files", "data_dir_script")


def _postinst():
    return _heredoc(_read(BUILD), "POSTINST")


def _function(text, name, where):
    m = re.search(r"^" + re.escape(name) + r"\(\) \{.*?^\}$", text, re.DOTALL | re.MULTILINE)
    if m is None:
        raise AssertionError(f"{where}: no function {name}()")
    return m.group(0)


def _functions(text, where):
    return "\n".join(_function(text, name, where) for name in FUNCTIONS)


SCRIPTS = {
    "build/build-linux.sh (postinst)": _postinst,
    INSTALLER: lambda: _read(INSTALLER),
}


class TestText(unittest.TestCase):

    def test_the_functions_are_the_same_in_both_scripts(self):
        copies = {where: _functions(get(), where) for where, get in SCRIPTS.items()}
        a, b = copies.values()
        self.assertEqual(a, b, "the postinst's and install.sh's data-dir functions drifted")

    def test_the_postinst_runs_them_as_the_user(self):
        code = _code(_postinst())
        self.assertRegex(code, re.compile(
            r'^\s+runuser -u "\$REAL_USER" -- bash -c "\$\(data_dir_script "\$1"\)"$', re.M))
        self.assertRegex(code, re.compile(r"^as_real_user data_dir_setup", re.M))
        self.assertRegex(code, re.compile(r"^\s+as_real_user first_install_files", re.M))
        # Only root's own home is written by root (the no-user fallback).
        self.assertRegex(code, re.compile(
            r'^\s+if \[ "\$REAL_USER" = "root" \]; then\n\s+bash -c "\$\(data_dir_script "\$1"\)"$', re.M))

    def test_install_sh_runs_them_as_the_user(self):
        code = _code(_read(INSTALLER))
        self.assertIn('run_as_user bash -c "$(data_dir_script data_dir_setup)"', code)
        self.assertIn('run_as_user bash -c "$(data_dir_script first_install_files)"', code)

    def test_the_first_install_files_are_written_only_there(self):
        for where, get in SCRIPTS.items():
            with self.subTest(script=where):
                code = _code(get())
                writes = re.findall(r'(?:>|>>|touch|install|cp)\s+"\$DATA_DIR/(?:secrets|config)"', code)
                inside = re.findall(r'(?:>|>>|touch|install|cp)\s+"\$DATA_DIR/(?:secrets|config)"',
                                    _code(_function(get(), "first_install_files", where)))
                self.assertEqual(sorted(writes), sorted(inside), f"{where}: a write outside the function")
                self.assertEqual(len(inside), 2)

    def test_no_root_write_into_the_data_dir_remains(self):
        for where, get in SCRIPTS.items():
            with self.subTest(script=where):
                code = _code(get())
                names = r'"\$DATA_DIR(/[^"]*)?"'
                self.assertNotRegex(code, re.compile(r"\bchown\b.*" + names),
                                    f"{where}: the ownership fixup is for root writes that no longer happen")
                self.assertNotRegex(code, re.compile(r"install -d -o.*" + names),
                                    f"{where}: the folder is made as the user, not for them")
                self.assertNotRegex(code, re.compile(r'mkdir -p "\$DATA_DIR'))

    def test_root_opens_no_file_under_the_data_dir(self):
        """Issue #490: the Docker bootstrap's log was root's `>` and `chown`
        under ~/.onionpress. Every redirection into $DATA_DIR (or the log's
        variable) is inside a function the user's shell runs."""
        code = _code(_postinst())
        user_side = [_function(code, name, "postinst")
                     for name in ("first_install_files", "as_user_logging_to")]
        outside = code
        for body in user_side:
            outside = outside.replace(body, "")
        self.assertNotRegex(outside, re.compile(r'>>?\s*"\$(DATA_DIR/|_rootless_log)'))
        self.assertNotRegex(outside, re.compile(r"\bchown\b.*\$(DATA_DIR|_rootless_log)"))
        self.assertIn('as_user_logging_to "$_rootless_log" ">"', outside)
        self.assertIn('as_user_logging_to "$_rootless_log" ">>"', outside)
        helper = user_side[1]
        self.assertIn('runuser -u "$REAL_USER" -- bash -c', helper)
        self.assertIn("umask 077", helper)
        self.assertIn('[ -L "$log" ]', helper)

    def test_the_functions_make_0700_and_0600_without_a_window(self):
        fn = _functions(_postinst(), "postinst")
        setup = _function(fn, "data_dir_setup", "postinst")
        files = _function(fn, "first_install_files", "postinst")
        self.assertIn('install -d -m 700 "$DATA_DIR"', setup)
        self.assertRegex(setup, re.compile(r"^\s+umask 077$", re.M))
        self.assertRegex(files, re.compile(r"^\s+umask 077$", re.M))
        self.assertRegex(files, re.compile(r"^\s+set -C$", re.M))
        # The existing-file rule: neither file is ever rewritten.
        self.assertIn('if [ ! -f "$DATA_DIR/secrets" ]; then', files)
        self.assertIn('if [ ! -f "$DATA_DIR/config" ]; then', files)
        # Every name is checked for a link before `-f`/`-d` could follow one.
        self.assertIn('[ ! -L "$1" ]', _function(fn, "plain_path", "postinst"))
        self.assertIn('if [ -L "$DATA_DIR" ] || [ ! -d "$DATA_DIR" ]; then', files)
        self.assertIn('for f in "$DATA_DIR/secrets" "$DATA_DIR/config"; do', files)
        self.assertIn('if ! plain_path "$f" -f; then', files)


class TestFunctionsUnderBash(unittest.TestCase):
    """The functions, sourced from each script, run as this user against
    a temp HOME under a 022 umask: what `runuser -u <user> -- bash -c` runs."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="op-postinst-data-")
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", "--", self.tmp], check=False))
        self.sources = {where: os.path.join(self.tmp, f"fns-{i}.sh")
                        for i, where in enumerate(SCRIPTS)}
        for where, get in SCRIPTS.items():
            with open(self.sources[where], "w") as f:
                f.write(_functions(get(), where) + "\n")

    def _run(self, where, fn, data_dir):
        script = (f"umask 022\n. {self.sources[where]!r}\nDATA_DIR={data_dir!r}\n"
                  f'bash -c "$(data_dir_script {fn})"\n')
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                              timeout=60, env={"PATH": "/usr/bin:/bin", "HOME": self.tmp,
                                               "LC_ALL": "C"})

    def _mode(self, path):
        return stat.S_IMODE(os.lstat(path).st_mode)

    def _each(self):
        for n, where in enumerate(SCRIPTS):
            home = os.path.join(self.tmp, f"home{n}")
            os.mkdir(home)
            yield where, os.path.join(home, ".onionpress")

    def test_a_fresh_install_makes_0700_and_two_0600_files(self):
        for where, data in self._each():
            with self.subTest(script=where):
                r = self._run(where, "data_dir_setup", data)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(self._mode(data), 0o700)
                self.assertTrue(os.path.isdir(os.path.join(data, "shared", "vanity-keys")))
                r = self._run(where, "first_install_files", data)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("Database passwords generated", r.stdout)
                self.assertIn("Default config created", r.stdout)
                for name in ("secrets", "config"):
                    self.assertEqual(self._mode(os.path.join(data, name)), 0o600, name)
                with open(os.path.join(data, "secrets")) as f:
                    secrets = f.read()
                for key in ("WORDPRESS_DB_PASSWORD", "MYSQL_PASSWORD", "MYSQL_ROOT_PASSWORD"):
                    self.assertRegex(secrets, re.compile(r"^" + key + r"='[A-Za-z0-9]{32}'$", re.M))
                with open(os.path.join(data, "config")) as f:
                    config = f.read()
                self.assertIn("ADDRESS_PREFIX=op2\n", config)
                self.assertIn("REGISTER_WITH_ONIONHEAVEN=yes\n", config)
                self.assertIn("#ONIONPRESS_BIND_HOST=127.0.0.1\n", config)

    def test_existing_files_are_kept_and_a_loose_dir_tightened(self):
        for where, data in self._each():
            with self.subTest(script=where):
                os.makedirs(data, mode=0o755)
                for name, body in (("secrets", "MYSQL_ROOT_PASSWORD='mine'\n"), ("config", "MINE=yes\n")):
                    with open(os.path.join(data, name), "w") as f:
                        f.write(body)
                    os.chmod(os.path.join(data, name), 0o644)
                r = self._run(where, "data_dir_setup", data)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(self._mode(data), 0o700)
                r = self._run(where, "first_install_files", data)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("Existing secrets preserved", r.stdout)
                self.assertNotIn("Default config created", r.stdout)
                with open(os.path.join(data, "secrets")) as f:
                    self.assertEqual(f.read(), "MYSQL_ROOT_PASSWORD='mine'\n")
                with open(os.path.join(data, "config")) as f:
                    self.assertEqual(f.read(), "MINE=yes\n")
                # The owner's own files: their modes are not changed behind their back.
                self.assertEqual(self._mode(os.path.join(data, "config")), 0o644)

    def test_a_link_at_the_data_dir_is_refused_whole(self):
        for where, data in self._each():
            with self.subTest(script=where):
                target = os.path.join(os.path.dirname(data), "elsewhere")
                os.symlink(target, data)
                for fn in ("data_dir_setup", "first_install_files"):
                    r = self._run(where, fn, data)
                    self.assertNotEqual(r.returncode, 0, fn)
                    self.assertIn(f"{data} is a link or not a folder", r.stderr)
                    self.assertFalse(os.path.lexists(target), f"{fn}: the link was followed")
                self.assertTrue(os.path.islink(data))

    def test_a_link_at_secrets_or_config_is_refused_and_nothing_written(self):
        for where, data in self._each():
            for planted in ("secrets", "config"):
                with self.subTest(script=where, planted=planted):
                    if os.path.lexists(data):
                        subprocess.run(["rm", "-rf", "--", data], check=True)
                    os.makedirs(data, mode=0o700)
                    target = os.path.join(os.path.dirname(data), f"victim-{planted}")
                    os.symlink(target, os.path.join(data, planted))
                    r = self._run(where, "first_install_files", data)
                    self.assertNotEqual(r.returncode, 0)
                    self.assertIn(f"{data}/{planted} is a link or not a regular file", r.stderr)
                    self.assertFalse(os.path.lexists(target), "the link was followed")
                    self.assertEqual(sorted(os.listdir(data)), [planted],
                                     "the other file is not written either")

    def test_a_link_to_a_real_file_is_refused_too(self):
        # Not only dangling ones: `-f` is true through a link to a file.
        for where, data in self._each():
            with self.subTest(script=where):
                os.makedirs(data, mode=0o700)
                real = os.path.join(os.path.dirname(data), "real")
                with open(real, "w") as f:
                    f.write("theirs\n")
                os.symlink(real, os.path.join(data, "config"))
                r = self._run(where, "first_install_files", data)
                self.assertNotEqual(r.returncode, 0)
                with open(real) as f:
                    self.assertEqual(f.read(), "theirs\n")

    def test_a_file_at_the_data_dir_or_a_link_below_it_is_refused(self):
        for where, data in self._each():
            with self.subTest(script=where, what="file at the dir"):
                with open(data, "w") as f:
                    f.write("x")
                r = self._run(where, "data_dir_setup", data)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("is a link or not a folder", r.stderr)
                os.unlink(data)
            with self.subTest(script=where, what="link at shared/vanity-keys"):
                os.makedirs(os.path.join(data, "shared"), mode=0o700)
                target = os.path.join(os.path.dirname(data), "keys-elsewhere")
                os.symlink(target, os.path.join(data, "shared", "vanity-keys"))
                r = self._run(where, "data_dir_setup", data)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("vanity-keys is a link or not a folder", r.stderr)
                self.assertFalse(os.path.lexists(target))

    def test_the_create_only_write_refuses_a_name_that_exists(self):
        # What `set -C` buys past the checks: `>` is O_EXCL, so a link that
        # appears between the check and the write fails the write.
        for where, data in self._each():
            with self.subTest(script=where):
                os.makedirs(data, mode=0o700)
                script = (f". {self.sources[where]!r}\nDATA_DIR={data!r}\n"
                          "plain_path() { return 0; }\n"
                          f'ln -s {os.path.join(self.tmp, "late")!r} "$DATA_DIR/config"\n'
                          'bash -c "$(data_dir_script first_install_files)"\n')
                r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                                   timeout=60, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("cannot overwrite existing file", r.stderr)
                self.assertFalse(os.path.lexists(os.path.join(self.tmp, "late")))


class TestWholePostinst(unittest.TestCase):
    """The generated postinst with the first-install harness's stubs, but a
    real `install` and a `runuser` that runs what it is given: the data dir
    lands in the user's home with the right modes, through runuser."""

    def setUp(self):
        base = first_install.TestFirstInstallRuns
        self.tmp = tempfile.mkdtemp(prefix="op-postinst-whole-")
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", "--", self.tmp], check=False))
        self.bin = os.path.join(self.tmp, "bin")
        os.mkdir(self.bin)
        for tool in base.TOOLS + ("install",):
            for d in ("/usr/bin", "/bin"):
                if os.path.exists(os.path.join(d, tool)):
                    os.symlink(os.path.join(d, tool), os.path.join(self.bin, tool))
                    break
            else:
                self.skipTest(f"{tool} not found")
        stubs = dict(base.STUBS)
        del stubs["install"]
        stubs["chown"] = '#!/bin/bash\necho "chown $*" >> "$STUB_LOG"\n'
        for name, body in stubs.items():
            path = os.path.join(self.bin, name)
            with open(path, "w") as f:
                f.write(body)
            os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        self.contrib = os.path.join(self.tmp, "docker.io-contrib")
        os.mkdir(self.contrib)
        for name, body in (("dockerd-rootless-setuptool.sh", base.SETUPTOOL),
                           ("dockerd-rootless.sh", "#!/bin/sh\n")):
            path = os.path.join(self.contrib, name)
            with open(path, "w") as f:
                f.write(body)
            os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        self.home = os.path.join(self.tmp, "home")
        os.mkdir(self.home)
        self.data = os.path.join(self.home, ".onionpress")
        self.log = os.path.join(self.tmp, "calls.log")
        self.script = os.path.join(self.tmp, "postinst")
        with open(self.script, "w") as f:
            f.write(_postinst() + "\n")

    def _run(self, *args):
        env = {
            "PATH": self.bin, "HOME": self.home, "SUDO_USER": "opuser", "LC_ALL": "C",
            "STUB_LOG": self.log, "STUB_HOME": self.home,
            "ONIONPRESS_DOCKER_IO_CONTRIB": self.contrib,
        }
        old = os.umask(0o022)
        try:
            r = subprocess.run(["bash", self.script, "configure", *args], capture_output=True,
                               text=True, env=env, timeout=60)
        finally:
            os.umask(old)
        # The runuser stub logs the whole `bash -c` script, newlines and all.
        log = ""
        if os.path.exists(self.log):
            with open(self.log) as f:
                log = f.read()
        return r, log

    def _mode(self, path):
        return stat.S_IMODE(os.lstat(path).st_mode)

    def test_a_first_install_makes_the_data_dir_as_the_user(self):
        r, log = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(log.count("runuser -u opuser -- bash -c set -e; "), 2, log)
        setup = log.index(f"DATA_DIR={self.data}; data_dir_setup\n")
        files = log.index(f"DATA_DIR={self.data}; first_install_files\n")
        self.assertLess(setup, files)
        self.assertEqual(self._mode(self.data), 0o700)
        self.assertEqual(self._mode(os.path.join(self.data, "secrets")), 0o600)
        self.assertEqual(self._mode(os.path.join(self.data, "config")), 0o600)
        self.assertTrue(os.path.isdir(os.path.join(self.data, "shared", "vanity-keys")))
        self.assertNotRegex(log, re.compile(
            r"^chown .*" + re.escape(self.data) + r"(/secrets|/config)?$", re.M),
            "root chowns neither the folder nor its two files")

    def test_an_upgrade_makes_the_folder_and_leaves_the_files_alone(self):
        os.makedirs(self.data, mode=0o755)
        r, log = self._run("2.5.6")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._mode(self.data), 0o700)
        self.assertEqual(sorted(os.listdir(self.data)), ["shared"])
        self.assertNotIn("; first_install_files\n", log)

    def test_a_link_at_the_docker_log_is_not_written_through(self):
        """Issue #490: a link planted at docker-rootless-setup.log gets no
        output, its target stays untouched, the install goes on."""
        os.makedirs(self.data, mode=0o700)
        target = os.path.join(self.tmp, "root-owned-target")
        with open(target, "w") as f:
            f.write("untouched\n")
        os.symlink(target, os.path.join(self.data, "docker-rootless-setup.log"))
        r, log = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("docker-rootless-setup.log is a link or not a regular file", r.stderr)
        with open(target) as f:
            self.assertEqual(f.read(), "untouched\n")
        self.assertTrue(os.path.islink(os.path.join(self.data, "docker-rootless-setup.log")))
        self.assertIn("setuptool ", log, "the bootstrap still ran")

    def test_the_docker_log_is_the_users_and_private(self):
        r, _log = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        path = os.path.join(self.data, "docker-rootless-setup.log")
        self.assertEqual(self._mode(path), 0o600)
        with open(path) as f:
            self.assertIn("docker: not found", f.read())

    def test_a_planted_link_stops_the_data_dir_not_the_install(self):
        target = os.path.join(self.tmp, "elsewhere")
        os.symlink(target, self.data)
        r, _log = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"{self.data} is a link or not a folder", r.stderr)
        self.assertIn("Could not set up", r.stderr)
        self.assertIn("Could not write secrets and config", r.stderr)
        self.assertFalse(os.path.lexists(target))
        self.assertTrue(os.path.islink(self.data))


if __name__ == "__main__":
    unittest.main()

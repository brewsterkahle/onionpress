"""Issue #397: ~/.onionpress is this user's alone, and config is 0600.

Both launchers `mkdir -p`'d the data dir with the default umask, and
config was a copy of a 0644 template (the Mac) or a plain heredoc write
(Linux, install.sh), while it may hold CLOUDFLARE_TUNNEL_TOKEN. Every
creation path now makes the dir 0700 (the launchers on every start, so
an install made under a looser umask is fixed too) and the file 0600.

The other half of the issue, moving the mkp224o key out of shared/
(the one folder mounted into the Colima VM) once it is seeded into the
onionpress-onion-keys volume, is a design change left to its own PR:
the seed helper can read the key only from shared/, and restores, key
imports and the hub detection all keep that folder.
"""

import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAC = os.path.join(PROJECT_ROOT, "app/MacOS/onionpress")
LINUX = os.path.join(PROJECT_ROOT, "linux/onionpress")
INSTALL = os.path.join(PROJECT_ROOT, "linux/install.sh")
# The .deb's postinst, a heredoc in the build script (issue #471; its
# behaviour is in tests/test_deb_postinst_data_dir.py).
BUILD = os.path.join(PROJECT_ROOT, "build/build-linux.sh")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _block(text, start_marker, end_marker):
    start = text.index(start_marker)
    return text[start:text.index(end_marker, start)]


class TestLaunchersStatic(unittest.TestCase):

    def test_both_launchers_chmod_the_data_dir_right_after_making_it(self):
        for path in (MAC, LINUX):
            with self.subTest(launcher=os.path.relpath(path, PROJECT_ROOT)):
                text = _read(path)
                self.assertRegex(text, r'mkdir -p "\$DATA_DIR"\nchmod 700 "\$DATA_DIR"\n')

    def test_every_config_creation_sets_0600(self):
        for path in (MAC, LINUX, INSTALL, BUILD):
            with self.subTest(file=os.path.relpath(path, PROJECT_ROOT)):
                text = _read(path)
                block = _block(text, 'if [ ! -f "$DATA_DIR/config" ]; then', "\nfi\n")
                self.assertIn('chmod 600 "$DATA_DIR/config"', block)


class TestLaunchersUnderBash(unittest.TestCase):
    """The two blocks of each launcher, run under bash with a mode-022
    umask and a fresh HOME."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="op-perms-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _run(self, script, data_dir, resources=None):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.tmp,
               "DATA_DIR": data_dir, "RESOURCES_DIR": resources or self.tmp}
        r = subprocess.run(["bash", "-c", "umask 022\n" + script], env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)

    def _blocks(self, path):
        text = _read(path)
        mkdir = _block(text, 'mkdir -p "$DATA_DIR"\nchmod 700 "$DATA_DIR"', "\n\n")
        config = _block(text, 'if [ ! -f "$DATA_DIR/config" ]; then', "\nfi\n") + "\nfi\n"
        return mkdir + "\n" + config

    def test_mac_from_template(self):
        template = os.path.join(self.tmp, "config-template.txt")
        with open(template, "w") as f:
            f.write("ADDRESS_PREFIX=op2\n")
        os.chmod(template, 0o644)
        data = os.path.join(self.tmp, "data")
        self._run(self._blocks(MAC), data)
        self.assertEqual(stat.S_IMODE(os.stat(data).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(data, "config")).st_mode), 0o600)
        with open(os.path.join(data, "config")) as f:
            self.assertEqual(f.read(), "ADDRESS_PREFIX=op2\n")

    def test_mac_fallback_and_linux_heredoc(self):
        for path in (MAC, LINUX):
            with self.subTest(launcher=os.path.relpath(path, PROJECT_ROOT)):
                data = os.path.join(self.tmp, os.path.basename(os.path.dirname(path)))
                self._run(self._blocks(path), data, resources=os.path.join(self.tmp, "none"))
                self.assertEqual(stat.S_IMODE(os.stat(data).st_mode), 0o700)
                cfg = os.path.join(data, "config")
                self.assertEqual(stat.S_IMODE(os.stat(cfg).st_mode), 0o600)
                with open(cfg) as f:
                    self.assertIn("ADDRESS_PREFIX=op2", f.read())

    def test_an_existing_loose_dir_is_tightened_and_a_config_kept(self):
        data = os.path.join(self.tmp, "old")
        os.makedirs(data, mode=0o755)
        with open(os.path.join(data, "config"), "w") as f:
            f.write("MINE=yes\n")
        os.chmod(os.path.join(data, "config"), 0o644)
        self._run(self._blocks(LINUX), data)
        self.assertEqual(stat.S_IMODE(os.stat(data).st_mode), 0o700)
        # An existing config is the owner's: content untouched, and its
        # mode is not changed behind their back by the launcher.
        with open(os.path.join(data, "config")) as f:
            self.assertEqual(f.read(), "MINE=yes\n")


class TestPythonSide(unittest.TestCase):

    def setUp(self):
        import sys
        sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))
        from onionpress import config as op_config
        from onionpress.platform import resolve_paths
        self.op_config = op_config
        self.resolve_paths = resolve_paths
        self.tmp = tempfile.mkdtemp(prefix="op-perms-py-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_ensure_config_from_template_is_0600(self):
        bundle = os.path.join(self.tmp, "App.app")
        res = os.path.join(bundle, "Contents", "Resources")
        os.makedirs(res)
        with open(os.path.join(res, "config-template.txt"), "w") as f:
            f.write("ADDRESS_PREFIX=op2\n")
        os.chmod(os.path.join(res, "config-template.txt"), 0o644)
        data = os.path.join(self.tmp, "data")
        os.makedirs(data)
        paths = self.resolve_paths(data_dir=data, app_bundle=bundle)
        self.op_config.ensure_config(paths)
        self.assertEqual(stat.S_IMODE(os.stat(paths.config_file).st_mode), 0o600)

    def test_ensure_config_from_defaults_is_0600(self):
        data = os.path.join(self.tmp, "data")
        os.makedirs(data)
        paths = self.resolve_paths(data_dir=data, app_bundle="")
        old = os.umask(0o022)
        try:
            self.op_config.ensure_config(paths)
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(os.stat(paths.config_file).st_mode), 0o600)

    def test_every_makedirs_of_the_data_dir_is_0700(self):
        src = os.path.join(PROJECT_ROOT, "src")
        for dirpath, _d, files in os.walk(src):
            for name in files:
                if not name.endswith(".py"):
                    continue
                text = _read(os.path.join(dirpath, name))
                for m in re.finditer(r"os\.makedirs\(([^)]*data_dir[^)]*)\)", text):
                    with self.subTest(file=name, call=m.group(0)):
                        self.assertIn("mode=0o700", m.group(1))


if __name__ == "__main__":
    unittest.main()

"""Issue #388: install.sh installs a release, parses whole, and leaves a
busy system Docker alone.

The one-command install (`curl | bash`) cloned HEAD of main and
installed it into /opt/onionpress as root, so a push to main (no release,
no tag) changed what every new Linux user ran, and an install whose
version equalled the released one told `onionpress update` it was up to
date for good. The script was not wrapped, so a dropped connection
executed a prefix. With sudo it also disabled whatever system Docker ran
there, and added Docker's apt repository with a key fetched over TLS and
never checked.
"""

import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALLER = os.path.join(PROJECT_ROOT, "linux/install.sh")
BUILD_LINUX = os.path.join(PROJECT_ROOT, "build/build-linux.sh")

DOCKER_FPR = "9DC858229FC7DD38854AE2D88D81803C0EBFCD88"


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _code(text):
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def _function(text, name):
    m = re.search(r"^" + re.escape(name) + r"\(\) \{\n.*?^\}\n", text, re.S | re.M)
    assert m, f"no function {name}"
    return m.group(0)


class TestShape(unittest.TestCase):

    def setUp(self):
        self.text = _read(INSTALLER)
        self.code = _code(self.text)

    def test_the_script_is_one_function_run_last(self):
        lines = [l for l in self.code.splitlines() if l.strip()]
        self.assertEqual(lines[-1], 'main "$@"')
        self.assertEqual(lines[-2], "}")
        # main() opens before the first command after set -e.
        body = self.code[self.code.index("set -e\n") + len("set -e\n"):]
        self.assertEqual(body.lstrip().split("\n", 1)[0], "main() {")

    def test_a_truncated_script_runs_nothing(self):
        # Cut in the middle: bash refuses the unterminated function.
        cut = self.text[:len(self.text) // 2]
        r = subprocess.run(["bash", "-c", cut], capture_output=True, text=True,
                           env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")}, timeout=30)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        self.assertIn("syntax error", r.stderr)

    def test_the_clone_is_of_the_release_ref(self):
        self.assertIn('git clone --depth 1 --branch "$INSTALL_REF" "$REPO_URL"', self.code)
        self.assertNotRegex(self.code, r'git clone --depth 1 "\$REPO_URL"')
        self.assertIn("INSTALL_REF=$(release_ref_to_install)", self.code)

    def test_the_docker_disable_is_guarded_like_postinst(self):
        guard = self.code.index('if $SUDO docker ps -q 2>/dev/null | grep -q .; then')
        disable = self.code.index("systemctl disable --now docker.service docker.socket")
        self.assertLess(guard, disable)
        # And the postinst's check is the same.
        self.assertIn("if docker ps -q 2>/dev/null | grep -q .; then", _code(_read(BUILD_LINUX)))

    def test_the_apt_key_is_checked_before_the_repository_is_added(self):
        key = self.code.index("docker_apt_key_is_dockers /etc/apt/keyrings/docker.asc")
        repo = self.code.index("/etc/apt/sources.list.d/docker.list")
        self.assertLess(key, repo)
        self.assertIn(f'DOCKER_APT_KEY_FPR="{DOCKER_FPR}"', self.code)
        self.assertRegex(self.code, r"apt-get install -y -qq uidmap[^\n]* gnupg")

    def test_postinst_says_the_docker_group_is_root_equivalent(self):
        text = _read(BUILD_LINUX)
        i = text.index('usermod -aG docker "$REAL_USER"')
        self.assertIn("root-equivalent", text[i:i + 600])


class TestFunctionsUnderBash(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="op-install-rel-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.code = _code(_read(INSTALLER))

    def _fake(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w") as f:
            f.write("#!/bin/bash\n" + body)
        os.chmod(path, stat.S_IRWXU)

    def _run(self, script, env=None):
        full_env = {"PATH": self.bin + ":/usr/bin:/bin", "HOME": self.tmp}
        full_env.update(env or {})
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                              env=full_env, timeout=30)

    def _tag_script(self, extra=""):
        return ('REPO_URL="https://example.invalid/r"\n'
                + _function(self.code, "release_ref_to_install")
                + extra + "release_ref_to_install\n")

    def test_the_newest_release_tag_wins_by_version(self):
        self._fake("git", 'cat <<EOF\n'
                   'aaa\trefs/tags/v2.5.9\n'
                   'bbb\trefs/tags/v2.5.10\n'
                   'ccc\trefs/tags/v2.4.120\n'
                   'ddd\trefs/tags/v3.0.0-rc1\n'
                   'eee\trefs/tags/v2.5.10^{}\n'
                   'EOF\n')
        r = self._run(self._tag_script())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "v2.5.10")

    def test_no_tag_or_no_network_fails_with_nothing_printed(self):
        self._fake("git", "exit 128\n")
        r = self._run(self._tag_script())
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        self._fake("git", "echo 'x\trefs/tags/latest'\n")
        r = self._run(self._tag_script())
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_an_explicit_ref_is_taken_as_given(self):
        self._fake("git", "echo should-not-run >&2; exit 1\n")
        r = self._run(self._tag_script(), env={"ONIONPRESS_INSTALL_REF": "main"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "main")
        self.assertNotIn("should-not-run", r.stderr)

    def test_the_key_check_wants_exactly_dockers_fingerprint(self):
        fn = _function(self.code, "docker_apt_key_is_dockers")
        for fpr, ok in ((DOCKER_FPR, 0), ("0" * 40, 1), ("", 1)):
            with self.subTest(fingerprint=fpr or "(none)"):
                self._fake("gpg", f'[ -n "{fpr}" ] && echo "fpr:::::::::{fpr}:"\n')
                r = self._run(f'DOCKER_APT_KEY_FPR="{DOCKER_FPR}"\n' + fn
                              + "docker_apt_key_is_dockers /dev/null\n")
                self.assertEqual(r.returncode, ok, r.stderr)

    def test_a_failing_gpg_is_a_refusal(self):
        fn = _function(self.code, "docker_apt_key_is_dockers")
        self._fake("gpg", "exit 2\n")
        r = self._run(f'DOCKER_APT_KEY_FPR="{DOCKER_FPR}"\n' + fn
                      + "docker_apt_key_is_dockers /dev/null\n")
        self.assertEqual(r.returncode, 1)


if __name__ == "__main__":
    unittest.main()

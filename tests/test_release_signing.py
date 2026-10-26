"""Issues #246 and #386 (step 2): a signature on a release's SHA256SUMS.

Both updaters install only an asset whose sha256 is its SHA256SUMS line,
but that file comes from the same place as the assets. A release may now
carry SHA256SUMS.sig, an Ed25519 signature by a key the maintainer holds
off GitHub (build/release-key.py; build/release.sh signs when
ONIONPRESS_RELEASE_SIGNING_KEY names the key), checked against
release_signing.RELEASE_SIGNING_PUBLIC_KEYS. While that tuple is empty
nothing is enforced and the updaters say so; once a key is in it, an
unsigned or mis-signed release is refused.
"""

import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from onionpress import release_signing as rs  # noqa: E402
from onionpress import updater  # noqa: E402

SUMS = b"0" * 64 + b"  onionpress.deb\n" + b"1" * 64 + b"  onionpress.dmg\n"


def _read(rel):
    with open(os.path.join(PROJECT_ROOT, rel), encoding="utf-8") as f:
        return f.read()


class TestSignAndVerify(unittest.TestCase):

    def setUp(self):
        self.seed, self.pub = rs.create_keypair()

    def test_roundtrip_and_format(self):
        text = rs.sign(SUMS, self.seed)
        lines = text.splitlines()
        self.assertEqual(lines[0], rs.SIG_HEADER)
        self.assertEqual(lines[1], f"key: {rs.key_id(self.pub)}")
        self.assertTrue(lines[2].startswith("sig: "))
        self.assertEqual(rs.verify(SUMS, text, [self.pub]), rs.key_id(self.pub))

    def test_key_id_is_sha256_of_the_public_key(self):
        self.assertEqual(rs.key_id(self.pub), hashlib.sha256(self.pub).hexdigest()[:16])

    def test_a_changed_file_is_refused(self):
        text = rs.sign(SUMS, self.seed)
        with self.assertRaisesRegex(rs.SignatureError, "does not verify"):
            rs.verify(SUMS.replace(b"0" * 64, b"2" * 64), text, [self.pub])

    def test_another_key_is_refused(self):
        other_seed, other_pub = rs.create_keypair()
        text = rs.sign(SUMS, other_seed)
        with self.assertRaisesRegex(rs.SignatureError, "not one this OnionPress trusts"):
            rs.verify(SUMS, text, [self.pub])
        # Several trusted keys: any of them counts.
        self.assertEqual(rs.verify(SUMS, text, [self.pub, other_pub]), rs.key_id(other_pub))

    def test_malformed_signatures(self):
        for text in ("", "junk\nkey: x\nsig: y\n", f"{rs.SIG_HEADER}\nkey: zz\nsig: AAAA\n",
                     f"{rs.SIG_HEADER}\nkey: {'a' * 16}\nsig: not base64!\n",
                     f"{rs.SIG_HEADER}\nkey: {'a' * 16}\nsig: AAAA\n"):
            with self.subTest(text=text[:30]):
                with self.assertRaises(rs.SignatureError):
                    rs.verify(SUMS, text, [self.pub])

    def test_policy_without_an_embedded_key(self):
        self.assertEqual(rs.RELEASE_SIGNING_PUBLIC_KEYS, (),
                         "no key yet: once one is committed, this test changes")
        ok, note = rs.check_release_sums(SUMS, None, [])
        self.assertTrue(ok)
        self.assertEqual(note, rs.NO_KEY_NOTE)
        ok, note = rs.check_release_sums(SUMS, "garbage", [])
        self.assertTrue(ok)

    def test_policy_with_a_key(self):
        keys = [self.pub]
        self.assertEqual(rs.check_release_sums(SUMS, None, keys)[0], False)
        self.assertIn("carries no SHA256SUMS.sig", rs.check_release_sums(SUMS, None, keys)[1])
        ok, note = rs.check_release_sums(SUMS, rs.sign(SUMS, self.seed), keys)
        self.assertTrue(ok)
        self.assertIn(rs.key_id(self.pub), note)
        ok, note = rs.check_release_sums(SUMS + b"x", rs.sign(SUMS, self.seed), keys)
        self.assertFalse(ok)
        self.assertIn("refused", note)

    def test_verify_files(self):
        tmp = tempfile.mkdtemp(prefix="op-relsig-")
        self.addCleanup(shutil.rmtree, tmp, True)
        sums = os.path.join(tmp, "SHA256SUMS")
        sig = sums + ".sig"
        with open(sums, "wb") as f:
            f.write(SUMS)
        with open(sig, "w") as f:
            f.write(rs.sign(SUMS, self.seed))
        with mock.patch.object(rs, "RELEASE_SIGNING_PUBLIC_KEYS", (self.pub.hex(),)):
            self.assertEqual(rs.verify_files(sums, sig), 0)
            self.assertEqual(rs.verify_files(sums, None), 1)
            self.assertEqual(rs.verify_files(sums, os.path.join(tmp, "missing")), 1)
            with open(sig, "w") as f:
                f.write("junk")
            self.assertEqual(rs.verify_files(sums, sig), 1)
            self.assertEqual(rs.verify_files(os.path.join(tmp, "none"), sig), 2)
        self.assertEqual(rs.verify_files(sums, None), 0)   # no key embedded


class TestKeyTool(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="op-relkey-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.tool = os.path.join(PROJECT_ROOT, "build", "release-key.py")

    def _run(self, *args, env=None):
        return subprocess.run([sys.executable, "-I", self.tool, *args], capture_output=True,
                              text=True, timeout=60, env={**os.environ, **(env or {})})

    def test_create_sign_verify(self):
        key = os.path.join(self.tmp, "release.key")
        r = self._run("create", key)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(stat.S_IMODE(os.stat(key).st_mode), 0o600)
        pub = re.search(r'"([0-9a-f]{64})",', r.stdout).group(1)
        self.assertIn("RELEASE_SIGNING_PUBLIC_KEYS", r.stdout)
        self.assertNotEqual(self._run("create", key).returncode, 0, "never overwrites a key")
        sums = os.path.join(self.tmp, "SHA256SUMS")
        with open(sums, "wb") as f:
            f.write(SUMS)
        r = self._run("sign", sums, env={"ONIONPRESS_RELEASE_SIGNING_KEY": key})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(sums + ".sig"))
        self.assertEqual(self._run("verify", sums, sums + ".sig", "--public-key", pub).returncode, 0)
        with open(sums, "ab") as f:
            f.write(b"x")
        self.assertEqual(self._run("verify", sums, sums + ".sig", "--public-key", pub).returncode, 1)
        self.assertNotEqual(self._run("sign", sums).returncode, 0)   # no key given


class TestWiring(unittest.TestCase):

    def test_release_sh_signs_when_the_key_is_named(self):
        text = _read("build/release.sh")
        fn = text[text.index("write_sums() {"):text.index("\n}\n", text.index("write_sums() {"))]
        self.assertIn('if [ -n "${ONIONPRESS_RELEASE_SIGNING_KEY:-}" ]; then', fn)
        self.assertIn('release-key.py" sign "$SUMS_PATH" --key', fn)
        self.assertIn("not signed", fn)
        self.assertEqual(text.count("$SIG_ASSETS"), 3, "every upload/create carries the .sig")
        self.assertIn('${SIG_ASSETS:+ SHA256SUMS.sig}', text)

    def test_linux_update_verifies_before_the_deb_download(self):
        code = "\n".join(l for l in _read("linux/onionpress").splitlines()
                         if not l.lstrip().startswith("#"))
        fn = code[code.index("update_fetch_release() {"):]
        fn = fn[:fn.index("\n}\n")]
        self.assertIn('RELEASE_SIG="SHA256SUMS.sig"', code)
        self.assertLess(fn.index('op_py release-signature verify "$dir/$RELEASE_SUMS" "$dir/$RELEASE_SIG"'),
                        fn.index('-o "$dir/$RELEASE_DEB"'))
        self.assertIn('|| rm -f "$dir/$RELEASE_SIG"', fn, "a failed download is a missing signature")

    def test_mac_updater_checks_before_the_hash(self):
        src = _read("src/onionpress/updater.py")
        body = src[src.index("def download_and_install("):]
        self.assertLess(body.index("release_signing.check_release_sums("),
                        body.index("published_sha256(sums_text, dmg_name)"))
        self.assertIn("raise RuntimeError(note)", body)

    def test_the_module_ships_in_the_bundle(self):
        self.assertIn("'onionpress.release_signing'", _read("setup.py"))


class TestMacUpdaterWithAKey(unittest.TestCase):
    """download_and_install against fake curl: with a key embedded, a
    release without a .sig or with a bad one never mounts the DMG."""

    def setUp(self):
        from tests import test_updater_staged_swap as base
        self.base = base
        self.case = base.StagedSwapTest("test_copy_goes_to_a_hidden_sibling_and_is_swapped_in")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.seed, self.pub = rs.create_keypair()
        p = mock.patch.object(rs, "RELEASE_SIGNING_PUBLIC_KEYS", (self.pub.hex(),))
        p.start()
        self.addCleanup(p.stop)
        self.sums_text = f"{base.ZEROS_SHA256}  onionpress.dmg\n{'0' * 64}  onionpress.deb\n"
        os.environ["FAKE_SUMS"] = self.sums_text

    def _serve_sig(self, text):
        os.environ["FAKE_SIG"] = text
        self.case.release["assets"].append({"name": "SHA256SUMS.sig",
                                            "browser_download_url": "https://x/SHA256SUMS.sig"})
        curl = os.path.join(self.case.root, "bin", "curl")
        with open(curl) as f:
            body = f.read()
        body = body.replace('  */SHA256SUMS) printf \'%s\' "$FAKE_SUMS" > "$out" ;;',
                            '  */SHA256SUMS.sig) printf \'%s\' "$FAKE_SIG" > "$out" ;;\n'
                            '  */SHA256SUMS) printf \'%s\' "$FAKE_SUMS" > "$out" ;;')
        with open(curl, "w") as f:
            f.write(body)

    def test_a_signed_release_installs(self):
        self._serve_sig(rs.sign(self.sums_text.encode(), self.seed))
        self.case._install()
        self.assertTrue(any("signed by release key" in m for m in self.case.logged))

    def test_an_unsigned_release_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, "carries no SHA256SUMS.sig"):
            self.case._install()
        self.assertFalse(os.path.exists(self.case.cp_log))

    def test_a_mis_signed_release_is_refused(self):
        other_seed, _ = rs.create_keypair()
        self._serve_sig(rs.sign(self.sums_text.encode(), other_seed))
        with self.assertRaisesRegex(RuntimeError, "refused"):
            self.case._install()
        self.assertFalse(os.path.exists(self.case.cp_log))


if __name__ == "__main__":
    unittest.main()

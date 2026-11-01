#!/usr/bin/env python3
"""A backup's metadata.json is never trusted in a path, config, a command or
JSON.

metadata.json travels inside the backup file, and the password proves only
that whoever made the file chose it: anyone can make one and hand it over.
Its onion_address used to name a directory under
~/.onionpress/shared/vanity-keys/ in both launchers (so "../../x" escaped
it), feed an ADDRESS_PREFIX sed expression, and be pasted into the Settings
page's restore-result.json; its username became a config line. These tests
feed crafted metadata, and crafted keys, to every restore path.

No docker: the Python paths fail the test on any docker call, and the bash
paths run the extracted launcher functions with docker stubbed out, HOME and
TMPDIR pointed at a scratch directory.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(PROJECT_ROOT, "src")
sys.path.insert(0, SRC)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from onionpress import backup, backup_crypto, cli, key_manager  # noqa: E402

LAUNCHERS = ["app/MacOS/onionpress", "linux/onionpress"]
PW = "backup-pw"

PRIV = b"\x05" * 64
PUB = key_manager.derive_public_key(PRIV)
ADDR = key_manager.derive_onion_address(PUB)
# The identity already installed when a restore starts.
OLD_PRIV = b"\x07" * 64
OLD_ADDR = key_manager.derive_onion_address(key_manager.derive_public_key(OLD_PRIV))

CRAFTED_ADDRESSES = [
    "../../../../escaped.onion",
    "../" + ADDR,
    ADDR + "/../../escaped",
    ADDR + "\n../../escaped",
    ADDR + "\n",
    " " + ADDR,
    ADDR.upper(),
    ADDR[:-6] + "1.onion",          # "1" is not base32
    "a" * 55 + ".onion",
    "a" * 57 + ".onion",
    "x.onion",
    "evil.onion\"}, \"success\": false, \"x\": {\"a\": \"",
]
# Config a crafted backup could carry (config file or config-overrides.json).
# A restore takes none of it.
HOSTILE_CONFIG = {
    "ONIONPRESS_TOR_IMAGE": "evil.example/tor:latest",
    "ONIONPRESS_WORDPRESS_IMAGE": "evil.example/wordpress:latest",
    "ONIONPRESS_BIND_HOST": "0.0.0.0",
    "ONIONPRESS_PORT_OFFSET": "30000",
    "DATA_DIR": "/tmp/evil",
    "REGISTER_WITH_ONIONHEAVEN": "yes",
    "ONIONHEAVEN_ADDRESS": "evil" + "a" * 52 + ".onion",
    "ONIONHEAVEN_ACCEPT_REGISTRATIONS": "yes",
    "ONIONHEAVEN_MAX_SERVICES": "100000",
    "CLOUDFLARE_TUNNEL_TOKEN": "evil-token",
    "SHARE_ANALYTICS_WITH_ONIONHOME": "yes",
    "ONIONHOME_ADDRESS": "evil" + "b" * 52 + ".onion",
    "UPDATE_ON_LAUNCH": "no",
    "ONIONNAME": "evilname",
    "VM_DISK": "500",
    "ADDRESS_PREFIX": "zzz",                    # not the restored address's
    "LAUNCH_ON_LOGIN": "maybe",
    "PREVENT_SLEEP": "never; rm -rf ~",
    "VM_MEMORY": "2\nREGISTER_WITH_ONIONHEAVEN=yes",
    "VM_CPU": "999",
}


def _config_text(values):
    """*values* as a config file (single-line values only)."""
    return "".join(f"{k}={v}\n" for k, v in values.items() if "\n" not in v)


CRAFTED_USERNAMES = [
    "admin\nREGISTER_WITH_ONIONHEAVEN=yes",
    "admin\rCLOUDFLARE_TUNNEL_TOKEN=x",
    "../../../escaped",
    "a/b",
    "admin=1",
    "admin\u202e",
    "x" * 61,
]


def _read(rel):
    with open(os.path.join(PROJECT_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _bash_function(text, name):
    start = text.index(f"\n{name}() {{")
    end = text.index("\n}\n", start)
    return text[start:end + 3]


def _cheap_kdf(test):
    for name, value in (("OPSLIMIT", 1), ("MEMLIMIT", 8192)):
        patcher = mock.patch.object(backup_crypto, name, value)
        patcher.start()
        test.addCleanup(patcher.stop)


def _metadata(**fields):
    meta = {"onion_address": ADDR, "backup_date": "2026-10-01T12:00:00Z",
            "onionpress_version": "2.5.2", "username": "admin"}
    meta.update(fields)
    return meta


def _make_staging(root, metadata, *, pem=(PRIV, PUB), ctor_secret=None,
                  ctor_public=None, config=None, name="staging"):
    staging = os.path.join(root, name)
    tor_dir = os.path.join(staging, "tor-keys")
    os.makedirs(tor_dir)
    with open(os.path.join(staging, "metadata.json"), "w") as f:
        json.dump(metadata, f)
    if pem:
        with open(os.path.join(tor_dir, "ks_hs_id.ed25519_expanded_private"), "wb") as f:
            f.write(key_manager.build_openssh_key(*pem))
    if ctor_secret:
        with open(os.path.join(tor_dir, "hs_ed25519_secret_key"), "wb") as f:
            f.write(key_manager.CTOR_SECRET_HEADER + ctor_secret)
    if ctor_public:
        with open(os.path.join(tor_dir, "hs_ed25519_public_key"), "wb") as f:
            f.write(key_manager.CTOR_PUBLIC_HEADER + ctor_public)
    if config is not None:
        with open(os.path.join(staging, "config"), "w") as f:
            f.write(config)
    return staging


def _make_backup(root, metadata, name="backup.zip", **kw):
    """A format-2 backup file (as anyone can make one) with *metadata*."""
    staging = _make_staging(root, metadata, name=name + ".staging", **kw)
    out = os.path.join(root, name)
    backup.pack_backup(staging, out, PW, lambda m: None)
    shutil.rmtree(staging)
    return out


def _make_format1_backup(root, metadata, name="legacy.zip"):
    staging = _make_staging(root, metadata, name=name + ".staging")
    out = os.path.join(root, name)
    subprocess.run(["zip", "-r", "-P", PW, out, "."], cwd=staging,
                   capture_output=True, check=True)
    shutil.rmtree(staging)
    return out


class TestOnionAddressRule(unittest.TestCase):

    def test_accepts_derived_addresses(self):
        for priv in (PRIV, OLD_PRIV, b"\x11" * 64):
            addr = key_manager.derive_onion_address(key_manager.derive_public_key(priv))
            self.assertTrue(key_manager.is_valid_onion_address(addr), addr)

    def test_rejects_crafted_values(self):
        for value in CRAFTED_ADDRESSES + ["", None, 123, [ADDR], {"a": ADDR}]:
            with self.subTest(value=value):
                self.assertFalse(key_manager.is_valid_onion_address(value))

    def test_bash_rule_is_identical_in_both_launchers(self):
        bodies = [_bash_function(_read(p), "is_onion_address") for p in LAUNCHERS]
        self.assertEqual(bodies[0], bodies[1],
                         "is_onion_address() must be identical in both launchers")

    def test_bash_rule_agrees_with_python(self):
        values = [ADDR, OLD_ADDR] + CRAFTED_ADDRESSES + [""]
        script = (_bash_function(_read("linux/onionpress"), "is_onion_address")
                  + 'for v in "$@"; do if is_onion_address "$v"; then echo 1; '
                    'else echo 0; fi; done\n')
        r = subprocess.run(["bash", "-c", script, "bash", *values],
                           capture_output=True, text=True, timeout=10,
                           env={**os.environ, "LC_ALL": "en_US.UTF-8"})
        self.assertEqual(r.returncode, 0, r.stderr)
        got = r.stdout.split()
        want = ["1" if key_manager.is_valid_onion_address(v) else "0" for v in values]
        self.assertEqual(got, want)
        self.assertEqual(want[:2], ["1", "1"])


class TestKeyConsistency(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_key_pair_address(self):
        self.assertEqual(key_manager.key_pair_onion_address(PRIV, PUB), ADDR)

    def test_a_public_half_that_is_not_the_private_halfs_is_refused(self):
        # The address of the stored public half alone is whatever the key
        # file's author wrote: here, the installed identity's address.
        other_pub = key_manager.derive_public_key(OLD_PRIV)
        with self.assertRaises(ValueError):
            key_manager.key_pair_onion_address(PRIV, other_pub)

    def _tor_dir(self, **kw):
        return os.path.join(_make_staging(self.tmp, {}, **kw), "tor-keys")

    def test_staged_key_address_for_each_format(self):
        cases = {
            "pem": dict(),
            "ctor secret": dict(pem=None, ctor_secret=PRIV),
            "ctor pair": dict(pem=None, ctor_secret=PRIV, ctor_public=PUB),
            "all three": dict(ctor_secret=PRIV, ctor_public=PUB),
        }
        for label, kw in cases.items():
            with self.subTest(label):
                shutil.rmtree(os.path.join(self.tmp, "staging"), True)
                self.assertEqual(key_manager.staged_key_address(self._tor_dir(**kw)), ADDR)

    def test_no_key_is_an_empty_address(self):
        self.assertEqual(key_manager.staged_key_address(self._tor_dir(pem=None)), "")

    def test_disagreeing_or_broken_key_files_are_refused(self):
        old_pub = key_manager.derive_public_key(OLD_PRIV)
        cases = {
            "pem public half": dict(pem=(PRIV, old_pub)),
            "pem vs ctor": dict(ctor_secret=OLD_PRIV),
            "ctor public": dict(pem=None, ctor_secret=PRIV, ctor_public=old_pub),
            "short ctor secret": dict(pem=None, ctor_secret=PRIV[:40]),
        }
        for label, kw in cases.items():
            with self.subTest(label):
                shutil.rmtree(os.path.join(self.tmp, "staging"), True)
                with self.assertRaises(ValueError):
                    key_manager.staged_key_address(self._tor_dir(**kw))

    def test_garbage_pem_is_a_value_error(self):
        tor_dir = self._tor_dir(pem=None)
        with open(os.path.join(tor_dir, "ks_hs_id.ed25519_expanded_private"), "wb") as f:
            f.write(b"-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END\n")
        with self.assertRaises(ValueError):
            key_manager.staged_key_address(tor_dir)


class TestValidateBackupMetadata(unittest.TestCase):

    def test_valid_and_missing_fields_pass(self):
        for meta in (_metadata(), {}, {"onion_address": "", "username": ""},
                     {"onion_address": None, "username": None},
                     _metadata(username="John Doe"), _metadata(username="a.b-c_d@e")):
            with self.subTest(meta=meta):
                self.assertIs(backup.validate_backup_metadata(meta), meta)

    def test_crafted_addresses_are_refused(self):
        for value in CRAFTED_ADDRESSES + [123, [ADDR]]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as ctx:
                    backup.validate_backup_metadata(_metadata(onion_address=value))
                self.assertNotIn("escaped", str(ctx.exception))

    def test_crafted_usernames_are_refused(self):
        for value in CRAFTED_USERNAMES + [["admin"], 7]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as ctx:
                    backup.validate_backup_metadata(_metadata(username=value))
                self.assertNotIn("REGISTER", str(ctx.exception))

    def test_metadata_that_is_not_an_object_is_refused(self):
        for value in ([], "x", 1, None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    backup.validate_backup_metadata(value)

    def test_a_newer_format_still_says_so(self):
        with self.assertRaises(backup_crypto.NewerBackupError):
            backup._check_backup_format({"backup_format": 99})

    def test_metadata_text_for_dialogs(self):
        meta = {"backup_date": "2026-10-01\n\nThis backup is verified \u202e",
                "onionpress_version": "v" * 200, "n": 5}
        self.assertEqual(backup.metadata_text(meta, "backup_date"),
                         "2026-10-01This backup is verified")
        self.assertEqual(len(backup.metadata_text(meta, "onionpress_version")), 65)
        self.assertEqual(backup.metadata_text(meta, "n"), "unknown")
        self.assertEqual(backup.metadata_text(meta, "missing"), "unknown")
        self.assertEqual(backup.metadata_text(None, "x", default="?"), "?")


class _NoDocker:
    """Delegate subprocess.run, failing the test on any docker command."""

    def __init__(self, test):
        self.test = test
        self.real = subprocess.run

    def __call__(self, args, *a, **kw):
        if args and os.path.basename(str(args[0])) == "docker":
            self.test.fail(f"docker called: {args}")
        return self.real(args, *a, **kw)


class TestPythonRestorePaths(unittest.TestCase):
    """read/peek, restore_from_backup and prepare_install_from_backup refuse
    crafted metadata and keys before touching the install."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        _cheap_kdf(self)
        self.data_dir = os.path.join(self.tmp, "home", ".onionpress")
        old_dir = os.path.join(self.data_dir, "shared", "vanity-keys", OLD_ADDR)
        os.makedirs(old_dir)
        with open(os.path.join(old_dir, "hostname"), "w") as f:
            f.write(OLD_ADDR + "\n")
        self.config = "ADDRESS_PREFIX=abc\nONIONNAME=me\n"
        with open(os.path.join(self.data_dir, "config"), "w") as f:
            f.write(self.config)
        with open(os.path.join(self.data_dir, "onion_address"), "w") as f:
            f.write(OLD_ADDR + "\n")
        self.logs = []
        for target in ("onionpress.backup.subprocess.run",):
            p = mock.patch(target, _NoDocker(self))
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(key_manager, "write_private_key",
                              side_effect=AssertionError("key written"))
        p.start()
        self.addCleanup(p.stop)

    def assert_untouched(self):
        vanity = os.path.join(self.data_dir, "shared", "vanity-keys")
        self.assertEqual(os.listdir(vanity), [OLD_ADDR])
        self.assertEqual(sorted(os.listdir(os.path.join(self.data_dir, "shared"))),
                         ["vanity-keys"])
        with open(os.path.join(self.data_dir, "config")) as f:
            self.assertEqual(f.read(), self.config)
        with open(os.path.join(self.data_dir, "onion_address")) as f:
            self.assertEqual(f.read(), OLD_ADDR + "\n")
        self.assertFalse(os.path.exists(os.path.join(self.data_dir, "restore-staging")))
        self.assertFalse(os.path.exists(os.path.join(self.data_dir, ".install-from-backup")))
        for dirpath, _, files in os.walk(self.tmp):
            for name in files + [os.path.basename(dirpath)]:
                self.assertNotIn("escaped", name, dirpath)

    def _crafted_backups(self):
        cases = [(f"onion_address={v!r}", _metadata(onion_address=v), {})
                 for v in CRAFTED_ADDRESSES]
        cases += [(f"username={v!r}", _metadata(username=v), {})
                  for v in CRAFTED_USERNAMES]
        cases.append(("inconsistent key", _metadata(onion_address=OLD_ADDR),
                      {"pem": (PRIV, key_manager.derive_public_key(OLD_PRIV))}))
        for i, (label, meta, kw) in enumerate(cases):
            yield label, _make_backup(self.tmp, meta, name=f"crafted{i}.zip", **kw)

    def test_read_and_peek_refuse_crafted_metadata(self):
        for label, path in list(self._crafted_backups())[:-1]:
            for reader in (backup.read_backup_metadata, backup.peek_backup_metadata):
                with self.subTest(label, reader=reader.__name__):
                    with self.assertRaises(ValueError):
                        reader(path, PW)

    def test_format_1_backups_are_checked_too(self):
        path = _make_format1_backup(self.tmp, _metadata(onion_address="../../escaped"))
        for reader in (backup.read_backup_metadata, backup.peek_backup_metadata):
            with self.subTest(reader.__name__):
                with self.assertRaises(ValueError):
                    reader(path, PW)

    def test_peek_does_not_follow_a_member_name_out_of_its_temp_dir(self):
        # A format-1 zip whose only metadata.json member sits under "../".
        outside = os.path.join(self.tmp, "metadata.json")
        with open(outside, "w") as f:
            json.dump(_metadata(username="from-outside"), f)
        path = os.path.join(self.tmp, "odd.zip")
        import zipfile
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("../../../../../../../../../../../../../../.." + outside,
                        json.dumps(_metadata()))
        meta = backup.peek_backup_metadata(path, PW)
        self.assertEqual(meta["username"], "admin")

    def test_restore_from_backup_refuses_before_touching_anything(self):
        for label, path in self._crafted_backups():
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    backup.restore_from_backup(path, PW, self.logs.append,
                                               data_dir=self.data_dir)
                self.assert_untouched()

    def test_install_from_backup_refuses_before_touching_anything(self):
        for label, path in self._crafted_backups():
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    backup.prepare_install_from_backup(path, PW, self.logs.append,
                                                       data_dir=self.data_dir)
                self.assert_untouched()

    def test_seed_refuses_crafted_metadata_handed_in_directly(self):
        staging = _make_staging(self.tmp, {})
        for meta in ([_metadata(onion_address=v) for v in CRAFTED_ADDRESSES]
                     + [_metadata(username=v) for v in CRAFTED_USERNAMES]):
            with self.subTest(meta=meta):
                with self.assertRaises(ValueError):
                    backup.seed_onion_key_for_install(
                        staging, meta, self.logs.append, data_dir=self.data_dir)
                self.assert_untouched()

    def test_a_valid_backup_still_seeds_the_key_derived_address(self):
        path = _make_backup(self.tmp, _metadata())
        _, meta = backup.prepare_install_from_backup(
            path, PW, self.logs.append, data_dir=self.data_dir)
        self.assertEqual(meta["onion_address"], ADDR)
        self.assertEqual(os.listdir(os.path.join(self.data_dir, "shared", "vanity-keys")),
                         [ADDR])
        with open(os.path.join(self.data_dir, "config")) as f:
            self.assertIn("ONIONNAME=admin\n", f.read())

    def test_config_overrides_take_only_restorable_preferences(self):
        staging = os.path.join(self.tmp, "overrides")
        os.makedirs(staging)
        with open(os.path.join(staging, "config-overrides.json"), "w") as f:
            json.dump(dict(HOSTILE_CONFIG, VM_CPU="4"), f)
        backup.apply_config_overrides(staging, self.logs.append,
                                      data_dir=self.data_dir, onion_address=ADDR)
        with open(os.path.join(self.data_dir, "config")) as f:
            config = f.read()
        self.assertEqual(config, self.config + "VM_CPU=4\n")
        self.assertFalse(any("0.0.0.0" in m or "evil" in m for m in self.logs))


class TestRestoreBackupConfig(unittest.TestCase):
    """backup.restore_backup_config: an allowlist of user preferences."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.staging = os.path.join(self.tmp, "staging")
        os.makedirs(self.staging)
        self.config = os.path.join(self.tmp, "config")
        self.mine = "REGISTER_WITH_ONIONHEAVEN=no\nONIONPRESS_BIND_HOST=127.0.0.1\nVM_CPU=2\n"
        with open(self.config, "w") as f:
            f.write(self.mine)
        self.logs = []

    def _restore(self, file_values=None, overrides=None, address=ADDR):
        if file_values is not None:
            with open(os.path.join(self.staging, "config"), "w") as f:
                f.write(_config_text(file_values))
        if overrides is not None:
            with open(os.path.join(self.staging, "config-overrides.json"), "w") as f:
                json.dump(overrides, f)
        applied = backup.restore_backup_config(self.staging, self.config,
                                               self.logs.append, address)
        from onionpress.config import read_config
        return applied, read_config(self.config)

    def test_hostile_keys_keep_this_installs_values(self):
        applied, config = self._restore(HOSTILE_CONFIG, HOSTILE_CONFIG)
        self.assertEqual(applied, [])
        with open(self.config) as f:
            self.assertEqual(f.read(), self.mine)

    def test_preferences_are_restored(self):
        prefs = {"INSTALL_IA_PLUGIN": "no", "LAUNCH_ON_LOGIN": "no",
                 "PREVENT_SLEEP": "never", "VM_MEMORY": "4", "VM_CPU": "3",
                 "ADDRESS_PREFIX": ADDR[:4]}
        applied, config = self._restore(dict(HOSTILE_CONFIG, **prefs))
        self.assertEqual(sorted(applied), sorted(prefs))
        for key, value in prefs.items():
            self.assertEqual(config[key], value)
        self.assertEqual(config["REGISTER_WITH_ONIONHEAVEN"], "no")
        self.assertEqual(config["ONIONPRESS_BIND_HOST"], "127.0.0.1")
        self.assertNotIn("ONIONPRESS_TOR_IMAGE", config)

    def test_overrides_win_over_the_config_file(self):
        _, config = self._restore({"PREVENT_SLEEP": "never"},
                                  {"PREVENT_SLEEP": "on-battery"})
        self.assertEqual(config["PREVENT_SLEEP"], "on-battery")

    def test_address_prefix_only_for_the_restored_address(self):
        _, config = self._restore({"ADDRESS_PREFIX": ADDR[:3]}, address="")
        self.assertNotIn("ADDRESS_PREFIX", config)
        _, config = self._restore({"ADDRESS_PREFIX": ADDR[:3]}, address=OLD_ADDR)
        if not OLD_ADDR.startswith(ADDR[:3]):
            self.assertNotIn("ADDRESS_PREFIX", config)
        _, config = self._restore({"ADDRESS_PREFIX": ADDR[:6]})
        self.assertNotIn("ADDRESS_PREFIX", config)   # longer than 5

    def test_bad_overrides_json_is_skipped(self):
        with open(os.path.join(self.staging, "config-overrides.json"), "w") as f:
            f.write("[not an object")
        applied, _ = self._restore({"VM_CPU": "4"})
        self.assertEqual(applied, ["VM_CPU"])

    def test_cli_helper(self):
        with open(os.path.join(self.staging, "config"), "w") as f:
            f.write(_config_text(dict(HOSTILE_CONFIG, ADDRESS_PREFIX=ADDR[:3],
                                      VM_MEMORY="3")))
        err = io.StringIO()
        with mock.patch.object(cli.OnionPressCLI, "__init__",
                               side_effect=AssertionError("OnionPressCLI built")), \
             contextlib.redirect_stderr(err):
            rc = cli.main(["restore-config", "--staging", self.staging,
                           "--config", self.config, "--address", ADDR + "\n"])
        self.assertEqual(rc, 0)
        from onionpress.config import read_config
        config = read_config(self.config)
        self.assertEqual(config["VM_MEMORY"], "3")
        # A malformed --address counts as none: no ADDRESS_PREFIX.
        self.assertNotIn("ADDRESS_PREFIX", config)
        self.assertNotIn("evil", err.getvalue())


class TestBackupAddressCli(unittest.TestCase):
    """`onionpress.cli backup-address`, the launchers' gate."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # The helper must not build OnionPressCLI (config, secrets, ports
        # under ~/.onionpress): it runs mid-restore.
        p = mock.patch.object(cli.OnionPressCLI, "__init__",
                              side_effect=AssertionError("OnionPressCLI built"))
        p.start()
        self.addCleanup(p.stop)

    def _run(self, staging):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["backup-address", "--staging", staging])
        return rc, out.getvalue(), err.getvalue()

    def test_prints_the_key_derived_address(self):
        rc, out, _ = self._run(_make_staging(self.tmp, _metadata()))
        self.assertEqual((rc, out), (0, ADDR + "\n"))

    def test_a_stale_but_well_formed_metadata_address_is_logged_not_used(self):
        rc, out, err = self._run(_make_staging(self.tmp, _metadata(onion_address=OLD_ADDR)))
        self.assertEqual((rc, out), (0, ADDR + "\n"))
        self.assertIn("KEY-MISMATCH", err)

    def test_no_key_prints_nothing(self):
        rc, out, _ = self._run(_make_staging(self.tmp, _metadata(), pem=None))
        self.assertEqual((rc, out), (0, ""))

    def test_refuses_crafted_metadata_and_keys(self):
        cases = [_metadata(onion_address=v) for v in CRAFTED_ADDRESSES]
        cases += [_metadata(username=v) for v in CRAFTED_USERNAMES]
        cases += [[], "x"]
        for i, meta in enumerate(cases):
            with self.subTest(meta=meta):
                rc, out, _ = self._run(_make_staging(self.tmp, meta, name=f"s{i}"))
                self.assertEqual((rc, out), (1, ""))
        bad_key = _make_staging(self.tmp, _metadata(), name="badkey",
                                pem=(PRIV, key_manager.derive_public_key(OLD_PRIV)))
        self.assertEqual(self._run(bad_key)[:2], (1, ""))
        no_meta = _make_staging(self.tmp, _metadata(), name="nometa")
        os.remove(os.path.join(no_meta, "metadata.json"))
        self.assertEqual(self._run(no_meta)[:2], (1, ""))


class TestLauncherRestorePaths(unittest.TestCase):
    """`setup --from-backup` of both launchers (the restore's own
    backup.prepare_install_from_backup since #509, through
    cli.cmd_restore), run for real against crafted backup files. Every
    launcher restore is onionpress.cli's: `restore` (install-from-backup)
    and the Settings page's (restore_from_backup, through_wordpress=True;
    Linux's bash restore_backup went with issue #406)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        _cheap_kdf(self)

    def _fresh_data_dir(self):
        data = os.path.join(self.tmp, "home", ".onionpress")
        shutil.rmtree(os.path.join(self.tmp, "home"), True)
        old_dir = os.path.join(data, "shared", "vanity-keys", OLD_ADDR)
        os.makedirs(old_dir)
        with open(os.path.join(old_dir, "hostname"), "w") as f:
            f.write(OLD_ADDR + "\n")
        with open(os.path.join(data, "config"), "w") as f:
            f.write("ONIONNAME=me\n")
        return data

    def _setup_from_backup(self, path):
        """`setup --from-backup`'s host side, as `onionpress setup` runs it
        (#509: cmd_restore's prepare_install_from_backup, before any
        container is touched): (rc, the refusal); the log lines go to the
        log file, a refusal logged as cmd_restore logs it."""
        from onionpress import backup

        def log(msg):
            with open(os.path.join(self.tmp, "log"), "a") as f:
                f.write(msg + "\n")
        data = os.path.join(self.tmp, "home", ".onionpress")
        try:
            backup.prepare_install_from_backup(path, PW, log, data_dir=data)
        except Exception as e:
            log(f"Restore failed during extract/seed: {e}")
            return 1, str(e)
        return 0, ""

    def test_no_launcher_restores_in_bash(self):
        for launcher in LAUNCHERS:
            with self.subTest(launcher):
                code = _read(launcher)
                self.assertNotIn("\nrestore_backup() {", code)
                self.assertNotIn("\ncreate_backup() {", code)

    def _assert_untouched(self, data):
        self.assertEqual(os.listdir(os.path.join(data, "shared", "vanity-keys")), [OLD_ADDR])
        self.assertEqual(sorted(os.listdir(os.path.join(data, "shared"))), ["vanity-keys"])
        with open(os.path.join(data, "config")) as f:
            self.assertEqual(f.read(), "ONIONNAME=me\n")
        self.assertFalse(os.path.exists(os.path.join(data, ".import-key-pending")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "calls")))
        for dirpath, dirs, files in os.walk(self.tmp):
            for name in dirs + files:
                self.assertNotIn("escaped", name, dirpath)

    def _crafted(self):
        evil_config = "REGISTER_WITH_ONIONHEAVEN=yes\n"
        yield "address traversal", _make_backup(
            self.tmp, _metadata(onion_address="../../../../escaped.onion"),
            name="a.zip", config=evil_config)
        yield "address with newline", _make_backup(
            self.tmp, _metadata(onion_address=ADDR + "\n../escaped"),
            name="b.zip", config=evil_config)
        yield "username with newline", _make_backup(
            self.tmp, _metadata(username="admin\nREGISTER_WITH_ONIONHEAVEN=yes"),
            name="c.zip", config=evil_config)
        yield "inconsistent key", _make_backup(
            self.tmp, _metadata(onion_address=OLD_ADDR), name="d.zip",
            config=evil_config, pem=(PRIV, key_manager.derive_public_key(OLD_PRIV)))
        yield "C Tor key for another address", _make_backup(
            self.tmp, _metadata(), name="e.zip", config=evil_config,
            ctor_secret=OLD_PRIV)

    def test_setup_from_backup_refuses_crafted_backups(self):
        backups = list(self._crafted())
        for label, path in backups:
            with self.subTest(case=label):
                    data = self._fresh_data_dir()
                    rc, err = self._setup_from_backup(path)
                    self.assertNotEqual(rc, 0, label)
                    # Refused by the extraction (metadata) or the key
                    # check (staged_onion_key_address / the seed); either
                    # way before anything is copied out of the backup, and
                    # the restore's staging folder is gone with it.
                    self.assertTrue(err, label)
                    self.assertFalse(os.path.exists(os.path.join(data, "restore-staging")))
                    self._assert_untouched(data)

    def test_a_valid_backup_restores_under_the_key_derived_address(self):
        # A stale (but well-formed) metadata address, as older clients wrote,
        # and a config file (Linux backups carry the whole file) with
        # preferences next to keys a restore must not take.
        prefs = {"ADDRESS_PREFIX": ADDR[:3], "PREVENT_SLEEP": "never",
                 "INSTALL_IA_PLUGIN": "no"}
        path = _make_backup(self.tmp, _metadata(onion_address=OLD_ADDR),
                            config=_config_text(dict(HOSTILE_CONFIG, **prefs)))
        from onionpress.config import read_config
        want_config = dict(prefs, ONIONNAME="me")

        data = self._fresh_data_dir()
        self.assertEqual(self._setup_from_backup(path), (0, ""))
        vanity = os.path.join(data, "shared", "vanity-keys")
        self.assertEqual(os.listdir(vanity), [ADDR])
        with open(os.path.join(vanity, ADDR, "hostname")) as f:
            self.assertEqual(f.read(), ADDR + "\n")
        # The restore's prepare records the backup's admin as ONIONNAME.
        self.assertEqual(read_config(os.path.join(data, "config")),
                         dict(want_config, ONIONNAME="admin"))

    def _stage_source(self):
        """`setup --from-backup`'s host side: the restore's
        prepare_install_from_backup (#509)."""
        text = _read("src/onionpress/backup.py")
        i = text.index("def prepare_install_from_backup(")
        return text[i:text.index("\ndef ", i + 10)]

    def test_launchers_never_copy_a_backups_config(self):
        for launcher in LAUNCHERS:
            code = _read(launcher)
            with self.subTest(launcher):
                self.assertNotIn('cp "$staging/config"', code)
                self.assertNotIn("$staging/config", code)
                self.assertNotIn("config-overrides", code)
        stage = self._stage_source()
        self.assertIn("apply_config_overrides(", stage)
        self.assertNotIn("'config'", stage)
        self.assertNotIn('"config"', stage)

    def test_launchers_never_read_the_address_from_metadata(self):
        for launcher in LAUNCHERS:
            code = _read(launcher)
            with self.subTest(launcher):
                self.assertNotIn("get('onion_address'", code)
                self.assertNotIn("metadata.json", code)
        # The restore's prepare: the key is checked and the address derived
        # from it (the seed overwrites metadata's) before the config takes
        # the address; the setup flow itself never reads one.
        stage = self._stage_source()
        self.assertLess(stage.index("staged_onion_key_address("),
                        stage.index("apply_config_overrides("))
        self.assertLess(stage.index("seed_onion_key_for_install("),
                        stage.index("apply_config_overrides("))
        flow = _read("src/onionpress/setup_logic.py")
        flow = flow[flow.index("def setup_from_terminal("):]
        self.assertNotIn("onion_address'", flow)
        self.assertNotIn('onion_address"', flow)

    def test_failed_setup_and_restore_stop_the_launcher(self):
        for launcher in LAUNCHERS:
            code = _read(launcher)
            with self.subTest(launcher):
                self.assertIn('run_setup "$@" || setup_rc=$?', code)
                self.assertIn("                *) exit 1 ;;\n            esac", code)
                self.assertRegex(
                    code, r'if ! (?:ONIONPRESS_HUB_RECOVERY_KEY="\$_HUB_RECOVERY_KEY" )?'
                          r'ONIONPRESS_BACKUP_PASSWORD="\$BACKUP_PASS" '
                          r'(?:op_py|bundled_python -m onionpress\.cli) --data-dir "\$DATA_DIR" restore '
                          r'"\$restore_path" "\$\{restore_flags\[@\]\}"; then\n(?:[^\n]*\n){1,3}?\s*exit 1')


class TestRestoreResultJson(unittest.TestCase):
    """The page's result files are built by the shared handler with a JSON
    encoder (onionpress.handle_action Host.write_result), never by string
    pasting; the launcher's string-built results are gone."""

    def test_no_string_built_result_json_remains(self):
        code = _read("linux/onionpress")
        self.assertNotIn('"address":"\'"$rp_addr"', code)
        self.assertNotIn("restore-result.json", code)
        self.assertNotIn("write_restore_result", code)
        self.assertNotIn("write_key_result", code)
        handler = _read("src/onionpress/handle_action.py")
        self.assertIn("self.put_shared(filename, json.dumps(data))", handler)
        self.assertNotIn("'{\"success\"", handler)

    def test_hostile_text_stays_a_string(self):
        import test_handle_action as tha
        from onionpress import handle_action as ha
        from onionpress.platform import resolve_paths
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        docker = tha.FakeDocker(os.path.join(tmp, "sandbox"))
        host = ha.Host(launcher="/nonexistent", paths=resolve_paths(data_dir=tmp, app_bundle=""),
                       docker=docker, log=lambda m: None, os_name="linux")
        hostile = 'x","success":false,"error":"<b>pwned</b>\n'
        host.write_result("restore-result.json", {"success": True, "address": hostile,
                                                  "restored_at": "now"})
        doc = docker.result("restore-result.json")
        self.assertEqual(doc["success"], True)
        self.assertEqual(doc["address"], hostile)
        self.assertEqual(sorted(doc), ["address", "restored_at", "success"])


class TestMenubarRestoreDialog(unittest.TestCase):
    """src/menubar.py's Restore... (AppKit, so checked as source)."""

    def setUp(self):
        src = _read("src/menubar.py")
        start = src.index("    def restore(self, _):")
        self.body = src[start:src.index("\n    @rumps.clicked", start + 1)]

    def test_shown_fields_are_sanitized(self):
        # The facts come from `onionpress restore --check --json`
        # (cli.cmd_restore_check), which sanitizes them.
        cli_src = _read("src/onionpress/cli.py")
        check = cli_src[cli_src.index("    def cmd_restore_check("):cli_src.index("    def cmd_restore(")]
        for field in ("backup_date", "username", "onionpress_version"):
            self.assertIn(f'metadata_text(metadata, "{field}")', check)
            self.assertIn(f"facts.get('{field}')", self.body)
        self.assertIn('["restore", zip_path, "--check", "--json"]', self.body)

    def test_a_failed_launcher_restore_is_reported_as_a_failure(self):
        # run_launcher_restore's message starts "Restore failed" and says
        # why (tests/test_restore_failure_details.py).
        failed = self.body[self.body.index("if not ok:"):]
        failed = failed[:failed.index("return\n") + 7]
        self.assertIn('title="Restore Failed"', failed)
        self.assertIn("pw.finish(failure,", failed)

    def test_the_restored_address_is_not_taken_from_metadata(self):
        self.assertNotIn("metadata.get('onion_address', addr)", self.body)
        self.assertIn("key_manager.is_valid_onion_address(restored_addr)", self.body)


if __name__ == "__main__":
    unittest.main()

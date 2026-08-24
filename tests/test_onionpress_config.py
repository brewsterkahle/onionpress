"""Tests for src/onionpress/config.py."""

import os
import socket
import stat
import sys
import tempfile
import shutil
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from onionpress.config import (
    DEFAULTS,
    read_config, read_value, write_value, write_config,
    validate_address_prefix,
    Secrets, load_secrets, ensure_secrets,
    PortConfig, detect_port_offset, resolve_port_offset,
    ensure_config,
    SAFE_CONFIG_KEYS, redact_config,
)
from onionpress.platform import OnionPressPaths, resolve_paths


class TestRedactConfig(unittest.TestCase):
    """redact_config is the trust boundary for config leaving the machine
    (status.json into the WordPress container + the OnionHome upload). It is
    an allowlist, so secrets/new keys are withheld by default.
    """

    def test_secret_keys_are_withheld(self):
        cfg = {
            "TOR_IMPL": "tor",
            "CLOUDFLARE_TUNNEL_TOKEN": "super-secret-token",
            "ADDRESS_PREFIX": "op2",
        }
        out = redact_config(cfg)
        self.assertNotIn("CLOUDFLARE_TUNNEL_TOKEN", out)
        self.assertEqual(out.get("TOR_IMPL"), "tor")
        self.assertEqual(out.get("ADDRESS_PREFIX"), "op2")

    def test_cloudflare_token_not_in_allowlist(self):
        # The one secret that actually lives in config must never be allowed.
        self.assertNotIn("CLOUDFLARE_TUNNEL_TOKEN", SAFE_CONFIG_KEYS)

    def test_unknown_key_is_withheld_by_default(self):
        # Allowlist semantics: a brand-new key (which could be a future
        # credential) is dropped unless explicitly added to SAFE_CONFIG_KEYS.
        out = redact_config({"SOME_FUTURE_TOKEN": "x", "TOR_IMPL": "tor"})
        self.assertEqual(out, {"TOR_IMPL": "tor"})

    def test_allowlist_has_no_secret_named_keys(self):
        for key in SAFE_CONFIG_KEYS:
            for bad in ("TOKEN", "SECRET", "PASSWORD"):
                self.assertNotIn(
                    bad, key.upper(),
                    f"SAFE_CONFIG_KEYS contains a credential-looking key: {key}",
                )


class TestReadConfig(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.config_file = os.path.join(self.tmpdir, "config")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_read_simple(self):
        with open(self.config_file, "w") as f:
            f.write("ADDRESS_PREFIX=op2\nVM_MEMORY=2\n")
        result = read_config(self.config_file)
        self.assertEqual(result["ADDRESS_PREFIX"], "op2")
        self.assertEqual(result["VM_MEMORY"], "2")

    def test_skip_comments(self):
        with open(self.config_file, "w") as f:
            f.write("# comment\nKEY=value\n\n# another\n")
        result = read_config(self.config_file)
        self.assertEqual(result, {"KEY": "value"})

    def test_missing_file(self):
        result = read_config("/nonexistent/config")
        self.assertEqual(result, {})

    def test_value_with_equals(self):
        with open(self.config_file, "w") as f:
            f.write("TOKEN=abc=def=ghi\n")
        result = read_config(self.config_file)
        self.assertEqual(result["TOKEN"], "abc=def=ghi")


class TestReadValue(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.config_file = os.path.join(self.tmpdir, "config")
        with open(self.config_file, "w") as f:
            f.write("ADDRESS_PREFIX=op2\nVM_MEMORY=2\n")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_existing_key(self):
        self.assertEqual(read_value(self.config_file, "ADDRESS_PREFIX"), "op2")

    def test_missing_key(self):
        self.assertEqual(read_value(self.config_file, "MISSING", "default"), "default")

    def test_missing_file(self):
        self.assertEqual(read_value("/nonexistent", "KEY", "fallback"), "fallback")


class TestWriteValue(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.config_file = os.path.join(self.tmpdir, "config")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_update_existing(self):
        with open(self.config_file, "w") as f:
            f.write("KEY=old\n")
        write_value(self.config_file, "KEY", "new")
        self.assertEqual(read_value(self.config_file, "KEY"), "new")

    def test_append_new(self):
        with open(self.config_file, "w") as f:
            f.write("KEY1=val1\n")
        write_value(self.config_file, "KEY2", "val2")
        self.assertEqual(read_value(self.config_file, "KEY1"), "val1")
        self.assertEqual(read_value(self.config_file, "KEY2"), "val2")

    def test_write_to_new_file(self):
        write_value(self.config_file, "NEW_KEY", "new_val")
        self.assertEqual(read_value(self.config_file, "NEW_KEY"), "new_val")

    def test_preserves_comments(self):
        with open(self.config_file, "w") as f:
            f.write("# header\nKEY=old\n# footer\n")
        write_value(self.config_file, "KEY", "new")
        with open(self.config_file) as f:
            content = f.read()
        self.assertIn("# header", content)
        self.assertIn("# footer", content)
        self.assertIn("KEY=new", content)


class TestWriteConfig(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.config_file = os.path.join(self.tmpdir, "config")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_write_multiple(self):
        write_config(self.config_file, {"A": "1", "B": "2"})
        self.assertEqual(read_value(self.config_file, "A"), "1")
        self.assertEqual(read_value(self.config_file, "B"), "2")


class TestValidateAddressPrefix(unittest.TestCase):
    def test_valid_prefix(self):
        valid, err, suggestion = validate_address_prefix("op2")
        self.assertTrue(valid)
        self.assertEqual(err, "")
        self.assertEqual(suggestion, "op2")

    def test_empty_prefix(self):
        valid, err, suggestion = validate_address_prefix("")
        self.assertTrue(valid)

    def test_too_long(self):
        valid, err, suggestion = validate_address_prefix("abcdef")
        self.assertFalse(valid)
        self.assertIn("too long", err)
        self.assertEqual(suggestion, "abcde")

    def test_invalid_chars(self):
        valid, err, suggestion = validate_address_prefix("Op1")
        self.assertFalse(valid)
        self.assertIn("invalid", err.lower())
        # suggestion strips invalid chars (1) and lowercases (O→o, p stays)
        self.assertEqual(suggestion, "op")

    def test_invalid_digits(self):
        valid, err, suggestion = validate_address_prefix("test0189")
        self.assertFalse(valid)
        self.assertIn("0", err)
        self.assertEqual(suggestion, "test")

    def test_base32_chars_only(self):
        valid, _, _ = validate_address_prefix("ab2cd")
        self.assertTrue(valid)

    def test_uppercase_suggestion(self):
        valid, err, suggestion = validate_address_prefix("OP2")
        self.assertFalse(valid)
        self.assertIn("Uppercase", err)
        self.assertEqual(suggestion, "op2")


class TestSecrets(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.secrets_file = os.path.join(self.tmpdir, "secrets")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_load_secrets_single_quoted(self):
        with open(self.secrets_file, "w") as f:
            f.write("# comment\n")
            f.write("WORDPRESS_DB_PASSWORD='mypass123'\n")
            f.write("MYSQL_PASSWORD='mypass123'\n")
            f.write("MYSQL_ROOT_PASSWORD='rootpass456'\n")
        s = load_secrets(self.secrets_file)
        self.assertEqual(s.wordpress_db_password, "mypass123")
        self.assertEqual(s.mysql_password, "mypass123")
        self.assertEqual(s.mysql_root_password, "rootpass456")

    def test_load_secrets_bare_values(self):
        with open(self.secrets_file, "w") as f:
            f.write("WORDPRESS_DB_PASSWORD=barepass\n")
            f.write("MYSQL_PASSWORD=barepass\n")
            f.write("MYSQL_ROOT_PASSWORD=rootbare\n")
        s = load_secrets(self.secrets_file)
        self.assertEqual(s.wordpress_db_password, "barepass")

    def test_as_env(self):
        s = Secrets(
            wordpress_db_password="wp",
            mysql_password="mysql",
            mysql_root_password="root",
        )
        env = s.as_env()
        self.assertEqual(env["WORDPRESS_DB_PASSWORD"], "wp")
        self.assertEqual(env["MYSQL_PASSWORD"], "mysql")
        self.assertEqual(env["MYSQL_ROOT_PASSWORD"], "root")

    def test_ensure_secrets_creates_file(self):
        s = ensure_secrets(self.secrets_file)
        self.assertTrue(os.path.exists(self.secrets_file))
        self.assertTrue(len(s.wordpress_db_password) == 32)
        self.assertTrue(len(s.mysql_root_password) == 32)
        # wp and mysql passwords should match (same value)
        self.assertEqual(s.wordpress_db_password, s.mysql_password)

    def test_ensure_secrets_permissions(self):
        ensure_secrets(self.secrets_file)
        mode = stat.S_IMODE(os.stat(self.secrets_file).st_mode)
        self.assertEqual(mode, 0o600)

    def test_ensure_secrets_idempotent(self):
        s1 = ensure_secrets(self.secrets_file)
        s2 = ensure_secrets(self.secrets_file)
        self.assertEqual(s1.wordpress_db_password, s2.wordpress_db_password)
        self.assertEqual(s1.mysql_root_password, s2.mysql_root_password)

    def test_ensure_secrets_unique_passwords(self):
        s = ensure_secrets(self.secrets_file)
        # WP and root passwords should be different
        self.assertNotEqual(s.wordpress_db_password, s.mysql_root_password)


class TestEnsureConfig(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_creates_default_config(self):
        paths = resolve_paths(data_dir=self.tmpdir, app_bundle=None)
        # Ensure parent dir exists
        os.makedirs(os.path.dirname(paths.config_file), exist_ok=True)
        ensure_config(paths)
        self.assertTrue(os.path.exists(paths.config_file))
        config = read_config(paths.config_file)
        self.assertEqual(config["ADDRESS_PREFIX"], "op2")

    def test_does_not_overwrite(self):
        paths = resolve_paths(data_dir=self.tmpdir, app_bundle=None)
        os.makedirs(os.path.dirname(paths.config_file), exist_ok=True)
        with open(paths.config_file, "w") as f:
            f.write("ADDRESS_PREFIX=custom\n")
        ensure_config(paths)
        config = read_config(paths.config_file)
        self.assertEqual(config["ADDRESS_PREFIX"], "custom")


class TestPortDetection(unittest.TestCase):
    def test_detect_default_offset(self):
        """Port detection should return a valid PortConfig."""
        pc = detect_port_offset()
        self.assertIsInstance(pc, PortConfig)
        self.assertEqual(pc.wp_port, 8080 + pc.offset)
        self.assertEqual(pc.socks_port, 9050 + pc.offset)
        self.assertEqual(pc.proxy_port, 9077 + pc.offset)
        self.assertTrue(pc.offset >= 0)
        self.assertTrue(pc.offset % 10000 == 0)

    def test_port_config_values(self):
        pc = PortConfig(offset=10000, wp_port=18080, socks_port=19050, proxy_port=19077)
        self.assertEqual(pc.offset, 10000)
        self.assertEqual(pc.wp_port, 18080)

    def test_foreign_holder_still_bumps_offset(self):
        """detect_port_offset() is a bind-based allocator: it can't tell
        "someone else holds this port" from "we do" — it only knows
        "taken". That's correct for the multi-user path (another macOS
        account legitimately holds 8080) and must keep bumping to +10000
        in that case; resolve_port_offset() (tested below) is what adds
        the "is it actually us" distinction on top."""
        real_socket_cls = socket.socket

        class FakeSocket(real_socket_cls):
            def bind(self, addr):
                if addr[1] == 8080:
                    raise OSError("address in use (simulated foreign holder)")
                return super().bind(addr)

        with mock.patch("onionpress.config.socket.socket", FakeSocket):
            pc = detect_port_offset()
        self.assertEqual(pc.offset, 10000)
        self.assertEqual(pc.wp_port, 18080)


class TestResolvePortOffset(unittest.TestCase):
    """resolve_port_offset() is the runtime lookup detect_port_offset()
    can't be: it reads our own running container's published port first
    (authoritative) and only falls back to bind-probe allocation when
    nothing of ours is up yet.
    """

    def test_reads_running_stack_port_without_bumping(self):
        with mock.patch("onionpress.config.launcher_ops.get_running_wp_port",
                         return_value=8080):
            pc = resolve_port_offset()
        self.assertEqual(pc, PortConfig(offset=0, wp_port=8080, socks_port=9050, proxy_port=9077))

    def test_reads_running_stack_on_nonzero_offset(self):
        # A stack that came up on a non-default offset (multi-user, or a
        # prior restart that landed elsewhere) must be followed, not
        # silently mis-addressed for the rest of the session.
        with mock.patch("onionpress.config.launcher_ops.get_running_wp_port",
                         return_value=18080):
            pc = resolve_port_offset()
        self.assertEqual(pc, PortConfig(offset=10000, wp_port=18080, socks_port=19050, proxy_port=19077))

    def test_falls_back_to_detect_when_nothing_running(self):
        sentinel = PortConfig(offset=20000, wp_port=28080, socks_port=29050, proxy_port=29077)
        with mock.patch("onionpress.config.launcher_ops.get_running_wp_port",
                         return_value=None), \
             mock.patch("onionpress.config.detect_port_offset",
                         return_value=sentinel) as m:
            pc = resolve_port_offset()
        m.assert_called_once()
        self.assertEqual(pc, sentinel)


class TestDefaults(unittest.TestCase):
    def test_has_expected_keys(self):
        self.assertIn("ADDRESS_PREFIX", DEFAULTS)
        self.assertIn("TOR_IMPL", DEFAULTS)
        self.assertIn("VM_MEMORY", DEFAULTS)
        self.assertEqual(DEFAULTS["ADDRESS_PREFIX"], "op2")


if __name__ == "__main__":
    unittest.main()

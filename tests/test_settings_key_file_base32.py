#!/usr/bin/env python3
"""The Settings page's key-file import encodes in PHP (issue #401).

The import-key-file branch used to base32-encode the uploaded key with
`shell_exec(... | python3 -c ...)`, and the WordPress image installs no
python3: the branch always ended in "Failed to encode key file".
`onionpress_base32_encode` does it in PHP, and no plugin shells out.

The encoder is run under php (where installed) against Python's
base64.b32encode, which is what the host's import decodes.
"""

import base64
import os
import re
import shutil
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGINS = os.path.join(REPO, "app", "Resources", "plugins")
WP_IMAGE = os.path.join(REPO, "app", "Resources", "docker", "wordpress")
SETTINGS = os.path.join(PLUGINS, "onionpress-settings.php")

# Raw text, not tokens: the plugins mix PHP with HTML and inline script,
# and none of these names appears there as anything but a call.
SHELL_OUT = re.compile(r"\b(shell_exec|exec|system|passthru|proc_open|popen|pcntl_exec)\s*\(")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class TestNoShellOut(unittest.TestCase):

    def test_no_plugin_runs_a_shell_command(self):
        found = []
        for root, _dirs, files in (w for top in (PLUGINS, WP_IMAGE) for w in os.walk(top)):
            for name in files:
                if not name.endswith(".php"):
                    continue
                path = os.path.join(root, name)
                for n, line in enumerate(_read(path).splitlines(), 1):
                    if SHELL_OUT.search(line):
                        found.append(f"{os.path.relpath(path, REPO)}:{n}: {line.strip()}")
        self.assertEqual(found, [])

    def test_the_census_sees_the_old_call(self):
        old = '$k = trim( shell_exec( "echo x | python3 -c ..." ) );'
        self.assertTrue(SHELL_OUT.search(old))
        self.assertFalse(SHELL_OUT.search("$body = curl_exec( $ch );"))

    def test_the_key_file_branch_uses_the_php_encoder(self):
        text = _read(SETTINGS)
        branch = text[text.index("$action === 'import-key-file'"):]
        branch = branch[:branch.index("$cmd_map")]
        self.assertIn("onionpress_base32_encode( $raw )", branch)
        self.assertNotIn("python3", branch)


def _function(text, name):
    start = text.index(f"function {name}(")
    depth = 0
    for i in range(text.index("{", start), len(text)):
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        if depth == 0:
            return text[start:i + 1] + "\n"
    raise AssertionError(f"{name} not closed")


@unittest.skipUnless(shutil.which("php"), "php not installed")
class TestEncoderUnderPhp(unittest.TestCase):

    def test_matches_python_b32encode(self):
        samples = [b"", b"f", b"fo", b"foo", b"foob", b"fooba", b"foobar",
                   bytes(range(256)), os.urandom(64), os.urandom(96), os.urandom(97)]
        script = ("<?php\n" + _function(_read(SETTINGS), "onionpress_base32_encode")
                  + "while (($line = fgets(STDIN)) !== false) {\n"
                  + "    echo onionpress_base32_encode(hex2bin(trim($line))), \"\\n\";\n}\n")
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "b32.php")
            with open(path, "w", encoding="utf-8") as f:
                f.write(script)
            proc = subprocess.run(["php", path], input="".join(s.hex() + "\n" for s in samples),
                                  text=True, capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        got = proc.stdout.split("\n")[:len(samples)]
        self.assertEqual(got, [base64.b32encode(s).decode() for s in samples])
        for s, encoded in zip(samples, got):
            self.assertEqual(base64.b32decode(encoded, casefold=True), s)


if __name__ == "__main__":
    unittest.main()

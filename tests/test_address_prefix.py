#!/usr/bin/env python3
"""ADDRESS_PREFIX is validated by one set of rules, everywhere.

There are five places a prefix can enter or be checked, and they used to
disagree:

  config.validate_address_prefix()   base32, max 5, NO minimum
  app/MacOS/onionpress               base32, max 5
  src/onionpress/cli.py              2-6 chars, NO character check
  launcher_ops.generate_vanity_...   2-6 chars, NO character check
  setup_window.py                    NO validation at all

Two real consequences:

  * The same ~/.onionpress/config behaved differently per platform. A
    6-character prefix like "elphin" was accepted on Linux and generated a
    6-character address, while macOS silently fell back to "op2" and logged
    it — so the user got an address they never chose, evidenced only in
    ~/.onionpress/launcher.log.
  * Neither Linux path checked the character set. A prefix containing 0, 1,
    8 or 9 is not base32, so no onion address can ever start with it;
    mkp224o was handed it anyway and searched forever.

And the good validator — the one with helpful messages and a suggested
correction — was called by nothing but its own tests.
"""

import os
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from onionpress.config import (  # noqa: E402
    ADDRESS_PREFIX_MAX,
    ADDRESS_PREFIX_MIN,
    validate_address_prefix,
)

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _read(rel_path):
    with open(os.path.join(PROJECT_ROOT, rel_path), "r", encoding="utf-8") as f:
        return f.read()


def _code(rel_path):
    """Python source with comments removed, via tokenize.

    These modules document the rules they replaced, so the prose contains the
    exact expressions ("2 <= len(prefix) <= 6", "[a-z2-7]") that the checks
    below scan for. A raw text scan matches the explanation instead of the
    code and fails on a correct file. tokenize rather than a startswith("#")
    filter so a trailing comment on a line of code is stripped too, while
    string literals are preserved.
    """
    import io
    import tokenize
    source = _read(rel_path)
    out = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type != tokenize.COMMENT:
                out.append(token.string)
    except (tokenize.TokenError, IndentationError):
        return source  # fall back rather than silently passing
    # Joined with a space, not a newline: tokenizing splits `len(prefix)` into
    # separate tokens, and a newline between them defeats any regex that
    # expects them adjacent — which made this check pass vacuously once
    # already. The patterns below allow \s* between tokens to suit.
    return " ".join(out)


class TestCanonicalRules(unittest.TestCase):
    """The behaviour every entry point now inherits."""

    def test_too_long_is_rejected_with_a_truncated_suggestion(self):
        ok, error, suggestion = validate_address_prefix("elphin")
        self.assertFalse(ok)
        self.assertIn("too long", error)
        self.assertEqual("elphi", suggestion)

    def test_too_short_is_rejected(self):
        """Linux enforced a 2-character minimum and the canonical validator
        did not, so a 1-character prefix passed validation and was then
        rejected deeper in, by a ValueError from a library function.
        """
        ok, error, _ = validate_address_prefix("a")
        self.assertFalse(ok)
        self.assertIn("too short", error)

    def test_non_base32_digits_are_rejected(self):
        """0, 1, 8 and 9 are not in the base32 alphabet, so no address can
        begin with them. Passed to mkp224o this is an infinite search.
        """
        for prefix in ("op0", "op1", "op8", "op9"):
            with self.subTest(prefix=prefix):
                ok, error, _ = validate_address_prefix(prefix)
                self.assertFalse(ok, f"{prefix!r} must be rejected")
                self.assertIn("invalid characters", error)

    def test_empty_means_use_the_default(self):
        ok, _, _ = validate_address_prefix("")
        self.assertTrue(ok)

    def test_valid_prefixes_pass(self):
        for prefix in ("op", "op2", "ab2cd"):
            with self.subTest(prefix=prefix):
                ok, error, _ = validate_address_prefix(prefix)
                self.assertTrue(ok, f"{prefix!r} should be valid: {error}")

    def test_bounds_are_named_constants(self):
        """So the shell launchers have something to be checked against."""
        self.assertEqual(2, ADDRESS_PREFIX_MIN)
        self.assertEqual(5, ADDRESS_PREFIX_MAX)


class TestEmptyPrefix(unittest.TestCase):
    """An empty prefix is valid to the validator, because to UI callers it
    means "use the default". The two library-side callers must not take that
    literally — and before this branch they did not, by accident: a bare
    `2 <= len(prefix) <= 6` happened to reject "". Switching them to the
    validator dropped that guard, so `ADDRESS_PREFIX=` (present but empty)
    reached mkp224o as an empty filter. mkp224o then reports "0 filters",
    exits 0 having generated nothing, and the `startswith(prefix)` scan of the
    output directory matches every pre-existing key — an OLD address returned
    as if freshly minted.
    """

    def test_library_boundary_rejects_empty(self):
        from onionpress.launcher_ops import generate_vanity_in_container
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError) as ctx:
                generate_vanity_in_container("", tmp)
        self.assertIn("empty", str(ctx.exception))

    def test_cli_substitutes_the_default_for_a_present_but_empty_key(self):
        """read_value() returns its default only when the key is ABSENT; a
        bare `ADDRESS_PREFIX=` yields "". The CLI must give that the default
        it stands for rather than pass it on.
        """
        from onionpress.cli import OnionPressCLI
        seen = {}

        def fake_generate(prefix, vanity_dir, **kwargs):
            seen["prefix"] = prefix
            return None  # "nothing generated" — keeps the test off the key path

        with tempfile.TemporaryDirectory() as tmp:
            cli = OnionPressCLI(data_dir=tmp)
            with open(cli.paths.config_file, "w") as f:
                f.write("ADDRESS_PREFIX=\n")
            with mock.patch("onionpress.launcher_ops.tor_image_has_mkp224o", return_value=True), \
                 mock.patch("onionpress.launcher_ops.generate_vanity_in_container", fake_generate):
                rc = cli.cmd_generate_vanity()
        self.assertEqual(1, rc)  # generation reported nothing, as the fake said
        self.assertEqual("op2", seen.get("prefix"),
                         "an empty ADDRESS_PREFIX must become the default, "
                         f"not reach mkp224o as {seen.get('prefix')!r}")


class TestSetupWindowHintFits(unittest.TestCase):
    """The inline hint is one 14pt line ~260px wide (~45 characters). The
    validator's first line alone is ~90, so appending the suggestion to it
    put the suggestion exactly where the clipping happened.
    """

    def test_uses_compact_headlines_not_the_dialog_message(self):
        body = _read("src/onionpress/setup_window.py")
        self.assertNotIn(
            "message.strip().splitlines()[0]", body,
            "setup_window must not show the validator's dialog-length first "
            "line in the inline hint — it is clipped.",
        )
        for fragment in ("Too long (", "Too short (min", "Use only a-z and 2-7."):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, body)

    def test_worst_case_headline_fits(self):
        # Longest realistic headline: a too-long prefix with a full-length
        # suggestion appended.
        headline = f'Too long ({len("abcdefghij")} chars, max 5). Try "abcde".'
        self.assertLessEqual(len(headline), 48, headline)


class TestEveryEntryPointUsesTheValidator(unittest.TestCase):
    """A local length check or regex is how the rules drifted apart before."""

    CALLERS = (
        "src/onionpress/cli.py",
        "src/onionpress/launcher_ops.py",
        "src/onionpress/settings_ui.py",
        "src/onionpress/setup_window.py",
    )

    def test_each_caller_calls_the_canonical_validator(self):
        for path in self.CALLERS:
            with self.subTest(path=path):
                self.assertIn(
                    "validate_address_prefix", _read(path),
                    f"{path} handles ADDRESS_PREFIX but does not call "
                    "config.validate_address_prefix().",
                )

    def test_no_caller_reimplements_the_length_rule(self):
        """`2 <= len(prefix) <= 6` in cli.py and launcher_ops.py is exactly
        what disagreed with the macOS launcher's max of 5.
        """
        for path in self.CALLERS:
            with self.subTest(path=path):
                self.assertIsNone(
                    re.search(r"len\s*\(\s*prefix\s*\)\s*(<=|<|>|>=)\s*\d", _code(path)),
                    f"{path} compares len(prefix) against a literal instead of "
                    "using config.validate_address_prefix().",
                )

    def test_no_caller_reimplements_the_character_rule(self):
        for path in self.CALLERS:
            body = _code(path)
            with self.subTest(path=path):
                # The canonical module is allowed to contain the pattern.
                self.assertNotIn(
                    "[a-z2-7]", body,
                    f"{path} hard-codes the base32 character class instead of "
                    "using config.validate_address_prefix().",
                )

    def test_setup_window_blocks_submission(self):
        """It used to accept anything and rely on the launcher, which can only
        fall back to op2 and log it. That is where "elphin" got in.
        """
        body = _read("src/onionpress/setup_window.py")
        self.assertIn(
            "prefix_invalid", body,
            "setup_window must refuse to proceed on an invalid prefix.",
        )
        self.assertRegex(
            body, r"missing = prefix_invalid",
            "The prefix result must feed the same gate as the other required "
            "fields, or the window would validate and then submit anyway.",
        )

    def test_menubar_has_no_rival_wrapper(self):
        """src/menubar.py defined validate_address_prefix() that nothing
        called — a second name for the same thing is how you end up unsure
        which one is real.
        """
        self.assertNotIn(
            "def validate_address_prefix", _read("src/menubar.py"),
            "src/menubar.py should not define its own prefix validator.",
        )


class TestLaunchersMatchThePythonRules(unittest.TestCase):
    """The macOS launcher validates in bash, before any Python runs, so it
    cannot call the validator. It must still agree with it.
    """

    LAUNCHER = "app/MacOS/onionpress"

    def test_max_length_matches(self):
        body = _read(self.LAUNCHER)
        self.assertIn(
            f"-gt {ADDRESS_PREFIX_MAX} ", body,
            f"{self.LAUNCHER} must reject prefixes longer than "
            f"{ADDRESS_PREFIX_MAX}, matching config.ADDRESS_PREFIX_MAX.",
        )

    def test_min_length_matches(self):
        body = _read(self.LAUNCHER)
        self.assertIn(
            f"-lt {ADDRESS_PREFIX_MIN} ", body,
            f"{self.LAUNCHER} must reject prefixes shorter than "
            f"{ADDRESS_PREFIX_MIN}, matching config.ADDRESS_PREFIX_MIN.",
        )

    def test_character_class_matches(self):
        body = _read(self.LAUNCHER)
        self.assertIn(
            "'^[a-z2-7]+$'", body,
            f"{self.LAUNCHER}'s character class must match "
            "config.ADDRESS_PREFIX_CHARS.",
        )

    def test_validates_before_announcing(self):
        """The old order logged "Using address prefix: 'elphin'" and only then
        the rejection, so the log's first claim about the prefix was the one
        that did not happen.
        """
        body = _read(self.LAUNCHER)
        announce = body.index('log "Using address prefix:')
        validate = body.index("_prefix_problem=")
        self.assertLess(
            validate, announce,
            "The macOS launcher must validate the prefix before logging which "
            "one it is using.",
        )

    def test_tells_the_user_how_to_fix_it(self):
        body = _read(self.LAUNCHER)
        self.assertIn(
            "_prefix_suggestion", body,
            "On a bad prefix the launcher should offer the closest valid one "
            "— it is the only feedback a user gets for a hand-edited config.",
        )


class TestLauncherSuggestionMatchesPython(unittest.TestCase):
    """The launcher derives its suggestion in shell. It must produce the same
    string the Python validator would, or the two would advise differently.
    """

    def _shell_suggestion(self, prefix):
        import subprocess
        out = subprocess.run(
            ["sh", "-c",
             "echo \"$1\" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z2-7' | cut -c1-5",
             "sh", prefix],
            capture_output=True, text=True, timeout=30,
        )
        return out.stdout.strip()

    def test_agrees_with_the_validator(self):
        for prefix in ("elphin", "Op1", "test0189", "op0", "ABCDEFGH"):
            with self.subTest(prefix=prefix):
                _, _, python_suggestion = validate_address_prefix(prefix)
                self.assertEqual(
                    python_suggestion, self._shell_suggestion(prefix),
                    "The launcher's shell suggestion and "
                    "config.validate_address_prefix() must agree.",
                )


if __name__ == "__main__":
    unittest.main()

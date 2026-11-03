#!/usr/bin/env python3
"""The launchers' help is one text, cli.launcher_help (issue #529): the
Mac's heredoc and Linux's one-line usage, with each platform's own lines,
printed by `onionpress launcher-help` from both launchers' help case."""

import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

from onionpress import cli  # noqa: E402

ROOT = os.path.dirname(HERE)
COMMON = ("start", "stop", "daemon", "restart", "status", "version", "address", "open", "logs",
          "check-reachability", "check-docker", "get", "update", "backup", "restore", "setup",
          "import-key", "onionname", "config", "onionheaven", "hub-recovery-key", "uninstall",
          "reset")


def _verbs(text):
    return re.findall(r"(?m)^  ([a-z][a-z-]*)", text.split("\nDestructive commands")[0])


class TestText(unittest.TestCase):

    def test_every_user_command_is_listed_on_both_platforms(self):
        for os_name in ("macos", "linux"):
            verbs = _verbs(cli.launcher_help(os_name))
            for verb in COMMON:
                self.assertIn(verb, verbs, f"{os_name}: {verb}")

    def test_the_platforms_own_lines(self):
        mac, linux = cli.launcher_help("macos"), cli.launcher_help("linux")
        self.assertIn("quit", _verbs(mac))
        self.assertNotIn("quit", _verbs(linux))
        for verb in ("provision", "dashboard", "admin-password", "generate-vanity", "reset-onionheaven"):
            self.assertIn(verb, _verbs(linux), verb)
            self.assertNotIn(verb, _verbs(mac), verb)
        self.assertIn("a launchd agent", mac)
        self.assertIn("a systemd user unit", linux)
        self.assertIn("move\n                OnionPress.app to the Trash", mac)
        self.assertIn("/opt/onionpress (sudo)", linux)
        self.assertIn("  start-tor, launch,", mac)
        self.assertNotIn("start-tor", linux)

    def test_no_removed_command_and_the_protocol_lines(self):
        for os_name in ("macos", "linux"):
            text = cli.launcher_help(os_name)
            self.assertNotIn("alerts", text)
            self.assertNotIn("$0", text)
            for line in ("Usage: onionpress <command> [args]", "--check --json", "--confirmed <what>",
                         "ONIONPRESS_CONFIRMED", "Your WordPress site will be available at:"):
                self.assertIn(line, text, os_name)
            self.assertTrue(text.endswith("\n"))

    def test_every_listed_verb_is_a_case_or_a_subcommand(self):
        """A verb the help names must reach something: a case of that
        launcher's main, or a subcommand of onionpress.cli through its
        default case."""
        sys.path.insert(0, HERE)
        from test_cli_dispatch import parser_tree
        subcommands = {key[0] for key in parser_tree()}
        for os_name, rel in (("macos", "app/MacOS/onionpress"), ("linux", "linux/onionpress")):
            with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                text = f.read()
            cases = set()
            for m in re.finditer(r"(?m)^        ([a-z][a-z|-]*)\)$", text):
                cases.update(m.group(1).split("|"))
            for verb in _verbs(cli.launcher_help(os_name)):
                self.assertTrue(verb in cases or verb in subcommands, f"{rel}: {verb}")


class TestLaunchers(unittest.TestCase):

    def test_both_help_cases_print_the_clis_text(self):
        for rel, runner in (("app/MacOS/onionpress", "bundled_python -m onionpress.cli"),
                            ("linux/onionpress", "op_py")):
            with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                text = f.read()
            i = text.index("\n        help|-h|--help)\n")
            case = text[i:text.index("\n            ;;\n", i)]
            with self.subTest(rel):
                self.assertIn(f'{runner} --data-dir "$DATA_DIR" launcher-help', case)
                self.assertNotIn("Usage:", text.split("\n        help|-h|--help)\n")[1][:4000])
                self.assertNotIn("\nusage() {", text)


if __name__ == "__main__":
    unittest.main()

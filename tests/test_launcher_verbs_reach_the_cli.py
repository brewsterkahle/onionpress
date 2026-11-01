#!/usr/bin/env python3
"""Every verb the apps run through a launcher reaches an implementation.

`src/onionpress/daemon.py`'s `_reconcile_archive_s3_keys` ran the Mac
launcher with `ensure-archive-s3-keys`, a subcommand of onionpress.cli
that no launcher case had: the launcher's default case answered usage
and exit 1 on every run, and the reconcile never ran. Two rules hold it
now:

- each launcher's default case delegates to onionpress.cli, so every
  subcommand is reachable through either launcher and a verb with no case
  of its own is the CLI's (the text and the dispatch harnesses of
  tests/test_launcher_dispatch_{mac,linux}.py run it);
- every verb that the daemon, the menubar, the Linux tray, handle_action,
  setup_logic and launcher_ops hand a launcher is a case of that launcher
  or a subcommand of the CLI (this census).
"""

import ast
import os
import re
import sys
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_cli_dispatch import parser_tree  # noqa: E402

MAC = "app/MacOS/onionpress"
LINUX = "linux/onionpress"

# (file, the launchers its verbs may name): a Mac-only caller's verb must
# be the Mac launcher's; a shared caller branches on the platform
# (handle_action's `write-status` and `settings-page-update-check` are
# Linux's), so its verb must be some launcher's. The Linux tray is held by
# tests/test_launcher_dispatch_linux.py's TestTrayRunsOnlyLauncherVerbs.
CALLERS = {
    "src/onionpress/daemon.py": (MAC,),
    "src/menubar.py": (MAC,),
    "src/onionpress/settings_ui.py": (MAC,),
    "src/onionpress/handle_action.py": (MAC, LINUX),
    "src/onionpress/setup_logic.py": (MAC, LINUX),
    "src/onionpress/launcher_ops.py": (MAC, LINUX),
}

# Names a launcher argv list may start with: `[self.launcher_script, "x"]`,
# `[launcher_bin, "x"]`, `run_launcher(["x"])`; and the shared client's
# methods, `self.launcher.run("x", …)` / `run_json` / `wait` / `run_async`
# (ui_client.Launcher, #526) and `ui_client.run_confirmed(launcher, ["x", …])`.
LAUNCHER_NAMES = re.compile(r"launcher")
LAUNCHER_CALLS = {"run_launcher", "_run_launcher_json"}
CLIENT_METHODS = {"run", "run_json", "wait", "run_async"}


def _read(rel):
    with open(os.path.join(PROJECT_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def launcher_cases(rel):
    """The verbs a launcher's `main` has a case for."""
    text = _read(rel)
    main = text[text.index("\nmain() {"):]
    verbs = set()
    for m in re.finditer(r"^        ([a-z|-]+)\)$", main, re.M):
        verbs.update(m.group(1).split("|"))
    return verbs


def _is_launcher_name(node):
    if isinstance(node, ast.Attribute):
        return bool(LAUNCHER_NAMES.search(node.attr))
    return isinstance(node, ast.Name) and bool(LAUNCHER_NAMES.search(node.id))


def _first_literal(elts):
    if elts and isinstance(elts[0], ast.Constant) and isinstance(elts[0].value, str):
        return elts[0].value
    return None


def launcher_verbs(rel):
    """(line, verb) for every launcher argv in a Python file whose verb is
    a string literal."""
    tree = ast.parse(_read(rel))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.List) and node.elts and _is_launcher_name(node.elts[0]):
            verb = _first_literal(node.elts[1:])
            if verb:
                found.append((node.lineno, verb))
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if name in LAUNCHER_CALLS and node.args and isinstance(node.args[0], ast.List):
                verb = _first_literal(node.args[0].elts)
                if verb:
                    found.append((node.lineno, verb))
            elif (name in CLIENT_METHODS and isinstance(f, ast.Attribute)
                  and _is_launcher_name(f.value)):
                verb = _first_literal(node.args)
                if verb:
                    found.append((node.lineno, verb))
            elif name == "run_confirmed" and len(node.args) > 1:
                arg = node.args[1]
                verb = _first_literal(arg.elts) if isinstance(arg, ast.List) else None
                if verb:
                    found.append((node.lineno, verb))
    return found


class TestEveryVerbReachesAnImplementation(unittest.TestCase):

    def setUp(self):
        self.cases = {MAC: launcher_cases(MAC), LINUX: launcher_cases(LINUX)}
        self.subcommands = {key[0] for key in parser_tree()}

    def test_the_census_sees_the_known_calls(self):
        verbs = {v for _l, v in launcher_verbs("src/onionpress/daemon.py")}
        self.assertEqual(verbs, {"start-tor", "config", "handle-action"})
        self.assertIn("provision-post-install",
                      {v for _l, v in launcher_verbs("src/onionpress/setup_logic.py")})
        self.assertIn("restore", {v for _l, v in launcher_verbs("src/onionpress/launcher_ops.py")})
        menubar = {v for _l, v in launcher_verbs("src/menubar.py")}
        # run_json, run, wait and run_confirmed each contribute one
        self.assertTrue({"uninstall", "update", "daemon", "start", "quit"} <= menubar, menubar)

    def test_every_verb_is_a_case_or_a_subcommand(self):
        for rel, launchers in CALLERS.items():
            cases = set().union(*(self.cases[launcher] for launcher in launchers))
            for line, verb in launcher_verbs(rel):
                with self.subTest(file=rel, line=line, verb=verb):
                    self.assertTrue(
                        verb in cases or verb in self.subcommands,
                        f"{rel}:{line} runs `{verb}` through {' / '.join(launchers)}, "
                        f"which has no case for it and onionpress.cli no subcommand: "
                        f"the launcher's default case would hand it to the CLI, which "
                        f"refuses it")

    def test_the_daemon_runs_the_clis_own_subcommand_itself(self):
        """ensure-archive-s3-keys has no launcher case: the daemon runs it
        as the CLI's (tests/test_mac_daemon.TestCliOnlySubcommands)."""
        self.assertIn("ensure-archive-s3-keys", self.subcommands)
        self.assertNotIn("ensure-archive-s3-keys", self.cases[MAC])
        daemon = _read("src/onionpress/daemon.py")
        self.assertIn('self.run_cli(["ensure-archive-s3-keys"]', daemon)
        self.assertNotIn('run_launcher(["ensure-archive-s3-keys"]', daemon)


class TestTheDefaultCaseDelegates(unittest.TestCase):

    def test_mac(self):
        mac = _read(MAC)
        self.assertRegex(mac, r'\n        \*\)\n(?:            #.*\n)*'
                              r'            bundled_python -m onionpress\.cli --data-dir "\$DATA_DIR" "\$@"\n'
                              r'            ;;\n    esac\n')
        self.assertNotIn("run '$0 help' for the full list", mac)

    def test_linux(self):
        linux = _read(LINUX)
        self.assertRegex(linux, r'\n        \*\)\n(?:            #.*\n)*'
                                r'            op_py --data-dir "\$DATA_DIR" "\$@"\n'
                                r'            ;;\n    esac\n')
        self.assertNotIn("unknown command: $1", linux)


if __name__ == "__main__":
    unittest.main()

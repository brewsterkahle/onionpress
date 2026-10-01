#!/usr/bin/env python3
"""The docker-backed integration tests must not reach a live install.

test_bluesky_importer, test_mastodon_importer, test_twitter_importer and
test_wayback_sweep write to whatever onionpress-wordpress container the
host `docker` CLI reaches, and on 2026-09-30 that was a live site. These
tests pin the defences in wp_integration.py — the opt-in, the liveness
guard, the sandboxes — and the plugin contract the sandboxes rely on.
None of them uses Docker: a fake answers every probe.
"""

import ast
import contextlib
import glob
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, TESTS_DIR)

import wp_integration  # noqa: E402

DOCKER_BACKED = (
    "test_bluesky_importer",
    "test_mastodon_importer",
    "test_twitter_importer",
    "test_wayback_sweep",
)
WAYBACK_PLUGIN = os.path.join(
    PROJECT_DIR, "app", "Resources", "plugins", "onionpress-wayback-archive.php")

MAIN_ONLY = {"multisite": True, "main": 1, "root_site": "",
             "sites": [{"blog_id": "1", "path": "/"}]}


@contextlib.contextmanager
def _env(opted_in):
    """os.environ with the opt-in set or removed, restored afterwards."""
    with mock.patch.dict(os.environ):
        os.environ.pop(wp_integration.OPT_IN_ENV, None)
        if opted_in:
            os.environ[wp_integration.OPT_IN_ENV] = "1"
        yield


def _test_classes(module):
    return [obj for obj in vars(module).values()
            if isinstance(obj, type) and issubclass(obj, unittest.TestCase)
            and unittest.defaultTestLoader.getTestCaseNames(obj)]


class FakeDocker:
    """Stands in for subprocess.run: answers the probe's docker calls and
    records every argv. Anything else is a test failure."""

    def __init__(self, running=True, onion_file="absent", probe=None,
                 probe_stdout=None):
        self.running = running
        self.onion_file = onion_file
        self.probe = MAIN_ONLY if probe is None else probe
        self.probe_stdout = probe_stdout
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        exec_prefix = ["docker", "exec", wp_integration.WP_CONTAINER]
        if argv[:2] == ["docker", "inspect"]:
            out = "true\n" if self.running else ""
            return subprocess.CompletedProcess(argv, 0 if self.running else 1, out, "")
        if argv[:4] == exec_prefix + ["sh"]:
            return subprocess.CompletedProcess(argv, 0, self.onion_file + "\n", "")
        if argv[:5] == exec_prefix + ["wp", "eval"]:
            out = self.probe_stdout
            if out is None:
                out = "OPPROBE:" + json.dumps(self.probe)
            return subprocess.CompletedProcess(argv, 0, out, "")
        raise AssertionError(f"unexpected command: {argv}")

    def loaded_wordpress(self):
        return any(c[3:4] == ["wp"] for c in self.calls)


@contextlib.contextmanager
def _patch_docker(fake):
    with mock.patch.object(subprocess, "run", fake), \
            mock.patch.object(shutil, "which", lambda name: "/usr/local/bin/docker"):
        yield


class TestOptIn(unittest.TestCase):

    def test_only_exactly_1_opts_in(self):
        for value, expected in (("1", True), ("0", False), ("", False),
                                ("true", False), ("yes", False)):
            with mock.patch.dict(os.environ, {wp_integration.OPT_IN_ENV: value}):
                self.assertIs(wp_integration.opted_in(), expected, value)
        with _env(opted_in=False):
            self.assertFalse(wp_integration.opted_in())

    def _run_gated(self):
        calls = []

        class Probe(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                calls.append("setUpClass")

            def test_it(self):
                calls.append("test")

        gated = wp_integration.gate(Probe)
        result = unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromTestCase(gated).run(result)
        return result, calls

    def test_gate_skips_without_opt_in_and_says_why(self):
        with _env(opted_in=False):
            result, calls = self._run_gated()
        self.assertEqual(calls, [], "neither setUpClass nor the test may run")
        self.assertEqual(len(result.skipped), 1)
        reason = result.skipped[0][1]
        self.assertIn(wp_integration.OPT_IN_ENV + "=1", reason)
        self.assertIn("disposable", reason)

    def test_gate_runs_when_opted_in(self):
        with _env(opted_in=True):
            result, calls = self._run_gated()
        self.assertEqual(calls, ["setUpClass", "test"])
        self.assertEqual(result.skipped, [])

    def test_every_docker_backed_class_goes_through_the_sandbox_base(self):
        for name in DOCKER_BACKED:
            module = importlib.import_module(name)
            classes = _test_classes(module)
            self.assertTrue(classes, f"{name} has no test classes?")
            for cls in classes:
                with self.subTest(cls=f"{name}.{cls.__name__}"):
                    self.assertTrue(
                        issubclass(cls, wp_integration.SandboxTestCase),
                        "derive from wp_integration.SandboxTestCase so the "
                        "opt-in gate and the liveness guard apply")
                    self.assertIn(cls.SANDBOX_SLUG, wp_integration.SANDBOX_SLUGS)

    @unittest.skipIf(wp_integration.opted_in(),
                     "this run opted in to the integration tests")
    def test_default_run_never_calls_docker(self):
        """Run the four suites the way `unittest discover` does, with every
        subprocess entry point booby-trapped."""
        suite = unittest.TestSuite(
            unittest.defaultTestLoader.loadTestsFromModule(importlib.import_module(n))
            for n in DOCKER_BACKED)
        trap = mock.Mock(side_effect=AssertionError("a gated test reached subprocess"))
        result = unittest.TestResult()
        with mock.patch.multiple(subprocess, run=trap, Popen=trap, call=trap,
                                 check_call=trap, check_output=trap):
            suite.run(result)
        self.assertEqual(trap.call_count, 0)
        self.assertEqual((result.errors, result.failures), ([], []))
        self.assertGreater(result.testsRun, 0)
        self.assertEqual(len(result.skipped), result.testsRun)


class TestNoDirectDocker(unittest.TestCase):
    """Test modules reach Docker only through wp_integration, whose helpers
    refuse to run without the opt-in and the liveness guard. The four
    suites each used to call `subprocess.run(["docker", ...])` directly."""

    SUBPROCESS_CALLS = {"run", "call", "check_call", "check_output", "Popen"}

    def _starts_with_docker(self, node):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return self._starts_with_docker(node.left)
        if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
            first = node.elts[0]
            return isinstance(first, ast.Constant) and first.value == "docker"
        return False

    def test_no_test_module_runs_docker_itself(self):
        offenders = []
        for path in sorted(glob.glob(os.path.join(TESTS_DIR, "test_*.py"))):
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read(), path)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                func = node.func
                name = (func.attr if isinstance(func, ast.Attribute)
                        else getattr(func, "id", None))
                if name in self.SUBPROCESS_CALLS and self._starts_with_docker(node.args[0]):
                    offenders.append(f"{os.path.basename(path)}:{node.lineno}")
        self.assertEqual(offenders, [],
                         "run the container through wp_integration.wp() instead")


class TestHostSignals(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = tmp.name

    def _write(self, name, text):
        with open(os.path.join(self.data_dir, name), "w") as f:
            f.write(text)

    def test_fresh_machine_has_none(self):
        self.assertEqual(wp_integration.host_signals(self.data_dir), [])

    def test_registered_onion_name(self):
        self._write("config", "ADDRESS_PREFIX=op2\nONIONNAME=someone\n"
                              "ONIONNAME_REGISTERED=yes\n")
        signals = wp_integration.host_signals(self.data_dir)
        self.assertEqual(len(signals), 1)
        self.assertIn("ONIONNAME_REGISTERED=yes", signals[0])

    def test_unregistered_onion_name_is_not_a_signal(self):
        self._write("config", "ONIONNAME=someone\nONIONNAME_REGISTERED=no\n")
        self.assertEqual(wp_integration.host_signals(self.data_dir), [])

    def test_cached_onion_address(self):
        self._write("onion_address", "op2" + "a" * 53 + ".onion\n")
        signals = wp_integration.host_signals(self.data_dir)
        self.assertEqual(len(signals), 1)
        self.assertIn("onion_address", signals[0])
        self.assertNotIn("op2aaa", signals[0], "don't print the address")

    def test_empty_onion_address_file_is_not_a_signal(self):
        self._write("onion_address", "\n")
        self.assertEqual(wp_integration.host_signals(self.data_dir), [])

    def test_reads_the_real_data_dir_by_default(self):
        os.makedirs(os.path.join(self.data_dir, ".onionpress"))
        with open(os.path.join(self.data_dir, ".onionpress", "onion_address"), "w") as f:
            f.write("x.onion\n")
        with mock.patch.dict(os.environ, {"HOME": self.data_dir}):
            self.assertEqual(len(wp_integration.host_signals()), 1)


class TestTargetSignals(unittest.TestCase):

    def _signals(self, fake):
        with _patch_docker(fake):
            return wp_integration.target_signals()

    def test_disposable_stack_has_none(self):
        fake = FakeDocker(probe={**MAIN_ONLY, "sites": [
            {"blog_id": "1", "path": "/"},
            {"blog_id": "2", "path": "/op-bluesky-test/"},
            {"blog_id": "7", "path": "/op-wayback-test/"},
        ]})
        self.assertEqual(self._signals(fake), [])

    def test_owner_subsite(self):
        fake = FakeDocker(probe={**MAIN_ONLY, "sites": [
            {"blog_id": "1", "path": "/"},
            {"blog_id": "2", "path": "/elphinstone.farm/"},
            {"blog_id": "3", "path": "/op-mastodon-test/"},
        ]})
        signals = self._signals(fake)
        self.assertEqual(len(signals), 1)
        self.assertIn("/elphinstone.farm/", signals[0])
        self.assertNotIn("/op-mastodon-test/", signals[0])

    def test_branded_root_site(self):
        fake = FakeDocker(probe={**MAIN_ONLY, "root_site": "yes"})
        signals = self._signals(fake)
        self.assertEqual(len(signals), 1)
        self.assertIn("onionpress_root_site=yes", signals[0])

    def test_onion_address_in_container_stops_before_loading_wordpress(self):
        fake = FakeDocker(onion_file="present")
        signals = self._signals(fake)
        self.assertEqual(len(signals), 1)
        self.assertIn(wp_integration.CONTAINER_ONION_FILE, signals[0])
        self.assertFalse(fake.loaded_wordpress())

    def test_probe_survives_php_notices(self):
        answer = "OPPROBE:" + json.dumps({**MAIN_ONLY, "root_site": "yes"})
        for stdout in ("PHP Notice: something deprecated\n" + answer,
                       "Deprecated: no newline after this" + answer + "\n"):
            with self.subTest(stdout=stdout[:20]):
                self.assertEqual(len(self._signals(FakeDocker(probe_stdout=stdout))), 1)

    def test_unanswerable_probes_refuse(self):
        cases = {
            "container not running": FakeDocker(running=False),
            "file check failed": FakeDocker(onion_file="sh: not found"),
            "wordpress not answering": FakeDocker(probe_stdout="Error: no db"),
            "not multisite": FakeDocker(probe={"multisite": False}),
        }
        for label, fake in cases.items():
            with self.subTest(label):
                with self.assertRaises(wp_integration.UnsafeTargetError):
                    self._signals(fake)

    def test_no_docker_cli_refuses(self):
        with mock.patch.object(wp_integration.shutil, "which", return_value=None):
            with self.assertRaises(wp_integration.UnsafeTargetError):
                wp_integration.target_signals()


class TestRequireDisposableTarget(unittest.TestCase):

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(wp_integration, "_verdict", None))
        stack.enter_context(mock.patch.dict(os.environ, {"HOME": self.home.name}))
        stack.enter_context(_env(opted_in=True))

    def _mark_live_machine(self):
        os.makedirs(os.path.join(self.home.name, ".onionpress"))
        with open(os.path.join(self.home.name, ".onionpress", "config"), "w") as f:
            f.write("ONIONNAME_REGISTERED=yes\n")

    def test_without_opt_in_it_refuses_before_any_probe(self):
        fake = FakeDocker()
        with _env(opted_in=False), _patch_docker(fake):
            with self.assertRaises(RuntimeError):
                wp_integration.require_disposable_target()
        self.assertEqual(fake.calls, [])

    def test_live_machine_is_refused_without_asking_docker(self):
        self._mark_live_machine()
        fake = FakeDocker()
        with _patch_docker(fake):
            with self.assertRaises(wp_integration.UnsafeTargetError) as ctx:
                wp_integration.require_disposable_target()
        self.assertEqual(fake.calls, [])
        self.assertIn("live OnionPress install", str(ctx.exception))
        self.assertIn("docs/BUILDING.md", str(ctx.exception))

    def test_refusal_is_an_error_every_time_and_probed_once(self):
        fake = FakeDocker(onion_file="present")
        with _patch_docker(fake):
            for _ in range(3):
                with self.assertRaises(wp_integration.UnsafeTargetError):
                    wp_integration.require_disposable_target()
        self.assertEqual(len(fake.calls), 2, "inspect + file check, once")

    def test_disposable_target_passes_and_is_probed_once(self):
        fake = FakeDocker()
        with _patch_docker(fake):
            wp_integration.require_disposable_target()
            wp_integration.require_disposable_target()
        self.assertEqual(len(fake.calls), 3, "inspect + file check + probe, once")

    def test_a_crashing_probe_refuses(self):
        def boom(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 15)
        with _patch_docker(boom):
            with self.assertRaises(wp_integration.UnsafeTargetError) as ctx:
                wp_integration.require_disposable_target()
        self.assertIn("probe failed", str(ctx.exception))

    def test_helpers_never_run_a_command_on_a_refused_target(self):
        self._mark_live_machine()
        fake = FakeDocker()
        with _patch_docker(fake):
            for call in (lambda: wp_integration.wp(["post", "create"]),
                         lambda: wp_integration.wp_eval("echo 1;", None)):
                with self.assertRaises(wp_integration.UnsafeTargetError):
                    call()
        self.assertEqual(fake.calls, [])

    def test_helpers_refuse_without_opt_in(self):
        fake = FakeDocker()
        with _env(opted_in=False), _patch_docker(fake):
            with self.assertRaises(RuntimeError):
                wp_integration.wp(["site", "list"])
        self.assertEqual(fake.calls, [])


class TestSandboxes(unittest.TestCase):

    def test_only_sandbox_slugs_can_be_created_or_deleted(self):
        trap = mock.Mock(side_effect=AssertionError("reached WordPress"))
        with mock.patch.multiple(wp_integration, wp=trap, wp_eval=trap):
            for func in (lambda s: wp_integration.open_sandbox(s, "t"),
                         wp_integration.close_sandbox):
                for slug in ("elphinstone.farm", "", "op-bluesky-test/../x"):
                    with self.subTest(slug=slug):
                        with self.assertRaises(ValueError):
                            func(slug)
        trap.assert_not_called()

    def test_sandbox_is_created_excluded_and_non_public_in_one_call(self):
        evals = []

        def fake_wp_eval(php, url):
            evals.append(php)
            return "EXCLUDE:yes"

        with mock.patch.multiple(wp_integration, wp_eval=fake_wp_eval,
                                 close_sandbox=mock.Mock(),
                                 _sandbox_url=mock.Mock(return_value="http://localhost/op-twitter-test/")):
            url = wp_integration.open_sandbox("op-twitter-test", "T")
            wp_integration.close_sandbox.assert_called_once_with("op-twitter-test")
        self.assertEqual(url, "http://localhost/op-twitter-test/")
        self.assertEqual(len(evals), 1)
        php = evals[0]
        self.assertIn("wpmu_create_blog(", php)
        self.assertIn("'public' => 0", php)
        self.assertIn(f"'{wp_integration.WAYBACK_EXCLUDE_OPTION}' => 'yes'", php)

    def test_creation_without_the_exclusion_is_an_error(self):
        with mock.patch.multiple(wp_integration,
                                 wp_eval=mock.Mock(return_value="EXCLUDE:"),
                                 close_sandbox=mock.Mock()):
            with self.assertRaises(RuntimeError):
                wp_integration.open_sandbox("op-twitter-test", "T")

    def _sandbox_class(self, **attrs):
        return type("Sandboxed", (wp_integration.SandboxTestCase,),
                    {"SANDBOX_SLUG": "op-wayback-test", "SANDBOX_TITLE": "T",
                     "test_x": lambda self: None, **attrs})

    def test_refused_target_gets_no_sandbox(self):
        cls = self._sandbox_class()
        opened, closed = mock.Mock(), mock.Mock()
        with mock.patch.multiple(
                wp_integration, open_sandbox=opened, close_sandbox=closed,
                require_disposable_target=mock.Mock(
                    side_effect=wp_integration.UnsafeTargetError("live"))):
            with self.assertRaises(wp_integration.UnsafeTargetError):
                cls.setUpClass()
            cls.doClassCleanups()
        opened.assert_not_called()
        closed.assert_not_called()

    def test_half_created_sandbox_is_still_deleted(self):
        cls = self._sandbox_class()
        closed = mock.Mock()
        with mock.patch.multiple(
                wp_integration, require_disposable_target=mock.Mock(),
                _missing_php_functions=mock.Mock(return_value=[]),
                open_sandbox=mock.Mock(side_effect=RuntimeError("not listed")),
                close_sandbox=closed):
            with self.assertRaises(RuntimeError):
                cls.setUpClass()
            cls.doClassCleanups()
        closed.assert_called_once_with("op-wayback-test")

    def test_missing_plugin_functions_refuse_before_the_sandbox(self):
        cls = self._sandbox_class(REQUIRED_PHP_FUNCTIONS=("onionpress_wayback_sites",))
        opened = mock.Mock()
        with mock.patch.multiple(
                wp_integration, require_disposable_target=mock.Mock(),
                _missing_php_functions=mock.Mock(return_value=["onionpress_wayback_sites"]),
                open_sandbox=opened, close_sandbox=mock.Mock()):
            with self.assertRaises(RuntimeError) as ctx:
                cls.setUpClass()
            cls.doClassCleanups()
        opened.assert_not_called()
        self.assertIn("onionpress_wayback_sites", str(ctx.exception))


class TestWaybackPluginContract(unittest.TestCase):
    """What the sandboxes rely on in onionpress-wayback-archive.php."""

    @classmethod
    def setUpClass(cls):
        with open(WAYBACK_PLUGIN, encoding="utf-8") as f:
            cls.php = f.read()

    def test_exclusion_option_name_matches(self):
        m = re.search(r"define\(\s*'OP_WB_OPT_EXCLUDE',\s*'([^']+)'\s*\)", self.php)
        self.assertIsNotNone(m, "OP_WB_OPT_EXCLUDE is gone from the plugin")
        self.assertEqual(m.group(1), wp_integration.WAYBACK_EXCLUDE_OPTION)

    def test_every_network_walk_honors_the_exclusion(self):
        """get_sites() appears once, inside onionpress_wayback_sites(); the
        sweep loop and the queue totals walk that list."""
        self.assertEqual(self.php.count("get_sites("), 1)
        body = self.php[self.php.index("function onionpress_wayback_sites()"):]
        body = body[:body.index("\n}\n")]
        self.assertIn("get_sites(", body)
        self.assertIn("onionpress_wayback_site_excluded(", body)
        for func in ("onionpress_wayback_queue_totals", "onionpress_wayback_sweep_loop"):
            start = self.php.index(f"function {func}(")
            fbody = self.php[start:self.php.index("\n}\n", start)]
            self.assertIn("onionpress_wayback_sites()", fbody, func)

    def test_guard_checks_the_file_the_sweep_reads(self):
        start = self.php.index("function onionpress_wayback_onion_addr()")
        body = self.php[start:self.php.index("\n}\n", start)]
        self.assertIn(f"'{wp_integration.CONTAINER_ONION_FILE}'", body)
        self.assertIn("onionpress_wayback_onion_addr_mock", body)


if __name__ == "__main__":
    unittest.main()

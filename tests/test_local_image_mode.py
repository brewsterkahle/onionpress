#!/usr/bin/env python3
"""Local-image mode: the app must not pull over images you built yourself.

Building the images locally was only half a local build path. Every start ran
`docker compose pull`, and `start-tor` ran `docker compose up --pull always`,
so a locally built tag was replaced by the registry's copy before it ever
started — you would build an image, launch the app, and silently test someone
else's build. The failure is invisible: the stack comes up fine, it is just
not running your code.

Three implementations of the same predicate have to agree — app/MacOS/onionpress,
linux/onionpress and onionpress.containers.using_local_images() — so these
checks cover all three.
"""

import os
import re
import subprocess
import tempfile
import unittest
from unittest import mock
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from onionpress.containers import using_local_images  # noqa: E402

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

LAUNCHERS = ("app/MacOS/onionpress", "linux/onionpress")

# A shell function definition at column 0, e.g. `update_images() {`.
FUNC_DEF = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\(\) \{")


def _read(rel_path):
    with open(os.path.join(PROJECT_ROOT, rel_path), "r", encoding="utf-8") as f:
        return f.read()


def _code_lines(text):
    """(1-indexed line number, line) for non-comment lines."""
    return [(i, l) for i, l in enumerate(text.splitlines(), 1)
            if not l.lstrip().startswith("#")]


class TestPredicate(unittest.TestCase):
    """os.environ is mutated under mock.patch.dict deliberately: several
    modules read image env vars at import time, and tests/test_onionheaven_*.py
    import them at module scope, so an unscoped mutation would leak across
    discovery order.

    config_file=os.devnull everywhere, also deliberately: when the env is
    cleared the predicate falls through to the config FILE, and its default is
    the real ~/.onionpress/config on whatever machine runs the suite. These
    tests passed on the author's machine only because that file happened not
    to set an override.
    """

    def test_unset_means_published_images(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(using_local_images(config_file=os.devnull))

    def test_ghcr_reference_is_not_a_local_build(self):
        """Pointing at a published image by digest is a deliberate pin, not a
        local build — pulls must keep working.
        """
        for ref in (
            "ghcr.io/brewsterkahle/onionpress-tor:latest",
            "ghcr.io/brewsterkahle/onionpress-tor:latest@sha256:" + "a" * 64,
        ):
            with self.subTest(ref=ref):
                with mock.patch.dict(os.environ, {"ONIONPRESS_TOR_IMAGE": ref}, clear=True):
                    self.assertFalse(using_local_images(config_file=os.devnull))

    def test_local_tag_is_a_local_build(self):
        with mock.patch.dict(os.environ, {"ONIONPRESS_TOR_IMAGE": "onionpress-tor:dev"}, clear=True):
            self.assertTrue(using_local_images(config_file=os.devnull))

    def test_either_variable_is_enough(self):
        """A developer iterating on only the WordPress image still must not
        have it pulled over.
        """
        with mock.patch.dict(
            os.environ,
            {"ONIONPRESS_WORDPRESS_IMAGE": "onionpress-wordpress:dev"},
            clear=True,
        ):
            self.assertTrue(using_local_images(config_file=os.devnull))


class TestImageOverrideReadsTheConfigFile(unittest.TestCase):
    """The macOS MenubarApp spawns the launcher, so the launcher's exports can
    never reach it. It has to read ~/.onionpress/config itself, or the
    documented config route leaves its own pull ungated.
    """

    def _write(self, text):
        handle = tempfile.NamedTemporaryFile("w", suffix=".config", delete=False)
        handle.write(text)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_reads_the_value_from_the_given_file(self):
        from onionpress.containers import image_override
        cfg = self._write("ADDRESS_PREFIX=op2\nONIONPRESS_TOR_IMAGE=onionpress-tor:dev\n")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual("onionpress-tor:dev",
                             image_override("ONIONPRESS_TOR_IMAGE", cfg))
            self.assertTrue(using_local_images(config_file=cfg))

    def test_environment_wins_over_the_file(self):
        from onionpress.containers import image_override
        cfg = self._write("ONIONPRESS_TOR_IMAGE=onionpress-tor:from-file\n")
        with mock.patch.dict(os.environ, {"ONIONPRESS_TOR_IMAGE": "onionpress-tor:from-env"}, clear=True):
            self.assertEqual("onionpress-tor:from-env",
                             image_override("ONIONPRESS_TOR_IMAGE", cfg))

    def test_empty_value_and_missing_file_mean_no_override(self):
        from onionpress.containers import image_override
        cfg = self._write("ONIONPRESS_TOR_IMAGE=\n")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(image_override("ONIONPRESS_TOR_IMAGE", cfg))
            self.assertIsNone(image_override("ONIONPRESS_TOR_IMAGE",
                                             "/nonexistent/onionpress/config"))
            self.assertFalse(using_local_images(config_file="/nonexistent/onionpress/config"))

    def test_callers_with_paths_pass_their_config_file(self):
        """resolve_paths() supports a data_dir override; a hardcoded home path
        would silently read the wrong file under one. Callers that already
        hold an OnionPressPaths must pass it through.
        """
        containers = _read("src/onionpress/containers.py")
        self.assertIn("onionheaven_image(self.paths.config_file)", containers)
        menubar = _read("src/menubar.py")
        self.assertEqual(
            2, menubar.count("containers.using_local_images(self._paths.config_file)"),
            "Both menubar call sites must pass the resolved config path.",
        )


class TestConfigOverrideLoopIsInjectionSafe(unittest.TestCase):
    """Both launchers read the override with `eval`. ~/.onionpress/config is
    user-writable, and on macOS the config is also hand-edited per the docs,
    so a value must never be executed. This runs the launchers' ACTUAL loop
    text — extracted, not re-typed — against a hostile config.
    """

    LOOP = re.compile(
        r"for _img_var in ONIONPRESS_TOR_IMAGE ONIONPRESS_WORDPRESS_IMAGE; do.*?"
        r"unset _img_var _img_val",
        re.S,
    )

    def test_hostile_values_stay_literal(self):
        for launcher in LAUNCHERS:
            match = self.LOOP.search(_read(launcher))
            self.assertIsNotNone(
                match, f"Could not find the override loop in {launcher} — "
                       "restructured? Update this test.")
            with tempfile.TemporaryDirectory() as data_dir:
                marker = os.path.join(data_dir, "PWNED")
                with open(os.path.join(data_dir, "config"), "w") as f:
                    f.write(f"ONIONPRESS_TOR_IMAGE=$(touch {marker}-subst)\n")
                    f.write(f"ONIONPRESS_WORDPRESS_IMAGE=`touch {marker}-tick`;touch {marker}-semi\n")
                script = (
                    "set -e\n"
                    f"DATA_DIR={data_dir!r}\n"
                    + match.group(0)
                    + '\nprintf "%s|%s" "$ONIONPRESS_TOR_IMAGE" "$ONIONPRESS_WORDPRESS_IMAGE"\n'
                )
                result = subprocess.run(
                    ["bash", "-c", script], capture_output=True, text=True,
                    env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                    timeout=30,
                )
                with self.subTest(launcher=launcher):
                    self.assertEqual(0, result.returncode, result.stderr)
                    tor, wp = result.stdout.split("|", 1)
                    self.assertTrue(tor.startswith("$(touch "),
                                    f"value was interpreted, not stored: {tor!r}")
                    self.assertTrue(wp.startswith("`touch "),
                                    f"value was interpreted, not stored: {wp!r}")
                    created = [n for n in os.listdir(data_dir) if n.startswith("PWNED")]
                    self.assertEqual([], created,
                                     f"config value executed commands: {created}")


class TestLaunchersDefineThePredicate(unittest.TestCase):

    def test_both_launchers_define_it(self):
        for path in LAUNCHERS:
            with self.subTest(path=path):
                self.assertIsNotNone(
                    re.search(r"^using_local_images\(\) \{", _read(path), re.M),
                    f"{path} must define using_local_images().",
                )

    def test_both_treat_ghcr_as_published(self):
        for path in LAUNCHERS:
            with self.subTest(path=path):
                self.assertIn(
                    "ghcr.io/*)", _read(path),
                    f"{path}'s using_local_images() must treat a ghcr.io "
                    "reference as a published image, not a local build.",
                )

    def test_both_read_the_overrides_from_config(self):
        """So an installed app can be pointed at local images without editing
        the launcher.
        """
        for path in LAUNCHERS:
            text = _read(path)
            with self.subTest(path=path):
                self.assertIn("ONIONPRESS_TOR_IMAGE", text)
                self.assertIn("ONIONPRESS_WORDPRESS_IMAGE", text)
                self.assertRegex(
                    text, r'grep "\^\$\{_img_var\}=" "\$DATA_DIR/config"',
                    f"{path} must read the image overrides from "
                    "~/.onionpress/config.",
                )

    def test_config_read_survives_set_e(self):
        """`var=$(grep ...)` takes grep's exit status, so a missing key returns
        1 and kills the whole script under `set -e`. Piping through cut makes
        the pipeline's status cut's. This is the repo's established idiom and
        dropping the pipe is a real outage.
        """
        for path in LAUNCHERS:
            with self.subTest(path=path):
                self.assertRegex(
                    _read(path),
                    r'grep "\^\$\{_img_var\}=" "\$DATA_DIR/config" \| head -1 \| cut -d= -f2-',
                    f"{path}'s image-override config read must stay piped "
                    "through cut (see docstring).",
                )


class TestEveryPullIsGuarded(unittest.TestCase):
    """The pull that actually decides which image runs was never the opt-in
    one. On macOS, update_images() is gated by UPDATE_ON_LAUNCH but the
    `docker compose pull` four lines later was not, and ran on every single
    start. Gating only the opt-in path would have changed nothing.
    """

    PULL_PATTERNS = (
        r"docker compose pull",
        r"docker pull ",
        r"--pull always",
    )

    def _enclosing_function(self, lines, index):
        """(name, start_index) of the function containing lines[index].

        Scoping the check to the whole enclosing function rather than to a
        fixed window above the pull matters: update_images() guards itself
        with an early `return 0` at the top, and its `docker pull` sits ~20
        lines below inside a loop. A proximity check calls that unguarded.
        """
        for j in range(index, -1, -1):
            match = FUNC_DEF.match(lines[j][1])
            if match:
                return match.group(1), j
        return None, 0

    def _guarded(self, lines, index):
        _, start = self._enclosing_function(lines, index)
        return any("using_local_images" in lines[j][1] for j in range(start, index))

    def test_no_unguarded_pull_in_either_launcher(self):
        for path in LAUNCHERS:
            lines = _code_lines(_read(path))
            found = 0
            unguarded = []
            for i, (lineno, line) in enumerate(lines):
                if not any(re.search(p, line) for p in self.PULL_PATTERNS):
                    continue
                found += 1
                if not self._guarded(lines, i):
                    unguarded.append(f"{path}:{lineno}: {line.strip()}")
            with self.subTest(path=path):
                self.assertTrue(
                    found,
                    f"No image pull found in {path} at all — have they been "
                    "renamed or removed? Update this test rather than "
                    "letting it pass vacuously.",
                )
                self.assertEqual(
                    [], unguarded,
                    "These pulls are not guarded by using_local_images(), so "
                    "they would overwrite a locally built image:\n  "
                    + "\n  ".join(unguarded),
                )

    def test_start_tor_does_not_force_pull_local_images(self):
        """`--pull always` force-pulls even when every other pull is skipped,
        so it needs its own branch rather than just a surrounding guard.
        """
        text = _read("app/MacOS/onionpress")
        self.assertIn(
            "docker compose up -d tor", text,
            "The start-tor path must have a no-pull branch for local images.",
        )
        self.assertIn("docker compose up -d --pull always tor", text,
                      "...and keep the force-pull branch for published images.")


class TestMenubarGatesItsPull(unittest.TestCase):
    """src/menubar.py has its own pull path (the menubar launch and
    "Check for Updates"), separate from the bash launcher's.
    """

    def test_update_docker_images_checks_the_predicate(self):
        text = _read("src/menubar.py")
        match = re.search(
            r"def update_docker_images\(.*?\n(.*?)\n    def ", text, re.S)
        self.assertIsNotNone(
            match,
            "Could not find update_docker_images in src/menubar.py — renamed? "
            "Update this test.",
        )
        # Comment-stripped: the guard carries a three-line comment naming
        # containers.using_local_images(), so an unstripped scan passed even
        # with the guard itself deleted.
        body = "\n".join(
            line for line in match.group(1).splitlines()
            if not line.lstrip().startswith("#")
        )
        # `using_local_images(` — the call now passes the resolved config path,
        # so do not pin the exact argument list here.
        self.assertIn(
            "using_local_images(", body,
            "menubar.update_docker_images() must skip the pull when running "
            "locally built images, like both bash launchers do.",
        )
        self.assertLess(
            body.index("using_local_images("), body.index('["pull"]'),
            "The guard must come before the pull, not after it.",
        )

    def test_does_not_claim_images_are_up_to_date_when_skipping(self):
        """The generic 'All container images are up to date' alert would be a
        lie in local-image mode.
        """
        text = _read("src/menubar.py")
        match = re.search(
            r"def _check_docker_updates_async\(.*?\n(.*?)\n    def ", text, re.S)
        self.assertIsNotNone(
            match,
            "Could not find _check_docker_updates_async — renamed? Update "
            "this test.",
        )
        self.assertIn(
            "using_local_images(",
            "\n".join(line for line in match.group(1).splitlines()
                      if not line.lstrip().startswith("#")),
        )


class TestDevUpScript(unittest.TestCase):

    SCRIPT = "build/dev-up.sh"

    def test_exists_and_is_executable(self):
        path = os.path.join(PROJECT_ROOT, self.SCRIPT)
        self.assertTrue(os.path.exists(path), f"{self.SCRIPT} is missing.")
        self.assertTrue(os.access(path, os.X_OK), f"{self.SCRIPT} is not executable.")

    def test_refuses_to_start_over_a_running_stack(self):
        """docker-compose.yml hardcodes container_name: and volume name:, so
        there is exactly one OnionPress stack per daemon. Starting a "dev"
        stack does not isolate anything — it takes the running one over.
        """
        text = _read(self.SCRIPT)
        self.assertIn("docker ps --format", text)
        self.assertIn(
            "already running", text,
            "dev-up.sh must refuse to start over a running OnionPress stack.",
        )

    def test_never_removes_volumes(self):
        """`docker compose down -v` would delete the user's site: the
        database, the WordPress content and the onion service keys.
        """
        text = _read(self.SCRIPT)
        self.assertNotRegex(
            text, r"docker compose down\s+.*-v",
            "dev-up.sh --down must never remove volumes — they hold the "
            "user's site and onion keys.",
        )

    def test_verifies_the_images_exist_first(self):
        text = _read(self.SCRIPT)
        self.assertIn("docker image inspect", text)
        self.assertIn("do not exist", text)


if __name__ == "__main__":
    unittest.main()

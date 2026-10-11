"""The first-post experience for a brand new author (#193).

User testing found that a new user who wanted to write a post could not
find where to start. Two things fix that:

  1. The onboarding mu-plugin puts an explicit "New Post" entry in the
     admin bar, next to the site name, for anyone who can write posts.
  2. The per-user subsite's stock "Hello world!" post is replaced with
     an onboarding post that explains what an OnionPress site is for.

The replacement is guarded on the stock title so it can never rewrite a
post the author has touched, a restored backup, or an upgraded site.
"""

import os
import re
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from onionpress import setup_logic  # noqa: E402

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _read(rel):
    with open(os.path.join(PROJECT_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _ok(stdout=""):
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout=stdout, stderr="",
    )


def _fail(stderr="error", code=1):
    return subprocess.CompletedProcess(
        args=[], returncode=code, stdout="", stderr=stderr,
    )


def _wp_calls(mrun):
    """The wp-cli argv (after `wp --allow-root`) of every mocked call."""
    out = []
    for c in mrun.call_args_list:
        argv = c.args[0] if c.args else []
        if isinstance(argv, list) and "wp" in argv:
            out.append(argv[argv.index("--allow-root") + 1:])
    return out


class TestNewPostAdminBarNode(unittest.TestCase):
    """The onboarding mu-plugin registers a top-level "New Post" node."""

    def setUp(self):
        self.src = _read("app/Resources/plugins/onionpress-onboarding.php")
        m = re.search(
            r"function onionpress_onboarding_new_post_node\s*\(.*?\n\}",
            self.src, re.DOTALL,
        )
        self.assertIsNotNone(m, "onionpress_onboarding_new_post_node is missing")
        self.body = m.group(0)

    def test_hooked_on_admin_bar_menu_next_to_site_name(self):
        m = re.search(
            r"add_action\(\s*'admin_bar_menu',\s*"
            r"'onionpress_onboarding_new_post_node',\s*(\d+)\s*\)",
            self.src,
        )
        self.assertIsNotNone(m, "the node must be registered via admin_bar_menu")
        prio = int(m.group(1))
        # Core adds the site-name node at 30 and "+ New" at 70; the node
        # has to land between them to sit right after the site name.
        self.assertGreater(prio, 30)
        self.assertLess(prio, 70)

    def test_node_is_labelled_new_post_and_opens_the_editor(self):
        self.assertIn("'id'    => 'onionpress-new-post'", self.body)
        self.assertIn("New Post", self.body)
        self.assertIn("admin_url( 'post-new.php' )", self.body)
        # Top-level: no 'parent' — the whole point is that it is not
        # buried inside the "+ New" dropdown.
        self.assertNotIn("'parent'", self.body)

    def test_only_for_users_who_can_write_posts(self):
        self.assertIn("current_user_can( 'edit_posts' )", self.body)
        self.assertIn("is_user_logged_in()", self.body)

    def test_plugin_is_in_the_shipped_mu_plugin_list(self):
        from onionpress import multisite
        self.assertIn("onionpress-onboarding.php", multisite.MU_PLUGINS)


class TestWelcomePostContent(unittest.TestCase):
    """The onboarding post reads as block-editor content in the
    project's voice and names real features."""

    def test_uses_block_markup(self):
        c = setup_logic.WELCOME_POST_CONTENT
        self.assertIn("<!-- wp:heading -->", c)
        self.assertIn("<!-- wp:paragraph -->", c)
        self.assertEqual(c.count("<!-- wp:list-item -->"),
                         c.count("<!-- /wp:list-item -->"))

    def test_names_real_features(self):
        c = setup_logic.WELCOME_POST_CONTENT
        for feature in ("New Post", "Open in Tor Browser", "onionname",
                        "My Creations", "Backup", "menubar"):
            self.assertIn(feature, c)

    def test_project_naming_rules(self):
        text = setup_logic.WELCOME_POST_TITLE + setup_logic.WELCOME_POST_CONTENT
        self.assertIn("onion service", text)
        self.assertNotIn("hidden service", text.lower())
        self.assertNotIn("Onion.Press", text)
        self.assertNotIn("onion-press", text.lower())

    def test_length_is_a_short_read(self):
        words = re.sub(r"<[^>]+>", " ", setup_logic.WELCOME_POST_CONTENT).split()
        self.assertGreaterEqual(len(words), 150)
        self.assertLessEqual(len(words), 260)

    def test_default_title_matches_wordpress_core(self):
        # wp_install_defaults() writes __('Hello world!') — the guard
        # compares against exactly that.
        self.assertEqual(setup_logic.WP_DEFAULT_POST_TITLE, "Hello world!")


class TestInstallWelcomePost(unittest.TestCase):
    """install_welcome_post only rewrites post 1 while it is the stock
    "Hello world!"; everything else is left untouched."""

    def _run_with(self, responder):
        with mock.patch(
            "onionpress.setup_logic.subprocess.run", side_effect=responder,
        ) as mrun:
            ok = setup_logic.install_welcome_post(
                blog_url="http://localhost/alice/", log_func=lambda m: None,
            )
        return ok, mrun

    @staticmethod
    def _responder(title, comment_author="A WordPress Commenter"):
        def respond(argv, **_kw):
            if "get" in argv and "post" in argv:
                return _ok(title + "\n")
            if "get" in argv and "comment" in argv:
                return _ok(comment_author + "\n")
            return _ok()
        return respond

    def test_replaces_the_stock_post(self):
        ok, mrun = self._run_with(self._responder("Hello world!"))
        self.assertTrue(ok)
        updates = [a for a in _wp_calls(mrun) if a[:3] == ["post", "update", "1"]]
        self.assertEqual(len(updates), 1)
        argv = updates[0]
        self.assertIn(f"--post_title={setup_logic.WELCOME_POST_TITLE}", argv)
        self.assertIn(f"--post_name={setup_logic.WELCOME_POST_SLUG}", argv)
        self.assertIn(f"--post_content={setup_logic.WELCOME_POST_CONTENT}", argv)
        self.assertIn("--post_status=publish", argv)
        # Scoped to the subsite, never the network root.
        self.assertIn("--url=http://localhost/alice/", argv)

    def test_guard_checks_post_1_on_the_same_site_first(self):
        ok, mrun = self._run_with(self._responder("Hello world!"))
        calls = _wp_calls(mrun)
        self.assertEqual(calls[0][:3], ["post", "get", "1"])
        self.assertIn("--field=post_title", calls[0])
        self.assertIn("--url=http://localhost/alice/", calls[0])

    def test_leaves_an_edited_post_alone(self):
        ok, mrun = self._run_with(self._responder("My first real post"))
        self.assertTrue(ok)
        for argv in _wp_calls(mrun):
            self.assertNotIn("update", argv, "edited post must not be rewritten")
            self.assertNotIn("delete", argv)

    def test_leaves_the_already_installed_welcome_post_alone(self):
        # Idempotent: a second run (e.g. wizard re-run, "subsite already
        # exists" path) must not rewrite it again.
        ok, mrun = self._run_with(self._responder(setup_logic.WELCOME_POST_TITLE))
        self.assertTrue(ok)
        self.assertFalse(
            [a for a in _wp_calls(mrun) if "update" in a or "delete" in a])

    def test_leaves_post_alone_when_lookup_fails(self):
        # No post 1 at all (deleted, or a restored site) → do nothing.
        def respond(argv, **_kw):
            if "get" in argv:
                return _fail("Error: Could not find the post.")
            return _ok()
        ok, mrun = self._run_with(respond)
        self.assertTrue(ok)
        self.assertFalse([a for a in _wp_calls(mrun) if "update" in a])

    def test_removes_only_the_stock_sample_comment(self):
        ok, mrun = self._run_with(self._responder("Hello world!"))
        deletes = [a for a in _wp_calls(mrun) if a[:2] == ["comment", "delete"]]
        self.assertEqual(len(deletes), 1)
        self.assertEqual(deletes[0][2], "1")

        ok, mrun = self._run_with(
            self._responder("Hello world!", comment_author="alice"))
        self.assertFalse([a for a in _wp_calls(mrun) if a[:2] == ["comment", "delete"]])

    def test_update_failure_is_reported(self):
        def respond(argv, **_kw):
            if "get" in argv:
                return _ok("Hello world!\n")
            if "update" in argv:
                return _fail("Error: database gone")
            return _ok()
        ok, _ = self._run_with(respond)
        self.assertFalse(ok)


class TestWelcomePostWiring(unittest.TestCase):
    """provision_primary_subsite installs the welcome post on the new
    subsite; branded installs (no personal subsite) never get it."""

    def test_fresh_subsite_gets_the_welcome_post(self):
        with mock.patch(
            "onionpress.setup_logic.subprocess.run",
            side_effect=TestInstallWelcomePost._responder("Hello world!"),
        ) as mrun:
            setup_logic.provision_primary_subsite(
                onionname="alice", site_title="Alice", onion_addr="abc.onion",
            )
        updates = [a for a in _wp_calls(mrun) if a[:3] == ["post", "update", "1"]]
        self.assertEqual(len(updates), 1)
        self.assertIn("--url=http://localhost/alice/", updates[0])

    def test_branded_install_is_untouched(self):
        branded = next(iter(setup_logic._BRANDED_ONIONS))
        with mock.patch(
            "onionpress.setup_logic.subprocess.run", return_value=_ok(),
        ) as mrun:
            setup_logic.provision_primary_subsite(
                onionname="hub", site_title="Hub", onion_addr=branded,
            )
        for argv in _wp_calls(mrun):
            self.assertNotEqual(argv[:2], ["post", "get"])
            self.assertNotEqual(argv[:2], ["post", "update"])

    def test_restore_and_upgrade_paths_do_not_touch_post_1(self):
        # The only caller is the subsite provisioning that runs from the
        # setup wizard. The start-up / restore provisioning in
        # multisite.provision_post_install must stay out of it.
        src = _read("src/onionpress/multisite.py")
        self.assertNotIn("install_welcome_post", src)
        self.assertNotIn("post update", src)
        callers = re.findall(r"^\s+install_welcome_post\(",
                             _read("src/onionpress/setup_logic.py"), re.M)
        self.assertEqual(len(callers), 1)


if __name__ == "__main__":
    unittest.main()

"""Issue #402: the owner-only endpoints with a side effect carry a nonce.

`/onionpress-status-json` writes requested-action (asking the host for a
status rewrite), `wp_ajax_onionpress_check_update` makes a request
through Tor and writes a transient, and `wp_ajax_onionpress_poll_action`
removes service-result.json once read. All three are GETs gated on the
owner, so without a nonce any page the owner has open could make them
(CSRF). The status refresh is also never written over a pending request.
"""

import os
import re
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS = os.path.join(PROJECT_ROOT, "app/Resources/plugins/onionpress-settings.php")
STATUS = os.path.join(PROJECT_ROOT, "app/Resources/plugins/onionpress-status.php")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _handler(text, hook):
    """The body of add_action( '<hook>', function () { ... } );"""
    start = text.index(f"add_action( '{hook}'")
    end = text.index("\n} );", start)
    return text[start:end]


class TestAjaxHandlers(unittest.TestCase):

    def setUp(self):
        self.text = _read(SETTINGS)

    def test_poll_action_checks_its_nonce_after_the_owner_check(self):
        body = _handler(self.text, "wp_ajax_onionpress_poll_action")
        self.assertIn("check_ajax_referer( 'onionpress_poll_action' )", body)
        self.assertLess(body.index("onionpress_settings_owner_allowed()"),
                        body.index("check_ajax_referer"))
        self.assertLess(body.index("check_ajax_referer"), body.index("$_GET["))

    def test_check_update_checks_its_nonce_before_any_request(self):
        body = _handler(self.text, "wp_ajax_onionpress_check_update")
        self.assertIn("check_ajax_referer( 'onionpress_check_update' )", body)
        self.assertLess(body.index("onionpress_settings_owner_allowed()"),
                        body.index("check_ajax_referer"))
        self.assertLess(body.index("check_ajax_referer"), body.index("status.json"))
        # No request of its own any more (issue #528): status.json's release.
        self.assertNotIn("onionpress_curl_tor", body)

    def test_the_page_script_sends_each_nonce(self):
        self.assertIn("wp_create_nonce( 'onionpress_poll_action' )", self.text)
        self.assertRegex(self.text, r"action=onionpress_poll_action[^\n]*\n(?:[^\n]*\n){0,4}"
                                    r"[^\n]*_wpnonce=' \+ encodeURIComponent\(pollNonce\)")
        self.assertIn("wp_create_nonce( 'onionpress_check_update' )", self.text)
        self.assertIn("action=onionpress_check_update&_wpnonce=' + encodeURIComponent(checkNonce)",
                      self.text)

    def test_every_owner_ajax_handler_has_a_nonce(self):
        # The census: a new wp_ajax_ handler here gets a check_ajax_referer.
        for hook in re.findall(r"add_action\( '(wp_ajax_[a-z_]+)'", self.text):
            with self.subTest(hook=hook):
                self.assertIn("check_ajax_referer(", _handler(self.text, hook))


class TestStatusRefresh(unittest.TestCase):

    def setUp(self):
        self.text = _read(STATUS)
        start = self.text.index("if ( $path !== 'onionpress-status-json' )")
        self.endpoint = self.text[start:self.text.index("\n} );", start)]

    def test_the_endpoint_writes_nothing(self):
        """Issue #528: the daemon rewrites status.json every pass, so the
        endpoint asks the host for nothing; the nonce that guarded that
        write (#402) went with it."""
        self.assertNotIn("file_put_contents", self.endpoint)
        self.assertNotIn("onionpress_status_refresh", self.text)
        self.assertIn("fetch('/onionpress-status-json')", self.text)

    def test_the_viewer_check_comes_first(self):
        self.assertLess(self.endpoint.index("onionpress_status_viewer_allowed()"),
                        self.endpoint.index("file_get_contents"))


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""A follow never sends a request into the container network.

Following is per-site, so every subsite administrator has it. Whatever they
typed was fetched from inside the WordPress container: the follow probe
(wp_remote_get on a feed URL), feed discovery (wp_remote_head on
http://<key>/feed/, for any key that was not a URL) and the aggregator's
fetch_feed(). onionpress-tor-proxy sends requests through Tor except to the
hosts onionpress_is_local_host() names (wordpress, db, onionpress-tor,
onionheaven, localhost, 127.x, ::1), which it connects to directly. So a
follow of http://onionpress-tor:8083/... (or a bare key such as
"onionheaven:8083/x", discovered as http://onionheaven:8083/x/feed/) made
WordPress call the hub API, the healthcheck and redirect services, or probe
MariaDB's port, and the probe's verdict, the feed title and items told the
administrator what came back.

Now a follow request goes to a .onion or a public DNS name only, checked on
the first URL (onionpress_follow_url_allowed), on every request a fetch makes
(SimplePie follows <link rel="alternate">) and on every redirect. Feed
discovery runs only for a v3 .onion address, a follow stored before the rule
is skipped and its title dropped, and the follow form refuses the rest.

The same guard turns off WordPress's own safe-URL check for the URLs it
accepts: fetch_feed() asks for that check, which resolves a non-.onion host
with gethostbyname() inside the container, a DNS query outside Tor naming
the followed site whenever a visitor's page view refreshes the blogroll.

There is no PHP in the test environment, so these read the source, and run
the host rule's regular expressions in Python.
"""

import os
import re
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PLUGINS = os.path.join(ROOT, "app", "Resources", "plugins")
BLOGROLL = os.path.join(PLUGINS, "onionpress-blogroll.php")
SETTINGS = os.path.join(PLUGINS, "onionpress-settings.php")
TOR_PROXY = os.path.join(PLUGINS, "onionpress-tor-proxy.php")

ONION = "op2homeiwjb4fdqnfkj5kbokvcee45zpk2pwgvpz5rrkanp5qqwxzbyd.onion"


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _function(src, name):
    """A top-level PHP function, from its signature to the closing brace at
    column 0 (or at the indentation of an `if ( ! function_exists )` wrap)."""
    m = re.search(r"^( *)function " + re.escape(name) + r"\(", src, re.M)
    assert m, f"no function {name}"
    end = src.index("\n" + m.group(1) + "}\n", m.end())
    return src[m.start():end + len(m.group(1)) + 2]


def _code(src):
    """PHP source without its docblocks and comment lines."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(line for line in src.splitlines()
                     if not line.lstrip().startswith("//"))


def _php_regex(literal):
    """A single-quoted PHP '/.../D' pattern as a Python regex. D makes $ the
    very end of the subject, which is Python's \\Z, not its $."""
    m = re.fullmatch(r"/(.*)/([a-zA-Z]*)", literal, re.S)
    assert m, literal
    pattern, flags = m.group(1), m.group(2)
    assert "D" in flags, f"{literal} lets $ match before a trailing newline"
    assert pattern.endswith("$"), literal
    pattern = pattern[:-1] + r"\Z"
    # Single-quoted PHP: \\ is one backslash.
    pattern = pattern.replace("\\\\", "\\")
    return re.compile(pattern, re.I if "i" in flags else 0)


def _local_hosts():
    """onionpress-tor-proxy's direct-connection list, and its 127.x rule."""
    fn = _function(_read(TOR_PROXY), "onionpress_is_local_host")
    listed = re.search(r"\$local = array\((.*?)\);", fn, re.S).group(1)
    names = re.findall(r"'([^']+)'", listed)
    assert "preg_match( '/^127\\./', $host )" in fn
    return names


class FollowHostRule:
    """onionpress_follow_host_allowed(), with its regexes read from the
    plugin and onionpress_is_local_host() emulated from onionpress-tor-proxy."""

    def __init__(self):
        fn = _function(_read(BLOGROLL), "onionpress_follow_host_allowed")
        patterns = re.findall(r"preg_match\( '([^']+)', \$host \)", fn)
        assert len(patterns) == 2, patterns
        self.shape = _php_regex(patterns[0])
        self.reserved = _php_regex(patterns[1])
        self.local = set(_local_hosts())

    def is_local(self, host):
        return host in self.local or host.startswith("127.")

    def allowed(self, host):
        if not host or len(host) > 253:
            return False
        host = host.lower()
        if not self.shape.search(host):
            return False
        if self.reserved.search(host):
            return False
        return not self.is_local(host)


class TestHostRule(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.rule = FollowHostRule()

    def test_onion_and_public_names_are_allowed(self):
        for host in (ONION, "www." + ONION, ONION.upper(), "example.com",
                     "blog.example.co.uk", "a-b.example.org",
                     "xn--bcher-kva.example", "example.xn--p1ai"):
            with self.subTest(host=host):
                self.assertTrue(self.rule.allowed(host))

    def test_container_names_and_localhost_are_refused(self):
        for host in _local_hosts() + ["localhost.", "onionheaven.", "wordpress.",
                                      "foo.localhost", "printer.local",
                                      "db.internal", "box.localdomain",
                                      "router.home.arpa", "mariadb", "autoheal"]:
            with self.subTest(host=host):
                self.assertFalse(self.rule.allowed(host))

    def test_every_direct_host_is_refused_by_shape_alone(self):
        # Not only because the rule also asks onionpress_is_local_host().
        for host in _local_hosts():
            with self.subTest(host=host):
                self.assertIsNone(self.rule.shape.search(host.lower()))

    def test_ip_literals_are_refused_in_every_spelling(self):
        for host in ("127.0.0.1", "127.1", "10.0.0.1", "172.17.0.2",
                     "192.168.1.1", "169.254.169.254", "0.0.0.0", "0",
                     "2130706433", "0x7f000001", "017700000001",
                     "0177.0.0.1", "0x7f.0.0.1", "0x7f.0x0.0x0.0x1",
                     "1.2.3.4", "8.8.8.8", "[::1]", "::1",
                     "[::ffff:127.0.0.1]", "[fe80::1%25en0]", "[::]"):
            with self.subTest(host=host):
                self.assertFalse(self.rule.allowed(host))

    def test_malformed_names_are_refused(self):
        for host in ("", "example.com.", ".example.com", "a..example.com",
                     "-example.com", "example-.com", "exa_mple.com",
                     "exa mple.com", "example.com\n", "example.com\x00",
                     "example.c", "example.123", "example.x1",
                     "a" * 64 + ".com", "user@example.com",
                     "example.com:80", "bücher.example"):
            with self.subTest(host=host):
                self.assertFalse(self.rule.allowed(host))

    def test_rule_asks_the_tor_proxy_and_fails_closed_without_it(self):
        fn = _function(_read(BLOGROLL), "onionpress_follow_host_allowed")
        self.assertIn(
            "return function_exists( 'onionpress_is_local_host' ) "
            "&& ! onionpress_is_local_host( $host );", fn)
        self.assertNotIn("gethostbyname", fn)
        self.assertNotIn("dns_get_record", fn)

    def test_tor_proxy_decides_direct_or_tor_with_the_same_function(self):
        """The follow rule refuses what onionpress_is_local_host() calls
        local; the proxy's two direct-connection decisions must be that call."""
        php = _read(TOR_PROXY)
        curl = php[php.index("add_action( 'http_api_curl', function"):]
        self.assertTrue(curl.startswith(
            "add_action( 'http_api_curl', function ( $handle, $parsed_args = array(), $url = '' ) {\n"
            "    $host = strtolower( (string) parse_url( $url, PHP_URL_HOST ) );\n"
            "    if ( onionpress_is_local_host( $host ) ) {\n"
            "        return;\n"), curl[:300])
        pre = php[php.index("add_filter( 'pre_http_request', function"):]
        self.assertTrue(pre.startswith(
            "add_filter( 'pre_http_request', function ( $pre, $args, $url ) {\n"
            "    if ( false !== $pre ) {\n"
            "        return $pre;   // already answered (onionpress_tor_proxy_offline): no probe\n"
            "    }\n"
            "    $host = strtolower( (string) parse_url( $url, PHP_URL_HOST ) );\n"
            "    if ( '' === $host || onionpress_is_local_host( $host ) ) {\n"
            "        return $pre;\n"), pre[:300])
        self.assertEqual(php.count("add_action( 'http_api_curl'"), 1)
        self.assertEqual(php.count("add_filter( 'pre_http_request', function"), 1)


class TestUrlRule(unittest.TestCase):

    def setUp(self):
        self.fn = _function(_read(BLOGROLL), "onionpress_follow_url_allowed")

    def test_checks_in_order(self):
        steps = ["! is_string( $url ) || strlen( $url ) > 2048",
                 "preg_match( '/[\\x00-\\x20\\x7f\\\\\\\\]/', $url )",
                 "! preg_match( '#^https?://#i', $url )",
                 "$parts = wp_parse_url( $url );",
                 "isset( $parts['user'] ) || isset( $parts['pass'] )",
                 "return onionpress_follow_host_allowed( $parts['host'] );"]
        at = [self.fn.index(s) for s in steps]
        self.assertEqual(at, sorted(at))

    def test_whitespace_controls_and_backslashes_are_refused(self):
        literal = re.search(r"preg_match\( '(/\[.*?\]/)', \$url \)", self.fn).group(1)
        bad = re.compile(literal[1:-1].replace("\\\\", "\\"))
        for url in ("http://example.com/a b", "http://example.com/\t",
                    "http://example.com\\@onionheaven/", "http://example.com/\x7f",
                    "http://onionheaven\r\n.example.com/"):
            with self.subTest(url=url):
                self.assertIsNotNone(bad.search(url))
        self.assertIsNone(bad.search(f"http://{ONION}/onionhome/feed/?a=1&b=%20"))


class TestEveryFollowRequestIsChecked(unittest.TestCase):

    def setUp(self):
        self.blogroll = _read(BLOGROLL)
        self.settings = _read(SETTINGS)

    def test_settings_makes_no_raw_requests(self):
        for call in ("wp_remote_get(", "wp_remote_head(", "wp_remote_request(",
                     "wp_safe_remote_", "fetch_feed(", "download_url("):
            with self.subTest(call=call):
                self.assertNotIn(call, _code(self.settings))
        self.assertEqual(self.settings.count("onionpress_follow_remote_get( $probe_url,"), 2)

    def test_blogroll_requests_only_inside_the_wrappers(self):
        for call, wrapper in (("wp_remote_get(", "onionpress_follow_remote_get"),
                              ("wp_remote_head(", "onionpress_follow_remote_head"),
                              ("fetch_feed(", "onionpress_follow_fetch_feed")):
            with self.subTest(call=call):
                self.assertEqual(len(re.findall(r"(?<![\w_])" + re.escape(call),
                                                _code(self.blogroll))), 1)
                body = _function(self.blogroll, wrapper)
                check = body.index("if ( ! onionpress_follow_url_allowed( $url ) )")
                self.assertIn("return onionpress_follow_refused();", body[check:check + 120])
                guarded = body.index("return onionpress_follow_guarded( function () use ( $url")
                self.assertLess(check, guarded)
                raw = re.search(r"(?<![\w_])" + re.escape(call), body)
                self.assertIsNotNone(raw)
                self.assertLess(guarded, raw.start())
        for raw in ("wp_remote_request(", "wp_safe_remote_", "download_url(", "curl_init("):
            with self.subTest(raw=raw):
                self.assertNotIn(raw, _code(self.blogroll))

    def test_guard_checks_every_request_and_every_redirect(self):
        guarded = _function(self.blogroll, "onionpress_follow_guarded")
        for line in (
            "add_filter( 'http_request_args', 'onionpress_follow_guard_args', PHP_INT_MAX, 2 );",
            "remove_filter( 'http_request_args', 'onionpress_follow_guard_args', PHP_INT_MAX );",
            "add_filter( 'pre_http_request', 'onionpress_follow_guard_request', PHP_INT_MAX, 3 );",
            "add_action( 'requests-requests.before_request', 'onionpress_follow_guard_hops', 10, 5 );",
            "remove_filter( 'pre_http_request', 'onionpress_follow_guard_request', PHP_INT_MAX );",
            "remove_action( 'requests-requests.before_request', 'onionpress_follow_guard_hops', 10 );",
        ):
            with self.subTest(line=line):
                self.assertIn(line, guarded)
        self.assertLess(guarded.index("try {"), guarded.index("} finally {"))
        self.assertLess(guarded.index("} finally {"), guarded.index("remove_filter("))

        request = _function(self.blogroll, "onionpress_follow_guard_request")
        self.assertIn("if ( onionpress_follow_url_allowed( $url ) )", request)
        self.assertIn("return onionpress_follow_refused();", request)

        hops = _function(self.blogroll, "onionpress_follow_guard_hops")
        self.assertIn("$options['hooks']->register( 'requests.before_redirect', "
                      "'onionpress_follow_guard_redirect' );", hops)

        args = _function(self.blogroll, "onionpress_follow_guard_args")
        allowed = args.index("if ( onionpress_follow_url_allowed( $url ) ) {")
        self.assertIn("$args['reject_unsafe_urls'] = false;", args[allowed:allowed + 100])
        self.assertEqual(args.count("reject_unsafe_urls"), 1)
        # onionpress-tor-proxy's own lookup-free check gives way to the
        # follow rule (tests/test_no_clearnet_dns.py covers that check).
        self.assertIn("unset( $args['onionpress_check_url'] );", args[allowed:allowed + 150])

        redirect = _function(self.blogroll, "onionpress_follow_guard_redirect")
        self.assertIn("if ( onionpress_follow_url_allowed( $location ) )", redirect)
        # The class WP_Http::request() catches and turns into a WP_Error.
        self.assertIn("class_exists( 'WpOrg\\Requests\\Exception' )", redirect)
        self.assertIn("throw new $exception(", redirect)

    def test_discovery_probes_only_an_onion_address(self):
        fn = _function(self.blogroll, "onionpress_discover_feed_url")
        check = fn.index("if ( ! preg_match( '/^[a-z2-7]{56}\\.onion$/D', $addr )")
        self.assertIn("! preg_match( '/^[a-z0-9][a-z0-9._-]*$/D', $onionname )", fn)
        self.assertIn("return '';", fn[check:check + 200])
        self.assertLess(check, fn.index("$candidates = array();"))
        self.assertIn("onionpress_follow_remote_head( $url,", fn)

    def test_aggregator_skips_refused_follows_and_drops_their_titles(self):
        # The background refresh (tests/test_blogroll_refresh.py) does the
        # fetching; a page view only reads what it stored.
        fn = _function(self.blogroll, "onionpress_blogroll_fetch_due")
        skip = fn.index("if ( 'url' === $p['type'] && ! onionpress_follow_url_allowed( $p['url'] ) )")
        block = fn[skip:fn.index("continue;", skip)]
        self.assertIn("$refused[] = $addr;", block)
        self.assertLess(skip, fn.index("onionpress_discover_feed_url("))
        self.assertLess(skip, fn.index("onionpress_follow_fetch_feed( $feed_url,"))
        store = _function(self.blogroll, "onionpress_blogroll_store")
        drop = store.index("foreach ( $refused as $addr ) {")
        self.assertIn("unset( $map[ $addr ] );", store[drop:drop + 80])
        # Feeds, titles and items go; only the stats stay.
        self.assertIn("if ( 'stats' !== $field ) {", store[drop - 60:drop])
        for option in ("onionpress_following_feeds", "onionpress_following_titles",
                       "onionpress_following_items"):
            self.assertIn(f"=> '{option}',", store)
        serve = _function(self.blogroll, "onionpress_get_aggregated_feed_items")
        for call in ("onionpress_discover_feed_url(", "onionpress_follow_fetch_feed(",
                     "onionpress_blogroll_refresh(", "onionpress_blogroll_fetch_due("):
            self.assertNotIn(call, serve)


class TestFollowForm(unittest.TestCase):

    def setUp(self):
        php = _read(SETTINGS)
        start = php.index("if ( ! empty( $_POST['onionpress_follow_address'] ) ) {")
        self.follow = php[start:php.index("wp_safe_redirect(", start)]

    def test_url_follow_is_checked_before_it_is_stored(self):
        check = self.follow.index("$addr = onionpress_follow_url_allowed( $raw ) ? $raw : '';")
        self.assertIn("$feed_url = $addr;", self.follow[check:check + 120])
        self.assertLess(check, self.follow.index("update_option( 'onionpress_following'"))

    def test_directory_answer_must_be_an_onion_address(self):
        lookup = self.follow.index("onionpress_directory_lookup( $raw_lower )")
        check = self.follow.index("if ( preg_match( '/^[a-z2-7]{56}\\.onion$/D', $addr ) ) {", lookup)
        self.assertIn("$resolved_name = $raw_lower;", self.follow[check:check + 120])

    def test_anything_else_must_be_onionname_shaped(self):
        rule = self.follow.index("! preg_match( '/^[a-z0-9][a-z0-9._-]*$/D', $addr )")
        self.assertIn("$addr = '';", self.follow[rule:rule + 80])
        refused = self.follow.index("array( 'label' => $raw, 'refused' => true )")
        self.assertLess(rule, refused)
        self.assertLess(refused, self.follow.index("update_option( 'onionpress_following'"))

    def test_refusal_is_shown_escaped(self):
        php = _read(SETTINGS)
        notice = php[php.index("if ( ! empty( $probe['refused'] ) ) {"):]
        notice = notice[:notice.index("} elseif")]
        self.assertIn("esc_html( $label )", notice)


class TestFollowingNamesRetired(unittest.TestCase):
    """onionpress_following_names was a side map of onionname per follow.
    Since the schema-2 keys (<addr>.onion/<name>) carry the name, only the
    pre-v2 migration reads it, which then deletes it (issue #321)."""

    def test_only_the_migration_touches_it(self):
        for dirpath, _, files in os.walk(os.path.join(ROOT, "app", "Resources")):
            for name in files:
                if not name.endswith(".php"):
                    continue
                path = os.path.join(dirpath, name)
                if path == BLOGROLL:
                    continue
                with self.subTest(os.path.relpath(path, ROOT)):
                    self.assertNotIn("onionpress_following_names", _read(path))
        migrate = _function(_read(BLOGROLL), "onionpress_follow_migrate")
        rest = _read(BLOGROLL).replace(migrate, "")
        self.assertNotIn("onionpress_following_names", rest)
        self.assertNotRegex(migrate, r"update_option\(\s*'onionpress_following_names'")
        self.assertIn("delete_option( 'onionpress_following_names' )", migrate)


if __name__ == "__main__":
    unittest.main()

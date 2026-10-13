#!/usr/bin/env python3
"""WordPress never looks a name up outside Docker.

onionpress-tor-proxy sends the WordPress HTTP API's requests through Tor with
proxy-side name resolution (socks5h), so the connection never needs a DNS
answer. But WordPress's wp_http_validate_url(), which runs on every request
that sets reject_unsafe_urls (the wp_safe_remote_*() functions behind oEmbed,
pingbacks, enclosures, fetch_feed(), download_url() and the editor's link
previews), resolves the host with gethostbyname() in the container first. The
container's resolver is Docker's, which forwards every name that is not a
container to the host's resolvers: a clearnet DNS query naming what the site
is about to fetch, from the publisher's network, before the request goes
through Tor. The iptables clearnet log need not see it: current Docker sends
those forwarded queries from the Docker host's (on macOS, the Colima VM's)
own network namespace, not the container's.

Now, in layers:

  * onionpress-tor-proxy turns that check off for every host it sends through
    Tor and applies the same rules by the URL's shape, with no lookup
    (onionpress_remote_url_is_safe); every redirect stays on the route its
    request took, so a container-network request cannot be redirected to an
    outside host and connect there directly.
  * docker-compose.yml gives wordpress and db `dns: 127.0.0.1`. Docker's
    resolver still answers for the containers and forwards any other name to
    the container's own loopback, where nothing listens: whatever still
    calls the resolver (wp-cli's own HTTP client, a plugin's raw lookup)
    fails closed instead of leaking.
  * Raw curl in OnionPress's own plugins always sets a socks5h proxy, or
    talks to a container.

php is preinstalled on ubuntu-24.04, so the PHP half runs in CI; locally it
is skipped when php is missing. The harness runs the plugin against a small
emulation of WP_Http::request() and wp_http_validate_url(), with PHP's own
lookup functions disabled and replaced by recorders.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PLUGINS = os.path.join(ROOT, "app", "Resources", "plugins")
TOR_PROXY = os.path.join(PLUGINS, "onionpress-tor-proxy.php")
BLOGROLL = os.path.join(PLUGINS, "onionpress-blogroll.php")
DOCKER = os.path.join(ROOT, "app", "Resources", "docker")
COMPOSE = os.path.join(DOCKER, "docker-compose.yml")
CLOUDFLARE_COMPOSE = os.path.join(DOCKER, "docker-compose.cloudflare.yml")
AUDIT = os.path.join(DOCKER, "wordpress", "onionpress-security-audit.sh")
PHP = shutil.which("php")

ONION = "op2homeiwjb4fdqnfkj5kbokvcee45zpk2pwgvpz5rrkanp5qqwxzbyd.onion"
OTHER_ONION = "archivep75mbjunhxc6x4j5mwjmomyxb573v42baldlqu56ruil2oiad.onion"

# PHP's name-lookup functions. The harness disables them and puts recorders
# in their place; the census below keeps them out of the plugins.
LOOKUPS = ("gethostbyname", "gethostbynamel", "gethostbyaddr", "dns_get_record",
           "checkdnsrr", "dns_check_record", "getmxrr", "dns_get_mx")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _code(src):
    """PHP source without its docblocks and comment lines."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(line for line in src.splitlines()
                     if not line.lstrip().startswith(("//", "#", "*")))


def _function(src, name):
    """A top-level PHP function, from its signature to its closing brace."""
    m = re.search(r"^( *)function " + re.escape(name) + r"\(", src, re.M)
    assert m, f"no function {name}"
    end = src.index("\n" + m.group(1) + "}\n", m.end())
    return src[m.start():end + len(m.group(1)) + 2]


def _services(text):
    """{service: block text} for a compose file, comment lines dropped."""
    services, current, in_services = {}, None, False
    for line in text.splitlines():
        if line.lstrip().startswith("#") or not line.strip():
            continue
        if not line.startswith(" "):
            in_services = line.startswith("services:")
            current = None
            continue
        if not in_services:
            continue
        m = re.match(r"^  ([A-Za-z0-9_.-]+):\s*$", line)
        if m:
            current = m.group(1)
            services[current] = []
        elif current:
            services[current].append(line)
    return {name: "\n".join(lines) for name, lines in services.items()}


def _list_items(block, key):
    """The `- item` lines under `key:` in a service block."""
    m = re.search(rf"^    {key}:\s*\n((?:      - .*\n?)+)", block + "\n", re.M)
    if not m:
        return None
    return [re.sub(r"^\s*-\s*", "", line).strip().strip("'\"")
            for line in m.group(1).splitlines()]


class TestComposeKeepsDnsInsideDocker(unittest.TestCase):
    """wordpress and db: Docker's resolver for the containers, nothing else."""

    def setUp(self):
        self.services = _services(_read(COMPOSE))

    def test_wordpress_and_db_forward_to_their_own_loopback(self):
        for name in ("wordpress", "db"):
            with self.subTest(service=name):
                self.assertEqual(_list_items(self.services[name], "dns"), ["127.0.0.1"])

    def test_on_the_compose_network_where_dockers_resolver_answers(self):
        """The loopback upstream only means "the container's own loopback"
        behind Docker's embedded resolver, which a user-defined network has.
        On the default bridge or the host's network it would be the host's
        resolver, or nothing for the container names."""
        for name in ("wordpress", "db"):
            with self.subTest(service=name):
                block = self.services[name]
                self.assertEqual(_list_items(block, "networks"), ["onionpress-network"])
                self.assertNotIn("network_mode", block)
                self.assertNotIn("dns_search", block)
        self.assertIn("networks:\n  onionpress-network:\n    name: onionpress-network\n"
                      "    driver: bridge", _read(COMPOSE))

    def test_the_cloudflare_override_leaves_them_alone(self):
        override = _services(_read(CLOUDFLARE_COMPOSE))
        self.assertEqual(set(override), {"cloudflared"})


class TestTorProxySource(unittest.TestCase):

    def setUp(self):
        self.src = _read(TOR_PROXY)

    def test_check_is_replaced_last_and_for_every_tor_host(self):
        self.assertIn("add_filter( 'http_request_args', 'onionpress_tor_proxy_request_args', "
                      "PHP_INT_MAX, 2 );", self.src)
        self.assertIn("add_filter( 'pre_http_request', 'onionpress_tor_proxy_check_url', 10, 3 );",
                      self.src)
        self.assertIn("add_action( 'requests-requests.before_request', "
                      "'onionpress_tor_proxy_hops', 10, 5 );", self.src)
        args = _function(self.src, "onionpress_tor_proxy_request_args")
        self.assertIn("if ( '' === $host || onionpress_is_local_host( $host ) "
                      "|| empty( $args['reject_unsafe_urls'] ) ) {", args)
        self.assertIn("$args['reject_unsafe_urls'] = false;", args)
        # Nothing in the plugin asks WordPress for its lookup.
        self.assertEqual(_code(self.src).count("'reject_unsafe_urls'] = true"), 0)
        hops = _function(self.src, "onionpress_tor_proxy_hops")
        self.assertIn("$options['hooks']->register( 'requests.before_redirect', "
                      "'onionpress_tor_proxy_redirect' );", hops)
        redirect = _function(self.src, "onionpress_tor_proxy_redirect")
        self.assertIn("class_exists( 'WpOrg\\Requests\\Exception' )", redirect)

    def test_the_follow_rule_replaces_the_check_for_a_follow(self):
        args = _function(_read(BLOGROLL), "onionpress_follow_guard_args")
        self.assertIn("unset( $args['onionpress_check_url'] );", args)


class TestPluginCensus(unittest.TestCase):
    """OnionPress's own plugins: no lookups, and raw curl only via socks5h."""

    def _plugin_files(self):
        for dirpath, _dirs, files in os.walk(PLUGINS):
            for name in files:
                if name.endswith(".php"):
                    yield os.path.join(dirpath, name)

    def test_no_name_lookups(self):
        """None at all. (Auto-login resolved onionpress-tor, which Docker's
        resolver answers; it now reads the gateway from /proc/net/route.)"""
        found = set()
        for path in self._plugin_files():
            code = _code(_read(path))
            for fn in LOOKUPS:
                for m in re.finditer(r"\b" + fn + r"\s*\(\s*[^)]*\)", code):
                    found.add((os.path.basename(path), m.group(0)))
        self.assertEqual(found, set())

    def test_sockets_only_to_the_tor_containers(self):
        for path in self._plugin_files():
            code = _code(_read(path))
            for m in re.finditer(r"\b(p?fsockopen|stream_socket_client|socket_connect)\s*\(([^,]*),", code):
                with self.subTest(file=os.path.basename(path), call=m.group(0)):
                    self.assertEqual(os.path.basename(path), "onionpress-tor-proxy.php")
                    self.assertEqual(m.group(2).strip(), "$candidate")
        candidates = _function(_read(TOR_PROXY), "onionpress_socks_proxy_for")
        self.assertIn("? array( 'onionheaven', 'onionpress-tor' )\n"
                      "        : array( 'onionpress-tor', 'onionheaven' );", candidates)

    def test_no_remote_file_wrappers(self):
        for path in self._plugin_files():
            code = _code(_read(path))
            for fn in ("file_get_contents", "fopen", "readfile", "file", "copy",
                       "simplexml_load_file", "getimagesize"):
                with self.subTest(file=os.path.basename(path), fn=fn):
                    self.assertIsNone(
                        re.search(r"\b" + fn + r"\s*\(\s*['\"](?:https?|ftp)://", code))

    def test_raw_curl_goes_through_a_socks5h_proxy_or_stays_in_docker(self):
        """curl_init() bypasses the WordPress HTTP API and so the tor proxy.
        Each function that calls it must set a socks5h proxy itself (the
        hostname then goes to Tor, not to a resolver), use a helper that
        does, or talk only to a container."""
        local_only = {
            # http://onionpress-tor:8083, no redirects followed.
            "onionpress_namesync_call": "ONIONPRESS_NAMESYNC_ENDPOINT",
        }
        helpers = ("onionpress_wayback_curl_common( $ch );",)
        seen = set()
        for path in self._plugin_files():
            src = _read(path)
            for m in re.finditer(r"^( *)function (\w+)\(", src, re.M):
                body = _code(_function(src, m.group(2)))
                if "curl_init(" not in body:
                    continue
                name = m.group(2)
                seen.add(name)
                with self.subTest(function=name):
                    if name in local_only:
                        self.assertIn(local_only[name], body)
                        self.assertNotIn("CURLOPT_FOLLOWLOCATION", body)
                        continue
                    self.assertTrue(
                        "CURLPROXY_SOCKS5_HOSTNAME" in body or "'socks5h://" in body
                        or any(h in body for h in helpers), body[:400])
                    self.assertNotIn("CURLPROXY_SOCKS5 ", body)
                    self.assertNotIn("CURLPROXY_SOCKS5,", body)
        self.assertIn("onionpress_namesync_call", seen)
        namesync = _read(os.path.join(PLUGINS, "onionpress-name-sync.php"))
        self.assertIn("define( 'ONIONPRESS_NAMESYNC_ENDPOINT', 'http://onionpress-tor:8083' );",
                      namesync)
        common = _function(_read(os.path.join(PLUGINS, "onionpress-wayback-archive.php")),
                           "onionpress_wayback_curl_common")
        self.assertIn("CURLOPT_PROXY             => 'socks5h://onionheaven:9050',", common)
        # The SOCKS proxy constants are container names with no scheme, so
        # CURLPROXY_SOCKS5_HOSTNAME (beside them) decides how names resolve.
        for path in self._plugin_files():
            for const, value in re.findall(r"^const (ONIONPRESS_\w+_SOCKS_PROXY)\s*=\s*'([^']+)';",
                                           _read(path), re.M):
                with self.subTest(const=const):
                    self.assertIn(value, ("onionheaven:9050", "onionpress-tor:9050"))


class TestSecurityAuditSurvivesTheDnsBlock(unittest.TestCase):

    def test_core_update_is_judged_by_the_installed_version(self):
        """wp-cli's `core update` fetches wordpress.org's checksum lists with
        its own client after the update is in place; with no DNS outside
        Docker that fails and wp-cli exits non-zero. The audit must not
        report a completed update as failed, or skip the DB migration."""
        src = _read(AUDIT)
        update = src.index('$WP core update "$zip" --version="$target" >/dev/null 2>&1 || true')
        check = src.index('if [ "$($WP core version 2>/dev/null)" = "$target" ]; then', update)
        migrate = src.index("$WP core update-db --network", check)
        self.assertLess(update, check)
        self.assertLess(check, migrate)
        self.assertNotIn('if $WP core update "$zip"', src)


HARNESS = r"""<?php
namespace WpOrg\Requests {
    class Exception extends \Exception {
        public $type;
        public function __construct( $message, $type, $data = null, $code = 0 ) {
            parent::__construct( $message, $code );
            $this->type = $type;
        }
        public function getType() { return $this->type; }
    }
}

namespace {
define( 'ABSPATH', __DIR__ . '/' );
define( 'OP_HOME', 'http://' . %(onion)s );

$GLOBALS['op_hooks'] = array();
$GLOBALS['op_dns']   = array();

function add_filter( $name, $cb, $prio = 10, $accepted = 1 ) {
    $GLOBALS['op_hooks'][ $name ][ $prio ][] = array( $cb, $accepted );
    return true;
}
function add_action( $name, $cb, $prio = 10, $accepted = 1 ) {
    return add_filter( $name, $cb, $prio, $accepted );
}
function apply_filters( $name, $value, ...$rest ) {
    if ( empty( $GLOBALS['op_hooks'][ $name ] ) ) {
        return $value;
    }
    $hooks = $GLOBALS['op_hooks'][ $name ];
    ksort( $hooks );
    foreach ( $hooks as $callbacks ) {
        foreach ( $callbacks as $h ) {
            $value = call_user_func_array( $h[0], array_slice( array_merge( array( $value ), $rest ), 0, $h[1] ) );
        }
    }
    return $value;
}
function do_action_ref_array( $name, $args ) {
    if ( empty( $GLOBALS['op_hooks'][ $name ] ) ) {
        return;
    }
    $hooks = $GLOBALS['op_hooks'][ $name ];
    ksort( $hooks );
    foreach ( $hooks as $callbacks ) {
        foreach ( $callbacks as $h ) {
            call_user_func_array( $h[0], array_slice( $args, 0, $h[1] ) );
        }
    }
}
function __( $s ) { return $s; }
function get_option( $name ) { return 'home' === $name ? OP_HOME : false; }
function wp_parse_args( $args, $defaults = array() ) { return array_merge( $defaults, (array) $args ); }
function is_wp_error( $x ) { return $x instanceof WP_Error; }
class WP_Error {
    public $code;
    public function __construct( $code = '', $message = '' ) { $this->code = $code; }
    public function get_error_code() { return $this->code; }
}
/** Enough of kses for these URLs: drop a scheme that is not allowed, lowercase one that is. */
function wp_kses_bad_protocol( $url, $allowed ) {
    if ( preg_match( '/^([a-z][a-z0-9+.-]*):/i', $url, $m ) ) {
        if ( ! in_array( strtolower( $m[1] ), $allowed, true ) ) {
            return substr( $url, strlen( $m[0] ) );
        }
        return strtolower( $m[1] ) . substr( $url, strlen( $m[1] ) );
    }
    return $url;
}
if ( ! function_exists( 'curl_init' ) ) {
    function curl_init() { return null; }
}

/* PHP's lookups are disabled (-d disable_functions); these record instead.
 * Docker's resolver answers for the containers; nothing else resolves. */
function gethostbyname( $host ) {
    $GLOBALS['op_dns'][] = $host;
    $docker = array( 'localhost' => '127.0.0.1', 'wordpress' => '172.18.0.3',
                     'db' => '172.18.0.2', 'onionpress-tor' => '172.18.0.4',
                     'onionheaven' => '172.18.0.5' );
    return isset( $docker[ $host ] ) ? $docker[ $host ] : $host;
}
function gethostbynamel( $host ) { $GLOBALS['op_dns'][] = $host; return false; }
function gethostbyaddr( $ip ) { $GLOBALS['op_dns'][] = $ip; return $ip; }
function dns_get_record( $host, ...$rest ) { $GLOBALS['op_dns'][] = $host; return false; }
function checkdnsrr( $host, $type = 'MX' ) { $GLOBALS['op_dns'][] = $host; return false; }
function getmxrr( $host, &$hosts, &$weights = null ) { $GLOBALS['op_dns'][] = $host; return false; }
/* The tor proxy's SOCKS probe: the Tor containers answer. */
function fsockopen( $host, $port = -1, &$errno = null, &$errstr = null, $timeout = null ) {
    return fopen( 'php://memory', 'r' );
}

/** wp-includes/http.php wp_http_validate_url(), as WordPress has it. */
function wp_http_validate_url( $url ) {
    if ( ! is_string( $url ) || '' === $url || is_numeric( $url ) ) {
        return false;
    }
    $original_url = $url;
    $url          = wp_kses_bad_protocol( $url, array( 'http', 'https' ) );
    if ( ! $url || strtolower( $url ) !== strtolower( $original_url ) ) {
        return false;
    }
    $parsed_url = parse_url( $url );
    if ( ! $parsed_url || empty( $parsed_url['host'] ) ) {
        return false;
    }
    if ( isset( $parsed_url['user'] ) || isset( $parsed_url['pass'] ) ) {
        return false;
    }
    if ( false !== strpbrk( $parsed_url['host'], ':#?[]' ) ) {
        return false;
    }
    $parsed_home = parse_url( get_option( 'home' ) );
    $same_host   = isset( $parsed_home['host'] ) && strtolower( $parsed_home['host'] ) === strtolower( $parsed_url['host'] );
    $host        = trim( $parsed_url['host'], '.' );
    if ( ! $same_host ) {
        if ( preg_match( '#^(([1-9]?\d|1\d\d|25[0-5]|2[0-4]\d)\.){3}([1-9]?\d|1\d\d|25[0-5]|2[0-4]\d)$#', $host ) ) {
            $ip = $host;
        } else {
            $ip = gethostbyname( $host );
            if ( $ip === $host ) {
                return false;
            }
        }
        if ( $ip ) {
            $parts = array_map( 'intval', explode( '.', $ip ) );
            if ( 127 === $parts[0] || 10 === $parts[0] || 0 === $parts[0]
                || ( 172 === $parts[0] && 16 <= $parts[1] && 31 >= $parts[1] )
                || ( 192 === $parts[0] && 168 === $parts[1] )
            ) {
                if ( ! apply_filters( 'http_request_host_is_external', false, $host, $url ) ) {
                    return false;
                }
            }
        }
    }
    if ( empty( $parsed_url['port'] ) ) {
        return $url;
    }
    $port          = $parsed_url['port'];
    $allowed_ports = apply_filters( 'http_allowed_safe_ports', array( 80, 443, 8080 ), $host, $url );
    if ( is_array( $allowed_ports ) && in_array( $port, $allowed_ports, true ) ) {
        return $url;
    }
    if ( $parsed_home && $same_host && isset( $parsed_home['port'] ) && $parsed_home['port'] === $port ) {
        return $url;
    }
    return false;
}

/** WP_Http::request(), up to the transport: the filters and the URL check, in its order. */
function op_request( $url, $args ) {
    $defaults    = array(
        'method'             => 'GET',
        'redirection'        => 5,
        'reject_unsafe_urls' => apply_filters( 'http_request_reject_unsafe_urls', false, $url ),
    );
    $parsed_args = wp_parse_args( $args, $defaults );
    $parsed_args = apply_filters( 'http_request_args', $parsed_args, $url );
    $pre         = apply_filters( 'pre_http_request', false, $parsed_args, $url );
    if ( false !== $pre ) {
        return $pre;
    }
    if ( $parsed_args['reject_unsafe_urls'] ) {
        $url = wp_http_validate_url( $url );
    }
    if ( $url ) {
        $url = wp_kses_bad_protocol( $url, array( 'http', 'https', 'ssl' ) );
    }
    if ( empty( $url ) || ! parse_url( $url, PHP_URL_SCHEME ) ) {
        return new WP_Error( 'http_request_failed', 'A valid URL was not provided.' );
    }
    return 'sent';
}

class OpHooks {
    public $registered = array();
    public function register( $hook, $callback, $priority = 0 ) {
        $this->registered[] = array( $hook, $callback );
    }
}

require %(plugin)s;

$in = json_decode( file_get_contents( 'php://stdin' ), true );
if ( ! empty( $in['external'] ) ) {
    add_filter( 'http_request_host_is_external', function () { return true; }, 10, 3 );
}
if ( ! empty( $in['ports'] ) ) {
    add_filter( 'http_allowed_safe_ports', function ( $ports ) use ( $in ) {
        return array_merge( $ports, $in['ports'] );
    }, 10, 3 );
}
$out = array();
foreach ( $in['cases'] as $case ) {
    $GLOBALS['op_dns'] = array();
    $kind = $case[0];
    if ( 'safe' === $kind || 'plain' === $kind ) {
        $r = op_request( $case[1], 'safe' === $kind ? array( 'reject_unsafe_urls' => true ) : array() );
        $result = is_wp_error( $r ) ? 'error:' . $r->get_error_code() : $r;
    } elseif ( 'args' === $kind ) {
        $a = apply_filters( 'http_request_args', array( 'reject_unsafe_urls' => $case[2] ), $case[1] );
        $result = array( 'reject' => $a['reject_unsafe_urls'], 'check' => ! empty( $a['onionpress_check_url'] ) );
    } elseif ( 'redirect' === $kind ) {
        try {
            onionpress_tor_proxy_redirect( $case[2], array(), array(), array(), (object) array( 'url' => $case[1] ) );
            $result = 'followed';
        } catch ( WpOrg\Requests\Exception $e ) {
            $result = 'refused:' . $e->getType();
        }
    } elseif ( 'hops' === $kind ) {
        $hooks = new OpHooks();
        $url = $case[1]; $headers = array(); $data = array(); $type = 'GET';
        $options = array( 'hooks' => $hooks );
        do_action_ref_array( 'requests-requests.before_request', array( &$url, &$headers, &$data, &$type, &$options ) );
        $result = $hooks->registered;
    }
    $out[] = array( 'result' => $result, 'dns' => $GLOBALS['op_dns'] );
}
echo json_encode( $out );
}
"""


@unittest.skipUnless(PHP, "php not installed")
class TestTorProxyUnderPhp(unittest.TestCase):

    def _run(self, cases, external=False, ports=()):
        tmp = tempfile.mkdtemp(prefix="onionpress-no-clearnet-dns-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        harness = os.path.join(tmp, "harness.php")
        with open(harness, "w") as f:
            f.write(HARNESS % {"onion": json.dumps(ONION), "plugin": json.dumps(TOR_PROXY)})
        proc = subprocess.run(
            [PHP, "-d", "error_reporting=E_ALL", "-d", "display_errors=stderr",
             "-d", "disable_functions=" + ",".join(LOOKUPS + ("fsockopen", "pfsockopen")),
             harness],
            input=json.dumps({"cases": cases, "external": external, "ports": list(ports)}),
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "", proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(len(out), len(cases))
        return out

    def test_safe_requests_through_tor_look_nothing_up(self):
        urls = [
            "https://www.youtube.com/oembed?url=https%3A%2F%2Fyoutu.be%2Fx&format=json",
            "https://example.com/feed/",
            "https://wordpress.org/news/feed/",
            "http://planet.wordpress.org/feed/",
            "https://downloads.wordpress.org/plugin/x.zip",
            f"http://{OTHER_ONION}/feed/",
            f"http://{OTHER_ONION}:8888/feed/",   # .onion: no port rule, as before
            "https://203.0.113.7/x",
            "https://EXAMPLE.org./x",
            "HTTPS://example.net/x",
            "https://example.com:443/x",
            "http://example.com:8080/x",
            "https://xn--bcher-kva.example/",
        ]
        out = self._run([["safe", u] for u in urls])
        for url, r in zip(urls, out):
            with self.subTest(url=url):
                self.assertEqual(r, {"result": "sent", "dns": []})

    def test_unsafe_urls_are_refused_by_shape_without_a_lookup(self):
        urls = [
            "http://10.0.0.5/", "http://192.168.1.1/", "http://172.16.0.1/",
            "http://172.31.255.255/", "http://0.0.0.0/",
            "http://2130706433/", "http://0x7f000001/", "http://0177.0.0.1/",
            "http://10.1/", "http://intranet/", "http://localhost./",
            "http://foo.localhost/", "http://printer.local/", "http://x.internal/",
            "http://nas.home.arpa/", "http://db./", "http://example.123/",
            "http://user:pw@example.com/", "https://example.com:8443/",
            "http://example.com:22/", "ftp://example.com/", "http:///x",
        ]
        out = self._run([["safe", u] for u in urls])
        for url, r in zip(urls, out):
            with self.subTest(url=url):
                self.assertEqual(r, {"result": "error:http_request_failed", "dns": []})

    def test_wordpress_filters_still_decide_private_literals_and_ports(self):
        out = self._run([["safe", "http://10.0.0.5/"], ["safe", "https://example.com:8443/"],
                         ["safe", "http://0x7f000001/"]],
                        external=True, ports=[8443])
        self.assertEqual([r["result"] for r in out], ["sent", "sent", "error:http_request_failed"])
        self.assertEqual([r["dns"] for r in out], [[], [], []])

    def test_plain_requests_are_left_alone(self):
        """No reject_unsafe_urls, no check: WordPress never asked for one."""
        out = self._run([["plain", "https://example.com:8443/x"],
                         ["plain", "http://10.0.0.5/"]])
        self.assertEqual(out, [{"result": "sent", "dns": []}] * 2)

    def test_local_hosts_keep_wordpress_check(self):
        """Container names: WordPress's check, answered by Docker, refused as
        private. The only lookups left are names Docker's resolver knows."""
        out = self._run([["safe", "http://wordpress/wp-cron.php"],
                         ["safe", "http://onionpress-tor:8083/api"],
                         ["safe", "http://localhost/"],
                         ["safe", "http://127.0.0.1/"]])
        self.assertEqual([r["result"] for r in out], ["error:http_request_failed"] * 4)
        self.assertEqual([r["dns"] for r in out],
                         [["wordpress"], ["onionpress-tor"], ["localhost"], []])

    def test_request_args(self):
        out = self._run([
            ["args", "https://example.com/", True],
            ["args", "https://example.com/", False],
            ["args", f"http://{OTHER_ONION}/", True],
            ["args", "https://downloads.wordpress.org/x.zip", True],
            ["args", "http://wordpress/", True],
            ["args", "http://localhost:8080/", True],
            ["args", "not a url", True],
        ])
        self.assertEqual([r["result"] for r in out], [
            {"reject": False, "check": True},
            {"reject": False, "check": False},
            {"reject": False, "check": False},
            {"reject": False, "check": False},
            {"reject": True, "check": False},
            {"reject": True, "check": False},
            {"reject": True, "check": False},
        ])

    def test_redirects_stay_on_their_route(self):
        cases = [
            # (from, to, expected)
            ("https://example.com/a", "https://example.org/b", "followed"),
            ("https://example.com/a", f"http://{OTHER_ONION}/b", "followed"),
            (f"http://{OTHER_ONION}/a", "https://example.com/b", "followed"),
            ("https://example.com/a", "https://203.0.113.7/b", "followed"),
            ("http://wordpress/a", "http://localhost/b", "followed"),
            ("http://localhost:8080/a", "http://localhost:8080/b", "followed"),
            # Container network to outside: would connect directly.
            ("http://wordpress/a", "https://example.com/b", "refused"),
            ("http://localhost/a", f"http://{OTHER_ONION}/b", "refused"),
            ("http://127.0.0.1/a", "http://203.0.113.7/b", "refused"),
            # Tor to the container network, or to a local address.
            ("https://example.com/a", "http://onionpress-tor:8083/api", "refused"),
            ("https://example.com/a", "http://127.0.0.1/", "refused"),
            ("https://example.com/a", "http://10.0.0.5/", "refused"),
            ("https://example.com/a", "http://2130706433/", "refused"),
            ("https://example.com/a", "http://intranet/", "refused"),
            ("https://example.com/a", "http://router.local/", "refused"),
            ("https://example.com/a", "http://[::1]/", "refused"),
            # Unknown origin: refused.
            ("", "https://example.com/b", "refused"),
        ]
        out = self._run([["redirect", f, t] for f, t, _ in cases])
        for (frm, to, want), r in zip(cases, out):
            with self.subTest(frm=frm, to=to):
                expected = want if want == "followed" else "refused:onionpress_redirect_refused"
                self.assertEqual(r, {"result": expected, "dns": []})

    def test_every_request_registers_the_redirect_check(self):
        out = self._run([["hops", "https://example.com/"], ["hops", "http://wordpress/"]])
        for r in out:
            self.assertEqual(r["result"], [["requests.before_redirect", "onionpress_tor_proxy_redirect"]])


if __name__ == "__main__":
    unittest.main()

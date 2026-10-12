<?php
/**
 * OnionPress Internal Proxy
 *
 * Forwards HTTP requests through Tor's SOCKS proxy. Runs inside the
 * WordPress container on the existing Apache + PHP stack.
 *
 * Usage from Mac: GET/POST http://localhost:8080/__op_proxy.php
 *   Header: X-OnionPress-URL: http://xyz.onion/path
 *   Header: X-OnionPress-Token: the token below
 *
 * This eliminates the need for `docker exec` per request.
 */

// Who may use it. The one caller is the menubar app's onion proxy
// (onion_proxy.py), on 127.0.0.1:<wp port> on the Mac. Lima carries that
// to the VM's loopback, and Docker's published port hands it to this
// container from the onionpress-network gateway (docker-proxy dials out
// from it; without the userland proxy, NAT rewrites to it). Onion visitors
// reach this same Apache too: Tor -> socat in onionpress-tor ->
// wordpress:80, from the tor container's own address on that network, and
// Cloudflare Tunnel visitors from cloudflared's. The "172." prefix this
// used to admit let all of them in, so anyone on Tor could fetch other
// onion services through the publisher's Tor.
//
// A request now needs the token onion_proxy.install_php_proxy writes into
// the copy it installs (a new one each time the app starts; the bundled
// file holds a placeholder that never matches), and has to come from
// loopback or the gateway, an address no container on the network has.
const OP_PROXY_TOKEN = '__ONIONPRESS_PROXY_TOKEN__';

// This container's default gateway from the kernel's IPv4 routing table
// (hex in host byte order, hence pack('L')): the Docker host's address on
// onionpress-network. '' when there is none.
function op_proxy_gateway() {
    $rows = @file('/proc/net/route', FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
    foreach ($rows ?: [] as $row) {
        // Iface Destination Gateway Flags ...; RTF_UP | RTF_GATEWAY = 3
        $f = preg_split('/\s+/', trim($row));
        if (count($f) > 3 && $f[1] === '00000000' && $f[2] !== '00000000'
            && preg_match('/^[0-9A-F]{8}$/Di', $f[2])
            && preg_match('/^[0-9A-F]{1,4}$/Di', $f[3]) && (hexdec($f[3]) & 3) === 3) {
            return inet_ntop(pack('L', hexdec($f[2])));
        }
    }
    return '';
}

function op_proxy_caller_ok() {
    $token = $_SERVER['HTTP_X_ONIONPRESS_TOKEN'] ?? '';
    if (!preg_match('/^[A-Za-z0-9_-]{32,}$/D', OP_PROXY_TOKEN)
        || !is_string($token) || !hash_equals(OP_PROXY_TOKEN, $token)) {
        return false;
    }
    $remote = (string) ($_SERVER['REMOTE_ADDR'] ?? '');
    if (str_starts_with($remote, '::ffff:')) {
        $remote = substr($remote, 7);
    }
    if ($remote === '127.0.0.1' || $remote === '::1') {
        return true;
    }
    $gateway = op_proxy_gateway();
    return $gateway !== '' && $remote === $gateway;
}

if (!op_proxy_caller_ok()) {
    http_response_code(403);
    exit('Forbidden');
}

$url = $_SERVER['HTTP_X_ONIONPRESS_URL'] ?? '';
if (!$url) {
    // Status check
    if (($_SERVER['HTTP_X_ONIONPRESS_ACTION'] ?? '') === 'status') {
        header('Content-Type: application/json');
        echo json_encode(['ok' => true]);
        exit;
    }
    http_response_code(400);
    exit('Missing X-OnionPress-URL header');
}

// Only an http(s) URL whose host is a v3 .onion address (under subdomain
// labels too, as onion_proxy.py's ONION_HOST_RE). This was a ".onion"
// followed by "/", ":" or the end anywhere in the URL, which any URL that
// merely mentioned one passed, http://example.com/?.onion/ and
// file:///...#.onion/ (not a network request, so no proxy) alike.
$parts  = parse_url($url);
$scheme = is_array($parts) && isset($parts['scheme']) ? strtolower($parts['scheme']) : '';
$host   = is_array($parts) && isset($parts['host']) ? rtrim(strtolower($parts['host']), '.') : '';
if (!in_array($scheme, ['http', 'https'], true)
    || isset($parts['user']) || isset($parts['pass'])
    || !preg_match('/^(?:[a-z0-9-]+\.)*[a-z2-7]{56}\.onion$/D', $host)) {
    http_response_code(400);
    exit('Only .onion URLs allowed');
}

$ch = curl_init($url);
curl_setopt_array($ch, [
    // curl parses the URL again; whatever it makes of it, and of every
    // redirect, it may only speak HTTP(S), always through Tor.
    CURLOPT_PROTOCOLS       => CURLPROTO_HTTP | CURLPROTO_HTTPS,
    CURLOPT_REDIR_PROTOCOLS => CURLPROTO_HTTP | CURLPROTO_HTTPS,
    CURLOPT_PROXY           => 'socks5h://onionpress-tor:9050',
    CURLOPT_RETURNTRANSFER  => true,
    CURLOPT_HEADER          => true,
    CURLOPT_FOLLOWLOCATION  => true,
    CURLOPT_MAXREDIRS       => 10,
    CURLOPT_TIMEOUT         => 30,
    CURLOPT_CONNECTTIMEOUT  => 15,
    CURLOPT_SSL_VERIFYPEER  => false,  // .onion TLS is self-authenticating
]);

// Forward request method and body
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    curl_setopt($ch, CURLOPT_POST, true);
    curl_setopt($ch, CURLOPT_POSTFIELDS, file_get_contents('php://input'));
    $ct = $_SERVER['CONTENT_TYPE'] ?? 'application/x-www-form-urlencoded';
    curl_setopt($ch, CURLOPT_HTTPHEADER, ["Content-Type: $ct"]);
} elseif ($_SERVER['REQUEST_METHOD'] === 'HEAD') {
    curl_setopt($ch, CURLOPT_NOBODY, true);
}

$response = curl_exec($ch);

if ($response === false) {
    http_response_code(502);
    header('Content-Type: text/plain');
    echo 'Tor fetch failed: ' . curl_error($ch);
    curl_close($ch);
    exit;
}

$header_size = curl_getinfo($ch, CURLINFO_HEADER_SIZE);
$status = curl_getinfo($ch, CURLINFO_HTTP_CODE);
$body = substr($response, $header_size);
$headers_raw = substr($response, 0, $header_size);
curl_close($ch);

// Send status code
http_response_code($status);

// Forward selected response headers
$forward = ['content-type', 'cache-control', 'etag', 'last-modified',
            'content-disposition', 'content-encoding'];
foreach (explode("\r\n", $headers_raw) as $line) {
    if (!$line || strpos($line, ':') === false) continue;
    $name = strtolower(trim(explode(':', $line, 2)[0]));
    if (in_array($name, $forward)) {
        header($line);
    }
}

echo $body;

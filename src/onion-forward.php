<?php
/**
 * OnionPress Internal Proxy
 *
 * Forwards HTTP requests through Tor's SOCKS proxy. Runs inside the
 * WordPress container on the existing Apache + PHP stack.
 *
 * Usage from Mac: GET/POST http://localhost:8080/__op_proxy.php
 *   Header: X-OnionPress-URL: http://xyz.onion/path
 *
 * This eliminates the need for `docker exec` per request.
 */

// Only allow requests from localhost
$remote = $_SERVER['REMOTE_ADDR'] ?? '';
if ($remote !== '127.0.0.1' && $remote !== '::1' && !str_starts_with($remote, '172.')) {
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

// Only allow .onion URLs
if (!preg_match('/\.onion(\/|$|:)/', $url)) {
    http_response_code(400);
    exit('Only .onion URLs allowed');
}

$ch = curl_init($url);
curl_setopt_array($ch, [
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

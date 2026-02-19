<?php
/**
 * Plugin Name: OnionPress Cellar Registration
 * Description: Accepts registration POSTs from OnionPress instances for OnionCellar failover.
 * Version: 1.0
 * Network: true
 */

// Safety check — must be loaded by WordPress
if (!defined('ABSPATH')) {
    exit;
}

/**
 * Intercept POST /register early in the WordPress lifecycle.
 * This runs as an mu-plugin so it loads before themes and regular plugins.
 */
add_action('muplugins_loaded', 'onionpress_cellar_handle_register');

function onionpress_cellar_handle_register() {
    // Only handle POST /register
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        return;
    }

    $request_uri = strtok($_SERVER['REQUEST_URI'], '?');
    if ($request_uri !== '/register') {
        return;
    }

    // Read and validate JSON body
    $body = file_get_contents('php://input');
    $data = json_decode($body, true);

    if (!$data) {
        onionpress_cellar_respond(400, ['error' => 'Invalid JSON']);
        return;
    }

    $required = ['content_address', 'healthcheck_address', 'secret_key', 'public_key'];
    foreach ($required as $field) {
        if (empty($data[$field])) {
            onionpress_cellar_respond(400, ['error' => "Missing required field: $field"]);
            return;
        }
    }

    $content_address = $data['content_address'];
    $healthcheck_address = $data['healthcheck_address'];
    $secret_key_b64 = $data['secret_key'];
    $public_key_b64 = $data['public_key'];
    $version = isset($data['version']) ? $data['version'] : 'unknown';

    // Validate addresses look like .onion addresses
    if (!preg_match('/^[a-z2-7]{56}\.onion$/', $content_address)) {
        onionpress_cellar_respond(400, ['error' => 'Invalid content_address format']);
        return;
    }
    if (!preg_match('/^[a-z2-7]{56}\.onion$/', $healthcheck_address)) {
        onionpress_cellar_respond(400, ['error' => 'Invalid healthcheck_address format']);
        return;
    }

    // Validate base64-encoded keys
    $secret_key = base64_decode($secret_key_b64, true);
    $public_key = base64_decode($public_key_b64, true);
    if ($secret_key === false || $public_key === false) {
        onionpress_cellar_respond(400, ['error' => 'Invalid base64 key encoding']);
        return;
    }

    // Store keys on disk
    $cellar_dir = '/var/lib/onionpress/cellar';
    $keys_dir = "$cellar_dir/keys/$content_address";

    if (!is_dir($keys_dir)) {
        mkdir($keys_dir, 0700, true);
    }

    // Write the Tor key files in the expected format
    // Secret key: 32-byte header + 64-byte key
    $secret_header = "== ed25519v1-secret: type0 ==";
    $secret_header = str_pad($secret_header, 32, "\x00");
    file_put_contents("$keys_dir/hs_ed25519_secret_key", $secret_header . $secret_key);
    chmod("$keys_dir/hs_ed25519_secret_key", 0600);

    // Public key: raw bytes as received
    file_put_contents("$keys_dir/hs_ed25519_public_key", $public_key);
    chmod("$keys_dir/hs_ed25519_public_key", 0600);

    // Write hostname file
    file_put_contents("$keys_dir/hostname", $content_address . "\n");
    chmod("$keys_dir/hostname", 0600);

    // Update registry
    $registry_file = "$cellar_dir/registry.json";
    $registry = [];
    if (file_exists($registry_file)) {
        $existing = json_decode(file_get_contents($registry_file), true);
        if (is_array($existing)) {
            $registry = $existing;
        }
    }

    // Find existing entry or create new one
    $found = false;
    $now = gmdate('Y-m-d\TH:i:s\Z');
    foreach ($registry as &$entry) {
        if ($entry['content_address'] === $content_address) {
            // Update existing entry
            $entry['healthcheck_address'] = $healthcheck_address;
            $entry['registered_at'] = $now;
            $entry['version'] = $version;
            $found = true;
            break;
        }
    }
    unset($entry);

    if (!$found) {
        $registry[] = [
            'content_address' => $content_address,
            'healthcheck_address' => $healthcheck_address,
            'registered_at' => $now,
            'version' => $version,
            'status' => 'healthy',
            'last_healthcheck' => null,
            'fail_count' => 0,
            'takeover_active' => false,
        ];
    }

    file_put_contents($registry_file, json_encode($registry, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES));

    onionpress_cellar_respond(200, [
        'registered' => true,
        'content_address' => $content_address,
        'message' => $found ? 'Registration updated' : 'Registration created',
    ]);
}

/**
 * Send a JSON response and exit.
 */
function onionpress_cellar_respond($status_code, $data) {
    http_response_code($status_code);
    header('Content-Type: application/json');
    echo json_encode($data);
    exit;
}

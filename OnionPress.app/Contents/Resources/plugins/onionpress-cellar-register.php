<?php
/**
 * Plugin Name: OnionPress Cellar Registration
 * Description: Accepts registration POSTs from OnionPress instances for OnionCellar failover.
 * Version: 2.0
 * Network: true
 */

// Safety check — must be loaded by WordPress
if (!defined('ABSPATH')) {
    exit;
}

// Load crypto helper
require_once __DIR__ . '/onionpress-cellar-crypto.php';

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

    // Cellar must be unlocked to accept registrations
    if (!cellar_crypto_is_unlocked()) {
        onionpress_cellar_respond(503, ['error' => 'Cellar is locked', 'locked' => true]);
        return;
    }

    $master_key = cellar_crypto_read_unlocked_key();
    if ($master_key === false) {
        onionpress_cellar_respond(503, ['error' => 'Cellar is locked', 'locked' => true]);
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

    // Store keys on disk (encrypted)
    $cellar_dir = '/var/lib/onionpress/cellar';
    $keys_dir = "$cellar_dir/keys/$content_address";

    if (!is_dir($keys_dir)) {
        mkdir($keys_dir, 0700, true);
    }

    // Build the Tor key files in the expected format
    // Secret key: 32-byte header + 64-byte key
    $secret_header = "== ed25519v1-secret: type0 ==";
    $secret_header = str_pad($secret_header, 32, "\x00");
    $secret_full = $secret_header . $secret_key;

    // Encrypt and write key files
    $enc_secret = cellar_crypto_encrypt_key($secret_full, $master_key);
    $enc_public = cellar_crypto_encrypt_key($public_key, $master_key);

    if ($enc_secret === false || $enc_public === false) {
        onionpress_cellar_respond(500, ['error' => 'Encryption failed']);
        return;
    }

    file_put_contents("$keys_dir/hs_ed25519_secret_key.enc", $enc_secret);
    chmod("$keys_dir/hs_ed25519_secret_key.enc", 0600);

    file_put_contents("$keys_dir/hs_ed25519_public_key.enc", $enc_public);
    chmod("$keys_dir/hs_ed25519_public_key.enc", 0600);

    // Remove any plaintext key files (migration cleanup)
    @unlink("$keys_dir/hs_ed25519_secret_key");
    @unlink("$keys_dir/hs_ed25519_public_key");

    // Write hostname file (plaintext — public info)
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

// ---------------------------------------------------------------------------
// WordPress hooks for cellar unlock / key slot management
// ---------------------------------------------------------------------------

/**
 * Unlock the cellar on WordPress admin login.
 *
 * On standard form login, $_POST['pwd'] contains the plaintext password.
 * - If no master-key.json: generate master key, create first slot, write unlocked file
 * - If user has slot: derive key, decrypt master key, write unlocked file
 * - If user has no slot but cellar already unlocked: lazy enrollment
 * - After unlock: migrate any remaining plaintext keys to encrypted
 */
add_action('wp_login', 'onionpress_cellar_unlock', 10, 2);

function onionpress_cellar_unlock($user_login, $user) {
    // Only for super admins (multisite network admins)
    if (!$user->has_cap('manage_network')) {
        return;
    }

    // Need the plaintext password from the login form
    $password = isset($_POST['pwd']) ? $_POST['pwd'] : null;
    if ($password === null || $password === '') {
        return;
    }

    $slot_data = cellar_crypto_load_slots();
    $slots = &$slot_data['slots'];

    if (empty($slots)) {
        // First admin login ever — generate master key and create first slot
        $master_key = cellar_crypto_generate_master_key();
        $salt = random_bytes(16);
        $derived = cellar_crypto_derive_key($password, $salt);
        $encrypted = cellar_crypto_encrypt_slot($master_key, $derived);

        if ($encrypted === false) {
            return;
        }

        $slots[$user_login] = array_merge($encrypted, [
            'salt' => base64_encode($salt),
            'created_at' => gmdate('Y-m-d\TH:i:s\Z'),
        ]);

        cellar_crypto_save_slots($slot_data);
        cellar_crypto_write_unlocked_key($master_key);
        onionpress_cellar_migrate_plaintext_keys($master_key);
        return;
    }

    if (isset($slots[$user_login])) {
        // User has a slot — try to decrypt the master key
        $slot = $slots[$user_login];
        $salt = base64_decode($slot['salt'], true);
        if ($salt === false) {
            return;
        }

        $derived = cellar_crypto_derive_key($password, $salt);
        $master_key = cellar_crypto_decrypt_slot($slot, $derived);

        if ($master_key === false) {
            return;
        }

        cellar_crypto_write_unlocked_key($master_key);
        onionpress_cellar_migrate_plaintext_keys($master_key);
        return;
    }

    // User has no slot — lazy enrollment if cellar is already unlocked
    $master_key = cellar_crypto_read_unlocked_key();
    if ($master_key === false) {
        return;
    }

    $salt = random_bytes(16);
    $derived = cellar_crypto_derive_key($password, $salt);
    $encrypted = cellar_crypto_encrypt_slot($master_key, $derived);

    if ($encrypted === false) {
        return;
    }

    $slots[$user_login] = array_merge($encrypted, [
        'salt' => base64_encode($salt),
        'created_at' => gmdate('Y-m-d\TH:i:s\Z'),
    ]);

    cellar_crypto_save_slots($slot_data);
}

/**
 * Re-encrypt a user's key slot when their password changes.
 */
add_action('profile_update', 'onionpress_cellar_password_changed', 10, 2);

function onionpress_cellar_password_changed($user_id, $old_user_data) {
    // Only process if a new password was submitted
    $new_password = isset($_POST['pass1']) ? $_POST['pass1'] : null;
    if ($new_password === null || $new_password === '') {
        return;
    }

    // Only for super admins
    $user = get_userdata($user_id);
    if (!$user || !$user->has_cap('manage_network')) {
        return;
    }

    // Cellar must be unlocked to re-encrypt the slot
    $master_key = cellar_crypto_read_unlocked_key();
    if ($master_key === false) {
        return;
    }

    $slot_data = cellar_crypto_load_slots();
    $slots = &$slot_data['slots'];

    $salt = random_bytes(16);
    $derived = cellar_crypto_derive_key($new_password, $salt);
    $encrypted = cellar_crypto_encrypt_slot($master_key, $derived);

    if ($encrypted === false) {
        return;
    }

    $slots[$user->user_login] = array_merge($encrypted, [
        'salt' => base64_encode($salt),
        'created_at' => gmdate('Y-m-d\TH:i:s\Z'),
    ]);

    cellar_crypto_save_slots($slot_data);
}

/**
 * Remove a user's key slot when their super admin privileges are revoked.
 */
add_action('revoke_super_admin', 'onionpress_cellar_super_admin_revoked');

function onionpress_cellar_super_admin_revoked($user_id) {
    $user = get_userdata($user_id);
    if (!$user) {
        return;
    }

    $slot_data = cellar_crypto_load_slots();
    if (isset($slot_data['slots'][$user->user_login])) {
        unset($slot_data['slots'][$user->user_login]);
        cellar_crypto_save_slots($slot_data);
    }
}

/**
 * Migrate plaintext key files to encrypted format.
 * Scans cellar/keys/ subdirectories for plaintext key files without .enc companions,
 * encrypts them, and removes the plaintext originals.
 */
function onionpress_cellar_migrate_plaintext_keys($master_key) {
    $keys_base = '/var/lib/onionpress/cellar/keys';
    if (!is_dir($keys_base)) {
        return;
    }

    $dirs = scandir($keys_base);
    if ($dirs === false) {
        return;
    }

    foreach ($dirs as $dir) {
        if ($dir === '.' || $dir === '..') {
            continue;
        }

        $dir_path = "$keys_base/$dir";
        if (!is_dir($dir_path)) {
            continue;
        }

        // Check for plaintext secret key without encrypted companion
        $secret_plain = "$dir_path/hs_ed25519_secret_key";
        $secret_enc = "$dir_path/hs_ed25519_secret_key.enc";
        $public_plain = "$dir_path/hs_ed25519_public_key";
        $public_enc = "$dir_path/hs_ed25519_public_key.enc";

        if (file_exists($secret_plain) && !file_exists($secret_enc)) {
            $secret_data = file_get_contents($secret_plain);
            if ($secret_data !== false) {
                $encrypted = cellar_crypto_encrypt_key($secret_data, $master_key);
                if ($encrypted !== false) {
                    file_put_contents($secret_enc, $encrypted);
                    chmod($secret_enc, 0600);
                    unlink($secret_plain);
                }
            }
        }

        if (file_exists($public_plain) && !file_exists($public_enc)) {
            $public_data = file_get_contents($public_plain);
            if ($public_data !== false) {
                $encrypted = cellar_crypto_encrypt_key($public_data, $master_key);
                if ($encrypted !== false) {
                    file_put_contents($public_enc, $encrypted);
                    chmod($public_enc, 0600);
                    unlink($public_plain);
                }
            }
        }
    }
}

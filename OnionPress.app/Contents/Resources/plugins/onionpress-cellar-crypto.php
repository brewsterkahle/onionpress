<?php
/**
 * OnionPress Cellar Crypto Helper
 *
 * Encrypts OnionCellar keys at rest using AES-256-GCM with a master key
 * protected by LUKS-style per-admin key slots.
 *
 * Included by onionpress-cellar-register.php — not loaded standalone.
 */

if (!defined('ABSPATH')) {
    exit;
}

// Cellar data directory and file paths
define('CELLAR_CRYPTO_DIR', '/var/lib/onionpress/cellar');
define('CELLAR_MASTER_KEY_FILE', CELLAR_CRYPTO_DIR . '/master-key.json');
define('CELLAR_UNLOCKED_FILE', CELLAR_CRYPTO_DIR . '/.master-key-unlocked');

/**
 * Generate a new 256-bit master key.
 *
 * @return string 32 random bytes
 */
function cellar_crypto_generate_master_key() {
    return random_bytes(32);
}

/**
 * Derive an encryption key from a password and salt using PBKDF2.
 *
 * @param string $password  Plaintext password
 * @param string $salt      16-byte random salt
 * @return string           32-byte derived key
 */
function cellar_crypto_derive_key($password, $salt) {
    return hash_pbkdf2('sha256', $password, $salt, 600000, 32, true);
}

/**
 * Encrypt the master key for storage in a key slot.
 *
 * @param string $master_key   32-byte master key
 * @param string $derived_key  32-byte key derived from admin password
 * @return array {salt, iv, ciphertext, tag} as base64 strings
 */
function cellar_crypto_encrypt_slot($master_key, $derived_key) {
    $iv = random_bytes(12);
    $ciphertext = openssl_encrypt(
        $master_key,
        'aes-256-gcm',
        $derived_key,
        OPENSSL_RAW_DATA,
        $iv,
        $tag,
        '',
        16
    );

    if ($ciphertext === false) {
        return false;
    }

    return [
        'iv' => base64_encode($iv),
        'ciphertext' => base64_encode($ciphertext),
        'tag' => base64_encode($tag),
    ];
}

/**
 * Decrypt a master key from a key slot.
 *
 * @param array  $slot_data   {iv, ciphertext, tag} as base64 strings
 * @param string $derived_key 32-byte key derived from admin password
 * @return string|false       32-byte master key or false on failure
 */
function cellar_crypto_decrypt_slot($slot_data, $derived_key) {
    $iv = base64_decode($slot_data['iv'], true);
    $ciphertext = base64_decode($slot_data['ciphertext'], true);
    $tag = base64_decode($slot_data['tag'], true);

    if ($iv === false || $ciphertext === false || $tag === false) {
        return false;
    }

    $plaintext = openssl_decrypt(
        $ciphertext,
        'aes-256-gcm',
        $derived_key,
        OPENSSL_RAW_DATA,
        $iv,
        $tag
    );

    return $plaintext;
}

/**
 * Encrypt a key file (e.g., hs_ed25519_secret_key) with the master key.
 * Output format: [12-byte IV][16-byte tag][ciphertext]
 *
 * @param string $plaintext   Raw key file contents
 * @param string $master_key  32-byte master key
 * @return string|false       Binary encrypted data or false on failure
 */
function cellar_crypto_encrypt_key($plaintext, $master_key) {
    $iv = random_bytes(12);
    $ciphertext = openssl_encrypt(
        $plaintext,
        'aes-256-gcm',
        $master_key,
        OPENSSL_RAW_DATA,
        $iv,
        $tag,
        '',
        16
    );

    if ($ciphertext === false) {
        return false;
    }

    return $iv . $tag . $ciphertext;
}

/**
 * Decrypt a key file encrypted with cellar_crypto_encrypt_key().
 *
 * @param string $encrypted   Binary: [12-byte IV][16-byte tag][ciphertext]
 * @param string $master_key  32-byte master key
 * @return string|false       Plaintext key data or false on failure
 */
function cellar_crypto_decrypt_key($encrypted, $master_key) {
    if (strlen($encrypted) < 28) {
        return false;
    }

    $iv = substr($encrypted, 0, 12);
    $tag = substr($encrypted, 12, 16);
    $ciphertext = substr($encrypted, 28);

    return openssl_decrypt(
        $ciphertext,
        'aes-256-gcm',
        $master_key,
        OPENSSL_RAW_DATA,
        $iv,
        $tag
    );
}

/**
 * Read the unlocked master key from the shared volume.
 *
 * @return string|false 32-byte master key or false if locked
 */
function cellar_crypto_read_unlocked_key() {
    if (!file_exists(CELLAR_UNLOCKED_FILE)) {
        return false;
    }
    $key = file_get_contents(CELLAR_UNLOCKED_FILE);
    if ($key === false || strlen($key) !== 32) {
        return false;
    }
    return $key;
}

/**
 * Write the master key to the unlocked file on the shared volume.
 *
 * @param string $key 32-byte master key
 * @return bool
 */
function cellar_crypto_write_unlocked_key($key) {
    $dir = dirname(CELLAR_UNLOCKED_FILE);
    if (!is_dir($dir)) {
        mkdir($dir, 0700, true);
    }
    $result = file_put_contents(CELLAR_UNLOCKED_FILE, $key);
    if ($result !== false) {
        chmod(CELLAR_UNLOCKED_FILE, 0600);
        return true;
    }
    return false;
}

/**
 * Check if the cellar is currently unlocked.
 *
 * @return bool
 */
function cellar_crypto_is_unlocked() {
    return file_exists(CELLAR_UNLOCKED_FILE) && filesize(CELLAR_UNLOCKED_FILE) === 32;
}

/**
 * Load key slots from master-key.json.
 *
 * @return array Parsed JSON structure or default empty structure
 */
function cellar_crypto_load_slots() {
    if (!file_exists(CELLAR_MASTER_KEY_FILE)) {
        return ['version' => 1, 'slots' => []];
    }

    $data = json_decode(file_get_contents(CELLAR_MASTER_KEY_FILE), true);
    if (!is_array($data) || !isset($data['slots'])) {
        return ['version' => 1, 'slots' => []];
    }

    return $data;
}

/**
 * Save key slots to master-key.json.
 *
 * @param array $data Structure with version and slots
 * @return bool
 */
function cellar_crypto_save_slots($data) {
    $dir = dirname(CELLAR_MASTER_KEY_FILE);
    if (!is_dir($dir)) {
        mkdir($dir, 0700, true);
    }
    $json = json_encode($data, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES);
    $result = file_put_contents(CELLAR_MASTER_KEY_FILE, $json);
    if ($result !== false) {
        chmod(CELLAR_MASTER_KEY_FILE, 0600);
        return true;
    }
    return false;
}

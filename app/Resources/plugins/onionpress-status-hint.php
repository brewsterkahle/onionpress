<?php
/**
 * Plugin Name: OnionPress Status API Redirect
 * Description: Operators (and AI assistants) naturally try
 *              http://<onion>/status/<addr> on port 80 when checking
 *              OnionHeaven status, forgetting the API runs on port
 *              8083 on the canonical hub. Catch those requests and
 *              302-redirect to the canonical hub URL so the mistake
 *              is self-correcting — curl -L follows, browsers follow,
 *              and the Location header documents the right URL.
 * Version:     1.0
 * Network:     true
 * License:     AGPL-3.0-or-later
 * License URI: https://www.gnu.org/licenses/agpl-3.0.html
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

if ( ! defined( 'ONIONPRESS_ONIONHEAVEN_HUB' ) ) {
    // Canonical OnionHeaven hub — the one authoritative place to query
    // /status for any address. Any OnionPress install that gets a
    // port-80 `/status/*` request forwards here.
    define( 'ONIONPRESS_ONIONHEAVEN_HUB',
        'oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion' );
}

// Hook at init (earlier than parse_request) so HEAD requests are
// caught too — WordPress can short-circuit HEADs to a 404 before
// reaching parse_request.
add_action( 'init', function () {
    $uri  = $_SERVER['REQUEST_URI'] ?? '/';
    $path = parse_url( $uri, PHP_URL_PATH );
    if ( ! is_string( $path ) ) {
        return;
    }
    // Match /status or /status/<anything>. Leave /status-page etc. alone.
    if ( $path !== '/status' && strpos( $path, '/status/' ) !== 0 ) {
        return;
    }

    $hub    = ONIONPRESS_ONIONHEAVEN_HUB;
    $target = 'http://' . $hub . ':8083' . $path;
    // Preserve query string if any.
    $qs = $_SERVER['QUERY_STRING'] ?? '';
    if ( $qs !== '' ) {
        $target .= '?' . $qs;
    }

    nocache_headers();
    // Use header() directly rather than wp_redirect so the 302 ships
    // even for HEAD (wp_redirect wraps some method-specific logic).
    header( 'Location: ' . $target, true, 302 );
    exit;
}, 5 );

<?php
/**
 * Plugin Name: OnionPress Domain Map
 * Description: Rewrites WordPress-generated URLs so the host stored in
 *              siteurl is replaced with the hostname the visitor actually
 *              used. Lets a single WP install be served from .onion,
 *              clearnet (e.g. onionpress.org via Cloudflare tunnel), and
 *              localhost simultaneously without WP's canonical-host
 *              redirect kicking visitors over to siteurl. Also emits the
 *              Onion-Location response header on clearnet requests so
 *              Tor Browser shows the purple ".onion available" pill.
 * Version:     2.1
 * Network:     true
 * License:     AGPL-3.0-or-later
 * License URI: https://www.gnu.org/licenses/agpl-3.0.html
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

if ( empty( $_SERVER['HTTP_HOST'] ) ) {
    return;
}

/**
 * Read the stored siteurl host directly from wp_options, bypassing
 * get_option() so we don't recurse through our own option_siteurl filter.
 * Cached per-request.
 */
function onionpress_stored_host() {
    // Cache per-blog: $wpdb->options changes per switch_to_blog
    // (wp_options, wp_2_options, etc.). Without per-blog caching, the
    // first blog's siteurl host gets reused for every subsequent blog —
    // breaking My Sites links when different subsites have different
    // stored hosts (e.g. blog 1 = onion, blog 2 = localhost/<onionname>).
    static $cache = array();
    global $wpdb;
    if ( ! $wpdb ) {
        return '';
    }
    $key = $wpdb->options;
    if ( array_key_exists( $key, $cache ) ) {
        return $cache[ $key ];
    }
    $stored = $wpdb->get_var(
        "SELECT option_value FROM {$wpdb->options} WHERE option_name = 'siteurl' LIMIT 1"
    );
    if ( ! $stored ) {
        $cache[ $key ] = '';
        return '';
    }
    $h = parse_url( $stored, PHP_URL_HOST );
    $p = parse_url( $stored, PHP_URL_PORT );
    $cache[ $key ] = $h ? ( $p ? $h . ':' . $p : $h ) : '';
    return $cache[ $key ];
}

function onionpress_rewrite_url( $url ) {
    if ( ! is_string( $url ) || $url === '' ) {
        return $url;
    }
    $stored_host  = onionpress_stored_host();
    $request_host = $_SERVER['HTTP_HOST'];
    if ( ! $stored_host || $stored_host === $request_host ) {
        return $url;
    }
    return preg_replace(
        '~//' . preg_quote( $stored_host, '~' ) . '(?=[/?#]|$)~',
        '//' . $request_host,
        $url
    );
}

// Single-site options (per-blog home & siteurl).
add_filter( 'option_home',    'onionpress_rewrite_url' );
add_filter( 'option_siteurl', 'onionpress_rewrite_url' );

// Network-level URLs (admin bar, network admin links, etc.).
add_filter( 'network_home_url', 'onionpress_rewrite_url' );
add_filter( 'network_site_url', 'onionpress_rewrite_url' );

// Asset URLs (themes, plugins, wp-content, wp-includes).
add_filter( 'content_url',           'onionpress_rewrite_url' );
add_filter( 'plugins_url',           'onionpress_rewrite_url' );
add_filter( 'theme_file_uri',        'onionpress_rewrite_url' );
add_filter( 'style_loader_src',      'onionpress_rewrite_url' );
add_filter( 'script_loader_src',     'onionpress_rewrite_url' );
add_filter( 'wp_get_attachment_url', 'onionpress_rewrite_url' );
add_filter( 'includes_url',          'onionpress_rewrite_url' );

// REST API URL (required for Gutenberg block editor to save posts).
add_filter( 'rest_url', 'onionpress_rewrite_url' );

// Redirect URL used after post save, login, etc.
add_filter( 'wp_redirect', 'onionpress_rewrite_url' );

// Admin ajax URL.
add_filter( 'admin_url', 'onionpress_rewrite_url' );

// Onion-Location header: when a clearnet visitor (e.g. via Cloudflare
// tunnel) hits this site and we have a valid onion address on disk,
// advertise it so Tor Browser / Brave Tor can show the ".onion available"
// pill. Tor Browser only honors the header on HTTPS responses from a
// non-.onion host, so emitting unconditionally on every public request is
// safe — onion and direct-localhost responses just ignore it.
function onionpress_send_onion_location_header() {
    if ( is_admin() ) {
        return;
    }
    $request_host = strtolower( (string) ( $_SERVER['HTTP_HOST'] ?? '' ) );
    if ( $request_host === '' || strpos( $request_host, '.onion' ) !== false ) {
        return;
    }
    $addr_file = '/var/lib/onionpress/onion_address';
    if ( ! is_readable( $addr_file ) ) {
        return;
    }
    $addr = strtolower( trim( (string) @file_get_contents( $addr_file ) ) );
    if ( ! preg_match( '/^[a-z2-7]{56}\.onion$/D', $addr ) ) {
        return;
    }
    $request_uri = $_SERVER['REQUEST_URI'] ?? '/';
    header( 'Onion-Location: http://' . $addr . $request_uri );
}
add_action( 'send_headers', 'onionpress_send_onion_location_header' );

// Responsive image srcset URLs. Without this, hand-authored posts with
// uploaded images render `srcset="http://localhost/..."` (no port,
// from the unfiltered raw siteurl), which doesn't resolve from any
// browser — neither Tor nor on the local machine via the actual port.
// Imported social-archive posts already have relative URLs baked into
// post_content so srcset stays relative there; this filter only kicks
// in for posts whose original `src` was absolute.
add_filter( 'wp_calculate_image_srcset', function ( $sources ) {
    if ( ! is_array( $sources ) ) {
        return $sources;
    }
    foreach ( $sources as $key => $source ) {
        if ( isset( $source['url'] ) ) {
            $sources[ $key ]['url'] = onionpress_rewrite_url( $source['url'] );
        }
    }
    return $sources;
}, 10, 1 );

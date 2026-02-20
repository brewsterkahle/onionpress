<?php
/**
 * Plugin Name: OnionPress Domain Map
 * Description: Rewrites WordPress-generated URLs so "localhost" is replaced
 *              with the actual hostname the visitor is using (.onion, localhost:8080, etc.).
 * Version:     1.0
 * Network:     true
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

/**
 * Only rewrite when the request came in on a host other than "localhost".
 * When HTTP_HOST *is* "localhost" the stored URLs are already correct.
 */
if (
    isset( $_SERVER['HTTP_HOST'] )
    && $_SERVER['HTTP_HOST'] !== 'localhost'
) {
    $onionpress_real_host = $_SERVER['HTTP_HOST'];

    /**
     * Replace //localhost with //actual-host in a URL string.
     * Handles http://localhost, http://localhost/, and http://localhost/path
     * but NOT //localhost:8080 (which is itself a valid access path).
     */
    function onionpress_rewrite_url( $url ) {
        global $onionpress_real_host;
        return preg_replace( '#//localhost(?=/|$)#', '//' . $onionpress_real_host, $url );
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
}

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
     * Handles both http://localhost/… and //localhost/… forms.
     */
    function onionpress_rewrite_url( $url ) {
        global $onionpress_real_host;
        // Replace //localhost (with optional trailing content) but not //localhost:8080
        // since localhost:8080 is itself a valid access path.
        return str_replace( '//localhost/', '//' . $onionpress_real_host . '/', $url );
    }

    // Single-site options (per-blog home & siteurl).
    add_filter( 'option_home',    'onionpress_rewrite_url' );
    add_filter( 'option_siteurl', 'onionpress_rewrite_url' );

    // Network-level URLs (admin bar, network admin links, etc.).
    add_filter( 'network_home_url', 'onionpress_rewrite_url' );
    add_filter( 'network_site_url', 'onionpress_rewrite_url' );
}

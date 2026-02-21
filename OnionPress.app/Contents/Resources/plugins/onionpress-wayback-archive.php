<?php
/**
 * Plugin Name: OnionPress Wayback Archive
 * Description: Automatically archives published posts and the homepage to the
 *              Internet Archive Wayback Machine via Tor.
 * Version:     1.0
 * Network:     true
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

/**
 * Auto-detect and cache the clearnet domain.
 *
 * On every web request, if the incoming HTTP_HOST is not .onion, localhost*, or
 * the Docker-internal hostname "wordpress", we treat it as the clearnet domain
 * (set via Cloudflare Tunnel or similar) and persist it to disk.
 */
add_action( 'init', function () {
    if ( ! isset( $_SERVER['HTTP_HOST'] ) ) {
        return;
    }

    $host = $_SERVER['HTTP_HOST'];

    // Skip .onion, localhost (with or without port), and Docker-internal hostname
    if ( preg_match( '/\.onion$/i', $host )
        || preg_match( '/^localhost(:\d+)?$/i', $host )
        || $host === 'wordpress'
    ) {
        return;
    }

    $file = '/var/lib/onionpress/clearnet_domain';

    // Only write if the value changed (avoid disk churn)
    $current = @file_get_contents( $file );
    if ( $current !== false && trim( $current ) === $host ) {
        return;
    }

    @file_put_contents( $file, $host );
}, 1 );

/**
 * Archive to the Wayback Machine when a post or page is published/updated.
 */
add_action( 'save_post', function ( $post_id, $post, $update ) {
    // Skip autosaves and revisions
    if ( defined( 'DOING_AUTOSAVE' ) && DOING_AUTOSAVE ) {
        return;
    }
    if ( wp_is_post_revision( $post_id ) ) {
        return;
    }

    // Only archive published posts and pages
    if ( $post->post_status !== 'publish' ) {
        return;
    }
    if ( ! in_array( $post->post_type, array( 'post', 'page' ), true ) ) {
        return;
    }

    // Read the .onion address from the shared volume
    $onion_file = '/var/lib/onionpress/onion_address';
    if ( ! file_exists( $onion_file ) ) {
        return; // Tor not ready yet — skip silently
    }
    $onion_addr = trim( file_get_contents( $onion_file ) );
    if ( empty( $onion_addr ) ) {
        return;
    }

    // Get the post path from the permalink (strip the scheme+host)
    $permalink = get_permalink( $post_id );
    $path      = wp_parse_url( $permalink, PHP_URL_PATH ) ?: '/';

    // Build URLs to archive
    $urls = array();

    // 1. Post .onion URL
    $urls[] = 'http://' . $onion_addr . $path;

    // 2. Homepage .onion URL
    $urls[] = 'http://' . $onion_addr . '/';

    // 3. Clearnet URLs (if Cloudflare Tunnel is configured)
    $clearnet_file = '/var/lib/onionpress/clearnet_domain';
    if ( file_exists( $clearnet_file ) ) {
        $clearnet_domain = trim( file_get_contents( $clearnet_file ) );
        if ( ! empty( $clearnet_domain ) ) {
            $urls[] = 'https://' . $clearnet_domain . $path;
            $urls[] = 'https://' . $clearnet_domain . '/';
        }
    }

    // Deduplicate (e.g. if the post IS the homepage)
    $urls = array_unique( $urls );

    // Submit each URL to the Wayback Machine via Tor
    $save_endpoint = 'http://web.archivep75mbjunhxc6x4j5mwjmomyxb573v42baldlqu56ruil2oiad.onion/save/';
    $tor_proxy     = 'socks5h://onionpress-tor:9050';

    foreach ( $urls as $url ) {
        onionpress_wayback_submit( $save_endpoint, $url, $tor_proxy );
    }
}, 10, 3 );

/**
 * Submit a URL to the Wayback Machine Save Page Now API.
 *
 * Uses PHP curl directly (not wp_remote_post) because WordPress HTTP API
 * does not support SOCKS5 proxies.
 *
 * Fire-and-forget: logs result but does not block the post save.
 */
function onionpress_wayback_submit( $endpoint, $url, $proxy ) {
    if ( ! function_exists( 'curl_init' ) ) {
        error_log( '[OnionPress Wayback] curl extension not available' );
        return;
    }

    error_log( '[OnionPress Wayback] Archiving: ' . $url );

    $ch = curl_init();
    curl_setopt_array( $ch, array(
        CURLOPT_URL            => $endpoint,
        CURLOPT_POST           => true,
        CURLOPT_POSTFIELDS     => array( 'url' => $url ),
        CURLOPT_PROXY          => $proxy,
        CURLOPT_PROXYTYPE      => CURLPROXY_SOCKS5_HOSTNAME,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT        => 15,
        CURLOPT_CONNECTTIMEOUT => 10,
        CURLOPT_FOLLOWLOCATION => true,
        CURLOPT_MAXREDIRS      => 3,
    ) );

    $response  = curl_exec( $ch );
    $http_code = curl_getinfo( $ch, CURLINFO_HTTP_CODE );
    $err       = curl_error( $ch );
    curl_close( $ch );

    if ( $err ) {
        error_log( '[OnionPress Wayback] Curl error for ' . $url . ': ' . $err );
    } else {
        error_log( '[OnionPress Wayback] Submitted ' . $url . ' — HTTP ' . $http_code );
    }
}

<?php
/**
 * OnionPress Sunrise — inbound domain mapping for multisite.
 *
 * Loaded before WordPress's built-in multisite lookup (via the SUNRISE
 * constant in wp-config.php).  It solves the problem that WordPress stores
 * "localhost" as the canonical domain, but the site is accessed through
 * three different hostnames:
 *
 *   - <hash>.onion   (Tor Browser)
 *   - localhost:8080  (Mac / local dev)
 *   - wordpress       (Docker-internal)
 *
 * Instead of matching on HTTP_HOST we always query the DB with
 * domain = 'localhost' and route purely by URL path.
 *
 * Drop this file into  wp-content/sunrise.php  and set
 *   define('SUNRISE', true);
 * in wp-config.php.
 */

// This file is loaded very early — $wpdb is available but most of WP is not.
global $wpdb, $current_site, $current_blog, $blog_id;

// ── 1. Resolve the network (wp_site) ────────────────────────────────
$current_site = $wpdb->get_row(
    $wpdb->prepare(
        "SELECT * FROM {$wpdb->site} WHERE domain = %s AND path = %s LIMIT 1",
        'localhost',
        '/'
    )
);

if ( ! $current_site ) {
    // Multisite tables don't exist yet (first run before install finishes).
    return;
}

$current_site->blog_id = $wpdb->get_var(
    $wpdb->prepare(
        "SELECT blog_id FROM {$wpdb->blogs} WHERE domain = %s AND path = %s LIMIT 1",
        'localhost',
        '/'
    )
);

// ── 2. Resolve the blog by path ─────────────────────────────────────
$request_path = '/';
if ( isset( $_SERVER['REQUEST_URI'] ) ) {
    $request_path = parse_url( $_SERVER['REQUEST_URI'], PHP_URL_PATH );
    if ( ! $request_path ) {
        $request_path = '/';
    }
}

// Extract the first path segment: /alice/foo/bar → /alice/
if ( preg_match( '#^/([^/]+)/#', $request_path, $m ) ) {
    $slug = $m[1];

    // Check if this slug corresponds to a registered sub-site.
    $row = $wpdb->get_row(
        $wpdb->prepare(
            "SELECT * FROM {$wpdb->blogs} WHERE domain = %s AND path = %s LIMIT 1",
            'localhost',
            '/' . $slug . '/'
        )
    );

    if ( $row ) {
        $current_blog = $row;
    }
}

// Fall back to the main site.
if ( empty( $current_blog ) ) {
    $current_blog = $wpdb->get_row(
        $wpdb->prepare(
            "SELECT * FROM {$wpdb->blogs} WHERE domain = %s AND path = %s LIMIT 1",
            'localhost',
            '/'
        )
    );
}

// ── 3. Set $blog_id global (critical!) ──────────────────────────────
// Without this, get_current_blog_id() and switch_to_blog() break because
// they read the $blog_id global, not $current_blog->blog_id.
if ( ! empty( $current_blog ) ) {
    $blog_id = (int) $current_blog->blog_id;
}

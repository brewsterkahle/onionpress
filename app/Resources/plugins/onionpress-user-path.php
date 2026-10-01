<?php
/**
 * Plugin Name: OnionPress User Path
 * Description: Makes /<onionname>/ serve that user's content on every
 *              OnionPress install (either an author archive on blog_id=1
 *              or, once sub-blogs are created per issue #187, the user's
 *              own subsite) and sets user_url to /<onionname>/ so the WP
 *              profile "Website" link points at that stable per-user page.
 * Version:     1.1
 * Network:     true
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

/**
 * Rewrite /<name>/ (single-segment path) to an author-archive query when
 * NAME matches a WP user. We do this by populating $wp->query_vars at
 * parse_request time so WP's normal template hierarchy takes over — no
 * extra redirect, no custom template.
 *
 * Priority 30 on parse_request, which is also where onionpress-directory's
 * /follow?name= handling runs. The /NAME[/REST] name dispatch now lives on
 * template_redirect instead (after is_404() — it only ever fires once WP
 * has failed to find anything of its own), so a local user login served
 * from this hook always wins over a remote name with the same slug.
 */
add_action( 'parse_request', function ( $wp ) {
    // Only run on the network-root blog (blog_id=1). On real subsites
    // (created per issue #187), `/<onionname>/` IS the subsite root and
    // WP's native routing already serves it — intercepting here would
    // override the subsite's own homepage with an author archive.
    //
    // On branded installs (onionpress.org / OnionHome) with no subsites,
    // blog_id=1 is also the only blog and this plugin's author-archive
    // fallback still applies for legacy /<login>/ paths.
    if ( get_current_blog_id() !== 1 ) {
        return;
    }

    // Only GETs; leave wp-admin, XML-RPC, login, etc. untouched.
    if ( ( $_SERVER['REQUEST_METHOD'] ?? 'GET' ) !== 'GET' ) {
        return;
    }

    $uri  = $_SERVER['REQUEST_URI'] ?? '/';
    $path = parse_url( $uri, PHP_URL_PATH );
    if ( ! is_string( $path ) ) {
        return;
    }
    $segment = trim( $path, '/' );

    // Must be a single non-empty segment.
    if ( $segment === '' || strpos( $segment, '/' ) !== false ) {
        return;
    }

    // Length / charset filter matches the server-side validate_name rules,
    // so we don't pound the DB for obviously-invalid segments.
    if (
        strlen( $segment ) < 5 || strlen( $segment ) > 40
        || ! preg_match( '/^[A-Za-z0-9][A-Za-z0-9._-]*[A-Za-z0-9]$/', $segment )
        || preg_match( '/^[0-9]+$/', $segment )
    ) {
        return;
    }

    // Skip obvious WP paths — get_user_by doesn't return anything for these
    // anyway, but the short-circuit avoids a DB hit.
    $reserved = array(
        'wp-admin', 'wp-content', 'wp-login', 'wp-includes', 'wp-json',
        'wp-cron', 'wp-signup', 'wp-activate', 'feed', 'xmlrpc',
        'follow', 'onionpress-status', 'onionpress-settings',
    );
    if ( in_array( strtolower( $segment ), $reserved, true ) ) {
        return;
    }

    // Look up by login. Note: WP usernames are not case-insensitive at the
    // DB level by default, but our registry is — so we first try exact, then
    // iterate over any user whose lowercased login matches.
    $user = get_user_by( 'login', $segment );
    if ( ! $user ) {
        $lower = strtolower( $segment );
        $users = get_users( array(
            'search'         => $segment,
            'search_columns' => array( 'user_login' ),
            'number'         => 5,
            'fields'         => array( 'user_login', 'ID' ),
        ) );
        foreach ( $users as $candidate ) {
            if ( strtolower( $candidate->user_login ) === $lower ) {
                $user = get_userdata( $candidate->ID );
                break;
            }
        }
    }
    if ( ! $user ) {
        return;
    }

    // Set the author query on WP. Using nicename because that's what WP's
    // author-archive logic matches against; it falls back to user_login for
    // users who haven't customized their display name.
    $wp->query_vars = array(
        'author_name' => $user->user_nicename
            ? $user->user_nicename
            : $user->user_login,
    );
}, 30 );

/**
 * Derive the per-user URL for a given login:
 * http://<network-home>/<login>/ — always points at something useful
 * (author archive today, subsite once per-user blogs land).
 */
function onionpress_user_path_url_for( $login ) {
    $base = function_exists( 'network_home_url' )
        ? network_home_url( '/' )
        : home_url( '/' );
    return trailingslashit( $base ) . $login . '/';
}

/**
 * On user creation (single or multisite), set user_url to the per-user
 * page. Leave existing non-empty values alone unless they point at the
 * bare network root (the historical default that motivated this fix).
 */
add_action( 'user_register', function ( $user_id ) {
    $user = get_userdata( $user_id );
    if ( ! $user || empty( $user->user_login ) ) {
        return;
    }
    $desired = onionpress_user_path_url_for( $user->user_login );
    $current = (string) $user->user_url;
    $network_root = rtrim(
        function_exists( 'network_home_url' ) ? network_home_url( '/' ) : home_url( '/' ),
        '/'
    );
    $should_set = ( $current === '' )
        || ( rtrim( $current, '/' ) === $network_root );
    if ( ! $should_set ) {
        return;
    }
    // Use $wpdb to avoid re-entering user_register via wp_update_user hooks.
    global $wpdb;
    $wpdb->update(
        $wpdb->users,
        array( 'user_url' => $desired ),
        array( 'ID' => $user_id )
    );
    clean_user_cache( $user_id );
}, 20, 1 );

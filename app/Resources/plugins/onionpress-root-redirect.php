<?php
/**
 * Plugin Name: OnionPress Root Redirect
 * Description: On the network-root blog (blog_id=1), redirect / to the
 *              primary subsite's home so visitors see the user's
 *              /<onionname>/ URL rather than an empty root blog. Respects
 *              static front pages, the onionpress_root_site flag (set on
 *              branded installs like onionpress.org / OnionHome), and
 *              pages that aren't the actual front URL.
 * Version:     2.0
 * Network:     true
 * License:     AGPL-3.0-or-later
 * License URI: https://www.gnu.org/licenses/agpl-3.0.html
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

// Hide the network-root blog (blog_id=1) from the "My Sites" admin-bar
// dropdown — it's just infrastructure, not a real site the user should see.
// Branded installs keep it visible because blog_id=1 IS their public site.
add_action( 'admin_bar_menu', function ( $wp_admin_bar ) {
    if ( get_option( 'onionpress_root_site' ) === 'yes' ) {
        return;
    }
    $wp_admin_bar->remove_node( 'blog-1' );
}, 999 );

add_action( 'template_redirect', function () {
    // Only run on the network-root blog. On subsites the user is already
    // on /<onionname>/; they should see their own site, not get bounced.
    if ( get_current_blog_id() !== 1 ) {
        return;
    }

    // Branded installs (onionpress.org product pages, OnionHome directory)
    // keep blog_id=1 as the public face — never redirect.
    if ( get_option( 'onionpress_root_site' ) === 'yes' ) {
        return;
    }

    // Respect a static front page: the admin configured what `/` should
    // show; don't second-guess them.
    if ( get_option( 'show_on_front' ) === 'page' && get_option( 'page_on_front' ) ) {
        return;
    }

    // Only redirect the actual front URL — not /?s=search, /feed, etc.
    if ( ! is_front_page() ) {
        return;
    }

    // Find the primary subsite: first wp_blogs row whose path isn't '/'.
    // In the current single-primary-user model there's at most one; when
    // we grow secondary subsites, the primary will still be created first
    // and sort to the lowest id.
    $sites = get_sites( array(
        'number'  => 10,
        'orderby' => 'id',
        'order'   => 'ASC',
    ) );
    $primary = null;
    foreach ( $sites as $s ) {
        if ( $s->path !== '/' ) {
            $primary = $s;
            break;
        }
    }
    if ( ! $primary ) {
        // No subsite yet — nothing to redirect to; fall through so the
        // default blog listing still renders.
        return;
    }

    switch_to_blog( $primary->blog_id );
    $target = home_url( '/' );
    restore_current_blog();

    // 301 tells search engines the canonical location, but no-store keeps
    // browsers and proxies from caching it — so if the primary onionname
    // ever changes, users don't get permanently stuck on the old target.
    nocache_headers();
    wp_redirect( $target, 301 );
    exit;
} );

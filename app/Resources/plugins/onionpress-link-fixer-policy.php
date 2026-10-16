<?php
/**
 * Plugin Name: OnionPress Link Fixer Policy
 * Description: Holds the Internet Archive Wayback Machine Link Fixer to
 *              what suits an onion service: archive.org is asked only about
 *              clearnet links, over verified TLS, with timeouts a Tor
 *              circuit can meet, and a large social-media import is checked
 *              a few posts at a time instead of all at once.
 * Version:     1.0
 * Network:     true
 * License:     AGPL-3.0-or-later
 * License URI: https://www.gnu.org/licenses/agpl-3.0.html
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

/*
 * What the Link Fixer (1.4) does, as far as it matters here:
 *
 * - For each published post (when it is saved, and in a background scan
 *   of the posts it has not seen) it collects the http(s) links and, for
 *   every new one, asks archive.org's availability API for a snapshot, asks
 *   Save Page Now for one if there is none, and has archive.org's live-web
 *   checker (iabot-api.archive.org) test the link from time to time.
 * - It changes no post. Its front-end script, in the visitor's browser,
 *   points a link archive.org found broken at its Wayback copy.
 * - Every request goes through the WordPress HTTP API (wp_safe_remote_*),
 *   so onionpress-tor-proxy.php sends it through Tor and resolves no name
 *   in the container, and carries a WP-Wayback-Link-Fixer header.
 *
 * Its own link exclusions (the iawmlf_link_exclusions option, which
 * OnionPress also sets) only keep a link out of the front-end script: the
 * archive.org lookups for a new link are queued before they are applied.
 */

/** Whether $args are a Link Fixer request: it adds this header to all of them. */
function onionpress_link_fixer_is_its_request( $args ) {
    $headers = isset( $args['headers'] ) ? $args['headers'] : array();
    if ( is_string( $headers ) ) {
        return 1 === preg_match( '/^\s*wp-wayback-link-fixer\s*:/im', $headers );
    }
    foreach ( (array) $headers as $name => $value ) {
        if ( is_string( $name ) && 0 === strcasecmp( trim( $name ), 'WP-Wayback-Link-Fixer' ) ) {
            return true;
        }
    }
    return false;
}

/**
 * The link a Link Fixer request asks archive.org about: the url field of a
 * Save Page Now POST, or the url= query parameter (availability API,
 * live-web check); '' for one that is not a string. null when it names
 * none (job and system status).
 */
function onionpress_link_fixer_target( $args, $url ) {
    $body = isset( $args['body'] ) ? $args['body'] : null;
    if ( is_string( $body ) && '' !== $body ) {
        parse_str( $body, $body );
    }
    if ( is_array( $body ) && array_key_exists( 'url', $body ) ) {
        return is_string( $body['url'] ) ? $body['url'] : '';
    }
    $query = (string) parse_url( (string) $url, PHP_URL_QUERY );
    if ( '' !== $query ) {
        parse_str( $query, $params );
        if ( array_key_exists( 'url', $params ) ) {
            return is_string( $params['url'] ) ? $params['url'] : '';
        }
    }
    return null;
}

/**
 * Whether archive.org may be asked about $link: a link to a public
 * clearnet host it could reach (onionpress_remote_host_allowed(), the rule
 * the Tor proxy applies to WordPress's own requests). Never an onion
 * service: archive.org cannot reach one, the check would call a working
 * link broken, and it would learn the address. Never this machine, a
 * container or a private network. Without the Tor proxy's rules, nothing.
 */
function onionpress_link_fixer_may_ask_about( $link ) {
    $host = strtolower( rtrim( (string) parse_url( (string) $link, PHP_URL_HOST ), '.' ) );
    if ( '' === $host || 'onion' === $host || '.onion' === substr( $host, -6 ) ) {
        return false;
    }
    if ( ! function_exists( 'onionpress_remote_host_allowed' ) ) {
        return false;
    }
    return (bool) onionpress_remote_host_allowed( $host, (string) $link );
}

/**
 * pre_http_request: refuse a Link Fixer request about a link
 * onionpress_link_fixer_may_ask_about() rules out. The Link Fixer takes the
 * error as "no snapshot" or "archive.org unreachable": it never marks such
 * a link broken and never points it anywhere else.
 */
function onionpress_link_fixer_guard( $pre, $args, $url ) {
    if ( false !== $pre || ! onionpress_link_fixer_is_its_request( $args ) ) {
        return $pre;
    }
    $target = onionpress_link_fixer_target( $args, $url );
    if ( null === $target || onionpress_link_fixer_may_ask_about( $target ) ) {
        return $pre;
    }
    return new WP_Error( 'onionpress_link_fixer_refused',
        'OnionPress does not ask archive.org about onion, local or private-network links.' );
}
add_filter( 'pre_http_request', 'onionpress_link_fixer_guard', 5, 3 );

/**
 * http_request_args: every Link Fixer request verifies archive.org's
 * certificate (its Save Page Now POST turns verification off, and through
 * a Tor exit that would let the exit answer in archive.org's name and read
 * the request, API keys included), and gets 30 to 60 seconds: its 10-second
 * lookups time out over Tor, and its checker and Save Page Now ask for
 * 5000 and 1000 seconds, which would hold a PHP worker that long.
 */
function onionpress_link_fixer_request_args( $args, $url = '' ) {
    if ( ! is_array( $args ) || ! onionpress_link_fixer_is_its_request( $args ) ) {
        return $args;
    }
    $args['sslverify'] = true;
    $timeout           = isset( $args['timeout'] ) ? (float) $args['timeout'] : 0;
    $args['timeout']   = min( max( $timeout, 30 ), 60 );
    return $args;
}
add_filter( 'http_request_args', 'onionpress_link_fixer_request_args', 20, 2 );

/**
 * The background scan of existing posts: 5 posts every 15 minutes (the
 * Link Fixer's own pace is 10 every 10 minutes), 480 posts a day. Each new
 * link costs archive.org two to eight requests over Tor, so a 20,000-post
 * social-media import, perhaps 8,000 links, is checked over about six
 * weeks at a request every minute or so, instead of in one burst.
 */
function onionpress_link_fixer_posts_per_batch() {
    return 5;
}
add_filter( 'iawmlf_posts_per_batch', 'onionpress_link_fixer_posts_per_batch' );

function onionpress_link_fixer_scan_interval() {
    return 15 * MINUTE_IN_SECONDS;
}
add_filter( 'iawmlf_scan_posts_interval', 'onionpress_link_fixer_scan_interval' );

/**
 * save_post: an imported post (the social and blog importers give every
 * post a _source_id) is left to the scan above the first time it is saved.
 * Otherwise an import of thousands of posts queued archive.org lookups for
 * every link in them at once. Once scanned (it has iawmlf_links), edits are
 * handled as usual. The Link Fixer runs at priority 10, between these two.
 */
function onionpress_link_fixer_hold_import( $post_id ) {
    if ( '' === (string) get_post_meta( $post_id, '_source_id', true )
         || metadata_exists( 'post', $post_id, 'iawmlf_links' ) ) {
        return;
    }
    add_filter( 'pre_option_iawmlf_process_links', 'onionpress_link_fixer_processing_off' );
}
add_action( 'save_post', 'onionpress_link_fixer_hold_import', 9 );

function onionpress_link_fixer_release_import() {
    remove_filter( 'pre_option_iawmlf_process_links', 'onionpress_link_fixer_processing_off' );
}
add_action( 'save_post', 'onionpress_link_fixer_release_import', 11 );

function onionpress_link_fixer_processing_off() {
    return 0;
}

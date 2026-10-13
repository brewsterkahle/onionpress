<?php
/**
 * Plugin Name: OnionPress Blogroll
 * Description: RSS/Atom feed aggregator for followed sites. Fetches feeds
 *              through Tor (via onionpress-tor-proxy), caches results in a
 *              transient, and exposes onionpress_get_aggregated_feed_items()
 *              for the theme's blogroll page and sidebar.
 * Version: 1.0.0
 * Author: OnionPress
 * Network: true
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

// Re-enable the WordPress Links Manager (hidden since 3.5)
add_filter( 'pre_option_link_manager_enabled', '__return_true' );

// No global SimplePie cache lifetime here: it set every feed's to 1 hour,
// the aggregate's own TTL, so a follow's cached copy had expired by each
// refresh anyway. Follow fetches skip SimplePie's cache altogether
// (onionpress_follow_fetch_feed()); the last good items of each follow
// are kept in onionpress_following_items instead.

// Invalidate the aggregated cache when the following list changes. The
// first write to a site's list adds the option, and update_option() then
// fires add_option_*, not update_option_*: hook both, or a blog's first
// follow stays out of the sidebar and Blogroll until the cache expires.
add_action( 'add_option_onionpress_following', 'onionpress_blogroll_flush_items' );
add_action( 'update_option_onionpress_following', 'onionpress_blogroll_flush_items' );
function onionpress_blogroll_flush_items() {
    delete_transient( 'onionpress_blogroll_items' );
}

/**
 * Default follows seeded for a fresh install.
 *
 * Keys are the canonical "complete" form: <addr>.onion/<onionname>.
 */
if ( ! function_exists( 'onionpress_default_follows' ) ) {
function onionpress_default_follows() {
    return array(
        'op2homeiwjb4fdqnfkj5kbokvcee45zpk2pwgvpz5rrkanp5qqwxzbyd.onion/onionhome',
        'oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion/onionheaven',
    );
}
}

/**
 * Store the default follows in a site's list the first time it is read.
 *
 * The stored option is the one list: the sidebar, the Blogroll and the
 * aggregator read it with an empty default, and Settings does too. Settings
 * used to fall back to the defaults instead, while nothing ever wrote them,
 * so a fresh site showed OnionHome and OnionHeaven as followed in Settings
 * (stuck at "Not checked yet") and nothing in public. Seeding runs only when
 * the option does not exist: a list the administrator emptied stays empty.
 * Every site (each subsite too) is seeded on its own first request.
 */
if ( ! function_exists( 'onionpress_follow_seed_defaults' ) ) {
function onionpress_follow_seed_defaults() {
    if ( false === get_option( 'onionpress_following', false ) ) {
        add_option( 'onionpress_following', onionpress_default_follows() );
    }
}
}
add_action( 'init', 'onionpress_follow_seed_defaults' );

/**
 * Parse a follow key into its parts.
 *
 * A follow key is one of:
 *   - "<56>.onion"          → onion root site
 *   - "<56>.onion/<name>"   → onion subsite (path-based multisite)
 *   - "http(s)://…"         → direct feed / clearnet URL
 *
 * A single .onion address hosts a multisite with many onionname subsites, so
 * the onionname is part of the key — that's what lets two subsites on the same
 * address be followed independently.
 *
 * @return array{type:string,addr:string,name:string,url:string}
 */
if ( ! function_exists( 'onionpress_follow_parse' ) ) {
function onionpress_follow_parse( $key ) {
    $key = trim( (string) $key );
    if ( preg_match( '#^https?://#i', $key ) ) {
        return array( 'type' => 'url', 'addr' => '', 'name' => '', 'url' => $key );
    }
    if ( preg_match( '#^([a-z2-7]{56}\.onion)(?:/([a-z0-9][a-z0-9._-]*))?$#iD', $key, $m ) ) {
        return array(
            'type' => 'onion',
            'addr' => strtolower( $m[1] ),
            'name' => isset( $m[2] ) ? strtolower( $m[2] ) : '',
            'url'  => '',
        );
    }
    // Unrecognized (e.g. a malformed entry) — treat opaquely as an onion key.
    return array( 'type' => 'onion', 'addr' => strtolower( $key ), 'name' => '', 'url' => '' );
}
}

/**
 * Build a canonical follow key from an address and optional onionname.
 */
if ( ! function_exists( 'onionpress_follow_key' ) ) {
function onionpress_follow_key( $addr, $name = '' ) {
    $addr = strtolower( trim( (string) $addr ) );
    $name = strtolower( trim( (string) $name ) );
    return $name !== '' ? $addr . '/' . $name : $addr;
}
}

/**
 * The browsable site URL for a follow key (used by the "open site" link).
 */
if ( ! function_exists( 'onionpress_follow_site_url' ) ) {
function onionpress_follow_site_url( $key ) {
    $p = onionpress_follow_parse( $key );
    if ( 'url' === $p['type'] ) {
        return $p['url'];
    }
    return 'http://' . $p['addr'] . ( '' !== $p['name'] ? '/' . $p['name'] : '/' );
}
}

/**
 * Whether the Following feature may send a request to $host.
 *
 * Every site administrator has a Following list (it is per-site), and what
 * they type is fetched from inside the WordPress container.
 * onionpress-tor-proxy sends each request through Tor except to the hosts
 * onionpress_is_local_host() names (the containers, localhost), which it
 * connects to directly: following http://onionpress-tor:8083/... would have
 * WordPress call the hub API, the healthcheck service or MariaDB's port and
 * report what came back. So a follow names a .onion or a public DNS name,
 * both of which Tor resolves at its end:
 *
 *   - at least two labels of letters, digits and inner hyphens, the last
 *     one alphabetic (or an xn-- IDN TLD). That refuses every IP literal,
 *     dotted, IPv6 or in the integer, octal and hex spellings curl and
 *     inet_aton() accept (none ends in an alphabetic label), every single-
 *     label name (all the container names), trailing dots and userinfo;
 *   - not localhost or another name reserved for a local network;
 *   - not a host onionpress_is_local_host() would connect to directly, and
 *     nothing at all when that function (the Tor routing) is not loaded.
 *
 * There is no DNS lookup here: it would send the name outside Tor.
 */
function onionpress_follow_host_allowed( $host ) {
    if ( ! is_string( $host ) || '' === $host || strlen( $host ) > 253 ) {
        return false;
    }
    $host = strtolower( $host );
    if ( ! preg_match( '/^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})$/D', $host ) ) {
        return false;
    }
    if ( preg_match( '/(?:^|\.)(?:localhost|localdomain|local|internal|home\.arpa)$/D', $host ) ) {
        return false;
    }
    return function_exists( 'onionpress_is_local_host' ) && ! onionpress_is_local_host( $host );
}

/**
 * Whether the Following feature may request $url: http(s), no userinfo, no
 * whitespace, control characters or backslashes (URL parsers disagree about
 * those), and a host onionpress_follow_host_allowed() accepts. The host is
 * read with the same parse_url() onionpress-tor-proxy uses to choose between
 * Tor and a direct connection.
 */
function onionpress_follow_url_allowed( $url ) {
    if ( ! is_string( $url ) || strlen( $url ) > 2048 ) {
        return false;
    }
    if ( preg_match( '/[\x00-\x20\x7f\\\\]/', $url ) || ! preg_match( '#^https?://#i', $url ) ) {
        return false;
    }
    $parts = wp_parse_url( $url );
    if ( ! is_array( $parts ) || empty( $parts['host'] )
         || isset( $parts['user'] ) || isset( $parts['pass'] ) ) {
        return false;
    }
    return onionpress_follow_host_allowed( $parts['host'] );
}

/** What a follow request onionpress_follow_url_allowed() refuses returns. */
function onionpress_follow_refused() {
    return new WP_Error( 'onionpress_follow_refused',
        'OnionPress: a follow may request only .onion and public addresses' );
}

/**
 * Run $fn (a follow probe or feed fetch) with every HTTP request it makes,
 * and every redirect those follow, held to onionpress_follow_url_allowed().
 *
 * The first URL is checked by the caller; this covers the rest. fetch_feed()
 * can request URLs it was never given (SimplePie follows a page's
 * <link rel="alternate"> to find the feed), and a redirect is a new URL
 * the caller never saw. onionpress-tor-proxy picks Tor or direct from a
 * request's first URL and keeps it for every hop, so today a redirect from
 * an allowed host stays on Tor; checking each hop as well does not depend on
 * that.
 */
function onionpress_follow_guarded( $fn ) {
    static $depth = 0;
    if ( 0 === $depth++ ) {
        add_filter( 'http_request_args', 'onionpress_follow_guard_args', PHP_INT_MAX, 2 );
        add_filter( 'pre_http_request', 'onionpress_follow_guard_request', PHP_INT_MAX, 3 );
        add_action( 'requests-requests.before_request', 'onionpress_follow_guard_hops', 10, 5 );
    }
    try {
        return call_user_func( $fn );
    } finally {
        if ( 0 === --$depth ) {
            remove_filter( 'http_request_args', 'onionpress_follow_guard_args', PHP_INT_MAX );
            remove_filter( 'pre_http_request', 'onionpress_follow_guard_request', PHP_INT_MAX );
            remove_action( 'requests-requests.before_request', 'onionpress_follow_guard_hops', 10 );
        }
    }
}

/**
 * http_request_args inside onionpress_follow_guarded(): no
 * wp_http_validate_url() for a URL the follow rule accepts. fetch_feed()
 * asks for it (wp_safe_remote_request), and for a host that is not a .onion
 * it calls gethostbyname() in the container: a DNS query outside Tor naming
 * the followed site, sent when a visitor's page view refreshes the
 * blogroll. The request itself goes through Tor, which resolves the name at
 * the exit, and this guard holds the URL, every further request and every
 * redirect to onionpress_follow_url_allowed() in place of that check.
 * onionpress-tor-proxy replaces the check for every Tor-routed request with
 * its own lookup-free one (onionpress_check_url); for a follow, the follow
 * rule replaces that too, as before.
 */
function onionpress_follow_guard_args( $args, $url ) {
    if ( onionpress_follow_url_allowed( $url ) ) {
        $args['reject_unsafe_urls'] = false;
        unset( $args['onionpress_check_url'] );
        // The request keeps the timeout it asked for: onionpress-tor-proxy
        // otherwise raises every timeout to at least 60 s. Inside a
        // budgeted run (onionpress_follow_deadline()) it is also cut to
        // what is left of the budget, so the budget is a real cap and not
        // a check between follows.
        $args['onionpress_exact_timeout'] = true;
        $deadline = onionpress_follow_deadline();
        if ( $deadline > 0 ) {
            $left    = max( 1, $deadline - microtime( true ) );
            $timeout = isset( $args['timeout'] ) ? (float) $args['timeout'] : $left;
            $args['timeout'] = min( $timeout, $left );
        }
    }
    return $args;
}

/**
 * The wall-clock deadline (microtime) every follow request is held to, or 0
 * for none. onionpress_blogroll_refresh() sets it for its run and clears it
 * when done; the follow form's probes run without one, on their own
 * timeouts.
 */
function onionpress_follow_deadline( $set = null ) {
    static $deadline = 0;
    if ( null !== $set ) {
        $deadline = (float) $set;
    }
    return $deadline;
}

/** pre_http_request inside onionpress_follow_guarded(): refuse what a follow may not request. */
function onionpress_follow_guard_request( $pre, $args, $url ) {
    if ( onionpress_follow_url_allowed( $url ) ) {
        return $pre;
    }
    return onionpress_follow_refused();
}

/**
 * requests.before_request inside onionpress_follow_guarded(): check each
 * redirect on the request's own Requests hooks, where WordPress checks its
 * own (WP_Http::validate_redirects) and a refusal becomes a WP_Error.
 */
function onionpress_follow_guard_hops( $url, $headers = array(), $data = array(), $type = '', $options = array() ) {
    if ( isset( $options['hooks'] ) && is_object( $options['hooks'] )
         && method_exists( $options['hooks'], 'register' ) ) {
        $options['hooks']->register( 'requests.before_redirect', 'onionpress_follow_guard_redirect' );
    }
}

/** Requests' before_redirect hook: refuse a redirect a follow may not take. */
function onionpress_follow_guard_redirect( $location ) {
    if ( onionpress_follow_url_allowed( $location ) ) {
        return;
    }
    $exception = class_exists( 'WpOrg\Requests\Exception' ) ? 'WpOrg\Requests\Exception' : 'Requests_Exception';
    throw new $exception( 'OnionPress: a follow may be redirected only to .onion and public addresses',
        'onionpress_follow_refused' );
}

/**
 * wp_remote_get()/wp_remote_head() for the Following feature: refuses a URL
 * onionpress_follow_url_allowed() does not accept, and runs the request
 * under onionpress_follow_guarded().
 */
function onionpress_follow_remote_get( $url, $args = array() ) {
    if ( ! onionpress_follow_url_allowed( $url ) ) {
        return onionpress_follow_refused();
    }
    return onionpress_follow_guarded( function () use ( $url, $args ) {
        return wp_remote_get( $url, $args );
    } );
}

function onionpress_follow_remote_head( $url, $args = array() ) {
    if ( ! onionpress_follow_url_allowed( $url ) ) {
        return onionpress_follow_refused();
    }
    return onionpress_follow_guarded( function () use ( $url, $args ) {
        return wp_remote_head( $url, $args );
    } );
}

/**
 * fetch_feed() for the Following feature, held to the same rules.
 *
 * $timeout is SimplePie's per-request timeout (it would be 10 s, raised to
 * 60 s by onionpress-tor-proxy). SimplePie's cache is off: the refresh
 * keeps each follow's last good items itself, and a cached copy would only
 * hide new posts for as long as it lived.
 */
function onionpress_follow_fetch_feed( $url, $timeout = 60 ) {
    if ( ! onionpress_follow_url_allowed( $url ) ) {
        return onionpress_follow_refused();
    }
    $options = function ( $feed ) use ( $timeout ) {
        $feed->set_timeout( (int) $timeout );
        $feed->enable_cache( false );
    };
    add_action( 'wp_feed_options', $options );
    try {
        return onionpress_follow_guarded( function () use ( $url ) {
            return fetch_feed( $url );
        } );
    } finally {
        remove_action( 'wp_feed_options', $options );
    }
}

/**
 * One-time, per-blog migration from bare-.onion follow keys to the canonical
 * "<addr>.onion/<onionname>" form. Folds the old side `_names` map (and the
 * well-known default names) into the key, then re-keys all related options
 * atomically so nothing orphans. Gated on a per-blog schema version.
 */
if ( ! function_exists( 'onionpress_follow_migrate' ) ) {
function onionpress_follow_migrate() {
    if ( (int) get_option( 'onionpress_following_schema', 1 ) >= 2 ) {
        return;
    }
    $following = get_option( 'onionpress_following', null );
    if ( is_array( $following ) ) {
        $names = get_option( 'onionpress_following_names', array() );
        if ( ! is_array( $names ) ) { $names = array(); }
        // Well-known names previously injected only at display time.
        $names += array(
            'op2homeiwjb4fdqnfkj5kbokvcee45zpk2pwgvpz5rrkanp5qqwxzbyd.onion' => 'onionhome',
            'oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion' => 'onionheaven',
        );
        $feeds  = get_option( 'onionpress_following_feeds',  array() ); if ( ! is_array( $feeds ) )  { $feeds  = array(); }
        $titles = get_option( 'onionpress_following_titles', array() ); if ( ! is_array( $titles ) ) { $titles = array(); }
        $stats  = get_option( 'onionpress_following_stats',  array() ); if ( ! is_array( $stats ) )  { $stats  = array(); }

        $new_following = array();
        $new_feeds = array(); $new_titles = array(); $new_stats = array();
        foreach ( $following as $old ) {
            $p    = onionpress_follow_parse( $old );
            $name = $p['name'];
            if ( 'onion' === $p['type'] && '' === $name && isset( $names[ $old ] ) ) {
                $name = strtolower( (string) $names[ $old ] );
            }
            $key = ( 'url' === $p['type'] ) ? $p['url'] : onionpress_follow_key( $p['addr'], $name );
            if ( in_array( $key, $new_following, true ) ) {
                continue; // collapse duplicates produced by the re-key
            }
            $new_following[] = $key;
            if ( isset( $feeds[ $old ] ) )  { $new_feeds[ $key ]  = $feeds[ $old ]; }
            if ( isset( $titles[ $old ] ) ) { $new_titles[ $key ] = $titles[ $old ]; }
            if ( isset( $stats[ $old ] ) )  { $new_stats[ $key ]  = $stats[ $old ]; }
        }
        update_option( 'onionpress_following',        $new_following );
        // The name now lives in the key; nothing reads the side map again.
        delete_option( 'onionpress_following_names' );
        update_option( 'onionpress_following_feeds',  $new_feeds );
        update_option( 'onionpress_following_titles', $new_titles );
        update_option( 'onionpress_following_stats',  $new_stats );
    }
    update_option( 'onionpress_following_schema', 2 );
}
}
add_action( 'init', 'onionpress_follow_migrate' );

/**
 * Try to discover the RSS feed URL for a followed .onion address.
 *
 * For OnionPress multisite: http://<addr>/<onionname>/feed/
 * Fallback:                 http://<addr>/feed/
 *
 * Only a v3 .onion address is probed. A bare onionname waiting for its
 * OnionHome re-lookup, or any other key that is not an address, used to
 * become http://<key>/feed/ — with a key like "onionpress-tor:8083/api"
 * that was a request into the container network.
 *
 * @param string $addr      56-char .onion address
 * @param string $onionname Optional onionname (path prefix)
 * @param int    $timeout   Per-candidate HEAD timeout in seconds.
 * @return string Feed URL or empty string on failure
 */
function onionpress_discover_feed_url( $addr, $onionname = '', $timeout = 15 ) {
    $addr      = strtolower( (string) $addr );
    $onionname = strtolower( (string) $onionname );
    if ( ! preg_match( '/^[a-z2-7]{56}\.onion$/D', $addr )
         || ( '' !== $onionname && ! preg_match( '/^[a-z0-9][a-z0-9._-]*$/D', $onionname ) ) ) {
        return '';
    }
    $candidates = array();
    if ( $onionname ) {
        $candidates[] = 'http://' . $addr . '/' . $onionname . '/feed/';
    }
    $candidates[] = 'http://' . $addr . '/feed/';

    foreach ( $candidates as $url ) {
        $response = onionpress_follow_remote_head( $url, array( 'timeout' => $timeout ) );
        if ( ! is_wp_error( $response ) ) {
            $code = wp_remote_retrieve_response_code( $response );
            if ( $code >= 200 && $code < 400 ) {
                return $url;
            }
        }
    }
    return '';
}

/*
 * The followed-feeds aggregate is built in the background.
 *
 * A page view never fetches a feed: onionpress_get_aggregated_feed_items()
 * serves the cached aggregate, or, once that has expired or been cleared,
 * the items each follow last returned (onionpress_following_items), and
 * asks wp-cron for a refresh. The refresh used to run inside whichever
 * visitor's page render found the cache expired (the sidebar is on every
 * page): every follow fetched in turn over Tor, each request raised to at
 * least 60 s, the 60 s budget checked only between follows, and no lock, so
 * concurrent visitors each ran the same rebuild and tied up PHP workers and
 * the tor daemon that serves the site.
 *
 * Now one refresh runs at a time per site (onionpress_blogroll_lock()),
 * within ONIONPRESS_BLOGROLL_BUDGET seconds that every request is held to,
 * least recently tried follows first, and a follow that keeps failing is
 * retried 1, 2, 4 ... 24 hours after its last attempt while its last good
 * items stay on the blogroll.
 */
define( 'ONIONPRESS_BLOGROLL_BUDGET', 180 );           // seconds per refresh
define( 'ONIONPRESS_BLOGROLL_LOCK_TTL', ONIONPRESS_BLOGROLL_BUDGET + 120 );
define( 'ONIONPRESS_BLOGROLL_MIN_LEFT', 15 );          // don't start a follow with less
define( 'ONIONPRESS_BLOGROLL_DISCOVER_TIMEOUT', 30 );  // per feed-discovery HEAD
define( 'ONIONPRESS_BLOGROLL_FETCH_TIMEOUT', 60 );     // per feed request
define( 'ONIONPRESS_BLOGROLL_ITEMS_PER_FOLLOW', 10 );

/**
 * Serve the followed-feeds aggregate, newest first. Never fetches.
 *
 * Each item: title, permalink, date (unix), author, source, source_addr,
 * excerpt.
 *
 * @param int $max_items Maximum items to return.
 * @return array
 */
function onionpress_get_aggregated_feed_items( $max_items = 20 ) {
    $cached = get_transient( 'onionpress_blogroll_items' );
    if ( is_array( $cached ) ) {
        return array_slice( $cached, 0, $max_items );
    }
    // Expired, or cleared because the follows changed: serve what each
    // follow last returned while the refresh runs in the background.
    onionpress_blogroll_schedule_refresh();
    return array_slice( onionpress_blogroll_assemble(), 0, $max_items );
}

/** Ask wp-cron for a refresh, unless one is already waiting. */
function onionpress_blogroll_schedule_refresh() {
    if ( ! wp_next_scheduled( 'onionpress_blogroll_refresh' ) ) {
        wp_schedule_single_event( time(), 'onionpress_blogroll_refresh' );
    }
}
add_action( 'onionpress_blogroll_refresh', 'onionpress_blogroll_refresh' );
// Every path that clears the aggregate (a follow added, removed or
// re-keyed) wants the new follows fetched now, not at the next page view.
add_action( 'delete_transient_onionpress_blogroll_items', 'onionpress_blogroll_schedule_refresh' );

/**
 * The aggregate from each current follow's last good items, newest first,
 * at most 50. A follow that is no longer followed contributes nothing.
 */
function onionpress_blogroll_assemble() {
    $following = get_option( 'onionpress_following', array() );
    $items_map = get_option( 'onionpress_following_items', array() );
    if ( ! is_array( $following ) ) { $following = array(); }
    if ( ! is_array( $items_map ) ) { $items_map = array(); }

    $all_items = array();
    foreach ( $following as $key ) {
        if ( ! is_string( $key ) || empty( $items_map[ $key ] ) || ! is_array( $items_map[ $key ] ) ) {
            continue;
        }
        foreach ( $items_map[ $key ] as $item ) {
            if ( is_array( $item ) ) {
                $item['source_addr'] = $key;
                $all_items[] = $item;
            }
        }
    }
    usort( $all_items, function ( $a, $b ) {
        return (int) ( $b['date'] ?? 0 ) - (int) ( $a['date'] ?? 0 );
    } );
    return array_slice( $all_items, 0, 50 );
}

/**
 * Whether a follow is due for a fetch: never failed, or its backoff since
 * the last attempt is over (1 h after one failure, then 2, 4 ... 24 h).
 * Stats written before last_attempt existed count from last_success.
 */
function onionpress_blogroll_is_due( $st, $now ) {
    $fail_count = isset( $st['fail_count'] ) ? (int) $st['fail_count'] : 0;
    if ( $fail_count <= 0 ) {
        return true;
    }
    $since = isset( $st['last_attempt'] ) ? (int) $st['last_attempt']
           : ( isset( $st['last_success'] ) ? (int) $st['last_success'] : 0 );
    $backoff_hours = min( 1 << min( $fail_count - 1, 5 ), 24 );
    return $now - $since >= $backoff_hours * HOUR_IN_SECONDS;
}

/**
 * Take the per-site refresh lock: a row in the options table, created with
 * INSERT IGNORE so only one of two concurrent runs gets it (add_option()
 * would overwrite). Its value carries an expiry, so a run that died still
 * frees it. Returns the value to hand to onionpress_blogroll_unlock(), or
 * '' when another refresh holds it.
 */
function onionpress_blogroll_lock() {
    global $wpdb;
    $value = wp_generate_password( 20, false, false ) . ':' . ( time() + ONIONPRESS_BLOGROLL_LOCK_TTL );
    for ( $try = 0; $try < 2; $try++ ) {
        $wpdb->query( $wpdb->prepare(
            "INSERT IGNORE INTO {$wpdb->options} ( option_name, option_value, autoload ) VALUES ( %s, %s, 'off' )",
            'onionpress_blogroll_lock', $value
        ) );
        if ( 1 === (int) $wpdb->rows_affected ) {
            return $value;
        }
        $held  = (string) $wpdb->get_var( $wpdb->prepare(
            "SELECT option_value FROM {$wpdb->options} WHERE option_name = %s", 'onionpress_blogroll_lock'
        ) );
        $parts = explode( ':', $held, 2 );
        if ( isset( $parts[1] ) && (int) $parts[1] > time() ) {
            return '';
        }
        // Expired: remove exactly the row we read (a newer holder's stays).
        $wpdb->query( $wpdb->prepare(
            "DELETE FROM {$wpdb->options} WHERE option_name = %s AND option_value = %s",
            'onionpress_blogroll_lock', $held
        ) );
    }
    return '';
}

function onionpress_blogroll_unlock( $value ) {
    global $wpdb;
    $wpdb->query( $wpdb->prepare(
        "DELETE FROM {$wpdb->options} WHERE option_name = %s AND option_value = %s",
        'onionpress_blogroll_lock', $value
    ) );
}

/**
 * wp-cron: refresh the aggregate if it has expired or was cleared. Never
 * runs inside a visitor's page render.
 */
function onionpress_blogroll_refresh() {
    if ( false !== get_transient( 'onionpress_blogroll_items' ) ) {
        return;   // a refresh already ran since this one was asked for
    }
    $lock = onionpress_blogroll_lock();
    if ( '' === $lock ) {
        // Another refresh is running. It may have read the follows before
        // the change that cleared the aggregate, so try again after it.
        if ( ! wp_next_scheduled( 'onionpress_blogroll_refresh' ) ) {
            wp_schedule_single_event( time() + MINUTE_IN_SECONDS, 'onionpress_blogroll_refresh' );
        }
        return;
    }
    @set_time_limit( ONIONPRESS_BLOGROLL_LOCK_TTL );
    @ignore_user_abort( true );
    onionpress_follow_deadline( microtime( true ) + ONIONPRESS_BLOGROLL_BUDGET );
    try {
        $complete = onionpress_blogroll_fetch_due();
        // Follows left for lack of time get the next run soon, not in an hour.
        set_transient( 'onionpress_blogroll_items', onionpress_blogroll_assemble(),
            $complete ? HOUR_IN_SECONDS : 5 * MINUTE_IN_SECONDS );
    } finally {
        onionpress_follow_deadline( 0 );
        onionpress_blogroll_unlock( $lock );
    }
}

/**
 * Fetch every due follow, least recently tried first, until the budget
 * runs out, and store what came back. Returns false when due follows were
 * left for lack of time.
 */
function onionpress_blogroll_fetch_due() {
    $following = get_option( 'onionpress_following', array() );
    $feeds_map = get_option( 'onionpress_following_feeds', array() );
    $stats_map = get_option( 'onionpress_following_stats', array() );
    if ( ! is_array( $following ) ) { $following = array(); }
    if ( ! is_array( $feeds_map ) ) { $feeds_map = array(); }
    if ( ! is_array( $stats_map ) ) { $stats_map = array(); }

    $now     = time();
    $due     = array();
    $refused = array();
    foreach ( $following as $addr ) {
        if ( ! is_string( $addr ) ) {
            continue;
        }
        // A URL follow onionpress_follow_url_allowed() refuses (stored before
        // it existed) is never fetched. Its feed URL goes, and so do the
        // title and items an earlier fetch of it stored, which the sidebar shows.
        $p = onionpress_follow_parse( $addr );
        if ( 'url' === $p['type'] && ! onionpress_follow_url_allowed( $p['url'] ) ) {
            $refused[] = $addr;
            continue;
        }
        $st = isset( $stats_map[ $addr ] ) && is_array( $stats_map[ $addr ] ) ? $stats_map[ $addr ] : array();
        if ( onionpress_blogroll_is_due( $st, $now ) ) {
            $due[ $addr ] = isset( $st['last_attempt'] ) ? (int) $st['last_attempt'] : 0;
        }
    }
    // Least recently tried first (stable, so ties keep the list order): a
    // dead follow early in the list can no longer use up every run's
    // budget before the follows after it are reached.
    asort( $due );

    $deadline = onionpress_follow_deadline();
    $results  = array();
    $complete = true;
    foreach ( array_keys( $due ) as $addr ) {
        if ( microtime( true ) > $deadline - ONIONPRESS_BLOGROLL_MIN_LEFT ) {
            $complete = false;
            break;
        }
        $p  = onionpress_follow_parse( $addr );
        $st = isset( $stats_map[ $addr ] ) && is_array( $stats_map[ $addr ] ) ? $stats_map[ $addr ] : array();
        $st['last_attempt'] = time();
        $fail_count = isset( $st['fail_count'] ) ? (int) $st['fail_count'] : 0;
        $result = array();

        $feed_url = isset( $feeds_map[ $addr ] ) ? $feeds_map[ $addr ] : '';
        // Auto-discover feed URL if we don't have one yet.
        if ( ! $feed_url ) {
            // $addr is the follow key: a direct URL, or <onion>/<onionname>.
            if ( 'url' === $p['type'] ) {
                $feed_url = $p['url'];
            } else {
                $feed_url = onionpress_discover_feed_url( $p['addr'], $p['name'], ONIONPRESS_BLOGROLL_DISCOVER_TIMEOUT );
            }
            if ( $feed_url ) {
                $result['feed'] = $feed_url;
            }
        }

        // Held to the same rules: the stored feed URL, what SimplePie
        // discovers from it, and every redirect.
        $feed = $feed_url ? onionpress_follow_fetch_feed( $feed_url, ONIONPRESS_BLOGROLL_FETCH_TIMEOUT ) : null;
        if ( ! $feed || is_wp_error( $feed ) ) {
            // Back off from this attempt. Its last good items stay.
            $st['fail_count'] = $fail_count + 1;
            $result['stats']  = $st;
            $results[ $addr ] = $result;
            continue;
        }

        $feed_title = $feed->get_title();
        $items      = array();
        $newest     = 0;
        foreach ( $feed->get_items( 0, ONIONPRESS_BLOGROLL_ITEMS_PER_FOLLOW ) as $item ) {
            $item_date = (int) $item->get_date( 'U' );
            $newest    = max( $newest, $item_date );
            $items[] = array(
                'title'       => $item->get_title(),
                'permalink'   => $item->get_permalink(),
                'date'        => $item_date,
                'author'      => $item->get_author() ? $item->get_author()->get_name() : '',
                'source'      => $feed_title,
                'source_addr' => $addr,
                'excerpt'     => wp_trim_words( wp_strip_all_tags( $item->get_description() ), 30 ),
            );
        }
        // Success — reset failure count, record contact time
        $st['last_success'] = time();
        $st['fail_count']   = 0;
        if ( $newest ) {
            $st['last_post'] = $newest;
        }
        $result['stats'] = $st;
        $result['items'] = $items;
        if ( $feed_title ) {
            $result['title'] = $feed_title;
        }
        $results[ $addr ] = $result;
    }

    onionpress_blogroll_store( $results, $refused );
    return $complete;
}

/**
 * Write a refresh's results. The options are read again here, after the
 * fetches, and only follows that are still followed are written: the run
 * takes minutes, and writing back the maps it read at the start would undo
 * an unfollow (or a follow form's probe) made in the meantime.
 */
function onionpress_blogroll_store( $results, $refused ) {
    $following = get_option( 'onionpress_following', array() );
    if ( ! is_array( $following ) ) { $following = array(); }
    $followed = array_flip( array_filter( $following, 'is_string' ) );

    $maps = array(
        'feed'  => 'onionpress_following_feeds',
        'title' => 'onionpress_following_titles',
        'stats' => 'onionpress_following_stats',
        'items' => 'onionpress_following_items',
    );
    foreach ( $maps as $field => $option ) {
        $map = get_option( $option, array() );
        if ( ! is_array( $map ) ) { $map = array(); }
        $before = $map;
        foreach ( $results as $addr => $result ) {
            if ( isset( $followed[ $addr ] ) && isset( $result[ $field ] ) ) {
                $map[ $addr ] = $result[ $field ];
            }
        }
        if ( 'stats' !== $field ) {
            foreach ( $refused as $addr ) {
                unset( $map[ $addr ] );
            }
        }
        if ( 'items' === $field ) {
            // Nothing else removes a follow's items: drop the unfollowed.
            $map = array_intersect_key( $map, $followed );
        }
        if ( $map !== $before ) {
            update_option( $option, $map, 'items' === $field ? false : null );
        }
    }
}

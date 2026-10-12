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

// Override SimplePie cache lifetime: 1 hour instead of default 12 hours.
add_filter( 'wp_feed_cache_transient_lifetime', function () {
    return HOUR_IN_SECONDS;
} );

// Invalidate the aggregated cache when the following list changes.
add_action( 'update_option_onionpress_following', function () {
    delete_transient( 'onionpress_blogroll_items' );
} );

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
        $new_names = array(); $new_feeds = array(); $new_titles = array(); $new_stats = array();
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
            if ( '' !== $name ) { $new_names[ $key ] = $name; }
            if ( isset( $feeds[ $old ] ) )  { $new_feeds[ $key ]  = $feeds[ $old ]; }
            if ( isset( $titles[ $old ] ) ) { $new_titles[ $key ] = $titles[ $old ]; }
            if ( isset( $stats[ $old ] ) )  { $new_stats[ $key ]  = $stats[ $old ]; }
        }
        update_option( 'onionpress_following',        $new_following );
        update_option( 'onionpress_following_names',  $new_names );
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
 * @param string $addr      56-char .onion address
 * @param string $onionname Optional onionname (path prefix)
 * @param int    $timeout   Per-candidate HEAD timeout in seconds.
 * @return string Feed URL or empty string on failure
 */
function onionpress_discover_feed_url( $addr, $onionname = '', $timeout = 15 ) {
    $candidates = array();
    if ( $onionname ) {
        $candidates[] = 'http://' . $addr . '/' . $onionname . '/feed/';
    }
    $candidates[] = 'http://' . $addr . '/feed/';

    foreach ( $candidates as $url ) {
        $response = wp_remote_head( $url, array( 'timeout' => $timeout ) );
        if ( ! is_wp_error( $response ) ) {
            $code = wp_remote_retrieve_response_code( $response );
            if ( $code >= 200 && $code < 400 ) {
                return $url;
            }
        }
    }
    return '';
}

/**
 * Fetch and aggregate feed items from all followed sites.
 *
 * Returns a cached array of items sorted by date (newest first).
 * Each item: title, permalink, date (unix), author, source, source_addr, excerpt.
 *
 * @param int $max_items Maximum items to return.
 * @return array
 */
function onionpress_get_aggregated_feed_items( $max_items = 20 ) {
    $cached = get_transient( 'onionpress_blogroll_items' );
    if ( false !== $cached ) {
        return array_slice( $cached, 0, $max_items );
    }

    $following  = get_option( 'onionpress_following', array() );
    $feeds_map  = get_option( 'onionpress_following_feeds', array() );
    if ( ! is_array( $following ) ) { $following = array(); }
    if ( ! is_array( $feeds_map ) ) { $feeds_map = array(); }

    $titles_map = get_option( 'onionpress_following_titles', array() );
    if ( ! is_array( $titles_map ) ) { $titles_map = array(); }

    // Per-follow stats: last_success (unix), last_post (unix), fail_count (int)
    $stats_map = get_option( 'onionpress_following_stats', array() );
    if ( ! is_array( $stats_map ) ) { $stats_map = array(); }

    $all_items  = array();
    $start_time = time();
    $now        = time();
    $feeds_changed  = false;
    $titles_changed = false;
    $stats_changed  = false;

    foreach ( $following as $addr ) {
        // Bail if we've spent too long fetching feeds (Tor can be slow).
        if ( time() - $start_time > 60 ) {
            break;
        }

        // Back off on repeated failures: skip if not enough time has passed.
        // 0 failures = always try, 1 = 1h, 2 = 2h, 3 = 4h, ... max 24h
        $st = isset( $stats_map[ $addr ] ) ? $stats_map[ $addr ] : array();
        $fail_count  = isset( $st['fail_count'] ) ? (int) $st['fail_count'] : 0;
        $last_success = isset( $st['last_success'] ) ? (int) $st['last_success'] : 0;
        if ( $fail_count > 0 && $last_success > 0 ) {
            $backoff_hours = min( pow( 2, $fail_count - 1 ), 24 );
            if ( $now - $last_success < $backoff_hours * HOUR_IN_SECONDS ) {
                // Use cached items if available, skip fetching
                continue;
            }
        }

        $feed_url = isset( $feeds_map[ $addr ] ) ? $feeds_map[ $addr ] : '';

        // Auto-discover feed URL if we don't have one yet.
        if ( ! $feed_url ) {
            // $addr is the follow key: a direct URL, or <onion>/<onionname>.
            $p = onionpress_follow_parse( $addr );
            if ( 'url' === $p['type'] ) {
                $feed_url = $p['url'];
            } else {
                $feed_url = onionpress_discover_feed_url( $p['addr'], $p['name'] );
            }
            if ( $feed_url ) {
                $feeds_map[ $addr ] = $feed_url;
                $feeds_changed = true;
            } else {
                // Discovery failed — count as a failure
                $st['fail_count'] = $fail_count + 1;
                if ( ! $last_success ) { $st['last_success'] = $now; } // seed so backoff works
                $stats_map[ $addr ] = $st;
                $stats_changed = true;
                continue;
            }
        }

        $feed = fetch_feed( $feed_url );
        if ( is_wp_error( $feed ) ) {
            $st['fail_count'] = $fail_count + 1;
            if ( ! $last_success ) { $st['last_success'] = $now; }
            $stats_map[ $addr ] = $st;
            $stats_changed = true;
            continue;
        }

        // Success — reset failure count, record contact time
        $st['last_success'] = $now;
        $st['fail_count']   = 0;

        $feed_title = $feed->get_title();
        // Save the feed title for display in settings/sidebar
        if ( $feed_title && ( ! isset( $titles_map[ $addr ] ) || $titles_map[ $addr ] !== $feed_title ) ) {
            $titles_map[ $addr ] = $feed_title;
            $titles_changed = true;
        }
        $items = $feed->get_items( 0, 10 );
        $newest_post = 0;
        foreach ( $items as $item ) {
            $item_date = (int) $item->get_date( 'U' );
            if ( $item_date > $newest_post ) {
                $newest_post = $item_date;
            }
            $all_items[] = array(
                'title'       => $item->get_title(),
                'permalink'   => $item->get_permalink(),
                'date'        => $item_date,
                'author'      => $item->get_author() ? $item->get_author()->get_name() : '',
                'source'      => $feed_title,
                'source_addr' => $addr,
                'excerpt'     => wp_trim_words( wp_strip_all_tags( $item->get_description() ), 30 ),
            );
        }
        if ( $newest_post ) {
            $st['last_post'] = $newest_post;
        }
        $stats_map[ $addr ] = $st;
        $stats_changed = true;
    }

    // Persist any newly discovered feed URLs, titles, or stats.
    if ( $feeds_changed ) {
        update_option( 'onionpress_following_feeds', $feeds_map );
    }
    if ( $titles_changed ) {
        update_option( 'onionpress_following_titles', $titles_map );
    }
    if ( $stats_changed ) {
        update_option( 'onionpress_following_stats', $stats_map );
    }

    // Sort newest first.
    usort( $all_items, function ( $a, $b ) {
        return $b['date'] - $a['date'];
    } );

    // Store up to 50 items in the cache.
    $to_cache = array_slice( $all_items, 0, 50 );
    set_transient( 'onionpress_blogroll_items', $to_cache, HOUR_IN_SECONDS );

    return array_slice( $to_cache, 0, $max_items );
}

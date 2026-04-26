<?php
/**
 * Plugin Name: OnionPress Wayback Archive
 * Description: Archives the site's posts, home page, and RSS feed to the
 *              Internet Archive's Wayback Machine via Save Page Now (SPN2).
 *              Fire-and-forget pipeline: a 60s cron tick polls outstanding
 *              job_ids in batch, then submits fresh work up to the account's
 *              current available-slots count. No per-URL retry counter,
 *              no back-off chain — a failed submit is simply retried on a
 *              later tick. Two global gates throttle the whole sweep:
 *              (1) self-reachability of our onion, (2) SPN account slots.
 * Version:     4.0
 * Network:     true
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

// ───────────────────────────── tunables ─────────────────────────────

// How often wp-cron fires the entry point. The entry point runs a
// daemon-style inner loop for up to OP_WB_LOOP_MAX_SEC, so cron only
// has to fire as a watchdog that restarts the loop if it died. 5 min
// is plenty — if the loop is still running, cron is a no-op (mutex).
define( 'OP_WB_CRON_INTERVAL', 300 );

// Max new submissions per sweep tick. Through onionheaven SOCKS, 40
// is the sweet spot — sweeps complete in 50-80s, leaving headroom for
// poll/CDX. Bumping to 60 stretched elapsed to >120s (12 chunks of
// concurrency=5) and hurt total throughput despite higher per-tick
// submit count.
define( 'OP_WB_SUBMIT_BATCH_MAX', 40 );

// Max concurrent in-flight curl handles. Measured ceiling: 10 works
// reliably through our onionpress-tor SOCKS but starves other Tor
// consumers (notably the OnionHeaven heartbeat running in a sibling
// container). Dropped to 5 so the SPN loop leaves circuit headroom
// for the heartbeat and reachability checks. 20 saturates outright.
define( 'OP_WB_CONCURRENT_MAX', 5 );

// Max job_ids bundled into a single /save/status POST. SPN accepts
// comma-separated lists; 20 is comfortable.
define( 'OP_WB_STATUS_BATCH_MAX', 20 );

// If a job has been "pending" for this long, something went wrong at
// SPN — clear the job_id so the next tick resubmits fresh.
define( 'OP_WB_STALE_PENDING_SEC', 300 );

// Per-sweep wall-clock budget. Keeps a single tick well under wp-cron's
// timeout even with Tor jitter on every request.
define( 'OP_WB_SWEEP_BUDGET_SEC', 45 );

// Don't poll a job younger than this — SPN's minimum capture time is
// ~20s, so polling immediately wastes a Tor round-trip on a guaranteed
// "pending" answer.
define( 'OP_WB_YOUNG_JOB_SKIP_SEC', 15 );

// Continuous-loop tuning. On a cron tick, the sweep enters a while
// loop and runs indefinitely until the queue is drained, then exits.
// A mutex prevents overlapping invocations; the lock timestamp is
// heartbeated every iteration so a crashed process becomes unstuck
// quickly rather than holding the lock until MAX_SEC expires.
//
// If the loop crashes partway, the next cron tick (whenever a page
// view wakes wp-cron) sees a stale lock and restarts.
define( 'OP_WB_LOOP_IDLE_SLEEP',    30 );  // between iterations when work was done
define( 'OP_WB_LOOP_NOWORK_SLEEP',  90 );  // when iteration found nothing to submit
define( 'OP_WB_LOOP_LOCK_STALE_SEC', 300 );// lock not heartbeated in this long = dead

// Back-off durations (written to op_wayback_backoff_until option).
define( 'OP_WB_BACKOFF_NO_SLOTS',    20 );  // SPN says available=0
define( 'OP_WB_BACKOFF_UNREACHABLE', 120 ); // our own onion not responding
define( 'OP_WB_BACKOFF_SPN_DOWN',     60 ); // /save/status/user call itself failed

// Meta keys (kept compatible with v3 for the already-archived posts).
define( 'OP_WB_META_ARCHIVED_AT',     '_op_wayback_archived_at' );
define( 'OP_WB_META_SNAPSHOT_TS',     '_op_wayback_snapshot_ts' );
define( 'OP_WB_META_JOB_ID',          '_op_wayback_job_id' );
define( 'OP_WB_META_SUBMITTED_AT',    '_op_wayback_submitted_at' );
define( 'OP_WB_META_ORIGINAL_URL',    '_op_wayback_original_url' );
define( 'OP_WB_META_DURATION_SEC',    '_op_wayback_duration_sec' );
define( 'OP_WB_META_RESOURCES_COUNT', '_op_wayback_resources_count' );
define( 'OP_WB_META_OUTLINKS_COUNT',  '_op_wayback_outlinks_count' );
define( 'OP_WB_META_LAST_ERROR_EXT',  '_op_wayback_last_error_ext' );
define( 'OP_WB_META_LAST_ERROR_AT',   '_op_wayback_last_error_at' );

// wp_options keys.
define( 'OP_WB_OPT_HOME',          'op_wayback_home_state' );
define( 'OP_WB_OPT_FEED',          'op_wayback_feed_state' );
define( 'OP_WB_OPT_BACKOFF_UNTIL', 'op_wayback_backoff_until' );

// ─────────────────────────── logging + helpers ──────────────────────

function onionpress_wayback_log( $msg ) {
    error_log( '[OnionPress Wayback] ' . $msg );
}

function onionpress_wayback_version() {
    static $ver = null;
    if ( $ver === null ) {
        $f = '/var/lib/onionpress/version';
        $ver = file_exists( $f ) ? trim( (string) @file_get_contents( $f ) ) : 'dev';
    }
    return $ver;
}

function onionpress_wayback_auth_header() {
    $access = get_blog_option( 1, 'onionpress_archive_s3_access', '' );
    $secret = get_blog_option( 1, 'onionpress_archive_s3_secret', '' );
    if ( empty( $access ) || empty( $secret ) ) {
        return '';
    }
    return 'LOW ' . $access . ':' . $secret;
}

function onionpress_wayback_onion_addr() {
    $f = '/var/lib/onionpress/onion_address';
    if ( ! file_exists( $f ) ) {
        return '';
    }
    return trim( (string) @file_get_contents( $f ) );
}

function onionpress_wayback_post_url( $post_id ) {
    $onion = onionpress_wayback_onion_addr();
    if ( empty( $onion ) ) {
        return '';
    }
    $path = wp_parse_url( get_permalink( $post_id ), PHP_URL_PATH );
    return $path ? 'http://' . $onion . $path : '';
}

function onionpress_wayback_home_url_full() {
    $onion = onionpress_wayback_onion_addr();
    if ( empty( $onion ) ) {
        return '';
    }
    $path = wp_parse_url( home_url( '/' ), PHP_URL_PATH ) ?: '/';
    return 'http://' . $onion . $path;
}

function onionpress_wayback_feed_url_full() {
    $onion = onionpress_wayback_onion_addr();
    if ( empty( $onion ) ) {
        return '';
    }
    $path = wp_parse_url( home_url( '/' ), PHP_URL_PATH ) ?: '/';
    return 'http://' . $onion . rtrim( $path, '/' ) . '/feed/';
}

/**
 * Shared curl setup — all our HTTP goes through Tor SOCKS.
 *
 * Route chosen: onionheaven container's Tor, NOT onionpress-tor. This
 * isolates our outgoing SPN bursts from the onion service we're trying
 * to keep reachable. The onionpress-tor daemon does double duty — it
 * serves our onion AND handles the heartbeat — so heavy outgoing curl
 * bursts through it starve both inbound SPN crawls and the heartbeat,
 * which cascades into takeover. onionheaven container has its own Tor
 * with spare capacity, and already routes reachability probes, so
 * layering SPN traffic through it leaves onionpress-tor focused on
 * serving incoming traffic + keeping the heartbeat fresh.
 */
function onionpress_wayback_curl_common( $ch ) {
    curl_setopt_array( $ch, array(
        CURLOPT_RETURNTRANSFER    => true,
        CURLOPT_FOLLOWLOCATION    => true,
        CURLOPT_UNRESTRICTED_AUTH => true,
        CURLOPT_MAXREDIRS         => 3,
        CURLOPT_USERAGENT         => 'OnionPress/' . onionpress_wayback_version(),
        CURLOPT_PROXY             => 'socks5h://onionheaven:9050',
        CURLOPT_PROXYTYPE         => CURLPROXY_SOCKS5_HOSTNAME,
        CURLOPT_SSL_VERIFYPEER    => false,
        CURLOPT_SSL_VERIFYHOST    => 0,
        CURLOPT_CONNECTTIMEOUT    => 15,
    ) );
}

// ───────────────────── gates (self-reachability, slots) ─────────────

/**
 * HEAD our own onion. SPN's crawler follows the same path a client would,
 * so if we can't reach ourselves through Tor there's no point submitting
 * anything to SPN — every job will come back error:no-captures.
 *
 * Only 200 or 301 count. A 302 indicates the OnionHeaven takeover
 * redirector is in front of us, NOT the real WordPress instance — we
 * haven't fully come up yet.
 */
function onionpress_wayback_self_reachable( $onion ) {
    // Test hook: return non-null to short-circuit the HTTP check.
    $mock = apply_filters( 'onionpress_wayback_self_reachable_mock', null, $onion );
    if ( $mock !== null ) {
        return (bool) $mock;
    }
    $ch = curl_init( 'http://' . $onion . '/' );
    onionpress_wayback_curl_common( $ch );
    curl_setopt_array( $ch, array(
        CURLOPT_NOBODY         => true,
        CURLOPT_FOLLOWLOCATION => false, // a 302 means redirector, not us
        CURLOPT_TIMEOUT        => 20,
    ) );
    curl_exec( $ch );
    $code = (int) curl_getinfo( $ch, CURLINFO_HTTP_CODE );
    curl_close( $ch );
    return $code === 200 || $code === 301;
}

/**
 * GET /save/status/user. Returns the decoded body or null on failure.
 */
function onionpress_wayback_user_status() {
    // Test hook: return an array to short-circuit the SPN call.
    $mock = apply_filters( 'onionpress_wayback_user_status_mock', null );
    if ( $mock !== null ) {
        return is_array( $mock ) ? $mock : null;
    }
    $auth = onionpress_wayback_auth_header();
    if ( empty( $auth ) ) {
        return null;
    }
    $ch = curl_init( 'https://web.archivep75mbjunhxc6x4j5mwjmomyxb573v42baldlqu56ruil2oiad.onion/save/status/user?t=' . time() );
    onionpress_wayback_curl_common( $ch );
    curl_setopt_array( $ch, array(
        CURLOPT_TIMEOUT    => 20,
        CURLOPT_HTTPHEADER => array(
            'Accept: application/json',
            'Authorization: ' . $auth,
        ),
    ) );
    $response = curl_exec( $ch );
    $code     = (int) curl_getinfo( $ch, CURLINFO_HTTP_CODE );
    curl_close( $ch );
    if ( $code !== 200 || ! $response ) {
        return null;
    }
    $body = @json_decode( (string) $response, true );
    return is_array( $body ) ? $body : null;
}

// ────────────────────────── SPN submit + poll ───────────────────────

/**
 * Generic curl_multi runner. $setups is an array keyed by arbitrary ID,
 * each value is a callable receiving a fresh curl handle to configure.
 * Returns an array keyed the same way; each value is
 *   ['code' => int, 'body' => string]
 * regardless of success, so callers can decide what counts as an error.
 *
 * This is the heart of the throughput improvement — instead of paying
 * ~3-5s of Tor round-trip per request serially, all requests run in
 * parallel and the total time is roughly the slowest single request.
 */
function onionpress_wayback_curl_multi( array $setups ) {
    if ( empty( $setups ) ) {
        return array();
    }
    $mh = curl_multi_init();
    $handles = array();
    foreach ( $setups as $key => $configure ) {
        $ch = curl_init();
        onionpress_wayback_curl_common( $ch );
        $configure( $ch );
        curl_multi_add_handle( $mh, $ch );
        $handles[ $key ] = $ch;
    }
    do {
        $status = curl_multi_exec( $mh, $running );
        if ( $running > 0 ) {
            curl_multi_select( $mh, 1.0 );
        }
    } while ( $running > 0 && $status === CURLM_OK );

    $results = array();
    foreach ( $handles as $key => $ch ) {
        $body = (string) curl_multi_getcontent( $ch );
        $code = (int) curl_getinfo( $ch, CURLINFO_HTTP_CODE );
        curl_multi_remove_handle( $mh, $ch );
        curl_close( $ch );
        $results[ $key ] = array( 'code' => $code, 'body' => $body );
    }
    curl_multi_close( $mh );
    return $results;
}

/**
 * Submit many URLs to SPN in parallel. $urls is an array keyed by any
 * stable ID (caller's choice) with values being the URL to submit.
 * Returns an array keyed the same way; each value is a string:
 *   - a job_id on success
 *   - 'RATE_LIMITED' if SPN returned 429 for that URL
 *   - '' if the submit failed
 */
function onionpress_wayback_submit_parallel( array $urls ) {
    if ( empty( $urls ) ) {
        return array();
    }
    // Test hook: return a same-keyed map to short-circuit the SPN submit.
    $mock = apply_filters( 'onionpress_wayback_submit_parallel_mock', null, $urls );
    if ( is_array( $mock ) ) {
        return $mock;
    }
    $auth = onionpress_wayback_auth_header();
    if ( empty( $auth ) ) {
        return array_fill_keys( array_keys( $urls ), '' );
    }
    $headers = array( 'Accept: application/json', 'Authorization: ' . $auth );

    // Speed-tuning params per SPN docs (Tips for faster captures):
    //   skip_first_archive=1      don't compute "is this a first?"
    //   js_behavior_timeout=0     skip JS behaviors (WP content is server-rendered)
    // Dropped `if_not_archived_within=1h` — on retries after a failed
    // onion crawl, SPN was returning the cached error instead of re-
    // trying with a fresh circuit. Relying on SPN's built-in default
    // (45 min) lets genuinely failed URLs retry sooner on new circuits.
    //
    // Chunk the URL list so we never fire more than OP_WB_CONCURRENT_MAX
    // handles at once — Tor SOCKS saturates above that point and every
    // request fails with code=0. Each chunk runs in parallel, chunks run
    // sequentially.
    $results = array();
    $keys = array_keys( $urls );
    foreach ( array_chunk( $keys, OP_WB_CONCURRENT_MAX ) as $key_chunk ) {
        $setups = array();
        foreach ( $key_chunk as $key ) {
            $url = $urls[ $key ];
            $setups[ $key ] = function ( $ch ) use ( $url, $headers ) {
                curl_setopt_array( $ch, array(
                    CURLOPT_URL        => 'https://web.archivep75mbjunhxc6x4j5mwjmomyxb573v42baldlqu56ruil2oiad.onion/save',
                    CURLOPT_POST       => true,
                    CURLOPT_POSTFIELDS => http_build_query( array(
                        'url'                 => $url,
                        'skip_first_archive'  => 1,
                        'js_behavior_timeout' => 0,
                    ) ),
                    CURLOPT_TIMEOUT    => 40,
                    CURLOPT_HTTPHEADER => $headers,
                ) );
            };
        }
        $raw = onionpress_wayback_curl_multi( $setups );
        foreach ( $raw as $key => $r ) {
            if ( $r['code'] === 429 ) {
                $results[ $key ] = 'RATE_LIMITED';
                continue;
            }
            if ( $r['code'] < 200 || $r['code'] >= 400 || empty( $r['body'] ) ) {
                $results[ $key ] = '';
                continue;
            }
            $data = @json_decode( $r['body'], true );
            $results[ $key ] = ( is_array( $data ) && ! empty( $data['job_id'] ) )
                ? (string) $data['job_id'] : '';
        }
    }
    return $results;
}

/**
 * Ask CDX for the latest Wayback capture of each URL, returning the
 * YYYYMMDDHHMMSS timestamp on hit or '' on miss / transport failure.
 *
 * SPN's /save/status memory is unreliable: it flips "success" →
 * "error:no-captures" for the same job_id a few minutes later even when
 * the capture persists in the Wayback Machine. Before trashing a post
 * on SPN's verdict, we verify against CDX directly.
 *
 * (Tried /wayback/available as a lighter primary path — it's not
 * exposed on the archive.org onion mirror and 404s every time. CDX
 * is what works through Tor.)
 *
 * One retry on transport failure (Tor circuit jitter is common; the
 * retry catches most transient 504s without doubling traffic on good
 * circuits).
 */
function onionpress_wayback_cdx_lookup_parallel( array $urls ) {
    if ( empty( $urls ) ) {
        return array();
    }
    // Test hook: return a same-keyed map to short-circuit the CDX call.
    $mock = apply_filters( 'onionpress_wayback_cdx_lookup_parallel_mock', null, $urls );
    if ( is_array( $mock ) ) {
        return $mock;
    }
    // Single pass — no retry. Misses are acceptable: if CDX didn't see
    // a capture this tick, SPN either really hasn't archived it yet or
    // the rescue call itself transient-failed, and either way the URL
    // will be re-submitted on the next sweep, with another shot at
    // rescue later. Retrying here just doubles the Tor load on misses.
    return onionpress_wayback_cdx_one_pass( $urls );
}

// Back-compat alias for the new name.
function onionpress_wayback_availability_parallel( array $urls ) {
    return onionpress_wayback_cdx_lookup_parallel( $urls );
}

/**
 * Single CDX query pass. $urls is keyed map key → URL. Returns a
 * same-keyed map of key → timestamp (empty string on miss).
 */
function onionpress_wayback_cdx_one_pass( array $urls ) {
    $headers = array( 'Accept: application/json' );
    $result = array_fill_keys( array_keys( $urls ), '' );
    foreach ( array_chunk( array_keys( $urls ), OP_WB_CONCURRENT_MAX ) as $key_chunk ) {
        $setups = array();
        foreach ( $key_chunk as $key ) {
            $url_no_scheme = preg_replace( '#^https?://#', '', $urls[ $key ] );
            $endpoint = 'https://web.archivep75mbjunhxc6x4j5mwjmomyxb573v42baldlqu56ruil2oiad.onion/cdx/search/cdx?'
                . 'url=' . urlencode( $url_no_scheme ) . '&output=json&limit=-1';
            $setups[ $key ] = function ( $ch ) use ( $endpoint, $headers ) {
                curl_setopt_array( $ch, array(
                    CURLOPT_URL        => $endpoint,
                    CURLOPT_TIMEOUT    => 25,
                    CURLOPT_HTTPHEADER => $headers,
                ) );
            };
        }
        $raw = onionpress_wayback_curl_multi( $setups );
        foreach ( $raw as $key => $r ) {
            if ( $r['code'] !== 200 || empty( $r['body'] ) ) continue;
            $data = @json_decode( $r['body'], true );
            if ( ! is_array( $data ) || count( $data ) < 2 ) continue;
            // Row 0 is the CDX header; subsequent rows are capture
            // records. Timestamp is index 1. limit=-1 sorts oldest-
            // first so the last element is the most recent capture.
            $last = end( $data );
            if ( is_array( $last ) && ! empty( $last[1] ) ) {
                $result[ $key ] = (string) $last[1];
            }
        }
    }
    return $result;
}

/**
 * Poll SPN for many job_ids in parallel. $job_ids is a flat list; this
 * function chunks them into batches of OP_WB_STATUS_BATCH_MAX, fires
 * one POST per batch concurrently, and returns the flattened response
 * objects. Each object is the raw SPN status dict (with 'job_id',
 * 'status', and on success 'timestamp', 'original_url', 'duration_sec',
 * 'resources', 'outlinks').
 */
function onionpress_wayback_poll_parallel( array $job_ids ) {
    if ( empty( $job_ids ) ) {
        return array();
    }
    // Test hook: return a list of status dicts to short-circuit the SPN poll.
    $mock = apply_filters( 'onionpress_wayback_poll_parallel_mock', null, $job_ids );
    if ( is_array( $mock ) ) {
        return $mock;
    }
    $auth = onionpress_wayback_auth_header();
    $headers = array( 'Accept: application/json' );
    if ( $auth ) {
        $headers[] = 'Authorization: ' . $auth;
    }
    // Same chunking discipline as submit_parallel: cap concurrent handles
    // at OP_WB_CONCURRENT_MAX through the shared Tor SOCKS.
    $all = array();
    $id_chunks = array_chunk( $job_ids, OP_WB_STATUS_BATCH_MAX );
    foreach ( array_chunk( $id_chunks, OP_WB_CONCURRENT_MAX ) as $parallel_group ) {
        $setups = array();
        foreach ( $parallel_group as $i => $chunk ) {
            $setups[ $i ] = function ( $ch ) use ( $chunk, $headers ) {
                curl_setopt_array( $ch, array(
                    CURLOPT_URL        => 'https://web.archivep75mbjunhxc6x4j5mwjmomyxb573v42baldlqu56ruil2oiad.onion/save/status',
                    CURLOPT_POST       => true,
                    CURLOPT_POSTFIELDS => http_build_query( array( 'job_ids' => implode( ',', $chunk ) ) ),
                    CURLOPT_TIMEOUT    => 40,
                    CURLOPT_HTTPHEADER => $headers,
                ) );
            };
        }
        $raw = onionpress_wayback_curl_multi( $setups );
        foreach ( $raw as $r ) {
            if ( $r['code'] !== 200 || empty( $r['body'] ) ) continue;
            $data = @json_decode( $r['body'], true );
            if ( ! is_array( $data ) ) continue;
            foreach ( $data as $item ) {
                if ( is_array( $item ) ) $all[] = $item;
            }
        }
    }
    return $all;
}

// ───────────────── finalize: write outcomes into storage ────────────

/**
 * Record a successful capture. $data is one element from the
 * /save/status batch response. Generic over storage: the caller
 * supplies read/write callables so this works for both postmeta and
 * wp_options (home + feed).
 */
function onionpress_wayback_finalize_success( callable $write, $url, array $data ) {
    $state = array(
        'archived_at'     => time(),
        'snapshot_ts'     => (string) ( $data['timestamp'] ?? '' ),
        'original_url'    => (string) ( $data['original_url'] ?? '' ),
        'duration_sec'    => (float) ( $data['duration_sec'] ?? 0 ),
        'resources_count' => is_array( $data['resources'] ?? null ) ? count( $data['resources'] ) : 0,
        'outlinks_count'  => is_array( $data['outlinks']  ?? null ) ? count( $data['outlinks']  ) : 0,
    );
    // Only store original_url if it actually differs from what we submitted,
    // to avoid 4kB of duplicated URLs in postmeta.
    if ( $state['original_url'] === $url ) {
        unset( $state['original_url'] );
    }
    $write( $state );
}

function onionpress_wayback_finalize_error( callable $write, $ext ) {
    $write( array(
        'last_error_ext' => (string) $ext,
        'last_error_at'  => time(),
    ) );
}

// ────────────────────── state read/write helpers ────────────────────

function onionpress_wayback_post_read( $post_id ) {
    return array(
        'archived_at' => (int)    get_post_meta( $post_id, OP_WB_META_ARCHIVED_AT, true ),
        'job_id'      => (string) get_post_meta( $post_id, OP_WB_META_JOB_ID,       true ),
        'submitted_at'=> (int)    get_post_meta( $post_id, OP_WB_META_SUBMITTED_AT, true ),
    );
}

/**
 * Write a patch of fields to a post's wayback meta. Only keys present in
 * $patch are touched; the rest are preserved. An empty string deletes.
 */
function onionpress_wayback_post_write( $post_id, array $patch ) {
    $mapping = array(
        'archived_at'     => OP_WB_META_ARCHIVED_AT,
        'snapshot_ts'     => OP_WB_META_SNAPSHOT_TS,
        'job_id'          => OP_WB_META_JOB_ID,
        'submitted_at'    => OP_WB_META_SUBMITTED_AT,
        'original_url'    => OP_WB_META_ORIGINAL_URL,
        'duration_sec'    => OP_WB_META_DURATION_SEC,
        'resources_count' => OP_WB_META_RESOURCES_COUNT,
        'outlinks_count'  => OP_WB_META_OUTLINKS_COUNT,
        'last_error_ext'  => OP_WB_META_LAST_ERROR_EXT,
        'last_error_at'   => OP_WB_META_LAST_ERROR_AT,
    );
    foreach ( $patch as $key => $val ) {
        if ( ! isset( $mapping[ $key ] ) ) continue;
        if ( $val === '' || $val === 0 || $val === 0.0 ) {
            delete_post_meta( $post_id, $mapping[ $key ] );
        } else {
            update_post_meta( $post_id, $mapping[ $key ], $val );
        }
    }
}

function onionpress_wayback_opt_read( $option_key ) {
    $raw = get_option( $option_key, array() );
    return is_array( $raw ) ? $raw : array();
}

function onionpress_wayback_opt_write( $option_key, array $patch ) {
    $raw = onionpress_wayback_opt_read( $option_key );
    foreach ( $patch as $k => $v ) {
        if ( $v === '' || $v === 0 || $v === 0.0 ) {
            unset( $raw[ $k ] );
        } else {
            $raw[ $k ] = $v;
        }
    }
    update_option( $option_key, $raw, false /* no autoload */ );
}

// ───────────────────── work queue (posts + home/feed) ───────────────

/**
 * Unified record for one URL under management. Each is an associative
 * array with at minimum 'url', 'read' callable (returns current state),
 * 'write' callable (applies a patch). Used by the sweep so posts and
 * home/feed go through the same code path.
 */
function onionpress_wayback_posts_needing_submit( $limit ) {
    $posts = get_posts( array(
        'post_status'      => 'publish',
        'post_type'        => array( 'post', 'page' ),
        'numberposts'      => (int) $limit,
        'orderby'          => 'date',
        'order'            => 'DESC',
        'meta_query'       => array(
            'relation' => 'AND',
            array( 'key' => OP_WB_META_ARCHIVED_AT, 'compare' => 'NOT EXISTS' ),
            array( 'key' => OP_WB_META_JOB_ID,      'compare' => 'NOT EXISTS' ),
        ),
        'fields'           => 'ids',
        'suppress_filters' => false,
    ) );
    $records = array();
    foreach ( $posts as $post_id ) {
        $url = onionpress_wayback_post_url( $post_id );
        if ( empty( $url ) ) continue;
        $records[] = array(
            'key'   => 'post:' . $post_id,
            'url'   => $url,
            'read'  => function() use ( $post_id ) {
                return onionpress_wayback_post_read( $post_id );
            },
            'write' => function( $patch ) use ( $post_id ) {
                onionpress_wayback_post_write( $post_id, $patch );
            },
        );
    }
    return $records;
}

function onionpress_wayback_posts_with_in_flight() {
    $posts = get_posts( array(
        'post_status'      => 'publish',
        'post_type'        => array( 'post', 'page' ),
        'numberposts'      => -1,
        'meta_query'       => array(
            array( 'key' => OP_WB_META_JOB_ID, 'compare' => 'EXISTS' ),
        ),
        'fields'           => 'ids',
        'suppress_filters' => false,
    ) );
    $records = array();
    foreach ( $posts as $post_id ) {
        $job_id = (string) get_post_meta( $post_id, OP_WB_META_JOB_ID, true );
        if ( empty( $job_id ) ) continue;
        $url = onionpress_wayback_post_url( $post_id );
        $records[ $job_id ] = array(
            'key'          => 'post:' . $post_id,
            'url'          => $url,
            'submitted_at' => (int) get_post_meta( $post_id, OP_WB_META_SUBMITTED_AT, true ),
            'write'        => function( $patch ) use ( $post_id ) {
                onionpress_wayback_post_write( $post_id, $patch );
            },
        );
    }
    return $records;
}

/**
 * Home + feed as work records, matching the shape used for posts.
 * Returns a mixed list: some awaiting submission, some in-flight. The
 * sweep checks each record's state to decide what to do.
 */
function onionpress_wayback_sitewide_records() {
    $records = array();
    $home = onionpress_wayback_home_url_full();
    $feed = onionpress_wayback_feed_url_full();
    $items = array();
    if ( $home ) $items[ OP_WB_OPT_HOME ] = $home;
    if ( $feed ) $items[ OP_WB_OPT_FEED ] = $feed;
    foreach ( $items as $opt_key => $url ) {
        $records[] = array(
            'key'   => 'opt:' . $opt_key,
            'url'   => $url,
            'read'  => function() use ( $opt_key ) {
                return onionpress_wayback_opt_read( $opt_key );
            },
            'write' => function( $patch ) use ( $opt_key ) {
                onionpress_wayback_opt_write( $opt_key, $patch );
            },
        );
    }
    return $records;
}

// ──────────────────────────── sweep ─────────────────────────────────

/**
 * Entry point wired to wp-cron. Runs a continuous inner loop for up
 * to OP_WB_LOOP_MAX_SEC, so one cron invocation can drive many sweep
 * iterations. Exits early when:
 *   - queue fully drained (no posts with job_id=null, archived_at=null)
 *   - a gate tells us to back off for longer than remaining budget
 *
 * Next cron tick restarts us. This is the "daemon with cron watchdog"
 * pattern — cron only has to fire occasionally to keep the process
 * alive; all the real pacing is inside the inner loop.
 */
function onionpress_wayback_sweep() {
    // Single-process mutex. The lock value is "<token>:<last_heartbeat_ts>".
    // - token: unique per invocation; lets each daemon verify ownership
    //   on each iteration and gracefully exit if another has taken over.
    // - last_heartbeat_ts: updated every iteration; a stale ts means
    //   the prior process is dead and we take over.
    $lock_key = 'op_wayback_sweep_lock';
    $raw      = (string) get_option( $lock_key, '' );
    $now      = time();
    if ( $raw !== '' && strpos( $raw, ':' ) !== false ) {
        list( , $lock_ts_str ) = explode( ':', $raw, 2 );
        $lock_ts = (int) $lock_ts_str;
        if ( $lock_ts > 0 && ( $now - $lock_ts ) < OP_WB_LOOP_LOCK_STALE_SEC ) {
            return; // another invocation is still alive
        }
    } elseif ( $raw !== '' ) {
        // Legacy lock value (plain ts). Treat like the new format.
        $lock_ts = (int) $raw;
        if ( $lock_ts > 0 && ( $now - $lock_ts ) < OP_WB_LOOP_LOCK_STALE_SEC ) {
            return;
        }
    }

    // Claim the lock with our own unique token.
    $token = wp_generate_password( 16, false, false );
    update_option( $lock_key, $token . ':' . $now, false );

    @set_time_limit( 0 );
    @ignore_user_abort( true );

    try {
        onionpress_wayback_sweep_loop( $token );
    } finally {
        // Only clear the lock if it's still ours — otherwise another
        // daemon has taken over and needs its lock preserved.
        $cur = (string) get_option( $lock_key, '' );
        if ( strpos( $cur, $token . ':' ) === 0 ) {
            delete_option( $lock_key );
        }
    }
}

/**
 * Sum queue totals across every subsite in the network. Returns an
 * array with 'archived', 'in_flight', 'remaining', and 'total' post
 * counts (counting publish posts + pages only).
 */
function onionpress_wayback_queue_totals() {
    $out = array( 'archived' => 0, 'in_flight' => 0, 'remaining' => 0, 'total' => 0 );
    $sites = function_exists( 'get_sites' ) ? get_sites() : array();
    if ( empty( $sites ) ) {
        $sites = array( (object) array( 'blog_id' => get_current_blog_id() ) );
    }
    foreach ( $sites as $site ) {
        $bid = (int) $site->blog_id;
        if ( function_exists( 'switch_to_blog' ) ) switch_to_blog( $bid );
        try {
            global $wpdb;
            $prefix = $wpdb->get_blog_prefix( $bid );
            $posts_table = $prefix . 'posts';
            $meta_table  = $prefix . 'postmeta';
            $total     = (int) $wpdb->get_var( "SELECT COUNT(*) FROM {$posts_table} WHERE post_status='publish' AND post_type IN ('post','page')" );
            $archived  = (int) $wpdb->get_var( $wpdb->prepare(
                "SELECT COUNT(DISTINCT m.post_id) FROM {$meta_table} m JOIN {$posts_table} p ON p.ID=m.post_id "
                . "WHERE m.meta_key=%s AND p.post_status='publish' AND p.post_type IN ('post','page')",
                OP_WB_META_ARCHIVED_AT
            ) );
            $in_flight = (int) $wpdb->get_var( $wpdb->prepare(
                "SELECT COUNT(DISTINCT m.post_id) FROM {$meta_table} m JOIN {$posts_table} p ON p.ID=m.post_id "
                . "WHERE m.meta_key=%s AND p.post_status='publish' AND p.post_type IN ('post','page')",
                OP_WB_META_JOB_ID
            ) );
            $out['total']     += $total;
            $out['archived']  += $archived;
            $out['in_flight'] += $in_flight;
        } finally {
            if ( function_exists( 'restore_current_blog' ) ) restore_current_blog();
        }
    }
    $out['remaining'] = max( 0, $out['total'] - $out['archived'] - $out['in_flight'] );
    return $out;
}

function onionpress_wayback_sweep_loop( $token ) {
    $lock_key     = 'op_wayback_sweep_lock';
    $loop_start   = microtime( true );
    $totals       = array( 'submitted' => 0, 'success' => 0, 'cdx' => 0, 'error' => 0 );
    $last_progress = $loop_start;

    onionpress_wayback_log( 'Loop: starting daemon sweep (token=' . substr( $token, 0, 6 ) . ')' );
    $iter = 0;
    while ( true ) {
        $iter++;
        // Lock ownership check — if another daemon has claimed the
        // lock (possible if our heartbeat lapsed past stale threshold),
        // exit gracefully. Also heartbeats the ts if we still own it.
        $cur = (string) get_option( $lock_key, '' );
        if ( strpos( $cur, $token . ':' ) !== 0 ) {
            onionpress_wayback_log( 'Loop: lock taken by another daemon, exiting (token=' . substr( $token, 0, 6 ) . ', iter=' . $iter . ')' );
            return;
        }
        update_option( $lock_key, $token . ':' . time(), false );

        // Visit every subsite in the network. The daemon may have been
        // invoked from any site's cron; we need to do work on whichever
        // subsite actually has unarchived posts. Skip subsites whose
        // queue is fully drained — they exit the iteration for free.
        $sites = function_exists( 'get_sites' ) ? get_sites() : array();
        if ( empty( $sites ) ) {
            // Not multisite — fall back to single-site check.
            $sites = array( (object) array( 'blog_id' => get_current_blog_id() ) );
        }

        $any_work = false;
        foreach ( $sites as $site ) {
            $bid = (int) $site->blog_id;
            if ( function_exists( 'switch_to_blog' ) ) {
                switch_to_blog( $bid );
            }
            try {
                $remaining = get_posts( array(
                    'post_status' => 'publish',
                    'post_type'   => array( 'post', 'page' ),
                    'numberposts' => 1,
                    'meta_query'  => array(
                        'relation' => 'AND',
                        array( 'key' => OP_WB_META_ARCHIVED_AT, 'compare' => 'NOT EXISTS' ),
                        array( 'key' => OP_WB_META_JOB_ID,      'compare' => 'NOT EXISTS' ),
                    ),
                    'fields' => 'ids',
                ) );
                $in_flight = get_posts( array(
                    'post_status' => 'publish',
                    'post_type'   => array( 'post', 'page' ),
                    'numberposts' => 1,
                    'meta_query'  => array(
                        array( 'key' => OP_WB_META_JOB_ID, 'compare' => 'EXISTS' ),
                    ),
                    'fields' => 'ids',
                ) );
                if ( empty( $remaining ) && empty( $in_flight ) ) {
                    // No work on this subsite — skip.
                    continue;
                }
                $any_work = true;
                $stats = onionpress_wayback_sweep_iteration();
                if ( is_array( $stats ) ) {
                    foreach ( $totals as $k => $_ ) {
                        $totals[ $k ] += (int) ( $stats[ $k ] ?? 0 );
                    }
                }
            } finally {
                if ( function_exists( 'restore_current_blog' ) ) {
                    restore_current_blog();
                }
            }
        }

        if ( ! $any_work ) {
            $runtime = (int) ( microtime( true ) - $loop_start );
            onionpress_wayback_log( sprintf(
                'Loop: drained — %d iterations, %dm%02ds, submitted=%d archived=%d cdx-hit=%d errors=%d',
                $iter, intdiv( $runtime, 60 ), $runtime % 60,
                $totals['submitted'], $totals['success'], $totals['cdx'], $totals['error']
            ) );
            return;
        }

        // Periodic progress line — every 2 min of wall clock so the log
        // shows captures/min reliably without waiting for drain.
        // Includes queue-wide archived/remaining totals across all
        // subsites for a complete snapshot in one line.
        if ( microtime( true ) - $last_progress >= 120 ) {
            $last_progress = microtime( true );
            $runtime = max( 1, (int) ( microtime( true ) - $loop_start ) );
            $caps    = $totals['success'] + $totals['cdx'];
            $cap_per_min = round( ( $caps * 60 ) / $runtime, 1 );
            $sub_per_min = round( ( $totals['submitted'] * 60 ) / $runtime, 1 );
            $qstats = onionpress_wayback_queue_totals();
            onionpress_wayback_log( sprintf(
                'Loop progress: %dm%02ds iter=%d | archived=%d/%d (%s/min captured, %s remaining) | submitted=%d (%s/min) cdx=%d errors=%d',
                intdiv( $runtime, 60 ), $runtime % 60, $iter,
                $qstats['archived'], $qstats['total'],
                $cap_per_min, $qstats['remaining'],
                $totals['submitted'], $sub_per_min,
                $totals['cdx'], $totals['error']
            ) );
        }

        // Pace between iterations. Also respect any backoff set by a
        // gate inside the iteration (available=0, 429, etc.).
        $now = time();
        $backoff_until = (int) get_option( OP_WB_OPT_BACKOFF_UNTIL, 0 );
        $sleep = ( $backoff_until > $now )
            ? ( $backoff_until - $now )
            : OP_WB_LOOP_IDLE_SLEEP;
        sleep( max( 5, $sleep ) );
    }
}

/**
 * One sweep iteration. Formerly the body of onionpress_wayback_sweep;
 * called in a loop from the new entry point above.
 */
function onionpress_wayback_sweep_iteration() {
    $now = time();

    // Global back-off gate — either we recently saw available=0, failed
    // a self-reachability check, or SPN status was unreachable.
    $backoff_until = (int) get_option( OP_WB_OPT_BACKOFF_UNTIL, 0 );
    if ( $backoff_until > $now ) {
        return;
    }

    $onion = onionpress_wayback_onion_addr();
    if ( empty( $onion ) ) {
        onionpress_wayback_log( 'Sweep skipped: onion address not ready' );
        return;
    }

    // Gate: do we have SPN slots? If the check itself fails (Tor
    // jitter, SPN briefly unreachable), assume we have slots and
    // proceed. The worst case is one wasted submit batch — better
    // than a 60-120s global backoff starving every sweep while we
    // wait for Tor to recover. If the account is genuinely at
    // capacity, the submits will return 429 and we'll back off then.
    $user = onionpress_wayback_user_status();
    $available = ( $user === null )
        ? OP_WB_SUBMIT_BATCH_MAX // optimistic default
        : (int) ( $user['available'] ?? 0 );
    if ( $user !== null && $available <= 0 ) {
        update_option( OP_WB_OPT_BACKOFF_UNTIL, $now + OP_WB_BACKOFF_NO_SLOTS, false );
        onionpress_wayback_log( 'Sweep paused: available=0 processing='
            . ( $user['processing'] ?? '?' ) . ', backing off ' . OP_WB_BACKOFF_NO_SLOTS . 's' );
        return;
    }

    $deadline = microtime( true ) + OP_WB_SWEEP_BUDGET_SEC;

    // ---- Step A: poll all outstanding job_ids in batches ----
    $in_flight = onionpress_wayback_posts_with_in_flight();
    // Add home + feed in-flight jobs
    foreach ( onionpress_wayback_sitewide_records() as $rec ) {
        $state = $rec['read']();
        if ( ! empty( $state['job_id'] ) && empty( $state['archived_at'] ) ) {
            $in_flight[ $state['job_id'] ] = array(
                'key'          => $rec['key'],
                'url'          => $rec['url'],
                'submitted_at' => (int) ( $state['submitted_at'] ?? 0 ),
                'write'        => $rec['write'],
            );
        }
    }

    // Skip jobs younger than OP_WB_YOUNG_JOB_SKIP_SEC — SPN's minimum
    // capture time is ~20s, so polling any sooner is a wasted round-trip.
    $ripe_job_ids = array();
    foreach ( $in_flight as $jid => $rec ) {
        $age = $rec['submitted_at'] ? ( $now - $rec['submitted_at'] ) : PHP_INT_MAX;
        if ( $age >= OP_WB_YOUNG_JOB_SKIP_SEC ) {
            $ripe_job_ids[] = $jid;
        }
    }

    $polled_success = 0;
    $polled_error   = 0;
    $polled_pending = 0;
    $polled_cdx_hit = 0;
    $results        = onionpress_wayback_poll_parallel( $ripe_job_ids );

    // First pass: finalize successes and pending; collect error-jobs for
    // a CDX fallback check. SPN's job memory is unreliable — it flips
    // success→error after a few minutes even when the capture persists
    // in CDX. Before trashing a post we verify against CDX directly.
    $cdx_check_urls  = array();
    $cdx_check_recs  = array();
    $cdx_check_exts  = array();
    foreach ( $results as $res ) {
        if ( ! is_array( $res ) ) continue;
        $jid = (string) ( $res['job_id'] ?? '' );
        if ( ! isset( $in_flight[ $jid ] ) ) continue;
        $rec    = $in_flight[ $jid ];
        $status = (string) ( $res['status'] ?? '' );

        if ( $status === 'success' ) {
            onionpress_wayback_finalize_success( $rec['write'], $rec['url'], $res );
            $rec['write']( array( 'job_id' => '', 'submitted_at' => '', 'last_error_ext' => '', 'last_error_at' => '' ) );
            $polled_success++;
            onionpress_wayback_log( 'Archived ' . $rec['key'] . ' ts=' . (string) ( $res['timestamp'] ?? '' )
                . ' dur=' . (string) ( $res['duration_sec'] ?? '' ) );
        } elseif ( $status === 'error' ) {
            $ext = (string) ( $res['status_ext'] ?? 'error' );
            // Queue for CDX verification before deciding this is a real loss.
            $cdx_check_urls[ $jid ] = $rec['url'];
            $cdx_check_recs[ $jid ] = $rec;
            $cdx_check_exts[ $jid ] = $ext;
        } else {
            // Clear job_id if either:
            //  (a) submitted_at is set and older than STALE_PENDING_SEC — SPN
            //      should have resolved by now, something's stuck.
            //  (b) submitted_at is missing — v3-era zombie with a job_id SPN
            //      no longer remembers; it will poll "pending" forever.
            $age    = $rec['submitted_at'] ? ( $now - $rec['submitted_at'] ) : null;
            $stale  = $age !== null && $age > OP_WB_STALE_PENDING_SEC;
            $zombie = $age === null;
            if ( $stale || $zombie ) {
                $rec['write']( array( 'job_id' => '', 'submitted_at' => '' ) );
                onionpress_wayback_log( 'SPN stale-pending ' . $rec['key']
                    . ( $zombie ? ' (zombie, no submitted_at)' : ' (age=' . $age . 's)' )
                    . ', clearing for resubmit' );
            } else {
                $polled_pending++;
            }
        }
    }

    // Second pass: for each SPN-errored job, verify against Wayback's
    // /wayback/available (with CDX fallback). If there's a capture,
    // mark archived; otherwise resubmit next tick.
    //
    // Cap the rescue burst so Tor SOCKS stays responsive to other
    // consumers (heartbeat, reachability). Jobs beyond the cap get
    // their error recorded and job_id cleared immediately — they'll
    // run through rescue on a later sweep when they're re-errored.
    $cdx_do_now  = array_slice( $cdx_check_urls, 0, 5, true );
    $cdx_defer   = array_slice( $cdx_check_urls, 5, null, true );
    foreach ( $cdx_defer as $jid => $url ) {
        $rec = $cdx_check_recs[ $jid ];
        onionpress_wayback_finalize_error( $rec['write'], $cdx_check_exts[ $jid ] );
        $rec['write']( array( 'job_id' => '', 'submitted_at' => '' ) );
        $polled_error++;
    }
    if ( ! empty( $cdx_do_now ) ) {
        $cdx = onionpress_wayback_cdx_lookup_parallel( $cdx_do_now );
        foreach ( $cdx_do_now as $jid => $url ) {
            $rec = $cdx_check_recs[ $jid ];
            $ts  = (string) ( $cdx[ $jid ] ?? '' );
            if ( $ts !== '' ) {
                $rec['write']( array(
                    'archived_at'    => time(),
                    'snapshot_ts'    => $ts,
                    'job_id'         => '',
                    'submitted_at'   => '',
                    'last_error_ext' => '',
                    'last_error_at'  => '',
                ) );
                $polled_cdx_hit++;
                onionpress_wayback_log( 'CDX rescued ' . $rec['key'] . ' ts=' . $ts
                    . ' (SPN said ' . $cdx_check_exts[ $jid ] . ')' );
            } else {
                onionpress_wayback_finalize_error( $rec['write'], $cdx_check_exts[ $jid ] );
                $rec['write']( array( 'job_id' => '', 'submitted_at' => '' ) );
                $polled_error++;
            }
        }
    }

    // ---- Step B: submit fresh work up to available slots, in parallel ----
    // SPN's `available` is already net of `processing`, so we use it as
    // the submission budget directly. Sites-wide (home + feed) first so
    // they never starve, then fresh post URLs.
    $budget = max( 0, min( OP_WB_SUBMIT_BATCH_MAX, $available ) );
    $to_submit = array();      // map: key → url
    $records_by_key = array(); // map: key → record (for write-back)

    foreach ( onionpress_wayback_sitewide_records() as $rec ) {
        if ( count( $to_submit ) >= $budget ) break;
        $state = $rec['read']();
        if ( ! empty( $state['archived_at'] ) ) continue;
        if ( ! empty( $state['job_id'] ) ) continue;
        $to_submit[ $rec['key'] ] = $rec['url'];
        $records_by_key[ $rec['key'] ] = $rec;
    }

    if ( count( $to_submit ) < $budget ) {
        $posts = onionpress_wayback_posts_needing_submit( $budget - count( $to_submit ) );
        foreach ( $posts as $rec ) {
            if ( count( $to_submit ) >= $budget ) break;
            $to_submit[ $rec['key'] ] = $rec['url'];
            $records_by_key[ $rec['key'] ] = $rec;
        }
    }

    $submitted = 0;
    $hit_429   = false;
    if ( ! empty( $to_submit ) ) {
        $submit_results = onionpress_wayback_submit_parallel( $to_submit );
        foreach ( $submit_results as $key => $result ) {
            $rec = $records_by_key[ $key ] ?? null;
            if ( $rec === null ) continue;
            if ( $result === 'RATE_LIMITED' ) {
                $hit_429 = true;
                continue;
            }
            if ( $result !== '' ) {
                $rec['write']( array( 'job_id' => $result, 'submitted_at' => time() ) );
                $submitted++;
            }
        }
        if ( $hit_429 ) {
            update_option( OP_WB_OPT_BACKOFF_UNTIL, time() + OP_WB_BACKOFF_NO_SLOTS, false );
            onionpress_wayback_log( 'Submit batch hit 429, backing off ' . OP_WB_BACKOFF_NO_SLOTS . 's' );
        }
    }

    $elapsed = round( microtime( true ) - ( $deadline - OP_WB_SWEEP_BUDGET_SEC ), 2 );
    onionpress_wayback_log( sprintf(
        'Sweep: avail=%d polled(success=%d cdx-hit=%d err=%d pending=%d skipped-young=%d) submitted=%d elapsed=%ss',
        $available, $polled_success, $polled_cdx_hit, $polled_error, $polled_pending,
        count( $in_flight ) - count( $ripe_job_ids ), $submitted, $elapsed
    ) );

    return array(
        'submitted' => $submitted,
        'success'   => $polled_success,
        'cdx'       => $polled_cdx_hit,
        'error'     => $polled_error,
    );
}
add_action( 'onionpress_wayback_sweep', 'onionpress_wayback_sweep' );

// ────────────── save_post hook: invalidate home/feed + retry post ───
// Imported social posts (Twitter/Mastodon/Bluesky — anything with a
// `_source_id` meta) are captured exactly once and never re-archived
// on subsequent save_post events. Their content is frozen historical
// data, but the importers themselves call wp_update_post repeatedly
// (media-attach, thread fix-ups, etc.); treating those as "edit →
// re-capture" produced rounds of duplicate Wayback submissions for
// posts whose original content never changed.
//
// Original posts (no `_source_id`) keep the "edit → re-capture"
// behaviour — for hand-written blog content that's the right thing.
//
// Home page + feed always re-archive when content changes, regardless
// of source — they're a moving window that legitimately changes.

add_action( 'save_post', function ( $post_id, $post, $update ) {
    if ( defined( 'DOING_AUTOSAVE' ) && DOING_AUTOSAVE ) return;
    if ( wp_is_post_revision( $post_id ) ) return;
    if ( $post->post_status !== 'publish' ) return;
    if ( ! in_array( $post->post_type, array( 'post', 'page' ), true ) ) return;

    // Home page + feed content just changed — invalidate their captures.
    delete_option( OP_WB_OPT_HOME );
    delete_option( OP_WB_OPT_FEED );

    // Re-archive on edit, but only for original posts. Imported social
    // posts (anything with _source_id) stay archived once.
    $is_imported = (string) get_post_meta( $post_id, '_source_id', true ) !== '';
    if ( $update && ! $is_imported && get_post_meta( $post_id, OP_WB_META_ARCHIVED_AT, true ) ) {
        onionpress_wayback_post_write( $post_id, array(
            'archived_at'     => '',
            'snapshot_ts'     => '',
            'job_id'          => '',
            'submitted_at'    => '',
            'original_url'    => '',
            'duration_sec'    => '',
            'resources_count' => '',
            'outlinks_count'  => '',
            'last_error_ext'  => '',
            'last_error_at'   => '',
        ) );
    }

    // Also clear any site-wide back-off so the immediate sweep runs.
    delete_option( OP_WB_OPT_BACKOFF_UNTIL );

    wp_schedule_single_event( time(), 'onionpress_wayback_sweep' );
    onionpress_wayback_log( 'save_post ' . $post_id . ': cleared home/feed'
        . ( $update && ! $is_imported ? ' + post meta' : '' ) . ', scheduled immediate sweep' );
}, 10, 3 );

// ────────────────────────────── cron ────────────────────────────────

add_filter( 'cron_schedules', function ( $schedules ) {
    $schedules['onionpress_wayback_watchdog'] = array(
        'interval' => OP_WB_CRON_INTERVAL,
        'display'  => 'OnionPress Wayback watchdog',
    );
    return $schedules;
} );

add_action( 'init', function () {
    // (Re)schedule the sweep on the current watchdog schedule. Unschedule
    // any prior-schedule instances so we don't end up with two cron
    // entries for the same hook under different intervals.
    $existing = wp_next_scheduled( 'onionpress_wayback_sweep' );
    if ( $existing ) {
        $cron = _get_cron_array();
        foreach ( (array) $cron as $ts => $hooks ) {
            if ( isset( $hooks['onionpress_wayback_sweep'] ) ) {
                foreach ( $hooks['onionpress_wayback_sweep'] as $sig => $entry ) {
                    if ( ( $entry['schedule'] ?? '' ) !== 'onionpress_wayback_watchdog' ) {
                        wp_unschedule_event( $ts, 'onionpress_wayback_sweep', $entry['args'] ?? array() );
                    }
                }
            }
        }
    }
    if ( ! wp_next_scheduled( 'onionpress_wayback_sweep' ) ) {
        wp_schedule_event( time(), 'onionpress_wayback_watchdog', 'onionpress_wayback_sweep' );
    }

    // One-time v3 → v4 migration: drop the retry-machine postmeta we no
    // longer use. Preserve archived_at + snapshot_ts (the only real
    // outcome record) and job_id (active in-flight work).
    if ( get_option( 'op_wayback_v4_migrated' ) !== 'yes' ) {
        global $wpdb;
        $stale_keys = array(
            '_op_wayback_retry_count',
            '_op_wayback_retry_after',
            '_op_wayback_failed_at',
            '_op_wayback_failed_reason',
        );
        foreach ( $stale_keys as $k ) {
            $wpdb->delete( $wpdb->postmeta, array( 'meta_key' => $k ) );
        }
        @unlink( '/var/lib/onionpress/wayback-queue.json' );
        @unlink( '/var/lib/onionpress/wayback-archived.json' );

        $legacy_ts = wp_next_scheduled( 'onionpress_drain_wayback_queue' );
        if ( $legacy_ts ) {
            wp_unschedule_event( $legacy_ts, 'onionpress_drain_wayback_queue' );
        }
        update_option( 'op_wayback_v4_migrated', 'yes' );
        onionpress_wayback_log( 'v4 migration: stale retry-state meta cleared' );
    }
} );

// ───────────────────────── admin page ───────────────────────────────

/**
 * Register a Wayback admin submenu under the Social Archive top-level
 * menu. Uses late priority (20) so the Social Archive plugin has
 * registered the parent menu first.
 */
add_action( 'admin_menu', function () {
    if ( ! defined( 'ONIONPRESS_SOCIAL_ADMIN_SLUG' ) ) {
        // Social Archive plugin not loaded — fall back to a top-level menu.
        add_menu_page(
            'Wayback Archive',
            'Wayback Archive',
            'manage_options',
            'onionpress-wayback',
            'onionpress_wayback_admin_page',
            'dashicons-backup',
            26
        );
        return;
    }
    add_submenu_page(
        ONIONPRESS_SOCIAL_ADMIN_SLUG,
        'Wayback Archive',
        'Wayback',
        'manage_options',
        'onionpress-wayback',
        'onionpress_wayback_admin_page'
    );
}, 20 );

/**
 * Handle POST actions from the admin page (kick daemon, clear lock).
 * Registered before admin_menu render so redirects fire cleanly.
 */
add_action( 'admin_post_onionpress_wayback_action', function () {
    if ( ! current_user_can( 'manage_options' ) ) {
        wp_die( 'Forbidden', 403 );
    }
    check_admin_referer( 'onionpress_wayback_action' );

    $action = sanitize_text_field( $_POST['op_action'] ?? '' );
    switch ( $action ) {
        case 'kick':
            // Clear the lock and any backoff, then trigger the sweep
            // directly. If the sweep is already running under a valid
            // lock, the token-ownership check will make this invocation
            // exit cleanly without disrupting it — but clearing the
            // lock first breaks any truly-stuck state.
            delete_option( 'op_wayback_sweep_lock' );
            delete_option( OP_WB_OPT_BACKOFF_UNTIL );
            // Clear WP's `doing_cron` lock too. If a previous wp-cron
            // spawn died mid-run without cleaning up (container restart,
            // pkill), this transient blocks new wp-cron fires for up to
            // 60s from its timestamp. Each subsequent page load just
            // refreshes it via race, so it can stay stuck for a long
            // time in practice. Clearing it lets cron fire immediately.
            delete_transient( 'doing_cron' );
            // Schedule an immediate cron fire so the sweep starts in a
            // separate request (admin page doesn't hang for the whole
            // daemon run).
            wp_schedule_single_event( time(), 'onionpress_wayback_sweep' );
            // Also spawn a loopback to wp-cron.php so WP actually runs
            // scheduled events right now (WP-Cron only fires on HTTP).
            $cron_url = site_url( 'wp-cron.php?doing_wp_cron=' . microtime( true ) );
            wp_remote_post( $cron_url, array( 'timeout' => 0.01, 'blocking' => false, 'sslverify' => false ) );
            $msg = 'Daemon kicked — archiving will begin within a few seconds.';
            break;
        case 'clear_backoff':
            delete_option( OP_WB_OPT_BACKOFF_UNTIL );
            $msg = 'Backoff cleared.';
            break;
        case 'clear_lock':
            delete_option( 'op_wayback_sweep_lock' );
            $msg = 'Lock cleared. A stuck daemon (if any) will exit on its next heartbeat.';
            break;
        default:
            $msg = '';
    }
    wp_safe_redirect( add_query_arg(
        array( 'op_msg' => rawurlencode( $msg ) ),
        admin_url( 'admin.php?page=onionpress-wayback' )
    ) );
    exit;
} );

function onionpress_wayback_admin_page() {
    $totals        = onionpress_wayback_queue_totals();
    $backoff_until = (int) get_option( OP_WB_OPT_BACKOFF_UNTIL, 0 );
    $backoff_secs  = max( 0, $backoff_until - time() );
    $lock_raw      = (string) get_option( 'op_wayback_sweep_lock', '' );
    $lock_ts       = 0;
    $lock_token    = '';
    if ( strpos( $lock_raw, ':' ) !== false ) {
        list( $lock_token, $lock_ts_str ) = explode( ':', $lock_raw, 2 );
        $lock_ts = (int) $lock_ts_str;
    }
    $lock_age      = $lock_ts > 0 ? time() - $lock_ts : 0;
    $lock_active   = $lock_age > 0 && $lock_age < OP_WB_LOOP_LOCK_STALE_SEC;
    $next_cron     = wp_next_scheduled( 'onionpress_wayback_sweep' );
    $msg           = isset( $_GET['op_msg'] ) ? wp_unslash( $_GET['op_msg'] ) : '';
    $pct           = $totals['total'] > 0 ? round( $totals['archived'] * 100 / $totals['total'], 1 ) : 0;

    // Pull the most recent N log lines written by error_log.
    ?>
    <div class="wrap">
        <h1>Wayback Archive</h1>

        <?php if ( $msg ) : ?>
            <div class="notice notice-success is-dismissible"><p><?php echo esc_html( $msg ); ?></p></div>
        <?php endif; ?>

        <?php
        // Context-aware Wayback link: onion when viewing via .onion,
        // clearnet otherwise. Uses the helper from the Social Archive
        // plugin if loaded, else builds inline.
        if ( function_exists( 'onionpress_social_wayback_home_url' ) ) {
            $wb_home = onionpress_social_wayback_home_url();
        } else {
            $host = (string) ( $_SERVER['HTTP_HOST'] ?? '' );
            $wb_home = substr( $host, -6 ) === '.onion'
                ? 'https://web.archivep75mbjunhxc6x4j5mwjmomyxb573v42baldlqu56ruil2oiad.onion/'
                : 'https://web.archive.org/';
        }
        ?>
        <p>This site continuously submits its posts to the
            <a href="<?php echo esc_url( $wb_home ); ?>" target="_blank" rel="noopener">Internet Archive's Wayback Machine</a>
            via Save Page Now. The captures persist independent of this onion — so even if
            this server goes offline, your posts remain publicly accessible via
            <code>web.archive.org/web/&lt;timestamp&gt;/&lt;post-url&gt;</code>.</p>

        <h2>Progress</h2>
        <table class="wp-list-table widefat striped" style="max-width:720px;">
            <tbody>
                <tr>
                    <th style="width:220px;">Total posts</th>
                    <td><?php echo number_format_i18n( $totals['total'] ); ?></td>
                </tr>
                <tr>
                    <th>Archived in Wayback</th>
                    <td><strong><?php echo number_format_i18n( $totals['archived'] ); ?></strong>
                        <?php if ( $totals['total'] > 0 ) : ?>
                            &middot; <?php echo esc_html( $pct ); ?>%
                        <?php endif; ?>
                    </td>
                </tr>
                <tr>
                    <th>In flight at SPN</th>
                    <td><?php echo number_format_i18n( $totals['in_flight'] ); ?> (submitted, awaiting capture result)</td>
                </tr>
                <tr>
                    <th>Remaining</th>
                    <td><?php echo number_format_i18n( $totals['remaining'] ); ?></td>
                </tr>
            </tbody>
        </table>

        <h2>Daemon status</h2>
        <table class="wp-list-table widefat striped" style="max-width:720px;">
            <tbody>
                <tr>
                    <th style="width:220px;">Daemon</th>
                    <td>
                        <?php if ( $lock_active ) : ?>
                            <span style="color:#008000;">● Running</span>
                            &middot; token <code><?php echo esc_html( substr( $lock_token, 0, 6 ) ); ?></code>
                            &middot; last heartbeat <?php echo esc_html( $lock_age ); ?>s ago
                        <?php elseif ( $lock_ts > 0 ) : ?>
                            <span style="color:#a00;">● Stale lock</span>
                            (<?php echo esc_html( $lock_age ); ?>s since last heartbeat;
                            next cron fire will take over)
                        <?php else : ?>
                            <span style="color:#666;">● Idle</span>
                            — waits for next cron fire or the Kick button below
                        <?php endif; ?>
                    </td>
                </tr>
                <tr>
                    <th>Back-off</th>
                    <td>
                        <?php if ( $backoff_secs > 0 ) : ?>
                            <span style="color:#a00;">Active</span> — pauses sweeps for <?php echo esc_html( $backoff_secs ); ?> more seconds.
                            Typically set when SPN reports <code>available=0</code> or a transport error.
                        <?php else : ?>
                            <span style="color:#008000;">None</span>
                        <?php endif; ?>
                    </td>
                </tr>
                <tr>
                    <th>Next scheduled cron</th>
                    <td>
                        <?php if ( $next_cron ) : ?>
                            <?php echo esc_html( human_time_diff( time(), $next_cron ) ); ?>
                            (<?php echo esc_html( date( 'H:i:s', $next_cron ) ); ?>)
                        <?php else : ?>
                            <em>Not scheduled — will be re-registered on next page load.</em>
                        <?php endif; ?>
                    </td>
                </tr>
            </tbody>
        </table>

        <h2>Actions</h2>
        <form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>" style="display:inline-block; margin-right:8px;">
            <?php wp_nonce_field( 'onionpress_wayback_action' ); ?>
            <input type="hidden" name="action" value="onionpress_wayback_action">
            <input type="hidden" name="op_action" value="kick">
            <?php submit_button( 'Kick the daemon now', 'primary', 'submit', false ); ?>
        </form>
        <form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>" style="display:inline-block; margin-right:8px;">
            <?php wp_nonce_field( 'onionpress_wayback_action' ); ?>
            <input type="hidden" name="action" value="onionpress_wayback_action">
            <input type="hidden" name="op_action" value="clear_backoff">
            <?php submit_button( 'Clear backoff', 'secondary', 'submit', false ); ?>
        </form>
        <form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>" style="display:inline-block;">
            <?php wp_nonce_field( 'onionpress_wayback_action' ); ?>
            <input type="hidden" name="action" value="onionpress_wayback_action">
            <input type="hidden" name="op_action" value="clear_lock">
            <?php submit_button( 'Clear lock (force takeover)', 'secondary', 'submit', false ); ?>
        </form>

        <h2>How it works</h2>
        <p>A background daemon runs continuously, batching up to 40 post URLs per iteration
            through Save Page Now. Each submission returns a <code>job_id</code>;
            a subsequent poll tells us <code>success</code> (capture made),
            <code>pending</code> (SPN still crawling) or <code>error</code> (SPN crawler
            couldn't reach the URL).</p>
        <p>On <code>error:no-captures</code> the daemon falls back to CDX (Wayback's own
            index): if Wayback already has the URL captured (possibly from a prior attempt),
            the post is marked archived without resubmission. Otherwise the post is requeued
            for the next pass.</p>
        <p>Outgoing SPN traffic is routed through a separate Tor instance
            (<code>onionheaven</code>) so that heavy archival bursts don't starve the Tor
            daemon serving your onion. The <strong>heartbeat</strong> to OnionHeaven keeps your
            address registered as <em>online</em>; if heartbeats lapse for too long,
            OnionHeaven activates a <strong>takeover redirector</strong> at your address — so
            visitors get a polite "this site is offline, try again later" page instead of a
            timeout. The moment your heartbeat resumes, the takeover lifts and your real
            WordPress serves again.</p>
        <p>If the daemon dies (crash, reboot, Mac sleep), the mutex lock goes stale after
            5 minutes and the next WordPress page view causes wp-cron to restart the daemon
            from the persisted cursor. No progress is lost; no duplicates are created.</p>
    </div>
    <?php
}

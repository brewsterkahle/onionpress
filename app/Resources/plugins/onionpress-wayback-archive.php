<?php
/**
 * Plugin Name: OnionPress Wayback Archive
 * Description: Archives the site's posts, home page, and RSS feed to the
 *              Internet Archive's Wayback Machine via Save Page Now (SPN2).
 *              When a moss-generated static site is serving, its own pages
 *              (which are files, not WordPress posts) and its own feed join
 *              the same queue in place of the WP boilerplate posts — see
 *              "the static site's own pages" below.
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

// Per-sweep wall-clock budget, enforced between phases (CDX rescue,
// submit). It used to be 45 and enforced nowhere — $deadline was computed
// and then only ever used to derive `elapsed` for the log — so the real
// worst case was unbounded: submit_parallel alone can run 8 sequential
// chunks at a 40s timeout.
//
// What the budget actually protects is the lock. The loop heartbeats
// op_wayback_sweep_lock around each iteration, so an iteration that
// outruns OP_WB_LOOP_LOCK_STALE_SEC lets a second daemon declare the lock
// dead and start up alongside this one, both writing the same records.
//
// The bound has to be stated as budget PLUS overshoot, because every gate
// is checked on phase entry and the phase it admits then runs to its own
// timeout. Each phase re-checks, so at most one can overshoot:
//   poll    entering at the line: 1 group             = +40s
//   CDX     entering at the line: 1 group             = +25s
//   submit  entering at the line: 20s probe + 1 group = +60s  <- worst
// So one iteration is at most BUDGET + 60. At the old 240 that is exactly
// 300 — the stale threshold, with no margin at all. 200 leaves 40s.
define( 'OP_WB_SWEEP_BUDGET_SEC', 200 );

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
// Hard ceiling on one daemon's lifetime. The comments above and on
// onionpress_wayback_sweep_loop have always described this cap, but the
// constant was never defined and the loop was `while (true)` with no
// time-based exit — so a daemon that always found work ran forever.
//
// That is not merely untidy. WordPress caches options and post queries
// per REQUEST, and this daemon is a single request; a process that never
// exits never refreshes those caches. One that had been alive 70 hours
// went on reading a job_id that had been deleted from the database hours
// earlier, and because a non-empty job_id is itself what marks the queue
// "still has work", the stale read kept the loop alive that was keeping
// the read stale. Recycling on a timer breaks that circuit: cron starts
// a fresh process with a fresh cache within a minute or two.
define( 'OP_WB_LOOP_MAX_SEC',      1800 ); // recycle the daemon every 30 min

// How long a self-reachability verdict may be reused. One iteration
// visits every subsite and asks once per subsite, but the answer belongs
// to the onion service, not the subsite. Short enough that a recovering
// onion is noticed within an iteration or two.
define( 'OP_WB_SELF_REACHABLE_TTL',  60 );

// Back-off durations (written to op_wayback_backoff_until option).
define( 'OP_WB_BACKOFF_NO_SLOTS',    20 );  // SPN says available=0
define( 'OP_WB_BACKOFF_UNREACHABLE', 120 ); // our own onion not responding
// No OP_WB_BACKOFF_SPN_DOWN: an unreachable /save/status/user does NOT
// back the sweep off. The gate below says why — a status call failing is
// usually Tor jitter, and pausing every sweep for it starves the queue
// while submits would still have gone through. The constant existed for
// years, referenced nowhere, implying a behaviour the code had rejected.

// Meta keys (kept compatible with v3 for the already-archived posts).
define( 'OP_WB_META_ARCHIVED_AT',     '_op_wayback_archived_at' );
define( 'OP_WB_META_SNAPSHOT_TS',     '_op_wayback_snapshot_ts' );
define( 'OP_WB_META_JOB_ID',          '_op_wayback_job_id' );
define( 'OP_WB_META_SUBMITTED_AT',    '_op_wayback_submitted_at' );
define( 'OP_WB_META_ORIGINAL_URL',    '_op_wayback_original_url' );
define( 'OP_WB_META_DURATION_SEC',    '_op_wayback_duration_sec' );
define( 'OP_WB_META_RESOURCES_COUNT', '_op_wayback_resources_count' );
define( 'OP_WB_META_OUTLINKS_COUNT',  '_op_wayback_outlinks_count' );
// How much of the page actually landed in the archive — see
// onionpress_wayback_resources_state(). A capture can succeed and still
// replay as a bare page, so "archived" alone is not a claim we can make.
define( 'OP_WB_META_RESOURCES_STATE', '_op_wayback_resources_state' );
// Values for the above.
define( 'OP_WB_RES_COMPLETE',   'complete' );   // SPN captured embeds too
define( 'OP_WB_RES_BARE',       'bare' );       // page only; will replay unstyled
define( 'OP_WB_RES_UNVERIFIED', 'unverified' ); // we never saw a resource list
define( 'OP_WB_META_LAST_ERROR_EXT',  '_op_wayback_last_error_ext' );
define( 'OP_WB_META_LAST_ERROR_AT',   '_op_wayback_last_error_at' );
// Set when the post has been re-archived once due to a comment being
// added (i.e. social-importer threading folded a reply into this post).
// Caps the comment-driven re-archive at one snapshot per post — without
// this, every comment a thread accumulates would re-trigger SPN, which
// is the budget waste the once-only policy was originally designed to
// avoid. With it: a thread of 12 self-replies re-archives the parent
// exactly once after the first reply lands.
define( 'OP_WB_META_RESNAPSHOT_DONE',  '_op_wayback_resnapshot_done' );

// wp_options keys.
define( 'OP_WB_OPT_HOME',          'op_wayback_home_state' );
define( 'OP_WB_OPT_FEED',          'op_wayback_feed_state' );
define( 'OP_WB_OPT_BACKOFF_UNTIL', 'op_wayback_backoff_until' );

// Every page of the live static generation keeps its capture state in this ONE
// option, shaped { generation: '<id>', urls: { '<path>': {job_id, archived_at,
// …} } }. One row rather than one option per page: the set is bounded (see
// OP_WB_STATIC_MAX_PAGES), the whole thing is read and written together, and
// the sweep holds a single-process lock so there is no concurrent writer to
// lose an update to.
//
// The generation id rides along because it is what makes a publish re-archive.
// A row only answers for the generation it was written under, so a new id
// silently retires every row at once — no reset call has to fire, and nothing
// depends on the receiver's commit route remembering to clear anything. The
// same comparison is what makes a re-commit of the SAME generation harmless:
// the ids match, so rows survive and a capture still in flight is not thrown
// away. See onionpress_wayback_static_write().
define( 'OP_WB_OPT_STATIC',        'op_wayback_static_state' );

// Ceiling on how many static pages are tracked. This is a storage bound, not a
// throughput one — the sweep already drains whatever it enumerates across
// iterations at OP_WB_SUBMIT_BATCH_MAX a time. At roughly 150 serialized bytes
// per archived page, 1000 pages is ~150 kB in one non-autoloaded option, which
// is unremarkable; a hundred thousand would not be. Pages past the cap are not
// archived, and the enumeration logs that rather than dropping them silently.
define( 'OP_WB_STATIC_MAX_PAGES', 1000 );

// How many files the fallback directory walk may look at before giving up on
// the pages it has found so far. Only reached when a generation ships no
// sitemap.xml AND carries a very large tree.
define( 'OP_WB_STATIC_SCAN_MAX', 20000 );

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

/**
 * The feed URL to archive.
 *
 * `/feed/` is a WordPress route. When a static generation is serving, it has
 * replaced the whole site at the onion root and publishes its feed under its
 * own name — moss writes `/rss.xml` — and emits nothing at `/feed/` at all.
 * WordPress still answers `/feed/` with a boilerplate feed, 200 and all, so the
 * record looked healthy while archiving a feed no reader subscribes to.
 *
 * Decided by the file being on disk, not by "is a generation live": a generator
 * may legitimately ship no feed (moss gates rss.xml on its site_url being
 * deployed, so a site published before its onion name was registered has none),
 * and then WordPress's route really is the only feed that exists.
 *
 * Main site only, for the same reason the pages are: the generation sits at the
 * onion ROOT, so its feed is the main site's. A subsite's readers subscribe to
 * `http://<onion>/<subsite>/feed/`, which the generation neither publishes nor
 * replaces.
 */
function onionpress_wayback_feed_url_full() {
    $onion = onionpress_wayback_onion_addr();
    if ( empty( $onion ) ) {
        return '';
    }
    if ( onionpress_wayback_static_serves_this_site() ) {
        $gen = onionpress_wayback_static_generation();
        // Ordered by how likely a generator is to emit it. The first one
        // present wins; a generation shipping two feeds is not a case worth
        // guessing between, and picking deterministically at least keeps the
        // record's url stable across sweeps.
        foreach ( array( '/rss.xml', '/feed.xml', '/atom.xml' ) as $candidate ) {
            if ( $gen !== null && is_file( $gen['dir'] . $candidate ) ) {
                return 'http://' . $onion . $candidate;
            }
        }
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
    // Short-lived memo. The daemon calls this once per subsite per loop
    // iteration, and the answer is a property of the onion service, not of
    // the subsite — on a four-blog network that was four identical 20s Tor
    // round-trips inside one iteration, spent out of the same budget the
    // iteration has to finish in. Deliberately a TTL and not a plain
    // static: this process lives up to OP_WB_LOOP_MAX_SEC, and caching a
    // "down" verdict for half an hour would outlast the outage.
    static $memo = array(); // onion => array( expires_at, result )
    $mono = microtime( true );
    if ( isset( $memo[ $onion ] ) && $memo[ $onion ][0] > $mono ) {
        return $memo[ $onion ][1];
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
    $ok = ( $code === 200 || $code === 301 );
    $memo[ $onion ] = array( $mono + OP_WB_SELF_REACHABLE_TTL, $ok );
    return $ok;
}

/**
 * Age in seconds of an in-flight record, or null when we never recorded a
 * submitted_at (a v3-era row, or a write that died between its two halves).
 *
 * The distinction matters three separate ways in one sweep — ripeness,
 * stale-pending, and forgotten — and the three used to spell "unknown"
 * differently (PHP_INT_MAX in one place, null in two others). They happened
 * to agree; a fourth reader would not have been so lucky.
 */
function onionpress_wayback_job_age( array $rec, $now ) {
    return empty( $rec['submitted_at'] ) ? null : ( $now - (int) $rec['submitted_at'] );
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
    // Test hook: return a keyed array of ['code'=>int,'body'=>string] to
    // stand in for the whole parallel fetch. This is the only seam below
    // the per-caller mocks, and it exists so the response-handling code
    // above it — chunk/result alignment, the HTTP-200 gate, the
    // coverage bookkeeping in poll_parallel — can be tested at all.
    // Mocking each caller instead left that code with no tests, which is
    // precisely where a silent misclassification would live.
    $mock = apply_filters( 'onionpress_wayback_curl_multi_mock', null, $setups );
    if ( is_array( $mock ) ) {
        return $mock;
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
    //
    // There is deliberately NO parameter here asking SPN to capture the
    // page's images, CSS and favicons, because no such parameter exists.
    // The SPN2 API doc defines embeds as "Components of a web page, e.g.
    // images, CSS, JS, etc. When we capture a web page, we also try to
    // capture its embeds" — it is unconditional and always on. The two
    // options that look like they might govern it do not:
    //   capture_all=1       "Archive page even when the server returns an
    //                        HTTP error status (4xx or 5xx)" — about
    //                        status codes, not resources.
    //   capture_outlinks=1  archives LINKS found on the page (a[href]),
    //                        not embeds, capped at 100 and billed against
    //                        our capture budget. Doc: "Avoid ... unless
    //                        you need to archive all discovered outlinks."
    // Nor does js_behavior_timeout=0 switch the browser off: it bounds JS
    // run "after page load", and force_get=1 is the separate option that
    // would bypass the headless browser. We must NOT set force_get here —
    // that one really would reduce every capture to a bare HTML GET.
    //
    // So when a capture of ours replays without its images, the cause is
    // not on this line. Our onion answers in 3-20s when it answers at
    // all, against SPN's documented "Network connection timeout = 10s"
    // per resource and "Max web page capture time = 50s ... Partial
    // success may still be recorded if sufficient content has been
    // captured". A slow onion spends that budget on the HTML and the
    // embeds are what falls off. Raising js_behavior_timeout does not buy
    // those back — it spends the same scarce 50s on scroll/hover events
    // we have no use for, and a trial with behaviors enabled returned 504.
    // What we do instead is refuse to call such a capture complete: see
    // onionpress_wayback_resources_state() and the bare-captures counter.
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
function onionpress_wayback_poll_parallel( array $job_ids, &$covered = null ) {
    // $covered is an out-param: the set of job_ids whose batch actually
    // came back parseable, as a job_id => true map. The caller needs it to
    // tell "SPN answered and did not mention this job" (it forgot it) from
    // "the batch carrying this job never came back" (we simply don't know).
    // Both look identical in the return value — an absent entry — and
    // conflating them means one 40s Tor timeout silently reclassifies a
    // whole 20-job batch as forgotten and resubmits it.
    $covered = array();
    if ( empty( $job_ids ) ) {
        return array();
    }
    // Test hook: return a list of status dicts to short-circuit the SPN poll.
    $mock = apply_filters( 'onionpress_wayback_poll_parallel_mock', null, $job_ids );
    if ( is_array( $mock ) ) {
        // A mock stands in for a reachable SPN, so everything asked about
        // counts as covered — otherwise a mock returning [] would mean
        // "poll failed" and no test could exercise the forgotten path.
        // The second filter exists so a test can express the opposite:
        // batches that never came back, which is the case the coverage
        // tracking was added for and which the mock alone cannot produce.
        $covered = array_fill_keys( $job_ids, true );
        $covered_mock = apply_filters(
            'onionpress_wayback_poll_covered_mock', null, $job_ids );
        if ( is_array( $covered_mock ) ) {
            $covered = array_fill_keys( $covered_mock, true );
        }
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
        foreach ( $raw as $i => $r ) {
            if ( $r['code'] !== 200 || empty( $r['body'] ) ) continue;
            $data = @json_decode( $r['body'], true );
            // Must be a JSON *list* of status dicts. A 200 carrying an
            // object — {"message":"..."} for a rate limit, an auth error,
            // a maintenance notice — decodes to an array too, and counting
            // that as an answer would mark all 20 job_ids in the batch
            // covered on the strength of a response containing no statuses
            // at all. The forgotten loop would then read it as "SPN
            // answered and mentioned none of them" and resubmit the batch:
            // the same over-eager clearing the coverage tracking exists to
            // prevent, just through a narrower door.
            if ( ! is_array( $data ) || ! array_is_list( $data ) ) continue;
            // This batch answered, so every job_id in it is now accounted
            // for: any of them missing from $data really is one SPN has
            // forgotten, not one we failed to ask about. Keyed by $i,
            // which indexes $parallel_group the same way $setups does.
            foreach ( $parallel_group[ $i ] as $jid ) {
                $covered[ $jid ] = true;
            }
            foreach ( $data as $item ) {
                if ( is_array( $item ) ) $all[] = $item;
            }
        }
    }
    return $all;
}

// ───────────────── finalize: write outcomes into storage ────────────

// Investigated 2026-08-24: submitting each image/CSS/JS URL to SPN as
// its own job did not work that day, so no per-asset submission path
// ships here — but read the whole note before treating that as settled.
//
// The controlled experiment (same window, minutes apart, our Tor client
// verified healthy by a third-party onion fetch first):
//   - our onion's HOME PAGE as an SPN job  -> status:success in 10.3s,
//     http_status:200, and counters.embeds=0 / resources=[]. SPN's
//     crawler reaches this onion and captures HTML; it fetched no
//     embedded resources even from a successful page capture.
//   - our onion's CSS FILE as its own job, submitted in BOTH the
//     http:// and https:// URL forms -> error:no-captures for each,
//     "...unreachable" — which the successful page capture in the same
//     minutes proves is factually wrong as a reachability claim.
//   - earlier the same day: a favicon and a PNG on DuckDuckGo's onion
//     (fast, well-provisioned, unrelated to us), with force_get=1 and
//     without -> error:no-captures as well.
// So on 2026-08-24, SPN captured onion HTML pages while failing every
// standalone non-HTML onion URL, across two sites and both URL schemes.
//
// What this does NOT establish — and this file previously overclaimed
// it, in exactly the way that was already made and retracted on
// 2026-08-13 ("archive.org's onion is unreachable" -> our own Tor was
// degraded; "no onion captures since June, SPN regressed" -> nobody had
// submitted any; one live submission then succeeded in 6.6s):
//   - "cannot, for anyone, permanently". SPN's /save/status flips
//     success -> error:no-captures for a job whose capture actually
//     landed (that is why the CDX-rescue path in this file exists), IA
//     serves "Temporarily Offline" pages mid-outage, and CDX absence
//     only ever proves absence of submissions. A failure report from
//     any of these channels is weather until CDX, checked later, says
//     otherwise.
// Before acting on this note in either direction, re-run the experiment
// above — page control first, then assets, then CDX a while later. Do
// not write "SPN cannot" anywhere (comments, commits, issues) from
// error responses alone.
//
// Until a re-run shows asset jobs landing: resources_state below is the
// remedy we can ship — stop calling embed-less captures successful.

/**
 * Compare two URLs the way SPN's resource list needs them compared:
 * ignoring scheme and a trailing slash, which SPN varies freely between
 * what we submitted and what it echoes back.
 */
function onionpress_wayback_same_url( $a, $b ) {
    $norm = function ( $u ) {
        $u = preg_replace( '#^https?://#i', '', trim( (string) $u ) );
        return rtrim( $u, '/' );
    };
    return strcasecmp( $norm( $a ), $norm( $b ) ) === 0;
}

/**
 * How many EMBEDS a capture actually got — the images, CSS, JS and
 * favicons, not counting the page itself. SPN's `resources` list always
 * leads with the captured URL, so a list of length 1 means "the HTML
 * and nothing else".
 */
function onionpress_wayback_embed_count( array $resources, $url ) {
    $n = 0;
    foreach ( $resources as $r ) {
        if ( ! is_string( $r ) || $r === '' ) continue;
        if ( onionpress_wayback_same_url( $r, $url ) ) continue;
        $n++;
    }
    return $n;
}

/**
 * Decide how much of a capture landed, from SPN's own answer.
 *
 * Why this exists: a Wayback capture can report "success", be findable
 * in CDX, and still replay as a bare wall of text. The Wayback Machine
 * rewrites every `src`/`href` in the stored HTML into a `…im_/…` replay
 * URL whether or not those bytes were ever fetched, so a page whose
 * links all look correctly rewritten tells you nothing about whether
 * its images and stylesheet exist. The only evidence on hand is the
 * `resources` list SPN returns with the status — so that is what we
 * read, and when we have not seen one we say so instead of assuming.
 */
function onionpress_wayback_resources_state( array $data, $url ) {
    if ( ! is_array( $data['resources'] ?? null ) ) {
        return OP_WB_RES_UNVERIFIED;
    }
    return onionpress_wayback_embed_count( $data['resources'], $url ) > 0
        ? OP_WB_RES_COMPLETE
        : OP_WB_RES_BARE;
}

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
        'resources_state' => onionpress_wayback_resources_state( $data, $url ),
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
        'resources_state' => OP_WB_META_RESOURCES_STATE,
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

// ──────────────────── the static site's own pages ───────────────────
//
// A static publish replaces the whole site at the onion root in one atomic
// symlink flip. Its pages are FILES, served straight off disk by the
// static-first Apache config before WordPress's rewrite rules ever run — so
// get_posts() cannot see them, by construction, and until this section existed
// the archiver's entire view of such a site was home + feed. A site whose real
// content was 32 pages reported "archived 6/6, 100%" while archiving six
// leftover WordPress defaults and none of its actual content.
//
// These pages join the queue as ordinary records, in the same key/url/read/
// write shape posts and home/feed already use, so poll, CDX rescue, stale-
// pending, back-off, budget and logging all apply to them unchanged. There is
// no second submission path.

/**
 * Where the live generation is symlinked. The static receiver owns this path,
 * so read its constant rather than keeping a second copy of the same string in
 * two mu-plugins that would then have to agree.
 */
function onionpress_wayback_static_current_path() {
    $path = defined( 'ONIONPRESS_STATIC_CURRENT' )
        ? ONIONPRESS_STATIC_CURRENT
        : '/var/www/html/site/current';
    // Test hook: point the enumeration at a throwaway generation. The suite
    // runs against a live site, and repointing the real `current` symlink to
    // test this would swap out the site somebody is actually serving.
    return (string) apply_filters( 'onionpress_wayback_static_current_path_mock', $path );
}

/**
 * The live generation as array( 'id' => …, 'dir' => … ), or null when nothing
 * is serving statically (a plain WordPress site, or a first run before the
 * receiver has ever committed).
 *
 * realpath() is load-bearing, and not for the reason it looks like. The walk
 * below is indifferent to it — PHP descends a symlinked start path perfectly
 * well, unlike find(1), which needs -L. What resolving buys is the ID:
 * `site/current` is a symlink whose own basename is the constant "current".
 * Read the id off the unresolved path and it never changes, so every publish
 * matches the generation already recorded, no row is ever retired, and new
 * content is silently never submitted. The generation id is the entire
 * mechanism by which a publish re-archives.
 *
 * Agrees with the receiver's own onionpress_static_current_generation(), which
 * takes the basename of the readlink target; that function returns only the id,
 * and the enumeration needs the directory too.
 */
function onionpress_wayback_static_generation() {
    $real = realpath( onionpress_wayback_static_current_path() );
    if ( $real === false || ! is_dir( $real ) ) {
        return null;
    }
    return array( 'id' => basename( $real ), 'dir' => $real );
}

/**
 * Does the live generation publish THIS subsite's URLs?
 *
 * Everything static-shaped here is container-global — one `site/current`
 * symlink, one /var/lib/onionpress/onion_address — but the sweep visits every
 * subsite in the network in turn. Without this gate a four-subsite network
 * enumerated the same generation four times and submitted all 32 URLs four
 * times over, against an SPN account whose concurrent slots are counted in
 * single digits.
 *
 * The generation serves at the onion ROOT, so its pages are the main site's
 * URLs; a subsite lives under a path prefix and owns none of them.
 * function_exists() because is_main_site() ships with multisite only, and this
 * plugin also runs on single-site installs — where the one site is the main one
 * by definition.
 */
function onionpress_wayback_static_serves_this_site() {
    if ( function_exists( 'is_main_site' ) && ! is_main_site() ) {
        return false;
    }
    return onionpress_wayback_static_generation() !== null;
}

/**
 * Read the static capture map. Pinned to blog 1 — there is ONE static site per
 * network, at the root, so its capture state is one thing and not one per
 * subsite. Without the pin, a four-subsite network keeps four copies of the
 * same map and each submits the same pages under its own job_ids.
 *
 * Blog 1 by literal, matching onionpress_wayback_auth_header(), which pins the
 * SPN credentials the same way: on OnionPress the network's main site is
 * always blog 1, and having two functions disagree about where "the network's
 * own state" lives would be worse than the assumption.
 */
function onionpress_wayback_static_state_read() {
    $raw = function_exists( 'get_blog_option' )
        ? get_blog_option( 1, OP_WB_OPT_STATIC, array() )
        : get_option( OP_WB_OPT_STATIC, array() );
    return is_array( $raw ) ? $raw : array();
}

/**
 * Write the static capture map back to blog 1.
 *
 * Switches by hand rather than calling update_blog_option(), which has no
 * autoload parameter: this row reaches ~150 kB at OP_WB_STATIC_MAX_PAGES, and
 * autoloading it would put that on every request the container serves. The
 * $autoload=false here is the same choice onionpress_wayback_opt_write() makes.
 */
function onionpress_wayback_static_state_write( array $state ) {
    $switched = false;
    if ( function_exists( 'switch_to_blog' ) && get_current_blog_id() !== 1 ) {
        switch_to_blog( 1 );
        $switched = true;
    }
    try {
        update_option( OP_WB_OPT_STATIC, $state, false /* no autoload */ );
    } finally {
        if ( $switched && function_exists( 'restore_current_blog' ) ) {
            restore_current_blog();
        }
    }
}

/**
 * Every page path the live generation serves, as absolute site paths ('/',
 * '/about/', …), sorted and capped at OP_WB_STATIC_MAX_PAGES.
 *
 * sitemap.xml first, because it is the publisher's own statement of what the
 * site is — written from its build manifest, so it is authoritative in a way a
 * directory listing never is. Only the PATH of each <loc> is used: the host in
 * there comes from whatever site_url was configured at build time and can be
 * stale or even http://localhost, and archiving that submits URLs nobody can
 * fetch.
 *
 * The walk is the fallback because a generator may ship no sitemap at all —
 * moss gates sitemap.xml (and rss.xml) on its site_url being deployed, so a
 * site built before its onion name was registered ships neither. Without the
 * fallback those sites would archive nothing and the reason would be invisible.
 *
 * Memoized per REQUEST, keyed on the generation, because one sweep iteration
 * asks several times over (the has-work probe, the poll's record list, the
 * submit's) and on the fallback path each ask is a full directory walk. Keying
 * on the generation is what keeps a publish visible: a new generation is a new
 * key, so the memo cannot serve the previous publish's page list. Within one
 * generation the tree is immutable — each publish is built into its own
 * directory and the symlink moved — so nothing here can go stale.
 */
function onionpress_wayback_static_paths( array $gen ) {
    static $memo = array();
    $memo_key = (string) $gen['id'] . "\0" . (string) $gen['dir'];
    if ( isset( $memo[ $memo_key ] ) ) {
        return $memo[ $memo_key ];
    }

    $paths   = array();
    $sitemap = $gen['dir'] . '/sitemap.xml';

    if ( is_readable( $sitemap ) && function_exists( 'simplexml_load_file' ) ) {
        $prev = libxml_use_internal_errors( true );
        $xml  = simplexml_load_file( $sitemap );
        libxml_clear_errors();
        libxml_use_internal_errors( $prev );
        if ( $xml !== false && isset( $xml->url ) ) {
            foreach ( $xml->url as $u ) {
                $path = wp_parse_url( (string) $u->loc, PHP_URL_PATH );
                if ( is_string( $path ) && $path !== '' ) {
                    $paths[ $path ] = true;
                }
            }
        } elseif ( $xml !== false && $xml->getName() === 'sitemapindex' ) {
            // Past 50k URLs the convention is an INDEX of sitemaps, whose
            // children are <sitemap> not <url>. Deliberately not followed: a
            // site that large is two orders of magnitude past
            // OP_WB_STATIC_MAX_PAGES, so the pages the index would name are
            // ones this plugin refuses to track anyway. Say so rather than
            // letting an unrecognised sitemap look like an absent one — the
            // fallback below is much more expensive, and an operator seeing a
            // walk on a site that plainly ships a sitemap would have no way to
            // know why.
            onionpress_wayback_log(
                'static: sitemap.xml is a <sitemapindex> (a site past ~50k URLs); '
                . 'not followed — falling back to the directory walk, and only the '
                . 'first ' . OP_WB_STATIC_MAX_PAGES . ' pages are tracked' );
        }
    }

    // Fallback, only when the sitemap named nothing. A generation that ships a
    // usable one is authoritative, and the walk is the expensive path.
    //
    // A page is a directory carrying index.html. `_moss` is the generator's own
    // internal directory (hashed css/js, generated OG images); it outnumbers
    // the pages and can never contain one.
    //
    // Every part of this can throw. RecursiveDirectoryIterator reports an
    // unreadable directory as an UnexpectedValueException — from its own
    // constructor for the root, and from getChildren() for a subdirectory. One
    // directory the web user could not open would otherwise propagate out of
    // the sweep as a PHP fatal on every invocation, with no log line saying
    // what happened. CATCH_GET_CHILD skips the unreadable subtree; the
    // try/catch covers the root and anything else the iterator raises. Both
    // keep whatever was enumerated before the failure, because a partial page
    // list is worth far more than a dead sweep.
    if ( ! $paths ) {
        $scanned    = 0;
        $unreadable = array();
        try {
            $dir    = new RecursiveDirectoryIterator( $gen['dir'], FilesystemIterator::SKIP_DOTS );
            $filter = new RecursiveCallbackFilterIterator( $dir, function ( $file ) use ( &$unreadable ) {
                if ( $file->getFilename() === '_moss' ) {
                    return false;
                }
                // CATCH_GET_CHILD turns an unreadable subdirectory from a fatal
                // into a SILENT skip, and silent is its own failure: every page
                // under it stops being archived and nothing says why. One
                // is_readable() per directory — not per file — buys the log
                // line below. CATCH_GET_CHILD stays as the backstop for the gap
                // between this check and the descent.
                if ( $file->isDir() && ! $file->isReadable() ) {
                    $unreadable[] = $file->getPathname();
                    return false;
                }
                return true;
            } );
            $walk = new RecursiveIteratorIterator(
                $filter,
                RecursiveIteratorIterator::LEAVES_ONLY,
                RecursiveIteratorIterator::CATCH_GET_CHILD
            );
            foreach ( $walk as $file ) {
                if ( ++$scanned > OP_WB_STATIC_SCAN_MAX ) {
                    onionpress_wayback_log( sprintf(
                        'static: directory walk hit its %d-file ceiling; enumerated %d page(s) before stopping',
                        OP_WB_STATIC_SCAN_MAX, count( $paths )
                    ) );
                    break;
                }
                if ( $file->getFilename() !== 'index.html' ) {
                    continue;
                }
                $rel = substr( $file->getPath(), strlen( $gen['dir'] ) );
                $rel = str_replace( '\\', '/', $rel );
                $paths[ $rel === '' ? '/' : rtrim( $rel, '/' ) . '/' ] = true;
            }
        } catch ( Exception $e ) {
            onionpress_wayback_log( sprintf(
                'static: directory walk of %s failed after %d file(s) (%s); '
                . 'archiving the %d page(s) enumerated so far',
                $gen['dir'], $scanned, $e->getMessage(), count( $paths )
            ) );
        }
        if ( $unreadable ) {
            onionpress_wayback_log( sprintf(
                'static: %d director(y/ies) could not be opened and were skipped, '
                . 'so any page under them is NOT archived: %s',
                count( $unreadable ), implode( ', ', array_slice( $unreadable, 0, 5 ) )
                    . ( count( $unreadable ) > 5 ? ', …' : '' )
            ) );
        }
    }

    $out = array_keys( $paths );
    sort( $out );
    // Storage bound, not a throughput one — see OP_WB_STATIC_MAX_PAGES. Applied
    // inside the memo so the log line fires once per request, not once per ask.
    $total = count( $out );
    if ( $total > OP_WB_STATIC_MAX_PAGES ) {
        $out = array_slice( $out, 0, OP_WB_STATIC_MAX_PAGES );
        onionpress_wayback_log( sprintf(
            'static: %d page(s) found, tracking the first %d (OP_WB_STATIC_MAX_PAGES); %d NOT archived',
            $total, OP_WB_STATIC_MAX_PAGES, $total - OP_WB_STATIC_MAX_PAGES
        ) );
    }
    $memo[ $memo_key ] = $out;
    return $out;
}

/**
 * Patch one static page's state inside the single OP_WB_OPT_STATIC row.
 * Returns false when the write was REFUSED, so a caller counting successful
 * submissions can tell that apart from a write that did nothing.
 *
 * Refuses to write into a generation that is no longer the live one. A job
 * submitted against the previous generation can land here after a publish, and
 * finalize_success writes archived_at and clears job_id in two separate calls —
 * without this guard a late write resurrects a row for a page the new
 * generation may not even serve, and nothing would ever retire it.
 *
 * Adopting a new generation is the other half of the same comparison, and it
 * lives here because this is the WRITE path. Doing it while building records
 * would mean merely ASKING what work exists rewrote the option and logged a
 * publish, several times per subsite per iteration. Doing it on the first real
 * write instead keeps every read path pure and loses nothing: a record's read
 * already reports a page as unarchived whenever the stored map describes a
 * different generation, so a publish puts every page back in the queue whether
 * or not anything has been written yet.
 */
function onionpress_wayback_static_write( $gen_id, $path, array $patch ) {
    $state = onionpress_wayback_static_state_read();
    if ( (string) ( $state['generation'] ?? '' ) !== (string) $gen_id ) {
        // The map describes some other generation. Two cases, opposite
        // answers, and only the live generation id separates them.
        $live = onionpress_wayback_static_generation();
        if ( $live === null || (string) $live['id'] !== (string) $gen_id ) {
            return false; // a late write from a generation that has been retired
        }
        // A publish has landed and this is the first write against it: adopt
        // it, retiring every row of the old generation in one write. That
        // replacement is the entire mechanism by which a publish re-archives.
        onionpress_wayback_log( sprintf(
            'static: generation %s is live (was %s) — re-archiving its pages',
            $gen_id, ( $state['generation'] ?? '(none)' )
        ) );
        $state = array( 'generation' => (string) $gen_id, 'urls' => array() );
    }
    $urls = isset( $state['urls'] ) && is_array( $state['urls'] ) ? $state['urls'] : array();
    $row  = isset( $urls[ $path ] ) && is_array( $urls[ $path ] ) ? $urls[ $path ] : array();
    // Same convention as onionpress_wayback_opt_write(): '' / 0 deletes a key.
    foreach ( $patch as $k => $v ) {
        if ( $v === '' || $v === 0 || $v === 0.0 ) {
            unset( $row[ $k ] );
        } else {
            $row[ $k ] = $v;
        }
    }
    if ( $row ) {
        $urls[ $path ] = $row;
    } else {
        unset( $urls[ $path ] );
    }
    $state['urls'] = $urls;
    onionpress_wayback_static_state_write( $state );
    return true;
}

/**
 * The live generation's pages as work records, in the same shape posts and
 * home/feed already use.
 *
 * $covered is the set of URLs home/feed already claim. The sitemap lists the
 * root too, so without it '/' would be submitted twice every sweep.
 *
 * Reads nothing it does not need and writes nothing at all: building the work
 * list is a question, and the answer to a question should not depend on having
 * been asked. The generation adoption lives in static_write() — see there.
 */
function onionpress_wayback_static_records( array $covered = array() ) {
    $onion = onionpress_wayback_onion_addr();
    if ( $onion === '' ) {
        return array();
    }
    $gen = onionpress_wayback_static_generation();
    if ( $gen === null ) {
        return array();
    }

    $gen_id  = $gen['id'];
    $records = array();
    foreach ( onionpress_wayback_static_paths( $gen ) as $path ) {
        $url = 'http://' . $onion . $path;
        if ( isset( $covered[ $url ] ) ) {
            continue;
        }
        $records[] = array(
            'key'   => 'static:' . $path,
            'url'   => $url,
            // A row belongs to the generation the map was written for. When a
            // publish has replaced it, every page of the NEW generation is
            // unarchived by definition — including one whose path the old
            // generation happened to serve too — so a stale row must not be
            // reported as this page's state. That is what puts every page back
            // in the queue on a publish, and why nothing has to reset the
            // option eagerly for a publish to be noticed.
            'read'  => function () use ( $gen_id, $path ) {
                $s = onionpress_wayback_static_state_read();
                if ( (string) ( $s['generation'] ?? '' ) !== (string) $gen_id ) {
                    return array();
                }
                return isset( $s['urls'][ $path ] ) && is_array( $s['urls'][ $path ] )
                    ? $s['urls'][ $path ]
                    : array();
            },
            'write' => function ( $patch ) use ( $gen_id, $path ) {
                return onionpress_wayback_static_write( $gen_id, $path, $patch );
            },
        );
    }
    return $records;
}

/**
 * archived / in_flight / total for the live generation's pages, for the
 * progress counters. Derived from the same records the sweep works on, so the
 * admin page cannot report a state the queue disagrees with.
 *
 * Gated exactly like the submission, and deliberately by the same call: a
 * subsite neither owns these pages nor submits them, and counting them there
 * would show a root page permanently unarchived — the root is excluded from the
 * records because the MAIN site's home record covers it, which is not true of a
 * subsite's home url.
 */
function onionpress_wayback_static_totals() {
    $out = array( 'total' => 0, 'archived' => 0, 'in_flight' => 0 );
    if ( ! onionpress_wayback_static_serves_this_site() ) {
        return $out;
    }
    foreach ( onionpress_wayback_static_records( onionpress_wayback_sitewide_covered_urls() ) as $rec ) {
        $state = call_user_func( $rec['read'] );
        $out['total']++;
        if ( ! empty( $state['archived_at'] ) ) {
            $out['archived']++;
        } elseif ( ! empty( $state['job_id'] ) ) {
            $out['in_flight']++;
        }
    }
    return $out;
}

// ───────────────────── work queue (posts + home/feed) ───────────────

/**
 * Unified record for one URL under management. Each is an associative
 * array with at minimum 'url', 'read' callable (returns current state),
 * 'write' callable (applies a patch). Used by the sweep so posts and
 * home/feed go through the same code path.
 */
function onionpress_wayback_posts_needing_submit( $limit ) {
    // When a static generation is serving this site, its posts are the
    // leftover WordPress defaults (sample-page, hello-world, ...) — not
    // the site. Submitting them alongside the real pages would spend SPN
    // slots on boilerplate nobody asked to archive.
    if ( onionpress_wayback_static_serves_this_site() ) {
        return array();
    }
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
    // Same exclusion as onionpress_wayback_posts_needing_submit(): a
    // static generation's boilerplate WP posts were never submitted, so
    // none can be legitimately in flight either.
    if ( onionpress_wayback_static_serves_this_site() ) {
        return array();
    }
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
 * The URLs home + feed already claim, as a set. The static enumeration
 * subtracts these so the root — which every sitemap lists — is not queued a
 * second time under its own record.
 */
function onionpress_wayback_sitewide_covered_urls() {
    $covered = array();
    foreach ( array( onionpress_wayback_home_url_full(), onionpress_wayback_feed_url_full() ) as $url ) {
        if ( $url ) {
            $covered[ $url ] = true;
        }
    }
    return $covered;
}

/**
 * Home + feed as work records, matching the shape used for posts, plus every
 * page of the live static generation.
 *
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
            // A state naming a DIFFERENT url describes a different page and
            // cannot answer for this one — report unarchived and let it be
            // submitted. The feed url moves: it switches from WordPress's
            // /feed/ route to the generation's own feed the moment a publish
            // ships one. Without this the record only ever recorded THAT it
            // archived, never WHAT, so the first /feed/ capture pinned it as
            // done forever and the real feed was never once submitted.
            //
            // An absent stored url is a row written before this field existed.
            // Treating those as a mismatch would re-submit every site's home
            // page once on upgrade for no gain — a url is only known to be
            // stale when it is present and differs.
            'read'  => function() use ( $opt_key, $url ) {
                $state  = onionpress_wayback_opt_read( $opt_key );
                $stored = (string) ( $state['url'] ?? '' );
                if ( $stored !== '' && $stored !== $url ) {
                    return array();
                }
                return $state;
            },
            // Stamped on every write, so a row becomes self-describing at the
            // first thing that happens to it — submission included, which is
            // what keeps an in-flight job from being credited to a url that
            // changed while it was out.
            'write' => function( $patch ) use ( $opt_key, $url ) {
                $patch['url'] = $url;
                onionpress_wayback_opt_write( $opt_key, $patch );
            },
        );
    }
    // Home and feed are the only two URLs WordPress can name generically. When
    // a static generation is serving they are 2 of N, and every other page it
    // publishes sat outside the queue entirely — which is how a site could
    // report 100% archived while none of its actual pages had been submitted.
    if ( ! onionpress_wayback_static_serves_this_site() ) {
        return $records;
    }
    return array_merge(
        $records,
        onionpress_wayback_static_records( onionpress_wayback_sitewide_covered_urls() )
    );
}

/**
 * Does any sitewide URL still need attention? A record needs work unless it has
 * an archived_at and no job_id in flight.
 *
 * Derived from the records rather than from named options so it cannot drift
 * from what the sweep would actually try to do. The multisite loop uses this to
 * decide it may skip a subsite entirely, and reading only home + feed there was
 * the same bug one layer up: a subsite whose posts and home/feed were all
 * archived got skipped, so its static pages sat unarchived forever.
 */
function onionpress_wayback_sitewide_has_work() {
    foreach ( onionpress_wayback_sitewide_records() as $rec ) {
        $state = call_user_func( $rec['read'] );
        if ( empty( $state['archived_at'] ) || ! empty( $state['job_id'] ) ) {
            return true;
        }
    }
    return false;
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
 * Sum queue totals across every subsite in the network, plus the pages of the
 * live static generation. Returns an array with 'archived', 'in_flight',
 * 'remaining' and 'total'.
 *
 * The static pages have to be in here or the number is a lie: on a site whose
 * real content is a static generation, the posts this loop counts are the
 * leftover WordPress defaults, and archiving all six of them reported 100%
 * while 32 actual pages had never been submitted.
 *
 * Conversely, once a static generation IS serving a given site, its WP posts
 * are excluded here entirely rather than added on top — they are the same
 * leftover defaults, permanently unsubmitted (onionpress_wayback_posts_
 * needing_submit() skips them too), and counting them would report a fixed
 * number of pages as "remaining" forever with no sweep able to clear it.
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
            if ( onionpress_wayback_static_serves_this_site() ) {
                continue;
            }
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
    // Once, outside the loop: there is one static site per network, at the
    // root, and its state is pinned to blog 1. Adding it per subsite would
    // multiply the same 32 pages by the number of subsites.
    $static = onionpress_wayback_static_totals();
    $out['total']     += $static['total'];
    $out['archived']  += $static['archived'];
    $out['in_flight'] += $static['in_flight'];
    $out['remaining'] = max( 0, $out['total'] - $out['archived'] - $out['in_flight'] );
    return $out;
}

function onionpress_wayback_sweep_loop( $token ) {
    $lock_key     = 'op_wayback_sweep_lock';
    $loop_start   = microtime( true );
    // Every key here is summed from sweep_iteration()'s return, by name —
    // a key present in one and not the other silently stays 0, so the two
    // lists have to be kept in step. 'lost' is here because a sustained
    // poll outage is otherwise invisible in the summary lines an operator
    // actually reads: each sweep prints its own poll-lost, but the drained
    // and recycling lines are where you go to ask why nothing happened.
    $totals       = array(
        'submitted' => 0, 'success' => 0, 'cdx' => 0,
        'error'     => 0, 'forgotten' => 0, 'lost' => 0,
    );
    $last_progress = $loop_start;

    // Refresh the lock's timestamp, but only while it is still ours —
    // stamping our token onto a successor's claim would evict a daemon
    // that legitimately took over. Returns false once we have lost it.
    //
    // MUST be called in the invoking blog's context, never between a
    // switch_to_blog() and its restore: options are per-subsite, so a
    // heartbeat inside a switch writes a second lock into the wrong
    // options table and leaves the real one to go stale.
    $heartbeat = function () use ( $lock_key, $token ) {
        $cur = (string) get_option( $lock_key, '' );
        if ( strpos( $cur, $token . ':' ) !== 0 ) {
            return false;
        }
        update_option( $lock_key, $token . ':' . time(), false );
        return true;
    };

    onionpress_wayback_log( 'Loop: starting daemon sweep (token=' . substr( $token, 0, 6 ) . ')' );
    $iter = 0;
    while ( true ) {
        $iter++;
        // Lock ownership check — if another daemon has claimed the
        // lock (possible if our heartbeat lapsed past stale threshold),
        // exit gracefully. Also heartbeats the ts if we still own it.
        if ( ! $heartbeat() ) {
            onionpress_wayback_log( 'Loop: lock taken by another daemon, exiting (token=' . substr( $token, 0, 6 ) . ', iter=' . $iter . ')' );
            return;
        }

        // Lifetime cap. Exiting here is not giving up: the queue is
        // unchanged and the next cron tick starts a fresh daemon that picks
        // up exactly where this one stopped — but with an empty option and
        // query cache, so it sees the database as it actually is. See
        // OP_WB_LOOP_MAX_SEC. Deliberately below the ownership check: it
        // releases the lock, so it must first be sure the lock is ours.
        $runtime = (int) ( microtime( true ) - $loop_start );
        // Filtered so a test can shrink it to 0 and assert the handoff.
        // Everything else in this plugin is reachable from a mock filter;
        // a hard constant would have made the one mechanism that stops the
        // daemon wedging the only mechanism with no test.
        $max_sec = (int) apply_filters( 'onionpress_wayback_loop_max_sec', OP_WB_LOOP_MAX_SEC );
        if ( $runtime >= $max_sec ) {
            onionpress_wayback_log( sprintf(
                'Loop: recycling after %dm%02ds (%d iterations, cap=%ds) — '
                . 'submitted=%d archived=%d cdx-hit=%d forgotten=%d errors=%d poll-lost=%d; '
                . 'cron restarts a fresh daemon',
                intdiv( $runtime, 60 ), $runtime % 60, $iter - 1, $max_sec,
                $totals['submitted'], $totals['success'], $totals['cdx'],
                $totals['forgotten'], $totals['error'], $totals['lost']
            ) );
            // Hand off immediately rather than making the queue wait out
            // LOCK_STALE_SEC for a daemon we know has exited cleanly.
            delete_option( $lock_key );
            // Start the successor now rather than leaving it to the 300s
            // recurring schedule and whenever a page view next happens to
            // drive pseudo-cron. Deliberately NOT kick_now(): that also
            // clears the back-off, and a back-off set by the sweep we are
            // recycling out of is a decision worth keeping.
            //
            // Gated on a non-zero lifetime because a cap of 0 is the test
            // seam: firing a real loopback there would start a genuine
            // half-hour production daemon out of a unit test, and at cap=0
            // every successor would recycle on its own first iteration —
            // a restart loop rather than a handoff.
            if ( $runtime > 0 ) {
                onionpress_wayback_schedule_sweep_now( 'recycle' );
            }
            return;
        }

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
            // Heartbeat per SUBSITE, not just per loop iteration. Each
            // sweep_iteration() below gets its own OP_WB_SWEEP_BUDGET_SEC,
            // so on a four-blog network one pass through this foreach can
            // run four full budgets — far past OP_WB_LOOP_LOCK_STALE_SEC —
            // while the lock's timestamp sat untouched from the top of the
            // iteration. A second daemon would then read the lock as dead
            // and both would write the same records. Before switch_to_blog,
            // because the lock lives in the invoking blog's options table.
            $heartbeat();
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
                // Home, feed and the static pages are tracked in wp_options,
                // not post meta, so the post probes above miss all of them. A
                // subsite with every post archived can still need work if
                // save_post invalidated its home/feed capture, if a job is in
                // flight, or if a static publish put N pages back in the queue.
                // Ask the records rather than named options, so this cannot
                // decide there is nothing to do while the sweep would have
                // found something.
                $sitewide_work = onionpress_wayback_sitewide_has_work();
                if ( empty( $remaining ) && empty( $in_flight ) && ! $sitewide_work ) {
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
                'Loop: drained — %d iterations, %dm%02ds, submitted=%d archived=%d cdx-hit=%d forgotten=%d errors=%d poll-lost=%d',
                $iter, intdiv( $runtime, 60 ), $runtime % 60,
                $totals['submitted'], $totals['success'], $totals['cdx'],
                $totals['forgotten'], $totals['error'], $totals['lost']
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
                'Loop progress: %dm%02ds iter=%d | archived=%d/%d (%s/min captured, %s remaining) | submitted=%d (%s/min) cdx=%d forgotten=%d errors=%d poll-lost=%d',
                intdiv( $runtime, 60 ), $runtime % 60, $iter,
                $qstats['archived'], $qstats['total'],
                $cap_per_min, $qstats['remaining'],
                $totals['submitted'], $sub_per_min,
                $totals['cdx'], $totals['forgotten'], $totals['error'],
                $totals['lost']
            ) );
        }

        // Pace between iterations. Also respect any backoff set by a
        // gate inside the iteration (available=0, 429, etc.).
        $now = time();
        $backoff_until = (int) get_option( OP_WB_OPT_BACKOFF_UNTIL, 0 );
        $sleep = ( $backoff_until > $now )
            ? ( $backoff_until - $now )
            : OP_WB_LOOP_IDLE_SLEEP;
        // Heartbeat before sleeping, not just at the top of the iteration.
        // The sleep sits OUTSIDE the work the sweep budget bounds, and with
        // OP_WB_BACKOFF_UNREACHABLE it is 120s — so a budget-length iteration
        // followed by an unreachable back-off leaves the lock untouched for
        // well past OP_WB_LOOP_LOCK_STALE_SEC on the nominal path, not in
        // some worst case. A second daemon then declares this one dead and
        // both write the same records.
        $heartbeat();
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

    // Start the clock here, not after the status call. The budget's whole
    // job is to keep one iteration inside OP_WB_LOOP_LOCK_STALE_SEC, and
    // the status call below can sit on a 20s Tor timeout — 20s the lock
    // heartbeat experiences and, until this moved, the budget did not.
    // It also makes the elapsed= in the log the iteration's real cost.
    $deadline = microtime( true ) + OP_WB_SWEEP_BUDGET_SEC;

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
        // time() rather than $now for the same reason as the unreachable
        // back-off below: $now predates the status call this branch reacts
        // to, and a 20s pause dated 20s ago is not a pause.
        update_option( OP_WB_OPT_BACKOFF_UNTIL, time() + OP_WB_BACKOFF_NO_SLOTS, false );
        onionpress_wayback_log( 'Sweep paused: available=0 processing='
            . ( $user['processing'] ?? '?' ) . ', backing off ' . OP_WB_BACKOFF_NO_SLOTS . 's' );
        return;
    }

    // ---- Step A: poll all outstanding job_ids in batches ----
    $in_flight = onionpress_wayback_posts_with_in_flight();
    // Add home + feed in-flight jobs
    foreach ( onionpress_wayback_sitewide_records() as $rec ) {
        $state = $rec['read']();
        // Poll on job_id alone, exactly as posts_with_in_flight() does.
        // Requiring an empty archived_at here used to open a second door
        // into the same deadlock: finalize_success writes archived_at and
        // clears job_id in two separate writes, so a process that dies
        // between them (autoheal restart, Apache wedge) leaves a sitewide
        // record with BOTH set. Such a record was excluded from the poll
        // here, skipped by the submit step (which ignores anything already
        // archived), and refused by invalidate_sitewide (which will not
        // touch a record with a job_id) — while the loop's drain probe
        // counted its job_id as outstanding work. Unreachable and
        // undrainable: the daemon spins forever, logging a clean sweep.
        if ( ! empty( $state['job_id'] ) ) {
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
        // Unknown age counts as ripe: a record with no submitted_at is a
        // zombie the poll needs to see so the branches below can retire it.
        $age = onionpress_wayback_job_age( $rec, $now );
        if ( $age === null || $age >= OP_WB_YOUNG_JOB_SKIP_SEC ) {
            $ripe_job_ids[] = $jid;
        }
    }
    // Counted before the budget cap below trims the list, so the log keeps
    // telling these two apart: "too young to be worth polling" and "no time
    // left to poll it this iteration" are different problems.
    $skipped_young = count( $in_flight ) - count( $ripe_job_ids );

    // Bound the poll by the time left, the same way the submit below is
    // bounded. poll_parallel runs ceil(N / (CONCURRENT_MAX * STATUS_BATCH_MAX))
    // SEQUENTIAL groups, each able to sit on its 40s curl timeout, so its
    // cost grows with the backlog without limit — it was the one phase of
    // the iteration with no budget check at all, and the phase most likely
    // to blow the budget is exactly the one that runs when the queue is
    // deepest. Jobs cut here are simply absent from $ripe_job_ids, so they
    // are neither polled nor judged forgotten; the next iteration takes
    // them. Floor at one group so a huge backlog still makes progress.
    $poll_per_group  = OP_WB_CONCURRENT_MAX * OP_WB_STATUS_BATCH_MAX;
    $poll_groups_fit = max( 1, (int) floor(
        max( 0, $deadline - microtime( true ) ) / 40
    ) );
    $poll_cap     = $poll_groups_fit * $poll_per_group;
    $polled_capped = max( 0, count( $ripe_job_ids ) - $poll_cap );
    if ( $polled_capped > 0 ) {
        $ripe_job_ids = array_slice( $ripe_job_ids, 0, $poll_cap );
    }

    $polled_success = 0;
    $polled_error   = 0;
    $polled_pending = 0;
    $polled_cdx_hit = 0;
    $polled_unknown = 0;
    $polled_lost    = 0;
    // Successes that came back with the HTML and none of its embeds.
    // Counted apart from $polled_success because they are not the same
    // outcome: the page is in the archive, but it will replay unstyled
    // and imageless. Folding them into "success" is what let a sweep
    // report a healthy run while producing bare pages.
    $polled_bare    = 0;
    $poll_covered   = array();
    $results        = onionpress_wayback_poll_parallel( $ripe_job_ids, $poll_covered );

    // First pass: finalize successes and pending; collect error-jobs for
    // a CDX fallback check. SPN's job memory is unreliable — it flips
    // success→error after a few minutes even when the capture persists
    // in CDX. Before trashing a post we verify against CDX directly.
    $cdx_check_urls  = array();
    $cdx_check_recs  = array();
    $cdx_check_exts  = array();
    $answered        = array();
    foreach ( $results as $res ) {
        if ( ! is_array( $res ) ) continue;
        $jid = (string) ( $res['job_id'] ?? '' );
        if ( ! isset( $in_flight[ $jid ] ) ) continue;
        $answered[ $jid ] = true;
        $rec    = $in_flight[ $jid ];
        $status = (string) ( $res['status'] ?? '' );

        if ( $status === 'success' ) {
            onionpress_wayback_finalize_success( $rec['write'], $rec['url'], $res );
            $rec['write']( array( 'job_id' => '', 'submitted_at' => '', 'last_error_ext' => '', 'last_error_at' => '' ) );
            $polled_success++;
            $res_state = onionpress_wayback_resources_state( $res, $rec['url'] );
            if ( $res_state !== OP_WB_RES_COMPLETE ) $polled_bare++;
            onionpress_wayback_log( 'Archived ' . $rec['key'] . ' ts=' . (string) ( $res['timestamp'] ?? '' )
                . ' dur=' . (string) ( $res['duration_sec'] ?? '' )
                . ' embeds=' . ( is_array( $res['resources'] ?? null )
                    ? onionpress_wayback_embed_count( $res['resources'], $rec['url'] ) : '?' )
                . ( $res_state === OP_WB_RES_COMPLETE ? '' : ' [' . $res_state . ': replays without images/CSS]' ) );
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
            $age    = onionpress_wayback_job_age( $rec, $now );
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

    // Every branch above keys off a status dict SPN actually returned. But
    // SPN has a fourth behaviour its API doesn't document: a job_id it has
    // entirely forgotten is simply ABSENT from the /save/status response —
    // no 'success', no 'error', not even 'pending'. Such a job matches no
    // branch, so it is never finalized and never cleared, while Step B
    // skips any record still carrying a job_id. That is a permanent
    // deadlock, and it is not theoretical: this site's home and feed sat
    // on forgotten job_ids for five days, archiving nothing, while the
    // sweep logged a healthy avail=40 every single minute.
    //
    // Treat "ripe, answered-about, and unmentioned" exactly like
    // stale-pending. Two independent guards keep a transient failure from
    // being read as amnesia:
    //
    //  - $poll_covered — only batches that came back HTTP 200 and parsed
    //    count. Without this, one 40s Tor timeout on a single 20-job batch
    //    would reclassify all 20 as forgotten and resubmit them, and at
    //    100+ in flight that is several batches per sweep.
    //  - STALE_PENDING_SEC — the same age at which the branch above gives
    //    up on a job SPN *did* answer 'pending' for. A job SPN has genuinely
    //    forgotten cannot be younger than that in any useful sense.
    foreach ( $ripe_job_ids as $jid ) {
        if ( isset( $answered[ $jid ] ) || ! isset( $in_flight[ $jid ] ) ) continue;
        $rec = $in_flight[ $jid ];
        if ( ! isset( $poll_covered[ $jid ] ) ) {
            // We never got an answer covering this job — say so rather
            // than guessing. Counted so the log can show the shortfall.
            $polled_lost++;
            continue;
        }
        $age = onionpress_wayback_job_age( $rec, $now );
        if ( $age !== null && $age <= OP_WB_STALE_PENDING_SEC ) continue;
        $rec['write']( array( 'job_id' => '', 'submitted_at' => '' ) );
        $polled_unknown++;
        onionpress_wayback_log( 'SPN forgot ' . $rec['key'] . ' (job_id absent from status response'
            . ( $age === null ? ', no submitted_at' : ', age=' . $age . 's' )
            . '), clearing for resubmit' );
    }

    // Second pass: for each SPN-errored job, verify against Wayback's
    // /wayback/available (with CDX fallback). If there's a capture,
    // mark archived; otherwise resubmit next tick.
    //
    // Cap the rescue burst so Tor SOCKS stays responsive to other
    // consumers (heartbeat, reachability). Jobs beyond the cap get
    // their error recorded and job_id cleared immediately — they'll
    // run through rescue on a later sweep when they're re-errored.
    // Out of budget: leave these records exactly as they are and let the
    // next sweep re-poll them. Deliberately NOT the $cdx_defer path — that
    // one records an error and clears the job_id, and doing that without
    // having checked CDX would throw away captures that do exist. Better
    // to do nothing than to record a verdict we did not verify.
    $over_budget = microtime( true ) >= $deadline;
    $cdx_skipped = $over_budget ? count( $cdx_check_urls ) : 0;
    $cdx_do_now  = $over_budget ? array() : array_slice( $cdx_check_urls, 0, 5, true );
    $cdx_defer   = $over_budget ? array() : array_slice( $cdx_check_urls, 5, null, true );
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
                // CDX proves the PAGE exists at $ts. It says nothing about
                // whether the images and stylesheet came with it, and we
                // never saw a `resources` list for this job — SPN called it
                // an error. Record that gap rather than leaving the field
                // blank, which reads downstream as a clean capture.
                $rec['write']( array(
                    'archived_at'     => time(),
                    'snapshot_ts'     => $ts,
                    'resources_state' => OP_WB_RES_UNVERIFIED,
                    'job_id'          => '',
                    'submitted_at'    => '',
                    'last_error_ext'  => '',
                    'last_error_at'   => '',
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
    // Bound the batch by the time left as well as by slots. submit_parallel
    // runs ceil(N / OP_WB_CONCURRENT_MAX) SEQUENTIAL groups, each able to
    // sit on its 40s curl timeout, so at the full 40-URL budget one call
    // can run 320s on its own — long enough to outlive the lock heartbeat
    // no matter what the phase-entry check said. Floor at one group:
    // making no progress at all is worse than one slightly long iteration.
    // The reachability probe below runs before the submit and costs up to
    // its own 20s timeout, so the batch has to be sized out of what is left
    // AFTER paying for it — otherwise the probe is 20s the budget granted
    // to the submit and then spent on something else.
    $groups_that_fit = max( 1, (int) floor(
        max( 0, $deadline - microtime( true ) - 20 ) / 40
    ) );
    $budget = max( 0, min(
        OP_WB_SUBMIT_BATCH_MAX,
        $available,
        $groups_that_fit * OP_WB_CONCURRENT_MAX
    ) );
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

    $submitted   = 0;
    $discarded   = 0;
    $hit_429     = false;
    $submit_skip = '';
    if ( ! empty( $to_submit ) && microtime( true ) >= $deadline ) {
        // Step A ate the whole budget. Submitting now would push the
        // iteration past OP_WB_LOOP_LOCK_STALE_SEC without heartbeating the
        // lock, at which point a second daemon claims it and two of them
        // run at once against the same queue.
        $submit_skip = 'over budget';
    } elseif ( ! empty( $to_submit ) && ! onionpress_wayback_self_reachable( $onion ) ) {
        // The gate this plugin's header has always advertised, finally
        // wired up — it was defined, documented, mocked by the tests, and
        // called from nowhere. SPN's crawler reaches us the same way a
        // client does, so submitting while our own onion is unreachable
        // spends a slot to archive a failure page and comes back
        // error:no-captures. Back off instead, using the constant that
        // existed for this and was likewise never referenced.
        // time(), not $now: $now was captured at the top of the iteration,
        // before a status call, a poll and a CDX pass that between them can
        // burn the whole budget. Dating the back-off from then makes a 120s
        // pause into a 40s one on a normal sweep, and into no pause at all
        // on the slow sweeps this gate exists for — the daemon would go
        // straight back to probing every 30s while logging "backing off".
        update_option( OP_WB_OPT_BACKOFF_UNTIL, time() + OP_WB_BACKOFF_UNREACHABLE, false );
        $submit_skip = 'own onion unreachable';
        onionpress_wayback_log( 'Sweep paused: own onion not answering over Tor, '
            . 'backing off ' . OP_WB_BACKOFF_UNREACHABLE . 's rather than '
            . 'submitting ' . count( $to_submit ) . ' URL(s) SPN would fail to capture' );
    }
    if ( ! empty( $to_submit ) && $submit_skip === '' ) {
        $submit_results = onionpress_wayback_submit_parallel( $to_submit );
        foreach ( $submit_results as $key => $result ) {
            $rec = $records_by_key[ $key ] ?? null;
            if ( $rec === null ) continue;
            if ( $result === 'RATE_LIMITED' ) {
                $hit_429 = true;
                continue;
            }
            if ( $result !== '' ) {
                // A write can be REFUSED, and then this is not a submission we
                // may count. A static record's write goes into the generation
                // it was built against (see static_write), so a publish landing
                // between building the batch and writing its results back makes
                // the write a no-op: the job_id is dropped, SPN still runs the
                // capture and still holds the slot, nothing ever polls it, and
                // the sweep would log submitted=N as if all was well. Count
                // what landed, and say out loud what did not.
                if ( $rec['write']( array( 'job_id' => $result, 'submitted_at' => time() ) ) === false ) {
                    $discarded++;
                    onionpress_wayback_log( 'Submitted ' . $rec['key'] . ' but could not record its '
                        . 'job_id (the generation it belongs to is no longer live); the capture '
                        . 'completes at SPN unpolled and the page is resubmitted under the new '
                        . 'generation' );
                    continue;
                }
                $submitted++;
            }
        }
        if ( $hit_429 ) {
            update_option( OP_WB_OPT_BACKOFF_UNTIL, time() + OP_WB_BACKOFF_NO_SLOTS, false );
            onionpress_wayback_log( 'Submit batch hit 429, backing off ' . OP_WB_BACKOFF_NO_SLOTS . 's' );
        }
    }

    // The line that has to be self-diagnosing. Replay the five-day outage
    // against the OLD format and every counter reads zero — the two jobs
    // stuck in flight appeared nowhere, so "nothing to do" and "everything
    // is wedged" printed identically, 1621 times a day. in-flight and
    // queued are what make those two distinguishable at a glance, and
    // avail now says outright when it is a guess rather than a reading.
    $elapsed = round( microtime( true ) - ( $deadline - OP_WB_SWEEP_BUDGET_SEC ), 2 );
    $notes   = array();
    if ( $user === null )       $notes[] = 'spn-status=unreachable';
    if ( $polled_lost > 0 )     $notes[] = 'poll-lost=' . $polled_lost;
    if ( $polled_capped > 0 )   $notes[] = 'poll-capped=' . $polled_capped;
    // "skipped", not "deferred": the $cdx_defer path retires its records,
    // this one deliberately leaves them for the next sweep. Borrowing the
    // other path's name would tell a reader the opposite of what happened.
    if ( $cdx_skipped > 0 )     $notes[] = 'cdx-skipped=' . $cdx_skipped;
    // Surfaced as a note, not a silent postmeta field: a run where every
    // capture came back bare looks identical to a healthy one in the
    // counters above, and that is the failure this note exists to name.
    if ( $polled_bare > 0 )     $notes[] = 'bare-captures=' . $polled_bare;
    if ( $discarded > 0 )       $notes[] = 'job-id-discarded=' . $discarded;
    if ( $submit_skip !== '' )  $notes[] = 'submit-skipped=' . str_replace( ' ', '-', $submit_skip );
    onionpress_wayback_log( sprintf(
        'Sweep: avail=%d%s in-flight=%d polled(success=%d cdx-hit=%d err=%d pending=%d '
        . 'forgotten=%d skipped-young=%d) queued=%d submitted=%d elapsed=%ss%s',
        $available, ( $user === null ? '?' : '' ), count( $in_flight ),
        $polled_success, $polled_cdx_hit, $polled_error, $polled_pending, $polled_unknown,
        $skipped_young,
        count( $to_submit ), $submitted, $elapsed,
        $notes ? ' [' . implode( ' ', $notes ) . ']' : ''
    ) );

    return array(
        'submitted' => $submitted,
        'success'   => $polled_success,
        'bare'      => $polled_bare,
        'cdx'       => $polled_cdx_hit,
        'error'     => $polled_error,
        'forgotten' => $polled_unknown,
        'lost'      => $polled_lost,
    );
}
add_action( 'onionpress_wayback_sweep', 'onionpress_wayback_sweep' );

/**
 * Make wp-cron run `onionpress_wayback_sweep` as soon as possible.
 * $reason is a short slug ('kick', 'recycle', 'comment') identifying the
 * caller; it is passed as the event's argument and MUST be distinct per
 * caller — see below.
 */
function onionpress_wayback_schedule_sweep_now( $reason ) {
    // The argument is load-bearing, not annotation. wp_schedule_single_event()
    // silently refuses — returns false, and every caller here discarded that
    // return — when an event for the same hook with the same args already
    // exists within 10 minutes. The recurring watchdog entry has args=[] and
    // a 300s interval, so it is ALWAYS inside that window: every no-args
    // call this plugin made scheduled precisely nothing, and what actually
    // ran the sweep was the recurring event happening to be due. That made
    // "archive right now" mean "within the next five minutes", which is not
    // what publish-then-archive is supposed to feel like. Confirmed against
    // the running WordPress rather than inferred from the docs.
    wp_schedule_single_event( time(), 'onionpress_wayback_sweep', array( $reason ) );
    // WP-Cron is pseudo-cron: scheduling only marks the event due, and
    // nothing runs it until some request triggers WP's cron check. On a
    // low-traffic onion site that request may not arrive for a long time,
    // so fire the loopback ourselves and don't wait on it.
    $cron_url = site_url( 'wp-cron.php?doing_wp_cron=' . microtime( true ) );
    wp_remote_post( $cron_url, array( 'timeout' => 0.01, 'blocking' => false, 'sslverify' => false ) );
}

/**
 * Clear the sweep back-off and start a sweep immediately. Used by the
 * admin "kick" button and by the static receiver's commit route.
 */
function onionpress_wayback_kick_now() {
    delete_option( OP_WB_OPT_BACKOFF_UNTIL );
    onionpress_wayback_schedule_sweep_now( 'kick' );
}

/**
 * Invalidate the home page + feed captures so the next kick re-archives
 * them. Skipped when a capture is already in flight — see save_post's
 * comment below for why that matters.
 */
function onionpress_wayback_invalidate_sitewide() {
    $home_state = onionpress_wayback_opt_read( OP_WB_OPT_HOME );
    $feed_state = onionpress_wayback_opt_read( OP_WB_OPT_FEED );
    if ( empty( $home_state['job_id'] ) ) {
        delete_option( OP_WB_OPT_HOME );
    }
    if ( empty( $feed_state['job_id'] ) ) {
        delete_option( OP_WB_OPT_FEED );
    }
}

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
    // Skip invalidation if a capture is already in flight: SPN's crawl
    // happens many seconds after submission, so the in-flight job will
    // already render this save_post's content. Wiping the option would
    // drop the job_id, causing the next sweep to resubmit and burn an
    // SPN slot. Coalescing bursts is structural, not explicit — one row
    // per subsite already collapses N saves into ≤1 fresh submission.
    onionpress_wayback_invalidate_sitewide();

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
            'resources_state' => '',
            'last_error_ext'  => '',
            'last_error_at'   => '',
        ) );
    }

    // Kick the sweep now rather than waiting for wp-cron's next organic
    // fire — a fresh publish is exactly when a human is likely watching.
    onionpress_wayback_kick_now();
    onionpress_wayback_log( 'save_post ' . $post_id . ': cleared home/feed'
        . ( $update && ! $is_imported ? ' + post meta' : '' ) . ', scheduled immediate sweep' );
}, 10, 3 );

// ───── wp_insert_comment hook: one re-archive per post after threading ─
// Social-importer threading folds replies into comments on parent posts.
// The parent's rendered HTML changes (now shows the conversation), so
// the existing SPN snapshot becomes stale — but our save_post policy
// keeps imported posts at "archive once" to avoid the importer-fix-up
// re-archive loop. The fix: invalidate the parent's snapshot exactly
// once when the FIRST comment lands, then never again. A thread of 12
// self-replies re-archives the parent one time; further comments are a
// no-op. Original posts (no _source_id) follow the existing edit-aware
// save_post path and don't need this hook.
add_action( 'wp_insert_comment', function ( $comment_id, $comment ) {
    $post_id = (int) $comment->comment_post_ID;
    if ( ! $post_id ) return;
    $post = get_post( $post_id );
    if ( ! $post || $post->post_status !== 'publish' ) return;
    if ( ! in_array( $post->post_type, array( 'post', 'page' ), true ) ) return;
    // Only matters for imported posts. Originals get re-archived via
    // save_post when actually edited, which doesn't fire on comment add.
    if ( (string) get_post_meta( $post_id, '_source_id', true ) === '' ) return;
    // Already re-snapshotted once after threading → skip.
    if ( (string) get_post_meta( $post_id, OP_WB_META_RESNAPSHOT_DONE, true ) === '1' ) return;
    // No prior snapshot exists yet → save_post will pick it up the
    // normal way; no need to re-snapshot something that hasn't been
    // captured at all.
    if ( (string) get_post_meta( $post_id, OP_WB_META_ARCHIVED_AT, true ) === '' ) return;

    update_post_meta( $post_id, OP_WB_META_RESNAPSHOT_DONE, '1' );
    onionpress_wayback_post_write( $post_id, array(
        'archived_at'     => '',
        'snapshot_ts'     => '',
        'job_id'          => '',
        'submitted_at'    => '',
        'original_url'    => '',
        'duration_sec'    => '',
        'resources_count' => '',
        'outlinks_count'  => '',
        'resources_state' => '',
        'last_error_ext'  => '',
        'last_error_at'   => '',
    ) );
    delete_option( OP_WB_OPT_BACKOFF_UNTIL );
    onionpress_wayback_schedule_sweep_now( 'comment' );
    onionpress_wayback_log( 'wp_insert_comment ' . $comment_id
        . ' on post ' . $post_id . ': cleared snapshot for re-archive (one-shot)' );
}, 10, 2 );

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
                    // Only RECURRING entries under the wrong interval. A
                    // one-shot has schedule === false, and every immediate
                    // sweep — kick, recycle handoff, comment re-snapshot —
                    // is a one-shot. This ran on every init, including the
                    // init inside the very wp-cron.php request the kick had
                    // just fired, so it deleted those events moments before
                    // wp_cron() would have run them. Between that and the
                    // dedupe documented on schedule_sweep_now(), "sweep now"
                    // had two independent reasons to do nothing at all.
                    $schedule = $entry['schedule'] ?? '';
                    if ( $schedule && $schedule !== 'onionpress_wayback_watchdog' ) {
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
            // Clear WP's `doing_cron` lock too. If a previous wp-cron
            // spawn died mid-run without cleaning up (container restart,
            // pkill), this transient blocks new wp-cron fires for up to
            // 60s from its timestamp. Each subsequent page load just
            // refreshes it via race, so it can stay stuck for a long
            // time in practice. Clearing it lets cron fire immediately.
            delete_transient( 'doing_cron' );
            onionpress_wayback_kick_now();
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
    $static_totals = onionpress_wayback_static_totals();
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
                    <th style="width:220px;">Total pages</th>
                    <td><?php echo number_format_i18n( $totals['total'] ); ?></td>
                </tr>
                <?php if ( $static_totals['total'] > 0 ) : ?>
                <tr>
                    <th>&nbsp;&nbsp;&nbsp;of which static-site pages</th>
                    <td><?php echo number_format_i18n( $static_totals['archived'] ); ?>
                        of <?php echo number_format_i18n( $static_totals['total'] ); ?> archived
                        <?php if ( $static_totals['in_flight'] > 0 ) : ?>
                            &middot; <?php echo number_format_i18n( $static_totals['in_flight'] ); ?> in flight
                        <?php endif; ?>
                        <br><span class="description">Pages of the published static site, which are
                        files rather than WordPress posts. They re-enter the queue on every
                        publish.</span>
                    </td>
                </tr>
                <?php endif; ?>
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
                        <?php if ( $next_cron ) :
                            // human_time_diff() is direction-agnostic, so an
                            // overdue event reads identically to a future one
                            // — confusing when the site has had no traffic
                            // for a while (WP-Cron only fires on web hits).
                            // Disambiguate explicitly.
                            $diff_label = human_time_diff( time(), $next_cron );
                            $is_overdue = ( $next_cron < time() );
                            ?>
                            <?php if ( $is_overdue ) : ?>
                                <strong>Overdue</strong> by <?php echo esc_html( $diff_label ); ?>
                                <small style="color:#666;">(scheduled for <?php echo esc_html( date( 'Y-m-d H:i:s', $next_cron ) ); ?>; will fire on the next page load — WP-Cron requires HTTP traffic to tick)</small>
                            <?php else : ?>
                                in <?php echo esc_html( $diff_label ); ?>
                                (<?php echo esc_html( date( 'H:i:s', $next_cron ) ); ?>)
                            <?php endif; ?>
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

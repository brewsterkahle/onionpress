<?php
/**
 * Plugin Name: OnionPress Name Sync
 * Description: Registers each new WordPress username with OnionHome as an
 *              onionname. Does NOT release names when users are deleted —
 *              a registered onionname stays claimed forever so that
 *              Wayback-Machine archives of that user's posts remain
 *              resolvable via onionpress.org/<onionname>. The tor
 *              container does the signing (it has the HS private key);
 *              this plugin just tells the tor container which user was
 *              created.
 * Version:     1.0
 * Network:     true
 * License:     AGPL-3.0-or-later
 * License URI: https://www.gnu.org/licenses/agpl-3.0.html
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

// Local Docker-network endpoint on the tor container. Source-IP-filtered
// on the server side so it is not reachable from outside the compose
// network even though it rides on the same port as the onion-exposed API.
if ( ! defined( 'ONIONPRESS_NAMESYNC_ENDPOINT' ) ) {
    define( 'ONIONPRESS_NAMESYNC_ENDPOINT', 'http://onionpress-tor:8083' );
}

/**
 * Log through WP's error_log so the entry lands in docker logs alongside
 * other WordPress output and onionpress-wordpress container stderr.
 */
function onionpress_namesync_log( $message ) {
    error_log( '[onionpress-name-sync] ' . $message );
}

/**
 * POST {"onionname": <login>} to /api/name/{register,release}-local and
 * record the result. Never throws — user creation/deletion must not be
 * blocked by a registrar outage.
 *
 * Uses direct curl rather than wp_remote_post because onionpress-tor-proxy
 * globally routes WP HTTP through the Tor SOCKS proxy, which would turn a
 * cheap Docker-network call into a full Tor round-trip and fail when the
 * SOCKS proxy doesn't know how to reach docker-internal hostnames.
 */
function onionpress_namesync_call( $action, $onionname, $attempts = 3 ) {
    $url  = ONIONPRESS_NAMESYNC_ENDPOINT . '/api/name/' . $action . '-local';
    $body = wp_json_encode( array( 'onionname' => $onionname ) );

    // The relay to OnionHome is synchronous over Tor, so a transient Tor
    // failure (or OnionHome being briefly down) surfaces here as a curl
    // error or 5xx. Those are worth retrying; 4xx responses (bad name,
    // name taken by another address) are permanent and are not.
    $delays = array( 2, 5 );
    for ( $try = 1; $try <= $attempts; $try++ ) {
        $ch = curl_init( $url );
        if ( ! $ch ) {
            onionpress_namesync_log( "$action $onionname: curl_init failed" );
            return false;
        }
        curl_setopt_array( $ch, array(
            CURLOPT_POST           => 1,
            CURLOPT_POSTFIELDS     => $body,
            CURLOPT_HTTPHEADER     => array( 'Content-Type: application/json' ),
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_TIMEOUT        => 35,
            CURLOPT_CONNECTTIMEOUT => 5,
            CURLOPT_USERAGENT      => 'onionpress-name-sync/1.0',
        ) );
        $response = curl_exec( $ch );
        $code     = (int) curl_getinfo( $ch, CURLINFO_HTTP_CODE );
        $err      = curl_error( $ch );
        curl_close( $ch );

        if ( $response !== false && ! $err && $code >= 200 && $code < 300 ) {
            $short = is_string( $response ) ? substr( $response, 0, 300 ) : '';
            onionpress_namesync_log( "$action $onionname: $code $short" . ( $try > 1 ? " (attempt $try)" : '' ) );
            return true;
        }
        if ( $response !== false && ! $err && $code >= 400 && $code < 500 ) {
            $short = is_string( $response ) ? substr( $response, 0, 300 ) : '';
            onionpress_namesync_log( "$action $onionname: $code $short (permanent, not retrying)" );
            return false;
        }
        $reason = $err ? "ERROR $err" : "HTTP $code";
        if ( $try < $attempts ) {
            $delay = $delays[ min( $try, count( $delays ) ) - 1 ];
            onionpress_namesync_log( "$action $onionname: $reason (attempt $try/$attempts, retrying in {$delay}s)" );
            sleep( $delay );
        } else {
            onionpress_namesync_log( "$action $onionname: $reason (attempt $try/$attempts, giving up — daily reconcile will retry)" );
        }
    }
    return false;
}

/**
 * Fires when a new WordPress user is created — single-site and multisite
 * both dispatch this hook. Multisite-specific hooks like wpmu_new_user
 * also fire, but always in addition to user_register, so we only hook
 * user_register and rely on the server's idempotent register to absorb
 * any accidental double-calls (same address + same name = 200 OK).
 */
function onionpress_namesync_on_register( $user_id ) {
    $user = get_userdata( $user_id );
    if ( ! $user || empty( $user->user_login ) ) {
        return;
    }
    onionpress_namesync_call( 'register', $user->user_login );
}
add_action( 'user_register', 'onionpress_namesync_on_register', 10, 1 );

// Note: subsite creation is deliberately NOT a registration trigger.
// Only user accounts claim onionnames — in the standard signup flow the
// subsite and its owner share a slug, so the user_register hook above
// covers it. Misc extra subsites (test sites, admin experiments) stay
// out of the global directory.

// Note: we intentionally do NOT hook delete_user / wpmu_delete_user.
// Once an onionname is registered with OnionHome it stays registered
// forever so that Wayback-Machine archives of that user's posts remain
// resolvable via onionpress.org/<onionname>. Explicit release is only
// possible via the signed /api/name/release endpoint (operator action).

/**
 * Daily reconcile: re-register every user on the network with OnionHome.
 *
 * The at-creation registration above is a single shot — if OnionHome or
 * Tor is unreachable at that moment the name is silently never claimed,
 * and users created before this plugin shipped were never registered at
 * all. The server's register is idempotent (same address + same name =
 * 200 OK), so re-registering everyone daily is safe: it backfills missed
 * names, self-heals after outages, and refreshes last_seen_at so the
 * directory reflects which installs are alive.
 *
 * Scheduled on the main site only — every subsite runs this mu-plugin,
 * but user accounts are network-global, and the tor container's health
 * checks hit the main site every minute so its wp-cron fires reliably
 * even on an otherwise idle install.
 */
add_action( 'init', function () {
    if ( is_multisite() && ! is_main_site() ) {
        return;
    }
    if ( ! wp_next_scheduled( 'onionpress_namesync_reconcile' ) ) {
        wp_schedule_event( time() + MINUTE_IN_SECONDS, 'daily', 'onionpress_namesync_reconcile' );
    }
} );

add_action( 'onionpress_namesync_reconcile', 'onionpress_namesync_reconcile_all' );
function onionpress_namesync_reconcile_all() {
    // blog_id 0 = all users on the network, not just the current site.
    // Every subsite shares this install's single .onion address, and the
    // OnionHome registry maps any number of names to one address. Only
    // user logins are registered — subsites without a user (test sites,
    // admin experiments) deliberately stay out of the directory.
    $users = get_users( array( 'blog_id' => 0, 'fields' => array( 'user_login' ) ) );
    $ok    = 0;
    $total = 0;
    foreach ( $users as $u ) {
        if ( empty( $u->user_login ) ) {
            continue;
        }
        $total++;
        // Single attempt per name — the daily cadence is the retry.
        if ( onionpress_namesync_call( 'register', $u->user_login, 1 ) ) {
            $ok++;
        }
    }
    onionpress_namesync_log( "reconcile: $ok/$total user registrations confirmed" );
}

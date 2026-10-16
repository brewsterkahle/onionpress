<?php
/**
 * Plugin Name: OnionPress Social Archive — Mastodon Importer
 * Description: Pull a Mastodon account's public posts into the unified
 *              social_post archive via the public API. One handle, one
 *              click: backfills full history, then polls every few
 *              minutes to stay current. No takeout ZIP, no credentials.
 *              Safe to re-run — status IDs are idempotency keys.
 * Version:     0.1
 * Network:     true
 * License:     AGPL-3.0-or-later
 * License URI: https://www.gnu.org/licenses/agpl-3.0.html
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

add_action( 'plugins_loaded', function () {
    if ( function_exists( 'onionpress_social_register_importer' ) ) {
        onionpress_social_register_importer( 'mastodon' );
    }
} );

const ONIONPRESS_MASTODON_ADMIN_SLUG  = 'onionpress-social-archive-mastodon';
const ONIONPRESS_MASTODON_HANDLE_OPT  = 'onionpress_social_mastodon_handle';      // full "user@server" for card display
const ONIONPRESS_MASTODON_SERVER_OPT  = 'onionpress_social_mastodon_server';
const ONIONPRESS_MASTODON_USER_OPT    = 'onionpress_social_mastodon_username';
const ONIONPRESS_MASTODON_ACCT_ID_OPT = 'onionpress_social_mastodon_account_id';
const ONIONPRESS_MASTODON_OLDEST_OPT  = 'onionpress_social_mastodon_oldest_id';
const ONIONPRESS_MASTODON_NEWEST_OPT  = 'onionpress_social_mastodon_newest_id';
// Owner markers for the above. Each oldest/newest value is only valid
// when its owner matches the current account_id — if someone swapped
// the account_id out from under us (tests, manual wp-cli, buggy save
// path) the stored cursors are implicitly invalidated and we re-walk.
// See onionpress_mastodon_get_oldest_for() / _set_oldest_for().
const ONIONPRESS_MASTODON_OLDEST_OWNER_OPT = 'onionpress_social_mastodon_oldest_owner';
const ONIONPRESS_MASTODON_NEWEST_OWNER_OPT = 'onionpress_social_mastodon_newest_owner';
const ONIONPRESS_MASTODON_STATUSES_OPT = 'onionpress_social_mastodon_total_statuses';
const ONIONPRESS_MASTODON_LAST_SYNC   = 'onionpress_social_mastodon_last_sync';
const ONIONPRESS_MASTODON_LAST_NOTE   = 'onionpress_social_mastodon_last_note';
const ONIONPRESS_MASTODON_LOCK        = 'onionpress_social_mastodon_lock';
const ONIONPRESS_MASTODON_OPTS_OPT    = 'onionpress_social_mastodon_opts';
const ONIONPRESS_MASTODON_CRON_HOOK   = 'onionpress_social_mastodon_sync';

// Route everything through the bulk-outgoing Tor daemon in the onionheaven
// container, per the "separate Tor in/out" architecture. onionpress-tor is
// reserved for the hosted onion service.
const ONIONPRESS_MASTODON_SOCKS_PROXY = 'onionheaven:9050';

// Budget per tick. Most of the cost is media sideload (~3-5s per
// attachment over Tor); a strict wall-clock ceiling keeps the tick
// finishing cleanly regardless of how heavy the posts are. The page
// cap is a hard upper bound so a tick on a text-only account can't
// run the full history in one go either. 25s clears PHP's default 30s
// max_execution_time with a margin.
const ONIONPRESS_MASTODON_TICK_BUDGET_SEC = 25;
const ONIONPRESS_MASTODON_PAGES_PER_TICK  = 8;
const ONIONPRESS_MASTODON_PER_PAGE        = 40;

// Daemon-style outer loop: one wp-cron fire drives the whole backfill
// to completion rather than stopping after a tick and hoping wp-cron
// fires again soon. Lock lives in wp_options and is heartbeated every
// tick; a stale lock (no heartbeat for this long) means the prior
// daemon died and the next cron fire can take over.
const ONIONPRESS_MASTODON_DAEMON_LOCK      = 'onionpress_social_mastodon_daemon_lock';
const ONIONPRESS_MASTODON_DAEMON_MAX_SEC   = 1800; // 30 min per invocation, enough for ~45k statuses
const ONIONPRESS_MASTODON_DAEMON_STALE_SEC = 300;  // heartbeat older than this = dead process
const ONIONPRESS_MASTODON_DAEMON_IDLE_SEC  = 3;    // politeness pause between ticks (API rate-limit hygiene)
const ONIONPRESS_MASTODON_HTTP_TIMEOUT    = 45;

// Self-reply threading: replies to your own toots are imported as WP
// comments on the parent's post (so a thread reads as post + comments,
// not a flood of fragmentary top-level posts). Backfill walks
// newest→oldest, so a reply usually arrives before its parent — those
// land as top-level posts marked _pending_reattach, and a sweep at the
// end of each tick converts them once the parent shows up.
const ONIONPRESS_MASTODON_THREADS_MIGRATED_OPT = 'onionpress_mastodon_threads_v1_migrated';

// Conversation context: when WE reply to someone else's toot, the parent
// (and its parents, until we find one of our own toots or hit a depth
// cap) gets fetched from Mastodon and imported too — marked with
// _is_context=1 so themes can render them with author attribution. This
// turns "your fragmentary reply alone" into "the full conversation
// hanging off your original toot."
const ONIONPRESS_MASTODON_CONTEXT_DEPTH_MAX     = 6;
const ONIONPRESS_MASTODON_CONTEXT_BATCH_PER_TICK = 10; // ancestor fetches per backfill tick (Tor-routed, slow)

add_action( 'admin_menu', function () {
    if ( ! defined( 'ONIONPRESS_SOCIAL_ADMIN_SLUG' ) ) {
        return;
    }
    add_submenu_page(
        ONIONPRESS_SOCIAL_ADMIN_SLUG,
        'Import Mastodon',
        'Mastodon',
        'manage_options',
        ONIONPRESS_MASTODON_ADMIN_SLUG,
        'onionpress_mastodon_import_page'
    );
}, 20 );

/**
 * Schedule the recurring poll on first admin hit after a handle is
 * configured. mu-plugins have no activation hook, so we lazy-schedule.
 */
add_action( 'admin_init', function () {
    if ( ! wp_next_scheduled( ONIONPRESS_MASTODON_CRON_HOOK )
         && get_option( ONIONPRESS_MASTODON_ACCT_ID_OPT ) ) {
        wp_schedule_event( time() + 60, 'onionpress_mastodon_5min', ONIONPRESS_MASTODON_CRON_HOOK );
    }
    // One-shot conversion of self-reply posts that predate threading.
    // The function self-gates on its own option flag, so this is a
    // no-op after the first successful run.
    if ( get_option( ONIONPRESS_MASTODON_THREADS_MIGRATED_OPT ) !== 'yes'
         && get_option( ONIONPRESS_MASTODON_ACCT_ID_OPT ) ) {
        onionpress_mastodon_migrate_self_replies_to_comments();
    }
} );

add_filter( 'cron_schedules', function ( $schedules ) {
    if ( ! isset( $schedules['onionpress_mastodon_5min'] ) ) {
        $schedules['onionpress_mastodon_5min'] = array(
            'interval' => 300,
            'display'  => '5 min (OnionPress Mastodon poll)',
        );
    }
    return $schedules;
} );

add_action( ONIONPRESS_MASTODON_CRON_HOOK, 'onionpress_mastodon_run_sync_tick' );

/**
 * Owner-aware accessors for the oldest/newest cursors. Structural
 * guard: if the stored owner doesn't match the current account_id,
 * return empty — the cursor was set against a different account and
 * is implicitly invalid. Makes cross-account cursor drift (tests,
 * manual option pokes, interrupted save_handle flow) impossible.
 */
function onionpress_mastodon_get_oldest_for( $acct_id ) {
    $owner = (string) get_option( ONIONPRESS_MASTODON_OLDEST_OWNER_OPT, '' );
    if ( $owner !== '' && $owner !== $acct_id ) {
        return '';
    }
    return (string) get_option( ONIONPRESS_MASTODON_OLDEST_OPT, '' );
}
function onionpress_mastodon_set_oldest_for( $acct_id, $value ) {
    update_option( ONIONPRESS_MASTODON_OLDEST_OPT, $value );
    update_option( ONIONPRESS_MASTODON_OLDEST_OWNER_OPT, $acct_id );
}
function onionpress_mastodon_get_newest_for( $acct_id ) {
    $owner = (string) get_option( ONIONPRESS_MASTODON_NEWEST_OWNER_OPT, '' );
    if ( $owner !== '' && $owner !== $acct_id ) {
        return '';
    }
    return (string) get_option( ONIONPRESS_MASTODON_NEWEST_OPT, '' );
}
function onionpress_mastodon_set_newest_for( $acct_id, $value ) {
    update_option( ONIONPRESS_MASTODON_NEWEST_OPT, $value );
    update_option( ONIONPRESS_MASTODON_NEWEST_OWNER_OPT, $acct_id );
}
function onionpress_mastodon_clear_cursors() {
    delete_option( ONIONPRESS_MASTODON_OLDEST_OPT );
    delete_option( ONIONPRESS_MASTODON_OLDEST_OWNER_OPT );
    delete_option( ONIONPRESS_MASTODON_NEWEST_OPT );
    delete_option( ONIONPRESS_MASTODON_NEWEST_OWNER_OPT );
}

/**
 * Extract the server portion ("user@server") from a stored handle, lower-cased.
 * Returns '' if the handle is empty or unparseable. Used to detect drift between
 * the user-visible handle and the routing options (_server / _account_id) — a
 * symptom of test pollution or an interrupted save flow.
 */
function onionpress_mastodon_handle_server( $handle ) {
    $handle = trim( (string) $handle );
    if ( $handle === '' ) {
        return '';
    }
    $parts = explode( '@', ltrim( $handle, '@' ), 2 );
    if ( count( $parts ) !== 2 ) {
        return '';
    }
    return strtolower( trim( $parts[1] ) );
}

function onionpress_mastodon_import_page() {
    if ( ! current_user_can( 'manage_options' ) ) {
        wp_die( 'Unauthorized' );
    }

    $notice = null;
    if ( $_SERVER['REQUEST_METHOD'] === 'POST' ) {
        if ( isset( $_POST['onionpress_mastodon_save_handle'] ) ) {
            check_admin_referer( 'onionpress_mastodon_save_handle', 'onionpress_mastodon_handle_nonce' );
            $notice = onionpress_mastodon_handle_save_handle_post();
        } elseif ( isset( $_POST['onionpress_mastodon_sync_now'] ) ) {
            check_admin_referer( 'onionpress_mastodon_sync_now', 'onionpress_mastodon_sync_nonce' );
            // Kick the daemon asynchronously instead of running it
            // synchronously. The daemon runs up to 30 min, which would
            // hang the admin request. Instead: schedule an immediate
            // cron event and spawn a loopback to wp-cron.php so WP
            // actually fires it right now. The admin page then re-
            // renders with a "running" indicator that auto-refreshes.
            delete_option( ONIONPRESS_MASTODON_DAEMON_LOCK );
            delete_transient( 'doing_cron' );
            wp_schedule_single_event( time(), ONIONPRESS_MASTODON_CRON_HOOK );
            $cron_url = site_url( 'wp-cron.php?doing_wp_cron=' . microtime( true ) );
            wp_remote_post( $cron_url, array( 'timeout' => 0.01, 'blocking' => false, 'sslverify' => false ) );
            $notice = array( 'level' => 'success', 'message' => 'Sync started. Progress below — this page will refresh automatically.' );
        } elseif ( isset( $_POST['onionpress_mastodon_reset'] ) ) {
            check_admin_referer( 'onionpress_mastodon_reset', 'onionpress_mastodon_reset_nonce' );
            $notice = onionpress_mastodon_reset_cursors();
        }
    }

    $handle        = (string) get_option( ONIONPRESS_MASTODON_HANDLE_OPT, '' );
    $server        = (string) get_option( ONIONPRESS_MASTODON_SERVER_OPT, '' );
    $account_id    = (string) get_option( ONIONPRESS_MASTODON_ACCT_ID_OPT, '' );
    $total         = (int) get_option( ONIONPRESS_MASTODON_STATUSES_OPT, 0 );
    $imported_now  = onionpress_social_count_for_source( 'mastodon' );
    $last_sync     = (int) get_option( ONIONPRESS_MASTODON_LAST_SYNC, 0 );
    $last_note     = (string) get_option( ONIONPRESS_MASTODON_LAST_NOTE, '' );
    $oldest_id     = onionpress_mastodon_get_oldest_for( $account_id );
    $opts          = (array) get_option( ONIONPRESS_MASTODON_OPTS_OPT, array( 'include_replies' => 1 ) );

    $backfill_done = ( $account_id !== '' ) && ( $oldest_id === '' || $oldest_id === 'done' );

    // Daemon running indicator: read the daemon lock and compute age.
    // Lock format: "<token>:<last_heartbeat_unixts>". Anything fresher
    // than OP_WB-style stale threshold is "running."
    $dlock_raw  = (string) get_option( ONIONPRESS_MASTODON_DAEMON_LOCK, '' );
    $dlock_ts   = 0;
    if ( strpos( $dlock_raw, ':' ) !== false ) {
        list( , $dlock_ts_str ) = explode( ':', $dlock_raw, 2 );
        $dlock_ts = (int) $dlock_ts_str;
    }
    $dlock_age    = $dlock_ts > 0 ? time() - $dlock_ts : 0;
    $daemon_alive = $dlock_ts > 0 && $dlock_age < ONIONPRESS_MASTODON_DAEMON_STALE_SEC;

    ?>
    <div class="wrap">
        <?php if ( $daemon_alive ) : ?>
            <meta http-equiv="refresh" content="15">
        <?php endif; ?>
        <h1>Import Mastodon</h1>

        <?php if ( $notice ) : ?>
            <div class="notice notice-<?php echo esc_attr( $notice['level'] ); ?>">
                <p><?php echo wp_kses_post( $notice['message'] ); ?></p>
            </div>
        <?php endif; ?>

        <h2>Step 1 &mdash; Your Mastodon address</h2>
        <p>Enter your full Mastodon address, including the server. Example: <code>@brewsterkahle@mastodon.archive.org</code>.</p>
        <form method="post" style="margin-bottom:1.25em;">
            <?php wp_nonce_field( 'onionpress_mastodon_save_handle', 'onionpress_mastodon_handle_nonce' ); ?>
            <input type="hidden" name="onionpress_mastodon_save_handle" value="1">
            <table class="form-table" role="presentation">
                <tr>
                    <th><label for="mastodon_handle">Address</label></th>
                    <td>
                        <input type="text" id="mastodon_handle" name="mastodon_handle"
                               value="<?php echo esc_attr( $handle ); ?>"
                               placeholder="@you@mastodon.archive.org"
                               class="regular-text" style="max-width:380px;">
                        <fieldset style="margin-top:0.5em;">
                            <label><input type="checkbox" name="include_boosts" value="1" <?php checked( ! empty( $opts['include_boosts'] ) ); ?>> Include boosts (reblogs of other people's posts)</label><br>
                            <label><input type="checkbox" name="include_replies" value="1" <?php checked( ! empty( $opts['include_replies'] ) ); ?>> Include replies to other people</label>
                        </fieldset>
                        <p class="description">Originals and self-threads are always imported. Public posts only &mdash; followers-only and DMs need a future OAuth step.</p>
                        <?php submit_button( $account_id ? 'Update' : 'Save & look up account', 'primary', 'submit', false ); ?>
                    </td>
                </tr>
            </table>
        </form>

        <?php
        // Routing-drift warning: the displayed handle parses to a different
        // server than the one we'd actually poll. Symptoms include test
        // pollution writing _server/_account_id without touching _handle, or
        // an interrupted save where the form half-completed. Either way the
        // Sync button would silently poll the wrong host, so block it visibly.
        $handle_server = onionpress_mastodon_handle_server( $handle );
        $routing_drift = ( $handle !== '' && $server !== '' && $handle_server !== '' && $handle_server !== $server );
        if ( $routing_drift ) : ?>
            <div class="notice notice-error">
                <p><strong>Stored server doesn't match the address shown above.</strong>
                    Address parses to <code><?php echo esc_html( $handle_server ); ?></code>
                    but routing is pinned to <code><?php echo esc_html( $server ); ?></code>
                    (account id <code><?php echo esc_html( $account_id ); ?></code>).
                    Re-enter the address and click <strong>Save</strong> to re-resolve the account.
                    Until then, <strong>Sync now</strong> will fetch nothing.</p>
            </div>
        <?php endif; ?>

        <?php if ( $account_id && ! $routing_drift ) :
            // Drift warning: backfill claims done but imported count
            // is far below what the server said existed. Catches test
            // pollution and interrupted walks. 50% threshold leaves
            // room for users opting out of boosts (which count toward
            // the server's total) and for ordinary edits/deletions,
            // while still flagging "1,417 reported, 12 imported."
            $drift_warn = ( $backfill_done
                            && $total > 0
                            && $imported_now > 0
                            && $imported_now < (int) ( $total * 0.5 ) );
            if ( $drift_warn ) : ?>
                <div class="notice notice-warning">
                    <p><strong>Backfill ended early.</strong> Imported
                        <strong><?php echo number_format_i18n( $imported_now ); ?></strong>
                        of <strong><?php echo number_format_i18n( $total ); ?></strong>
                        statuses reported by the server. The cursor is in a bad
                        state (test pollution, interrupted walk, or account
                        swap). Click <strong>Reset cursors</strong> below then
                        <strong>Sync now</strong> — already-imported statuses
                        will be deduplicated automatically.</p>
                </div>
            <?php endif; ?>
            <h2>Step 2 &mdash; Sync</h2>
            <table class="wp-list-table widefat" style="max-width:620px;">
                <tbody>
                    <tr><th style="width:200px;">Account</th>
                        <td><code>@<?php echo esc_html( $handle ); ?></code> <span style="color:#888;">(id <?php echo esc_html( $account_id ); ?>)</span></td></tr>
                    <tr><th>Server reports</th>
                        <td><?php echo number_format_i18n( $total ); ?> total statuses
                            <small style="color:#666;">(including replies and boosts)</small>
                        </td></tr>
                    <tr><th>Imported here</th>
                        <td><?php echo number_format_i18n( $imported_now ); ?>
                            <small style="color:#666;">
                                (filtered by the "include replies / include boosts" options below;
                                skipped items still count as processed so the backfill advances)
                            </small>
                        </td></tr>
                    <tr><th>Backfill</th><td><?php
                        if ( $daemon_alive ) {
                            $age = max( 0, $dlock_age );
                            echo '<strong style="color:#2a7b2a;">● Running</strong> — last heartbeat ' . esc_html( $age ) . 's ago. This page auto-refreshes every 15s while syncing.';
                        } elseif ( $backfill_done ) {
                            echo '<strong style="color:#2a7b2a;">✓ complete</strong>';
                        } else {
                            echo '<em>paused — click "Sync now" to start or resume.</em>';
                        }
                    ?></td></tr>
                    <tr><th>Last sync</th><td><?php echo $last_sync ? esc_html( human_time_diff( $last_sync ) ) . ' ago' : '&mdash;'; ?>
                        <?php if ( $last_note ) : ?><br><small style="color:#666;"><?php echo esc_html( $last_note ); ?></small><?php endif; ?></td></tr>
                </tbody>
            </table>

            <form method="post" style="margin-top:1em;display:inline-block;">
                <?php wp_nonce_field( 'onionpress_mastodon_sync_now', 'onionpress_mastodon_sync_nonce' ); ?>
                <input type="hidden" name="onionpress_mastodon_sync_now" value="1">
                <?php submit_button( 'Sync now', 'primary', 'submit', false ); ?>
            </form>
            <form method="post" style="margin-top:1em;display:inline-block;margin-left:0.5em;"
                  onsubmit="return confirm('This clears the sync cursors (not the imported posts). Next sync will rescan from scratch but skip everything already imported. Continue?');">
                <?php wp_nonce_field( 'onionpress_mastodon_reset', 'onionpress_mastodon_reset_nonce' ); ?>
                <input type="hidden" name="onionpress_mastodon_reset" value="1">
                <?php submit_button( 'Reset cursors', 'secondary', 'submit', false ); ?>
            </form>

            <h2 style="margin-top:2em;">Recent imports</h2>
            <?php onionpress_mastodon_render_recent(); ?>
        <?php endif; ?>
    </div>
    <?php
}

/**
 * Save the user's Mastodon handle. Parses "@user@server" (leading @
 * optional), validates both halves, stores split pieces, and does a
 * live account lookup so we catch typos immediately. Also saves the
 * per-click import options (boosts / replies).
 */
function onionpress_mastodon_handle_save_handle_post() {
    $raw = isset( $_POST['mastodon_handle'] ) ? wp_unslash( $_POST['mastodon_handle'] ) : '';
    $raw = trim( (string) $raw );
    // Allow "user@server", "@user@server", or a full profile URL.
    if ( preg_match( '~^https?://([^/]+)/@([^/?#]+)~i', $raw, $m ) ) {
        $server   = strtolower( $m[1] );
        $username = $m[2];
    } else {
        $stripped = ltrim( $raw, '@' );
        $parts    = explode( '@', $stripped, 2 );
        if ( count( $parts ) !== 2 ) {
            return array( 'level' => 'error', 'message' => 'Enter your full address including the server, like <code>@you@mastodon.archive.org</code>.' );
        }
        list( $username, $server ) = $parts;
        $server = strtolower( trim( $server ) );
    }

    if ( ! preg_match( '/^[A-Za-z0-9_]{1,30}$/D', $username ) ) {
        return array( 'level' => 'error', 'message' => 'The username part must be letters, digits, or underscores (up to 30 chars).' );
    }
    if ( ! preg_match( '/^[a-z0-9]([a-z0-9\-]{0,62}\.)+[a-z]{2,}$/D', $server ) ) {
        return array( 'level' => 'error', 'message' => 'The server must look like a domain, e.g. <code>mastodon.archive.org</code>.' );
    }

    // Save the options (boosts/replies) regardless of lookup outcome so
    // the user doesn't have to re-tick them if the lookup fails once.
    $opts = array(
        'include_boosts'  => ! empty( $_POST['include_boosts'] ),
        'include_replies' => ! empty( $_POST['include_replies'] ),
    );
    update_option( ONIONPRESS_MASTODON_OPTS_OPT, $opts );

    // Live lookup against the declared server.
    $lookup = onionpress_mastodon_api_get(
        'https://' . $server . '/api/v1/accounts/lookup?acct=' . rawurlencode( $username )
    );
    if ( is_wp_error( $lookup ) ) {
        return array( 'level' => 'error', 'message' => 'Could not reach <code>' . esc_html( $server ) . '</code> over Tor: ' . esc_html( $lookup->get_error_message() ) );
    }
    if ( (int) $lookup['code'] !== 200 || empty( $lookup['json']['id'] ) ) {
        return array( 'level' => 'error', 'message' => 'Server returned HTTP ' . intval( $lookup['code'] ) . ' for <code>@' . esc_html( $username ) . '@' . esc_html( $server ) . '</code>. Check spelling.' );
    }

    $acct_full  = $username . '@' . $server;
    $account_id = (string) $lookup['json']['id'];
    $total      = isset( $lookup['json']['statuses_count'] ) ? (int) $lookup['json']['statuses_count'] : 0;

    // If the user is switching to a different account, reset cursors so
    // a fresh backfill walks the new account's history. The owner-aware
    // accessors below also defend against someone writing ACCT_ID_OPT
    // directly (tests, manual wp-cli) without going through this path.
    $prev_id = (string) get_option( ONIONPRESS_MASTODON_ACCT_ID_OPT, '' );
    if ( $prev_id !== '' && $prev_id !== $account_id ) {
        onionpress_mastodon_clear_cursors();
    }

    update_option( ONIONPRESS_MASTODON_HANDLE_OPT,  $acct_full );
    update_option( ONIONPRESS_MASTODON_USER_OPT,    $username );
    update_option( ONIONPRESS_MASTODON_SERVER_OPT,  $server );
    update_option( ONIONPRESS_MASTODON_ACCT_ID_OPT, $account_id );
    update_option( ONIONPRESS_MASTODON_STATUSES_OPT, $total );

    // Kick off the recurring poll immediately if it wasn't scheduled yet.
    if ( ! wp_next_scheduled( ONIONPRESS_MASTODON_CRON_HOOK ) ) {
        wp_schedule_event( time() + 30, 'onionpress_mastodon_5min', ONIONPRESS_MASTODON_CRON_HOOK );
    }

    return array(
        'level'   => 'success',
        'message' => 'Saved <code>@' . esc_html( $acct_full ) . '</code>. Server reports <strong>' . number_format_i18n( $total ) . '</strong> statuses. Click <strong>Sync now</strong> below to start importing.',
    );
}

/**
 * Discard the backfill/since cursors without deleting imported posts.
 * Useful if a previous import was interrupted in a bad state.
 */
function onionpress_mastodon_reset_cursors() {
    onionpress_mastodon_clear_cursors();
    return array( 'level' => 'success', 'message' => 'Cursors cleared. Next sync will rescan from scratch (but will skip already-imported statuses).' );
}

/**
 * Entry point wired to wp-cron and the admin button. Daemon-style:
 * one invocation runs the inner loop until the backfill is fully
 * drained (oldest_id === 'done'), then exits. If the loop crashes or
 * the container restarts, the next wp-cron fire detects the stale
 * heartbeat and takes over — so "does wp-cron eventually fire?" is
 * the only requirement for recovery.
 *
 * A token-based mutex in wp_options prevents overlapping daemons:
 * whoever claims the lock first runs; any other invocation sees the
 * fresh heartbeat and exits without entering the loop.
 *
 * Returns an admin-notice-shaped array when called from admin UI so
 * the page can render the result inline; ignored by cron.
 */
function onionpress_mastodon_run_sync_tick( $from_admin = false ) {
    $server     = (string) get_option( ONIONPRESS_MASTODON_SERVER_OPT, '' );
    $account_id = (string) get_option( ONIONPRESS_MASTODON_ACCT_ID_OPT, '' );
    if ( $server === '' || $account_id === '' ) {
        return array( 'level' => 'error', 'message' => 'Enter your Mastodon address first.' );
    }

    // --- Daemon mutex (token-based with heartbeat) --------------------
    $now    = time();
    $raw    = (string) get_option( ONIONPRESS_MASTODON_DAEMON_LOCK, '' );
    if ( $raw !== '' && strpos( $raw, ':' ) !== false ) {
        list( $other_tok, $ts_str ) = explode( ':', $raw, 2 );
        $lock_ts = (int) $ts_str;
        if ( $lock_ts > 0 && ( $now - $lock_ts ) < ONIONPRESS_MASTODON_DAEMON_STALE_SEC ) {
            if ( $from_admin ) {
                return array( 'level' => 'warning', 'message' => 'Another sync is already running. Try again in a few minutes.' );
            }
            return; // cron path: silent no-op
        }
    }
    $token = function_exists( 'wp_generate_password' ) ? wp_generate_password( 16, false, false ) : bin2hex( random_bytes( 8 ) );
    update_option( ONIONPRESS_MASTODON_DAEMON_LOCK, $token . ':' . $now, false );

    // Long-running: don't let PHP kill us.
    @set_time_limit( ONIONPRESS_MASTODON_DAEMON_MAX_SEC + 60 );
    @ignore_user_abort( true );

    $loop_deadline = microtime( true ) + ONIONPRESS_MASTODON_DAEMON_MAX_SEC;
    $total = array( 'imported' => 0, 'skipped' => 0, 'errors' => 0, 'pages' => 0 );
    $last_note = '';

    try {
        while ( microtime( true ) < $loop_deadline ) {
            // Lock ownership check — another daemon may have taken over
            // if our heartbeat lapsed. If so, exit gracefully.
            $cur = (string) get_option( ONIONPRESS_MASTODON_DAEMON_LOCK, '' );
            if ( strpos( $cur, $token . ':' ) !== 0 ) {
                break;
            }
            update_option( ONIONPRESS_MASTODON_DAEMON_LOCK, $token . ':' . time(), false );

            // Run one tick (the old tick body, now a helper). Returns the
            // same stats + errors + completion hint.
            $result = onionpress_mastodon_sync_one_tick( $server, $account_id );
            foreach ( array( 'imported', 'skipped', 'errors', 'pages' ) as $k ) {
                $total[ $k ] += (int) ( $result['stats'][ $k ] ?? 0 );
            }
            $last_note = $result['note'];

            if ( $result['done'] || ! empty( $result['errors'] ) ) {
                break; // backfill complete OR tick errored; let next cron fire retry on errors
            }

            // Be polite to the Mastodon server between ticks.
            sleep( ONIONPRESS_MASTODON_DAEMON_IDLE_SEC );
        }
    } finally {
        // Release the lock only if it's still ours.
        $cur = (string) get_option( ONIONPRESS_MASTODON_DAEMON_LOCK, '' );
        if ( strpos( $cur, $token . ':' ) === 0 ) {
            delete_option( ONIONPRESS_MASTODON_DAEMON_LOCK );
        }
    }

    $summary = sprintf(
        '%d imported, %d skipped, %d errors across %d pages (daemon run)',
        $total['imported'], $total['skipped'], $total['errors'], $total['pages']
    );
    update_option( ONIONPRESS_MASTODON_LAST_NOTE, $summary );

    if ( ! $from_admin ) {
        return; // cron path
    }
    $level = ( $total['errors'] > 0 && $total['imported'] === 0 ) ? 'error' : 'success';
    return array( 'level' => $level, 'message' => 'Sync: ' . esc_html( $summary ) );
}

/**
 * One sync tick: catch up forward from newest_id, then walk backward
 * from oldest_id for up to PAGES_PER_TICK pages. Idempotent — posts
 * whose source_id is already present are skipped silently. Takes a
 * transient mutex for the tick itself (separate from the outer daemon
 * lock) so nothing else can interleave inside a single tick.
 *
 * Returns:
 *   array(
 *     'stats'  => array(imported, skipped, errors, pages),
 *     'errors' => array<string>,
 *     'note'   => string summary,
 *     'done'   => bool  // true when oldest_id sentinel reached
 *   )
 */
function onionpress_mastodon_sync_one_tick( $server, $account_id ) {
    $lock = get_transient( ONIONPRESS_MASTODON_LOCK );
    if ( $lock ) {
        return array(
            'stats'  => array( 'imported' => 0, 'skipped' => 0, 'errors' => 0, 'pages' => 0 ),
            'errors' => array( 'tick mutex held' ),
            'note'   => 'tick mutex held',
            'done'   => false,
        );
    }
    set_transient( ONIONPRESS_MASTODON_LOCK, time(), 10 * MINUTE_IN_SECONDS );

    // Cron and CLI contexts inherit shorter time limits; bump to something
    // that won't abort mid-import. The wall-clock deadline below is the
    // real budget — this is just a safety net against php.ini defaults.
    @set_time_limit( ONIONPRESS_MASTODON_TICK_BUDGET_SEC + 30 );
    $deadline = microtime( true ) + ONIONPRESS_MASTODON_TICK_BUDGET_SEC;

    $opts = (array) get_option( ONIONPRESS_MASTODON_OPTS_OPT, array( 'include_replies' => 1 ) );

    $stats = array( 'imported' => 0, 'skipped' => 0, 'errors' => 0, 'pages' => 0 );
    $errors = array();

    try {
        // Forward catch-up: fetch since_id=newest_id until empty.
        $newest_id = onionpress_mastodon_get_newest_for( $account_id );
        if ( $newest_id !== '' ) {
            $rounds = 0;
            while ( $stats['pages'] < ONIONPRESS_MASTODON_PAGES_PER_TICK
                    && microtime( true ) < $deadline
                    && $rounds < 50 ) {
                $rounds++;
                $page = onionpress_mastodon_fetch_statuses( $server, $account_id, array( 'since_id' => $newest_id ) );
                if ( is_wp_error( $page ) ) { $errors[] = $page->get_error_message(); break; }
                $stats['pages']++;
                if ( empty( $page ) ) break;
                // Statuses come newest-first; iterate reverse so newest_id
                // advances monotonically and we don't "lose" posts if the
                // loop is interrupted.
                $reversed = array_reverse( $page );
                foreach ( $reversed as $status ) {
                    if ( microtime( true ) >= $deadline ) break;
                    $r = onionpress_mastodon_import_status( $status, $opts );
                    $stats[ $r ] = ( $stats[ $r ] ?? 0 ) + 1;
                    if ( $r !== 'errors' ) {
                        $newest_id = (string) $status['id'];
                    }
                }
                onionpress_mastodon_set_newest_for( $account_id, $newest_id );
                if ( count( $page ) < ONIONPRESS_MASTODON_PER_PAGE ) break;
            }
        }

        // Backward backfill: walk max_id=oldest_id until an EMPTY page
        // is returned. Do NOT trust "fewer than limit" to mean end of
        // history — Mastodon returns short pages for many reasons
        // (pinned-status filtering, rate-limit trimming, per-instance
        // limits below our requested 40). Only an empty response is a
        // reliable terminator.
        //
        // Cycle guard: if the cursor hasn't advanced after two
        // iterations, the server is looping us — mark done rather than
        // spin forever. Use the raw max_id input vs the max_id after
        // the page; if they're the same we made no progress.
        $oldest_id = onionpress_mastodon_get_oldest_for( $account_id );
        if ( $oldest_id !== 'done'
             && $stats['pages'] < ONIONPRESS_MASTODON_PAGES_PER_TICK
             && microtime( true ) < $deadline ) {
            $no_progress_rounds = 0;
            while ( $stats['pages'] < ONIONPRESS_MASTODON_PAGES_PER_TICK
                    && microtime( true ) < $deadline ) {
                $params = array();
                if ( $oldest_id !== '' ) {
                    $params['max_id'] = $oldest_id;
                }
                $before_id = $oldest_id;
                $page = onionpress_mastodon_fetch_statuses( $server, $account_id, $params );
                if ( is_wp_error( $page ) ) { $errors[] = $page->get_error_message(); break; }
                $stats['pages']++;
                if ( empty( $page ) ) {
                    // Truly walked off the start of history — done.
                    onionpress_mastodon_set_oldest_for( $account_id, 'done' );
                    break;
                }
                foreach ( $page as $status ) {
                    if ( microtime( true ) >= $deadline ) break;
                    $r = onionpress_mastodon_import_status( $status, $opts );
                    $stats[ $r ] = ( $stats[ $r ] ?? 0 ) + 1;
                    $oldest_id = (string) $status['id'];
                    if ( onionpress_mastodon_get_newest_for( $account_id ) === '' ) {
                        onionpress_mastodon_set_newest_for( $account_id, (string) $status['id'] );
                    }
                }
                onionpress_mastodon_set_oldest_for( $account_id, $oldest_id );

                // Cycle guard: cursor didn't move → server is stuck
                // returning the same range. After a few such rounds,
                // give up on this tick so we don't loop forever; the
                // next cron fire retries with fresh state.
                if ( $oldest_id === $before_id ) {
                    $no_progress_rounds++;
                    if ( $no_progress_rounds >= 3 ) {
                        $errors[] = 'cursor stuck at ' . $oldest_id . ' after short-page retries';
                        break;
                    }
                } else {
                    $no_progress_rounds = 0;
                }
                // Previously: count($page) < PER_PAGE → mark done. That
                // was the bug — Mastodon returns short pages for
                // reasons that don't mean "end of history." Keep
                // walking until we see a truly empty page.
            }
        }

        // End-of-tick: convert pending self-reply posts into comments
        // on parents that have since been imported. The backward backfill
        // walks newest→oldest, so a self-reply typically arrives before
        // its parent and is held as a top-level post until this sweep
        // catches up. Capped per tick so a single call can't blow the
        // wall-clock budget — leftovers carry to the next tick.
        $reattached = onionpress_mastodon_reattach_pending();
        if ( $reattached > 0 ) {
            $stats['reattached'] = ( $stats['reattached'] ?? 0 ) + $reattached;
        }
        // End-of-tick: walk a small batch of existing reply posts whose
        // parent isn't here yet, fetch the parent (and its chain) from
        // Mastodon, and re-thread. Each call is one Tor-routed API hit
        // per ancestor — strictly capped so a backlog of hundreds gets
        // chipped at across many ticks instead of blowing one budget.
        if ( microtime( true ) < $deadline ) {
            $context_done = onionpress_mastodon_backfill_context();
            if ( $context_done > 0 ) {
                $stats['context'] = ( $stats['context'] ?? 0 ) + $context_done;
            }
        }
    } finally {
        delete_transient( ONIONPRESS_MASTODON_LOCK );
    }

    update_option( ONIONPRESS_MASTODON_LAST_SYNC, time() );
    $note = sprintf(
        '%d imported, %d skipped, %d errors across %d pages',
        intval( $stats['imported'] ?? 0 ),
        intval( $stats['skipped']  ?? 0 ),
        intval( $stats['errors']   ?? 0 ),
        intval( $stats['pages']    ?? 0 )
    );
    if ( $errors ) { $note .= ' — last error: ' . $errors[ count($errors) - 1 ]; }

    $done = ( onionpress_mastodon_get_oldest_for( $account_id ) === 'done' );
    return array(
        'stats'  => array(
            'imported'   => intval( $stats['imported']   ?? 0 ),
            'skipped'    => intval( $stats['skipped']    ?? 0 ),
            'errors'     => intval( $stats['errors']     ?? 0 ),
            'pages'      => intval( $stats['pages']      ?? 0 ),
            'reattached' => intval( $stats['reattached'] ?? 0 ),
            'context'    => intval( $stats['context']    ?? 0 ),
        ),
        'errors' => $errors,
        'note'   => $note,
        'done'   => $done,
    );
}

/**
 * Fetch one page of the account's statuses. Returns the parsed array
 * of status objects, or WP_Error on transport/HTTP failure. Empty
 * array means "no more data past this cursor."
 */
function onionpress_mastodon_fetch_statuses( $server, $account_id, $params ) {
    $params = array_merge( array(
        'limit'            => ONIONPRESS_MASTODON_PER_PAGE,
        'exclude_reblogs'  => 'false',
        'exclude_replies'  => 'false',
    ), $params );

    // Test hook: lets integration tests inject canned responses without
    // making any network call. Filter receives (null, $params) and may
    // return an array (canned page), a WP_Error, or null to fall
    // through to the real HTTP fetch below. In production nothing hooks
    // this filter, so it's a zero-cost no-op.
    $mock = apply_filters( 'onionpress_mastodon_fetch_statuses_mock', null, $params );
    if ( $mock !== null ) {
        return $mock;
    }

    $url = sprintf(
        'https://%s/api/v1/accounts/%s/statuses?%s',
        $server,
        rawurlencode( $account_id ),
        http_build_query( $params )
    );
    $r = onionpress_mastodon_api_get( $url );
    if ( is_wp_error( $r ) ) return $r;
    if ( (int) $r['code'] !== 200 ) {
        return new WP_Error( 'mastodon_http', 'HTTP ' . $r['code'] . ' from ' . $server );
    }
    return is_array( $r['json'] ) ? $r['json'] : array();
}

/**
 * Tor-over-SOCKS HTTP GET via the onionheaven daemon. Returns
 * ['code'=>int, 'body'=>string, 'json'=>array|null], or WP_Error on
 * transport failure. We can't use wp_remote_get here because it offers
 * no SOCKS option and we must never egress clearnet directly.
 */
function onionpress_mastodon_api_get( $url ) {
    if ( ! function_exists( 'curl_init' ) ) {
        return new WP_Error( 'no_curl', 'curl extension required for Mastodon import' );
    }
    $ch = curl_init( $url );
    curl_setopt_array( $ch, array(
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_FOLLOWLOCATION => true,
        CURLOPT_MAXREDIRS      => 3,
        CURLOPT_PROXY          => ONIONPRESS_MASTODON_SOCKS_PROXY,
        CURLOPT_PROXYTYPE      => CURLPROXY_SOCKS5_HOSTNAME,
        CURLOPT_TIMEOUT        => ONIONPRESS_MASTODON_HTTP_TIMEOUT,
        CURLOPT_CONNECTTIMEOUT => 30,
        CURLOPT_USERAGENT      => 'OnionPress/SocialArchive (+https://onionpress.org)',
        CURLOPT_HTTPHEADER     => array( 'Accept: application/json' ),
    ) );
    $body = curl_exec( $ch );
    if ( $body === false ) {
        $err = curl_error( $ch );
        curl_close( $ch );
        return new WP_Error( 'curl', $err ?: 'curl failure' );
    }
    $code = (int) curl_getinfo( $ch, CURLINFO_HTTP_CODE );
    curl_close( $ch );
    $json = json_decode( $body, true );
    return array( 'code' => $code, 'body' => $body, 'json' => is_array( $json ) ? $json : null );
}

/**
 * Download a file over Tor into a temp path. Used for media sideloads.
 * Returns the local file path or WP_Error.
 */
function onionpress_mastodon_fetch_file( $url, $dest_path ) {
    $fh = @fopen( $dest_path, 'wb' );
    if ( ! $fh ) return new WP_Error( 'io', 'open temp failed' );
    $ch = curl_init( $url );
    curl_setopt_array( $ch, array(
        CURLOPT_FILE           => $fh,
        CURLOPT_FOLLOWLOCATION => true,
        CURLOPT_MAXREDIRS      => 5,
        CURLOPT_PROXY          => ONIONPRESS_MASTODON_SOCKS_PROXY,
        CURLOPT_PROXYTYPE      => CURLPROXY_SOCKS5_HOSTNAME,
        CURLOPT_TIMEOUT        => 120,
        CURLOPT_CONNECTTIMEOUT => 30,
        CURLOPT_USERAGENT      => 'OnionPress/SocialArchive (+https://onionpress.org)',
    ) );
    $ok = curl_exec( $ch );
    $code = (int) curl_getinfo( $ch, CURLINFO_HTTP_CODE );
    $err = curl_error( $ch );
    curl_close( $ch );
    fclose( $fh );
    if ( ! $ok || $code < 200 || $code >= 300 ) {
        @unlink( $dest_path );
        return new WP_Error( 'curl', 'HTTP ' . $code . ' ' . $err );
    }
    return $dest_path;
}

/**
 * Look up a post by its `_source_id` postmeta. Returns post ID or 0.
 * Used to find the parent post when threading a self-reply as a comment.
 */
function onionpress_mastodon_find_post_by_source_id( $source_id ) {
    $posts = get_posts( array(
        'post_type'      => 'post',
        'meta_key'       => '_source_id',
        'meta_value'     => $source_id,
        'post_status'    => 'any',
        'posts_per_page' => 1,
        'fields'         => 'ids',
    ) );
    return ! empty( $posts ) ? (int) $posts[0] : 0;
}

/**
 * Look up a comment by its `_source_id` commentmeta. Returns comment ID or 0.
 * Used both for dedup (re-import same self-reply → no second comment) and
 * for nesting (a self-reply whose parent is itself a self-reply gets its
 * comment_parent set to the parent comment).
 */
function onionpress_mastodon_find_comment_by_source_id( $source_id ) {
    $ids = get_comments( array(
        'meta_key'   => '_source_id',
        'meta_value' => $source_id,
        'number'     => 1,
        'fields'     => 'ids',
    ) );
    return ! empty( $ids ) ? (int) $ids[0] : 0;
}

/**
 * Find the post a self-reply should attach to, given the source_id of
 * the toot it replies to. The parent might be:
 *   - a top-level post (the typical case: thread root) → return its ID, or
 *   - a comment that we converted earlier in the same thread → walk up
 *     to the comment's post and return that.
 * Returns 0 when neither exists yet (parent not yet imported).
 */
function onionpress_mastodon_find_attachable_post( $parent_source_id ) {
    $pid = onionpress_mastodon_find_post_by_source_id( $parent_source_id );
    if ( $pid ) return $pid;
    $cid = onionpress_mastodon_find_comment_by_source_id( $parent_source_id );
    if ( $cid ) {
        $c = get_comment( $cid );
        if ( $c ) return (int) $c->comment_post_ID;
    }
    return 0;
}

/**
 * Fetch a single status by ID from the configured Mastodon server.
 * Returns the parsed status array, or null on failure (404, network
 * error, malformed response). Cheap test mock via the
 * `onionpress_mastodon_fetch_status_mock` filter — same shape as the
 * fetch_statuses mock.
 */
function onionpress_mastodon_fetch_status_by_id( $server, $status_id ) {
    $mock = apply_filters( 'onionpress_mastodon_fetch_status_mock', null, $status_id );
    if ( $mock !== null ) {
        return is_array( $mock ) ? $mock : null;
    }
    if ( $server === '' || $status_id === '' ) return null;
    $r = onionpress_mastodon_api_get(
        'https://' . $server . '/api/v1/statuses/' . rawurlencode( $status_id )
    );
    if ( is_wp_error( $r ) ) return null;
    if ( (int) $r['code'] !== 200 ) return null;
    return is_array( $r['json'] ) ? $r['json'] : null;
}

/**
 * Ensure a toot is in our DB, fetching it (and recursively, its parent
 * chain) from Mastodon if necessary. Returns the post ID we should
 * attach a child reply to (post itself, or the post a comment-form
 * version of this toot was attached to), or 0 on failure.
 *
 * Walks at most CONTEXT_DEPTH_MAX hops up. Each hop is one Tor-routed
 * API call, so the cap matters for budget. A return of 0 means: parent
 * couldn't be imported (404, depth exhausted, or transport failure) —
 * the caller should fall back to importing as a top-level post with
 * _pending_reattach=1 in case the parent shows up later.
 */
function onionpress_mastodon_ensure_imported_with_ancestry( $status_id, $opts, $depth = 0 ) {
    if ( $depth > ONIONPRESS_MASTODON_CONTEXT_DEPTH_MAX ) return 0;
    $source_id = 'mastodon:' . $status_id;
    $existing  = onionpress_mastodon_find_attachable_post( $source_id );
    if ( $existing ) return $existing;
    $server = (string) get_option( ONIONPRESS_MASTODON_SERVER_OPT, '' );
    $status = onionpress_mastodon_fetch_status_by_id( $server, $status_id );
    if ( ! is_array( $status ) ) return 0;
    // Recurse to ensure ancestor chain is in place BEFORE this one is
    // imported — that way when import_status processes this status it
    // sees the parent and threads correctly in one pass.
    if ( ! empty( $status['in_reply_to_id'] ) ) {
        onionpress_mastodon_ensure_imported_with_ancestry(
            (string) $status['in_reply_to_id'], $opts, $depth + 1
        );
    }
    $our_id = (string) get_option( ONIONPRESS_MASTODON_ACCT_ID_OPT, '' );
    $is_ours = ( (string) ( $status['account']['id'] ?? '' ) === $our_id );
    onionpress_mastodon_import_status( $status, $opts, ! $is_ours );
    return onionpress_mastodon_find_attachable_post( $source_id );
}

/**
 * Import a self-reply toot as a WP comment on the parent post. Idempotent
 * by `_source_id` commentmeta. If the toot it replies to was itself a
 * self-reply (already imported as a comment), the new comment is nested
 * under that one via comment_parent so threading survives.
 */
function onionpress_mastodon_create_self_reply_comment( $status, $parent_post_id ) {
    $status_id = isset( $status['id'] ) ? (string) $status['id'] : '';
    if ( $status_id === '' ) return 'errors';
    $source_id = 'mastodon:' . $status_id;

    if ( onionpress_mastodon_find_comment_by_source_id( $source_id ) ) {
        return 'skipped';
    }

    $ts = strtotime( $status['created_at'] ?? '' );
    if ( ! $ts ) return 'errors';

    list( $content_html, ) = onionpress_mastodon_render_content( $status );

    $author = (string) ( $status['account']['display_name']
                         ?? $status['account']['username']
                         ?? 'me' );
    $author_url = (string) ( $status['url'] ?? '' );

    // Nest under the parent comment if the toot we're replying to was
    // also a self-reply already converted. Top-level if the parent is the
    // post itself (root of the thread).
    $comment_parent = 0;
    $reply_to_id = (string) ( $status['in_reply_to_id'] ?? '' );
    if ( $reply_to_id !== '' ) {
        $comment_parent = onionpress_mastodon_find_comment_by_source_id( 'mastodon:' . $reply_to_id );
    }

    $gmt = gmdate( 'Y-m-d H:i:s', $ts );
    $comment_id = wp_insert_comment( array(
        'comment_post_ID'    => $parent_post_id,
        'comment_author'     => $author,
        'comment_author_url' => $author_url,
        'comment_content'    => $content_html,
        'comment_date'       => get_date_from_gmt( $gmt ),
        'comment_date_gmt'   => $gmt,
        'comment_approved'   => 1,
        'comment_parent'     => $comment_parent,
        'comment_type'       => 'comment',
        'comment_meta'       => array(
            '_source_id'  => $source_id,
            '_source_url' => $author_url,
            '_raw'        => wp_json_encode( $status ),
        ),
    ) );

    return $comment_id ? 'imported' : 'errors';
}

/**
 * Convert any top-level posts marked `_pending_reattach=1` whose parent
 * has since been imported into comments on that parent. Run at the end
 * of each sync tick so within a single backward backfill walk, posts
 * created earlier in the tick get folded into newly-arrived parents.
 *
 * Capped per call so a tick that crosses many threads can't blow its
 * wall-clock budget here — leftovers get picked up the next tick.
 *
 * Returns the number of posts converted.
 */
function onionpress_mastodon_reattach_pending( $limit = 200 ) {
    // Process oldest pending first so that, in a chain A→B→C all imported
    // pending in reverse order, B converts before C — at which point C's
    // comment-aware lookup can resolve B (now a comment) and chain in.
    $pending = get_posts( array(
        'post_type'      => 'post',
        'meta_key'       => '_pending_reattach',
        'meta_value'     => '1',
        'post_status'    => 'publish',
        'posts_per_page' => (int) $limit,
        'orderby'        => 'date',
        'order'          => 'ASC',
        'fields'         => 'ids',
    ) );
    $converted = 0;
    foreach ( $pending as $pid ) {
        $reply_to = (string) get_post_meta( $pid, '_reply_to_id', true );
        if ( $reply_to === '' ) {
            // Marked pending without a reply target — clear the flag so
            // we don't re-scan this post forever.
            delete_post_meta( $pid, '_pending_reattach' );
            continue;
        }
        $parent_pid = onionpress_mastodon_find_attachable_post( 'mastodon:' . $reply_to );
        if ( ! $parent_pid ) {
            continue; // parent still hasn't been imported; try next tick
        }
        $raw = (string) get_post_meta( $pid, '_raw', true );
        $status = json_decode( $raw, true );
        if ( ! is_array( $status ) ) {
            // No raw payload to reconstruct the comment from — drop the
            // flag and leave the post as-is rather than losing data.
            delete_post_meta( $pid, '_pending_reattach' );
            continue;
        }
        $r = onionpress_mastodon_create_self_reply_comment( $status, $parent_pid );
        if ( $r === 'imported' || $r === 'skipped' ) {
            wp_delete_post( $pid, true );
            $converted++;
        }
    }
    return $converted;
}

/**
 * Convert an existing top-level post into a comment on $parent_post_id.
 * Reads the post's own fields + postmeta (NOT the `_raw` Mastodon JSON,
 * which on older installs was stored with broken escaping and can't be
 * decoded). Idempotent on `_source_id` commentmeta. On success the
 * placeholder post is force-deleted.
 *
 * Returns true on conversion (or skipped-as-already-converted), false
 * on failure to insert.
 */
function onionpress_mastodon_convert_post_to_comment( $post_id, $parent_post_id ) {
    $post = get_post( $post_id );
    if ( ! $post ) return false;
    $source_id = (string) get_post_meta( $post_id, '_source_id', true );
    if ( $source_id === '' ) return false;

    if ( onionpress_mastodon_find_comment_by_source_id( $source_id ) ) {
        // Already converted in a prior run — drop the placeholder.
        wp_delete_post( $post_id, true );
        return true;
    }

    // Nest under the parent comment if the toot we're replying to was
    // also a self-reply already converted in this same migration pass.
    $reply_to_id = (string) get_post_meta( $post_id, '_reply_to_id', true );
    $comment_parent = 0;
    if ( $reply_to_id !== '' ) {
        $comment_parent = onionpress_mastodon_find_comment_by_source_id( 'mastodon:' . $reply_to_id );
    }

    $author = (string) get_option( ONIONPRESS_MASTODON_USER_OPT, '' );
    if ( $author === '' ) $author = 'me';
    $author_url = (string) get_post_meta( $post_id, '_source_url', true );

    $gmt = $post->post_date_gmt ?: gmdate( 'Y-m-d H:i:s' );
    $comment_id = wp_insert_comment( array(
        'comment_post_ID'    => $parent_post_id,
        'comment_author'     => $author,
        'comment_author_url' => $author_url,
        'comment_content'    => (string) $post->post_content,
        'comment_date'       => get_date_from_gmt( $gmt ),
        'comment_date_gmt'   => $gmt,
        'comment_approved'   => 1,
        'comment_parent'     => $comment_parent,
        'comment_type'       => 'comment',
        'comment_meta'       => array(
            '_source_id'  => $source_id,
            '_source_url' => $author_url,
        ),
    ) );
    if ( ! $comment_id ) return false;

    wp_delete_post( $post_id, true );
    return true;
}

/**
 * Find existing reply posts whose parent toot isn't in our DB, fetch
 * the parent (and chain) from Mastodon, and re-thread the reply as a
 * comment on it. Capped per call so a single tick can't blow its
 * Tor-call budget — leftovers carry over to the next tick.
 *
 * Returns the number of replies successfully threaded.
 */
function onionpress_mastodon_backfill_context( $limit = null ) {
    if ( $limit === null ) $limit = ONIONPRESS_MASTODON_CONTEXT_BATCH_PER_TICK;
    global $wpdb;
    $candidates = $wpdb->get_results( $wpdb->prepare(
        "SELECT p.ID, m2.meta_value AS reply_to_id
         FROM {$wpdb->posts} p
         JOIN {$wpdb->postmeta} m1 ON m1.post_id=p.ID AND m1.meta_key='_source_id'
         JOIN {$wpdb->postmeta} m2 ON m2.post_id=p.ID AND m2.meta_key='_reply_to_id'
         JOIN {$wpdb->postmeta} m4 ON m4.post_id=p.ID AND m4.meta_key='_is_reply' AND m4.meta_value='1'
         WHERE m1.meta_value LIKE 'mastodon:%' AND m2.meta_value <> ''
           AND p.post_status='publish'
           AND NOT EXISTS (
             SELECT 1 FROM {$wpdb->postmeta} pm
             WHERE pm.meta_key='_source_id'
               AND pm.meta_value = CONCAT('mastodon:', m2.meta_value)
           )
           AND NOT EXISTS (
             SELECT 1 FROM {$wpdb->commentmeta} cm
             WHERE cm.meta_key='_source_id'
               AND cm.meta_value = CONCAT('mastodon:', m2.meta_value)
           )
         ORDER BY p.post_date DESC
         LIMIT %d",
        (int) $limit
    ) );
    if ( empty( $candidates ) ) return 0;
    $opts = (array) get_option( ONIONPRESS_MASTODON_OPTS_OPT, array() );
    $threaded = 0;
    foreach ( $candidates as $c ) {
        $parent_pid = onionpress_mastodon_ensure_imported_with_ancestry(
            (string) $c->reply_to_id, $opts, 1
        );
        if ( ! $parent_pid ) continue;
        if ( onionpress_mastodon_convert_post_to_comment( (int) $c->ID, $parent_pid ) ) {
            $threaded++;
        }
    }
    return $threaded;
}

/**
 * One-time migration for installs that imported self-reply toots as
 * top-level posts before threading was wired up. Finds existing posts
 * with `_is_reply=1` whose `_reply_to_id` matches a known `_source_id`
 * of another mastodon post, converts them to comments on that parent,
 * and deletes the now-redundant top-level post.
 *
 * Gated by an option flag so it only runs once per install. Safe to
 * call repeatedly — the flag check short-circuits.
 *
 * Returns the number of posts converted.
 */
function onionpress_mastodon_migrate_self_replies_to_comments() {
    if ( get_option( ONIONPRESS_MASTODON_THREADS_MIGRATED_OPT ) === 'yes' ) {
        return 0;
    }
    $our_id = (string) get_option( ONIONPRESS_MASTODON_ACCT_ID_OPT, '' );
    if ( $our_id === '' ) {
        // Can't classify self-replies without our own account id.
        // Don't set the flag — try again later when the account is set.
        return 0;
    }
    global $wpdb;
    $candidates = $wpdb->get_results(
        "SELECT p.ID, m1.meta_value AS source_id, m2.meta_value AS reply_to_id, m3.meta_value AS raw
         FROM {$wpdb->posts} p
         JOIN {$wpdb->postmeta} m1 ON m1.post_id=p.ID AND m1.meta_key='_source_id'
         JOIN {$wpdb->postmeta} m2 ON m2.post_id=p.ID AND m2.meta_key='_reply_to_id'
         LEFT JOIN {$wpdb->postmeta} m3 ON m3.post_id=p.ID AND m3.meta_key='_raw'
         JOIN {$wpdb->postmeta} m4 ON m4.post_id=p.ID AND m4.meta_key='_is_reply' AND m4.meta_value='1'
         WHERE m1.meta_value LIKE 'mastodon:%' AND m2.meta_value <> ''
         ORDER BY p.post_date ASC"
    );
    $converted = 0;
    foreach ( $candidates as $c ) {
        // Self-reply confirmation: prefer an explicit account_id from
        // _raw if it decodes (newer imports). Older imports stored _raw
        // with broken HTML escaping inside the content field — those
        // fail json_decode and we fall back to "parent is in our DB"
        // as proof of self-reply, since the importer only ever pulls
        // our own account's statuses.
        $status = $c->raw ? json_decode( $c->raw, true ) : null;
        if ( is_array( $status ) ) {
            $reply_to_acct = (string) ( $status['in_reply_to_account_id'] ?? '' );
            if ( $reply_to_acct !== '' && $reply_to_acct !== $our_id ) {
                continue; // explicit evidence: not a self-reply
            }
        }
        // Comment-aware lookup so chains migrate in one pass: an earlier
        // iteration may have converted this candidate's parent into a
        // comment, in which case we attach to the comment's post.
        $parent_pid = onionpress_mastodon_find_attachable_post( 'mastodon:' . $c->reply_to_id );
        if ( ! $parent_pid ) continue;
        if ( onionpress_mastodon_convert_post_to_comment( (int) $c->ID, $parent_pid ) ) {
            $converted++;
        }
    }
    update_option( ONIONPRESS_MASTODON_THREADS_MIGRATED_OPT, 'yes' );
    return $converted;
}

/**
 * Import one Mastodon status object. Returns 'imported' | 'skipped' |
 * 'errors' for the caller's tally.
 *
 * Mastodon's `reblog` field holds the boosted status; when it's set,
 * the outer status is a boost shell with no original content. Replies
 * are detected by `in_reply_to_id` and are folded into the parent
 * post's comment thread — for self-replies the parent is from our own
 * timeline, for replies-to-others the parent is fetched from Mastodon
 * via ensure_imported_with_ancestry() and stored as context (marked
 * _is_context=1 if it's not our own toot). When a parent fetch fails
 * (404, depth cap, transport error) the reply lands as a top-level
 * post with _pending_reattach=1 so the end-of-tick sweep can convert
 * it later if the parent shows up.
 *
 * $as_context=true marks the imported post (top-level only — comments
 * are inherently attributed to their author) with `_is_context=1` so
 * the theme can render it with a "@author said:" wrapper. Set by
 * ensure_imported_with_ancestry when fetching foreign ancestors.
 */
function onionpress_mastodon_import_status( $status, $opts, $as_context = false ) {
    $status_id = isset( $status['id'] ) ? (string) $status['id'] : '';
    if ( $status_id === '' ) return 'errors';

    $source_id = 'mastodon:' . $status_id;
    $existing  = get_posts( array(
        'post_type'      => 'post',
        'meta_key'       => '_source_id',
        'meta_value'     => $source_id,
        'post_status'    => 'any',
        'posts_per_page' => 1,
        'fields'         => 'ids',
    ) );
    if ( ! empty( $existing ) ) return 'skipped';

    $is_boost = ! empty( $status['reblog'] );
    $in_reply = ! empty( $status['in_reply_to_id'] );
    $our_id   = (string) get_option( ONIONPRESS_MASTODON_ACCT_ID_OPT, '' );
    $reply_to_acct = isset( $status['in_reply_to_account_id'] ) ? (string) $status['in_reply_to_account_id'] : '';
    $is_self_reply = $in_reply && $our_id !== '' && $reply_to_acct === $our_id;

    if ( $is_boost && empty( $opts['include_boosts'] ) ) {
        return 'skipped';
    }
    if ( $in_reply && ! $is_self_reply && empty( $opts['include_replies'] ) ) {
        return 'skipped';
    }

    // Any reply (self OR to someone else) → fold into the parent's
    // comment thread. For self-replies the parent comes from our own
    // timeline; for replies-to-others we fetch the parent (and its
    // ancestor chain) from Mastodon so the conversation reads in
    // context. If parent fetch fails (404, depth cap, transport),
    // fall through with _pending_reattach=1 — the end-of-tick sweep
    // converts later if the parent eventually arrives.
    $pending_reattach = false;
    if ( $in_reply ) {
        $reply_to_source = 'mastodon:' . (string) $status['in_reply_to_id'];
        $parent_pid = onionpress_mastodon_find_attachable_post( $reply_to_source );
        if ( ! $parent_pid ) {
            // Recurse one hop into ensure_imported_with_ancestry — start
            // at depth 1 so total chain (this status + ancestors) stays
            // within CONTEXT_DEPTH_MAX.
            $parent_pid = onionpress_mastodon_ensure_imported_with_ancestry(
                (string) $status['in_reply_to_id'], $opts, 1
            );
        }
        if ( $parent_pid ) {
            return onionpress_mastodon_create_self_reply_comment( $status, $parent_pid );
        }
        $pending_reattach = true;
    }

    $ts = strtotime( $status['created_at'] ?? '' );
    if ( ! $ts ) return 'errors';

    list( $content_html, $preview_text ) = onionpress_mastodon_render_content( $status );
    $title = wp_trim_words( $preview_text, 10, '…' );
    if ( $title === '' ) $title = gmdate( 'Y-m-d', $ts ) . ' toot';

    $source_url = $status['url'] ?? '';
    // Boost shells have `url` pointing at the /activity endpoint; prefer
    // the boosted status's permalink so "View on mastodon.…" leads
    // somewhere useful.
    if ( $is_boost && ! empty( $status['reblog']['url'] ) ) {
        $source_url = $status['reblog']['url'];
    }

    $post_date_gmt = gmdate( 'Y-m-d H:i:s', $ts );
    $post_id = wp_insert_post( array(
        'post_type'     => 'post',
        'post_status'   => 'publish',
        'post_title'    => $title,
        'post_content'  => $content_html,
        'post_excerpt'  => wp_trim_words( $preview_text, 35, '…' ),
        'post_date_gmt' => $post_date_gmt,
        'post_date'     => get_date_from_gmt( $post_date_gmt ),
        'meta_input'    => array(
            '_source_id'        => $source_id,
            '_source_url'       => $source_url,
            '_is_repost'        => $is_boost  ? '1' : '0',
            '_is_reply'         => $in_reply  ? '1' : '0',
            '_reply_to_id'      => $in_reply  ? (string) $status['in_reply_to_id'] : '',
            '_thread_root_id'   => $is_self_reply ? '' : $status_id,
            '_pending_reattach' => $pending_reattach ? '1' : '0',
            '_is_context'       => $as_context ? '1' : '0',
            '_raw'              => wp_json_encode( $status ),
        ),
    ), true );

    if ( is_wp_error( $post_id ) ) return 'errors';

    if ( function_exists( 'onionpress_social_ensure_category' ) ) {
        $cat_id = onionpress_social_ensure_category( 'mastodon' );
        if ( $cat_id ) {
            wp_set_post_categories( $post_id, array( $cat_id ), false );
        }
    }

    // Tags: Mastodon posts carry explicit hashtag entities.
    $tag_names = array();
    foreach ( ( $status['tags'] ?? array() ) as $t ) {
        if ( ! empty( $t['name'] ) ) $tag_names[] = $t['name'];
    }
    if ( $tag_names ) {
        wp_set_post_tags( $post_id, $tag_names, false );
    }

    // Media: sideload each attachment from the source URL.
    $media = $is_boost
        ? ( $status['reblog']['media_attachments'] ?? array() )
        : ( $status['media_attachments'] ?? array() );
    if ( $media ) {
        onionpress_mastodon_sideload_media( $post_id, $status_id, $media );
    }

    return 'imported';
}

/**
 * Turn a status into ( $content_html, $preview_text ).
 *
 * Mastodon already returns sanitized HTML in `content`. We keep it as
 * is (wp_kses_post on display, no double-escape) and build the
 * preview by stripping tags. For boost shells, the outer content is
 * empty; we render a small "boosted by" wrapper above the boosted
 * post's content so the card conveys what's going on.
 */
function onionpress_mastodon_render_content( $status ) {
    if ( ! empty( $status['reblog'] ) ) {
        $inner = (string) ( $status['reblog']['content'] ?? '' );
        $boosted_acct = (string) ( $status['reblog']['account']['acct'] ?? '' );
        $boosted_url  = (string) ( $status['reblog']['url']             ?? '' );
        $header = $boosted_acct
            ? sprintf(
                '<p><em>Boosted <a href="%s" rel="nofollow noopener">@%s</a>:</em></p>',
                esc_url( $boosted_url ),
                esc_html( $boosted_acct )
            )
            : '<p><em>Boosted:</em></p>';
        $html = $header . $inner;
    } else {
        $html = (string) ( $status['content'] ?? '' );
    }
    // The Mastodon `content` field wraps paragraphs in <p>; that's fine
    // for direct insertion as post_content (theme + wpautop won't re-wrap).
    $preview = trim( html_entity_decode( wp_strip_all_tags( $html ), ENT_QUOTES, 'UTF-8' ) );
    return array( $html, $preview );
}

/**
 * Sideload media_attachments into uploads. Downloads each over Tor into
 * /tmp, then uses WP's media helpers to register + attach. Appends
 * <img>/<video> tags to the post content so the card renders them.
 * First image becomes the featured image so archive listings have a
 * thumbnail.
 */
function onionpress_mastodon_sideload_media( $post_id, $status_id, $media ) {
    require_once ABSPATH . 'wp-admin/includes/image.php';
    require_once ABSPATH . 'wp-admin/includes/file.php';
    require_once ABSPATH . 'wp-admin/includes/media.php';

    $upload = wp_upload_dir();
    $tags   = array();
    foreach ( $media as $m ) {
        $url  = (string) ( $m['url'] ?? '' );
        $type = (string) ( $m['type'] ?? '' );
        if ( $url === '' ) continue;

        $ext = strtolower( pathinfo( parse_url( $url, PHP_URL_PATH ) ?? '', PATHINFO_EXTENSION ) );
        // The server picks the URL, and the file goes straight into uploads
        // without wp_handle_sideload()'s checks: keep only an extension
        // WordPress accepts for media, never .php and the like.
        if ( ! preg_match( '/^[a-z0-9]{1,5}$/D', $ext ) || ! wp_check_filetype( 'x.' . $ext )['ext'] ) {
            // Guess an extension from MIME type.
            $ext = ( $type === 'video' || $type === 'gifv' ) ? 'mp4'
                 : ( $type === 'audio' ? 'mp3' : 'jpg' );
        }
        $basename  = $status_id . '-' . wp_generate_password( 6, false ) . '.' . $ext;
        $tmp_path  = sys_get_temp_dir() . '/' . $basename;
        $fetched   = onionpress_mastodon_fetch_file( $url, $tmp_path );
        if ( is_wp_error( $fetched ) ) continue;

        $dest_name = wp_unique_filename( $upload['path'], $basename );
        $dest_path = $upload['path'] . '/' . $dest_name;
        if ( ! @rename( $tmp_path, $dest_path ) && ! @copy( $tmp_path, $dest_path ) ) {
            @unlink( $tmp_path );
            continue;
        }
        @unlink( $tmp_path );

        $filetype = wp_check_filetype( $dest_name );
        $attach_id = wp_insert_attachment( array(
            'post_mime_type' => $filetype['type'] ?? 'application/octet-stream',
            'post_title'     => sanitize_title( pathinfo( $dest_name, PATHINFO_FILENAME ) ),
            'post_content'   => '',
            'post_status'    => 'inherit',
        ), $dest_path, $post_id );
        if ( is_wp_error( $attach_id ) ) continue;

        $meta = wp_generate_attachment_metadata( $attach_id, $dest_path );
        wp_update_attachment_metadata( $attach_id, $meta );

        $mime = $filetype['type'] ?? '';
        if ( empty( $tags ) && strpos( $mime, 'image/' ) === 0 ) {
            set_post_thumbnail( $post_id, $attach_id );
        }

        if ( strpos( $mime, 'image/' ) === 0 ) {
            $img = wp_get_attachment_image(
                $attach_id, 'large', false,
                array( 'loading' => 'lazy', 'style' => 'max-width:100%;height:auto;' )
            );
            $tags[] = preg_replace( '~https?://[^/\s"\']+/~', '/', $img );
        } elseif ( strpos( $mime, 'video/' ) === 0 || $type === 'gifv' ) {
            $url_rel = wp_make_link_relative( wp_get_attachment_url( $attach_id ) );
            $loop = ( $type === 'gifv' ) ? ' autoplay loop muted playsinline' : '';
            $tags[] = sprintf(
                '<video controls preload="metadata" style="max-width:100%%;"%s><source src="%s" type="%s"></video>',
                $loop, esc_url( $url_rel ), esc_attr( $mime )
            );
        } elseif ( strpos( $mime, 'audio/' ) === 0 ) {
            $url_rel = wp_make_link_relative( wp_get_attachment_url( $attach_id ) );
            $tags[] = sprintf(
                '<audio controls preload="metadata" style="max-width:100%%;"><source src="%s" type="%s"></audio>',
                esc_url( $url_rel ), esc_attr( $mime )
            );
        }
    }

    if ( $tags ) {
        $post = get_post( $post_id );
        wp_update_post( array(
            'ID'           => $post_id,
            'post_content' => $post->post_content . "\n\n" . implode( "\n\n", $tags ),
        ) );
    }
}

function onionpress_mastodon_render_recent() {
    $recent = get_posts( array(
        'post_type'      => 'post',
        'posts_per_page' => 10,
        'orderby'        => 'date',
        'order'          => 'DESC',
        'category_name'  => 'mastodon',
    ) );
    if ( empty( $recent ) ) {
        echo '<p><em>No Mastodon posts imported yet.</em></p>';
        return;
    }
    echo '<ul>';
    foreach ( $recent as $p ) {
        printf(
            '<li><a href="%s">%s</a> &middot; <small>%s</small></li>',
            esc_url( get_permalink( $p->ID ) ),
            esc_html( get_the_title( $p ) ),
            esc_html( get_the_date( '', $p ) )
        );
    }
    echo '</ul>';
}

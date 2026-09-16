<?php
/**
 * Plugin Name: OnionPress Social Archive — Bluesky Importer
 * Description: Pull a Bluesky account's public posts into the unified
 *              social_post archive via the AT Protocol public API
 *              (app.bsky.feed.getAuthorFeed, unauthenticated). One
 *              handle, one click: backfills full history via opaque
 *              cursors, then polls every few minutes to stay current.
 *              Idempotent — posts are keyed by their AT-URI.
 * Version:     0.1
 * Network:     true
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

add_action( 'plugins_loaded', function () {
    if ( function_exists( 'onionpress_social_register_importer' ) ) {
        onionpress_social_register_importer( 'bluesky' );
    }
} );

const ONIONPRESS_BLUESKY_ADMIN_SLUG   = 'onionpress-social-archive-bluesky';
const ONIONPRESS_BLUESKY_HANDLE_OPT   = 'onionpress_social_bluesky_handle';      // e.g. "brewster.archive.org"
const ONIONPRESS_BLUESKY_DID_OPT      = 'onionpress_social_bluesky_did';         // "did:plc:..." authoritative actor
const ONIONPRESS_BLUESKY_DISPLAY_OPT  = 'onionpress_social_bluesky_display_name';
const ONIONPRESS_BLUESKY_NEWEST_OPT   = 'onionpress_social_bluesky_newest_uri';  // top marker for incremental
const ONIONPRESS_BLUESKY_CURSOR_OPT   = 'onionpress_social_bluesky_backfill_cursor'; // opaque; 'done' when finished
// Owner markers for the above. Each cursor/newest value is only valid
// if its owner matches the current DID — if the DID has been swapped
// out from under us (e.g. a test harness poking production options, or
// a user switching accounts without going through save_handle) the
// cursor is implicitly invalidated and we re-walk. See also
// onionpress_bluesky_get_cursor_for() / _set_cursor_for().
const ONIONPRESS_BLUESKY_CURSOR_OWNER_OPT = 'onionpress_social_bluesky_cursor_owner';
const ONIONPRESS_BLUESKY_NEWEST_OWNER_OPT = 'onionpress_social_bluesky_newest_owner';
const ONIONPRESS_BLUESKY_POSTS_OPT    = 'onionpress_social_bluesky_total_posts';
const ONIONPRESS_BLUESKY_LAST_SYNC    = 'onionpress_social_bluesky_last_sync';
const ONIONPRESS_BLUESKY_LAST_NOTE    = 'onionpress_social_bluesky_last_note';
const ONIONPRESS_BLUESKY_LOCK         = 'onionpress_social_bluesky_lock';
const ONIONPRESS_BLUESKY_OPTS_OPT     = 'onionpress_social_bluesky_opts';
const ONIONPRESS_BLUESKY_CRON_HOOK    = 'onionpress_social_bluesky_sync';

// Route everything through the bulk-outgoing Tor daemon per the
// "separate Tor in/out" rule. onionpress-tor stays focused on the
// hosted onion service.
const ONIONPRESS_BLUESKY_SOCKS_PROXY  = 'onionheaven:9050';

// Public AppView — unauthenticated reads. No instance to configure,
// unlike Mastodon.
const ONIONPRESS_BLUESKY_API_HOST     = 'public.api.bsky.app';

// Budget per tick.
const ONIONPRESS_BLUESKY_TICK_BUDGET_SEC = 25;
const ONIONPRESS_BLUESKY_PAGES_PER_TICK  = 8;
const ONIONPRESS_BLUESKY_PER_PAGE        = 100;
const ONIONPRESS_BLUESKY_HTTP_TIMEOUT    = 45;

// Daemon-style outer loop with token-lock heartbeat, same shape as the
// Mastodon importer.
const ONIONPRESS_BLUESKY_DAEMON_LOCK      = 'onionpress_social_bluesky_daemon_lock';
const ONIONPRESS_BLUESKY_DAEMON_MAX_SEC   = 1800; // 30 min
const ONIONPRESS_BLUESKY_DAEMON_STALE_SEC = 300;
const ONIONPRESS_BLUESKY_DAEMON_IDLE_SEC  = 3;

// Threading: replies are imported as WP comments on the parent's post.
// Backfill walks newest→oldest (cursor-based), so a reply usually
// arrives before its parent — those land top-level with
// _pending_reattach=1 and an end-of-tick sweep converts them.
// Replies to other accounts trigger a context fetch (the parent +
// ancestor chain are pulled from the AT Protocol API and imported,
// foreign ancestors marked _is_context=1) so the conversation reads
// in full instead of a fragmentary reply.
const ONIONPRESS_BLUESKY_THREADS_MIGRATED_OPT  = 'onionpress_bluesky_threads_v1_migrated';
const ONIONPRESS_BLUESKY_CONTEXT_DEPTH_MAX     = 6;
const ONIONPRESS_BLUESKY_CONTEXT_BATCH_PER_TICK = 10;

/**
 * Owner-aware accessors for the backfill cursor and the newest-uri top
 * marker. Each value carries a DID marker noting whose feed it was
 * derived from. When the current DID doesn't match the stored owner,
 * the value is treated as empty, forcing a fresh walk rather than
 * silently continuing against the wrong account.
 *
 * This is a structural guard against an entire class of bugs: tests,
 * manual wp-cli pokes, or buggy save_handle paths that swap the DID
 * in production options without also clearing the cursor. The cursor
 * isn't "sticky across DIDs" anymore — it's implicitly invalid as
 * soon as the DID it belongs to changes.
 */
function onionpress_bluesky_get_cursor_for( $did ) {
    $owner = (string) get_option( ONIONPRESS_BLUESKY_CURSOR_OWNER_OPT, '' );
    if ( $owner !== '' && $owner !== $did ) {
        return '';
    }
    return (string) get_option( ONIONPRESS_BLUESKY_CURSOR_OPT, '' );
}
function onionpress_bluesky_set_cursor_for( $did, $value ) {
    update_option( ONIONPRESS_BLUESKY_CURSOR_OPT, $value );
    update_option( ONIONPRESS_BLUESKY_CURSOR_OWNER_OPT, $did );
}
function onionpress_bluesky_get_newest_for( $did ) {
    $owner = (string) get_option( ONIONPRESS_BLUESKY_NEWEST_OWNER_OPT, '' );
    if ( $owner !== '' && $owner !== $did ) {
        return '';
    }
    return (string) get_option( ONIONPRESS_BLUESKY_NEWEST_OPT, '' );
}
function onionpress_bluesky_set_newest_for( $did, $uri ) {
    update_option( ONIONPRESS_BLUESKY_NEWEST_OPT, $uri );
    update_option( ONIONPRESS_BLUESKY_NEWEST_OWNER_OPT, $did );
}
function onionpress_bluesky_clear_cursors() {
    delete_option( ONIONPRESS_BLUESKY_CURSOR_OPT );
    delete_option( ONIONPRESS_BLUESKY_CURSOR_OWNER_OPT );
    delete_option( ONIONPRESS_BLUESKY_NEWEST_OPT );
    delete_option( ONIONPRESS_BLUESKY_NEWEST_OWNER_OPT );
}

add_action( 'admin_menu', function () {
    if ( ! defined( 'ONIONPRESS_SOCIAL_ADMIN_SLUG' ) ) {
        return;
    }
    add_submenu_page(
        ONIONPRESS_SOCIAL_ADMIN_SLUG,
        'Import Bluesky',
        'Bluesky',
        'manage_options',
        ONIONPRESS_BLUESKY_ADMIN_SLUG,
        'onionpress_bluesky_import_page'
    );
}, 20 );

add_action( 'admin_init', function () {
    if ( ! wp_next_scheduled( ONIONPRESS_BLUESKY_CRON_HOOK )
         && get_option( ONIONPRESS_BLUESKY_DID_OPT ) ) {
        wp_schedule_event( time() + 60, 'onionpress_bluesky_5min', ONIONPRESS_BLUESKY_CRON_HOOK );
    }
    // One-shot migration of pre-threading reply posts into comments.
    // Self-gates on its own option flag, so this is a no-op once done.
    if ( get_option( ONIONPRESS_BLUESKY_THREADS_MIGRATED_OPT ) !== 'yes'
         && get_option( ONIONPRESS_BLUESKY_DID_OPT ) ) {
        onionpress_bluesky_migrate_replies_to_comments();
    }
} );

add_filter( 'cron_schedules', function ( $schedules ) {
    if ( ! isset( $schedules['onionpress_bluesky_5min'] ) ) {
        $schedules['onionpress_bluesky_5min'] = array(
            'interval' => 300,
            'display'  => '5 min (OnionPress Bluesky poll)',
        );
    }
    return $schedules;
} );

add_action( ONIONPRESS_BLUESKY_CRON_HOOK, 'onionpress_bluesky_run_sync_tick' );

function onionpress_bluesky_import_page() {
    if ( ! current_user_can( 'manage_options' ) ) {
        wp_die( 'Unauthorized' );
    }

    $notice = null;
    if ( $_SERVER['REQUEST_METHOD'] === 'POST' ) {
        if ( isset( $_POST['onionpress_bluesky_save_handle'] ) ) {
            check_admin_referer( 'onionpress_bluesky_save_handle', 'onionpress_bluesky_handle_nonce' );
            $notice = onionpress_bluesky_handle_save_handle_post();
        } elseif ( isset( $_POST['onionpress_bluesky_sync_now'] ) ) {
            check_admin_referer( 'onionpress_bluesky_sync_now', 'onionpress_bluesky_sync_nonce' );
            // Fire-and-forget — kick the daemon via a loopback wp-cron
            // call so the admin request doesn't block for 30 min.
            delete_option( ONIONPRESS_BLUESKY_DAEMON_LOCK );
            delete_transient( 'doing_cron' );
            wp_schedule_single_event( time(), ONIONPRESS_BLUESKY_CRON_HOOK );
            $cron_url = site_url( 'wp-cron.php?doing_wp_cron=' . microtime( true ) );
            wp_remote_post( $cron_url, array( 'timeout' => 0.01, 'blocking' => false, 'sslverify' => false ) );
            $notice = array( 'level' => 'success', 'message' => 'Sync started. Progress below — this page will refresh automatically.' );
        } elseif ( isset( $_POST['onionpress_bluesky_reset'] ) ) {
            check_admin_referer( 'onionpress_bluesky_reset', 'onionpress_bluesky_reset_nonce' );
            $notice = onionpress_bluesky_reset_cursors();
        }
    }

    $handle       = (string) get_option( ONIONPRESS_BLUESKY_HANDLE_OPT, '' );
    $did          = (string) get_option( ONIONPRESS_BLUESKY_DID_OPT, '' );
    $display      = (string) get_option( ONIONPRESS_BLUESKY_DISPLAY_OPT, '' );
    $total        = (int) get_option( ONIONPRESS_BLUESKY_POSTS_OPT, 0 );
    $last_sync    = (int) get_option( ONIONPRESS_BLUESKY_LAST_SYNC, 0 );
    $last_note    = (string) get_option( ONIONPRESS_BLUESKY_LAST_NOTE, '' );
    $cursor_state = onionpress_bluesky_get_cursor_for( $did );
    // Default include_replies=ON, include_reposts=OFF.
    $opts         = (array) get_option( ONIONPRESS_BLUESKY_OPTS_OPT, array( 'include_replies' => 1 ) );

    // Is the backfill daemon alive? (heartbeat within stale threshold)
    $daemon_raw   = (string) get_option( ONIONPRESS_BLUESKY_DAEMON_LOCK, '' );
    $daemon_alive = false;
    if ( $daemon_raw !== '' && strpos( $daemon_raw, ':' ) !== false ) {
        list( , $hb ) = explode( ':', $daemon_raw, 2 );
        if ( (int) $hb > 0 && ( time() - (int) $hb ) < ONIONPRESS_BLUESKY_DAEMON_STALE_SEC ) {
            $daemon_alive = true;
        }
    }

    // Imported count — posts whose _source_id starts with bluesky:.
    global $wpdb;
    $imported_now = (int) $wpdb->get_var( $wpdb->prepare(
        "SELECT COUNT(*) FROM {$wpdb->postmeta} WHERE meta_key='_source_id' AND meta_value LIKE %s",
        'bluesky:%'
    ) );

    // The daemon is kicked asynchronously, so on the "Sync now" POST it
    // hasn't heartbeated yet — refresh on that request too, or the
    // "refreshes automatically" promise in the notice doesn't hold.
    $just_kicked = isset( $_POST['onionpress_bluesky_sync_now'] );
    ?>
    <div class="wrap">
        <?php if ( $daemon_alive || $just_kicked ) : ?>
            <meta http-equiv="refresh" content="15">
        <?php endif; ?>
        <h1>Bluesky import</h1>
        <p>Archive your Bluesky posts here, just like a locally-hosted Twitter/X or Mastodon mirror.
           Uses the public AT Protocol API — no Bluesky login needed.</p>

        <?php if ( $notice ) : ?>
            <div class="notice notice-<?php echo esc_attr( $notice['level'] ); ?>">
                <p><?php echo wp_kses_post( $notice['message'] ); ?></p>
            </div>
        <?php endif; ?>

        <h2>Step 1 &mdash; Your Bluesky handle</h2>
        <p>Enter your Bluesky handle. Examples: <code>brewster.archive.org</code>, <code>you.bsky.social</code>.</p>
        <form method="post" style="margin-bottom:1.25em;">
            <?php wp_nonce_field( 'onionpress_bluesky_save_handle', 'onionpress_bluesky_handle_nonce' ); ?>
            <input type="hidden" name="onionpress_bluesky_save_handle" value="1">
            <table class="form-table" role="presentation">
                <tr>
                    <th><label for="bluesky_handle">Handle</label></th>
                    <td>
                        <input type="text" id="bluesky_handle" name="bluesky_handle"
                               value="<?php echo esc_attr( $handle ); ?>"
                               placeholder="you.bsky.social"
                               class="regular-text" style="max-width:380px;">
                        <fieldset style="margin-top:0.5em;">
                            <label><input type="checkbox" name="include_replies" value="1" <?php checked( ! empty( $opts['include_replies'] ) ); ?>> Include replies to other people</label><br>
                            <label><input type="checkbox" name="include_reposts" value="1" <?php checked( ! empty( $opts['include_reposts'] ) ); ?>> Include reposts (other people's posts you reposted)</label>
                        </fieldset>
                        <p class="description">Originals and self-threads are always imported. Quoted posts are preserved inline as blockquotes so the context survives even if the original is later deleted on Bluesky.</p>
                        <?php submit_button( $did ? 'Update' : 'Save & look up handle', 'primary', 'submit', false ); ?>
                    </td>
                </tr>
            </table>
        </form>

        <?php if ( $did ) :
            // Drift warning: if backfill claims "done" but we've
            // imported far fewer posts than Bluesky says exist, the
            // cursor landed in a bad state (e.g. test pollution or an
            // interrupted walk). Threshold is 50% of the server's
            // reported postsCount — leaves room for users who opt out
            // of reposts (which Bluesky counts toward the total), while
            // still catching obvious drift like "43 imported of 102".
            $drift_warn = ( $cursor_state === 'done'
                            && $total > 0
                            && $imported_now > 0
                            && $imported_now < (int) ( $total * 0.5 ) );
            if ( $drift_warn ) : ?>
                <div class="notice notice-warning">
                    <p><strong>Backfill ended early.</strong> Imported
                        <strong><?php echo number_format_i18n( $imported_now ); ?></strong>
                        posts but Bluesky reports <strong><?php echo number_format_i18n( $total ); ?></strong>.
                        The backfill cursor says "done" but probably landed in a
                        bad state (test pollution, interrupted walk, or an account
                        swap). Click <strong>Reset cursors</strong> below and then
                        <strong>Sync now</strong> — already-imported posts will be
                        deduplicated automatically.</p>
                </div>
            <?php endif; ?>
            <?php
            // A dead daemon with an unfinished backfill used to render as a
            // silent "0" — say so instead, and surface the last note, which
            // carries the actual error when a tick failed.
            if ( ! $daemon_alive && ! $just_kicked && $cursor_state !== 'done' ) : ?>
                <div class="notice notice-warning">
                    <p><strong>Sync is not running.</strong>
                        <?php if ( $cursor_state === '' && $imported_now === 0 ) : ?>
                            Nothing has been imported yet.
                        <?php else : ?>
                            The backfill is incomplete and paused.
                        <?php endif; ?>
                        Click <strong>Sync now</strong> below to start it.
                        <?php if ( $last_note ) : ?>
                            <br><small style="color:#666;">Last sync<?php echo $last_sync ? ' (' . esc_html( human_time_diff( $last_sync ) ) . ' ago)' : ''; ?>: <?php echo esc_html( $last_note ); ?></small>
                        <?php endif; ?>
                    </p>
                </div>
            <?php endif; ?>
            <h2>Step 2 &mdash; Sync</h2>
            <table class="wp-list-table widefat" style="max-width:620px;">
                <tbody>
                    <tr><th style="width:200px;">Account</th>
                        <td><code><?php echo esc_html( $handle ); ?></code>
                            <?php if ( $display ) : ?> <span style="color:#888;">(<?php echo esc_html( $display ); ?>)</span><?php endif; ?>
                            <br><small style="color:#666;"><?php echo esc_html( $did ); ?></small></td></tr>
                    <tr><th>Bluesky reports</th>
                        <td><?php echo number_format_i18n( $total ); ?> total posts
                            <small style="color:#666;">(including replies and reposts)</small>
                        </td></tr>
                    <tr><th>Imported here</th>
                        <td><?php echo number_format_i18n( $imported_now ); ?></td></tr>
                    <tr><th>Backfill</th><td><?php
                        if ( $daemon_alive ) {
                            echo 'Running — auto-refreshing.';
                        } elseif ( $cursor_state === 'done' ) {
                            echo 'Complete';
                        } elseif ( $cursor_state === '' ) {
                            echo 'Not started';
                        } else {
                            echo 'In progress (paused between ticks)';
                        }
                    ?></td></tr>
                    <tr><th>Last sync</th>
                        <td><?php echo $last_sync ? esc_html( human_time_diff( $last_sync ) . ' ago' ) : '—'; ?>
                            <?php if ( $last_note ) : ?>
                                <br><small style="color:#666;"><?php echo esc_html( $last_note ); ?></small>
                            <?php endif; ?>
                        </td></tr>
                </tbody>
            </table>

            <form method="post" style="margin-top:1em;display:inline-block;">
                <?php wp_nonce_field( 'onionpress_bluesky_sync_now', 'onionpress_bluesky_sync_nonce' ); ?>
                <input type="hidden" name="onionpress_bluesky_sync_now" value="1">
                <?php submit_button( 'Sync now', 'primary', 'submit', false ); ?>
            </form>
            <form method="post" style="margin-top:1em;display:inline-block;margin-left:.5em;">
                <?php wp_nonce_field( 'onionpress_bluesky_reset', 'onionpress_bluesky_reset_nonce' ); ?>
                <input type="hidden" name="onionpress_bluesky_reset" value="1">
                <?php submit_button( 'Reset cursors', 'secondary', 'submit', false, array( 'onclick' => "return confirm('Clear the backfill cursor? Imported posts are kept; next sync will rescan.');" ) ); ?>
            </form>
        <?php endif; ?>
    </div>
    <?php
}

function onionpress_bluesky_handle_save_handle_post() {
    $raw = isset( $_POST['bluesky_handle'] ) ? wp_unslash( $_POST['bluesky_handle'] ) : '';
    $raw = trim( (string) $raw );

    // Allow "at://did:plc:..." (already a DID URI), "handle.bsky.social",
    // "@handle.bsky.social", or a bsky.app profile URL.
    $handle = '';
    $did    = '';
    if ( preg_match( '~^at://(did:[a-z0-9:._\-]+)~i', $raw, $m ) ) {
        $did = $m[1];
    } elseif ( preg_match( '~^https?://bsky\.app/profile/([^/?#]+)~i', $raw, $m ) ) {
        $candidate = $m[1];
        if ( strpos( $candidate, 'did:' ) === 0 ) {
            $did = $candidate;
        } else {
            $handle = strtolower( $candidate );
        }
    } else {
        $handle = strtolower( ltrim( $raw, '@' ) );
    }

    if ( $did === '' && ! preg_match( '/^[a-z0-9]([a-z0-9\-]{0,62}\.)+[a-z]{2,}$/', $handle ) ) {
        return array( 'level' => 'error', 'message' => 'That doesn\'t look like a Bluesky handle. Try <code>you.bsky.social</code> or <code>brewster.archive.org</code>.' );
    }

    // Save options regardless of lookup outcome.
    $opts = array(
        'include_replies' => ! empty( $_POST['include_replies'] ),
        'include_reposts' => ! empty( $_POST['include_reposts'] ),
    );
    update_option( ONIONPRESS_BLUESKY_OPTS_OPT, $opts );

    // Resolve handle → DID if we don't already have the DID.
    if ( $did === '' ) {
        $resolved = onionpress_bluesky_resolve_handle( $handle );
        if ( is_wp_error( $resolved ) ) {
            return array( 'level' => 'error', 'message' => 'Could not resolve <code>' . esc_html( $handle ) . '</code>: ' . esc_html( $resolved->get_error_message() ) );
        }
        $did = $resolved;
    }

    // Fetch profile for display name + post count.
    $profile = onionpress_bluesky_fetch_profile( $did );
    $display = '';
    $total   = 0;
    if ( ! is_wp_error( $profile ) && is_array( $profile ) ) {
        $display = (string) ( $profile['displayName'] ?? '' );
        $total   = (int)    ( $profile['postsCount']  ?? 0 );
        // If we only had a DID, try to backfill the handle from the profile.
        if ( $handle === '' && ! empty( $profile['handle'] ) ) {
            $handle = (string) $profile['handle'];
        }
    }

    // Reset cursors if we're pointing at a different DID than before.
    // (The owner-aware cursor accessors below also defend against
    // someone writing DID_OPT directly without going through this
    // path — e.g. a test harness.)
    $prev_did = (string) get_option( ONIONPRESS_BLUESKY_DID_OPT, '' );
    if ( $prev_did !== '' && $prev_did !== $did ) {
        onionpress_bluesky_clear_cursors();
    }

    update_option( ONIONPRESS_BLUESKY_HANDLE_OPT,  $handle );
    update_option( ONIONPRESS_BLUESKY_DID_OPT,     $did );
    update_option( ONIONPRESS_BLUESKY_DISPLAY_OPT, $display );
    update_option( ONIONPRESS_BLUESKY_POSTS_OPT,   $total );

    if ( ! wp_next_scheduled( ONIONPRESS_BLUESKY_CRON_HOOK ) ) {
        wp_schedule_event( time() + 30, 'onionpress_bluesky_5min', ONIONPRESS_BLUESKY_CRON_HOOK );
    }

    $summary = $total
        ? 'Bluesky reports <strong>' . number_format_i18n( $total ) . '</strong> posts.'
        : 'Profile reachable.';
    return array(
        'level'   => 'success',
        'message' => 'Saved <code>' . esc_html( $handle ?: $did ) . '</code>. ' . $summary . ' Click <strong>Sync now</strong> below to start importing.',
    );
}

function onionpress_bluesky_reset_cursors() {
    onionpress_bluesky_clear_cursors();
    return array( 'level' => 'success', 'message' => 'Cursors cleared. Next sync will rescan from scratch (but will skip already-imported posts).' );
}

/**
 * Daemon entry. One invocation runs sync_one_tick in a loop with
 * heartbeated token-lock mutex, same shape as the Mastodon importer.
 */
function onionpress_bluesky_run_sync_tick( $from_admin = false ) {
    $did = (string) get_option( ONIONPRESS_BLUESKY_DID_OPT, '' );
    if ( $did === '' ) {
        return array( 'level' => 'error', 'message' => 'Enter your Bluesky handle first.' );
    }

    $now = time();
    $raw = (string) get_option( ONIONPRESS_BLUESKY_DAEMON_LOCK, '' );
    if ( $raw !== '' && strpos( $raw, ':' ) !== false ) {
        list( , $ts_str ) = explode( ':', $raw, 2 );
        $lock_ts = (int) $ts_str;
        if ( $lock_ts > 0 && ( $now - $lock_ts ) < ONIONPRESS_BLUESKY_DAEMON_STALE_SEC ) {
            if ( $from_admin ) {
                return array( 'level' => 'warning', 'message' => 'Another sync is already running. Try again in a few minutes.' );
            }
            return;
        }
    }
    $token = function_exists( 'wp_generate_password' ) ? wp_generate_password( 16, false, false ) : bin2hex( random_bytes( 8 ) );
    update_option( ONIONPRESS_BLUESKY_DAEMON_LOCK, $token . ':' . $now, false );

    @set_time_limit( ONIONPRESS_BLUESKY_DAEMON_MAX_SEC + 60 );
    @ignore_user_abort( true );

    $loop_deadline = microtime( true ) + ONIONPRESS_BLUESKY_DAEMON_MAX_SEC;
    $total = array( 'imported' => 0, 'skipped' => 0, 'errors' => 0, 'pages' => 0 );

    try {
        while ( microtime( true ) < $loop_deadline ) {
            $cur = (string) get_option( ONIONPRESS_BLUESKY_DAEMON_LOCK, '' );
            if ( strpos( $cur, $token . ':' ) !== 0 ) {
                break;
            }
            update_option( ONIONPRESS_BLUESKY_DAEMON_LOCK, $token . ':' . time(), false );

            $result = onionpress_bluesky_sync_one_tick( $did );
            foreach ( array( 'imported', 'skipped', 'errors', 'pages' ) as $k ) {
                $total[ $k ] += (int) ( $result['stats'][ $k ] ?? 0 );
            }

            if ( $result['done'] ) {
                break;
            }
            // Transport errors don't abort the daemon — the cursor is
            // preserved, so the next tick resumes from the same point.
            // Only bail if we've seen errors on several ticks in a row
            // (something persistent, not a Tor circuit hiccup).
            if ( ! empty( $result['errors'] ) ) {
                $consecutive_error_ticks = ( $consecutive_error_ticks ?? 0 ) + 1;
                if ( $consecutive_error_ticks >= 3 ) {
                    break;
                }
                sleep( 10 ); // longer pause to let Tor pick a fresh circuit
            } else {
                $consecutive_error_ticks = 0;
                sleep( ONIONPRESS_BLUESKY_DAEMON_IDLE_SEC );
            }
        }
    } finally {
        $cur = (string) get_option( ONIONPRESS_BLUESKY_DAEMON_LOCK, '' );
        if ( strpos( $cur, $token . ':' ) === 0 ) {
            delete_option( ONIONPRESS_BLUESKY_DAEMON_LOCK );
        }
    }

    $summary = sprintf(
        '%d imported, %d skipped, %d errors across %d pages (daemon run)',
        $total['imported'], $total['skipped'], $total['errors'], $total['pages']
    );
    update_option( ONIONPRESS_BLUESKY_LAST_NOTE, $summary );

    if ( ! $from_admin ) return;
    $level = ( $total['errors'] > 0 && $total['imported'] === 0 ) ? 'error' : 'success';
    return array( 'level' => $level, 'message' => 'Sync: ' . esc_html( $summary ) );
}

/**
 * One sync tick: forward catch-up from top until we hit the newest-URI
 * marker, then backfill using the stored cursor until absent (= end of
 * feed). Bluesky's pagination signals end explicitly via a missing
 * `cursor` in the response — no "short-page" guessing needed.
 */
function onionpress_bluesky_sync_one_tick( $did ) {
    $lock = get_transient( ONIONPRESS_BLUESKY_LOCK );
    if ( $lock ) {
        return array(
            'stats'  => array( 'imported' => 0, 'skipped' => 0, 'errors' => 0, 'pages' => 0 ),
            'errors' => array( 'tick mutex held' ),
            'note'   => 'tick mutex held',
            'done'   => false,
        );
    }
    set_transient( ONIONPRESS_BLUESKY_LOCK, time(), 10 * MINUTE_IN_SECONDS );

    @set_time_limit( ONIONPRESS_BLUESKY_TICK_BUDGET_SEC + 30 );
    $deadline = microtime( true ) + ONIONPRESS_BLUESKY_TICK_BUDGET_SEC;

    $opts = (array) get_option( ONIONPRESS_BLUESKY_OPTS_OPT, array( 'include_replies' => 1 ) );
    $stats = array( 'imported' => 0, 'skipped' => 0, 'errors' => 0, 'pages' => 0 );
    $errors = array();

    try {
        // Phase 1 — forward catch-up. Fetch from the top (no cursor) and
        // stop at the first known AT-URI. Only meaningful once we have a
        // newest_uri marker (first run: skip to backfill directly).
        $newest_uri = onionpress_bluesky_get_newest_for( $did );
        if ( $newest_uri !== '' ) {
            $cursor = '';
            $top_uri_seen = '';
            $caught_up = false;
            while ( $stats['pages'] < ONIONPRESS_BLUESKY_PAGES_PER_TICK
                    && microtime( true ) < $deadline
                    && ! $caught_up ) {
                $resp = onionpress_bluesky_fetch_feed( $did, $cursor );
                if ( is_wp_error( $resp ) ) { $errors[] = $resp->get_error_message(); break; }
                $stats['pages']++;
                $feed = is_array( $resp['feed'] ?? null ) ? $resp['feed'] : array();
                if ( empty( $feed ) ) break;
                foreach ( $feed as $item ) {
                    if ( microtime( true ) >= $deadline ) break;
                    $uri = onionpress_bluesky_feed_item_uri( $item );
                    if ( $uri === '' ) { $stats['errors']++; continue; }
                    if ( $top_uri_seen === '' ) $top_uri_seen = $uri;
                    if ( $uri === $newest_uri ) { $caught_up = true; break; }
                    $r = onionpress_bluesky_import_post( $item, $opts );
                    $stats[ $r ] = ( $stats[ $r ] ?? 0 ) + 1;
                }
                if ( $caught_up ) break;
                $cursor = (string) ( $resp['cursor'] ?? '' );
                if ( $cursor === '' ) break; // hit end of feed without finding newest
            }
            if ( $top_uri_seen !== '' ) {
                onionpress_bluesky_set_newest_for( $did, $top_uri_seen );
            }
        }

        // Phase 2 — backfill. Walk older posts via the persisted cursor
        // until the API omits `cursor` (signals end of history).
        $backfill_cursor = onionpress_bluesky_get_cursor_for( $did );
        if ( $backfill_cursor !== 'done'
             && $stats['pages'] < ONIONPRESS_BLUESKY_PAGES_PER_TICK
             && microtime( true ) < $deadline ) {
            $no_progress = 0;
            while ( $stats['pages'] < ONIONPRESS_BLUESKY_PAGES_PER_TICK
                    && microtime( true ) < $deadline ) {
                $before = $backfill_cursor;
                $resp = onionpress_bluesky_fetch_feed( $did, $backfill_cursor );
                if ( is_wp_error( $resp ) ) { $errors[] = $resp->get_error_message(); break; }
                $stats['pages']++;
                $feed = is_array( $resp['feed'] ?? null ) ? $resp['feed'] : array();
                foreach ( $feed as $item ) {
                    if ( microtime( true ) >= $deadline ) break;
                    $uri = onionpress_bluesky_feed_item_uri( $item );
                    if ( $uri === '' ) { $stats['errors']++; continue; }
                    // First ever item seen becomes the top marker so
                    // subsequent runs have something to stop at.
                    if ( onionpress_bluesky_get_newest_for( $did ) === '' ) {
                        onionpress_bluesky_set_newest_for( $did, $uri );
                    }
                    $r = onionpress_bluesky_import_post( $item, $opts );
                    $stats[ $r ] = ( $stats[ $r ] ?? 0 ) + 1;
                }
                $new_cursor = (string) ( $resp['cursor'] ?? '' );
                if ( $new_cursor === '' ) {
                    onionpress_bluesky_set_cursor_for( $did, 'done' );
                    break;
                }
                if ( $new_cursor === $before ) {
                    // Cycle guard — the API is returning the same cursor
                    // without making progress. Stop after a few tries;
                    // the next cron fire retries with fresh state.
                    $no_progress++;
                    if ( $no_progress >= 3 ) {
                        $errors[] = 'cursor stuck at ' . substr( $new_cursor, 0, 24 ) . '… after retries';
                        break;
                    }
                } else {
                    $no_progress = 0;
                }
                $backfill_cursor = $new_cursor;
                onionpress_bluesky_set_cursor_for( $did, $backfill_cursor );
            }
        }

        // End-of-tick threading sweeps. Same shape as the Mastodon
        // importer: reattach folds pending self-replies into freshly-
        // arrived parents; backfill_context walks the AT API for any
        // legacy reply whose parent isn't here yet. Both are capped so
        // a single tick can't blow its budget — leftovers carry over.
        $reattached = onionpress_bluesky_reattach_pending();
        if ( $reattached > 0 ) {
            $stats['reattached'] = ( $stats['reattached'] ?? 0 ) + $reattached;
        }
        if ( microtime( true ) < $deadline ) {
            $context_done = onionpress_bluesky_backfill_context();
            if ( $context_done > 0 ) {
                $stats['context'] = ( $stats['context'] ?? 0 ) + $context_done;
            }
        }
    } finally {
        delete_transient( ONIONPRESS_BLUESKY_LOCK );
    }

    update_option( ONIONPRESS_BLUESKY_LAST_SYNC, time() );
    $note = sprintf(
        '%d imported, %d skipped, %d errors across %d pages',
        intval( $stats['imported'] ), intval( $stats['skipped'] ),
        intval( $stats['errors'] ),   intval( $stats['pages'] )
    );
    if ( $errors ) { $note .= ' — last error: ' . $errors[ count( $errors ) - 1 ]; }
    $done = ( onionpress_bluesky_get_cursor_for( $did ) === 'done' );
    return array(
        'stats'  => $stats,
        'errors' => $errors,
        'note'   => $note,
        'done'   => $done,
    );
}

// ─────────────────────────── fetchers ───────────────────────────────

/**
 * Resolve a Bluesky handle to its DID via the public identity endpoint.
 * DIDs are stable across handle renames, so we always store/act on DID
 * downstream.
 */
function onionpress_bluesky_resolve_handle( $handle ) {
    $mock = apply_filters( 'onionpress_bluesky_resolve_handle_mock', null, $handle );
    if ( $mock !== null ) {
        if ( is_wp_error( $mock ) ) return $mock;
        return (string) $mock;
    }
    $r = onionpress_bluesky_api_get(
        'https://' . ONIONPRESS_BLUESKY_API_HOST . '/xrpc/com.atproto.identity.resolveHandle?handle=' . rawurlencode( $handle )
    );
    if ( is_wp_error( $r ) ) return $r;
    if ( (int) $r['code'] !== 200 || empty( $r['json']['did'] ) ) {
        return new WP_Error( 'resolve_failed', 'handle not found (HTTP ' . $r['code'] . ')' );
    }
    return (string) $r['json']['did'];
}

function onionpress_bluesky_fetch_profile( $did ) {
    $mock = apply_filters( 'onionpress_bluesky_fetch_profile_mock', null, $did );
    if ( $mock !== null ) return $mock;
    $r = onionpress_bluesky_api_get(
        'https://' . ONIONPRESS_BLUESKY_API_HOST . '/xrpc/app.bsky.actor.getProfile?actor=' . rawurlencode( $did )
    );
    if ( is_wp_error( $r ) ) return $r;
    if ( (int) $r['code'] !== 200 ) {
        return new WP_Error( 'profile_http', 'HTTP ' . $r['code'] );
    }
    return is_array( $r['json'] ) ? $r['json'] : array();
}

/**
 * Fetch one page of the author's feed. Returns the decoded body (an
 * array with `feed` and optional `cursor`) or WP_Error on failure.
 *
 * Test hook: `onionpress_bluesky_fetch_feed_mock` lets integration
 * tests inject canned pages without hitting the network.
 */
function onionpress_bluesky_fetch_feed( $did, $cursor = '' ) {
    $params = array(
        'actor'  => $did,
        'limit'  => ONIONPRESS_BLUESKY_PER_PAGE,
        'filter' => 'posts_with_replies',
    );
    if ( $cursor !== '' && $cursor !== 'done' ) {
        $params['cursor'] = $cursor;
    }

    $mock = apply_filters( 'onionpress_bluesky_fetch_feed_mock', null, $params );
    if ( $mock !== null ) {
        return $mock;
    }

    $url = 'https://' . ONIONPRESS_BLUESKY_API_HOST . '/xrpc/app.bsky.feed.getAuthorFeed?' . http_build_query( $params );
    $r = onionpress_bluesky_api_get( $url );
    if ( is_wp_error( $r ) ) return $r;
    if ( (int) $r['code'] !== 200 ) {
        return new WP_Error( 'bluesky_http', 'HTTP ' . $r['code'] . ' from public.api.bsky.app' );
    }
    return is_array( $r['json'] ) ? $r['json'] : array( 'feed' => array() );
}

function onionpress_bluesky_api_get( $url ) {
    if ( ! function_exists( 'curl_init' ) ) {
        return new WP_Error( 'no_curl', 'curl extension required for Bluesky import' );
    }
    $ch = curl_init( $url );
    curl_setopt_array( $ch, array(
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_FOLLOWLOCATION => true,
        CURLOPT_MAXREDIRS      => 3,
        CURLOPT_PROXY          => ONIONPRESS_BLUESKY_SOCKS_PROXY,
        CURLOPT_PROXYTYPE      => CURLPROXY_SOCKS5_HOSTNAME,
        CURLOPT_TIMEOUT        => ONIONPRESS_BLUESKY_HTTP_TIMEOUT,
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

function onionpress_bluesky_fetch_file( $url, $dest_path ) {
    $fh = @fopen( $dest_path, 'wb' );
    if ( ! $fh ) return new WP_Error( 'io', 'open temp failed' );
    $ch = curl_init( $url );
    curl_setopt_array( $ch, array(
        CURLOPT_FILE           => $fh,
        CURLOPT_FOLLOWLOCATION => true,
        CURLOPT_MAXREDIRS      => 5,
        CURLOPT_PROXY          => ONIONPRESS_BLUESKY_SOCKS_PROXY,
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

// ──────────────────────── import + rendering ────────────────────────

/**
 * Extract the AT-URI of a feed item. For reposts, the outer `reason`
 * wraps the original post — we still dedupe on `post.uri` (the
 * original's URI) plus a `repost:` prefix so one person reposting the
 * same item twice doesn't collide. For regular posts, just `post.uri`.
 */
function onionpress_bluesky_feed_item_uri( $item ) {
    $post_uri = (string) ( $item['post']['uri'] ?? '' );
    if ( $post_uri === '' ) return '';
    $reason = $item['reason'] ?? null;
    if ( is_array( $reason ) && ( $reason['$type'] ?? '' ) === 'app.bsky.feed.defs#reasonRepost' ) {
        // Scoped by who reposted it + when: a subject DID can be reposted
        // by many actors, and we don't want those to collide under the
        // same author's feed if the API ever includes both.
        $ts = (string) ( $reason['indexedAt'] ?? '' );
        return 'repost:' . $ts . ':' . $post_uri;
    }
    return $post_uri;
}

/**
 * Import one feed-view item. Returns 'imported' | 'skipped' | 'errors'.
 */
/**
 * Look up a post by its `_source_id` postmeta. Returns post ID or 0.
 */
function onionpress_bluesky_find_post_by_source_id( $source_id ) {
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
 * Look up a comment by its `_source_id` commentmeta. Used for dedup
 * and for nesting a reply under another reply that's already been
 * folded into a thread.
 */
function onionpress_bluesky_find_comment_by_source_id( $source_id ) {
    $ids = get_comments( array(
        'meta_key'   => '_source_id',
        'meta_value' => $source_id,
        'number'     => 1,
        'fields'     => 'ids',
    ) );
    return ! empty( $ids ) ? (int) $ids[0] : 0;
}

/**
 * Find the post a reply should attach to, given the source_id of the
 * post it replies to. Returns the post ID (its own, or the post a
 * comment-form ancestor was attached to), or 0 when the parent isn't
 * yet imported.
 */
function onionpress_bluesky_find_attachable_post( $parent_source_id ) {
    $pid = onionpress_bluesky_find_post_by_source_id( $parent_source_id );
    if ( $pid ) return $pid;
    $cid = onionpress_bluesky_find_comment_by_source_id( $parent_source_id );
    if ( $cid ) {
        $c = get_comment( $cid );
        if ( $c ) return (int) $c->comment_post_ID;
    }
    return 0;
}

/**
 * Fetch a single AT-URI from the Bluesky public AppView. Wraps the
 * `getPosts` endpoint (plural-uris is the cheapest single-post lookup).
 * Returns a feed-item-shaped array `{post: PostView}` so it can flow
 * straight into import_post(), or null on failure (404, network, etc.).
 *
 * Test mock: `onionpress_bluesky_fetch_post_mock` filter receives
 * (null, $uri) and may return either a feed-item array, null, or a
 * WP_Error.
 */
function onionpress_bluesky_fetch_post_by_uri( $uri ) {
    $mock = apply_filters( 'onionpress_bluesky_fetch_post_mock', null, $uri );
    if ( $mock !== null ) {
        if ( is_array( $mock ) ) return $mock;
        return null;
    }
    if ( $uri === '' ) return null;
    $url = 'https://' . ONIONPRESS_BLUESKY_API_HOST
         . '/xrpc/app.bsky.feed.getPosts?uris=' . rawurlencode( $uri );
    $r = onionpress_bluesky_api_get( $url );
    if ( is_wp_error( $r ) ) return null;
    if ( (int) $r['code'] !== 200 ) return null;
    $posts = (array) ( $r['json']['posts'] ?? array() );
    if ( empty( $posts[0] ) ) return null;
    // import_post() expects a feed-view item with `post` wrapping a
    // PostView. getPosts returns bare PostViews — wrap to match shape.
    return array( 'post' => $posts[0] );
}

/**
 * Ensure an AT-URI is in our DB, fetching it (and its ancestor chain
 * via record.reply.parent) on demand. Returns the post ID a child
 * reply should attach to, or 0 on failure (404, depth cap, transport).
 *
 * Walks at most CONTEXT_DEPTH_MAX hops up. Each hop is a single
 * Tor-routed `getPosts` call.
 */
function onionpress_bluesky_ensure_imported_with_ancestry( $uri, $opts, $depth = 0 ) {
    if ( $depth > ONIONPRESS_BLUESKY_CONTEXT_DEPTH_MAX ) return 0;
    $source_id = 'bluesky:' . $uri;
    $existing  = onionpress_bluesky_find_attachable_post( $source_id );
    if ( $existing ) return $existing;
    $item = onionpress_bluesky_fetch_post_by_uri( $uri );
    if ( ! is_array( $item ) ) return 0;
    // Recurse upward FIRST so the parent is in DB before we import this
    // ancestor — that way import_post can thread it correctly in one pass.
    $parent_uri = (string) ( $item['post']['record']['reply']['parent']['uri'] ?? '' );
    if ( $parent_uri !== '' ) {
        onionpress_bluesky_ensure_imported_with_ancestry( $parent_uri, $opts, $depth + 1 );
    }
    $our_did = (string) get_option( ONIONPRESS_BLUESKY_DID_OPT, '' );
    $author_did = (string) ( $item['post']['author']['did'] ?? '' );
    $is_ours = ( $our_did !== '' && $author_did === $our_did );
    onionpress_bluesky_import_post( $item, $opts, ! $is_ours );
    return onionpress_bluesky_find_attachable_post( $source_id );
}

/**
 * Create a WP comment on $parent_post_id from a Bluesky feed item.
 * Idempotent on _source_id commentmeta. If the parent reply (the URI
 * this item replies to) is itself a comment in our DB, the new comment
 * nests under it via comment_parent — preserving thread depth.
 */
function onionpress_bluesky_create_reply_comment( $item, $parent_post_id ) {
    $uri = onionpress_bluesky_feed_item_uri( $item );
    if ( $uri === '' ) return 'errors';
    $source_id = 'bluesky:' . $uri;
    if ( onionpress_bluesky_find_comment_by_source_id( $source_id ) ) {
        return 'skipped';
    }
    $post = $item['post'] ?? array();
    $record = $post['record'] ?? array();
    $ts = strtotime( (string) ( $record['createdAt'] ?? '' ) );
    if ( ! $ts ) return 'errors';
    list( $content_html, ) = onionpress_bluesky_render_content( $item );
    $author = (string) ( $post['author']['displayName'] ?? $post['author']['handle'] ?? 'me' );
    $author_url = onionpress_bluesky_at_uri_to_web_url(
        (string) ( $post['uri'] ?? '' ), (string) ( $post['author']['handle'] ?? '' )
    );
    $comment_parent = 0;
    $reply_to_uri = (string) ( $record['reply']['parent']['uri'] ?? '' );
    if ( $reply_to_uri !== '' ) {
        $comment_parent = onionpress_bluesky_find_comment_by_source_id( 'bluesky:' . $reply_to_uri );
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
            '_raw'        => wp_json_encode( $item ),
        ),
    ) );
    return $comment_id ? 'imported' : 'errors';
}

/**
 * Convert a top-level reply post into a comment on $parent_post_id.
 * Reads from post fields + postmeta (no dependence on _raw being
 * decodeable). Idempotent on _source_id; deletes the placeholder post
 * on success.
 */
function onionpress_bluesky_convert_post_to_comment( $post_id, $parent_post_id ) {
    $post = get_post( $post_id );
    if ( ! $post ) return false;
    $source_id = (string) get_post_meta( $post_id, '_source_id', true );
    if ( $source_id === '' ) return false;
    if ( onionpress_bluesky_find_comment_by_source_id( $source_id ) ) {
        wp_delete_post( $post_id, true );
        return true;
    }
    $reply_to_uri = (string) get_post_meta( $post_id, '_reply_to_id', true );
    $comment_parent = 0;
    if ( $reply_to_uri !== '' ) {
        $comment_parent = onionpress_bluesky_find_comment_by_source_id( 'bluesky:' . $reply_to_uri );
    }
    $author = (string) get_option( ONIONPRESS_BLUESKY_DISPLAY_OPT, '' );
    if ( $author === '' ) $author = (string) get_option( ONIONPRESS_BLUESKY_HANDLE_OPT, 'me' );
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
 * Convert _pending_reattach=1 posts into comments once their parent
 * has arrived. Run at the end of each sync tick; oldest first so a
 * chain A→B→C ends up with B as a comment before C tries to attach.
 */
function onionpress_bluesky_reattach_pending( $limit = 200 ) {
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
            delete_post_meta( $pid, '_pending_reattach' );
            continue;
        }
        $parent_pid = onionpress_bluesky_find_attachable_post( 'bluesky:' . $reply_to );
        if ( ! $parent_pid ) continue;
        if ( onionpress_bluesky_convert_post_to_comment( $pid, $parent_pid ) ) {
            $converted++;
        }
    }
    return $converted;
}

/**
 * End-of-tick context backfill: take a small batch of existing
 * _is_reply=1 posts whose parent isn't in our DB, fetch the parent
 * (and ancestor chain) via getPosts, and re-thread the reply as a
 * comment. Capped per call because each ancestor walk is Tor-bound.
 */
function onionpress_bluesky_backfill_context( $limit = null ) {
    if ( $limit === null ) $limit = ONIONPRESS_BLUESKY_CONTEXT_BATCH_PER_TICK;
    global $wpdb;
    $candidates = $wpdb->get_results( $wpdb->prepare(
        "SELECT p.ID, m2.meta_value AS reply_to_id
         FROM {$wpdb->posts} p
         JOIN {$wpdb->postmeta} m1 ON m1.post_id=p.ID AND m1.meta_key='_source_id'
         JOIN {$wpdb->postmeta} m2 ON m2.post_id=p.ID AND m2.meta_key='_reply_to_id'
         JOIN {$wpdb->postmeta} m4 ON m4.post_id=p.ID AND m4.meta_key='_is_reply' AND m4.meta_value='1'
         WHERE m1.meta_value LIKE 'bluesky:%' AND m2.meta_value <> ''
           AND p.post_status='publish'
           AND NOT EXISTS (
             SELECT 1 FROM {$wpdb->postmeta} pm
             WHERE pm.meta_key='_source_id'
               AND pm.meta_value = CONCAT('bluesky:', m2.meta_value)
           )
           AND NOT EXISTS (
             SELECT 1 FROM {$wpdb->commentmeta} cm
             WHERE cm.meta_key='_source_id'
               AND cm.meta_value = CONCAT('bluesky:', m2.meta_value)
           )
         ORDER BY p.post_date DESC
         LIMIT %d",
        (int) $limit
    ) );
    if ( empty( $candidates ) ) return 0;
    $opts = (array) get_option( ONIONPRESS_BLUESKY_OPTS_OPT, array() );
    $threaded = 0;
    foreach ( $candidates as $c ) {
        $parent_pid = onionpress_bluesky_ensure_imported_with_ancestry(
            (string) $c->reply_to_id, $opts, 1
        );
        if ( ! $parent_pid ) continue;
        if ( onionpress_bluesky_convert_post_to_comment( (int) $c->ID, $parent_pid ) ) {
            $threaded++;
        }
    }
    return $threaded;
}

/**
 * One-shot migration for installs that imported reply posts before
 * threading was wired up. Finds existing posts with _is_reply=1 whose
 * _reply_to_id matches a known _source_id, converts them to comments
 * on that parent, and deletes the now-redundant top-level post.
 *
 * Gated by an option flag so it only runs once per install.
 */
function onionpress_bluesky_migrate_replies_to_comments() {
    if ( get_option( ONIONPRESS_BLUESKY_THREADS_MIGRATED_OPT ) === 'yes' ) {
        return 0;
    }
    if ( get_option( ONIONPRESS_BLUESKY_DID_OPT ) === false
         || (string) get_option( ONIONPRESS_BLUESKY_DID_OPT, '' ) === '' ) {
        return 0;
    }
    global $wpdb;
    $candidates = $wpdb->get_results(
        "SELECT p.ID, m2.meta_value AS reply_to_id
         FROM {$wpdb->posts} p
         JOIN {$wpdb->postmeta} m1 ON m1.post_id=p.ID AND m1.meta_key='_source_id'
         JOIN {$wpdb->postmeta} m2 ON m2.post_id=p.ID AND m2.meta_key='_reply_to_id'
         JOIN {$wpdb->postmeta} m4 ON m4.post_id=p.ID AND m4.meta_key='_is_reply' AND m4.meta_value='1'
         WHERE m1.meta_value LIKE 'bluesky:%' AND m2.meta_value <> ''
         ORDER BY p.post_date ASC"
    );
    $converted = 0;
    foreach ( $candidates as $c ) {
        $parent_pid = onionpress_bluesky_find_attachable_post( 'bluesky:' . $c->reply_to_id );
        if ( ! $parent_pid ) continue;
        if ( onionpress_bluesky_convert_post_to_comment( (int) $c->ID, $parent_pid ) ) {
            $converted++;
        }
    }
    update_option( ONIONPRESS_BLUESKY_THREADS_MIGRATED_OPT, 'yes' );
    return $converted;
}

function onionpress_bluesky_import_post( $item, $opts, $as_context = false ) {
    $uri = onionpress_bluesky_feed_item_uri( $item );
    if ( $uri === '' ) return 'errors';

    $source_id = 'bluesky:' . $uri;
    $existing  = get_posts( array(
        'post_type'      => 'post',
        'meta_key'       => '_source_id',
        'meta_value'     => $source_id,
        'post_status'    => 'any',
        'posts_per_page' => 1,
        'fields'         => 'ids',
    ) );
    if ( ! empty( $existing ) ) return 'skipped';

    $post   = $item['post'] ?? array();
    $record = $post['record'] ?? array();
    $reason = $item['reason'] ?? null;

    $is_repost = is_array( $reason ) && ( $reason['$type'] ?? '' ) === 'app.bsky.feed.defs#reasonRepost';
    $reply     = $record['reply'] ?? null;
    $in_reply  = is_array( $reply );

    // Self-reply: parent AT-URI starts with our DID (thread continuation).
    $our_did   = (string) get_option( ONIONPRESS_BLUESKY_DID_OPT, '' );
    $parent_uri = (string) ( $reply['parent']['uri'] ?? '' );
    $is_self_reply = $in_reply && $our_did !== '' && strpos( $parent_uri, 'at://' . $our_did . '/' ) === 0;

    if ( $is_repost && empty( $opts['include_reposts'] ) ) {
        return 'skipped';
    }
    if ( $in_reply && ! $is_self_reply && empty( $opts['include_replies'] ) ) {
        return 'skipped';
    }

    // Any reply (self OR to another account) → fold into the parent's
    // comment thread so the conversation reads whole. For self-replies
    // the parent comes from our own feed; for replies-to-others we fetch
    // the parent (and its ancestor chain) via getPosts so the
    // conversation hangs off whatever root we can find. Failed fetch →
    // top-level + _pending_reattach=1, picked up by the end-of-tick
    // sweep if the parent ever shows up.
    $pending_reattach = false;
    if ( $in_reply && ! $is_repost ) {
        $reply_to_source = 'bluesky:' . $parent_uri;
        $parent_pid = onionpress_bluesky_find_attachable_post( $reply_to_source );
        if ( ! $parent_pid ) {
            $parent_pid = onionpress_bluesky_ensure_imported_with_ancestry(
                $parent_uri, $opts, 1
            );
        }
        if ( $parent_pid ) {
            return onionpress_bluesky_create_reply_comment( $item, $parent_pid );
        }
        $pending_reattach = true;
    }

    // Timestamp: prefer record.createdAt (author's stated time). Reposts
    // use the reason.indexedAt so the post appears in timeline order.
    $ts_src = $is_repost
        ? ( $reason['indexedAt'] ?? $record['createdAt'] ?? '' )
        : ( $record['createdAt'] ?? '' );
    $ts = strtotime( (string) $ts_src );
    if ( ! $ts ) return 'errors';

    list( $content_html, $preview_text ) = onionpress_bluesky_render_content( $item );
    $title = wp_trim_words( $preview_text, 10, '…' );
    if ( $title === '' ) $title = gmdate( 'Y-m-d', $ts ) . ' post';

    // Source URL: link back to the original post on bsky.app.
    $source_url = onionpress_bluesky_at_uri_to_web_url( $post['uri'] ?? '', $post['author']['handle'] ?? '' );

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
            '_is_repost'        => $is_repost ? '1' : '0',
            '_is_reply'         => $in_reply  ? '1' : '0',
            '_reply_to_id'      => $in_reply  ? $parent_uri : '',
            '_thread_root_id'   => $is_self_reply ? '' : (string) ( $post['uri'] ?? '' ),
            '_pending_reattach' => $pending_reattach ? '1' : '0',
            '_is_context'       => $as_context ? '1' : '0',
            '_raw'              => wp_json_encode( $item ),
        ),
    ), true );

    if ( is_wp_error( $post_id ) ) return 'errors';

    if ( function_exists( 'onionpress_social_ensure_category' ) ) {
        $cat_id = onionpress_social_ensure_category( 'bluesky' );
        if ( $cat_id ) {
            wp_set_post_categories( $post_id, array( $cat_id ), false );
        }
    }

    // Tags come from facet#tag features.
    $tag_names = array();
    foreach ( (array) ( $record['facets'] ?? array() ) as $f ) {
        foreach ( (array) ( $f['features'] ?? array() ) as $feat ) {
            if ( ( $feat['$type'] ?? '' ) === 'app.bsky.richtext.facet#tag'
                 && ! empty( $feat['tag'] ) ) {
                $tag_names[] = (string) $feat['tag'];
            }
        }
    }
    if ( $tag_names ) {
        wp_set_post_tags( $post_id, array_unique( $tag_names ), false );
    }

    // Sideload images (top-level only — quoted-post media is
    // intentionally NOT sideloaded).
    $images = onionpress_bluesky_extract_images( $post['embed'] ?? null );
    if ( $images ) {
        onionpress_bluesky_sideload_media( $post_id, $uri, $images );
    }

    return 'imported';
}

/**
 * Turn a Bluesky feed item into (HTML, preview-text).
 *
 * For reposts: header "Reposted @<handle>:" followed by the original's
 * body. For replies: header "Replying to @<handle>:". Facets (links,
 * mentions, tags) are rendered inline. Embeds (images, external cards,
 * quote posts) are appended below the text.
 */
function onionpress_bluesky_render_content( $item ) {
    $post   = $item['post']   ?? array();
    $record = $post['record'] ?? array();
    $reason = $item['reason'] ?? null;

    $parts = array();

    if ( is_array( $reason ) && ( $reason['$type'] ?? '' ) === 'app.bsky.feed.defs#reasonRepost' ) {
        $by_handle = (string) ( $post['author']['handle'] ?? '' );
        $by_url    = $by_handle ? 'https://bsky.app/profile/' . rawurlencode( $by_handle ) : '';
        $parts[] = '<p><em>Reposted'
            . ( $by_url ? ' <a href="' . esc_url( $by_url ) . '" rel="nofollow noopener">@' . esc_html( $by_handle ) . '</a>' : '' )
            . ':</em></p>';
    } elseif ( is_array( $record['reply'] ?? null ) ) {
        $parent_uri = (string) ( $record['reply']['parent']['uri'] ?? '' );
        $parent_handle = onionpress_bluesky_handle_from_at_uri( $parent_uri );
        if ( $parent_handle ) {
            $parts[] = '<p><em>Replying to <a href="' . esc_url( 'https://bsky.app/profile/' . rawurlencode( $parent_handle ) ) . '" rel="nofollow noopener">@' . esc_html( $parent_handle ) . '</a>:</em></p>';
        }
    }

    $text   = (string) ( $record['text']   ?? '' );
    $facets = (array)  ( $record['facets'] ?? array() );
    $body_html = onionpress_bluesky_render_text_with_facets( $text, $facets );
    if ( $body_html !== '' ) {
        $parts[] = '<p>' . $body_html . '</p>';
    }

    $embed_html = onionpress_bluesky_render_embed( $post['embed'] ?? null );
    if ( $embed_html !== '' ) {
        $parts[] = $embed_html;
    }

    $html    = implode( "\n", $parts );
    $preview = trim( html_entity_decode( wp_strip_all_tags( $text ), ENT_QUOTES, 'UTF-8' ) );
    return array( $html, $preview );
}

/**
 * Turn text + facets (link/mention/tag ranges described as byte
 * offsets) into HTML. Bluesky facets use BYTE offsets into the UTF-8
 * text, so we slice with mb_strcut (byte-aware).
 */
function onionpress_bluesky_render_text_with_facets( $text, $facets ) {
    if ( $text === '' ) return '';
    $nl = "\n"; // preserve line breaks as <br>
    if ( empty( $facets ) ) {
        return nl2br( esc_html( $text ), false );
    }
    usort( $facets, function ( $a, $b ) {
        return ( (int) ( $a['index']['byteStart'] ?? 0 ) )
             - ( (int) ( $b['index']['byteStart'] ?? 0 ) );
    } );
    $out = '';
    $pos = 0;
    $len = strlen( $text );
    foreach ( $facets as $f ) {
        $start = (int) ( $f['index']['byteStart'] ?? 0 );
        $end   = (int) ( $f['index']['byteEnd']   ?? 0 );
        if ( $start < $pos || $end <= $start || $end > $len ) continue;
        if ( $start > $pos ) {
            $out .= nl2br( esc_html( substr( $text, $pos, $start - $pos ) ), false );
        }
        $facet_text = substr( $text, $start, $end - $start );
        $feat = $f['features'][0] ?? array();
        $type = (string) ( $feat['$type'] ?? '' );
        if ( $type === 'app.bsky.richtext.facet#link' && ! empty( $feat['uri'] ) ) {
            $out .= '<a href="' . esc_url( $feat['uri'] ) . '" rel="nofollow noopener">' . esc_html( $facet_text ) . '</a>';
        } elseif ( $type === 'app.bsky.richtext.facet#mention' && ! empty( $feat['did'] ) ) {
            $handle = ltrim( $facet_text, '@' );
            $out .= '<a href="https://bsky.app/profile/' . esc_attr( $handle ) . '" rel="nofollow noopener">' . esc_html( $facet_text ) . '</a>';
        } elseif ( $type === 'app.bsky.richtext.facet#tag' && ! empty( $feat['tag'] ) ) {
            $out .= '<a href="https://bsky.app/tag/' . rawurlencode( (string) $feat['tag'] ) . '" rel="nofollow noopener">' . esc_html( $facet_text ) . '</a>';
        } else {
            $out .= esc_html( $facet_text );
        }
        $pos = $end;
    }
    if ( $pos < $len ) {
        $out .= nl2br( esc_html( substr( $text, $pos ) ), false );
    }
    return $out;
}

/**
 * Render a post's `embed` object. Handles:
 *   - images / videos (inline figures)
 *   - external links (title + description card)
 *   - record (quote post) — inline blockquote with quoted author + text,
 *     plus "View on Bluesky" link. Quoted-post MEDIA is intentionally
 *     not sideloaded; we note "[image]" placeholders instead so the
 *     storage footprint stays small and attribution stays clean.
 *   - recordWithMedia — media + quote, rendered as both
 *   - viewNotFound / viewBlocked / viewDetached — muted one-liners
 *
 * Images are rendered as simple <img> tags here; the actual file
 * attachment happens in sideload_media after the post is saved. The
 * <img> tags will be rewritten to local uploads URLs by sideload_media.
 */
function onionpress_bluesky_render_embed( $embed ) {
    if ( ! is_array( $embed ) ) return '';
    $type = (string) ( $embed['$type'] ?? '' );

    if ( $type === 'app.bsky.embed.images#view' ) {
        return onionpress_bluesky_render_images( $embed['images'] ?? array() );
    }
    if ( $type === 'app.bsky.embed.video#view' ) {
        $thumb = (string) ( $embed['thumbnail'] ?? '' );
        if ( $thumb ) {
            return '<figure class="op-bluesky-video"><img src="' . esc_url( $thumb ) . '" alt=""></figure>';
        }
        return '<p><em>[video]</em></p>';
    }
    if ( $type === 'app.bsky.embed.external#view' ) {
        return onionpress_bluesky_render_external( $embed['external'] ?? array() );
    }
    if ( $type === 'app.bsky.embed.record#view' ) {
        return onionpress_bluesky_render_quote( $embed['record'] ?? array() );
    }
    if ( $type === 'app.bsky.embed.recordWithMedia#view' ) {
        $media = onionpress_bluesky_render_embed( $embed['media'] ?? null );
        $quote = onionpress_bluesky_render_quote( $embed['record']['record'] ?? array() );
        return $media . $quote;
    }
    return '';
}

function onionpress_bluesky_render_images( $images ) {
    if ( empty( $images ) ) return '';
    $out = '<div class="op-bluesky-images">';
    foreach ( $images as $img ) {
        $src = (string) ( $img['fullsize'] ?? $img['thumb'] ?? '' );
        $alt = (string) ( $img['alt'] ?? '' );
        if ( $src === '' ) continue;
        $out .= '<figure><img src="' . esc_url( $src ) . '" alt="' . esc_attr( $alt ) . '">';
        if ( $alt !== '' ) {
            $out .= '<figcaption>' . esc_html( $alt ) . '</figcaption>';
        }
        $out .= '</figure>';
    }
    $out .= '</div>';
    return $out;
}

function onionpress_bluesky_render_external( $ext ) {
    $uri   = (string) ( $ext['uri']   ?? '' );
    $title = (string) ( $ext['title'] ?? $uri );
    $desc  = (string) ( $ext['description'] ?? '' );
    if ( $uri === '' ) return '';
    $out = '<div class="op-bluesky-external"><p><a href="' . esc_url( $uri ) . '" rel="nofollow noopener">' . esc_html( $title ) . '</a>';
    if ( $desc !== '' ) {
        $out .= '<br><small>' . esc_html( $desc ) . '</small>';
    }
    $out .= '</p></div>';
    return $out;
}

/**
 * Render a quoted post as an inline blockquote. Preserves the quoted
 * text in the archive even if the quoted post is later deleted/blocked
 * on Bluesky. Quoted-post embeds are rendered as "[image]" placeholders
 * — we don't sideload someone else's media into this archive.
 */
function onionpress_bluesky_render_quote( $record ) {
    if ( ! is_array( $record ) ) return '';
    $rt = (string) ( $record['$type'] ?? '' );
    if ( $rt === 'app.bsky.embed.record#viewNotFound' ) {
        return '<blockquote class="op-bluesky-quote op-bluesky-quote--unavailable"><em>Quoted post deleted</em></blockquote>';
    }
    if ( $rt === 'app.bsky.embed.record#viewBlocked' ) {
        return '<blockquote class="op-bluesky-quote op-bluesky-quote--unavailable"><em>Quoted post unavailable (blocked)</em></blockquote>';
    }
    if ( $rt === 'app.bsky.embed.record#viewDetached' ) {
        return '<blockquote class="op-bluesky-quote op-bluesky-quote--unavailable"><em>Quoted post unavailable (quote removed)</em></blockquote>';
    }
    $author = $record['author'] ?? array();
    $handle = (string) ( $author['handle'] ?? '' );
    $value  = $record['value']  ?? array();
    $text   = (string) ( $value['text']   ?? '' );
    $facets = (array)  ( $value['facets'] ?? array() );
    $quoted_uri = (string) ( $record['uri'] ?? '' );

    $inner = onionpress_bluesky_render_text_with_facets( $text, $facets );
    // Summarize any nested media as placeholders.
    $has_media_note = '';
    foreach ( (array) ( $record['embeds'] ?? array() ) as $nested ) {
        $nt = (string) ( $nested['$type'] ?? '' );
        if ( $nt === 'app.bsky.embed.images#view' ) { $has_media_note = '[image]'; break; }
        if ( $nt === 'app.bsky.embed.video#view' )  { $has_media_note = '[video]'; break; }
    }

    $out  = '<blockquote class="op-bluesky-quote">';
    if ( $handle !== '' ) {
        $out .= '<p class="op-bluesky-quote-author"><a href="' . esc_url( 'https://bsky.app/profile/' . rawurlencode( $handle ) ) . '" rel="nofollow noopener">@' . esc_html( $handle ) . '</a></p>';
    }
    if ( $inner !== '' ) {
        $out .= '<p class="op-bluesky-quote-body">' . $inner . '</p>';
    }
    if ( $has_media_note !== '' ) {
        $out .= '<p class="op-bluesky-quote-media"><em>' . esc_html( $has_media_note ) . '</em></p>';
    }
    $view_url = onionpress_bluesky_at_uri_to_web_url( $quoted_uri, $handle );
    if ( $view_url !== '' ) {
        $out .= '<p class="op-bluesky-quote-link"><a href="' . esc_url( $view_url ) . '" rel="nofollow noopener">View on Bluesky ↗</a></p>';
    }
    $out .= '</blockquote>';
    return $out;
}

/**
 * Translate `at://did:plc:xxx/app.bsky.feed.post/yyy` into the
 * public web URL `https://bsky.app/profile/<handle-or-did>/post/yyy`.
 */
function onionpress_bluesky_at_uri_to_web_url( $at_uri, $handle_hint = '' ) {
    if ( ! preg_match( '~^at://([^/]+)/app\.bsky\.feed\.post/([^/]+)~', $at_uri, $m ) ) {
        return '';
    }
    $actor = $handle_hint !== '' ? $handle_hint : $m[1];
    return 'https://bsky.app/profile/' . rawurlencode( $actor ) . '/post/' . rawurlencode( $m[2] );
}

/**
 * Extract just the actor segment from an at:// URI. We use the DID
 * since the public API always returns DIDs there, not handles.
 */
function onionpress_bluesky_handle_from_at_uri( $at_uri ) {
    // The reply.parent.uri uses DID, not handle. Returning the DID is
    // still a valid bsky.app profile identifier (bsky.app resolves both).
    if ( preg_match( '~^at://([^/]+)~', $at_uri, $m ) ) {
        return $m[1];
    }
    return '';
}

function onionpress_bluesky_extract_images( $embed ) {
    if ( ! is_array( $embed ) ) return array();
    $type = (string) ( $embed['$type'] ?? '' );
    if ( $type === 'app.bsky.embed.images#view' ) {
        return (array) ( $embed['images'] ?? array() );
    }
    if ( $type === 'app.bsky.embed.recordWithMedia#view' ) {
        $media = $embed['media'] ?? null;
        if ( is_array( $media ) && ( $media['$type'] ?? '' ) === 'app.bsky.embed.images#view' ) {
            return (array) ( $media['images'] ?? array() );
        }
    }
    return array();
}

/**
 * Sideload image attachments. Downloads each from Bluesky's CDN over
 * Tor, registers with WP media, and rewrites the <img> tags in the
 * post body to local upload URLs. First image becomes featured.
 */
function onionpress_bluesky_sideload_media( $post_id, $at_uri, $images ) {
    require_once ABSPATH . 'wp-admin/includes/image.php';
    require_once ABSPATH . 'wp-admin/includes/file.php';
    require_once ABSPATH . 'wp-admin/includes/media.php';

    $post = get_post( $post_id );
    if ( ! $post ) return;
    $content = $post->post_content;
    $first_attach_id = 0;

    foreach ( $images as $i => $img ) {
        $src = (string) ( $img['fullsize'] ?? $img['thumb'] ?? '' );
        if ( $src === '' ) continue;

        $ext = 'jpg';
        if ( preg_match( '/\.(jpe?g|png|webp|gif)(\?|$)/i', $src, $m ) ) {
            $ext = strtolower( $m[1] );
            if ( $ext === 'jpeg' ) $ext = 'jpg';
        }
        $tmp = wp_tempnam( 'bsky-' . $i . '.' . $ext );
        if ( ! $tmp ) continue;
        $dl = onionpress_bluesky_fetch_file( $src, $tmp );
        if ( is_wp_error( $dl ) ) continue;

        $file_array = array(
            'name'     => 'bluesky-' . basename( $at_uri ) . '-' . $i . '.' . $ext,
            'tmp_name' => $tmp,
        );
        $attach_id = media_handle_sideload( $file_array, $post_id, (string) ( $img['alt'] ?? '' ) );
        if ( is_wp_error( $attach_id ) ) {
            @unlink( $tmp );
            continue;
        }
        $local_url = wp_get_attachment_url( $attach_id );
        if ( $local_url ) {
            $content = str_replace( esc_url( $src ), esc_url( $local_url ), $content );
        }
        if ( ! $first_attach_id ) $first_attach_id = $attach_id;
    }

    if ( $first_attach_id ) {
        set_post_thumbnail( $post_id, $first_attach_id );
    }
    if ( $content !== $post->post_content ) {
        wp_update_post( array( 'ID' => $post_id, 'post_content' => $content ) );
    }
}

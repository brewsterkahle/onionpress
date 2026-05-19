<?php
/**
 * Plugin Name: OnionPress Social Archive — Blog Importer
 * Description: Pull an external WordPress blog's posts into the unified
 *              social_post archive via the public REST API. Enter a URL,
 *              the importer backfills history and polls every 30 minutes
 *              to stay current — when source posts get edited, the local
 *              mirror updates too. Same admin page accepts a WXR upload
 *              as an archival-fidelity snapshot stored on disk under
 *              ~/OnionPress/Creations/My Creations/blog-archives/.
 * Version:     0.1
 * Network:     true
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

add_action( 'plugins_loaded', function () {
    if ( function_exists( 'onionpress_social_register_importer' ) ) {
        onionpress_social_register_importer( 'blog' );
    }
} );

// --- Option keys --------------------------------------------------------
const ONIONPRESS_BLOG_ADMIN_SLUG       = 'onionpress-social-archive-blog';
const ONIONPRESS_BLOG_URL_OPT          = 'onionpress_social_blog_url';            // canonical https URL the user typed
const ONIONPRESS_BLOG_HOST_OPT         = 'onionpress_social_blog_host';           // parsed host (owner marker)
const ONIONPRESS_BLOG_NEWEST_OPT       = 'onionpress_social_blog_newest_id';      // most-recent imported source post ID
const ONIONPRESS_BLOG_BACKFILL_PAGE_OPT = 'onionpress_social_blog_backfill_page';  // next page to walk backward (1..N, or 'done')
// Owner markers — each cursor is only valid when the stored owner matches
// the current host. Swapping the source URL to a new host implicitly
// invalidates the cursors and forces a fresh walk.
const ONIONPRESS_BLOG_NEWEST_OWNER_OPT   = 'onionpress_social_blog_newest_owner';
const ONIONPRESS_BLOG_BACKFILL_OWNER_OPT = 'onionpress_social_blog_backfill_owner';
const ONIONPRESS_BLOG_LAST_EDITS_PASS_OPT = 'onionpress_social_blog_last_edits_pass'; // unix ts of last edits-pass
const ONIONPRESS_BLOG_TOTAL_POSTS_OPT    = 'onionpress_social_blog_total_posts';    // X-WP-Total reported by source
const ONIONPRESS_BLOG_TOTAL_PAGES_OPT    = 'onionpress_social_blog_total_pages';    // X-WP-TotalPages reported by source
const ONIONPRESS_BLOG_LAST_SYNC          = 'onionpress_social_blog_last_sync';
const ONIONPRESS_BLOG_LAST_NOTE          = 'onionpress_social_blog_last_note';
const ONIONPRESS_BLOG_TICK_LOCK          = 'onionpress_social_blog_tick_lock';      // transient
const ONIONPRESS_BLOG_DAEMON_LOCK        = 'onionpress_social_blog_daemon_lock';    // option
const ONIONPRESS_BLOG_CRON_HOOK          = 'onionpress_social_blog_sync';
const ONIONPRESS_BLOG_CRON_SCHEDULE      = 'onionpress_blog_30min';

// Route everything through the bulk-outgoing Tor daemon. brewster.kahle.org
// is clearnet, but SOCKS5_HOSTNAME resolves clearnet names fine and this
// keeps the project rule "never make direct clearnet requests" intact.
const ONIONPRESS_BLOG_SOCKS_PROXY = 'onionheaven:9050';

// Per-tick budget. Per-page is intentionally small (20) so a single
// tick can normally complete a full page within the 25-second wall-
// clock budget — even when each post sideloads multiple images over
// Tor. The cursor only advances after a page completes, so smaller
// pages = faster cursor progress = quicker backfill. Re-imports are
// idempotent via _source_id, so if a tick can't finish a page the
// next tick re-fetches it and skips the already-imported posts cheaply.
const ONIONPRESS_BLOG_TICK_BUDGET_SEC = 25;
const ONIONPRESS_BLOG_PAGES_PER_TICK  = 8;
const ONIONPRESS_BLOG_PER_PAGE        = 20;

// Daemon outer-loop caps. Same shape as the Mastodon importer — one
// wp-cron fire drives the whole backfill rather than relying on cron
// re-firing soon. Heartbeat lets a crashed daemon be taken over.
const ONIONPRESS_BLOG_DAEMON_MAX_SEC   = 1800; // 30 min per invocation
const ONIONPRESS_BLOG_DAEMON_STALE_SEC = 300;  // heartbeat older than this = dead
const ONIONPRESS_BLOG_DAEMON_IDLE_SEC  = 3;    // politeness pause between ticks
const ONIONPRESS_BLOG_HTTP_TIMEOUT     = 45;

// Edits pass: re-check the source's recently-modified posts every Nth
// tick to mirror edits. At 30-min cron, every 4th tick = once every 2h —
// fast enough that a blog edit shows up the same day, slow enough that
// we're not hammering the source.
const ONIONPRESS_BLOG_EDITS_PASS_INTERVAL_SEC = 2 * HOUR_IN_SECONDS;
const ONIONPRESS_BLOG_EDITS_PASS_PER_PAGE     = 20;

// Forward-catch-up termination: stop scanning page 1 after this many
// consecutive already-known posts in the response. Page 1 holds the
// newest 100 posts; 10 known-in-a-row means we've reached the trailing
// edge of the source's recent activity.
const ONIONPRESS_BLOG_KNOWN_STREAK_STOP = 10;

// --- Admin menu + scheduling -------------------------------------------

add_action( 'admin_menu', function () {
    if ( ! defined( 'ONIONPRESS_SOCIAL_ADMIN_SLUG' ) ) {
        return;
    }
    add_submenu_page(
        ONIONPRESS_SOCIAL_ADMIN_SLUG,
        'Import Blog',
        'Blog',
        'manage_options',
        ONIONPRESS_BLOG_ADMIN_SLUG,
        'onionpress_blog_import_page'
    );
}, 20 );

/**
 * Lazy-schedule the recurring poll. mu-plugins have no activation hook,
 * so we schedule on the first admin hit after a URL is configured.
 */
add_action( 'admin_init', function () {
    if ( ! wp_next_scheduled( ONIONPRESS_BLOG_CRON_HOOK )
         && get_option( ONIONPRESS_BLOG_HOST_OPT ) ) {
        wp_schedule_event( time() + 60, ONIONPRESS_BLOG_CRON_SCHEDULE, ONIONPRESS_BLOG_CRON_HOOK );
    }
} );

add_filter( 'cron_schedules', function ( $schedules ) {
    if ( ! isset( $schedules[ ONIONPRESS_BLOG_CRON_SCHEDULE ] ) ) {
        $schedules[ ONIONPRESS_BLOG_CRON_SCHEDULE ] = array(
            'interval' => 30 * MINUTE_IN_SECONDS,
            'display'  => '30 min (OnionPress Blog poll)',
        );
    }
    return $schedules;
} );

add_action( ONIONPRESS_BLOG_CRON_HOOK, 'onionpress_blog_run_sync_tick' );

// Admin-authenticated streaming download endpoint for WXR snapshots.
// Uses admin-post.php so it's gated by WP admin auth + nonce.
add_action( 'admin_post_onionpress_blog_download_wxr', 'onionpress_blog_serve_wxr_download' );

// --- Owner-aware cursor accessors --------------------------------------

/**
 * Cursors are valid only when their stored owner matches the current
 * host. Swapping the source URL implicitly invalidates them. Mirrors
 * the Mastodon importer's account-ID rotation defense.
 */
function onionpress_blog_get_newest_for( $host ) {
    $owner = (string) get_option( ONIONPRESS_BLOG_NEWEST_OWNER_OPT, '' );
    if ( $owner !== '' && $owner !== $host ) {
        return '';
    }
    return (string) get_option( ONIONPRESS_BLOG_NEWEST_OPT, '' );
}
function onionpress_blog_set_newest_for( $host, $value ) {
    update_option( ONIONPRESS_BLOG_NEWEST_OPT, (string) $value );
    update_option( ONIONPRESS_BLOG_NEWEST_OWNER_OPT, $host );
}
function onionpress_blog_get_backfill_page_for( $host ) {
    $owner = (string) get_option( ONIONPRESS_BLOG_BACKFILL_OWNER_OPT, '' );
    if ( $owner !== '' && $owner !== $host ) {
        return 1; // fresh walk for the new host
    }
    $raw = get_option( ONIONPRESS_BLOG_BACKFILL_PAGE_OPT, 1 );
    return $raw === 'done' ? 'done' : max( 1, (int) $raw );
}
function onionpress_blog_set_backfill_page_for( $host, $value ) {
    update_option( ONIONPRESS_BLOG_BACKFILL_PAGE_OPT, $value );
    update_option( ONIONPRESS_BLOG_BACKFILL_OWNER_OPT, $host );
}
function onionpress_blog_clear_cursors() {
    delete_option( ONIONPRESS_BLOG_NEWEST_OPT );
    delete_option( ONIONPRESS_BLOG_NEWEST_OWNER_OPT );
    delete_option( ONIONPRESS_BLOG_BACKFILL_PAGE_OPT );
    delete_option( ONIONPRESS_BLOG_BACKFILL_OWNER_OPT );
    delete_option( ONIONPRESS_BLOG_TOTAL_POSTS_OPT );
    delete_option( ONIONPRESS_BLOG_TOTAL_PAGES_OPT );
}

// --- URL helpers --------------------------------------------------------

/**
 * Normalize a user-entered URL to {host, base_url}. Returns null on
 * inputs that don't look like an http(s) URL.
 */
function onionpress_blog_parse_url( $raw ) {
    $raw = trim( (string) $raw );
    if ( $raw === '' ) return null;
    if ( ! preg_match( '~^https?://~i', $raw ) ) {
        $raw = 'https://' . $raw;
    }
    $parts = wp_parse_url( $raw );
    if ( ! is_array( $parts ) || empty( $parts['host'] ) ) {
        return null;
    }
    $host = strtolower( $parts['host'] );
    $scheme = isset( $parts['scheme'] ) ? strtolower( $parts['scheme'] ) : 'https';
    $path = isset( $parts['path'] ) ? rtrim( $parts['path'], '/' ) : '';
    $base = $scheme . '://' . $host . $path;
    return array( 'host' => $host, 'base_url' => $base );
}

/**
 * Build a REST API URL for the configured source. $path begins with
 * '/wp/v2/...', $params is a query-arg array.
 */
function onionpress_blog_rest_url( $path, $params = array() ) {
    $base = (string) get_option( ONIONPRESS_BLOG_URL_OPT, '' );
    if ( $base === '' ) return '';
    $url = rtrim( $base, '/' ) . '/wp-json' . $path;
    if ( ! empty( $params ) ) {
        $url .= ( strpos( $url, '?' ) === false ? '?' : '&' ) . http_build_query( $params );
    }
    return $url;
}

/**
 * Count posts whose _source_id starts with "blog:<host>:". Used by the
 * admin progress meter so a host switch doesn't show "172 of 50" by
 * counting prior-host imports against the new host's reported total.
 */
function onionpress_blog_imported_count_for_host( $host ) {
    if ( $host === '' ) return 0;
    global $wpdb;
    $prefix = 'blog:' . $host . ':';
    $like   = $wpdb->esc_like( $prefix ) . '%';
    return (int) $wpdb->get_var( $wpdb->prepare(
        "SELECT COUNT(DISTINCT p.ID)
         FROM {$wpdb->posts} p
         JOIN {$wpdb->postmeta} m ON m.post_id = p.ID AND m.meta_key = '_source_id'
         WHERE p.post_status = 'publish'
           AND m.meta_value LIKE %s",
        $like
    ) );
}

/**
 * Count blog-imported posts that are NOT from the current host —
 * surfaces a notice on the admin page so the user knows the
 * /category/blog/ index is multi-source.
 */
function onionpress_blog_imported_count_other_hosts( $current_host ) {
    global $wpdb;
    $not_prefix = 'blog:' . $current_host . ':';
    $not_like   = $wpdb->esc_like( $not_prefix ) . '%';
    return (int) $wpdb->get_var( $wpdb->prepare(
        "SELECT COUNT(DISTINCT p.ID)
         FROM {$wpdb->posts} p
         JOIN {$wpdb->postmeta} m ON m.post_id = p.ID AND m.meta_key = '_source_id'
         WHERE p.post_status = 'publish'
           AND m.meta_value LIKE 'blog:%'
           AND m.meta_value NOT LIKE %s",
        $not_like
    ) );
}

// --- Admin page render --------------------------------------------------

function onionpress_blog_import_page() {
    if ( ! current_user_can( 'manage_options' ) ) {
        wp_die( 'Unauthorized' );
    }

    $notice = null;
    if ( $_SERVER['REQUEST_METHOD'] === 'POST' ) {
        if ( isset( $_POST['onionpress_blog_save_url'] ) ) {
            check_admin_referer( 'onionpress_blog_save_url', 'onionpress_blog_url_nonce' );
            $notice = onionpress_blog_handle_save_url_post();
        } elseif ( isset( $_POST['onionpress_blog_sync_now'] ) ) {
            check_admin_referer( 'onionpress_blog_sync_now', 'onionpress_blog_sync_nonce' );
            delete_option( ONIONPRESS_BLOG_DAEMON_LOCK );
            delete_transient( 'doing_cron' );
            wp_schedule_single_event( time(), ONIONPRESS_BLOG_CRON_HOOK );
            $cron_url = site_url( 'wp-cron.php?doing_wp_cron=' . microtime( true ) );
            wp_remote_post( $cron_url, array( 'timeout' => 0.01, 'blocking' => false, 'sslverify' => false ) );
            $notice = array( 'level' => 'success', 'message' => 'Sync started. Progress below — this page will refresh automatically.' );
        } elseif ( isset( $_POST['onionpress_blog_reset'] ) ) {
            check_admin_referer( 'onionpress_blog_reset', 'onionpress_blog_reset_nonce' );
            $notice = onionpress_blog_reset_cursors();
        } elseif ( isset( $_POST['onionpress_blog_upload_wxr'] ) ) {
            check_admin_referer( 'onionpress_blog_upload_wxr', 'onionpress_blog_wxr_nonce' );
            $notice = onionpress_blog_handle_wxr_upload();
        } elseif ( isset( $_POST['onionpress_blog_delete_wxr'] ) ) {
            check_admin_referer( 'onionpress_blog_delete_wxr', 'onionpress_blog_wxr_delete_nonce' );
            $notice = onionpress_blog_handle_wxr_delete();
        }
    }

    $url           = (string) get_option( ONIONPRESS_BLOG_URL_OPT, '' );
    $host          = (string) get_option( ONIONPRESS_BLOG_HOST_OPT, '' );
    // Count posts imported from the CURRENT host only, not the whole
    // /category/blog/. Old hosts' posts stay in place after a URL
    // switch but don't count toward the new host's X-of-Y total.
    $imported_now  = $host !== '' ? onionpress_blog_imported_count_for_host( $host ) : 0;
    $total_other   = $host !== '' ? onionpress_blog_imported_count_other_hosts( $host ) : 0;
    $last_sync     = (int) get_option( ONIONPRESS_BLOG_LAST_SYNC, 0 );
    $last_note     = (string) get_option( ONIONPRESS_BLOG_LAST_NOTE, '' );
    $backfill_page = onionpress_blog_get_backfill_page_for( $host );
    $backfill_done = ( $host !== '' && $backfill_page === 'done' );
    $total_posts   = (int) get_option( ONIONPRESS_BLOG_TOTAL_POSTS_OPT, 0 );
    $total_pages   = (int) get_option( ONIONPRESS_BLOG_TOTAL_PAGES_OPT, 0 );

    $dlock_raw  = (string) get_option( ONIONPRESS_BLOG_DAEMON_LOCK, '' );
    $dlock_ts   = 0;
    if ( strpos( $dlock_raw, ':' ) !== false ) {
        list( , $dlock_ts_str ) = explode( ':', $dlock_raw, 2 );
        $dlock_ts = (int) $dlock_ts_str;
    }
    $dlock_age    = $dlock_ts > 0 ? time() - $dlock_ts : 0;
    $daemon_alive = $dlock_ts > 0 && $dlock_age < ONIONPRESS_BLOG_DAEMON_STALE_SEC;
    ?>
    <div class="wrap">
        <?php if ( $daemon_alive ) : ?>
            <meta http-equiv="refresh" content="15">
        <?php endif; ?>
        <h1>Import Blog</h1>
        <p>Pull posts from an external WordPress blog into your archive. Posts
           land under <code>/category/blog/</code> and mingle with your other
           imported social posts. Re-importing is safe — already-imported
           posts are skipped. Edits on the source are mirrored within ~2 hours.</p>

        <?php if ( $notice ) : ?>
            <div class="notice notice-<?php echo esc_attr( $notice['level'] ); ?>">
                <p><?php echo wp_kses_post( $notice['message'] ); ?></p>
            </div>
        <?php endif; ?>

        <h2>Source blog URL</h2>
        <p>Enter the URL of the WordPress blog to import. The blog must have a
           publicly-readable REST API (most WordPress sites do by default).</p>
        <script>
        function op_blog_confirm_switch( form ) {
            var raw    = ( form.blog_url.value || '' ).trim();
            var curHost   = form.dataset.curHost   || '';
            var curCount  = parseInt( form.dataset.curCount, 10 ) || 0;
            if ( ! curHost || curCount <= 0 ) return true;
            var newHost = '';
            try {
                var u = /^https?:\/\//i.test( raw ) ? raw : 'https://' + raw;
                newHost = new URL( u ).hostname.toLowerCase();
            } catch ( e ) { return true; }
            if ( newHost === '' || newHost === curHost ) return true;
            return confirm(
                'Switch source from ' + curHost + ' to ' + newHost + '?\n\n' +
                'The ' + curCount + ' post' + ( curCount === 1 ? '' : 's' ) +
                ' already imported from ' + curHost + ' will stay in /category/blog/ ' +
                '(they aren\'t deleted) and will mingle chronologically with new posts from ' +
                newHost + '. Backfill cursors reset; the new blog walks from scratch.'
            );
        }
        </script>
        <form method="post" style="margin-bottom:1.25em;"
              data-cur-host="<?php echo esc_attr( $host ); ?>"
              data-cur-count="<?php echo esc_attr( (string) $imported_now ); ?>"
              onsubmit="return op_blog_confirm_switch( this );">
            <?php wp_nonce_field( 'onionpress_blog_save_url', 'onionpress_blog_url_nonce' ); ?>
            <input type="hidden" name="onionpress_blog_save_url" value="1">
            <table class="form-table" role="presentation">
                <tr>
                    <th><label for="blog_url">Blog URL</label></th>
                    <td>
                        <input type="url" id="blog_url" name="blog_url"
                               value="<?php echo esc_attr( $url ); ?>"
                               placeholder="https://example.com/"
                               class="regular-text" style="max-width:480px;">
                        <p class="description">Examples: <code>https://brewster.kahle.org/</code>, <code>https://blog.example.com/</code>.
                           Changing the URL to a different host resets the backfill cursors.</p>
                        <?php submit_button( $host ? 'Save changes' : 'Start archiving', 'primary', 'submit', false ); ?>
                    </td>
                </tr>
            </table>
        </form>

        <?php if ( $host && $total_other > 0 ) : ?>
            <div class="notice notice-info inline">
                <p><strong>Heads up:</strong>
                    <?php echo number_format_i18n( $total_other ); ?> post<?php echo $total_other === 1 ? '' : 's'; ?>
                    from <em>other</em> blog source<?php echo $total_other === 1 ? '' : 's'; ?> remain in
                    <code>/category/blog/</code>. They stay where they are — switching the source URL doesn't delete prior imports.
                    Progress below counts only the current host's posts.</p>
            </div>
        <?php endif; ?>

        <?php if ( $host ) : ?>
            <h2>Sync</h2>
            <table class="wp-list-table widefat" style="max-width:620px;">
                <tbody>
                    <tr><th style="width:200px;">Source</th>
                        <td><code><?php echo esc_html( $url ); ?></code></td></tr>
                    <tr><th>Progress</th>
                        <td><?php
                            if ( $total_posts > 0 ) {
                                $pct = min( 100, (int) round( 100 * $imported_now / max( 1, $total_posts ) ) );
                                printf(
                                    '<strong>%s of %s posts</strong> imported (%d%%)',
                                    number_format_i18n( $imported_now ),
                                    number_format_i18n( $total_posts ),
                                    $pct
                                );
                                echo '<br><progress value="' . esc_attr( (string) $imported_now ) . '" max="' . esc_attr( (string) $total_posts ) . '" style="width:340px;height:14px;"></progress>';
                            } else {
                                printf( '<strong>%s</strong> posts imported (source total unknown until first sync)', number_format_i18n( $imported_now ) );
                            }
                        ?></td></tr>
                    <tr><th>Backfill</th><td><?php
                        if ( $daemon_alive ) {
                            $age = max( 0, $dlock_age );
                            echo '<strong style="color:#2a7b2a;">● Running</strong> — last heartbeat ' . esc_html( $age ) . 's ago. This page auto-refreshes every 15s while syncing.';
                        } elseif ( $backfill_done ) {
                            echo '<strong style="color:#2a7b2a;">✓ complete</strong> — polling every 30 min for new posts and edits.';
                        } else {
                            $page_part = '';
                            if ( $total_pages > 0 && is_numeric( $backfill_page ) ) {
                                $page_part = sprintf( ' (page %d of %d)', (int) $backfill_page, $total_pages );
                            } elseif ( is_numeric( $backfill_page ) ) {
                                $page_part = sprintf( ' (at page %d)', (int) $backfill_page );
                            }
                            echo '<em>paused' . esc_html( $page_part ) . ' — click "Sync now" to start or resume.</em>';
                        }
                    ?></td></tr>
                    <tr><th>Last sync</th><td><?php echo $last_sync ? esc_html( human_time_diff( $last_sync ) ) . ' ago' : '&mdash;'; ?>
                        <?php if ( $last_note ) : ?><br><small style="color:#666;"><?php echo esc_html( $last_note ); ?></small><?php endif; ?></td></tr>
                </tbody>
            </table>

            <form method="post" style="margin-top:1em;display:inline-block;">
                <?php wp_nonce_field( 'onionpress_blog_sync_now', 'onionpress_blog_sync_nonce' ); ?>
                <input type="hidden" name="onionpress_blog_sync_now" value="1">
                <?php submit_button( 'Sync now', 'primary', 'submit', false ); ?>
            </form>
            <form method="post" style="margin-top:1em;display:inline-block;margin-left:0.5em;"
                  onsubmit="return confirm('This clears the sync cursors (not the imported posts). Next sync will rescan from scratch but skip everything already imported. Continue?');">
                <?php wp_nonce_field( 'onionpress_blog_reset', 'onionpress_blog_reset_nonce' ); ?>
                <input type="hidden" name="onionpress_blog_reset" value="1">
                <?php submit_button( 'Reset cursors', 'secondary', 'submit', false ); ?>
            </form>
        <?php endif; ?>

        <h2 style="margin-top:2em;">Archival snapshot (WXR upload)</h2>
        <p>The REST import above produces a normalized <em>reading copy</em>.
           For lossless archival fidelity, export a full WXR from the source
           blog's <code>wp-admin → Tools → Export</code> and upload it here.
           Snapshots are stored on disk under
           <code>~/OnionPress/Creations/My Creations/blog-archives/&lt;host&gt;/</code>
           where they survive across reinstalls. <strong>Note:</strong> these files
           are <em>not</em> included in the OnionPress backup tarball — bring
           <code>~/OnionPress/</code> along when migrating machines.</p>
        <form method="post" enctype="multipart/form-data" style="margin-bottom:1.25em;">
            <?php wp_nonce_field( 'onionpress_blog_upload_wxr', 'onionpress_blog_wxr_nonce' ); ?>
            <input type="hidden" name="onionpress_blog_upload_wxr" value="1">
            <table class="form-table" role="presentation">
                <tr>
                    <th><label for="blog_wxr">WXR file</label></th>
                    <td>
                        <input type="file" id="blog_wxr" name="blog_wxr" accept=".xml,.xml.gz" required>
                        <p class="description">Plain <code>.xml</code> or pre-gzipped <code>.xml.gz</code>. Stored as gzipped on disk regardless.</p>
                    </td>
                </tr>
            </table>
            <?php submit_button( 'Upload snapshot' ); ?>
        </form>

        <?php onionpress_blog_render_snapshots(); ?>

        <?php if ( $host ) : ?>
            <h2 style="margin-top:2em;">Recent imports</h2>
            <?php onionpress_blog_render_recent(); ?>
        <?php endif; ?>

        <h2 style="margin-top:2em;">Known limitations</h2>
        <ul style="list-style:disc;padding-left:1.5em;">
            <li>Only publicly-readable REST data is imported. Private, draft, and password-protected posts are not (use the WXR snapshot above for those).</li>
            <li>Shortcodes from the source site appear as raw <code>[shortcode]</code> text if the corresponding plugin isn't installed locally.</li>
            <li>Posts deleted from the source remain in the local archive.</li>
            <li>Local edits to imported posts are overwritten when the source post is edited (re-pull behavior).</li>
        </ul>
    </div>
    <?php
}

// --- Save / reset handlers ---------------------------------------------

function onionpress_blog_handle_save_url_post() {
    $raw    = isset( $_POST['blog_url'] ) ? wp_unslash( $_POST['blog_url'] ) : '';
    $parsed = onionpress_blog_parse_url( $raw );
    if ( ! $parsed ) {
        return array( 'level' => 'error', 'message' => 'Enter a URL like <code>https://brewster.kahle.org/</code>.' );
    }
    $host     = $parsed['host'];
    $base_url = $parsed['base_url'];

    // Probe the REST API root to catch typos and disabled-REST sites early.
    $probe = onionpress_blog_api_get( $base_url . '/wp-json/' );
    if ( is_wp_error( $probe ) ) {
        return array( 'level' => 'error', 'message' => 'Could not reach <code>' . esc_html( $host ) . '</code> over Tor: ' . esc_html( $probe->get_error_message() ) );
    }
    if ( (int) $probe['code'] !== 200 || empty( $probe['json']['namespaces'] ) ) {
        return array(
            'level'   => 'error',
            'message' => 'Server returned HTTP ' . intval( $probe['code'] ) . ' for <code>' . esc_html( $base_url ) . '/wp-json/</code>. Confirm the URL points at a WordPress site with the REST API enabled.',
        );
    }
    if ( ! in_array( 'wp/v2', (array) $probe['json']['namespaces'], true ) ) {
        return array(
            'level'   => 'error',
            'message' => 'The site responded to <code>/wp-json/</code> but does not expose the <code>wp/v2</code> namespace. This importer needs the WP core REST API.',
        );
    }

    $prev_host = (string) get_option( ONIONPRESS_BLOG_HOST_OPT, '' );
    if ( $prev_host !== '' && $prev_host !== $host ) {
        onionpress_blog_clear_cursors();
    }

    update_option( ONIONPRESS_BLOG_URL_OPT,  $base_url );
    update_option( ONIONPRESS_BLOG_HOST_OPT, $host );

    if ( ! wp_next_scheduled( ONIONPRESS_BLOG_CRON_HOOK ) ) {
        wp_schedule_event( time() + 30, ONIONPRESS_BLOG_CRON_SCHEDULE, ONIONPRESS_BLOG_CRON_HOOK );
    }

    // Kick the daemon right now so "Start archiving" actually starts —
    // not wait for the 30-min cron fire. Same trick as Sync now: clear
    // the lock, schedule an immediate single event, fire off a non-
    // blocking loopback POST to wp-cron.php to make WP actually run it.
    delete_option( ONIONPRESS_BLOG_DAEMON_LOCK );
    delete_transient( 'doing_cron' );
    wp_schedule_single_event( time(), ONIONPRESS_BLOG_CRON_HOOK );
    $cron_url = site_url( 'wp-cron.php?doing_wp_cron=' . microtime( true ) );
    wp_remote_post( $cron_url, array( 'timeout' => 0.01, 'blocking' => false, 'sslverify' => false ) );

    return array(
        'level'   => 'success',
        'message' => 'Saved <code>' . esc_html( $base_url ) . '</code>. Backfill started — progress shown below, this page auto-refreshes while syncing.',
    );
}

function onionpress_blog_reset_cursors() {
    onionpress_blog_clear_cursors();
    return array( 'level' => 'success', 'message' => 'Cursors cleared. Next sync will rescan from scratch (but will skip already-imported posts).' );
}

// --- Daemon outer loop --------------------------------------------------

/**
 * Cron + admin entry point. Daemon-style: one fire drives the whole
 * backfill to completion. Token-based mutex with heartbeat means a
 * crashed daemon gets taken over by the next cron fire.
 */
function onionpress_blog_run_sync_tick( $from_admin = false ) {
    $url  = (string) get_option( ONIONPRESS_BLOG_URL_OPT, '' );
    $host = (string) get_option( ONIONPRESS_BLOG_HOST_OPT, '' );
    if ( $url === '' || $host === '' ) {
        return array( 'level' => 'error', 'message' => 'Enter the source blog URL first.' );
    }

    $now = time();
    $raw = (string) get_option( ONIONPRESS_BLOG_DAEMON_LOCK, '' );
    if ( $raw !== '' && strpos( $raw, ':' ) !== false ) {
        list( , $ts_str ) = explode( ':', $raw, 2 );
        $lock_ts = (int) $ts_str;
        if ( $lock_ts > 0 && ( $now - $lock_ts ) < ONIONPRESS_BLOG_DAEMON_STALE_SEC ) {
            if ( $from_admin ) {
                return array( 'level' => 'warning', 'message' => 'Another sync is already running. Try again in a few minutes.' );
            }
            return; // cron path
        }
    }
    $token = function_exists( 'wp_generate_password' ) ? wp_generate_password( 16, false, false ) : bin2hex( random_bytes( 8 ) );
    update_option( ONIONPRESS_BLOG_DAEMON_LOCK, $token . ':' . $now, false );

    @set_time_limit( ONIONPRESS_BLOG_DAEMON_MAX_SEC + 60 );
    @ignore_user_abort( true );

    $loop_deadline = microtime( true ) + ONIONPRESS_BLOG_DAEMON_MAX_SEC;
    $total = array( 'imported' => 0, 'skipped' => 0, 'updated' => 0, 'errors' => 0, 'pages' => 0 );
    $last_note = '';

    try {
        while ( microtime( true ) < $loop_deadline ) {
            $cur = (string) get_option( ONIONPRESS_BLOG_DAEMON_LOCK, '' );
            if ( strpos( $cur, $token . ':' ) !== 0 ) {
                break;
            }
            update_option( ONIONPRESS_BLOG_DAEMON_LOCK, $token . ':' . time(), false );

            $result = onionpress_blog_sync_one_tick( $host );
            foreach ( array( 'imported', 'skipped', 'updated', 'errors', 'pages' ) as $k ) {
                $total[ $k ] += (int) ( $result['stats'][ $k ] ?? 0 );
            }
            $last_note = $result['note'];

            if ( $result['done'] || ! empty( $result['errors'] ) ) {
                break;
            }
            sleep( ONIONPRESS_BLOG_DAEMON_IDLE_SEC );
        }
    } finally {
        $cur = (string) get_option( ONIONPRESS_BLOG_DAEMON_LOCK, '' );
        if ( strpos( $cur, $token . ':' ) === 0 ) {
            delete_option( ONIONPRESS_BLOG_DAEMON_LOCK );
        }
    }

    $summary = sprintf(
        '%d imported, %d updated, %d skipped, %d errors across %d pages (daemon run)',
        $total['imported'], $total['updated'], $total['skipped'], $total['errors'], $total['pages']
    );
    update_option( ONIONPRESS_BLOG_LAST_NOTE, $summary );

    if ( ! $from_admin ) return;
    $level = ( $total['errors'] > 0 && $total['imported'] === 0 && $total['updated'] === 0 ) ? 'error' : 'success';
    return array( 'level' => $level, 'message' => 'Sync: ' . esc_html( $summary ) );
}

// --- One tick: forward catch-up + backward backfill + edits pass -------

function onionpress_blog_sync_one_tick( $host ) {
    $lock = get_transient( ONIONPRESS_BLOG_TICK_LOCK );
    if ( $lock ) {
        return array(
            'stats'  => array(),
            'errors' => array( 'tick mutex held' ),
            'note'   => 'tick mutex held',
            'done'   => false,
        );
    }
    set_transient( ONIONPRESS_BLOG_TICK_LOCK, time(), 10 * MINUTE_IN_SECONDS );

    @set_time_limit( ONIONPRESS_BLOG_TICK_BUDGET_SEC + 30 );
    $deadline = microtime( true ) + ONIONPRESS_BLOG_TICK_BUDGET_SEC;

    $stats  = array( 'imported' => 0, 'skipped' => 0, 'updated' => 0, 'errors' => 0, 'pages' => 0 );
    $errors = array();

    try {
        $backfill_page = onionpress_blog_get_backfill_page_for( $host );

        // Two phases, mutually exclusive within a tick:
        //   - Backfill phase: backfill_page !== 'done'. Walk pages
        //     forward from backfill_page (initially 1) and advance the
        //     cursor as each page completes. Naturally covers page 1
        //     on the very first tick, so no separate "forward catch-up"
        //     is needed yet — we're still walking history.
        //   - Catch-up phase: backfill is done. Each tick re-checks
        //     page 1 to pick up newly-published posts, terminating
        //     early on a known-streak. Already-imported posts return
        //     'skipped' so this is cheap when nothing's new.
        if ( $backfill_page !== 'done' ) {
            $no_progress_rounds = 0;
            while ( $stats['pages'] < ONIONPRESS_BLOG_PAGES_PER_TICK
                    && microtime( true ) < $deadline ) {
                $page = onionpress_blog_fetch_posts( (int) $backfill_page );
                $stats['pages']++;
                if ( is_wp_error( $page ) ) {
                    $msg = $page->get_error_message();
                    if ( strpos( $msg, 'invalid_page_number' ) !== false ) {
                        onionpress_blog_set_backfill_page_for( $host, 'done' );
                        break;
                    }
                    $errors[] = $msg;
                    break;
                }
                if ( ! is_array( $page ) || empty( $page ) ) {
                    onionpress_blog_set_backfill_page_for( $host, 'done' );
                    break;
                }
                // Bookmark the newest source ID we've ever seen so the
                // catch-up phase can compare against it.
                if ( $backfill_page === 1 && isset( $page[0]['id'] ) ) {
                    onionpress_blog_set_newest_for( $host, (string) $page[0]['id'] );
                }
                $page_completed   = true;
                $imported_in_page = 0;
                $skipped_in_page  = 0;
                foreach ( $page as $post ) {
                    if ( microtime( true ) >= $deadline ) {
                        $page_completed = false;
                        break;
                    }
                    $r = onionpress_blog_import_post( $post );
                    $stats[ $r ] = ( $stats[ $r ] ?? 0 ) + 1;
                    if ( $r === 'imported' ) $imported_in_page++;
                    if ( $r === 'skipped' )  $skipped_in_page++;
                }
                // Only advance the page cursor when we walked the whole
                // page. Otherwise the next tick re-fetches the same
                // page and picks up where we left off (idempotent —
                // already-imported posts skip cheaply).
                if ( $page_completed ) {
                    $backfill_page = (int) $backfill_page + 1;
                    onionpress_blog_set_backfill_page_for( $host, $backfill_page );
                } else {
                    break; // out of time
                }
                if ( $imported_in_page === 0 && $skipped_in_page === 0 ) {
                    $no_progress_rounds++;
                    if ( $no_progress_rounds >= 3 ) break;
                } else {
                    $no_progress_rounds = 0;
                }
                if ( count( $page ) < ONIONPRESS_BLOG_PER_PAGE ) {
                    onionpress_blog_set_backfill_page_for( $host, 'done' );
                    break;
                }
            }
        } else {
            // Catch-up phase: re-check page 1 for newly-published posts.
            // Stop on a known-streak so we don't waste time iterating
            // already-imported posts when nothing changed.
            if ( $stats['pages'] < ONIONPRESS_BLOG_PAGES_PER_TICK && microtime( true ) < $deadline ) {
                $page = onionpress_blog_fetch_posts( 1 );
                $stats['pages']++;
                if ( is_wp_error( $page ) ) {
                    $errors[] = $page->get_error_message();
                } elseif ( is_array( $page ) ) {
                    $known_streak = 0;
                    foreach ( $page as $post ) {
                        if ( microtime( true ) >= $deadline ) break;
                        $r = onionpress_blog_import_post( $post );
                        $stats[ $r ] = ( $stats[ $r ] ?? 0 ) + 1;
                        if ( $r === 'skipped' ) {
                            $known_streak++;
                            if ( $known_streak >= ONIONPRESS_BLOG_KNOWN_STREAK_STOP ) break;
                        } else {
                            $known_streak = 0;
                        }
                    }
                    if ( ! empty( $page ) && isset( $page[0]['id'] ) ) {
                        onionpress_blog_set_newest_for( $host, (string) $page[0]['id'] );
                    }
                }
            }
        }

        // --- Edits pass: re-check recently-modified posts ---------------
        if ( microtime( true ) < $deadline ) {
            $last_pass = (int) get_option( ONIONPRESS_BLOG_LAST_EDITS_PASS_OPT, 0 );
            if ( time() - $last_pass >= ONIONPRESS_BLOG_EDITS_PASS_INTERVAL_SEC ) {
                $updated_count = onionpress_blog_run_edits_pass( $host, $deadline );
                $stats['updated'] += $updated_count;
                update_option( ONIONPRESS_BLOG_LAST_EDITS_PASS_OPT, time() );
            }
        }
    } finally {
        delete_transient( ONIONPRESS_BLOG_TICK_LOCK );
    }

    update_option( ONIONPRESS_BLOG_LAST_SYNC, time() );
    $note = sprintf(
        '%d imported, %d updated, %d skipped, %d errors across %d pages',
        intval( $stats['imported'] ?? 0 ),
        intval( $stats['updated']  ?? 0 ),
        intval( $stats['skipped']  ?? 0 ),
        intval( $stats['errors']   ?? 0 ),
        intval( $stats['pages']    ?? 0 )
    );
    if ( $errors ) { $note .= ' — last error: ' . $errors[ count( $errors ) - 1 ]; }

    $done = ( onionpress_blog_get_backfill_page_for( $host ) === 'done' );
    return array(
        'stats'  => $stats,
        'errors' => $errors,
        'note'   => $note,
        'done'   => $done,
    );
}

// --- REST API fetch helpers --------------------------------------------

/**
 * Fetch one page of /wp/v2/posts from the configured source. Returns the
 * parsed array of post objects, WP_Error on transport failure, or
 * WP_Error with 'invalid_page_number' in the message when paged past
 * the end of history.
 */
function onionpress_blog_fetch_posts( $page_num ) {
    $url = onionpress_blog_rest_url( '/wp/v2/posts', array(
        'per_page' => ONIONPRESS_BLOG_PER_PAGE,
        'page'     => max( 1, (int) $page_num ),
        'orderby'  => 'date',
        'order'    => 'desc',
        '_embed'   => 'true',
    ) );
    if ( $url === '' ) return new WP_Error( 'no_source', 'no source URL configured' );

    // Test hook so integration tests can inject canned pages.
    $mock = apply_filters( 'onionpress_blog_fetch_posts_mock', null, $page_num );
    if ( $mock !== null ) return $mock;

    $r = onionpress_blog_api_get( $url );
    if ( is_wp_error( $r ) ) return $r;
    if ( (int) $r['code'] === 400 ) {
        // Inspect body for the standard WP error code.
        $body = (string) ( $r['body'] ?? '' );
        if ( strpos( $body, 'rest_post_invalid_page_number' ) !== false ) {
            return new WP_Error( 'blog_rest', 'rest_post_invalid_page_number' );
        }
    }
    if ( (int) $r['code'] !== 200 ) {
        return new WP_Error( 'blog_http', 'HTTP ' . $r['code'] . ' from ' . onionpress_blog_rest_url( '/wp/v2/posts' ) );
    }
    // Refresh the source-reported totals from response headers. WP REST
    // sets X-WP-Total + X-WP-TotalPages on every paginated response, so
    // we get a free progress denominator without an extra HEAD request.
    if ( isset( $r['headers']['x-wp-total'] ) ) {
        update_option( ONIONPRESS_BLOG_TOTAL_POSTS_OPT, (int) $r['headers']['x-wp-total'] );
    }
    if ( isset( $r['headers']['x-wp-totalpages'] ) ) {
        update_option( ONIONPRESS_BLOG_TOTAL_PAGES_OPT, (int) $r['headers']['x-wp-totalpages'] );
    }
    return is_array( $r['json'] ) ? $r['json'] : array();
}

/**
 * Tor-over-SOCKS HTTP GET via the onionheaven daemon. Same shape as the
 * Mastodon importer's helper: ['code', 'body', 'json', 'headers'] on
 * success, WP_Error on transport failure. Response headers are returned
 * as a lower-cased name => value array so callers can read
 * X-WP-Total / X-WP-TotalPages on REST endpoints.
 */
function onionpress_blog_api_get( $url ) {
    if ( ! function_exists( 'curl_init' ) ) {
        return new WP_Error( 'no_curl', 'curl extension required for blog import' );
    }
    $headers = array();
    $ch = curl_init( $url );
    curl_setopt_array( $ch, array(
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_FOLLOWLOCATION => true,
        CURLOPT_MAXREDIRS      => 5,
        CURLOPT_PROXY          => ONIONPRESS_BLOG_SOCKS_PROXY,
        CURLOPT_PROXYTYPE      => CURLPROXY_SOCKS5_HOSTNAME,
        CURLOPT_TIMEOUT        => ONIONPRESS_BLOG_HTTP_TIMEOUT,
        CURLOPT_CONNECTTIMEOUT => 30,
        CURLOPT_USERAGENT      => 'OnionPress/SocialArchive (+https://onionpress.org)',
        CURLOPT_HTTPHEADER     => array( 'Accept: application/json' ),
        CURLOPT_HEADERFUNCTION => function ( $ch, $line ) use ( &$headers ) {
            $len = strlen( $line );
            $parts = explode( ':', $line, 2 );
            if ( count( $parts ) === 2 ) {
                $headers[ strtolower( trim( $parts[0] ) ) ] = trim( $parts[1] );
            }
            return $len;
        },
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
    return array(
        'code'    => $code,
        'body'    => $body,
        'json'    => is_array( $json ) ? $json : null,
        'headers' => $headers,
    );
}

/**
 * Download a file over Tor into a temp path. Returns the local file
 * path or WP_Error.
 */
function onionpress_blog_fetch_file( $url, $dest_path ) {
    $fh = @fopen( $dest_path, 'wb' );
    if ( ! $fh ) return new WP_Error( 'io', 'open temp failed' );
    $ch = curl_init( $url );
    curl_setopt_array( $ch, array(
        CURLOPT_FILE           => $fh,
        CURLOPT_FOLLOWLOCATION => true,
        CURLOPT_MAXREDIRS      => 5,
        CURLOPT_PROXY          => ONIONPRESS_BLOG_SOCKS_PROXY,
        CURLOPT_PROXYTYPE      => CURLPROXY_SOCKS5_HOSTNAME,
        CURLOPT_TIMEOUT        => 120,
        CURLOPT_CONNECTTIMEOUT => 30,
        CURLOPT_USERAGENT      => 'OnionPress/SocialArchive (+https://onionpress.org)',
    ) );
    $ok = curl_exec( $ch );
    $code = (int) curl_getinfo( $ch, CURLINFO_HTTP_CODE );
    $err  = curl_error( $ch );
    curl_close( $ch );
    fclose( $fh );
    if ( ! $ok || $code < 200 || $code >= 300 ) {
        @unlink( $dest_path );
        return new WP_Error( 'curl', 'HTTP ' . $code . ' ' . $err );
    }
    return $dest_path;
}

// --- Post mapping -------------------------------------------------------

/**
 * Lookup a local post by its `_source_id`. Returns post ID or 0.
 */
function onionpress_blog_find_post_by_source_id( $source_id ) {
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
 * Import or update one /wp/v2/posts JSON object. Returns
 * 'imported' | 'updated' | 'skipped' | 'errors'.
 *
 * Idempotency key: `_source_id = "blog:<host>:<wp_id>"`. If a local post
 * already exists, the source's `modified_gmt` is compared against the
 * stored `_source_modified`. Strictly-newer source = re-pull (overwrite
 * body, excerpt, tags, _source_modified). Equal-or-older = skip.
 */
function onionpress_blog_import_post( $post ) {
    if ( empty( $post['id'] ) ) return 'errors';
    $host = (string) get_option( ONIONPRESS_BLOG_HOST_OPT, '' );
    if ( $host === '' ) return 'errors';
    $source_id = 'blog:' . $host . ':' . (string) $post['id'];

    $modified_gmt_raw = (string) ( $post['modified_gmt'] ?? $post['date_gmt'] ?? '' );
    $modified_ts      = $modified_gmt_raw ? strtotime( $modified_gmt_raw . ' UTC' ) : 0;

    $existing_id = onionpress_blog_find_post_by_source_id( $source_id );
    if ( $existing_id ) {
        $stored_modified = (string) get_post_meta( $existing_id, '_source_modified', true );
        $stored_ts       = $stored_modified ? strtotime( $stored_modified . ' UTC' ) : 0;
        if ( $modified_ts && $modified_ts > $stored_ts ) {
            return onionpress_blog_update_post( $existing_id, $post );
        }
        return 'skipped';
    }

    $date_gmt_raw = (string) ( $post['date_gmt'] ?? '' );
    $date_ts      = $date_gmt_raw ? strtotime( $date_gmt_raw . ' UTC' ) : 0;
    if ( ! $date_ts ) return 'errors';

    list( $content_html, $preview_text ) = onionpress_blog_render_content( $post );
    $title = onionpress_blog_render_title( $post );
    if ( $title === '' ) $title = wp_trim_words( $preview_text, 10, '…' );
    if ( $title === '' ) $title = gmdate( 'Y-m-d', $date_ts ) . ' post';

    $source_url = (string) ( $post['link'] ?? '' );

    $post_date_gmt = gmdate( 'Y-m-d H:i:s', $date_ts );
    $post_id = wp_insert_post( array(
        'post_type'     => 'post',
        'post_status'   => 'publish',
        'post_title'    => $title,
        'post_content'  => $content_html,
        'post_excerpt'  => onionpress_blog_render_excerpt( $post, $preview_text ),
        'post_date_gmt' => $post_date_gmt,
        'post_date'     => get_date_from_gmt( $post_date_gmt ),
        'meta_input'    => array(
            '_source_id'        => $source_id,
            '_source_url'       => $source_url,
            '_source_modified'  => $modified_gmt_raw,
            '_source_slug'      => (string) ( $post['slug'] ?? '' ),
            '_is_repost'        => '0',
            '_is_reply'         => '0',
            '_raw'              => wp_json_encode( $post ),
        ),
    ), true );
    if ( is_wp_error( $post_id ) ) return 'errors';

    if ( function_exists( 'onionpress_social_ensure_category' ) ) {
        $cat_id = onionpress_social_ensure_category( 'blog' );
        if ( $cat_id ) {
            wp_set_post_categories( $post_id, array( $cat_id ), false );
        }
    }

    onionpress_blog_apply_tags( $post_id, $post );
    onionpress_blog_sideload_media( $post_id, $post );

    return 'imported';
}

/**
 * Refresh an existing local post against a newer source revision.
 * Overwrites title, content, excerpt, tags, and the `_source_modified`
 * marker; preserves the imported date_gmt so existing permalinks /
 * date archives don't shift. Returns 'updated' or 'errors'.
 */
function onionpress_blog_update_post( $existing_id, $post ) {
    list( $content_html, $preview_text ) = onionpress_blog_render_content( $post );
    $title = onionpress_blog_render_title( $post );
    if ( $title === '' ) $title = wp_trim_words( $preview_text, 10, '…' );
    if ( $title === '' ) {
        $existing = get_post( $existing_id );
        $title    = $existing ? $existing->post_title : 'post';
    }
    $modified_gmt_raw = (string) ( $post['modified_gmt'] ?? $post['date_gmt'] ?? '' );

    $r = wp_update_post( array(
        'ID'           => $existing_id,
        'post_title'   => $title,
        'post_content' => $content_html,
        'post_excerpt' => onionpress_blog_render_excerpt( $post, $preview_text ),
    ), true );
    if ( is_wp_error( $r ) ) return 'errors';

    update_post_meta( $existing_id, '_source_modified', $modified_gmt_raw );
    update_post_meta( $existing_id, '_raw', wp_json_encode( $post ) );
    if ( ! empty( $post['link'] ) ) {
        update_post_meta( $existing_id, '_source_url', (string) $post['link'] );
    }
    if ( ! empty( $post['slug'] ) ) {
        update_post_meta( $existing_id, '_source_slug', (string) $post['slug'] );
    }
    onionpress_blog_apply_tags( $existing_id, $post );

    return 'updated';
}

/**
 * Render the source post's title.rendered as plain text — strip HTML
 * since WP titles can contain entities, but no block-level markup.
 */
function onionpress_blog_render_title( $post ) {
    $raw = (string) ( $post['title']['rendered'] ?? '' );
    if ( $raw === '' ) return '';
    return trim( html_entity_decode( wp_strip_all_tags( $raw ), ENT_QUOTES, 'UTF-8' ) );
}

/**
 * Render the source post's excerpt.rendered, or fall back to a
 * 35-word preview of the body.
 */
function onionpress_blog_render_excerpt( $post, $preview_text ) {
    $raw = (string) ( $post['excerpt']['rendered'] ?? '' );
    if ( $raw !== '' ) {
        return trim( html_entity_decode( wp_strip_all_tags( $raw ), ENT_QUOTES, 'UTF-8' ) );
    }
    return wp_trim_words( $preview_text, 35, '…' );
}

/**
 * Render the source post's content.rendered. WP REST returns it as
 * HTML; unlike Mastodon, it's NOT pre-sanitized — we strip script /
 * iframe / embed / object blocks plus on*= event-handler attributes
 * before storing. Display-time wp_kses_post() handles the rest.
 *
 * Returns array( $content_html, $preview_text ).
 */
function onionpress_blog_render_content( $post ) {
    $html = (string) ( $post['content']['rendered'] ?? '' );
    $html = onionpress_blog_strip_dangerous_html( $html );
    $preview = trim( html_entity_decode( wp_strip_all_tags( $html ), ENT_QUOTES, 'UTF-8' ) );
    return array( $html, $preview );
}

/**
 * Strip executable-HTML constructs at ingest time. This is destructive
 * sanitization — we'd rather lose a working <iframe> embed than admit
 * <script> from a foreign site. Display-time wp_kses_post is the second
 * line of defense, not the first.
 */
function onionpress_blog_strip_dangerous_html( $html ) {
    // Drop tag-pair blocks entirely (including content between open + close).
    $html = preg_replace( '#<(script|style|iframe|embed|object|applet|noscript|form)\b[^>]*>.*?</\1\s*>#si', '', $html );
    // Drop unpaired / self-closing variants.
    $html = preg_replace( '#<(script|style|iframe|embed|object|applet|noscript|form|input|button)\b[^>]*/?>#si', '', $html );
    // Strip on*= event handlers (handles double-quoted, single-quoted, unquoted).
    $html = preg_replace( '#\son[a-z][a-z0-9_-]*\s*=\s*"[^"]*"#i', '', $html );
    $html = preg_replace( "#\son[a-z][a-z0-9_-]*\s*=\s*'[^']*'#i", '', $html );
    $html = preg_replace( '#\son[a-z][a-z0-9_-]*\s*=\s*[^\s>]+#i', '', $html );
    // Neutralize javascript: / data:text/html URLs in href/src.
    $html = preg_replace( '#(href|src)\s*=\s*"\s*(javascript|data)\s*:[^"]*"#i', '$1=""', $html );
    $html = preg_replace( "#(href|src)\s*=\s*'\s*(javascript|data)\s*:[^']*'#i", "$1=''", $html );
    return $html;
}

/**
 * Apply the source post's tags as local WP tags. Reads tag names from
 * `_embedded['wp:term']` (the second array element is tag terms; first
 * is categories), falling back to the numeric `tags` array (which is
 * useless without names, so we just skip in that case). Mirror-all
 * policy per the issue defaults.
 */
function onionpress_blog_apply_tags( $post_id, $post ) {
    $names = array();
    if ( ! empty( $post['_embedded']['wp:term'] ) && is_array( $post['_embedded']['wp:term'] ) ) {
        foreach ( $post['_embedded']['wp:term'] as $term_group ) {
            if ( ! is_array( $term_group ) ) continue;
            foreach ( $term_group as $term ) {
                if ( ! is_array( $term ) ) continue;
                if ( ( $term['taxonomy'] ?? '' ) !== 'post_tag' ) continue;
                $n = (string) ( $term['name'] ?? '' );
                if ( $n !== '' ) $names[] = $n;
            }
        }
    }
    if ( $names ) {
        wp_set_post_tags( $post_id, array_values( array_unique( $names ) ), false );
    }
}

// --- Media sideload + inline <img> rewrite ------------------------------

/**
 * Sideload featured media + inline body images into local uploads, so
 * the imported post is self-contained over Tor. Rewrites the body's
 * <img src> URLs to local paths after each successful sideload.
 */
function onionpress_blog_sideload_media( $post_id, $post ) {
    require_once ABSPATH . 'wp-admin/includes/image.php';
    require_once ABSPATH . 'wp-admin/includes/file.php';
    require_once ABSPATH . 'wp-admin/includes/media.php';

    // Featured image first so it doubles as the thumbnail.
    $featured_url = onionpress_blog_featured_media_url( $post );
    if ( $featured_url !== '' ) {
        $attach_id = onionpress_blog_sideload_url( $featured_url, $post_id );
        if ( $attach_id ) {
            set_post_thumbnail( $post_id, $attach_id );
        }
    }

    // Inline body images.
    $existing = get_post( $post_id );
    if ( ! $existing ) return;
    $body = (string) $existing->post_content;
    if ( $body === '' || strpos( $body, '<img' ) === false ) return;

    $map = array(); // src_url => local_url
    if ( preg_match_all( '#<img\s[^>]*src=["\']([^"\']+)["\'][^>]*>#i', $body, $m ) ) {
        foreach ( array_unique( $m[1] ) as $src ) {
            if ( ! preg_match( '~^https?://~i', $src ) ) continue; // already relative
            if ( isset( $map[ $src ] ) ) continue;
            $attach_id = onionpress_blog_sideload_url( $src, $post_id );
            if ( ! $attach_id ) continue;
            $local = (string) wp_get_attachment_url( $attach_id );
            if ( $local !== '' ) {
                $map[ $src ] = wp_make_link_relative( $local );
            }
        }
    }
    if ( ! empty( $map ) ) {
        $new_body = $body;
        foreach ( $map as $src => $local ) {
            $new_body = str_replace( $src, $local, $new_body );
        }
        if ( $new_body !== $body ) {
            wp_update_post( array( 'ID' => $post_id, 'post_content' => $new_body ) );
        }
    }
}

/**
 * Pull the featured-media URL from _embedded['wp:featuredmedia'][0].source_url
 * when present. Empty string when the post has no featured image.
 */
function onionpress_blog_featured_media_url( $post ) {
    $fm = $post['_embedded']['wp:featuredmedia'][0] ?? null;
    if ( ! is_array( $fm ) ) return '';
    $url = (string) ( $fm['source_url'] ?? '' );
    return preg_match( '~^https?://~i', $url ) ? $url : '';
}

/**
 * Download a single URL via Tor and register it as a WP attachment
 * tied to $post_id. Returns the attachment ID or 0.
 */
function onionpress_blog_sideload_url( $url, $post_id ) {
    $ext = strtolower( pathinfo( parse_url( $url, PHP_URL_PATH ) ?? '', PATHINFO_EXTENSION ) );
    if ( ! preg_match( '/^[a-z0-9]{1,5}$/', $ext ) ) {
        $ext = 'jpg';
    }
    $basename = 'blog-' . $post_id . '-' . wp_generate_password( 6, false ) . '.' . $ext;
    $tmp_path = sys_get_temp_dir() . '/' . $basename;
    $fetched  = onionpress_blog_fetch_file( $url, $tmp_path );
    if ( is_wp_error( $fetched ) ) return 0;

    $upload    = wp_upload_dir();
    $dest_name = wp_unique_filename( $upload['path'], $basename );
    $dest_path = $upload['path'] . '/' . $dest_name;
    if ( ! @rename( $tmp_path, $dest_path ) && ! @copy( $tmp_path, $dest_path ) ) {
        @unlink( $tmp_path );
        return 0;
    }
    @unlink( $tmp_path );

    $filetype = wp_check_filetype( $dest_name );
    $attach_id = wp_insert_attachment( array(
        'post_mime_type' => $filetype['type'] ?? 'application/octet-stream',
        'post_title'     => sanitize_title( pathinfo( $dest_name, PATHINFO_FILENAME ) ),
        'post_content'   => '',
        'post_status'    => 'inherit',
    ), $dest_path, $post_id );
    if ( is_wp_error( $attach_id ) || ! $attach_id ) return 0;

    $meta = wp_generate_attachment_metadata( $attach_id, $dest_path );
    wp_update_attachment_metadata( $attach_id, $meta );
    return (int) $attach_id;
}

// --- Edits pass ---------------------------------------------------------

/**
 * Re-check the source's recently-modified posts. Fetches a small
 * page sorted by modified-desc, compares each modified_gmt against
 * the stored _source_modified, and re-imports any that the source
 * has touched since last time. Bails as soon as a returned post is
 * not newer locally (since the API is ordered by modified-desc, the
 * tail can't be either).
 *
 * Returns the number of posts updated.
 */
function onionpress_blog_run_edits_pass( $host, $deadline ) {
    $url = onionpress_blog_rest_url( '/wp/v2/posts', array(
        'per_page' => ONIONPRESS_BLOG_EDITS_PASS_PER_PAGE,
        'page'     => 1,
        'orderby'  => 'modified',
        'order'    => 'desc',
        '_fields'  => 'id,modified_gmt',
    ) );
    if ( $url === '' ) return 0;

    $mock = apply_filters( 'onionpress_blog_edits_pass_mock', null );
    if ( $mock !== null ) {
        $list = is_array( $mock ) ? $mock : array();
    } else {
        $r = onionpress_blog_api_get( $url );
        if ( is_wp_error( $r ) || (int) $r['code'] !== 200 || ! is_array( $r['json'] ) ) {
            return 0;
        }
        $list = $r['json'];
    }

    $updated = 0;
    foreach ( $list as $row ) {
        if ( microtime( true ) >= $deadline ) break;
        $src_id = isset( $row['id'] ) ? (string) $row['id'] : '';
        $src_mod = (string) ( $row['modified_gmt'] ?? '' );
        if ( $src_id === '' || $src_mod === '' ) continue;
        $source_id   = 'blog:' . $host . ':' . $src_id;
        $existing_id = onionpress_blog_find_post_by_source_id( $source_id );
        if ( ! $existing_id ) continue; // not imported yet; backfill/forward will catch it
        $stored = (string) get_post_meta( $existing_id, '_source_modified', true );
        $src_ts    = strtotime( $src_mod . ' UTC' );
        $stored_ts = $stored ? strtotime( $stored . ' UTC' ) : 0;
        if ( $src_ts <= $stored_ts ) {
            // Ordered modified-desc — everything past this is also
            // not-newer. Stop scanning.
            break;
        }
        // Fetch the full post and update locally.
        $full_url = onionpress_blog_rest_url( '/wp/v2/posts/' . rawurlencode( $src_id ), array( '_embed' => 'true' ) );
        $rr = onionpress_blog_api_get( $full_url );
        if ( is_wp_error( $rr ) || (int) $rr['code'] !== 200 || ! is_array( $rr['json'] ) ) continue;
        $r2 = onionpress_blog_update_post( $existing_id, $rr['json'] );
        if ( $r2 === 'updated' ) $updated++;
    }
    return $updated;
}

// --- WXR snapshot storage ----------------------------------------------

const ONIONPRESS_BLOG_SNAPSHOTS_ROOT = '/var/www/html/wp-content/creations/blog-archives';

/**
 * Return the canonical (realpath-resolved) snapshots dir for a given
 * host, creating it if it doesn't exist. Returns '' if the root mount
 * isn't writable (i.e. the Creations bind-mount isn't in place).
 */
function onionpress_blog_snapshots_dir_for_host( $host ) {
    $host = preg_replace( '/[^a-z0-9._-]/i', '', strtolower( $host ) );
    if ( $host === '' ) return '';
    $root = ONIONPRESS_BLOG_SNAPSHOTS_ROOT;
    if ( ! is_dir( $root ) ) {
        if ( ! @mkdir( $root, 0755, true ) ) return '';
    }
    $target = $root . '/' . $host;
    if ( ! is_dir( $target ) ) {
        if ( ! @mkdir( $target, 0755, true ) ) return '';
    }
    $real_root   = realpath( $root );
    $real_target = realpath( $target );
    if ( ! $real_root || ! $real_target ) return '';
    if ( $real_target !== $real_root && strpos( $real_target, $real_root . '/' ) !== 0 ) return '';
    return $real_target;
}

/**
 * Handle a WXR upload from the admin form. Validates magic bytes,
 * derives the snapshot host (from <wp:base_blog_url> if present, else
 * from the configured importer host), gzips and stashes under the
 * per-host snapshots dir.
 */
function onionpress_blog_handle_wxr_upload() {
    if ( empty( $_FILES['blog_wxr'] ) || (int) $_FILES['blog_wxr']['error'] !== UPLOAD_ERR_OK ) {
        return array( 'level' => 'error', 'message' => 'Upload failed or no file provided.' );
    }
    $tmp  = $_FILES['blog_wxr']['tmp_name'];
    $name = (string) $_FILES['blog_wxr']['name'];
    if ( ! is_uploaded_file( $tmp ) ) {
        return array( 'level' => 'error', 'message' => 'Bad upload.' );
    }
    $is_gz = ( substr( strtolower( $name ), -3 ) === '.gz' );

    // Read a probe window (first 32 KiB raw, or first 32 KiB after
    // gunzip if the upload looks gzipped). Use it for two checks:
    // (1) confirm the file is a WordPress WXR (look for <wp:wxr_version>),
    // and (2) extract <wp:base_blog_url> if present.
    $probe = '';
    if ( $is_gz ) {
        $gz = @gzopen( $tmp, 'rb' );
        if ( ! $gz ) return array( 'level' => 'error', 'message' => 'Could not read the .gz file (corrupt?).' );
        $probe = (string) gzread( $gz, 32 * 1024 );
        gzclose( $gz );
    } else {
        $fh = @fopen( $tmp, 'rb' );
        if ( ! $fh ) return array( 'level' => 'error', 'message' => 'Could not read upload.' );
        $probe = (string) fread( $fh, 32 * 1024 );
        fclose( $fh );
    }
    if ( strpos( $probe, '<wp:wxr_version>' ) === false ) {
        return array( 'level' => 'error', 'message' => 'That file doesn&rsquo;t look like a WordPress WXR export (no <code>&lt;wp:wxr_version&gt;</code> tag in the first 32 KiB).' );
    }

    // Derive host. Prefer <wp:base_blog_url> from the WXR for accuracy
    // (the file is the source of truth about which blog this is from).
    $wxr_host = '';
    if ( preg_match( '#<wp:base_blog_url>\s*([^<]+)\s*</wp:base_blog_url>#i', $probe, $m ) ) {
        $parsed = onionpress_blog_parse_url( $m[1] );
        if ( $parsed ) $wxr_host = $parsed['host'];
    }
    if ( $wxr_host === '' ) {
        $wxr_host = (string) get_option( ONIONPRESS_BLOG_HOST_OPT, '' );
    }
    if ( $wxr_host === '' ) {
        return array( 'level' => 'error', 'message' => 'Could not determine the blog this WXR is from — set the source URL above first, or upload a WXR that contains a <code>&lt;wp:base_blog_url&gt;</code>.' );
    }

    $target_dir = onionpress_blog_snapshots_dir_for_host( $wxr_host );
    if ( $target_dir === '' ) {
        return array( 'level' => 'error', 'message' => 'Snapshot dir is not writable. Make sure <code>~/OnionPress/Creations/My Creations/</code> exists on the host.' );
    }

    $ts        = gmdate( 'Y-m-d\TH-i-s\Z' );
    $dest_path = $target_dir . '/' . $ts . '.xml.gz';
    // Path-traversal guard (defense-in-depth — host is already sanitized).
    $real_target = realpath( $target_dir );
    if ( ! $real_target || strpos( $dest_path, $real_target . '/' ) !== 0 ) {
        return array( 'level' => 'error', 'message' => 'Refusing to write outside the snapshots dir.' );
    }

    // Gzip on the fly if the upload wasn't already gzipped. Use chunked
    // copy so a 200 MB WXR doesn't blow PHP memory.
    if ( $is_gz ) {
        if ( ! @move_uploaded_file( $tmp, $dest_path ) ) {
            if ( ! @copy( $tmp, $dest_path ) ) {
                return array( 'level' => 'error', 'message' => 'Could not write snapshot file.' );
            }
        }
    } else {
        $in  = @fopen( $tmp, 'rb' );
        $out = @gzopen( $dest_path, 'wb6' );
        if ( ! $in || ! $out ) {
            if ( $in )  fclose( $in );
            if ( $out ) gzclose( $out );
            return array( 'level' => 'error', 'message' => 'Could not open files for gzip.' );
        }
        while ( ! feof( $in ) ) {
            $chunk = fread( $in, 64 * 1024 );
            if ( $chunk === false ) break;
            gzwrite( $out, $chunk );
        }
        fclose( $in );
        gzclose( $out );
    }
    @chmod( $dest_path, 0644 );

    $size = @filesize( $dest_path );
    return array(
        'level'   => 'success',
        'message' => sprintf(
            'Stored WXR snapshot for <code>%s</code> (%s). Survives across reinstalls.',
            esc_html( $wxr_host ),
            esc_html( size_format( (int) $size ) )
        ),
    );
}

function onionpress_blog_handle_wxr_delete() {
    $host = isset( $_POST['blog_wxr_host'] ) ? (string) wp_unslash( $_POST['blog_wxr_host'] ) : '';
    $file = isset( $_POST['blog_wxr_file'] ) ? (string) wp_unslash( $_POST['blog_wxr_file'] ) : '';
    $host = preg_replace( '/[^a-z0-9._-]/i', '', strtolower( $host ) );
    $file = basename( $file );
    if ( $host === '' || $file === '' ) {
        return array( 'level' => 'error', 'message' => 'Missing snapshot details.' );
    }
    $dir = onionpress_blog_snapshots_dir_for_host( $host );
    if ( $dir === '' ) {
        return array( 'level' => 'error', 'message' => 'Snapshots dir not found.' );
    }
    $path = $dir . '/' . $file;
    $real = realpath( $path );
    if ( ! $real || strpos( $real, $dir . '/' ) !== 0 ) {
        return array( 'level' => 'error', 'message' => 'Refusing to delete outside the snapshots dir.' );
    }
    if ( ! @unlink( $real ) ) {
        return array( 'level' => 'error', 'message' => 'Could not delete the snapshot.' );
    }
    return array( 'level' => 'success', 'message' => 'Snapshot deleted.' );
}

/**
 * Stream a snapshot file to the browser, admin-authenticated via the
 * admin-post.php nonce. We do NOT publish the snapshots dir under the
 * webroot — downloads always go through this handler.
 */
function onionpress_blog_serve_wxr_download() {
    if ( ! current_user_can( 'manage_options' ) ) {
        wp_die( 'Unauthorized', 'OnionPress', array( 'response' => 403 ) );
    }
    check_admin_referer( 'onionpress_blog_download_wxr' );
    $host = isset( $_GET['host'] ) ? (string) wp_unslash( $_GET['host'] ) : '';
    $file = isset( $_GET['file'] ) ? (string) wp_unslash( $_GET['file'] ) : '';
    $host = preg_replace( '/[^a-z0-9._-]/i', '', strtolower( $host ) );
    $file = basename( $file );
    if ( $host === '' || $file === '' ) {
        wp_die( 'Bad request', 'OnionPress', array( 'response' => 400 ) );
    }
    $dir = onionpress_blog_snapshots_dir_for_host( $host );
    if ( $dir === '' ) {
        wp_die( 'No such host', 'OnionPress', array( 'response' => 404 ) );
    }
    $path = $dir . '/' . $file;
    $real = realpath( $path );
    if ( ! $real || strpos( $real, $dir . '/' ) !== 0 || ! is_file( $real ) ) {
        wp_die( 'No such snapshot', 'OnionPress', array( 'response' => 404 ) );
    }
    while ( ob_get_level() ) { ob_end_clean(); }
    header( 'Content-Type: application/gzip' );
    header( 'Content-Length: ' . filesize( $real ) );
    header( 'Content-Disposition: attachment; filename="' . $host . '-' . $file . '"' );
    readfile( $real );
    exit;
}

/**
 * Render the list of existing snapshots, grouped by host.
 */
function onionpress_blog_render_snapshots() {
    $root = ONIONPRESS_BLOG_SNAPSHOTS_ROOT;
    if ( ! is_dir( $root ) ) {
        echo '<p><em>No snapshots yet.</em></p>';
        return;
    }
    $entries = @scandir( $root );
    if ( ! $entries ) {
        echo '<p><em>No snapshots yet.</em></p>';
        return;
    }
    $hosts = array();
    foreach ( $entries as $e ) {
        if ( $e === '.' || $e === '..' ) continue;
        $sub = $root . '/' . $e;
        if ( is_dir( $sub ) ) $hosts[] = $e;
    }
    if ( empty( $hosts ) ) {
        echo '<p><em>No snapshots yet.</em></p>';
        return;
    }
    echo '<h3>Stored snapshots</h3>';
    foreach ( $hosts as $h ) {
        $dir = $root . '/' . $h;
        $files = array();
        foreach ( (array) @scandir( $dir ) as $f ) {
            if ( $f === '.' || $f === '..' ) continue;
            $p = $dir . '/' . $f;
            if ( is_file( $p ) ) $files[] = array( 'name' => $f, 'size' => filesize( $p ), 'mtime' => filemtime( $p ) );
        }
        if ( empty( $files ) ) continue;
        usort( $files, function( $a, $b ) { return $b['mtime'] <=> $a['mtime']; } );
        echo '<p style="margin-bottom:0.25em;"><strong>' . esc_html( $h ) . '</strong></p>';
        echo '<table class="wp-list-table widefat" style="max-width:780px;margin-bottom:1em;"><thead><tr>'
           . '<th>Filename</th><th style="width:120px;">Size</th><th style="width:200px;">Saved</th><th style="width:160px;">Actions</th>'
           . '</tr></thead><tbody>';
        foreach ( $files as $f ) {
            $dl_url = wp_nonce_url(
                add_query_arg(
                    array(
                        'action' => 'onionpress_blog_download_wxr',
                        'host'   => $h,
                        'file'   => $f['name'],
                    ),
                    admin_url( 'admin-post.php' )
                ),
                'onionpress_blog_download_wxr'
            );
            echo '<tr>'
               . '<td><code>' . esc_html( $f['name'] ) . '</code></td>'
               . '<td>' . esc_html( size_format( (int) $f['size'] ) ) . '</td>'
               . '<td>' . esc_html( gmdate( 'Y-m-d H:i \U\T\C', (int) $f['mtime'] ) ) . '</td>'
               . '<td><a class="button button-small" href="' . esc_url( $dl_url ) . '">Download</a> '
               . '<form method="post" style="display:inline;" onsubmit="return confirm(\'Delete this snapshot?\');">'
               . wp_nonce_field( 'onionpress_blog_delete_wxr', 'onionpress_blog_wxr_delete_nonce', true, false )
               . '<input type="hidden" name="onionpress_blog_delete_wxr" value="1">'
               . '<input type="hidden" name="blog_wxr_host" value="' . esc_attr( $h ) . '">'
               . '<input type="hidden" name="blog_wxr_file" value="' . esc_attr( $f['name'] ) . '">'
               . '<button type="submit" class="button button-small button-link-delete">Delete</button>'
               . '</form></td>'
               . '</tr>';
        }
        echo '</tbody></table>';
    }
}

// --- Recent imports list ------------------------------------------------

function onionpress_blog_render_recent() {
    $recent = get_posts( array(
        'post_type'      => 'post',
        'posts_per_page' => 10,
        'orderby'        => 'date',
        'order'          => 'DESC',
        'category_name'  => 'blog',
    ) );
    if ( empty( $recent ) ) {
        echo '<p><em>No blog posts imported yet.</em></p>';
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

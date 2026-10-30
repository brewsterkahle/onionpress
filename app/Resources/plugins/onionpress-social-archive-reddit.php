<?php
/**
 * Plugin Name: OnionPress Social Archive — Reddit Importer
 * Description: Import a Reddit data-request CSV ZIP into the unified
 *              social_post archive. Parses posts.csv and comments.csv,
 *              creates one post per Reddit submission and one post per
 *              Reddit comment (option 2 modeling — comments become posts
 *              because they have no natural WP-comment parent). Safe to
 *              re-run — Reddit fullnames (t3_ / t1_) are used as
 *              idempotency keys.
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
        onionpress_social_register_importer( 'reddit' );
    }
} );

const ONIONPRESS_REDDIT_ADMIN_SLUG   = 'onionpress-social-archive-reddit';
const ONIONPRESS_REDDIT_HANDLE_OPT   = 'onionpress_social_reddit_handle';
const ONIONPRESS_REDDIT_DEEPLINK_URL = 'https://www.reddit.com/settings/data-request';

// Sync (live JSON-API polling) — separate from the CSV upload path. Both
// feed into the same import_submission/import_comment functions, so
// dedup on _source_id makes them safely combinable.
const ONIONPRESS_REDDIT_CRON_HOOK       = 'onionpress_social_reddit_sync';
// Route through the bulk-outgoing Tor daemon ([[feedback-separate-tor-in-out]]).
const ONIONPRESS_REDDIT_SOCKS_PROXY     = 'onionheaven:9050';
// Reddit's official .onion mirror — Tor read-friendly, low captcha risk
// vs. the clearnet host even with a Tor exit.
const ONIONPRESS_REDDIT_API_HOST        = 'www.reddittorjg6rue252oqsxryoxengawnmo46qy4kyii5wtqnwfj4ooad.onion';
const ONIONPRESS_REDDIT_HTTP_TIMEOUT    = 45;
const ONIONPRESS_REDDIT_PAGES_PER_TICK  = 5;   // per feed; ~500 items max per tick
const ONIONPRESS_REDDIT_PER_PAGE        = 100; // Reddit's listing cap per page
const ONIONPRESS_REDDIT_TICK_BUDGET_SEC = 25;
const ONIONPRESS_REDDIT_LOCK_OPT        = 'onionpress_social_reddit_lock';
const ONIONPRESS_REDDIT_LAST_SYNC_OPT   = 'onionpress_social_reddit_last_sync';
const ONIONPRESS_REDDIT_LAST_NOTE_OPT   = 'onionpress_social_reddit_last_note';
// Cache of resolved parent-thread titles keyed by Reddit fullname (t3_*).
// Empty string in the cache means "we tried, thread is gone" — prevents
// infinite retries on deleted parents.
const ONIONPRESS_REDDIT_TITLE_CACHE_OPT = 'onionpress_social_reddit_thread_titles';
// Max comments resolved per tick. The /api/info endpoint returns up to
// 100 fullnames per call, and we may process more than one batch per tick.
const ONIONPRESS_REDDIT_TITLES_PER_TICK = 200;

// Reddit base URL preference depends on viewer context: when the admin
// is hitting WP over .onion, prefer the Reddit .onion mirror so any
// click-throughs stay on Tor.
function onionpress_reddit_base_url() {
    $host = (string) ( $_SERVER['HTTP_HOST'] ?? '' );
    if ( substr( $host, -6 ) === '.onion' ) {
        return 'https://www.reddittorjg6rue252oqsxryoxengawnmo46qy4kyii5wtqnwfj4ooad.onion';
    }
    return 'https://www.reddit.com';
}

add_action( 'admin_menu', function () {
    if ( ! defined( 'ONIONPRESS_SOCIAL_ADMIN_SLUG' ) ) {
        return;
    }
    add_submenu_page(
        ONIONPRESS_SOCIAL_ADMIN_SLUG,
        'Import Reddit',
        'Reddit',
        'manage_options',
        ONIONPRESS_REDDIT_ADMIN_SLUG,
        'onionpress_reddit_import_page'
    );
}, 20 );

// Cron auto-enables itself as soon as the user saves a handle, and
// auto-disables when the handle is cleared.
add_action( 'admin_init', function () {
    $handle    = onionpress_reddit_get_saved_handle();
    $scheduled = wp_next_scheduled( ONIONPRESS_REDDIT_CRON_HOOK );
    if ( $handle !== '' && ! $scheduled ) {
        wp_schedule_event( time() + 60, 'onionpress_reddit_10min', ONIONPRESS_REDDIT_CRON_HOOK );
    } elseif ( $handle === '' && $scheduled ) {
        wp_unschedule_event( $scheduled, ONIONPRESS_REDDIT_CRON_HOOK );
    }
} );

add_filter( 'cron_schedules', function ( $schedules ) {
    if ( ! isset( $schedules['onionpress_reddit_10min'] ) ) {
        $schedules['onionpress_reddit_10min'] = array(
            'interval' => 600,
            'display'  => '10 min (OnionPress Reddit poll)',
        );
    }
    return $schedules;
} );

add_action( ONIONPRESS_REDDIT_CRON_HOOK, 'onionpress_reddit_run_sync_tick' );

function onionpress_reddit_import_page() {
    if ( ! current_user_can( 'manage_options' ) ) {
        wp_die( 'Unauthorized' );
    }

    $result        = null;
    $handle_notice = null;
    $sync_notice   = null;
    if ( $_SERVER['REQUEST_METHOD'] === 'POST' ) {
        if ( isset( $_POST['onionpress_reddit_save_handle'] ) ) {
            check_admin_referer( 'onionpress_reddit_save_handle', 'onionpress_reddit_handle_nonce' );
            $handle_notice = onionpress_reddit_handle_save_handle_post();
        } elseif ( isset( $_POST['onionpress_reddit_sync_now'] ) ) {
            check_admin_referer( 'onionpress_reddit_sync_now', 'onionpress_reddit_sync_nonce' );
            $sync_notice = onionpress_reddit_handle_sync_now_post();
        } elseif ( isset( $_FILES['reddit_zip'] ) ) {
            check_admin_referer( 'onionpress_reddit_import', 'onionpress_reddit_nonce' );
            $result = onionpress_reddit_handle_upload();
        }
    }

    $saved_handle = onionpress_reddit_get_saved_handle();
    ?>
    <div class="wrap">
        <h1>Import Reddit archive</h1>

        <?php if ( $result ) : ?>
            <div class="notice notice-<?php echo esc_attr( $result['level'] ); ?>">
                <p><?php echo wp_kses_post( $result['message'] ); ?></p>
            </div>
        <?php endif; ?>
        <?php if ( $handle_notice ) : ?>
            <div class="notice notice-<?php echo esc_attr( $handle_notice['level'] ); ?>">
                <p><?php echo wp_kses_post( $handle_notice['message'] ); ?></p>
            </div>
        <?php endif; ?>
        <?php if ( $sync_notice ) : ?>
            <div class="notice notice-<?php echo esc_attr( $sync_notice['level'] ); ?>">
                <p><?php echo wp_kses_post( $sync_notice['message'] ); ?></p>
            </div>
        <?php endif; ?>

        <h2>Step 1 &mdash; Tell us your Reddit username</h2>
        <p>Your username is saved on this onion, never transmitted anywhere. We use it to label imported posts and to link back to your profile.</p>
        <form method="post" style="margin-bottom:1em;">
            <?php wp_nonce_field( 'onionpress_reddit_save_handle', 'onionpress_reddit_handle_nonce' ); ?>
            <input type="hidden" name="onionpress_reddit_save_handle" value="1">
            <table class="form-table" role="presentation">
                <tr>
                    <th><label for="reddit_handle">Your Reddit username</label></th>
                    <td>
                        <input type="text" id="reddit_handle" name="reddit_handle"
                               value="<?php echo esc_attr( $saved_handle ); ?>"
                               placeholder="yourname"
                               pattern="u?\/?[A-Za-z0-9_\-]{3,20}"
                               class="regular-text" style="max-width:260px;">
                        <?php submit_button( 'Save', 'secondary', 'submit', false ); ?>
                        <p class="description">Just the username &mdash; no URL, no password. Example: <code>spez</code> or <code>u/spez</code>.</p>
                    </td>
                </tr>
            </table>
        </form>

        <?php onionpress_reddit_render_sync_panel( $saved_handle ); ?>

        <h2>Optional bulk upload &mdash; full history beyond the sync cap</h2>
        <p>Skip this if you're happy with the most recent ~1,000 submissions and ~1,000 comments that automatic sync covers. For longer-tenured accounts with deeper history, request the official CSV export and upload it here &mdash; it merges cleanly with sync (every item is deduped by Reddit ID).</p>

        <h3>1. Request your data from Reddit</h3>
        <p>
            <a href="<?php echo esc_url( ONIONPRESS_REDDIT_DEEPLINK_URL ); ?>"
               target="_blank" rel="noopener noreferrer"
               class="button button-primary">
                Open Reddit data-request page &rarr;
            </a>
        </p>
        <p class="description">On the request page, select <strong>CSV</strong> (not GDPR/JSON), choose <strong>All time</strong>, and submit. You're already logged in to Reddit in your own browser &mdash; OnionPress doesn't need, store, or want your Reddit password.</p>

        <h3>2. Wait for the email</h3>
        <p><strong>Typical wait: a few hours</strong>, occasionally up to 30 days. Reddit emails a download link that expires in about a week.</p>

        <h3>3. Upload the ZIP here</h3>
        <p>Download the ZIP from Reddit's email <em>without unzipping it</em>, then drop it below.</p>
        <form method="post" enctype="multipart/form-data">
            <?php wp_nonce_field( 'onionpress_reddit_import', 'onionpress_reddit_nonce' ); ?>
            <table class="form-table" role="presentation">
                <tr>
                    <th><label for="reddit_zip">Archive ZIP</label></th>
                    <td>
                        <input type="file" name="reddit_zip" id="reddit_zip" accept=".zip" required>
                        <p class="description">Upload the CSV ZIP from Reddit as-downloaded. Nothing in this file is sent off your onion.</p>
                    </td>
                </tr>
                <tr>
                    <th>What to import</th>
                    <td>
                        <fieldset>
                            <label><input type="checkbox" name="include_submissions" value="1" checked> Include submissions (your posts to subreddits)</label><br>
                            <label><input type="checkbox" name="include_comments" value="1" checked> Include comments (your replies on other threads)</label>
                        </fieldset>
                        <p class="description">Each Reddit item becomes its own post. Comments link back to the thread they replied on.</p>
                    </td>
                </tr>
            </table>
            <?php submit_button( 'Import Reddit content' ); ?>
        </form>

        <h2>Recent imports</h2>
        <?php onionpress_reddit_render_recent(); ?>
    </div>
    <?php
}

function onionpress_reddit_get_saved_handle() {
    return (string) get_option( ONIONPRESS_REDDIT_HANDLE_OPT, '' );
}

function onionpress_reddit_handle_save_handle_post() {
    $raw = isset( $_POST['reddit_handle'] ) ? wp_unslash( $_POST['reddit_handle'] ) : '';
    $handle = trim( (string) $raw );
    // Accept "u/name", "/u/name", or bare "name". Strip any of those prefixes.
    $handle = preg_replace( '#^/?u/#i', '', $handle );
    if ( $handle === '' ) {
        delete_option( ONIONPRESS_REDDIT_HANDLE_OPT );
        return array( 'level' => 'success', 'message' => 'Username cleared.' );
    }
    // Reddit username rules: 3-20 chars, letters/digits/_/-.
    if ( ! preg_match( '/^[A-Za-z0-9_\-]{3,20}$/D', $handle ) ) {
        return array(
            'level'   => 'error',
            'message' => 'That doesn&rsquo;t look like a Reddit username. Must be 3&ndash;20 letters, digits, underscores, or hyphens.',
        );
    }
    update_option( ONIONPRESS_REDDIT_HANDLE_OPT, $handle );
    return array(
        'level'   => 'success',
        'message' => sprintf( 'Saved username <strong>u/%s</strong>.', esc_html( $handle ) ),
    );
}

function onionpress_reddit_handle_upload() {
    if ( ! isset( $_FILES['reddit_zip'] ) || $_FILES['reddit_zip']['error'] !== UPLOAD_ERR_OK ) {
        return array( 'level' => 'error', 'message' => 'Upload failed or no file provided.' );
    }

    $upload_name = $_FILES['reddit_zip']['name'];
    $upload_tmp  = $_FILES['reddit_zip']['tmp_name'];
    $opts = array(
        'include_submissions' => ! empty( $_POST['include_submissions'] ),
        'include_comments'    => ! empty( $_POST['include_comments'] ),
    );
    if ( ! $opts['include_submissions'] && ! $opts['include_comments'] ) {
        return array( 'level' => 'error', 'message' => 'Nothing selected to import.' );
    }

    $workdir = sys_get_temp_dir() . '/onionpress-reddit-' . time() . '-' . wp_generate_password( 8, false );
    if ( ! wp_mkdir_p( $workdir ) ) {
        return array( 'level' => 'error', 'message' => 'Could not create temp dir for extraction.' );
    }
    @chmod( $workdir, 0700 );

    $extracted = onionpress_reddit_extract_safely( $upload_tmp, $workdir );
    if ( is_wp_error( $extracted ) ) {
        onionpress_reddit_rrmdir( $workdir );
        return array(
            'level'   => 'error',
            'message' => 'ZIP refused: ' . esc_html( $extracted->get_error_message() ) . ' Upload the CSV export as Reddit delivered it.',
        );
    }

    $posts_csv    = onionpress_reddit_find_csv( $workdir, 'posts.csv' );
    $comments_csv = onionpress_reddit_find_csv( $workdir, 'comments.csv' );
    if ( ! $posts_csv && ! $comments_csv ) {
        onionpress_reddit_rrmdir( $workdir );
        return array(
            'level'   => 'error',
            'message' => 'No <code>posts.csv</code> or <code>comments.csv</code> found in the ZIP. Make sure you selected <strong>CSV</strong> on the Reddit data-request page (not GDPR/JSON).',
        );
    }

    // Sort each set oldest-first so created_at ordering survives import.
    // Reddit's CSVs come newest-first, which would invert the WP date
    // archive timeline.
    $stats = array( 'imported' => 0, 'skipped' => 0, 'errors' => 0 );

    if ( $opts['include_submissions'] && $posts_csv ) {
        $rows = onionpress_reddit_read_csv( $posts_csv );
        usort( $rows, function ( $a, $b ) {
            return strcmp( (string) ( $a['date'] ?? '' ), (string) ( $b['date'] ?? '' ) );
        } );
        foreach ( $rows as $row ) {
            $r = onionpress_reddit_import_submission( $row );
            if ( isset( $stats[ $r ] ) ) { $stats[ $r ]++; }
        }
    }

    if ( $opts['include_comments'] && $comments_csv ) {
        $rows = onionpress_reddit_read_csv( $comments_csv );
        usort( $rows, function ( $a, $b ) {
            return strcmp( (string) ( $a['date'] ?? '' ), (string) ( $b['date'] ?? '' ) );
        } );
        foreach ( $rows as $row ) {
            $r = onionpress_reddit_import_comment( $row );
            if ( isset( $stats[ $r ] ) ) { $stats[ $r ]++; }
        }
    }

    onionpress_reddit_rrmdir( $workdir );

    $msg = sprintf(
        'Processed <strong>%s</strong>. Imported: <strong>%d</strong>. Skipped (already imported or empty): <strong>%d</strong>. Errors: <strong>%d</strong>.',
        esc_html( $upload_name ),
        intval( $stats['imported'] ),
        intval( $stats['skipped'] ),
        intval( $stats['errors'] )
    );
    $level = $stats['errors'] > 0 && $stats['imported'] === 0 ? 'error' : 'success';
    return array( 'level' => $level, 'message' => $msg );
}

/**
 * The members of a Reddit data export the importer reads: posts.csv and
 * comments.csv, at the root or under one per-request folder. Nothing
 * else in the archive is extracted (#473).
 */
const ONIONPRESS_REDDIT_ZIP_MEMBERS = '#^(?:[^/]+/)?(?:posts|comments)\.csv$#D';

/**
 * Unpack the two CSVs from a Reddit data export at ``$zip_path`` into
 * ``$target_dir`` through onionpress_social_zip_extract(): the archive
 * is held to the shared limits (members, per-member and total size,
 * expansion, free space), names that would escape the target, symlinks
 * and special files refuse it whole, and nothing is written before the
 * checks pass (#473). Returns true or a WP_Error saying why.
 */
function onionpress_reddit_extract_safely( $zip_path, $target_dir ) {
    return onionpress_social_zip_extract( $zip_path, $target_dir, ONIONPRESS_REDDIT_ZIP_MEMBERS );
}

/**
 * Locate a named CSV anywhere inside the extracted ZIP. Reddit's export
 * historically ships files at the top level, but some exports nest them
 * inside a per-request folder — walk one level deep.
 */
function onionpress_reddit_find_csv( $root, $name ) {
    $p = $root . '/' . $name;
    if ( is_file( $p ) ) {
        return $p;
    }
    foreach ( scandir( $root ) as $entry ) {
        if ( $entry === '.' || $entry === '..' ) continue;
        $sub = $root . '/' . $entry;
        if ( is_dir( $sub ) && is_file( $sub . '/' . $name ) ) {
            return $sub . '/' . $name;
        }
    }
    return null;
}

/**
 * Read a Reddit CSV into an array of associative rows keyed by header.
 * Reddit's CSVs use standard RFC-4180 quoting; PHP's fgetcsv handles it.
 */
function onionpress_reddit_read_csv( $path ) {
    $rows = array();
    $fh = fopen( $path, 'r' );
    if ( ! $fh ) {
        return $rows;
    }
    $headers = fgetcsv( $fh );
    if ( ! is_array( $headers ) ) {
        fclose( $fh );
        return $rows;
    }
    $headers = array_map( 'strtolower', $headers );
    while ( ( $cols = fgetcsv( $fh ) ) !== false ) {
        if ( ! is_array( $cols ) ) continue;
        $row = array();
        foreach ( $headers as $i => $h ) {
            $row[ $h ] = $cols[ $i ] ?? '';
        }
        $rows[] = $row;
    }
    fclose( $fh );
    return $rows;
}

/**
 * Import a single submission row. Returns one of 'imported', 'skipped',
 * 'errors' for the caller's tally. Idempotent on _source_id.
 */
function onionpress_reddit_import_submission( $row ) {
    $rid = (string) ( $row['id'] ?? '' );
    if ( $rid === '' ) {
        return 'errors';
    }
    $source_id = 'reddit:t3_' . $rid;
    if ( onionpress_reddit_find_post_by_source_id( $source_id ) ) {
        return 'skipped';
    }

    $ts = onionpress_reddit_parse_date( (string) ( $row['date'] ?? '' ) );
    if ( ! $ts ) {
        return 'errors';
    }
    $post_date_gmt = gmdate( 'Y-m-d H:i:s', $ts );

    // A thread title is another Reddit user's text (#472): plain text
    // before it becomes post_title.
    $title     = onionpress_social_plain_text( $row['title'] ?? '' );
    $body_md   = (string) ( $row['body']  ?? '' );
    $link_url  = (string) ( $row['url']   ?? '' );
    $subreddit = (string) ( $row['subreddit'] ?? '' );
    $permalink = onionpress_reddit_clean_permalink( (string) ( $row['permalink'] ?? '' ) );

    $is_link_post = $link_url !== '' && $link_url !== 'self' && $body_md === '';

    // Compose the content. For text submissions, render the body markdown;
    // for link submissions, lead with the link and any provided body below.
    $content = '';
    if ( $is_link_post ) {
        $content .= sprintf(
            '<p><strong>Link:</strong> <a href="%s" rel="nofollow noopener">%s</a></p>',
            esc_url( $link_url ),
            esc_html( $link_url )
        );
    }
    if ( $body_md !== '' ) {
        $content .= onionpress_reddit_markdown_to_html( $body_md );
    }
    if ( $content === '' && $title !== '' ) {
        // Edge case: a deleted text post with no body. Title alone goes in.
        $content = '<p>' . esc_html( $title ) . '</p>';
    }

    if ( $title === '' ) {
        $title = wp_trim_words( onionpress_social_plain_text( $content ), 10, '…' );
    }
    if ( $title === '' ) {
        $title = gmdate( 'Y-m-d', $ts ) . ' Reddit post';
    }

    $meta = array(
        '_source_id'   => $source_id,
        '_source_url'  => $permalink,
        '_subreddit'   => $subreddit,
        '_is_repost'   => '0',
        '_is_reply'    => '0',
        '_raw'         => wp_json_encode( $row ),
    );

    return onionpress_reddit_insert_post( $title, $content, $post_date_gmt, $meta, $subreddit );
}

/**
 * Import a single comment row. Comments become posts under option 2 —
 * see issue #241. Returns 'imported' / 'skipped' / 'errors'.
 */
function onionpress_reddit_import_comment( $row ) {
    $rid = (string) ( $row['id'] ?? '' );
    if ( $rid === '' ) {
        return 'errors';
    }
    $source_id = 'reddit:t1_' . $rid;
    if ( onionpress_reddit_find_post_by_source_id( $source_id ) ) {
        return 'skipped';
    }

    $ts = onionpress_reddit_parse_date( (string) ( $row['date'] ?? '' ) );
    if ( ! $ts ) {
        return 'errors';
    }
    $post_date_gmt = gmdate( 'Y-m-d H:i:s', $ts );

    $body_md   = (string) ( $row['body']      ?? '' );
    $link_url  = (string) ( $row['link']      ?? '' );
    $parent    = (string) ( $row['parent']    ?? '' );
    $subreddit = (string) ( $row['subreddit'] ?? '' );
    $permalink = onionpress_reddit_clean_permalink( (string) ( $row['permalink'] ?? '' ) );

    if ( $body_md === '' ) {
        return 'skipped';
    }

    // Title derivation: cache lookup → slug fallback → body fallback.
    // The cache is populated by onionpress_reddit_resolve_titles_batch()
    // during sync ticks, so newly-imported comments benefit from titles
    // resolved by earlier imports.
    $link_id     = onionpress_reddit_extract_link_id( $row );
    $cache       = onionpress_reddit_get_title_cache();
    $thread_slug = onionpress_reddit_thread_slug( $permalink ?: $link_url );
    $slug_human  = $thread_slug !== '' ? onionpress_reddit_humanize_slug( $thread_slug ) : '';

    $used_cached_title = false;
    if ( $link_id !== '' && isset( $cache[ $link_id ] ) && $cache[ $link_id ] !== '' ) {
        $title = 'Re: ' . $cache[ $link_id ];
        $reply_label = $cache[ $link_id ];
        $used_cached_title = true;
    } elseif ( $slug_human !== '' ) {
        $title = 'Re: ' . $slug_human;
        $reply_label = $slug_human;
    } else {
        $title = wp_trim_words( onionpress_social_plain_text( $body_md ), 10, '…' );
        $reply_label = $link_url;
    }
    if ( $title === '' ) {
        $title = gmdate( 'Y-m-d', $ts ) . ' Reddit comment';
    }

    $content  = '';
    if ( $link_url !== '' ) {
        $content .= sprintf(
            '<p><em>In reply to <a href="%s" rel="nofollow noopener">%s</a></em></p>',
            esc_url( $link_url ),
            esc_html( $reply_label )
        );
    }
    $content .= onionpress_reddit_markdown_to_html( $body_md );

    $meta = array(
        '_source_id'      => $source_id,
        '_source_url'     => $permalink,
        '_subreddit'      => $subreddit,
        '_is_repost'      => '0',
        '_is_reply'       => '1',
        '_reply_to_id'    => $parent,
        '_thread_link_id' => $link_id,
        '_raw'            => wp_json_encode( $row ),
    );
    if ( $used_cached_title ) {
        // No need to revisit this one during title resolution.
        $meta['_thread_title_resolved'] = '1';
    }

    return onionpress_reddit_insert_post( $title, $content, $post_date_gmt, $meta, $subreddit );
}

/**
 * Shared insert path used by both submissions and comments. Tags the
 * post with the subreddit name and assigns it to the Reddit category.
 */
function onionpress_reddit_insert_post( $title, $content, $post_date_gmt, $meta, $subreddit ) {
    // Every Reddit title and excerpt is plain text at ingest (#472),
    // whatever path built it.
    $title   = onionpress_social_plain_text( $title );
    $excerpt = wp_trim_words( onionpress_social_plain_text( $content ), 35, '…' );
    $post_id = wp_insert_post( array(
        'post_type'     => 'post',
        'post_status'   => 'publish',
        'post_title'    => $title,
        'post_content'  => $content,
        'post_excerpt'  => $excerpt,
        'post_date_gmt' => $post_date_gmt,
        'post_date'     => get_date_from_gmt( $post_date_gmt ),
        'meta_input'    => $meta,
    ), true );

    if ( is_wp_error( $post_id ) ) {
        return 'errors';
    }

    if ( function_exists( 'onionpress_social_ensure_category' ) ) {
        $cat_id = onionpress_social_ensure_category( 'reddit' );
        if ( $cat_id ) {
            wp_set_post_categories( $post_id, array( $cat_id ), false );
        }
    }

    if ( $subreddit !== '' ) {
        // Tag with "r/<sub>" so subreddit-based filtering works from the
        // standard WP tag cloud / tag archive UI.
        wp_set_post_tags( $post_id, array( 'r/' . $subreddit ), false );
    }

    return 'imported';
}

function onionpress_reddit_find_post_by_source_id( $source_id ) {
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
 * Parse Reddit's date format. The CSV ships dates like "2018-06-14 13:42:11 UTC"
 * — strtotime handles that directly. Falls back to 0 on unparseable input.
 */
function onionpress_reddit_parse_date( $date ) {
    if ( $date === '' ) return 0;
    $ts = strtotime( $date );
    return $ts ?: 0;
}

/**
 * Rewrite a Reddit permalink to point at the user's preferred base —
 * .onion when the viewer is on .onion, clearnet otherwise. Reddit's
 * CSV emits absolute clearnet URLs; we substitute the host.
 */
function onionpress_reddit_clean_permalink( $url ) {
    if ( $url === '' ) return '';
    $base = onionpress_reddit_base_url();
    return preg_replace( '#^https?://(?:www\.|old\.|new\.)?reddit\.com#i', $base, $url );
}

/**
 * Extract the thread slug from a Reddit URL. Permalink shape:
 *   /r/<sub>/comments/<post_id>/<slug>/[<comment_id>/]
 * Returns the slug, or '' if not parseable.
 */
function onionpress_reddit_thread_slug( $url ) {
    if ( $url === '' ) return '';
    $path = parse_url( $url, PHP_URL_PATH );
    if ( ! $path ) return '';
    if ( preg_match( '#/comments/[^/]+/([^/]+)/?#', $path, $m ) ) {
        return $m[1];
    }
    return '';
}

/**
 * Turn a Reddit URL slug (lower-snake-cased thread title) into a
 * human-readable title approximation. Capitalizes words and replaces
 * underscores with spaces. Used as a fallback until step 4 (live API
 * resolution of actual thread titles) lands.
 */
function onionpress_reddit_humanize_slug( $slug ) {
    $s = str_replace( '_', ' ', $slug );
    return ucwords( $s );
}

/**
 * Conservative Markdown-to-HTML converter for Reddit comment/post bodies.
 * Handles paragraphs, line breaks, bold/italic/code inlines, links,
 * blockquotes, and code fences. Doesn't try to be a full CommonMark
 * implementation — Reddit's markdown subset is small and we already have
 * the raw stored in _raw for any future re-rendering.
 */
function onionpress_reddit_markdown_to_html( $md ) {
    if ( $md === '' ) return '';

    $md = str_replace( "\r\n", "\n", $md );

    // Split into blocks separated by blank lines.
    $blocks = preg_split( "/\n{2,}/", trim( $md ) );
    $out = array();
    foreach ( $blocks as $block ) {
        $block = trim( $block, "\n" );
        if ( $block === '' ) continue;

        // Fenced code block (```)
        if ( preg_match( '/^```\s*\n(.*?)\n```$/sD', $block, $m ) ) {
            $out[] = '<pre><code>' . esc_html( $m[1] ) . '</code></pre>';
            continue;
        }
        // Indented code block — at least four spaces on every line.
        if ( preg_match( '/^(?: {4}|\t).+/s', $block )
             && ! preg_match( '/^\s*>/m', $block ) ) {
            $lines = preg_split( "/\n/", $block );
            $stripped = array_map( function ( $l ) {
                return preg_replace( '/^(?: {4}|\t)/', '', $l );
            }, $lines );
            $out[] = '<pre><code>' . esc_html( implode( "\n", $stripped ) ) . '</code></pre>';
            continue;
        }
        // Blockquote — one or more lines beginning with "> ".
        if ( preg_match( '/^>\s?/', $block ) ) {
            $lines = preg_split( "/\n/", $block );
            $stripped = array_map( function ( $l ) {
                return preg_replace( '/^>\s?/', '', $l );
            }, $lines );
            $inner = onionpress_reddit_markdown_inlines( implode( "\n", $stripped ) );
            $out[] = '<blockquote><p>' . nl2br( $inner ) . '</p></blockquote>';
            continue;
        }
        // Regular paragraph. Inline-format and convert single newlines to <br>.
        $inner = onionpress_reddit_markdown_inlines( $block );
        $out[] = '<p>' . nl2br( $inner ) . '</p>';
    }
    return implode( "\n", $out );
}

/**
 * Inline-only Markdown for paragraph bodies. Handles **bold**, *italic*,
 * `code`, [text](url), and bare u/name / r/sub references. Output is
 * safe to embed inside <p>...</p>.
 */
function onionpress_reddit_markdown_inlines( $text ) {
    $base = onionpress_reddit_base_url();
    // Escape HTML first; we'll re-inject controlled tags below.
    $text = esc_html( $text );

    // Inline code (`code`) — protect from further substitution by stashing.
    $stash = array();
    $text = preg_replace_callback( '/`([^`\n]+)`/', function ( $m ) use ( &$stash ) {
        $key = "\0CODE" . count( $stash ) . "\0";
        $stash[ $key ] = '<code>' . $m[1] . '</code>';
        return $key;
    }, $text );

    // Markdown links [label](url) — accept https/http/relative URLs only.
    $text = preg_replace_callback(
        '/\[([^\]]+)\]\((https?:\/\/[^\s)]+|\/[^\s)]+)\)/',
        function ( $m ) {
            return sprintf(
                '<a href="%s" rel="nofollow noopener">%s</a>',
                esc_url( $m[2] ),
                $m[1]
            );
        },
        $text
    );

    // Bold (**x** or __x__). Order matters: bold first, then italic.
    $text = preg_replace( '/\*\*([^*\n]+)\*\*/', '<strong>$1</strong>', $text );
    $text = preg_replace( '/__([^_\n]+)__/', '<strong>$1</strong>', $text );
    // Italic (*x* or _x_).
    $text = preg_replace( '/(?<![*\w])\*([^*\n]+)\*(?!\w)/', '<em>$1</em>', $text );
    $text = preg_replace( '/(?<![_\w])_([^_\n]+)_(?!\w)/', '<em>$1</em>', $text );

    // Bare subreddit/user references → links. Match /r/sub, r/sub, /u/name, u/name.
    $text = preg_replace_callback(
        '#(?<![\w/])/?(r|u)/([A-Za-z0-9_\-]+)#',
        function ( $m ) use ( $base ) {
            $kind = $m[1];
            $name = $m[2];
            $href = $base . '/' . $kind . '/' . $name;
            return sprintf(
                '<a href="%s" rel="nofollow noopener">%s/%s</a>',
                esc_url( $href ),
                $kind,
                $name
            );
        },
        $text
    );

    // Restore stashed code spans.
    if ( ! empty( $stash ) ) {
        $text = strtr( $text, $stash );
    }
    return $text;
}

function onionpress_reddit_render_recent() {
    $recent = get_posts( array(
        'post_type'      => 'post',
        'posts_per_page' => 10,
        'orderby'        => 'date',
        'order'          => 'DESC',
        'category_name'  => 'reddit',
    ) );
    if ( empty( $recent ) ) {
        echo '<p><em>No Reddit posts imported yet.</em></p>';
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

// ──────────────────────── live sync ────────────────────────

/**
 * Render the sync status panel between the handle form and the CSV
 * upload steps. Shows nothing if no handle is saved yet (sync needs a
 * handle to mean anything).
 */
function onionpress_reddit_render_sync_panel( $handle ) {
    if ( $handle === '' ) {
        return;
    }
    $last_sync = (int) get_option( ONIONPRESS_REDDIT_LAST_SYNC_OPT, 0 );
    $last_note = (string) get_option( ONIONPRESS_REDDIT_LAST_NOTE_OPT, '' );
    $next_run  = wp_next_scheduled( ONIONPRESS_REDDIT_CRON_HOOK );
    ?>
    <h2>Automatic sync &mdash; catches new content as you post</h2>
    <p>Polls Reddit's public listings (over Tor, via Reddit's <code>.onion</code> mirror) every 10 minutes. No password &mdash; just the username above. Catches up to the most recent ~1,000 items per feed; the deep history needs the CSV upload below.</p>
    <table class="form-table" role="presentation">
        <tr>
            <th>Status</th>
            <td>
                <?php if ( $next_run ) : ?>
                    <strong>Enabled</strong> for <code>u/<?php echo esc_html( $handle ); ?></code>.
                    Next automatic run: <?php echo esc_html( human_time_diff( time(), $next_run ) ); ?>.
                <?php else : ?>
                    <strong>Not scheduled.</strong> Save the username above to enable.
                <?php endif; ?>
            </td>
        </tr>
        <tr>
            <th>Last sync</th>
            <td>
                <?php if ( $last_sync ) : ?>
                    <?php echo esc_html( human_time_diff( $last_sync, time() ) ); ?> ago
                    (<?php echo esc_html( gmdate( 'Y-m-d H:i:s', $last_sync ) ); ?> UTC)
                <?php else : ?>
                    Never
                <?php endif; ?>
                <?php if ( $last_note !== '' ) : ?>
                    <br><span style="color:#666;"><?php echo esc_html( $last_note ); ?></span>
                <?php endif; ?>
            </td>
        </tr>
    </table>
    <form method="post" style="margin-bottom:1.5em;">
        <?php wp_nonce_field( 'onionpress_reddit_sync_now', 'onionpress_reddit_sync_nonce' ); ?>
        <input type="hidden" name="onionpress_reddit_sync_now" value="1">
        <?php submit_button( 'Sync now', 'secondary', 'submit', false ); ?>
        <span class="description" style="margin-left:10px;">Runs one tick immediately instead of waiting for the cron.</span>
    </form>
    <?php
}

/**
 * "Sync now" POST handler — runs one tick synchronously and reports the
 * result inline. Returns the same notice shape as the other handlers.
 */
function onionpress_reddit_handle_sync_now_post() {
    $handle = onionpress_reddit_get_saved_handle();
    if ( $handle === '' ) {
        return array( 'level' => 'error', 'message' => 'Save a username first.' );
    }
    $stats = onionpress_reddit_run_sync_tick();
    if ( is_wp_error( $stats ) ) {
        return array( 'level' => 'error', 'message' => 'Sync failed: ' . esc_html( $stats->get_error_message() ) );
    }
    $msg = sprintf(
        'Sync tick complete. Imported: <strong>%d</strong>. Skipped: <strong>%d</strong>. Titles resolved: <strong>%d</strong>. Errors: <strong>%d</strong>.',
        intval( $stats['imported'] ),
        intval( $stats['skipped'] ),
        intval( $stats['resolved'] ?? 0 ),
        intval( $stats['errors'] )
    );
    $level = ( $stats['errors'] > 0 && $stats['imported'] === 0 ) ? 'warning' : 'success';
    return array( 'level' => $level, 'message' => $msg );
}

/**
 * One sync tick: poll both feeds (submitted, comments) newest-first and
 * import anything new. Bails on each feed when it hits an already-imported
 * item — usually within the first page after the initial backfill.
 *
 * Cron-safe: a transient lock prevents two ticks from overlapping (the
 * 10-min cron interval shouldn't collide with itself, but "Sync now"
 * could race the cron).
 */
function onionpress_reddit_run_sync_tick() {
    $handle = onionpress_reddit_get_saved_handle();
    if ( $handle === '' ) {
        return new WP_Error( 'no_handle', 'No Reddit username saved' );
    }

    // Cheap transient-style lock. set_transient with a TTL doubles as
    // automatic expiry if a tick dies mid-run.
    if ( get_transient( ONIONPRESS_REDDIT_LOCK_OPT ) ) {
        return new WP_Error( 'busy', 'Another sync tick is in progress' );
    }
    set_transient( ONIONPRESS_REDDIT_LOCK_OPT, time(), 5 * MINUTE_IN_SECONDS );

    $deadline = time() + ONIONPRESS_REDDIT_TICK_BUDGET_SEC;
    $totals   = array( 'imported' => 0, 'skipped' => 0, 'errors' => 0 );
    $err_note = '';

    foreach ( array( 'submitted', 'comments' ) as $feed ) {
        if ( time() >= $deadline ) {
            $err_note = 'Tick budget exhausted before all feeds were polled';
            break;
        }
        $r = onionpress_reddit_sync_one_feed( $handle, $feed, $deadline );
        if ( is_wp_error( $r ) ) {
            $err_note = $feed . ': ' . $r->get_error_message();
            $totals['errors']++;
            continue;
        }
        foreach ( $totals as $k => $_ ) {
            $totals[ $k ] += (int) ( $r[ $k ] ?? 0 );
        }
    }

    // Tail-end of the tick: resolve any pending parent-thread titles. Cheap
    // when caught up (single meta_query returns empty); otherwise drains a
    // batch per tick until the queue empties.
    if ( time() < $deadline ) {
        $tr = onionpress_reddit_resolve_titles_batch( $deadline );
        $totals['resolved'] = (int) ( $tr['resolved'] ?? 0 );
        if ( ! empty( $tr['errors'] ) ) {
            $totals['errors'] += (int) $tr['errors'];
        }
    }

    delete_transient( ONIONPRESS_REDDIT_LOCK_OPT );
    update_option( ONIONPRESS_REDDIT_LAST_SYNC_OPT, time() );
    update_option(
        ONIONPRESS_REDDIT_LAST_NOTE_OPT,
        $err_note !== ''
            ? $err_note
            : sprintf( 'imported %d, skipped %d', $totals['imported'], $totals['skipped'] )
    );
    return $totals;
}

/**
 * Walk one feed (submitted or comments) newest-first, importing as we
 * go. Stops on: end of available history, tick deadline reached, or
 * the first already-imported item (caught-up signal).
 */
function onionpress_reddit_sync_one_feed( $handle, $feed, $deadline ) {
    $cursor = '';
    $pages  = 0;
    $stats  = array( 'imported' => 0, 'skipped' => 0, 'errors' => 0 );

    while ( $pages < ONIONPRESS_REDDIT_PAGES_PER_TICK ) {
        if ( time() >= $deadline ) {
            break;
        }
        $page = onionpress_reddit_fetch_listing( $handle, $feed, $cursor );
        if ( is_wp_error( $page ) ) {
            return $page;
        }
        $children = $page['data']['children'] ?? array();
        $any_imported = false;
        $hit_known    = false;

        foreach ( $children as $child ) {
            $kind = (string) ( $child['kind'] ?? '' );
            $data = $child['data'] ?? array();
            $row  = $kind === 't3'
                ? onionpress_reddit_api_post_to_row( $data )
                : ( $kind === 't1'
                    ? onionpress_reddit_api_comment_to_row( $data )
                    : null );
            if ( ! $row ) {
                $stats['errors']++;
                continue;
            }
            $result = $kind === 't3'
                ? onionpress_reddit_import_submission( $row )
                : onionpress_reddit_import_comment( $row );
            if ( isset( $stats[ $result ] ) ) {
                $stats[ $result ]++;
            }
            if ( $result === 'imported' ) {
                $any_imported = true;
            } elseif ( $result === 'skipped' ) {
                // First skipped item on a newest-first walk = we've reached
                // the boundary of what we already have. No point continuing.
                $hit_known = true;
            }
        }

        // Reached items we already imported on a prior tick → caught up.
        if ( $hit_known && ! $any_imported ) {
            break;
        }
        $next_after = (string) ( $page['data']['after'] ?? '' );
        if ( $next_after === '' ) {
            break;
        }
        $cursor = $next_after;
        $pages++;
    }
    return $stats;
}

/**
 * Fetch one listing page from Reddit's JSON API over Tor. Returns the
 * decoded JSON object, or WP_Error on failure.
 *
 * `raw_json=1` tells Reddit not to HTML-encode `&`, `<`, `>` in returned
 * strings — important so post bodies come back as clean Markdown.
 */
function onionpress_reddit_fetch_listing( $handle, $feed, $cursor ) {
    $qs = array(
        'limit'    => ONIONPRESS_REDDIT_PER_PAGE,
        'raw_json' => 1,
    );
    if ( $cursor !== '' ) {
        $qs['after'] = $cursor;
    }
    $url = 'https://' . ONIONPRESS_REDDIT_API_HOST
         . '/user/' . rawurlencode( $handle ) . '/' . $feed . '.json?'
         . http_build_query( $qs );

    // Test/mock hook — lets us stub the network in tests.
    $mock = apply_filters( 'onionpress_reddit_fetch_listing_mock', null, $handle, $feed, $cursor );
    if ( $mock !== null ) {
        return $mock;
    }

    $r = onionpress_reddit_api_get( $url );
    if ( is_wp_error( $r ) ) return $r;
    if ( (int) $r['code'] !== 200 ) {
        return new WP_Error( 'reddit_http', 'HTTP ' . $r['code'] . ' from ' . ONIONPRESS_REDDIT_API_HOST );
    }
    return is_array( $r['json'] ) ? $r['json'] : array( 'data' => array( 'children' => array() ) );
}

function onionpress_reddit_api_get( $url ) {
    if ( ! function_exists( 'curl_init' ) ) {
        return new WP_Error( 'no_curl', 'curl extension required for Reddit sync' );
    }
    $ch = curl_init( $url );
    curl_setopt_array( $ch, array(
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_FOLLOWLOCATION => true,
        CURLOPT_MAXREDIRS      => 3,
        CURLOPT_PROXY          => ONIONPRESS_REDDIT_SOCKS_PROXY,
        CURLOPT_PROXYTYPE      => CURLPROXY_SOCKS5_HOSTNAME,
        CURLOPT_TIMEOUT        => ONIONPRESS_REDDIT_HTTP_TIMEOUT,
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
 * Convert a Reddit JSON-API submission record into the same row shape
 * the CSV importer accepts, so both paths share import_submission().
 */
function onionpress_reddit_api_post_to_row( $data ) {
    $ts = (int) ( $data['created_utc'] ?? 0 );
    return array(
        'id'        => (string) ( $data['id'] ?? '' ),
        'permalink' => onionpress_reddit_base_url() . (string) ( $data['permalink'] ?? '' ),
        'date'      => $ts ? gmdate( 'Y-m-d H:i:s', $ts ) . ' UTC' : '',
        'subreddit' => (string) ( $data['subreddit'] ?? '' ),
        'title'     => (string) ( $data['title'] ?? '' ),
        'url'       => (string) ( $data['url'] ?? '' ),
        'body'      => (string) ( $data['selftext'] ?? '' ),
    );
}

function onionpress_reddit_api_comment_to_row( $data ) {
    $ts = (int) ( $data['created_utc'] ?? 0 );
    $permalink = (string) ( $data['permalink'] ?? '' );
    if ( $permalink !== '' ) {
        $permalink = onionpress_reddit_base_url() . $permalink;
    }
    // link_permalink is occasionally absent; fall back to deriving from
    // link_id (t3_<id>) and the subreddit + a stub slug.
    $link = (string) ( $data['link_permalink'] ?? '' );
    return array(
        'id'        => (string) ( $data['id'] ?? '' ),
        'permalink' => $permalink,
        'date'      => $ts ? gmdate( 'Y-m-d H:i:s', $ts ) . ' UTC' : '',
        'subreddit' => (string) ( $data['subreddit'] ?? '' ),
        'body'      => (string) ( $data['body'] ?? '' ),
        'link'      => $link,
        'parent'    => (string) ( $data['parent_id'] ?? '' ),
    );
}

// ──────────────────────── parent-thread title resolution ────────────────────────

function onionpress_reddit_get_title_cache() {
    $c = get_option( ONIONPRESS_REDDIT_TITLE_CACHE_OPT, array() );
    if ( ! is_array( $c ) ) {
        return array();
    }
    // Plain text on the way out too (#472): a cache written before the
    // titles were cleaned at fetch time still holds raw ones.
    foreach ( $c as $id => $title ) {
        $c[ $id ] = onionpress_social_plain_text( $title );
    }
    return $c;
}

function onionpress_reddit_save_title_cache( $cache ) {
    update_option( ONIONPRESS_REDDIT_TITLE_CACHE_OPT, $cache );
}

/**
 * Pull the parent thread fullname (t3_*) out of a comment row.
 * Preferred source: `parent` column when it's already a t3_ (top-level
 * comment). Otherwise reach into the thread `link` URL — Reddit
 * permalinks embed the thread ID at /r/<sub>/comments/<id>/<slug>/.
 * Returns '' when neither source yields one.
 */
function onionpress_reddit_extract_link_id( $row ) {
    $parent = (string) ( $row['parent'] ?? '' );
    if ( strpos( $parent, 't3_' ) === 0 ) {
        return $parent;
    }
    $link = (string) ( $row['link'] ?? '' );
    if ( $link === '' ) {
        return '';
    }
    $path = parse_url( $link, PHP_URL_PATH );
    if ( ! $path ) return '';
    if ( preg_match( '#/comments/([^/]+)#', $path, $m ) ) {
        return 't3_' . $m[1];
    }
    return '';
}

/**
 * Walk pending comment posts (Reddit category, _is_reply=1, no
 * _thread_title_resolved meta), batch-fetch their parents' real titles
 * over Tor via /api/info, retroactively update the post title and the
 * "In reply to" line in the content. Bound by the per-tick deadline
 * and ONIONPRESS_REDDIT_TITLES_PER_TICK.
 *
 * Returns ['resolved' => N, 'errors' => M].
 */
function onionpress_reddit_resolve_titles_batch( $deadline ) {
    $stats = array( 'resolved' => 0, 'errors' => 0 );

    $unresolved = get_posts( array(
        'post_type'      => 'post',
        'posts_per_page' => ONIONPRESS_REDDIT_TITLES_PER_TICK,
        'category_name'  => 'reddit',
        'meta_query'     => array(
            'relation' => 'AND',
            array( 'key' => '_thread_title_resolved', 'compare' => 'NOT EXISTS' ),
            array( 'key' => '_thread_link_id',        'compare' => 'EXISTS' ),
        ),
        'fields'         => 'ids',
        'orderby'        => 'date',
        'order'          => 'ASC',
    ) );
    if ( empty( $unresolved ) ) {
        return $stats;
    }

    // Group post IDs by parent thread so one title update covers all
    // comments on the same thread.
    $by_link = array();
    foreach ( $unresolved as $pid ) {
        $link_id = (string) get_post_meta( $pid, '_thread_link_id', true );
        if ( $link_id === '' ) continue;
        $by_link[ $link_id ][] = (int) $pid;
    }

    $cache = onionpress_reddit_get_title_cache();
    $misses = array();
    foreach ( array_keys( $by_link ) as $link_id ) {
        if ( ! array_key_exists( $link_id, $cache ) ) {
            $misses[] = $link_id;
        }
    }

    // Fetch misses in chunks of 100 (Reddit's /api/info cap).
    foreach ( array_chunk( $misses, 100 ) as $chunk ) {
        if ( time() >= $deadline ) break;
        $titles = onionpress_reddit_fetch_thread_titles( $chunk );
        if ( is_wp_error( $titles ) ) {
            $stats['errors']++;
            continue;
        }
        foreach ( $chunk as $id ) {
            // '' means "Reddit returned no data for this id" — thread
            // deleted, suspended, or otherwise gone. Cache the empty
            // string so we don't retry.
            $cache[ $id ] = $titles[ $id ] ?? '';
        }
    }
    if ( ! empty( $misses ) ) {
        onionpress_reddit_save_title_cache( $cache );
    }

    // Apply cached titles to posts. Posts whose cache entry is empty
    // still get marked resolved so they're not re-queued forever.
    foreach ( $by_link as $link_id => $pids ) {
        if ( ! array_key_exists( $link_id, $cache ) ) {
            continue;
        }
        $title = onionpress_social_plain_text( $cache[ $link_id ] );
        foreach ( $pids as $pid ) {
            if ( $title !== '' ) {
                $post = get_post( $pid );
                $new_content = $post ? onionpress_reddit_swap_inreplyto_label(
                    (string) $post->post_content, $title
                ) : null;
                $update = array( 'ID' => $pid, 'post_title' => 'Re: ' . $title );
                if ( $new_content !== null && $new_content !== $post->post_content ) {
                    $update['post_content'] = $new_content;
                }
                wp_update_post( $update );
            }
            update_post_meta( $pid, '_thread_title_resolved', '1' );
            $stats['resolved']++;
        }
    }
    return $stats;
}

/**
 * Batch lookup of thread titles via Reddit's /api/info endpoint.
 * Accepts an array of t3_ fullnames, returns assoc(fullname → title).
 * Missing entries (deleted threads) won't appear in the result.
 */
function onionpress_reddit_fetch_thread_titles( $link_ids ) {
    if ( empty( $link_ids ) ) return array();
    $url = 'https://' . ONIONPRESS_REDDIT_API_HOST
         . '/api/info.json?'
         . http_build_query( array(
             'id'       => implode( ',', $link_ids ),
             'raw_json' => 1,
         ) );

    $mock = apply_filters( 'onionpress_reddit_fetch_thread_titles_mock', null, $link_ids );
    if ( $mock !== null ) return $mock;

    $r = onionpress_reddit_api_get( $url );
    if ( is_wp_error( $r ) ) return $r;
    if ( (int) $r['code'] !== 200 ) {
        return new WP_Error( 'reddit_http', 'HTTP ' . $r['code'] . ' from /api/info' );
    }
    $kids = $r['json']['data']['children'] ?? array();
    $out = array();
    foreach ( $kids as $child ) {
        $id = (string) ( $child['data']['name'] ?? '' );
        if ( $id === '' ) continue;
        // raw_json=1 leaves `<` and `>` unescaped; the title is cached
        // and stored as a post title, so plain text here (#472).
        $out[ $id ] = onionpress_social_plain_text( $child['data']['title'] ?? '' );
    }
    return $out;
}

/**
 * Rewrite the visible label inside the "In reply to" link in post
 * content so it matches the newly-resolved title. Idempotent — running
 * twice with the same title is a no-op. Leaves the href untouched.
 */
function onionpress_reddit_swap_inreplyto_label( $content, $new_label ) {
    return preg_replace_callback(
        '#(<p><em>In reply to <a href="[^"]+" rel="nofollow noopener">)([^<]+)(</a></em></p>)#',
        function ( $m ) use ( $new_label ) {
            return $m[1] . esc_html( $new_label ) . $m[3];
        },
        $content,
        1
    );
}

/**
 * Recursive rmdir. Defined here only if the Twitter plugin (which also
 * ships one) hasn't loaded first. Both copies behave identically.
 */
if ( ! function_exists( 'onionpress_reddit_rrmdir' ) ) {
    function onionpress_reddit_rrmdir( $dir ) {
        if ( ! is_dir( $dir ) ) {
            return;
        }
        $it = new RecursiveIteratorIterator(
            new RecursiveDirectoryIterator( $dir, FilesystemIterator::SKIP_DOTS ),
            RecursiveIteratorIterator::CHILD_FIRST
        );
        foreach ( $it as $f ) {
            if ( $f->isDir() ) {
                @rmdir( $f->getRealPath() );
            } else {
                @unlink( $f->getRealPath() );
            }
        }
        @rmdir( $dir );
    }
}

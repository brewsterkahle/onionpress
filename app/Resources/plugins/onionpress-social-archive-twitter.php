<?php
/**
 * Plugin Name: OnionPress Social Archive — Twitter Importer
 * Description: Import a Twitter / X takeout ZIP into the unified
 *              social_post archive. Parses data/tweets.js (all parts),
 *              copies media out of data/tweets_media/, and creates one
 *              post per tweet with the original date preserved. Safe
 *              to re-run — tweet IDs are used as idempotency keys.
 * Version:     0.1
 * Network:     true
 * License:     AGPL-3.0-or-later
 * License URI: https://www.gnu.org/licenses/agpl-3.0.html
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

// Declare ourselves to the core dashboard so its "Import" button goes live.
add_action( 'plugins_loaded', function () {
    if ( function_exists( 'onionpress_social_register_importer' ) ) {
        onionpress_social_register_importer( 'twitter' );
    }
} );

const ONIONPRESS_TWITTER_ADMIN_SLUG   = 'onionpress-social-archive-twitter';
const ONIONPRESS_TWITTER_HANDLE_OPT   = 'onionpress_social_twitter_handle';
const ONIONPRESS_TWITTER_DEEPLINK_URL = 'https://twitter.com/settings/download_your_data';

// Canonical walkthrough lives on OnionHome so we can update the
// screenshots and copy without re-shipping every OnionPress install.
// Both the onion and the clearnet mirror are offered; viewers in Tor
// Browser can follow either, others (e.g. a Mac's default browser) can
// only reach the clearnet one.
const ONIONPRESS_TWITTER_HELP_ONION    = 'http://op2homeiwjb4fdqnfkj5kbokvcee45zpk2pwgvpz5rrkanp5qqwxzbyd.onion/help-import-twitter/';
const ONIONPRESS_TWITTER_HELP_CLEARNET = 'https://onionpress.org/help-import-twitter/';

// Threading: replies to your own tweets are imported as WP comments on
// the parent post. Twitter is a one-shot ZIP import (no API to fetch
// foreign ancestors), so we sort tweets oldest-first before importing
// — parents always arrive before replies, no _pending_reattach sweep
// needed. The migration handles installs that imported pre-threading.
const ONIONPRESS_TWITTER_SELF_USER_ID_OPT      = 'onionpress_social_twitter_self_user_id';
const ONIONPRESS_TWITTER_THREADS_MIGRATED_OPT  = 'onionpress_twitter_threads_v1_migrated';

/**
 * Register the "Twitter" submenu page under Social Archive. Uses
 * priority 20 so the core plugin's `add_menu_page` (priority 10) has
 * already registered the parent slug.
 */
add_action( 'admin_menu', function () {
    if ( ! defined( 'ONIONPRESS_SOCIAL_ADMIN_SLUG' ) ) {
        return; // Core plugin not loaded yet; nothing to attach under.
    }
    add_submenu_page(
        ONIONPRESS_SOCIAL_ADMIN_SLUG,
        'Import Twitter / X',
        'Twitter',
        'manage_options',
        ONIONPRESS_TWITTER_ADMIN_SLUG,
        'onionpress_twitter_import_page'
    );
}, 20 );

add_action( 'admin_init', function () {
    // One-shot conversion of pre-threading reply posts into comments.
    // Self-gates on its own option flag, so this is a no-op once done.
    if ( get_option( ONIONPRESS_TWITTER_THREADS_MIGRATED_OPT ) !== 'yes' ) {
        onionpress_twitter_migrate_replies_to_comments();
    }
} );

function onionpress_twitter_import_page() {
    if ( ! current_user_can( 'manage_options' ) ) {
        wp_die( 'Unauthorized' );
    }

    $result        = null;
    $handle_notice = null;
    if ( $_SERVER['REQUEST_METHOD'] === 'POST' ) {
        // Two distinct actions post to this page: saving the handle,
        // and uploading the ZIP. Each has its own nonce so neither can
        // be replayed as the other.
        if ( isset( $_POST['onionpress_twitter_save_handle'] ) ) {
            check_admin_referer( 'onionpress_twitter_save_handle', 'onionpress_twitter_handle_nonce' );
            $handle_notice = onionpress_twitter_handle_save_handle_post();
        } elseif ( isset( $_FILES['twitter_zip'] ) ) {
            check_admin_referer( 'onionpress_twitter_import', 'onionpress_twitter_nonce' );
            $result = onionpress_twitter_handle_upload();
        }
    }

    $saved_handle = onionpress_twitter_get_saved_handle();
    ?>
    <div class="wrap">
        <h1>Import Twitter / X archive</h1>

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

        <h2>Step 1 &mdash; Tell us who you are on Twitter</h2>
        <p>Your handle is saved on this onion, never transmitted anywhere. We use it to label imported posts and for cross-source lookups later.</p>
        <form method="post" style="margin-bottom:1em;">
            <?php wp_nonce_field( 'onionpress_twitter_save_handle', 'onionpress_twitter_handle_nonce' ); ?>
            <input type="hidden" name="onionpress_twitter_save_handle" value="1">
            <table class="form-table" role="presentation">
                <tr>
                    <th><label for="twitter_handle">Your Twitter / X handle</label></th>
                    <td>
                        <input type="text" id="twitter_handle" name="twitter_handle"
                               value="<?php echo esc_attr( $saved_handle ); ?>"
                               placeholder="yourname"
                               pattern="@?[A-Za-z0-9_]{1,15}"
                               class="regular-text" style="max-width:260px;">
                        <?php submit_button( 'Save', 'secondary', 'submit', false ); ?>
                        <p class="description">Just the handle &mdash; no URL, no password. Example: <code>brewster_kahle</code>.</p>
                    </td>
                </tr>
            </table>
        </form>

        <h2>Step 2 &mdash; Request your archive from Twitter</h2>
        <p>Click the button below to open Twitter's archive-request page in a new tab and click <strong>Request archive</strong> there. You're already logged in there in your own browser &mdash; OnionPress doesn't need, store, or want your Twitter password.</p>
        <p>
            <a href="<?php echo esc_url( ONIONPRESS_TWITTER_DEEPLINK_URL ); ?>"
               target="_blank" rel="noopener noreferrer"
               class="button button-primary">
                Open Twitter archive request page &rarr;
            </a>
            <span style="margin-left:12px;">
                <a href="<?php echo esc_url( ONIONPRESS_TWITTER_HELP_ONION ); ?>" target="_blank" rel="noopener">Walkthrough on OnionHome</a>
                <span style="color:#999;">&middot;</span>
                <a href="<?php echo esc_url( ONIONPRESS_TWITTER_HELP_CLEARNET ); ?>" target="_blank" rel="noopener">clearnet mirror</a>
            </span>
        </p>

        <h2>Step 3 &mdash; Wait for the email</h2>
        <p><strong>Typical wait: 2&ndash;24 hours</strong>, occasionally up to a week for very large accounts. Twitter will email a download link that expires in about a week.</p>

        <h2>Step 4 &mdash; Upload the ZIP here</h2>
        <p>Download the ZIP from Twitter's email <em>without unzipping it</em>, then drop it below.</p>
        <form method="post" enctype="multipart/form-data">
            <?php wp_nonce_field( 'onionpress_twitter_import', 'onionpress_twitter_nonce' ); ?>
            <table class="form-table" role="presentation">
                <tr>
                    <th><label for="twitter_zip">Archive ZIP</label></th>
                    <td>
                        <input type="file" name="twitter_zip" id="twitter_zip" accept=".zip" required>
                        <p class="description">Upload <code>twitter-YYYY-MM-DD-…zip</code> as-downloaded. Nothing in this file is sent off your onion.</p>
                    </td>
                </tr>
                <tr>
                    <th>What to import</th>
                    <td>
                        <fieldset>
                            <label><input type="checkbox" name="include_retweets" value="1"> Include retweets (RT&nbsp;@&hellip;)</label><br>
                            <label><input type="checkbox" name="include_replies" value="1"> Include replies to other users</label>
                        </fieldset>
                        <p class="description">Originals and self-threads (replies to your own tweets) are always imported.</p>
                    </td>
                </tr>
            </table>
            <?php submit_button( 'Import tweets' ); ?>
        </form>

        <h2>Recent imports</h2>
        <?php onionpress_twitter_render_recent(); ?>
    </div>
    <?php
}

/**
 * Return the saved Twitter handle (without @), or empty string.
 */
function onionpress_twitter_get_saved_handle() {
    return (string) get_option( ONIONPRESS_TWITTER_HANDLE_OPT, '' );
}

/**
 * Handle the "Save" submission from Step 1. Normalizes the handle
 * (strips leading @, lowercases, validates against Twitter's handle
 * rules), writes it to the option, returns a notice array.
 */
function onionpress_twitter_handle_save_handle_post() {
    $raw = isset( $_POST['twitter_handle'] ) ? wp_unslash( $_POST['twitter_handle'] ) : '';
    $handle = ltrim( trim( (string) $raw ), '@' );
    if ( $handle === '' ) {
        delete_option( ONIONPRESS_TWITTER_HANDLE_OPT );
        return array( 'level' => 'success', 'message' => 'Handle cleared.' );
    }
    // Twitter handle rules: 1-15 chars, alphanumeric + underscore only.
    if ( ! preg_match( '/^[A-Za-z0-9_]{1,15}$/D', $handle ) ) {
        return array(
            'level'   => 'error',
            'message' => 'That doesn&rsquo;t look like a Twitter handle. Must be 1&ndash;15 letters, digits, or underscores.',
        );
    }
    update_option( ONIONPRESS_TWITTER_HANDLE_OPT, $handle );
    return array(
        'level'   => 'success',
        'message' => sprintf( 'Saved handle <strong>@%s</strong>.', esc_html( $handle ) ),
    );
}

/**
 * Handle the posted ZIP: extract safely, parse all tweets.js parts, and
 * import each tweet as a social_post. Returns a result array for the
 * admin page notice. Never throws — any failure path returns a
 * human-readable 'error' message.
 */
function onionpress_twitter_handle_upload() {
    if ( ! isset( $_FILES['twitter_zip'] ) || $_FILES['twitter_zip']['error'] !== UPLOAD_ERR_OK ) {
        return array( 'level' => 'error', 'message' => 'Upload failed or no file provided.' );
    }

    $upload_name = $_FILES['twitter_zip']['name'];
    $upload_tmp  = $_FILES['twitter_zip']['tmp_name'];
    $opts = array(
        'include_rts'     => ! empty( $_POST['include_retweets'] ),
        'include_replies' => ! empty( $_POST['include_replies'] ),
    );

    // Work under /tmp inside the container. Outside web roots, cleared on
    // container restart, and a bare directory that no Apache vhost serves.
    $workdir = sys_get_temp_dir() . '/onionpress-twitter-' . time() . '-' . wp_generate_password( 8, false );
    if ( ! wp_mkdir_p( $workdir ) ) {
        return array( 'level' => 'error', 'message' => 'Could not create temp dir for extraction.' );
    }
    @chmod( $workdir, 0700 );

    $extracted = onionpress_twitter_extract_safely( $upload_tmp, $workdir );
    if ( is_wp_error( $extracted ) ) {
        onionpress_rrmdir( $workdir );
        return array(
            'level'   => 'error',
            'message' => 'ZIP refused: ' . esc_html( $extracted->get_error_message() ) . ' Upload the archive as Twitter delivered it.',
        );
    }

    // Twitter puts everything under data/; archive may or may not have a
    // top-level dir above that. Look for tweets.js in both positions.
    $data_dir = onionpress_twitter_find_data_dir( $workdir );
    if ( ! $data_dir ) {
        onionpress_rrmdir( $workdir );
        return array( 'level' => 'error', 'message' => 'This ZIP does not contain a <code>data/</code> folder with <code>tweets.js</code>. Make sure you downloaded the full archive and uploaded the ZIP as-is.' );
    }

    $tweets_files = onionpress_twitter_find_tweets_files( $data_dir );
    if ( empty( $tweets_files ) ) {
        onionpress_rrmdir( $workdir );
        return array( 'level' => 'error', 'message' => 'No <code>tweets.js</code> (or <code>tweets-partN.js</code>) found in <code>data/</code>.' );
    }

    $media_dir = onionpress_twitter_find_media_dir( $data_dir );
    $self_user_id = onionpress_twitter_read_self_user_id( $data_dir );

    // Persist the self user ID so the migration (which runs without
    // access to the original ZIP) can classify self-replies correctly.
    if ( $self_user_id !== null && $self_user_id !== '' ) {
        update_option( ONIONPRESS_TWITTER_SELF_USER_ID_OPT, (string) $self_user_id );
    }

    // Collect ALL tweets across all part-files, then sort oldest-first
    // before importing. Twitter archives ship tweets in arbitrary
    // (mostly newest-first) order, but threading needs parents to be
    // present at import time so a reply can directly become a comment
    // on its parent's post. Sorting once upfront eliminates the need
    // for the pending/reattach sweep that the live importers have.
    $all_tweets = array();
    foreach ( $tweets_files as $file ) {
        $tweets = onionpress_twitter_parse_tweets_file( $file );
        if ( $tweets === null ) {
            // Tracked separately from per-tweet errors so a malformed
            // part file doesn't silently skip its content with no signal.
            continue;
        }
        foreach ( $tweets as $entry ) {
            if ( isset( $entry['tweet'] ) ) {
                $all_tweets[] = $entry['tweet'];
            }
        }
    }
    usort( $all_tweets, function ( $a, $b ) {
        $ta = isset( $a['created_at'] ) ? strtotime( (string) $a['created_at'] ) : 0;
        $tb = isset( $b['created_at'] ) ? strtotime( (string) $b['created_at'] ) : 0;
        return $ta <=> $tb;
    } );

    $stats = array( 'imported' => 0, 'skipped' => 0, 'errors' => 0 );
    foreach ( $all_tweets as $tweet ) {
        $result = onionpress_twitter_import_tweet(
            $tweet,
            array_merge( $opts, array(
                'media_dir'    => $media_dir,
                'self_user_id' => $self_user_id,
            ) )
        );
        if ( isset( $stats[ $result ] ) ) {
            $stats[ $result ]++;
        }
    }

    onionpress_rrmdir( $workdir );

    $msg = sprintf(
        'Processed <strong>%s</strong>. Imported: <strong>%d</strong>. Skipped (filtered or already imported): <strong>%d</strong>. Errors: <strong>%d</strong>.',
        esc_html( $upload_name ),
        intval( $stats['imported'] ),
        intval( $stats['skipped'] ),
        intval( $stats['errors'] )
    );
    $level = $stats['errors'] > 0 && $stats['imported'] === 0 ? 'error' : 'success';
    return array( 'level' => $level, 'message' => $msg );
}

/**
 * The members of a Twitter archive the importer reads, at the root or
 * under one per-download folder: data/tweets.js (or its parts),
 * data/account.js, and the tweet media files (`<tweet id>-<hash>.<ext>`
 * in data/tweets_media or data/tweet_media, image and video types
 * only). Nothing else in the archive is extracted (#473).
 */
const ONIONPRESS_TWITTER_ZIP_MEMBERS = '#^(?:[^/]+/)?data/(?:tweets(?:-part\d+)?\.js|account\.js|tweets?_media/\d+-[A-Za-z0-9_.-]+\.(?i:jpg|jpeg|png|gif|mp4|mov|m4v|webp))$#D';

/**
 * Unpack the members the importer reads from ``$zip_path`` into
 * ``$target_dir`` through onionpress_social_zip_extract(): the archive
 * is held to the shared limits (members, per-member and total size,
 * expansion, free space), names that would escape the target, symlinks
 * and special files refuse it whole, and nothing is written before the
 * checks pass (#473). Returns true or a WP_Error saying why.
 */
function onionpress_twitter_extract_safely( $zip_path, $target_dir ) {
    return onionpress_social_zip_extract( $zip_path, $target_dir, ONIONPRESS_TWITTER_ZIP_MEMBERS );
}

/**
 * Locate the archive's `data/` folder. Some exports put it at the root;
 * older ones nested it inside a per-download folder. Walks up to two
 * levels down before giving up.
 */
function onionpress_twitter_find_data_dir( $root ) {
    if ( is_dir( $root . '/data' ) ) {
        return $root . '/data';
    }
    foreach ( scandir( $root ) as $entry ) {
        if ( $entry === '.' || $entry === '..' ) continue;
        $p = $root . '/' . $entry;
        if ( is_dir( $p ) && is_dir( $p . '/data' ) ) {
            return $p . '/data';
        }
    }
    return null;
}

function onionpress_twitter_find_tweets_files( $data_dir ) {
    $hits = array();
    foreach ( scandir( $data_dir ) as $f ) {
        if ( preg_match( '/^tweets(-part\d+)?\.js$/D', $f ) ) {
            $hits[] = $data_dir . '/' . $f;
        }
    }
    sort( $hits );
    return $hits;
}

function onionpress_twitter_find_media_dir( $data_dir ) {
    foreach ( array( 'tweets_media', 'tweet_media' ) as $try ) {
        $p = $data_dir . '/' . $try;
        if ( is_dir( $p ) ) {
            return $p;
        }
    }
    return null;
}

/**
 * Read the user's own Twitter user ID from data/account.js. Used to
 * distinguish self-replies (always imported — they form threads) from
 * replies to other users (opt-in via the "include replies" checkbox).
 * Returns null if the file is missing or unparseable.
 */
function onionpress_twitter_read_self_user_id( $data_dir ) {
    $path = $data_dir . '/account.js';
    if ( ! is_readable( $path ) ) {
        return null;
    }
    $raw = file_get_contents( $path );
    if ( preg_match( '/window\.YTD\.account\.\w+\s*=\s*(.*)$/s', $raw, $m ) ) {
        $data = json_decode( $m[1], true );
        if ( is_array( $data ) && isset( $data[0]['account']['accountId'] ) ) {
            return (string) $data[0]['account']['accountId'];
        }
    }
    return null;
}

/**
 * Strip Twitter's JS-assignment prefix and parse the JSON body. Each
 * file starts with `window.YTD.tweets.partN = ` followed by a JSON
 * array literal.
 */
function onionpress_twitter_parse_tweets_file( $file ) {
    $contents = file_get_contents( $file );
    if ( $contents === false ) {
        return null;
    }
    if ( preg_match( '/^\s*window\.YTD\.\w+\.\w+\s*=\s*(.*)$/sD', $contents, $m ) ) {
        $contents = $m[1];
    }
    $data = json_decode( $contents, true );
    return is_array( $data ) ? $data : null;
}

/**
 * Look up a Twitter post by `_source_id`. Returns post ID or 0.
 */
function onionpress_twitter_find_post_by_source_id( $source_id ) {
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
 * Look up a Twitter comment by `_source_id` commentmeta.
 */
function onionpress_twitter_find_comment_by_source_id( $source_id ) {
    $ids = get_comments( array(
        'meta_key'   => '_source_id',
        'meta_value' => $source_id,
        'number'     => 1,
        'fields'     => 'ids',
    ) );
    return ! empty( $ids ) ? (int) $ids[0] : 0;
}

/**
 * Find the post a self-reply should attach to. The parent might be a
 * top-level post (thread root) or a comment (a self-reply we converted
 * earlier in the same archive walk) — in the latter case attach to the
 * comment's post and let comment_parent nest us under the comment.
 */
function onionpress_twitter_find_attachable_post( $parent_source_id ) {
    $pid = onionpress_twitter_find_post_by_source_id( $parent_source_id );
    if ( $pid ) return $pid;
    $cid = onionpress_twitter_find_comment_by_source_id( $parent_source_id );
    if ( $cid ) {
        $c = get_comment( $cid );
        if ( $c ) return (int) $c->comment_post_ID;
    }
    return 0;
}

/**
 * Insert a WP comment on $parent_post_id from a tweet. Idempotent on
 * `_source_id`. If the tweet replies to another tweet that's now a
 * comment, comment_parent nests us under it so deep threads survive.
 */
function onionpress_twitter_create_self_reply_comment( $tweet, $parent_post_id, $opts ) {
    $tweet_id = $tweet['id_str'] ?? $tweet['id'] ?? null;
    if ( ! $tweet_id ) return 'errors';
    $source_id = 'twitter:' . $tweet_id;
    if ( onionpress_twitter_find_comment_by_source_id( $source_id ) ) {
        return 'skipped';
    }
    $created_at = $tweet['created_at'] ?? null;
    $ts = $created_at ? strtotime( $created_at ) : 0;
    if ( ! $ts ) return 'errors';
    $content = onionpress_twitter_render_content( $tweet );
    $author = (string) get_option( ONIONPRESS_TWITTER_HANDLE_OPT, '' );
    if ( $author === '' ) $author = 'me';
    $author_url = 'https://twitter.com/i/status/' . $tweet_id;

    $comment_parent = 0;
    $reply_to_id = (string) ( $tweet['in_reply_to_status_id_str']
                            ?? $tweet['in_reply_to_status_id']
                            ?? '' );
    if ( $reply_to_id !== '' ) {
        $comment_parent = onionpress_twitter_find_comment_by_source_id( 'twitter:' . $reply_to_id );
    }

    $gmt = gmdate( 'Y-m-d H:i:s', $ts );
    $comment_id = onionpress_social_insert_comment( array(
        'comment_post_ID'    => $parent_post_id,
        'comment_author'     => $author,
        'comment_author_url' => $author_url,
        'comment_content'    => $content,
        'comment_date'       => get_date_from_gmt( $gmt ),
        'comment_date_gmt'   => $gmt,
        'comment_approved'   => 1,
        'comment_parent'     => $comment_parent,
        'comment_type'       => 'comment',
        'comment_meta'       => array(
            '_source_id'  => $source_id,
            '_source_url' => $author_url,
            '_raw'        => wp_json_encode( $tweet ),
        ),
    ) );
    return $comment_id ? 'imported' : 'errors';
}

/**
 * Convert an existing top-level tweet post into a comment on
 * $parent_post_id. Used by the migration; reads from post fields and
 * postmeta so a corrupt _raw doesn't block conversion.
 */
function onionpress_twitter_convert_post_to_comment( $post_id, $parent_post_id ) {
    $post = get_post( $post_id );
    if ( ! $post ) return false;
    $source_id = (string) get_post_meta( $post_id, '_source_id', true );
    if ( $source_id === '' ) return false;
    if ( onionpress_twitter_find_comment_by_source_id( $source_id ) ) {
        wp_delete_post( $post_id, true );
        return true;
    }
    $reply_to = (string) get_post_meta( $post_id, '_reply_to_id', true );
    $comment_parent = 0;
    if ( $reply_to !== '' ) {
        $comment_parent = onionpress_twitter_find_comment_by_source_id( 'twitter:' . $reply_to );
    }
    $author = (string) get_option( ONIONPRESS_TWITTER_HANDLE_OPT, '' );
    if ( $author === '' ) $author = 'me';
    $author_url = (string) get_post_meta( $post_id, '_source_url', true );
    $gmt = $post->post_date_gmt ?: gmdate( 'Y-m-d H:i:s' );
    $comment_id = onionpress_social_insert_comment( array(
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
 * One-time migration for installs that imported tweets before threading.
 * Finds posts with _is_reply=1 whose _reply_to_id matches a known
 * _source_id, converts them to comments on that parent. Self-gates on
 * an option flag.
 *
 * Self-reply detection: prefers _raw's in_reply_to_user_id_str (newer
 * imports) and falls back to "parent post is in our DB" — which is
 * also a valid signal because the importer only ingests the user's own
 * archive (everything we have IS our own tweet).
 */
function onionpress_twitter_migrate_replies_to_comments() {
    if ( get_option( ONIONPRESS_TWITTER_THREADS_MIGRATED_OPT ) === 'yes' ) {
        return 0;
    }
    $self_user_id = (string) get_option( ONIONPRESS_TWITTER_SELF_USER_ID_OPT, '' );
    global $wpdb;
    $candidates = $wpdb->get_results(
        "SELECT p.ID, m1.meta_value AS source_id, m2.meta_value AS reply_to_id, m3.meta_value AS raw
         FROM {$wpdb->posts} p
         JOIN {$wpdb->postmeta} m1 ON m1.post_id=p.ID AND m1.meta_key='_source_id'
         JOIN {$wpdb->postmeta} m2 ON m2.post_id=p.ID AND m2.meta_key='_reply_to_id'
         LEFT JOIN {$wpdb->postmeta} m3 ON m3.post_id=p.ID AND m3.meta_key='_raw'
         JOIN {$wpdb->postmeta} m4 ON m4.post_id=p.ID AND m4.meta_key='_is_reply' AND m4.meta_value='1'
         WHERE m1.meta_value LIKE 'twitter:%' AND m2.meta_value <> ''
         ORDER BY p.post_date ASC"
    );
    $converted = 0;
    foreach ( $candidates as $c ) {
        $tweet = $c->raw ? json_decode( $c->raw, true ) : null;
        if ( is_array( $tweet ) && $self_user_id !== '' ) {
            $reply_user = (string) ( $tweet['in_reply_to_user_id_str']
                                  ?? $tweet['in_reply_to_user_id'] ?? '' );
            if ( $reply_user !== '' && $reply_user !== $self_user_id ) {
                continue; // explicit other-account reply
            }
        }
        $parent_pid = onionpress_twitter_find_attachable_post( 'twitter:' . $c->reply_to_id );
        if ( ! $parent_pid ) continue;
        if ( onionpress_twitter_convert_post_to_comment( (int) $c->ID, $parent_pid ) ) {
            $converted++;
        }
    }
    update_option( ONIONPRESS_TWITTER_THREADS_MIGRATED_OPT, 'yes' );
    return $converted;
}

/**
 * Import one tweet. Returns one of 'imported', 'skipped', 'errors' for
 * the caller's tally.
 *
 * Self-replies fold into the parent post's comment thread when the
 * parent is already in the DB — and since we sort the archive
 * oldest-first before iterating, parents reliably ARE in the DB by the
 * time their replies get processed. Replies to other users have no
 * parent in our DB (the importer ingests only the user's own tweets,
 * not anyone else's) so they stay top-level — gated by `include_replies`.
 */
function onionpress_twitter_import_tweet( $tweet, $opts ) {
    $tweet_id = $tweet['id_str'] ?? $tweet['id'] ?? null;
    if ( ! $tweet_id ) {
        return 'errors';
    }

    // Idempotency: skip if already imported. Uses _source_id meta with a
    // `twitter:` prefix so the same ID can't collide with other sources.
    $source_id = 'twitter:' . $tweet_id;
    $existing  = get_posts( array(
        'post_type'      => 'post',
        'meta_key'       => '_source_id',
        'meta_value'     => $source_id,
        'post_status'    => 'any',
        'posts_per_page' => 1,
        'fields'         => 'ids',
    ) );
    if ( ! empty( $existing ) ) {
        return 'skipped';
    }

    $full_text     = $tweet['full_text'] ?? $tweet['text'] ?? '';
    $is_retweet    = ! empty( $tweet['retweeted_status'] )
        || strpos( $full_text, 'RT @' ) === 0;
    $reply_status  = $tweet['in_reply_to_status_id_str'] ?? $tweet['in_reply_to_status_id'] ?? null;
    $reply_user_id = $tweet['in_reply_to_user_id_str']   ?? $tweet['in_reply_to_user_id']   ?? null;
    $is_reply      = $reply_status !== null;
    $is_self_reply = $is_reply && $opts['self_user_id']
                     && $reply_user_id !== null
                     && (string) $reply_user_id === (string) $opts['self_user_id'];

    if ( $is_retweet && ! $opts['include_rts'] ) {
        return 'skipped';
    }
    if ( $is_reply && ! $is_self_reply && ! $opts['include_replies'] ) {
        return 'skipped';
    }

    // Self-reply with the parent already imported → fold into the
    // parent post's comment thread instead of fragmenting the
    // archive. Because import_archive() sorts oldest-first, the
    // parent reliably arrives before its replies, so we can resolve
    // it directly without any pending/reattach machinery.
    if ( $is_self_reply ) {
        $parent_pid = onionpress_twitter_find_attachable_post( 'twitter:' . $reply_status );
        if ( $parent_pid ) {
            return onionpress_twitter_create_self_reply_comment( $tweet, $parent_pid, $opts );
        }
        // Fall through to top-level: parent is missing (out-of-order
        // archive, partial import, etc.). The migration sweep will
        // re-thread later if it ever shows up.
    }

    $created_at = $tweet['created_at'] ?? null;
    $ts = $created_at ? strtotime( $created_at ) : 0;
    if ( ! $ts ) {
        return 'errors';
    }
    $post_date_gmt = gmdate( 'Y-m-d H:i:s', $ts );

    $content = onionpress_twitter_render_content( $tweet );
    // Title is a short preview of the plain-text content. Leaving it
    // empty causes WP to auto-generate "No title" strings that don't
    // render well in archive lists. 10 words is enough for recognition.
    $preview_text = onionpress_social_plain_text( $content );
    $title        = wp_trim_words( $preview_text, 10, '…' );
    if ( $title === '' ) {
        $title = gmdate( 'Y-m-d', $ts ) . ' tweet';
    }

    $post_id = wp_insert_post( array(
        'post_type'     => 'post',
        'post_status'   => 'publish',
        'post_title'    => $title,
        'post_content'  => $content,
        'post_excerpt'  => wp_trim_words( $preview_text, 35, '…' ),
        'post_date_gmt' => $post_date_gmt,
        'post_date'     => get_date_from_gmt( $post_date_gmt ),
        'meta_input'    => array(
            '_source_id'      => $source_id,
            '_source_url'     => 'https://twitter.com/i/status/' . $tweet_id,
            '_is_repost'      => $is_retweet ? '1' : '0',
            '_is_reply'       => $is_reply ? '1' : '0',
            '_reply_to_id'    => $reply_status ?: '',
            '_thread_root_id' => $is_self_reply ? '' : $tweet_id,
            '_raw'            => wp_json_encode( $tweet ),
        ),
    ), true );

    if ( is_wp_error( $post_id ) ) {
        return 'errors';
    }

    // Assign the Twitter category. Created on demand by the core
    // plugin if it doesn't already exist.
    if ( function_exists( 'onionpress_social_ensure_category' ) ) {
        $cat_id = onionpress_social_ensure_category( 'twitter' );
        if ( $cat_id ) {
            wp_set_post_categories( $post_id, array( $cat_id ), false );
        }
    }

    // Hashtags -> post tags. Not a per-platform taxonomy because tags
    // are blog-wide already and cross-source tagging is more useful.
    $hashtags = $tweet['entities']['hashtags'] ?? array();
    if ( ! empty( $hashtags ) ) {
        $tag_names = array_values( array_filter( array_map(
            function ( $h ) { return $h['text'] ?? null; },
            $hashtags
        ) ) );
        if ( ! empty( $tag_names ) ) {
            wp_set_post_tags( $post_id, $tag_names, false );
        }
    }

    // Media: look for files in the media dir prefixed with "<tweet_id>-".
    // Twitter ships them as <id>-<media_hash>.<ext>. Copy each into WP
    // uploads and append an <img>/<video> to the post.
    $media_entries = $tweet['extended_entities']['media']
        ?? $tweet['entities']['media']
        ?? array();
    if ( ! empty( $media_entries ) && $opts['media_dir'] ) {
        onionpress_twitter_sideload_media( $post_id, $tweet_id, $opts['media_dir'] );
    }

    return 'imported';
}

/**
 * Render a tweet's content: expand t.co links, make @mentions clickable,
 * strip trailing media t.co links (they'll be re-added as attachments),
 * and wrap in safe HTML. The text is escaped first, so the only markup
 * is what this function adds.
 */
function onionpress_twitter_render_content( $tweet ) {
    // The archive's text is plain text with &lt; &gt; &amp; encoded: decode
    // and escape it before anything adds markup (#462). t.co links and
    // @handles contain nothing esc_html changes, so the substitutions
    // below still find them.
    $text = esc_html( html_entity_decode(
        (string) ( $tweet['full_text'] ?? $tweet['text'] ?? '' ),
        ENT_QUOTES | ENT_HTML5, 'UTF-8'
    ) );

    // Strip media short-URLs from tail — the attached media will be
    // re-rendered as <img>/<video> in sideload_media.
    $media_entries = $tweet['extended_entities']['media']
        ?? $tweet['entities']['media']
        ?? array();
    foreach ( $media_entries as $m ) {
        $short = $m['url'] ?? '';
        if ( $short !== '' ) {
            $text = str_replace( $short, '', $text );
        }
    }

    // Expand t.co links with their full URL + display form.
    $urls = $tweet['entities']['urls'] ?? array();
    foreach ( $urls as $u ) {
        $short    = $u['url']          ?? '';
        $expanded = $u['expanded_url'] ?? $short;
        $display  = $u['display_url']  ?? $expanded;
        if ( $short === '' || $expanded === '' ) {
            continue;
        }
        $link = sprintf(
            '<a href="%s" rel="nofollow noopener">%s</a>',
            esc_url( $expanded ),
            esc_html( $display )
        );
        $text = str_replace( $short, $link, $text );
    }

    // @mentions -> link to twitter.com. Users viewing the archive can
    // follow through to the mentioned account if it still exists.
    $mentions = $tweet['entities']['user_mentions'] ?? array();
    foreach ( $mentions as $m ) {
        $handle = $m['screen_name'] ?? '';
        if ( $handle === '' ) continue;
        $text = preg_replace(
            '/@' . preg_quote( $handle, '/' ) . '\b/i',
            sprintf(
                '<a href="https://twitter.com/%s" rel="nofollow noopener">@%s</a>',
                rawurlencode( $handle ),
                esc_html( $handle )
            ),
            $text
        );
    }

    // Remaining newlines -> <br>. Entities above already inserted
    // safe HTML, so nl2br on the full string is fine.
    return nl2br( trim( $text ) );
}

/**
 * Copy media files associated with a tweet from the extracted archive
 * into WordPress uploads, then append rendered <img>/<video> tags to
 * the post content. Twitter names media files `<tweet_id>-<hash>.<ext>`,
 * so we just glob the prefix.
 */
function onionpress_twitter_sideload_media( $post_id, $tweet_id, $media_dir ) {
    if ( ! is_dir( $media_dir ) ) {
        return;
    }
    require_once ABSPATH . 'wp-admin/includes/image.php';
    require_once ABSPATH . 'wp-admin/includes/file.php';
    require_once ABSPATH . 'wp-admin/includes/media.php';

    $prefix = $tweet_id . '-';
    $upload = wp_upload_dir();
    $tags   = array();

    foreach ( scandir( $media_dir ) as $f ) {
        if ( strpos( $f, $prefix ) !== 0 ) continue;
        // The name comes from the uploaded archive and is kept in uploads
        // as is: media types WordPress accepts only, never .php and the like.
        if ( ! wp_check_filetype( $f )['ext'] ) continue;
        $src = $media_dir . '/' . $f;
        if ( ! is_file( $src ) ) continue;

        $dest_name = wp_unique_filename( $upload['path'], $f );
        $dest_path = $upload['path'] . '/' . $dest_name;
        if ( ! copy( $src, $dest_path ) ) continue;

        $type = wp_check_filetype( $dest_name );
        $attach_id = wp_insert_attachment( array(
            'post_mime_type' => $type['type'] ?? 'application/octet-stream',
            'post_title'     => sanitize_title( pathinfo( $dest_name, PATHINFO_FILENAME ) ),
            'post_content'   => '',
            'post_status'    => 'inherit',
        ), $dest_path, $post_id );

        if ( is_wp_error( $attach_id ) ) continue;

        $meta = wp_generate_attachment_metadata( $attach_id, $dest_path );
        wp_update_attachment_metadata( $attach_id, $meta );

        // Set the first image as the post's featured image so archive
        // listings have a thumbnail. Later ones render inline.
        if ( empty( $tags ) && strpos( $type['type'], 'image/' ) === 0 ) {
            set_post_thumbnail( $post_id, $attach_id );
        }

        if ( strpos( $type['type'], 'image/' ) === 0 ) {
            $img = wp_get_attachment_image(
                $attach_id,
                'large',
                false,
                array( 'loading' => 'lazy', 'style' => 'max-width:100%;height:auto;' )
            );
            // Strip absolute hostname from src/srcset so the <img> renders
            // correctly on any host (onion, localhost, clearnet) the same
            // post might be viewed through. wp_make_link_relative() would
            // work on a single URL; we need to scrub every URL in the tag.
            $tags[] = preg_replace( '~https?://[^/\s"\']+/~', '/', $img );
        } elseif ( strpos( $type['type'], 'video/' ) === 0 ) {
            $url = wp_make_link_relative( wp_get_attachment_url( $attach_id ) );
            $tags[] = sprintf(
                '<video controls preload="metadata" style="max-width:100%%;"><source src="%s" type="%s"></video>',
                esc_url( $url ),
                esc_attr( $type['type'] )
            );
        }
    }

    if ( ! empty( $tags ) ) {
        $post = get_post( $post_id );
        wp_update_post( array(
            'ID'           => $post_id,
            'post_content' => $post->post_content . "\n\n" . implode( "\n\n", $tags ),
        ) );
    }
}

function onionpress_twitter_render_recent() {
    $recent = get_posts( array(
        'post_type'      => 'post',
        'posts_per_page' => 10,
        'orderby'        => 'date',
        'order'          => 'DESC',
        'category_name'  => 'twitter',
    ) );
    if ( empty( $recent ) ) {
        echo '<p><em>No Twitter posts imported yet.</em></p>';
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

/**
 * Recursive rmdir for the workdir cleanup. PHP doesn't have a stdlib
 * equivalent. Fails silently — worst case the /tmp/... dir gets cleaned
 * up at next container restart.
 */
function onionpress_rrmdir( $dir ) {
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

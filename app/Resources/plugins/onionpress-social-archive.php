<?php
/**
 * Plugin Name: OnionPress Social Archive
 * Description: Imports posts from social platforms (Twitter/X, Mastodon,
 *              Bluesky, …) into the regular WordPress blog as ordinary
 *              posts, tagged with a per-source category so each platform
 *              gets a standard `/category/<source>/` tab and everything
 *              mingles chronologically with your own writing. Per-source
 *              importers are separate mu-plugins that register a submenu
 *              under this one.
 * Version:     0.2
 * Network:     true
 * License:     AGPL-3.0-or-later
 * License URI: https://www.gnu.org/licenses/agpl-3.0.html
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

const ONIONPRESS_SOCIAL_ADMIN_SLUG = 'onionpress-social-archive';

/**
 * wp_insert_comment() for every importer comment (#462). Unlike
 * wp_new_comment() it runs no comment filter (wp_filter_comment), and
 * WordPress does not kses comment_text on output, so the author, its URL
 * and the content are cleaned here, before they are stored. The author
 * may be anyone's display name (Bluesky/Mastodon thread ancestors).
 */
function onionpress_social_insert_comment( $args ) {
    $args['comment_author']     = sanitize_text_field( (string) ( $args['comment_author'] ?? '' ) );
    $args['comment_author_url'] = esc_url_raw( (string) ( $args['comment_author_url'] ?? '' ) );
    $args['comment_content']    = wp_kses_post( (string) ( $args['comment_content'] ?? '' ) );
    return wp_insert_comment( $args );
}

/**
 * Canonical list of supported sources. Each entry carries:
 *   - label:    human-facing platform name (e.g. "Twitter / X")
 *   - cat_name: display name for the WP category
 *   - cat_slug: URL slug for the WP category, ends up in /category/<slug>/
 *   - color:    hex string used in the admin dashboard
 *
 * Importers can extend via the `onionpress_social_sources` filter.
 */
function onionpress_social_sources() {
    $sources = array(
        'twitter'  => array(
            'label'       => 'Twitter / X',
            'cat_name'    => 'Twitter',
            'cat_slug'    => 'twitter',
            'color'       => '#1da1f2',
            'nav_label'   => 'My Tweets',
            'brand_label' => 'X-Twitter',
        ),
        'mastodon' => array(
            'label'       => 'Mastodon',
            'cat_name'    => 'Mastodon',
            'cat_slug'    => 'mastodon',
            'color'       => '#6364ff',
            'nav_label'   => 'My Toots',
            'brand_label' => 'Mastodon',
        ),
        'bluesky'  => array(
            'label'       => 'Bluesky',
            'cat_name'    => 'Bluesky',
            'cat_slug'    => 'bluesky',
            'color'       => '#0085ff',
            'nav_label'   => 'My Skeets',
            'brand_label' => 'Bluesky',
        ),
        'reddit'   => array(
            'label'       => 'Reddit',
            'cat_name'    => 'Reddit',
            'cat_slug'    => 'reddit',
            'color'       => '#ff4500',
            'nav_label'   => 'My Reddit',
            'brand_label' => 'Reddit',
        ),
        'blog'     => array(
            'label'       => 'Blog',
            'cat_name'    => 'Blog',
            'cat_slug'    => 'blog',
            'color'       => '#21759b',
            'nav_label'   => 'My Blog',
            'brand_label' => 'WordPress',
        ),
    );
    return apply_filters( 'onionpress_social_sources', $sources );
}

/**
 * Inline brand-logo SVG for a source slug. Sized to 1em × 1em,
 * `fill="currentColor"` so the caller controls color via CSS.
 * Returns '' for unknown slugs so callers can fall back gracefully.
 *
 * Logos are simplified single-path marks — enough to read as the
 * platform's brand at small sizes (20-24px) without pulling in a
 * whole icon font dependency.
 */
function onionpress_social_source_logo_svg( $slug ) {
    static $paths = array(
        // X (post-2023 Twitter) — two crossed strokes.
        'twitter-x'    => 'M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z',
        // Classic Twitter bird.
        'twitter-bird' => 'M23 3a10.9 10.9 0 0 1-3.14 1.53 4.48 4.48 0 0 0-7.86 3v1A10.66 10.66 0 0 1 3 4s-4 9 5 13a11.64 11.64 0 0 1-7 2c9 5 20 0 20-11.5a4.5 4.5 0 0 0-.08-.83A7.72 7.72 0 0 0 23 3z',
        // Default Twitter slug → X (current branding).
        'twitter'      => 'M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z',
        // Mastodon — the "M" mark inside rounded rectangle.
        'mastodon'     => 'M23.193 7.88c0-5.207-3.411-6.733-3.411-6.733C18.062.357 15.108.1 12.041.078h-.076c-3.068.022-6.02.279-7.74 1.069 0 0-3.412 1.526-3.412 6.733 0 1.193-.023 2.619.015 4.13.124 5.092.933 10.109 5.637 11.354 2.168.574 4.03.695 5.528.612 2.717-.151 4.242-.97 4.242-.97l-.09-1.974s-1.94.613-4.12.538c-2.16-.075-4.436-.232-4.786-2.88a5.43 5.43 0 0 1-.048-.744s2.118.517 4.801.64c1.641.075 3.18-.096 4.742-.283 2.996-.357 5.607-2.2 5.937-3.884.52-2.652.477-6.472.477-6.472zm-4.03 6.72h-2.504V8.47c0-1.29-.543-1.944-1.628-1.944-1.2 0-1.802.776-1.802 2.312v3.349h-2.49V8.836c0-1.536-.602-2.312-1.802-2.312-1.085 0-1.628.655-1.628 1.945V14.6H4.805V8.283c0-1.289.328-2.313.987-3.07.68-.758 1.569-1.147 2.674-1.147 1.278 0 2.246.491 2.886 1.474l.622 1.042.623-1.042c.64-.983 1.608-1.474 2.886-1.474 1.104 0 1.994.389 2.674 1.146.658.758.986 1.782.986 3.071z',
        // Bluesky — the butterfly mark.
        'bluesky'      => 'M5.064 3.843c2.671 2.006 5.546 6.075 6.603 8.258.17.35.17.549 0 .898-1.057 2.184-3.932 6.252-6.603 8.259C3.495 22.45 1 20.41 1 19.124c0-1.286.725-4.06 1.176-5.006.45-.946 3.226-2.183 4.706-2.183-1.48 0-4.256-1.237-4.706-2.183C1.725 8.806 1 6.032 1 4.747c0-1.286 2.495-3.327 4.064-.904zm13.872 0c-2.671 2.006-5.546 6.075-6.603 8.258-.17.35-.17.549 0 .898 1.057 2.184 3.932 6.252 6.603 8.259C20.505 22.45 23 20.41 23 19.124c0-1.286-.725-4.06-1.176-5.006-.45-.946-3.226-2.183-4.706-2.183 1.48 0 4.256-1.237 4.706-2.183.451-.946 1.176-3.72 1.176-5.006 0-1.286-2.495-3.327-4.064-.904z',
        // Reddit — Snoo alien in circle (simple-icons-derived single path).
        'reddit'       => 'M12 0A12 12 0 0 0 0 12a12 12 0 0 0 12 12 12 12 0 0 0 12-12A12 12 0 0 0 12 0zm5.01 4.744c.688 0 1.25.561 1.25 1.249a1.25 1.25 0 0 1-2.498.056l-2.597-.547-.8 3.747c1.824.07 3.48.632 4.674 1.488.308-.309.73-.491 1.207-.491.968 0 1.754.786 1.754 1.754 0 .716-.435 1.333-1.01 1.614a3.111 3.111 0 0 1 .042.52c0 2.694-3.13 4.87-7.004 4.87-3.874 0-7.004-2.176-7.004-4.87 0-.183.015-.366.043-.534A1.748 1.748 0 0 1 4.028 12.5c0-.968.786-1.754 1.754-1.754.463 0 .898.196 1.207.49 1.207-.883 2.878-1.43 4.744-1.487l.885-4.182a.342.342 0 0 1 .14-.197.35.35 0 0 1 .238-.042l2.906.617a1.214 1.214 0 0 1 1.108-.701zM9.25 12C8.561 12 8 12.562 8 13.25c0 .687.561 1.248 1.25 1.248.687 0 1.248-.561 1.248-1.249 0-.688-.561-1.249-1.249-1.249zm5.5 0c-.687 0-1.248.561-1.248 1.25 0 .687.561 1.248 1.249 1.248.688 0 1.249-.561 1.249-1.249 0-.687-.562-1.249-1.25-1.249zm-5.466 3.99a.327.327 0 0 0-.231.094.33.33 0 0 0 0 .463c.842.842 2.484.913 2.961.913.477 0 2.105-.056 2.961-.913a.361.361 0 0 0 .029-.463.33.33 0 0 0-.464 0c-.547.533-1.684.73-2.512.73-.828 0-1.979-.196-2.512-.73a.326.326 0 0 0-.232-.095z',
        // Blog — WordPress "W in circle" (simple-icons-derived single path).
        'blog'         => 'M21.469 6.825c.84 1.537 1.318 3.3 1.318 5.175 0 3.979-2.156 7.456-5.363 9.325l3.295-9.527c.615-1.54.82-2.771.82-3.864 0-.405-.026-.78-.07-1.11m-7.981.105c.647-.03 1.232-.105 1.232-.105.582-.075.514-.93-.067-.899 0 0-1.755.135-2.88.135-1.064 0-2.85-.135-2.85-.135-.585-.03-.661.855-.075.885 0 0 .54.075 1.125.105l1.68 4.605-2.37 7.08L5.354 6.9c.649-.03 1.234-.1 1.234-.1.585-.075.516-.93-.065-.896 0 0-1.746.138-2.874.138-.2 0-.438-.008-.69-.015C4.911 3.15 8.235 1.215 12 1.215c2.809 0 5.365 1.072 7.286 2.833-.046-.003-.091-.009-.141-.009-1.06 0-1.812.923-1.812 1.914 0 .89.513 1.643 1.06 2.531.411.72.89 1.643.89 2.977 0 .915-.354 1.994-.821 3.479l-1.075 3.585-3.9-11.61.001.014zM12 22.784c-1.059 0-2.081-.153-3.048-.437l3.237-9.406 3.315 9.087c.024.053.05.101.078.149-1.121.395-2.327.612-3.582.612M1.211 12c0-1.564.336-3.05.935-4.39L7.29 21.709C3.694 19.96 1.212 16.271 1.211 12M12 0C5.385 0 0 5.385 0 12s5.385 12 12 12 12-5.385 12-12S18.615 0 12 0',
    );
    if ( ! isset( $paths[ $slug ] ) ) return '';
    return '<svg class="op-social-logo" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" focusable="false"><path d="' . $paths[ $slug ] . '"/></svg>';
}

/**
 * Era-correct badge logo for one imported post. Twitter is a moving
 * target: posts created before the 2023-07-23 X rebrand announcement
 * show the classic bird; posts after that date show the X. Other
 * sources just get their single brand mark.
 *
 * Why announcement-date (July 23 2023) rather than domain-cutover
 * (May 2024): we want "era the person posted under," not "when the
 * backend domain flipped." The name change was immediate and
 * culturally visible the day of the announcement.
 */
function onionpress_social_post_badge_logo_svg( $source_slug, $post_id ) {
    if ( $source_slug !== 'twitter' ) {
        return onionpress_social_source_logo_svg( $source_slug );
    }
    static $cutoff = null;
    if ( $cutoff === null ) { $cutoff = strtotime( '2023-07-23 00:00:00 UTC' ); }
    $post_gmt = get_post_field( 'post_date_gmt', $post_id, 'raw' );
    $ts       = $post_gmt ? strtotime( $post_gmt . ' UTC' ) : 0;
    $variant  = ( $ts && $ts < $cutoff ) ? 'bird' : 'x';
    return onionpress_social_source_logo_svg( 'twitter-' . $variant );
}

/**
 * Nav-tab icon HTML for a source. Twitter gets a small composite of
 * X and bird separated by a slash to signal the account spans both
 * eras; other sources get their single mark. Callers wrap this in
 * the .op-social-nav-icon span; Twitter returns its own composite
 * wrapper so the "X / bird" alignment is self-contained.
 */
function onionpress_social_nav_icon_html( $source_slug ) {
    if ( $source_slug === 'twitter' ) {
        return '<span class="op-social-nav-icon op-social-nav-icon--combo" aria-hidden="true">'
             . onionpress_social_source_logo_svg( 'twitter-x' )
             . '<span class="op-social-nav-icon__slash">/</span>'
             . onionpress_social_source_logo_svg( 'twitter-bird' )
             . '</span>';
    }
    $logo = onionpress_social_source_logo_svg( $source_slug );
    return $logo ? '<span class="op-social-nav-icon" aria-hidden="true">' . $logo . '</span>' : '';
}

/**
 * Inject per-source nav items into a custom primary nav menu via
 * the `wp_nav_menu_items` filter. Only applies when the user has
 * configured a primary menu AND has imported posts for that source;
 * sites without configured menus use the theme's fallback nav,
 * which has parallel logic in header.php.
 *
 * Each item is icon + text label; the text collapses to visually-
 * hidden on narrow screens via the CSS below, leaving just the
 * platform logo.
 */
function onionpress_social_inject_nav_items( $items, $args ) {
    if ( ! is_object( $args ) || ( isset( $args->theme_location ) && $args->theme_location !== 'primary' ) ) {
        return $items;
    }
    $extra = '';
    foreach ( onionpress_social_sources() as $slug => $info ) {
        $term = get_term_by( 'slug', $info['cat_slug'], 'category' );
        if ( ! $term || is_wp_error( $term ) || $term->count < 1 ) {
            continue;
        }
        $url   = get_term_link( $term );
        if ( is_wp_error( $url ) ) {
            continue;
        }
        $extra .= sprintf(
            '<li class="menu-item"><a class="op-social-nav op-social-nav--%s" href="%s" style="--op-accent:%s;" aria-label="%s">%s<span class="op-social-nav-label">%s</span></a></li>',
            esc_attr( $slug ),
            esc_url( $url ),
            esc_attr( $info['color'] ),
            esc_attr( $info['nav_label'] ),
            onionpress_social_nav_icon_html( $slug ),
            esc_html( $info['nav_label'] )
        );
    }
    return $items . $extra;
}
add_filter( 'wp_nav_menu_items', 'onionpress_social_inject_nav_items', 10, 2 );

/**
 * Ensure a category term exists for *source_slug* and return its term ID.
 * Cached per-request so repeated lookups during a large import are cheap.
 * Creates the category on first use — no admin step required.
 */
function onionpress_social_ensure_category( $source_slug ) {
    static $cache = array();
    if ( isset( $cache[ $source_slug ] ) ) {
        return $cache[ $source_slug ];
    }
    $sources = onionpress_social_sources();
    if ( ! isset( $sources[ $source_slug ] ) ) {
        return 0;
    }
    $info = $sources[ $source_slug ];
    $term = get_term_by( 'slug', $info['cat_slug'], 'category' );
    if ( $term && ! is_wp_error( $term ) ) {
        return $cache[ $source_slug ] = (int) $term->term_id;
    }
    $result = wp_insert_term(
        $info['cat_name'],
        'category',
        array(
            'slug'        => $info['cat_slug'],
            'description' => sprintf( 'Posts imported from %s.', $info['label'] ),
        )
    );
    if ( is_wp_error( $result ) ) {
        return 0;
    }
    return $cache[ $source_slug ] = (int) $result['term_id'];
}

/**
 * Count posts currently tagged with the given source's category.
 * Used by the admin dashboard.
 */
function onionpress_social_count_for_source( $source_slug ) {
    $sources = onionpress_social_sources();
    if ( ! isset( $sources[ $source_slug ] ) ) {
        return 0;
    }
    $term = get_term_by( 'slug', $sources[ $source_slug ]['cat_slug'], 'category' );
    if ( ! $term || is_wp_error( $term ) ) {
        return 0;
    }
    return (int) $term->count;
}

/**
 * One-shot rewrite-rule flush on first activation. mu-plugins have no
 * activation hook, so version-gate the flush via a WP option: flush
 * once, record, never again (until the version tag bumps). Per-site on
 * multisite because each site stores its own rewrite_rules option.
 */
add_action( 'wp_loaded', function () {
    if ( get_option( 'onionpress_social_archive_rewrite_version' ) !== '0.2' ) {
        flush_rewrite_rules( false );
        update_option( 'onionpress_social_archive_rewrite_version', '0.2' );
    }
}, 10 );

/**
 * Top-level admin menu "Social Archive". Importer sibling plugins
 * register submenu pages under this slug. The landing page is a
 * dashboard — per-source counts and per-source "Import" buttons.
 */
add_action( 'admin_menu', function () {
    add_menu_page(
        'Social Archive',
        'Social Archive',
        'manage_options',
        ONIONPRESS_SOCIAL_ADMIN_SLUG,
        'onionpress_social_archive_dashboard',
        'dashicons-format-chat',
        25
    );
    add_submenu_page(
        ONIONPRESS_SOCIAL_ADMIN_SLUG,
        'Social Archive Dashboard',
        'Dashboard',
        'manage_options',
        ONIONPRESS_SOCIAL_ADMIN_SLUG,
        'onionpress_social_archive_dashboard'
    );
}, 10 );

function onionpress_social_archive_dashboard() {
    $sources = onionpress_social_sources();
    ?>
    <div class="wrap">
        <h1>Social Archive</h1>
        <p>Import your posts from social platforms into this blog as regular posts. Each platform gets its own category tab (e.g. <code>/category/twitter/</code>); everything interleaves chronologically on your main blog and in the category archives.</p>

        <h2>Per-source</h2>
        <table class="wp-list-table widefat striped" style="max-width:720px;">
            <thead>
                <tr>
                    <th>Source</th>
                    <th style="width:140px;">Posts</th>
                    <th style="width:240px;">Action</th>
                </tr>
            </thead>
            <tbody>
                <?php foreach ( $sources as $slug => $info ) :
                    $count         = onionpress_social_count_for_source( $slug );
                    $importer_slug = ONIONPRESS_SOCIAL_ADMIN_SLUG . '-' . $slug;
                    $import_url    = admin_url( 'admin.php?page=' . $importer_slug );
                    $has_importer  = onionpress_social_importer_registered( $slug );
                    $cat_term      = get_term_by( 'slug', $info['cat_slug'], 'category' );
                    $cat_url       = $cat_term ? get_term_link( $cat_term ) : '';
                    ?>
                    <?php $home_url = onionpress_social_source_home_url( $slug ); ?>
                    <tr>
                        <td>
                            <span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:<?php echo esc_attr( $info['color'] ); ?>;margin-right:6px;"></span>
                            <?php if ( $home_url ) : ?>
                                <a href="<?php echo esc_url( $home_url ); ?>" target="_blank" rel="noopener"><strong><?php echo esc_html( $info['label'] ); ?></strong></a>
                            <?php else : ?>
                                <strong><?php echo esc_html( $info['label'] ); ?></strong>
                            <?php endif; ?>
                        </td>
                        <td>
                            <?php echo intval( $count ); ?>
                            <?php if ( $count > 0 && $cat_url && ! is_wp_error( $cat_url ) ) : ?>
                                &middot; <a href="<?php echo esc_url( $cat_url ); ?>">view</a>
                            <?php endif; ?>
                        </td>
                        <td>
                            <?php if ( $has_importer ) : ?>
                                <a href="<?php echo esc_url( $import_url ); ?>" class="button button-primary">Import</a>
                            <?php else : ?>
                                <em>Importer not installed</em>
                            <?php endif; ?>
                        </td>
                    </tr>
                <?php endforeach; ?>

                <?php
                // Wayback Machine row — not a source, but a related
                // archival sink. Shows how many posts (across all
                // subsites) are captured by Internet Archive's Wayback
                // Machine via Save Page Now, driven by the sibling
                // wayback plugin's daemon.
                if ( function_exists( 'onionpress_wayback_queue_totals' ) ) :
                    $wb = onionpress_wayback_queue_totals();
                    $wb_total     = (int) $wb['total'];
                    $wb_archived  = (int) $wb['archived'];
                    $wb_in_flight = (int) $wb['in_flight'];
                    $wb_pct       = $wb_total > 0 ? round( $wb_archived * 100 / $wb_total, 1 ) : 0;
                    $wb_home      = onionpress_social_wayback_home_url();
                    $wb_details   = admin_url( 'admin.php?page=onionpress-wayback' );
                ?>
                    <tr>
                        <td>
                            <span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:#7c3aed;margin-right:6px;"></span>
                            <a href="<?php echo esc_url( $wb_home ); ?>" target="_blank" rel="noopener"><strong>Wayback Machine</strong></a>
                            <br><small style="color:#666;">Internet Archive &middot; all subsites</small>
                        </td>
                        <td>
                            <strong><?php echo number_format_i18n( $wb_archived ); ?></strong>
                            <?php if ( $wb_total > 0 ) : ?>
                                <small style="color:#666;">
                                    / <?php echo number_format_i18n( $wb_total ); ?>
                                    &middot; <?php echo esc_html( $wb_pct ); ?>%
                                </small>
                            <?php endif; ?>
                            <?php if ( $wb_in_flight > 0 ) : ?>
                                <br><small style="color:#666;">+ <?php echo number_format_i18n( $wb_in_flight ); ?> in flight</small>
                            <?php endif; ?>
                        </td>
                        <td>
                            <?php // The Wayback page is the owner's (onionpress-wayback-archive.php). ?>
                            <?php if ( is_super_admin() ) : ?>
                                <a href="<?php echo esc_url( $wb_details ); ?>" class="button">Details</a>
                            <?php endif; ?>
                        </td>
                    </tr>
                <?php endif; ?>
            </tbody>
        </table>

    </div>
    <?php
}

/**
 * Has a sibling importer plugin for *source_slug* been loaded?
 *
 * Importers register themselves via
 * `onionpress_social_register_importer( 'twitter' )` at plugins_loaded
 * time. The dashboard uses this to show an active "Import" button vs.
 * a greyed "not installed" notice.
 */
/**
 * Wayback Machine URL, context-aware: onion when the user is viewing
 * this site via .onion (so their clicks don't leak to clearnet),
 * https://web.archive.org/ otherwise.
 */
function onionpress_social_wayback_home_url() {
    $host = (string) ( $_SERVER['HTTP_HOST'] ?? '' );
    if ( substr( $host, -6 ) === '.onion' ) {
        return 'https://web.archivep75mbjunhxc6x4j5mwjmomyxb573v42baldlqu56ruil2oiad.onion/';
    }
    return 'https://web.archive.org/';
}

/**
 * Resolve a "home" URL for a source's platform, preferring a link to
 * the user's own profile when we know their handle, falling back to
 * the platform homepage. Used by the dashboard to make the source
 * names clickable.
 */
function onionpress_social_source_home_url( $slug ) {
    $handle = (string) get_option( 'onionpress_social_' . $slug . '_handle', '' );
    switch ( $slug ) {
        case 'twitter':
            if ( $handle !== '' ) {
                // Twitter handles imported from archives can come with or without @.
                return 'https://x.com/' . ltrim( $handle, '@' );
            }
            return 'https://x.com/';
        case 'mastodon':
            if ( $handle !== '' && strpos( $handle, '@' ) !== false ) {
                // Mastodon handle is "user@server" (no leading @).
                $parts = explode( '@', trim( $handle, '@' ) );
                if ( count( $parts ) === 2 ) {
                    return 'https://' . $parts[1] . '/@' . $parts[0];
                }
            }
            return 'https://joinmastodon.org/';
        case 'bluesky':
            if ( $handle !== '' ) {
                return 'https://bsky.app/profile/' . ltrim( $handle, '@' );
            }
            return 'https://bsky.app/';
        case 'reddit':
            // Reddit runs an official .onion mirror; prefer it when the user
            // is viewing this site via .onion so profile clicks don't leak
            // to clearnet. Falls back to www.reddit.com otherwise.
            $host = (string) ( $_SERVER['HTTP_HOST'] ?? '' );
            $base = substr( $host, -6 ) === '.onion'
                ? 'https://www.reddittorjg6rue252oqsxryoxengawnmo46qy4kyii5wtqnwfj4ooad.onion'
                : 'https://www.reddit.com';
            if ( $handle !== '' ) {
                return $base . '/user/' . ltrim( $handle, '@' );
            }
            return $base . '/';
    }
    return '';
}

function onionpress_social_importer_registered( $slug ) {
    $registered = apply_filters( 'onionpress_social_importers', array() );
    return in_array( $slug, $registered, true );
}

function onionpress_social_register_importer( $slug ) {
    add_filter( 'onionpress_social_importers', function ( $list ) use ( $slug ) {
        $list[] = $slug;
        return array_values( array_unique( $list ) );
    } );
}

/**
 * When on a category archive page, narrow the built-in WP Archives
 * widget's month list + post counts to posts IN that category.
 *
 * Without these filters the "Archives" widget's counts are site-wide,
 * so on /category/twitter/ a month with 80 tweets + 1 original blog
 * post would read "(81)", which is misleading. Filtered:
 * "(80) tweets in Oct 2022" etc.
 *
 * Also flips on show_post_count for category archives so the user
 * doesn't have to reconfigure the widget manually — on category
 * pages the counts are now the point.
 *
 * Generic (not social-specific): applies whenever is_category() is
 * true, so a regular category also gets filtered monthly counts.
 */
add_filter( 'getarchives_join', function ( $join, $parsed_args ) {
    global $wpdb;
    if ( ! is_category() ) {
        return $join;
    }
    $term = get_queried_object();
    if ( ! ( $term instanceof WP_Term ) ) {
        return $join;
    }
    return $join
        . " INNER JOIN {$wpdb->term_relationships} tr ON {$wpdb->posts}.ID = tr.object_id"
        . " INNER JOIN {$wpdb->term_taxonomy} tt ON tr.term_taxonomy_id = tt.term_taxonomy_id";
}, 10, 2 );

add_filter( 'getarchives_where', function ( $where, $parsed_args ) {
    global $wpdb;
    if ( ! is_category() ) {
        return $where;
    }
    $term = get_queried_object();
    if ( ! ( $term instanceof WP_Term ) ) {
        return $where;
    }
    return $where . $wpdb->prepare(
        ' AND tt.taxonomy = %s AND tt.term_id = %d',
        'category',
        $term->term_id
    );
}, 10, 2 );

add_filter( 'widget_archives_args', function ( $args ) {
    if ( is_category() ) {
        $args['show_post_count'] = true;
    }
    return $args;
} );

/**
 * Rewrite the Archives widget's monthly links so clicking a month on
 * /category/<slug>/ stays in the category instead of jumping to the
 * global month archive. The default link is /YYYY/MM/ and the built-in
 * archives widget emits that regardless of context.
 *
 * We rewrite to /YYYY/MM/?category_name=<slug>. WP honors this to
 * narrow the main query to that category for that month — clicking a
 * month from /category/mastodon/ now stays on Mastodon-only posts.
 *
 * Applied to any category archive (not just social).
 */
add_filter( 'get_archives_link', function ( $link_html ) {
    if ( ! is_category() ) {
        return $link_html;
    }
    $term = get_queried_object();
    if ( ! ( $term instanceof WP_Term ) ) {
        return $link_html;
    }
    $slug = $term->slug;
    // The emitted link tag looks like: <li><a href='https://host/2026/04/'>April 2026</a>&nbsp;(80)</li>
    // Append ?category_name=<slug> to the href.
    return preg_replace_callback(
        '#(<a\s[^>]*href=[\'"])([^\'"]+)([\'"])#i',
        function ( $m ) use ( $slug ) {
            $url = $m[2];
            $glue = ( strpos( $url, '?' ) === false ) ? '?' : '&amp;';
            return $m[1] . $url . $glue . 'category_name=' . rawurlencode( $slug ) . $m[3];
        },
        $link_html,
        1
    );
} );

/**
 * Prepend the platform logo to the Archives widget title when viewing
 * one of our social-source category archives. Turns plain "Archives"
 * into e.g. "<mastodon-logo> Archives" so the widget obviously reflects
 * the category it's filtered to.
 *
 * Covers both classic widgets (widget_title filter) and Gutenberg
 * block themes (render_block filter on core/heading with text
 * "Archives"). Most modern WP themes emit block widgets.
 */
add_filter( 'widget_title', function ( $title, $instance = array(), $id_base = '' ) {
    if ( ! is_category() ) return $title;
    if ( $id_base !== '' && $id_base !== 'archives' ) return $title;
    return onionpress_social_maybe_prefix_archives_title( $title );
}, 10, 3 );

add_filter( 'render_block', function ( $block_content, $block ) {
    if ( ! is_category() ) return $block_content;
    if ( empty( $block['blockName'] ) || $block['blockName'] !== 'core/heading' ) return $block_content;
    // Only match a heading whose text content (stripped of tags) is
    // exactly "Archives" — avoid touching unrelated headings.
    $text = trim( wp_strip_all_tags( $block_content ) );
    if ( $text !== 'Archives' ) return $block_content;
    $logo_html = onionpress_social_current_category_logo_html();
    if ( ! $logo_html ) return $block_content;
    // Insert the logo just after the opening heading tag, preserving
    // whatever classes/attrs the theme chose.
    return preg_replace(
        '#(<h[1-6][^>]*>)#i',
        '$1' . $logo_html . ' ',
        $block_content,
        1
    );
}, 10, 2 );

function onionpress_social_maybe_prefix_archives_title( $title ) {
    $logo_html = onionpress_social_current_category_logo_html();
    if ( ! $logo_html ) return $title;
    return '<span class="op-social-archive-widget-title">' . $logo_html . ' ' . $title . '</span>';
}

/**
 * If the current query is a social-source category, return the inline
 * logo HTML (wrapped in an accent-colored span); otherwise ''.
 */
function onionpress_social_current_category_logo_html() {
    $term = get_queried_object();
    if ( ! ( $term instanceof WP_Term ) ) return '';
    if ( ! function_exists( 'onionpress_social_nav_icon_html' ) ) return '';
    foreach ( onionpress_social_sources() as $slug => $info ) {
        if ( $info['cat_slug'] === $term->slug ) {
            $logo = onionpress_social_nav_icon_html( $slug );
            if ( $logo ) {
                return '<span class="op-social-archive-widget-title" style="--op-accent:'
                    . esc_attr( $info['color'] ) . ';">'
                    . $logo . '</span>';
            }
            return '';
        }
    }
    return '';
}

add_filter( 'widget_archives_dropdown_args', function ( $args ) {
    if ( is_category() ) {
        $args['show_post_count'] = true;
    }
    return $args;
} );

/**
 * Uniform posts_per_page=200 on every front-end main-query listing
 * (home, category/tag archives, date/author archives, search results).
 * Singular views and admin queries keep WP defaults.
 *
 * Rationale: imported tweets are short and the card rendering is
 * lightweight; 200 per page is a reasonable browse unit, and uniform
 * pagination means one consistent rhythm instead of "10 on the home,
 * 200 on the twitter tab." Pairs with the infinite-scroll footer
 * script below so scrolling doesn't feel like a wall anyway.
 */
add_action( 'pre_get_posts', function ( $query ) {
    if ( is_admin() || ! $query->is_main_query() ) {
        return;
    }
    if ( $query->is_singular() ) {
        return;
    }
    $query->set( 'posts_per_page', 200 );
} );

/**
 * Render a Twitter-style profile header at the top of the main loop
 * when viewing a social-source category archive. Injected via
 * loop_start so the theme's index.php template can handle the post
 * listing itself — no custom category template needed.
 *
 * Gated to main query + front-end + is_category() + source-slug
 * match, so it won't fire on admin listings, widgets, or
 * non-source categories.
 */
add_action( 'loop_start', function ( $query ) {
    if ( ! $query instanceof WP_Query
        || ! $query->is_main_query()
        || is_admin()
        || is_feed()
        || ! is_category() ) {
        return;
    }
    $term = $query->get_queried_object();
    if ( ! ( $term instanceof WP_Term ) ) {
        return;
    }
    foreach ( onionpress_social_sources() as $slug => $info ) {
        if ( isset( $info['cat_slug'] ) && $info['cat_slug'] === $term->slug ) {
            echo onionpress_social_render_profile_header( $slug, $info, $term );
            return;
        }
    }
} );

function onionpress_social_render_profile_header( $source_slug, $info, $term ) {
    $admin_user = get_user_by( 'id', 1 );
    $avatar_url = $admin_user ? get_avatar_url( $admin_user->ID, array( 'size' => 128 ) ) : '';
    $name       = $admin_user ? $admin_user->display_name : get_bloginfo( 'name' );
    $handle_opt = 'onionpress_social_' . $source_slug . '_handle';
    $handle     = (string) get_option( $handle_opt, '' );

    $post_count = intval( $term->count );
    $earliest   = get_posts( array(
        'category'       => $term->term_id,
        'posts_per_page' => 1,
        'orderby'        => 'date',
        'order'          => 'ASC',
    ) );
    $latest = get_posts( array(
        'category'       => $term->term_id,
        'posts_per_page' => 1,
        'orderby'        => 'date',
        'order'          => 'DESC',
    ) );
    $date_range = '';
    if ( $earliest && $latest ) {
        $from = mysql2date( 'M Y', $earliest[0]->post_date );
        $to   = mysql2date( 'M Y', $latest[0]->post_date );
        $date_range = ( $from === $to ) ? $from : "$from &ndash; $to";
    }

    ob_start();
    ?>
    <section class="op-social-profile" style="--op-accent:<?php echo esc_attr( $info['color'] ); ?>;">
        <?php
        // Brand mark in the top-right: "X-Twitter" + X/bird logo,
        // "Mastodon" + mastodon logo, etc. Shows which platform the
        // category was imported from at a glance. Only on the profile
        // card at the top of the category — individual post cards
        // already have the per-source badge in their own header.
        $brand_label = $info['brand_label'] ?? $info['label'];
        $brand_logo  = function_exists( 'onionpress_social_nav_icon_html' )
            ? onionpress_social_nav_icon_html( $source_slug )
            : '';
        ?>
        <div class="op-social-profile__brand" aria-hidden="true">
            <span class="op-social-profile__brand-label"><?php echo esc_html( $brand_label ); ?></span>
            <?php echo $brand_logo; ?>
        </div>
        <?php if ( $avatar_url ) : ?>
            <img class="op-social-profile__avatar" src="<?php echo esc_url( $avatar_url ); ?>" alt="">
        <?php endif; ?>
        <div class="op-social-profile__meta">
            <h1 class="op-social-profile__name"><?php echo esc_html( $name ); ?></h1>
            <?php if ( $handle !== '' ) : ?>
                <div class="op-social-profile__handle">@<?php echo esc_html( $handle ); ?></div>
            <?php endif; ?>
            <div class="op-social-profile__source">
                Imported from <strong><?php echo esc_html( $info['label'] ); ?></strong>
            </div>
            <div class="op-social-profile__stats">
                <strong><?php echo number_format_i18n( $post_count ); ?></strong>
                post<?php echo $post_count === 1 ? '' : 's'; ?>
                <?php if ( $date_range ) : ?>
                    &middot; <?php echo $date_range; ?>
                <?php endif; ?>
            </div>
        </div>
    </section>
    <?php
    return (string) ob_get_clean();
}

/**
 * Universal infinite-scroll on any listing page that renders a
 * `.pagination` block. IntersectionObserver watches pagination; when
 * it scrolls into view the script fetches the next page URL from its
 * <a class="next"> link, extracts all <article> elements from the
 * fetched document, and appends them above the (newly-replaced)
 * pagination block. Continues until no next link exists.
 *
 * Layered on top of server-rendered pagination, not a replacement —
 * with JS off or on fetch failure, the normal next-page link is
 * clickable. Crawlers (SPN, Googlebot) see the same server-side
 * <a class="next"> and can follow it without JS.
 */
add_action( 'wp_footer', function () {
    if ( is_admin() || is_feed() || is_singular() ) {
        return;
    }
    ?>
    <script id="op-social-infinite-scroll">
    (function () {
        var seen = new Set();
        var loading = false;
        function currentNext() {
            var p = document.querySelector('.pagination .next');
            return p && p.href ? p.href : null;
        }
        function sig(el) {
            return el.outerHTML.length + ':' + (el.textContent || '').slice(0, 60);
        }
        function loadMore(url) {
            if (loading) return;
            loading = true;
            var pag = document.querySelector('.pagination');
            if (!pag) { loading = false; return; }
            pag.classList.add('is-loading');
            fetch(url, { credentials: 'same-origin' })
                .then(function (r) { return r.ok ? r.text() : Promise.reject(r.status); })
                .then(function (html) {
                    var doc = new DOMParser().parseFromString(html, 'text/html');
                    // Grab the main content area's articles — avoid
                    // sidebar widgets with post-like markup.
                    var newArts = doc.querySelectorAll('.main-content article');
                    newArts.forEach(function (art) {
                        var s = sig(art);
                        if (seen.has(s)) return;
                        seen.add(s);
                        pag.parentNode.insertBefore(art, pag);
                    });
                    var newPag = doc.querySelector('.pagination');
                    if (newPag) {
                        pag.replaceWith(newPag);
                        observe();
                    } else {
                        pag.remove();
                    }
                })
                .catch(function () { /* leave pagination click-through intact */ })
                .finally(function () { loading = false; });
        }
        var io = new IntersectionObserver(function (entries) {
            entries.forEach(function (e) {
                if (!e.isIntersecting) return;
                var n = currentNext();
                if (n) loadMore(n);
            });
        }, { rootMargin: '600px 0px' });
        function observe() {
            var pag = document.querySelector('.pagination');
            if (pag) io.observe(pag);
        }
        // Seed 'seen' with articles already on the page
        document.querySelectorAll('.main-content article').forEach(function (art) {
            seen.add(sig(art));
        });
        observe();
    })();
    </script>
    <style id="op-social-profile-styles">
    .op-social-profile {
        position: relative;
        display: flex;
        align-items: center;
        gap: 1.25em;
        max-width: 640px;
        margin: 1em 0 2em;
        padding: 1.25em 1.5em;
        border: 1px solid #e1e8ed;
        border-top: 6px solid var(--op-accent, #1da1f2);
        border-radius: 14px;
        background: #fff;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
    }
    /* Brand mark top-right: "X-Twitter" + X/bird, "Mastodon" + logo, etc.
       Uses the --op-accent CSS variable for the logo color. */
    .op-social-profile__brand {
        position: absolute;
        top: 0.9em;
        right: 1.1em;
        display: inline-flex;
        align-items: center;
        gap: 0.3em;
        color: var(--op-accent, #1da1f2);
        font-weight: 600;
        font-size: 0.9em;
        line-height: 1;
    }
    .op-social-profile__brand .op-social-logo {
        width: 1.15em; height: 1.15em;
        display: inline-block;
    }
    .op-social-profile__brand .op-social-nav-icon,
    .op-social-profile__brand .op-social-nav-icon--combo {
        display: inline-flex;
        font-weight: inherit;
    }
    .op-social-profile__brand .op-social-nav-icon__slash {
        opacity: 0.55;
        padding: 0 0.1em;
    }
    .op-social-profile__avatar {
        flex: 0 0 auto; width: 80px; height: 80px;
        border-radius: 50%; object-fit: cover; background: #eee;
    }
    .op-social-profile__meta { min-width: 0; flex: 1; }
    .op-social-profile__name  { margin: 0 0 0.15em; font-size: 1.6em; line-height: 1.2; color: #0f1419; }
    .op-social-profile__handle { color: #536471; font-size: 1em; margin-bottom: 0.5em; }
    .op-social-profile__source,
    .op-social-profile__stats  { color: #536471; font-size: 0.9em; line-height: 1.5; }
    .op-social-profile__stats strong { color: #0f1419; }
    @media (prefers-color-scheme: dark) {
        .op-social-profile { background: #15202b; border-color: #38444d; }
        .op-social-profile__name { color: #e7e9ea; }
        .op-social-profile__handle, .op-social-profile__source, .op-social-profile__stats { color: #8b98a5; }
        .op-social-profile__stats strong { color: #e7e9ea; }
    }
    </style>
    <?php
} );

/**
 * Wrap imported posts in a tweet-style card.
 *
 * Implemented as a `the_content` filter rather than baked into stored
 * post_content so:
 *   - the stored post stays clean (readable in wp-admin, easy to edit,
 *     friendly to future re-processing);
 *   - every existing import picks up the styling without a migration;
 *   - future importers for Mastodon, Bluesky, etc. inherit the same
 *     card for free just by populating _source_id / _source_url, with
 *     the card's accent color coming from the per-source definition.
 *
 * Source slug is read from the `source:` prefix in _source_id, so a
 * post without that meta (your own original writing) is untouched.
 */
function onionpress_social_wrap_as_card( $content ) {
    if ( ! in_the_loop() || is_admin() || is_feed() ) {
        return $content;
    }
    $post_id = get_the_ID();
    if ( ! $post_id ) {
        return $content;
    }
    $source_id  = get_post_meta( $post_id, '_source_id', true );
    $source_url = get_post_meta( $post_id, '_source_url', true );
    if ( empty( $source_id ) || strpos( $source_id, ':' ) === false ) {
        return $content;
    }
    list( $source_slug ) = explode( ':', $source_id, 2 );
    $sources = onionpress_social_sources();
    if ( ! isset( $sources[ $source_slug ] ) ) {
        return $content;
    }
    $info = $sources[ $source_slug ];

    $post   = get_post( $post_id );
    $author = $post ? get_userdata( $post->post_author ) : null;

    // Display name: fall back from user → "OnionPress archive" rather
    // than blank, since an imported post's author should always read
    // as a person or entity rather than an empty box.
    $display_name = $author && $author->display_name
        ? $author->display_name
        : get_bloginfo( 'name' );
    $avatar_url   = $author ? get_avatar_url( $author->ID, array( 'size' => 96 ) ) : '';

    // Per-source saved handle (e.g. onionpress_social_twitter_handle).
    // The Twitter importer sets this via its admin page; other
    // importers will follow the same convention.
    $handle_opt = 'onionpress_social_' . $source_slug . '_handle';
    $handle     = (string) get_option( $handle_opt, '' );

    // Twitter's "3:57 AM · Nov 22, 2022" format. Two separate
    // get_the_time() calls so the literal middle-dot doesn't have to
    // be escaped inside a date() format string (a past version tried
    // '\&middot;' and produced garbage when date() interpreted
    // 'middot' as format chars).
    $ts_time = get_the_time( 'g:i A', $post_id );
    $ts_date = get_the_time( 'M j, Y', $post_id );
    $ts_display = esc_html( $ts_time ) . ' &middot; ' . esc_html( $ts_date );

    $host       = $source_url ? parse_url( $source_url, PHP_URL_HOST ) : '';
    $host_label = $host ? esc_html( $host ) : esc_html( $info['label'] );

    // The source badge in the header links to the per-source category
    // archive (e.g. /category/twitter/) so it's a usable filter, not
    // just decoration. Renders as a circular brand-logo pill; falls
    // back to the platform name if we don't have a logo path for that
    // slug (e.g. a third-party importer that added itself via the
    // `onionpress_social_sources` filter).
    $badge_logo  = onionpress_social_post_badge_logo_svg( $source_slug, $post_id );
    $has_logo    = $badge_logo !== '';
    $badge_inner = $has_logo ? $badge_logo : esc_html( $info['label'] );
    $badge_class = 'op-social-card__badge' . ( $has_logo ? ' op-social-card__badge--icon' : '' );
    $cat_term    = get_term_by( 'slug', $info['cat_slug'], 'category' );
    $cat_url     = ( $cat_term && ! is_wp_error( $cat_term ) ) ? get_term_link( $cat_term ) : '';
    if ( $cat_url && ! is_wp_error( $cat_url ) ) {
        $badge_element = sprintf(
            '<a class="%s" href="%s" style="background:%s;" title="Browse all imported %s posts" aria-label="Imported from %s">%s</a>',
            esc_attr( $badge_class ),
            esc_url( $cat_url ),
            esc_attr( $info['color'] ),
            esc_attr( $info['label'] ),
            esc_attr( $info['label'] ),
            $badge_inner
        );
    } else {
        $badge_element = sprintf(
            '<span class="%s" style="background:%s;" title="Imported from %s" aria-label="Imported from %s">%s</span>',
            esc_attr( $badge_class ),
            esc_attr( $info['color'] ),
            esc_attr( $info['label'] ),
            esc_attr( $info['label'] ),
            $badge_inner
        );
    }

    $header  = '<div class="op-social-card__head">';
    if ( $avatar_url ) {
        $header .= sprintf(
            '<img class="op-social-card__avatar" src="%s" alt="" loading="lazy">',
            esc_url( $avatar_url )
        );
    }
    $header .= '<div class="op-social-card__identity">'
        . '<span class="op-social-card__name">' . esc_html( $display_name ) . '</span>';
    if ( $handle !== '' ) {
        $header .= '<span class="op-social-card__handle">@' . esc_html( $handle ) . '</span>';
    }
    $header .= '</div>';
    $header .= $badge_element;
    $header .= '</div>';

    // Link the timestamp to the post's own permalink when we're rendering
    // in a listing (archive/index/search), so readers have a visible way
    // to reach the single-post view. On the single-post page itself, keep
    // the plain span — linking to the current page is pointless.
    $permalink = get_permalink( $post_id );
    $is_self   = $permalink && is_singular() && (int) get_queried_object_id() === (int) $post_id;

    $footer  = '<div class="op-social-card__foot">';
    if ( $permalink && ! $is_self ) {
        $footer .= sprintf(
            '<a class="op-social-card__ts" href="%s" title="Permalink">%s</a>',
            esc_url( $permalink ),
            $ts_display
        );
    } else {
        $footer .= '<span class="op-social-card__ts">' . $ts_display . '</span>';
    }
    if ( $source_url ) {
        $footer .= sprintf(
            ' <span class="op-social-card__sep">&middot;</span> '
                . '<a class="op-social-card__viewlink" href="%s" rel="nofollow noopener" target="_blank">View on %s &rarr;</a>',
            esc_url( $source_url ),
            $host_label
        );
    }
    $footer .= '</div>';

    return sprintf(
        '<div class="op-social-card op-social-card--%s" style="--op-accent:%s;">%s<div class="op-social-card__body">%s</div>%s</div>',
        esc_attr( $source_slug ),
        esc_attr( $info['color'] ),
        $header,
        $content,
        $footer
    );
}
add_filter( 'the_content', 'onionpress_social_wrap_as_card', 20 );

/**
 * Twin of the_content filter for excerpt contexts (homepage, search
 * results, date/author archives — anywhere the theme calls
 * the_excerpt() on a listing). Without this, imported posts show as
 * title + plain-text excerpt when scrolling the main blog, which
 * looks wrong next to cards on the category pages and single-tweet
 * pages. Returns the full card HTML instead of the excerpt so a
 * tweet looks like a tweet wherever it appears.
 */
function onionpress_social_wrap_excerpt_as_card( $excerpt ) {
    if ( is_admin() || is_feed() ) {
        return $excerpt;
    }
    $post_id = get_the_ID();
    if ( ! $post_id ) {
        return $excerpt;
    }
    $source_id = get_post_meta( $post_id, '_source_id', true );
    if ( empty( $source_id ) || strpos( $source_id, ':' ) === false ) {
        return $excerpt;
    }
    $post = get_post( $post_id );
    if ( ! $post ) {
        return $excerpt;
    }
    // Render the card using the same code path as the_content so the
    // visual treatment is identical everywhere. The_content filter
    // expects raw post_content and adds the card chrome around it.
    return onionpress_social_wrap_as_card( wpautop( $post->post_content ) );
}
add_filter( 'the_excerpt', 'onionpress_social_wrap_excerpt_as_card', 20 );
add_filter( 'get_the_excerpt', 'onionpress_social_wrap_excerpt_as_card', 20 );

/**
 * Tag the <body> and <article> elements with op-is-social when we're
 * rendering an imported post. The theme-level title + date/author
 * chrome looks redundant above our tweet-style card (the card already
 * shows the author and time); CSS in the card stylesheet below uses
 * these classes to hide the redundant chrome on single-post views,
 * leaving just the card.
 */
add_filter( 'body_class', function ( $classes ) {
    if ( is_singular() ) {
        $post_id = get_queried_object_id();
        if ( $post_id && get_post_meta( $post_id, '_source_id', true ) ) {
            $classes[] = 'op-is-social';
        }
    }
    return $classes;
} );
add_filter( 'post_class', function ( $classes, $class, $post_id ) {
    if ( $post_id && get_post_meta( $post_id, '_source_id', true ) ) {
        $classes[] = 'op-is-social';
    }
    return $classes;
}, 10, 3 );

/**
 * Inline stylesheet for the tweet-style card. Emitted once per page
 * via wp_head so we don't pollute post_content with styles that can't
 * be overridden in the theme. Uses CSS custom property --op-accent so
 * each source's brand color lights up the border without us needing
 * one CSS block per source.
 */
function onionpress_social_card_styles() {
    // Only emit on front-end views (skip admin / feeds).
    if ( is_admin() || is_feed() ) {
        return;
    }
    ?>
    <style id="onionpress-social-card-styles">
    .op-social-card {
        max-width: 600px;
        margin: 1.25em 0;
        padding: 1em 1.25em;
        border: 1px solid #e1e8ed;
        border-left: 4px solid var(--op-accent, #1da1f2);
        border-radius: 14px;
        background: #ffffff;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
        line-height: 1.45;
        color: #0f1419;
    }
    .op-social-card__head {
        display: flex;
        align-items: center;
        gap: 0.75em;
        margin-bottom: 0.75em;
    }
    .op-social-card__avatar {
        flex: 0 0 auto;
        width: 44px;
        height: 44px;
        border-radius: 50%;
        object-fit: cover;
        background: #eee;
    }
    .op-social-card__identity {
        display: flex;
        flex-direction: column;
        min-width: 0;
        flex: 1;
    }
    .op-social-card__name {
        font-weight: 700;
        color: #0f1419;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    .op-social-card__handle {
        color: #536471;
        font-size: 0.9em;
    }
    .op-social-card__badge {
        flex: 0 0 auto;
        padding: 0.15em 0.55em;
        border-radius: 999px;
        font-size: 0.72em;
        font-weight: 600;
        color: #fff !important;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        opacity: 0.85;
        text-decoration: none;
    }
    /* Icon-mode badge: circular, logo-only, slightly larger than the
       old text pill so the brand mark is actually readable. */
    .op-social-card__badge--icon {
        padding: 0;
        width: 26px; height: 26px;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        opacity: 1;
    }
    .op-social-card__badge--icon .op-social-logo {
        width: 60%; height: 60%;
        display: block;
    }
    a.op-social-card__badge:hover { opacity: 1; text-decoration: none; }
    a.op-social-card__badge--icon:hover { transform: scale(1.08); transition: transform 0.15s; }

    /* Primary-nav Social Archive links: wide screens get the text
       label only ("My Tweets"), narrow (≤480px) swaps to the brand
       logo only. The <a> itself carries an aria-label so screen
       readers hear the full name regardless of which visual state
       renders. Twitter uses the X / bird composite to signal the
       account spans both eras. */
    .op-social-nav {
        display: inline-flex;
        align-items: center;
    }
    .op-social-nav-icon {
        display: none;
        align-items: center;
        justify-content: center;
        width: 1.4em; height: 1.4em;
        color: var(--op-accent, currentColor);
    }
    .op-social-nav-icon .op-social-logo {
        width: 100%; height: 100%;
        display: block;
    }
    .op-social-nav-icon--combo {
        width: auto;
        gap: 1px;
        font-weight: 600;
    }
    .op-social-nav-icon--combo .op-social-logo {
        width: 1em; height: 1em;
    }
    .op-social-nav-icon__slash {
        display: inline-block;
        padding: 0 0.15em;
        opacity: 0.55;
        font-size: 0.9em;
    }
    @media (max-width: 768px) {
        .op-social-nav-icon { display: inline-flex; }
        .op-social-nav-label { display: none; }
    }
    /* Archives widget title on a social category: inline logo + "Archives".
       Force the nested .op-social-nav-icon visible at all widths — the
       nav-icon class is display:none at ≥768px (where the nav text is
       shown instead), but inside the widget title we ALWAYS want the
       logo visible. */
    .op-social-archive-widget-title {
        display: inline-flex;
        align-items: center;
        gap: 0.4em;
        color: var(--op-accent, currentColor);
    }
    .op-social-archive-widget-title .op-social-nav-icon {
        display: inline-flex !important;
        height: 1em;
        /* Don't cap width: the Twitter combo ("X / bird") is wider
           than one character. Let the wrapper size to its contents. */
    }
    .op-social-archive-widget-title .op-social-nav-icon:not(.op-social-nav-icon--combo) {
        width: 1em;
    }
    .op-social-archive-widget-title .op-social-logo {
        width: 1em; height: 1em;
        display: inline-block;
    }
    /* Combo variant: size slightly larger inside the h2 heading so the
       two glyphs + slash are legible next to the "Archives" text. */
    .op-social-archive-widget-title .op-social-nav-icon--combo {
        font-size: 0.85em;
        gap: 0.05em;
    }
    .op-social-archive-widget-title .op-social-nav-icon--combo .op-social-logo {
        width: 0.95em; height: 0.95em;
    }
    .op-social-archive-widget-title .op-social-nav-icon--combo .op-social-nav-icon__slash {
        opacity: 0.55;
        padding: 0 0.05em;
        font-weight: 600;
    }
    .op-social-card__body {
        font-size: 1.02em;
        color: #0f1419;
        word-wrap: break-word;
    }
    .op-social-card__body p:first-child { margin-top: 0; }
    .op-social-card__body p:last-child  { margin-bottom: 0; }
    .op-social-card__body img,
    .op-social-card__body video {
        max-width: 100%;
        height: auto;
        border-radius: 12px;
        margin: 0.5em 0;
        display: block;
    }
    .op-social-card__body a {
        color: #1d9bf0;
        text-decoration: none;
    }
    .op-social-card__body a:hover { text-decoration: underline; }
    .op-social-card__foot {
        margin-top: 0.9em;
        padding-top: 0.65em;
        border-top: 1px solid #eff3f4;
        color: #536471;
        font-size: 0.85em;
    }
    .op-social-card__foot a { color: #1d9bf0; text-decoration: none; }
    .op-social-card__foot a:hover { text-decoration: underline; }
    .op-social-card__sep { margin: 0 0.25em; }
    /* On single-post views of imported content, hide the theme's
       own title and date/author meta — the card already shows those,
       and doubling them up makes an imported tweet look like a blog
       post that happens to contain a tweet. Targets .op-is-social
       on body (single pages) or on article (archive pages) so the
       rule applies either way without cross-theme fragility. */
    body.op-is-social .post-title,
    body.op-is-social .post-meta,
    .op-is-social > .post-title,
    .op-is-social > .post-meta,
    .op-is-social > .read-more { display: none; }
    /* The theme wraps listing excerpts in <div class="post-content">.
       Our card is a block element that should fill the article — no
       extra padding from that wrapper. */
    .op-is-social > .post-content { padding: 0; }
    @media (prefers-color-scheme: dark) {
        .op-social-card {
            background: #15202b;
            border-color: #38444d;
            color: #e7e9ea;
        }
        .op-social-card__name  { color: #e7e9ea; }
        .op-social-card__body  { color: #e7e9ea; }
        .op-social-card__foot  { border-top-color: #22303c; color: #8b98a5; }
        .op-social-card__handle, .op-social-card__foot { color: #8b98a5; }
    }
    </style>
    <?php
}
add_action( 'wp_head', 'onionpress_social_card_styles' );

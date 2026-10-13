<?php
/**
 * OnionPress Theme Functions
 *
 * @package OnionPress
 */

if (!defined('ABSPATH')) {
    exit;
}

/**
 * Theme setup
 */
function onionpress_setup() {
    add_theme_support('title-tag');
    add_theme_support('post-thumbnails');
    add_theme_support('custom-header', array(
        'width'       => 1920,
        'height'      => 480,
        'flex-width'  => true,
        'flex-height' => true,
        'header-text' => false,
    ));
    add_theme_support('custom-logo', array(
        'width'       => 200,
        'height'      => 200,
        'flex-width'  => true,
        'flex-height' => true,
    ));
    add_theme_support('html5', array(
        'search-form', 'comment-form', 'comment-list', 'gallery', 'caption',
    ));

    register_nav_menus(array(
        'primary' => 'Primary Menu',
    ));
}
add_action('after_setup_theme', 'onionpress_setup');

/**
 * Enqueue theme styles
 */
function onionpress_scripts() {
    wp_enqueue_style('onionpress-style', get_stylesheet_uri(), array(), '1.0.4');
}
add_action('wp_enqueue_scripts', 'onionpress_scripts');

/**
 * Register sidebar for blogroll / follows
 */
function onionpress_widgets_init() {
    register_sidebar(array(
        'name'          => 'Sidebar',
        'id'            => 'sidebar-1',
        'before_widget' => '<div class="sidebar-section">',
        'after_widget'  => '</div>',
        'before_title'  => '<h3>',
        'after_title'   => '</h3>',
    ));
}
add_action('widgets_init', 'onionpress_widgets_init');

/**
 * Render hit counter in footer automatically
 */
function onionpress_footer_counter() {
    if (shortcode_exists('hit_counter')) {
        echo '<div class="footer-counter">';
        echo do_shortcode('[hit_counter]');
        echo '</div>';
    }
}
add_action('onionpress_footer', 'onionpress_footer_counter');

/**
 * Get the user's avatar URL (Gravatar or custom logo)
 */
function onionpress_get_avatar_url() {
    $custom_logo_id = get_theme_mod('custom_logo');
    if ($custom_logo_id) {
        $logo_url = wp_get_attachment_image_url($custom_logo_id, 'thumbnail');
        if ($logo_url) {
            return $logo_url;
        }
    }

    // Fall back to admin user's Gravatar
    $admin = get_user_by('email', get_option('admin_email'));
    if ($admin) {
        return get_avatar_url($admin->ID, array('size' => 200));
    }

    return false;
}

/**
 * Get first letter of site title for avatar placeholder
 */
function onionpress_get_avatar_letter() {
    $title = get_bloginfo('name');
    return mb_strtoupper(mb_substr($title, 0, 1));
}

/**
 * Return this blog's onionname — the WP user_login of the primary author /
 * site admin. On multisite each blog's URL is /<onionname>/; on single-site
 * we fall back to the site's admin user. Returns an empty string if we
 * can't determine one (pre-name-sync installs).
 */
function onionpress_get_onionname() {
    // Multisite: the owner of the current blog. A subsite lives at
    // /<onionname>/ (`wp site create --slug=<onionname>`, setup_logic), and
    // that path is the only place WordPress records whose it is: WP_Site
    // has no user_id, and the lowest-ID administrator is usually the
    // network owner, which put the owner's name on every subsite's Follow
    // button (issue #321).
    if ( is_multisite() ) {
        $blog_id = get_current_blog_id();
        $blog    = function_exists( 'get_site' ) ? get_site( $blog_id ) : null;
        $slug    = $blog ? trim( (string) $blog->path, '/' ) : '';
        if ( '' !== $slug && false === strpos( $slug, '/' ) ) {
            $user = get_user_by( 'login', $slug );
            if ( $user && is_user_member_of_blog( $user->ID, $blog_id ) ) {
                return $user->user_login;
            }
        }
        // The network root, or a subsite not named after a member: the
        // first administrator registered for this blog.
        $admins = get_users( array(
            'blog_id' => $blog_id,
            'role'    => 'administrator',
            'number'  => 1,
            'orderby' => 'ID',
            'order'   => 'ASC',
            'fields'  => array( 'user_login' ),
        ) );
        if ( ! empty( $admins ) ) {
            return $admins[0]->user_login;
        }
    }
    // Single-site: admin_email → user.
    $admin = get_user_by( 'email', get_option( 'admin_email' ) );
    if ( $admin ) {
        return $admin->user_login;
    }
    return '';
}

/**
 * Return the .onion address this request arrived on.
 *
 * When a visitor reaches the site via Tor the HTTP Host header is the full
 * v3 .onion address — use that directly. Fall back to the shared-volume
 * file the launcher populates so server-side code paths (cron, wp-cli)
 * can still answer truthfully.
 */
function onionpress_follow_get_own_address() {
    if ( ! empty( $_SERVER['HTTP_HOST'] ) ) {
        $host = strtolower( (string) $_SERVER['HTTP_HOST'] );
        // Strip port if present (rare on .onion, but defensive).
        if ( strpos( $host, ':' ) !== false ) {
            $host = substr( $host, 0, strpos( $host, ':' ) );
        }
        if ( preg_match( '/^[a-z2-7]{56}\.onion$/D', $host ) ) {
            return $host;
        }
    }
    $addr_file = '/var/lib/onionpress/onion_address';
    if ( is_readable( $addr_file ) ) {
        $addr = trim( (string) @file_get_contents( $addr_file ) );
        if ( preg_match( '/^[a-z2-7]{56}\.onion$/D', $addr ) ) {
            return $addr;
        }
    }
    return null;
}

/**
 * Create Follow page on theme activation if it doesn't exist
 */
function onionpress_create_follow_page() {
    $page = get_page_by_path('follow');
    if ($page) {
        return;
    }
    wp_insert_post(array(
        'post_title'    => 'Follow',
        'post_name'     => 'follow',
        'post_status'   => 'publish',
        'post_type'     => 'page',
        'post_content'  => '',
        'page_template' => 'page-follow.php',
    ));
}
add_action('after_switch_theme', 'onionpress_create_follow_page');

/**
 * Create Blogroll page on theme activation if it doesn't exist
 */
function onionpress_create_blogroll_page() {
    $page = get_page_by_path('blogroll');
    if ($page) {
        return;
    }
    wp_insert_post(array(
        'post_title'    => 'Blogroll',
        'post_name'     => 'blogroll',
        'post_status'   => 'publish',
        'post_type'     => 'page',
        'post_content'  => '',
        'page_template' => 'page-blogroll.php',
    ));
}
add_action('after_switch_theme', 'onionpress_create_blogroll_page');

// Also create on init for existing installs that already have the theme active
add_action('init', function () {
    if (get_option('onionpress_blogroll_page_created')) {
        return;
    }
    onionpress_create_blogroll_page();
    update_option('onionpress_blogroll_page_created', true);
});

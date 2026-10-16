<?php
/**
 * Plugin Name: OnionPress Cache Policy
 * Description: What Cache Enabler may keep and serve on an OnionPress site:
 *              pages for anonymous visitors to this site's own onion
 *              address, and nothing else. Cache Enabler is written for a
 *              site with one host name; OnionPress serves one WordPress to
 *              its .onion, to localhost and to the containers, and Tor
 *              hands WordPress whatever Host header a visitor sends.
 * Version:     1.0
 * Network:     true
 * License:     AGPL-3.0-or-later
 * License URI: https://www.gnu.org/licenses/agpl-3.0.html
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

/*
 * How Cache Enabler 1.8 works, as far as it matters here:
 *
 * - Pages are kept and served by its advanced-cache.php drop-in, only for
 *   requests to index.php (never wp-login.php, wp-admin, admin-ajax.php,
 *   wp-cron.php). It keys each page by the request's Host (port dropped)
 *   and path: wp-content/cache/cache-enabler/<host>/<path>/http-index.html.
 * - Serving happens before WordPress, and so before this plugin, loads.
 *   It checks only the excluded_* settings, read from a settings file that
 *   the plugin writes per host and site (wp-content/settings/cache-enabler/
 *   <host>[.<site>].php) from the site's `cache_enabler` option.
 * - Keeping happens when WordPress has finished the page, and asks the
 *   cache_enabler_bypass_cache filter (onionpress_cache_bypass below).
 * - Clearing (on publish, comment, settings) goes to the host of the
 *   site's home URL, which onionpress-domain-map.php makes the host of the
 *   request doing the clearing: usually localhost, where the owner works.
 *
 * So OnionPress keeps pages for one host only, its own onion address
 * (nothing localhost or a container sees can reach a Tor visitor, and no
 * visitor can fill the disk with copies for made-up hosts), sends every
 * clearing to that host, and sets the exclusions serving relies on itself.
 */

/**
 * This install's onion address (lower case, no port), from the file the
 * launcher writes into the shared volume; '' when there is none yet, and
 * then nothing is cached.
 */
function onionpress_cache_own_onion() {
    static $addr = null;
    if ( null === $addr ) {
        $addr = '';
        $file = defined( 'ONIONPRESS_CACHE_ONION_FILE' ) ? ONIONPRESS_CACHE_ONION_FILE
                                                         : '/var/lib/onionpress/onion_address';
        if ( is_readable( $file ) ) {
            $read = strtolower( trim( (string) @file_get_contents( $file, false, null, 0, 256 ) ) );
            if ( preg_match( '/^[a-z2-7]{56}\.onion$/D', $read ) ) {
                $addr = $read;
            }
        }
    }
    return $addr;
}

/**
 * Settings for a site that has none yet: one hour, cleared whenever
 * published content changes. Cache Enabler fills in its own defaults for
 * whatever is missing.
 */
function onionpress_cache_defaults() {
    return array(
        'cache_expires'                      => 1,
        'cache_expiry_time'                  => 1,
        'clear_site_cache_on_saved_post'     => 1,
        'clear_site_cache_on_saved_comment'  => 1,
        'clear_site_cache_on_saved_term'     => 1,
        'clear_site_cache_on_saved_user'     => 0,
        'clear_site_cache_on_changed_plugin' => 1,
        'convert_image_urls_to_webp'         => 0,
        'mobile_cache'                       => 0,
        'compress_cache'                     => 0,
        'minify_html'                        => 0,
        'minify_inline_css_js'               => 0,
        'excluded_post_ids'                  => '',
    );
}

/**
 * Settings every site gets, whatever was saved. The exclusions are all
 * that stands between a stored page and a request it must not answer when
 * the page is served (before WordPress loads):
 *
 * - any query string: the cache key ignores it, so ?op_login=<token>
 *   (auto-login), ?onionpress_creation=<file>, ?p=<id> and the rest would
 *   otherwise get, or replace, the copy of the bare path. Cache Enabler's
 *   own default lets utm_*, ref and a few others through.
 * - a logged-in, password-post or commenter cookie (Cache Enabler's
 *   default list, widened to every wordpress* and wp-settings* cookie).
 * - the admin, REST and login paths and the status page, which are never
 *   stored either (not index.php, not HTML, or logged-in only).
 *
 * Pages expire within one to 24 hours, so a clearing that misses one
 * (a file dropped into My Creations, a feed refresh on the blogroll)
 * still shows up. Cache Enabler accepts a regex only as a whole /.../.
 */
function onionpress_cache_enforced() {
    return array(
        'cache_expires'          => 1,
        'excluded_query_strings' => '/./',
        'excluded_cookies'       => '/^(wordpress|wp-postpass|wp-settings|comment_author)/',
        'excluded_page_paths'    => '/(^|\/)(wp-admin|wp-json|wp-login\.php|onionpress-status(-json)?)(\/|$)/',
    );
}

/**
 * cache_enabler_settings_before_validation: Cache Enabler validates a
 * site's settings when it is activated (every site, for a network
 * activation), when a site is created, when its settings page is saved,
 * when it is upgraded, and when `wp onionpress-cache-policy apply` runs.
 */
function onionpress_cache_settings( $settings ) {
    $settings = is_array( $settings ) ? $settings : array();
    if ( ! array_intersect_key( $settings, onionpress_cache_defaults() ) ) {
        $settings = array_merge( $settings, onionpress_cache_defaults() );
    }
    $settings = array_merge( $settings, onionpress_cache_enforced() );
    $hours    = isset( $settings['cache_expiry_time'] ) ? (int) $settings['cache_expiry_time'] : 0;
    $settings['cache_expiry_time'] = min( max( $hours, 1 ), 24 );
    return $settings;
}
add_filter( 'cache_enabler_settings_before_validation', 'onionpress_cache_settings', 99 );

/**
 * Page templates never kept: My Creations lists a folder that changes
 * outside WordPress (and shows the owner an upload form), the Directory
 * page orders its downloads by the visitor's User-Agent.
 */
function onionpress_cache_uncached_templates() {
    return array( 'page-creations.php', 'page-directory.php' );
}

/**
 * Whether the page WordPress has just made may be kept for every visitor.
 * $host is the request's Host header, $headers the response headers.
 *
 * - The Host must be this site's onion address exactly: a page made for
 *   localhost, a container or a LAN address is the owner's or a probe's
 *   (and links there), and a Host header is a visitor's to choose, so any
 *   other host, or a port, would let a visitor plant pages or fill the disk.
 * - The visitor must be anonymous, and the response must not set a cookie
 *   or ask not to be cached (nocache_headers(): WordPress for logged-in
 *   users, the auto-login, root redirect, status and directory handlers).
 * - Not something WordPress found no page for (a plugin may have answered
 *   it), and not one of onionpress_cache_uncached_templates().
 */
function onionpress_cache_may_store( $host, $headers ) {
    $own = onionpress_cache_own_onion();
    if ( '' === $own || $own !== $host ) {
        return false;
    }
    if ( function_exists( 'is_user_logged_in' ) && is_user_logged_in() ) {
        return false;
    }
    foreach ( (array) $headers as $header ) {
        if ( preg_match( '/^\s*set-cookie\s*:/i', (string) $header )
             || preg_match( '/^\s*cache-control\s*:.*\b(no-cache|no-store|private)\b/i', (string) $header ) ) {
            return false;
        }
    }
    if ( function_exists( 'is_404' ) && is_404() ) {
        return false;
    }
    if ( function_exists( 'is_page' ) && is_page()
         && in_array( get_page_template_slug( get_queried_object_id() ), onionpress_cache_uncached_templates(), true ) ) {
        return false;
    }
    return true;
}

/**
 * cache_enabler_bypass_cache, asked when a finished page is about to be
 * kept. (It is asked before serving too, but then this plugin has not
 * loaded; serving relies on the exclusions in onionpress_cache_enforced().)
 */
function onionpress_cache_bypass( $bypass ) {
    if ( $bypass ) {
        return true;
    }
    $host = isset( $_SERVER['HTTP_HOST'] ) ? (string) $_SERVER['HTTP_HOST'] : '';
    return ! onionpress_cache_may_store( $host, headers_list() );
}
add_filter( 'cache_enabler_bypass_cache', 'onionpress_cache_bypass', 99 );

/**
 * Send Cache Enabler's clearing to the onion address.
 *
 * Every clearing (and every size count) goes through
 * Cache_Enabler_Disk::cache_iterator( $url ), whose first step is
 * esc_url_raw( $url ). The URL is the site's home URL or a permalink, so
 * its host is the one onionpress-domain-map.php gave this request:
 * localhost:8080 when the owner publishes from the local admin, where
 * nothing is ever kept. Rewritten to http://<onion>/<same path>, the
 * clearing reaches the only pages there are. Only inside cache_iterator;
 * esc_url_raw everywhere else is left alone.
 */
function onionpress_cache_purge_url( $url, $original_url = '', $context = 'display' ) {
    if ( 'db' !== $context || ! is_string( $url ) || ! class_exists( 'Cache_Enabler_Disk', false ) ) {
        return $url;
    }
    $own  = onionpress_cache_own_onion();
    $host = strtolower( (string) parse_url( $url, PHP_URL_HOST ) );
    if ( '' === $own || '' === $host || $own === $host ) {
        return $url;
    }
    $from_iterator = false;
    foreach ( debug_backtrace( DEBUG_BACKTRACE_IGNORE_ARGS, 12 ) as $frame ) {
        if ( isset( $frame['class'], $frame['function'] )
             && 'Cache_Enabler_Disk' === $frame['class'] && 'cache_iterator' === $frame['function'] ) {
            $from_iterator = true;
            break;
        }
    }
    if ( ! $from_iterator ) {
        return $url;
    }
    $rewritten = preg_replace( '#^https?://[^/?\#]*#i', 'http://' . $own, $url, 1, $count );
    return ( 1 === $count && is_string( $rewritten ) ) ? $rewritten : $url;
}
add_filter( 'clean_url', 'onionpress_cache_purge_url', 10, 3 );

/**
 * When a site's settings change, drop the onion address's settings files
 * so the next request there writes them anew from the database. Cache
 * Enabler rewrites only the file of the host doing the change (usually
 * localhost), and serving reads the onion address's files.
 */
function onionpress_cache_forget_onion_settings() {
    $own = onionpress_cache_own_onion();
    if ( '' === $own || ! defined( 'CACHE_ENABLER_SETTINGS_DIR' ) ) {
        return;
    }
    $files = array_merge(
        array( CACHE_ENABLER_SETTINGS_DIR . '/' . $own . '.php' ),
        (array) glob( CACHE_ENABLER_SETTINGS_DIR . '/' . $own . '.*.php' )
    );
    foreach ( $files as $file ) {
        if ( is_string( $file ) && ( is_file( $file ) || is_link( $file ) ) ) {
            @unlink( $file );
        }
    }
}
add_action( 'add_option_cache_enabler', 'onionpress_cache_forget_onion_settings' );
add_action( 'update_option_cache_enabler', 'onionpress_cache_forget_onion_settings' );

/**
 * The hosts whose settings files stay: the onion address and the names the
 * owner and the containers use. Cache Enabler writes a settings file for
 * every host a page is asked for under (on the first such request); a Tor
 * visitor chooses the Host header, so without this one PHP file per made-up
 * host would pile up in wp-content.
 */
function onionpress_cache_settings_hosts() {
    return array_values( array_filter( array(
        onionpress_cache_own_onion(), 'localhost', '127.0.0.1', '[::1]', 'wordpress',
    ) ) );
}

/**
 * shutdown: remove the settings file Cache Enabler wrote for this request's
 * host if it is not one of onionpress_cache_settings_hosts(). The file only
 * serves stored pages, and no page is stored for such a host. Compared as
 * sent, port aside, as Cache Enabler names the file: an upper-case
 * spelling of the onion address gets a file of its own, so it counts as
 * another host.
 */
function onionpress_cache_drop_stray_settings() {
    if ( ! class_exists( 'Cache_Enabler_Disk', false ) || ! function_exists( 'home_url' ) ) {
        return;
    }
    $host = isset( $_SERVER['HTTP_HOST'] ) ? (string) $_SERVER['HTTP_HOST'] : '';
    $host = preg_replace( '/:\d+$/D', '', $host );
    if ( '' === $host || in_array( $host, onionpress_cache_settings_hosts(), true ) ) {
        return;
    }
    Cache_Enabler_Disk::delete_settings_file();
}
add_action( 'shutdown', 'onionpress_cache_drop_stray_settings', 100 );

/**
 * `wp onionpress-cache-policy apply`: validate every site's Cache Enabler
 * settings again, through onionpress_cache_settings(). The launchers run it
 * at every start, after activating Cache Enabler, so a policy change in a
 * new OnionPress reaches sites whose settings were saved under the old one;
 * they then remove the settings files and stored pages (written under
 * whatever settings were in force), which come back from the new ones.
 */
function onionpress_cache_policy_cli( $args ) {
    if ( array( 'apply' ) !== array_values( (array) $args ) ) {
        WP_CLI::error( 'Usage: wp onionpress-cache-policy apply' );
    }
    if ( ! class_exists( 'Cache_Enabler' ) ) {
        WP_CLI::error( 'Cache Enabler is not active.' );
    }
    $sites = is_multisite()
        ? get_sites( array( 'fields' => 'ids', 'number' => 0 ) )
        : array( get_current_blog_id() );
    foreach ( $sites as $site ) {
        $switched = is_multisite() && switch_to_blog( (int) $site );
        Cache_Enabler::update_backend();
        if ( $switched ) {
            restore_current_blog();
        }
    }
    WP_CLI::success( sprintf( 'Cache Enabler settings checked on %d site(s).', count( $sites ) ) );
}
if ( defined( 'WP_CLI' ) && WP_CLI && class_exists( 'WP_CLI' ) ) {
    WP_CLI::add_command( 'onionpress-cache-policy', 'onionpress_cache_policy_cli' );
}

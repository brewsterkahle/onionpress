<?php
/**
 * Plugin Name: OnionPress My Creations
 * Plugin URI: https://github.com/brewsterkahle/onionpress
 * Description: Displays files from the My Creations directory as a gallery/list page
 * Version: 1.0.0
 * Author: OnionPress
 * Author URI: https://github.com/brewsterkahle/onionpress
 * License: AGPL-3.0
 * Text Domain: onionpress-creations
 */

if (!defined('ABSPATH')) {
    exit;
}

class OnionPress_Creations {

    const CREATIONS_DIR = '/var/www/html/wp-content/creations';
    const FILES_CACHE_KEY = 'op_creations_files';
    const FILES_CACHE_TTL = 60;  // seconds
    // File types a web server may run instead of serve. My Creations sits
    // under DocumentRoot, so these are refused at upload; the WordPress
    // image (onionpress-creations.conf) and the multisite .htaccess also
    // refuse to serve them from /wp-content/creations/.
    const BLOCKED_EXT_RE = '/\.(php[0-9]*|pht(ml)?|phar|phps|cgi|shtml)$/i';
    // Generated thumbnails: .thumbs/<path of a listed file>.png
    const THUMBS_PREFIX = '.thumbs/';

    private static $instance = null;

    /**
     * Whether $rel, a path relative to CREATIONS_DIR, names something My
     * Creations publishes. Hidden files and folders (.git/, .DS_Store,
     * .thumbs/ ...) are never listed, so they are never served or written
     * to either: no component may be empty or start with '.'.
     */
    public static function is_public_path($rel) {
        if (!is_string($rel) || $rel === '' || strpos($rel, "\0") !== false) {
            return false;
        }
        foreach (explode('/', $rel) as $part) {
            if ($part === '' || $part[0] === '.') {
                return false;
            }
        }
        return true;
    }

    /**
     * is_public_path(), plus the generated thumbnail of a public file,
     * which the Creations pages link to as THUMBS_PREFIX . <path> . '.png'.
     */
    public static function is_servable_path($rel) {
        if (is_string($rel) && strpos($rel, self::THUMBS_PREFIX) === 0
                && substr($rel, -4) === '.png') {
            $rel = substr($rel, strlen(self::THUMBS_PREFIX));
        }
        return self::is_public_path($rel);
    }

    /**
     * The path of $real (already realpath()ed) relative to the realpath()ed
     * base, '' for the base itself, or null when it lies outside it.
     */
    private static function rel_inside($real, $real_base) {
        if (!$real || !$real_base) {
            return null;
        }
        if ($real === $real_base) {
            return '';
        }
        if (strpos($real, $real_base . '/') !== 0) {
            return null;
        }
        return substr($real, strlen($real_base) + 1);
    }

    public static function get_instance() {
        if (null === self::$instance) {
            self::$instance = new self();
        }
        return self::$instance;
    }

    private function __construct() {
        add_action('template_redirect', array($this, 'serve_file'));
        add_action('template_redirect', array($this, 'handle_upload'), 5);
        add_filter('theme_page_templates', array($this, 'register_template'));
        add_filter('template_include', array($this, 'load_template'));
        register_activation_hook(__FILE__, array($this, 'activate'));
    }

    /**
     * Whether the current user may upload into My Creations.
     *
     * CREATIONS_DIR is one folder for the whole network: the owner's
     * ~/OnionPress/Creations/My Creations on the host, published on every
     * site and served from this origin. upload_files is a per-site
     * capability that every subsite's authors and administrators hold, so
     * it cannot decide who writes into the owner's folder; the network's
     * super admin (the owner; on a single site, an administrator) can.
     */
    public static function can_upload() {
        return is_user_logged_in() && is_super_admin();
    }

    /**
     * Handle file uploads from the My Creations page.
     * Requires can_upload() and a valid nonce. Skips dotfiles;
     * refuses BLOCKED_EXT_RE types and tells the uploader which ones.
     */
    public function handle_upload() {
        if (empty($_FILES['creations_upload']) || empty($_POST['creations_upload_nonce'])) {
            return;
        }
        if (!self::can_upload()) {
            return;
        }
        if (!wp_verify_nonce($_POST['creations_upload_nonce'], 'creations_upload')) {
            return;
        }

        $creations_dir = self::CREATIONS_DIR;
        // Support uploading into a subfolder
        $subfolder = isset($_POST['creations_upload_folder']) ? sanitize_text_field($_POST['creations_upload_folder']) : '';
        if ($subfolder && !self::is_public_path($subfolder)) {
            return;
        }
        $target_dir = $subfolder ? $creations_dir . '/' . $subfolder : $creations_dir;
        // Security: ensure target is a listed folder inside creations dir
        $real_target = realpath($target_dir);
        $real_base = realpath($creations_dir);
        $target_rel = self::rel_inside($real_target, $real_base);
        if ($target_rel === null || ($target_rel !== '' && !self::is_public_path($target_rel))) {
            return;
        }
        if (!is_dir($real_target) || !is_writable($real_target)) {
            return;
        }

        $files = $_FILES['creations_upload'];
        $count = is_array($files['name']) ? count($files['name']) : 1;

        // Normalize single file upload to array format
        if (!is_array($files['name'])) {
            $files = array(
                'name'     => array($files['name']),
                'tmp_name' => array($files['tmp_name']),
                'error'    => array($files['error']),
            );
        }

        $refused = array();
        for ($i = 0; $i < $count; $i++) {
            if ($files['error'][$i] !== UPLOAD_ERR_OK) {
                continue;
            }
            $name = sanitize_file_name($files['name'][$i]);
            if (empty($name) || $name[0] === '.') {
                continue;
            }
            if (preg_match(self::BLOCKED_EXT_RE, $name)) {
                $refused[] = $name;
                continue;
            }
            $dest = $real_target . '/' . $name;
            // Don't overwrite existing files — append a number
            if (file_exists($dest)) {
                $ext = pathinfo($name, PATHINFO_EXTENSION);
                $base = pathinfo($name, PATHINFO_FILENAME);
                $n = 1;
                while (file_exists($dest)) {
                    $dest = $real_target . '/' . $base . '-' . $n . ($ext ? '.' . $ext : '');
                    $n++;
                }
            }
            move_uploaded_file($files['tmp_name'][$i], $dest);
        }

        delete_transient(self::FILES_CACHE_KEY);

        if ($refused) {
            error_log('OnionPress Creations: refused upload of ' . implode(', ', $refused) . ' (server-executable file type)');
            wp_die(
                '<p>Not uploaded: ' . esc_html(implode(', ', $refused)) . '</p>'
                . '<p>My Creations publishes files as they are, so it does not accept files a web server could run as a program (.php and similar). Other files in this upload were not affected.</p>',
                'File type not allowed',
                array('response' => 415, 'back_link' => true)
            );
        }

        // Redirect back to avoid form resubmission
        wp_redirect(wp_get_referer() ? wp_get_referer() : home_url('/'));
        exit;
    }

    /**
     * Register "My Creations" page template
     */
    public function register_template($templates) {
        $templates['page-creations.php'] = 'My Creations';
        return $templates;
    }

    /**
     * Load the creations template from the plugin if the theme doesn't have one
     */
    public function load_template($template) {
        if (is_page()) {
            $page_template = get_page_template_slug();
            if ($page_template === 'page-creations.php') {
                // Prefer theme's template if it exists
                $theme_template = locate_template('page-creations.php');
                if ($theme_template) {
                    return $theme_template;
                }
                // Fall back to plugin's template
                $plugin_template = plugin_dir_path(__FILE__) . 'page-creations.php';
                if (file_exists($plugin_template)) {
                    return $plugin_template;
                }
            }
        }
        return $template;
    }

    /**
     * Serve files from My Creations directory (supports subdirectories).
     * Only what the Creations pages list, and their generated thumbnails:
     * a hidden file or folder is a 404 whether it is named in the request
     * or reached through a symlink.
     */
    public function serve_file() {
        if (!isset($_GET['onionpress_creation'])) {
            return;
        }

        $requested = wp_unslash($_GET['onionpress_creation']);
        $creations_dir = self::CREATIONS_DIR;

        if (!self::is_servable_path($requested) || !is_dir($creations_dir)) {
            status_header(404);
            exit;
        }

        $filepath = realpath($creations_dir . '/' . $requested);

        // Security: ensure the resolved path is a servable file inside creations dir
        $real_base = realpath($creations_dir);
        $resolved_rel = self::rel_inside($filepath, $real_base);
        if ($resolved_rel === null || !self::is_servable_path($resolved_rel)) {
            status_header(404);
            exit;
        }

        if (!is_file($filepath) || !is_readable($filepath)) {
            status_header(404);
            exit;
        }

        $basename = basename($filepath);
        $mime = wp_check_filetype($basename);
        $content_type = $mime['type'] ? $mime['type'] : 'application/octet-stream';

        // Last-Modified: standard HTTP header that lets clients (browsers,
        // mirror tools) detect changes without re-downloading. No downside.
        $mtime = filemtime($filepath);
        if ($mtime) {
            header('Last-Modified: ' . gmdate('D, d M Y H:i:s', $mtime) . ' GMT');
            // Honor If-Modified-Since with a 304 response.
            if (!empty($_SERVER['HTTP_IF_MODIFIED_SINCE'])) {
                $since = strtotime($_SERVER['HTTP_IF_MODIFIED_SINCE']);
                if ($since !== false && $since >= $mtime) {
                    status_header(304);
                    exit;
                }
            }
        }

        header('Content-Type: ' . $content_type);
        header('Content-Length: ' . filesize($filepath));

        // Allow images/media to be displayed inline, force download for others
        $inline_types = array('image/', 'video/', 'audio/', 'text/html', 'text/plain', 'application/pdf');
        $is_inline = false;
        foreach ($inline_types as $type) {
            if (strpos($content_type, $type) === 0) {
                $is_inline = true;
                break;
            }
        }

        if ($is_inline) {
            header('Content-Disposition: inline; filename="' . $basename . '"');
        } else {
            header('Content-Disposition: attachment; filename="' . $basename . '"');
        }

        readfile($filepath);
        exit;
    }

    /**
     * Create "My Creations" page on plugin activation
     */
    public function activate() {
        $page = get_page_by_path('my-creations');
        if ($page) {
            return;
        }

        wp_insert_post(array(
            'post_title'    => 'My Creations',
            'post_name'     => 'my-creations',
            'post_status'   => 'publish',
            'post_type'     => 'page',
            'post_content'  => '',
            'page_template' => 'page-creations.php',
        ));
    }

    /**
     * Helper: format file size for display
     */
    public static function format_size($bytes) {
        if ($bytes >= 1073741824) {
            return number_format($bytes / 1073741824, 1) . ' GB';
        } elseif ($bytes >= 1048576) {
            return number_format($bytes / 1048576, 1) . ' MB';
        } elseif ($bytes >= 1024) {
            return number_format($bytes / 1024, 0) . ' KB';
        }
        return $bytes . ' B';
    }

    /**
     * Helper: get file type icon
     */
    public static function file_icon($filename) {
        $ext = strtolower(pathinfo($filename, PATHINFO_EXTENSION));
        $icons = array(
            'jpg'  => '&#x1f5bc;',
            'jpeg' => '&#x1f5bc;',
            'png'  => '&#x1f5bc;',
            'gif'  => '&#x1f5bc;',
            'webp' => '&#x1f5bc;',
            'svg'  => '&#x1f5bc;',
            'heic' => '&#x1f5bc;',
            'heif' => '&#x1f5bc;',
            'pdf'  => '&#x1f4c4;',
            'html' => '&#x1f310;',
            'htm'  => '&#x1f310;',
            'mp3'  => '&#x1f3b5;',
            'wav'  => '&#x1f3b5;',
            'ogg'  => '&#x1f3b5;',
            'flac' => '&#x1f3b5;',
            'mp4'  => '&#x1f3ac;',
            'webm' => '&#x1f3ac;',
            'mov'  => '&#x1f3ac;',
            'avi'  => '&#x1f3ac;',
            'txt'  => '&#x1f4dd;',
            'md'   => '&#x1f4dd;',
            'zip'  => '&#x1f4e6;',
            'tar'  => '&#x1f4e6;',
            'gz'   => '&#x1f4e6;',
        );
        return isset($icons[$ext]) ? $icons[$ext] : '&#x1f4ce;';
    }

    /**
     * Helper: is this an image file?
     */
    public static function is_image($filename) {
        $ext = strtolower(pathinfo($filename, PATHINFO_EXTENSION));
        return in_array($ext, array('jpg', 'jpeg', 'png', 'gif', 'webp', 'svg'));
    }

    public static function is_video($filename) {
        $ext = strtolower(pathinfo($filename, PATHINFO_EXTENSION));
        return in_array($ext, array('mp4', 'webm', 'mov', 'avi', 'ogv', 'm4v'));
    }

    /**
     * Get folders and files in a specific subdirectory (one level, like Google Drive).
     * Returns array with 'folders' and 'files' keys.
     */
    public static function get_folder_contents( $subfolder = '' ) {
        $creations_dir = self::CREATIONS_DIR;
        $browse_dir = $subfolder ? $creations_dir . '/' . $subfolder : $creations_dir;

        $result = array( 'folders' => array(), 'files' => array() );

        if ($subfolder && !self::is_public_path($subfolder)) {
            return $result;
        }

        // Security: ensure resolved path is a listed folder inside creations dir
        $real_browse = realpath($browse_dir);
        $real_base = realpath($creations_dir);
        $browse_rel = self::rel_inside($real_browse, $real_base);
        if ($browse_rel === null || ($browse_rel !== '' && !self::is_public_path($browse_rel))) {
            return $result;
        }
        if (!is_dir($real_browse)) {
            return $result;
        }

        $entries = scandir($real_browse);
        foreach ($entries as $entry) {
            if ($entry[0] === '.') {
                continue;
            }
            $full_path = $real_browse . '/' . $entry;
            $rel_path = $subfolder ? $subfolder . '/' . $entry : $entry;

            if (is_dir($full_path)) {
                // Count items inside
                $count = 0;
                $sub_entries = @scandir($full_path);
                if ($sub_entries) {
                    foreach ($sub_entries as $se) {
                        if ($se[0] !== '.') $count++;
                    }
                }
                $result['folders'][] = array(
                    'name'     => $entry,
                    'rel_path' => $rel_path,
                    'count'    => $count,
                );
            } else {
                $thumb_path = $creations_dir . '/.thumbs/' . $rel_path . '.png';
                $thumb_url = null;
                if (file_exists($thumb_path)) {
                    $thumb_url = add_query_arg('onionpress_creation', urlencode('.thumbs/' . $rel_path . '.png'), home_url('/'));
                }
                $result['files'][] = array(
                    'name'      => $entry,
                    'rel_path'  => $rel_path,
                    'path'      => $full_path,
                    'size'      => filesize($full_path),
                    'time'      => filemtime($full_path),
                    'is_image'  => self::is_image($entry),
                    'is_video'  => self::is_video($entry),
                    'icon'      => self::file_icon($entry),
                    'url'       => add_query_arg('onionpress_creation', urlencode($rel_path), home_url('/')),
                    'thumb_url' => $thumb_url,
                );
            }
        }

        // Sort folders alphabetically, files by modification time (newest first)
        usort($result['folders'], function($a, $b) {
            return strcasecmp($a['name'], $b['name']);
        });
        usort($result['files'], function($a, $b) {
            return $b['time'] - $a['time'];
        });

        return $result;
    }

    /**
     * Get all files in the creations directory, recursively scanning subdirectories.
     *
     * Results are cached in a transient for FILES_CACHE_TTL seconds. The
     * creations dir is a FUSE/SSHFS mount into the host's Documents folder
     * — recursive stat()s across that boundary on every homepage render
     * piled apache workers on fuse_lock_inode and took onionpress.org
     * down for 34h on 2026-04-19. handle_upload() busts the transient so
     * freshly uploaded files show up within one page load, not a minute.
     */
    public static function get_files() {
        $cached = get_transient(self::FILES_CACHE_KEY);
        if (is_array($cached)) {
            return $cached;
        }

        $creations_dir = self::CREATIONS_DIR;
        $files = array();

        if (!is_dir($creations_dir)) {
            return $files;
        }

        // Hidden folders (.git/, .thumbs/ ...) are neither listed nor
        // descended into: not publications, and a .git can hold thousands
        // of objects to stat across the mount.
        $iterator = new \RecursiveIteratorIterator(
            new \RecursiveCallbackFilterIterator(
                new \RecursiveDirectoryIterator($creations_dir, \FilesystemIterator::SKIP_DOTS),
                function ($file_info) {
                    return $file_info->getFilename()[0] !== '.';
                }
            ),
            \RecursiveIteratorIterator::LEAVES_ONLY
        );

        foreach ($iterator as $file_info) {
            if (!$file_info->isFile() || $file_info->getFilename()[0] === '.') {
                continue;
            }
            $path = $file_info->getPathname();
            // Skip files inside the .thumbs directory
            if (strpos($path, $creations_dir . '/.thumbs') === 0) {
                continue;
            }
            $name = $file_info->getFilename();
            // Relative path from creations dir (for URL and subfolder display)
            $rel_path = substr($path, strlen($creations_dir) + 1);
            // Check for a generated thumbnail in .thumbs/
            $thumb_path = $creations_dir . '/.thumbs/' . $rel_path . '.png';
            $thumb_url = null;
            if (file_exists($thumb_path)) {
                $thumb_url = add_query_arg('onionpress_creation', urlencode('.thumbs/' . $rel_path . '.png'), home_url('/'));
            }
            $files[] = array(
                'name'      => $name,
                'rel_path'  => $rel_path,
                'path'      => $path,
                'size'      => $file_info->getSize(),
                'time'      => $file_info->getMTime(),
                'is_image'  => self::is_image($name),
                'is_video'  => self::is_video($name),
                'icon'      => self::file_icon($name),
                'url'       => add_query_arg('onionpress_creation', urlencode($rel_path), home_url('/')),
                'thumb_url' => $thumb_url,
            );
        }

        // Sort by modification time, newest first
        usort($files, function($a, $b) {
            return $b['time'] - $a['time'];
        });

        set_transient(self::FILES_CACHE_KEY, $files, self::FILES_CACHE_TTL);
        return $files;
    }
}

OnionPress_Creations::get_instance();

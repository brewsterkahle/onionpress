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

    private static $instance = null;

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
     * Handle file uploads from the My Creations page.
     * Requires upload_files capability and a valid nonce.
     */
    public function handle_upload() {
        if (empty($_FILES['creations_upload']) || empty($_POST['creations_upload_nonce'])) {
            return;
        }
        if (!current_user_can('upload_files')) {
            return;
        }
        if (!wp_verify_nonce($_POST['creations_upload_nonce'], 'creations_upload')) {
            return;
        }

        $creations_dir = self::CREATIONS_DIR;
        // Support uploading into a subfolder
        $subfolder = isset($_POST['creations_upload_folder']) ? sanitize_text_field($_POST['creations_upload_folder']) : '';
        $target_dir = $subfolder ? $creations_dir . '/' . $subfolder : $creations_dir;
        // Security: ensure target is inside creations dir
        $real_target = realpath($target_dir);
        $real_base = realpath($creations_dir);
        if (!$real_target || !$real_base || ($real_target !== $real_base && strpos($real_target, $real_base . '/') !== 0)) {
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

        for ($i = 0; $i < $count; $i++) {
            if ($files['error'][$i] !== UPLOAD_ERR_OK) {
                continue;
            }
            $name = sanitize_file_name($files['name'][$i]);
            if (empty($name) || $name[0] === '.') {
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
     * Serve files from My Creations directory (supports subdirectories)
     */
    public function serve_file() {
        if (!isset($_GET['onionpress_creation'])) {
            return;
        }

        $requested = sanitize_text_field(wp_unslash($_GET['onionpress_creation']));
        $creations_dir = self::CREATIONS_DIR;

        if (!is_dir($creations_dir)) {
            status_header(404);
            exit;
        }

        $filepath = realpath($creations_dir . '/' . $requested);

        // Security: ensure the resolved path is inside creations dir
        $real_base = realpath($creations_dir);
        if (!$filepath || !$real_base || strpos($filepath, $real_base . '/') !== 0) {
            status_header(404);
            exit;
        }

        if (!is_file($filepath) || !is_readable($filepath)) {
            status_header(404);
            exit;
        }

        /**
         * Gate file downloads. Member Vault hooks here to require
         * validated-member login when the vault is enabled.
         *
         * @param bool   $allowed  Whether download is permitted.
         * @param string $filepath Absolute path inside Creations.
         */
        $allowed = apply_filters('onionpress_creations_allow_download', true, $filepath);
        if (!$allowed) {
            if (!is_user_logged_in()) {
                auth_redirect();
            }
            status_header(403);
            header('Content-Type: text/plain; charset=utf-8');
            echo 'Access denied. Validated member credentials are required.';
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

        // Security: ensure resolved path is inside creations dir
        $real_browse = realpath($browse_dir);
        $real_base = realpath($creations_dir);
        if (!$real_browse || !$real_base || ($real_browse !== $real_base && strpos($real_browse, $real_base . '/') !== 0)) {
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

        $iterator = new \RecursiveIteratorIterator(
            new \RecursiveDirectoryIterator($creations_dir, \FilesystemIterator::SKIP_DOTS),
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

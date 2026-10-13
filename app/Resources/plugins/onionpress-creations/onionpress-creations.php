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
    // Renamed when what get_files() leaves out, or what the cache holds,
    // changes, so a list cached by the previous version is not reused
    // (v2: blog-archives/ left out; v3: no URLs, see listing_entry()).
    const FILES_CACHE_KEY = 'op_creations_files_v3';
    // The home page shows the newest four files of the whole tree, so a
    // rebuild walks all of it: at most every 10 minutes, or sooner when
    // the top folder or its thumbnails change, or after an upload.
    const FILES_CACHE_TTL = 600;  // seconds
    // One folder's listing (get_folder_contents()), keyed by its path.
    const FOLDER_CACHE_PREFIX = 'op_creations_dir_v1_';
    // Rebuilt sooner when the folder or its thumbnail folder changes.
    const FOLDER_CACHE_TTL = 300;  // seconds
    // How long a cached listing is kept to hand out while it is rebuilt.
    const CACHE_KEEP_TTL = 86400;  // seconds
    const CACHE_LOCK_TTL = 60;  // seconds
    // Network-wide counter in every cache stamp: handle_upload() raises it,
    // so an upload shows on every site at the next page load.
    const CACHE_GENERATION_OPTION = 'op_creations_generation';
    // Streamed in pieces this size, so a large file never sits in memory.
    const SEND_CHUNK = 1048576;
    // File types a web server may run instead of serve. My Creations sits
    // under DocumentRoot, so these are refused at upload; the WordPress
    // image (onionpress-creations.conf) and the multisite .htaccess also
    // refuse to serve them from /wp-content/creations/.
    const BLOCKED_EXT_RE = '/\.(php[0-9]*|pht(ml)?|phar|phps|cgi|shtml)$/i';
    // Generated thumbnails: .thumbs/<path of a listed file>.png
    const THUMBS_PREFIX = '.thumbs/';
    // The blog importer wrote its WXR snapshots (drafts, private posts,
    // authors' emails) to this top-level folder through 2.5.2. It keeps them
    // in the hidden .blog-archives/ now and moves this folder there
    // (onionpress-social-archive-blog.php); until it has, this name is not
    // published either. Compared without case: the Mac's disk ignores it.
    const LEGACY_SNAPSHOTS_DIR = 'blog-archives';

    private static $instance = null;

    /**
     * Whether $rel, a path relative to CREATIONS_DIR, names something My
     * Creations publishes. Hidden files and folders (.git/, .DS_Store,
     * .thumbs/, .blog-archives/ ...) are never listed, so they are never
     * served or written to either: no component may be empty or start with
     * '.'. Nor may the first be LEGACY_SNAPSHOTS_DIR.
     */
    public static function is_public_path($rel) {
        if (!is_string($rel) || $rel === '' || strpos($rel, "\0") !== false) {
            return false;
        }
        $parts = explode('/', $rel);
        if (strcasecmp($parts[0], self::LEGACY_SNAPSHOTS_DIR) === 0) {
            return false;
        }
        foreach ($parts as $part) {
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
     * Requires can_upload() and a valid nonce. Refuses BLOCKED_EXT_RE
     * types and dotfiles, and tells the uploader about every file that did
     * not arrive: refused, failed in PHP's upload (too large, partial ...),
     * not moved into place, or dropped by PHP for exceeding
     * max_file_uploads (counted against the form's creations_upload_count).
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
        $subfolder = isset($_POST['creations_upload_folder']) ? sanitize_text_field(wp_unslash($_POST['creations_upload_folder'])) : '';
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
            error_log('OnionPress Creations: upload folder missing or not writable');
            wp_die(
                '<p>Nothing was uploaded: the folder is missing or OnionPress cannot write to it.</p>',
                'Upload failed',
                array('response' => 500, 'back_link' => true)
            );
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

        $refused = array();  // server-executable types and dotfiles
        $failed = array();   // array(display name, why), one per file
        $received = 0;
        for ($i = 0; $i < $count; $i++) {
            $error = (int) $files['error'][$i];
            if ($error === UPLOAD_ERR_NO_FILE) {
                continue;
            }
            $received++;
            $name = sanitize_file_name($files['name'][$i]);
            $shown = $name !== '' ? $name : 'file ' . ($i + 1);
            if ($error !== UPLOAD_ERR_OK) {
                $failed[] = array($shown, self::upload_error_text($error));
                continue;
            }
            if ($name === '' || $name[0] === '.') {
                $refused[] = $shown;
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
            if (!move_uploaded_file($files['tmp_name'][$i], $dest)) {
                $failed[] = array($name, 'could not be saved into the folder');
            }
        }

        self::clear_listing_caches();

        // PHP keeps the first max_file_uploads files of a request and drops
        // the rest without telling the script; the page's form says how
        // many it sent.
        $sent = isset($_POST['creations_upload_count']) ? (int) $_POST['creations_upload_count'] : 0;
        $dropped = $sent > $received ? $sent - $received : 0;

        if ($refused || $failed || $dropped) {
            $html = '';
            if ($failed) {
                $items = array();
                foreach ($failed as $f) {
                    $items[] = '<li>' . esc_html($f[0]) . ': ' . esc_html($f[1]) . '</li>';
                }
                $html .= '<p>Not uploaded:</p><ul>' . implode('', $items) . '</ul>';
                error_log('OnionPress Creations: ' . count($failed) . ' upload(s) failed');
            }
            if ($dropped) {
                $html .= '<p>' . esc_html($dropped) . ' more file(s) were not received: at most '
                    . esc_html((int) ini_get('max_file_uploads'))
                    . ' files can be uploaded at once. Upload the rest in smaller groups.</p>';
                error_log('OnionPress Creations: ' . $dropped . ' upload(s) dropped over max_file_uploads');
            }
            if ($refused) {
                error_log('OnionPress Creations: refused upload of ' . implode(', ', $refused) . ' (server-executable file type or hidden name)');
                $html .= '<p>Not uploaded: ' . esc_html(implode(', ', $refused)) . '</p>'
                    . '<p>My Creations publishes files as they are, so it does not accept files a web server could run as a program (.php and similar), nor hidden files (names starting with a dot), which it never publishes.</p>';
            }
            $html .= '<p>Other files in this upload were not affected.</p>';
            wp_die(
                $html,
                ($failed || $dropped) ? 'Some files were not uploaded' : 'File type not allowed',
                array('response' => $failed ? 500 : ($dropped ? 413 : 415), 'back_link' => true)
            );
        }

        // Redirect back to the folder just uploaded into, to avoid form
        // resubmission. Not wp_get_referer(): the form posts to this page,
        // so the referer equals the request URI and it returns false.
        $page_id = get_queried_object_id();
        $back = $page_id ? get_permalink($page_id) : false;
        if (!$back) {
            $back = home_url('/');
        } elseif ($subfolder) {
            $back = add_query_arg('folder', urlencode($subfolder), $back);
        }
        wp_safe_redirect($back);
        exit;
    }

    /**
     * What went wrong with one file of an upload, for the uploader.
     */
    private static function upload_error_text($error) {
        switch ($error) {
            case UPLOAD_ERR_INI_SIZE:
            case UPLOAD_ERR_FORM_SIZE:
                return 'larger than the ' . size_format(wp_max_upload_size()) . ' upload limit';
            case UPLOAD_ERR_PARTIAL:
                return 'the upload was interrupted';
            case UPLOAD_ERR_NO_TMP_DIR:
            case UPLOAD_ERR_CANT_WRITE:
                return 'the server could not store it';
            default:
                return 'the upload failed (error ' . (int) $error . ')';
        }
    }

    /**
     * Limits the upload form checks before it sends anything: PHP drops
     * files past max_file_uploads silently, and a request over
     * post_max_size arrives with no files (and no nonce) at all.
     */
    public static function upload_limits() {
        return array(
            'max_files' => (int) ini_get('max_file_uploads'),
            'max_bytes' => (int) wp_convert_hr_to_bytes(ini_get('post_max_size')),
        );
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
     *
     * Answers conditional requests (ETag, Last-Modified) with 304 and a
     * single byte range with 206, so a video can seek and a download
     * interrupted over Tor resumes where it stopped.
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

        $size = filesize($filepath);
        $mtime = filemtime($filepath);
        if ($size === false) {
            status_header(404);
            exit;
        }
        // Validators: Last-Modified, and an ETag from mtime and size (as
        // Apache makes one), so a client can revalidate or resume safely.
        $etag = self::etag($mtime, $size);
        $last_modified = $mtime ? gmdate('D, d M Y H:i:s', $mtime) . ' GMT' : null;
        header('ETag: ' . $etag);
        if ($last_modified) {
            header('Last-Modified: ' . $last_modified);
        }
        header('Accept-Ranges: bytes');

        // If-None-Match takes precedence over If-Modified-Since (RFC 9110).
        if (isset($_SERVER['HTTP_IF_NONE_MATCH'])) {
            if (self::etag_matches($_SERVER['HTTP_IF_NONE_MATCH'], $etag)) {
                status_header(304);
                exit;
            }
        } elseif ($mtime && !empty($_SERVER['HTTP_IF_MODIFIED_SINCE'])) {
            $since = strtotime($_SERVER['HTTP_IF_MODIFIED_SINCE']);
            if ($since !== false && $since >= $mtime) {
                status_header(304);
                exit;
            }
        }

        // A Range is honored only while If-Range (when sent) still names
        // this version of the file; otherwise the whole file goes out.
        $range = null;
        if (isset($_SERVER['HTTP_RANGE'])) {
            $if_range = isset($_SERVER['HTTP_IF_RANGE']) ? trim($_SERVER['HTTP_IF_RANGE']) : null;
            if ($if_range === null || $if_range === $etag
                    || ($last_modified !== null && $if_range === $last_modified)) {
                $range = self::parse_range($_SERVER['HTTP_RANGE'], $size);
            }
        }
        if ($range === false) {
            status_header(416);
            header('Content-Range: bytes */' . $size);
            exit;
        }

        header('Content-Type: ' . $content_type);

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

        if ($range) {
            list($first, $last) = $range;
            status_header(206);
            header('Content-Range: bytes ' . $first . '-' . $last . '/' . $size);
        } else {
            $first = 0;
            $last = $size - 1;
        }
        $length = $last - $first + 1;
        header('Content-Length: ' . $length);

        if (isset($_SERVER['REQUEST_METHOD']) && $_SERVER['REQUEST_METHOD'] === 'HEAD') {
            exit;
        }
        self::send_bytes($filepath, $first, $length);
        exit;
    }

    /**
     * The ETag serve_file() sends for a file of this mtime and size.
     */
    public static function etag($mtime, $size) {
        return '"' . dechex((int) $mtime) . '-' . dechex((int) $size) . '"';
    }

    /**
     * Whether an If-None-Match header names $etag (weak comparison: a
     * W/ prefix is ignored) or is "*".
     */
    public static function etag_matches($header, $etag) {
        $header = trim((string) $header);
        if ($header === '*') {
            return true;
        }
        foreach (explode(',', $header) as $tag) {
            $tag = trim($tag);
            if (strncmp($tag, 'W/', 2) === 0) {
                $tag = substr($tag, 2);
            }
            if ($tag === $etag) {
                return true;
            }
        }
        return false;
    }

    /**
     * The byte range a Range header asks of a file of $size bytes:
     * array(first, last) inclusive; false when it cannot be satisfied
     * (answer 416); null when the whole file should be sent instead (no
     * range, a malformed one, another unit, or several ranges, which a
     * server may answer with the whole file).
     */
    public static function parse_range($header, $size) {
        if (!is_string($header) || !preg_match('/^\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*$/i', $header, $m)) {
            return null;
        }
        // Numbers longer than any file size are clamped, never overflowed.
        $num = function ($digits) {
            return strlen($digits) > 18 ? PHP_INT_MAX : (int) $digits;
        };
        if ($m[1] === '') {
            if ($m[2] === '') {
                return null;
            }
            // bytes=-N: the last N bytes.
            $suffix = $num($m[2]);
            if ($suffix === 0 || $size === 0) {
                return false;
            }
            return array(max(0, $size - $suffix), $size - 1);
        }
        $first = $num($m[1]);
        if ($m[2] !== '' && $num($m[2]) < $first) {
            return null;
        }
        if ($first >= $size) {
            return false;
        }
        $last = ($m[2] === '') ? $size - 1 : min($num($m[2]), $size - 1);
        return array($first, $last);
    }

    /**
     * Send $length bytes of $path from offset $first, a piece at a time,
     * stopping when the client goes away.
     */
    private static function send_bytes($path, $first, $length) {
        while (ob_get_level() > 0) {
            ob_end_clean();
        }
        $fh = fopen($path, 'rb');
        if (!$fh) {
            return;
        }
        if ($first > 0 && fseek($fh, $first) !== 0) {
            fclose($fh);
            return;
        }
        @set_time_limit(0);
        while ($length > 0 && !feof($fh)) {
            $chunk = fread($fh, min(self::SEND_CHUNK, $length));
            if ($chunk === false || $chunk === '') {
                break;
            }
            echo $chunk;
            flush();
            $length -= strlen($chunk);
            if (connection_aborted()) {
                break;
            }
        }
        fclose($fh);
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
     *
     * Cached per folder (see cached_listing()): a listing stats every
     * entry and counts every subfolder's items across the host mount,
     * the access pattern behind the outage described at get_files().
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

        // Adding, removing or renaming an entry changes the folder's mtime,
        // a new thumbnail its .thumbs/ folder's: either rebuilds at once.
        $thumbs_dir = $creations_dir . '/' . self::THUMBS_PREFIX . $subfolder;
        $stamp = @filemtime($real_browse) . '|' . @filemtime($thumbs_dir);

        $listing = self::cached_listing(
            self::FOLDER_CACHE_PREFIX . md5($subfolder),
            $stamp,
            self::FOLDER_CACHE_TTL,
            function () use ($subfolder, $real_browse) {
                return self::scan_folder($subfolder, $real_browse);
            }
        );

        foreach ($listing['folders'] as $folder) {
            $result['folders'][] = $folder;
        }
        foreach ($listing['files'] as $file) {
            $result['files'][] = self::listing_entry($file);
        }
        return $result;
    }

    /**
     * One folder's entries, for get_folder_contents() to cache: no URLs
     * (listing_entry() adds those per request).
     */
    private static function scan_folder($subfolder, $real_browse) {
        $creations_dir = self::CREATIONS_DIR;
        $result = array( 'folders' => array(), 'files' => array() );

        $entries = @scandir($real_browse);
        if (!$entries) {
            return $result;
        }
        foreach ($entries as $entry) {
            $rel_path = $subfolder ? $subfolder . '/' . $entry : $entry;
            if (!self::is_public_path($rel_path)) {
                continue;
            }
            $full_path = $real_browse . '/' . $entry;

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
                $result['files'][] = array(
                    'name'      => $entry,
                    'rel_path'  => $rel_path,
                    'path'      => $full_path,
                    'size'      => @filesize($full_path),
                    'time'      => @filemtime($full_path),
                    'has_thumb' => file_exists($creations_dir . '/.thumbs/' . $rel_path . '.png'),
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
     * A cached file entry as the templates use it. URLs are made here,
     * per request, never cached: the domain map makes home_url() the
     * request's host (localhost for the owner), and a cached localhost
     * link must not reach a Tor visitor.
     */
    private static function listing_entry($file) {
        $name = $file['name'];
        $rel_path = $file['rel_path'];
        $thumb_url = null;
        if (!empty($file['has_thumb'])) {
            $thumb_url = add_query_arg('onionpress_creation', urlencode('.thumbs/' . $rel_path . '.png'), home_url('/'));
        }
        return array(
            'name'      => $name,
            'rel_path'  => $rel_path,
            'path'      => $file['path'],
            'size'      => $file['size'],
            'time'      => $file['time'],
            'is_image'  => self::is_image($name),
            'is_video'  => self::is_video($name),
            'icon'      => self::file_icon($name),
            'url'       => add_query_arg('onionpress_creation', urlencode($rel_path), home_url('/')),
            'thumb_url' => $thumb_url,
        );
    }

    /**
     * $build()'s result, cached in the transient $key.
     *
     * Rebuilt once it is $ttl seconds old or $stamp (with the upload
     * generation in front) no longer matches. While one request rebuilds,
     * the others get the previous listing instead of all scanning the
     * mount at once: piled-up scans are what took the site down.
     */
    private static function cached_listing($key, $stamp, $ttl, $build) {
        $stamp = (int) get_site_option(self::CACHE_GENERATION_OPTION, 0) . '|' . $stamp;
        $entry = get_transient($key);
        $have = is_array($entry) && isset($entry['stamp'], $entry['built'])
            && isset($entry['data']) && is_array($entry['data']);
        if ($have && $entry['stamp'] === $stamp && time() - (int) $entry['built'] < $ttl) {
            return $entry['data'];
        }
        $lock = $key . '_lock';
        if ($have && get_transient($lock)) {
            return $entry['data'];
        }
        set_transient($lock, 1, self::CACHE_LOCK_TTL);
        $data = $build();
        set_transient($key, array('stamp' => $stamp, 'built' => time(), 'data' => $data), self::CACHE_KEEP_TTL);
        delete_transient($lock);
        return $data;
    }

    /**
     * Make every cached listing, on every site of the network, rebuild at
     * its next use (handle_upload()).
     */
    public static function clear_listing_caches() {
        update_site_option(self::CACHE_GENERATION_OPTION,
            (int) get_site_option(self::CACHE_GENERATION_OPTION, 0) + 1);
    }

    /**
     * Get all files in the creations directory, recursively scanning subdirectories.
     *
     * Results are cached in a transient (see cached_listing()). The
     * creations dir is a FUSE/SSHFS mount into the host's Documents folder
     * — recursive stat()s across that boundary on every homepage render
     * piled apache workers on fuse_lock_inode and took onionpress.org
     * down for 34h on 2026-04-19. handle_upload() raises the cache
     * generation so freshly uploaded files show up at the next page load;
     * files added on the host show within FILES_CACHE_TTL (at once when
     * they land in the top folder).
     */
    public static function get_files() {
        $creations_dir = self::CREATIONS_DIR;
        if (!is_dir($creations_dir)) {
            return array();
        }
        $stamp = @filemtime($creations_dir) . '|' . @filemtime($creations_dir . '/.thumbs');
        $files = self::cached_listing(self::FILES_CACHE_KEY, $stamp, self::FILES_CACHE_TTL, array(__CLASS__, 'scan_tree'));
        return array_map(array(__CLASS__, 'listing_entry'), $files);
    }

    /**
     * Every listed file of the tree, newest first, for get_files() to cache.
     */
    public static function scan_tree() {
        $creations_dir = self::CREATIONS_DIR;
        $files = array();

        // Hidden folders (.git/, .thumbs/ ...) and the legacy blog-archives/
        // are neither listed nor descended into: not publications, and a
        // .git can hold thousands of objects to stat across the mount.
        $iterator = new \RecursiveIteratorIterator(
            new \RecursiveCallbackFilterIterator(
                new \RecursiveDirectoryIterator($creations_dir, \FilesystemIterator::SKIP_DOTS),
                function ($file_info) use ($creations_dir) {
                    return $file_info->getFilename()[0] !== '.'
                        && self::is_public_path(substr($file_info->getPathname(), strlen($creations_dir) + 1));
                }
            ),
            \RecursiveIteratorIterator::LEAVES_ONLY
        );

        try {
            foreach ($iterator as $file_info) {
                if (!$file_info->isFile() || $file_info->getFilename()[0] === '.') {
                    continue;
                }
                $path = $file_info->getPathname();
                // Relative path from creations dir (for URL and subfolder display)
                $rel_path = substr($path, strlen($creations_dir) + 1);
                $files[] = array(
                    'name'      => $file_info->getFilename(),
                    'rel_path'  => $rel_path,
                    'path'      => $path,
                    'size'      => $file_info->getSize(),
                    'time'      => $file_info->getMTime(),
                    // Generated thumbnail in .thumbs/
                    'has_thumb' => file_exists($creations_dir . '/.thumbs/' . $rel_path . '.png'),
                );
            }
        } catch (\UnexpectedValueException $e) {
            // A folder that vanished or cannot be read mid-walk: list what
            // was found rather than fail the page.
            error_log('OnionPress Creations: listing stopped early: ' . $e->getMessage());
        }

        // Sort by modification time, newest first
        usort($files, function($a, $b) {
            return $b['time'] - $a['time'];
        });

        return $files;
    }
}

OnionPress_Creations::get_instance();

<?php
/**
 * Plugin Name: OnionPress Hit Counter
 * Plugin URI: https://github.com/brewsterkahle/onionpress
 * Description: Retro-style animated hit counter with persistent storage that survives reboots and upgrades
 * Version: 1.0.0
 * Author: OnionPress
 * Author URI: https://github.com/brewsterkahle/onionpress
 * License: AGPL-3.0
 * Text Domain: onionpress-hit-counter
 */

if (!defined('ABSPATH')) {
    exit; // Exit if accessed directly
}

class OnionPress_Hit_Counter {

    private static $instance = null;
    private $counter_file;

    public static function get_instance() {
        if (null === self::$instance) {
            self::$instance = new self();
        }
        return self::$instance;
    }

    private function __construct() {
        // Store counter in persistent location outside WordPress directory
        // This location should be mounted as a Docker volume
        $this->counter_file = '/var/lib/onionpress/hit-counter.txt';

        // Ensure directory exists
        $this->ensure_counter_directory();

        // Register hooks
        add_shortcode('hit_counter', array($this, 'render_counter'));
        add_action('wp_enqueue_scripts', array($this, 'enqueue_assets'));
        add_action('wp_ajax_increment_counter', array($this, 'ajax_increment_counter'));
        add_action('wp_ajax_nopriv_increment_counter', array($this, 'ajax_increment_counter'));
    }

    /**
     * Ensure the counter directory exists
     */
    private function ensure_counter_directory() {
        $dir = dirname($this->counter_file);
        if (!file_exists($dir)) {
            @mkdir($dir, 0755, true);
        }

        // Initialize counter file if it doesn't exist
        if (!file_exists($this->counter_file)) {
            file_put_contents($this->counter_file, '0');
            @chmod($this->counter_file, 0644);
        }
    }

    /**
     * Get current counter value
     */
    private function get_counter() {
        if (!file_exists($this->counter_file)) {
            return 0;
        }

        $count = @file_get_contents($this->counter_file);
        return (int) $count;
    }

    /**
     * Increment counter and return new value
     */
    private function increment_counter() {
        $count = $this->get_counter();
        $count++;

        // Write new count
        file_put_contents($this->counter_file, $count);

        return $count;
    }

    /**
     * AJAX handler to increment counter
     */
    public function ajax_increment_counter() {
        $new_count = $this->increment_counter();

        wp_send_json_success(array(
            'count' => $new_count,
            'formatted' => $this->format_counter($new_count)
        ));
    }

    /**
     * Format counter value for display (pad with zeros)
     */
    private function format_counter($count, $digits = 6) {
        return str_pad($count, $digits, '0', STR_PAD_LEFT);
    }

    /**
     * Render the hit counter shortcode
     */
    public function render_counter($atts) {
        $atts = shortcode_atts(array(
            'style' => 'odometer',  // odometer, digital, classic
            'digits' => 6,
            'auto_increment' => 'true',
        ), $atts);

        $count = $this->get_counter();
        $formatted = $this->format_counter($count, $atts['digits']);

        ob_start();
        ?>
        <div class="onionpress-hit-counter"
             data-style="<?php echo esc_attr($atts['style']); ?>"
             data-auto-increment="<?php echo esc_attr($atts['auto_increment']); ?>"
             data-current-count="<?php echo esc_attr($count); ?>">

            <div class="hit-counter-display hit-counter-<?php echo esc_attr($atts['style']); ?>">
                <?php
                // Render each digit as a separate element for animation
                $digits_array = str_split($formatted);
                foreach ($digits_array as $digit) {
                    ?>
                    <span class="counter-digit" data-digit="<?php echo esc_attr($digit); ?>">
                        <span class="digit-inner"><?php echo esc_html($digit); ?></span>
                    </span>
                    <?php
                }
                ?>
            </div>

            <div class="hit-counter-label">
                <span class="counter-eye">👁️</span>
                <span class="counter-text">Visitors</span>
            </div>
        </div>
        <?php
        return ob_get_clean();
    }

    /**
     * Enqueue JavaScript and CSS
     */
    public function enqueue_assets() {
        wp_enqueue_style(
            'onionpress-hit-counter',
            plugins_url('assets/hit-counter.css', __FILE__),
            array(),
            '1.0.0'
        );

        wp_enqueue_script(
            'onionpress-hit-counter',
            plugins_url('assets/hit-counter.js', __FILE__),
            array('jquery'),
            '1.0.0',
            true
        );

        // Pass AJAX URL to JavaScript
        wp_localize_script('onionpress-hit-counter', 'onionpressCounter', array(
            'ajax_url' => admin_url('admin-ajax.php'),
            'nonce' => wp_create_nonce('onionpress_counter_nonce')
        ));
    }
}

// Initialize plugin
OnionPress_Hit_Counter::get_instance();

<?php
/**
 * Plugin Name: OnionPress Hit Counter
 * Plugin URI: https://github.com/brewsterkahle/onionpress
 * Description: Retro-style animated hit counter with persistent storage that survives reboots and upgrades
 * Version: 1.2.0
 * Author: OnionPress
 * Author URI: https://github.com/brewsterkahle/onionpress
 * License: AGPL-3.0-or-later
 * License URI: https://www.gnu.org/licenses/agpl-3.0.html
 * Text Domain: onionpress-hit-counter
 */

if (!defined('ABSPATH')) {
    exit; // Exit if accessed directly
}

class OnionPress_Hit_Counter {

    private static $instance = null;
    private $option_key = 'onionpress_hit_counter';

    public static function get_instance() {
        if (null === self::$instance) {
            self::$instance = new self();
        }
        return self::$instance;
    }

    private function __construct() {
        // Register hooks
        add_shortcode('hit_counter', array($this, 'render_counter'));
        add_action('wp_enqueue_scripts', array($this, 'enqueue_assets'));
        add_action('wp_ajax_increment_counter', array($this, 'ajax_increment_counter'));
        add_action('wp_ajax_nopriv_increment_counter', array($this, 'ajax_increment_counter'));
        add_action('wp_ajax_get_counter', array($this, 'ajax_get_counter'));
        add_action('wp_ajax_nopriv_get_counter', array($this, 'ajax_get_counter'));
    }

    /**
     * Get current counter value
     */
    private function get_counter() {
        return (int) get_option($this->option_key, 0);
    }

    /**
     * Increment counter and return new value
     */
    private function increment_counter() {
        $count = $this->get_counter() + 1;
        update_option($this->option_key, $count, 'no');
        return $count;
    }

    /**
     * AJAX handler to increment counter.
     *
     * Anonymous increments are the feature, so the nonce is the one the
     * page's script was given (issue #398: it was minted and never
     * checked). A request without it, or with a stale one (a page cached
     * past the nonce's life), gets the count as it is and counts nothing:
     * the counter still shows, and nothing off the page moves it.
     */
    public function ajax_increment_counter() {
        if (check_ajax_referer('onionpress_counter_nonce', 'nonce', false)) {
            $count = $this->increment_counter();
        } else {
            $count = $this->get_counter();
        }

        wp_send_json_success(array(
            'count' => $count,
            'formatted' => $this->format_counter($count)
        ));
    }

    /**
     * AJAX handler to get current counter
     */
    public function ajax_get_counter() {
        $count = $this->get_counter();

        wp_send_json_success(array(
            'count' => $count,
            'formatted' => $this->format_counter($count)
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
            '1.2.0'
        );

        wp_enqueue_script(
            'onionpress-hit-counter',
            plugins_url('assets/hit-counter.js', __FILE__),
            array('jquery'),
            '1.2.0',
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

<?php
/**
 * Plugin Name: OnionPress Offline Publishing
 * Description: WordPress runs locally in Docker — override browser offline
 *              detection so the Gutenberg editor can always publish.
 * Version:     1.0
 * Network:     true
 * License:     AGPL-3.0-or-later
 * License URI: https://www.gnu.org/licenses/agpl-3.0.html
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

/**
 * Inject a script into the admin that forces navigator.onLine = true.
 *
 * The Gutenberg editor checks navigator.onLine and refuses to publish
 * when the browser thinks it's offline. Since WordPress is running
 * locally (localhost), publishing always works — the browser's offline
 * detection is a false alarm.
 *
 * We also suppress the 'offline' window event and fire 'online' on load
 * to clear any pre-existing offline state in Gutenberg's heartbeat.
 */
function onionpress_force_online() {
    if ( ! is_admin() ) {
        return;
    }
    ?>
    <script>
    (function() {
        // Override navigator.onLine to always return true
        Object.defineProperty(navigator, 'onLine', {
            get: function() { return true; },
            configurable: true
        });

        // Suppress 'offline' events and fire 'online' to reset Gutenberg state
        window.addEventListener('offline', function(e) {
            e.stopImmediatePropagation();
            window.dispatchEvent(new Event('online'));
        }, true);

        // If page loaded while "offline", fire online event to clear the state
        if (!window.navigator.onLine) {
            window.dispatchEvent(new Event('online'));
        }
    })();
    </script>
    <?php
}
add_action( 'admin_head', 'onionpress_force_online', 1 );

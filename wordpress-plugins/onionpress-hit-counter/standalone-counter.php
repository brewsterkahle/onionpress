<?php
/**
 * Standalone Hit Counter API Endpoint
 *
 * This file can be called directly without WordPress for use in static pages
 * Place in wp-content/plugins/onionpress-hit-counter/
 *
 * Usage:
 * GET  /wp-content/plugins/onionpress-hit-counter/standalone-counter.php?action=get
 * POST /wp-content/plugins/onionpress-hit-counter/standalone-counter.php?action=increment
 */

// Counter storage file
$counter_file = '/var/lib/onionpress/hit-counter.txt';

// Ensure directory exists
$dir = dirname($counter_file);
if (!file_exists($dir)) {
    @mkdir($dir, 0755, true);
}

// Initialize counter if doesn't exist
if (!file_exists($counter_file)) {
    file_put_contents($counter_file, '0');
    @chmod($counter_file, 0644);
}

/**
 * Get current counter value
 */
function get_counter($file) {
    if (!file_exists($file)) {
        return 0;
    }
    $count = @file_get_contents($file);
    return (int) $count;
}

/**
 * Increment counter
 */
function increment_counter($file) {
    $count = get_counter($file);
    $count++;
    file_put_contents($file, $count);
    return $count;
}

/**
 * Format counter with leading zeros
 */
function format_counter($count, $digits = 6) {
    return str_pad($count, $digits, '0', STR_PAD_LEFT);
}

// Handle API requests
header('Content-Type: application/json');

$action = isset($_GET['action']) ? $_GET['action'] : (isset($_POST['action']) ? $_POST['action'] : 'get');

if ($action === 'increment') {
    $new_count = increment_counter($counter_file);
    echo json_encode(array(
        'success' => true,
        'count' => $new_count,
        'formatted' => format_counter($new_count)
    ));
} else {
    $count = get_counter($counter_file);
    echo json_encode(array(
        'success' => true,
        'count' => $count,
        'formatted' => format_counter($count)
    ));
}

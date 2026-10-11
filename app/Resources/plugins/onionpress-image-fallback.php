<?php
/**
 * Plugin Name: OnionPress Image Derivative Fallback
 * Description: OnionPress backups carry original images only; after a
 *              restore the resized copies (-WxH sizes, -scaled) are rebuilt
 *              in the background by `wp media regenerate --only-missing`
 *              (issue #257). Until that finishes, any size whose file is
 *              not on disk yet is served from the original (or -scaled)
 *              file instead — heavier images for a while, never broken
 *              ones. Does nothing once the regeneration marker is gone.
 * Version:     1.0
 * Network:     true
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

/**
 * The regeneration sweep removes this file when every size is rebuilt. It
 * arrives inside the backup's wp-content/uploads/, so a restore by any
 * OnionPress version leaves it in place. One stat per request.
 */
function onionpress_derivatives_pending() {
    static $pending = null;
    if ( $pending === null ) {
        $pending = file_exists( WP_CONTENT_DIR . '/uploads/.onionpress-regenerate-derivatives' );
    }
    return $pending;
}

/** Directory (with trailing slash) holding an attachment's files, or ''. */
function onionpress_attachment_dir( $attachment_id ) {
    $file = get_attached_file( $attachment_id );
    if ( ! $file ) {
        return '';
    }
    return trailingslashit( dirname( $file ) );
}

/**
 * URL of the largest file that exists for an attachment: the attached
 * (-scaled, if any) file when present, else the pristine original.
 */
function onionpress_derivative_fallback_url( $attachment_id ) {
    $file = get_attached_file( $attachment_id );
    if ( $file && file_exists( $file ) ) {
        return wp_get_attachment_url( $attachment_id );
    }
    if ( function_exists( 'wp_get_original_image_url' ) ) {
        $orig = wp_get_original_image_url( $attachment_id );
        if ( $orig ) {
            return $orig;
        }
    }
    return wp_get_attachment_url( $attachment_id );
}

// The attachment URL itself points at the -scaled copy of a big image;
// while that copy is missing, hand out the original. Re-entrancy guard:
// wp_get_original_image_url() calls wp_get_attachment_url() to build its
// answer.
add_filter( 'wp_get_attachment_url', function ( $url, $attachment_id ) {
    static $busy = false;
    if ( $busy || ! $url || ! onionpress_derivatives_pending() ) {
        return $url;
    }
    if ( ! preg_match( '/-scaled\.(jpe?g|png|gif|webp|avif)$/i', $url ) ) {
        return $url;
    }
    $file = get_attached_file( $attachment_id );
    if ( ! $file || file_exists( $file ) || ! function_exists( 'wp_get_original_image_url' ) ) {
        return $url;
    }
    $busy = true;
    $orig = wp_get_original_image_url( $attachment_id );
    $busy = false;
    return $orig ? $orig : $url;
}, 10, 2 );

// Theme-rendered images (featured images, galleries, wp_get_attachment_image).
add_filter( 'wp_get_attachment_image_src', function ( $image, $attachment_id, $size, $icon ) {
    if ( ! onionpress_derivatives_pending() || ! is_array( $image ) || empty( $image[0] ) ) {
        return $image;
    }
    // [url, width, height, is_intermediate]
    $dir = onionpress_attachment_dir( $attachment_id );
    if ( $dir === '' || file_exists( $dir . wp_basename( $image[0] ) ) ) {
        return $image;
    }
    $url = onionpress_derivative_fallback_url( $attachment_id );
    if ( ! $url || $url === $image[0] ) {
        return $image;
    }
    $meta = wp_get_attachment_metadata( $attachment_id );
    $w    = ! empty( $meta['width'] ) ? (int) $meta['width'] : $image[1];
    $h    = ! empty( $meta['height'] ) ? (int) $meta['height'] : $image[2];
    return array( $url, $w, $h, false );
}, 10, 4 );

// srcset candidates — drop every entry whose file is not on disk yet, so the
// browser never picks a URL that 404s. An empty set means "no srcset".
add_filter( 'wp_calculate_image_srcset', function ( $sources, $size_array, $image_src, $image_meta, $attachment_id ) {
    if ( ! onionpress_derivatives_pending() || ! is_array( $sources ) ) {
        return $sources;
    }
    $dir = onionpress_attachment_dir( $attachment_id );
    if ( $dir === '' ) {
        return $sources;
    }
    foreach ( $sources as $width => $source ) {
        if ( empty( $source['url'] ) ) {
            continue;
        }
        if ( ! file_exists( $dir . wp_basename( $source['url'] ) ) ) {
            unset( $sources[ $width ] );
        }
    }
    return $sources;
}, 10, 5 );

// Images embedded in post content carry a hard-coded sized URL in src=;
// rewrite it to the fallback when that file is missing (WP 6.0+ filter).
add_filter( 'wp_content_img_tag', function ( $filtered_image, $context, $attachment_id ) {
    if ( ! onionpress_derivatives_pending() || ! $attachment_id ) {
        return $filtered_image;
    }
    if ( ! preg_match( '/\ssrc=(["\'])([^"\']+)\1/', $filtered_image, $m ) ) {
        return $filtered_image;
    }
    $src = $m[2];
    if ( ! preg_match( '/-(\d+x\d+|scaled)\.(jpe?g|png|gif|webp|avif)$/i', $src ) ) {
        return $filtered_image;
    }
    $dir = onionpress_attachment_dir( $attachment_id );
    if ( $dir === '' || file_exists( $dir . wp_basename( $src ) ) ) {
        return $filtered_image;
    }
    $url = onionpress_derivative_fallback_url( $attachment_id );
    if ( ! $url || $url === $src ) {
        return $filtered_image;
    }
    return str_replace( $m[0], ' src=' . $m[1] . esc_url( $url ) . $m[1], $filtered_image );
}, 10, 3 );

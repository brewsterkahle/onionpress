<?php
/**
 * Plugin Name: OnionPress Local Avatar
 * Description: Adds a profile photo picker (WP media modal, drag-and-drop) to
 *              the user profile page and serves it locally instead of leaking
 *              email hashes to Gravatar. Hides the misleading core "Profile
 *              Picture / Gravatar" section on profile.php. Users without an
 *              uploaded photo get the OnionPress onion-with-rainbow default
 *              instead of a Gravatar mystery person.
 * Version:     1.2
 * Network:     true
 * License:     AGPL-3.0-or-later
 * License URI: https://www.gnu.org/licenses/agpl-3.0.html
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

/**
 * URL of the default onion-rainbow avatar PNG shipped alongside this plugin.
 * Returned for any user who hasn't uploaded a profile photo.
 */
function onionpress_avatar_default_url() {
    return plugins_url( 'onionpress-avatar-default.png', __FILE__ );
}

/**
 * Map a requested pixel size to the named WP thumbnail that fits best.
 *
 * wp_get_attachment_image_url() with array($w, $h) only matches when WP
 * has a pre-generated size of exactly those dimensions. For avatars we
 * ask for arbitrary sizes like 96 or 200 pixels, which rarely match,
 * and WP silently falls back to the ORIGINAL (which can be several MB).
 *
 * Using named sizes ('thumbnail' = 150x150, 'medium' = ~300) always
 * hits a pre-generated file, so an avatar rendered at 100x100 CSS pixels
 * downloads as ~14 KB instead of ~400 KB.
 */
function onionpress_avatar_named_size( $size ) {
    $s = (int) $size;
    if ( $s <= 150 ) return 'thumbnail';   // 150x150 cropped, ideal for avatar
    if ( $s <= 300 ) return 'medium';      // ~300 wide, aspect-preserved
    if ( $s <= 1024 ) return 'large';
    return 'full';
}

/**
 * Hide the core "Profile Picture" row (which always talks about Gravatar
 * even when overridden). Our own "Profile Photo" section replaces it.
 */
function onionpress_avatar_hide_core_section() {
    $screen = function_exists( 'get_current_screen' ) ? get_current_screen() : null;
    if ( ! $screen || ! in_array( $screen->id, array( 'profile', 'user-edit', 'profile-network', 'user-edit-network' ), true ) ) {
        return;
    }
    ?>
    <style>
        .user-profile-picture { display: none !important; }
    </style>
    <?php
}
add_action( 'admin_head', 'onionpress_avatar_hide_core_section' );

/**
 * Enqueue the WP media library on the profile page so the picker modal works.
 */
function onionpress_avatar_enqueue( $hook ) {
    if ( ! in_array( $hook, array( 'profile.php', 'user-edit.php' ), true ) ) {
        return;
    }
    wp_enqueue_media();
}
add_action( 'admin_enqueue_scripts', 'onionpress_avatar_enqueue' );

/**
 * Show the "Profile Photo" picker on the user profile page.
 */
function onionpress_avatar_field( $user ) {
    if ( ! current_user_can( 'upload_files' ) ) {
        return;
    }
    $avatar_id  = (int) get_user_meta( $user->ID, 'onionpress_avatar_id', true );
    $avatar_url = $avatar_id ? wp_get_attachment_image_url( $avatar_id, 'thumbnail' ) : '';
    ?>
    <h3>Profile Photo</h3>
    <table class="form-table">
        <tr>
            <th><label>Photo</label></th>
            <td>
                <div id="onionpress-avatar-preview" style="margin-bottom:10px;">
                    <?php if ( $avatar_url ) : ?>
                        <img src="<?php echo esc_url( $avatar_url ); ?>"
                             style="width:96px;height:96px;border-radius:50%;object-fit:cover;display:block;">
                    <?php else : ?>
                        <img src="<?php echo esc_url( onionpress_avatar_default_url() ); ?>"
                             style="width:96px;height:96px;border-radius:50%;object-fit:cover;display:block;">
                    <?php endif; ?>
                </div>
                <input type="hidden" name="onionpress_avatar_id" id="onionpress-avatar-id"
                       value="<?php echo (int) $avatar_id; ?>">
                <button type="button" class="button" id="onionpress-avatar-pick">
                    <?php echo $avatar_id ? 'Change photo' : 'Choose or drop photo'; ?>
                </button>
                <?php if ( $avatar_id ) : ?>
                    <button type="button" class="button-link-delete" id="onionpress-avatar-remove" style="margin-left:10px;">
                        Remove
                    </button>
                <?php endif; ?>
                <p class="description">
                    Click to open the media library, or drag an image into the modal that opens.
                    Stored locally on your onion — never sent to Gravatar.
                </p>
            </td>
        </tr>
    </table>
    <script>
    (function($){
        $(function(){
            var frame;
            $('#onionpress-avatar-pick').on('click', function(e){
                e.preventDefault();
                if (frame) { frame.open(); return; }
                frame = wp.media({
                    title: 'Choose profile photo',
                    button: { text: 'Use this photo' },
                    library: { type: 'image' },
                    multiple: false
                });
                frame.on('select', function(){
                    var att = frame.state().get('selection').first().toJSON();
                    $('#onionpress-avatar-id').val(att.id);
                    var url = (att.sizes && att.sizes.thumbnail) ? att.sizes.thumbnail.url : att.url;
                    $('#onionpress-avatar-preview').html(
                        '<img src="' + url + '" style="width:96px;height:96px;border-radius:50%;object-fit:cover;display:block;">'
                    );
                    $('#onionpress-avatar-pick').text('Change photo');
                });
                frame.open();
            });
            $('#onionpress-avatar-remove').on('click', function(e){
                e.preventDefault();
                $('#onionpress-avatar-id').val('0');
                $('#onionpress-avatar-preview').html(
                    '<img src="<?php echo esc_js( esc_url( onionpress_avatar_default_url() ) ); ?>" style="width:96px;height:96px;border-radius:50%;object-fit:cover;display:block;">'
                );
                $(this).remove();
                $('#onionpress-avatar-pick').text('Choose or drop photo');
            });
        });
    })(jQuery);
    </script>
    <?php
}
add_action( 'show_user_profile', 'onionpress_avatar_field' );
add_action( 'edit_user_profile', 'onionpress_avatar_field' );

/**
 * Save the selected profile photo (media-library attachment id).
 */
function onionpress_avatar_save( $user_id ) {
    if ( ! current_user_can( 'edit_user', $user_id ) ) {
        return;
    }
    if ( ! isset( $_POST['onionpress_avatar_id'] ) ) {
        return;
    }

    $new_id = (int) $_POST['onionpress_avatar_id'];
    $old_id = (int) get_user_meta( $user_id, 'onionpress_avatar_id', true );

    if ( $new_id <= 0 ) {
        if ( $old_id ) {
            delete_user_meta( $user_id, 'onionpress_avatar_id' );
        }
        return;
    }

    // Validate the attachment exists and is an image.
    $mime = get_post_mime_type( $new_id );
    if ( ! $mime || strpos( $mime, 'image/' ) !== 0 ) {
        return;
    }

    if ( $new_id !== $old_id ) {
        update_user_meta( $user_id, 'onionpress_avatar_id', $new_id );
    }
}
add_action( 'personal_options_update', 'onionpress_avatar_save' );
add_action( 'edit_user_profile_update', 'onionpress_avatar_save' );

/**
 * Override WordPress avatars with the local upload.
 * Falls back to the default if no local avatar is set.
 */
function onionpress_avatar_override( $avatar, $id_or_email, $size, $default, $alt, $args ) {
    $user = false;

    if ( is_numeric( $id_or_email ) ) {
        $user = get_user_by( 'id', (int) $id_or_email );
    } elseif ( is_string( $id_or_email ) ) {
        $user = get_user_by( 'email', $id_or_email );
    } elseif ( $id_or_email instanceof WP_User ) {
        $user = $id_or_email;
    } elseif ( $id_or_email instanceof WP_Comment ) {
        $user = get_user_by( 'id', $id_or_email->user_id );
    }

    if ( ! $user ) {
        return $avatar;
    }

    $avatar_id = get_user_meta( $user->ID, 'onionpress_avatar_id', true );
    $url = '';
    if ( $avatar_id ) {
        $url = wp_get_attachment_image_url( $avatar_id, onionpress_avatar_named_size( $size ) );
    }
    if ( ! $url ) {
        $url = onionpress_avatar_default_url();
    }

    return sprintf(
        '<img alt="%s" src="%s" class="avatar avatar-%d photo" height="%d" width="%d" loading="lazy">',
        esc_attr( $alt ),
        esc_url( $url ),
        (int) $size,
        (int) $size,
        (int) $size
    );
}
add_filter( 'get_avatar', 'onionpress_avatar_override', 10, 6 );

/**
 * Override avatar URL too (used by the theme's onionpress_get_avatar_url).
 */
function onionpress_avatar_url_override( $url, $id_or_email, $args ) {
    $user = false;

    if ( is_numeric( $id_or_email ) ) {
        $user = get_user_by( 'id', (int) $id_or_email );
    } elseif ( is_string( $id_or_email ) ) {
        $user = get_user_by( 'email', $id_or_email );
    } elseif ( $id_or_email instanceof WP_User ) {
        $user = $id_or_email;
    } elseif ( $id_or_email instanceof WP_Comment ) {
        $user = get_user_by( 'id', $id_or_email->user_id );
    }

    if ( ! $user ) {
        return $url;
    }

    $avatar_id = get_user_meta( $user->ID, 'onionpress_avatar_id', true );
    if ( $avatar_id ) {
        $size = isset( $args['size'] ) ? (int) $args['size'] : 96;
        $local_url = wp_get_attachment_image_url( $avatar_id, onionpress_avatar_named_size( $size ) );
        if ( $local_url ) {
            return $local_url;
        }
    }
    return onionpress_avatar_default_url();
}
add_filter( 'get_avatar_url', 'onionpress_avatar_url_override', 10, 3 );

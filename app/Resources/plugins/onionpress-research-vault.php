<?php
/**
 * Plugin Name: OnionPress Member Vault
 * Description: Member-only catalog with an approval workflow. Gates vault
 *              records and Creations file downloads behind validated accounts.
 * Version:     1.0.0
 * Network:     true
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

/** Capability granted to validated members (and admins). */
define( 'ONIONPRESS_RESEARCH_CAP', 'onionpress_view_research' );

/** User-meta key for application workflow status. */
define( 'ONIONPRESS_RESEARCH_STATUS_META', 'onionpress_research_status' );

/** Site option — vault off by default so existing installs are unchanged. */
define( 'ONIONPRESS_RESEARCH_VAULT_OPTION', 'onionpress_research_vault_enabled' );

/**
 * Asset types for the member catalog.
 */
function onionpress_research_asset_types() {
    return array(
        'document'   => 'Document',
        'dataset'    => 'Dataset',
        'design'     => 'Design / CAD',
        'method'     => 'Method / Procedure',
        'media'      => 'Media / Recording',
        'other'      => 'Other',
    );
}

function onionpress_research_release_statuses() {
    return array(
        'draft'      => 'Draft (not in catalog)',
        'review'     => 'Under review',
        'released'   => 'Released to members',
        'deprecated' => 'Deprecated',
    );
}

function onionpress_research_vault_is_enabled() {
    return (bool) get_site_option( ONIONPRESS_RESEARCH_VAULT_OPTION, false );
}

function onionpress_research_user_can_view() {
    return current_user_can( ONIONPRESS_RESEARCH_CAP );
}

// ── Role & capability ───────────────────────────────────────────────

add_action( 'init', 'onionpress_research_register_role', 5 );
function onionpress_research_register_role() {
    if ( get_role( 'validated_researcher' ) ) {
        return;
    }
    add_role(
        'validated_researcher',
        'Validated Member',
        array(
            'read'                  => true,
            ONIONPRESS_RESEARCH_CAP => true,
            'read_research_asset'   => true,
        )
    );
}

/**
 * Caps WordPress derives from capability_type — grant to admins once.
 */
function onionpress_research_admin_caps() {
    return array(
        ONIONPRESS_RESEARCH_CAP,
        'edit_research_asset',
        'read_research_asset',
        'delete_research_asset',
        'edit_research_assets',
        'edit_others_research_assets',
        'publish_research_assets',
        'read_private_research_assets',
        'delete_research_assets',
        'delete_private_research_assets',
        'delete_published_research_assets',
        'delete_others_research_assets',
        'edit_private_research_assets',
        'edit_published_research_assets',
    );
}

add_action( 'admin_init', 'onionpress_research_ensure_admin_cap' );
function onionpress_research_ensure_admin_cap() {
    $admin = get_role( 'administrator' );
    if ( ! $admin ) {
        return;
    }
    foreach ( onionpress_research_admin_caps() as $cap ) {
        if ( ! $admin->has_cap( $cap ) ) {
            $admin->add_cap( $cap );
        }
    }
}

// ── Custom post type: research_asset ────────────────────────────────

add_action( 'init', 'onionpress_research_register_cpt' );
function onionpress_research_register_cpt() {
    register_post_type( 'research_asset', array(
        'labels' => array(
            'name'          => 'Vault Items',
            'singular_name' => 'Vault Item',
            'add_new_item'  => 'Add Vault Item',
            'edit_item'     => 'Edit Vault Item',
            'view_item'     => 'View Vault Item',
            'search_items'  => 'Search Vault Items',
        ),
        'public'              => true,
        'publicly_queryable'  => true,
        'show_ui'             => true,
        'show_in_menu'        => 'onionpress-research-vault',
        'show_in_rest'        => false,
        'has_archive'         => true,
        'rewrite'             => array( 'slug' => 'research' ),
        'supports'            => array( 'title', 'editor', 'author', 'revisions' ),
        'capability_type'     => array( 'research_asset', 'research_assets' ),
        'map_meta_cap'        => true,
    ) );
}

// ── Meta fields ─────────────────────────────────────────────────────

function onionpress_research_meta_keys() {
    return array(
        'asset_id'         => 'Asset ID',
        'version'          => 'Version',
        'asset_type'       => 'Type',
        'release_status'   => 'Release status',
        'organism'         => 'Subject',
        'technique'        => 'Category',
        'confidentiality'  => 'Confidentiality',
        'file_path'        => 'Creations file path',
        'checksum_sha256'  => 'SHA-256 checksum',
        'pi'               => 'Contact',
        'release_date'     => 'Release date',
    );
}

add_action( 'add_meta_boxes', 'onionpress_research_meta_boxes' );
function onionpress_research_meta_boxes() {
    add_meta_box(
        'onionpress-research-fields',
        'Vault Item Metadata',
        'onionpress_research_render_meta_box',
        'research_asset',
        'normal',
        'high'
    );
}

function onionpress_research_render_meta_box( $post ) {
    wp_nonce_field( 'onionpress_research_save', 'onionpress_research_nonce' );
    $types    = onionpress_research_asset_types();
    $statuses = onionpress_research_release_statuses();
    echo '<table class="form-table"><tbody>';
    foreach ( onionpress_research_meta_keys() as $key => $label ) {
        $meta_key = '_research_' . $key;
        $value    = get_post_meta( $post->ID, $meta_key, true );
        echo '<tr><th><label for="' . esc_attr( $meta_key ) . '">' . esc_html( $label ) . '</label></th><td>';
        if ( $key === 'asset_type' ) {
            echo '<select name="' . esc_attr( $meta_key ) . '" id="' . esc_attr( $meta_key ) . '">';
            echo '<option value="">—</option>';
            foreach ( $types as $slug => $name ) {
                printf(
                    '<option value="%s" %s>%s</option>',
                    esc_attr( $slug ),
                    selected( $value, $slug, false ),
                    esc_html( $name )
                );
            }
            echo '</select>';
        } elseif ( $key === 'release_status' ) {
            echo '<select name="' . esc_attr( $meta_key ) . '" id="' . esc_attr( $meta_key ) . '">';
            foreach ( $statuses as $slug => $name ) {
                printf(
                    '<option value="%s" %s>%s</option>',
                    esc_attr( $slug ),
                    selected( $value, $slug, false ),
                    esc_html( $name )
                );
            }
            echo '</select>';
        } elseif ( $key === 'confidentiality' ) {
            $tiers = array( 'internal', 'shared', 'restricted' );
            echo '<select name="' . esc_attr( $meta_key ) . '" id="' . esc_attr( $meta_key ) . '">';
            foreach ( $tiers as $tier ) {
                printf(
                    '<option value="%s" %s>%s</option>',
                    esc_attr( $tier ),
                    selected( $value, $tier, false ),
                    esc_html( ucfirst( $tier ) )
                );
            }
            echo '</select>';
        } elseif ( $key === 'file_path' ) {
            printf(
                '<input type="text" class="large-text" name="%1$s" id="%1$s" value="%2$s" placeholder="vault-files/item-001/document-v2.pdf">',
                esc_attr( $meta_key ),
                esc_attr( $value )
            );
            echo '<p class="description">Relative path under <code>~/OnionPress/Creations/My Creations/</code></p>';
        } else {
            printf(
                '<input type="text" class="regular-text" name="%1$s" id="%1$s" value="%2$s">',
                esc_attr( $meta_key ),
                esc_attr( $value )
            );
        }
        echo '</td></tr>';
    }
    echo '</tbody></table>';
}

add_action( 'save_post_research_asset', 'onionpress_research_save_meta' );
function onionpress_research_save_meta( $post_id ) {
    if ( ! isset( $_POST['onionpress_research_nonce'] )
      || ! wp_verify_nonce( $_POST['onionpress_research_nonce'], 'onionpress_research_save' ) ) {
        return;
    }
    if ( defined( 'DOING_AUTOSAVE' ) && DOING_AUTOSAVE ) {
        return;
    }
    if ( ! current_user_can( 'edit_post', $post_id ) ) {
        return;
    }
    foreach ( array_keys( onionpress_research_meta_keys() ) as $key ) {
        $meta_key = '_research_' . $key;
        if ( ! isset( $_POST[ $meta_key ] ) ) {
            continue;
        }
        $raw = wp_unslash( $_POST[ $meta_key ] );
        if ( $key === 'file_path' ) {
            $val = sanitize_text_field( $raw );
            $val = ltrim( str_replace( '\\', '/', $val ), '/' );
        } else {
            $val = sanitize_text_field( $raw );
        }
        update_post_meta( $post_id, $meta_key, $val );
    }
}

// ── Admin: vault settings & member approvals ────────────────────────

add_action( 'admin_menu', 'onionpress_research_admin_menu' );
function onionpress_research_admin_menu() {
    add_menu_page(
        'Member Vault',
        'Member Vault',
        'manage_options',
        'onionpress-research-vault',
        'onionpress_research_settings_page',
        'dashicons-shield',
        58
    );
    add_submenu_page(
        'onionpress-research-vault',
        'Vault Settings',
        'Settings',
        'manage_options',
        'onionpress-research-vault',
        'onionpress_research_settings_page'
    );
    add_submenu_page(
        'onionpress-research-vault',
        'Member Applications',
        'Applications',
        'manage_options',
        'onionpress-research-applications',
        'onionpress_research_applications_page'
    );
}

function onionpress_research_settings_page() {
    if ( ! current_user_can( 'manage_options' ) ) {
        return;
    }
    if ( isset( $_POST['onionpress_research_save_settings'] )
      && check_admin_referer( 'onionpress_research_settings' ) ) {
        $enabled = ! empty( $_POST['vault_enabled'] );
        update_site_option( ONIONPRESS_RESEARCH_VAULT_OPTION, $enabled );
        echo '<div class="notice notice-success"><p>Settings saved.</p></div>';
    }
    $enabled = onionpress_research_vault_is_enabled();
    ?>
    <div class="wrap">
        <h1>Member Vault Settings</h1>
        <p>Gate vault records and Creations file downloads behind validated member accounts.</p>
        <form method="post">
            <?php wp_nonce_field( 'onionpress_research_settings' ); ?>
            <table class="form-table">
                <tr>
                    <th>Enable Member Vault</th>
                    <td>
                        <label>
                            <input type="checkbox" name="vault_enabled" value="1" <?php checked( $enabled ); ?>>
                            Require validated-member login for vault items and Creations downloads
                        </label>
                    </td>
                </tr>
            </table>
            <?php if ( $enabled ) : ?>
            <div class="notice notice-warning inline">
                <p><strong>Privacy checklist</strong> (recommended when sharing restricted materials):</p>
                <ul style="list-style:disc;margin-left:1.5em;">
                    <li>Set <code>REGISTER_WITH_ONIONHEAVEN=no</code> in <code>~/.onionpress/config</code></li>
                    <li>Set <code>INSTALL_IA_PLUGIN=no</code> and disable the Wayback archiver plugin</li>
                    <li>Leave <code>CLOUDFLARE_TUNNEL_TOKEN</code> empty</li>
                    <li>Store vault files under <code>~/OnionPress/Creations/My Creations/vault-files/</code></li>
                </ul>
            </div>
            <?php endif; ?>
            <p class="submit">
                <button type="submit" name="onionpress_research_save_settings" class="button button-primary">Save</button>
            </p>
        </form>
        <h2>Front-end pages</h2>
        <ul>
            <li>Catalog: <a href="<?php echo esc_url( home_url( '/research-catalog/' ) ); ?>"><?php echo esc_html( home_url( '/research-catalog/' ) ); ?></a></li>
            <li>Apply: <a href="<?php echo esc_url( home_url( '/research-apply/' ) ); ?>"><?php echo esc_html( home_url( '/research-apply/' ) ); ?></a></li>
        </ul>
    </div>
    <?php
}

function onionpress_research_applications_page() {
    if ( ! current_user_can( 'manage_options' ) ) {
        return;
    }

    if ( isset( $_GET['action'], $_GET['user_id'], $_GET['_wpnonce'] ) ) {
        $user_id = (int) $_GET['user_id'];
        $action  = sanitize_key( $_GET['action'] );
        if ( wp_verify_nonce( $_GET['_wpnonce'], 'onionpress_research_' . $action . '_' . $user_id ) ) {
            if ( $action === 'approve' ) {
                $user = get_userdata( $user_id );
                if ( $user ) {
                    $user->add_role( 'validated_researcher' );
                    update_user_meta( $user_id, ONIONPRESS_RESEARCH_STATUS_META, 'approved' );
                    update_user_meta( $user_id, 'onionpress_research_approved_at', time() );
                }
            } elseif ( $action === 'reject' ) {
                $user = get_userdata( $user_id );
                if ( $user ) {
                    $user->remove_role( 'validated_researcher' );
                    update_user_meta( $user_id, ONIONPRESS_RESEARCH_STATUS_META, 'rejected' );
                }
            }
        }
    }

    $pending = get_users( array(
        'meta_key'   => ONIONPRESS_RESEARCH_STATUS_META,
        'meta_value' => 'pending',
        'number'     => 100,
    ) );
    ?>
    <div class="wrap">
        <h1>Member Applications</h1>
        <?php if ( empty( $pending ) ) : ?>
            <p>No pending applications.</p>
        <?php else : ?>
            <table class="widefat striped">
                <thead>
                    <tr>
                        <th>User</th>
                        <th>Institution</th>
                        <th>External ID</th>
                        <th>Area of interest</th>
                        <th>Submitted</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody>
                <?php foreach ( $pending as $user ) : ?>
                    <tr>
                        <td>
                            <strong><?php echo esc_html( $user->display_name ); ?></strong><br>
                            <code><?php echo esc_html( $user->user_login ); ?></code>
                        </td>
                        <td><?php echo esc_html( get_user_meta( $user->ID, 'onionpress_research_institution', true ) ); ?></td>
                        <td><?php echo esc_html( get_user_meta( $user->ID, 'onionpress_research_orcid', true ) ); ?></td>
                        <td><?php echo esc_html( get_user_meta( $user->ID, 'onionpress_research_area', true ) ); ?></td>
                        <td><?php echo esc_html( get_user_meta( $user->ID, 'onionpress_research_applied_at', true ) ); ?></td>
                        <td>
                            <?php
                            $approve_url = wp_nonce_url(
                                admin_url( 'admin.php?page=onionpress-research-applications&action=approve&user_id=' . $user->ID ),
                                'onionpress_research_approve_' . $user->ID
                            );
                            $reject_url = wp_nonce_url(
                                admin_url( 'admin.php?page=onionpress-research-applications&action=reject&user_id=' . $user->ID ),
                                'onionpress_research_reject_' . $user->ID
                            );
                            ?>
                            <a class="button button-primary" href="<?php echo esc_url( $approve_url ); ?>">Approve</a>
                            <a class="button" href="<?php echo esc_url( $reject_url ); ?>">Reject</a>
                        </td>
                    </tr>
                <?php endforeach; ?>
                </tbody>
            </table>
        <?php endif; ?>
    </div>
    <?php
}

add_filter( 'manage_research_asset_posts_columns', 'onionpress_research_admin_columns' );
function onionpress_research_admin_columns( $cols ) {
    $new = array();
    foreach ( $cols as $k => $v ) {
        $new[ $k ] = $v;
        if ( $k === 'title' ) {
            $new['asset_id'] = 'Asset ID';
            $new['asset_type'] = 'Type';
            $new['release_status'] = 'Status';
        }
    }
    return $new;
}

add_action( 'manage_research_asset_posts_custom_column', 'onionpress_research_admin_column_data', 10, 2 );
function onionpress_research_admin_column_data( $col, $post_id ) {
    if ( $col === 'asset_id' ) {
        echo esc_html( get_post_meta( $post_id, '_research_asset_id', true ) );
    } elseif ( $col === 'asset_type' ) {
        $type = get_post_meta( $post_id, '_research_asset_type', true );
        $types = onionpress_research_asset_types();
        echo esc_html( isset( $types[ $type ] ) ? $types[ $type ] : $type );
    } elseif ( $col === 'release_status' ) {
        echo esc_html( get_post_meta( $post_id, '_research_release_status', true ) );
    }
}

// ── Access control ──────────────────────────────────────────────────

add_filter( 'onionpress_creations_allow_download', 'onionpress_research_gate_creations', 10, 2 );
function onionpress_research_gate_creations( $allowed, $filepath ) {
    if ( ! onionpress_research_vault_is_enabled() ) {
        return $allowed;
    }
    return onionpress_research_user_can_view();
}

add_action( 'template_redirect', 'onionpress_research_template_gate' );
function onionpress_research_template_gate() {
    if ( ! onionpress_research_vault_is_enabled() ) {
        return;
    }
    if ( onionpress_research_user_can_view() ) {
        return;
    }

    $gated = is_singular( 'research_asset' )
          || is_post_type_archive( 'research_asset' )
          || is_page( 'research-catalog' )
          || is_page( 'my-creations' );

    if ( ! $gated ) {
        return;
    }

    if ( ! is_user_logged_in() ) {
        auth_redirect();
    }

    // Logged in but not validated — send to apply page.
    wp_safe_redirect( home_url( '/research-apply/' ) );
    exit;
}

add_action( 'pre_get_posts', 'onionpress_research_filter_queries' );
function onionpress_research_filter_queries( $query ) {
    if ( is_admin() || ! $query->is_main_query() ) {
        return;
    }
    if ( ! onionpress_research_vault_is_enabled() ) {
        return;
    }
    if ( $query->is_post_type_archive( 'research_asset' ) || $query->get( 'post_type' ) === 'research_asset' ) {
        if ( ! onionpress_research_user_can_view() ) {
            $query->set( 'post__in', array( 0 ) );
            return;
        }
        // Members only see released assets on the public archive.
        if ( ! current_user_can( 'edit_research_assets' ) ) {
            $query->set( 'meta_query', array(
                array(
                    'key'     => '_research_release_status',
                    'value'   => 'released',
                    'compare' => '=',
                ),
            ) );
        }
    }
}

add_filter( 'the_content', 'onionpress_research_filter_content' );
function onionpress_research_filter_content( $content ) {
    if ( ! onionpress_research_vault_is_enabled() ) {
        return $content;
    }
    if ( ! is_singular( 'research_asset' ) ) {
        return $content;
    }
    if ( onionpress_research_user_can_view() ) {
        $post_id   = get_the_ID();
        $file_path = get_post_meta( $post_id, '_research_file_path', true );
        $checksum  = get_post_meta( $post_id, '_research_checksum_sha256', true );
        $extra     = onionpress_research_render_asset_meta_html( $post_id );
        if ( $file_path ) {
            $url = add_query_arg( 'onionpress_creation', rawurlencode( $file_path ), home_url( '/' ) );
            $extra .= '<p><a class="button" href="' . esc_url( $url ) . '">Download attached file</a></p>';
            if ( $checksum ) {
                $extra .= '<p><small>SHA-256: <code>' . esc_html( $checksum ) . '</code></small></p>';
            }
        }
        return $extra . $content;
    }
    return '<p>This item is available only to validated members. <a href="' . esc_url( home_url( '/research-apply/' ) ) . '">Apply for access</a>.</p>';
}

function onionpress_research_render_asset_meta_html( $post_id ) {
    $types    = onionpress_research_asset_types();
    $asset_id = get_post_meta( $post_id, '_research_asset_id', true );
    $version  = get_post_meta( $post_id, '_research_version', true );
    $type     = get_post_meta( $post_id, '_research_asset_type', true );
    $organism = get_post_meta( $post_id, '_research_organism', true );
    $technique = get_post_meta( $post_id, '_research_technique', true );
    $pi       = get_post_meta( $post_id, '_research_pi', true );

    $html = '<div class="research-asset-meta" style="background:#f6f6f6;padding:1em;margin-bottom:1.5em;border-radius:4px;">';
    $html .= '<dl style="display:grid;grid-template-columns:auto 1fr;gap:0.25em 1em;margin:0;">';
    if ( $asset_id ) {
        $html .= '<dt>Asset ID</dt><dd>' . esc_html( $asset_id ) . '</dd>';
    }
    if ( $version ) {
        $html .= '<dt>Version</dt><dd>' . esc_html( $version ) . '</dd>';
    }
    if ( $type && isset( $types[ $type ] ) ) {
        $html .= '<dt>Type</dt><dd>' . esc_html( $types[ $type ] ) . '</dd>';
    }
    if ( $organism ) {
        $html .= '<dt>Subject</dt><dd>' . esc_html( $organism ) . '</dd>';
    }
    if ( $technique ) {
        $html .= '<dt>Category</dt><dd>' . esc_html( $technique ) . '</dd>';
    }
    if ( $pi ) {
        $html .= '<dt>Contact</dt><dd>' . esc_html( $pi ) . '</dd>';
    }
    $html .= '</dl></div>';
    return $html;
}

// ── Shortcodes: catalog & application form ──────────────────────────

add_shortcode( 'onionpress_research_catalog', 'onionpress_research_catalog_shortcode' );
function onionpress_research_catalog_shortcode() {
    if ( ! onionpress_research_vault_is_enabled() ) {
        return '<p>Member Vault is not enabled.</p>';
    }
    if ( ! onionpress_research_user_can_view() ) {
        if ( ! is_user_logged_in() ) {
            return '<p>Please <a href="' . esc_url( wp_login_url( get_permalink() ) ) . '">log in</a> to view the member catalog.</p>';
        }
        return '<p>Your account is not yet validated. <a href="' . esc_url( home_url( '/research-apply/' ) ) . '">Apply for member access</a>.</p>';
    }

    $assets = get_posts( array(
        'post_type'      => 'research_asset',
        'post_status'    => 'publish',
        'posts_per_page' => 100,
        'meta_query'     => array(
            array(
                'key'     => '_research_release_status',
                'value'   => 'released',
                'compare' => '=',
            ),
        ),
        'orderby'        => 'date',
        'order'          => 'DESC',
    ) );

    if ( empty( $assets ) ) {
        return '<p>No released vault items yet.</p>';
    }

    $types = onionpress_research_asset_types();
    $html  = '<table class="research-catalog" style="width:100%;border-collapse:collapse;">';
    $html .= '<thead><tr>';
    foreach ( array( 'Asset ID', 'Title', 'Type', 'Version', 'Released' ) as $h ) {
        $html .= '<th style="text-align:left;border-bottom:2px solid #ccc;padding:0.5em;">' . esc_html( $h ) . '</th>';
    }
    $html .= '</tr></thead><tbody>';

    foreach ( $assets as $asset ) {
        $asset_id = get_post_meta( $asset->ID, '_research_asset_id', true );
        $type     = get_post_meta( $asset->ID, '_research_asset_type', true );
        $version  = get_post_meta( $asset->ID, '_research_version', true );
        $date     = get_post_meta( $asset->ID, '_research_release_date', true );
        $html    .= '<tr>';
        $html    .= '<td style="padding:0.5em;border-bottom:1px solid #eee;">' . esc_html( $asset_id ) . '</td>';
        $html    .= '<td style="padding:0.5em;border-bottom:1px solid #eee;"><a href="' . esc_url( get_permalink( $asset ) ) . '">' . esc_html( get_the_title( $asset ) ) . '</a></td>';
        $html    .= '<td style="padding:0.5em;border-bottom:1px solid #eee;">' . esc_html( isset( $types[ $type ] ) ? $types[ $type ] : $type ) . '</td>';
        $html    .= '<td style="padding:0.5em;border-bottom:1px solid #eee;">' . esc_html( $version ) . '</td>';
        $html    .= '<td style="padding:0.5em;border-bottom:1px solid #eee;">' . esc_html( $date ) . '</td>';
        $html    .= '</tr>';
    }
    $html .= '</tbody></table>';
    return $html;
}

add_shortcode( 'onionpress_research_apply', 'onionpress_research_apply_shortcode' );
function onionpress_research_apply_shortcode() {
    if ( ! onionpress_research_vault_is_enabled() ) {
        return '<p>Member Vault is not enabled.</p>';
    }

    if ( ! is_user_logged_in() ) {
        return '<p>You need an account before applying. <a href="' . esc_url( wp_login_url( get_permalink() ) ) . '">Log in</a> or contact the site administrator for an account.</p>';
    }

    $user_id = get_current_user_id();
    $status  = get_user_meta( $user_id, ONIONPRESS_RESEARCH_STATUS_META, true );

    if ( onionpress_research_user_can_view() ) {
        return '<p>You are a validated member. <a href="' . esc_url( home_url( '/research-catalog/' ) ) . '">View the member catalog</a>.</p>';
    }
    if ( $status === 'pending' ) {
        return '<p>Your application is under review. You will be notified when an administrator approves your access.</p>';
    }
    if ( $status === 'rejected' ) {
        return '<p>Your previous application was not approved. Contact the site administrator if you believe this was in error.</p>';
    }

    $message = '';
    if ( isset( $_POST['onionpress_research_apply_submit'] )
      && isset( $_POST['onionpress_research_apply_nonce'] )
      && wp_verify_nonce( $_POST['onionpress_research_apply_nonce'], 'onionpress_research_apply' ) ) {

        $institution = sanitize_text_field( wp_unslash( $_POST['institution'] ?? '' ) );
        $orcid       = sanitize_text_field( wp_unslash( $_POST['orcid'] ?? '' ) );
        $area        = sanitize_text_field( wp_unslash( $_POST['research_area'] ?? '' ) );
        $reason      = sanitize_textarea_field( wp_unslash( $_POST['reason'] ?? '' ) );
        $agree       = ! empty( $_POST['agree_terms'] );

        if ( ! $institution || ! $area || ! $reason || ! $agree ) {
            $message = '<p style="color:#b00;">Please complete all required fields and accept the access terms.</p>';
        } else {
            update_user_meta( $user_id, 'onionpress_research_institution', $institution );
            update_user_meta( $user_id, 'onionpress_research_orcid', $orcid );
            update_user_meta( $user_id, 'onionpress_research_area', $area );
            update_user_meta( $user_id, 'onionpress_research_reason', $reason );
            update_user_meta( $user_id, ONIONPRESS_RESEARCH_STATUS_META, 'pending' );
            update_user_meta( $user_id, 'onionpress_research_applied_at', gmdate( 'Y-m-d H:i:s' ) );
            return '<p>Application submitted. An administrator will review your request.</p>';
        }
    }

    ob_start();
    echo $message;
    ?>
    <form method="post" class="onionpress-research-apply">
        <?php wp_nonce_field( 'onionpress_research_apply', 'onionpress_research_apply_nonce' ); ?>
        <p>
            <label for="institution"><strong>Institution / affiliation</strong> (required)</label><br>
            <input type="text" name="institution" id="institution" class="regular-text" required style="width:100%;max-width:32em;">
        </p>
        <p>
            <label for="orcid"><strong>External ID</strong> (optional)</label><br>
            <input type="text" name="orcid" id="orcid" placeholder="e.g. profile or registry ID" style="width:100%;max-width:20em;">
        </p>
        <p>
            <label for="research_area"><strong>Area of interest</strong> (required)</label><br>
            <input type="text" name="research_area" id="research_area" required style="width:100%;max-width:32em;">
        </p>
        <p>
            <label for="reason"><strong>Why do you need access?</strong> (required)</label><br>
            <textarea name="reason" id="reason" rows="4" required style="width:100%;max-width:40em;"></textarea>
        </p>
        <p>
            <label>
                <input type="checkbox" name="agree_terms" value="1" required>
                I agree to use shared materials only for approved purposes and not to redistribute without permission.
            </label>
        </p>
        <p><button type="submit" name="onionpress_research_apply_submit" class="button">Submit application</button></p>
    </form>
    <?php
    return ob_get_clean();
}

// ── Bootstrap pages (mu-plugins have no activation hook) ──────────────

add_action( 'init', 'onionpress_research_maybe_create_pages', 20 );
function onionpress_research_maybe_create_pages() {
    if ( get_site_option( 'onionpress_research_pages_created' ) ) {
        return;
    }
    if ( ! function_exists( 'wp_insert_post' ) ) {
        return;
    }

    $catalog_id = wp_insert_post( array(
        'post_title'   => 'Member Catalog',
        'post_name'    => 'research-catalog',
        'post_status'  => 'publish',
        'post_type'    => 'page',
        'post_content' => '[onionpress_research_catalog]',
    ), true );

    $apply_id = wp_insert_post( array(
        'post_title'   => 'Member Access',
        'post_name'    => 'research-apply',
        'post_status'  => 'publish',
        'post_type'    => 'page',
        'post_content' => '[onionpress_research_apply]',
    ), true );

    if ( ! is_wp_error( $catalog_id ) && ! is_wp_error( $apply_id ) ) {
        update_site_option( 'onionpress_research_pages_created', 1 );
    }
}

<?php
/**
 * Plugin Name: OnionPress Onboarding
 * Description: First-run setup wizard for new OnionPress installs. Replaces the
 *              Mac-only AppKit setup window so Linux and Pi installs get the same
 *              guided experience. Runs inside WP itself, so it works identically
 *              across every platform.
 * Version:     1.0
 * Network:     true
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

/**
 * Has the user completed (or skipped) onboarding?
 */
function onionpress_onboarding_needed() {
    return ! get_site_option( 'onionpress_onboarded', false );
}

/**
 * Redirect admins to the wizard on first login until they finish or skip it.
 */
add_action( 'admin_init', 'onionpress_onboarding_maybe_redirect' );
function onionpress_onboarding_maybe_redirect() {
    if ( ! is_super_admin() ) {
        return;
    }
    if ( ! onionpress_onboarding_needed() ) {
        return;
    }
    // Allow access to the wizard page itself, AJAX, and logout
    $page = isset( $_GET['page'] ) ? sanitize_key( $_GET['page'] ) : '';
    if ( $page === 'onionpress-onboarding' ) {
        return;
    }
    $request = $_SERVER['REQUEST_URI'] ?? '';
    if ( strpos( $request, 'admin-ajax.php' ) !== false
      || strpos( $request, 'wp-login.php' )    !== false
      || strpos( $request, 'admin-post.php' )  !== false ) {
        return;
    }

    wp_safe_redirect( admin_url( 'admin.php?page=onionpress-onboarding' ) );
    exit;
}

/**
 * Register the wizard as a hidden admin page (no parent → not in menu).
 */
add_action( 'admin_menu', 'onionpress_onboarding_register_page' );
function onionpress_onboarding_register_page() {
    add_submenu_page(
        '',                              // no parent — hidden
        'OnionPress Setup',
        'OnionPress Setup',
        'manage_options',
        'onionpress-onboarding',
        'onionpress_onboarding_render'
    );
}

/**
 * "New Post" in the admin bar, right after the site name (core adds the
 * site-name node at priority 30; "+ New" sits further right at 70).
 *
 * User testing (#193): a first-time user wanted to write a post and could
 * not find where to start — "Edit Site" is not an obvious path and the
 * generic "+ New" dropdown does not say "post". One explicit, always-visible
 * entry fixes that. Shown on the front end and in wp-admin for anyone who
 * can write posts on the current site; `admin_url()` resolves to the
 * current blog, so on a per-user subsite it opens that subsite's editor.
 */
add_action( 'admin_bar_menu', 'onionpress_onboarding_new_post_node', 31 );
function onionpress_onboarding_new_post_node( $wp_admin_bar ) {
    if ( ! is_user_logged_in() || ! current_user_can( 'edit_posts' ) ) {
        return;
    }
    $wp_admin_bar->add_node( [
        'id'    => 'onionpress-new-post',
        'title' => '<span class="ab-icon dashicons dashicons-edit"></span><span class="ab-label">New Post</span>',
        'href'  => admin_url( 'post-new.php' ),
        'meta'  => [ 'title' => 'Write a new post' ],
    ] );
}

/**
 * Read the .onion address from the shared volume.
 */
function onionpress_onboarding_get_onion() {
    $f = '/var/lib/onionpress/onion_address';
    if ( is_readable( $f ) ) {
        return trim( file_get_contents( $f ) );
    }
    return '';
}

/**
 * Mark onboarding complete and bounce to the dashboard.
 */
function onionpress_onboarding_finish() {
    update_site_option( 'onionpress_onboarded', time() );
    wp_safe_redirect( admin_url() );
    exit;
}

/**
 * Wizard controller.
 */
function onionpress_onboarding_render() {
    $step = max( 1, min( 4, (int) ( $_REQUEST['op_step'] ?? 1 ) ) );

    // Skip-everything escape hatch
    if ( isset( $_GET['op_skip'] ) ) {
        onionpress_onboarding_finish();
    }

    // POST handler: save data for the step we just submitted, then advance.
    if ( $_SERVER['REQUEST_METHOD'] === 'POST' ) {
        check_admin_referer( 'onionpress-onboarding' );

        if ( $step === 2 ) {
            $title   = sanitize_text_field( $_POST['site_title']   ?? '' );
            $tagline = sanitize_text_field( $_POST['site_tagline'] ?? '' );
            if ( $title !== '' ) {
                update_option( 'blogname', $title );
            }
            update_option( 'blogdescription', $tagline );
        }
        if ( $step === 3 ) {
            $new     = $_POST['new_password']     ?? '';
            $confirm = $_POST['confirm_password'] ?? '';
            if ( $new !== '' && $new === $confirm ) {
                $uid = get_current_user_id();
                wp_set_password( $new, $uid );
                // wp_set_password kills the session — log them back in immediately.
                wp_set_auth_cookie( $uid, true );
            }
        }

        $next = $step + 1;
        if ( $next > 4 ) {
            onionpress_onboarding_finish();
        }
        wp_safe_redirect( admin_url( 'admin.php?page=onionpress-onboarding&op_step=' . $next ) );
        exit;
    }

    // Render
    ?>
    <style>
        .op-wiz       { max-width:720px; margin:40px auto; background:#fff;
                        padding:40px 50px; border-radius:8px;
                        box-shadow:0 2px 12px rgba(0,0,0,0.06); }
        .op-wiz h1   { margin:0 0 16px; font-size:28px; color:#1a1a2e; }
        .op-wiz p    { font-size:15px; line-height:1.6; color:#374151; }
        .op-steps    { display:flex; gap:8px; margin-bottom:32px; }
        .op-step     { flex:1; text-align:center; padding:10px; border-radius:4px;
                        font-size:13px; background:#e5e7eb; color:#6b7280; }
        .op-step.on  { background:#8b5cf6; color:#fff; font-weight:600; }
        .op-wiz input[type=text],
        .op-wiz input[type=password]
                      { font-size:16px; padding:10px 12px; width:100%;
                        max-width:480px; border:1px solid #d1d5db;
                        border-radius:4px; }
        .op-wiz label{ display:block; margin:18px 0 6px; font-weight:600; }
        .op-wiz .op-onion
                      { background:#1a1a2e; padding:18px 22px; margin:18px 0;
                        border-radius:6px; font-family:Menlo,monospace;
                        font-size:14px; color:#43e97b; word-break:break-all; }
        .op-wiz .op-skip
                      { float:right; font-size:13px; color:#9ca3af;
                        text-decoration:none; }
        .op-wiz .op-skip:hover { color:#6b7280; }
    </style>
    <div class="op-wiz">
        <a class="op-skip" href="<?php echo esc_url( admin_url( 'admin.php?page=onionpress-onboarding&op_skip=1' ) ); ?>">skip setup</a>
        <div class="op-steps">
            <?php
            $names = array( 'Welcome', 'Site', 'Password', 'Done' );
            foreach ( $names as $i => $name ) :
                $n   = $i + 1;
                $cls = ( $n === $step ) ? 'op-step on' : 'op-step';
                ?>
                <div class="<?php echo esc_attr( $cls ); ?>"><?php echo $n; ?>. <?php echo esc_html( $name ); ?></div>
            <?php endforeach; ?>
        </div>

        <?php
        switch ( $step ) {
            case 1: onionpress_onboarding_step_welcome(); break;
            case 2: onionpress_onboarding_step_identity(); break;
            case 3: onionpress_onboarding_step_password(); break;
            case 4: onionpress_onboarding_step_done(); break;
        }
        ?>
    </div>
    <?php
}

function onionpress_onboarding_step_welcome() {
    ?>
    <h1>Welcome to OnionPress.</h1>
    <p>You're running a self-hosted blog on the Tor network. Your site has a permanent <code>.onion</code> address that anyone with Tor Browser can visit, anywhere in the world — no hosting company, no monthly bill, no one can take it down but you.</p>
    <p>This four-step setup takes about a minute.</p>
    <form method="post">
        <?php wp_nonce_field( 'onionpress-onboarding' ); ?>
        <input type="hidden" name="op_step" value="1">
        <p style="margin-top:28px;">
            <button type="submit" class="button button-primary button-hero">Get started &rarr;</button>
        </p>
    </form>
    <?php
}

function onionpress_onboarding_step_identity() {
    $title   = get_option( 'blogname',        'OnionPress' );
    $tagline = get_option( 'blogdescription', '' );
    ?>
    <h1>What should we call your site?</h1>
    <p>This shows up at the top of every page and in the title bar when someone visits in Tor Browser. You can change it later under Settings &rarr; General.</p>
    <form method="post">
        <?php wp_nonce_field( 'onionpress-onboarding' ); ?>
        <input type="hidden" name="op_step" value="2">
        <label for="site_title">Site title</label>
        <input id="site_title" type="text" name="site_title" value="<?php echo esc_attr( $title ); ?>" required>
        <label for="site_tagline">Tagline <span style="color:#9ca3af;font-weight:400;">(optional &mdash; a one-liner about your site)</span></label>
        <input id="site_tagline" type="text" name="site_tagline" value="<?php echo esc_attr( $tagline ); ?>">
        <p style="margin-top:28px;">
            <button type="submit" class="button button-primary button-hero">Continue &rarr;</button>
        </p>
    </form>
    <?php
}

function onionpress_onboarding_step_password() {
    ?>
    <h1>Pick a password.</h1>
    <p>You're logged in with the random password from your install (saved to <code>~/.onionpress/wp-admin-password</code>). Let's swap it for one you'll remember.</p>
    <p style="color:#9ca3af;font-size:13px;">Want to keep the random one? Leave both fields blank.</p>
    <form method="post" autocomplete="off">
        <?php wp_nonce_field( 'onionpress-onboarding' ); ?>
        <input type="hidden" name="op_step" value="3">
        <label for="new_password">New password</label>
        <input id="new_password" type="password" name="new_password" autocomplete="new-password">
        <label for="confirm_password">Confirm</label>
        <input id="confirm_password" type="password" name="confirm_password" autocomplete="new-password">
        <p style="margin-top:28px;">
            <button type="submit" class="button button-primary button-hero">Continue &rarr;</button>
        </p>
    </form>
    <?php
}

function onionpress_onboarding_step_done() {
    $onion = onionpress_onboarding_get_onion();
    ?>
    <h1>You're live.</h1>
    <p>Your site is set up. Here's your public <code>.onion</code> address:</p>
    <?php if ( $onion ) : ?>
        <div class="op-onion"><?php echo esc_html( $onion ); ?></div>
        <p><strong>To test it in Tor Browser:</strong></p>
        <ol style="line-height:1.8;">
            <li>Launch Tor Browser. On Ubuntu/Debian, run <code>torbrowser-launcher</code> in a terminal &mdash; first launch downloads and signature-verifies the official Tor Browser. (If <code>torbrowser-launcher</code> isn't installed: <code>sudo apt install torbrowser-launcher</code>.)</li>
            <li>Copy the <code>.onion</code> address above and paste into the address bar.</li>
            <li>If you get "Onionsite Not Found", wait 5&ndash;10 minutes and try again &mdash; Tor needs time to publish the service descriptor.</li>
        </ol>
        <p style="color:#9ca3af;font-size:13px;">The <code>localhost:8080</code> URL only works on this machine. The <code>.onion</code> address is what you share with everyone else.</p>
    <?php else : ?>
        <p style="color:#a00;">Your <code>.onion</code> address is still being generated. Check the status bar in your dashboard in a moment &mdash; it'll show up there once Tor has published it.</p>
    <?php endif; ?>
    <form method="post" style="margin-top:28px;">
        <?php wp_nonce_field( 'onionpress-onboarding' ); ?>
        <input type="hidden" name="op_step" value="4">
        <button type="submit" class="button button-primary button-hero">Finish &mdash; take me to my dashboard</button>
    </form>
    <?php
}

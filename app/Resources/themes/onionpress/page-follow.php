<?php
/**
 * Template Name: Follow
 *
 * Landing page for the Follow button — explains how to follow this site,
 * shows QR codes for the .onion address and Tor Browser download.
 */

require_once get_template_directory() . '/inc/qrcode.php';

// Get this site's onion address
$onion_addr = '';
if (function_exists('onionpress_follow_get_own_address')) {
    $onion_addr = onionpress_follow_get_own_address();
}
if (!$onion_addr) {
    // From the query string: shown, put in a QR code and linked, so only a
    // whole v3 onion address, never a host of the visitor's choosing.
    $onion_addr = isset($_GET['address']) ? strtolower(trim((string) wp_unslash($_GET['address']))) : '';
    if (!preg_match('/^[a-z2-7]{56}\.onion$/D', $onion_addr)) {
        $onion_addr = '';
    }
}

$site_name = get_bloginfo('name');
// The complete followable identity: this .onion address plus the current
// subsite's onionname. OnionPress is path-based multisite, so the blog path
// IS the onionname (e.g. addr.onion/alice); the root blog has no path.
// Following the complete addr/onionname is what distinguishes subsites that
// share one .onion address.
$follow_id = $onion_addr;
if ($onion_addr) {
    $sub_path = trim((string) parse_url(home_url('/'), PHP_URL_PATH), '/');
    if ($sub_path !== '') {
        $follow_id = $onion_addr . '/' . $sub_path;
    }
}
$onion_url = $follow_id ? 'http://' . $follow_id . '/' : '';
$tor_url = 'https://www.torproject.org/download/';

get_header();
?>

<article class="post follow-page">
    <h1 class="post-title">Follow <?php echo esc_html($site_name); ?></h1>

    <?php if ($onion_addr) : ?>

    <div class="follow-section">
        <h2>Visit this site</h2>
        <p>This site is published as a Tor onion service. To visit it, you need
           <a href="<?php echo esc_url($tor_url); ?>">Tor Browser</a> and this address:</p>

        <div class="follow-address-box">
            <code class="follow-address"><?php echo esc_html($follow_id); ?></code>
        </div>

        <div class="follow-qr-row">
            <div class="follow-qr-card">
                <div class="follow-qr">
                    <?php echo onionpress_qr_svg($onion_url, 180); ?>
                </div>
                <div class="follow-qr-label">This site's address</div>
            </div>

            <div class="follow-qr-card">
                <div class="follow-qr">
                    <?php echo onionpress_qr_svg($tor_url, 180); ?>
                </div>
                <div class="follow-qr-label">Get Tor Browser</div>
            </div>
        </div>
    </div>

    <div class="follow-section">
        <h2>Follow with OnionPress</h2>
        <p>If you run <a href="https://github.com/brewsterkahle/onionpress">OnionPress</a>,
           you can follow this site to see new posts in your feed.</p>

        <div class="follow-steps">
            <div class="follow-step">
                <span class="follow-step-num">1</span>
                <div>
                    <strong>Open OnionPress Settings</strong>
                    <p>Go to your WordPress dashboard and click OnionPress in the sidebar.</p>
                </div>
            </div>
            <div class="follow-step">
                <span class="follow-step-num">2</span>
                <div>
                    <strong>Add this address</strong>
                    <p>Paste the address below into the Follow field and click Follow:</p>
                    <code class="follow-address-inline"><?php echo esc_html($follow_id); ?></code>
                </div>
            </div>
            <div class="follow-step">
                <span class="follow-step-num">3</span>
                <div>
                    <strong>Done</strong>
                    <p>New posts from this site will appear in your OnionPress feed.</p>
                </div>
            </div>
        </div>

        <p class="follow-direct-link">
            Or click directly:
            <a href="onionpress://follow/<?php echo esc_attr($follow_id); ?>">
                + Follow <?php echo esc_html($site_name); ?>
            </a>
            <span class="follow-direct-note">(opens OnionPress if installed)</span>
        </p>
    </div>

    <?php else : ?>
        <p>No onion address available for this site.</p>
    <?php endif; ?>
</article>

<?php get_footer(); ?>

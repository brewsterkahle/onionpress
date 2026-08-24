<?php
/**
 * test-auto-login-redirect.php — regression coverage for the redirect_to
 * sanitize-before-validate ordering in onionpress-auto-login.php.
 *
 * The validation lives inline in the plugin's `init` closure, so — same
 * technique as tests/test-tor-bridge-config.sh — the block is extracted from
 * the shipped source and evaluated here, keeping the test honest against the
 * real code instead of a copy that can drift. The few WordPress functions the
 * block touches are stubbed below; wp_sanitize_redirect() is a faithful
 * minimal port of core's character-allowlist strip, because the whole bug is
 * about what that strip does to a value validated too early.
 *
 * Runs standalone: `php tests/test-auto-login-redirect.php`.
 * Exit status is 0 only if every assertion passes.
 */

$pass = 0;
$fail = 0;
function ok( $label ) {
    global $pass;
    $pass++;
    printf( "PASS  %s\n", $label );
}
function bad( $label, $detail = '' ) {
    global $fail;
    $fail++;
    printf( "FAIL  %s%s\n", $label, $detail !== '' ? " ($detail)" : '' );
}
function assert_eq( $label, $expected, $actual ) {
    if ( $expected === $actual ) {
        ok( $label );
    } else {
        bad( $label, 'expected ' . var_export( $expected, true ) . ', got ' . var_export( $actual, true ) );
    }
}

// --- WordPress stubs ---------------------------------------------------

// Minimal faithful port of core wp_sanitize_redirect(): strip everything
// outside the allowlist, then remove encoded CR/LF. This is the strip that
// collapses "/<tab>/evil.com" into "//evil.com" — the reason validation must
// run on the sanitized value, never the raw one.
function wp_sanitize_redirect( $location ) {
    $location = str_replace( ' ', '%20', $location );
    $location = preg_replace( '|[^a-z0-9-~+_.?#=&;,/:%!*\[\]()@]|i', '', $location );
    return str_ireplace( array( '%0d', '%0a' ), '', $location );
}

// In WordPress $_GET arrives slashed and wp_unslash() undoes that; the test
// feeds the already-decoded raw value directly, so identity is faithful here.
function wp_unslash( $value ) {
    return $value;
}

// The closure's fallback when redirect_to is absent or rejected: the current
// URL minus the token. A sentinel keeps accept/reject outcomes unambiguous.
define( 'OP_FALLBACK', '/fallback-current-url' );
function remove_query_arg( $arg ) {
    return OP_FALLBACK;
}

// --- Extract the validation block from the shipped plugin --------------

$plugin = __DIR__ . '/../app/Resources/plugins/onionpress-auto-login.php';
$src    = file_get_contents( $plugin );
if ( $src === false
    || ! preg_match( '/\$redirect_to = remove_query_arg.*?(?=\n\s*wp_redirect)/s', $src, $m ) ) {
    echo "FAIL  could not extract the redirect_to validation block from $plugin\n";
    exit( 1 );
}
eval( "function op_pick_redirect() {\n" . $m[0] . "\nreturn \$redirect_to;\n}" );

function pick( $redirect_to ) {
    $_GET = array( 'redirect_to' => $redirect_to );
    return op_pick_redirect();
}

// --- Cases --------------------------------------------------------------

// Ordinary relative path: accepted as-is.
assert_eq( 'accepts /dashboard', '/dashboard', pick( '/dashboard' ) );

// Tab-smuggled protocol-relative bypass (the URL form is /%09/evil.com; PHP
// hands the plugin the decoded "/\t/evil.com"). Raw, the second character is
// a tab, so validate-before-sanitize passed it — and wp_redirect()'s internal
// sanitize then collapsed it to "//evil.com", an open redirect. Sanitizing
// first collapses it BEFORE the regex looks, so it must fall back.
assert_eq( 'rejects /%09/evil.com (tab-smuggled //)', OP_FALLBACK, pick( "/\t/evil.com" ) );

// Backslash form: browsers normalize "/\evil.com" like "//evil.com". After
// the sanitize strips the backslash it survives only as the harmless local
// path "/evil.com" — what matters is that no backslash and no leading "//"
// can ever reach the Location header.
$out = pick( '/\\evil.com' );
if ( strpos( $out, '\\' ) === false && substr( $out, 0, 2 ) !== '//' ) {
    ok( 'rejects /\\evil.com as an external target (got ' . var_export( $out, true ) . ')' );
} else {
    bad( 'rejects /\\evil.com as an external target', 'got ' . var_export( $out, true ) );
}
// And the regex itself rejects a surviving leading backslash outright
// (defense in depth for any sanitize that lets one through).
assert_eq( 'regex refuses a leading backslash after /',
    0, preg_match( '#^/($|[^/\\\\])#', '/\\evil.com' ) );

printf( "\n%d passed, %d failed\n", $pass, $fail );
exit( $fail === 0 ? 0 : 1 );

<?php
/**
 * test-static-receiver-upload.php — unit coverage for the pure helpers added
 * to onionpress-static-receiver.php for the v1.2 multipart carrier:
 * onionpress_static_upload_error_message() (the UPLOAD_ERR_* mapping) and the
 * extractor's short-read/short-write/fclose truncation guards.
 *
 * These two are plain functions with no WordPress dependency, so they run
 * standalone: `php tests/test-static-receiver-upload.php`. No live stack, no
 * WP stubs needed — ABSPATH is defined first so the plugin file's top-of-
 * file `exit` guard does not fire.
 *
 * Exit status is 0 only if every assertion passes (mirrors test-receiver.sh).
 */

define( 'ABSPATH', __DIR__ );
// The plugin file's bottom-of-file add_action( 'rest_api_init', ... ) call
// needs a stub outside a full WordPress load; the closure it registers is
// never invoked here, so a no-op is enough for the two pure functions this
// script actually exercises.
function add_action( $hook, $callback ) {}
require __DIR__ . '/../app/Resources/plugins/onionpress-static-receiver.php';

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

// --- UPLOAD_ERR_* mapping ----------------------------------------------

$cases = array(
    array( UPLOAD_ERR_OK,         null ), // never routed through the mapper; see caller
    array( UPLOAD_ERR_INI_SIZE,   413 ),
    array( UPLOAD_ERR_FORM_SIZE,  413 ),
    array( UPLOAD_ERR_PARTIAL,    400 ),
    array( UPLOAD_ERR_NO_FILE,    400 ),
    array( UPLOAD_ERR_NO_TMP_DIR, 500 ),
    array( UPLOAD_ERR_CANT_WRITE, 500 ),
    array( UPLOAD_ERR_EXTENSION,  500 ),
    array( -1,                    400 ), // unknown code, e.g. missing 'error' key
);
foreach ( $cases as $c ) {
    list( $code, $expected_status ) = $c;
    if ( $code === UPLOAD_ERR_OK ) {
        continue; // route callback never calls the mapper for UPLOAD_ERR_OK
    }
    list( $msg, $status ) = onionpress_static_upload_error_message( $code );
    assert_eq( "upload error $code maps to HTTP $expected_status", $expected_status, $status );
    if ( is_string( $msg ) && $msg !== '' ) {
        ok( "upload error $code has a non-empty message" );
    } else {
        bad( "upload error $code has a non-empty message", 'got ' . var_export( $msg, true ) );
    }
}
// INI_SIZE and FORM_SIZE must name the ini key they map to, per the spec.
list( $ini_msg ) = onionpress_static_upload_error_message( UPLOAD_ERR_INI_SIZE );
if ( strpos( $ini_msg, 'upload_max_filesize' ) !== false ) {
    ok( 'UPLOAD_ERR_INI_SIZE message names upload_max_filesize' );
} else {
    bad( 'UPLOAD_ERR_INI_SIZE message names upload_max_filesize', $ini_msg );
}

// --- extractor truncation guards ----------------------------------------

function make_tar_with_one_file( $rel, $contents ) {
    $tmp = sys_get_temp_dir() . '/onionpress-test-' . uniqid( '', true );
    mkdir( $tmp, 0755, true );
    file_put_contents( $tmp . '/' . $rel, $contents );
    $tar = $tmp . '.tar';
    exec( sprintf( 'tar -cf %s -C %s .', escapeshellarg( $tar ), escapeshellarg( $tmp ) ), $out, $rc );
    if ( $rc !== 0 ) {
        fwrite( STDERR, "fixture tar creation failed\n" );
        exit( 2 );
    }
    return array( $tar, $tmp );
}

function rrmdir( $dir ) {
    if ( ! is_dir( $dir ) ) {
        return;
    }
    foreach ( scandir( $dir ) as $entry ) {
        if ( $entry === '.' || $entry === '..' ) {
            continue;
        }
        $p = $dir . '/' . $entry;
        is_dir( $p ) ? rrmdir( $p ) : unlink( $p );
    }
    rmdir( $dir );
}

// 1. A well-formed tar extracts cleanly end-to-end (control case).
list( $tar, $srcdir ) = make_tar_with_one_file( 'hello.txt', str_repeat( 'x', 10000 ) );
$dest = sys_get_temp_dir() . '/onionpress-test-dest-' . uniqid( '', true );
list( $ok_result, $err ) = onionpress_static_extract_tar( $tar, $dest );
if ( $ok_result && file_exists( $dest . '/hello.txt' )
    && filesize( $dest . '/hello.txt' ) === 10000 ) {
    ok( 'well-formed tar extracts completely' );
} else {
    bad( 'well-formed tar extracts completely', $err );
}
rrmdir( $srcdir );
rrmdir( $dest );
unlink( $tar );

// 2. A tar whose data section is truncated mid-file must fail the
//    extraction, not silently return array(true, null) with a short file
//    on disk. Truncate right after the header (byte 512): the loop's fread
//    hits EOF with $remaining > 0 well before the declared $size is met.
list( $tar, $srcdir ) = make_tar_with_one_file( 'big.bin', str_repeat( 'y', 20000 ) );
$full = file_get_contents( $tar );
$truncated_tar = $tar . '.truncated';
file_put_contents( $truncated_tar, substr( $full, 0, 512 + 100 ) ); // header + 100 bytes of the 20000
$dest2 = sys_get_temp_dir() . '/onionpress-test-dest2-' . uniqid( '', true );
list( $ok2, $err2 ) = onionpress_static_extract_tar( $truncated_tar, $dest2 );
if ( $ok2 === false && is_string( $err2 ) && strpos( $err2, 'truncated' ) !== false ) {
    ok( 'truncated tar data is rejected, not silently accepted' );
} else {
    bad( 'truncated tar data is rejected, not silently accepted',
        'ok=' . var_export( $ok2, true ) . ' err=' . var_export( $err2, true ) );
}
rrmdir( $srcdir );
rrmdir( $dest2 );
unlink( $tar );
unlink( $truncated_tar );

// --- extractor security guards ------------------------------------------
//
// The reject paths are the security-critical half of the extractor, and a
// system tar refuses to *create* most of these archives — so they are built
// by hand: a raw 512-byte ustar header with a correct checksum, byte-for-
// byte what a hostile client could POST.

function make_ustar_header( $name, $size, $typeflag, $linkname = '' ) {
    $h  = str_pad( substr( $name, 0, 100 ), 100, "\0" );      // name
    $h .= "0000755\0";                                         // mode
    $h .= "0000000\0";                                         // uid
    $h .= "0000000\0";                                         // gid
    $h .= str_pad( decoct( $size ), 11, '0', STR_PAD_LEFT ) . "\0"; // size
    $h .= "00000000000\0";                                     // mtime
    $h .= '        ';                                          // chksum (spaces while summing)
    $h .= $typeflag;                                           // typeflag
    $h .= str_pad( substr( $linkname, 0, 100 ), 100, "\0" );   // linkname
    $h .= "ustar\0";                                           // magic
    $h .= '00';                                                // version
    $h  = str_pad( $h, 512, "\0" );
    $sum = 0;
    for ( $i = 0; $i < 512; $i++ ) {
        $sum += ord( $h[ $i ] );
    }
    $chk = str_pad( decoct( $sum ), 6, '0', STR_PAD_LEFT ) . "\0 ";
    return substr( $h, 0, 148 ) . $chk . substr( $h, 156 );
}

function make_evil_tar( $entries ) { // array of [name, data, typeflag, linkname]
    $tar = '';
    foreach ( $entries as $e ) {
        list( $name, $data, $typeflag, $linkname ) = array_pad( $e, 4, '' );
        $tar .= make_ustar_header( $name, strlen( $data ), $typeflag, $linkname );
        if ( $data !== '' ) {
            $tar .= str_pad( $data, (int) ( ceil( strlen( $data ) / 512 ) * 512 ), "\0" );
        }
    }
    $tar .= str_repeat( "\0", 1024 ); // end-of-archive
    $path = sys_get_temp_dir() . '/onionpress-evil-' . uniqid( '', true ) . '.tar';
    file_put_contents( $path, $tar );
    return $path;
}

function assert_rejected( $label, $tar_path, $expect_substr ) {
    $dest = sys_get_temp_dir() . '/onionpress-evil-dest-' . uniqid( '', true );
    list( $ok_r, $err_r ) = onionpress_static_extract_tar( $tar_path, $dest );
    if ( $ok_r === false && is_string( $err_r )
        && strpos( $err_r, $expect_substr ) !== false ) {
        ok( $label );
    } else {
        bad( $label, 'ok=' . var_export( $ok_r, true ) . ' err=' . var_export( $err_r, true ) );
    }
    rrmdir( $dest );
    unlink( $tar_path );
}

// Symlink and hardlink entries (typeflag 2 / 1).
assert_rejected( 'symlink entry is rejected',
    make_evil_tar( array( array( 'link', '', '2', '/etc/passwd' ) ) ),
    'link' );
assert_rejected( 'hardlink entry is rejected',
    make_evil_tar( array( array( 'link', '', '1', 'target' ) ) ),
    'link' );

// Device / FIFO entries (typeflag 3, 4, 6).
assert_rejected( 'character device entry is rejected',
    make_evil_tar( array( array( 'dev', '', '3' ) ) ),
    'device' );
assert_rejected( 'block device entry is rejected',
    make_evil_tar( array( array( 'dev', '', '4' ) ) ),
    'device' );
assert_rejected( 'FIFO entry is rejected',
    make_evil_tar( array( array( 'fifo', '', '6' ) ) ),
    'device' );

// Path traversal in the ustar name field.
assert_rejected( '.. traversal in entry name is rejected',
    make_evil_tar( array( array( '../../escape.txt', 'x', '0' ) ) ),
    '..' );
assert_rejected( 'absolute path in entry name is rejected',
    make_evil_tar( array( array( '/etc/cron.d/evil', 'x', '0' ) ) ),
    'absolute' );

// Traversal smuggled through a GNU longname ('L') carry entry: the ustar
// name is innocent, the payload names the real target.
assert_rejected( '.. traversal via GNU longname is rejected',
    make_evil_tar( array(
        array( '././@LongLink', "../../escape.txt\0", 'L' ),
        array( 'innocent.txt', 'x', '0' ),
    ) ),
    '..' );

// Traversal smuggled through a pax 'x' path= record.
$pax_record = '';
$pax_body   = " path=../../escape.txt\n";
$pax_record = ( strlen( $pax_body ) + strlen( (string) ( strlen( $pax_body ) + 3 ) ) + 1 ) . $pax_body;
assert_rejected( '.. traversal via pax path record is rejected',
    make_evil_tar( array(
        array( 'pax-header', $pax_record, 'x' ),
        array( 'innocent.txt', 'x', '0' ),
    ) ),
    '..' );

// A header that is not ustar/pax at all must fail closed.
$not_ustar = sys_get_temp_dir() . '/onionpress-evil-' . uniqid( '', true ) . '.tar';
file_put_contents( $not_ustar,
    str_pad( 'garbage header pretending to be tar', 512, 'A' ) . str_repeat( "\0", 1024 ) );
assert_rejected( 'non-ustar header fails closed',
    $not_ustar, 'ustar' );

// And the guards must not make the extractor over-reject: a benign nested
// directory layout still extracts (control for the traversal checks).
list( $tar, $srcdir ) = make_tar_with_one_file( 'page.html', '<html></html>' );
$dest3 = sys_get_temp_dir() . '/onionpress-test-dest3-' . uniqid( '', true );
list( $ok3, $err3 ) = onionpress_static_extract_tar( $tar, $dest3 );
if ( $ok3 && file_exists( $dest3 . '/page.html' ) ) {
    ok( 'guards do not reject a benign archive' );
} else {
    bad( 'guards do not reject a benign archive', (string) $err3 );
}
rrmdir( $srcdir );
rrmdir( $dest3 );
unlink( $tar );

// --- permission callback: deterministic $_SERVER-driven paths ------------

function with_server( $server, $fn ) {
    $saved = $_SERVER;
    $_SERVER = $server;
    $result = $fn();
    $_SERVER = $saved;
    return $result;
}

assert_eq( 'X-Forwarded-For header denies the request', false,
    with_server( array( 'REMOTE_ADDR' => '127.0.0.1', 'HTTP_X_FORWARDED_FOR' => '1.2.3.4' ),
        'onionpress_static_is_local_request' ) );
assert_eq( 'missing REMOTE_ADDR denies the request', false,
    with_server( array(), 'onionpress_static_is_local_request' ) );
assert_eq( 'IPv4 loopback is allowed', true,
    with_server( array( 'REMOTE_ADDR' => '127.0.0.1' ),
        'onionpress_static_is_local_request' ) );
assert_eq( 'IPv6 loopback is allowed', true,
    with_server( array( 'REMOTE_ADDR' => '::1' ),
        'onionpress_static_is_local_request' ) );
// TEST-NET-3 can never be this machine's default gateway, so the allowlist
// must deny it (this is the positive-check property: an unknown source is
// out, without any container-name enumeration).
assert_eq( 'a non-loopback, non-gateway source is denied', false,
    with_server( array( 'REMOTE_ADDR' => '203.0.113.7' ),
        'onionpress_static_is_local_request' ) );

echo "\n";
printf( "RESULT: %d passed, %d failed\n", $pass, $fail );
exit( $fail === 0 ? 0 : 1 );

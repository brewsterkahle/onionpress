<?php
/**
 * Plugin Name: OnionPress Static Receiver
 * Description: Loopback REST endpoints that let a static-site publisher
 *              deliver a pre-rendered site into OnionPress. The publisher
 *              uploads a tar of a generated site, the receiver extracts it under
 *              /var/www/html/site-generations/<id>/, and an atomic symlink
 *              flip of /var/www/html/site/current makes it live. Apache then
 *              serves those files ahead of WordPress (see
 *              onionpress-static-site.conf). Trusts only local requests.
 * Version:     1.0
 * Network:     true
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

/**
 * Filesystem layout shared by the three endpoints.
 *
 *   GENERATIONS/<id>/   — one extracted site generation (static files)
 *   SITE/current        — symlink to the live generation
 *
 * `current` lives next to (not inside) the generations dir so a generation
 * can never contain a name that collides with the `current` symlink.
 */
define( 'ONIONPRESS_STATIC_WEB_ROOT',    '/var/www/html' );
define( 'ONIONPRESS_STATIC_SITE_DIR',    '/var/www/html/site' );
define( 'ONIONPRESS_STATIC_CURRENT',     '/var/www/html/site/current' );
define( 'ONIONPRESS_STATIC_GENERATIONS', '/var/www/html/site-generations' );
define( 'ONIONPRESS_STATIC_KEEP',        3 );

/**
 * The default gateway of this container's network namespace, or null when it
 * cannot be determined. Cached for the request lifetime.
 *
 * Traffic that enters through Docker's host port map is NATed, so inside the
 * container its source address is the bridge gateway — never 127.0.0.1. This
 * is the one address that means "came in through the published port" rather
 * than "came from some other container on the bridge".
 */
function onionpress_static_gateway_ip() {
    static $gateway = false; // false = not computed yet; null = unknown
    if ( $gateway !== false ) {
        return $gateway;
    }
    $gateway = null;
    $route = @file_get_contents( '/proc/net/route' );
    if ( is_string( $route ) ) {
        foreach ( explode( "\n", $route ) as $line ) {
            $cols = preg_split( '/\s+/', trim( $line ) );
            // Destination 00000000 is the default route; column 2 is the
            // gateway as little-endian hex.
            if ( count( $cols ) >= 3 && $cols[1] === '00000000' ) {
                $ip = long2ip( (int) hexdec( implode( '', array_reverse(
                    str_split( $cols[2], 2 ) ) ) ) );
                if ( $ip !== false && $ip !== '0.0.0.0' ) {
                    $gateway = $ip;
                }
                break;
            }
        }
    }
    return $gateway;
}

/**
 * Localhost trust, shared by every route: allow only requests that provably
 * arrived through the host's port map, deny everything else.
 *
 * The receiver is driven by a publisher on the same machine, reaching Apache
 * over the host's loopback port map. It must never be reachable by a public
 * onion visitor. This is a positive allowlist, not a denylist:
 *
 *   1. Any `X-Forwarded-*` header means the request was relayed by a proxy.
 *      Neither the onion serving path (tor -> apache) nor the host port map
 *      sets one, so their presence is a spoof or misconfiguration — deny.
 *   2. REMOTE_ADDR must be loopback (php-served directly) or this
 *      container's default gateway. A bare `REMOTE_ADDR === '127.0.0.1'`
 *      check does not work under Docker: the host port publish NATs the
 *      connection, so a request from the host's loopback arrives with the
 *      bridge gateway as its source. Conversely the tor and onionheaven
 *      containers reach Apache by their own bridge addresses, which are
 *      never the gateway — so onion-side traffic is denied without having
 *      to enumerate container names.
 *
 * The gateway cannot distinguish host-loopback from LAN clients when the
 * port map is bound to 0.0.0.0 — NAT erases that. The compose file binds
 * the publish to 127.0.0.1 by default (ONIONPRESS_BIND_HOST); overriding
 * that to a public interface deliberately widens who can publish.
 *
 * Fails closed: no REMOTE_ADDR, or an undeterminable gateway on a
 * non-loopback source, means deny.
 *
 * @return bool True when the request may proceed.
 */
function onionpress_static_is_local_request() {
    // 1. Reject anything carrying proxy-forwarding headers.
    foreach ( array_keys( $_SERVER ) as $key ) {
        if ( strpos( $key, 'HTTP_X_FORWARDED_' ) === 0 ) {
            return false;
        }
    }

    // 2. Positive source check: loopback, or the NAT gateway.
    $remote = isset( $_SERVER['REMOTE_ADDR'] ) ? $_SERVER['REMOTE_ADDR'] : '';
    if ( $remote === '' ) {
        return false;
    }
    if ( $remote === '::1' || strpos( $remote, '127.' ) === 0 ) {
        return true;
    }
    $gateway = onionpress_static_gateway_ip();
    return $gateway !== null && $remote === $gateway;
}

/**
 * Read the current onion address, or '' if not known yet.
 * Primary source is the flat address file; status.json is the fallback.
 */
function onionpress_static_onion_address() {
    $f = '/var/lib/onionpress/onion_address';
    if ( is_readable( $f ) ) {
        $addr = trim( (string) @file_get_contents( $f ) );
        if ( $addr !== '' ) {
            return $addr;
        }
    }
    $sf = '/var/lib/onionpress/status.json';
    if ( is_readable( $sf ) ) {
        $data = json_decode( (string) @file_get_contents( $sf ), true );
        if ( is_array( $data ) && ! empty( $data['onion_address'] ) ) {
            return (string) $data['onion_address'];
        }
    }
    return '';
}

/**
 * External reachability, as last observed by the health checker — a
 * tri-state: null means not yet checked, never "confirmed unreachable".
 *
 * Read straight from status.json — the same file onion_address falls back
 * to — rather than re-probing here: the reachability check is a real
 * Tor-routed curl (see health.py's check_external_reachability), too slow
 * to run inline in a REST response. `reachable` is null (not false) until
 * the checker has actually completed Check 5 at least once; a caller must
 * not read null as "confirmed unreachable".
 *
 * @return array{reachable: bool|null, http_code: string|null}
 */
function onionpress_static_reachability() {
    $sf = '/var/lib/onionpress/status.json';
    if ( is_readable( $sf ) ) {
        $data = json_decode( (string) @file_get_contents( $sf ), true );
        if ( is_array( $data ) ) {
            $reachable = array_key_exists( 'onion_reachable', $data ) ? $data['onion_reachable'] : null;
            $http_code = array_key_exists( 'onion_http_code', $data ) ? $data['onion_http_code'] : null;
            return array(
                'reachable' => is_bool( $reachable ) ? $reachable : null,
                'http_code' => is_string( $http_code ) ? $http_code : null,
            );
        }
    }
    return array( 'reachable' => null, 'http_code' => null );
}

/**
 * The id of the live generation (basename of the `current` symlink target),
 * or null when nothing has been committed yet.
 */
function onionpress_static_current_generation() {
    if ( is_link( ONIONPRESS_STATIC_CURRENT ) ) {
        $target = @readlink( ONIONPRESS_STATIC_CURRENT );
        if ( is_string( $target ) && $target !== '' ) {
            return basename( $target );
        }
    }
    return null;
}

/**
 * A generation id is an opaque directory name. Reject anything that could
 * escape the generations dir. Publishers send an opaque id such as
 * `gen-<unix_seconds>`.
 */
function onionpress_static_valid_id( $id ) {
    return is_string( $id )
        && $id !== ''
        && strpos( $id, "\0" ) === false
        && strpos( $id, '/' ) === false
        && strpos( $id, '..' ) === false
        && $id === basename( $id );
}

/** Uniform error response: 4xx/5xx JSON `{ ok:false, error:… }`. */
function onionpress_static_error( $message, $status ) {
    return new WP_REST_Response(
        array( 'ok' => false, 'error' => $message ),
        $status
    );
}

/**
 * Recursively delete a path. Never follows symlinks (a symlink is unlinked,
 * not descended into).
 */
function onionpress_static_rmrf( $path ) {
    if ( is_link( $path ) || ! file_exists( $path ) ) {
        @unlink( $path );
        return;
    }
    if ( is_dir( $path ) ) {
        $items = new RecursiveIteratorIterator(
            new RecursiveDirectoryIterator( $path, FilesystemIterator::SKIP_DOTS ),
            RecursiveIteratorIterator::CHILD_FIRST
        );
        foreach ( $items as $item ) {
            if ( $item->isLink() || ! $item->isDir() ) {
                @unlink( $item->getPathname() );
            } else {
                @rmdir( $item->getPathname() );
            }
        }
        @rmdir( $path );
    } else {
        @unlink( $path );
    }
}

/**
 * Read exactly $size bytes of tar payload, then consume padding up to the
 * next 512-byte boundary ($padded). Returns the bytes read.
 */
function onionpress_static_read_n( $fh, $size, $padded ) {
    $buf       = '';
    $remaining = $size;
    while ( $remaining > 0 ) {
        $chunk = fread( $fh, $remaining > 524288 ? 524288 : $remaining );
        if ( $chunk === '' || $chunk === false ) {
            break;
        }
        $buf       .= $chunk;
        $remaining -= strlen( $chunk );
    }
    $pad = $padded - $size;
    if ( $pad > 0 ) {
        fread( $fh, $pad );
    }
    return $buf;
}

/**
 * Streaming, security-hardened extractor for a plain (uncompressed) tar.
 *
 * Why not PharData: the contract's `tar -cf x.tar -C <gendir> .` emits a `.`
 * self-entry that PharData::extractTo cannot handle ("Cannot extract '.'"),
 * so it fails on every real publisher upload. This reader also lets the
 * traversal
 * and link guards run inline, in one pass, failing closed on anything it does
 * not positively recognise as a regular file or directory.
 *
 * Rejects: hard/symlinks (typeflag 1/2), devices/FIFOs (3/4/6), absolute
 * paths, `..` traversal, embedded NUL, and non-ustar headers.
 *
 * @return array{0:bool,1:?string} [ok, error message]
 */
function onionpress_static_extract_tar( $tar_path, $dest_dir ) {
    $fh = @fopen( $tar_path, 'rb' );
    if ( ! $fh ) {
        return array( false, 'cannot open tar' );
    }
    if ( ! is_dir( $dest_dir ) && ! @mkdir( $dest_dir, 0755, true ) ) {
        fclose( $fh );
        return array( false, 'cannot create destination' );
    }
    $dest_real = realpath( $dest_dir );
    if ( $dest_real === false ) {
        fclose( $fh );
        return array( false, 'destination missing' );
    }

    $pending_longname = null; // carried from a GNU 'L' entry
    $pending_paxpath  = null; // carried from a pax 'x' path= record

    while ( ! feof( $fh ) ) {
        $header = fread( $fh, 512 );
        if ( $header === '' || $header === false ) {
            break; // clean EOF
        }
        if ( strlen( $header ) < 512 ) {
            fclose( $fh );
            return array( false, 'truncated tar header' );
        }
        // A zero block marks the end of the archive.
        if ( trim( $header, "\0" ) === '' ) {
            break;
        }

        $name     = rtrim( substr( $header, 0, 100 ), "\0" );
        $size_fld = trim( substr( $header, 124, 12 ), " \0" );
        $typeflag = substr( $header, 156, 1 );
        $magic    = substr( $header, 257, 5 );
        $prefix   = rtrim( substr( $header, 345, 155 ), "\0" );

        // Fail closed on anything that is not a POSIX ustar/pax header.
        if ( $magic !== 'ustar' ) {
            fclose( $fh );
            return array( false, 'unrecognised tar header (not ustar/pax)' );
        }

        // GNU base-256 large-file size (high bit set) is out of scope.
        if ( $size_fld !== '' && ( ord( $size_fld[0] ) & 0x80 ) ) {
            fclose( $fh );
            return array( false, 'unsupported large-file size field' );
        }
        $size        = $size_fld === '' ? 0 : (int) octdec( $size_fld );
        $data_padded = (int) ( ceil( $size / 512 ) * 512 );

        // GNU long-name / long-link carry entries: payload names the *next*
        // entry. Links are rejected below, so a long link name is discarded.
        if ( $typeflag === 'L' ) {
            $pending_longname = rtrim(
                onionpress_static_read_n( $fh, $size, $data_padded ), "\0" );
            continue;
        }
        if ( $typeflag === 'K' ) {
            onionpress_static_read_n( $fh, $size, $data_padded );
            continue;
        }
        // pax extended header: pull a `path=` record if present.
        if ( $typeflag === 'x' || $typeflag === 'g' ) {
            $paxdata = onionpress_static_read_n( $fh, $size, $data_padded );
            if ( preg_match( '/^\d+ path=([^\n]*)\n/m', $paxdata, $m ) ) {
                $pending_paxpath = $m[1];
            }
            continue;
        }

        // Resolve the effective name: pax > GNU longname > ustar name+prefix.
        if ( $pending_paxpath !== null ) {
            $full = $pending_paxpath;
        } elseif ( $pending_longname !== null ) {
            $full = $pending_longname;
        } else {
            $full = $prefix !== '' ? $prefix . '/' . $name : $name;
        }
        $pending_longname = null;
        $pending_paxpath  = null;

        // SECURITY: reject links and special files.
        if ( $typeflag === '1' || $typeflag === '2' ) {
            fclose( $fh );
            return array( false, 'archive contains a hard/symbolic link' );
        }
        if ( $typeflag === '3' || $typeflag === '4' || $typeflag === '6' ) {
            fclose( $fh );
            return array( false, 'archive contains a device or FIFO entry' );
        }

        // SECURITY: reject NUL, absolute paths, and `..` traversal.
        if ( strpos( $full, "\0" ) !== false ) {
            fclose( $fh );
            return array( false, 'archive entry name contains NUL' );
        }
        $full = str_replace( '\\', '/', $full );
        if ( isset( $full[0] ) && $full[0] === '/' ) {
            fclose( $fh );
            return array( false, 'archive entry uses an absolute path' );
        }
        $segments = array();
        foreach ( explode( '/', $full ) as $seg ) {
            if ( $seg === '' || $seg === '.' ) {
                continue;
            }
            if ( $seg === '..' ) {
                fclose( $fh );
                return array( false, 'archive entry escapes with ..' );
            }
            $segments[] = $seg;
        }
        $rel = implode( '/', $segments );

        $is_dir = ( $typeflag === '5' );

        // The `.` self-entry from `tar -C <dir> .` normalises to empty.
        if ( $rel === '' ) {
            if ( ! $is_dir && $size > 0 ) {
                onionpress_static_read_n( $fh, $size, $data_padded );
            }
            continue;
        }

        $target = $dest_real . '/' . $rel;

        if ( $is_dir ) {
            if ( ! is_dir( $target ) && ! @mkdir( $target, 0755, true ) ) {
                fclose( $fh );
                return array( false, 'cannot create directory ' . $rel );
            }
            continue;
        }

        // Regular file ('0', "\0", or '7' contiguous): stream its bytes out.
        $parent = dirname( $target );
        if ( ! is_dir( $parent ) && ! @mkdir( $parent, 0755, true ) ) {
            fclose( $fh );
            return array( false, 'cannot create parent for ' . $rel );
        }
        $out = @fopen( $target, 'wb' );
        if ( ! $out ) {
            fclose( $fh );
            return array( false, 'cannot write ' . $rel );
        }
        $remaining = $size;
        while ( $remaining > 0 ) {
            $chunk = fread( $fh, $remaining > 524288 ? 524288 : $remaining );
            if ( $chunk === '' || $chunk === false ) {
                fclose( $out );
                fclose( $fh );
                return array( false, 'truncated file data in ' . $rel );
            }
            $written = fwrite( $out, $chunk );
            if ( $written === false || $written !== strlen( $chunk ) ) {
                fclose( $out );
                fclose( $fh );
                return array( false, 'short write extracting ' . $rel );
            }
            $remaining -= $written;
        }
        if ( ! fclose( $out ) ) {
            fclose( $fh );
            return array( false, 'short write extracting ' . $rel );
        }
        // Consume padding to the next 512-byte boundary.
        $pad = $data_padded - $size;
        if ( $pad > 0 ) {
            fread( $fh, $pad );
        }
    }

    fclose( $fh );
    return array( true, null );
}

/**
 * GET /status — advertise the receiver so a publisher can discover its port.
 */
function onionpress_static_route_status() {
    $reachability = onionpress_static_reachability();
    return new WP_REST_Response( array(
        'onion_address'      => onionpress_static_onion_address(),
        'current_generation' => onionpress_static_current_generation(),
        'receiver_version'   => '2.0',
        'onion_reachable'    => $reachability['reachable'],
        'onion_http_code'    => $reachability['http_code'],
    ), 200 );
}

/**
 * Map a PHP upload error code to a message naming the likely ini key.
 * rfc1867.c cancels an oversize part mid-write and reports
 * UPLOAD_ERR_INI_SIZE rather than failing the request outright, so this
 * check must run before is_uploaded_file() -- otherwise a truncated tar
 * is silently accepted as if it were complete.
 */
function onionpress_static_upload_error_message( $code ) {
    switch ( $code ) {
        case UPLOAD_ERR_INI_SIZE:
            return array( 'upload exceeds upload_max_filesize', 413 );
        case UPLOAD_ERR_FORM_SIZE:
            return array( 'upload exceeds the form-declared MAX_FILE_SIZE', 413 );
        case UPLOAD_ERR_PARTIAL:
            return array( 'upload was only partially received', 400 );
        case UPLOAD_ERR_NO_FILE:
            return array( 'no file part received', 400 );
        case UPLOAD_ERR_NO_TMP_DIR:
            return array( 'server has no upload_tmp_dir', 500 );
        case UPLOAD_ERR_CANT_WRITE:
            return array( 'server failed to write the upload to disk', 500 );
        case UPLOAD_ERR_EXTENSION:
            return array( 'a PHP extension aborted the upload', 500 );
        default:
            return array( 'unknown upload error (code ' . $code . ')', 400 );
    }
}

/**
 * Move an uploaded file part to $dest, streaming rather than buffering it
 * into a PHP string when move_uploaded_file() cannot (cross-device tmp).
 *
 * @return array{0:bool,1:?string} [ok, error message]
 */
function onionpress_static_save_uploaded_part( $tmp_name, $dest ) {
    if ( @move_uploaded_file( $tmp_name, $dest ) ) {
        return array( true, null );
    }

    $in = @fopen( $tmp_name, 'rb' );
    if ( ! $in ) {
        return array( false, 'cannot open uploaded part' );
    }
    $out = @fopen( $dest, 'wb' );
    if ( ! $out ) {
        fclose( $in );
        return array( false, 'cannot write upload' );
    }
    $copied = @stream_copy_to_stream( $in, $out );
    $closed_in  = fclose( $in );
    $closed_out = fclose( $out );
    if ( $copied === false || ! $closed_in || ! $closed_out ) {
        @unlink( $dest );
        return array( false, 'cannot write upload' );
    }
    return array( true, null );
}

/**
 * POST /generation?id=<id> — accept a tar of one site generation.
 *
 * The only carrier is a multipart/form-data upload with the tar in a part
 * named `tar`: PHP's rfc1867.c streams that straight to upload_tmp_dir, so
 * a large site never transits PHP memory. A raw `application/x-tar` body
 * is NOT accepted (removed at receiver_version 2.0): WordPress's REST
 * server buffers a raw body into a PHP string *before* the permission
 * callback runs, so an oversize upload spent its memory before the route
 * could reject it — the multipart carrier is what makes the upload safe,
 * not just faster.
 *
 * The part is written to <id>.tar, extracted into <id>.tmp/, then
 * atomically renamed to <id>/.
 */
function onionpress_static_route_generation( $request ) {
    $id = $request->get_param( 'id' );
    if ( ! onionpress_static_valid_id( $id ) ) {
        return onionpress_static_error( 'invalid generation id', 400 );
    }

    if ( ! is_dir( ONIONPRESS_STATIC_GENERATIONS )
        && ! @mkdir( ONIONPRESS_STATIC_GENERATIONS, 0755, true ) ) {
        return onionpress_static_error( 'cannot create generations dir', 500 );
    }

    $tar_path  = ONIONPRESS_STATIC_GENERATIONS . '/' . $id . '.tar';
    $tmp_dir   = ONIONPRESS_STATIC_GENERATIONS . '/' . $id . '.tmp';
    $final_dir = ONIONPRESS_STATIC_GENERATIONS . '/' . $id;

    $files = $request->get_file_params();
    if ( ! isset( $files['tar'] ) ) {
        return onionpress_static_error(
            'expected a multipart/form-data upload with the tar in a part named "tar"',
            400 );
    }
    $f = $files['tar'];

    // Check the upload error first and explicitly -- see the docblock
    // above onionpress_static_upload_error_message().
    if ( ! isset( $f['error'] ) || $f['error'] !== UPLOAD_ERR_OK ) {
        list( $msg, $status ) = onionpress_static_upload_error_message(
            isset( $f['error'] ) ? $f['error'] : -1 );
        return onionpress_static_error( $msg, $status );
    }
    if ( empty( $f['tmp_name'] ) || ! is_uploaded_file( $f['tmp_name'] ) ) {
        return onionpress_static_error( 'upload did not arrive via HTTP POST', 400 );
    }

    list( $ok, $err ) = onionpress_static_save_uploaded_part( $f['tmp_name'], $tar_path );
    if ( ! $ok ) {
        return onionpress_static_error( $err, 500 );
    }

    // Fresh extraction target.
    onionpress_static_rmrf( $tmp_dir );

    list( $ok, $err ) = onionpress_static_extract_tar( $tar_path, $tmp_dir );
    if ( ! $ok ) {
        onionpress_static_rmrf( $tmp_dir );
        @unlink( $tar_path );
        return onionpress_static_error( 'extract failed: ' . $err, 400 );
    }

    // A generation id is single-use; refuse to clobber an existing one.
    if ( file_exists( $final_dir ) ) {
        onionpress_static_rmrf( $tmp_dir );
        @unlink( $tar_path );
        return onionpress_static_error( 'generation already exists', 409 );
    }

    if ( ! @rename( $tmp_dir, $final_dir ) ) {
        onionpress_static_rmrf( $tmp_dir );
        @unlink( $tar_path );
        return onionpress_static_error( 'cannot finalise generation', 500 );
    }

    @unlink( $tar_path );

    return new WP_REST_Response(
        array( 'ok' => true, 'generation' => $id ), 200 );
}

/**
 * POST /commit {generation:"<id>"} — flip the live site to a generation.
 *
 * Guards against a generation whose top-level names would shadow WordPress
 * itself or an existing subsite, flips `site/current` atomically, then GCs
 * old generations (keeping the newest few and always the live one).
 */
function onionpress_static_route_commit( $request ) {
    $id = $request->get_param( 'generation' );
    if ( ! onionpress_static_valid_id( $id ) ) {
        return onionpress_static_error( 'invalid generation id', 400 );
    }

    $gen_dir = ONIONPRESS_STATIC_GENERATIONS . '/' . $id;
    if ( ! is_dir( $gen_dir ) ) {
        return onionpress_static_error( 'unknown generation', 404 );
    }

    // Collision guard: a static file served at the site root must never
    // shadow WordPress's own paths or an existing subsite.
    $reserved = array(
        'wp-admin', 'wp-content', 'wp-includes', 'wp-json',
        'wp-login.php', 'wp-cron.php', 'xmlrpc.php',
        'site', 'site-generations',
    );
    global $wpdb;
    if ( isset( $wpdb->blogs ) ) {
        $rows = $wpdb->get_col( "SELECT path FROM {$wpdb->blogs}" );
        foreach ( (array) $rows as $p ) {
            $seg = strtok( trim( (string) $p, '/' ), '/' );
            if ( $seg !== false && $seg !== '' ) {
                $reserved[] = $seg;
            }
        }
    }
    $reserved = array_unique( $reserved );

    foreach ( scandir( $gen_dir ) as $entry ) {
        if ( $entry === '.' || $entry === '..' ) {
            continue;
        }
        if ( in_array( $entry, $reserved, true ) ) {
            return onionpress_static_error(
                'generation collides with reserved path: ' . $entry, 409 );
        }
    }

    // Atomic flip: build the new symlink beside `current`, then rename over
    // it. rename() of a symlink is atomic on the same filesystem.
    if ( ! is_dir( ONIONPRESS_STATIC_SITE_DIR )
        && ! @mkdir( ONIONPRESS_STATIC_SITE_DIR, 0755, true ) ) {
        return onionpress_static_error( 'cannot create site dir', 500 );
    }
    $tmp_link = ONIONPRESS_STATIC_SITE_DIR . '/current.tmp-' . uniqid( '', true );
    @unlink( $tmp_link );
    if ( ! @symlink( $gen_dir, $tmp_link ) ) {
        return onionpress_static_error( 'cannot create symlink', 500 );
    }
    if ( ! @rename( $tmp_link, ONIONPRESS_STATIC_CURRENT ) ) {
        @unlink( $tmp_link );
        return onionpress_static_error( 'cannot activate generation', 500 );
    }

    onionpress_static_gc_generations();

    // A commit is the publisher's whole publish — it replaces the entire static
    // site in one atomic flip rather than creating/updating individual
    // WordPress posts, so the wayback archiver's post-level save_post
    // hook never fires for it. Re-archive home + feed the same way
    // save_post does for a WordPress-side publish, following the
    // archiver's own invalidate-then-kick mechanism instead of adding a
    // second submission path.
    if ( function_exists( 'onionpress_wayback_invalidate_sitewide' )
        && function_exists( 'onionpress_wayback_kick_now' ) ) {
        onionpress_wayback_invalidate_sitewide();
        onionpress_wayback_kick_now();
    }

    $addr = onionpress_static_onion_address();
    $url  = $addr !== '' ? 'http://' . $addr . '/' : '';
    return new WP_REST_Response(
        array( 'ok' => true, 'url' => $url ), 200 );
}

/**
 * Keep only the newest ONIONPRESS_STATIC_KEEP generations, never deleting the
 * one `current` points at.
 */
function onionpress_static_gc_generations() {
    $current = onionpress_static_current_generation();
    $dirs    = array();
    foreach ( scandir( ONIONPRESS_STATIC_GENERATIONS ) as $entry ) {
        if ( $entry === '.' || $entry === '..' ) {
            continue;
        }
        $p = ONIONPRESS_STATIC_GENERATIONS . '/' . $entry;
        if ( is_dir( $p ) && ! is_link( $p ) ) {
            $dirs[ $entry ] = @filemtime( $p );
        }
    }
    arsort( $dirs ); // newest first

    $i = 0;
    foreach ( $dirs as $name => $mtime ) {
        $i++;
        if ( $i <= ONIONPRESS_STATIC_KEEP ) {
            continue;
        }
        if ( $name === $current ) {
            continue; // never GC the live generation
        }
        onionpress_static_rmrf( ONIONPRESS_STATIC_GENERATIONS . '/' . $name );
    }
}

/**
 * Register the three routes under /wp-json/onionpress/v1/*. All share the
 * localhost-trust permission callback.
 */
add_action( 'rest_api_init', function () {
    $local = 'onionpress_static_is_local_request';

    register_rest_route( 'onionpress/v1', '/status', array(
        'methods'             => 'GET',
        'permission_callback' => $local,
        'callback'            => 'onionpress_static_route_status',
    ) );

    register_rest_route( 'onionpress/v1', '/generation', array(
        'methods'             => 'POST',
        'permission_callback' => $local,
        'callback'            => 'onionpress_static_route_generation',
    ) );

    register_rest_route( 'onionpress/v1', '/commit', array(
        'methods'             => 'POST',
        'permission_callback' => $local,
        'callback'            => 'onionpress_static_route_commit',
    ) );
} );

<?php
/**
 * OnionPress core-update helper. Run with `wp eval-file`, never directly.
 *
 * Why this file exists: wp-cli's own `core check-update`, `core update` and
 * `core verify-checksums` do NOT use WordPress's HTTP API. They call
 * WP_CLI\Utils\http_request(), which talks to api.wordpress.org directly and
 * honours neither the onionpress-tor-proxy mu-plugin nor HTTP(S)_PROXY (this
 * was verified: HTTPS_PROXY=socks5h://127.0.0.1:1 still returned a live
 * result). So every startup audit was phoning wordpress.org from the user's
 * real IP, over clearnet, with clearnet DNS — and failing outright whenever
 * the Colima VM's resolver wasn't up yet.
 *
 * Running inside WordPress via `wp eval-file` puts all of it back on the
 * WordPress HTTP API, which the http_api_curl hook below forces through Tor.
 *
 * Modes (first argument):
 *   check [<branch>]   — force a version check; print the release to update
 *                        to, if any (op_pick_core_update: this site's branch,
 *                        or <branch>, the image's, when newer)
 *   download <url>     — fetch a core zip to /tmp, print its path
 *   verify [<version>] — compare every core file against the official
 *                        checksums for that version
 *
 * Output is `key=value` lines on stdout, so the calling shell script can
 * parse it without jq (the image has no jq). `status` is always printed and
 * is one of: ok, netfail, error.
 */

if ( ! defined( 'ABSPATH' ) ) {
    fwrite( STDERR, "onionpress-core-update.php must be run via `wp eval-file`\n" );
    exit( 1 );
}

/** Emit one shell-parsable result line. */
function op_out( $key, $value = '' ) {
    printf( "%s=%s\n", $key, str_replace( array( "\r", "\n" ), ' ', (string) $value ) );
}

/**
 * Pick a Tor SOCKS proxy.
 *
 * Bulk outgoing traffic belongs on the onionheaven container, not on
 * onionpress-tor: that daemon is also hosting the user's onion service, and
 * a 25 MB core download would compete with inbound visitor circuits. Fall
 * back to onionpress-tor if onionheaven isn't accepting connections, since
 * a slower update beats no update — but never fall back to clearnet.
 */
function op_socks_proxy() {
    static $proxy = null;
    if ( null !== $proxy ) {
        return $proxy;
    }
    foreach ( array( 'onionheaven', 'onionpress-tor' ) as $host ) {
        $sock = @fsockopen( $host, 9050, $errno, $errstr, 3 );
        if ( $sock ) {
            fclose( $sock );
            return $proxy = 'socks5h://' . $host . ':9050';
        }
    }
    return $proxy = '';
}

/**
 * Force every request this script makes through Tor.
 *
 * Deliberately self-contained rather than relying on the
 * onionpress-tor-proxy mu-plugin: that plugin lives in the wordpress-data
 * volume and is installed by the app's post-install step, which can run
 * AFTER the container entrypoint that invokes this helper. On a first boot
 * the mu-plugin may simply not be there yet, and "no proxy" must never mean
 * "clearnet". Keep the two in sync —
 * app/Resources/plugins/onionpress-tor-proxy.php.
 *
 * CURLPROXY_SOCKS5_HOSTNAME resolves the hostname at the proxy, so there is
 * no clearnet DNS lookup either.
 */
add_action( 'http_api_curl', function ( $handle, $parsed_args = array() ) {
    $proxy = op_socks_proxy();
    if ( '' === $proxy ) {
        return;
    }
    curl_setopt( $handle, CURLOPT_PROXY, $proxy );
    curl_setopt( $handle, CURLOPT_PROXYTYPE, CURLPROXY_SOCKS5_HOSTNAME );
    curl_setopt( $handle, CURLOPT_CONNECTTIMEOUT, 30 );
    // Honour the caller's timeout when it is longer than the Tor floor — a
    // core zip over Tor takes minutes, and clamping it to a flat 30s (what
    // the mu-plugin used to do unconditionally) guarantees a failed download.
    $timeout = isset( $parsed_args['timeout'] ) ? (int) $parsed_args['timeout'] : 0;
    curl_setopt( $handle, CURLOPT_TIMEOUT, max( 120, $timeout ) );
}, 20, 2 );

/**
 * Refuse to make the request at all if the curl transport is unavailable.
 *
 * WordPress would silently fall back to the fsockopen transport, which the
 * hook above cannot proxy — that fallback is exactly how a "Tor-only" build
 * leaks to clearnet.
 */
add_filter( 'pre_http_request', function ( $pre, $args, $url ) {
    if ( ! function_exists( 'curl_init' ) ) {
        return new WP_Error(
            'onionpress_no_curl',
            'Refusing to request ' . $url . ': curl transport unavailable, cannot guarantee Tor routing'
        );
    }
    return $pre;
}, 10, 3 );

/**
 * The release to update to from $current, as array( version, full zip URL ),
 * or array( '', '' ): the newest of $offers (update_core's) on the site's
 * own branch (7.0.2 -> 7.0.3), where security backports land, or on
 * $image_branch, the branch this image was built and tested on
 * (WORDPRESS_BRANCH in the Dockerfile), when that is newer (7.0.x ->
 * 7.1.3). An unattended major could break the multisite conversion or the
 * bundled mu-plugins, so a new branch comes only with an image whose
 * release checked them against it: never past that branch, and an image of
 * an older branch never moves a site down.
 */
function op_pick_core_update( $current, $image_branch, $offers ) {
    $parts  = explode( '.', $current );
    $branch = ( count( $parts ) >= 2 ) ? $parts[0] . '.' . $parts[1] : $current;

    $branches = array( $branch . '.' );
    if ( preg_match( '/^[0-9]+\.[0-9]+$/D', $image_branch ) && version_compare( $image_branch, $branch, '>' ) ) {
        $branches[] = $image_branch . '.';
    }

    $target  = '';
    $package = '';
    foreach ( $offers as $offer ) {
        if ( empty( $offer->version ) || ! in_array( $offer->response, array( 'upgrade', 'autoupdate' ), true ) ) {
            continue;
        }
        $allowed = false;
        foreach ( $branches as $prefix ) {
            if ( 0 === strpos( $offer->version . '.', $prefix ) ) {
                $allowed = true;
            }
        }
        if ( ! $allowed ) {
            continue;   // neither this site's branch nor the image's
        }
        if ( version_compare( $offer->version, $current, '<=' ) ) {
            continue;
        }
        // Prefer the full zip: the partial/rollback packages assume a
        // specific from-version and wp-cli cannot apply them.
        $full = '';
        if ( ! empty( $offer->packages->full ) ) {
            $full = $offer->packages->full;
        } elseif ( ! empty( $offer->download ) ) {
            $full = $offer->download;
        }
        if ( '' === $full ) {
            continue;
        }
        if ( '' === $target || version_compare( $offer->version, $target, '>' ) ) {
            $target  = $offer->version;
            $package = $full;
        }
    }
    return array( $target, $package );
}

/**
 * The host a core package may come from (issue #395): WordPress.org's
 * download host, over https. The update offer names the URL, and the
 * offer arrives over Tor; a package from anywhere else is refused before
 * a byte is fetched.
 */
function op_package_url_allowed( $url ) {
    $p = parse_url( (string) $url );
    if ( ! is_array( $p ) || empty( $p['scheme'] ) || empty( $p['host'] ) ) {
        return false;
    }
    if ( 'https' !== strtolower( $p['scheme'] ) || ! empty( $p['user'] ) || ! empty( $p['pass'] ) ) {
        return false;
    }
    return 'downloads.wordpress.org' === strtolower( $p['host'] );
}

/**
 * WordPress.org's md5 list for *version* (en_US), over Tor. Returns the
 * array of relative path => md5, or a string saying why not (never
 * evidence of tampering: a fetch that failed is "netfail").
 */
function op_fetch_checksums( $version ) {
    $url = 'https://api.wordpress.org/core/checksums/1.0/?version=' . rawurlencode( $version )
         . '&locale=en_US';
    $res = wp_remote_get( $url, array(
        'timeout'            => 120,
        'reject_unsafe_urls' => false,
    ) );
    if ( is_wp_error( $res ) || 200 != wp_remote_retrieve_response_code( $res ) ) {
        return is_wp_error( $res )
            ? $res->get_error_message()
            : 'HTTP ' . wp_remote_retrieve_response_code( $res );
    }
    $body = json_decode( wp_remote_retrieve_body( $res ), true );
    $sums = isset( $body['checksums'][ $version ]['en_US'] )
        ? $body['checksums'][ $version ]['en_US']
        : ( isset( $body['checksums'] ) && is_array( $body['checksums'] ) ? $body['checksums'] : null );
    if ( ! is_array( $sums ) || count( $sums ) < 100 ) {
        return 'no usable checksum list for ' . $version;
    }
    return $sums;
}

/**
 * Check a downloaded core package against the release's checksum list
 * before anything unzips it over a live install (issue #395). Every file
 * member under wordpress/ that the list names must have its md5; a
 * member the list does not name is refused unless it is under
 * wp-content/ (the tree WordPress's own verify leaves alone); every
 * listed file outside wp-content/ must be in the package. Returns '' when
 * the package is the release, else one line saying what is wrong.
 */
function op_verify_core_zip( $zip_path, $sums ) {
    if ( ! class_exists( 'ZipArchive' ) ) {
        return 'cannot verify the package: the zip extension is missing';
    }
    $zip = new ZipArchive();
    if ( true !== $zip->open( $zip_path, ZipArchive::RDONLY ) ) {
        return 'cannot open the package as a zip';
    }
    $seen = array();
    $bad  = array();
    for ( $i = 0; $i < $zip->numFiles; $i++ ) {
        $name = (string) $zip->getNameIndex( $i );
        if ( '' === $name || '/' === substr( $name, -1 ) ) {
            continue;   // a folder entry
        }
        if ( 0 !== strpos( $name, 'wordpress/' ) || false !== strpos( $name, '..' ) ) {
            $bad[] = 'outside wordpress/: ' . $name;
            continue;
        }
        $rel = substr( $name, strlen( 'wordpress/' ) );
        if ( ! isset( $sums[ $rel ] ) ) {
            if ( 0 !== strpos( $rel, 'wp-content/' ) ) {
                $bad[] = 'not in the release: ' . $rel;
            }
            continue;
        }
        $seen[ $rel ] = true;
        $stream = $zip->getStream( $name );
        if ( ! $stream ) {
            $bad[] = 'unreadable: ' . $rel;
            continue;
        }
        $ctx = hash_init( 'md5' );
        hash_update_stream( $ctx, $stream );
        fclose( $stream );
        if ( hash_final( $ctx ) !== strtolower( (string) $sums[ $rel ] ) ) {
            $bad[] = 'mismatch: ' . $rel;
        }
    }
    $zip->close();
    foreach ( $sums as $rel => $md5 ) {
        if ( 0 === strpos( $rel, 'wp-content/' ) || isset( $seen[ $rel ] ) ) {
            continue;
        }
        $bad[] = 'missing: ' . $rel;
    }
    if ( $bad ) {
        return count( $bad ) . ' file(s) differ from the release: ' . implode( ', ', array_slice( $bad, 0, 5 ) );
    }
    return '';
}

if ( '' === op_socks_proxy() ) {
    op_out( 'status', 'netfail' );
    op_out( 'error', 'no Tor SOCKS proxy reachable (tried onionheaven:9050, onionpress-tor:9050)' );
    exit( 0 );
}

$mode = isset( $args[0] ) ? (string) $args[0] : 'check';

/* ------------------------------------------------------------------ check */
if ( 'check' === $mode ) {
    $current = get_bloginfo( 'version' );
    op_out( 'current', $current );

    $before = time();
    wp_version_check( array(), true );   // force — ignore the 12h transient
    $updates = get_site_transient( 'update_core' );

    // wp_version_check() returns void and simply leaves the transient alone
    // when the API call fails, so freshness of last_checked is the only
    // honest signal of whether we actually reached WordPress.org. This is
    // the distinction the old shell code could not make: it read an empty
    // stdout as "you are up to date".
    if ( ! is_object( $updates ) || empty( $updates->last_checked ) || $updates->last_checked < $before ) {
        op_out( 'status', 'netfail' );
        op_out( 'error', 'version check did not complete over Tor' );
        exit( 0 );
    }

    list( $target, $package ) = op_pick_core_update(
        $current, isset( $args[1] ) ? (string) $args[1] : '', (array) $updates->updates );

    op_out( 'status', 'ok' );
    if ( '' !== $target ) {
        op_out( 'target', $target );
        op_out( 'package', $package );
    }
    exit( 0 );
}

/* --------------------------------------------------------------- download */
if ( 'download' === $mode ) {
    $url = isset( $args[1] ) ? (string) $args[1] : '';
    if ( '' === $url ) {
        op_out( 'status', 'error' );
        op_out( 'error', 'download mode needs a package URL' );
        exit( 0 );
    }

    if ( ! op_package_url_allowed( $url ) ) {
        op_out( 'status', 'error' );
        op_out( 'error', 'refusing to download a package from anywhere but https://downloads.wordpress.org: ' . $url );
        exit( 0 );
    }
    $name = basename( parse_url( $url, PHP_URL_PATH ) );
    if ( ! preg_match( '/^wordpress-([0-9.]+)(-[a-z-]+)?\.zip$/D', $name, $nm ) ) {
        op_out( 'status', 'error' );
        op_out( 'error', 'refusing to download unexpected package name: ' . $name );
        exit( 0 );
    }
    // The version the package claims, and the one the caller wants: the
    // checksum list is fetched for it, so the two must agree.
    $version = $nm[1];
    $wanted  = isset( $args[2] ) ? (string) $args[2] : '';
    if ( '' !== $wanted && $wanted !== $version ) {
        op_out( 'status', 'error' );
        op_out( 'error', 'package ' . $name . ' is not version ' . $wanted );
        exit( 0 );
    }
    $dest = '/tmp/onionpress-' . $name;
    @unlink( $dest );

    // Plain wp_remote_get, not download_url(): the latter goes through
    // wp_safe_remote_get() -> wp_http_validate_url(), which gethostbyname()s
    // the host. That is both a clearnet DNS leak and a hard failure when the
    // VM resolver is down — and it is pointless here, since the request
    // egresses via Tor and cannot reach anything on the local network.
    $res = wp_remote_get( $url, array(
        'timeout'            => 900,
        'stream'             => true,
        'filename'           => $dest,
        'reject_unsafe_urls' => false,
        'redirection'        => 5,
    ) );

    if ( is_wp_error( $res ) ) {
        @unlink( $dest );
        op_out( 'status', 'netfail' );
        op_out( 'error', $res->get_error_message() );
        exit( 0 );
    }
    $code = wp_remote_retrieve_response_code( $res );
    if ( 200 != $code ) {
        @unlink( $dest );
        op_out( 'status', 'netfail' );
        op_out( 'error', 'HTTP ' . $code . ' fetching ' . $url );
        exit( 0 );
    }

    // Sanity-check the payload before handing it to `wp core update`: a
    // truncated or error-page download would otherwise be unzipped over a
    // live WordPress install.
    $size = (int) @filesize( $dest );
    $head = (string) @file_get_contents( $dest, false, null, 0, 2 );
    if ( $size < 1048576 || 'PK' !== $head ) {
        @unlink( $dest );
        op_out( 'status', 'error' );
        op_out( 'error', 'downloaded file is not a plausible core zip (' . $size . ' bytes)' );
        exit( 0 );
    }

    // The package is the release WordPress.org publishes checksums for,
    // or it is not handed to `wp core update` (issue #395): until now the
    // md5 check ran only after the install, report-only.
    $sums = op_fetch_checksums( $version );
    if ( ! is_array( $sums ) ) {
        @unlink( $dest );
        op_out( 'status', 'netfail' );
        op_out( 'error', 'could not fetch the checksum list for ' . $version . ': ' . $sums );
        exit( 0 );
    }
    $problem = op_verify_core_zip( $dest, $sums );
    if ( '' !== $problem ) {
        @unlink( $dest );
        op_out( 'status', 'error' );
        op_out( 'error', 'package ' . $name . ' refused: ' . $problem );
        exit( 0 );
    }

    op_out( 'status', 'ok' );
    op_out( 'verified', count( $sums ) );
    op_out( 'path', $dest );
    op_out( 'bytes', $size );
    exit( 0 );
}

/* ----------------------------------------------------------------- verify */
if ( 'verify' === $mode ) {
    $version = isset( $args[1] ) && '' !== $args[1] ? (string) $args[1] : get_bloginfo( 'version' );

    // A checksum fetch that never happened is NOT evidence of tampering.
    // The old code piped this straight into "CORE FILE INTEGRITY FAILED",
    // so a DNS hiccup at boot wrote a fake compromise finding into the
    // user's security report.
    $sums = op_fetch_checksums( $version );
    if ( ! is_array( $sums ) ) {
        op_out( 'status', 'netfail' );
        op_out( 'error', $sums );
        exit( 0 );
    }

    // wp-config-docker.php is shipped by the upstream image, wp-config.php is
    // generated per install, and wp-content is the user's own files — none of
    // them are core and none appear in the checksum list.
    $skip = array( 'wp-config.php', 'wp-config-docker.php', 'wp-config-sample.php' );

    $bad = 0;
    foreach ( $sums as $rel => $md5 ) {
        if ( in_array( $rel, $skip, true ) || 0 === strpos( $rel, 'wp-content/' ) ) {
            continue;
        }
        $path = ABSPATH . $rel;
        if ( ! file_exists( $path ) ) {
            $bad++;
            op_out( 'missing', $rel );
            continue;
        }
        if ( md5_file( $path ) !== $md5 ) {
            $bad++;
            op_out( 'mismatch', $rel );
        }
    }

    op_out( 'status', 'ok' );
    op_out( 'version', $version );
    op_out( 'bad', $bad );
    exit( 0 );
}

op_out( 'status', 'error' );
op_out( 'error', 'unknown mode: ' . $mode );
exit( 0 );

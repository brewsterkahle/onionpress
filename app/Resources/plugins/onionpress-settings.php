<?php
/**
 * Plugin Name: OnionPress Settings
 * Description: Admin settings page for remote OnionPress configuration.
 *              Reads current config from the shared volume and writes
 *              updates for the menubar app to pick up.
 * Version:     1.0
 * Network:     true
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

/**
 * Run a curl request through Tor SOCKS proxy to archive.org's .onion.
 *
 * @param string $url     The .onion URL to request.
 * @param array  $opts    Extra curl options (CURLOPT_* => value).
 * @return array{body:string,code:int,error:string}
 */
function onionpress_curl_tor( $url, $opts = array() ) {
    $ch = curl_init( $url );
    curl_setopt( $ch, CURLOPT_PROXY, 'socks5h://onionpress-tor:9050' );
    curl_setopt( $ch, CURLOPT_RETURNTRANSFER, true );
    curl_setopt( $ch, CURLOPT_FOLLOWLOCATION, true );
    curl_setopt( $ch, CURLOPT_TIMEOUT, 30 );
    curl_setopt( $ch, CURLOPT_USERAGENT, 'OnionPress (+https://github.com/brewsterkahle/onionpress)' );
    foreach ( $opts as $k => $v ) {
        curl_setopt( $ch, $k, $v );
    }
    $body  = curl_exec( $ch );
    $code  = curl_getinfo( $ch, CURLINFO_HTTP_CODE );
    $error = curl_error( $ch );
    curl_close( $ch );
    return array( 'body' => $body, 'code' => $code, 'error' => $error );
}

/**
 * Request an archive.org path via Tor: .onion first, clearnet-via-Tor-exit fallback.
 *
 * @param string $path  Path with query string (e.g. "/services/xauthn/?op=login").
 * @param array  $opts  Extra curl options.
 * @return array{body:string,code:int,error:string}
 */
function onionpress_ia_request( $path, $opts = array() ) {
    $resp = onionpress_curl_tor(
        'http://archivep75mbjunhxc6x4j5mwjmomyxb573v42baldlqu56ruil2oiad.onion' . $path, $opts
    );
    if ( ! $resp['error'] && $resp['code'] > 0 ) {
        return $resp;
    }
    // Fallback: clearnet via Tor exit
    return onionpress_curl_tor( 'https://archive.org' . $path, $opts );
}

/**
 * Convert Archive.org account + password into S3 API keys via xauthn.
 * .onion first, clearnet-via-Tor-exit fallback.
 *
 * @return array{'access':string,'secret':string}|string  Keys on success, error message on failure.
 */
function onionpress_fetch_ia_s3_keys( $email, $password ) {
    $resp = onionpress_ia_request(
        '/services/xauthn/?op=login',
        array(
            CURLOPT_POST       => true,
            CURLOPT_POSTFIELDS => http_build_query( array( 'email' => $email, 'password' => $password ) ),
        )
    );
    if ( $resp['error'] ) {
        return 'Request failed: ' . $resp['error'];
    }
    $data = json_decode( $resp['body'], true );
    if ( empty( $data['success'] ) ) {
        return $data['values']['reason'] ?? 'Login failed';
    }

    $access = $data['values']['s3']['access'] ?? '';
    $secret = $data['values']['s3']['secret'] ?? '';
    if ( $access && $secret ) {
        return array( 'access' => $access, 'secret' => $secret );
    }

    // Fallback: fetch S3 keys separately using returned cookies
    $sig  = $data['values']['cookies']['logged-in-sig'] ?? '';
    $user = $data['values']['cookies']['logged-in-user'] ?? '';
    if ( $sig && $user ) {
        $resp2 = onionpress_ia_request(
            '/account/s3.php?output_json=1',
            array(
                CURLOPT_COOKIE => "logged-in-sig=$sig; logged-in-user=$user",
            )
        );
        if ( ! $resp2['error'] ) {
            $s3 = json_decode( $resp2['body'], true );
            $access = $s3['key']['s3accesskey'] ?? '';
            $secret = $s3['key']['s3secretkey'] ?? '';
            if ( $access && $secret ) {
                return array( 'access' => $access, 'secret' => $secret );
            }
        }
    }

    return 'Login succeeded but could not retrieve S3 keys';
}

/**
 * Base64-encoded OnionPress menu icon (favicon.png scaled for WP admin sidebar).
 */
function onionpress_menu_icon_base64() {
    return 'iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAjWElEQVR4nMV7CZhcVZn2e+5Sa9fSVV3V1Wv1lt67s3ZICBBCBKICBoYOCiIiLuA4Ioz+MgOaNCoojjgCCiqLI5vQLLKFCCEkZA/ZOul0upPet6qurn29+/mfWwkqOvObhPDPeZ7u1K3cc/t+7/m2837fIfj/PNauXct88Lmrax2le8HdfOs9F47sDSpxMTf6yRuWBbqevFGA9vdz1nWtowRE/0jP1vuQs/Wg/+HZ9PWnDxXueG1HrYUlU3c+NT1LSJfy5zsY4Mb2H36yuqn0tfaVNWosnpqKBRKDI4cCo7Mj4aN2R8H77Teed/Ab31iR/uu37aSdbPPaZtrV1aWdjZf82AallHzt3Hsfr20uu9LosiYnB4PpXDwzxhtoDwx0Z06T+8QJ7b5/vv+zqxdd0KCpxzVGKlCRyGUwFZjB6NBkemDPRCg0Ft3PseTVg+8c3/6O+PAYGKgnNKST7ezsRHf3GvV/FYC1OKmidN2HVPN637cthnJHX9ezN1eW13DIxUWksgaEjqYwsm0Wb726VVRsIn/f+n8hlueMhPQqGlPGU/hYDaVgUQKGlgGhVBTHDoyid89QJhpMvD2wY2J9OBTZ8mbsl8eg/sVMTpoI/d/RAPL3lnnL+XdWMKyj/54XbjLbJ39NkQoADj+EQDERpxvxi18fJO5LHfjn2zsh/IdAGYEh+kOoDEClUAsItdxmpJm0gtyISszVLEnLGYwcm0Tf4WMTB989ti0bU54tt8pvdm3pUvR3OF3zIB9V7rVrnzckd07Mk1W10Whm6zTKFBMVTkIZdvxooLi2o+q8e35/NcjuLlAhCFniMdPDYaDHjse3svjWK19FB9+M5DMiWPNJ/8gAqgDw8xkYr2Iw9U4SqXEJVAGshUbVVmainJtyKZLCgb2Hlf3v9h8JjyceOrZ37NW3hUdCulZQUHIq2sCdqeCd6GS70a0G3j7WHgumd19580o0zK2A3WaFhTMAw0Y8cv/LsBYbKaE5ogk5MMQEWTQBqgVTIcBW60ZDbSVSL6sQFALu5Hrov1VWg6mVIBOUIGcoTEUcNJkiJ4hs5qgAjmc1g4WnS6uWcMvu7Jjbe/jYb3e+dejbnm3uR1fffv7j5Csk+t/r5YcHc6YANC9vJsuXr+VqWj0hRVX7GheWawsualCs4UIt12vQwv2yFgknNXepk0BOgkpZUFFALpRDJkZxfEpBTUc1HM4CqHYNpISBRCgESUM2QUE9BFwlQWZchipoUHMUVAMMVhZGJwvCU0ahChsdz9CZjQKdX9eufuO+zzbc9O+rf7rpdwc3f+eK+xfpwq9dS5mPQwNI3uYA7DiA8Qtwy45tbxxunr+oUUslsowoUGQkCVlFgMNlABw1YNpvA5FjMMkJiANJhOReLF1SCzmnge/QYLvQACISyCGKzIAGcy1B5HgWkaM5mF0cWCPJgyBnNOiazRgZQHd5LCWmMgaUymyBxaz6a72MLIulnMUknYqJc2ckPgP60L895d/41P6VdrttXjosrDi08xjNaTnG01CA2f4M0iyFRlUUei0QA0ZM7m+C0cqDUzjMZAehOCfRPL8c4jTFyMY4OAuTX12Lj4d1oQGmIgMMihlMjiAxIiA5JYEYCSxFHDgDCympQRFVgAfMBhaOMjMd2T7B/uBfH8uyJvaae//wrUPA8yy6PhyZPioAhOEIvW7OXfdP9iTW3PDty8vqbbUY3hnC4689px3vHSO1jjpIGRWZlEglRYLVYYKS1ogQS0JOMRAnCA4dPg57mZmWlHgQ2pMhrI2AL2ChqRSxIQGhnkzecs1uDq5qMyqWOUAIkJ6WMHssi0QsB7OHhdnDQxMpyhrtCI3OknvueALhsRTrbym54Nq2O0ae7Vsz3KX+xV+dDQDyQ8jKjL3QXrb6KxdquV2qljnMMWYUMD07j9H2LzSCNxBoigrCE2o0G4kiatAUAoYwEFIaArEA/OeVEJPNBGcpwMgEuVkZUk6FxlDwDpJXcVWmmD6UwuT7SRgsbB6M6gudICpB8FAKsYksGi/xQkgJ+NG/PU65lAk/vOVfDdPS9Pf3jh/8ly8Yv/9LhSq/enrfPQGcLSeoKRTPj957586Ne/e+8pstDO9jibmQZ0odJbR35zBRNRUFNhNUVSOUqIyB50BVCt7OwmTjIOVkRHMRzGmvgDylYnDjNBQlh6ImA6qWOVDZ5oTDYQbSgBRXoVF9LgMYgGB/Gj3PBXB8SxiuWhNaLi0GJxE8dP+zGDowQ86tupBw1Ew+dWUH/T9dXyj84q2r75rT4H/76yvv/QzD5UUlHxUAuhaUIQzJlNUVffeZX24QRyZGmdImB60q8WNsIERnpsPUaDBCySlgGAKWZSEmVYhRBUoSCM/EILBZ1DSWIdybQd/TAez52SjeXXcU2+4fwMCmCUhSDt5GM2oWO1FaZ4PRwEGKK9CoBnMxDyEpY3RXDIzEoLv7bbz16vtYNe9SWl5eTGO5JD28I0hMipFeeO18edU157aEh1J//M6lv7gQRDesv2zGzkgD1ukg0LXMo3vWbsrEc4/9/sE3iaFYpXNq/boa08Fj43D4DFChUko0jWUYcEYC1kSgSkA0EYfBQVBa4kFiMgPCMtAUDWJMRfRoFoOvhbDzgWFsXHcEvRvG4ag0geGAIr8FPn8B1KQKRVXQsKAY+/Ycob99+GUsrliK5ppGQjmVFFaaSN1SN8BC2/DANv6Xdz5POSvz4MJFcw7pexM9Yf9IPoB8kF2JINfdteJHj31vw5Wbl+72zV3YQR2vFZJDB45hSd2ivOpqhEKTKISEDLOTQ3ZEQzA8C0ephdpNNkLaeJx7Ww0S0znER7LIzIjIRKR83E+HBHAGDkpOhSJpiE1m807SW2NFodOF4ESY/ucDT6HM5NeWz11KBElAVZuT+Oe6QFlZfeg/X2Jf6X4v5atyffP5I/f87pkTmfHfJUYMzmzoDyHXfWv1tKvY9vPnfv8O0YyC2lbTgIGDY0RWVLAcQ0ApqEKpJmuQsyqElIJQYhbeCidhwCB4LIVMVIOnqRDzr69G+3V+LP5qNdquKUf9p0tQv6wY4/timBlK5gEscBkgxzWYGAOefOFVEjiWIhe3f4LIskaq2wtp/TwPYumw+r1//w370nPvjDSdW3b504d++DtF1F0r/W+zQuYMAdB3X6AqyIVXL/jDxMzUxBt/2sxVl/u1mdEYjc5GKEc5qKoCKlGkQxJiYzkkZrN0Nhmm5dUeqFlASCsQBTUfz+PTOcz0p2F0GuHwF6DugmKYbUbUzStC02IfskEJ0ck0vIUubHh7O33rrV30k3NXUStvQ1ljAWpai5hDAwPKd7/9S/bgoaO7Lrtx8SUPvn7nFqp0srrv/p/2BdyZAnBit9XJfvOn105c7Ln5lTc2bvnGnKoqLTqbYkMTcc1hsRFF1kCSBA6jBXExh3goRTJyhpaVeqAlKPTwaPeZYHLwmNwXB8MzeXPIxmVYnDwiE1kkgwLcZVZUtbghzhAMHZ/Cf/3xJVJvb0aZoxKeahOqmzx02773lZ8/8CyvsOL6+7o/d8OiFSvC/6/4/5E1QB/LlzcTVaKoai5bn9FSorXMwNktLm3jhl1MeCYJVQFJh3I4MnxYs1tMVExrkCGS4iI3srMSZFFBfDKbt3l7iUnPLxAZz0JKq8jMSsgmpXymN3RwFttfGYGU0fD6rg1UDFM6v2IR9ZQXoK65GJt2b9N+9OPHeM2orP/Nrq9/Xhde36f8I+E/MgCbN6/L/4GH37pzhyjmAu5CF7l80VV0954+bDywBWajkfQeOU5+/OBvyeZde5BMZsBZAafRnk96OMKBA4/ZwTTEpIICpwEGK4eyZgdUTUMmIUDJKlBkhVb4vLR3/Cje2/U+aSvqIOWlPlLX6sGmA1uU+x9+iuUdzMafvPG1z/v97TGdHNlycq/ysQJASN6uiNHMJ1SFhrJqGkWFHtLqXYADU3uQyEWxfc9+fGLVYoylhmnv8AAK7CYYJCNSUxIC02EMzQxDiKiARmEu4JGaETB2eBaQCczUQgPDcaokCaFUI3/c+gZ1a+VYXLsYdY3F2DWwW3nw0Wc5k53b++Ufrry+vb09psf50+EKGZyFIeZkwrBczmDiAZaiyFQGp8OG6fAM4tEM/eQ5n8KCc+eQA6FdMPFGqkyYMdw7jRc3vIYX33hdO3DoiKYkCKb6Y9CoAmWWpalpgR4c3odgZBrF1mLsPLYbwckY6ag8Fxk+gu0jW9VfPfU0yxgRuPhzi7+w5sY1weVYzgGnR5RyH1H2D0ILQ6nmcthsYMIMoLFgNB4WO4/LL7qEbH86RDrWNGPZ6h4MbJ0lLzy9BTsmt+BTnzkfWtxINu59G35fObWqhUSMCxiNTWHX5Hs6+UHMxAq/vQ5v730XmgI6nD6CoX4FPZM9RFIU7ZxLm796xwNfOaqrfVfXqan9WdOAzs5OfT5Zd8OvWu0uS3lVVTmEiEIEJglrCcENn+2EMVxBAuNR7H4xiCuWr6aNS0vo1pm38dmbVqJQmAN5yA2vzYcHnnkUe/YcxYYdm/Ba/wukqX0OOa9iFdKZLF7d/xomQ9Pw2DzEb20jRtggCjJTVV/884ffXPs6aCd7phQ5c6bC64lFd3eIGMwcffvlXV9ftKylsNxUo44PhUlRixWXX7iaFmda6exkGq1LfKib48aep6LkvIZLyVc+dwP4yUqM700gHhbIgsLlxOm2YcPkC5Srj+DrX7+WmuN+ZGYpFEjYO7YH59YtwwL3JeBgVAdmjhCTgx342t3X/lhTKFmL5jMulHBnKjwBYRgeykrvN691FhpuvPziS7X9LweZ2XQMxUoJirJOYvPxSMRSaGjzwmox4mhPEIUoxOF3A4jORLF8RSMO9UyASEZc0XgNDbMBUt9YgWN/ipPe96fRUusHR4yY461Hk30ZJqeSIM4QiSsxWuR3/vaSq5dE9FjfBX3Xf2aDOd0Juq3pWRVrYNTLq279osdjfeyznVcaJjdREgokyMJLytHS5EP9/EIUeFnYnWY0LihCLJ6BvdCM9iXF8JSa4fJYUNFgQ12zBzIroqGunPEKVeTdJyfA5lg4HWakMyJKbGWocbRicjINs5mjyVyYkTUxcvUtn3iNapToFPiZCn8GAHSyXXd3aZRS/qra2+6tqip/bNX5nzaqR92aklFJ2ye8dP7iMrh8FpQ0WRCezmDhRaUwu1kkYwJ8FQWwlfKwOY1weaywlxpR0+aCtZAHY1PQvKAITrcJ3uICVFa4EU9l4OCL9YQKkiTDajbQlJjQc4TgF75x5ZCujB+1PMad+q1rGTBd6ncue7D0uvbvPtKxeN7lPrZWM8ecGiEa42400vqmYmJkOfjmWTHZk4C3xkL9C5xEEjTkEgrmnOMC4wSsDh48w6LAy4E1W1GVKKSpVBqNTWWoa3aT4GSc1tZ6kc3KBAxHZxMxmKASE8tB0VSwBiZhMPLqqdDeZ0UDOtHJgunSvnTe3fNDU7NvXfKJiy4vyjWo3gIXnF4Dw7ooaWwpJmYrD2+HGbKmQOMo6s5zE2JHnsMzmlhULXLkIbfYjSiqtMBQyKDAzaF+kYfoXC9nA5m7rBQqqxKrzUgq/A6UFReRMkcZkWQRLMvDZDDrVJlZEuWzUtViTkF8tpvtVm84599WUkF+66orVrVIR11qTZ2LLamyMzPBNGlbVAp3qQ3FF1nA+hgYCjmULiiA0c0BBcDsaAZlcx0wVxgAjsLi4FBUZQGsAMxAYaUpf52VRfgbnZjT6kE4mUBTmw9pUcC8pho4bBYIggKX2QWWYYuPHBktPsH7f5jhOasArMVahrDd6pfO6VoBhX3+ms9dUTS5mai18xxs3QI3erYHsPQzfpQ0ODDFD+Ohnz2Bh+57AlPRafBFHGDSeQAFikhRfYEd4Gme2zMWcLBXGPPaQIwU1AiUtzmhEAW8HZi7tAxpOYcCrwFlfici6SQ6OvyIZ9KkzFZNbUa77+4bf7FSN4C+vj7ysQCwVs+pmS7t1k/d35KJZZ/5/Jc/4xp7iyq+OjO7bE013ntuhM77lI9WnufEjqHtuOn6O/DW+m2AheKRh59EcCYI2AiycRG++RZY/Twoo4JyKkxuDuZSDpTVwDgZkALAXsmjYl4hYCAoaSyAt9SOqUgEHRdWIp3LwWThUFVrJ6xiU5tcLez0WPR6qlFjd3d3PiyfbQBIF7qw/vX19v79Q/+15uaLfeH3OU2WBG71d1ux55UJ2HxG0nZlCQkEgvjPe57A+EQAF12xBN+460tIpbN4+82tgJnAVMrA026EntLAoEHjNDgbeDAFGoiN4Pjocdx99/342s134MmXnodoEsDYCJoXeRGcisNaymL+Qj/6j0+jvs4HGRLX4pmvFTHeT/xTx21rQKCuw7qzC8DatWsJyxHtwZveuGvFZQsWOmipcmTLJLn81haEjwoY2hdBx9XlAEtw8P0jGB6YgNvkxOtPbcKt13wfOzbsRzQcy/tn3qjHGg1gTvwwBgpzMQFjYjA9NYn7fvRrtF/QjJYl9ej63oP0kcefpDpIZS0O6JT62GwUzYt8sNmNGJ8Mo729FOkMyDnl57KxidS9P739N9VdyIdmclYA6MTz+bz6K8vvme/0WG5ZdlEH3f74KDN/VRkpdbiw//UJ+JoKUDzHDn1R07IIRaYwGwwIBWN4u3sHEtEMvD5Xvh6gaXq00kO1hnx1E9qJ78xs3mSOHxlH+6JGFJUUwgQjee73b5Aj/QNgCxlUNbgw3DsL3sNg3nw/xqZm89Vnb4mVGJQida5rftn6P+z+GaWUJfre/AxAYP72i26sofqDjh8Y/eryTy8uCB/W1ExKZBYtrEZ0MIfARBL153mBRA44GkWTwU7rSkuoW3agCLY8G9w4rwoXX7YMRNUATRf8g1BN85+pquZBCAXDCE1E8KWV38bP73gCRrMByUgae/YdyDNB/iYXMiERMTENf40LHq8dfcemsKC9GoIiMXMKWzU3cV+5ZsHtnyMMtDVkzWlHBObDl2sZEGhfXLLO7/CYrmppq6P7108wc1q8cBntmBqOgZoISmkG6N4N+sc9aB3MkR+tugRLO+q0ltY5uPKqi/CjB27H0MA4Jo6PgSu0nVQAHQwKQik4kwGMgUWR15XHJh3JwaTwcBisIBpBLBLXkYLTa4bNYsbURAxWH4+WtlJMBSJQVA2N9SUkHlPQUbYY4enE9596+NWiExTY6YVF7sOXXSAsEA5EOhpaK71amtcSMxlm6cI5kNMapibisHvNMIWjeuMOSIE5P6ut0Esa1tSR+Io5cLnt4IwsvvhP/wpJlOh3f/xV0tZSA8ZoOKEJioa923sQnI7i/Is68OKct0BHZLhtTkiqCtVCUd9ST8AREB75tHh6NIm2Bg3+KjfsdjP6j01hbmM1evsnGAdTpFWa/HP+8NCbN4HgJ6Bd9KOkwlTXUkWTV8xpqaSzg1nwBhY2kxWhsSyisSxKPYVAswt0JgkSz+YnqUYWhrpyeAvdeb+QSCSgiAr2bu8jt675ARoX1KKyugQMIQhOzmLbOwexaEkLLnvmYnT9x7fw/C/fgDyZpg3uQpT65uIis4dgMA7YnSjy2TGwLYR4LAcDMaK6pgg9+yfQMsePugofBoanSGtJq/bqwNDXDwyP/Hp+dXX8IwGgKZRZUfTl+b7SIhLal4HRwsFiMCEazCGVEFDA84CjEFi1CDQQy6s2W+wAddtBkxoIR2DhLHA67WAZDrG4gO3rD2A39kHU7Z9hoWkKqmrLAZVg0Tnz0VxfR5Mv7YYvxxAwLNAbBD0aAmmqhsNVppfXkUzm4DWYUOl3YfeuYUyFwqguK8GB/mHiJS74rCUVd199/9UAHu3s7GS7u/8xI6yPv7aXvAft74cVFMVWqxm5pAwjx4GhLISMotf8wRs4yEEKWmABqS8DaapAlrFCS2tgVAZUpOCNPJpbG+BggSuKC3FrQyW+01KL25trcb7PDavZiPkLWgGVgRJTYCEW4itwEpqToEkywLEgCgUODsGYy4AlPJKJLBgD4HIVwGLlMTkZgYEzwGY1I5mU1WpnDQkORy43mDl0d3fnyVqcDgD05KZq5PBWEzTNyDEsKNE9NgVRka/PSaIChiWQYxRKVAVkCjEsY3hrKO8j8s5OBJABLr1kBeY2VdJVHg+9qr4BS4pLsNTpRAfH4oIL5mHpOR1AgoJVWFDKQGvzA64CMLICCDIgy6CFVrAWC1jKIBkVICuavg+A02VGcDaGTFaCx+5EKBljPOZiWI3m1l/d/Tu/7nL1XOa0TICc/Ley3KXKsqIpkowChwHBXAKS3uygp1yKBkXW0SAQppV8S4te7IgHBR0oChmE0RjQNEVteQWuufVL2PLkK5RIoq5SpCceoRP1Jfjm7TcTO1cAmqUgemugAhCvB1hRAATDQFoA5XjQUg9o2gKiUb1Ikl9VQgnsdhMdGAggk8vBZraSrJAlZtatGajJ//LjO1oAjPV1ndoegfsrC8irQMvSlrSsqfFoNFHuqyql+zeMk3RaAM9zUGUN8UguX65WMoA4pSGXlpFKiHrXBtEF0Xv0CEugJTVctGQZKS8up3t27qZ9yST1nd+Gb56zhBQ5nHmQ9PugN0XqQ29pMpuRdpfCUMrCUADIEUDS+wo0hcpZQvVapF4hZjWW6GRpJiPAwHGQFJmA8qoFNi6aDlWCxSnUhP4OgBODN7DSEsd1wyODgdbPfLodIpW0mWCM+L0lRO+ymB5NQMpq4HhASVJkQhLi0SxUUQNMyDcz6lDq1V+aoKgv9zP1a/w6MDSvRiKgpU4YnJiToXMIOhmgR0iiUgxsCsK/wIkixgphWoIiANmMRMwSA6IzQzkViagAURKRyQow8TxkVYbemWJiTHr/oJdlCVQ17wf+4WD+9lovSXmK3Vt79w+hqMxMS+vtZHRyhui2pzuc6WAE4REBclbTHTbVE5f4bBaJKTG/+vmMV19VGaASEOhLQQ6r+ooTvSCq5Wjej8SnBYwfjOeF0iRdeCAbUzAzngTNAeKkBi1HIMkK4okMzDwPvQ4ZGEkjGk1DpUo+sxZlBaIqgYAB0YvuHGPXe5FOlSliPny5Nv9Qj8e2dWhoXB0emyQXX9lGRmcmkMkJ8LlcmI1EEQ6mMNsvITEtEaOR11UQkakMVUKAmtUTiRNAiCkFoz0xvYyez/CIcsLe9VebHUtDyOR7WkHyvcHA+KEo0nEBRskAKaWBoQTxcAbxZBIFvJkM9cQQnsoinknl22909c9JAhRFzucYOUnvRtXd9qnnQsyHL7vyc3/97s2HeY7Z9uKzm5jFK2uVikYX+kaHUO7xgCoajk9MUCKzNNSfo7mA3gGm5/VxqqQpclMqxFktb9OZuIRMTASrMfnrvHnoIGSBmfEUDEaS/z4XVpAekNG3MwirzQAeHHJRGclJCX37AtAUBUzOgMi0AFEVEIrPwmQywGIyIZqJ54XnWIKUlIQiI6bpafcp0n3M31xTvb7GsGVZn9/z5I5NB7Hv8CFy7U0XalEpogmKgKrSUvT0DVIdbaIyRIkRWFgjBo8GSWZWBhUAJUYhTVHER3LIpaW8mp8wC5oXOBuSMTORAJdjkO6XkRmUMTuSwfj0rOYyWml8IIfoQA7JaRF9QxMoctkBiYXebzQamUQ0FYPb5siDMBIag9tWCInmSFpJ6mFiSssXyDrPRAOAzXSzSjUwD/fc2U2Juv+RX7zA2msJXbFiAQlnImiurkJWkJgDR48RPSpoooaSIiedCkQRGEsgHZSQi0vQcic1ICHQ7KSK3LiC3LgKYULF8P4I4pEMLJoRYkwBz3IYmJyikiSTQqYAqRkRHGURiscwEw2jusQHTWCREOM4MnYcGihqSkoRTsxiMhJEja9cm0qMswonJRray/t1LT418fH3AOjb6rVYiyJSlFy8ouH7vfuO45HHniRtlxbD7yuF3VyAxa0N2N13FBPBEOIRAQ6DnUiijMHxKaokKBLjIhITAjJxAUJOIkpChRxVIccUqGmK4aFZmA08dAo9m5ARnExg78FBVBW7YSRGoiiazhpgd18/nA4Liq1eZAUR+8cOIRANodRTBJ+zCDsHDsJqMMNX6NSOzvRRwqLnFy99/5AuRje6T6lewPx3X+oMi84JPrhh3Ru1bWU/e+nJrcyL7/1RabzMTWWFormklpaXuOjWvgPISgKIyKOwoID29A8jmRXy3VxiSoWQVJBIZKju8TNROd/6EglmMDgagIM3IzEpIhdRse/oEERZIi3+KiIrSj7d7h0fxVBgEvNq6ylkHvvGenB0bDCfjyyqbcVQcAKDgXEsrG3DRGqInUxNkPJK78uEEHE5lut9QWcUBv881mEdVSXKXP+DlV1FZdYNv/rpi/xr+19R6y6zUkmlpKNqLmRG0nYN9VDCMKj3+ZlALEp6hkaRTatIpaU8BZAVBJJNS3lA5AxwfDSIWCqFElehXmBDPJfCvuFjdE5ZORWyQCQuYmAySP+0dx+tK6ukHpOXbB3Yg93HD0LSFCyubwXH8Njcuxe13ip4nBZt8/FNxGBljncf7HpCX/33yHunXCZn/qf/ONlVRVevXp267Wdfvs5eZNn80E+6uac2PaOUrQItLneT8+s6mJlkhOwZ6YHP4UWNtxS7B3sRy2SQzSpQJYKUmKXhZAZpQUFOVtA7MaqXuMDChNlkGut73qcs4YiTKyKBUAZTkShe37uVFBbYSGvZHLLl6G5sH9iPnChjXnUzqt2VePvQbjgtTiyqa8TGwfU0kgtj7oLafyfElcintH9moD4CACdHvvlh1Zpzo9/6j6s6iysdrz756Jv8PY88AKVhQl1wfhUuW3whJuMhbBvaTxfWNkGDiq0DPUhkRcgyhaQqJBCJI5VSMDgRwrGpSbgtRQjHRGw6egAT4RnSVt6o56CI5KJ4s3cL7BYrFta0YuvAPuw8dgiqCsyvaUZNURX+1LMHZsaCZY3tdNv4Rrkv2M/WNpY/8Pj2+144WSQ5LUKEOYV7NL00dsV1V4TXDz/62ab5Fffu3N5Lv9N1H/vG0Auaf7FJu/7Tq5CRs9g9fIieM6cFU8kAjocmIIksqEpoMB7VaUAcnhqCxWREpascvdP9ODYzjCW1C2E3OzAYHqLvDuyk5e5itFTU4d2+Pdg3NAATb8HiunZ4bMXY1n8YRQUetNdWaRtH/kj3TuzlK+s8L9z90C13yqL6gSwfz6kxevIQEssT3PLJuz69/71jP0knpJa66jJcccFKpaVoLjlwcJTMzKQJpQoJJiJ0ed0SjIUDJCmm0FbShM2D27G0bj7CqTj6A8exqHI+DKwJfYEBJMUkrS32E70SvG+4D1kxhwq3F35vGdIZFdORCMo8LnDmlLJ1+B0ulJtBdUPpb17pe+RWQohwpoVScjo3nwRBn6NRGnVc2XbXtwKj0euFjFRbWerD0sYFGp9zIpWkdDaRJDw1kXpfJQmmgjDqu0koMHEWhJJRVLsqkMplEUiF9H08HEYHJsKzmI7P5pOcskIvOGJCMJKg8Wyamq0iknSUGQwPgLcx0zWN5T948fBDj8iCcqKKdZrNUWcEwF8NXd00/ffPv/dE1daX9/1TaCpyfSySmusucMFldkOTjBCyHPVaS1Ujb4CkSsRqtBCGEOK02CEoAtFpfDNrpdmchHA6TgwcTwsL7OBYA01lUlowEURaDXMSF0FKmYXMCkpppe+JRctrf/7DJ+44Cu3P73/GJXJyphNPzNXj7RaFcMDLL2yz7ezeubB3z0Dn1MTsCiNnKGE0zqnkdErNCB7m/IpauAL94A01GyzUABMVZJno7e+8gQFhVEaQ00iJcYhIgfASNKOoqlQeL650r1+wZO5vf/z0bT2KpG87O09j1//xAJAfJ9vT8kf9/urMMP/jbz/cvnP9wY5UJNPE8EwFBcplSSmRBLlIk4mJakR3kHmDIoSAEhWUVShnZOMms3GaZcigqtKB2ubSnY+9c9+7hCGJD9b5o6j8x3l4mgAf8HB/eTmdyNRPkGx++XX3Cw/uLAoGki5BER0MQ20a4UwMS1moOhtA9dMTCaudj849r3X2m+s+P6WfOcpvn/NDD3H6YYfTOxv8j8b/BQwQpm41qmmwAAAAAElFTkSuQmCC';
}

/**
 * Register the admin menu page.
 */
add_action( 'admin_menu', function () {
    $icon = content_url( 'mu-plugins/onionpress-sidebar-icon.png' );
    add_menu_page(
        'OnionPress Settings',
        'OnionPress',
        'manage_options',
        'onionpress-settings',
        'onionpress_settings_page',
        $icon,
        80
    );
    // Constrain the icon to match other sidebar icons
    add_action( 'admin_head', function () {
        echo '<style>
#adminmenu .toplevel_page_onionpress-settings .wp-menu-image img {
    width: 20px;
    height: 20px;
    padding: 7px 0;
}
</style>' . "\n";
    } );
} );

// For multisite, also add to network admin
add_action( 'network_admin_menu', function () {
    $icon = content_url( 'mu-plugins/onionpress-sidebar-icon.png' );
    add_menu_page(
        'OnionPress Settings',
        'OnionPress',
        'manage_network_options',
        'onionpress-settings',
        'onionpress_settings_page',
        $icon,
        80
    );
    // Constrain the icon to match other sidebar icons (same as site admin)
    add_action( 'admin_head', function () {
        echo '<style>
#adminmenu .toplevel_page_onionpress-settings .wp-menu-image img {
    width: 20px;
    height: 20px;
    padding: 7px 0;
}
</style>' . "\n";
    } );
} );

// REST endpoint: trigger analytics upload by touching the trigger file
add_action( 'rest_api_init', function () {
    register_rest_route( 'onionpress/v1', '/upload-analytics', array(
        'methods'             => 'POST',
        'permission_callback' => function () { return current_user_can( 'manage_options' ); },
        'callback'            => function () {
            // Touch the trigger file the menubar watches. The menubar's
            // handler itself decides whether to run the upload now (if
            // online + ready) or queue it for when the service comes
            // back online. Either outcome is reported to the user with
            // the same "queued" wording because we can't cheaply know
            // the menubar's state from here without more plumbing.
            $trigger = '/var/lib/onionpress/.upload-analytics';
            if ( touch( $trigger ) ) {
                return new WP_REST_Response( array(
                    'message' => 'Upload queued — will run as soon as the service is online.',
                ), 200 );
            }
            return new WP_REST_Response( array( 'message' => 'Failed to create trigger file.' ), 500 );
        },
    ) );
} );

/**
 * Handle service control actions (restart/stop) on Linux.
 */
add_action( 'admin_init', function () {
    if ( ! isset( $_POST['onionpress_action_nonce'] ) ) {
        return;
    }
    if ( ! wp_verify_nonce( $_POST['onionpress_action_nonce'], 'onionpress_action' ) ) {
        return;
    }
    if ( ! current_user_can( 'manage_options' ) ) {
        return;
    }
    $action = sanitize_text_field( $_POST['onionpress_action'] ?? '' );
    if ( ! in_array( $action, array( 'restart', 'stop', 'start', 'save-restart', 'check-reachability', 'generate-vanity', 'import-key-file', 'create-backup', 'restore-backup', 'update' ), true ) ) {
        return;
    }

    // If this is a save-restart, also save config updates first
    if ( $action === 'save-restart' ) {
        $_POST['onionpress_settings_nonce'] = wp_create_nonce( 'onionpress_settings_save' );
        // The settings handler below will fire on the same request
    }

    // Handle backup creation — verify WP password and use it to encrypt the zip
    if ( $action === 'create-backup' ) {
        // $_POST is slashed (wp_magic_quotes). A password set through wp-cli
        // (OnionPress setup) is hashed as typed, so check the unslashed
        // value first; a ' " or \ in it used to make every backup from this
        // page fail. WordPress's own forms store the slashed form
        // (edit_user() and wp_signon() never unslash), so that is checked
        // next. The host is handed whichever form matched: it is the one
        // the Linux launcher re-verifies, and the backup is encrypted with.
        $bp_raw   = isset( $_POST['op_backup_password'] ) && is_string( $_POST['op_backup_password'] )
            ? $_POST['op_backup_password'] : '';
        $bp_typed = wp_unslash( $bp_raw );
        if ( $bp_typed === '' ) {
            add_action( 'admin_notices', function () {
                echo '<div class="notice notice-error"><p>Please enter your password.</p></div>';
            } );
            return;
        }
        $current_user = wp_get_current_user();
        if ( wp_check_password( $bp_typed, $current_user->user_pass, $current_user->ID ) ) {
            $bp_pass = $bp_typed;
        } elseif ( $bp_raw !== $bp_typed
                && wp_check_password( $bp_raw, $current_user->user_pass, $current_user->ID ) ) {
            $bp_pass = $bp_raw;
        } else {
            add_action( 'admin_notices', function () {
                echo '<div class="notice notice-error"><p>Incorrect password. Please enter the password for your WordPress account.</p></div>';
            } );
            return;
        }
        if ( @file_put_contents( '/var/lib/onionpress/backup-password', $bp_pass ) === false ) {
            add_action( 'admin_notices', function () {
                echo '<div class="notice notice-error"><p>Failed to write backup password.</p></div>';
            } );
            return;
        }
    }

    // Handle restore — write password and uploaded zip to shared volume
    if ( $action === 'restore-backup' ) {
        // Exactly what was typed (unslashed), as the Mac and Linux restore
        // prompts take it: the password the backup was encrypted with.
        $rp_pass = isset( $_POST['op_restore_password'] ) && is_string( $_POST['op_restore_password'] )
            ? wp_unslash( $_POST['op_restore_password'] ) : '';
        if ( $rp_pass === '' ) {
            add_action( 'admin_notices', function () {
                echo '<div class="notice notice-error"><p>Please enter the backup password.</p></div>';
            } );
            return;
        }
        if ( empty( $_FILES['op_restore_file'] ) || $_FILES['op_restore_file']['error'] !== UPLOAD_ERR_OK ) {
            add_action( 'admin_notices', function () {
                echo '<div class="notice notice-error"><p>Please select a backup zip file to restore.</p></div>';
            } );
            return;
        }
        if ( @file_put_contents( '/var/lib/onionpress/restore-password', $rp_pass ) === false ) {
            add_action( 'admin_notices', function () {
                echo '<div class="notice notice-error"><p>Failed to write restore password.</p></div>';
            } );
            return;
        }
        if ( ! move_uploaded_file( $_FILES['op_restore_file']['tmp_name'], '/var/lib/onionpress/restore-upload.zip' ) ) {
            @unlink( '/var/lib/onionpress/restore-password' );
            add_action( 'admin_notices', function () {
                echo '<div class="notice notice-error"><p>Failed to save uploaded backup file.</p></div>';
            } );
            return;
        }
    }

    // Handle key file upload for import-key-file action
    if ( $action === 'import-key-file' ) {
        if ( ! empty( $_POST['op_key_b32'] ) ) {
            // Base32-encoded key pasted directly
            $key_data = sanitize_text_field( wp_unslash( $_POST['op_key_b32'] ) );
            if ( @file_put_contents( '/var/lib/onionpress/import-key-data', $key_data ) === false ) {
                add_action( 'admin_notices', function () {
                    echo '<div class="notice notice-error"><p>Failed to write key data.</p></div>';
                } );
                return;
            }
        } elseif ( ! empty( $_FILES['op_key_file'] ) && $_FILES['op_key_file']['error'] === UPLOAD_ERR_OK ) {
            // Binary key file uploaded — encode to base32
            $raw = file_get_contents( $_FILES['op_key_file']['tmp_name'] );
            if ( $raw === false || strlen( $raw ) < 64 ) {
                add_action( 'admin_notices', function () {
                    echo '<div class="notice notice-error"><p>Invalid key file. Expected at least 64 bytes.</p></div>';
                } );
                return;
            }
            $key_data = base64_encode( $raw ); // PHP doesn't have base32, use base64 then convert
            // Use Python-compatible base32 encoding
            $key_data = trim( shell_exec( "echo " . escapeshellarg( bin2hex( $raw ) ) . " | python3 -c \"import base64,sys; print(base64.b32encode(bytes.fromhex(sys.stdin.read().strip())).decode())\"" ) );
            if ( empty( $key_data ) ) {
                add_action( 'admin_notices', function () {
                    echo '<div class="notice notice-error"><p>Failed to encode key file.</p></div>';
                } );
                return;
            }
            if ( @file_put_contents( '/var/lib/onionpress/import-key-data', $key_data ) === false ) {
                add_action( 'admin_notices', function () {
                    echo '<div class="notice notice-error"><p>Failed to write key data.</p></div>';
                } );
                return;
            }
        } else {
            add_action( 'admin_notices', function () {
                echo '<div class="notice notice-error"><p>Please provide a key — either paste the base32 key or upload the key file.</p></div>';
            } );
            return;
        }
    }

    $file = '/var/lib/onionpress/requested-action';

    $cmd_map = array(
        'stop' => 'stop', 'start' => 'start', 'restart' => 'restart', 'save-restart' => 'restart',
        'check-reachability' => 'check-reachability', 'generate-vanity' => 'generate-vanity',
        'import-key-file' => 'import-key-file',
        'create-backup' => 'create-backup', 'restore-backup' => 'restore-backup',
        'update' => 'update',
    );
    $cmd = $cmd_map[ $action ];
    if ( @file_put_contents( $file, $cmd ) === false ) {
        add_action( 'admin_notices', function () {
            echo '<div class="notice notice-error"><p>Failed to write action request. The shared volume may not be mounted.</p></div>';
        } );
        return;
    }

    $label_map = array(
        'stop' => 'Stopping', 'start' => 'Starting', 'restart' => 'Restarting', 'save-restart' => 'Restarting',
        'check-reachability' => 'Testing Tor reachability for',
        'generate-vanity' => 'Generating vanity address for',
        'import-key-file' => 'Importing key for',
        'create-backup' => 'Creating backup of',
        'restore-backup' => 'Restoring backup for',
        'update' => 'Updating',
    );
    $label = $label_map[ $action ] ?? 'Processing';
    $causes_downtime = in_array( $action, array( 'restart', 'save-restart', 'stop', 'restore-backup', 'update' ), true );
    $poll_action = $cmd_map[ $action ] ?? $action;
    add_action( 'admin_notices', function () use ( $label, $causes_downtime, $poll_action ) {
        $msg = esc_html( $label ) . ' OnionPress... This may take a minute.';
        if ( $causes_downtime ) {
            $msg .= ' The page will become unavailable during restart.';
        }
        echo '<div class="notice notice-info op-action-notice" data-op-action="' . esc_attr( $poll_action ) . '"><p>' . $msg . '</p></div>';
    } );
} );

/**
 * Surface the follow-probe result (set by the admin_init handler below)
 * on the next page render.
 */
add_action( 'admin_notices', function () {
    $key   = 'onionpress_follow_probe_' . get_current_user_id();
    $probe = get_transient( $key );
    if ( ! $probe ) {
        return;
    }
    delete_transient( $key );
    $label = isset( $probe['label'] ) ? $probe['label'] : 'follow';
    $verb  = isset( $probe['verb'] ) ? $probe['verb'] : 'Added';
    if ( ! empty( $probe['error'] ) ) {
        echo '<div class="notice notice-warning is-dismissible"><p>'
            . 'Could not resolve <strong>' . esc_html( $label ) . '</strong> &mdash; '
            . 'that onionname is not registered with OnionHome. '
            . 'Check the spelling, or follow the full .onion address instead.'
            . '</p></div>';
    } elseif ( ! empty( $probe['ok'] ) ) {
        echo '<div class="notice notice-success is-dismissible"><p>'
            . esc_html( $verb ) . ' <strong>' . esc_html( $label ) . '</strong> &mdash; feed verified.'
            . '</p></div>';
    } else {
        echo '<div class="notice notice-warning is-dismissible"><p>'
            . esc_html( $verb ) . ' <strong>' . esc_html( $label ) . '</strong>, but the feed could not be reached on this first try. '
            . 'It will be retried in the background with backoff.'
            . '</p></div>';
    }
} );

/**
 * Handle follow/unfollow submissions.
 */
add_action( 'admin_init', function () {
    if ( ! isset( $_POST['onionpress_follow_nonce'] ) ) {
        return;
    }
    if ( ! wp_verify_nonce( $_POST['onionpress_follow_nonce'], 'onionpress_follow_save' ) ) {
        return;
    }
    if ( ! current_user_can( 'manage_options' ) ) {
        return;
    }

    $following = get_option( 'onionpress_following', onionpress_default_follows() );
    if ( ! is_array( $following ) ) {
        $following = array();
    }

    // Unfollow
    if ( ! empty( $_POST['onionpress_unfollow_address'] ) ) {
        $remove = sanitize_text_field( wp_unslash( $_POST['onionpress_unfollow_address'] ) );
        $following = array_values( array_filter( $following, function ( $a ) use ( $remove ) {
            return $a !== $remove;
        } ) );
        update_option( 'onionpress_following', $following );
        // Clean up name, feed, title, and stats mappings
        $names  = get_option( 'onionpress_following_names', array() );
        $feeds  = get_option( 'onionpress_following_feeds', array() );
        $titles = get_option( 'onionpress_following_titles', array() );
        $stats  = get_option( 'onionpress_following_stats', array() );
        unset( $names[ $remove ], $feeds[ $remove ], $titles[ $remove ], $stats[ $remove ] );
        update_option( 'onionpress_following_names', $names );
        update_option( 'onionpress_following_feeds', $feeds );
        update_option( 'onionpress_following_titles', $titles );
        update_option( 'onionpress_following_stats', $stats );
        wp_safe_redirect( admin_url( 'admin.php?page=onionpress-settings' ) );
        exit;
    }

    // Re-lookup — retry the OnionHome directory lookup for a follow that
    // was stored as a bare onionname (its add-time lookup failed, e.g.
    // OnionHome was unreachable, or the name wasn't registered yet). On
    // success the entry is re-keyed to the canonical <addr>.onion/<name>
    // form, exactly as if the original add had resolved.
    if ( ! empty( $_POST['onionpress_relookup_address'] ) ) {
        $orig  = sanitize_text_field( wp_unslash( $_POST['onionpress_relookup_address'] ) );
        $p     = onionpress_follow_parse( $orig );
        $bare  = ( 'url' !== $p['type'] && ! preg_match( '/^[a-z2-7]{56}\.onion$/', $p['addr'] ) );
        $probe = array( 'label' => '@' . $orig, 'ok' => false, 'url' => '', 'verb' => 'Resolved' );
        $addr  = '';
        if ( $bare && in_array( $orig, $following, true ) && function_exists( 'onionpress_directory_lookup' ) ) {
            $name = strtolower( $orig );
            $info = onionpress_directory_lookup( $name );
            $addr = $info ? strtolower( (string) ( $info['onionaddress'] ?? '' ) ) : '';
        }
        if ( $addr && preg_match( '/^[a-z2-7]{56}\.onion$/', $addr ) ) {
            $key = onionpress_follow_key( $addr, $name );
            $following = array_values( array_unique( array_map( function ( $k ) use ( $orig, $key ) {
                return $k === $orig ? $key : $k;
            }, $following ) ) );
            update_option( 'onionpress_following', $following );
            $names = get_option( 'onionpress_following_names', array() );
            if ( ! is_array( $names ) ) { $names = array(); }
            $names[ $key ] = $name;
            unset( $names[ $orig ] );
            update_option( 'onionpress_following_names', $names );
            // Stats/feeds/titles under the bare key were placeholders — drop them.
            foreach ( array( 'onionpress_following_stats', 'onionpress_following_feeds', 'onionpress_following_titles' ) as $opt ) {
                $map = get_option( $opt, array() );
                if ( is_array( $map ) && isset( $map[ $orig ] ) ) {
                    unset( $map[ $orig ] );
                    update_option( $opt, $map );
                }
            }
            // Probe the feed so the row can go straight to a purple check.
            if ( function_exists( 'onionpress_discover_feed_url' ) ) {
                $probe_url = onionpress_discover_feed_url( $addr, $name, 8 );
                if ( $probe_url ) {
                    $resp = wp_remote_get( $probe_url, array(
                        'timeout' => 8,
                        'headers' => array( 'Accept' => 'application/rss+xml, application/atom+xml, application/xml, text/xml' ),
                    ) );
                    if ( ! is_wp_error( $resp ) ) {
                        $code = wp_remote_retrieve_response_code( $resp );
                        $body = wp_remote_retrieve_body( $resp );
                        if ( $code >= 200 && $code < 400
                             && preg_match( '#<(rss|feed|rdf:RDF)\b#i', substr( $body, 0, 2048 ) ) ) {
                            $probe['ok']  = true;
                            $probe['url'] = $probe_url;
                            $stats = get_option( 'onionpress_following_stats', array() );
                            if ( ! is_array( $stats ) ) { $stats = array(); }
                            $stats[ $key ] = array( 'last_success' => time(), 'fail_count' => 0 );
                            update_option( 'onionpress_following_stats', $stats );
                            $feeds = get_option( 'onionpress_following_feeds', array() );
                            if ( ! is_array( $feeds ) ) { $feeds = array(); }
                            $feeds[ $key ] = $probe_url;
                            update_option( 'onionpress_following_feeds', $feeds );
                        }
                    }
                }
            }
            delete_transient( 'onionpress_blogroll_items' );
        } else {
            $probe['error'] = 'not-registered';
        }
        set_transient( 'onionpress_follow_probe_' . get_current_user_id(), $probe, 60 );
        wp_safe_redirect( admin_url( 'admin.php?page=onionpress-settings' ) );
        exit;
    }

    // Follow — accept .onion address, URL, onionname, or direct feed URL
    if ( ! empty( $_POST['onionpress_follow_address'] ) ) {
        $raw = sanitize_text_field( wp_unslash( $_POST['onionpress_follow_address'] ) );
        $raw = trim( $raw );
        // Accept @onionname — theme displays names as @handle, so users
        // will copy/paste with the @ still attached.
        $raw = ltrim( $raw, '@' );
        $resolved_name = '';
        $feed_url = '';

        if ( preg_match( '/([a-z2-7]{56}\.onion)/i', $raw, $m ) ) {
            $addr = strtolower( $m[1] );
            // Extract onionname from URL path: ...onion/NAME or ...onion/NAME/
            if ( preg_match( '#[a-z2-7]{56}\.onion/([a-zA-Z0-9][a-zA-Z0-9._-]+[a-zA-Z0-9])#i', $raw, $pm ) ) {
                $resolved_name = strtolower( $pm[1] );
            }
        } elseif ( preg_match( '#^https?://#i', $raw ) ) {
            // Direct feed or clearnet URL — store the URL itself as the key.
            $addr = $raw;
            $feed_url = $raw;
        } elseif ( function_exists( 'onionpress_directory_lookup' ) ) {
            // Treat as onionname — resolve to .onion via local registry
            $raw_lower = strtolower( $raw );
            $info = onionpress_directory_lookup( $raw_lower );
            $addr = $info ? ( $info['onionaddress'] ?? '' ) : '';
            if ( $addr ) {
                $resolved_name = $raw_lower;
            } else {
                $addr = $raw_lower;
            }
        } else {
            $addr = strtolower( $raw );
        }
        if ( $addr ) {
            // Canonical follow key: the URL itself for feed follows, otherwise
            // <addr>.onion/<onionname> (or bare <addr> when there is no name).
            // Keying by the full path is what lets two onionname subsites on
            // the same .onion address be followed independently.
            if ( preg_match( '#^https?://#i', $addr ) ) {
                $key = $addr;
            } else {
                $key = onionpress_follow_key( $addr, $resolved_name );
            }

            if ( ! in_array( $key, $following, true ) ) {
                $following[] = $key;
                update_option( 'onionpress_following', $following );
            }
            // The onionname is embedded in the key, but record it in the side
            // map too (keyed by the full key) for direct lookups.
            if ( $resolved_name ) {
                $names = get_option( 'onionpress_following_names', array() );
                if ( ! is_array( $names ) ) { $names = array(); }
                if ( ! isset( $names[ $key ] ) || $names[ $key ] !== $resolved_name ) {
                    $names[ $key ] = $resolved_name;
                    update_option( 'onionpress_following_names', $names );
                }
            }
            if ( $feed_url ) {
                $feeds = get_option( 'onionpress_following_feeds', array() );
                if ( ! is_array( $feeds ) ) { $feeds = array(); }
                $feeds[ $key ] = $feed_url;
                update_option( 'onionpress_following_feeds', $feeds );
            }

            // Quick-probe the feed so the admin gets immediate confirmation.
            // Tight timeout keeps the save responsive; on failure we still
            // keep the follow — the background fetcher retries with backoff.
            $probe_label = $resolved_name ? ( '@' . $resolved_name ) : $addr;
            $probe = array( 'label' => $probe_label, 'ok' => false, 'url' => '' );
            $probe_url = '';
            if ( preg_match( '/^[a-z2-7]{56}\.onion$/', $addr )
                 && function_exists( 'onionpress_discover_feed_url' ) ) {
                $probe_url = onionpress_discover_feed_url( $addr, $resolved_name, 8 );
            } elseif ( preg_match( '#^https?://#i', $addr ) ) {
                $probe_url = $addr;
            }
            if ( $probe_url ) {
                $resp = wp_remote_get( $probe_url, array(
                    'timeout' => 8,
                    'headers' => array( 'Accept' => 'application/rss+xml, application/atom+xml, application/xml, text/xml' ),
                ) );
                if ( ! is_wp_error( $resp ) ) {
                    $code = wp_remote_retrieve_response_code( $resp );
                    $body = wp_remote_retrieve_body( $resp );
                    if ( $code >= 200 && $code < 400
                         && preg_match( '#<(rss|feed|rdf:RDF)\b#i', substr( $body, 0, 2048 ) ) ) {
                        $probe['ok']  = true;
                        $probe['url'] = $probe_url;
                        // Seed stats so the UI immediately shows a green check.
                        $stats = get_option( 'onionpress_following_stats', array() );
                        if ( ! is_array( $stats ) ) { $stats = array(); }
                        $stats[ $key ] = array(
                            'last_success' => time(),
                            'fail_count'   => 0,
                        );
                        update_option( 'onionpress_following_stats', $stats );
                        // Cache the verified feed URL too.
                        $feeds = get_option( 'onionpress_following_feeds', array() );
                        if ( ! is_array( $feeds ) ) { $feeds = array(); }
                        $feeds[ $key ] = $probe_url;
                        update_option( 'onionpress_following_feeds', $feeds );
                    }
                }
            }
            set_transient( 'onionpress_follow_probe_' . get_current_user_id(), $probe, 60 );
        }
        wp_safe_redirect( admin_url( 'admin.php?page=onionpress-settings' ) );
        exit;
    }
} );

/**
 * Handle form submission — write config-updates.json to the shared volume.
 */
add_action( 'admin_init', function () {
    if ( ! isset( $_POST['onionpress_settings_nonce'] ) ) {
        return;
    }
    if ( ! wp_verify_nonce( $_POST['onionpress_settings_nonce'], 'onionpress_settings_save' ) ) {
        add_action( 'admin_notices', function () {
            echo '<div class="notice notice-error"><p>Security check failed. Please try again.</p></div>';
        } );
        return;
    }
    if ( ! current_user_can( 'manage_options' ) ) {
        return;
    }

    // The host applies only the keys this page may change, with valid values
    // (SETTINGS_PAGE_KEYS in src/onionpress/config.py) and drops the rest; the
    // checks here mirror it so a bad value gets a notice instead of vanishing.
    $updates = array();
    $fields  = onionpress_settings_fields();

    $old_values  = array();
    $config_file = '/var/lib/onionpress/config-current.json';
    if ( file_exists( $config_file ) ) {
        $decoded = json_decode( file_get_contents( $config_file ), true );
        if ( is_array( $decoded ) ) {
            $old_values = $decoded;
        }
    }

    foreach ( $fields as $key => $field ) {
        // Read-only fields are shown, never sent: the host does not take them
        // from this page. The token field is handled below.
        if ( ! empty( $field['readonly'] ) || $field['type'] === 'secret' ) {
            continue;
        }
        if ( ! isset( $_POST[ 'op_' . $key ] ) ) {
            continue;
        }
        $val = sanitize_text_field( wp_unslash( $_POST[ 'op_' . $key ] ) );

        // Validate against allowed values if specified
        if ( ! empty( $field['options'] ) && ! array_key_exists( $val, $field['options'] ) ) {
            continue;
        }

        // Write under the config key the host reads (LAUNCH_ON_LOGIN on macOS)
        $config_key = ! empty( $field['config_key'] ) ? $field['config_key'] : $key;

        // Whole numbers within the field's range. Blank or untouched leaves
        // the value as it is, even one set out of range elsewhere.
        if ( isset( $field['min'] ) ) {
            if ( $val === '' || $val === (string) ( $old_values[ $config_key ] ?? '' ) ) {
                continue;
            }
            if ( ! preg_match( '/^[0-9]{1,6}$/', $val ) || (int) $val < $field['min'] || (int) $val > $field['max'] ) {
                $msg = sprintf( '%s must be a whole number from %d to %d. It was not changed.', $field['label'], $field['min'], $field['max'] );
                add_action( 'admin_notices', function () use ( $msg ) {
                    echo '<div class="notice notice-error"><p>' . esc_html( $msg ) . '</p></div>';
                } );
                continue;
            }
        }

        $updates[ $config_key ] = $val;
    }

    // Cloudflare Tunnel token: write-only. The token never reaches WordPress,
    // so the field is always empty and blank means "keep the current token";
    // removing it takes the explicit checkbox.
    $cf_token = isset( $_POST['op_CLOUDFLARE_TUNNEL_TOKEN'] ) ? trim( wp_unslash( $_POST['op_CLOUDFLARE_TUNNEL_TOKEN'] ) ) : '';
    if ( $cf_token !== '' ) {
        if ( preg_match( '#^[A-Za-z0-9+/_-]{32,2048}={0,2}$#', $cf_token ) ) {
            $updates['CLOUDFLARE_TUNNEL_TOKEN'] = $cf_token;
        } else {
            add_action( 'admin_notices', function () {
                echo '<div class="notice notice-error"><p>That does not look like a Cloudflare Tunnel token. Paste only the token itself: the long string at the end of the <code>cloudflared</code> command Cloudflare shows. The saved token was not changed.</p></div>';
            } );
        }
    } elseif ( ! empty( $_POST['op_CLOUDFLARE_TUNNEL_TOKEN_remove'] ) ) {
        $updates['CLOUDFLARE_TUNNEL_TOKEN'] = '';
    }

    // Handle Archive.org credentials — convert account/password to S3 keys via xauthn API
    $ia_account  = isset( $_POST['op_ia_account'] )  ? sanitize_text_field( wp_unslash( $_POST['op_ia_account'] ) )  : '';
    $ia_password = isset( $_POST['op_ia_password'] ) ? wp_unslash( $_POST['op_ia_password'] ) : '';
    if ( $ia_account !== '' && $ia_password !== '' ) {
        $s3_keys = onionpress_fetch_ia_s3_keys( $ia_account, $ia_password );
        if ( is_array( $s3_keys ) ) {
            update_option( 'onionpress_archive_s3_access', $s3_keys['access'] );
            update_option( 'onionpress_archive_s3_secret', $s3_keys['secret'] );
            add_action( 'admin_notices', function () {
                echo '<div class="notice notice-success"><p>Archive.org credentials saved.</p></div>';
            } );
        } else {
            add_action( 'admin_notices', function () use ( $s3_keys ) {
                echo '<div class="notice notice-error"><p>Archive.org login failed: ' . esc_html( $s3_keys ) . '</p></div>';
            } );
        }
    } elseif ( $ia_account === '' && $ia_password === '' ) {
        // Both empty — clear credentials
        $had_creds = get_option( 'onionpress_archive_s3_access', '' ) !== '';
        if ( $had_creds ) {
            update_option( 'onionpress_archive_s3_access', '' );
            update_option( 'onionpress_archive_s3_secret', '' );
        }
    }

    // Check if a restart is actually needed ($old_values was read before writing updates).
    // Only Linux shows the restart prompt. CLOUDFLARE_TUNNEL_TOKEN (a secret) is
    // not in config-current.json, so it could never compare equal, and it does
    // not take effect on Linux.
    $needs_restart = false;
    $restart_keys = array( 'ADDRESS_PREFIX', 'VM_MEMORY', 'ONIONHEAVEN_ADDRESS' );
    if ( ! empty( $updates ) ) {
        $onion_address = '';
        $sf = '/var/lib/onionpress/status.json';
        if ( file_exists( $sf ) ) {
            $sd = json_decode( file_get_contents( $sf ), true );
            if ( is_array( $sd ) ) {
                $onion_address = $sd['onion_address'] ?? '';
            }
        }
        foreach ( $updates as $key => $val ) {
            if ( ! in_array( $key, $restart_keys, true ) ) {
                continue;
            }
            if ( isset( $old_values[ $key ] ) && $old_values[ $key ] === $val ) {
                continue;
            }
            if ( $key === 'ADDRESS_PREFIX' && $onion_address && strpos( $onion_address, $val ) === 0 ) {
                continue;
            }
            $needs_restart = true;
        }

        // Write config-updates.json for the onionpress script to pick up
        $json = json_encode( $updates, JSON_PRETTY_PRINT );
        $file = '/var/lib/onionpress/config-updates.json';
        if ( @file_put_contents( $file, $json ) === false ) {
            add_action( 'admin_notices', function () {
                echo '<div class="notice notice-error"><p>Failed to write config update. The shared volume may not be mounted.</p></div>';
            } );
            return;
        }

        // Update config-current.json so the form reflects the saved values
        // immediately — never with the token, which that file must not hold
        $current = array_merge( $old_values, $updates );
        unset( $current['CLOUDFLARE_TUNNEL_TOKEN'] );
        @file_put_contents( $config_file, json_encode( $current, JSON_PRETTY_PRINT ) );
    }

    // Show success message
    add_action( 'admin_notices', function () use ( $needs_restart ) {
        $is_linux = false;
        $sf = '/var/lib/onionpress/status.json';
        if ( file_exists( $sf ) ) {
            $sr = file_get_contents( $sf );
            if ( $sr !== false ) {
                $sd = json_decode( $sr, true );
                if ( is_array( $sd ) && isset( $sd['platform'] ) && $sd['platform'] === 'linux' ) {
                    $is_linux = true;
                }
            }
        }
        if ( $is_linux && $needs_restart ) {
            $restart_nonce = wp_create_nonce( 'onionpress_action' );
            echo '<div class="notice notice-success is-dismissible"><p>Settings saved. Restart OnionPress for changes to take effect. ';
            echo '<form method="post" style="display:inline;"><input type="hidden" name="onionpress_action_nonce" value="' . esc_attr( $restart_nonce ) . '"><input type="hidden" name="onionpress_action" value="restart"><button type="submit" class="button button-primary" style="margin-left:8px;">Restart Now</button></form>';
            echo '</p></div>';
        } elseif ( ! $is_linux ) {
            echo '<div class="notice notice-success is-dismissible"><p>Settings saved. Changes will take effect within 30 seconds.</p></div>';
        } else {
            echo '<div class="notice notice-success is-dismissible"><p>Settings saved.</p></div>';
        }
    } );
} );

/**
 * Increase upload limit for backup restore on the settings page.
 */
add_filter( 'upload_size_limit', function ( $size ) {
    if ( isset( $_GET['page'] ) && $_GET['page'] === 'onionpress-settings' ) {
        return 512 * 1024 * 1024; // 512 MB
    }
    return $size;
} );

// Set PHP limits when on the settings page (backup uploads can be large)
add_action( 'admin_init', function () {
    if ( isset( $_GET['page'] ) && $_GET['page'] === 'onionpress-settings' ) {
        @ini_set( 'upload_max_filesize', '512M' );
        @ini_set( 'post_max_size', '512M' );
        @ini_set( 'max_execution_time', '600' );
    }
} );

/**
 * AJAX handler for downloading backup files.
 */
add_action( 'wp_ajax_onionpress_download_backup', function () {
    if ( ! current_user_can( 'manage_options' ) ) {
        wp_die( 'Unauthorized' );
    }
    check_ajax_referer( 'onionpress_download_backup' );

    $filename = sanitize_file_name( $_GET['file'] ?? '' );
    if ( empty( $filename ) ) {
        wp_die( 'No file specified' );
    }

    $filepath = '/var/lib/onionpress/' . $filename;
    if ( ! file_exists( $filepath ) ) {
        wp_die( 'Backup file not found. It may have been cleaned up.' );
    }

    header( 'Content-Type: application/zip' );
    header( 'Content-Disposition: attachment; filename="' . $filename . '"' );
    header( 'Content-Length: ' . filesize( $filepath ) );
    readfile( $filepath );

    // Clean up after download
    @unlink( $filepath );
    @unlink( '/var/lib/onionpress/backup-result.json' );
    exit;
} );

/**
 * AJAX handler for polling action completion.
 */
add_action( 'wp_ajax_onionpress_poll_action', function () {
    if ( ! current_user_can( 'manage_options' ) ) {
        wp_send_json_error( 'Unauthorized' );
    }

    $action = sanitize_text_field( $_GET['op_action'] ?? '' );
    $pending = file_exists( '/var/lib/onionpress/requested-action' );

    // Map actions to their result files
    $result_files = array(
        'check-reachability' => '/var/lib/onionpress/reachability-result.json',
        'generate-vanity'    => '/var/lib/onionpress/vanity-result.json',
        'import-key-file'    => '/var/lib/onionpress/import-result.json',
        'create-backup'      => '/var/lib/onionpress/backup-result.json',
        'restore-backup'     => '/var/lib/onionpress/restore-result.json',
        'update'             => '/var/lib/onionpress/update-result.json',
    );

    if ( isset( $result_files[ $action ] ) ) {
        $rf = $result_files[ $action ];
        if ( file_exists( $rf ) ) {
            $result = json_decode( file_get_contents( $rf ), true );
            wp_send_json( array( 'done' => true, 'result' => $result ) );
        }
        wp_send_json( array( 'done' => false ) );
    }

    // For start/stop/restart — check for result file, pending marker, then action-consumed
    $service_result  = '/var/lib/onionpress/service-result.json';
    $service_pending = '/var/lib/onionpress/service-pending';
    if ( file_exists( $service_result ) ) {
        $result = json_decode( file_get_contents( $service_result ), true );
        @unlink( $service_result );
        wp_send_json( array( 'done' => true, 'result' => $result ) );
    }
    // Still in progress if pending marker exists (written before container restart)
    if ( file_exists( $service_pending ) ) {
        wp_send_json( array( 'done' => false ) );
    }
    wp_send_json( array( 'done' => ! $pending ) );
} );

/**
 * AJAX handler for checking latest version (cached 10 min).
 */
add_action( 'wp_ajax_onionpress_check_update', function () {
    if ( ! current_user_can( 'manage_options' ) ) {
        wp_send_json_error( 'Unauthorized' );
    }

    $current = trim( @file_get_contents( '/var/lib/onionpress/version' ) ?: 'unknown' );

    // Check transient cache first
    $cached = get_transient( 'onionpress_latest_release' );
    if ( $cached !== false ) {
        wp_send_json( array(
            'current'   => $current,
            'latest'    => $cached['tag'],
            'update'    => version_compare( ltrim( $cached['tag'], 'v' ), ltrim( $current, 'v' ), '>' ),
        ) );
    }

    // Fetch from GitHub releases API (via Tor to avoid clearnet leak)
    $resp = onionpress_curl_tor( 'https://api.github.com/repos/brewsterkahle/onionpress/releases/latest' );
    if ( $resp['error'] || $resp['code'] !== 200 ) {
        wp_send_json( array( 'current' => $current, 'latest' => null, 'update' => false, 'error' => 'Failed to check for updates' ) );
    }

    $data = json_decode( $resp['body'], true );
    $tag  = $data['tag_name'] ?? '';

    set_transient( 'onionpress_latest_release', array( 'tag' => $tag ), 600 ); // 10 min

    wp_send_json( array(
        'current'   => $current,
        'latest'    => $tag,
        'update'    => version_compare( ltrim( $tag, 'v' ), ltrim( $current, 'v' ), '>' ),
    ) );
} );

/**
 * Define the settings fields and their metadata.
 */
function onionpress_settings_fields() {
    // Detect platform from status.json
    $is_linux = false;
    $status_file = '/var/lib/onionpress/status.json';
    if ( file_exists( $status_file ) ) {
        $raw = file_get_contents( $status_file );
        if ( $raw !== false ) {
            $s = json_decode( $raw, true );
            if ( is_array( $s ) && isset( $s['platform'] ) && $s['platform'] === 'linux' ) {
                $is_linux = true;
            }
        }
    }

    return array(
        'ADDRESS_PREFIX' => array(
            'label'       => 'Onion Address Prefix',
            'description' => 'The beginning of your .onion address, chosen when you first set up OnionPress. It can no longer be changed here — to use a different address, restore from a backup that has it.',
            'type'        => 'text',
            'readonly'    => true,
            'placeholder' => 'op2',
        ),
        'UPDATE_ON_LAUNCH' => array(
            'label'       => 'Update on Launch',
            'description' => 'Automatically check for and download updated Docker images when the app launches.',
            'type'        => 'select',
            'options'     => array( 'yes' => 'Enabled', 'no' => 'Disabled' ),
        ),
        'START_ON_BOOT' => array(
            'label'       => $is_linux ? 'Start on Boot' : 'Launch on Login',
            'description' => $is_linux
                ? 'Automatically start OnionPress when the system boots.'
                : 'Automatically start OnionPress when you log in to macOS.',
            'type'        => 'select',
            'options'     => array( 'yes' => 'Enabled', 'no' => 'Disabled' ),
            'config_key'  => $is_linux ? 'START_ON_BOOT' : 'LAUNCH_ON_LOGIN',
        ),
        'PREVENT_SLEEP' => array(
            'label'       => 'Sleep Prevention',
            'description' => 'Control whether OnionPress keeps your Mac awake while running.',
            'type'        => 'select',
            'options'     => array(
                'normal'     => 'Normal (Mac sleeps as usual)',
                'on-battery' => 'On Battery (stay awake on AC power)',
                'never'      => 'Never (always stay awake)',
            ),
            'platform'    => 'macos',
        ),
        'VM_MEMORY' => array(
            'label'       => 'VM Memory (GB)',
            'description' => 'RAM allocated to the container VM. Requires restart to take effect.',
            'type'        => 'text',
            'placeholder' => '1',
            'platform'    => 'macos',
            'min'         => 1,
            'max'         => 64,
        ),
        'CLOUDFLARE_TUNNEL_TOKEN' => array(
            'label'       => 'Cloudflare Tunnel Token',
            'description' => 'Expose your site on the regular internet via Cloudflare Tunnel. Privacy note: this reveals your IP to Cloudflare. Do NOT install cloudflared on your Mac — OnionPress runs it automatically inside Docker. The saved token is never shown: paste a token to set or replace it, or leave the field blank to keep the current one.',
            'type'        => 'secret',
            'placeholder' => 'Paste a new token, or leave blank',
        ),
        'REGISTER_WITH_ONIONHEAVEN' => array(
            'label'       => 'Keep site reachable via OnionHeaven (recommended)',
            'description' => 'Keeps your .onion address working while this machine is off, asleep, or disconnected: when your heartbeats stop, the OnionHeaven hub takes over your address and sends visitors to the Wayback Machine\'s archived copy of your site; your live site takes over again when you come back online. What is shared: a copy of your onion service private key is sent to the hub (run by the Internet Archive) so it can publish your address on your behalf. It is used only to keep your site reachable. Opt-in; strongly recommended.',
            'type'        => 'select',
            'options'     => array( '' => 'Not decided yet (OnionPress will ask)', 'yes' => 'Enabled (recommended)', 'no' => 'Disabled' ),
        ),
        'SHARE_ANALYTICS_WITH_ONIONHOME' => array(
            'label'       => 'Share diagnostic logs with OnionHome',
            'description' => 'Periodically upload completed log files to the OnionHome hub for remote debugging. Logs are scrubbed of your home directory path before upload. Disabled by default.',
            'type'        => 'select',
            'options'     => array( 'no' => 'Disabled', 'yes' => 'Enabled' ),
        ),
        'ONIONHEAVEN_ADDRESS' => array(
            'label'       => 'OnionHeaven Hub Address (advanced)',
            'description' => 'The .onion address of the OnionHeaven hub to register with for Wayback Machine fallback. Your onion service key is sent to this hub, so it cannot be changed from WordPress: on a Mac use Settings in the OnionPress menu, on Linux edit ~/.onionpress/config, then restart OnionPress.',
            'type'        => 'text',
            'readonly'    => true,
            'placeholder' => 'oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion',
        ),
        'ONIONHEAVEN_MAX_SERVICES' => array(
            'label'       => 'Max Services per Takeover Worker',
            'description' => 'Maximum onion services each takeover container can handle. Lower values use more containers but are more reliable.',
            'type'        => 'text',
            'placeholder' => '50',
            'show_when'   => 'onionheaven_server',
            'min'         => 1,
            'max'         => 500,
        ),
        'ONIONHEAVEN_PROPAGATION_DELAY' => array(
            'label'       => 'Takeover Delay (seconds)',
            'description' => 'Seconds after last heartbeat before triggering takeover. Default 180 (~3 missed heartbeats).',
            'type'        => 'text',
            'placeholder' => '180',
            'show_when'   => 'onionheaven_server',
            'min'         => 60,
            'max'         => 3600,
        ),
        'ONIONHEAVEN_HUB_THRESHOLD' => array(
            'label'       => 'Hub Auto-Promote Threshold',
            'description' => 'Number of registered sites before auto-promoting to hub mode (10 intro points). Default 5.',
            'type'        => 'text',
            'placeholder' => '5',
            'show_when'   => 'onionheaven_server',
            'min'         => 1,
            'max'         => 1000,
        ),
    );
}

/**
 * Render the settings page.
 */
function onionpress_settings_page() {
    if ( ! current_user_can( 'manage_options' ) ) {
        wp_die( 'Unauthorized' );
    }

    // Read current config from the shared volume
    $current = array();
    $config_file = '/var/lib/onionpress/config-current.json';
    if ( file_exists( $config_file ) ) {
        $raw = file_get_contents( $config_file );
        if ( $raw !== false ) {
            $decoded = json_decode( $raw, true );
            if ( is_array( $decoded ) ) {
                $current = $decoded;
            }
        }
    }

    // Read Wayback S3 credentials from wp_options
    $s3_access = get_option( 'onionpress_archive_s3_access', '' );
    $s3_secret = get_option( 'onionpress_archive_s3_secret', '' );

    // Read status for version display
    $status_file = '/var/lib/onionpress/status.json';
    $status = null;
    if ( file_exists( $status_file ) ) {
        $raw = file_get_contents( $status_file );
        if ( $raw !== false ) {
            $status = json_decode( $raw, true );
        }
    }

    $fields = onionpress_settings_fields();

    // Minimal status values for the status bar
    $state         = $status ? ( $status['state'] ?? 'unknown' ) : 'unknown';
    $onion_address = $status ? ( $status['onion_address'] ?? '' ) : '';
    $onionname     = $status ? ( $status['onionname'] ?? '' ) : '';

    $state_colors = array(
        'running'  => '#4ade80',
        'starting' => '#eab308',
        'stopped'  => '#9ca3af',
        'unknown'  => '#9ca3af',
    );
    $state_color = $state_colors[ $state ] ?? '#9ca3af';

    ?>
    <style>
        .onionpress-state-dot {
            display: inline-block;
            width: 8px;
            height: 8px;
            border-radius: 50%;
            margin-right: 6px;
            vertical-align: middle;
        }
    </style>
    <?php
    $current_platform = isset( $status['platform'] ) ? $status['platform'] : 'macos';
    $oh_server_active = ! empty( $status['onionheaven']['server_active'] );
    ?>
    <div class="wrap">
        <h1>OnionPress Settings</h1>
        <?php
        $op_current_version = trim( @file_get_contents( '/var/lib/onionpress/version' ) ?: 'unknown' );
        $op_current_display = 'v' . ltrim( $op_current_version, 'v' );
        ?>
        <div id="op-update-status" style="margin: 0 0 12px 0; font-size: 13px; color: #555;">
            <span class="description">version: <?php echo esc_html( $op_current_display ); ?>, checking for updates...</span>
        </div>

        <p style="margin-bottom: 16px; font-size: 14px;">
            <span class="onionpress-state-dot" style="background:<?php echo esc_attr( $state_color ); ?>"></span>
            <strong><?php echo esc_html( ucfirst( $state ) ); ?></strong>
            <?php if ( $onion_address && strpos( $onion_address, '.onion' ) !== false ) : ?>
                &mdash; <?php
                    $display_url = $onion_address;
                    if ( $onionname ) {
                        $display_url .= '/' . $onionname;
                    }
                ?><a href="http://<?php echo esc_attr( $display_url ); ?>/" style="font-size:12px;font-family:monospace;color:#8b5cf6;"><?php echo esc_html( $display_url ); ?></a>
            <?php endif; ?>
            <?php
            // Linked only for those onionpress-status.php lets in: the site owner.
            if ( is_super_admin() ) :
                ?>
            &mdash; <a href="/onionpress-status">View full status &amp; logs &rarr;</a>
            <?php endif; ?>
        </p>

        <!-- Following Section -->
        <?php
        $following = get_option( 'onionpress_following', onionpress_default_follows() );
        if ( ! is_array( $following ) ) {
            $following = array();
        }
        // Feed titles learned from RSS fetches (keyed by the full follow key).
        // The onionname now lives in the key itself, so there's no separate
        // names map to read here — it's derived via onionpress_follow_parse().
        $following_titles = get_option( 'onionpress_following_titles', array() );
        if ( ! is_array( $following_titles ) ) { $following_titles = array(); }
        // Per-follow stats — used to color-code the status indicator.
        $following_stats = get_option( 'onionpress_following_stats', array() );
        if ( ! is_array( $following_stats ) ) { $following_stats = array(); }
        ?>
        <div style="margin-bottom: 20px; border: 1px solid #c3c4c7; border-radius: 4px; padding: 12px 16px; background: #f9f9f9;">
            <h2 style="margin-top: 0;">Following</h2>
            <div id="onionpress-following-list" style="max-height: 200px; overflow-y: auto; margin-bottom: 10px;">
                <?php if ( empty( $following ) ) : ?>
                <p class="description" id="onionpress-following-empty">No onion services followed yet. Add one below.</p>
                <?php endif; ?>
                <?php foreach ( $following as $key ) :
                    // $key is the canonical follow key: a URL, or <addr>.onion[/<name>].
                    $p    = onionpress_follow_parse( $key );
                    $name = $p['name'];
                ?>
                <div class="onionpress-following-entry" style="display: flex; align-items: center; margin-bottom: 4px;">
                    <?php
                        // Status glyph matches the menubar vocabulary:
                        //   purple ✓ = fetched successfully (like menubar "running")
                        //   yellow ⚠ = failing or malformed (like menubar "starting/stuck")
                        //   blank    = never attempted yet (new follow, before first fetch)
                        $st           = isset( $following_stats[ $key ] ) ? $following_stats[ $key ] : array();
                        $last_success = isset( $st['last_success'] ) ? (int) $st['last_success'] : 0;
                        $fail_count   = isset( $st['fail_count'] ) ? (int) $st['fail_count'] : 0;
                        $well_formed  = ( 'url' === $p['type'] ) || preg_match( '/^[a-z2-7]{56}\.onion$/', $p['addr'] );
                        // A bare onionname: stored when the add-time OnionHome
                        // lookup failed. Offered a re-lookup button below.
                        $is_bare_name = ( ! $well_formed
                            && preg_match( '/^[a-z0-9][a-z0-9._-]*$/i', $p['addr'] ) );
                        if ( $last_success && ! $fail_count ) {
                            $glyph = '&#10003;'; $color = '#6b46a8'; // purple: verified
                            $status_label = 'Feed fetched successfully — last update '
                                . human_time_diff( $last_success ) . ' ago.';
                        } elseif ( $is_bare_name ) {
                            $glyph = '&#9888;';  $color = '#dba617'; // yellow triangle: unresolved name
                            $status_label = 'Unresolved onionname — the OnionHome lookup has not succeeded for this name. Use the retry button to look it up again.';
                        } elseif ( ! $well_formed ) {
                            $glyph = '&#9888;';  $color = '#dba617'; // yellow triangle: malformed
                            $status_label = 'Malformed address — this entry is not a valid .onion address or feed URL, so no feed can be fetched.';
                        } elseif ( $fail_count ) {
                            $glyph = '&#9888;';  $color = '#dba617'; // yellow triangle: failing
                            $status_label = 'Feed not reachable yet (' . $fail_count . ' failed '
                                . _n( 'attempt', 'attempts', $fail_count )
                                . '). Onion feeds are fetched over Tor and can take a few minutes after you follow — it keeps retrying.';
                        } else {
                            $glyph = '';         $color = '';        // blank: untested
                            $status_label = 'Not checked yet — waiting for the first feed fetch.';
                        }
                    ?>
                    <span class="onionpress-following-status" title="<?php echo esc_attr( $status_label ); ?>" style="margin-right: 6px; color: <?php echo esc_attr( $color ); ?>; min-width: 14px; display: inline-block; cursor: help;"><?php echo $glyph; ?></span>
                    <code style="flex: 1; font-size: 12px;"><?php
                        $title    = isset( $following_titles[ $key ] ) ? $following_titles[ $key ] : '';
                        $addr_disp = esc_html( substr( $p['addr'], 0, 12 ) ) . '&hellip;.onion'
                                   . ( '' !== $name ? '/' . esc_html( $name ) : '' );
                        if ( $title ) {
                            // Title is primary; key (addr/name or URL) in gray.
                            echo '<strong>' . esc_html( $title ) . '</strong>';
                            echo ' <span style="color:#999;">'
                                . ( 'url' === $p['type'] ? esc_html( $p['url'] ) : $addr_disp )
                                . '</span>';
                        } elseif ( 'url' === $p['type'] ) {
                            echo esc_html( $p['url'] );
                        } elseif ( '' !== $name ) {
                            // Onionname is primary; truncated address in gray.
                            echo '<strong>' . esc_html( $name ) . '</strong>';
                            echo ' <span style="color:#999;">' . esc_html( substr( $p['addr'], 0, 12 ) ) . '&hellip;.onion</span>';
                        } else {
                            echo esc_html( $key );
                        }
                    ?></code>
                    <a class="onionpress-following-open" href="<?php echo esc_url( onionpress_follow_site_url( $key ) ); ?>" target="_blank" rel="noopener noreferrer" title="Open this site (requires Tor Browser)" aria-label="Open this site" style="margin-left: 8px; color: #6b46a8; text-decoration: none; display: inline-flex; align-items: center;"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path><polyline points="15 3 21 3 21 9"></polyline><line x1="10" y1="14" x2="21" y2="3"></line></svg></a>
                    <?php if ( $is_bare_name ) : ?>
                    <button type="button" class="button-link onionpress-relookup" data-address="<?php echo esc_attr( $key ); ?>" title="Look up this onionname on OnionHome again" aria-label="Look up this onionname again" style="color: #6b46a8; margin-left: 8px;">&#8635;</button>
                    <?php endif; ?>
                    <button type="button" class="button-link onionpress-unfollow" data-address="<?php echo esc_attr( $key ); ?>" style="color: #b32d2e; margin-left: 8px;">&times;</button>
                </div>
                <?php endforeach; ?>
            </div>
            <form method="post" id="onionpress-follow-form" style="display: flex; gap: 8px;">
                <?php wp_nonce_field( 'onionpress_follow_save', 'onionpress_follow_nonce' ); ?>
                <input type="text" name="onionpress_follow_address" id="onionpress-follow-input"
                       placeholder="Onionname, .onion address, or feed URL" class="regular-text" style="flex: 1;">
                <button type="submit" class="button button-primary" id="onionpress-follow-submit">Follow</button>
            </form>
            <p id="onionpress-follow-status" role="status" aria-live="polite" style="display: none; margin: 6px 0 0; color: #646970;">
                <span class="spinner is-active" style="float: none; margin: 0 6px 0 0; vertical-align: middle;"></span><span id="onionpress-follow-status-text"></span>
            </p>
            <?php foreach ( $following as $addr ) : ?>
            <input type="hidden" name="onionpress_following_existing[]" form="onionpress-unfollow-form" value="<?php echo esc_attr( $addr ); ?>">
            <?php endforeach; ?>
            <form method="post" id="onionpress-unfollow-form" style="display: none;">
                <?php wp_nonce_field( 'onionpress_follow_save', 'onionpress_follow_nonce' ); ?>
                <input type="hidden" name="onionpress_unfollow_address" id="onionpress-unfollow-address" value="">
            </form>
            <form method="post" id="onionpress-relookup-form" style="display: none;">
                <?php wp_nonce_field( 'onionpress_follow_save', 'onionpress_follow_nonce' ); ?>
                <input type="hidden" name="onionpress_relookup_address" id="onionpress-relookup-address" value="">
            </form>
        </div>
        <script>
        (function() {
            // Extract .onion from pasted URLs
            var input = document.getElementById('onionpress-follow-input');
            input.addEventListener('paste', function(e) {
                setTimeout(function() {
                    var val = input.value.trim();
                    var match = val.match(/([a-z2-7]{56}\.onion)/i);
                    if (match) {
                        input.value = match[1].toLowerCase();
                    }
                }, 0);
            });
            input.addEventListener('change', function() {
                var val = input.value.trim();
                var match = val.match(/([a-z2-7]{56}\.onion)/i);
                if (match) {
                    input.value = match[1].toLowerCase();
                }
            });
            // Follow button: the POST resolves an onionname through OnionHome
            // over Tor (a few seconds, longer on a bad circuit), so show what is
            // happening and block a double-submit until the page reloads with
            // the result. Plain POST round trip — no AJAX.
            var followForm = document.getElementById('onionpress-follow-form');
            var followBtn = document.getElementById('onionpress-follow-submit');
            var followStatus = document.getElementById('onionpress-follow-status');
            var followStatusText = document.getElementById('onionpress-follow-status-text');
            var followBusy = false;
            function setFollowBusy(on, message) {
                followBusy = on;
                followBtn.disabled = on;
                // readOnly, not disabled: a disabled field is dropped from the POST.
                input.readOnly = on;
                followStatusText.textContent = on ? message : '';
                followStatus.style.display = on ? 'block' : 'none';
            }
            followForm.addEventListener('submit', function(e) {
                if (followBusy) {
                    e.preventDefault();
                    return;
                }
                var val = input.value.trim();
                if (!val) {
                    return;
                }
                var isName = !/\.onion\b/i.test(val) && !/^[a-z][a-z0-9+.-]*:\/\//i.test(val);
                setFollowBusy(true, isName
                    ? 'Looking up ' + val.replace(/^@/, '') + ' on OnionHome over Tor\u2026'
                    : 'Checking the feed over Tor\u2026');
            });
            // Back/forward cache can restore the page with the busy state intact.
            window.addEventListener('pageshow', function() {
                setFollowBusy(false, '');
            });
            // Unfollow buttons
            document.querySelectorAll('.onionpress-unfollow').forEach(function(btn) {
                btn.addEventListener('click', function() {
                    document.getElementById('onionpress-unfollow-address').value = btn.dataset.address;
                    document.getElementById('onionpress-unfollow-form').submit();
                });
            });
            document.querySelectorAll('.onionpress-relookup').forEach(function(btn) {
                btn.addEventListener('click', function() {
                    btn.disabled = true;
                    btn.textContent = '…';
                    document.getElementById('onionpress-relookup-address').value = btn.dataset.address;
                    document.getElementById('onionpress-relookup-form').submit();
                });
            });
        })();
        </script>

        <hr style="border: none; border-top: 1px solid #c3c4c7; margin: 20px 0;">

        <form method="post">
            <?php wp_nonce_field( 'onionpress_settings_save', 'onionpress_settings_nonce' ); ?>
            <table class="form-table" role="presentation">
                <?php foreach ( $fields as $key => $field ) : ?>
                <?php
                // Skip fields restricted to a different platform
                if ( ! empty( $field['platform'] ) && $field['platform'] !== $current_platform ) {
                    continue;
                }
                // Skip fields that require OnionHeaven server to be active
                if ( ! empty( $field['show_when'] ) && $field['show_when'] === 'onionheaven_server' && ! $oh_server_active ) {
                    continue;
                }
                // Use config_key if specified (for renamed settings like LAUNCH_ON_LOGIN -> START_ON_BOOT)
                $config_key = ! empty( $field['config_key'] ) ? $field['config_key'] : $key;
                ?>
                <tr>
                    <th scope="row"><label for="op_<?php echo esc_attr( $key ); ?>"><?php echo esc_html( $field['label'] ); ?></label></th>
                    <td>
                        <?php
                        $val = $current[ $config_key ] ?? ( $current[ $key ] ?? '' );
                        if ( $field['type'] === 'select' && ! empty( $field['options'] ) ) :
                        ?>
                            <select name="op_<?php echo esc_attr( $key ); ?>" id="op_<?php echo esc_attr( $key ); ?>">
                                <?php foreach ( $field['options'] as $opt_val => $opt_label ) : ?>
                                <option value="<?php echo esc_attr( $opt_val ); ?>" <?php selected( $val, $opt_val ); ?>>
                                    <?php echo esc_html( $opt_label ); ?>
                                </option>
                                <?php endforeach; ?>
                            </select>
                        <?php elseif ( $field['type'] === 'secret' ) : ?>
                            <?php // Write-only: the saved value is never sent to the browser. new-password keeps browsers from filling in a saved login. ?>
                            <span class="op-password-wrap">
                                <input type="password" name="op_<?php echo esc_attr( $key ); ?>" id="op_<?php echo esc_attr( $key ); ?>"
                                       value="" autocomplete="new-password" spellcheck="false"
                                       placeholder="<?php echo esc_attr( $field['placeholder'] ?? '' ); ?>"
                                       class="regular-text">
                                <button type="button" class="op-eye-toggle" aria-label="Show password">&#128065;</button>
                            </span>
                            <label style="display: block; margin-top: 6px;">
                                <input type="checkbox" name="op_<?php echo esc_attr( $key ); ?>_remove" value="1">
                                Remove the saved token (turns Cloudflare Tunnel off when OnionPress restarts)
                            </label>
                        <?php else : ?>
                            <input type="text" name="op_<?php echo esc_attr( $key ); ?>" id="op_<?php echo esc_attr( $key ); ?>"
                                   value="<?php echo esc_attr( $val ); ?>"
                                   placeholder="<?php echo esc_attr( $field['placeholder'] ?? '' ); ?>"
                                   <?php echo ! empty( $field['readonly'] ) ? 'readonly' : ''; ?>
                                   class="regular-text">
                        <?php endif; ?>
                        <?php if ( $key === 'SHARE_ANALYTICS_WITH_ONIONHOME' ) : ?>
                            <button type="button" id="onionpress-share-now" class="button button-secondary" style="margin-left: 8px;">Share Now</button>
                            <span id="onionpress-share-now-status" style="margin-left: 8px;"></span>
                            <script>
                            document.getElementById('onionpress-share-now').addEventListener('click', function() {
                                var btn = this;
                                var status = document.getElementById('onionpress-share-now-status');
                                btn.disabled = true;
                                status.textContent = 'Triggering upload...';
                                fetch('/wp-json/onionpress/v1/upload-analytics', { method: 'POST',
                                    headers: { 'X-WP-Nonce': '<?php echo wp_create_nonce( 'wp_rest' ); ?>' }
                                }).then(function(r) { return r.json(); }).then(function(data) {
                                    status.textContent = data.message || 'Upload triggered!';
                                    btn.disabled = false;
                                }).catch(function(e) {
                                    status.textContent = 'Error: ' + e.message;
                                    btn.disabled = false;
                                });
                            });
                            </script>
                        <?php endif; ?>
                        <p class="description"><?php echo esc_html( $field['description'] ); ?></p>
                    </td>
                </tr>
                <?php endforeach; ?>

                <!-- Archive.org Credentials -->
                <tr>
                    <th scope="row"><label for="op_ia_account">Archive.org Account</label></th>
                    <td>
                        <input type="text" name="op_ia_account" id="op_ia_account"
                               value="" class="regular-text" autocomplete="off"
                               placeholder="<?php echo $s3_access ? '(credentials saved)' : ''; ?>">
                        <p class="description">Used to archive your site on the Wayback Machine, making it more robust and permanent. <?php if ( $s3_access ) echo '<strong>Credentials are configured.</strong> Re-enter to update.'; ?></p>
                    </td>
                </tr>
                <tr>
                    <th scope="row"><label for="op_ia_password">Archive.org Password</label></th>
                    <td>
                        <span class="op-password-wrap">
                            <input type="password" name="op_ia_password" id="op_ia_password"
                                   value="" class="regular-text" autocomplete="off">
                            <button type="button" class="op-eye-toggle" aria-label="Show password">&#128065;</button>
                        </span>
                        <p class="description">Your Archive.org password. Used to retrieve API keys — not stored.</p>
                    </td>
                </tr>
            </table>

            <?php submit_button( 'Save Settings' ); ?>

            <?php if ( $current_platform !== 'linux' ) : ?>
            <p class="description">
                Settings are picked up by the OnionPress menubar app within 30 seconds.
                Some settings (VM Memory, Address Prefix) require a restart to take effect.
            </p>
            <?php endif; ?>
        </form>

        <hr style="border: none; border-top: 3px solid #c3c4c7; margin: 30px 0;">
        <h2>Service Control</h2>
        <p class="description">Some settings (Address Prefix, Cloudflare Tunnel) require a restart to take effect.</p>
        <form method="post" style="display: inline-block; margin-right: 10px;">
            <?php wp_nonce_field( 'onionpress_action', 'onionpress_action_nonce' ); ?>
            <input type="hidden" name="onionpress_action" value="restart">
            <?php submit_button( 'Restart OnionPress', 'primary', 'submit', false ); ?>
        </form>
        <form method="post" style="display: inline-block; margin-right: 10px;">
            <?php wp_nonce_field( 'onionpress_action', 'onionpress_action_nonce' ); ?>
            <input type="hidden" name="onionpress_action" value="stop">
            <?php submit_button( 'Stop OnionPress', 'secondary', 'submit', false ); ?>
        </form>
        <p class="description" style="margin-top: 10px;">
            Restart will apply any saved settings changes. The page will be unavailable briefly during restart.
            <?php if ( $current_platform === 'linux' ) : ?>
            <br>To start after a full stop, use SSH: <code>sudo systemctl start onionpress</code>
            <?php endif; ?>
        </p>

        <!-- Tor Reachability Test -->
        <hr>
        <h2>Tor Reachability Test</h2>
        <?php
        $reach_file = '/var/lib/onionpress/reachability-result.json';
        if ( file_exists( $reach_file ) ) {
            $reach = json_decode( file_get_contents( $reach_file ), true );
            if ( is_array( $reach ) ) {
                $reach_color = ! empty( $reach['reachable'] ) ? '#16a34a' : '#dc2626';
                $reach_label = ! empty( $reach['reachable'] ) ? 'Reachable' : 'Not reachable';
                echo '<p><strong style="color:' . esc_attr( $reach_color ) . '">' . esc_html( $reach_label ) . '</strong>';
                if ( ! empty( $reach['http_code'] ) ) {
                    echo ' (HTTP ' . esc_html( $reach['http_code'] ) . ')';
                }
                if ( ! empty( $reach['error'] ) ) {
                    echo ' &mdash; ' . esc_html( $reach['error'] );
                }
                if ( ! empty( $reach['tested_at'] ) ) {
                    echo ' <span class="description">&mdash; tested ' . esc_html( $reach['tested_at'] ) . '</span>';
                }
                echo '</p>';
            }
        }
        ?>
        <p class="description">Test whether your onion service is accessible from the Tor network. This takes up to 60 seconds.</p>
        <form method="post" style="margin-top: 8px;">
            <?php wp_nonce_field( 'onionpress_action', 'onionpress_action_nonce' ); ?>
            <input type="hidden" name="onionpress_action" value="check-reachability">
            <?php submit_button( 'Test Reachability', 'secondary', 'submit', false ); ?>
        </form>

        <!-- Backup -->
        <hr>
        <h2>Backup</h2>
        <?php
        $backup_file = '/var/lib/onionpress/backup-result.json';
        if ( file_exists( $backup_file ) ) {
            $backup_result = json_decode( file_get_contents( $backup_file ), true );
            if ( is_array( $backup_result ) ) {
                if ( ! empty( $backup_result['success'] ) ) {
                    $dl_filename = $backup_result['filename'] ?? '';
                    echo '<p><strong style="color:#16a34a">Backup created:</strong> <code>' . esc_html( $dl_filename ) . '</code>';
                    if ( $dl_filename ) {
                        $dl_url = admin_url( 'admin-ajax.php?action=onionpress_download_backup&file=' . urlencode( $dl_filename ) . '&_wpnonce=' . wp_create_nonce( 'onionpress_download_backup' ) );
                        echo ' &mdash; <a href="' . esc_url( $dl_url ) . '">Download</a>';
                    }
                    echo '</p>';
                } elseif ( ! empty( $backup_result['error'] ) ) {
                    echo '<p><strong style="color:#dc2626">Error:</strong> ' . esc_html( $backup_result['error'] ) . '</p>';
                }
            }
        }
        ?>
        <p class="description">Create a password-protected backup of your database, wp-content, Tor keys, and config. Your WordPress password encrypts the backup — you'll need it to restore.</p>
        <form method="post" style="margin-top: 8px;">
            <?php wp_nonce_field( 'onionpress_action', 'onionpress_action_nonce' ); ?>
            <input type="hidden" name="onionpress_action" value="create-backup">
            <table class="form-table" role="presentation">
                <tr>
                    <th scope="row"><label for="op_backup_password">Password for <?php echo esc_html( wp_get_current_user()->user_login ); ?></label></th>
                    <td>
                        <span class="op-password-wrap">
                            <input type="password" name="op_backup_password" id="op_backup_password" class="regular-text" required autocomplete="current-password">
                            <button type="button" class="op-eye-toggle" aria-label="Show password">&#128065;</button>
                        </span>
                        <p class="description">Enter your WordPress password to create the backup.</p>
                    </td>
                </tr>
            </table>
            <?php submit_button( 'Create Backup', 'secondary', 'submit', false ); ?>
        </form>

        <!-- Restore -->
        <hr>
        <h2>Restore</h2>
        <?php
        $restore_file = '/var/lib/onionpress/restore-result.json';
        if ( file_exists( $restore_file ) ) {
            $restore_result = json_decode( file_get_contents( $restore_file ), true );
            if ( is_array( $restore_result ) ) {
                if ( ! empty( $restore_result['success'] ) ) {
                    echo '<p><strong style="color:#16a34a">Restored:</strong> <code>' . esc_html( $restore_result['address'] ?? '' ) . '</code></p>';
                } elseif ( ! empty( $restore_result['error'] ) ) {
                    echo '<p><strong style="color:#dc2626">Error:</strong> ' . esc_html( $restore_result['error'] ) . '</p>';
                }
            }
        }
        ?>
        <p class="description">Restore from an OnionPress backup zip. This will <strong>overwrite</strong> your current database, wp-content, and Tor keys. OnionPress will restart automatically after restore.</p>
        <form method="post" enctype="multipart/form-data" style="margin-top: 8px;">
            <?php wp_nonce_field( 'onionpress_action', 'onionpress_action_nonce' ); ?>
            <input type="hidden" name="onionpress_action" value="restore-backup">
            <table class="form-table" role="presentation">
                <tr>
                    <th scope="row"><label for="op_restore_file">Backup File</label></th>
                    <td>
                        <input type="file" name="op_restore_file" id="op_restore_file" accept=".zip" required>
                        <p class="description">Upload your OnionPress backup .zip file.</p>
                    </td>
                </tr>
                <tr>
                    <th scope="row"><label for="op_restore_password">Backup Password</label></th>
                    <td>
                        <span class="op-password-wrap">
                            <input type="password" name="op_restore_password" id="op_restore_password" class="regular-text" required autocomplete="current-password">
                            <button type="button" class="op-eye-toggle" aria-label="Show password">&#128065;</button>
                        </span>
                        <p class="description">The WordPress password of the admin who created the backup.</p>
                    </td>
                </tr>
            </table>
            <?php submit_button( 'Restore from Backup', 'secondary', 'submit', false ); ?>
        </form>

        <!-- Vanity Address Generation removed (#256 phase 4b): the .onion address
             is chosen once at install (welcome screen) and is no longer changed on
             the fly — that path churned the onion service and risked clobbering the
             address. To use a different address, restore from a backup that has it. -->

        <!-- Import Key -->
        <hr>
        <h2>Import Onion Service Key</h2>
        <?php
        $import_file = '/var/lib/onionpress/import-result.json';
        if ( file_exists( $import_file ) ) {
            $import_result = json_decode( file_get_contents( $import_file ), true );
            if ( is_array( $import_result ) ) {
                if ( ! empty( $import_result['success'] ) ) {
                    echo '<p><strong style="color:#16a34a">Imported:</strong> <code>' . esc_html( $import_result['address'] ?? '' ) . '</code></p>';
                } elseif ( ! empty( $import_result['error'] ) ) {
                    echo '<p><strong style="color:#dc2626">Error:</strong> ' . esc_html( $import_result['error'] ) . '</p>';
                }
            }
        }
        ?>
        <p class="description">Import a pre-generated ed25519 key. This will <strong>replace</strong> your current onion address, forever. Not reversible. Paste the base32-encoded key or upload the <code>hs_ed25519_secret_key</code> file.</p>
        <form method="post" enctype="multipart/form-data" style="margin-top: 8px;" id="op-import-key-form">
            <?php wp_nonce_field( 'onionpress_action', 'onionpress_action_nonce' ); ?>
            <input type="hidden" name="onionpress_action" value="import-key-file">
            <table class="form-table" role="presentation">
                <tr>
                    <th scope="row"><label for="op_key_b32">Base32 Key</label></th>
                    <td>
                        <input type="text" name="op_key_b32" id="op_key_b32" class="large-text" placeholder="Paste base32-encoded key here...">
                        <p class="description">The base32-encoded 64-byte expanded ed25519 secret key.</p>
                    </td>
                </tr>
                <tr>
                    <th scope="row"><label for="op_key_file">Or Upload Key File</label></th>
                    <td>
                        <input type="file" name="op_key_file" id="op_key_file">
                        <p class="description">Upload <code>hs_ed25519_secret_key</code> from mkp224o output or a backup.</p>
                    </td>
                </tr>
            </table>
            <?php submit_button( 'Import Key', 'secondary', 'submit', false ); ?>
        </form>

    </div>
    <style>
        .op-password-wrap { display: inline-flex; align-items: center; }
        .op-password-wrap input { margin-right: 0; }
        .op-eye-toggle { background: none; border: 1px solid #8c8f94; border-left: 0; border-radius: 0 4px 4px 0; padding: 0 8px; cursor: pointer; font-size: 16px; line-height: 30px; height: 30px; color: #50575e; }
        .op-eye-toggle:hover { color: #135e96; }
        .op-password-wrap input.regular-text { border-radius: 4px 0 0 4px; }
    </style>
    <script>
        /* Client-side validation: highlight empty required fields */
        document.querySelectorAll('form').forEach(function(form) {
            form.addEventListener('submit', function(e) {
                var missing = false;
                form.querySelectorAll('input[required]').forEach(function(input) {
                    var label = form.querySelector('label[for="' + input.id + '"]');
                    if (!input.value && input.type !== 'file' || input.type === 'file' && !input.files.length) {
                        if (label) label.style.color = '#dc2626';
                        input.style.borderColor = '#dc2626';
                        missing = true;
                    } else {
                        if (label) label.style.color = '';
                        input.style.borderColor = '';
                    }
                });
                if (missing) e.preventDefault();
            });
            form.querySelectorAll('input[required]').forEach(function(input) {
                input.addEventListener('input', function() {
                    var label = form.querySelector('label[for="' + input.id + '"]');
                    if (label) label.style.color = '';
                    input.style.borderColor = '';
                });
                if (input.type === 'file') {
                    input.addEventListener('change', function() {
                        var label = form.querySelector('label[for="' + input.id + '"]');
                        if (label) label.style.color = '';
                        input.style.borderColor = '';
                    });
                }
            });
        });

        /* Import key form: require at least one of base32 or file */
        var ikForm = document.getElementById('op-import-key-form');
        if (ikForm) {
            ikForm.addEventListener('submit', function(e) {
                var b32 = ikForm.querySelector('#op_key_b32');
                var file = ikForm.querySelector('#op_key_file');
                var b32Label = ikForm.querySelector('label[for="op_key_b32"]');
                var fileLabel = ikForm.querySelector('label[for="op_key_file"]');
                if (!b32.value && !file.files.length) {
                    if (b32Label) b32Label.style.color = '#dc2626';
                    if (fileLabel) fileLabel.style.color = '#dc2626';
                    b32.style.borderColor = '#dc2626';
                    file.style.borderColor = '#dc2626';
                    e.preventDefault();
                } else {
                    if (b32Label) b32Label.style.color = '';
                    if (fileLabel) fileLabel.style.color = '';
                    b32.style.borderColor = '';
                    file.style.borderColor = '';
                }
            });
            ['op_key_b32', 'op_key_file'].forEach(function(id) {
                var el = document.getElementById(id);
                if (el) el.addEventListener(el.type === 'file' ? 'change' : 'input', function() {
                    var label = ikForm.querySelector('label[for="' + id + '"]');
                    if (label) label.style.color = '';
                    el.style.borderColor = '';
                });
            });
        }

        document.querySelectorAll('.op-eye-toggle').forEach(function(btn) {
            btn.addEventListener('click', function() {
                var input = this.previousElementSibling;
                if (input.type === 'password') {
                    input.type = 'text';
                    this.setAttribute('aria-label', 'Hide password');
                } else {
                    input.type = 'password';
                    this.setAttribute('aria-label', 'Show password');
                }
            });
        });

        /* Poll for action completion and update the notice */
        (function() {
            var notice = document.querySelector('.op-action-notice');
            if (!notice) return;
            var action = notice.getAttribute('data-op-action');
            if (!action) return;

            var poll = setInterval(function() {
                fetch(ajaxurl + '?action=onionpress_poll_action&op_action=' + encodeURIComponent(action))
                    .then(function(r) { return r.json(); })
                    .then(function(data) {
                        if (!data.done) return;
                        clearInterval(poll);

                        var p = notice.querySelector('p');
                        var result = data.result;

                        if (result && result.success === true) {
                            notice.className = 'notice notice-success is-dismissible';
                            var msg = result.message || 'Done!';
                            if (result.address) msg = 'Done! Address: ' + result.address;
                            if (result.filename) msg = 'Backup ready: ' + result.filename;
                            if (result.reachable === true) msg = 'Onion service is reachable (HTTP ' + (result.http_code || '200') + ')';
                            if (result.reachable === false) msg = 'Onion service is not reachable' + (result.error ? ': ' + result.error : '');
                            p.textContent = msg;
                        } else if (result && result.error) {
                            notice.className = 'notice notice-error is-dismissible';
                            p.textContent = 'Error: ' + result.error;
                        } else {
                            notice.className = 'notice notice-success is-dismissible';
                            p.textContent = 'Done!';
                            /* Reload to show updated state */
                            setTimeout(function() { location.reload(); }, 1500);
                        }

                        /* Add download link for backup */
                        if (result && result.success && result.filename) {
                            var link = document.createElement('a');
                            link.href = ajaxurl + '?action=onionpress_download_backup&file=' + encodeURIComponent(result.filename) + '&_wpnonce=<?php echo wp_create_nonce( "onionpress_download_backup" ); ?>';
                            link.textContent = ' Download';
                            link.style.marginLeft = '8px';
                            p.appendChild(link);
                        }
                    })
                    .catch(function() { /* server may be restarting — keep polling */ });
            }, 3000);
        })();

        /* Check for updates (async, doesn't stall page) */
        (function() {
            var el = document.getElementById('op-update-status');
            if (!el) return;
            var nonce = '<?php echo esc_js( wp_create_nonce( "onionpress_action" ) ); ?>';
            function vTag(s) {
                if (!s) return '';
                return /^v/i.test(s) ? s : 'v' + s;
            }
            function render() {
                el.innerHTML = '<span class="description">version: <?php echo esc_js( $op_current_display ); ?>, checking for updates...</span>';
                fetch(ajaxurl + '?action=onionpress_check_update')
                    .then(function(r) { return r.json(); })
                    .then(function(data) {
                        var cur = vTag(data.current || '<?php echo esc_js( $op_current_display ); ?>');
                        if (data.update && data.latest) {
                            el.innerHTML =
                                '<span class="description">version: ' + cur + ', update available: ' + vTag(data.latest) + '</span>' +
                                ' <form method="post" style="display:inline;margin-left:8px;">' +
                                '<input type="hidden" name="onionpress_action_nonce" value="' + nonce + '">' +
                                '<input type="hidden" name="onionpress_action" value="update">' +
                                '<button type="submit" class="button button-primary button-small">Update Now</button>' +
                                '</form>';
                        } else if (data.latest) {
                            el.innerHTML = '<span class="description">version: ' + cur + ", you're on the latest</span>";
                        } else {
                            el.innerHTML =
                                '<span class="description">version: ' + cur + ", couldn't check for updates</span>" +
                                ' <button type="button" class="button button-small" id="op-update-retry" style="margin-left:8px;">Retry</button>';
                            var btn = document.getElementById('op-update-retry');
                            if (btn) btn.addEventListener('click', render);
                        }
                    })
                    .catch(function() {
                        el.innerHTML =
                            '<span class="description">version: <?php echo esc_js( $op_current_display ); ?>, couldn\'t check for updates</span>' +
                            ' <button type="button" class="button button-small" id="op-update-retry" style="margin-left:8px;">Retry</button>';
                        var btn = document.getElementById('op-update-retry');
                        if (btn) btn.addEventListener('click', render);
                    });
            }
            render();
        })();
    </script>
    <?php
}

"""Which php the PHP-run tests may use, and the extensions it has.

A developer's box may have a bare php-cli (no zip, no curl) on PATH. A
harness that needs an extension then fails on `Class "ZipArchive" not
found` or `Undefined constant "CURLOPT_PROXY"`, which says nothing about
the code under test. Such a module skips with `php_with("zip")`, whose
reason names what is missing; CI's php has every extension, so there it
runs.

    from php_ext import PHP, php_with
    @unittest.skipUnless(*php_with("zip"))
"""

import functools
import shutil
import subprocess

PHP = shutil.which("php")


@functools.lru_cache(maxsize=None)
def php_has(extension):
    """True when `php` is on PATH and has `extension` loaded (asked once)."""
    if not PHP:
        return False
    try:
        r = subprocess.run(
            [PHP, "-r", "exit(extension_loaded($argv[1]) ? 0 : 1);", extension],
            capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def php_with(*extensions):
    """`(condition, reason)` for `unittest.skipUnless(*php_with(...))`."""
    if not PHP:
        return False, "php not installed"
    missing = [e for e in extensions if not php_has(e)]
    if missing:
        return False, "php without the %s extension%s (install php-%s)" % (
            ", ".join(missing), "s" if len(missing) > 1 else "",
            " php-".join(missing))
    return True, ""

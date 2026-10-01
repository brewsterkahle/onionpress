#!/usr/bin/env python3
"""Opt-in gate, liveness guard and sandboxes for the docker-backed tests.

test_bluesky_importer, test_mastodon_importer, test_twitter_importer and
test_wayback_sweep drive the real mu-plugins inside an
`onionpress-wordpress` container via `docker exec … wp`. They create
subsites, publish and delete posts, and rewrite options — in whichever
container the host `docker` CLI reaches. On a machine that also runs
OnionPress, that is the live site: on 2026-09-30 a plain
`python -m unittest discover tests` on such a Mac created three sandbox
subsites served at the live onion address, created and force-deleted posts
on the owner's real subsite, deleted its Wayback lock and back-off options,
and the live Wayback sweep then submitted the sandboxes to the public
Wayback Machine.

So every docker-backed test class derives from SandboxTestCase:

1. Opt-in. The class is skipped unless ONIONPRESS_INTEGRATION_TESTS=1, and
   a skipped class never touches Docker at all — the default run is fast
   and safe on any machine.

2. Liveness guard. Opted in, the first class to start checks the target
   once per process and raises UnsafeTargetError — an error, not a skip —
   if any of these hold, in this order:

   ~/.onionpress/config has ONIONNAME_REGISTERED=yes
       This machine registered a public onion name.
   ~/.onionpress/onion_address is not empty
       The menubar app on this machine has published an onion service.
   the container has /var/lib/onionpress/onion_address
       The launcher has published this WordPress as an onion service. It
       is also the file the Wayback sweep reads to build every URL it
       submits; without it the sweep cannot submit anything.
   a subsite other than the network root and SANDBOX_SLUGS
       Setup creates /<onionname>/ for the owner of every real install.
   the network root has onionpress_root_site=yes
       A branded install (onionpress.org, OnionHeaven) whose root site is
       its public site.

   The two ~/.onionpress signals describe this machine, not the container,
   so they also refuse when `docker` reaches some other daemon. That is the
   safe direction: run the suite where OnionPress is not installed. The
   checks stop at the first stage that finds something, so a machine with
   a live install is refused before Docker is asked anything. A probe that
   cannot answer — no docker CLI, no running container, WordPress missing
   or not multisite — refuses too.

3. Sandboxes. Each class gets a fresh subsite from SANDBOX_SLUGS, created
   non-public and with op_wayback_exclude=yes in the same call, so the
   Wayback plugin never visits, counts or schedules from it. The subsite —
   its tables, uploads and role grants — is deleted when the class
   finishes, even if setUpClass fails part-way.

A disposable target is a WordPress multisite with the mu-plugins from this
checkout and no onion service — no tor container, so nothing ever writes
the onion address file. docs/BUILDING.md, "Integration tests", has the
recipe.
"""

import json
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from onionpress import config as op_config  # noqa: E402

OPT_IN_ENV = "ONIONPRESS_INTEGRATION_TESTS"
WP_CONTAINER = "onionpress-wordpress"

# Every sandbox subsite a suite may create. The liveness guard accepts
# these and the network root, and nothing else.
SANDBOX_SLUGS = frozenset({
    "op-bluesky-test",
    "op-mastodon-test",
    "op-twitter-test",
    "op-wayback-test",
})

# The per-subsite option onionpress-wayback-archive.php honors.
WAYBACK_EXCLUDE_OPTION = "op_wayback_exclude"

CONTAINER_ONION_FILE = "/var/lib/onionpress/onion_address"

SKIP_REASON = (
    f"writes to whatever {WP_CONTAINER} container `docker` reaches, which "
    f"may be a live site; set {OPT_IN_ENV}=1 to run it against a disposable "
    "stack (see tests/wp_integration.py)"
)

_ADVICE = (
    "\nThese tests create subsites and posts in whatever "
    f"{WP_CONTAINER} container `docker` reaches. Run them on a machine "
    "without OnionPress, against a stack with no onion service — see "
    'docs/BUILDING.md, "Integration tests".'
)


class UnsafeTargetError(RuntimeError):
    """The container `docker` reaches is, or may be, a live install."""


def opted_in():
    return os.environ.get(OPT_IN_ENV) == "1"


def gate(cls):
    """Skip every test in `cls` unless this run opted in."""
    return unittest.skipUnless(opted_in(), SKIP_REASON)(cls)


def host_signals(data_dir=None):
    """Signs that this machine runs a real OnionPress install."""
    if data_dir is None:
        data_dir = os.path.join(os.path.expanduser("~"), ".onionpress")
    found = []
    config_file = os.path.join(data_dir, "config")
    if op_config.read_value(config_file, "ONIONNAME_REGISTERED").strip() == "yes":
        found.append(f"{config_file} has ONIONNAME_REGISTERED=yes — this "
                     "machine registered a public onion name")
    cached_address = os.path.join(data_dir, "onion_address")
    try:
        with open(cached_address, encoding="utf-8", errors="replace") as f:
            has_address = bool(f.read().strip())
    except FileNotFoundError:
        has_address = False
    if has_address:
        found.append(f"{cached_address} holds an onion address — this "
                     "machine publishes an onion service")
    return found


def _run(argv, **kwargs):
    return subprocess.run(
        argv, capture_output=True, text=True, encoding='utf-8',
        errors='replace', **kwargs,
    )


def _wp_argv(args, url=None):
    cmd = ["wp"] + args + ["--path=/var/www/html", "--allow-root"]
    if url:
        cmd.append("--url=" + url)
    return ["docker", "exec", WP_CONTAINER] + cmd


# Runs on the network root. Marker-prefixed so a PHP notice printed by
# some plugin can't be mistaken for the answer.
_PROBE_PHP = r"""
global $wpdb;
$out = array( 'multisite' => is_multisite() );
if ( $out['multisite'] ) {
    $out['main']  = (int) get_main_site_id();
    $out['sites'] = $wpdb->get_results( "SELECT blog_id, path FROM {$wpdb->blogs}", ARRAY_A );
    $out['root_site'] = (string) get_blog_option( $out['main'], 'onionpress_root_site', '' );
}
echo 'OPPROBE:' . json_encode( $out );
"""


def target_signals():
    """Signs that the container `docker` reaches serves a live site.

    Raises UnsafeTargetError when the probe cannot answer."""
    if not shutil.which("docker"):
        raise UnsafeTargetError("there is no docker CLI on PATH")
    r = _run(["docker", "inspect", WP_CONTAINER, "--format={{.State.Running}}"],
             timeout=15)
    if r.returncode != 0 or r.stdout.strip() != "true":
        raise UnsafeTargetError(
            f"no running {WP_CONTAINER} container is reachable through `docker`")

    # A plain file test, before WordPress itself is loaded on what may be
    # a live site.
    r = _run(["docker", "exec", WP_CONTAINER, "sh", "-c",
              f"if [ -e {CONTAINER_ONION_FILE} ]; then echo present; "
              "else echo absent; fi"], timeout=15)
    state = r.stdout.strip()
    if state == "present":
        return [f"the container has {CONTAINER_ONION_FILE} — this WordPress "
                "is served as an onion service, and the Wayback sweep can "
                "submit its pages"]
    if state != "absent":
        raise UnsafeTargetError(
            f"could not check {CONTAINER_ONION_FILE} in the container: "
            f"{(r.stderr or r.stdout).strip()[:200]}")

    r = _run(_wp_argv(["eval", _PROBE_PHP]), timeout=60)
    _, marker, answer = r.stdout.rpartition("OPPROBE:")
    try:
        if not marker:
            raise ValueError("no answer")
        probe = json.loads(answer.splitlines()[0])
    except (IndexError, ValueError):
        raise UnsafeTargetError(
            "could not query WordPress in the container: "
            f"{(r.stderr or r.stdout).strip()[-200:]}") from None
    if not probe.get("multisite"):
        raise UnsafeTargetError(
            "WordPress in the container is not a multisite install, so its "
            "content cannot be told apart from the test sandboxes")

    found = []
    sandbox_paths = {f"/{slug}/" for slug in SANDBOX_SLUGS}
    others = sorted(
        s["path"] for s in probe["sites"]
        if int(s["blog_id"]) != probe["main"] and s["path"] not in sandbox_paths
    )
    if others:
        found.append(f"subsites other than the test sandboxes exist "
                     f"({', '.join(others[:5])}) — setup creates "
                     "/<onionname>/ for the owner of every real install")
    if probe.get("root_site") == "yes":
        found.append("the network root has onionpress_root_site=yes — a "
                     "branded install whose root site is its public site")
    return found


def _check_target():
    """'' if the target is disposable, else the reason it is refused."""
    try:
        signals = host_signals() or target_signals()
    except UnsafeTargetError as e:
        return f"Refusing to run the integration tests: {e}.{_ADVICE}"
    except (OSError, subprocess.SubprocessError) as e:
        return (f"Refusing to run the integration tests: the liveness "
                f"probe failed ({e!r}).{_ADVICE}")
    if signals:
        return ("Refusing to run the integration tests — the target looks "
                "like a live OnionPress install:\n"
                + "\n".join(f"  - {s}" for s in signals) + _ADVICE)
    return ""


_verdict = None


def require_disposable_target():
    """Raise unless this run opted in and the target is disposable. The
    target is checked once per process; every later call repeats the
    verdict."""
    global _verdict
    if not opted_in():
        raise RuntimeError(f"the integration helpers need {OPT_IN_ENV}=1")
    if _verdict is None:
        _verdict = _check_target()
    if _verdict:
        raise UnsafeTargetError(_verdict)


def wp(args, url=None, **kwargs):
    require_disposable_target()
    return _run(_wp_argv(args, url), **kwargs)


def wp_eval(php, url):
    """Run PHP inside WP, return stdout (stripped)."""
    r = wp(["eval", php], url=url, timeout=90)
    return r.stdout.strip()


def _check_slug(slug):
    if slug not in SANDBOX_SLUGS:
        raise ValueError(f"{slug!r} is not in SANDBOX_SLUGS, so the liveness "
                         "guard would take it for a real subsite")


def _sandbox_url(slug):
    r = wp(["site", "list", "--fields=blog_id,path,url", "--format=json"],
           timeout=15)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(
            f"wp site list failed: {(r.stderr or r.stdout).strip()[:200]}")
    for s in json.loads(r.stdout):
        if s.get("path") == f"/{slug}/":
            return s["url"].rstrip("/") + "/"
    return None


def close_sandbox(slug):
    """Delete sandbox subsite `slug` (tables, uploads, role grants) if it
    exists, and make sure it is gone."""
    _check_slug(slug)
    if _sandbox_url(slug) is None:
        return
    r = wp(["site", "delete", f"--slug={slug}", "--yes"], timeout=60)
    if _sandbox_url(slug) is not None:
        raise RuntimeError(f"could not delete sandbox subsite {slug!r}: "
                           f"{(r.stderr or r.stdout).strip()[:200]}")


def open_sandbox(slug, title):
    """Create sandbox subsite `slug` afresh and return its URL.

    wpmu_create_blog() writes the Wayback exclusion while it initializes
    the site, before WordPress adds the default post and page — so not
    even those are ever submitted. This is the call `wp site create`
    makes, minus any way to pass that option."""
    close_sandbox(slug)  # whatever a crashed run left behind
    out = wp_eval(f"""
    $network = get_network();
    $admins  = get_super_admins();
    $owner   = $admins ? get_user_by( 'login', reset( $admins ) ) : false;
    $id = wpmu_create_blog(
        $network->domain, $network->path . '{slug}/', '{title}',
        $owner ? $owner->ID : 1,
        array( 'public' => 0, '{WAYBACK_EXCLUDE_OPTION}' => 'yes' ),
        $network->id
    );
    echo is_wp_error( $id ) ? 'ERR:' . $id->get_error_message()
        : 'EXCLUDE:' . get_blog_option( $id, '{WAYBACK_EXCLUDE_OPTION}' );
    """, None)
    if out.splitlines()[-1:] != ["EXCLUDE:yes"]:
        raise RuntimeError(f"could not create sandbox subsite {slug!r}: {out[-300:]}")
    url = _sandbox_url(slug)
    if url is None:
        raise RuntimeError(f"sandbox subsite {slug!r} was created but is not listed")
    return url


def _missing_php_functions(names):
    if not names:
        return []
    listed = ", ".join(f"'{n}'" for n in names)
    out = wp_eval(f"""
    $missing = array();
    foreach ( array( {listed} ) as $f ) {{
        if ( ! function_exists( $f ) ) $missing[] = $f;
    }}
    echo 'MISSING:' . implode( ',', $missing );
    """, None)
    lines = [ln for ln in out.splitlines() if ln.startswith("MISSING:")]
    if not lines:
        raise RuntimeError(f"could not check the container's mu-plugins: {out[-300:]}")
    return [f for f in lines[-1][len("MISSING:"):].split(",") if f]


@gate
class SandboxTestCase(unittest.TestCase):
    """Base for every docker-backed test class: skipped unless opted in,
    refused on a live target, and run against a fresh sandbox subsite
    (`cls.url`) that is deleted when the class finishes."""

    SANDBOX_SLUG = None
    SANDBOX_TITLE = None
    # PHP functions the class needs from the container's mu-plugins,
    # checked before its sandbox is created.
    REQUIRED_PHP_FUNCTIONS = ()

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _check_slug(cls.SANDBOX_SLUG)
        require_disposable_target()
        missing = _missing_php_functions(cls.REQUIRED_PHP_FUNCTIONS)
        if missing:
            raise RuntimeError(
                f"the mu-plugins in {WP_CONTAINER} lack {', '.join(missing)} "
                "— install the ones from this checkout first")
        cls.addClassCleanup(close_sandbox, cls.SANDBOX_SLUG)
        cls.url = open_sandbox(cls.SANDBOX_SLUG, cls.SANDBOX_TITLE)

"""Image overrides: which images the stack runs instead of its pins, and
whether those are built on this machine (issue #475).

Split out of onionpress.containers so a process can ask without importing
any docker code: the menubar asks using_local_images before saying "all
container images are up to date", and it loads no docker module
(tests/test_mac_daemon.py's TestMenubarIsAView checks the closure).
onionpress.containers re-exports every name here.
"""

import os


def _default_config_file() -> str:
    # Same path resolve_paths() uses when no data_dir override is given. Kept
    # as a plain join rather than a resolve_paths() call so importing this
    # module has no side effects and no dependency on the app bundle.
    return os.path.join(os.path.expanduser("~"), ".onionpress", "config")


def image_override(name: str, config_file: str | None = None) -> str | None:
    """An image override from the environment, or failing that from the config.

    `config_file` defaults to ~/.onionpress/config. Callers that already hold
    an OnionPressPaths should pass `paths.config_file` — resolve_paths()
    supports a data_dir override, and a hardcoded home path would silently
    read the wrong file under one.

    Reading the config file matters on macOS, and is not merely a
    convenience. The bash launcher exports these after reading
    ~/.onionpress/config, but the MenubarApp is that launcher's PARENT — it
    spawns the launcher, never the reverse — so a child's exports can never
    reach it. Without this, a developer who followed the documented route of
    putting ONIONPRESS_TOR_IMAGE in ~/.onionpress/config still had the
    menubar's own `docker compose pull` overwrite their local image on every
    launch, and be told the images were up to date.

    The config is parsed the same way the launchers parse it: first matching
    `KEY=` line, everything after the first `=`.
    """
    value = os.environ.get(name)
    if value:
        return value
    try:
        with open(config_file or _default_config_file(),
                  "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip() or None
    except OSError:
        pass
    return None


def using_local_images(config_file: str | None = None) -> bool:
    """True when the stack points at images built on this machine.

    build/build-images.sh prints ONIONPRESS_TOR_IMAGE / ONIONPRESS_WORDPRESS_IMAGE
    for a locally built stack. Every image pull is gated on this: without the
    gate a pull overwrites the local tag with the registry's copy, so a
    developer builds an image, starts the app, and silently tests someone
    else's build.

    A reference that still points at ghcr.io is a deliberate pin, not a local
    build, so it does not count. Mirrors using_local_images() in both bash
    launchers — change all three together.
    """
    for name in ("ONIONPRESS_TOR_IMAGE", "ONIONPRESS_WORDPRESS_IMAGE"):
        ref = image_override(name, config_file)
        if ref and not ref.startswith("ghcr.io/"):
            return True
    return False

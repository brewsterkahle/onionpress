#!/usr/bin/env python3
"""`e2e/run.sh --probe`: what this host could test, and with which build.
Starts nothing, writes nothing."""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import PROJECT_DIR, platforms, safety  # noqa: E402
from harness.session import real_home  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mac-app", default="")
    ap.add_argument("--platforms", default="")
    args = ap.parse_args()

    app_hint = args.mac_app or next((p for p in (os.path.join(PROJECT_DIR, "OnionPress.app"),
                                                 "/Applications/OnionPress.app") if os.path.isdir(p)), None)
    facts = platforms.probe_host(mac_app=app_hint)
    offer_list = platforms.offers(facts)
    wanted = [p for p in args.platforms.split(",") if p] or None
    chosen, not_tested = platforms.choose(offer_list, wanted)

    built = [p for p in (args.mac_app, os.path.join(PROJECT_DIR, "OnionPress.app"),
                         os.path.join(PROJECT_DIR, "build", "onionpress.dmg")) if p and os.path.exists(p)]
    if "macos" in chosen and not built:
        del chosen["macos"]
        not_tested["macos"] = ("no Mac build: run `make dmg`, or pass --mac-app, --mac-dmg "
                               "or --release to e2e/run.sh")
    print(f"Host: {facts.label}")
    for line in platforms.describe(offer_list):
        print(line)
    print()
    for plat in platforms.PLATFORMS:
        label = platforms.PLATFORM_LABELS[plat]
        if plat in chosen:
            print(f"Would test:     {label} ({chosen[plat].kind})")
        else:
            print(f"Would not test: {label} — {not_tested[plat]}")
    if "macos" in chosen:
        print(f"Mac build:      {built[0]}")
    if "debian" in chosen:
        print("Debian build:   built in the VM from this checkout (or pass --deb/--release)")
    live = safety.live_onion_address(real_home())
    if live:
        print(f"Live install:   {live} — test nodes are isolated from it and refused if they reach it")


if __name__ == "__main__":
    main()

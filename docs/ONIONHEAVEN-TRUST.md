# OnionHeaven and the site key: the trust statement and the options

Issue #392, written 2026-10-26 for a decision. Nothing here is built yet
beyond the README statement.

## What is true today

- With `REGISTER_WITH_ONIONHEAVEN` unset or `yes` (the default), every
  node's heartbeat (`/online`, every 60 s) carries `arti_key_pem`, the
  onion service's private key, and the hub stores it unencrypted under
  `keys/<address>/` on the hub data volume.
- The hub can, at any moment, publish its own descriptor for any
  registrant's address and serve anything there, not only a Wayback
  redirect. The Tor network cannot tell the hub from the owner: the key is
  the identity.
- `/unregister` asks the hub to delete its copy. The node cannot verify
  that it did.
- A node can notice a second publisher of its address while it is up
  (`HealthChecker.classify_wedge` → `desc-other`; the takeover page's
  `X-OnionHeaven-Takeover` header) but cannot win: two publishers of one
  address alternate on the HSDirs.
- Everything else in the hub (signed heartbeats, the key-belongs-to-address
  check, the farm controller's allowlist, capability drops, the hub volume
  off the web-facing containers, the replay marks, the fleet brake, the
  registration caps) protects the hub from outsiders and the registrants
  from each other. None of it protects a registrant from the hub.

So the trust statement is: **registering means trusting the hub's
operator and the hub's security with your site's identity for as long as
the hub holds the key, and a compromise of the hub is a compromise of every
registered site's identity at once.** The README now says this (the
"What the hub can do with your key" paragraph).

## Options, cheapest first

1. **Say it, and leave the design as it is.** The README paragraph, the
   Settings dialog's and page's wording, `onionpress onionheaven status`
   and install.sh's banner all say the key is sent; they should say what
   the hub can do with it (the README does now; the others still say only
   that it is sent). Cost: words. Protects: nobody, but nobody is misled.

2. **A loud alarm in the apps on `desc-other`.** The probe already
   classifies "the HSDirs serve a descriptor of another lineage than
   ours". Today that is a log line and a republish. A native notification
   ("Someone else is publishing your address") and a line in the Settings
   page and status.json would make a hub-side impersonation visible to
   the owner within minutes while the site is up. Cost: a day. Protects:
   detection, not prevention. False positives: the benign window after
   the hub's own takeover ends (the node's DEL+ADD) and period skew; the
   class already distinguishes `desc-current` and `desc-stale`, so the
   alarm would fire only on `desc-other` persisting past one republish.

3. **A signed receipt per takeover.** The hub signs (with its own key)
   a statement "I published <address> from <time> because <reason>" and
   serves it at `/status/<address>`; the node fetches and logs it, and
   the apps show a takeover history. Cost: two days. Protects: audit
   after the fact; a hub that publishes without a receipt is caught by
   option 2, a hub that lies in the receipt is not caught.

4. **Seal the stored key to a key only a takeover worker holds.** The
   hub's API container (which faces Tor) stores `crypto_box_seal(key,
   worker_pub)`; only the worker containers (no inbound network) can open
   it, at takeover time, in memory. Cost: a week (key distribution to
   workers through the farm controller, rotation, the backup's sealed
   format already exists for the recovery key). Protects: a compromise of
   the API container alone no longer yields every key; a compromise of
   the host, or the operator, still does.

5. **Threshold or escrow split.** The node splits its key (or a
   per-site delegation) between the hub and a second party so the hub
   alone cannot sign a descriptor. Tor's v3 onion service descriptors are
   signed by a per-period blinded key derived from the identity key; a
   2-of-2 Ed25519 signing (FROST-style) between hub and a co-signer is
   possible in principle but means a second always-on party and a
   protocol the existing watchdog/queue-manager pipeline does not have.
   Cost: months, and a research-grade design. Protects: the hub alone
   cannot impersonate. This is the only option that changes the trust
   statement.

6. **Opt-in instead of opt-out.** Reverses the 2026-10-21 decision.
   Cost: words and a prompt. Protects: the people who would have said no.

## What this PR does

- README: the trust statement (above), under "OnionHeaven".
- This document.

## What it does not do

Options 2 to 6. Option 2 is the next thing worth building whatever the
answer on the rest; it is small and makes the trade visible.

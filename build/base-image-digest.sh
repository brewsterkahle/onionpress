#!/bin/bash
# Resolve an image tag to its multi-arch INDEX digest, with no Docker daemon.
#
# WHY
#   The Dockerfiles pin every base image as tag@digest, and the digest MUST be
#   the multi-arch index (one digest that resolves on both amd64 and arm64),
#   never a per-platform manifest digest — that one resolves on the
#   maintainer's Mac and 404s on the other CI runner. `docker buildx imagetools
#   inspect` reports it, but needs a daemon and buildx, and the bundled
#   OnionPress docker CLI has neither. The registry API needs only curl, but
#   the token dance differs per registry: Docker Hub hands out anonymous
#   tokens from auth.docker.io, the Tor Project's containers.torproject.org
#   from gitlab.torproject.org/jwt/auth. Both announce the realm in the 401's
#   WWW-Authenticate header, so this script reads it from there and works for
#   any registry that speaks the standard token flow.
#
# USAGE
#   build/base-image-digest.sh containers.torproject.org/tpo/onion-services/onimages/tor:trixie
#   build/base-image-digest.sh docker:29.8.1-cli
#   build/base-image-digest.sh wordpress:latest
#
#   Prints the pinned reference (tag@sha256:…) on stdout and the platforms it
#   carries on stderr. Exits 1 if the tag resolves to a single-platform
#   manifest, because pinning that is exactly the mistake this exists to
#   prevent. See docs/BUILDING.md, "Bumping a pinned input".

set -euo pipefail

REF="${1:-}"
if [ -z "$REF" ] || [ "$REF" = "-h" ] || [ "$REF" = "--help" ]; then
    sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
fi

python3 - "$REF" <<'PY'
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

INDEX_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
)
ACCEPT = ", ".join(INDEX_TYPES + (
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))

ref = sys.argv[1]
if "@" in ref:
    ref = ref.split("@", 1)[0]  # already pinned: re-resolve the tag part
first, _, rest = ref.partition("/")
if rest and ("." in first or ":" in first or first == "localhost"):
    host, path = first, rest
else:
    host, path = "docker.io", ref
if host == "docker.io":
    host = "registry-1.docker.io"
    if "/" not in path:
        path = "library/" + path
repo, _, tag = path.rpartition(":")
if not repo:
    repo, tag = path, "latest"
url = f"https://{host}/v2/{repo}/manifests/{tag}"


def fetch(headers):
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.headers, err.read()


headers = {"Accept": ACCEPT}
status, hdrs, body = fetch(headers)
if status == 401:
    challenge = hdrs.get("WWW-Authenticate", "")
    params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
    if not challenge.startswith("Bearer") or "realm" not in params:
        sys.exit(f"ERROR: {host} wants authentication this script does not "
                 f"speak: {challenge!r}")
    query = {k: v for k, v in params.items() if k in ("service", "scope")}
    token_url = params["realm"] + "?" + urllib.parse.urlencode(query)
    with urllib.request.urlopen(token_url, timeout=60) as resp:
        tok = json.load(resp)
    headers["Authorization"] = "Bearer " + (tok.get("token") or tok["access_token"])
    status, hdrs, body = fetch(headers)
if status != 200:
    sys.exit(f"ERROR: {url} returned HTTP {status}: {body[:200]!r}")

digest = hdrs.get("Docker-Content-Digest", "")
ctype = hdrs.get("Content-Type", "").split(";")[0].strip()
if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
    sys.exit(f"ERROR: no Docker-Content-Digest header in the response for {url}")
if ctype not in INDEX_TYPES:
    sys.exit(f"ERROR: {ref} is a single-platform manifest ({ctype}), not a "
             "multi-arch index. Pinning it would 404 on the other architecture.")

manifest = json.loads(body)
platforms = sorted(
    f"{m['platform']['os']}/{m['platform']['architecture']}"
    + (f"/{m['platform']['variant']}" if m["platform"].get("variant") else "")
    for m in manifest.get("manifests", [])
    if m.get("platform", {}).get("os") != "unknown"  # attestation manifests
)
print(f"{ref}: index for {', '.join(platforms)}", file=sys.stderr)
print(f"{ref}@{digest}")
PY

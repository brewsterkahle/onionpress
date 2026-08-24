#!/usr/bin/env bash
#
# test-receiver.sh — end-to-end smoke test for the OnionPress static receiver.
#
# Exercises the wire contract (docs/static-publish-protocol.md) exactly as a publisher
# client does: probe GET /status, tar a fixture with `tar -cf x -C dir .`,
# POST /generation, POST /commit, then confirm the static file is served at
# the site root ahead of WordPress.
#
# PREREQUISITE: OnionPress must already be running locally with a provisioned
# WordPress (the onionpress-static-receiver mu-plugin installed). Start the app,
# wait for the site to come up, then run this script. It talks to the receiver
# over the host loopback port map — no Tor required.
#
# Usage:
#   ./test-receiver.sh                # auto-discover the port
#   RECEIVER_PORT=8080 ./test-receiver.sh
#
# Exit status is 0 only if every step passes.

set -u

# Candidate ports: OnionPress offsets each additional macOS user by +10000
# (see the multi-user notes in CLAUDE.md). Port discovery mirrors publisher
# clients: first port whose /status returns a body containing receiver_version
# wins.
CANDIDATE_PORTS=(8080 18080 28080 38080 48080)

pass=0
fail=0
ok()   { printf 'PASS  %s\n' "$1"; pass=$((pass + 1)); }
bad()  { printf 'FAIL  %s\n' "$1"; fail=$((fail + 1)); }
info() { printf '----  %s\n' "$1"; }

# Minimal JSON string-field reader (keeps the script dependency-free; jq is
# not assumed to be installed).
json_str() { # $1=json  $2=key -> value (empty if absent)
  printf '%s' "$1" \
    | grep -oE "\"$2\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" \
    | head -n1 \
    | sed -E "s/.*:[[:space:]]*\"([^\"]*)\"/\1/"
}
json_has_true() { # $1=json $2=key -> 0 if "key":true
  printf '%s' "$1" | grep -qE "\"$2\"[[:space:]]*:[[:space:]]*true"
}

# --- Step 0: discover the receiver -----------------------------------------
BASE=""
PORT=""
try_ports=("${CANDIDATE_PORTS[@]}")
if [ -n "${RECEIVER_PORT:-}" ]; then
  try_ports=("$RECEIVER_PORT")
fi
for p in "${try_ports[@]}"; do
  body="$(curl -s --max-time 5 "http://127.0.0.1:${p}/wp-json/onionpress/v1/status" 2>/dev/null || true)"
  if printf '%s' "$body" | grep -q 'receiver_version'; then
    PORT="$p"
    BASE="http://127.0.0.1:${p}"
    break
  fi
done

if [ -z "$BASE" ]; then
  bad "discover receiver — no port in {${CANDIDATE_PORTS[*]}} answered /status with receiver_version. Is OnionPress running?"
  echo
  echo "RESULT: ${pass} passed, ${fail} failed"
  exit 1
fi
ok "discover receiver on port ${PORT}"
API="${BASE}/wp-json/onionpress/v1"

# --- Step 1: GET /status ----------------------------------------------------
status="$(curl -s --max-time 5 "${API}/status")"
info "status: ${status}"
if printf '%s' "$status" | grep -q '"receiver_version"'; then
  ok "GET /status returns receiver_version"
else
  bad "GET /status missing receiver_version"
fi
ONION="$(json_str "$status" onion_address)"
info "onion_address: ${ONION:-<none yet>}"

# --- Step 2: build a fixture generation and POST /generation ----------------
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
MARKER="op-receiver-e2e-$$-$RANDOM"
mkdir -p "$WORK/fixture/assets"
printf '<!doctype html><title>e2e</title><h1>%s</h1>' "$MARKER" > "$WORK/fixture/index.html"
printf '<!doctype html><p>%s about</p>' "$MARKER"           > "$WORK/fixture/about.html"
printf 'body{color:#0a0}' > "$WORK/fixture/assets/site.css"

# Exactly the command a publisher client uses: the leading "."
# emits a `.` self-entry that PharData cannot extract — the receiver's own
# streaming tar reader must handle it.
TAR="$WORK/gen.tar"
( cd "$WORK/fixture" && tar -cf "$TAR" -C . . )

# Multipart carrier, exactly as the contract mandates (receiver_version 2.0
# dropped the raw application/x-tar body). No manual Content-Type: curl
# generates the multipart boundary itself.
GENID="gen-$(date +%s)"
gen_resp="$(curl -s --max-time 30 -X POST \
  -F "tar=@${TAR}" \
  "${API}/generation?id=${GENID}")"
info "generation: ${gen_resp}"
if json_has_true "$gen_resp" ok; then
  ok "POST /generation accepted ${GENID}"
else
  bad "POST /generation rejected: ${gen_resp}"
fi

# --- Step 3: POST /commit ---------------------------------------------------
commit_resp="$(curl -s --max-time 30 -X POST \
  -H 'Content-Type: application/json' \
  --data "{\"generation\":\"${GENID}\"}" \
  "${API}/commit")"
info "commit: ${commit_resp}"
if json_has_true "$commit_resp" ok; then
  ok "POST /commit flipped current to ${GENID}"
else
  bad "POST /commit failed: ${commit_resp}"
fi
COMMIT_URL="$(json_str "$commit_resp" url)"
info "committed url: ${COMMIT_URL:-<none>}"

# --- Step 4: confirm the static file is served at the site root -------------
# Same Apache, same server-scope <Directory> block, so the loopback root is
# the faithful equivalent of the onion root. The response body must contain
# THIS run's marker (proving the freshly committed static file wins over the
# WordPress front controller).
root="$(curl -s --max-time 10 "${BASE}/")"
if printf '%s' "$root" | grep -q "$MARKER"; then
  ok "GET / serves the committed static index.html (static wins over WordPress)"
else
  bad "GET / did not serve the static generation (marker '${MARKER}' absent)"
  info "root body was: $(printf '%s' "$root" | head -c 200)"
fi

# A nested asset must also come from the generation.
css="$(curl -s --max-time 10 "${BASE}/assets/site.css")"
if printf '%s' "$css" | grep -q 'color:#0a0'; then
  ok "GET /assets/site.css serves the static asset"
else
  bad "GET /assets/site.css did not serve the static asset"
fi

# The REST API must NOT be shadowed by static serving.
still="$(curl -s --max-time 5 "${API}/status")"
if printf '%s' "$still" | grep -q '"receiver_version"'; then
  ok "GET /wp-json/... still reaches the receiver after commit (REST not shadowed)"
else
  bad "GET /wp-json/... was shadowed by static serving after commit"
fi

# --- Optional / MANUAL: fetch the real onion root through Tor ----------------
# The loopback check above proves the static-first Apache config; the onion
# path uses the SAME server-scope <Directory> block, so it behaves identically.
# To verify over Tor as well (needs the running stack), from the host run:
#
#   docker exec onionpress-tor sh -c \
#     'command -v curl >/dev/null 2>&1 \
#        && curl -s --socks5-hostname 127.0.0.1:9050 --max-time 60 http://'"${ONION:-<addr>.onion}"'/ \
#        || printf "GET / HTTP/1.1\r\nHost: '"${ONION:-<addr>.onion}"'\r\nConnection: close\r\n\r\n" \
#           | socat -t 60 - SOCKS4A:127.0.0.1:'"${ONION:-<addr>.onion}"':80,socksport=9050'
#
# and confirm the response contains the committed marker. Left manual because
# it depends on a live onion circuit and is not part of the loopback contract.
info "MANUAL: onion fetch over Tor — see the commented docker exec recipe above"

echo
echo "RESULT: ${pass} passed, ${fail} failed"
[ "$fail" -eq 0 ]

# OnionPress static-publish protocol (receiver v2.0)

The static receiver (`app/Resources/plugins/onionpress-static-receiver.php`, a
mu-plugin) lets a static-site publisher running on the same machine — any
static site generator's deploy step — publish a pre-rendered site into
OnionPress over loopback REST. The published generation is served **ahead of
WordPress** by `onionpress-static-site.conf`; WordPress keeps serving anything
the generation doesn't shadow (`/wp-admin`, `/wp-json`, subsites).

This document is the wire contract. Both sides implement exactly this.

## Transport

- Publisher → receiver over plain HTTP on loopback. No auth token: trust is
  "same machine", enforced by the permission callback below plus the compose
  file publishing the port on `127.0.0.1` only (`ONIONPRESS_BIND_HOST`).
- Base URL: `http://127.0.0.1:<port>/wp-json/onionpress/v1`
- Port discovery: probe `GET /status` on ports **8080, 18080, 28080, 38080,
  48080** (OnionPress offsets each additional user by +10000). The first port
  whose `/status` returns a JSON body containing `receiver_version` wins.

## Generation id

Chosen by the publisher, e.g. `gen-<unix_seconds>`. The receiver treats it as
an opaque directory basename and rejects anything containing `/`, `..`, or NUL
(`onionpress_static_valid_id`). A generation id is single-use: re-POSTing an
id that already exists is a 409.

## Endpoints

### GET /status  (no body)

200 →

```json
{
  "onion_address": "<addr>.onion",
  "current_generation": "gen-1699999999" | null,
  "receiver_version": "2.0",
  "onion_reachable": true | false | null,
  "onion_http_code": "301" | "takeover" | "000:rc=28" | null
}
```

- `onion_address` is read from `/var/lib/onionpress/onion_address` (fallback:
  `status.json`'s `onion_address`).
- `current_generation` = `basename(readlink(/var/www/html/site/current))`,
  or null before the first commit.
- `onion_reachable`/`onion_http_code` (since 1.1) mirror `status.json`'s
  tri-state external-reachability check: `null` means the health checker has
  not completed a Tor-network probe since coming up — a client must never
  read `null` as "confirmed unreachable"; only an explicit `false` means
  checked-and-unreachable. Both are strings, never numbers — `onion_http_code`
  carries an HTTP status, the `"takeover"` sentinel (OnionHeaven hub takeover
  response), or `"000:rc=<curl exit code>"` for a transport failure.
- Version history: `1.1` added the reachability fields; `1.2` added the
  multipart carrier; `2.0` removed the legacy raw-body carrier.

### POST /generation?id=<genid>

Carrier: **`multipart/form-data` with the tar in a part named `tar`** — a
plain (not gzipped) tar of the generation directory's CONTENTS at tar root:

```
tar -cf site.tar -C <generation-dir> .
curl -F tar=@site.tar 'http://127.0.0.1:8080/wp-json/onionpress/v1/generation?id=<genid>'
```

Do NOT set `Content-Type` manually; curl generates the multipart boundary
itself, and overriding it breaks PHP's parser.

Why multipart is the only carrier (raw `application/x-tar` was removed at
2.0): WordPress's REST server calls `set_body(self::get_raw_data())` —
`file_get_contents('php://input')` — for EVERY request, before the route or
even the permission callback runs. A raw tar body therefore buffered the whole
site into a PHP string and could exhaust `memory_limit` before the receiver
could reject it. With multipart, PHP's `rfc1867.c` registers a NULL post
reader: `php://input` is empty and the part streams straight to
`upload_tmp_dir` at constant memory. Symmetric win on the client: `curl -F`
streams from disk, `curl --data-binary` buffers ~2x the file in curl's RSS.

Receiver behavior:

- Reads `$request->get_file_params()['tar']` and checks
  `$f['error'] === UPLOAD_ERR_OK` FIRST — rfc1867.c cancels an oversize part
  mid-write and reports `UPLOAD_ERR_INI_SIZE` rather than failing the request,
  so an unchecked `is_uploaded_file()` would silently accept a truncated tar.
  Then `is_uploaded_file()`, then `move_uploaded_file()` (or a checked
  streaming copy for a cross-device tmp dir) into
  `/var/www/html/site-generations/<genid>.tar`.
- `UPLOAD_ERR_*` → HTTP status: `INI_SIZE`/`FORM_SIZE` → 413 (message names
  `upload_max_filesize`/`MAX_FILE_SIZE`); `PARTIAL`/`NO_FILE`/unknown → 400;
  `NO_TMP_DIR`/`CANT_WRITE`/`EXTENSION` → 500.
- **Extraction: streaming tar reader, NOT `PharData`.** `PharData::extractTo`
  errors "Cannot extract '.'" on the `.` self-entry that `tar -cf x -C <dir> .`
  always produces, so it fails on every real upload. The hardened streaming
  reader skips the `.` entry and runs its guards inline, failing closed on
  anything not positively a regular file or directory: hard/symlinks,
  devices/FIFOs, absolute paths, `..` traversal (including via GNU longname
  and pax `path=` records), embedded NUL, and non-ustar headers. Per-file
  writes fail closed too — a short `fread`/`fwrite` or failed `fclose`
  (e.g. ENOSPC) fails the whole extraction rather than landing a silently
  truncated file. Reject-path coverage: `tests/test-static-receiver-upload.php`.
- The tar is extracted into `<genid>.tmp/`, atomically `rename()`d to
  `<genid>/`, and the tar deleted.
- 200 → `{ "ok": true, "generation": "<genid>" }`;
  4xx/413/500 → `{ "ok": false, "error": "…" }`.

### POST /commit   Content-Type: application/json

- Body `{ "generation": "<genid>" }`.
- Collision guard: reject (409) if any top-level name in the generation
  collides with a reserved name {`wp-admin`, `wp-content`, `wp-includes`,
  `wp-json`, `wp-login.php`, `wp-cron.php`, `xmlrpc.php`, `site`,
  `site-generations`} or with an existing subsite path (from `$wpdb->blogs`).
- Atomic flip: `symlink(site-generations/<genid>, site/current.tmp-<uniqid>)`
  then `rename()` over `site/current` — atomic on the same filesystem, so a
  visitor never sees a half-switched site.
- GC: keep the newest 3 generations, never deleting the one `current` points
  at.
- If the Wayback archiver mu-plugin is present, a commit re-archives home +
  feed through the archiver's own invalidate-then-kick mechanism (a commit
  replaces the whole site at once, so no per-post `save_post` hook ever fires).
- 200 → `{ "ok": true, "url": "http://<onion_address>/" }`.

## Localhost trust (all three endpoints, shared permission callback)

A positive allowlist — see `onionpress_static_is_local_request`:

- Deny if any `HTTP_X_FORWARDED_*` header is present (neither the onion
  serving path nor the host port map sets one; presence = spoof/misconfig).
- Allow only a loopback `REMOTE_ADDR` (`127.0.0.0/8`, `::1`) or this
  container's default gateway (from `/proc/net/route`) — the gateway is the
  NATed source of every connection that entered through Docker's host port
  publish. Anything else — including the tor and onionheaven containers'
  bridge addresses — is denied without being enumerated.
- Fails closed on a missing `REMOTE_ADDR` or an undeterminable gateway.

## End-to-end smoke test

`./test-receiver.sh` against a running stack: discovers the port, uploads a
fixture generation over multipart, commits it, and verifies the static file is
served at the site root ahead of WordPress.

## Known limitations

- One static site per network, served at the root. Per-subsite namespacing
  would change the `/generation` API and is deliberately out of scope.
- Port discovery is probe-based; a `status`-CLI JSON source would be cleaner
  but needs launcher support.

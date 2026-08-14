# dashboard

The command center: live PR queue, connected clients and their declared
capabilities, rounds in flight, policy levers.

`index.html` is the whole thing. The hub serves it at `GET /dashboard` so it
shares an origin with the API — opened from the filesystem, its polling would
be a cross-origin request to `:7575` and the browser would block it.

## What's on the page

- **Sign in** — GitHub's device flow, brokered by the hub (`POST
  /auth/device/start`, then `POST /auth/device/poll`). No password reaches this
  page and the hub never stores your GitHub token; the session bearer it mints
  lives in `localStorage`, not a cookie, because the hub authenticates by
  `Authorization` header and a credential the browser attaches automatically is
  one a cross-site request can spend.
- **The card** — work called from GitHub, polled from `GET /jobs`, scoped to
  repos your account can reach. Pick a cell to get the job id to hand to a
  client.
- **Policy floors** (RFC 0002 B2) — the minimum model tier each repo will
  accept, joined from `GET /auth/me` and `GET /policies` and saved through
  `PUT /policies/{owner}/{repo}`. Which rows are editable is derived from
  `/auth/me`'s per-repo permission (D-ME); the hub re-checks admin on every
  write, so the page deciding wrongly costs a 403, never a bad write.
- **Plugged in** (RFC 0002 B3) — the roster from `GET /clients`. Each row marks
  whether it is one of your own machines, states its attestation age and
  time-to-expiry, and flags the ones that are near expiry or already stale.
  Your own rows carry a `Revoke` button that confirms, then calls
  `DELETE /clients/{client_id}`. Revocation is self-service within an identity
  (D-SELFREVOKE) — other people's machines are GitHub's to cut off, not the
  hub's.

Ownership and staleness are read off the roster payload rather than computed
here: they are the same answers the revoke endpoint and the lease gate decide,
and a second local copy would be free to disagree with the one that matters.

## Why one file, and what that costs

`dashboard/index.html` is a single file by deliberate constraint (RFC 0002
D-ONEFILE), which is why dashboard tickets serialize instead of running in
parallel — two workers editing it guarantees a conflict.

Its only test surface is the served-HTML assertions in
`hub/review_bingo_hub/tests/integration/test_dashboard.py`: there is no JS
harness, by resolved scope decision. Those tests pin the markup and endpoints
the page cannot work without — the containers rows render into, the fetches
each panel makes, the revoke confirmation. Runtime behaviour (keyed row reuse,
dirty-edit preservation, the mid-revoke render skip) is not assertable from
there and is not claimed to be.

Rendering it from `templates/react-template` remains an option if the page
outgrows this; nothing here depends on it staying hand-written.

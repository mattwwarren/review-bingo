# RFC 0002 — Unattended operation and the management surface

**Date:** 2026-08-12
**Status:** approved design
**Milestone:** (filled at buildout)
**Issues:** (filled at buildout)
**Builds on:** [RFC 0001](2026-08-07-github-identity-grid-design.md) — the
invariant ("the hub is never a privilege escalation over GitHub") and every
decision there stand unmodified.

## Why

RFC 0001 made the grid safe to expose to more than one person. This RFC makes
it survivable at a workplace, where the operator is not watching:

1. **A peer left alone dies within a workday.** `loop` never re-attests;
   the access snapshot freezes at enrolment and hits the
   `IDENTITY_ACCESS_TTL_SECONDS` staleness wall (8h default), after which the
   hub correctly refuses to lease and the peer sits dead until a human runs an
   interactive device flow (issue #35). "Plug in a Mac mini and leave it" — the
   pitch's core promise — does not survive one working day.
2. **Every management operation is curl.** Policy floors, the one hub-side
   lever, are set with a hand-built `PUT /policies/{owner}/{repo}`. The
   dashboard (`dashboard/index.html`) is a read-only monitor: jobs, roster,
   sign-in, copy-a-job-id. Nobody at a workplace will hand-manage this.
3. **No way to kick a machine.** Clients check out voluntarily; there is no
   revocation surface at all. A lost or compromised laptop holds a valid hub
   bearer token until someone edits the database.
4. **Review rounds only trigger on PR lifecycle events.** `REVIEWABLE_ACTIONS`
   (`hub/review_bingo_hub/api/webhooks.py:28`) covers opened / synchronize /
   reopened / ready_for_review. A human clicking **request review** — the
   universal "look at this again" gesture — does nothing.

## Resolved decisions

- **D-REFRESH — The GitHub credential stays client-side; the client now keeps it.**
  The device flow already returns a refresh token when the App has expiring
  user tokens enabled. The client stores whatever the flow returned
  (`access_token`, `refresh_token`, expiry) in its state file
  (`~/.config/review-bingo/client.json`, already mode 0600) alongside the hub
  token. The hub's posture is unchanged: it reads a presented token at
  enrolment/check-in, then discards it (RFC 0001 D-DEVICE). Nothing new
  reaches the hub; the *client* stops throwing away the credential it needs
  to stay attested.
- **D-HALFTTL — Re-attestation is proactive at half the TTL, reactive on 409.**
  `loop` re-attests when the last successful attestation is older than half
  `IDENTITY_ACCESS_TTL_SECONDS` (client learns the TTL from the hub at
  check-in; additive response field), and also on any staleness 409 from
  lease. Renewal path: stored access token if unexpired → refresh-token
  exchange → fail. Failure is a clear exit naming the fix
  (`bingo_client.py login`), never a bare 409 loop — and if the App has
  expiring user tokens disabled (no refresh token exists), `login` says so
  once at enrolment so the operator knows that box needs a re-login cadence.
- **D-SELFREVOKE — Revocation is self-service within an identity; kicking
  strangers stays GitHub's job.** `DELETE /clients/{client_id}` succeeds when
  the caller's identity (grid-client token or dashboard session) matches the
  target client's `identity_id`. That covers the real case: my machine is
  lost/compromised/retired, revoke it now. Removing *someone else's* machine
  is deliberately not a hub feature — remove their repo access in GitHub and
  the invariant does the rest (their next attestation or TTL expiry ends
  their leasing). A hub-side cross-user kick would be a second, staler
  authority — exactly what RFC 0001 refused to build. Hard delete plus an
  activity-log entry; any active lease is released for requeue. Dev mode:
  the enrolment secret may revoke, behind the same named mode and startup
  warning (RFC 0001 D-DEVMODE).
- **D-REQUESTED — `review_requested` enqueues a round.** The webhook already
  receives `pull_request` events; the handler gains one action. Existing
  active-job dedup on `(repo, pr_number, head_sha)` still applies — a
  re-request while a round is queued or leased is a no-op; a re-request after
  a reported round enqueues a fresh job, which is precisely the "look again"
  semantics. No GitHub App configuration change needed.
- **D-ME — One identity endpoint feeds the dashboard.** `GET /auth/me` returns
  the caller's `github_login` and their repo access set with permission
  levels, for any authenticated caller (client token or dashboard session).
  The dashboard derives "which repos can I set policy on" from it. The
  alternative — `can_edit` flags sprinkled per response row — scatters the
  same fact across endpoints and drifts. `/auth/me` reveals only what the
  caller's own GitHub account already knows about itself.
- **D-SERIAL-DASH — Dashboard tickets serialize.** `dashboard/index.html` is a
  single file; two parallel workers editing it guarantees a conflict. B2 and
  B3 declare an explicit dependency chain instead.

## Design

### Epic I — Unattended grid

The grid survives a workday with nobody home. A1 keeps peers attested without
a human (closes #35 — its open questions are answered by D-REFRESH/D-HALFTTL).
A2 gives the operator a kill switch for their own machines. A3 lets reviewers
summon a round on demand.

### Epic II — Management surface

The dashboard grows hands. B1 is the identity endpoint both panels need; B2
turns policy floors into a form; B3 turns the roster into a management view
with revoke and attestation-freshness.

## Tickets

### A1 — Unattended re-attestation in the client loop

- **Epic:** I
- **Wave:** 1
- **Sprint:** 1
- **Depends on:** none
- **Context:** `loop` (`client/bingo_client.py:337`) never re-attests; the
  access snapshot freezes at enrolment and the peer dies at the TTL wall
  (issue #35). The device flow already hands back a refresh token when the
  App enables expiring user tokens; the client currently discards everything
  GitHub returns. Hub-side nothing changes — the hub continues to read and
  discard presented tokens (RFC 0001 D-DEVICE).
- **Scope:** D-REFRESH, D-HALFTTL
- **Acceptance:**
  - `login` persists `access_token`, `refresh_token`, and expiry (when
    present) in the state file, which remains mode 0600; the hub still never
    stores a GitHub credential.
  - `loop` re-attests via check-in when the last attestation is older than
    half the TTL, and reactively on a staleness 409 from lease; renewal
    prefers a still-valid stored access token, then the refresh-token
    exchange.
  - A client started with `loop` and left alone leases across at least one
    TTL boundary in a test using a faked GitHub seam.
  - When renewal is impossible (no refresh token, refresh rejected), the
    client exits non-zero with a message naming `bingo_client.py login`; when
    the App has expiring tokens disabled, `login` warns that unattended
    renewal is unavailable.
  - Check-in response carries the hub's `IDENTITY_ACCESS_TTL_SECONDS`
    (additive field) so the client never hardcodes the cadence.
  - `client/README.md` documents the unattended story and the App's
    "expiring user tokens" prerequisite; `scripts/github-app-setup.sh`'s
    manual-steps note mentions it alongside device flow.
  - Issue #35 is closed by this ticket with a pointer to D-REFRESH/D-HALFTTL.

### A2 — Client revocation, self-service within an identity

- **Epic:** I
- **Wave:** 1
- **Sprint:** 1
- **Depends on:** none
- **Context:** No revocation surface exists: clients check out voluntarily
  (`hub/review_bingo_hub/api/clients.py:186`) and a lost machine holds a
  valid bearer token until someone edits the database. The caller's identity
  is already resolvable from either credential kind
  (`services/identity_service.py`), so authorization is one comparison
  against the target's `identity_id`.
- **Scope:** D-SELFREVOKE
- **Acceptance:**
  - `DELETE /clients/{client_id}` removes the client when the caller's
    identity matches the target's `identity_id`; the response distinguishes
    nothing for out-of-identity targets (404, matching RFC 0001 D-404's
    disclosure rule).
  - A revoked client's bearer token stops working immediately; any active
    lease it held is released for requeue.
  - The action lands in the activity log with caller identity and target
    client name.
  - Under `dev` enrolment mode the enrolment secret may revoke, behind the
    existing named mode and startup warning.
  - `README.md` documents that removing another person's machine is done by
    removing their GitHub repo access, and why (the invariant).

### A3 — review_requested triggers a round

- **Epic:** I
- **Wave:** 1
- **Sprint:** 1
- **Depends on:** none
- **Context:** `REVIEWABLE_ACTIONS` (`hub/review_bingo_hub/api/webhooks.py:28`)
  covers PR lifecycle only; a reviewer clicking **request review** does
  nothing. The webhook already receives `pull_request` events, so this is a
  handler change, not an App configuration change.
- **Scope:** D-REQUESTED
- **Acceptance:**
  - A `pull_request` event with action `review_requested` enqueues a job for
    the PR head under the same policy-floor snapshotting as other actions.
  - Active-job dedup still applies: re-request during a queued/leased round
    is a no-op; re-request after a reported round enqueues a fresh job. A
    test covers both.
  - Disabled repos (`repo_policy.enabled = false`) enqueue nothing, same as
    every other action.

### B1 — GET /auth/me: caller identity and repo permissions

- **Epic:** II
- **Wave:** 1
- **Sprint:** 1
- **Depends on:** none
- **Context:** The dashboard has a session but no way to ask "who am I and
  where am I admin", so it cannot decide where to offer policy editing.
  Identity and the permission-bearing access set already exist
  (`models/github_identity.py`); this exposes the caller's own slice of it.
- **Scope:** D-ME
- **Acceptance:**
  - `GET /auth/me` returns `github_login`, `access_refreshed_at`, and the
    caller's repos each with its permission level, for both credential kinds
    (grid-client token and dashboard session).
  - It never returns another identity's data, and unauthenticated callers
    get 401 via the existing middleware.
  - An expired or stale identity is reported as such (additive field), not
    hidden — the dashboard uses it to prompt re-login.

### B2 — Dashboard policy editor

- **Epic:** II
- **Wave:** 2
- **Sprint:** 2
- **Depends on:** B1
- **Context:** Policy floors are curl-only. The API is complete
  (`GET /policies`, `PUT /policies/{owner}/{repo}` behind repo-admin,
  `api/policies.py:87`); what is missing is a surface. The dashboard already
  polls and renders authenticated data, so this is a panel, not an app.
- **Scope:** D-ME, D-SERIAL-DASH
- **Acceptance:**
  - A signed-in user sees policies for repos they can see, and an editor
    (min-tier select, enabled toggle) exactly on repos where `/auth/me`
    reports `admin` — including repos with no policy row yet.
  - Saving calls the existing `PUT`; a 403 (stale permission) surfaces the
    hub's message rather than a silent failure.
  - Non-admin repos render read-only with no implication of editability.
  - The existing poll loop's focus/selection preservation still holds.

### B3 — Dashboard client management

- **Epic:** II
- **Wave:** 2
- **Sprint:** 2
- **Depends on:** A2, B2
- **Context:** The roster view lists clients but manages nothing. With A2's
  endpoint and B1's identity, the dashboard can mark the caller's own
  machines, show attestation freshness (the thing that silently kills peers
  today), and offer revoke. Serialized after B2 because both edit
  `dashboard/index.html` (D-SERIAL-DASH).
- **Scope:** D-SELFREVOKE, D-SERIAL-DASH
- **Acceptance:**
  - The roster marks clients belonging to the signed-in identity and shows
    each one's attestation age and time-to-expiry (roster payload gains the
    additive fields it needs).
  - A revoke control appears only on the caller's own clients, calls
    `DELETE /clients/{id}`, confirms before firing, and the roster reflects
    the removal on the next poll.
  - A stale or near-expiry client is visually flagged.
  - `dashboard/README.md` stops claiming the dashboard is not started and
    describes what exists.

## Out of scope

- Cross-user revocation or any hub-side role concept — D-SELFREVOKE names
  GitHub as the authority, deliberately.
- Comment-command triggers (`/review` in a PR comment). `review_requested`
  covers the native gesture; a comment grammar is a product decision for
  later.
- Provider/data-egress allowlists, capability attestation, retention
  policies, deploy packaging — the compliance layer. Real, tracked, not this
  sprint.
- Any change to prompting, review depth, or aggregation (RFC 0001 D-ONEJOB
  stands).

## Open-source considerations

Same posture as RFC 0001: no secret or hostname lands in tracked files;
`client/README.md` carries the unattended-operation story (the first thing a
workplace adopter needs); the App's "expiring user tokens" toggle joins
"Enable Device Flow" in the documented manual setup steps.

## References

- `client/bingo_client.py:143` — device flow; where the refresh token is
  currently discarded
- `client/bingo_client.py:292` — check-in with optional `--reattest`
- `client/bingo_client.py:337` — `cmd_loop`, the unattended path
- `hub/review_bingo_hub/api/webhooks.py:28` — `REVIEWABLE_ACTIONS`
- `hub/review_bingo_hub/api/clients.py:124` — enrolment; `:147` check-in;
  `:186` check-out; `:194` roster
- `hub/review_bingo_hub/api/policies.py:87` — admin-gated policy upsert
- `hub/review_bingo_hub/api/auth.py:115` — device start/poll, session mint
- `hub/review_bingo_hub/services/identity_service.py:164` —
  `accessible_repo_names`; `:195` staleness; `:337` policy-write authz
- `hub/review_bingo_hub/models/github_identity.py` — identity + repo access
- `hub/review_bingo_hub/models/dashboard_session.py` — session rows
- `dashboard/index.html` — the single-file dashboard both B tickets edit
- `scripts/github-app-setup.sh:22` — the documented manual App steps

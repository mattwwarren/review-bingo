# RFC 0001 — GitHub-derived identity and repo-scoped authorization

**Date:** 2026-08-07
**Status:** approved design
**Milestone:** [v0.1.0](https://github.com/mattwwarren/review-bingo/milestone/2)
**Issues:** epics #15, #16 · tickets S1 #17, S2 #18, S3 #19, A1 #20, A2 #21, A3 #22, A4 #23, B1 #24, B2 #25 · pulled in #11
**Supersedes:** the "hub has no auth" framing in `.handoffs/handoff-2026-08-06-0221.md`

## Why

The work-team demo answered "does the grid work". It did not answer "can anyone
else plug into it". Today the hub is an open read surface: `GET /jobs`,
`/jobs/{id}/comment`, `/clients`, and `/policies` require no credential at all,
so anyone who can reach the hub can read every PR title, review summary, and
finding across every repo the GitHub App watches. `PUT /policies/{owner}/{repo}`
is world-writable, which means the one policy lever the pitch reserves for the
hub — the per-repo model floor — can be lowered by anyone.

Three modules inherited from `fastapi-template` and wired into `api/routes.py`
carry no authentication dependency of any kind: `organizations.py`,
`memberships.py`, and `documents.py`. The last of those is a file-upload and
storage surface. review-bingo uses none of them. Separately,
`/_admin/internal/*` and `/_admin/webhooks/kratos/*` are unauthenticated by
design — `api/admin.py:39` says they "MUST be blocked from external access via
Traefik", and this deployment has no Traefik.

The client-facing spine is in better shape than the rest: registration mints an
opaque bearer token and stores only its SHA-256 digest, and `lease`, `report`,
and `check-in` all sit behind it (`api/clients.py:36`). Webhooks are HMAC-verified
whenever `GITHUB_WEBHOOK_SECRET` is set (`api/webhooks.py:53-55`). Those two
surfaces need no rework.

## The invariant

> **The hub is never a privilege escalation over GitHub.** A client sees a job
> only if GitHub already says that person can read that repo.

Everything below follows from that sentence. The hub does not need its own
identity provider, its own tenancy model, or its own notion of roles, because
the thing it relays — PR activity — already has all three, maintained by GitHub
and kept current by the people who administer the repos. Any authorization
model we invent is a second, staler copy of that.

An enrolment secret, by contrast, only decides who gets to hold the firehose. It
leaves the invariant violated.

## Resolved decisions

- **D-ONEJOB — One job per PR head stands.** Multiple clients mean throughput across
different PRs, not depth on one. This closes the PITCH open question about
aggregation and dedup by deciding it away rather than building merge machinery:
no quorum, no finding-level dedup, no multi-round comment. `enqueue_job`'s
existing dedup on `(repo_full_name, pr_number, head_sha)`
(`services/job_service.py:38-47`) is the intended behavior, not a limitation to
lift. PITCH.md's open-questions list and next-step 6 should be updated to record
this.

- **D-GHAUTH — Authorization is derived from GitHub, not issued by the hub.** Enrolment
requires a GitHub user access token obtained through the App's device flow.
Dispatch, job reads, the client roster, and policy writes are all scoped by what
GitHub reports that user can access.

- **D-NET — The network boundary stays, demoted to defence-in-depth.** Public ingress
terminates exactly one path — `/webhooks/github` — because GitHub must reach it
and HMAC already authenticates it. Everything else binds to a private interface.
This is no longer what makes the design correct; it is what contains the blast
radius when something else is wrong. It also ends the ngrok dependency.

- **D-DENY — Deny by default at the application layer, too.** A `RequireTokenMiddleware`
rejects anything not on an explicit public allowlist: `/health`, `/ping`,
`/webhooks/github`, and the `/dashboard` static shell (which carries no data;
prompting inside a loaded page beats a blank 401). `/docs` and `/openapi.json`
are deliberately *not* public — they describe the private surface. The reason
for both layers is that the network layer fails silently and in the direction of
exposure: a `HUB_HOST=0.0.0.0` left in a shell, a VPN route that does not
survive a reboot, a firewall rule edited at 1am. The middleware turns that class
of mistake into a 401 instead of a data leak.

- **D-SUB — Subtract before adding.** Remove `organizations`, `memberships`, `documents`,
and both `/_admin` routers from `api/routes.py` and `main.py`. review-bingo has
no user accounts, no document storage, and no Kratos. Deleting them removes more
attack surface than any middleware adds, and it keeps the public allowlist small
enough to review at a glance. The modules stay on disk untouched, so a future
multi-tenant pivot re-includes a router rather than rebuilding one.

- **D-DEVICE — Enrolment uses the App's device flow, and the hub stores no GitHub credential.** The CLI holds its own user token client-side and presents it at enrolment and check-in; the hub reads identity and access from it, then discards it. Only `client_id` is needed, which is not a secret. The dashboard cannot poll GitHub's token endpoint from the browser (no CORS headers), so the hub brokers that flow and mints its own short-lived session token.
- **D-IDENT — Identity is its own row, not columns on `review_client`.** A CLI client and a dashboard session need the same "who is this and what can they read", so `github_identity` plus `identity_repo_access` is shared by both. Many clients may point at one identity; refreshing it updates all of them at once.
- **D-TTL — Cached access expires on a TTL, defaulting to 8 hours.** `github_identity.access_refreshed_at` past `IDENTITY_ACCESS_TTL_SECONDS` means the hub refuses to lease and asks for a fresh check-in. Matching the GitHub user token's own default lifetime keeps the two from drifting apart.
- **D-404 — An out-of-access job is 404, never 403.** A 403 confirms the job exists; job ids plus a 403/404 split turn the endpoint into an oracle for "does this private repo have an open PR right now". Below-tier inside the access set stays 403, because there the explanation is the caller's to have.
- **D-ROSTER — The client roster is scoped to overlap.** `GET /clients` returns clients sharing at least one accessible repo with the caller, plus the caller's own — not a directory of who is on the grid and what hardware they run.
- **D-POLICY — Policy writes require repo admin, as reported by GitHub.** Whoever GitHub says administers a repo may set that repo's model floor. No admin token, no new role concept, no second source of truth.
- **D-DEVMODE — Offline paths live behind one named mode, not scattered branches.** `CLIENT_ENROLMENT_MODE` defaults to `github`; the secret-based enrolment path and the dev policy-write path exist only under `dev`, and the hub logs loudly at startup when the mode is not `github`. A bypass that can be switched on silently is the entire failure mode of this feature.
- **D-MIGRATE — Existing clients re-enrol; nothing is backfilled.** `review_client.identity_id` is nullable and a client without an identity never leases, which is the correct answer rather than a special case. There is no honest way to invent a GitHub identity for the existing `demo-workstation` row.

## Rejected alternatives

- **Shared enrolment secret as the primary control.** Simple, but it authorizes
  admission only — every enrolled client still reads every job in the grid. It
  does not restore the invariant.
- **Full multi-tenant via the template's stack.** `AUTH_PROVIDER_TYPE` defaults
  to `"none"` (`core/config.py:93`) and the valid providers are
  `ory / auth0 / keycloak / cognito`, so "just turn on the template's tenancy"
  means running an identity provider, plus threading `org_id` through
  `review_job`, `review_client`, and `repo_policy` with migrations and dispatch
  filters. GitHub already supplies identity and tenancy for free.
- **Single flat public grid.** One ingress, no VPN, but every client can read
  every job. Expensive to walk back once people rely on the open read surface.

## Design

### Enrolment — two flows

**MCP client (CLI).** Runs the GitHub App device flow directly:
`POST https://github.com/login/device/code`, then poll
`POST https://github.com/login/oauth/access_token`. Only `client_id` is
required — the device flow uses no client secret — so shipping it in client
config leaks nothing. Device flow must be enabled in the App's settings, and
`GITHUB_APP_CLIENT_ID` must be added to hub/client config; `core/config.py:202-209`
carries only `GITHUB_APP_ID` and the private key today.

The client keeps the resulting user token client-side and presents it to
`POST /clients` at enrolment and again at `POST /clients/check-in`. The hub calls
`GET /user` for identity and `GET /user/installations/{id}/repositories` for the
access set, records what it learned, and discards the token. **The hub never
persists a GitHub credential.**

`/user/installations/{id}/repositories` returns the intersection of the App's
installation and the user's own read/write/admin access, and per GitHub's
documentation "the access the user has to each repository is included in the
hash under the permissions key" — so permission level arrives with the listing,
with no extra call per repo.

**Dashboard (browser).** Browser JavaScript cannot poll
`login/oauth/access_token`; it sends no CORS headers. So the hub brokers the
flow: `POST /auth/device/start` returns the user code and verification URI, the
page polls `POST /auth/device/poll`, and on success the hub mints a short-lived
dashboard session token and discards the GitHub token. No callback URL and no
`client_secret` need to be registered anywhere.

### Schema

Identity is its own row, not a set of columns on `review_client`. Both a CLI
client and a dashboard session need the same thing — "who is this GitHub user
and what can they read" — and duplicating the access set per client would mean
refreshing it in two places and having them disagree.

- New table `github_identity(id, github_user_id UNIQUE, github_login,
  access_refreshed_at)`.
- New table `identity_repo_access(identity_id, repo_full_name, permission)`. A
  join table rather than a JSONB column, because dispatch is about to filter on
  it and this keeps that an indexed `WHERE repo_full_name IN (SELECT ...)`.
- `review_client` gains a nullable `identity_id` foreign key.
- New table for dashboard sessions (`token_hash`, `identity_id`, `expires_at`).
  A human is not a client; it does not belong in `review_client`.
- One person may register several machines: many `review_client` rows may point
  at one `github_identity`. Refreshing that identity — from any of their
  clients' check-ins, or from a dashboard login — updates all of them at once.

### Dispatch

`lease_next_job` and `lease_specific_job` already filter on the tier floor; both
gain `repo_full_name IN (client's access set)`. `FOR UPDATE SKIP LOCKED`, lazy
lease reclamation, and attempt counting are untouched. The change is a `WHERE`
clause in two functions.

**Error codes carry security weight here.** `POST /jobs/{id}/lease` currently
404s on a missing job and 403s with a message naming the required tier
(`api/jobs.py:76-80`). Once visibility is repo-derived:

- Job outside the caller's access set → **404**, indistinguishable from
  nonexistent. A 403 would confirm the job exists, and job ids plus a 403/404
  split turn the endpoint into an oracle for "does this private repo have an
  open PR right now".
- Job inside the access set but above the caller's tier → **403** with the
  existing explanatory message. There the information is the caller's to have,
  and the message is the point.

The same rule governs `GET /jobs/{id}`, `/jobs/{id}/comment`, and
`/jobs/{id}/relay-target`. `GET /jobs` filters the list to the caller's access
set.

### Roster

`GET /clients` currently returns every client with its model, provider, and
quant. It becomes scoped to overlap: clients sharing at least one accessible
repo with the caller, plus the caller's own. That keeps the dashboard useful
without publishing a directory of who is on the grid and what hardware they run
to anyone who enrols.

### Policy authorization

`PUT /policies/{owner}/{repo}` requires `permission == "admin"` on that repo in
the caller's cached access set. Whoever GitHub says administers a repo is
exactly who may set that repo's model floor — no admin token, no new role
concept, no second source of truth. `GET /policies` filters to repos the caller
can see.

### Unchanged

Relay authenticates as the App with an installation token
(`services/relay_service.py:69-86`) and posts to a repo the App is installed on.
No user identity is involved. Webhook handling, HMAC verification, job
enqueueing, tier-floor snapshotting, and the lease/report state machine are all
untouched.

### Consequence worth naming

The dashboard stops being a god view. It shows the jobs, clients, and policies
of whoever is logged into it. On a one-person grid that is invisible; the moment
a teammate opens it, it is the whole point.

### Failure modes

**GitHub unavailable at enrolment:** fail closed. No identity, no client.

**GitHub unavailable at check-in:** keep the existing access set until its TTL
expires, rather than locking out the grid over a transient 502. Past the TTL,
refuse. This grace window is a deliberate decision, not whatever the code
happens to do.

**Stale authorization** is the interesting failure. A cached access set goes
wrong the moment someone loses repo access, and nothing tells the hub. So
`github_identity.access_refreshed_at` is load-bearing: past its TTL the hub
refuses to lease and answers "check in again". The TTL is 8 hours, configurable
as `IDENTITY_ACCESS_TTL_SECONDS` — matching the user token's own default
lifetime so the two expire together instead of drifting apart.

**Device flow errors** — `authorization_pending`, `slow_down`, `expired_token`,
`access_denied` — are the client's to handle. `slow_down` means honouring the
returned interval, not retrying harder.

**Rate limits** are not a concern: two calls per check-in against a per-user
5000/hour budget.

### Dev mode

Tests, `scripts/demo.sh`, and the tier demo cannot reach GitHub.
`CLIENT_ENROLMENT_MODE` defaults to `github`; a secret-based path exists only
under `dev`. The hub logs loudly at startup when the mode is not `github`. A
bypass that can be switched on silently is the entire failure mode of this
feature — it must be a named configuration state, not an `if TESTING` branch.

Policy writes need the same treatment: in `dev` mode there is no real GitHub
permission to consult, so `PUT /policies/{owner}/{repo}` accepts the enrolment
secret in place of a repo-admin check. Same switch, same startup warning — one
named mode, not a second bypass with its own rules.

### Migration

`review_client.identity_id` is nullable, and a client with no identity never
leases — the dispatch filter has nothing to match against, which is the correct
answer rather than a special case. The existing `demo-workstation` row has no
GitHub identity and there is no honest way to invent one, so it re-enrols
through the new flow. Sixteen job rows on a laptop database do not justify a
data-migration story.

### Testing

GitHub calls go behind one seam — a `github_identity` service — so tests inject
a fake instead of scattering httpx mocks across the suite.

The matrix that matters:

- enrolment accepted, denied, and with an expired device code
- check-in refresh when a repo is **added** and when a repo is **removed**
- stale `access_refreshed_at` refuses to lease
- dispatch excludes jobs outside the access set
- targeted lease on an out-of-access job returns **404, not 403** — a security
  test, and it should read like one
- policy write allowed for `admin`, rejected for `write` and `read`
- roster scoped to overlap
- dev-mode enrolment path, and the startup warning when the mode is not `github`

The autouse `.env` fixture in `hub/review_bingo_hub/tests/conftest.py` stays and
grows: it must now also pin `CLIENT_ENROLMENT_MODE`. A dev `.env` bleeding into
the suite is exactly how webhook signature verification once switched itself on
mid-test-run and broke seven tests.

### Epic I — Identity and repo-scoped authorization

Everything that makes the invariant true: who a client is, what GitHub says they
can read, and every place the hub must consult that before handing something
over. Enrolment, re-attestation, dispatch filtering, and policy authorization
land here. The epic is complete when no endpoint serves data the caller's GitHub
access does not already entitle them to.

### Epic II — Surfaces and proof

The parts a human touches, and the evidence the policy works. The dashboard
stops being a god view and gets its own login; the tier floor — the pitch's one
hub-side policy lever, never yet demonstrated — gets a repeatable demo whose
assertions run in CI.

## Tickets

### S1 — Remove the unused open routers

- **Epic:** none
- **Wave:** 0
- **Sprint:** 0
- **Depends on:** none
- **Context:** `organizations.py`, `memberships.py`, and `documents.py` came from fastapi-template, are wired into `api/routes.py`, carry no authentication dependency of any kind, and are unused by review-bingo; `documents.py` is a file-upload surface. `/_admin/internal/*` and `/_admin/webhooks/kratos/*` are unauthenticated by design and rely on a Traefik that does not exist in this deployment. Deleting them removes more attack surface than any middleware adds and keeps the later public allowlist small enough to review at a glance.
- **Scope:** D-SUB
- **Acceptance:**
  - Those routers are no longer included in `api/routes.py` or `main.py`, and their paths return 404.
  - The module files remain on disk, unmodified, so a future multi-tenant pivot re-includes a router rather than rebuilding one.
  - The hub suite passes, with any tests covering the removed routes deleted rather than skipped.

### S2 — Deny-by-default request middleware

- **Epic:** none
- **Wave:** 0
- **Sprint:** 0
- **Depends on:** S1
- **Context:** The hub currently authenticates only the client spine and webhooks; everything else is open, and the network boundary that will front it fails silently and in the direction of exposure. A `RequireTokenMiddleware` that rejects anything not on an explicit public allowlist turns a VPN misconfiguration or a stray `HUB_HOST=0.0.0.0` into a 401 instead of a data leak.
- **Scope:** D-DENY, D-NET
- **Acceptance:**
  - An unauthenticated request to any path not on the allowlist returns 401.
  - The allowlist is exactly `/health`, `/ping`, `/webhooks/github`, and the `/dashboard` static shell; `/docs` and `/openapi.json` are not public.
  - A test asserts the allowlist's contents, so adding a route cannot silently widen the public surface.

### S3 — Record the aggregation decision in PITCH.md

- **Epic:** none
- **Wave:** 0
- **Sprint:** 0
- **Depends on:** none
- **Context:** PITCH.md still carries "merging/deduping when multiple clients review the same PR" as an open design question and lists aggregation as next-step 6, but the decision is made: one job per PR head stands, so multiple clients mean throughput across PRs rather than depth on one. Leaving the question open in the pitch invites the next contributor to design machinery this sprint deliberately declined to build.
- **Scope:** D-ONEJOB
- **Acceptance:**
  - PITCH.md's open-questions list records the resolution rather than posing the question.
  - Next-step 6 is marked resolved with a pointer to this RFC.
  - The existing dedup behavior in `services/job_service.py` is described as intended, not as a limitation.

### A1 — GitHub identity service and device-flow enrolment

- **Epic:** I
- **Wave:** 1
- **Sprint:** 1
- **Depends on:** S2
- **Context:** Enrolment is open today — anyone who can reach the hub self-registers and receives a bearer token. This ticket makes admission derive from GitHub: the client obtains a user access token through the App's device flow and presents it to `POST /clients`, and the hub reads identity and repo access from it and then discards it. This is the foundation every other Epic I ticket filters on, and it introduces the `github_identity` seam that keeps GitHub calls testable.
- **Scope:** D-GHAUTH, D-DEVICE, D-IDENT, D-DEVMODE, D-MIGRATE
- **Acceptance:**
  - `GITHUB_APP_CLIENT_ID` is configurable and device flow is enabled on the App; the client obtains a user token without any client secret.
  - `github_identity` and `identity_repo_access` exist with a migration, and `review_client.identity_id` is a nullable foreign key.
  - Enrolment populates identity and the repo access set including each repo's permission level, and the hub persists no GitHub token.
  - `CLIENT_ENROLMENT_MODE` defaults to `github`, the secret path exists only under `dev`, and the hub logs loudly at startup when the mode is not `github`.
  - GitHub calls sit behind one injectable seam so tests use a fake rather than scattered httpx mocks.

### A2 — Check-in re-attestation and access staleness

- **Epic:** I
- **Wave:** 2
- **Sprint:** 1
- **Depends on:** A1
- **Context:** A cached access set goes wrong the moment someone loses repo access, and nothing tells the hub. Check-in is already the grid's availability signal, which makes it the natural refresh point, and the GitHub user token's 8-hour default lifetime is roughly the same cadence. Without a TTL the hub would serve authorization decisions from data of unbounded age.
- **Scope:** D-TTL
- **Acceptance:**
  - Check-in accepts a fresh GitHub token and refreshes the identity's access set and `access_refreshed_at`.
  - A repo added to a user's access appears after the next check-in; a repo removed disappears.
  - Leasing is refused once `access_refreshed_at` is older than `IDENTITY_ACCESS_TTL_SECONDS`, with a response that says to check in again.
  - A GitHub outage during check-in keeps the existing access set until the TTL expires rather than locking the grid out immediately.

### A3 — Repo-scoped dispatch, job reads, and roster

- **Epic:** I
- **Wave:** 2
- **Sprint:** 1
- **Depends on:** A1
- **Context:** This is where the invariant becomes true for the data plane. Dispatch and every job read gain the access-set filter, and the targeted-lease path's error codes stop leaking existence: a 403 on an out-of-access job would confirm that job exists, turning the endpoint into an oracle for whether a private repo has an open PR. The roster stops publishing a directory of everyone on the grid and their hardware.
- **Scope:** D-GHAUTH, D-404, D-ROSTER
- **Acceptance:**
  - `lease_next_job` and `lease_specific_job` filter to the caller's access set in addition to the existing tier floor, leaving lease reclamation and attempt counting untouched.
  - `GET /jobs` returns only jobs in the caller's access set; `GET /jobs/{id}`, `/comment`, and `/relay-target` return 404 for anything outside it.
  - A targeted lease on an out-of-access job returns 404, and a security test asserts it is indistinguishable from a nonexistent job.
  - A targeted lease on an in-access job above the caller's tier still returns 403 with its explanatory message.
  - `GET /clients` returns only clients sharing at least one accessible repo with the caller, plus the caller's own.

### A4 — Policy writes require repo admin

- **Epic:** I
- **Wave:** 2
- **Sprint:** 1
- **Depends on:** A1
- **Context:** `PUT /policies/{owner}/{repo}` is world-writable, so the per-repo model floor — the one review-config knob the pitch reserves for the hub — can be lowered by anyone who can reach it. GitHub already knows who administers a repo, and that permission level arrives with the repo listing at enrolment, so the check needs no extra API call and no hub-side role concept.
- **Scope:** D-POLICY, D-DEVMODE
- **Acceptance:**
  - `PUT /policies/{owner}/{repo}` succeeds only when the caller's cached access for that repo is `admin`.
  - A caller with `write` or `read` on the repo is rejected, as is a caller with no access.
  - `GET /policies` returns only policies for repos the caller can see.
  - Under `dev` enrolment mode the write accepts the enrolment secret in place of the admin check, behind the same named mode and startup warning.

### B1 — Dashboard login and scoped views

- **Epic:** II
- **Wave:** 2
- **Sprint:** 2
- **Depends on:** A1, A3
- **Context:** Once reads require a token the dashboard needs an identity of its own, and browser JavaScript cannot poll GitHub's token endpoint because it sends no CORS headers. The hub therefore brokers the device flow and mints its own short-lived session token. The visible consequence is intended: the dashboard stops being a god view and shows only what the logged-in user can see.
- **Scope:** D-DEVICE, D-IDENT
- **Acceptance:**
  - `POST /auth/device/start` returns a user code and verification URI; `POST /auth/device/poll` completes the flow server-side.
  - On success the hub mints a short-lived dashboard session bound to a `github_identity` and discards the GitHub token.
  - The dashboard prompts for login once, persists the session token in `localStorage`, and recovers cleanly when it expires.
  - Job, roster, and policy views show only what the logged-in identity may see, and the existing poll loop still preserves focus and selection.

### B2 — Tier-floor demo with CI assertions

- **Epic:** II
- **Wave:** 3
- **Sprint:** 2
- **Depends on:** A3
- **Context:** Tier floors are implemented and have never been demonstrated with more than one client, which makes the pitch's only hub-side policy lever the least-evidenced part of the system. A script alone rots because nobody runs it, so the same assertions belong in the integration suite where CI executes them on every change.
- **Scope:** D-DEVMODE
- **Acceptance:**
  - `scripts/demo-tiers.sh` registers a frontier and an experimental client in dev enrolment mode and sets a repo floor above experimental.
  - The experimental client receives nothing from `/jobs/lease` and a 403 from the targeted lease, while the frontier client leases the same job successfully.
  - Equivalent assertions live in `hub/review_bingo_hub/tests/integration/` and run in CI.
  - The script runs offline with no GitHub credentials.

## Out of scope

- Aggregation, dedup, or quorum across clients — decided against above.
- Org or tenant columns, and any identity provider.
- Trust or capability weighting of self-declared model tiers. Clients still
  self-declare; nothing verifies the declaration. Unchanged by this work.
- Relay-after-merge behavior. A round that reports after its PR merged still
  posts a comment.
- Standing up the stable public ingress that replaces ngrok. The design needs
  *a* public hostname for webhooks; choosing and deploying it is separate
  infrastructure work and deserves its own ticket.
- Filing client-side CI as a new ticket. This work puts real logic in `client/`
  — device flow, token handling, a login command — and root CI runs
  `working-directory: hub`, so none of it would run. That gap is already tracked
  by issue #11, which is pulled into this milestone rather than refiled.

## Open-source considerations

This repo is intended to be published. Two consequences for this work: no
secret, token, or hostname may land in tracked files — `CLIENT_ENROLMENT_MODE`
and `GITHUB_APP_CLIENT_ID` belong in documented configuration, not in defaults
that happen to work on one laptop — and the enrolment flow is the first thing a
contributor will touch, so the device-flow login needs contributor-facing
documentation in `client/README.md` rather than tribal knowledge.

## References

- `hub/review_bingo_hub/api/routes.py` — router composition; where the unused open routers are included
- `hub/review_bingo_hub/api/clients.py:36` — `get_current_client`, the existing bearer-token dependency
- `hub/review_bingo_hub/api/clients.py:52` — open registration endpoint that device-flow enrolment replaces
- `hub/review_bingo_hub/api/jobs.py:59` — targeted lease endpoint carrying the 403/404 disclosure rule
- `hub/review_bingo_hub/api/jobs.py:132` — unauthenticated job feed
- `hub/review_bingo_hub/api/policies.py:18` — world-writable policy upsert
- `hub/review_bingo_hub/api/webhooks.py:53` — HMAC verification, the one already-correct public surface
- `hub/review_bingo_hub/api/admin.py:39` — `/_admin` endpoints relying on a Traefik that does not exist here
- `hub/review_bingo_hub/services/job_service.py:38` — active-job dedup on repo/PR/head_sha
- `hub/review_bingo_hub/services/job_service.py:95` — `lease_next_job`, gains the access-set filter
- `hub/review_bingo_hub/services/job_service.py:120` — `lease_specific_job`, gains the same filter
- `hub/review_bingo_hub/services/relay_service.py:69` — installation-token relay, unchanged by this work
- `hub/review_bingo_hub/models/review_client.py:76` — `ReviewClient`, gains `identity_id`
- `hub/review_bingo_hub/core/config.py:93` — `AUTH_PROVIDER_TYPE`, the template's inert IdP stack
- `hub/review_bingo_hub/core/config.py:202` — GitHub App settings, gains `GITHUB_APP_CLIENT_ID`
- `hub/review_bingo_hub/main.py:203` — the disabled template AuthMiddleware this design does not revive
- `hub/review_bingo_hub/tests/conftest.py` — autouse `.env` fixture that must also pin the enrolment mode
- `dashboard/index.html` — single-file dashboard gaining a login prompt
- `client/bingo_mcp.py` — MCP client gaining the device-flow login command

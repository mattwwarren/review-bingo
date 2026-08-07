# GitHub-derived identity and repo-scoped authorization

**Date:** 2026-08-07
**Status:** approved design, not yet ticketed
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

## Decisions

**One job per PR head stands.** Multiple clients mean throughput across
different PRs, not depth on one. This closes the PITCH open question about
aggregation and dedup by deciding it away rather than building merge machinery:
no quorum, no finding-level dedup, no multi-round comment. `enqueue_job`'s
existing dedup on `(repo_full_name, pr_number, head_sha)`
(`services/job_service.py:38-47`) is the intended behavior, not a limitation to
lift. PITCH.md's open-questions list and next-step 6 should be updated to record
this.

**Authorization is derived from GitHub, not issued by the hub.** Enrolment
requires a GitHub user access token obtained through the App's device flow.
Dispatch, job reads, the client roster, and policy writes are all scoped by what
GitHub reports that user can access.

**The network boundary stays, demoted to defence-in-depth.** Public ingress
terminates exactly one path — `/webhooks/github` — because GitHub must reach it
and HMAC already authenticates it. Everything else binds to a private interface.
This is no longer what makes the design correct; it is what contains the blast
radius when something else is wrong. It also ends the ngrok dependency.

**Deny by default at the application layer, too.** A `RequireTokenMiddleware`
rejects anything not on an explicit public allowlist: `/health`, `/ping`,
`/webhooks/github`, and the `/dashboard` static shell (which carries no data;
prompting inside a loaded page beats a blank 401). `/docs` and `/openapi.json`
are deliberately *not* public — they describe the private surface. The reason
for both layers is that the network layer fails silently and in the direction of
exposure: a `HUB_HOST=0.0.0.0` left in a shell, a VPN route that does not
survive a reboot, a firewall rule edited at 1am. The middleware turns that class
of mistake into a 401 instead of a data leak.

**Subtract before adding.** Remove `organizations`, `memberships`, `documents`,
and both `/_admin` routers from `api/routes.py` and `main.py`. review-bingo has
no user accounts, no document storage, and no Kratos. Deleting them removes more
attack surface than any middleware adds, and it keeps the public allowlist small
enough to review at a glance. The modules stay on disk untouched, so a future
multi-tenant pivot re-includes a router rather than rebuilding one.

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

## Architecture

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

## Failure modes

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

## Dev mode

Tests, `scripts/demo.sh`, and the tier demo cannot reach GitHub.
`CLIENT_ENROLMENT_MODE` defaults to `github`; a secret-based path exists only
under `dev`. The hub logs loudly at startup when the mode is not `github`. A
bypass that can be switched on silently is the entire failure mode of this
feature — it must be a named configuration state, not an `if TESTING` branch.

Policy writes need the same treatment: in `dev` mode there is no real GitHub
permission to consult, so `PUT /policies/{owner}/{repo}` accepts the enrolment
secret in place of a repo-admin check. Same switch, same startup warning — one
named mode, not a second bypass with its own rules.

## Migration

`review_client.identity_id` is nullable, and a client with no identity never
leases — the dispatch filter has nothing to match against, which is the correct
answer rather than a special case. The existing `demo-workstation` row has no
GitHub identity and there is no honest way to invent one, so it re-enrols
through the new flow. Sixteen job rows on a laptop database do not justify a
data-migration story.

## Testing

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

## Work items

Each is independently landable, in this order.

1. **Subtract unused routers.** Remove `organizations`, `memberships`,
   `documents`, and both `/_admin` routers from `api/routes.py` and `main.py`.
   Modules stay on disk. *Done when:* those paths 404 and the suite is green.
2. **Deny-by-default middleware.** `RequireTokenMiddleware` with the explicit
   public allowlist. *Done when:* an unauthenticated request to any non-allowlisted
   path gets 401, and a test asserts the allowlist contents so adding a route
   cannot silently widen it.
3. **GitHub identity service and enrolment.** `GITHUB_APP_CLIENT_ID` config, the
   `github_identity` seam, device-flow enrolment on `POST /clients`, schema
   changes and migration, `CLIENT_ENROLMENT_MODE`. *Done when:* a real client
   enrols via device flow and its access set is populated.
4. **Check-in re-attestation and staleness TTL.** *Done when:* removing a
   collaborator's repo access removes their ability to lease that repo's jobs
   after the next check-in, and a stale client is refused.
5. **Repo-scoped dispatch and job reads,** including the 404-not-403 rule.
   *Done when:* the security tests above pass.
6. **Policy authorization from repo admin permission.** *Done when:* a
   non-admin's `PUT /policies` is rejected and an admin's succeeds.
7. **Dashboard session via brokered device flow.** `POST /auth/device/start`,
   `POST /auth/device/poll`, session table, and the dashboard's login prompt
   with `localStorage` persistence. *Done when:* the dashboard shows only what
   the logged-in user can see.
8. **Tier-floor demo.** `scripts/demo-tiers.sh` registers a frontier and an
   experimental client in dev mode, sets a repo floor, and shows the
   experimental client getting nothing from `/jobs/lease` and a 403 from the
   targeted lease while the frontier client takes the job. The same assertions
   land in `tests/integration/` so CI runs them — a demo script nobody executes
   rots, and this is the only evidence the pitch's one policy lever works.
9. **Client CI (existing issue #11).** This sprint puts real logic in `client/`
   — device flow, token handling, the login command — and root CI runs
   `working-directory: hub`, so none of it would run. Lint, type-check, and test
   coverage for `client/` folds into this sprint rather than staying a loose end.

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

## Open-source considerations

This repo is intended to be published. Two consequences for this work: no
secret, token, or hostname may land in tracked files — `CLIENT_ENROLMENT_MODE`
and `GITHUB_APP_CLIENT_ID` belong in documented configuration, not in defaults
that happen to work on one laptop — and the enrolment flow is the first thing a
contributor will touch, so the device-flow login needs contributor-facing
documentation in `client/README.md` rather than tribal knowledge.

# RFC 0003 — PR handoff: outbound events, review strategies, and the bidirectional grid

**Date:** 2026-08-14
**Status:** approved design
**Milestone:** [v0.3.0](https://github.com/mattwwarren/review-bingo/milestone/4)
**Issues:** epic #41 (continues) · tickets A1 #59, A2 #60, A3 #61, A4 #65 · discussion #62, #63, #64
**Builds on:** [RFC 0001](2026-08-07-github-identity-grid-design.md) and
[RFC 0002](2026-08-12-work-grid-operations.md) — the invariant ("the hub is
never a privilege escalation over GitHub") and every decision there stand
unmodified.

## Why

RFC 0002 made the grid survivable unattended. This RFC makes the *handoff*
real: the moment a review round completes and something — a person, but more
interestingly an agent — picks the result up and acts on it.

1. **The signal chain ends in a side effect.** `report_job_endpoint`
   (`hub/review_bingo_hub/api/jobs.py`) commits the report, calls
   `relay_result`, and sets `job.state = RELAYED` — nothing is emitted. The
   PR comment (App mode) or a log line (log mode) is the only externally
   observable artifact. A consumer that wants to act on a completed round
   must poll `GET /jobs` and diff, tracking "last seen" client-side. There
   is no "review done" signal to subscribe to.
2. **Job intent is inexpressible.** The job carries
   `{repo_full_name, pr_number, head_sha, event_action, pr_title}` and the
   client declares `{model_name, provider, quant, tier}`. "What kind of review" lives
   entirely in the client's `REVIEW_CMD`. There is no way to ask for
   "review this PR with a security lens" — the agentic-native request.
3. **The contributor side has no on-ramp.** The grid is bidirectional by
   design — `bingo_client.py loop` with a real `REVIEW_CMD` is the
   contributor side — but no reference `REVIEW_CMD` exists. The docs say
   "point it at `claude -p`, an ollama wrapper, a two-model loop" and hand
   the contributor nothing to start from.
4. **Requestor-side trust stops at the tier floor.** The floor answers "how
   good must the model be", not "which models do we accept". A workplace
   wants to know it is GLM, Codex, or Claude backing its reviews; an OSS
   repo may happily take everything — it's free labor. Neither can express
   that today.

## Resolved decisions

- **D-EVENT — SSE is the outbound surface, filtered per-event.**
  `GET /events` streams job state transitions to authenticated subscribers
  via `ScopedCallerDep`. An SSE stream is a long-lived read surface, so
  RFC 0001 D-404 applies **per emission, not at connection time**: access
  sets change (check-ins refresh them, TTLs expire), and a stream opened
  with one access set that later narrows must stop pushing for
  newly-inaccessible repos — re-evaluate the access set on each emission,
  and close the stream when the identity's TTL lapses to force a
  reconnect. Event payload is
  `{job_id, repo_full_name, pr_number, head_sha, verdict, summary}`;
  findings stay
  off the wire (variable shape, can be large) — the event is the poke,
  `GET /jobs/{id}` is the fetch. `job.relayed` is the only event type this
  round; see Out of scope for the rest.

- **D-STRAT — strategies are a known registry; the hub validates names,
  never meanings.** `ReviewJob` gains optional
  `requested_strategies: list[str]` (empty = match-any, backward
  compatible); `ReviewClient` gains `offered_strategies: list[str]` at
  check-in. The vocabulary is a small named registry (`security`,
  `shallow`, `full-loop`, `fix-and-reverify`) with `custom:*` as the
  escape hatch — free-form on both sides would reproduce the "what does
  this client emit" ambiguity `verdict` already has. What a strategy
  *means* stays client-side, per the pitch's deliberate non-goal.
  Lease semantics are **any-match**: a job requesting multiple strategies
  is one job (D-ONEJOB — never two jobs per head), and a client leases it
  by offering at least one requested strategy. The verdict stays
  unconstrained — strategy is a dispatch filter, not a verdict dimension.
  First population source is the repo-policy default (`RepoPolicy` gains
  `default_strategies`, snapshotted at `enqueue_job` like `min_tier` is
  today); the explicit request endpoint and PR-label mapping are
  follow-ups.

- **D-TRUST — trust is two-way, identity/allowlist-based, not
  reputation-based.** Reviewers identify themselves: check-in gains a
  runtime identity (Hermes, Claude Code, Codex, an ollama wrapper, …)
  alongside the existing `{model_name, provider, quant, tier}` declaration.
  Requestors constrain who may review: `RepoPolicy` gains
  `accepted_models` / `accepted_model_groups`, where **model groups** are
  platform-curated named bundles (e.g. `frontier`, `enterprise-approved`)
  — the enterprise-grade control lever. Empty allowlist = match-any, the
  OSS default. The trust boundary is registration plus declared identity:
  attestation (RFC 0001) verifies repo access, not model claims, so
  "capability honesty" (PITCH.md) remains open as a deferred verification
  question. Reputation scoring and sampled re-review are explicitly
  deferred, not rejected.

- **D-GATE — dispatch gates compose in a fixed order: tier floor → model
  allowlist → strategy match.** All three are additive `WHERE` clauses in
  `lease_next_job`. None touch D-ONEJOB; a job that no connected client
  can clear simply waits, exactly as an under-tier job does today.

- **D-REVIEWCMD — the contributor contract is stdin/stdout, and the
  reference lives in-repo.** A `REVIEW_CMD` takes job JSON on stdin
  (`repo_full_name, pr_number, head_sha`, plus `requested_strategies` once
  A2 lands) and emits `{verdict, summary, findings}` on stdout. The
  reference implementation ships in `client/examples/` — it splits into
  its own package only when external installs demand separate versioning,
  the same rationale as the repo-structure decision in PITCH.md. It ships
  with a dry-run mode (review only, no auto-fix) as the default posture; a
  subscribing bot graduates to fix-and-reverify only once its operator
  trusts it unsupervised. Cross-model loops (cheap model fixes, expensive
  model reviews) live **inside one `REVIEW_CMD`** — the hub sees one
  round, preserving D-ONEJOB; hub-level multi-round jobs are not built.

## Design

### Epic — Unattended grid (#41, continues)

The four workstreams close the handoff loop from both sides. Consumer side:
A1 makes a completed round observable, A2 makes the request expressive, A4
makes the reviewer set controllable. Contributor side: A3 gives an agent a
working on-ramp to be the thing the events are about. A subscribing agent
that leases with its own compute is the full bidirectional picture — one
`github_identity`, both roles, no schema change (RFC 0001 D-IDENT).

## Tickets

### A1 — SSE event stream for job lifecycle (#59)

- `GET /events` SSE endpoint, authenticated via `ScopedCallerDep`.
- `job.relayed` emitted from `report_job_endpoint` after the relay
  succeeds; payload per D-EVENT.
- Access-set filter applies per-event (D-404 compliant); stream closes on
  identity TTL lapse.
- Tests: subscriber receives events only for repos in their access set; a
  subscriber whose access set narrows mid-stream stops receiving events
  for lost repos.

### A2 — Review strategy contract (#60)

- `ReviewJob.requested_strategies` / `ReviewClient.offered_strategies`
  per D-STRAT; registry-validated at check-in and job creation.
- `lease_next_job` strategy gate (any-match), ordered per D-GATE.
- Repo-policy default as the first population source.

### A3 — Reference REVIEW_CMD for contributor-side agents (#61)

- Reference script(s) in `client/examples/` honoring D-REVIEWCMD: a
  simple pass-through (lease → fetch diff via `gh` → review → emit) and
  the two-model cheap-fix/expensive-review loop.
- Documentation for both wiring paths: CLI
  (`REVIEW_CMD=<script> bingo_client.py loop` — the reviewer command is
  the `REVIEW_CMD` environment variable; `loop` has no flag for it) and
  MCP (`bingo_mcp.py`: `list_jobs → lease_job → report_result`).
- Dry-run default per D-REVIEWCMD.

### A4 — Model allowlist + model groups (#65)

- Runtime identity at check-in; `accepted_models` /
  `accepted_model_groups` on `RepoPolicy`; group names are a known
  registry validated at policy-set time.
- Allowlist gate in `lease_next_job`, ordered per D-GATE; empty =
  match-any.
- Tests: a client whose declared model is not accepted never leases;
  empty allowlist leases to anyone above the tier floor; a group edit
  applies to subsequent leases.

## Out of scope

- **Webhook-out for fire-and-forget consumers.** SSE requires a held
  connection; a cron-based consumer can't hold one. Revisit when a real
  consumer hits that wall — not before.
- **Event types beyond `job.relayed`.** `REPORTED`, `EXHAUSTED`,
  `CANCELLED`, and contributor-side `job.queued` push are follow-ups once
  a consumer demonstrates the need.
- **Verifying declared model identity.** Capability honesty stays an open
  question (PITCH.md); D-TRUST's boundary is registration + declaration.
- **Reputation scoring / sampled re-review.** Deferred by D-TRUST.
- **Hub-level multi-round jobs.** Cross-model loops live inside one
  `REVIEW_CMD` per D-REVIEWCMD.
- **Budget and schedule declarations.** "Spend at most N tokens" /
  "only run 9pm–6am" is client-side wrapper config for now; a hub-side
  capacity declaration at check-in (`max_rounds`, `available_until`) is
  future work.
- **Per-PR strategy request endpoint and PR-label mapping.** Population
  sources beyond the repo-policy default follow once the contract exists.
- Any change to prompting, review depth, or aggregation (RFC 0001
  D-ONEJOB stands; the hub still never sees a prompt).

## Open-source considerations

Model groups are the hub's first *curation* surface — someone maintains
what `enterprise-approved` contains. That is still policy, not prompting,
so it sits inside the pitch's one policy exception, but it is a new kind of
hub responsibility: group definitions are operator config, and a
self-hosted grid defines its own. No group definition, secret, or hostname
lands in tracked files — same posture as RFC 0001 and 0002.

## References

- [RFC 0001 — GitHub-derived identity and repo-scoped authorization](2026-08-07-github-identity-grid-design.md)
- [RFC 0002 — Unattended operation and the management surface](2026-08-12-work-grid-operations.md)
- PITCH.md — non-goals, the policy exception, open questions
- Discussion issues: #62 (SSE), #63 (strategy contract), #64 (bidirectional
  compute and trust)

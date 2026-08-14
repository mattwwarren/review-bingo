# review-bingo

A plug-in compute grid for agentic PR review — bring your own tokens or local
compute, check in for a round of reviews, check out when you need your
compute back.

**Status: v1 prototype.** The full loop works end to end: GitHub webhook →
policy floor → client lease → report → result relayed to the PR. Concept and
open design questions live in [PITCH.md](PITCH.md).

## See it run (no GitHub required)

```
scripts/demo.sh
```

Boots an ephemeral Postgres + the hub (relay in log mode), sets a repo policy
floor, delivers a fake `pull_request` webhook, then a client registers,
checks in, leases the job, reports a round, and the rendered PR comment is
printed. Requires docker, uv, curl.

```
scripts/demo-tiers.sh
```

Same offline scaffold, walking the tier-floor scenario instead: a repo floor
above `experimental` blocks an experimental client (dry queue, then 403 on
naming the job directly) while a frontier client leases that exact job.

## Layout

- [`hub/`](hub/) — the grid operator (FastAPI, from `fastapi-template`):
  GitHub App webhook intake, client registry with capability declarations,
  job queue with leases, per-repo minimum-model-tier policy, result relay
  (GitHub App comment, or log mode without credentials)
- [`client/`](client/) — thin single-file CLI: check in → lease → run *your*
  reviewer (`REVIEW_CMD`) → report → check out
- [`dashboard/`](dashboard/) — command center: live PR queue, connected
  clients, rounds in flight (React; render from `react-template`, later)

## Wiring up real GitHub

1. Create a GitHub App (pull request events, webhook → `POST /webhooks/github`).
2. Set `GITHUB_WEBHOOK_SECRET`, `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY` in
   the hub's environment; without the App credentials the relay logs instead
   of posting.
3. Install the App on the repos you want reviewed; set policy floors via
   `PUT /policies/{owner}/{repo}`.

## Retiring a machine

A client retires itself with `DELETE /clients/{client_id}`, authenticated
either with that client's own token or with a dashboard session signed in as
the same GitHub account. The row is deleted outright: the token stops working
immediately, and any round it was holding goes straight back on the queue
instead of waiting out its lease. Use it the moment a machine is lost,
compromised, or decommissioned — check-out is a courtesy a *working* client
sends, and a stolen laptop does not send courtesies.

**Removing someone else's machine is not a hub endpoint.** Revoke their access
to the repo in GitHub, and their next check-in — or the expiry of their cached
access, whichever comes first — ends their leasing.

That is a deliberate refusal, not a missing feature. GitHub is already the
authority on who can reach a repo, and the hub only ever holds a cached, aging
copy of its answer. A hub-side "kick this person off" button would be a second
authority over the same question, one that can disagree with GitHub and always
in the more dangerous direction: still granting what GitHub has revoked. One
authority, one answer.

## Development

Each subdir owns its toolchain (`hub/`, `client/` — uv projects with pinned
lockfiles). The repo enforces its lint and type gates at commit time via
pre-commit, running the same commands CI runs with the same locked tool
versions:

```bash
uv tool install pre-commit
pre-commit install        # once per clone; worktrees share the hook
```

CI remains authoritative — the hooks exist so a formatting or typing failure
surfaces before push instead of bouncing off a CI round-trip.

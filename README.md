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

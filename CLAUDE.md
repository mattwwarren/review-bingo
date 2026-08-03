# review-bingo

Plug-in compute grid for agentic PR review: a hub (GitHub App + FastAPI)
relays PR activity to voluntarily registered clients, which run review rounds
on their own compute/tokens and report back through the hub.

Read [PITCH.md](PITCH.md) before designing anything — it fixes the
non-goals (the hub does transport/orchestration/policy, never prompting) and
the one policy exception (per-repo minimum-model floors enforced at
dispatch).

## Layout

- `hub/` — FastAPI service (from `fastapi-template`; keep its Copier answers
  file scoped here so template updates flow)
- `client/` — thin CLI: lease job → run review with local config → report
- `dashboard/` — React command center (from `react-template`; not started)

Each subdir keeps its own tooling; nothing at the repo root but docs and
cross-cutting config.

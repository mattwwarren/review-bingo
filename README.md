# review-bingo

A plug-in compute grid for agentic PR review — bring your own tokens or local
compute, check in for a round of reviews, check out when you need your
compute back.

**Status: scaffolding.** The full concept, decided structure, and open design
questions live in [PITCH.md](PITCH.md).

## Layout

- [`hub/`](hub/) — the grid operator: GitHub App webhooks, client registry,
  job dispatch, result relay (FastAPI; render from `fastapi-template`)
- [`client/`](client/) — thin CLI grid members install: lease a job, run the
  review with your own model/config, report back
- [`dashboard/`](dashboard/) — command center: live PR queue, connected
  clients, rounds in flight (React; render from `react-template`, later)

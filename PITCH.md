# review-bingo

> A plug-in compute grid for agentic code review. Teams and individuals
> contribute their own tokens or local compute to a shared PR-review pool:
> "I've got tokens — plug me in for a round of reviews."
>
> The name: clients drop in like players joining a bingo round; a PR that
> clears every reviewer's card is a bingo.

## Problem

At enterprise scale, code churn and accrued debt bottleneck everything, and
code review is where it piles up. Agentic review helps, but it makes review a
**compute cost multiplier** — every extra autonomous cycle costs more tokens.

The mitigation: a team *collectively* has far more compute than any single
budget line. Some teams have private compute where open-source models run at
the cost of electricity. Some have fat Mac minis loaded with memory running
quantized local models. Some just have spare API tokens. Where compute is
effectively "free," you *want* to offload extra independent autonomous review
cycles — chase "perfect code" as far as your electricity bill allows.

## Core concept

- **Hub (the grid operator).** A registered GitHub App monitors PR activity
  and relays it. The hub maintains a client registry, dispatches review jobs
  to connected clients, and relays results back to the PR.
- **Clients (the plugs).** Register and deregister at will — check in when you
  have spare tokens/compute, check out when you don't. Each client runs
  reviews with its **own** model, compute, prompts, and depth settings, then
  posts results back through the hub.
- **Deliberate non-goal:** the hub is transport + orchestration + policy. What
  "perfect" means, how far a review goes, the actual prompting — all
  subjective, all client-side configuration. Not this codebase's problem.
- **The policy exception — minimum viable models.** The one review-config
  knob that *is* the hub's business: repo/org owners can declare a model
  floor per repo or job class ("no experimental model configs on banking
  PRs"). Clients declare capabilities (model, provider, quant level) at
  check-in; dispatch only matches a job to clients that clear its floor.
  Policy gating, not prompting.
- **Cross-model / cross-provider loops.** A round can be a loop between two
  different models or providers — e.g. cheap model applies fixes, expensive
  model reviews the result. The hub stays agnostic about whether that's a
  good idea: let people make their own decisions; it's client-side config.

The accessibility is the point: joining the grid should be as simple as
client registration; leaving as simple as deregistration.

## Architecture sketch (from existing templates)

| Piece | Source | Role |
|-------|--------|------|
| `hub/` | `templates/fastapi-template` | GitHub App webhooks, client registry (check-in/out, capabilities, capacity), job queue + dispatch, result relay back to PRs |
| `client/` | thin CLI | Leases jobs, runs the review with local config, reports back |
| `dashboard/` | `templates/react-template` | The command center / monitor: live PR queue, connected clients, rounds in flight, config levers |

Prior art in-house: the cloud-workspace project covers a chunk of the
session/orchestration thinking.

## Open design questions (the levers)

- What is "a round of reviews" — one pass? N autonomous cycles? until quiet?
- ~~Merging/deduping when multiple clients review the same PR — who wins,
  how are findings aggregated, is there a quorum/verify step?~~
  **Resolved (D-ONEJOB):** one job per PR head stands — multiple clients
  buy throughput across different PRs, not depth on one. No quorum, no
  finding-level dedup, no multi-round comment; `enqueue_job`'s existing
  dedup on `(repo_full_name, pr_number, head_sha)` is intended behavior,
  not a limitation. See [RFC 0001](docs/superpowers/specs/2026-08-07-github-identity-grid-design.md#resolved-decisions), issue #19.
- Trust and quality weighting across heterogeneous clients (a turbo-quant
  local model vs. a frontier model shouldn't be weighted identically).
- Registration scope: org-level fleet vs. individual volunteers, auth model.
- Capability honesty: model floors only work if the client's declared
  model/provider/quant can be trusted or spot-verified — attestation, or
  social trust within a team, or sampled re-review by a known-good client?
- Job leasing, timeouts, and reassignment for clients that vanish mid-round.
- How far configuration goes before it stops being the hub's business.

## Repo structure — decided

One product repo, monorepo-lite by subdirectory — **not** a
workspace/container repo (container-as-project is exactly the conflation the
cockpit exists to undo), and not fastapi-template at root:

```
review-bingo/
  hub/        # fastapi-template render — webhooks, registry, dispatch  (now)
  client/     # thin CLI the grid members install — lease, run, report  (early)
  dashboard/  # react-template render — command center                  (later)
```

Rationale: the MVP spine is hub + client lease loop; the dashboard is
optional for a long while. Rendering each Copier template into its own
subdir (answers file scoped per subdir) keeps template updates flowing
independently. The client splits into its own repo/package only if external
users start installing it — that's the point where separate versioning
earns its keep.

## Next steps

1. ~~Pick the name~~ — **review-bingo**.
2. ~~Create the repo~~ — done if you're reading this in `companies/review-bingo/`.
3. ~~Copier-render `fastapi-template` into `hub/`~~ — done (copier branch,
   port 7575, auth off); `react-template` into `dashboard/` once the hub API
   settles.
4. ~~Define the client registration/lease API~~ — done: check-in/check-out,
   `FOR UPDATE SKIP LOCKED` leasing with lazy lease reclamation, tier-floor
   matching at dispatch, report → best-effort relay. `scripts/demo.sh` runs
   the whole loop offline.
5. Register the GitHub App (webhook → hub) against a test repo and run a
   real PR through the grid.
6. ~~Aggregation: decide what happens when multiple clients report rounds
   on the same PR~~ — resolved: one job per PR head stands (D-ONEJOB), no
   merge/dedup/quorum machinery. See [RFC 0001](docs/superpowers/specs/2026-08-07-github-identity-grid-design.md#resolved-decisions), issue #19.

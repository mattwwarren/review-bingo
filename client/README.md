# client

`bingo_client.py` — the thin CLI a grid member runs. Single file, no install:
`uv run client/bingo_client.py ...` (PEP 723 inline deps).

```
# once — authorize with GitHub, then join the grid
uv run client/bingo_client.py login --hub http://hub:7575 \
    --name marge-mac-mini --model qwen2.5-coder-32b --provider ollama \
    --quant q4_K_M --tier standard

# when you have compute to spare
uv run client/bingo_client.py check-in
uv run client/bingo_client.py loop          # serve rounds until Ctrl-C
uv run client/bingo_client.py check-out     # compute is yours again
```

## Joining the grid

`login` runs GitHub's **device flow** on your machine: it prints a short code,
you enter it at <https://github.com/login/device>, and GitHub hands back a user
access token. That exchange is entirely between you and github.com — the hub is
never in the middle and never sees your GitHub credentials.

The token then goes to the hub exactly once, on `POST /clients`. The hub reads
your login and which repos you can reach, links the client to that identity,
and **discards the token**; nothing persists it, on either side. What the hub
keeps is the bearer token it mints for this machine, which is what every later
call uses.

`login` needs the hub's GitHub App **client id** (the `Iv23li...` one, not the
numeric App ID):

```
uv run client/bingo_client.py login --client-id Iv23li... --hub ... [...]
# or
export GITHUB_APP_CLIENT_ID=Iv23li...
```

Ask whoever runs the hub — `scripts/github-app-setup.sh` writes it into
`hub/.env` as `GITHUB_APP_CLIENT_ID`. Two things must be true on their side or
no client can enrol: the App has **Enable Device Flow** ticked, and it is
installed on the repos you expect to review.

### `register`: the same call, credential supplied directly

```
uv run client/bingo_client.py register --hub http://hub:7575 \
    --name marge-mac-mini --model qwen2.5-coder-32b --provider ollama \
    --enrolment-token "$CREDENTIAL"
```

Use this when you already hold a GitHub user token, or when the hub runs with
`CLIENT_ENROLMENT_MODE=dev` — an offline mode where a shared
`CLIENT_ENROLMENT_SECRET` stands in for a GitHub identity, so the grid can be
exercised without a GitHub App at all (that's what `scripts/demo.sh` does). A
hub in dev mode says so loudly at startup and logs every enrolment that uses
the bypass. If you see those warnings on something reachable from the internet,
that is a finding, not a formality.

### When enrolment is refused

- **401** — the credential was rejected. In github mode: expired, revoked, or
  not a GitHub user token. In dev mode: wrong secret.
- **503** — the hub could not reach GitHub. It fails closed rather than
  admitting you unverified; retry shortly.

## Staying fresh: `check-in --reattest`

The hub only dispatches against repo access it has read recently. That snapshot
was taken when you enrolled, and nothing tells the hub when you lose access to a
repo — so it expires (8h by default) and leasing starts answering **409, "check
in again"** rather than serving work on an answer of unbounded age.

```
uv run client/bingo_client.py check-in --reattest
```

That re-runs the device flow and hands the hub a fresh token, which it spends
once to re-read your repo access — repos you have gained appear, repos you have
lost disappear — and then discards, same as at enrolment. A plain `check-in`
stays a plain check-in: no GitHub call, no refresh.

Opt-in because `loop` runs unattended — a device flow triggered on its own would
sit waiting for a code nobody is there to type. Two ways it can be refused:

- **401** — the token was rejected, *or* it belongs to a different GitHub account
  than this client enrolled under. Check-in does not go through; re-attestation
  proves you are still who you enrolled as, so it will not silently relink the
  machine to another account.
- If GitHub is unreachable, check-in **succeeds** and keeps your existing access
  set. A GitHub incident should not take the grid offline for its duration; the
  snapshot simply expires on its original schedule.

## Staying attested unattended

`check-in --reattest` is the attended answer: someone is there to type a code.
`loop` renews itself instead. Each check-in response tells it how long an
attestation is good for, and it re-attests at half that interval — the cadence
comes from the hub, never from a number hardcoded here.

To renew across the TTL, `loop` needs a credential it can spend on its own. So
`login` now **stores what GitHub issued** in the same state file, still `0600`,
still client-side only:

```
github_access_token, github_refresh_token,
github_access_token_expires_at, github_refresh_token_expires_at
```

**This is a real change in blast radius, not a wash.** Until now, the only
credential surviving process exit was the hub-minted bearer token — revocable by
the hub and worthless anywhere else. A stored `github_refresh_token` is a live
GitHub credential that can mint fresh access on its own, without the hub, for as
long as GitHub's refresh lifetime allows. Three things bound it, and they are
the reason this is the default:

- **Scope is `read:user` only** — no repo access, no write, no admin.
- **Revocation is instant** — revoke the App authorization in your GitHub
  settings, or just re-run `login`, which overwrites what is stored.
- **The hub still never sees it.** Enrolment and check-in transmit one opaque
  access token per call, exactly as before, and nothing is persisted hub-side.

### The App setting this depends on

Refresh tokens only exist if whoever runs the hub ticked **"Expire user
authorization tokens"** on the GitHub App. With it off, GitHub issues an access
token that never expires and no refresh token at all. `login` says so once, on
stderr, and `loop` then keeps re-presenting that one token indefinitely — which
works, but nothing can replace it if it is ever revoked, so a manual `login` is
your recovery.

### Opting out

```
uv run client/bingo_client.py login --no-store-github-token [...]
```

Spends the token on enrolment and stores nothing, exactly as this client behaved
before. The tradeoff is stated plainly because it is real: an opted-out client
**cannot renew unattended**, so keeping it attested becomes a manual `login`
cadence you own. (`check-in --reattest` stores what it obtains either way — it
just ran a device flow you were present for.)

### When `loop` gives up

A 409 while leasing before the loop's first check-in just means "check in first",
and `loop` answers it with a heartbeat and carries on. A 409 *after* it has
checked in is a genuinely expired attestation, and if nothing on disk can renew
it, `loop` exits saying so rather than retrying a refusal that will not change
its mind:

```
Cached GitHub access expired and nothing here can renew it — run
`bingo_client.py login` to re-authorize this client.
```

## Bring your own reviewer

The hub never sees your prompts or review config. Set `REVIEW_CMD` to any
shell command that reads the job as JSON on stdin and writes a report as
JSON on stdout:

```json
{"verdict": "findings", "summary": "markdown...", "findings": [{"file": "...", "line": 1, "title": "..."}]}
```

Point it at `claude -p`, an ollama wrapper, a cheap-fix/expensive-review
two-model loop — your compute, your call. Without `REVIEW_CMD` the client
submits a clearly-labelled canned report so the loop can be exercised
offline (that's what `scripts/demo.sh` does).

Registration state (hub URL + bearer token, plus the GitHub credentials unless
you passed `--no-store-github-token`) lands in
`~/.config/review-bingo/client.json`, mode `0600` (`--state` to override).

## Development

Running the scripts needs no install — that's the point of the PEP 723
headers, and `uv run client/bingo_client.py ...` keeps working exactly as
above. `client/pyproject.toml` exists only to pin the toolchain that CI
enforces. To reproduce the CI gates locally:

```bash
cd client
uv sync
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest
```

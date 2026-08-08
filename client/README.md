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

Registration state (hub URL + bearer token) lands in
`~/.config/review-bingo/client.json` (`--state` to override).

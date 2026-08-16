# Reference `REVIEW_CMD` scripts

Two working reviewers you can point the client at today, and read before you
write your own. Neither is a product surface: the hub never sees these files,
never sees your prompts, and never sees which model answered. That is the whole
point — these are examples of the contributor's half of the grid, and they are
meant to be copied and rewritten.

- **`pass_through_review.py`** — read-only. `gh pr diff` → your model → verdict.
  No clone, no working copy, no write of any kind.
- **`two_model_review.py`** — cheap-fix / expensive-review. A small model drafts
  a patch into a throwaway clone; an expensive model reviews the PR knowing what
  that patch would change. The patch comes back as text in the report and goes
  nowhere else.
- **`_common.py`** — the plumbing they share. Not a `REVIEW_CMD` itself.

## The contract, in one paragraph

`REVIEW_CMD` is any shell command. The client hands it the job as JSON on stdin
and parses **all** of its stdout as the JSON report:

```json
{"verdict": "findings", "summary": "markdown...", "findings": [{"file": "charge.py", "line": 42, "title": "..."}]}
```

Two consequences worth internalising before you write your own:

- **stdout is the report and nothing else.** One stray `print` — a progress
  line, a "thinking...", a model's banner — and the round fails to parse.
  Everything diagnostic goes to stderr. In these scripts, `_common.emit()` is
  the only function that writes to stdout, and every subprocess runs with
  `capture_output=True` so a chatty `gh` cannot leak into it.
- **Exit 0, always.** `bingo_client.run_review` invokes `REVIEW_CMD` with
  `check=True`. A nonzero exit is not a failed round — it is an exception
  raised inside an unattended `loop`. Every failure in these scripts becomes
  `{"verdict": "error", "summary": "<what broke>", "findings": []}` instead.

## Prerequisites

**`gh`, installed and separately authenticated.** This is a *second* credential,
distinct from the one you enrolled with. The hub's device flow asks GitHub for
`read:user` scope only — it cannot read a diff, and it is deliberately never
written anywhere these scripts can reach it. Give `gh` your own repo-read
credential:

```bash
gh auth login          # or export GH_TOKEN=...
gh auth status         # should list the account and its scopes
```

It needs read access to the repos you expect to be dispatched work for. If it
does not have it, the round degrades to an error report naming the repo — which
is the correct outcome, not a bug: you were asked to review something you cannot
see.

**`git`**, for `two_model_review.py` only.

Neither script installs anything. Both carry PEP 723 headers with no
dependencies beyond the standard library, so `uv run` is enough.

## Running them

```bash
# read-only, one model
REVIEWER_MODEL_CMD="claude -p" \
REVIEW_CMD="uv run client/examples/pass_through_review.py" \
    uv run client/bingo_client.py loop

# two models, fix step still off (dry run is the default)
FIX_MODEL_CMD="ollama run qwen2.5-coder:7b" \
REVIEW_MODEL_CMD="claude -p" \
REVIEW_CMD="uv run client/examples/two_model_review.py" \
    uv run client/bingo_client.py loop

# two models, fix step on
REVIEW_CMD_DRY_RUN=0 \
FIX_MODEL_CMD="ollama run qwen2.5-coder:7b" \
REVIEW_MODEL_CMD="claude -p" \
REVIEW_CMD="uv run client/examples/two_model_review.py" \
    uv run client/bingo_client.py loop
```

There is no `--review-cmd` flag. `REVIEW_CMD` is read from the environment, so
it can be set once in the shell or the unit file that runs `loop`.

To try a script without the hub in the picture at all, feed it a job by hand:

```bash
echo '{"repo_full_name": "owner/repo", "pr_number": 1, "head_sha": "abc123"}' \
    | REVIEWER_MODEL_CMD="claude -p" uv run client/examples/pass_through_review.py
```

## Environment variables

Every model slot is its own variable, and **none of them has a default**. A
reference script that silently reaches for one vendor's CLI when a variable is
unset is a script that couples the grid to that vendor by accident, so an unset
slot is an error report naming the variable instead.

- **`REVIEWER_MODEL_CMD`** — `pass_through_review.py`'s single model. Required.
- **`FIX_MODEL_CMD`** — `two_model_review.py`'s cheap patch-drafting model.
  Required only when the fix step is enabled.
- **`REVIEW_MODEL_CMD`** — `two_model_review.py`'s reviewing model. Required.
- **`REVIEW_CMD_DRY_RUN`** — `0` enables the local fix step. Anything else, and
  unset, means dry run. Default: dry run.

A model command is any shell command that **reads a prompt on stdin and writes
its answer on stdout**. Anything satisfying that works; these are examples, not
endorsements:

```bash
REVIEWER_MODEL_CMD="claude -p"
REVIEWER_MODEL_CMD="ollama run qwen2.5-coder:32b"
REVIEWER_MODEL_CMD="llm -m gpt-4o"
REVIEWER_MODEL_CMD="./my-wrapper.sh"          # your own script, your own API
```

The scripts ask the model for a JSON object and parse tolerantly — a fenced
code block, or prose either side of the object, both work. A model that answers
with prose and no JSON at all produces an error report, which is honest:
nothing reviewable came back.

`REVIEW_CMD_DRY_RUN` reads oddly on purpose. It is a **safety default, not a
verbosity flag**: the fix step writes a model's patch into a working copy, so
the variable is written so that every value except one explicit opt-out leaves
that off. Getting it wrong in the other direction would mean a patch landing in
a checkout you did not expect to be written to.

## What `two_model_review.py` does, and where the blast radius ends

1. `gh repo clone` into a fresh temp directory, then `git checkout <head_sha>` —
   the exact commit the job names, not the PR's branch, because the branch moves
   and a review reported against a different commit is a review of something
   else.
2. `gh pr diff` for the diff under review.
3. **Only with `REVIEW_CMD_DRY_RUN=0`:** ask `FIX_MODEL_CMD` for a unified diff,
   write it to a file in that same temp directory, and `git apply` it inside the
   clone. A patch that will not apply is an error report, never a silent skip.
4. Ask `REVIEW_MODEL_CMD` to review the PR, handing it the candidate patch
   alongside the PR's own diff.
5. Report. When a patch was applied, the report carries it under a top-level
   `patch` key — additive and optional, so a consumer that does not know about
   it is unaffected.
6. The temp directory is deleted when the script returns, success or failure.

**Nothing is committed. Nothing is pushed. No PR is opened, no comment is left.**
The patch's only route back to a human is as text in the report the hub relays.
Graduating that to an auto-push is an operator customization of your own copy —
it is deliberately not a mode of this script, and no flag turns it on.

## The MCP path

`client/bingo_mcp.py` exposes the same loop to any MCP client, so an agent can
be the reviewer directly instead of being wrapped in a `REVIEW_CMD`. The tools
an MCP client actually sees are:

- `check_in()` — declare this client available. Do this first; checking in is
  the grid's availability signal.
- `list_queued_jobs()` — queued work, with the repo, PR, and the tier floor each
  requires.
- `lease(job_id)` — take one named job. The lease is a deadline, not a
  reservation.
- `report(job_id, verdict, summary, findings)` — submit the finished round.
- `check_out()` — stop being offered work.

The underlying functions in that file are named `list_jobs`, `lease_job` and
`report_result`, which is what you will find in the source and in RFC 0003; the
tool names above are what an MCP client calls.

Everything in this directory applies on that path too, except the stdout rule —
an MCP agent returns a value rather than printing one. The `gh` prerequisite,
the "review the head_sha you were given" discipline, and the "an error report
beats a crash" posture are all the same.

## Writing your own

Steal `_common.py`. It is small and it holds the parts that are easy to get
subtly wrong: reading the job tolerantly (the hub sends the whole of
`ReviewJobRead`; read the fields you need and ignore the rest, so a field added
later cannot break you), resolving `gh`/`git` to absolute paths before invoking
them, putting an explicit timeout on every subprocess (a `REVIEW_CMD` that hangs
holds its lease until the hub reclaims it and gives the job to someone else),
and turning every failure into a report rather than an exit code.

Tests live beside the scripts and run in the same CI gate as the rest of
`client/`:

```bash
cd client
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest
```

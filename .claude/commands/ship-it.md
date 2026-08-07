# ship-it (review-bingo)

Ship the current branch as a PR. Invoked by `/prep-pr` Step 8, or directly.

This repo is monorepo-lite: `hub/` (FastAPI, uv), `client/` (single-file CLI),
`dashboard/` (not started). Root CI (`.github/workflows/ci.yml`) runs the hub
gates with `working-directory: hub`, so a PR touching only `scripts/` still
gets a green check — CI passing is not evidence that shell changes were tested.

## Arguments

- `--draft` — open the PR as a draft
- `--title <title>` — override the generated title
- `--headless` — never prompt; on any ambiguity emit the block format `/prep-pr` expects

## Step 1: Preconditions

```bash
git branch --show-current          # must not be main
git status --short                 # must be clean; /prep-pr commits before calling this
```

If the tree is dirty, stop — committing is `/prep-pr`'s job, not this command's.

## Step 2: Push

```bash
BRANCH=$(git branch --show-current)
git push -u origin HEAD:refs/heads/"$BRANCH"
git fetch origin "$BRANCH"
test "$(git rev-parse origin/"$BRANCH")" = "$(git rev-parse HEAD)"
```

A mismatch means the push silently failed — stop and report, do not open a PR
against a stale remote branch.

## Step 3: Open the PR

Title: `<type>(#<issue>): <what changed>` matching the commit convention
(`fix(#1623): …`). Body:

```markdown
## Summary

<what this changes and why, in prose>

## Testing

<what was actually run — name the gates and their result. "hub: 581 passed,
mypy clean, ruff clean" beats "tests pass". If something was verified against
the live hub or real GitHub traffic, say so and say how.>

## Notes

<anything the reviewer would otherwise have to discover: settings changed,
follow-up tickets filed, deliberate omissions>

Closes #<issue>
```

```bash
gh pr create --base main --head "$BRANCH" --title "<title>" --body "<body>"
```

Use `--draft` if requested. `Closes #N` only when the PR genuinely completes
the ticket — a partial step gets `Refs #N` so the issue survives the merge.

## Step 4: Auto-merge

Arm it:

```bash
gh pr merge --auto --squash
```

Both preconditions this step used to wait on are now met (2026-08-07):

- `allow_auto_merge` is enabled at the repo level.
- `main` has an active ruleset requiring the `Tests (hub)` check, with
  squash-only merges, linear history, and no bypass actors.

So auto-merge has something real to wait for. It fires when `Tests (hub)`
reports green, not on creation. Squash is the only method the ruleset permits —
passing `--merge` or `--rebase` will be rejected.

Earlier revisions of this file said the opposite, and said so for a good
reason: with no required checks, arming auto-merge merges a PR essentially
immediately, before CI reports anything. That reasoning was correct while it
held. It stopped holding when the ruleset landed, and a stale instruction here
is expensive — issue #19 shipped a correct PR (#28) that then sat unmerged with
green checks, because the worker read this step and declined to arm.

Two things worth knowing, since neither is obvious from a green PR:

- The ruleset requires only `Tests (hub)`. `Lint & type-check (hub)` runs and
  reports but **cannot block a merge**, so a ruff or mypy failure will not stop
  auto-merge. Do not treat "auto-merge fired" as evidence the lint gate passed;
  read it.
- `delete_branch_on_merge` is off, so merged branches persist. `cw worktree gc
  --apply` prunes worktrees whose PR merged; the branches themselves need
  `--delete-branch` at merge time or manual cleanup.

## Step 5: Register the review monitor

```bash
PR_NUMBER=$(gh pr view --json number --jq .number)
REPO=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
REPO_PATH=$(git rev-parse --show-toplevel)
HEAD_SHA=$(gh pr view --json headRefOid --jq .headRefOid)

~/.claude/scripts/review_monitor.py register "$PR_NUMBER" \
  --role author --repo "$REPO" --repo-path "$REPO_PATH" --sha "$HEAD_SHA"
```

## Step 6: Report

Print the PR URL, the auto-merge state (and why), and the gate results the PR
body claims. If any step was skipped, say which and why — a ship report that
omits a skipped step is the failure this file exists to prevent.

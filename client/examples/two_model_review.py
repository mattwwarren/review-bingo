#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""A cheap-fix / expensive-review `REVIEW_CMD`: two models, two jobs.

    REVIEW_CMD_DRY_RUN=0 \\
    FIX_MODEL_CMD="ollama run qwen2.5-coder:7b" \\
    REVIEW_MODEL_CMD="claude -p" \\
    REVIEW_CMD="uv run client/examples/two_model_review.py" \\
        uv run client/bingo_client.py loop

The shape is the one the pitch keeps describing: a small local model drafts a
patch, and an expensive model reviews the PR knowing what that patch would
change. Two model slots, so the cheap one does the volume work and the
expensive one is spent once, on judgement.

**Where the patch goes.** Into a throwaway clone in a temp directory, and
nowhere else. The clone is deleted when this script returns, success or
failure. Nothing is committed, nothing is pushed, no PR is opened — the patch
travels back to the hub as text in the report, for a human to look at. Wiring
this up to push is a change an operator makes deliberately to their own copy;
it is not a mode of this script.

**The fix step is off by default.** It only runs with `REVIEW_CMD_DRY_RUN=0`
explicitly set. Unset, or set to anything else, this behaves like the
pass-through reviewer with an extra clone: the expensive model reviews the diff
and no patch is generated at all.

Prerequisites: `gh` and `git`, both installed, with `gh` authenticated
*separately* from your hub enrolment — see `client/examples/README.md`.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any

from _common import (
    GIT_TIMEOUT_SECONDS,
    MODEL_TIMEOUT_SECONDS,
    REPORT_CONTRACT,
    GitError,
    PatchApplyError,
    ReviewCmdError,
    emit,
    error_report,
    fetch_pr_diff,
    is_dry_run,
    log,
    parse_model_report,
    read_job,
    run_gh,
    run_git,
    run_model_cmd,
    strip_code_fence,
)

FIX_ENV_VAR = "FIX_MODEL_CMD"
REVIEW_ENV_VAR = "REVIEW_MODEL_CMD"

FIX_PROMPT = """A pull request needs a fix. Write the patch, nothing else.

Repository: {repo}
Pull request: #{pr_number}
Head commit: {head_sha}

--- BEGIN DIFF ---
{diff}
--- END DIFF ---

Answer with a unified diff that applies cleanly with `git apply` against the
head commit above, and with no prose around it. Paths must be the repository
paths shown in the diff, with the usual a/ and b/ prefixes. If nothing needs
fixing, answer with an empty patch."""

REVIEW_PROMPT = """You are reviewing a GitHub pull request.

Repository: {repo}
Pull request: #{pr_number}
Head commit: {head_sha}

--- BEGIN DIFF ---
{diff}
--- END DIFF ---
{candidate}
{contract}
"""

# Handed to the expensive model alongside the PR's own diff rather than instead
# of it: a post-fix diff alone would hide what the PR originally did, and the
# review is of the PR, not of the patch.
CANDIDATE_FIX = """
A cheap model drafted the following patch and it applied cleanly to a local
checkout. It has NOT been pushed anywhere. Treat it as a proposal — say whether
it is right, and review the pull request itself either way.

--- BEGIN CANDIDATE PATCH ---
{patch}
--- END CANDIDATE PATCH ---
"""


def clone_and_checkout(job: dict[str, Any], clone_dir: Path) -> None:
    """A throwaway clone pinned to the exact commit the job names.

    `head_sha`, not the PR's branch: the branch moves, and a review reported
    against a commit the hub did not dispatch is a review of something else.
    """
    args = ["repo", "clone", job["repo_full_name"], str(clone_dir), "--", "--quiet"]
    run_gh(args, timeout=GIT_TIMEOUT_SECONDS)
    run_git(["checkout", job["head_sha"]], cwd=clone_dir, timeout=GIT_TIMEOUT_SECONDS)


def build_fix_prompt(job: dict[str, Any], diff: str) -> str:
    return FIX_PROMPT.format(
        repo=job["repo_full_name"],
        pr_number=job["pr_number"],
        head_sha=job["head_sha"],
        diff=diff,
    )


def build_review_prompt(job: dict[str, Any], diff: str, patch: str | None) -> str:
    return REVIEW_PROMPT.format(
        repo=job["repo_full_name"],
        pr_number=job["pr_number"],
        head_sha=job["head_sha"],
        diff=diff,
        candidate=CANDIDATE_FIX.format(patch=patch) if patch else "",
        contract=REPORT_CONTRACT,
    )


def apply_fix(job: dict[str, Any], diff: str, clone_dir: Path, patch_path: Path) -> str:
    """Ask the cheap model for a patch and apply it inside the throwaway clone.

    Returns the patch text so the report can carry it. A patch that will not
    apply is an error, not a silent skip — a review that claims a fix was
    applied when it was not is worse than no review.
    """
    patch = strip_code_fence(run_model_cmd(FIX_ENV_VAR, build_fix_prompt(job, diff), timeout=MODEL_TIMEOUT_SECONDS))
    if not patch.strip():
        empty = f"{FIX_ENV_VAR} returned an empty patch"
        raise PatchApplyError(empty)

    # `git apply` wants a trailing newline; a fenced answer loses its own.
    patch_path.write_text(patch if patch.endswith("\n") else patch + "\n")
    try:
        run_git(["apply", str(patch_path)], cwd=clone_dir, timeout=GIT_TIMEOUT_SECONDS)
    except GitError as exc:
        raise PatchApplyError(f"the {FIX_ENV_VAR} patch did not apply cleanly: {exc}") from exc
    log(f"applied a {len(patch)}-character patch inside {clone_dir}")
    return patch


def review_result(job: dict[str, Any], diff: str, patch: str | None) -> dict[str, Any]:
    """The expensive model's report, carrying the candidate patch when there is one."""
    prompt = build_review_prompt(job, diff, patch)
    report = parse_model_report(run_model_cmd(REVIEW_ENV_VAR, prompt, timeout=MODEL_TIMEOUT_SECONDS))
    if patch is not None:
        # Additive and optional: the hub relays a report's extra keys without
        # caring about them, so a patch here costs nothing to a consumer that
        # does not know about it yet.
        report["patch"] = patch
    return report


def run(job: dict[str, Any]) -> dict[str, Any]:
    """One round, entirely inside a directory that does not outlive it."""
    with tempfile.TemporaryDirectory(prefix="review-bingo-") as workspace:
        clone_dir = Path(workspace) / "repo"
        clone_and_checkout(job, clone_dir)
        diff = fetch_pr_diff(job)
        if is_dry_run():
            log(f"dry run (set REVIEW_CMD_DRY_RUN=0 to enable the {FIX_ENV_VAR} step)")
            patch = None
        else:
            patch = apply_fix(job, diff, clone_dir, Path(workspace) / "fix.patch")
        return review_result(job, diff, patch)


def main() -> None:
    """Always emits a report, always exits 0 — see `pass_through_review.main`."""
    try:
        job = read_job(sys.stdin.read())
        log(f"reviewing {job['repo_full_name']}#{job['pr_number']} @ {job['head_sha'][:12]}")
        report = run(job)
    except ReviewCmdError as exc:
        report = error_report(str(exc))
    except Exception as exc:
        report = error_report(f"two_model_review.py failed unexpectedly: {exc!r}")
    emit(report)


if __name__ == "__main__":
    main()

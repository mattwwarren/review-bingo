#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""The smallest useful `REVIEW_CMD`: read the PR's diff, ask a model, report.

    REVIEWER_MODEL_CMD="claude -p" \\
    REVIEW_CMD="uv run client/examples/pass_through_review.py" \\
        uv run client/bingo_client.py loop

One `gh pr diff`, one model call, one JSON report on stdout. Nothing is cloned,
nothing is written, nothing is pushed — the whole script is a read of the diff
and an opinion about it, which is why it is the one to start from.

The model is yours: `REVIEWER_MODEL_CMD` is any shell command that reads a
prompt on stdin and writes its answer on stdout. There is no default, on
purpose — see `client/examples/README.md`.

Prerequisite: `gh`, installed and authenticated *separately* from your hub
enrolment. The hub's device-flow token is `read:user` scope and cannot read a
diff; this needs your own repo-read credential and never leaves your machine.
"""

from __future__ import annotations

import sys
from typing import Any

from _common import (
    MODEL_TIMEOUT_SECONDS,
    ReviewCmdError,
    emit,
    error_report,
    fetch_pr_diff,
    log,
    parse_model_report,
    read_job,
    run_model_cmd,
)

REVIEWER_ENV_VAR = "REVIEWER_MODEL_CMD"

# Stated as a contract rather than a suggestion because `parse_model_report` is
# tolerant but not psychic: it can dig a JSON object out of surrounding prose,
# it cannot invent one.
REPORT_CONTRACT = """Answer with a single JSON object and nothing else:

{
  "verdict": "approve" or "findings",
  "summary": "markdown; this is posted to the pull request verbatim",
  "findings": [{"file": "path/to/file.py", "line": 42, "title": "one line, specific"}]
}

Report only what the diff itself shows. An empty `findings` list with an
"approve" verdict is a valid and often correct answer."""

PROMPT = """You are reviewing a GitHub pull request.

Repository: {repo}
Pull request: #{pr_number}
Head commit: {head_sha}

--- BEGIN DIFF ---
{diff}
--- END DIFF ---

{contract}
"""


def build_prompt(job: dict[str, Any], diff: str) -> str:
    """The whole prompt, in one place, so it is obvious what the model is told."""
    return PROMPT.format(
        repo=job["repo_full_name"],
        pr_number=job["pr_number"],
        head_sha=job["head_sha"],
        diff=diff,
        contract=REPORT_CONTRACT,
    )


def review(job: dict[str, Any]) -> dict[str, Any]:
    """Fetch, ask, parse. The importable core, so a test never touches `main`."""
    diff = fetch_pr_diff(job)
    log(f"diff is {len(diff)} characters; asking {REVIEWER_ENV_VAR}")
    answer = run_model_cmd(REVIEWER_ENV_VAR, build_prompt(job, diff), timeout=MODEL_TIMEOUT_SECONDS)
    return parse_model_report(answer)


def main() -> None:
    """Always emits a report, always exits 0.

    `bingo_client.run_review` invokes `REVIEW_CMD` with `check=True`, so a
    nonzero exit here does not fail one round — it raises inside an unattended
    `loop`. A bad round is a report that says so.
    """
    try:
        job = read_job(sys.stdin.read())
        log(f"reviewing {job['repo_full_name']}#{job['pr_number']} @ {job['head_sha'][:12]}")
        report = review(job)
    except ReviewCmdError as exc:
        report = error_report(str(exc))
    except Exception as exc:
        report = error_report(f"pass_through_review.py failed unexpectedly: {exc!r}")
    emit(report)


if __name__ == "__main__":
    main()

"""Plumbing shared by the reference `REVIEW_CMD` scripts in this directory.

Not a `REVIEW_CMD` itself — the leading underscore says so. It exists because
`pass_through_review.py` and `two_model_review.py` have to agree, exactly, on
four things that are easy to get subtly wrong:

**stdout is the report.** `bingo_client.run_review` parses the whole of a
script's stdout as JSON. So `emit()` is the only function here that writes to
stdout, everything else goes to stderr through `log()`, and every subprocess is
run with `capture_output=True` so a chatty `gh` or a progress-bar-happy model
cannot land a byte on it. Whatever the child said on stderr is forwarded to
ours, where it is useful for debugging and harmless to the contract.

**Nothing escapes.** The parent invokes `REVIEW_CMD` with `check=True`, so a
nonzero exit is not a failed round — it is an exception inside an unattended
`loop`. Every failure in here is a typed `ReviewCmdError` the caller turns into
`error_report(...)`, and every subprocess gets an explicit timeout, because a
`REVIEW_CMD` that hangs holds its lease until the hub reclaims it.

**Dry-run is the default.** Read `is_dry_run()` before doing anything that
writes to a working copy.

**The job payload is read, not validated.** The hub sends the whole of
`ReviewJobRead`; these scripts want four fields of it and ignore the rest, so a
field added hub-side later cannot turn a contributor's working script into a
crashing one.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import Any

# Explicit on every call. A hung `REVIEW_CMD` is worse than a failed one: the
# lease is held until the hub expires it, and the job is requeued to somebody
# else while this process sits there.
GH_TIMEOUT_SECONDS = 120.0
GIT_TIMEOUT_SECONDS = 300.0
MODEL_TIMEOUT_SECONDS = 900.0

# What these scripts read out of the job. Everything else in `ReviewJobRead`
# (state, min_tier, lease_expires_at, attempts, timestamps, ...) is deliberately
# read past — see the module docstring.
REQUIRED_JOB_KEYS = ("repo_full_name", "pr_number", "head_sha")

DRY_RUN_ENV_VAR = "REVIEW_CMD_DRY_RUN"
# Dry-run is the default and only this exact string turns it off. "false", "no",
# "off" and "" all stay dry, because guessing wrong in that direction means a
# model's patch landing in a working copy nobody expected to be written to.
DRY_RUN_DISABLED = "0"

INSTALL_HINTS = {
    "gh": "https://cli.github.com — then `gh auth login`",
    "git": "https://git-scm.com/downloads",
}

FENCE = "```"

# Stated as a contract rather than a suggestion because `parse_model_report` is
# tolerant but not psychic: it can dig a JSON object out of surrounding prose,
# it cannot invent one. Shared by both scripts' review prompts.
REPORT_CONTRACT = """Answer with a single JSON object and nothing else:

{
  "verdict": "approve" or "findings",
  "summary": "markdown; this is posted to the pull request verbatim",
  "findings": [{"file": "path/to/file.py", "line": 42, "title": "one line, specific"}]
}

Report only what the diff itself shows. An empty `findings` list with an
"approve" verdict is a valid and often correct answer."""


class ReviewCmdError(RuntimeError):
    """A round that cannot be completed. Always becomes an error report, never an exit code."""


class JobParseError(ReviewCmdError):
    """Whatever arrived on stdin was not a job this script can act on."""


class MissingExecutableError(ReviewCmdError):
    """A prerequisite the contributor has to install and authenticate themselves."""


class GhError(ReviewCmdError):
    """`gh` refused or failed — usually auth, or a PR this token cannot see."""


class GitError(ReviewCmdError):
    """`git` refused or failed."""


class PatchApplyError(GitError):
    """A patch a model wrote did not apply to the checked-out tree."""


class ModelCmdError(ReviewCmdError):
    """The operator's model command is unset, failed, or never answered."""


class ModelReportError(ReviewCmdError):
    """The model answered, but not with the JSON report it was asked for."""


def log(message: str) -> None:
    """Diagnostics belong on stderr. stdout belongs to the report, in full."""
    print(message, file=sys.stderr)


def emit(report: dict[str, Any]) -> None:
    """The single write to stdout in this directory. Keep it that way."""
    print(json.dumps(report))


def error_report(what_failed: str) -> dict[str, Any]:
    """A round that failed, said in the report vocabulary the hub already relays."""
    return {"verdict": "error", "summary": what_failed, "findings": []}


def _decode(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise JobParseError(f"REVIEW_CMD stdin was not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise JobParseError(f"REVIEW_CMD stdin was a JSON {type(payload).__name__}, not an object")
    return payload


def _narrow(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return {
            "repo_full_name": str(payload["repo_full_name"]),
            "pr_number": int(payload["pr_number"]),
            "head_sha": str(payload["head_sha"]),
            # Only ever used in log lines, so a job without one is not an error.
            "id": str(payload.get("id", "")),
        }
    except (TypeError, ValueError) as exc:
        raise JobParseError(f"job payload has an unusable field: {exc}") from exc


def read_job(raw: str) -> dict[str, Any]:
    """The four fields these scripts need, out of whatever the hub sent."""
    payload = _decode(raw)
    missing = [key for key in REQUIRED_JOB_KEYS if key not in payload]
    if missing:
        raise JobParseError(f"job payload is missing required key(s): {', '.join(missing)}")
    return _narrow(payload)


def is_dry_run() -> bool:
    """True unless the operator explicitly set `REVIEW_CMD_DRY_RUN=0`."""
    return os.environ.get(DRY_RUN_ENV_VAR) != DRY_RUN_DISABLED


def require_executable(name: str) -> str:
    """The absolute path to a prerequisite, or a typed error naming what to install.

    Absolute because the scripts then invoke it by full path (ruff S607), and
    because resolving it up front turns "not installed" into the same graceful
    error report as any other failure rather than a traceback mid-round.
    """
    path = shutil.which(name)
    if not path:
        hint = INSTALL_HINTS.get(name, "")
        raise MissingExecutableError(f"{name} is not on PATH — install it first ({hint})")
    return path


def _forward_stderr(label: str, stderr: object) -> None:
    """Re-say the child's stderr on ours. Never on stdout, whatever it contains."""
    if isinstance(stderr, str) and stderr.strip():
        log(f"[{label}] {stderr.strip()}")


def run_model_cmd(env_var: str, prompt: str, *, timeout: float) -> str:
    """Run the operator's model for one slot: prompt on stdin, answer on stdout.

    There is deliberately no default. A reference script that silently reaches
    for `claude -p` when a variable is unset is a script that couples the grid
    to one vendor by accident, so an unset slot is an error naming the variable.
    """
    command = os.environ.get(env_var)
    if not command:
        raise ModelCmdError(
            f"{env_var} is not set — these scripts ship no default model. Set it to a shell "
            f"command that reads a prompt on stdin and writes its answer to stdout."
        )
    try:
        completed = subprocess.run(  # noqa: S602 - operator-supplied command is the point
            command,
            shell=True,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise ModelCmdError(f"{env_var} did not answer within {timeout:.0f}s") from exc
    except subprocess.CalledProcessError as exc:
        _forward_stderr(env_var, exc.stderr)
        raise ModelCmdError(f"{env_var} exited {exc.returncode}") from exc
    _forward_stderr(env_var, completed.stderr)
    return completed.stdout


def _run(argv: list[str], *, timeout: float, error: type[ReviewCmdError], label: str) -> str:
    try:
        completed = subprocess.run(  # noqa: S603 - argv[0] came from shutil.which(); the rest is ours
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise error(f"{label} did not finish within {timeout:.0f}s") from exc
    except subprocess.CalledProcessError as exc:
        _forward_stderr(label, exc.stderr)
        detail = str(exc.stderr or "").strip().splitlines()
        raise error(f"{label} exited {exc.returncode}: {detail[-1] if detail else 'no output'}") from exc
    _forward_stderr(label, completed.stderr)
    return completed.stdout


def run_gh(args: list[str], *, timeout: float) -> str:
    """Run `gh` with its own credential — see this directory's README on why it is separate."""
    return _run([require_executable("gh"), *args], timeout=timeout, error=GhError, label=f"gh {' '.join(args[:2])}")


def run_git(args: list[str], *, timeout: float, cwd: str | os.PathLike[str] | None = None) -> str:
    """Run `git`, scoped to a working copy with `-C` so the target is visible in the argv."""
    scope = ["-C", str(cwd)] if cwd is not None else []
    argv = [require_executable("git"), *scope, *args]
    return _run(argv, timeout=timeout, error=GitError, label=f"git {' '.join(args[:1])}")


def fetch_pr_diff(job: dict[str, Any]) -> str:
    """The PR's diff, read through `gh`. No clone, no working copy, no write."""
    args = ["pr", "diff", str(job["pr_number"]), "--repo", job["repo_full_name"]]
    return run_gh(args, timeout=GH_TIMEOUT_SECONDS)


def strip_code_fence(text: str) -> str:
    """The body of a fenced block, or the text untouched when there is no fence.

    Models wrap output in triple backticks far more often than they are asked
    to, and a unified diff with a ```diff line on top is a diff `git apply`
    rejects. Unfenced text comes back byte-identical — including its trailing
    newline, which a patch needs.
    """
    lines = text.strip().splitlines()
    if not lines or not lines[0].startswith(FENCE):
        return text
    body = lines[1:]
    if body and body[-1].strip().startswith(FENCE):
        body = body[:-1]
    return "\n".join(body).strip()


def _as_line(value: object) -> int | None:
    """Line numbers arrive as ints, as strings, and sometimes not at all."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def _normalise_finding(item: dict[str, Any]) -> dict[str, Any]:
    """One finding in the shape the hub relays: file, line, title."""
    return {
        "file": str(item.get("file", "")),
        "line": _as_line(item.get("line")),
        "title": str(item.get("title", "")),
    }


def _first_json_object(text: str) -> dict[str, Any]:
    candidate = strip_code_fence(text).strip()
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end <= start:
        raise ModelReportError(f"the model did not answer with a JSON object: {candidate[:200]!r}")
    try:
        payload = json.loads(candidate[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ModelReportError(f"the model's JSON did not parse: {exc}") from exc
    if not isinstance(payload, dict):
        raise ModelReportError(f"the model answered with a JSON {type(payload).__name__}, not an object")
    return payload


def parse_model_report(text: str) -> dict[str, Any]:
    """A model's answer, turned into the report the hub expects.

    Tolerant on the way in — a fenced block, or prose either side of the JSON,
    both parse — and strict on the way out, so what reaches the hub is always
    `{verdict, summary, findings[{file, line, title}]}` no matter how loosely
    the model held up its end.
    """
    payload = _first_json_object(text)
    findings = payload.get("findings") or []
    if not isinstance(findings, list):
        detail = f"the model's `findings` was a {type(findings).__name__}, not a list"
        raise ModelReportError(detail)
    return {
        "verdict": str(payload.get("verdict") or "findings"),
        "summary": str(payload.get("summary") or ""),
        "findings": [_normalise_finding(item) for item in findings if isinstance(item, dict)],
    }

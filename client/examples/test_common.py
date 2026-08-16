"""Tests for `_common.py`, the plumbing both reference `REVIEW_CMD` scripts share.

Three contracts carry everything else in this directory, and they all live here:

- **stdout is the report and nothing else.** `bingo_client.run_review` parses
  the whole of a script's stdout as JSON, so one stray `print` breaks a round.
- **dry-run is the default.** Only the literal `"0"` turns the fix step on;
  anything else, including "no" and "false" and unset, stays read-only.
- **nothing raises out of a script.** The parent runs with `check=True`, so a
  nonzero exit takes down an unattended `loop`. Every failure has to become an
  error report instead.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from typing import Any

import _common
import pytest
from conftest import RecordingSubprocess


def test_read_job_extracts_required_fields_from_a_full_review_job_read_payload(job: dict[str, Any]) -> None:
    parsed = _common.read_job(json.dumps(job))

    assert parsed["repo_full_name"] == "acme/payments"
    assert parsed["pr_number"] == 7
    assert parsed["head_sha"] == "0123456789abcdef0123456789abcdef01234567"
    assert parsed["id"] == job["id"]


def test_read_job_raises_a_typed_error_on_missing_required_key(job: dict[str, Any]) -> None:
    del job["head_sha"]

    with pytest.raises(_common.JobParseError) as caught:
        _common.read_job(json.dumps(job))

    assert "head_sha" in str(caught.value)


def test_read_job_raises_a_typed_error_on_malformed_json() -> None:
    with pytest.raises(_common.JobParseError):
        _common.read_job("this is not json")


def test_is_dry_run_defaults_true_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REVIEW_CMD_DRY_RUN", raising=False)

    assert _common.is_dry_run() is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [("0", False), ("1", True), ("false", True), ("", True), ("no", True)],
)
def test_is_dry_run_false_only_for_the_literal_string_zero(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
    expected: bool,
) -> None:
    monkeypatch.setenv("REVIEW_CMD_DRY_RUN", value)

    assert _common.is_dry_run() is expected


def test_run_model_cmd_raises_when_env_var_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REVIEWER_MODEL_CMD", raising=False)

    with pytest.raises(_common.ModelCmdError) as caught:
        _common.run_model_cmd("REVIEWER_MODEL_CMD", "review this", timeout=1.0)

    assert "REVIEWER_MODEL_CMD" in str(caught.value)


def test_run_model_cmd_invokes_subprocess_with_stdin_prompt_and_returns_stdout(
    monkeypatch: pytest.MonkeyPatch,
    recording_subprocess: RecordingSubprocess,
) -> None:
    monkeypatch.setenv("REVIEWER_MODEL_CMD", "fake-reviewer --json")
    recording_subprocess.when("fake-reviewer", stdout="the model's answer", stderr="loading weights")

    answer = _common.run_model_cmd("REVIEWER_MODEL_CMD", "review this", timeout=12.0)

    assert answer == "the model's answer"
    call = recording_subprocess.calls[-1]
    assert call.cmd == "fake-reviewer --json"
    assert call.kwargs["shell"] is True
    assert call.kwargs["input"] == "review this"
    assert call.kwargs["capture_output"] is True
    assert call.kwargs["text"] is True
    assert call.kwargs["timeout"] == 12.0


def test_run_model_cmd_forwards_the_commands_stderr_to_our_stderr(
    monkeypatch: pytest.MonkeyPatch,
    recording_subprocess: RecordingSubprocess,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("REVIEWER_MODEL_CMD", "fake-reviewer")
    recording_subprocess.when("fake-reviewer", stdout="{}", stderr="loading weights")

    _common.run_model_cmd("REVIEWER_MODEL_CMD", "review this", timeout=1.0)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "loading weights" in captured.err


def test_run_model_cmd_wraps_called_process_error(
    monkeypatch: pytest.MonkeyPatch,
    recording_subprocess: RecordingSubprocess,
) -> None:
    monkeypatch.setenv("REVIEWER_MODEL_CMD", "fake-reviewer")
    recording_subprocess.when("fake-reviewer", returncode=2, stderr="out of context")

    with pytest.raises(_common.ModelCmdError):
        _common.run_model_cmd("REVIEWER_MODEL_CMD", "review this", timeout=1.0)


def test_run_model_cmd_wraps_timeout(
    monkeypatch: pytest.MonkeyPatch,
    recording_subprocess: RecordingSubprocess,
) -> None:
    monkeypatch.setenv("REVIEWER_MODEL_CMD", "fake-reviewer")
    recording_subprocess.when("fake-reviewer", raises=subprocess.TimeoutExpired("fake-reviewer", 1.0))

    with pytest.raises(_common.ModelCmdError):
        _common.run_model_cmd("REVIEWER_MODEL_CMD", "review this", timeout=1.0)


def test_run_gh_resolves_an_absolute_path_and_returns_stdout(recording_subprocess: RecordingSubprocess) -> None:
    recording_subprocess.when("pr diff", stdout="diff --git a/x b/x")

    output = _common.run_gh(["pr", "diff", "7"], timeout=5.0)

    assert output == "diff --git a/x b/x"
    call = recording_subprocess.calls[-1]
    assert call.argv == ["/usr/bin/gh", "pr", "diff", "7"]
    assert call.kwargs["timeout"] == 5.0
    assert call.kwargs["capture_output"] is True


def test_run_gh_wraps_failure_in_a_typed_error(recording_subprocess: RecordingSubprocess) -> None:
    recording_subprocess.when("pr diff", returncode=1, stderr="could not resolve to a PullRequest")

    with pytest.raises(_common.GhError):
        _common.run_gh(["pr", "diff", "7"], timeout=5.0)


def test_run_git_targets_a_working_copy_with_dash_c(recording_subprocess: RecordingSubprocess) -> None:
    _common.run_git(["checkout", "deadbeef"], timeout=5.0, cwd="/work/clone")

    assert recording_subprocess.calls[-1].argv == ["/usr/bin/git", "-C", "/work/clone", "checkout", "deadbeef"]


def test_emit_writes_exactly_one_json_line_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    report = {"verdict": "findings", "summary": "one thing", "findings": []}

    print("diagnostics belong here", file=sys.stderr)
    _common.emit(report)

    captured = capsys.readouterr()
    assert captured.out == json.dumps(report) + "\n"
    assert "diagnostics belong here" in captured.err


def test_error_report_shape() -> None:
    assert _common.error_report("gh failed") == {"verdict": "error", "summary": "gh failed", "findings": []}


def test_require_executable_raises_a_clear_message_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)

    with pytest.raises(_common.MissingExecutableError) as caught:
        _common.require_executable("gh")

    message = str(caught.value)
    assert "gh" in message
    assert "install" in message.lower()


def test_parse_model_report_accepts_a_bare_json_object() -> None:
    raw = json.dumps({"verdict": "approve", "summary": "looks fine", "findings": []})

    assert _common.parse_model_report(raw) == {"verdict": "approve", "summary": "looks fine", "findings": []}


def test_parse_model_report_tolerates_fenced_json_and_surrounding_prose() -> None:
    body = '{"verdict": "findings", "summary": "s", "findings": [{"file": "a.py", "line": 3, "title": "t"}]}'
    raw = f"Sure!\n```json\n{body}\n```\nHope that helps."

    report = _common.parse_model_report(raw)

    assert report["verdict"] == "findings"
    assert report["findings"] == [{"file": "a.py", "line": 3, "title": "t"}]


def test_parse_model_report_raises_when_there_is_no_json_at_all() -> None:
    with pytest.raises(_common.ModelReportError):
        _common.parse_model_report("I refuse to answer in JSON.")


def test_strip_code_fence_returns_the_fenced_body() -> None:
    fenced = "```diff\ndiff --git a/x b/x\n+one line\n```"

    assert _common.strip_code_fence(fenced) == "diff --git a/x b/x\n+one line"

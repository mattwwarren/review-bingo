"""Tests for `pass_through_review.py`, the read-only reference reviewer.

The script is one hop — `gh pr diff` into a model into a verdict — so what is
worth pinning is not the hop but the guarantees around it: it never clones, it
never pushes, its stdout stays parseable no matter how loud `gh` and the model
are, and every way it can fail still ends in a report the hub can read.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from typing import Any

import _common
import pass_through_review
import pytest
from conftest import RecordingSubprocess

MODEL_ANSWER = json.dumps(
    {
        "verdict": "findings",
        "summary": "One real problem.",
        "findings": [{"file": "payments/charge.py", "line": 42, "title": "Charges twice on retry"}],
    }
)


def feed_stdin(monkeypatch: pytest.MonkeyPatch, job: dict[str, Any]) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(job)))


def emitted(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    """The one JSON object the script printed, and proof it printed only that."""
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert len(lines) == 1, f"stdout must carry exactly one JSON line, got: {captured.out!r}"
    report: dict[str, Any] = json.loads(lines[0])
    return report


def test_main_emits_only_json_to_stdout_with_gh_and_model_noise_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
    recording_subprocess: RecordingSubprocess,
    capsys: pytest.CaptureFixture[str],
    job: dict[str, Any],
) -> None:
    monkeypatch.setenv("REVIEWER_MODEL_CMD", "fake-reviewer")
    recording_subprocess.when("pr diff", stdout="diff --git a/x b/x", stderr="gh: using cached token")
    recording_subprocess.when("fake-reviewer", stdout=MODEL_ANSWER, stderr="model: 12 tok/s")
    feed_stdin(monkeypatch, job)

    pass_through_review.main()

    report = emitted(capsys)
    assert report["verdict"] == "findings"
    assert report["summary"] == "One real problem."
    assert report["findings"][0]["file"] == "payments/charge.py"


def test_gh_and_model_chatter_lands_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
    recording_subprocess: RecordingSubprocess,
    capsys: pytest.CaptureFixture[str],
    job: dict[str, Any],
) -> None:
    monkeypatch.setenv("REVIEWER_MODEL_CMD", "fake-reviewer")
    recording_subprocess.when("pr diff", stdout="diff --git a/x b/x", stderr="gh: using cached token")
    recording_subprocess.when("fake-reviewer", stdout=MODEL_ANSWER, stderr="model: 12 tok/s")
    feed_stdin(monkeypatch, job)

    pass_through_review.main()

    captured = capsys.readouterr()
    assert "gh: using cached token" in captured.err
    assert "model: 12 tok/s" in captured.err
    json.loads(captured.out)


def test_fetch_pr_diff_calls_gh_pr_diff_with_pr_number_and_repo(
    recording_subprocess: RecordingSubprocess,
    job: dict[str, Any],
) -> None:
    recording_subprocess.when("pr diff", stdout="diff --git a/x b/x")

    diff = pass_through_review.fetch_pr_diff(job)

    assert diff == "diff --git a/x b/x"
    call = recording_subprocess.calls[-1]
    assert call.argv == ["/usr/bin/gh", "pr", "diff", "7", "--repo", "acme/payments"]
    assert call.kwargs["capture_output"] is True
    assert call.kwargs["text"] is True
    assert call.kwargs["timeout"] == _common.GH_TIMEOUT_SECONDS


def test_pass_through_never_clones_or_pushes(
    monkeypatch: pytest.MonkeyPatch,
    recording_subprocess: RecordingSubprocess,
    capsys: pytest.CaptureFixture[str],
    job: dict[str, Any],
) -> None:
    monkeypatch.setenv("REVIEWER_MODEL_CMD", "fake-reviewer")
    recording_subprocess.when("pr diff", stdout="diff --git a/x b/x")
    recording_subprocess.when("fake-reviewer", stdout=MODEL_ANSWER)
    feed_stdin(monkeypatch, job)

    pass_through_review.main()

    emitted(capsys)
    assert not recording_subprocess.saw("clone")
    assert not recording_subprocess.saw("push")


def test_build_prompt_includes_diff_and_pr_context(job: dict[str, Any]) -> None:
    prompt = pass_through_review.build_prompt(job, "diff --git a/charge.py b/charge.py")

    assert "diff --git a/charge.py b/charge.py" in prompt
    assert "acme/payments" in prompt
    assert "7" in prompt


def test_main_degrades_to_error_report_when_reviewer_model_cmd_unset(
    monkeypatch: pytest.MonkeyPatch,
    recording_subprocess: RecordingSubprocess,
    capsys: pytest.CaptureFixture[str],
    job: dict[str, Any],
) -> None:
    monkeypatch.delenv("REVIEWER_MODEL_CMD", raising=False)
    recording_subprocess.when("pr diff", stdout="diff --git a/x b/x")
    feed_stdin(monkeypatch, job)

    pass_through_review.main()

    report = emitted(capsys)
    assert report["verdict"] == "error"
    assert "REVIEWER_MODEL_CMD" in report["summary"]
    assert report["findings"] == []


def test_main_degrades_to_error_report_when_gh_pr_diff_fails(
    monkeypatch: pytest.MonkeyPatch,
    recording_subprocess: RecordingSubprocess,
    capsys: pytest.CaptureFixture[str],
    job: dict[str, Any],
) -> None:
    monkeypatch.setenv("REVIEWER_MODEL_CMD", "fake-reviewer")
    recording_subprocess.when("pr diff", raises=subprocess.CalledProcessError(1, "gh", stderr="no such PR"))
    feed_stdin(monkeypatch, job)

    pass_through_review.main()

    assert emitted(capsys)["verdict"] == "error"


def test_main_degrades_to_error_report_when_the_model_answers_with_prose(
    monkeypatch: pytest.MonkeyPatch,
    recording_subprocess: RecordingSubprocess,
    capsys: pytest.CaptureFixture[str],
    job: dict[str, Any],
) -> None:
    monkeypatch.setenv("REVIEWER_MODEL_CMD", "fake-reviewer")
    recording_subprocess.when("pr diff", stdout="diff --git a/x b/x")
    recording_subprocess.when("fake-reviewer", stdout="Looks good to me!")
    feed_stdin(monkeypatch, job)

    pass_through_review.main()

    assert emitted(capsys)["verdict"] == "error"


def test_main_ignores_unknown_job_fields(
    monkeypatch: pytest.MonkeyPatch,
    recording_subprocess: RecordingSubprocess,
    capsys: pytest.CaptureFixture[str],
    job: dict[str, Any],
) -> None:
    monkeypatch.setenv("REVIEWER_MODEL_CMD", "fake-reviewer")
    recording_subprocess.when("pr diff", stdout="diff --git a/x b/x")
    recording_subprocess.when("fake-reviewer", stdout=MODEL_ANSWER)
    # A field A2 has not added yet, alongside every field the hub sends today.
    job["requested_strategies"] = ["security", "performance"]
    feed_stdin(monkeypatch, job)

    pass_through_review.main()

    assert emitted(capsys)["verdict"] == "findings"

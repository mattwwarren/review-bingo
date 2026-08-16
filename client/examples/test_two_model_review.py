"""Tests for `two_model_review.py`, the cheap-fix / expensive-review reference loop.

This script is the one with teeth: it clones, it checks out, it applies a patch
a model wrote. So the tests spend most of their effort on the boundaries of that
blast radius — the fix step is off unless the operator turns it on, everything
happens inside a temp directory that is gone when the script returns, and
nothing anywhere pushes. The rest is the same contract the pass-through script
has: pure stdout, exit 0, an error report for every failure.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import two_model_review
from conftest import RecordingSubprocess, emitted, feed_stdin

PATCH_TEXT = "diff --git a/charge.py b/charge.py\n@@\n-bad\n+good\n"

REVIEW_ANSWER = json.dumps(
    {
        "verdict": "findings",
        "summary": "Fixed the double charge; one thing left.",
        "findings": [{"file": "charge.py", "line": 42, "title": "Retry still unbounded"}],
    }
)


def happy_path(recorder: RecordingSubprocess) -> None:
    """Script every subprocess a full fix-then-review round makes."""
    recorder.when("repo clone", stdout="")
    recorder.when("checkout", stdout="")
    recorder.when("pr diff", stdout="diff --git a/charge.py b/charge.py")
    recorder.when("fake-fixer", stdout=PATCH_TEXT)
    recorder.when("apply", stdout="")
    recorder.when("fake-reviewer", stdout=REVIEW_ANSWER)


def test_dry_run_default_skips_fix_step_and_never_calls_fix_model_cmd_or_git_apply(
    monkeypatch: pytest.MonkeyPatch,
    recording_subprocess: RecordingSubprocess,
    capsys: pytest.CaptureFixture[str],
    job: dict[str, Any],
) -> None:
    monkeypatch.delenv("REVIEW_CMD_DRY_RUN", raising=False)
    monkeypatch.delenv("FIX_MODEL_CMD", raising=False)
    monkeypatch.setenv("REVIEW_MODEL_CMD", "fake-reviewer")
    happy_path(recording_subprocess)
    feed_stdin(monkeypatch, job)

    two_model_review.main()

    report = emitted(capsys)
    assert report["verdict"] == "findings"
    assert "patch" not in report
    assert not recording_subprocess.saw("fake-fixer")
    assert not recording_subprocess.saw_argument("apply")


def test_dry_run_zero_enables_fix_step_and_applies_patch_inside_the_clone(
    monkeypatch: pytest.MonkeyPatch,
    recording_subprocess: RecordingSubprocess,
    capsys: pytest.CaptureFixture[str],
    job: dict[str, Any],
) -> None:
    monkeypatch.setenv("REVIEW_CMD_DRY_RUN", "0")
    monkeypatch.setenv("FIX_MODEL_CMD", "fake-fixer")
    monkeypatch.setenv("REVIEW_MODEL_CMD", "fake-reviewer")
    happy_path(recording_subprocess)
    feed_stdin(monkeypatch, job)

    two_model_review.main()

    report = emitted(capsys)
    assert report["patch"] == PATCH_TEXT

    clone_dir = recording_subprocess.matching("repo clone")[0].argv[4]
    apply_call = next(call for call in recording_subprocess.calls if "apply" in call.argv)
    assert apply_call.argv[:4] == ["/usr/bin/git", "-C", clone_dir, "apply"]
    # The patch file lives in the same throwaway directory as the clone, so it
    # goes away with it rather than being left in /tmp for someone to find.
    assert Path(apply_call.argv[4]).parent == Path(clone_dir).parent


def test_clone_uses_gh_repo_clone_then_checks_out_head_sha(
    monkeypatch: pytest.MonkeyPatch,
    recording_subprocess: RecordingSubprocess,
    capsys: pytest.CaptureFixture[str],
    job: dict[str, Any],
) -> None:
    monkeypatch.setenv("REVIEW_MODEL_CMD", "fake-reviewer")
    happy_path(recording_subprocess)
    feed_stdin(monkeypatch, job)

    two_model_review.main()

    emitted(capsys)
    clone = recording_subprocess.matching("repo clone")[0]
    clone_dir = clone.argv[4]
    assert clone.argv == ["/usr/bin/gh", "repo", "clone", "acme/payments", clone_dir, "--", "--quiet"]
    checkout = recording_subprocess.matching("checkout")[0]
    assert checkout.argv == ["/usr/bin/git", "-C", clone_dir, "checkout", job["head_sha"]]
    # The checkout targets the clone that was just made, so it has to happen after it.
    assert recording_subprocess.calls.index(clone) < recording_subprocess.calls.index(checkout)


@pytest.mark.parametrize("dry_run", ["0", "1"])
def test_never_pushes_in_either_mode(
    monkeypatch: pytest.MonkeyPatch,
    recording_subprocess: RecordingSubprocess,
    capsys: pytest.CaptureFixture[str],
    job: dict[str, Any],
    dry_run: str,
) -> None:
    monkeypatch.setenv("REVIEW_CMD_DRY_RUN", dry_run)
    monkeypatch.setenv("FIX_MODEL_CMD", "fake-fixer")
    monkeypatch.setenv("REVIEW_MODEL_CMD", "fake-reviewer")
    happy_path(recording_subprocess)
    feed_stdin(monkeypatch, job)

    two_model_review.main()

    emitted(capsys)
    assert not recording_subprocess.saw("push")
    assert not recording_subprocess.saw("pr create")


def test_temp_clone_directory_is_removed_after_main_returns(
    monkeypatch: pytest.MonkeyPatch,
    recording_subprocess: RecordingSubprocess,
    capsys: pytest.CaptureFixture[str],
    job: dict[str, Any],
) -> None:
    monkeypatch.setenv("REVIEW_MODEL_CMD", "fake-reviewer")
    happy_path(recording_subprocess)
    feed_stdin(monkeypatch, job)

    two_model_review.main()

    emitted(capsys)
    clone_dir = Path(recording_subprocess.matching("repo clone")[0].argv[4])
    assert not clone_dir.exists()
    assert not clone_dir.parent.exists()


def test_temp_clone_directory_is_removed_even_when_the_round_fails(
    monkeypatch: pytest.MonkeyPatch,
    recording_subprocess: RecordingSubprocess,
    capsys: pytest.CaptureFixture[str],
    job: dict[str, Any],
) -> None:
    monkeypatch.setenv("REVIEW_MODEL_CMD", "fake-reviewer")
    happy_path(recording_subprocess)
    recording_subprocess.when("fake-reviewer", raises=subprocess.TimeoutExpired("fake-reviewer", 1.0))
    feed_stdin(monkeypatch, job)

    two_model_review.main()

    assert emitted(capsys)["verdict"] == "error"
    clone_dir = Path(recording_subprocess.matching("repo clone")[0].argv[4])
    assert not clone_dir.parent.exists()


def test_main_degrades_to_error_report_when_fix_model_cmd_unset_and_dry_run_disabled(
    monkeypatch: pytest.MonkeyPatch,
    recording_subprocess: RecordingSubprocess,
    capsys: pytest.CaptureFixture[str],
    job: dict[str, Any],
) -> None:
    monkeypatch.setenv("REVIEW_CMD_DRY_RUN", "0")
    monkeypatch.delenv("FIX_MODEL_CMD", raising=False)
    monkeypatch.setenv("REVIEW_MODEL_CMD", "fake-reviewer")
    happy_path(recording_subprocess)
    feed_stdin(monkeypatch, job)

    two_model_review.main()

    report = emitted(capsys)
    assert report["verdict"] == "error"
    assert "FIX_MODEL_CMD" in report["summary"]


def test_main_degrades_to_error_report_when_review_model_cmd_unset(
    monkeypatch: pytest.MonkeyPatch,
    recording_subprocess: RecordingSubprocess,
    capsys: pytest.CaptureFixture[str],
    job: dict[str, Any],
) -> None:
    monkeypatch.delenv("REVIEW_MODEL_CMD", raising=False)
    happy_path(recording_subprocess)
    feed_stdin(monkeypatch, job)

    two_model_review.main()

    report = emitted(capsys)
    assert report["verdict"] == "error"
    assert "REVIEW_MODEL_CMD" in report["summary"]


def test_main_degrades_to_error_report_when_patch_fails_to_apply(
    monkeypatch: pytest.MonkeyPatch,
    recording_subprocess: RecordingSubprocess,
    capsys: pytest.CaptureFixture[str],
    job: dict[str, Any],
) -> None:
    monkeypatch.setenv("REVIEW_CMD_DRY_RUN", "0")
    monkeypatch.setenv("FIX_MODEL_CMD", "fake-fixer")
    monkeypatch.setenv("REVIEW_MODEL_CMD", "fake-reviewer")
    happy_path(recording_subprocess)
    recording_subprocess.when("apply", returncode=1, stderr="patch does not apply")
    feed_stdin(monkeypatch, job)

    two_model_review.main()

    report = emitted(capsys)
    assert report["verdict"] == "error"
    assert "patch" in report["summary"].lower()


def test_main_emits_only_json_to_stdout_with_git_and_gh_noise_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
    recording_subprocess: RecordingSubprocess,
    capsys: pytest.CaptureFixture[str],
    job: dict[str, Any],
) -> None:
    monkeypatch.setenv("REVIEW_CMD_DRY_RUN", "0")
    monkeypatch.setenv("FIX_MODEL_CMD", "fake-fixer")
    monkeypatch.setenv("REVIEW_MODEL_CMD", "fake-reviewer")
    recording_subprocess.when("repo clone", stderr="Cloning into 'repo'...")
    recording_subprocess.when("checkout", stderr="HEAD is now at 0123456")
    recording_subprocess.when("pr diff", stdout="diff --git a/charge.py b/charge.py", stderr="gh: cached token")
    recording_subprocess.when("fake-fixer", stdout=PATCH_TEXT, stderr="fixer: 40 tok/s")
    recording_subprocess.when("apply", stdout="")
    recording_subprocess.when("fake-reviewer", stdout=REVIEW_ANSWER, stderr="reviewer: 8 tok/s")
    feed_stdin(monkeypatch, job)

    two_model_review.main()

    captured = capsys.readouterr()
    assert len(captured.out.splitlines()) == 1
    json.loads(captured.out)
    assert "Cloning into 'repo'..." in captured.err
    assert "gh: cached token" in captured.err
    assert "reviewer: 8 tok/s" in captured.err


def test_main_ignores_unknown_job_fields(
    monkeypatch: pytest.MonkeyPatch,
    recording_subprocess: RecordingSubprocess,
    capsys: pytest.CaptureFixture[str],
    job: dict[str, Any],
) -> None:
    monkeypatch.setenv("REVIEW_MODEL_CMD", "fake-reviewer")
    happy_path(recording_subprocess)
    job["requested_strategies"] = ["security"]
    feed_stdin(monkeypatch, job)

    two_model_review.main()

    assert emitted(capsys)["verdict"] == "findings"

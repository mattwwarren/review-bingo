"""Shared test doubles for the reference `REVIEW_CMD` scripts in this directory.

Every script here reaches the outside world through exactly one door —
`subprocess.run` — so a single recorder placed over that door covers `gh`,
`git`, and whatever model command the operator brought, all at once. The shape
follows `RecordingHub` in `client/test_bingo_client.py`: record what was asked,
hand back a scripted answer, and let the test assert against the record
afterwards. This is the sibling of that pattern for subprocesses rather than
HTTP.

`shutil.which` is stubbed for every test in this directory (autouse) so `gh` and
`git` resolve to stable absolute paths that assertions can name. A test that
wants the "not installed" branch re-patches it itself.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any

import pytest

# Absolute paths the stubbed `shutil.which` hands back. Absolute because the
# scripts resolve executables before invoking them (ruff S607), so every
# recorded argv starts with one of these.
FAKE_EXECUTABLES = {"gh": "/usr/bin/gh", "git": "/usr/bin/git"}


@dataclass(frozen=True)
class RecordedCall:
    """One intercepted `subprocess.run`, in both forms it gets called in.

    List form is `gh`/`git` (argv, no shell); string form is the operator's
    model command (`shell=True`). `text` flattens both so a test can ask "did
    anything anywhere push?" without caring which form the call took.
    """

    cmd: str | list[str]
    kwargs: dict[str, Any]

    @property
    def argv(self) -> list[str]:
        return list(self.cmd) if isinstance(self.cmd, list) else [self.cmd]

    @property
    def text(self) -> str:
        return self.cmd if isinstance(self.cmd, str) else " ".join(self.cmd)


@dataclass
class _Scripted:
    """What the recorder should do when a rule matches."""

    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    raises: Exception | None = None


@dataclass
class RecordingSubprocess:
    """Stand-in for `subprocess.run` that records calls and returns scripted answers.

    Rules are matched against the flattened command text, most recently
    registered first, and an unmatched call gets a silent success — the common
    case in these tests is "this call is not what I am asserting about, just let
    it through". Last-registered-wins is what lets a test lay down a whole happy
    path with one helper and then override the single step it is about.

    `check=True` is honoured the way the real thing honours it: a nonzero
    scripted returncode raises `CalledProcessError`, so the scripts' error paths
    are exercised through their real trigger rather than a bespoke one.
    """

    calls: list[RecordedCall] = field(default_factory=list)
    _rules: list[tuple[str, _Scripted]] = field(default_factory=list)

    def when(
        self,
        fragment: str,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        raises: Exception | None = None,
    ) -> None:
        """Script the answer for any call whose flattened text contains `fragment`."""
        self._rules.append((fragment, _Scripted(stdout=stdout, stderr=stderr, returncode=returncode, raises=raises)))

    def matching(self, fragment: str) -> list[RecordedCall]:
        return [call for call in self.calls if fragment in call.text]

    def saw(self, fragment: str) -> bool:
        return bool(self.matching(fragment))

    def saw_argument(self, token: str) -> bool:
        """True when `token` appears as a whole argv element in any recorded call."""
        return any(token in call.argv for call in self.calls)

    def _scripted_for(self, call: RecordedCall) -> _Scripted:
        for fragment, scripted in reversed(self._rules):
            if fragment in call.text:
                return scripted
        return _Scripted()

    def __call__(self, cmd: str | list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        call = RecordedCall(cmd=cmd, kwargs=dict(kwargs))
        self.calls.append(call)
        scripted = self._scripted_for(call)
        if scripted.raises is not None:
            raise scripted.raises
        if scripted.returncode != 0 and kwargs.get("check"):
            raise subprocess.CalledProcessError(
                scripted.returncode,
                cmd,
                output=scripted.stdout,
                stderr=scripted.stderr,
            )
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=scripted.returncode,
            stdout=scripted.stdout,
            stderr=scripted.stderr,
        )


@pytest.fixture
def recording_subprocess(monkeypatch: pytest.MonkeyPatch) -> RecordingSubprocess:
    """Intercept every `subprocess.run` the scripts under test make."""
    recorder = RecordingSubprocess()
    monkeypatch.setattr(subprocess, "run", recorder)
    return recorder


@pytest.fixture(autouse=True)
def stub_which(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve `gh`/`git` to stable absolute paths; everything else is "not installed"."""

    def fake_which(name: str) -> str | None:
        return FAKE_EXECUTABLES.get(name)

    monkeypatch.setattr(shutil, "which", fake_which)


@pytest.fixture(autouse=True)
def clean_review_cmd_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from an unconfigured environment.

    The scripts' whole contract is "read these four env vars", so a var leaking
    in from the developer's own shell would quietly change what a test proves.
    """
    for name in ("REVIEWER_MODEL_CMD", "FIX_MODEL_CMD", "REVIEW_MODEL_CMD", "REVIEW_CMD_DRY_RUN"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def job() -> dict[str, Any]:
    """The full `ReviewJobRead` payload the hub hands `REVIEW_CMD` on stdin.

    Every field of `hub/review_bingo_hub/models/review_job.py:ReviewJobRead`,
    not the three-field shorthand the scripts actually read — the point of
    several tests here is that the extra fields are ignored rather than
    validated, so a later hub-side addition cannot break a contributor's script.
    """
    return {
        "repo_full_name": "acme/payments",
        "pr_number": 7,
        "head_sha": "0123456789abcdef0123456789abcdef01234567",
        "pr_title": "Charge the card twice, just to be sure",
        "event_action": "synchronize",
        "id": "6f1b7a1e-9b1a-4a3e-8f0e-1a2b3c4d5e6f",
        "state": "leased",
        "min_tier": "standard",
        "leased_by": "11111111-2222-3333-4444-555555555555",
        "lease_expires_at": "2026-08-16T12:00:00+00:00",
        "attempts": 1,
        "verdict": None,
        "summary": None,
        "findings": [],
        "relay_error": None,
        "created_at": "2026-08-16T11:00:00+00:00",
        "updated_at": "2026-08-16T11:30:00+00:00",
    }

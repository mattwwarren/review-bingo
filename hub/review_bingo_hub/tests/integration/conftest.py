"""Shared seams for integration tests that exercise GitHub-derived identity.

`FakeGithubIdentityService` and its three companions started life inside
`test_client_enrolment.py`. They live here now because a second consumer
arrived (`test_repo_scoped_access.py`): the same fake has to stand in for
GitHub whenever a test needs a client enrolled under a *specific* identity
and access set, and copying it would let the two copies drift.

These are plain module-level helpers, not pytest fixtures — callers import
and call them directly. They sit in `conftest.py` only because that is the
canonical home for test-support code shared across a directory.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from review_bingo_hub.core.config import settings
from review_bingo_hub.main import app
from review_bingo_hub.services.github_identity_service import (
    GithubRepoAccess,
    GithubUserIdentity,
    get_github_identity_service,
)

GITHUB_TOKEN = "gho_16C7e42F292c6912E7710c838347Ae178B4a"


@dataclass
class FakeGithubIdentityService:
    """Stands in for GitHub. Raises if called when it should not be."""

    identity: GithubUserIdentity | None = None
    repo_access: list[GithubRepoAccess] | None = None
    error: Exception | None = None
    forbidden: bool = False
    calls: int = 0

    async def get_identity(self, token: str) -> GithubUserIdentity:
        self._record(token)
        if self.error is not None:
            raise self.error
        assert self.identity is not None
        return self.identity

    async def get_repo_access(self, token: str) -> list[GithubRepoAccess]:
        self._record(token)
        if self.error is not None:
            raise self.error
        return list(self.repo_access or [])

    def _record(self, token: str) -> None:
        if self.forbidden:
            error_msg = "github_identity_service must not be consulted in dev mode"
            raise AssertionError(error_msg)
        assert token == GITHUB_TOKEN
        self.calls += 1


def use_github_mode(monkeypatch: pytest.MonkeyPatch, fake: FakeGithubIdentityService) -> None:
    monkeypatch.setattr(settings, "client_enrolment_mode", "github")
    app.dependency_overrides[get_github_identity_service] = lambda: fake


def enrolment_headers(credential: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {credential}"}


def marge(login: str = "marge-bouvier", user_id: int = 20482231) -> GithubUserIdentity:
    return GithubUserIdentity(github_user_id=user_id, github_login=login)

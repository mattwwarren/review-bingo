"""Tests for the GitHub identity seam: what a user token tells the hub.

Two things are pinned here and they are not equally obvious:

1. The permission collapse. GitHub reports five booleans; the schema stores
   one level. The mapping decides who can later lower a repo's model floor,
   so each bucket gets its own test rather than one parametrised sweep.
2. The token never reaching a log record. Asserted as a substring search over
   every captured record at DEBUG, because a "safe" prefix/length is still a
   leak of the only secret in this code path.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from review_bingo_hub.core.config import settings
from review_bingo_hub.models.github_identity import PermissionLevel
from review_bingo_hub.services import github_identity_service as svc

FIXTURES = Path(__file__).parents[1] / "fixtures" / "github"

# Matches user_installations.json's first (review-bingo) installation.
TEST_APP_ID = "424242"
FAKE_TOKEN = "gho_16C7e42F292c6912E7710c838347Ae178B4a"


def load_fixture(name: str) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return payload


def patch_transport(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]) -> None:
    """Route the service's http_client() through a MockTransport."""

    @contextlib.asynccontextmanager
    async def fake_http_client(timeout: float = 30.0) -> AsyncGenerator[httpx.AsyncClient]:  # noqa: ARG001
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            yield client

    monkeypatch.setattr(svc, "http_client", fake_http_client)


def installations_response(*installations: dict[str, Any]) -> dict[str, Any]:
    return {"total_count": len(installations), "installations": list(installations)}


def repositories_response(*repositories: dict[str, Any]) -> dict[str, Any]:
    return {"total_count": len(repositories), "repositories": list(repositories)}


async def test_get_identity_returns_user_id_and_login(monkeypatch: pytest.MonkeyPatch) -> None:
    body = load_fixture("user_identity.json")
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["authorization"] = request.headers["authorization"]
        return httpx.Response(200, json=body)

    patch_transport(monkeypatch, handler)

    identity = await svc.LiveGithubIdentityService().get_identity(FAKE_TOKEN)

    assert seen["path"] == "/user"
    assert seen["authorization"] == f"Bearer {FAKE_TOKEN}"
    assert identity.github_user_id == body["id"]
    assert identity.github_login == body["login"]


async def test_get_identity_raises_on_401(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_transport(monkeypatch, lambda _request: httpx.Response(401, json={"message": "Bad credentials"}))

    with pytest.raises(svc.GithubIdentityError) as exc_info:
        await svc.LiveGithubIdentityService().get_identity(FAKE_TOKEN)

    # An explicit rejection is not an outage: it must not be reported as one.
    assert not isinstance(exc_info.value, svc.GithubUnavailableError)


async def test_get_identity_raises_unavailable_on_500(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_transport(monkeypatch, lambda _request: httpx.Response(500, json={"message": "boom"}))

    with pytest.raises(svc.GithubUnavailableError):
        await svc.LiveGithubIdentityService().get_identity(FAKE_TOKEN)


async def test_get_identity_logs_no_token_material(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    patch_transport(monkeypatch, lambda _request: httpx.Response(200, json=load_fixture("user_identity.json")))

    with caplog.at_level(logging.DEBUG):
        await svc.LiveGithubIdentityService().get_identity(FAKE_TOKEN)

    for record in caplog.records:
        rendered = record.getMessage() + json.dumps(record.__dict__, default=str)
        assert FAKE_TOKEN not in rendered
        # Not even a "harmless" fragment: a prefix narrows the search space.
        assert FAKE_TOKEN[:8] not in rendered
        assert FAKE_TOKEN[-8:] not in rendered


def _repo(full_name: str, **permissions: bool) -> dict[str, Any]:
    flags = {"admin": False, "maintain": False, "push": False, "triage": False, "pull": False}
    flags.update(permissions)
    return {"full_name": full_name, "permissions": flags}


def repo_access_handler(
    installations: dict[str, Any],
    repositories_by_installation: dict[int, dict[str, Any]],
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/user/installations":
            return httpx.Response(200, json=installations)
        if request.url.path.endswith("/repositories"):
            installation_id = int(request.url.path.split("/")[-2])
            return httpx.Response(200, json=repositories_by_installation[installation_id])
        pytest.fail(f"unexpected path {request.url.path}")

    return handler


async def test_get_repo_access_maps_admin_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "github_app_id", TEST_APP_ID)
    fixture = load_fixture("installation_repositories.json")
    installations = load_fixture("user_installations.json")
    patch_transport(monkeypatch, repo_access_handler(installations, {51234567: fixture}))

    access = await svc.LiveGithubIdentityService().get_repo_access(FAKE_TOKEN)

    by_repo = {a.repo_full_name: a.permission for a in access}
    assert by_repo["acme/payments"] == PermissionLevel.ADMIN


async def test_get_repo_access_maps_maintain_and_push_to_write(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "github_app_id", TEST_APP_ID)
    installations = load_fixture("user_installations.json")
    repos = repositories_response(
        _repo("acme/ledger", maintain=True, push=True, triage=True, pull=True),
        _repo("acme/maintain-only", maintain=True, triage=True, pull=True),
        _repo("acme/push-only", push=True, pull=True),
    )
    patch_transport(monkeypatch, repo_access_handler(installations, {51234567: repos}))

    access = await svc.LiveGithubIdentityService().get_repo_access(FAKE_TOKEN)

    by_repo = {a.repo_full_name: a.permission for a in access}
    assert by_repo["acme/ledger"] == PermissionLevel.WRITE
    assert by_repo["acme/maintain-only"] == PermissionLevel.WRITE
    assert by_repo["acme/push-only"] == PermissionLevel.WRITE


async def test_get_repo_access_maps_pull_and_triage_only_to_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "github_app_id", TEST_APP_ID)
    installations = load_fixture("user_installations.json")
    repos = repositories_response(
        _repo("acme/docs", triage=True, pull=True),
        _repo("acme/wiki", pull=True),
    )
    patch_transport(monkeypatch, repo_access_handler(installations, {51234567: repos}))

    access = await svc.LiveGithubIdentityService().get_repo_access(FAKE_TOKEN)

    by_repo = {a.repo_full_name: a.permission for a in access}
    assert by_repo["acme/docs"] == PermissionLevel.READ
    assert by_repo["acme/wiki"] == PermissionLevel.READ


async def test_get_repo_access_filters_installations_by_app_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "github_app_id", TEST_APP_ID)
    installations = load_fixture("user_installations.json")
    asked: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/user/installations":
            return httpx.Response(200, json=installations)
        installation_id = int(request.url.path.split("/")[-2])
        asked.append(installation_id)
        return httpx.Response(200, json=repositories_response(_repo(f"acme/repo-{installation_id}", pull=True)))

    patch_transport(monkeypatch, handler)

    access = await svc.LiveGithubIdentityService().get_repo_access(FAKE_TOKEN)

    # 60000001 belongs to a different App; its repos are none of our business.
    assert asked == [51234567]
    assert [a.repo_full_name for a in access] == ["acme/repo-51234567"]


async def test_get_repo_access_is_empty_when_no_installation_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "github_app_id", "999999999")
    installations = load_fixture("user_installations.json")
    patch_transport(monkeypatch, repo_access_handler(installations, {}))

    access = await svc.LiveGithubIdentityService().get_repo_access(FAKE_TOKEN)

    # No fallback to a broader repo list: no installation means no access.
    assert access == []


async def test_get_repo_access_unions_multiple_matching_installations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "github_app_id", TEST_APP_ID)
    installations = installations_response(
        {"id": 1, "app_id": int(TEST_APP_ID)},
        {"id": 2, "app_id": int(TEST_APP_ID)},
    )
    repositories = {
        1: repositories_response(_repo("acme/payments", admin=True, maintain=True, push=True, triage=True, pull=True)),
        2: repositories_response(_repo("beta/tools", push=True, pull=True)),
    }
    patch_transport(monkeypatch, repo_access_handler(installations, repositories))

    access = await svc.LiveGithubIdentityService().get_repo_access(FAKE_TOKEN)

    by_repo = {a.repo_full_name: a.permission for a in access}
    assert by_repo == {"acme/payments": PermissionLevel.ADMIN, "beta/tools": PermissionLevel.WRITE}


async def test_get_repo_access_keeps_the_highest_permission_for_a_duplicated_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "github_app_id", TEST_APP_ID)
    installations = installations_response(
        {"id": 1, "app_id": int(TEST_APP_ID)},
        {"id": 2, "app_id": int(TEST_APP_ID)},
    )
    repositories = {
        1: repositories_response(_repo("acme/payments", pull=True)),
        2: repositories_response(_repo("acme/payments", admin=True, push=True, pull=True)),
    }
    patch_transport(monkeypatch, repo_access_handler(installations, repositories))

    access = await svc.LiveGithubIdentityService().get_repo_access(FAKE_TOKEN)

    assert [(a.repo_full_name, a.permission) for a in access] == [("acme/payments", PermissionLevel.ADMIN)]


async def test_get_repo_access_paginates_installations_and_repos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "github_app_id", TEST_APP_ID)
    monkeypatch.setattr(svc, "PAGE_SIZE", 2)
    pages_seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page")
        pages_seen.append((request.url.path, page))
        if request.url.path == "/user/installations":
            if page == "1":
                return httpx.Response(
                    200,
                    json=installations_response(
                        {"id": 1, "app_id": int(TEST_APP_ID)},
                        {"id": 2, "app_id": int(TEST_APP_ID)},
                    ),
                )
            return httpx.Response(200, json=installations_response())
        installation_id = int(request.url.path.split("/")[-2])
        if page == "1":
            return httpx.Response(
                200,
                json=repositories_response(
                    _repo(f"acme/repo-{installation_id}-a", pull=True),
                    _repo(f"acme/repo-{installation_id}-b", pull=True),
                ),
            )
        return httpx.Response(200, json=repositories_response(_repo(f"acme/repo-{installation_id}-c", pull=True)))

    patch_transport(monkeypatch, handler)

    access = await svc.LiveGithubIdentityService().get_repo_access(FAKE_TOKEN)

    assert sorted(a.repo_full_name for a in access) == [
        "acme/repo-1-a",
        "acme/repo-1-b",
        "acme/repo-1-c",
        "acme/repo-2-a",
        "acme/repo-2-b",
        "acme/repo-2-c",
    ]
    assert ("/user/installations", "2") in pages_seen


async def test_get_repo_access_raises_on_github_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "github_app_id", TEST_APP_ID)

    def handler(_request: httpx.Request) -> httpx.Response:
        error_msg = "name resolution failed"
        raise httpx.ConnectError(error_msg)

    patch_transport(monkeypatch, handler)

    with pytest.raises(svc.GithubUnavailableError):
        await svc.LiveGithubIdentityService().get_repo_access(FAKE_TOKEN)


async def test_get_repo_access_raises_on_credential_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "github_app_id", TEST_APP_ID)
    patch_transport(monkeypatch, lambda _request: httpx.Response(403, json={"message": "Forbidden"}))

    with pytest.raises(svc.GithubIdentityError) as exc_info:
        await svc.LiveGithubIdentityService().get_repo_access(FAKE_TOKEN)

    assert not isinstance(exc_info.value, svc.GithubUnavailableError)


# The three below cover the strict-parsing branches. They exist because the
# GitHub fixtures in this suite are hand-built (see tests/fixtures/github/
# README.md) and therefore cannot falsify our model of GitHub's shape. Strict
# parsing is the instrument that will: when the real API disagrees with us,
# these are the paths that fire instead of a silent empty result.


async def test_get_identity_raises_when_response_lacks_id_and_login(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_transport(monkeypatch, lambda _request: httpx.Response(200, json={"message": "Not Found"}))

    with pytest.raises(svc.GithubIdentityError):
        await svc.LiveGithubIdentityService().get_identity(FAKE_TOKEN)


async def test_get_identity_raises_unavailable_on_a_non_json_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """A proxy's HTML error page is an outage, not a rejected credential."""
    patch_transport(monkeypatch, lambda _request: httpx.Response(200, text="<html>502 Bad Gateway</html>"))

    with pytest.raises(svc.GithubUnavailableError):
        await svc.LiveGithubIdentityService().get_identity(FAKE_TOKEN)


async def test_get_repo_access_raises_when_the_envelope_shape_is_unexpected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "github_app_id", TEST_APP_ID)
    patch_transport(monkeypatch, lambda _request: httpx.Response(200, json={"total_count": 1}))

    with pytest.raises(svc.GithubIdentityError):
        await svc.LiveGithubIdentityService().get_repo_access(FAKE_TOKEN)


def test_get_github_identity_service_returns_the_live_implementation() -> None:
    assert isinstance(svc.get_github_identity_service(), svc.LiveGithubIdentityService)

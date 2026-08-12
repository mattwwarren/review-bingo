"""Integration tests for repo-admin authorization on `/policies`.

The model floor is the one review-config knob the hub owns, and it is a
*policy* knob: whoever can set it decides which models are allowed to touch a
repo's PRs. Leaving that open to anyone who can enrol would let a stranger
lower a bank's floor to `experimental`. So writes require GitHub-recorded
`admin` on the repo, and reads are scoped to what the caller can already see.

Three properties are asserted directly rather than inferred:

* **`admin`, not "can push".** GitHub's `maintain` role collapses to
  `PermissionLevel.WRITE`, never `ADMIN` (see `collapse_permissions`), so a
  maintainer is refused here exactly like any other non-admin collaborator.
* **A repo you cannot see answers like a repo with no policy.** `GET
  /policies/{owner}/{repo}` returns the same 404 either way — a 403 would turn
  the endpoint into an oracle for which repos are on the hub at all.
* **The dev-mode carve-out is the *same* named mode, not a second bypass.**
  Under `CLIENT_ENROLMENT_MODE=dev` the shared enrolment secret substitutes for
  the admin check, through the very same comparison `POST /clients` uses, and
  every use of it still lands in the log stream.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from http import HTTPStatus

import pytest
from httpx import AsyncClient

from review_bingo_hub.core.config import settings
from review_bingo_hub.models.github_identity import PermissionLevel
from review_bingo_hub.services.github_identity_service import GithubRepoAccess, collapse_permissions
from review_bingo_hub.services.identity_service import CALLER_IDENTITY_UNAVAILABLE_DEV_MODE
from review_bingo_hub.tests.integration.conftest import (
    GITHUB_TOKEN,
    Enrolee,
    FakeGithubIdentityService,
    access,
    dump,
    enrolment_headers,
    marge,
    records_named,
    start_dashboard_session,
    use_github_mode,
)

# The suite's autouse `isolate_github_app_config` pins dev mode and this secret,
# so it is also the credential the shared `client` fixture already sends.
DEV_SECRET = "test-fixture-placeholder"

# One repo the caller administers, one it merely appears in, one it cannot see.
ADMIN_REPO = "acme/alpha"
OTHER_REPO = "acme/bravo"
HIDDEN_REPO = "acme/charlie"

REGISTRATION_PAYLOAD = {
    "name": "marge-mac-mini",
    "model_name": "qwen2.5-coder-32b",
    "provider": "ollama",
    "tier": "standard",
}


@dataclass(frozen=True)
class Enrolled:
    """A registered client, as the tests below need to talk about it.

    `client_id` and `identity_id` are here because the audit-trail tests assert
    the log record names the same row the enrolment created — a log line that
    says "someone was denied" without saying who is not an audit trail.
    """

    client_id: str
    identity_id: str | None
    headers: dict[str, str]


async def enrol_github(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    repo_access: list[GithubRepoAccess],
    login: str = "marge-bouvier",
) -> Enrolled:
    """Register a github-mode client under one identity and access set.

    Drives the real `POST /clients` flow with the fake identity service rather
    than inserting `IdentityRepoAccess` rows directly: policy authorization
    reads whatever enrolment actually wrote, so seeding it by hand would let
    the two halves disagree without any test noticing.
    """
    fake = FakeGithubIdentityService(identity=marge(login=login), repo_access=list(repo_access))
    use_github_mode(monkeypatch, fake)

    response = await client.post(
        "/clients",
        json={**REGISTRATION_PAYLOAD, "name": login},
        headers=enrolment_headers(GITHUB_TOKEN),
    )
    assert response.status_code == HTTPStatus.CREATED
    body = response.json()
    return Enrolled(
        client_id=body["client"]["id"],
        identity_id=body["client"]["identity_id"],
        headers=enrolment_headers(body["token"]),
    )


async def enrol_dev(client: AsyncClient, name: str = "dev-box") -> Enrolled:
    """Register a dev-mode client — no GitHub account behind it, so no identity."""
    response = await client.post("/clients", json={**REGISTRATION_PAYLOAD, "name": name})
    assert response.status_code == HTTPStatus.CREATED
    body = response.json()
    assert body["client"]["identity_id"] is None
    return Enrolled(
        client_id=body["client"]["id"],
        identity_id=None,
        headers=enrolment_headers(body["token"]),
    )


def github_mode_without_github(monkeypatch: pytest.MonkeyPatch) -> None:
    """Switch to github mode for a test that must never reach GitHub."""
    use_github_mode(monkeypatch, FakeGithubIdentityService(forbidden=True))


async def seed_policy(client: AsyncClient, repo: str, min_tier: str = "standard") -> None:
    """Put a policy row in place while the suite is still in dev mode."""
    assert settings.client_enrolment_mode == "dev"
    response = await client.put(f"/policies/{repo}", json={"min_tier": min_tier})
    assert response.status_code == HTTPStatus.OK


# ---------------------------------------------------------------------------
# PUT /policies/{owner}/{repo} — github mode
# ---------------------------------------------------------------------------


async def test_policy_write_allowed_for_admin_permission(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The positive control: recorded repo admin is what opens the write."""
    enrolled = await enrol_github(
        client, monkeypatch, repo_access=[access(repo=ADMIN_REPO, permission=PermissionLevel.ADMIN)]
    )

    response = await client.put(f"/policies/{ADMIN_REPO}", json={"min_tier": "frontier"}, headers=enrolled.headers)

    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["repo_full_name"] == ADMIN_REPO
    assert body["min_tier"] == "frontier"


async def test_policy_write_rejected_for_write_permission(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Push access is not policy access — the floor is an owner's decision."""
    enrolled = await enrol_github(
        client, monkeypatch, repo_access=[access(repo=ADMIN_REPO, permission=PermissionLevel.WRITE)]
    )

    response = await client.put(f"/policies/{ADMIN_REPO}", json={"min_tier": "experimental"}, headers=enrolled.headers)

    assert response.status_code == HTTPStatus.FORBIDDEN


async def test_policy_write_rejected_for_maintain_collapsed_permission(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A GitHub `maintain` collaborator is refused, same as any other non-admin.

    Traceability, not duplication of the case above: `collapse_permissions`
    maps GitHub's `maintain` boolean to `PermissionLevel.WRITE` and never to
    `ADMIN` (that mapping is tested where it lives), so `WRITE` *is* what a
    maintainer looks like by the time it reaches this gate. Pinned separately
    so a future change that promoted `maintain` to `ADMIN` upstream would fail
    a test that names the role, not only one that names the enum value.

    Driven through the real `collapse_permissions` rather than a hand-picked
    `PermissionLevel.WRITE`: `GithubRepoAccess` only carries an
    already-collapsed level, so the raw GitHub-shaped booleans are collapsed
    here first, the same way `LiveGithubIdentityService.get_repo_access`
    would, and the assertion below confirms that collapse actually lands on
    `WRITE` before it is used to build the access entry.
    """
    maintain_permission = collapse_permissions({"admin": False, "maintain": True, "push": False, "pull": True})
    assert maintain_permission == PermissionLevel.WRITE

    enrolled = await enrol_github(
        client, monkeypatch, repo_access=[access(repo=ADMIN_REPO, permission=maintain_permission)]
    )

    response = await client.put(f"/policies/{ADMIN_REPO}", json={"min_tier": "experimental"}, headers=enrolled.headers)

    assert response.status_code == HTTPStatus.FORBIDDEN


async def test_policy_write_rejected_for_read_permission(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enrolled = await enrol_github(
        client, monkeypatch, repo_access=[access(repo=ADMIN_REPO, permission=PermissionLevel.READ)]
    )

    response = await client.put(f"/policies/{ADMIN_REPO}", json={"min_tier": "experimental"}, headers=enrolled.headers)

    assert response.status_code == HTTPStatus.FORBIDDEN


async def test_policy_write_rejected_when_repo_absent_from_access_set(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Admin on one repo is not admin on another — the check is per repo."""
    enrolled = await enrol_github(
        client, monkeypatch, repo_access=[access(repo=OTHER_REPO, permission=PermissionLevel.ADMIN)]
    )

    response = await client.put(f"/policies/{ADMIN_REPO}", json={"min_tier": "experimental"}, headers=enrolled.headers)

    assert response.status_code == HTTPStatus.FORBIDDEN


async def test_policy_write_rejected_for_unknown_token(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """401, not 403: a credential that resolves to nobody is an auth failure."""
    github_mode_without_github(monkeypatch)

    response = await client.put(
        f"/policies/{ADMIN_REPO}",
        json={"min_tier": "experimental"},
        headers=enrolment_headers("never-minted-this"),
    )

    assert response.status_code == HTTPStatus.UNAUTHORIZED


async def test_policy_write_rejected_for_dev_enrolled_client_in_github_mode(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client with no GitHub identity administers nothing once github mode is on.

    The stale-row case: the token is real and resolves, so this is a 403, not a
    401 — but "no identity" must read as "no permission", never as "unscoped".
    """
    enrolled = await enrol_dev(client)
    monkeypatch.setattr(settings, "client_enrolment_mode", "github")

    response = await client.put(f"/policies/{ADMIN_REPO}", json={"min_tier": "experimental"}, headers=enrolled.headers)

    assert response.status_code == HTTPStatus.FORBIDDEN


async def dashboard_session_headers(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    permission: PermissionLevel,
) -> dict[str, str]:
    """A signed-in dashboard session for one identity at one permission on ADMIN_REPO."""
    enrolee = Enrolee(
        login="marge-bouvier",
        user_id=20482231,
        repo_access=[access(repo=ADMIN_REPO, permission=permission)],
    )
    return await start_dashboard_session(client, monkeypatch, FakeGithubIdentityService(), enrolee)


async def test_policy_write_allowed_for_admin_dashboard_session(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signed-in admin may set the floor from the browser, exactly as from a client token.

    Why RFC 0002 B2: this supersedes the RFC 0001-era pin that refused every
    dashboard session here with 401 regardless of permission. B2's dashboard
    policy editor saves through this very endpoint, and D-POLICY's invariant —
    whoever GitHub says administers a repo may set that repo's floor — names the
    *identity*, not the credential kind. A session resolves to the same
    `github_identity` and the same cached per-repo permission a grid client
    does, so the admin check below is the identical one, not a relaxed one.
    """
    headers = await dashboard_session_headers(client, monkeypatch, PermissionLevel.ADMIN)

    response = await client.put(f"/policies/{ADMIN_REPO}", json={"min_tier": "frontier"}, headers=headers)

    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["repo_full_name"] == ADMIN_REPO
    assert body["min_tier"] == "frontier"


async def test_policy_write_rejected_for_non_admin_dashboard_session(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """403, not 401 — the write path knows this credential and refuses it on permission.

    Why RFC 0002 B2: the widening is to the credential *kinds* the resolver
    admits, never to the check itself. Push access is still not policy access
    for a browser session, exactly as `test_policy_write_rejected_for_write_permission`
    pins it for a machine token. The status code is the tell: 401 would mean the
    session was never resolved at all, 403 means it was and lacked admin.
    """
    headers = await dashboard_session_headers(client, monkeypatch, PermissionLevel.WRITE)

    response = await client.put(f"/policies/{ADMIN_REPO}", json={"min_tier": "experimental"}, headers=headers)

    assert response.status_code == HTTPStatus.FORBIDDEN


# ---------------------------------------------------------------------------
# The audit trail
# ---------------------------------------------------------------------------


async def test_policy_write_denied_logs_policy_write_denied(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A refusal has to name the human, the machine, the repo, and the level.

    `github_login`/`github_user_id` are required fields rather than nice
    extras: `client_id` and `identity_id` are opaque UUIDs, and "which GitHub
    account tried to lower this repo's floor" must be answerable from the log
    stream alone.
    """
    enrolled = await enrol_github(
        client, monkeypatch, repo_access=[access(repo=ADMIN_REPO, permission=PermissionLevel.WRITE)]
    )

    with caplog.at_level(logging.DEBUG):
        response = await client.put(
            f"/policies/{ADMIN_REPO}",
            json={"min_tier": "experimental"},
            headers=enrolled.headers,
        )

    assert response.status_code == HTTPStatus.FORBIDDEN
    denied = records_named(caplog, "policy_write_denied")
    assert len(denied) == 1
    record = denied[0]
    assert record.levelno == logging.WARNING
    assert record.__dict__["repo_full_name"] == ADMIN_REPO
    assert record.__dict__["client_id"] == enrolled.client_id
    assert record.__dict__["identity_id"] == enrolled.identity_id
    assert record.__dict__["permission"] == PermissionLevel.WRITE
    assert record.__dict__["github_login"] == "marge-bouvier"
    assert record.__dict__["github_user_id"] == 20482231

    # The bearer token is the means to retry; identity fields say who, not how.
    token = enrolled.headers["Authorization"].removeprefix("Bearer ")
    for captured in caplog.records:
        assert token not in dump(captured)


async def test_policy_write_denied_for_session_logs_the_identity_without_a_client_id(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A session-originated refusal still names the human — there is just no machine to name.

    Why RFC 0002 B2: once a dashboard session reaches this gate, `client_id`
    stops being a field every denial carries, because a person browsing has no
    row on the grid. It logs as None rather than being dropped: a missing key
    would read as an omission bug, and the identity fields — which are what
    "who tried to lower this repo's floor" actually needs — are still required.
    """
    headers = await dashboard_session_headers(client, monkeypatch, PermissionLevel.READ)

    with caplog.at_level(logging.DEBUG):
        response = await client.put(
            f"/policies/{ADMIN_REPO}",
            json={"min_tier": "experimental"},
            headers=headers,
        )

    assert response.status_code == HTTPStatus.FORBIDDEN
    denied = records_named(caplog, "policy_write_denied")
    assert len(denied) == 1
    record = denied[0]
    assert record.levelno == logging.WARNING
    assert record.__dict__["repo_full_name"] == ADMIN_REPO
    assert record.__dict__["client_id"] is None
    assert record.__dict__["identity_id"] is not None
    assert record.__dict__["permission"] == PermissionLevel.READ
    assert record.__dict__["github_login"] == "marge-bouvier"
    assert record.__dict__["github_user_id"] == 20482231

    # Same rule as the client-token denial above: identity fields say who, not how.
    token = headers["Authorization"].removeprefix("Bearer ")
    for captured in caplog.records:
        assert token not in dump(captured)


async def test_policy_write_authorized_logs_policy_write_authorized(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Successful writes are logged too — an audit trail of only refusals is half a trail."""
    enrolled = await enrol_github(
        client, monkeypatch, repo_access=[access(repo=ADMIN_REPO, permission=PermissionLevel.ADMIN)]
    )

    with caplog.at_level(logging.DEBUG):
        response = await client.put(
            f"/policies/{ADMIN_REPO}",
            json={"min_tier": "frontier"},
            headers=enrolled.headers,
        )

    assert response.status_code == HTTPStatus.OK
    authorized = records_named(caplog, "policy_write_authorized")
    assert len(authorized) == 1
    record = authorized[0]
    assert record.levelno == logging.INFO
    assert record.__dict__["repo_full_name"] == ADMIN_REPO
    assert record.__dict__["client_id"] == enrolled.client_id
    assert record.__dict__["identity_id"] == enrolled.identity_id
    assert record.__dict__["github_login"] == "marge-bouvier"
    assert record.__dict__["github_user_id"] == 20482231


# ---------------------------------------------------------------------------
# PUT /policies/{owner}/{repo} — dev mode
# ---------------------------------------------------------------------------


async def test_dev_mode_policy_write_accepts_shared_secret(client: AsyncClient) -> None:
    """No GitHub account, no repo admin to check — the shared secret stands in.

    Note there is no client registered at all: dev mode authorizes the raw
    enrolment secret, not a dev-registered client's own bearer token.
    """
    assert settings.client_enrolment_mode == "dev"

    response = await client.put(
        f"/policies/{ADMIN_REPO}",
        json={"min_tier": "frontier"},
        headers=enrolment_headers(DEV_SECRET),
    )

    assert response.status_code == HTTPStatus.OK
    assert response.json()["min_tier"] == "frontier"


async def test_dev_mode_policy_write_logs_the_bypass(
    client: AsyncClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Both records, every time: the shared comparison's, and this endpoint's own.

    `dev_mode_secret_used` says the secret was accepted; `policy_write_dev_mode_bypass`
    says *what it bought* — without the repo name, the audit trail cannot answer
    "whose floor was changed under the carve-out".
    """
    with caplog.at_level(logging.DEBUG):
        response = await client.put(
            f"/policies/{ADMIN_REPO}",
            json={"min_tier": "frontier"},
            headers=enrolment_headers(DEV_SECRET),
        )

    assert response.status_code == HTTPStatus.OK
    assert len(records_named(caplog, "dev_mode_secret_used")) == 1
    bypass = records_named(caplog, "policy_write_dev_mode_bypass")
    assert len(bypass) == 1
    assert bypass[0].levelno == logging.WARNING
    assert bypass[0].__dict__["repo_full_name"] == ADMIN_REPO
    assert bypass[0].__dict__["caller_identity"] == CALLER_IDENTITY_UNAVAILABLE_DEV_MODE
    assert all(DEV_SECRET not in dump(r) for r in caplog.records)


async def test_dev_mode_policy_write_rejects_wrong_secret(
    client: AsyncClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        response = await client.put(
            f"/policies/{ADMIN_REPO}",
            json={"min_tier": "frontier"},
            headers=enrolment_headers("not-the-secret"),
        )

    assert response.status_code == HTTPStatus.UNAUTHORIZED
    denied = records_named(caplog, "enrolment_denied")
    assert len(denied) == 1
    assert denied[0].__dict__["reason"] == "credential_rejected"


async def test_dev_mode_policy_write_denies_without_configured_secret(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unset secret must not degrade into "any header will do"."""
    monkeypatch.setattr(settings, "client_enrolment_secret", None)

    response = await client.put(
        f"/policies/{ADMIN_REPO}",
        json={"min_tier": "frontier"},
        headers=enrolment_headers("anything"),
    )

    assert response.status_code == HTTPStatus.UNAUTHORIZED


# ---------------------------------------------------------------------------
# GET /policies/{owner}/{repo}
# ---------------------------------------------------------------------------


async def test_get_single_policy_visible_with_read_permission(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reading a floor takes any recorded access; only changing it takes admin."""
    await seed_policy(client, ADMIN_REPO, min_tier="frontier")
    enrolled = await enrol_github(
        client, monkeypatch, repo_access=[access(repo=ADMIN_REPO, permission=PermissionLevel.READ)]
    )

    response = await client.get(f"/policies/{ADMIN_REPO}", headers=enrolled.headers)

    assert response.status_code == HTTPStatus.OK
    assert response.json()["repo_full_name"] == ADMIN_REPO


async def test_get_single_policy_hidden_when_repo_outside_access_set(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repo you cannot see answers exactly like a repo with no policy.

    A distinguishable 403 would make the endpoint an oracle: "this repo is on
    the hub, you just can't have it" leaks the repo layout of every other org
    to anyone who can enrol.
    """
    await seed_policy(client, HIDDEN_REPO)
    enrolled = await enrol_github(
        client, monkeypatch, repo_access=[access(repo=ADMIN_REPO, permission=PermissionLevel.ADMIN)]
    )

    hidden = await client.get(f"/policies/{HIDDEN_REPO}", headers=enrolled.headers)
    never_existed = await client.get(f"/policies/{OTHER_REPO}", headers=enrolled.headers)

    assert hidden.status_code == HTTPStatus.NOT_FOUND
    assert hidden.status_code == never_existed.status_code
    assert hidden.json() == never_existed.json()
    assert hidden.json() == {"detail": "No policy for this repo"}


async def test_get_single_policy_rejects_unknown_token(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed_policy(client, ADMIN_REPO)
    github_mode_without_github(monkeypatch)

    response = await client.get(f"/policies/{ADMIN_REPO}", headers=enrolment_headers("never-minted-this"))

    assert response.status_code == HTTPStatus.UNAUTHORIZED


# ---------------------------------------------------------------------------
# GET /policies
# ---------------------------------------------------------------------------


async def test_list_policies_filtered_to_caller_repos(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The list shows the caller's repos only, and pages over *those*.

    `limit=1` is the load-bearing half: filtering an already-paged list would
    hand back fewer rows than exist for this caller — silently broken
    pagination for anyone with partial access.
    """
    for repo in (ADMIN_REPO, OTHER_REPO, HIDDEN_REPO):
        await seed_policy(client, repo)
    enrolled = await enrol_github(
        client,
        monkeypatch,
        repo_access=[
            access(repo=ADMIN_REPO, permission=PermissionLevel.ADMIN),
            access(repo=HIDDEN_REPO, permission=PermissionLevel.READ),
        ],
    )

    response = await client.get("/policies", headers=enrolled.headers)

    assert response.status_code == HTTPStatus.OK
    assert [row["repo_full_name"] for row in response.json()] == [ADMIN_REPO, HIDDEN_REPO]

    first_page = await client.get("/policies", params={"limit": 1}, headers=enrolled.headers)
    assert first_page.status_code == HTTPStatus.OK
    assert [row["repo_full_name"] for row in first_page.json()] == [ADMIN_REPO]

    second_page = await client.get("/policies", params={"limit": 1, "offset": 1}, headers=enrolled.headers)
    assert second_page.status_code == HTTPStatus.OK
    assert [row["repo_full_name"] for row in second_page.json()] == [HIDDEN_REPO]


async def test_list_policies_empty_access_set_returns_empty_list(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seeing nothing is an answer, not an error — the caller is authenticated fine."""
    await seed_policy(client, ADMIN_REPO)
    enrolled = await enrol_github(client, monkeypatch, repo_access=[])

    response = await client.get("/policies", headers=enrolled.headers)

    assert response.status_code == HTTPStatus.OK
    assert response.json() == []


async def test_list_policies_dev_mode_returns_everything_unfiltered(client: AsyncClient) -> None:
    """The carve-out is inert, not permissive-by-accident: no account, no scope to derive."""
    assert settings.client_enrolment_mode == "dev"
    for repo in (ADMIN_REPO, OTHER_REPO, HIDDEN_REPO):
        await seed_policy(client, repo)

    response = await client.get("/policies", headers=enrolment_headers(DEV_SECRET))

    assert response.status_code == HTTPStatus.OK
    assert [row["repo_full_name"] for row in response.json()] == [ADMIN_REPO, OTHER_REPO, HIDDEN_REPO]


async def test_list_policies_rejects_unknown_token(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read filtering is permissive, but never open to a credential that resolves to nobody."""
    await seed_policy(client, ADMIN_REPO)
    github_mode_without_github(monkeypatch)

    response = await client.get("/policies", headers=enrolment_headers("never-minted-this"))

    assert response.status_code == HTTPStatus.UNAUTHORIZED

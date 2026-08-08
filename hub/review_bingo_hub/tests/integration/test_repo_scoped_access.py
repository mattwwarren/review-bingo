"""Integration tests for repo-scoped dispatch, job reads, and the roster.

Authorization on the grid is *derived from GitHub*, not declared by the client:
a client may only lease, read, or discover work in repos the GitHub account it
enrolled under can actually reach. These tests pin the data-plane half of that
invariant — the enrolment half lives in `test_client_enrolment.py`.

Two properties matter more than the rest and are asserted directly rather than
inferred:

* A job outside the caller's access set is **indistinguishable from a job that
  never existed**. A 403 there would turn the targeted-lease endpoint into an
  existence oracle: "this id is real, you just can't have it" leaks the repo
  layout of every org on the hub to anyone who can enrol.
* The dev-mode carve-out is *inert*, not *permissive-by-accident*. Under
  `CLIENT_ENROLMENT_MODE=dev` there is no GitHub account behind a client, so
  there is no access set to filter by and tier remains the only gate. Under
  `github` mode a client with no identity at all sees nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from review_bingo_hub.core.config import settings
from review_bingo_hub.models.github_identity import IdentityRepoAccess
from review_bingo_hub.models.review_client import ReviewClient, ReviewClientCreate
from review_bingo_hub.services.client_service import register_client
from review_bingo_hub.services.github_identity_service import GithubRepoAccess
from review_bingo_hub.services.identity_service import (
    accessible_repo_names,
    get_or_create_identity,
    identity_access_is_stale,
)
from review_bingo_hub.services.job_service import lease_next_job
from review_bingo_hub.tests.integration.conftest import (
    GITHUB_TOKEN,
    FakeGithubIdentityService,
    backdate_access_refreshed_at,
    enrolment_headers,
    marge,
    readable,
    use_github_mode,
)

PR_WEBHOOK_HEADERS = {"X-GitHub-Event": "pull_request"}

# One repo the caller can reach and one it cannot: every access assertion below
# is "in ALLOWED, not in FORBIDDEN", so naming them once keeps the tests about
# the boundary rather than about string literals.
ALLOWED = "acme/payments"
FORBIDDEN = "acme/other-repo"
UNRELATED = "acme/unrelated"


def pr_payload(repo: str, sha: str, number: int = 7) -> dict[str, Any]:
    return {
        "action": "opened",
        "repository": {"full_name": repo},
        "pull_request": {"number": number, "head": {"sha": sha}, "title": "Fix rounding"},
    }


async def enqueue(client: AsyncClient, repo: str, sha: str, number: int = 7) -> str:
    """Call a review round from a PR webhook, returning the queued job's id."""
    response = await client.post("/webhooks/github", json=pr_payload(repo, sha, number), headers=PR_WEBHOOK_HEADERS)
    assert response.status_code == HTTPStatus.OK
    assert response.json()["status"] == "queued"
    job_id: str = response.json()["job_id"]
    return job_id


@dataclass(frozen=True)
class Enrolee:
    """One GitHub account to enrol a client under, plus what GitHub says it can reach.

    Bundled into one object rather than spread across parameters of
    `enrol_github_client` so that helper stays inside the repo's argument-count
    limit; the fields are exactly the knobs the tests below need to turn.
    """

    login: str
    user_id: int
    repo_access: list[GithubRepoAccess] = field(default_factory=list)
    tier: str = "standard"
    name: str | None = None


async def enrol_github_client(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeGithubIdentityService,
    enrolee: Enrolee,
) -> tuple[str, dict[str, str]]:
    """Register + check in one github-mode client under a specific identity + access set.

    Calls `use_github_mode` (idempotent if a test already called it) then points
    `fake` at this identity/access set immediately before the POST it makes.
    Safe to call repeatedly with distinct enrolees in the same test: registration
    is awaited one request at a time, and once a call returns, that client's row
    and bearer token are independent durable facts — the fake only has to
    represent one identity at the instant of registration, not all of them at
    once. Returns (client_id, bearer headers).
    """
    use_github_mode(monkeypatch, fake)
    fake.identity = marge(login=enrolee.login, user_id=enrolee.user_id)
    fake.repo_access = list(enrolee.repo_access)

    response = await client.post(
        "/clients",
        json={
            "name": enrolee.name or enrolee.login,
            "model_name": "test-model",
            "provider": "test",
            "tier": enrolee.tier,
        },
        headers=enrolment_headers(GITHUB_TOKEN),
    )
    assert response.status_code == HTTPStatus.CREATED
    body = response.json()
    headers = enrolment_headers(body["token"])

    check_in = await client.post("/clients/check-in", headers=headers)
    assert check_in.status_code == HTTPStatus.OK
    client_id: str = body["client"]["id"]
    return client_id, headers


async def enrol_dev_client(client: AsyncClient, name: str) -> tuple[str, dict[str, str]]:
    """Register + check in a dev-mode client, riding the fixture's placeholder secret."""
    response = await client.post(
        "/clients",
        json={"name": name, "model_name": "test-model", "provider": "test", "tier": "standard"},
    )
    assert response.status_code == HTTPStatus.CREATED
    body = response.json()
    headers = enrolment_headers(body["token"])

    check_in = await client.post("/clients/check-in", headers=headers)
    assert check_in.status_code == HTTPStatus.OK
    client_id: str = body["client"]["id"]
    return client_id, headers


async def make_client_row(session: AsyncSession, name: str, identity_id: UUID | None = None) -> ReviewClient:
    """A registered client straight through the service layer, no HTTP."""
    client_row, _token = await register_client(
        session,
        ReviewClientCreate(name=name, model_name="test-model", provider="test"),
        identity_id=identity_id,
    )
    return client_row


# ---------------------------------------------------------------------------
# accessible_repo_names — the one place "what can this caller see" is decided
# ---------------------------------------------------------------------------


async def test_accessible_repo_names_none_in_dev_mode(session: AsyncSession) -> None:
    """Dev mode has no GitHub account to derive access from, so the filter stays inert."""
    assert settings.client_enrolment_mode == "dev"

    grid_client = await make_client_row(session, "dev-box")

    assert await accessible_repo_names(session, grid_client) is None


async def test_accessible_repo_names_empty_for_github_mode_client_without_identity(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A github-mode client with no identity sees nothing — not everything.

    This is the stale pre-A1 row case. It would fail loudly if the dev-mode
    carve-out were implemented as a blanket `identity_id is None` check, which
    would silently hand every legacy row the whole queue.
    """
    monkeypatch.setattr(settings, "client_enrolment_mode", "github")

    grid_client = await make_client_row(session, "stale-pre-a1-row")
    assert grid_client.identity_id is None

    assert await accessible_repo_names(session, grid_client) == frozenset()


async def test_accessible_repo_names_reflects_identity_repo_access_rows(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "client_enrolment_mode", "github")

    identity = await get_or_create_identity(
        session,
        github_user_id=20482231,
        github_login="marge-bouvier",
        repo_access=[readable(ALLOWED), readable("acme/docs")],
    )
    grid_client = await make_client_row(session, "marge-mac-mini", identity_id=identity.id)

    assert await accessible_repo_names(session, grid_client) == frozenset({ALLOWED, "acme/docs"})


# ---------------------------------------------------------------------------
# Dispatch filtering
# ---------------------------------------------------------------------------


async def test_lease_next_job_excludes_jobs_outside_access_set(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FIFO must not reach past the access set to the older, out-of-access job.

    The out-of-access job is enqueued first and clears the same tier floor, so
    only the access filter can explain the newer job winning.
    """
    await enqueue(client, FORBIDDEN, "outside-older")
    wanted = await enqueue(client, ALLOWED, "inside-newer", number=8)

    fake = FakeGithubIdentityService()
    _, headers = await enrol_github_client(
        client, monkeypatch, fake, Enrolee(login="solo", user_id=1, repo_access=[readable(ALLOWED)])
    )

    response = await client.post("/jobs/lease", headers=headers)

    assert response.status_code == HTTPStatus.OK
    lease = response.json()
    assert lease is not None
    assert lease["job"]["id"] == wanted
    assert lease["job"]["repo_full_name"] == ALLOWED


async def test_lease_next_job_returns_null_when_only_out_of_access_jobs_queued(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queue full of other people's repos reads as a dry queue, not an error."""
    await enqueue(client, FORBIDDEN, "only-outside")

    fake = FakeGithubIdentityService()
    _, headers = await enrol_github_client(
        client, monkeypatch, fake, Enrolee(login="solo", user_id=1, repo_access=[readable(ALLOWED)])
    )

    response = await client.post("/jobs/lease", headers=headers)

    assert response.status_code == HTTPStatus.OK
    assert response.json() is None


async def test_targeted_lease_on_out_of_access_job_returns_404_indistinguishable_from_nonexistent(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Naming a real job you cannot reach must answer exactly like naming a fiction.

    Guards against `/jobs/{id}/lease` becoming an existence oracle. A 403 (or
    any distinguishable body) would let anyone who can enrol enumerate job ids
    and learn which ones are real, which leaks the repo layout of every other
    org on the hub.
    """
    forbidden_id = await enqueue(client, FORBIDDEN, "oracle-probe")

    fake = FakeGithubIdentityService()
    _, headers = await enrol_github_client(
        client, monkeypatch, fake, Enrolee(login="prober", user_id=1, repo_access=[readable(ALLOWED)])
    )

    real_but_forbidden = await client.post(f"/jobs/{forbidden_id}/lease", headers=headers)
    never_existed = await client.post(f"/jobs/{uuid4()}/lease", headers=headers)

    assert real_but_forbidden.status_code == HTTPStatus.NOT_FOUND
    assert real_but_forbidden.status_code == never_existed.status_code
    assert real_but_forbidden.json() == never_existed.json()


async def test_targeted_lease_in_access_but_above_tier_still_403(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tier refusal still explains itself once the access check has passed.

    Access and tier are different questions: hiding a repo you cannot see is
    security, but hiding a floor you *could* clear with a better model would
    just be unhelpful.
    """
    policy = await client.put(f"/policies/{ALLOWED}", json={"min_tier": "frontier"})
    assert policy.status_code == HTTPStatus.OK
    job_id = await enqueue(client, ALLOWED, "floor-in-access")

    fake = FakeGithubIdentityService()
    _, headers = await enrol_github_client(
        client,
        monkeypatch,
        fake,
        Enrolee(login="toy-box", user_id=1, repo_access=[readable(ALLOWED)], tier="experimental"),
    )

    response = await client.post(f"/jobs/{job_id}/lease", headers=headers)

    assert response.status_code == HTTPStatus.FORBIDDEN
    assert "frontier" in response.json()["detail"]


async def test_targeted_lease_in_access_hands_the_job_over(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control for the targeted path — the other three cases are all refusals.

    Without this, an access filter that excluded *everything* would satisfy
    every 404 above and look correct.
    """
    job_id = await enqueue(client, ALLOWED, "target-in-access")

    fake = FakeGithubIdentityService()
    _, headers = await enrol_github_client(
        client, monkeypatch, fake, Enrolee(login="picker", user_id=1, repo_access=[readable(ALLOWED)])
    )

    response = await client.post(f"/jobs/{job_id}/lease", headers=headers)

    assert response.status_code == HTTPStatus.OK
    lease = response.json()
    assert lease["job"]["id"] == job_id
    assert lease["job"]["state"] == "leased"


async def test_report_succeeds_after_access_narrows_post_lease(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Holding the lease is enough to report, even if access has since narrowed.

    Pins `report_job_endpoint`'s deliberate exception: it stays on the
    unscoped `get_job` rather than `get_job_for_client`, because refusing a
    report over a since-narrowed access snapshot would destroy a client's
    finished work without protecting anything the caller couldn't already
    reach when the lease was granted.
    """
    job_id = await enqueue(client, ALLOWED, "narrows-after-lease")

    fake = FakeGithubIdentityService()
    client_id, headers = await enrol_github_client(
        client, monkeypatch, fake, Enrolee(login="narrows", user_id=1, repo_access=[readable(ALLOWED)])
    )

    lease = await client.post(f"/jobs/{job_id}/lease", headers=headers)
    assert lease.status_code == HTTPStatus.OK
    assert lease.json()["job"]["id"] == job_id

    # Simulate the account's GitHub access narrowing after the lease was
    # granted, ahead of A2's own re-attestation: drop the identity's
    # IdentityRepoAccess rows directly.
    grid_client = (
        await session.execute(select(ReviewClient).where(col(ReviewClient.id) == UUID(client_id)))
    ).scalar_one()
    await session.execute(
        delete(IdentityRepoAccess).where(col(IdentityRepoAccess.identity_id) == grid_client.identity_id)
    )
    await session.commit()
    assert await accessible_repo_names(session, grid_client) == frozenset()

    report = {"verdict": "approve", "summary": "Nothing found.", "findings": []}
    response = await client.post(f"/jobs/{job_id}/report", json=report, headers=headers)

    assert response.status_code == HTTPStatus.OK
    assert response.json()["state"] == "relayed"


# ---------------------------------------------------------------------------
# Job reads
# ---------------------------------------------------------------------------


async def test_list_jobs_filters_to_callers_access_set(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inside = await enqueue(client, ALLOWED, "list-inside")
    await enqueue(client, FORBIDDEN, "list-outside")

    fake = FakeGithubIdentityService()
    _, headers = await enrol_github_client(
        client, monkeypatch, fake, Enrolee(login="lister", user_id=1, repo_access=[readable(ALLOWED)])
    )

    response = await client.get("/jobs", headers=headers)

    assert response.status_code == HTTPStatus.OK
    assert [job["id"] for job in response.json()] == [inside]

    # The pre-existing state filter ANDs with the access filter, same as repo does.
    queued = await client.get("/jobs", params={"state": "queued"}, headers=headers)
    assert queued.status_code == HTTPStatus.OK
    assert [job["id"] for job in queued.json()] == [inside]


async def test_get_job_out_of_access_returns_404(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forbidden_id = await enqueue(client, FORBIDDEN, "get-outside")

    fake = FakeGithubIdentityService()
    _, headers = await enrol_github_client(
        client, monkeypatch, fake, Enrolee(login="reader", user_id=1, repo_access=[readable(ALLOWED)])
    )

    response = await client.get(f"/jobs/{forbidden_id}", headers=headers)

    assert response.status_code == HTTPStatus.NOT_FOUND


async def test_job_comment_out_of_access_returns_404(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """404 rather than the 409 an unreported-but-visible job would get.

    The status code difference is the assertion: reaching 409 would mean the
    access check never ran.
    """
    forbidden_id = await enqueue(client, FORBIDDEN, "comment-outside")

    fake = FakeGithubIdentityService()
    _, headers = await enrol_github_client(
        client, monkeypatch, fake, Enrolee(login="reader", user_id=1, repo_access=[readable(ALLOWED)])
    )

    response = await client.get(f"/jobs/{forbidden_id}/comment", headers=headers)

    assert response.status_code == HTTPStatus.NOT_FOUND


async def test_relay_target_out_of_access_returns_404(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forbidden_id = await enqueue(client, FORBIDDEN, "relay-outside")

    fake = FakeGithubIdentityService()
    _, headers = await enrol_github_client(
        client, monkeypatch, fake, Enrolee(login="reader", user_id=1, repo_access=[readable(ALLOWED)])
    )

    response = await client.get(f"/jobs/{forbidden_id}/relay-target", headers=headers)

    assert response.status_code == HTTPStatus.NOT_FOUND


async def test_get_job_in_access_returns_200(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control: an over-broad filter that excluded everything would pass all the 404s above."""
    job_id = await enqueue(client, ALLOWED, "get-inside")

    fake = FakeGithubIdentityService()
    _, headers = await enrol_github_client(
        client, monkeypatch, fake, Enrolee(login="reader", user_id=1, repo_access=[readable(ALLOWED)])
    )

    response = await client.get(f"/jobs/{job_id}", headers=headers)

    assert response.status_code == HTTPStatus.OK
    assert response.json()["repo_full_name"] == ALLOWED


async def test_list_jobs_repo_filter_outside_access_set_returns_empty_not_error(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`?repo_full_name=` ANDs with the access set — no special case, no oracle.

    A list response never confirms or denies one specific job's existence, so
    an empty list is both the honest answer and a safe one.
    """
    await enqueue(client, FORBIDDEN, "and-filter")

    fake = FakeGithubIdentityService()
    _, headers = await enrol_github_client(
        client, monkeypatch, fake, Enrolee(login="lister", user_id=1, repo_access=[readable(ALLOWED)])
    )

    response = await client.get("/jobs", params={"repo_full_name": FORBIDDEN}, headers=headers)

    assert response.status_code == HTTPStatus.OK
    assert response.json() == []


# ---------------------------------------------------------------------------
# Roster
# ---------------------------------------------------------------------------


async def test_roster_returns_clients_sharing_an_accessible_repo(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The roster is the set of machines you could plausibly be sharing work with."""
    fake = FakeGithubIdentityService()
    a_id, a_headers = await enrol_github_client(
        client, monkeypatch, fake, Enrolee(login="ay", user_id=1, repo_access=[readable(ALLOWED)])
    )
    b_id, _ = await enrol_github_client(
        client, monkeypatch, fake, Enrolee(login="bee", user_id=2, repo_access=[readable(ALLOWED)])
    )
    c_id, _ = await enrol_github_client(
        client, monkeypatch, fake, Enrolee(login="cee", user_id=3, repo_access=[readable(UNRELATED)])
    )

    response = await client.get("/clients", headers=a_headers)

    assert response.status_code == HTTPStatus.OK
    ids = {row["id"] for row in response.json()}
    assert ids == {a_id, b_id}
    assert c_id not in ids


async def test_roster_always_includes_callers_own_client_with_no_overlap(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An account that shares no repo with anyone still sees itself, and only itself."""
    fake = FakeGithubIdentityService()
    lonely_id, lonely_headers = await enrol_github_client(
        client, monkeypatch, fake, Enrolee(login="dee", user_id=4, repo_access=[])
    )
    other_id, _ = await enrol_github_client(
        client, monkeypatch, fake, Enrolee(login="ee", user_id=5, repo_access=[readable(ALLOWED)])
    )

    response = await client.get("/clients", headers=lonely_headers)

    assert response.status_code == HTTPStatus.OK
    ids = {row["id"] for row in response.json()}
    assert ids == {lonely_id}
    assert other_id not in ids


async def test_roster_unfiltered_in_dev_mode(client: AsyncClient) -> None:
    """Regression guard for the carve-out on the roster path, mirroring the dispatch one."""
    assert settings.client_enrolment_mode == "dev"

    first_id, first_headers = await enrol_dev_client(client, "dev-one")
    second_id, _ = await enrol_dev_client(client, "dev-two")

    response = await client.get("/clients", headers=first_headers)

    assert response.status_code == HTTPStatus.OK
    assert {row["id"] for row in response.json()} == {first_id, second_id}


# ---------------------------------------------------------------------------
# Staleness (D-TTL) — an access snapshot too old to lease against
# ---------------------------------------------------------------------------
#
# Access scoping above answers "which repos"; this answers "how old an answer
# will we still act on". The two are independent gates: everything below has a
# perfectly good access set, and is refused purely on the age of the clock.
#
# The design decision these tests pin is *where* the gate sits. Leasing enforces
# staleness; reads and reports do not. Leasing is the hub handing out new work
# on the strength of a cached authorization, so that is the moment the age of
# the cache matters. A read shows a client the queue it was already shown, and a
# report is finished work whose authorization was granted at lease time — both
# have a narrower blast radius than a fresh dispatch, and refusing the report
# would destroy work rather than protect anything.


DAY_SECONDS = 24 * 60 * 60


async def identity_id_of(session: AsyncSession, client_id: str) -> UUID:
    """The GitHub identity a registered client is linked to.

    `expire_all()` first: `expire_on_commit=False` means a row this session read
    before the enrolment request would still carry its pre-request values.
    """
    session.expire_all()
    grid_client = (
        await session.execute(select(ReviewClient).where(col(ReviewClient.id) == UUID(client_id)))
    ).scalar_one()
    assert grid_client.identity_id is not None
    return grid_client.identity_id


async def enrol_with_stale_access(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    login: str,
) -> dict[str, str]:
    """Enrol a client on ALLOWED, then age its access snapshot past the default TTL.

    Backdates a full day rather than shrinking the TTL: these tests are about
    the endpoint's refusal, and the boundary itself is pinned by the
    `identity_access_is_stale` tests above. Returns the client's bearer headers.
    """
    fake = FakeGithubIdentityService()
    client_id, headers = await enrol_github_client(
        client, monkeypatch, fake, Enrolee(login=login, user_id=1, repo_access=[readable(ALLOWED)])
    )
    await backdate_access_refreshed_at(session, await identity_id_of(session, client_id), seconds_ago=DAY_SECONDS)
    return headers


async def test_identity_access_is_stale_true_past_ttl(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "client_enrolment_mode", "github")
    monkeypatch.setattr(settings, "identity_access_ttl_seconds", 60)

    identity = await get_or_create_identity(
        session, github_user_id=1, github_login="aged", repo_access=[readable(ALLOWED)]
    )
    grid_client = await make_client_row(session, "aged-box", identity_id=identity.id)
    await backdate_access_refreshed_at(session, identity.id, seconds_ago=3600)

    assert await identity_access_is_stale(session, grid_client) is True


async def test_identity_access_is_stale_false_within_ttl(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The positive control: a gate that returned True unconditionally would pass the rest."""
    monkeypatch.setattr(settings, "client_enrolment_mode", "github")
    monkeypatch.setattr(settings, "identity_access_ttl_seconds", 3600)

    identity = await get_or_create_identity(
        session, github_user_id=2, github_login="fresh", repo_access=[readable(ALLOWED)]
    )
    grid_client = await make_client_row(session, "fresh-box", identity_id=identity.id)
    await backdate_access_refreshed_at(session, identity.id, seconds_ago=60)

    assert await identity_access_is_stale(session, grid_client) is False


async def test_identity_access_is_stale_false_in_dev_mode(session: AsyncSession) -> None:
    """Dev mode has no GitHub snapshot to go stale, so the clock is not consulted."""
    assert settings.client_enrolment_mode == "dev"

    identity = await get_or_create_identity(
        session, github_user_id=3, github_login="dev-mode", repo_access=[readable(ALLOWED)]
    )
    grid_client = await make_client_row(session, "dev-box", identity_id=identity.id)
    await backdate_access_refreshed_at(session, identity.id, seconds_ago=DAY_SECONDS)

    assert await identity_access_is_stale(session, grid_client) is False


async def test_identity_access_is_stale_false_without_identity_id(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No account means no snapshot to age — and `accessible_repo_names` already fails it closed.

    Calling this client stale would answer "check in again" to something whose
    real problem is that it has an empty access set and can lease nothing
    regardless. One refusal per cause.
    """
    monkeypatch.setattr(settings, "client_enrolment_mode", "github")

    grid_client = await make_client_row(session, "stale-pre-a1-row")
    assert grid_client.identity_id is None

    assert await identity_access_is_stale(session, grid_client) is False


async def test_lease_next_job_refused_when_access_stale(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leasable, in-access job is still refused once the snapshot is too old.

    The job clears both existing gates — right repo, right tier — so only the
    TTL can explain the 409.
    """
    await enqueue(client, ALLOWED, "stale-next")
    headers = await enrol_with_stale_access(client, session, monkeypatch, "staler")

    response = await client.post("/jobs/lease", headers=headers)

    assert response.status_code == HTTPStatus.CONFLICT
    assert "check in again" in response.json()["detail"].lower()


async def test_lease_specific_job_refused_when_access_stale(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Naming a job is not a way around the TTL, same as it is not a way around the floor.

    409 rather than the 404 an out-of-access job gets: the caller *can* see this
    repo, so there is no existence to protect — only a refresh to ask for.
    """
    job_id = await enqueue(client, ALLOWED, "stale-specific")
    headers = await enrol_with_stale_access(client, session, monkeypatch, "specific-staler")

    response = await client.post(f"/jobs/{job_id}/lease", headers=headers)

    assert response.status_code == HTTPStatus.CONFLICT
    assert "check in again" in response.json()["detail"].lower()


async def test_lease_next_job_unaffected_by_staleness_in_dev_mode(
    client: AsyncClient,
    session: AsyncSession,
) -> None:
    """The dev-mode carve-out is inert here too, and asserted rather than assumed.

    Deliberately stronger than "a dev-mode client has no identity to age": this
    one *does* link a fully backdated identity, so a staleness gate implemented
    without the mode check would refuse, and only the carve-out can explain the
    lease succeeding.
    """
    assert settings.client_enrolment_mode == "dev"
    await enqueue(client, ALLOWED, "dev-mode-stale")

    identity = await get_or_create_identity(
        session, github_user_id=4, github_login="dev-mode-lease", repo_access=[readable(ALLOWED)]
    )
    grid_client = await make_client_row(session, "dev-lease-box", identity_id=identity.id)
    await backdate_access_refreshed_at(session, identity.id, seconds_ago=DAY_SECONDS)

    leased = await lease_next_job(session, grid_client)

    assert leased is not None
    assert leased.repo_full_name == ALLOWED


async def test_list_jobs_unaffected_by_stale_access(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reads keep working on a stale snapshot — the gate is on leasing only.

    A regression guard for the scope of the gate, not an oversight: pushing the
    TTL onto reads would blank the dashboard of anyone who had not checked in
    recently, without preventing a single dispatch.
    """
    job_id = await enqueue(client, ALLOWED, "stale-list")
    headers = await enrol_with_stale_access(client, session, monkeypatch, "lister-staler")

    response = await client.get("/jobs", headers=headers)

    assert response.status_code == HTTPStatus.OK
    assert [job["id"] for job in response.json()] == [job_id]


async def test_report_job_unaffected_by_stale_access(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lease granted while fresh can still be reported after the snapshot ages out.

    The same reasoning as `test_report_succeeds_after_access_narrows_post_lease`,
    applied to the other way authorization can go stale: the work is already
    done, and refusing it would throw that work away without withdrawing an
    access the client no longer has.
    """
    job_id = await enqueue(client, ALLOWED, "stale-report")

    fake = FakeGithubIdentityService()
    client_id, headers = await enrol_github_client(
        client, monkeypatch, fake, Enrolee(login="reporter-staler", user_id=1, repo_access=[readable(ALLOWED)])
    )
    lease = await client.post(f"/jobs/{job_id}/lease", headers=headers)
    assert lease.status_code == HTTPStatus.OK

    await backdate_access_refreshed_at(session, await identity_id_of(session, client_id), seconds_ago=DAY_SECONDS)

    report = {"verdict": "approve", "summary": "Nothing found.", "findings": []}
    response = await client.post(f"/jobs/{job_id}/report", json=report, headers=headers)

    assert response.status_code == HTTPStatus.OK
    assert response.json()["state"] == "relayed"

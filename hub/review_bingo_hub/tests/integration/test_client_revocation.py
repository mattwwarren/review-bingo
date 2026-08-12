"""Integration tests for `DELETE /clients/{client_id}` — self-service revocation.

A lost, compromised, or retired machine holds a valid bearer token until
something removes it. Check-out does not: it is a courtesy signal a *working*
client sends, and a stolen laptop does not send courtesies. So revocation is
its own endpoint, and RFC 0002 D-SELFREVOKE fixes exactly how far it reaches.

Four properties are asserted directly rather than inferred:

* **Self-service within an identity, and no further.** Either credential kind
  that resolves to a GitHub account — a grid client's own token or a signed-in
  dashboard session — may revoke a client bound to that same account. Removing
  *someone else's* machine is deliberately not a hub feature: that authority
  stays in GitHub, where revoking repo access ends their leasing at their next
  attestation or at TTL expiry. A hub-side cross-user kick would be a second,
  staler authority — the thing RFC 0001 refused to build.
* **A refusal is a 404, and an unknown id is the same 404.** Out-of-identity
  and nonexistent answer identically (RFC 0001 D-404), or the endpoint becomes
  an oracle confirming which client ids are real — and with them, how many
  machines every other org runs. The collapse governs the *response* only: the
  hub operator's log still says which of the two happened, because a log line
  is not a surface a caller can probe.
* **The lease is released now, not at TTL.** "This machine is gone" is a
  present-tense claim; making the queue wait out `LEASE_TTL_SECONDS` for work
  nobody is doing would answer it in the future tense.
* **The dev-mode carve-out is the *same* named mode, not a second bypass.**
  Under `CLIENT_ENROLMENT_MODE=dev` the shared enrolment secret may revoke —
  and a client's own hub-minted token, which is not that secret, may not.
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from review_bingo_hub.core.config import settings
from review_bingo_hub.models.review_client import ReviewClient
from review_bingo_hub.models.review_job import JobState, ReviewJob
from review_bingo_hub.services.identity_service import CALLER_IDENTITY_UNAVAILABLE_DEV_MODE
from review_bingo_hub.tests.integration.conftest import (
    ALLOWED,
    Enrolee,
    FakeGithubIdentityService,
    dump,
    enqueue,
    enrol_github_client,
    enrolment_headers,
    readable,
    records_named,
    start_dashboard_session,
    use_github_mode,
)

# The suite's autouse `isolate_github_app_config` pins dev mode and this secret,
# so it is also the credential the shared `client` fixture already sends.
DEV_SECRET = "test-fixture-placeholder"

# The one refusal string the endpoint is allowed to produce. Named once so a
# test cannot accidentally assert two different-but-both-404 answers and call
# the disclosure rule satisfied.
NOT_FOUND_DETAIL = "Client not found"

REGISTRATION_PAYLOAD = {
    "name": "marge-mac-mini",
    "model_name": "qwen2.5-coder-32b",
    "provider": "ollama",
    "tier": "standard",
}

REPORT_PAYLOAD = {"verdict": "approve", "summary": "Nothing found.", "findings": []}


async def register_dev_client(client: AsyncClient, name: str) -> tuple[str, dict[str, str]]:
    """Register a dev-mode client — no GitHub account behind it, so no identity."""
    response = await client.post("/clients", json={**REGISTRATION_PAYLOAD, "name": name})
    assert response.status_code == HTTPStatus.CREATED
    body = response.json()
    assert body["client"]["identity_id"] is None
    return body["client"]["id"], enrolment_headers(body["token"])


def github_mode_without_github(monkeypatch: pytest.MonkeyPatch) -> None:
    """Switch to github mode for a test that must never reach GitHub."""
    use_github_mode(monkeypatch, FakeGithubIdentityService(forbidden=True))


async def job_state(session: AsyncSession, job_id: str) -> str:
    """Read one job's state as a bare column, not an ORM row.

    A column read rather than a `select(ReviewJob)`: the test session already
    holds identity-mapped `ReviewJob` objects from earlier assertions, and an
    ORM fetch would hand back the stale in-session copy rather than what the
    app's own session just committed.
    """
    result = await session.execute(select(ReviewJob.state).where(col(ReviewJob.id) == UUID(job_id)))
    return str(result.scalar_one())


# ---------------------------------------------------------------------------
# github mode — the authorized paths
# ---------------------------------------------------------------------------


async def test_self_revoke_removes_client_and_invalidates_token(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point: a machine can retire itself, and its token dies with it.

    The follow-up check-in is the load-bearing half. A soft delete, or a row
    left behind with a status flag, would satisfy "204 returned" while leaving
    the stolen laptop's bearer token working.
    """
    fake = FakeGithubIdentityService()
    client_id, headers = await enrol_github_client(
        client, monkeypatch, fake, Enrolee(login="retiree", user_id=1, repo_access=[readable(ALLOWED)])
    )

    response = await client.delete(f"/clients/{client_id}", headers=headers)

    assert response.status_code == HTTPStatus.NO_CONTENT
    row = await session.execute(select(ReviewClient).where(col(ReviewClient.id) == UUID(client_id)))
    assert row.scalar_one_or_none() is None

    check_in = await client.post("/clients/check-in", headers=headers)
    assert check_in.status_code == HTTPStatus.UNAUTHORIZED


async def test_dashboard_session_revokes_client_under_same_identity(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A person signed into the dashboard may retire their own machine.

    D-SELFREVOKE names the *identity*, not the credential kind: the machine
    that needs revoking is often the one that cannot make the call — that is
    what "lost or compromised" means.
    """
    fake = FakeGithubIdentityService()
    owner = Enrolee(login="marge-bouvier", user_id=20482231, repo_access=[readable(ALLOWED)])
    client_id, _ = await enrol_github_client(client, monkeypatch, fake, owner)
    session_headers = await start_dashboard_session(client, monkeypatch, fake, owner)

    response = await client.delete(f"/clients/{client_id}", headers=session_headers)

    assert response.status_code == HTTPStatus.NO_CONTENT
    row = await session.execute(select(ReviewClient).where(col(ReviewClient.id) == UUID(client_id)))
    assert row.scalar_one_or_none() is None


# ---------------------------------------------------------------------------
# github mode — the refusals, and the disclosure rule
# ---------------------------------------------------------------------------


async def test_revoke_out_of_identity_target_returns_404(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another account's machine is not yours to unplug — and stays running."""
    fake = FakeGithubIdentityService()
    _, mine = await enrol_github_client(
        client, monkeypatch, fake, Enrolee(login="mine", user_id=1, repo_access=[readable(ALLOWED)])
    )
    theirs_id, _ = await enrol_github_client(
        client, monkeypatch, fake, Enrolee(login="theirs", user_id=2, repo_access=[readable(ALLOWED)])
    )

    response = await client.delete(f"/clients/{theirs_id}", headers=mine)

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["detail"] == NOT_FOUND_DETAIL
    row = await session.execute(select(ReviewClient).where(col(ReviewClient.id) == UUID(theirs_id)))
    assert row.scalar_one_or_none() is not None


async def test_revoke_nonexistent_client_returns_404_indistinguishably_and_still_audits(
    client: AsyncClient,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fiction and a stranger's machine must answer identically — but log differently.

    Guards `DELETE /clients/{id}` against becoming an existence oracle: a
    distinguishable answer would let anyone who can enrol enumerate client ids
    and count the machines every other org runs.

    The log half is the other side of that same rule, and is asserted here
    rather than assumed: the response collapse must not also erase the audit
    trail for the "id doesn't exist" branch. A hub operator reading the log is
    not a caller probing the endpoint, so `reason` may — and must — say which
    of the two happened.
    """
    fake = FakeGithubIdentityService()
    _, mine = await enrol_github_client(
        client, monkeypatch, fake, Enrolee(login="prober", user_id=1, repo_access=[readable(ALLOWED)])
    )
    theirs_id, _ = await enrol_github_client(
        client, monkeypatch, fake, Enrolee(login="theirs", user_id=2, repo_access=[readable(ALLOWED)])
    )
    missing_id = uuid4()

    with caplog.at_level(logging.DEBUG):
        never_existed = await client.delete(f"/clients/{missing_id}", headers=mine)
    real_but_forbidden = await client.delete(f"/clients/{theirs_id}", headers=mine)

    assert never_existed.status_code == HTTPStatus.NOT_FOUND
    assert never_existed.status_code == real_but_forbidden.status_code
    assert never_existed.json() == real_but_forbidden.json()
    assert never_existed.json() == {"detail": NOT_FOUND_DETAIL}

    denied = records_named(caplog, "client_revoke_denied")
    assert len(denied) == 1
    assert denied[0].__dict__["reason"] == "client_not_found"
    assert denied[0].__dict__["target_client_id"] == str(missing_id)
    assert denied[0].__dict__["github_login"] == "prober"
    assert denied[0].__dict__["github_user_id"] == 1
    # Nothing was found, so there is nothing to name. An empty or invented name
    # would read as a real client whose name happens to be blank.
    assert "target_client_name" not in denied[0].__dict__


async def test_missing_credential_returns_401(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed Authorization header gets past the presence gate but not the endpoint."""
    github_mode_without_github(monkeypatch)

    response = await client.delete(f"/clients/{uuid4()}", headers={"Authorization": "not-a-bearer"})

    assert response.status_code == HTTPStatus.UNAUTHORIZED


async def test_identityless_caller_cannot_revoke_identityless_client(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two clients with no GitHub account must not read as "the same identity".

    Rows with `identity_id IS NULL` are reachable in github mode — a client
    registered before GitHub-derived admission landed, or one carried over from
    a dev-mode deployment. A bare `caller.identity_id == target.identity_id`
    comparison answers True for `None == None` and hands every such client the
    power to revoke every other one. So the absence of an identity fails closed
    here, exactly as it already does for dispatch and for policy writes.
    """
    mine_id, mine = await register_dev_client(client, "identityless-a")
    theirs_id, _ = await register_dev_client(client, "identityless-b")
    github_mode_without_github(monkeypatch)

    response = await client.delete(f"/clients/{theirs_id}", headers=mine)

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["detail"] == NOT_FOUND_DETAIL
    for surviving in (mine_id, theirs_id):
        row = await session.execute(select(ReviewClient).where(col(ReviewClient.id) == UUID(surviving)))
        assert row.scalar_one_or_none() is not None


# ---------------------------------------------------------------------------
# Leases and history
# ---------------------------------------------------------------------------


async def test_revoke_releases_active_lease_immediately(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The queue must not wait out a TTL for a machine the hub was just told is gone.

    The second client leasing the *same* job is the assertion: `lease_next_job`
    reclaims expired leases on its way in, so a job that only came back after
    `LEASE_TTL_SECONDS` would leave this test hanging on the clock rather than
    on the revocation.
    """
    job_id = await enqueue(client, ALLOWED, "released-by-revoke")

    fake = FakeGithubIdentityService()
    holder_id, holder = await enrol_github_client(
        client, monkeypatch, fake, Enrolee(login="holder", user_id=1, repo_access=[readable(ALLOWED)])
    )
    _, successor = await enrol_github_client(
        client, monkeypatch, fake, Enrolee(login="successor", user_id=2, repo_access=[readable(ALLOWED)])
    )

    lease = await client.post("/jobs/lease", headers=holder)
    assert lease.status_code == HTTPStatus.OK
    assert lease.json()["job"]["id"] == job_id

    # The successor finds nothing while the lease is held — the control that
    # makes the re-lease below evidence of the revocation and nothing else.
    blocked = await client.post("/jobs/lease", headers=successor)
    assert blocked.status_code == HTTPStatus.OK
    assert blocked.json() is None

    revoke = await client.delete(f"/clients/{holder_id}", headers=holder)
    assert revoke.status_code == HTTPStatus.NO_CONTENT

    relet = await client.post("/jobs/lease", headers=successor)
    assert relet.status_code == HTTPStatus.OK
    assert relet.json() is not None
    assert relet.json()["job"]["id"] == job_id
    assert relet.json()["job"]["state"] == JobState.LEASED.value


async def test_revoke_exhausts_job_at_max_attempts(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Releasing a lease is not a free retry — a job out of attempts still exhausts.

    Mirrors `reclaim_expired_leases`' second branch. Requeueing unconditionally
    would let a client that revokes itself mid-round reset the attempt budget
    every time, turning a job that can never succeed into one that is dispatched
    forever.
    """
    job_id = await enqueue(client, ALLOWED, "out-of-attempts")

    fake = FakeGithubIdentityService()
    holder_id, holder = await enrol_github_client(
        client, monkeypatch, fake, Enrolee(login="last-chance", user_id=1, repo_access=[readable(ALLOWED)])
    )

    lease = await client.post("/jobs/lease", headers=holder)
    assert lease.status_code == HTTPStatus.OK
    assert lease.json()["job"]["attempts"] == 1

    # Spend the budget by lowering the ceiling onto the attempt just used —
    # cheaper than leasing and expiring twice, and it pins the same predicate.
    await session.execute(update(ReviewJob).where(col(ReviewJob.id) == UUID(job_id)).values(max_attempts=1))
    await session.commit()

    revoke = await client.delete(f"/clients/{holder_id}", headers=holder)
    assert revoke.status_code == HTTPStatus.NO_CONTENT

    assert await job_state(session, job_id) == JobState.EXHAUSTED.value


async def test_revoke_lands_in_the_log_with_caller_target_and_detached_history(
    client: AsyncClient,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The audit record names who revoked what, and what provenance it cost.

    `review_job.reported_by` is `ON DELETE SET NULL`, so a hard delete quietly
    detaches every finished round this client produced from the machine that
    produced it. The verdict itself survives on the job row and in the PR
    comment; the *attribution* does not. That is an acceptable trade — blocking
    revocation on review history would contradict "self-service, no operator
    gate" — but it must not be a silent one.
    """
    job_id = await enqueue(client, ALLOWED, "reported-then-revoked")

    fake = FakeGithubIdentityService()
    client_id, headers = await enrol_github_client(
        client,
        monkeypatch,
        fake,
        Enrolee(login="marge-bouvier", user_id=20482231, repo_access=[readable(ALLOWED)], name="marge-mac-mini"),
    )

    lease = await client.post("/jobs/lease", headers=headers)
    assert lease.status_code == HTTPStatus.OK
    report = await client.post(f"/jobs/{job_id}/report", json=REPORT_PAYLOAD, headers=headers)
    assert report.status_code == HTTPStatus.OK

    with caplog.at_level(logging.DEBUG):
        response = await client.delete(f"/clients/{client_id}", headers=headers)

    assert response.status_code == HTTPStatus.NO_CONTENT
    authorized = records_named(caplog, "client_revoke_authorized")
    assert len(authorized) == 1
    assert authorized[0].__dict__["target_client_id"] == client_id
    assert authorized[0].__dict__["target_client_name"] == "marge-mac-mini"
    assert authorized[0].__dict__["github_login"] == "marge-bouvier"
    assert authorized[0].__dict__["github_user_id"] == 20482231
    assert authorized[0].__dict__["reported_jobs_detached"] == 1
    assert authorized[0].__dict__["reported_job_ids"] == [job_id]


# ---------------------------------------------------------------------------
# dev mode — the same named bypass, and its limit
# ---------------------------------------------------------------------------


async def test_dev_mode_secret_revokes_any_client(
    client: AsyncClient,
    session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Offline the grid has no identities to match, so the shared secret stands in.

    Logged as a bypass, not merely allowed: "was dev mode used, and on what"
    has to be answerable from the log stream rather than from a config file
    read months later.
    """
    assert settings.client_enrolment_mode == "dev"
    client_id, _ = await register_dev_client(client, "dev-box")

    with caplog.at_level(logging.DEBUG):
        response = await client.delete(f"/clients/{client_id}", headers=enrolment_headers(DEV_SECRET))

    assert response.status_code == HTTPStatus.NO_CONTENT
    row = await session.execute(select(ReviewClient).where(col(ReviewClient.id) == UUID(client_id)))
    assert row.scalar_one_or_none() is None

    assert len(records_named(caplog, "dev_mode_secret_used")) == 1
    bypass = records_named(caplog, "client_revoke_dev_mode_bypass")
    assert len(bypass) == 1
    assert bypass[0].levelno == logging.WARNING
    assert bypass[0].__dict__["target_client_id"] == client_id
    assert bypass[0].__dict__["caller_identity"] == CALLER_IDENTITY_UNAVAILABLE_DEV_MODE
    assert all(DEV_SECRET not in dump(r) for r in caplog.records)


async def test_dev_mode_client_token_alone_cannot_revoke(
    client: AsyncClient,
    session: AsyncSession,
) -> None:
    """The carve-out is the enrolment secret, not "any bearer the hub minted".

    Without this, dev mode would silently be a cross-client kick: every
    registered machine could delete every other one, which is the exact
    authority D-SELFREVOKE refuses to put in the hub.
    """
    assert settings.client_enrolment_mode == "dev"
    _, mine = await register_dev_client(client, "dev-box-a")
    theirs_id, _ = await register_dev_client(client, "dev-box-b")

    response = await client.delete(f"/clients/{theirs_id}", headers=mine)

    assert response.status_code == HTTPStatus.UNAUTHORIZED
    row = await session.execute(select(ReviewClient).where(col(ReviewClient.id) == UUID(theirs_id)))
    assert row.scalar_one_or_none() is not None


async def test_dev_mode_secret_revoking_nonexistent_client_returns_404_and_logs(
    client: AsyncClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The disclosure rule and the audit trail both survive the dev-mode branch.

    The combination the github-mode cases cannot reach: a valid credential that
    skips the identity comparison entirely still has to meet a missing target
    with the same 404 and the same "we looked, there was nothing there" record.
    """
    assert settings.client_enrolment_mode == "dev"
    missing_id = uuid4()

    with caplog.at_level(logging.DEBUG):
        response = await client.delete(f"/clients/{missing_id}", headers=enrolment_headers(DEV_SECRET))

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["detail"] == NOT_FOUND_DETAIL

    denied = records_named(caplog, "client_revoke_denied")
    assert len(denied) == 1
    assert denied[0].__dict__["reason"] == "client_not_found"
    assert denied[0].__dict__["target_client_id"] == str(missing_id)
    assert denied[0].__dict__["caller_identity"] == CALLER_IDENTITY_UNAVAILABLE_DEV_MODE
    assert "target_client_name" not in denied[0].__dict__

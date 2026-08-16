"""Integration tests for `GET /events` — the SSE job-lifecycle stream (RFC 0003 A1).

The stream is a *read* surface, so it answers the same question every other read
already does: which repos can this caller reach. What makes it different is
*when* that question is asked. A job feed is filtered once per request; a stream
outlives the answer it opened with, so the access set has to be re-read per
event (D-404) and the snapshot's age re-checked on every wake (D-TTL). Both are
asserted directly below rather than inferred from the happy path:

* `test_subscriber_access_narrows_mid_stream_stops_receiving_events` narrows an
  identity's access *while the connection is open* — the case a filter captured
  at connect time would silently keep serving.
* `test_stream_closes_when_identity_access_becomes_stale` ages the snapshot and
  asserts the hub hangs up rather than streaming on against an expired
  authorization.

Every wait is bounded by `asyncio.wait_for` inside the `SseStream` helper. No
test sleeps, and no test polls.
"""

from __future__ import annotations

import asyncio
from http import HTTPStatus

import pytest
from httpx import AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from review_bingo_hub.api.events import RETRY_AFTER_SECONDS
from review_bingo_hub.core.config import settings
from review_bingo_hub.models.github_identity import IdentityRepoAccess
from review_bingo_hub.tests.integration.conftest import (
    ALLOWED,
    FORBIDDEN,
    SSE_EVENT_TIMEOUT,
    SSE_TEST_HEARTBEAT_SECONDS,
    UNRELATED,
    Enrolee,
    FakeGithubIdentityService,
    backdate_access_refreshed_at,
    enqueue,
    enrol_github_client,
    identity_id_of,
    open_sse_stream,
    readable,
    start_dashboard_session,
)

# The six keys `JobRelayedEvent` puts on the wire. Written out as a literal
# rather than derived from the model: a wire contract a client parses has to
# fail this test when it changes, not follow the change silently.
EVENT_KEYS = {"job_id", "repo_full_name", "pr_number", "head_sha", "verdict", "summary"}

SUBSCRIBER_CAP = 2


async def relay_a_round(
    client: AsyncClient,
    headers: dict[str, str],
    job_id: str,
    *,
    verdict: str = "approve",
    summary: str = "Nothing found.",
) -> None:
    """Lease and report one job through to `relayed` — the state that emits."""
    lease = await client.post(f"/jobs/{job_id}/lease", headers=headers)
    assert lease.status_code == HTTPStatus.OK

    report = await client.post(
        f"/jobs/{job_id}/report",
        json={"verdict": verdict, "summary": summary, "findings": []},
        headers=headers,
    )
    assert report.status_code == HTTPStatus.OK
    assert report.json()["state"] == "relayed"


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------


async def test_events_endpoint_requires_auth(client: AsyncClient) -> None:
    """No credential, no stream — `/events` is not on the public allowlist.

    An empty header value reproduces "absent" for `RequireTokenMiddleware`'s
    presence check without a second client fixture, matching
    `test_auth_me.py`'s precedent for the same assertion.
    """
    response = await asyncio.wait_for(
        client.get("/events", headers={"Authorization": ""}),
        timeout=SSE_EVENT_TIMEOUT,
    )

    assert response.status_code == HTTPStatus.UNAUTHORIZED


# ---------------------------------------------------------------------------
# Delivery, and the access set it is filtered by
# ---------------------------------------------------------------------------


async def test_subscriber_receives_job_relayed_for_accessible_repo(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The happy path: a relayed round on a repo the subscriber can reach arrives."""
    fake = FakeGithubIdentityService()
    _, headers = await enrol_github_client(
        client, monkeypatch, fake, Enrolee(login="watcher", user_id=1, repo_access=[readable(ALLOWED)])
    )
    job_id = await enqueue(client, ALLOWED, "sse-inside")

    async with open_sse_stream(headers) as stream:
        assert stream.status_code == HTTPStatus.OK
        assert stream.headers["content-type"].startswith("text/event-stream")

        await relay_a_round(client, headers, job_id, verdict="findings", summary="One nit.")
        event = await stream.next_event()

    assert event.name == "job.relayed"
    assert event.data == {
        "job_id": job_id,
        "repo_full_name": ALLOWED,
        "pr_number": 7,
        "head_sha": "sse-inside",
        "verdict": "findings",
        "summary": "One nit.",
    }


async def test_subscriber_does_not_receive_job_relayed_for_inaccessible_repo(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A round on a repo outside the subscriber's access set never reaches it.

    The reporter is a *second* client — one that can actually reach FORBIDDEN —
    because the subscriber could not have leased that job in the first place.
    Only the publish-path filter can explain the silence.

    `expect_no_event` passing on its timeout is the assertion here, deliberately:
    an absence is only observable by waiting for it. The window is bounded and
    short, and the positive control above proves a deliverable event lands well
    inside it.
    """
    fake = FakeGithubIdentityService()
    _, watcher_headers = await enrol_github_client(
        client, monkeypatch, fake, Enrolee(login="watcher", user_id=1, repo_access=[readable(ALLOWED)])
    )
    _, reporter_headers = await enrol_github_client(
        client, monkeypatch, fake, Enrolee(login="insider", user_id=2, repo_access=[readable(FORBIDDEN)])
    )
    job_id = await enqueue(client, FORBIDDEN, "sse-outside")

    async with open_sse_stream(watcher_headers) as stream:
        await relay_a_round(client, reporter_headers, job_id)

        await stream.expect_no_event()


async def test_no_event_published_when_relay_fails(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of R6/the AC: emission is gated on the relay succeeding.

    `report_job_endpoint` only builds and publishes a `JobRelayedEvent` inside
    `if relay_error is None:`. Forcing `relay_result` to return an error string
    (its documented failure contract — see `relay_service.relay_result`) drives
    that branch false directly, without needing a real GitHub relay failure.
    """
    fake = FakeGithubIdentityService()
    _, headers = await enrol_github_client(
        client, monkeypatch, fake, Enrolee(login="watcher", user_id=1, repo_access=[readable(ALLOWED)])
    )
    job_id = await enqueue(client, ALLOWED, "sse-relay-fails")

    async def failing_relay(_job: object) -> str:
        return "simulated relay failure"

    monkeypatch.setattr("review_bingo_hub.api.jobs.relay_result", failing_relay)

    async with open_sse_stream(headers) as stream:
        lease = await client.post(f"/jobs/{job_id}/lease", headers=headers)
        assert lease.status_code == HTTPStatus.OK

        report = await client.post(
            f"/jobs/{job_id}/report",
            json={"verdict": "approve", "summary": "Nothing found.", "findings": []},
            headers=headers,
        )
        assert report.status_code == HTTPStatus.OK
        assert report.json()["state"] == "reported"
        assert report.json()["relay_error"] == "simulated relay failure"

        await stream.expect_no_event()


async def test_subscriber_access_narrows_mid_stream_stops_receiving_events(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The D-404 assertion: filtering reads the access set as it is *now*.

    The subscriber opens with two repos and is proven to receive an event on the
    second one. Its access to that repo is then revoked while the connection
    stays open, and a further round on it must not be delivered. A filter
    captured at connect time passes the first half and fails the second.
    """
    fake = FakeGithubIdentityService()
    watcher_id, watcher_headers = await enrol_github_client(
        client,
        monkeypatch,
        fake,
        Enrolee(login="narrowing", user_id=1, repo_access=[readable(ALLOWED), readable(UNRELATED)]),
    )
    _, reporter_headers = await enrol_github_client(
        client, monkeypatch, fake, Enrolee(login="reporter", user_id=2, repo_access=[readable(UNRELATED)])
    )
    identity_id = await identity_id_of(session, watcher_id)
    first_job = await enqueue(client, UNRELATED, "before-narrowing")
    second_job = await enqueue(client, UNRELATED, "after-narrowing", number=8)

    async with open_sse_stream(watcher_headers) as stream:
        await relay_a_round(client, reporter_headers, first_job)
        delivered = await stream.next_event()
        assert delivered.data["job_id"] == first_job

        # The account loses the repo mid-stream, exactly as
        # `test_report_succeeds_after_access_narrows_post_lease` simulates it:
        # drop the IdentityRepoAccess row directly, ahead of re-attestation.
        await session.execute(
            delete(IdentityRepoAccess).where(
                col(IdentityRepoAccess.identity_id) == identity_id,
                col(IdentityRepoAccess.repo_full_name) == UNRELATED,
            )
        )
        await session.commit()

        await relay_a_round(client, reporter_headers, second_job)
        await stream.expect_no_event()


async def test_stream_closes_when_identity_access_becomes_stale(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The D-TTL assertion: an aged snapshot ends the stream instead of outliving it.

    Closing rather than filtering is the point — the caller reconnects, and a
    reconnect is what makes it re-present a credential. A stream that quietly
    stayed open would be an authorization with no expiry at all.
    """
    monkeypatch.setattr(settings, "sse_heartbeat_seconds", SSE_TEST_HEARTBEAT_SECONDS)
    fake = FakeGithubIdentityService()
    watcher_id, headers = await enrol_github_client(
        client, monkeypatch, fake, Enrolee(login="ager", user_id=1, repo_access=[readable(ALLOWED)])
    )

    async with open_sse_stream(headers) as stream:
        await backdate_access_refreshed_at(
            session,
            await identity_id_of(session, watcher_id),
            seconds_ago=settings.identity_access_ttl_seconds * 2,
        )

        await stream.wait_closed()


async def test_dashboard_session_subscriber_receives_events_for_own_access_set(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A person's browser session subscribes too, and is scoped the same way.

    A dashboard session has no client row (`client_id is None`), so everything
    the stream decides — the access filter and the staleness check — has to key
    on the identity alone. This is the case `identity_access_is_stale` could not
    have served.
    """
    fake = FakeGithubIdentityService()
    _, reporter_headers = await enrol_github_client(
        client, monkeypatch, fake, Enrolee(login="marge-bouvier", user_id=1, repo_access=[readable(ALLOWED)])
    )
    session_headers = await start_dashboard_session(
        client, monkeypatch, fake, Enrolee(login="marge-bouvier", user_id=1, repo_access=[readable(ALLOWED)])
    )
    job_id = await enqueue(client, ALLOWED, "sse-dashboard")

    async with open_sse_stream(session_headers) as stream:
        assert stream.status_code == HTTPStatus.OK
        await relay_a_round(client, reporter_headers, job_id)
        event = await stream.next_event()

    assert event.name == "job.relayed"
    assert event.data["job_id"] == job_id
    assert event.data["repo_full_name"] == ALLOWED


async def test_event_payload_matches_proposed_shape(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wire contract, asserted as an exact key set.

    `findings` is deliberately absent: the stream is a notification that a round
    landed, not a second delivery channel for its contents. An extra key here
    would be a contract change a client cannot see coming, so the assertion is
    equality rather than a subset check.
    """
    fake = FakeGithubIdentityService()
    _, headers = await enrol_github_client(
        client, monkeypatch, fake, Enrolee(login="shape", user_id=1, repo_access=[readable(ALLOWED)])
    )
    job_id = await enqueue(client, ALLOWED, "sse-shape")

    async with open_sse_stream(headers) as stream:
        await relay_a_round(client, headers, job_id)
        event = await stream.next_event()

    assert set(event.data) == EVENT_KEYS


# ---------------------------------------------------------------------------
# Fan-out bound
# ---------------------------------------------------------------------------


async def test_events_endpoint_returns_503_when_subscriber_cap_reached(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past the cap the hub refuses new connections — and keeps the old ones.

    503 rather than 429 or a silent accept: the caller should retry, and a
    subscriber accepted past the bound would be one the publish path fans out to
    on the request-serving write path.
    """
    monkeypatch.setattr(settings, "max_sse_subscribers", SUBSCRIBER_CAP)
    fake = FakeGithubIdentityService()
    _, headers = await enrol_github_client(
        client, monkeypatch, fake, Enrolee(login="crowd", user_id=1, repo_access=[readable(ALLOWED)])
    )
    job_id = await enqueue(client, ALLOWED, "sse-capped")

    async with open_sse_stream(headers) as first, open_sse_stream(headers) as second:
        assert first.status_code == HTTPStatus.OK
        assert second.status_code == HTTPStatus.OK

        refused = await asyncio.wait_for(client.get("/events", headers=headers), timeout=SSE_EVENT_TIMEOUT)
        assert refused.status_code == HTTPStatus.SERVICE_UNAVAILABLE
        assert refused.headers["retry-after"] == str(RETRY_AFTER_SECONDS)

        # The refusal is not allowed to cost the connections already accepted.
        await relay_a_round(client, headers, job_id)
        assert (await first.next_event()).data["job_id"] == job_id
        assert (await second.next_event()).data["job_id"] == job_id

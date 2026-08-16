"""Unit tests for `EventBus` — the process-local fan-out behind `GET /events`.

Session-free on purpose: everything asserted here is about what the bus does
when a *subscriber* misbehaves, and the two ways a subscriber can are a queue
nobody is draining and an access check that fails. Both have to end the same
way — that one subscriber loses the event, and nothing else does — because
`publish` runs inside `report_job_endpoint` after its commit has already
succeeded. Anything that escapes here becomes a 500 on a request whose work is
already durable.
"""

from __future__ import annotations

import logging
from typing import cast
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from review_bingo_hub.core import event_bus as event_bus_module
from review_bingo_hub.core.config import settings
from review_bingo_hub.core.event_bus import EventBus, EventBusFullError
from review_bingo_hub.models.event import JobRelayedEvent

REPO = "acme/payments"
OTHER_REPO = "acme/other-repo"
SUBSCRIBER_CAP = 2


def an_event(repo: str = REPO) -> JobRelayedEvent:
    return JobRelayedEvent(
        job_id=uuid4(),
        repo_full_name=repo,
        pr_number=7,
        head_sha="deadbeef",
        verdict="approve",
        summary="Nothing found.",
    )


def a_session() -> AsyncSession:
    """A stand-in session: every access check in these tests is patched out."""
    return cast(AsyncSession, MagicMock(spec=AsyncSession))


def reachable(monkeypatch: pytest.MonkeyPatch, answers: dict[UUID | None, frozenset[str] | None]) -> None:
    """Script `accessible_repo_names` per identity, without a database."""

    async def fake_accessible_repo_names(_session: AsyncSession, identity_id: UUID | None) -> frozenset[str] | None:
        return answers[identity_id]

    monkeypatch.setattr(event_bus_module, "accessible_repo_names", fake_accessible_repo_names)


async def test_publish_delivers_to_every_subscriber_that_can_reach_the_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The positive control: a bus that dropped everything would pass the rest."""
    inside, outside = uuid4(), uuid4()
    reachable(monkeypatch, {inside: frozenset({REPO}), outside: frozenset({OTHER_REPO})})
    bus = EventBus()
    in_scope = bus.subscribe(inside)
    out_of_scope = bus.subscribe(outside)
    event = an_event()

    await bus.publish(a_session(), event)

    assert in_scope.queue.get_nowait() == event
    assert out_of_scope.queue.empty()


async def test_publish_drops_the_event_when_a_subscribers_queue_is_full(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A subscriber that stopped reading loses events; nobody else does.

    Dropping is the deliberate choice over blocking: `publish` runs on the
    reporting client's request, so waiting on a full queue would make one
    stalled dashboard tab slow down the grid.
    """
    stalled, healthy = uuid4(), uuid4()
    reachable(monkeypatch, {stalled: None, healthy: None})
    bus = EventBus(queue_maxsize=1)
    stalled_sub = bus.subscribe(stalled)
    healthy_sub = bus.subscribe(healthy)
    stalled_sub.queue.put_nowait(an_event())

    with caplog.at_level(logging.WARNING):
        await bus.publish(a_session(), an_event())

    assert stalled_sub.queue.qsize() == 1
    assert healthy_sub.queue.qsize() == 1
    dropped = [r for r in caplog.records if r.getMessage() == "event_dropped"]
    assert len(dropped) == 1
    # `extra=` fields land on the record's __dict__, which is where the suite
    # already reads them (`dump` in the integration conftest).
    assert dropped[0].__dict__["identity_id"] == str(stalled)
    assert dropped[0].__dict__["repo_full_name"] == REPO
    assert dropped[0].__dict__["queue_maxsize"] == 1


async def test_publish_drops_a_subscriber_whose_access_check_raises(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing access check costs that subscriber its event and nothing more.

    The access check is a database call, so it can fail for reasons that have
    nothing to do with the subscriber. Letting it out of `publish` would turn a
    transient blip into a 500 on a report the hub has already committed and
    relayed.
    """
    broken, healthy = uuid4(), uuid4()

    async def fake_accessible_repo_names(_session: AsyncSession, identity_id: UUID | None) -> frozenset[str] | None:
        if identity_id == broken:
            error_msg = "connection reset"
            raise RuntimeError(error_msg)
        return None

    monkeypatch.setattr(event_bus_module, "accessible_repo_names", fake_accessible_repo_names)
    bus = EventBus()
    broken_sub = bus.subscribe(broken)
    healthy_sub = bus.subscribe(healthy)

    with caplog.at_level(logging.WARNING):
        await bus.publish(a_session(), an_event())

    assert broken_sub.queue.empty()
    assert healthy_sub.queue.qsize() == 1
    assert [r for r in caplog.records if r.getMessage() == "event_dropped"]


async def test_unsubscribed_subscriber_receives_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unsubscribing really unregisters — a closed stream must not accrue a queue."""
    identity = uuid4()
    reachable(monkeypatch, {identity: None})
    bus = EventBus()
    subscription = bus.subscribe(identity)

    bus.unsubscribe(subscription)
    await bus.publish(a_session(), an_event())

    assert subscription.queue.empty()
    assert bus.subscriber_count == 0


async def test_unsubscribe_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Closing twice is not an error.

    The stream's own `finally` runs on disconnect, on staleness, and on
    cancellation, and nothing guarantees exactly one of those wins.
    """
    identity = uuid4()
    reachable(monkeypatch, {identity: None})
    bus = EventBus()
    subscription = bus.subscribe(identity)

    bus.unsubscribe(subscription)
    bus.unsubscribe(subscription)

    assert bus.subscriber_count == 0


async def test_subscribe_refuses_past_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fan-out bound is enforced where connections are accepted.

    `publish` walks every subscriber sequentially on the reporting request, so
    the subscriber count is the length of that walk. Capping it is what keeps
    that cost bounded.
    """
    monkeypatch.setattr(settings, "max_sse_subscribers", SUBSCRIBER_CAP)
    bus = EventBus()
    for _ in range(SUBSCRIBER_CAP):
        bus.subscribe(uuid4())

    with pytest.raises(EventBusFullError):
        bus.subscribe(uuid4())

    assert bus.subscriber_count == SUBSCRIBER_CAP


async def test_subscribe_admits_again_once_a_subscriber_leaves(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cap is a live count, not a high-water mark."""
    monkeypatch.setattr(settings, "max_sse_subscribers", SUBSCRIBER_CAP)
    bus = EventBus()
    first = bus.subscribe(uuid4())
    bus.subscribe(uuid4())

    bus.unsubscribe(first)

    assert bus.subscribe(uuid4()) is not None

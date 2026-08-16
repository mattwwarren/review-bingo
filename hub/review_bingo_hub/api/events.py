"""Server-sent events: one long-lived connection per dashboard, per client.

Hand-rolled rather than reached for from a library (RFC 0003 A1 R3). SSE is
`data: ...\\n\\n` over a text/event-stream response — a dependency to produce
that would be a dependency to audit, and the framing is three lines.

Two things this generator has to do itself, both because of the middleware
underneath it. `LoggingMiddleware` and `RequireTokenMiddleware` are
`BaseHTTPMiddleware` subclasses, which do not propagate an ASGI client
disconnect down into the response body they are wrapping — so the loop polls
`request.is_disconnected()` rather than waiting to be cancelled. And nothing
re-checks authorization for a connection that is already open, so the loop
re-checks it: the access set per event (in `EventBus.publish`) and the snapshot's
age per wake (here). Both were decided once, at connect, in the version of this
that would have been wrong.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from review_bingo_hub.api.clients import ScopedCallerDep
from review_bingo_hub.core.config import settings
from review_bingo_hub.core.event_bus import EventBus, EventBusFullError, Subscription
from review_bingo_hub.core.logging import get_logging_context
from review_bingo_hub.models.event import EventType, JobRelayedEvent
from review_bingo_hub.services.identity_service import identity_id_access_is_stale

LOGGER = logging.getLogger(__name__)

router = APIRouter(tags=["events"])

# A comment line, not an event: it keeps proxies and idle timeouts from
# reclaiming a quiet connection without a subscriber having to filter it out.
HEARTBEAT = ": ping\n\n"

DETAIL_TOO_MANY_SUBSCRIBERS = "Too many open event streams; retry shortly"

# A short, fixed backoff hint rather than something tied to the heartbeat
# cadence: the cap frees up as soon as any one of up to `max_sse_subscribers`
# streams closes, which happens far more often than once per heartbeat.
RETRY_AFTER_SECONDS = 5


def _frame(event: JobRelayedEvent) -> str:
    """One SSE frame: the event's name, then its JSON payload."""
    return f"event: {EventType.JOB_RELAYED}\ndata: {event.model_dump_json()}\n\n"


async def _access_is_stale(request: Request, subscription: Subscription) -> bool:
    """Ask the staleness question on a session that lives no longer than the question.

    Deliberately *not* a `SessionDep` on the endpoint. A dependency-supplied
    session is released only when the response completes, and this response is
    designed never to complete on its own — so every open stream would hold a
    pooled connection for its entire lifetime, and `max_sse_subscribers` (200)
    would exhaust `db_pool_size + db_max_overflow` (15) at the sixteenth
    dashboard tab, blocking the whole hub rather than just the stream.

    Reaching for `app.state.async_session_maker` directly is the existing way
    out of that, already used where work happens outside a request's own
    lifecycle (`core/activity_logging.py`, and the test suite's own middleware).
    """
    async with request.app.state.async_session_maker() as session:
        stale: bool = await identity_id_access_is_stale(session, subscription.identity_id)
    return stale


async def _event_stream(
    request: Request,
    bus: EventBus,
    subscription: Subscription,
) -> AsyncIterator[str]:
    """Yield frames until the caller hangs up or its authorization ages out.

    The wait is bounded by the heartbeat rather than left open-ended, which is
    what makes both exit conditions reachable: a connection with no traffic on
    it still wakes on schedule to ask whether it should still exist.
    """
    log_extra = {
        **get_logging_context(),
        "identity_id": str(subscription.identity_id) if subscription.identity_id else None,
    }
    try:
        while True:
            if await request.is_disconnected():
                return
            if await _access_is_stale(request, subscription):
                # Closed, not filtered: the caller reconnects, and reconnecting
                # is what makes it present a credential the hub can re-read.
                LOGGER.info("event_stream_closed_stale", extra=log_extra)
                return
            try:
                event = await asyncio.wait_for(subscription.queue.get(), timeout=settings.sse_heartbeat_seconds)
            except TimeoutError:
                yield HEARTBEAT
                continue
            yield _frame(event)
    finally:
        bus.unsubscribe(subscription)


@router.get("/events")
async def events_endpoint(request: Request, caller: ScopedCallerDep) -> StreamingResponse:
    """Stream job-lifecycle events (SSE) for repos this caller can reach; access is re-checked on every event."""
    bus: EventBus = request.app.state.event_bus
    try:
        subscription = bus.subscribe(caller.identity_id)
    except EventBusFullError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=DETAIL_TOO_MANY_SUBSCRIBERS,
            headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
        ) from exc

    return StreamingResponse(
        _event_stream(request, bus, subscription),
        media_type="text/event-stream",
    )

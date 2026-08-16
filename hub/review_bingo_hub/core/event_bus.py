"""In-process fan-out from the report path to whatever streams are listening.

**Process-local by construction, and that is the design, not an oversight.**
Subscribers live in a list on one `EventBus` instance held on `app.state`, so a
hub running more than one worker would deliver an event only to the streams
attached to the worker that happened to serve the report. Single-worker
deployment is therefore a *requirement* of RFC 0003 A1, not an incidental
property of it. A broker (Redis pub/sub, Postgres LISTEN/NOTIFY) is what lifts
that, and is deliberately out of scope until there is a second worker to
justify one — the alternative was importing a dependency to solve a problem the
deployment does not have yet.

Fan-out is sequential: one subscriber at a time, each with its own access
check, on the reporting client's own request. That cost is bounded by
`settings.max_sse_subscribers`, enforced in `subscribe` — the bound is the
whole reason the sequential walk is safe to reason about, so the two travel
together and must not drift apart.

`publish` never raises. It runs inside `report_job_endpoint` *after* that
endpoint has committed the report and relayed it to the PR, so an exception
escaping here would answer 500 to a client whose finished work is already
durable and already posted. Every per-subscriber failure — a queue nobody is
draining, a database blip in the access check — is logged and skipped, the same
posture `relay_service.relay_result` takes around its whole attempt.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from review_bingo_hub.core.config import settings
from review_bingo_hub.core.logging import get_logging_context
from review_bingo_hub.models.event import JobRelayedEvent
from review_bingo_hub.services.identity_service import accessible_repo_names

LOGGER = logging.getLogger(__name__)

# How many undelivered events one stream may fall behind by before the oldest
# start being dropped. Bounded rather than unbounded because the producer is a
# request handler: an unbounded queue would let one stalled reader turn the
# hub's memory into its backlog.
DEFAULT_QUEUE_MAXSIZE = 100


class EventBusFullError(RuntimeError):
    """The subscriber cap is reached, so this connection cannot be accepted.

    A domain error rather than an `HTTPException`, following the convention
    `identity_service` documents: this module has no HTTP layer of its own, so
    `api/events.py` maps it to 503 at the boundary.
    """

    def __init__(self, cap: int) -> None:
        self.cap = cap
        super().__init__(f"Event subscriber limit reached ({cap})")


@dataclass(frozen=True, eq=False)
class Subscription:
    """One open stream: who it belongs to, and where its events queue up.

    Keyed on `identity_id`, never on a client id: a dashboard session has no
    client row at all, and everything downstream — the access filter here, the
    staleness check in the stream — already keys on the identity.

    Compared by object identity (`eq=False`) rather than by field, so two
    connections from the same account are two subscriptions and unsubscribing
    one cannot remove the other.
    """

    identity_id: UUID | None
    queue: asyncio.Queue[JobRelayedEvent]


class EventBus:
    """Process-local publish/subscribe for job-lifecycle events."""

    def __init__(self, *, queue_maxsize: int = DEFAULT_QUEUE_MAXSIZE) -> None:
        self._subscribers: list[Subscription] = []
        self._queue_maxsize = queue_maxsize

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self, identity_id: UUID | None) -> Subscription:
        """Register a new stream, or refuse it if the bus is at capacity.

        Raises:
            EventBusFullError: `settings.max_sse_subscribers` is already
                reached. Refusing at the door is the point: a subscriber
                admitted past the bound is one every subsequent report pays for.
        """
        cap = settings.max_sse_subscribers
        if len(self._subscribers) >= cap:
            LOGGER.warning(
                "event_subscribe_refused",
                extra={
                    **get_logging_context(),
                    "identity_id": str(identity_id) if identity_id else None,
                    "max_sse_subscribers": cap,
                },
            )
            raise EventBusFullError(cap)

        subscription = Subscription(
            identity_id=identity_id,
            queue=asyncio.Queue(maxsize=self._queue_maxsize),
        )
        self._subscribers.append(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        """Drop a stream. Idempotent — the stream's own cleanup has several ways to fire."""
        with contextlib.suppress(ValueError):
            self._subscribers.remove(subscription)

    async def publish(self, session: AsyncSession, event: JobRelayedEvent) -> None:
        """Offer an event to every subscriber that can currently reach its repo.

        "Currently" is the load-bearing word (RFC 0001 D-404): the access set is
        re-read per subscriber, per event, through the same
        `accessible_repo_names` that dispatch and the job feed use. A set
        captured when the connection opened would keep serving a repo the
        account lost access to hours ago, and nothing would ever notice.

        Takes the emitting endpoint's session rather than opening one of its
        own: `job.relayed` is always published from `report_job_endpoint`, which
        is already holding a live session, and a second session here would be a
        second transaction reading around the first.

        Never raises. See the module docstring for why that is a contract rather
        than defensiveness.
        """
        for subscription in list(self._subscribers):
            try:
                reachable = await accessible_repo_names(session, subscription.identity_id)
                # None means "scoping is inert" (dev mode), not "reaches nothing".
                if reachable is not None and event.repo_full_name not in reachable:
                    continue
                subscription.queue.put_nowait(event)
            except Exception:
                LOGGER.warning(
                    "event_dropped",
                    extra={
                        **get_logging_context(),
                        "identity_id": str(subscription.identity_id) if subscription.identity_id else None,
                        "repo_full_name": event.repo_full_name,
                        "job_id": str(event.job_id),
                        "queue_maxsize": self._queue_maxsize,
                    },
                    exc_info=True,
                )

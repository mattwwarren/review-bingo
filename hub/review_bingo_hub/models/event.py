"""What the hub pushes to a subscriber, and what it is called on the wire.

Nothing here is persisted. `review_job` already records everything an event
carries, and a second copy of it in a table would be a second answer to "what
happened to this job" that could disagree with the first. An event is a
*notification that the row changed*, with just enough of the row inline to save
the subscriber a round trip.

Declared here rather than in `api/events.py` for the reason every other
lifecycle type in `models/` is: the shape a client parses is a contract, and
contracts live beside the model they describe, not inside the HTTP layer that
happens to be the first thing to serialize one.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from sqlmodel import SQLModel


class EventType(StrEnum):
    """The `event:` names `GET /events` puts on the wire.

    One member today. It exists as an enum rather than a literal because the
    name is the thing a subscriber switches on — RFC 0003 A2/A3 add siblings,
    and a stream that spelled its first event type inline would have nowhere
    for the second one to be discovered from.
    """

    JOB_RELAYED = "job.relayed"


class JobRelayedEvent(SQLModel):
    """A review round that reached the PR, as a subscriber sees it.

    A non-table `SQLModel`, the same wire-only shape `ReviewJobLease` and
    `ReviewJobReport` already use.

    `findings` is deliberately absent. The stream says a round landed; the
    round's contents are `GET /jobs/{id}`'s answer, and duplicating an
    unbounded, client-shaped blob into every open connection would make the
    fan-out cost of one report a function of how much the client had to say.
    """

    job_id: UUID
    repo_full_name: str
    pr_number: int
    head_sha: str
    verdict: str | None
    summary: str | None

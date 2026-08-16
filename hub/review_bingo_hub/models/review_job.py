"""Review jobs: one unit of dispatchable review work per PR head.

Lifecycle:

    queued ──lease──> leased ──report──> reported ──relay──> relayed
      ^                 │                                      │
      └──lease expiry───┘                              (relay_error set
                                                        on best-effort
                                                        relay failure)

Leases are reclaimed lazily: every lease request first requeues jobs whose
lease expired, so no background worker is needed. `attempts` counts leases
handed out; dispatch stops offering a job after `max_attempts`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar
from uuid import UUID

import sqlalchemy as sa
from pydantic import ConfigDict
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from review_bingo_hub.models.base import TimestampedTable
from review_bingo_hub.models.review_client import ModelTier


class JobState(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    REPORTED = "reported"
    RELAYED = "relayed"
    EXHAUSTED = "exhausted"
    # Distinct from EXHAUSTED: the work became moot (its PR closed), it did not
    # run out of attempts. Collapsing the two would hide dispatch failures among
    # ordinary merges.
    CANCELLED = "cancelled"


class ReviewJobBase(SQLModel):
    """PR reference a job reviews."""

    repo_full_name: str = Field(index=True, description="owner/repo")
    pr_number: int
    head_sha: str = Field(description="Commit the review round is pinned to")
    pr_title: str | None = Field(default=None)
    event_action: str = Field(description="Webhook action that spawned the job (opened, synchronize, ...)")


class ReviewJob(TimestampedTable, ReviewJobBase, table=True):
    __tablename__ = "review_job"

    state: JobState = Field(default=JobState.QUEUED, sa_type=sa.String(), index=True)
    min_tier: ModelTier = Field(
        default=ModelTier.EXPERIMENTAL,
        sa_type=sa.String(),
        description="Policy floor snapshotted from RepoPolicy at job creation",
    )
    leased_by: UUID | None = Field(
        default=None,
        sa_column=sa.Column(
            sa.UUID(as_uuid=True),
            sa.ForeignKey("review_client.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    requested_strategies: list[str] = Field(
        default_factory=list,
        sa_column=sa.Column(JSONB(), server_default="[]", nullable=False),
        description="Review strategies this job accepts; empty means any client's offered strategies match",
    )
    lease_expires_at: datetime | None = Field(default=None, sa_type=sa.DateTime(timezone=True))
    attempts: int = Field(default=0, description="Number of leases handed out")
    max_attempts: int = Field(default=3)

    # Result, populated by the leaseholder's report
    verdict: str | None = Field(default=None, description="Client's overall verdict (e.g. 'approve', 'findings')")
    summary: str | None = Field(default=None, description="Markdown summary relayed to the PR")
    findings: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=sa.Column(JSONB(), server_default="[]", nullable=False),
        description="Structured findings, shape owned by the client",
    )
    reported_by: UUID | None = Field(
        default=None,
        sa_column=sa.Column(
            sa.UUID(as_uuid=True),
            sa.ForeignKey("review_client.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    relay_error: str | None = Field(default=None, description="Set when posting back to the PR failed")

    __table_args__ = (sa.Index("ix_review_job_repo_pr", "repo_full_name", "pr_number"),)


class ReviewJobRead(ReviewJobBase):
    id: UUID
    state: JobState
    min_tier: ModelTier
    requested_strategies: list[str]
    leased_by: UUID | None
    lease_expires_at: datetime | None
    attempts: int
    verdict: str | None
    summary: str | None
    findings: list[dict[str, Any]]
    relay_error: str | None
    created_at: datetime
    updated_at: datetime

    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)  # type: ignore[assignment]


@dataclass
class ReviewJobFilters:
    """Optional narrowing of the job feed — one object, not four parameters.

    Grouped because they travel together everywhere: `GET /jobs` takes them as
    query parameters (FastAPI reads this dataclass's fields directly) and hands
    the same object to `list_jobs`. Access scoping is deliberately *not* a field
    here — it is derived from the caller, never supplied by one.
    """

    state: JobState | None = None
    repo_full_name: str | None = None
    offset: int = 0
    limit: int = 100


class ReviewJobLease(SQLModel):
    """Lease response: the job plus when the lease expires."""

    job: ReviewJobRead
    lease_expires_at: datetime


class ReviewJobReport(SQLModel):
    """Payload a leaseholder submits when its round completes."""

    verdict: str = Field(description="Overall verdict, e.g. 'approve' or 'findings'")
    summary: str = Field(description="Markdown summary; relayed to the PR verbatim")
    findings: list[dict[str, Any]] = Field(default_factory=list)

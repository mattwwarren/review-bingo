"""Per-repo dispatch policy: the one review-config knob the hub owns.

The hub deliberately knows nothing about prompts, depth, or what "perfect"
means — those are client-side decisions. The exception is the model floor:
a repo owner can require a minimum capability tier so experimental configs
never touch sensitive code. See PITCH.md ("minimum viable models").
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar
from uuid import UUID

import sqlalchemy as sa
from pydantic import ConfigDict
from sqlmodel import Field, SQLModel

from review_bingo_hub.models.base import TimestampedTable
from review_bingo_hub.models.review_client import ModelTier


class RepoPolicyBase(SQLModel):
    min_tier: ModelTier = Field(
        default=ModelTier.EXPERIMENTAL,
        sa_type=sa.String(),
        description="Minimum client tier allowed to lease this repo's jobs",
    )
    enabled: bool = Field(default=True, description="Disabled repos accept webhooks but queue no jobs")


class RepoPolicy(TimestampedTable, RepoPolicyBase, table=True):
    __tablename__ = "repo_policy"

    repo_full_name: str = Field(index=True, unique=True, description="owner/repo")


class RepoPolicyUpsert(RepoPolicyBase):
    """PUT payload; repo name comes from the path."""


class RepoPolicyRead(RepoPolicyBase):
    id: UUID
    repo_full_name: str
    created_at: datetime
    updated_at: datetime

    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)  # type: ignore[assignment]

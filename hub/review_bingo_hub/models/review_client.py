"""Review client registry: the machines that plug into the grid.

A client is any process with spare compute — a Mac mini running a quantized
local model, a CI box with API tokens, a teammate's workstation. Clients
register once (receiving a bearer token), then check in when they have
compute to offer and check out when they need it back.

Capability declarations (model, provider, quant, tier) are matched against
per-repo policy floors at dispatch. The hub never inspects prompts or review
config — capabilities exist solely for policy gating.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import ClassVar
from uuid import UUID

import sqlalchemy as sa
from pydantic import ConfigDict
from sqlmodel import Field, SQLModel

from review_bingo_hub.models.base import TimestampedTable


class ModelTier(StrEnum):
    """Ordered capability tiers for policy floors.

    Repo policies declare the minimum tier allowed to review their PRs
    ("no experimental model configs on banking PRs"). Ordering lives in
    MODEL_TIER_RANK, not the enum, so the DB stores readable strings.
    """

    EXPERIMENTAL = "experimental"
    STANDARD = "standard"
    FRONTIER = "frontier"


MODEL_TIER_RANK: dict[ModelTier, int] = {
    ModelTier.EXPERIMENTAL: 0,
    ModelTier.STANDARD: 1,
    ModelTier.FRONTIER: 2,
}


def tiers_at_or_below(tier: ModelTier) -> list[ModelTier]:
    """Tiers a client of the given tier is allowed to serve.

    A frontier client clears every floor; an experimental client only
    clears repos with no floor above experimental.
    """
    rank = MODEL_TIER_RANK[tier]
    return [t for t, r in MODEL_TIER_RANK.items() if r <= rank]


class ClientStatus(StrEnum):
    CHECKED_IN = "checked_in"
    CHECKED_OUT = "checked_out"


class ReviewClientBase(SQLModel):
    """Declared identity and capabilities of a grid client."""

    name: str = Field(index=True, description="Human-readable client name (e.g. 'marge-mac-mini')")
    model_name: str = Field(description="Model the client reviews with (e.g. 'qwen2.5-coder-32b')")
    provider: str = Field(description="Where the model runs (e.g. 'ollama', 'anthropic', 'vllm')")
    quant: str | None = Field(default=None, description="Quantization, if any (e.g. 'q4_K_M')")
    tier: ModelTier = Field(
        default=ModelTier.STANDARD,
        sa_type=sa.String(),
        description="Self-declared capability tier, matched against repo policy floors",
    )


class ReviewClient(TimestampedTable, ReviewClientBase, table=True):
    """Registered grid client.

    Auth: the hub mints an opaque bearer token at registration and stores
    only its SHA-256 hex digest. The plaintext token is returned exactly
    once in the registration response.
    """

    __tablename__ = "review_client"

    token_hash: str = Field(index=True, unique=True, description="SHA-256 hex digest of the client's bearer token")
    identity_id: UUID | None = Field(
        default=None,
        sa_column=sa.Column(
            sa.UUID(as_uuid=True),
            sa.ForeignKey("github_identity.id", ondelete="SET NULL"),
            nullable=True,
        ),
        description="GitHub account this client enrolled under; NULL under dev-mode enrolment",
    )
    status: ClientStatus = Field(
        default=ClientStatus.CHECKED_OUT,
        sa_type=sa.String(),
        index=True,
    )
    last_seen_at: datetime | None = Field(default=None, sa_type=sa.DateTime(timezone=True))


class ReviewClientCreate(ReviewClientBase):
    """Registration payload."""


class CheckInRequest(SQLModel):
    """Optional body for POST /clients/check-in.

    Wholly optional, and empty by default, because check-in's original job —
    "I have compute, plug me in" — needs no body at all and must keep working
    without one. The token is the opt-in half: it turns the same heartbeat into
    a re-attestation of the caller's GitHub repo access. Spent once and never
    persisted, exactly as at enrolment.
    """

    github_token: str | None = Field(
        default=None,
        description=(
            "Fresh GitHub user token to re-attest repo access during check-in; omitted or falsy "
            "leaves check-in a plain heartbeat"
        ),
    )


class ReviewClientRead(ReviewClientBase):
    """Public view of a client — never includes the token hash."""

    id: UUID
    status: ClientStatus
    identity_id: UUID | None
    last_seen_at: datetime | None
    created_at: datetime

    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)  # type: ignore[assignment]


class ReviewClientRegistered(SQLModel):
    """Registration response: the one and only time the token is shown."""

    client: ReviewClientRead
    token: str

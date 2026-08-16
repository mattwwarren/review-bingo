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
from pydantic import ConfigDict, field_validator
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from review_bingo_hub.models.base import TimestampedTable
from review_bingo_hub.models.review_strategy import validate_strategies


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
    runtime_identity: str | None = Field(
        default=None,
        description=(
            "Self-declared runtime this client reviews from (Hermes, Claude Code, Codex, an "
            "ollama wrapper, ...). On the base rather than on CheckInRequest because it is a "
            "static property of the registered process, the same kind of thing as model and "
            "provider — not something a heartbeat renegotiates. Nothing enforces that "
            "immutability beyond there being no client-update endpoint to change it through, and "
            "nothing gates on it yet: it is declared capability metadata, not a fourth dispatch "
            "gate. Optional, so a client that never mentions it still registers"
        ),
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
    offered_strategies: list[str] = Field(
        default_factory=list,
        sa_column=sa.Column(JSONB(), server_default="[]", nullable=False),
        description="Review strategies this client is willing to run; matched against a job's "
        "requested_strategies at lease time",
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
    offered_strategies: list[str] | None = Field(
        default=None,
        description=(
            "Review strategies this client is willing to run; omitted leaves offered_strategies "
            "unchanged, an explicit empty list clears it"
        ),
    )

    @field_validator("offered_strategies")
    @classmethod
    def check_offered_strategies(cls, value: list[str] | None) -> list[str] | None:
        """Reject an out-of-vocabulary strategy at the door, leaving omission alone.

        None is not "no strategies" here — it is "I did not mention them", and
        the endpoint reads it as leave-unchanged. Only a list that actually
        arrived is a declaration worth validating.
        """
        if value is not None:
            validate_strategies(value)
        return value


class ReviewClientRead(ReviewClientBase):
    """Public view of a client — never includes the token hash."""

    id: UUID
    status: ClientStatus
    identity_id: UUID | None
    offered_strategies: list[str]
    last_seen_at: datetime | None
    created_at: datetime

    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)  # type: ignore[assignment]


class ReviewClientCheckInRead(ReviewClientRead):
    """Check-in's response: the client, plus how long its attestation is good for.

    Deliberately a check-in-only subclass rather than a field on the shared
    ReviewClientRead. The TTL answers "when should I come back", and check-in's
    caller is the only one asking: a client that must stay attested unattended
    needs a cadence, and the only honest source of one is the hub that enforces
    the deadline. Registration and the dashboard roster gain nothing from it —
    on the roster it would be the same configured number repeated once per row.

    Widening the shared model later is trivial if a real consumer appears;
    un-widening it after a dashboard starts reading it is not.
    """

    identity_access_ttl_seconds: int = Field(
        description=(
            "How long, in seconds, a fresh attestation stays valid for leasing. Clients that "
            "re-attest unattended schedule off this rather than hardcoding the hub's TTL"
        )
    )


class ReviewClientRosterRead(ReviewClientRead):
    """The roster's response: the client, plus whose it is and how long it stays admitted.

    A roster-only subclass for the same reason `ReviewClientCheckInRead` is a
    check-in-only one — and the sibling to compare it against. These four fields
    are answers to "which of these machines are mine, and which are about to go
    dark", which is a question only a roster's caller is asking: registration
    returns one client the caller just created, and check-in returns the caller
    itself, so neither has an ownership comparison to make.

    Note what is *not* here: `identity_access_ttl_seconds`. That constant is
    check-in's, and on a roster it would be the same configured number repeated
    once per row — `test_only_check_in_gained_the_ttl_field` pins its absence.
    `access_expires_at` is the per-row form instead: an absolute deadline the
    dashboard can count down from without knowing the hub's configuration, and
    one that stays correct for a row whose attestation is older than its
    neighbours'.
    """

    is_own: bool = Field(
        description=(
            "Whether this client enrolled under the calling account's GitHub identity. False for "
            "every dev-mode row, where no identity exists on either side and equality would "
            "otherwise read as ownership"
        )
    )
    access_refreshed_at: datetime | None = Field(
        description="When this client's GitHub repo access was last re-read; None with no linked identity"
    )
    access_expires_at: datetime | None = Field(
        description=("When this client's cached access goes too stale to lease against; None with no linked identity")
    )
    access_is_stale: bool = Field(
        description="Whether that deadline has already passed. False with no linked identity — nothing to age"
    )


class ReviewClientRegistered(SQLModel):
    """Registration response: the one and only time the token is shown."""

    client: ReviewClientRead
    token: str

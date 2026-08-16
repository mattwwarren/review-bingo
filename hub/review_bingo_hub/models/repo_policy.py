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
from pydantic import ConfigDict, field_validator
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from review_bingo_hub.models.base import TimestampedTable
from review_bingo_hub.models.review_client import ModelTier
from review_bingo_hub.models.review_strategy import validate_strategies


def model_allowed(
    accepted_models: list[str],
    accepted_model_groups: list[str],
    client_model: str,
    model_groups: dict[str, list[str]],
) -> bool:
    """The model-allowlist gate's matching rule, in-process: both empty is match-any, else union.

    `services.job_service._model_allowlist_clause` expresses this same rule as
    Postgres JSONB predicates so `lease_next_job`/`lease_specific_job` can gate
    inside their locking `SELECT` without a second query; this function is the
    one other place the rule needs to run in plain Python — the targeted-lease
    endpoint's pre-check (`api/jobs.py`), which already has the policy loaded
    and would otherwise be a second, independent encoding of the same rule.
    Keep the two in lockstep by construction: change the semantics here, then
    carry the change into the SQL expression, not the other way around. This is
    the shape `review_strategy.strategies_overlap` already established for the
    strategy gate.

    `model_groups` is passed in rather than read off `settings` so the rule
    stays a pure function of its inputs — the SQL side has to resolve group
    membership in Python anyway, and a hidden read of a mutable global is how
    the two encodings would drift without either one changing.

    An empty allowlist on *both* fields is the match-any sentinel: a repo that
    named no models did not name an impossible one. A named group with no
    definition is the opposite, and deliberately so — it narrows to nobody
    rather than widening to everybody, so an operator deleting a group cannot
    silently open every repo that referenced it.
    """
    if not accepted_models and not accepted_model_groups:
        return True
    if client_model in accepted_models:
        return True
    return any(client_model in model_groups.get(group, []) for group in accepted_model_groups)


class RepoPolicyBase(SQLModel):
    min_tier: ModelTier = Field(
        default=ModelTier.EXPERIMENTAL,
        sa_type=sa.String(),
        description="Minimum client tier allowed to lease this repo's jobs",
    )
    enabled: bool = Field(default=True, description="Disabled repos accept webhooks but queue no jobs")
    accepted_models: list[str] = Field(
        default_factory=list,
        sa_column=sa.Column(JSONB(), server_default="[]", nullable=False),
        description="Exact model names allowed to lease this repo's jobs; empty is match-any",
    )
    accepted_model_groups: list[str] = Field(
        default_factory=list,
        sa_column=sa.Column(JSONB(), server_default="[]", nullable=False),
        description="Model-group names allowed to lease this repo's jobs, resolved against "
        "Settings.model_groups at lease time; empty is match-any, and the effective allowlist is "
        "the union of this and accepted_models when either is non-empty",
    )


class _DefaultStrategiesField(SQLModel):
    """`default_strategies`'s one canonical declaration, isolated from `RepoPolicyBase`.

    `RepoPolicyUpsert` needs to widen this field to `list[str] | None` (the
    omitted-vs-explicit-empty distinction `upsert_policy` relies on) without
    that becoming an LSP-violating override of a mutable field's type on a
    direct parent -- mypy correctly rejects that shape. Keeping the field on a
    sibling mixin that only `RepoPolicy`/`RepoPolicyRead` inherit (mirroring
    how `TimestampedTable` already contributes shared columns the same way)
    means `RepoPolicyUpsert` declares its own field fresh instead of
    overriding one, while `RepoPolicy`/`RepoPolicyRead` still share this single
    `Field(...)` definition, not three copies of it.
    """

    default_strategies: list[str] = Field(
        default_factory=list,
        sa_column=sa.Column(JSONB(), server_default="[]", nullable=False),
        description="Review strategies snapshotted onto a new job's requested_strategies at enqueue "
        "time; empty means any client's offered strategies match",
    )


class RepoPolicy(TimestampedTable, RepoPolicyBase, _DefaultStrategiesField, table=True):
    __tablename__ = "repo_policy"

    repo_full_name: str = Field(index=True, unique=True, description="owner/repo")


class RepoPolicyUpsert(RepoPolicyBase):
    """PUT payload; repo name comes from the path."""

    default_strategies: list[str] | None = Field(
        default=None,
        description=(
            "Review strategies snapshotted onto a new job's requested_strategies at enqueue time; "
            "omitted leaves default_strategies unchanged on an existing policy (or empty on a new "
            "one), an explicit empty list clears it"
        ),
    )

    @field_validator("default_strategies")
    @classmethod
    def check_default_strategies(cls, value: list[str] | None) -> list[str] | None:
        """Validate the vocabulary on the way *in*, and only here.

        Deliberately on the write schema rather than `RepoPolicyBase`: a row
        already in the table was validated when it was written, and re-running
        the check on every read would mean a later narrowing of the registry
        turns stored policies into 500s instead of a migration.

        `None` is not "no strategies" here -- it is "PUT didn't mention them",
        which `upsert_policy` reads as leave-unchanged. Only a list that
        actually arrived is a declaration worth validating, mirroring
        `CheckInRequest.check_offered_strategies`'s omission handling.
        """
        if value is not None:
            validate_strategies(value)
        return value


class RepoPolicyRead(RepoPolicyBase, _DefaultStrategiesField):
    id: UUID
    repo_full_name: str
    created_at: datetime
    updated_at: datetime

    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)  # type: ignore[assignment]

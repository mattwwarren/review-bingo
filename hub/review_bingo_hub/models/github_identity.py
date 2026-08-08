"""GitHub identities behind grid clients, and what each one can reach.

A grid client is a machine; a `GithubIdentity` is the human GitHub account
that vouched for it. One person may plug in several boxes, so identities are
keyed on `github_user_id` and shared across their clients — the account is the
unit of admission, not the machine.

`IdentityRepoAccess` is a *snapshot* of what GitHub said that account could
reach at enrolment time, not a live authority. It is refreshed wholesale on
each enrolment (delete + reinsert) rather than merged, because a repo the
account lost access to has to disappear: a merge would keep granting access
that GitHub has already revoked.

The hub reads these to decide which repos' jobs a client may lease. It never
stores the GitHub token that produced them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import ClassVar
from uuid import UUID

import sqlalchemy as sa
from pydantic import ConfigDict
from sqlmodel import Field, SQLModel

from review_bingo_hub.models.base import TimestampedTable


class PermissionLevel(StrEnum):
    """Collapsed view of GitHub's five per-repo permission booleans.

    Ordering lives in PERMISSION_RANK, not the enum, so the DB stores readable
    strings — same convention as ModelTier.
    """

    READ = "read"
    WRITE = "write"
    ADMIN = "admin"


PERMISSION_RANK: dict[PermissionLevel, int] = {
    PermissionLevel.READ: 0,
    PermissionLevel.WRITE: 1,
    PermissionLevel.ADMIN: 2,
}


def highest_permission(left: PermissionLevel, right: PermissionLevel) -> PermissionLevel:
    """The stronger of two levels, for unioning access across installations."""
    return left if PERMISSION_RANK[left] >= PERMISSION_RANK[right] else right


class GithubIdentityBase(SQLModel):
    """The GitHub account a client enrolled under."""

    github_user_id: int = Field(
        index=True,
        unique=True,
        description="GitHub's numeric user id — stable across login renames, unlike the login",
    )
    github_login: str = Field(description="GitHub login at last enrolment; display only, never a key")


class GithubIdentity(TimestampedTable, GithubIdentityBase, table=True):
    """A GitHub account admitted to the grid."""

    __tablename__ = "github_identity"

    access_refreshed_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_type=sa.DateTime(timezone=True),
        description="When the repo access snapshot below was last re-read from GitHub",
    )


class GithubIdentityRead(GithubIdentityBase):
    id: UUID
    access_refreshed_at: datetime
    created_at: datetime
    updated_at: datetime

    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)  # type: ignore[assignment]


class IdentityRepoAccessBase(SQLModel):
    """One repo an identity could reach, at the permission GitHub reported."""

    repo_full_name: str = Field(index=True, description="owner/repo")
    permission: PermissionLevel = Field(
        sa_type=sa.String(),
        description="Collapsed from GitHub's admin/maintain/push/triage/pull booleans",
    )


class IdentityRepoAccess(TimestampedTable, IdentityRepoAccessBase, table=True):
    __tablename__ = "identity_repo_access"

    identity_id: UUID = Field(
        sa_column=sa.Column(
            sa.UUID(as_uuid=True),
            sa.ForeignKey("github_identity.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )

    __table_args__ = (
        sa.UniqueConstraint("identity_id", "repo_full_name", name="uq_identity_repo_access_identity_repo"),
    )


class IdentityRepoAccessRead(IdentityRepoAccessBase):
    id: UUID
    identity_id: UUID
    created_at: datetime

    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)  # type: ignore[assignment]

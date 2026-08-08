"""Browser sign-ins to the dashboard, keyed to the GitHub account behind them.

The other half of `review_client`: that table is a *machine* that joined the
grid, this one is a *person* looking at it. Both authenticate with an opaque
bearer token and both store only its SHA-256 digest, because the threat model is
identical — a dump of either table must not hand anyone a working credential.

A session carries no permissions of its own. It names an identity, and every
read it makes is scoped by that identity's GitHub repo access, exactly as a
client's is. So a login can never widen what the underlying GitHub account could
already see; it only lets a browser ask on that account's behalf.

Sessions expire on their own clock (`DASHBOARD_SESSION_TTL_SECONDS`) rather than
riding the identity's access-snapshot TTL: one bounds a sign-in, the other bounds
how stale an authorization may be at dispatch, and collapsing them would mean
tightening dispatch safety by logging everybody out.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlmodel import Field

from review_bingo_hub.models.base import TimestampedTable


class DashboardSession(TimestampedTable, table=True):
    """One signed-in browser, and the GitHub account it speaks for."""

    __tablename__ = "dashboard_session"

    identity_id: UUID = Field(
        sa_column=sa.Column(
            sa.UUID(as_uuid=True),
            sa.ForeignKey("github_identity.id", ondelete="CASCADE"),
            nullable=False,
        ),
        description="GitHub account this session reads on behalf of",
    )
    token_hash: str = Field(
        index=True,
        unique=True,
        description="SHA-256 hex digest of the session bearer token; the plaintext is never stored",
    )
    expires_at: datetime = Field(
        sa_type=sa.DateTime(timezone=True),
        index=True,
        description="After this instant the session resolves to nobody, exactly like an unknown token",
    )

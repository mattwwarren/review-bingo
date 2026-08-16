"""add model allowlist and runtime identity

Two JSONB string arrays on repo_policy — the exact model names a repo accepts
(accepted_models) and the operator-curated groups it accepts (accepted_model_groups)
— plus review_client.runtime_identity, the self-declared runtime a client reviews
from.

server_default='[]' with nullable=False is what backfills the allowlist columns,
so no data migration accompanies this: an empty array is the match-any sentinel,
which means every repo already in the table keeps leasing to exactly the clients
it leased to before. runtime_identity is nullable instead, because "this client
never said" is a real answer and not the same as an empty declaration.

Revision ID: c7d81f2a4b39
Revises: e06bf706f804
Create Date: 2026-08-16 12:04:18.221905

"""
import sqlalchemy as sa
import sqlmodel
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'c7d81f2a4b39'
down_revision = 'e06bf706f804'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('repo_policy', sa.Column('accepted_models', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False))
    op.add_column('repo_policy', sa.Column('accepted_model_groups', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False))
    op.add_column('review_client', sa.Column('runtime_identity', sqlmodel.sql.sqltypes.AutoString(), nullable=True))  # type: ignore[attr-defined]


def downgrade() -> None:
    op.drop_column('review_client', 'runtime_identity')
    op.drop_column('repo_policy', 'accepted_model_groups')
    op.drop_column('repo_policy', 'accepted_models')

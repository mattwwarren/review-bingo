"""add dashboard_session table

Revision ID: d4e5f6a7b8c9
Revises: b2c3d4e5f6a7
Create Date: 2026-08-08 00:00:00.000000

"""

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision = 'd4e5f6a7b8c9'
down_revision = 'b2c3d4e5f6a7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'dashboard_session',
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('identity_id', sa.UUID(), nullable=False),
        sa.Column('token_hash', sqlmodel.sql.sqltypes.AutoString(), nullable=False),  # type: ignore[attr-defined]
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['identity_id'], ['github_identity.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_dashboard_session_created_at'), 'dashboard_session', ['created_at'], unique=False)
    op.create_index(op.f('ix_dashboard_session_expires_at'), 'dashboard_session', ['expires_at'], unique=False)
    op.create_index(op.f('ix_dashboard_session_token_hash'), 'dashboard_session', ['token_hash'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_dashboard_session_token_hash'), table_name='dashboard_session')
    op.drop_index(op.f('ix_dashboard_session_expires_at'), table_name='dashboard_session')
    op.drop_index(op.f('ix_dashboard_session_created_at'), table_name='dashboard_session')
    op.drop_table('dashboard_session')

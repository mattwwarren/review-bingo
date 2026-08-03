"""add grid tables: review_client, review_job, repo_policy

Revision ID: a1b2c3d4e5f6
Revises: 9c89a84af0d3
Create Date: 2026-08-03 00:00:00.000000

"""

import sqlalchemy as sa
import sqlmodel
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = '9c89a84af0d3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'review_client',
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),  # type: ignore[attr-defined]
        sa.Column('model_name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),  # type: ignore[attr-defined]
        sa.Column('provider', sqlmodel.sql.sqltypes.AutoString(), nullable=False),  # type: ignore[attr-defined]
        sa.Column('quant', sqlmodel.sql.sqltypes.AutoString(), nullable=True),  # type: ignore[attr-defined]
        sa.Column('tier', sa.String(), nullable=False),
        sa.Column('token_hash', sqlmodel.sql.sqltypes.AutoString(), nullable=False),  # type: ignore[attr-defined]
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_review_client_created_at'), 'review_client', ['created_at'], unique=False)
    op.create_index(op.f('ix_review_client_name'), 'review_client', ['name'], unique=False)
    op.create_index(op.f('ix_review_client_status'), 'review_client', ['status'], unique=False)
    op.create_index(op.f('ix_review_client_token_hash'), 'review_client', ['token_hash'], unique=True)

    op.create_table(
        'review_job',
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('repo_full_name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),  # type: ignore[attr-defined]
        sa.Column('pr_number', sa.Integer(), nullable=False),
        sa.Column('head_sha', sqlmodel.sql.sqltypes.AutoString(), nullable=False),  # type: ignore[attr-defined]
        sa.Column('pr_title', sqlmodel.sql.sqltypes.AutoString(), nullable=True),  # type: ignore[attr-defined]
        sa.Column('event_action', sqlmodel.sql.sqltypes.AutoString(), nullable=False),  # type: ignore[attr-defined]
        sa.Column('state', sa.String(), nullable=False),
        sa.Column('min_tier', sa.String(), nullable=False),
        sa.Column('leased_by', sa.UUID(), nullable=True),
        sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('max_attempts', sa.Integer(), nullable=False),
        sa.Column('verdict', sqlmodel.sql.sqltypes.AutoString(), nullable=True),  # type: ignore[attr-defined]
        sa.Column('summary', sqlmodel.sql.sqltypes.AutoString(), nullable=True),  # type: ignore[attr-defined]
        sa.Column('findings', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
        sa.Column('reported_by', sa.UUID(), nullable=True),
        sa.Column('relay_error', sqlmodel.sql.sqltypes.AutoString(), nullable=True),  # type: ignore[attr-defined]
        sa.ForeignKeyConstraint(['leased_by'], ['review_client.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['reported_by'], ['review_client.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_review_job_created_at'), 'review_job', ['created_at'], unique=False)
    op.create_index(op.f('ix_review_job_repo_full_name'), 'review_job', ['repo_full_name'], unique=False)
    op.create_index(op.f('ix_review_job_state'), 'review_job', ['state'], unique=False)
    op.create_index('ix_review_job_repo_pr', 'review_job', ['repo_full_name', 'pr_number'], unique=False)

    op.create_table(
        'repo_policy',
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('min_tier', sa.String(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('repo_full_name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),  # type: ignore[attr-defined]
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_repo_policy_created_at'), 'repo_policy', ['created_at'], unique=False)
    op.create_index(op.f('ix_repo_policy_repo_full_name'), 'repo_policy', ['repo_full_name'], unique=True)


def downgrade() -> None:
    op.drop_table('repo_policy')
    op.drop_table('review_job')
    op.drop_table('review_client')

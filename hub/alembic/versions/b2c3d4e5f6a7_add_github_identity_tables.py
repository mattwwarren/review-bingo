"""add github identity tables: github_identity, identity_repo_access

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-07 00:00:00.000000

"""

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision = 'b2c3d4e5f6a7'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'github_identity',
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('github_user_id', sa.Integer(), nullable=False),
        sa.Column('github_login', sqlmodel.sql.sqltypes.AutoString(), nullable=False),  # type: ignore[attr-defined]
        sa.Column('access_refreshed_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_github_identity_created_at'), 'github_identity', ['created_at'], unique=False)
    op.create_index(op.f('ix_github_identity_github_user_id'), 'github_identity', ['github_user_id'], unique=True)

    op.create_table(
        'identity_repo_access',
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('identity_id', sa.UUID(), nullable=False),
        sa.Column('repo_full_name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),  # type: ignore[attr-defined]
        sa.Column('permission', sa.String(), nullable=False),
        sa.ForeignKeyConstraint(['identity_id'], ['github_identity.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('identity_id', 'repo_full_name', name='uq_identity_repo_access_identity_repo'),
    )
    op.create_index(op.f('ix_identity_repo_access_created_at'), 'identity_repo_access', ['created_at'], unique=False)
    op.create_index(
        op.f('ix_identity_repo_access_repo_full_name'), 'identity_repo_access', ['repo_full_name'], unique=False
    )

    op.add_column('review_client', sa.Column('identity_id', sa.UUID(), nullable=True))
    op.create_foreign_key(
        'fk_review_client_identity_id_github_identity',
        'review_client',
        'github_identity',
        ['identity_id'],
        ['id'],
        ondelete='SET NULL',
    )


def downgrade() -> None:
    op.drop_constraint('fk_review_client_identity_id_github_identity', 'review_client', type_='foreignkey')
    op.drop_column('review_client', 'identity_id')

    op.drop_index(op.f('ix_identity_repo_access_repo_full_name'), table_name='identity_repo_access')
    op.drop_index(op.f('ix_identity_repo_access_created_at'), table_name='identity_repo_access')
    op.drop_table('identity_repo_access')

    op.drop_index(op.f('ix_github_identity_github_user_id'), table_name='github_identity')
    op.drop_index(op.f('ix_github_identity_created_at'), table_name='github_identity')
    op.drop_table('github_identity')

"""fixes

Revision ID: 1510be4b0e2c
Revises: bf3d47f7b07f
Create Date: 2026-07-22 19:27:23.581631

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '1510be4b0e2c'
down_revision: Union[str, None] = 'bf3d47f7b07f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Phase 3 step 2: one proposed fix or merchant to-do per row. after_json NULL marks a
    # non-publishable to-do; source_json carries the grounding citation. run_id nullable + SET NULL
    # from day one; reverts_fix_id is the append-only rollback link (populated in step 4).
    op.create_table('fixes',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('product_id', sa.Integer(), nullable=False),
    sa.Column('run_id', sa.Integer(), nullable=True),
    sa.Column('type', sa.String(length=32), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('target', sa.String(length=255), nullable=False),
    sa.Column('before_json', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('after_json', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('source_json', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('diff', sa.Text(), nullable=True),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('base_source_hash', sa.Text(), nullable=True),
    sa.Column('base_shopify_updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('reverts_fix_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['reverts_fix_id'], ['fixes.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['run_id'], ['agent_runs.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_fixes_product_id'), 'fixes', ['product_id'], unique=False)
    op.create_index(op.f('ix_fixes_run_id'), 'fixes', ['run_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_fixes_run_id'), table_name='fixes')
    op.drop_index(op.f('ix_fixes_product_id'), table_name='fixes')
    op.drop_table('fixes')

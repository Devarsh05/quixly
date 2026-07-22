"""audits

Revision ID: 11a93152cf9c
Revises: fd51f9b101b6
Create Date: 2026-07-22 11:54:22.991039

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '11a93152cf9c'
down_revision: Union[str, None] = 'fd51f9b101b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Phase 3 step 1: one deterministic product-audit result per row. run_id is nullable + SET
    # NULL (same as engine_runs.run_id) — audits are measurement data, run-scoped from day one so
    # Phase 4's Verifier can compare a pre-fix audit to a post-fix audit of the same product.
    op.create_table('audits',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('product_id', sa.Integer(), nullable=False),
    sa.Column('run_id', sa.Integer(), nullable=True),
    # product_class snapshots the class the rubric scored against (coffee/equipment/other) so the
    # Verifier compares like-for-like. spec_coverage is NULLABLE — spec scoring only applies to a
    # class with a grounded vocabulary (coffee); equipment/other/not-audited carry NULL, not 0.0.
    sa.Column('product_class', sa.String(length=32), nullable=True),
    sa.Column('gaps_json', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('spec_coverage', sa.Float(), nullable=True),
    sa.Column('severity', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['run_id'], ['agent_runs.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_audits_product_id'), 'audits', ['product_id'], unique=False)
    op.create_index(op.f('ix_audits_run_id'), 'audits', ['run_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_audits_run_id'), table_name='audits')
    op.drop_index(op.f('ix_audits_product_id'), table_name='audits')
    op.drop_table('audits')

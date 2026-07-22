"""products_product_type_category

Revision ID: bf3d47f7b07f
Revises: 11a93152cf9c
Create Date: 2026-07-22 18:26:59.283663

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'bf3d47f7b07f'
down_revision: Union[str, None] = '11a93152cf9c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Raw merchant fields for per-class auditing (Phase 3 step 1). Nullable — populated on the
    # next ingest; the product class is derived from these deterministically at audit time.
    op.add_column('products', sa.Column('product_type', sa.String(length=255), nullable=True))
    op.add_column('products', sa.Column('category', sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column('products', 'category')
    op.drop_column('products', 'product_type')

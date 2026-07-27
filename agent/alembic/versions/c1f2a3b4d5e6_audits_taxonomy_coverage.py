"""audits taxonomy_coverage (replaces structured_coverage)

Revision ID: c1f2a3b4d5e6
Revises: 9b4947bc6fbb
Create Date: 2026-07-24 20:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c1f2a3b4d5e6'
down_revision: Union[str, None] = '9b4947bc6fbb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Phase 3 step 2d — the 2c write-target spike proved custom.* metafields are NOT the AI-legible
    # channel, so the 2b ``structured_coverage`` (which counted custom.*) now has a wrong meaning.
    # It is DROPPED (not repurposed — overwriting a column's meaning while keeping its name is the
    # trap) and replaced by ``taxonomy_coverage``: families written to their shopify-namespace
    # taxonomy attribute / applicable taxonomy-home families. Every 2b audit row was measured before
    # any taxonomy write existed, so there is nothing to migrate; new audits populate the column.
    #
    # NULLABLE, mirroring spec_coverage: spec scoring applies only to classes with a grounded
    # vocabulary (coffee today), so equipment / other / not-audited rows carry NULL, never 0.0.
    op.add_column('audits', sa.Column('taxonomy_coverage', sa.Float(), nullable=True))
    op.drop_column('audits', 'structured_coverage')


def downgrade() -> None:
    op.add_column('audits', sa.Column('structured_coverage', sa.Float(), nullable=True))
    op.drop_column('audits', 'taxonomy_coverage')

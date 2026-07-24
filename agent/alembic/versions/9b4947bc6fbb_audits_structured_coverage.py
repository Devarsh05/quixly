"""audits structured_coverage

Revision ID: 9b4947bc6fbb
Revises: 1510be4b0e2c
Create Date: 2026-07-24 12:22:17.268273

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9b4947bc6fbb'
down_revision: Union[str, None] = '1510be4b0e2c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Phase 3 step 2b — the headline AI-legibility score: spec families carried by a metafield.
    # ADDED alongside spec_coverage (prose coverage), never replacing it: the difference between
    # the two is the addressable set the Optimizer can fix automatically.
    #
    # NULLABLE, mirroring spec_coverage: spec scoring applies only to classes with a grounded
    # vocabulary (coffee today), so equipment / other / not-audited rows carry NULL rather than a
    # misleading 0.0. Existing audit rows keep NULL — they were scored before this column existed,
    # and backfilling a 0.0 would assert a measurement that was never taken.
    op.add_column('audits', sa.Column('structured_coverage', sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column('audits', 'structured_coverage')

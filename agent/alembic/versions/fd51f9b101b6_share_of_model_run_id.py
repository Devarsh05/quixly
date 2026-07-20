"""share_of_model_run_id

Revision ID: fd51f9b101b6
Revises: 4d303431605b
Create Date: 2026-07-20 13:40:44.227002

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fd51f9b101b6'
down_revision: Union[str, None] = '4d303431605b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # share_of_model is a derived cache — every row is recomputed from engine_runs on the next
    # scan and holds no production data. Clearing it is non-destructive and lets the NOT NULL
    # run_id column be added directly. (This migration also runs on deploy.)
    op.execute("DELETE FROM share_of_model")

    op.add_column("share_of_model", sa.Column("run_id", sa.Integer(), nullable=False))
    op.create_index(
        op.f("ix_share_of_model_run_id"), "share_of_model", ["run_id"], unique=False
    )
    op.create_foreign_key(
        "fk_share_of_model_run_id_agent_runs",
        "share_of_model",
        "agent_runs",
        ["run_id"],
        ["id"],
        ondelete="CASCADE",
    )
    # Move the unique key off (shop_id, engine, period) onto (run_id, engine): one aggregate row
    # per scan, so same-day runs no longer collide.
    op.drop_constraint(
        "uq_share_of_model_shop_engine_period", "share_of_model", type_="unique"
    )
    op.create_unique_constraint(
        "uq_share_of_model_run_engine", "share_of_model", ["run_id", "engine"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_share_of_model_run_engine", "share_of_model", type_="unique")
    # Run-scoped rows may hold duplicate (shop_id, engine, period); clearing the derived cache
    # (recomputed on the next scan, no production data) lets the looser old unique key re-add
    # without a duplicate-key violation.
    op.execute("DELETE FROM share_of_model")
    op.create_unique_constraint(
        "uq_share_of_model_shop_engine_period",
        "share_of_model",
        ["shop_id", "engine", "period"],
    )
    op.drop_constraint(
        "fk_share_of_model_run_id_agent_runs", "share_of_model", type_="foreignkey"
    )
    op.drop_index(op.f("ix_share_of_model_run_id"), table_name="share_of_model")
    op.drop_column("share_of_model", "run_id")

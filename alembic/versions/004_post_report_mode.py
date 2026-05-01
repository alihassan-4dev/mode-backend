"""add mode decision columns to post_reports

Revision ID: 004
Revises: 003
Create Date: 2026-05-01
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("post_reports") as batch:
        batch.add_column(sa.Column("mode_label", sa.String(length=40), nullable=True))
        batch.add_column(sa.Column("mode_confidence", sa.Float(), nullable=True))
        batch.add_column(sa.Column("mode_drivers", sa.JSON(), nullable=True))
    op.create_index("ix_post_reports_mode_label", "post_reports", ["mode_label"])


def downgrade() -> None:
    op.drop_index("ix_post_reports_mode_label", table_name="post_reports")
    with op.batch_alter_table("post_reports") as batch:
        batch.drop_column("mode_drivers")
        batch.drop_column("mode_confidence")
        batch.drop_column("mode_label")

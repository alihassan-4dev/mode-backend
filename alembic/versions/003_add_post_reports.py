"""add post_reports table

Revision ID: 003
Revises: 002
Create Date: 2026-04-30
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "post_reports",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("platform", sa.String(length=20), nullable=False),
        sa.Column("post_id", sa.String(length=255), nullable=False),
        sa.Column("post_text", sa.Text(), nullable=True),
        sa.Column("permalink", sa.Text(), nullable=True),
        sa.Column("media_type", sa.String(length=50), nullable=True),
        sa.Column("media_url", sa.Text(), nullable=True),
        sa.Column("post_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("likes_count", sa.Integer(), nullable=True),
        sa.Column("comments_count", sa.Integer(), nullable=True),
        sa.Column("sentiment_label", sa.String(length=20), nullable=True),
        sa.Column("sentiment_score", sa.Float(), nullable=True),
        sa.Column("engagement_quality", sa.String(length=20), nullable=True),
        sa.Column("engagement_score", sa.Float(), nullable=True),
        sa.Column("recommendation", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("tone", sa.String(length=50), nullable=True),
        sa.Column("topics", sa.JSON(), nullable=True),
        sa.Column("strengths", sa.JSON(), nullable=True),
        sa.Column("weaknesses", sa.JSON(), nullable=True),
        sa.Column("raw_analysis", sa.JSON(), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("platform IN ('facebook', 'instagram')", name="ck_post_report_platform"),
        sa.UniqueConstraint("user_id", "platform", "post_id", name="uq_user_platform_post"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_post_reports_user_id", "post_reports", ["user_id"])
    op.create_index("ix_post_reports_platform", "post_reports", ["platform"])


def downgrade() -> None:
    op.drop_index("ix_post_reports_platform", table_name="post_reports")
    op.drop_index("ix_post_reports_user_id", table_name="post_reports")
    op.drop_table("post_reports")

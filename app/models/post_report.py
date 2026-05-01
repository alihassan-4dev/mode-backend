import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class PostReport(Base):
    __tablename__ = "post_reports"
    __table_args__ = (
        UniqueConstraint("user_id", "platform", "post_id", name="uq_user_platform_post"),
        CheckConstraint("platform IN ('facebook', 'instagram')", name="ck_post_report_platform"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    platform: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    post_id: Mapped[str] = mapped_column(String(255), nullable=False)
    post_text: Mapped[str | None] = mapped_column(Text)
    permalink: Mapped[str | None] = mapped_column(Text)
    media_type: Mapped[str | None] = mapped_column(String(50))
    media_url: Mapped[str | None] = mapped_column(Text)
    post_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    likes_count: Mapped[int | None] = mapped_column(Integer)
    comments_count: Mapped[int | None] = mapped_column(Integer)

    sentiment_label: Mapped[str | None] = mapped_column(String(20))
    sentiment_score: Mapped[float | None] = mapped_column(Float)
    engagement_quality: Mapped[str | None] = mapped_column(String(20))
    engagement_score: Mapped[float | None] = mapped_column(Float)
    recommendation: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    tone: Mapped[str | None] = mapped_column(String(50))
    topics: Mapped[list | None] = mapped_column(JSON, default=list)
    strengths: Mapped[list | None] = mapped_column(JSON, default=list)
    weaknesses: Mapped[list | None] = mapped_column(JSON, default=list)
    raw_analysis: Mapped[dict | None] = mapped_column(JSON, default=dict)

    # Mode decision: which "mode" this single post signals about the user.
    mode_label: Mapped[str | None] = mapped_column(String(40), index=True)
    mode_confidence: Mapped[float | None] = mapped_column(Float)
    mode_drivers: Mapped[list | None] = mapped_column(JSON, default=list)

    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

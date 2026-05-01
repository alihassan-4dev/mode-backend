from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class ModeDistributionItem(BaseModel):
    label: str
    share: float
    count: int


class ModeSummary(BaseModel):
    period: Literal["current", "weekly", "monthly"]
    window_days: int | None = None
    post_count: int
    mode_label: str | None = None
    mode_vibe: str | None = None
    confidence: float
    distribution: list[ModeDistributionItem] = []
    narrative: str


class PostReportOut(BaseModel):
    platform: Literal["facebook", "instagram"]
    post_id: str
    post_text: str | None = None
    permalink: str | None = None
    media_type: str | None = None
    media_url: str | None = None
    post_created_at: datetime | None = None
    likes_count: int | None = None
    comments_count: int | None = None
    sentiment_label: str | None = None
    sentiment_score: float | None = None
    engagement_quality: str | None = None
    engagement_score: float | None = None
    recommendation: str | None = None
    summary: str | None = None
    tone: str | None = None
    topics: list[str] = []
    strengths: list[str] = []
    weaknesses: list[str] = []
    mode_label: str | None = None
    mode_confidence: float | None = None
    mode_drivers: list[str] = []
    generated_at: datetime
    updated_at: datetime


class OverallSentimentPoint(BaseModel):
    label: str
    count: int


class PlatformOverview(BaseModel):
    platform: Literal["facebook", "instagram"]
    total_posts: int
    avg_sentiment: float
    total_likes: int
    total_comments: int
    positive: int
    neutral: int
    negative: int


class ReportsResponse(BaseModel):
    user_id: str
    generated_at: datetime
    refresh_interval_minutes: int
    next_refresh_at: datetime | None = None
    facebook: list[PostReportOut]
    instagram: list[PostReportOut]
    overall: list[PlatformOverview]
    overall_recommendation: str | None = None
    current_mode: ModeSummary
    weekly_mode: ModeSummary
    monthly_mode: ModeSummary

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class PostAiAnalysis(BaseModel):
    sentiment_label: Literal["positive", "neutral", "negative"]
    sentiment_score: float = Field(ge=-1.0, le=1.0)
    engagement_quality: Literal["low", "medium", "high"]
    recommendation: str


class SocialPostOut(BaseModel):
    platform: Literal["facebook", "instagram"]
    post_id: str
    text: str | None = None
    created_at: datetime | None = None
    permalink: str | None = None
    media_type: str | None = None
    media_url: str | None = None
    likes_count: int | None = None
    comments_count: int | None = None
    analysis: PostAiAnalysis | None = None


class SocialPostsMeta(BaseModel):
    platform: Literal["facebook", "instagram"]
    count: int
    limit: int
    next_cursor: str | None = None
    fetched_at: datetime
    notice: str | None = None


class SocialPostsResponse(BaseModel):
    posts: list[SocialPostOut]
    meta: SocialPostsMeta

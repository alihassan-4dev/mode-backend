from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from app.core.config import Settings
from app.schemas.social_posts import PostAiAnalysis, SocialPostOut

logger = logging.getLogger(__name__)

MAX_ANALYSIS_POSTS = 10
MAX_TEXT_CHARS = 360


def _safe_json_loads(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("[")
        end = text.rfind("]")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
    return None


def _build_llm(settings: Settings) -> ChatOpenAI:
    return ChatOpenAI(
        api_key=settings.GROQ_API_KEY,
        base_url=settings.GROQ_BASE_URL,
        model=settings.GROQ_MODEL,
        temperature=0.1,
        timeout=15.0,
    )


def _heuristic_analysis(text: str | None, likes: int | None, comments: int | None) -> PostAiAnalysis:
    value = (text or "").lower().strip()
    positive_words = {"great", "happy", "love", "excited", "good", "amazing"}
    negative_words = {"sad", "bad", "angry", "tired", "stress", "stressed"}

    pos_hits = sum(1 for word in positive_words if word in value)
    neg_hits = sum(1 for word in negative_words if word in value)

    if pos_hits > neg_hits:
        sentiment_label = "positive"
        sentiment_score = 0.5
    elif neg_hits > pos_hits:
        sentiment_label = "negative"
        sentiment_score = -0.5
    else:
        sentiment_label = "neutral"
        sentiment_score = 0.0

    likes_count = max(0, likes or 0)
    comments_count = max(0, comments or 0)
    engagement_score = likes_count + (comments_count * 2)
    if engagement_score >= 40:
        engagement_quality = "high"
    elif engagement_score >= 10:
        engagement_quality = "medium"
    else:
        engagement_quality = "low"

    recommendation = "Keep this post style and timing."
    if sentiment_label == "negative":
        recommendation = "Use a calmer tone and add a clear supportive call-to-action."
    elif engagement_quality == "low":
        recommendation = "Try a shorter caption with one direct question to improve interaction."

    return PostAiAnalysis(
        sentiment_label=sentiment_label,
        sentiment_score=sentiment_score,
        engagement_quality=engagement_quality,
        recommendation=recommendation,
    )


async def attach_basic_ai_analysis(
    *,
    posts: list[SocialPostOut],
    settings: Settings,
) -> list[SocialPostOut]:
    if not posts:
        return posts

    for post in posts[MAX_ANALYSIS_POSTS:]:
        post.analysis = _heuristic_analysis(post.text, post.likes_count, post.comments_count)

    analyzable = posts[:MAX_ANALYSIS_POSTS]
    if not settings.GROQ_API_KEY:
        for post in analyzable:
            post.analysis = _heuristic_analysis(post.text, post.likes_count, post.comments_count)
        return posts

    try:
        llm = _build_llm(settings)
        compact_posts: list[dict[str, Any]] = [
            {
                "id": p.post_id,
                "text": (p.text or "")[:MAX_TEXT_CHARS],
                "likes": p.likes_count or 0,
                "comments": p.comments_count or 0,
            }
            for p in analyzable
        ]
        system = SystemMessage(
            content=(
                "You are a strict JSON assistant for social media post analysis. "
                "Return a JSON array with one item per input id. "
                "Each item keys: id, sentiment_label(positive|neutral|negative), "
                "sentiment_score(-1..1), engagement_quality(low|medium|high), recommendation(<=120 chars). "
                "No markdown, JSON only."
            )
        )
        user = HumanMessage(content=json.dumps(compact_posts, ensure_ascii=True))
        response = await llm.ainvoke([system, user])
        payload = response.content if isinstance(response.content, str) else "[]"
        parsed = _safe_json_loads(payload)
        if not isinstance(parsed, list):
            raise ValueError("AI output is not a list")

        by_id: dict[str, PostAiAnalysis] = {}
        for item in parsed:
            if not isinstance(item, dict) or "id" not in item:
                continue
            try:
                by_id[str(item["id"])] = PostAiAnalysis(
                    sentiment_label=str(item.get("sentiment_label", "neutral")),  # type: ignore[arg-type]
                    sentiment_score=float(item.get("sentiment_score", 0.0)),
                    engagement_quality=str(item.get("engagement_quality", "medium")),  # type: ignore[arg-type]
                    recommendation=str(item.get("recommendation", "Keep testing this content style."))[:120],
                )
            except Exception:
                continue

        for post in analyzable:
            post.analysis = by_id.get(
                post.post_id,
                _heuristic_analysis(post.text, post.likes_count, post.comments_count),
            )
    except Exception:
        logger.exception("Post AI analysis failed, falling back to heuristic mode.")
        for post in analyzable:
            post.analysis = _heuristic_analysis(post.text, post.likes_count, post.comments_count)

    return posts

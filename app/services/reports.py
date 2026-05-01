"""
Reports service: per-post AI reports for Facebook and Instagram, plus an overall
analytics rollup. Reports are persisted in `post_reports` and refreshed on a
schedule (see `app.services.report_scheduler`).
"""

from __future__ import annotations


import json
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.post_report import PostReport
from app.models.social_connection import SocialConnection
from app.schemas.social_posts import SocialPostOut
from app.services import meta, post_ai

logger = logging.getLogger(__name__)

MAX_TEXT_CHARS = 600

# Controlled mode vocabulary used across the app. Keep this in sync with
# frontend display/colors (see Reports.tsx and dashboard ModeReportCard).
MODE_LABELS: tuple[str, ...] = (
    "Energetic",
    "Joyful",
    "Calm",
    "Focused",
    "Reflective",
    "Social",
    "Stressed",
    "Anxious",
    "Burned-out",
    "Withdrawn",
)

# Sentiment-based fallback when no LLM is available.
_HEURISTIC_MODE_BY_SENTIMENT = {
    "positive": ("Joyful", 0.55),
    "neutral": ("Reflective", 0.45),
    "negative": ("Stressed", 0.55),
}


def _build_llm(settings: Settings) -> ChatOpenAI:
    return ChatOpenAI(
        api_key=settings.GROQ_API_KEY,
        base_url=settings.GROQ_BASE_URL,
        model=settings.GROQ_MODEL,
        temperature=0.2,
        timeout=20.0,
    )


def _normalize_facebook(post: dict) -> SocialPostOut:
    likes = post.get("likes", {}).get("summary", {}).get("total_count", 0)
    comments = post.get("comments", {}).get("summary", {}).get("total_count", 0)
    permalink = post.get("permalink_url")
    if not permalink and post.get("id"):
        permalink = f"https://www.facebook.com/{post['id']}"
    return SocialPostOut(
        platform="facebook",
        post_id=str(post.get("id", "")),
        text=post.get("message"),
        created_at=post.get("created_time"),
        permalink=permalink,
        media_type=post.get("type"),
        media_url=None,
        likes_count=int(likes or 0),
        comments_count=int(comments or 0),
    )


def _normalize_instagram(post: dict) -> SocialPostOut:
    return SocialPostOut(
        platform="instagram",
        post_id=str(post.get("id", "")),
        text=post.get("caption"),
        created_at=post.get("timestamp"),
        permalink=post.get("permalink"),
        media_type=post.get("media_type"),
        media_url=post.get("media_url") or post.get("thumbnail_url"),
        likes_count=None,
        comments_count=None,
    )


async def _fetch_platform_posts(
    *, platform: str, token: str, limit: int
) -> list[SocialPostOut]:
    try:
        if platform == "facebook":
            result = await meta.fetch_fb_posts_detailed(token, limit=limit)
            return [_normalize_facebook(p) for p in result.posts]
        if platform == "instagram":
            raw = await meta.fetch_ig_media(token, limit=limit)
            return [_normalize_instagram(p) for p in raw]
    except httpx.HTTPStatusError as exc:
        logger.warning("Reports fetch %s failed: %s", platform, exc)
    return []


def _heuristic_full(post: SocialPostOut) -> dict[str, Any]:
    base = post_ai._heuristic_analysis(post.text, post.likes_count, post.comments_count)
    likes = post.likes_count or 0
    comments = post.comments_count or 0
    engagement = likes + comments * 2
    mode_label, mode_conf = _HEURISTIC_MODE_BY_SENTIMENT.get(
        base.sentiment_label, ("Reflective", 0.4)
    )
    return {
        "sentiment_label": base.sentiment_label,
        "sentiment_score": base.sentiment_score,
        "engagement_quality": base.engagement_quality,
        "engagement_score": float(engagement),
        "recommendation": base.recommendation,
        "summary": (post.text or "")[:160] or "No caption.",
        "tone": "neutral",
        "topics": [],
        "strengths": [],
        "weaknesses": [],
        "mode_label": mode_label,
        "mode_confidence": mode_conf,
        "mode_drivers": [base.sentiment_label],
    }


async def _llm_analyze_post(post: SocialPostOut, settings: Settings) -> dict[str, Any]:
    if not settings.GROQ_API_KEY:
        return _heuristic_full(post)
    try:
        llm = _build_llm(settings)
        payload = {
            "platform": post.platform,
            "text": (post.text or "")[:MAX_TEXT_CHARS],
            "likes": post.likes_count or 0,
            "comments": post.comments_count or 0,
            "media_type": post.media_type or "unknown",
        }
        mode_choices = "|".join(MODE_LABELS)
        system = SystemMessage(
            content=(
                "You are an expert wellness + social-media analyst for the Mode app. "
                "Read ONE social post and decide: (1) sentiment/engagement signals, "
                "(2) which user MODE this single post most likely reflects. "
                "Return a strict JSON object with keys: "
                "sentiment_label(positive|neutral|negative), sentiment_score(-1..1), "
                "engagement_quality(low|medium|high), engagement_score(0..100), "
                "tone(short word like cheerful, formal, urgent), summary(<=160 chars human summary), "
                "topics(list of 1-4 short tags), strengths(list of 1-3 strings), "
                "weaknesses(list of 1-3 strings), recommendation(<=180 chars actionable advice), "
                f"mode_label(one of: {mode_choices}), mode_confidence(0..1), "
                "mode_drivers(list of 1-3 short cues from the post that drove the mode pick). "
                "JSON only, no markdown."
            )
        )
        user = HumanMessage(content=json.dumps(payload, ensure_ascii=True))
        response = await llm.ainvoke([system, user])
        text = response.content if isinstance(response.content, str) else "{}"
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("not a dict")

        def _list(key: str) -> list[str]:
            value = data.get(key, [])
            if isinstance(value, list):
                return [str(item)[:60] for item in value][:4]
            return []

        mode_label_raw = str(data.get("mode_label", "")).strip()
        mode_label = next(
            (m for m in MODE_LABELS if m.lower() == mode_label_raw.lower()),
            None,
        )
        if mode_label is None:
            fallback_label, fallback_conf = _HEURISTIC_MODE_BY_SENTIMENT.get(
                str(data.get("sentiment_label", "neutral")), ("Reflective", 0.4)
            )
            mode_label = fallback_label
            mode_conf = fallback_conf
        else:
            try:
                mode_conf = max(0.0, min(1.0, float(data.get("mode_confidence", 0.6))))
            except (TypeError, ValueError):
                mode_conf = 0.6

        return {
            "sentiment_label": str(data.get("sentiment_label", "neutral")),
            "sentiment_score": float(data.get("sentiment_score", 0.0)),
            "engagement_quality": str(data.get("engagement_quality", "medium")),
            "engagement_score": float(data.get("engagement_score", 0.0)),
            "recommendation": str(data.get("recommendation", ""))[:200],
            "summary": str(data.get("summary", ""))[:200],
            "tone": str(data.get("tone", "neutral"))[:30],
            "topics": _list("topics"),
            "strengths": _list("strengths"),
            "weaknesses": _list("weaknesses"),
            "mode_label": mode_label,
            "mode_confidence": mode_conf,
            "mode_drivers": _list("mode_drivers"),
            "raw": data,
        }
    except Exception:
        logger.exception("LLM per-post analysis failed; falling back to heuristic")
        return _heuristic_full(post)


async def _upsert_report(
    db: AsyncSession,
    *,
    user_id: str,
    post: SocialPostOut,
    analysis: dict[str, Any],
) -> None:
    stmt = select(PostReport).where(
        PostReport.user_id == user_id,
        PostReport.platform == post.platform,
        PostReport.post_id == post.post_id,
    )
    existing = (await db.execute(stmt)).scalar_one_or_none()
    now = datetime.now(timezone.utc)

    fields = dict(
        post_text=post.text,
        permalink=post.permalink,
        media_type=post.media_type,
        media_url=post.media_url,
        post_created_at=post.created_at,
        likes_count=post.likes_count,
        comments_count=post.comments_count,
        sentiment_label=analysis.get("sentiment_label"),
        sentiment_score=analysis.get("sentiment_score"),
        engagement_quality=analysis.get("engagement_quality"),
        engagement_score=analysis.get("engagement_score"),
        recommendation=analysis.get("recommendation"),
        summary=analysis.get("summary"),
        tone=analysis.get("tone"),
        topics=analysis.get("topics") or [],
        strengths=analysis.get("strengths") or [],
        weaknesses=analysis.get("weaknesses") or [],
        raw_analysis=analysis.get("raw") or {},
        mode_label=analysis.get("mode_label"),
        mode_confidence=analysis.get("mode_confidence"),
        mode_drivers=analysis.get("mode_drivers") or [],
        updated_at=now,
    )

    if existing:
        for key, value in fields.items():
            setattr(existing, key, value)
    else:
        db.add(
            PostReport(
                user_id=user_id,
                platform=post.platform,
                post_id=post.post_id,
                generated_at=now,
                **fields,
            )
        )


async def refresh_user_reports(
    db: AsyncSession,
    *,
    user_id: str,
    settings: Settings,
) -> dict[str, int]:
    """Refresh stored reports for a user across all connected platforms."""
    stmt = select(SocialConnection).where(SocialConnection.user_id == user_id)
    connections = list((await db.execute(stmt)).scalars().all())
    counts: dict[str, int] = {"facebook": 0, "instagram": 0}

    for conn in connections:
        if conn.platform not in {"facebook", "instagram"}:
            continue
        posts = await _fetch_platform_posts(
            platform=conn.platform,
            token=conn.access_token,
            limit=settings.REPORTS_POSTS_PER_PLATFORM,
        )
        posts = [p for p in posts if p.post_id]
        for post in posts:
            analysis = await _llm_analyze_post(post, settings)
            await _upsert_report(db, user_id=user_id, post=post, analysis=analysis)
        conn.last_synced_at = datetime.now(timezone.utc)
        counts[conn.platform] = len(posts)

    await db.commit()
    return counts


async def get_user_reports(
    db: AsyncSession, *, user_id: str
) -> list[PostReport]:
    stmt = (
        select(PostReport)
        .where(PostReport.user_id == user_id)
        .order_by(PostReport.post_created_at.desc().nullslast(), PostReport.updated_at.desc())
    )
    return list((await db.execute(stmt)).scalars().all())


def build_overall_overview(reports: list[PostReport]) -> tuple[list[dict], str | None]:
    by_platform: dict[str, list[PostReport]] = {"facebook": [], "instagram": []}
    for report in reports:
        if report.platform in by_platform:
            by_platform[report.platform].append(report)

    overall: list[dict] = []
    total_posts = 0
    total_positive = 0
    total_negative = 0
    for platform, items in by_platform.items():
        if not items:
            overall.append(
                {
                    "platform": platform,
                    "total_posts": 0,
                    "avg_sentiment": 0.0,
                    "total_likes": 0,
                    "total_comments": 0,
                    "positive": 0,
                    "neutral": 0,
                    "negative": 0,
                }
            )
            continue
        avg = sum((r.sentiment_score or 0.0) for r in items) / len(items)
        likes = sum((r.likes_count or 0) for r in items)
        comments = sum((r.comments_count or 0) for r in items)
        pos = sum(1 for r in items if r.sentiment_label == "positive")
        neg = sum(1 for r in items if r.sentiment_label == "negative")
        neu = sum(1 for r in items if r.sentiment_label == "neutral")
        total_posts += len(items)
        total_positive += pos
        total_negative += neg
        overall.append(
            {
                "platform": platform,
                "total_posts": len(items),
                "avg_sentiment": round(avg, 3),
                "total_likes": likes,
                "total_comments": comments,
                "positive": pos,
                "neutral": neu,
                "negative": neg,
            }
        )

    recommendation: str | None = None
    if total_posts:
        if total_positive >= total_negative * 2 and total_positive > 0:
            recommendation = (
                "Your overall content tone is mostly positive. Keep this voice and "
                "double down on the formats that earn the highest engagement."
            )
        elif total_negative > total_positive:
            recommendation = (
                "Sentiment skews negative across recent posts. Try a calmer tone, "
                "add a clear supportive call-to-action, and balance with a hopeful update."
            )
        else:
            recommendation = (
                "Sentiment is balanced. Experiment with one shorter caption + one direct "
                "question per post to lift engagement."
            )

    return overall, recommendation


# ── Mode decision rollups ────────────────────────────────────────

# Each mode is grouped into a high-level "vibe" used for the dashboard tone.
MODE_VIBE: dict[str, str] = {
    "Energetic": "uplifted",
    "Joyful": "uplifted",
    "Calm": "balanced",
    "Focused": "balanced",
    "Reflective": "balanced",
    "Social": "uplifted",
    "Stressed": "strained",
    "Anxious": "strained",
    "Burned-out": "strained",
    "Withdrawn": "strained",
}


def _weighted_mode_pick(reports: list[PostReport]) -> tuple[str | None, float, list[dict]]:
    """Return (top mode, confidence 0..1, distribution list)."""
    if not reports:
        return None, 0.0, []
    weights: dict[str, float] = {}
    for report in reports:
        if not report.mode_label:
            continue
        weight = float(report.mode_confidence or 0.5)
        weights[report.mode_label] = weights.get(report.mode_label, 0.0) + weight
    if not weights:
        return None, 0.0, []
    total = sum(weights.values()) or 1.0
    distribution = [
        {"label": label, "share": round(value / total, 3), "count": Counter(
            r.mode_label for r in reports if r.mode_label
        ).get(label, 0)}
        for label, value in sorted(weights.items(), key=lambda kv: kv[1], reverse=True)
    ]
    top_label, top_weight = max(weights.items(), key=lambda kv: kv[1])
    confidence = round(top_weight / total, 3)
    return top_label, confidence, distribution


def _filter_recent(reports: list[PostReport], *, days: int) -> list[PostReport]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out: list[PostReport] = []
    for report in reports:
        ref = report.post_created_at or report.updated_at
        if ref is None:
            continue
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=timezone.utc)
        if ref >= cutoff:
            out.append(report)
    return out


def _mode_narrative(label: str | None, confidence: float, count: int, *, period: str) -> str:
    if not label or count == 0:
        return f"Not enough {period} signal yet. Connect more sources or post a few times to unlock a mode read."
    vibe = MODE_VIBE.get(label, "balanced")
    pct = int(round(confidence * 100))
    if vibe == "uplifted":
        return (
            f"Your {period} mode reads as {label} ({pct}% confidence across {count} posts). "
            "Keep this rhythm — your content is energizing your audience."
        )
    if vibe == "strained":
        return (
            f"Your {period} mode skews {label} ({pct}% confidence across {count} posts). "
            "Consider lighter scheduling, a calmer tone, or a recovery break."
        )
    return (
        f"Your {period} mode is {label} ({pct}% confidence across {count} posts). "
        "Steady — a focused, intentional cadence is showing."
    )


def build_mode_summary(reports: list[PostReport], *, period: str, days: int | None) -> dict:
    """Build a mode summary for a window. period is 'current' | 'weekly' | 'monthly'."""
    sliced = reports if days is None else _filter_recent(reports, days=days)
    label, confidence, distribution = _weighted_mode_pick(sliced)
    return {
        "period": period,
        "window_days": days,
        "post_count": len(sliced),
        "mode_label": label,
        "mode_vibe": MODE_VIBE.get(label or "", None),
        "confidence": confidence,
        "distribution": distribution,
        "narrative": _mode_narrative(label, confidence, len(sliced), period=period),
    }

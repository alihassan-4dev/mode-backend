"""
Stack advisor.

Looks at the user's connected platforms, chat-derived mood history, and
dashboard metrics, compresses them to a small JSON payload, and asks
Groq (openai/gpt-oss-120b) to pick the most relevant "stacks" (tools /
next-steps) to surface on the dashboard.

This module is intentionally separate from `dashboard.build_dashboard_summary`
so the data-gathering logic and the LLM-driven recommendation logic stay
decoupled and easy to reason about.

If `GROQ_API_KEY` is not configured, or the Groq call fails for any
reason, a deterministic heuristic fallback is used so the dashboard
always returns something useful.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.user import User
from app.services import chat_store, token_store

logger = logging.getLogger(__name__)

_MAX_RECENT_MESSAGES = 6
_MAX_MESSAGE_CHARS = 160
_MAX_RECOMMENDATIONS = 5


@dataclass
class UserStackSnapshot:
    """Compact, LLM-friendly view of the user's Mode state."""

    user_name: str
    connected_platforms: list[str]
    missing_platforms: list[str]
    platform_details: list[dict]
    recent_user_messages: list[str]
    mood_points: int

    def to_payload(self) -> dict:
        return {
            "user_name": self.user_name,
            "connected_platforms": self.connected_platforms,
            "missing_platforms": self.missing_platforms,
            "platform_details": self.platform_details,
            "recent_user_messages": self.recent_user_messages,
            "mood_points": self.mood_points,
        }


async def build_user_stack_snapshot(db: AsyncSession, user: User) -> UserStackSnapshot:
    """Assemble a compact snapshot of everything the advisor / agent needs.

    Kept small on purpose: raw social posts are NOT included, only
    counts and identifiers. Recent chat messages are truncated.
    """
    connections = await token_store.list_connections(db, user_id=user.id)
    connected_platforms = [c.platform for c in connections]
    missing_platforms = [p for p in ("facebook", "instagram") if p not in connected_platforms]

    platform_details = [
        {
            "platform": c.platform,
            "name": c.platform_name or c.platform_username or c.platform_user_id,
            "last_synced_at": c.last_synced_at.isoformat() if c.last_synced_at else None,
        }
        for c in connections
    ]

    messages = await chat_store.list_recent_messages(db, user_id=user.id, limit=30)
    user_texts: list[str] = []
    for msg in reversed(messages):
        if msg.role != "user":
            continue
        text = msg.content.strip().replace("\n", " ")
        if len(text) > _MAX_MESSAGE_CHARS:
            text = text[: _MAX_MESSAGE_CHARS - 1] + "..."
        user_texts.append(text)
        if len(user_texts) >= _MAX_RECENT_MESSAGES:
            break

    return UserStackSnapshot(
        user_name=user.full_name or user.email.split("@")[0],
        connected_platforms=connected_platforms,
        missing_platforms=missing_platforms,
        platform_details=platform_details,
        recent_user_messages=user_texts,
        mood_points=len([m for m in messages if m.role == "user"]),
    )


def _heuristic_recommendations(snapshot: UserStackSnapshot) -> list[str]:
    """Deterministic fallback used when Groq is unavailable."""
    recs: list[str] = []
    connected = snapshot.connected_platforms
    if not connected:
        recs.append("Connect Facebook or Instagram to start live dashboard analysis.")
    elif len(connected) == 1:
        other = "Instagram" if connected[0] == "facebook" else "Facebook"
        recs.append(f"{connected[0].title()} is connected. Connect {other} to compare signals.")
    else:
        recs.append("Both platforms are connected. Keep sync fresh so the dashboard stays accurate.")

    if snapshot.mood_points:
        recs.append("Your mood journey is based on your recent chat check-ins.")
    else:
        recs.append("Use the chat assistant for short daily check-ins to build a real mood timeline.")
    return recs


def _build_advisor_messages(snapshot: UserStackSnapshot) -> list[dict]:
    system = (
        "You are the Mode Stack Advisor. Given a compact JSON snapshot of one user's "
        "Mode account (connected social platforms, recent chat check-ins, mood data "
        "volume), pick the most relevant next-step recommendations ('stacks') for "
        "their dashboard. Rules: "
        "1) Return STRICT JSON matching {\"recommendations\": [\"string\", ...]}. "
        f"2) Maximum {_MAX_RECOMMENDATIONS} items. "
        "3) Each recommendation is one short actionable sentence (<=140 chars). "
        "4) Do not invent data the user did not provide. "
        "5) If the user has no platform connected, first item must tell them to "
        "connect Facebook or Instagram. "
        "6) Be specific, calm, non-clinical."
    )
    user_payload = json.dumps(snapshot.to_payload(), ensure_ascii=False)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": f"User snapshot JSON:\n{user_payload}"},
    ]


def _parse_recommendations(raw: str) -> list[str]:
    data = json.loads(raw)
    items = data.get("recommendations", []) if isinstance(data, dict) else []
    out: list[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        cleaned = item.strip()
        if cleaned:
            out.append(cleaned[:200])
        if len(out) >= _MAX_RECOMMENDATIONS:
            break
    return out


async def recommend_stacks(db: AsyncSession, user: User) -> list[str]:
    """Return LLM-curated dashboard recommendations; fall back on error."""
    snapshot = await build_user_stack_snapshot(db, user)
    settings = get_settings()

    if not settings.GROQ_API_KEY:
        return _heuristic_recommendations(snapshot)

    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=settings.GROQ_API_KEY, base_url=settings.GROQ_BASE_URL)
        response = await client.chat.completions.create(
            model=settings.GROQ_MODEL,
            temperature=settings.AGENT_TEMPERATURE,
            response_format={"type": "json_object"},
            messages=_build_advisor_messages(snapshot),
        )
        raw = response.choices[0].message.content or "{}"
        recs = _parse_recommendations(raw)
        if recs:
            return recs
        logger.warning("Stack advisor returned empty list; using heuristic fallback")
    except Exception:
        logger.exception("Stack advisor Groq call failed; using heuristic fallback")

    return _heuristic_recommendations(snapshot)

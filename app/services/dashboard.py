from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.schemas.dashboard import DashboardMetric, DashboardSummaryResponse, MoodHistoryPoint, PlatformBreakdownItem
from app.services import chat_store
from app.services import token_store


def _keyword_score(text: str, keywords: set[str]) -> int:
    lower = text.lower()
    return sum(1 for keyword in keywords if keyword in lower)


def _profile_connection_score(connected_count: int) -> float:
    if connected_count <= 0:
        return 0.0
    return min(100.0, connected_count * 50.0)


def _score_text_mood(text: str) -> tuple[float, float]:
    positive = _keyword_score(text, {"calm", "focus", "balanced", "strong", "good", "well", "happy", "better"})
    caution = _keyword_score(text, {"stress", "tired", "burnout", "anxious", "sad", "low", "down"})
    mood = max(1.0, min(10.0, 5.8 + positive * 0.6 - caution * 0.8))
    energy = max(1.0, min(10.0, 6.0 + positive * 0.4 - caution * 0.7))
    return round(mood, 1), round(energy, 1)


async def _build_mood_history(db: AsyncSession, user: User) -> list[MoodHistoryPoint]:
    messages = await chat_store.list_recent_messages(db, user_id=user.id, limit=30)
    user_messages = [message for message in reversed(messages) if message.role == "user"]
    history: list[MoodHistoryPoint] = []

    for message in user_messages[-14:]:
        mood, energy = _score_text_mood(message.content)
        history.append(
            MoodHistoryPoint(
                date=message.created_at.strftime("%b %d"),
                mood=mood,
                energy=energy,
            )
        )

    return history


async def build_dashboard_summary(db: AsyncSession, user: User) -> DashboardSummaryResponse:
    connections = await token_store.list_connections(db, user_id=user.id)
    connected_count = len(connections)
    connection_score = _profile_connection_score(connected_count)

    profile_text = " ".join(
        filter(
            None,
            [
                user.full_name or "",
                " ".join(connection.platform for connection in connections),
            ],
        )
    )
    positive = _keyword_score(profile_text, {"calm", "focus", "balanced", "strong", "good", "well"})
    caution = _keyword_score(profile_text, {"stress", "tired", "burnout", "anxious", "sad"})

    if connected_count:
        mood_score = max(35.0, min(92.0, 55.0 + connected_count * 10 + positive * 3 - caution * 4))
        stress_score = max(8.0, min(78.0, 32.0 - connected_count * 5 + caution * 7))
        readiness_score = max(30.0, min(96.0, (mood_score + (100 - stress_score) + connection_score) / 3))
    else:
        mood_score = 0.0
        stress_score = 0.0
        readiness_score = 0.0

    platform_breakdown = [
        PlatformBreakdownItem(
            platform=platform,
            connected=False,
            activity_count=0,
            summary=f"{platform.title()} is not connected yet.",
        )
        for platform in ("facebook", "instagram")
    ]

    connection_map = {connection.platform: connection for connection in connections}
    for item in platform_breakdown:
        connection = connection_map.get(item.platform)
        if not connection:
            continue
        item.connected = True
        item.activity_count = 1
        item.connected_at = connection.connected_at
        item.last_synced_at = connection.last_synced_at
        item.summary = (
            f"{item.platform.title()} connected as "
            f"{connection.platform_name or connection.platform_username or connection.platform_user_id}."
        )

    mood_history = await _build_mood_history(db, user)

    recommendations = []
    if connected_count == 0:
        recommendations.append("Connect Facebook or Instagram to start live dashboard analysis.")
    elif connected_count == 1:
        connected_platform = connections[0].platform.title()
        recommendations.append(f"{connected_platform} is connected. Connect the second platform for comparison.")
    else:
        recommendations.append("Both platforms are connected. Keep sync fresh so the dashboard stays accurate.")

    if mood_history:
        recommendations.append("Your mood journey is based on your recent chat check-ins.")
    else:
        recommendations.append("Use the chat assistant for short daily check-ins to build a real mood timeline.")

    return DashboardSummaryResponse(
        user_id=user.id,
        generated_at=datetime.now(timezone.utc),
        metrics=[
            DashboardMetric(
                label="Mood score",
                value=round(mood_score, 1),
                display_value=f"{round(mood_score, 1)}/100" if connected_count else "0/100",
                trend="stable",
                detail=(
                    "Estimated from connected data sources and recent account context."
                    if connected_count
                    else "No connected data sources yet."
                ),
            ),
            DashboardMetric(
                label="Stress risk",
                value=round(stress_score, 1),
                display_value=f"{round(stress_score, 1)}%" if connected_count else "0%",
                trend="monitor",
                detail=(
                    "A lower score is better. This is currently a heuristic, not a medical assessment."
                    if connected_count
                    else "No live platform signal is available yet."
                ),
            ),
            DashboardMetric(
                label="Readiness",
                value=round(readiness_score, 1),
                display_value=f"{round(readiness_score, 1)}%",
                trend="up",
                detail="Combines mood estimate, stress risk, and data coverage.",
            ),
            DashboardMetric(
                label="Connected sources",
                value=float(connected_count),
                display_value=str(connected_count),
                trend="up" if connected_count else "flat",
                detail="Number of active platform integrations feeding the dashboard.",
            ),
        ],
        platform_breakdown=platform_breakdown,
        mood_history=mood_history,
        recommendations=recommendations,
    )

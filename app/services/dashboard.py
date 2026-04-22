from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.schemas.dashboard import (
    DashboardMetric,
    DashboardSummaryResponse,
    PlatformBreakdownItem,
)
from app.services import token_store


def _keyword_score(text: str, keywords: set[str]) -> int:
    lower = text.lower()
    return sum(1 for keyword in keywords if keyword in lower)


async def build_dashboard_summary(db: AsyncSession, user: User) -> DashboardSummaryResponse:
    connections = await token_store.list_connections(db, user_id=user.id)
    connected_count = len(connections)
    connection_score = min(100, connected_count * 35)

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

    mood_score = max(42.0, min(92.0, 58.0 + connected_count * 8 + positive * 3 - caution * 4))
    stress_score = max(8.0, min(78.0, 30.0 - connected_count * 4 + caution * 7))
    readiness_score = max(35.0, min(96.0, (mood_score + (100 - stress_score) + connection_score) / 3))

    platform_breakdown = [
        PlatformBreakdownItem(
            platform=platform,
            connected=False,
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
        item.connected_at = connection.connected_at
        item.last_synced_at = connection.last_synced_at
        item.summary = (
            f"{item.platform.title()} connected as "
            f"{connection.platform_name or connection.platform_username or connection.platform_user_id}."
        )

    recommendations = [
        "Connect both social accounts so the dashboard can compare activity across channels."
        if connected_count < 2
        else "Keep social sync fresh so the dashboard stays accurate."
    ]
    recommendations.append(
        "Use the chat assistant for short daily check-ins to build a richer wellness timeline."
    )

    return DashboardSummaryResponse(
        user_id=user.id,
        generated_at=datetime.now(timezone.utc),
        metrics=[
            DashboardMetric(
                label="Mood score",
                value=round(mood_score, 1),
                display_value=f"{round(mood_score, 1)}/100",
                trend="stable",
                detail="Estimated from connected data sources and recent account context.",
            ),
            DashboardMetric(
                label="Stress risk",
                value=round(stress_score, 1),
                display_value=f"{round(stress_score, 1)}%",
                trend="monitor",
                detail="A lower score is better. This is currently a heuristic, not a medical assessment.",
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
        recommendations=recommendations,
    )

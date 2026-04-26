from __future__ import annotations

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.services import dashboard as dashboard_service
from app.services import token_store


class PlatformLookupInput(BaseModel):
    platform: str = Field(description="facebook or instagram")


class WellnessSummaryInput(BaseModel):
    include_recommendations: bool = Field(
        default=True,
        description="Whether to include next-step recommendations.",
    )


class UserModeInput(BaseModel):
    detail_level: str = Field(
        default="short",
        description="short or detailed user mode explanation.",
    )


async def build_agent_tools(db: AsyncSession, user: User):
    from langchain_core.tools import tool

    @tool("get_connected_platform", args_schema=PlatformLookupInput)
    async def get_connected_platform(platform: str) -> str:
        """Look up whether a social platform is connected and when it was last synced."""
        normalized = platform.strip().lower()
        if normalized not in {"facebook", "instagram"}:
            return "Supported platforms are facebook and instagram."

        connection = await token_store.get_connection(db, user_id=user.id, platform=normalized)
        if not connection:
            return f"{normalized.title()} is not connected. Activity count is 0."

        account_name = connection.platform_name or connection.platform_username or connection.platform_user_id
        synced_at = connection.last_synced_at.isoformat() if connection.last_synced_at else "never"
        return f"{normalized.title()} is connected as {account_name}. Activity count is 1. Last synced at {synced_at}."

    @tool("get_wellness_summary", args_schema=WellnessSummaryInput)
    async def get_wellness_summary(include_recommendations: bool = True) -> str:
        """Return the latest dashboard summary, real platform counts, mood history state, and recommendations."""
        summary = await dashboard_service.build_dashboard_summary(
            db,
            user,
            include_ai=False,
            include_live_activity=False,
        )
        lines = [
            f"generated_at={summary.generated_at.isoformat()}",
            f"user_id={summary.user_id}",
        ]
        lines.extend(f"{metric.label}: {metric.display_value} - {metric.detail}" for metric in summary.metrics)
        lines.extend(
            f"{item.platform.title()}: {'connected' if item.connected else 'not connected'}, "
            f"activity_count={item.activity_count}, "
            f"connected_at={item.connected_at.isoformat() if item.connected_at else 'n/a'}, "
            f"last_synced_at={item.last_synced_at.isoformat() if item.last_synced_at else 'never'}"
            for item in summary.platform_breakdown
        )
        lines.append(f"Mood history points: {len(summary.mood_history)}")
        if include_recommendations:
            lines.extend(summary.recommendations)
        return "\n".join(lines)

    @tool("explain_user_mode", args_schema=UserModeInput)
    async def explain_user_mode(detail_level: str = "short") -> str:
        """Explain how Mode understands the current user's dashboard state."""
        summary = await dashboard_service.build_dashboard_summary(
            db,
            user,
            include_ai=False,
            include_live_activity=False,
        )
        connected = [item.platform.title() for item in summary.platform_breakdown if item.connected]
        missing = [item.platform.title() for item in summary.platform_breakdown if not item.connected]
        base = (
            f"Connected sources: {', '.join(connected) if connected else 'none'}. "
            f"Missing sources: {', '.join(missing) if missing else 'none'}. "
            f"Mood history points: {len(summary.mood_history)}."
        )
        if detail_level.strip().lower() != "detailed":
            return base
        return base + " Mode only shows charts from real connected data or real chat check-ins; it does not invent history."

    @tool("get_current_user_context")
    async def get_current_user_context() -> str:
        """Return concise current user profile and integration context for personalized answers."""
        connections = await token_store.list_connections(db, user_id=user.id)
        connection_items = []
        for connection in connections:
            account_name = connection.platform_name or connection.platform_username or connection.platform_user_id
            connection_items.append(
                f"{connection.platform}: account={account_name}, "
                f"last_synced_at={connection.last_synced_at.isoformat() if connection.last_synced_at else 'never'}"
            )
        if not connection_items:
            connection_items.append("no connected integrations")
        display_name = user.full_name or user.email.split("@")[0]
        return (
            f"user_id={user.id}\n"
            f"name={display_name}\n"
            f"email={user.email}\n"
            f"integrations={'; '.join(connection_items)}"
        )

    return [get_connected_platform, get_wellness_summary, explain_user_mode, get_current_user_context]

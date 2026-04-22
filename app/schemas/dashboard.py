from datetime import datetime

from pydantic import BaseModel


class DashboardMetric(BaseModel):
    label: str
    value: float
    display_value: str
    trend: str
    detail: str


class PlatformBreakdownItem(BaseModel):
    platform: str
    connected: bool
    connected_at: datetime | None = None
    last_synced_at: datetime | None = None
    summary: str


class DashboardSummaryResponse(BaseModel):
    user_id: str
    generated_at: datetime
    metrics: list[DashboardMetric]
    platform_breakdown: list[PlatformBreakdownItem]
    recommendations: list[str]

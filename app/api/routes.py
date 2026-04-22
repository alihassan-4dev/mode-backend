"""Core HTTP routes mounted under `/api`."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.deps import get_current_user
from app.models.user import User
from app.schemas.dashboard import DashboardSummaryResponse
from app.services.dashboard import build_dashboard_summary

router = APIRouter(tags=["core"])


@router.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "mode-prediction-api",
        "time": datetime.now(UTC).isoformat(),
    }


@router.get("/version")
def version() -> dict:
    return {"version": "2.0.0"}


@router.get("/me")
async def me(user: User = Depends(get_current_user)) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "createdAt": user.created_at,
        "metadata": {"full_name": user.full_name} if user.full_name else {},
    }


@router.get("/dashboard/summary", response_model=DashboardSummaryResponse)
async def dashboard_summary(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DashboardSummaryResponse:
    return await build_dashboard_summary(db, user)

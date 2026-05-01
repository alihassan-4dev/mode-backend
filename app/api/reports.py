from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.deps import get_current_user
from app.models.user import User
from app.schemas.reports import (
    ModeSummary,
    OverallSentimentPoint,
    PlatformOverview,
    PostReportOut,
    ReportsResponse,
)
from app.services import reports as reports_service
from app.services.report_scheduler import scheduler_state

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/reports", tags=["reports"])


def _to_out(report) -> PostReportOut:
    return PostReportOut(
        platform=report.platform,
        post_id=report.post_id,
        post_text=report.post_text,
        permalink=report.permalink,
        media_type=report.media_type,
        media_url=report.media_url,
        post_created_at=report.post_created_at,
        likes_count=report.likes_count,
        comments_count=report.comments_count,
        sentiment_label=report.sentiment_label,
        sentiment_score=report.sentiment_score,
        engagement_quality=report.engagement_quality,
        engagement_score=report.engagement_score,
        recommendation=report.recommendation,
        summary=report.summary,
        tone=report.tone,
        topics=report.topics or [],
        strengths=report.strengths or [],
        weaknesses=report.weaknesses or [],
        mode_label=report.mode_label,
        mode_confidence=report.mode_confidence,
        mode_drivers=report.mode_drivers or [],
        generated_at=report.generated_at,
        updated_at=report.updated_at,
    )


@router.get("", response_model=ReportsResponse)
async def get_reports(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    settings = get_settings()
    reports = await reports_service.get_user_reports(db, user_id=user.id)

    # If the user has no reports yet, generate them on-demand so the page is
    # never empty for a first-time visitor with connected platforms.
    if not reports:
        try:
            await reports_service.refresh_user_reports(
                db, user_id=user.id, settings=settings
            )
            reports = await reports_service.get_user_reports(db, user_id=user.id)
        except Exception:
            logger.exception("On-demand reports refresh failed for user_id=%s", user.id)

    fb = [_to_out(r) for r in reports if r.platform == "facebook"]
    ig = [_to_out(r) for r in reports if r.platform == "instagram"]
    overview_dicts, overall_recommendation = reports_service.build_overall_overview(reports)
    overview = [PlatformOverview(**item) for item in overview_dicts]

    current = ModeSummary(
        **reports_service.build_mode_summary(reports, period="current", days=None)
    )
    weekly = ModeSummary(
        **reports_service.build_mode_summary(reports, period="weekly", days=7)
    )
    monthly = ModeSummary(
        **reports_service.build_mode_summary(reports, period="monthly", days=30)
    )

    return ReportsResponse(
        user_id=user.id,
        generated_at=datetime.now(timezone.utc),
        refresh_interval_minutes=settings.REPORTS_REFRESH_MINUTES,
        next_refresh_at=scheduler_state.next_run_at,
        facebook=fb,
        instagram=ig,
        overall=overview,
        overall_recommendation=overall_recommendation,
        current_mode=current,
        weekly_mode=weekly,
        monthly_mode=monthly,
    )


@router.post("/refresh", response_model=ReportsResponse)
async def refresh_reports(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    settings = get_settings()
    try:
        await reports_service.refresh_user_reports(
            db, user_id=user.id, settings=settings
        )
    except Exception as exc:
        logger.exception("Manual reports refresh failed user_id=%s", user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not refresh reports right now.",
        ) from exc
    return await get_reports(user=user, db=db)  # type: ignore[arg-type]


class ModeOverview(ReportsResponse):
    """Reused as the response of /reports/mode for the dashboard."""


@router.get("/mode", response_model=dict)
async def get_mode_overview(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Lightweight mode read for the dashboard (current + weekly + monthly)."""
    reports = await reports_service.get_user_reports(db, user_id=user.id)
    return {
        "current": reports_service.build_mode_summary(reports, period="current", days=None),
        "weekly": reports_service.build_mode_summary(reports, period="weekly", days=7),
        "monthly": reports_service.build_mode_summary(reports, period="monthly", days=30),
    }


# Re-export to silence unused import warning for the schema module.
_ = OverallSentimentPoint

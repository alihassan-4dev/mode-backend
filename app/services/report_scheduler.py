"""
Background scheduler that refreshes per-user social-post reports on a
fixed cadence. The interval is read from `Settings.REPORTS_REFRESH_MINUTES`.

The scheduler runs as a single asyncio task started in the FastAPI lifespan.
It picks up every user that has at least one social connection and rebuilds
their post reports through `reports.refresh_user_reports`. Failures for a
single user do not stop the loop.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import get_async_session_factory
from app.models.social_connection import SocialConnection
from app.services import reports as reports_service

logger = logging.getLogger(__name__)


@dataclass
class _SchedulerState:
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    last_status: str = "pending"
    users_processed: int = 0
    errors: list[str] = field(default_factory=list)


scheduler_state = _SchedulerState()
_task: asyncio.Task | None = None
_stop_event: asyncio.Event | None = None


async def _run_one_cycle() -> None:
    settings = get_settings()
    factory = get_async_session_factory()
    if factory is None:
        logger.warning("Reports scheduler skipped: DB factory not initialized.")
        return

    async with factory() as db:
        stmt = select(SocialConnection.user_id).distinct()
        user_ids = [row[0] for row in (await db.execute(stmt)).all()]

    processed = 0
    errors: list[str] = []
    for user_id in user_ids:
        try:
            async with factory() as db:
                await reports_service.refresh_user_reports(
                    db, user_id=user_id, settings=settings
                )
            processed += 1
        except Exception as exc:
            logger.exception("Scheduler refresh failed user_id=%s", user_id)
            errors.append(f"{user_id}: {exc}")

    scheduler_state.last_run_at = datetime.now(timezone.utc)
    scheduler_state.users_processed = processed
    scheduler_state.errors = errors[-10:]
    scheduler_state.last_status = "ok" if not errors else "partial"


async def _scheduler_loop() -> None:
    settings = get_settings()
    interval_minutes = max(1, int(settings.REPORTS_REFRESH_MINUTES))
    delay = interval_minutes * 60
    logger.info("Reports scheduler started (every %d min).", interval_minutes)

    assert _stop_event is not None
    # Initial small delay so the API is fully up before the first cycle.
    try:
        await asyncio.wait_for(_stop_event.wait(), timeout=10)
        return
    except asyncio.TimeoutError:
        pass

    while not _stop_event.is_set():
        scheduler_state.next_run_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
        try:
            await _run_one_cycle()
        except Exception:
            logger.exception("Reports scheduler cycle crashed")
            scheduler_state.last_status = "error"
        try:
            await asyncio.wait_for(_stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            continue


def start_scheduler() -> None:
    global _task, _stop_event
    if _task is not None and not _task.done():
        return
    _stop_event = asyncio.Event()
    _task = asyncio.create_task(_scheduler_loop(), name="reports-scheduler")


async def stop_scheduler() -> None:
    global _task, _stop_event
    if _stop_event is not None:
        _stop_event.set()
    if _task is not None:
        try:
            await asyncio.wait_for(_task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            _task.cancel()
    _task = None
    _stop_event = None

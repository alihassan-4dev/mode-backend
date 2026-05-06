from __future__ import annotations

import logging
import logging.config
import time
from collections.abc import Awaitable, Callable

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app.core.config import get_settings


class RequestLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        logger = logging.getLogger("app.http")
        started_at = time.perf_counter()
        client_host = request.client.host if request.client else "-"

        try:
            response = await call_next(request)
        except Exception:
            duration_ms = (time.perf_counter() - started_at) * 1000
            logger.exception(
                "HTTP %s %s -> 500 %.2fms client=%s",
                request.method,
                request.url.path,
                duration_ms,
                client_host,
            )
            raise

        duration_ms = (time.perf_counter() - started_at) * 1000
        logger.info(
            "HTTP %s %s -> %s %.2fms client=%s",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
            client_host,
        )
        return response


def setup_logging() -> None:
    settings = get_settings()
    level_name = settings.LOG_LEVEL.strip().upper() or "INFO"
    level = getattr(logging, level_name, logging.INFO)

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "standard": {
                    "format": "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                    "datefmt": "%Y-%m-%d %H:%M:%S",
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "standard",
                    "level": level_name,
                }
            },
            "root": {
                "handlers": ["console"],
                "level": level_name,
            },
            "loggers": {
                "uvicorn": {"level": level_name, "propagate": True},
                "uvicorn.error": {"level": level_name, "propagate": True},
                "uvicorn.access": {"handlers": ["console"], "level": level_name, "propagate": False},
            },
        }
    )

    logging.getLogger(__name__).debug("Logging configured at level %s (%s).", level_name, level)

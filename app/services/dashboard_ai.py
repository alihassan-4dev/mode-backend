from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from openai import APIConnectionError, APITimeoutError, RateLimitError

from app.core.config import Settings

logger = logging.getLogger(__name__)

MAX_INPUT_CHARS = 24_000
CHUNK_TARGET_CHARS = 5_000
MAX_CHUNKS = 6
MAX_LLM_RETRIES = 4
_RETRY_DELAY_RE = re.compile(r"try again in ([0-9]+(?:\.[0-9]+)?)(ms|s)", re.IGNORECASE)


@dataclass
class DashboardAiOutput:
    mood_score: float
    stress_risk: float
    readiness: float
    recommendations: list[str]
    mood_history: list[dict[str, Any]]


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _chunk_text_lines(lines: list[str], target_chars: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    size = 0

    for line in lines:
        line_size = len(line) + 1
        if current and size + line_size > target_chars:
            chunks.append("\n".join(current))
            current = []
            size = 0
        current.append(line)
        size += line_size
        if len(chunks) >= MAX_CHUNKS:
            break

    if current and len(chunks) < MAX_CHUNKS:
        chunks.append("\n".join(current))
    return chunks


def _safe_json_loads(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return {}
    return {}


def _build_llm(settings: Settings) -> ChatOpenAI:
    return ChatOpenAI(
        api_key=settings.GROQ_API_KEY,
        base_url=settings.GROQ_BASE_URL,
        model=settings.GROQ_MODEL,
        temperature=0.1,
        timeout=20.0,
    )


def _extract_retry_after_seconds(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers:
        retry_after = headers.get("retry-after")
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass

    match = _RETRY_DELAY_RE.search(str(exc))
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2).lower()
    return value / 1000.0 if unit == "ms" else value


def _backoff_seconds(attempt: int, exc: Exception) -> float:
    hinted = _extract_retry_after_seconds(exc)
    if hinted is not None:
        return min(8.0, hinted + random.uniform(0.05, 0.2))
    base = min(8.0, 0.5 * (2 ** (attempt - 1)))
    return base + random.uniform(0.05, 0.3)


async def _ainvoke_with_retry(
    llm: ChatOpenAI,
    messages: list[SystemMessage | HumanMessage],
    *,
    operation: str,
) -> Any:
    for attempt in range(1, MAX_LLM_RETRIES + 1):
        try:
            return await llm.ainvoke(messages)
        except (RateLimitError, APIConnectionError, APITimeoutError) as exc:
            if attempt >= MAX_LLM_RETRIES:
                raise
            delay = _backoff_seconds(attempt, exc)
            logger.warning(
                "Dashboard AI %s transient failure (%s), retry %d/%d in %.2fs",
                operation,
                exc.__class__.__name__,
                attempt,
                MAX_LLM_RETRIES,
                delay,
            )
            await asyncio.sleep(delay)


async def _summarize_chunk(
    llm: ChatOpenAI,
    *,
    chunk_text: str,
    idx: int,
    total: int,
) -> dict[str, Any]:
    system = SystemMessage(
        content=(
            "You are a strict JSON analyzer. Return compact JSON only with keys: "
            "sentiment_balance (-100..100), stress_signal (0..100), energy_signal (0..100), "
            "main_themes (array of short strings), key_risks (array of short strings)."
        )
    )
    user = HumanMessage(
        content=(
            f"Analyze chunk {idx}/{total}.\n"
            "Input text:\n"
            f"{chunk_text}\n\n"
            "Return JSON only."
        )
    )
    response = await _ainvoke_with_retry(
        llm,
        [system, user],
        operation=f"chunk-summary-{idx}-{total}",
    )
    payload = response.content if isinstance(response.content, str) else "{}"
    return _safe_json_loads(payload)


async def generate_ai_dashboard_output(
    *,
    settings: Settings,
    user_context: dict[str, Any],
    connected_platforms: list[str],
    chat_messages: list[dict[str, str]],
) -> DashboardAiOutput | None:
    """
    Generate AI-backed dashboard metrics using Groq.

    Returns None on any recoverable error so callers can safely fallback.
    """
    if not settings.GROQ_API_KEY:
        return None

    try:
        llm = _build_llm(settings)

        message_lines = [
            f"- [{item.get('created_at', '')}] {item.get('role', '')}: {item.get('content', '')}"
            for item in chat_messages
            if item.get("content")
        ]
        full_text = "\n".join(message_lines)[:MAX_INPUT_CHARS]
        chunks = _chunk_text_lines(full_text.splitlines(), CHUNK_TARGET_CHARS) if full_text else []

        chunk_signals: list[dict[str, Any]] = []
        for i, chunk in enumerate(chunks, start=1):
            chunk_signals.append(await _summarize_chunk(llm, chunk_text=chunk, idx=i, total=len(chunks)))

        system = SystemMessage(
            content=(
                "You are a production dashboard scoring engine. Return strict JSON only with keys:\n"
                "mood_score (0..100), stress_risk (0..100), readiness (0..100), "
                "recommendations (array of max 4 concise strings), "
                "mood_history (array of objects: {date, mood, energy} where mood and energy are 1..10).\n"
                "Rules:\n"
                "- Keep values conservative and realistic.\n"
                "- Higher stress means worse state.\n"
                "- If signals are weak, stay near neutral values.\n"
                "- mood_history length must be <= 14.\n"
                "- Output JSON only."
            )
        )
        user = HumanMessage(
            content=json.dumps(
                {
                    "user_context": user_context,
                    "connected_platforms": connected_platforms,
                    "chat_message_count": len(chat_messages),
                    "chunk_signals": chunk_signals,
                },
                ensure_ascii=True,
            )
        )
        response = await _ainvoke_with_retry(llm, [system, user], operation="final-summary")
        payload = response.content if isinstance(response.content, str) else "{}"
        data = _safe_json_loads(payload)
        if not data:
            return None

        mood_score = _clamp(_coerce_float(data.get("mood_score"), 50.0), 0.0, 100.0)
        stress_risk = _clamp(_coerce_float(data.get("stress_risk"), 35.0), 0.0, 100.0)
        readiness = _clamp(_coerce_float(data.get("readiness"), 50.0), 0.0, 100.0)

        recommendations_raw = data.get("recommendations")
        recommendations = (
            [str(item).strip() for item in recommendations_raw if str(item).strip()][:4]
            if isinstance(recommendations_raw, list)
            else []
        )

        mood_history_raw = data.get("mood_history")
        mood_history: list[dict[str, Any]] = []
        if isinstance(mood_history_raw, list):
            for point in mood_history_raw[:14]:
                if not isinstance(point, dict):
                    continue
                date_value = str(point.get("date", "")).strip() or "Now"
                mood_value = _clamp(_coerce_float(point.get("mood"), 5.0), 1.0, 10.0)
                energy_value = _clamp(_coerce_float(point.get("energy"), 5.0), 1.0, 10.0)
                mood_history.append(
                    {
                        "date": date_value,
                        "mood": round(mood_value, 1),
                        "energy": round(energy_value, 1),
                    }
                )

        return DashboardAiOutput(
            mood_score=round(mood_score, 1),
            stress_risk=round(stress_risk, 1),
            readiness=round(readiness, 1),
            recommendations=recommendations,
            mood_history=mood_history,
        )
    except (RateLimitError, APIConnectionError, APITimeoutError):
        logger.warning("AI dashboard generation skipped after transient provider failures")
        return None
    except Exception:
        logger.exception("AI dashboard generation failed")
        return None

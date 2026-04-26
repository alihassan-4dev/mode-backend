from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.mark.asyncio
async def test_generate_reply_falls_back_when_agent_raises(monkeypatch: pytest.MonkeyPatch):
    from app.services import agent_service

    async def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("tool_use_failed")

    monkeypatch.setattr(agent_service, "run_agent", _boom)

    result = await agent_service.generate_reply(
        db=None,
        user=None,
        message="Can you check my dashboard?",
        history=[],
    )
    assert result.provider == "fallback"
    assert "dashboard" in result.content.lower()

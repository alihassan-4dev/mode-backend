from pathlib import Path
import sys

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture()
def test_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("USE_LOCAL_SQLITE", "false")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-with-32-characters")
    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setenv("META_APP_ID", "")
    monkeypatch.setenv("META_APP_SECRET", "")
    monkeypatch.setenv("META_STATE_SECRET", "test-meta-state-secret")

    from app.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest_asyncio.fixture()
async def client(test_env):
    from app.core.database import get_async_engine
    from app.models.social_connection import SocialConnection
    from app.models.user import User
    from app.services import chat_store
    from main import app

    chat_store.reset_memory_store()
    engine = get_async_engine()
    assert engine is not None
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: User.__table__.create(sync_conn, checkfirst=True))
        await conn.run_sync(lambda sync_conn: SocialConnection.__table__.create(sync_conn, checkfirst=True))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as api_client:
        yield api_client

    await engine.dispose()


@pytest.mark.asyncio
async def test_auth_dashboard_and_chat_flow(client: AsyncClient):
    register_response = await client.post(
        "/api/auth/register",
        json={
            "email": "person@example.com",
            "password": "strongpass123",
            "full_name": "Ali Tester",
        },
    )
    assert register_response.status_code == 200
    access_token = register_response.json()["access_token"]
    headers = {"Authorization": f"Bearer {access_token}"}

    me_response = await client.get("/api/me", headers=headers)
    assert me_response.status_code == 200
    assert me_response.json()["email"] == "person@example.com"

    dashboard_response = await client.get("/api/dashboard/summary", headers=headers)
    assert dashboard_response.status_code == 200
    dashboard_payload = dashboard_response.json()
    assert len(dashboard_payload["metrics"]) == 4
    assert dashboard_payload["platform_breakdown"][0]["platform"] == "facebook"

    chat_response = await client.post(
        "/api/chat/message",
        headers=headers,
        json={"message": "I feel stressed today and want a quick summary."},
    )
    assert chat_response.status_code == 200
    chat_payload = chat_response.json()
    assert chat_payload["provider"] == "fallback"
    assert chat_payload["reply"]["role"] == "assistant"
    assert "stress" in chat_payload["reply"]["content"].lower()

    sessions_response = await client.get("/api/chat/sessions", headers=headers)
    assert sessions_response.status_code == 200
    sessions_payload = sessions_response.json()
    assert len(sessions_payload) == 1

    session_id = sessions_payload[0]["id"]
    session_detail_response = await client.get(f"/api/chat/sessions/{session_id}", headers=headers)
    assert session_detail_response.status_code == 200
    assert len(session_detail_response.json()["messages"]) == 2


@pytest.mark.asyncio
async def test_chat_reuses_client_provided_session_uuid(client: AsyncClient):
    register_response = await client.post(
        "/api/auth/register",
        json={
            "email": "session@example.com",
            "password": "strongpass123",
            "full_name": "Session Tester",
        },
    )
    assert register_response.status_code == 200
    access_token = register_response.json()["access_token"]
    headers = {"Authorization": f"Bearer {access_token}"}
    session_id = "4d6a2b55-4d08-4108-a521-0d6cbcf4a4cf"

    first_message_response = await client.post(
        "/api/chat/message",
        headers=headers,
        json={
            "message": "Start a new focused chat for me.",
            "session_id": session_id,
        },
    )
    assert first_message_response.status_code == 200
    first_payload = first_message_response.json()
    assert first_payload["session"]["id"] == session_id

    second_message_response = await client.post(
        "/api/chat/message",
        headers=headers,
        json={
            "message": "Continue the same chat.",
            "session_id": session_id,
        },
    )
    assert second_message_response.status_code == 200
    second_payload = second_message_response.json()
    assert second_payload["session"]["id"] == session_id
    assert len(second_payload["session"]["messages"]) == 4


@pytest.mark.asyncio
async def test_integration_authorize_requires_and_uses_meta_config(client: AsyncClient, test_env, monkeypatch: pytest.MonkeyPatch):
    login_response = await client.post(
        "/api/auth/register",
        json={
            "email": "meta@example.com",
            "password": "strongpass123",
            "full_name": "Meta Tester",
        },
    )
    assert login_response.status_code == 200
    access_token = login_response.json()["access_token"]
    headers = {"Authorization": f"Bearer {access_token}"}

    missing_config_response = await client.get("/api/integrations/facebook/authorize", headers=headers)
    assert missing_config_response.status_code == 503
    assert missing_config_response.json()["detail"] == "Meta login is not configured yet. Set META_APP_ID and META_APP_SECRET."

    monkeypatch.setenv("META_APP_ID", "meta-app-id")
    monkeypatch.setenv("META_APP_SECRET", "meta-app-secret")
    from app.core.config import get_settings
    get_settings.cache_clear()

    facebook_response = await client.get("/api/integrations/facebook/authorize", headers=headers)
    assert facebook_response.status_code == 200
    assert "facebook.com" in facebook_response.json()["url"]
    assert "client_id=meta-app-id" in facebook_response.json()["url"]
    assert "redirect_uri=http%3A%2F%2Ftestserver%2Fapi%2Fintegrations%2Ffacebook%2Fcallback" in facebook_response.json()["url"]

    instagram_response = await client.get("/api/integrations/instagram/authorize", headers=headers)
    assert instagram_response.status_code == 200
    assert "api.instagram.com" in instagram_response.json()["url"]
    assert "client_id=meta-app-id" in instagram_response.json()["url"]

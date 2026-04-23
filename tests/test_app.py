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
    monkeypatch.setenv("FB_APP_ID", "")
    monkeypatch.setenv("FB_APP_SECRET", "")
    monkeypatch.setenv("IG_APP_ID", "")
    monkeypatch.setenv("IG_APP_SECRET", "")
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
async def test_dashboard_reports_missing_platform_as_zero(client: AsyncClient):
    register_response = await client.post(
        "/api/auth/register",
        json={
            "email": "dashboard@example.com",
            "password": "strongpass123",
            "full_name": "Dashboard Tester",
        },
    )
    assert register_response.status_code == 200
    access_token = register_response.json()["access_token"]
    headers = {"Authorization": f"Bearer {access_token}"}

    from app.core.database import get_async_session_factory
    from app.services import token_store

    sessionmaker = get_async_session_factory()
    assert sessionmaker is not None
    async with sessionmaker() as db:
        await token_store.upsert_connection(
            db,
            user_id=register_response.json()["user"]["id"],
            platform="facebook",
            platform_user_id="facebook-user-id",
            access_token="token",
            platform_name="Facebook Person",
        )

    dashboard_response = await client.get("/api/dashboard/summary", headers=headers)
    assert dashboard_response.status_code == 200
    platforms = {item["platform"]: item for item in dashboard_response.json()["platform_breakdown"]}
    assert platforms["facebook"]["connected"] is True
    assert platforms["facebook"]["activity_count"] == 1
    assert platforms["instagram"]["connected"] is False
    assert platforms["instagram"]["activity_count"] == 0


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
    assert "Facebook login is not configured yet." in missing_config_response.json()["detail"]

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
    assert "instagram.com/oauth/authorize" in instagram_response.json()["url"]
    assert "client_id=meta-app-id" in instagram_response.json()["url"]
    assert "instagram_business_basic" in instagram_response.json()["url"]

    monkeypatch.setenv("IG_APP_ID", "ig-app-id")
    monkeypatch.setenv("IG_APP_SECRET", "ig-app-secret")
    monkeypatch.setenv("INSTAGRAM_REDIRECT_URI", "https://api.example.com/api/integrations/instagram/callback")
    get_settings.cache_clear()

    instagram_response_with_specific_creds = await client.get("/api/integrations/instagram/authorize", headers=headers)
    assert instagram_response_with_specific_creds.status_code == 200
    assert "client_id=ig-app-id" in instagram_response_with_specific_creds.json()["url"]
    assert "instagram.com/oauth/authorize" in instagram_response_with_specific_creds.json()["url"]
    assert "redirect_uri=https%3A%2F%2Fapi.example.com%2Fapi%2Fintegrations%2Finstagram%2Fcallback" in instagram_response_with_specific_creds.json()["url"]


@pytest.mark.asyncio
async def test_instagram_callback_provider_error_redirects_cleanly(client: AsyncClient, monkeypatch: pytest.MonkeyPatch):
    register_response = await client.post(
        "/api/auth/register",
        json={
            "email": "igerror@example.com",
            "password": "strongpass123",
            "full_name": "IG Error Tester",
        },
    )
    assert register_response.status_code == 200
    access_token = register_response.json()["access_token"]
    headers = {"Authorization": f"Bearer {access_token}"}

    monkeypatch.setenv("META_APP_ID", "meta-app-id")
    monkeypatch.setenv("META_APP_SECRET", "meta-app-secret")
    from app.core.config import get_settings

    get_settings.cache_clear()

    response = await client.get(
        "/api/integrations/instagram/callback"
        "?error=invalid_request"
        "&error_reason=user_denied"
        "&error_description=Invalid%20platform%20app",
        headers=headers,
    )
    assert response.status_code in (302, 307)
    assert "integrations?error=instagram_oauth_invalid_request" in response.headers.get("location", "")


@pytest.mark.asyncio
async def test_instagram_callback_without_state_uses_pending_session_fallback(client: AsyncClient, monkeypatch: pytest.MonkeyPatch):
    register_response = await client.post(
        "/api/auth/register",
        json={
            "email": "igfallback@example.com",
            "password": "strongpass123",
            "full_name": "IG Fallback Tester",
        },
    )
    assert register_response.status_code == 200
    access_token = register_response.json()["access_token"]
    headers = {"Authorization": f"Bearer {access_token}"}

    monkeypatch.setenv("IG_APP_ID", "ig-app-id")
    monkeypatch.setenv("IG_APP_SECRET", "ig-app-secret")
    monkeypatch.setenv("META_STATE_SECRET", "test-meta-state-secret")
    from app.core.config import get_settings
    from app.services import meta as meta_service

    get_settings.cache_clear()

    async def _fake_exchange_ig_code(code: str, callback_url: str, settings):
        assert code == "valid-code"
        assert callback_url.endswith("/api/integrations/instagram/callback")
        return "ig-token", 3600

    async def _fake_fetch_ig_profile(token: str):
        assert token == "ig-token"
        return {"id": "ig-user-1", "username": "insta_test", "media_count": 3}

    monkeypatch.setattr(meta_service, "exchange_ig_code", _fake_exchange_ig_code)
    monkeypatch.setattr(meta_service, "fetch_ig_profile", _fake_fetch_ig_profile)

    authorize_response = await client.get("/api/integrations/instagram/authorize", headers=headers)
    assert authorize_response.status_code == 200

    callback_response = await client.get(
        "/api/integrations/instagram/callback?code=valid-code",
        headers=headers,
    )
    assert callback_response.status_code in (302, 307)
    assert "integrations?connected=instagram" in callback_response.headers.get("location", "")

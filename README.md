# Backend

FastAPI backend for the wellness dashboard, auth, Meta integrations, and chat assistant.

## What changed

- Clearer app-first structure under `app/`
- Session-scoped chat memory by default, with optional persistent mode
- Real `/api/chat/*` endpoints
- Dashboard summary service behind `/api/dashboard/summary`
- LangChain agent service with tool binding
- Groq OpenAI-compatible support for `openai/gpt-oss-120b`
- Fixed the broken integration upsert path in `token_store`

## Structure

- `main.py`: app entrypoint and router wiring
- `app/api/`: HTTP route modules
- `app/core/`: config and database setup
- `app/models/`: SQLAlchemy models
- `app/schemas/`: request and response models
- `app/services/`: business logic, chat persistence, integrations, dashboard, agent
- `alembic/versions/`: database migrations
- `tests/`: backend API tests

## Environment

Copy `.env.example` to `.env`.

Important values:

- `USE_LOCAL_SQLITE=true`: default local database
- `JWT_SECRET_KEY`: set a long random secret
- `META_APP_ID`, `META_APP_SECRET`, `META_STATE_SECRET`: Facebook and Instagram integration
- `GROQ_API_KEY`: enables the LangChain-backed assistant
- `GROQ_MODEL=openai/gpt-oss-120b`: default model target
- `CHAT_HISTORY_MODE=persistent`: stores chat sessions/messages in the database

If `GROQ_API_KEY` is not set, chat still works through a deterministic fallback responder for local development and testing.

## Run locally

```bash
cd backend
uv sync --extra dev
uv run python -m alembic upgrade head
uv run uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Open `http://127.0.0.1:8000/docs`.

## Test

```bash
cd backend
.\.venv\Scripts\python.exe -m pytest tests\test_app.py
```

## API overview

- `POST /api/auth/register`
- `POST /api/auth/login`
- `PATCH /api/auth/password`
- `GET /api/me`
- `GET /api/dashboard/summary`
- `GET /api/integrations/`
- `GET /api/integrations/facebook/authorize`
- `GET /api/integrations/instagram/authorize`
- `DELETE /api/integrations/facebook`
- `DELETE /api/integrations/instagram`
- `POST /api/chat/message`
- `GET /api/chat/sessions`
- `GET /api/chat/sessions/{session_id}`

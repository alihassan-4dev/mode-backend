# Dashboard and Agent Notes

This backend keeps the dashboard honest: it shows real connection state and real chat-derived history only.

## Dashboard

- `GET /api/dashboard/summary` is the single source of truth for dashboard data.
- Facebook and Instagram are always returned in `platform_breakdown`.
- If a platform is not connected, `connected=false` and `activity_count=0`.
- If no platform is connected, mood, stress, readiness, and connected sources return `0`.
- `mood_history` is built from recent user chat messages. If the user has no check-ins, it is an empty list so the frontend shows an empty state instead of fake graph data.

## Agent

- Chat calls `app.services.agent_service.generate_reply`.
- The real agent lives in `app/agent/`.
- `state.py` defines the LangGraph state.
- `prompt.py` defines the Mode-aware system prompt.
- `tools.py` exposes dashboard, platform, and user-mode tools.
- `main_agent.py` builds the LangGraph flow: model call, tool call, model response.
- If `GROQ_API_KEY` is missing, the app uses a deterministic fallback so local development and tests still work.

## Frontend Expectations

- The chat input should stay fixed inside the chat card.
- Only the messages panel scrolls.
- The dashboard should not invent old history. Empty history must show an empty state.
- If only Facebook is connected, Facebook shows real count and Instagram shows `0`. The reverse is also true.

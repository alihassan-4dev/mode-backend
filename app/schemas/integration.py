from datetime import datetime

from pydantic import BaseModel


class ConnectionOut(BaseModel):
    platform: str
    platform_user_id: str
    platform_username: str | None = None
    platform_name: str | None = None
    avatar_url: str | None = None
    token_expires_at: datetime | None = None
    scopes: list[str] | None = None
    connected_at: datetime
    last_synced_at: datetime | None = None

    model_config = {"from_attributes": True}


class AuthorizeResponse(BaseModel):
    url: str


class DisconnectResponse(BaseModel):
    success: bool


class PlatformDataResponse(BaseModel):
    platform: str
    posts: list[dict]
    stats: dict

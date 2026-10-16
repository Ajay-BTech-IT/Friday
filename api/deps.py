"""Optional bearer-token guard. Empty API_AUTH_TOKEN (local PC) = open on localhost.
Mobile/hosted builds set the token later; nothing else changes."""
from __future__ import annotations

import secrets

from fastapi import Header, HTTPException

from config import settings


async def require_auth(authorization: str | None = Header(default=None)) -> None:
    if not settings.api_auth_token:
        return
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not secrets.compare_digest(supplied, settings.api_auth_token):
        raise HTTPException(status_code=401, detail="Invalid or missing API token")

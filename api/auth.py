"""Google OAuth connect + callback (Gmail + Calendar)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from google_svc import oauth

router = APIRouter(prefix="/auth/google", tags=["auth"])


@router.get("/start")
async def start():
    try:
        return RedirectResponse(oauth.start_url())
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@router.get("/callback", response_class=HTMLResponse)
async def callback(code: str | None = None, state: str | None = None, error: str | None = None):
    if error or not code or not state:
        raise HTTPException(400, f"Google sign-in failed: {error or 'missing code'}")
    try:
        await oauth.finish(code, state)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return "<h3>Google connected.</h3><p>Gmail and Calendar are linked. You can close this tab.</p>"


@router.get("/status")
async def status() -> dict:
    return await oauth.status()

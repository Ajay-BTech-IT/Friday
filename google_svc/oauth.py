"""Google OAuth (Gmail + Calendar). Tokens are Fernet-encrypted before they touch the DB."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from cryptography.fernet import Fernet
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from config import settings
from database import db

log = logging.getLogger("google.oauth")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/youtube.readonly",
]
TOKEN_URI = "https://oauth2.googleapis.com/token"

_pending: dict[str, str | None] = {}  # state -> PKCE code_verifier


class GoogleNotConnected(Exception):
    pass


class GoogleAuthExpired(Exception):
    """Google refused the refresh token (revoked / expired): the user must reconnect."""


class GoogleTransient(Exception):
    """Network trouble while talking to Google (Wi-Fi not up yet, DNS, timeout). Nothing is wrong with the login."""


def _fernet() -> Fernet:
    if not settings.token_encryption_key:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is empty")
    return Fernet(settings.token_encryption_key.encode())


def _flow() -> Flow:
    cfg = {
        "web": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": TOKEN_URI,
            "redirect_uris": [settings.google_redirect_uri],
        }
    }
    return Flow.from_client_config(cfg, scopes=SCOPES, redirect_uri=settings.google_redirect_uri)


def start_url() -> str:
    if not settings.google_enabled:
        raise RuntimeError("Set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and TOKEN_ENCRYPTION_KEY in .env first")
    flow = _flow()
    url, state = flow.authorization_url(access_type="offline", prompt="consent", include_granted_scopes="true")
    _pending[state] = getattr(flow, "code_verifier", None)
    return url


async def finish(code: str, state: str) -> None:
    if state not in _pending:
        raise ValueError("Unknown or expired OAuth state. Start again from /auth/google/start")
    verifier = _pending.pop(state)

    def _exchange() -> Credentials:
        flow = _flow()
        if verifier:
            flow.code_verifier = verifier
        flow.fetch_token(code=code)
        return flow.credentials

    creds = await asyncio.to_thread(_exchange)
    if not creds.refresh_token:
        raise ValueError("Google returned no refresh token. Remove the app at myaccount.google.com/permissions and retry.")
    await _store(creds)


async def _store(creds: Credentials) -> None:
    f = _fernet()
    await db.execute(
        "INSERT INTO oauth_credentials(user_id,provider,scopes,refresh_token_enc,access_token_enc,expires_at) "
        "VALUES (%s,'google',%s,%s,%s,%s) ON CONFLICT (user_id,provider) DO UPDATE SET scopes=EXCLUDED.scopes, "
        "refresh_token_enc=EXCLUDED.refresh_token_enc, access_token_enc=EXCLUDED.access_token_enc, "
        "expires_at=EXCLUDED.expires_at, updated_at=now()",
        db.owner_id(), list(creds.scopes or SCOPES), f.encrypt(creds.refresh_token.encode()),
        f.encrypt(creds.token.encode()) if creds.token else None,
        creds.expiry.replace(tzinfo=timezone.utc) if creds.expiry else None,
    )


async def status() -> dict:
    row = await db.fetchrow(
        "SELECT scopes, expires_at, updated_at, gmail_history_id IS NOT NULL AS gmail_synced, "
        "calendar_sync_token IS NOT NULL AS calendar_synced FROM oauth_credentials WHERE user_id=%s AND provider='google'",
        db.owner_id(),
    )
    return {"configured": settings.google_enabled, "connected": bool(row), **(row or {})}


async def get_credentials() -> Credentials:
    if not settings.google_enabled:
        raise GoogleNotConnected("Google is not configured in .env")
    row = await db.fetchrow(
        "SELECT * FROM oauth_credentials WHERE user_id=%s AND provider='google'", db.owner_id()
    )
    if not row:
        raise GoogleNotConnected("Google is not connected. Open /auth/google/start in a browser.")
    f = _fernet()
    creds = Credentials(
        token=f.decrypt(bytes(row["access_token_enc"])).decode() if row["access_token_enc"] else None,
        refresh_token=f.decrypt(bytes(row["refresh_token_enc"])).decode(),
        token_uri=TOKEN_URI,
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        scopes=row["scopes"],
        expiry=row["expires_at"].astimezone(timezone.utc).replace(tzinfo=None) if row["expires_at"] else None,
    )
    if not creds.valid:
        try:
            await asyncio.to_thread(creds.refresh, Request())
        except RefreshError as e:  # Google said no: the refresh token is dead
            from database import notifications

            recent = await db.fetchval(
                "SELECT count(*) FROM notifications WHERE user_id=%s AND title='Reconnect Google' AND created_at > now() - interval '6 hours'",
                db.owner_id())
            if not recent:  # one alert, not one per attempt
                await notifications.add("Reconnect Google", "Gmail/Calendar access expired. Open /auth/google/start to reconnect.",
                                        kind="alert", urgent=True)
            raise GoogleAuthExpired(str(e)) from e
        except Exception as e:  # network / DNS / timeout: transient, no alarm
            raise GoogleTransient(f"{type(e).__name__}: {e}") from e
        await _store(creds)
    return creds


async def get_cursor(field: str) -> str | None:
    assert field in ("gmail_history_id", "calendar_sync_token")
    return await db.fetchval(f"SELECT {field} FROM oauth_credentials WHERE user_id=%s AND provider='google'", db.owner_id())


async def set_cursor(field: str, value: str | None) -> None:
    assert field in ("gmail_history_id", "calendar_sync_token")
    await db.execute(f"UPDATE oauth_credentials SET {field}=%s WHERE user_id=%s AND provider='google'", value, db.owner_id())

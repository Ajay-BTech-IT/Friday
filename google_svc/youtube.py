"""YouTube Data API v3: search videos and return playable watch links.

Auth, in order: YOUTUBE_API_KEY from .env (a key created in Google Cloud with the YouTube Data API v3 enabled), else the
user's Google login (needs the youtube.readonly scope: open /auth/google/start once to grant it).
A search costs 100 quota units (default free quota is 10,000 a day), the duration lookup 1 more."""
from __future__ import annotations

import re

import httpx

from config import settings

API = "https://www.googleapis.com/youtube/v3"


class YouTubeError(Exception):
    """Message is safe to tell the user."""


_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(20, connect=10))
    return _client


async def _auth() -> tuple[dict, dict]:
    """(query params, headers) that authenticate one request."""
    if settings.youtube_api_key:
        return {"key": settings.youtube_api_key}, {}
    from google_svc import oauth

    try:
        creds = await oauth.get_credentials()
    except oauth.GoogleNotConnected as e:
        raise YouTubeError("YouTube is not set up: add YOUTUBE_API_KEY to .env, or connect Google at /auth/google/start.") from e
    except oauth.GoogleAuthExpired as e:
        raise YouTubeError("Google access expired. Reconnect at /auth/google/start.") from e
    except oauth.GoogleTransient as e:
        raise YouTubeError("No internet connection to Google right now.") from e
    if not any("youtube" in s for s in (creds.scopes or [])):
        raise YouTubeError("YouTube access is not granted yet. Open /auth/google/start once and approve it, "
                           "or add YOUTUBE_API_KEY to .env.")
    return {}, {"Authorization": f"Bearer {creds.token}"}


def _error(r: httpx.Response) -> YouTubeError:
    try:
        err = r.json().get("error", {})
    except ValueError:
        err = {}
    reason = ((err.get("errors") or [{}])[0]).get("reason", "")
    if reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
        return YouTubeError("The YouTube search quota for today is used up. Try again tomorrow.")
    if reason in ("accessNotConfigured", "SERVICE_DISABLED") or "has not been used" in err.get("message", ""):
        return YouTubeError("The YouTube Data API v3 is not enabled for this Google Cloud project.")
    if reason in ("insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"):
        return YouTubeError("YouTube access is not granted yet. Open /auth/google/start once and approve it.")
    if r.status_code in (400, 401, 403) and ("API key" in err.get("message", "") or reason in ("keyInvalid", "badRequest")):
        return YouTubeError("YOUTUBE_API_KEY is not accepted by YouTube. Check the key and its API restrictions.")
    return YouTubeError(f"YouTube returned {r.status_code}: {err.get('message', r.text[:150])}")


def _duration(iso: str | None) -> str | None:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return None
    d, h, mi, s = (int(x or 0) for x in m.groups())
    h += d * 24
    return f"{h}:{mi:02d}:{s:02d}" if h else f"{mi}:{s:02d}"


async def search(query: str, max_results: int = 3) -> list[dict]:
    """Top videos for a query: [{video_id, title, channel, duration, url}]. Raises YouTubeError."""
    query = query.strip()
    if not query:
        raise YouTubeError("Nothing to search for.")
    params, headers = await _auth()
    try:
        r = await _http().get(f"{API}/search", headers=headers, params={
            **params, "part": "snippet", "q": query, "type": "video", "maxResults": max(1, min(int(max_results), 8)),
            "safeSearch": "moderate", "videoEmbeddable": "any"})
        if r.status_code != 200:
            raise _error(r)
        items = r.json().get("items", [])
        ids = [i["id"]["videoId"] for i in items if i.get("id", {}).get("videoId")]
        durations: dict[str, str | None] = {}
        if ids:
            d = await _http().get(f"{API}/videos", headers=headers, params={**params, "part": "contentDetails", "id": ",".join(ids)})
            if d.status_code == 200:
                durations = {v["id"]: _duration(v["contentDetails"].get("duration")) for v in d.json().get("items", [])}
    except httpx.HTTPError as e:
        raise YouTubeError("Could not reach YouTube (check the internet connection).") from e
    return [
        {"video_id": i["id"]["videoId"], "title": _unescape(i["snippet"]["title"]), "channel": i["snippet"]["channelTitle"],
         "duration": durations.get(i["id"]["videoId"]), "url": f"https://www.youtube.com/watch?v={i['id']['videoId']}"}
        for i in items if i.get("id", {}).get("videoId")
    ]


def _unescape(s: str) -> str:
    import html

    return html.unescape(s)

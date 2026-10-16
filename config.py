"""All configuration comes from environment variables (.env). Nothing hard-coded."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _b(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "")
    owner_email: str = os.getenv("OWNER_EMAIL", "owner@example.com")
    owner_name: str = os.getenv("OWNER_NAME", "Friend")
    owner_timezone: str = os.getenv("OWNER_TIMEZONE", "Asia/Kolkata")

    google_api_key: str = os.getenv("GOOGLE_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
    # local (Ollama) is primary; "gemini" is still selectable but its vectors are a different space (re-embeds on start)
    embedding_provider: str = os.getenv("EMBEDDING_PROVIDER", "ollama").strip().lower()
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "nomic-embed-text")
    ollama_url: str = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
    embedding_dim: int = _i("EMBEDDING_DIM", 768)

    email_reminders_default: bool = _b("EMAIL_REMINDERS_DEFAULT", True)
    reminder_email: str = os.getenv("REMINDER_EMAIL", "").strip()  # reminders by email go ONLY here
    host: str = os.getenv("HOST", "127.0.0.1")
    port: int = _i("PORT", 8000)
    api_auth_token: str = os.getenv("API_AUTH_TOKEN", "")

    daily_notify_cap: int = _i("DAILY_NOTIFY_CAP", 8)
    reminder_poll_seconds: int = _i("REMINDER_POLL_SECONDS", 30)
    extract_poll_seconds: int = _i("EXTRACT_POLL_SECONDS", 20)
    desktop_notifications: bool = _b("DESKTOP_NOTIFICATIONS", True)
    overnight_job_time: str = os.getenv("OVERNIGHT_JOB_TIME", "03:30")
    evening_briefing_time: str = os.getenv("EVENING_BRIEFING_TIME", "20:30")

    murf_api_key: str = os.getenv("MURF_API_KEY", "")
    tts_mode: str = os.getenv("TTS_MODE", "full").strip().lower()  # full | sentence
    murf_model: str = os.getenv("MURF_MODEL", "falcon-2")
    # fallback voices for personas that have no voice of their own (chosen by the persona's gender); the real voice
    # of each persona lives in the personas table and is changed by talking to the agent
    murf_voice_male: str = os.getenv("MURF_VOICE_MALE") or os.getenv("MURF_VOICE_JARVIS") or "en-IN-nikhil"
    murf_voice_female: str = os.getenv("MURF_VOICE_FEMALE") or os.getenv("MURF_VOICE_HELLA") or "en-IN-pooja"

    google_client_id: str = os.getenv("GOOGLE_CLIENT_ID", "")
    google_client_secret: str = os.getenv("GOOGLE_CLIENT_SECRET", "")
    google_redirect_uri: str = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8000/auth/google/callback")
    token_encryption_key: str = os.getenv("TOKEN_ENCRYPTION_KEY", "")

    @property
    def embedder_tag(self) -> str:
        """Identifies the vector space. Stored with every memory so vectors from different models are never mixed."""
        return f"{self.embedding_provider}:{self.embedding_model}"

    @property
    def voice_enabled(self) -> bool:
        return bool(self.murf_api_key)

    @property
    def google_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret and self.token_encryption_key)

    def validate(self) -> list[str]:
        problems = []
        if not self.database_url:
            problems.append("DATABASE_URL is empty")
        if not self.google_api_key:
            problems.append("GOOGLE_API_KEY is empty (chat will not work)")
        return problems


settings = Settings()

"""Delivery on the PC: in-app inbox (always) + optional Windows toast. Phone push (FCM) is held."""
from __future__ import annotations

import asyncio
import logging
import sys
from uuid import UUID

from config import settings
from database import notifications

log = logging.getLogger("notify")


def _toast_blocking(title: str, body: str) -> None:
    try:
        from winotify import Notification

        Notification(app_id="Personal Agent", title=title[:60], msg=(body or "")[:240]).show()
    except Exception as e:  # toast is best-effort
        log.debug("toast skipped: %s", e)


async def deliver(title: str, body: str | None = None, kind: str = "reminder", urgent: bool = False,
                  reminder_id: UUID | None = None) -> dict:
    n = await notifications.add(title, body, kind, urgent, reminder_id)
    if settings.desktop_notifications and sys.platform == "win32":
        await asyncio.to_thread(_toast_blocking, title, body or "")
    log.info("notify[%s]: %s", kind, title)
    return n

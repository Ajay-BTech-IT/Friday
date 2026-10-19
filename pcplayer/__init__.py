"""Launches the floating PC player (pcplayer.window) as a separate process. One player at a time: playing a new video
replaces the old one. The window always runs on the project's own Python (.venv), so it works no matter which Python
started the terminal client."""
from __future__ import annotations

import os
import subprocess
import sys
import time

_proc: subprocess.Popen | None = None
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _python() -> str:
    venv = os.path.join(_ROOT, ".venv", "Scripts", "python.exe")
    return venv if os.path.exists(venv) else sys.executable


def play(video_id: str, title: str | None = None) -> bool:
    """Open (or replace) the floating player. Returns False when it cannot start, so the caller can open the browser."""
    global _proc
    stop()
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        # own stdin/stdout so the window never touches the terminal; its errors go to a log file for diagnosis
        log = open(os.path.join(_ROOT, "pcplayer", "player.log"), "w", encoding="utf-8")
        _proc = subprocess.Popen([_python(), "-m", "pcplayer.window", video_id, title or ""], cwd=_ROOT, creationflags=flags,
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=log)
    except OSError:
        return False
    time.sleep(1.2)  # a missing dependency or a crash shows up immediately
    if _proc.poll() is not None:
        _proc = None
        return False
    return True


def stop() -> None:
    global _proc
    if _proc and _proc.poll() is None:
        # kill the whole tree: the venv python.exe is only a launcher for the process that owns the window
        subprocess.run(["taskkill", "/PID", str(_proc.pid), "/T", "/F"], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    _proc = None

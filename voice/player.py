"""Client-side audio queue: plays MP3 chunks in order, without blocking the text stream.
Windows uses the built-in MCI (no extra packages); macOS/Linux fall back to afplay/mpg123/ffplay."""
from __future__ import annotations

import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading


def _play_file(path: str) -> None:
    if sys.platform == "win32":
        import ctypes

        mci = ctypes.windll.winmm.mciSendStringW
        alias = f"a{threading.get_ident()}"
        mci(f'open "{path}" type mpegvideo alias {alias}', None, 0, 0)
        mci(f"play {alias} wait", None, 0, 0)
        mci(f"close {alias}", None, 0, 0)
        return
    for cmd in (["afplay"], ["mpg123", "-q"], ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]):
        if shutil.which(cmd[0]):
            subprocess.run(cmd + [path], check=False)
            return


class Player:
    def __init__(self) -> None:
        self.q: queue.Queue[bytes | None] = queue.Queue()
        self.enabled = True
        self._busy = False
        threading.Thread(target=self._run, daemon=True).start()

    def add(self, mp3: bytes) -> None:
        if self.enabled and mp3:
            self.q.put(mp3)

    def wait_idle(self, timeout: float = 60.0) -> None:
        """Block until everything queued has been spoken (so a video does not start over the assistant's voice)."""
        import time

        end = time.time() + timeout
        time.sleep(0.15)
        while (not self.q.empty() or self._busy) and time.time() < end:
            time.sleep(0.1)

    def stop(self) -> None:
        """Drop anything still queued (e.g. user starts typing the next message)."""
        try:
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass

    def _run(self) -> None:
        while True:
            data = self.q.get()
            if data is None:
                return
            self._busy = True
            fd, path = tempfile.mkstemp(suffix=".mp3")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                _play_file(path)
            except Exception:
                pass
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass
                self._busy = False

"""Terminal client for the PC. Replies stream as text AND voice; reminders pop up and are spoken too.

    python cli.py            (the server must be running: python run.py)

Commands:  /voice on|off  /tasks  /brief  /notifications  /drafts  /approve <id>  /reject <id>  /audit  /quit
"""
from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time

import httpx
from dotenv import load_dotenv

from voice.player import Player

load_dotenv()
_host = os.getenv("HOST", "127.0.0.1")
_host = "127.0.0.1" if _host in ("0.0.0.0", "::", "") else _host  # 0.0.0.0 means "listen everywhere", not an address to call
BASE = os.getenv("AGENT_URL", f"http://{_host}:{os.getenv('PORT', '8000')}")
TOKEN = os.getenv("API_AUTH_TOKEN", "")
HEADERS = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
client = httpx.Client(base_url=BASE, headers=HEADERS, timeout=120)
player = Player()
_print_lock = threading.Lock()


def say(text: str, end: str = "\n") -> None:
    with _print_lock:
        print(text, end=end, flush=True)


def poll_notifications() -> None:
    """Background: show reminders/briefings the moment they land, speak them, mark them seen."""
    c = httpx.Client(base_url=BASE, headers=HEADERS, timeout=30)
    while True:
        time.sleep(8)
        try:
            items = c.get("/notifications", params={"unseen": True}).json()
            if items:  # mark seen FIRST: a failing voice request must never make the same notice repeat forever
                c.post("/notifications/seen", json={"ids": [n["id"] for n in items]})
            for n in items:
                say(f"\n  [{n['kind']}] {n['title']}" + (f"\n     {n['body']}" if n.get("body") else "") + "\nyou> ", end="")
                if player.enabled:
                    try:
                        r = c.post("/chat/tts", json={"text": n.get("body") or n["title"]})
                        if r.status_code == 200:
                            player.add(r.content)
                    except Exception:
                        pass  # voice is optional
        except Exception:
            pass


def show(path: str, **params) -> None:
    r = client.get(path, params=params)
    say(r.text if r.status_code != 200 else json.dumps(r.json(), indent=2, ensure_ascii=False))


def _start_video(act: dict) -> None:
    import pcplayer

    if not (act.get("video_id") and pcplayer.play(act["video_id"], act.get("title"))):
        import webbrowser  # floating player unavailable: fall back to the browser

        say("  (player window could not start, opened the browser; see pcplayer/player.log)")
        webbrowser.open(act["url"])


def chat(text: str) -> None:
    """Stream one turn: print tokens as they arrive, queue audio chunks as they arrive."""
    player.stop()
    persona = "agent"
    started = False
    pending_play: dict | None = None
    with client.stream("POST", "/chat/stream", json={"text": text, "voice": player.enabled}) as r:
        if r.status_code != 200:
            r.read()
            say(f"(error {r.status_code}: {r.text[:200]})")
            return
        for line in r.iter_lines():
            if not line.startswith("data: "):
                continue
            ev = json.loads(line[6:])
            t = ev.get("type")
            if t == "start":
                persona = ev["persona"]
            elif t == "token":
                if not started:
                    say(f"{persona}> ", end="")
                    started = True
                say(ev["text"], end="")
            elif t == "audio":
                player.add(base64.b64decode(ev["data"]))
            elif t == "action" and ev["action"].get("type") == "play_video":
                import pcplayer

                pending_play = ev["action"]          # start it after the voice has finished, never on top of it
            elif t == "action" and ev["action"].get("type") == "close_video":
                import pcplayer

                pcplayer.stop()
            elif t == "voice_error":
                say(f"\n  (voice unavailable: {ev['message']})", end="")
            elif t == "done":
                if not started:
                    say(f"{persona}> {ev.get('reply', '')}", end="")
                say("")
                if pending_play:
                    player.wait_idle()
                    _start_video(pending_play)
                    pending_play = None


def main() -> None:
    try:
        h = client.get("/health").json()
    except Exception:
        sys.exit(f"Cannot reach {BASE}. Start the server first:  python run.py")
    say(f"Connected ({h['status']}). Text + voice on. /voice off to mute, /quit to leave.")
    threading.Thread(target=poll_notifications, daemon=True).start()
    while True:
        try:
            line = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        if line in ("/quit", "/exit"):
            break
        if line.startswith("/voice"):
            arg = line.split()[-1]
            player.enabled = arg != "off"
            if not player.enabled:
                player.stop()
            say(f"voice {'on' if player.enabled else 'off'}")
        elif line == "/tasks":
            show("/tasks")
        elif line == "/brief":
            show("/briefing/morning")
        elif line == "/notifications":
            show("/notifications", unseen=False)
        elif line == "/drafts":
            show("/drafts")
        elif line == "/audit":
            show("/audit", limit=15)
        elif line.startswith("/approve "):
            say(client.post(f"/drafts/{line.split()[1]}/approve").text)
        elif line.startswith("/reject "):
            say(client.post(f"/drafts/{line.split()[1]}/reject").text)
        else:
            try:
                chat(line)
            except Exception as e:
                say(f"(error: {e})")


if __name__ == "__main__":
    main()

"""YouTube tools: find videos and links, and play one. The agent never streams anything itself: play_youtube returns a
CLIENT ACTION and the app the user is on opens it (the phone app opens YouTube; the PC terminal opens the browser)."""
from __future__ import annotations

import logging
import re

from langchain_core.tools import tool

from google_svc import youtube
from tools._util import out, safe

log = logging.getLogger("youtube")

_seen: dict[str, dict] = {}  # video_id -> info from recent searches, so play_youtube(video_id) knows the title
_last: dict = {"video_id": None, "query": ""}
_pl: dict = {"items": [], "i": 0}  # what the last search found and which one is playing, for "next song"


def _action(info: dict) -> dict:
    return {"type": "play_video", "url": info["url"], "video_id": info["video_id"], "title": info["title"],
            "channel": info.get("channel")}


# Words people say around a request that are not part of the title. YouTube needs the title words only: a chatty
# sentence ("bindass hella lets chill with banger") finds nothing, "bindass" finds the song.
_FILLER = set("""a an the and or but is are was be it its this that these those of to in on at for with from by as so
me my mine we us you your yours i im lets let's let please pls just now then also only one some any more really very
hey hi hello yo okay ok sir boss buddy bro da yaar dude mate chill vibe vibes banger mood feel feeling want wanna
need can could would will shall play""".split())


def _variants(query: str, names: tuple[str, ...]) -> list[str]:
    """The query as said, then cleaned of persona names and filler, then shortened: tried in that order."""
    drop = {n.lower() for n in names}
    words = re.findall(r"[\w']+", query.lower(), flags=re.UNICODE)
    core = [w for w in words if w not in _FILLER and w not in drop]
    tries = [query.strip(), " ".join(core), " ".join(core[:4]), " ".join(core[:2]), " ".join(core[:1])]
    out: list[str] = []
    for t in tries:
        if t and t.lower() not in [o.lower() for o in out]:
            out.append(t)
    return out


def short_title(title: str | None) -> str:
    """"Bindaas - Lyric Video | Jailer 2| Superstar Rajinikanth | Sun Pictures..." -> "Bindaas - Lyric Video": what a person
    would say out loud. The full title stays on the player."""
    t = (title or "").split("|")[0].strip() or (title or "").strip()
    return t if len(t) <= 70 else t[:67].rsplit(" ", 1)[0] + "..."


def _relevant(variant: str, results: list[dict]) -> bool:
    """Does the best result look like what was asked for? Compares word by word, forgiving spelling (bindass ~ Bindaas)."""
    import difflib

    want = [w for w in re.findall(r"[\w']+", variant.lower()) if w not in _FILLER]
    have = re.findall(r"[\w']+", " ".join(f"{v['title']} {v['channel']}" for v in results[:3]).lower())
    return not want or any(difflib.SequenceMatcher(None, w, h).ratio() >= .78 for w in want for h in have)


async def play_query(query: str, names: tuple[str, ...] = ()) -> dict:
    """Search and pick the video to play: {"info": {...}} or {"error": "..."}. Used by the router (no model involved) and
    by play_youtube. `names` are the assistant's own names (never part of a title). Asking again for the same thing
    never replays the video that was just played."""
    found: list[dict] = []
    for q in _variants(query, names):
        try:
            found = await youtube.search(q, 8)
        except youtube.YouTubeError as e:
            log.warning("youtube failed: %s", e)
            return {"error": str(e)}
        if found and (q == query.strip() or _relevant(q, found)):
            if q != query.strip():
                log.info("youtube: %r found nothing, %r did", query, q)
            query = q
            break
        found = []
    if not found:
        return {"error": "I couldn't find a video for that.", "not_found": True}
    _remember(found)
    pick = next((v for v in found if v["video_id"] != _last["video_id"]), found[0]) if _same(query) else found[0]
    _pl.update(items=found, i=found.index(pick))
    _last.update(video_id=pick["video_id"], query=(query or "").strip().lower())
    return {"info": pick, "others": [{"video_id": v["video_id"], "title": v["title"]} for v in found if v is not pick][:3]}


def step(delta: int) -> dict:
    """Move to the next (+1) or previous (-1) video of the last search: {"info": {...}} or {"error": "..."}."""
    if not _pl["items"]:
        return {"error": "Nothing was playing, so there is no next one. Tell me what to play."}
    i = _pl["i"] + delta
    if not 0 <= i < len(_pl["items"]):
        return {"error": "That was the last one from that search. Tell me what else to play." if delta > 0
                else "That was the first one from that search."}
    _pl["i"] = i
    info = _pl["items"][i]
    _last.update(video_id=info["video_id"])
    return {"info": info}


async def _names() -> tuple[str, ...]:
    """Every name the assistant answers to (personas and their aliases): talking TO it is not part of a title."""
    try:
        from database import persona as persona_db

        return tuple({n for p in await persona_db.list_personas() for n in [p["name"], *(p["aliases"] or [])] if n})
    except Exception:
        return ()


def _remember(results: list[dict]) -> None:
    for v in results:
        _seen[v["video_id"]] = v
    while len(_seen) > 50:
        _seen.pop(next(iter(_seen)))


def _same(query: str) -> bool:
    return (query or "").strip().lower() == _last["query"]


@tool
@safe
async def search_youtube(query: str, max_results: int = 3) -> str:
    """Search YouTube and return video links (title, channel, duration, url). Use when the user asks to find, look up or
    get the link of a video, song, tutorial, trailer, talk, etc. Give the link(s) in your reply. To actually start
    playing one, use play_youtube instead."""
    try:
        results = await youtube.search(query, max_results)
    except youtube.YouTubeError as e:
        log.warning("youtube failed: %s", e)
        return out({"error": str(e)})
    if not results:
        return out({"results": [], "note": "No videos found. Try different words."})
    _remember(results)
    return out({"results": results})


@tool
@safe
async def play_youtube(query: str | None = None, video_id: str | None = None) -> str:
    """Play a YouTube video for the user ("play calm music", "play the Jailer trailer"). Pass a search `query` (the best
    match plays) or a `video_id` taken from a search_youtube result. Opens on the device the user is on (phone: the
    YouTube app; PC: a small floating player window). Then say in one short line what is playing (use only the short title it returns, never read out a long title with bars); never claim it is
    playing if this returned an error.
    WHEN THE USER CORRECTS YOU ("not that one", "the first movie, not 2", "another one", "different version"):
    NEVER repeat the same query. Call search_youtube with a more specific query, READ the titles, pick the one that
    matches what they meant, then call play_youtube(video_id=...). If you play by query and the result lists `others`,
    check that the title really matches the request before saying it is the right one."""
    if video_id:
        info = _seen.get(video_id) or {"video_id": video_id, "title": None, "channel": None,
                                       "url": f"https://www.youtube.com/watch?v={video_id}"}
        others: list = []
        _last.update(video_id=video_id)
        if video_id in [v["video_id"] for v in _pl["items"]]:
            _pl["i"] = [v["video_id"] for v in _pl["items"]].index(video_id)
    else:
        if not (query or "").strip():
            return out({"error": "Ask the user what they want to play."})
        res = await play_query(query, await _names())
        if "error" in res:
            return out({"error": res["error"]})
        info, others = res["info"], res["others"]
    result = {"playing": short_title(info["title"]) or info["url"], "channel": info.get("channel"), "duration": info.get("duration"),
              "client_action": _action(info)}
    if others:
        result["others"] = others
    return out(result)


@tool
@safe
async def close_player() -> str:
    """ALWAYS call this when the user wants the video/music player closed or stopped ("close", "close the player",
    "stop the video", "stop it", "shut it", "turn it off"). It is the ONLY thing that closes the floating player on
    the PC: without calling it nothing closes, so never just say it is closed. On the phone the video plays inside the
    YouTube app, which the user closes themselves; say that in that case. After the call, confirm in a few words."""
    return out({"closed": True, "client_action": {"type": "close_video"}})

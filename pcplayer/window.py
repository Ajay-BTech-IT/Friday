"""Floating YouTube player for the PC: a small frameless, always-on-top window (bottom-right corner) that plays the
video with YouTube's official embedded player. Run as its own process:

    python -m pcplayer.window <video_id> "<title>"

A tiny local web server hands the page to the window, because the YouTube player refuses to start from a page that has
no web origin. If YouTube does not allow a video to be embedded, the window says so and offers 'Open on YouTube'."""
from __future__ import annotations

import ctypes
import json
import os
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import webview

W, H = 480, 300  # H includes the 30px title bar


PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;height:100%;background:#0b0f14;color:#e6edf3;font-family:Segoe UI,Arial,sans-serif;overflow:hidden}
#bar{height:30px;display:flex;align-items:center;background:#151b23;border-bottom:1px solid #263041;font-size:12px}
#title{flex:1;padding:0 10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:move}
button{width:34px;height:30px;border:0;background:transparent;color:#9fb0c3;font-size:14px;cursor:pointer}
button:hover{background:#263041;color:#fff} #x:hover{background:#c0392b}
#stage{position:absolute;top:31px;left:0;right:0;bottom:0}
#stage iframe{width:100%;height:100%;border:0}
#err{display:none;position:absolute;inset:31px 0 0 0;align-items:center;justify-content:center;flex-direction:column;
text-align:center;padding:20px;gap:12px;font-size:14px} #err button{width:auto;padding:0 16px;height:34px;background:#2d6cdf;color:#fff;border-radius:6px}
</style></head><body>
<div id="bar"><div id="title" class="pywebview-drag-region">__TITLE__</div>
<button id="min" title="Minimise">&#8211;</button><button id="x" title="Close">&#10005;</button></div>
<div id="stage"><div id="yt"></div></div>
<div id="err"><div id="msg"></div><button id="open">Open on YouTube</button></div>
<script>
const VIDEO = __VIDEO__;
const api = () => window.pywebview && window.pywebview.api;
document.getElementById('x').onclick = () => api() && api().close();
document.getElementById('min').onclick = () => api() && api().minimize();
document.getElementById('open').onclick = () => api() && api().open_youtube();
function showError(text){ document.getElementById('msg').textContent = text;
  document.getElementById('stage').style.display='none'; document.getElementById('err').style.display='flex'; }
var tag = document.createElement('script'); tag.src = 'https://www.youtube.com/iframe_api'; document.head.appendChild(tag);
function onYouTubeIframeAPIReady(){
  new YT.Player('yt', {videoId: VIDEO, width:'100%', height:'100%',
    playerVars:{autoplay:1, rel:0, modestbranding:1, playsinline:1},
    events:{ onError: e => showError(e.data===101||e.data===150 ? 'The owner does not allow this video to play outside YouTube.' : 'This video could not be played here.'),
             onReady: e => e.target.playVideo() }});
  const f = document.querySelector('#stage iframe'); if (f) f.style.height='100%';
}
</script></body></html>"""


class _Api:
    def __init__(self, video_id: str):
        self.video_id = video_id
        self._window: webview.Window | None = None  # underscore: pywebview must not scan it

    def close(self):
        if self._window:
            self._window.destroy()

    def minimize(self):
        if self._window:
            self._window.minimize()

    def open_youtube(self):
        webbrowser.open(f"https://www.youtube.com/watch?v={self.video_id}")


def _serve(html: str) -> ThreadingHTTPServer:
    body = html.encode("utf-8")

    class H_(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H_)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main(video_id: str, title: str) -> None:
    import html as htmllib

    # the user just asked for this video, so let it start with sound (WebView2 blocks that by default)
    os.environ.setdefault("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "--autoplay-policy=no-user-gesture-required")
    page = PAGE.replace("__TITLE__", htmllib.escape(title or "YouTube")).replace("__VIDEO__", json.dumps(video_id))
    srv = _serve(page)
    sw, sh = ctypes.windll.user32.GetSystemMetrics(0), ctypes.windll.user32.GetSystemMetrics(1)
    api = _Api(video_id)
    win = webview.create_window(title or "YouTube", f"http://127.0.0.1:{srv.server_port}/", js_api=api, width=W, height=H,
                                x=max(sw - W - 24, 0), y=max(sh - H - 72, 0), frameless=True, on_top=True,
                                resizable=True, min_size=(320, 200), background_color="#0b0f14")
    api._window = win
    webview.start()  # blocks until the window is closed
    srv.shutdown()


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "")


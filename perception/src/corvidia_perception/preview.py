"""Annotated preview served as MJPEG over HTTP, plus a JSON health endpoint.

Open http://<jetson>:<port>/ in a browser. Drawing and JPEG encoding happen on
the preview thread at a capped rate, never on the detector thread.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

from .records import Detection, Frame

# BGR colors per candidate phase / result
COLORS = {
    "observing": (0, 215, 255),
    "pending": (255, 160, 0),
    "cooldown": (0, 140, 255),
    "suppressed": (160, 160, 160),
    "confirmed": (0, 200, 0),
    "rejected": (0, 0, 230),
    "unknown": (200, 0, 200),
}


@dataclass
class PreviewState:
    frame: Frame
    detections: list[Detection]
    labels: dict[int, str]          # track_id -> phase or final result
    stats: list[str] = field(default_factory=list)


def annotate(state: PreviewState, max_width: int = 960) -> np.ndarray:
    import cv2

    image = state.frame.image
    scale = min(1.0, max_width / image.shape[1])
    if scale < 1.0:
        image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    else:
        image = image.copy()
    for det in state.detections:
        label = state.labels.get(det.track_id, "observing")
        color = COLORS.get(label, (255, 255, 255))
        b = det.bbox
        p1 = (int(b.x1 * scale), int(b.y1 * scale))
        p2 = (int(b.x2 * scale), int(b.y2 * scale))
        cv2.rectangle(image, p1, p2, color, 2)
        text = f"#{det.track_id} {det.confidence:.2f} {label}"
        cv2.putText(image, text, (p1[0], max(12, p1[1] - 5)), cv2.FONT_HERSHEY_SIMPLEX,
                    0.45, color, 1, cv2.LINE_AA)
    y = 18
    for line in state.stats:
        cv2.putText(image, line, (8, y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(image, line, (8, y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1,
                    cv2.LINE_AA)
        y += 20
    return image


_PAGE = b"""<!doctype html><html><head><meta charset="utf-8"><title>Corvidia preview</title>
<style>body{margin:0;background:#111;color:#ddd;font:14px system-ui}img{max-width:100%;display:block}
pre{padding:8px;white-space:pre-wrap}</style></head><body>
<img src="/stream"><pre id="h"></pre><script>
async function poll(){try{const r=await fetch('/health');document.getElementById('h').textContent=
JSON.stringify(await r.json(),null,1)}catch(e){}setTimeout(poll,1000)}poll()</script></body></html>"""


class PreviewServer:
    def __init__(self, port: int = 8080, fps: float = 10.0, host: str = "0.0.0.0",
                 health_fn=lambda: {}) -> None:
        self.fps = fps
        self._health_fn = health_fn
        self._cond = threading.Condition()
        self._state: PreviewState | None = None
        self._jpeg: bytes | None = None
        self._jpeg_seq = 0
        self._stop = threading.Event()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def do_GET(self) -> None:
                if self.path == "/":
                    self._send(200, "text/html", _PAGE)
                elif self.path == "/health":
                    body = json.dumps(server._health_fn(), default=str).encode()
                    self._send(200, "application/json", body)
                elif self.path == "/frame.jpg":
                    jpeg = server.wait_jpeg(0, 2.0)[0]
                    if jpeg is None:
                        self._send(503, "text/plain", b"no frame yet")
                    else:
                        self._send(200, "image/jpeg", jpeg)
                elif self.path == "/stream":
                    self.send_response(200)
                    self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                    self.end_headers()
                    seq = 0
                    try:
                        while not server._stop.is_set():
                            jpeg, seq = server.wait_jpeg(seq, 2.0)
                            if jpeg is None:
                                continue
                            self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                             + f"Content-Length: {len(jpeg)}\r\n\r\n".encode()
                                             + jpeg + b"\r\n")
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                else:
                    self._send(404, "text/plain", b"not found")

            def _send(self, code: int, ctype: str, body: bytes) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

        self._httpd = ThreadingHTTPServer((host, port), Handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._threads = [
            threading.Thread(target=self._httpd.serve_forever, daemon=True, name="preview-http"),
            threading.Thread(target=self._render_loop, daemon=True, name="preview-render"),
        ]
        for t in self._threads:
            t.start()

    def update(self, state: PreviewState) -> None:
        """Called from the detector thread; only stores a reference."""
        with self._cond:
            self._state = state

    def wait_jpeg(self, after_seq: int, timeout: float) -> tuple[bytes | None, int]:
        with self._cond:
            if self._jpeg_seq <= after_seq:
                self._cond.wait(timeout)
            return self._jpeg, self._jpeg_seq

    def _render_loop(self) -> None:
        import cv2

        period = 1.0 / self.fps
        while not self._stop.is_set():
            t = time.monotonic()
            with self._cond:
                state, self._state = self._state, None
            if state is not None:
                ok, buf = cv2.imencode(".jpg", annotate(state), [cv2.IMWRITE_JPEG_QUALITY, 75])
                if ok:
                    with self._cond:
                        self._jpeg = buf.tobytes()
                        self._jpeg_seq += 1
                        self._cond.notify_all()
            self._stop.wait(max(0.0, period - (time.monotonic() - t)))

    def close(self) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        self._httpd.shutdown()
        self._httpd.server_close()

"""Tiny local web server for the editor (standard library only)."""
from __future__ import annotations

import json
import os
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .midi_export import midi_bytes
from .model import VOICES
from .session import Session

STATIC = os.path.join(os.path.dirname(__file__), "static")


def make_handler(session: Session):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # keep the console quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200):
            self._send(code, json.dumps(obj).encode(), "application/json")

        def do_GET(self):
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                with open(os.path.join(STATIC, "editor.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif url.path == "/image.png":
                self._send(200, session.image_png, "image/png")
            elif url.path == "/api/state":
                with session.lock:
                    self._json(session.state())
            elif url.path == "/api/midi":
                q = parse_qs(url.query)
                voices = [v for v in q.get("voices", ["SATB"])[0] if v in VOICES] or list(VOICES)
                with session.lock:
                    data = midi_bytes(session.score, voices)
                stem = os.path.splitext(os.path.basename(session.score_path))[0]
                suffix = "" if len(voices) == 4 else "_" + "".join(voices)
                self._send(200, data, "audio/midi",
                           {"Content-Disposition": f'attachment; filename="{stem}{suffix}.mid"'})
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if urlparse(self.path).path != "/api/op":
                return self._send(404, b"not found", "text/plain")
            try:
                op = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                result = session.apply(op)
                with session.lock:
                    state = session.state()
                state.update(result)
                self._json(state)
            except (KeyError, ValueError, AttributeError) as e:
                self._json({"error": f"{type(e).__name__}: {e}"}, 400)
            except Exception as e:  # pragma: no cover - surface unexpected bugs to the UI
                traceback.print_exc()
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    return Handler


def serve(session: Session, host: str = "127.0.0.1", port: int = 8765) -> None:
    httpd = ThreadingHTTPServer((host, port), make_handler(session))
    print(f"Editor: http://{host}:{port}/   (score autosaves to {session.score_path}; Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()

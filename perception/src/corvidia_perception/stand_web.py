"""Local operator interface for a stand mission; never owns a hardware connection."""

from __future__ import annotations

import hmac
import ipaddress
import json
import math
from pathlib import Path
import secrets
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


_MAX_BODY = 4096
_READINESS = ("guarded_stand", "hands_clear", "power_disconnect_accessible",
              "motors_still", "esc_startup_finished")


def _command(action: str, body: dict) -> dict:
    """Validate HTTP input before it enters the supervisor's bounded queue."""
    if action == "prepare":
        if set(body) - {"appearance", "percent", "duration_ms"}:
            raise ValueError("Unexpected preparation field")
        appearance = body.get("appearance", "")
        percent = body.get("percent", 5)
        duration = body.get("duration_ms", 10000)
        if not isinstance(appearance, str) or not appearance.strip() or len(appearance) > 240:
            raise ValueError("Describe the person's visible appearance in 1–240 characters")
        if any(ord(c) < 32 or ord(c) == 127 for c in appearance.strip()):
            raise ValueError("Use a single-line appearance description without control characters")
        if type(percent) not in (int, float) or not math.isfinite(percent) or not 0 < percent <= 20:
            raise ValueError("Motor input must be greater than 0 and at most 20 percent")
        if type(duration) is not int or not 1000 <= duration <= 60000:
            raise ValueError("Duration must be an integer between 1000 and 60000 milliseconds")
        return dict(action=action, appearance=appearance.strip(), percent=percent,
                    duration_ms=duration)
    if action not in ("start", "stop", "lease"):
        raise ValueError("Unknown command")
    allowed = ({"mission_id", "readiness", "lease_id"} if action == "start" else
               {"mission_id", "lease_id"} if action == "lease" else {"mission_id"})
    if set(body) - allowed:
        raise ValueError("Unexpected command field")
    mission_id = body.get("mission_id")
    if not isinstance(mission_id, str) or not 1 <= len(mission_id) <= 128:
        raise ValueError("A current mission ID is required")
    event = dict(action=action, mission_id=mission_id)
    if "lease_id" in body:
        lease_id = body["lease_id"]
        if (not isinstance(lease_id, str) or not 16 <= len(lease_id) <= 96
                or not all(c.isascii() and (c.isalnum() or c in "_-") for c in lease_id)):
            raise ValueError("A valid operator lease ID is required")
        event["lease_id"] = lease_id
    if action == "start":
        readiness = body.get("readiness", {})
        if readiness == {}:
            # Observation mode needs no physical assertions. The supervisor
            # alone knows its immutable mode and rejects this in hardware mode.
            event["readiness"] = {}
            return event
        if not isinstance(readiness, dict) or set(readiness) != set(_READINESS):
            raise ValueError("All five readiness observations are required")
        if any(readiness[key] is not True for key in _READINESS):
            raise ValueError("Confirm all readiness observations before starting")
        event["readiness"] = dict(readiness)
    return event


class StandWebServer:
    """Serve the dashboard and forward validated commands to a nonblocking callback.

    ``command_fn(event)`` returns a JSON dictionary or raises ``ValueError``.
    It must enqueue work, never wait for inference, serial commands, or motor stops.
    ``preview_fn()`` returns the latest JPEG bytes or None. Optional
    ``evidence_fn(mission_id)`` returns ``(bytes, mime_type)`` or None.
    """

    def __init__(self, port: int = 8080, host: str = "127.0.0.1", *,
                 status_fn=lambda: {}, command_fn=lambda _event: {},
                 preview_fn=lambda: None, evidence_fn=None, token: str | None = None):
        try:
            loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise ValueError("The stand interface must bind to a loopback address")
        self.token = token or secrets.token_urlsafe(32)
        self._stop = threading.Event()
        self._closed = False
        page = Path(__file__).with_name("stand_dashboard.html").read_bytes()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "CorvidiaStand/1"

            def setup(self):
                super().setup()
                self.connection.settimeout(2.0)

            def log_message(self, *_args):
                # In particular, never log the bootstrap token from the URL.
                pass

            def _json(self, code: int, value: dict):
                self._send(code, "application/json", json.dumps(value, allow_nan=False).encode())

            def _send(self, code: int, mime: str, data: bytes):
                self.send_response(code)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' blob:; "
                                 "script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                                 "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError, TimeoutError):
                    pass

            def _trusted(self, *, mutation: bool = False) -> bool:
                hosts = {f"127.0.0.1:{owner.port}", f"localhost:{owner.port}",
                         f"[::1]:{owner.port}"}
                host_values = self.headers.get_all("Host", [])
                origin_values = self.headers.get_all("Origin", [])
                if len(host_values) != 1 or host_values[0].lower() not in hosts:
                    self._json(403, {"error": "Unrecognized Host"})
                    return False
                if len(origin_values) > 1 or (origin_values and
                        origin_values[0].lower() != "http://" + host_values[0].lower()):
                    self._json(403, {"error": "Cross-origin requests are not allowed"})
                    return False
                if self.headers.get("Sec-Fetch-Site", "") == "cross-site":
                    self._json(403, {"error": "Cross-site requests are not allowed"})
                    return False
                if mutation and not self._authenticated():
                    return False
                return True

            def _authenticated(self) -> bool:
                supplied = self.headers.get_all("X-Corvidia-Token", [])
                if len(supplied) != 1 or not hmac.compare_digest(
                        supplied[0].encode("utf-8"), owner.token.encode("utf-8")):
                    self._json(403, {"error": "Open the dashboard using its operator link"})
                    return False
                return True

            def do_GET(self):
                if not self._trusted():
                    return
                target = urlsplit(self.path)
                try:
                    if target.path == "/":
                        self._send(200, "text/html; charset=utf-8", page)
                    elif target.path == "/api/status":
                        self._json(200, status_fn())
                    elif target.path == "/frame.jpg":
                        frame = preview_fn()
                        if frame is None:
                            self._json(503, {"error": "Waiting for a camera frame"})
                        else:
                            self._send(200, "image/jpeg", frame)
                    elif target.path == "/api/evidence":
                        if not self._authenticated():
                            return
                        mission_ids = parse_qs(target.query).get("mission_id", [])
                        if len(mission_ids) != 1 or not 1 <= len(mission_ids[0]) <= 128:
                            self._json(400, {"error": "A mission ID is required"})
                            return
                        result = evidence_fn(mission_ids[0]) if evidence_fn else None
                        if result is None:
                            self._json(404, {"error": "No committed capture for this mission"})
                        elif result[1] not in ("image/jpeg", "image/png"):
                            self._json(500, {"error": "Unsupported evidence format"})
                        else:
                            self._send(200, result[1], result[0])
                    else:
                        self._json(404, {"error": "Not found"})
                except Exception:
                    self._json(500, {"error": "Unable to read mission state"})

            def do_POST(self):
                if not self._trusted(mutation=True):
                    return
                target = urlsplit(self.path)
                if target.query or target.path not in ("/api/prepare", "/api/start", "/api/stop", "/api/lease"):
                    self._json(404, {"error": "Not found"})
                    return
                if self.headers.get_content_type() != "application/json":
                    self._json(415, {"error": "Send application/json"})
                    return
                lengths = self.headers.get_all("Content-Length", [])
                try:
                    if self.headers.get("Transfer-Encoding") or len(lengths) != 1:
                        raise ValueError
                    length = int(lengths[0])
                    if not 0 < length <= _MAX_BODY:
                        raise ValueError
                except ValueError:
                    self._json(413, {"error": "JSON body must be between 1 and 4096 bytes"})
                    return
                try:
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        raise ValueError("Incomplete request body")
                    body = json.loads(raw)
                    if not isinstance(body, dict):
                        raise ValueError("Request body must be an object")
                    event = _command(target.path.removeprefix("/api/"), body)
                    result = command_fn(event)
                    self._json(202, result if result is not None else {"accepted": True})
                except (ValueError, UnicodeDecodeError) as exc:
                    self._json(400, {"error": str(exc)})
                except (TimeoutError, OSError):
                    self._json(408, {"error": "Request body timed out"})
                except Exception:
                    self._json(503, {"error": "Mission command is unavailable"})

        server_cls = ThreadingHTTPServer
        if ":" in host:
            class IPv6Server(ThreadingHTTPServer):
                address_family = socket.AF_INET6
            server_cls = IPv6Server
        self._httpd = server_cls((host, port), Handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        daemon=True, name="stand-http")
        self._thread.start()

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=2)

"""LlamaCppBackend against a fake llama-server (no GPU or model needed)."""

import base64
import json
import socket
import threading
import time
from concurrent.futures import Future
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from corvidia_perception.confirmer import normalize
from corvidia_perception.cosmos import LlamaCppBackend, LlamaCppConfig
from corvidia_perception.records import ConfirmRequest, Result
from corvidia_perception.worker import WorkerState

from conftest import person


class FakeLlamaServer:
    """mode: answer | slow | stuck | overflow | length | error"""

    def __init__(self) -> None:
        self.mode = "answer"
        self.answer = '{"answer": "yes"}'
        self.processing = False
        self.requests: list[dict] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def _json(self, code: int, obj) -> None:
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                if self.path == "/health":
                    self._json(200, {"status": "ok"})
                elif self.path == "/props":
                    self._json(200, {"model_path": "/models/fake.gguf", "build_info": "b-test"})
                elif self.path == "/slots":
                    self._json(200, [{"id": 0, "is_processing": fake.processing}])
                else:
                    self._json(404, {})

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(body)
                if fake.mode == "overflow":
                    return self._json(400, {"error": {"type": "exceed_context_size_error",
                                                      "message": "request exceeds the available context size"}})
                if fake.mode == "error":
                    return self._json(500, {"error": {"message": "boom"}})
                if fake.mode in ("slow", "stuck"):
                    fake.processing = True
                    # Like llama-server: stop when the client disconnects.
                    while True:
                        time.sleep(0.02)
                        try:
                            if self.connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b"":
                                break
                        except BlockingIOError:
                            continue
                        except OSError:
                            break
                    if fake.mode == "slow":
                        fake.processing = False
                    return
                finish = "length" if fake.mode == "length" else "stop"
                self._json(200, {
                    "choices": [{"message": {"content": fake.answer}, "finish_reason": finish}],
                    "usage": {"prompt_tokens": 123, "completion_tokens": 5},
                    "timings": {"prompt_ms": 400.0, "predicted_ms": 50.0},
                })

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    s = FakeLlamaServer()
    yield s
    s.close()


def backend_for(server, **kw) -> LlamaCppBackend:
    return LlamaCppBackend(LlamaCppConfig(url=server.url, cancel_ack_s=1.0, drain_idle_s=0.5, **kw))


def request(i: str = "r1") -> ConfirmRequest:
    return ConfirmRequest(i, b"\xff\xd8fakejpeg", "Is there a person?")


def wait(fut: Future, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not fut.done() and time.monotonic() < deadline:
        time.sleep(0.01)


def test_answer_is_parsed_with_usage_and_schema(server):
    b = backend_for(server)
    assert b.info.model_revision == "/models/fake.gguf" and b.info.backend_version == "b-test"
    fut = b.submit(request())
    wait(fut)
    result, reason, details = normalize(fut)
    assert (result, reason) == (Result.CONFIRMED, "answer_yes")
    assert details["usage"]["prompt_tokens"] == 123
    body = server.requests[0]
    assert body["response_format"]["json_schema"]["schema"]["properties"]["answer"]["enum"] == [
        "yes", "no", "uncertain"]
    image = body["messages"][0]["content"][0]["image_url"]["url"]
    assert base64.b64decode(image.split(",", 1)[1]) == request().image_jpeg
    assert body["temperature"] == 0.0


@pytest.mark.parametrize("mode, answer, expected", [
    ("answer", '{"answer": "no"}', (Result.REJECTED, "answer_no")),
    ("answer", '{"answer": "uncertain"}', (Result.UNKNOWN, "ambiguous")),
    ("answer", "Yes, a person.", (Result.UNKNOWN, "malformed_output")),
    ("length", '{"answer": "yes"}', (Result.UNKNOWN, "malformed_output")),
    ("overflow", "", (Result.UNKNOWN, "context_overflow")),
    ("error", "", (Result.UNKNOWN, "inference_error")),
])
def test_error_mapping(server, mode, answer, expected):
    server.mode, server.answer = mode, answer
    fut = backend_for(server).submit(request())
    wait(fut)
    assert normalize(fut)[:2] == expected


def test_unreachable_server_is_unavailable():
    b = LlamaCppBackend(LlamaCppConfig(url="http://127.0.0.1:9"))
    fut = b.submit(request())
    wait(fut)
    assert normalize(fut)[:2] == (Result.UNKNOWN, "server_unavailable")
    assert not b.healthy() and not b.reset()


def test_cancel_is_acknowledged_only_when_server_goes_idle(server):
    server.mode = "slow"
    b = backend_for(server)
    fut = b.submit(request())
    deadline = time.monotonic() + 2
    while not server.processing and time.monotonic() < deadline:
        time.sleep(0.01)
    assert b.is_idle() is False
    assert b.cancel("r1") is True
    assert b.is_idle() is True
    wait(fut)
    assert normalize(fut)[1] == "inference_error"  # cancelled; never an answer


def test_cancel_not_acknowledged_when_server_stays_busy(server):
    server.mode = "stuck"
    b = backend_for(server)
    fut = b.submit(request())
    deadline = time.monotonic() + 2
    while not server.processing and time.monotonic() < deadline:
        time.sleep(0.01)
    assert b.cancel("r1") is False
    time.sleep(1.0)  # longer than drain_idle_s
    assert not fut.done()  # parked: the worker will declare the backend unavailable
    server.processing = False
    assert b.reset() is True


def test_worker_timeout_with_real_adapter_recovers_after_cancel(harness, server):
    server.mode = "slow"
    h = harness(backend=backend_for(server))
    h.frames(3, person(1))
    deadline = time.monotonic() + 2
    while not server.processing and time.monotonic() < deadline:
        time.sleep(0.01)
    h.clock.advance(3.5)  # past the 3 s deadline
    h.pipe.tick()
    assert h.pipe.worker.state is WorkerState.IDLE
    (event,) = h.events()
    assert (event["result"], event["reason"]) == ("unknown", "timeout")
    assert server.processing is False

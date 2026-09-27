"""Confirmation backend interface, response normalization, and stub backends."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Protocol

from .records import ConfirmRequest, Result


class BackendError(Exception):
    reason = "inference_error"


class BackendUnavailable(BackendError):
    reason = "server_unavailable"


class ContextOverflow(BackendError):
    reason = "context_overflow"


@dataclass(frozen=True)
class BackendInfo:
    name: str
    model_revision: str
    backend_version: str


@dataclass(frozen=True)
class BackendReply:
    """Response text plus optional server-reported usage and timings."""

    text: str
    usage: dict | None = None
    timings: dict | None = None


class ConfirmerBackend(Protocol):
    """A local model server. Implementations must not retry transport errors."""

    info: BackendInfo

    def submit(self, request: ConfirmRequest) -> Future[str | BackendReply]:
        """Start one request; the future yields the raw response or raises BackendError."""
        ...

    def cancel(self, request_id: str) -> bool:
        """Request cancellation. True only if the backend confirms it is idle for this request."""
        ...

    def reset(self) -> bool:
        """Controlled recovery after the backend was marked unavailable. True if healthy."""
        ...


_ANSWERS = {"yes": Result.CONFIRMED, "no": Result.REJECTED, "uncertain": Result.UNKNOWN}


def parse_answer(text: str) -> tuple[Result, str]:
    """Parse a constrained `{"answer": "yes"|"no"|"uncertain"}` response.

    Only the answer field is read; free-form prose is malformed, never searched for "yes".
    """
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return Result.UNKNOWN, "malformed_output"
    if not isinstance(payload, dict):
        return Result.UNKNOWN, "malformed_output"
    answer = payload.get("answer")
    if not isinstance(answer, str) or answer not in _ANSWERS:
        return Result.UNKNOWN, "malformed_output"
    result = _ANSWERS[answer]
    return result, "ambiguous" if result is Result.UNKNOWN else f"answer_{answer}"


def normalize(future: Future) -> tuple[Result, str, dict]:
    """Map a finished backend future to (result, reason, model I/O details)."""
    try:
        reply = future.result(timeout=0)
    except BackendError as e:
        return Result.UNKNOWN, e.reason, {"error": str(e) or e.reason}
    except Exception as e:  # noqa: BLE001 - adapter bugs still map to unknown
        return Result.UNKNOWN, f"inference_error:{type(e).__name__}", {"error": repr(e)}
    if isinstance(reply, BackendReply):
        details = {"raw_output": reply.text, "usage": reply.usage, "timings": reply.timings}
        text = reply.text
    else:
        details = {"raw_output": reply}
        text = reply
    result, reason = parse_answer(text)
    return result, reason, details


Responder = Callable[[ConfirmRequest], "str | BaseException | None"]


class StubBackend:
    """Deterministic in-process backend for tests.

    `responder` returns response text, an exception to raise, or None to leave the
    request pending until `complete()` or `fail()` is called.
    """

    def __init__(self, responder: Responder | None = None, *, cancel_acks: bool = True,
                 reset_ok: bool = True) -> None:
        self.info = BackendInfo("stub", "stub", "0")
        self._responder = responder or (lambda _req: '{"answer": "yes"}')
        self.cancel_acks = cancel_acks
        self.reset_ok = reset_ok
        self.pending: dict[str, Future[str]] = {}
        self.requests: list[ConfirmRequest] = []
        self.cancelled: list[str] = []
        self.resets = 0

    def submit(self, request: ConfirmRequest) -> Future[str]:
        self.requests.append(request)
        fut: Future[str] = Future()
        response = self._responder(request)
        if response is None:
            self.pending[request.request_id] = fut
        elif isinstance(response, BaseException):
            fut.set_exception(response)
        else:
            fut.set_result(response)
        return fut

    def complete(self, request_id: str, text: str) -> None:
        self.pending.pop(request_id).set_result(text)

    def fail(self, request_id: str, exc: BaseException) -> None:
        self.pending.pop(request_id).set_exception(exc)

    def cancel(self, request_id: str) -> bool:
        self.cancelled.append(request_id)
        if not self.cancel_acks:
            return False
        fut = self.pending.pop(request_id, None)
        if fut is not None:
            fut.set_exception(BackendError("cancelled"))
        return True

    def reset(self) -> bool:
        self.resets += 1
        return self.reset_ok


class DelayedStubBackend:
    """Threaded stub that answers after a fixed delay; used by the simulator."""

    def __init__(self, delay_s: float, answer: Callable[[ConfirmRequest], str]) -> None:
        self.info = BackendInfo("delayed-stub", "stub", "0")
        self._delay_s = delay_s
        self._answer = answer
        self._cancel = threading.Event()

    def submit(self, request: ConfirmRequest) -> Future[str]:
        fut: Future[str] = Future()
        self._cancel.clear()

        def run() -> None:
            if self._cancel.wait(self._delay_s):
                fut.set_exception(BackendError("cancelled"))
            else:
                fut.set_result(self._answer(request))

        threading.Thread(target=run, daemon=True, name="stub-inference").start()
        return fut

    def cancel(self, request_id: str) -> bool:
        self._cancel.set()
        return False  # completion is observed through the future instead

    def reset(self) -> bool:
        return True

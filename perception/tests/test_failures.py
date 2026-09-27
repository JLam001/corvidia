"""Inference timeouts, backend loss, and storage failures."""

import json

from corvidia_perception import evidence
from corvidia_perception.confirmer import BackendUnavailable, ContextOverflow, StubBackend
from corvidia_perception.worker import WorkerState

from conftest import Harness, person


def hold(**kwargs) -> StubBackend:
    return StubBackend(lambda _req: None, **kwargs)


def test_timeout_then_late_answer_cannot_confirm(harness):
    h = harness(backend=hold(cancel_acks=False))
    h.frames(3, person(1))
    (request,) = h.backend.requests
    h.frames(31, person(1))  # past the 3 s deadline
    assert h.pipe.worker.state is WorkerState.DRAINING
    assert h.backend.cancelled == [request.request_id]
    (event,) = h.events()
    assert (event["result"], event["reason"]) == ("unknown", "timeout")

    # A new candidate waits while the backend is draining.
    h.frames(3, person(2, x=200))
    assert len(h.backend.requests) == 1

    h.backend.complete(request.request_id, '{"answer": "yes"}')
    h.pipe.tick()
    assert h.pipe.health.count("late_results_ignored") == 1
    assert h.pipe.worker.state is not WorkerState.DRAINING
    first = json.loads((h.session_dir / request.request_id / "event.json").read_text())
    assert first["result"] == "unknown"
    assert not any(c.result.value == "confirmed" and c.event_id == request.request_id
                   for c in h.pipe.completions)


def test_acknowledged_cancel_frees_worker_immediately(harness):
    h = harness(backend=hold(cancel_acks=True))
    h.frames(3, person(1))
    h.frames(31, person(1))
    assert h.pipe.worker.state is not WorkerState.DRAINING
    assert h.pipe.health.count("confirmation_timeouts") == 1


def test_backend_that_never_goes_idle_becomes_unavailable_until_recovered(harness):
    h = harness(backend=hold(cancel_acks=False, reset_ok=True))
    h.frames(3, person(1), person(2, x=200))
    h.frames(85, person(1), person(2, x=200))  # 3 s deadline + 5 s drain
    assert h.pipe.worker.state is WorkerState.UNAVAILABLE
    assert "backend" in h.pipe.health.snapshot()["faults"]
    reasons = {s["reason"] for s in h.skips()}
    assert "backend_unavailable" in reasons or "queue_expired" in reasons

    queued_before = h.pipe.health.count("candidates_queued")
    h.frames(10, person(3, x=120))
    assert h.pipe.health.count("candidates_queued") == queued_before  # admission blocked
    assert len(h.backend.requests) == 1

    assert h.pipe.worker.recover()
    assert h.backend.resets == 1
    h.frames(3, person(3, x=120))
    assert len(h.backend.requests) == 2


def test_backend_errors_map_to_unknown(harness):
    errors = iter([BackendUnavailable(), ContextOverflow(), RuntimeError("boom"), "I think yes"])
    h = harness(backend=StubBackend(lambda _r: next(errors)))
    for track in range(4):
        h.frames(3, person(track, x=10 + 70 * track))
        h.idle(2.0)
    reasons = sorted(e["reason"] for e in h.events())
    assert reasons == ["context_overflow", "inference_error:RuntimeError",
                       "malformed_output", "server_unavailable"]
    assert {e["result"] for e in h.events()} == {"unknown"}


def test_storage_limit_blocks_admission_and_raises_fault(harness):
    h = harness(free_bytes=lambda _p: 0)
    h.frames(10, person(1))
    assert h.backend.requests == []
    assert "storage_limit" in h.pipe.health.snapshot()["faults"]
    assert h.pipe.health.count("admission_blocked_storage") > 0


def test_evidence_write_failure_skips_dispatch(harness, monkeypatch):
    real = evidence.atomic_write

    def failing(path, data):
        if path.name == "crop.jpg":
            raise OSError(28, "No space left on device")
        real(path, data)

    monkeypatch.setattr(evidence, "atomic_write", failing)
    h = harness()
    h.frames(10, person(1))
    assert h.backend.requests == []
    assert [s["reason"] for s in h.skips()][:1] == ["storage_error"]
    assert "storage" in h.pipe.health.snapshot()["faults"]
    assert not list(h.session_dir.glob("*/event.json"))


def test_result_write_failure_is_not_a_successful_capture(harness, monkeypatch):
    h = harness()

    def failing(event_id, record):
        raise evidence.StorageError("disk full")

    monkeypatch.setattr(h.pipe.store, "commit_result", failing)
    h.frames(3, person(1))
    assert list(h.pipe.completions) == []
    (event,) = h.events()
    assert event["status"] == "pending"


def test_interrupted_events_are_marked_on_startup(tmp_path, monkeypatch):
    h = Harness(tmp_path, backend=hold())
    h.frames(3, person(1))
    (event,) = h.events()
    assert event["status"] == "pending"

    # A second event directory that crashed after the frame but before the record.
    partial = h.session_dir / "partial-event"
    partial.mkdir()
    (partial / "frame.jpg").write_bytes(b"x")
    (partial / ".tmp-crop.jpg").write_bytes(b"x")

    restarted = Harness(tmp_path, backend=hold())
    assert len(restarted.pipe.interrupted) == 2
    for path in (h.session_dir / event["event_id"], partial):
        record = json.loads((path / "event.json").read_text())
        assert record["status"] == "interrupted"
        assert record["result"] is None
    assert not (partial / ".tmp-crop.jpg").exists()

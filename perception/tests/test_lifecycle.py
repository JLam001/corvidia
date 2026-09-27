"""Behavioral checks from docs/event-pipeline.md: gate, queue, and lifecycle."""

from corvidia_perception import gate as gate_module
from corvidia_perception.confirmer import StubBackend
from corvidia_perception.gate import Phase
from corvidia_perception.records import CandidateKey

from conftest import person


def hold() -> StubBackend:
    return StubBackend(lambda _req: None)


def test_persistent_track_creates_one_confirmed_event_and_other_track_is_eligible(harness):
    h = harness()
    h.frames(20, person(1))
    events = h.events()
    assert len(events) == 1
    assert events[0]["track_id"] == 1
    assert events[0]["status"] == "complete"
    assert events[0]["result"] == "confirmed"

    h.frames(20, person(1), person(2, x=200))
    events = h.events()
    assert sorted(e["track_id"] for e in events) == [1, 2]
    assert len(h.backend.requests) == 2


def test_interleaved_tracks_cannot_satisfy_one_gate(harness):
    h = harness()
    a, b = person(1), person(2, x=200)
    for det in (a, b, a, b):
        h.frame(det)
    assert h.backend.requests == []
    # Track 1 now has its own 3 hits in the last 5 frames.
    h.frame(a)
    assert len(h.backend.requests) == 1


def test_short_flicker_does_not_trigger(harness):
    h = harness()
    h.frames(2, person(1))
    h.frames(10)
    h.frame(person(1))
    assert h.backend.requests == []


def test_observations_spread_beyond_time_window_do_not_trigger(harness):
    h = harness(fps=1.8)  # three consecutive frames span ~1.1 s > 1.0 s window
    h.frames(10, person(1))
    assert h.backend.requests == []


def test_camera_loss_clears_observation_history(harness):
    h = harness()
    h.frames(2, person(1))
    h.pipe.source_lost()
    h.frame(person(1))
    assert h.backend.requests == []
    h.frames(2, person(1))
    assert len(h.backend.requests) == 1


def test_long_processing_gap_clears_history(harness):
    h = harness()
    h.frames(2, person(1))
    h.clock.advance(1.5)
    h.frame(person(1))
    assert h.backend.requests == []


def test_low_confidence_detections_keep_a_confirmed_encounter_alive(harness):
    h = harness()
    h.frames(5, person(1))
    h.frames(30, person(1, conf=0.15))  # 3 s of weak but still-tracked detections
    h.frames(10, person(1))
    assert len(h.backend.requests) == 1


def test_low_confidence_and_tiny_boxes_do_not_count(harness):
    h = harness()
    h.frames(5, person(1, conf=0.1))
    h.frames(5, person(2, w=5, h=10))
    assert h.backend.requests == []


def test_queue_overflow_admits_at_most_two_and_does_not_copy_images(harness, monkeypatch):
    calls = []
    real = gate_module.select_crop
    monkeypatch.setattr(gate_module, "select_crop", lambda *a: calls.append(1) or real(*a))
    h = harness(backend=hold())
    dets = [person(i, x=10 + 70 * i) for i in range(4)]
    h.frames(3, *dets, tick=False)
    assert len(h.pipe.queue) == 2
    assert h.pipe.health.count("admission_blocked_queue_full") == 2
    assert len(calls) == 2  # no pixels copied for rejected admissions

    # Worker takes one; the next fresh observation of a waiting track fills the slot.
    h.pipe.tick()
    assert len(h.pipe.queue) == 1
    h.frame(*dets, tick=False)
    assert len(h.pipe.queue) == 2
    phases = [h.pipe.gate.tracks[CandidateKey("s1", 0, i)].phase for i in range(4)]
    assert phases.count(Phase.PENDING) == 3
    assert phases.count(Phase.OBSERVING) == 1


def test_slow_inference_keeps_queue_bounded(harness):
    h = harness(backend=hold())
    dets = [person(i, x=5 + 30 * i, w=25) for i in range(10)]
    for _ in range(25):  # 2.5 s, below the 3 s deadline
        h.frame(*dets)
        assert len(h.pipe.queue) <= 2
    assert len(h.backend.requests) == 1


def test_queue_expiry_is_recorded_as_skip_not_rejection(harness):
    h = harness(backend=hold())
    h.frames(3, person(1), person(2, x=200))
    assert len(h.backend.requests) == 1
    assert len(h.pipe.queue) == 1
    h.frames(21, person(1), person(2, x=200))
    skips = h.skips()
    assert skips and skips[0]["reason"] == "queue_expired"
    assert skips[0]["track_id"] == 2
    events = h.events()
    assert [e["track_id"] for e in events] == [1]  # expired candidate was never committed
    assert all(e["result"] != "rejected" for e in events)


def test_track_lost_before_dispatch_is_skipped(harness):
    h = harness(backend=hold())
    h.frames(3, person(1), person(2, x=200))
    h.frames(16, person(1))
    reasons = [(s["track_id"], s["reason"]) for s in h.skips()]
    assert (2, "track_lost") in reasons
    assert len(h.pipe.queue) == 0


def test_result_stays_tied_to_original_image_after_track_leaves(harness):
    h = harness(backend=hold())
    h.frames(3, person(1))
    request = h.backend.requests[0]
    h.idle(2.0)  # track lost while confirming
    h.backend.complete(request.request_id, '{"answer": "yes"}')
    h.pipe.tick()
    (event,) = h.events()
    assert event["result"] == "confirmed"
    crop = (h.session_dir / event["event_id"] / "crop.jpg").read_bytes()
    assert crop == request.image_jpeg


def test_source_restart_drops_queued_work_and_starts_new_epoch(harness):
    h = harness(backend=hold())
    h.frames(3, person(1), person(2, x=200))
    first = h.backend.requests[0]
    h.epoch = 1
    h.frame(person(1), person(2, x=200))
    assert [s["reason"] for s in h.skips()] == ["source_reset"]
    assert all(k.source_epoch == 1 for k in h.pipe.gate.tracks)

    h.backend.complete(first.request_id, '{"answer": "yes"}')
    h.frames(3, person(1))
    events = {(e["source_epoch"], e["track_id"]) for e in h.events()}
    assert events == {(0, 1), (1, 1)}


def test_frozen_crop_is_independent_of_camera_buffer(harness):
    h = harness(backend=hold())
    h.frames(3, person(1), tick=False)
    (candidate,) = h.pipe.queue._items.values()
    before = candidate.crop.copy()
    h.image[:] = 255
    assert (candidate.crop == before).all()
    assert not candidate.crop.flags.writeable
    assert candidate.frame is not None and not candidate.frame.flags.writeable


def test_rejected_result_suppresses_encounter(harness):
    h = harness(backend=StubBackend(lambda _r: '{"answer": "no"}'))
    h.frames(30, person(1))
    assert [e["result"] for e in h.events()] == ["rejected"]


def test_unknown_retries_once_after_cooldown_with_fresh_evidence(harness):
    h = harness(backend=StubBackend(lambda _r: '{"answer": "uncertain"}'))
    h.frames(3, person(1))
    assert len(h.backend.requests) == 1
    h.frames(45, person(1))  # still inside the 5 s cooldown
    assert len(h.backend.requests) == 1
    h.frames(100, person(1))
    events = sorted(h.events(), key=lambda e: e["attempt"])
    assert [(e["attempt"], e["result"], e["reason"]) for e in events] == [
        (1, "unknown", "ambiguous"),
        (2, "unknown", "ambiguous"),
    ]
    gap = events[1]["timestamps"]["queued_mono"] - events[0]["timestamps"]["dispatched_mono"]
    assert gap >= 5.0


def test_missing_pose_attitude_distance_never_blocks(harness):
    h = harness()
    h.frames(3, person(1))
    (event,) = h.events()
    assert event["result"] == "confirmed"
    assert event["pose"] is None and event["attitude"] is None and event["distance_m"] is None
    assert event["location_status"] == "unavailable"
    assert event["person_score"] is None

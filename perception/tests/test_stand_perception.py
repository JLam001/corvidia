"""No serial or hardware: mission appearance, event provenance and bounded IPC."""

import base64
import hashlib
import io
import json
import queue
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from corvidia_perception import evidence
from corvidia_perception.confirmer import BackendReply, BackendUnavailable, StubBackend
from corvidia_perception.cosmos import APPEARANCE_SYSTEM, LlamaCppBackend, LlamaCppConfig, validate_appearance
from corvidia_perception.records import ConfirmRequest, Frame, FrameInfo, ClockDomain
from corvidia_perception.stand_perception import (
    EventSink, ManualGuidance, _begin_values, _stand_pipeline_config,
)

from conftest import person


def drain(q):
    result = []
    while not q.empty():
        result.append(q.get_nowait())
    return result


def observed_backend(**fields):
    observed = {"subject": "human", "upper_color": "red", "upper_garment": "shirt", **fields}
    return StubBackend(lambda _request: json.dumps(observed))


def test_stand_omits_extra_best_shots_but_commits_primary_frame_and_crop(tmp_path, harness):
    from corvidia_perception.config import PipelineConfig

    cfg = _stand_pipeline_config("~/models/yolo11n.engine", tmp_path / "stand-events")
    assert cfg.best_shot.enabled is False
    assert PipelineConfig().best_shot.enabled is True  # General pipeline is unchanged.
    assert cfg.storage.save_frame is True
    assert cfg.storage.root == tmp_path / "stand-events"
    assert cfg.detector.model == "~/models/yolo11n.engine"
    assert cfg.depth.enabled is False and cfg.freespace.hz == 0
    assert cfg.confirm.deadline_s == 4 and cfg.queue.max_pending == 1
    notices = queue.Queue(maxsize=16)
    h = harness(cfg=cfg, appearance="wearing a red shirt", observer_queue=notices,
                backend=observed_backend())
    assert h.pipe.best is None
    h.frames(3, person(1))
    event = h.events()[0]
    directory = h.session_dir / event["event_id"]
    assert (directory / "frame.jpg").is_file()
    assert (directory / "crop.jpg").read_bytes() == h.backend.requests[0].image_jpeg
    assert drain(notices)[-1]["committed"] is True
    h.pipe.stop()
    assert not (directory / "best.jpg").exists()


@pytest.mark.parametrize("value", [None, "", "   ", "x" * 241, "red\nshirt", "red\x00shirt"])
def test_appearance_is_bounded_single_line_data(value):
    with pytest.raises(ValueError):
        validate_appearance(value)


def test_appearance_request_asks_for_independent_observations_without_desired_traits():
    backend = LlamaCppBackend.__new__(LlamaCppBackend)
    backend.cfg = LlamaCppConfig()
    description = 'person wearing a blue polo'
    req = ConfirmRequest("r1", b"exact jpeg bytes", "ignored mission instructions",
                         {"target_appearance": description,
                          "requirements": {"upper_color": "blue", "upper_garment": "polo"}})
    payload = backend.build_payload(req)
    assert payload["messages"][0] == {"role": "system", "content": APPEARANCE_SYSTEM}
    content = payload["messages"][1]["content"]
    assert content[1]["text"] == "Describe TARGET: subject, upper_garment, upper_color."
    assert description not in json.dumps(payload)
    assert '"requirements"' not in json.dumps(payload)
    assert base64.b64decode(content[0]["image_url"]["url"].split(",", 1)[1]) == req.image_jpeg
    assert payload["max_tokens"] == 96
    assert payload["response_format"]["json_schema"]["schema"]["additionalProperties"] is False


def test_marked_crop_saved_and_sent_identically_without_altering_frame(harness):
    notices = queue.Queue(maxsize=16)
    h = harness(appearance="wearing a red shirt", observer_queue=notices,
                backend=observed_backend())
    h.frames(3, person(1))
    request = h.backend.requests[0]
    event = h.events()[0]
    directory = h.session_dir / event["event_id"]
    assert request.image_jpeg == (directory / "crop.jpg").read_bytes()
    assert event["files"]["crop_sha256"] == hashlib.sha256(request.image_jpeg).hexdigest()
    assert event["target"]["appearance"] == "wearing a red shirt"
    crop = np.asarray(Image.open(io.BytesIO(request.image_jpeg)))
    raw = np.asarray(Image.open(directory / "frame.jpg"))
    assert int(crop.max()) > 100  # TARGET outline was drawn
    assert int(raw.max()) < 10   # original flat frame has no annotation
    assert np.all(h.image == 3)  # source buffer remains untouched
    notifications = drain(notices)
    assert [(n["stage"], n["phase"]) for n in notifications if n["type"] == "stage"] == [
        ("saving", "candidate"), ("confirming", "inference"), ("saving", "result")]
    completion = notifications[-1]
    assert completion["type"] == "completion" and completion["committed"] is True
    assert json.loads((directory / "event.json").read_text())["status"] == "complete"
    assert completion["path"] == str(directory.resolve())
    assert completion["capture_mono"] == pytest.approx(100.2)
    assert completion["track_id"] == event["track_id"] == 1
    assert len(h.pipe.completions) == 1  # observer did not consume preview history


def test_stand_collects_other_tracks_without_recapturing_visible_matches(tmp_path, harness):
    """One active and one pending request still progress through a small crowd."""
    cfg = _stand_pipeline_config("~/models/yolo11n.engine", tmp_path / "stand-events")
    notices = queue.Queue(maxsize=64)
    backend = StubBackend(lambda _: None)
    h = harness(cfg=cfg, appearance="person", backend=backend, observer_queue=notices)
    people = [person(track, x=20 + 75 * (track - 1)) for track in range(1, 5)]
    h.frames(3, *people)
    assert len(backend.requests) == 1
    reply = json.dumps({"subject": "human", "upper_color": "unknown", "upper_garment": "unknown"})
    for _ in people:
        # Keep every track alive and force meaningful queue pressure while one
        # VLM request is in flight. No request is submitted twice for a match.
        h.frames(8, *people)
        assert len(h.pipe.queue) <= 1
        active = h.pipe.worker._inflight.candidate.event_id
        backend.complete(active, reply)
        h.pipe.tick()
        h.frames(3, *people)
    h.frames(30, *people)
    captures = [item for item in drain(notices) if item["type"] == "completion"]
    assert len(backend.requests) == len(captures) == 4
    assert {item["track_id"] for item in captures} == {1, 2, 3, 4}
    assert all(item["result"] == "confirmed" and item["committed"] for item in captures)
    assert {item["session_id"] for item in captures} == {"s1"}
    assert {item["source_epoch"] for item in captures} == {0}
    for item in captures:
        folder = h.session_dir / item["event_id"]
        event = json.loads((folder / "event.json").read_text())
        assert event["track_id"] == item["track_id"]
        assert (folder / "frame.jpg").is_file() and (folder / "crop.jpg").is_file()


def test_reentry_after_track_loss_is_another_sighting_not_a_unique_person(harness):
    notices = queue.Queue(maxsize=32)
    h = harness(appearance="person", backend=observed_backend(), observer_queue=notices)
    h.frames(10, person(1))
    h.idle(2.0)
    # Tracking is not person re-identification: a new ID may belong to the same
    # person returning to the camera. Consumers must label these as sightings.
    h.frames(10, person(2))
    captures = [item for item in drain(notices) if item["type"] == "completion"]
    assert [item["track_id"] for item in captures] == [1, 2]
    assert all(item["result"] == "confirmed" for item in captures)


@pytest.mark.parametrize("observed,result", [
    ({"subject": "human", "upper_color": "gray", "upper_garment": "hoodie"}, "rejected"),
    ({"subject": "human", "upper_color": "white", "upper_garment": "polo"}, "rejected"),
    ({"subject": "human", "upper_color": "blue", "upper_garment": "hoodie"}, "rejected"),
    ({"subject": "human", "upper_color": "blue", "upper_garment": "polo"}, "confirmed"),
    ({"subject": "human", "upper_color": "blue", "upper_garment": "unknown"}, "unknown"),
    ({"subject": "nonhuman", "upper_color": "blue", "upper_garment": "polo"}, "rejected"),
    ({"subject": "human", "upper_color": "blue"}, "unknown"),
    ({"answer": "yes"}, "unknown"),
])
def test_blue_polo_match_is_decided_from_observed_attributes_and_saved(harness, observed, result):
    backend = StubBackend(lambda _: BackendReply(json.dumps(observed), {"tokens": 30}, {"ms": 250}))
    h = harness(appearance="person wearing a blue polo", backend=backend)
    h.frames(3, person(1))
    event = h.events()[0]
    requirements = {"subject": "human", "upper_color": "blue", "upper_garment": "polo"}
    assert event["result"] == result
    assert event["target"]["requirements"] == requirements
    assert event["target"]["requirements_version"] == "appearance-v1"
    details = event["model_io"]
    assert details["raw_output"] == json.dumps(observed)
    assert details["usage"] == {"tokens": 30} and details["timings"] == {"ms": 250}
    assert details["appearance_evaluation"]["observed"] == observed
    assert details["appearance_evaluation"]["requirements"] == requirements
    assert details["appearance_evaluation"]["reason"] == event["reason"]
    assert len(h.pipe.completions) == 1
    assert h.pipe.completions[0].result.value == result


@pytest.mark.parametrize("reply", ["yes", "{", None])
def test_unstructured_appearance_reply_cannot_complete_mission(harness, reply):
    # BackendReply permits a completed malformed value, including null.
    h = harness(appearance="blue polo", backend=StubBackend(lambda _: BackendReply(reply)))
    h.frames(3, person(1))
    assert h.events()[0]["result"] == "unknown"
    assert all(item.result.value != "confirmed" for item in h.pipe.completions)


@pytest.mark.parametrize("reply", [
    '{"subject":"nonhuman","subject":"human","upper_color":"blue","upper_garment":"polo"}',
    '{"subject":"human","upper_color":"white","upper_color":"blue","upper_garment":"polo"}',
    '{"subject":"human","upper_color":NaN,"upper_garment":"polo"}',
    '{"subject":"human","upper_color":Infinity,"upper_garment":"polo"}',
])
def test_ambiguous_or_nonfinite_observations_are_unknown_and_evidence_still_commits(harness, reply):
    notices = queue.Queue(maxsize=16)
    h = harness(appearance="blue polo", backend=StubBackend(lambda _: reply), observer_queue=notices)
    h.frames(3, person(1))
    event = h.events()[0]
    assert (event["result"], event["reason"]) == ("unknown", "unsupported_observations")
    assert event["model_io"]["raw_output"] == reply
    assert event["model_io"]["appearance_evaluation"]["observed"] is None
    assert drain(notices)[-1]["committed"] is True


def test_appearance_backend_failure_preserves_transport_reason(harness):
    h = harness(appearance="blue polo", backend=StubBackend(lambda _: BackendUnavailable("offline")))
    h.frames(3, person(1))
    event = h.events()[0]
    assert event["result"] == "unknown" and event["reason"] == "server_unavailable"
    assert event["model_io"]["error"] == "offline"
    assert all(item.result.value != "confirmed" for item in h.pipe.completions)


@pytest.mark.parametrize("late_by", [0., .001])
@pytest.mark.parametrize("appearance,reply", [
    (None, '{"answer":"yes"}'),
    ("blue polo", '{"subject":"human","upper_color":"blue","upper_garment":"polo"}'),
])
def test_completed_future_at_or_after_deadline_cannot_confirm(harness, late_by, appearance, reply):
    backend = StubBackend(lambda _: None)
    h = harness(appearance=appearance, backend=backend)
    h.frames(3, person(1))
    request = backend.requests[0]
    h.clock.t = h.pipe.worker._inflight.deadline + late_by
    # The late model answer arrives before the next scheduled worker tick.
    backend.complete(request.request_id, reply)
    h.pipe.tick()
    event = h.events()[0]
    assert (event["result"], event["reason"]) == ("unknown", "timeout")
    assert all(item.result.value != "confirmed" for item in h.pipe.completions)
    assert h.pipe.health.count("late_results_ignored") == 1
    assert backend.cancelled == []  # already-completed work needs no cancellation


def test_generic_pipeline_still_accepts_legacy_yes_and_person_only_uses_observations(harness):
    h = harness()
    h.frames(3, person(1))
    assert h.events()[0]["result"] == "confirmed"
    assert "target" not in h.events()[0]


def test_person_only_mission_requires_human_observation_without_clothing_match(harness):
    h = harness(appearance="person", backend=observed_backend(upper_color="unknown", upper_garment="unknown"))
    h.frames(3, person(1))
    assert h.events()[0]["result"] == "confirmed"
    assert h.events()[0]["target"]["requirements"] == {
        "subject": "human", "upper_color": None, "upper_garment": None}


def test_failed_commit_notifies_failure_but_is_not_in_success_history(harness, monkeypatch):
    notices = queue.Queue(maxsize=16)
    h = harness(observer_queue=notices)

    def fail(*_):
        raise evidence.StorageError("disk full")

    monkeypatch.setattr(h.pipe.store, "commit_result", fail)
    h.frames(3, person(1))
    completion = drain(notices)[-1]
    assert completion["type"] == "completion" and completion["committed"] is False
    assert not h.pipe.completions


def test_full_observer_latches_fault_and_stops_new_admission(harness):
    notices = queue.Queue(maxsize=1)
    h = harness(observer_queue=notices)
    h.frames(3, person(1))
    assert h.pipe.observer_overflow.is_set()
    assert "observer_overflow" in h.pipe.health.snapshot()["faults"]
    drain(notices)
    h.frames(3, person(2))
    assert len(h.backend.requests) == 1
    assert len(h.pipe.completions) == 1


def test_background_confirmation_exception_reaches_supervisor(harness, monkeypatch):
    notices = queue.Queue(maxsize=4)
    h = harness(observer_queue=notices)

    def fail():
        raise RuntimeError("unexpected worker failure")

    monkeypatch.setattr(h.pipe, "tick", fail)
    h.pipe.start()
    fault = notices.get(timeout=1.0)
    h.pipe.stop()
    assert fault["type"] == "fault"
    assert "unexpected worker failure" in fault["reason"]
    assert "worker_thread" in h.pipe.health.snapshot()["faults"]


def test_ipc_full_latches_even_after_queue_is_drained():
    output = queue.Queue(maxsize=1)
    sink = EventSink(output)
    observer = sink.for_mission("m1")
    observer.put_nowait({"type": "stage", "stage": "saving"})
    with pytest.raises(queue.Full):
        observer.put_nowait({"type": "completion"})
    assert sink.failed.is_set()
    assert output.get_nowait()["mission_id"] == "m1"
    assert sink.send({"type": "progress"}) is False


def test_guidance_excludes_rejected_tracks_and_holds_pending(harness):
    from corvidia_perception.confirmer import StubBackend

    h = harness(backend=StubBackend(lambda _: '{"answer":"no"}'))
    h.frames(4, person(1, x=240))
    info = FrameInfo("test", 0, 5, 320, 240, 100.4, 0.0)
    frame = Frame(info, h.image)
    guide = ManualGuidance(100.0)
    assert guide.update(frame, [person(1, x=240)], h.pipe, 100.4) == "search_right"
    h.frame(person(2, x=240), tick=False)
    assert guide.update(frame, [person(2, x=240)], h.pipe, 100.4) == "right"
    h.frames(2, person(2, x=240), tick=False)
    assert guide.update(frame, [person(2, x=240)], h.pipe, 100.4) == "hold"
    assert guide.update(frame, [person(2, x=240)], h.pipe, 101.0) == "hold"  # stale frame


@pytest.mark.parametrize("change", [
    {"mission_id": "../escape"}, {"start_mono": float("nan")}, {"start_mono": True},
    {"appearance": ""}, {"record_extra": []},
])
def test_begin_rejects_invalid_identity_description_and_time(change):
    command = {"mission_id": "m1", "appearance": "red shirt", "start_mono": 10.0}
    command.update(change)
    with pytest.raises(ValueError):
        _begin_values(command)


def test_worker_ignores_premission_frames_and_faults_on_source_reset(tmp_path, monkeypatch):
    """Drive the actual process loop with fake camera/model, never opening hardware."""
    from corvidia_perception import stand_perception, detector, cosmos, sources, pipeline

    clock = [100.0]
    commands, events = queue.Queue(), queue.Queue()
    fed = []
    closed = []

    class Model:
        last_ms = 1
        def warmup(self, shape):
            assert shape == (720, 1280, 3)
        def detect(self, image):
            return [], []
        def close(self):
            closed.append("model")

    class Backend:
        info = SimpleNamespace(model_revision="real-interface-test-double")
        def __init__(self, *_): pass
        def healthy(self): return True
        def is_idle(self): return True

    class Tracker:
        def __init__(self, *_): pass
        def reset(self): pass
        def update(self, *_): return []

    class Source:
        connected = SimpleNamespace(wait=lambda _: True)
        def __init__(self, *_, **__):
            self.index = 0
        def read(self, **_):
            self.index += 1
            clock[0] = 100.15 + self.index * 0.05
            captured = (100.05, 100.10, 100.25, 100.30)[self.index - 1]
            epoch = 1 if self.index == 4 else 0
            if self.index == 1:
                commands.put({"type": "begin", "mission_id": "m1", "appearance": "red shirt",
                              "start_mono": 100.20})
            info = FrameInfo("fake", epoch, self.index, 1280, 720, clock[0], 0,
                             capture_ts=captured, capture_clock=ClockDomain.HOST_MONOTONIC)
            return Frame(info, np.zeros((2, 2, 3), np.uint8))
        def close(self):
            closed.append("camera")

    class Pipe:
        def __init__(self, cfg, *_, **__):
            self.cfg = cfg
            self.store = SimpleNamespace(accepting=lambda: True, session_dir=tmp_path)
            self.health = SimpleNamespace(snapshot=lambda: {"faults": {}})
            self.gate = SimpleNamespace(tracks={})
            self.worker = SimpleNamespace(state=SimpleNamespace(value="idle"))
            self.observer_overflow = SimpleNamespace(is_set=lambda: False)
        def start(self): pass
        def stop(self): closed.append("pipeline")
        def on_frame(self, frame, _): fed.append(frame.info.frame_id)

    monkeypatch.setattr(stand_perception, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(detector, "make_detector", lambda _: Model())
    monkeypatch.setattr(detector, "PersonTracker", Tracker)
    monkeypatch.setattr(cosmos, "LlamaCppBackend", Backend)
    monkeypatch.setattr(sources, "CameraSource", Source)
    monkeypatch.setattr(pipeline, "EventPipeline", Pipe)
    stand_perception.perception_worker(commands, events, None, {"events_root": str(tmp_path)})
    observed = drain(events)
    assert observed[0]["type"] == "ready"
    assert fed == [3]  # frame 2 was after begin, but captured before mission start
    assert observed[-1]["type"] == "fault"
    assert "epoch changed" in observed[-1]["reason"]
    assert observed[-1]["mission_id"] == "m1"
    assert set(closed) == {"model", "camera", "pipeline"}


@pytest.mark.parametrize("steps, error", [
    ([(1.0, False), (3.0, False), (4.0, True)], None),
    ([(1.0, False), (15.01, False)], "no first frame within 15 s"),
    ([(1.0, False), (3.0, True), (3.8, False)], "no advancing camera frame for 0.75 s"),
])
def test_worker_first_frame_grace_does_not_relax_runtime_freshness(monkeypatch, steps, error):
    from corvidia_perception import stand_perception, detector, cosmos, sources

    clock = [100.0]
    commands, events = queue.Queue(), queue.Queue()
    detector_frames = []

    class Model:
        def warmup(self, _): pass
        def detect(self, image):
            detector_frames.append(clock[0])
            return [], []
        def close(self): pass

    class Backend:
        info = SimpleNamespace(model_revision="test-double")
        def __init__(self, *_): pass
        def healthy(self): return True
        def is_idle(self): return True

    class Tracker:
        def __init__(self, *_): pass
        def reset(self): pass
        def update(self, *_): return []

    class Source:
        connected = SimpleNamespace(wait=lambda _: True)
        def __init__(self, *_, **__):
            self.steps = iter(steps)
            self.frame_id = 0
        def read(self, **_):
            try:
                elapsed, is_frame = next(self.steps)
            except StopIteration:
                commands.put({"type": "shutdown"})
                return None
            clock[0] = 100.0 + elapsed
            if not is_frame:
                if self.frame_id == 0:
                    assert events.empty()  # no ready/progress during startup grace
                return None
            self.frame_id += 1
            info = FrameInfo("fake", 0, self.frame_id, 1280, 720, clock[0], 0,
                             capture_ts=clock[0], capture_clock=ClockDomain.HOST_MONOTONIC)
            return Frame(info, np.zeros((2, 2, 3), np.uint8))
        def close(self): pass

    monkeypatch.setattr(stand_perception, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(detector, "make_detector", lambda _: Model())
    monkeypatch.setattr(detector, "PersonTracker", Tracker)
    monkeypatch.setattr(cosmos, "LlamaCppBackend", Backend)
    monkeypatch.setattr(sources, "CameraSource", Source)
    stand_perception.perception_worker(commands, events, None, {})
    observed = drain(events)
    ready = [e for e in observed if e["type"] == "ready"]
    faults = [e for e in observed if e["type"] == "fault"]
    assert len(ready) == len(detector_frames)
    if ready:
        assert ready[0]["capture_mono"] == detector_frames[0]
    if error:
        assert len(faults) == 1 and error in faults[0]["reason"]
    else:
        assert not faults and len(ready) == 1

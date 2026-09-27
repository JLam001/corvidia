"""Timed collection boundaries with fake time and no hardware access."""
import json
import queue

import pytest

from corvidia_perception.stand import AsyncJournal, MissionSpec, StandSupervisor
from test_stand import Clock, Motor, progress


def running(*, duration=30, root=None, journal=None):
    clock, motor, commands = Clock(), Motor(), queue.Queue(16)
    sup = StandSupervisor(lambda: motor, commands, clock=clock,
                          evidence_root=root, journal=journal)
    sup.memory_available_mib = 2048
    sup.event(dict(type="ready", capture_mono=clock(), source_epoch=0))
    receipt = sup.submit(dict(action="mission", appearance=
                             f"find as many people as possible within {duration} seconds"))
    sup.tick()
    assert sup.spec.mission_id == receipt["mission_id"]
    assert sup.state == "searching"
    assert commands.get_nowait()["type"] == "begin"
    return sup, clock, motor, commands


def capture(sup, clock, track=1, **overrides):
    event = dict(type="completion", mission_id=sup.spec.mission_id, result="confirmed",
                 committed=True, capture_mono=clock() - .05, commit_mono=clock(),
                 source_epoch=0, track_id=track, event_id=f"event-{track}", path=f"/tmp/event-{track}")
    event.update(overrides)
    sup.event(event)


def test_multiple_captures_keep_running_and_deadline_never_moves():
    sup, clock, motor, _ = running()
    assert motor.starts == [(5, 30000)] and sup.deadline == 130
    for index in range(1, 4):
        clock.t += .1
        progress(sup, clock)
        capture(sup, clock, index)
        sup.tick()
        assert sup.state == "searching" and not motor.stops
        assert sup.snapshot()["capture_count"] == index
        assert sup.deadline == 130
    clock.t = 130
    progress(sup, clock)
    refreshes = motor.refreshes
    sup.tick()
    assert sup.state == "complete" and motor.stops == ["collection time elapsed"]
    assert motor.refreshes == refreshes and motor.s["zero_confirmed"]
    assert sup.result["capture_count"] == 3
    assert sup.result["evidence"]["event_id"] == "event-3"


def test_no_matches_still_completes_at_collection_deadline():
    sup, clock, motor, _ = running()
    clock.t = sup.deadline
    progress(sup, clock)
    sup.tick()
    assert sup.state == "complete" and sup.result["capture_count"] == 0
    assert not sup.snapshot()["evidence_available"] and motor.s["zero_confirmed"]


def test_duplicate_track_or_event_does_not_inflate_count():
    sup, clock, motor, _ = running()
    clock.t += .1
    capture(sup, clock)
    capture(sup, clock)
    capture(sup, clock, 1, event_id="another-event")
    capture(sup, clock, 2, event_id="event-1")
    assert len(sup.captures) == 1 and not motor.stops
    capture(sup, clock, 2)
    assert len(sup.captures) == 2


@pytest.mark.parametrize("arrival", [130, 130.01])
def test_late_receipt_never_counts_even_if_disk_commit_was_before_deadline(arrival):
    sup, clock, motor, commands = running()
    clock.t = arrival
    progress(sup, clock)
    capture(sup, clock, capture_mono=129, commit_mono=129.5)
    sup.tick()
    assert sup.state == "complete" and sup.result["capture_count"] == 0
    assert len(motor.stops) == 1 and commands.get_nowait()["type"] == "end"
    capture(sup, clock, 2)
    assert not sup.captures


@pytest.mark.parametrize("overrides", [dict(commit_mono=130), dict(commit_mono=float("nan")),
    dict(commit_mono=True), dict(commit_mono=99), dict(track_id=None), dict(track_id=True),
    dict(event_id=""), dict(path=None), dict(capture_mono=float("nan")), dict(source_epoch=1)])
def test_invalid_provenance_fails_without_counting(overrides):
    sup, clock, motor, _ = running()
    clock.t += .1
    capture(sup, clock, **overrides)
    sup.tick()
    assert sup.state == "failed" and not sup.captures and motor.s["zero_confirmed"]


@pytest.mark.parametrize("fault", ["motor", "camera", "memory", "abort"])
def test_fault_at_deadline_is_not_reported_as_success(fault):
    sup, clock, motor, _ = running()
    clock.t = sup.deadline
    if fault != "camera":
        progress(sup, clock)
    if fault == "motor":
        motor.s.update(error="serial lost", fault=True)
    elif fault == "memory":
        sup.resource_error = "memory exhausted"
    elif fault == "abort":
        sup.submit(dict(action="stop", mission_id=sup.spec.mission_id))
    sup.tick()
    assert sup.state == ("cancelled" if fault == "abort" else "failed")
    assert motor.s["zero_confirmed"]


def test_deadline_during_confirmation_stops_without_waiting_for_model():
    sup, clock, motor, _ = running()
    clock.t = 129
    progress(sup, clock)
    sup.event(dict(type="stage", stage="confirming", mission_id=sup.spec.mission_id, mono=clock()))
    clock.t = 130
    progress(sup, clock)
    sup.tick()
    assert sup.state == "complete" and motor.s["zero_confirmed"]
    capture(sup, clock, commit_mono=130)
    assert not sup.captures


def test_deadline_before_start_ack_fails_and_never_begins_perception():
    class DelayedMotor(Motor):
        def start_all(self, *args):
            super().start_all(*args)
            self.s["start_status"] = "pending"
    clock, motor, commands = Clock(), DelayedMotor(), queue.Queue(16)
    sup = StandSupervisor(lambda: motor, commands, clock=clock)
    sup.memory_available_mib = 2048
    sup.event(dict(type="ready", capture_mono=clock(), source_epoch=0))
    sup.submit(dict(action="mission", appearance="find people for 1 second"))
    sup.tick()
    assert sup.state == "starting"
    clock.t += 1
    progress(sup, clock)
    sup.tick()
    assert sup.state == "failed"
    assert commands.get_nowait()["type"] == "end" and commands.empty()
    motor.s["start_status"] = "accepted"
    sup.tick()
    assert sup.state == "failed" and commands.empty()


def test_saved_image_is_available_during_collection_and_indexed_on_disk(tmp_path):
    journal = AsyncJournal(tmp_path / "missions")
    try:
        sup, clock, _, _ = running(root=tmp_path, journal=journal)
        for track in (1, 2):
            folder = tmp_path / f"event-{track}"
            folder.mkdir()
            (folder / "frame.jpg").write_bytes(f"frame-{track}".encode())
            clock.t += .1
            progress(sup, clock)
            capture(sup, clock, track, path=str(folder))
            sup.tick()
            assert sup.snapshot()["evidence_available"]
            assert sup.evidence_image(sup.spec.mission_id) == (f"frame-{track}".encode(), "image/jpeg")
        assert sup.evidence_image("unknown") is None
        clock.t = sup.deadline
        progress(sup, clock)
        sup.tick()
    finally:
        journal.close()
    rows = [json.loads(line) for line in (tmp_path / "missions" / f"{sup.spec.mission_id}.jsonl").read_text().splitlines()]
    assert [row["capture_count"] for row in rows if row["event"] == "capture_saved"] == [1, 2]
    result = next(row["result"] for row in rows if row["event"] == "mission_result")
    assert result["capture_count"] == len(result["captures"]) == 2


def test_ordinary_brief_keeps_first_match_and_default_limit():
    spec = MissionSpec.create("find people")
    assert spec.completion_mode == "first_match" and spec.duration_ms == 60000


def test_private_duration_cannot_conflict_with_timed_brief():
    with pytest.raises(ValueError, match="conflicts"):
        MissionSpec.create("find people for 30 seconds", duration_ms=10000)

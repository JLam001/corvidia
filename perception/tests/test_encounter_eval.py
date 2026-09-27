"""Scoring logic for the step 5 encounter evaluation."""

import json

from corvidia_perception.encounter_eval import score


def event(session, eid, track, result, attempt=1, first=0.0, queued=1.0, infer=1.5):
    d = session / eid
    d.mkdir(parents=True)
    (d / "event.json").write_text(json.dumps({
        "event_id": eid, "source_epoch": 0, "track_id": track, "attempt": attempt,
        "result": result, "inference_duration_s": infer,
        "timestamps": {"track_first_seen_mono": first, "queued_mono": queued}}))


def test_scores_yolo_alone_against_yolo_plus_cosmos(tmp_path):
    s = tmp_path / "run"
    s.mkdir()
    (s / "run_report.json").write_text(json.dumps({"source": "video:hall.avi"}))
    event(s, "a", 1, "confirmed")                      # p1, real
    event(s, "b", 2, "confirmed")                      # p1 again (fragmented track)
    event(s, "c", 3, "rejected")                       # poster, rejected
    event(s, "d", 4, "confirmed")                      # screen, falsely confirmed
    event(s, "e", 5, "unknown")                        # p2, first attempt unknown
    event(s, "f", 5, "rejected", attempt=2)            # p2 retry rejected -> missed by Cosmos
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps({
        "clips": {"hall": {"people": ["p1", "p2", "p3"]}},
        "tracks": {"hall/e0t1": {"truth": "person", "person": "p1"},
                   "hall/e0t2": {"truth": "partial", "person": "p1"},
                   "hall/e0t3": {"truth": "depiction"},
                   "hall/e0t4": {"truth": "depiction"},
                   "hall/e0t5": {"truth": "person", "person": "p2"}}}))
    r = score([s], labels)
    y, c = r["summary"]["yolo"], r["summary"]["cosmos"]
    assert (y["events"], y["false_events"], y["precision"], y["person_recall"]) == (5, 2, 0.6, 0.667)
    assert y["duplicate_events"] == 1
    assert (c["events"], c["false_events"], c["precision"], c["person_recall"]) == (3, 1, 0.667, 0.333)
    assert c["unknown_tracks"] == 0  # track 5 ended rejected, not all-unknown
    assert c["time_to_confirm_s"]["p50"] == 2.5
    assert r["per_clip"]["hall"]["cosmos"]["missed_people"] == ["p2", "p3"]
    assert r["unlabeled_tracks"] == []

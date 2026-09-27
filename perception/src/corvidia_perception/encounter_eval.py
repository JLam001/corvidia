"""Step 5: YOLO alone vs YOLO + Cosmos on labeled clip encounters.

1. Replay each clip in accuracy mode with Cosmos:
       uv run corvidia-run --video ~/corvidia-data/clips/poster.avi --mode every \
           --confirmer cosmos --events ~/corvidia-data/eval-runs
2. Make contact sheets and a label template (one entry per track):
       uv run corvidia-encounter-eval sheets --runs ~/corvidia-data/eval-runs/* \
           --labels ~/corvidia-data/clips/labels.json
3. Fill labels.json: per clip, the real people who appear ("people"); per track,
   "truth" (person, partial, not_person, depiction) and "person" (which one).
4. Score:
       uv run corvidia-encounter-eval score --runs ~/corvidia-data/eval-runs/* \
           --labels ~/corvidia-data/clips/labels.json

Scoring unit is the track (one encounter candidate). YOLO alone = every track
the gate sent for confirmation; YOLO + Cosmos = tracks with a confirmed result.
`person` and `partial` count as a real human present. In accuracy mode video
time pauses during inference, so time-to-confirm is (first seen -> queued, in
video time) + inference duration (wall time, `inference_wall_s`).
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

import numpy as np

REAL = {"person", "partial"}
TRUTHS = {"person", "partial", "not_person", "depiction"}


def load_run(session: Path) -> tuple[str, dict[tuple[int, int], list[dict]]]:
    report = json.loads((session / "run_report.json").read_text())
    clip = Path(report["source"].split(":", 1)[1]).stem  # clip directory or file name
    tracks: dict[tuple[int, int], list[dict]] = defaultdict(list)
    for path in sorted(session.glob("*/event.json")):
        event = json.loads(path.read_text())
        tracks[(event["source_epoch"], event["track_id"])].append(event)
    for events in tracks.values():
        events.sort(key=lambda e: e["attempt"])
    return clip, tracks


def track_key(clip: str, key: tuple[int, int]) -> str:
    return f"{clip}/e{key[0]}t{key[1]}"


def sheets(runs: list[Path], labels_path: Path) -> None:
    import cv2

    labels = json.loads(labels_path.read_text()) if labels_path.exists() else {"clips": {}, "tracks": {}}
    for session in runs:
        clip, tracks = load_run(session)
        labels["clips"].setdefault(clip, {"people": [], "notes": ""})
        tiles = []
        for key, events in sorted(tracks.items()):
            name = track_key(clip, key)
            labels["tracks"].setdefault(name, {"truth": "", "person": ""})
            crop = cv2.imread(str(session / events[0]["event_id"] / "crop.jpg"))
            if crop is None:
                continue
            h, w = crop.shape[:2]
            s = min(150 / w, 172 / h)
            crop = cv2.resize(crop, (max(1, int(w * s)), max(1, int(h * s))))
            tile = np.full((200, 150, 3), 40, np.uint8)
            tile[26:26 + crop.shape[0], :crop.shape[1]] = crop
            results = "/".join(e.get("result") or "-" for e in events)
            cv2.putText(tile, f"t{key[1]} {results}"[:22], (3, 17), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                        (0, 255, 255), 1)
            tiles.append(tile)
        cols = 8
        while tiles and len(tiles) % cols:
            tiles.append(np.full((200, 150, 3), 40, np.uint8))
        if tiles:
            sheet = np.vstack([np.hstack(tiles[i:i + cols]) for i in range(0, len(tiles), cols)])
            out = labels_path.parent / f"sheet_{clip}.jpg"
            cv2.imwrite(str(out), sheet)
            print(f"{out}  ({len(tracks)} tracks)")
    labels_path.write_text(json.dumps(labels, indent=2, sort_keys=True))
    print(f"label template: {labels_path}")


def score(runs: list[Path], labels_path: Path) -> dict:
    labels = json.loads(labels_path.read_text())
    per_clip = {}
    totals = defaultdict(float)
    delays: list[float] = []
    missing = []
    for session in runs:
        clip, tracks = load_run(session)
        people = set(labels["clips"].get(clip, {}).get("people", []))
        yolo_true = yolo_false = cos_true = cos_false = unknown = 0
        yolo_people: dict[str, int] = defaultdict(int)
        cos_people: set[str] = set()
        for key, events in tracks.items():
            lab = labels["tracks"].get(track_key(clip, key), {})
            truth = lab.get("truth", "")
            if truth not in TRUTHS:
                missing.append(track_key(clip, key))
                continue
            real = truth in REAL
            results = [e.get("result") for e in events]
            confirmed = "confirmed" in results
            if all(r == "unknown" for r in results):
                unknown += 1
            if real:
                yolo_true += 1
                if lab.get("person"):
                    yolo_people[lab["person"]] += 1
            else:
                yolo_false += 1
            if confirmed:
                if real:
                    cos_true += 1
                    if lab.get("person"):
                        cos_people.add(lab["person"])
                else:
                    cos_false += 1
                ev = next(e for e in events if e.get("result") == "confirmed")
                ts = ev["timestamps"]
                if ts.get("track_first_seen_mono") is not None:
                    infer = ev.get("inference_wall_s", ev.get("inference_duration_s")) or 0.0
                    delays.append(ts["queued_mono"] - ts["track_first_seen_mono"] + infer)
        found_yolo = people & set(yolo_people)
        found_cos = people & cos_people
        row = {
            "people": len(people),
            "tracks": len(tracks),
            "yolo": {"events": yolo_true + yolo_false, "false_events": yolo_false,
                     "precision": _ratio(yolo_true, yolo_true + yolo_false),
                     "people_found": len(found_yolo), "missed_people": sorted(people - found_yolo),
                     "duplicate_events": sum(n - 1 for n in yolo_people.values() if n > 1)},
            "cosmos": {"events": cos_true + cos_false, "false_events": cos_false,
                       "precision": _ratio(cos_true, cos_true + cos_false),
                       "people_found": len(found_cos), "missed_people": sorted(people - found_cos),
                       "unknown_tracks": unknown},
        }
        per_clip[clip] = row
        for variant in ("yolo", "cosmos"):
            totals[f"{variant}_true"] += row[variant]["events"] - row[variant]["false_events"]
            totals[f"{variant}_false"] += row[variant]["false_events"]
            totals[f"{variant}_found"] += row[variant]["people_found"]
        totals["people"] += len(people)
        totals["yolo_duplicates"] += row["yolo"]["duplicate_events"]
        totals["unknown"] += unknown
    summary = {
        variant: {
            "events": int(totals[f"{variant}_true"] + totals[f"{variant}_false"]),
            "false_events": int(totals[f"{variant}_false"]),
            "precision": _ratio(totals[f"{variant}_true"], totals[f"{variant}_true"] + totals[f"{variant}_false"]),
            "person_recall": _ratio(totals[f"{variant}_found"], totals["people"]),
        }
        for variant in ("yolo", "cosmos")
    }
    summary["people"] = int(totals["people"])
    summary["yolo"]["duplicate_events"] = int(totals["yolo_duplicates"])
    summary["cosmos"]["unknown_tracks"] = int(totals["unknown"])
    summary["cosmos"]["time_to_confirm_s"] = {
        f"p{q}": round(float(np.percentile(delays, q)), 2) for q in (50, 95)} if delays else None
    return {"summary": summary, "per_clip": per_clip, "unlabeled_tracks": missing}


def _ratio(a: float, b: float) -> float | None:
    return round(a / b, 3) if b else None


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["sheets", "score"])
    ap.add_argument("--runs", type=Path, nargs="+", required=True, help="session directories")
    ap.add_argument("--labels", type=Path, required=True)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)
    if args.command == "sheets":
        sheets(args.runs, args.labels)
        return
    result = score(args.runs, args.labels)
    print(json.dumps(result, indent=2))
    if args.out:
        args.out.write_text(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

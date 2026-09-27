"""Fill corvidia-encounter-eval labels.json from MOT ground truth instead of by hand.

    autolabel.py --runs <session dir>... --clips <clips root> --labels labels.json
                 [--iou 0.5] [--partial-iou 0.2] [--min-frames 15] [--min-height 48]
                 [--min-width 24] [--min-visibility 0.5] [--edge-px 2] [--depiction]

For each run session (from `corvidia-run --video <clips root>/<clip> --mode every`), the first
event of every track is matched at its frame_id against <clip>/gt.txt by IoU:
    IoU >= --iou             truth = person,  person = "id<gt id>"
    IoU >= --partial-iou     truth = partial, person = "id<gt id>"
    overlaps a depiction row (MOT class 8 distractor, 12 reflection)   truth = depiction
    otherwise                truth = not_person
Tracks are keyed exactly as encounter_eval does: <clip>/e<epoch>t<track>.

Left UNLABELED (the scorer excludes them and lists them in unlabeled_tracks) instead of guessed:
    - tracks overlapping only an ignore region (gt conf == 0). MOT17 flags every class 2
      "person on vehicle" and 7 "static person" row that way, so those never count as people.
    - unmatched tracks touching the frame edge (within --edge-px): MOT annotators skip people
      mostly outside the frame; on MOT17-05 every such track was a real person entering/leaving.
    - tracks whose frame has no gt rows at all (sparse annotations).
Review those on the contact sheet (`corvidia-encounter-eval sheets`) and label them by hand.

A clip's "people" are the gt identities with at least --min-frames frames in which they are
visible (MOT visibility >= --min-visibility) at box height >= --min-height and width >=
--min-width (the crop gate's minimums), so people the gate could never admit are not "missed".

Entries written here carry "source": "auto". Entries without that tag (hand labels) are never
overwritten; a clip's previous auto entries are replaced on every run.
--depiction labels every track as a depiction and lists no people (spoof / screen clips).
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

PERSON_CLASSES = {1, 2, 7}
DEPICTION_CLASSES = {8, 12}
Row = tuple[int, list[float], float]  # gt id, [x1, y1, x2, y2], visibility


def iou(a: list[float], b: list[float]) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def load_gt(path: Path) -> tuple[dict[int, list[Row]], dict[int, list[Row]], dict[int, list[Row]]]:
    """Three frame -> rows maps: real people, depictions, ignore regions."""
    people: dict[int, list[Row]] = defaultdict(list)
    depictions: dict[int, list[Row]] = defaultdict(list)
    ignore: dict[int, list[Row]] = defaultdict(list)
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        p = line.split(",")
        conf = 0 if len(p) > 6 and float(p[6]) == 0 else 1
        cls = int(float(p[7])) if len(p) > 7 else 1
        if cls <= 0:
            cls = 1
        vis = float(p[8]) if len(p) > 8 else 1.0
        if vis < 0:
            vis = 1.0
        x, y, w, h = (float(v) for v in p[2:6])
        row: Row = (int(p[1]), [x, y, x + w, y + h], vis)
        fr = int(p[0])
        if cls in DEPICTION_CLASSES:
            depictions[fr].append(row)
        elif cls in PERSON_CLASSES and conf == 0:
            ignore[fr].append(row)
        elif cls in PERSON_CLASSES:
            people[fr].append(row)
    return people, depictions, ignore


def best_match(box: list[float], rows: list[Row]) -> tuple[int | None, float]:
    best_id, best = None, 0.0
    for gid, gbox, _ in rows:
        v = iou(box, gbox)
        if v > best:
            best_id, best = gid, v
    return best_id, best


def _touches_edge(box: list[float], w: int, h: int, margin: float) -> bool:
    return box[0] <= margin or box[1] <= margin or box[2] >= w - margin or box[3] >= h - margin


def people_in_clip(gt: dict[int, list[Row]], min_frames: int, min_height: float, min_width: float,
                   min_vis: float) -> list[str]:
    frames_per_id: dict[int, int] = defaultdict(int)
    for rows in gt.values():
        for gid, box, vis in rows:
            if vis >= min_vis and box[3] - box[1] >= min_height and box[2] - box[0] >= min_width:
                frames_per_id[gid] += 1
    return sorted(f"id{gid}" for gid, n in frames_per_id.items() if n >= min_frames)


def label_session(session: Path, clips_root: Path, args) -> tuple[str, dict, dict, dict]:
    report = json.loads((session / "run_report.json").read_text())
    clip = Path(report["source"].split(":", 1)[1]).stem  # same derivation as encounter_eval
    tracks: dict[tuple[int, int], list[dict]] = defaultdict(list)
    for path in sorted(session.glob("*/event.json")):
        e = json.loads(path.read_text())
        tracks[(e["source_epoch"], e["track_id"])].append(e)
    stats = {"clip": clip, "tracks": len(tracks), "person": 0, "partial": 0, "not_person": 0,
             "depiction": 0, "unlabeled_ignore_region": 0, "unlabeled_edge": 0,
             "unlabeled_no_gt_frame": 0, "unlabeled_looped": 0}
    labels: dict[str, dict] = {}
    if args.depiction:
        for key in tracks:
            labels[f"{clip}/e{key[0]}t{key[1]}"] = {"truth": "depiction", "person": "", "source": "auto"}
            stats["depiction"] += 1
        return clip, {"people": [], "notes": "auto: all depiction", "source": "auto"}, labels, stats
    gt_path = clips_root / clip / "gt.txt"
    if not gt_path.exists():
        raise SystemExit(f"no gt.txt for clip {clip} under {clips_root} (use --depiction for spoof clips)")
    gt, depictions, ignore = load_gt(gt_path)
    for key, events in tracks.items():
        events.sort(key=lambda e: e["attempt"])
        e = events[0]
        name = f"{clip}/e{key[0]}t{key[1]}"
        if e["source_epoch"] != 0:
            stats["unlabeled_looped"] += 1
            continue  # looped replay: frame ids no longer map onto gt
        fid, box = e["frame_id"], e["bbox"]
        best_id, best = best_match(box, gt.get(fid, []))
        if best_id is not None and best >= args.iou:
            truth, person = "person", f"id{best_id}"
        elif best_id is not None and best >= args.partial_iou:
            truth, person = "partial", f"id{best_id}"
        else:
            _, dep = best_match(box, depictions.get(fid, []))
            _, ign = best_match(box, ignore.get(fid, []))
            if dep > 0 and dep >= args.partial_iou:
                truth, person, best = "depiction", "", dep
            elif ign > 0 and ign >= args.partial_iou:
                stats["unlabeled_ignore_region"] += 1
                continue
            elif not (gt.get(fid) or depictions.get(fid) or ignore.get(fid)):
                stats["unlabeled_no_gt_frame"] += 1
                continue
            elif _touches_edge(box, e["image"]["width"], e["image"]["height"], args.edge_px):
                stats["unlabeled_edge"] += 1
                continue
            else:
                truth, person = "not_person", ""
        stats[truth] += 1
        labels[name] = {"truth": truth, "person": person, "iou": round(best, 3), "source": "auto"}
    people = people_in_clip(gt, args.min_frames, args.min_height, args.min_width, args.min_visibility)
    note = (f"auto from gt.txt: iou>={args.iou}, partial>={args.partial_iou}; people = ids visible "
            f">={args.min_frames} frames at vis>={args.min_visibility}, h>={args.min_height}px, "
            f"w>={args.min_width}px")
    return clip, {"people": people, "notes": note, "source": "auto"}, labels, stats


def merge(out: dict, clip: str, clip_entry: dict, labels: dict, stats: dict) -> None:
    """Replace this clip's auto entries; never touch hand-written ones."""
    for k in [k for k, v in out["tracks"].items() if k.startswith(clip + "/") and v.get("source") == "auto"]:
        del out["tracks"][k]
    kept = 0
    for k, v in labels.items():
        if k in out["tracks"]:  # a hand label survived the purge above
            kept += 1
            continue
        out["tracks"][k] = v
    if out["clips"].get(clip, {}).get("source", "auto") == "auto":
        out["clips"][clip] = clip_entry
    else:
        kept += 1
    stats["hand_labels_kept"] = kept


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=Path, nargs="+", required=True)
    ap.add_argument("--clips", type=Path, required=True, help="clips root (contains <clip>/gt.txt)")
    ap.add_argument("--labels", type=Path, required=True)
    ap.add_argument("--iou", type=float, default=0.5)
    ap.add_argument("--partial-iou", type=float, default=0.2)
    ap.add_argument("--min-frames", type=int, default=15)
    ap.add_argument("--min-height", type=float, default=48.0)
    ap.add_argument("--min-width", type=float, default=24.0)
    ap.add_argument("--min-visibility", type=float, default=0.5)
    ap.add_argument("--edge-px", type=float, default=2.0)
    ap.add_argument("--depiction", action="store_true")
    args = ap.parse_args(argv)
    if not 0 < args.partial_iou <= args.iou <= 1:
        raise SystemExit("need 0 < --partial-iou <= --iou <= 1")
    out = json.loads(args.labels.read_text()) if args.labels.exists() else {"clips": {}, "tracks": {}}
    for session in args.runs:
        clip, clip_entry, labels, stats = label_session(session, args.clips, args)
        merge(out, clip, clip_entry, labels, stats)
        print(json.dumps(stats))
    args.labels.parent.mkdir(parents=True, exist_ok=True)
    args.labels.write_text(json.dumps(out, indent=2, sort_keys=True))
    print(f"labels: {args.labels}")


if __name__ == "__main__":
    main()

"""Assemble a starter crop set for evaluating the confirmer.

Writes <out>/images/*.jpg and <out>/labels.jsonl with one record per image:
    {"file": "images/x.jpg", "label": ..., "source": ..., "note": ...}

Labels:
    person       a real person is clearly visible (full or most of the body)
    partial      only part of a real person (hand, arm, top of head); "yes" or
                 "uncertain" are both acceptable
    not_person   no person (background, objects)
    depiction    a picture, animation, statue, mannequin, or screen image
    unlabeled    needs a human label before use

This is a starter set: it has no real screens, printed photos, mannequins, or
statues yet, so it cannot measure the hardest false positives.

    uv run python tools/build_starter_eval.py --out ~/corvidia-data/eval/starter \
        --person-events /tmp/ev-every/<session> \
        --unlabeled-events ~/corvidia-data/events/<live session>
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

import cv2
import numpy as np

VTEST = "/usr/share/opencv4/samples/data/vtest.avi"
MEGAMIND = "/usr/share/opencv4/samples/data/Megamind.avi"


def iou(a, b) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


class Writer:
    def __init__(self, out: Path) -> None:
        self.out = out
        (out / "images").mkdir(parents=True, exist_ok=True)
        self.records: list[dict] = []

    def add(self, image: np.ndarray | bytes, name: str, label: str, source: str, note: str = "") -> None:
        path = self.out / "images" / f"{name}.jpg"
        if isinstance(image, bytes):
            path.write_bytes(image)
        else:
            cv2.imwrite(str(path), image, [cv2.IMWRITE_JPEG_QUALITY, 95])
        self.records.append({"file": f"images/{path.name}", "label": label, "source": source,
                             "note": note})

    def save(self) -> None:
        with open(self.out / "labels.jsonl", "w") as f:
            for r in self.records:
                f.write(json.dumps(r) + "\n")


def event_crops(session: Path):
    for event_json in sorted(session.glob("*/event.json")):
        crop = event_json.parent / "crop.jpg"
        if crop.exists():
            yield event_json.parent.name, crop.read_bytes()


def person_boxes(model, image: np.ndarray, conf: float = 0.1) -> list[list[float]]:
    r = model.predict(image, device=0, classes=[0], conf=conf, verbose=False)[0]
    return r.boxes.xyxy.cpu().numpy().tolist()


def background_crops(model, video: str, w: Writer, n: int, rng: random.Random, tag: str) -> None:
    """Person-shaped crops that do not overlap any detection, even a weak one."""
    cap = cv2.VideoCapture(video)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    made = 0
    for idx in sorted(rng.sample(range(total), min(total, n * 3))):
        if made >= n:
            break
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = cap.read()
        if not ok:
            continue
        h, wd = frame.shape[:2]
        boxes = person_boxes(model, frame)
        for _ in range(20):
            bw = rng.randint(wd // 12, wd // 5)
            bh = int(bw * rng.uniform(1.6, 2.6))
            if bh >= h:
                continue
            x, y = rng.randint(0, wd - bw), rng.randint(0, h - bh)
            box = [x, y, x + bw, y + bh]
            if all(iou(box, b) < 0.02 for b in boxes):
                w.add(frame[y:y + bh, x:x + bw], f"{tag}_bg_{idx}", "not_person", video,
                      "background, no detection overlap")
                made += 1
                break
    cap.release()


def detector_crops(model, video: str, w: Writer, n: int, label: str, tag: str) -> None:
    """YOLO person detections in footage with no real people (animation)."""
    cap = cv2.VideoCapture(video)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    step = max(1, total // (n * 2))
    made = 0
    for idx in range(0, total, step):
        if made >= n:
            break
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = cap.read()
        if not ok:
            continue
        h, wd = frame.shape[:2]
        for x1, y1, x2, y2 in person_boxes(model, frame, conf=0.25)[:1]:
            pw, ph = (x2 - x1) * 0.2, (y2 - y1) * 0.2
            xa, ya = max(0, int(x1 - pw)), max(0, int(y1 - ph))
            xb, yb = min(wd, int(x2 + pw)), min(h, int(y2 + ph))
            w.add(frame[ya:yb, xa:xb], f"{tag}_{idx}", label, video, "YOLO person detection")
            made += 1
    cap.release()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--person-events", type=Path, action="append", default=[],
                    help="event session whose crops are all clearly people")
    ap.add_argument("--unlabeled-events", type=Path, action="append", default=[],
                    help="event session whose crops need a human label")
    ap.add_argument("--model", default=str(Path("~/models/yolo11n.pt").expanduser()))
    ap.add_argument("--negatives", type=int, default=40)
    ap.add_argument("--depictions", type=int, default=15)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    from ultralytics import YOLO

    rng = random.Random(args.seed)
    model = YOLO(args.model)
    w = Writer(args.out)
    for session in args.person_events:
        for name, data in event_crops(session):
            w.add(data, f"vtest_{name[:8]}", "person", str(session), "vtest event crop")
    for session in args.unlabeled_events:
        for name, data in event_crops(session):
            w.add(data, f"live_{name[:8]}", "unlabeled", str(session), "live camera event crop")
    background_crops(model, VTEST, w, args.negatives // 2, rng, "vtest")
    for session in args.unlabeled_events:
        frames = sorted(session.glob("*/frame.jpg"))
        for i, frame_path in enumerate(rng.sample(frames, min(len(frames), args.negatives // 2))):
            event = json.loads((frame_path.parent / "event.json").read_text())
            frame = cv2.imread(str(frame_path))
            h, wd = frame.shape[:2]
            boxes = person_boxes(model, frame)
            for _ in range(30):
                bw = rng.randint(wd // 10, wd // 4)
                bh = min(h - 1, int(bw * rng.uniform(1.2, 2.2)))
                x, y = rng.randint(0, wd - bw), rng.randint(0, h - bh)
                box = [x, y, x + bw, y + bh]
                if all(iou(box, b) < 0.02 for b in boxes + [event["bbox"]]):
                    w.add(frame[y:y + bh, x:x + bw], f"live_bg_{i}", "unlabeled", str(frame_path),
                          "live background candidate")
                    break
    detector_crops(model, MEGAMIND, w, args.depictions, "depiction", "megamind")
    w.save()
    counts: dict[str, int] = {}
    for r in w.records:
        counts[r["label"]] = counts.get(r["label"], 0) + 1
    print(args.out, counts)


if __name__ == "__main__":
    main()

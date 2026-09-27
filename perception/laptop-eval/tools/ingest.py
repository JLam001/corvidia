"""Convert public datasets into corvidia-record clip directories with MOT ground truth.

Clip layout (readable by `corvidia-run --video <clip dir>` since commit 8050566):
    <clip>/000000.jpg ...   frames, 0-based (event.json frame_id = index + 1, epoch 0)
    <clip>/frames.jsonl     {"index","frame_id","epoch","capture_ts","capture_quality","wall"}
    <clip>/gt.txt           MOT format: frame,id,x,y,w,h,conf,class,visibility (1-based frame, px)
    <clip>/clip.json        provenance: source, fps, size, frames, scale applied

    ingest.py mot      <seq dir with img1/, seqinfo.ini, gt/gt.txt>   --out clips/<name>
    ingest.py fiftyone <video.mp4> --frames frames.json --samples samples.json --out clips/<name>
    ingest.py video    <video.mp4> [--gt gt.txt]                       --out clips/<name>

Options: --max-side N   downscale so the longest side is N px (gt scaled to match)
         --limit N      keep only the first N frames
         --quality Q    JPEG quality (default 95)

Frames are re-encoded to JPEG once. Keep --max-side unset unless you want to emulate a
specific camera output size: YOLO letterboxes every frame to 640 px, depth resizes its input
to 518x294, and the confirmer crop is capped at 448 px on its longest side regardless of the
source, so a moderate downscale changes nothing downstream and a downscale below a 640 px long
side costs detector accuracy and pushes small people under the 48 px crop gate.
"""

from __future__ import annotations

import argparse
import configparser
import json
import sys
from pathlib import Path

import cv2
import numpy as np


def _scale_for(w: int, h: int, max_side: int | None) -> float:
    if not max_side or max(w, h) <= max_side:
        return 1.0
    return max_side / max(w, h)


def _resize(img: np.ndarray, scale: float) -> np.ndarray:
    if scale == 1.0:
        return img
    h, w = img.shape[:2]
    return cv2.resize(img, (max(1, round(w * scale)), max(1, round(h * scale))), interpolation=cv2.INTER_AREA)


class ClipWriter:
    def __init__(self, out: Path, fps: float, quality: int, scale: float, source: str) -> None:
        if "." in out.name:
            raise SystemExit("clip names must not contain '.': corvidia-encounter-eval keys clips by "
                             "Path(name).stem, so 'mot17-05.380' would collide with 'mot17-05'")
        if out.exists() and any(out.iterdir()):
            raise SystemExit(f"{out} exists and is not empty; choose another --out")
        out.mkdir(parents=True, exist_ok=True)
        self.out, self.fps, self.scale, self.source = out, fps, scale, source
        self.params = [cv2.IMWRITE_JPEG_QUALITY, quality]
        self.n = 0
        self.size: tuple[int, int] | None = None
        self.gt_stats: dict | None = None
        self._log = open(out / "frames.jsonl", "w")

    def add(self, img: np.ndarray) -> None:
        img = _resize(img, self.scale)
        if self.size is None:
            self.size = (img.shape[1], img.shape[0])
        ok, buf = cv2.imencode(".jpg", img, self.params)
        if not ok:
            raise RuntimeError(f"JPEG encode failed at frame {self.n}")
        (self.out / f"{self.n:06d}.jpg").write_bytes(buf.tobytes())
        self._log.write(json.dumps({"index": self.n, "frame_id": self.n + 1, "epoch": 0,
                                    "capture_ts": self.n / self.fps,
                                    "capture_quality": "dataset_fps", "wall": None}) + "\n")
        self.n += 1

    def write_gt(self, rows: list[tuple]) -> None:
        # rows: (frame, id, x, y, w, h, conf, cls, vis) in source pixels; scaled here.
        # gt frames must be 1-based like event.json frame_id; a 0-based file is refused.
        if not rows:
            raise SystemExit("gt has no rows")
        lo, hi = min(r[0] for r in rows), max(r[0] for r in rows)
        if lo < 1:
            raise SystemExit(f"gt frame numbers start at {lo}; they must be 1-based (renumber the file)")
        if lo != 1:
            print(f"warning: gt starts at frame {lo}, not 1", file=sys.stderr)
        if hi > self.n:
            print(f"warning: gt covers frames up to {hi} but the clip has {self.n} frames "
                  f"(--limit, or gt for a different video?); rows beyond {self.n} dropped", file=sys.stderr)
        kept = dropped = 0
        mix: dict[str, int] = {}
        with open(self.out / "gt.txt", "w") as f:
            for fr, tid, x, y, w, h, conf, cls, vis in rows:
                if fr > self.n:
                    dropped += 1
                    continue
                s = self.scale
                f.write(f"{fr},{tid},{x * s:.2f},{y * s:.2f},{w * s:.2f},{h * s:.2f},{conf},{cls},{vis}\n")
                kept += 1
                mix[f"class{cls}_conf{conf}"] = mix.get(f"class{cls}_conf{conf}", 0) + 1
        self.gt_stats = {"rows_kept": kept, "rows_dropped": dropped, "frame_range": [lo, min(hi, self.n)],
                         "identities": len({r[1] for r in rows if r[0] <= self.n}), "class_conf_mix": mix}

    def close(self) -> None:
        self._log.close()
        meta = {"source": self.source, "fps": self.fps, "frames": self.n,
                "width": self.size[0] if self.size else None, "height": self.size[1] if self.size else None,
                "scale": self.scale, "gt": "gt.txt" if (self.out / "gt.txt").exists() else None,
                "gt_stats": self.gt_stats}
        (self.out / "clip.json").write_text(json.dumps(meta, indent=2))
        print(json.dumps(meta, indent=2))


def read_mot_gt(path: Path) -> list[tuple]:
    rows = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        p = line.split(",")
        fr, tid = int(p[0]), int(p[1])
        x, y, w, h = (float(v) for v in p[2:6])
        # MOT semantics: conf 0 = ignore region, anything else = considered (some exporters
        # write detector scores here, so do not truncate). Class <= 0 (MOT15 "-1") means
        # unlabeled class: treat as pedestrian. Visibility -1 (MOT15) means unknown: treat as 1.
        conf = 0 if len(p) > 6 and float(p[6]) == 0 else 1
        cls = int(float(p[7])) if len(p) > 7 else 1
        if cls <= 0:
            cls = 1
        vis = float(p[8]) if len(p) > 8 else 1.0
        if vis < 0:
            vis = 1.0
        rows.append((fr, tid, x, y, w, h, conf, cls, vis))
    return rows


def cmd_mot(args: argparse.Namespace) -> None:
    seq = Path(args.src)
    ini = configparser.ConfigParser()
    ini.read(seq / "seqinfo.ini")
    s = ini["Sequence"]
    fps = float(s["frameRate"])
    img_dir = seq / s.get("imDir", "img1")
    ext = s.get("imExt", ".jpg")
    w, h = int(s["imWidth"]), int(s["imHeight"])
    n_total = int(s["seqLength"])
    scale = _scale_for(w, h, args.max_side)
    cw = ClipWriter(Path(args.out), fps, args.quality, scale, f"mot:{seq.name}")
    limit = min(n_total, args.limit or n_total)
    for i in range(1, limit + 1):
        img = cv2.imread(str(img_dir / f"{i:06d}{ext}"))
        if img is None:
            raise SystemExit(f"missing frame {i} in {img_dir}")
        cw.add(img)
    gt = seq / "gt" / "gt.txt"
    if gt.exists():
        cw.write_gt(read_mot_gt(gt))
    cw.close()


def _decode_video(cw: ClipWriter, video: Path, limit: int | None) -> None:
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise SystemExit(f"cannot open {video}")
    while True:
        ok, img = cap.read()
        if not ok or (limit and cw.n >= limit):
            break
        cw.add(img)
    cap.release()


def cmd_video(args: argparse.Namespace) -> None:
    video = Path(args.src)
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise SystemExit(f"cannot open {video}")
    fps = float(cap.get(cv2.CAP_PROP_FPS)) or 30.0
    w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    cw = ClipWriter(Path(args.out), fps, args.quality, _scale_for(w, h, args.max_side), f"video:{video.name}")
    _decode_video(cw, video, args.limit)
    if args.gt:
        cw.write_gt(read_mot_gt(Path(args.gt)))
    cw.close()


def cmd_fiftyone(args: argparse.Namespace) -> None:
    """Voxel51 video export (samples.json + frames.json), e.g. Voxel51/DanceTrack."""
    video = Path(args.src)
    samples = json.loads(Path(args.samples).read_text())["samples"]
    sample = next((s for s in samples if Path(s["filepath"]).name == video.name), None)
    if sample is None:
        raise SystemExit(f"{video.name} not found in {args.samples}")
    oid = sample["_id"]["$oid"]
    meta = sample["metadata"]
    fps = float(meta["frame_rate"])
    w, h = int(meta["frame_width"]), int(meta["frame_height"])
    frames = [f for f in json.loads(Path(args.frames).read_text())["frames"] if f["_sample_id"]["$oid"] == oid]
    rows = []
    for f in frames:
        for d in f.get("gt", {}).get("detections", []):
            if d.get("label", "person") != "person":
                continue
            rx, ry, rw, rh = d["bounding_box"]  # relative [x, y, w, h]
            if d.get("index") is None:
                raise SystemExit(f"frame {f['frame_number']}: detection without a track 'index'; "
                                 "this export has no identities, so it cannot score encounters")
            rows.append((int(f["frame_number"]), int(d["index"]) + 1, rx * w, ry * h, rw * w, rh * h,
                         1, 1, float(d.get("visibility", 1.0))))
    cw = ClipWriter(Path(args.out), fps, args.quality, _scale_for(w, h, args.max_side), f"fiftyone:{video.name}")
    _decode_video(cw, video, args.limit)
    cw.write_gt(rows)
    cw.close()
    print(f"gt: {len(frames)} annotated frames, {len(rows)} boxes, "
          f"{len({r[1] for r in rows})} identities", file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("mot", cmd_mot), ("video", cmd_video), ("fiftyone", cmd_fiftyone)):
        p = sub.add_parser(name)
        p.add_argument("src")
        p.add_argument("--out", required=True)
        p.add_argument("--max-side", type=int, default=None)
        p.add_argument("--limit", type=int, default=None)
        p.add_argument("--quality", type=int, default=95)
        if name == "video":
            p.add_argument("--gt", default=None, help="MOT gt.txt for this video (optional)")
        if name == "fiftyone":
            p.add_argument("--frames", required=True)
            p.add_argument("--samples", required=True)
        p.set_defaults(fn=fn)
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()

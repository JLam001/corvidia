"""Record camera clips for evaluation (replayable with corvidia-run --video).

    uv run corvidia-record --name poster --seconds 60
    # preview while recording: http://192.168.2.2:8080/

Writes <out>/<name>/000000.jpg, ... and <out>/<name>/frames.jsonl (each frame's
index and capture timestamp). The Orin Nano has no hardware video encoder and
one CPU core cannot JPEG-encode 1080p at 30 fps, so frames are encoded on a
small thread pool. Uses the same camera settings as corvidia-run so replays
match live input; replay with `corvidia-run --video <out>/<name>`.
"""

from __future__ import annotations

import argparse
import json
import signal
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2

from .preview import PreviewServer, PreviewState
from .sources import CameraSource, argus_pipeline


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", required=True, help="clip name, e.g. people-near, poster, empty")
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--out", type=Path, default=Path("~/corvidia-data/clips").expanduser())
    ap.add_argument("--sensor-id", type=int, default=0)
    ap.add_argument("--sensor-mode", type=int, choices=[0, 1], default=1)
    ap.add_argument("--camera-size", default="1280x720")
    ap.add_argument("--camera-fps", type=int, default=30)
    ap.add_argument("--flip", type=int, default=0)
    ap.add_argument("--quality", type=int, default=90, help="JPEG quality per frame")
    ap.add_argument("--workers", type=int, default=4, help="JPEG encoder threads")
    ap.add_argument("--preview-port", type=int, default=8080)
    args = ap.parse_args(argv)

    clip_dir = args.out / args.name
    if clip_dir.exists():
        raise SystemExit(f"{clip_dir} exists; choose another --name")
    clip_dir.mkdir(parents=True)
    w, h = (int(x) for x in args.camera_size.lower().split("x"))
    source = CameraSource(argus_pipeline(args.sensor_id, args.sensor_mode, (w, h), args.camera_fps,
                                         args.flip), fps=args.camera_fps)
    if not source.connected.wait(15.0):
        source.close()
        raise SystemExit("camera did not start within 15 s")
    pool = ThreadPoolExecutor(max_workers=args.workers)
    pending: deque = deque()
    params = [cv2.IMWRITE_JPEG_QUALITY, args.quality]

    def write(path: Path, image) -> None:
        ok, buf = cv2.imencode(".jpg", image, params)
        if not ok:
            raise RuntimeError(f"JPEG encode failed for {path}")
        path.write_bytes(buf.tobytes())

    preview = PreviewServer(args.preview_port, fps=8) if args.preview_port else None
    stop = threading.Event()
    prev = signal.signal(signal.SIGINT, lambda *_: stop.set())
    n = 0
    last_id = None
    gaps = 0
    backlog_drops = 0
    started = time.monotonic()
    print(f"recording {clip_dir} for {args.seconds:.0f} s (Ctrl-C to stop early)", flush=True)
    try:
        with open(clip_dir / "frames.jsonl", "w") as log:
            while not stop.is_set() and time.monotonic() - started < args.seconds:
                frame = source.read(timeout=1.0)
                if frame is None:
                    continue
                if last_id is not None and frame.info.frame_id != last_id + 1:
                    gaps += frame.info.frame_id - last_id - 1
                last_id = frame.info.frame_id
                while pending and pending[0].done():
                    pending.popleft().result()
                if len(pending) >= args.workers * 3:
                    backlog_drops += 1  # encoder behind: drop rather than grow memory
                    continue
                pending.append(pool.submit(write, clip_dir / f"{n:06d}.jpg", frame.image))
                log.write(json.dumps({"index": n, "frame_id": frame.info.frame_id,
                                      "epoch": frame.info.source_epoch,
                                      "capture_ts": frame.info.capture_ts,
                                      "capture_quality": frame.info.capture_quality,
                                      "wall": frame.info.arrival_wall}) + "\n")
                n += 1
                if preview is not None:
                    elapsed = time.monotonic() - started
                    preview.update(PreviewState(frame, [], {}, [
                        f"REC {args.name}  {elapsed:5.1f} / {args.seconds:.0f} s  frames {n}"
                        f"  skipped {gaps + backlog_drops}"]))
    finally:
        signal.signal(signal.SIGINT, prev)
        source.close()
        for fut in pending:
            fut.result()
        pool.shutdown()
        if preview is not None:
            preview.close()
    elapsed = time.monotonic() - started
    size = sum(p.stat().st_size for p in clip_dir.glob("*.jpg"))
    print(json.dumps({"clip": str(clip_dir), "frames": n, "seconds": round(elapsed, 1),
                      "fps": round(n / elapsed, 1) if elapsed else None,
                      "skipped_frames": gaps + backlog_drops, "megabytes": round(size / 1e6, 1)},
                     indent=2))


if __name__ == "__main__":
    main()

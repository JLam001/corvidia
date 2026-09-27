"""Live distance readout for calibrating depth against a tape measure.

    uv run corvidia-depth-check            # preview at http://192.168.2.2:8080/

Stand (alone) at marked distances, e.g. 1, 2, 3, 4 m from the lens. The largest
detected person's estimated distance is overlaid and printed about twice a
second, with a rolling median. The ratio true / estimated at each mark gives
`depth.scale` (use the median ratio if it is roughly constant).
"""

from __future__ import annotations

import argparse
import signal
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np

from .config import DepthConfig, DetectorConfig, PipelineConfig, load_config
from .depth import DepthEstimator
from .detector import make_detector
from .preview import PreviewServer, PreviewState
from .records import BBox, Detection
from .sources import CameraSource, argus_pipeline


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path)
    ap.add_argument("--depth-model")
    ap.add_argument("--camera-size", default="1280x720")
    ap.add_argument("--sensor-mode", type=int, default=1)
    ap.add_argument("--interval", type=float, default=0.5)
    ap.add_argument("--preview-port", type=int, default=8080)
    args = ap.parse_args(argv)
    cfg = load_config(args.config) if args.config else PipelineConfig()
    depth_cfg = DepthConfig(model=args.depth_model or cfg.depth.model, scale=1.0)
    detector = make_detector(DetectorConfig(model=cfg.detector.model))
    depth = DepthEstimator(depth_cfg)
    w, h = (int(x) for x in args.camera_size.split("x"))
    source = CameraSource(argus_pipeline(0, args.sensor_mode, (w, h), 30))
    if not source.connected.wait(15.0):
        raise SystemExit("camera did not start")
    preview = PreviewServer(args.preview_port, fps=8)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    recent: deque[float] = deque(maxlen=9)
    last = 0.0
    print("stand at marked distances; Ctrl-C to stop", flush=True)
    try:
        while not stop.is_set():
            frame = source.read(timeout=1.0)
            if frame is None:
                continue
            xyxy, conf = detector.detect(frame.image)
            keep = conf >= 0.4
            xyxy, conf = xyxy[keep], conf[keep]
            dets, lines = [], []
            if len(xyxy):
                i = int(np.argmax((xyxy[:, 2] - xyxy[:, 0]) * (xyxy[:, 3] - xyxy[:, 1])))
                box = BBox(*map(float, xyxy[i]))
                dets = [Detection(0, box, float(conf[i]))]
                if time.monotonic() - last >= args.interval:
                    last = time.monotonic()
                    m = depth.measure(frame.image, box)
                    if m.get("distance_m") is not None:
                        recent.append(m["distance_m"])
                        med = float(np.median(recent))
                        cut = box.y2 >= h - 2 or box.y1 <= 2
                        print(f"estimated {m['distance_m']:5.2f} m (IQR {m['roi_p25_m']:.2f}-"
                              f"{m['roi_p75_m']:.2f})  rolling median {med:5.2f} m  box height "
                              f"{box.height:.0f}px{'  [cut off at frame edge]' if cut else ''}", flush=True)
            if recent:
                lines.append(f"distance (uncalibrated): {recent[-1]:.2f} m   median {np.median(recent):.2f} m")
            lines.append(f"depth {depth.last_ms:.0f} ms  model {Path(depth.model_path).name}")
            preview.update(PreviewState(frame, dets, {0: f"person {recent[-1]:.1f}m" if recent else "person"},
                                        lines))
    finally:
        source.close()
        preview.close()
        depth.close()


if __name__ == "__main__":
    main()

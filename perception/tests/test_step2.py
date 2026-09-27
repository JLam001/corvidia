"""Video replay, tracker, camera source, preview, and end-to-end detector runs."""

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from corvidia_perception.config import TrackerConfig  # noqa: E402
from corvidia_perception.preview import PreviewServer, PreviewState  # noqa: E402
from corvidia_perception.records import BBox, Detection, Frame, FrameInfo  # noqa: E402
from corvidia_perception.run import ReplayClock  # noqa: E402
from corvidia_perception.sources import CameraSource, VideoFileSource  # noqa: E402

VTEST = Path("/usr/share/opencv4/samples/data/vtest.avi")
MODEL = Path("~/models/yolo11n.pt").expanduser()
ENGINE = Path("~/models/yolo11n.engine").expanduser()


def make_video(path: Path, n: int = 30, fps: float = 10.0) -> Path:
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, (64, 48))
    for i in range(n):
        writer.write(np.full((48, 64, 3), i * 8 % 256, dtype=np.uint8))
    writer.release()
    return path


# -- sources ---------------------------------------------------------------------------

def test_every_frame_replay_yields_all_frames_with_video_pts(tmp_path):
    src = VideoFileSource(make_video(tmp_path / "v.avi"))
    frames = []
    while (f := src.read()) is not None:
        frames.append(f)
    src.close()
    assert src.finished
    assert [f.info.frame_id for f in frames] == list(range(1, 31))
    assert frames[10].info.pts_s == pytest.approx(1.0)
    assert frames[0].info.capture_clock.value == "video_pts"


def test_realtime_replay_drops_frames_for_a_slow_consumer(tmp_path):
    src = VideoFileSource(make_video(tmp_path / "v.avi", n=60), realtime=True, speed=10.0)
    ids = []
    while True:
        f = src.read(timeout=1.0)
        if f is None:
            break
        ids.append(f.info.frame_id)
        time.sleep(0.05)  # slower than the 100 fps source
    src.close()
    assert ids == sorted(ids)
    assert len(ids) < 60
    assert src.health.count("frames_superseded") > 0


def test_looping_starts_new_epoch(tmp_path):
    src = VideoFileSource(make_video(tmp_path / "v.avi", n=5), loop=True)
    epochs = [src.read().info.source_epoch for _ in range(12)]
    src.close()
    assert epochs[:5] == [0] * 5 and epochs[5:10] == [1] * 5 and epochs[10] == 2


def test_replay_clock_is_monotonic_across_epochs():
    clock = ReplayClock()
    times = []
    for epoch, pts in [(0, 0.0), (0, 0.1), (0, 5.0), (1, 0.0), (1, 0.1)]:
        clock.set(Frame(FrameInfo("v", epoch, 1, 1, 1, 0, 0, pts_s=pts), np.zeros(1)))
        times.append(clock())
    assert times == sorted(times) and len(set(times)) == len(times)


class FakeCapture:
    """Yields `good` frames per connection, then fails a read (camera loss)."""

    opens = 0

    def __init__(self, good: int) -> None:
        FakeCapture.opens += 1
        self.left = good

    def isOpened(self) -> bool:
        return True

    def read(self):
        if self.left <= 0:
            return False, None
        self.left -= 1
        return True, np.zeros((8, 8, 3), np.uint8)

    def release(self) -> None:
        pass


def test_camera_loss_reconnects_with_new_epoch():
    FakeCapture.opens = 0
    src = CameraSource("fake", reconnect_s=0.01, open_capture=lambda _p: FakeCapture(3))
    epochs = set()
    deadline = time.monotonic() + 5
    while len(epochs) < 3 and time.monotonic() < deadline:
        f = src.read(timeout=0.5)
        if f is not None:
            epochs.add(f.info.source_epoch)
    src.close()
    assert {0, 1, 2} <= epochs
    assert src.health.count("camera_lost") >= 2
    assert FakeCapture.opens >= 3


# -- tracker ---------------------------------------------------------------------------

def _box(x: float) -> np.ndarray:
    return np.array([[x, 50, x + 40, 150]], np.float32)


def test_tracker_keeps_id_and_reports_detector_box():
    from corvidia_perception.detector import PersonTracker

    tr = PersonTracker(TrackerConfig(frame_rate=10))
    ids = set()
    for i in range(1, 11):
        dets = tr.update(_box(10 + 3 * i), np.array([0.9]), frame_id=i)
        for d in dets:
            ids.add(d.track_id)
            assert d.bbox.x1 == pytest.approx(10 + 3 * i)
            assert d.confidence == pytest.approx(0.9)
    assert len(ids) == 1


def test_tracker_ages_through_dropped_frames():
    from corvidia_perception.detector import PersonTracker

    cfg = TrackerConfig(frame_rate=30, track_buffer=30)
    tr = PersonTracker(cfg)
    for i in range(1, 6):
        (first,) = tr.update(_box(100), np.array([0.9]), frame_id=i)
    # Short gap: same track resumes.
    (d,) = tr.update(_box(100), np.array([0.9]), frame_id=15)
    assert d.track_id == first.track_id
    # A gap longer than the track buffer expires the track even though only one
    # update call happened in between.
    tr.update(_box(100), np.array([0.9]), frame_id=16)
    tr.update(np.zeros((0, 4)), np.zeros(0), frame_id=17)
    out = []
    for i in range(60, 64):
        out = tr.update(_box(100), np.array([0.9]), frame_id=i) or out
    assert out and out[0].track_id != first.track_id


# -- preview ---------------------------------------------------------------------------

def test_preview_serves_frames_and_health():
    server = PreviewServer(port=0, fps=50, host="127.0.0.1", health_fn=lambda: {"ok": 1})
    try:
        frame = Frame(FrameInfo("t", 0, 1, 320, 240, 0, 0), np.zeros((240, 320, 3), np.uint8))
        server.update(PreviewState(frame, [Detection(7, BBox(10, 10, 60, 120), 0.8)],
                                   {7: "confirmed"}, ["hello"]))
        base = f"http://127.0.0.1:{server.port}"
        jpeg = urllib.request.urlopen(base + "/frame.jpg", timeout=5).read()
        assert jpeg[:2] == b"\xff\xd8"
        assert json.loads(urllib.request.urlopen(base + "/health", timeout=5).read()) == {"ok": 1}
    finally:
        server.close()


# -- end to end on the GPU -------------------------------------------------------------

needs_gpu = pytest.mark.skipif(not (VTEST.exists() and MODEL.exists() and ENGINE.exists()),
                               reason="needs vtest.avi and ~/models/yolo11n.{pt,engine}")


@pytest.mark.gpu
@needs_gpu
def test_every_frame_replay_end_to_end(tmp_path):
    from corvidia_perception.run import run

    report = run(["--video", str(VTEST), "--mode", "every", "--max-frames", "150",
                  "--events", str(tmp_path), "--quiet"])
    assert report["frames_processed"] == 150 and report["frames_dropped"] == 0
    assert report["detector_runtime"] == "TrtYoloDetector"
    assert report["events"] > 0
    assert report["max_events_per_track"] == 1
    assert set(report["results"]) == {"confirmed"}
    assert not report["health"]["faults"]
    session = next(tmp_path.iterdir())
    for path in session.glob("*/event.json"):
        event = json.loads(path.read_text())
        assert event["status"] == "complete"
        assert (path.parent / "crop.jpg").stat().st_size > 0
        assert event["detector"]["model"].endswith("yolo11n.engine")
        assert event["timestamps"]["capture_clock"] == "video_pts"
        x1, y1, x2, y2 = event["crop_region"]
        assert 0 <= x1 < x2 <= 768 and 0 <= y1 < y2 <= 576


@pytest.mark.gpu
@needs_gpu
def test_realtime_replay_drops_frames_without_faults(tmp_path):
    from corvidia_perception.run import run

    report = run(["--video", str(VTEST), "--mode", "realtime", "--speed", "4", "--duration", "12",
                  "--stub-delay", "0.5", "--events", str(tmp_path), "--quiet"])
    assert report["frames_dropped"] > 0
    assert report["events"] > 0
    assert report["max_events_per_track"] == 1
    assert report["confirmation_ms"]["p50"] >= 500
    assert not report["health"]["faults"]


@pytest.mark.gpu
@needs_gpu
def test_gpu_detections_match_cpu():
    """The cu130 PyTorch wheel has no sm_87 build; check Orin results against the CPU."""
    script = f"""
import cv2, json, sys
from ultralytics import YOLO
dev = sys.argv[1]
cap = cv2.VideoCapture({str(VTEST)!r})
for _ in range(40): ok, f = cap.read()
r = YOLO({str(MODEL)!r}).predict(f, device=dev, classes=[0], conf=0.25, verbose=False)[0]
print(json.dumps(sorted(r.boxes.xyxy.cpu().numpy().round(1).tolist())))
"""
    out = {}
    for dev in ("0", "cpu"):  # separate processes: selecting cpu hides CUDA in-process
        res = subprocess.run([sys.executable, "-c", script, dev], capture_output=True, text=True,
                             check=True)
        out[dev] = np.array(json.loads(res.stdout.strip().splitlines()[-1]))
    assert out["0"].shape == out["cpu"].shape and len(out["cpu"]) > 0
    assert np.abs(out["0"] - out["cpu"]).max() < 1.0


def test_memory_floor_raises_and_clears_fault():
    from corvidia_perception.health import Health
    from corvidia_perception.run import SystemSampler

    if not Path("/proc/meminfo").exists():
        pytest.skip("needs /proc/meminfo")
    health = Health()
    sampler = SystemSampler(interval_s=60, health=health, min_available_mb=10**9)
    sampler.sample()
    assert "memory_low" in health.snapshot()["faults"]
    sampler._min_available_mb = 0
    sampler.sample()
    assert "memory_low" not in health.snapshot()["faults"]
    report = sampler.stop()
    assert report["min_available_mb"] > 0


def _vtest_frames(n: int, step: int = 1) -> list:
    cap = cv2.VideoCapture(str(VTEST))
    frames = []
    for i in range(n * step):
        ok, f = cap.read()
        if not ok:
            break
        if i % step == 0:
            frames.append(f)
    cap.release()
    return frames


@pytest.mark.gpu
@needs_gpu
def test_live_pipeline_never_imports_torch(tmp_path):
    script = f"""
import sys
from corvidia_perception.run import run
r = run(["--video", {str(VTEST)!r}, "--mode", "every", "--max-frames", "60",
         "--events", {str(tmp_path)!r}, "--quiet"])
assert r["detector_runtime"] == "TrtYoloDetector", r["detector_runtime"]
bad = sorted(m for m in sys.modules if m.split(".")[0] in ("torch", "ultralytics", "torchvision"))
print("LOADED", bad)
assert not bad, bad
"""
    res = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert res.returncode == 0, res.stdout[-2000:] + res.stderr[-2000:]


@pytest.mark.gpu
@needs_gpu
def test_trt_detector_matches_ultralytics_engine():
    """Our pre/post-processing on the same engine must match Ultralytics'."""
    script = f"""
import json, os, sys
os.environ["YOLO_AUTOINSTALL"] = "False"
import cv2
from ultralytics import YOLO
cap = cv2.VideoCapture({str(VTEST)!r})
m = YOLO({str(ENGINE)!r}, task="detect")
out = []
for i in range(120):
    ok, f = cap.read()
    if i % 10: continue
    r = m.predict(f, device=0, classes=[0], conf=0.1, iou=0.7, verbose=False)[0]
    out.append([r.boxes.xyxy.cpu().numpy().tolist(), r.boxes.conf.cpu().numpy().tolist()])
print(json.dumps(out))
"""
    res = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
    ref = json.loads(res.stdout.strip().splitlines()[-1])

    from corvidia_perception.config import DetectorConfig
    from corvidia_perception.detector import make_detector

    det = make_detector(DetectorConfig(model=str(ENGINE)))
    frames = _vtest_frames(12, step=10)
    matched = total = 0
    for f, (rboxes, rconf) in zip(frames, ref):
        boxes, conf = det.detect(f)
        rboxes = np.array(rboxes).reshape(-1, 4)
        total += len(rboxes)
        if len(boxes) and len(rboxes):
            from corvidia_perception.bytetrack import iou_matrix
            iou = iou_matrix(rboxes, boxes)
            matched += int((iou.max(1) > 0.98).sum())
        assert abs(len(boxes) - len(rboxes)) <= 1
    det.close()
    assert total > 20
    assert matched / total >= 0.97, (matched, total)


@pytest.mark.gpu
@needs_gpu
def test_bytetrack_matches_ultralytics_tracker():
    """Same detections in, same track IDs and boxes out."""
    pytest.importorskip("ultralytics")
    from types import SimpleNamespace

    from ultralytics.trackers.byte_tracker import BYTETracker

    from corvidia_perception.bytetrack import ByteTracker
    from corvidia_perception.config import DetectorConfig
    from corvidia_perception.detector import make_detector

    class Dets:
        def __init__(self, xyxy, conf):
            self.xyxy, self.conf = xyxy.astype(np.float32), conf.astype(np.float32)
            self.cls = np.zeros_like(self.conf)

        @property
        def xywh(self):
            return np.concatenate([(self.xyxy[:, :2] + self.xyxy[:, 2:]) / 2,
                                   self.xyxy[:, 2:] - self.xyxy[:, :2]], 1)

        def __len__(self):
            return len(self.conf)

        def __getitem__(self, i):
            return Dets(self.xyxy[i], self.conf[i])

    cfg = TrackerConfig(frame_rate=10)
    ours = ByteTracker(cfg)
    args = SimpleNamespace(tracker_type="bytetrack", track_high_thresh=cfg.track_high_thresh,
                           track_low_thresh=cfg.track_low_thresh, new_track_thresh=cfg.new_track_thresh,
                           track_buffer=cfg.track_buffer, match_thresh=cfg.match_thresh,
                           fuse_score=cfg.fuse_score)
    theirs = BYTETracker(args, frame_rate=cfg.frame_rate)
    det = make_detector(DetectorConfig(model=str(ENGINE)))
    same = total = 0
    for f in _vtest_frames(400):
        xyxy, conf = det.detect(f)
        a = ours.update(xyxy, conf)
        b = theirs.update(Dets(xyxy, conf))
        a = a[np.argsort(a[:, 4])]
        b = np.asarray(b, dtype=float).reshape(-1, 8)
        b = b[np.argsort(b[:, 4])]
        total += 1
        same += (len(a) == len(b) and np.array_equal(a[:, 4], b[:, 4])
                 and (len(a) == 0 or np.abs(a[:, :4] - b[:, :4]).max() < 0.5))
    det.close()
    assert same / total >= 0.99, (same, total)

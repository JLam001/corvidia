"""ByteTrack multi-object tracker (Zhang et al., 2022), NumPy only.

Implemented from the published algorithm and the MIT-licensed reference
(github.com/ifzhang/ByteTrack), with the association stages and thresholds used
by Ultralytics' `bytetrack.yaml` so results match the previous tracker. It has
no PyTorch dependency, so the live pipeline does not load PyTorch.
"""

from __future__ import annotations

import enum

import lap
import numpy as np

from .config import TrackerConfig


class KalmanFilterXYAH:
    """Constant-velocity Kalman filter on (center x, center y, aspect, height)."""

    def __init__(self) -> None:
        self._motion = np.eye(8)
        for i in range(4):
            self._motion[i, 4 + i] = 1.0
        self._update = np.eye(4, 8)
        self._w_pos = 1.0 / 20
        self._w_vel = 1.0 / 160

    def initiate(self, xyah: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        mean = np.r_[xyah, np.zeros(4)]
        h = xyah[3]
        std = [2 * self._w_pos * h, 2 * self._w_pos * h, 1e-2, 2 * self._w_pos * h,
               10 * self._w_vel * h, 10 * self._w_vel * h, 1e-5, 10 * self._w_vel * h]
        return mean, np.diag(np.square(std))

    def multi_predict(self, mean: np.ndarray, cov: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        h = mean[:, 3]
        std = np.stack([self._w_pos * h, self._w_pos * h, np.full_like(h, 1e-2), self._w_pos * h,
                        self._w_vel * h, self._w_vel * h, np.full_like(h, 1e-5), self._w_vel * h], 1)
        motion_cov = np.stack([np.diag(s) for s in np.square(std)])
        mean = mean @ self._motion.T
        cov = self._motion @ cov @ self._motion.T + motion_cov
        return mean, cov

    def update(self, mean: np.ndarray, cov: np.ndarray, xyah: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        h = mean[3]
        std = [self._w_pos * h, self._w_pos * h, 1e-1, self._w_pos * h]
        proj_mean = self._update @ mean
        proj_cov = self._update @ cov @ self._update.T + np.diag(np.square(std))
        gain = np.linalg.solve(proj_cov, (cov @ self._update.T).T).T
        mean = mean + (xyah - proj_mean) @ gain.T
        cov = cov - gain @ proj_cov @ gain.T
        return mean, cov


class State(enum.IntEnum):
    NEW = 0
    TRACKED = 1
    LOST = 2
    REMOVED = 3


def _tlwh_to_xyah(tlwh: np.ndarray) -> np.ndarray:
    ret = np.asarray(tlwh, dtype=float).copy()
    ret[:2] += ret[2:] / 2
    ret[2] /= ret[3]
    return ret


class STrack:
    def __init__(self, xyxy: np.ndarray, score: float, idx: int) -> None:
        x1, y1, x2, y2 = (float(v) for v in xyxy)
        self._tlwh = np.array([x1, y1, x2 - x1, y2 - y1], dtype=float)
        self.score = float(score)
        self.idx = idx
        self.mean: np.ndarray | None = None
        self.cov: np.ndarray | None = None
        self.track_id = 0
        self.state = State.NEW
        self.is_activated = False
        self.frame_id = 0
        self.start_frame = 0
        self.tracklet_len = 0

    @property
    def tlwh(self) -> np.ndarray:
        if self.mean is None:
            return self._tlwh.copy()
        ret = self.mean[:4].copy()
        ret[2] *= ret[3]
        ret[:2] -= ret[2:] / 2
        return ret

    @property
    def xyxy(self) -> np.ndarray:
        ret = self.tlwh
        ret[2:] += ret[:2]
        return ret

    @property
    def end_frame(self) -> int:
        return self.frame_id

    def activate(self, kf: KalmanFilterXYAH, frame_id: int, track_id: int) -> None:
        self.track_id = track_id
        self.mean, self.cov = kf.initiate(_tlwh_to_xyah(self._tlwh))
        self.tracklet_len = 0
        self.state = State.TRACKED
        if frame_id == 1:
            self.is_activated = True
        self.frame_id = self.start_frame = frame_id

    def re_activate(self, kf: KalmanFilterXYAH, new: STrack, frame_id: int) -> None:
        self.mean, self.cov = kf.update(self.mean, self.cov, _tlwh_to_xyah(new.tlwh))
        self.tracklet_len = 0
        self.state = State.TRACKED
        self.is_activated = True
        self.frame_id = frame_id
        self.score, self.idx = new.score, new.idx

    def update(self, kf: KalmanFilterXYAH, new: STrack, frame_id: int) -> None:
        self.frame_id = frame_id
        self.tracklet_len += 1
        self.mean, self.cov = kf.update(self.mean, self.cov, _tlwh_to_xyah(new.tlwh))
        self.state = State.TRACKED
        self.is_activated = True
        self.score, self.idx = new.score, new.idx


def iou_matrix(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    if len(a) == 0 or len(b) == 0:
        return np.zeros((len(a), len(b)))
    x1 = np.maximum(a[:, None, 0], b[None, :, 0])
    y1 = np.maximum(a[:, None, 1], b[None, :, 1])
    x2 = np.minimum(a[:, None, 2], b[None, :, 2])
    y2 = np.minimum(a[:, None, 3], b[None, :, 3])
    inter = np.clip(x2 - x1, 0, None) * np.clip(y2 - y1, 0, None)
    area_a = (a[:, 2] - a[:, 0]) * (a[:, 3] - a[:, 1])
    area_b = (b[:, 2] - b[:, 0]) * (b[:, 3] - b[:, 1])
    return inter / (area_a[:, None] + area_b[None, :] - inter + 1e-7)


def iou_distance(tracks: list[STrack], dets: list[STrack]) -> np.ndarray:
    a = np.array([t.xyxy for t in tracks]).reshape(-1, 4)
    b = np.array([d.xyxy for d in dets]).reshape(-1, 4)
    return 1 - iou_matrix(a, b)


def fuse_score(cost: np.ndarray, dets: list[STrack]) -> np.ndarray:
    if cost.size == 0:
        return cost
    scores = np.array([d.score for d in dets])
    return 1 - (1 - cost) * scores[None, :]


def linear_assignment(cost: np.ndarray, thresh: float) -> tuple[list[tuple[int, int]], np.ndarray, np.ndarray]:
    if cost.size == 0:
        return [], np.arange(cost.shape[0]), np.arange(cost.shape[1])
    _, x, y = lap.lapjv(cost, extend_cost=True, cost_limit=thresh)
    matches = [(i, int(j)) for i, j in enumerate(x) if j >= 0]
    return matches, np.where(x < 0)[0], np.where(y < 0)[0]


def _joint(a: list[STrack], b: list[STrack]) -> list[STrack]:
    seen = {t.track_id for t in a}
    return a + [t for t in b if t.track_id not in seen]


def _sub(a: list[STrack], b: list[STrack]) -> list[STrack]:
    ids = {t.track_id for t in b}
    return [t for t in a if t.track_id not in ids]


def _remove_duplicates(a: list[STrack], b: list[STrack]) -> tuple[list[STrack], list[STrack]]:
    dist = iou_distance(a, b)
    dup_a, dup_b = set(), set()
    for p, q in zip(*np.where(dist < 0.15)):
        if a[p].frame_id - a[p].start_frame > b[q].frame_id - b[q].start_frame:
            dup_b.add(q)
        else:
            dup_a.add(p)
    return ([t for i, t in enumerate(a) if i not in dup_a],
            [t for i, t in enumerate(b) if i not in dup_b])


class ByteTracker:
    def __init__(self, cfg: TrackerConfig) -> None:
        self.cfg = cfg
        self.max_time_lost = int(cfg.frame_rate / 30.0 * cfg.track_buffer)
        self.kf = KalmanFilterXYAH()
        self.reset()

    def reset(self) -> None:
        self.tracked: list[STrack] = []
        self.lost: list[STrack] = []
        self.removed: list[STrack] = []
        self.frame_id = 0
        self._next_id = 0

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _predict(self, tracks: list[STrack]) -> None:
        if not tracks:
            return
        mean = np.stack([t.mean.copy() for t in tracks])
        cov = np.stack([t.cov for t in tracks])
        for i, t in enumerate(tracks):
            if t.state != State.TRACKED:
                mean[i, 7] = 0
        mean, cov = self.kf.multi_predict(mean, cov)
        for i, t in enumerate(tracks):
            t.mean, t.cov = mean[i], cov[i]

    def update(self, xyxy: np.ndarray, scores: np.ndarray) -> np.ndarray:
        """One frame of detections -> rows [x1, y1, x2, y2, track_id, score, det_index]."""
        cfg = self.cfg
        self.frame_id += 1
        xyxy = np.asarray(xyxy, dtype=float).reshape(-1, 4)
        scores = np.asarray(scores, dtype=float).reshape(-1)
        high = np.flatnonzero(scores >= cfg.track_high_thresh)
        second = np.flatnonzero((scores > cfg.track_low_thresh) & (scores < cfg.track_high_thresh))
        detections = [STrack(xyxy[i], scores[i], int(i)) for i in high]

        activated, refind, lost, removed = [], [], [], []
        unconfirmed = [t for t in self.tracked if not t.is_activated]
        tracked = [t for t in self.tracked if t.is_activated]

        # First association: confirmed and lost tracks against high-score boxes.
        pool = _joint(tracked, self.lost)
        self._predict(pool)
        dists = iou_distance(pool, detections)
        if cfg.fuse_score:
            dists = fuse_score(dists, detections)
        matches, u_track, u_det = linear_assignment(dists, cfg.match_thresh)
        for it, idet in matches:
            track, det = pool[it], detections[idet]
            if track.state == State.TRACKED:
                track.update(self.kf, det, self.frame_id)
                activated.append(track)
            else:
                track.re_activate(self.kf, det, self.frame_id)
                refind.append(track)

        # Second association: remaining tracked tracks against low-score boxes.
        second_dets = [STrack(xyxy[i], scores[i], int(i)) for i in second]
        remaining = [pool[i] for i in u_track if pool[i].state == State.TRACKED]
        dists = iou_distance(remaining, second_dets)
        matches, u_track2, _ = linear_assignment(dists, 0.5)
        for it, idet in matches:
            track, det = remaining[it], second_dets[idet]
            if track.state == State.TRACKED:
                track.update(self.kf, det, self.frame_id)
                activated.append(track)
            else:
                track.re_activate(self.kf, det, self.frame_id)
                refind.append(track)
        for it in u_track2:
            track = remaining[it]
            if track.state != State.LOST:
                track.state = State.LOST
                lost.append(track)

        # Unconfirmed tracks (seen once) against leftover high-score boxes.
        detections = [detections[i] for i in u_det]
        dists = iou_distance(unconfirmed, detections)
        if cfg.fuse_score:
            dists = fuse_score(dists, detections)
        matches, u_unconfirmed, u_det = linear_assignment(dists, 0.7)
        for it, idet in matches:
            unconfirmed[it].update(self.kf, detections[idet], self.frame_id)
            activated.append(unconfirmed[it])
        for it in u_unconfirmed:
            unconfirmed[it].state = State.REMOVED
            removed.append(unconfirmed[it])

        # New tracks.
        for idet in u_det:
            det = detections[idet]
            if det.score >= cfg.new_track_thresh:
                det.activate(self.kf, self.frame_id, self._new_id())
                activated.append(det)

        for track in self.lost:
            if self.frame_id - track.end_frame > self.max_time_lost:
                track.state = State.REMOVED
                removed.append(track)

        self.tracked = [t for t in self.tracked if t.state == State.TRACKED]
        self.tracked = _joint(self.tracked, activated)
        self.tracked = _joint(self.tracked, refind)
        self.lost = _sub(self.lost, self.tracked)
        self.lost.extend(lost)
        self.lost = _sub(self.lost, self.removed)
        self.tracked, self.lost = _remove_duplicates(self.tracked, self.lost)
        self.removed.extend(removed)
        self.removed = self.removed[-1000:]

        rows = [[*t.xyxy, t.track_id, t.score, t.idx] for t in self.tracked if t.is_activated]
        return np.asarray(rows, dtype=float).reshape(-1, 7)

"""Person-description stand demo; defaults to real perception and simulated motors.

Only the supervisor renews the motor lease. Inference, HTTP handlers, and file
writes cannot renew it. No service startup or model response can start motors.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import multiprocessing as mp
import os
from pathlib import Path
import queue
import signal
import threading
import time
import uuid

from .appearance import AppearanceRequirements
from .mission_prompt import parse_mission_prompt


ACTIVE = {"starting", "searching", "confirming", "saving"}
TERMINAL = {"complete", "failed", "timed_out", "cancelled"}
MISSION_PERCENT = 25.0
MISSION_DURATION_MS = 60_000
PREPARING_TIMEOUT_S = 15.0
READINESS = {"guarded_stand", "hands_clear", "power_disconnect_accessible", "motors_still", "esc_startup_finished"}


@dataclass(frozen=True)
class MissionSpec:
    mission_id: str
    appearance: str
    requirements: AppearanceRequirements
    percent: float = MISSION_PERCENT
    duration_ms: int = MISSION_DURATION_MS
    completion_mode: str = "first_match"
    prompt: str = ""

    @classmethod
    def create(cls, appearance, percent=MISSION_PERCENT, duration_ms=None):
        parsed = parse_mission_prompt(appearance)
        if duration_ms is None:
            duration_ms = parsed.duration_ms
        elif parsed.completion_mode == "timed_collection" and duration_ms != parsed.duration_ms:
            raise ValueError("Duration conflicts with the mission brief")
        if isinstance(percent, bool) or not isinstance(percent, (int, float)) or not math.isfinite(percent):
            raise ValueError("Motor input must be a finite number")
        if not 0 < percent <= MISSION_PERCENT:
            raise ValueError(f"Stand input must be greater than zero and at most {MISSION_PERCENT:g}%")
        if type(duration_ms) is not int or not 1000 <= duration_ms <= 60_000:
            raise ValueError("Duration must be 1,000–60,000 integer milliseconds")
        return cls(uuid.uuid4().hex, parsed.appearance, parsed.requirements, float(percent),
                   duration_ms, parsed.completion_mode, parsed.prompt)

    @property
    def description_sha256(self):
        return hashlib.sha256(self.appearance.encode()).hexdigest()


class AsyncJournal:
    """Bounded audit writer. A stalled disk never stalls the motor supervisor."""
    def __init__(self, root: Path):
        self.root = root
        self.jobs = queue.Queue(maxsize=128)
        self.error = None
        self.busy_since = None
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name="stand-journal")
        self.thread.start()

    def record(self, row):
        try:
            self.jobs.put_nowait((time.monotonic(), row))
        except queue.Full:
            self.error = "mission audit queue overflow"

    def fault(self):
        if self.busy_since is not None and time.monotonic() - self.busy_since > 2:
            return "mission audit write exceeded two seconds"
        return self.error

    def _run(self):
        while not self.closed.is_set() or not self.jobs.empty():
            try:
                queued, row = self.jobs.get(timeout=.1)
            except queue.Empty:
                continue
            self.busy_since = queued
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                destination = self.root / (row["mission_id"] + ".jsonl")
                with destination.open("a") as stream:
                    stream.write(json.dumps(row, allow_nan=False) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            except Exception as exc:
                self.error = f"mission audit failed: {type(exc).__name__}: {exc}"
            finally:
                self.busy_since = None

    def close(self):
        self.closed.set()
        self.thread.join(2)


class StandSupervisor:
    """Deterministic state machine; tick() is called by one non-GPU thread."""
    def __init__(self, motor_factory, worker_commands, *, mode="observe", clock=time.monotonic,
                 journal=None, evidence_root=None, telemetry_observer=None):
        self.clock, self.mode = clock, mode
        self.motor_factory, self.worker_commands = motor_factory, worker_commands
        self.motor = motor_factory()
        self.motor.start_background()
        self.observer = telemetry_observer
        if self.observer:
            self.observer.start_background()
        self.journal = journal
        self.evidence_root = Path(evidence_root).resolve() if evidence_root else None
        self.commands = queue.Queue(maxsize=16)
        self.lock = threading.Lock()
        self.stop_requested = threading.Event()
        self._pending_mission = None
        self._cancelled_pending = set()
        self.prepare_deadline = None
        self.pending_readiness = {}
        self._snapshot = {}
        self.spec = None
        self.state = "idle"
        self.error = None
        self.guidance = "hold"
        self.perception_ready = False
        self.perception_error = None
        self.source_epoch = None
        self.last_capture = self.last_processed = None
        self.started_mono = self.deadline = self.stopping_mono = None
        self.stage = None
        self.stage_started = None
        self.pending_terminal = None
        self.result_path = None
        self.evidence = None
        self.captures = []
        self.capture_events = set()
        self.capture_tracks = set()
        self.result = None
        self.finished_missions = {}
        self.recovery_required = False
        self.resource_error = None
        self.memory_available_mib = None
        self.resource_checked_mono = None
        self._publish()

    def submit(self, message):
        """Reserve one complete mission; clients cannot choose motor settings."""
        action = message.get("action")
        if action == "stop":
            if set(message) - {"action", "mission_id"}:
                raise ValueError("Unexpected command field")
            with self.lock:
                requested = message.get("mission_id")
                if self._pending_mission and requested == self._pending_mission:
                    self._cancelled_pending.add(requested)
                elif requested is not None and requested == self._snapshot.get("mission_id"):
                    self.stop_requested.set()
                else:
                    raise ValueError("Mission does not match current session")
            return {"accepted": True}
        if action != "mission":
            raise ValueError("Unknown action; submit a mission or stop it")
        if set(message) - {"action", "appearance", "readiness"}:
            raise ValueError("Mission input cannot set motor power or raw timing fields; use the mission brief")
        spec = MissionSpec.create(message.get("appearance"))
        readiness = message.get("readiness", {})
        if not isinstance(readiness, dict) or (readiness and
                (set(readiness) != READINESS or any(v is not True for v in readiness.values()))):
            raise ValueError("Confirm all five readiness observations")
        if self.mode == "hardware" and set(readiness) != READINESS:
            raise ValueError("Confirm guarded stand, hands clear, and accessible power disconnect")
        if self.mode == "telemetry":
            raise ValueError("Telemetry mode cannot run missions")
        with self.lock:
            if self.recovery_required:
                raise ValueError("Stop was unverified: verify physical stopping and restart the service")
            if self._pending_mission or self._snapshot.get("state") not in ({"idle"} | TERMINAL):
                raise ValueError("A mission is already in progress")
            self._pending_mission = spec.mission_id
            try:
                self.commands.put_nowait({"action": "mission", "spec": spec,
                                         "readiness": dict(readiness), "submitted_mono": self.clock()})
            except queue.Full:
                self._pending_mission = None
                raise ValueError("Command queue full") from None
        return {"accepted": True, "mission_id": spec.mission_id}

    def snapshot(self):
        with self.lock:
            return dict(self._snapshot)

    def _audit(self, event, **extra):
        if self.journal is not None and self.spec is not None:
            telemetry = self.observer.snapshot() if self.observer else self.motor.snapshot()
            self.journal.record({"mission_id": self.spec.mission_id, "mono": self.clock(),
                                 "wall": time.time(), "event": event, "state": self.state,
                                 "mode": self.mode, "raw_sensor_telemetry": telemetry.get("telemetry", {}),
                                 "telemetry_received_mono": telemetry.get("telemetry_received_mono", {}),
                                 "telemetry_simulated": self.mode == "observe" and self.observer is None,
                                 "attitude_frame": "sensor", "capture_time_alignment": "unavailable", **extra})

    def _send(self, message):
        try:
            self.worker_commands.put_nowait(message)
            return True
        except queue.Full:
            if self.state in ACTIVE:
                self._stop("perception command queue overflow", "failed")
            else:
                self.error = "perception command queue overflow"
            return False

    def _fresh(self, now):
        return (self.perception_ready and self.last_capture is not None and self.last_processed is not None
                and 0 <= now - self.last_capture <= .5 and 0 <= now - self.last_processed <= .5)

    def _prepare(self, message):
        if self.recovery_required:
            raise ValueError("Stop was unverified: verify physical stopping and restart the service")
        if self.state in ACTIVE or self.state in {"preparing", "stopping"}:
            raise ValueError("Stop the current mission before preparing another")
        if self.mode == "telemetry":
            raise ValueError("Telemetry mode only reads USB; start observation mode to rehearse")
        if self.state in TERMINAL:
            # One motor object owns one operator run. Never reuse a consumed token/session.
            self.motor.close()
            self.motor = self.motor_factory()
            self.motor.start_background()
        self.spec = message.get("spec") or MissionSpec.create(
            message.get("appearance"), message.get("percent", MISSION_PERCENT),
            message.get("duration_ms"))
        self.state, self.error, self.guidance = "prepared", None, "hold"
        self.started_mono = self.deadline = self.stopping_mono = None
        self.stage = self.stage_started = self.pending_terminal = None
        self.evidence = self.result_path = self.result = None
        self.captures = []
        self.capture_events = set()
        self.capture_tracks = set()
        self.stop_requested.clear()
        self._audit("prepared", settings=asdict(self.spec), description_sha256=self.spec.description_sha256)

    def _start(self, message):
        if not self.spec or message.get("mission_id") != self.spec.mission_id:
            raise ValueError("Prepare and review this mission first")
        if self.state not in {"prepared", "preparing"}:
            raise ValueError("Start is accepted once per prepared mission")
        if self.mode == "hardware" and not all(message.get("readiness", {}).get(k) is True for k in READINESS):
            raise ValueError("Confirm guarded stand, hands clear, and accessible power disconnect")
        now = self.clock()
        if self.state == "preparing" and now >= self.prepare_deadline:
            raise ValueError("mission preflight deadline expired")
        if not self._fresh(now):
            raise ValueError("Waiting for fresh camera and detector progress")
        if not self.motor.snapshot().get("ready"):
            raise ValueError("Motor interface is not ready")
        if self.observer and not self.observer.snapshot().get("ready"):
            raise ValueError("Waiting for healthy read-only STM32 telemetry")
        if self.resource_error:
            raise ValueError(self.resource_error)
        if self.memory_available_mib is None:
            raise ValueError("Waiting for Jetson memory headroom measurement")
        if self.journal and self.journal.fault():
            raise ValueError(self.journal.fault())
        # Serialize the final start decision with HTTP Abort admission. All motor
        # methods here only update/enqueue bounded in-memory state, never do I/O.
        with self.lock:
            cancelled = (self.spec.mission_id in self._cancelled_pending
                         or self.stop_requested.is_set())
            if not cancelled:
                now = self.clock()
                if self.state == "preparing" and now >= self.prepare_deadline:
                    raise ValueError("mission preflight deadline expired")
                if not self._fresh(now):
                    raise ValueError("Waiting for fresh camera and detector progress")
                self.started_mono, self.deadline = now, now + self.spec.duration_ms / 1000
                self.state, self.error = "starting", None
                self.motor.refresh_lease()
                self.motor.start_all(self.spec.percent, self.spec.duration_ms)
        if cancelled:
            self._stop("operator stop", "cancelled")
            return
        self._audit("start_requested", settings=asdict(self.spec))

    def _stop(self, reason, terminal):
        if self.state == "stopping":
            if self.pending_terminal == "complete" and terminal != "complete":
                self.pending_terminal, self.error = terminal, reason
            return
        if self.state in TERMINAL:
            return
        if self.state not in ACTIVE:
            if self.state in {"prepared", "preparing"}:
                self.state, self.error = terminal, reason
                self._audit("mission_ended_before_start", reason=reason, terminal=terminal)
            return
        # Revoke motor authority before any queue, file, model or HTTP operation.
        self.motor.stop(reason)
        self.perception_ready = False  # Require a new ready after model cleanup.
        self.state, self.pending_terminal = "stopping", terminal
        self.error = None if terminal == "complete" else reason
        self.guidance, self.stopping_mono = "hold", self.clock()
        if terminal == "complete" and self.evidence is not None:
            for source, metric in (("capture_mono", "capture_to_stop_request_ms"),
                                   ("commit_mono", "commit_to_stop_request_ms")):
                value = self.evidence.get(source)
                if isinstance(value, (int, float)) and math.isfinite(value):
                    self.evidence[metric] = (self.stopping_mono - value) * 1000
        self._audit("stop_requested", reason=reason, terminal=terminal, motor=self.motor.snapshot())
        try:
            self.worker_commands.put_nowait({"type": "end", "mission_id": self.spec.mission_id})
        except queue.Full:
            pass  # Motor authority has already been revoked.

    def _active_failure(self, now, motor):
        """The same checks apply before accepting success and renewing authority."""
        fault = self.resource_error or (self.journal.fault() if self.journal else None)
        if self.observer and not self.observer.snapshot().get("ready"):
            fault = "live STM32 telemetry unhealthy"
        if fault:
            return fault, "failed"
        if not self._fresh(now):
            return "camera or detector progress stale", "failed"
        if motor.get("error") or motor.get("fault") or motor.get("start_status") == "failed":
            return str(motor.get("error") or motor.get("fault") or "motor start failed"), "failed"
        if self.stage and now - self.stage_started > (2 if self.stage == "saving" else 4):
            return self.stage + " deadline exceeded", "failed"
        if self.state == "starting" and now - self.started_mono > 3:
            return "motor start acknowledgement timed out", "failed"
        if now >= self.deadline:
            return self._deadline_outcome()
        return None

    def _deadline_outcome(self):
        if self.state == "starting":
            return "mission expired before motor start acknowledgement", "failed"
        if self.spec.completion_mode == "timed_collection":
            return "collection time elapsed", "complete"
        return "mission time limit", "timed_out"

    def event(self, event):
        now = self.clock()
        kind = event.get("type")
        if kind == "ready":
            self.perception_ready = True
            self.perception_error = None
            if self.state not in ACTIVE and self.state not in TERMINAL:
                self.error = None
        if kind in {"ready", "progress"}:
            capture, processed = event.get("capture_mono"), event.get("processed_mono", now)
            if not all(isinstance(v, (int, float)) and math.isfinite(v) and v <= now + .05
                       for v in (capture, processed)):
                self._stop("invalid perception timestamp", "failed")
                return
            epoch = event.get("source_epoch")
            if self.state in ACTIVE and self.source_epoch is not None and epoch != self.source_epoch:
                self._stop("camera source restarted", "failed")
                return
            self.source_epoch = epoch
            self.last_capture, self.last_processed = capture, processed
            if self.state in ACTIVE and event.get("mission_id") == self.spec.mission_id:
                self.guidance = event.get("guidance", "hold")
            return
        if kind == "fault":
            if event.get("mission_id") not in (None, self.spec.mission_id if self.spec else None):
                return
            self.perception_ready = False
            self.perception_error = self.perception_error or str(event.get("reason", "perception failure"))
            self.error = self.perception_error
            self._stop(self.error, "failed")
            return
        if not self.spec or event.get("mission_id") != self.spec.mission_id or self.state not in ACTIVE:
            return
        if now >= self.deadline:
            self._stop(*(self._active_failure(now, self.motor.snapshot()) or self._deadline_outcome()))
            return
        if kind == "stage":
            stage = event.get("stage")
            if stage in {"saving", "confirming"}:
                self.stage, self.stage_started = stage, event.get("mono", now)
                self.state, self.guidance = stage, "hold"
            elif stage == "idle":
                self.stage = self.stage_started = None
                self.state = "searching"
            self._audit("perception_stage", detail=event)
        elif kind == "completion":
            failure = self._active_failure(now, self.motor.snapshot())
            self.stage = self.stage_started = None
            self._audit("confirmation", detail=event)
            if not event.get("committed"):
                self._stop("capture could not be committed", "failed")
            elif event.get("result") == "confirmed":
                captured = event.get("capture_mono")
                if (event.get("source_epoch") != self.source_epoch
                        or isinstance(captured, bool) or not isinstance(captured, (int, float))
                        or not math.isfinite(captured)
                        or not self.started_mono <= captured <= now):
                    self._stop("capture does not belong to active mission frames", "failed")
                    return
                if self.spec.completion_mode == "timed_collection":
                    committed = event.get("commit_mono")
                    track_id, event_id = event.get("track_id"), event.get("event_id")
                    if (isinstance(committed, bool) or not isinstance(committed, (int, float))
                            or not math.isfinite(committed)
                            or not captured <= committed <= now or committed >= self.deadline
                            or type(track_id) is not int or track_id < 0
                            or not isinstance(event_id, str) or not event_id or not event.get("path")):
                        self._stop("invalid collection capture provenance", "failed")
                        return
                    key = (self.source_epoch, track_id)
                    if event_id not in self.capture_events and key not in self.capture_tracks:
                        self.result_path, self.evidence = event["path"], dict(event)
                        self.captures.append(self.evidence)
                        self.capture_events.add(event_id)
                        self.capture_tracks.add(key)
                        self._audit("capture_saved", capture=dict(self.evidence),
                                    capture_count=len(self.captures))
                else:
                    self.result_path, self.evidence = event.get("path"), dict(event)
                    self.captures = [self.evidence]
                if self.stop_requested.is_set():
                    self._stop("operator stop", "cancelled")
                elif failure:
                    self._stop(*failure)
                elif self.spec.completion_mode == "first_match":
                    self._stop("matching person captured", "complete")
                else:
                    self.state, self.guidance = "searching", "search_right"
            elif event.get("result") == "rejected" or (
                    event.get("result") == "unknown" and event.get("reason") in {
                        "ambiguous", "subject_unknown", "upper_color_unknown", "upper_garment_unknown"}):
                if failure:
                    self._stop(*failure)
                else:
                    self.state, self.guidance = "searching", "search_right"
            else:
                self._stop("confirmation failed: " + str(event.get("reason")), "failed")

    def tick(self):
        now = self.clock()
        if self.stop_requested.is_set():
            self.stop_requested.clear()
            self._stop("operator stop", "cancelled")
        # One reserved request starts autonomously after bounded onboard preflight.
        try:
            message = self.commands.get_nowait()
        except queue.Empty:
            message = None
        if message is not None:
            try:
                self._prepare(message)
                self.state = "preparing"
                self.prepare_deadline = message["submitted_mono"] + PREPARING_TIMEOUT_S
                self.pending_readiness = message["readiness"]
            except (ValueError, RuntimeError) as exc:
                self.spec = message["spec"]
                self.error = str(exc)
                self.state = "failed"
        with self.lock:
            cancelled = self.spec and self.spec.mission_id in self._cancelled_pending
            if cancelled:
                self._cancelled_pending.discard(self.spec.mission_id)
        if cancelled:
            self._stop("operator stop", "cancelled")
        if self.state == "preparing":
            if now >= self.prepare_deadline:
                self._stop("mission preflight timed out: " + (self.error or "not ready"), "failed")
            elif self.perception_error:
                self._stop(self.perception_error, "failed")
            else:
                try:
                    self._start({"mission_id": self.spec.mission_id, "readiness": self.pending_readiness})
                except (ValueError, RuntimeError) as exc:
                    self.error = str(exc)
                    if self.state in ACTIVE:
                        self._stop(self.error, "failed")
        motor = self.motor.snapshot()
        if self.state in ACTIVE:
            failure = self._active_failure(now, motor)
            if failure:
                self._stop(*failure)
            else:
                self.motor.refresh_lease()
                if self.state == "starting" and motor.get("start_status") == "accepted":
                    self.state = "searching"
                    self._audit("start_accepted", motor=motor)
                    self._send({"type": "begin", "mission_id": self.spec.mission_id,
                                "appearance": self.spec.appearance, "start_mono": self.started_mono,
                                "record_extra": {"mission": asdict(self.spec), "stand_mode": self.mode,
                                                 "description_sha256": self.spec.description_sha256}})
        if self.state == "stopping":
            motor = self.motor.snapshot()
            if self.pending_terminal == "complete" and (motor.get("fault") or motor.get("error")):
                self.pending_terminal, self.error = "failed", str(motor.get("error") or motor.get("fault"))
            if motor.get("stop_status") == "verified" and motor.get("zero_confirmed"):
                self.state = self.pending_terminal
                self._audit("zero_input_confirmed", motor=motor, evidence=self.evidence)
            elif motor.get("stop_status") == "unverified" or now - self.stopping_mono > 2:
                self.state, self.error = "failed", "STOP UNVERIFIED — use the accessible power disconnect"
                self.recovery_required = True
                self._audit("stop_unverified", motor=motor, evidence=self.evidence)
            if self.state in TERMINAL:
                self.result = {"state": self.state, "error": self.error, "evidence": self.evidence,
                               "completion_mode": self.spec.completion_mode,
                               "capture_count": len(self.captures), "captures": list(self.captures),
                               "motor": motor, "finished_mono": now}
                self.finished_missions[self.spec.mission_id] = self.result
                self._audit("mission_result", result=self.result)
        self._publish()

    def _publish(self):
        now = self.clock()
        motor = self.motor.snapshot()
        telemetry = self.observer.snapshot() if self.observer else motor
        settings = asdict(self.spec) if self.spec else {}
        received = telemetry.get("timestamps", {}).get("last_rx")
        telemetry_rx_age_ms = (round(max(0.0, now - received) * 1000, 1)
                               if isinstance(received, (int, float)) and math.isfinite(received)
                               else None)
        status = {"mode": self.mode, "state": self.state, **settings,
                  "operator": "jetson", "mission_owner": "jetson", "control_authority": "onboard",
                  "error": self.error, "guidance": self.guidance,
                  "lease_required": False, "mission_api": 2,
                  "mission_profile": {"percent": MISSION_PERCENT, "duration_ms": MISSION_DURATION_MS},
                  "remaining_ms": max(0, int((self.deadline - now) * 1000)) if self.deadline else None,
                  "motor": motor, "imu": telemetry.get("telemetry", {}),
                  "telemetry_mode": "live_read_only" if self.observer or self.mode == "telemetry"
                                    else "simulated" if self.mode == "observe" else "live",
                  "recovery_required": self.recovery_required,
                  "preflight": {"camera_ready": self._fresh(now), "motor_ready": motor.get("ready", False),
                                "telemetry_ready": telemetry.get("ready", False),
                                "telemetry_connected": telemetry.get("connected", False),
                                "telemetry_error": telemetry.get("error"),
                                "telemetry_fault": telemetry.get("fault", False),
                                "telemetry_rx_age_ms": telemetry_rx_age_ms,
                                "resource_ready": self.resource_error is None and self.memory_available_mib is not None,
                                "resource_error": self.resource_error,
                                "memory_available_mib": self.memory_available_mib,
                                "minimum_memory_mib": 1536,
                                "resource_checked_mono": self.resource_checked_mono},
                  "perception": {"ready": self.perception_ready, "source_epoch": self.source_epoch,
                                 "error": self.perception_error,
                                 "frame_age_ms": round((now - self.last_capture) * 1000, 1)
                                 if self.last_capture is not None else None},
                  "capture_count": len(self.captures), "captures": list(self.captures),
                  "evidence_available": bool(self.evidence), "result": self.result}
        with self.lock:
            self._snapshot = status
            if self._pending_mission and self.spec and self.spec.mission_id == self._pending_mission:
                self._pending_mission = None

    def evidence_image(self, mission_id):
        current = self.snapshot()
        result = ({"evidence": current["captures"][-1]} if current.get("mission_id") == mission_id
                  and current.get("captures") else self.finished_missions.get(mission_id))
        if not result or not result.get("evidence"):
            return None
        folder = Path(result["evidence"]["path"]).resolve()
        if self.evidence_root is None or not folder.is_relative_to(self.evidence_root):
            return None
        frame = folder / "frame.jpg"
        if not frame.resolve().is_relative_to(self.evidence_root):
            return None
        try:
            return frame.read_bytes(), "image/jpeg"
        except OSError:
            return None

    def close(self):
        self._stop("supervisor shutdown", "cancelled")
        # MotorSession independently supervises stop; wait for bounded verification.
        deadline = time.monotonic() + 2
        while self.state == "stopping" and time.monotonic() < deadline:
            self.tick()
            time.sleep(.02)
        self.motor.close()
        if self.observer:
            self.observer.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("observe", "telemetry", "hardware"), default="observe")
    parser.add_argument("--port", help="Stable STM32 /dev/serial/by-id path (required for telemetry/hardware)")
    parser.add_argument("--http-port", type=int, default=8080)
    parser.add_argument("--events", type=Path, default=Path("~/corvidia-data/stand-events").expanduser())
    parser.add_argument("--model", default="~/models/yolo11n.engine")
    parser.add_argument("--cosmos-url", default="http://127.0.0.1:8010")
    parser.add_argument("--duration", type=float, help="Optional service observation duration; never a motor limit")
    args = parser.parse_args(argv)
    if args.mode != "observe" and not args.port:
        parser.error("--port is required for telemetry or hardware mode")
    from .stand_motor import MotorSession, SimulatedMotorSession
    from .stand_perception import perception_worker
    from .stand_web import StandWebServer
    from .stand_session import default_session_path, write_session, remove_session

    ctx = mp.get_context("spawn")
    commands, events, previews = ctx.Queue(16), ctx.Queue(128), ctx.Queue(1)
    process = ctx.Process(target=perception_worker, args=(commands, events, previews, {
        "model": args.model, "cosmos_url": args.cosmos_url, "events_root": str(args.events)}),
        name="stand-perception", daemon=True)
    factory = (lambda: SimulatedMotorSession()) if args.mode == "observe" else (
        lambda: MotorSession(args.port, execute=args.mode == "hardware"))
    journal = AsyncJournal(args.events.parent / "stand-missions")
    observer = MotorSession(args.port, execute=False) if args.mode == "observe" and args.port else None
    supervisor = StandSupervisor(factory, commands, mode=args.mode, journal=journal,
                                 evidence_root=args.events, telemetry_observer=observer)
    latest_preview = [None]
    web = StandWebServer(port=args.http_port, host="127.0.0.1", status_fn=supervisor.snapshot,
                         command_fn=supervisor.submit, preview_fn=lambda: latest_preview[0],
                         evidence_fn=supervisor.evidence_image)
    done = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: done.set())
    session_path = default_session_path(web.port)
    started = time.monotonic()
    sampled = 0
    process_exit_seen = False
    try:
        write_session(session_path, url=f"http://127.0.0.1:{web.port}", token=web.token, pid=os.getpid())
        process.start()
        print(f"Stand demo mode={args.mode}; terminal session {session_path}", flush=True)
        print(f"Dashboard http://127.0.0.1:{web.port}/?token={web.token}", flush=True)
        while not done.is_set():
            tick_start = time.monotonic()
            for _ in range(64):
                try:
                    event = events.get_nowait()
                    if event.get("type") == "fault":
                        print("Perception fault: " + str(event.get("reason")), flush=True)
                    supervisor.event(event)
                except queue.Empty:
                    break
            try:
                while True:
                    latest_preview[0] = previews.get_nowait()
            except queue.Empty:
                pass
            if not process.is_alive() and not process_exit_seen:
                process_exit_seen = True
                supervisor.event({"type": "fault", "reason": "perception process exited"})
            if tick_start - sampled >= 1:
                try:
                    available = next(int(line.split()[1]) for line in Path("/proc/meminfo").read_text().splitlines()
                                     if line.startswith("MemAvailable:")) / 1024
                    supervisor.memory_available_mib = available
                    supervisor.resource_error = (f"memory headroom {available:.0f} MiB is below 1536 MiB"
                                                 if available < 1536 else None)
                except (OSError, StopIteration, ValueError):
                    supervisor.memory_available_mib = None
                    supervisor.resource_error = "unable to measure Jetson memory headroom"
                supervisor.resource_checked_mono = time.monotonic()
                sampled = tick_start
            supervisor.tick()
            if args.duration is not None and time.monotonic() - started >= args.duration:
                done.set()
            done.wait(max(0, .02 - (time.monotonic() - tick_start)))
    finally:
        supervisor.close()  # Stop before any model/process/network cleanup.
        try:
            commands.put_nowait({"type": "shutdown"})
        except queue.Full:
            pass
        if process.pid is not None:
            process.join(2)
            if process.is_alive():
                process.terminate()
                process.join(2)
        remove_session(session_path, web.token)
        web.close()
        journal.close()


if __name__ == "__main__":
    main()

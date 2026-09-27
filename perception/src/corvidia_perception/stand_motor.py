"""One-shot, operator-bounded MAVLink motor session for a restrained stand demo.

This is not a flight controller. Only this worker owns the serial port. The LLM
must never receive this object: the deterministic supervisor owns its lease.
Importing this module needs neither pyserial nor pymavlink; simulation needs neither.
"""
from __future__ import annotations

import copy
import math
import threading
import time
from collections import deque
from typing import Callable

MODE_ID = 0x44534935
ENABLE_COMMAND, ALL_COMMAND = 31010, 31012
MAX_PERCENT, MAX_DURATION_MS = 25.0, 60000
LEASE_S, HEARTBEAT_S = .250, .100
STATUS_MAX_AGE_S = 1.5
ACK_TIMEOUT_S, STOP_TIMEOUT_S = 1.0, 2.0
_READY = {"DS_ALLOW": 1, "DS_OUTPUT": 1, "DS_ALL": 1, "DS_FAULT": 0,
          "IMU_OK": 1, "DS_ACTIVE": 0, "M1_DS": 0, "M2_DS": 0,
          "M3_DS": 0, "M4_DS": 0, "FW_REV": 5, "DS_MAXP": 100,
          "DS_MAXMS": 60000, "FLT_READY": 0}
_ZERO = ("DS_ACTIVE", "DS_FAULT", "M1_DS", "M2_DS", "M3_DS", "M4_DS")


def _validate(percent, duration_ms):
    if type(percent) not in (int, float) or not math.isfinite(percent) or not 0 < percent <= MAX_PERCENT:
        raise ValueError(f"stand input must be finite and in (0, {MAX_PERCENT:g}] percent")
    if type(duration_ms) is not int or not 20 <= duration_ms <= MAX_DURATION_MS:
        raise ValueError("stand duration must be an integer in [20, 60000] ms")


class _SerialTransport:
    def __init__(self, port: str):
        import serial
        from pymavlink.dialects.v20 import common
        self._serial = serial.Serial(port, 115200, timeout=.01, write_timeout=.05, exclusive=True)
        self._mav = common.MAVLink(self, srcSystem=255, srcComponent=190)
        self._mav.robust_parsing = True

    def write(self, data):
        if self._serial.write(data) != len(data):
            raise OSError("partial MAVLink write")

    def receive(self):
        data = self._serial.read(min(4096, max(1, self._serial.in_waiting)))
        return self._mav.parse_buffer(data) or []

    def heartbeat(self):
        self._mav.heartbeat_send(6, 8, 0, 0, 0)  # GCS, autopilot invalid

    def command(self, number, params):
        self._mav.command_long_send(1, 1, number, 0, *params)

    def close(self):
        self._serial.close()


class MotorSession:
    """Real serial session; execute=False is strictly read-only.

    start_all queues one run after ready=True. refresh_lease must be called by
    healthy mission supervision at least every 250 ms until stopping. No command
    enables a new run after this session has stopped or failed. snapshot never
    blocks on serial I/O. A fresh session is required for a new operator mission.
    """
    def __init__(self, port: str, *, execute: bool = False,
                 _clock: Callable[[], float] = time.monotonic,
                 _transport_factory=None):
        self.port, self.execute = port, execute
        self._clock = _clock
        self._factory = _transport_factory or _SerialTransport
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._cancel = threading.Event()
        self._quit = threading.Event()
        self._thread = None
        self._transport = None
        self._identified = False
        self._used = False
        self._lease_at = -math.inf
        self._heartbeat_at = -math.inf
        self._identified_at = None
        self._request = None
        self._token = None
        self._pending = deque()
        self._stop_sent = False
        self._stop_ack_at = None
        self._run_deadline = None
        self._last_imu_stamp = None
        self._last_imu_progress = None
        self._max_boot_ms = None
        self._state = dict(ready=False, connected=False, active=False,
                           zero_confirmed=False, fault=False, error=None,
                           mode="execute" if execute else "read_only", phase="new",
                           start_status="not_requested", stop_status="not_requested",
                           telemetry={}, telemetry_received_mono={}, timestamps={},
                           requested_percent=None, duration_ms=None, stop_reason=None,
                           firmware_mode=None, physical_stop_measured=False)

    def start_background(self):
        with self._lock:
            if self._thread is not None or self._state["phase"] != "new":
                raise RuntimeError("motor session has already been started")
            self._state["phase"] = "connecting"
            self._thread = threading.Thread(target=self._run, name="stand-mavlink", daemon=True)
            self._thread.start()
        return self

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._state)

    def refresh_lease(self):
        with self._lock:
            if not self._cancel.is_set():
                self._lease_at = self._clock()
        self._wake.set()

    def start_all(self, percent: float, duration_ms: int):
        _validate(percent, duration_ms)
        with self._lock:
            if not self.execute:
                raise RuntimeError("read-only motor session cannot start motors")
            if self._used or self._cancel.is_set():
                raise RuntimeError("motor session is one-shot; create a new operator session")
            if not self._state["ready"] or not self._ready(self._clock()):
                raise RuntimeError("fresh, idle v5 readiness is required")
            if self._clock() - self._lease_at >= LEASE_S:
                raise RuntimeError("fresh supervisor lease is required")
            self._used = True
            self._request = (float(percent), duration_ms)
            self._token = self._state["telemetry"]["DS_TOKEN"]
            self._state.update(ready=False, zero_confirmed=False, start_status="pending",
                               requested_percent=float(percent), duration_ms=duration_ms,
                               phase="preparing")
            self._stamp("start_requested")
        self._wake.set()

    def stop(self, reason: str):
        # This event revokes heartbeat/start permission before taking a lock.
        self._cancel.set()
        with self._lock:
            self._stop(reason)
        self._wake.set()

    def _stop(self, reason):
        self._state["ready"] = False
        if self._state["stop_reason"] is None:
            self._state["stop_reason"] = str(reason)
            self._stamp("stop_requested")
        if self._state["start_status"] == "pending":
            self._state["start_status"] = "failed"
        if self._state["stop_status"] == "not_requested":
            self._state.update(stop_status="pending", phase="stopping", zero_confirmed=False)

    def close(self):
        self.stop("session closed")
        # Let the sole writer issue/verify stop; do not open a second serial port.
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(STOP_TIMEOUT_S + .3)
        self._quit.set()
        self._wake.set()
        if thread and thread is not threading.current_thread():
            thread.join(.2)

    def _stamp(self, name, at=None):
        self._state["timestamps"][name] = self._clock() if at is None else at

    def _fault(self, reason, *, identity_lost=False):
        self._cancel.set()
        with self._lock:
            if identity_lost:
                self._identified = False
            self._state.update(fault=True, ready=False)
            if self._state["error"] is None:
                self._state["error"] = str(reason)
            self._stop(str(reason))

    def _ready(self, now):
        s = self._state
        return (self._identified and self._identified_at is not None and
                now - self._identified_at >= 3.1 and not self._used and
                not self._cancel.is_set() and self._last_imu_progress is not None and
                now - self._last_imu_progress < .250 and
                all(s["telemetry"].get(k) == v and
                    now - s["telemetry_received_mono"].get(k, -math.inf) < STATUS_MAX_AGE_S
                    for k, v in _READY.items()) and
                1 <= s["telemetry"].get("DS_TOKEN", 0) <= 16777215 and
                now - s["telemetry_received_mono"].get("DS_TOKEN", -math.inf) < STATUS_MAX_AGE_S)

    def _connect(self):
        self._transport = self._factory(self.port)
        with self._lock:
            self._state.update(connected=True, phase="observing")
            self._stamp("connected")

    def _run(self):
        try:
            self._connect()
            while not self._quit.is_set():
                self._step()
                with self._lock:
                    finished = self._state["stop_status"] in ("verified", "unverified")
                if finished:
                    break
                self._wake.wait(.005)
                self._wake.clear()
        except Exception as exc:
            self._fault(f"serial worker: {exc}")
            # A write may have failed. No further start or heartbeat is attempted.
            if self.execute and self._identified and self._transport and not self._stop_sent:
                try:
                    self._send_stop()
                except Exception:
                    pass
            with self._lock:
                self._state.update(stop_status="unverified", zero_confirmed=False, phase="failed")
        finally:
            if self._transport:
                try:
                    self._transport.close()
                except Exception:
                    pass
            with self._lock:
                self._state.update(connected=False, ready=False)
                self._stamp("closed")

    def _receive(self, msg, now):
        if (msg.get_srcSystem(), msg.get_srcComponent()) != (1, 1):
            return
        kind = msg.get_type()
        if kind == "BAD_DATA":
            return
        self._stamp("last_rx", now)
        if kind == "HEARTBEAT":
            self._state["firmware_mode"] = msg.custom_mode
            if msg.custom_mode != MODE_ID:
                self._fault("unexpected firmware identity", identity_lost=True)
                return
            if not self._identified:
                self._identified = True
                self._identified_at = now
            self._stamp("heartbeat_rx", now)
        if kind in ("ATTITUDE", "ATTITUDE_QUATERNION", "NAMED_VALUE_INT"):
            boot = int(msg.time_boot_ms)
            if self._max_boot_ms is not None:
                delta = (boot - self._max_boot_ms) & 0xffffffff
                # Minor differences between sensor and status timestamps are normal.
                if 0x80000000 < delta < 0xffffffff - 1000:
                    self._fault("firmware clock reset; restart requires a new session")
                elif delta < 0x80000000:
                    self._max_boot_ms = boot
            else:
                self._max_boot_ms = boot
        if kind in ("ATTITUDE", "ATTITUDE_QUATERNION"):
            stamp = int(msg.time_boot_ms)
            if self._last_imu_stamp is None or 0 < (stamp - self._last_imu_stamp) & 0xffffffff < 0x80000000:
                self._last_imu_stamp = stamp
                self._last_imu_progress = now
                self._stamp("imu_progress", now)
            values = msg.to_dict()
            self._state["telemetry"][kind] = values
            self._state["telemetry_received_mono"][kind] = now
        elif kind == "NAMED_VALUE_INT":
            name = msg.name.decode(errors="replace").rstrip("\0") if isinstance(msg.name, bytes) else str(msg.name)
            self._state["telemetry"][name] = int(msg.value)
            self._state["telemetry_received_mono"][name] = now
            if self._used and not self._cancel.is_set():
                if (name == "DS_FAULT" and msg.value != 0) or (name == "IMU_OK" and msg.value != 1):
                    self._fault(f"firmware health failed: {name}")
                if (name == "DS_ACTIVE" and self._state["phase"] == "running" and msg.value != 1
                        and self._run_deadline is not None and now < self._run_deadline):
                    self._fault("firmware ended the run before host deadline")
        elif kind == "COMMAND_ACK":
            # ACK has no token. FIFO matching is conservative if an ACK is lost:
            # a later stop ACK can never become permission for a canceled start.
            match = next((p for p in self._pending if p[0] == msg.command), None)
            if match is None:
                return
            self._pending.remove(match)
            _, purpose, _ = match
            if msg.result != 0:
                self._fault(f"{purpose} rejected: MAV_RESULT {msg.result}")
                if purpose == "stop":
                    self._state.update(stop_status="unverified", phase="failed")
                return
            self._stamp(f"{purpose}_ack", now)
            if purpose == "enable" and not self._cancel.is_set():
                self._state["phase"] = "enabled"
            elif purpose == "start" and not self._cancel.is_set():
                self._state.update(start_status="accepted", active=True, phase="running")
            elif purpose == "stop":
                self._stop_ack_at = now

    def _send(self, number, params, purpose):
        if not self.execute:
            raise RuntimeError("read-only session attempted a write")
        if purpose != "stop" and self._cancel.is_set():
            return False
        now = self._clock()
        self._transport.command(number, params)
        with self._lock:
            self._pending.append((number, purpose, now))
            self._stamp(f"{purpose}_sent", now)
        return True

    def _send_stop(self):
        self._stop_sent = True
        self._send(ENABLE_COMMAND, [0] * 7, "stop")

    def _step(self):
        # Only this worker touches transport. Public stop never waits for receive.
        messages = self._transport.receive()
        now = self._clock()
        with self._lock:
            for msg in messages:
                self._receive(msg, now)
            if self._cancel.is_set():
                self._stop(self._state["stop_reason"] or "canceled")
            elif self._used:
                if now - self._lease_at >= LEASE_S:
                    self._fault("supervisor lease expired")
                elif self._last_imu_progress is None or now - self._last_imu_progress >= .250:
                    self._fault("IMU telemetry stopped advancing")
                elif self._run_deadline is not None and now >= self._run_deadline:
                    self._cancel.set()
                    self._stop("duration expired")
            elif now - self._state["timestamps"]["connected"] > 6 and not self._identified:
                self._fault("v5 heartbeat not received")
            if not self._used and not self._cancel.is_set():
                self._state["ready"] = self._ready(now)
                self._state["zero_confirmed"] = self._state["ready"]
                if self._state["ready"]:
                    self._state["phase"] = "idle"
            if not self._cancel.is_set():
                for _, purpose, sent in tuple(self._pending):
                    if now - sent >= ACK_TIMEOUT_S:
                        self._fault(f"{purpose} acknowledgement timeout; command not retried")
                        break
            phase = self._state["phase"]
            request = self._request
            token = self._token
            cancel = self._cancel.is_set()
        if cancel:
            if not self.execute or not self._identified:
                with self._lock:
                    self._state.update(stop_status="unverified", phase="stopped")
                return
            if not self._stop_sent:
                self._send_stop()
            with self._lock:
                if self._stop_ack_at is not None and all(
                    self._state["telemetry"].get(k) == 0 and
                    self._state["telemetry_received_mono"].get(k, -math.inf) > self._stop_ack_at
                    for k in _ZERO
                ):
                    self._state.update(active=False, zero_confirmed=True, stop_status="verified",
                                       phase="failed" if self._state["fault"] else "stopped")
                    self._stamp("zero_confirmed", now)
                elif now - self._state["timestamps"].get("stop_sent", now) >= STOP_TIMEOUT_S:
                    self._state.update(zero_confirmed=False, stop_status="unverified", phase="failed", fault=True)
                    if self._state["error"] is None:
                        self._state["error"] = "fresh zero inputs and stop ACK not confirmed"
            return
        if not self.execute or not self._used:
            return
        if now - self._heartbeat_at >= HEARTBEAT_S and not self._cancel.is_set():
            self._transport.heartbeat()
            self._heartbeat_at = now
            with self._lock:
                self._stamp("heartbeat_sent", now)
        if phase == "preparing":
            percent, duration = request
            with self._lock:
                self._state["phase"] = "enabling"
            self._send(ENABLE_COMMAND, [1, token, percent, duration, 0, 0, 0], "enable")
        elif phase == "enabled":
            percent, duration = request
            with self._lock:
                self._state["phase"] = "starting"
                self._run_deadline = self._clock() + duration / 1000
                self._state["timestamps"]["run_deadline"] = self._run_deadline
            self._send(ALL_COMMAND, [4, percent, duration, token, 0, 0, 0], "start")


class _SimMessage:
    def __init__(self, kind, **values):
        self.kind = kind
        self.__dict__.update(values)
    def get_type(self): return self.kind
    def get_srcSystem(self): return 1
    def get_srcComponent(self): return 1
    def to_dict(self): return dict(self.__dict__)


class _SimTransport:
    """No hardware imports or I/O; emulates the bench wire contract, not dynamics."""
    def __init__(self, clock):
        self.clock = clock
        self.pending = []
        self.token = 1
        self.value = 0
        self.deadline = -math.inf
        self.last_heartbeat = -math.inf
        self.enabled_token = None
        self.closed = False
    def heartbeat(self): self.last_heartbeat = self.clock()
    def command(self, number, params):
        if number == ENABLE_COMMAND and params == [0] * 7:
            self.value = 0
            self.enabled_token = None
        elif number == ENABLE_COMMAND:
            self.enabled_token = params[1]
            self.token += 1
        elif number == ALL_COMMAND:
            self.value = 48 + math.floor(1999 * params[1] / 100)
            self.deadline = self.clock() + params[2] / 1000
        self.pending.append(_SimMessage("COMMAND_ACK", command=number, result=0))
    def receive(self):
        now = self.clock()
        if now >= self.deadline or now - self.last_heartbeat >= .5:
            self.value = 0
        boot = int(now * 1000) & 0xffffffff
        msgs, self.pending = self.pending, []
        msgs.append(_SimMessage("HEARTBEAT", custom_mode=MODE_ID))
        msgs.append(_SimMessage("ATTITUDE", time_boot_ms=boot, roll=0., pitch=0., yaw=0.))
        vals = dict(_READY, DS_TOKEN=self.token, DS_ACTIVE=int(self.value != 0),
                    **{f"M{i}_DS": self.value for i in range(1, 5)})
        msgs.extend(_SimMessage("NAMED_VALUE_INT", name=k, value=v, time_boot_ms=boot) for k, v in vals.items())
        return msgs
    def close(self): self.closed = True


class SimulatedMotorSession(MotorSession):
    """Same supervisor API, with no serial connection or motor commands."""
    def __init__(self, port="simulation", *, execute=False, _clock=time.monotonic):
        if execute:
            raise ValueError("simulation cannot execute hardware commands")
        super().__init__(port, execute=True, _clock=_clock,
                         _transport_factory=lambda _: _SimTransport(_clock))
        self._state["mode"] = "simulated"

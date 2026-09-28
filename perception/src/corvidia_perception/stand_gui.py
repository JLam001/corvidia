"""Native Jetson mission console. The onboard service owns camera and motor control."""

from __future__ import annotations

import argparse
import collections
import copy
import io
import math
from pathlib import Path
import queue
import threading
import time
from urllib.parse import urlencode

from .mission_prompt import parse_mission_prompt
from .stand_cli import ApiClient, ClientError, DEFAULT_SESSION, SessionRejected, load_session


ACTIVE = {"preparing", "starting", "searching", "confirming", "saving", "stopping"}
TERMINAL = {"complete", "failed", "timed_out", "cancelled"}
READINESS = (
    ("guarded_stand", "The drone is secured in its guarded stand; turning uses the external handle."),
    ("hands_clear", "Hands, people, and loose cables are clear of the motors and propellers."),
    ("power_disconnect_accessible", "The ESC power disconnect is accessible outside the guarded area."),
    ("esc_startup_finished", "ESC startup tones have finished."),
    ("motors_still", "All four motors are still before starting."),
)
GUIDANCE = {"hold": "Hold the stand still", "search_right": "Turn the stand slowly right",
            "search_left": "Turn the stand slowly left", "right": "Turn the stand right",
            "left": "Turn the stand left"}
ARROWS = {"search_right": "  →", "right": "  →", "search_left": "←  ", "left": "←  "}
STATE_LABELS = {"idle": "Ready", "prepared": "Preparing", "preparing": "Preparing", "starting": "Starting",
                "searching": "Searching", "confirming": "Confirming a match", "saving": "Saving capture",
                "stopping": "Stopping", "complete": "Complete", "failed": "Failed",
                "timed_out": "Timed out · no match", "cancelled": "Aborted", "connecting": "Connecting"}
EXAMPLES = ("a person wearing a red shirt",
            "find people wearing a blue polo for 30 seconds",
            "find as many people as possible within 30 seconds")
BG, PANEL, INPUT_BG, BORDER = "#0f1317", "#161b21", "#1e252d", "#2b343e"
TEXT, SOFT, MUTED, MUTED_BG = "#e6ebef", "#c3ccd4", "#8793a0", "#1e252d"
# Rescue orange is the one signal colour; green/amber/red/blue only report state.
ACCENT, GREEN, AMBER, RED, BLUE = "#ff7a1f", "#3fb97a", "#f0b429", "#ef4b55", "#6ba6ff"
# Poll the 30 fps preview at twice its rate: polling at the same rate as the
# camera beats against it, fetching some frames twice and missing others.
# Unchanged frames cost one ~2 ms loopback request. Mission status stays at 5 Hz.
FRAME_INTERVAL_S = 1 / 60
PREVIEW_REDRAW_MS = 10


def _mix(color, base, amount):
    """Blend ``amount`` of ``color`` into ``base`` (both #rrggbb)."""
    a = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(base[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(y + (x - y) * amount):02x}" for x, y in zip(a, b))


# Button palettes: state -> (fill, text[, outline]).
PRIMARY = {"normal": (TEXT, BG), "hover": ("#ffffff", BG), "disabled": (INPUT_BG, "#5c6773", BORDER),
           "focus": ACCENT}
DANGER = {"normal": (RED, "#ffffff"), "hover": ("#f4646d", "#ffffff"), "disabled": (PANEL, "#5c6773", BORDER),
          "focus": "#ffffff"}
CHIP = {"normal": (INPUT_BG, SOFT), "hover": (_mix(ACCENT, INPUT_BG, .14), TEXT), "disabled": (INPUT_BG, MUTED),
        "focus": ACCENT}
START = {"normal": (ACCENT, BG), "hover": ("#ff9142", BG), "disabled": (INPUT_BG, "#5c6773", BORDER),
         "focus": "#ffffff"}
GHOST = {"normal": (PANEL, TEXT, BORDER), "hover": (INPUT_BG, TEXT, BORDER), "disabled": (PANEL, MUTED, BORDER),
         "focus": ACCENT}


def _brand_mark(master):
    """A rounded rescue-orange tile with a search reticle."""
    from PIL import Image, ImageDraw, ImageTk
    size, s = 36, 4
    image = Image.new("RGBA", (size * s, size * s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, size * s - 1, size * s - 1), radius=9 * s, fill=ACCENT)
    c, r = size * s / 2, 9 * s
    draw.ellipse((c - r, c - r, c + r, c + r), outline=BG, width=int(2.4 * s))
    for x0, y0, x1, y1 in ((c, 5 * s, c, 12 * s), (c, size * s - 12 * s, c, size * s - 5 * s),
                           (5 * s, c, 12 * s, c), (size * s - 12 * s, c, size * s - 5 * s, c)):
        draw.line((x0, y0, x1, y1), fill=BG, width=int(2.4 * s))
    draw.ellipse((c - 2 * s, c - 2 * s, c + 2 * s, c + 2 * s), fill=BG)
    return ImageTk.PhotoImage(image.resize((size, size), Image.LANCZOS), master=master)


def fit_image(data, bounds, *, enlarge=False, rounded=None):
    """Decode a JPEG/PNG and fit it inside ``bounds`` as an RGB PIL image.

    Safe off the Tk thread. Uses OpenCV when installed: on the Orin a 720p
    frame fits in ~5 ms with two threads, where PIL's resize alone takes ~13 ms.
    """
    from PIL import Image
    header = Image.open(io.BytesIO(data))  # reads only the header
    width, height = header.size
    if width > 4096 or height > 4096 or width * height > 16_000_000:
        raise ValueError("Image exceeds console display limits")
    scale = min(bounds[0] / width, bounds[1] / height)
    if not enlarge:
        scale = min(scale, 1.0)
    size = (max(1, int(width * scale)), max(1, int(height * scale)))
    cv2 = _cv2()
    if cv2 is not None:
        import numpy as np
        array = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if array is None:
            raise ValueError("Image could not be decoded")
        if size != (width, height):
            # INTER_AREA is 2x slower for the ~0.9x ratios seen here, with no visible gain.
            array = cv2.resize(array, size, interpolation=cv2.INTER_LINEAR)
        array = cv2.cvtColor(array, cv2.COLOR_BGR2RGB)
        if rounded:
            from .stand_widgets import round_corners_array
            round_corners_array(array, *rounded)
        return Image.fromarray(array)
    else:
        image = header.convert("RGB")
        if size != image.size:
            image = image.resize(size, Image.BILINEAR)
    if rounded:
        from .stand_widgets import round_corners
        image = round_corners(image, *rounded)
    return image


_CV2 = []


def _cv2():
    """OpenCV if available, else None. Two threads halve the preview resize; more add little."""
    if not _CV2:
        try:
            import cv2
            cv2.setNumThreads(2)
        except ImportError:
            cv2 = None
        _CV2.append(cv2)
    return _CV2[0]


def description(value):
    return parse_mission_prompt(value).prompt


def collection_text(status):
    """Report saved observations, without claiming unique person identities."""
    if status.get("completion_mode") != "timed_collection":
        return ""
    count = status.get("capture_count", 0)
    count = count if type(count) is int and count >= 0 else 0
    saved = f"{count} capture{'s' if count != 1 else ''} saved"
    if status.get("state") == "complete":
        return "Timed search complete · " + saved
    return "Timed search · " + saved


def telemetry_text(status, *, connected=True):
    """Format reported sensor/link state without implying measured motor speed."""
    def finite(value):
        return type(value) in (int, float) and math.isfinite(value)

    preflight, motor = status.get("preflight", {}), status.get("motor", {})
    frame_age = status.get("perception", {}).get("frame_age_ms")
    camera_fresh = (connected and preflight.get("camera_ready") is True
                    and finite(frame_age) and 0 <= frame_age < 500)
    camera = f"Camera · Live / {frame_age:.0f} ms" if camera_fresh else "Camera · Waiting"
    if not connected:
        camera = "Camera · Disconnected"
    simulated_imu = status.get("telemetry_mode") == "simulated"
    rx_age = preflight.get("telemetry_rx_age_ms")
    link_fresh = (connected and preflight.get("telemetry_connected") is True
                  and finite(rx_age) and 0 <= rx_age < 1500)
    if simulated_imu:
        controller = "STM32 · Simulated"
    elif link_fresh:
        controller = f"STM32 · Live / {rx_age:.0f} ms"
    else:
        controller = "STM32 · Waiting" if connected else "STM32 · Disconnected"
    imu = status.get("imu", {})
    attitude = imu.get("ATTITUDE", {})
    valid_imu = connected and (link_fresh or simulated_imu) and imu.get("IMU_OK") == 1

    def angle(name):
        value = attitude.get(name)
        return f"{math.degrees(value):+.1f}°" if valid_imu and finite(value) else "—"

    axes = f"Roll {angle('roll')} · Pitch {angle('pitch')}\nYaw {angle('yaw')}"
    axes += " · simulated" if simulated_imu else " · sensor frame"
    simulated_motors = status.get("mode") == "observe"
    label = "Sim input" if simulated_motors else "Motor input"
    if not connected or not motor.get("connected") or (not simulated_motors and not link_fresh):
        inputs = f"{label} · Unavailable"
    elif motor.get("zero_confirmed") is True:
        inputs = f"{label} · Zero reported"
    elif motor.get("stop_status") == "unverified":
        inputs = f"{label} · Stop unverified"
    elif motor.get("stop_reason"):
        inputs = f"{label} · Stop requested"
    elif finite(motor.get("requested_percent")):
        inputs = f"{label} · {motor['requested_percent']:g}% requested"
    else:
        inputs = f"{label} · Awaiting telemetry"
    return {"camera": camera, "controller": controller, "attitude": axes, "motors": inputs}


# Measured sensor-to-body axis map (docs/references/control/imu-alignment):
# a 180° turn about Y. Dominant signs only; the precise mount is not calibrated.
SENSOR_TO_BODY = (-1, 1, -1)


def body_attitude(status, *, connected=True):
    """Body-frame (roll, pitch, yaw) in radians from reported IMU telemetry, else None.

    Conventions after the axis map: roll > 0 is right side down, pitch > 0 is
    nose up, yaw > 0 is turned right. Uses the attitude quaternion when present
    (no gimbal-lock ambiguity), otherwise the Euler angles.
    """
    def finite(value):
        return type(value) in (int, float) and math.isfinite(value)

    preflight = status.get("preflight", {})
    simulated = status.get("telemetry_mode") == "simulated"
    rx_age = preflight.get("telemetry_rx_age_ms")
    fresh = (connected and preflight.get("telemetry_connected") is True
             and finite(rx_age) and 0 <= rx_age < 1500)
    imu = status.get("imu", {})
    if not (connected and (fresh or simulated) and imu.get("IMU_OK") == 1):
        return None
    quaternion = imu.get("ATTITUDE_QUATERNION", {})
    q = [quaternion.get(k) for k in ("q1", "q2", "q3", "q4")]
    norm = math.sqrt(sum(v * v for v in q)) if all(finite(v) for v in q) else 0
    if norm > .5:
        w, x, y, z = (v / norm for v in q)
        r = [[1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
             [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
             [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)]]
    else:
        euler = imu.get("ATTITUDE", {})
        roll, pitch, yaw = (euler.get(k) for k in ("roll", "pitch", "yaw"))
        if not all(finite(v) for v in (roll, pitch, yaw)):
            return None
        cr, sr, cp, sp, cy, sy = (math.cos(roll), math.sin(roll), math.cos(pitch),
                                  math.sin(pitch), math.cos(yaw), math.sin(yaw))
        r = [[cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
             [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
             [-sp, cp * sr, cp * cr]]
    m = SENSOR_TO_BODY
    b = [[m[i] * m[j] * r[i][j] for j in range(3)] for i in range(3)]
    return (math.atan2(b[2][1], b[2][2]), math.asin(max(-1., min(1., -b[2][0]))),
            math.atan2(b[1][0], b[0][0]))


def tilt_text(attitude):
    """Plain-language tilt, e.g. 'Left side down 3° · Nose up 12°'."""
    if attitude is None:
        return "No attitude data"
    roll, pitch = (math.degrees(v) for v in attitude[:2])
    parts = []
    if abs(roll) >= .5:
        parts.append(f"{'Right' if roll > 0 else 'Left'} side down {abs(roll):.0f}°")
    if abs(pitch) >= .5:
        parts.append(f"Nose {'up' if pitch > 0 else 'down'} {abs(pitch):.0f}°")
    return " · ".join(parts) or "Level"


# First-time operators: plain words, one thing to do next, jargon behind "details".
STEPS = ("Describe the person", "Safety check", "Drone searches", "See the result")
PLAIN_STATE = {"idle": "Ready", "prepared": "Getting ready", "preparing": "Getting ready",
               "starting": "Starting the motors", "searching": "Searching",
               "confirming": "Checking a possible match", "saving": "Saving the photo",
               "stopping": "Stopping", "complete": "Found a match", "failed": "Something went wrong",
               "timed_out": "Time ran out", "cancelled": "Stopped", "connecting": "Connecting"}
TURN = {"search_right": ("→", "Turn the stand slowly to the right"),
        "right": ("→", "Turn the stand to the right"),
        "search_left": ("←", "Turn the stand slowly to the left"),
        "left": ("←", "Turn the stand to the left")}


def current_step(view):
    """Index into STEPS for the operator's position in the flow."""
    state = view["status"].get("state")
    if view["pending"] or state in ACTIVE:
        return 2
    if view["mission_id"] and state in TERMINAL:
        return 3
    return 0


def plain_banner(view, *, entered, valid, hardware):
    """(icon, headline, detail, tone) answering: what is happening, what do I do?"""
    status = view["status"]
    state = status.get("state", "connecting")
    brief = status.get("prompt") or status.get("appearance")
    if not view["connected"]:
        return ("!", "Lost connection to the drone",
                "Trying to reconnect. The drone keeps doing what it was doing; it does not stop on its own.", "bad")
    if status.get("recovery_required"):
        return ("!", "Check that the motors have stopped",
                "The drone could not confirm the motors stopped. Use the power switch before touching the stand.", "bad")
    if view["uncertain"] or view["pending"]:
        return ("…", "Sending your search to the drone", view["notice"], "active")
    if state in ACTIVE:
        looking = f"Looking for “{brief}”. " if brief else ""
        if state == "confirming":
            return ("?", "Checking a possible match", looking + "Hold the stand still for a moment.", "active")
        if state == "saving":
            return ("■", "Saving the photo", looking + "Hold the stand still.", "active")
        if status.get("motor_rearm"):
            return ("■", "Hold the stand still", "The motors restart every 60 seconds. " + looking, "active")
        arrow, text = TURN.get(status.get("guidance"), ("■", "Hold the stand still"))
        return (arrow, text, looking + "Press Stop search at any time.", "turn" if arrow != "■" else "active")
    if state in TERMINAL and view["mission_id"]:
        after = ("Look at the stand: make sure the motors have stopped before touching it. "
                 if hardware else "")
        detail = {"complete": "The photo is on the right. ",
                  "timed_out": "Nobody matching was found in time. ",
                  "cancelled": "The search was stopped. ",
                  "failed": (status.get("error") or "The search ended early") + ". "}.get(state, "")
        headline = {"complete": "Found a match", "timed_out": "No match found",
                    "cancelled": "Search stopped", "failed": "Something went wrong"}[state]
        tone = {"complete": "good", "failed": "bad"}.get(state, "warn")
        return ("✓" if state == "complete" else "!" if state == "failed" else "•", headline,
                detail + after + "You can start a new search.", tone)
    if view["submission_block_reason"]:
        return ("…", "Getting ready", view["submission_block_reason"], "idle")
    if not entered:
        return ("1", "Ready when you are",
                "Start on the left: describe who the drone should look for, or pick an example.", "idle")
    if valid is False:
        return ("!", "Let's adjust that description", "See the note under the text box.", "warn")
    return ("▶", "Ready to start",
            "Press Start search. You'll confirm five safety checks before the motors spin." if hardware
            else "Press Start search. This is practice mode: the motors stay off.", "idle")


def plain_health(status, *, connected, memory):
    """(tone, sentence) for the one thing a non-expert needs to know about the drone."""
    telemetry = telemetry_text(status, connected=connected)
    minimum = status.get("preflight", {}).get("minimum_memory_mib", 0)
    if not connected:
        return "bad", "Can't reach the drone"
    if status.get("recovery_required") or "unverified" in telemetry["motors"].lower():
        return "bad", "Motors may still be spinning"
    if "Disconnected" in telemetry["controller"] or "Waiting" in telemetry["controller"]:
        return "bad", "Can't reach the flight controller"
    if "Waiting" in telemetry["camera"]:
        return "warn", "Waiting for the camera"
    if not isinstance(memory, (int, float)):
        return "warn", "Checking the drone's computer"
    if memory < minimum:
        return "warn", "The drone's computer is low on memory"
    return "good", "Everything is working"


class ConsoleController:
    """Headless state/network layer. Only explicit submit/abort methods enqueue writes.

    The command and polling workers are independent, so a slow preview cannot
    queue in front of an Abort request. Closing either worker sends no command.
    """

    def __init__(self, api, *, hardware=False, clock=time.monotonic, start=True):
        self.api, self.clock = api, clock
        self.expected_mode = "hardware" if hardware else "observe"
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self.commands = queue.Queue(maxsize=4)
        self.status = {}
        self.connected = False
        self.last_status_at = None
        self.notice = "Connecting to the onboard mission service…"
        self.network_error = None
        self.pending = False
        self.pending_id = None
        self.uncertain = False
        self.session_rejected = False
        self.abort_pending = False
        self.frame = None
        self.frame_version = 0
        # Optional frame_prepare(jpeg) -> display image, run on the frame thread so
        # decoding and scaling never block the Tk main loop.
        self.frame_prepare = None
        self.frame_display = None
        self.evidence = None
        self.evidence_id = None
        self.evidence_key = None
        self.evidence_version = 0
        self.threads = []
        if start:
            self.start()

    def _safe(self, value):
        text = str(value)
        token = getattr(self.api, "token", None)
        return text.replace(token, "[redacted]") if token else text

    def start(self):
        if self.threads:
            raise RuntimeError("Console workers already started")
        if self.closed.is_set():
            raise RuntimeError("A closed console cannot be restarted")
        self.threads = [threading.Thread(target=self._poll_loop, daemon=True, name="console-status"),
                        threading.Thread(target=self._frame_loop, daemon=True, name="console-preview"),
                        threading.Thread(target=self._command_loop, daemon=True, name="console-commands")]
        for thread in self.threads:
            thread.start()

    def _submission_block_reason(self):
        if self.closed.is_set():
            return "This console is closed. Reopen the console to start a mission."
        if not self.connected:
            return "Waiting for the onboard mission service to reconnect."
        fresh = self.last_status_at is not None and 0 <= self.clock() - self.last_status_at < 2
        if not fresh:
            return "Waiting for fresh mission status from the Jetson."
        if self.session_rejected:
            return "Operator session rejected. Reopen the console to reconnect to the current service."
        if self.uncertain:
            return "Submission outcome unknown. Check the mission status or explicitly Abort; this request will not be retried."
        if self.pending:
            return "Waiting for the Jetson to acknowledge the submitted mission."
        if self.abort_pending:
            return "Waiting for the Jetson to acknowledge Abort."
        if self.status.get("mode") != self.expected_mode:
            return (f"Console expects {self.expected_mode}; service is {self.status.get('mode', 'unknown')}. "
                    "Reopen the console for the current service mode.")
        if self.status.get("mission_api") != 2:
            return "This console requires mission API version 2. Update the onboard service."
        if self.status.get("control_authority") != "onboard":
            return "This console requires an onboard mission service."
        if self.status.get("recovery_required"):
            return "Stop is unverified. Check physical stopping and the power disconnect; recover the service before another mission."
        if self.status.get("state") not in TERMINAL | {"idle"}:
            return "Wait for the current mission to finish, or explicitly Abort it."
        # A finished motor session is one-shot and reports ready=False. The
        # supervisor creates a fresh session after accepting the next mission;
        # its preflight checks still decide whether motor authority can start.
        return None

    def _can_submit(self):
        return self._submission_block_reason() is None

    def _reconcile(self):
        # Only an acknowledged ID identifies our submission. Matching text does
        # not: another operator could have submitted the same description.
        if self.pending and self.pending_id and self.status.get("mission_id") == self.pending_id:
            self.pending, self.uncertain = False, False
            self.network_error = None
            self.notice = f"Mission {self.pending_id} is managed by the Jetson."
            self.pending_id = None

    def snapshot(self):
        with self.lock:
            return {"status": copy.deepcopy(self.status), "connected": self.connected,
                    "notice": self.notice, "network_error": self.network_error,
                    "can_submit": self._can_submit(), "submission_block_reason": self._submission_block_reason(),
                    "pending": self.pending,
                    "uncertain": self.uncertain, "mission_id": self.pending_id or self.status.get("mission_id"),
                    "can_abort": bool(self.pending_id or self.status.get("mission_id")) and not self.abort_pending and not self.closed.is_set(),
                    "frame": self.frame, "frame_version": self.frame_version,
                    "frame_display": self.frame_display,
                    "evidence": self.evidence, "evidence_id": self.evidence_id,
                    "evidence_version": self.evidence_version}

    def submit(self, appearance, readiness=None):
        appearance = description(appearance)
        body = {"appearance": appearance}
        if self.expected_mode == "hardware":
            if (not isinstance(readiness, dict) or set(readiness) != {key for key, _ in READINESS}
                    or any(value is not True for value in readiness.values())):
                raise ValueError("Confirm all five hardware readiness observations before submitting.")
            body["readiness"] = dict(readiness)
        elif readiness is not None:
            raise ValueError("Observation mode does not require hardware readiness assertions.")
        with self.lock:
            if not self._can_submit():
                raise ValueError(self._submission_block_reason())
            self.pending = True
            self.pending_id = None
            self.evidence = self.evidence_id = None
            self.evidence_key = None
            self.evidence_version += 1
            self.notice = "Submitting this description once…"
            try:
                self.commands.put_nowait(("mission", body))
            except queue.Full:
                self.pending = False
                raise ValueError("Console command queue is busy; no mission was sent.") from None

    def abort(self):
        with self.lock:
            mission_id = self.pending_id or self.status.get("mission_id")
            if not mission_id or self.abort_pending or self.closed.is_set():
                raise ValueError("No identified mission is available to abort.")
            self.abort_pending = True
            self.notice = "Requesting mission abort…"
            try:
                self.commands.put_nowait(("stop", {"mission_id": mission_id}))
            except queue.Full:
                self.abort_pending = False
                raise ValueError("Abort could not be queued. Check the onboard status.") from None

    def process_command_once(self):
        """Process one explicitly queued operation. Never retry an uncertain write."""
        if self.closed.is_set():
            return False
        try:
            action, body = self.commands.get_nowait()
        except queue.Empty:
            return False
        try:
            result = self.api.post(action, body)
            if result.get("accepted") is not True:
                raise ClientError("The service did not acknowledge this request")
            if action == "mission":
                mission_id = result.get("mission_id")
                if not isinstance(mission_id, str) or not 1 <= len(mission_id) <= 128:
                    raise ClientError("The service response did not identify the mission")
                with self.lock:
                    self.pending_id = mission_id
                    self.notice = f"Mission {mission_id} submitted. The Jetson owns execution."
                    self._reconcile()
            else:
                with self.lock:
                    self.abort_pending = False
                    self.notice = "Abort requested. Waiting for the onboard stop result."
        except Exception as exc:
            with self.lock:
                if isinstance(exc, SessionRejected):
                    self.session_rejected = True
                    if action == "mission":
                        self.pending, self.pending_id, self.uncertain = False, None, False
                        self.notice = "Mission request rejected before execution. Reopen the console to reconnect; it will not be retried."
                    else:
                        self.abort_pending = False
                        self.notice = "Abort rejected. Do not assume motors stopped; check telemetry and the accessible power disconnect."
                elif action == "mission":
                    self.uncertain = True
                    self.notice = "Submission outcome unknown. It will not be retried. Check the mission status or explicitly Abort."
                else:
                    self.abort_pending = False
                    self.notice = "Abort outcome unknown; do not assume motors stopped. Check telemetry and the accessible power disconnect."
                self.network_error = self._safe(exc)
        return True

    def poll_once(self):
        if self.closed.is_set():
            return
        try:
            status = self.api.status()
            if not isinstance(status, dict):
                raise ClientError("Invalid onboard status")
        except Exception as exc:
            with self.lock:
                self.connected = False
                self.network_error = self._safe(exc)
            return
        with self.lock:
            self.status = copy.deepcopy(status)
            self.connected, self.last_status_at = True, self.clock()
            if not self.uncertain:
                self.network_error = None
            mission_id = status.get("mission_id")
            self._reconcile()
            if self.evidence_id is not None and self.evidence_id != mission_id:
                self.evidence = self.evidence_id = self.evidence_key = None
                self.evidence_version += 1
            if status.get("mode") != self.expected_mode:
                self.notice = f"Console expects {self.expected_mode}; service is {status.get('mode', 'unknown')}. Mission submission is disabled."
            elif status.get("control_authority") != "onboard":
                self.notice = "This console requires an onboard mission service. Submission is disabled."
            elif status.get("mission_api") != 2:
                self.notice = "This console requires mission API version 2. Update the onboard service."
            elif status.get("recovery_required"):
                self.notice = "Stop is unverified. Check physical stopping and the power disconnect; recover the service before another mission."
            elif status.get("state") in TERMINAL:
                motor = status.get("motor", {})
                if motor.get("stop_status") == "verified" and motor.get("zero_confirmed") is True:
                    self.notice = ("Mission ended; simulated zero inputs verified." if status.get("mode") == "observe"
                                   else "Mission ended; zero-input telemetry verified. Observe physical motor stopping before approaching.")
                elif motor.get("start_status") == "not_requested":
                    self.notice = "Mission ended before a motor start was reported."
            if self.uncertain:
                self.notice = "Submission outcome unknown. It will not be retried. The current mission status is shown; explicitly Abort if needed."
            evidence_key = (mission_id, status.get("capture_count", 0))
            get_evidence = (bool(status.get("evidence_available")) and bool(mission_id)
                            and evidence_key != self.evidence_key and not self.pending)
        # Image fetching never changes mission authority and may be retried.
        if get_evidence:
            try:
                image = self.api.get_image("/api/evidence?" + urlencode({"mission_id": mission_id}), authenticated=True)
                with self.lock:
                    if not self.pending and self.status.get("mission_id") == mission_id:
                        self.evidence, self.evidence_id = image, mission_id
                        self.evidence_key = evidence_key
                        self.evidence_version += 1
            except Exception as exc:
                with self.lock:
                    self.network_error = "Saved image unavailable: " + self._safe(exc)

    def poll_frame_once(self):
        """Fetch the newest camera preview; return True when it changed."""
        try:
            frame = self.api.get_image("/frame.jpg")
        except Exception:
            return False  # Camera readiness and frame age remain visible in status.
        if frame == self.frame:
            return False
        display = None
        prepare = self.frame_prepare
        if prepare is not None:
            try:
                display = prepare(frame)
            except Exception:
                display = None  # the window falls back to decoding the JPEG itself
        with self.lock:
            self.frame, self.frame_display = frame, display
            self.frame_version += 1
        return True

    def preview(self):
        """(version, jpeg, prepared image) without copying mission status."""
        with self.lock:
            return self.frame_version, self.frame, self.frame_display

    def _frame_loop(self):
        while not self.closed.is_set():
            started = self.clock()
            self.poll_frame_once()
            self.closed.wait(max(0., FRAME_INTERVAL_S - (self.clock() - started)))

    def _poll_loop(self):
        while not self.closed.is_set():
            started = self.clock()
            self.poll_once()
            self.closed.wait(max(0., .2 - (self.clock() - started)))

    def _command_loop(self):
        while not self.closed.is_set():
            if not self.process_command_once():
                self.closed.wait(.02)

    def close(self, *, wait=False):
        """Detach this display. No Stop, cancellation, or keepalive is sent."""
        self.closed.set()
        if wait:
            for thread in self.threads:
                thread.join(.8)


class MissionWindow:
    """Tk widgets only. All HTTP work is performed by ConsoleController workers."""

    def __init__(self, controller, *, root=None):
        import tkinter as tk
        from tkinter import ttk

        self.tk, self.ttk, self.controller = tk, ttk, controller
        self.root = root or tk.Tk()
        self.root.title("Corvidia · Search & Rescue")
        screen_width, screen_height = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.compact = screen_width <= 800 or screen_height <= 600
        self.usable_width = min(1240, screen_width - 80)
        self.usable_height = min(820, screen_height - 90)
        self.root.geometry(f"{self.usable_width}x{self.usable_height}+72+45")
        self.root.minsize(min(520, self.usable_width), min(340, self.usable_height))
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        if not self.compact:
            # Large low-DPI monitors (this stand's reports ~60 dpi) render point
            # sizes unreadably small. Never shrink below the 96 dpi baseline.
            self.root.tk.call("tk", "scaling", max(float(self.root.tk.call("tk", "scaling")), 96 / 72))
        self.last_frame = self.last_evidence = -1
        self.preview_photo = self.result_photo = None
        self.closing = False
        self.local_error = ""
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=PANEL, foreground=TEXT, font=("DejaVu Sans", 11))
        style.configure("Muted.TLabel", foreground=MUTED, font=("DejaVu Sans", 10))
        style.configure("Title.TLabel", background=BG, font=("DejaVu Sans", 22, "bold"))
        style.configure("Subtitle.TLabel", background=BG, foreground=MUTED, font=("DejaVu Sans", 11))
        style.configure("Section.TLabel", foreground=TEXT, font=("DejaVu Sans", 14, "bold"))
        style.configure("Telemetry.TLabel", foreground=SOFT, font=("DejaVu Sans", 10), padding=(0, 3))
        style.configure("Badge.TLabel", background=INPUT_BG, foreground=SOFT, padding=10)
        style.configure("Cue.TLabel", foreground=ACCENT, font=("DejaVu Sans", 20, "bold"))
        style.configure("Find.TButton", background=TEXT, foreground=BG, font=("DejaVu Sans", 13, "bold"), padding=12)
        style.map("Find.TButton", background=[("disabled", INPUT_BG), ("active", "#ffffff")],
                  foreground=[("disabled", "#5c6773")])
        style.configure("Abort.TButton", background=RED, foreground="#ffffff", font=("DejaVu Sans", 12, "bold"), padding=10)
        style.map("Abort.TButton", background=[("disabled", PANEL), ("active", "#f4646d")],
                  foreground=[("disabled", "#5c6773")])
        style.configure("Quiet.TButton", background=INPUT_BG, foreground=SOFT, padding=5)
        style.map("Quiet.TButton", background=[("active", BORDER)])
        style.configure("TCheckbutton", background=PANEL, foreground=TEXT, font=("DejaVu Sans", 11))
        style.configure("TCheckbutton", indicatorsize=18, padding=4)
        style.map("TCheckbutton", background=[("active", PANEL)])

        if self.compact:
            style.configure("TLabel", font=("DejaVu Sans", 9))
            style.configure("Muted.TLabel", font=("DejaVu Sans", 9))
            style.configure("Title.TLabel", font=("DejaVu Sans", 12, "bold"))
            style.configure("Subtitle.TLabel", font=("DejaVu Sans", 8))
            style.configure("Section.TLabel", font=("DejaVu Sans", 10, "bold"))
            style.configure("Telemetry.TLabel", font=("DejaVu Sans", 8), padding=(0, 1))
            style.configure("Badge.TLabel", font=("DejaVu Sans", 8, "bold"), padding=4)
            style.configure("Cue.TLabel", font=("DejaVu Sans", 11, "bold"))
            style.configure("Find.TButton", font=("DejaVu Sans", 10, "bold"), padding=5)
            style.configure("Abort.TButton", font=("DejaVu Sans", 10, "bold"), padding=5)
            style.configure("Quiet.TButton", font=("DejaVu Sans", 9), padding=3)
            style.configure("TCheckbutton", font=("DejaVu Sans", 9))
            self._build_compact()
        else:
            self._build_full()
        self._preview_target = None
        self._drawn = collections.deque(maxlen=120)  # monotonic times of drawn previews
        self.controller.frame_prepare = self._prepare_preview
        self.render()
        self._render_preview()

    def _build_full(self):
        """A first-time operator's page: what is happening, what to do, what the drone sees."""
        from .stand_widgets import AttitudeView, Meter, Pill, RoundButton, Surface, pick_family

        tk = self.tk
        self.sans = pick_family(self.root, "Barlow", "DejaVu Sans")
        self.mono = pick_family(self.root, "IBM Plex Mono", "DejaVu Sans Mono")
        sans = lambda size, weight="normal": (self.sans, size, weight)
        mono = lambda size, weight="normal": (self.mono, size, weight)

        def label(parent, text="", *, font, fg=TEXT, bg=PANEL, anchor="w", **kwargs):
            return tk.Label(parent, text=text, font=font, fg=fg, bg=bg, anchor=anchor, justify="left", **kwargs)

        def button(parent, text, command, palette, *, ground=PANEL, height=48, font=None, anchor="center"):
            return RoundButton(parent, text=text, command=command, palette=palette, ground=ground,
                               height=height, font=font or sans(15, "bold"), anchor=anchor)

        def card(parent, title, **grid):
            surface = Surface(parent, fill=PANEL, ground=BG, radius=16, pad=20, fit=True)
            surface.body.columnconfigure(0, weight=1)
            label(surface.body, title, font=sans(15, "bold")).grid(row=0, column=0, columnspan=3, sticky="w",
                                                                   pady=(0, 10))
            return surface

        self._button = button
        outer = tk.Frame(self.root, bg=BG, padx=22, pady=14)
        outer.pack(fill="both", expand=True)

        # Header: who we are, and the two facts that never change mid-session.
        header = tk.Frame(outer, bg=BG)
        header.pack(fill="x", pady=(0, 12))
        mark = tk.Canvas(header, width=36, height=36, bg=BG, highlightthickness=0)
        mark.pack(side="left")
        self._mark_image = _brand_mark(self.root)
        mark.create_image(0, 0, anchor="nw", image=self._mark_image)
        label(header, "Corvidia drone search", font=sans(19, "bold"), bg=BG).pack(side="left", padx=(12, 0))
        self.badge = Pill(header, font=sans(12, "bold"), ground=BG, height=36, pad=16)
        self.badge.pack(side="right")
        self.connection = Pill(header, font=sans(12, "bold"), ground=BG, height=36, pad=16)
        self.connection.pack(side="right", padx=(0, 10))

        # Where am I in the process?
        steps = tk.Frame(outer, bg=BG)
        steps.pack(fill="x", pady=(0, 12))
        self.step_widgets = []
        for index, name in enumerate(STEPS):
            cell = tk.Frame(steps, bg=BG)
            cell.pack(side="left", padx=(0, 10))
            dot = tk.Canvas(cell, width=30, height=30, bg=BG, highlightthickness=0)
            dot.pack(side="left")
            text = label(cell, name, font=sans(13, "bold"), fg=MUTED, bg=BG)
            text.pack(side="left", padx=(8, 0))
            if index < len(STEPS) - 1:
                tk.Frame(steps, bg=BORDER, height=2, width=48).pack(side="left", padx=(0, 10), pady=14)
            self.step_widgets.append((dot, text))
        self._step_images = {}
        self._shown_step = None

        # What is happening right now, and what should I do? One place, big.
        self.cue_surface = Surface(outer, fill=PANEL, ground=BG, radius=16, pad=16, fit=True)
        self.cue_surface.pack(fill="x", pady=(0, 14))
        cue_row = self.cue_surface.body
        cue_row.columnconfigure(1, weight=1)
        self.cue_icon = label(cue_row, "", font=sans(30, "bold"), fg=ACCENT, width=2, anchor="center")
        self.cue_icon.grid(row=0, column=0, rowspan=2, sticky="ns", padx=(4, 14))
        self.cue = label(cue_row, "Connecting to the drone…", font=sans(24, "bold"))
        self.cue.grid(row=0, column=1, sticky="w")
        self.notice_label = label(cue_row, "", font=sans(14), fg=SOFT, wraplength=1500)
        self.notice_label.grid(row=1, column=1, sticky="w", pady=(2, 0))

        self.closing_note = label(outer, "Closing this window does not stop the drone. To stop it, press Stop search.",
                                  font=sans(11), fg=MUTED, bg=BG)
        self.closing_note.pack(side="bottom", fill="x", pady=(10, 0))
        content = tk.Frame(outer, bg=BG)
        content.pack(fill="both", expand=True)
        content.columnconfigure(1, weight=1)
        content.rowconfigure(0, weight=1)

        # Left: the only place the operator acts.
        rail = Surface(content, fill=PANEL, ground=BG, radius=16, pad=18, width=420)
        rail.grid(row=0, column=0, sticky="ns", padx=(0, 16))
        left = rail.body
        left.columnconfigure(0, weight=1)
        label(left, "Who should the drone look for?", font=sans(18, "bold")).grid(row=0, column=0, sticky="w",
                                                                                  pady=(0, 10))
        self.input_surface = Surface(left, fill=INPUT_BG, outline=BORDER, ground=PANEL, radius=10, pad=12, fit=True)
        self.input_surface.grid(row=2, column=0, sticky="ew")
        self.input = tk.Text(self.input_surface.body, height=2, width=28, wrap="word", bg=INPUT_BG, fg=TEXT,
                             insertbackground=ACCENT, insertwidth=2, relief="flat", bd=0,
                             highlightthickness=0, selectbackground=_mix(ACCENT, INPUT_BG, .35),
                             font=sans(15))
        self.input.pack(fill="x")
        self.input.bind("<FocusIn>", lambda _e: self.input_surface.recolor(outline=ACCENT), add="+")
        self.input.bind("<FocusOut>", lambda _e: self.input_surface.recolor(outline=BORDER), add="+")
        self._bind_prompt()
        self.hint = label(left, font=sans(13), fg=MUTED, wraplength=370)
        self.hint.grid(row=3, column=0, sticky="ew", pady=(6, 12))
        label(left, "Or try one of these:", font=sans(13), fg=MUTED).grid(row=4, column=0, sticky="w", pady=(0, 4))
        examples = tk.Frame(left, bg=PANEL)
        examples.grid(row=5, column=0, sticky="ew", pady=(0, 18))
        examples.columnconfigure(0, weight=1)
        for row, example in enumerate(EXAMPLES):
            button(examples, example, lambda value=example: self._use_example(value), CHIP,
                   height=32, font=sans(12), anchor="w").grid(row=row, column=0, sticky="ew", pady=1)
        self.find = button(left, "Start search", self.submit, START, height=52, font=sans(17, "bold"))
        self.find.grid(row=6, column=0, sticky="ew")
        self.abort_button = button(left, "Stop search", self.abort, DANGER, height=46, font=sans(15, "bold"))
        self.abort_button.grid(row=7, column=0, sticky="ew", pady=(10, 12))
        self.error_label = label(left, font=sans(13, "bold"), fg=RED, wraplength=370)
        self.error_label.grid(row=8, column=0, sticky="ew")
        left.rowconfigure(9, weight=1)

        # Middle: what the drone sees.
        middle = tk.Frame(content, bg=BG)
        middle.grid(row=0, column=1, sticky="nsew")
        middle.columnconfigure(0, weight=1)
        middle.rowconfigure(2, weight=1)
        top = tk.Frame(middle, bg=BG)
        top.grid(row=0, column=0, sticky="ew")
        label(top, "What the drone sees", font=sans(15, "bold"), bg=BG).pack(side="left")
        self.camera_label = label(top, "", font=sans(13), fg=SOFT, bg=BG)
        self.camera_label.pack(side="left", padx=(16, 0))
        self.preview = tk.Label(middle, text="Connecting to the drone's camera…", bg=BG, fg=MUTED,
                                font=sans(14), anchor="n", width=1, height=1)
        self.preview.grid(row=2, column=0, sticky="nsew", pady=(8, 8))
        self.preview.bind("<Configure>", lambda _event: setattr(self, "last_frame", -1))
        # Explain the coloured boxes right above the picture, on a line of their own
        # so a narrow window never hides part of the key.
        legend = tk.Frame(middle, bg=BG)
        legend.grid(row=1, column=0, sticky="w", pady=(4, 0))
        for index, (color, meaning) in enumerate((("#ffd700", "person noticed"), ("#00a0ff", "checking them"),
                                                  ("#00c800", "match"), ("#e60000", "not a match"))):
            tk.Frame(legend, bg=color, width=14, height=14).pack(side="left", padx=(0 if index == 0 else 14, 6))
            label(legend, meaning, font=sans(12), fg=SOFT, bg=BG).pack(side="left")
        self.progress = Meter(middle, track=INPUT_BG, fill=ACCENT, ground=BG, height=8)
        self.progress.grid(row=3, column=0, sticky="ew", pady=(4, 0))

        # Right: the result, and whether the drone is OK.
        side = tk.Frame(content, bg=BG, width=380)
        side.grid(row=0, column=2, sticky="ns", padx=(16, 0))
        side.columnconfigure(0, weight=1, minsize=380)
        self.result_box = card(side, "What the drone found")
        self.result_box.grid(row=0, column=0, sticky="ew")
        self.result_label = label(self.result_box.body, "Nothing yet. When the drone finds the person, "
                                  "their photo appears here.", font=sans(13), fg=MUTED, wraplength=330)
        self.result_label.grid(row=1, column=0, sticky="w")
        self.result_image = tk.Label(self.result_box.body, bg=PANEL, bd=0)
        self.result_image.grid(row=2, column=0, sticky="w", pady=(8, 0))
        # Until there is a photo, this card teaches the whole flow in three lines.
        self.how_it_works = tk.Frame(self.result_box.body, bg=PANEL)
        self.how_it_works.grid(row=3, column=0, sticky="w", pady=(10, 0))
        label(self.how_it_works, "How it works", font=sans(13, "bold"), fg=SOFT).pack(anchor="w", pady=(0, 2))
        for number, text in enumerate(("You describe one person.", "The drone scans with its camera "
                                       "while you turn the stand.", "When it finds them, it saves a photo."),
                                      start=1):
            label(self.how_it_works, f"{number}.  {text}", font=sans(12), fg=MUTED, wraplength=330).pack(
                anchor="w", pady=1)

        health = card(side, "Drone status")
        health.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        body = health.body
        summary = tk.Frame(body, bg=PANEL)
        summary.grid(row=1, column=0, sticky="ew")
        self.health_icon = label(summary, "●", font=sans(16, "bold"), fg=MUTED)
        self.health_icon.pack(side="left")
        self.health_summary = label(summary, "Checking…", font=sans(14, "bold"))
        self.health_summary.pack(side="left", padx=(8, 0))
        self.attitude_view = AttitudeView(body, height=120, colors=dict(
            ground=PANEL, level=BORDER, arm=SOFT, motor=INPUT_BG, rim=MUTED, nose=ACCENT, off=MUTED))
        self.attitude_view.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self.tilt_label = label(body, "No balance data yet", font=sans(14, "bold"))
        self.tilt_label.grid(row=3, column=0, sticky="w")
        label(body, "Orange = front of the drone. The ring shows level.", font=sans(12), fg=MUTED,
              wraplength=330).grid(
            row=4, column=0, sticky="w", pady=(2, 8))
        self.details_toggle = label(body, "Show technical details  ▸", font=sans(12, "bold"), fg=ACCENT,
                                    cursor="hand2")
        self.details_toggle.grid(row=5, column=0, sticky="w")
        self.details_toggle.bind("<Button-1>", lambda _event: self._toggle_details())
        self.details = tk.Frame(body, bg=PANEL)
        self.details.columnconfigure(1, weight=1)
        self.health_rows = {}
        for row, (key, name) in enumerate((("camera", "Camera"), ("controller", "Flight controller"),
                                           ("motors", "Motors"), ("memory", "Onboard memory"),
                                           ("attitude", "Tilt angles")), start=0):
            dot = label(self.details, "●", font=sans(10), fg=MUTED)
            dot.grid(row=row, column=0, sticky="w", pady=2)
            label(self.details, name, font=sans(12), fg=MUTED).grid(row=row, column=1, sticky="w", padx=(8, 0))
            value = label(self.details, "—", font=mono(11))
            value.grid(row=row, column=2, sticky="e")
            self.health_rows[key] = (dot, value)
        self.attitude_label = label(self.details, "", font=sans(11), fg=MUTED)
        self.attitude_label.grid(row=5, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.details_shown = False
        self.telemetry_label = self.health_label = None

    def _toggle_details(self):
        self.details_shown = not self.details_shown
        if self.details_shown:
            self.details.grid(row=6, column=0, sticky="ew", pady=(8, 0))
        else:
            self.details.grid_remove()
        self.details_toggle.configure(text=("Hide" if self.details_shown else "Show") + " technical details  "
                                      + ("▾" if self.details_shown else "▸"))

    def _show_step(self, current):
        from .stand_widgets import rounded_image
        if current == self._shown_step:
            return
        self._shown_step = current
        for index, (dot, text) in enumerate(self.step_widgets):
            done, now = index < current, index == current
            fill = GREEN if done else ACCENT if now else INPUT_BG
            key = (fill,)
            if key not in self._step_images:
                self._step_images[key] = rounded_image(self.root, 30, 30, 15, fill)
            dot.delete("all")
            dot.create_image(0, 0, anchor="nw", image=self._step_images[key])
            dot.create_text(15, 15, text="✓" if done else str(index + 1),
                            fill=BG if (done or now) else MUTED, font=(self.sans, 13, "bold"))
            text.configure(fg=TEXT if now else SOFT if done else MUTED)

    def _build_compact(self):
        """Keep camera, description and both actions visible on a 640×480 desktop."""
        tk, ttk = self.tk, self.ttk
        outer = ttk.Frame(self.root, padding=6)
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 4))
        brand = ttk.Frame(header)
        brand.pack(side="left")
        ttk.Label(brand, text="Corvidia", style="Title.TLabel").pack(side="left")
        ttk.Label(brand, text=" / Search & Rescue", style="Subtitle.TLabel").pack(side="left", padx=(4, 0))
        self.badge = ttk.Label(header, text="Connecting", style="Badge.TLabel")
        self.badge.pack(side="right")
        self.cue = ttk.Label(outer, text="Waiting for camera", style="Cue.TLabel", wraplength=self.usable_width - 20)
        self.cue.pack(fill="x", pady=(0, 5))
        # Reserve the status footer before the expanding camera/body consumes
        # space. It must remain visible when a validation error wraps.
        footer = ttk.Frame(outer)
        footer.pack(side="bottom", fill="x", pady=(3, 0))
        self.error_label = ttk.Label(footer, text="", foreground="#ffb3b9", wraplength=self.usable_width - 20)
        self.error_label.pack(fill="x")
        self.closing_note = ttk.Label(footer, text="Closing the window leaves the onboard mission running.",
                                     style="Muted.TLabel")
        self.closing_note.pack(anchor="w", pady=(2, 0))
        body = ttk.Frame(outer)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=0, minsize=190)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        form = ttk.Frame(body, style="Panel.TFrame", padding=5)
        form.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        form.columnconfigure(0, weight=1)
        ttk.Label(form, text="Mission brief", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        self.input = tk.Text(form, height=1, width=19, wrap="word", bg="#09131f", fg="#eef4fa",
                             insertbackground="#eef4fa", relief="flat", padx=5, pady=3,
                             font=("DejaVu Sans", 10))
        self.input.grid(row=1, column=0, sticky="ew", pady=(3, 5))
        self._bind_prompt()
        self.find = ttk.Button(form, text="Begin search", style="Find.TButton", command=self.submit)
        self.find.grid(row=2, column=0, sticky="ew")
        self.abort_button = ttk.Button(form, text="Abort mission", style="Abort.TButton", command=self.abort)
        self.abort_button.grid(row=3, column=0, sticky="ew", pady=(4, 4))
        self.status_label = ttk.Label(form, text="Connecting…", wraplength=175)
        self.status_label.grid(row=4, column=0, sticky="w", pady=(4, 3))
        self.telemetry_label = ttk.Label(form, text="", style="Telemetry.TLabel", wraplength=175)
        self.telemetry_label.grid(row=5, column=0, sticky="w")
        self.details_button = ttk.Button(form, text="Details", style="Quiet.TButton", command=self._show_details)
        self.details_button.grid(row=6, column=0, sticky="ew", pady=(3, 0))
        camera = ttk.Frame(body, style="Panel.TFrame", padding=4)
        camera.grid(row=0, column=1, sticky="nsew")
        camera.columnconfigure(0, weight=1)
        camera.rowconfigure(0, weight=1)
        # Pixel dimensions come from the containing grid, never the image's
        # natural size, so new frames cannot force the window off screen.
        self.preview = tk.Label(camera, text="Waiting for camera", width=1, height=1,
                                bg="#050a11", fg="#9cb0c5", font=("DejaVu Sans", 9))
        self.preview.grid(row=0, column=0, sticky="nsew")
        self.preview.bind("<Configure>", lambda _event: setattr(self, "last_frame", -1))
        self.camera_label = ttk.Label(camera, text="", style="Muted.TLabel", wraplength=self.usable_width - 230)
        self.camera_label.grid(row=1, column=0, sticky="ew", pady=(3, 0))
        self.details = tk.Toplevel(self.root)
        self.details.title("Corvidia · Mission details")
        self.details.geometry(f"{self.usable_width - 10}x{self.usable_height - 10}+76+45")
        self.details.configure(bg="#142131")
        self.details.transient(self.root)
        self.details.protocol("WM_DELETE_WINDOW", self.details.withdraw)
        self.details.withdraw()
        shell = ttk.Frame(self.details, padding=8)
        shell.pack(fill="both", expand=True)
        ttk.Button(shell, text="Back to camera", command=self.details.withdraw).pack(anchor="w", pady=(0, 6))
        scrolling = ttk.Frame(shell)
        scrolling.pack(fill="both", expand=True)
        canvas = tk.Canvas(scrolling, bg="#142131", highlightthickness=0)
        scroll = ttk.Scrollbar(scrolling, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        content = ttk.Frame(canvas, style="Panel.TFrame", padding=6)
        item = canvas.create_window((0, 0), window=content, anchor="nw")
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(item, width=event.width))
        content.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        wrap = self.usable_width - 60
        self.details_status = ttk.Label(content, text="", wraplength=wrap)
        self.details_status.pack(anchor="w", pady=(0, 12))
        self.health_label = ttk.Label(content, text="", style="Muted.TLabel", wraplength=wrap)
        self.health_label.pack(anchor="w", pady=(0, 12))
        self.notice_label = ttk.Label(content, text="", style="Muted.TLabel", wraplength=wrap)
        self.notice_label.pack(anchor="w", pady=(0, 12))
        self.result_box = ttk.Frame(content, style="Panel.TFrame")
        self.result_box.pack(fill="x")
        self.result_label = ttk.Label(self.result_box, text="Saved capture", style="Muted.TLabel", wraplength=wrap)
        self.result_label.pack(anchor="w")
        self.result_image = tk.Label(self.result_box, bg="#142131")
        self.result_image.pack(anchor="w", pady=(6, 0))
        self.result_box.pack_forget()

    def _show_details(self):
        self.details.deiconify()
        self.details.lift()

    def _bind_prompt(self):
        self.input.bind("<Key>", lambda _event: setattr(self, "local_error", ""))
        self.input.bind("<Return>", self._submit_from_enter)
        self.input.bind("<KP_Enter>", self._submit_from_enter)

    def _submit_from_enter(self, _event=None):
        self.submit()
        return "break"  # consume the key before Tk inserts a newline

    def _use_example(self, value):
        self.input.delete("1.0", "end")
        self.input.insert("1.0", value)
        self.local_error = ""
        self.input.focus_set()

    def _brief_check(self, raw):
        """Explain what a brief will do before it is submitted: (ok, text)."""
        if not raw.strip():
            return None, "Describe them by what they're wearing, like “a person wearing a blue polo”."
        try:
            parsed = parse_mission_prompt(raw)
        except ValueError as exc:
            return False, "✗  " + str(exc)
        if parsed.completion_mode == "timed_collection":
            return True, f"✓  Looks good. Saves a photo of everyone matching for {parsed.duration_ms // 1000} seconds."
        return True, "✓  Looks good. The drone stops when it finds this person."

    def _readiness(self):
        tk, ttk = self.tk, self.ttk
        family = getattr(self, "sans", "DejaVu Sans")
        font = lambda size, weight="normal": (family, size, weight)
        window = tk.Toplevel(self.root)
        window.title("Hardware readiness")
        window.configure(bg=PANEL)
        window.transient(self.root)
        window.grab_set()
        if self.compact:
            window.geometry(f"{self.usable_width}x{self.usable_height}+72+45")
        frame = tk.Frame(window, bg=PANEL, padx=10 if self.compact else 30, pady=10 if self.compact else 26)
        frame.pack(fill="both", expand=True)
        if not self.compact:
            tk.Label(frame, text="HARDWARE MISSION", font=font(10, "bold"), fg=RED, bg=PANEL).pack(anchor="w")
        tk.Label(frame, text="Safety check before motors spin", font=font(11 if self.compact else 20, "bold"),
                 fg=TEXT, bg=PANEL).pack(anchor="w")
        tk.Label(frame, text="Look at the stand. Tick each item only after you have checked it.",
                 font=font(9 if self.compact else 12), fg=MUTED, bg=PANEL).pack(anchor="w", pady=(4, 14))
        style = ttk.Style(self.root)
        style.configure("Ready.TCheckbutton", background=INPUT_BG, foreground=TEXT, font=font(9 if self.compact else 13),
                        indicatorsize=18, indicatorbackground=PANEL, indicatorforeground=BG,
                        upperbordercolor=BORDER, lowerbordercolor=BORDER, padding=(12, 10))
        style.map("Ready.TCheckbutton", background=[("active", _mix(ACCENT, INPUT_BG, .08))],
                  indicatorbackground=[("selected", ACCENT)])
        flags = {}
        answer = [None]
        for number, (key, text) in enumerate(READINESS, start=1):
            flags[key] = tk.BooleanVar(value=False)
            # The whole row is the click target, not just the small box.
            ttk.Checkbutton(frame, variable=flags[key], text=f"{number}   {text}", style="Ready.TCheckbutton").pack(
                fill="x", anchor="w", pady=2 if self.compact else 4)
        counter = tk.Label(frame, text="", font=font(9 if self.compact else 12), fg=MUTED, bg=PANEL)
        counter.pack(anchor="w", pady=(12, 0))

        def accept(_event=None):
            if all(value.get() for value in flags.values()):
                answer[0] = {key: True for key in flags}
                window.destroy()

        row = tk.Frame(frame, bg=PANEL)
        row.pack(fill="x", pady=(16, 0))
        from .stand_widgets import RoundButton
        cancel = RoundButton(row, text="Cancel", command=window.destroy, palette=GHOST, ground=PANEL,
                             height=34 if self.compact else 46, width=120, font=font(10 if self.compact else 13, "bold"))
        cancel.pack(side="left")
        confirm = RoundButton(row, text="Start mission", command=accept, palette=PRIMARY, ground=PANEL,
                              height=34 if self.compact else 46, width=200, font=font(10 if self.compact else 13, "bold"))
        confirm.pack(side="right")

        def update(*_):
            done = sum(value.get() for value in flags.values())
            counter.configure(text=f"{done} of {len(flags)} confirmed", fg=GREEN if done == len(flags) else MUTED)
            confirm.configure(state="normal" if done == len(flags) else "disabled")

        for value in flags.values():
            value.trace_add("write", update)
        update()
        window.bind("<Escape>", lambda _event: window.destroy())
        self.root.wait_window(window)
        return answer[0]

    def submit(self):
        try:
            raw = self.input.get("1.0", "end-1c")
            if not raw.strip():
                return
            view = self.controller.snapshot()
            if not view["can_submit"]:
                self.local_error = view["submission_block_reason"]
                return
            self.local_error = ""
            text = description(raw)
            readiness = self._readiness() if self.controller.expected_mode == "hardware" else None
            if self.controller.expected_mode == "hardware" and readiness is None:
                return
            self.controller.submit(text, readiness)
        except ValueError as exc:
            self.local_error = str(exc)

    def abort(self):
        try:
            self.local_error = ""
            self.controller.abort()
        except ValueError as exc:
            self.local_error = str(exc)

    def _photo(self, data, bounds, *, enlarge=False, rounded=None):
        from PIL import ImageTk
        return ImageTk.PhotoImage(fit_image(data, bounds, enlarge=enlarge, rounded=rounded), master=self.root)

    def _preview_style(self):
        # The full console fills the preview; the compact one never enlarges.
        return dict(enlarge=True, rounded=(14, BG)) if not self.compact else {}

    def _prepare_preview(self, data):
        """Runs on the controller's frame thread: no Tk calls here."""
        bounds = self._preview_target
        if bounds is None:
            return None
        return bounds, fit_image(data, bounds, **self._preview_style())

    def drawn_fps(self):
        """Previews actually drawn in the last second."""
        cutoff = time.monotonic() - 1
        return sum(1 for drawn in self._drawn if drawn >= cutoff)

    def _render_preview(self):
        """Fast loop: only swaps pixels into the existing Tk image (~2 ms)."""
        if self.closing:
            return
        from PIL import ImageTk
        bounds = (max(100, self.preview.winfo_width()), max(100, self.preview.winfo_height()))
        self._preview_target = bounds
        version, frame, prepared = self.controller.preview()
        if frame is not None and version != self.last_frame:
            self.last_frame = version
            try:
                if prepared is None or prepared[0] != bounds:
                    # Resized window or no prepared image yet: decode here once.
                    image = fit_image(frame, bounds, **self._preview_style())
                else:
                    image = prepared[1]
                photo = self.preview_photo
                if photo is not None and (photo.width(), photo.height()) == image.size:
                    photo.paste(image)
                else:
                    self.preview_photo = ImageTk.PhotoImage(image, master=self.root)
                    self.preview.configure(image=self.preview_photo, text="")
                self._drawn.append(time.monotonic())
            except Exception:
                self.preview_photo = None
                self.preview.configure(image="", text="Camera image could not be displayed")
        self.root.after(PREVIEW_REDRAW_MS, self._render_preview)

    def render(self):
        if self.closing:
            return
        view = self.controller.snapshot()
        status = view["status"]
        mode = status.get("mode")
        connected = view["connected"]
        state = status.get("state", "connecting")
        active = state in ACTIVE
        badge = {"observe": "OBSERVE · motors simulated", "hardware": "HARDWARE · live motors",
                 "telemetry": "READ-ONLY TELEMETRY"}.get(mode, "Connecting")
        plain_badge = {"observe": "Practice mode · motors off", "hardware": "Real motors ON",
                       "telemetry": "View only"}.get(mode, "Connecting…")
        raw = self.input.get("1.0", "end-1c")
        entered = bool(raw.strip())
        valid = True
        if not self.compact:
            tint = {"hardware": RED, "observe": BLUE}.get(mode)
            self.badge.set(plain_badge, fg=_mix(tint, TEXT, .35) if tint else SOFT,
                           fill=_mix(tint, BG, .2) if tint else PANEL, dot=tint)
            self.connection.set("Connected to drone" if connected else "Reconnecting…", fg=SOFT, fill=PANEL,
                                dot=GREEN if connected else RED)
            valid, hint = self._brief_check(raw)
            self.hint.configure(text=hint, fg={True: GREEN, False: AMBER}.get(valid, MUTED))
        else:
            self.badge.configure(text=badge.replace(" · live motors", ""))
        self.find.configure(state="normal" if view["can_submit"] and entered and valid is not False else "disabled")
        # A finished mission cannot be aborted, so keep the red button quiet unless
        # something might still be running or its outcome is unknown.
        live = (active or state not in TERMINAL or view["pending"] or view["uncertain"]
                or status.get("recovery_required"))
        self.abort_button.configure(state="normal" if view["can_abort"] and live else "disabled")
        mission = view["mission_id"]
        progress = collection_text(status)
        full_status = f"State: {state.replace('_', ' ')}\nMission: {mission or 'none'}"
        if progress and self.compact:
            full_status += "\n" + progress
        preflight = status.get("preflight", {})
        memory = preflight.get("memory_available_mib")
        memory_label = f"{memory:.0f} MiB free" if isinstance(memory, (int, float)) else "Awaiting measurement"
        telemetry = telemetry_text(status, connected=connected)
        error = self.local_error or view["network_error"] or status.get("error") or preflight.get("resource_error") or ""
        self.error_label.configure(text=error)
        remaining = status.get("remaining_ms")
        guidance = status.get("guidance")
        if self.compact:
            self.status_label.configure(text=f"State: {state.replace('_', ' ')}")
            self.details_status.configure(text=full_status)
            self.details_button.configure(text="Details / saved capture" if view["evidence"] else "Details")
            cue = GUIDANCE.get(guidance, "Hold the stand still")
            self.cue.configure(text=cue if connected else "Disconnected · check onboard mission status")
            self.telemetry_label.configure(text="\n".join(telemetry.values()))
            self.health_label.configure(text=f"Service: {'connected' if connected else 'reconnecting'}\nMemory: {memory_label}")
            self.notice_label.configure(text=view["submission_block_reason"] or view["notice"])
            age = status.get("perception", {}).get("frame_age_ms")
            time_label = f" · {max(0, remaining) / 1000:.0f}s remaining" if isinstance(remaining, (int, float)) and active else ""
            camera_text = f"Latest frame age: {age} ms" if age is not None else "Waiting for camera timing"
            self.camera_label.configure(text=camera_text + time_label + ("\n" + progress if progress else ""))
        else:
            self._render_full(view, status, state, active, mission, progress, telemetry, memory,
                              memory_label, remaining, guidance, entered, valid)
        if view["evidence_version"] != self.last_evidence:
            self.last_evidence = view["evidence_version"]
            label = "Latest capture" if status.get("completion_mode") == "timed_collection" else "Saved capture"
            if view["evidence"] is None:
                if self.compact:
                    self.result_box.pack_forget()
                else:
                    self.how_it_works.grid()
                    self.result_image.configure(image="")
                    self.result_label.configure(text="Nothing yet. When the drone finds the person, "
                                                "their photo appears here.")
            else:
                try:
                    bounds = (420, 190) if self.compact else (330, 140)
                    if not self.compact:
                        self.how_it_works.grid_remove()
                    self.result_photo = self._photo(view["evidence"], bounds, rounded=None if self.compact else (10, PANEL))
                    self.result_image.configure(image=self.result_photo)
                    self.result_label.configure(text=f"{label} · {view['evidence_id']}" if self.compact
                                                else "Photo saved on the drone.")
                except Exception:
                    self.result_label.configure(text="Capture saved; image could not be displayed")
                if self.compact:
                    self.result_box.pack(fill="x")
        self.root.after(100, self.render)

    def _render_full(self, view, status, state, active, mission, progress, telemetry, memory,
                     memory_label, remaining, guidance, entered, valid):
        connected = view["connected"]
        hardware = self.controller.expected_mode == "hardware"
        self._show_step(current_step(view))

        icon, headline, detail, tone = plain_banner(view, entered=entered, valid=valid, hardware=hardware)
        fill = {"turn": _mix(ACCENT, BG, .24), "active": _mix(ACCENT, BG, .1), "good": _mix(GREEN, BG, .16),
                "bad": _mix(RED, BG, .2), "warn": _mix(AMBER, BG, .12)}.get(tone, PANEL)
        accent = {"turn": ACCENT, "active": ACCENT, "good": GREEN, "bad": RED, "warn": AMBER}.get(tone, ACCENT)
        self.cue_surface.recolor(fill=fill)
        self.cue_icon.configure(text=icon, bg=fill, fg=accent)
        self.cue.configure(text=headline, bg=fill, fg=accent if tone == "turn" else TEXT)
        self.notice_label.configure(text=detail, bg=fill)

        duration = status.get("duration_ms")
        if active and isinstance(remaining, (int, float)) and isinstance(duration, (int, float)) and duration > 0:
            self.progress.grid()
            self.progress.set(1 - remaining / duration)
            time_text = f"{max(0, remaining) / 1000:.0f} seconds left"
        else:
            self.progress.grid_remove()
            time_text = "No time limit" if active else ""
        self.camera_label.configure(text=" · ".join(part for part in (time_text, progress) if part))

        tone, sentence = plain_health(status, connected=connected, memory=memory)
        color = {"good": GREEN, "warn": AMBER, "bad": RED}[tone]
        self.health_icon.configure(text={"good": "✓", "warn": "!", "bad": "!"}[tone], fg=color)
        self.health_summary.configure(text=sentence)

        attitude = body_attitude(status, connected=connected)
        self.attitude_view.set(attitude)
        balance = tilt_text(attitude)
        self.tilt_label.configure(text="No balance data yet" if attitude is None else balance,
                                  fg=TEXT if attitude else MUTED)

        def health(text):
            lowered = text.lower()
            if any(word in lowered for word in ("disconnected", "unavailable", "unverified")):
                return RED
            if "simulated" in lowered or "sim input" in lowered:
                return BLUE
            if any(word in lowered for word in ("waiting", "awaiting", "—")):
                return AMBER
            return GREEN

        camera = telemetry["camera"].split(" · ", 1)[-1]
        if camera.startswith("Live"):
            camera = f"{self.drawn_fps()} fps · " + camera.removeprefix("Live / ")
        rows = {"camera": camera, "controller": telemetry["controller"].split(" · ", 1)[-1],
                "motors": telemetry["motors"].split(" · ", 1)[-1]
                + (" (sim)" if telemetry["motors"].startswith("Sim") else ""),
                "memory": memory_label}
        if attitude:
            roll, pitch, yaw = (math.degrees(v) for v in attitude)
            rows["attitude"] = f"R {roll:+.1f}° P {pitch:+.1f}° H {yaw % 360:.0f}°"
            source = "Simulated." if status.get("telemetry_mode") == "simulated" else \
                "Approximate: the sensor mount is not calibrated yet."
            self.attitude_label.configure(text=source)
        else:
            rows["attitude"] = "—"
            self.attitude_label.configure(text="")
        for key, text in rows.items():
            dot, value = self.health_rows[key]
            color = health(telemetry["motors"] if key == "motors" else text)
            if key == "memory":
                minimum = status.get("preflight", {}).get("minimum_memory_mib", 0)
                color = (AMBER if not isinstance(memory, (int, float))
                         else GREEN if memory >= minimum else RED)
            dot.configure(fg=color)
            value.configure(text=text)

    def close(self):
        self.closing = True
        self.controller.close()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=Path, default=DEFAULT_SESSION)
    parser.add_argument("--hardware", action="store_true", help="Connect to a hardware service; each mission still requires readiness confirmation")
    args = parser.parse_args(argv)
    controller = None
    try:
        api = ApiClient(load_session(args.session))
        controller = ConsoleController(api, hardware=args.hardware)
        from .stand_widgets import install_fonts
        install_fonts()
        window = MissionWindow(controller)
        window.run()
        return 0
    except KeyboardInterrupt:
        print("Console closed. Any onboard mission continues; use Abort or the explicit stop command to stop it.")
        return 0
    except Exception as exc:
        message = controller._safe(exc) if controller else str(exc)
        if type(exc).__name__ == "TclError":
            message = "Open this program from a terminal on the Jetson desktop. " + message
        print("Console unavailable: " + message)
        return 1
    finally:
        if controller:
            controller.close()


if __name__ == "__main__":
    raise SystemExit(main())

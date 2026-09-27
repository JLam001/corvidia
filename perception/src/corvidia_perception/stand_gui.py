"""Native Jetson mission console. The onboard service owns camera and motor control."""

from __future__ import annotations

import argparse
import copy
import io
import math
from pathlib import Path
import queue
import threading
import time
from urllib.parse import urlencode

from .mission_prompt import parse_mission_prompt
from .stand_cli import ApiClient, ClientError, DEFAULT_SESSION, load_session


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
        self.abort_pending = False
        self.frame = None
        self.frame_version = 0
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
        self.threads = [threading.Thread(target=self._poll_loop, daemon=True, name="console-preview"),
                        threading.Thread(target=self._command_loop, daemon=True, name="console-commands")]
        for thread in self.threads:
            thread.start()

    def _can_submit(self):
        fresh = self.last_status_at is not None and 0 <= self.clock() - self.last_status_at < 2
        return (self.connected and fresh and not self.closed.is_set() and not self.pending
                and not self.uncertain and not self.abort_pending
                and self.status.get("mode") == self.expected_mode
                and self.status.get("mission_api") == 2
                and self.status.get("control_authority") == "onboard"
                and self.status.get("state") in TERMINAL | {"idle"}
                and not self.status.get("recovery_required"))

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
                    "can_submit": self._can_submit(), "pending": self.pending,
                    "uncertain": self.uncertain, "mission_id": self.pending_id or self.status.get("mission_id"),
                    "can_abort": bool(self.pending_id or self.status.get("mission_id")) and not self.abort_pending and not self.closed.is_set(),
                    "frame": self.frame, "frame_version": self.frame_version,
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
                raise ValueError("Wait for a connected, idle service in the selected mode before submitting.")
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
                if action == "mission":
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
            if status.get("recovery_required"):
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
        try:
            frame = self.api.get_image("/frame.jpg")
            with self.lock:
                self.frame = frame
                self.frame_version += 1
        except Exception:
            pass  # Camera readiness and frame age remain visible in status.
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
        self.root.configure(bg="#0b121c")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.last_frame = self.last_evidence = -1
        self.preview_photo = self.result_photo = None
        self.closing = False
        self.local_error = ""
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background="#0b121c")
        style.configure("Panel.TFrame", background="#142131")
        style.configure("TLabel", background="#142131", foreground="#e9eff6", font=("DejaVu Sans", 11))
        style.configure("Muted.TLabel", foreground="#91a8be", font=("DejaVu Sans", 10))
        style.configure("Title.TLabel", background="#0b121c", font=("DejaVu Sans", 24, "bold"))
        style.configure("Subtitle.TLabel", background="#0b121c", foreground="#6dcfbd", font=("DejaVu Sans", 11))
        style.configure("Section.TLabel", foreground="#d5e6f0", font=("DejaVu Sans", 13, "bold"))
        style.configure("Telemetry.TLabel", foreground="#a9c4d5", font=("DejaVu Sans", 10), padding=(0, 3))
        style.configure("Badge.TLabel", background="#233b49", foreground="#99ecda", padding=10)
        style.configure("Cue.TLabel", foreground="#9ce9d7", font=("DejaVu Sans", 20, "bold"))
        style.configure("Find.TButton", background="#89e0ce", foreground="#102b25", font=("DejaVu Sans", 12, "bold"), padding=11)
        style.map("Find.TButton", background=[("disabled", "#294047"), ("active", "#a9f2e2")],
                  foreground=[("disabled", "#81999d")])
        style.configure("Abort.TButton", background="#a23d4c", foreground="#ffffff", font=("DejaVu Sans", 11, "bold"), padding=10)
        style.map("Abort.TButton", background=[("disabled", "#392a34"), ("active", "#bd4b5b")],
                  foreground=[("disabled", "#a28e96")])
        style.configure("Quiet.TButton", background="#203449", foreground="#c4d8e4", padding=5)
        style.configure("TCheckbutton", background="#142131", foreground="#e9eff6", font=("DejaVu Sans", 11))

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
            self.render()
            return

        outer = ttk.Frame(self.root, padding=24)
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 20))
        brand = ttk.Frame(header)
        brand.pack(side="left")
        ttk.Label(brand, text="Corvidia", style="Title.TLabel").pack(anchor="w")
        ttk.Label(brand, text="Search & Rescue", style="Subtitle.TLabel").pack(anchor="w", pady=(3, 0))
        self.badge = ttk.Label(header, text="Connecting", style="Badge.TLabel")
        self.badge.pack(side="right")
        self.closing_note = ttk.Label(outer, text="Closing this window does not stop the mission. The Jetson owns execution.",
                                      style="Muted.TLabel", wraplength=self.usable_width - 60)
        self.closing_note.pack(side="bottom", fill="x", pady=(12, 0))
        content = ttk.Frame(outer)
        content.pack(fill="both", expand=True)
        content.columnconfigure(1, weight=1)
        content.rowconfigure(0, weight=1)
        left = ttk.Frame(content, style="Panel.TFrame", padding=20, width=320)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 18))
        left.grid_propagate(False)
        left.columnconfigure(0, weight=1)
        ttk.Label(left, text="Mission brief", style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 12))
        self.input = tk.Text(left, height=4, width=29, wrap="word", bg="#09131f", fg="#eef4fa",
                             insertbackground="#eef4fa", relief="flat", padx=10, pady=10,
                             font=("DejaVu Sans", 12))
        self.input.grid(row=2, column=0, sticky="ew")
        self._bind_prompt()
        self.find = ttk.Button(left, text="Begin search", style="Find.TButton", command=self.submit)
        self.find.grid(row=4, column=0, sticky="ew", pady=(14, 0))
        self.abort_button = ttk.Button(left, text="Abort mission", style="Abort.TButton", command=self.abort)
        self.abort_button.grid(row=5, column=0, sticky="ew", pady=(10, 20))
        self.status_label = ttk.Label(left, text="Connecting…", wraplength=280)
        self.status_label.grid(row=6, column=0, sticky="w")
        self.telemetry_label = ttk.Label(left, text="", style="Telemetry.TLabel", wraplength=280)
        self.telemetry_label.grid(row=7, column=0, sticky="w", pady=(14, 0))
        self.health_label = ttk.Label(left, text="", style="Muted.TLabel", wraplength=280)
        self.health_label.grid(row=8, column=0, sticky="w", pady=(10, 14))
        self.notice_label = ttk.Label(left, text="", style="Muted.TLabel", wraplength=280)
        self.notice_label.grid(row=9, column=0, sticky="w")
        self.error_label = ttk.Label(left, text="", foreground="#ffb3b9", wraplength=280)
        self.error_label.grid(row=10, column=0, sticky="w", pady=(12, 0))
        left.rowconfigure(11, weight=1)
        right = ttk.Frame(content, style="Panel.TFrame", padding=18)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        self.cue = ttk.Label(right, text="Waiting for camera", style="Cue.TLabel")
        self.cue.grid(row=0, column=0, sticky="w", pady=(0, 12))
        self.preview = tk.Label(right, text="Connecting to the shared Jetson camera…", bg="#050a11",
                                fg="#9cb0c5", font=("DejaVu Sans", 12), anchor="center")
        self.preview.grid(row=1, column=0, sticky="nsew")
        self.preview.bind("<Configure>", lambda _event: setattr(self, "last_frame", -1))
        self.camera_label = ttk.Label(right, text="", style="Muted.TLabel")
        self.camera_label.grid(row=2, column=0, sticky="w", pady=(8, 12))
        self.result_box = ttk.Frame(right, style="Panel.TFrame")
        self.result_box.grid(row=3, column=0, sticky="ew")
        self.result_label = ttk.Label(self.result_box, text="Saved capture", style="Muted.TLabel")
        self.result_label.pack(anchor="w")
        self.result_image = tk.Label(self.result_box, bg="#142131")
        self.result_image.pack(anchor="w", pady=(6, 0))
        self.result_box.grid_remove()
        self.render()

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

    def _readiness(self):
        tk, ttk = self.tk, self.ttk
        window = tk.Toplevel(self.root)
        window.title("Hardware readiness")
        window.configure(bg="#142131")
        window.transient(self.root)
        window.grab_set()
        if self.compact:
            window.geometry(f"{self.usable_width}x{self.usable_height}+72+45")
        frame = ttk.Frame(window, style="Panel.TFrame", padding=10 if self.compact else 22)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Confirm before this hardware mission", font=("DejaVu Sans", 11 if self.compact else 14, "bold")).pack(anchor="w", pady=(0, 8))
        flags = {}
        answer = [None]
        for key, text in READINESS:
            flags[key] = tk.BooleanVar(value=False)
            row = ttk.Frame(frame, style="Panel.TFrame")
            row.pack(fill="x", pady=3 if self.compact else 6)
            ttk.Checkbutton(row, variable=flags[key]).pack(side="left", anchor="n")
            ttk.Label(row, text=text, wraplength=self.usable_width - 70 if self.compact else 650).pack(side="left", fill="x", expand=True)

        def accept():
            if all(value.get() for value in flags.values()):
                answer[0] = {key: True for key in flags}
                window.destroy()

        row = ttk.Frame(frame, style="Panel.TFrame")
        row.pack(fill="x", pady=(18, 0))
        ttk.Button(row, text="Cancel", command=window.destroy).pack(side="left")
        confirm = ttk.Button(row, text="Submit mission", command=accept, state="disabled")
        confirm.pack(side="right")
        for value in flags.values():
            value.trace_add("write", lambda *_: confirm.configure(state="normal" if all(v.get() for v in flags.values()) else "disabled"))
        self.root.wait_window(window)
        return answer[0]

    def submit(self):
        try:
            raw = self.input.get("1.0", "end-1c")
            if not raw.strip() or not self.controller.snapshot()["can_submit"]:
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

    def _photo(self, data, bounds):
        from PIL import Image, ImageTk
        image = Image.open(io.BytesIO(data))
        if image.width > 4096 or image.height > 4096 or image.width * image.height > 16_000_000:
            raise ValueError("Image exceeds console display limits")
        image.thumbnail(bounds)
        return ImageTk.PhotoImage(image, master=self.root)

    def render(self):
        if self.closing:
            return
        view = self.controller.snapshot()
        status = view["status"]
        mode = status.get("mode")
        self.badge.configure(text={"observe": "OBSERVE · motors simulated", "hardware": "HARDWARE", "telemetry": "READ-ONLY TELEMETRY"}.get(mode, "Connecting"))
        entered = bool(self.input.get("1.0", "end-1c").strip())
        self.find.configure(state="normal" if view["can_submit"] and entered else "disabled")
        self.abort_button.configure(state="normal" if view["can_abort"] else "disabled")
        state = status.get("state", "connecting")
        mission = view["mission_id"]
        full_status = f"State: {state.replace('_', ' ')}\nMission: {mission or 'none'}"
        progress = collection_text(status)
        if progress and self.compact:
            full_status += "\n" + progress
        self.status_label.configure(text=f"State: {state.replace('_', ' ')}" if self.compact else full_status)
        if self.compact:
            self.details_status.configure(text=full_status)
            self.details_button.configure(text="Details / saved capture" if view["evidence"] else "Details")
        cue = GUIDANCE.get(status.get("guidance"), "Hold the stand still")
        self.cue.configure(text=cue if view["connected"] else "Disconnected · check onboard mission status")
        preflight = status.get("preflight", {})
        memory = preflight.get("memory_available_mib")
        memory_label = f"{memory:.0f} MiB available" if isinstance(memory, (int, float)) else "awaiting measurement"
        telemetry = telemetry_text(status, connected=view["connected"])
        self.telemetry_label.configure(text="\n".join(telemetry.values()))
        self.health_label.configure(text=f"Service: {'connected' if view['connected'] else 'reconnecting'}\nMemory: {memory_label}")
        self.notice_label.configure(text=view["notice"])
        self.error_label.configure(text=self.local_error or view["network_error"] or status.get("error") or preflight.get("resource_error") or "")
        age = status.get("perception", {}).get("frame_age_ms")
        remaining = status.get("remaining_ms")
        time_label = f" · {max(0, remaining) / 1000:.0f}s remaining" if isinstance(remaining, (int, float)) and state in ACTIVE else ""
        camera_text = f"Latest frame age: {age} ms" if age is not None else "Waiting for camera timing"
        self.camera_label.configure(text=camera_text + time_label + ("\n" + progress if progress else ""))
        if view["frame"] is not None and view["frame_version"] != self.last_frame:
            self.last_frame = view["frame_version"]
            try:
                size = (max(100, self.preview.winfo_width()), max(100, self.preview.winfo_height()))
                self.preview_photo = self._photo(view["frame"], size)
                self.preview.configure(image=self.preview_photo, text="")
            except Exception:
                self.preview.configure(image="", text="Camera image could not be displayed")
        if view["evidence_version"] != self.last_evidence:
            self.last_evidence = view["evidence_version"]
            if view["evidence"] is None:
                self.result_box.pack_forget() if self.compact else self.result_box.grid_remove()
            else:
                try:
                    self.result_photo = self._photo(view["evidence"], (420, 190))
                    self.result_image.configure(image=self.result_photo)
                    label = "Latest capture" if status.get("completion_mode") == "timed_collection" else "Saved capture"
                    self.result_label.configure(text=f"{label} · {view['evidence_id']}")
                    self.result_box.pack(fill="x") if self.compact else self.result_box.grid()
                except Exception:
                    self.result_label.configure(text="Capture saved; image could not be displayed")
                    self.result_box.pack(fill="x") if self.compact else self.result_box.grid()
        self.root.after(100, self.render)

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

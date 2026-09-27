"""Native console tests never create a camera, serial connection, or Tk display."""

import copy
import math
import os
import threading

import pytest

from corvidia_perception import stand_gui as gui
from corvidia_perception.stand_cli import ApiClient, ClientError, Session
from corvidia_perception.stand_web import StandWebServer


class Api:
    token = "private-test-token"

    def __init__(self, mode="observe"):
        self.state = dict(mode=mode, mission_api=2, control_authority="onboard",
                          state="idle", mission_id=None, appearance=None,
                          preflight={"camera_ready": True}, motor={}, evidence_available=False)
        self.posts, self.images = [], []
        self.post_error = self.status_error = None
        self.post_hook = None

    def status(self):
        if self.status_error:
            raise self.status_error
        return copy.deepcopy(self.state)

    def post(self, action, body):
        self.posts.append((action, copy.deepcopy(body)))
        if action == "mission":
            self.state.update(mission_id="mission-1", state="searching", appearance=body["appearance"])
        if self.post_hook:
            self.post_hook()
        if self.post_error:
            raise self.post_error
        return {"accepted": True, "mission_id": self.state["mission_id"]}

    def get_image(self, path, *, authenticated=False):
        self.images.append((path, authenticated))
        return b"jpeg-preview" if path == "/frame.jpg" else b"jpeg-capture"


def controller(mode="observe", **kwargs):
    api = Api(mode)
    control = gui.ConsoleController(api, hardware=mode == "hardware", start=False, **kwargs)
    control.poll_once()
    return control, api


def test_prompt_is_only_mission_input_and_second_click_cannot_duplicate_submission():
    control, api = controller()
    control.submit("  wearing a red shirt  ")
    with pytest.raises(ValueError):
        control.submit("wearing a red shirt")
    assert api.posts == []  # GUI thread only queues work.
    assert control.process_command_once()
    assert not control.process_command_once()
    assert api.posts == [("mission", {"appearance": "wearing a red shirt"})]
    control.poll_once()
    assert not control.snapshot()["pending"]
    control.close()
    assert len(api.posts) == 1 and api.state["state"] == "searching"


def test_timed_brief_uses_same_single_submission_path_without_timing_overrides():
    control, api = controller()
    brief = "find as many people as possible within 30 seconds"
    control.submit(brief)
    control.process_command_once()
    assert api.posts == [("mission", {"appearance": brief})]


@pytest.mark.parametrize("brief", ["find people for 61 seconds",
                                   "find people wearing a blue polo and glasses for 30 seconds"])
def test_invalid_timed_brief_cannot_queue_a_mission(brief):
    control, api = controller()
    with pytest.raises(ValueError):
        control.submit(brief)
    assert not control.process_command_once() and api.posts == []


@pytest.mark.parametrize("value", ["", " ", "a" * 241, "red\nshirt", "red\x00shirt", 5])
def test_invalid_description_never_queues_a_mission(value):
    control, api = controller()
    with pytest.raises(ValueError):
        control.submit(value)
    assert not control.process_command_once() and api.posts == []


@pytest.mark.parametrize("value", ["blue hat", "blue polo and glasses", "person with a bag"])
def test_unsupported_description_never_queues_or_marks_submission_pending(value):
    control, api = controller()
    with pytest.raises(ValueError):
        control.submit(value)
    assert not control.process_command_once() and api.posts == []
    assert control.snapshot()["pending"] is False


@pytest.mark.parametrize("override", [{"mode": "hardware"}, {"mission_api": 1},
                                     {"control_authority": "client"}, {"state": "unknown"},
                                     {"state": "searching"}, {"recovery_required": True}])
def test_service_mode_version_authority_and_state_must_be_known(override):
    control, api = controller()
    api.state.update(override)
    control.poll_once()
    assert not control.snapshot()["can_submit"]
    with pytest.raises(ValueError):
        control.submit("red shirt")
    assert not api.posts


def test_stale_status_disables_submission_but_not_explicit_abort():
    now = [100.]
    control, api = controller(clock=lambda: now[0])
    api.state.update(mission_id="old", state="complete")
    control.poll_once()
    now[0] += 3
    assert not control.snapshot()["can_submit"]
    control.abort()
    control.process_command_once()
    assert api.posts == [("stop", {"mission_id": "old"})]


def test_hardware_requires_all_five_true_observations_every_submission():
    control, api = controller("hardware")
    flags = {name: True for name, _ in gui.READINESS}
    for invalid in (None, {}, {**flags, "hands_clear": False}, {**flags, "motors_still": 1},
                    {**flags, "extra": True}):
        with pytest.raises(ValueError):
            control.submit("blue polo", invalid)
    control.submit("blue polo", flags)
    control.process_command_once()
    assert api.posts == [("mission", {"appearance": "blue polo", "readiness": flags})]
    api.state["state"] = "complete"
    control.poll_once()
    with pytest.raises(ValueError):
        control.submit("green hat")


def test_observation_does_not_fabricate_physical_readiness_assertions():
    control, api = controller()
    with pytest.raises(ValueError):
        control.submit("red shirt", {name: True for name, _ in gui.READINESS})
    assert not api.posts


def test_ambiguous_submission_never_retries_stops_or_adopts_same_description():
    control, api = controller()
    api.post_error = ClientError("request outcome unknown: " + api.token)
    control.submit("red shirt")
    control.process_command_once()
    # Even if a mission with the exact same description appears, only an
    # acknowledged response ID could establish that it was our submission.
    for _ in range(3):
        control.poll_once()
        control.process_command_once()
    view = control.snapshot()
    assert view["uncertain"] and not view["can_submit"]
    assert "unknown" in view["notice"] and api.token not in view["network_error"]
    assert view["mission_id"] == "mission-1" and view["can_abort"]
    control.close()
    assert api.posts == [("mission", {"appearance": "red shirt"})]


def test_explicit_abort_can_stop_current_displayed_mission_after_ambiguous_submission():
    control, api = controller()
    api.post_error = ClientError("unknown")
    control.submit("red shirt")
    control.process_command_once()
    control.poll_once()
    api.post_error = None
    control.abort()
    control.process_command_once()
    assert api.posts[-1] == ("stop", {"mission_id": "mission-1"})
    assert "Waiting" in control.snapshot()["notice"]
    assert "verified" not in control.snapshot()["notice"]


def test_status_arriving_before_acceptance_response_reconciles_exact_id():
    control, api = controller()
    api.post_hook = control.poll_once
    control.submit("red shirt")
    control.process_command_once()
    assert not control.snapshot()["pending"]
    assert control.snapshot()["mission_id"] == "mission-1"
    assert len(api.posts) == 1


def test_new_mission_from_another_operator_becomes_displayed_abort_target():
    control, api = controller()
    control.submit("red shirt")
    control.process_command_once()
    control.poll_once()
    api.state.update(mission_id="other-operator", appearance="blue shirt")
    control.poll_once()
    control.abort()
    control.process_command_once()
    assert api.posts[-1] == ("stop", {"mission_id": "other-operator"})


def test_disconnect_and_closing_are_read_only_and_do_not_claim_stop():
    control, api = controller()
    api.state.update(state="searching", mission_id="running")
    control.poll_once()
    api.status_error = ClientError("connection lost")
    control.poll_once()
    assert not control.snapshot()["connected"]
    assert not control.snapshot()["can_submit"]
    control.close(wait=True)
    assert not api.posts and api.state["state"] == "searching"


def test_closing_before_command_worker_consumes_request_does_not_submit():
    control, api = controller()
    control.submit("red shirt")
    control.close()
    assert not control.process_command_once() and not api.posts


def test_evidence_uses_authenticated_shared_api_and_zero_does_not_claim_physical_stop():
    control, api = controller("hardware")
    api.state.update(state="complete", mission_id="saved", evidence_available=True,
                     motor={"stop_status": "verified", "zero_confirmed": True})
    control.poll_once()
    assert ("/frame.jpg", False) in api.images
    assert ("/api/evidence?mission_id=saved", True) in api.images
    view = control.snapshot()
    assert view["evidence"] == b"jpeg-capture" and view["evidence_id"] == "saved"
    assert "zero-input telemetry verified" in view["notice"]
    assert "Observe physical" in view["notice"]


def test_collection_fetches_new_captures_during_search_and_caches_each_version():
    control, api = controller()
    api.state.update(state="searching", mission_id="collection", evidence_available=True,
                     completion_mode="timed_collection", capture_count=1)
    control.poll_once()
    assert control.snapshot()["evidence"] == b"jpeg-capture"
    first_version = control.snapshot()["evidence_version"]
    control.poll_once()
    assert control.snapshot()["evidence_version"] == first_version
    api.state["capture_count"] = 2
    control.poll_once()
    assert control.snapshot()["evidence_version"] == first_version + 1
    assert api.images.count(("/api/evidence?mission_id=collection", True)) == 2
    assert api.posts == []  # Captures do not cause the display to stop or restart a mission.


def test_new_external_collection_clears_previous_missions_capture():
    control, api = controller()
    api.state.update(state="complete", mission_id="previous", evidence_available=True, capture_count=1)
    control.poll_once()
    assert control.snapshot()["evidence"] is not None
    api.state.update(state="searching", mission_id="new", completion_mode="timed_collection",
                     evidence_available=False, capture_count=0)
    control.poll_once()
    assert control.snapshot()["evidence"] is None
    assert api.posts == []


@pytest.mark.parametrize("state,count,expected", [
    ("searching", 1, "Timed search · 1 capture saved"),
    ("complete", 2, "Timed search complete · 2 captures saved"),
    ("complete", 0, "Timed search complete · 0 captures saved"),
    ("failed", 2, "Timed search · 2 captures saved"),
])
def test_collection_summary_reports_captures_without_claiming_unique_people(state, count, expected):
    status = dict(completion_mode="timed_collection", state=state, capture_count=count)
    assert gui.collection_text(status) == expected
    assert gui.collection_text({**status, "completion_mode": "first_match"}) == ""


def test_old_evidence_cannot_replace_new_submission_result():
    control, api = controller()
    api.state.update(state="complete", mission_id="old", evidence_available=True)
    original_get = api.get_image

    def delayed_image(path, **kwargs):
        if path.startswith("/api/evidence"):
            control.submit("new target")
        return original_get(path, **kwargs)

    api.get_image = delayed_image
    control.poll_once()
    assert control.snapshot()["evidence"] is None
    assert not api.posts  # Queued only; no implicit start from a poll.


def test_slow_preview_does_not_block_explicit_abort_worker():
    control, api = controller()
    api.state.update(state="searching", mission_id="running")
    control.poll_once()
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
    original_post = api.post

    def slow_image(*_args, **_kwargs):
        entered.set()
        release.wait(2)
        return b"jpeg"

    def post(action, body):
        result = original_post(action, body)
        stopped.set()
        return result

    api.get_image, api.post = slow_image, post
    control.start()
    try:
        assert entered.wait(1)
        control.abort()
        assert stopped.wait(1), "Preview blocked the separate command worker"
        assert api.posts == [("stop", {"mission_id": "running"})]
    finally:
        release.set()
        control.close(wait=True)


def test_controller_and_actual_http_boundary_share_singlemission_contract():
    fake = Api()
    events = []

    def command(event):
        events.append(event)
        return fake.post(event["action"], {key: value for key, value in event.items() if key != "action"})

    server = StandWebServer(port=0, status_fn=fake.status, command_fn=command,
                            preview_fn=lambda: b"preview",
                            evidence_fn=lambda _id: (b"capture", "image/jpeg"))
    control = gui.ConsoleController(ApiClient(Session(f"http://127.0.0.1:{server.port}", server.token)), start=False)
    try:
        control.poll_once()
        control.submit("person wearing a red shirt")
        control.process_command_once()
        control.poll_once()
        assert events == [{"action": "mission", "appearance": "person wearing a red shirt", "readiness": {}}]
        assert control.snapshot()["mission_id"] == "mission-1"
        assert control.snapshot()["frame"] == b"preview"
        control.close()
        assert fake.state["state"] == "searching" and len(events) == 1
    finally:
        control.close()
        server.close()


def test_missing_desktop_display_produces_useful_error_and_no_mutation(monkeypatch, capsys):
    api = Api()
    monkeypatch.setattr(gui, "load_session", lambda _path: object())
    monkeypatch.setattr(gui, "ApiClient", lambda _session: api)
    TclError = type("TclError", (Exception,), {})

    def missing_display(_controller):
        raise TclError("no display name")

    monkeypatch.setattr(gui, "MissionWindow", missing_display)
    assert gui.main([]) == 1
    assert "terminal on the Jetson desktop" in capsys.readouterr().out
    assert api.posts == []


class Prompt:
    def __init__(self, value):
        self.value, self.bindings = value, {}

    def get(self, *_):
        return self.value

    def bind(self, sequence, callback):
        self.bindings[sequence] = callback


def prompt_window(value="person wearing a blue polo", mode="observe", readiness=None):
    control, api = controller(mode)
    window = gui.MissionWindow.__new__(gui.MissionWindow)
    window.controller, window.input, window.local_error = control, Prompt(value), ""
    observations = []

    def confirm():
        observations.append(True)
        return readiness

    window._readiness = confirm
    window._bind_prompt()
    return window, api, observations


@pytest.mark.parametrize("key", ["<Return>", "<KP_Enter>"])
def test_enter_consumes_key_and_submits_exactly_once_through_normal_path(key):
    window, api, observations = prompt_window()
    assert window.input.bindings[key](None) == "break"
    assert window.input.bindings[key](None) == "break"  # pending submission blocks repeats
    assert window.input.value == "person wearing a blue polo"  # no newline inserted
    assert window.controller.process_command_once()
    assert not window.controller.process_command_once()
    assert api.posts == [("mission", {"appearance": window.input.value})]
    assert observations == []


@pytest.mark.parametrize("value,enabled", [("", True), ("  ", True), ("blue polo", False)])
def test_enter_on_empty_or_unavailable_prompt_cannot_open_readiness_or_queue(value, enabled):
    window, api, observations = prompt_window(value, mode="hardware")
    if not enabled:
        api.state["state"] = "searching"
        window.controller.poll_once()
    assert window.input.bindings["<Return>"](None) == "break"
    assert observations == [] and api.posts == []
    assert not window.controller.process_command_once()


def test_enter_requires_same_hardware_readiness_and_valid_description_as_button():
    window, api, observations = prompt_window(mode="hardware")
    assert window.input.bindings["<Return>"](None) == "break"
    assert observations == [True] and not window.controller.process_command_once()
    window.input.value = "blue polo and glasses"
    window.input.bindings["<Return>"](None)
    assert "not supported" in window.local_error
    assert observations == [True]  # invalid description never opens physical readiness
    flags = {name: True for name, _ in gui.READINESS}
    window.input.value = "blue polo"
    window._readiness = lambda: flags
    window.input.bindings["<Return>"](None)
    assert window.controller.process_command_once()
    assert api.posts == [("mission", {"appearance": "blue polo", "readiness": flags})]


def live_telemetry():
    return {"mode": "hardware", "telemetry_mode": "live",
            "perception": {"frame_age_ms": 38.5},
            "preflight": {"camera_ready": True, "telemetry_connected": True,
                          "telemetry_rx_age_ms": 18.2},
            "imu": {"IMU_OK": 1, "ATTITUDE": {"roll": math.pi / 6, "pitch": -math.pi / 12,
                                                "yaw": math.pi / 2}},
            "motor": {"connected": True, "requested_percent": 5., "zero_confirmed": False}}


def test_sidebar_converts_radians_to_sensor_degrees_and_reports_requested_input():
    rows = gui.telemetry_text(live_telemetry())
    assert rows["camera"] == "Camera · Live / 38 ms"
    assert rows["controller"] == "STM32 · Live / 18 ms"
    assert rows["attitude"] == "Roll +30.0° · Pitch -15.0°\nYaw +90.0° · sensor frame"
    assert rows["motors"] == "Motor input · 5% requested"
    text = " ".join(rows.values()).lower()
    assert all(claim not in text for claim in ("rpm", "altitude", "location", "hover"))


@pytest.mark.parametrize("update", [{"IMU_OK": 0}, {"ATTITUDE": {}},
                                     {"ATTITUDE": {"roll": float("nan"), "pitch": True, "yaw": float("inf")}}])
def test_missing_invalid_or_unhealthy_attitude_never_appears_as_zero(update):
    status = live_telemetry()
    status["imu"].update(update)
    rows = gui.telemetry_text(status)
    assert rows["attitude"] == "Roll — · Pitch —\nYaw — · sensor frame"


def test_stale_links_and_disconnection_do_not_show_live_telemetry():
    status = live_telemetry()
    status["preflight"]["telemetry_rx_age_ms"] = 1600.
    status["perception"]["frame_age_ms"] = 501.
    rows = gui.telemetry_text(status)
    assert rows["camera"] == "Camera · Waiting" and rows["controller"] == "STM32 · Waiting"
    assert "30.0" not in rows["attitude"]
    assert rows["motors"] == "Motor input · Unavailable"
    rows = gui.telemetry_text(live_telemetry(), connected=False)
    assert "Disconnected" in rows["camera"] and "Disconnected" in rows["controller"]
    assert rows["motors"] == "Motor input · Unavailable"
    assert "30.0" not in rows["attitude"]


def test_observation_labels_simulated_motors_while_preserving_real_readonly_imu():
    status = live_telemetry()
    status.update(mode="observe", telemetry_mode="live_read_only")
    rows = gui.telemetry_text(status)
    assert rows["controller"].startswith("STM32 · Live")
    assert "sensor frame" in rows["attitude"]
    assert rows["motors"] == "Sim input · 5% requested"
    status["telemetry_mode"] = "simulated"
    rows = gui.telemetry_text(status)
    assert rows["controller"] == "STM32 · Simulated"
    assert "simulated" in rows["attitude"]


def test_zero_report_and_unverified_stop_are_not_claims_of_physical_stopping():
    status = live_telemetry()
    status["motor"]["zero_confirmed"] = True
    assert gui.telemetry_text(status)["motors"] == "Motor input · Zero reported"
    status["motor"].update(zero_confirmed=False, stop_status="unverified", stop_reason="operator stop")
    assert gui.telemetry_text(status)["motors"] == "Motor input · Stop unverified"


@pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="Tk geometry requires an X display (use Xvfb)")
def test_compact_geometry_reserves_error_and_footer_without_hiding_actions():
    tk = pytest.importorskip("tkinter")
    root = tk.Tk()
    root.winfo_screenwidth = lambda: 640
    root.winfo_screenheight = lambda: 480
    control, api = controller("hardware")
    api.state.update(live_telemetry())
    control.poll_once()
    window = gui.MissionWindow(control, root=root)
    try:
        window.input.insert("1.0", "white polo")
        window.local_error = ("Use a person or one upper garment, such as 'a person wearing a blue polo'. "
                              "Extra traits, multiple garments, and negation are not supported.")
        window.render()
        root.update()
        left, top = root.winfo_rootx(), root.winfo_rooty()
        right, bottom = left + root.winfo_width(), top + root.winfo_height()
        assert window.compact and (root.winfo_width(), root.winfo_height()) == (560, 390)
        for widget in (window.input, window.find, window.abort_button, window.telemetry_label,
                       window.error_label, window.closing_note):
            assert widget.winfo_ismapped()
            assert left <= widget.winfo_rootx() < right
            assert top <= widget.winfo_rooty() < bottom
            assert widget.winfo_rootx() + widget.winfo_width() <= right
            assert widget.winfo_rooty() + widget.winfo_height() <= bottom
            parent = widget.master
            while parent is not root:
                assert widget.winfo_rooty() >= parent.winfo_rooty()
                assert widget.winfo_rooty() + widget.winfo_height() <= parent.winfo_rooty() + parent.winfo_height()
                parent = parent.master
        assert window.find.winfo_height() >= 24 and window.abort_button.winfo_height() >= 24
    finally:
        window.close()


@pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="Tk geometry requires an X display (use Xvfb)")
def test_normal_collection_completion_keeps_abort_error_and_footer_visible_with_capture():
    import io
    from PIL import Image

    tk = pytest.importorskip("tkinter")
    root = tk.Tk()
    root.winfo_screenwidth = lambda: 1920
    root.winfo_screenheight = lambda: 1080
    control, api = controller()
    picture = io.BytesIO()
    Image.new("RGB", (640, 480)).save(picture, format="JPEG")
    api.get_image = lambda *_args, **_kwargs: picture.getvalue()
    api.state.update(state="complete", mission_id="a" * 32, completion_mode="timed_collection",
                     capture_count=12, evidence_available=True,
                     motor={"connected": True, "stop_status": "verified", "zero_confirmed": True})
    control.poll_once()
    window = gui.MissionWindow(control, root=root)
    try:
        window.local_error = "Timed collections must last 1–60 whole seconds"
        window.render()
        root.update()
        assert "12 captures saved" in window.camera_label.cget("text")
        for widget in (window.find, window.abort_button, window.error_label,
                       window.closing_note, window.result_box):
            assert widget.winfo_ismapped()
            parent = widget.master
            while True:
                assert widget.winfo_rooty() >= parent.winfo_rooty()
                assert widget.winfo_rooty() + widget.winfo_height() <= parent.winfo_rooty() + parent.winfo_height()
                if parent is root:
                    break
                parent = parent.master
    finally:
        window.close()

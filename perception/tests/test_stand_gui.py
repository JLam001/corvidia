"""Native console tests never create a camera, serial connection, or Tk display."""

import copy
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


@pytest.mark.parametrize("value", ["", " ", "a" * 241, "red\nshirt", "red\x00shirt", 5])
def test_invalid_description_never_queues_a_mission(value):
    control, api = controller()
    with pytest.raises(ValueError):
        control.submit(value)
    assert not control.process_command_once() and api.posts == []


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
            control.submit("blue hat", invalid)
    control.submit("blue hat", flags)
    control.process_command_once()
    assert api.posts == [("mission", {"appearance": "blue hat", "readiness": flags})]
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

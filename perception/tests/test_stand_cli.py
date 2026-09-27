"""Terminal mission tests use a fake HTTP boundary, never serial or a model."""

import copy
import json
import queue
from pathlib import Path
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from corvidia_perception import stand_cli as cli


class Clock:
    def __init__(self):
        self.t = 100.

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        assert seconds >= 0
        self.t += seconds


class Input:
    def __init__(self, tty=False):
        self.tty = tty

    def isatty(self):
        return self.tty


class Api:
    token = "private-test-token"

    def __init__(self, *, mode="observe"):
        self.state = dict(mode=mode, state="idle", mission_id=None, guidance="hold",
                          control_authority="onboard", mission_owner="jetson", lease_required=False, mission_api=2,
                          preflight=dict(camera_ready=True, motor_ready=True, resource_ready=True,
                                         telemetry_ready=True),
                          motor=dict(start_status="not_requested", stop_status="not_requested",
                                     zero_confirmed=True), result=None)
        self.posts = []
        self.status_calls = 0
        self.start_actual = False
        self.error_after = None
        self.active_error = None
        self.pending_reads = 0
        self.unverified_stop = False

    def status(self):
        self.status_calls += 1
        if self.start_actual and self.active_error is not None:
            error, self.active_error = self.active_error, None
            raise error
        if self.pending_reads > 0:
            self.pending_reads -= 1
            self.state["state"] = "searching" if self.pending_reads == 0 else "starting"
            self.state["motor"]["start_status"] = "accepted" if self.pending_reads == 0 else "not_requested"
        return copy.deepcopy(self.state)

    def post(self, action, body):
        self.posts.append((action, copy.deepcopy(body)))
        if action == "mission":
            assert set(body) <= {"appearance", "readiness"}
            self.start_actual = True
            self.state.update(appearance=body["appearance"], mission_id="new-mission", state="searching",
                              percent=5., duration_ms=60000)
            self.state["motor"].update(start_status="accepted", zero_confirmed=False)
        elif action == "stop":
            self.state["state"] = "cancelled"
            if self.start_actual:
                self.state["motor"].update(stop_status="unverified" if self.unverified_stop else "verified",
                                           zero_confirmed=not self.unverified_stop)
        else:
            raise AssertionError("Client must not send prepare/start/lease or power overrides")
        if action == self.error_after:
            self.error_after = None
            raise cli.ClientError("ambiguous network timeout")
        return {"accepted": True, "mission_id": self.state["mission_id"]}


def runner(api=None, **kwargs):
    api = api or Api()
    clock, output = Clock(), []
    control = cli.TerminalMission(api, clock=clock, sleep=clock.sleep, out=output.append,
                                 stop_timeout=1, **kwargs)
    return control, api, clock, output


def test_success_submits_one_prompt_and_returns_while_onboard_mission_is_active():
    control, api, clock, output = runner()
    assert control.run("red shirt") == 0
    assert api.posts == [("mission", {"appearance": "red shirt"})]
    assert control.mission_id == "new-mission"
    assert api.state["state"] == "searching" and clock() == 100.
    assert not api.state["motor"]["zero_confirmed"]
    assert any("submitted to the Jetson" in row for row in output)
    assert any("independently" in row for row in output)
    assert all(api.token not in row for row in output)


def test_timed_brief_is_validated_and_submitted_once_without_execution_overrides():
    control, api, _, _ = runner()
    brief = "find as many people as possible within 30 seconds"
    assert control.run(brief) == 0
    assert api.posts == [("mission", {"appearance": brief})]


@pytest.mark.parametrize("brief", ["find people for 61 seconds",
                                   "find people with glasses for 30 seconds"])
def test_invalid_timed_brief_does_not_contact_supervisor(brief):
    control, api, _, _ = runner()
    assert control.run(brief) == 1
    assert api.status_calls == 0 and not api.posts


def test_collection_progress_updates_for_new_capture_with_same_countdown_second():
    control, api, _, output = runner()
    api.state.update(completion_mode="timed_collection", state="searching", remaining_ms=8000,
                     capture_count=1)
    control._display(api.state)
    api.state["capture_count"] = 2
    control._display(api.state)
    control._display(api.state)
    assert len(output) == 2
    assert "8.0s remaining · 1 capture saved" in output[0]
    assert "2 captures saved" in output[1]


@pytest.mark.parametrize("count", [0, 2])
def test_collection_completion_summarizes_saved_captures_after_verified_stop(count):
    control, api, _, output = runner()
    api.state.update(completion_mode="timed_collection", state="complete", capture_count=count,
                     motor={"stop_status": "verified", "zero_confirmed": True})
    assert control._result(api.state) == 0
    assert output[-1] == f"Timed search complete: {count} captures saved."


@pytest.mark.parametrize("mode,hardware", [("hardware", False), ("observe", True), ("telemetry", False)])
def test_mode_mismatch_never_submits(mode, hardware):
    control, api, _, output = runner(Api(mode=mode), stdin=Input(True))
    assert control.run("red shirt", hardware=hardware) == 1
    assert api.posts == []
    assert any("Expected" in row for row in output)


@pytest.mark.parametrize("appearance", [None, "", " ", "x" * 241, "red\nshirt", "red\x00shirt"])
def test_invalid_description_never_contacts_supervisor(appearance):
    control, api, _, _ = runner()
    assert control.run(appearance) == 1
    assert api.status_calls == 0 and not api.posts


@pytest.mark.parametrize("appearance", ["blue polo and glasses", "person holding a bag", "blue hat"])
def test_cli_explains_unsupported_traits_without_contacting_supervisor(appearance):
    control, api, _, output = runner()
    assert control.run(appearance) == 1
    assert api.status_calls == 0 and not api.posts
    assert any("not supported" in row and "polo" in row for row in output)


@pytest.mark.parametrize("args", [["--percent", "5"], ["--seconds", "10"]])
def test_cli_has_no_power_or_duration_options(args):
    with pytest.raises(SystemExit) as exc:
        cli.main(["run", "red shirt", *args])
    assert exc.value.code == 2


def test_existing_active_mission_is_not_adopted():
    api = Api()
    api.state.update(state="searching", mission_id="someone-else")
    control, api, _, _ = runner(api)
    assert control.run("red shirt") == 1
    assert api.posts == []


def test_ambiguous_submission_is_never_retried_adopted_or_stopped():
    api = Api()
    api.error_after = "mission"
    control, api, _, output = runner(api)
    assert control.run("red shirt") == 1
    assert api.posts == [("mission", {"appearance": "red shirt"})]
    assert control.mission_id is None
    assert api.state["state"] == "searching"
    assert any("outcome unknown" in row for row in output)
    assert any("No retry or Stop" in row for row in output)


@pytest.mark.parametrize("error", [KeyboardInterrupt(), cli.Cancelled(), cli.ClientError("connection failed")])
def test_submission_interruption_or_connection_failure_has_no_authority(error):
    api = Api()
    original = api.post

    def uncertain(action, body):
        original(action, body)
        raise error

    api.post = uncertain
    control, api, _, _ = runner(api)
    code = control.run("red shirt")
    assert code == (1 if isinstance(error, cli.ClientError) else 130)
    assert [action for action, _ in api.posts] == ["mission"]
    assert api.state["state"] == "searching" and control.cleaning
    assert control.mission_id is None


def test_valid_receipt_returns_while_server_is_preparing_without_waiting_for_motor_ack():
    api = Api()
    original = api.post

    def queued(action, body):
        reply = original(action, body)
        api.state.update(state="preparing")
        api.state["motor"]["start_status"] = "not_requested"
        api.active_error = cli.ClientError("Any further status read would fail")
        return reply

    api.post = queued
    control, api, clock, output = runner(api)
    assert control.run("red shirt") == 0
    assert clock() == 100.0 and len(api.posts) == 1 and api.status_calls == 2
    assert api.state["motor"]["start_status"] == "not_requested"
    assert any("does not confirm motor motion" in row for row in output)


@pytest.mark.parametrize("reply", [{"accepted": True}, {"accepted": False, "mission_id": "new"},
                                   {"accepted": True, "mission_id": "old"}])
def test_missing_or_reused_receipt_identity_is_not_adopted_or_stopped(reply):
    api = Api()
    api.state.update(mission_id="old", state="complete")
    original = api.post

    def malformed(action, body):
        original(action, body)
        return reply

    api.post = malformed
    control, api, _, _ = runner(api)
    assert control.run("red shirt") == 1
    assert control.mission_id is None
    assert [action for action, _ in api.posts] == ["mission"]


@pytest.mark.parametrize("field", ["camera_ready", "motor_ready", "resource_ready", "telemetry_ready"])
def test_preflight_belongs_to_server_including_replacing_a_consumed_motor_session(field):
    api = Api()
    api.state["preflight"][field] = False
    control, api, _, _ = runner(api)
    assert control.run("red shirt") == 0
    assert api.posts == [("mission", {"appearance": "red shirt"})]


def test_recovery_requirement_blocks_submission():
    api = Api()
    api.state["recovery_required"] = True
    control, api, _, _ = runner(api)
    assert control.run("red shirt") == 1 and not api.posts


def test_changed_mission_during_readiness_is_never_replaced_or_cancelled():
    api = Api(mode="hardware")

    def answer(*_):
        api.state.update(state="searching", mission_id="another-operator")
        return "yes"

    control, api, _, _ = runner(api, stdin=Input(True), readiness_reader=answer)
    assert control.run("red shirt", hardware=True) == 1
    assert not api.posts and api.state["mission_id"] == "another-operator"


def test_hardware_cannot_take_piped_readiness():
    control, api, _, output = runner(Api(mode="hardware"), stdin=Input(False))
    assert control.run("red shirt", hardware=True) == 1
    assert api.posts == []
    assert any("interactive terminal" in row for row in output)


def test_hardware_requires_five_exact_yes_answers_in_single_mission_request():
    prompts = []

    def yes(prompt, _deadline):
        prompts.append(prompt)
        return "yes"

    control, api, _, _ = runner(Api(mode="hardware"), stdin=Input(True), readiness_reader=yes)
    assert control.run("red shirt", hardware=True) == 0
    assert len(prompts) == 5
    assert api.posts == [("mission", {"appearance": "red shirt", "readiness": {key: True for key, _ in cli.READINESS}})]


@pytest.mark.parametrize("answer", ["no", "YES", " yes", "", "true"])
def test_readiness_denial_performs_no_mutation(answer):
    control, api, _, _ = runner(Api(mode="hardware"), stdin=Input(True), readiness_reader=lambda *_: answer)
    assert control.run("red shirt", hardware=True) == 1
    assert not api.posts and not api.start_actual


def test_stop_without_fresh_zero_never_claims_verified():
    api = Api()
    api.unverified_stop = api.start_actual = True
    api.state.update(mission_id="active", state="searching")
    control, api, _, output = runner(api)
    with pytest.raises(cli.ClientError, match="STOP UNVERIFIED"):
        control.stop_current()
    assert not any("zero inputs verified" in row or "zero-input telemetry verified" in row for row in output)


def test_explicit_stop_verifies_zero_on_an_active_mission():
    api = Api()
    api.start_actual = True
    api.state.update(mission_id="active", state="searching")
    api.state["motor"].update(start_status="accepted", zero_confirmed=False)
    control, api, _, output = runner(api)
    assert control.stop_current() == 0
    assert [action for action, _ in api.posts] == ["stop"]
    assert api.state["motor"]["zero_confirmed"]
    assert any("Simulated zero inputs verified" in row for row in output)


@pytest.mark.parametrize("error", [KeyboardInterrupt(), cli.Cancelled(), cli.ClientError("connection lost")])
def test_watch_interrupt_or_disconnect_never_changes_authority(error):
    api = Api()
    api.start_actual = True
    api.state.update(mission_id="active", state="searching")
    api.active_error = error
    control, api, _, output = runner(api)
    assert control.watch() == (1 if isinstance(error, cli.ClientError) else 130)
    assert api.posts == [] and api.state["state"] == "searching"
    assert any("Jetson" in line for line in output)


def test_watch_shows_completed_evidence_without_writing():
    api = Api()
    api.state.update(mission_id="done", state="complete", result={"evidence": {"path": "/tmp/capture"}})
    api.state["motor"].update(start_status="accepted", stop_status="verified", zero_confirmed=True)
    control, api, _, output = runner(api)
    assert control.watch() == 0 and api.posts == []
    assert any("Saved evidence: /tmp/capture" in line for line in output)


@pytest.mark.parametrize("old", [{"control_authority": "client", "lease_required": True}, {"mission_api": 1}])
def test_legacy_service_is_rejected_before_submission(old):
    api = Api()
    api.state.update(old)
    control, api, _, _ = runner(api)
    assert control.run("red shirt") == 1 and not api.posts


def test_status_command_is_read_only_and_redacts_token(monkeypatch, capsys):
    api = Api()
    api.state["diagnostic"] = api.token
    monkeypatch.setattr(cli, "load_session", lambda *a, **kw: cli.Session())
    monkeypatch.setattr(cli, "ApiClient", lambda _session: api)
    assert cli.main(["status", "--json"]) == 0
    assert api.status_calls == 1 and api.posts == []
    assert api.token not in capsys.readouterr().out


def test_human_status_is_concise_and_read_only(monkeypatch, capsys):
    api = Api()
    monkeypatch.setattr(cli, "load_session", lambda *a, **kw: cli.Session())
    monkeypatch.setattr(cli, "ApiClient", lambda _session: api)
    assert cli.main(["status"]) == 0
    output = capsys.readouterr().out
    assert "Mode: observe" in output and "Camera:" in output
    assert len(output.splitlines()) <= 7 and api.posts == []


@pytest.mark.parametrize("url", ["http://192.168.2.2:8080", "https://localhost:8080",
                                  "http://user:secret@localhost:8080", "http://localhost:8080/?token=secret",
                                  "http://localhost:8080/other", "http://localhost:8080/#secret"])
def test_unsafe_session_urls_are_rejected(url):
    with pytest.raises(cli.ClientError, match="loopback"):
        cli.ApiClient(cli.Session(url, "secret"))


def test_private_session_file_and_read_only_missing_fallback(tmp_path):
    path = tmp_path / "session.json"
    assert cli.load_session(path, required=False) == cli.Session()
    path.write_text(json.dumps({"version": 1, "url": cli.DEFAULT_URL, "token": "secret", "pid": 123}))
    path.chmod(0o600)
    assert cli.load_session(path).token == "secret"
    assert "secret" not in repr(cli.load_session(path))
    path.chmod(0o644)
    with pytest.raises(cli.ClientError, match="0600"):
        cli.load_session(path)


def test_redirects_are_not_followed():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            received.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/redirect-target")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        api = cli.ApiClient(cli.Session(f"http://127.0.0.1:{server.server_port}", "secret"))
        with pytest.raises(cli.ClientError, match="redirect"):
            api.status()
        assert received == ["/api/status"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(1)


@pytest.mark.parametrize("mime,body,authenticated,error", [
    ("image/jpeg", b"test-jpeg", False, None),
    ("image/png", b"test-png", True, None),
    ("text/html", b"not an image", False, "JPEG or PNG"),
    ("image/jpeg", b"", False, "empty or too large"),
    ("image/jpeg", b"x" * (10 * 1024 * 1024 + 1), False, "empty or too large"),
])
def test_image_client_auth_mime_and_size_bound(mime, body, authenticated, error):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            received.append((self.path, self.headers.get("X-Corvidia-Token")))
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        api = cli.ApiClient(cli.Session(f"http://127.0.0.1:{server.server_port}", "image-token"))
        if error:
            with pytest.raises(cli.ClientError, match=error):
                api.get_image("/frame.jpg", authenticated=authenticated)
        else:
            assert api.get_image("/frame.jpg", authenticated=authenticated) == body
        assert received == [("/frame.jpg", "image-token" if authenticated else None)]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(1)


def test_cli_http_server_supervisor_contract_without_hardware():
    from corvidia_perception.stand import StandSupervisor
    from corvidia_perception.stand_web import StandWebServer

    class Motor:
        def __init__(self):
            self.starts = 0
            self.state = dict(ready=True, start_status="not_requested", stop_status="not_requested",
                              zero_confirmed=True, fault=False, error=None)

        def start_background(self): pass
        def snapshot(self): return dict(self.state)
        def refresh_lease(self): pass
        def close(self): pass

        def start_all(self, percent, duration_ms):
            assert percent == 5 and duration_ms == 60000
            self.starts += 1
            self.state.update(start_status="accepted", zero_confirmed=False)

        def stop(self, reason):
            self.state.update(stop_status="verified", zero_confirmed=True)

    motor = Motor()
    supervisor = StandSupervisor(lambda: motor, queue.Queue())
    supervisor.memory_available_mib = 2500
    supervisor.resource_checked_mono = time.monotonic()
    now = time.monotonic()
    supervisor.event(dict(type="ready", capture_mono=now, processed_mono=now, source_epoch=0))
    supervisor.tick()
    received = []

    def command(event):
        received.append(copy.deepcopy(event))
        return supervisor.submit(event)

    server = StandWebServer(port=0, status_fn=supervisor.snapshot, command_fn=command)
    done = threading.Event()
    permit_completion = threading.Event()

    def pump():
        completed = False
        while not done.is_set():
            now = time.monotonic()
            supervisor.event(dict(type="progress", capture_mono=now, processed_mono=now,
                                  source_epoch=0, mission_id=supervisor.spec.mission_id if supervisor.spec else None))
            supervisor.tick()
            if not completed and permit_completion.is_set():
                supervisor.event(dict(type="completion", mission_id=supervisor.spec.mission_id,
                                      result="confirmed", committed=True, capture_mono=now,
                                      commit_mono=now, source_epoch=0, path="/tmp/synthetic-evidence"))
                completed = True
            done.wait(.01)

    thread = threading.Thread(target=pump, daemon=True)
    thread.start()
    output = []
    try:
        api = cli.ApiClient(cli.Session(f"http://127.0.0.1:{server.port}", server.token))
        control = cli.TerminalMission(api, out=output.append)
        assert control.run("red shirt") == 0, output
        deadline = time.monotonic() + 1
        while supervisor.snapshot()["state"] != "searching" and time.monotonic() < deadline:
            time.sleep(.01)
        assert motor.starts == 1
        starts = [event for event in received if event["action"] == "mission"]
        assert len(starts) == 1
        assert [event["action"] for event in received] == ["mission"]
        assert supervisor.snapshot()["mission_owner"] == "jetson"
        assert supervisor.snapshot()["state"] == "searching"
        assert not supervisor.snapshot()["motor"]["zero_confirmed"]
        # The caller has returned and sends no HTTP activity for longer than
        # the former client watchdog. The onboard mission must remain active.
        time.sleep(1.2)
        assert supervisor.snapshot()["state"] == "searching"
        permit_completion.set()
        assert control.watch() == 0, output
        assert supervisor.snapshot()["motor"]["zero_confirmed"]
        assert [event["action"] for event in received] == ["mission"]
    finally:
        done.set()
        thread.join(1)
        server.close()
        supervisor.close()

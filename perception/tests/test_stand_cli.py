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

    def __init__(self, *, mode="observe", finish_after=2):
        self.state = dict(mode=mode, state="idle", mission_id=None, guidance="hold",
                          preflight=dict(camera_ready=True, motor_ready=True, resource_ready=True,
                                         telemetry_ready=True),
                          motor=dict(start_status="not_requested", stop_status="not_requested",
                                     zero_confirmed=True), result=None)
        self.posts = []
        self.status_calls = 0
        self.leases = 0
        self.finish_after = finish_after
        self.start_actual = False
        self.prepare_noop = False
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
            if self.pending_reads == 0:
                self.state["state"] = "searching"
            else:
                self.state["state"] = "prepared"
        return copy.deepcopy(self.state)

    def post(self, action, body):
        self.posts.append((action, copy.deepcopy(body)))
        if action == "prepare" and not self.prepare_noop:
            self.state.update(**body, mission_id="new-mission", state="prepared")
        elif action == "start":
            self.start_actual = True
            self.state.update(state="searching")
            self.state["motor"].update(start_status="accepted", zero_confirmed=False)
        elif action == "lease":
            assert self.state["state"] in cli.ACTIVE
            self.leases += 1
            if self.finish_after is not None and self.leases >= self.finish_after:
                self.state.update(state="complete", result={"evidence": {"path": "/tmp/saved-capture"}})
                self.state["motor"].update(stop_status="verified", zero_confirmed=True)
        elif action == "stop":
            self.state["state"] = "cancelled"
            if self.start_actual:
                self.state["motor"].update(stop_status="unverified" if self.unverified_stop else "verified",
                                           zero_confirmed=not self.unverified_stop)
        if action == self.error_after:
            self.error_after = None
            raise cli.ClientError("ambiguous network timeout")
        return {"accepted": True}


def runner(api=None, **kwargs):
    api = api or Api()
    clock, output = Clock(), []
    control = cli.TerminalMission(api, clock=clock, sleep=clock.sleep, out=output.append,
                                 prepare_timeout=1, stop_timeout=1, **kwargs)
    return control, api, clock, output


def test_success_uses_new_prepared_mission_and_one_start():
    control, api, _, output = runner()
    assert control.run("red shirt", 5, 10) == 0
    assert [action for action, _ in api.posts].count("prepare") == 1
    assert [action for action, _ in api.posts].count("start") == 1
    start = next(body for action, body in api.posts if action == "start")
    assert start["mission_id"] == "new-mission"
    assert len(start["lease_id"]) == 32
    assert "readiness" not in start
    assert all(body["lease_id"] == start["lease_id"] for action, body in api.posts if action == "lease")
    assert any("Saved evidence: /tmp/saved-capture" in row for row in output)
    assert any("Simulated zero" in row for row in output)
    assert all(api.token not in row for row in output)


@pytest.mark.parametrize("mode,hardware", [("hardware", False), ("observe", True), ("telemetry", False)])
def test_mode_mismatch_never_prepares_or_starts(mode, hardware):
    control, api, _, output = runner(Api(mode=mode), stdin=Input(True))
    assert control.run("red shirt", hardware=hardware) == 1
    assert api.posts == []
    assert any("Expected" in row for row in output)


@pytest.mark.parametrize("percent,seconds", [(20.01, 10), (0, 10), (True, 10), (float("nan"), 10),
                                           (5, 61), (5, 0), (5, 1.1), (5, True)])
def test_invalid_limits_never_contact_supervisor(percent, seconds):
    control, api, _, _ = runner()
    assert control.run("red shirt", percent, seconds) == 1
    assert api.status_calls == 0 and not api.posts


def test_old_prepared_id_is_never_adopted():
    api = Api()
    api.state.update(state="prepared", mission_id="old", appearance="red shirt", percent=5., duration_ms=10000)
    api.prepare_noop = True
    control, api, clock, _ = runner(api)
    assert control.run("red shirt") == 1
    assert [action for action, _ in api.posts] == ["prepare"]
    assert control.mission_id is None and clock() <= 101.1


def test_existing_active_mission_is_not_adopted():
    api = Api()
    api.state.update(state="searching", mission_id="someone-else")
    control, api, _, _ = runner(api)
    assert control.run("red shirt") == 1
    assert api.posts == []


@pytest.mark.parametrize("operation", ["prepare", "start"])
def test_ambiguous_mutation_is_never_retried_and_is_cancelled(operation):
    api = Api()
    api.error_after = operation
    control, api, _, output = runner(api)
    assert control.run("red shirt") == 1
    assert [action for action, _ in api.posts].count(operation) == 1
    assert [action for action, _ in api.posts].count("stop") == 1
    assert api.leases == 0
    if operation == "prepare":
        assert not api.start_actual
        assert any("cancelled before a motor start" in row for row in output)


@pytest.mark.parametrize("error", [KeyboardInterrupt(), cli.Cancelled(), cli.ClientError("connection failed")])
def test_interruption_or_connection_failure_stops_without_new_lease(error):
    api = Api()
    api.active_error = error
    control, api, _, _ = runner(api)
    code = control.run("red shirt")
    assert code == (1 if isinstance(error, cli.ClientError) else 130)
    assert [action for action, _ in api.posts].count("stop") == 1
    assert api.leases == 0 and control.cleaning


def test_deadline_is_fixed_despite_continuing_leases():
    control, api, clock, output = runner(Api(finish_after=None))
    assert control.run("red shirt", seconds=1) == 1
    assert clock() <= 101.1
    actions = [action for action, _ in api.posts]
    assert actions.count("start") == 1 and actions[-1] == "stop"
    assert any("Client time limit" in row for row in output)


def test_start_202_does_not_allow_lease_until_applied():
    api = Api(finish_after=1)
    original = api.post

    def queued_start(action, body):
        result = original(action, body)
        if action == "start":
            api.pending_reads = 3
        return result

    api.post = queued_start
    control, api, _, _ = runner(api)
    assert control.run("red shirt") == 0
    assert api.leases == 1


def test_start_queue_acceptance_has_short_fixed_timeout():
    api = Api(finish_after=None)
    original = api.post

    def not_applied(action, body):
        response = original(action, body)
        if action == "start":
            api.state.update(state="prepared")
            api.state["motor"].update(start_status="not_requested")
        return response

    api.post = not_applied
    control, api, clock, output = runner(api)
    assert control.run("red shirt", seconds=60) == 1
    assert clock() < 104
    assert api.leases == 0
    assert any("three seconds" in line for line in output)


def test_faulted_active_status_stops_before_sending_another_lease():
    api = Api(finish_after=None)
    original = api.post

    def faulted_start(action, body):
        response = original(action, body)
        if action == "start":
            api.state["motor"].update(fault=True)
        return response

    api.post = faulted_start
    control, api, _, _ = runner(api)
    assert control.run("red shirt") == 1
    assert api.leases == 0


def test_hardware_cannot_take_piped_readiness():
    control, api, _, output = runner(Api(mode="hardware"), stdin=Input(False))
    assert control.run("red shirt", hardware=True) == 1
    assert api.posts == []
    assert any("interactive terminal" in row for row in output)


def test_hardware_requires_five_exact_yes_answers():
    prompts = []

    def yes(prompt, _deadline):
        prompts.append(prompt)
        return "yes"

    control, api, _, output = runner(Api(mode="hardware"), stdin=Input(True), readiness_reader=yes)
    assert control.run("red shirt", hardware=True) == 0
    start = next(body for action, body in api.posts if action == "start")
    assert len(prompts) == 5
    assert start["readiness"] == {key: True for key, _ in cli.READINESS}
    assert any("physical motor stopping" in row for row in output)


@pytest.mark.parametrize("answer", ["no", "YES", " yes", "", "true"])
def test_readiness_denial_cancels_without_start(answer):
    control, api, _, output = runner(Api(mode="hardware"), stdin=Input(True),
                                     readiness_reader=lambda _p, _d: answer)
    assert control.run("red shirt", hardware=True) == 1
    assert not api.start_actual
    assert [action for action, _ in api.posts] == ["prepare", "stop"]
    assert any("cancelled before a motor start" in row for row in output)


def test_stop_without_fresh_zero_never_claims_verified():
    api = Api(finish_after=None)
    api.unverified_stop = True
    control, api, _, output = runner(api)
    assert control.run("red shirt", seconds=1) == 1
    assert any("STOP UNVERIFIED" in row for row in output)
    assert not any("zero inputs verified" in row or "zero-input telemetry verified" in row for row in output)


def test_prepared_stop_returns_without_waiting_for_motor_telemetry():
    api = Api()
    api.state.update(state="prepared", mission_id="new-mission")
    control, api, clock, output = runner(api)
    assert control.stop_current() == 0
    assert clock() == 100.
    assert any("cancelled before a motor start" in row for row in output)


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
            assert percent == 5 and duration_ms == 10000
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

    def pump():
        completed = False
        while not done.is_set():
            now = time.monotonic()
            supervisor.event(dict(type="progress", capture_mono=now, processed_mono=now,
                                  source_epoch=0, mission_id=supervisor.spec.mission_id if supervisor.spec else None))
            supervisor.tick()
            if not completed and any(event["action"] == "lease" for event in received):
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
        assert motor.starts == 1
        starts = [event for event in received if event["action"] == "start"]
        leases = [event for event in received if event["action"] == "lease"]
        assert len(starts) == 1 and leases
        assert all(event["lease_id"] == starts[0]["lease_id"] for event in leases)
        assert supervisor.snapshot()["operator"] == "terminal"
        assert supervisor.snapshot()["motor"]["zero_confirmed"]
    finally:
        done.set()
        thread.join(1)
        server.close()
        supervisor.close()

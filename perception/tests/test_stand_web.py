"""Operator HTTP boundary tests; callbacks never open hardware or a model."""

import http.client
import json

import pytest

from corvidia_perception.stand_web import StandWebServer


@pytest.fixture
def web():
    events = []

    def accept(event):
        events.append(event)
        return {"accepted": True}

    server = StandWebServer(
        port=0, token="test-operator-token", command_fn=accept,
        status_fn=lambda: {"mode": "observe", "state": "idle"},
        preview_fn=lambda: b"jpeg-preview",
        evidence_fn=lambda mission: (b"jpeg-evidence", "image/jpeg") if mission == "m1" else None,
    )
    yield server, events
    server.close()


def request(web, method, path, body=None, *, token=True, headers=None):
    server, _events = web
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=3)
    h = {"Origin": f"http://127.0.0.1:{server.port}"}
    if token:
        h["X-Corvidia-Token"] = server.token
    if body is not None:
        body = json.dumps(body)
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    try:
        connection.request(method, path, body=body, headers=h)
        response = connection.getresponse()
        return response.status, dict(response.headers), response.read()
    finally:
        connection.close()


def test_local_dashboard_never_embeds_operator_token(web):
    code, headers, body = request(web, "GET", "/?token=example")
    assert code == 200
    assert b"Stand mission" in body
    assert web[0].token.encode() not in body
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["X-Frame-Options"] == "DENY"
    assert b"sessionStorage" in body


def test_status_preview_and_authorized_evidence(web):
    assert json.loads(request(web, "GET", "/api/status")[2]) == {"mode": "observe", "state": "idle"}
    assert request(web, "GET", "/frame.jpg")[2] == b"jpeg-preview"
    assert request(web, "GET", "/api/evidence?mission_id=m1")[2] == b"jpeg-evidence"
    assert request(web, "GET", "/api/evidence?mission_id=m1", token=False)[0] == 403
    assert request(web, "GET", "/api/evidence?mission_id=old")[0] == 404


def test_preparation_defaults_are_bounded_and_enqueued(web):
    assert request(web, "POST", "/api/prepare", {"appearance": " red shirt "})[0] == 202
    assert web[1] == [dict(action="prepare", appearance="red shirt", percent=5, duration_ms=10000)]


@pytest.mark.parametrize("changes", [
    {"percent": True}, {"percent": 0}, {"percent": 20.01}, {"percent": float("nan")},
    {"duration_ms": 60001}, {"duration_ms": 1000.0}, {"duration_ms": True},
    {"appearance": "x" * 241}, {"appearance": {}}, {"appearance": "  "}, {"throttle": 50},
])
def test_invalid_preparation_never_reaches_supervisor(web, changes):
    body = dict(appearance="red shirt", percent=5, duration_ms=10000)
    body.update(changes)
    assert request(web, "POST", "/api/prepare", body)[0] == 400
    assert web[1] == []


@pytest.mark.parametrize("path,body", [
    ("/api/prepare", {"appearance": ""}),
    ("/api/start", {"mission_id": "m1", "readiness": {
        "guarded_stand": True, "hands_clear": True, "power_disconnect_accessible": True,
        "motors_still": True, "esc_startup_finished": True}}),
    ("/api/stop", {"mission_id": "m1"}),
    ("/api/lease", {"mission_id": "m1"}),
])
def test_every_mutation_requires_operator_token(web, path, body):
    assert request(web, "POST", path, body, token=False)[0] == 403
    assert request(web, "POST", path, body, headers={"X-Corvidia-Token": "wrong"})[0] == 403
    assert web[1] == []


@pytest.mark.parametrize("headers", [
    {"Host": "attacker.example"},
    {"Origin": "http://attacker.example"},
    {"Origin": "null"},
    {"Sec-Fetch-Site": "cross-site"},
])
def test_host_and_origin_are_validated_even_with_valid_token(web, headers):
    assert request(web, "POST", "/api/stop", {"mission_id": "m1"}, headers=headers)[0] == 403
    assert web[1] == []


def test_start_requires_fresh_explicit_readiness_flags(web):
    readiness = dict(guarded_stand=True, hands_clear=True, power_disconnect_accessible=True,
                     motors_still=True, esc_startup_finished=True)
    for value in (False, 1, "true", None):
        flags = dict(readiness, hands_clear=value)
        assert request(web, "POST", "/api/start", {"mission_id": "m1", "readiness": flags})[0] == 400
    assert web[1] == []
    assert request(web, "POST", "/api/start", {"mission_id": "m1", "readiness": readiness})[0] == 202
    assert web[1][-1] == dict(action="start", mission_id="m1", readiness=readiness)


def test_observation_start_may_omit_physical_assertions(web):
    # Only the supervisor owns immutable execution mode. It must reject this
    # event when running in hardware mode; the web layer cannot select mode.
    assert request(web, "POST", "/api/start", {"mission_id": "m1"})[0] == 202
    assert web[1][-1] == dict(action="start", mission_id="m1", readiness={})
    assert request(web, "POST", "/api/start", {"mission_id": "m1", "mode": "observe"})[0] == 400


def test_body_size_content_type_and_json_object_enforced(web):
    assert request(web, "POST", "/api/stop", [], headers={"Content-Type": "text/plain"})[0] == 415
    assert request(web, "POST", "/api/stop", ["m1"])[0] == 400
    assert request(web, "POST", "/api/prepare", {"appearance": "x" * 5000})[0] == 413
    assert web[1] == []


def test_stop_and_lease_preserve_mission_identity(web):
    for action in ("stop", "lease"):
        assert request(web, "POST", f"/api/{action}", {"mission_id": "m1"})[0] == 202
        assert web[1][-1] == dict(action=action, mission_id="m1")
        assert request(web, "POST", f"/api/{action}", {"mission_id": ""})[0] == 400


def test_terminal_lease_id_passes_through_start_and_renewal(web):
    owner = "terminal_test_12345678"
    for action in ("start", "lease"):
        assert request(web, "POST", f"/api/{action}", {"mission_id": "m1", "lease_id": owner})[0] == 202
        assert web[1][-1]["lease_id"] == owner
        for bad in (None, 2, "short", "x" * 97, "a" * 16 + "\n"):
            assert request(web, "POST", f"/api/{action}", {"mission_id": "m1", "lease_id": bad})[0] == 400
    assert request(web, "POST", "/api/stop", {"mission_id": "m1", "lease_id": owner})[0] == 400


def test_callbacks_reject_without_disclosing_internal_errors():
    def reject(_event):
        raise ValueError("Mission is no longer prepared")

    server = StandWebServer(port=0, command_fn=reject)
    try:
        code, _, body = request((server, []), "POST", "/api/stop", {"mission_id": "m1"})
        assert code == 400
        assert json.loads(body)["error"] == "Mission is no longer prepared"
    finally:
        server.close()


def test_non_loopback_bind_is_rejected():
    with pytest.raises(ValueError, match="loopback"):
        StandWebServer(host="0.0.0.0", port=0)


def test_close_is_idempotent():
    server = StandWebServer(port=0)
    server.close()
    server.close()

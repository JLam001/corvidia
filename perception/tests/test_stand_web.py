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
    assert b"Search &amp; Rescue" in body or b"Search & Rescue" in body
    assert web[0].token.encode() not in body
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["X-Frame-Options"] == "DENY"
    assert b"sessionStorage" in body
    assert b'id="percent"' not in body and b'id="duration"' not in body
    assert b"Prepare mission" not in body


def test_status_preview_and_authorized_evidence(web):
    assert json.loads(request(web, "GET", "/api/status")[2]) == {"mode": "observe", "state": "idle"}
    assert request(web, "GET", "/frame.jpg")[2] == b"jpeg-preview"
    assert request(web, "GET", "/api/evidence?mission_id=m1")[2] == b"jpeg-evidence"
    assert request(web, "GET", "/api/evidence?mission_id=m1", token=False)[0] == 403
    assert request(web, "GET", "/api/evidence?mission_id=old")[0] == 404


def test_mission_does_not_accept_user_motor_settings(web):
    assert request(web, "POST", "/api/mission", {"appearance": " red shirt "})[0] == 202
    assert web[1] == [dict(action="mission", appearance="red shirt", readiness={})]


def test_timed_brief_is_forwarded_as_data_without_client_execution_settings(web):
    brief = "find as many people as possible within 30 seconds"
    assert request(web, "POST", "/api/mission", {"appearance": brief})[0] == 202
    assert web[1] == [dict(action="mission", appearance=brief, readiness={})]


@pytest.mark.parametrize("brief", ["find people for 61 seconds", "find people for zero seconds",
                                   "find people wearing a blue polo and glasses for 30 seconds"])
def test_invalid_timed_briefs_do_not_reach_supervisor(web, brief):
    assert request(web, "POST", "/api/mission", {"appearance": brief})[0] == 400
    assert web[1] == []


@pytest.mark.parametrize("appearance", ["person in a blue polo and glasses", "red shirt with stripes",
                                      "person carrying a bag"])
def test_unsupported_traits_return_actionable_400_without_queueing(web, appearance):
    code, _, body = request(web, "POST", "/api/mission", {"appearance": appearance})
    assert code == 400 and web[1] == []
    error = json.loads(body)["error"]
    assert "polo" in error.lower() or "shirt" in error.lower()


@pytest.mark.parametrize("changes", [
    {"percent": True}, {"percent": 0}, {"percent": 20.01}, {"percent": float("nan")},
    {"duration_ms": 60001}, {"duration_ms": 1000.0}, {"duration_ms": True},
    {"appearance": "x" * 241}, {"appearance": {}}, {"appearance": "  "}, {"throttle": 50},
    {"completion_mode": "timed_collection"}, {"capture_count": 20},
])
def test_invalid_preparation_never_reaches_supervisor(web, changes):
    body = dict(appearance="red shirt")
    body.update(changes)
    assert request(web, "POST", "/api/mission", body)[0] == 400
    assert web[1] == []


@pytest.mark.parametrize("path,body", [
    ("/api/mission", {"appearance": ""}),
    ("/api/mission", {"appearance": "red shirt", "readiness": {
        "guarded_stand": True, "hands_clear": True, "power_disconnect_accessible": True,
        "motors_still": True, "esc_startup_finished": True}}),
    ("/api/stop", {"mission_id": "m1"}),
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
        assert request(web, "POST", "/api/mission", {"appearance": "red shirt", "readiness": flags})[0] == 400
    assert web[1] == []
    assert request(web, "POST", "/api/mission", {"appearance": "red shirt", "readiness": readiness})[0] == 202
    assert web[1][-1] == dict(action="mission", appearance="red shirt", readiness=readiness)


def test_observation_start_may_omit_physical_assertions(web):
    # Only the supervisor owns immutable execution mode. It must reject this
    # event when running in hardware mode; the web layer cannot select mode.
    assert request(web, "POST", "/api/mission", {"appearance": "red shirt"})[0] == 202
    assert web[1][-1] == dict(action="mission", appearance="red shirt", readiness={})
    assert request(web, "POST", "/api/mission", {"mission_id": "m1", "mode": "observe"})[0] == 400


def test_body_size_content_type_and_json_object_enforced(web):
    assert request(web, "POST", "/api/stop", [], headers={"Content-Type": "text/plain"})[0] == 415
    assert request(web, "POST", "/api/stop", ["m1"])[0] == 400
    assert request(web, "POST", "/api/mission", {"appearance": "x" * 5000})[0] == 413
    assert web[1] == []


def test_stop_preserves_mission_identity(web):
    assert request(web, "POST", "/api/stop", {"mission_id": "m1"})[0] == 202
    assert web[1][-1] == dict(action="stop", mission_id="m1")
    assert request(web, "POST", "/api/stop", {"mission_id": ""})[0] == 400


def test_retired_lease_endpoint_and_start_field_are_rejected(web):
    owner = "terminal_test_12345678"
    assert request(web, "POST", "/api/lease", {"mission_id": "m1", "lease_id": owner})[0] == 404
    assert request(web, "POST", "/api/mission", {"mission_id": "m1", "lease_id": owner})[0] == 400
    assert request(web, "POST", "/api/stop", {"mission_id": "m1", "lease_id": owner})[0] == 400
    assert web[1] == []


def test_dashboard_does_not_stop_on_hiding_or_renew_leases(web):
    page = request(web, "GET", "/")[2]
    assert b"visibilitychange" not in page and b"pagehide" not in page
    assert b"command('lease'" not in page
    assert b"mission continues on the Jetson" in page


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


@pytest.mark.parametrize("path", ["/api/prepare", "/api/start", "/api/lease"])
def test_retired_two_step_controls_are_unavailable(web, path):
    assert request(web, "POST", path, {"appearance": "person"})[0] == 404
    assert web[1] == []

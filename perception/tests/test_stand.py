"""Mission boundaries and failure behavior without opening serial or a camera."""
import queue

import pytest

from corvidia_perception.stand import MissionSpec, StandSupervisor


class Clock:
    t = 100.0

    def __call__(self):
        return self.t


class Motor:
    def __init__(self):
        self.starts, self.stops, self.refreshes = [], [], 0
        self.s = dict(ready=True, connected=True, active=False, zero_confirmed=True,
                      start_status="not_requested", stop_status="not_requested")

    def start_background(self):
        pass

    def snapshot(self):
        return dict(self.s)

    def refresh_lease(self):
        self.refreshes += 1

    def start_all(self, percent, duration_ms):
        self.starts.append((percent, duration_ms))
        self.s.update(active=True, zero_confirmed=False, start_status="accepted")

    def stop(self, reason):
        self.stops.append(reason)
        self.s.update(active=False, zero_confirmed=True, stop_status="verified")

    def close(self):
        pass


def setup(mode="observe"):
    clock, motor, messages = Clock(), Motor(), queue.Queue(16)
    sup = StandSupervisor(lambda: motor, messages, mode=mode, clock=clock)
    sup.event(dict(type="ready", capture_mono=clock(), processed_mono=clock(), source_epoch=0))
    sup.submit(dict(action="prepare", appearance="wearing a red shirt", percent=5, duration_ms=10000))
    sup.tick()
    return sup, clock, motor, messages


def start(sup, **kwargs):
    sup.submit(dict(action="start", mission_id=sup.spec.mission_id, **kwargs))
    sup.tick()


def progress(sup, clock):
    sup.event(dict(type="progress", capture_mono=clock(), processed_mono=clock(), source_epoch=0,
                   mission_id=sup.spec.mission_id))
    sup.submit(dict(action="lease", mission_id=sup.spec.mission_id))


@pytest.mark.parametrize("percent,duration", [(20.01, 10000), (float("nan"), 10000),
                                              (True, 10000), (5, 60001), (5, True), (0, 10000)])
def test_invalid_limits_never_create_mission(percent, duration):
    with pytest.raises(ValueError):
        MissionSpec.create("red shirt", percent, duration)


def test_prepare_does_not_start_and_hardware_requires_readiness():
    sup, clock, motor, _ = setup("hardware")
    assert not motor.starts
    start(sup)
    assert not motor.starts and sup.state == "prepared"
    start(sup, readiness=dict(guarded_stand=True, hands_clear=True, power_disconnect_accessible=True,
                             motors_still=True, esc_startup_finished=True))
    assert motor.starts == [(5, 10000)]


def test_invalid_description_is_rejected_before_motor_authority():
    sup, _, motor, _ = setup("hardware")
    with pytest.raises(ValueError, match="single-line"):
        sup.submit(dict(action="prepare", appearance="red shirt\nblue pants"))
    assert not motor.starts


def test_committed_matching_capture_stops_early():
    sup, clock, motor, commands = setup()
    start(sup)
    assert commands.get_nowait()["type"] == "begin"
    clock.t += .1
    sup.event(dict(type="completion", mission_id=sup.spec.mission_id, result="confirmed",
                   committed=True, capture_mono=100.01, commit_mono=100.1, source_epoch=0,
                   path="/tmp/evidence", event_id="one"))
    sup.tick()
    assert sup.state == "complete" and len(motor.stops) == 1
    assert sup.deadline == 110
    assert motor.s["zero_confirmed"]
    assert sup.evidence["capture_to_stop_request_ms"] == pytest.approx(90)
    assert sup.evidence["commit_to_stop_request_ms"] == pytest.approx(0)


@pytest.mark.parametrize("condition", ["motor_fault", "operator_stop", "stale_camera", "memory", "stage_timeout"])
def test_completion_cannot_hide_active_failure(condition):
    sup, clock, motor, _ = setup()
    start(sup)
    clock.t += .1
    if condition == "motor_fault":
        motor.s.update(fault=True, error="supervisor lease expired")
    elif condition == "operator_stop":
        sup.submit(dict(action="stop", mission_id=sup.spec.mission_id))
    elif condition == "stale_camera":
        clock.t += .5
    elif condition == "memory":
        sup.resource_error = "insufficient memory"
    elif condition == "stage_timeout":
        sup.stage, sup.stage_started = "confirming", clock() - 4.1
    sup.event(dict(type="completion", mission_id=sup.spec.mission_id, result="confirmed",
                   committed=True, capture_mono=100.01, commit_mono=clock(), source_epoch=0,
                   path="/tmp/evidence", event_id="one"))
    sup.tick()
    assert sup.state == ("cancelled" if condition == "operator_stop" else "failed")
    assert sup.evidence["event_id"] == "one"
    assert len(motor.stops) == 1


@pytest.mark.parametrize("condition", ["motor_fault", "operator_stop"])
def test_failure_during_stop_verification_overrides_completion(condition):
    sup, clock, motor, _ = setup()
    start(sup)
    clock.t += .1
    sup.event(dict(type="completion", mission_id=sup.spec.mission_id, result="confirmed",
                   committed=True, capture_mono=100.01, commit_mono=clock(), source_epoch=0,
                   path="/tmp/evidence", event_id="one"))
    assert sup.state == "stopping"
    if condition == "motor_fault":
        motor.s.update(fault=True, error="serial lost")
    else:
        sup.submit(dict(action="stop", mission_id=sup.spec.mission_id))
    sup.tick()
    assert sup.state == ("cancelled" if condition == "operator_stop" else "failed")
    assert sup.evidence["event_id"] == "one"


def test_wrong_mission_and_duplicate_start_do_not_restart_or_extend():
    sup, clock, motor, _ = setup()
    start(sup)
    deadline = sup.deadline
    start(sup)
    sup.event(dict(type="completion", mission_id="old", result="confirmed", committed=True))
    assert len(motor.starts) == 1 and not motor.stops and sup.deadline == deadline


@pytest.mark.parametrize("result,reason,committed", [("confirmed", "answer_yes", False),
                                                     ("unknown", "timeout", True),
                                                     ("unknown", "malformed_output", True)])
def test_failed_save_or_confirmation_stops(result, reason, committed):
    sup, clock, motor, _ = setup()
    start(sup)
    sup.event(dict(type="completion", mission_id=sup.spec.mission_id, result=result,
                   reason=reason, committed=committed))
    sup.tick()
    assert sup.state == "failed" and motor.stops


@pytest.mark.parametrize("result,reason", [("rejected", "answer_no"), ("unknown", "ambiguous")])
def test_valid_nonmatch_or_uncertainty_continues_search(result, reason):
    sup, clock, motor, _ = setup()
    start(sup)
    sup.event(dict(type="completion", mission_id=sup.spec.mission_id,
                   result=result, reason=reason, committed=True))
    sup.tick()
    assert sup.state == "searching" and not motor.stops


def test_ui_loss_stops_even_while_camera_progresses():
    sup, clock, motor, _ = setup()
    start(sup)
    clock.t += 1.01
    sup.event(dict(type="progress", capture_mono=clock(), processed_mono=clock(), source_epoch=0))
    sup.tick()
    assert sup.state == "failed" and "operator page" in motor.stops[0]


def test_stale_camera_stops_despite_ui_lease():
    sup, clock, motor, _ = setup()
    start(sup)
    clock.t += .51
    sup.submit(dict(action="lease", mission_id=sup.spec.mission_id))
    sup.tick()
    assert sup.state == "failed" and "camera" in motor.stops[0]


def test_deadline_and_late_result_never_restart():
    sup, clock, motor, _ = setup()
    start(sup)
    clock.t = 110.01
    progress(sup, clock)
    sup.tick()
    assert sup.state == "timed_out"
    sup.event(dict(type="completion", mission_id=sup.spec.mission_id, result="confirmed", committed=True))
    sup.tick()
    assert sup.state == "timed_out" and len(motor.starts) == 1


def test_stalled_save_has_independent_deadline():
    sup, clock, motor, _ = setup()
    start(sup)
    sup.event(dict(type="stage", mission_id=sup.spec.mission_id, stage="saving", mono=clock()))
    clock.t += 2.01
    progress(sup, clock)
    sup.tick()
    assert sup.state == "failed" and "saving" in motor.stops[0]


def test_camera_reset_and_operator_cancel_are_terminal():
    sup, clock, motor, _ = setup()
    start(sup)
    sup.event(dict(type="progress", capture_mono=clock(), processed_mono=clock(), source_epoch=1))
    sup.tick()
    assert sup.state == "failed" and "restarted" in motor.stops[0]
    sup2, _, motor2, _ = setup()
    start(sup2)
    sup2.submit(dict(action="stop", mission_id=sup2.spec.mission_id))
    sup2.tick()
    assert sup2.state == "cancelled" and motor2.stops


def test_missing_stop_telemetry_is_unverified():
    sup, clock, motor, _ = setup()
    start(sup)
    motor.stop = lambda reason: motor.s.update(stop_status="pending", zero_confirmed=False)
    sup.submit(dict(action="stop", mission_id=sup.spec.mission_id))
    sup.tick()
    clock.t += 2.01
    sup.tick()
    assert sup.state == "failed" and "STOP UNVERIFIED" in sup.error
    assert sup.recovery_required
    sup.submit(dict(action="prepare", appearance="red shirt"))
    sup.tick()
    assert sup.state == "failed" and len(motor.starts) == 1


def test_read_only_mode_cannot_prepare():
    sup, _, motor, _ = setup("telemetry")
    assert sup.spec is None and not motor.starts


def test_preflight_distinguishes_live_telemetry_from_simulated_motor_readiness():
    clock, motor, observer = Clock(), Motor(), Motor()
    observer.s.update(ready=False, connected=False, error='USB disconnected', fault=True,
                      timestamps={'last_rx': clock() - .75}, telemetry={'IMU_OK': 1})
    sup = StandSupervisor(lambda: motor, queue.Queue(16), mode='observe', clock=clock,
                          telemetry_observer=observer)
    status = sup.snapshot()
    p = status['preflight']
    assert p['motor_ready'] is True
    assert p['telemetry_ready'] is False and p['telemetry_connected'] is False
    assert p['telemetry_error'] == 'USB disconnected' and p['telemetry_fault'] is True
    assert p['telemetry_rx_age_ms'] == 750
    assert status['telemetry_mode'] == 'live_read_only'
    assert status['imu'] == {'IMU_OK': 1}  # Cached reading is accompanied by failure/freshness.
    assert not motor.starts and not observer.starts


def test_preflight_reports_memory_gate_and_unknown_sample():
    sup, clock, motor, _ = setup()
    p = sup.snapshot()['preflight']
    assert p['resource_ready'] is False and p['memory_available_mib'] is None
    assert p['minimum_memory_mib'] == 1536
    sup.memory_available_mib = 1200.5
    sup.resource_checked_mono = clock()
    sup.resource_error = 'memory headroom 1200 MiB is below 1536 MiB'
    sup.tick()
    p = sup.snapshot()['preflight']
    assert p['resource_ready'] is False and p['memory_available_mib'] == 1200.5
    assert p['resource_error'] == sup.resource_error
    assert p['resource_checked_mono'] == clock()
    assert 'camera_ready' in p and 'motor_ready' in p
    sup.memory_available_mib, sup.resource_error = 1800.0, None
    sup.tick()
    assert sup.snapshot()['preflight']['resource_ready'] is True
    assert not motor.starts


def test_preflight_without_telemetry_packet_does_not_claim_zero_age():
    sup, _, _, _ = setup()
    assert sup.snapshot()['preflight']['telemetry_rx_age_ms'] is None

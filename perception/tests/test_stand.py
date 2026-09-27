"""Mission boundaries and failure behavior without opening serial or a camera."""
import queue
import threading
from concurrent.futures import ThreadPoolExecutor

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
    sup.memory_available_mib = 2048.0
    if mode != "telemetry":
        sup._prepare(dict(appearance="wearing a red shirt", percent=5, duration_ms=10000))
    sup._publish()
    return sup, clock, motor, messages


def start(sup, **kwargs):
    sup._start(dict(mission_id=sup.spec.mission_id, **kwargs))
    sup.tick()


def progress(sup, clock):
    sup.event(dict(type="progress", capture_mono=clock(), processed_mono=clock(), source_epoch=0,
                   mission_id=sup.spec.mission_id))


@pytest.mark.parametrize("percent,duration", [(20.01, 10000), (float("nan"), 10000),
                                              (True, 10000), (5, 60001), (5, True), (0, 10000)])
def test_invalid_limits_never_create_mission(percent, duration):
    with pytest.raises(ValueError):
        MissionSpec.create("red shirt", percent, duration)


def test_prepare_does_not_start_and_hardware_requires_readiness():
    sup, clock, motor, _ = setup("hardware")
    assert not motor.starts
    with pytest.raises(ValueError):
        start(sup)
    assert not motor.starts and sup.state == "prepared"
    start(sup, readiness=dict(guarded_stand=True, hands_clear=True, power_disconnect_accessible=True,
                             motors_still=True, esc_startup_finished=True))
    assert motor.starts == [(5, 10000)]


def test_invalid_description_is_rejected_before_motor_authority():
    sup, _, motor, _ = setup("hardware")
    with pytest.raises(ValueError, match="single-line"):
        sup.submit(dict(action="mission", appearance="red shirt\nblue pants"))
    assert not motor.starts


@pytest.mark.parametrize("appearance", ["blue polo and glasses", "red shirt and blue pants",
                                      "blue striped polo", "person holding a red bag"])
def test_unsupported_traits_are_rejected_before_queue_or_motor_start(appearance):
    sup, _, motor, _ = setup("hardware")
    with pytest.raises(ValueError):
        sup.submit(dict(action="mission", appearance=appearance))
    assert sup.commands.empty() and sup._pending_mission is None and not motor.starts


def test_compiled_requirements_are_preserved_in_status_and_worker_evidence():
    sup, _, _, commands = setup()
    start(sup)
    required = {"subject": "human", "upper_color": "red", "upper_garment": "shirt"}
    assert sup.snapshot()["requirements"] == required
    command = commands.get_nowait()
    assert command["record_extra"]["mission"]["requirements"] == required


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
    with pytest.raises(ValueError):
        start(sup)
    sup.event(dict(type="completion", mission_id="old", result="confirmed", committed=True))
    assert len(motor.starts) == 1 and not motor.stops and sup.deadline == deadline


@pytest.mark.parametrize("result,reason,committed", [("confirmed", "answer_yes", False),
                                                     ("unknown", "timeout", True),
                                                     ("unknown", "malformed_output", True),
                                                     ("unknown", "unsupported_observations", True)])
def test_failed_save_or_confirmation_stops(result, reason, committed):
    sup, clock, motor, _ = setup()
    start(sup)
    sup.event(dict(type="completion", mission_id=sup.spec.mission_id, result=result,
                   reason=reason, committed=committed))
    sup.tick()
    assert sup.state == "failed" and motor.stops


@pytest.mark.parametrize("result,reason", [("rejected", "answer_no"), ("unknown", "ambiguous"),
                                          ("rejected", "upper_color_mismatch"),
                                          ("rejected", "upper_garment_mismatch"),
                                          ("unknown", "subject_unknown"),
                                          ("unknown", "upper_color_unknown"),
                                          ("unknown", "upper_garment_unknown")])
def test_valid_nonmatch_or_uncertainty_continues_search(result, reason):
    sup, clock, motor, _ = setup()
    start(sup)
    sup.event(dict(type="completion", mission_id=sup.spec.mission_id,
                   result=result, reason=reason, committed=True))
    sup.tick()
    assert sup.state == "searching" and not motor.stops


def test_onboard_mission_continues_without_client_and_keeps_fixed_deadline():
    sup, clock, motor, _ = setup()
    start(sup)
    deadline = sup.deadline
    for _ in range(20):
        clock.t += .25
        progress(sup, clock)
        sup.tick()
    assert sup.state == "searching" and not motor.stops
    assert sup.deadline == deadline and len(motor.starts) == 1
    assert motor.refreshes > 20
    assert sup.snapshot()["mission_owner"] == "jetson"
    assert sup.snapshot()["lease_required"] is False


def test_retired_client_leases_cannot_extend_or_stop_onboard_mission():
    sup, clock, motor, _ = setup()
    start(sup)
    deadline = sup.deadline
    clock.t += .4
    with pytest.raises(ValueError, match="Unknown action"):
        sup.submit(dict(action="lease", mission_id=sup.spec.mission_id))
    sup.tick()
    assert sup.state == "searching" and sup.deadline == deadline and not motor.stops
    sup.submit(dict(action="stop", mission_id=sup.spec.mission_id))
    sup.tick()
    assert sup.state == "cancelled" and motor.stops == ["operator stop"]


def test_capture_can_finish_after_client_exits():
    sup, clock, motor, _ = setup()
    start(sup)
    clock.t += 3
    progress(sup, clock)
    sup.event(dict(type="completion", mission_id=sup.spec.mission_id, result="confirmed",
                   committed=True, capture_mono=102.9, commit_mono=103., source_epoch=0,
                   path="/tmp/evidence", event_id="after-client-exit"))
    sup.tick()
    assert sup.state == "complete" and motor.s["zero_confirmed"]
    assert sup.evidence["event_id"] == "after-client-exit"


def test_stale_camera_stops_onboard_mission():
    sup, clock, motor, _ = setup()
    start(sup)
    clock.t += .51
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
    with pytest.raises(ValueError):
        sup.submit(dict(action="mission", appearance="red shirt"))
    sup.tick()
    assert sup.state == "failed" and len(motor.starts) == 1


def test_read_only_mode_cannot_submit_mission():
    sup, _, motor, _ = setup("telemetry")
    with pytest.raises(ValueError):
        sup.submit(dict(action="mission", appearance="red shirt"))
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
    sup.memory_available_mib = None
    sup._publish()
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


def public_setup(*, mode='observe', camera=True, memory=True, observer=None):
    """Public one-step submission, with independently created one-shot motors."""
    clock, motors, commands = Clock(), [], queue.Queue(16)
    def factory():
        motor = Motor()
        motors.append(motor)
        return motor
    sup = StandSupervisor(factory, commands, mode=mode, clock=clock,
                          telemetry_observer=observer)
    if memory:
        sup.memory_available_mib = 2048.0
        sup.resource_checked_mono = clock()
    if camera:
        sup.event(dict(type='ready', capture_mono=clock(), processed_mono=clock(), source_epoch=0))
    sup._publish()
    return sup, clock, motors, commands


def mission(sup, **extra):
    return sup.submit(dict(action='mission', appearance='person wearing a red shirt', **extra))


def ready_event(sup, clock):
    sup.event(dict(type='ready', capture_mono=clock(), processed_mono=clock(), source_epoch=0))


def test_one_step_submission_reserves_id_and_uses_only_fixed_onboard_settings():
    sup, clock, motors, commands = public_setup()
    receipt = mission(sup)
    assert receipt['accepted'] is True and isinstance(receipt['mission_id'], str)
    assert not motors[0].starts  # Admission does not perform serial work in the HTTP thread.
    sup.tick()
    assert sup.spec.mission_id == receipt['mission_id']
    assert motors[0].starts == [(5, 60000)]
    assert sup.deadline == clock() + 60
    assert sup.state == 'searching'
    assert commands.get_nowait()['mission_id'] == receipt['mission_id']
    for _ in range(4): sup.tick()
    assert len(motors[0].starts) == 1


@pytest.mark.parametrize('override', [dict(percent=1), dict(percent=20), dict(duration_ms=1000),
                                     dict(duration_ms=60000), dict(mission_id='chosen'), dict(token=1)])
def test_public_mission_rejects_motor_and_identity_overrides(override):
    sup, _, motors, _ = public_setup()
    with pytest.raises(ValueError): mission(sup, **override)
    sup.tick()
    assert not motors[0].starts
    assert sup.spec is None


@pytest.mark.parametrize('action', ['prepare', 'start', 'lease'])
def test_retired_two_step_and_client_lease_actions_are_unavailable(action):
    sup, _, motors, _ = public_setup()
    with pytest.raises(ValueError):
        sup.submit(dict(action=action, appearance='red shirt', mission_id='old'))
    assert not motors[0].starts


def test_hardware_readiness_is_required_before_reserving_mission():
    sup, _, motors, _ = public_setup(mode='hardware')
    for readiness in ({}, {'guarded_stand': True},
                      dict(guarded_stand=True, hands_clear=True, power_disconnect_accessible=True,
                           motors_still=False, esc_startup_finished=True)):
        with pytest.raises(ValueError): mission(sup, readiness=readiness)
    assert not motors[0].starts and sup.spec is None
    receipt = mission(sup, readiness=dict(guarded_stand=True, hands_clear=True,
                                         power_disconnect_accessible=True, motors_still=True,
                                         esc_startup_finished=True))
    sup.tick()
    assert sup.spec.mission_id == receipt['mission_id']
    assert motors[0].starts == [(5, 60000)]


def test_reserved_mission_can_be_stopped_before_the_first_tick():
    sup, clock, motors, commands = public_setup()
    receipt = mission(sup)
    assert sup.submit(dict(action='stop', mission_id=receipt['mission_id']))['accepted']
    sup.tick()
    assert sup.state == 'cancelled' and not motors[0].starts
    ready_event(sup, clock)
    sup.tick()
    assert sup.state == 'cancelled' and not motors[0].starts
    assert commands.empty()


def test_preparing_waits_without_motor_authority_then_starts_exactly_once():
    sup, clock, motors, commands = public_setup(camera=False)
    receipt = mission(sup)
    sup.tick()
    assert sup.state == 'preparing'
    assert not motors[0].starts and motors[0].refreshes == 0 and commands.empty()
    clock.t += 2
    ready_event(sup, clock)
    sup.tick()
    assert sup.state == 'searching' and motors[0].starts == [(5, 60000)]
    assert sup.spec.mission_id == receipt['mission_id'] and sup.deadline == clock() + 60
    sup.tick()
    assert len(motors[0].starts) == 1


@pytest.mark.parametrize('elapsed', [15., 15.001, 20.])
def test_preparing_timeout_is_measured_from_submission_and_cannot_start_late(elapsed):
    sup, clock, motors, _ = public_setup(camera=False)
    mission(sup)
    # Deliberately delay even the first dequeue; preparation cannot get another15s.
    clock.t += elapsed
    ready_event(sup, clock)
    sup.tick()
    assert sup.state == 'failed' and not motors[0].starts
    clock.t += 1
    ready_event(sup, clock)
    sup.tick()
    assert sup.state == 'failed' and not motors[0].starts


def test_explicit_stop_during_preparation_cannot_be_revived_by_camera_recovery():
    sup, clock, motors, _ = public_setup(camera=False)
    receipt = mission(sup)
    sup.tick()
    assert sup.state == 'preparing'
    sup.submit(dict(action='stop', mission_id=receipt['mission_id']))
    sup.tick()
    assert sup.state == 'cancelled'
    clock.t += .1
    ready_event(sup, clock)
    sup.tick()
    assert sup.state == 'cancelled' and not motors[0].starts


def test_unknown_memory_headroom_blocks_preparing_until_a_sample_exists():
    sup, clock, motors, _ = public_setup(memory=False)
    mission(sup)
    sup.tick()
    assert sup.state == 'preparing' and not motors[0].starts
    sup.memory_available_mib = 2048
    sup.resource_checked_mono = clock()
    sup.tick()
    assert sup.state == 'searching' and motors[0].starts == [(5, 60000)]


def test_pending_and_active_mission_reject_additional_submissions():
    sup, _, motors, _ = public_setup()
    first = mission(sup)
    with pytest.raises(ValueError): mission(sup)
    sup.tick()
    with pytest.raises(ValueError): mission(sup)
    assert sup.spec.mission_id == first['mission_id'] and motors[0].starts == [(5, 60000)]


def test_concurrent_submissions_reserve_only_one_mission():
    sup, _, motors, _ = public_setup()
    barrier = threading.Barrier(8)
    def submit_one(_):
        barrier.wait()
        try: return mission(sup)
        except ValueError: return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        replies = list(pool.map(submit_one, range(8)))
    accepted = [reply for reply in replies if reply is not None]
    assert len(accepted) == 1
    sup.tick()
    assert sup.spec.mission_id == accepted[0]['mission_id']
    assert motors[0].starts == [(5, 60000)]


def test_public_mission_runs_without_client_until_fixed_timeout_and_never_restarts():
    sup, clock, motors, _ = public_setup()
    receipt = mission(sup)
    sup.tick()
    deadline = sup.deadline
    for _ in range(240):
        clock.t += .25
        progress(sup, clock)
        sup.tick()
    assert sup.state == 'timed_out' and sup.deadline == deadline
    assert motors[0].starts == [(5, 60000)] and len(motors[0].stops) == 1
    sup.event(dict(type='completion', mission_id=receipt['mission_id'], result='confirmed', committed=True,
                   capture_mono=deadline-.1, commit_mono=clock(), source_epoch=0, path='/tmp/late'))
    ready_event(sup, clock)
    sup.tick()
    assert sup.state == 'timed_out' and len(motors[0].starts) == 1


def test_explicit_new_mission_after_completion_uses_a_new_motor_session():
    sup, clock, motors, _ = public_setup()
    first = mission(sup)
    sup.tick()
    clock.t += .1
    sup.event(dict(type='completion', mission_id=first['mission_id'], result='confirmed', committed=True,
                   capture_mono=clock()-.01, commit_mono=clock(), source_epoch=0, path='/tmp/first'))
    sup.tick()
    assert sup.state == 'complete'
    ready_event(sup, clock)
    second = mission(sup)
    sup.tick()
    assert second['mission_id'] != first['mission_id'] and len(motors) == 2
    assert motors[0].starts == [(5, 60000)] and motors[1].starts == [(5, 60000)]
    assert sup.state == 'searching'


def test_stop_accepted_after_pending_check_still_prevents_motor_start():
    sup, _, motors, _ = public_setup()
    receipt = mission(sup)
    original_start = sup._start
    def stop_before_start(message):
        # An HTTP handler accepts Stop after tick's first cancellation check.
        assert sup.submit(dict(action='stop', mission_id=receipt['mission_id']))['accepted']
        return original_start(message)
    sup._start = stop_before_start
    sup.tick()
    assert not motors[0].starts
    sup.tick()
    assert sup.state == 'cancelled'


def test_preflight_that_crosses_preparation_deadline_cannot_start_motors():
    sup, clock, motors, _ = public_setup()
    mission(sup)
    snapshot = motors[0].snapshot
    delayed = False
    def delayed_snapshot():
        nonlocal delayed
        if not delayed:
            delayed = True
            clock.t += 15
        return snapshot()
    motors[0].snapshot = delayed_snapshot
    sup.tick()
    assert not motors[0].starts
    sup.tick()
    assert sup.state == 'failed'


def test_perception_failure_during_preparation_cannot_be_revived_by_recovery():
    sup, clock, motors, _ = public_setup(camera=False)
    receipt = mission(sup)
    sup.tick()
    assert sup.state == 'preparing'
    sup.event(dict(type='fault', mission_id=receipt['mission_id'], reason='camera disconnected'))
    ready_event(sup, clock)
    sup.tick()
    assert sup.state == 'failed' and not motors[0].starts


def test_resource_and_read_only_telemetry_gates_never_renew_motor_lease():
    observer = Motor()
    observer.s.update(ready=False, connected=False, error='USB disconnected')
    sup, clock, motors, _ = public_setup(observer=observer)
    mission(sup)
    sup.tick()
    assert sup.state == 'preparing' and motors[0].refreshes == 0 and not motors[0].starts
    observer.s.update(ready=True, connected=True, error=None)
    sup.resource_error = 'insufficient memory'
    sup.tick()
    assert sup.state == 'preparing' and motors[0].refreshes == 0 and not motors[0].starts
    sup.resource_error = None
    ready_event(sup, clock)
    sup.tick()
    assert sup.state == 'searching' and motors[0].starts == [(5, 60000)]
    assert not observer.starts and observer.refreshes == 0


def test_preflight_stall_cannot_start_from_frames_that_became_stale():
    sup, clock, motors, _ = public_setup()
    mission(sup)
    snapshot = motors[0].snapshot
    delayed = False
    def delayed_snapshot():
        nonlocal delayed
        if not delayed:
            delayed = True
            clock.t += .51
        return snapshot()
    motors[0].snapshot = delayed_snapshot
    sup.tick()
    assert sup.state == 'preparing' and not motors[0].starts
    assert motors[0].refreshes == 0
    ready_event(sup, clock)
    sup.tick()
    assert sup.state == 'searching' and motors[0].starts == [(5, 60000)]


def test_stop_for_wrong_id_cannot_cancel_pending_or_active_mission():
    sup, _, motors, _ = public_setup()
    receipt = mission(sup)
    with pytest.raises(ValueError):
        sup.submit(dict(action='stop', mission_id='stale-mission'))
    sup.tick()
    with pytest.raises(ValueError):
        sup.submit(dict(action='stop', mission_id='stale-mission'))
    sup.tick()
    assert sup.state == 'searching' and sup.spec.mission_id == receipt['mission_id']
    assert motors[0].starts == [(5, 60000)] and not motors[0].stops

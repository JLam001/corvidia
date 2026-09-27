"""Fake wire/clock tests; never open a serial device or send motor commands."""
import pytest

from corvidia_perception.stand_motor import (
    ALL_COMMAND, ENABLE_COMMAND, MODE_ID, MotorSession, SimulatedMotorSession,
    _SimMessage, _SimTransport,
)


class Clock:
    def __init__(self): self.now = 100.
    def __call__(self): return self.now
    def advance(self, delta=.02): self.now += delta


class Wire(_SimTransport):
    def __init__(self, clock):
        super().__init__(clock)
        self.commands, self.heartbeats = [], []
        self.drop = set()
        self.fail_write = False
        self.freeze_imu = False
        self.mode = MODE_ID
        self.suppress = set()
        self.zero_lag = False
        self.receive_error = False
    def command(self, number, params):
        if self.fail_write: raise OSError('write timed out')
        self.commands.append((number, list(params)))
        # An enabled token changes the public next-token field. A start must
        # still carry the consumed token, not that newly advertised token.
        if number == ALL_COMMAND:
            assert params[3] == self.enabled_token
        super().command(number, params)
        if (number, params[0]) in self.drop:
            self.pending.pop()
    def heartbeat(self):
        self.heartbeats.append(self.clock())
        super().heartbeat()
    def receive(self):
        if self.receive_error: raise OSError('USB disconnected')
        messages = super().receive()
        for m in messages:
            if m.kind == 'HEARTBEAT': m.custom_mode = self.mode
            if m.kind == 'ATTITUDE' and self.freeze_imu: m.time_boot_ms = 1
            if m.kind == 'NAMED_VALUE_INT' and self.zero_lag and m.name.startswith('M'):
                m.value = 147
        return [m for m in messages if getattr(m, 'name', None) not in self.suppress]


def session(*, execute=True):
    clock = Clock()
    wire = Wire(clock)
    motor = MotorSession('/fake', execute=execute, _clock=clock,
                         _transport_factory=lambda _: wire)
    motor._connect()
    return motor, wire, clock


def pump(motor, clock, seconds=.02, *, lease=True):
    for _ in range(max(1, round(seconds/.02))):
        clock.advance()
        if lease: motor.refresh_lease()
        motor._step()


def ready(motor, clock):
    pump(motor, clock, 3.3)
    assert motor.snapshot()['ready']


def start(motor, clock, percent=5, duration=10000):
    ready(motor, clock)
    motor.start_all(percent, duration)
    pump(motor, clock, .1)
    assert motor.snapshot()['start_status'] == 'accepted'
    assert motor.snapshot()['active']


def test_real_read_only_never_writes_even_on_stop():
    motor, wire, clock = session(execute=False)
    ready(motor, clock)
    with pytest.raises(RuntimeError, match='read-only'):
        motor.start_all(5, 1000)
    motor.stop('done')
    pump(motor, clock)
    assert wire.commands == wire.heartbeats == []
    assert motor.snapshot()['stop_status'] == 'unverified'


@pytest.mark.parametrize('percent,duration', [(7.51, 1000), (25, 1000), (0, 1000), (True, 1000),
    (float('nan'), 1000), (float('inf'), 1000), (5, 60001), (5, 19), (5, True), (5, 20.5)])
def test_invalid_envelope_cannot_write(percent, duration):
    motor, wire, clock = session()
    ready(motor, clock)
    with pytest.raises(ValueError): motor.start_all(percent, duration)
    assert not wire.commands and not wire.heartbeats


def test_readiness_and_fresh_lease_required():
    motor, wire, clock = session()
    with pytest.raises(RuntimeError, match='readiness'): motor.start_all(5, 1000)
    ready(motor, clock)
    clock.advance(.251)
    # Keep telemetry fresh while deliberately not renewing the supervisor.
    motor._step()
    with pytest.raises(RuntimeError, match='lease'): motor.start_all(5, 1000)
    assert not wire.commands


def test_one_exact_envelope_and_start_token_then_early_stop():
    motor, wire, clock = session()
    start(motor, clock, 7.5, 60000)
    assert wire.commands[:2] == [
        (ENABLE_COMMAND, [1, 1, 7.5, 60000, 0, 0, 0]),
        (ALL_COMMAND, [4, 7.5, 60000, 1, 0, 0, 0]),
    ]
    motor.stop('capture committed')
    count = len(wire.heartbeats)
    pump(motor, clock, .1)
    motor.stop('duplicate')
    pump(motor, clock, .1)
    s = motor.snapshot()
    assert s['stop_status'] == 'verified' and s['zero_confirmed']
    assert not s['active'] and not s['fault']
    assert len(wire.heartbeats) == count
    assert wire.commands[-1] == (ENABLE_COMMAND, [0]*7)
    assert len(wire.commands) == 3
    assert s['timestamps']['zero_confirmed'] > s['timestamps']['stop_ack']
    with pytest.raises(RuntimeError, match='one-shot'): motor.start_all(5, 1000)


def test_deadline_is_fixed_and_successful_expiry_does_not_become_fault():
    motor, wire, clock = session()
    start(motor, clock, duration=1000)
    deadline = motor.snapshot()['timestamps']['run_deadline']
    pump(motor, clock, 1.2)
    s = motor.snapshot()
    assert s['timestamps']['run_deadline'] == deadline
    assert s['stop_reason'] == 'duration expired'
    assert s['stop_status'] == 'verified' and not s['fault']
    assert sum(cmd == ALL_COMMAND for cmd, _ in wire.commands) == 1


def test_supervisor_lease_expiry_revokes_heartbeat_and_latches_fault():
    motor, wire, clock = session()
    start(motor, clock)
    count = len(wire.heartbeats)
    clock.advance(.251)
    motor._step()
    motor.refresh_lease()
    pump(motor, clock, .1)
    assert motor.snapshot()['error'] == 'supervisor lease expired'
    assert len(wire.heartbeats) == count
    assert motor.snapshot()['stop_status'] == 'verified'


def test_lost_start_ack_stops_without_retry():
    motor, wire, clock = session()
    wire.drop.add((ALL_COMMAND, 4))
    ready(motor, clock)
    motor.start_all(5, 10000)
    pump(motor, clock, 1.3)
    s = motor.snapshot()
    assert s['start_status'] == 'failed'
    assert s['stop_status'] == 'verified'
    assert 'acknowledgement timeout' in s['error']
    assert sum(cmd == ALL_COMMAND for cmd, _ in wire.commands) == 1


def test_lost_enable_ack_cannot_be_confused_with_successful_stop_ack():
    motor, wire, clock = session()
    wire.drop.add((ENABLE_COMMAND, 1))
    ready(motor, clock)
    motor.start_all(5, 10000)
    pump(motor, clock, 3.3)
    assert motor.snapshot()['stop_status'] == 'unverified'
    assert not motor.snapshot()['zero_confirmed']
    assert not any(cmd == ALL_COMMAND for cmd, _ in wire.commands)


def test_cancel_between_enable_and_ack_never_sends_start():
    motor, wire, clock = session()
    ready(motor, clock)
    motor.start_all(5, 10000)
    pump(motor, clock)
    motor.stop('operator stop')
    pump(motor, clock, .2)
    assert not any(cmd == ALL_COMMAND for cmd, _ in wire.commands)
    assert motor.snapshot()['stop_status'] == 'verified'


def test_missing_zero_or_lost_stop_ack_remains_unverified():
    for missing_zero in (True, False):
        motor, wire, clock = session()
        start(motor, clock)
        if missing_zero: wire.zero_lag = True
        else: wire.drop.add((ENABLE_COMMAND, 0))
        motor.stop('stop')
        pump(motor, clock, 2.2)
        assert motor.snapshot()['stop_status'] == 'unverified'
        assert not motor.snapshot()['zero_confirmed']


def test_wrong_firmware_cannot_receive_enable_or_stop():
    motor, wire, clock = session()
    wire.mode = 123
    pump(motor, clock)
    assert motor.snapshot()['fault']
    assert not wire.commands and not wire.heartbeats


def test_missing_capability_cannot_become_ready():
    motor, wire, clock = session()
    wire.suppress.add('DS_MAXMS')
    pump(motor, clock, 4)
    assert not motor.snapshot()['ready']
    assert not wire.commands


def test_imu_loss_during_run_stops():
    motor, wire, clock = session()
    start(motor, clock)
    wire.freeze_imu = True
    pump(motor, clock, .4)
    assert motor.snapshot()['fault']
    assert motor.snapshot()['stop_status'] == 'verified'


def test_transport_write_failure_is_not_retried():
    motor, wire, clock = session()
    ready(motor, clock)
    motor.start_all(5, 1000)
    wire.fail_write = True
    with pytest.raises(OSError, match='write timed out'): motor._step()
    assert not wire.commands


def test_simulation_has_explicit_mode_and_needs_no_hardware():
    clock = Clock()
    motor = SimulatedMotorSession(_clock=clock)
    motor._connect()
    start(motor, clock, duration=1000)
    pump(motor, clock, 1.2)
    s = motor.snapshot()
    assert s['mode'] == 'simulated' and s['stop_status'] == 'verified'
    assert s['physical_stop_measured'] is False
    with pytest.raises(ValueError): SimulatedMotorSession(execute=True)


def test_snapshot_is_detached_from_mutable_internal_state():
    motor, wire, clock = session()
    ready(motor, clock)
    snap = motor.snapshot()
    snap['telemetry']['DS_FAULT'] = 999
    assert motor.snapshot()['telemetry']['DS_FAULT'] == 0


def test_receive_failure_closes_link_and_leaves_stop_unverified():
    motor, wire, clock = session()
    start(motor, clock)
    wire.receive_error = True
    motor._run()
    s = motor.snapshot()
    assert s['fault'] and 'USB disconnected' in s['error']
    assert not s['connected'] and not s['zero_confirmed']
    assert s['stop_status'] == 'unverified' and wire.closed
    assert wire.commands[-1] == (ENABLE_COMMAND, [0]*7)
    assert sum(cmd == ALL_COMMAND for cmd, _ in wire.commands) == 1


def test_stop_cancels_even_a_just_queued_start():
    motor, wire, clock = session()
    ready(motor, clock)
    motor.start_all(5, 10000)
    motor.stop('stop before dispatch')
    pump(motor, clock, .1)
    assert all(cmd == ENABLE_COMMAND and params == [0]*7 for cmd, params in wire.commands)
    assert not wire.heartbeats
    assert motor.snapshot()['start_status'] == 'failed'
    assert motor.snapshot()['stop_status'] == 'verified'


def test_firmware_reboot_cannot_resume_a_running_session():
    motor, wire, clock = session()
    start(motor, clock)
    count = len(wire.heartbeats)
    motor._receive(_SimMessage('NAMED_VALUE_INT', name='DS_TOKEN', value=1,
                              time_boot_ms=2), clock())
    pump(motor, clock, .1)
    assert motor.snapshot()['fault']
    assert 'clock reset' in motor.snapshot()['error']
    assert len(wire.heartbeats) == count
    assert sum(cmd == ALL_COMMAND for cmd, _ in wire.commands) == 1


def test_readiness_disappears_when_named_status_is_stale():
    motor, wire, clock = session()
    ready(motor, clock)
    wire.suppress.add('DS_FAULT')
    pump(motor, clock, 1.6)
    assert not motor.snapshot()['ready']
    with pytest.raises(RuntimeError, match='readiness'): motor.start_all(5, 1000)
    assert not wire.commands

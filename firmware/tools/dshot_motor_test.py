#!/usr/bin/env python3
"""DShot bench planner; v6 includes a fixed five-second ramp in each run."""
import argparse
import json
import math
import time

LEGACY_MODE_ID = 0x4453494D
MODE_ID = 0x44534935
RAMP_MODE_ID = 0x44534936
RAMP_MS = 5000
ENABLE_COMMAND = 31010
TEST_COMMAND = 31011
TEST_ALL_COMMAND = 31012


def plan(motor, percent, duration_ms, *, limit_percent=5, limit_duration_ms=500):
    if motor != 'all' and (type(motor) is not int or motor not in range(1, 5)):
        raise ValueError('motor must be an integer from 1 to 4 or explicit "all"')
    if type(limit_percent) not in (int, float) or not 0 < limit_percent <= 100 or not math.isfinite(limit_percent):
        raise ValueError('operator limit must be finite and in (0, 100] percent')
    if type(limit_duration_ms) is not int or not 20 <= limit_duration_ms <= 60000:
        raise ValueError('operator duration limit must be an integer in [20, 60000] ms')
    if type(percent) not in (int, float) or not 0 < percent <= limit_percent or not math.isfinite(percent):
        raise ValueError('throttle must be positive and within the explicit operator limit (default 5%)')
    if type(duration_ms) is not int or not 20 <= duration_ms <= limit_duration_ms:
        raise ValueError('duration must be within the explicit operator limit (default 500 ms)')
    return dict(motor=motor, percent=percent, duration_ms=duration_ms,
                limit_percent=limit_percent, limit_duration_ms=limit_duration_ms,
                dshot_value=48 + math.floor(1999 * percent / 100),
                feather_pin=[9, 10, 6, 5] if motor == 'all' else {1: 9, 2: 10, 3: 6, 4: 5}[motor])


def execute(port, request=None, stop_only=False, *, bench_ready=False,
            individual_stops_checked=False):
    if not stop_only and not bench_ready:
        raise RuntimeError('Explicit bench readiness is required for each DShot test')
    if not stop_only:
        request = plan(request['motor'], request['percent'], request['duration_ms'],
                       limit_percent=request.get('limit_percent', 5),
                       limit_duration_ms=request.get('limit_duration_ms', 500))
        if request['motor'] == 'all' and not individual_stops_checked:
            raise RuntimeError('All four individual physical stop checks are required')
    from pymavlink import mavutil
    link = mavutil.mavlink_connection(port, baud=115200, source_system=255, source_component=190)
    identified = False
    firmware_mode = None
    last_heartbeat = -math.inf
    reports = {}

    def command(number, params):
        link.mav.command_long_send(1, 1, number, 0, *params)

    def receive(heartbeat=False):
        nonlocal last_heartbeat, firmware_mode
        now = time.monotonic()
        if heartbeat and now-last_heartbeat >= .1:
            link.mav.heartbeat_send(mavutil.mavlink.MAV_TYPE_GCS,
                                    mavutil.mavlink.MAV_AUTOPILOT_INVALID, 0, 0, 0)
            last_heartbeat = now
        msg = link.recv_match(blocking=True, timeout=.02)
        if msg is None or (msg.get_srcSystem(), msg.get_srcComponent()) != (1, 1):
            return None
        if msg.get_type() == 'HEARTBEAT':
            if msg.custom_mode not in (MODE_ID, RAMP_MODE_ID, LEGACY_MODE_ID) or (
                    firmware_mode is not None and msg.custom_mode != firmware_mode):
                raise RuntimeError('Connected firmware is not the expected integrated DShot IMU image')
            firmware_mode = msg.custom_mode
        if msg.get_type() == 'NAMED_VALUE_INT':
            name = msg.name.decode(errors='replace') if isinstance(msg.name, bytes) else str(msg.name)
            reports[name.rstrip('\0')] = (msg.value, time.monotonic())
        return msg

    def wait_ack(number, *, heartbeat=True):
        deadline = time.monotonic()+1
        while time.monotonic() < deadline:
            msg = receive(heartbeat=heartbeat and not stop_only)
            if msg and msg.get_type() == 'COMMAND_ACK' and msg.command == number:
                if msg.result != mavutil.mavlink.MAV_RESULT_ACCEPTED:
                    raise RuntimeError(f'Command {number} rejected: MAV_RESULT {msg.result}')
                return
        raise TimeoutError(f'No acknowledgement for {number}; command will not be retried')

    try:
        deadline = time.monotonic()+6
        while time.monotonic() < deadline:
            msg = receive()
            if msg and msg.get_type() == 'HEARTBEAT':
                identified = True
                break
        if not identified:
            raise TimeoutError('Integrated DShot firmware heartbeat not received')
        if stop_only:
            command(ENABLE_COMMAND, [0]*7)
            wait_ack(ENABLE_COMMAND, heartbeat=False)
            print('Zero-throttle request acknowledged; physical motor stop is not measured.')
            return
        if firmware_mode == LEGACY_MODE_ID and (request['limit_percent'] > 5 or request['limit_duration_ms'] > 500):
            raise RuntimeError('Extended operator limits require v5 or v6 firmware; legacy firmware remains capped')
        deadline = time.monotonic()+3.1
        while time.monotonic() < deadline:
            receive(heartbeat=True)
        now = time.monotonic()
        for name, expected in [('DS_ALLOW', 1), ('DS_OUTPUT', 1), ('DS_FAULT', 0), ('IMU_OK', 1),
                               ('DS_ACTIVE', 0), ('M1_DS', 0), ('M2_DS', 0),
                               ('M3_DS', 0), ('M4_DS', 0)]:
            value, at = reports.get(name, (None, -math.inf))
            if value != expected or now-at > 1.5:
                raise RuntimeError(f'Fresh ready status required: {name}')
        if firmware_mode in (MODE_ID, RAMP_MODE_ID):
            revision = 6 if firmware_mode == RAMP_MODE_ID else 5
            capabilities = [('FW_REV',revision), ('DS_MAXP',100), ('DS_MAXMS',60000), ('FLT_READY',0)]
            if firmware_mode == RAMP_MODE_ID:
                capabilities.append(('DS_RAMPMS', RAMP_MS))
            for name, expected in capabilities:
                value, at = reports.get(name, (None, -math.inf))
                if value != expected or now-at > 1.5:
                    raise RuntimeError(f'Fresh v{revision} capability required: {name}')
        if request['motor'] == 'all':
            capable, at = reports.get('DS_ALL', (0, -math.inf))
            if capable != 1 or now-at > 1.5:
                raise RuntimeError('Firmware must report fresh all-four test capability')
        token, at = reports.get('DS_TOKEN', (0, -math.inf))
        if not 1 <= token <= 16777215 or now-at > 1.5:
            raise RuntimeError('Fresh single-use token required')
        if firmware_mode == RAMP_MODE_ID:
            print(f"V6 target: {request['percent']:g}% requested input; fixed {RAMP_MS} ms startup ramp "
                  f"is included in the {request['duration_ms']} ms run. Stop remains immediate.")
            if request['duration_ms'] <= RAMP_MS:
                print('This run ends before the target is reached; its duration will not be extended.')
        limits = ([request['limit_percent'], request['limit_duration_ms']]
                  if firmware_mode in (MODE_ID, RAMP_MODE_ID) else [0,0])
        command(ENABLE_COMMAND, [1, token, *limits, 0, 0, 0])
        wait_ack(ENABLE_COMMAND)
        all_motors = request['motor'] == 'all'
        test_command = TEST_ALL_COMMAND if all_motors else TEST_COMMAND
        command(test_command, [4 if all_motors else request['motor'], request['percent'],
                               request['duration_ms'], token, 0, 0, 0])
        wait_ack(test_command)
        deadline = time.monotonic()+request['duration_ms']/1000
        while time.monotonic() < deadline:
            receive(heartbeat=True)
        command(ENABLE_COMMAND, [0]*7)
        wait_ack(ENABLE_COMMAND, heartbeat=False)
        stopped_at = time.monotonic()
        stop_fields = ['DS_ACTIVE', 'DS_FAULT', 'M1_DS', 'M2_DS', 'M3_DS', 'M4_DS']
        deadline = time.monotonic()+2
        while time.monotonic() < deadline:
            receive()  # no further keepalives after stop
            if all(reports.get(k, (None, -math.inf))[0] == 0 and
                   reports[k][1] > stopped_at for k in stop_fields):
                break
        else:
            raise RuntimeError('Stop acknowledged but fresh zero/fault-free status not confirmed; check physical stop')
        print('Bounded test and stop acknowledged; fresh zero inputs confirmed. Physical stop/RPM require observation.')
    finally:
        if identified:
            try:
                command(ENABLE_COMMAND, [0]*7)
            except Exception:
                pass
        link.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--port')
    selection = ap.add_mutually_exclusive_group()
    selection.add_argument('--motor', type=int, default=1)
    selection.add_argument('--all-motors', action='store_true',
                           help='explicit simultaneous test after individual stop checks')
    ap.add_argument('--percent', type=float, default=1,
                    help='requested input target, not measured RPM; v6 ramps toward it over five seconds')
    ap.add_argument('--duration-ms', type=int, default=200,
                    help='total run duration including the mandatory startup ramp on v6')
    ap.add_argument('--limit-percent', type=float, default=5,
                    help='explicit operator envelope for this one test (default 5, hardware range up to 100)')
    ap.add_argument('--limit-duration-ms', type=int, default=500,
                    help='fixed run envelope for this one test (default 500, up to 60000 ms)')
    ap.add_argument('--stop', action='store_true')
    ap.add_argument('--execute', action='store_true')
    ap.add_argument('--bench-ready', action='store_true')
    ap.add_argument('--individual-stops-checked', action='store_true',
                    help='operator observed correct motion and stop for each motor')
    args = ap.parse_args()
    try:
        request = None if args.stop else plan('all' if args.all_motors else args.motor,
                                              args.percent, args.duration_ms,
                                              limit_percent=args.limit_percent,
                                              limit_duration_ms=args.limit_duration_ms)
        if not args.execute:
            print(json.dumps(dict(dry_run=True, protocol='DShot300',
                                  startup_profile='v5 immediate; v6 fixed 5000 ms ramp included in duration',
                                  request=request or dict(action='stop')), indent=2))
            return
        if not args.port:
            ap.error('--execute requires --port')
        if not args.stop and not args.bench_ready:
            ap.error('a motor test requires --bench-ready')
        execute(args.port, request, args.stop, bench_ready=args.bench_ready,
                individual_stops_checked=args.individual_stops_checked)
    except (ValueError, RuntimeError, TimeoutError, ImportError, OSError) as exc:
        ap.exit(1, f'{exc}\n')
    except KeyboardInterrupt:
        ap.exit(130, 'Interrupted; zero requested if the integrated firmware was identified.\n')


if __name__ == '__main__':
    main()

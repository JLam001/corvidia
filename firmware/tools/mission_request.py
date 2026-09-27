#!/usr/bin/env python3
"""Preview operator mission settings and LLM actions as MAVLink; never opens serial."""
import argparse
import json
import math

import motion_request

MAX_ID = 16777215


def integer(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{name} must be an integer in [{low}, {high}]')
    return value


def settings(value):
    fields = {'nominal_collective', 'motor_ceiling', 'duration_ms'}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f'operator settings require exactly {sorted(fields)}')
    for key in ('nominal_collective', 'motor_ceiling'):
        v = value[key]
        if type(v) not in (int, float) or not 0 < v <= 1 or not math.isfinite(v):
            raise ValueError(f'{key} must be finite and in (0, 1]')
    if value['nominal_collective'] >= value['motor_ceiling']:
        raise ValueError('nominal collective must leave correction headroom below the ceiling')
    integer(value['duration_ms'], 1, 1800000, 'duration_ms')
    return dict(value)


def intent(value, sequence):
    if not isinstance(value, dict):
        raise ValueError('intent must be an object')
    if value == {'action': 'stop_mission'}:
        return dict(value)
    if set(value) != {'action', 'amount', 'duration_ms'}:
        raise ValueError('LLM movement fields: action, amount, duration_ms only')
    return motion_request.validate(value | {'sequence': integer(sequence, 0, MAX_ID, 'sequence')})


def preview(config, proposal, session_id=1, sequence=1):
    """Encode candidate packets for offline verification; no transmission API."""
    from pymavlink import mavutil
    cfg = settings(config)
    session_id = integer(session_id, 1, MAX_ID, 'session_id')
    r = intent(proposal, sequence)
    result = {'execution_available': False, 'settings': cfg, 'intent': r,
              'session_id': session_id, 'airborne_fallback_implemented': False}
    if r['action'] == 'stop_mission':
        result['supervisor_operation'] = 'end_session_and_request_flight_fallback'
        result['packets'] = []
        return result

    def packet(system, component, command, params):
        encoder = mavutil.mavlink.MAVLink(None, srcSystem=system, srcComponent=component)
        msg = encoder.command_long_encode(1, 1, command, 0, *params)
        return {'command': command, 'source': [system, component], 'params': params,
                'hex': msg.pack(encoder).hex()}

    result['packets'] = [
        packet(255, 190, mavutil.mavlink.MAV_CMD_USER_5,
               [session_id, cfg['nominal_collective'], cfg['motor_ceiling'], cfg['duration_ms'], 0, 0, 0]),
        packet(42, 191, mavutil.mavlink.MAV_CMD_USER_4,
               [motion_request.ACTIONS.index(r['action']), r['amount'], r['duration_ms'],
                session_id, r['sequence'], 0, 0]),
    ]
    result['attitude_demand'] = motion_request.preview(r)['attitude_demand']
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--settings', required=True, help='operator configuration JSON file')
    ap.add_argument('--intent', required=True, help='LLM intent JSON string')
    ap.add_argument('--session-id', type=int, default=1)
    ap.add_argument('--sequence', type=int, default=1)
    args = ap.parse_args()
    try:
        with open(args.settings) as f:
            config = json.load(f)
        print(json.dumps(preview(config, json.loads(args.intent), args.session_id, args.sequence),
                         indent=2, allow_nan=False))
    except (ValueError, OSError, ImportError) as exc:
        ap.exit(1, f'{exc}\n')


if __name__ == '__main__':
    main()

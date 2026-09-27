#!/usr/bin/env python3
"""Capture one held frame pose over MAVLink. Reads USB only; sends no commands."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time

import serial
from pymavlink.dialects.v20 import common as mavlink


def capture(port, label, seconds):
    parser = mavlink.MAVLink(None)
    parser.robust_parsing = True
    start = time.monotonic()
    records = []
    counts = Counter()
    with serial.Serial(port, 115200, timeout=.1, exclusive=True) as stream:
        while time.monotonic() - start < seconds:
            data = stream.read(min(max(stream.in_waiting, 1), 4096))
            for msg in parser.parse_buffer(data) or []:
                if msg.get_type() == 'BAD_DATA':
                    counts['BAD_DATA'] += 1
                    continue
                if (msg.get_srcSystem(), msg.get_srcComponent()) != (1, 1):
                    continue
                counts[msg.get_type()] += 1
                if msg.get_type() in ('ATTITUDE', 'ATTITUDE_QUATERNION', 'HEARTBEAT', 'NAMED_VALUE_INT'):
                    records.append(dict(received_s=time.monotonic()-start, message=msg.to_dict()))
    attitudes = [r['message'] for r in records if r['message']['mavpackettype'] == 'ATTITUDE']
    named = {r['message']['name']: r['message']['value'] for r in records
             if r['message']['mavpackettype'] == 'NAMED_VALUE_INT'}
    ranges = {}
    for field in ('roll', 'pitch', 'yaw', 'rollspeed', 'pitchspeed', 'yawspeed'):
        values = [math.degrees(a[field]) for a in attitudes if math.isfinite(a[field])]
        if values:
            ranges[field] = dict(min=min(values), max=max(values), mean=sum(values)/len(values))
    return dict(label=label, recorded_utc=datetime.now(timezone.utc).isoformat(),
                read_only=True, port=port, seconds=time.monotonic()-start,
                counts=dict(counts), latest_named=named,
                sensor_euler_degrees_and_rates_dps=ranges, records=records)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--port', required=True)
    ap.add_argument('--label', required=True)
    ap.add_argument('--seconds', type=float, default=8)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    if not math.isfinite(args.seconds) or not 2 <= args.seconds <= 30:
        ap.error('--seconds must be between 2 and 30')
    if args.output.exists():
        ap.error('output already exists; choose a new capture path')
    result = capture(args.port, args.label, args.seconds)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k != 'records'}, indent=2))
    if result['counts'].get('ATTITUDE',0) < 10 or result['counts'].get('ATTITUDE_QUATERNION',0) < 10:
        raise SystemExit('Insufficient telemetry: pose not verified')


if __name__ == '__main__':
    main()

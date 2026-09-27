#!/usr/bin/env python3
"""Read-only MAVLink IMU dashboard. No heartbeats or commands are transmitted."""
import argparse
from collections import Counter, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import threading
import time

import serial
from serial.tools import list_ports
from pymavlink.dialects.v20 import common as mavlink


class Telemetry:
    def __init__(self, system=1, component=1):
        self.lock = threading.Lock()
        self.system, self.component = system, component
        self.connected = False
        self.port = None
        self.error = 'Waiting for STM32 USB'
        self.reset()

    def reset(self):
        self.attitude = None
        self.last_attitude = self.last_heartbeat = None
        self.heartbeat = None
        self.counts = Counter()
        self.samples = deque(maxlen=600)
        self.messages = deque(maxlen=15)
        self.arrivals = deque(maxlen=300)
        self.invalid = 0
        self.reports = {}
        self.ranges = {}
        self.named_values = {}

    def consume(self, message, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            if message.get_type() == 'BAD_DATA':
                self.invalid += 1
                return
            if (message.get_srcSystem(), message.get_srcComponent()) != (self.system, self.component):
                return
            kind = message.get_type()
            self.counts[kind] += 1
            if kind in ('SYS_STATUS', 'SERVO_OUTPUT_RAW', 'ATTITUDE_QUATERNION', 'AUTOPILOT_VERSION'):
                report = message.to_dict()
                numbers = [n for v in report.values() for n in (v if isinstance(v, (list, tuple)) else [v])
                           if isinstance(n, (int, float))]
                if all(math.isfinite(n) for n in numbers):
                    self.reports[kind] = dict(received=now, data=report)
                else:
                    self.invalid += 1
            if kind == 'ATTITUDE':
                values = [message.roll, message.pitch, message.yaw,
                          message.rollspeed, message.pitchspeed, message.yawspeed]
                if not all(math.isfinite(v) for v in values):
                    self.invalid += 1
                    return
                self.attitude = dict(zip(('roll', 'pitch', 'yaw', 'gx', 'gy', 'gz'),
                                         (math.degrees(v) for v in values)))
                self.attitude['boot_ms'] = message.time_boot_ms
                for key in ('roll', 'pitch', 'yaw', 'gx', 'gy', 'gz'):
                    value = self.attitude[key]
                    lo, hi = self.ranges.get(key, (value, value))
                    self.ranges[key] = [min(lo, value), max(hi, value)]
                self.last_attitude = now
                self.arrivals.append(now)
                # Limit plotted history to 20 Hz, independently of serial rate.
                if not self.samples or now - self.samples[-1][0] >= .05:
                    self.samples.append([now, *values[3:]])
            elif kind == 'HEARTBEAT':
                self.last_heartbeat = now
                self.heartbeat = dict(custom_mode=message.custom_mode,
                                      system_status=message.system_status,
                                      armed=bool(message.base_mode & 128),
                                      bench=message.custom_mode == 0x42454E43,
                                      diagnostic=message.custom_mode == 0x50574D44,
                                      dshot=message.custom_mode in (0x44533330, 0x4453494D, 0x44534935, 0x44534936),
                                      dshot_imu=message.custom_mode in (0x4453494D, 0x44534935, 0x44534936),
                                      dshot_ramp=message.custom_mode == 0x44534936)
            elif kind == 'NAMED_VALUE_INT':
                name = message.name.decode(errors='replace') if isinstance(message.name, bytes) else str(message.name)
                name = name.rstrip('\0')
                if name in {f'M{i}_{field}' for i in range(1, 5)
                            for field in ('US', 'PERIOD', 'MIN', 'MAX', 'DS')} | {
                                'IMU_OK', 'ATT_AGE', 'GYR_AGE', 'DS_FAULT', 'DS_OUTPUT',
                                'DS_ALLOW', 'DS_TOKEN', 'DS_ACTIVE', 'DS_EXPIRE', 'DS_FRAMES',
                                'IMU_INIT', 'IMU_ADDR', 'I2C_MASK',
                                'IMU_ATT', 'IMU_GYR', 'IMU_REJ', 'DS_ALL',
                                'FW_REV', 'DS_MAXP', 'DS_MAXMS', 'DS_CAP_BP',
                                'DS_CAP_MS', 'DS_RAMPMS', 'FLT_READY', 'AXES_OK'}:
                    self.named_values[name] = dict(value=message.value, received=now)
            elif kind == 'STATUSTEXT':
                self.messages.append(dict(text=str(message.text), severity=message.severity))

    def snapshot(self, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            age = None if self.last_attitude is None else now-self.last_attitude
            hb_age = None if self.last_heartbeat is None else now-self.last_heartbeat
            times = [t for t in self.arrivals if now-t <= 2]
            hz = (len(times)-1)/(times[-1]-times[0]) if len(times)>1 and times[-1]>times[0] else 0
            return dict(connected=self.connected, port=self.port, error=self.error,
                        source=f'{self.system}/{self.component}', attitude=self.attitude,
                        attitude_age=age, fresh=self.connected and age is not None and age < .5,
                        heartbeat_age=hb_age, heartbeat=self.heartbeat, hz=round(hz, 1),
                        counts=dict(self.counts), invalid=self.invalid,
                        ranges=dict(self.ranges),
                        named_values={k: dict(value=v['value'], age=now-v['received'])
                                      for k, v in self.named_values.items()},
                        reports={k: dict(age=now-v['received'], data=v['data'])
                                 for k, v in self.reports.items()},
                        messages=list(self.messages),
                        history=[[t-now, *[math.degrees(v) for v in rates]]
                                 for t, *rates in self.samples if now-t <= 15])


def detect_port():
    ports = [p.device for p in list_ports.comports()
             if (p.vid, p.pid) == (0x0483, 0x5740)]
    if len(ports) > 1:
        raise RuntimeError('Multiple STM32 ports; restart with --port')
    return ports[0] if ports else None


def read_serial(state, stop, port=None, baud=115200):
    while not stop.is_set():
        try:
            selected = port or detect_port()
            if not selected:
                stop.wait(1)
                continue
            # Parser only: unlike a GCS, this application has no transmit path.
            parser = mavlink.MAVLink(None)
            parser.robust_parsing = True
            with serial.Serial(selected, baud, timeout=.2, exclusive=True) as stream:
                with state.lock:
                    state.reset()
                    state.connected, state.port, state.error = True, selected, None
                while not stop.is_set():
                    data = stream.read(min(max(stream.in_waiting, 1), 4096))
                    if data:
                        for msg in parser.parse_buffer(data) or []:
                            state.consume(msg)
        except (OSError, serial.SerialException, RuntimeError) as exc:
            with state.lock:
                state.error = str(exc)
        finally:
            with state.lock:
                state.connected = False
        stop.wait(1)


def make_handler(state):
    page = Path(__file__).with_name('imu_dashboard.html').read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/api/state':
                data = json.dumps(state.snapshot(), allow_nan=False).encode()
                content_type = 'application/json'
            elif self.path == '/':
                data, content_type = page, 'text/html; charset=utf-8'
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_):
            pass
    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--port', help='Explicit serial port; otherwise waits for STM32 0483:5740')
    ap.add_argument('--baud', type=int, default=115200)
    ap.add_argument('--http-port', type=int, default=8765)
    ap.add_argument('--system', type=int, default=1)
    ap.add_argument('--component', type=int, default=1)
    args = ap.parse_args()
    state, stop = Telemetry(args.system, args.component), threading.Event()
    server = ThreadingHTTPServer(('127.0.0.1', args.http_port), make_handler(state))
    worker = threading.Thread(target=read_serial, args=(state, stop, args.port, args.baud), daemon=True)
    worker.start()
    print(f'IMU dashboard: http://127.0.0.1:{args.http_port} — listen only; Ctrl+C to release USB', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        worker.join(timeout=3)


if __name__ == '__main__':
    main()

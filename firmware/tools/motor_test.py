#!/usr/bin/env python3
"""Operator-only, single-motor bench test. Defaults to a JSON dry run."""
import argparse
import json
import math
import time

MODE_ID = 0x42454E43
ENABLE_COMMAND = 31010  # MAV_CMD_USER_1 (vendored common.xml enum)
MOTOR_TEST_COMMAND = 209
# September 27 hardware failure: motor 4 continued turning after stop ACKs.
# Re-enable only after measured ESC stop behavior and channel wiring are fixed.
# There is deliberately no CLI override. Dry runs and stop remain available.
SPIN_TESTS_ENABLED = False


def plan(motor: int, pulse_us: int, seconds: float) -> dict:
    if type(motor) is not int or motor not in range(1, 5):
        raise ValueError("motor must be an integer from 1 to 4")
    if type(pulse_us) is not int or not 1000 <= pulse_us <= 1100:
        raise ValueError("pulse must be an integer from 1000 to 1100 microseconds")
    if isinstance(seconds, bool) or not math.isfinite(seconds) or not 0.02 <= seconds <= 2:
        raise ValueError("duration must be between 0.02 and 2 seconds")
    return {"mode": "motor_bench", "motor": motor, "pulse_us": pulse_us,
            "seconds": seconds, "feather_pin": {1: 9, 2: 10, 3: 6, 4: 5}[motor]}


def execute(port: str, request: dict | None, stop_only: bool = False) -> None:
    if not stop_only and not SPIN_TESTS_ENABLED:
        raise RuntimeError("powered motor tests are disabled after the motor-4 stop failure; "
                           "keep ESC power disconnected and see MOTOR_CONTROL.md")
    # Imported only for execution. No serial device is opened by a dry run.
    from pymavlink import mavutil
    link = mavutil.mavlink_connection(port, baud=115200, source_system=255,
                                     source_component=190)
    identified = False
    last_heartbeat = -math.inf

    def heartbeat():
        nonlocal last_heartbeat
        if time.monotonic() - last_heartbeat >= 0.1:
            link.mav.heartbeat_send(mavutil.mavlink.MAV_TYPE_GCS,
                                    mavutil.mavlink.MAV_AUTOPILOT_INVALID, 0, 0, 0)
            last_heartbeat = time.monotonic()

    def command(number, params):
        link.mav.command_long_send(1, 1, number, 0, *params)

    def wait_ack(number):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            heartbeat()
            msg = link.recv_match(type="COMMAND_ACK", blocking=True, timeout=0.05)
            if msg and msg.get_srcSystem() == 1 and msg.get_srcComponent() == 1 and msg.command == number:
                if msg.result != mavutil.mavlink.MAV_RESULT_ACCEPTED:
                    raise RuntimeError(f"command {number} rejected (MAV_RESULT={msg.result})")
                return
        raise TimeoutError(f"no acknowledgement for {number}; no automatic retry")

    try:
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            msg = link.recv_match(type="HEARTBEAT", blocking=True, timeout=0.1)
            if msg and msg.get_srcSystem() == 1 and msg.get_srcComponent() == 1:
                if msg.custom_mode != MODE_ID:
                    raise RuntimeError("connected firmware is not the Corvidia motor-bench image")
                identified = True
                if not stop_only and msg.system_status == mavutil.mavlink.MAV_STATE_UNINIT:
                    raise RuntimeError("bench output interlock is disabled in the firmware build")
                break
        if not identified:
            raise TimeoutError("motor-bench firmware heartbeat not received")
        if stop_only:
            command(ENABLE_COMMAND, [0] * 7)
            wait_ack(ENABLE_COMMAND)
            print("Stop acknowledged.")
            return
        # Ensure minimum PWM has been present for the firmware's boot gate.
        deadline = time.monotonic() + 3.1
        while time.monotonic() < deadline:
            heartbeat()
            link.recv_match(blocking=True, timeout=0.05)
        command(ENABLE_COMMAND, [1, 0, 0, 0, 0, 0, 0])
        wait_ack(ENABLE_COMMAND)
        command(MOTOR_TEST_COMMAND, [request["motor"], 1, request["pulse_us"],
                                    request["seconds"], 1, 2, 0])
        wait_ack(MOTOR_TEST_COMMAND)
        print("Test accepted. Watch the selected motor; Ctrl-C sends stop.")
        deadline = time.monotonic() + request["seconds"]
        while time.monotonic() < deadline:
            heartbeat()
            link.recv_match(blocking=True, timeout=0.05)
        command(ENABLE_COMMAND, [0] * 7)
        wait_ack(ENABLE_COMMAND)
        print("Stop acknowledged. Motor motion itself is not measured by this tool.")
    finally:
        if identified:
            try:
                command(ENABLE_COMMAND, [0] * 7)
            except Exception:
                pass  # firmware has independent finite duration/deadman checks
        link.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port")
    parser.add_argument("--motor", type=int, default=1)
    parser.add_argument("--pulse-us", type=int, default=1050)
    parser.add_argument("--seconds", type=float, default=1)
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--bench-ready", action="store_true",
                        help="operator confirms props removed, motors secured, wiring and PWM settings checked")
    args = parser.parse_args()
    try:
        request = None if args.stop else plan(args.motor, args.pulse_us, args.seconds)
        if not args.execute:
            print(json.dumps({"dry_run": True, "request": request or {"action": "stop"}}, indent=2))
            return
        if not args.port:
            parser.error("--execute requires --port")
        if not args.stop and not args.bench_ready:
            parser.error("a motor test requires the operator's --bench-ready acknowledgement")
        execute(args.port, request, args.stop)
    except (ValueError, RuntimeError, TimeoutError, ImportError, OSError) as exc:
        parser.exit(1, f"{exc}\n")
    except KeyboardInterrupt:
        parser.exit(130, "Interrupted; stop sent if a bench controller was identified.\n")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Validate a future LLM movement request and print its attitude demand.

No serial access. This is a proposal/preview interface, not flight execution.
"""
import argparse
import json
import math

ACTIONS = ("level", "tilt_forward", "tilt_backward", "tilt_left", "tilt_right",
           "yaw_left", "yaw_right")


def validate(request: dict) -> dict:
    required = {"action", "amount", "duration_ms", "sequence"}
    if not isinstance(request, dict) or set(request) != required:
        raise ValueError(f"exactly these fields are required: {sorted(required)}")
    action = request["action"]
    if action not in ACTIONS:
        raise ValueError("unsupported movement action")
    amount = request["amount"]
    if type(amount) not in (int, float):
        raise ValueError("amount must be a finite number")
    try:
        valid_amount = math.isfinite(amount) and 0 <= amount <= (30 if action.startswith("yaw_") else 10)
    except OverflowError:
        valid_amount = False
    if not valid_amount or (action == "level" and amount != 0):
        raise ValueError("tilt limit is 10 degrees; yaw limit is 30 deg/s; level requires zero")
    if type(request["duration_ms"]) is not int or not 1 <= request["duration_ms"] <= 1000:
        raise ValueError("duration_ms must be an integer in [1, 1000]")
    if type(request["sequence"]) is not int or not 0 <= request["sequence"] <= 0xFFFFFFFF:
        raise ValueError("sequence must be an unsigned 32-bit integer")
    return dict(request)


def preview(request: dict) -> dict:
    r = validate(request)
    result = {"roll_deg": 0, "pitch_deg": 0, "yaw_rate_dps": 0}
    axes = {"tilt_forward": ("pitch_deg", -1), "tilt_backward": ("pitch_deg", 1),
            "tilt_left": ("roll_deg", -1), "tilt_right": ("roll_deg", 1),
            "yaw_left": ("yaw_rate_dps", -1), "yaw_right": ("yaw_rate_dps", 1)}
    if r["action"] in axes:
        key, sign = axes[r["action"]]
        result[key] = sign * r["amount"]
    return {"execution_available": False, "request": r, "attitude_demand": result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", help="JSON request; preview only")
    args = parser.parse_args()
    try:
        print(json.dumps(preview(json.loads(args.request)), indent=2, allow_nan=False))
    except (ValueError, TypeError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()

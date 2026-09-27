"""Terminal operator for the local stand HTTP API. No serial or model access."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
import ipaddress
import json
import math
import os
from pathlib import Path
import select
import signal
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


DEFAULT_SESSION = Path("~/.local/state/corvidia/stand-8080.json").expanduser()
DEFAULT_URL = "http://127.0.0.1:8080"
ACTIVE = {"starting", "searching", "confirming", "saving"}
TERMINAL = {"complete", "failed", "timed_out", "cancelled"}
READINESS = (
    ("guarded_stand", "Drone secured in the guarded stand; turning uses the external handle"),
    ("hands_clear", "Hands, people, and loose cables clear of the motors and propellers"),
    ("power_disconnect_accessible", "ESC power disconnect accessible outside the guarded area"),
    ("esc_startup_finished", "ESC startup tones finished"),
    ("motors_still", "All four motors still before starting"),
)
GUIDANCE = {"hold": "Hold the stand still", "search_right": "Turn the stand slowly right",
            "search_left": "Turn the stand slowly left", "right": "Turn the stand right",
            "left": "Turn the stand left"}


class ClientError(Exception):
    pass


class Cancelled(Exception):
    pass


def _url(value):
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port or 80
        loopback = parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname).is_loopback
    except (TypeError, ValueError):
        raise ClientError("Session URL must use loopback HTTP") from None
    if (parsed.scheme != "http" or not loopback or not 1 <= port <= 65535 or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        raise ClientError("Session URL must use loopback HTTP without credentials or query parameters")
    return value.rstrip("/")


@dataclass(frozen=True)
class Session:
    url: str = DEFAULT_URL
    token: str | None = field(default=None, repr=False)


def load_session(path, *, required=True):
    """Read only a private, user-owned regular file; never print its token."""
    try:
        fd = os.open(Path(path).expanduser(), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        if not required:
            return Session()
        raise ClientError("No operator session file; start the stand service first") from None
    except OSError:
        raise ClientError("Cannot open the private operator session file") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ClientError("Session file must be user-owned with permissions 0600")
        with os.fdopen(fd, "rb") as stream:
            fd = None
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise ClientError("Invalid operator session file")
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("token"), str) or not data["token"]:
            raise ClientError("Invalid operator session file")
        return Session(_url(data.get("url")), data["token"])
    except (ValueError, UnicodeError):
        raise ClientError("Invalid operator session file") from None
    finally:
        if fd is not None:
            os.close(fd)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ClientError("HTTP redirects are not allowed")


class ApiClient:
    def __init__(self, session: Session, *, timeout=.3):
        self.url, self.token, self.timeout = _url(session.url), session.token, timeout
        # Explicitly ignore proxy environment variables for the local control API.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def _request(self, path, body=None):
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            if not self.token:
                raise ClientError("An operator session is required for commands")
            headers.update({"Content-Type": "application/json", "X-Corvidia-Token": self.token})
            data = json.dumps(body, allow_nan=False).encode()
        request = urllib.request.Request(self.url + path, data=data, headers=headers)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(256 * 1024 + 1)
                if len(raw) > 256 * 1024:
                    raise ClientError("Supervisor response is too large")
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ClientError("Invalid supervisor response")
                return result
        except urllib.error.HTTPError as exc:
            # Do not print request objects, headers, redirect locations, or URLs.
            raise ClientError(f"Supervisor rejected the request (HTTP {exc.code})") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ClientError("Supervisor request timed out or connection failed; command outcome may be unknown") from None
        except (ValueError, UnicodeError):
            raise ClientError("Invalid supervisor response; command outcome may be unknown") from None

    def status(self):
        return self._request("/api/status")

    def post(self, action, body):
        return self._request("/api/" + action, body)


def settings(appearance, percent, seconds):
    if not isinstance(appearance, str) or not 1 <= len(appearance.strip()) <= 240:
        raise ClientError("Describe the person's visible appearance in 1–240 characters")
    appearance = appearance.strip()
    if any(ord(c) < 32 or ord(c) == 127 for c in appearance):
        raise ClientError("Use a single-line description without control characters")
    if type(percent) not in (int, float) or not math.isfinite(percent) or not 0 < percent <= 20:
        raise ClientError("Motor input must be greater than zero and at most 20%")
    if type(seconds) is not int or not 1 <= seconds <= 60:
        raise ClientError("Maximum duration must be 1–60 whole seconds")
    return dict(appearance=appearance, percent=float(percent), duration_ms=seconds * 1000)


class TerminalMission:
    def __init__(self, api, *, clock=time.monotonic, sleep=time.sleep, out=print,
                 stdin=None, readiness_reader=None, prepare_timeout=15., stop_timeout=4.):
        self.api, self.clock, self.sleep, self.out = api, clock, sleep, out
        self.stdin = sys.stdin if stdin is None else stdin
        self.readiness_reader = readiness_reader or self._read_readiness
        self.prepare_timeout, self.stop_timeout = prepare_timeout, stop_timeout
        self.mission_id = None
        self.lease_id = uuid.uuid4().hex
        self.cleaning = False
        self.last_display = None

    def say(self, message):
        token = getattr(self.api, "token", None)
        if token:
            message = str(message).replace(token, "[redacted]")
        self.out(str(message))

    def _read_readiness(self, prompt, deadline):
        self.say(prompt + ' — type exactly "yes":')
        remaining = deadline - self.clock()
        if remaining <= 0 or not select.select([self.stdin], [], [], remaining)[0]:
            raise ClientError("Readiness confirmation timed out; mission was not started")
        return self.stdin.readline().rstrip("\r\n")

    def _mode(self, status, hardware):
        expected = "hardware" if hardware else "observe"
        if status.get("mode") != expected:
            raise ClientError(f"Expected {expected} service; actual mode is {status.get('mode', 'unknown')}. No start sent")

    def _matches(self, status, wanted):
        return all(status.get(key) == value for key, value in wanted.items())

    def _ready(self, status):
        preflight = status.get("preflight", {})
        return (preflight.get("camera_ready") is True and preflight.get("motor_ready") is True
                and preflight.get("resource_ready", True) is True
                and preflight.get("telemetry_ready", True) is True
                and not status.get("recovery_required"))

    def _display(self, status):
        remaining = status.get("remaining_ms")
        seconds = math.ceil(max(0, remaining) / 1000) if type(remaining) in (int, float) else None
        marker = (status.get("state"), status.get("guidance"), seconds)
        if marker != self.last_display:
            guide = GUIDANCE.get(status.get("guidance"), "Hold the stand still")
            suffix = f" · {max(0, remaining) / 1000:.1f}s remaining" if type(remaining) in (int, float) else ""
            self.say(f"{status.get('state', 'unknown')}: {guide}{suffix}")
            self.last_display = marker

    def _verified(self, status):
        motor = status.get("motor", {})
        return (status.get("state") in TERMINAL and motor.get("stop_status") == "verified"
                and motor.get("zero_confirmed") is True)

    def _unstarted_cancel(self, status):
        motor = status.get("motor", {})
        return (status.get("state") == "cancelled" and motor.get("start_status") == "not_requested"
                and motor.get("stop_status") == "not_requested")

    def _stop_and_verify(self):
        """Revoke authority first. No lease renewal or new start is possible here."""
        self.cleaning = True
        deadline = self.clock() + self.stop_timeout
        try:
            self.api.post("stop", {"mission_id": self.mission_id})
        except Exception:
            self.say("Stop request was not acknowledged; checking final status without renewing the lease.")
        while self.clock() < deadline:
            try:
                status = self.api.status()
                if status.get("mission_id") != self.mission_id:
                    raise ClientError("Mission identity changed; stop cannot be verified")
                self._display(status)
                if self._verified(status) or self._unstarted_cancel(status):
                    return status
                if status.get("recovery_required") or status.get("motor", {}).get("stop_status") == "unverified":
                    break
            except (ClientError, OSError):
                pass
            self.sleep(min(.25, max(0., deadline - self.clock())))
        raise ClientError("STOP UNVERIFIED. Do not approach; use the accessible ESC power disconnect if needed")

    def _result(self, status):
        if self._unstarted_cancel(status):
            self.say("Mission cancelled before a motor start. No physical-stop claim is made.")
            return 1
        if not self._verified(status):
            raise ClientError("STOP UNVERIFIED. Fresh zero-input telemetry was not confirmed")
        self.say("Simulated zero inputs verified." if status.get("mode") == "observe" else
                 "Fresh zero-input telemetry verified. Observe physical motor stopping before approaching.")
        result = status.get("result") or {}
        evidence = result.get("evidence") or {}
        if evidence.get("path"):
            self.say("Saved evidence: " + str(evidence["path"]))
        if status.get("state") == "complete":
            if status.get("motor", {}).get("fault") or status.get("motor", {}).get("error"):
                raise ClientError("Motor interface reported a fault; this mission cannot be reported successful")
            self.say("Mission complete.")
            return 0
        self.say(f"Mission {status.get('state')}: {status.get('error') or 'no successful capture'}")
        return 1

    def run(self, appearance, percent=5, seconds=10, *, hardware=False):
        final = None
        wanted = None
        prior_id = None
        prepare_attempted = False
        primary_error = None
        interrupted = False
        try:
            wanted = settings(appearance, percent, seconds)
            initial = self.api.status()
            self._mode(initial, hardware)
            if initial.get("state") in ACTIVE | {"stopping"}:
                raise ClientError("A mission is already active; this run will not adopt or replace it")
            if not getattr(self.api, "token", None):
                raise ClientError("An operator session is required for commands")
            if hardware and not self.stdin.isatty():
                raise ClientError("Hardware readiness requires an interactive terminal; piped answers are refused")
            prior_id = initial.get("mission_id")
            prepare_attempted = True
            self.api.post("prepare", wanted)  # Exactly once, including ambiguous failures.
            deadline = self.clock() + self.prepare_timeout
            while self.clock() < deadline:
                current = self.api.status()
                self._mode(current, hardware)
                if current.get("mission_id") and current["mission_id"] != prior_id:
                    if not self._matches(current, wanted) or current.get("state") != "prepared":
                        raise ClientError("Prepared mission does not match this request; no start sent")
                    self.mission_id = current["mission_id"]
                    if self._ready(current):
                        break
                self.sleep(min(.25, max(0., deadline - self.clock())))
            else:
                raise ClientError("Preparation/preflight timed out; no start sent")
            self.say(f"Review: {wanted['appearance']} | {initial['mode']} | {percent:g}% motor input | maximum {seconds}s")
            readiness = {}
            if hardware:
                readiness_deadline = self.clock() + 120.
                for key, prompt in READINESS:
                    if self.readiness_reader(prompt, readiness_deadline) != "yes":
                        raise ClientError("Readiness was not confirmed; mission was not started")
                    if self.clock() >= readiness_deadline:
                        raise ClientError("Readiness confirmation timed out; mission was not started")
                    readiness[key] = True
            current = self.api.status()
            self._mode(current, hardware)
            if (current.get("mission_id") != self.mission_id or current.get("state") != "prepared"
                    or not self._matches(current, wanted) or not self._ready(current)):
                raise ClientError("Prepared mission or preflight changed; no start sent")
            run_deadline = self.clock() + seconds  # Never renewed by responses or leases.
            acceptance_deadline = self.clock() + 3.
            body = {"mission_id": self.mission_id, "lease_id": self.lease_id}
            if hardware:
                body["readiness"] = readiness
            self.api.post("start", body)  # Exactly once. HTTP 202 is only queue admission.
            while self.clock() < run_deadline:
                cycle = self.clock()
                current = self.api.status()
                if current.get("mission_id") != self.mission_id:
                    raise ClientError("Current mission changed; this terminal will not control another mission")
                self._mode(current, hardware)
                self._display(current)
                if current.get("state") in TERMINAL:
                    final = current
                    break
                motor = current.get("motor", {})
                if current.get("recovery_required") or motor.get("fault") or motor.get("error") or motor.get("start_status") == "failed":
                    raise ClientError("Motor interface reported a fault or failed start")
                if self.clock() >= acceptance_deadline and motor.get("start_status") != "accepted":
                    raise ClientError("Motor start was not accepted within three seconds; command will not be retried")
                if current.get("state") in ACTIVE:
                    self.api.post("lease", {"mission_id": self.mission_id, "lease_id": self.lease_id})
                elif current.get("state") == "prepared" and current.get("error"):
                    raise ClientError("Supervisor did not accept the start")
                self.sleep(max(0., min(.25 - (self.clock() - cycle), run_deadline - self.clock())))
            if final is None:
                self.say("Client time limit reached; requesting stop and final verification.")
        except (KeyboardInterrupt, Cancelled):
            interrupted = True
            primary_error = "Interrupted; ending this mission"
        except Exception as exc:
            primary_error = str(exc)
        finally:
            self.cleaning = True  # Repeated terminal signals cannot interrupt bounded cleanup.
            # An ambiguous prepare may have created an unstarted mission. Adopt
            # only a new, matching prepared ID for cancellation, never for Start.
            if self.mission_id is None and prepare_attempted and wanted is not None:
                try:
                    candidate = self.api.status()
                    if (candidate.get("mission_id") and candidate["mission_id"] != prior_id
                            and candidate.get("state") == "prepared" and self._matches(candidate, wanted)):
                        self.mission_id = candidate["mission_id"]
                except Exception:
                    pass
            if self.mission_id is not None and (final is None or not self._verified(final)):
                try:
                    final = self._stop_and_verify()
                except Exception as exc:
                    self.say(str(exc))
                    final = None
            if primary_error:
                self.say(primary_error)
        if final is not None:
            try:
                code = self._result(final)
            except ClientError as exc:
                self.say(str(exc))
                code = 1
            return 130 if interrupted else 1 if primary_error else code
        return 130 if interrupted else 1

    def stop_current(self):
        self.cleaning = True  # A stop operation itself must survive terminal termination signals.
        current = self.api.status()
        self.mission_id = current.get("mission_id")
        if not self.mission_id:
            self.say("No current mission. No physical-stop claim is made.")
            return 0
        final = self._stop_and_verify()
        self._result(final)
        return 0 if self._verified(final) or self._unstarted_cancel(final) else 1


@contextmanager
def _signals(runner):
    previous = {}

    def cancel(_signum, _frame):
        if not runner.cleaning:
            raise Cancelled()

    try:
        for name in ("SIGINT", "SIGTERM", "SIGHUP"):
            sig = getattr(signal, name, None)
            if sig is not None:
                previous[sig] = signal.signal(sig, cancel)
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=Path, default=DEFAULT_SESSION)
    sub = parser.add_subparsers(dest="command", required=True)
    status_parser = sub.add_parser("status", help="Read status without sending a command")
    status_parser.add_argument("--json", action="store_true", help="Print full diagnostic status")
    sub.add_parser("stop", help="Stop the current mission and verify zero-input status")
    run = sub.add_parser("run", help="Prepare and execute one bounded mission")
    run.add_argument("appearance")
    run.add_argument("--percent", type=float, default=5)
    run.add_argument("--seconds", type=int, default=10)
    run.add_argument("--hardware", action="store_true", help="Require a hardware service and interactive readiness")
    args = parser.parse_args(argv)
    try:
        session = load_session(args.session, required=args.command != "status")
        api = ApiClient(session)
        runner = TerminalMission(api)
        if args.command == "status":
            value = api.status()
            if args.json:
                runner.say(json.dumps(value, indent=2))
            else:
                preflight, motor = value.get("preflight", {}), value.get("motor", {})
                runner.say(f"Mode: {value.get('mode', 'unknown')} | state: {value.get('state', 'unknown')} | operator: {value.get('operator', 'none')}")
                runner.say(f"Camera: {'ready' if preflight.get('camera_ready') else 'not ready'} | frame age: {value.get('perception', {}).get('frame_age_ms', 'unknown')} ms")
                runner.say(f"IMU: {value.get('telemetry_mode', 'unknown')} | ready: {bool(preflight.get('telemetry_ready'))} | RX age: {preflight.get('telemetry_rx_age_ms', 'unknown')} ms")
                runner.say(f"Motor input requested: {motor.get('requested_percent')}% | zero inputs confirmed: {bool(motor.get('zero_confirmed'))} | simulated: {value.get('mode') == 'observe'}")
                runner.say(f"Memory: {preflight.get('memory_available_mib', 'unknown')} MiB available | ready: {bool(preflight.get('resource_ready'))}")
                for error in (value.get('error'), preflight.get('resource_error'), preflight.get('telemetry_error')):
                    if error:
                        runner.say("Error: " + str(error))
                evidence = (value.get("result") or {}).get("evidence") or {}
                if evidence.get("path"):
                    runner.say("Saved evidence: " + str(evidence["path"]))
            return 0
        with _signals(runner):
            if args.command == "stop":
                return runner.stop_current()
            return runner.run(args.appearance, args.percent, args.seconds, hardware=args.hardware)
    except (ClientError, Cancelled, KeyboardInterrupt) as exc:
        print(str(exc) or "Interrupted", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

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

from .mission_prompt import parse_mission_prompt


DEFAULT_SESSION = Path("~/.local/state/corvidia/stand-8080.json").expanduser()
DEFAULT_URL = "http://127.0.0.1:8080"
ACTIVE = {"preparing", "starting", "searching", "confirming", "saving"}
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

    def get_image(self, path, *, authenticated=False):
        """Read a bounded image from this same loopback supervisor."""
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
            raise ClientError("Image path must be relative to the supervisor")
        headers = {"Accept": "image/jpeg, image/png"}
        if authenticated:
            if not self.token:
                raise ClientError("An operator session is required for saved evidence")
            headers["X-Corvidia-Token"] = self.token
        request = urllib.request.Request(self.url + path, headers=headers)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if response.headers.get_content_type() not in ("image/jpeg", "image/png"):
                    raise ClientError("Supervisor did not return a JPEG or PNG image")
                data = response.read(10 * 1024 * 1024 + 1)
                if not data or len(data) > 10 * 1024 * 1024:
                    raise ClientError("Supervisor image is empty or too large")
                return data
        except urllib.error.HTTPError as exc:
            raise ClientError(f"Image request rejected (HTTP {exc.code})") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ClientError("Image request timed out or connection failed") from None


def settings(appearance):
    try:
        parsed = parse_mission_prompt(appearance)
    except ValueError as exc:
        raise ClientError(str(exc)) from None
    return dict(appearance=parsed.prompt)


class TerminalMission:
    def __init__(self, api, *, clock=time.monotonic, sleep=time.sleep, out=print,
                 stdin=None, readiness_reader=None, stop_timeout=4.):
        self.api, self.clock, self.sleep, self.out = api, clock, sleep, out
        self.stdin = sys.stdin if stdin is None else stdin
        self.readiness_reader = readiness_reader or self._read_readiness
        self.stop_timeout = stop_timeout
        self.mission_id = None
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
            raise ClientError(f"Expected {expected} service; actual mode is {status.get('mode', 'unknown')}")

    def _onboard(self, status):
        if (status.get("control_authority") != "onboard" or status.get("lease_required") is not False
                or status.get("mission_api") != 2):
            raise ClientError("The service must advertise onboard mission API 2 before submitting a mission")

    def _display(self, status):
        remaining = status.get("remaining_ms")
        seconds = math.ceil(max(0, remaining) / 1000) if type(remaining) in (int, float) else None
        count = status.get("capture_count", 0)
        count = count if type(count) is int and count >= 0 else 0
        collecting = status.get("completion_mode") == "timed_collection"
        marker = (status.get("state"), status.get("guidance"), seconds, count)
        if marker != self.last_display:
            guide = GUIDANCE.get(status.get("guidance"), "Hold the stand still")
            suffix = f" · {max(0, remaining) / 1000:.1f}s remaining" if type(remaining) in (int, float) else ""
            if collecting:
                suffix += f" · {count} capture{'s' if count != 1 else ''} saved"
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
        """Explicitly request stop, then verify its result without starting anything."""
        self.cleaning = True
        deadline = self.clock() + self.stop_timeout
        try:
            self.api.post("stop", {"mission_id": self.mission_id})
        except Exception:
            self.say("Stop request was not acknowledged; checking final status.")
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
            if status.get("completion_mode") == "timed_collection":
                count = status.get("capture_count", 0)
                self.say(f"Timed search complete: {count} capture{'s' if count != 1 else ''} saved.")
            else:
                self.say("Mission complete.")
            return 0
        fallback = "timed search ended early" if status.get("completion_mode") == "timed_collection" else "no successful capture"
        self.say(f"Mission {status.get('state')}: {status.get('error') or fallback}")
        return 1

    def run(self, appearance, *, hardware=False):
        """Submit once; all execution and deadlines belong to the Jetson."""
        self.cleaning = False
        self.mission_id = None
        submitted = receipt = interrupted = False
        error = None
        try:
            wanted = settings(appearance)
            initial = self.api.status()
            self._mode(initial, hardware)
            self._onboard(initial)
            if initial.get("state") in ACTIVE | {"stopping"}:
                raise ClientError("A mission is already active; this run will not adopt or replace it")
            if not getattr(self.api, "token", None):
                raise ClientError("An operator session is required for commands")
            if initial.get("recovery_required"):
                raise ClientError("The Jetson requires physical recovery before another mission")
            if hardware and not self.stdin.isatty():
                raise ClientError("Hardware readiness requires an interactive terminal; piped answers are refused")
            prior_id = initial.get("mission_id")
            self.say(f"Mission: {wanted['appearance']} | {initial['mode']} | onboard demo profile")
            readiness = {}
            if hardware:
                readiness_deadline = self.clock() + 120.
                for key, prompt in READINESS:
                    if self.readiness_reader(prompt, readiness_deadline) != "yes":
                        raise ClientError("Readiness was not confirmed; no mission was submitted")
                    if self.clock() >= readiness_deadline:
                        raise ClientError("Readiness confirmation timed out; no mission was submitted")
                    readiness[key] = True
            current = self.api.status()
            self._mode(current, hardware)
            self._onboard(current)
            if (current.get("mission_id") != prior_id or current.get("state") in ACTIVE | {"stopping"}
                    or current.get("recovery_required")):
                raise ClientError("Mission state or recovery requirement changed; no mission was submitted")
            body = dict(wanted)
            if hardware:
                body["readiness"] = readiness
            # A response can be lost after acceptance. Never retry, infer an ID
            # from matching text, or automatically Stop on an ambiguous result.
            submitted = True
            reply = self.api.post("mission", body)
            mid = reply.get("mission_id")
            if (reply.get("accepted") is not True or not isinstance(mid, str)
                    or not 1 <= len(mid) <= 128 or mid == prior_id):
                raise ClientError("Mission response did not identify a newly accepted request")
            self.mission_id = mid
            receipt = True
            self.say(f"Mission {self.mission_id} submitted to the Jetson.")
            self.say("The Jetson performs preflight and runs the fixed demo profile independently.")
            self.say("Submission does not confirm motor motion. Use 'watch' or 'status' for the outcome; 'stop' ends the mission.")
            return 0
        except (KeyboardInterrupt, Cancelled):
            interrupted = True
            error = "Terminal operation interrupted"
        except Exception as exc:
            error = str(exc)
        finally:
            self.cleaning = True
            if error:
                self.say(error)
                if submitted:
                    outcome = "Submitted mission status needs checking" if receipt else "Mission submission outcome unknown"
                    identity = f" for mission {self.mission_id}" if self.mission_id else ""
                    self.say(f"{outcome}{identity}. No retry or Stop was sent.")
                    self.say("The Jetson may continue until its fixed deadline. Use 'status' or an explicit 'stop'.")
        return 130 if interrupted else 1

    def watch(self):
        """Display progress only. Losing or closing this display changes no authority."""
        self.cleaning = False
        self.say("Read-only watch. Closing this display does not stop an onboard mission.")
        try:
            while True:
                cycle = self.clock()
                current = self.api.status()
                self._display(current)
                if current.get("state") in TERMINAL:
                    return self._result(current)
                self.sleep(max(0., .25 - (self.clock() - cycle)))
        except (KeyboardInterrupt, Cancelled):
            self.say("Watch ended; an active Jetson mission continues. No Stop command was sent.")
            return 130
        except Exception as exc:
            self.say(str(exc))
            self.say("Watch disconnected. Mission authority remains on the Jetson; use 'status' or explicit 'stop'.")
            return 1

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
    sub.add_parser("watch", help="Read-only guidance; exit without stopping the mission")
    run = sub.add_parser("run", help="Submit a first-match or timed person-search mission to the Jetson")
    run.add_argument("appearance")
    run.add_argument("--hardware", action="store_true", help="Require a hardware service and interactive readiness")
    args = parser.parse_args(argv)
    try:
        session = load_session(args.session, required=args.command not in ("status", "watch"))
        api = ApiClient(session)
        runner = TerminalMission(api)
        if args.command == "status":
            value = api.status()
            if args.json:
                runner.say(json.dumps(value, indent=2))
            else:
                preflight, motor = value.get("preflight", {}), value.get("motor", {})
                runner.say(f"Mode: {value.get('mode', 'unknown')} | state: {value.get('state', 'unknown')} | mission owner: {value.get('mission_owner', 'unknown')}")
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
            if args.command == "watch":
                return runner.watch()
            return runner.run(args.appearance, hardware=args.hardware)
    except (ClientError, Cancelled, KeyboardInterrupt) as exc:
        print(str(exc) or "Interrupted", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

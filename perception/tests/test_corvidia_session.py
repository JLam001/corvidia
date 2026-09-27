"""Mocked Xorg-session launcher tests: no X server, services, or hardware."""

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

import pytest


DEPLOY = Path(__file__).parents[1] / "deploy"
pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="Session deliberately refuses root login")


@pytest.fixture
def launch(tmp_path):
    # Spaces in the checkout also exercise shell quoting of paths and arguments.
    project = tmp_path / "release with spaces" / "perception"
    deploy, venv, mockbin = project / "deploy", project / ".venv/bin", tmp_path / "bin"
    for path in (deploy, venv, mockbin):
        path.mkdir(parents=True)
    for name in ("corvidia-session.sh", "corvidia-openbox.xml"):
        shutil.copyfile(DEPLOY / name, deploy / name)
    (deploy / "corvidia-session.sh").chmod(0o755)
    wrapper = tmp_path / "public-session-entry"
    wrapper.write_text((DEPLOY / "corvidia-session-wrapper.sh.in").read_text().replace(
        "@CORVIDIA_PRIVATE_SESSION_LAUNCHER@", str(deploy / "corvidia-session.sh")))
    wrapper.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    fake = tmp_path / "mock-command"
    fake.write_text(f"#!{sys.executable}\n" + '''
import json, os, signal, sys, time
from pathlib import Path
name=Path(sys.argv[0]).name
def record(event, **extra):
    with open(os.environ["MOCK_LOG"],"a") as out:
        out.write(json.dumps(dict(event=event, name=name, args=sys.argv[1:], pid=os.getpid(), **extra))+"\\n")
record("call")
if name == "systemctl":
    assert sys.argv[1:] == ["--user", "is-active", "--quiet", "corvidia-stand.service"]
    sys.exit(int(os.environ.get("SERVICE_STATUS", "0")))
if name == "loginctl":
    assert sys.argv[1] == "show-user" and sys.argv[3:] == ["--property=Linger", "--value"]
    print(os.environ.get("LINGER", "yes"))
elif name == "python":
    sys.stdin.read()
    if os.environ.get("PROBE_ERROR"):
        print(os.environ["PROBE_ERROR"], file=sys.stderr); sys.exit(1)
    print(os.environ.get("SERVICE_MODE", "observe"))
elif name == "openbox":
    if os.environ.get("WM_FAIL"): sys.exit(1)
    def stopped(signum, frame):
        record("terminated", signum=signum); sys.exit(0)
    signal.signal(signal.SIGTERM, stopped)
    while True: time.sleep(.01)
elif name == "corvidia-console":
    if os.environ.get("GUI_WAIT"):
        def stopped(signum, frame):
            record("terminated", signum=signum); sys.exit(0)
        signal.signal(signal.SIGTERM, stopped)
        while True: time.sleep(.01)
    sys.exit(int(os.environ.get("GUI_STATUS", "0")))
elif name != "xterm":
    raise AssertionError("Unexpected command: " + name)
''')
    fake.chmod(0o755)
    for name in ("systemctl", "loginctl", "openbox", "xterm"):
        (mockbin / name).symlink_to(fake)
    for name in ("python", "corvidia-console"):
        (venv / name).symlink_to(fake)
    env = {**os.environ, "PATH": str(mockbin) + ":/usr/bin:/bin", "MOCK_LOG": str(log),
           "DISPLAY": ":mock-display", "XAUTHORITY": "/mock/authority", "XDG_SESSION_TYPE": "x11"}

    def run(overrides=None, *, background=False, public_wrapper=False):
        args = [str(wrapper)] if public_wrapper else ["bash", str(deploy / "corvidia-session.sh")]
        options = dict(env={**env, **(overrides or {})}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        result = subprocess.Popen(args, **options) if background else subprocess.run(args, timeout=5, **options)
        return result

    def records():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    return run, records


@pytest.mark.parametrize("mode,arguments", [("observe", []), ("hardware", ["--hardware"])])
def test_existing_service_selects_one_console_without_backend_lifecycle_calls(launch, mode, arguments):
    run, records = launch
    result = run({"SERVICE_MODE": mode})
    assert result.returncode == 0, result.stderr
    calls = records()
    assert [call["args"] for call in calls if call["event"] == "call" and call["name"] == "corvidia-console"] == [arguments]
    wm = next(call for call in calls if call["event"] == "call" and call["name"] == "openbox")
    assert wm["args"][0] == "--config-file" and wm["args"][1].endswith("/deploy/corvidia-openbox.xml")
    assert not any("--replace" in call["args"] for call in calls)
    assert [(call["name"], call["pid"]) for call in calls if call["event"] == "terminated"] == [("openbox", wm["pid"])]
    assert not any(call["name"] == "xterm" for call in calls)


@pytest.mark.parametrize("overrides,message", [
    ({"SERVICE_STATUS": "3"}, "must already be running"),
    ({"LINGER": "no"}, "lingering must already be enabled"),
    ({"PROBE_ERROR": "The service is in read-only telemetry mode."}, "read-only telemetry mode"),
    ({"SERVICE_MODE": "telemetry"}, "mode could not be verified"),
    ({"WM_FAIL": "1"}, "Openbox could not start"),
])
def test_failed_preflight_opens_visible_explanation_and_never_launches_console(launch, overrides, message):
    run, records = launch
    result = run(overrides)
    assert result.returncode == 1 and message in result.stderr
    calls = records()
    assert not any(call["name"] == "corvidia-console" for call in calls)
    xterm = next(call for call in calls if call["name"] == "xterm")
    assert message in xterm["args"][-1]
    assert "normal desktop" in xterm["args"][-3]


def test_gui_failure_is_not_restarted_and_keeps_backend_unchanged(launch):
    run, records = launch
    result = run({"GUI_STATUS": "7"})
    assert result.returncode == 1
    calls = records()
    assert sum(call["name"] == "corvidia-console" for call in calls) == 1
    assert any(call["name"] == "xterm" and "status 7" in call["args"][-1] for call in calls)
    assert sum(call["name"] == "systemctl" for call in calls) == 1


def test_public_entry_executes_private_launcher_with_quoted_checkout_path(launch):
    run, records = launch
    result = run({"SERVICE_MODE": "hardware"}, public_wrapper=True)
    assert result.returncode == 0, result.stderr
    assert [call["args"] for call in records() if call["name"] == "corvidia-console"] == [["--hardware"]]


def test_session_signal_terminates_only_owned_gui_and_window_manager(launch):
    run, records = launch
    process = run({"GUI_WAIT": "1"}, background=True)
    try:
        deadline = time.monotonic() + 3
        while not any(call["name"] == "corvidia-console" for call in records()):
            assert time.monotonic() < deadline
            time.sleep(.01)
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=3)
        assert process.returncode == 143
        calls = records()
        assert {call["name"] for call in calls if call["event"] == "terminated"} == {"corvidia-console", "openbox"}
        assert sum(call["name"] == "systemctl" for call in calls) == 1
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_openbox_keeps_close_controls_and_maximizes_only_main_console():
    tree = ET.parse(DEPLOY / "corvidia-openbox.xml")
    ns = {"o": "http://openbox.org/3.4/rc"}
    layout = tree.findtext("o:theme/o:titleLayout", namespaces=ns)
    assert "C" in layout and "I" not in layout
    maximized = tree.findall("o:applications/o:application[o:maximized]", ns)
    assert len(maximized) == 1 and maximized[0].get("title") == "Corvidia · Search & Rescue"
    assert tree.find("o:mouse/o:context[@name='Close']/o:mousebind/o:action[@name='Close']", ns) is not None


def test_session_template_preserves_normal_login_and_requires_explicit_registration():
    text = (DEPLOY / "corvidia-session.desktop.in").read_text()
    assert text.count("@CORVIDIA_PUBLIC_SESSION_WRAPPER@") == 2
    assert "@CORVIDIA_PRIVATE_SESSION_LAUNCHER@" not in text
    rendered = text.replace("@CORVIDIA_PUBLIC_SESSION_WRAPPER@", "/usr/local/bin/corvidia-session")
    assert "TryExec=/usr/local/bin/corvidia-session" in rendered
    assert "Type=Application" in text
    assert "autologin" not in text.lower() and "gnome-session" not in text

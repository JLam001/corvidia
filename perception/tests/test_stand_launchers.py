"""Launcher checks replace every service, network, and GUI command with a fake."""

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest


DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


def executable(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)


@pytest.fixture
def console(tmp_path):
    project = tmp_path / "project"
    script = project / "deploy/stand-console.sh"
    script.parent.mkdir(parents=True)
    shutil.copyfile(DEPLOY / script.name, script)
    log = tmp_path / "calls"
    env = dict(os.environ, PATH=f"{tmp_path / 'bin'}:{os.environ['PATH']}",
               TEST_LOG=str(log), TEST_STATE="active", TEST_PROBE="0")
    executable(tmp_path / "bin/systemctl", """#!/bin/sh
if [ "$2" = show-environment ]; then exit 0; fi
if [ "$2" = is-active ]; then echo "$TEST_STATE"; exit 0; fi
echo unexpected-systemctl >> "$TEST_LOG"
exit 1
""")
    executable(project / "deploy/stand.sh", """#!/bin/sh
echo "service:$*" >> "$TEST_LOG"
""")
    executable(project / ".venv/bin/python", """#!/bin/sh
cat >/dev/null
echo "probe:$*" >> "$TEST_LOG"
exit "$TEST_PROBE"
""")
    executable(project / ".venv/bin/corvidia-console", """#!/bin/sh
echo "console:$*" >> "$TEST_LOG"
""")

    def run(*args, state="active", probe=0):
        env.update(TEST_STATE=state, TEST_PROBE=str(probe))
        result = subprocess.run(["bash", str(script), *args], env=env,
                                text=True, capture_output=True, timeout=5)
        calls = log.read_text().splitlines() if log.exists() else []
        return result, calls
    return run


def test_inactive_console_starts_only_observation_service(console):
    result, calls = console(state="inactive")
    assert result.returncode == 0
    assert calls == ["service:start observe", "probe:- observe", "console:"]


@pytest.mark.parametrize("args,mode", [((), "observe"), (("--hardware",), "hardware")])
def test_active_service_is_preserved_and_console_mode_is_explicit(console, args, mode):
    result, calls = console(*args)
    assert result.returncode == 0
    assert calls == [f"probe:- {mode}", "console:" + " ".join(args)]


def test_hardware_console_cannot_start_inactive_service(console):
    result, calls = console("--hardware", state="inactive")
    assert result.returncode != 0 and calls == []
    assert "must already be explicitly" in result.stderr


def test_probe_failure_does_not_replace_service_or_open_console(console):
    result, calls = console(probe=1)
    assert result.returncode != 0 and calls == ["probe:- observe"]


def test_shutdown_in_progress_is_not_restarted(console):
    result, calls = console(state="deactivating")
    assert result.returncode != 0 and calls == []


def test_optional_ssh_wrapper_preserves_literal_appearance_without_local_tunnel(tmp_path):
    executable(tmp_path / "ssh", f"#!{sys.executable}\n"
               "import json, os, sys\n"
               "open(os.environ['TEST_LOG'], 'w').write(json.dumps(sys.argv[1:]))\n")
    log = tmp_path / "calls.json"
    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}", TEST_LOG=str(log))
    description = "person in Jim's red shirt; $(touch unexpected)"
    result = subprocess.run(["bash", str(DEPLOY / "stand-terminal.sh"), "run", description],
                            env=env, text=True, capture_output=True, timeout=5)
    assert result.returncode == 0
    args = json.loads(log.read_text())
    assert args[0] == "-T" and "-L" not in args and "-M" not in args
    assert args[-2] == "corvidia-jetson"
    assert shlex.split(args[-1])[-2:] == ["run", description]
    assert not (tmp_path / "unexpected").exists()


def test_optional_ssh_wrapper_rejects_piped_hardware_before_network(tmp_path):
    executable(tmp_path / "ssh", "#!/bin/sh\nexit 99\n")
    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}")
    result = subprocess.run(["bash", str(DEPLOY / "stand-terminal.sh"), "run", "red shirt", "--hardware"],
                            env=env, input="yes\n" * 5, text=True, capture_output=True, timeout=5)
    assert result.returncode == 2 and "interactive terminal" in result.stderr


def test_backend_service_drops_stale_desktop_environment(tmp_path):
    project = tmp_path / "project"
    script = project / "deploy/stand.sh"
    script.parent.mkdir(parents=True)
    shutil.copyfile(DEPLOY / script.name, script)
    executable(project / ".venv/bin/python", "#!/bin/sh\ncat >/dev/null\nexit 0\n")
    executable(tmp_path / "bin/systemctl", """#!/bin/sh
case "$2" in
    show-environment) exit 0 ;;
    show) echo inactive ;;
    *) exit 99 ;;
esac
""")
    executable(tmp_path / "bin/loginctl", "#!/bin/sh\necho yes\n")
    executable(tmp_path / "bin/udevadm", "#!/bin/sh\nexit 1\n")
    executable(tmp_path / "bin/systemd-run", f"#!{sys.executable}\n"
               "import json, os, sys\n"
               "open(os.environ['TEST_LOG'], 'w').write(json.dumps(sys.argv[1:]))\n")
    log = tmp_path / "calls.json"
    env = dict(os.environ, PATH=f"{tmp_path / 'bin'}:{os.environ['PATH']}", TEST_LOG=str(log),
               DISPLAY=":99", WAYLAND_DISPLAY="stale-session", XAUTHORITY="/stale/authority")
    result = subprocess.run(["bash", str(script), "start", "observe"], env=env,
                            text=True, capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
    args = json.loads(log.read_text())
    assert "--property=UnsetEnvironment=DISPLAY WAYLAND_DISPLAY XAUTHORITY" in args
    assert "--property=Restart=no" in args
    assert args[args.index("--") + 1:] == [str(project / ".venv/bin/python"), "-m",
                                         "corvidia_perception.stand", "--mode", "observe",
                                         "--http-port", "8080"]

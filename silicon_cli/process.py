"""Process supervision — start/stop instances with an auto-restart watchdog.

Mirrors the bash watchdog: a detached supervisor process runs `python -u main.py`,
restarts it on exit (with crash-loop detection), honors a .silicon.stop sentinel,
writes .silicon.log + .silicon.pid (the pid is the *watchdog's*, so a stop signal
reaches the supervisor which then kills its child and cleans up).
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import glassagent, registry, ui
from .config import python_run_cmd

RESTART_DELAY = 5
MAX_RAPID = 5
RAPID_WINDOW = 60


def get_pid(pid_file: str) -> str | None:
    try:
        pid = Path(pid_file).read_text().strip()
        return pid or None
    except Exception:
        return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def is_running(pid_file: str) -> bool:
    pid = get_pid(pid_file)
    if not pid:
        return False
    try:
        return _alive(int(pid))
    except ValueError:
        return False


def _floater_pids(path: str, skip: int | None = None) -> list[int]:
    """PIDs of python processes running this dir's main.py (orphans)."""
    main_py = str(Path(path) / "main.py")
    try:
        out = subprocess.run(["ps", "-eo", "pid=,command="], capture_output=True, text=True).stdout
    except Exception:
        return []
    pids = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(\d+)\s+(.*)$", line)
        if not m:
            continue
        pid, cmd = int(m.group(1)), m.group(2)
        if "python" in cmd and main_py in cmd:
            if skip is not None and pid == skip:
                continue
            if pid == os.getpid():
                continue
            pids.append(pid)
    return pids


def kill_floaters(path: str, skip: int | None = None) -> None:
    for pid in _floater_pids(path, skip):
        ui.warn(f"Killing orphaned process (PID {pid}) from {path}")
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(1)
            if _alive(pid):
                os.kill(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass


# --------------------------------------------------------------- watchdog
def watchdog_loop(name: str, path: str, pid_file: str) -> None:
    """Runs as the detached `silicon _watchdog` process."""
    log_file = Path(path) / ".silicon.log"
    main_py = str(Path(path) / "main.py")
    stop_file = Path(path) / ".silicon.stop"
    py = python_run_cmd()
    child: subprocess.Popen | None = None

    def _terminate(signum=None, frame=None):
        if child and child.poll() is None:
            try:
                child.terminate()
                for _ in range(6):
                    if child.poll() is not None:
                        break
                    time.sleep(0.5)
                if child.poll() is None:
                    child.kill()
            except Exception:
                pass
        try:
            Path(pid_file).unlink()
        except OSError:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)

    restart_times: list[float] = []
    while True:
        kill_floaters(path, skip=os.getpid())
        with open(log_file, "a") as lf:
            child = subprocess.Popen([py, "-u", main_py], cwd=path, stdout=lf, stderr=subprocess.STDOUT)
            exit_code = child.wait()
        child = None

        if stop_file.exists():
            stop_file.unlink(missing_ok=True)
            Path(pid_file).unlink(missing_ok=True)
            break

        now = time.time()
        restart_times.append(now)
        cutoff = now - RAPID_WINDOW
        restart_times = [t for t in restart_times if t >= cutoff]
        if len(restart_times) >= MAX_RAPID:
            with open(log_file, "a") as lf:
                lf.write(f"[silicon-watchdog] {time.ctime()}: '{name}' crashed {MAX_RAPID} times "
                         f"in {RAPID_WINDOW}s. Giving up.\n")
            Path(pid_file).unlink(missing_ok=True)
            break

        with open(log_file, "a") as lf:
            lf.write(f"[silicon-watchdog] {time.ctime()}: '{name}' exited (code {exit_code}). "
                     f"Restarting in {RESTART_DELAY}s...\n")
        time.sleep(RESTART_DELAY)


# --------------------------------------------------------------- start/stop
def _spawn_watchdog(name: str, path: str, pid_file: str) -> int:
    """Launch the detached watchdog; return its PID."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "silicon_cli.cli", "_watchdog", path, name, pid_file],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,  # detach so it survives this CLI exiting
    )
    return proc.pid


def start_one(target: str | None) -> None:
    inst = registry.resolve_one(target)
    if is_running(inst.pid_file):
        ui.warn(f"'{inst.name}' is already running (PID {get_pid(inst.pid_file)})")
        glassagent.start(inst.path)
        return

    kill_floaters(inst.path)
    Path(inst.pid_file).unlink(missing_ok=True)
    (Path(inst.path) / ".silicon.stop").unlink(missing_ok=True)

    ui.info(f"Starting '{inst.name}' (with auto-restart)...")
    pid = _spawn_watchdog(inst.name, inst.path, inst.pid_file)
    Path(inst.pid_file).write_text(str(pid))

    time.sleep(2)
    if _alive(pid):
        ui.success(f"'{inst.name}' started (PID {pid})")
        ui.info(f"Auto-restart enabled. Logs: {inst.path}/.silicon.log")
    else:
        ui.error(f"'{inst.name}' failed to start. Check logs: {inst.path}/.silicon.log")
        Path(inst.pid_file).unlink(missing_ok=True)

    glassagent.start(inst.path)


def stop_one(target: str | None, full: bool = False) -> None:
    inst = registry.resolve_one(target)
    if not is_running(inst.pid_file):
        ui.warn(f"'{inst.name}' is not running")
        kill_floaters(inst.path)
        Path(inst.pid_file).unlink(missing_ok=True)
        (Path(inst.path) / ".silicon.stop").unlink(missing_ok=True)
        if full:
            glassagent.stop(inst.path)
        return

    pid = int(get_pid(inst.pid_file))
    (Path(inst.path) / ".silicon.stop").touch()  # tell the watchdog not to restart
    ui.info(f"Stopping '{inst.name}' (PID {pid})...")
    try:
        os.kill(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass
    for _ in range(10):
        if not _alive(pid):
            break
        time.sleep(0.5)
    if _alive(pid):
        ui.warn("Force stopping...")
        try:
            os.kill(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass

    kill_floaters(inst.path)
    Path(inst.pid_file).unlink(missing_ok=True)
    (Path(inst.path) / ".silicon.stop").unlink(missing_ok=True)
    ui.success(f"'{inst.name}' stopped")

    if full:
        glassagent.stop(inst.path)
    else:
        ui.info("Glass agent still running (use --full to stop it too).")


def _multi(target: str, verb: str, fn) -> bool:
    """Dispatch a multi-target selector. Returns True if it handled it."""
    if not (target and registry.is_multi_target(target)):
        return False
    names = registry.resolve_targets(target)
    if not names:
        ui.error("No matching installations")
        sys.exit(1)
    if target in {"all", "*"}:
        joined = ", ".join(names)
        if not ui.confirm(f"Are you sure you want to {verb} the following silicons: {joined}?"):
            return True
    for n in names:
        fn(n)
    return True


def start(target: str | None) -> None:
    if _multi(target or "", "start", start_one):
        return
    start_one(target)


def stop(target: str | None, full: bool = False) -> None:
    if _multi(target or "", "stop", lambda n: stop_one(n, full)):
        return
    stop_one(target, full)


def restart(target: str | None) -> None:
    stop(target)
    time.sleep(1)
    start(target)

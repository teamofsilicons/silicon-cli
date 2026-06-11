"""The per-silicon Glass agent (glass_agent.py) — remote control / backups.

Only relevant when the silicon dir has a .glass.json. Tracked via .glass_agent.pid.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

from . import ui
from .config import python_run_cmd


def _pid_file(path: str) -> Path:
    return Path(path) / ".glass_agent.pid"


def _read_pid(path: str) -> int | None:
    try:
        return int(_pid_file(path).read_text().strip())
    except Exception:
        return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def status(path: str) -> bool:
    pid = _read_pid(path)
    return bool(pid and _alive(pid))


def start(path: str) -> None:
    if not (Path(path) / ".glass.json").exists():
        return
    if status(path):
        return
    log = open(Path(path) / ".glass_agent.log", "a")
    proc = subprocess.Popen(
        [python_run_cmd(path), "-u", "glass_agent.py"], cwd=path,
        stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
    )
    _pid_file(path).write_text(str(proc.pid))
    ui.info(f"Glass agent started (PID {proc.pid})")


def stop(path: str) -> None:
    pid = _read_pid(path)
    if pid and _alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(1)
            if _alive(pid):
                os.kill(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        ui.info("Glass agent stopped")
    _pid_file(path).unlink(missing_ok=True)

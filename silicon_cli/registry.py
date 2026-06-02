"""~/.silicon/registry.json — the list of known silicon installations.

Same file + schema as the original bash CLI, so installs carry over unchanged:
    {"installations": [{"name", "path", "pid_file"}, ...]}
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from . import ui
from .config import REGISTRY_DIR, REGISTRY_FILE


@dataclass
class Install:
    index: int
    name: str
    path: str
    pid_file: str


def _load() -> dict:
    if REGISTRY_FILE.exists():
        try:
            return json.loads(REGISTRY_FILE.read_text())
        except Exception:
            return {"installations": []}
    return {"installations": []}


def _save(reg: dict) -> None:
    REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    REGISTRY_FILE.write_text(json.dumps(reg, indent=2))


def installs() -> list[Install]:
    reg = _load()
    out = []
    for i, inst in enumerate(reg.get("installations", [])):
        out.append(Install(i, inst["name"], inst["path"], inst.get("pid_file", "")))
    return out


def count() -> int:
    return len(_load().get("installations", []))


def register(name: str, path: str, pid_file: str | None = None) -> str:
    """Add an installation. Returns 'added' or 'exists'."""
    path = str(Path(path))
    pid_file = pid_file or str(Path(path) / ".silicon.pid")
    reg = _load()
    for inst in reg.get("installations", []):
        if inst.get("path") == path or inst.get("name") == name:
            return "exists"
    reg.setdefault("installations", []).append({"name": name, "path": path, "pid_file": pid_file})
    _save(reg)
    return "added"


def name_taken(name: str) -> bool:
    return any(i.name == name for i in installs())


def find(search: str | None = None) -> Install | None:
    """By name if given, else the install whose path contains the cwd."""
    rows = installs()
    if search:
        for i in rows:
            if i.name == search:
                return i
        return None
    cwd = os.getcwd()
    for i in rows:
        if cwd == i.path or cwd.startswith(i.path.rstrip("/") + "/"):
            return i
    return None


def is_multi_target(s: str) -> bool:
    if s == "all":
        return True
    parts = s.split(",")
    return all(p.strip().isdigit() for p in parts) and bool(parts)


def resolve_targets(selector: str) -> list[str]:
    """'all' | '1,2,4' (1-based indices) → list of install names."""
    rows = installs()
    if selector == "all":
        return [i.name for i in rows]
    out = []
    for num in selector.split(","):
        num = num.strip()
        if not num.isdigit():
            continue
        idx = int(num) - 1
        for i in rows:
            if i.index == idx:
                out.append(i.name)
    return out


def pick() -> Install:
    """Interactive picker; auto-selects when there's exactly one."""
    rows = installs()
    if not rows:
        ui.error("No silicon installations found. Run 'silicon install' first.")
        sys.exit(1)
    if len(rows) == 1:
        return rows[0]

    from .process import is_running
    sys.stderr.write(f"\n{ui.BOLD}Select a silicon instance:{ui.RESET}\n\n")
    for i in rows:
        running = is_running(i.pid_file)
        status = f"{ui.GREEN}● running{ui.RESET}" if running else f"{ui.DIM}○ stopped{ui.RESET}"
        sys.stderr.write(f"  {ui.BOLD}{i.index + 1}){ui.RESET} {i.name:<20} {status}  {ui.DIM}{i.path}{ui.RESET}\n")
    sys.stderr.write("\n")
    choice = ui.ask("Choice", "1")
    try:
        target_idx = int(choice) - 1
    except ValueError:
        ui.error("Invalid choice")
        sys.exit(1)
    for i in rows:
        if i.index == target_idx:
            return i
    ui.error("Invalid choice")
    sys.exit(1)


def resolve_one(target: str | None) -> Install:
    """For single-target commands: by name, else cwd, else interactive pick."""
    if target:
        inst = find(target)
        if not inst:
            ui.error(f"Silicon '{target}' not found")
            sys.exit(1)
        return inst
    return find() or pick()

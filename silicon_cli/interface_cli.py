"""Install the Silicon Interface CLI into a silicon folder."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from . import ui
from .config import (
    SILICON_INTERFACE_CLI_PACKAGE,
    SILICON_INTERFACE_CLI_SKIP,
    SILICON_INTERFACE_CLI_SOURCE,
    SILICON_INTERFACE_CLI_TARBALL,
)


def _node_major() -> int | None:
    node = shutil.which("node")
    if not node:
        return None
    try:
        out = subprocess.run(
            [node, "--version"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
    except Exception:
        return None
    m = re.match(r"^v?(\d+)", out)
    return int(m.group(1)) if m else None


def _source_script() -> Path | None:
    if SILICON_INTERFACE_CLI_SOURCE:
        source = Path(SILICON_INTERFACE_CLI_SOURCE).expanduser().resolve()
        if source.is_file():
            return source
        candidate = source / "bin" / "silicon-interface.mjs"
        if candidate.exists():
            return candidate
        return None

    # Local dev layout: ../silicon-interface/packages/silicon-interface-cli
    repo_root = Path(__file__).resolve().parents[2]
    candidate = (
        repo_root
        / "silicon-interface"
        / "packages"
        / "silicon-interface-cli"
        / "bin"
        / "silicon-interface.mjs"
    )
    return candidate if candidate.exists() else None


def _run(cmd: list[str], target: Path, *, warn: bool = True) -> bool:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(target),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except Exception as exc:
        if warn:
            ui.warn(f"Silicon Interface CLI setup skipped: {exc}")
        return False
    if proc.returncode == 0:
        return True
    if not warn:
        return False
    detail = (proc.stderr or proc.stdout or "").strip().splitlines()
    suffix = f": {detail[-1]}" if detail else ""
    ui.warn(f"Silicon Interface CLI setup skipped{suffix}")
    return False


def _npm_install_command(target: Path, package_spec: str) -> list[str] | None:
    npm = shutil.which("npm")
    if not npm:
        return None
    return [
        npm,
        "exec",
        "--yes",
        "--package",
        package_spec,
        "--",
        "silicon-interface",
        "install",
        str(target),
    ]


def _npm_install_commands(target: Path) -> list[list[str]]:
    package_specs = [SILICON_INTERFACE_CLI_PACKAGE]
    if (
        SILICON_INTERFACE_CLI_TARBALL
        and SILICON_INTERFACE_CLI_TARBALL not in package_specs
    ):
        package_specs.append(SILICON_INTERFACE_CLI_TARBALL)

    commands: list[list[str]] = []
    for package_spec in package_specs:
        cmd = _npm_install_command(target, package_spec)
        if cmd:
            commands.append(cmd)
    return commands


def setup(target: str | Path) -> bool:
    """Install local si/silicon-interface wrappers into ``target``.

    This is intentionally best-effort. A missing Node runtime or unpublished npm
    package should not break `silicon new` or `silicon pull`; the user can rerun
    the setup later after installing Node or setting SILICON_INTERFACE_CLI_SOURCE.
    """
    if SILICON_INTERFACE_CLI_SKIP:
        return False

    target_path = Path(target).resolve()
    major = _node_major()
    if major is None:
        ui.warn("Silicon Interface CLI setup skipped: node not found.")
        return False
    if major < 22:
        ui.warn(
            "Silicon Interface CLI setup skipped: Node 22+ is required "
            f"(found Node {major})."
        )
        return False

    script = _source_script()
    ui.info("Setting up Silicon Interface CLI...")
    if script:
        ok = _run([shutil.which("node") or "node", str(script), "install", str(target_path)], target_path)
    else:
        commands = _npm_install_commands(target_path)
        if not commands:
            ui.warn("Silicon Interface CLI setup skipped: npm not found.")
            return False
        ok = False
        for index, cmd in enumerate(commands):
            final_attempt = index == len(commands) - 1
            ok = _run(cmd, target_path, warn=final_attempt)
            if ok:
                break
            if not final_attempt:
                ui.warn(
                    "Silicon Interface CLI package lookup failed; "
                    "retrying with published tarball."
                )

    if ok:
        ui.success(
            "Silicon Interface CLI ready: "
            f"{target_path}/.silicon-interface/bin/si"
        )
    return ok

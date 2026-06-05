"""Update silicon instances from the stemcell, and self-update this CLI."""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import process, registry, stemcell, ui
from .config import CLI_SOURCE_FILE, python_run_cmd


def update_instance(target: str | None) -> None:
    if shutil.which("git") is None:
        ui.warn("git not found — some non-conflicting merges may be skipped.")

    tmp_src = tempfile.mkdtemp(prefix="silicon-update-src-")
    try:
        ui.info("Downloading latest Silicon source...")
        stemcell.download_stemcell(tmp_src)
        updater = Path(tmp_src) / "scripts" / "silicon_update.py"
        if not updater.exists():
            ui.error("Downloaded source did not include the updater script.")
            sys.exit(1)

        if target and registry.is_multi_target(target):
            names = registry.resolve_targets(target)
            if not names:
                ui.error("No matching installations")
                sys.exit(1)
            if target == "all" and not ui.confirm(f"Are you sure you want to update: {', '.join(names)}?"):
                return
            for n in names:
                _update_one(n, tmp_src, str(updater), multi=True)
            return
        _update_one(target, tmp_src, str(updater), multi=False)
    finally:
        shutil.rmtree(tmp_src, ignore_errors=True)


def _update_one(target: str | None, tmp_src: str, updater: str, multi: bool) -> None:
    inst = registry.resolve_one(target)
    if process.is_running(inst.pid_file):
        msg = f"'{inst.name}' is running. Stop it first with: silicon stop {inst.name}"
        if multi:
            ui.warn(f"Skipping '{inst.name}' — it is running.")
            return
        ui.error(msg)
        sys.exit(1)

    ui.info(f"Updating '{inst.name}' safely...")
    r = subprocess.run([python_run_cmd(), updater, "update", "--source", tmp_src, "--target", inst.path])
    if r.returncode == 0:
        ui.success(f"'{inst.name}' updated successfully")
    elif r.returncode == 2:
        ui.error(f"Update aborted — merge conflicts detected in '{inst.name}'. No files overwritten.")
        if not multi:
            sys.exit(2)
    else:
        ui.error(f"Update failed for '{inst.name}'.")
        if not multi:
            sys.exit(r.returncode)


def update_cli() -> None:
    """Reinstall/upgrade this CLI from its recorded source (pip into our venv)."""
    ui.info("Updating silicon CLI...")
    source = CLI_SOURCE_FILE.read_text().strip() if CLI_SOURCE_FILE.exists() else ""
    if not source:
        ui.warn("No install source recorded. Reinstall manually:")
        ui.info("  pip install --upgrade <path-or-git-url-to-silicon-cli>")
        return
    r = subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", source])
    if r.returncode == 0:
        ui.success("CLI updated to latest version")
    else:
        ui.error("CLI update failed.")
        sys.exit(r.returncode)


def trigger_update_check(target: str | None) -> None:
    """Trigger the local stemcell's Glass system-version check now."""
    inst = registry.resolve_one(target)
    root = Path(inst.path)
    updater = root / "update.py"
    main_py = root / "main.py"

    if updater.exists():
        cmd = [python_run_cmd(), str(updater)]
    elif main_py.exists():
        cmd = [python_run_cmd(), "main.py", "update-check"]
    else:
        ui.error(f"'{inst.name}' does not look like a stemcell with update.py or main.py.")
        sys.exit(1)

    ui.info(f"Triggering system update check for '{inst.name}'...")
    r = subprocess.run(cmd, cwd=inst.path)
    if r.returncode == 0:
        ui.success(f"Update check finished for '{inst.name}'")
    else:
        ui.error(f"Update check failed for '{inst.name}'.")
        sys.exit(r.returncode)

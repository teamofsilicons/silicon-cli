"""Glass sync — pull a silicon from a Glass server and run backups (push).

Faithful port of the bash `pull`/`push`. HTTP via stdlib urllib. Backups shell
out to the `glass` CLI (auto-installed if missing), same as the original.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from . import registry, stemcell, ui
from .config import GLASS_CLI_REPO, GLASS_SERVER_URL


def _has_glass() -> bool:
    return bool(shutil.which("glass"))


def ensure_glass_cli() -> None:
    if _has_glass():
        return
    ui.info("glass CLI not found. Installing it...")
    install_glass_cli()
    if not _has_glass():
        ui.error("glass CLI installation failed")
        sys.exit(1)


def install_glass_cli() -> None:
    glass_dir = Path.home() / ".glass"
    bin_dir = Path.home() / ".local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    archive = f"https://codeload.github.com/{GLASS_CLI_REPO}/tar.gz/refs/heads/main"
    tmp = tempfile.mkdtemp(prefix="glass-install-")
    try:
        ui.info("Installing glass CLI...")
        tarball = Path(tmp) / "glass.tar.gz"
        urllib.request.urlretrieve(archive, tarball)
        with tarfile.open(tarball) as tf:
            tf.extractall(tmp)
        src = next((p for p in Path(tmp).iterdir() if p.is_dir() and p.name.startswith("glass-")), None)
        if not src or not (src / "glass").exists():
            ui.warn("Could not auto-install glass CLI. Downloaded archive was invalid.")
            return
        shutil.rmtree(glass_dir, ignore_errors=True)
        glass_dir.mkdir(parents=True, exist_ok=True)
        for item in src.iterdir():
            if item.name in {".git", "__pycache__"}:
                continue
            dest = glass_dir / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)
        os.chmod(glass_dir / "glass", 0o755)
        wrapper = bin_dir / "glass"
        if wrapper.exists() or wrapper.is_symlink():
            wrapper.unlink()
        wrapper.symlink_to(glass_dir / "glass")
        ui.success("glass CLI installed") if _has_glass() else ui.warn("glass installed but not on PATH (add ~/.local/bin).")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _post_json(url: str, payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)}


def pull(username: str | None) -> None:
    if not username:
        ui.error("Usage: silicon pull <silicon-username>")
        sys.exit(1)
    ensure_glass_cli()

    target = Path.cwd() / username
    if target.exists():
        ui.error(f"Target folder already exists: {target}")
        sys.exit(1)

    connector_code = ui.read_secret("Connector code")
    target.mkdir(parents=True)
    fingerprint = hashlib.sha256(f"{socket.gethostname()}::{target.resolve()}".encode()).hexdigest()

    code, claim = _post_json(f"{GLASS_SERVER_URL}/sync/api/pull/claim/", {
        "username": username, "connector_code": connector_code,
        "folder_label": username, "folder_fingerprint": fingerprint,
    })
    if not (200 <= code < 300):
        shutil.rmtree(target, ignore_errors=True)
        ui.error(claim.get("error", "Pull claim failed."))
        sys.exit(1)

    if claim.get("has_snapshot"):
        archive = tempfile.mktemp(suffix=".tar.gz", prefix="silicon-pull-")
        req = urllib.request.Request(
            f"{GLASS_SERVER_URL}/sync/api/silicons/{username}/latest.tar.gz",
            headers={"X-Source-Token": claim["source_token"]},
        )
        with urllib.request.urlopen(req) as resp, open(archive, "wb") as f:
            shutil.copyfileobj(resp, f)
        with tarfile.open(archive) as tf:
            tf.extractall(target)
        os.unlink(archive)

    (target / ".glass.json").write_text(json.dumps({
        "server_url": GLASS_SERVER_URL, "silicon_username": username,
        "source_token": claim["source_token"], "api_key": claim["api_key"],
        "folder_fingerprint": fingerprint, "last_tree_hash": claim.get("latest_tree_hash", ""),
    }, indent=2) + "\n")

    sj = target / "silicon.json"
    silicon = {}
    if sj.exists():
        try:
            silicon = json.loads(sj.read_text())
        except json.JSONDecodeError:
            silicon = {}
    silicon.setdefault("name", "Silicon")
    silicon.setdefault("run", "python main.py")
    silicon.setdefault("brain", "claude")
    silicon.setdefault("workers", {"browser": ["claude"], "terminal": ["claude"], "writer": ["claude"]})
    silicon.pop("version", None)
    silicon["address"] = username
    silicon["glass"] = {"server_url": GLASS_SERVER_URL, "silicon_username": username,
                        "api_key": claim["api_key"], "source_token": claim["source_token"]}
    sj.write_text(json.dumps(silicon, indent=4) + "\n")
    stemcell._env_upsert(target / "env.py", "GLASS_API_KEY", claim["api_key"])

    registry.register(username, str(target))
    ui.success(f"Pulled '{username}' into {target}")
    ui.info("Registered as a silicon instance.")

    # Empty-repo detection → offer to populate
    bare = {".glass.json", "silicon.json", "env.py"}
    real = [f for f in target.iterdir()
            if not (f.name.startswith(".") and f.name != ".glass.json")
            and f.name != "__pycache__" and f.name not in bare]
    if not real and ui.interactive():
        ui.warn("This looks like an empty repository (only silicon.json and env.py).")
        if ui.confirm("Do you want to populate it with Silicon?"):
            stemcell.hydrate(str(target))

    if ui.interactive() and ui.confirm("Do you want to enable backups for this silicon?"):
        ensure_glass_cli()
        ui.info("Running initial backup...")
        if subprocess.run(["glass", "push", "now"], cwd=str(target)).returncode == 0:
            ui.success("Backup complete.")
            _start_backup_loop(str(target), username)
        else:
            ui.warn(f"Initial backup failed. Retry with: silicon push {username} now")


def _start_backup_loop(path: str, name: str) -> None:
    ui.info("Starting hourly backup loop in background...")
    log = open(Path(path) / ".glass-push.log", "a")
    proc = subprocess.Popen(["glass", "push"], cwd=path, stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=True)
    (Path(path) / ".glass-push.pid").write_text(str(proc.pid))
    ui.success(f"Hourly backups running (PID {proc.pid}). Logs: {path}/.glass-push.log")
    ui.info(f"Use 'silicon push {name} now' for a manual backup anytime.")


def push(target: str | None, subcmd: str | None) -> None:
    inst = registry.resolve_one(target)
    if not (Path(inst.path) / ".glass.json").exists():
        ui.error(f"'{inst.name}' is not connected to Glass. No .glass.json found.")
        sys.exit(1)
    ensure_glass_cli()
    pid_file = Path(inst.path) / ".glass-push.pid"

    if subcmd == "now":
        ui.info(f"Pushing '{inst.name}' to Glass...")
        ok = subprocess.run(["glass", "push", "now"], cwd=inst.path).returncode == 0
        ui.success("Backup complete.") if ok else ui.error("Push failed.")
    elif subcmd == "stop":
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 15)
            pid_file.unlink(missing_ok=True)
            ui.success(f"Stopped backup loop for '{inst.name}'.")
            return
        except Exception:
            ui.warn(f"No backup loop running for '{inst.name}'.")
            pid_file.unlink(missing_ok=True)
    else:
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, 0)
                ui.warn(f"Backup loop already running for '{inst.name}' (PID {pid})")
                return
            except Exception:
                pass
        ui.info(f"Starting hourly backup loop for '{inst.name}'...")
        subprocess.run(["glass", "push", "now"], cwd=inst.path)
        _start_backup_loop(inst.path, inst.name)

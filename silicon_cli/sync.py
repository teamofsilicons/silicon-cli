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
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import registry, stemcell, ui
from .config import GLASS_CLI_REPO, GLASS_SERVER_URL

MANIFEST_NAME = ".backupsilicon"
BACKUP_UPLOAD_PATH = "/api/v1/silicon-backups/"
BACKUP_INTERVAL_SECS = 60 * 60


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


def _manifest_path(path: str) -> Path:
    return Path(path) / MANIFEST_NAME


def _read_manifest(path: str) -> list[str]:
    manifest = _manifest_path(path)
    if not manifest.exists():
        return []
    patterns = []
    for line in manifest.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            patterns.append(line)
    return patterns


def _resolve_manifest_path(root: Path, pattern: str) -> list[Path]:
    import glob

    raw = Path(os.path.expanduser(pattern))
    if not raw.is_absolute():
        raw = root / raw
    return [Path(p).resolve() for p in glob.glob(str(raw), recursive=True)]


def _build_manifest_archive(path: str) -> tuple[bytes, list[str]]:
    root = Path(path).resolve()
    resolved = []
    for pattern in _read_manifest(path):
        resolved.extend(_resolve_manifest_path(root, pattern))

    existing = sorted({p for p in resolved if p.exists()}, key=lambda p: str(p))
    dirs = [p for p in existing if p.is_dir()]
    top = [
        p
        for p in existing
        if not any(p != d and str(p).startswith(str(d) + os.sep) for d in dirs)
    ]

    archive = tempfile.NamedTemporaryFile(prefix="silicon-backup-", suffix=".tar.gz", delete=False)
    archive.close()
    included: list[str] = []
    try:
        with tarfile.open(archive.name, "w:gz") as tf:
            for item in top:
                arcname = item.relative_to(root).as_posix() if item.is_relative_to(root) else item.name
                try:
                    tf.add(item, arcname=arcname)
                    included.append(arcname)
                except Exception:
                    pass
        data = Path(archive.name).read_bytes()
    finally:
        Path(archive.name).unlink(missing_ok=True)
    return data, included


def _multipart(fields: dict[str, str], file_field: str, filename: str, content_type: str, data: bytes) -> tuple[bytes, str]:
    boundary = f"silicon-{uuid_hex()}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            str(value).encode(),
            b"\r\n",
        ])
    chunks.extend([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode(),
        f"Content-Type: {content_type}\r\n\r\n".encode(),
        data,
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    return b"".join(chunks), boundary


def uuid_hex() -> str:
    import uuid

    return uuid.uuid4().hex


def _glass_config(path: str) -> dict:
    cfg = Path(path) / ".glass.json"
    if not cfg.exists():
        raise FileNotFoundError("No .glass.json found.")
    return json.loads(cfg.read_text())


def _manifest_backup_now(path: str, note: str = "manual") -> bool:
    patterns = _read_manifest(path)
    if not patterns:
        ui.warn("No .backupsilicon manifest found. Nothing to back up.")
        return False

    data, included = _build_manifest_archive(path)
    if not included:
        ui.warn(".backupsilicon matched no files.")
        return False

    cfg = _glass_config(path)
    api_key = cfg.get("api_key") or cfg.get("silicon_api_key") or ""
    if not api_key:
        ui.error(".glass.json does not contain api_key.")
        return False

    server = (cfg.get("server_url") or GLASS_SERVER_URL).rstrip("/")
    body, boundary = _multipart(
        {"manifest": json.dumps(included), "note": note},
        "file",
        "backup.tar.gz",
        "application/gzip",
        data,
    )
    req = urllib.request.Request(
        server + BACKUP_UPLOAD_PATH,
        data=body,
        headers={
            "X-Silicon-Key": api_key,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            payload = json.loads(resp.read().decode() or "{}")
            ui.success(f"Backup uploaded v{payload.get('seq', '?')} ({len(included)} paths).")
            return True
    except urllib.error.HTTPError as e:
        msg = e.read().decode(errors="replace")[:200]
        ui.error(f"Backup upload failed HTTP {e.code}: {msg}")
    except Exception as e:
        ui.error(f"Backup upload failed: {e}")
    return False


def backup_loop(path: str, name: str | None = None) -> None:
    label = name or Path(path).name
    while True:
        ui.info(f"Running scheduled backup for '{label}'...")
        _manifest_backup_now(path, note="scheduled")
        time.sleep(BACKUP_INTERVAL_SECS)


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
    if _manifest_path(path).exists():
        cmd = [sys.executable, "-m", "silicon_cli.cli", "_backup_loop", path, name]
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    else:
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
        if _manifest_path(inst.path).exists():
            ok = _manifest_backup_now(inst.path, note="manual")
        else:
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

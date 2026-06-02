"""Create / hydrate a silicon from the silicon-stemcell base.

`silicon new <dir>` downloads the stemcell, copies in any files the target is
missing (never clobbering env.py / silicon.json / .glass.json), seeds config +
env keys, prompts for tokens + brain/worker providers, installs requirements,
and registers the instance — same flow as the original bash CLI.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import registry, ui
from .config import STEMCELL_GIT_URL, STEMCELL_ZIP_URL, python_run_cmd

SKIP_NAMES = {".git", "__pycache__", ".DS_Store"}
PRESERVE_ROOT = {"env.py", "silicon.json", ".glass.json"}
ALLOWED_PROVIDERS = {"claude", "codex", "chatgpt"}


def download_stemcell(target: str) -> None:
    shutil.rmtree(target, ignore_errors=True)
    os.makedirs(target, exist_ok=True)
    if shutil.which("git"):
        subprocess.run(["git", "clone", "--depth", "1", STEMCELL_GIT_URL, target],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        shutil.rmtree(Path(target) / ".git", ignore_errors=True)
        return
    # Fallback: download the zip
    dl = shutil.which("curl") or shutil.which("wget")
    if not dl:
        ui.error("Need git, curl, or wget to download Silicon.")
        sys.exit(1)
    tmp_zip = tempfile.mktemp(suffix=".zip", prefix="silicon-")
    if "curl" in dl:
        subprocess.run([dl, "-fsSL", STEMCELL_ZIP_URL, "-o", tmp_zip], check=True)
    else:
        subprocess.run([dl, "-q", STEMCELL_ZIP_URL, "-O", tmp_zip], check=True)
    subprocess.run(["unzip", "-q", tmp_zip, "-d", target], check=True)
    os.unlink(tmp_zip)
    extracted = [p for p in Path(target).iterdir() if p.is_dir() and p.name.startswith("silicon-")]
    if extracted:
        inner = extracted[0]
        for item in inner.iterdir():
            shutil.move(str(item), str(Path(target) / item.name))
        shutil.rmtree(inner, ignore_errors=True)


def _env_value(env_path: Path, key: str) -> str:
    if not env_path.exists():
        return ""
    m = re.search(rf'^{key}\s*=\s*["\'](.*)["\']\s*$', env_path.read_text(), re.M)
    return m.group(1) if m else ""


def _env_upsert(env_path: Path, key: str, value: str) -> None:
    text = env_path.read_text() if env_path.exists() else ""
    pattern = rf'^{key}\s*=\s*["\'].*["\']\s*$'
    replacement = f'{key} = "{value}"'
    if re.search(pattern, text, re.M):
        text = re.sub(pattern, replacement, text, flags=re.M)
    else:
        text = (text.rstrip() + "\n" if text.strip() else "") + replacement + "\n"
    env_path.write_text(text.rstrip() + "\n")


def _provider_list(value, default):
    if not isinstance(value, list):
        return default
    out = []
    for item in value:
        if isinstance(item, str) and item in ALLOWED_PROVIDERS:
            v = "codex" if item == "chatgpt" else item
            if v not in out:
                out.append(v)
    return out or default


def _choose_provider_order(worker_type: str, default_choice: str = "claude") -> list[str]:
    choice = ui.ask(f"Which provider should {worker_type} workers use – claude or codex?", default_choice)
    if choice == "codex":
        return ["codex", "claude"] if ui.confirm(f"Keep claude as fallback for {worker_type} workers?") else ["codex"]
    return ["claude", "codex"] if ui.confirm(f"Keep codex as fallback for {worker_type} workers?") else ["claude"]


def hydrate(target: str) -> None:
    abs_target = str(Path(target).resolve())
    os.makedirs(abs_target, exist_ok=True)
    dst = Path(abs_target)

    tmp_src = tempfile.mkdtemp(prefix="silicon-src-")
    try:
        ui.info("Downloading Silicon stemcell...")
        download_stemcell(tmp_src)
        src = Path(tmp_src)

        # Instance name: silicon.json address/name, else folder name
        name = ""
        sj = dst / "silicon.json"
        if sj.exists():
            try:
                data = json.loads(sj.read_text())
                name = (data.get("address") or data.get("name") or "").strip()
            except Exception:
                pass
        if not name:
            name = dst.name

        ui.info(f"Hydrating {abs_target}...")
        for path in src.rglob("*"):
            rel = path.relative_to(src)
            if any(part in SKIP_NAMES for part in rel.parts):
                continue
            tgt = dst / rel
            if path.is_dir():
                tgt.mkdir(parents=True, exist_ok=True)
                continue
            if len(rel.parts) == 1 and rel.parts[0] in PRESERVE_ROOT and tgt.exists():
                continue
            if tgt.exists():
                continue
            tgt.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, tgt)

        # Seed silicon.json
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
        if not silicon.get("address"):  # the stemcell ships an empty address — fill it
            silicon["address"] = name
        silicon.pop("version", None)
        sj.write_text(json.dumps(silicon, indent=4) + "\n")

        # Seed env.py required keys
        env_path = dst / "env.py"
        for key, default in {"TELEGRAM_BOT_TOKEN": "", "OPENAI_API_KEY": "", "GEMINI_API_KEY": "", "BROWSER_PROFILE": name}.items():
            if not _env_value(env_path, key):
                _env_upsert(env_path, key, default)

        # Run the stemcell's snapshot hook (for safe future updates), best-effort
        updater = src / "scripts" / "silicon_update.py"
        if updater.exists():
            subprocess.run([python_run_cmd(), str(updater), "snapshot", "--source", str(src), "--target", abs_target],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Interactive setup
        if ui.interactive():
            _interactive_setup(dst, env_path, sj, name)

        # Install dependencies
        req = dst / "requirements.txt"
        if req.exists():
            ui.info("Installing Python dependencies...")
            r = subprocess.run([python_run_cmd(), "-m", "pip", "install", "-r", str(req), "--quiet"])
            if r.returncode != 0:
                subprocess.run([python_run_cmd(), "-m", "pip", "install", "-r", str(req), "--quiet", "--user"])

        registry.register(name, abs_target)
        ui.success(f"Hydrated '{name}' at {abs_target}")
        ui.info(f"Run 'silicon start {name}' when you're ready.")
    finally:
        shutil.rmtree(tmp_src, ignore_errors=True)


def _interactive_setup(dst: Path, env_path: Path, sj: Path, name: str) -> None:
    if not _env_value(env_path, "TELEGRAM_BOT_TOKEN"):
        ui.info("You need a Telegram bot token to use Silicon.")
        sys.stderr.write(f"{ui.DIM}  1. Open Telegram and search for @BotFather{ui.RESET}\n")
        sys.stderr.write(f"{ui.DIM}  2. Send /newbot and follow the prompts{ui.RESET}\n")
        sys.stderr.write(f"{ui.DIM}  3. Copy the token BotFather gives you{ui.RESET}\n")
        token = ui.read_secret("Telegram bot token")
        if not token:
            ui.error("Telegram bot token is required.")
            sys.exit(1)
        _env_upsert(env_path, "TELEGRAM_BOT_TOKEN", token)

    if not _env_value(env_path, "OPENAI_API_KEY"):
        ui.info("OpenAI API key (for incoming voice transcription via Whisper). Enter to skip.")
        v = ui.read_secret("OpenAI API key (optional)")
        if v:
            _env_upsert(env_path, "OPENAI_API_KEY", v)

    if not _env_value(env_path, "GEMINI_API_KEY"):
        ui.info("Gemini API key (for outgoing text-to-speech). Enter to skip.")
        v = ui.read_secret("Gemini API key (optional)")
        if v:
            _env_upsert(env_path, "GEMINI_API_KEY", v)

    # Brain / worker providers — only ask when both claude + codex are present
    brain = "claude"
    workers = {"browser": ["claude"], "terminal": ["claude"], "writer": ["claude"]}
    have_claude = bool(shutil.which("claude"))
    have_codex = bool(shutil.which("codex"))
    if have_claude and have_codex:
        ui.info("Detected both claude and codex.")
        brain = "codex" if ui.ask("Which brain should Silicon use – claude or codex?", "claude") == "codex" else "claude"
        # Each worker defaults to the chosen brain (matches the current stemcell CLI).
        workers = {
            "browser": _choose_provider_order("browser", brain),
            "terminal": _choose_provider_order("terminal", brain),
            "writer": _choose_provider_order("writer", brain),
        }
    elif have_codex:
        brain = "codex"
        workers = {"browser": ["codex"], "terminal": ["codex"], "writer": ["codex"]}

    try:
        silicon = json.loads(sj.read_text())
    except Exception:
        silicon = {}
    silicon["brain"] = brain
    silicon["workers"] = {k: _provider_list(v, ["claude"]) for k, v in workers.items()}
    sj.write_text(json.dumps(silicon, indent=4) + "\n")

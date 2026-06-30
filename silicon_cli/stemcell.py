"""Create / hydrate a silicon from the silicon-stemcell base.

`silicon new <dir>` downloads the stemcell, copies in any files the target is
missing (never clobbering env.py / silicon.json / .glass.json), seeds config +
env keys, prompts for the one brain provider order, installs requirements, and
registers the instance.
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

from . import interface_cli, registry, ui
from .config import STEMCELL_GIT_URL, STEMCELL_ZIP_URL, base_python_cmd, python_run_cmd, venv_python

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


def _choose_brain_order(primary: str) -> list[str]:
    primary = "codex" if primary == "codex" else "claude"
    fallback = "claude" if primary == "codex" else "codex"
    if ui.confirm(f"Use {fallback} as fallback brain for all workers?", default_yes=True):
        return [primary, fallback]
    return [primary]


def _ensure_venv(dst: Path) -> str | None:
    """Create <dst>/.venv if missing. Returns its interpreter, or None.

    Returns None when the venv can't be created — e.g. Debian/Ubuntu without
    python3-venv, where ensurepip is stripped from the system interpreter.
    """
    existing = venv_python(dst)
    if existing:
        return existing
    r = subprocess.run([base_python_cmd(), "-m", "venv", str(dst / ".venv")],
                       capture_output=True, text=True)
    if r.returncode != 0:
        shutil.rmtree(dst / ".venv", ignore_errors=True)
        return None
    return venv_python(dst)


def install_requirements(dst: Path, req: Path) -> None:
    """Install the silicon's requirements, preferring an instance-local venv.

    System interpreters are commonly externally managed (PEP 668), so a plain
    `pip install` is refused. Try, in order: the silicon's own .venv, plain
    pip, `--user`, `--break-system-packages`. A failure here must abort the
    hydration — a silicon without its dependencies starts but never completes
    its Glass handshake.
    """
    ui.info("Installing Python dependencies...")
    vpy = _ensure_venv(dst)
    if vpy:
        r = subprocess.run([vpy, "-m", "pip", "install", "-r", str(req), "--quiet"])
        if r.returncode == 0:
            return
        ui.warn("Install into the silicon's venv failed; falling back to the system interpreter.")
        # A half-provisioned venv must not survive: python_run_cmd() would
        # prefer it over the system interpreter that gets the deps below.
        shutil.rmtree(dst / ".venv", ignore_errors=True)
    py = base_python_cmd()
    last = None
    for extra in ([], ["--user"], ["--break-system-packages"]):
        last = subprocess.run([py, "-m", "pip", "install", "-r", str(req), "--quiet", *extra],
                              capture_output=True, text=True)
        if last.returncode == 0:
            return
    if last is not None and last.stderr:
        sys.stderr.write(last.stderr)
    ui.error("Could not install Python dependencies — this silicon would start but never "
             "complete its Glass handshake, so hydration was aborted.")
    ui.info("On Debian/Ubuntu, install venv support and retry:  sudo apt install python3-venv")
    ui.info("Or point SILICON_PYTHON at an interpreter that allows pip installs.")
    sys.exit(1)


def hydrate(
    target: str,
    setup_config=None,
    *,
    install_deps: bool = True,
    setup_interface: bool = True,
    register_install: bool = True,
) -> None:
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

        # Seed env.py required keys used by the current stemcell.
        env_path = dst / "env.py"
        for key, default in {"GLASS_API_KEY": "", "BROWSER_PROFILE": name}.items():
            if not _env_value(env_path, key):
                _env_upsert(env_path, key, default)

        # Run the stemcell's snapshot hook (for safe future updates), best-effort
        updater = src / "scripts" / "silicon_update.py"
        if updater.exists():
            subprocess.run([python_run_cmd(), str(updater), "snapshot", "--source", str(src), "--target", abs_target],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Interactive setup
        if setup_config is not None:
            _apply_setup(sj, setup_config)
        elif ui.interactive():
            _interactive_setup(sj)

        # Install dependencies
        req = dst / "requirements.txt"
        if install_deps and req.exists():
            install_requirements(dst, req)

        if register_install:
            registry.register(name, abs_target)
        if setup_interface:
            interface_cli.setup(abs_target)
        ui.success(f"Hydrated '{name}' at {abs_target}")
        ui.info(f"Run 'silicon start {name}' when you're ready.")
    finally:
        shutil.rmtree(tmp_src, ignore_errors=True)


def choose_setup_config(
    label: str = "Default silicon settings",
    *,
    brain: str | None = None,
    brain_order: list[str] | None = None,
) -> dict:
    # Brain provider order — one choice drives manager + every worker type.
    if label:
        ui.info(label)
    # A caller-supplied brain (e.g. Glass's non-interactive setup agent) skips the
    # prompt entirely and is honored even if that tool isn't detected on PATH yet
    # (it may still be installing during provisioning).
    if brain in ("claude", "codex"):
        order = [b for b in (brain_order or [brain]) if b in ("claude", "codex")] or [brain]
        return {
            "brain": brain,
            "brain_order": order,
            "workers": {k: _provider_list(order, [brain]) for k in ("browser", "terminal", "writer")},
        }
    brain = "claude"
    order = ["claude"]
    workers = {"browser": ["claude"], "terminal": ["claude"], "writer": ["claude"]}
    have_claude = bool(shutil.which("claude"))
    have_codex = bool(shutil.which("codex"))
    if have_claude and have_codex:
        ui.info("Detected both claude and codex.")
        brain = "codex" if ui.ask("Who do you want the brain to be – claude or codex?", "claude") == "codex" else "claude"
        order = _choose_brain_order(brain)
        workers = {"browser": order, "terminal": order, "writer": order}
    elif have_codex:
        brain = "codex"
        order = ["codex"]
        workers = {"browser": ["codex"], "terminal": ["codex"], "writer": ["codex"]}
    return {
        "brain": brain,
        "brain_order": order,
        "workers": {k: _provider_list(v, ["claude"]) for k, v in workers.items()},
    }


def _apply_setup(sj: Path, setup_config: dict) -> None:
    try:
        silicon = json.loads(sj.read_text())
    except Exception:
        silicon = {}
    brain = setup_config.get("brain") if isinstance(setup_config, dict) else ""
    order = setup_config.get("brain_order") if isinstance(setup_config, dict) else None
    workers = setup_config.get("workers") if isinstance(setup_config, dict) else None
    silicon["brain"] = "codex" if brain == "codex" else "claude"
    silicon["brain_order"] = _provider_list(order, [silicon["brain"]])
    silicon["workers"] = {
        "browser": _provider_list((workers or {}).get("browser"), silicon["brain_order"]),
        "terminal": _provider_list((workers or {}).get("terminal"), silicon["brain_order"]),
        "writer": _provider_list((workers or {}).get("writer"), silicon["brain_order"]),
    }
    sj.write_text(json.dumps(silicon, indent=4) + "\n")


def _interactive_setup(sj: Path) -> None:
    _apply_setup(sj, choose_setup_config(""))

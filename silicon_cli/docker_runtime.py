"""Docker runtime backend for one-container-per-Silicon installs."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import tempfile
import urllib.request
from pathlib import Path
from typing import Iterable

from . import registry, ui
from .config import REGISTRY_DIR

CONFIG_FILE = REGISTRY_DIR / "docker.json"
DEFAULT_ROOT = Path.home() / "silicons"
DEFAULT_IMAGE = "ghcr.io/teamofsilicons/silicon-runtime:latest"
CONTAINER_PATH = "/silicon"
CONTAINER_SHARED_HOME = "/silicon-shared-home"
DOCKER_INSTALL_URL = "https://get.docker.com"
AUTH_FILE = ".silicon-auth.json"
AUTH_PROVIDERS = {"claude", "codex"}


def _truthy(value: str | None) -> bool:
    return str(value or "").lower() in {"1", "true", "yes", "on"}


def _falsey(value: str | None) -> bool:
    return str(value or "").lower() in {"0", "false", "no", "off"}


def _safe_name(value: str) -> str:
    raw = (value or "silicon").strip().lower()
    safe = re.sub(r"[^a-z0-9_.-]+", "-", raw)
    safe = re.sub(r"-+", "-", safe).strip("._-")
    return safe or "silicon"


def service_name(name: str) -> str:
    return f"silicon-{_safe_name(name)}"


def container_name(name: str) -> str:
    return f"silicon-{_safe_name(name)}"


def _json(value: str) -> str:
    return json.dumps(str(value))


def host_user() -> str:
    if hasattr(os, "getuid") and hasattr(os, "getgid"):
        return f"{os.getuid()}:{os.getgid()}"
    return ""


def runtime_opted_out() -> bool:
    runtime = os.environ.get("SILICON_RUNTIME", "").strip().lower()
    return runtime in {"local", "host", "native", "none", "off"} or _falsey(os.environ.get("SILICON_RUNTIME_DOCKER"))


def _save_config(config: dict) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n")


def load_config(required: bool = False) -> dict:
    env_enabled = _truthy(os.environ.get("SILICON_RUNTIME_DOCKER")) or os.environ.get("SILICON_RUNTIME") == "docker"
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text())
        except Exception:
            data = {}
    elif required and not env_enabled:
        ui.error("Docker runtime is not initialized. Run: silicon docker init")
        sys.exit(1)

    root = Path(os.environ.get("SILICON_DOCKER_ROOT") or data.get("root") or DEFAULT_ROOT).expanduser()
    compose_file = Path(
        os.environ.get("SILICON_DOCKER_COMPOSE")
        or data.get("compose_file")
        or root / "compose.yml"
    ).expanduser()
    shared_home = Path(
        os.environ.get("SILICON_DOCKER_SHARED_HOME")
        or data.get("shared_home")
        or root / ".shared-home"
    ).expanduser()
    image = os.environ.get("SILICON_RUNTIME_IMAGE") or data.get("image") or DEFAULT_IMAGE
    env_sudo = os.environ.get("SILICON_DOCKER_SUDO")
    docker_sudo = _truthy(env_sudo) if env_sudo is not None else bool(data.get("docker_sudo", False))
    return {
        "enabled": bool(data.get("enabled", False) or env_enabled),
        "root": str(root),
        "compose_file": str(compose_file),
        "shared_home": str(shared_home),
        "image": image,
        "docker_sudo": docker_sudo,
    }


def config_for_install(inst: registry.Install) -> dict:
    cfg = load_config()
    if inst.path:
        cfg["root"] = str(Path(inst.path).expanduser().resolve().parent)
    if inst.compose_file:
        cfg["compose_file"] = inst.compose_file
    if inst.image:
        cfg["image"] = inst.image
    return cfg


def enabled() -> bool:
    if _truthy(os.environ.get("SILICON_CONTAINER_MODE")):
        return False
    return bool(load_config().get("enabled"))


def init(
    root: str | None = None,
    image: str | None = None,
    *,
    shared_home: str | None = None,
    docker_sudo: bool | None = None,
    quiet: bool = False,
) -> None:
    chosen_root = Path(root).expanduser() if root else DEFAULT_ROOT
    chosen_root = chosen_root.resolve()
    chosen_image = image or DEFAULT_IMAGE
    current = load_config()
    chosen_shared_home = (
        Path(shared_home).expanduser().resolve()
        if shared_home
        else Path(current.get("shared_home") if not root else chosen_root / ".shared-home").expanduser().resolve()
    )
    if docker_sudo is None:
        docker_sudo = bool(current.get("docker_sudo", False))
    chosen_root.mkdir(parents=True, exist_ok=True)
    chosen_shared_home.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    config = {
        "enabled": True,
        "root": str(chosen_root),
        "compose_file": str(chosen_root / "compose.yml"),
        "shared_home": str(chosen_shared_home),
        "image": chosen_image,
        "docker_sudo": docker_sudo,
    }
    _save_config(config)
    render_compose(config)
    if not quiet:
        ui.success(f"Docker runtime enabled. Instances root: {chosen_root}")
        ui.info(f"Compose file: {config['compose_file']}")
        ui.info(f"Shared auth home: {chosen_shared_home}")
        ui.info(f"Runtime image: {chosen_image}")
        if docker_sudo:
            ui.info("Docker commands will run through sudo for this runtime.")


def target_path(name_or_path: str | None) -> Path:
    cfg = load_config(required=True)
    root = Path(cfg["root"]).expanduser()
    if name_or_path:
        p = Path(name_or_path).expanduser()
        return p.resolve() if p.is_absolute() or len(p.parts) > 1 or name_or_path in {".", ".."} else (root / _safe_name(name_or_path)).resolve()
    name = ui.ask("New silicon folder name", "silicon")
    if not name:
        ui.error("A folder name is required.")
        sys.exit(1)
    return (root / _safe_name(name)).resolve()


def register_instance(name: str, path: str | Path, *, image: str | None = None) -> registry.Install:
    cfg = load_config(required=True)
    root = Path(cfg["root"])
    root.mkdir(parents=True, exist_ok=True)
    abs_path = Path(path).expanduser().resolve()
    svc = service_name(name)
    cname = container_name(name)
    img = image or cfg["image"]
    registry.register(
        name,
        str(abs_path),
        str(abs_path / ".silicon.pid"),
        runtime="docker",
        service=svc,
        compose_file=cfg["compose_file"],
        image=img,
        container_name=cname,
        update_existing=True,
    )
    render_compose(cfg)
    inst = registry.find(name)
    if inst is None:
        ui.error(f"Could not register Docker silicon '{name}'.")
        sys.exit(1)
    return inst


def _docker_installs(compose_file: str | None = None) -> list[registry.Install]:
    rows = [i for i in registry.installs() if i.is_docker]
    if compose_file:
        rows = [i for i in rows if not i.compose_file or Path(i.compose_file) == Path(compose_file)]
    return rows


def render_compose(config: dict | None = None) -> Path:
    cfg = config or load_config(required=True)
    compose = Path(cfg["compose_file"]).expanduser()
    compose.parent.mkdir(parents=True, exist_ok=True)

    rows = _docker_installs(str(compose))
    lines = ["name: silicon-runtime", "", "services:"]
    shared_home = str(Path(cfg["shared_home"]).expanduser().resolve())
    Path(shared_home).mkdir(parents=True, exist_ok=True)
    if not rows:
        lines.append("  # Services are added by `silicon new` or `silicon pull`.")
    for inst in rows:
        svc = inst.service or service_name(inst.name)
        cname = inst.container_name or container_name(inst.name)
        image = inst.image or cfg["image"]
        user = host_user()
        path = str(Path(inst.path).expanduser().resolve())
        lines.extend([
            f"  {svc}:",
            f"    image: {_json(image)}",
            f"    container_name: {_json(cname)}",
            "    restart: unless-stopped",
            *([f"    user: {_json(user)}"] if user else []),
            "    environment:",
            f"      SILICON_INSTANCE_NAME: {_json(inst.name)}",
            f"      SILICON_SHARED_HOME: {_json(CONTAINER_SHARED_HOME)}",
            '      SILICON_CONTAINER_MODE: "1"',
            "    volumes:",
            f"      - {_json(path + ':' + CONTAINER_PATH)}",
            f"      - {_json(shared_home + ':' + CONTAINER_SHARED_HOME)}",
            "",
        ])
    compose.write_text("\n".join(lines).rstrip() + "\n")
    return compose


def _cmd(cmd: list[str], *, capture: bool = True) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, text=True, capture_output=capture)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]} not found")


def _is_linux() -> bool:
    return sys.platform.startswith("linux")


def _sudo_prefix() -> list[str] | None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return []
    if not shutil.which("sudo"):
        return None
    return ["sudo"]


def _docker_cmd(config: dict | None = None) -> list[str]:
    cfg = config if config is not None else load_config()
    return [*(["sudo"] if cfg.get("docker_sudo") else []), "docker"]


def _manual_docker_steps() -> None:
    ui.info("Install Docker manually, then rerun the same silicon command:")
    ui.info("  curl -fsSL https://get.docker.com -o get-docker.sh")
    ui.info("  sudo sh get-docker.sh")
    ui.info("  sudo systemctl enable --now docker")
    ui.info("  sudo usermod -aG docker $USER")
    ui.info("Then log out/in, or run: newgrp docker")


def _download_docker_installer() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="silicon-docker-install-"))
    script = tmp / "get-docker.sh"
    with urllib.request.urlopen(DOCKER_INSTALL_URL, timeout=60) as resp:
        script.write_bytes(resp.read())
    script.chmod(0o700)
    return script


def _install_docker_engine() -> bool:
    if not _is_linux():
        ui.error("Automatic Docker Engine install is only supported on Linux servers.")
        ui.info("Install Docker Desktop or Docker Engine for this OS, then rerun the same silicon command.")
        return False

    auto_install = _truthy(os.environ.get("SILICON_DOCKER_AUTO_INSTALL"))
    if not auto_install and ui.interactive():
        auto_install = ui.confirm(
            "Docker is required for Silicon runtime. Install Docker Engine now using Docker's official installer?",
            default_yes=True,
        )
    if not auto_install:
        _manual_docker_steps()
        return False

    sudo = _sudo_prefix()
    if sudo is None:
        ui.error("sudo was not found, so the CLI cannot install Docker automatically.")
        _manual_docker_steps()
        return False

    try:
        script = _download_docker_installer()
    except Exception as e:
        ui.error(f"Could not download Docker installer: {e}")
        _manual_docker_steps()
        return False

    ui.info("Installing Docker Engine...")
    result = _run([*sudo, "sh", str(script)])
    if result.returncode != 0:
        ui.error("Docker installer failed.")
        _manual_docker_steps()
        return False

    if shutil.which("systemctl"):
        _run([*sudo, "systemctl", "enable", "--now", "docker"])

    user = os.environ.get("SUDO_USER") or os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    if user and user != "root":
        _run([*sudo, "usermod", "-aG", "docker", user])
        ui.warn("Added this user to the docker group. A new login shell may be needed for non-sudo Docker access.")
    return True


def _ensure_docker_binary(install: bool) -> None:
    if shutil.which("docker"):
        return
    if install and _install_docker_engine() and shutil.which("docker"):
        return
    ui.error("Docker was not found on PATH.")
    _manual_docker_steps()
    sys.exit(127)


def _ensure_compose(config: dict) -> None:
    result = _cmd([*_docker_cmd(config), "compose", "version"])
    if result.returncode == 0:
        return
    ui.error("Docker Compose v2 plugin is not available.")
    ui.info("Install the Docker Compose plugin, then rerun the same silicon command.")
    ui.info("Ubuntu/Debian package: sudo apt-get install docker-compose-plugin")
    sys.exit(1)


def _ensure_daemon(config: dict) -> dict:
    result = _cmd([*_docker_cmd(config), "info"])
    if result.returncode == 0:
        return config

    if _is_linux() and shutil.which("systemctl"):
        sudo = _sudo_prefix()
        if sudo is not None:
            ui.info("Starting Docker daemon...")
            _run([*sudo, "systemctl", "enable", "--now", "docker"])
            result = _cmd([*_docker_cmd(config), "info"])
            if result.returncode == 0:
                return config

    sudo = _sudo_prefix()
    if sudo is not None and not config.get("docker_sudo"):
        result = _cmd(["sudo", "docker", "info"])
        if result.returncode == 0:
            config = {**config, "docker_sudo": True}
            ui.warn("Current shell cannot access Docker directly; using sudo docker for Silicon commands.")
            ui.info("For non-sudo Docker access later: sudo usermod -aG docker $USER && newgrp docker")
            return config

    stderr = (result.stderr or result.stdout or "").strip()
    ui.error("Docker daemon is not reachable." + (f" {stderr}" if stderr else ""))
    ui.info("Start Docker, then rerun the same silicon command:")
    ui.info("  sudo systemctl enable --now docker")
    sys.exit(1)


def _ensure_image(config: dict) -> None:
    image = config.get("image") or DEFAULT_IMAGE
    inspect = _cmd([*_docker_cmd(config), "image", "inspect", image])
    if inspect.returncode == 0:
        return
    ui.info(f"Pulling Silicon runtime image: {image}")
    pulled = _run([*_docker_cmd(config), "pull", image])
    if pulled.returncode == 0:
        return
    ui.error(f"Could not pull Docker image: {image}")
    ui.info("If this is a private or not-yet-published image, build or login first, then rerun the same command.")
    ui.info("  docker login ghcr.io")
    ui.info(f"  docker build -f docker/runtime/Dockerfile -t {DEFAULT_IMAGE} .")
    sys.exit(pulled.returncode or 1)


def ensure_ready(
    *,
    auto_init: bool = False,
    install: bool = True,
    pull_image: bool = True,
    root: str | None = None,
    image: str | None = None,
    quiet: bool = False,
) -> dict:
    """Make Docker usable for Silicon commands, installing/initializing when allowed."""
    if _truthy(os.environ.get("SILICON_CONTAINER_MODE")):
        return load_config()
    if runtime_opted_out():
        return load_config()

    cfg = load_config()
    if root:
        cfg["root"] = str(Path(root).expanduser().resolve())
        cfg["compose_file"] = str(Path(cfg["root"]) / "compose.yml")
        cfg["shared_home"] = str(Path(cfg["root"]) / ".shared-home")
    if image:
        cfg["image"] = image

    if not cfg.get("enabled") and not auto_init:
        load_config(required=True)

    _ensure_docker_binary(install)
    cfg = _ensure_daemon(cfg)
    _ensure_compose(cfg)

    if auto_init or not cfg.get("enabled") or not CONFIG_FILE.exists():
        init(
            cfg["root"],
            cfg["image"],
            shared_home=cfg.get("shared_home"),
            docker_sudo=bool(cfg.get("docker_sudo")),
            quiet=quiet,
        )
        cfg = load_config(required=True)
    else:
        _save_config({**cfg, "enabled": True})
        render_compose(cfg)

    if pull_image:
        _ensure_image(cfg)
    return cfg


def _auth_path(config: dict) -> Path:
    return Path(config["shared_home"]).expanduser().resolve() / AUTH_FILE


def _read_auth(config: dict) -> dict:
    path = _auth_path(config)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_auth(config: dict, updates: dict) -> None:
    path = _auth_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _read_auth(config)
    data.update(updates)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _select_auth_providers(args: list[str], *, prompt: bool) -> list[str]:
    selected: list[str] = []
    for arg in args:
        value = arg.strip().lower()
        if value in {"--all", "all"}:
            selected = ["claude", "codex"]
        elif value in {"--claude", "claude"}:
            selected.append("claude")
        elif value in {"--codex", "codex"}:
            selected.append("codex")
        elif value:
            ui.error(f"Unknown docker login option: {arg}")
            sys.exit(1)
    if selected:
        out = []
        for provider in selected:
            if provider not in out:
                out.append(provider)
        return out
    if not prompt or not ui.interactive():
        return []
    out = []
    if ui.confirm("Set up shared Claude Code account for this VM?", default_yes=True):
        out.append("claude")
    if ui.confirm("Set up shared Codex account for this VM?", default_yes=True):
        out.append("codex")
    return out


def _auth_container(config: dict, provider: str) -> int:
    shared_home = Path(config["shared_home"]).expanduser().resolve()
    shared_home.mkdir(parents=True, exist_ok=True)
    user = host_user()
    cmd = [
        *_docker_cmd(config),
        "run",
        "--rm",
        "-it",
        *(["--user", user] if user else []),
        "-e",
        f"SILICON_SHARED_HOME={CONTAINER_SHARED_HOME}",
        "-v",
        f"{shared_home}:{CONTAINER_SHARED_HOME}",
        "--entrypoint",
        "/usr/local/bin/silicon-runtime-entrypoint",
        config.get("image") or DEFAULT_IMAGE,
        "auth",
        provider,
    ]
    return _run(cmd).returncode


def _shared_tool_container(config: dict, tool: str, args: list[str]) -> int:
    if tool not in AUTH_PROVIDERS:
        ui.error(f"Unknown shared runtime tool: {tool}")
        return 1
    shared_home = Path(config["shared_home"]).expanduser().resolve()
    shared_home.mkdir(parents=True, exist_ok=True)
    user = host_user()
    command = [tool, *args] if args else [tool]
    cmd = [
        *_docker_cmd(config),
        "run",
        "--rm",
        *(["-it"] if ui.interactive() else []),
        *(["--user", user] if user else []),
        "-e",
        f"SILICON_SHARED_HOME={CONTAINER_SHARED_HOME}",
        "-v",
        f"{shared_home}:{CONTAINER_SHARED_HOME}",
        "--entrypoint",
        "/usr/local/bin/silicon-runtime-entrypoint",
        config.get("image") or DEFAULT_IMAGE,
        "shared",
        *command,
    ]
    return _run(cmd).returncode


def run_shared_tool(tool: str, args: list[str] | None = None) -> None:
    actual_args = args or []
    if not ui.interactive() and not actual_args:
        ui.error(f"silicon {tool} must be run from an interactive terminal.")
        sys.exit(1)
    cfg = ensure_ready(auto_init=True, install=True, pull_image=True)
    code = _shared_tool_container(cfg, tool, actual_args)
    if code:
        sys.exit(code)


def login(args: list[str] | None = None, *, config: dict | None = None, prompt: bool = True) -> None:
    if not ui.interactive():
        ui.error("Shared Claude/Codex login must be run from an interactive terminal.")
        ui.info("Run: silicon docker login")
        sys.exit(1)
    cfg = config or ensure_ready(auto_init=True, install=True, pull_image=True)
    providers = _select_auth_providers(args or [], prompt=prompt)
    if not providers:
        ui.warn("No Claude/Codex account setup selected.")
        _write_auth(cfg, {"skipped": True})
        return

    for provider in providers:
        label = "Claude Code" if provider == "claude" else "Codex"
        ui.info(f"Opening shared {label} setup shell.")
        ui.info("Complete the sign-in flow in the container. When finished, exit the shell.")
        code = _auth_container(cfg, provider)
        if code != 0:
            ui.warn(f"{label} setup shell exited with code {code}.")
        if ui.confirm(f"Did you finish signing in to {label}? Type y if signed in.", default_yes=False):
            _write_auth(cfg, {provider: True, "skipped": False})
            ui.success(f"{label} marked as signed in for this VM.")
        else:
            ui.warn(f"{label} was not marked as signed in. You can retry with: silicon docker login {provider}")


def maybe_prompt_login(config: dict) -> None:
    if not ui.interactive():
        ui.info("To set up shared Claude/Codex accounts later, run: silicon docker login")
        return
    status = _read_auth(config)
    if status.get("claude") or status.get("codex") or status.get("skipped"):
        return
    if ui.confirm("Set up shared Claude/Codex accounts before installing Silicons?", default_yes=True):
        login([], config=config, prompt=True)
    else:
        _write_auth(config, {"skipped": True})
        ui.info("Skipping shared Claude/Codex login. You can run later: silicon docker login")


def ensure_pull_runtime() -> bool:
    if runtime_opted_out():
        ui.info("SILICON_RUNTIME is set to local/host; pulling without Docker runtime.")
        return False
    cfg = ensure_ready(auto_init=True, install=True, pull_image=True)
    maybe_prompt_login(cfg)
    return True


def _run(cmd: list[str], *, check: bool = False, capture: bool = False) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd,
            check=check,
            text=True,
            capture_output=capture,
        )
    except FileNotFoundError:
        ui.error(f"Command not found: {cmd[0]}")
        sys.exit(127)


def _compose_args(inst: registry.Install) -> list[str]:
    cfg = config_for_install(inst)
    return [*_docker_cmd(cfg), "compose", "-f", cfg["compose_file"]]


def _exec_args(inst: registry.Install, command: Iterable[str]) -> list[str]:
    return [*_docker_cmd(config_for_install(inst)), "exec", inst.container_name or container_name(inst.name), *command]


def container_running(inst: registry.Install) -> bool:
    cname = inst.container_name or container_name(inst.name)
    result = _run([*_docker_cmd(config_for_install(inst)), "inspect", "-f", "{{.State.Running}}", cname], capture=True)
    return result.returncode == 0 and result.stdout.strip().lower() == "true"


def silicon_running(inst: registry.Install) -> bool:
    if not container_running(inst):
        return False
    result = _run(_exec_args(inst, [
        "sh",
        "-lc",
        'test -s /silicon/.silicon.pid && kill -0 "$(cat /silicon/.silicon.pid)"',
    ]), capture=True)
    return result.returncode == 0


def _wait_for_container(inst: registry.Install, seconds: float = 20.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if container_running(inst):
            return True
        time.sleep(0.5)
    return False


def _exec_silicon(inst: registry.Install, args: list[str], *, check: bool = False) -> subprocess.CompletedProcess:
    return _run(_exec_args(inst, ["silicon", *args]), check=check)


def maintenance_silicon(inst: registry.Install, args: list[str], *, check: bool = False) -> subprocess.CompletedProcess:
    cfg = config_for_install(inst)
    image = inst.image or cfg.get("image") or DEFAULT_IMAGE
    shared_home = Path(cfg["shared_home"]).expanduser().resolve()
    shared_home.mkdir(parents=True, exist_ok=True)
    env = [
        "-e", f"SILICON_INSTANCE_NAME={inst.name}",
        "-e", "SILICON_CONTAINER_MODE=1",
        "-e", f"SILICON_SHARED_HOME={CONTAINER_SHARED_HOME}",
    ]
    user = host_user()
    volume = [
        "-v", f"{Path(inst.path).expanduser().resolve()}:{CONTAINER_PATH}",
        "-v", f"{shared_home}:{CONTAINER_SHARED_HOME}",
    ]
    cmd = [
        *_docker_cmd(cfg),
        "run",
        "--rm",
        "--entrypoint",
        "/usr/local/bin/silicon-runtime-entrypoint",
        *(["--user", user] if user else []),
        *env,
        *volume,
        image,
        "run",
        "silicon",
        *args,
    ]
    return _run(cmd, check=check)


def start_one(inst: registry.Install) -> None:
    ensure_ready(auto_init=False, install=True, pull_image=False, quiet=True)
    _ensure_image(config_for_install(inst))
    render_compose(config_for_install(inst))
    svc = inst.service or service_name(inst.name)
    ui.info(f"Starting Docker service '{svc}' for '{inst.name}'...")
    _run([*_compose_args(inst), "up", "-d", svc], check=True)
    if not _wait_for_container(inst):
        ui.error(f"Container for '{inst.name}' did not become healthy enough to exec into.")
        return
    # If the container is already alive but its Silicon was stopped, restart just the
    # Silicon process. If entrypoint already started it, this prints "already running".
    for _ in range(5):
        if silicon_running(inst):
            break
        result = _exec_silicon(inst, ["start", inst.name])
        if result.returncode == 0 or silicon_running(inst):
            break
        time.sleep(2)
    ui.success(f"'{inst.name}' Docker service is running.")


def stop_one(inst: registry.Install, *, full: bool = False) -> None:
    svc = inst.service or service_name(inst.name)
    if full:
        if container_running(inst):
            _exec_silicon(inst, ["stop", "--full", inst.name])
        _run([*_compose_args(inst), "stop", svc])
        ui.success(f"'{inst.name}' container stopped.")
        return

    if not container_running(inst):
        ui.warn(f"'{inst.name}' container is not running.")
        return
    _exec_silicon(inst, ["stop", inst.name])


def restart_one(inst: registry.Install) -> None:
    stop_one(inst, full=False)
    time.sleep(1)
    start_one(inst)


def run_silicon(inst: registry.Install, args: list[str]) -> int:
    ensure_ready(auto_init=False, install=True, pull_image=False, quiet=True)
    _ensure_image(config_for_install(inst))
    if container_running(inst):
        return _exec_silicon(inst, args).returncode
    return maintenance_silicon(inst, args).returncode


def debug(inst: registry.Install) -> None:
    log_file = Path(inst.path) / ".silicon.log"
    if not log_file.exists():
        ui.error(f"No log file found at {log_file}")
        sys.exit(1)
    print(f"\n{ui.BOLD}{ui.CYAN}Debugging '{inst.name}'{ui.RESET} (Docker)")
    print(f"{ui.DIM}  Log: {log_file}{ui.RESET}")
    print(f"{ui.DIM}  Press Ctrl+C to detach{ui.RESET}\n")
    try:
        subprocess.run(["tail", "-f", str(log_file)])
    except KeyboardInterrupt:
        pass


def print_status(inst: registry.Install) -> None:
    container = "running" if container_running(inst) else "stopped"
    silicon = "running" if silicon_running(inst) else "stopped"
    color = ui.GREEN if silicon == "running" else ui.DIM
    print(f"\n{ui.BOLD}{inst.name}{ui.RESET} {color}● {silicon}{ui.RESET} (Docker container {container})")
    print(f"{ui.DIM}  Path: {inst.path}{ui.RESET}")
    print(f"{ui.DIM}  Service: {inst.service or service_name(inst.name)}{ui.RESET}")
    print(f"{ui.DIM}  Compose: {inst.compose_file}{ui.RESET}\n")


def parse_init_args(args: list[str]) -> tuple[str | None, str | None, str | None]:
    root = None
    image = None
    shared_home = None
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--root" and i + 1 < len(args):
            root = args[i + 1]
            i += 2
        elif arg.startswith("--root="):
            root = arg.split("=", 1)[1]
            i += 1
        elif arg == "--image" and i + 1 < len(args):
            image = args[i + 1]
            i += 2
        elif arg.startswith("--image="):
            image = arg.split("=", 1)[1]
            i += 1
        elif arg == "--shared-home" and i + 1 < len(args):
            shared_home = args[i + 1]
            i += 2
        elif arg.startswith("--shared-home="):
            shared_home = arg.split("=", 1)[1]
            i += 1
        else:
            ui.error(f"Unknown docker init option: {arg}")
            sys.exit(1)
    return root, image, shared_home


def cmd_docker(args: list[str]) -> None:
    sub = args[0] if args else "status"
    if sub in {"init", "bootstrap", "doctor"}:
        root, image, shared_home = parse_init_args(args[1:])
        cfg = ensure_ready(auto_init=True, install=True, pull_image=True, root=root, image=image)
        if shared_home:
            init(cfg["root"], cfg["image"], shared_home=shared_home, docker_sudo=bool(cfg.get("docker_sudo")))
        if sub == "doctor":
            ui.success("Docker runtime is ready.")
        return
    if sub == "login":
        login(args[1:])
        return
    if sub == "status":
        cfg = load_config()
        if not cfg.get("enabled"):
            ui.warn("Docker runtime is not enabled. Run: silicon docker init")
            return
        ui.info(f"Root: {cfg['root']}")
        ui.info(f"Compose: {cfg['compose_file']}")
        ui.info(f"Shared auth home: {cfg['shared_home']}")
        ui.info(f"Image: {cfg['image']}")
        ui.info(f"Docker command: {'sudo docker' if cfg.get('docker_sudo') else 'docker'}")
        return
    if sub == "compose":
        print(load_config(required=True)["compose_file"])
        return
    ui.error("Usage: silicon docker <init|bootstrap|doctor|login|status|compose> [--root PATH] [--image IMAGE]")
    sys.exit(1)

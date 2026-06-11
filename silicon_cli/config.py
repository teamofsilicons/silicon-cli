"""Paths + endpoints. Everything is env-overridable so this CLI can point at
either the original Glass or your own."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

HOME = Path.home()
REGISTRY_DIR = Path(os.environ.get("SILICON_HOME", HOME / ".silicon"))
REGISTRY_FILE = REGISTRY_DIR / "registry.json"
CLI_SOURCE_FILE = REGISTRY_DIR / "cli-source"  # where `silicon script update` reinstalls from

# Glass sync server (pull/push). Override with GLASS_SERVER_URL to point elsewhere.
GLASS_SERVER_URL = os.environ.get("GLASS_SERVER_URL", "https://glass.teamofsilicons.com").rstrip("/")

# Stemcell — the base every new silicon is hydrated from.
STEMCELL_REPO = os.environ.get("SILICON_STEMCELL_REPO", "unlikefraction/silicon-stemcell")
STEMCELL_GIT_URL = f"https://github.com/{STEMCELL_REPO}.git"
STEMCELL_ZIP_URL = f"https://github.com/{STEMCELL_REPO}/archive/refs/heads/main.zip"

# Glass CLI (used by pull/push for backups).
GLASS_CLI_REPO = os.environ.get("SILICON_GLASS_CLI_REPO", "unlikefraction/glass")

# Silicon Interface CLI. During local development, silicon-cli will auto-detect
# a sibling silicon-interface checkout; in production this package spec is used.
SILICON_INTERFACE_CLI_PACKAGE = os.environ.get(
    "SILICON_INTERFACE_CLI_PACKAGE",
    "@teamofsilicons/silicon-interface-cli",
)
SILICON_INTERFACE_CLI_TARBALL = os.environ.get(
    "SILICON_INTERFACE_CLI_TARBALL",
    "https://registry.npmjs.org/@teamofsilicons/silicon-interface-cli/-/silicon-interface-cli-0.1.3.tgz",
)
SILICON_INTERFACE_CLI_SOURCE = os.environ.get("SILICON_INTERFACE_CLI_SOURCE", "")
SILICON_INTERFACE_CLI_SKIP = os.environ.get("SILICON_INTERFACE_CLI_SKIP", "").lower() in {
    "1", "true", "yes", "on",
}
SILICON_INTERFACE_DAEMON_SKIP = os.environ.get("SILICON_INTERFACE_DAEMON_SKIP", "").lower() in {
    "1", "true", "yes", "on",
}


def venv_python(path: str | os.PathLike) -> str | None:
    """The silicon's own .venv interpreter, if one exists."""
    sub = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    cand = Path(path) / ".venv" / sub
    return str(cand) if cand.exists() else None


def base_python_cmd() -> str:
    """The interpreter used to CREATE a silicon's venv (not this CLI's venv)."""
    return os.environ.get("SILICON_PYTHON") or shutil.which("python3") or shutil.which("python") or "python3"


def python_run_cmd(path: str | os.PathLike | None = None) -> str:
    """The interpreter used to RUN a silicon's code (not this CLI's venv).

    SILICON_PYTHON always wins; otherwise prefer the silicon's own .venv —
    system interpreters are often externally managed (PEP 668) and never
    received the silicon's dependencies.
    """
    if os.environ.get("SILICON_PYTHON"):
        return os.environ["SILICON_PYTHON"]
    if path:
        venv = venv_python(path)
        if venv:
            return venv
    return shutil.which("python3") or shutil.which("python") or "python3"

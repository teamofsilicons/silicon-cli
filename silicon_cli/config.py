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

# Glass sync server (pull/push). Kept as the original default for compatibility;
# override with GLASS_SERVER_URL to point at your own.
GLASS_SERVER_URL = os.environ.get("GLASS_SERVER_URL", "https://glass.unlikefraction.com").rstrip("/")

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
SILICON_INTERFACE_CLI_SOURCE = os.environ.get("SILICON_INTERFACE_CLI_SOURCE", "")
SILICON_INTERFACE_CLI_SKIP = os.environ.get("SILICON_INTERFACE_CLI_SKIP", "").lower() in {
    "1", "true", "yes", "on",
}


def python_run_cmd() -> str:
    """The interpreter used to RUN a silicon's main.py (not this CLI's venv)."""
    return os.environ.get("SILICON_PYTHON") or shutil.which("python3") or shutil.which("python") or "python3"

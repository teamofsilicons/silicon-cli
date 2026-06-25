#!/usr/bin/env bash
set -euo pipefail

SILICON_ROOT="${SILICON_ROOT:-/silicon}"
INSTANCE_NAME="${SILICON_INSTANCE_NAME:-silicon}"
SILICON_SHARED_HOME="${SILICON_SHARED_HOME:-/silicon-shared-home}"

export HOME="${SILICON_HOME_DIR:-$SILICON_ROOT/.home}"
export SILICON_HOME="${SILICON_CLI_HOME:-$HOME/.silicon}"
export SILICON_BROWSER_HOME="${SILICON_BROWSER_HOME:-$SILICON_ROOT/.silicon-browser}"
export PATH="/opt/silicon-runtime/bin:$PATH"

log() {
  printf '[silicon-runtime] %s\n' "$*" >&2
}

hash_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

link_shared_dir() {
  local name="$1"
  local target="$HOME/$name"
  local shared="$SILICON_SHARED_HOME/$name"
  mkdir -p "$shared"
  if [ -L "$target" ]; then
    return
  fi
  if [ -e "$target" ]; then
    mv "$target" "$target.local.$(date +%s)"
  fi
  ln -s "$shared" "$target"
}

link_shared_file() {
  local name="$1"
  local target="$HOME/$name"
  local shared="$SILICON_SHARED_HOME/$name"
  mkdir -p "$(dirname "$shared")"
  if [ -L "$target" ]; then
    return
  fi
  if [ -e "$target" ]; then
    if [ ! -e "$shared" ]; then
      mv "$target" "$shared"
    else
      mv "$target" "$target.local.$(date +%s)"
    fi
  fi
  ln -s "$shared" "$target"
}

prepare_shared_auth() {
  mkdir -p "$HOME" "$SILICON_SHARED_HOME"
  if [ "$HOME" = "$SILICON_SHARED_HOME" ]; then
    mkdir -p "$HOME/.claude" "$HOME/.codex" "$HOME/.config"
    return
  fi
  link_shared_dir ".claude"
  link_shared_dir ".codex"
  link_shared_dir ".config"
  link_shared_file ".claude.json"
}

prepare_runtime() {
  if [ ! -d "$SILICON_ROOT" ]; then
    log "missing mount: $SILICON_ROOT"
    exit 1
  fi
  if [ ! -f "$SILICON_ROOT/main.py" ]; then
    log "$SILICON_ROOT does not look like a Silicon instance; expected main.py"
    exit 1
  fi

  mkdir -p "$HOME" "$SILICON_HOME" "$SILICON_BROWSER_HOME"
  prepare_shared_auth
  cd "$SILICON_ROOT"

  if [ -f requirements.txt ]; then
    local venv_python="$SILICON_ROOT/.venv/bin/python"
    local req_hash
    local marker="$SILICON_ROOT/.venv/.silicon_requirements.sha256"
    req_hash="$(hash_file requirements.txt)"
    if [ ! -x "$venv_python" ]; then
      log "creating instance venv"
      python3 -m venv "$SILICON_ROOT/.venv"
    fi
    if [ ! -f "$marker" ] || [ "$(cat "$marker" 2>/dev/null || true)" != "$req_hash" ]; then
      log "installing instance Python dependencies"
      "$venv_python" -m pip install --upgrade pip >/dev/null
      "$venv_python" -m pip install -r requirements.txt
      printf '%s\n' "$req_hash" > "$marker"
    fi
  fi

  if command -v silicon-interface >/dev/null 2>&1; then
    if [ ! -x "$SILICON_ROOT/.silicon-interface/bin/si" ]; then
      log "installing Silicon Interface shim"
      silicon-interface install "$SILICON_ROOT" >/dev/null
    fi
  fi

  python - "$INSTANCE_NAME" "$SILICON_ROOT" <<'PY'
import sys
from pathlib import Path

from silicon_cli import registry

name = sys.argv[1] or "silicon"
root = Path(sys.argv[2]).resolve()
try:
    registry.register(name, str(root), str(root / ".silicon.pid"), update_existing=True)
except TypeError:
    registry.register(name, str(root), str(root / ".silicon.pid"))
PY
}

stop_runtime() {
  log "stopping Silicon"
  silicon stop --full "$INSTANCE_NAME" || true
}

if [ "${1:-}" = "auth" ]; then
  provider="${2:-all}"
  export HOME="$SILICON_SHARED_HOME"
  export SILICON_HOME="$HOME/.silicon"
  export SILICON_BROWSER_HOME="$HOME/.silicon-browser"
  mkdir -p "$HOME" "$SILICON_HOME" "$SILICON_BROWSER_HOME" "$HOME/.claude" "$HOME/.codex" "$HOME/.config"
  cd "$HOME"
  if [ "$provider" = "codex" ]; then
    log "Codex login uses the shared VM auth home: $HOME"
    codex login || true
  elif [ "$provider" = "claude" ]; then
    log "Claude Code uses the shared VM auth home: $HOME"
    printf '\nRun `claude` in this shell and complete the sign-in flow.\n'
    printf 'When sign-in is done, type `exit` to return to silicon.\n\n'
  else
    log "Shared auth shell: $HOME"
    printf '\nRun `claude` and/or `codex login` here.\n'
    printf 'When sign-in is done, type `exit` to return to silicon.\n\n'
  fi
  exec "${SHELL:-/bin/bash}"
fi

if [ "${1:-}" = "shared" ]; then
  shift
  export HOME="$SILICON_SHARED_HOME"
  export SILICON_HOME="$HOME/.silicon"
  export SILICON_BROWSER_HOME="$HOME/.silicon-browser"
  mkdir -p "$HOME" "$SILICON_HOME" "$SILICON_BROWSER_HOME" "$HOME/.claude" "$HOME/.codex" "$HOME/.config"
  cd "$HOME"
  if [ "$#" -eq 0 ]; then
    exec "${SHELL:-/bin/bash}"
  fi
  exec "$@"
fi

if [ "${1:-}" = "run" ]; then
  shift
  prepare_runtime
  exec "$@"
fi

if [ "${1:-}" = "shell" ]; then
  shift
  prepare_runtime
  exec "${SHELL:-/bin/bash}" "$@"
fi

prepare_runtime

# PIDs are namespaced to each container boot. Stale files from a previous
# container can point at an unrelated new process, so remove them before start.
rm -f "$SILICON_ROOT/.silicon.pid" "$SILICON_ROOT/.glass_agent.pid"

trap stop_runtime TERM INT

log "starting $INSTANCE_NAME"
silicon start "$INSTANCE_NAME" || log "initial start returned non-zero; container stays alive for inspection"

while true; do
  sleep 3600 &
  wait $! || true
done

# silicon-manager

Our own **`silicon`** CLI — a Python (pip-installable) port of the original bash
silicon manager. Manages silicon instances on a machine: create them from the
[silicon-stemcell](https://github.com/unlikefraction/silicon-stemcell) base,
start/stop them under an auto-restart watchdog, stream logs, and back them up to
Glass. It reads the same `~/.silicon/registry.json`, so existing installs carry
over unchanged.

## Install

```bash
python3 -m venv ~/.silicon/cli-venv
~/.silicon/cli-venv/bin/pip install /path/to/silicon-manager
ln -sf ~/.silicon/cli-venv/bin/silicon ~/.local/bin/silicon   # ensure ~/.local/bin is on PATH
```

(Zero runtime dependencies — stdlib only.)

## Commands

```
silicon                      Show status or list instances
silicon new [dir]            Create a new Silicon (hydrate from stemcell)
silicon new .                Hydrate the current folder into a runnable silicon
silicon start <target>       Start silicon(s). target = name, index, 1,2,4, or all
silicon stop [--full] <target>  Stop silicon(s) (--full also stops the glass agent)
silicon restart <target>     Restart silicon(s)
silicon agent <start|stop|status> [name]   Manage the per-silicon glass agent
silicon status [name]        Show instance status
silicon browser [name]       Open a headed browser for login
silicon debug [name]         Tail a running instance's logs
silicon attach [path]        Register an existing silicon directory
silicon pull <username>      Pull a silicon from Glass into a new folder
silicon push [name] [now|stop]   Hourly backups to Glass (now = one-shot, stop = end loop)
silicon update <target>      Update silicon(s) from the latest stemcell
silicon list                 List all instances
silicon script update        Update this CLI itself
silicon help                 Show help
```

## Configuration (env vars)

| Var | Default | Purpose |
| --- | --- | --- |
| `SILICON_HOME` | `~/.silicon` | registry + CLI state |
| `GLASS_SERVER_URL` | `https://glass.unlikefraction.com` | Glass sync server (pull/push) |
| `SILICON_STEMCELL_REPO` | `unlikefraction/silicon-stemcell` | base for `new` |
| `SILICON_GLASS_CLI_REPO` | `unlikefraction/glass` | glass backup CLI |
| `SILICON_PYTHON` | `python3` | interpreter used to run a silicon's `main.py` |

## How it differs from the bash version

- Pure Python package with a `silicon` console entry point (installed in an
  isolated venv), instead of a single bash script.
- The auto-restart watchdog runs as `silicon _watchdog` (a detached supervisor
  process) rather than a backgrounded bash function — same crash-loop detection,
  `.silicon.stop` sentinel, and `.silicon.pid`/`.silicon.log` behavior.
- `silicon script update` reinstalls the package via pip from its recorded source.
- Everything (server, stemcell repo) is env-overridable.

# silicon-cli

This is the single source for the installable **`silicon`** command. The PyPI
package is named `silicon-cli`, and the code lives here in this `silicon-cli`
repo.

The command manages silicon instances on a machine: create them from the
[silicon-stemcell](https://github.com/teamofsilicons/silicon-stemcell) base,
start/stop them under an auto-restart watchdog, stream logs, and back them up to
Glass. It reads the same `~/.silicon/registry.json`, so existing installs carry
over unchanged.

## Install

```bash
pip install silicon-cli
```

(Zero runtime dependencies — stdlib only.)

## Commands

```
silicon                      Show status or list instances
silicon new [dir]            Create a new Silicon (hydrate from stemcell)
silicon new .                Hydrate the current folder into a runnable silicon
silicon start <target>       Start silicon(s). target = name, *, all, 1,2,4, or name,name
silicon stop [--full] <target>  Stop silicon(s). target = name, *, all, 1,2,4, or name,name
silicon restart <target>     Restart silicon(s). target = name, *, all, 1,2,4, or name,name
silicon agent <start|stop|status> [name]   Manage the per-silicon glass agent
silicon status [name]        Show instance status
silicon browser [name]       Open a headed browser for login
silicon browser-profile setup <token>  Create a Steel browser profile through Glass
silicon browser-profile finish <token> <session_id> [before_ids_csv]  Finish profile setup
silicon debug [name]         Tail a running instance's logs
silicon attach [path]        Register an existing silicon directory
silicon pull [api_token]     Pull a Glass team or silicon into local folders
silicon push [name] [now|stop]   Daily 23:59 GMT backups to Glass (now = one-shot, stop = end loop)
silicon backup [name] [now|stop] Alias for silicon push
silicon update <target>      Update silicon(s) from the latest stemcell
silicon list                 List all instances
silicon docker init [--root ~/silicons] [--image ghcr.io/teamofsilicons/silicon-runtime:latest]
                             Install/check Docker and enable one-container-per-Silicon runtime
silicon docker doctor        Check/repair Docker runtime setup
silicon docker login [claude|codex|all]
                             Set up shared Claude/Codex auth for Docker silicons
silicon docker compose       Print generated Compose file path
silicon claude [args...]     Run Claude Code with shared Docker auth
silicon codex [args...]      Run Codex with shared Docker auth
silicon script update        Update this CLI itself
silicon help                 Show help
```

## Configuration (env vars)

| Var | Default | Purpose |
| --- | --- | --- |
| `SILICON_HOME` | `~/.silicon` | registry + CLI state |
| `GLASS_SERVER_URL` | `https://glass.teamofsilicons.com` | Glass sync server (pull/push) |
| `SILICON_STEMCELL_REPO` | `teamofsilicons/silicon-stemcell` | base for `new` |
| `SILICON_GLASS_CLI_REPO` | `teamofsilicons/glass` | glass backup CLI |
| `SILICON_PYTHON` | `python3` | interpreter used to run a silicon's `main.py` |
| `SILICON_INTERFACE_CLI_PACKAGE` | `@teamofsilicons/silicon-interface-cli` | npm package used to install the Silicon Interface CLI |
| `SILICON_INTERFACE_CLI_TARBALL` | versioned npm tarball | fallback package URL if registry metadata is briefly unavailable |
| `SILICON_INTERFACE_CLI_SOURCE` | *(empty)* | local package dir or `silicon-interface.mjs` path for dev installs |
| `SILICON_INTERFACE_CLI_SKIP` | *(empty)* | set to `1` to skip interface CLI setup |
| `SILICON_INTERFACE_DAEMON_SKIP` | *(empty)* | set to `1` to install the CLI without starting its listener daemon |
| `SILICON_RUNTIME` | *(empty)* | default pull uses Docker; set to `local` to opt out |
| `SILICON_RUNTIME_IMAGE` | `ghcr.io/teamofsilicons/silicon-runtime:latest` | runtime image for Docker-backed silicons |
| `SILICON_DOCKER_ROOT` | `~/silicons` | Docker-backed instance root |
| `SILICON_DOCKER_COMPOSE` | `<root>/compose.yml` | generated Compose file path |
| `SILICON_DOCKER_SHARED_HOME` | `<root>/.shared-home` | VM-wide Claude/Codex auth home mounted into every container |
| `SILICON_DOCKER_SUDO` | *(empty)* | set to `1` to run Docker commands through `sudo docker` |
| `SILICON_DOCKER_AUTO_INSTALL` | *(empty)* | set to `1` to allow non-interactive Docker install attempts |

## Docker runtime

Docker mode keeps the existing `silicon` command but runs each Silicon in its own
container. Mutable instance state lives on the host under `~/silicons/<name>` and
is bind-mounted at `/silicon` inside the container. Provider secrets are still
Glass-managed: the container stores only the Silicon's `.glass.json` key and the
stemcell fetches provider keys from Glass on boot.

Claude Code and Codex account state is shared across all Docker-backed silicons
on the VM. The shared auth home defaults to `~/silicons/.shared-home` and is
mounted into every container. During an interactive `silicon pull`, the CLI asks
whether to set up Claude Code, Codex, or both before installing the team.

On a fresh Linux server, the host only needs Python and this CLI. `silicon pull`
checks Docker Engine, Docker Compose v2, the daemon, current-user access, runtime
config, and the runtime image. When Docker is missing on Linux, it can install
Docker Engine through Docker's official `get.docker.com` installer, start the
daemon, and continue.

`silicon pull`, `silicon docker bootstrap`, and `silicon docker doctor` refresh
the configured runtime image with `docker pull` even when a local `latest` image
already exists. If the refresh fails but a cached image is available, the CLI
continues with the cached image and tells you to rerun `silicon docker doctor`.

```bash
pip install silicon-cli
silicon pull sct_live_...
```

To run the same checks explicitly:

```bash
silicon docker bootstrap --root ~/silicons --image ghcr.io/teamofsilicons/silicon-runtime:latest
silicon docker doctor
```

To set up or repair shared Claude/Codex login manually:

```bash
silicon docker login        # asks which accounts to set up
silicon docker login claude # Claude Code only
silicon docker login codex  # Codex only
silicon docker login all    # both
```

Codex login uses `codex login --device-auth` inside the runtime container so it
works on remote/headless servers without browser port forwarding.

To use those same shared accounts directly from the VM without installing Claude
or Codex on the host:

```bash
silicon claude
silicon codex
silicon claude --version
silicon codex --version
```

The runtime image contains the Silicon runtime dependencies: Python tooling, git,
Node, Silicon Browser, Silicon Interface CLI, Claude Code, and Codex. Each
container creates `/silicon/.venv` and installs that Silicon's `requirements.txt`
inside the mounted instance folder, so per-Silicon Python dependencies do not
pollute the host.

To force the older host-local install path:

```bash
SILICON_RUNTIME=local silicon pull sct_live_...
```

After that, the normal commands continue to work:

```bash
silicon list
silicon start all
silicon stop ada          # stops the Silicon process; container stays up
silicon stop --full ada   # stops the container
silicon debug ada
silicon update ada
silicon backup ada now
```

The generated Compose file is written to `~/silicons/compose.yml`. You can inspect
it with:

```bash
silicon docker compose
```

Build the runtime image from this repo:

```bash
docker build -f docker/runtime/Dockerfile -t ghcr.io/teamofsilicons/silicon-runtime:latest .
```

## Silicon Interface CLI

For Docker-backed installs, the Silicon Interface CLI is installed inside the
runtime image/container. For host-local installs, `silicon new`, `silicon install`,
and `silicon pull` set up the Silicon Interface CLI in the silicon folder when
Node 22+ is available. When a Glass `.glass.json` is present, setup also starts
the background listener daemon so the silicon receives live conversation frames
without polling.

`silicon pull` is token-native. Generate a team setup token from Glass >
Silicons > Team setup token, then run:

```bash
silicon pull
# paste the token when prompted

# or, less private because it lands in shell history:
silicon pull sct_live_...
```

The command validates the token with Glass, mints one local silicon API key per
team silicon, creates one folder per silicon, hydrates the stemcell, writes
`.glass.json`, `.env`, and `env.py`, registers each instance, regenerates Compose
for Docker-backed installs, and starts each silicon process.

During team pull, setup asks for the default brain/fallback settings once. You
can apply those settings to every silicon, or select specific silicons that
need different settings and answer the brain prompt for those silicons only.
Older per-silicon `scs_live_...` tokens still pull just that one silicon.

Provider API keys for voice, browser profiles, billing, and architecture
generation are configured on the Glass backend. After the silicon API token is
validated, `silicon pull` reads the team's provider-key metadata from Glass and
automatically marks the pulled silicon(s) to use Glass-managed keys when every
provider is available. Plaintext secrets are never returned to the CLI. A
Glass-pulled silicon only stores its Glass API token plus a local marker that
provider keys come from Glass.

The local wrappers are written to:

```bash
<silicon>/.silicon-interface/bin/si
<silicon>/.silicon-interface/bin/silicon-interface
<silicon>/.silicon-interface/inbox.jsonl
<silicon>/.silicon-interface/state.json
```

For Glass-pulled silicons, those wrappers automatically use the folder's
`.glass.json` (`server_url` + `api_key`) for conversation API auth.

During local development, point `SILICON_INTERFACE_CLI_SOURCE` at the package:

```bash
SILICON_INTERFACE_CLI_SOURCE=../silicon-interface/packages/silicon-interface-cli silicon new ./ada
```

## Backups

`silicon push <name> now` and `silicon backup <name> now` use the
`.backupsilicon` manifest when the instance has one. The CLI archives those
paths and uploads them to Glass via `/api/v1/silicon-backups/` with the
instance's `.glass.json` API key. If no manifest exists, the command falls back
to the legacy `glass push` snapshot flow.

`silicon push <name>` starts the background loop. Scheduled manifest backups run
daily at 23:59 GMT; `now` remains available for one-shot manual backups.

## How it differs from the bash version

- Pure Python package with a `silicon` console entry point (installed in an
  isolated venv), instead of a single bash script.
- The auto-restart watchdog runs as `silicon _watchdog` (a detached supervisor
  process) rather than a backgrounded bash function — same crash-loop detection,
  `.silicon.stop` sentinel, and `.silicon.pid`/`.silicon.log` behavior.
- `silicon script update` reinstalls the package via pip from its recorded source.
- Everything (server, stemcell repo) is env-overridable.

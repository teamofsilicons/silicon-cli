# silicon-cli

This is the single source for the installable **`silicon`** command. The PyPI
package is named `silicon-cli`, and the code lives here in this `silicon-cli`
repo.

The command manages silicon instances on a machine: create them from the
[silicon-stemcell](https://github.com/unlikefraction/silicon-stemcell) base,
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
silicon debug [name]         Tail a running instance's logs
silicon attach [path]        Register an existing silicon directory
silicon pull [api_token]     Pull a Glass team or silicon into local folders
silicon push [name] [now|stop]   Daily 23:59 GMT backups to Glass (now = one-shot, stop = end loop)
silicon backup [name] [now|stop] Alias for silicon push
silicon update <target>      Update silicon(s) from the latest stemcell
silicon list                 List all instances
silicon script update        Update this CLI itself
silicon help                 Show help
```

## Configuration (env vars)

| Var | Default | Purpose |
| --- | --- | --- |
| `SILICON_HOME` | `~/.silicon` | registry + CLI state |
| `GLASS_SERVER_URL` | `https://glass.teamofsilicons.com` | Glass sync server (pull/push) |
| `SILICON_STEMCELL_REPO` | `unlikefraction/silicon-stemcell` | base for `new` |
| `SILICON_GLASS_CLI_REPO` | `unlikefraction/glass` | glass backup CLI |
| `SILICON_PYTHON` | `python3` | interpreter used to run a silicon's `main.py` |
| `SILICON_INTERFACE_CLI_PACKAGE` | `@teamofsilicons/silicon-interface-cli` | npm package used to install the Silicon Interface CLI |
| `SILICON_INTERFACE_CLI_TARBALL` | versioned npm tarball | fallback package URL if registry metadata is briefly unavailable |
| `SILICON_INTERFACE_CLI_SOURCE` | *(empty)* | local package dir or `silicon-interface.mjs` path for dev installs |
| `SILICON_INTERFACE_CLI_SKIP` | *(empty)* | set to `1` to skip interface CLI setup |
| `SILICON_INTERFACE_DAEMON_SKIP` | *(empty)* | set to `1` to install the CLI without starting its listener daemon |

## Silicon Interface CLI

`silicon new`, `silicon install`, and `silicon pull` also set up the
Silicon Interface CLI in the silicon folder when Node 22+ is available.
When a Glass `.glass.json` is present, setup also starts the background listener
daemon so the silicon receives live conversation frames without polling.

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
`.glass.json`, `.env`, and `env.py`, registers each instance, starts each
Silicon Interface daemon, and starts each silicon process.

During team pull, setup asks for the default brain/fallback settings once. You
can apply those settings to every silicon, or select specific silicons that
need different settings and answer the brain prompt for those silicons only.
Older per-silicon `scs_live_...` tokens still pull just that one silicon.

Provider API keys for voice, browser profiles, billing, and architecture
generation are configured on the Glass backend. After the silicon API token is
validated, `silicon pull` reads the team's provider-key metadata from Glass,
shows each provider as saved/missing without returning plaintext secrets, and
asks whether the pulled silicon(s) should use the Glass-managed keys. A
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

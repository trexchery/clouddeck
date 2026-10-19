# CloudDeck guide

CloudDeck is the terminal UI on top of the `ddc` controller. The controller (`clouddeck/controller.py`) does everything that touches
leases, bids, SSH, checkpoints and the spending rules; the TUI and the remote API call it, so `ddc` and `clouddeck` always agree on config,
limits and state. Overview of the pieces: [ARCHITECTURE.md](ARCHITECTURE.md).

```
clouddeck/tui/*      Textual app: tabs, dialogs, polling, task runner                      (what you see)
clouddeck/service.py live probes, projects and config edits, Claude sessions, dispatch      (works on any provider with SSH)
clouddeck/providers/ Provider contract + registry | akash.py (adapter over the controller) | demo.py (simulated)
clouddeck/remote*.py loopback controller API + stdlib client for a terminal on the box
clouddeck/controller.py   the `ddc` engine
```

## Running it

| Command | Purpose |
|---|---|
| `clouddeck` | the TUI on Akash (needs a config; `ddc init` creates one). Needs an interactive terminal |
| `clouddeck -p demo` | the same UI on a simulated provider: try everything, nothing real is touched (`CLOUDDECK_PROVIDER=demo` makes it the default) |
| `clouddeck status [--json]` | one-shot summary for scripts |
| `clouddeck doctor` | read-only environment check: Python, Textual, config problems and limits, `state_repo`, `ssh`, `gpg`, key and secrets present, Akash API reachable, balance, state file, watchdog registered |
| `clouddeck providers` | list providers and their capabilities |
| `clouddeck install` | writes a `clouddeck` launcher next to existing shims (`%USERPROFILE%\bin\clouddeck.cmd` / `~/.local/bin/clouddeck`); never touches `ddc` or `claude-cloud` |
| `clouddeck serve ...` / `clouddeck remote ...` | remote-terminal access, see [below](#remote-terminal-access) |
| `python -m clouddeck` | the same as `clouddeck` |

With `-p demo` or `clouddeck doctor -p demo` a missing config is only a warning. `-p` may come before or after the subcommand.

From a plain checkout (not `pip install`): `bin/clouddeck` (Linux/macOS) or `windows\clouddeck.cmd`, which put the checkout on `PYTHONPATH`.

## Keys

`1` to `5` tabs, `Ctrl+P` command palette (every action, plus theme switching), `/` search the current table, `s` or a header click sorts,
`r` refresh, `?` help, `q` quit (leases keep running; **quitting never closes anything**).

| Tab | Keys |
|---|---|
| **1 Compute** | `d` deploy, `h` hardware picker, `z` resize, `x` terminate, Enter open a shell, `i` / `p` / `g` sub-tabs (instances, providers, GPU market), `f` GPU-only, `s` sort, `u` reload market |
| **2 Workers** | `+` add one, `s` scale to N, `d` dispatch a workload, Enter inspect a node, `x` terminate, `h` heal, `a` shell |
| **3 Projects** | `a` add, `Delete` forget, `e` environment, `n` new Claude session, `c` continue, Enter attach, `k` kill session, `s` seed to the box, `D` deploy the project's environment, `Tab` switch table |
| **4 Monitor** | `l` choose the log source (events, `ddc.log`, dev agent, Claude pane), `f` follow |
| **5 Recovery** | `c` checkpoint now, `v` verify, `a` reconnect, `h` heal workers, `s` start / restore, `z` snooze idle shutdown, `t` idle policy, `X` stop everything |

Every action is also in the palette. Narrow terminals switch to a stacked layout.

## Safety in the UI

The rules are the controller's ([COSTS-AND-SAFETY.md](COSTS-AND-SAFETY.md)); the UI adds visibility and friction:

* **Only leases CloudDeck created can be changed.** Other deployments on the account are listed read-only, marked *external*; the UI
  refuses to terminate or resize them.
* **Every deploy, resize and scale-up shows the spend guard's verdict before you confirm:** price cap, worst case (cap x hours),
  balance after the worst case, and whether the guard allows it. A refusal (or a guard that cannot be evaluated) shows the reason and
  offers no confirmation. The controller checks again when the request is actually made.
* **Every terminate, resize, scale-down and stop-all lists what will be destroyed, what it cost so far, and the checkpoint status,
  and needs a typed phrase** (`stop dev`, `terminate`, `resize`, `scale down`, `stop all`; `discard workspace` when the dev box
  would be destroyed with checkpoints off).
* **Closing the dev box checkpoints first.** Terminate and resize (replace-and-restore) run a final checkpoint, decrypted and compared on
  your PC. If it fails the box stays up. Workers are closed only after that.
* **No signing keys and no Akash API key ever leave the PC.** The settings form edits `idle_minutes`, `worker_idle_minutes` and
  `checkpoint_minutes` only (it keeps comments, writes `config.toml.bak`, and re-parses the result before swapping it in); `budget_guard`
  and the limits are never edited from the UI.
* **The UI never freezes on errors.** Blocking work runs in threads; failures become a message with a next step.

## Compute

`d` opens the deploy form: role (dev box, worker, or generic SSH instance), preset (the `[profiles.N]` of your config) or custom
vCPU / RAM / disk, GPU count and model, runtime limit and price cap. The cap defaults to a suggestion from the network reference price
and your own lease history; the panel shows expected price, cap, worst case and the guard verdict as you type. The dev box's runtime
limit always follows `max_runtime_hours`, so its final checkpoint happens before the platform closes it.

Akash cannot change the resources of a running lease, so **Resize = replace**: the old instance is closed (the dev box after a
verified checkpoint) and a new one is created (the dev box restored from the newest checkpoint). Custom sizes are stored in
`custom_profiles.json` in the state directory so the watchdog and `ddc scale -p x8-16-50` know them.

Generic instances (role "box") have **no idle agent and no checkpoint**: they are bounded only by their runtime limit, and a plain
`ddc stop` leaves them running (close them in the UI or with `ddc stop --only <dseq>`).

**Choosing hardware.** Beyond presets you can set CPU, RAM, disk, GPU count and model by hand, or open the **hardware picker** (`h` on the Compute
tab, or from the deploy form) to browse what the network advertises and pick a CPU architecture or a GPU (vendor, model, memory, interface),
a region, audited providers only, or specific providers. What is advertised, screened and confirmed, how prices are estimated, and what happens
offline: [HARDWARE.md](HARDWARE.md). The picker never deploys: it hands a spec to the deploy dialog, which shows the guard verdict as usual.

## Workers

Scale to N (new workers use a preset), add one, close one, heal. New workers need the dev box first, and N is bounded by `max_workers`,
`max_total_leases` and the budget guard. **Dispatch** runs any shell command on all workers, the selected node or the dev box and
merges the output live. Dispatching sends a heartbeat so the workers are not closed as idle while a job runs.

Reusable commands and named hardware environments live in `config.toml`:

```toml
[[workloads]]
name = "Unit tests"
command = "cd /work/my-project && npm test"
target = "workers"          # or "dev"
description = "full suite on every worker"

[environments.ml]           # named presets a project can point at (Projects -> e, then D to deploy it)
profile = "32"              # a profile name, or: cpu / mem_gb / disk_gb / gpu / gpu_model / max_usd_hr / hours
```

On the box, `dist.sh` (see [`lease/README.md`](../lease/README.md)) copies a project to the workers and runs jobs across them. A sync
is **destructive on the workers' copy** (`rsync --delete`), and workers receive your project files that way.

## Projects, environments, Claude Code

Projects are the `[[repos]]` and `[[dirs]]` of `config.toml`. Adding or forgetting one edits the config (comments and line endings are
kept, a `config.toml.bak` is written, the result is re-parsed before it replaces the file). "Forget" removes only the config entry,
never anything on the box. Seeding copies a project onto the box only when it is not there yet.

Claude Code sessions are tmux windows named `cc-<project>` on the dev box; attaching hands your terminal to `ssh` + `tmux` and
reconnects on drops, then returns to CloudDeck when you detach (`Ctrl-\` then `d`). Whether Claude Code is installed on the box is
`install_claude_code` in the config; whether your login is copied there is `push_claude_credentials` (default off). Claude Code is
Anthropic's software, installed from npm and subject to Anthropic's terms.

## Recovery and idle shutdown

Checkpoint, verify, reconnect, heal, start/restore and stop everything. **Snooze** (30 min to 4 h) keeps the dev box up past an *idle*
shutdown request; it is stored in `state.json` as `idle_hold_until` and honoured by the watchdog. It never overrides the runtime-limit
stop. Checkpoint details and failure modes: [CHECKPOINTS.md](CHECKPOINTS.md).

"Stop everything" closes the dev box (after a verified checkpoint) and the workers; generic instances stay unless closed individually.
With checkpoints off it refuses unless you type `discard workspace`.

## Providers

A provider is a class with five required methods; everything else is optional and gated by `capabilities`. The contract, the spending
guard that every deploy, scale and resize must go through, and how to register a plugin are in
[ARCHITECTURE.md](ARCHITECTURE.md#extension-points). `clouddeck/providers/demo.py` is the smallest complete example and what the
tests drive.

## Remote-terminal access

Goal: use the fleet from a terminal on the dev box (including Akash Console's shell into it) **without that box ever holding Akash or
signing credentials**.

```powershell
clouddeck serve --tunnel --scope fleet     # on your PC: API on 127.0.0.1:8765 + SSH reverse tunnel to the dev box + token and client pushed to it
# on the dev box:
clouddeck-remote status
clouddeck-remote scale 2 32                # 2 workers of preset 32
clouddeck-remote dispatch "npm test" --on workers
clouddeck-remote checkpoint
```

* The API binds to loopback only; the dev box reaches it through an SSH **reverse tunnel opened from your PC**. Nothing listens on the
  internet. Because of the tunnel, every process on the box can reach the API: the box is untrusted, so tokens unlock only what their
  scope allows.
* Scopes: `read` (status only) < `fleet` (also scale workers, dispatch to workers, checkpoint) < `admin` (also run commands on the dev
  box). Scaling is bounded by `remote_max_workers` (default 4), `max_workers` and the budget guard, and is refused when the guard is off.
  No scope can deploy a dev box, terminate it, read secrets or edit config.
* Tokens expire (default 24 h; with `--tunnel` the dev box's runtime limit plus 1 h; `--expires-hours H`, 0 = never), only their SHA-256 is
  stored, `clouddeck serve --list-tokens` lists them, `--revoke LABEL` revokes one. While `serve --tunnel` runs, each new dev box gets a
  fresh token (the old one stops working). Every request is appended to `remote-audit.log` in the state directory. `status` lists only
  the instances CloudDeck created, and error replies never contain secrets or your PC's folder names. `--tunnel` works with Akash only.
* `clouddeck serve --new-token LABEL --scope read` prints a token once for any other HTTP client (`Authorization: Bearer ...`); the
  endpoints are `GET /v1/status` and `POST /v1/scale`, `/v1/dispatch`, `/v1/checkpoint`.
* `clouddeck remote ACTION` runs the same client from your PC (`--url` or `CLOUDDECK_URL`).

Full list of limits and the threat model: [SECURITY.md](../SECURITY.md#the-remote-controller-api).

## Tests

`python -m pytest` runs the whole suite offline: provider contract, spending and ownership safety, state and ledger damage handling,
the watchdog, config validation, lease scripts (Linux/WSL), remote API authentication and limits, and end-to-end TUI flows driven by
real key presses on the demo provider. How to run it safely: [CONTRIBUTING.md](../CONTRIBUTING.md).

## Known limits

Resize replaces instead of resizing in place (an Akash limitation). Seeding and checkpoints are implemented for Akash only. Which provider
wins an order is decided by Akash's auction and your price cap (the catalogue helps you set expectations); the dev box's runtime limit is
fixed by `max_runtime_hours`. When the Akash API itself is unreachable the UI keeps working from local state (the last known data stays on screen)
and the summary line shows a warning such as `⚠ Akash API unreachable: <short reason> (cached data)`; the full text is in Monitor → events, and the
warning clears with the next successful refresh (`clouddeck doctor` also reports whether the API is reachable). The full list of safety limitations is in [SECURITY.md](../SECURITY.md#known-limitations).

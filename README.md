# CloudDeck

Rent a disposable Linux dev box (and optional test workers) on [Akash Network](https://akash.network) for the hours you need it,
driven from your own PC: a keyboard-driven terminal UI (`clouddeck`) on top of the `ddc` command-line controller
("Disposable Dev Cloud"). Your work is saved in encrypted, verified checkpoints, spending is capped at several levels, and when you
stop, billing stops.

**Status: early alpha (0.1.0).** It works, it is used, and it has sharp edges. Read the warning below before you point it at an
account that holds money.

## Warning: money, secrets and limits

> [!WARNING]
> * **This spends real money.** Every lease CloudDeck creates bills your Akash balance by the block until it is closed. Price caps,
>   runtime limits and a budget guard bound the cost, but they are limits, not guarantees, and this is alpha software.
> * **Leases keep billing until they are closed:** by `ddc stop`, by the TUI, by the watchdog, or by the platform's runtime limit. The
>   watchdog only works while **your PC is on, awake and logged in**.
> * **The Akash Console API key can spend your balance** and create and close deployments. Use a dedicated account with a small
>   balance and a key you can revoke. The key stays on your PC and is never sent to a box.
> * **Secrets and source code are copied to rented, third-party machines.** The dev box receives your project files, the checkpoint
>   passphrase, a write deploy key for your state repository, optionally one repository's deploy key and optionally your Claude login.
>   Akash providers can read everything on their machines while a lease runs. Treat every box as untrusted.
> * **Runtime limits:** every lease is ordered with a platform-enforced runtime limit (default 8 h for the dev box, 4 h for workers, at
>   most 24 h). That limit is Akash's behaviour and is the only protection that works with your PC off; it has not been independently
>   verified for this release, so test it once with a tiny lease ([Quickstart](https://github.com/trexchery/clouddeck/blob/main/docs/QUICKSTART.md#7-first-real-start-with-a-small-profile)).
> * **No warranty** (MIT license). Nothing here is financial advice. You are responsible for your Akash account and for complying
>   with the terms of Akash and of the providers you rent from.
> * **Not affiliated with or endorsed by Akash Network or Anthropic.** "Claude" and "Claude Code" are trademarks of Anthropic, PBC.
>   Installing Claude Code on the box is optional (`install_claude_code` in the config), and signing in to it is up to you.

Details: [Costs and safety](https://github.com/trexchery/clouddeck/blob/main/docs/COSTS-AND-SAFETY.md) and
[SECURITY.md](https://github.com/trexchery/clouddeck/blob/main/SECURITY.md).

## What it is

`ddc start` orders one Linux container on Akash with a price cap and a runtime limit, installs a small set of scripts on it, restores your
latest checkpoint, seeds your repositories from your PC, and starts a tmux session (`claude-cloud` attaches to it from your terminal). Optional
worker boxes fan out test or build jobs. When you are done, `ddc stop` takes a final checkpoint, **verifies it by decrypting it on your PC**,
closes only the leases CloudDeck created, and checks that they stopped billing. The simulated provider (`clouddeck -p demo`) lets you try the
whole UI for free.

## Features

* **Dev box and worker fleet** on Akash: presets (16 / 32 / 64 vCPU by default), custom sizes, GPU orders, per-order price caps.
* **Terminal UI** with five tabs (Compute, Workers, Projects, Monitor, Recovery): live utilisation, spend and burn rate, provider catalogue
  and GPU market, command palette, themes. Destructive actions list what they close and need a typed phrase.
* **Hardware picker:** any CPU architecture or GPU the network advertises right now (vendor, model, memory, interface, count, region, audited
  or pinned providers), with advertised, screened and confirmed availability kept apart ([docs/HARDWARE.md](https://github.com/trexchery/clouddeck/blob/main/docs/HARDWARE.md)).
* **Encrypted checkpoints** (gpg AES256) in a private GitHub repository you own; periodic and final; verified on your PC; restored on the
  next start. Refuses to replace a good checkpoint with a truncated one.
* **Layered spending limits**: price cap in the order, platform runtime limit, budget and reserve guard, lease and worker caps, hard
  ceilings that hold even with the guard off, and a scheduled watchdog for idle shutdown.
* **Batch jobs** (`ddc job`): one short-lived lease, lease late, verified artifacts, verified close, through the same spending gate as every lease.
* **Ownership safety**: only leases that CloudDeck created (recorded in its state file *and* named `ddc-*`) can ever be closed; other
  deployments on your account are shown read-only.
* **Fails closed**: a damaged state file or ledger stops new leases; an unknown API answer is never read as "closed"; a create request is
  never blindly retried.
* **Remote access for a terminal on the box** (loopback API over an SSH reverse tunnel, scoped expiring tokens) without credentials on the box.
* **Claude Code in tmux** on the dev box (optional, pinned version), project sessions from the TUI.
* **Provider plugin contract**: other clouds plug in; the simulated provider is the reference implementation.

## Quickstart

Prerequisites: Python 3.11+, `git`, an OpenSSH client, `gpg` (for checkpoints), the GitHub CLI `gh` (signed in, for the one-time install), and an
Akash Console account with a funded balance and an API key. Per-OS commands: [QUICKSTART.md](https://github.com/trexchery/clouddeck/blob/main/docs/QUICKSTART.md).

```bash
# 1. Install, and try the simulated provider (free; nothing real is touched)
pipx install "git+https://github.com/trexchery/clouddeck@claude-skill"   # private repo, no PyPI: `gh auth login` + `gh auth setup-git` first
clouddeck -p demo

# 2. Create your config and secrets, then paste your Akash API key into the secrets file `ddc init` prints
ddc init
#    set state_repo = "OWNER/your-private-state-repo" in the config (a NEW private GitHub repo) and, for a cheap first test,
#    max_runtime_hours = 2; then register keys, the state repo and the watchdog:
ddc install

# 3. Rent a box, work on it, stop it
ddc start -p 16        # worst case: $0.50/h price cap x 2 h runtime limit = $1.00
claude-cloud           # your terminal becomes the box's tmux session
ddc stop               # final checkpoint, verified here, then closed; billing confirmed stopped
```

## Command reference

`ddc` (the controller; `ddc --version`, `ddc --config PATH`, `ddc COMMAND --help`):

| Command | What it does |
|---|---|
| `ddc init` | write a starter `config.toml` and `secrets.env` (never overwrites) |
| `ddc install [--dry-run] [--yes] [--no-watchdog] [--migrate]` | keys, private state repo, deploy keys, balance alert, watchdog (Windows task / systemd user timer / cron); `--dry-run` lists the GitHub actions, `--migrate` (Windows) replaces a pre-release task and shims |
| `ddc uninstall` | remove the watchdog registration (keys, config and state stay) |
| `ddc start [-p PROFILE] [-w N] [--worker-profile P]` | provision or re-check the dev box, restore, seed, start tmux; optionally add N workers |
| `ddc attach [claude\|workers\|shell]` | interactive session over ssh + tmux (`claude-cloud` is the same) |
| `ddc scale N [-p PROFILE] [--explore] [--only ID] [--dry-run] [--yes]` | grow or shrink the workers; shrinking asks for confirmation |
| `ddc status` | leases, spend, budget, alerts, idle state, last checkpoint |
| `ddc bench [cpu] [cmd]` | benchmark every box and print the cost per workload (`cmd` runs `bench_cmd` from the config) |
| `ddc checkpoint [--allow-shrink]` | encrypted checkpoint now, verified on this PC |
| `ddc verify` | fetch and re-verify the newest checkpoint |
| `ddc stop [--workers-only] [--dry-run] [--yes] [--only ID] [--force] [--discard-workspace] [--allow-shrink]` | list what will be closed, final verified checkpoint of the dev box, then close only our leases and confirm billing stopped |
| `ddc heal [--only ID] [--dry-run]` | re-provision workers whose container the provider restarted |
| `ddc reconcile [--restore-backup] [--adopt [--only ID] [--yes]]` | compare our records with the account; repair a damaged state file; closes only a lease whose create reply was lost |
| `ddc watchdog [--once]` | one enforcement pass (what the scheduler runs) |
| `ddc ssh [dev\|wNN]`, `ddc run 'CMD' [--timeout S]` | shell or one command on a box |
| `ddc job run\|check\|sdl\|hardware\|report\|close ...` | batch jobs on one short-lived lease with minimal paid idle time (`ddc job --help`; see above) |

`clouddeck` (the UI and helpers; `-p/--provider NAME`, `--version`):

| Command | What it does |
|---|---|
| `clouddeck` / `clouddeck tui` | the terminal UI (`-p demo` = simulated) |
| `clouddeck status [--json]` | one-shot summary |
| `clouddeck doctor` | read-only environment check |
| `clouddeck providers` | list providers and capabilities |
| `clouddeck install` | create a `clouddeck` launcher next to existing shims |
| `clouddeck serve [--port N] [--tunnel] [--scope read\|fleet\|admin] [--expires-hours H] [--new-token LABEL] [--revoke LABEL] [--list-tokens]` | loopback controller API for a terminal on the box |
| `clouddeck remote status\|instances\|scale\|dispatch\|checkpoint [--url URL] [--json]` | talk to that API; on the box the same client is `clouddeck-remote` |

`claude-cloud` is `ddc attach`. Keys in the UI: `1` to `5` tabs, `Ctrl+P` palette, `/` search, `?` help, `q` quit (never closes anything).
Full guide: [docs/CLOUDDECK.md](https://github.com/trexchery/clouddeck/blob/main/docs/CLOUDDECK.md).

## Safety model: what is and is not guaranteed

**By design (and covered by the offline test suite):**

* The Akash API key, wallet material and your access SSH key never leave your PC.
* Only leases in `state.json` that are also named `ddc-*` (and belong to your account) can be closed. Anything else is read-only. (If
  `state.json` itself is damaged, `ddc stop` falls back to a read-only listing of the `ddc-*` deployments on your account.)
* Every order carries a price cap (no bid above it can win) and `runtimeLimitHours`. A new lease is refused if it would exceed the
  lease/worker caps, the hard ceilings (8 leases, 7 workers, $2.00/h by default, 24 h) or, with the guard on, `budget_usd` / `reserve_usd`
  (defaults $25 / $5; `budget_usd` counts lifetime spend).
* Nothing is closed (workers included) until the dev box's final checkpoint decrypts on your PC with matching entry counts and git HEADs,
  unless you pass `--force --discard-workspace`.
* Creates are sent once; unknown outcomes are reconciled, not retried. Damaged state or ledger blocks new leases, never stops.
* Secrets and keys are created owner-only on Linux/macOS; the remote API is loopback-only with expiring, scoped tokens.

**Not guaranteed:**

* **The platform's runtime limit is the only independent hard stop**, and it is not verified for this release. Everything else depends on
  software running on your PC.
* **The watchdog needs your PC on, awake and logged in** (a scheduled task, systemd user timer or cron entry). If it is not running, an
  idle box keeps billing until its runtime limit. macOS has no watchdog installer.
* **You pay the winning bid**, anything up to the cap, for as long as a lease runs. Generic instances (TUI "box" role) are not closed by
  the watchdog or by a plain `ddc stop`.
* **Providers are untrusted but see everything on their machine.** SSH host keys are trusted on first use, deploy keys are long-lived
  and reused across leases (not per-lease), and checkpoint verification proves hashes, decryptability, entry counts and git HEADs, not a
  content digest. `ddc stop --force --discard-workspace` and the watchdog's emergency stops (budget exhausted, box unreachable 30 min)
  close a box without a verified final checkpoint and without a rescue copy.
* Several machines using one Akash account do not know about each other's leases.
* Windows file protection relies on your user-profile permissions.
* Not independently audited.

Precise statements, the limits table and worked worst cases: [COSTS-AND-SAFETY.md](https://github.com/trexchery/clouddeck/blob/main/docs/COSTS-AND-SAFETY.md).
Threat model and which secret goes to which host: [SECURITY.md](https://github.com/trexchery/clouddeck/blob/main/SECURITY.md).

## Generic jobs with minimal paid idle time

`ddc job` runs a batch workload (CPU or GPU, any hardware the network offers) on **one short-lived lease** and keeps what you pay for
close to what actually runs. It assumes no persistent storage: inputs are uploaded or downloaded, steps run detached and resumable,
results are pulled back and checksum-verified, and the lease is closed and verified closed.

```bash
ddc job check examples/cpu-smoke.toml        # preflight, read-only (includes the same spending gate a real lease passes)
ddc job run   examples/cpu-smoke.toml        # create -> bids -> lease late -> steps -> verified artifacts -> verified close
ddc job close NAME                           # close a job's lease (after --keep or a killed run)
```

What it does about idle time: **bid early, lease late** (local preparation overlaps bidding and boot), downloads and steps start the
moment sshd is up, a prebuilt image skips apt, bids are ranked by expected total cost, and an idle or runaway job is closed by the runner.
The largest waste is usually a box idling while local preparation finishes, not the boot; a simulation with assumed, synthetic inputs (clearly
labelled ESTIMATED) models the saving ([JOBS.md](https://github.com/trexchery/clouddeck/blob/main/docs/JOBS.md)). The `examples/` used above are in the
[repository](https://github.com/trexchery/clouddeck/tree/main/examples) and the sdist, not in the pip wheel: copy one from there.

Safety: a job lease is created **only through the controller's single creation path** (the same budget guard, lease caps, per-lease price
ceiling, 24 h runtime ceiling, write-ahead record and unknown-outcome handling as a dev box) and closed **only through its
ownership-checked close**. If the `ddc job run` process dies, `ddc watchdog` closes the lease after `job_orphan_minutes` (default 30, at least 10). A plain `ddc stop` closes job leases too. The
secrets a job spec lists are pushed over ssh only, never put in the SDL or logged, **but the rented host can read them while the lease
runs.** Details: [Costs and safety](https://github.com/trexchery/clouddeck/blob/main/docs/COSTS-AND-SAFETY.md#jobs-ddc-job), [prebuilt image](https://github.com/trexchery/clouddeck/blob/main/docs/prebuilt-image.md).

## Hardware selection

Three levels, all ending in a price cap and a runtime limit that go into the order:

1. **Presets:** the `[profiles.N]` tables in `config.toml` (default: 16, 32 and 64 vCPU with caps of $0.50, $0.90 and $1.70 per hour).
   `ddc start -p 32`, `ddc scale 3 -p 16`.
2. **Custom sizes** in the TUI deploy form (`d` on the Compute tab): vCPU, RAM, disk, GPU count and model, runtime limit and cap, with a live
   price estimate, the worst case and the spend guard's verdict before you confirm. Custom profiles are remembered so the watchdog knows them.
3. **The hardware picker** (`h` on the Compute tab, or from the deploy form): choose any CPU architecture or GPU (vendor, model, memory,
   interface, count), a region, audited providers only, or specific providers, and compare providers side by side. The lists come from Akash's
   public data at run time, not from a list in this program, so new hardware shows up without a release; offline it falls back to the last
   cached copy and says so. Every row says how sure it is: **advertised** (what providers report, an upper bound), **screened** (you pressed
   "check availability" and the network says a node of that provider fits this exact order; still unpriced) or **confirmed** (a real bid, which
   only exists after you order). Prices before an order are estimates with their source named. Constraints only narrow an order: they never loosen a limit.

Which provider wins an order is decided by the auction among those that bid under your cap: the best price per (measured, else estimated) CPU
speed wins. How the picker works, what the labels mean and what happens offline: [docs/HARDWARE.md](https://github.com/trexchery/clouddeck/blob/main/docs/HARDWARE.md).

## Platform support

| Platform | Status | Tested | Watchdog |
|---|---|---|---|
| Windows 11 (Python 3.11 to 3.13) | supported | CI (`windows-latest`, 3.12) and maintainer runs on 3.12; used against real leases | scheduled task, every 5 min |
| WSL2 Ubuntu (24.04, Python 3.12) | supported | maintainer runs; used against real leases | systemd user timer or cron (WSL VM must be awake) |
| Linux (Ubuntu/Debian-family, 3.11 to 3.13) | supported | CI (`ubuntu-latest`, 3.11 and 3.13) | systemd user timer or cron |
| macOS | **unsupported / experimental** | not tested | **none** |

"Tested" means the offline suite (simulated provider, in-memory Akash fake) unless it says real leases. Details and per-OS
setup: [docs/PLATFORMS.md](https://github.com/trexchery/clouddeck/blob/main/docs/PLATFORMS.md).

### Verified and not verified (0.1.0)

<!-- Release manager: replace each placeholder in the Status column with "verified <date>" or "not verified"; release_check.py --strict fails until then. -->
What was checked against real Akash leases for this release (small 2-vCPU leases, the released wheel, an isolated test config and a
throw-away private state repository; every close was confirmed separately through the Console API's deployment state and spend rate),
and what was only tested offline:

| Area | Status |
|---|---|
| Dev box start, attach, checkpoint, verified stop (Windows) | verified 2026-10-18 (Windows 11 and WSL2 Ubuntu 24.04): start, `ddc run`, `ddc checkpoint` + PC-side verify, `ddc stop` (final checkpoint verified before the close), restore of that checkpoint onto a new lease from another provider |
| Workers: scale up, distributed run, scale down | scale up, the `max_total_leases` refusal and scale down verified 2026-10-18; a distributed run was not exercised against real leases |
| Watchdog idle shutdown and budget stop | verified 2026-10-18 (one `ddc watchdog --once` pass at a time, as the scheduler runs it): idle stop and budget stop, each with a verified final checkpoint before the close |
| `ddc job` end to end (one CPU job, artifacts, verified close) | verified 2026-10-18 (`examples/cpu-smoke.toml`: bid, lease, step, sha256-verified artifact, verified close in under 2 minutes) |
| Platform runtime limit closes a lease with the PC off | verified once 2026-10-18: a `ddc job --keep` lease with `runtimeLimitHours=1` and no watchdog was closed by Akash 60 min after it was leased (one provider, one observation) |
| Linux and WSL against real leases | verified 2026-10-18 on WSL2 Ubuntu 24.04 (dev box lifecycle, checkpoint, `ddc job`); not on a native Linux host |
| GPU orders | not verified (no GPU lease was rented for this release) |

Also found live and fixed before release: a private key that other Windows accounts can read (for example after copying `keys\` to a
new PC) made ssh refuse it and the lease time out; `ddc start` now loads the key locally first and refuses before anything is rented.
Not fixed: the Akash Console API answered `HTTP 405` to the balance-alert request, so set that alert in the Console web UI.

Everything else is covered by the offline suite only. Known gaps: [SECURITY.md](https://github.com/trexchery/clouddeck/blob/main/SECURITY.md#known-limitations).

## Configuration

`ddc init` writes `config.toml` from [`config.example.toml`](https://github.com/trexchery/clouddeck/blob/main/config.example.toml): limits,
lifetimes, `state_repo`, the image, profiles, and the projects (`[[repos]]`, `[[dirs]]`) to seed. Secrets live in `secrets.env`
(names in [`.env.example`](https://github.com/trexchery/clouddeck/blob/main/.env.example)). Defaults are deliberately small; raise them on purpose.
Locations: `%APPDATA%\clouddeck` / `~/.config/clouddeck` (config), `~/.config/clouddeck` (secrets), `%LOCALAPPDATA%\clouddeck` /
`~/.local/state/clouddeck` (state); `DDC_CONFIG`, `DDC_SECRETS_DIR`, `DDC_STATE_DIR` override.

## Troubleshooting

Start with `clouddeck doctor` (read-only) and `ddc status`; the log is `ddc.log` in the state directory (also in the TUI's Monitor tab).
`CLOUDDECK_DEBUG=1` shows full tracebacks.

| Symptom | Meaning and fix |
|---|---|
| `no config found: run ddc init` | create a config with `ddc init`, or set `DDC_CONFIG` |
| `AKASH_API_KEY missing in ...` / `HTTP 401` | put the key in `secrets.env` (uncomment the line); a 401 means Akash rejected it: create a new key |
| `preflight failed, nothing was created: ...` | the listed tool, key file, secret or project path is missing; fix it (`ddc install` creates keys). Nothing was spent |
| `would exceed budget` / `would breach reserve` / `max_total_leases` / `max_workers` | the guard says no. `budget_usd` counts lifetime spend: raise it on purpose, add balance, or lower the plan ([COSTS-AND-SAFETY.md](https://github.com/trexchery/clouddeck/blob/main/docs/COSTS-AND-SAFETY.md)) |
| `no acceptable bids` | no provider bid under your price cap (or none with enough uptime). Raise the cap, shrink the spec, or retry; the empty order is closed for you |
| `ssh never came up` / `lease never exposed ssh port` | the provider was slow or broken. The new lease is closed automatically; retry |
| `checkpoints are disabled (state_repo is empty): closing the dev box would DESTROY ...` | `ddc stop` refuses to discard the box's contents. Set `state_repo` and run `ddc install`, or `ddc stop --discard-workspace` if you accept losing them |
| `stop aborted, dev box left RUNNING because the final checkpoint did not verify` | nothing was closed and everything is still billing. Check state-repo access, `gpg` and the passphrase (`ddc verify`), then `ddc stop`; `--force --discard-workspace` closes without a verified checkpoint and loses unsaved work |
| `state.json is unusable ... will NOT create leases` | damaged state file (a copy is kept). `ddc reconcile` shows what the account has; `--restore-backup` or `--adopt` repairs it. `ddc status` and `ddc stop` keep working (stop works from a read-only listing of the `ddc-*` deployments on your account) |
| `another ddc command holds ...` | a start, stop or watchdog pass is running (cross-process lock); wait for it |
| `gh` errors during `ddc install` | install the GitHub CLI and run `gh auth login`; `ddc install --dry-run` shows what it will do |
| `ddc` not found after install (Windows) | open a new terminal (the PATH change applies to new shells) |
| WSL: ssh rejects the key | keep the secrets directory on the Linux filesystem, not under `/mnt/c` |
| workers unreachable after a provider restart | `ddc heal` re-provisions them (their disk is empty again) |
| `CloudDeck needs an interactive terminal` | run `clouddeck` in a real terminal, or use `clouddeck status --json` in scripts |

## Documentation

[Quickstart](https://github.com/trexchery/clouddeck/blob/main/docs/QUICKSTART.md) ·
[CloudDeck guide](https://github.com/trexchery/clouddeck/blob/main/docs/CLOUDDECK.md) ·
[Costs and safety](https://github.com/trexchery/clouddeck/blob/main/docs/COSTS-AND-SAFETY.md) ·
[Checkpoints](https://github.com/trexchery/clouddeck/blob/main/docs/CHECKPOINTS.md) ·
[Hardware](https://github.com/trexchery/clouddeck/blob/main/docs/HARDWARE.md) ·
[Jobs](https://github.com/trexchery/clouddeck/blob/main/docs/JOBS.md) ·
[Platforms](https://github.com/trexchery/clouddeck/blob/main/docs/PLATFORMS.md) ·
[Architecture](https://github.com/trexchery/clouddeck/blob/main/docs/ARCHITECTURE.md) ·
[Lease scripts](https://github.com/trexchery/clouddeck/blob/main/lease/README.md) ·
[Changelog](https://github.com/trexchery/clouddeck/blob/main/CHANGELOG.md)

## Contributing, security, license

Contributions are welcome: [CONTRIBUTING.md](https://github.com/trexchery/clouddeck/blob/main/CONTRIBUTING.md) (tests never spend real money or
touch real state) and the [Code of Conduct](https://github.com/trexchery/clouddeck/blob/main/CODE_OF_CONDUCT.md). Report vulnerabilities privately
via [GitHub security advisories](https://github.com/trexchery/clouddeck/security/advisories/new), see
[SECURITY.md](https://github.com/trexchery/clouddeck/blob/main/SECURITY.md). Third-party software and trademarks:
[THIRD_PARTY_NOTICES.md](https://github.com/trexchery/clouddeck/blob/main/THIRD_PARTY_NOTICES.md).

Licensed under the [MIT License](https://github.com/trexchery/clouddeck/blob/main/LICENSE).

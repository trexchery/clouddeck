# Quickstart

From nothing to a running dev box and back to "nothing is billing", on Windows 11, Linux, WSL or macOS (experimental). Plan on about
half an hour and **a few dollars at most** for the first real run. Steps 1 to 3 cost nothing.

> **Real money.** Steps 7 and 8 create and close a paid Akash lease. Read the warning block in the [README](../README.md#good-to-know)
> and [COSTS-AND-SAFETY.md](COSTS-AND-SAFETY.md) first. Use a dedicated Akash account with a small balance.

## 1. Prerequisites

| You need | Why | Check |
|---|---|---|
| Python **3.11 or newer** | CloudDeck itself | `python --version` (Windows: `py --version`) |
| `git` | seeds your repos onto the box, verifies checkpoints | `git --version` |
| OpenSSH client (`ssh`, `ssh-keygen`) | every connection to a box | `ssh -V` |
| `gpg` (GnuPG) | verifies encrypted checkpoints on your PC (only if you set `state_repo`) | `gpg --version` |
| GitHub CLI `gh`, signed in | `ddc install` only: creates the private state repo and deploy keys, fetches GitHub's SSH host keys | `gh auth status` (sign in with `gh auth login`) |
| A GitHub account | the private repository for encrypted checkpoints | |
| An Akash Console account with a funded balance and an API key | the money and the API | step 4 |

(`tmux` and `tar` are needed on the box, not on your PC; the default box image has them.) Details per OS, including where files live:
[PLATFORMS.md](PLATFORMS.md).

**Windows 11**

```powershell
winget install Python.Python.3.12
winget install Git.Git
winget install GitHub.cli
```

The OpenSSH client is built into Windows 11 (Settings > System > Optional features if it is missing). Git for Windows brings `gpg`.
Open a new terminal after installing.

**Ubuntu / Debian / WSL**

```bash
sudo apt install python3 python3-venv python3-pip git openssh-client gnupg
```

Install `gh` from <https://cli.github.com/> (distribution packages may be old). Ubuntu 24.04 has Python 3.12; Ubuntu 22.04 has 3.10,
which is too old: install a newer Python first. On WSL, keep everything on the Linux side (see [PLATFORMS.md](PLATFORMS.md#wsl-specifics)).

**macOS** (unsupported, expect rough edges; there is no watchdog installer): `brew install python git gnupg gh`.

For checkpoint-backed dev boxes, sign in with `gh auth login` before `ddc install`.

## 2. Install CloudDeck

Install CloudDeck from PyPI (version 0.1.1 or newer). You don't need a GitHub login to install the package.

With [pipx](https://pipx.pypa.io) (its own virtual environment, commands on your PATH):

```bash
pipx install clouddeck
ddc --version
clouddeck --help
```

Or with pip (ideally in a virtual environment):

```bash
python -m pip install --user clouddeck
```

This installs three commands: `clouddeck` (the terminal UI), `ddc` (the command-line controller) and `claude-cloud` (attach to the box;
the same as `ddc attach`). To update, run the same command again with `--force` (pipx) or `--upgrade` (pip).

## 3. Try the simulated provider (free)

```bash
clouddeck -p demo
```

A full TUI on fake instances: nothing contacts Akash and nothing is rented (CloudDeck only keeps its own log and theme preference in its
state directory).
Press `1` to `5` for the tabs, `?` for all keys, `Ctrl+P` for the command palette, `q` to quit. Try `d` (deploy) on the Compute tab to
see the price cap, worst case and budget-guard verdict a real deploy would show. `clouddeck doctor -p demo` shows what is configured.

## 4. Akash account, API key and funding

1. Sign in at <https://console.akash.network> (create an account if you have none). **Use an account you dedicate to this**: the API key
   can spend whatever it holds.
2. Add credits to the account's balance. How you pay, the minimum and the exact screens are Akash's and change over time: follow
   Akash's own documentation. Fund only what you are willing to lose.
3. Create an **API key** (API Keys section of the Console) and keep it safe; you will paste it into a file in step 5. It stays on your
   PC.

**How much?** The first real run below uses the defaults of `config.example.toml`. A box is ordered with a *price cap* and a *runtime
limit*, so its worst case is cap x hours:

| What | Cap | Runtime limit | Worst case |
|---|---|---|---|
| default dev box (profile `32`: 32 vCPU, 64 GiB) | $0.90/h | 8 h | $7.20 |
| small first test (profile `16`: 16 vCPU, 32 GiB) | $0.50/h | 2 h (set in step 5) | $1.00 |

The budget guard refuses to start a lease unless `balance - what running leases may still cost - the new worst case` stays above
`reserve_usd` (default $5): about $12.20 for the default box, about $6 for the small test. The price you actually pay is the winning
bid, normally below the cap, and only for the time the box runs. A balance of **$10 to $15** covers a first session comfortably.
Everything about limits: [COSTS-AND-SAFETY.md](COSTS-AND-SAFETY.md).

## 5. `ddc init`: config and secrets

```bash
ddc init
```

This writes two files and refuses to overwrite anything that exists:

* the config: `%APPDATA%\clouddeck\config.toml` (Windows) or `~/.config/clouddeck/config.toml`
* the secrets file: `secrets.env` in `%USERPROFILE%\.config\clouddeck\` or `~/.config/clouddeck/` (mode 0600 on Linux/macOS)

**Secrets.** `secrets.env` starts as commented placeholders. Remove the leading `#` and paste your key (no quotes needed):

```
AKASH_API_KEY=paste-your-api-key-here
# `ddc install` generates DDC_BACKUP_PASSPHRASE for you ...
# CLAUDE_CODE_OAUTH_TOKEN=
```

`ddc install` appends `DDC_BACKUP_PASSPHRASE` (it encrypts your checkpoints; **back this file up in a password manager**, a lost passphrase
means unrecoverable checkpoints). `CLAUDE_CODE_OAUTH_TOKEN` is optional and only used if you turn on `push_claude_credentials`.
Names of every setting: [`.env.example`](../.env.example).

**Config.** Open `config.toml`; the defaults are deliberately small. For the first run change:

```toml
max_runtime_hours = 2        # platform hard stop for the dev box: worst case 2 h x $0.50 = $1.00 with profile 16
# state_repo = "OWNER/your-private-state-repo"   # see step 6
```

Optionally add a project to seed onto the box (a path on *this* machine; `push = true` gives the box a write key for that one repository):

```toml
[[repos]]
name = "my-project"
path = "~/code/my-project"
github = "OWNER/my-project"
push = false
```

With no `[[repos]]` you simply get an empty `/work`. Settings are explained in [`config.example.toml`](../config.example.toml).

## 6. `ddc install`: keys, state repo, watchdog

Checkpoints (encrypted snapshots in a private GitHub repo) are what let you throw a box away without losing work. For a first
experiment with nothing valuable on the box you can leave `state_repo` empty, but then `ddc stop` will refuse to close the dev box unless
you pass `--discard-workspace`. For real use, set `state_repo` to a **new private repository** you own:

```toml
state_repo = "OWNER/your-private-state-repo"
```

```bash
ddc install --dry-run     # lists the GitHub actions it would take, changes nothing
ddc install               # asks "Continue? [type yes]" before the GitHub actions; --yes skips the question
```

`ddc install` (needs `gh` signed in, even with an empty `state_repo`):

* generates the backup passphrase if missing, and SSH keys (`access`, `worker`, plus `state_deploy` and one key per `push = true` repo)
  in the secrets directory;
* creates the private state repo if it does not exist (it refuses an existing repo that holds anything else), and adds a **write**
  deploy key to the state repo and to each `push = true` repo;
* saves GitHub's SSH host keys and your global git name/email for the box to use;
* registers the **watchdog** that stops idle boxes: a Windows scheduled task `CloudDeck-Watchdog` (plus `ddc.cmd` / `claude-cloud.cmd`
  in `%USERPROFILE%\bin`, added to your user PATH, and five Desktop shortcuts), or a systemd user timer / crontab entry on Linux and
  WSL (`--no-watchdog` skips it; macOS: nothing is registered);
* tries to create an Akash Console email alert for a low balance (if it logs "Akash balance alert not configured", set one in the
  Akash Console web UI: see [COSTS-AND-SAFETY.md](COSTS-AND-SAFETY.md)).

Open a new terminal on Windows afterwards. Check everything: `clouddeck doctor`.

## 7. First real start with a small profile

```bash
ddc status                 # balance and limits; "No ddc leases running (nothing is billing)."
ddc start -p 16            # shows the worst-case quote; creates nothing without approval
ddc start -p 16 --approve-usd 1.00  # after approving the $1.00 maximum for this 2 h example
```

With `--approve-usd`, `ddc start` validates the config, checks tools and keys, then creates the lease, waits for bids, picks the best price per measured speed
within your cap, installs the lease scripts, restores your newest checkpoint (if any), seeds repos missing from it, and starts a tmux
session. It prints the host, the price and the hard stop time and ends with:

```
Ready. Run:  claude-cloud     (dev box <host>:<port>, $<price>/h, hard stop after 2 h)
```

Then:

```bash
claude-cloud               # your terminal becomes the box's tmux session (reconnects on drops; Ctrl-\ then d detaches; F2/F3 switch windows)
ddc run 'nproc; free -g'   # or run a single command
clouddeck                  # the full TUI: instances, spend, projects, checkpoints
```

Claude Code is installed on the box (pinned version, `install_claude_code = true`) but **not signed in** unless you set
`push_claude_credentials = true` (not recommended: the host can read it). Sign in inside the session yourself; the login is not saved in
checkpoints, so you repeat it on each new box. Set `install_claude_code = false` to skip installing it.

**Verify the platform hard stop once.** The runtime limit is the only protection that works with your PC off, and it is Akash's
behaviour, not this program's. Test it cheaply: set `max_runtime_hours = 1`, run `ddc uninstall` (so the watchdog does not close the box
first), `ddc start -p 16 --approve-usd 0.50`, do **not** run `ddc stop`, and look at the Console's Deployments page after an hour: the deployment should have
closed by itself. Then run `ddc install` again. Worst case for the test: $0.50.

## 8. Verify billing stopped, and stop

```bash
ddc stop --dry-run         # lists exactly what would be closed and the checkpoint status
ddc stop                   # shows the list, asks "Close these leases now? [type yes]"
```

`ddc stop` first takes a final checkpoint on the dev box and **verifies it on your PC** (decrypts it and compares entry counts and git
HEADs); only then does it close the workers and the dev box. If the checkpoint cannot be verified, **nothing** is closed and you get an
alert: fix the cause, or, if you accept losing the box's unsaved work, `ddc stop --force --discard-workspace`. With `state_repo` empty
there is nothing to verify and you must add `--discard-workspace` yourself. In scripts use `--yes`.
Closing the box through the TUI (Compute tab, `x`) asks you to type a confirmation phrase.

After each close the controller asks the Akash API whether the lease is still billed, and only records it as closed when it no longer
is. You can confirm yourself:

```bash
ddc status                 # "No ddc leases running (nothing is billing)."
```

and in the Akash Console: no active deployment you did not expect, and a balance that stops falling. If `ddc stop` ever reports that a
lease was *not* closed, run it again or close that deployment in the Console.

## Optional: a batch job on one short-lived lease

For a batch workload (not an interactive box), `ddc job` leases late, runs the steps, pulls and verifies the results and closes the lease.
Try the smallest one first (CPU only, a price cap of $0.20/h, one hour). The examples are in the [repository](https://github.com/trexchery/clouddeck/tree/main/examples)
(and in the sdist), **not** in the pip wheel: from a pip install, copy `examples/cpu-smoke.toml` from there into a directory of your own first.

```bash
ddc job check examples/cpu-smoke.toml     # preflight, read-only; includes the same spending gate a real lease passes
ddc job run   examples/cpu-smoke.toml --approve-usd 0.20  # after approving the $0.20 maximum; outputs next to the spec
ddc status                                # job leases show up here (role job); nothing should be left running
```

A job lease counts toward `max_total_leases` and the budget like any other, and `ddc watchdog` closes one whose runner died. If you
set `unsafe_disable_limits` or raised `max_price_usd_hr`, read [JOBS.md](JOBS.md) first.

### Jobs-only setup (GPU/CPU batch jobs)

For batch jobs you need only Python, OpenSSH and an Akash Console API key (no `gh`, `gpg` or state repo). Install as in step 2, then one
command creates the config, secrets and ssh key, stores the key and registers the watchdog (Windows: only the `CloudDeck-Watchdog` task):

```powershell
Get-Content key.txt | ddc setup --api-key-stdin --watchdog      # bash: ddc setup --api-key-stdin --watchdog < key.txt
```

Or type the key then press Ctrl-D (Ctrl-Z, Enter on Windows). It prints what is missing and exits 0 only when ready (`--json` for a
machine-readable report). Then `ddc job hardware --available` lists what can be rented. Every paid command (`ddc job run`, `ddc start`,
`ddc scale`) needs `--approve-usd X`: run it once without to see the worst case, then re-run with at least that amount.

To let Claude Code drive it: `npx skills add trexchery/clouddeck -g -a claude-code`.

## 9. Next steps

* Workers and distributed jobs: `ddc scale 2` (quote first, then approve via `--approve-usd`; within `max_workers`), `dist.sh` on the box, the TUI's Workers tab. See [CLOUDDECK.md](CLOUDDECK.md).
* Hardware (GPU, custom sizes): the deploy form and [HARDWARE.md](HARDWARE.md).
* Batch jobs with minimal paid idle time: [JOBS.md](JOBS.md).
* Checkpoints, restore and failure modes: [CHECKPOINTS.md](CHECKPOINTS.md).
* Raise limits on purpose, understand worst cases: [COSTS-AND-SAFETY.md](COSTS-AND-SAFETY.md).
* If something fails: [Troubleshooting in the README](../README.md#troubleshooting).

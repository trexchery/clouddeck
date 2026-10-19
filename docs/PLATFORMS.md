# Platform support

CloudDeck runs on **your own machine** (the "PC"): it needs Python 3.11 or newer and a few command-line tools, and it talks to the Akash
Console API and to the rented boxes over SSH. The boxes themselves are always Linux containers (Debian/Ubuntu-based image).

## Support matrix

| Platform | Status | Automated tests | Used against real Akash leases | Watchdog (idle / budget enforcement) |
|---|---|---|---|---|
| **Windows 11** (Python 3.11, 3.12, 3.13) | supported | CI (`windows-latest`, 3.12); the maintainers also run the suite locally on 3.12 | yes: this is the platform the tool was written and used on | Windows Task Scheduler task `CloudDeck-Watchdog`, every 5 min, registered by `ddc install` |
| **WSL2, Ubuntu** (24.04, Python 3.12) | supported | run locally on Ubuntu 24.04 in WSL2 | yes, for 0.1.0: dev box lifecycle, checkpoint and restore, `ddc job` (watchdog passes were run by hand, not by the timer) | systemd user timer, else cron; see the WSL notes below |
| **Linux** (Ubuntu/Debian-family, Python 3.11 to 3.13) | supported | CI (`ubuntu-latest`, 3.11 and 3.13) | not on a native Linux host yet (WSL2 runs the same code paths) | systemd user timer `clouddeck-watchdog.timer`, else a marked crontab block |
| **macOS** | **unsupported / experimental** | none | none | **none**: `ddc install` does not register anything; see below |

"Automated tests" always means the offline test suite (simulated provider, in-memory Akash fake, temp directories): it never contacts
Akash and never spends money. It does not prove that a real lease works on that OS. The release checks against real leases are listed in the README
("Verified and not verified"). On a native Linux host, treat CloudDeck as "should work, please report": the only OS-specific parts are
the `ssh` invocation, file permissions, tool discovery and the scheduler, which WSL2 exercises with the same code.

Python 3.11 is the floor (`requires-python = ">=3.11"`). CI runs 3.11 and 3.13 on Linux and 3.12 on Windows. Dependencies: `textual>=8.2,<9`,
`rich>=14`, `tomlkit>=0.12`; everything else is the standard library.

## Tools the PC needs

| Tool | Needed for | Notes |
|---|---|---|
| Python >= 3.11 | everything | `pip install clouddeck` |
| `git` | seeding your repos onto the box, verifying checkpoints | |
| OpenSSH client (`ssh`, `ssh-keygen`) | every connection to a box, key generation | Windows: the built-in client in `C:\Windows\System32\OpenSSH` is used when present |
| `gpg` (GnuPG) | decrypting and verifying checkpoints on the PC (only when `state_repo` is set) | Windows: Git for Windows' bundled `gpg` is found automatically; otherwise set `DDC_GPG` |
| GitHub CLI `gh`, logged in (`gh auth login`) | `ddc install` only: creates the private state repo, adds deploy keys, downloads GitHub's SSH host keys | a one-time setup tool; day-to-day use does not call it |
| `schtasks` (Windows) / `systemctl --user` or `crontab` (Linux) | the watchdog | provided by the OS |

`ddc install` needs `gh` even if you leave `state_repo` empty (it downloads GitHub's SSH host keys through it). `tmux` and `tar` are
needed on the **box**, not on the PC; the default image has them.

## Where files live

| What | Windows | Linux / macOS / WSL |
|---|---|---|
| config (`config.toml`) | `%APPDATA%\clouddeck\` | `~/.config/clouddeck/` (honours `XDG_CONFIG_HOME`) |
| secrets (`secrets.env`, `keys/`) | `%USERPROFILE%\.config\clouddeck\` | `~/.config/clouddeck/` |
| state (`state.json`, `ledger.jsonl`, logs, local checkpoint copies) | `%LOCALAPPDATA%\clouddeck\` | `~/.local/state/clouddeck/` (honours `XDG_STATE_HOME`) |

`DDC_CONFIG`, `DDC_SECRETS_DIR` and `DDC_STATE_DIR` override all three, and `ddc --config PATH` overrides the config. Installations
made before the project was renamed keep working: if a directory from the old layout already exists it is still used, so nothing has to
be moved. The file list and formats are in [ARCHITECTURE.md](ARCHITECTURE.md#state-files).

`ddc job` uses the same ssh client and key; its files (`jobs/`, `telemetry/`) live in the state directory. It needs no extra tool on your PC.

## Windows 11

* Install the prerequisites with winget (`winget install Python.Python.3.12`, `Git.Git`, `GitHub.cli`). The OpenSSH client is a built-in
  optional feature (Settings > System > Optional features) and is present on a default Windows 11.
* `ddc install` creates `ddc.cmd` and `claude-cloud.cmd` in `%USERPROFILE%\bin`, adds that folder to your **user** PATH, creates five
  Desktop shortcuts (CloudDeck Start / Stop / Status / Benchmark / Attach) and the scheduled task `CloudDeck-Watchdog`. Existing shims
  and shortcuts are never overwritten (`--migrate` replaces them). Open a new terminal afterwards.
* The task is created for your user without a stored password, so it runs **only while you are logged on** and the PC is awake.
* Desktop alerts use `msg.exe`, which is missing on some editions (for example Windows Home). Set `alert_command` in `config.toml` for
  anything else (a script that posts to a webhook, for instance).
* Secrets and keys are protected by the permissions of your user profile; CloudDeck does not change Windows ACLs.

## Linux and WSL

* Debian/Ubuntu: `sudo apt install python3 python3-venv python3-pip git openssh-client gnupg`, plus the GitHub CLI from
  <https://cli.github.com/>. Python 3.11+ is required: Ubuntu 22.04 ships 3.10 (use a newer Python), Ubuntu 24.04 ships 3.12.
* `ddc install` registers a **systemd user timer** (`clouddeck-watchdog.timer`, every 5 min). Without a systemd user session it falls back
  to a marked block in your crontab (a copy of your previous crontab is saved in the state directory first, and nothing is written if
  `crontab -l` cannot be read); with neither it prints a loud warning and registers nothing. Skip the registration with `--no-watchdog`;
  remove it with `ddc uninstall`.
* A systemd user timer stops with your last login session unless you run `loginctl enable-linger $USER`.
* Secrets and keys are created `0600` (directories `0700`), the token store of `clouddeck serve` is `0600`.
* Desktop notifications use `notify-send` when a display is available; otherwise only the log, `ddc status` and your `alert_command`.

### WSL specifics

* The watchdog only runs while the WSL virtual machine is awake. `ddc install` prints this reminder; to keep it running when no WSL
  terminal is open, also schedule `wsl.exe -d <your-distro> -- ddc watchdog --once` every 5 minutes in the Windows Task Scheduler.
* Keep `~/.config/clouddeck` on the WSL filesystem. Directories under `/mnt/c` carry Windows permissions, and OpenSSH refuses private
  keys that look world-readable.
* Use the Linux versions of `git`, `gh` and `gpg` inside WSL, not the Windows binaries through interop.
* Do not run `ddc` from Windows and from WSL against the same account at the same time with separate state directories: each one only
  knows the leases it created (see [COSTS-AND-SAFETY.md](COSTS-AND-SAFETY.md#several-machines-one-account)).

## macOS (unsupported)

The code is portable Python and may well work, but nothing in this project has been run on macOS. Known gaps:

* `ddc install` does **not** register a watchdog (launchd is not implemented). It logs a warning and says so again in its last line; until you schedule
  `ddc watchdog --once` every 5 minutes yourself (launchd `StartInterval` 300), nothing stops an idle box and only the platform's
  runtime limit bounds spending. `ddc start` cannot detect a missing watchdog on macOS and does not warn, so remember this yourself.
* There are no desktop notifications; use `alert_command`.
* Paths follow the Linux layout (`~/.config/clouddeck`, `~/.local/state/clouddeck`).

If you try it, please report what you find.

## Watchdog schedulers at a glance

| OS | Registered by `ddc install` | Runs when | Remove with |
|---|---|---|---|
| Windows | scheduled task `CloudDeck-Watchdog` (5 min) | you are logged on, PC awake | `ddc uninstall` |
| Linux / WSL | `clouddeck-watchdog.timer` (user), else a crontab block | user manager running (see linger); WSL VM awake | `ddc uninstall` |
| macOS | nothing | never, unless you schedule it | n/a |

`clouddeck doctor` reports whether a watchdog is registered (a failure once something is running and none is), `ddc start` logs a warning
when it finds none (on Windows and Linux/WSL; on macOS it cannot tell and says nothing), and `ddc status` prints recent alerts,
including watchdog failures. What the watchdog does, and what protects you when it is not running, is described in
[COSTS-AND-SAFETY.md](COSTS-AND-SAFETY.md#the-watchdog-and-what-happens-when-it-does-not-run).

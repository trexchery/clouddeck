# Security policy

CloudDeck handles an API key that can spend money and copies credentials and source code onto rented, third-party machines. Please
read the [threat model](#threat-model) and the [safe-use checklist](#safe-use-checklist) before using it, and report problems
privately.

This project is alpha software and has **not** had an independent security audit.

## Supported versions

| Version | Supported |
|---|---|
| latest `0.1.x` release | yes |
| anything older, or an unreleased branch | no |

Fixes ship in a new patch release of the latest minor version; there are no backports.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting:

**<https://github.com/trexchery/clouddeck/security/advisories/new>**

Please do **not** open a public issue, pull request or discussion for a vulnerability, and do not paste real secrets (API keys, deploy
keys, passphrases, tokens) into a report: describe where they would appear instead.

Helpful to include: the version (`ddc --version`), your OS, what you did, what happened, and what you expected. A proof of concept
that works against the simulated provider (`clouddeck -p demo`) or the test suite is ideal; never test against an account that holds
money you cannot afford to lose.

This is a volunteer project: there is no service-level agreement and no bug bounty. The aim is to acknowledge a report within a week,
to fix confirmed issues in order of severity, and to publish an advisory with credit (if you want it) once a fix is available.

**In scope:** the controller (`ddc`), the TUI, the remote controller API and its client, the lease scripts under `lease/`, the
installers (scheduled task, systemd timer, cron, shims), the packaging, and documentation that would lead users into an unsafe
setup. Examples: a way to make `ddc` close a deployment that is not its own, to exceed a documented spending limit, to leak a secret
off the PC or into a log, to run code on your PC from data a provider returns, or to reach the remote API without a token.

**Out of scope:** a rented provider reading what you put on its machine (that is the design assumption, see below); problems in
Akash Network, the Akash Console, GitHub, Claude Code or other third-party software (report those upstream); an attacker who already
runs code as your user on your PC; spending that stays inside the documented limits; findings that require `unsafe_disable_limits`,
`budget_guard = false` or `--force` and then complain that limits are off; denial of service against the loopback API by a local user
who holds a valid token.

## Threat model

**What we protect:** your Akash balance (via the API key), your source code and workspace, your GitHub repositories (deploy keys), the
checkpoint passphrase, and your Claude login if you choose to push it.

**Who is trusted.** Your PC and your user account on it. GitHub (state repository, deploy keys). The Akash Console API. Python
packages you install.

**Who is not.** Every rented provider and everything running on it. A provider can read the memory, disk, process list, environment and
network traffic of your lease while it runs, can tamper with it, and can return false data (a hostname or port, bids, deployment
status). Other tenants of a provider are out of our control. A network attacker between your PC and a box is partly covered (see
[host keys](#known-limitations)).

**Design consequences:**

* The Akash API key, wallet material and your access SSH key **never leave the PC**. A box receives no way to spend your money or to
  close leases. It can only *ask*: a shutdown request, a checkpoint.
* Everything a box needs is pushed to it explicitly and is **assumed readable by the provider** (next section). Scope what you push:
  one deploy key per repo, a dedicated state repository, a low-balance account.
* Data coming from a provider is validated before use (for `ddc job` leases too): host names and ports (they end up in SSH arguments and remote shell commands),
  bid prices and denominations (a bid above your cap, or not priced in the expected denomination, is never accepted), and the lease
  owner address.
* Config values that reach the order or a shell (image, profile fields, names, paths, regexes) are validated, quoted or escaped; the
  lease environment file escapes `$`, backticks, quotes and backslashes and refuses control characters.
* Only leases this program created can be closed (state record **and** `ddc-` name **and** account owner); see
  [ARCHITECTURE.md](docs/ARCHITECTURE.md#ownership-model). One deliberate exception: when `state.json` is damaged, `ddc stop` and the
  watchdog's safe stop work from a read-only listing of the account and close only `ddc-*` deployments whose owner matches the owner
  recorded before (or the single owner of all of them); other names are never touched.
* A damaged state file or ledger stops *new* leases (fails closed) but never stops you from looking at or closing existing ones.

## Which secrets go where

| Material | Akash Console API | GitHub | Dev box (rented) | Workers (rented) |
|---|---|---|---|---|
| `AKASH_API_KEY` | yes (HTTPS header) | never | **never** | **never** |
| your GitHub login (`gh` / git credentials) | never | yes, used by `ddc install` and checkpoint verification on the PC | never | never |
| your SSH **access** private key | never | never | never (only its public half, in the SDL) | never (public half only) |
| checkpoint passphrase `DDC_BACKUP_PASSPHRASE` | never | never | **yes**, `~/.config/ddc/backup.pass` | never |
| deploy key for the state repo (write access) | never | public half registered | **yes** | never |
| deploy key of the **first** `push = true` repo (write access) | never | public half registered | **yes** (only that one) | never |
| **worker** private key (lets the dev box log in to workers) | never | never | **yes** | never (public half only) |
| your git name and email (`keys/gitconfig`) | never | never | yes | never |
| `CLAUDE_CODE_OAUTH_TOKEN`, `~/.claude/.credentials.json` | never | never | **only if** `push_claude_credentials = true` (default `false`) | never |
| remote API token | never | never | only with `clouddeck serve --tunnel` | never |
| your workspace (repos, files) | never | checkpoints (encrypted) | **yes**, seeded from the PC | copies made by `dist.sh sync` |
| SDL: image, role, **public** keys, price cap | yes | never | yes | yes |

Notes:

* The seeder refuses uncommitted files that look like secrets (private keys, `.env` files, wallet key files, known token formats, any
  value of 12 or more characters from your `secrets.env`). It cannot know about everything: your committed history is copied as it is, and anything you create
  on the box is yours to police.
* Checkpoints also contain `~/.bash_history`, `.npmrc`, Claude Code session history and `.claude.json`, encrypted with the passphrase
  ([CHECKPOINTS.md](docs/CHECKPOINTS.md#what-is-in-a-checkpoint)).
* When the Claude token is pushed it is exported into every shell on the box through `/etc/profile.d`. If you enable it, use a login
  you can revoke.
* The lease environment file, passphrase file and keys are written with mode 0600 in a 0700 directory, never put on a command line and
  never logged. Providers can still read them.
* **`ddc job` secrets.** Variable names listed in a job spec's `env.secrets` are read from your environment and pushed over ssh into a mode-600
  file on that job's box; they are never in the SDL and never logged. **A rented provider host can read them while the lease runs** (and a URL
  you expand from one shows in the box's process list). Give a job only a credential that is scoped to it and revocable. A spec that lists a
  name starting `AKASH_` or `DDC_` is rejected (the Akash API key and the controller's own variables never leave your machine), preflight
  prints the names it will push (never values), and you must not list wallet material or your access key either: the runner cannot know
  every sensitive name you might type. A job box receives no deploy keys, passphrase or checkpoints. **A job spec is code**: `prep.cmd` runs on
  your PC with your environment and `step.run` on the box next to the secrets, so read a spec you did not write before you run it.

## The remote controller API

`clouddeck serve --tunnel` lets a terminal **on the dev box** (for example Akash Console's shell) drive the fleet without holding any
credential. It is off unless you start it.

* **Loopback only.** The server binds `127.0.0.1` (there is no option to change it) and re-checks the peer address and the `Host`
  header on every request (non-loopback peers get 403, rebinding hosts 421). Nothing listens on the internet.
* **The tunnel is the exposure.** The PC opens an SSH reverse tunnel (`-R 127.0.0.1:PORT:127.0.0.1:PORT`) to the dev box, so **every
  process on the box can connect to the API**. The box is untrusted, so a token only unlocks the minimum its scope grants. Other local
  users on your PC can also reach the loopback port; a token is required in both cases.
* **Tokens.** `cdk_` followed by 32 random bytes; only the SHA-256 is stored (`clouddeck-tokens.json`, mode 0600 on POSIX); compared in
  constant time; they expire (default 24 h; for `--tunnel` the dev box's runtime limit plus one hour; `--expires-hours 0` = never);
  `clouddeck serve --revoke LABEL` revokes; `--list-tokens` lists. An unknown scope never authenticates, a damaged token store refuses
  everyone. While `serve --tunnel` runs, every new dev box gets a fresh token and the previous one stops working.
* **Scopes.**

  | Scope | May |
  |---|---|
  | `read` | `GET /v1/status` (instances, balance, burn rate) |
  | `fleet` | also `POST /v1/scale` (resize the **worker** fleet: at most `min(remote_max_workers, max_workers)` workers, a known preset, within the budget guard; growing is refused outright when `budget_guard = false`; shrinking closes workers), `POST /v1/dispatch` (run a command on the workers), `POST /v1/checkpoint` |
  | `admin` | also run commands on the dev box |

  No scope can deploy a dev box, terminate the dev box, read secrets, or change the config.
* **Limits.** 16 KiB request bodies (larger ones get 413 and the connection is dropped), 30 s socket timeout, rate limits per token
  (120 reads and 12 writes per minute; 10 failed logins per minute get 429), one mutating call at a time (409 otherwise).
* **No secret echo.** Error replies pass through a redactor that removes every value in `secrets.env` and the folder names of your PC
  (state, secrets, config, home); unexpected exceptions return a generic 500 (detail only in the PC-side audit log). `status` lists only
  the instances this tool created, never other deployments on your account. Every request is appended to `remote-audit.log`
  (sanitised against log forging, never contains tokens). On Windows the port is bound exclusively, so another local program cannot sit
  in front of the server.

## Local file permissions

| What | Linux / macOS / WSL | Windows |
|---|---|---|
| `secrets.env`, `claude-credentials.json`, `clouddeck-tokens.json` | created `0600` | per-user profile ACLs (not changed by CloudDeck) |
| secrets directory and `keys/` | `0700` | per-user profile ACLs |
| SSH private keys (`ssh-keygen`) | `0600` | per-user profile ACLs |
| state directory (`state.json`, ledger, logs, checkpoint copies) | default umask | per-user profile ACLs |

The state directory holds no secrets, but its logs contain paths, lease host names and ports. Checkpoint copies there are encrypted.
Nothing is encrypted at rest on your PC beyond that: anyone who can read your user profile can read `secrets.env`. Full-disk encryption is
your responsibility.

## Software supply chain

* **On the box:** the SDL boot script runs `apt-get install` as root from the image's package mirrors, and `image` defaults to a mutable
  tag (`node:22-bookworm`). Pin it with `@sha256:<digest>` to control what runs; `ddc status` warns while it is not pinned. Claude Code is optional (`install_claude_code`), installed from the public npm registry at an exact pinned version with the
  auto-updater disabled; there is no download-and-pipe-to-shell installer.
* **On the PC:** three runtime dependencies (`textual`, `rich`, `tomlkit`) plus their dependencies. CI runs `pip-audit`, `gitleaks`,
  dependency review and a release-hygiene scan on every change; GitHub Actions are pinned by commit SHA and Dependabot keeps them and
  the Python dependencies current. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Known limitations

These are known, documented and not (yet) fixed:

* **SSH host keys are trusted on first use** (`StrictHostKeyChecking=accept-new`; `ddc job` uses the same options with one known-hosts file per
  lease, so a provider that reuses host:port for a new container does not trip over an old key), and secrets are pushed to the box right after the first connection. A network attacker who can impersonate the lease's endpoint during that first connection could receive them. Host and
  port come from the provider's own lease status.
* **Deploy keys are long-lived and shared.** The state-repo key and a repo's key are reused across leases rather than created per lease and
  revoked afterwards. If a box is compromised, rotate them (below). Only the first `push = true` repo's key is shipped, because the box
  has a single `github.com` identity.
* **Checkpoint verification is not a content digest.** It proves part hashes, decryptability, entry counts and repo HEADs, not that every
  byte matches the box.
* **No rescue copy.** `ddc stop --force --discard-workspace` and the watchdog's emergency stops (budget exhausted, box unreachable for
  30 minutes) close a box even if the final checkpoint did not verify. (`--force` alone is refused for a dev box.)
* **The platform runtime limit is Akash's, not ours.** It is the only protection that works with your PC off. It was observed working
  once for this release (a 1-hour lease closed by Akash after 60 minutes, see [COSTS-AND-SAFETY.md](docs/COSTS-AND-SAFETY.md)); one
  observation is not a guarantee, so test it yourself with a tiny lease.
* **The watchdog needs your PC** to be on, awake and logged in. On macOS no watchdog is installed. A `ddc job` run that is killed leaves its
  lease billing until the watchdog's orphan rule (`job_orphan_minutes`) or the platform runtime limit closes it.
* **One passphrase for all checkpoints**, never rotated automatically.
* **`ddc install` uses your own `gh` login** to create the state repository and to add deploy keys. It lists these actions and asks first.
* Secrets can end up in checkpoints through shell history, `.npmrc` and Claude Code history (see above).
* Windows file protection relies on your profile ACLs.

## Safe-use checklist

1. **Use a dedicated Akash account with a small balance** and an API key you can revoke; fund it for the worst case you accept
   ([COSTS-AND-SAFETY.md](docs/COSTS-AND-SAFETY.md)), not more.
2. **Leave `budget_guard = true`** and keep `unsafe_disable_limits` out of your config.
3. **Scope deploy keys to one repository each** (that is how `ddc install` creates them) and give the state repository nothing else.
4. **Keep the state repository private** and back up `DDC_BACKUP_PASSPHRASE` in a password manager.
5. **Do not push Claude credentials** unless you accept that the provider can read them; if you do, use a revocable login. The same goes
   for anything you list in a job spec's `env.secrets`.
6. **Pin the image** by digest once you have a setup you trust.
7. **Treat the dev box as untrusted**: no personal credentials in the shell, no other repositories' keys, no production secrets in
   `/work`.
8. **Rotate after use if anything looked wrong:** delete the deploy keys in the repository settings, delete `keys/state_deploy*` and
   `keys/deploy_*` and run `ddc install` again, and replace `AKASH_API_KEY` in the Console.
9. **Check that the watchdog is registered** (`ddc start` warns if it is not), and that billing really stopped after `ddc stop`
   (`ddc status` says so).
10. **Try the simulated provider first** (`clouddeck -p demo`) and the first real lease with a 1 to 2 hour runtime limit.
11. Keep CloudDeck and its dependencies updated.

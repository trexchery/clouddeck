# Lease-side scripts

Everything in this directory is uploaded to `/opt/ddc` on every lease (dev box and workers) by the controller.
The scripts run on the lease, never on your PC. Requirements: a Debian/Ubuntu-style image with user `dev`,
bash, git, gpg, tar, python3, flock, tmux, node/npm (pigz and GNU parallel are used when present).

| file | role |
| --- | --- |
| `setup.sh` | per-lease setup (root): tmux config, env, ssh config, optional pinned Claude Code, starts `agent.sh` |
| `agent.sh` | activity tracker: periodic checkpoints, idle / max-runtime shutdown *requests* (it closes nothing) |
| `checkpoint.sh`, `restore.sh` | encrypted checkpoint to / from the private state repo |
| `ddc-lib.sh` | helpers sourced by the three scripts above (paths, state-repo identity checks, busy-process scan) |
| `bench.sh` | `ddc bench`: cpu score plus an optional `DDC_BENCH_CMD` of your own |
| `dist.sh`, `workers.sh` | fan jobs out to worker leases, live worker view (tmux `workers` window) |
| `tmux.conf` | tmux keys that survive Windows Terminal -> ssh -> tmux |

## What the controller puts on a lease

Dev box, under `~dev/.config/ddc/` (dir 700, files 600, never written to logs, never in a command line):
`env` (DDC_* settings and, if configured, `CLAUDE_CODE_OAUTH_TOKEN`), `backup.pass` (checkpoint passphrase),
`state_deploy_key` (write access to the state repo), `repo_deploy_key` / `desk_deploy_key` (write access to the
repos you marked push), `worker_key` (reaches the workers), `github_known_hosts`, `gitconfig`, and
`claude-credentials.json` if present. Workers receive the scripts and public keys only: no private key, passphrase or token. **Treat every lease provider as able to read all of the above while the
lease runs**; revoke the deploy keys if a lease is ever left running unattended.

Checkpoints contain `/work/*`, Claude Code session history, `~/.bash_history`, `.gitconfig`, `.npmrc`, never
`~/.config/ddc`, `~/.ssh` or `.claude/.credentials.json`. They are gpg AES256 encrypted with `backup.pass`.

## Lease contract (stable, existing deployments depend on it)

`/opt/ddc` (scripts), `/work` (+ `results`, `.ddc/complete/<name>` seeded/restored markers, `.ddc-restored`),
`~/.config/ddc/*`, `/run/ddc/{state.json,checkpoint_failed,shutdown_requested,last_checkpoint,status-line,worker_activity}`,
`/var/tmp/ddc-state` (clone of the state repo), tmux session `main`, branches `main` / `prev`,
archives `ws-<repo>`, `results`, `claude`, `config` as `archives/<name>.tar.gz.gpg.partNNN` + `manifest.json`.

## Environment

| variable | effect |
| --- | --- |
| `DDC_STATE_REPO` / `DDC_STATE_REMOTE` | state repo `owner/name` (via the `github-state` ssh host) / full git URL override |
| `DDC_IDLE_MINUTES`, `DDC_CHECKPOINT_MINUTES`, `DDC_MAX_RUNTIME_HOURS` | agent timings; the last must equal the lease's `runtimeLimitHours` |
| `DDC_BUSY_REGEX` | extra ERE appended to the default busy-process pattern (package managers, compilers, test runners, git network ops, rsync, ...) |
| `DDC_BENCH_CMD`, `DDC_BENCH_DIR` | `bench.sh cmd`: command to time, and the directory to run it in |
| `DDC_SYNC_EXCLUDES` | extra names `dist.sh sync` skips (`node_modules` always) |
| `DDC_INSTALL_CLAUDE_CODE=1`, `DDC_CLAUDE_CODE_VERSION` | `setup.sh` installs `@anthropic-ai/claude-code` at that exact version via npm (opt-in) |
| `DDC_ADOPT_REMOTE=1` | checkpoint overwrites a state repo main that another lease wrote |
| `DDC_RESTORE_ALLOW_HEAD_MISMATCH=1` | restore accepts a repo whose HEAD differs from the manifest |
| `DDC_CHECKPOINT_IGNORE_UNREADABLE=1` | checkpoint skips unreadable files instead of failing |

## Safety behaviour

* Restore extracts into `/work/.ddc/stage`, verifies sha256, entry count and git HEAD, then renames into place.
  `/work/.ddc-restored` appears only after everything succeeded; `checkpoint.sh` refuses to run without it.
* Checkpoint refuses a workspace that is missing or lost over half its entries (`--allow-shrink` overrides),
  refuses a state repo that is not ours, and publishes main + prev in one atomic, lease-checked push. If the
  upload fails nothing remote changes and the new checkpoint stays in `/var/tmp/ddc-state` (branch `ck-new`).
* Idle detection ignores the controller's own `ssh ... bash -c` probes, and treats any detection error as
  "busy": it can delay an idle shutdown, never cause one. `runtimeLimitHours` remains the hard stop.

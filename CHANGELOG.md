# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/); while the version is `0.x`, minor releases may change behaviour and say so here.

## [0.1.2] - 2026-10-20

### Added
* `ddc job workflows` lists the ready-made workflows and `ddc job workflows --copy DIR` copies them (specs, scripts, README, an empty `inputs/`) out of the pip install into a directory you own, so no clone of the repository is needed. It needs no config. The workflows ship inside the wheel under `clouddeck/data/workflows/`; your own media and `my-*.toml` specs never do.
* Ready-made `ddc job` workflows in `workflows/` for AI video, each with GPU selection, a pinned ComfyUI install, sha256-verified model downloads pinned to immutable Hugging Face commits, inference and video retrieval: Wan 2.2 text-to-video and image-to-video (`wan22-ti2v-5b`, `wan22-t2v-a14b`, `wan22-i2v-a14b`) and, kept apart as character-swap workflows that need a reference image and a driving video, Wan 2.2 Animate (`wan22-animate`) and SCAIL-2 (`scail2`, `scail2-fp8`). Long clips are chained in one prompt (Animate windows of 77 frames, SCAIL-2 segments of 81), prompts can be a `prompt.txt` input, and an optional prebuilt image (`workflows/comfyui/Dockerfile`) removes the install step. See `workflows/README.md` for what was verified on a real GPU and what was not.
* `workflows/templates/custom-comfyui.toml` and `comfy_run.py custom` run your own ComfyUI graph (exported with "Save (API Format)") as a `ddc job`; `workflows/README.md` and the `clouddeck` skill walk through making and keeping one.

### Fixed
* The workflows' README, specs and template now give every path relative to their own directory (they said `workflows/...`, which only worked from a repository root), so the commands work in a copy made by `ddc job workflows --copy`.
* `ddc init`, and the `ssh_key` preflight failure, point to `ddc setup` (jobs) as well as `ddc install` (dev boxes with checkpoints); `ddc job check` no longer tells a jobs-only user to run the GitHub-dependent installer. `ddc setup --help` describes the Linux/WSL watchdog.
* `tests/test_workflows.py` ran `bash -n` with the WSL launcher on Windows (`System32ash.exe`) and failed on machines without a WSL distro. It now uses Git Bash there; Linux and Windows CI both still check the install script's syntax.
* `inputs` / `inputs_dest` in `examples/gpu-batch.toml` sat inside the `[prep]` table, where `ddc job` silently ignores them; they are top-level keys and now come before the first table. A new test fails for any shipped spec with `inputs` inside a table.

### Verification
* Windows 11 native: full suite 1138 passed, 64 skipped (Linux-only). A clean-venv install from the built wheel was exercised offline: entry points, first run without a config, `ddc setup --json`, live public `ddc job hardware`, demo mode including a ConPTY TUI session, `ddc job workflows` and `--copy`, `ddc job check --offline` on every copied spec, spend authorization (no `--approve-usd` and too-low `--approve-usd` both refuse and create nothing), and `npx skills add`. Lease closure, watchdog reaping and budget stops are covered by the fake-Akash tests.
* Not verified: nothing was run on a rented Akash box for this release; the new Wan 2.2 / SCAIL-2 specs were run on a local GPU container, not at full size (80 GB bf16 weights, 720p, long chained clips) on Akash. Real watchdog registration (schtasks, systemd timer, cron) was not exercised by the clean-install pass because it would change the host scheduler; it is covered by tests and was exercised in 0.1.0 on Windows.

## [0.1.1] - 2026-10-19

Small update to the first alpha release.

### Added
* A Claude Code skill in `skills/clouddeck/SKILL.md` for GPU/CPU selection, cost estimates, approved workload execution, result retrieval and confirmed lease closure. Install the skill separately with `npx skills add trexchery/clouddeck -g -a claude-code`.
* Noninteractive, machine-readable JSON responses for agent-controlled GPU/CPU jobs, hardware discovery and setup. A jobs-only setup command (`ddc setup --api-key-stdin --watchdog`) avoids requiring a GitHub state repository for batch workloads.

### Changed
* Every paid `ddc job run`, `ddc start` and scale-up operation requires an explicit, operation-specific `--approve-usd` limit; preflight can show the worst-case quote without purchasing a lease.
* Hardened spend authorization, state checking and error handling so agents do not bypass cost guards or silently reattempt paid operations.
* Shorter README and corrected main-branch installation, pricing approval and security instructions.

### Verification
* Expanded local tests for cost approval, setup, JSON output and failure handling. Release workflow checks strict history hygiene, package metadata, wheel/sdist contents and SHA256 sums before publishing.
* TUI tests wait for a dialog to finish mounting before querying its widgets, removing a race on slow Windows runners.

## [0.1.0] - 2026-10-18

First public release (alpha).

### Added

* **`ddc`, the controller** (`ddc init`, `install`, `uninstall`, `start`, `attach`, `scale`, `status`, `bench`, `checkpoint`, `verify`,
  `stop`, `heal`, `reconcile`, `watchdog`, `ssh`, `run`): provisions a dev box and disposable workers on Akash through the Console API
  (create, bids, lease, close), with a price cap and `runtimeLimitHours` in every order.
* **`clouddeck`, a terminal UI** with five tabs (Compute, Workers, Projects, Monitor, Recovery), a command palette, a provider catalogue
  and GPU market view, live utilisation and spend, typed confirmations for destructive actions, a summary-line warning when the Akash API is
  unreachable (the last known data stays on screen), and a simulated provider (`-p demo`).
  Also `clouddeck status`, `doctor`, `providers`, `install`, `serve` and `remote`.
* **Hardware picker** (`h` on the Compute tab): choose any CPU architecture or GPU vendor, model, memory variant and interface in any count,
  a region, audited providers only or pinned providers, from lists built from Akash's public data at run time (nothing is hard-coded, offline
  falls back to the last saved list), with advertised, screened and confirmed availability kept apart. Needs no new limit: price cap and
  runtime limit still go into every order, and `max_price_usd_hr` (default 2.0, at most 100) is the explicit ceiling for one price cap.
* **Encrypted checkpoints** (gpg AES256, split into 45 MiB parts) in a private GitHub repository you own, taken periodically and before
  every stop, verified on the PC (part hashes, decryption, entry counts, git HEADs), restored on the next start.
* **Spending protection:** budget and reserve guard (on by default: $25 / $5), lease and worker caps, hard ceilings that hold with the guard off
  (8 leases, 7 workers, $2.00/h price cap per profile by default, 24 h, 8 per call), `unsafe_disable_limits` as the only override, worst-case accounting that counts
  creations whose outcome is unknown, and a lifetime ledger.
* **Watchdog** registered by `ddc install` (Windows scheduled task, Linux and WSL systemd user timer or cron): idle shutdown of the dev box
  and workers, runtime and budget enforcement, cleanup of leases that never became ready, alerts (desktop notification, `alert_command`, log).
* **Ownership safety:** only leases recorded in `state.json` and named `ddc-*` (and owned by your account) can be closed; other
  deployments are read-only.
* **Failure handling:** atomic state and ledger writes with a backup, damaged state or ledger blocks new leases but never blocks status or stop,
  create requests are sent once and reconciled when the outcome is unknown, `ddc reconcile` repairs and adopts.
* **Remote controller API** for a terminal on the dev box: loopback only, SSH reverse tunnel, `read` / `fleet` / `admin` scopes, expiring
  hashed tokens, rate and size limits, audit log.
* **Lease scripts** for the box (checkpoint, restore, activity agent, benchmark, worker fan-out) that fail safe: restore verifies before it
  renames, checkpoints refuse truncated workspaces, foreign repositories and second writers, and pushes are atomic and lease-checked.
* **Configuration** with validated, safe defaults (`config.example.toml`), custom hardware profiles, projects, named environments, reusable
  workloads, an optional pinned Claude Code install (`install_claude_code`) and opt-in credential push (`push_claude_credentials`, off by default).
* **Provider plugin contract** (`clouddeck.providers` entry points) with a mandatory spend-guard seam.
* **`ddc job`** (also `python -m clouddeck.job`): a generic runner for batch workloads on one short-lived lease (role `job`) with minimal paid
  idle time: preflight, bid early / lease late, parallel local preparation, resumable detached steps, sha256-verified transfers, verified
  close, per-phase telemetry. Docs: [docs/JOBS.md](docs/JOBS.md), a prebuilt-image guide, examples under `examples/` (in the repository and the
  sdist, not in the wheel), a toolchain `docker/Dockerfile`, and an old-vs-new simulation (assumed, synthetic inputs) in the test suite.
  Safety rules: artifact retrieval is capped (`max_artifact_gb`) and bound by the job's own cost and wall-clock limits, ssh host keys are kept
  per lease, `env.secrets` refuses `AKASH_*` / `DDC_*`, downloads need a sha256 (or an explicit `allow_unverified`) and an http(s) URL, the GPU
  vendor must be named, one run per job name, and a signal during the close never aborts it.
* **One creation path for every lease.** `check_budget` is now a thin wrapper around `check_spend(label, n, hours, cap_usd_hr, role)`, and
  `launch_lease` (write-ahead record, one create request, promotion) is used by both `create_box` and `ddc job`: job leases are subject to
  the lease caps, the hard ceilings (including the per-lease price cap), the budget guard and the unknown-outcome handling like any other lease.
  Behaviour and messages of the existing commands are unchanged.
* **Orphaned job protection:** the runner records a heartbeat; `ddc watchdog` closes an owned job lease whose runner has been silent for
  `job_orphan_minutes` (new optional config key, default 30) or that is past its own deadline, and alerts. The budget stop closes job leases
  too. `ddc status`, `ddc stop`, `ddc reconcile` and the TUI handle role `job` (not resizable from the TUI). A plain `ddc stop` (and the TUI's
  stop-everything) also closes owned job leases, as the user's kill switch; `--only` / `--workers-only` and the watchdog's idle stop keep their
  narrower meaning. `job_orphan_minutes` has a floor of 10.
* `ssh_args` takes an optional `known_hosts` path (default unchanged); `close_box` no longer books a lease that another process already
  recorded as closed.
* Documentation: quickstart, architecture, costs and safety, checkpoints, platforms, security policy, contributing guide.
* CI (tests on Linux for Python 3.11 and 3.13 and on Windows for 3.12, lint, shellcheck, wheel smoke test on Linux and Windows, secret
  scanning, dependency audit), a tag-triggered release workflow (PyPI Trusted Publishing, GitHub release with SHA256SUMS) and a
  release-hygiene checker (`scripts/release_check.py`).
* Preflight loads the ssh access key the way ssh does before any lease is created (a key other accounts can read, for example after
  copying `secrets.env` and `keys\` to a new Windows PC, is refused locally with the `icacls` / `chmod` fix instead of a paid lease that
  never answers ssh); on Windows `ddc install` makes the keys it generates owner-only. `clouddeck doctor` counts a scheduled watchdog only
  when it runs this config.

### Upgrading from the pre-release `ddc.py`

* The command lock is an OS lock on `ddc.flock`; `ddc.lock` is left to the pre-release tool, and `ddc` waits while a pre-release
  `ddc.lock` (holding `<pid> <time>`) younger than 3 h exists. Disable the pre-release watchdog task before the first `ddc` command: the
  pre-release tool does not see `ddc.flock`. `ddc install --migrate` replaces the old watchdog task and also rewrites a
  `%USERPROFILE%\bin\clouddeck.cmd` that points elsewhere (`--dry-run` lists the shims).
* Scheduled watchdogs (Windows task, systemd, cron) are registered with `--config`, and a watchdog run that cannot load its config or finds
  conflicting state dirs raises an alert instead of failing silently.
* Every checkpoint first re-sends the lease env, so a changed `state_repo` (for example after the state repository moved to another owner) takes effect without
  restarting the box; restored push repos get `origin` set to their configured `github`; the checkpoint verification clone never prompts
  and fails closed after 600 s.

### Known limitations

See [SECURITY.md](SECURITY.md#known-limitations) and [docs/COSTS-AND-SAFETY.md](docs/COSTS-AND-SAFETY.md#what-is-not-bounded). In short:
the platform runtime limit was observed working once (not a guarantee), the watchdog needs your PC, SSH host keys are trusted on first use, deploy keys are
not per-lease, checkpoint verification is not a content digest, macOS is unsupported (no watchdog installer), and a native Linux host has not been used against real
leases (WSL2 has). What was checked against real leases: README, "Verified and not verified".

[Unreleased]: https://github.com/trexchery/clouddeck/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/trexchery/clouddeck/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/trexchery/clouddeck/releases/tag/v0.1.0

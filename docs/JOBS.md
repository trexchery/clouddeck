# Jobs: batch work on one short-lived lease with minimal paid idle time (`ddc job`)

`ddc job` runs a batch workload (CPU or GPU) on one Akash lease and keeps the time you pay for as close as possible to the time the
workload runs. It assumes **no persistent storage**: every lease starts empty, so everything the job needs is uploaded, downloaded or baked
into the image, and every result is pulled back and checksum-verified before the lease is closed.

Label used in these docs: **ESTIMATED** (modelled or assumed). Nothing in this document was measured by renting hardware while writing it,
and the code path has been exercised only against a stubbed Akash API and a fake box.

> [!WARNING]
> A job lease is a real lease: it bills until it is closed, and a rented provider host can read everything on it, including the secrets you
> list in the spec, while it runs. Read [Costs and safety](COSTS-AND-SAFETY.md#jobs-ddc-job) first. Start with the CPU smoke example.

## Why

In a typical batch session the largest waste is not a slow boot but **paying while nothing is ready**: a box that idles while local preparation
finishes, gaps between runs that someone queues by hand, and a lease that stays open after the last file was delivered. `ddc job` overlaps
preparation with bidding and boot, starts steps the moment their inputs exist, and closes the lease itself.

## Quick start

```bash
ddc job check   examples/cpu-smoke.toml            # preflight, read-only network checks (includes the controller's own spending gate)
ddc job check   examples/cpu-smoke.toml --offline  # no network at all
ddc job run     examples/cpu-smoke.toml            # create -> bids -> lease -> steps -> artifacts -> verified close
ddc job run     spec.toml --dry-run                # preflight + budget math, creates nothing
ddc job hardware --gpu SOME-MODEL                  # public GPU price/availability (read-only)
ddc job report [NAME]                              # last telemetry summary
ddc job close NAME                                 # verified close of a job's lease (after --keep or a killed run)
python -m clouddeck.job ...                        # the same CLI
python tests/job_sim.py                            # old-vs-new simulation: run it from a checkout of the repository (fake clock, no network, throw-away state dir)
```

The `examples/` directory and `tests/job_sim.py` live in the repository (and the sdist), **not** in the pip wheel: with a pip install, copy
`examples/cpu-smoke.toml` from the repository first. The simulation needs a checkout and the dev extra (`pip install -e ".[dev]"`; it uses
pytest's monkeypatch); it adds the checkout to `sys.path` itself, so no `PYTHONPATH` is needed.

`workflows/` holds complete, ready-to-run specs (not placeholders) for AI video: Wan 2.2 text/image-to-video, and the two character-swap pipelines Wan 2.2 Animate and
SCAIL-2, each with GPU selection, a pinned ComfyUI install, sha256-verified model downloads, inference and video retrieval; see [workflows/README.md](../workflows/README.md).
Like `examples/`, they are in the repository and the sdist, not in the wheel.

Prerequisites are the same as for the rest of `ddc`: `AKASH_API_KEY` in `secrets.env` and the SSH key pair in the secrets dir (`ddc install`
creates both), plus a config file. Job leases are named `ddc-job-<name>-<MMDDHHMM>-<hex>`, are recorded in the controller's state and ledger,
and show up in `ddc status`, `ddc stop --dry-run` and the CloudDeck TUI (role `job`).

## How a job relates to the rest of ddc

A job lease is an ordinary ddc lease with role `job`; it is not a second system.

* **One creation path.** The deployment is created by `ddc.launch_lease` (the same function `create_box` uses): `check_spend` (valid config,
  no damaged state or ledger, lease caps including unresolved creations, hard ceilings, budget and reserve guard), a write-ahead `pending`
  record, **one** `POST /v1/deployments` (an unknown outcome is looked up by name, never repeated), promotion to a state record with
  `ours = true`. `runtimeLimitHours` is always sent. The job package contains no other deployment-creating or deleting API call
  (a test enforces that).
* **One closing path.** Closing is only `ddc.close_box` (ownership assertions, confirmed `closed` by the platform, absent from spend-rate,
  cost booked once).
* **Counts like any lease.** A job lease counts toward `max_total_leases` and the hard lease ceiling (not toward `max_workers`), toward the
  budget at its price cap while it is alive or pending, and the budget guard is on by default.
* **The spec's own caps are clamped to the config.** `hardware.max_usd_hr` is lowered to `max_price_usd_hr` (default $2/h, up to $100/h),
  `runtime_hours` to 24 h, and `max_cost_usd` to `budget_usd` while the guard is on. A line says so; `unsafe_disable_limits = true` lifts the
  first two, as everywhere else. Preflight prints the clamp as a warning.
* **Orphan protection.** The runner records a heartbeat on its lease record about once a minute. `ddc watchdog` closes (through
  `close_box`) an owned job lease whose heartbeat has been silent for `job_orphan_minutes` (default 30) or that is past its own deadline,
  so a killed runner (power loss, `kill -9`) no longer bills until the platform runtime limit. See [Costs and safety](COSTS-AND-SAFETY.md#jobs-ddc-job).
  `--keep` leaves the lease after success but nothing heartbeats it any more, so the watchdog still closes it after `job_orphan_minutes`.
  `job_orphan_minutes` cannot be set below 10 (config check): the runner also beats while it waits on ssh or the provider, but one blocking
  transfer call can still be silent for a while. Raise it, never lower it, if your artifacts are very large.
* **`ddc stop`.** A plain `ddc stop` (and the TUI's stop-everything) closes owned job leases too: it is the kill switch and must stop all
  billing. `ddc stop --workers-only`, `ddc stop --only DSEQ` and the watchdog's idle stop keep their narrower meaning; there job leases are
  left to the orphan rule, the budget stop and `ddc job close NAME`.
* **One run per job name.** `jobs/NAME.lock` is an OS lock held for the whole run: a second `ddc job run` of the same name refuses to start
  (it would otherwise resume the first run's live lease and both would drive one box). A crash releases the lock, so a resume still works.
* **ssh.** The options come from the controller's `ssh_args` (accept-new host keys, i.e. trust on first use: a *changed* key is refused; key
  only, BatchMode). One known-hosts file per **lease** (`jobs/NAME-<dseq>.known_hosts`, removed when the lease is closed): a provider that reuses
  host:port for a new container hands out a new host key, which a per-job file would refuse for ever. A host key that changes within one lease
  fails the attempt at once (no waiting out the boot timeout). The provider-reported host and port are checked with `valid_host` /
  `valid_port` before any use; an unusable endpoint aborts the attempt, closes the lease and marks the provider bad for a day.

## What the runner does, by goal

| # | Goal | Where | How |
|---|---|---|---|
| 1 | Preflight | `clouddeck/job/preflight.py` | API key present; SSH key; SDL valid for every image; price cap vs live GPU prices; online providers with matching hardware, uptime and free GPUs; disk/memory/VRAM sanity and the config's hardware limits; inputs present locally; cache checksums; secrets present locally and never in the SDL; **budget math** (`runtimeLimitHours x price cap` must be <= `max_cost_usd`); and the controller's own `check_spend` (balance, lease caps, ceilings, budget/reserve) so preflight predicts what the launch enforces. Network checks are read-only. `fail` stops before any money moves. |
| 2 | Prebuilt image, affinity | `docker/Dockerfile`, `jobremote/boot.sh`, `market.py` | The start command installs only packages `dpkg -s` reports missing, so a prebuilt image skips apt and a stock Debian/Ubuntu image still works. If the prebuilt image does not give an SSH endpoint within `prebuilt_timeout_s`, the lease is closed and the job is redeployed on the stock image. Provider history (`provider_history.json`) stores the measured lease-to-sshd time per (provider, image tag). Bids are ranked by **expected cost = $/h x (estimated job seconds + expected boot seconds)**, with `cold_boot_s` for providers that have not run the image; failed (provider, image) pairs are blocked for 24 h; a bid above your price cap is never leased. See [prebuilt-image.md](prebuilt-image.md). |
| 3 | Optional external cache | `jobremote/fetch.sh`, `[cache]` | Per item: ordered sources (mirrors first, origin last), sha256 check, `aria2c -x16 -s16 -c` (or `curl -C -` when aria2 is absent). A dead mirror, a slow mirror or a mirror serving different bytes falls through to the next source. Without `[cache].mirrors` it is a checksummed, resumable origin download. Items download in parallel the moment sshd is up. |
| 4 | Parallel preparation | `runner.py` | Bidding starts at t0 together with the local `prep` command. **Bid early, lease late**: the lease is issued when prep is done, or when `prep.estimate_s` minus the expected boot minus `setup_estimate_s` is reached, so boot and downloads finish as prep does. Inputs are uploaded as soon as they exist. Downloads and steps are launched the instant sshd is up; a step starts the moment its own `needs` are done. |
| 5 | Resumable jobs | `jobremote/step.sh`, `remote.py` | Steps are detached on the box with marker files (`.started`, `.done`, `.failed`, `.log`): re-running a done step is a no-op, a live step is never duplicated, a failed one is retried, a failed need fails its dependants. Transfers append to `.part` at a known offset and are `sha256`-verified before the rename, so a dropped connection resumes. A crashed runner leaves an open lease; the next `run` of the same spec **resumes it** (if the record points at an active job lease of ours) or closes it (through `close_box`, which refuses anything that is not ours). Every ssh call is short and stateless. |
| 6 | Startup telemetry | `telemetry.py` | Marks: `create, bid, lease, manifest, container_ready, ssh_ready, setup_done, first_job_start, job_done, artifact_retrieved, closed` (plus `prep_start/done`, `container_start`, `inputs_ready`, `image_fallback`) as JSON under the state dir (`telemetry/`), and a printed summary with billed vs productive time, % and $. The box reports its own container start and apt time. |
| 7 | Retrieval and termination | `runner.py` | Artifact globs per step are pulled as soon as the step is done, chunked and resumable, sha256-verified against the box (a path the box reports must stay below `artifacts_dir`), then the lease is closed with `ddc.close_box`. Retrieval is bounded: the box reports the sizes, so `max_artifact_gb` (default 20) caps each file and the whole job, the free local disk is checked first, and the job's own cost and wall-clock limits are applied inside every transfer. The close runs in `finally`, in a SIGINT/SIGTERM/SIGHUP/SIGBREAK handler (first Ctrl-C: salvage finished artifacts for up to `salvage_s`, then close; second Ctrl-C: close immediately; SIGHUP is POSIX only: the terminal or ssh session went away) and in an `atexit` safety net that stays registered until the close is verified. A signal that arrives while the close is running is deferred until the lease is closed. If the runner is interrupted between the create request and its reply, it looks the deployment up by name (the controller's reconcile) and closes it. `--keep` only applies to success. |
| 8 | Any hardware | `[hardware]` | `cpu`, `memory`, `disk`; optional `[hardware.gpu]` with `units`, `vendor` (required: it is never assumed) and a model list with optional VRAM (`["MODEL-ID:80Gi", "OTHER-ID"]`); CPU-only by omitting the table. GPU ids are shaped by the same rules as the controller's profiles (`clouddeck/sdlgen.py`), so no vendor or model is built in. `max_usd_hr` is required and becomes the SDL price cap. Prices are never assumed: only your caps count. |

## Job spec reference

TOML, see `examples/` (placeholders only). Paths in the spec are relative to the spec file and must stay below it; remote paths are relative
to `/work`.

| Key | Meaning |
|---|---|
| `name` | Lowercase letters, digits, dashes. Names the lease (`ddc-job-<name>-...`) and the state record. |
| `runtime_hours`, `max_cost_usd` | Platform backstop (rounded up to whole hours for `runtimeLimitHours`, at most 24) and the job-level cost cap (also clamped to `budget_usd`). The runner closes the lease itself just under `runtime_hours`. Default `max_cost_usd` is 10. |
| `estimated_runtime_min`, `cold_boot_s`, `setup_estimate_s` | Inputs to bid ranking and lease timing. |
| `bid_wait_s` (20), `poll_s` (10), `idle_abort_s` (900), `max_prep_wait_s` (3600), `salvage_s` (120), `chunk_mb` (64) | Behaviour knobs. Idle abort closes the lease if nothing runs or transfers for that long. |
| `max_artifact_gb` (20) | Most the runner will pull back, per file and for the whole job. The sizes are reported by the box; a larger report fails the job (the lease is closed) before any byte is pulled. |
| `[hardware]`, `[hardware.gpu]` | See above. `min_uptime` filters providers. `gpu.min_vram_gi` is a preflight check. |
| `[image]` | `prebuilt`, `stock`, `prebuilt_timeout_s`, `stock_timeout_s`, `apt` (extra packages installed only if missing). Plain image references only. |
| `[env]` | Plain environment for the container (visible to the provider in the SDL). `secrets = ["NAME"]` lists variables read from your environment and sent over SSH to `/work/.ddc/secrets.env` (mode 600; preflight prints the names it will push, never values); secret-looking names in `[env]`, names starting `DDC_` in `[env]`, and `AKASH_*` / `DDC_*` in `secrets` (the controller's own credentials) are rejected. |
| `[prep]` | `cmd` (runs locally), `estimate_s`, `produces` (inputs that appear after it). |
| `inputs`, `inputs_dest` | Local files (below the spec's directory) uploaded to `/work/<inputs_dest>/<name>`, resumable and verified. |
| `[cache]`, `[[cache.item]]` | `mirrors`, and per item `name, dest, url\|urls, sha256, size_gb, path, allow_unverified`. Sources must be plain `http://` or `https://` URLs. `sha256` is required (64 hex digits); an item may only go without one with an explicit `allow_unverified = true`, which preflight reports as a warning. `${NAME}` in URLs is expanded locally from `[env]` and the declared secrets only (a presigned URL is a secret: it appears in the box's process list). |
| `[[step]]` | `name, run, needs, productive, artifacts`. Default `needs` = `inputs` + all cache items + the previous step. `productive = false` marks setup-like steps in the telemetry. `artifacts` are plain relative globs of files to retrieve. |

There is no `deposit_usd`: escrow deposits are decided by the controller.

**A job spec is code.** `prep.cmd` runs on your PC with your full environment; `step.run` runs on the box next to the secrets you listed.
Read a spec you did not write (its `prep.cmd`, `step.run` and `env.secrets`) before you run it.

## Telemetry output

Real runs print the phases they reached, each as seconds since `create` and since the previous phase (this is the shape, not a result):

```
== NAME: startup/shutdown telemetry ==
  create              +    0.0s
  bid                 +    X.Xs  (+X.Xs)
  lease               +    X.Xs  (+X.Xs)     <- deliberately late when local prep is still running
  manifest            +    X.Xs  (+0.0s)
  container_ready     +    X.Xs  (+X.Xs)
  ssh_ready           +    X.Xs  (+X.Xs)
  setup_done          +    X.Xs  (+X.Xs)
  first_job_start     +    X.Xs  (+0.0s)     <- starts the moment prep and setup are both done
  job_done            +    X.Xs  (+X.Xs)
  artifact_retrieved  +    X.Xs  (+X.Xs)
  closed              +    X.Xs  (+0.0s)
  billed (lease->closed) N s = $N est at $P/h
  productive (jobs)      N s = N%   non-productive N s = $N
    boot N s | wait before first job N s | gaps between jobs N s | after last job N s
```

Billing starts at `lease`, not at `create`. "Productive" is the union of the intervals of `productive` steps; everything else the lease pays
for is listed under boot, wait before the first job, gaps between jobs and after the last job.

## Old vs new, simulated (ESTIMATED)

`python tests/job_sim.py` (from a checkout) runs the **same runner** twice against a stubbed Akash API, a fake box and a virtual clock:
old = `overlap=False` (lease at once, prep after boot, setup after prep, retrieval at the end, manual close lag), new = the default flow. Every
input is an **assumption** in round, synthetic numbers (bid arrival, lease call, boot, prep, run and gap durations, a price); nothing in it was
measured on a real lease, and its outputs are model results, not predictions for your workload. It prints a table for a multi-run session, a
sensitivity table of the new flow (no boot history, a prep ETA that is too optimistic or too pessimistic, no ETA, a prebuilt image that saves
more or less apt time) and a single-job table; edit the constants at the top of the script to model your own numbers. The simulation runs in a
throw-away state dir and is part of the test suite (`tests/test_job_runner.py`).

What the model shows qualitatively, and the tests assert: for the same work the new flow bills fewer seconds, because the lease is not paid
for while local preparation runs and no human gap precedes the first run; the productive seconds are identical; an optimistic prep ETA costs
paid idle time while a pessimistic one (or none) costs only latency, so set `prep.estimate_s` on the high side; and gaps between runs that
someone queues by hand are not removed by any controller, which is why steps should be queued up front.

## Hold or close between jobs?

Re-leasing costs the boot plus the setup (downloads, installs) plus bidding risk. So the rule is: queue everything as steps up front (no
gaps), keep the box for gaps shorter than a restart, close for longer ones. `idle_abort_s` enforces the upper bound automatically. Measure
your own boot and setup with the telemetry before tuning this.

## Limitations

* **Image pull time is provider-side.** A cold pull of a multi-GB image on a provider that has not run it is not covered by any evidence here.
  `ddc job` records `container_start` (the box's own clock, offset-corrected) so the first real runs measure it; the bid ranking then learns
  per provider.
* The apt share of a boot is not measured; the simulation assumes one and its sensitivity table shows it is worth only seconds when the boot is
  already fast. A prebuilt image matters most on cold providers and when many packages are installed.
* Provider "image cached" is a heuristic from the tool's own history, not an Akash API.
* Mirror speed was not measured; the cache only helps when the mirror is faster than the origin.
* `container_ready` uses provider-reported replica counts when present and otherwise the first sight of the SSH port.
* The runner is single-threaded: uploads and artifact pulls block polling for one chunk (`chunk_mb`) at a time.
* Debian/Ubuntu images only (apt, dpkg). A container restart by the provider loses `/work`; the job fails, salvages what it can and closes.
* The orphan rule needs the watchdog to run (your PC on and awake). A laptop that sleeps past `job_orphan_minutes` has its job lease closed
  when it wakes; without the watchdog only the platform runtime limit protects you.
* A job does not hold the controller lock for its whole run: only creation does (and a per-name lock keeps two runs of one job apart).
  A plain `ddc stop` closes job leases; `--workers-only`, `--only` and the watchdog's idle stop keep their narrower meaning.
* One ssh call can block for a long time (finalizing or listing a multi-GB artifact allows up to 30 minutes). The runner beats before and
  between ssh attempts but not inside a single call, which is why `job_orphan_minutes` has a floor of 10 and a generous default.
* Nothing here was run against the live Akash API; the stubbed API follows the endpoints `ddc` already uses. The first real run of each new
  spec should be the CPU smoke example or a short `canary` job.

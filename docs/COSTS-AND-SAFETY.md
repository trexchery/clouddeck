# Costs and safety

CloudDeck spends real money: every lease it creates bills your Akash account by the block until the lease is closed. This page
explains what bounds that spending, what it does not bound, and how to change the limits. All numbers are the **defaults** shipped
in [`config.example.toml`](../config.example.toml) unless stated otherwise; the code that enforces them is `check_spend` (`check_budget` is its profile wrapper),
`launch_lease`, `can_afford` and the watchdog in `clouddeck/controller.py`.

> Limits reduce the damage a mistake or a bug can do. They are not a guarantee, and nothing here is financial advice. Use a dedicated
> Akash account with a small balance, and read [What is not bounded](#what-is-not-bounded).

## The layers, from independent to convenient

| # | Layer | Where it runs | Needs your PC? | Bounds |
|---|---|---|---|---|
| 1 | **Platform runtime limit** (`runtimeLimitHours` in the create request) | Akash | no | how long any one lease can live (8 h dev box, 4 h workers by default; at most 24 h) |
| 2 | **Price cap in the order** (`max_usd_hr` becomes the SDL price) | Akash | no | the price per hour: no bid above the cap can win |
| 3 | **Hard ceilings** (8 leases, 7 workers, $2.00/h per lease by default, 24 h, 8 per call) | this program, before every create | yes (at create time) | what a config mistake can ask for |
| 4 | **Budget guard** (`budget_usd`, `reserve_usd`, lease and worker caps) | this program, before every create | yes (at create time) | cumulative worst-case spend |
| 5 | **Watchdog** (idle shutdown, budget stop, stale-lease cleanup) | scheduled on your PC | **yes, awake and logged in** | wasted hours |

Layers 1 and 2 are written into the order itself, so they hold even if your PC is off, asleep or dead. The runtime limit is Akash's
behaviour, not this project's: the project passes the value and relies on the platform to enforce it. It was checked once for this
release (2026-10-18): a 1-hour lease that nothing on the PC side could close was closed by Akash 60 minutes after it was leased. One
observation on one provider is not a guarantee, and the platform can change: verify it yourself once with a deliberately tiny lease, as
described in [QUICKSTART.md](QUICKSTART.md#7-first-real-start-with-a-small-profile).

## How money is counted

* **Price cap.** `max_usd_hr` (dollars per hour) is converted to Akash's unit, `uact` per block, at 600 blocks per hour, rounded down
  (a $0.50 cap becomes 833 uact/block, about $0.4998/h). The winning bid is at most the cap. Bids priced in any other denomination
  or above the cap are ignored a second time on the client side.
* **Worst case of one lease** = price cap x runtime limit in hours. This is what the guard uses when it decides whether a *new* lease
  may start. Once a lease is running, its committed amount is recomputed from the price it actually won and the hours it has left.
* **Spent** = the cost of every closed lease in `ledger.jsonl` + what the running leases have used so far (the larger of the escrow
  "transferred" amount and elapsed time x price).
* **`budget_usd` is a lifetime limit, not a monthly or per-session budget.** Spent money stays in the ledger, so after the ledger
  holds $25 of closed leases the default budget is used up and no new lease starts until you raise `budget_usd`. This surprises people:
  raise it deliberately when you want to allow more.
* **Reserve.** A new lease is refused unless `account balance - committed worst case of running leases - worst case of the new lease`
  stays at or above `reserve_usd`. The balance is read live from Akash, so this part is account-wide.
* **Unknowns fail closed.** A damaged `state.json` or ledger refuses new leases even with the guard off; an unreadable account balance
  refuses them while the guard is on. `ddc status`, `ddc stop` and the watchdog keep working.
* A creation whose outcome is unknown (timeout, server error) is recorded before the request, counted at its price cap, never retried
  blindly, and resolved by `ddc reconcile` or the watchdog.

## Limit reference

| Setting (config.toml) | Default | Hard ceiling | What it does |
|---|---|---|---|
| `budget_guard` | `true` | n/a | `false` turns the budget/reserve check off. The ceilings and caps below still apply, and the remote API refuses to scale while it is off |
| `budget_usd` | `25.0` | n/a | lifetime worst-case spend this tool may commit (see above) |
| `reserve_usd` | `5.0` | n/a | the account may never be planned below this balance |
| `max_total_leases` | `4` | 8 | leases alive at once, **dev box, workers, `ddc job` leases and generic instances together** |
| `max_workers` | `4` | 7 | workers alive at once. With the default `max_total_leases = 4` you can have at most 3 workers next to the dev box |
| `max_runtime_hours` | `8` | 24 | platform runtime limit of the dev box |
| `worker_runtime_hours` | `4` | 24 | platform runtime limit of a worker |
| `[profiles.N] max_usd_hr` | 0.50 / 0.90 / 1.70 | `max_price_usd_hr` (2.00) | price cap of a profile (16 / 32 / 64 vCPU) |
| `max_price_usd_hr` | `2.0` | 100 | the ceiling for one price cap. Raise it on purpose for GPU orders, then the absolute bound below scales with it |
| leases per `start` / `scale` call | n/a | 8 | |
| `idle_minutes` | `45` | n/a | dev box idle time before a checkpoint-and-stop |
| `worker_idle_minutes` | `10` | n/a | workers are closed after this long without distributed work |
| `checkpoint_minutes` | `15` | n/a | periodic encrypted checkpoint |
| `job_orphan_minutes` | `30` | at least 10 | a `ddc job` lease whose runner has been silent this long is closed by the watchdog ([Jobs](#jobs-ddc-job)) |
| `remote_max_workers` | `4` | n/a | most workers a remote terminal may ask for (also capped by `max_workers`); see [SECURITY.md](../SECURITY.md#the-remote-controller-api) |
| `unsafe_disable_limits` | off | lifts them | removes the hard ceilings, including `max_price_usd_hr` (not the configured caps, not the budget guard). Prints a warning on every run. Almost certainly leave it out |

`ddc start` and `ddc scale` validate the whole config first (finite positive numbers, real booleans, names, image reference) and refuse to
create anything when it is invalid.

## Worst-case spend per profile (defaults)

Price caps x runtime limit. These are **ceilings, not predictions**: you pay the price of the bid that wins, which is normally below the
cap, and only for the time the lease is actually running.

| Profile | Size | Cap | Dev box, 8 h | Worker, 4 h |
|---|---|---|---|---|
| `16` | 16 vCPU / 32 GiB / 100 GiB | $0.50/h | $4.00 | $2.00 |
| `32` | 32 vCPU / 64 GiB / 100 GiB | $0.90/h | $7.20 | $3.60 |
| `64` | 64 vCPU / 128 GiB / 150 GiB | $1.70/h | $13.60 | $6.80 |

What the default guard lets you start, checked one lease at a time against `budget_usd = 25` and `reserve_usd = 5` (the numbers below
come from the controller's own `can_afford`):

| Plan | Worst case | Result |
|---|---|---|
| dev box `32` | $7.20 | allowed with a balance of about $12.20 or more (worst case + reserve) |
| dev box `64` | $13.60 | refused unless the balance is above about $18.60 |
| dev `32` + 3 workers `32` | $7.20 + 3 x $3.60 = $18.00 | allowed (the 4-lease cap is reached) |
| dev `32` + 3 workers `64` | $7.20 + 3 x $6.80 = $27.60 | the third worker is refused: "would exceed budget" |
| dev `16` with `max_runtime_hours = 2` | $1.00 | the cheapest sensible first test |

**Absolute upper bounds without `unsafe_disable_limits`**, with the guard switched off and every cap raised to its ceiling
(`max_total_leases = 8`, `max_workers = 7`, `max_runtime_hours = 24`, `worker_runtime_hours = 24`, caps at the default $2.00/h):
8 leases x $2.00/h x 24 h = **$384** in one go. Each dollar you add to `max_price_usd_hr` multiplies that by 192 (8 x 24): at
the largest allowed value ($100/h) the bound is $19,200. With `unsafe_disable_limits = true` there is no upper bound.

GPU orders follow the same rules, and the price-cap ceiling applies to the whole order (all GPUs plus CPU). At the default $2.00/h a
multi-GPU order usually does not fit: raise `max_price_usd_hr` deliberately, keep the budget guard on, and look at the worst case in the
deploy dialog before you confirm.

## Jobs (`ddc job`)

[`ddc job`](JOBS.md) runs a batch workload on one short-lived lease (role `job`). It is an ordinary ddc lease as far as money is concerned:

* **Created only through the same gate.** The deployment goes through `launch_lease` (what `create_box` uses), which calls `check_spend`
  and writes the `pending` record before the single create request, exactly as for a dev box. So the lease-count caps (a job lease counts
  toward `max_total_leases` and the hard 8, not toward `max_workers`; unresolved creations count too), the hard ceilings (per-lease price
  cap `max_price_usd_hr`, 24 h runtime limit), the budget and reserve guard (on by default for jobs) and the fail-closed rules for a damaged
  state or ledger all apply. With `budget_guard = false` the hard ceilings still apply; `unsafe_disable_limits = true` lifts them as it does
  for every other lease. An alive or pending job lease is counted in the budget at its price cap until its runtime limit.
* **The spec's own caps are clamped, never raised, by the config.** `hardware.max_usd_hr` is lowered to the price ceiling, `runtime_hours` to
  24 h, `max_cost_usd` to `budget_usd` (guard on); the runner says so. `runtimeLimitHours` is always sent, rounded up to whole hours.
  `ddc job check` / `--dry-run` runs the same `check_spend` read-only, so preflight predicts what the launch will enforce.
* **Closed only through `close_box`**, with the ownership assertions (state record, `ddc-` name, owner). A job record that points at anything
  else is refused. A lease closed twice (runner, watchdog, `ddc stop --only`) is booked once.
* **Orphan protection (`job_orphan_minutes`, default 30, never below 10).** The runner records a heartbeat on its lease record about once a minute (also while it waits on ssh or the provider) and a
  deadline when it leases. If the runner dies (power loss, `kill -9`), `ddc watchdog` closes the lease through `close_box` once the heartbeat
  has been silent for `job_orphan_minutes`, or when the lease is past its own deadline, and raises an alert. It only ever considers leases
  recorded as ours, with role `job` and a `ddc-` name; adopted records are left to you, and nothing else is touched. If another ddc command
  holds the lock, or the close fails, the next pass retries (a failed close is alerted). The budget stop of the watchdog closes job leases too.
  Without the watchdog (PC off or asleep) only the platform runtime limit protects a job lease: a job that must not overrun should have a
  short `runtime_hours`. `--keep` leaves a lease after success, but nothing heartbeats it, so it is closed after `job_orphan_minutes` too.
* **A plain `ddc stop` closes job leases too** (and says so in the log): it is the kill switch, so it must stop all billing. The narrower forms
  keep their meaning: `ddc stop --workers-only` and `ddc stop --only DSEQ` close just what they name, and the watchdog's idle stop of the dev box
  leaves job leases to the orphan rule and the budget stop above. `ddc job close NAME` closes one job lease. `ddc status` lists them.
* **Secrets exposure.** Variable names listed in a job spec's `env.secrets` are read from your environment and pushed over ssh into a
  mode-600 file on the job's box. They are never put in the SDL and never logged, but **the provider's host can read them while the lease
  runs**, like everything else on it. A URL you expand from a secret appears in the box's process list. Use a credential that is scoped to the
  job and revocable. Names starting `AKASH_` or `DDC_` (the controller's own credentials) are refused, and preflight prints the names it
  will push (never a value). A job spec is code: `prep.cmd` runs on your PC with your environment, `step.run` runs on the box with the secrets,
  so read a spec you did not write before you run it. See [SECURITY.md](../SECURITY.md#which-secrets-go-where).
* **ssh** uses the controller's options (trust on first use via `StrictHostKeyChecking=accept-new`: a changed key is refused, key-only,
  BatchMode); the provider-reported host and port are validated before use. Host keys are remembered per lease (a provider that reuses
  host:port for a new container gets a fresh file), and a changed key fails the attempt at once. Job inputs and artifact paths must stay below
  the spec's directory and `/work`.
* **Retrieval is bounded.** The box reports the artifact sizes, so the runner caps them (`max_artifact_gb`, default 20, per file and per job),
  checks the free local disk first, and keeps applying the job's own cost and wall-clock limits while it pulls: a box cannot keep a lease
  billing by feeding a transfer forever.

## The watchdog, and what happens when it does not run

`ddc install` registers a scheduled job that runs `ddc watchdog --once` every 5 minutes ([PLATFORMS.md](PLATFORMS.md#watchdog-schedulers-at-a-glance)).
Each run does one enforcement pass over the leases listed in your `state.json`:

* **Idle dev box.** The agent on the box measures activity (CPU use, Claude Code transcript writes, build/test/package-manager processes,
  recent terminal input) and, after `idle_minutes` without any, takes a final checkpoint and *requests* a shutdown (it can close nothing:
  it has no Akash credentials). The watchdog then runs a normal stop: final checkpoint, verified on your PC, then close. If the
  verification fails the box (and the workers) stay up and you get an alert; if you come back while the final checkpoint runs, the stop
  is called off. If the agent cannot see the box's CPU use it counts the box as busy, never idle.
* **Runtime limit.** The agent requests a stop 20 minutes before the runtime limit; the watchdog stops the box 15 minutes before it. The
  platform closes it at the limit regardless.
* **Idle workers.** Workers with no distributed job for `worker_idle_minutes` (a freshly created worker gets that long as grace) are closed.
* **Budget stop** (guard on). If spent + 15 minutes of the current burn would reach `budget_usd`, it alerts and stops the dev box and
  workers, trying a final checkpoint but closing even if that fails. An alert is also raised once at 80% of the budget.
* **Orphaned job leases.** An owned `job` lease whose runner has been silent for `job_orphan_minutes`, or that is past its own deadline, is closed
  (and alerted); the budget stop closes job leases too ([Jobs](#jobs-ddc-job)).
* **Stale leases.** Leases this tool created that never became ready are closed after 30 minutes (leases you adopted with `ddc reconcile --adopt`
  are never closed automatically).
* **Lost box.** A dev box that cannot be reached (connection failure or timeout) for 30 minutes is closed: a final checkpoint is
  attempted and cannot succeed, so the last periodic checkpoint in the state repo is what you keep. A problem on *your* PC (an
  unreadable key, a changed host key, no `ssh`) is not "unreachable": it raises an alert and nothing is closed.
* **Trouble reports.** Failures, a stuck lock, an agent that stopped updating, and checkpoints that keep failing raise alerts: a
  desktop notification where available, an `alert_command` if you set one, the log (`ddc.log`), `alerts.jsonl`, and a line in
  `ddc status`. After 3 failed watchdog runs in a row it tries a safe, verified stop; if that cannot complete it alerts that the box
  is still running.
* **No checkpoints configured.** With an empty `state_repo` the watchdog only alerts about an idle dev box (hourly) instead of
  destroying its workspace; set `idle_discards_workspace = true` to let it close idle boxes and discard their contents.

**If the watchdog is not running** (PC off, asleep or logged out; scheduler removed; macOS), none of the above happens. What still holds
is layer 1 and 2: the platform closes each lease at its runtime limit and no lease costs more than its cap. Between idle and that limit
you keep paying for an idle box. The on-box agent keeps taking periodic checkpoints and a final one 20 minutes before the runtime
limit without your PC, so work is normally preserved, but nothing verifies those checkpoints until the next time your PC checks.

`ddc install` also tries to create an Akash Console email alert (`ddc low balance`, balance below `reserve_usd + 10`) through the
Console API, independent of the watchdog. In October 2026 the Console API refused that request (`HTTP 405` on `POST /v1/alerts`);
`ddc install` then logs "Akash balance alert not configured" and carries on. Set a balance alert in the Akash Console web UI yourself
when you see that line.

## What is not bounded

* **The price you pay inside the cap.** You pay the winning bid, anything up to the cap, for as long as the lease runs.
* **Generic instances** (role `box`, created from the TUI's Compute tab) have no idle agent, no checkpoint, and are **not** closed by
  the watchdog or by a plain `ddc stop`. They are bounded only by their runtime limit and the guard at creation. Close one with the TUI
  or `ddc stop --only <dseq>`.
* **Job leases without the watchdog.** A killed `ddc job run` leaves its lease billing until the watchdog (needs your PC) or the platform runtime limit closes it.
* **What a job's host can read.** Secrets a job spec lists are readable by the provider while the lease runs ([Jobs](#jobs-ddc-job)).
* **Spend by other tools or other machines on the same account.** Ownership and budget tracking are per `state.json`.
* **Provider behaviour.** A provider can close or restart a lease early. Restarted workers are repaired with `ddc heal`.
* **The accuracy of the numbers.** Spend shown is an estimate until the escrow settles; the ledger records the larger of the estimate
  and the settled amount at close.
* **Akash itself.** Pricing, billing, deposits and the Console API are Akash's. This project is not affiliated with Akash Network.

### Several machines, one account

If two machines (or Windows and WSL) use the same Akash API key with separate state directories, each keeps its own list of leases and
its own ledger. Each thinks it has the whole budget; each can only close the leases it created; the reserve check is the only
account-wide limit. `ddc reconcile` lists `ddc-*` deployments on the account that this machine does not know; `ddc reconcile --adopt`
records them as yours so `ddc stop` can close them. Prefer one controller machine per Akash account.

## Tuning

Edit your `config.toml`. `ddc status` warns about unsafe settings and `clouddeck doctor` shows the effective limits.

| Goal | Change |
|---|---|
| A very cheap first test | `max_runtime_hours = 2`, `budget_usd = 5`, `reserve_usd = 2`, use `ddc start -p 16` (worst case $1.00) |
| Leave nothing running overnight | `idle_minutes = 20`, `max_runtime_hours = 6` |
| A bigger fleet | `max_total_leases = 8`, `max_workers = 7` (the ceilings), raise `budget_usd` to cover the worst case |
| Allow one more budget period | raise `budget_usd` (the ledger keeps counting lifetime spend) |
| Limit what a remote terminal may do | `remote_max_workers = 2`; serve it with `--scope read` |
| Pin the box's software | `image = "node:22-bookworm@sha256:<digest>"` |
| No checkpoints | `state_repo = ""`: `ddc stop` then refuses to close the dev box without `--discard-workspace` |

Things worth thinking twice about: `budget_guard = false`, `unsafe_disable_limits = true`, a raised `max_price_usd_hr`, caps near their
ceilings, `ddc stop --force --discard-workspace` (closes without a verified checkpoint and destroys what is on the box; `--force` alone
is refused), `--discard-workspace`, and `push_claude_credentials = true`.

## Verifying that billing stopped

`ddc stop` closes each lease, then asks the Akash API whether the deployment is still listed in the spend rate and only records it as
closed once it is not (it retries for a short while because the listing can lag). Afterwards:

* `ddc status` prints `No ddc leases running (nothing is billing).`
* Akash Console, Deployments tab: nothing active that you did not expect (other deployments on the account are never touched).
* Your balance stops falling.

An answer from the API that is empty, a 404 or an error is treated as "unknown", never as "closed".

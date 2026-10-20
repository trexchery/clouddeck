# clouddeck

rent gpus and cpu dev boxes on [akash network](https://akash.network). compare prices, run jobs, and manage leases from a tui, cli, or claude code.

**early alpha · windows 11 / linux / wsl2 · python 3.11+**

## features

- **gpu and cpu jobs:** rent a short-lived machine, run a workload, retrieve sha256-verified results, and close the lease.
- **claude code skill:** find available hardware, compare prices, get a worst-case quote, run an approved job, and verify shutdown from a prompt.
- **terminal ui:** browse the gpu market, compare providers, monitor spending, and manage instances and projects from a keyboard-driven dashboard.
- **dev boxes:** spin up an interactive linux environment with ssh, tmux, and optional claude code. reconnect with `claude-cloud`.
- **workers:** add and remove compute for parallel tests and builds.
- **spend controls:** per-command approval for paid cli operations, hourly price caps, budget and reserve limits, runtime limits, and a local watchdog.
- **encrypted checkpoints:** save and verify dev-box work in a private github state repository before closing, then restore it on another lease.
- **demo mode:** try the full terminal interface on simulated machines without paying for a lease.

## install

requires [python 3.11+](https://www.python.org/), [pipx](https://pipx.pypa.io/), git, and openssh for real deployments. windows 11, linux, and wsl2 are supported; macos is experimental.

~~~bash
pipx install clouddeck
clouddeck -p demo
~~~

demo mode needs no akash account or payment.

## claude code

install the cli above, then add the skill:

~~~bash
npx skills add trexchery/clouddeck -g -a claude-code
~~~

example request:

> find the cheapest available nvidia gpu with at least 24 gb vram on akash. show the price and maximum cost before spending anything. run a small pytorch test only after approval, fetch the result, and confirm the lease is closed.

without claude code:

~~~bash
ddc job hardware --available  # live gpu availability and pricing; no setup required
ddc setup --json              # local setup and remaining prerequisites
~~~

paid workloads require a funded [akash console](https://console.akash.network) account and api key. follow the [quickstart](docs/QUICKSTART.md) to configure secrets and the watchdog. `ddc job run`, `ddc start`, and scale-up commands require a separate `--approve-usd` amount for each paid operation; running without it shows the worst-case quote and creates nothing.

## good to know

- **real money:** leases keep billing until closed. spending limits reduce risk but are not guarantees. use `ddc status --json` to verify that clouddeck-owned leases stopped billing.
- **local watchdog:** idle and orphan cleanup requires the host pc to remain awake and the scheduler to run. the platform runtime limit is the separate fallback.
- **untrusted providers:** rented machines can read the code, data, and credentials sent to them. use a dedicated, minimally funded akash account and scoped tokens. never paste api keys into claude chat.
- **dev-box workspaces:** checkpoint-backed sessions require gpg and a separate private github state repository; closing an uncheckpointed box can lose its contents.
- **early alpha:** real leases have been exercised on windows 11 and wsl2. macos is untested and has no watchdog installer. no independent security audit has been performed.

sample workloads in `examples/` and ready-made video workflows in `workflows/` (wan 2.2 text/image-to-video, wan 2.2 animate and scail-2 character swap: gpu selection, comfyui install, pinned model downloads, inference, video retrieval) are shipped in the source distribution and github repository, not the pip wheel. copy a spec before running it with an installed package. see [workflows/README.md](workflows/README.md).

## docs

[quickstart](docs/QUICKSTART.md) · [batch jobs](docs/JOBS.md) · [hardware](docs/HARDWARE.md) · [costs and limits](docs/COSTS-AND-SAFETY.md) · [checkpoints](docs/CHECKPOINTS.md) · [platforms](docs/PLATFORMS.md) · [security](SECURITY.md) · [changelog](CHANGELOG.md).

[mit license](LICENSE). independent project; not affiliated with akash network or anthropic.

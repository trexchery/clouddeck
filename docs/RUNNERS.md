# GitHub Actions runners on the dev box

`ddc runner` puts ephemeral, self-hosted GitHub Actions runners for your **private** repos on a dev box that is **already running**. It
never starts, extends or pays for a lease: when no dev box is up it refuses, and when the box closes the runners go with it. Jobs whose
`runs-on` asks for these labels then wait in the queue (falling back to GitHub-hosted runners is the workflow's job, for example a
repository variable that `runs-on` reads at queue time).

```bash
ddc runner up me/private-app me/private-lib --slots 2   # one pass: 2 runners per repo
ddc runner up me/private-app --slots 2 --watch          # keep refilling used slots every 30 s until the box is gone
                                                        # (re-checks `private` each pass; when the box is gone it deletes its idle registrations)
ddc runner down                                          # remove our idle runners (GitHub API) and stop them on the box
ddc runner down me/private-app --force                   # also busy ones: kills their running jobs
```

Labels: `self-hosted, Linux, X64, proj-ephemeral, ddc`; runner names `ddc-<dseq>-<n>`; runner group 1 (Default).

## How it works

* **GitHub side, on your PC.** `ddc` calls the logged-in `gh` CLI (`gh api`), so the GitHub token never leaves `gh`: not in `ddc`'s
  memory, arguments, logs or files. For each free slot it asks for a single-use JIT config
  (`POST /repos/{owner}/{repo}/actions/runners/generate-jitconfig`). Public repos are refused: a self-hosted runner on a public repo would
  run code from anyone's pull request.
* **Box side.** The encoded JIT config is the only thing sent, over the ssh **stdin** of `bash /opt/ddc/runner.sh start <slot>` (root).
  `runner.sh` passes it down a pipe to a `ci` shell that execs `run.sh` with it in `ACTIONS_RUNNER_INPUT_JITCONFIG`, never as an
  argument, so it is in no command line on the PC or the box, in no file `ddc` writes, and in no root process's environment. It does stay
  in the environment of the runner's own long-lived `ci` processes (`run.sh`, `run-helper.sh`, `Runner.Listener`; the listener removes it
  from its live environment before it starts the job's `Runner.Worker`), and the runner stores the same credentials in its slot directory
  (`/ci/runners/<slot>/.runner`, `.credentials*`, mode 700, user `ci`), deleted when its one job ends. So the job itself, running as
  `ci`, can read them: they are good for this one runner registration only, and it takes no other job.
* **Install.** `runner.sh install` downloads actions/runner **2.338.2** (linux-x64), checks its sha256 against the value pinned in the
  script, and extracts it root-owned into `/opt/actions-runner` (outside `/work`, so never checkpointed). Each slot is a hard-link copy:
  the files stay root-owned, `ci` owns only the directories.
* **Ephemeral.** A JIT runner takes one job and exits; `--watch` (or re-running `ddc runner up`) starts a fresh one in that slot.

## The `ci` user

`setup.sh` creates `ci` on every dev box: home `/ci` (700), its own group, no sudo, no ssh login (no `authorized_keys`), not in the
`dev` group. It cannot read what `dev` holds: `/home/dev` and `~dev/.config/ddc` are 700 (`backup.pass`, `state_deploy_key`,
`repo_deploy_key`, `worker_key`, the lease `env`, Claude login), `/work` is 750 `dev:dev`, the state repo clone `/var/tmp/ddc-state`
is 700 (`restore.sh` and `checkpoint.sh` create it 700 again whenever they recreate it), and dev's ssh control sockets are
in `~dev/.ssh` (700), not `/tmp`. Jobs that need root (`apt-get`, `playwright install --with-deps`, Docker) do not work there; give those jobs a GitHub-hosted
`runs-on`, or bake the packages into the box image.

The rented host can still read everything on the box, CI jobs included: the same "untrusted providers" rule as for the rest of the
lease applies.

## Idle shutdown

A running job keeps the box alive: `agent.sh` counts a `Runner.Worker` process of user `ci` as activity (`active_reason` `ci_job`). An
idle runner waiting for work (`Runner.Listener`) does **not** count, so runners never keep an otherwise idle box billing. If the process
list cannot be read the agent reports `ci_detect_error` and counts that as busy (the usual fail-towards-busy rule; the max-runtime
request and the platform hard stop still apply). A queued-but-not-started job does not count either: the box can close under it, and
the job then waits for a runner or for your fallback.

## Live verification (needs a running dev box, so only with an owner-approved `ddc start`)

1. `ddc status` shows the dev box `ready`; `ddc run 'id ci; stat -c "%a %U" /home/dev /home/dev/.config/ddc /work /ci /var/tmp/ddc-state'`
   prints the `ci` uid and `700 dev`, `700 dev`, `750 dev`, `700 ci`, `700 dev`.
2. `ddc ssh`, then as root (`ssh root@...` or the same key): `runuser -u ci -- cat /home/dev/.config/ddc/backup.pass` and
   `runuser -u ci -- ls /work /var/tmp/ddc-state` must all fail with "Permission denied" (`ddc start` has already run `restore.sh`, which
   recreates the state clone); `runuser -u ci -- sudo -n true` must fail; `grep ControlPath ~dev/.ssh/config` prints `~/.ssh/cm-%C`.
3. `ddc runner up OWNER/PRIVATE-REPO --slots 1`: prints `runner 2.338.2 installed` the first time and `runner started: ...`; the repo's
   Settings > Actions > Runners lists `ddc-<dseq>-1` idle with the five labels. `ddc runner up OWNER/PUBLIC-REPO` must refuse.
4. On the box, as root: `ps -eo user,args | grep -i jit` shows no JIT config;
   `for f in $(grep -l ACTIONS_RUNNER_INPUT_JITCONFIG /proc/[0-9]*/environ 2>/dev/null); do p=${f%/environ}; stat -c %U $p; cat $p/comm; done`
   lists only user `ci` and `run.sh`, `run-helper.sh` or `Runner.Listener` (no root process, no `Runner.Worker` while a job runs);
   `stat -c "%a %U" /ci /ci/runners/*/ /ci/runners/*/.credentials*` shows `700 ci` for the directories and `ci`-owned files, and
   `runuser -u dev -- cat /ci/runners/*/.credentials` fails. `cat /run/ddc/state.json` does not say `ci_job` while idle.
5. Trigger a workflow with `runs-on: [self-hosted, proj-ephemeral]`. While it runs, `state.json` shows `"active_reason":"ci_job"` (when no
   CPU / dev activity comes first); afterwards the runner is gone from GitHub and `/ci/runners/<slot>` is deleted. Run `ddc runner up`
   again (or use `--watch`) and a fresh `ddc-<dseq>-1` appears.
6. `ddc runner down`: the runner disappears from GitHub; `bash /opt/ddc/runner.sh list` (root) prints nothing.
   With `ddc runner up ... --watch` running, `ddc stop` the box: the loop ends and the box's idle `ddc-<dseq>-*` runners are gone from GitHub.
7. Leave the box idle with a runner registered: the agent still requests the idle shutdown after `idle_minutes`.

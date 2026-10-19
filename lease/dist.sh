#!/usr/bin/env bash
# Distribute work from the dev box to worker leases (GNU parallel over ssh; hosts w01..wNN
# come from ~/.ssh/config.d/workers, written by ddc.py). Workers hold no secrets or GitHub keys.
#   dist.sh hosts                         list workers
#   dist.sh exec '<cmd>'                  run on every worker
#   dist.sh sync <dir> ['<setup cmd>']    rsync /work/<dir> to all workers (DESTRUCTIVE on the worker copy:
#                                         rsync --delete), then run setup in it on each. Skips node_modules and
#                                         the names in DDC_SYNC_EXCLUDES (space separated).
#   dist.sh run <name> <jobs-file> [per-host]  run one shell command per line, queued over all workers
#                                         (default 1 job per worker at a time); worker files written to
#                                         /work/out/<name>/ are collected into /work/results/<name>/
# While it runs, the agent sees "dist.sh" in the process list and keeps the workers alive; it also touches
# /run/ddc/worker_activity, which the agent treats as worker activity too.
set -euo pipefail
set +x
W=${DDC_WORK:-/work}
touch "${DDC_RUN:-/run/ddc}/worker_activity" 2>/dev/null || true
# DIST_HOSTS="w01 w07" limits any command to those workers (e.g. re-syncing only a healed worker)
# shellcheck disable=SC2086  # DIST_HOSTS is a space-separated list
hosts() { if [ -n "${DIST_HOSTS:-}" ]; then printf '%s\n' $DIST_HOSTS; else awk '/^Host w[0-9]+/{print $2}' "$HOME/.ssh/config.d/workers" 2>/dev/null; fi; }
cmd=${1:?usage}; shift
case $cmd in
  hosts) hosts ;;
  exec)
    hosts | parallel -j0 --tag "ssh {} $(printf '%q' "$1")" ;;
  sync)
    dir=${1:?dir}; setup=${2:-true}
    ex="--exclude node_modules"; for e in ${DDC_SYNC_EXCLUDES:-}; do ex+=" --exclude $(printf '%q' "$e")"; done
    hosts | parallel -j0 --tag "rsync -a --delete $ex $W/$dir/ {}:/work/$dir/ && ssh {} $(printf '%q' "cd /work/$dir && $setup")" ;;
  run)
    name=${1:?name}; jobs=${2:?jobs file}; per=${3:-1}
    R=$W/results/$name; mkdir -p "$R"
    live=$(mktemp)
    # skip workers that are unreachable right now (provider restarted the container) instead of aborting
    hosts | parallel -j0 "ssh -o ConnectTimeout=10 {} 'mkdir -p /work/out/$name' >/dev/null 2>&1 && echo {}" > "$live"
    login=$(sed "s#^#$per/#" "$live" | paste -sd, -); rm -f "$live"
    [ -n "$login" ] || { echo "no workers" >&2; exit 1; }
    start=$(date +%s)
    parallel --sshlogin "$login" --joblog "$R/joblog.tsv" --results "$R/jobs/{#}" --retries 2 --halt never \
      --workdir /work :::: "$jobs" || true
    hosts | parallel -j0 "rsync -a {}:/work/out/$name/ $R/from-{}/" || true
    touch "${DDC_RUN:-/run/ddc}/worker_activity" 2>/dev/null || true
    echo "dist_run_done name=$name wall_s=$(( $(date +%s) - start )) failed=$(awk 'NR>1 && $7!=0' "$R/joblog.tsv" | wc -l)" | tee "$R/summary.txt" ;;
  *) echo "unknown: $cmd" >&2; exit 2 ;;
esac

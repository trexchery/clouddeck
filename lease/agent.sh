#!/usr/bin/env bash
# Dev-box activity tracker: periodic encrypted checkpoints, idle / max-runtime shutdown requests.
# It never closes anything itself (no Akash credentials on the lease): the PC watchdog reads
# /run/ddc/state.json and closes, and Akash's runtimeLimitHours is the independent hard stop
# (DDC_MAX_RUNTIME_HOURS must equal it: the agent asks for a final checkpoint 20 min before).
#
# Idle detection fails towards "busy": if the process scan itself fails the box counts as active
# (active_reason "procs_detect_error"), so a detection error can delay an idle shutdown but never causes one.
# The cost of that direction is bounded by the max-runtime request and the platform hard stop.
# Busy processes: DDC_BUSY_DEFAULT in ddc-lib.sh plus an optional DDC_BUSY_REGEX (ERE) appended to it, among dev's processes;
# plus a running GitHub Actions job of user ci (ddc_ci_job; an idle runner waiting for work does not count).
# Test seams: DDC_AGENT_INTERVAL (seconds between samples, default 60), DDC_AGENT_ONCE=1 (one sample, then exit),
# DDC_CGROUP_CPU_STAT (file read for our CPU use, default /sys/fs/cgroup/cpu.stat; unreadable = active_reason "cpu_detect_error", busy),
# DDC_PTS_GLOB (terminals checked for recent input, default '/dev/pts/[0-9]*'; tests point it at nothing so a real terminal cannot count).
set -uo pipefail
set +x
D=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HOME/.config/ddc/env"
# shellcheck source=ddc-lib.sh
. "$D/ddc-lib.sh"
S=$DDC_RUN W=$DDC_WORK
IDLE_MIN=${DDC_IDLE_MINUTES:-45}
CKPT_MIN=${DDC_CHECKPOINT_MINUTES:-15}
MAX_MIN=$(( ${DDC_MAX_RUNTIME_HOURS:-8} * 60 - 20 ))   # final checkpoint well before the platform close
[ -f "$S/started" ] || date +%s > "$S/started"
start=$(cat "$S/started")
last_active=$(date +%s); last_dist=$last_active; last_ckpt=$last_active

log() { echo "$(date -u +%FT%TZ) $*"; }

write_state() {
  printf '{"now":%s,"started":%s,"uptime_min":%s,"idle_min":%s,"active_reason":"%s","dist_idle_min":%s,"last_checkpoint":"%s","checkpoint_failed":%s,"shutdown_requested":"%s"}\n' \
    "$now" "$start" "$up" "$idle" "$why" "${dist_idle:-0}" "${ck:-}" "$([ -f "$S/checkpoint_failed" ] && echo true || echo false)" "$(cat "$S/shutdown_requested" 2>/dev/null)" > "$S/state.json.tmp"
  mv "$S/state.json.tmp" "$S/state.json"
}

# A checkpoint can take minutes; keep "now" moving meanwhile, or the PC watchdog (stale after 3 min) reports a busy agent as dead.
ckpt() {
  ( while sleep 30; do now=$(date +%s); write_state; done ) & local hb=$!
  "$D/checkpoint.sh" "$@"; local rc=$?
  kill "$hb" 2>/dev/null; wait "$hb" 2>/dev/null
  return $rc
}

busy_re=$DDC_BUSY_DEFAULT
if [ -n "${DDC_BUSY_REGEX:-}" ]; then
  [[ x =~ $DDC_BUSY_REGEX ]]; rc=$?   # 2 = not a valid ERE
  if [ $rc -eq 2 ]; then log "ignoring invalid DDC_BUSY_REGEX"; else busy_re+="|$DDC_BUSY_REGEX"; fi
fi
dist_re='(^|[/ ])dist\.sh( |$)'

while :; do
  now=$(date +%s); why=""
  # /proc/loadavg is the whole (shared) host's; our own CPU use comes from the cgroup
  usage=$(awk '/^usage_usec/{print $2}' "${DDC_CGROUP_CPU_STAT:-/sys/fs/cgroup/cpu.stat}" 2>/dev/null)
  if [ -z "$usage" ]; then usage=0 cpu_known=0; else cpu_known=1; fi   # cgroup v1 / restricted container: our CPU use cannot be seen
  cores=$(awk -v u="$usage" -v p="${prev_usage:-$usage}" -v dt=$(( now - ${prev_t:-now} + 1 )) 'BEGIN{printf "%.2f", (u-p)/1e6/dt}')
  prev_usage=$usage; prev_t=$now
  awk -v c="$cores" 'BEGIN{exit !(c>=0.5)}' && why="cpu=${cores}cores"
  [ -z "$why" ] && [ "$cpu_known" = 0 ] && why=cpu_detect_error   # blind to CPU use: busy, never idle (the header says this fails towards busy)
  if [ -z "$why" ] && find "$HOME/.claude/projects" -type f -newermt '-2 minutes' -print -quit 2>/dev/null | grep -q .; then why=claude; fi
  if [ -z "$why" ]; then
    ddc_busy_procs "$busy_re"
    case $? in 0) why=procs ;; 2) why=procs_detect_error ;; esac   # starts with "procs": the watchdog keeps workers too
  fi
  if [ -z "$why" ]; then
    ddc_ci_job
    case $? in 0) why=ci_job ;; 2) why=ci_detect_error ;; esac   # `ddc runner` jobs run as user ci, which the scan above does not see
  fi
  if [ -z "$why" ]; then
    # shellcheck disable=SC2231  # the glob in DDC_PTS_GLOB must expand
    for t in ${DDC_PTS_GLOB:-/dev/pts/[0-9]*}; do
      a=$(stat -c %X "$t" 2>/dev/null || echo 0)
      [ $((now - a)) -lt 120 ] && { why='tty'; break; }
    done
  fi
  [ -n "$why" ] && last_active=$now
  # Worker (job) activity: dist.sh running (or undetectable), or anyone touching $S/worker_activity
  # (dist.sh does; a controller that dispatches jobs to workers over ssh can too).
  ddc_busy_procs "$dist_re"; [ $? -ne 1 ] && last_dist=$now
  wa=$(stat -c %Y "$S/worker_activity" 2>/dev/null || echo 0)
  [ "$wa" -gt "$last_dist" ] && last_dist=$wa

  idle=$(( (now - last_active) / 60 )); up=$(( (now - start) / 60 ))
  if ! ddc_ckpt_disabled && [ $(( (now - last_ckpt) / 60 )) -ge "$CKPT_MIN" ]; then
    if out=$(ckpt periodic 2>&1); then last_ckpt=$now; log "$out" | tail -1; else log "checkpoint FAILED: $(echo "$out" | tail -3)"; echo "$now" > "$S/checkpoint_failed"; fi
  fi

  req=""
  [ "$idle" -ge "$IDLE_MIN" ] && req=idle
  [ "$up" -ge "$MAX_MIN" ] && req=max_runtime
  if [ -n "$req" ] && [ ! -f "$S/shutdown_requested" ]; then
    log "shutdown requested ($req): final checkpoint"
    if ckpt "final-$req" >>"$W/results/agent.log" 2>&1; then echo "$req" > "$S/shutdown_requested"; last_ckpt=$now; fi
  elif [ -z "$req" ] && [ -n "$why" ] && [ -f "$S/shutdown_requested" ] && [ "$(cat "$S/shutdown_requested")" = idle ]; then
    rm -f "$S/shutdown_requested"; log "activity resumed ($why): idle shutdown request withdrawn"
  fi

  dist_idle=$(( (now - last_dist) / 60 ))
  ck=$(cut -d' ' -f1 "$S/last_checkpoint" 2>/dev/null)
  write_state
  nw=$(grep -c '^Host w' "$HOME/.ssh/config.d/workers" 2>/dev/null); nw=${nw:-0}
  printf 'idle %sm/%s | up %sh%02dm | ckpt %s | workers %s%s' "$idle" "$IDLE_MIN" $((up/60)) $((up%60)) "${ck:11:5}" "$nw" \
    "$([ -f "$S/shutdown_requested" ] && echo ' | STOPPING')" > "$S/status-line"
  [ "${DDC_AGENT_ONCE:-}" = 1 ] && break
  sleep "${DDC_AGENT_INTERVAL:-60}"
done

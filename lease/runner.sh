#!/usr/bin/env bash
# `ddc runner`: ephemeral GitHub Actions runners on the dev box, run as root by the controller, jobs run as the unprivileged user ci
# (created by setup.sh: own home /ci, no sudo, no read access to /home/dev, /work or the state clone).
#   install          pinned runner, sha256-verified, into /opt/actions-runner (root-owned, outside /work so never checkpointed)
#   start <slot>     one JIT runner; the encoded JIT config comes on stdin (never argv, never a file written here)
#   list             slots whose runner is still alive
#   stop [--force]   stop runners that are not running a job (--force: all)
# Test seams: DDC_RUNNER_ROOT, DDC_CI_HOME, DDC_RUNNER_STATE, DDC_CI_USER.
set -euo pipefail
set +x   # stdin carries a credential
VER=2.338.2
SHA=baebf204307bac6193480852732f0aba2ae97b6c5597c244f4d5558390147c40   # actions-runner-linux-x64-2.338.2.tar.gz, from the release notes
R=${DDC_RUNNER_ROOT:-/opt/actions-runner} H=${DDC_CI_HOME:-/ci} ST=${DDC_RUNNER_STATE:-/var/lib/ddc-runner} U=${DDC_CI_USER:-ci}
die() { echo "refusing: $*" >&2; exit 1; }
alive() { local p; p=$(cat "$ST/$1.pid" 2>/dev/null) && kill -0 "$p" 2>/dev/null; }
slot_ok() { [[ ${1:-} =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die "bad slot name"; }

case ${1:-} in
install)
  id "$U" >/dev/null 2>&1 || die "no $U user (setup.sh creates it)"
  [ -f "$R/.ddc-$VER" ] && { echo "runner $VER ok"; exit 0; }
  tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
  curl -fsSL --retry 3 -o "$tmp/r.tgz" "https://github.com/actions/runner/releases/download/v$VER/actions-runner-linux-x64-$VER.tar.gz"
  echo "$SHA  $tmp/r.tgz" | sha256sum -c --quiet - || die "runner tarball sha256 mismatch"
  rm -rf "$R.new"; mkdir -p "$R.new"; tar -xzf "$tmp/r.tgz" -C "$R.new"
  chown -R 0:0 "$R.new"; chmod -R go-w "$R.new"
  "$R.new/bin/installdependencies.sh" >"$tmp/deps.log" 2>&1 || { tail -20 "$tmp/deps.log"; die "runner dependencies"; }
  rm -rf "$R"; mv "$R.new" "$R"; touch "$R/.ddc-$VER"
  echo "runner $VER installed" ;;
start)
  slot_ok "${2:-}"; slot=$2
  IFS= read -r jit || true
  [[ $jit =~ ^[A-Za-z0-9+/=]{20,}$ ]] || die "no JIT config on stdin"
  [ -f "$R/.ddc-$VER" ] || die "runner not installed"
  alive "$slot" && die "$slot is already running"
  install -d -m 700 "$ST"; install -d -o "$U" -g "$U" -m 700 "$H"; install -d -o 0 -g "$U" -m 750 "$H/runners"
  d=$H/runners/$slot; rm -rf "$d"
  # Hard links: the runner's files stay root-owned (ci cannot change the shared install); ci owns only the directories,
  # where the runner writes its JIT settings (.runner/.credentials), _diag and _work. The whole dir goes when the job ends.
  cp -al "$R" "$d"; find "$d" -type d -exec chown "$U:$U" {} +
  # The runner reads ACTIONS_RUNNER_INPUT_JITCONFIG from its environment: inherited, so it never appears in a command line.
  export ACTIONS_RUNNER_INPUT_JITCONFIG=$jit
  setsid bash "$0" _run "$slot" >/dev/null 2>&1 </dev/null &
  unset ACTIONS_RUNNER_INPUT_JITCONFIG jit
  echo "started $slot" ;;
_run)   # session leader for one runner: run it as ci, then remove its directory (ephemeral: it exits after one job)
  slot_ok "${2:-}"; slot=$2; d=$H/runners/$slot
  echo $$ > "$ST/$slot.pid"
  trap : TERM   # `stop` signals the whole session: the runner exits, this shell survives to clean up (a handler, not an ignore, so children keep the default)
  (cd "$d" && runuser -u "$U" -- ./run.sh) >>"$ST/$slot.log" 2>&1 </dev/null || true
  rm -rf "$d" "$ST/$slot.pid" ;;
list)
  for f in "$ST"/*.pid; do [ -e "$f" ] || continue; s=$(basename "$f" .pid); if alive "$s"; then echo "$s"; else rm -f "$f"; fi; done ;;
stop)
  for f in "$ST"/*.pid; do
    [ -e "$f" ] || continue; s=$(basename "$f" .pid); alive "$s" || { rm -f "$f"; continue; }
    sid=$(cat "$f")
    if [ "${2:-}" != --force ] && ps -s "$sid" -o comm= | grep -qx Runner.Worker; then echo "busy $s (left to finish)"; continue; fi
    pkill -TERM -s "$sid" || true; echo "stopped $s"
  done ;;
*) die "usage: runner.sh install|start <slot>|list|stop [--force]" ;;
esac

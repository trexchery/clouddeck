# shellcheck shell=bash
# Shared helpers for checkpoint.sh / restore.sh / agent.sh (sourced, never executed).
# Every lease path is overridable so the scripts can run in a sandbox (tests/test_lease_scripts.py);
# on a real lease none of these is set and the defaults below are the lease contract.
export DDC_WORK=${DDC_WORK:-/work} DDC_RUN=${DDC_RUN:-/run/ddc} DDC_STATE_CLONE=${DDC_STATE_CLONE:-/var/tmp/ddc-state}

die() { echo "refusing: $*" >&2; exit 1; }

# gzip level 1; pigz when present (same format, so archives are interchangeable with gzip).
ddc_gz() { if command -v pigz >/dev/null 2>&1; then pigz -1; else gzip -1; fi; }
ddc_gunz() { if command -v pigz >/dev/null 2>&1; then pigz -dc; else gzip -dc; fi; }

# No state repo configured (DDC_STATE_REPO empty; DDC_STATE_REMOTE is the test override) = checkpoints are disabled:
# checkpoint.sh skips (exit 0), restore.sh reports "nothing to restore", and the controller refuses to close the dev box
# without --discard-workspace, because nothing would be saved.
ddc_ckpt_disabled() { [ -z "${DDC_STATE_REPO:-}" ] && [ -z "${DDC_STATE_REMOTE:-}" ]; }

ddc_remote() { printf '%s' "${DDC_STATE_REMOTE:-git@github-state:${DDC_STATE_REPO:?DDC_STATE_REPO not set}.git}"; }

# "<sha> <branch>" per branch of the state repo. Fails (rc 1, message on stderr) on auth / network /
# "Repository not found", so an unreachable repo can never be mistaken for an empty one.
ddc_state_heads() {
  local err out
  err=$(mktemp)
  if ! out=$(git ls-remote --heads "$(ddc_remote)" 2>"$err"); then
    echo "cannot reach the state repo: $(tail -3 "$err")" >&2; rm -f "$err"; return 1
  fi
  rm -f "$err"
  awk 'NF{sub("refs/heads/", "", $2); print $1, $2}' <<<"$out"
}

# $1 = ddc_state_heads output. A ddc state repo holds only main (+ prev): any other branch, or branches
# without main, means a foreign repo (a misconfigured state_repo) and we never write to it.
ddc_state_identity() {
  local other
  other=$(awk 'NF && $2 != "main" && $2 != "prev" {printf "%s ", $2}' <<<"$1")
  [ -z "$other" ] || { echo "refusing: state repo has other branches ($other): not a ddc state repo, check state_repo" >&2; return 1; }
  if [ -n "$1" ] && ! grep -q ' main$' <<<"$1"; then
    echo "refusing: state repo has branches but no main: not a ddc state repo, check state_repo" >&2; return 1
  fi
}

# $1 = file. The manifest schema marker: JSON with created_at and an archives object.
ddc_manifest_valid() {
  python3 - "$1" <<'PY' 2>/dev/null
import json, sys
m = json.load(open(sys.argv[1]))
sys.exit(0 if isinstance(m, dict) and "created_at" in m and isinstance(m.get("archives"), dict) else 1)
PY
}

# Idle detection. Default busy patterns (ERE, matched against a process command line): package managers,
# compilers / test runners, git network operations, file transfer, the dist/bench lease scripts.
export DDC_BUSY_DEFAULT='(^|[/ ])(npm|npx|pnpm|yarn|bun|pip3?|poetry|uv|cargo|rustc|go (build|test)|apt(-get)?|dpkg|tsc|tsx|esbuild|vite|vitest|jest|mocha|pytest|gcc|g\+\+|clang|make|cmake|ninja|gradle|mvn|javac|dotnet (build|test)|git (clone|fetch|pull|push|gc|rebase|merge)|rsync|scp|parallel|dist\.sh|bench\.sh)( |$)'

# 0 = a busy process exists, 1 = none, 2 = could not look (callers must treat that as busy).
# $1 = ERE. Wrapper processes are skipped, because their command line merely CONTAINS the work they
# launched (the real child matches by itself): `bash -c '<script>'` (CloudDeck's probe runs this way every
# 20 s), ssh / sshd, sudo, tmux, watch, timeout, and the tools used to look.
ddc_busy_procs() {
  local out pid comm args
  out=$(ps -u "$(id -un)" -o pid=,comm=,args= 2>/dev/null) || return 2
  [ -n "$out" ] || return 2
  while read -r pid comm args; do
    [ "$pid" = "$$" ] && continue
    case $comm in
      bash|sh|dash|zsh) [[ $args =~ ^[^\ ]+\ -[a-zA-Z]*c ]] && continue ;;
      ssh|sshd|sudo|su|tmux*|watch|timeout|pgrep|ps|grep|awk|sed|cat|sleep) continue ;;
    esac
    [[ $args =~ $1 ]] && return 0
  done <<<"$out"
  return 1
}

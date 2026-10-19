#!/usr/bin/env bash
# VERBATIM copy of the first-release lease/checkpoint.sh (only the git committer name differs): tests/test_lease_scripts.py runs it
# to prove old checkpoints restore with the current scripts and new ones with this one. Do not edit.
# Encrypted checkpoint -> private GitHub state repo (main = latest, prev = the one before).
# Separate archives: one per /work repo/dir, results, Claude Code state, shell/git config.
# Secrets (~/.config/ddc, ~/.ssh, ~/.claude/.credentials.json) are never archived.
# Usage: checkpoint.sh [reason] [--allow-shrink]
set -euo pipefail
. "$HOME/.config/ddc/env"
reason=${1:-manual}
S=/run/ddc
exec 9>$S/checkpoint.lock; flock -w 600 9 || { echo "checkpoint lock timeout" >&2; exit 1; }
[ -f /work/.ddc-restored ] || { echo "refusing: workspace restore not finished on this lease" >&2; exit 1; }
SR=/var/tmp/ddc-state
PASS=$HOME/.config/ddc/backup.pass
export GIT_SSH_COMMAND="ssh -o ControlMaster=no"

if [ "$reason" = periodic ] && [ -f $S/last_checkpoint ] && \
   ! find /work "$HOME/.claude" "$HOME/.claude.json" "$HOME/.bash_history" \( -name node_modules -o -name .stryker-tmp -o -name sandbox \) -prune -o \
        -newer $S/last_checkpoint -print -quit 2>/dev/null | grep -q .; then
  echo "checkpoint_skip unchanged"; exit 0
fi

tmp=$(mktemp -d /var/tmp/ddc-ck.XXXXXX); trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/out/archives"
EXC=(--exclude=node_modules --exclude=.stryker-tmp --exclude=.vite --exclude=.cache --exclude='*.sock'
     --exclude=.claude/.credentials.json --exclude=.claude/shell-snapshots --exclude=.claude/statsig --exclude=.claude/downloads)
declare -A ENTRIES

pack() { # name, base dir, members...
  local name=$1 base=$2; shift 2
  tar -C "$base" "${EXC[@]}" --warning=no-file-changed --ignore-failed-read -cf - "$@" | pigz -1 > "$tmp/$name.tar.gz" || [ $? -eq 1 ]
  ENTRIES[$name]=$(pigz -dc "$tmp/$name.tar.gz" | tar -tf - | wc -l)
  gpg --batch --yes --quiet --pinentry-mode loopback --passphrase-file "$PASS" --symmetric --cipher-algo AES256 \
      --compress-algo none -o "$tmp/$name.tar.gz.gpg" "$tmp/$name.tar.gz"
  rm -f "$tmp/$name.tar.gz"
  split -b 45m -d -a 3 "$tmp/$name.tar.gz.gpg" "$tmp/out/archives/$name.tar.gz.gpg.part"
  rm -f "$tmp/$name.tar.gz.gpg"
}

names=()
for d in /work/*/; do
  n=$(basename "$d"); [ "$n" = results ] && continue
  find "$d" -name '*.lock' -path '*/.git/*' -delete 2>/dev/null || true
  pack "ws-$n" /work "$n"; names+=("ws-$n")
done
pack results /work results; names+=(results)
cl=(); for f in .claude .claude.json; do [ -e "$HOME/$f" ] && cl+=("$f"); done
[ ${#cl[@]} -gt 0 ] && { pack claude "$HOME" "${cl[@]}"; names+=(claude); }
cf=(); for f in .bash_history .gitconfig .vimrc .npmrc; do [ -e "$HOME/$f" ] && cf+=("$f"); done
[ ${#cf[@]} -gt 0 ] && { pack config "$HOME" "${cf[@]}"; names+=(config); }

# Data-loss guard: never replace a checkpoint with one that silently lost a workspace.
if [ -f "$SR/manifest.json" ] && [ "${2:-}" != --allow-shrink ]; then
  missing=$(python3 - "$SR/manifest.json" "${names[@]}" <<'PY'
import json, sys
old = json.load(open(sys.argv[1]))["archives"]
print(" ".join(sorted(n for n in old if n.startswith("ws-") and n not in sys.argv[2:])))
PY
)
  [ -z "$missing" ] || { echo "refusing: checkpoint would drop $missing (pass --allow-shrink)" >&2; exit 1; }
fi

repos_json=$(for d in /work/*/; do
  [ -d "$d/.git" ] || continue
  n=$(basename "$d")
  printf '"%s":{"head":"%s","branch":"%s","dirty":%s,"stashes":%s}\n' "$n" \
    "$(git -C "$d" rev-parse HEAD 2>/dev/null)" "$(git -C "$d" branch --show-current 2>/dev/null)" \
    "$(git -C "$d" status --porcelain 2>/dev/null | wc -l)" "$(git -C "$d" stash list 2>/dev/null | wc -l)"
done | paste -sd, -)
ts=$(date -u +%FT%TZ)
{
  printf '{"created_at":"%s","reason":"%s","dseq":"%s","host":"%s","repos":{%s},"archives":{' "$ts" "$reason" "${DDC_DSEQ:-}" "$(hostname)" "$repos_json"
  first=1
  for n in "${names[@]}"; do
    parts=$(cd "$tmp/out/archives" && ls "$n.tar.gz.gpg.part"* | while read -r p; do printf '{"file":"%s","sha256":"%s","bytes":%s},' "$p" "$(sha256sum "$p" | cut -c1-64)" "$(stat -c %s "$p")"; done)
    [ $first = 1 ] || printf ','; first=0
    printf '"%s":{"entries":%s,"parts":[%s]}' "$n" "${ENTRIES[$n]}" "${parts%,}"
  done
  printf '}}\n'
} > "$tmp/out/manifest.json"
python3 -m json.tool "$tmp/out/manifest.json" >/dev/null

# Publish: rotate main -> prev, then a fresh orphan commit (keeps the repo small).
[ -d "$SR/.git" ] || git init -q -b main "$SR"
git -C "$SR" remote get-url origin >/dev/null 2>&1 || git -C "$SR" remote add origin "${DDC_STATE_REMOTE:-git@github-state:${DDC_STATE_REPO}.git}"
cd "$SR"
git config user.name "ddc"; git config user.email "ddc@users.noreply.github.com"
git rev-parse -q --verify main >/dev/null && git push -qf origin main:refs/heads/prev
git checkout -q --orphan ck-new
git rm -rq --cached . 2>/dev/null || true
rm -rf archives manifest.json
mv "$tmp/out/archives" "$tmp/out/manifest.json" .
git add -A
git commit -qm "checkpoint $ts $reason"
git branch -qM main
git push -qf origin main
git reflog expire --expire=now --all; git gc -q --prune=now
commit=$(git rev-parse HEAD)
echo "$ts $commit" > $S/last_checkpoint
rm -f $S/checkpoint_failed
echo "checkpoint_ok $ts $commit reason=$reason archives=${#names[@]}"

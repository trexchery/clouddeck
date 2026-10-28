#!/usr/bin/env bash
# Encrypted checkpoint -> private GitHub state repo (main = latest, prev = the one before).
# Separate archives: one per /work repo/dir, results, Claude Code state, shell/git config.
# Secrets (~/.config/ddc, ~/.ssh, ~/.claude/.credentials.json) are never archived.
# Usage: checkpoint.sh [reason] [--allow-shrink]
#
# Fail-safe by construction (a bad checkpoint must never replace a good one):
#  - refuses while a restore is unfinished (/work/.ddc-restored missing or /work/.ddc/restore-started present)
#  - refuses to write to a repo that is not a ddc state repo (only main/prev branches, main holds a manifest.json)
#  - refuses when main moved since this lease last saw it (second writer) unless DDC_ADOPT_REMOTE=1
#  - refuses when a workspace is missing, or a workspace / the Claude state / the shell config lost more than half its entries vs the
#    previous manifest (--allow-shrink)
#  - fails on unreadable files (DDC_CHECKPOINT_IGNORE_UNREADABLE=1 restores the old skip-with-warning behaviour)
#  - publishes with ONE atomic push (main + prev, each with a lease on the sha we saw): never a blind force-push,
#    prev is only replaced together with a new main; a failed upload leaves the local copy in $DDC_STATE_CLONE (ck-new)
# Archive layout, manifest schema and branch names are unchanged: old checkpoints restore with this release's
# restore.sh and new ones with the previous one.
set -euo pipefail
set +x
D=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HOME/.config/ddc/env"
# shellcheck source=ddc-lib.sh
. "$D/ddc-lib.sh"
reason=${1:-manual}
if ddc_ckpt_disabled; then echo "checkpoint_skip disabled (no state repo configured)"; exit 0; fi
S=$DDC_RUN W=$DDC_WORK SR=$DDC_STATE_CLONE
exec 9>"$S/checkpoint.lock"; flock -w 600 9 || { echo "checkpoint lock timeout" >&2; exit 1; }
[ -f "$W/.ddc-restored" ] || die "workspace restore not finished on this lease"
[ ! -e "$W/.ddc/restore-started" ] || die "a restore was started on this lease and never completed (re-run restore.sh)"
PASS=$HOME/.config/ddc/backup.pass
export GIT_SSH_COMMAND="ssh -o ControlMaster=no"
shopt -s nullglob
mkdir -p "$W/results"

if [ "$reason" = periodic ] && [ -f "$S/last_checkpoint" ] && \
   ! find "$W" "$HOME/.claude" "$HOME/.claude.json" "$HOME/.bash_history" \( -name node_modules -o -name .stryker-tmp -o -name sandbox \) -prune -o \
        -newer "$S/last_checkpoint" -print -quit 2>/dev/null | grep -q .; then
  echo "checkpoint_skip unchanged"; exit 0
fi

tmp=$(mktemp -d "${TMPDIR:-/var/tmp}/ddc-ck.XXXXXX"); trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/out/archives"

# Preflight (before the expensive packing): is the state repo reachable, ours, and still where this lease left it?
heads=$(ddc_state_heads) || exit 1
ddc_state_identity "$heads" || exit 1
remote_main=$(awk '$2 == "main" {print $1}' <<<"$heads")
remote_prev=$(awk '$2 == "prev" {print $1}' <<<"$heads")
mkdir -p "$SR"; chmod 700 "$SR"   # same as restore.sh: the state clone stays private to dev
[ -d "$SR/.git" ] || git init -q -b main "$SR"
git -C "$SR" remote add origin "$(ddc_remote)" 2>/dev/null || git -C "$SR" remote set-url origin "$(ddc_remote)"
local_main=$(git -C "$SR" rev-parse -q --verify refs/heads/main || true)
prev_manifest=$tmp/prev-manifest.json
if [ -n "$remote_main" ]; then
  if [ "$local_main" = "$remote_main" ]; then
    git -C "$SR" show main:manifest.json > "$prev_manifest" 2>/dev/null || true
  else
    # not the main this lease restored/pushed last: only ours again if the manifest says this lease (dseq) wrote it
    git -C "$SR" fetch -q --depth 1 origin +refs/heads/main:refs/remotes/origin/main || die "cannot fetch the state repo main"
    git -C "$SR" show refs/remotes/origin/main:manifest.json > "$prev_manifest" 2>/dev/null || true
    ddc_manifest_valid "$prev_manifest" || die "state repo main has no valid manifest.json: not a ddc checkpoint repo"
    if [ "${DDC_ADOPT_REMOTE:-}" != 1 ] && [ "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("dseq",""))' "$prev_manifest")" != "${DDC_DSEQ:-}" ]; then
      die "state repo main ${remote_main:0:10} was not written by this lease (last seen ${local_main:0:10}): another lease checkpointed since the restore. Restore again, or set DDC_ADOPT_REMOTE=1 to overwrite"
    fi
  fi
  ddc_manifest_valid "$prev_manifest" || die "state repo main has no valid manifest.json: not a ddc checkpoint repo"
elif [ -n "$local_main" ]; then
  echo "warning: state repo has no main (deleted?); local copy ${local_main:0:10} stays the reference" >&2
  git -C "$SR" show main:manifest.json > "$prev_manifest" 2>/dev/null || true
fi

EXC=(--exclude=node_modules --exclude=.stryker-tmp --exclude=.vite --exclude=.cache --exclude='*.sock'
     --exclude=.claude/.credentials.json --exclude=.claude/shell-snapshots --exclude=.claude/statsig --exclude=.claude/downloads)
TAR_X=(); [ "${DDC_CHECKPOINT_IGNORE_UNREADABLE:-}" = 1 ] && TAR_X=(--ignore-failed-read)
declare -A ENTRIES

pack() { # name, base dir, members...
  local name=$1 base=$2 rc; shift 2
  # tar exit 1 = a file changed while being read (normal on a live box); anything else (unreadable file) fails loudly
  tar -C "$base" "${EXC[@]}" "${TAR_X[@]}" --warning=no-file-changed -cf - "$@" | ddc_gz > "$tmp/$name.tar.gz" || {
    rc=$?; [ $rc -eq 1 ] || { echo "tar failed ($rc) packing $name (unreadable files? DDC_CHECKPOINT_IGNORE_UNREADABLE=1 skips them)" >&2; exit 1; }; }
  ENTRIES[$name]=$(ddc_gunz < "$tmp/$name.tar.gz" | tar -tf - | wc -l)
  gpg --batch --yes --quiet --pinentry-mode loopback --passphrase-file "$PASS" --symmetric --cipher-algo AES256 \
      --compress-algo none -o "$tmp/$name.tar.gz.gpg" "$tmp/$name.tar.gz"
  rm -f "$tmp/$name.tar.gz"
  split -b 45m -d -a 3 "$tmp/$name.tar.gz.gpg" "$tmp/out/archives/$name.tar.gz.gpg.part"
  rm -f "$tmp/$name.tar.gz.gpg"
}

# HEADs are read BEFORE packing, so an archive can only be newer than its manifest entry (restore accepts that).
repos_json=$(for d in "$W"/*/; do
  [ -d "$d/.git" ] || continue
  n=$(basename "$d")
  printf '"%s":{"head":"%s","branch":"%s","dirty":%s,"stashes":%s}\n' "$n" \
    "$(git -C "$d" rev-parse HEAD 2>/dev/null)" "$(git -C "$d" branch --show-current 2>/dev/null)" \
    "$(git -C "$d" status --porcelain 2>/dev/null | wc -l)" "$(git -C "$d" stash list 2>/dev/null | wc -l)"
done | paste -sd, -)

names=()
for d in "$W"/*/; do
  n=$(basename "$d"); [ "$n" = results ] && continue
  # runs/ is scratch space for throwaway per-run copies (recreated on demand); packing it can push a checkpoint
  # past GitHub's 2 GB push limit
  [ "$n" = runs ] && continue
  find "$d" -name '*.lock' -path '*/.git/*' -delete 2>/dev/null || true
  pack "ws-$n" "$W" "$n"; names+=("ws-$n")
done
pack results "$W" results; names+=(results)
cl=(); for f in .claude .claude.json; do [ -e "$HOME/$f" ] && cl+=("$f"); done
[ ${#cl[@]} -gt 0 ] && { pack claude "$HOME" "${cl[@]}"; names+=(claude); }
cf=(); for f in .bash_history .gitconfig .vimrc .npmrc; do [ -e "$HOME/$f" ] && cf+=("$f"); done
[ ${#cf[@]} -gt 0 ] && { pack config "$HOME" "${cf[@]}"; names+=(config); }

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

# Data-loss guard: never replace a checkpoint with one that lost a workspace or most of its contents.
if [ -s "$prev_manifest" ] && [ "${2:-}" != --allow-shrink ]; then
  lost=$(python3 - "$prev_manifest" "$tmp/out/manifest.json" <<'PY'
import json, sys
old = json.load(open(sys.argv[1]))["archives"]
new = json.load(open(sys.argv[2]))["archives"]
out = []
for n, a in sorted(old.items()):
    if n == "ws-runs" or not (n.startswith("ws-") or n in ("claude", "config")):   # results and runs are scratch space
        continue
    if n not in new:
        if n.startswith("ws-"):   # claude/config are optional (not installed / no dotfiles): only a big shrink counts
            out.append(f"{n} (missing)")
    elif a["entries"] >= 3 and new[n]["entries"] * 2 < a["entries"]:
        out.append(f"{n} ({a['entries']} -> {new[n]['entries']} entries)")
print(", ".join(out))
PY
)
  [ -z "$lost" ] || die "checkpoint would lose $lost (pass --allow-shrink if intended)"
fi

# Publish. The commit is built with plumbing (separate index, no checkout), so the previous checkpoint in
# $SR (main) stays untouched until the push is confirmed.
export GIT_AUTHOR_NAME=ddc GIT_AUTHOR_EMAIL=ddc@users.noreply.github.com GIT_COMMITTER_NAME=ddc GIT_COMMITTER_EMAIL=ddc@users.noreply.github.com
GIT_INDEX_FILE=$tmp/index git -C "$SR" --work-tree="$tmp/out" add -Af
commit=$(GIT_INDEX_FILE=$tmp/index git -C "$SR" commit-tree "$(GIT_INDEX_FILE=$tmp/index git -C "$SR" write-tree)" -m "checkpoint $ts $reason")
git -C "$SR" update-ref refs/heads/ck-new "$commit"   # replaces the ref of an earlier failed upload (now prunable)
gc_state() { git -C "$SR" reflog expire --expire=now --all; git -C "$SR" gc -q --prune=now; }
gc_state
if [ -n "$remote_main" ]; then
  push=(--atomic --force-with-lease="refs/heads/main:$remote_main" --force-with-lease="refs/heads/prev:$remote_prev"
        origin "$commit:refs/heads/main" "$remote_main:refs/heads/prev")
else
  push=(origin "$commit:refs/heads/main")   # first checkpoint: plain push, fails if main appeared meanwhile
fi
git -C "$SR" push -q "${push[@]}" 2>"$tmp/push.err" || {
  echo "upload FAILED, state repo untouched; the new checkpoint is kept in $SR (branch ck-new): $(tail -3 "$tmp/push.err")" >&2; exit 1; }
[ "$(git -C "$SR" ls-remote origin refs/heads/main | cut -f1)" = "$commit" ] || { echo "upload not confirmed: remote main is not $commit; local copy kept in $SR (ck-new)" >&2; exit 1; }
git -C "$SR" update-ref refs/heads/main "$commit"
git -C "$SR" symbolic-ref HEAD refs/heads/main
git -C "$SR" update-ref -d refs/heads/ck-new
rm -f "$SR/.git/shallow"   # every checkpoint is a root commit
gc_state
echo "$ts $commit" > "$S/last_checkpoint"
rm -f "$S/checkpoint_failed"
echo "checkpoint_ok $ts $commit reason=$reason archives=${#names[@]}"

#!/usr/bin/env bash
# Restore the latest encrypted checkpoint onto a fresh lease. Verifies every part's sha256, the tar entry
# count and (for repos) the git HEAD against the manifest. Existing targets are kept (never overwrites work
# already on this lease) unless --force, which moves them aside to /work/.ddc/replaced/ instead of deleting.
# Prints: restored=<names> missing=<names>   (ddc.py seeds "missing" repos from the PC)
#
# Fail-safe: everything is extracted into /work/.ddc/stage and verified there, then moved into place with
# one rename, so an interrupted restore can never leave a half-extracted workspace that looks complete.
# /work/.ddc/restore-started exists while a restore runs; /work/.ddc-restored is written ONLY after every
# archive is restored and verified. checkpoint.sh refuses to run without that marker.
set -euo pipefail
set +x
D=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HOME/.config/ddc/env"
# shellcheck source=ddc-lib.sh
. "$D/ddc-lib.sh"
S=$DDC_RUN W=$DDC_WORK SR=$DDC_STATE_CLONE
PASS=$HOME/.config/ddc/backup.pass
force=${1:-}
if ddc_ckpt_disabled; then   # no state repo: nothing to restore, and the lease is usable (checkpoint.sh is a no-op too)
  mkdir -p "$W/.ddc" "$W/results"; touch "$W/.ddc-restored"; echo "restored= missing=all"; exit 0
fi
export GIT_SSH_COMMAND="ssh -o ControlMaster=no"
mkdir -p "$W/.ddc"
exec 9>"$S/checkpoint.lock"; flock -w 600 9 || { echo "checkpoint lock timeout" >&2; exit 1; }
[ -e "$W/.ddc/restore-started" ] && interrupted=1 || interrupted=0   # an earlier restore on this lease died half way
rm -f "$W/.ddc-restored"; touch "$W/.ddc/restore-started"
stage=$W/.ddc/stage; rm -rf "$stage"; mkdir -p "$stage"   # leftovers of an interrupted restore

finish() { rm -rf "$stage"; rm -f "$W/.ddc/restore-started"; touch "$W/.ddc-restored"; }

# Only a repo with no branches at all counts as "no checkpoint yet"; auth/network errors fail loudly (inside
# ddc_state_heads), otherwise a PC re-seed could later be pushed over cloud-only work.
heads=$(ddc_state_heads) || exit 1
ddc_state_identity "$heads" || exit 1
# keep an unpushed checkpoint of a failed upload (checkpoint.sh keeps it as ck-new) instead of deleting it
if [ -d "$SR/.git" ] && git -C "$SR" rev-parse -q --verify refs/heads/ck-new >/dev/null; then mv "$SR" "$SR.unpushed-$(date +%s)"; else rm -rf "$SR"; fi
mkdir -m 700 "$SR"   # private before git writes into it (the clone holds the encrypted archives and their manifest; ci must not list it)
if [ -z "$heads" ]; then
  git init -q -b main "$SR"; finish; echo "restored= missing=all"; exit 0
fi
err=$(mktemp)
git clone -q --depth 1 --branch main "$(ddc_remote)" "$SR" 2>"$err" || { cat "$err" >&2; rm -f "$err"; exit 1; }
rm -f "$err"
cd "$SR"
ddc_manifest_valid manifest.json || die "state repo main has no valid manifest.json: not a ddc checkpoint repo"

# name entries head, validated: names become paths below
rows=$(python3 - <<'PY'
import json, re, sys
m = json.load(open("manifest.json"))
for k, v in m["archives"].items():
    if not re.fullmatch(r"[A-Za-z0-9._-]+", k) or k in (".", "..") or not isinstance(v["entries"], int):
        sys.exit(f"bad archive entry in manifest: {k!r}")
    for p in v["parts"]:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", p["file"]):
            sys.exit(f"bad part name in manifest: {p['file']!r}")
    print(k, v["entries"], (m.get("repos", {}).get(k[3:], {}).get("head") or "-") if k.startswith("ws-") else "-")
PY
)

dec() { cat archives/"$1".tar.gz.gpg.part* | gpg --batch --quiet --pinentry-mode loopback --passphrase-file "$PASS" -d | ddc_gunz; }

# $1 = ws-<repo>, $2 = HEAD recorded in the manifest. The manifest is written before the archive is packed, so
# the restored HEAD may be newer (descendant), never different.
check_head() {
  local d=$stage/$1/${1#ws-} got
  [[ $2 =~ ^[0-9a-f]{40,64}$ ]] || return 0   # unborn repo / not a repo
  [ -e "$d/.git" ] || { echo "restore verification failed: $1 has no .git but the manifest has a HEAD" >&2; return 1; }
  got=$(git -C "$d" rev-parse HEAD 2>/dev/null) || { echo "restore verification failed: $1 HEAD unreadable" >&2; return 1; }
  [ "$got" = "$2" ] && return 0
  git -C "$d" merge-base --is-ancestor "$2" "$got" 2>/dev/null && return 0
  if [ "${DDC_RESTORE_ALLOW_HEAD_MISMATCH:-}" = 1 ]; then echo "warning: $1 HEAD ${got:0:10} != manifest ${2:0:10} (allowed)" >&2; return 0; fi
  echo "restore verification failed: $1 HEAD ${got:0:10} != manifest ${2:0:10} (DDC_RESTORE_ALLOW_HEAD_MISMATCH=1 accepts it)" >&2; return 1
}

restored=()
while read -r name entries head; do
  case $name in
    ws-*) base=$W; target=$W/${name#ws-} ;;
    results) base=$W; target=$W/results/.restored ;;
    claude) base=$HOME; target=$HOME/.claude/projects ;;
    config) base=$HOME; target=$HOME/.bash_history ;;
    *) continue ;;
  esac
  if [ "$name" = claude ] || [ "$name" = config ]; then
    # These are merged into $HOME, so the target existing proves nothing (an interrupted copy leaves a partial one): only the completion
    # marker written after the copy does. A target without a marker is live data from before markers existed and is kept, unless a
    # restore was interrupted on this lease (then it is re-copied: the same archive over the same files).
    mark=$W/.ddc/archive-complete/$name
    if [ -e "$mark" ] && [ "$force" != --force ]; then continue; fi
    if [ -e "$target" ] && [ "$force" != --force ] && [ "$interrupted" = 0 ]; then continue; fi
  elif [ -e "$target" ] && [ "$force" != --force ]; then continue; fi
  python3 - manifest.json "$name" <<'PY'
import hashlib, json, sys
m = json.load(open(sys.argv[1]))["archives"][sys.argv[2]]
for p in m["parts"]:
    h = hashlib.sha256(open("archives/" + p["file"], "rb").read()).hexdigest()
    if h != p["sha256"]:
        sys.exit(f"sha256 mismatch {p['file']}")
PY
  got=$(dec "$name" | tar -tf - | wc -l)
  [ "$got" = "$entries" ] || { echo "entry count mismatch $name: $got != $entries" >&2; exit 1; }
  mkdir -p "$stage/$name"
  dec "$name" | tar -C "$stage/$name" -xf -
  case $name in
    ws-*)
      [ -e "$stage/$name/${name#ws-}" ] || { echo "restore verification failed: $name has no ${name#ws-} directory" >&2; exit 1; }
      check_head "$name" "$head" || exit 1
      if [ -e "$target" ]; then   # --force: keep what was there
        mkdir -p "$W/.ddc/replaced"; mv "$target" "$W/.ddc/replaced/${name#ws-}.$(date +%s)"
      fi
      # marker first: if we die between marker and rename, a re-run still finds the target missing and restores it
      mkdir -p "$W/.ddc/complete"; touch "$W/.ddc/complete/${name#ws-}"
      mv "$stage/$name/${name#ws-}" "$target" ;;
    *)
      cp -a "$stage/$name/." "$base/"
      [ "$name" = results ] && touch "$target"
      if [ "$name" = claude ] || [ "$name" = config ]; then mkdir -p "$W/.ddc/archive-complete"; touch "$W/.ddc/archive-complete/$name"; fi ;;
  esac
  rm -rf "${stage:?}/$name"
  restored+=("$name")
done <<<"$rows"
find "$W" -maxdepth 4 -path '*/.git/*.lock' -delete 2>/dev/null || true
missing=$(python3 - <<'PY'
import json, os
m = json.load(open("manifest.json"))["archives"]
w = os.environ.get("DDC_WORK", "/work")
want = [r for r in os.environ.get("DDC_REPOS", "").split() if r]
print(",".join(r for r in want if "ws-" + r not in m and not os.path.exists(os.path.join(w, r))))
PY
)
finish
echo "restored=$(IFS=,; echo "${restored[*]:-}") missing=$missing manifest=$(python3 -c 'import json;print(json.load(open("manifest.json"))["created_at"])')"

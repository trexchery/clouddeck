#!/usr/bin/env bash
# VERBATIM copy of the first-release lease/restore.sh (only the git committer name differs): tests/test_lease_scripts.py runs it
# to prove old checkpoints restore with the current scripts and new ones with this one. Do not edit.
# Restore the latest encrypted checkpoint onto a fresh lease. Verifies every part's sha256
# and the tar entry count against the manifest before extracting. Existing targets are kept
# (never overwrites work already on this lease) unless --force.
# Prints: restored=<names> missing=<names>   (ddc.py seeds "missing" repos from the PC)
set -euo pipefail
. "$HOME/.config/ddc/env"
SR=/var/tmp/ddc-state
PASS=$HOME/.config/ddc/backup.pass
force=${1:-}
export GIT_SSH_COMMAND="ssh -o ControlMaster=no"
rm -rf "$SR"
if ! git clone -q --depth 1 --branch main "${DDC_STATE_REMOTE:-git@github-state:${DDC_STATE_REPO}.git}" "$SR" 2>/tmp/ddc-clone.err; then
  # Only a repo with no main branch counts as "no checkpoint yet"; auth/network errors must fail,
  # otherwise a PC re-seed could later be pushed over cloud-only work.
  if grep -q 'Remote branch main not found' /tmp/ddc-clone.err; then
    git init -q -b main "$SR"; touch /work/.ddc-restored; echo "restored= missing=all"; exit 0
  fi
  cat /tmp/ddc-clone.err >&2; exit 1
fi
cd "$SR"
[ -f manifest.json ] || { touch /work/.ddc-restored; echo "restored= missing=all"; exit 0; }
restored=()
while read -r name entries; do
  case $name in
    ws-*) base=/work; target=/work/${name#ws-} ;;
    results) base=/work; target=/work/results/.restored ;;
    claude) base=$HOME; target=$HOME/.claude/projects ;;
    config) base=$HOME; target=$HOME/.bash_history ;;
    *) continue ;;
  esac
  if [ -e "$target" ] && [ "$force" != --force ]; then continue; fi
  python3 - manifest.json "$name" <<'PY'
import hashlib, json, sys
m = json.load(open(sys.argv[1]))["archives"][sys.argv[2]]
for p in m["parts"]:
    h = hashlib.sha256(open("archives/" + p["file"], "rb").read()).hexdigest()
    if h != p["sha256"]:
        sys.exit(f"sha256 mismatch {p['file']}")
PY
  got=$(cat archives/"$name".tar.gz.gpg.part* | gpg --batch --quiet --pinentry-mode loopback --passphrase-file "$PASS" -d | pigz -dc | tar -tf - | wc -l)
  [ "$got" = "$entries" ] || { echo "entry count mismatch $name: $got != $entries" >&2; exit 1; }
  cat archives/"$name".tar.gz.gpg.part* | gpg --batch --quiet --pinentry-mode loopback --passphrase-file "$PASS" -d | pigz -dc | tar -C "$base" -xf -
  [ "$name" = results ] && touch "$target"
  case $name in ws-*) mkdir -p /work/.ddc/complete; touch "/work/.ddc/complete/${name#ws-}" ;; esac
  restored+=("$name")
done < <(python3 -c 'import json; [print(k, v["entries"]) for k, v in json.load(open("manifest.json"))["archives"].items()]')
find /work -maxdepth 4 -path '*/.git/*.lock' -delete 2>/dev/null || true
missing=$(python3 - <<'PY'
import json, os
m = json.load(open("manifest.json"))["archives"]
want = [r for r in os.environ.get("DDC_REPOS", "").split() if r]
print(",".join(r for r in want if "ws-" + r not in m and not os.path.exists("/work/" + r)))
PY
)
touch /work/.ddc-restored
echo "restored=$(IFS=,; echo "${restored[*]}") missing=$missing manifest=$(python3 -c 'import json;print(json.load(open("manifest.json"))["created_at"])')"

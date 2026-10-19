#!/usr/bin/env bash
# Idempotent, resumable step runner. Usage: step.sh NAME NEEDS(comma list or -) COMMAND
# Marker files in $DDC_WORK/.ddc: NAME.started (epoch), NAME.done / NAME.failed (json), NAME.log, NAME.pid.
# Re-running a finished step is a no-op; re-running a live step is a no-op; a dead/failed step is retried.
W=${DDC_WORK:-/work}; D=$W/.ddc; mkdir -p "$D"
name=$1; needs=$2; cmd=$3
[ -e "$D/$name.done" ] && exit 0
if [ -e "$D/$name.pid" ] && kill -0 "$(cat "$D/$name.pid")" 2>/dev/null; then exit 0; fi
echo $$ > "$D/$name.pid"; rm -f "$D/$name.failed" "$D/$name.started"
now() { date +%s.%N; }
for n in ${needs//,/ }; do
  [ "$n" = "-" ] && continue
  while [ ! -e "$D/$n.done" ]; do
    if [ -e "$D/$n.failed" ]; then printf '{"rc":97,"start":%s,"end":%s,"why":"need %s failed"}\n' "$(now)" "$(now)" "$n" > "$D/$name.failed"; exit 97; fi
    sleep 1
  done
done
# shellcheck source=/dev/null
[ -e /root/.ddc_env ] && . /root/.ddc_env
# shellcheck source=/dev/null
[ -e "$D/secrets.env" ] && . "$D/secrets.env"
cd "$W" || exit 1; s=$(now); echo "$s" > "$D/$name.started"
bash -c "$cmd" >> "$D/$name.log" 2>&1; rc=$?
e=$(now)
if [ $rc -eq 0 ]; then printf '{"rc":0,"start":%s,"end":%s}\n' "$s" "$e" > "$D/$name.done"
else printf '{"rc":%s,"start":%s,"end":%s}\n' "$rc" "$s" "$e" > "$D/$name.failed"; fi
rm -f "$D/$name.pid"; exit $rc

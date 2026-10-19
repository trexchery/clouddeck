#!/usr/bin/env bash
# One-screen live view of every worker (run in the tmux "workers" window via `watch`).
# CPUS = our cgroup quota; BUSY = cores our container is actually using (host loadavg is meaningless here);
# CURRENT_JOB = the command using the most CPU right now (blank when nothing is busy).
# shellcheck disable=SC2016  # the probe is a script for the worker's shell, not for this one
probe='. /etc/profile.d/ddc-cpus.sh 2>/dev/null; printf "%s|%s|%s|%s|%s\n" "${DDC_CPUS:-$(nproc)}" \
  "$(ps -eo pcpu= | awk "{s+=\$1} END {printf \"%.1f\", s/100}")" \
  "$(awk "{printf \"%.0fG\", \$1/1073741824}" /sys/fs/cgroup/memory.current 2>/dev/null)" \
  "$(grep -m1 "model name" /proc/cpuinfo | cut -d: -f2 | sed "s/(R)//g;s/(TM)//g;s/ CPU//;s/ [0-9]*-Core Processor//;s/  */ /g" | cut -c2-34)" \
  "$(ps -eo pcpu=,args= --sort=-pcpu | awk "NR==1 && \$1>=5 {print substr(\$0, index(\$0,\$2), 40)}")"'
printf '%-4s %5s %6s %6s %-34s %s\n' HOST CPUS BUSY MEM CPU_MODEL CURRENT_JOB
hosts=$(awk '/^Host w[0-9]+/{print $2}' "$HOME/.ssh/config.d/workers" 2>/dev/null)
[ -n "$hosts" ] || { echo "(no workers — start some with: ddc scale N)"; exit 0; }
for h in $hosts; do
  ( r=$(timeout 8 ssh -o ConnectTimeout=5 "$h" "$probe" 2>/dev/null) || r="?|unreachable|||"
    IFS='|' read -r n b m c p <<<"$r"; printf '%-4s %5s %6s %6s %-34s %s\n' "$h" "$n" "$b" "$m" "$c" "$p" ) &
done | sort
wait
for f in "${DDC_WORK:-/work}"/results/*/joblog.tsv; do
  [ -f "$f" ] || continue
  printf '\n%s: %s jobs done, %s failed' "$(basename "$(dirname "$f")")" "$(($(wc -l < "$f") - 1))" "$(awk 'NR>1 && $7!=0' "$f" | wc -l)"
done
echo

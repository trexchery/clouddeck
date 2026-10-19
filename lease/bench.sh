#!/usr/bin/env bash
# Benchmark this lease. Prints one JSON line. Usage: bench.sh [workload ...]
# workloads (default: cpu, plus cmd when DDC_BENCH_CMD is set):
#   cpu   plain sha256 loop on one core, then on all cores (needs node)
#   cmd   your own command: DDC_BENCH_CMD (shell) run in DDC_BENCH_DIR
#         (default: the first repo of DDC_REPOS under /work, else /work), timed with cpu utilisation
set -uo pipefail
# shellcheck source=/dev/null
[ -f /etc/profile.d/ddc.sh ] && . /etc/profile.d/ddc.sh
W=${DDC_WORK:-/work}
n=${DDC_CPUS:-$(nproc)}
model=$(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2 | sed 's/^ *//')
want=("$@"); [ ${#want[@]} -gt 0 ] || { want=(cpu); [ -n "${DDC_BENCH_CMD:-}" ] && want+=(cmd); }
res=()
mkdir -p "$W/results"

timed() { # name dir cmd...
  local name=$1 dir=$2; shift 2
  [ -d "$dir" ] || { echo "bench: $name skipped, no directory $dir" >&2; return 0; }
  local t; t=$(mktemp)
  ( cd "$dir" && /usr/bin/time -f '%e %U %S' -o "$t" "$@" > "$W/results/bench-$name.log" 2>&1 ); local rc=$?
  read -r e u s < <(tail -n 1 "$t"); rm -f "$t"   # GNU time prepends "Command exited with non-zero status" on failure
  res+=("$(printf '"%s":{"wall_s":%s,"cpu_s":%.1f,"cpu_util":%.3f,"exit":%s}' "$name" "$e" "$(echo "$u + $s" | bc -l)" "$(echo "($u + $s) / ($e * $n)" | bc -l)" "$rc")")
}

for w in "${want[@]}"; do
  case $w in
    cpu)
      command -v node >/dev/null || { echo "bench: cpu skipped, node not found" >&2; continue; }
      js='const c=require("crypto");let b=Buffer.alloc(64);const t=process.hrtime.bigint();for(let i=0;i<400000;i++)b=c.createHash("sha256").update(b).digest();console.log(Number(process.hrtime.bigint()-t)/1e6)'
      st=$(node -e "$js")
      mt_start=$(date +%s.%N); seq "$n" | xargs -P "$n" -I{} node -e "$js" >/dev/null; mt=$(echo "$(date +%s.%N) - $mt_start" | bc -l)
      res+=("$(printf '"cpu":{"single_ms":%.0f,"single_score":%.1f,"multi_s":%.2f,"multi_score":%.1f}' "$st" "$(echo "400000 / $st" | bc -l)" "$mt" "$(echo "400 * $n / $mt" | bc -l)")") ;;
    cmd)
      [ -n "${DDC_BENCH_CMD:-}" ] || { echo "bench: cmd skipped, DDC_BENCH_CMD is not set" >&2; continue; }
      first=${DDC_REPOS:-}; first=${first%% *}
      timed cmd "${DDC_BENCH_DIR:-$W/${first:-.}}" bash -c "$DDC_BENCH_CMD" ;;
    *) echo "bench: unknown workload $w (cpu, cmd)" >&2 ;;
  esac
done
printf '{"host":"%s","cpus":%s,"host_nproc":'"$(nproc)"',"mem_gb":%s,"cpu_model":"%s","at":"%s"%s}\n' "$(hostname)" "$n" \
  "$(m=$(cat /sys/fs/cgroup/memory.max 2>/dev/null); [ "${m:-max}" != max ] && echo $((m/1073741824)) || free -g | awk '/Mem:/{print $2}')" "$model" "$(date -u +%FT%TZ)" \
  "$([ ${#res[@]} -gt 0 ] && printf ',%s' "$(IFS=,; echo "${res[*]}")")"

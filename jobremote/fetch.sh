#!/usr/bin/env bash
# Resumable, checksum-verified download with ordered fallbacks. Usage: fetch.sh DEST SHA256|- URL [URL...]
# URLs are tried in order (put mirrors first, the origin last). aria2c (parallel, -c resume) when present, else curl -C -.
# A mirror that is down, slow to connect, or serves different bytes just falls through to the next URL.
# Only http:// and https:// sources are used (a value starting with `-` would be read as an option by curl/aria2c; file:// and ftp:// are not
# wanted), and SHA256 must be 64 lower-case hex digits. `-` (no verification) is only passed by the runner for an item the spec marks allow_unverified.
W=${DDC_WORK:-/work}; dest=$W/${1#/}; sha=$2; shift 2
if [ "$sha" != "-" ] && ! [[ $sha =~ ^[0-9a-f]{64}$ ]]; then echo "refusing: SHA256 must be 64 hex digits or -" >&2; exit 2; fi
mkdir -p "$(dirname "$dest")"
ok() { [ -s "$dest" ] && [ ! -e "$dest.aria2" ] && { [ "$sha" = "-" ] || [ "$(sha256sum "$dest" | cut -d' ' -f1)" = "$sha" ]; }; }
ok && { echo "have $(basename "$dest")"; exit 0; }
i=0
for url in "$@"; do
  i=$((i+1))
  if ! [[ $url =~ ^https?://[^[:space:]]+$ ]]; then echo "source $i refused: not an http(s) URL; trying next" >&2; continue; fi
  if command -v aria2c >/dev/null 2>&1; then
    aria2c -q -x16 -s16 -k8M --file-allocation=none --max-tries=3 --retry-wait=3 --connect-timeout=15 -c \
      -d "$(dirname "$dest")" -o "$(basename "$dest")" "$url" && rc=0 || rc=$?
  else
    curl -fsSL --proto '=http,https' --proto-redir '=http,https' --retry 3 --connect-timeout 15 -C - -o "$dest" "$url" && rc=0 || rc=$?
  fi
  if [ "$rc" -eq 0 ] && ok; then echo "done $(basename "$dest") via source $i"; exit 0; fi
  echo "source $i failed (rc=$rc or checksum mismatch); trying next" >&2
  { [ -s "$dest" ] && [ "$sha" != "-" ]; } && rm -f "$dest" "$dest.aria2"
done
echo "all sources failed for $(basename "$dest")" >&2; exit 1

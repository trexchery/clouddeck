#!/usr/bin/env bash
set -e
# Container start command (inlined into the SDL). Image-agnostic: installs only what the image lacks, so a prebuilt
# image (docker/Dockerfile) skips apt entirely and a stock Debian/Ubuntu image still works.
export DEBIAN_FRONTEND=noninteractive
T0=$(date +%s.%N)
MISSING=""
for p in $DDC_APT_PKGS; do dpkg -s "$p" >/dev/null 2>&1 || MISSING="$MISSING $p"; done
if [ -n "$MISSING" ]; then
  # shellcheck disable=SC2086  # $MISSING is a space-separated package list: word splitting is the point
  for _ in 1 2 3; do apt-get update -qq && apt-get install -y -qq --no-install-recommends $MISSING >/dev/null && break; sleep 5; done
fi
T1=$(date +%s.%N)
mkdir -p /root/.ssh /run/sshd /work/.ddc
echo "$DDC_AUTHORIZED_KEYS_B64" | base64 -d > /root/.ssh/authorized_keys
chmod 700 /root/.ssh; chmod 600 /root/.ssh/authorized_keys
# container env (PATH, CUDA, HF_*) is invisible to sshd sessions: dump it for `. /root/.ddc_env`
export -p | grep -v -E ' (DDC_AUTHORIZED_KEYS_B64|SSH_[A-Z_]*|OLDPWD|PWD|SHLVL|_)=' > /root/.ddc_env
printf '%s\n' 'PasswordAuthentication no' 'KbdInteractiveAuthentication no' 'PermitRootLogin prohibit-password' \
  'ClientAliveInterval 30' 'ClientAliveCountMax 6' 'MaxStartups 100:30:200' 'MaxSessions 64' > /etc/ssh/sshd_config.d/ddc.conf
ssh-keygen -A >/dev/null
printf '{"start":%s,"toolchain_done":%s,"installed":"%s"}\n' "$T0" "$T1" "$MISSING" > /run/ddc-ready
exec /usr/sbin/sshd -D -e

#!/usr/bin/env bash
# Idempotent per-lease setup, run as root by ddc.py after secrets are pushed.
# Usage: setup.sh dev|worker
# Needs a Debian/Ubuntu-style image with user "dev", node/npm, git, gpg, tmux, python3 (see lease/README.md).
# Nothing is downloaded here except the opt-in, version-pinned Claude Code npm package below.
set -euo pipefail
set +x   # the lease env file holds secrets: never trace them
role=${1:?role}
D=/home/dev
C=$D/.config/ddc
install -d -o dev -g dev /work /work/results $D/.config $C $D/.ssh $D/.ssh/config.d
install -d -o dev -g dev -m 755 /run/ddc
install -m 644 -o dev -g dev /opt/ddc/tmux.conf $D/.tmux.conf
chmod 755 /opt/ddc/*.sh

# Akash limits CPU with a cgroup quota, so nproc reports the whole host; size thread pools from the quota.
read -r q per < /sys/fs/cgroup/cpu.max 2>/dev/null || q=max
if [ "$q" != max ]; then cpus=$(( (q + per - 1) / per )); else cpus=$(nproc); fi
echo "export DDC_CPUS=$cpus" > /etc/profile.d/ddc-cpus.sh
cat > /etc/profile.d/ddc.sh <<'EOF'
. /etc/profile.d/ddc-cpus.sh
export COLORTERM=truecolor LANG=C.UTF-8 EDITOR=vim.tiny
# some providers advertise IPv6 without routing it (npm ENETUNREACH)
export NODE_OPTIONS="--dns-result-order=ipv4first ${NODE_OPTIONS:-}"
export PATH="$HOME/.local/bin:$PATH"
# Claude Code is pinned (setup.sh); it must not replace itself with a newer, unreviewed version
export DISABLE_AUTOUPDATER=1
[ -f "$HOME/.config/ddc/env" ] && . "$HOME/.config/ddc/env"
EOF
# First line, before Debian's "not interactive -> return", so `ssh box cmd` gets the same env.
grep -q profile.d/ddc.sh $D/.bashrc 2>/dev/null || sed -i '1i . /etc/profile.d/ddc.sh' $D/.bashrc

# Plenty of file handles / processes for parallel test runners.
cat > /etc/security/limits.d/ddc.conf <<'EOF'
dev soft nofile 1048576
dev hard nofile 1048576
dev soft nproc 1048576
dev hard nproc 1048576
EOF

# Worker key lets the dev box reach workers; workers never receive any private key.
cat > $D/.ssh/config <<'EOF'
Include ~/.ssh/config.d/*
Host *
  ServerAliveInterval 15
  ServerAliveCountMax 4
  ControlMaster auto
  ControlPath /tmp/ssh-%C
  ControlPersist 10m
EOF
chown -R dev: $D/.ssh $C
chmod 700 $D/.ssh $C
find $C -type f -exec chmod 600 {} +

# Optional flags come from the process env or from the lease env file pushed by the controller (grepped,
# never sourced: that file also holds secrets).
envflag() { local v=${!1:-}; [ -n "$v" ] || v=$(sed -n "s/^export $1=\"\(.*\)\"\$/\1/p" "$C/env" 2>/dev/null | tail -1); printf '%s' "$v"; }

if [ "$role" = dev ]; then
  if [ -f $C/gitconfig ]; then install -m 644 -o dev -g dev $C/gitconfig $D/.gitconfig; fi
  RK=$C/repo_deploy_key; [ -f "$RK" ] || RK=$C/desk_deploy_key   # old name kept working
  cat > $D/.ssh/config.d/github <<EOF
Host github-state
  HostName github.com
  User git
  IdentityFile $C/state_deploy_key
  IdentitiesOnly yes
  UserKnownHostsFile $C/github_known_hosts
Host github.com
  User git
  IdentityFile $RK
  IdentitiesOnly yes
  UserKnownHostsFile $C/github_known_hosts
EOF
  chown dev: $D/.ssh/config.d/github
  # Claude Code: opt-in (DDC_INSTALL_CLAUDE_CODE=1) and pinned to an exact npm version
  # (DDC_CLAUDE_CODE_VERSION, default below); no download-and-pipe-to-shell installer.
  if [ ! -x $D/.local/bin/claude ] && [ "$(envflag DDC_INSTALL_CLAUDE_CODE)" = 1 ]; then
    ver=$(envflag DDC_CLAUDE_CODE_VERSION); ver=${ver:-2.1.292}
    [[ $ver =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "bad DDC_CLAUDE_CODE_VERSION: $ver" >&2; exit 1; }
    log=$(mktemp)
    su - dev -c "npm install -g --prefix \$HOME/.local --no-audit --no-fund --loglevel=error @anthropic-ai/claude-code@$ver" >"$log" 2>&1 || { cat "$log"; exit 1; }
    rm -f "$log"
  fi
  if [ -f $C/claude-credentials.json ]; then
    install -d -o dev -g dev -m 700 $D/.claude
    install -m 600 -o dev -g dev $C/claude-credentials.json $D/.claude/.credentials.json
  fi
  [ -f $D/.claude.json ] || { echo '{"hasCompletedOnboarding":true,"theme":"dark"}' > $D/.claude.json; chown dev: $D/.claude.json; }
  # Activity/idle tracker + periodic checkpoints (single instance).
  if ! pgrep -u dev -f /opt/ddc/agent.sh >/dev/null; then
    su - dev -c 'setsid nohup /opt/ddc/agent.sh >> /work/results/agent.log 2>&1 < /dev/null &'
  fi
fi
echo "setup_ok role=$role"

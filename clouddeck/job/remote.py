"""SSH access to the box: probe, resumable transfers, detached idempotent steps. No rsync/scp/tunnel needed.

Every call is a short, stateless `ssh` invocation, so a dropped connection never loses work: steps run detached on the
box (marker files), transfers append to `.part` files at a known offset and are verified by sha256 before the rename.

The ssh options are the controller's (`ddc.ssh_args`: key, BatchMode, accept-new host keys, endpoint validation); only the known-hosts
file differs (one per LEASE, see runner._ssh_remote: a provider that reuses host:port for a new container would otherwise trip it).  The provider-reported host/port are validated with valid_host / valid_port before they are ever used.
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from clouddeck import controller as ddc

from .spec import GLOB_RE, RESERVED_SECRET_PREFIXES, SHA256_RE, safe_rel_path

# what ssh says when the host key differs from the one recorded for this host:port (retrying cannot help: it is not a network problem)
HOSTKEY_CHANGED = (b"host key verification failed", b"remote host identification has changed")


def remote_dir() -> Path:
    return ddc.data_file("jobremote")


class RemoteLost(RuntimeError):
    """ssh kept failing for longer than the reconnect budget."""


class HostKeyChanged(RuntimeError):
    """ssh refused the box because its host key differs from the one this lease recorded.  Fatal for the attempt: retrying only burns paid boot time."""


class SshRemote:
    work = "/work"

    def __init__(self, host: str, port: int, key: Path | None, known_hosts: Path, clock, reconnect_s: float = 300, ssh: str | None = None,
                 beat: Callable[[], None] | None = None):
        self.host, self.port, self.known_hosts, self.clock = host, port, known_hosts, clock
        ddc.ssh_endpoint({"host": host, "port": port, "name": "the job's box"})   # refuses anything but a plain hostname/IP and a port
        self.key = key or ddc.KEYS / "access"
        self.reconnect_s = reconnect_s
        self.ssh = ssh   # None = the controller's ssh binary
        self.offset = 0.0  # remote clock - local clock, seconds
        self.beat = beat   # the runner's heartbeat: called while this object waits or retries, so a long blocking operation is not mistaken for a dead runner

    # ---- raw
    def _argv(self, cmd: str) -> list[str]:
        argv = ddc.ssh_args({"host": self.host, "port": self.port}, user="root", known_hosts=self.known_hosts)
        if self.ssh:
            argv[0] = self.ssh
        return argv + [f". /root/.ddc_env 2>/dev/null; export DDC_WORK={self.work}; cd {self.work} 2>/dev/null; {cmd}"]

    def run(self, cmd: str, data: bytes | None = None, timeout: float = 120, retry: bool = True) -> tuple[int, bytes]:
        """Returns (rc, stdout). ssh-level failure (rc 255 / timeout) is retried until the reconnect budget is spent."""
        t_end = self.clock.time() + self.reconnect_s
        delay = 3.0
        while True:
            if self.beat:
                self.beat()
            try:
                cp = subprocess.run(self._argv(cmd), input=data, capture_output=True, timeout=timeout, creationflags=ddc.NOWIN)
                if cp.returncode != 255:
                    return cp.returncode, cp.stdout
                if any(m in (cp.stderr or b"").lower() for m in HOSTKEY_CHANGED):
                    raise HostKeyChanged(f"ssh refused {self.host}:{self.port}: its host key changed ({self.known_hosts.name}); not retrying")
            except subprocess.TimeoutExpired:
                pass
            if not retry or self.clock.time() >= t_end:
                raise RemoteLost(f"ssh to {self.host}:{self.port} failing for {self.reconnect_s:.0f}s")
            self.clock.sleep(delay)
            delay = min(delay * 2, 30)

    def _sh(self, cmd: str, **kw) -> str:
        return self.run(cmd, **kw)[1].decode("utf-8", "replace")

    # ---- lifecycle
    def probe(self) -> bool:
        try:
            rc, out = self.run("cat /run/ddc-ready", timeout=40, retry=False)
        except RemoteLost:
            return False
        return rc == 0 and out.strip().startswith(b"{")

    def boot_info(self) -> dict[str, Any]:
        """/run/ddc-ready (container start + toolchain-done epochs, packages apt had to install) and the clock offset."""
        t0 = self.clock.time()
        out = self._sh("date +%s.%N; cat /run/ddc-ready")
        t1 = self.clock.time()
        first, _, rest = out.partition("\n")
        try:
            self.offset = float(first) - (t0 + t1) / 2
            info = json.loads(rest)
        except ValueError:
            return {}
        return {k: (float(v) - self.offset if k != "installed" else v) for k, v in info.items()}

    def install_scripts(self) -> None:
        self.run("mkdir -p .ddc")
        for n in ("step.sh", "fetch.sh"):
            self.run(f"cat > .ddc/{n} && chmod +x .ddc/{n}", data=(remote_dir() / n).read_bytes().replace(b"\r\n", b"\n"))

    def push_secrets(self, env: dict[str, str]) -> None:
        """Secrets travel over ssh only (never in the SDL, never in a log line) into a mode-600 file.  The provider's host can read them."""
        if env:
            bad = [k for k in env if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k)]
            if bad:
                raise ValueError(f"not a valid environment variable name: {bad[0][:40]!r}")
            reserved = [k for k in env if k.upper().startswith(RESERVED_SECRET_PREFIXES)]
            if reserved:   # the spec check refuses these too; this is the last line before they would leave the machine
                raise ValueError(f"{reserved[0]} is a controller or Akash credential name: never pushed to a rented host")
            body = "".join(f"export {k}={shlex.quote(v)}\n" for k, v in env.items()).encode()
            self.run("umask 077; cat > .ddc/secrets.env", data=body)

    # ---- steps
    def start(self, name: str, needs: list[str], cmd: str) -> None:
        inner = f"bash .ddc/step.sh {shlex.quote(name)} {shlex.quote(','.join(needs) or '-')} {shlex.quote(cmd)}"
        self.run(f"S=$(command -v setsid || true); $S nohup {inner} >/dev/null 2>&1 < /dev/null & echo started")

    def markers(self) -> dict[str, dict[str, Any]]:
        out = self._sh('cd .ddc 2>/dev/null && for f in *.started *.done *.failed; do [ -e "$f" ] && echo "$f $(cat "$f")"; done')
        res: dict[str, dict[str, Any]] = {}
        for line in out.splitlines():
            fn, _, body = line.partition(" ")
            name, _, kind = fn.rpartition(".")
            if kind == "started":
                res.setdefault(name, {})
                res[name].setdefault("state", "running")
                res[name]["start"] = float(body) - self.offset
            else:
                try:
                    j = json.loads(body)
                except ValueError:
                    continue
                r = res.setdefault(name, {})
                r.update(state="done" if kind == "done" else "failed", rc=j.get("rc"), why=j.get("why"))
                r["start"], r["end"] = float(j["start"]) - self.offset, float(j["end"]) - self.offset
        return res

    def mark_done(self, name: str) -> None:
        self.run(f'mkdir -p .ddc; t=$(date +%s.%N); printf \'{{"rc":0,"start":%s,"end":%s}}\\n\' $t $t > .ddc/{shlex.quote(name)}.done')

    def tail_log(self, name: str, n: int = 15) -> str:
        return self._sh(f"tail -n {int(n)} .ddc/{shlex.quote(name)}.log 2>/dev/null")

    # ---- transfers (resumable, sha256-verified)
    def size(self, path: str) -> tuple[int, bool]:
        """(bytes present, is_final). Looks at the final file first, then `.part`."""
        q = shlex.quote(path)
        out = self._sh(f"if [ -f {q} ]; then echo F $(stat -c %s {q}); elif [ -f {q}.part ]; then echo P $(stat -c %s {q}.part); else echo P 0; fi").split()
        return (int(out[1]), out[0] == "F") if len(out) == 2 else (0, False)

    def put(self, path: str, offset: int, data: bytes) -> bool:
        q = shlex.quote(path)
        rc, _ = self.run(f"mkdir -p $(dirname {q}); [ \"$(stat -c %s {q}.part 2>/dev/null || echo 0)\" = {int(offset)} ] && cat >> {q}.part", data=data, timeout=600)
        return rc == 0

    def finalize(self, path: str, sha: str) -> bool:
        if not SHA256_RE.fullmatch(sha):
            raise ValueError("sha256 must be 64 lower-case hex characters")
        q = shlex.quote(path)
        rc, _ = self.run(f"[ \"$(sha256sum {q}.part | cut -d' ' -f1)\" = {sha} ] && mv {q}.part {q}", timeout=1800)
        return rc == 0

    def drop_part(self, path: str) -> None:
        self.run(f"rm -f {shlex.quote(path)}.part")

    def glob(self, patterns: list[str]) -> list[tuple[str, int, str]]:
        """[(path, size, sha256)] for files matching the globs (relative to /work). Paths with spaces are not supported.  The patterns are
        expanded by the remote shell, so only plain glob characters are accepted; every returned path is checked too (the box is not trusted)."""
        for p in patterns:
            if not (GLOB_RE.fullmatch(p) and safe_rel_path(p, glob=True)):
                raise ValueError(f"not a plain relative glob: {p[:60]!r}")
        pats = " ".join(patterns)  # validated above, unquoted on purpose so the shell expands the globs
        out = self._sh(f'shopt -s nullglob; for f in {pats}; do [ -f "$f" ] && echo "$(sha256sum "$f" | cut -d" " -f1) $(stat -c %s "$f") $f"; done', timeout=1800)
        res = []
        for ln in out.splitlines():
            parts = ln.split(" ", 2)
            if len(parts) != 3 or not SHA256_RE.fullmatch(parts[0]) or not parts[1].isdigit() or not safe_rel_path(parts[2]):
                continue   # anything that is not a well-formed row for a path below /work is dropped, never trusted
            res.append((parts[2], int(parts[1]), parts[0]))
        return res

    def get(self, path: str, offset: int, n: int) -> bytes:
        return self.run(f"tail -c +{int(offset) + 1} {shlex.quote(path)} | head -c {int(n)}", timeout=1800)[1]

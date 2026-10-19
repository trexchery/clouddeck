"""Shared pieces of the end-to-end lifecycle tests (tests/test_e2e_*.py).  Not a test module.

Two worlds, both fully offline and isolated (tests/conftest.py + the root conftest.py redirect config, state, secrets and keys):

  E2EFake      tests/fake_akash.py (read-only dependency) plus an activity-agent model that follows the fake clock, ssh hooks / failure injection
               and a capture of everything sent to the lease.  Used for provisioning, scaling, failure recovery, shutdown and the TUI.
  LeaseFake    E2EFake whose DEV BOX is a real sandbox: every ssh command the controller sends to it is executed by bash against a private
               HOME, /work, /run/ddc, /opt/ddc and state clone, so the REAL lease scripts (restore.sh, checkpoint.sh, agent.sh) are driven
               through the REAL controller, with a local bare git repo as the state repo and real gpg.  Linux / WSL only (needs bash, flock,
               python3, gpg): skipped with the reason elsewhere.  setup.sh (needs root and apt) and tmux are the only lease steps not run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time as _time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fake_akash import FakeAkash

from clouddeck.engine import ddc

REAL_VERIFY = ddc.verify_checkpoint        # captured at import, before any fixture patches it
START = argparse.Namespace(profile="32", workers=0, worker_profile=None)


# ---------------------------------------------------------------------------------------------- small helpers
def run_ddc(capsys: Any, *argv: str) -> tuple[int, str]:
    """`ddc <argv>` through the real argument parser: (exit status, everything it printed)."""
    rc = ddc.main(list(argv))
    cap = capsys.readouterr()
    return rc, cap.out + cap.err


def deps() -> dict[str, dict[str, Any]]:
    return ddc.load_state()["deps"]


def live(role: str | None = None) -> list[dict[str, Any]]:
    return [d | {"dseq": k} for k, d in ddc.active_deps().items() if role is None or d.get("role") == role]


def ledger_events(kind: str | None = None) -> list[dict[str, Any]]:
    rows, bad = ddc.ledger_read()
    assert not bad, f"unreadable ledger lines {bad}"
    return [r for r in rows if kind is None or r.get("event") == kind]


def alerts_text() -> str:
    return " | ".join(ddc.recent_alerts(100))


def deleted(fake: FakeAkash) -> list[str]:
    """dseqs a DELETE was ever sent for."""
    return [route.rsplit("/", 1)[1] for method, route in fake.calls if method == "DELETE"]


def created(fake: FakeAkash) -> int:
    return sum(1 for m, r in fake.calls if (m, r) == ("POST", "/v1/deployments"))


def assert_foreign_untouched(fake: FakeAkash, dseq: str) -> None:
    assert dseq not in deleted(fake), f"a DELETE was sent for the foreign deployment {dseq}"
    assert fake.deployments[dseq]["state"] == "active"


def watchdog(fake: FakeAkash, minutes: float = 5.0) -> int:
    """One scheduler tick: the clock moves, then `ddc watchdog --once` runs (the exit status is the scheduler's view)."""
    fake.clock += minutes * 60
    return ddc.main(["watchdog", "--once"])


# ---------------------------------------------------------------------------------------------- the fake with an agent model
class E2EFake(FakeAkash):
    """FakeAkash + a model of lease/agent.sh that follows the fake clock (idle minutes, worker idle minutes, shutdown requests, periodic
    checkpoints), ssh hooks and failure injection, and a capture of what was sent to the lease."""

    def __init__(self, d: Any, monkeypatch: Any, balance: float = 100.0):
        super().__init__(d, monkeypatch, balance)
        self.model = True                       # False: serve self.agent verbatim as /run/ddc/state.json (like FakeAkash)
        self.last_activity = self.clock         # the user's last keystroke / busy moment
        self.worker_activity = 0.0              # last job dispatched to the workers
        self.busy = ""                          # active_reason while something runs ("procs", "cpu=2.0cores", ...)
        self.hooks: list[Callable[[str, str], None]] = []
        self.inputs: list[tuple[str, str, bytes | None]] = []   # (host, cmd, stdin) of every ssh call
        self.checkpoint_runs: list[tuple[str, float]] = []      # (reason, fake clock) of every successful checkpoint.sh
        self.ssh_errors: list[list[Any]] = []
        self.replies: list[list[Any]] = []
        self.offline = False                    # the Akash API is unreachable from this PC (GETs fail, writes are "uncertain" like a real timeout)
        self._requested = ""
        self._requested_at = 0.0
        self._last_ckpt = 0.0
        self._ilock = threading.Lock()          # workers are provisioned from threads: failure injection must fire exactly `times`

    def sleep(self, s: float) -> None:
        """Fake time moves by `s`; waits of half a second or less are also real: the controller's short retries (Windows' os.replace while another thread
        reads state.json, the lock polls) mean 'let the other thread finish', which an instant fake sleep would turn into a spurious failure."""
        self.clock += s
        if s <= 0.5:
            _time.sleep(s)

    # -- the user
    def activity(self) -> None:
        self.last_activity = self.clock

    def minutes(self, m: float) -> None:
        self.clock += m * 60

    # -- ssh failure injection by command text
    def fail_ssh(self, contains: str, message: str, times: int = 1, host: str | None = None, code: int = 1, exc: BaseException | None = None) -> None:
        """The next `times` ssh commands containing `contains` fail: DdcError "exit <code>: <message>" like the real ssh(), or `exc` raised as is."""
        self.ssh_errors.append([contains, message, times, host, code, exc])

    def reply_with(self, contains: str, text: str, times: int = 1) -> None:
        """The next `times` ssh commands containing `contains` succeed with this (possibly garbage) output."""
        self.replies.append([contains, text, times])

    def commands(self, contains: str) -> list[tuple[str, str]]:
        return [(h, c) for h, c in self.ssh_log if contains in c]

    # -- agent model
    def _dev_t0(self) -> float:
        ts = [v["t_lease"] for v in self.deployments.values() if v["name"].startswith("ddc-dev-") and v["state"] == "active" and "t_lease" in v]
        return min(ts, default=self.clock)

    def agent_state(self) -> dict[str, Any]:
        cfg = self.ddc.CFG
        t0 = self._dev_t0()
        enabled = bool(self.ddc.state_repo())
        if self.busy:
            self.last_activity = self.clock
        idle = int((self.clock - self.last_activity) // 60)
        up = int((self.clock - t0) // 60)
        ck_min, idle_min = int(cfg["checkpoint_minutes"]), int(cfg["idle_minutes"])
        max_min = int(cfg["max_runtime_hours"]) * 60 - 20
        failed = False
        if enabled and up >= ck_min:                       # periodic checkpoints
            if self.checkpoint == "ok":
                self._last_ckpt = max(self._last_ckpt, t0 + (up // ck_min) * ck_min * 60)
            else:
                failed = True
        req = "max_runtime" if up >= max_min else "idle" if idle >= idle_min else ""
        if req and not self._requested:                    # the agent takes a final checkpoint first, then asks
            if not enabled or self.checkpoint == "ok":
                self._requested, self._requested_at = req, self.clock
                if enabled:
                    self._last_ckpt = self.clock
                    self.checkpoint_runs.append((f"final-{req}", self.clock))
            else:
                failed = True
        elif not req and self._requested == "idle" and self.last_activity > self._requested_at:   # activity resumed: request withdrawn
            self._requested = ""
        worker_t = max(self.worker_activity, t0)
        wi = int((self.clock - worker_t) // 60)
        last = (_time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(self._last_ckpt)) if self._last_ckpt else "")
        return {"now": self.clock, "started": t0, "uptime_min": up, "idle_min": idle, "active_reason": self.busy, "dist_idle_min": wi,
                "last_checkpoint": last, "checkpoint_failed": failed, "shutdown_requested": self._requested}

    def ssh(self, d: dict[str, Any], cmd: str, user: str = "dev", input: bytes | None = None, timeout: int = 600, check: bool = True) -> str:
        host = self._box(d)
        self.inputs.append((host, cmd, input))
        for h in list(self.hooks):
            h(host, cmd)
        if host in self.unreachable and not check:       # the real ssh() only raises when check is set: callers with check=False see empty output
            self.ssh_log.append((host, cmd))
            return ""
        with self._ilock:
            hit = next((e for e in self.ssh_errors if e[2] > 0 and e[0] in cmd and e[3] in (None, host)), None)
            if hit:
                hit[2] -= 1
        if hit:
            self.ssh_log.append((host, cmd))
            if hit[5] is not None:
                raise hit[5]
            raise self.ddc.DdcError(f"ssh {user}@{host}: `{cmd[:60]}` exit {hit[4]}: {hit[1]}")
        with self._ilock:
            rep = next((r for r in self.replies if r[2] > 0 and r[0] in cmd), None)
            if rep:
                rep[2] -= 1
        if rep:
            self.ssh_log.append((host, cmd))
            return rep[1]
        if "touch /run/ddc/worker_activity" in cmd:      # dist.sh, the controller's bench/heal and CloudDeck's dispatch heartbeat all do this
            self.worker_activity = self.clock
        if self.model and "cat /run/ddc/state.json" in cmd and host not in self.unreachable:
            self.ssh_log.append((host, cmd))
            return json.dumps(self.agent_state())
        if cmd.startswith("# cd:probe") and host not in self.unreachable:
            self.ssh_log.append((host, cmd))
            return "cores_used=0.10\ncpus=32\nmem_used=1073741824\nmem_max=68719476736\ndisk=1073741824 107374182400\nload=0.10\nuptime_s=600\njob=\ntop=sshd:0\n"
        out = super().ssh(d, cmd, user=user, input=input, timeout=timeout, check=check)
        if "checkpoint.sh" in cmd:
            m = re.search(r"checkpoint\.sh (\S+)", cmd)
            self.checkpoint_runs.append((m.group(1) if m else "?", self.clock))
            self._last_ckpt = self.clock
        return out

    def api(self, method: str, path: str, body: Any = None, timeout: int = 60, ok404: bool = False) -> Any:
        if self.offline:
            self.calls.append((method, path.split("?")[0]))
            if method in ("GET", "HEAD"):
                raise self.ddc.DdcError(f"{method} {path}: connection failed")
            raise self.ddc.ApiUncertain(f"{method} {path}: timed out; it may have been executed")
        return super().api(method, path, body, timeout, ok404)

    def public_get(self, path: str) -> Any:
        if self.offline:
            raise OSError("network unreachable")
        return super().public_get(path)

    def started_dev(self) -> dict[str, Any]:
        self.ddc.cmd_start(START)
        return self.ddc.dev_box()


def install_fake(monkeypatch: Any, balance: float = 100.0, repos: bool = False, cls: type = E2EFake, **kw: Any) -> E2EFake:
    f = cls(ddc, monkeypatch, balance, **kw)
    return f.install(repos=repos)


@pytest.fixture
def e2e(monkeypatch):
    """An E2EFake with the agent model, guard on, no project repos."""
    return install_fake(monkeypatch)


@pytest.fixture
def fleet(e2e):
    """A started dev box plus two workers (all through the real controller)."""
    e2e.started_dev()
    ddc.cmd_scale(argparse.Namespace(n=2, profile="32"))
    assert len(e2e.open_dseqs()) == 3
    e2e.activity()
    return e2e


# ---------------------------------------------------------------------------------------------- real lease scripts
def _find_bash() -> str | None:
    b = shutil.which("bash")
    if os.name == "nt" and (not b or "system32" in b.lower()):
        b = None
    return b


BASH = _find_bash()
NEEDS = ("git", "gpg", "python3", "flock", "tar", "split", "sha256sum", "gzip", "hostname", "mktemp", "ps", "stat", "pgrep", "pkill")


def _real_lease_skip_reason() -> str:
    if os.name == "nt":
        return "the real lease scripts run under Linux/WSL (Git Bash has no flock or python3); the Windows run covers the same flows against the fake"
    if not BASH:
        return "real lease scripts need bash (run under WSL/Linux)"
    out = subprocess.run([BASH, "-c", "for t in %s; do command -v $t >/dev/null || echo $t; done" % " ".join(NEEDS)], capture_output=True, text=True).stdout.split()
    return f"real lease scripts need {', '.join(out)} (run under WSL/Linux)" if out else ""


SKIP_REASON = _real_lease_skip_reason()
needs_real_lease = pytest.mark.skipif(bool(SKIP_REASON), reason=SKIP_REASON or "n/a")

GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def git(*args: str, cwd: Path | str | None = None, check: bool = True) -> str:
    cp = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env={**os.environ, **GIT_ENV})
    if check and cp.returncode:
        raise AssertionError(f"git {args}: {cp.stderr}")
    return cp.stdout.strip()


def heads(remote: Path) -> dict[str, str]:
    return {r.split("\t")[1].removeprefix("refs/heads/"): r.split("\t")[0] for r in git("ls-remote", "--heads", "file://" + remote.as_posix()).splitlines()}


def snap(repo: Path) -> dict[str, Any]:
    """HEAD + git status of a repo and a digest of every file outside .git (what must survive stop -> start unchanged)."""
    files = {str(p.relative_to(repo)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(repo.rglob("*"))
             if ".git" not in p.relative_to(repo).parts and p.is_file()}
    if not (repo / ".git").exists():
        return {"files": files}
    return {"head": git("rev-parse", "HEAD", cwd=repo), "status": sorted(git("status", "--porcelain", cwd=repo).splitlines()), "files": files}


class Sandbox:
    """One lease's filesystem: HOME, /work, /run/ddc, /opt/ddc and the state clone, all under one short temp dir (gpg socket paths)."""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ddc-e2e-"))
        r = self.root
        self.home, self.work, self.run_dir, self.sr, self.tmp, self.opt, self.bin, self.gnupg = (r / n for n in ("h", "w", "r", "s", "t", "o", "bin", "g"))
        for d in (self.home / ".config" / "ddc", self.home / ".ssh" / "config.d", self.work / "results", self.run_dir, self.tmp, self.opt, self.bin, self.gnupg):
            d.mkdir(parents=True)             # setup.sh (not run here) creates /work/results, /run/ddc and the dirs under $HOME
        self.gnupg.chmod(0o700)
        (self.gnupg / "gpg.conf").write_text("s2k-count 65536\n")     # the key-derivation rounds are stored in each file: encrypting here is cheap, and so is every decryption (restore, PC verify)
        self._map = {"/var/tmp/ddc-state": str(self.sr), "/home/dev": str(self.home), "/opt/ddc": str(self.opt), "/run/ddc": str(self.run_dir), "/work": str(self.work)}
        self._pat = re.compile("(" + "|".join(re.escape(k) for k in self._map) + r")(?![\w-])")
        real_stat = shutil.which("stat") or "/usr/bin/stat"
        for name, body in (("ps", 'echo "1 init /sbin/init"\n'),     # the agent's process scan must not see the test runner itself (pytest is "work")
                           ("stat", f'case "$*" in *"/dev/pts/"*) echo 0;; *) exec {real_stat} "$@";; esac\n')):   # nor an open terminal
            (self.bin / name).write_text("#!/bin/sh\n" + body, newline="\n")
            (self.bin / name).chmod(0o755)

    def tr(self, s: str) -> str:
        """The lease paths of a command, mapped into this sandbox (single pass; /run/ddc-role etc. are left alone)."""
        return self._pat.sub(lambda m: self._map[m.group(1)], s)

    def env(self, extra: dict[str, str] | None = None, shims: bool = False) -> dict[str, str]:
        e = {**os.environ, **GIT_ENV, "HOME": str(self.home), "DDC_WORK": str(self.work), "DDC_RUN": str(self.run_dir), "DDC_STATE_CLONE": str(self.sr),
             "TMPDIR": str(self.tmp), "GNUPGHOME": str(self.gnupg), "DDC_CPUS": "32"}
        if shims:
            e["PATH"] = str(self.bin) + os.pathsep + e["PATH"]
        return {**e, **(extra or {})}

    def sh(self, cmd: str, input: bytes | None = None, timeout: int = 600) -> subprocess.CompletedProcess:
        return subprocess.run([BASH, "-c", self.tr(cmd)], input=input, capture_output=True, env=self.env(), timeout=timeout)

    def script(self, name: str, *args: str, env: dict[str, str] | None = None, shims: bool = False, timeout: int = 300) -> subprocess.CompletedProcess:
        return subprocess.run([BASH, str(self.opt / name), *args], capture_output=True, text=True, env=self.env(env, shims), timeout=timeout)

    def set_env(self, **kv: str) -> None:
        """Override lease env values (the file the controller wrote is sourced by every script; the last assignment wins)."""
        with (self.home / ".config" / "ddc" / "env").open("a", encoding="utf-8", newline="\n") as f:
            f.writelines(f'export {k}="{v}"\n' for k, v in kv.items())

    def agent_once(self, **env: str) -> dict[str, Any]:
        """One real agent.sh sample (DDC_AGENT_ONCE=1) with the process scan / terminal check shimmed; returns the state.json it wrote."""
        if env:
            self.set_env(**env)
        cp = self.script("agent.sh", env={"DDC_AGENT_ONCE": "1"}, shims=True)
        assert cp.returncode == 0, f"agent.sh rc={cp.returncode}\n{cp.stdout}\n{cp.stderr}"
        return json.loads((self.run_dir / "state.json").read_text())

    def head(self, name: str) -> str:
        return git("rev-parse", "HEAD", cwd=self.work / name)

    def leftovers(self) -> list[str]:
        """Processes whose command line mentions this sandbox, except gpg-agent / dirmngr (those are killed, they are not a finding)."""
        cp = subprocess.run(["pgrep", "-af", str(self.root)], capture_output=True, text=True)
        return [ln for ln in cp.stdout.splitlines() if "pgrep" not in ln and "gpg-agent" not in ln and "dirmngr" not in ln]

    def cleanup(self) -> list[str]:
        """Kill gpg-agent, remove the sandbox; returns the other processes that were still running (the caller fails the test on those)."""
        subprocess.run(["gpgconf", "--homedir", str(self.gnupg), "--kill", "gpg-agent"], capture_output=True, env=self.env())
        left = self.leftovers()
        subprocess.run(["pkill", "-f", str(self.root)], capture_output=True)        # whatever mentions this sandbox, gpg-agent included
        for _ in range(5):
            shutil.rmtree(self.root, ignore_errors=True)
            if not self.root.exists():
                break
            _time.sleep(0.3)
        return left


class LeaseFake(E2EFake):
    """E2EFake whose dev box is a real Sandbox per lease (a new lease = a new, empty disk); workers stay on the plain fake."""

    def __init__(self, d: Any, monkeypatch: Any, balance: float = 100.0):
        super().__init__(d, monkeypatch, balance)
        self.sandboxes: dict[tuple[Any, int], Sandbox] = {}
        self.skipped: list[str] = []              # lease commands that are answered, not executed (setup.sh, tmux, agent restart)
        self.clock = _time.time()                 # the real agent writes real timestamps into state.json

    def sandbox_of(self, d: dict[str, Any]) -> Sandbox:
        key = (d.get("host"), int(d.get("port") or 22))
        if key not in self.sandboxes:
            self.sandboxes[key] = Sandbox()
        return self.sandboxes[key]

    @property
    def box(self) -> Sandbox:
        """The sandbox of the dev box that is running now."""
        return self.sandbox_of(self.ddc.dev_box())

    def ssh(self, d: dict[str, Any], cmd: str, user: str = "dev", input: bytes | None = None, timeout: int = 600, check: bool = True) -> str:
        if not str(d.get("name", "")).startswith("ddc-dev-") or cmd.startswith("test -f /run/ddc-role"):
            return super().ssh(d, cmd, user=user, input=input, timeout=timeout, check=check)
        host = self._box(d)
        self.inputs.append((host, cmd, input))
        for h in list(self.hooks):
            h(host, cmd)
        self.ssh_log.append((host, cmd))
        if host in self.unreachable:
            if not check:
                return ""
            raise self.ddc.DdcError(f"ssh {user}@{host}: `{cmd[:60]}` exit 255: Connection timed out")
        for e in self.ssh_errors:
            if e[2] > 0 and e[0] in cmd and e[3] in (None, host):
                e[2] -= 1
                if e[5] is not None:
                    raise e[5]
                raise self.ddc.DdcError(f"ssh {user}@{host}: `{cmd[:60]}` exit {e[4]}: {e[1]}")
        if ("setup.sh" in cmd and "test -f" not in cmd) or cmd.startswith("tmux ") or ("agent.sh" in cmd and "setsid" in cmd):   # the agent restart: never start a real background loop
            self.skipped.append(cmd)
            return "setup_ok role=dev\n" if "setup.sh" in cmd else ""
        sb = self.sandbox_of(d)
        cp = sb.sh(cmd, input=input, timeout=timeout)
        out = cp.stdout.decode("utf-8", "replace")
        if check and cp.returncode != 0:
            raise self.ddc.DdcError(f"ssh {user}@{host}: `{cmd[:80]}` exit {cp.returncode}: {cp.stderr.decode('utf-8', 'replace')[-800:]}{out[-400:]}")
        if "checkpoint.sh" in cmd and cp.returncode == 0:
            m = re.search(r"checkpoint\.sh (\S+)", cmd)
            self.checkpoint_runs.append((m.group(1) if m else "?", self.clock))
        return out

    def cleanup(self) -> list[str]:
        return [ln for sb in self.sandboxes.values() for ln in sb.cleanup()]


def text_of(obj: Any) -> str:
    """Plain text of whatever a Textual widget renders (str, Rich Text / Table, Textual Content)."""
    obj = getattr(obj, "content", obj) if not isinstance(obj, str) else obj
    if isinstance(obj, str):
        return obj
    if hasattr(obj, "plain"):
        return obj.plain
    import io

    from rich.console import Console
    buf = io.StringIO()
    Console(file=buf, width=200, color_system=None).print(obj)
    return buf.getvalue()


def akash_on(fake: E2EFake) -> Any:
    """The real AkashProvider (CloudDeck's adapter over the controller) with its interactive ssh routed through the patched subprocess.run, so nothing
    can start a real ssh process.  Needs fake.patch_subprocess() (done here)."""
    from clouddeck.providers.akash import AkashProvider
    from clouddeck.providers.base import Provider

    class AkashOnFake(AkashProvider):
        stream = Provider.stream          # generic stream = run(): AkashProvider.stream spawns a real ssh process

    fake.patch_subprocess()
    return AkashOnFake()


class PC:
    """The owner's machine: two git repos and a plain folder (neutral names), registered in the config like a real setup."""

    def __init__(self, base: Path):
        self.web, self.api, self.audit = base / "web-app", base / "api-server", base / "audit"
        self.web.mkdir(parents=True)
        git("init", "-q", "-b", "main", cwd=self.web)
        for i in range(6):
            (self.web / f"f{i}.txt").write_text(f"file {i}\n" * 40)
        (self.web / "sub").mkdir()
        (self.web / "sub" / "x.py").write_text("print('x')\n")
        git("add", "-A", cwd=self.web)
        git("commit", "-qm", "one", cwd=self.web)
        (self.web / "f2.txt").write_text("changed on the pc\n")
        (self.web / "f3.txt").write_text("changed and staged\n")
        git("add", "f3.txt", cwd=self.web)
        (self.web / "notes.txt").write_text("untracked on the pc\n")
        self.api.mkdir()
        git("init", "-q", "-b", "main", cwd=self.api)
        for i in range(3):
            (self.api / f"s{i}.py").write_text(f"V = {i}\n" * 20)
        git("add", "-A", cwd=self.api)
        git("commit", "-qm", "api", cwd=self.api)
        self.audit.mkdir()
        for i in range(4):
            (self.audit / f"a{i}.md").write_text(f"audit {i}\n" * 30)
        self.repos = [{"name": "web-app", "path": str(self.web), "github": "example/web-app", "push": True}, {"name": "api-server", "path": str(self.api), "push": False}]
        self.dirs = [{"name": "audit", "path": str(self.audit)}]
        ddc.CFG["repos"], ddc.CFG["dirs"] = list(self.repos), list(self.dirs)

    def snapshot(self) -> dict[str, Any]:
        return {"web": snap(self.web), "api": snap(self.api), "audit": snap(self.audit)}


class Lease:
    """A real-lease world: LeaseFake + bare state repo + PC repos.  `start()` / `stop()` run the real CLI."""

    def __init__(self, fake: LeaseFake, remote: Path, pc: PC, capsys: Any):
        self.fake, self.remote, self.pc, self.capsys = fake, remote, pc, capsys

    def ddc(self, *argv: str) -> tuple[int, str]:
        return run_ddc(self.capsys, *argv)

    def use(self, *names: str) -> Lease:
        """Register only these projects (fewer archives = a faster checkpoint); the default is all three."""
        ddc.CFG["repos"] = [r for r in self.pc.repos if r["name"] in names]
        ddc.CFG["dirs"] = [r for r in self.pc.dirs if r["name"] in names]
        return self

    def start(self) -> tuple[int, str]:
        return self.ddc("start", "-p", "32")

    def stop(self, *flags: str) -> tuple[int, str]:
        return self.ddc("stop", "--yes", *flags)

    @property
    def box(self) -> Sandbox:
        return self.fake.box

    @property
    def heads(self) -> dict[str, str]:
        return heads(self.remote)

    def work_on_lease(self) -> dict[str, Any]:
        """What a session does on the dev box: a commit, an edit, a new untracked file, a staged file, a result file, a Claude session note."""
        w = self.box.work / "web-app"
        (w / "f0.txt").write_text("committed on the lease\n")
        git("commit", "-qam", "lease commit", cwd=w)
        (w / "f1.txt").write_text("edited on the lease, not committed\n")
        (w / "lease-only.txt").write_text("untracked, created on the lease\n")
        (w / "staged.txt").write_text("staged on the lease\n")
        git("add", "staged.txt", cwd=w)
        (self.box.work / "results").mkdir(exist_ok=True)
        (self.box.work / "results" / "run.log").write_text("42 passed\n")
        (self.box.home / ".claude" / "projects" / "p").mkdir(parents=True, exist_ok=True)
        (self.box.home / ".claude" / "projects" / "p" / "s.jsonl").write_text('{"note":"session"}\n')
        old = _time.time() - 3600   # an old session file: the agent must not read it as "the user is here"
        os.utime(self.box.home / ".claude" / "projects" / "p" / "s.jsonl", (old, old))
        return {r["name"]: snap(self.box.work / r["name"]) for r in ddc.CFG["repos"] + ddc.CFG["dirs"]}


@pytest.fixture
def lease(monkeypatch, tmp_path, capsys):
    """A started-on-demand real-lease world (skipped where the lease scripts cannot run)."""
    if SKIP_REASON:
        pytest.skip(SKIP_REASON)
    for k, v in GIT_ENV.items():                       # the PC side (seed packing, verify clone) is isolated from any user git config too
        monkeypatch.setenv(k, v)
    remote = tmp_path / "state.git"
    git("init", "-q", "--bare", "-b", "main", str(remote))
    url = "file://" + remote.as_posix()
    monkeypatch.setenv("DDC_STATE_REMOTE", url)       # lease side: local bare repo instead of github
    monkeypatch.setenv("DDC_STATE_URL", url)          # PC side: verify_checkpoint clones it
    fake = LeaseFake(ddc, monkeypatch)
    fake.install(repos=True)
    monkeypatch.setattr(ddc, "verify_checkpoint", REAL_VERIFY)      # real clone + real gpg decryption on "this PC"
    pc = PC(tmp_path / "pc")
    fake.last_activity = fake.clock
    world = Lease(fake, remote, pc, capsys)
    try:
        yield world
    finally:
        leaked = fake.cleanup()
    assert not leaked, f"processes were left running by the lease sandbox: {leaked}"

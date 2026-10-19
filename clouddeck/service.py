"""Provider-independent features built on `Provider.run`: live probes, projects and Claude sessions, workloads, logs.

Everything here talks to instances over the provider's SSH, so any provider that offers SSH gets it for free."""
from __future__ import annotations

import concurrent.futures as cf
import re
import shlex
import threading
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomlkit

from .engine import ddc
from .providers.base import Instance, Provider, ProviderError

HEARTBEAT_S = 60   # while a dispatched job runs, tell the dev box every minute that the workers are busy (the agent cannot see ssh jobs started from here)
HEARTBEAT_CMD = "touch /run/ddc/worker_activity 2>/dev/null || true"

# The lease agent (lease/ddc-lib.sh ddc_busy_procs) skips `bash -c '<script>'` wrappers, ssh and the tools used to look, because their command
# line merely contains the work they launched.  This probe runs as exactly such a wrapper, and its own job column uses the same rule (top CPU
# user of the box, wrappers skipped): it must never contain a word the agent counts as work, or looking at the box would keep it awake.
# The first-line marker comments ("# cd:probe") let a test double recognise a command without parsing bash.
PROBE_CMD = r"""# cd:probe
u1=$(awk '/^usage_usec/{print $2}' /sys/fs/cgroup/cpu.stat 2>/dev/null); sleep 0.5
u2=$(awk '/^usage_usec/{print $2}' /sys/fs/cgroup/cpu.stat 2>/dev/null)
echo cores_used=$(awk -v a="${u1:-0}" -v b="${u2:-0}" 'BEGIN{printf "%.2f",(b-a)/500000}')
read -r q per < /sys/fs/cgroup/cpu.max 2>/dev/null
if [ -n "$q" ] && [ "$q" != max ]; then cpus=$(( (q + per - 1) / per )); else . /etc/profile.d/ddc-cpus.sh 2>/dev/null; cpus=${DDC_CPUS:-$(nproc)}; fi
echo cpus=$cpus
echo mem_used=$(cat /sys/fs/cgroup/memory.current 2>/dev/null)
echo mem_max=$(cat /sys/fs/cgroup/memory.max 2>/dev/null)
echo disk=$(df -B1 --output=used,size /work 2>/dev/null | tail -1)
echo load=$(cut -d' ' -f1 /proc/loadavg)
echo uptime_s=$(cut -d. -f1 /proc/uptime)
echo job=$(ps -eo pcpu=,args= --sort=-pcpu 2>/dev/null | awk '$1>=5 { n=split($2,a,"/"); if (a[n] !~ /^(ssh|sshd|sudo|tmux|watch|timeout|ps|awk|bash|sh|sleep|cat)(:|$)/) { $1=""; sub(/^ /,""); print; exit } }' | cut -c1-80)
echo top=$(ps -eo pcpu=,comm= --sort=-pcpu | head -3 | awk '{printf "%s:%s ",$2,$1}')
command -v nvidia-smi >/dev/null && echo gpu=$(nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits | head -1)
true
"""

WORKSPACE_CMD = r"""# cd:workspace
for r in /work/*/; do n=$(basename $r)
  if [ -d $r/.git ]; then echo "$n|git|$(git -C $r rev-parse --abbrev-ref HEAD 2>/dev/null)|$(git -C $r rev-parse --short HEAD 2>/dev/null)|$(git -C $r status --porcelain 2>/dev/null | wc -l)"
  else echo "$n|dir|||$(du -sm $r 2>/dev/null | cut -f1)"; fi
done
true
"""

SESSIONS_CMD = "tmux list-windows -t main -F '#{window_index}|#{window_name}|#{window_active}|#{pane_current_command}|#{pane_current_path}|#{window_activity}' 2>/dev/null || true"

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,40}$")
TUNABLE = {"idle_minutes", "worker_idle_minutes", "checkpoint_minutes"}   # budget_guard is deliberately not editable here
BUILTIN_WORKLOADS = [
    {"name": "Smoke test", "command": "hostname; nproc; free -g | sed -n 2p; df -h /work | tail -1", "target": "workers",
     "description": "Prove every node answers and show its size"},
    {"name": "CPU benchmark", "command": "bash /opt/ddc/bench.sh cpu", "target": "workers", "description": "Single/multi-thread score per node (lease script)"},
]


@dataclass
class Probe:
    ok: bool = False
    error: str = ""
    at: float = 0.0
    cpus: float = 0.0
    cores_used: float = 0.0
    mem_used_gb: float | None = None
    mem_max_gb: float | None = None
    disk_used_gb: float | None = None
    disk_gb: float | None = None
    load: float | None = None
    uptime_s: int = 0
    job: str = ""
    top: str = ""
    gpu_util: float | None = None
    gpu_mem_used_mb: float | None = None
    gpu_mem_mb: float | None = None

    @property
    def cpu_frac(self) -> float | None:
        return min(1.0, self.cores_used / self.cpus) if self.ok and self.cpus else None

    def mem_frac(self, fallback_gb: float = 0.0) -> float | None:
        cap = self.mem_max_gb or fallback_gb
        return min(1.0, self.mem_used_gb / cap) if self.ok and self.mem_used_gb is not None and cap else None

    @property
    def disk_frac(self) -> float | None:
        return min(1.0, self.disk_used_gb / self.disk_gb) if self.ok and self.disk_used_gb is not None and self.disk_gb else None


def parse_probe(text: str) -> Probe:
    kv: dict[str, str] = {}
    for line in text.splitlines():
        k, _, v = line.partition("=")
        if _:
            kv[k.strip()] = v.strip()

    def num(k: str) -> float | None:
        try:
            return float(kv[k])
        except (KeyError, ValueError):
            return None

    p = Probe(ok="cpus" in kv, at=time.time(), cpus=num("cpus") or 0.0, cores_used=num("cores_used") or 0.0, load=num("load"),
              uptime_s=int(num("uptime_s") or 0), job=kv.get("job", ""), top=kv.get("top", ""))
    if (mu := num("mem_used")) is not None:
        p.mem_used_gb = mu / 2**30
    if (mm := num("mem_max")) is not None:
        p.mem_max_gb = mm / 2**30
    parts = kv.get("disk", "").split()
    if len(parts) == 2 and all(x.isdigit() for x in parts):
        p.disk_used_gb, p.disk_gb = int(parts[0]) / 2**30, int(parts[1]) / 2**30
    g = [x.strip() for x in kv.get("gpu", "").split(",")]
    if len(g) == 3:
        try:
            p.gpu_util, p.gpu_mem_used_mb, p.gpu_mem_mb = float(g[0]) / 100, float(g[1]), float(g[2])
        except ValueError:
            pass
    if not p.ok:
        p.error = "no data"
    return p


@dataclass
class Project:
    name: str
    kind: str                  # repo | dir
    path: str
    github: str = ""
    push: bool = False
    environment: str = ""


@dataclass
class Workspace:
    name: str
    kind: str
    branch: str = ""
    head: str = ""
    dirty: int = 0
    size_mb: int = 0


@dataclass
class Session:
    index: int
    name: str
    active: bool
    command: str
    path: str
    activity: float = 0.0


@dataclass
class DispatchResult:
    host: str
    code: int
    lines: list[str] = field(default_factory=list)


class Service:
    def __init__(self, provider: Provider):
        self.provider = provider
        self._cfg_lock = threading.Lock()

    # ------------------------------------------------------------ probes
    def probe(self, inst: Instance, timeout: int = 40) -> Probe:
        try:
            p = parse_probe(self.provider.run(inst, PROBE_CMD, timeout=timeout))
        except ProviderError as e:
            return Probe(error=str(e).split(": ", 1)[-1][:160], at=time.time())
        return p

    def probe_all(self, insts: list[Instance]) -> dict[str, Probe]:
        live = [i for i in insts if i.reachable]
        if not live:
            return {}
        with cf.ThreadPoolExecutor(max_workers=min(16, len(live))) as ex:
            return dict(zip((i.id for i in live), ex.map(self.probe, live)))

    # ------------------------------------------------------------ config-backed: projects, environments, workloads
    @property
    def config_path(self) -> Path:
        if not ddc.CONFIG_FILE or ddc.CONFIG_ERROR:
            raise ProviderError(ddc.CONFIG_ERROR or "no config found: run `ddc init`")
        return Path(ddc.CONFIG_FILE)

    def projects(self) -> list[Project]:
        out = [Project(r["name"], "repo", r["path"], r.get("github", ""), bool(r.get("push")), r.get("environment", "")) for r in ddc.CFG.get("repos", [])]
        return out + [Project(r["name"], "dir", r["path"], environment=r.get("environment", "")) for r in ddc.CFG.get("dirs", [])]

    def environments(self) -> dict[str, dict[str, Any]]:
        """Named presets from [environments.<name>]: profile / workers / hours, plus the config's profiles as built-ins."""
        env = {f"profile {k}": {"profile": k} for k in ddc.CFG["profiles"]}
        env.update({k: dict(v) for k, v in (ddc.CFG.get("environments") or {}).items()})
        return env

    def workloads(self) -> list[dict[str, Any]]:
        return [dict(w) for w in BUILTIN_WORKLOADS] + [dict(w) for w in ddc.CFG.get("workloads", [])]

    def _edit_config(self, fn: Callable[[Any], None]) -> None:
        """Comment-preserving edit of config.toml with a .bak copy and atomic replace; keeps the file's line endings."""
        with self._cfg_lock:
            raw = self.config_path.read_bytes().decode("utf-8")
            crlf = "\r\n" in raw
            doc = tomlkit.parse(raw)
            fn(doc)
            text = tomlkit.dumps(doc)
            if crlf:
                text = text.replace("\r\n", "\n").replace("\n", "\r\n")
            new = tomllib.loads(text)
            if "profiles" not in new:  # never write a file ddc cannot read back
                raise ProviderError("refusing to write an unreadable config.toml")
            before = set(ddc.config_problems(tomllib.loads(raw)))
            if added := [p for p in ddc.config_problems(new) if p not in before]:   # the controller's own validator: an edit may not make the config unsafe
                raise ProviderError("refusing to write config.toml: " + "; ".join(added))
            bak = self.config_path.with_suffix(".toml.bak")
            bak.write_bytes(raw.encode("utf-8"))
            tmp = self.config_path.with_suffix(".toml.tmp")
            tmp.write_bytes(text.encode("utf-8"))
            tmp.replace(self.config_path)

    def add_project(self, name: str, path: str, github: str = "", push: bool = False, environment: str = "") -> Project:
        name = name.strip()
        if not NAME_RE.match(name):
            raise ProviderError("name: lowercase letters, digits, '-', '_' or '.', up to 41 characters")
        if name in {p.name for p in self.projects()} | {"results", "out"}:
            raise ProviderError(f"a project called '{name}' already exists")
        path = str(Path(path.strip()).expanduser())
        if not Path(path).is_dir():
            raise ProviderError(f"{path} is not a folder on this PC")
        if github and not ddc.GITHUB_RE.fullmatch(github):
            raise ProviderError("github must look like owner/repo")
        self._need_environment(environment)
        is_repo = bool(github) or (Path(path) / ".git").exists()
        entry: dict[str, Any] = {"name": name, "path": Path(path).as_posix()}
        if is_repo:
            if github:
                entry["github"] = github
            entry["push"] = bool(push and github)
        if environment:
            entry["environment"] = environment
        key = "repos" if is_repo else "dirs"

        def apply(doc: Any) -> None:
            t = tomlkit.table()
            for k, v in entry.items():
                t[k] = v
            if key not in doc:
                doc[key] = tomlkit.aot()
            doc[key].append(t)
        self._edit_config(apply)
        ddc.CFG.setdefault(key, []).append(entry)
        return Project(name, "repo" if is_repo else "dir", entry["path"], github, bool(entry.get("push")), environment)

    def remove_project(self, name: str) -> None:
        """Forgets the project in config.toml only.  The workspace on the dev box and checkpoints are never touched."""
        kind = next((k for k in ("repos", "dirs") if any(r["name"] == name for r in ddc.CFG.get(k, []))), None)
        if not kind:
            raise ProviderError(f"no project '{name}'")

        def apply(doc: Any) -> None:
            aot = doc[kind]
            for i, t in enumerate(aot):
                if t["name"] == name:
                    del aot[i]
                    break
            if not len(aot):
                del doc[kind]
        self._edit_config(apply)
        ddc.CFG[kind] = [r for r in ddc.CFG[kind] if r["name"] != name]

    def _need_environment(self, environment: str) -> None:
        if environment and environment not in self.environments():
            raise ProviderError(f"unknown environment '{environment}' (have: {', '.join(self.environments())})")

    def set_project_environment(self, name: str, environment: str) -> None:
        self._need_environment(environment)
        kind = next((k for k in ("repos", "dirs") if any(r["name"] == name for r in ddc.CFG.get(k, []))), None)
        if not kind:
            raise ProviderError(f"no project '{name}'")

        def apply(doc: Any) -> None:
            for t in doc[kind]:
                if t["name"] == name:
                    if environment:
                        t["environment"] = environment
                    elif "environment" in t:
                        del t["environment"]
        self._edit_config(apply)
        for r in ddc.CFG[kind]:
            if r["name"] == name:
                r["environment"] = environment
                if not environment:
                    r.pop("environment", None)

    def set_tunable(self, key: str, value: int) -> None:
        if key not in TUNABLE:
            raise ProviderError(f"{key} cannot be changed from CloudDeck")
        if not 1 <= value <= 1440:
            raise ProviderError("minutes must be 1–1440")
        self._edit_config(lambda doc: doc.__setitem__(key, value))
        ddc.CFG[key] = value

    # ------------------------------------------------------------ dev box: workspaces and Claude sessions
    def workspaces(self, dev: Instance) -> dict[str, Workspace]:
        out: dict[str, Workspace] = {}
        for line in self.provider.run(dev, WORKSPACE_CMD, timeout=60).splitlines():
            f = line.split("|")
            if len(f) == 5:
                n, kind, branch, head, tail = f
                out[n] = Workspace(n, kind, branch, head, int(tail or 0) if kind == "git" else 0, int(tail or 0) if kind == "dir" else 0)
        return out

    def sessions(self, dev: Instance) -> list[Session]:
        out = []
        for line in self.provider.run(dev, SESSIONS_CMD, timeout=30).splitlines():
            f = line.split("|")
            if len(f) == 6 and f[0].isdigit():
                out.append(Session(int(f[0]), f[1], f[2] == "1", f[3], f[4], float(f[5] or 0)))
        return out

    @staticmethod
    def window_name(project: str) -> str:
        return "cc-" + re.sub(r"[^A-Za-z0-9_-]", "-", project)

    def new_session(self, dev: Instance, project: str, resume: bool = False) -> str:
        if project not in {p.name for p in self.projects()}:
            raise ProviderError(f"unknown project '{project}'")
        taken = {s.name for s in self.sessions(dev)}
        base = self.window_name(project)
        name = next(n for n in [base] + [f"{base}-{i}" for i in range(2, 20)] if n not in taken)
        q = shlex.quote
        claude = "claude --continue" if resume else "claude"
        self.provider.run(dev, f"tmux has-session -t main 2>/dev/null || tmux new-session -d -s main -n shell -c /work; "
                               f"tmux new-window -d -t main -n {q(name)} -c {q('/work/' + project)} && "
                               f"tmux send-keys -t main:{q(name)} {q(claude)} Enter", timeout=30)
        return name

    def kill_session(self, dev: Instance, name: str) -> None:
        if name in ("claude", "workers", "shell"):
            raise ProviderError(f"'{name}' is one of the built-in windows; refusing to close it")
        self.provider.run(dev, f"tmux kill-window -t main:{shlex.quote(name)}", timeout=30)

    def attach_command(self, dev: Instance, window: str = "") -> list[str]:
        remote = "tmux new-session -A -s main" + (f" \\; select-window -t main:{shlex.quote(window)}" if window else "")
        return self.provider.ssh_command(dev, tty=True) + [remote]

    def seed_missing(self, dev: Instance, name: str) -> str:
        """Copy a project from this PC to the dev box, but only when it is not on the box yet (seeding replaces the folder)."""
        if not NAME_RE.match(name):   # config.toml can be edited by hand; the name ends up in a path on the box
            raise ProviderError(f"'{name}' is not a valid project name (lowercase letters, digits, '-', '_' or '.')")
        if name not in {p.name for p in self.projects()}:   # ddc.seed() silently skips names it does not know: do not claim success
            raise ProviderError(f"unknown project '{name}'")
        done = self.provider.run(dev, "ls /work/.ddc/complete 2>/dev/null || true", timeout=30).split()
        if name in done:
            raise ProviderError(f"'{name}' is already on the dev box; refusing to overwrite it (seeding replaces the folder)")
        if self.provider.id != "akash":
            raise ProviderError("seeding is only implemented for Akash")
        if not (box := ddc.dev_box()):
            raise ProviderError("no dev box is running")
        ddc.seed(box, [name])
        return f"seeded {name}"

    # ------------------------------------------------------------ workloads
    def dispatch(self, command: str, targets: list[Instance], on_line: Callable[[str, str], None], stop: threading.Event | None = None,
                 cwd: str = "") -> list[DispatchResult]:
        """Run one shell command on every target in parallel, streaming `on_line(host, line)`."""
        if not targets:
            raise ProviderError("no target instances")
        script = (f"cd {shlex.quote(cwd)} && " if cwd else "") + command
        done = threading.Event()
        if any(t.role == "worker" for t in targets):
            threading.Thread(target=self._heartbeat, args=(done,), daemon=True).start()

        def one(inst: Instance) -> DispatchResult:
            tag = inst.name.split("-")[1] if inst.name.count("-") >= 2 else inst.name
            res = DispatchResult(tag, 1)

            def line(s: str) -> None:
                res.lines.append(s)
                on_line(tag, s)
            try:
                res.code = self.provider.stream(inst, script, line, stop)
            except ProviderError as e:
                line(f"! {e}")
            return res

        try:
            with cf.ThreadPoolExecutor(max_workers=min(32, len(targets))) as ex:
                return list(ex.map(one, targets))
        finally:
            done.set()

    def _heartbeat(self, done: threading.Event) -> None:
        """Best effort: the dev box's idle logic closes workers that did no distributed work for a while, but a job started from here over
        ssh is invisible to it.  Touch the marker it watches now, every HEARTBEAT_S while the job runs, and once at the end."""
        def beat() -> None:
            try:
                dev = next((i for i in self.provider.instances(False) if i.role == "dev" and i.reachable), None)
                if dev and self.provider.id == "akash":
                    ddc.touch_worker_activity({"host": dev.host, "port": dev.port})   # the controller owns the marker (SAF-24); it never raises
                elif dev:
                    self.provider.run(dev, HEARTBEAT_CMD, timeout=20)
            except Exception:
                pass   # never let bookkeeping break a job
        beat()
        while not done.wait(HEARTBEAT_S):
            beat()
        beat()

    # ------------------------------------------------------------ logs
    @staticmethod
    def local_log(lines: int = 200) -> str:
        try:
            return "\n".join(ddc.LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
        except FileNotFoundError:
            return ""

    def remote_log(self, dev: Instance, what: str, lines: int = 200) -> str:
        q = shlex.quote
        cmd = {"agent": f"tail -n {lines} /work/results/agent.log 2>&1",
               "claude": f"tmux capture-pane -p -t main:claude -S -{lines} 2>&1",
               "workers": f"tmux capture-pane -p -t main:workers -S -{lines} 2>&1"}.get(what)
        if cmd is None:  # a Claude session window name
            cmd = f"tmux capture-pane -p -t main:{q(what)} -S -{lines} 2>&1"
        return self.provider.run(dev, cmd, timeout=30)

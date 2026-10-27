#!/usr/bin/env python
"""ddc (Disposable Dev Cloud): on-demand Akash dev box + test workers, driven from your own PC.

  ddc init               one-time: write a starter config + secrets.env (placeholders only)
  ddc install            one-time: keys, private state repo, deploy keys, watchdog (Windows task / systemd timer / cron), balance alert
  ddc uninstall          remove the watchdog again (keys, config and state are left alone)
  ddc start [-p 32]      provision/reuse the dev box, restore workspace, start Claude Code in tmux
  ddc attach [window]    interactive Claude Code over ssh+tmux (what `claude-cloud` runs)
  ddc scale N [-p 32]    grow/shrink worker leases (no secrets on workers); [--dry-run] [--yes] [--only ID]
  ddc status             leases, spend, budget, idle state
  ddc bench [workload..] benchmark dev box + workers, cost per workload
  ddc checkpoint         encrypted checkpoint now (+ verify from this PC)
  ddc stop               list what will be closed, final checkpoint, verify it decrypts here, close ONLY our leases,
                         confirm billing stopped; [--dry-run] [--yes] [--only ID] [--discard-workspace] [--allow-shrink]
  ddc reconcile          list unresolved/unknown ddc-* deployments; closes only a lease whose create reply was lost; repair a corrupt state file
  ddc run '<cmd>'        run a command on the dev box (used by audit agents)
  ddc job ...            run a batch job on one short-lived lease with minimal paid idle time (see docs/JOBS.md; `ddc job --help`)
  ddc watchdog [--once]  (scheduled) idle / max-runtime / budget enforcement

Uses the Akash Console API flow (create -> bids -> lease), with runtimeLimitHours as the platform-side
hard stop. Only deployments recorded in our state file AND named "ddc-*" can ever be closed by this tool.
Spending is limited by the budget guard (on by default) and by hard ceilings that hold even when the guard is off.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import contextlib
import datetime as dt
import hashlib
import html
import http.client
import io
import ipaddress
import json
import math
import os
import re
import secrets as pysecrets
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Collection, Mapping
from pathlib import Path
from typing import Any

from . import __version__, sdlgen

if os.name == "nt":
    import msvcrt
else:
    import fcntl
PKG = Path(__file__).resolve().parent
ROOT = PKG.parent
# A git checkout (as opposed to an installed wheel) has ddc.py + lease/ next to the package; the original
# layout kept config.toml there too, so it stays a LEGACY config location.
CHECKOUT: Path | None = ROOT if (ROOT / "ddc.py").is_file() and (ROOT / "lease").is_dir() else None
HOME = Path.home()
WIN = os.name == "nt"


def data_file(name: str) -> Path:
    """Runtime data (sdl.yaml.tmpl, config.example.toml, lease/): packaged under clouddeck/data in a wheel, repo root in a checkout."""
    p = PKG / "data" / name
    return p if p.exists() else ROOT / name


# ---------------------------------------------------------------- config & directories
def user_config_dir(env: Mapping[str, str], home: Path, win: bool) -> Path:
    if win:
        return Path(env.get("APPDATA") or home / "AppData" / "Roaming") / "clouddeck"
    return Path(env.get("XDG_CONFIG_HOME") or home / ".config") / "clouddeck"


def find_config(env: Mapping[str, str], home: Path, win: bool, checkout: Path | None) -> Path | None:
    """$DDC_CONFIG, then the user config dir, then LEGACY: config.toml next to a checkout's ddc.py, ~/.config/drill-dev-cloud."""
    if env.get("DDC_CONFIG"):
        return Path(env["DDC_CONFIG"]).expanduser().resolve()
    cands = [user_config_dir(env, home, win) / "config.toml"]
    if checkout:
        cands.append(checkout / "config.toml")
    cands.append(home / ".config" / "drill-dev-cloud" / "config.toml")
    return next((c for c in cands if c.is_file()), None)


def secrets_dir(env: Mapping[str, str], home: Path) -> Path:
    if env.get("DDC_SECRETS_DIR"):
        return Path(env["DDC_SECRETS_DIR"]).expanduser()
    legacy = home / ".config" / "drill-dev-cloud"
    return legacy if legacy.is_dir() else home / ".config" / "clouddeck"


def state_dir(env: Mapping[str, str], home: Path, win: bool) -> Path:
    if env.get("DDC_STATE_DIR"):
        return Path(env["DDC_STATE_DIR"]).expanduser()
    xdg = Path(env.get("XDG_STATE_HOME") or home / ".local" / "state")
    for p in (Path(env.get("LOCALAPPDATA") or home) / "drill-dev-cloud", xdg / "drill-dev-cloud"):  # legacy dirs keep winning
        if p.is_dir():
            return p
    return Path(env.get("LOCALAPPDATA") or home / "AppData" / "Local") / "clouddeck" if win else xdg / "clouddeck"


def state_dir_conflict(env: Mapping[str, str], home: Path, win: bool) -> str | None:
    """An existing legacy state dir wins over the new default (installs keep their leases). If the new default dir ALSO holds a different
    state.json, which one is real is ambiguous: say so instead of guessing."""
    if env.get("DDC_STATE_DIR"):
        return None
    chosen = state_dir(env, home, win)
    new = (Path(env.get("LOCALAPPDATA") or home / "AppData" / "Local") / "clouddeck") if win else (Path(env.get("XDG_STATE_HOME") or home / ".local" / "state") / "clouddeck")
    a, b = chosen / "state.json", new / "state.json"
    if chosen != new and a.is_file() and b.is_file() and a.read_bytes() != b.read_bytes():
        return (f"two different ddc state files exist: {a} (in use: the legacy location) and {b}. ddc cannot tell which one lists your running "
                f"leases. Keep the right one in {chosen} (or set DDC_STATE_DIR) and move the other away.")
    return None


def load_config(path: Path | None) -> tuple[dict[str, Any], str | None]:
    """(config, error). Keys missing from the user's file fall back to the packaged example's safe defaults; a broken or
    absent file leaves the example in place and reports why, so importing (--help, doctor, demo mode) never crashes."""
    cfg = tomllib.loads(data_file("config.example.toml").read_text(encoding="utf-8"))
    if path is None:
        return cfg, "no config found: run `ddc init`, or set DDC_CONFIG"
    try:
        cfg.update(tomllib.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return cfg, f"config not found: {path} (run `ddc init`)"
    except (OSError, tomllib.TOMLDecodeError) as e:
        return cfg, f"cannot read config {path}: {e}"
    return cfg, None


CONFIG_FILE = find_config(os.environ, HOME, WIN, CHECKOUT)
CFG, CONFIG_ERROR = load_config(CONFIG_FILE)
SECRETS_DIR = secrets_dir(os.environ, HOME)
KEYS = SECRETS_DIR / "keys"
STATE_DIR = state_dir(os.environ, HOME, WIN)
if "pytest" in sys.modules and not (os.environ.get("DDC_STATE_DIR") and os.environ.get("DDC_SECRETS_DIR")):
    # a test run that resolved the real state/secrets dirs once overwrote a live state.json: never allow it again
    raise RuntimeError("refusing to load the controller under pytest without DDC_STATE_DIR and DDC_SECRETS_DIR (tests must never touch real state)")
STATE_FILE = STATE_DIR / "state.json"
LEDGER = STATE_DIR / "ledger.jsonl"
PERF = STATE_DIR / "perf.json"
LOG = STATE_DIR / "ddc.log"
API = os.environ.get("DDC_AKASH_API", "https://console-api.akash.network")
BLOCKS_PER_HOUR = 600  # ~6 s blocks; billed spend is read back from escrow "transferred"
NAME_PREFIX = "ddc-"  # ownership key of existing leases: frozen
REF_SINGLE_SCORE = float(CFG.get("ref_single_score", 600))  # bench.sh sha256 score of a DDR5-era core
NOWIN = subprocess.CREATE_NO_WINDOW if WIN else 0
SSH = r"C:\Windows\System32\OpenSSH\ssh.exe" if WIN and Path(r"C:\Windows\System32\OpenSSH\ssh.exe").exists() else "ssh"
SSH_KEYGEN = str(Path(SSH).with_name("ssh-keygen.exe")) if SSH.endswith(".exe") else "ssh-keygen"
PLATFORM = sys.platform
_mono = time.monotonic  # lock timeouts use the real clock even when a test replaces now()/sleep()

# Hard ceilings: enforced for every lease this tool creates, whatever the config says and even with budget_guard = false.
# Only `unsafe_disable_limits = true` in the config lifts them (and prints a loud warning on every run).
HARD_MAX_LEASES = 8        # dev box + workers alive at once
HARD_MAX_WORKERS = 7
HARD_MAX_USD_HR = 2.0      # DEFAULT ceiling of the price cap of one profile (max_usd_hr); `max_price_usd_hr` in config.toml raises it, see price_ceiling()
ABSOLUTE_MAX_USD_HR = 100.0  # what `max_price_usd_hr` itself can never exceed (only `unsafe_disable_limits = true` lifts the ceiling entirely)
HARD_MAX_RUNTIME_H = 24    # runtimeLimitHours of one lease
HARD_MAX_SCALE = 8         # leases created by one start / scale call
PENDING_GRACE_S = 3600     # an unresolved "creation outcome unknown" record is kept this long before it is dropped
JOB_ORPHAN_MIN_MINUTES = 10  # floor of `job_orphan_minutes`: the runner can be silent that long inside one blocking ssh call or after a laptop sleep
JOB_ORPHAN_MINUTES = 30    # default `job_orphan_minutes`: a `ddc job` lease whose runner has not been heard from for this long is closed by the watchdog
STALE_UNREADY_S = 30 * 60  # leases WE created that never became ready (no work on them) are closed by the watchdog after this (bids, endpoint and ssh can take ~15 min)
WATCHDOG_MAX_FAILS = 3     # consecutive watchdog failures before it attempts a safe (verified-checkpoint) stop


def resolve_gpg() -> str:
    """$DDC_GPG, then gpg on PATH, then (Windows) Git for Windows' bundled gpg."""
    if os.environ.get("DDC_GPG"):
        return os.environ["DDC_GPG"]
    found = shutil.which("gpg")
    if found:
        return found
    git_gpg = Path(r"C:\Program Files\Git\usr\bin\gpg.exe")
    return str(git_gpg) if WIN and git_gpg.exists() else "gpg"


GPG = resolve_gpg()


# ---------------------------------------------------------------- basics
LOG_STDERR = False   # --json commands: stdout carries only the JSON


def log(msg: str) -> None:
    line = f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True, file=sys.stderr if LOG_STDERR else None)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(dt.datetime.now().isoformat(timespec="seconds") + " " + msg + "\n")


class DdcError(RuntimeError):
    pass


def die(msg: str) -> None:
    raise DdcError(msg)


def now() -> float:
    return time.time()


def load_secrets() -> dict[str, str]:
    out: dict[str, str] = {}
    p = SECRETS_DIR / "secrets.env"
    if p.exists():
        for raw in p.read_text(encoding="utf-8").splitlines():
            s = raw.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def set_secret(name: str, value: str) -> None:
    p = SECRETS_DIR / "secrets.env"
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    if not text.endswith("\n") and text:
        text += "\n"
    write_private(p, text + f"{name}={value}\n")


def write_private(p: Path, text: str) -> None:
    """Secrets are owner-only (0600) on POSIX; Windows keeps the per-user profile ACLs."""
    p.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        f.write(text)
    if not WIN:
        p.chmod(0o600)


def state_repo() -> str:
    """GitHub owner/repo of the private checkpoint repo; empty = checkpoints disabled."""
    return str(CFG.get("state_repo") or "").strip()


def need_state_repo() -> str:
    return state_repo() or die("checkpoints are disabled: set state_repo (a private GitHub repo, OWNER/your-private-state-repo) in your config.toml")


def repo_path(r: dict[str, Any]) -> Path:
    """Local folder of a [[repos]]/[[dirs]] entry; '~' expands, and a path that is not a folder HERE (e.g. C:/ under WSL) names the key."""
    p = Path(str(r["path"])).expanduser()
    if not p.is_dir():
        die(f"{r['name']}: `path = \"{r['path']}\"` is not a folder on this machine (check [[repos]]/[[dirs]] in {CONFIG_FILE or 'config.toml'})")
    return p


def read_json(p: Path, default: Any) -> Any:
    """The parsed file, or `default` when it is missing, not valid JSON/UTF-8, or not the type `default` has (a damaged bookkeeping
    file must never crash the caller)."""
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):  # JSONDecodeError and UnicodeDecodeError are ValueErrors; a transient PermissionError still raises
        return default
    if isinstance(default, (dict, list)) and not isinstance(data, type(default)):
        return default
    return data


def write_json(p: Path, data: Any) -> None:
    """Atomic: temp file (fsynced) + os.replace. The state file also keeps the previous good version as state.json.bak."""
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    if p == STATE_FILE:
        _backup_state()
    for attempt in range(8):
        try:
            os.replace(tmp, p)
            return
        except PermissionError:  # a reader (CloudDeck, watchdog) holds the file for a moment
            if attempt == 7:
                raise
            time.sleep(0.05 * (attempt + 1))


def _backup_state() -> None:
    try:
        text = STATE_FILE.read_text(encoding="utf-8")
        json.loads(text)  # never replace a good backup with a damaged file
        bak = STATE_FILE.with_name(STATE_FILE.name + ".bak")
        tmp = bak.with_name(bak.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, bak)
    except (OSError, ValueError):
        pass

def load_custom_profiles() -> None:
    """Profiles created from CloudDeck (custom cpu/mem/disk/gpu) live next to the state, not in config.toml."""
    for k, v in read_json(STATE_DIR / "custom_profiles.json", {}).items():
        CFG["profiles"].setdefault(k, v)


def save_custom_profile(name: str, prof: dict[str, Any]) -> None:
    bad = profile_problems(name, prof)  # CloudDeck's custom-profile dialog ends up here: never store something that cannot be rendered safely
    if bad:
        die("; ".join(bad))
    allp = read_json(STATE_DIR / "custom_profiles.json", {})
    allp[name] = prof
    write_json(STATE_DIR / "custom_profiles.json", allp)
    CFG["profiles"][name] = prof


class LockBusy(DdcError):
    """Another ddc process holds the lock."""


class ApiUncertain(DdcError):
    """A non-idempotent request (POST/DELETE) may or may not have been executed: reconcile, never blindly retry."""


class OutcomeUnknown(DdcError):
    """A deployment create may have succeeded; it is recorded as pending and resolved by `ddc reconcile` / the watchdog."""


class RollbackFailed(DdcError):
    """Provisioning failed and closing the half-created lease failed too: it may still be billing."""


class StateCorrupt(DdcError):
    """state.json is damaged: no lease is created until it is resolved."""


class DiscardRefused(DdcError):
    """Closing the dev box would destroy an unsaved workspace and --discard-workspace was not given."""


# ---------------------------------------------------------------- locks (OS-level: released by the OS when a process dies)
def _lock_fd(path: Path, timeout: float) -> int | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    end = _mono() + timeout
    while True:
        try:
            if WIN:
                os.lseek(fd, 0, 0)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            if _mono() >= end:
                os.close(fd)
                return None
            time.sleep(0.2)


def _unlock_fd(fd: int) -> None:
    try:
        if WIN:
            os.lseek(fd, 0, 0)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    os.close(fd)


LEGACY_LOCK_STALE_S = 3 * 3600  # the pre-release tool's `ddc.lock` (O_EXCL file, deleted on exit) counts as held while younger than this


class FileLock:
    """Cross-process lock so the watchdog never closes a lease while `start` is provisioning it. It is an OS file lock, so a crashed
    run can never leave it stuck (no stale-lock timeout, no pid files). The OS lock lives in `<name>.flock`: `ddc.lock` itself is the
    pre-release tool's lock FILE (held while it exists), so the main lock also waits while a legacy `ddc.lock` younger than 3 h exists
    (older = a crashed legacy run, ignored and left for the legacy tool to remove)."""

    def __init__(self, name: str = "ddc.lock", wait_s: float = 900):
        self.p = STATE_DIR / ("ddc.flock" if name == "ddc.lock" else name)  # only the main lock shares its name with the pre-release tool
        self.legacy = STATE_DIR / name if name == "ddc.lock" else None
        self.wait_s = wait_s
        self.fd: int | None = None

    def _legacy_held(self) -> bool:
        """The pre-release tool writes `<pid> <time>` into its lock file; an empty `ddc.lock` is an OS-lock file left by an earlier build
        (or, for a few seconds, a pre-release lock between its O_EXCL create and its write: held)."""
        try:
            age = dt.datetime.now().timestamp() - self.legacy.stat().st_mtime if self.legacy is not None else LEGACY_LOCK_STALE_S  # wall clock
            if age >= LEGACY_LOCK_STALE_S:
                return False
            text = self.legacy.read_text(errors="replace").strip()
            return (not text and age < 5) or re.fullmatch(r"\d+ \d+(\.\d+)?", text) is not None
        except OSError:
            return False

    def __enter__(self):
        end = _mono() + self.wait_s
        self.fd = _lock_fd(self.p, self.wait_s)
        if self.fd is None:
            raise LockBusy(f"another ddc command holds {self.p} (waited {self.wait_s:.0f}s)")
        while self._legacy_held():  # holding our lock: a legacy command that started meanwhile cannot be told to wait, so we wait for it
            if _mono() >= end:
                _unlock_fd(self.fd)
                self.fd = None
                raise LockBusy(f"another ddc command holds {self.legacy} (the pre-release tool's lock; waited {self.wait_s:.0f}s)")
            time.sleep(0.2)
        return self

    def __exit__(self, *a):
        if self.fd is not None:
            _unlock_fd(self.fd)
            self.fd = None


class _StateLock:
    """Read-modify-write lock for state.json and the ledger: re-entrant in a thread, exclusive across threads and processes."""

    def __init__(self) -> None:
        self._t = threading.RLock()
        self._n = 0
        self._fd: int | None = None

    def __enter__(self):
        self._t.acquire()
        try:
            if self._n == 0:
                fd = _lock_fd(STATE_DIR / "state.lock", 60)
                if fd is None:
                    raise LockBusy(f"{STATE_DIR / 'state.lock'} is held by another ddc process for over 60 s")
                self._fd = fd
            self._n += 1
        except BaseException:
            self._t.release()
            raise
        return self

    def __exit__(self, *a):
        self._n -= 1
        if self._n == 0 and self._fd is not None:
            _unlock_fd(self._fd)
            self._fd = None
        self._t.release()


_state_lock = _StateLock()
_create_lock = threading.Lock()  # limit check + write-ahead record of a new lease are one step

# ---------------------------------------------------------------- per-operation cost approval
# A paid CLI command (`ddc start`, `ddc scale` up, `ddc job run`) needs `--approve-usd X`: X is what the owner explicitly approved for THIS
# one command. It covers the command's total exposure: closed leases at what they cost, live ones at their worst case (cap x runtime limit).
# A lease that would push the exposure past X is refused, so a fallback or retry never spends beyond the approval. There is no env var or
# config key for it on purpose: an approval lasts one process and is never persisted.
APPROVAL: dict[str, Any] | None = None   # {"usd": X, "live": {name|dseq: worst}, "spent": float}
_approval_lock = threading.Lock()


def approve_hint(worst: float) -> float:
    return math.ceil(round(worst * 100, 6)) / 100


def require_approval(usd: float | None, worst: float, what: str) -> None:
    """Called by a paid CLI command before it creates anything; `worst` is the command's worst case at the price caps."""
    global APPROVAL
    if usd is None:
        die(f"{what} creates paid lease(s): worst case ${worst:.2f} (price cap x runtime limit). Nothing was created. "
            f"Ask the owner to approve that amount, then re-run with --approve-usd {approve_hint(worst):.2f}")
    if not (isinstance(usd, int | float) and math.isfinite(usd) and usd > 0):
        die(f"--approve-usd must be a positive amount, not {usd!r}")
    if worst > usd + 1e-9:
        die(f"{what}: worst case ${worst:.2f} is above the approved ${usd:.2f}; nothing was created (approve at least {approve_hint(worst):.2f}, or lower the price cap / runtime)")
    with _approval_lock:
        if APPROVAL is None:   # `ddc start -w N` approves dev box + workers once, its inner scale call keeps that approval
            APPROVAL = {"usd": float(usd), "live": {}, "spent": 0.0}


def _approval_reserve(name: str, worst: float) -> None:
    if APPROVAL is None:
        return
    with _approval_lock:
        exposure = APPROVAL["spent"] + sum(APPROVAL["live"].values()) + worst
        if exposure > APPROVAL["usd"] + 1e-9:
            die(f"refusing to create {name}: this command's exposure would reach ${exposure:.2f}, above the ${APPROVAL['usd']:.2f} approved "
                f"(--approve-usd); no further paid attempt without a new approval")
        APPROVAL["live"][name] = worst


def _approval_rekey(name: str, dseq: str) -> None:
    if APPROVAL is not None:
        with _approval_lock:
            if name in APPROVAL["live"]:
                APPROVAL["live"][dseq] = APPROVAL["live"].pop(name)


def _approval_closed(dseq: str, cost: float) -> None:
    if APPROVAL is not None:
        with _approval_lock:
            if APPROVAL["live"].pop(dseq, None) is not None:
                APPROVAL["spent"] += max(0.0, float(cost))


# ---------------------------------------------------------------- config validation
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}")
GITHUB_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+")
SIZE_RE = re.compile(r"\d{1,6}(\.\d{1,3})?(Mi|Gi|Ti)")
IMAGE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}")
MODEL_RE = re.compile(r"[A-Za-z0-9._-]{1,40}")
PROFILE_RE = re.compile(r"[A-Za-z0-9._-]{1,40}")
VERSION_RE = re.compile(r"\d+\.\d+\.\d+")
_HOST_LABEL = re.compile(r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)")


def limits_unsafe() -> bool:
    """`unsafe_disable_limits = true` lifts the hard ceilings and the lease-count caps (never the budget guard itself)."""
    return CFG.get("unsafe_disable_limits") is True


def price_ceiling() -> float:
    """The most one profile's price cap (`max_usd_hr`) may be, in USD per hour: `max_price_usd_hr` from config.toml (default 2.0, never above 100).
    A malformed value falls back to the default (the config check reports it).  `unsafe_disable_limits = true` lifts the ceiling as before."""
    v = CFG.get("max_price_usd_hr", HARD_MAX_USD_HR)
    return float(v) if _num(v) and 0 < v <= ABSOLUTE_MAX_USD_HR else HARD_MAX_USD_HR


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def cfg_flag(key: str, default: bool) -> bool:
    v = CFG.get(key, default)
    return v if isinstance(v, bool) else default


def valid_host(h: Any) -> bool:
    """A provider-controlled host goes into ssh config files and shell commands: only a plain hostname or IP is accepted."""
    if not isinstance(h, str) or not h or len(h) > 253 or not re.fullmatch(r"[0-9A-Za-z.:-]+", h):
        return False
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        pass
    return all(_HOST_LABEL.fullmatch(x) for x in h.rstrip(".").split("."))


def valid_port(p: Any) -> bool:
    return (_int(p) or (isinstance(p, str) and p.isascii() and p.isdigit())) and 1 <= int(p) <= 65535


def profile_problems(name: str, pr: Any) -> list[str]:
    p = f"profile {name!r}: "
    if not isinstance(pr, dict):
        return [p + "must be a table"]
    out = []
    if not PROFILE_RE.fullmatch(str(name)):
        out.append(p + "the name may only use letters, digits and . _ -")
    out += [p + x for x in sdlgen.hardware_problems(pr, CFG)]   # cpu / gpu counts and sizes, GPU vendor / model / VRAM / interface, CPU arch / vendor, region, pins, auditor
    for k in ("mem", "disk"):
        if not (isinstance(pr.get(k), str) and SIZE_RE.fullmatch(pr[k])):
            out.append(p + f"{k} must look like 32Gi (units Mi, Gi or Ti), got {pr.get(k)!r}")
    price = pr.get("max_usd_hr")
    if not (_num(price) and price > 0):
        out.append(p + f"max_usd_hr must be a positive number, got {price!r}")
    elif uact_block_from_usd_hr(price) < 1:  # the SDL price is a whole number of uact per block: below this it renders as 0
        out.append(p + f"max_usd_hr {price} is too small to be a price (the SDL needs at least ${usd_hr_from_uact_block(1):.4f}/h)")
    elif price > price_ceiling() and not limits_unsafe():
        out.append(p + f"max_usd_hr {price} is above the hard ceiling ${price_ceiling():g}/h (raise `max_price_usd_hr` in config.toml, up to ${ABSOLUTE_MAX_USD_HR:g}, "
                       "or `unsafe_disable_limits = true` lifts it)")
    gm = pr.get("gpu_model")
    if gm not in (None, "") and not (isinstance(gm, str) and MODEL_RE.fullmatch(gm)):
        out.append(p + f"gpu_model may only use letters, digits and . _ -, got {gm!r}")
    return out


def config_problems(cfg: Mapping[str, Any] | None = None, profile: str | None = None) -> list[str]:
    """Everything wrong with the config that could make a lease unsafe or break the SDL/shell. [] = fine. `profile` limits the
    profile checks to one profile (so a broken unrelated custom profile does not block the rest)."""
    c = CFG if cfg is None else cfg
    out: list[str] = []
    unsafe = c.get("unsafe_disable_limits") is True

    def number(key: str, lo: float, hi: float | None, whole: bool = False, ceiling: float | None = None) -> None:
        if key not in c:
            return
        v = c[key]
        if not (_int(v) if whole else _num(v)) or v < lo or (hi is not None and v > hi):
            out.append(f"{key} must be {'a whole number' if whole else 'a number'} >= {lo}" + (f" and <= {hi}" if hi is not None else "") + f", got {v!r}")
        elif ceiling is not None and v > ceiling and not unsafe:
            out.append(f"{key} = {v} is above the hard ceiling {ceiling:g} (only `unsafe_disable_limits = true` lifts it)")

    number("budget_usd", 0.01, 1e6)
    number("reserve_usd", 0, 1e6)
    number("max_runtime_hours", 1, None, whole=True, ceiling=HARD_MAX_RUNTIME_H)
    number("worker_runtime_hours", 1, None, whole=True, ceiling=HARD_MAX_RUNTIME_H)
    number("idle_minutes", 1, 100000)
    number("worker_idle_minutes", 1, 100000)
    number("checkpoint_minutes", 1, 100000)
    number("job_orphan_minutes", JOB_ORPHAN_MIN_MINUTES, 100000)
    number("max_total_leases", 1, None, whole=True, ceiling=HARD_MAX_LEASES)
    number("max_workers", 0, None, whole=True, ceiling=HARD_MAX_WORKERS)
    number("max_leases_per_provider", 1, 1000, whole=True)
    number("min_uptime_7d", 0, 1)
    number("max_price_usd_hr", 0.01, ABSOLUTE_MAX_USD_HR)   # the price-cap ceiling of one profile: default 2.0, raise it on purpose for GPUs
    for k, cap in sdlgen.ABS_MAX.items():                    # fat-finger bounds of the hardware form, up to the network's own maxima
        number(k, 1, cap, whole=True)
    for k in ("budget_guard", "unsafe_disable_limits", "install_claude_code", "push_claude_credentials", "idle_discards_workspace"):
        if k in c and not isinstance(c[k], bool):
            out.append(f"{k} must be true or false (no quotes), got {c[k]!r}")
    cv = c.get("claude_code_version")
    if cv not in ("", None) and not (isinstance(cv, str) and VERSION_RE.fullmatch(cv)):
        out.append(f"claude_code_version must look like 2.1.292, got {cv!r}")
    for k in ("busy_regex", "bench_cmd", "bench_dir", "alert_command", "worker_prep_command"):
        v = c.get(k, "")
        if not isinstance(v, str) or re.search(r"[\x00-\x08\x0a-\x1f\x7f]", v):
            out.append(f"{k} must be a single-line string")
    sx = c.get("sync_excludes", [])
    names = sx.split() if isinstance(sx, str) else sx
    if not isinstance(names, list) or not all(isinstance(x, str) and re.fullmatch(r"[A-Za-z0-9._*/-]+", x) for x in names):
        out.append("sync_excludes must be a list of plain file/directory names")
    img = c.get("image", "")
    if not (isinstance(img, str) and IMAGE_RE.fullmatch(img)):
        out.append(f"image must be a plain container image reference (letters, digits and . _ / : @ -), got {img!r}")
    repo = c.get("state_repo", "")
    if repo and not (isinstance(repo, str) and GITHUB_RE.fullmatch(repo)):
        out.append(f"state_repo must look like OWNER/NAME, got {repo!r}")
    profs = c.get("profiles")
    if not isinstance(profs, dict) or not profs:
        out.append("no [profiles.*] defined")
    else:
        for name, pr in profs.items():
            if profile is None or name == profile:
                out += profile_problems(name, pr)
        if profile is not None and profile not in profs:
            out.append(f"unknown profile {profile!r}")
    seen: set[str] = set()
    for kind in ("repos", "dirs"):
        for r in c.get(kind) or []:
            nm = r.get("name") if isinstance(r, dict) else None
            if not (isinstance(nm, str) and NAME_RE.fullmatch(nm)):
                out.append(f"[[{kind}]] name {nm!r} must use only letters, digits . _ - (it becomes a directory under /work)")
                continue
            if nm in seen:
                out.append(f"[[{kind}]] name {nm!r} is used twice")
            seen.add(nm)
            if not isinstance(r.get("path"), str) or not r["path"]:
                out.append(f"[[{kind}]] {nm}: path is required")
            if r.get("github") is not None and not (isinstance(r["github"], str) and GITHUB_RE.fullmatch(r["github"])):
                out.append(f"[[{kind}]] {nm}: github must look like OWNER/NAME, got {r['github']!r}")
            for x in r.get("exclude") or []:
                if not (isinstance(x, str) and x and "/" not in x):
                    out.append(f"[[{kind}]] {nm}: exclude entries must be plain names, got {x!r}")
    return out


def require_config(profile: str | None = None) -> None:
    bad = config_problems(profile=profile)
    if bad:
        die("config problems (fix them in " + str(CONFIG_FILE or "your config.toml") + "):\n  - " + "\n  - ".join(bad))


def config_warnings() -> list[str]:
    """Not errors, but worth a loud line in `ddc status` / `clouddeck doctor`."""
    w = []
    if limits_unsafe():
        w.append("unsafe_disable_limits = true: the hard spending ceilings are OFF")
    if not CFG.get("budget_guard", True):
        try:  # the limits check_spend enforces (a malformed value is reported by config_problems)
            leases, wk = min(int(CFG.get("max_total_leases", 4)), HARD_MAX_LEASES), min(int(CFG.get("max_workers", 4)), HARD_MAX_WORKERS)
        except (TypeError, ValueError):
            leases, wk = HARD_MAX_LEASES, HARD_MAX_WORKERS
        w.append("budget_guard = false: budget/reserve are not checked" + ("" if limits_unsafe() else
                 f" (limits still apply: {leases} leases, {wk} workers, ${price_ceiling():g}/h per lease, {HARD_MAX_RUNTIME_H} h)"))
    if not state_repo():
        w.append("state_repo is empty: checkpoints are disabled, stopping the dev box destroys anything not pushed to GitHub")
    if "@sha256:" not in str(CFG.get("image", "")):
        w.append("image is a mutable tag, not pinned by @sha256:<digest>: the box runs whatever the tag points to when it starts")
    if cfg_flag("push_claude_credentials", False):
        w.append("push_claude_credentials = true: your Claude login/token is copied to rented hosts, which can read it while the lease runs")
    return w

load_custom_profiles()


def api(method: str, path: str, body: Any = None, timeout: int = 60, ok404: bool = False) -> Any:
    """One Console API call. Only GET is retried on timeouts/5xx. A POST/DELETE that times out or hits a 5xx may already have been
    executed, so it raises ApiUncertain and the caller reconciles (lists what exists) instead of repeating it."""
    key = load_secrets().get("AKASH_API_KEY") or die(f"AKASH_API_KEY missing in {SECRETS_DIR / 'secrets.env'}")
    data = json.dumps(body).encode() if body is not None else None
    idempotent = method in ("GET", "HEAD")
    for attempt in range(4):
        req = urllib.request.Request(API + path, data=data, method=method, headers={
            "x-api-key": key, "Accept": "application/json", "Content-Type": "application/json", "User-Agent": f"ddc/{__version__}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "replace")
                try:
                    return json.loads(raw) if raw else {}
                except ValueError as e:
                    if idempotent:
                        die(f"{method} {path}: unreadable response ({e})")
                    raise ApiUncertain(f"{method} {path}: unreadable response ({e}); it may have been executed") from e
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:600]
            if e.code == 404 and ok404:
                return None
            if e.code == 429 or (e.code >= 500 and idempotent):  # 429 = rejected before processing: safe to repeat
                time.sleep(3 * (attempt + 1))
                continue
            if e.code >= 500:
                raise ApiUncertain(f"HTTP {e.code} {method} {path}: {detail}; it may have been executed") from e
            die(f"HTTP {e.code} {method} {path}: {detail}")
        except (OSError, http.client.HTTPException) as e:  # URLError, timeouts, resets
            if not idempotent:
                raise ApiUncertain(f"{method} {path}: {e}; it may have been executed") from e
            if attempt == 3:
                die(f"{method} {path}: {e}")
            time.sleep(3 * (attempt + 1))
    die(f"{method} {path}: retries exhausted")

def public_get(path: str) -> Any:
    req = urllib.request.Request(API + path, headers={"User-Agent": f"ddc/{__version__}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def balance_usd() -> float:
    d = api("GET", "/v1/balances")["data"]
    return float(d["total"]) / 1e6


def deployment(dseq: str) -> dict[str, Any]:
    body = api("GET", f"/v1/deployments/{dseq}", ok404=True) or {}
    data = body.get("data") if isinstance(body, dict) else None
    if data and not isinstance(data, dict):  # a list / string where details belong: not a usable answer (never "closed")
        die(f"GET /v1/deployments/{dseq}: unexpected reply shape ({type(data).__name__})")
    return data or {}


def _transferred_usd(det: dict[str, Any] | None) -> float:
    try:
        return sum(float(x.get("amount") or 0) for x in (((det or {}).get("escrow_account") or {}).get("state") or {}).get("transferred") or []) / 1e6
    except (AttributeError, TypeError, ValueError):  # an odd reply shape: the running estimate is used instead (close_box / refresh_live take the max)
        return 0.0


def _det_owner(det: dict[str, Any] | None) -> str:
    d = (det or {}).get("deployment") or {}
    return str((d.get("id") or {}).get("owner") or (det or {}).get("owner") or "")


def _det_endpoint(det: dict[str, Any]) -> tuple[Any, Any]:
    """(host, externalPort) of the lease's ssh port, or (None, None)."""
    for lease in det.get("leases") or []:
        for fp in ((lease.get("status") or {}).get("forwarded_ports") or {}).get("box") or []:
            if int(fp.get("port") or 0) == 22:
                return fp.get("host"), fp.get("externalPort")
    return None, None


def deployment_status(dseq: str) -> tuple[str, dict[str, Any]]:
    """("open" | "closed" | "unknown", details). Only an explicit closed state counts as closed: an empty body, a 404 or an API
    error is "unknown" (it never means the lease stopped billing)."""
    try:
        det = deployment(dseq)
    except DdcError as e:
        log(f"warning: dseq {dseq}: cannot read the deployment ({e})")
        return "unknown", {}
    dep = det.get("deployment") if det else None
    state = dep.get("state") if isinstance(dep, dict) else None
    if not state or not isinstance(state, str):
        return "unknown", det or {}
    return ("closed" if state == "closed" else "open"), det


def billing_dseqs() -> set[str] | None:
    """dseqs the platform is billing right now (/v1/spend-rate); None when that cannot be read. Only a well-formed reply (a `deployments`
    list, possibly empty) counts: an empty body, `{}` or `{"data": null}` is "unknown", never "nothing is billing"."""
    try:
        data = api("GET", "/v1/spend-rate").get("data")
        rows = data.get("deployments") if isinstance(data, dict) else None
        return {str(x["dseq"]) for x in rows} if isinstance(rows, list) else None
    except (DdcError, KeyError, TypeError, AttributeError):
        return None


# ---------------------------------------------------------------- state, ledger, budget
def _read_state_text() -> str | None:
    for attempt in range(6):
        try:
            return STATE_FILE.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except PermissionError:  # a writer's os.replace is in flight (Windows)
            if attempt == 5:
                raise
            time.sleep(0.05 * (attempt + 1))
    return None


def _read_bak() -> str | None:
    try:
        return STATE_FILE.with_name(STATE_FILE.name + ".bak").read_text(encoding="utf-8")
    except OSError:
        return None


def _parse_state(text: str | None) -> dict[str, Any] | None:
    try:
        st = json.loads(text or "")
    except ValueError:
        return None
    if not isinstance(st, dict):
        return None
    st.setdefault("deps", {})  # a state file that only holds flags (e.g. idle_hold_until) is fine
    if not (isinstance(st["deps"], dict) and all(isinstance(v, dict) for v in st["deps"].values()) and isinstance(st.get("pending", {}), dict)):
        return None
    return st


def _state_help() -> str:
    info = read_json(STATE_DIR / "state.corrupt", {})
    return (f"{STATE_FILE} is unusable ({info.get('why', 'unknown reason')}). ddc will NOT create leases until this is resolved, because it "
            f"could no longer see what is already billing. Your data is kept: {STATE_FILE.name}.bak (previous good copy)"
            f"{', ' + str(info['moved_to']) if info.get('moved_to') else ''}. Fix: `ddc reconcile` shows what the Akash account has; "
            "`ddc reconcile --restore-backup` puts the .bak back, or `ddc reconcile --adopt` rebuilds records from the account. "
            "`ddc status` and `ddc stop` keep working meanwhile: stop (and the watchdog's safe stop) then work from a read-only listing of "
            "the ddc-* deployments on the account and never touch other names.")


def _flag_corrupt(why: str, moved_to: str | None = None) -> None:
    write_json(STATE_DIR / "state.corrupt", {"at": dt.datetime.now().isoformat(timespec="seconds"), "why": why, "moved_to": moved_to})
    log(f"ERROR: state problem: {why}")


def _quarantine_state() -> dict[str, Any] | None:
    """The state file did not parse. Move it aside (never delete) and refuse to continue. Returns a good state if another process
    fixed it meanwhile."""
    with _state_lock:
        text = _read_state_text()
        st = _parse_state(text)
        if st is not None:
            return st
        moved = None
        if text is not None:
            moved = f"state.json.corrupt-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"
            try:
                os.replace(STATE_FILE, STATE_FILE.with_name(moved))
            except OSError as e:
                log(f"warning: could not move the damaged state file aside: {e}")
                moved = None
        _flag_corrupt("state.json is empty or not valid ddc state" if text is not None else "state.json is missing", moved)
        return None


def load_state() -> dict[str, Any]:
    """The lease records. A missing file on a fresh install is empty state; a damaged one is NEVER read as "no leases": it is moved
    aside, a flag blocks new leases, and StateCorrupt is raised until `ddc reconcile` resolves it."""
    if (STATE_DIR / "state.corrupt").exists():
        raise StateCorrupt(_state_help())
    text = _read_state_text()
    if text is None:
        bak = _parse_state(_read_bak())
        if bak and any(v.get("status") != "closed" for v in bak["deps"].values()):  # deleted by hand while leases were running
            with _state_lock:
                if not STATE_FILE.exists():
                    _flag_corrupt("state.json is missing but state.json.bak lists running leases")
            raise StateCorrupt(_state_help())
        return {"deps": {}}
    st = _parse_state(text)
    if st is None:
        st = _quarantine_state()
        if st is None:
            raise StateCorrupt(_state_help())
    return st


def update_state(fn: Callable[[dict[str, Any]], Any]) -> Any:
    """Read-modify-write of state.json under the cross-process lock (the watchdog, `ddc` and CloudDeck can all write)."""
    with _state_lock:
        st = load_state()
        r = fn(st)
        write_json(STATE_FILE, st)
        return r


def update_dep(dseq: str, **fields: Any) -> None:
    update_state(lambda st: st["deps"].setdefault(dseq, {}).update(fields))


def pending_creations(st: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """Write-ahead records of deployments whose creation was started but not yet recorded under their dseq (name -> record)."""
    return dict((st if st is not None else load_state()).get("pending") or {})


def ledger_read() -> tuple[list[dict[str, Any]], list[int]]:
    """(entries, numbers of unreadable lines). A torn final line (no trailing newline: power loss mid-append) is not an error."""
    entries: list[dict[str, Any]] = []
    bad: list[int] = []
    if not LEDGER.exists():
        return entries, bad
    lines = LEDGER.read_bytes().decode("utf-8", "replace").split("\n")
    for i, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except ValueError:
            e = None
        if isinstance(e, dict):
            entries.append(e)
        elif not (i == len(lines)):  # the last element has no newline after it: a torn tail
            bad.append(i)
    return entries, bad


def ledger_add(entry: dict[str, Any]) -> None:
    """Append one line (fsynced). A torn final line from an earlier crash is cut off first (kept in ledger.jsonl.torn-*) so the new
    line never glues onto it."""
    line = (json.dumps({"at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"), **entry}) + "\n").encode()
    with _state_lock:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(LEDGER, "a+b") as f:
            f.seek(0, 2)
            size = f.tell()
            if size:
                f.seek(0)
                data = f.read()
                if not data.endswith(b"\n"):
                    cut = data.rfind(b"\n") + 1
                    tail = data[cut:]
                    try:
                        ok = isinstance(json.loads(tail), dict)
                    except ValueError:
                        ok = False
                    if ok:
                        line = b"\n" + line
                    else:
                        (STATE_DIR / f"ledger.jsonl.torn-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}").write_bytes(tail)
                        log("warning: ledger.jsonl ended in a torn line (interrupted write); it was set aside")
                        f.truncate(cut)
            f.seek(0, 2)
            f.write(line)
            f.flush()
            os.fsync(f.fileno())


_warned: set[str] = set()


def warn_once(key: str, msg: str) -> None:
    if key not in _warned:
        _warned.add(key)
        log(msg)


def ledger_spent() -> float:
    entries, bad = ledger_read()
    if bad:
        warn_once("ledger-bad", f"warning: {LEDGER} has unreadable lines {bad[:5]}: spend may be under-counted; new leases are refused until it is fixed")
    return sum(float(e.get("cost_usd") or 0) for e in entries if e.get("event") == "closed")


def active_deps(st: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    st = st or load_state()
    return {k: v for k, v in st["deps"].items() if v.get("status") not in ("closed",)}

def usd_hr_from_uact_block(amount: float) -> float:
    return amount * BLOCKS_PER_HOUR / 1e6


def uact_block_from_usd_hr(usd_hr: float) -> int:
    return int(usd_hr * 1e6 / BLOCKS_PER_HOUR)


def _finite(v: Any, default: float = 0.0, positive: bool = False) -> float:
    """float(v) when that is a finite number (and above zero if `positive`), else `default`: a record is a file on disk and can hold anything."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) and (f > 0 or not positive) else default


def dep_usd_hr(d: dict[str, Any]) -> float:
    """$/h a lease record is costed at: its real price, else its price cap, else 0 when it holds neither.  A price that is present but NaN, infinite or
    not above zero (a damaged or hostile record) is never taken at face value, which would count the lease as free: it falls back to the cap, and
    without a usable cap to the hard price ceiling."""
    broken = False
    for k in ("usd_hr", "max_usd_hr"):
        v = d.get(k)
        if v in (None, "", 0):
            continue
        f = _finite(v, 0.0, positive=True)
        if f:
            return f
        broken = True
    return price_ceiling() if broken else 0.0


def dep_running_cost(d: dict[str, Any], t: float | None = None) -> tuple[float, float]:
    """(spent so far, worst-case still committed until the platform runtime limit)."""
    t = t or now()
    usd_hr = dep_usd_hr(d)
    start = _finite(d.get("lease_at") or d.get("created_at"), t)
    limit_h = _finite(d.get("limit_h"), 0.0, positive=True) or float(CFG["max_runtime_hours"])
    elapsed_h = max(0.0, (t - start) / 3600)
    spent = max(_finite(d.get("transferred_usd")), elapsed_h * usd_hr)
    return spent, usd_hr * max(0.0, limit_h - elapsed_h)


def budget_view(st: dict[str, Any] | None = None, t: float | None = None) -> dict[str, float]:
    """spent = closed (ledger) + running so far; committed = what running leases (and creations whose outcome is still unknown,
    counted at their price cap) can still cost until their runtime limit."""
    st = st if st is not None else load_state()
    spent_closed = ledger_spent()
    running = [dep_running_cost(d, t) for d in active_deps(st).values()]
    running += [dep_running_cost(p, t) for p in (st.get("pending") or {}).values()]
    spent = spent_closed + sum(r[0] for r in running)
    committed = sum(r[1] for r in running)
    return {"spent": spent, "committed": committed, "worst": spent + committed, "budget": float(CFG["budget_usd"])}

def can_afford(view: dict[str, float], balance: float, new_usd_hr: float, new_hours: float,
               budget: float, reserve: float) -> tuple[bool, str]:
    new = new_usd_hr * new_hours
    if not all(math.isfinite(x) for x in (new, balance, budget, reserve, *view.values())):   # NaN compares False with everything: it would pass both checks below
        return False, f"cannot verify the budget: a price, balance or recorded cost is not a finite number (new ${new_usd_hr!r}/h x {new_hours!r} h, view {view})"
    if new <= 0:
        return False, f"refusing a lease that costs {new_usd_hr!r} $/h x {new_hours!r} h (must be above zero)"
    if view["worst"] + new > budget:
        return False, f"would exceed budget: worst-case ${view['worst'] + new:.2f} > ${budget:.2f}"
    if balance - view["committed"] - new < reserve:
        return False, f"would breach reserve: balance ${balance:.2f} - committed ${view['committed'] + new:.2f} < ${reserve:.2f}"
    return True, f"ok: worst-case ${view['worst'] + new:.2f} of ${budget:.2f}, balance after ${balance - view['committed'] - new:.2f}"


# ---------------------------------------------------------------- providers & bids
_prov_cache: dict[str, Any] = {}


def providers() -> dict[str, dict[str, Any]]:
    if not _prov_cache:
        for p in public_get("/v1/providers"):
            _prov_cache[p["owner"]] = p
    return _prov_cache


def prov_memory(p: dict[str, Any]) -> str:
    for a in p.get("attributes") or []:
        if a.get("key") in ("capabilities/memory", "hardware-memory"):
            return str(a.get("value") or "").lower()
    return str(p.get("hardwareMemory") or "").lower()


def speed_factor(prov: str, pinfo: dict[str, Any], perf: dict[str, Any]) -> float:
    """Relative single-thread speed. Measured (bench) beats the DDR5/DDR4 heuristic."""
    if (perf.get(prov) or {}).get("single_score"):
        return perf[prov]["single_score"] / REF_SINGLE_SCORE
    mem = prov_memory(pinfo)
    # ponytail: DDR5 implies Zen4/Sapphire Rapids or newer; calibration knob, replaced by bench data per provider.
    return 1.0 if "ddr5" in mem else 0.6 if "ddr4" in mem else 0.75


def bid_usd_hr(price: Any) -> float | None:
    """$/h of a bid's price, or None when the bid can never be chosen: a denom other than uact, or an amount that is not a finite number
    above zero.  The ONE price check of every bid ranking (the dev box / workers here, `ddc job`): a NaN or negative price must not reach the
    ledger, where it would blind the budget guard."""
    price = price if isinstance(price, Mapping) else {}
    if price.get("denom", "uact") != "uact":
        return None
    try:
        amount = float(price["amount"])
    except (KeyError, TypeError, ValueError):
        return None
    return usd_hr_from_uact_block(amount) if math.isfinite(amount) and amount > 0 else None


def rank_bids(bids: list[dict[str, Any]], pinfo: dict[str, dict[str, Any]], perf: dict[str, Any],
              counts: dict[str, int], min_uptime: float, max_per_provider: int, t: float | None = None,
              max_usd_hr: float | None = None, only: Collection[str] | None = None, audited_only: bool = False) -> list[dict[str, Any]]:
    """Acceptable open bids, best value first. A bid above the profile's price cap or not priced in uact is never acceptable
    (the SDL cap should already prevent it; this is the second check).  `only` (provider addresses chosen by the user) and `audited_only`
    can only REMOVE bids, after every other rule: a pin never lets through a provider the uptime, lease-count or bad-provider rules reject."""
    t = t or now()
    out = []
    for b in bids:
        bid = b.get("bid") or {}
        prov = (bid.get("id") or {}).get("provider")
        if bid.get("state") != "open" or not prov:
            continue
        if only and prov not in only:
            continue
        if audited_only and not (pinfo.get(prov) or {}).get("isAudited"):   # unknown provider = not audited
            continue
        p = pinfo.get(prov) or {}
        if p and float(p.get("uptime7d") or 0) < min_uptime:
            continue
        if counts.get(prov, 0) >= max_per_provider or float((perf.get(prov) or {}).get("bad_until") or 0) > t:
            continue
        usd_hr = bid_usd_hr(bid.get("price"))
        if usd_hr is None:   # not priced in uact, or without a real price (nan, inf, 0, negative): never chosen
            continue
        if max_usd_hr is not None and usd_hr > max_usd_hr * 1.001:
            continue
        f = speed_factor(prov, p, perf)
        out.append({"score": usd_hr / f, "usd_hr": usd_hr, "speed": f, "provider": prov, "host": (p.get("hostUri") or "").split("//")[-1].split(":")[0],
                    "mem": prov_memory(p), "id": bid["id"]})
    return sorted(out, key=lambda r: (r["score"], r["usd_hr"]))

# ---------------------------------------------------------------- SDL
def pubkey(name: str) -> str:
    try:
        return (KEYS / f"{name}.pub").read_text(encoding="utf-8").strip()
    except OSError:
        die(f"missing key {KEYS / (name + '.pub')}: run `ddc install`")

def gpu_sdl(pr: dict[str, Any]) -> str:
    """SDL `gpu:` block (8-space indent, inside resources) for a profile with gpu >= 1; empty otherwise."""
    return sdlgen.gpu_block(pr)   # strict: tokens are shape-checked, nothing typed can add a key (see sdlgen.py)

def render_sdl(role: str, profile: str) -> str:
    import base64
    bad = config_problems(profile=profile)  # image / profile fields go into YAML: validated, never escaped
    if bad:
        die("refusing to build the SDL: " + "; ".join(bad))
    if not re.fullmatch(r"[a-z]{1,12}", role):
        die(f"invalid role {role!r}")
    pr = CFG["profiles"][profile]
    keys = pubkey("access") + "\n" + (pubkey("worker") + "\n" if role == "worker" else "")
    gpu = gpu_sdl(pr)
    rep = {"__IMAGE__": CFG["image"], "__GPU__": gpu, "__PLACEMENT_ATTRS__": sdlgen.placement_block(pr), "__ROLE__": role, "__AUTHORIZED_KEYS_B64__": base64.b64encode(keys.encode()).decode(),
           "__CPU__": str(pr["cpu"]), "__MEM__": pr["mem"], "__DISK__": pr["disk"],
           "__MAX_UACT_PER_BLOCK__": str(uact_block_from_usd_hr(pr["max_usd_hr"]))}
    s = data_file("sdl.yaml.tmpl").read_text(encoding="utf-8")
    s = re.sub(r"^[ \t]*__GPU__\n", lambda m: rep.pop("__GPU__"), s, flags=re.M)
    s = re.sub(r"^[ \t]*__PLACEMENT_ATTRS__\n", lambda m: rep.pop("__PLACEMENT_ATTRS__"), s, flags=re.M)
    for k, v in rep.items():
        s = s.replace(k, v)
    if "__" in re.sub(r"#.*", "", s):
        die("unrendered SDL placeholder")
    try:
        sdlgen.check_sdl(s)   # exactly one integer price cap >= 1 uact per block, version line, no leftovers
    except sdlgen.SdlError as e:
        die(f"refusing to build the SDL: {e}")
    return s

# ---------------------------------------------------------------- ssh
def ssh_endpoint(d: dict[str, Any]) -> tuple[str, str]:
    """(host, port) of a lease, re-validated every time: both come from the provider and end up in ssh arguments, ssh config files
    and remote shell commands."""
    host, port = d.get("host"), d.get("port")
    if not valid_host(host) or not valid_port(port):
        die(f"refusing to use the ssh endpoint {host!r}:{port!r} of {d.get('name') or d.get('dseq') or 'a lease'}: not a plain hostname/IP and port")
    return str(host), str(int(port))


def ssh_args(d: dict[str, Any], user: str = "dev", tty: bool = False, known_hosts: Path | None = None) -> list[str]:
    """The one ssh option set (trust-on-first-use host keys via accept-new: a CHANGED key is refused; BatchMode unless tty). `known_hosts`
    lets `ddc job` keep one file per job, because a provider that reuses host:port for a new container would otherwise trip the shared file."""
    host, port = ssh_endpoint(d)
    a = [SSH, "-i", str(KEYS / "access"), "-p", port,
         "-o", f"UserKnownHostsFile={(known_hosts or STATE_DIR / 'known_hosts').as_posix()}", "-o", "StrictHostKeyChecking=accept-new",
         "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4", "-o", "ConnectTimeout=15", "-o", "IdentitiesOnly=yes"]
    a += ["-t"] if tty else ["-o", "BatchMode=yes"]
    return a + [f"{user}@{host}"]


def ssh(d: dict[str, Any], cmd: str, user: str = "dev", input: bytes | None = None, timeout: int = 600, check: bool = True) -> str:
    cp = subprocess.run(ssh_args(d, user) + [cmd], input=input, capture_output=True, timeout=timeout, creationflags=NOWIN)
    outp = cp.stdout.decode("utf-8", "replace")
    if check and cp.returncode != 0:
        die(f"ssh {user}@{d['host']}: `{cmd[:80]}` exit {cp.returncode}: {cp.stderr.decode('utf-8', 'replace')[-800:]}{outp[-400:]}")
    return outp


def put_file(d: dict[str, Any], path: str, data: bytes, user: str = "dev", mode: str = "600") -> None:
    if not re.fullmatch(r"[0-7]{3,4}", mode):
        die(f"invalid file mode {mode!r}")
    q = shlex.quote(path)
    ssh(d, f"umask 077; mkdir -p \"$(dirname {q})\" && cat > {q}.tmp && chmod {mode} {q}.tmp && mv {q}.tmp {q}", user=user, input=data)


def touch_worker_activity(d: dict[str, Any]) -> None:
    """Tell the dev box's idle tracker that jobs are being dispatched to workers (best effort), so they are not closed as idle."""
    try:
        ssh(d, "touch /run/ddc/worker_activity 2>/dev/null; true", timeout=30, check=False)
    except Exception as e:  # never let a bookkeeping touch break a dispatch
        log(f"warning: could not mark worker activity: {e}")

# ---------------------------------------------------------------- deployments
def list_account_deployments() -> list[dict[str, str]]:
    """Active deployments on the Akash account, ours or not (read-only): [{dseq, name, owner}]."""
    out: list[dict[str, str]] = []
    for skip in range(0, 1000, 100):
        rows = ((api("GET", f"/v1/deployments?skip={skip}&limit=100&state=active") or {}).get("data") or {}).get("deployments") or []
        for x in rows:
            dep = x.get("deployment") or {}
            dseq = str((dep.get("id") or {}).get("dseq") or x.get("dseq") or "")
            if dseq:
                out.append({"dseq": dseq, "name": str(x.get("name") or dep.get("name") or ""), "owner": str((dep.get("id") or {}).get("owner") or "")})
        if len(rows) < 100:
            break
    return out


def find_deployment_named(name: str, tries: int = 3) -> str | None:
    for i in range(tries):
        for x in list_account_deployments():
            if x["name"] == name:
                return x["dseq"]
        if i < tries - 1:
            time.sleep(5)
    return None


def _adopt_pending(name: str, dseq: str, status: str = "created") -> None:
    """The deployment `name` exists as `dseq`: record it as ours (atomically replacing its write-ahead record)."""
    def fn(st: dict[str, Any]) -> None:
        rec = (st.get("pending") or {}).pop(name, {})
        st["deps"].setdefault(dseq, {}).update(ours=True, name=name, role=rec.get("role", "box"), idx=rec.get("idx", 0), profile=rec.get("profile"),
                                               status=status, created_at=rec.get("created_at") or now(), limit_h=rec.get("limit_h"),
                                               max_usd_hr=rec.get("max_usd_hr"))
    update_state(fn)
    ledger_add({"event": "created", "dseq": dseq, "name": name, "adopted": True})


def _post_deployment(name: str, sdl: str, limit_h: int) -> dict[str, Any]:
    """POST /v1/deployments exactly once. If the outcome is unknown (timeout, 5xx, unreadable reply) the deployment is looked up by its
    unique name instead of being created a second time."""
    try:
        created = api("POST", "/v1/deployments", {"data": {"sdl": sdl, "name": name, "runtimeLimitHours": limit_h}})["data"]
        str(created["dseq"])
        created["manifest"]
        return created
    except ApiUncertain as e:
        err: Exception = e
    except (KeyError, TypeError) as e:  # a reply we cannot read may still have created the deployment
        err = e
    except DdcError:  # definitively rejected (HTTP 4xx, no API key): nothing was created
        update_state(lambda st: (st.get("pending") or {}).pop(name, None))
        raise
    log(f"{name}: creation outcome unknown ({err}); NOT retrying, looking the deployment up by name")
    try:
        dseq = find_deployment_named(name)
    except DdcError as e2:
        raise OutcomeUnknown(f"{name}: creation outcome unknown ({err}) and the deployment list failed ({e2}). Recorded as pending and NOT "
                             "retried (it would risk a duplicate). Run `ddc reconcile` once the API answers.") from err
    if dseq is None:
        raise OutcomeUnknown(f"{name}: creation outcome unknown ({err}) and it is not on the account (yet). Recorded as pending and counted in "
                             "the budget; NOT retried. `ddc reconcile` (or the watchdog) resolves it.") from err
    _adopt_pending(name, dseq)
    try:  # the reply carried the manifest the lease needs: without it this deployment is useless, so close it
        close_box(dseq)
    except Exception as ce:
        raise RollbackFailed(f"{name}: dseq {dseq} was created but its reply was lost ({err}) and closing it failed ({ce}): it may be billing, run `ddc stop`") from err
    die(f"{name}: the create reply was lost ({err}); the deployment dseq {dseq} was found and closed again (nothing was duplicated). Run the command again.")


def launch_lease(name: str, sdl: str, *, role: str, limit_h: int, cap_usd_hr: float, label: str, profile: str | None = None, idx: int = 0,
                 fields: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The ONE place a deployment is created (create_box for dev/worker/box leases, `ddc job` for role "job"): check_spend (config, lease caps
    incl. pending creations, hard ceilings, budget guard) and the write-ahead `pending` record are one step under _create_lock, then
    POST /v1/deployments exactly once (an unknown outcome is looked up by name, never repeated, see _post_deployment), then the record is
    promoted to its dseq as ours.  `fields` are extra state keys (a job's name, image ...).  Returns the create reply ({dseq, manifest}).
    Every new lease, whoever asks for it, therefore costs against the same budget and the same ceilings, and always carries runtimeLimitHours."""
    if not name.startswith(NAME_PREFIX):
        die(f"refusing to create a deployment named {name!r}: ddc leases are named {NAME_PREFIX}*")
    if not _int(limit_h) or limit_h < 1:
        die(f"invalid runtime limit {limit_h!r} h (a whole number of at least 1: runtimeLimitHours is always sent)")
    extra = dict(fields or {})
    if {"ours", "name", "role", "idx", "profile", "status", "created_at", "limit_h", "max_usd_hr"} & set(extra):
        die("launch_lease: `fields` cannot override the keys the controller owns")
    with _create_lock:
        check_spend(label, 1, limit_h, cap_usd_hr, role, profile=profile)
        try:   # defence in depth: the price the order really carries is the one that was just checked against the budget (and exactly one instance)
            ordered = sdlgen.single_price(sdl)
        except sdlgen.SdlError as e:
            die(f"refusing to create {name}: {e}")
        if usd_hr_from_uact_block(ordered) > cap_usd_hr * 1.001:
            die(f"refusing to create {name}: the SDL orders at {ordered} uact/block = ${usd_hr_from_uact_block(ordered):.4f}/h, above the ${cap_usd_hr:g}/h cap the budget check used")
        _approval_reserve(name, cap_usd_hr * limit_h)   # an unknown create outcome stays reserved: it may be billing
        update_state(lambda st: st.setdefault("pending", {}).__setitem__(
            name, {"role": role, "profile": profile, "idx": idx, "limit_h": limit_h, "max_usd_hr": cap_usd_hr, "created_at": now(), **extra}))
    created = _post_deployment(name, sdl, limit_h)
    dseq = str(created["dseq"])

    def promote(st: dict[str, Any]) -> None:
        st["deps"].setdefault(dseq, {}).update(ours=True, name=name, role=role, idx=idx, profile=profile, status="created", created_at=now(),
                                               limit_h=limit_h, max_usd_hr=cap_usd_hr, **extra)
        (st.get("pending") or {}).pop(name, None)
    update_state(promote)
    _approval_rekey(name, dseq)
    ledger_add({"event": "created", "dseq": dseq, "name": name, "role": role, "profile": profile})
    return created


def create_box(role: str, profile: str, idx: int = 0, counts: dict[str, int] | None = None, explore: bool = False,
               limit_h: int | None = None) -> dict[str, Any]:
    """The ONE place leases are created (start, scale, heal, TUI and remote calls all end up here). Order: validate config, preflight the
    local tools/keys/paths, render the SDL, enforce limits + budget, write a pending record (write-ahead), POST once, then promote the
    record to its dseq. Any failure after the POST closes only the lease this call created."""
    require_config(profile)
    pr = CFG["profiles"][profile]
    if limit_h is not None and not (_num(limit_h) and limit_h >= 1):
        die(f"invalid runtime limit {limit_h!r} h (at least 1)")
    limit_h = int(limit_h or (CFG["max_runtime_hours"] if role == "dev" else CFG["worker_runtime_hours"]))
    bad = preflight(role)
    if bad:
        die("preflight failed, nothing was created:\n  - " + "\n  - ".join(bad))
    tag = f"w{idx:02d}" if role == "worker" else role
    name = f"{NAME_PREFIX}{tag}-{profile}-{dt.datetime.now().strftime('%m%d%H%M')}-{pysecrets.token_hex(2)}"  # unique: reconcile finds it by name
    sdl = render_sdl(role, profile)
    created = launch_lease(name, sdl, role=role, limit_h=limit_h, cap_usd_hr=pr["max_usd_hr"], label=profile_label(profile), profile=profile, idx=idx)
    dseq = str(created["dseq"])
    log(f"{name}: created dseq={dseq}, waiting for bids")
    try:
        ranked: list[dict[str, Any]] = []
        pins, audited = sdlgen.pins(pr), pr.get("audited_only") is True   # the user's provider choice: bid filters only (validated with the profile)
        for _ in range(30):
            time.sleep(4)
            bids = api("GET", "/v1/bids?" + urllib.parse.urlencode({"dseq": dseq})).get("data") or []
            perf = read_json(PERF, {})
            ranked = rank_bids(bids, providers(), perf, counts or {}, CFG["min_uptime_7d"], 1 if explore else CFG["max_leases_per_provider"],
                               max_usd_hr=float(pr["max_usd_hr"]), only=pins, audited_only=audited)
            if explore:
                ranked = [r for r in ranked if not (perf.get(r["provider"]) or {}).get("single_score")]
            if len(ranked) >= 3 or (ranked and _ > 10):
                break
        if not ranked:
            die("no acceptable bids" + (f" (only bids from the {len(pins)} chosen provider(s) are accepted)" if pins else "") + (" (audited providers only)" if audited else ""))
        chosen = ranked[0]
        update_dep(dseq, bids=[{k: r[k] for k in ("provider", "host", "usd_hr", "speed")} for r in ranked[:5]])   # the real, confirmed prices (state only)
        if counts is not None:
            counts[chosen["provider"]] = counts.get(chosen["provider"], 0) + 1
        log(f"{name}: {len(ranked)} acceptable bids; chose {chosen['host'] or chosen['provider']} ${chosen['usd_hr']:.3f}/h "
            f"(speed {chosen['speed']:.2f}, {chosen['mem'] or 'mem?'}); next: " +
            ", ".join(f"${r['usd_hr']:.3f}" for r in ranked[1:4]))
        bid_id = chosen["id"]
        lease_body = {"manifest": created["manifest"], "leases": [{"dseq": dseq, "gseq": bid_id["gseq"], "oseq": bid_id["oseq"], "provider": bid_id["provider"]}]}
        for attempt in range(3):
            try:
                api("POST", "/v1/leases", lease_body, timeout=120)
                break
            except ApiUncertain:
                if deployment(dseq).get("leases"):  # it went through
                    break
                raise
            except DdcError as e:
                if "manifest_not_delivered" not in str(e) or attempt == 2:
                    raise
                time.sleep(10)
        update_dep(dseq, status="leased", provider=chosen["provider"], provider_host=chosen["host"], usd_hr=chosen["usd_hr"], lease_at=now())
        ledger_add({"event": "leased", "dseq": dseq, "provider": chosen["provider"], "usd_hr": chosen["usd_hr"]})
        host = port = None
        owner = ""
        for _ in range(60):
            det = deployment(dseq)
            owner = owner or _det_owner(det)
            h, p = _det_endpoint(det)
            if h:
                host, port = h, p
                break
            time.sleep(5)
        if not host:
            die("lease never exposed ssh port")
        if not valid_host(host) or not valid_port(port):
            die(f"the provider returned an unusable ssh endpoint {host!r}:{port!r}")
        # a brand-new container: forget a key left by an earlier lease on the same reused host:port, or accept-new refuses it as CHANGED
        subprocess.run([SSH_KEYGEN, "-f", str(STATE_DIR / "known_hosts"), "-R", f"[{host}]:{int(port)}"], capture_output=True, creationflags=NOWIN)
        update_dep(dseq, host=host, port=int(port), **({"owner": owner} if owner else {}))
        d = load_state()["deps"][dseq]
        for _ in range(60):
            try:
                if "ok" in ssh(d, "test -f /run/ddc-role && echo ok", user="root", timeout=40, check=False):
                    break
            except subprocess.TimeoutExpired:
                pass
            time.sleep(8)
        else:
            die("ssh never came up")
        update_dep(dseq, status="ready")
        log(f"{name}: ssh ready at {host}:{port}")
        return load_state()["deps"][dseq] | {"dseq": dseq}
    except BaseException as e:
        log(f"{name}: provisioning failed ({e}); closing our dseq {dseq}")
        try:
            perf = read_json(PERF, {})
            prov = load_state()["deps"][dseq].get("provider")
            if prov:
                perf.setdefault(prov, {})["bad_until"] = now() + 24 * 3600
                write_json(PERF, perf)
        except Exception:
            pass
        try:
            close_box(dseq)
        except Exception as ce:
            if isinstance(e, Exception):
                raise RollbackFailed(f"{name}: provisioning failed ({e}) AND closing dseq {dseq} failed ({ce}): it may still be billing, run `ddc stop`") from e
            log(f"{name}: could not close dseq {dseq} after the interrupt ({ce}): it may still be billing, run `ddc stop`")
        raise


def assert_ours(dseq: str, det: dict[str, Any] | None, listed: dict[str, Any] | None = None) -> None:
    """A lease may only be closed when ALL hold: it is recorded in our state as ours, its name (the console's when we have it, else our
    record's) starts with ddc-, and, when the API reports an owner, it is the account our other leases belong to.
    `listed` (state.json damaged, see fallback_deps) stands in for the state record: it comes from the read-only account listing."""
    st = {"deps": {}} if listed is not None else load_state()
    rec = listed if listed is not None else st["deps"].get(dseq)
    if not rec or not rec.get("ours"):
        die(f"refusing to close dseq {dseq}: not created by ddc")
    rec_name = str(rec.get("name") or "")
    if rec_name and not rec_name.startswith(NAME_PREFIX):
        die(f"refusing to close dseq {dseq}: recorded name {rec_name!r} is not ours")
    if det:
        api_name = det.get("name")
        if not str(api_name or "").startswith(NAME_PREFIX):
            die(f"refusing to close dseq {dseq}: console name {api_name!r} is not ours")
    elif not rec_name:
        die(f"refusing to close dseq {dseq}: the API returned no details and the record has no name to verify")
    owner = _det_owner(det)
    if owner:
        owners = {str(r["owner"]) for r in [*st["deps"].values(), *([listed] if listed else [])] if r.get("owner")}
        if owners and owner not in owners:
            die(f"refusing to close dseq {dseq}: it belongs to account {owner}, not to ours ({', '.join(sorted(owners))})")


def close_box(dseq: str, listed: dict[str, Any] | None = None) -> float:
    """DELETE our lease and record it closed, but only after the platform confirmed it: an explicit closed state, or (when the
    deployment cannot be read) a well-formed spend-rate that no longer lists it. An empty or odd answer never counts as closed: the
    DELETE is sent again instead. `listed` = state.json is damaged and the record comes from the read-only account listing: nothing
    is written to the state file (only the ledger)."""
    status, det = deployment_status(dseq)
    assert_ours(dseq, det, listed)
    confirmed = status == "closed"
    for _attempt in range(3):
        if confirmed:
            break
        try:
            api("DELETE", f"/v1/deployments/{dseq}", timeout=120, ok404=True)
        except ApiUncertain as e:  # may or may not have gone through: look at the state, only then send it again
            log(f"DELETE dseq {dseq}: {e}; checking the state before anything else")
        for _ in range(30):
            status, det = deployment_status(dseq)
            if status == "closed":
                confirmed = True
                break
            if status == "unknown":  # cannot read the deployment: only a well-formed spend-rate without it proves billing stopped
                billing = billing_dseqs()
                if billing is not None and dseq not in billing:
                    confirmed = True
                    break
            time.sleep(4)
        if confirmed:
            break
    if not confirmed:
        die(f"dseq {dseq} is not confirmed closed (the API did not report it closed): it may still be billing, check `ddc status`")
    rec = listed if listed is not None else load_state()["deps"][dseq]
    if listed is None and rec.get("status") == "closed":  # another process (watchdog, runner, stop) booked this close while we waited: never book a lease twice
        log(f"{rec.get('name')} dseq={dseq}: already recorded as closed by another ddc process")
        _approval_closed(dseq, float(rec.get("cost_usd") or 0))
        return float(rec.get("cost_usd") or 0)
    transferred = _transferred_usd(det)
    est, _ = dep_running_cost(rec)
    cost = max(transferred, est)  # escrow settles lazily: never book less than the running estimate
    for attempt in range(10):  # spend-rate can lag a few seconds behind a close; only a persistent listing is an error
        billing = billing_dseqs()
        if billing is None or dseq not in billing:  # unreadable here is fine: the confirmation above already came from the platform
            break
        if attempt == 9:
            die(f"dseq {dseq} closed but still listed in spend-rate")
        time.sleep(4)
    if listed is None:
        update_dep(dseq, status="closed", closed_at=now(), cost_usd=cost)
    ledger_add({"event": "closed", "dseq": dseq, "cost_usd": round(cost, 4), "transferred_usd": round(transferred, 4), **({"by": "fallback-listing"} if listed is not None else {})})
    log(f"closed {rec.get('name')} dseq={dseq}: confirmed closed, billing stopped, cost ${cost:.3f}")
    _approval_closed(dseq, cost)
    return cost


def refresh_live() -> None:
    """Sync our state with the API: platform-closed (runtime limit) deployments, escrow spend. An empty/404/error answer is UNKNOWN:
    the lease stays marked live (billing is never assumed to have stopped); only an explicit closed state closes the record."""
    for dseq, rec in active_deps().items():
        status, det = deployment_status(dseq)
        tr = _transferred_usd(det)
        if status == "closed":
            est, _ = dep_running_cost(rec)
            cost = max(tr, est)
            update_dep(dseq, status="closed", closed_at=now(), cost_usd=cost)
            ledger_add({"event": "closed", "dseq": dseq, "cost_usd": round(cost, 4), "transferred_usd": round(tr, 4), "by": "platform"})
            log(f"{rec.get('name')} dseq={dseq} was closed by the platform (runtime limit or provider)")
        elif status == "open":
            update_dep(dseq, transferred_usd=tr)
        else:
            warn_once(f"unknown-{dseq}", f"warning: {rec.get('name')} dseq={dseq}: no usable answer from the API; keeping it marked live (still counted as billing)")
            start = float(rec.get("lease_at") or rec.get("created_at") or now())
            if now() - start > (float(rec.get("limit_h") or CFG["max_runtime_hours"]) + 0.5) * 3600:  # the platform hard stop has passed
                billing = billing_dseqs()
                if billing is not None and dseq not in billing:
                    est, _ = dep_running_cost(rec)
                    update_dep(dseq, status="closed", closed_at=now(), cost_usd=est)
                    ledger_add({"event": "closed", "dseq": dseq, "cost_usd": round(est, 4), "transferred_usd": 0, "by": "expired"})
                    log(f"{rec.get('name')} dseq={dseq}: past its runtime limit and no longer billed: recorded as closed")


def reconcile_pending() -> list[str]:
    """Resolve write-ahead records of creations whose outcome was unknown, using only read-only listing: a deployment found by its
    unique name is recorded as ours and closed again (its manifest is lost); an old record that matches nothing is dropped."""
    pend = pending_creations()
    if not pend:
        return []
    by_name = {x["name"]: x["dseq"] for x in list_account_deployments()}  # raises if the API is down: records are kept
    msgs = []
    for name, rec in pend.items():
        if name in by_name:
            dseq = by_name[name]
            _adopt_pending(name, dseq)
            msgs.append(f"pending {name} was created as dseq {dseq}: recorded as ours")
            try:
                close_box(dseq)
                msgs.append(f"closed {dseq} (its create reply, with the manifest, was lost)")
            except DdcError as e:
                msgs.append(f"could not close {dseq} yet ({e}); it is recorded and the watchdog retries")
        elif now() - float(rec.get("created_at") or 0) > PENDING_GRACE_S:
            update_state(lambda st, n=name: (st.get("pending") or {}).pop(n, None))
            msgs.append(f"pending {name} matches no deployment after {PENDING_GRACE_S // 60} min: dropped")
    for m in msgs:
        log(m)
    return msgs


def dev_box() -> dict[str, Any] | None:
    for dseq, d in active_deps().items():
        if d.get("role") == "dev" and d.get("status") == "ready":
            return d | {"dseq": dseq}
    return None


def workers() -> list[dict[str, Any]]:
    return sorted([d | {"dseq": k} for k, d in active_deps().items() if d.get("role") == "worker" and d.get("status") == "ready"],
                  key=lambda d: d.get("idx", 0))

# ---------------------------------------------------------------- workspace seed (PC -> lease)
DENY_NAME = re.compile(r"(^|/)(\.env(\.(?!example$|sample$|template$)[^/]*)?|id_(rsa|ed25519|ecdsa)(\.pub)?|[^/]*\.(pem|key|p12|pfx|keystore)|[^/]*keypair[^/]*\.json|wallets?/[^/]*\.json)$", re.I)
SECRET_CONTENT = [re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
                  re.compile(rb"^\s*\[\s*(\d{1,3}\s*,\s*){63}\d{1,3}\s*\]\s*$", re.S),  # crypto wallet keypair JSON (64-byte array)
                  re.compile(rb"\b(ghp|gho|ghs|github_pat)_[A-Za-z0-9_]{30,}"),
                  re.compile(rb"\bsk-ant-[A-Za-z0-9_-]{20,}"),
                  re.compile(rb"\bac\.sk\.[A-Za-z0-9._-]{20,}")]
SKIP_DIRS = ("node_modules/", ".worktrees/", "dist/", "coverage/", ".stryker-tmp/", "reports/")


def secret_hits(rel: str, data: bytes, known: list[bytes]) -> list[str]:
    hits = []
    if DENY_NAME.search(rel):
        hits.append("name")
    if any(p.search(data) for p in SECRET_CONTENT):
        hits.append("content")
    if any(k in data for k in known):
        hits.append("known-secret")
    return hits


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True, creationflags=NOWIN).stdout.decode("utf-8", "replace")


def pack_repo(path: Path, out: Path) -> dict[str, Any]:
    """Ship .git + the files git reports as modified/untracked (not ignored). Clean tracked files are
    NOT shipped: the lease regenerates them with `git checkout-index` (LF, fresh index stat), so the lease
    tree is byte-identical to what git stores and `git status` matches the PC. Modified/untracked text
    files get CRLF->LF where git would store LF. Refuses secrets in uncommitted files."""
    known = [v.encode() for k, v in load_secrets().items() if len(v) >= 12]
    eol = {}
    for line in git(path, "ls-files", "--eol", "-co", "--exclude-standard", "-z").split("\0"):
        if "\t" in line:
            meta, rel = line.split("\t", 1)
            eol[rel] = meta.split()
    changed = set(git(path, "ls-files", "-mo", "--exclude-standard", "-z").split("\0")) - {""}
    tracked = set(git(path, "ls-files", "-z").split("\0")) - {""}
    clean = sorted(r for r in tracked - changed if not r.startswith(SKIP_DIRS))
    files = sorted(r for r in changed if not r.startswith(SKIP_DIRS))
    refused, warned, converted = [], [], 0
    for rel in clean:  # committed content is already on GitHub; still flag it for the owner
        fp = path / rel
        if fp.is_file() and fp.stat().st_size < 5_000_000 and secret_hits(rel, fp.read_bytes(), known):
            warned.append(rel)
    with tarfile.open(out, "w:gz", compresslevel=3) as tf:
        for rel in files:
            fp = path / rel
            if not fp.is_file():
                continue
            data = fp.read_bytes()
            hits = secret_hits(rel, data, known)
            if hits:
                refused.append(f"{rel} ({','.join(hits)})")
                continue
            meta = eol.get(rel, [])
            if len(meta) >= 2 and meta[1] == "w/crlf" and meta[0] in ("i/lf", "i/") and "eol=crlf" not in " ".join(meta[2:]):
                data = data.replace(b"\r\n", b"\n")
                converted += 1
            ti = tarfile.TarInfo(rel)
            ti.size, ti.mtime, ti.mode = len(data), int(fp.stat().st_mtime), 0o755 if data.startswith(b"#!") else 0o644
            tf.addfile(ti, io.BytesIO(data))
        # The Windows index holds CRLF sizes/stat data; the lease rebuilds it from HEAD and then replays
        # exactly the entries that are staged on the PC (blob ids, or removal for staged deletions).
        staged = [x for x in git(path, "diff", "--cached", "--name-only", "-z").split("\0") if x]
        entries = {}
        for e in (git(path, "ls-files", "-s", "-z", "--", *staged).split("\0") if staged else []):
            if "\t" in e:
                entries[e.split("\t", 1)[1]] = e
        info_lines = "".join((entries.get(x) or f"0 {'0' * 40}\t{x}") + "\0" for x in staged)
        for name, payload in ((".git/ddc-clean-files", "\0".join(clean)), (".git/ddc-index-info", info_lines)):
            b = payload.encode()
            ti = tarfile.TarInfo(name)
            ti.size = len(b)
            tf.addfile(ti, io.BytesIO(b))
        gitdir = path / ".git"
        if gitdir.is_file():
            die(f"{path} is a linked worktree; point config at the main checkout")
        cfg_text = (gitdir / "config").read_bytes()
        if any(p.search(cfg_text) for p in SECRET_CONTENT[2:]):
            die(f"{path}/.git/config contains a token; remove it before seeding")
        for fp in gitdir.rglob("*"):
            rel = fp.relative_to(path).as_posix()
            if fp.is_file() and not rel.startswith((".git/worktrees/", ".git/lfs/")) and not rel.endswith(".lock"):
                tf.add(fp, rel, recursive=False)
    if refused:
        die(f"refusing to seed {path}: possible secrets in uncommitted files: {refused[:10]}")
    pc_status = sorted(l for l in git(path, "status", "--porcelain").splitlines() if not l[3:].startswith(".worktrees"))
    return {"files": len(files) + len(clean), "shipped": len(files), "crlf_to_lf": converted, "warned_tracked": warned,
            "pc_dirty": len(pc_status), "pc_status_sha": hashlib.sha256(("\n".join(pc_status) + "\n" if pc_status else "").encode()).hexdigest(),
            "head": git(path, "rev-parse", "HEAD").strip(), "branch": git(path, "branch", "--show-current").strip()}


def pack_dir(path: Path, out: Path, exclude: list[str]) -> int:
    n = 0
    known = [v.encode() for k, v in load_secrets().items() if len(v) >= 12]
    with tarfile.open(out, "w:gz", compresslevel=3) as tf:
        for fp in path.rglob("*"):
            rel = fp.relative_to(path).as_posix()
            if fp.is_file() and not any(x in rel.split("/") for x in [*exclude, "node_modules", ".cache"]):
                if secret_hits(rel, fp.read_bytes(), known):
                    die(f"refusing to seed {fp}: looks like a secret")
                tf.add(fp, rel, recursive=False)
                n += 1
    return n


def seed(d: dict[str, Any], names: list[str]) -> None:
    for r in CFG.get("repos", []) + CFG.get("dirs", []):
        if r["name"] not in names:
            continue
        name = r["name"]
        if not NAME_RE.fullmatch(name):  # becomes a path under /work in remote shell commands
            die(f"refusing to seed {name!r}: a project name may only use letters, digits . _ -")
        if r.get("github") and not GITHUB_RE.fullmatch(str(r["github"])):
            die(f"refusing to seed {name}: github {r['github']!r} must look like OWNER/NAME")
        src = repo_path(r)
        w = shlex.quote(f"/work/{name}")
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "seed.tar.gz"
            if "github" in r or (src / ".git").exists():
                info = pack_repo(src, out)
            else:
                info = {"files": pack_dir(src, out, r.get("exclude", []))}
            mb = out.stat().st_size / 1e6
            log(f"seeding {name} from PC ({info['files']} files, {mb:.1f} MB)")
            # an existing directory (half-seeded, or cloud-only work that shares the name) is moved to /work/.ddc/trash, never deleted
            ssh(d, f"d={w}; if [ -e \"$d\" ]; then mkdir -p /work/.ddc/trash && mv \"$d\" /work/.ddc/trash/{shlex.quote(name)}-$(date +%s); fi"
                   f" && mkdir -p \"$d\" && tar -xzf - -C \"$d\"", input=out.read_bytes(), timeout=1800)
        if "head" in info:
            setup = ("git config core.autocrlf input && git worktree prune"
                     " && rm -f .git/index && git reset -q && git update-index -z --index-info < .git/ddc-index-info"
                     " && git checkout-index -f -z --stdin < .git/ddc-clean-files && rm .git/ddc-clean-files .git/ddc-index-info"
                     " && git update-index -q --refresh")
            if r.get("github") and r.get("push"):
                setup += " && git remote set-url origin " + shlex.quote("git@github.com:" + r["github"] + ".git")
            elif not r.get("push"):
                setup += " && git remote set-url --push origin DISABLED-no-push-from-cloud"
            got = ssh(d, f"cd {w} && {{ {setup}; }} >/dev/null; git rev-parse HEAD && git status --porcelain | wc -l"
                         f" && git status --porcelain | LC_ALL=C sort | sha256sum").split()
            log(f"  {name}: lease HEAD {got[0][:10]} (PC {info['head'][:10]}), dirty {got[1]} (PC {info['pc_dirty']}), "
                f"git status {'identical to PC' if got[2] == info['pc_status_sha'] else 'DIFFERS from PC'}, CRLF->LF {info['crlf_to_lf']}")
            if got[0] != info["head"] or int(got[1]) != info["pc_dirty"]:
                die(f"seed verification failed for {name}: HEAD or dirty count differs from the PC")
        ssh(d, f"mkdir -p /work/.ddc/complete && touch {shlex.quote(f'/work/.ddc/complete/{name}')}")
        if info.get("warned_tracked"):
            log(f"  note: secret-like committed files shipped as-is (already in git history): {info['warned_tracked'][:5]}")

def fix_push_remotes(d: dict[str, Any]) -> None:
    """Point origin of every push=true repo at its configured `github` (the URL seed() sets). A repo restored from a checkpoint keeps the
    origin in its archived .git/config, which may name an old owner (a repository that moved). Idempotent; a repo that is not there is skipped."""
    cmds = []
    for r in CFG.get("repos", []):
        if r.get("github") and r.get("push") and NAME_RE.fullmatch(r["name"]) and GITHUB_RE.fullmatch(str(r["github"])):
            url = shlex.quote("git@github.com:" + r["github"] + ".git")
            w = shlex.quote(f"/work/{r['name']}")
            cmds.append(f"if [ -d {w}/.git ]; then git -C {w} remote set-url origin {url} 2>/dev/null || git -C {w} remote add origin {url}; fi")
    if cmds:
        ssh(d, " ; ".join(cmds) + " ; true")

# ---------------------------------------------------------------- dev box provisioning
def _env_line(k: str, v: Any) -> str:
    """One `export K="v"` line for the lease env file (sourced by the lease scripts, and grepped by setup.sh in exactly this
    double-quoted shape). `$`, backticks, quotes and backslashes are escaped so a config value can never run code on the lease."""
    s = str(v)
    if re.search(r"[\x00-\x1f\x7f]", s):
        die(f"refusing to put a control character into the lease env ({k})")
    return f'export {k}="' + re.sub(r'(["\\$`])', r"\\\1", s) + '"\n'


def lease_env(dseq: str) -> str:
    s = load_secrets()
    names = [r["name"] for r in CFG.get("repos", []) + CFG.get("dirs", [])]
    env: dict[str, Any] = {"DDC_STATE_REPO": state_repo(), "DDC_IDLE_MINUTES": CFG["idle_minutes"], "DDC_CHECKPOINT_MINUTES": CFG["checkpoint_minutes"],
                           "DDC_MAX_RUNTIME_HOURS": CFG["max_runtime_hours"], "DDC_WORKER_IDLE_MINUTES": CFG["worker_idle_minutes"],
                           "DDC_DSEQ": dseq, "DDC_REPOS": " ".join(names),
                           "DDC_INSTALL_CLAUDE_CODE": "1" if cfg_flag("install_claude_code", True) else "0"}
    if CFG.get("claude_code_version"):
        env["DDC_CLAUDE_CODE_VERSION"] = CFG["claude_code_version"]
    for key, name in (("busy_regex", "DDC_BUSY_REGEX"), ("bench_cmd", "DDC_BENCH_CMD"), ("bench_dir", "DDC_BENCH_DIR")):
        if CFG.get(key):
            env[name] = CFG[key]
    sx = CFG.get("sync_excludes") or []
    sx = sx.split() if isinstance(sx, str) else sx
    if sx:
        env["DDC_SYNC_EXCLUDES"] = " ".join(sx)
    if os.environ.get("DDC_STATE_REMOTE"):  # tests: local bare repo instead of GitHub
        env["DDC_STATE_REMOTE"] = os.environ["DDC_STATE_REMOTE"]
    if push_claude_credentials() and s.get("CLAUDE_CODE_OAUTH_TOKEN"):
        env["CLAUDE_CODE_OAUTH_TOKEN"] = s["CLAUDE_CODE_OAUTH_TOKEN"]
    return "".join(_env_line(k, v) for k, v in env.items())


def push_claude_credentials() -> bool:
    """Opt-in (default off): copy the Claude login token / credentials file to the rented host, which can read them while it runs."""
    return cfg_flag("push_claude_credentials", False)

def push_lease_scripts(d: dict[str, Any]) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for fp in sorted(data_file("lease").iterdir()):
            data = fp.read_bytes().replace(b"\r\n", b"\n")
            ti = tarfile.TarInfo(fp.name)
            ti.size, ti.mode = len(data), 0o755
            tf.addfile(ti, io.BytesIO(data))
    ssh(d, "mkdir -p /opt/ddc && tar -xf - -C /opt/ddc", user="root", input=buf.getvalue())


def repo_deploy_keys() -> list[tuple[str, str]]:
    """(key file name in KEYS, GitHub owner/repo) for every push=true repo: the repo's `deploy_key` override, else
    deploy_<name>. A legacy `desk_deploy` key file keeps serving the first push repo (existing installs keep their key)."""
    out: list[tuple[str, str]] = []
    for r in CFG.get("repos", []):
        if not (r.get("push") and r.get("github")):
            continue
        k = r.get("deploy_key") or f"deploy_{r['name']}"
        if not r.get("deploy_key") and not out and not (KEYS / k).exists() and (KEYS / "desk_deploy").exists():
            k = "desk_deploy"
        if not re.fullmatch(r"[\w.-]+", k) or k.startswith("."):
            die(f"{r['name']}: invalid deploy key name {k!r}")
        out.append((k, r["github"]))
    return out


def provision_dev(d: dict[str, Any]) -> None:
    s = load_secrets()
    push_lease_scripts(d)
    C = "/home/dev/.config/ddc"
    put_file(d, f"{C}/env", lease_env(d["dseq"]).encode(), user="root")
    put_file(d, f"{C}/backup.pass", s["DDC_BACKUP_PASSPHRASE"].encode(), user="root")
    rk = repo_deploy_keys()
    if len(rk) > 1:  # lease/setup.sh configures a single github.com identity (repo_deploy_key)
        log(f"warning: only {rk[0][1]} gets its deploy key on the dev box; pushing to {', '.join(r for _, r in rk[1:])} from there will fail")
    ship = [("worker", "worker")]  # (key file on this PC, name on the lease)
    if state_repo():
        ship.append(("state_deploy", "state_deploy"))
    if rk:
        ship.append((rk[0][0], "repo_deploy"))
    for k, lease_name in ship:
        if not (KEYS / k).exists():
            die(f"missing key {KEYS / k}: run `ddc install`")
        put_file(d, f"{C}/{lease_name}_key", (KEYS / k).read_bytes(), user="root")
    put_file(d, f"{C}/github_known_hosts", (KEYS / "github_known_hosts").read_bytes(), user="root")
    put_file(d, f"{C}/gitconfig", (KEYS / "gitconfig").read_bytes(), user="root")
    cred = SECRETS_DIR / "claude-credentials.json"
    if push_claude_credentials() and cred.exists():  # opt-in: the rented host can read it while the lease runs
        put_file(d, f"{C}/claude-credentials.json", cred.read_bytes(), user="root")
    log("dev box: installing tools" + (" + pinned Claude Code" if cfg_flag("install_claude_code", True) else ""))
    log("  " + ssh(d, "bash /opt/ddc/setup.sh dev", user="root", timeout=900).strip().splitlines()[-1])
    out = ssh(d, "test -f /work/.ddc-restored && echo already || /opt/ddc/restore.sh", timeout=1800).strip().splitlines()[-1]
    log(f"  restore: {out}")
    names = [r["name"] for r in CFG.get("repos", []) + CFG.get("dirs", [])]
    done = ssh(d, "ls /work/.ddc/complete 2>/dev/null", check=False).split()
    missing = [n for n in names if n not in done]  # includes half-seeded dirs from an interrupted start
    if missing:
        seed(d, missing)
    fix_push_remotes(d)
    log("dev box: npm ci in every repo")
    ssh(d, "for r in /work/*/; do [ -f $r/package-lock.json ] && [ ! -d $r/node_modules ] && (cd $r && npm ci --no-audit --no-fund --loglevel=error > /work/results/npm-ci-$(basename $r).log 2>&1 || echo FAIL $r) & done; wait", timeout=1800)
    start_session(d)


def start_session(d: dict[str, Any]) -> None:
    first = next((f"/work/{r['name']}" for r in CFG.get("repos", [])), "/work")
    ssh(d, f"""tmux has-session -t main 2>/dev/null || {{
      tmux new-session -d -s main -n claude -c {first} -x 220 -y 50
      tmux send-keys -t main:claude 'claude' Enter
      tmux new-window -d -t main -n workers 'watch -t -c -n 5 /opt/ddc/workers.sh'
      tmux new-window -d -t main -n shell -c /work
    }}""")


def sync_worker_hosts() -> None:
    d = dev_box()
    if not d:
        return
    conf = ""
    for w in workers():
        host, port = ssh_endpoint(w)  # provider-controlled: validated before it can reach the dev box's ssh config
        conf += (f"Host w{int(w['idx']):02d}\n  HostName {host}\n  Port {port}\n  User dev\n  IdentityFile ~/.config/ddc/worker_key\n"
                 f"  IdentitiesOnly yes\n  StrictHostKeyChecking accept-new\n  UserKnownHostsFile ~/.ssh/known_hosts_workers\n")
    put_file(d, "/home/dev/.ssh/config.d/workers", conf.encode(), mode="644")

def provision_worker(w: dict[str, Any]) -> None:
    push_lease_scripts(w)
    ssh(w, "bash /opt/ddc/setup.sh worker", user="root", timeout=600)


def _wants(rec: dict[str, Any], only: list[str] | None) -> bool:
    """--only filter: a dseq, a lease name, or wNN for a worker. No filter = everything."""
    if not only:
        return True
    tag = f"w{int(rec['idx']):02d}" if rec.get("role") == "worker" and rec.get("idx") is not None else ""
    return str(rec.get("dseq")) in only or rec.get("name") in only or (tag != "" and tag in only)


def cmd_heal(a: argparse.Namespace | None) -> list[str]:
    """Re-provision workers whose container was restarted by the provider (new host key, empty disk).
    The lease is still ours and billing, so we reuse it instead of closing it. --only limits it, --dry-run only reports."""
    d = dev_box() or die("no dev box")
    only = getattr(a, "only", None) or []
    dry = bool(getattr(a, "dry_run", False))
    healed = []
    for w in workers():
        if not _wants(w, only):
            continue
        host, port = ssh_endpoint(w)
        hostport = f"[{host}]:{port}"
        probe = subprocess.run(ssh_args(w, "root") + ["test -f /opt/ddc/setup.sh && echo ok"], capture_output=True, timeout=60, creationflags=NOWIN)
        out = probe.stdout.decode() + probe.stderr.decode()
        if "ok" in probe.stdout.decode():
            continue
        if "HOST IDENTIFICATION HAS CHANGED" in out or probe.returncode == 0:
            if dry:
                log(f"[dry-run] would re-provision w{w['idx']:02d} ({w['name']}): its container restarted")
                healed.append(f"w{w['idx']:02d}")
                continue
            # restart = new container; Akash re-ran the SDL boot script, so only our layers are missing
            subprocess.run([SSH_KEYGEN, "-f", str(STATE_DIR / "known_hosts"), "-R", hostport], capture_output=True, creationflags=NOWIN)
            ssh(d, f"ssh-keygen -f ~/.ssh/known_hosts_workers -R {shlex.quote(hostport)} >/dev/null 2>&1; true")
            provision_worker(w)
            healed.append(f"w{w['idx']:02d}")
            log(f"healed {w['name']} (container had restarted)")
        else:
            log(f"{w['name']} unreachable: {out.strip()[-200:]}")
    prep = str(CFG.get("worker_prep_command") or "").strip()
    if healed and prep and not dry:  # runs ON the dev box for ONLY the healed workers (a re-sync could overwrite work in progress on busy ones)
        touch_worker_activity(d)
        out = ssh(d, f"DIST_HOSTS={shlex.quote(' '.join(healed))} bash -c {shlex.quote(prep)} 2>&1 | tail -n 20", timeout=1800, check=False)
        log("worker_prep_command: " + (out.strip().splitlines() or ["done"])[-1])
    return healed

def msys(p: Path | str) -> str:
    """Git-for-Windows' gpg is an msys binary (<Git>\\usr\\bin\\gpg.exe): it needs /c/... paths. A native gpg (Gpg4win, scoop) does not."""
    s = Path(p).as_posix()
    g = Path(GPG)
    is_msys = WIN and g.parent.name.lower() == "bin" and g.parent.parent.name.lower() == "usr"
    return f"/{s[0].lower()}{s[2:]}" if is_msys and len(s) > 1 and s[1] == ":" else s

def rmtree(p: Path) -> None:
    """shutil.rmtree that also removes git's read-only object files on Windows."""
    def fix(fn, path, *_):
        os.chmod(path, 0o700)
        fn(path)
    if p.exists():
        if sys.version_info >= (3, 12):
            shutil.rmtree(p, onexc=fix)
        else:  # onexc is 3.12+; onerror has the same (func, path, <error>) shape
            shutil.rmtree(p, onerror=fix)


# ---------------------------------------------------------------- checkpoint verification (on this PC)
def verify_checkpoint(expect_commit: str | None = None, keep_local: bool = True) -> dict[str, Any]:
    """Clone the state repo with your own GitHub credentials, check every part's sha256,
    decrypt every archive here and compare entry counts with the manifest."""
    repo = need_state_repo()
    vdir = STATE_DIR / "verify"
    rmtree(vdir)
    try:  # never a credential prompt (nobody may be there to answer: the watchdog holds the command lock meanwhile), never unbounded
        cp = subprocess.run(["git", "clone", "-q", "--depth", "1", os.environ.get("DDC_STATE_URL", f"https://github.com/{repo}.git"), str(vdir)],
                            capture_output=True, creationflags=NOWIN, timeout=600,
                            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"})
    except subprocess.TimeoutExpired:
        die(f"cannot fetch state repo {repo}: git clone timed out after 600 s (checkpoint NOT verified)")
    if cp.returncode:
        die(f"cannot fetch state repo: {cp.stderr.decode('utf-8', 'replace')[-400:]}")
    commit = git(vdir, "rev-parse", "HEAD").strip()
    if expect_commit and commit != expect_commit:
        die(f"state repo HEAD {commit[:10]} != checkpoint {expect_commit[:10]}")
    m = json.loads((vdir / "manifest.json").read_text(encoding="utf-8"))
    pw = load_secrets()["DDC_BACKUP_PASSPHRASE"]
    results = {}
    with tempfile.TemporaryDirectory() as home:
        pwf = Path(home) / "p"
        pwf.write_text(pw, encoding="utf-8")
        for name, a in m["archives"].items():
            blob = b""
            for p in a["parts"]:
                data = (vdir / "archives" / p["file"]).read_bytes()
                if hashlib.sha256(data).hexdigest() != p["sha256"]:
                    die(f"checkpoint part {p['file']} sha256 mismatch")
                blob += data
            cp = subprocess.run([GPG, "--batch", "--quiet", "--homedir", msys(home), "--pinentry-mode", "loopback",
                                 "--passphrase-file", msys(pwf), "-d"], input=blob, capture_output=True, creationflags=NOWIN)
            if cp.returncode != 0:
                die(f"cannot decrypt {name}: {cp.stderr.decode('utf-8', 'replace')[:300]}")
            with tarfile.open(fileobj=io.BytesIO(cp.stdout), mode="r:gz") as tf:
                n = len(tf.getmembers())
            if n != a["entries"]:
                die(f"{name}: {n} entries decrypted, manifest says {a['entries']}")
            results[name] = n
    if keep_local:
        dest = STATE_DIR / "checkpoints" / m["created_at"].replace(":", "")
        if not dest.exists():
            shutil.copytree(vdir, dest, ignore=shutil.ignore_patterns(".git"))
        for old in sorted((STATE_DIR / "checkpoints").iterdir())[:-3]:
            rmtree(old)
    log(f"checkpoint {commit[:10]} ({m['created_at']}, {m['reason']}) verified on this PC: " + ", ".join(f"{k}={v}" for k, v in results.items()))
    return {"commit": commit, "manifest": m, "entries": results}


# ---------------------------------------------------------------- alerts
def _notify(msg: str) -> None:
    """Best-effort desktop/pluggable notification. Never raises: an alert path must not be able to break enforcement."""
    try:
        if WIN:
            subprocess.run(["msg", os.environ.get("USERNAME", "*"), "/TIME:3600", f"ddc: {msg}"], creationflags=NOWIN, capture_output=True, timeout=30)
        elif shutil.which("notify-send") and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            subprocess.run(["notify-send", "-u", "critical", "ddc", msg], capture_output=True, timeout=30)
        cmd = str(CFG.get("alert_command") or "").strip()  # e.g. a script that posts to a webhook; the message is its last argument
        if cmd:
            subprocess.run([*shlex.split(cmd, posix=not WIN), msg], capture_output=True, timeout=60, creationflags=NOWIN)
    except Exception as e:
        log(f"warning: alert notification failed: {e}")


def alert(msg: str) -> None:
    log("ALERT " + msg)
    try:
        with (STATE_DIR / "alerts.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": dt.datetime.now().isoformat(timespec="seconds"), "msg": msg}) + "\n")
    except OSError:
        pass
    _notify(msg)


def recent_alerts(n: int = 3, max_age_h: float = 24) -> list[str]:
    try:
        rows = [json.loads(x) for x in (STATE_DIR / "alerts.jsonl").read_text(encoding="utf-8").splitlines()[-50:] if x.strip()]
    except (OSError, ValueError):
        return []
    cut = dt.datetime.now() - dt.timedelta(hours=max_age_h)
    return [f"{r['at']} {r['msg']}" for r in rows if dt.datetime.fromisoformat(r["at"]) >= cut][-n:]


def wd_update(fn: Callable[[dict[str, Any]], Any]) -> Any:
    """Watchdog bookkeeping (consecutive failures, alert times, first-seen timestamps) lives in watchdog.json, not in state.json."""
    p = STATE_DIR / "watchdog.json"
    h = read_json(p, {})
    r = fn(h)
    write_json(p, h)
    return r


def alert_once(key: str, msg: str, every_s: float = 3600) -> None:
    """alert() at most once per `every_s` per key (the watchdog runs every 5 minutes); otherwise only a log line."""
    last = float((read_json(STATE_DIR / "watchdog.json", {}).get("alerts") or {}).get(key) or 0)
    if now() - last >= every_s:
        alert(msg)
        try:
            wd_update(lambda h: h.setdefault("alerts", {}).__setitem__(key, now()))
        except OSError as e:  # a failing bookkeeping file only costs throttling
            log(f"warning: cannot record the alert time ({e})")
    else:
        log("(already alerted) " + msg)

# ---------------------------------------------------------------- commands
class LimitExceeded(DdcError):
    """A spending / count / runtime limit refused a new lease."""


def _my_sid() -> str:
    """Windows: the current account's SID (ASCII, unlike a localised account name; USERNAME can be unset)."""
    out = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, check=True).stdout.decode("ascii", "replace")
    sid = out.strip().rsplit(",", 1)[-1].strip().strip('"')
    if not sid.startswith("S-1-"):
        raise DdcError(f"cannot read the current account's SID from whoami: {out.strip()!r}")
    return sid


def owner_only_cmd(path: Path) -> list[str]:
    """Windows: icacls that drops inherited ACEs and leaves only the current account (works the same in cmd and PowerShell)."""
    return ["icacls", str(path), "/inheritance:r", "/grant:r", f"*{_my_sid()}:F"]


def owner_only(path: Path) -> None:
    subprocess.run(owner_only_cmd(path), check=True, capture_output=True)


def key_problem(key: Path) -> str | None:
    """ssh refuses a private key that other accounts can read (on Windows: inherited ACLs, e.g. after copying secrets to a new PC) and
    then only times out against the box: load it locally first, for free, with the same OpenSSH rules."""
    try:
        p = subprocess.run([SSH_KEYGEN, "-y", "-P", "", "-f", str(key)], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"cannot check the ssh key {key} with {SSH_KEYGEN}: {e}"
    text = (p.stderr or p.stdout).strip()
    if p.returncode == 0 or "passphrase" in text.lower():  # permissions are checked before decryption: a protected key (ssh-agent) is fine
        return None
    why = (text.splitlines() or ["ssh-keygen could not load it"])[-1]
    try:
        fix = cmdline(owner_only_cmd(key)) if os.name == "nt" else f"chmod 600 {key}"
    except (OSError, subprocess.SubprocessError, DdcError):
        fix = f'icacls "{key}" /inheritance:r /grant:r "<your account>:F"'
    return f"ssh would refuse the key {key} ({why}); if its permissions are too open: {fix}"


def preflight(role: str = "dev") -> list[str]:
    """Everything that would otherwise only fail AFTER a lease exists and bills: local tools, keys, secrets, project paths."""
    out: list[str] = []
    tools = {"ssh": SSH, "git": "git"}
    if state_repo():
        tools["gpg"] = GPG  # verifies every checkpoint on this PC
    for label, exe in tools.items():
        if not shutil.which(exe):
            out.append(f"`{label}` was not found ({exe}): install it and put it on PATH" + (" (checkpoints are verified with it)" if label == "gpg" else ""))
    s = load_secrets()
    if not s.get("AKASH_API_KEY"):
        out.append(f"AKASH_API_KEY is missing in {SECRETS_DIR / 'secrets.env'}")
    keys = ["access.pub", "worker.pub"]
    if role == "dev":
        if not s.get("DDC_BACKUP_PASSPHRASE"):
            out.append(f"DDC_BACKUP_PASSPHRASE is missing in {SECRETS_DIR / 'secrets.env'}: run `ddc install`")
        keys += ["access", "worker", "github_known_hosts", "gitconfig"] + (["state_deploy"] if state_repo() else [])
        try:
            rk = repo_deploy_keys()
            keys += [rk[0][0]] if rk else []
        except DdcError as e:
            out.append(str(e))
    for k in keys:
        if not (KEYS / k).is_file():
            out.append(f"missing key file {KEYS / k}: run `ddc install`")
    if (KEYS / "access").is_file() and (bad := key_problem(KEYS / "access")):
        out.append(bad)
    if role == "dev":
        for r in CFG.get("repos", []) + CFG.get("dirs", []):
            try:
                repo_path(r)
            except DdcError as e:
                out.append(str(e))
    return out


def _require_global_config() -> None:
    """The config checks that do not belong to one profile (a lease that uses no profile, e.g. a `ddc job`, is not blocked by a broken profile)."""
    bad = [p for p in config_problems() if not p.startswith("profile ")]
    if bad:
        die("config problems (fix them in " + str(CONFIG_FILE or "your config.toml") + "):\n  - " + "\n  - ".join(bad))


def check_spend(label: str, n: int, hours: float, cap_usd_hr: float, role: str | None = None, *, profile: str | None = None) -> None:
    """THE gate for new leases of every kind: valid config, no damaged state or ledger, lease-count caps (pending creations count), hard
    ceilings (leases per call, runtime, price cap per lease; lifted only by `unsafe_disable_limits`), then the budget guard (worst case at
    the price cap).  `label` only names the order in log lines; `profile` limits the config check to one profile (None = global keys only).
    create_box reaches it through launch_lease, `check_budget` is the profile-shaped wrapper, `ddc job` uses it with role "job"."""
    require_config(profile) if profile is not None else _require_global_config()
    unsafe = limits_unsafe()
    if unsafe:
        log("WARNING: unsafe_disable_limits = true: the hard spending ceilings are OFF (budget_guard is a separate switch)")
    if not _int(n) or n < 1:
        die(f"cannot create {n!r} leases")
    if not _num(hours) or hours < 1:
        die(f"cannot create a lease with a {hours!r} h runtime limit (at least 1)")
    if not (_num(cap_usd_hr) and cap_usd_hr > 0) or uact_block_from_usd_hr(cap_usd_hr) < 1:
        die(f"cannot create a lease with a price cap of {cap_usd_hr!r} $/h (a positive number of at least ${usd_hr_from_uact_block(1):.4f}/h: the SDL price is a whole number of uact per block)")
    st = load_state()  # raises StateCorrupt: no new lease while the state file is damaged
    _, bad = ledger_read()
    if bad:
        raise LimitExceeded(f"{LEDGER} has unreadable lines {bad[:5]}: past spend may be under-counted, so no new lease is created. Repair or remove those lines")
    alive = [*active_deps(st).values(), *(st.get("pending") or {}).values()]
    n_workers = sum(1 for d in alive if d.get("role") == "worker")
    max_leases = int(CFG.get("max_total_leases", 4))
    max_workers = int(CFG.get("max_workers", 4))
    if not unsafe:
        max_leases, max_workers = min(max_leases, HARD_MAX_LEASES), min(max_workers, HARD_MAX_WORKERS)
        if n > HARD_MAX_SCALE:
            raise LimitExceeded(f"refusing to create {n} leases in one call (hard ceiling {HARD_MAX_SCALE})")
        if hours > HARD_MAX_RUNTIME_H:
            raise LimitExceeded(f"refusing a {hours} h runtime limit (hard ceiling {HARD_MAX_RUNTIME_H} h)")
        if cap_usd_hr > price_ceiling():
            raise LimitExceeded(f"refusing a price cap of ${cap_usd_hr:g}/h (hard ceiling ${price_ceiling():g}/h per lease: `max_price_usd_hr` in config.toml raises it, up to ${ABSOLUTE_MAX_USD_HR:g})")
    if len(alive) + n > max_leases:
        raise LimitExceeded(f"{len(alive)} lease(s) already alive or being created + {n} new > max_total_leases = {max_leases}")
    if role == "worker" and n_workers + n > max_workers:
        raise LimitExceeded(f"{n_workers} worker(s) already alive or being created + {n} new > max_workers = {max_workers}")
    if not CFG.get("budget_guard", True):
        log(f"budget guard OFF (budget_guard = false): launching {n}x{label}, worst case ${cap_usd_hr * n * hours:.2f} at the price cap")
        return
    ok, why = can_afford(budget_view(st), balance_usd(), cap_usd_hr * n, hours, float(CFG["budget_usd"]), float(CFG["reserve_usd"]))
    log(f"budget check for {n}x{label} x {hours} h at cap ${cap_usd_hr}/h: {why}")
    if not ok:
        raise LimitExceeded(why)


def default_profile() -> str:
    """`-p` default: "32" when the config has it (the shipped profiles), else the first profile, so a config with only custom profiles
    still runs `ddc start` / `ddc scale 0` without -p."""
    names = sorted(CFG.get("profiles") or {})
    return "32" if "32" in names or not names else names[0]


def profile_label(profile: str) -> str:
    """'32 vCPU' for the default profiles (named after their vCPU count), 'fast (12 vCPU)' for a custom one."""
    cpu = CFG["profiles"][profile].get("cpu")
    n = f"{cpu:g}" if isinstance(cpu, float) else str(cpu)
    return f"{profile} vCPU" if n == profile else f"{profile} ({n} vCPU)"


def check_budget(profile: str, n: int, hours: int, role: str | None = None) -> None:
    """check_spend for a config profile (start / scale / heal / CloudDeck / remote calls): the profile's price cap is the order's cap."""
    require_config(profile)
    check_spend(profile_label(profile), n, hours, CFG["profiles"][profile]["max_usd_hr"], role, profile=profile)


def check_worker_count(n: Any, with_dev: bool = False) -> None:
    """Refuse a worker count the limits would refuse later anyway (cmd_scale, check_budget): `start -w N` calls this BEFORE the dev box exists."""
    unsafe = limits_unsafe()
    if not _int(n) or n < 0 or (n > HARD_MAX_WORKERS and not unsafe):
        die(f"cannot scale to {n!r} workers (0..{HARD_MAX_WORKERS})")
    max_workers, max_leases = int(CFG.get("max_workers", 4)), int(CFG.get("max_total_leases", 4))
    if not unsafe:
        max_workers, max_leases = min(max_workers, HARD_MAX_WORKERS), min(max_leases, HARD_MAX_LEASES)
    if n > max_workers:
        raise LimitExceeded(f"{n} workers > max_workers = {max_workers}")
    if n + (1 if with_dev else 0) > max_leases:
        raise LimitExceeded(f"{n} workers" + (" + the dev box" if with_dev else "") + f" > max_total_leases = {max_leases}")


def cmd_start(a: argparse.Namespace) -> None:
    require_config(a.profile)
    if a.workers:  # refuse an impossible request before the dev box exists and starts billing
        check_worker_count(a.workers, with_dev=dev_box() is None)
        require_config(a.worker_profile or a.profile)
    if getattr(a, "cli_paid", False):
        worst = (0 if dev_box() else CFG["profiles"][a.profile]["max_usd_hr"] * int(CFG["max_runtime_hours"])) \
            + a.workers * CFG["profiles"][a.worker_profile or a.profile]["max_usd_hr"] * int(CFG["worker_runtime_hours"])
        if worst > 0:
            require_approval(a.approve_usd, worst, "ddc start")
    if not state_repo():
        log("WARNING: state_repo is empty, checkpoints are disabled: stopping this box discards anything not pushed to GitHub")
    bad = preflight("dev")
    if bad:
        die("preflight failed, nothing was created:\n  - " + "\n  - ".join(bad))
    if watchdog_installed() is False:
        log("WARNING: no watchdog is registered: nothing will stop an idle box or enforce the budget (only the platform's "
            f"{CFG['max_runtime_hours']} h runtime limit will). Run `ddc install` to register it.")
    with FileLock():
        refresh_live()
        try:
            reconcile_pending()
        except DdcError as e:
            log(f"warning: could not resolve pending creations yet: {e}")
        d = dev_box()
        if d:
            log(f"dev box already running: {d['name']} dseq={d['dseq']} at {d['host']}:{d['port']}; re-checking provisioning")
            provision_dev(d)
        else:
            for dseq, rec in active_deps().items():  # half-provisioned leftovers from a crashed start
                if rec.get("role") == "dev":
                    if rec.get("adopted"):  # rebuilt from the account: it may well be a running dev box with a workspace
                        die(f"the adopted dev lease {dseq} ({rec.get('name')}) has no known ssh endpoint, so ddc cannot tell whether it holds a workspace and will "
                            f"not close it by itself. Close it yourself (`ddc stop --only {dseq}`, add --discard-workspace to destroy its workspace) and run start again")
                    log(f"cleaning up unfinished dev deployment {dseq}")
                    errs: list[str] = []
                    _close_unready(dseq, rec, errs)
                    if errs:
                        die(errs[0])
            if getattr(a, "cli_paid", False):   # priced again after refresh_live: a stale record of a dead dev box must not skip the approval
                require_approval(a.approve_usd, CFG["profiles"][a.profile]["max_usd_hr"] * int(CFG["max_runtime_hours"])
                                 + a.workers * CFG["profiles"][a.worker_profile or a.profile]["max_usd_hr"] * int(CFG["worker_runtime_hours"]), "ddc start")
            check_budget(a.profile, 1, int(CFG["max_runtime_hours"]), role="dev")
            d = None
            for attempt in range(2):
                try:
                    d = create_box("dev", a.profile)  # re-runs the limit + budget check
                    break
                except (LimitExceeded, OutcomeUnknown, RollbackFailed, StateCorrupt):
                    raise  # never a second try after a refusal, an unknown outcome, or a lease that could not be cleaned up
                except DdcError as e:
                    if attempt == 1:
                        raise
                    log(f"retrying with another provider: {e}")
            provision_dev(d)
            d = dev_box()
        log("cpu: " + ssh(d, "grep -m1 'model name' /proc/cpuinfo | cut -d: -f2; echo cpus=$DDC_CPUS").replace("\n", " ").strip())
    if a.workers:
        cmd_scale(argparse.Namespace(n=a.workers, profile=a.worker_profile or a.profile, explore=False))
    print(f"\nReady. Run:  claude-cloud     (dev box {d['host']}:{d['port']}, ${d['usd_hr']:.3f}/h, hard stop after {d['limit_h']} h)")


def confirm(prompt: str) -> None:
    if not (sys.stdin and sys.stdin.isatty()):
        die("this needs a confirmation and there is no terminal: pass --yes (or --dry-run to only look)")
    if input(f"{prompt} [type yes] ").strip().lower() != "yes":
        die("aborted: nothing was closed")


def _close_one(dseq: str, errors: list[str], listed: dict[str, Any] | None = None) -> None:
    try:
        close_box(dseq, listed)
    except Exception as e:  # keep closing the others: one failure must not leave the rest billing
        log(f"ERROR closing dseq {dseq}: {e}")
        errors.append(f"{dseq}: {e}")


def _close_unready(dseq: str, rec: dict[str, Any], errors: list[str]) -> None:
    """Close a lease that never became ready. It was never provisioned, so it holds no workspace; a dev box whose ssh endpoint is known still
    gets a checkpoint attempt first (a dev lease is never closed without one). The caller makes sure it is not an adopted record."""
    if rec.get("role") == "dev" and rec.get("host") and rec.get("port") and state_repo():
        try:
            run_checkpoint({**rec, "dseq": dseq}, "final-stop")
        except Exception as e:  # expected for a box that never finished starting: the checkpoint script refuses without a restored workspace
            log(f"{rec.get('name')}: no checkpoint of the never-ready dev box ({e}); nothing was provisioned on it, closing it")
    _close_one(dseq, errors)


def _lease_line(r: dict[str, Any], note: str) -> str:
    up = (now() - float(r.get("lease_at") or r.get("created_at") or now())) / 3600
    return (f"  {r['dseq']:14} {str(r.get('name', '?')):26} {str(r.get('role', '?')):7} ${float(r.get('usd_hr') or r.get('max_usd_hr') or 0):5.3f}/h "
            f"up {up:4.1f}h  {note}")


def _ckpt_note(r: dict[str, Any], probe: bool) -> str:
    role = r.get("role")
    if role == "worker":
        return "workers hold no data"
    if role != "dev":
        return "NOT checkpointed: anything on it is lost"
    if not state_repo():
        return "CHECKPOINTS DISABLED: the workspace will be DESTROYED (needs --discard-workspace)"
    if r.get("status") != "ready":
        if r.get("adopted") or r.get("fallback"):
            return "rebuilt from the account, no ssh endpoint known: its workspace CANNOT be saved (needs --discard-workspace)"
        return "never became ready: nothing to save"
    if not probe:
        return "final checkpoint, verified on this PC, before closing"
    try:
        j = json.loads(ssh(r, "cat /run/ddc/state.json", timeout=20))
        return f"last checkpoint {j.get('last_checkpoint') or 'NONE yet'}{' (CHECKPOINTS FAILING)' if j.get('checkpoint_failed') else ''}; a final one is taken and verified first"
    except Exception:
        return "dev box unreachable: its checkpoint state is UNKNOWN"


def print_plan(verb: str, sel: list[dict[str, Any]], others: list[dict[str, Any]], probe: bool) -> None:
    print(f"Will {verb} {len(sel)} lease(s):")
    for r in sel:
        print(_lease_line(r, _ckpt_note(r, probe)))
    if others:
        print("Left running (not dev/worker, or not named with --only):")
        for r in others:
            print(_lease_line(r, "close it with --only " + str(r["dseq"]) if r.get("role") not in ("dev", "worker") else ""))


def cmd_scale(a: argparse.Namespace) -> None:
    n = a.n
    if not _int(n) or n < 0 or (n > HARD_MAX_WORKERS and not limits_unsafe()):
        die(f"cannot scale to {n!r} workers (0..{HARD_MAX_WORKERS})")
    only = getattr(a, "only", None) or []
    dry, yes = bool(getattr(a, "dry_run", False)), bool(getattr(a, "yes", False))
    require_config(a.profile)
    with FileLock():
        refresh_live()
        if not dev_box():
            die("start the dev box first (ddc start)")
        cur = workers()
        if n < len(cur):
            cands = [w for w in cur if _wants(w, only)][: len(cur) - n] if only else cur[n:]
            if only and not cands:
                die("--only matches none of the running workers")
            print_plan("close", cands, [w for w in cur if w not in cands], probe=False)
            if dry:
                return
            if getattr(a, "confirm_needed", False) and not yes:
                confirm("Close these workers?")
            errors: list[str] = []
            for w in cands:
                _close_one(w["dseq"], errors)
            if errors:
                die("some workers were NOT closed:\n  - " + "\n  - ".join(errors))
        elif n > len(cur):
            add = n - len(cur)
            hours = int(CFG["worker_runtime_hours"])
            pr = CFG["profiles"][a.profile]
            if dry:
                print(f"Would create {add} worker lease(s) of profile {a.profile}: worst case ${pr['max_usd_hr'] * add * hours:.2f} at the price cap ({hours} h limit)")
                return
            if getattr(a, "cli_paid", False):
                require_approval(a.approve_usd, pr["max_usd_hr"] * add * hours, "ddc scale")
            check_budget(a.profile, add, hours, role="worker")
            used = {w["idx"] for w in cur}
            idxs = [i for i in range(1, 100) if i not in used][:add]
            # the dev box cannot reach leases on its own provider through that provider's public NAT port
            counts: dict[str, int] = {dev_box()["provider"]: 10**6}
            for w in cur:
                counts[w["provider"]] = counts.get(w["provider"], 0) + 1
            with cf.ThreadPoolExecutor(max_workers=min(12, add)) as ex:
                futs = {ex.submit(create_box, "worker", a.profile, i, counts, getattr(a, "explore", False)): i for i in idxs}
                for f in cf.as_completed(futs):
                    try:
                        box = f.result()
                    except Exception as e:
                        log(f"worker w{futs[f]:02d} failed: {e}")
                        continue
                    try:
                        provision_worker(box)
                    except Exception as e:  # a lease that did not provision is useless and billing: close it
                        log(f"worker w{futs[f]:02d} provisioning failed: {e}; closing the new lease")
                        try:
                            close_box(box["dseq"])
                        except Exception as ce:
                            log(f"ERROR: could not close {box['dseq']}: {ce}; run `ddc stop --only {box['dseq']}`")
        sync_worker_hosts()
        log(f"workers ready: {len(workers())} ({sum(CFG['profiles'][w['profile']]['cpu'] for w in workers())} vCPU)")

def run_checkpoint(d: dict[str, Any], reason: str, allow_shrink: bool = False) -> str:
    """Run checkpoint.sh on the dev box; returns its last output line. A refusal by the box (empty workspace, restore unfinished,
    foreign state repo, ...) is surfaced with the box's own words instead of a raw ssh error."""
    cmd = f"/opt/ddc/checkpoint.sh {shlex.quote(reason)}" + (" --allow-shrink" if allow_shrink else "")
    try:  # re-send the lease env first, so a state_repo (or other setting) changed in config.toml since `start` takes effect on this box
        put_file(d, "/home/dev/.config/ddc/env", lease_env(d["dseq"]).encode())  # as dev: the box's dev-user scripts source it (mode 600)
    except Exception as e:  # the box keeps its old env; verify_checkpoint on this PC still checks what was actually pushed
        log(f"warning: could not refresh the lease env before the checkpoint ({e}); checkpointing with the env from `start`")
    try:
        lines = ssh(d, cmd, timeout=3600).strip().splitlines()
    except DdcError as e:
        m = re.search(r"refusing: [^\n]+", str(e))
        if m:
            hint = " (if you removed that workspace on purpose, pass --allow-shrink)" if re.search(r"shrink|entries|missing", m.group(0)) else ""
            raise DdcError(f"the dev box refused the checkpoint: {m.group(0)}{hint}") from e
        raise
    if not lines:
        die("checkpoint.sh printed nothing")
    log(lines[-1])
    return lines[-1]


def final_checkpoint(d: dict[str, Any], force: bool = False, discard_workspace: bool = False, allow_shrink: bool = False) -> None:
    """Final checkpoint on the dev box, decrypted and compared on this PC. Raises (box stays up) unless force. With checkpoints
    disabled (empty state_repo) it refuses to let the dev box be closed unless discard_workspace says the workspace may be destroyed."""
    try:
        if state_repo():
            # every /work/<dir> (checkpoint.sh packs each as ws-<dir>; `-` = not a git repo) and, for repos, the HEAD
            live = ssh(d, "for r in /work/*/; do n=$(basename \"$r\"); [ \"$n\" = results ] && continue; "
                          "if [ -d \"$r/.git\" ]; then echo \"$n $(git -C \"$r\" rev-parse HEAD)\"; else echo \"$n -\"; fi; done", timeout=60)
            out = run_checkpoint(d, "final-stop", allow_shrink)
            if not out.startswith("checkpoint_ok"):
                die(f"unexpected checkpoint output: {out!r}")
            commit = out.split()[2]
            v = verify_checkpoint(commit)
            archives = v["manifest"].get("archives") or {}
            for line in live.splitlines():
                name, head = line.rsplit(" ", 1)
                if f"ws-{name}" not in archives:  # a dropped workspace must not pass just because the archives that remain decrypt
                    die(f"checkpoint has no archive for /work/{name}: it would be lost")
                got = v["manifest"]["repos"].get(name, {}).get("head")
                if head != "-" and got != head:
                    die(f"checkpoint repo {name} head {got} != live {head}")
        elif discard_workspace:
            log("state_repo is empty and --discard-workspace was given: no final checkpoint, anything not pushed to GitHub is discarded with the box")
        else:
            raise DiscardRefused("checkpoints are disabled (state_repo is empty): closing the dev box would DESTROY everything on it that is not "
                                 "pushed to GitHub. Set state_repo (then `ddc install`), or pass --discard-workspace to close it anyway")
        if push_claude_credentials():
            cred = ssh(d, "cat ~/.claude/.credentials.json 2>/dev/null", check=False)
            if cred.strip().startswith("{"):
                write_private(SECRETS_DIR / "claude-credentials.json", cred)
                log("saved Claude Code login from the lease to the PC secrets dir (not in backups)")
    except DiscardRefused:
        raise
    except Exception as e:
        if not force:
            alert_once("stop-aborted", f"stop aborted, dev box left RUNNING because the final checkpoint did not verify: {e}", 3600)
            raise
        if not discard_workspace:  # --force alone never destroys work: losing the workspace has to be said out loud
            raise DiscardRefused(f"the final checkpoint did not verify ({e}). --force alone does not destroy the workspace: add --discard-workspace "
                                 "to close the dev box anyway and lose everything since the last good checkpoint") from e
        log(f"AUDIT FORCED CLOSE of dev box dseq={d.get('dseq')} {d.get('name')}: closing WITHOUT a verified checkpoint, workspace discarded "
            f"(--force --discard-workspace). Reason: {e}")


def is_job_lease(d: dict[str, Any]) -> bool:
    """An owned `ddc job` lease: recorded as ours, role "job", named ddc-*.  Anything else is never a job lease."""
    return d.get("role") == "job" and d.get("ours") is True and str(d.get("name") or "").startswith(NAME_PREFIX)


def _stop_rows(deps: dict[str, dict[str, Any]], only: list[str], workers_only: bool, include_jobs: bool = False) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(to close, left running). Default = our dev box and workers; anything else (hand-made experiments, role box) only when named.
    `include_jobs` (the user's explicit stop-everything, see cmd_stop) adds the owned job leases."""
    sel, rest = [], []
    for dseq, d in deps.items():
        rec = d | {"dseq": dseq}
        if only:
            take = _wants(rec, only)
        elif include_jobs and not workers_only and is_job_lease(d):
            take = True
        else:
            take = d.get("role") in ("dev", "worker") and not (workers_only and d.get("role") == "dev")
        (sel if take else rest).append(rec)
    key = lambda r: (r.get("role") == "dev", r.get("idx") or 0)  # noqa: E731  workers first, dev box last
    return sorted(sel, key=key), sorted(rest, key=key)


def refresh_best_effort() -> None:
    """refresh_live() for callers that must not be blocked by API trouble (closing leases matters more than a fresh view)."""
    try:
        refresh_live()
    except StateCorrupt:
        raise
    except DdcError as e:
        log(f"warning: could not refresh from the API ({e}); continuing with what is recorded")


def fallback_deps() -> dict[str, dict[str, Any]]:
    """state.json is damaged, so nothing we recorded can be trusted. Read-only: the ddc-* deployments on the Akash account as records
    (dseq -> record), so `stop` and the watchdog can still close what we created. Hand-made deployments (any other name) never appear,
    and every row needs one consistent owner (also the owner in state.json.bak when that is readable)."""
    rows = [x for x in list_account_deployments() if x["name"].startswith(NAME_PREFIX)]
    owners = {x["owner"] for x in rows if x["owner"]}
    bak = _parse_state(_read_bak())
    known = {str(r["owner"]) for r in (bak or {"deps": {}})["deps"].values() if r.get("owner")}
    if len(owners) > 1 and not known:  # nothing left to tell which of them is ours
        die(f"the ddc-* deployments on the account belong to more than one owner ({', '.join(sorted(owners))}) and no recorded owner is left to compare: "
            "not closing anything from a listing")
    out: dict[str, dict[str, Any]] = {}
    for x in rows:
        if not x["owner"] or (known and x["owner"] not in known):
            log(f"warning: not touching {x['name']} dseq={x['dseq']}: its owner {x['owner'] or '?'} does not match the recorded one")
            continue
        role, idx = _role_from_name(x["name"])
        try:
            host, port = _det_endpoint(deployment(x["dseq"]))
        except DdcError:
            host = port = None
        ok = valid_host(host) and valid_port(port)
        rec: dict[str, Any] = {"ours": True, "fallback": True, "name": x["name"], "role": role, "idx": idx, "owner": x["owner"], "status": "ready" if ok else "leased"}
        if ok:
            rec.update(host=host, port=int(port))
        out[x["dseq"]] = rec
    return out


def _activity_changed(d: dict[str, Any], expect: str) -> str | None:
    """After the final checkpoint of an idle stop: why the box must NOT be closed now (None = still idle, go ahead). The agent withdraws
    its shutdown request when the user comes back, which can happen during a checkpoint that takes minutes."""
    try:
        j = json.loads(ssh(d, "cat /run/ddc/state.json", timeout=40))
    except Exception as e:
        return f"cannot re-check the box's activity ({e})"
    if j.get("shutdown_requested") != expect or j.get("active_reason"):
        return f"activity resumed during the final checkpoint (shutdown_requested={j.get('shutdown_requested')!r}, active_reason={j.get('active_reason')!r})"
    return None


def cmd_stop(a: argparse.Namespace) -> None:
    """Close our leases. Lists exactly what will be closed first; the CLI asks for confirmation (--yes skips, --dry-run only lists).
    The dev box is checkpointed (and verified) BEFORE anything is closed: if that fails, nothing is closed. Every close is attempted even
    if one fails; the exit status is non-zero if any lease was left open. With a damaged state.json the ddc-* deployments are taken from
    a read-only account listing (see fallback_deps).  The user's own stop-everything (`include_jobs`: CLI without --only/--workers-only, the TUI's stop) also
    closes the owned `ddc job` leases; the watchdog's stops do not (job leases have their own orphan rule and budget stop)."""
    g = lambda k, dflt=None: getattr(a, k, dflt)  # noqa: E731  (providers/TUI call this with a bare Namespace)
    only = g("only") or []
    dry, force, discard, shrink = bool(g("dry_run")), bool(g("force")), bool(g("discard_workspace")), bool(g("allow_shrink"))
    expect = g("expect_shutdown")  # the watchdog's idle stop: abort if the user came back while the final checkpoint ran
    include_jobs = bool(g("include_jobs"))  # set only by the user's own stop-everything (CLI / TUI): the watchdog's stops leave job leases to the orphan rule and the budget stop
    with FileLock(wait_s=g("lock_wait", 900)):
        listed = False
        try:
            refresh_best_effort()
            deps = active_deps()
        except StateCorrupt:
            log("WARNING: state.json is damaged: closing from a read-only listing of the ddc-* deployments on the account (other names are never touched)")
            deps, listed = fallback_deps(), True
        sel, rest = _stop_rows(deps, only, bool(g("workers_only")), include_jobs)
        if only and not sel:
            die(f"--only {' '.join(only)} matches no active lease")
        cli = bool(g("confirm_needed")) or dry
        if cli:
            print_plan("close", sel, rest, probe=True)
        if dry:
            return
        if g("confirm_needed") and not g("yes"):
            confirm("Close these leases now?")
        jobs = [r["dseq"] for r in sel if is_job_lease(r) and not only]
        if jobs and not dry:
            log(f"explicit stop: also closing {len(jobs)} job lease(s) ({', '.join(jobs)}): the stop-everything command must stop all billing; a running `ddc job run` will notice and report it")
        errors: list[str] = []
        for r in sel:  # phase 1: save the dev box. Nothing is closed (workers included) unless every dev checkpoint is verified.
            if r.get("role") != "dev":
                continue
            try:
                if r.get("status") == "ready":
                    final_checkpoint(r, force, discard, shrink)
                    why = _activity_changed(r, expect) if expect else None
                    if why:
                        alert(f"stop aborted, dev box left RUNNING: {why}")
                        raise DdcError(why)
                elif (listed or r.get("adopted")) and not discard:  # rebuilt from the account: no ssh endpoint, nothing can be saved, so the user has to say so
                    raise DiscardRefused(f"{r['dseq']} is a dev lease with no known ssh endpoint (rebuilt from the account): its workspace cannot be checkpointed. Pass --discard-workspace to close it anyway")
            except Exception as e:
                errors.append(f"{r['dseq']} (dev box) left RUNNING: {e}")
        if errors:
            raise DdcError("stop aborted, nothing was closed:\n  - " + "\n  - ".join(errors))
        for r in sel:
            _close_one(r["dseq"], errors, r if listed else None)
        if errors:
            raise DdcError("some leases were NOT closed (they may still be billing):\n  - " + "\n  - ".join(errors))
        left = [r["dseq"] for r in rest]
        spent = f"; total spent ${budget_view()['spent']:.2f} of ${CFG['budget_usd']}" if not listed else ""
        log(f"closed {len(sel)} lease(s){spent}" + (f"; left running: {', '.join(left)}" if left else ""))


def status_json() -> dict[str, Any]:
    """`ddc status --json`: our records plus two independent platform views (spend-rate = what is billing, the account's active deployments).
    `nothing_billing` is true only when all three agree that nothing runs; an unreadable platform view makes it false, never true."""
    out: dict[str, Any] = {"warnings": config_warnings(), "alerts": recent_alerts()}
    try:
        refresh_live()
        deps, pend = active_deps(), pending_creations()
        out["state_ok"] = True
    except StateCorrupt as e:
        deps, pend = {}, {}
        out.update(state_ok=False, state_problem=str(e))
    try:
        out["balance_usd"] = round(balance_usd(), 4)
    except DdcError as e:
        out["balance_usd"], out["balance_error"] = None, str(e)
    out["budget"] = {k: round(float(v), 4) for k, v in budget_view().items()} if out["state_ok"] else None
    out["reserve_usd"], out["budget_guard"] = CFG.get("reserve_usd"), CFG.get("budget_guard") is not False
    leases = []
    for k, d in sorted(deps.items()):
        spent, _ = dep_running_cost(d)
        leases.append({"dseq": k, "spent_usd": round(spent, 4), **{f: d.get(f) for f in ("name", "role", "job", "status", "usd_hr", "max_usd_hr", "limit_h",
                       "provider", "provider_host", "image", "created_at", "lease_at", "deadline_at", "heartbeat_at")}})
    out["leases"], out["pending"] = leases, sorted(pend)
    out["burn_usd_hr"] = round(sum(_finite(d.get("usd_hr")) for d in deps.values()), 4)
    billing = billing_dseqs()
    out["billing_dseqs"] = sorted(billing) if billing is not None else None
    try:
        out["account_active"] = list_account_deployments()
    except DdcError as e:
        out["account_active"], out["account_error"] = None, str(e)
    out["nothing_billing"] = bool(out["state_ok"] and not leases and not pend and billing == set() and out["account_active"] == [])
    # the same, limited to ddc: deployments named ddc-* or ever recorded by us (other deployments on the account are never ours to judge or touch)
    acct = out["account_active"]
    known = set((load_state().get("deps") or {}) if out["state_ok"] else ())
    ours = sorted({x["dseq"] for x in acct or [] if x["name"].startswith(NAME_PREFIX)} | ((billing or set()) & known))
    out["ddc_active_on_account"] = ours
    out["foreign_active"] = [x for x in acct or [] if not x["name"].startswith(NAME_PREFIX) and x["dseq"] not in known]
    out["ddc_nothing_billing"] = bool(out["state_ok"] and not leases and not pend and billing is not None and acct is not None and not ours)
    return out


def cmd_status(a: argparse.Namespace) -> None:
    if getattr(a, "json", False):
        import contextlib
        with contextlib.redirect_stdout(sys.stderr):   # log lines go to stderr, stdout carries only the JSON
            out = status_json()
        print(json.dumps(out, indent=1, default=str))
        return
    for w in config_warnings()[:3]:
        print(f"WARNING: {w}")
    try:
        refresh_live()
        deps = active_deps()
        pend = pending_creations()
    except StateCorrupt as e:
        print(f"STATE PROBLEM: {e}\n")
        print("What the Akash account says (read-only):")
        for x in list_account_deployments():
            print(f"  {x['dseq']:14} {x['name']}{'' if x['name'].startswith(NAME_PREFIX) else '   (not ours)'}")
        return
    bal = balance_usd()
    v = budget_view()
    print(f"Akash balance ${bal:.2f} | spent by ddc ${v['spent']:.2f} | worst-case incl. running ${v['worst']:.2f} / budget ${v['budget']:g} | reserve ${CFG['reserve_usd']:g}")
    for line in recent_alerts():
        print("ALERT " + line)
    for name in pend:
        print(f"PENDING creation (outcome unknown, counted in the budget): {name}; run `ddc reconcile`")
    if not deps:
        print("No ddc leases running (nothing is billing)." if not pend else "No ddc leases recorded.")
        return
    tot = 0.0
    print(f"{'NAME':24} {'DSEQ':14} {'STATUS':8} {'$/h':>6} {'UP':>6} {'SPENT':>7}  PROVIDER")
    for k, d in sorted(deps.items(), key=lambda x: (x[1].get("role") != "dev", x[1].get("idx", 0))):
        spent, _ = dep_running_cost(d)
        up = (now() - float(d.get("lease_at") or now())) / 3600
        tot += _finite(d.get("usd_hr"))
        print(f"{d.get('name', '?'):24} {k:14} {d.get('status', '?'):8} {float(d.get('usd_hr') or 0):6.3f} {up:5.1f}h {spent:7.3f}  {d.get('provider_host') or d.get('provider', '')}")
    print(f"burn rate ${tot:.2f}/h")
    if any(d.get("role") == "job" for d in deps.values()):
        print(f"job lease(s) are run by `ddc job` (close one: `ddc job close JOB`; a plain `ddc stop` closes them too); the watchdog closes one whose runner is silent for {CFG.get('job_orphan_minutes', JOB_ORPHAN_MINUTES)} min")
    dv = dev_box()
    if dv:
        s = ssh(dv, "cat /run/ddc/state.json 2>/dev/null", check=False, timeout=30)
        if s.strip():
            j = json.loads(s)
            print(f"dev box: idle {j['idle_min']}m (stop at {CFG['idle_minutes']}m) | active: {j['active_reason'] or '-'} | up {j['uptime_min']}m | "
                  f"last checkpoint {j['last_checkpoint'] or 'none yet'}{' | CHECKPOINT FAILING' if j['checkpoint_failed'] else ''}"
                  f"{' | shutdown requested: ' + j['shutdown_requested'] if j['shutdown_requested'] else ''}")


def cmd_attach(a: argparse.Namespace) -> int:
    d = dev_box()
    if not d:
        print("No dev box running. Start one with: ddc start")
        return 1
    target = f"main:{a.window}" if a.window else "main"
    while True:
        rc = subprocess.call(ssh_args(d, tty=True) + [f"tmux new-session -A -s main \\; select-window -t {target}" if a.window else "tmux new-session -A -s main"])
        if rc != 255:
            return rc
        print("connection lost; reconnecting in 3 s (Ctrl+C to quit) ...")
        time.sleep(3)
        refresh_live()
        d = dev_box()
        if not d:
            print("dev box is gone (closed).")
            return 1


def cmd_bench(a: argparse.Namespace) -> None:
    boxes = ([dev_box()] if dev_box() else []) + workers()
    if not boxes:
        die("nothing running")
    wl = " ".join(shlex.quote(w) for w in a.workloads)
    if dev_box():
        touch_worker_activity(dev_box())  # jobs are about to run on the workers: keep them from being closed as idle

    def run(b: dict[str, Any]) -> dict[str, Any]:
        out = ssh(b, f"bash /opt/ddc/bench.sh {wl}", timeout=3600).strip().splitlines()[-1]
        r = json.loads(out)
        r.update(dseq=b["dseq"], name=b["name"], provider=b["provider"], provider_host=b.get("provider_host"), usd_hr=b["usd_hr"], profile=b["profile"])
        return r

    with cf.ThreadPoolExecutor(max_workers=len(boxes)) as ex:
        results = list(ex.map(run, boxes))
    perf = read_json(PERF, {})
    with (STATE_DIR / "bench.jsonl").open("a", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")
            if "cpu" in r:
                perf.setdefault(r["provider"], {}).update(single_score=r["cpu"]["single_score"], multi_per_cpu=r["cpu"]["multi_score"] / r["cpus"],
                                                          cpu_model=r["cpu_model"], measured_at=r["at"])
    write_json(PERF, perf)
    for r in results:
        print(f"\n{r['name']}  {r['cpu_model']}  {r['cpus']} vCPU  ${r['usd_hr']:.3f}/h  ({r.get('provider_host')})")
        for k, v in r.items():
            if isinstance(v, dict) and "wall_s" in v:
                print(f"  {k:18} {v['wall_s']:7.1f}s  cpu {v['cpu_s']:8.1f}s  util {v['cpu_util'] * 100:5.1f}%  cost ${v['wall_s'] / 3600 * r['usd_hr']:.4f}  exit {v['exit']}")
            elif k == "cpu":
                print(f"  cpu single-thread score {v['single_score']:.0f}  multi {v['multi_score']:.0f} ({v['multi_score'] / r['cpus']:.0f}/vCPU)")


def cmd_checkpoint(a: argparse.Namespace) -> None:
    need_state_repo()
    d = dev_box() or die("no dev box")
    out = run_checkpoint(d, "manual", bool(getattr(a, "allow_shrink", False)))
    if not out.startswith("checkpoint_ok"):  # e.g. `checkpoint_skip ...`: nothing was taken, so there is nothing new to verify or report
        die(f"no checkpoint was taken: {out}")
    verify_checkpoint(out.split()[2])


def _role_from_name(name: str) -> tuple[str, int]:
    m = re.match(rf"{NAME_PREFIX}(dev|w(\d{{2}})|[a-z]+)-", name)
    if not m:
        return "box", 0
    return ("worker", int(m.group(2))) if m.group(2) else (m.group(1), 0)


def cmd_reconcile(a: argparse.Namespace) -> None:
    """Compare our records with the Akash account and repair a damaged state file (--restore-backup / --adopt). It can CLOSE a lease whose
    creation was recorded but whose reply was lost (a pending creation), so it holds the same lock as start/scale/stop/watchdog: it can
    never close a lease another command is still provisioning."""
    with FileLock(wait_s=float(getattr(a, "lock_wait", 120))):
        _reconcile_locked(a)


def _reconcile_locked(a: argparse.Namespace) -> None:
    flagged = (STATE_DIR / "state.corrupt").exists()
    if not flagged and not getattr(a, "restore_backup", False):
        try:
            load_state()  # a missing / damaged state file flags itself here (and raises); the repair below then applies
        except StateCorrupt:
            flagged = True
    if getattr(a, "restore_backup", False):
        with _state_lock:
            text = _read_bak()
            if _parse_state(text) is None:
                die(f"{STATE_FILE.name}.bak is missing or not valid: nothing to restore (use --adopt)")
            tmp = STATE_FILE.with_name(STATE_FILE.name + ".restore")
            tmp.write_text(text or "", encoding="utf-8")
            if STATE_FILE.exists():
                os.replace(STATE_FILE, STATE_FILE.with_name(f"state.json.replaced-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"))
            os.replace(tmp, STATE_FILE)
            (STATE_DIR / "state.corrupt").unlink(missing_ok=True)
        log("restored state.json from state.json.bak; run `ddc reconcile` again to compare it with the account")
        return
    for m in ([] if flagged else reconcile_pending()):
        print(m)
    st = {"deps": {}, "pending": {}} if flagged else load_state()
    known = set(st["deps"])
    unknown = [x for x in list_account_deployments() if x["name"].startswith(NAME_PREFIX) and x["dseq"] not in known]
    print(f"{len(st['deps'])} lease record(s), {len(st.get('pending') or {})} pending creation(s); {len(unknown)} ddc-* deployment(s) on the account that are not in the state file:")
    for x in unknown:
        print(f"  {x['dseq']:14} {x['name']}   (owner {x['owner'] or '?'})")
    for dseq, d in st["deps"].items():
        if d.get("status") not in ("closed",):
            print(f"  recorded: {dseq:14} {d.get('name', '?')} [{d.get('status')}]")
    if getattr(a, "adopt", False):
        only = getattr(a, "only", None) or []
        if only and flagged:
            die("--only cannot be used while the state file is damaged: adopting a part would hide the other leases (adopt all, or --restore-backup)")
        if only:
            unknown = [x for x in unknown if _wants({"dseq": x["dseq"], "name": x["name"]}, only)]
        if unknown and getattr(a, "confirm_needed", False) and not getattr(a, "yes", False):
            confirm(f"Record these {len(unknown)} deployment(s) as ddc leases that `ddc stop` may close?")
        usd = {str(x["dseq"]): usd_hr_from_uact_block(float(x["price"]["amount"])) for x in (api("GET", "/v1/spend-rate").get("data") or {}).get("deployments") or []}
        for x in unknown:
            det = deployment(x["dseq"])
            role, idx = _role_from_name(x["name"])
            host, port = _det_endpoint(det)
            ok = valid_host(host) and valid_port(port)
            profile = x["name"].split("-")[2] if x["name"].count("-") >= 2 else ""
            cap = (CFG["profiles"].get(profile) or {}).get("max_usd_hr")
            # `adopted` records are never closed automatically (stale-unready watchdog rule, `start` cleanup): only a user naming them closes them.
            # Without a usable ssh endpoint the status stays "leased" so nothing treats it as a ready dev box / worker.
            rec = {"ours": True, "name": x["name"], "role": role, "idx": idx, "status": "ready" if ok else "leased", "adopted": True, "created_at": now(),
                   "lease_at": now(), "limit_h": int(CFG["max_runtime_hours"] if role == "dev" else CFG["worker_runtime_hours"]),
                   "usd_hr": usd.get(x["dseq"], 0.0), "owner": x["owner"], "profile": profile,
                   "max_usd_hr": cap if _num(cap) else price_ceiling()}  # the budget commits the price cap when the real price is unknown
            if ok:
                rec.update(host=host, port=int(port))

            def fn(s: dict[str, Any], dseq: str = x["dseq"], r: dict[str, Any] = rec) -> None:
                s["deps"][dseq] = r
            with _state_lock:
                if flagged and not STATE_FILE.exists():
                    write_json(STATE_FILE, {"deps": {}})
                (STATE_DIR / "state.corrupt").unlink(missing_ok=True)
                update_state(fn)
            ledger_add({"event": "created", "dseq": x["dseq"], "name": x["name"], "adopted": True})
            print(f"adopted {x['dseq']} {x['name']}")
        if flagged:
            with _state_lock:
                if not STATE_FILE.exists():
                    write_json(STATE_FILE, {"deps": {}})
                (STATE_DIR / "state.corrupt").unlink(missing_ok=True)
            print("state problem cleared: the state file now matches the account (adopted records have unknown spend history: budget counts from now)")
    elif unknown or flagged:
        print("Nothing was changed. `ddc reconcile --adopt` records these as ours (so `ddc stop` can close them)" + ("; `--restore-backup` restores the last good state file." if flagged else "."))


# ---------------------------------------------------------------- watchdog
_SSH_LOCAL = re.compile(r"permission denied|load key|no such identity|identity file|bad permissions|unprotected private key|invalid format|host key verification failed|"
                        r"host identification has changed|too many authentication failures|no matching (host key|key exchange|cipher)|could not create directory", re.I)
_SSH_NETWORK = re.compile(r"timed out|connection refused|no route to host|network is unreachable|could not resolve hostname|connection reset|"
                          r"connection closed|broken pipe|host is down|kex_exchange_identification", re.I)


def ssh_unreachable(e: BaseException) -> bool:
    """True only when the dev box itself could not be reached (connect failure or timeout). A problem on this PC (missing/unreadable key,
    refused auth, changed host key, no ssh binary) or an unreadable reply says nothing about the box: it is a failure to alert on, and a
    possibly busy box is never closed because of it."""
    if isinstance(e, subprocess.TimeoutExpired):
        return True
    m = str(e)
    return isinstance(e, DdcError) and "exit 255" in m and not _SSH_LOCAL.search(m) and bool(_SSH_NETWORK.search(m))


def agent_restart_command(lease_dir: str = "/opt/ddc", log_path: str = "/work/results/agent.log") -> str:
    """Shell text that starts the lease-side agent unless it is running. `pgrep -f` also sees the shell that runs this very text, so the
    pattern is bracketed ([/]opt...) and the path never appears literally anywhere else in the command line (it is built from $d)."""
    path = f"{lease_dir}/agent.sh"
    pattern = "[" + path[0] + "]" + re.escape(path[1:])
    return (f"d={shlex.quote(lease_dir)}; pgrep -u \"$(id -un)\" -f {shlex.quote(pattern)} >/dev/null || "
            f"(setsid nohup \"$d/agent.sh\" >> {shlex.quote(log_path)} 2>&1 < /dev/null &)")


def job_leases() -> dict[str, dict[str, Any]]:
    """Active leases created by `ddc job` runs: recorded as ours, role "job", named ddc-*.  Anything else is never a job lease."""
    return {k: v for k, v in active_deps().items() if is_job_lease(v)}


def job_reap_reason(rec: dict[str, Any], t: float | None = None) -> str | None:
    """Why the watchdog must close this job lease now (None = leave it): it is past its own deadline, or its runner has not recorded a
    heartbeat for job_orphan_minutes (the runner died: power loss, kill -9).  A record with no timestamp at all counts as silent."""
    t = now() if t is None else t
    deadline = _finite(rec.get("deadline_at"), 0.0, positive=True)   # an unreadable deadline counts as none: the heartbeat rule below still applies
    if deadline and t > deadline:
        return f"past its own deadline by {(t - deadline) / 60:.0f} min"
    limit = float(CFG["job_orphan_minutes"] if _num(CFG.get("job_orphan_minutes")) and CFG["job_orphan_minutes"] >= JOB_ORPHAN_MIN_MINUTES else JOB_ORPHAN_MINUTES)
    # an unreadable heartbeat is not "alive for ever": it falls back to created_at, and without that to 0 (silent, so the lease is closed)
    silent = t - (_finite(rec.get("heartbeat_at"), 0.0, positive=True) or _finite(rec.get("created_at"), 0.0, positive=True))
    if silent > limit * 60:
        return f"its runner has been silent for {silent / 60:.0f} min (job_orphan_minutes = {limit:g})"
    return None


def reap_jobs(everything: bool = False) -> list[str]:
    """Close (through close_box, so the ownership assertions apply) the job leases whose runner is gone; `everything` = close all of them
    (budget stop).  Only owned role-"job" leases are ever considered; adopted records are left to the user, like every other adopted lease.
    The failure direction is "stop billing": a close that fails is alerted and retried by the next pass."""
    closed: list[str] = []
    for dseq, rec in job_leases().items():
        why = "?"
        try:   # one malformed record must never abort the pass (the dev-box checks and the other job leases come after it)
            if rec.get("adopted"):
                continue
            why = "the budget is exhausted" if everything else job_reap_reason(rec)
            if not why:
                continue
            with FileLock(wait_s=0):  # not while `ddc start` / stop / reconcile hold the lock; the next pass retries
                cur = load_state()["deps"].get(dseq) or {}
                if cur.get("status") == "closed" or cur.get("role") != "job" or not (everything or job_reap_reason(cur)):  # closed or heard from meanwhile
                    continue
                alert(f"closing job lease {cur.get('name')} dseq={dseq}: {why}")
                close_box(dseq)
                closed.append(dseq)
        except LockBusy:
            log(f"watchdog: job lease {rec.get('name')} dseq={dseq} should be closed ({why}) but another ddc command holds the lock: next pass")
        except DdcError as e:
            alert_once(f"job-reap-{dseq}", f"could not close the job lease {rec.get('name')} dseq={dseq} ({why}): {e}. It may still be billing: `ddc stop --only {dseq}`", 1800)
        except Exception as e:  # noqa: BLE001  (a record that cannot even be evaluated: say so, go on with the others)
            alert_once(f"job-reap-bad-{dseq}", f"job lease dseq={dseq} skipped: its record cannot be evaluated ({type(e).__name__}: {e}). It may still be billing: `ddc stop --only {dseq}`", 1800)
    return closed


def watchdog_pass() -> str:
    """One enforcement pass (idle / max-runtime / budget / stale leases). Returns what happened; raises on a real failure."""
    st = load_state()  # StateCorrupt here = a failure the caller alerts on
    if not (active_deps(st) or pending_creations(st)):
        return "idle"
    try:
        with FileLock(wait_s=5):
            try:
                refresh_live()
                reconcile_pending()
            except DdcError as e:  # the API being down must not switch the ssh-based checks off
                log(f"watchdog: refresh failed ({e}); continuing with what is recorded")
                alert_once("wd-refresh", f"watchdog cannot reach the Akash API: {e}", 3600)
    except LockBusy:  # a start/stop is in progress
        n = wd_update(lambda h: h.__setitem__("skips", int(h.get("skips") or 0) + 1) or h["skips"])
        log(f"watchdog: another ddc command holds the lock (skipped {n} run(s) in a row)")
        if n >= 6:
            alert_once("wd-skipped", f"watchdog could not run for {n * 5} min: another ddc command has held the lock the whole time", 3600)
        return "skipped"
    wd_update(lambda h: h.update(skips=0))
    deps = active_deps()
    if not deps:
        return "nothing running"
    reaped = reap_jobs()  # before everything else: a dead runner's lease bills while the dev-box checks below run (or return early)
    if reaped:
        deps = active_deps()
        if not deps:
            return f"closed {len(reaped)} orphaned job lease(s)"
    v = budget_view()
    burn = sum(dep_usd_hr(d) for d in deps.values())
    if CFG.get("budget_guard", True) and v["spent"] + burn * 0.25 >= v["budget"]:
        alert(f"budget nearly exhausted (${v['spent']:.2f} of ${v['budget']:g}); stopping")
        reap_jobs(everything=True)  # job leases are not dev/worker leases: cmd_stop leaves them running
        cmd_stop(argparse.Namespace(workers_only=False, force=True, discard_workspace=True, lock_wait=120))
        return "budget stop"
    if v["spent"] >= 0.8 * v["budget"] and not load_state().get("alerted_80"):
        alert(f"80% of the ${v['budget']:g} budget used (${v['spent']:.2f})")
        update_state(lambda s: s.__setitem__("alerted_80", True))
    for dseq, rec in deps.items():  # leases WE created that never became ready hold no work: do not let them bill until the platform limit
        if rec.get("adopted"):  # rebuilt from the account (`reconcile --adopt`): its status says nothing about what runs on it, only the user closes it
            continue
        if rec.get("role") == "job":  # a job leases late on purpose (its deployment may wait for local prep for a long time): reap_jobs' heartbeat rule covers it
            continue
        if rec.get("status") in ("created", "leased") and now() - float(rec.get("created_at") or now()) > STALE_UNREADY_S:
            try:
                with FileLock(wait_s=0):  # a slow `start` may be about to make it ready: it holds the lock while it provisions
                    cur = load_state()["deps"].get(dseq) or {}
                    if cur.get("status") in ("created", "leased") and not cur.get("adopted"):
                        log(f"watchdog: {rec.get('name')} dseq={dseq} never became ready: closing it")
                        errors: list[str] = []
                        _close_unready(dseq, cur, errors)
                        if errors:
                            alert_once(f"unready-{dseq}", f"could not close the unready lease {dseq}: {errors[0]}", 1800)
            except LockBusy:
                log(f"watchdog: {rec.get('name')} dseq={dseq} looks unready but another ddc command holds the lock: not closing it now")
    d = dev_box()
    if not d:
        if workers():
            log("watchdog: workers without a dev box; closing them")
            cmd_stop(argparse.Namespace(workers_only=True, force=False, lock_wait=120))
        return "no dev box"
    try:
        j = json.loads(ssh(d, "cat /run/ddc/state.json", timeout=40))
        update_state(lambda s: s.__setitem__("dev_unreachable_since", None))
    except Exception as e:
        if not ssh_unreachable(e):  # a problem on THIS PC (key, auth, host key, ssh binary) or an unreadable reply says nothing about the box
            update_state(lambda s: s.__setitem__("dev_unreachable_since", None))
            alert_once("ssh-local", f"the watchdog cannot check the dev box and the problem is not 'unreachable' ({e}). The box is NOT being closed; "
                       "idle and runtime enforcement are blocked until this is fixed (ssh key, host key, ssh installation?)", 3600)
            raise
        since = load_state().get("dev_unreachable_since") or now()
        update_state(lambda s: s.__setitem__("dev_unreachable_since", since))
        if now() - since > 30 * 60:
            alert(f"dev box unreachable for 30 min ({e}); closing (last checkpoint is in the state repo)")
            cmd_stop(argparse.Namespace(workers_only=False, force=True, discard_workspace=True, lock_wait=120))
            return "unreachable stop"
        log(f"watchdog: dev box unreachable ({e})")
        return "unreachable"
    up_h = (now() - float(d.get("lease_at") or now())) / 3600
    max_h = float(CFG["max_runtime_hours"])
    # The box's clock is not this PC's: the agent is stale when the "now" it writes has not changed for 3 minutes of THIS PC's time.
    jn = float(j.get("now") or 0)

    def agent_age(h: dict[str, Any]) -> float:
        seen = h.get("agent_seen") if isinstance(h.get("agent_seen"), dict) else {}
        if not jn or float(seen.get("now") or 0) != jn:
            h["agent_seen"] = seen = {"now": jn, "at": now()}
        return now() - float(seen.get("at") or now())
    age = wd_update(agent_age)
    if age > 180:  # the lease-side agent died or hung: its idle/shutdown fields are stale and must not be trusted
        alert_once("agent-stale", f"the dev box's activity agent has not updated for {age / 60:.0f} min: idle shutdown cannot work "
                   f"(restart attempted; the platform stops it after {max_h:.0f} h)", 3600)
        ssh(d, agent_restart_command(), check=False, timeout=40)
        j = {k: v for k, v in j.items() if k not in ("shutdown_requested", "dist_idle_min", "idle_min")}
    failing = bool(j.get("checkpoint_failed")) and state_repo() != ""
    first = wd_update(lambda h: (h.setdefault("ckpt_failed_since", now()) if failing else h.pop("ckpt_failed_since", None)))
    if failing and now() - float(first) > 2 * float(CFG["checkpoint_minutes"]) * 60:
        alert_once("ckpt-failed", f"checkpoints have been failing for {(now() - float(first)) / 60:.0f} min (state repo unreachable, wrong key or "
                   "refused?): idle shutdown cannot save your work until this is fixed; see /work/results/agent.log on the dev box", 3600)
    held = j.get("shutdown_requested") == "idle" and now() < float(load_state().get("idle_hold_until") or 0)  # CloudDeck snooze
    if held:
        log("watchdog: idle shutdown snoozed from CloudDeck")
    runtime_up = up_h >= max_h - 0.25 or j.get("shutdown_requested") == "max_runtime"
    if (j.get("shutdown_requested") and not held) or runtime_up:
        why = j.get("shutdown_requested") or "max runtime"
        if not state_repo() and not runtime_up and not cfg_flag("idle_discards_workspace", False):
            alert_once("idle-no-ckpt", f"the dev box is idle but checkpoints are disabled, so ddc will not destroy the workspace. It keeps billing until "
                       f"you run `ddc stop --discard-workspace` (or set state_repo; the platform stops it after {max_h:.0f} h)", 3600)
            return "idle, refused (no checkpoints)"
        log(f"watchdog: stopping ({why})")
        # at the runtime limit the platform closes the box anyway, so a disabled-checkpoint workspace is lost either way
        cmd_stop(argparse.Namespace(workers_only=False, force=False, discard_workspace=(not state_repo()) and (runtime_up or cfg_flag("idle_discards_workspace", False)),
                                    lock_wait=120, expect_shutdown=None if runtime_up else j.get("shutdown_requested")))  # idle: abort if the user returns meanwhile
        return f"stop ({why})"
    ws = workers()
    newest_min = min(((now() - float(w.get("lease_at") or now())) / 60 for w in ws), default=0)
    idle_min = min(int(j.get("dist_idle_min") or 0), newest_min)  # a fresh worker gets the full grace period
    if ws and idle_min >= int(CFG["worker_idle_minutes"]) and not str(j.get("active_reason") or "").startswith(("procs", "cpu")):
        log(f"watchdog: workers idle {idle_min:.0f} min; closing workers")
        cmd_stop(argparse.Namespace(workers_only=True, force=False, lock_wait=120))
        return "workers closed"
    return "ok"


def cmd_watchdog(a: argparse.Namespace | None = None) -> int:
    """One scheduled pass (`ddc watchdog --once`; every invocation is one pass). Failure direction: an error never silently leaves the
    box billing. It is logged and alerted, and after WATCHDOG_MAX_FAILS in a row the watchdog tries a safe stop of the dev box that
    only happens if the final checkpoint verifies; otherwise the box keeps running (data first), loudly, until the platform limit."""
    try:
        res = watchdog_pass()
    except Exception as e:
        err = str(e)[:300]
        try:
            n = wd_update(lambda h: h.update(fails=int(h.get("fails") or 0) + 1, last_error=err, last_error_at=now()) or h["fails"])
        except Exception as e0:  # the bookkeeping file must not be able to stop the failure handling (or the safe stop below)
            n = 1
            log(f"warning: cannot update watchdog.json ({e0}); failures cannot be counted")
        log(f"ERROR: watchdog failed ({n} in a row): {e}")
        alert_once("wd-fail", f"the watchdog is failing ({n} in a row): {e}. Idle shutdown and budget enforcement may not be working", 1800)
        if n >= WATCHDOG_MAX_FAILS and (n - WATCHDOG_MAX_FAILS) % 6 == 0:
            try:
                cmd_stop(argparse.Namespace(workers_only=False, force=False, discard_workspace=False, lock_wait=120))
                log("watchdog: safe stop after repeated failures completed (the final checkpoint verified)")
            except Exception as e2:
                alert_once("wd-safe-stop", f"the watchdog has failed {n} times and the safe checkpointed stop did NOT complete ({e2}): the dev box is "
                           "STILL RUNNING and billing until you run `ddc stop` or the platform runtime limit", 3600)
        return 1
    wd_update(lambda h: h.update(fails=0, last_ok=now(), last_result=res))
    return 0


SECRETS_TEMPLATE = """# ddc secrets: never commit or share this file. Remove the leading '#' and fill in a value to enable it.
# Akash Console API key (https://console.akash.network, API Keys). Required.
# AKASH_API_KEY=paste-your-api-key-here
# `ddc install` generates DDC_BACKUP_PASSPHRASE for you. Back this file up: without it checkpoints cannot be decrypted.
# Optional: Claude Code login token to push to the dev box.
# CLAUDE_CODE_OAUTH_TOKEN=
"""


def default_config_path() -> Path:
    return Path(os.environ["DDC_CONFIG"]).expanduser() if os.environ.get("DDC_CONFIG") else user_config_dir(os.environ, HOME, WIN) / "config.toml"


def init_files(target: Path) -> Path:
    """Write the starter config at `target` (when absent) plus the secrets dir / secrets.env / keys dir (an existing secrets.env is kept); returns secrets.env."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        shutil.copyfile(data_file("config.example.toml"), target)
    SECRETS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    KEYS.mkdir(exist_ok=True, mode=0o700)
    sf = SECRETS_DIR / "secrets.env"
    if sf.exists():
        log(f"kept existing {sf}")
    else:
        write_private(sf, SECRETS_TEMPLATE)
    return sf


def cmd_init(a: argparse.Namespace) -> None:
    target = default_config_path()
    if target.exists():
        die(f"{target} already exists; not overwriting it (edit it, or delete it to start over)")
    sf = init_files(target)
    log(f"wrote {target} and {sf}")
    print(f"\nNext: put your AKASH_API_KEY into {sf}, edit [[repos]] in {target}, then run: ddc setup (GPU/CPU jobs only) or ddc install (dev boxes with checkpoints)")


def ensure_state_repo_is_ours(repo: str) -> None:
    """checkpoint.sh force-pushes to the state repo, so `ddc install` refuses an existing repo that holds anything else (a mistyped
    state_repo that names a real project): it must be empty, or have only main/prev with a ddc manifest.json on main."""
    cp = subprocess.run(["gh", "api", f"repos/{repo}/branches", "--jq", ".[].name"], capture_output=True)
    if cp.returncode != 0:
        die(f"cannot list the branches of {repo} to check it is a ddc state repo: {cp.stderr.decode('utf-8', 'replace')[-200:]}")
    names = cp.stdout.decode().split()
    if not names:
        return
    has_manifest = subprocess.run(["gh", "api", f"repos/{repo}/contents/manifest.json?ref=main"], capture_output=True).returncode == 0
    if set(names) - {"main", "prev"} or "main" not in names or not has_manifest:
        die(f"{repo} already holds other content (branches: {', '.join(names)}): it is not a ddc state repo and checkpoints would overwrite it. "
            "Use an empty private repo (or a new name) for state_repo")


def ensure_key(k: str) -> None:
    """Generate the ed25519 key KEYS/k when missing (never replaces one)."""
    if (KEYS / k).exists():
        return
    subprocess.run([SSH_KEYGEN, "-q", "-t", "ed25519", "-N", "", "-C", f"ddc-{k}", "-f", str(KEYS / k)], check=True)
    if os.name == "nt":  # owner-only, as OpenSSH requires: a profile with extra inherited ACEs would otherwise make ssh refuse the key
        try:
            owner_only(KEYS / k)
        except (OSError, subprocess.SubprocessError, DdcError) as e:  # the key check below (and preflight) still reports a refused key
            log(f"warning: could not restrict {KEYS / k} to this account: {e}")
    log(f"generated key {k}")


def cmd_install(a: argparse.Namespace) -> None:
    if not getattr(a, "dry_run", False):
        SECRETS_DIR.mkdir(parents=True, exist_ok=True)
        KEYS.mkdir(exist_ok=True)
    s = load_secrets()
    if not s.get("AKASH_API_KEY"):
        die(f"put AKASH_API_KEY=... into {SECRETS_DIR / 'secrets.env'}")
    repo = state_repo()
    repo_keys = repo_deploy_keys()
    if re.search(r"OWNER|your-private-state-repo", repo):
        die(f"state_repo {repo!r} is the documentation placeholder: set your own private repo, or leave it empty to disable checkpoints")
    plan = ([f"create the private repo {repo} if it does not exist"] if repo else []) + \
        [f"add a WRITE deploy key to {r}" for _, r in ([("state_deploy", repo)] if repo else []) + repo_keys]
    if WIN and getattr(a, "dry_run", False):
        shims = windows_shims(bool(getattr(a, "migrate", False)))
        print("ddc install would write:\n  - " + "\n  - ".join(str(p) for p, _ in shims) if shims else "ddc install writes no shims.")
    if plan:  # the only GitHub writes ddc ever makes: shown first, never silent
        print("ddc install will, using your `gh` login:\n  - " + "\n  - ".join(plan))
        if getattr(a, "dry_run", False):
            return
        if not getattr(a, "yes", False):
            confirm("Continue?")
    elif getattr(a, "dry_run", False):
        print("ddc install makes no GitHub changes with this config.")
        return
    if not s.get("DDC_BACKUP_PASSPHRASE"):
        set_secret("DDC_BACKUP_PASSPHRASE", pysecrets.token_urlsafe(48))
        log("generated DDC_BACKUP_PASSPHRASE (back up secrets.env in your password manager: without it checkpoints cannot be decrypted)")
    for k in ["access", "worker"] + (["state_deploy"] if repo else []) + [k for k, _ in repo_keys]:
        ensure_key(k)
    if (KEYS / "access").is_file() and (bad := key_problem(KEYS / "access")):
        log(f"warning: {bad}")
    if repo:
        if subprocess.run(["gh", "repo", "view", repo], capture_output=True).returncode != 0:
            subprocess.run(["gh", "repo", "create", repo, "--private", "--description", "ddc encrypted checkpoints (gpg AES256 only)"], check=True)
            log(f"created private repo {repo}")
        else:
            ensure_state_repo_is_ours(repo)
    else:
        log("state_repo is empty: checkpoints stay disabled (set it in config.toml and rerun `ddc install` to enable them)")
    for key, r in ([("state_deploy", repo)] if repo else []) + repo_keys:
        existing = subprocess.run(["gh", "api", f"repos/{r}/keys"], capture_output=True, check=True).stdout.decode()
        if pubkey(key).split()[1] not in existing:
            subprocess.run(["gh", "repo", "deploy-key", "add", str(KEYS / f"{key}.pub"), "-R", r, "-w", "-t", f"ddc {key}"], check=True)
            log(f"added write deploy key to {r}")
    meta = json.loads(subprocess.run(["gh", "api", "meta"], capture_output=True, check=True).stdout)
    (KEYS / "github_known_hosts").write_text("".join(f"github.com {k}\n" for k in meta["ssh_keys"]), encoding="utf-8")
    name = subprocess.run(["git", "config", "--global", "user.name"], capture_output=True).stdout.decode().strip()
    email = subprocess.run(["git", "config", "--global", "user.email"], capture_output=True).stdout.decode().strip()
    (KEYS / "gitconfig").write_text(f"[user]\n\tname = {name}\n\temail = {email}\n[init]\n\tdefaultBranch = main\n[pull]\n\trebase = false\n", encoding="utf-8")
    no_watchdog = False
    if WIN:
        install_windows(a.migrate)
    elif not getattr(a, "no_watchdog", False):
        no_watchdog = install_watchdog() in ("unsupported", "none")  # macOS (launchd is not supported) or no scheduler available
    try:
        setup_alerts()
    except DdcError as e:
        log(f"Akash balance alert not configured: {e}")
    log("install complete" + ("; NO watchdog is registered (see the warning above): nothing but the platform runtime limit stops an idle box" if no_watchdog else ""))


TASK = "CloudDeck-Watchdog"
LEGACY_TASK = "DrillDevCloud-Watchdog"  # installs from before the rename; only touched by `ddc install --migrate`


def launcher() -> list[str]:
    """Command that runs this controller: the checkout's ddc.py, or the module for an installed package."""
    return [sys.executable, str(CHECKOUT / "ddc.py")] if CHECKOUT else [sys.executable, "-m", "clouddeck.controller"]


def cmdline(parts: list[str]) -> str:
    return " ".join(f'"{x}"' for x in parts)


def windows_shims(migrate: bool) -> list[tuple[Path, str]]:
    """(path, content) of the %USERPROFILE%\\bin shims install_windows writes: ddc / claude-cloud when absent or with --migrate, and with
    --migrate an existing clouddeck.cmd that points elsewhere (e.g. an old checkout on PYTHONPATH), so `clouddeck` runs this install too."""
    run, bindir, out = launcher(), HOME / "bin", []
    for nm, args in (("claude-cloud.cmd", " attach"), ("ddc.cmd", "")):
        if (bindir / nm).exists() and not migrate:  # never overwrite a shim that may point at another checkout
            log(f"kept existing {bindir / nm}")
        else:
            out.append((bindir / nm, f"@echo off\r\n{cmdline(run)}{args} %*\r\n"))
    cd = bindir / "clouddeck.cmd"  # the content `clouddeck install` writes
    body = "@echo off\r\n" + (f'set "PYTHONPATH={CHECKOUT};%PYTHONPATH%"\r\n' if CHECKOUT else "") + f'"{sys.executable}" -m clouddeck %*\r\n'
    if migrate and cd.exists() and cd.read_bytes() != body.encode():
        out.append((cd, body))
    return out


def install_windows(migrate: bool = False) -> None:
    run = launcher()
    bindir = HOME / "bin"
    bindir.mkdir(exist_ok=True)
    for path, body in windows_shims(migrate):
        path.write_text(body, encoding="utf-8", newline="")
        log(f"wrote {path}")
    desktop = HOME / "Desktop"
    ps = []
    for nm, args in [("CloudDeck Start", "start"), ("CloudDeck Stop", "stop"), ("CloudDeck Status", "status"),
                     ("CloudDeck Benchmark", "bench"), ("CloudDeck Attach", "attach")]:
        if (desktop / (nm + ".lnk")).exists() and not migrate:
            continue
        hold = "" if args == "attach" else " & pause"
        ps.append(f"$s=$w.CreateShortcut('{desktop / (nm + '.lnk')}');$s.TargetPath='cmd.exe';"
                  f"$s.Arguments='/c \"{cmdline(run)} {args}{hold}\"';$s.WorkingDirectory='{CHECKOUT or HOME}';$s.Save()")
    if ps:
        subprocess.run(["powershell", "-NoProfile", "-Command", "$w=New-Object -ComObject WScript.Shell;" + ";".join(ps)], check=True)
    # put ~/bin on the user PATH so `claude-cloud` works in any new PowerShell window
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    f"$p=[Environment]::GetEnvironmentVariable('Path','User'); if (($p -split ';') -notcontains '{bindir}') "
                    f"{{ [Environment]::SetEnvironmentVariable('Path', ($p.TrimEnd(';') + ';{bindir}'), 'User') }}"], check=True)
    if install_windows_task(migrate):
        log(f"installed claude-cloud + ddc into {bindir}, desktop shortcuts, watchdog task {TASK} (every 5 min)")


def install_windows_task(migrate: bool = False) -> bool:
    """Register the CloudDeck-Watchdog scheduled task (every 5 min). False when a legacy task was left in place (no --migrate)."""
    run = launcher()
    pyw = str(Path(run[0]).with_name("pythonw.exe"))
    legacy = subprocess.run(["schtasks", "/Query", "/TN", LEGACY_TASK], capture_output=True).returncode == 0
    if legacy and not migrate:
        log(f"legacy watchdog task {LEGACY_TASK} is present: left untouched and no second task created (`ddc install --migrate` replaces it with {TASK})")
        return False
    cfg = ["--config", str(CONFIG_FILE)] if CONFIG_FILE and CONFIG_FILE.is_file() else []  # the task never depends on config discovery
    tr = f"{cmdline([pyw, *run[1:], *cfg])} watchdog"
    if len(tr) > 261:  # schtasks /TR limit: fail with a reason instead of a bare CalledProcessError (the legacy task, if any, is kept)
        die(f"the watchdog command is {len(tr)} characters, over the 261 schtasks allows; install into a venv or config path with a shorter path:\n  {tr}")
    subprocess.run(["schtasks", "/Create", "/F", "/SC", "MINUTE", "/MO", "5", "/TN", TASK, "/TR", tr], check=True, capture_output=True)
    if legacy:
        subprocess.run(["schtasks", "/Delete", "/F", "/TN", LEGACY_TASK], check=True, capture_output=True)
        log(f"replaced legacy task {LEGACY_TASK} with {TASK}")
    return True

# ---------------------------------------------------------------- watchdog scheduling on Linux / WSL (systemd user timer, else cron)
UNIT = "clouddeck-watchdog"
CRON_BEGIN = "# BEGIN clouddeck-watchdog"
CRON_END = "# END clouddeck-watchdog"


def _watchdog_command() -> list[str]:
    cfg = ["--config", str(CONFIG_FILE)] if CONFIG_FILE and CONFIG_FILE.is_file() else []  # the scheduler never depends on config discovery
    return [*launcher(), *cfg, "watchdog", "--once"]


def _watchdog_env() -> dict[str, str]:
    """The scheduler starts the watchdog with a minimal environment: carry over PATH (ssh, git, gpg) and the DDC_* locations."""
    keys = ("PATH", "DDC_CONFIG", "DDC_STATE_DIR", "DDC_SECRETS_DIR", "DDC_AKASH_API", "DDC_GPG")
    return {k: os.environ[k] for k in keys if os.environ.get(k)}


def _sd_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def _systemd_user() -> bool:
    if not shutil.which("systemctl"):
        return False
    try:
        return subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _unit_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "systemd" / "user"


def _crontab_text() -> str:
    """The user's crontab. Empty ONLY when there is none ("no crontab for ..."): any other failure raises, because writing a crontab
    built from an unreadable one would wipe the user's own entries."""
    try:
        cp = subprocess.run(["crontab", "-l"], capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        raise DdcError(f"cannot read your crontab ({e}): leaving it untouched") from e
    if cp.returncode == 0:
        return cp.stdout.decode("utf-8", "replace")
    err = cp.stderr.decode("utf-8", "replace")
    if cp.returncode == 1 and "no crontab" in err.lower():
        return ""
    raise DdcError(f"`crontab -l` failed (exit {cp.returncode}: {err.strip()[:200]}): leaving your crontab untouched")


def _cron_without_block(text: str) -> str:
    return re.sub(rf"{re.escape(CRON_BEGIN)}.*?{re.escape(CRON_END)}\n?", "", text, flags=re.S)


def _cron_block() -> str:
    env = " ".join(f"{k}={shlex.quote(v)}" for k, v in _watchdog_env().items())
    cmd = " ".join(shlex.quote(x) for x in _watchdog_command())
    line = f"*/5 * * * * env {env} {cmd} >> {shlex.quote(str(STATE_DIR / 'watchdog.log'))} 2>&1".replace("%", "\\%")
    return f"{CRON_BEGIN} (managed by `ddc install`; remove with `ddc uninstall`)\n{line}\n{CRON_END}\n"


def _crontab_set(text: str) -> None:
    subprocess.run(["crontab", "-"], input=text.encode(), check=True, capture_output=True, timeout=30)


def install_watchdog() -> str:
    """Register the 5-minute watchdog (idle shutdown, budget and runtime enforcement) for this user. Idempotent. Returns
    "systemd", "cron", "none" (nothing available; a loud warning was logged) or "unsupported" (macOS)."""
    if PLATFORM == "darwin":
        log("WARNING: no watchdog installed: macOS launchd is not supported by `ddc install`. Schedule "
            f"`{' '.join(_watchdog_command())}` every 5 minutes yourself (launchd StartInterval=300); until then only the platform runtime limit stops spending")
        return "unsupported"
    if os.path.exists("/proc/version") and "microsoft" in Path("/proc/version").read_text(errors="replace").lower():
        log("note (WSL): the watchdog only runs while the WSL VM is awake. To keep it running, also schedule "
            "`wsl.exe -d <distro> -- ddc watchdog --once` every 5 minutes from the Windows Task Scheduler")
    if _systemd_user():
        d = _unit_dir()
        d.mkdir(parents=True, exist_ok=True)
        env = "".join(f"Environment={_sd_quote(f'{k}={v}')}\n" for k, v in _watchdog_env().items())
        (d / f"{UNIT}.service").write_text("[Unit]\nDescription=CloudDeck/ddc watchdog: idle shutdown, runtime and budget enforcement\n\n[Service]\n"
                                           f"Type=oneshot\n{env}ExecStart={' '.join(_sd_quote(x) for x in _watchdog_command())}\n", encoding="utf-8")
        (d / f"{UNIT}.timer").write_text("[Unit]\nDescription=CloudDeck/ddc watchdog every 5 minutes\n\n[Timer]\nOnBootSec=2min\nOnUnitActiveSec=5min\n"
                                         "AccuracySec=30s\n\n[Install]\nWantedBy=timers.target\n", encoding="utf-8")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, capture_output=True, timeout=30)
        subprocess.run(["systemctl", "--user", "enable", "--now", f"{UNIT}.timer"], check=True, capture_output=True, timeout=30)
        log(f"installed the watchdog as the systemd user timer {UNIT}.timer (every 5 min). To keep it running while you are logged out: loginctl enable-linger $USER")
        return "systemd"
    if shutil.which("crontab"):
        old = _crontab_text()
        if old.strip():  # a copy of what was there before we changed it
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            (STATE_DIR / f"crontab.backup-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}").write_text(old, encoding="utf-8")
        base = _cron_without_block(old).rstrip("\n")
        _crontab_set((base + "\n" if base else "") + _cron_block())
        log("installed the watchdog as a user crontab entry (every 5 min; marked block, removable with `ddc uninstall`)")
        return "cron"
    log("WARNING: no watchdog installed: neither a systemd user session nor crontab is available. Run "
        f"`{' '.join(_watchdog_command())}` every 5 minutes some other way; until then only the platform runtime limit stops spending")
    return "none"


def uninstall_watchdog() -> list[str]:
    """Remove what install_watchdog / install_windows registered (keys, config, state and shims stay). Returns what was removed."""
    gone = []
    if WIN:
        for t in (TASK,):
            if subprocess.run(["schtasks", "/Delete", "/F", "/TN", t], capture_output=True, creationflags=NOWIN).returncode == 0:
                gone.append(f"scheduled task {t}")
        if subprocess.run(["schtasks", "/Query", "/TN", LEGACY_TASK], capture_output=True, creationflags=NOWIN).returncode == 0:
            log(f"the legacy task {LEGACY_TASK} is still registered: remove it with `schtasks /Delete /TN {LEGACY_TASK}` if you no longer use that install")
        return gone
    d = _unit_dir()
    if (d / f"{UNIT}.timer").exists() or (d / f"{UNIT}.service").exists():
        if _systemd_user():
            subprocess.run(["systemctl", "--user", "disable", "--now", f"{UNIT}.timer"], capture_output=True, timeout=30)
        for ext in ("timer", "service"):
            (d / f"{UNIT}.{ext}").unlink(missing_ok=True)
        if _systemd_user():
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=30)
        gone.append(f"systemd user timer {UNIT}")
    text = _crontab_text() if shutil.which("crontab") else ""
    if CRON_BEGIN in text:
        _crontab_set(_cron_without_block(text))
        gone.append("crontab block")
    return gone


def _same_path(a: str, b: Path) -> bool:
    try:
        return os.path.normcase(str(Path(a).expanduser().resolve())) == os.path.normcase(str(b.expanduser().resolve()))
    except (OSError, ValueError, RuntimeError):
        return False


def _cmd_args(cmd: str) -> list[str]:
    """The arguments of a scheduled command line (task XML, systemd ExecStart, crontab line), unquoted."""
    if WIN:
        return [a.strip('"') for a in re.findall(r'"[^"]*"|\S+', cmd)]
    try:
        return shlex.split(cmd.replace("%%", "%").replace("\\%", "%"), comments=False)
    except ValueError:
        return cmd.split()


def _watches_this_config(cmd: str, legacy: bool = False) -> bool:
    """A scheduled watchdog only enforces the config it runs with: the path after `--config` must be ours (compared as resolved paths);
    without it the entry discovers its config (the legacy task: the config next to its ddc.py)."""
    if not CONFIG_FILE:
        return True
    args = _cmd_args(cmd)
    for i, a in enumerate(args[:-1]):
        if a == "--config":
            return _same_path(args[i + 1], CONFIG_FILE)
    if not legacy:
        return True
    return any(a.lower().endswith("ddc.py") and _same_path(str(Path(a).parent), CONFIG_FILE.parent) for a in args)


def watchdog_installed() -> bool | None:
    """True / False when this machine's scheduler can be asked, None when it cannot (macOS). A watchdog registered for another config
    (a second install, the legacy task of another checkout) does not count."""
    try:
        if WIN:
            for t in (TASK, LEGACY_TASK):
                p = subprocess.run(["schtasks", "/Query", "/TN", t, "/XML"], capture_output=True, text=True, errors="replace", creationflags=NOWIN, timeout=30)
                cmd = html.unescape(" ".join(re.findall(r"<(?:Command|Arguments)>(.*?)</", p.stdout or "", re.S)))
                if p.returncode == 0 and _watches_this_config(cmd, legacy=t == LEGACY_TASK):
                    return True
            return False
        if PLATFORM == "darwin":
            return None
        unit = _unit_dir() / f"{UNIT}.service"
        if (_unit_dir() / f"{UNIT}.timer").exists() and _systemd_user() and \
                subprocess.run(["systemctl", "--user", "is-enabled", f"{UNIT}.timer"], capture_output=True, timeout=30).returncode == 0:
            ex = re.search(r"^ExecStart=(.*)$", unit.read_text(encoding="utf-8", errors="replace") if unit.exists() else "", re.M)
            if ex and _watches_this_config(ex.group(1).replace("\\\\", "\\")):
                return True
        if not shutil.which("crontab"):
            return False
        cron = _crontab_text()
        if CRON_BEGIN not in cron or CRON_END not in cron:
            return False
        block = cron.split(CRON_BEGIN, 1)[1].split(CRON_END, 1)[0]  # only our marked block: other jobs' lines do not count
        return any(_watches_this_config(line) for line in block.splitlines() if " watchdog" in line and not line.lstrip().startswith("#"))
    except (OSError, subprocess.SubprocessError, DdcError):  # cannot ask (unreadable crontab, no scheduler tool): unknown
        return None


def cmd_uninstall(a: argparse.Namespace) -> None:
    gone = uninstall_watchdog()
    log("removed: " + ", ".join(gone) if gone else "no watchdog registration found")

def cmd_setup(a: argparse.Namespace) -> int:
    """One noninteractive step to 'ready for `ddc job run`': config, secrets.env (+ API key from stdin), access key, optional watchdog; 0 when ready, else 2."""
    key = ""
    if a.api_key_stdin:
        key = sys.stdin.read().strip()
        if len(key) < 8 or not re.fullmatch(r"[!-~]+", key):
            die("the API key read from stdin is empty, too short or contains whitespace/non-printable characters: pipe only the key (Akash Console > API Keys)")
    with contextlib.redirect_stdout(sys.stderr if a.json else sys.stdout):  # --json: stdout carries only the report
        target = CONFIG_FILE or default_config_path()   # an existing config (also a legacy one) is used as is: a new starter file would shadow its limits
        if not target.exists() or not (SECRETS_DIR / "secrets.env").exists():
            init_files(target)
            if CONFIG_ERROR:
                reload_config(str(target))
        sf = SECRETS_DIR / "secrets.env"
        if key:
            lines = [ln for ln in sf.read_text(encoding="utf-8").splitlines() if not re.match(r"\s*AKASH_API_KEY\s*=", ln)]
            write_private(sf, "\n".join(lines + [f"AKASH_API_KEY={key}"]) + "\n")
            log(f"stored AKASH_API_KEY in {sf}")
        ensure_key("access")
        if a.watchdog:
            try:
                if WIN:
                    install_windows_task()
                else:
                    install_watchdog()
            except (DdcError, OSError, subprocess.SubprocessError) as e:
                log(f"watchdog not registered: {e}")
        key_set, valid, bal = bool(load_secrets().get("AKASH_API_KEY")), None, None
        if key_set:
            try:
                bal, valid = balance_usd(), True
            except DdcError as e:
                valid = False if re.search(r"\b(401|403)\b|unauthori|forbidden", str(e), re.I) else None
                log(f"Akash API key {'not accepted' if valid is False else 'not checked (API unreachable?)'}: {str(e)[:200]}")
    ssh_ok = shutil.which(SSH) is not None or Path(SSH).exists()
    cfg_ok = not CONFIG_ERROR and not config_problems()
    access = (KEYS / "access").is_file() and not key_problem(KEYS / "access")
    wd = watchdog_installed()
    ready = bool(cfg_ok and key_set and valid and access and ssh_ok)
    todo = ([f"fix the config: {CONFIG_ERROR or '; '.join(config_problems())}"] if not cfg_ok else []) + \
        ([f"put your Akash Console API key into {sf}, or run: ddc setup --api-key-stdin"] if not key_set else
         [f"the Akash API key was not accepted: check it in {sf}"] if valid is False else
         ["could not reach the Akash API to check the key: check the network and run ddc setup again"] if valid is None else []) + \
        ([] if ssh_ok else ["install OpenSSH (the `ssh` and `ssh-keygen` programs)"]) + \
        ([] if access else [f"the ssh access key in {KEYS} is missing or refused: {key_problem(KEYS / 'access') if (KEYS / 'access').is_file() else 'missing'}"]) + \
        ([] if wd else ["recommended: ddc setup --watchdog (closes a job lease whose runner died; without it only the runtime limit does)"]) + \
        (["run: ddc job hardware --available"] if ready else [])
    out = {"config_file": str(CONFIG_FILE or target), "secrets_file": str(sf), "api_key_set": key_set, "api_key_valid": valid, "balance_usd": bal, "access_key": access,
           "ssh": ssh_ok, "watchdog": wd, "ready_for_jobs": ready, "next_steps": todo}
    if a.json:
        print(json.dumps(out, indent=2))
    else:
        print("\n".join(f"{k}: {v}" for k, v in out.items() if k != "next_steps") + "\n" + ("next steps:\n  - " + "\n  - ".join(todo) if todo else "ready"))
    return 0 if ready else 2


def setup_alerts() -> None:
    chans = api("GET", "/v1/notification-channels").get("data") or []
    if not chans:
        api("POST", "/v1/notification-channels/default", {})
        chans = api("GET", "/v1/notification-channels").get("data") or []
    alerts = api("GET", "/v1/alerts").get("data") or []
    if any(x.get("name") in ("ddc low balance", "drill-dev-cloud low balance") for x in alerts):  # the old name = same alert
        return
    uid = api("GET", "/v1/user/me")["data"]["id"]
    owner = (api("GET", f"/v1/wallets?userId={uid}").get("data") or [{}])[0].get("address")
    floor = float(CFG["reserve_usd"]) + 10
    api("POST", "/v1/alerts", {"data": {"notificationChannelId": chans[0]["id"], "name": "ddc low balance", "enabled": True,
                                        "summary": f"Akash balance below ${floor:.0f}", "description": "ddc reserve guard",
                                        "type": "WALLET_BALANCE", "conditions": {"operator": "lt", "field": "balance", "value": floor * 1e6},
                                        "params": {"owner": owner, "denom": "uact"}}})
    log(f"Akash email alert: wallet balance < ${floor:.0f}")


def reload_config(path: str) -> None:
    """--config: replace the config chosen at import (CFG keeps its identity: CloudDeck holds references to it)."""
    global CONFIG_FILE, CONFIG_ERROR
    CONFIG_FILE = Path(path).expanduser().resolve()
    os.environ["DDC_CONFIG"] = str(CONFIG_FILE)
    cfg, CONFIG_ERROR = load_config(CONFIG_FILE)
    CFG.clear()
    CFG.update(cfg)
    load_custom_profiles()


def main(argv: list[str] | None = None) -> int:
    global APPROVAL, LOG_STDERR
    APPROVAL = None   # an approval never outlives the command it was given to
    LOG_STDERR = "--json" in (sys.argv[1:] if argv is None else argv)
    pre = argparse.ArgumentParser(add_help=False)  # profile choices below depend on the config, so --config is applied first
    pre.add_argument("--config")
    cfg_arg = pre.parse_known_args(argv)[0].config
    if cfg_arg:
        reload_config(cfg_arg)
    p = argparse.ArgumentParser(prog="ddc", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"ddc {__version__}")
    p.add_argument("--config", metavar="PATH", help="config file (default: $DDC_CONFIG, then the user config dir)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def safety_flags(sp: argparse.ArgumentParser, only_help: str) -> None:
        sp.add_argument("--only", action="append", metavar="ID", help=only_help)
        sp.add_argument("--dry-run", action="store_true", help="only list what would happen")
        sp.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")

    sub.add_parser("init", help="write a starter config.toml and secrets.env (never overwrites)")
    s = sub.add_parser("install")
    s.add_argument("--migrate", action="store_true", help=f"Windows: replace the legacy {LEGACY_TASK} task and existing shims/shortcuts")
    s.add_argument("--no-watchdog", action="store_true", help="Linux/WSL: do not register the systemd timer / cron entry")
    s.add_argument("--yes", "-y", action="store_true", help="do not ask before creating the state repo / adding deploy keys on GitHub")
    s.add_argument("--dry-run", action="store_true", help="only list the GitHub actions install would take")
    s = sub.add_parser("setup", help="jobs-only setup in one command: config, secrets, ssh access key, optional watchdog; exit 0 when ready for `ddc job run`")
    s.add_argument("--api-key-stdin", action="store_true", help="read the Akash Console API key from stdin and store it in secrets.env")
    s.add_argument("--watchdog", action="store_true", help="also register the watchdog (Windows: scheduled task; Linux/WSL: systemd user timer or cron)")
    s.add_argument("--json", action="store_true", help="machine-readable report")
    sub.add_parser("uninstall", help="remove the watchdog registration (keys, config and state are kept)")
    s = sub.add_parser("start")
    s.add_argument("-p", "--profile", default=default_profile(), choices=sorted(CFG["profiles"]))
    s.add_argument("-w", "--workers", type=int, default=0)
    s.add_argument("--worker-profile", choices=sorted(CFG["profiles"]))
    s.add_argument("--approve-usd", type=float, metavar="USD", help="the spend the owner approved for this command (required; run without it to see the worst case)")
    s = sub.add_parser("scale")
    s.add_argument("n", type=int)
    s.add_argument("--approve-usd", type=float, metavar="USD", help="scaling up: the spend the owner approved for this command (required; run without it to see the worst case)")
    s.add_argument("--explore", action="store_true", help="one lease per not-yet-benchmarked provider (CPU comparison)")
    s.add_argument("-p", "--profile", default=default_profile(), choices=sorted(CFG["profiles"]))
    safety_flags(s, "when scaling down, close this worker (dseq, name or wNN) instead of the highest-numbered ones")
    s = sub.add_parser("stop")
    s.add_argument("--workers-only", action="store_true")
    s.add_argument("--force", action="store_true", help="close even if the final checkpoint cannot be verified (needs --discard-workspace too: the workspace is lost)")
    s.add_argument("--discard-workspace", action="store_true", help="checkpoints are disabled (empty state_repo): close the dev box anyway and DESTROY its workspace")
    s.add_argument("--allow-shrink", action="store_true", help="let the final checkpoint replace one that is much larger (a workspace was removed on purpose)")
    safety_flags(s, "close only this lease (dseq, name or wNN); the only way to close a lease that is not a dev box/worker")
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true", help="machine-readable: our leases + spend-rate + the account's active deployments, and nothing_billing")
    s = sub.add_parser("attach")
    s.add_argument("window", nargs="?", choices=["claude", "workers", "shell"])
    s = sub.add_parser("bench")
    s.add_argument("workloads", nargs="*", help="cpu and/or cmd (cmd runs bench_cmd from the config)")
    s = sub.add_parser("checkpoint")
    s.add_argument("--allow-shrink", action="store_true", help="let this checkpoint replace one that is much larger")
    s = sub.add_parser("heal", help="re-provision workers whose container restarted")
    s.add_argument("--only", action="append", metavar="ID", help="heal only this worker (dseq, name or wNN)")
    s.add_argument("--dry-run", action="store_true", help="only list what would be re-provisioned")
    sub.add_parser("verify")
    s = sub.add_parser("watchdog", help="one enforcement pass: idle / max-runtime / budget / stale leases (what the scheduler runs)")
    s.add_argument("--once", action="store_true", help="accepted for scheduler entries; every run is exactly one pass")
    s = sub.add_parser("reconcile", help="compare our records with the Akash account; repair a damaged state file (closes only a lease whose create reply was lost)")
    s.add_argument("--restore-backup", action="store_true", help="put state.json.bak back as state.json")
    s.add_argument("--adopt", action="store_true", help="record every unknown ddc-* deployment on the account as ours (so stop can close it)")
    s.add_argument("--only", action="append", metavar="ID", help="with --adopt: adopt only this deployment (dseq or name)")
    s.add_argument("--yes", "-y", action="store_true", help="with --adopt: do not ask for confirmation")
    s = sub.add_parser("ssh")
    s.add_argument("target", nargs="?", default="dev")
    s = sub.add_parser("run", help="run one non-interactive shell command on the dev box (as dev), print its output")
    s.add_argument("command")
    s.add_argument("--timeout", type=int, default=1800)
    s = sub.add_parser("job", help="run a batch job on one short-lived lease, minimal paid idle time (`ddc job --help`)", add_help=False)
    s.add_argument("job_args", nargs=argparse.REMAINDER)   # listed for --help only: `ddc job ...` is handed to the job CLI untouched (argparse cannot pass a leading --help through REMAINDER)
    toks = list(sys.argv[1:] if argv is None else argv)
    while toks[:1] and toks[0].startswith("--config"):
        toks = toks[1:] if "=" in toks[0] else toks[2:]
    a = argparse.Namespace(cmd="job", job_args=toks[1:]) if toks[:1] == ["job"] else p.parse_args(argv)
    try:
        if CONFIG_ERROR and a.cmd not in ("init", "setup") and not (a.cmd == "job" and a.job_args[:1] in (["hardware"], ["workflows"])):   # the public price table and the workflow list/copy need no config
            if a.cmd == "watchdog":  # a scheduled run has nobody watching its exit code: idle/budget enforcement is off until this is fixed
                alert_once("wd-config", f"watchdog NOT running: {CONFIG_ERROR}")
            die(CONFIG_ERROR)
        conflict = state_dir_conflict(os.environ, HOME, WIN)
        if conflict:
            if a.cmd == "watchdog":
                alert_once("wd-state-dir", f"watchdog NOT running: {conflict}")
            if a.cmd in ("start", "scale", "heal", "install", "setup", "stop", "watchdog", "job"):
                die(conflict)
            log("warning: " + conflict)
        if limits_unsafe():
            log("WARNING: unsafe_disable_limits = true: the hard spending ceilings (lease count, price cap, runtime, scale size) are OFF")
        if a.cmd in ("start", "scale", "heal", "install", "bench", "checkpoint"):
            require_config()
        if a.cmd in ("start", "scale"):
            a.cli_paid = True  # only the CLI needs --approve-usd; CloudDeck's TUI asks its own confirmation, the watchdog never creates
        if a.cmd in ("stop", "scale") or (a.cmd == "reconcile" and a.adopt):
            a.confirm_needed = True  # only the CLI asks; the watchdog and CloudDeck call cmd_stop/cmd_scale without it
        if a.cmd == "stop":
            a.include_jobs = not a.only and not a.workers_only  # a plain `ddc stop` is the kill switch: it closes owned job leases too (--only / --workers-only keep their narrower meaning)
        if a.cmd == "attach":
            return cmd_attach(a)
        if a.cmd == "job":
            from .job.cli import main as job_main  # imported on use: the job package imports this module
            return job_main(a.job_args, prog="ddc job")
        if a.cmd == "ssh":
            t = dev_box() if a.target == "dev" else next((w for w in workers() if f"w{w['idx']:02d}" == a.target), None)
            return subprocess.call(ssh_args(t or die(f"no {a.target}"), tty=True))
        if a.cmd == "run":
            d = dev_box() or die("no dev box running")
            cp = subprocess.run(ssh_args(d) + [a.command], timeout=a.timeout)
            return cp.returncode
        if a.cmd == "verify":
            verify_checkpoint()
            return 0
        rc = {"init": cmd_init, "setup": cmd_setup, "install": cmd_install, "uninstall": cmd_uninstall, "start": cmd_start, "scale": cmd_scale, "stop": cmd_stop,
              "status": cmd_status, "bench": cmd_bench, "checkpoint": cmd_checkpoint, "heal": cmd_heal, "watchdog": cmd_watchdog,
              "reconcile": cmd_reconcile}[a.cmd](a)
        return rc if isinstance(rc, int) else 0
    except DdcError as e:
        log(f"ERROR: {e}")
        return 1
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:  # a missing tool or a failed command: a message, not a traceback
        log(f"ERROR: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

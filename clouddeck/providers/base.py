"""The provider contract.  A new cloud only has to implement the five abstract methods below
(offers, quote, instances, deploy, terminate); everything else has a working default or is an
optional capability the UI hides when `capabilities` does not list it."""
from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, ClassVar

from .. import sdlgen
from ..hardware import GpuVariant, HardwareCatalog, Match, Screening, advertised_matches, catalog_from_gpu_models
from ..sdlgen import norm_model  # noqa: F401  (re-exported)

LogFn = Callable[[str], None]
ROLES = ("dev", "worker", "box")
ROLE_LABEL = {"dev": "Dev box (workspace + Claude Code)", "worker": "Worker (fleet node)", "box": "Generic instance (SSH only)"}


class ProviderError(RuntimeError):
    """An operation failed in a way the user can read and act on."""


class Unsupported(ProviderError):
    """The provider does not offer this capability."""


def gi(s: Any) -> float:
    """'64Gi' / '512Mi' / '2G' -> GiB."""
    m = re.fullmatch(r"\s*([\d.]+)\s*([A-Za-z]*)", str(s or ""))
    if not m:
        return 0.0
    n, unit = float(m.group(1)), m.group(2).lower()
    return n / 1024 if unit.startswith("m") else n * 1024 if unit.startswith("t") else n


MODEL_TEXT = re.compile(r"(?=.*[A-Za-z0-9])[A-Za-z0-9 ._-]{1,40}")   # what a person may type for a model: letters, digits, space . _ -  (anything else is refused)
EXTENDED = ("gpu_vendor", "gpu_ram", "gpu_interface", "gpu_models", "cpu_arch", "cpu_vendor", "region", "providers", "audited_only")   # profile keys beyond the original ones


@dataclass(frozen=True)
class Spec:
    """What to rent.  `max_usd_hr` is a hard price cap written into the order (no bid above it can win).  The fields from `gpu_vendor` on
    are optional placement constraints ('' / empty = any); they only ever narrow an order, and every token is a shape-checked id, never a
    value from a fixed list, so hardware that appears on the network later can be ordered without a code change."""
    cpu: int = 8
    mem_gb: int = 16
    disk_gb: int = 50
    gpu: int = 0
    gpu_model: str = ""
    role: str = "box"
    max_usd_hr: float = 0.0
    hours: int = 4          # platform-enforced runtime limit
    preset: str = ""        # named profile in config.toml this spec was derived from
    gpu_vendor: str = ""    # "" = nvidia (what an order without vendor has always meant)
    gpu_ram: str = ""       # "24Gi": VRAM variant ("" = any)
    gpu_interface: str = ""  # "pcie" | "sxm" | other advertised family ("" = any)
    gpu_alt: tuple[str, ...] = ()   # more acceptable models: any of gpu_model + these
    cpu_arch: str = ""      # "x86-64" | "arm-64" ... as providers state it ("" = any)
    cpu_vendor: str = ""    # "intel" | "amd" | "arm" ... as providers state it
    region: str = ""        # location-region token ("" = anywhere)
    providers: tuple[str, ...] = ()   # only accept bids from these provider addresses
    audited_only: bool = False        # only providers signed by the network's auditor

    def validate(self, limits: Mapping[str, Any] | None = None) -> list[str]:
        """Sanity bounds only (what a form field can sensibly hold).  They are NOT the spending limits: those are `Provider.guard`,
        which every deploy / scale / resize goes through, so a spec that passes here can still be refused.  `limits` (config.toml) can move
        the numeric bounds (max_cpu, max_mem_gb, max_disk_gb, max_gpu) within the network's own maxima."""
        b = sdlgen.bounds(limits)
        bad = []
        for name, v, lo, hi in (("CPU", self.cpu, 1, b["max_cpu"]), ("RAM (GB)", self.mem_gb, 1, b["max_mem_gb"]), ("Disk (GB)", self.disk_gb, 1, b["max_disk_gb"]),
                                ("GPU", self.gpu, 0, b["max_gpu"]), ("Hours", self.hours, 1, 48)):
            if not lo <= v <= hi:
                bad.append(f"{name} must be {lo}–{hi}")
        if self.role not in ROLES:
            bad.append(f"role must be one of {', '.join(ROLES)}")
        models = [self.gpu_model, *self.gpu_alt]
        if (any(models) or self.gpu_vendor or self.gpu_ram or self.gpu_interface) and not self.gpu:
            bad.append("a GPU model needs GPU ≥ 1")
        if any(m and not MODEL_TEXT.fullmatch(m) for m in models):
            bad.append("GPU model may only use letters, digits, spaces and . _ -")
        elif self.gpu:
            bad += sdlgen.extension_problems(self.to_profile())
        else:
            bad += sdlgen.placement_problems(self.to_profile(), False)
        if not 0 < self.max_usd_hr <= 100:
            bad.append("price cap must be > $0 and ≤ $100/h")
        return bad

    def label(self) -> str:
        g = f" + {self.gpu}×{self.gpu_model or 'GPU'}" if self.gpu else ""
        return f"{self.cpu} vCPU / {self.mem_gb} GB / {self.disk_gb} GB{g}"

    def placement_label(self) -> str:
        """The constraints beyond size, one line ('' when the spec has none)."""
        bits = []
        if self.gpu:
            v = " ".join(x for x in (self.gpu_vendor, "/".join([m for m in (self.gpu_model, *self.gpu_alt) if m]), self.gpu_ram, self.gpu_interface) if x)
            bits.append(f"GPU {v}" if v else "")
        for k, v in (("arch", self.cpu_arch), ("CPU", self.cpu_vendor), ("region", self.region)):
            bits.append(f"{k} {v}" if v else "")
        bits.append("audited providers only" if self.audited_only else "")
        bits.append(f"provider {', '.join(a[:12] + '…' for a in self.providers)}" if self.providers else "")
        return " · ".join(b for b in bits if b)

    def with_(self, **kw: Any) -> Spec:
        return replace(self, **kw)

    def to_profile(self) -> dict[str, Any]:
        """The profile dict (config.toml / custom_profiles.json shape).  The original keys are always the original ones; extended keys are
        written only when set, so an older reader of the file simply ignores them."""
        pr: dict[str, Any] = {"cpu": self.cpu, "mem": f"{self.mem_gb}Gi", "disk": f"{self.disk_gb}Gi", "max_usd_hr": self.max_usd_hr}
        if self.gpu:
            pr.update(gpu=self.gpu, gpu_model=norm_model(self.gpu_model))
        ext: dict[str, Any] = {"gpu_vendor": self.gpu_vendor.strip().lower(), "gpu_ram": self.gpu_ram.strip(), "gpu_interface": self.gpu_interface.strip().lower(),
                               "gpu_models": [norm_model(m) for m in self.gpu_alt if m], "cpu_arch": self.cpu_arch.strip().lower(),
                               "cpu_vendor": self.cpu_vendor.strip().lower(), "region": self.region.strip().lower(), "providers": [a.strip() for a in self.providers],
                               "audited_only": self.audited_only}
        pr.update({k: v for k, v in ext.items() if v and (self.gpu or not k.startswith("gpu_"))})
        return pr

    @classmethod
    def from_profile(cls, pr: Mapping[str, Any], preset: str = "", **over: Any) -> Spec:
        """The spec a profile describes (a profile without extended keys gives exactly the spec earlier versions built)."""
        kw: dict[str, Any] = dict(cpu=int(pr["cpu"]), mem_gb=int(gi(pr["mem"])), disk_gb=int(gi(pr["disk"])), gpu=int(pr.get("gpu") or 0), gpu_model=pr.get("gpu_model") or "",
                                  max_usd_hr=float(pr["max_usd_hr"]), preset=preset)
        for k in ("gpu_vendor", "gpu_ram", "gpu_interface", "cpu_arch", "cpu_vendor", "region"):
            kw[k] = str(pr.get(k) or "")
        kw["gpu_alt"] = tuple(str(x) for x in pr.get("gpu_models") or ())
        kw["providers"] = tuple(str(x) for x in pr.get("providers") or ())
        kw["audited_only"] = pr.get("audited_only") is True
        kw.update(over)
        return cls(**kw)

    def extended(self) -> bool:
        """Does this spec carry any constraint beyond cpu / ram / disk / gpu count / model?"""
        return bool(self.to_profile().keys() & set(EXTENDED))

    def profile_suffix(self) -> str:
        """Short stable hash of the extended fields: two different placements never share a profile name ('' for a plain spec)."""
        ext = {k: v for k, v in self.to_profile().items() if k in EXTENDED}
        return hashlib.sha1(json.dumps(ext, sort_keys=True).encode()).hexdigest()[:6] if ext else ""


@dataclass(frozen=True)
class Verdict:
    """The spend guard's answer for one planned deploy / scale / resize."""
    ok: bool
    text: str                          # one human line: why it passes, or what blocks it
    ceiling_usd: float = 0.0           # the most it can cost: price cap x runtime hours x count
    guard_on: bool | None = None       # budget guard enabled?  None: this provider has no budget guard


def judge(spec: Spec, n: int, limits: Mapping[str, Any], *, leases: int = 0, workers: int = 0, worst: float = 0.0, committed: float = 0.0,
          balance: float | None = None, hours: float | None = None) -> Verdict:
    """Pure limit check shared by providers (the numbers come from the provider's own view of the account).  `leases` / `workers` are what is
    alive now, `worst` the worst case of everything already planned (spent + still committed), `committed` the part not yet spent.
    Mirrors the controller's can_afford; any limit that is not a finite number refuses (fail closed), and only a literal `false` turns
    the budget guard off."""
    hours = spec.hours if hours is None else hours
    ceiling = spec.max_usd_hr * hours * n
    guard_on = limits.get("budget_guard", True) is not False

    def no(why: str) -> Verdict:
        return Verdict(False, why, ceiling, guard_on)

    if bad := spec.validate(limits):
        return no("; ".join(bad))
    if not isinstance(n, int) or n < 1:
        return no("count must be at least 1")
    for key in ("budget_usd", "reserve_usd", "max_total_leases", "max_workers", "max_usd_hr_hard", "max_hours_hard", "max_batch"):
        if key in limits:
            try:
                ok = math.isfinite(float(limits[key])) and float(limits[key]) >= 0
            except (TypeError, ValueError):
                ok = False
            if not ok:
                return no(f"{key} in config.toml is not a valid number: refusing to spend")
    if "max_usd_hr_hard" in limits and spec.max_usd_hr > float(limits["max_usd_hr_hard"]):
        return no(f"price cap ${spec.max_usd_hr:.2f}/h is above max_usd_hr_hard (${float(limits['max_usd_hr_hard']):.2f}/h); raise max_price_usd_hr in config.toml to allow more")
    if "max_hours_hard" in limits and hours > float(limits["max_hours_hard"]):
        return no(f"a {hours:g} h runtime limit is above the hard ceiling of {float(limits['max_hours_hard']):g} h")
    if "max_batch" in limits and n > float(limits["max_batch"]):
        return no(f"{n} instances in one go is above the hard ceiling of {int(float(limits['max_batch']))}")
    if "max_total_leases" in limits and leases + n > float(limits["max_total_leases"]):
        return no(f"would be {leases + n} leases; max_total_leases is {int(float(limits['max_total_leases']))}")
    if spec.role == "worker" and "max_workers" in limits and workers + n > float(limits["max_workers"]):
        return no(f"would be {workers + n} workers; max_workers is {int(float(limits['max_workers']))}")
    if not guard_on:
        return Verdict(True, f"budget guard is OFF: only the price cap and runtime limit bound this (ceiling ${ceiling:.2f})", ceiling, False)
    budget, reserve = float(limits.get("budget_usd", 0)), float(limits.get("reserve_usd", 0))
    if balance is None:
        return no("account balance unknown (cannot reach the provider API): not spending without a budget check")
    if worst + ceiling > budget:
        return no(f"would exceed budget: worst-case ${worst + ceiling:.2f} > ${budget:.2f}")
    if balance - committed - ceiling < reserve:
        return no(f"would breach reserve: balance ${balance:.2f} - committed ${committed + ceiling:.2f} < ${reserve:.2f}")
    return Verdict(True, f"ok: worst-case ${worst + ceiling:.2f} of ${budget:.2f} budget, balance after ${balance - committed - ceiling:.2f}", ceiling, True)


@dataclass
class Offer:
    """A host/provider you can rent from."""
    id: str
    name: str
    host: str = ""
    region: str = ""
    uptime: float = 0.0              # 0..1 over 7 days
    audited: bool = False
    cpu_free: float = 0.0            # cores
    mem_free_gb: float = 0.0
    disk_free_gb: float = 0.0
    gpus: list[str] = field(default_factory=list)
    gpu_free: int = 0
    cpu_model: str = ""
    mem_type: str = ""
    usd_per_vcpu_hr: float | None = None   # observed from our own past leases here
    speed: float | None = None             # relative single-thread speed (1.0 = reference)
    # what the provider STATES about its hardware, as the SDL placement matches it ('' / () = not stated: unknown, never inferred)
    cpu_vendor: str = ""
    cpu_arch: str = ""
    region_code: str = ""                  # location-region attribute
    gpu_variants: tuple[GpuVariant, ...] = ()


@dataclass
class GpuModel:
    vendor: str
    model: str
    ram: str
    available: int
    total: int
    providers: int
    price_min: float
    price_avg: float
    price_max: float           # USD per GPU-hour
    interface: str = ""


@dataclass
class Quote:
    usd_hr: float                                   # expected price/hour for the spec
    cap_usd_hr: float                               # suggested hard cap
    reference: dict[str, float] = field(default_factory=dict)   # comparable on-demand $/h, e.g. aws/gcp/azure
    basis: str = ""
    notes: list[str] = field(default_factory=list)
    cpu_ref_hr: float | None = None                 # market reference for the CPU / RAM / disk part alone (None = unknown)
    gpu_low_hr: float | None = None                 # market band for the whole GPU part (None = no GPU or no price)
    gpu_high_hr: float | None = None
    source: str = ""                                # where `usd_hr` comes from: market-average | provider-specific | none


@dataclass
class Instance:
    id: str
    name: str
    role: str
    state: str                      # running | provisioning | closed | unknown
    provider: str
    host: str = ""
    port: int = 22
    location: str = ""
    cpu: int = 0
    mem_gb: float = 0
    disk_gb: float = 0
    gpu: int = 0
    gpu_model: str = ""
    usd_hr: float = 0.0
    started_at: float = 0.0
    limit_h: float = 0.0
    spent_usd: float = 0.0
    managed: bool = True            # False: visible but never changeable from here
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def uptime_h(self) -> float:
        return max(0.0, (time.time() - self.started_at) / 3600) if self.started_at else 0.0

    @property
    def remaining_h(self) -> float | None:
        return max(0.0, self.limit_h - self.uptime_h) if self.limit_h and self.started_at else None

    @property
    def reachable(self) -> bool:
        return bool(self.managed and self.state == "running" and self.host)

    def short(self) -> str:
        return f"{self.name} [{self.id[-6:]}]"


@dataclass
class Spend:
    balance: float | None = None
    burn_hr: float = 0.0
    accrued_running: float = 0.0     # spent so far by what is running now
    closed_total: float = 0.0        # accumulated, from closed leases
    today: float = 0.0
    worst_case: float = 0.0          # if everything runs to its platform limit
    daily: list[tuple[str, float]] = field(default_factory=list)   # (YYYY-MM-DD, USD), oldest first
    by_role: dict[str, float] = field(default_factory=dict)
    by_provider: dict[str, float] = field(default_factory=dict)
    budget_guard: bool | None = None
    budget: float | None = None

    @property
    def projected_24h(self) -> float:
        return self.burn_hr * 24


@dataclass
class IdleState:
    idle_min: int = 0
    stop_at_min: int = 0
    active_reason: str = ""
    uptime_min: int = 0
    last_checkpoint: str = ""
    checkpoint_failed: bool = False
    shutdown_requested: str = ""
    held_until: float = 0.0


@dataclass
class CheckpointInfo:
    created_at: str
    reason: str
    repos: dict[str, str]            # name -> HEAD
    entries: dict[str, int]
    path: str = ""


class Provider(ABC):
    id: ClassVar[str]
    label: ClassVar[str]
    capabilities: ClassVar[frozenset[str]] = frozenset()   # gpu fleet ssh spend workspace checkpoint heal resize snooze

    # ---- required
    @abstractmethod
    def offers(self) -> list[Offer]: ...

    @abstractmethod
    def quote(self, spec: Spec) -> Quote: ...

    @abstractmethod
    def instances(self, refresh: bool = False) -> list[Instance]:
        """Current instances.  refresh=True may hit the network; False must be fast (local state only)."""

    @abstractmethod
    def deploy(self, spec: Spec, log: LogFn = print) -> Instance: ...

    @abstractmethod
    def terminate(self, instance_id: str, log: LogFn = print, *, discard_workspace: bool = False) -> float:
        """Close the instance and return what it cost.  A provider that checkpoints the dev box refuses to close it when checkpoints are
        switched off, unless `discard_workspace` says the unsaved workspace may be destroyed (only passed after the user typed that)."""

    # ---- optional, with defaults
    def has(self, cap: str) -> bool:
        return cap in self.capabilities

    def need(self, cap: str) -> None:
        if cap not in self.capabilities:
            raise Unsupported(f"{self.label} does not support '{cap}'")

    def gpu_models(self) -> list[GpuModel]:
        return []

    # ---- hardware discovery (capability "hardware"; "screen" for the explicit availability check)
    def catalog(self) -> HardwareCatalog:
        """What can be ordered: GPU kinds with free counts, CPU architectures, regions.  Default: derived from `gpu_models()`."""
        return catalog_from_gpu_models(self.gpu_models())

    def matches(self, spec: Spec, capacity: bool = True) -> list[Match]:
        """Offers whose ADVERTISED data fits the spec (no network beyond what `offers()` / `catalog()` already cache)."""
        try:
            cpu_hr = self.quote(spec.with_(gpu=0, gpu_model="")).cpu_ref_hr
        except Exception:   # no price reference (offline, unsupported): the rows just carry no estimate
            cpu_hr = None
        return advertised_matches(spec, self.offers(), self.catalog(), cpu_hr, capacity)

    def screen(self, spec: Spec) -> Screening:
        """Explicit availability check: which providers could take exactly this order (capacity + attributes, unpriced).  Never deploys."""
        raise Unsupported(f"{self.label} cannot check availability")

    def problems(self, spec: Spec) -> list[str]:
        """Everything wrong with a spec before it is offered for review."""
        return spec.validate()

    def balance(self) -> float | None:
        return None

    def spend(self) -> Spend:
        return Spend(burn_hr=sum(i.usd_hr for i in self.instances() if i.managed))

    def guard(self, spec: Spec, n: int = 1, replacing: Instance | None = None) -> Verdict:
        """May `n` more instances of `spec` be started (replacing `replacing`, which is closed first)?  Providers with spending limits
        override this; deploy, scale and resize all call `require`, so the limits cannot be bypassed by a different entry point."""
        ceiling = spec.max_usd_hr * spec.hours * n
        if bad := spec.validate():
            return Verdict(False, "; ".join(bad), ceiling)
        return Verdict(True, f"no budget guard in this provider; ceiling ${ceiling:.2f}", ceiling)

    def require(self, spec: Spec, n: int = 1, replacing: Instance | None = None) -> Verdict:
        v = self.guard(spec, n, replacing)
        if not v.ok:
            raise ProviderError(v.text)
        return v

    def checkpoint_enabled(self) -> bool:
        """Will terminating the workspace box save its work?  (A provider can have checkpoints and still have them switched off.)"""
        return self.has("checkpoint")

    def presets(self) -> dict[str, Spec]:
        """Named ready-made specs (for Akash: the profiles in config.toml)."""
        return {}

    def ssh_command(self, inst: Instance, tty: bool = False) -> list[str]:
        raise Unsupported(f"{self.label} has no SSH access")

    def run(self, inst: Instance, cmd: str, timeout: int = 60) -> str:
        """Run a shell command on the instance and return stdout (raises ProviderError on failure)."""
        argv = self.ssh_command(inst) + [cmd]
        try:
            cp = subprocess.run(argv, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise ProviderError(f"{inst.name}: timed out after {timeout}s") from e
        if cp.returncode != 0:
            raise ProviderError(f"{inst.name}: exit {cp.returncode}: {cp.stderr.decode('utf-8', 'replace')[-300:].strip()}")
        return cp.stdout.decode("utf-8", "replace")

    def stream(self, inst: Instance, cmd: str, on_line: Callable[[str], None], stop: Any = None, timeout: int = 3600) -> int:
        """Run a command and deliver output line by line.  Returns the exit code."""
        try:
            out = self.run(inst, cmd, timeout=timeout)
        except ProviderError as e:
            on_line(f"! {e}")
            return 1
        for line in out.splitlines():
            on_line(line)
        return 0

    def scale_victims(self, n: int) -> list[Instance]:
        """The workers `scale(n, ...)` would close (the newest ones), so a UI can name them before asking."""
        have = sorted((i for i in self.instances() if i.role == "worker" and i.managed), key=lambda i: i.started_at)
        return have[n:]

    def scale(self, n: int, spec: Spec, log: LogFn = print) -> list[Instance]:
        """Make the number of workers equal n (generic: deploy / terminate one by one)."""
        have = sorted((i for i in self.instances(True) if i.role == "worker" and i.managed), key=lambda i: i.started_at)
        if n > len(have):
            self.require(spec.with_(role="worker"), n - len(have))
        for inst in have[n:]:
            self.terminate(inst.id, log)
        for _ in range(max(0, n - len(have))):
            self.deploy(spec.with_(role="worker"), log)
        return [i for i in self.instances(True) if i.role == "worker"]

    def stop_all(self, log: LogFn = print, *, discard_workspace: bool = False) -> None:
        for inst in self.instances(True):
            if inst.managed:
                self.terminate(inst.id, log, **({"discard_workspace": True} if discard_workspace else {}))

    # workspace / recovery capabilities
    def checkpoint(self, log: LogFn = print) -> CheckpointInfo:
        raise Unsupported(f"{self.label} has no checkpoints")

    def checkpoints(self) -> list[CheckpointInfo]:
        return []

    def verify(self, log: LogFn = print) -> CheckpointInfo:
        """Prove the newest checkpoint can be restored (fetch, decrypt and compare it on this machine)."""
        raise Unsupported(f"{self.label} has no checkpoints")

    def heal(self, log: LogFn = print) -> list[str]:
        raise Unsupported(f"{self.label} cannot heal workers")

    def idle_state(self) -> IdleState | None:
        return None

    def hold_idle(self, minutes: int) -> float:
        raise Unsupported(f"{self.label} has no idle shutdown to snooze")

    def resize(self, inst: Instance, spec: Spec, log: LogFn = print, *, discard_workspace: bool = False) -> Instance:
        """Replace an instance with one of a different size.  Providers that can resize in place override this."""
        self.need("resize")
        if inst.role == "job":
            raise ProviderError("a job lease is owned by its `ddc job` run and cannot be resized or replaced")
        self.require(spec.with_(role=inst.role), 1, replacing=inst)   # before anything is closed: a refused resize must leave the old instance alone
        self.terminate(inst.id, log, **({"discard_workspace": True} if discard_workspace else {}))
        return self.deploy(spec.with_(role=inst.role), log)

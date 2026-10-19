"""In-memory demo provider: `clouddeck --provider demo`.  Nothing here touches a network or spends money, so every
screen and destructive action can be tried (and tested) safely.  It is also the smallest example of a provider."""
from __future__ import annotations

import os
import random
import re
import shlex
import time
from collections.abc import Callable
from typing import Any

from .. import hardware
from ..hardware import GpuVariant
from .base import CheckpointInfo, GpuModel, IdleState, Instance, LogFn, Offer, Provider, ProviderError, Quote, Spec, Spend, Verdict, judge

USD_PER_VCPU_HR = 0.0035
GPUS = [("nvidia", "rtx4090", "24Gi", 31, 54, 6, 0.38, 0.52, 0.71), ("nvidia", "a100", "80Gi", 64, 192, 4, 1.07, 1.49, 1.91),
        ("nvidia", "h100", "80Gi", 12, 64, 3, 1.9, 2.3, 2.9), ("nvidia", "v100", "32Gi", 8, 10, 2, 0.2, 0.23, 0.25)]

# A deterministic hardware market in the shape of the real public APIs (so the real catalogue code builds it): several VRAM / interface variants of
# one model, an AMD vendor, a model nobody has heard of yet ("zx9000", no price) and models that are supported but not offered right now.
_SUPPORTED = [("amd", "AMD", [("mi100", "MI100", ["32Gi"], ["pcie"]), ("mi60", "MI60", ["32Gi"], ["pcie"])]),
              ("nvidia", "NVIDIA", [("rtx4090", "RTX 4090", ["24Gi"], ["pcie"]), ("a100", "A100", ["40Gi", "80Gi"], ["pcie", "sxm"]), ("h100", "H100", ["80Gi"], ["sxm"]),
                                    ("v100", "V100", ["16Gi", "32Gi"], ["pcie", "sxm"]), ("b300", "B300", ["288Gi"], ["sxm"])])]
# (vendor, model, ram, interface, free, total, providers, max GPUs on one node, price min / avg / max or None)
_MARKET = [("nvidia", "rtx4090", "24Gi", "PCIe", 31, 54, 6, 8, (0.38, 0.52, 0.71)), ("nvidia", "a100", "80Gi", "SXM4", 64, 192, 4, 8, (1.07, 1.49, 1.91)),
           ("nvidia", "a100", "40Gi", "PCIe", 10, 32, 2, 4, (0.80, 1.10, 1.40)), ("nvidia", "h100", "80Gi", "SXM5", 12, 64, 3, 8, (1.9, 2.3, 2.9)),
           ("nvidia", "v100", "32Gi", "PCIe", 8, 10, 2, 4, (0.2, 0.23, 0.25)), ("nvidia", "v100", "16Gi", "SXM2", 4, 8, 1, 4, (0.15, 0.18, 0.2)),
           ("amd", "mi100", "32Gi", "PCIe", 6, 8, 1, 4, (0.45, 0.6, 0.8)), ("nvidia", "zx9000", "48Gi", "PCIe", 2, 2, 1, 2, None)]


def _market_payloads() -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    models = [{"name": v, "displayName": d, "models": [{"name": m, "displayName": md, "memory": mem, "interface": itf} for m, md, mem, itf in ms]} for v, d, ms in _SUPPORTED]
    prices = {"models": [{"vendor": v, "model": m, "ram": r, "interface": i, "availability": {"available": f, "total": t}, "providerAvailability": {"total": n},
                          "price": {"min": p[0], "avg": p[1], "max": p[2]} if p else None} for v, m, r, i, f, t, n, _, p in _MARKET]}
    by: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for v, m, r, i, f, _t, n, mx, _ in _MARKET:
        by.setdefault((v, m), []).append({"memory": r, "interface": hardware.norm_interface(i), "providerCount": n, "availableUnits": f, "maxNodeFreeUnits": mx})
    vendors: dict[str, list[dict[str, Any]]] = {}
    for (v, m), vs in by.items():
        vendors.setdefault(v, []).append({"name": m, "variants": [{"memory": None, "interface": None, "providerCount": max(x["providerCount"] for x in vs),
                                                                   "availableUnits": sum(x["availableUnits"] for x in vs), "maxNodeFreeUnits": max(x["maxNodeFreeUnits"] for x in vs)}, *vs]})
    return models, {"gpus": [{"vendor": v, "models": ms} for v, ms in vendors.items()]}, prices


class DemoProvider(Provider):
    id = "demo"
    label = "Demo (simulated)"
    capabilities = frozenset({"gpu", "fleet", "ssh", "spend", "workspace", "checkpoint", "heal", "resize", "snooze", "hardware", "screen"})

    def __init__(self, seed: int = 7, latency: float | None = None, populate: bool = True, hw_state: str = "ok") -> None:
        self.hw_state = hw_state                  # "ok" | "stale" (catalogue from 3 h ago) | "down" (offline, nothing cached): to try the offline paths
        self.rng = random.Random(seed)
        self.latency = float(os.environ.get("CLOUDDECK_DEMO_LATENCY", 0.15)) if latency is None else latency
        self._n = 1000
        self._items: dict[str, Instance] = {}
        self.windows = [(0, "claude", True), (1, "workers", False), (2, "shell", False)]
        self.held_until = 0.0
        self.checkpoint_log: list[CheckpointInfo] = []
        self.closed_cost = 12.40
        self.calls: list[tuple[str, Any]] = []   # what the UI asked for, for tests
        self.checkpoints_on = True                # like a set / empty state_repo
        self.limits: dict[str, Any] = {"budget_guard": True, "budget_usd": 85.0, "reserve_usd": 15.0, "max_total_leases": 8, "max_workers": 6}   # the simulated guard
        if populate:
            self._add(Spec(cpu=32, mem_gb=64, disk_gb=100, role="dev", max_usd_hr=0.9, hours=8), 3.4, "provider-a.example")
            for i, host in enumerate(["provider-b.example", "provider-c.example", "provider-d.example"], 1):
                self._add(Spec(cpu=32, mem_gb=64, disk_gb=100, role="worker", max_usd_hr=0.9, hours=4), 0.6 + i * 0.2, host, idx=i)
            self.checkpoint_log.append(CheckpointInfo("2026-10-16T20:41:00Z", "periodic", {"web-app": "3f9a1c2", "api-server": "88d0e4b"}, {"web-app": 5214, "api-server": 9120}))

    # ------------------------------------------------------------ helpers
    def _wait(self, factor: float = 1.0) -> None:
        time.sleep(self.latency * factor)

    def _add(self, spec: Spec, age_h: float, host: str, idx: int = 0) -> Instance:
        self._n += 1
        tag = f"w{idx:02d}" if spec.role == "worker" else spec.role
        usd = round(USD_PER_VCPU_HR * spec.cpu * self.rng.uniform(0.8, 1.6) + 0.5 * spec.gpu, 4)
        inst = Instance(id=str(1792000000000 + self._n), name=f"ddc-{tag}-{spec.cpu}-{self._n}", role=spec.role, state="running", provider=self.id,
                        host=f"{host}.example", port=30000 + self._n, location=host, cpu=spec.cpu, mem_gb=spec.mem_gb, disk_gb=spec.disk_gb, gpu=spec.gpu,
                        gpu_model=spec.gpu_model, usd_hr=usd, started_at=time.time() - age_h * 3600, limit_h=spec.hours,
                        meta={"idx": idx or None, "demo": True})
        inst.spent_usd = usd * age_h
        self._items[inst.id] = inst
        return inst

    def _get(self, instance_id: str) -> Instance:
        try:
            return self._items[instance_id]
        except KeyError:
            raise ProviderError(f"no instance {instance_id}") from None

    # ------------------------------------------------------------ contract
    def offers(self) -> list[Offer]:
        regions = [("Frankfurt, DE", "alpha"), ("Stockholm, SE", "bravo"), ("Florida, US", "charlie"), ("Kyiv, UA", "delta"),
                   ("Helsinki, FI", "echo"), ("Texas, US", "foxtrot")]
        if self.hw_state == "down":
            raise ProviderError("provider list unavailable (simulated offline)")
        codes = ["eu-central", "eu-north", "na-us-southeast", "eastern-europe", "eu-north", "na-us-southwest"]
        gpus = {2: ("rtx4090", [GpuVariant("nvidia", "rtx4090", "24Gi", "pcie"), GpuVariant("nvidia", "zx9000", "48Gi", "pcie")]),
                4: ("a100,h100,mi100", [GpuVariant("nvidia", "a100", "80Gi", "sxm"), GpuVariant("nvidia", "h100", "80Gi", "sxm"), GpuVariant("amd", "mi100", "32Gi", "pcie")])}
        out = []
        for i, (reg, name) in enumerate(regions):
            out.append(Offer(id=f"akash1demo{i:02d}", name=name, host=f"provider.{name}.example", region=reg, uptime=0.97 + 0.03 * (i % 2),
                             audited=i % 2 == 0, cpu_free=40 + 25 * i, mem_free_gb=200 + 90 * i, disk_free_gb=2000 + 500 * i,
                             gpus=gpus[i][0].split(",") if i in gpus else [], gpu_free=(3 if i == 2 else 8 if i == 4 else 0),
                             cpu_model="Ampere Altra Q80-30" if i == 5 else "AMD EPYC 7713P" if i % 2 else "Intel Xeon Gold", mem_type="ddr4" if i % 2 else "ddr5", speed=0.8 + 0.1 * i,
                             usd_per_vcpu_hr=USD_PER_VCPU_HR * (1 + 0.1 * i) if i < 4 else None,
                             cpu_vendor="arm" if i == 5 else "amd" if i % 2 else "intel", cpu_arch="arm-64" if i == 5 else "x86-64", region_code=codes[i],
                             gpu_variants=tuple(gpus[i][1]) if i in gpus else ()))
        return out

    # ------------------------------------------------------------ hardware discovery (deterministic, offline)
    def catalog(self) -> hardware.HardwareCatalog:
        if self.hw_state == "down":
            return hardware.HardwareCatalog(sources={"demo": "failed: network unreachable (simulated)"}, errors=["demo: failed: network unreachable (simulated)"])
        models, placement, prices = _market_payloads()
        cat = hardware.build_catalog(None, models, placement, prices, sources={"demo": "ok"})
        offers = self.offers()
        for attr, key in (("cpu_archs", "cpu_arch"), ("cpu_vendors", "cpu_vendor"), ("regions", "region_code")):
            counts: dict[str, int] = {}
            for o in offers:
                if getattr(o, key):
                    counts[getattr(o, key)] = counts.get(getattr(o, key), 0) + 1
            setattr(cat, attr, counts)
        cat.providers_online, cat.auditors = len(offers), ["akash1demoauditor"]
        if self.hw_state == "stale":
            cat.stale, cat.fetched_at, cat.sources = True, time.time() - 3 * 3600, {"demo": "cached 3.0 h ago"}
        return cat

    def screen(self, spec: Spec) -> hardware.Screening:
        self.calls.append(("screen", spec))
        if self.hw_state == "down":
            return hardware.Screening([], at=time.time(), ok=False, error="network unreachable (simulated)")
        self._wait()
        adv = self.matches(spec)
        # deterministic stand-in for the network's node-level answer: every advertised fit except the last one has a node that can take the order
        reply = {"providers": [{"owner": m.offer.id, "hostUri": f"https://{m.offer.host}:8443", "isAudited": m.offer.audited, "location": m.offer.region_code,
                                "organization": m.offer.name, "availableGpus": m.offer.gpu_free, "incidents": [{"date": "2026-10-16", "incidentCount": i}]}
                               for i, m in enumerate(adv[:-1] if len(adv) > 1 else adv)]}
        s = hardware.merge_screening(spec, adv, reply, self.offers(), self.catalog(), self.quote(spec.with_(gpu=0, gpu_model="", gpu_alt=())).cpu_ref_hr)
        s.at = time.time()
        return s

    def gpu_models(self) -> list[GpuModel]:
        return [GpuModel(*g) for g in GPUS]

    def quote(self, spec: Spec) -> Quote:
        px = hardware.gpu_unit_price(spec, self.catalog()) if spec.gpu else None
        gpu = (px[1] if px else next((g[7] for g in GPUS if g[1] == spec.gpu_model), 0.5)) * spec.gpu
        est = USD_PER_VCPU_HR * spec.cpu + 0.0004 * spec.mem_gb + gpu
        akash = est * 2.2
        return Quote(usd_hr=est, cap_usd_hr=round(akash + 0.004, 2), reference={"akash": akash, "aws": akash * 4.9, "gcp": akash * 5.4, "azure": akash * 5.8},
                     basis="simulated pricing", cpu_ref_hr=(USD_PER_VCPU_HR * spec.cpu + 0.0004 * spec.mem_gb) * 2.2, source="market-average",
                     gpu_low_hr=px[0] * spec.gpu if px else None, gpu_high_hr=px[2] * spec.gpu if px else None)

    def presets(self) -> dict[str, Spec]:
        return {"16": Spec(16, 32, 100, role="box", max_usd_hr=0.5, preset="16"), "32": Spec(32, 64, 100, role="box", max_usd_hr=0.9, preset="32"),
                "64": Spec(64, 128, 150, role="box", max_usd_hr=1.7, preset="64")}

    def instances(self, refresh: bool = False) -> list[Instance]:
        if refresh:
            self._wait(0.5)
        order = {"dev": 0, "worker": 1, "box": 2}
        for i in self._items.values():
            i.spent_usd = i.usd_hr * i.uptime_h
        mine = sorted(self._items.values(), key=lambda i: (order.get(i.role, 9), i.meta.get("idx") or 0, i.started_at))
        foreign = Instance(id="1000000000001", name="other-service+db+web", role="external", state="running", provider=self.id, cpu=2, mem_gb=5,
                          disk_gb=21, usd_hr=0.016, managed=False)
        return mine + [foreign]

    def deploy(self, spec: Spec, log: LogFn = print) -> Instance:
        self.calls.append(("deploy", spec))
        self.require(spec)
        if spec.role == "dev" and any(i.role == "dev" for i in self._items.values()):
            raise ProviderError("a dev box is already running: terminate or resize it instead")
        if spec.role == "worker" and not any(i.role == "dev" for i in self._items.values()):
            raise ProviderError("start the dev box first: workers are driven from it")
        log(f"demo: creating deployment for {spec.label()} (cap ${spec.max_usd_hr:.2f}/h)")
        self._wait(2)
        log("demo: 4 acceptable bids; chose provider-d.example")
        self._wait(2)
        idx = 0
        if spec.role == "worker":
            idx = next(i for i in range(1, 100) if i not in {x.meta.get("idx") for x in self._items.values()})
        inst = self._add(spec, 0.0, "provider-d.example", idx)
        log(f"demo: {inst.name} ssh ready")
        return inst

    def terminate(self, instance_id: str, log: LogFn = print, *, discard_workspace: bool = False) -> float:
        self.calls.append(("terminate", instance_id))
        if instance_id not in self._items:
            raise ProviderError("refusing to close an instance CloudDeck did not create")
        inst = self._get(instance_id)
        if discard_workspace:
            self.calls.append(("discard", instance_id))
        if inst.role == "dev" and not self.checkpoints_on and not discard_workspace:   # what the real controller does with an empty state_repo
            raise ProviderError("checkpoints are disabled: closing the dev box would DESTROY its workspace; pass discard_workspace to close it anyway")
        if inst.role == "dev":
            log("demo: final checkpoint (verified)" if self.checkpoints_on else "demo: checkpoints are off: workspace discarded")
            self._wait(2)
            for w in [i for i in self._items.values() if i.role == "worker"]:
                self._items.pop(w.id)
        self._wait()
        cost = inst.usd_hr * inst.uptime_h
        self.closed_cost += cost
        self._items.pop(instance_id)
        log(f"demo: closed {inst.name}, cost ${cost:.3f}")
        return cost

    def stop_all(self, log: LogFn = print, *, discard_workspace: bool = False) -> None:
        for inst in [i for i in self.instances(True) if i.managed]:
            if inst.id in self._items:   # closing the dev box already closed its workers
                self.terminate(inst.id, log, discard_workspace=discard_workspace)

    def scale(self, n: int, spec: Spec, log: LogFn = print) -> list[Instance]:
        self.calls.append(("scale", n))
        return super().scale(n, spec, log)

    def checkpoint_enabled(self) -> bool:
        return self.checkpoints_on

    def balance(self) -> float | None:
        return 63.64 - sum(i.spent_usd for i in self._items.values())

    def guard(self, spec: Spec, n: int = 1, replacing: Instance | None = None) -> Verdict:
        mine = [i for i in self._items.values() if i.id != (replacing.id if replacing else None)]   # a replaced instance is closed first
        worst = sum(i.usd_hr * i.limit_h for i in mine)
        committed = sum(i.usd_hr * max(0.0, i.limit_h - i.uptime_h) for i in mine)
        return judge(spec, n, self.limits, leases=len(mine), workers=sum(i.role == "worker" for i in mine), worst=worst, committed=committed, balance=self.balance())

    def spend(self) -> Spend:
        mine = [i for i in self._items.values()]
        burn = sum(i.usd_hr for i in mine)
        daily = [(f"2026-10-{d:02d}", v) for d, v in zip(range(3, 17), [0.4, 0, 1.2, 0.8, 2.1, 0, 0.3, 1.7, 3.2, 0.9, 2.4, 1.1, 4.6, 2.2])]
        return Spend(balance=self.balance(), burn_hr=burn, accrued_running=sum(i.spent_usd for i in mine), closed_total=self.closed_cost, today=daily[-1][1] + burn,
                     worst_case=sum(i.usd_hr * i.limit_h for i in mine), daily=daily, by_role={"dev": 3.1, "worker": 9.3}, by_provider={"provider-a.example": 4.2, "provider-d.example": 8.2},
                     budget_guard=True, budget=85.0)

    # ------------------------------------------------------------ access
    def ssh_command(self, inst: Instance, tty: bool = False) -> list[str]:
        if os.environ.get("CLOUDDECK_DEMO_SSH_FAIL"):   # lets tests exercise the "interactive command failed" path
            return ["clouddeck-no-such-program"]
        import sys
        return [sys.executable, "-c", f"print('demo session on {inst.host}: nothing real is connected')"]

    def run(self, inst: Instance, cmd: str, timeout: int = 60) -> str:
        self._get(inst.id)
        self._wait(0.3)
        if cmd.startswith("# cd:probe"):
            seed = sum(map(ord, inst.id)) + int(time.time() // 5)
            r = random.Random(seed)
            busy = inst.role == "worker" and r.random() < 0.7
            used = inst.cpu * (r.uniform(0.6, 0.97) if busy else r.uniform(0.01, 0.12))
            lines = [f"cores_used={used:.2f}", f"cpus={inst.cpu}", f"mem_used={int(inst.mem_gb * 2**30 * r.uniform(0.15, 0.7))}", f"mem_max={int(inst.mem_gb * 2**30)}",
                     f"disk={int(inst.disk_gb * 2**30 * r.uniform(0.05, 0.4))} {int(inst.disk_gb * 2**30)}", f"load={used:.2f}", f"uptime_s={int(inst.uptime_h * 3600)}",
                     f"job={'npm test -- --shard=%d/48' % r.randint(1, 48) if busy else ''}", "top=node:%.0f bash:0 sshd:0" % (used * 100)]
            if inst.gpu:
                lines.append(f"gpu={r.randint(5, 95)}, {r.randint(1000, 20000)}, 24564")
            return "\n".join(lines) + "\n"
        if cmd.startswith("# cd:workspace"):
            return "web-app|git|main|3f9a1c2|4\napi-server|git|feat/search|88d0e4b|0\naudit|dir|||212\nresults|dir|||13\n"
        if cmd.startswith("tmux list-windows"):
            return "".join(f"{i}|{n}|{int(a)}|{'claude' if n.startswith(('claude', 'cc-')) else 'bash'}|/work/web-app|{int(time.time())}\n" for i, n, a in self.windows)
        if cmd.startswith("tmux has-session"):
            name = re.search(r"-n (\S+) -c", cmd.split(";", 1)[1]).group(1).strip("'")  # type: ignore[union-attr]
            self.windows.append((len(self.windows), name, False))
            return ""
        if cmd.startswith("tmux kill-window"):
            name = shlex.split(cmd)[-1].split(":", 1)[1]
            self.windows = [w for w in self.windows if w[1] != name]
            return ""
        if cmd.startswith("tmux capture-pane"):
            return "● Reading src/app.ts\n● Running npm test (42 passed)\n> \n"
        if cmd.startswith("tail -n") and "agent.log" in cmd:
            return "2026-10-16T21:00:03Z checkpoint_ok 3f9a1c2\n2026-10-16T21:15:04Z checkpoint_skip unchanged\n"
        if cmd.startswith("ls /work/.ddc/complete"):
            return "web-app\napi-server\naudit\n"
        return f"[demo] {inst.name}: ran `{cmd[:60]}`\n"

    def stream(self, inst: Instance, cmd: str, on_line: Callable[[str], None], stop: Any = None, timeout: int = 3600) -> int:
        self.calls.append(("stream", (inst.id, cmd)))
        for i in range(3):
            if stop is not None and stop.is_set():
                on_line("! stopped")
                return 143
            self._wait(0.5)
            on_line(f"{inst.name} step {i + 1}/3: {cmd[:40]}")
        return 0

    # ------------------------------------------------------------ recovery
    def checkpoint(self, log: LogFn = print) -> CheckpointInfo:
        self.calls.append(("checkpoint", None))
        if not any(i.role == "dev" for i in self._items.values()):
            raise ProviderError("no dev box is running")
        log("demo: packing /work ...")
        self._wait(3)
        info = CheckpointInfo(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "manual", {"web-app": "3f9a1c2", "api-server": "88d0e4b"},
                              {"web-app": 5214, "api-server": 9120})
        self.checkpoint_log.insert(0, info)
        return info

    def checkpoints(self) -> list[CheckpointInfo]:
        return list(self.checkpoint_log)

    def verify(self, log: LogFn = print) -> CheckpointInfo:
        self.calls.append(("verify", None))
        if not self.checkpoint_log:
            raise ProviderError("no checkpoint to verify")
        log("demo: fetched, decrypted and compared the newest checkpoint")
        self._wait(2)
        return self.checkpoint_log[0]

    def heal(self, log: LogFn = print) -> list[str]:
        self.calls.append(("heal", None))
        self._wait(2)
        log("demo: w02 had restarted; re-provisioned")
        return ["w02"]

    def idle_state(self) -> IdleState | None:
        if not any(i.role == "dev" for i in self._items.values()):
            return None
        return IdleState(idle_min=int(time.time() // 60) % 40, stop_at_min=45, active_reason="claude", uptime_min=204, last_checkpoint="2026-10-16T20:41:00Z",
                         held_until=self.held_until)

    def hold_idle(self, minutes: int) -> float:
        self.calls.append(("hold_idle", minutes))
        self.held_until = time.time() + minutes * 60 if minutes else 0.0
        return self.held_until

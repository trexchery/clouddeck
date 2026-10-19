"""Akash provider: a thin adapter over ddc.py.  Every lease/bid/SSH/checkpoint operation and every safety rule
(only state-recorded `ddc-*` leases can ever be closed) is ddc's; this file only translates to the Provider contract."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import statistics
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import __version__, hardware, sdlgen
from ..engine import ddc
from .base import CheckpointInfo, GpuModel, IdleState, Instance, LogFn, Offer, Provider, ProviderError, Quote, Spec, Spend, Verdict, gi, judge  # noqa: F401  (gi: re-exported)

HOURS_PER_MONTH = 730.0
GIB = 2**30
CATALOG_SNAPSHOT = "hw-catalog.json"   # last good derived catalogue, next to the state: what an offline start falls back to


def norm_gpu(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _wrap(e: Exception) -> ProviderError:
    return e if isinstance(e, ProviderError) else ProviderError(str(e))


class AkashProvider(Provider):
    id = "akash"
    label = "Akash Network"
    capabilities = frozenset({"gpu", "fleet", "ssh", "spend", "workspace", "checkpoint", "heal", "resize", "snooze", "hardware", "screen"})

    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self._errors: dict[str, str] = {}          # source -> what went wrong the last time it was tried; a success clears its entry
        self.offers_status = "ok"                 # "ok" | "cached 12 min ago": how fresh the provider list behind offers() is
        self._screen = hardware.ScreenCache()      # explicit availability checks: cached 60 s, rate limited

    # ------------------------------------------------------------ plumbing
    @property
    def last_error(self) -> str:
        """What is wrong right now (empty = nothing): the unresolved errors of the data sources, each cleared by that source's next success."""
        return "; ".join(self._errors.values())

    def _note(self, source: str, err: str = "") -> None:
        if err:
            self._errors[source] = err
        else:
            self._errors.pop(source, None)

    def _cached(self, key: str, ttl: float, fn: Callable[[], Any]) -> Any:
        with self._lock:
            hit = self._cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        val = fn()
        with self._lock:
            self._cache[key] = (time.time(), val)
        return val

    def _fetch(self, key: str, ttl: float, fn: Callable[[], Any]) -> tuple[Any, str]:
        """(value, status) from the TTL cache; when the network fails the last good value with its age, else (None, 'failed: ...').
        Status is 'ok' only for fresh data."""
        with self._lock:
            hit = self._cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1], "ok"
        try:
            val = fn()
        except Exception as e:   # any network / parse failure: fall back, never raise into the UI
            if hit:
                return hit[1], f"cached {hardware.HardwareCatalog(fetched_at=hit[0]).age_text()}"
            return None, f"failed: {e}"
        with self._lock:
            self._cache[key] = (time.time(), val)
        return val, "ok"

    @staticmethod
    def _post_public(path: str, body: Any) -> Any:
        req = urllib.request.Request(ddc.API + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": f"clouddeck/{__version__}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            raise ProviderError(f"request to {path} failed: {e}") from e

    @staticmethod
    def _deps() -> dict[str, dict[str, Any]]:
        return ddc.load_state()["deps"]

    def _history(self) -> list[dict[str, Any]]:
        """Our own leases that actually got a price: the best predictor of what the next one costs."""
        out = []
        for d in self._deps().values():
            pr = ddc.CFG["profiles"].get(d.get("profile"), {})
            if d.get("usd_hr") and pr.get("cpu"):
                out.append({"provider": d.get("provider"), "cpu": int(pr["cpu"]), "usd_hr": float(d["usd_hr"]), "role": d.get("role")})
        return out

    # ------------------------------------------------------------ catalogue
    def offers(self) -> list[Offer]:
        raw, self.offers_status = self._fetch("providers", 300, lambda: ddc.public_get("/v1/providers"))
        if raw is None:
            raise ProviderError(f"provider list unavailable ({self.offers_status})")
        perf = ddc.read_json(ddc.PERF, {})
        per_vcpu: dict[str, list[float]] = {}
        for h in self._history():
            per_vcpu.setdefault(h["provider"], []).append(h["usd_hr"] / h["cpu"])
        out = []
        for p in raw:
            if not p.get("isOnline"):
                continue
            st = p.get("stats") or {}
            attrs = {a.get("key"): a.get("value") for a in p.get("attributes") or []}
            pf = perf.get(p["owner"]) or {}
            gpus = sorted({f"{g.get('model')}" for g in p.get("gpuModels") or []})
            obs = per_vcpu.get(p["owner"])
            hw = hardware.provider_facts(p)   # what the provider states, as the SDL placement matches it ('' = not stated)
            out.append(Offer(
                cpu_vendor=hw["cpu_vendor"], cpu_arch=hw["cpu_arch"], region_code=hw["region_code"], gpu_variants=hw["gpus"],
                id=p["owner"],
                name=attrs.get("organization") or p.get("organization") or (p.get("hostUri") or "").split("//")[-1].split(":")[0],
                host=(p.get("hostUri") or "").split("//")[-1].split(":")[0],
                region=", ".join(x for x in (p.get("ipRegion"), p.get("ipCountryCode") or p.get("country")) if x) or str(attrs.get("location-region") or ""),
                uptime=float(p.get("uptime7d") or 0),
                audited=bool(p.get("isAudited")),
                cpu_free=float((st.get("cpu") or {}).get("available") or 0) / 1000,
                mem_free_gb=float((st.get("memory") or {}).get("available") or 0) / GIB,
                disk_free_gb=float(((st.get("storage") or {}).get("ephemeral") or {}).get("available") or 0) / GIB,
                gpus=gpus,
                gpu_free=int((st.get("gpu") or {}).get("available") or 0),
                cpu_model=pf.get("cpu_model") or str(attrs.get("cpu") or ""),
                mem_type=ddc.prov_memory(p),
                usd_per_vcpu_hr=statistics.median(obs) if obs else None,
                speed=ddc.speed_factor(p["owner"], p, perf),
            ))
        return sorted(out, key=lambda o: (-o.uptime, -o.cpu_free))

    def gpu_models(self) -> list[GpuModel]:
        data = self._cached("gpu", 600, lambda: ddc.public_get("/v1/gpu-prices"))
        out = []
        for m in data.get("models") or []:
            av, pr = m.get("availability") or {}, m.get("price") or {}
            out.append(GpuModel(vendor=m.get("vendor", ""), model=m.get("model", ""), ram=m.get("ram", ""),
                                available=int(av.get("available") or 0), total=int(av.get("total") or 0),
                                providers=int((m.get("providerAvailability") or {}).get("total") or 0),
                                price_min=float(pr.get("min") or 0), price_avg=float(pr.get("avg") or 0), price_max=float(pr.get("max") or 0)))
        # one row per model name: keep the variant with the most free GPUs
        best: dict[str, GpuModel] = {}
        for g in out:
            if g.model not in best or g.available > best[g.model].available:
                best[g.model] = g
        return sorted(best.values(), key=lambda g: (-g.available, g.price_avg))

    def quote(self, spec: Spec) -> Quote:
        notes: list[str] = []
        ref: dict[str, float] = {}
        ref_total = 0.0
        try:
            body = [{"cpu": spec.cpu * 1000, "memory": int(spec.mem_gb * GIB), "storage": int(spec.disk_gb * GIB), "amount": 1}]
            r = self._cached(f"price-{spec.cpu}-{spec.mem_gb}-{spec.disk_gb}", 120, lambda: self._post_public("/v1/pricing", body))
            row = r[0] if isinstance(r, list) else r
            ref = {k: float(row[k]) / HOURS_PER_MONTH for k in ("akash", "aws", "gcp", "azure") if k in row}
            ref_total = ref.get("akash", 0.0)
        except ProviderError as e:
            notes.append(str(e))
        gpu_hr = 0.0
        band: tuple[float, float] | None = None
        if spec.gpu:
            prices, st = self._fetch("gpu", 600, lambda: ddc.public_get("/v1/gpu-prices"))
            px = hardware.gpu_unit_price(spec, hardware.build_catalog(None, None, None, prices)) if prices else None   # (low, average, high) $/GPU·h, market data
            if prices is None:
                notes.append(f"GPU market prices unavailable ({st})")
            elif st != "ok":
                notes.append(f"GPU market prices are {st}")
            if px:
                gpu_hr = px[1] * spec.gpu
                band = (px[0] * spec.gpu, px[2] * spec.gpu)
                notes.append(f"GPU: {spec.gpu} × {spec.gpu_model or 'GPU'} at ~${px[1]:.2f}/GPU·h (market average, range ${px[0]:.2f}-${px[2]:.2f})")
            elif spec.gpu_model:
                notes.append(f"no market price for GPU '{spec.gpu_model}'")
        hist = [h for h in self._history() if h["role"] in ("dev", "worker", "box")]
        if hist:
            per = statistics.median(h["usd_hr"] / h["cpu"] for h in hist)
            est = per * spec.cpu + gpu_hr
            basis = f"your past leases (median ${per:.4f}/vCPU·h over {len(hist)} leases)"
        else:
            est = ref_total + gpu_hr
            basis = "network reference price (winning bids are usually lower)"
        cap = max(ref_total + gpu_hr, est * 2)
        if "akash" in ref:
            ref["akash"] += gpu_hr  # the other clouds' CPU-only price is not comparable once a GPU is added
        return Quote(usd_hr=est, cap_usd_hr=max(0.02, round(cap + 0.004, 2)), reference=ref, basis=basis, notes=notes,
                     cpu_ref_hr=ref_total if ref_total else None, gpu_low_hr=band[0] if band else None, gpu_high_hr=band[1] if band else None,
                     source="own-history" if hist else "market-average" if ref_total else "none")

    # ------------------------------------------------------------ hardware discovery
    def catalog(self) -> hardware.HardwareCatalog:
        """Every GPU kind / CPU architecture / region the network advertises, from four independent public sources (any may fail: the rest still
        give a catalogue).  Offline: the last good copy (saved next to the state) marked stale, else an empty catalogue that says why."""
        return self._cached("catalog", 60, self._build_catalog)

    def _build_catalog(self) -> hardware.HardwareCatalog:
        sources: dict[str, str] = {}
        got: dict[str, Any] = {}
        for name, key, ttl, path in (("providers", "providers", 300, "/v1/providers"), ("gpu-models", "gpu-models", 86400, "/v1/gpu-models"),
                                     ("placement-options", "placement", 120, "/v1/placement-options"), ("gpu-prices", "gpu", 600, "/v1/gpu-prices")):
            got[name], sources[name] = self._fetch(key, ttl, lambda path=path: ddc.public_get(path))
        snap = ddc.STATE_DIR / CATALOG_SNAPSHOT
        if all(v is None for v in got.values()):
            old = ddc.read_json(snap, None)
            try:
                cat = hardware.HardwareCatalog.from_dict(old) if old else hardware.HardwareCatalog()
            except (TypeError, ValueError, KeyError, AttributeError):
                cat = hardware.HardwareCatalog()
            cat.stale = bool(old and cat.fetched_at)
            cat.sources = {**sources, **({"snapshot": f"saved {cat.age_text()}"} if cat.stale else {})}
            cat.errors = [f"{k}: {v}" for k, v in sources.items() if v.startswith("failed")]
            return cat
        cat = hardware.build_catalog(got["providers"], got["gpu-models"], got["placement-options"], got["gpu-prices"], sources=sources)
        cat.stale = any(v != "ok" for v in sources.values())
        if not cat.stale:
            try:
                ddc.write_json(snap, cat.to_dict())   # small derived file; a failed write must not break discovery
            except OSError:
                pass
        return cat

    def _order_profile(self, spec: Spec) -> dict[str, Any]:
        """The profile dict this spec becomes.  An audited-only order needs the auditor's address (SDL signedBy): it comes from the network's
        own data, and without it the order is refused rather than silently sent to unaudited providers."""
        pr = spec.to_profile()
        if spec.audited_only:
            aud = self.catalog().auditors[:1]
            if not aud:
                raise ProviderError("cannot prepare an audited-only order: the auditor address is unknown (the network could not be reached and no saved catalogue exists)")
            pr["signed_by"] = aud
        return pr

    def screen(self, spec: Spec) -> hardware.Screening:
        """Ask the network which providers could take exactly this order (bid-screening: capacity of single nodes + attributes, NO price, not a
        bid).  Explicit action only; an identical request within 60 s is answered from memory, requests are rate limited.  Never deploys."""
        adv = self.matches(spec)
        try:
            pr = self._order_profile(spec)
            body = sdlgen.screening_request(pr, max(1, ddc.uact_block_from_usd_hr(spec.max_usd_hr) if spec.max_usd_hr > 0 else 1))
            reply, cached, at = self._screen.fetch(json.dumps(body, sort_keys=True), lambda: self._post_public("/v1/bid-screening", body))
        except Exception as e:   # unreachable / refused / rate limited / invalid: the advertised rows stay, labelled as such
            return hardware.Screening(adv, at=time.time(), ok=False, error=str(e))
        try:
            cpu_hr = self.quote(spec.with_(gpu=0, gpu_model="", gpu_alt=())).cpu_ref_hr
        except ProviderError:
            cpu_hr = None
        s = hardware.merge_screening(spec, adv, reply, self.offers(), self.catalog(), cpu_hr)
        s.cached, s.at = cached, at
        return s

    def problems(self, spec: Spec) -> list[str]:
        return spec.validate(ddc.CFG)

    def presets(self) -> dict[str, Spec]:
        custom = ddc.read_json(ddc.STATE_DIR / "custom_profiles.json", {})
        out = {}
        for name, pr in ddc.CFG["profiles"].items():
            if name in custom:
                continue
            out[name] = Spec.from_profile(pr, preset=name)
        return out

    # ------------------------------------------------------------ instances
    def _inst(self, dseq: str, d: dict[str, Any]) -> Instance:
        pr = ddc.CFG["profiles"].get(d.get("profile"), {})
        spent, _ = ddc.dep_running_cost(d)
        state = {"ready": "running", "closed": "closed"}.get(d.get("status"), "provisioning")
        return Instance(id=dseq, name=d.get("name", dseq), role=d.get("role", "box"), state=state, provider=self.id,
                        host=d.get("host") or "", port=int(d.get("port") or 22), location=d.get("provider_host") or d.get("provider") or "",
                        cpu=int(pr.get("cpu") or 0), mem_gb=gi(pr.get("mem")), disk_gb=gi(pr.get("disk")), gpu=int(pr.get("gpu") or 0),
                        gpu_model=pr.get("gpu_model", ""), usd_hr=float(d.get("usd_hr") or 0), started_at=float(d.get("lease_at") or d.get("created_at") or 0),
                        limit_h=float(d.get("limit_h") or 0), spent_usd=spent,
                        meta={"profile": d.get("profile"), "idx": d.get("idx"), "provider": d.get("provider"), "bids": d.get("bids") or []})   # bids: the real, confirmed prices

    def _external(self) -> list[Instance]:
        """Deployments on the account that this tool did not create (e.g. other services): shown, never changeable."""
        def fetch() -> list[Instance]:
            ours = set(self._deps())
            deps = ddc.api("GET", "/v1/deployments?skip=0&limit=100&state=active")["data"]["deployments"]
            rate = {x["dseq"]: float(x["price"]["amount"]) for x in (ddc.api("GET", "/v1/spend-rate").get("data") or {}).get("deployments") or []}
            out = []
            for x in deps:
                dseq = str(x["deployment"]["id"]["dseq"])
                if dseq in ours:
                    continue
                res = [r for g in x.get("groups") or [] for r in (g.get("group_spec") or {}).get("resources") or []]
                cpu = sum(int(r["resource"]["cpu"]["units"]["val"]) * int(r.get("count") or 1) for r in res) / 1000
                mem = sum(int(r["resource"]["memory"]["quantity"]["val"]) * int(r.get("count") or 1) for r in res) / GIB
                disk = sum(int(s["quantity"]["val"]) for r in res for s in r["resource"].get("storage") or []) / GIB
                out.append(Instance(id=dseq, name=x.get("name") or dseq, role="external", state="running", provider=self.id, cpu=round(cpu), mem_gb=mem,
                                    disk_gb=disk, usd_hr=ddc.usd_hr_from_uact_block(rate.get(dseq, 0.0)), managed=False))
            return out
        try:
            out = self._cached("external", 60, fetch)
        except Exception as e:  # visibility only: never let it break the managed list
            self._note("external", f"external deployments unavailable: {e}")
            return []
        self._note("external")
        return out

    def instances(self, refresh: bool = False) -> list[Instance]:
        if refresh:
            try:
                with ddc.FileLock(wait_s=1):  # a start/stop in progress does its own refresh
                    ddc.refresh_live()
                self._note("instances")
            except ddc.LockBusy:
                pass
            except Exception as e:
                self._note("instances", str(e))
        mine = [self._inst(k, d) for k, d in ddc.active_deps().items()]
        ext = self._external() if refresh or "external" in self._cache else []
        order = {"dev": 0, "worker": 1, "box": 2, "job": 2, "external": 3}
        return sorted(mine, key=lambda i: (order.get(i.role, 9), i.meta.get("idx") or 0, i.name)) + ext

    def _profile_for(self, spec: Spec) -> str:
        pre = ddc.CFG["profiles"].get(spec.preset)
        if pre and not spec.extended() and (int(pre["cpu"]), int(gi(pre["mem"])), int(gi(pre["disk"])), int(pre.get("gpu") or 0), pre.get("gpu_model", ""), float(pre["max_usd_hr"])) == \
                (spec.cpu, spec.mem_gb, spec.disk_gb, spec.gpu, spec.gpu_model, spec.max_usd_hr):
            return spec.preset
        name = f"x{spec.cpu}-{spec.mem_gb}-{spec.disk_gb}" + (f"-g{spec.gpu}{norm_gpu(spec.gpu_model)}" if spec.gpu else "")
        if spec.extended():   # a different placement is a different profile; the suffix is a stable hash of it
            name += "-" + spec.profile_suffix()
        if len(name) > 40:    # profile names are at most 40 characters
            name = f"{name[:33]}-{spec.profile_suffix() or hashlib.sha1(name.encode()).hexdigest()[:6]}"
        ddc.save_custom_profile(name, self._order_profile(spec))
        return name

    # ------------------------------------------------------------ spend guard
    def guard(self, spec: Spec, n: int = 1, replacing: Instance | None = None) -> Verdict:
        """The configured limits (config.toml: budget_*, reserve_usd, max_total_leases, max_workers, max_usd_hr_hard) applied to what is
        planned.  Raises (rather than guessing) when the state or ledger cannot be read."""
        try:
            st = ddc.load_state()   # raises (never "empty") when the state file is damaged
            if (bad := ddc.ledger_read()[1]):
                return Verdict(False, f"{ddc.LEDGER} has unreadable lines {bad[:5]}: past spend may be under-counted, so nothing new is started until it is repaired",
                               spec.max_usd_hr * spec.hours * n, ddc.CFG.get("budget_guard", True) is not False)
            view = ddc.budget_view(st)
            deps = ddc.active_deps(st)
        except Exception as e:
            raise _wrap(e) from e
        worst, committed = view["worst"], view["committed"]
        alive = [*deps.values(), *(st.get("pending") or {}).values()]   # creations whose outcome is still unknown count too
        leases, workers = len(alive), sum(1 for d in alive if d.get("role") == "worker")
        old = deps.get(replacing.id) if replacing else None
        if old:   # the replaced lease is closed first: its still-committed spend and its slot are free again
            gone = ddc.dep_running_cost(old)[1]
            worst, committed, leases = worst - gone, committed - gone, leases - 1
            workers -= old.get("role") == "worker"
        guard_on = ddc.CFG.get("budget_guard", True) is not False
        hours = float(ddc.CFG.get("max_runtime_hours", 8)) if spec.role == "dev" else spec.hours   # the dev box limit always follows config.toml
        return judge(spec, n, self.limits(), leases=leases, workers=workers, worst=worst, committed=committed, hours=hours,
                     balance=self.balance() if guard_on else None)

    @staticmethod
    def limits() -> dict[str, Any]:
        """config.toml's limits with the controller's hard ceilings applied on top (they hold even when the config asks for more or the
        budget guard is off; only the controller's `unsafe_disable_limits` lifts them).  ponytail: names mirror the controller's HARD_MAX_*;
        if the controller ever exposes one check function, call that instead."""
        lim = dict(ddc.CFG)
        if getattr(ddc, "limits_unsafe", lambda: False)():
            return lim
        ceil = getattr(ddc, "price_ceiling", None)   # the price-cap ceiling: max_price_usd_hr from config.toml (default 2.0, at most 100)
        for key, const in (("max_total_leases", "HARD_MAX_LEASES"), ("max_workers", "HARD_MAX_WORKERS"), ("max_usd_hr_hard", "HARD_MAX_USD_HR")):
            hard = ceil() if key == "max_usd_hr_hard" and ceil else getattr(ddc, const, None)
            if hard is not None:
                try:
                    lim[key] = min(float(lim.get(key, hard)), float(hard))
                except (TypeError, ValueError):
                    lim[key] = lim.get(key)   # malformed: judge() refuses
        for key, const in (("max_hours_hard", "HARD_MAX_RUNTIME_H"), ("max_batch", "HARD_MAX_SCALE")):
            if (hard := getattr(ddc, const, None)) is not None:
                lim[key] = hard
        return lim

    def checkpoint_enabled(self) -> bool:
        return bool(ddc.state_repo())

    # ------------------------------------------------------------ lifecycle
    def deploy(self, spec: Spec, log: LogFn = print) -> Instance:
        self.require(spec)
        prof = self._profile_for(spec)
        try:
            if spec.role == "dev":
                if ddc.dev_box():
                    raise ProviderError("a dev box is already running: terminate or resize it instead")
                # the dev box limit must match the lease-side agent and the watchdog, so it always follows config.toml
                ddc.cmd_start(argparse.Namespace(profile=prof, workers=0, worker_profile=None))
                return self._inst((dev := self._dev())["dseq"], dev)
            with ddc.FileLock():
                ddc.refresh_live()
                dev = ddc.dev_box()
                if spec.role == "worker" and not dev:
                    raise ProviderError("start the dev box first: workers are driven from it")
                ddc.check_budget(prof, 1, spec.hours, role=spec.role)
                counts: dict[str, int] = {dev["provider"]: 10**6} if dev else {}  # a dev box cannot reach leases on its own provider
                for w in ddc.workers():
                    counts[w["provider"]] = counts.get(w["provider"], 0) + 1
                used = {w["idx"] for w in ddc.workers()}
                idx = next(i for i in range(1, 100) if i not in used) if spec.role == "worker" else 0
                box = ddc.create_box(spec.role, prof, idx, counts, limit_h=spec.hours)
                if spec.role == "worker":
                    try:
                        ddc.provision_worker(box)
                        ddc.sync_worker_hosts()
                    except Exception:
                        log(f"{box['name']}: provisioning failed; closing the new lease")
                        ddc.close_box(box["dseq"])
                        raise
                return self._inst(box["dseq"], ddc.load_state()["deps"][box["dseq"]])
        except Exception as e:
            raise _wrap(e) from e

    def terminate(self, instance_id: str, log: LogFn = print, *, discard_workspace: bool = False) -> float:
        rec = self._deps().get(instance_id)
        if not rec or not rec.get("ours"):
            raise ProviderError(f"refusing to close {instance_id}: not created by ddc/CloudDeck")
        try:
            with ddc.FileLock():
                ddc.refresh_live()
                if rec.get("role") == "dev":
                    dev = ddc.dev_box()
                    if dev and dev["dseq"] == instance_id:
                        ddc.final_checkpoint(dev, force=False, discard_workspace=discard_workspace)  # raises (and leaves everything running) unless verified
                    for w in ddc.workers():  # workers hold no data and cannot work without their dev box
                        ddc.close_box(w["dseq"])
                    return ddc.close_box(instance_id)
                cost = ddc.close_box(instance_id)
                if rec.get("role") == "worker":
                    ddc.sync_worker_hosts()
                return cost
        except Exception as e:
            raise _wrap(e) from e

    def scale_victims(self, n: int) -> list[Instance]:
        """ddc closes the highest-numbered workers (w04 before w01), not the newest."""
        have = sorted((i for i in self.instances() if i.role == "worker" and i.state == "running"), key=lambda i: i.meta.get("idx") or 0)
        return have[n:]

    def scale(self, n: int, spec: Spec, log: LogFn = print) -> list[Instance]:
        try:
            have = sum(1 for d in ddc.active_deps().values() if d.get("role") == "worker")
        except Exception as e:
            raise _wrap(e) from e
        if n > have:
            self.require(spec.with_(role="worker"), n - have)
        prof = self._profile_for(spec.with_(role="worker"))
        try:
            ddc.cmd_scale(argparse.Namespace(n=n, profile=prof, explore=False))
        except Exception as e:
            raise _wrap(e) from e
        return [i for i in self.instances() if i.role == "worker"]

    stop_all_roles = ("dev", "worker", "job")   # what the user's stop-everything closes; other roles (hand-made "box" leases) keep running until named

    def stop_all(self, log: LogFn = print, *, discard_workspace: bool = False) -> None:
        try:
            ddc.cmd_stop(argparse.Namespace(workers_only=False, force=False, discard_workspace=discard_workspace, include_jobs=True))   # the user's stop-everything: owned job leases too
        except Exception as e:
            raise _wrap(e) from e

    def balance(self) -> float | None:
        try:
            bal = self._cached("balance", 60, ddc.balance_usd)
        except Exception as e:
            self._note("balance", str(e))
            return None
        self._note("balance")
        return bal

    # ------------------------------------------------------------ access
    def ssh_command(self, inst: Instance, tty: bool = False) -> list[str]:
        if not inst.reachable:
            raise ProviderError(f"{inst.name} is not reachable (state: {inst.state})")
        return ddc.ssh_args({"host": inst.host, "port": inst.port}, tty=tty)

    def stream(self, inst: Instance, cmd: str, on_line: Callable[[str], None], stop: Any = None, timeout: int = 3600) -> int:
        argv = self.ssh_command(inst) + [cmd]
        p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=ddc.NOWIN)
        t0 = time.time()
        try:
            assert p.stdout
            for raw in iter(p.stdout.readline, b""):
                on_line(raw.decode("utf-8", "replace").rstrip("\r\n"))
                if (stop is not None and stop.is_set()) or time.time() - t0 > timeout:
                    p.terminate()
                    on_line("! stopped")
                    break
            return p.wait(timeout=30)
        finally:
            if p.poll() is None:
                p.kill()

    # ------------------------------------------------------------ money
    def spend(self) -> Spend:
        view = ddc.budget_view()
        deps = self._deps()
        today = dt.date.today()
        daily: dict[str, float] = {}
        by_role: dict[str, float] = {}
        by_prov: dict[str, float] = {}
        entries, bad = ddc.ledger_read()
        # the controller refuses new leases while the ledger has unreadable lines; say so instead of silently under-counting
        self._note("ledger", f"{ddc.LEDGER} has unreadable lines {bad[:5]}: spend may be under-counted" if bad else "")
        for e in entries:
            if e.get("event") != "closed":
                continue
            cost = float(e.get("cost_usd") or 0)
            day = dt.datetime.fromisoformat(e["at"]).astimezone().date().isoformat()
            daily[day] = daily.get(day, 0) + cost
            d = deps.get(str(e.get("dseq")), {})
            by_role[d.get("role", "?")] = by_role.get(d.get("role", "?"), 0) + cost
            prov = d.get("provider_host") or d.get("provider") or "?"
            by_prov[prov] = by_prov.get(prov, 0) + cost
        midnight = dt.datetime.combine(today, dt.time()).timestamp()
        burn = 0.0
        accrued = 0.0
        for d in ddc.active_deps().values():
            usd_hr = float(d.get("usd_hr") or 0)
            burn += usd_hr
            spent, _ = ddc.dep_running_cost(d)
            accrued += spent
            start = float(d.get("lease_at") or d.get("created_at") or time.time())
            daily[today.isoformat()] = daily.get(today.isoformat(), 0) + max(0.0, time.time() - max(start, midnight)) / 3600 * usd_hr
            by_role[d.get("role", "?")] = by_role.get(d.get("role", "?"), 0) + spent
        series = [((today - dt.timedelta(days=n)).isoformat()) for n in range(13, -1, -1)]
        return Spend(balance=self.balance(), burn_hr=burn, accrued_running=accrued, closed_total=ddc.ledger_spent(),
                     today=daily.get(today.isoformat(), 0.0), worst_case=view["worst"], daily=[(d, daily.get(d, 0.0)) for d in series],
                     by_role=by_role, by_provider=by_prov, budget_guard=ddc.CFG.get("budget_guard", True) is not False, budget=float(ddc.CFG["budget_usd"]))

    # ------------------------------------------------------------ recovery
    def _dev(self) -> dict[str, Any]:
        dev = ddc.dev_box()
        if not dev:
            raise ProviderError("no dev box is running")
        return dev

    def checkpoint(self, log: LogFn = print) -> CheckpointInfo:
        try:
            d = self._dev()
            out = ddc.ssh(d, "/opt/ddc/checkpoint.sh manual", timeout=3600).strip().splitlines()[-1]
            log(out)
            v = ddc.verify_checkpoint(out.split()[2] if out.startswith("checkpoint_ok") else None)
        except Exception as e:
            raise _wrap(e) from e
        m = v["manifest"]
        return CheckpointInfo(m["created_at"], m["reason"], {k: r.get("head", "") for k, r in m["repos"].items()}, v["entries"])

    def verify(self, log: LogFn = print) -> CheckpointInfo:
        try:
            v = ddc.verify_checkpoint()
        except Exception as e:
            raise _wrap(e) from e
        m = v["manifest"]
        return CheckpointInfo(m["created_at"], m["reason"], {k: r.get("head", "") for k, r in m["repos"].items()}, v["entries"])

    def checkpoints(self) -> list[CheckpointInfo]:
        out = []
        for p in sorted((ddc.STATE_DIR / "checkpoints").glob("*/manifest.json"), reverse=True):
            m = ddc.read_json(p, None)
            if m:
                out.append(CheckpointInfo(m.get("created_at", ""), m.get("reason", ""), {k: r.get("head", "") for k, r in (m.get("repos") or {}).items()},
                                          {k: a.get("entries", 0) for k, a in (m.get("archives") or {}).items()}, str(p.parent)))
        return out

    def heal(self, log: LogFn = print) -> list[str]:
        try:
            return ddc.cmd_heal(None) or []
        except Exception as e:
            raise _wrap(e) from e

    def idle_state(self) -> IdleState | None:
        dev = ddc.dev_box()
        if not dev:
            return None
        try:
            j = json.loads(ddc.ssh(dev, "cat /run/ddc/state.json", timeout=30))
        except Exception as e:
            raise ProviderError(f"dev box state unreadable: {e}") from e
        return IdleState(idle_min=int(j.get("idle_min") or 0), stop_at_min=int(ddc.CFG["idle_minutes"]), active_reason=j.get("active_reason") or "",
                         uptime_min=int(j.get("uptime_min") or 0), last_checkpoint=j.get("last_checkpoint") or "",
                         checkpoint_failed=bool(j.get("checkpoint_failed")), shutdown_requested=j.get("shutdown_requested") or "",
                         held_until=float(ddc.load_state().get("idle_hold_until") or 0))

    def hold_idle(self, minutes: int) -> float:
        until = time.time() + minutes * 60 if minutes > 0 else 0.0
        with ddc._state_lock:
            st = ddc.load_state()
            st["idle_hold_until"] = until
            ddc.write_json(ddc.STATE_FILE, st)
        return until

    def snooze_effective(self) -> bool:
        """Is the scheduled watchdog new enough to honor a snooze?  A config that lives in a checkout (legacy layout) means the
        task runs that checkout's code, so read it; otherwise the watchdog is this installed package."""
        cfg_dir = Path(ddc.CONFIG_FILE or ".").parent
        if not (cfg_dir / "ddc.py").exists():
            return True
        try:
            return any("idle_hold_until" in f.read_text(encoding="utf-8") for f in (cfg_dir / "ddc.py", cfg_dir / "clouddeck" / "controller.py") if f.exists())
        except OSError:
            return False

"""Hardware discovery model: what the network advertises, normalised, with nothing guessed.

Pure functions over already-parsed JSON (the Akash adapter fetches and caches; tests feed fixtures), so this module has no network, no
config and no Textual.  Three evidence levels are kept apart everywhere:

  advertised  inventory a provider reports (aggregate totals, an upper bound) plus a price *estimate* whose source is named
  screened    the network's bid-screening says a node of this provider fits this exact order and its attributes (still no price, not a bid)
  confirmed   a real bid with a price: only exists after an order is placed through the normal deploy flow (see create_box)

A missing attribute is "" / None (unknown) and is never inferred.  Nothing here lists GPU models, vendors, architectures or regions: all
are read from the data, so tomorrow's hardware appears without a code change."""
from __future__ import annotations

import re
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .sdlgen import ADDR_RE, TOKEN_RE, norm_model

if TYPE_CHECKING:   # providers.base imports this module: never import it back at run time
    from .providers.base import Offer, Spec

ADVERTISED, SCREENED, CONFIRMED = "advertised", "screened", "confirmed"
PRICE_PROVIDER, PRICE_MARKET, PRICE_NONE = "provider-specific", "market-average", "none"
SCREEN_TTL = 60.0          # seconds a bid-screening answer is reused
SCREEN_MIN_GAP = 2.0       # seconds between two requests to the network (rate limit; an identical request is answered from the cache)
INTERFACES = ("pcie", "sxm", "mig")


# ---------------------------------------------------------------------------------------------- normalisation
def norm_token(v: Any) -> str:
    """Lower-case id token, or '' when empty / not shaped like one (never raises)."""
    s = str(v if v is not None else "").strip().lower()
    return s if TOKEN_RE.fullmatch(s) else ""


def norm_interface(v: Any) -> str:
    """'SXM4' / 'SXM5' -> 'sxm', 'PCIe' -> 'pcie'; an interface this code has never heard of is kept as a lower-case token."""
    s = str(v if v is not None else "").strip().lower()
    for fam in INTERFACES:
        if s.startswith(fam):
            return fam
    return norm_token(s)


def norm_ram(v: Any) -> str:
    """'24Gi' / '24 GB' / '24gi' / 24 -> '24Gi'; '512Mi' stays; anything unparseable -> '' (unknown)."""
    m = re.fullmatch(r"\s*(\d{1,4})\s*(gi|gib|gb|g|mi|mib|mb|m)?\s*", str(v if v is not None else ""), re.I)
    if not m or int(m.group(1)) < 1:
        return ""
    unit = (m.group(2) or "gi").lower()
    return f"{int(m.group(1))}{'Mi' if unit.startswith('m') else 'Gi'}"


@dataclass(frozen=True)
class GpuVariant:
    """One orderable GPU kind exactly as the SDL spells it: lower-case ids, ram '24Gi', interface family.  '' = unspecified."""
    vendor: str
    model: str
    ram: str = ""
    interface: str = ""

    @property
    def is_any(self) -> bool:
        return not (self.ram or self.interface)

    def label(self) -> str:
        return " ".join(x for x in (self.model, self.ram, self.interface) if x)


def parse_gpu_attr(key: Any) -> GpuVariant | None:
    """`capabilities/gpu/vendor/<v>/model/<m>` followed by `ram/<r>` and `interface/<i>` pairs in either order.  Anything else -> None."""
    parts = str(key or "").split("/")
    if len(parts) < 6 or parts[:3] != ["capabilities", "gpu", "vendor"] or parts[4] != "model":
        return None
    vendor, model = norm_token(parts[3]), norm_token(parts[5])
    if not vendor or not model:
        return None
    rest, ram, iface = parts[6:], "", ""
    if len(rest) % 2:
        return None
    for k, v in zip(rest[::2], rest[1::2]):
        if k == "ram" and not ram:
            ram = norm_ram(v)
        elif k == "interface" and not iface:
            iface = norm_interface(v)
        else:
            return None
        if (k == "ram" and not ram) or (k == "interface" and not iface):
            return None
    return GpuVariant(vendor, model, ram, iface)


def attr_map(p: Mapping[str, Any]) -> dict[str, str]:
    return {str(a.get("key")): str(a.get("value")) for a in p.get("attributes") or [] if isinstance(a, Mapping) and a.get("key") is not None}


def provider_gpus(p: Mapping[str, Any]) -> tuple[GpuVariant, ...]:
    """GPU variants of one provider: the structured inventory list when present (authoritative), else the self-declared attributes."""
    out: dict[GpuVariant, None] = {}
    for g in p.get("gpuModels") or []:
        if isinstance(g, Mapping) and norm_token(g.get("vendor")) and norm_token(g.get("model")):
            out[GpuVariant(norm_token(g["vendor"]), norm_token(g["model"]), norm_ram(g.get("ram")), norm_interface(g.get("interface")))] = None
    if not out:
        for k in attr_map(p):
            v = parse_gpu_attr(k)
            if v and v.ram and v.interface:   # the shortest spellings (model only) repeat the full one
                out[v] = None
        if not out:
            for k in attr_map(p):
                if (v := parse_gpu_attr(k)):
                    out[v] = None
    return tuple(out)


def provider_facts(p: Mapping[str, Any]) -> dict[str, Any]:
    """What a provider states about its hardware, as the SDL placement matches it (attribute values only; '' = not stated)."""
    a = attr_map(p)
    return {"cpu_vendor": norm_token(a.get("capabilities/cpu")), "cpu_arch": norm_token(a.get("capabilities/cpu/arch")),
            "region_code": norm_token(a.get("location-region")), "gpus": provider_gpus(p)}


# ---------------------------------------------------------------------------------------------- catalogue
@dataclass
class GpuEntry:
    variant: GpuVariant
    display: str = ""
    supported: bool = False             # listed in the network's model catalogue
    free: int | None = None             # advertised free units across online providers (None = not advertised right now)
    total: int | None = None
    providers: int | None = None        # online providers advertising it
    free_audited: int | None = None     # free units on audited providers (placement-options)
    max_node_free: int | None = None    # most GPUs of this kind free on ONE node of an audited provider: the biggest order that can fit
    price_min: float | None = None
    price_avg: float | None = None      # USD per GPU-hour, network-wide estimate from past leases
    price_max: float | None = None
    evidence: str = "supported-only"    # "advertised" once any source reports it on a provider

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["variant"] = dict(self.variant.__dict__)
        return d

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> GpuEntry:
        e = cls(GpuVariant(**d["variant"]))
        for k, v in d.items():
            if k != "variant" and hasattr(e, k):
                setattr(e, k, v)
        return e


@dataclass
class ModelRow:
    vendor: str
    model: str
    display: str
    any: GpuEntry                       # the whole model, whatever its VRAM / interface
    variants: list[GpuEntry]            # the concrete VRAM x interface combinations known


@dataclass
class HardwareCatalog:
    gpus: list[GpuEntry] = field(default_factory=list)
    cpu_archs: dict[str, int] = field(default_factory=dict)     # advertised value -> online providers
    cpu_vendors: dict[str, int] = field(default_factory=dict)
    regions: dict[str, int] = field(default_factory=dict)
    unknown: dict[str, int] = field(default_factory=dict)       # online providers that do not state: cpu_arch, cpu_vendor, region
    auditors: list[str] = field(default_factory=list)           # signer addresses seen on audited providers, most common first
    providers_online: int = 0
    fetched_at: float = 0.0
    stale: bool = False                 # served from the last good copy because the network could not be reached
    sources: dict[str, str] = field(default_factory=dict)       # name -> "ok" | "cached 3 h ago" | "failed: ..."
    errors: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.gpus or self.cpu_archs or self.regions or self.providers_online)

    def age_text(self, now: float | None = None) -> str:
        if not self.fetched_at:
            return "never"
        s = max(0.0, (now if now is not None else time.time()) - self.fetched_at)
        return f"{s / 3600:.1f} h ago" if s >= 5400 else f"{s / 60:.0f} min ago" if s >= 90 else "just now"

    def vendors(self) -> list[tuple[str, int]]:
        """(vendor, models) for every vendor in the data, ordered by how much of it can be rented right now."""
        free: Counter[str] = Counter()
        models: dict[str, set[str]] = {}
        for e in self.gpus:
            models.setdefault(e.variant.vendor, set()).add(e.variant.model)
            if e.variant.is_any:
                free[e.variant.vendor] += e.free or 0
        return sorted(((v, len(m)) for v, m in models.items()), key=lambda t: (-free[t[0]], t[0]))

    def models(self, vendor: str) -> list[ModelRow]:
        """Models of a vendor: those with free units first (most free first), then the rest alphabetically."""
        rows: dict[str, ModelRow] = {}
        for e in self.gpus:
            v = e.variant
            if v.vendor != vendor:
                continue
            row = rows.setdefault(v.model, ModelRow(vendor, v.model, e.display or v.model, GpuEntry(GpuVariant(vendor, v.model)), []))
            if v.is_any:
                row.any = e
            else:
                row.variants.append(e)
            row.display = row.display if row.display != v.model else (e.display or v.model)
        for r in rows.values():   # a partial pin ("any VRAM, sxm") only appears when no concrete variant (VRAM + interface) already covers it
            full = [e for e in r.variants if e.variant.ram and e.variant.interface]
            r.variants = full + [e for e in r.variants if not (e.variant.ram and e.variant.interface)
                                 and not any((not e.variant.ram or f.variant.ram == e.variant.ram) and (not e.variant.interface or f.variant.interface == e.variant.interface) for f in full)]
            r.variants.sort(key=lambda e: (-(e.free or 0), e.variant.ram, e.variant.interface))
        return sorted(rows.values(), key=lambda r: (-(r.any.free or 0), r.model))

    def entry(self, v: GpuVariant) -> GpuEntry | None:
        return next((e for e in self.gpus if e.variant == v), None)

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["gpus"] = [e.to_dict() for e in self.gpus]
        return d

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> HardwareCatalog:
        c = cls()
        for k, v in d.items():
            if k == "gpus":
                c.gpus = [GpuEntry.from_dict(x) for x in v]
            elif hasattr(c, k):
                setattr(c, k, v)
        return c


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def _int(v: Any) -> int | None:
    f = _num(v)
    return int(f) if f is not None else None


def build_catalog(providers: Iterable[Mapping[str, Any]] | None, models: Any = None, placement: Any = None, prices: Any = None, now: float | None = None,
                  sources: Mapping[str, str] | None = None) -> HardwareCatalog:
    """Merge the independent sources (any of them may be None = unavailable) into one catalogue.  Union by (vendor, model, ram, interface):
    /v1/gpu-models gives every supported model (also those nobody offers now: free stays None), /v1/placement-options the free units and
    the biggest order one node can hold, /v1/gpu-prices the market price, and the providers' own inventory adds anything the lists lag behind."""
    ent: dict[GpuVariant, GpuEntry] = {}

    def get(v: GpuVariant) -> GpuEntry:
        if v not in ent:
            ent[v] = GpuEntry(v)
        return ent[v]

    for vendor in models or []:
        vname = norm_token(vendor.get("name"))
        for m in vendor.get("models") or []:
            mname = norm_token(m.get("name"))
            if not (vname and mname):
                continue
            disp = str(m.get("displayName") or "")[:40] or mname
            e = get(GpuVariant(vname, mname))
            e.display, e.supported = disp, True
            ifaces = [norm_interface(i) for i in m.get("interface") or []]
            for mem in m.get("memory") or []:
                if norm_ram(mem) and len(ifaces) <= 1:   # with several interfaces the real VRAM x interface pairs are only known once advertised
                    v = get(GpuVariant(vname, mname, norm_ram(mem), ifaces[0] if ifaces else ""))
                    v.display, v.supported = disp, True

    for vendor in (placement or {}).get("gpus") or []:
        vname = norm_token(vendor.get("vendor"))
        for m in vendor.get("models") or []:
            mname = norm_token(m.get("name"))
            for var in m.get("variants") or [{"memory": None, "interface": None, **{k: m.get(k) for k in ("providerCount", "availableUnits", "maxNodeFreeUnits")}}]:
                if not (vname and mname):
                    continue
                e = get(GpuVariant(vname, mname, norm_ram(var.get("memory")), norm_interface(var.get("interface"))))
                e.free_audited, e.max_node_free, e.providers = _int(var.get("availableUnits")), _int(var.get("maxNodeFreeUnits")), _int(var.get("providerCount"))
                e.free = e.free_audited if e.free is None else e.free
                e.evidence = ADVERTISED

    for row in (prices or {}).get("models") or []:
        vname, mname = norm_token(row.get("vendor")), norm_token(row.get("model"))
        if not (vname and mname):
            continue
        e = get(GpuVariant(vname, mname, norm_ram(row.get("ram")), norm_interface(row.get("interface"))))
        av, pr = row.get("availability") or {}, row.get("price") or {}
        e.free, e.total = _int(av.get("available")), _int(av.get("total"))
        e.providers = _int((row.get("providerAvailability") or {}).get("total")) if e.providers is None else e.providers
        e.price_min, e.price_avg, e.price_max = _num(pr.get("min")), _num(pr.get("avg")), _num(pr.get("max"))
        e.evidence = ADVERTISED

    archs: Counter[str] = Counter()
    vendors: Counter[str] = Counter()
    regions: Counter[str] = Counter()
    auditors: Counter[str] = Counter()
    seen: Counter[GpuVariant] = Counter()
    unknown: Counter[str] = Counter()
    online = 0
    for p in providers or []:
        if not p.get("isOnline"):
            continue
        online += 1
        f = provider_facts(p)
        for key, counter in (("cpu_arch", archs), ("cpu_vendor", vendors), ("region_code", regions)):
            if f[key]:
                counter[f[key]] += 1
            else:
                unknown["region" if key == "region_code" else key] += 1
        for v in f["gpus"]:
            seen[v] += 1
        if p.get("isAudited"):
            for a in p.get("attributes") or []:
                for who in a.get("auditedBy") or []:
                    if ADDR_RE.fullmatch(str(who)):
                        auditors[str(who)] += 1
    for v, n in seen.items():     # a model advertised on a provider before any list knows it
        e = get(v)
        if e.evidence != ADVERTISED:
            e.evidence = ADVERTISED
        if e.providers is None:
            e.providers = n
    for e in list(ent.values()):  # the "whole model" row of a model that only has concrete variants
        if not e.variant.is_any:
            any_ = get(GpuVariant(e.variant.vendor, e.variant.model))
            any_.display = any_.display or e.display
            if e.evidence == ADVERTISED:
                any_.evidence = ADVERTISED
    for any_ in [x for x in ent.values() if x.variant.is_any]:
        kids = [x for x in ent.values() if not x.variant.is_any and x.variant.vendor == any_.variant.vendor and x.variant.model == any_.variant.model]
        full = [k for k in kids if k.variant.ram and k.variant.interface and k.free is not None]   # partial pins repeat the same units: count concrete variants only
        if full:
            any_.free = sum(k.free or 0 for k in full)
            any_.total = sum(k.total for k in full if k.total is not None) if any(k.total is not None for k in full) else any_.total
        if any_.free is None and any(k.free is not None for k in kids):
            any_.free = sum(k.free or 0 for k in kids)
        if any_.total is None and any(k.total is not None for k in kids):
            any_.total = sum(k.total or 0 for k in kids)
        if any_.max_node_free is None and any(k.max_node_free is not None for k in kids):
            any_.max_node_free = max(k.max_node_free or 0 for k in kids)
        if any_.providers is None and kids:
            any_.providers = max((k.providers or 0) for k in kids) or None
        if any_.price_avg is None:
            priced = [k for k in kids if k.price_avg is not None]
            if priced:
                any_.price_min, any_.price_max = min(k.price_min or k.price_avg for k in priced), max(k.price_max or k.price_avg for k in priced)
                any_.price_avg = sum(k.price_avg for k in priced if k.price_avg is not None) / len(priced)
    for e in ent.values():
        e.display = e.display or e.variant.model
    gpus = sorted(ent.values(), key=lambda e: (e.variant.vendor, e.variant.model, not e.variant.is_any, e.variant.ram, e.variant.interface))
    cat = HardwareCatalog(gpus=gpus, cpu_archs=dict(archs), cpu_vendors=dict(vendors), regions=dict(regions), unknown=dict(unknown),
                          auditors=[a for a, _ in auditors.most_common()], providers_online=online, fetched_at=now if now is not None else time.time(),
                          sources=dict(sources or {}))
    cat.errors = [f"{k}: {v}" for k, v in cat.sources.items() if v.startswith("failed")]
    return cat


def catalog_from_gpu_models(models: Iterable[Any], now: float | None = None) -> HardwareCatalog:
    """A catalogue derived from a provider's plain `gpu_models()` list (what a provider without a richer API can offer)."""
    ent = []
    for g in models:
        v = GpuVariant(norm_token(g.vendor) or "nvidia", norm_token(g.model), norm_ram(g.ram), norm_interface(getattr(g, "interface", "")))
        if v.model:
            ent.append(GpuEntry(v, v.model, False, g.available, g.total, g.providers, None, None, g.price_min or None, g.price_avg or None, g.price_max or None, ADVERTISED))
            if not v.is_any:
                ent.append(GpuEntry(GpuVariant(v.vendor, v.model), v.model, False, g.available, g.total, g.providers, None, None, g.price_min or None, g.price_avg or None,
                                    g.price_max or None, ADVERTISED))
    return HardwareCatalog(gpus=ent, fetched_at=now if now is not None else time.time(), sources={"provider": "ok"})


# ---------------------------------------------------------------------------------------------- matching
@dataclass
class Match:
    offer: Offer
    evidence: str = ADVERTISED
    free_gpus: int | None = None
    est_usd_hr: float | None = None
    price_source: str = PRICE_NONE
    notes: list[str] = field(default_factory=list)
    incidents_7d: int | None = None


@dataclass
class Screening:
    """Result of one explicit availability check (bid-screening)."""
    matches: list[Match] = field(default_factory=list)
    at: float = 0.0
    cached: bool = False
    ok: bool = False
    error: str = ""
    screened: int = 0        # providers the network said can fit the order

    @property
    def summary(self) -> str:
        if not self.ok:
            return f"screening unavailable ({self.error or 'no answer'}): advertised capacity only"
        when = time.strftime("%H:%M:%S", time.localtime(self.at)) if self.at else "?"
        return f"screened {self.screened} provider(s) at {when}" + (" (cached)" if self.cached else "")


def variant_matches(v: GpuVariant, spec: Spec) -> bool:
    models = {norm_model(m) for m in (spec.gpu_model, *spec.gpu_alt) if m}
    return ((not spec.gpu_vendor or v.vendor == spec.gpu_vendor) and (not models or v.model in models)
            and (not spec.gpu_ram or v.ram == spec.gpu_ram) and (not spec.gpu_interface or v.interface == spec.gpu_interface))


def gpu_unit_price(spec: Spec, cat: HardwareCatalog | None) -> tuple[float, float, float] | None:
    """(low, average, high) USD per GPU-hour for the requested GPU from the market data, or None when no priced entry matches.  Only entries that
    are priced count; several models (any-of) are averaged."""
    if not (cat and spec.gpu):
        return None
    best: dict[str, GpuEntry] = {}      # per model the variant with the most free units (the one an unpinned order most likely gets)
    for e in cat.gpus:
        if e.price_avg and variant_matches(e.variant, spec) and (not e.variant.is_any or not any(k.price_avg and not k.variant.is_any and k.variant.model == e.variant.model for k in cat.gpus)):
            cur = best.get(e.variant.model)
            if cur is None or (e.free or 0, -(e.price_avg or 0)) > (cur.free or 0, -(cur.price_avg or 0)):
                best[e.variant.model] = e
    if not best:
        return None
    hits = list(best.values())
    return (min(e.price_min or e.price_avg or 0 for e in hits), sum(e.price_avg or 0 for e in hits) / len(hits), max(e.price_max or e.price_avg or 0 for e in hits))


def estimate(spec: Spec, offer: Offer | None, cpu_hr: float | None, gpu_hr: float | None) -> tuple[float | None, str]:
    """(USD/hour, source).  CPU/RAM/disk part: the provider's own observed price per vCPU (your past leases) when there is one, else the market
    reference; GPU part: market average per GPU.  Unknown parts make the whole estimate unknown (never guessed)."""
    own = offer.usd_per_vcpu_hr if offer is not None and offer.usd_per_vcpu_hr else None
    cpu = own * spec.cpu if own else cpu_hr
    if cpu is None or (spec.gpu and gpu_hr is None):
        return None, PRICE_NONE
    return cpu + (gpu_hr or 0.0) * spec.gpu, PRICE_PROVIDER if own else PRICE_MARKET


def _gpu_ok(o: Offer, spec: Spec) -> bool:
    if not spec.gpu:
        return True
    return o.gpu_free >= spec.gpu and any(variant_matches(v, spec) for v in o.gpu_variants)


def fits(o: Offer, spec: Spec, capacity: bool = True) -> list[str]:
    """Reasons an offer does NOT fit the order (empty = fits).  Placement attributes need the provider to state them (an unstated attribute cannot
    bid); capacity is the provider-wide advertised free total, so a fit here is an upper bound."""
    bad = []
    if spec.providers and o.id not in spec.providers:
        bad.append("not a chosen provider")
    if spec.audited_only and not o.audited:
        bad.append("not audited")
    for want, have, what in ((spec.region, o.region_code, "region"), (spec.cpu_arch, o.cpu_arch, "CPU architecture"), (spec.cpu_vendor, o.cpu_vendor, "CPU vendor")):
        if want and have != want:
            bad.append(f"{what} not {want}" if have else f"{what} not stated")
    if spec.gpu and not _gpu_ok(o, spec) and (capacity or not any(variant_matches(v, spec) for v in o.gpu_variants)):
        bad.append("no matching free GPU")
    if capacity:
        if o.cpu_free < spec.cpu:
            bad.append(f"only {o.cpu_free:.0f} vCPU free")
        if o.mem_free_gb < spec.mem_gb:
            bad.append(f"only {o.mem_free_gb:.0f} GB RAM free")
        if o.disk_free_gb < spec.disk_gb:
            bad.append(f"only {o.disk_free_gb:.0f} GB disk free")
    return bad


def advertised_matches(spec: Spec, offers: Iterable[Offer], cat: HardwareCatalog | None = None, cpu_hr: float | None = None, capacity: bool = True) -> list[Match]:
    """Offers whose ADVERTISED data fits the spec (evidence 'advertised'), most reliable first.  `capacity=False` keeps providers that match the
    placement constraints but do not currently have the free capacity (marked in notes)."""
    gpu = gpu_unit_price(spec, cat)
    out = []
    for o in offers:
        bad = fits(o, spec, capacity)
        if bad:
            continue
        short = fits(o, spec, True)
        est, src = estimate(spec, o, cpu_hr, gpu[1] if gpu else None)
        notes = ["no free capacity for this order: " + "; ".join(short)] if short else []
        if spec.gpu and len(o.gpu_variants) > 1:
            notes.append("free GPUs are counted across all of its GPU kinds")
        out.append(Match(o, ADVERTISED, o.gpu_free if spec.gpu else None, est, src, notes))
    return sorted(out, key=lambda m: (bool(m.notes and m.notes[0].startswith("no free")), -m.offer.uptime, -m.offer.cpu_free))


def merge_screening(spec: Spec, advertised: list[Match], reply: Mapping[str, Any], offers: Iterable[Offer], cat: HardwareCatalog | None, cpu_hr: float | None) -> Screening:
    """Combine an advertised list with a bid-screening reply: providers the network says can fit become 'screened' (also ones the advertised
    totals missed, since screening checks single nodes); the rest stay 'advertised' with a note.  A pin keeps only chosen providers."""
    from .providers.base import Offer  # late: base imports this module
    by_id = {m.offer.id: m for m in advertised}
    known = {o.id: o for o in offers}
    gpu = gpu_unit_price(spec, cat)
    rows: list[Match] = []
    hit: set[str] = set()
    for p in (reply.get("providers") or []) if isinstance(reply, Mapping) else []:
        if not isinstance(p, Mapping) or not p.get("owner"):
            continue
        owner = str(p["owner"])
        if spec.providers and owner not in spec.providers:
            continue
        hit.add(owner)
        inc = sum(int(i.get("incidentCount") or 0) for i in p.get("incidents") or [] if isinstance(i, Mapping))
        free = _int(p.get("availableGpus")) if spec.gpu else None
        if owner in by_id:
            m = by_id[owner]
        else:
            o = known.get(owner) or Offer(id=owner, name=str(p.get("organization") or p.get("hostUri") or owner)[:40], host=str(p.get("hostUri") or "").split("//")[-1].split(":")[0],
                                          region=str(p.get("location") or ""), audited=bool(p.get("isAudited")))
            est, src = estimate(spec, o, cpu_hr, gpu[1] if gpu else None)
            m = Match(o, ADVERTISED, None, est, src, [])
        m.evidence, m.incidents_7d = SCREENED, inc
        m.free_gpus = free if free is not None else m.free_gpus
        m.notes = [n for n in m.notes if not n.startswith("no free")]
        rows.append(m)
    rest = [m for k, m in by_id.items() if k not in hit]
    for m in rest:
        m.notes.append("no single node fits this exact order (screening)")
    return Screening(rows + rest, ok=True, screened=len(rows))


def alternatives(spec: Spec, offers: list[Offer], cat: HardwareCatalog | None, cpu_hr: float | None = None) -> list[str]:
    """When nothing fits: the closest orders that do, as short sentences.  First each relaxation on its own; when none alone helps, the
    relaxations added up one after the other (so 'fewer GPUs AND any region' can be found)."""
    out: list[str] = []

    def count(s: Spec) -> int:
        return len(advertised_matches(s, offers, cat, cpu_hr))
    tries: list[tuple[str, dict[str, Any]]] = []        # least important to the order first: that is the order they are added up in
    cap = 0
    if spec.gpu:
        e = cat.entry(GpuVariant(spec.gpu_vendor or "nvidia", norm_model(spec.gpu_model), spec.gpu_ram, spec.gpu_interface)) if cat and spec.gpu_model else None
        cap = (e.max_node_free if e else None) or 0
        if spec.gpu > 1:
            n = min(spec.gpu - 1, cap) if cap else spec.gpu - 1
            tries.append((f"{n} GPU(s) instead of {spec.gpu}" + (f" (at most {cap} per node)" if cap else ""), {"gpu": n}))
    if spec.region:
        tries.append(("any region", {"region": ""}))
    if spec.cpu_arch or spec.cpu_vendor:
        tries.append(("any CPU architecture / vendor", {"cpu_arch": "", "cpu_vendor": ""}))
    if spec.audited_only:
        tries.append(("unaudited providers too", {"audited_only": False}))
    if spec.providers:
        tries.append(("any provider", {"providers": ()}))
    if spec.gpu and (spec.gpu_ram or spec.gpu_interface):
        tries.append(("any VRAM / interface variant of that model", {"gpu_ram": "", "gpu_interface": ""}))
    if spec.gpu and spec.gpu_model:
        tries.append(("any GPU model of that vendor", {"gpu_model": "", "gpu_alt": (), "gpu_ram": "", "gpu_interface": ""}))
    for desc, kw in tries:
        if (n := count(spec.with_(**kw))):
            out.append(f"{n} provider(s) advertise capacity for {desc}")
    if not out:
        acc: dict[str, Any] = {}
        for i, (_, kw) in enumerate(tries):
            acc.update(kw)
            if i and (n := count(spec.with_(**acc))):
                out.append(f"{n} provider(s) advertise capacity for " + " and ".join(d for d, _ in tries[:i + 1]))
                break
    return out[:4]


def ceiling(spec: Spec, n: int = 1) -> float:
    """The most this order can cost: price cap x runtime hours x count."""
    return spec.max_usd_hr * spec.hours * n


# ---------------------------------------------------------------------------------------------- screening cache
class ScreenCache:
    """Cache + rate limit for the explicit bid-screening call: an identical request within `ttl` seconds is answered from memory, and two
    different requests are at least `gap` seconds apart (a request that comes sooner is refused with a message; the UI just retries)."""

    def __init__(self, ttl: float = SCREEN_TTL, gap: float = SCREEN_MIN_GAP, clock: Callable[[], float] = time.time) -> None:
        self.ttl, self.gap, self.clock = ttl, gap, clock
        self._hits: dict[str, tuple[float, Any]] = {}
        self._last = -1e18
        self._lock = threading.Lock()
        self.calls = 0

    def fetch(self, key: str, call: Callable[[], Any]) -> tuple[Any, bool, float]:
        """(reply, from_cache, fetched_at).  Raises RuntimeError('too fast') when a new request comes before `gap` has passed."""
        with self._lock:
            now = self.clock()
            hit = self._hits.get(key)
            if hit and now - hit[0] < self.ttl:
                return hit[1], True, hit[0]
            if now - self._last < self.gap:
                raise RuntimeError(f"availability checks are rate limited: wait {self.gap - (now - self._last):.0f} s")
            self._last = now
            self.calls += 1
        reply = call()
        with self._lock:
            self._hits[key] = (self.clock(), reply)
            if len(self._hits) > 32:
                self._hits.pop(next(iter(self._hits)))
        return reply, False, self.clock()

"""Hardware discovery: normalisation, the catalogue (also for hardware no list knows yet), offline behaviour, filtering / sorting of offers,
estimate sources, availability screening (cache, debounce, rate limit) and 'nothing here ever deploys'.  Fully offline: the saved public-API
payloads in tests/fixtures/hardware stand in for the network."""
import json
import time

import pytest
from hw_helpers import FakeNet, future_providers, load, payloads, providers, screening_reply

from clouddeck import hardware
from clouddeck.engine import ddc
from clouddeck.hardware import ADVERTISED, SCREENED, GpuVariant, ScreenCache
from clouddeck.providers.akash import AkashProvider
from clouddeck.providers.base import Offer, Provider, Spec


def cat_from(extra=None, **drop):
    pl = payloads(extra)
    return hardware.build_catalog(None if "providers" in drop else pl["/v1/providers"], None if "models" in drop else pl["/v1/gpu-models"],
                                  None if "placement" in drop else pl["/v1/placement-options"], None if "prices" in drop else pl["/v1/gpu-prices"])


# ------------------------------------------------------------------ normalisation
def test_gpu_attribute_keys_in_both_orders_and_garbage():
    full = GpuVariant("nvidia", "rtx4090", "24Gi", "pcie")
    assert hardware.parse_gpu_attr("capabilities/gpu/vendor/nvidia/model/rtx4090/ram/24Gi/interface/pcie") == full
    assert hardware.parse_gpu_attr("capabilities/gpu/vendor/nvidia/model/rtx4090/interface/PCIe/ram/24Gi") == full      # both orders occur in real data
    assert hardware.parse_gpu_attr("capabilities/gpu/vendor/NVIDIA/model/RTX4090") == GpuVariant("nvidia", "rtx4090")    # case folded
    assert hardware.parse_gpu_attr("capabilities/gpu/vendor/nvidia/model/h100/interface/SXM5") == GpuVariant("nvidia", "h100", "", "sxm")
    for bad in ("", None, 5, "capabilities/gpu", "capabilities/gpu/vendor/nvidia", "capabilities/cpu/arch", "capabilities/gpu/vendor/nvidia/model/x/pcie/ram/16Gi",
                "capabilities/gpu/vendor/nvidia/model/x/ram", "capabilities/gpu/vendor/nvidia/model/x/ram/lots", "capabilities/gpu/vendor/nv idia/model/x"):
        assert hardware.parse_gpu_attr(bad) is None, bad                                                                  # never raises


def test_interface_and_ram_normalisation():
    assert [hardware.norm_interface(x) for x in ("PCIe", "SXM4", "sxm5", "SXM2", "MIG", "OAM", "", None)] == ["pcie", "sxm", "sxm", "sxm", "mig", "oam", "", ""]
    assert [hardware.norm_ram(x) for x in ("24Gi", "24gi", "24 GB", 24, "512Mi", "0Gi", "lots", "", None)] == ["24Gi", "24Gi", "24Gi", "24Gi", "512Mi", "", "", "", ""]


def test_provider_facts_state_only_what_is_advertised():
    by = {p["_fixture_note"] + str(i): p for i, p in enumerate(providers())}
    arm = next(p for k, p in by.items() if k.startswith("cpu-arm"))
    f = hardware.provider_facts(arm)
    assert (f["cpu_vendor"], f["cpu_arch"], f["region_code"]) == ("arm", "arm-64", "")                                      # region not stated -> '' (unknown), not guessed
    assert f["gpus"] == (GpuVariant("nvidia", "rtxa2000", "6Gi", "pcie"),)                                                  # no inventory list: the provider's own GPU attributes are used
    sparse = next(p for k, p in by.items() if k.startswith("cpu-only-sparse"))
    f = hardware.provider_facts(sparse)
    assert f["cpu_vendor"] == f["cpu_arch"] == f["region_code"] == ""
    multi = next(p for p in providers() if len(p.get("gpuModels") or []) >= 3)
    assert {"rtx4000ada", "v100", "t4"} <= {v.model for v in hardware.provider_facts(multi)["gpus"]}
    assert GpuVariant("nvidia", "v100", "16Gi", "sxm") in hardware.provider_facts(multi)["gpus"]      # SXM2 -> sxm


# ------------------------------------------------------------------ catalogue
def test_catalogue_from_the_live_shaped_payloads():
    cat = cat_from()
    assert cat.providers_online == 20 and not cat.stale and not cat.errors                  # 22 listed, 2 offline
    assert cat.cpu_archs == {"x86-64": 16, "arm-64": 1} and cat.cpu_vendors["arm"] == 1 and "eu-west" in cat.regions
    assert cat.unknown == {"cpu_arch": 3, "cpu_vendor": 3, "region": 4}                     # providers that do not say: counted, never guessed
    assert cat.auditors == ["akash1AUDITOR"]
    v = {m.model: m for m in cat.models("nvidia")}
    assert [(e.variant.ram, e.variant.interface) for e in v["v100"].variants] == [("32Gi", "pcie"), ("16Gi", "sxm")]    # VRAM variants are kept
    assert v["v100"].any.free == 14 and v["v100"].any.max_node_free == 4
    assert v["rtx4090"].variants[0].price_avg == pytest.approx(0.34) and v["rtx4090"].variants[0].variant == GpuVariant("nvidia", "rtx4090", "24Gi", "pcie")
    assert v["h100"].variants[0].variant.interface == "sxm"                                  # SXM5 -> sxm
    assert v["b300"].any.free == 0 and v["b300"].any.price_avg is None                      # advertised, nothing free, no price: shown as such
    assert v["a40"].any.free is None and v["a40"].any.evidence == "supported-only"          # supported by the network, not offered now: None, not 0
    assert [m.model for m in cat.models("nvidia")][:3] == ["a100", "v100", "p2000"]         # free units first
    assert {x for x, _ in cat.vendors()} == {"nvidia", "amd"}


def test_future_vendors_and_models_appear_without_a_code_change():
    cat = cat_from(future_providers())
    vendors = dict(cat.vendors())
    assert vendors["zzvendor"] == 1 and "amd" in vendors
    amd = {m.model: m for m in cat.models("amd")}
    assert {"mi100", "mi60", "mi300x"} <= set(amd)                                           # supported list + a model only a provider advertises
    assert amd["mi300x"].variants[0].variant == GpuVariant("amd", "mi300x", "192Gi", "oam") and amd["mi300x"].any.evidence == ADVERTISED
    assert amd["mi100"].any.evidence == "supported-only" and amd["mi100"].any.free is None
    assert cat.entry(GpuVariant("zzvendor", "zz9000", "48Gi", "pcie")).providers == 1


def test_a_catalogue_needs_none_of_its_sources():
    full = cat_from()
    for drop in ("providers", "models", "placement", "prices"):
        c = cat_from(**{drop: True})
        assert c.gpus, drop                                                                  # any subset gives a usable catalogue
    assert cat_from(providers=True).cpu_archs == {} and cat_from(models=True).cpu_archs == full.cpu_archs
    assert cat_from(models=True).entry(GpuVariant("nvidia", "a40")) is None                  # without the model list the supported-only models are simply absent
    assert hardware.build_catalog(None, None, None, None).empty


def test_snapshot_round_trip():
    cat = cat_from(future_providers())
    again = hardware.HardwareCatalog.from_dict(json.loads(json.dumps(cat.to_dict())))
    assert again.gpus == cat.gpus and again.cpu_archs == cat.cpu_archs and again.auditors == cat.auditors and again.fetched_at == cat.fetched_at


def test_catalogue_age_text():
    now = time.time()
    assert hardware.HardwareCatalog(fetched_at=now - 10).age_text(now) == "just now"
    assert hardware.HardwareCatalog(fetched_at=now - 600).age_text(now) == "10 min ago"
    assert hardware.HardwareCatalog(fetched_at=now - 3 * 3600).age_text(now) == "3.0 h ago"
    assert hardware.HardwareCatalog().age_text(now) == "never"


# ------------------------------------------------------------------ the Akash adapter: sources, cache, offline
def test_catalog_one_providers_download_is_shared_with_offers(monkeypatch):
    net = FakeNet(monkeypatch)
    p = AkashProvider()
    assert len(p.offers()) == 20 and p.catalog().providers_online == 20 and p.offers() and p.catalog()
    assert net.count("/v1/providers") == 1                                                   # 3.8 MB: fetched once, cached
    assert all(net.count(x) == 1 for x in ("/v1/gpu-models", "/v1/placement-options", "/v1/gpu-prices"))
    assert not net.posts                                                                      # a catalogue read sends nothing


def test_offers_carry_what_providers_state(monkeypatch):
    FakeNet(monkeypatch)
    by = {o.id: o for o in AkashProvider().offers()}
    a = by["akash1FIXTURE001"]
    assert (a.cpu_vendor, a.cpu_arch, a.region_code) == ("intel", "x86-64", "na-us-southwest") and a.gpu_variants == (GpuVariant("nvidia", "a100", "80Gi", "sxm"),)
    arm = by["akash1FIXTURE016"]
    assert (arm.cpu_vendor, arm.cpu_arch, arm.region_code) == ("arm", "arm-64", "")
    assert by["akash1FIXTURE017"].cpu_arch == ""                                              # sparse attributes: unknown
    assert "akash1FIXTURE021" not in by and "akash1FIXTURE022" not in by                      # offline providers are never offered


def test_offline_with_a_saved_catalogue_is_stale_and_says_when(monkeypatch):
    net = FakeNet(monkeypatch)
    p = AkashProvider()
    good = p.catalog()
    assert not good.stale and (ddc.STATE_DIR / "hw-catalog.json").exists()
    net.down = True
    off = AkashProvider().catalog()                                                           # a new start with the network gone: the saved copy
    assert off.stale and off.gpus and off.sources["snapshot"].startswith("saved") and off.errors and off.cpu_archs == good.cpu_archs


def test_offline_without_anything_saved_is_an_empty_catalogue_not_an_exception(monkeypatch):
    net = FakeNet(monkeypatch)
    net.down = True
    c = AkashProvider().catalog()
    assert c.empty and not c.stale and c.errors and all(v.startswith("failed") for v in c.sources.values())
    with pytest.raises(Exception, match="provider list unavailable"):
        AkashProvider().offers()


def test_each_source_failing_alone_is_labelled_and_the_rest_still_work(monkeypatch):
    net = FakeNet(monkeypatch)
    net.fail = {"/v1/placement-options"}
    c = AkashProvider().catalog()
    assert c.stale and c.sources["placement-options"].startswith("failed") and c.sources["providers"] == "ok" and c.cpu_archs and c.gpus
    assert not (ddc.STATE_DIR / "hw-catalog.json").exists()                                   # an incomplete catalogue is not saved as "last good"


def test_a_provider_list_that_goes_stale_in_memory_is_served_with_its_age(monkeypatch):
    net = FakeNet(monkeypatch)
    p = AkashProvider()
    assert p.offers() and p.offers_status == "ok"
    p._cache["providers"] = (time.time() - 4000, p._cache["providers"][1])                    # older than the 5 minute TTL
    net.down = True
    assert len(p.offers()) == 20 and p.offers_status.startswith("cached")                     # last good list, labelled


def test_quote_gpu_price_comes_from_the_matching_variant_and_carries_its_source(monkeypatch):
    FakeNet(monkeypatch)
    p = AkashProvider()
    q = p.quote(Spec(cpu=8, mem_gb=32, disk_gb=100, gpu=2, gpu_model="RTX 4090", max_usd_hr=1))
    assert q.gpu_low_hr == pytest.approx(0.34 * 2) and q.gpu_high_hr == pytest.approx(0.34 * 2) and q.cpu_ref_hr == pytest.approx(123.54 / 730)
    assert q.source == "market-average" and q.usd_hr == pytest.approx(q.cpu_ref_hr + 0.68)
    none = p.quote(Spec(cpu=8, mem_gb=32, disk_gb=100, gpu=1, gpu_model="zz9000", max_usd_hr=1))
    assert none.gpu_low_hr is None and any("no market price" in n for n in none.notes)        # unknown price: said so, not guessed


# ------------------------------------------------------------------ matching
def offers_of(monkeypatch, extra=None):
    FakeNet(monkeypatch, extra)
    p = AkashProvider()
    return p, p.offers()


def test_filters_by_arch_vendor_region_and_audited(monkeypatch):
    p, offers = offers_of(monkeypatch)
    base = Spec(cpu=2, mem_gb=4, disk_gb=10, max_usd_hr=1)
    ids = lambda s, **kw: {m.offer.id for m in hardware.advertised_matches(s, offers, p.catalog(), 0.2, **kw)}   # noqa: E731
    everyone = ids(base)
    assert ids(base.with_(cpu_arch="arm-64")) == set() and ids(base.with_(cpu_arch="arm-64"), capacity=False) == {"akash1FIXTURE016"}   # the one ARM provider has nothing free: shown only when asked
    assert "akash1FIXTURE017" in everyone and "akash1FIXTURE017" not in ids(base.with_(cpu_arch="x86-64"))      # unstated attribute: excluded when filtering by it
    assert ids(base.with_(cpu_vendor="amd")) < everyone and all(o.cpu_vendor == "amd" for o in offers if o.id in ids(base.with_(cpu_vendor="amd")))
    assert ids(base.with_(region="eu-west")) == {o.id for o in offers if o.region_code == "eu-west"}
    assert ids(base.with_(audited_only=True)) == {o.id for o in offers if o.audited} & everyone
    assert ids(base.with_(providers=("akash1FIXTURE014",))) == {"akash1FIXTURE014"}
    assert ids(base.with_(cpu=10**6)) == set() and ids(base.with_(cpu=10**6), capacity=False) == {o.id for o in offers}      # capacity filter can be switched off (rows are then annotated)


def test_gpu_filters_vendor_model_variant_interface_and_any_of(monkeypatch):
    p, offers = offers_of(monkeypatch, future_providers())
    g = Spec(cpu=2, mem_gb=4, disk_gb=10, gpu=1, max_usd_hr=2)
    ids = lambda s: {m.offer.id for m in hardware.advertised_matches(s, offers, p.catalog(), 0.2)}   # noqa: E731
    assert ids(g.with_(gpu_model="a100")) == {"akash1FIXTURE001"} | ({"akash1FIXTURE020"} if next(o for o in offers if o.id == "akash1FIXTURE020").gpu_free else set())
    assert ids(g.with_(gpu_model="A100", gpu_ram="80Gi", gpu_interface="sxm")) == ids(g.with_(gpu_model="a100"))     # typed case is folded
    assert ids(g.with_(gpu_model="a100", gpu_ram="40Gi")) == set() and ids(g.with_(gpu_model="a100", gpu_interface="pcie")) == set()
    assert ids(g.with_(gpu_model="v100")) >= {"akash1FIXTURE013"}
    assert ids(g.with_(gpu_model="v100", gpu_ram="32Gi", gpu_interface="pcie")) == {"akash1FIXTURE013", "akash1FIXTURE003"}   # the PCIe 32 Gi variant (also on a multi-model provider)
    assert ids(g.with_(gpu_model="v100", gpu_interface="sxm")) == {"akash1FIXTURE003"}                                  # the SXM 16 Gi one lives on one provider only
    assert ids(g.with_(gpu_model="h100", gpu_alt=("rtx4090",))) == ids(g.with_(gpu_model="h100")) | ids(g.with_(gpu_model="rtx4090"))   # any-of
    assert ids(g.with_(gpu_vendor="amd")) == {"akash1FUTURE000"} and ids(g.with_(gpu_model="zz9000")) == {"akash1FUTURE001"}      # future vendor / model: no code change
    assert ids(g.with_(gpu_model="a100", gpu=10**3)) == set()                                                       # more GPUs than any provider has free


def test_offers_are_sorted_with_the_ones_that_fit_first(monkeypatch):
    p, offers = offers_of(monkeypatch)
    ms = hardware.advertised_matches(Spec(cpu=10**5, mem_gb=4, disk_gb=10, max_usd_hr=1), offers, p.catalog(), 0.2, capacity=False)
    assert ms and all(m.notes and m.notes[0].startswith("no free capacity") for m in ms)     # nothing has 100000 vCPU: annotated, not hidden
    fit = hardware.advertised_matches(Spec(cpu=4, mem_gb=4, disk_gb=10, max_usd_hr=1), offers, p.catalog(), 0.2)
    assert [m.offer.uptime for m in fit] == sorted((m.offer.uptime for m in fit), reverse=True)


def test_estimates_name_their_source_and_never_guess(monkeypatch):
    p, offers = offers_of(monkeypatch)
    cat = p.catalog()
    spec = Spec(cpu=8, mem_gb=16, disk_gb=50, gpu=2, gpu_model="rtx4090", max_usd_hr=2)
    gpu_avg = hardware.gpu_unit_price(spec, cat)[1]
    plain = Offer(id="o1", name="plain")
    own = Offer(id="o2", name="own", usd_per_vcpu_hr=0.01)
    assert hardware.estimate(spec, plain, 0.17, gpu_avg) == (pytest.approx(0.17 + 2 * 0.34), hardware.PRICE_MARKET)
    assert hardware.estimate(spec, own, 0.17, gpu_avg) == (pytest.approx(8 * 0.01 + 2 * 0.34), hardware.PRICE_PROVIDER)
    assert hardware.estimate(spec, plain, None, gpu_avg) == (None, hardware.PRICE_NONE)       # no CPU reference: unknown
    assert hardware.estimate(spec, plain, 0.17, None) == (None, hardware.PRICE_NONE)          # GPU without a market price: unknown
    cpu_only = spec.with_(gpu=0, gpu_model="")
    assert hardware.estimate(cpu_only, plain, 0.17, None) == (pytest.approx(0.17), hardware.PRICE_MARKET)
    assert hardware.gpu_unit_price(spec.with_(gpu_model="zz9000"), cat) is None
    m = hardware.advertised_matches(Spec(cpu=4, mem_gb=8, disk_gb=20, gpu=1, gpu_model="rtx4090", max_usd_hr=1), offers, cat, 0.17)[0]
    assert m.evidence == ADVERTISED and m.price_source == hardware.PRICE_MARKET and m.est_usd_hr == pytest.approx(0.17 + 0.34)


def test_a_zero_match_order_gets_closest_alternatives_without_raising(monkeypatch):
    p, offers = offers_of(monkeypatch)
    cat = p.catalog()
    spec = Spec(cpu=4, mem_gb=8, disk_gb=20, gpu=100, gpu_model="a100", gpu_ram="80Gi", region="eu-west", audited_only=True, max_usd_hr=2)
    assert hardware.advertised_matches(spec, offers, cat, 0.2) == []
    alts = hardware.alternatives(spec, offers, cat, 0.2)
    assert len(alts) == 1 and alts[0].endswith("8 GPU(s) instead of 100 (at most 8 per node) and any region")                    # no single change helps: the first that does together
    assert hardware.alternatives(spec.with_(region=""), offers, cat, 0.2)[0].endswith("8 GPU(s) instead of 100 (at most 8 per node)")    # bounded by what one node holds
    assert any("any region" in a for a in hardware.alternatives(spec.with_(gpu=1), offers, cat, 0.2))
    assert hardware.alternatives(Spec(cpu=10**6, mem_gb=1, disk_gb=1, max_usd_hr=1), offers, cat) == []      # nothing to relax


def test_a_provider_pin_is_only_ever_a_narrowing(monkeypatch):
    p, offers = offers_of(monkeypatch)
    spec = Spec(cpu=2, mem_gb=4, disk_gb=10, max_usd_hr=1)
    some = hardware.advertised_matches(spec, offers, p.catalog(), 0.2)
    pinned = hardware.advertised_matches(spec.with_(providers=(some[0].offer.id, "akash1UNKNOWN")), offers, p.catalog(), 0.2)
    assert [m.offer.id for m in pinned] == [some[0].offer.id]


# ------------------------------------------------------------------ the explicit availability check
def test_screen_sends_the_order_it_describes_and_labels_the_result(monkeypatch):
    net = FakeNet(monkeypatch)
    p = AkashProvider()
    spec = Spec(cpu=4, mem_gb=8, disk_gb=20, gpu=1, gpu_model="rtx4090", gpu_ram="24Gi", gpu_interface="pcie", cpu_arch="x86-64", region="na-us-southeast",
                max_usd_hr=0.9)
    owners = [m.offer.id for m in p.matches(spec)]
    assert owners
    net.screening = screening_reply(owners[:1] + ["akash1ELSEWHERE"], gpus=1, incidents=2)
    s = p.screen(spec)
    path, body = net.posts[-1]
    assert path == "/v1/bid-screening"
    r = body["resources"][0]["resource"]
    assert r["gpu"] == {"units": {"val": "1"}, "attributes": [{"key": "vendor/nvidia/model/rtx4090/ram/24Gi/interface/pcie", "value": "true"}]}
    assert r["cpu"]["units"]["val"] == "4000" and r["memory"]["quantity"]["val"] == str(8 * 2**30) and r["storage"][0]["quantity"]["val"] == str(20 * 2**30)
    assert body["requirements"]["attributes"] == [{"key": "capabilities/cpu/arch", "value": "x86-64"}, {"key": "location-region", "value": "na-us-southeast"}]
    assert body["resources"][0]["price"]["amount"] == str(int(0.9 * 1e6 / 600))
    assert s.ok and s.screened == 2 and not s.cached
    by = {m.offer.id: m for m in s.matches}
    assert by[owners[0]].evidence == SCREENED and by[owners[0]].incidents_7d == 2 and by[owners[0]].free_gpus == 1
    assert by["akash1ELSEWHERE"].evidence == SCREENED and by["akash1ELSEWHERE"].offer.name == "org-akash1ELSEWHERE"            # a provider the list does not know: built from the reply
    assert all(m.est_usd_hr is None or m.price_source in (hardware.PRICE_MARKET, hardware.PRICE_PROVIDER) for m in s.matches)   # an estimate, never labelled a bid
    assert "screened 2 provider(s)" in s.summary


def test_providers_the_network_did_not_return_stay_advertised_with_a_note(monkeypatch):
    net = FakeNet(monkeypatch)
    p = AkashProvider()
    spec = Spec(cpu=2, mem_gb=4, disk_gb=10, max_usd_hr=1)
    adv = p.matches(spec)
    net.screening = screening_reply([adv[0].offer.id])
    s = p.screen(spec)
    assert s.matches[0].evidence == SCREENED
    rest = [m for m in s.matches if m.evidence == ADVERTISED]
    assert len(rest) == len(adv) - 1 and all("no single node fits" in m.notes[-1] for m in rest)


def test_screening_respects_a_pin_and_the_audited_requirement(monkeypatch):
    net = FakeNet(monkeypatch)
    p = AkashProvider()
    spec = Spec(cpu=2, mem_gb=4, disk_gb=10, max_usd_hr=1, audited_only=True, providers=("akash1FIXTURE001",))
    net.screening = screening_reply(["akash1FIXTURE001", "akash1FIXTURE002"])
    s = p.screen(spec)
    assert [m.offer.id for m in s.matches if m.evidence == SCREENED] == ["akash1FIXTURE001"]       # the pin removes the other one
    assert net.posts[-1][1]["requirements"]["signedBy"] == {"allOf": [], "anyOf": ["akash1AUDITOR"]}   # audited -> the network's auditor signs


def test_screening_is_cached_for_60_seconds_and_rate_limited():
    t = [1000.0]
    c = ScreenCache(ttl=60, gap=2, clock=lambda: t[0])
    calls = []
    call = lambda: calls.append(1) or {"n": len(calls)}   # noqa: E731
    assert c.fetch("a", call) == ({"n": 1}, False, 1000.0)
    t[0] += 0.5
    assert c.fetch("a", call)[1:] == (True, 1000.0) and len(calls) == 1                         # identical request: from memory, no request
    with pytest.raises(RuntimeError, match="rate limited"):
        c.fetch("b", call)                                                                      # a different request right after the last one: refused, not sent
    assert len(calls) == 1
    t[0] += 3
    assert c.fetch("b", call)[0] == {"n": 2}
    t[0] += 61
    assert c.fetch("a", call)[0] == {"n": 3} and c.calls == 3                                   # expired: asked again


def test_screen_failure_leaves_the_advertised_rows_and_says_why(monkeypatch):
    net = FakeNet(monkeypatch)
    p = AkashProvider()
    spec = Spec(cpu=2, mem_gb=4, disk_gb=10, max_usd_hr=1)
    n = len(p.matches(spec))
    net.fail = {"/v1/bid-screening"}
    s = p.screen(spec)
    assert not s.ok and len(s.matches) == n and all(m.evidence == ADVERTISED for m in s.matches)
    assert "screening unavailable" in s.summary and "advertised capacity only" in s.summary
    p._screen = ScreenCache(gap=3600)                                                           # and a second different request too soon is a message, not a crash
    net.fail = set()
    p.screen(spec)
    s2 = p.screen(spec.with_(cpu=4))
    assert not s2.ok and "rate limited" in s2.error


def test_nothing_in_discovery_can_create_a_deployment(monkeypatch):
    """Catalogue, offers, advertised matches and screening never touch the authenticated API: no deployment, bid or lease can come out of them."""
    net = FakeNet(monkeypatch)
    monkeypatch.setattr(ddc, "api", lambda *a, **k: (_ for _ in ()).throw(AssertionError("the authenticated API must not be used for discovery")))
    p = AkashProvider()
    spec = Spec(cpu=2, mem_gb=4, disk_gb=10, gpu=1, gpu_model="a100", max_usd_hr=1)
    p.catalog(), p.offers(), p.matches(spec), p.screen(spec), p.quote(spec)
    assert {path for path, _ in net.posts} <= {"/v1/pricing", "/v1/bid-screening"}
    assert Provider.screen is not AkashProvider.screen                                         # (the contract's default refuses; Akash overrides with the read-only check)


def test_the_default_provider_contract_still_works_for_third_party_providers():
    class Mini(Provider):
        id, label = "mini", "Mini"
        def offers(self): return [Offer(id="o", name="o", cpu_free=8, mem_free_gb=16, disk_free_gb=100)]
        def quote(self, spec): raise Exception("no prices")
        def instances(self, refresh=False): return []
        def deploy(self, spec, log=print): raise NotImplementedError
        def terminate(self, instance_id, log=print, *, discard_workspace=False): return 0.0
    m = Mini()
    assert m.catalog().gpus == [] and not m.has("hardware")
    assert [x.offer.id for x in m.matches(Spec(cpu=4, mem_gb=4, disk_gb=10, max_usd_hr=1))] == ["o"]      # derived from offers(); a failing quote() does not matter
    with pytest.raises(Exception, match="cannot check availability"):
        m.screen(Spec(max_usd_hr=1))
    assert m.problems(Spec(cpu=0, max_usd_hr=1))


def test_live_payload_fixtures_are_what_the_tests_say_they_are():
    assert len(load("providers_sample.json")) == 22 and load("bid_screening_samples.json")["gpu_rtx4090_x1"]["providers"][0]["availableGpus"] == 1
    assert load("bid_screening_400_invalid_attr_key.json")["status"] == 400

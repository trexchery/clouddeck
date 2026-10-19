"""Custom hardware inside the existing safety net: old profiles and presets unchanged, the guard and the price ceiling still apply, a provider
pin can only narrow, and an order nobody fills ends in a normal 'no bids' result with the created deployment closed."""
import json

import pytest
from fake_akash import FakeAkash

from clouddeck import hardware
from clouddeck.engine import ddc
from clouddeck.providers import demo
from clouddeck.providers.akash import AkashProvider
from clouddeck.providers.base import Instance, ProviderError, Spec
from clouddeck.tui.compute import spec_of

AUDITOR = "akash1AUDITORFIXTURE"
PIN = "akash1provb"


def state(**deps):
    ddc.write_json(ddc.STATE_FILE, {"deps": deps})


# ------------------------------------------------------------------ backward compatibility
def test_a_spec_built_the_old_way_is_unchanged():
    s = Spec(16, 32, 100, 0, "", "box", 0.5)                                # positional, as the demo provider builds them
    assert (s.cpu, s.mem_gb, s.disk_gb, s.gpu, s.gpu_model, s.role, s.max_usd_hr, s.hours, s.preset) == (16, 32, 100, 0, "", "box", 0.5, 4, "")
    assert not s.extended() and s.profile_suffix() == "" and s.placement_label() == ""
    assert s.to_profile() == {"cpu": 16, "mem": "32Gi", "disk": "100Gi", "max_usd_hr": 0.5}
    g = Spec(8, 32, 100, 1, "RTX 4090", "box", 0.8)
    assert g.to_profile() == {"cpu": 8, "mem": "32Gi", "disk": "100Gi", "max_usd_hr": 0.8, "gpu": 1, "gpu_model": "rtx4090"}       # exactly what custom profiles always held
    assert len(Spec(cpu=0, mem_gb=2000, disk_gb=0, gpu=9, hours=0, role="x", max_usd_hr=0).validate()) == 7


def test_config_presets_are_the_same_specs_as_before():
    p = AkashProvider().presets()
    assert set(p) >= {"16", "32", "64"}
    s = p["32"]
    assert (s.cpu, s.mem_gb, s.disk_gb, s.gpu, s.gpu_model, s.max_usd_hr, s.preset) == (32, 64, 100, 0, "", 0.9, "32") and not s.extended()
    assert Spec.from_profile(s.to_profile(), preset="32") == s


def test_an_existing_custom_profiles_file_loads_unchanged_and_its_names_are_kept(tmp_path):
    legacy = {"x8-32-100-g1rtx4090": {"cpu": 8, "mem": "32Gi", "disk": "100Gi", "max_usd_hr": 0.8, "gpu": 1, "gpu_model": "rtx4090"},
              "x4-8-20": {"cpu": 4, "mem": "8Gi", "disk": "20Gi", "max_usd_hr": 0.2}}
    (ddc.STATE_DIR / "custom_profiles.json").write_text(json.dumps(legacy), encoding="utf-8")
    ddc.load_custom_profiles()
    p = AkashProvider()
    assert set(legacy) <= set(ddc.CFG["profiles"]) and not set(legacy) & set(p.presets())      # loaded, but not offered as presets
    assert ddc.config_problems() == []
    assert p._profile_for(Spec(cpu=8, mem_gb=32, disk_gb=100, gpu=1, gpu_model="RTX 4090", max_usd_hr=0.8)) == "x8-32-100-g1rtx4090"
    assert json.loads((ddc.STATE_DIR / "custom_profiles.json").read_text(encoding="utf-8")) == legacy        # nothing rewritten, no new keys added to old entries
    assert Spec.from_profile(legacy["x8-32-100-g1rtx4090"]) == Spec(cpu=8, mem_gb=32, disk_gb=100, gpu=1, gpu_model="rtx4090", max_usd_hr=0.8)
    assert ddc.render_sdl("box", "x8-32-100-g1rtx4090").count("model: rtx4090") == 1


def test_extended_orders_get_their_own_profile_names_and_round_trip():
    p = AkashProvider()
    base = Spec(cpu=8, mem_gb=32, disk_gb=100, gpu=1, gpu_model="a100", max_usd_hr=1.5)
    plain = p._profile_for(base)
    eu = p._profile_for(base.with_(region="eu-west"))
    us = p._profile_for(base.with_(region="na-us-west"))
    assert plain == "x8-32-100-g1a100" and len({plain, eu, us}) == 3 and eu.startswith(plain + "-") and len(eu) <= 40
    big = base.with_(gpu_model="a-very-long-model-id-that-is-allowed", gpu_alt=("another-fairly-long-model-id",), cpu=128, mem_gb=1000, disk_gb=3000, gpu=8, region="eu-west")
    name = p._profile_for(big.with_(max_usd_hr=2.0))
    assert len(name) <= 40 and name in ddc.CFG["profiles"]                                                   # over-long names are shortened, never refused
    stored = json.loads((ddc.STATE_DIR / "custom_profiles.json").read_text(encoding="utf-8"))
    assert stored[eu]["region"] == "eu-west" and stored[eu]["gpu_model"] == "a100" and "gpu_vendor" not in stored[eu]       # new keys only when set
    assert Spec.from_profile(stored[eu]) == base.with_(region="eu-west")
    pin = base.with_(providers=(PIN,), cpu_arch="x86-64", gpu_ram="80Gi", gpu_interface="sxm", gpu_alt=("h100",))
    assert Spec.from_profile(ddc.CFG["profiles"][p._profile_for(pin)]) == pin
    assert p._profile_for(pin) == p._profile_for(pin) and p._profile_for(pin) != p._profile_for(pin.with_(providers=("akash1other",)))


def test_a_preset_with_a_placement_is_not_the_preset_any_more():
    p = AkashProvider()
    pre = p.presets()["32"]
    assert p._profile_for(pre) == "32"
    assert p._profile_for(pre.with_(region="eu-west")) != "32"
    assert p._profile_for(pre.with_(cpu_arch="arm-64")).startswith("x32-64-100-")


def test_a_resize_keeps_the_hardware_the_instance_was_ordered_with():
    spec = Spec(cpu=8, mem_gb=32, disk_gb=100, gpu=1, gpu_model="a100", gpu_ram="80Gi", region="eu-west", providers=(PIN,), audited_only=True, max_usd_hr=1.5)
    ddc.CFG["profiles"]["x-test"] = {**spec.to_profile(), "signed_by": [AUDITOR]}
    inst = Instance(id="1", name="ddc-box-x", role="box", state="running", provider="akash", cpu=8, mem_gb=32, disk_gb=100, gpu=1, gpu_model="a100", usd_hr=0.5, limit_h=4,
                    meta={"profile": "x-test"})
    got = spec_of(inst, None)
    assert (got.gpu_ram, got.region, got.providers, got.audited_only) == ("80Gi", "eu-west", (PIN,), True) and got.max_usd_hr == 1.5
    plain = Instance(id="2", name="ddc-box-y", role="box", state="running", provider="akash", cpu=8, mem_gb=16, disk_gb=50, usd_hr=0.1, limit_h=4, meta={"profile": "nope"})
    assert not spec_of(plain, None).extended()


# ------------------------------------------------------------------ the spend guard still applies to any hardware
def guard_of(**cfg):
    ddc.CFG.update(cfg)
    return AkashProvider.limits()


def test_the_price_ceiling_is_a_validated_config_key_default_2_never_above_100():
    assert ddc.price_ceiling() == 2.0 == ddc.HARD_MAX_USD_HR
    for ok in (0.5, 2, 5.5, 100, 100.0):
        ddc.CFG["max_price_usd_hr"] = ok
        assert ddc.price_ceiling() == float(ok) and not [m for m in ddc.config_problems() if m.startswith("max_price_usd_hr")]
    for bad in (0, -1, 100.01, 1000, "5", True, float("nan"), float("inf"), None):
        ddc.CFG["max_price_usd_hr"] = bad
        assert any(m.startswith("max_price_usd_hr") for m in ddc.config_problems()), bad                  # reported ...
        assert ddc.price_ceiling() == 2.0                                                         # ... and never silently widens the limit


def test_a_gpu_order_above_the_default_ceiling_is_refused_until_the_user_raises_it(monkeypatch):
    FakeAkash(ddc, monkeypatch).install()
    p = AkashProvider()
    gpu = Spec(cpu=16, mem_gb=64, disk_gb=200, gpu=8, gpu_model="h100", max_usd_hr=8.0, hours=4)
    assert gpu.validate() == []
    v = p.guard(gpu)
    assert not v.ok and "max_usd_hr_hard" in v.text and "max_price_usd_hr" in v.text and v.ceiling_usd == pytest.approx(32.0)
    with pytest.raises(ProviderError, match="max_price_usd_hr"):
        p.deploy(gpu)
    assert not [n for n in ddc.CFG["profiles"] if n.startswith("x16-64-200-g8")]                # refused before a profile was even saved
    ddc.CFG["max_price_usd_hr"] = 10.0                                                            # the user's explicit choice
    ddc.CFG["budget_usd"] = 100.0
    assert p.guard(gpu).ok
    assert not p.guard(gpu.with_(max_usd_hr=11.0)).ok                                            # still capped, at the user's number
    ddc.CFG["max_price_usd_hr"] = 100.0
    assert p.guard(gpu.with_(max_usd_hr=11.0, hours=1)).ok and not p.guard(gpu.with_(max_usd_hr=100.01, hours=1)).ok


def test_unsafe_disable_limits_still_lifts_the_price_ceiling_and_nothing_else_changed():
    big = {"cpu": 8, "mem": "8Gi", "disk": "10Gi", "max_usd_hr": 50.0}
    ddc.CFG["profiles"]["big"] = big
    assert any("hard ceiling" in m for m in ddc.profile_problems("big", big))
    ddc.CFG["max_price_usd_hr"] = 100
    assert ddc.profile_problems("big", big) == []                                                # the key raises it ...
    ddc.CFG["max_price_usd_hr"] = 2
    ddc.CFG["unsafe_disable_limits"] = True
    assert ddc.profile_problems("big", big) == []                                                # ... and so does the loud override, exactly as before
    assert "max_usd_hr_hard" not in AkashProvider.limits()                                       # the controller's guard mirror does not impose the ceiling either


def test_custom_hardware_is_held_to_budget_lease_and_worker_limits(monkeypatch):
    FakeAkash(ddc, monkeypatch).install()
    p = AkashProvider()
    ddc.CFG.update(max_price_usd_hr=5.0, budget_usd=10.0, reserve_usd=1.0, budget_guard=True)
    gpu = Spec(cpu=8, mem_gb=32, disk_gb=100, gpu=2, gpu_model="a100", gpu_ram="80Gi", region="eu-west", max_usd_hr=3.0, hours=4)
    v = p.guard(gpu)                                                                                # worst case 12 USD against a 10 USD budget
    assert not v.ok and "would exceed budget" in v.text and v.ceiling_usd == pytest.approx(12.0)
    assert p.guard(gpu.with_(hours=3)).ok                                                           # 9 USD fits
    ddc.CFG.update(budget_guard=False, max_total_leases=1)
    state(a={"ours": True, "name": "ddc-box-x", "role": "box", "status": "ready", "profile": "16", "usd_hr": 0.1, "lease_at": 0, "limit_h": 4})
    v = p.guard(gpu.with_(hours=1))
    assert not v.ok and "max_total_leases" in v.text                                                # the lease cap holds with the budget guard off
    ddc.CFG["max_total_leases"] = 4
    v = p.guard(gpu.with_(hours=48))
    assert not v.ok and "hard ceiling" in v.text                                                    # the 24 h runtime ceiling too


def test_the_picker_warns_before_confirm_and_the_controller_checks_again_at_deploy(monkeypatch):
    fake = FakeAkash(ddc, monkeypatch).install()
    p = AkashProvider()
    spec = Spec(cpu=8, mem_gb=32, disk_gb=100, gpu=1, gpu_model="a100", max_usd_hr=1.0, hours=4)
    assert p.guard(spec).ok
    ddc.CFG["budget_usd"] = 0.5                                                                     # the limits change between the preview and the deploy
    with pytest.raises(ProviderError, match="would exceed budget"):
        p.deploy(spec)
    assert not fake.deployments


# ------------------------------------------------------------------ bid choice: a pin or "audited only" can only remove bids
def bid(provider, amount=500, state="open"):
    return {"bid": {"id": {"provider": provider, "dseq": "1", "gseq": 1, "oseq": 1}, "state": state, "price": {"denom": "uact", "amount": str(amount)}}}


INFO = {"akash1a": {"uptime7d": 0.99, "isAudited": True, "hostUri": "https://a.example:8443"}, "akash1b": {"uptime7d": 0.99, "isAudited": False, "hostUri": "https://b.example:8443"},
        "akash1c": {"uptime7d": 0.5, "isAudited": True, "hostUri": "https://c.example:8443"}}
BIDS = [bid("akash1a", 600), bid("akash1b", 400), bid("akash1c", 100), bid("akash1d", 300)]


def rank(**kw):
    args = dict(bids=BIDS, pinfo=INFO, perf={}, counts={}, min_uptime=0.95, max_per_provider=4)
    args.update(kw)
    return [r["provider"] for r in ddc.rank_bids(args.pop("bids"), args.pop("pinfo"), args.pop("perf"), args.pop("counts"), args.pop("min_uptime"), args.pop("max_per_provider"), **args)]


def test_rank_bids_default_behaviour_is_unchanged():
    assert rank() == ["akash1d", "akash1b", "akash1a"]                                              # cheapest first; the 0.5 uptime provider is out; unknown providers pass as before
    assert rank(only=None, audited_only=False) == rank() and rank(only=[]) == rank()


def test_a_provider_pin_keeps_only_the_chosen_providers():
    assert rank(only=["akash1a"]) == ["akash1a"] and rank(only=("akash1a", "akash1b")) == ["akash1b", "akash1a"]
    assert rank(only=["akash1zzz"]) == []                                                           # nobody chosen bid: nothing acceptable (not "take the cheapest anyway")


def test_a_pin_never_lets_through_what_the_other_rules_reject():
    assert rank(only=["akash1c"]) == []                                                            # chosen but below the minimum 7-day uptime
    assert rank(only=["akash1a"], counts={"akash1a": 4}) == []                                       # chosen but already at the per-provider lease cap
    assert rank(only=["akash1a"], perf={"akash1a": {"bad_until": 10**12}}) == []                    # chosen but marked bad after a failed provisioning
    assert rank(only=["akash1a"], max_usd_hr=0.2) == [] and rank(only=["akash1a"], bids=[bid("akash1a", 1000, state="closed")]) == []   # price cap and bid state too
    assert rank(only=["akash1a"], bids=[{"bid": {**bid("akash1a")["bid"], "price": {"denom": "uakt", "amount": "5"}}}]) == []


def test_audited_only_drops_unaudited_and_unknown_providers():
    assert rank(audited_only=True) == ["akash1a"]                                                   # b is not audited, d is unknown (counts as not audited), c is audited but unreliable
    assert rank(audited_only=True, only=["akash1b"]) == []


# ------------------------------------------------------------------ the created deployment: pin honoured, no-bid outcome handled
@pytest.fixture
def net(monkeypatch):
    fake = FakeAkash(ddc, monkeypatch).install()
    ddc._prov_cache.update({"akash1prova": {"uptime7d": 1.0, "isAudited": True, "hostUri": "https://prov-a.example:8443"},
                            "akash1provb": {"uptime7d": 1.0, "isAudited": False, "hostUri": "https://prov-b.example:8443"},
                            "akash1provc": {"uptime7d": 1.0, "isAudited": True, "hostUri": "https://prov-c.example:8443"}})
    return fake


def test_a_pinned_order_is_leased_from_the_chosen_provider_and_the_confirmed_bids_are_recorded(net):
    spec = Spec(cpu=4, mem_gb=8, disk_gb=20, max_usd_hr=0.5, hours=4, providers=("akash1provb",))
    name = AkashProvider()._profile_for(spec)
    box = ddc.create_box("box", name, 0, {}, limit_h=4)
    assert box["provider"] == "akash1provb" and net.deployments[box["dseq"]]["provider"] == "akash1provb"
    assert [b["provider"] for b in ddc.load_state()["deps"][box["dseq"]]["bids"]] == ["akash1provb"]       # only the chosen provider's bid was acceptable
    assert ddc.load_state()["deps"][box["dseq"]]["bids"][0]["usd_hr"] == pytest.approx(0.3)


def test_unpinned_orders_record_the_real_bids_for_the_detail_view(net):
    box = ddc.create_box("box", "16", 0, {}, limit_h=4)
    bids = ddc.load_state()["deps"][box["dseq"]]["bids"]
    assert {b["provider"] for b in bids} == {"akash1prova", "akash1provb", "akash1provc"} and all(set(b) == {"provider", "host", "usd_hr", "speed"} for b in bids)
    inst = AkashProvider()._inst(box["dseq"], ddc.load_state()["deps"][box["dseq"]])
    assert inst.meta["bids"] == bids


def test_a_pin_nobody_answers_is_a_normal_no_bids_result_and_the_deployment_is_closed(net):
    spec = Spec(cpu=4, mem_gb=8, disk_gb=20, max_usd_hr=0.5, hours=4, providers=("akash1nobody",))
    name = AkashProvider()._profile_for(spec)
    with pytest.raises(ddc.DdcError, match=r"no acceptable bids \(only bids from the 1 chosen provider\(s\) are accepted\)"):
        ddc.create_box("box", name, 0, {}, limit_h=4)
    assert net.open_dseqs() == []                                                                   # the order that was created is closed again: nothing is left billing
    (dseq,) = net.deployments
    assert net.deployments[dseq]["state"] == "closed" and not net.deployments[dseq].get("leased")  # and no lease was ever made
    assert ddc.load_state()["deps"][dseq]["status"] == "closed" and not ddc.load_state().get("pending")
    assert ("DELETE", f"/v1/deployments/{dseq}") in net.calls and not [c for c in net.calls if c[0] == "POST" and c[1] == "/v1/leases"]


def test_the_provider_deploy_reports_the_no_bid_outcome_as_an_error_not_a_crash(net):
    p = AkashProvider()
    with pytest.raises(ProviderError, match="no acceptable bids"):
        p.deploy(Spec(cpu=4, mem_gb=8, disk_gb=20, max_usd_hr=0.5, hours=4, providers=("akash1nobody",)))
    assert net.open_dseqs() == []


def test_audited_only_orders_take_the_auditor_from_the_network_data_and_only_audited_bids(net, monkeypatch):
    monkeypatch.setattr(AkashProvider, "catalog", lambda self: hardware.HardwareCatalog(auditors=[AUDITOR], gpus=[], fetched_at=1.0))
    sent = []
    real = ddc.api
    monkeypatch.setattr(ddc, "api", lambda m, path, body=None, *a, **k: (sent.append((m, path, body)), real(m, path, body, *a, **k))[1])
    spec = Spec(cpu=4, mem_gb=8, disk_gb=20, max_usd_hr=0.5, hours=4, audited_only=True)
    box = AkashProvider().deploy(spec)
    sdl = next(b["data"]["sdl"] for m, path, b in sent if (m, path) == ("POST", "/v1/deployments"))
    assert "signedBy:\n        anyOf:\n          - \"%s\"" % AUDITOR in sdl
    assert net.deployments[box.id]["provider"] in ("akash1prova", "akash1provc")                  # prov-b is not audited: its bid was never acceptable
    stored = json.loads((ddc.STATE_DIR / "custom_profiles.json").read_text(encoding="utf-8"))
    assert next(iter(stored.values()))["signed_by"] == [AUDITOR]


def test_an_audited_order_without_a_known_auditor_is_refused_not_sent_to_everyone(net, monkeypatch):
    monkeypatch.setattr(AkashProvider, "catalog", lambda self: hardware.HardwareCatalog())
    with pytest.raises(ProviderError, match="auditor address is unknown"):
        AkashProvider().deploy(Spec(cpu=4, mem_gb=8, disk_gb=20, max_usd_hr=0.5, hours=4, audited_only=True))
    assert not net.deployments


# ------------------------------------------------------------------ the demo provider has the same contract
def test_the_demo_provider_serves_the_same_contract_offline():
    d = demo.DemoProvider(latency=0)
    assert d.has("hardware") and d.has("screen")
    cat = d.catalog()
    assert {"nvidia", "amd"} <= {v for v, _ in cat.vendors()} and cat.entry(hardware.GpuVariant("nvidia", "zx9000", "48Gi", "pcie")).price_avg is None
    spec = Spec(cpu=4, mem_gb=8, disk_gb=20, gpu=1, gpu_vendor="amd", gpu_model="mi100", max_usd_hr=2.0)
    ms = d.matches(spec)
    assert [m.offer.name for m in ms] == ["echo"] and ms[0].evidence == hardware.ADVERTISED
    s = d.screen(spec)
    assert s.ok and s.matches[0].evidence == hardware.SCREENED and ("screen", spec) in d.calls
    assert d.quote(spec).source == "market-average" and d.quote(spec).gpu_low_hr == pytest.approx(0.45)
    d.hw_state = "down"
    assert d.catalog().empty and not d.screen(spec).ok
    with pytest.raises(ProviderError, match="unavailable"):
        d.matches(spec)

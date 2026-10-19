"""Provider contract, Akash adapter (offline), and the new ddc.py pieces: GPU SDL, custom profiles, snooze, safe state writes."""
import datetime as dt
import json
import time

import pytest

from clouddeck import providers
from clouddeck.engine import ddc
from clouddeck.providers.akash import AkashProvider, gi, norm_gpu
from clouddeck.providers.base import Instance, Provider, ProviderError, Spec, Unsupported


def state(**deps):
    ddc.write_json(ddc.STATE_FILE, {"deps": deps})


def dep(role="worker", status="ready", usd_hr=0.1, profile="32", **kw):
    return {"ours": True, "name": f"ddc-{role}-32-x", "role": role, "status": status, "profile": profile, "usd_hr": usd_hr, "lease_at": time.time() - 3600,
            "limit_h": 4, "provider": "akash1p", "provider_host": "h.example", "host": "h.example", "port": 30001, "idx": 1, **kw}


# ------------------------------------------------------------------ Spec
def test_spec_validation():
    ok = Spec(cpu=8, mem_gb=16, disk_gb=50, max_usd_hr=0.5)
    assert ok.validate() == []
    bad = Spec(cpu=0, mem_gb=2000, disk_gb=0, gpu=9, hours=0, role="x", max_usd_hr=0).validate()
    assert len(bad) == 7
    assert any("GPU model" in b for b in Spec(gpu_model="a100", max_usd_hr=1).validate())
    assert Spec(max_usd_hr=101).validate() and not Spec(max_usd_hr=100).validate()   # the absolute ceiling is $100/h; the configured limit (max_price_usd_hr) is applied by the guard
    assert Spec(max_usd_hr=1, hours=49).validate() and not Spec(max_usd_hr=1, hours=48).validate()   # the platform allows at most 48 h


# ------------------------------------------------------------------ registry / plugin architecture
def test_registry_has_akash_and_demo_and_builds_them():
    names = providers.available()
    assert {"akash", "demo"} <= set(names)
    assert providers.get("demo").id == "demo"
    with pytest.raises(ProviderError):
        providers.get("nope")


def test_a_new_provider_needs_only_the_five_abstract_methods():
    class Mini(Provider):
        id, label = "mini", "Mini cloud"
        def __init__(self): self.items = []
        def offers(self): return []
        def quote(self, spec): raise Unsupported("no prices")
        def instances(self, refresh=False): return list(self.items)
        def deploy(self, spec, log=print):
            i = Instance(id=str(len(self.items)), name="m", role=spec.role, state="running", provider="mini", usd_hr=spec.max_usd_hr)
            self.items.append(i)
            return i
        def terminate(self, instance_id, log=print):
            self.items = [i for i in self.items if i.id != instance_id]
            return 0.0

    p = Mini()
    providers.register(Mini)
    assert providers.get("mini").id == "mini"
    p.scale(3, Spec(max_usd_hr=0.1))              # generic scale from the base class
    assert len(p.instances()) == 3
    p.scale(1, Spec(max_usd_hr=0.1))
    assert len(p.instances()) == 1
    assert p.has("gpu") is False
    with pytest.raises(Unsupported):
        p.checkpoint()
    with pytest.raises(Unsupported):
        p.hold_idle(5)
    del providers._REGISTRY["mini"]


# ------------------------------------------------------------------ demo provider behaviour
def test_demo_enforces_dev_rules_and_dev_terminate_closes_workers():
    d = providers.get("demo")
    assert [i.role for i in d.instances()][:4] == ["dev", "worker", "worker", "worker"]
    with pytest.raises(ProviderError, match="already running"):
        d.deploy(Spec(role="dev", max_usd_hr=1))
    with pytest.raises(ProviderError, match="refusing"):
        d.terminate("1000000000001")           # the external deployment
    dev = d.instances()[0]
    d.terminate(dev.id)
    assert not [i for i in d.instances() if i.role in ("dev", "worker")]
    with pytest.raises(ProviderError, match="dev box first"):
        d.deploy(Spec(role="worker", max_usd_hr=1))


# ------------------------------------------------------------------ Akash adapter, offline
def test_helpers():
    assert gi("64Gi") == 64 and gi("512Mi") == 0.5 and gi("") == 0
    assert norm_gpu("RTX-4090") == "rtx4090"


def test_akash_instances_come_from_state_and_never_include_closed():
    state(a=dep("dev", profile="32"), b=dep("worker", status="closed"), c=dep("worker", status="created"))
    got = {i.id: i for i in AkashProvider().instances()}
    assert set(got) == {"a", "c"}
    assert got["a"].state == "running" and got["a"].cpu == 32 and got["a"].mem_gb == 64
    assert got["c"].state == "provisioning" and not got["c"].reachable
    assert all(i.managed for i in got.values())


def test_akash_refuses_to_terminate_what_it_did_not_create():
    state(x={"name": "ddc-dev", "role": "dev"})        # recorded without ours=True
    p = AkashProvider()
    for key in ("1000000000001", "x"):
        with pytest.raises(ProviderError, match="refusing"):
            p.terminate(key)


def test_custom_profile_roundtrip_gpu_sdl_and_preset_reuse(tmp_path):
    p = AkashProvider()
    pre = p.presets()["32"]
    assert p._profile_for(pre) == "32"                             # unchanged preset keeps its config.toml name
    assert p._profile_for(pre.with_(max_usd_hr=0.95)) .startswith("x32-64-100")   # changed cap -> custom profile
    spec = Spec(cpu=8, mem_gb=32, disk_gb=100, gpu=1, gpu_model="RTX 4090", max_usd_hr=0.8, role="box")
    name = p._profile_for(spec)
    assert name == "x8-32-100-g1rtx4090"
    sdl = ddc.render_sdl("box", name)
    assert "gpu:\n          units: 1" in sdl and "- model: rtx4090" in sdl and "units: 8" in sdl and "size: 32Gi" in sdl and "__" not in sdl
    assert "amount: 1333" in sdl                                   # 0.8 $/h price cap
    plain = ddc.render_sdl("worker", "32")
    assert "gpu:" not in plain and "units: 32" in plain and "amount: 1500" in plain
    # custom profiles persist next to the state so the watchdog / ddc CLI know them too
    assert json.loads((ddc.STATE_DIR / "custom_profiles.json").read_text())[name]["gpu"] == 1
    ddc.CFG["profiles"].pop(name)
    ddc.load_custom_profiles()
    assert name in ddc.CFG["profiles"]
    assert name not in p.presets()                                 # but they are not offered as config presets


def test_gpu_without_model_renders_bare_vendor():
    ddc.CFG["profiles"]["g"] = {"cpu": 4, "mem": "8Gi", "disk": "20Gi", "max_usd_hr": 1.0, "gpu": 2}
    s = ddc.render_sdl("box", "g")
    assert "units: 2" in s and "nvidia:\n" in s and "model:" not in s


def test_spend_aggregates_ledger_and_running():
    now = dt.datetime.now(dt.UTC)
    state(a=dep("dev", usd_hr=0.12, provider_host="alpha"), old=dep("worker", status="closed", provider_host="beta"))
    with ddc.LEDGER.open("w") as f:
        f.write(json.dumps({"at": now.isoformat(timespec="seconds"), "event": "closed", "dseq": "old", "cost_usd": 1.5}) + "\n")
        f.write(json.dumps({"at": (now - dt.timedelta(days=3)).isoformat(timespec="seconds"), "event": "closed", "dseq": "old", "cost_usd": 2.0}) + "\n")
    s = AkashProvider().spend()
    assert s.closed_total == pytest.approx(3.5)
    assert s.burn_hr == pytest.approx(0.12)
    assert dict(s.daily)[dt.date.today().isoformat()] >= 1.5
    assert len(s.daily) == 14 and s.by_role["worker"] == pytest.approx(3.5)
    assert s.budget_guard is True                                # the fixture config has the guard on (the safe default)


def test_snooze_holds_only_idle_and_is_stored_in_state():
    p = AkashProvider()
    until = p.hold_idle(30)
    assert ddc.load_state()["idle_hold_until"] == pytest.approx(until) and until > time.time() + 1700
    assert p.hold_idle(0) == 0.0 and ddc.load_state()["idle_hold_until"] == 0.0


def test_watchdog_honors_snooze(monkeypatch):
    """A snoozed idle request must not stop the box; a max-runtime or non-idle request still must."""
    state(dev=dep("dev"))
    stopped = []
    monkeypatch.setattr(ddc, "refresh_live", lambda: None)
    monkeypatch.setattr(ddc, "cmd_stop", lambda a: stopped.append(a))
    monkeypatch.setattr(ddc, "alert", lambda m: None)
    monkeypatch.setattr(ddc, "ssh", lambda *a, **k: json.dumps({"shutdown_requested": "idle", "active_reason": "", "dist_idle_min": 0}))
    ddc.CFG["budget_guard"] = False
    ddc.cmd_watchdog(None)
    assert len(stopped) == 1                                # no snooze: idle request stops the box
    stopped.clear()
    AkashProvider().hold_idle(30)
    ddc.cmd_watchdog(None)
    assert stopped == []                                    # snoozed
    monkeypatch.setattr(ddc, "ssh", lambda *a, **k: json.dumps({"shutdown_requested": "max_runtime"}))
    ddc.cmd_watchdog(None)
    assert len(stopped) == 1                                # snooze never overrides max runtime


def test_write_json_retries_while_a_reader_holds_the_file(monkeypatch):
    calls = {"n": 0}
    real = ddc.os.replace

    def flaky(a, b):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("held by a reader")
        return real(a, b)
    monkeypatch.setattr(ddc.os, "replace", flaky)
    ddc.write_json(ddc.STATE_DIR / "x.json", {"a": 1})
    assert calls["n"] == 3 and json.loads((ddc.STATE_DIR / "x.json").read_text()) == {"a": 1}


def test_final_checkpoint_failure_leaves_the_box_up(monkeypatch):
    monkeypatch.setattr(ddc, "ssh", lambda *a, **k: (_ for _ in ()).throw(ddc.DdcError("boom")))
    monkeypatch.setattr(ddc, "alert", lambda m: None)
    with pytest.raises(ddc.DdcError):
        ddc.final_checkpoint({"host": "h", "port": 1})
    with pytest.raises(ddc.DiscardRefused):                      # SAF-19: --force alone never discards the workspace
        ddc.final_checkpoint({"host": "h", "port": 1}, force=True)
    ddc.final_checkpoint({"host": "h", "port": 1}, force=True, discard_workspace=True)   # --force --discard-workspace still proceeds


def test_close_box_tolerates_spend_rate_lag_but_not_a_persistent_listing(monkeypatch):
    state(x=dep("box"))
    seen = {"n": 0}

    def api(method, path, *a, **k):
        if path.startswith("/v1/spend-rate"):
            seen["n"] += 1
            return {"data": {"deployments": [{"dseq": "x"}] if seen["n"] <= 2 else []}}
        return {}
    monkeypatch.setattr(ddc, "deployment", lambda d: {"name": "ddc-box-x", "deployment": {"state": "closed"}})
    monkeypatch.setattr(ddc, "api", api)
    monkeypatch.setattr(ddc.time, "sleep", lambda s: None)
    ddc.close_box("x")
    assert seen["n"] == 3 and ddc.load_state()["deps"]["x"]["status"] == "closed"
    state(y=dep("box"))
    monkeypatch.setattr(ddc, "api", lambda m, p, *a, **k: {"data": {"deployments": [{"dseq": "y"}]}})
    with pytest.raises(ddc.DdcError, match="still listed"):
        ddc.close_box("y")


# ------------------------------------------------------------------ the spend guard: Spec bounds are sanity checks, the configured limits always apply
from clouddeck.providers.base import judge  # noqa: E402

LIMITS = {"budget_guard": True, "budget_usd": 25.0, "reserve_usd": 5.0, "max_total_leases": 4, "max_workers": 3}


def test_judge_agrees_with_the_controllers_can_afford():
    """judge() duplicates the controller's budget arithmetic (providers cannot import it); this keeps the two from drifting apart."""
    import random
    rng = random.Random(1)
    for _ in range(300):
        spec = Spec(cpu=4, mem_gb=8, disk_gb=20, role="box", max_usd_hr=rng.uniform(0.01, 3.0), hours=rng.randint(1, 24))
        n, spent, committed = rng.randint(1, 4), rng.uniform(0, 30), rng.uniform(0, 20)
        balance, budget, reserve = rng.uniform(0, 80), rng.uniform(5, 60), rng.uniform(0, 20)
        view = {"worst": spent + committed, "committed": committed}
        ok, _ = ddc.can_afford(view, balance, spec.max_usd_hr * n, spec.hours, budget, reserve)
        v = judge(spec, n, {"budget_guard": True, "budget_usd": budget, "reserve_usd": reserve}, worst=spent + committed, committed=committed, balance=balance)
        assert v.ok == ok and v.guard_on is True and v.ceiling_usd == pytest.approx(spec.max_usd_hr * spec.hours * n)


def test_judge_enforces_every_limit_and_fails_closed():
    spec = Spec(cpu=4, mem_gb=8, disk_gb=20, role="worker", max_usd_hr=0.5, hours=4)
    base = dict(worst=2.0, committed=1.0, balance=40.0)
    assert judge(spec, 1, LIMITS, leases=1, workers=0, **base).ok
    assert "max_workers is 3" in judge(spec, 1, LIMITS, leases=3, workers=3, **base).text
    assert "max_workers" not in judge(spec.with_(role="box"), 1, LIMITS, leases=3, workers=3, **base).text            # only workers count against max_workers
    assert "max_total_leases is 4" in judge(spec, 2, LIMITS, leases=3, workers=0, **base).text
    assert "above max_usd_hr_hard" in judge(spec, 1, {**LIMITS, "max_usd_hr_hard": 0.4}, **base).text
    assert "would exceed budget" in judge(spec, 1, {**LIMITS, "budget_usd": 3.0}, **base).text
    assert "would breach reserve" in judge(spec, 1, LIMITS, worst=2.0, committed=1.0, balance=7.5).text
    assert "balance unknown" in judge(spec, 1, LIMITS, worst=0, committed=0, balance=None).text
    assert "count must be" in judge(spec, 0, LIMITS, **base).text
    assert "CPU must be" in judge(spec.with_(cpu=0), 1, LIMITS, **base).text                                      # Spec bounds still apply
    for key, bad in (("budget_usd", float("nan")), ("budget_usd", float("inf")), ("reserve_usd", "lots"), ("max_workers", -1), ("max_total_leases", None)):
        v = judge(spec, 1, {**LIMITS, key: bad}, leases=0, workers=0, **base)
        assert not v.ok and "not a valid number" in v.text, (key, bad)                                             # NaN / inf must not switch a check off


def test_only_a_literal_false_switches_the_budget_guard_off():
    spec = Spec(role="box", max_usd_hr=0.5, hours=4)
    for val in (True, "false", "no", 0, None):
        v = judge(spec, 1, {**LIMITS, "budget_guard": val}, worst=100, committed=100, balance=None)
        assert v.guard_on is True and not v.ok
    v = judge(spec, 1, {**LIMITS, "budget_guard": False}, worst=100, committed=100, balance=None)
    assert v.ok and v.guard_on is False and "OFF" in v.text and v.ceiling_usd == pytest.approx(2.0)
    assert not judge(spec, 9, {**LIMITS, "budget_guard": False}, leases=0).ok                                       # count limits still hold with the guard off


def with_guard(monkeypatch, balance=40.0, **limits):
    ddc.CFG.update({"budget_guard": True, **LIMITS, "max_runtime_hours": 8, "worker_runtime_hours": 4, **limits})
    p = AkashProvider()
    monkeypatch.setattr(p, "balance", lambda: balance)
    return p


def test_akash_guard_counts_leases_workers_and_the_dev_box_limit(monkeypatch):
    state(d=dep("dev", usd_hr=0.2, limit_h=8), w1=dep("worker", usd_hr=0.2), w2=dep("worker", usd_hr=0.2, idx=2), old=dep("worker", status="closed"))
    p = with_guard(monkeypatch)
    worker = Spec(cpu=8, mem_gb=16, disk_gb=50, role="worker", max_usd_hr=0.5, hours=4)
    v = p.guard(worker)
    assert v.ok and v.guard_on is True and v.ceiling_usd == pytest.approx(2.0) and "ok" in v.text                    # 3 live leases, 2 workers: one more fits
    assert not p.guard(worker, 2).ok and "max_total_leases is 4" in p.guard(worker, 2).text
    with_guard(monkeypatch, max_workers=2)
    assert "max_workers is 2" in p.guard(worker).text
    dev_spec = Spec(cpu=8, mem_gb=16, disk_gb=50, role="dev", max_usd_hr=0.5, hours=1)
    assert p.guard(dev_spec.with_(role="box")).ceiling_usd == pytest.approx(0.5)
    ddc.CFG["max_total_leases"] = 9
    assert p.guard(dev_spec).ceiling_usd == pytest.approx(4.0)                                                       # the dev box runs for max_runtime_hours, not spec.hours
    # replacing a worker frees its slot and its still-committed spend
    ddc.CFG["max_workers"] = 2
    inst = next(i for i in p.instances() if i.id == "w1")
    assert p.guard(worker, 1, replacing=inst).ok and not p.guard(worker, 1).ok


def test_akash_guard_refuses_when_the_balance_or_the_state_cannot_be_read(monkeypatch):
    p = with_guard(monkeypatch, balance=None)
    spec = Spec(cpu=8, mem_gb=16, disk_gb=50, role="box", max_usd_hr=0.5, hours=4)
    assert not p.guard(spec).ok and "balance unknown" in p.guard(spec).text
    p = with_guard(monkeypatch)
    monkeypatch.setattr(ddc, "budget_view", lambda *a, **k: (_ for _ in ()).throw(ddc.DdcError("ledger is damaged")))
    with pytest.raises(ProviderError, match="ledger is damaged"):
        p.guard(spec)
    with pytest.raises(ProviderError, match="ledger is damaged"):
        p.require(spec)


def test_akash_deploy_and_scale_stop_at_the_limits_before_touching_the_controller(monkeypatch):
    """A spec that passes Spec.validate() is still refused by the configured limits, whichever entry point is used."""
    state(d=dep("dev"), w1=dep("worker"), w2=dep("worker", idx=2), w3=dep("worker", idx=3))
    p = with_guard(monkeypatch, max_total_leases=4)
    for name in ("cmd_start", "create_box", "cmd_scale", "check_budget", "save_custom_profile"):
        monkeypatch.setattr(ddc, name, lambda *a, **k: (_ for _ in ()).throw(AssertionError("the controller must not be reached")))
    worker = Spec(cpu=8, mem_gb=16, disk_gb=50, role="worker", max_usd_hr=0.5, hours=4)
    assert worker.validate() == []
    with pytest.raises(ProviderError, match="max_"):
        p.deploy(worker)
    with pytest.raises(ProviderError, match="max_"):
        p.scale(4, worker)
    with pytest.raises(ProviderError, match="max_"):
        p.deploy(worker.with_(role="box"))
    state()
    ddc.CFG["max_usd_hr_hard"] = 0.4
    with pytest.raises(ProviderError, match="max_usd_hr_hard"):
        p.deploy(worker.with_(role="box"))
    ddc.CFG.pop("max_usd_hr_hard")
    p2 = with_guard(monkeypatch, balance=1.0)
    with pytest.raises(ProviderError, match="would exceed budget"):
        p2.deploy(worker.with_(role="box", max_usd_hr=2.0, hours=24))                                              # $48 ceiling against a $25 budget: valid spec, refused
    with pytest.raises(ProviderError, match="hard ceiling"):
        p2.deploy(worker.with_(role="box", max_usd_hr=0.5, hours=48))                                              # Spec allows 48 h (the platform maximum); the controller's hard ceiling is 24 h
    with pytest.raises(ProviderError, match="max_usd_hr_hard"):
        p2.deploy(worker.with_(role="box", max_usd_hr=50, hours=4))                                                # Spec allows $100/h; the configured ceiling (default $2/h) is lower
    ddc.CFG["max_usd_hr_hard"] = 9.0
    ddc.CFG["max_workers"] = 99                                                                                    # a config asking for more than the hard ceiling gets the ceiling
    assert p2.limits()["max_usd_hr_hard"] == ddc.HARD_MAX_USD_HR and p2.limits()["max_workers"] == ddc.HARD_MAX_WORKERS


def test_scale_down_and_shrinking_never_need_the_guard(monkeypatch):
    state(d=dep("dev"), w1=dep("worker"), w2=dep("worker", idx=2))
    p = with_guard(monkeypatch, balance=None)                                                                      # guard cannot even be evaluated
    called = []
    monkeypatch.setattr(ddc, "cmd_scale", lambda a: called.append(a.n))
    p.scale(1, Spec(cpu=8, mem_gb=16, disk_gb=50, role="worker", max_usd_hr=0.5))
    assert called == [1]


def test_a_refused_resize_leaves_the_old_instance_alone():
    d = providers.get("demo")
    worker = next(i for i in d.instances() if i.role == "worker")
    d.limits["max_usd_hr_hard"] = 0.1
    with pytest.raises(ProviderError, match="max_usd_hr_hard"):
        d.resize(worker, Spec(cpu=16, mem_gb=32, disk_gb=100, max_usd_hr=0.5, hours=4))
    assert worker.id in {i.id for i in d.instances()} and not [c for c in d.calls if c[0] in ("terminate", "deploy")]
    d.limits.pop("max_usd_hr_hard")
    d.resize(worker, Spec(cpu=16, mem_gb=32, disk_gb=100, max_usd_hr=0.5, hours=4))
    assert [c[0] for c in d.calls if c[0] in ("terminate", "deploy")] == ["terminate", "deploy"]


def test_generic_scale_up_goes_through_the_guard_of_a_third_party_provider():
    class Capped(Provider):
        id, label = "capped", "Capped"

        def __init__(self): self.items, self.asked = [], []
        def offers(self): return []
        def quote(self, spec): raise Unsupported("x")
        def instances(self, refresh=False): return list(self.items)
        def deploy(self, spec, log=print):
            self.items.append(Instance(id=str(len(self.items)), name="w", role="worker", state="running", provider="capped", managed=True))
            return self.items[-1]
        def terminate(self, instance_id, log=print): return 0.0
        def guard(self, spec, n=1, replacing=None):
            self.asked.append(n)
            return super().guard(spec, n, replacing) if n <= 2 else providers.base.Verdict(False, "too many", 0.0)

    c = Capped()
    c.scale(2, Spec(max_usd_hr=0.1))
    with pytest.raises(ProviderError, match="too many"):
        c.scale(5, Spec(max_usd_hr=0.1))
    assert len(c.items) == 2 and c.asked == [2, 3]
    assert "no budget guard" in c.guard(Spec(max_usd_hr=0.1)).text and not c.guard(Spec(cpu=0, max_usd_hr=0.1)).ok


def test_terminating_the_dev_box_verifies_the_checkpoint_before_closing_anything(monkeypatch):
    state(d=dep("dev"), w1=dep("worker"))
    closed, order = [], []
    monkeypatch.setattr(ddc, "refresh_live", lambda: None)
    monkeypatch.setattr(ddc, "close_box", lambda dseq: (closed.append(dseq), 0.5)[1])
    monkeypatch.setattr(ddc, "sync_worker_hosts", lambda: None)

    def failing(d, force=False, **kw):
        order.append("checkpoint")
        raise ddc.DdcError("checkpoint did not verify")
    monkeypatch.setattr(ddc, "final_checkpoint", failing)
    p = AkashProvider()
    with pytest.raises(ProviderError, match="did not verify"):
        p.terminate("d")
    assert closed == [] and order == ["checkpoint"]                                      # the workers are still up too: nothing was closed
    monkeypatch.setattr(ddc, "final_checkpoint", lambda d, force=False, **kw: order.append(("checkpoint", kw)))
    p.terminate("d")
    assert closed == ["w1", "d"] and order == ["checkpoint", ("checkpoint", {"discard_workspace": False})]
    closed.clear()
    state(d=dep("dev"))
    p.terminate("d", discard_workspace=True)
    assert order[-1] == ("checkpoint", {"discard_workspace": True})


def test_scale_victims_follow_each_providers_closing_rule(monkeypatch):
    state(d=dep("dev"), w1=dep("worker", idx=1, lease_at=time.time() - 100), w2=dep("worker", idx=2, lease_at=time.time() - 9000),
          w3=dep("worker", idx=3, lease_at=time.time() - 50))
    got = AkashProvider().scale_victims(1)
    assert [i.id for i in got] == ["w2", "w3"]                                           # ddc closes the highest-numbered workers
    d = providers.get("demo")
    newest = sorted((i for i in d.instances() if i.role == "worker"), key=lambda i: i.started_at)[1:]
    assert d.scale_victims(1) == newest and d.scale_victims(3) == []


def test_checkpoint_enabled_follows_state_repo():
    p = AkashProvider()
    assert p.checkpoint_enabled() is True
    ddc.CFG["state_repo"] = ""
    assert p.checkpoint_enabled() is False
    assert providers.get("demo").checkpoint_enabled() is True


@pytest.fixture
def damaged_state():
    """A truncated state.json, as a crash mid-write would leave it; the controller moves it aside and blocks new leases."""
    ddc.STATE_FILE.write_text('{"deps": {"x": ', encoding="utf-8")
    yield
    for p in ddc.STATE_DIR.glob("state.*"):
        if p.name != "state.json" or p.exists():
            p.unlink(missing_ok=True)


def test_a_damaged_state_file_is_a_clear_refusal_everywhere_not_an_empty_account(damaged_state):
    p = AkashProvider()
    spec = Spec(cpu=8, mem_gb=16, disk_gb=50, role="box", max_usd_hr=0.5, hours=4)
    for call in (p.instances, lambda: p.guard(spec), lambda: p.deploy(spec), lambda: p.scale(1, spec), lambda: p.require(spec)):
        with pytest.raises((ProviderError, ddc.DdcError), match="will NOT create leases"):
            call()
    assert (ddc.STATE_DIR / "state.corrupt").exists() and not ddc.STATE_FILE.exists()          # moved aside, flagged, never replaced by an empty state

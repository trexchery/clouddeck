"""`ddc job` spending safety: a job lease is created ONLY through the controller's single creation path (ddc.launch_lease -> check_spend), so it costs
against the same budget, lease caps and hard ceilings as every other lease; the write-ahead record exists before the POST; an unknown POST outcome is
looked up, never repeated.  Virtual-time fake Akash (tests/job_fakes.py), isolated state."""
import re
from pathlib import Path

import pytest
from job_helpers import make_spec, run_job

from clouddeck.engine import ddc
from clouddeck.job.market import History, Market
from clouddeck.job.runner import JobError, JobRunner
from clouddeck.job.spec import render_sdl
from clouddeck.job.telemetry import Telemetry

JOB = dict(label="job t1 lease", n=1, hours=2, cap_usd_hr=3.0, role="job")


def spend(**over):
    a = {**JOB, **over}
    ddc.check_spend(a["label"], a["n"], a["hours"], a["cap_usd_hr"], a["role"])


def rec(role="job", status="ready", usd_hr=0.3, name=None, **kw):
    return {"ours": True, "name": name or f"ddc-{role}-x-0101", "role": role, "status": status, "usd_hr": usd_hr, "lease_at": ddc.now(), "limit_h": 8,
            "max_usd_hr": 0.9, "provider": "p", "host": "h.example", "port": 22, "idx": 1, **kw}


def state(**deps):
    ddc.write_json(ddc.STATE_FILE, {"deps": deps})


def job_sdl(s):
    """The real SDL of a spec: launch_lease reads the price back out of it (defence in depth), so a stand-in text no longer passes."""
    return render_sdl(s, "i:1", "ssh-ed25519 AAAATESTPUBLIC test")


def market(jw, **over):
    s = make_spec(jw.tmp, **over)
    return Market(s, jw.clock, Telemetry("t1", jw.clock), History(ddc.STATE_DIR / "provider_history.json", jw.clock), lambda m: None), s


# ------------------------------------------------------------------ static: no side door
def test_the_job_package_never_creates_or_deletes_deployments_itself():
    src = "\n".join(p.read_text(encoding="utf-8") for p in sorted((Path(ddc.PKG) / "job").glob("*.py")))
    assert not re.search(r"""api\(\s*["'](POST|DELETE|PUT|PATCH)["']\s*,\s*[f]?["']/v1/deployments""", src)       # creation = ddc.launch_lease, closing = ddc.close_box
    assert "launch_lease" in src and "close_box" in src and src.count('api("POST"') == 1                           # the only POST left is the lease call (manifest delivery)
    assert not re.search(r"\bdeployment_status\(|\bupdate_state\(", src)


def test_a_job_launch_calls_check_spend_with_role_job_and_its_own_cap(jw, monkeypatch):
    seen = []
    real = ddc.check_spend
    monkeypatch.setattr(ddc, "check_spend", lambda *a, **k: (seen.append((a, k)), real(*a, **k))[1])
    m, _ = market(jw)
    m.create(job_sdl(m.s), "i:1")
    (label, n, hours, cap, role), kw = seen[0]
    assert (n, hours, cap, role) == (1, 2, 3.0, "job") and "t1" in label and kw.get("profile") is None
    assert len(seen) == 1 and len(jw.akash.posts) == 1


def test_create_box_goes_through_the_same_helper(fake, monkeypatch):
    called = []
    real = ddc.launch_lease
    monkeypatch.setattr(ddc, "launch_lease", lambda *a, **k: (called.append(k["role"]), real(*a, **k))[1])
    ddc.create_box("worker", "32", 1, limit_h=4)
    assert called == ["worker"]


# ------------------------------------------------------------------ refusals: budget, lease caps, price ceiling, runtime ceiling
def test_budget_refuses_a_job_before_anything_is_posted(jw):
    ddc.CFG["budget_usd"] = 5.0                                      # 2 h x $3 = $6 > $5
    m, _ = market(jw, max_cost_usd=100)
    with pytest.raises(ddc.LimitExceeded, match="budget"):
        m.create(job_sdl(m.s), "i:1")
    assert not jw.akash.posts and ddc.load_state().get("pending") in (None, {})


def test_reserve_refuses_a_job_when_the_balance_is_too_low(jw):
    jw.akash.balance = 8.0                                            # 8 - 6 < reserve 5
    with pytest.raises(ddc.LimitExceeded, match="reserve"):
        spend()


def test_budget_guard_is_on_by_default_for_jobs_and_counts_running_jobs_at_their_cap(jw):
    assert ddc.CFG["budget_guard"] is True
    state(a=rec("job", max_usd_hr=3.0, usd_hr=0, limit_h=24, lease_at=jw.clock.time()))     # a running job: worst case = $3 x 24 h = $72 committed
    v = ddc.budget_view()
    assert v["committed"] == pytest.approx(72.0) and v["worst"] >= 72.0
    spend()                                                           # $72 committed + $6 new = $78 <= $85 budget, balance 100 - 78 >= reserve
    ddc.CFG["budget_usd"] = 75.0
    with pytest.raises(ddc.LimitExceeded, match="budget"):
        spend()                                                       # $78 > $75: the running job's whole worst case counts


def test_pending_job_creation_counts_toward_the_budget_and_the_lease_cap(jw):
    ddc.CFG["max_total_leases"] = 1
    ddc.write_json(ddc.STATE_FILE, {"deps": {}, "pending": {"ddc-job-x-0101-ab12": {"role": "job", "max_usd_hr": 3.0, "limit_h": 2, "created_at": jw.clock.time()}}})
    with pytest.raises(ddc.LimitExceeded, match="max_total_leases = 1"):
        spend()
    assert ddc.budget_view()["committed"] == pytest.approx(6.0, abs=0.05)       # at its price cap until it is resolved


def test_job_leases_count_toward_max_total_leases_but_not_toward_max_workers(jw):
    ddc.CFG.update(max_total_leases=2, max_workers=1)
    state(a=rec("job"), b=rec("worker"))
    with pytest.raises(ddc.LimitExceeded, match="max_total_leases = 2"):
        spend()
    ddc.CFG["max_total_leases"] = 3
    spend()                                                            # a job is not a worker: max_workers = 1 (already used) does not block it
    with pytest.raises(ddc.LimitExceeded, match="max_workers = 1"):
        ddc.check_spend("w", 1, 4, 0.3, "worker")


def test_hard_lease_ceiling_holds_for_jobs_with_the_guard_off(jw):
    ddc.CFG.update(max_total_leases=8, budget_guard=False)
    state(**{str(i): rec("job", name=f"ddc-job-x-{i}") for i in range(ddc.HARD_MAX_LEASES)})
    with pytest.raises(ddc.LimitExceeded, match="max_total_leases = 8"):
        spend()


@pytest.mark.parametrize("guard", [True, False])
def test_price_ceiling_per_lease_refuses_a_job_with_the_guard_on_or_off(jw, guard):
    ddc.CFG.update(budget_guard=guard, max_price_usd_hr=2.0)
    with pytest.raises(ddc.LimitExceeded, match=r"price cap of \$2.5/h .*ceiling \$2/h"):
        spend(cap_usd_hr=2.5)
    spend(cap_usd_hr=2.0)                                              # at the ceiling: fine
    ddc.CFG["max_price_usd_hr"] = 3.0                                  # `max_price_usd_hr` raises it on purpose
    spend(cap_usd_hr=2.5)


@pytest.mark.parametrize("guard", [True, False])
def test_runtime_ceiling_refuses_a_job_with_the_guard_on_or_off(jw, guard):
    ddc.CFG.update(budget_guard=guard, budget_usd=10_000.0)
    with pytest.raises(ddc.LimitExceeded, match="hard ceiling 24 h"):
        spend(hours=25, cap_usd_hr=1.0)
    spend(hours=24, cap_usd_hr=1.0)


def test_unsafe_disable_limits_semantics_are_unchanged_for_jobs(jw, monkeypatch):
    ddc.CFG.update(unsafe_disable_limits=True, max_price_usd_hr=2.0, budget_usd=10_000.0)
    jw.akash.balance = 1e6
    spend(cap_usd_hr=50.0, hours=48)                                 # ceilings off (and loudly warned) ...
    ddc.CFG["budget_usd"] = 5.0
    with pytest.raises(ddc.LimitExceeded, match="budget"):
        spend(cap_usd_hr=50.0, hours=48)                               # ... the budget guard is a separate switch and still applies
    ddc.CFG["budget_guard"] = False
    spend(cap_usd_hr=50.0, hours=48)


def test_check_spend_refuses_nonsense_orders_and_a_damaged_state_or_ledger(jw):
    for bad in ({"cap_usd_hr": 0}, {"cap_usd_hr": -1}, {"cap_usd_hr": float("nan")}, {"cap_usd_hr": True}, {"hours": 0}, {"n": 0}, {"cap_usd_hr": 1e-9}):
        with pytest.raises(ddc.DdcError):
            spend(**bad)
    ddc.LEDGER.write_text('{"event": "closed", "cost_usd": 1}\nnot json\n{"event": "x"}\n')
    with pytest.raises(ddc.LimitExceeded, match="unreadable lines"):
        spend()
    ddc.LEDGER.unlink()
    (ddc.STATE_DIR / "state.corrupt").write_text("{}")
    with pytest.raises(ddc.StateCorrupt):
        spend()


def test_a_broken_unrelated_profile_does_not_block_a_job_but_a_broken_global_key_does(jw):
    ddc.CFG["profiles"]["broken"] = {"cpu": 0}
    spend()
    ddc.CFG["budget_guard"] = "false"
    with pytest.raises(ddc.DdcError, match="budget_guard must be true or false"):
        spend()


def test_check_budget_is_a_thin_wrapper_with_identical_messages(fake, monkeypatch):
    lines = []
    monkeypatch.setattr(ddc, "log", lines.append)
    ddc.check_budget("32", 1, 8, role="dev")
    assert re.fullmatch(r"budget check for 1x32 vCPU x 8 h at cap \$0\.9/h: ok: worst-case \$7\.20 of \$70\.00, balance after \$92\.80", lines[-1]), lines
    ddc.CFG["budget_guard"] = False
    ddc.check_budget("32", 2, 4)
    assert lines[-1] == "budget guard OFF (budget_guard = false): launching 2x32 vCPU, worst case $7.20 at the price cap"


def test_launch_lease_refuses_foreign_names_and_a_missing_runtime_limit(fake):
    with pytest.raises(ddc.DdcError, match="named ddc-"):
        ddc.launch_lease("other-service", "sdl", role="job", limit_h=2, cap_usd_hr=1.0, label="x")
    for bad in (0, None, 1.5, True):
        with pytest.raises(ddc.DdcError, match="runtime limit"):
            ddc.launch_lease("ddc-job-x-0101-ab12", "sdl", role="job", limit_h=bad, cap_usd_hr=1.0, label="x")
    with pytest.raises(ddc.DdcError, match="cannot override"):
        ddc.launch_lease("ddc-job-x-0101-ab12", "sdl", role="job", limit_h=2, cap_usd_hr=1.0, label="x", fields={"role": "dev"})
    assert not fake.deployments


# ------------------------------------------------------------------ the spec's caps never exceed the config; runtimeLimitHours is always sent
def test_a_job_with_a_huge_runtime_and_price_is_clamped_to_the_hard_ceilings_before_the_post(jw):
    ddc.CFG.update(max_price_usd_hr=2.5, budget_usd=500.0, reserve_usd=1.0)
    jw.akash.balance = 1000.0
    s = make_spec(jw.tmp, runtime_hours=100, max_cost_usd=10_000, hardware={"cpu": 4, "memory": "16Gi", "disk": "50Gi", "max_usd_hr": 9.0, "gpu": {"vendor": "vendor-a", "models": ["model-a"]}})
    jw.durations = {"cache-m": 5, "work": 5}
    run_job(jw, s)
    assert jw.akash.posts[0]["runtimeLimitHours"] == ddc.HARD_MAX_RUNTIME_H
    assert f"amount: {ddc.uact_block_from_usd_hr(2.5)}\n" in jw.akash.posts[0]["sdl"]


def test_a_job_that_the_controller_refuses_creates_nothing_through_the_runner_either(jw):
    ddc.CFG["budget_usd"] = 1.0
    ddc.CFG["budget_guard"] = True
    s = make_spec(jw.tmp, max_cost_usd=100)
    r = JobRunner(s, clock=jw.clock, remote_factory=jw.factory, install_signals=False, log=lambda m: None, offline=True)
    with pytest.raises((ddc.LimitExceeded, JobError)):
        r.run()
    assert not jw.akash.posts and r.dseq is None


# ------------------------------------------------------------------ write-ahead record, single POST, promotion
def test_pending_record_exists_before_the_post_and_is_promoted_after(jw, monkeypatch):
    seen = {}
    real = jw.akash.api

    def spy(method, path, body=None, **kw):
        if (method, path) == ("POST", "/v1/deployments"):
            st = ddc.load_state()
            seen["pending"] = dict(st.get("pending") or {})
            seen["deps"] = dict(st["deps"])
        return real(method, path, body, **kw)

    monkeypatch.setattr(ddc, "api", spy)
    m, _ = market(jw)
    dseq = m.create(job_sdl(m.s), "i:1")
    (name, p), = seen["pending"].items()
    assert name == m.name and name.startswith("ddc-job-t1-") and seen["deps"] == {}
    assert p["role"] == "job" and p["limit_h"] == 2 and p["max_usd_hr"] == 3.0 and p["job"] == "t1"           # what budget_view / reconcile need, written before the POST
    st = ddc.load_state()
    assert st.get("pending") == {} and st["deps"][dseq]["ours"] is True and st["deps"][dseq]["name"] == name and st["deps"][dseq]["role"] == "job"
    assert st["deps"][dseq]["limit_h"] == 2 and st["deps"][dseq]["max_usd_hr"] == 3.0 and st["deps"][dseq]["heartbeat_at"] > 0
    assert ddc.ledger_read()[0][0]["event"] == "created" and len(jw.akash.posts) == 1


def test_an_uncertain_post_is_not_retried_the_deployment_is_found_by_name_and_closed(jw):
    jw.akash.fail_next("POST /v1/deployments", "timeout_after")         # executed by the platform, but the reply is lost
    m, _ = market(jw)
    with pytest.raises(ddc.DdcError, match="found and closed again"):
        m.create(job_sdl(m.s), "i:1")
    assert len(jw.akash.posts) == 1                                      # never posted twice
    (dseq, d), = jw.akash.deps.items()
    assert d["state"] == "closed" and d["name"] == m.name
    st = ddc.load_state()
    assert st.get("pending") == {} and st["deps"][dseq]["status"] == "closed" and st["deps"][dseq]["role"] == "job"


def test_a_post_that_may_not_have_run_stays_pending_counted_and_is_reconciled_later(jw):
    jw.akash.fail_next("POST /v1/deployments", "timeout_before")        # not executed: nothing on the account
    m, _ = market(jw)
    with pytest.raises(ddc.OutcomeUnknown):
        m.create(job_sdl(m.s), "i:1")
    assert len(jw.akash.posts) == 0 and [c for c in jw.akash.calls if c[0] == "POST"] == [("POST", "/v1/deployments")]       # one attempt, no retry
    pend = ddc.pending_creations()
    assert list(pend) == [m.name] and ddc.budget_view()["committed"] == pytest.approx(6.0, abs=0.05)                          # still counted at its cap (minus the seconds the lookup took)
    assert ddc.reconcile_pending() == []                                  # too young to drop
    jw.clock.sleep(ddc.PENDING_GRACE_S + 1)
    assert "dropped" in ddc.reconcile_pending()[0] and ddc.pending_creations() == {}


def test_a_rejected_post_leaves_no_pending_record(jw):
    jw.akash.fail_next("POST /v1/deployments", "reject")
    m, _ = market(jw)
    with pytest.raises(ddc.DdcError, match="HTTP 400"):
        m.create(job_sdl(m.s), "i:1")
    assert ddc.pending_creations() == {} and not jw.akash.posts


def test_the_runner_does_not_retry_an_unknown_creation_outcome(jw):
    jw.akash.fail_next("POST /v1/deployments", "timeout_before")
    with pytest.raises(ddc.OutcomeUnknown):
        run_job(jw, make_spec(jw.tmp))
    assert [c for c in jw.akash.calls if c == ("POST", "/v1/deployments")] == [("POST", "/v1/deployments")]
    assert jw.runner.dseq is None and not jw.akash.deps                   # nothing to close, and nothing guessed


# ------------------------------------------------------------------ the lease POST handles ApiUncertain like create_box
def test_an_uncertain_lease_post_that_did_not_happen_is_not_blindly_repeated(jw):
    jw.akash.fail_next("POST /v1/leases", "timeout_before")
    with pytest.raises(ddc.ApiUncertain):
        run_job(jw, make_spec(jw.tmp, step=[{"name": "work", "run": "x"}], inputs=[], cache={}))
    assert [c for c in jw.akash.calls if c == ("POST", "/v1/leases")] == [("POST", "/v1/leases")]
    assert all(d["state"] == "closed" for d in jw.akash.deps.values())    # the runner closed the deployment it created


def test_an_uncertain_lease_post_that_did_happen_is_recognised(jw):
    jw.akash.port_delay_s = 0                                              # the lease shows up in the deployment details at once
    jw.akash.fail_next("POST /v1/leases", "timeout_after")
    jw.durations = {"cache-m": 5, "work": 5}
    run_job(jw, make_spec(jw.tmp))
    assert [c for c in jw.akash.calls if c == ("POST", "/v1/leases")] == [("POST", "/v1/leases")]      # looked at the deployment instead of posting again
    assert jw.akash.deps[jw.runner.dseq]["state"] == "closed" and (jw.tmp / "got" / "out" / "result.bin").exists()

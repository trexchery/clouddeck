"""Orphaned `ddc job` leases: the runner records a heartbeat, the watchdog closes (through close_box) an owned role-"job" lease whose runner has been silent for
job_orphan_minutes or that is past its own deadline, and never anything else.  Also: status / stop / reconcile / refresh_live / the provider (TUI) handle
role "job" without crashing.  In-memory fake Akash (tests/fake_akash.py), isolated state."""
import types

import pytest

from clouddeck import providers
from clouddeck.engine import ddc
from clouddeck.providers.base import ProviderError

N = [0]


def put(fake, role="job", name=None, hb_min=0.0, ours=True, deadline_in_min=None, status="ready", **kw):
    """An active deployment on the fake plus its state record (a job lease whose runner last spoke `hb_min` minutes ago)."""
    N[0] += 1
    dseq = str(2000000000000 + N[0])
    name = name or f"ddc-{role}-t1-0101-{N[0]:04x}"
    fake.add_foreign(dseq, name=name)
    now = fake.clock
    r = {"ours": ours, "name": name, "role": role, "status": status, "usd_hr": 0.5, "lease_at": now - 3600, "created_at": now - 3700, "limit_h": 4, "max_usd_hr": 1.0,
         "host": "h.example", "port": 22, "heartbeat_at": now - hb_min * 60, "job": "t1", "provider": "akash1p", **kw}
    if deadline_in_min is not None:
        r["deadline_at"] = now + deadline_in_min * 60
    ddc.update_state(lambda st: st["deps"].__setitem__(dseq, r))
    return dseq


def active(fake):
    return set(fake.open_dseqs())


def alerts():
    return " | ".join(ddc.recent_alerts(50))


def watchdog():
    return ddc.cmd_watchdog(None)


# ------------------------------------------------------------------ the reaper
def test_only_a_stale_owned_job_lease_is_closed_everything_else_is_left_alone(fake):
    stale = put(fake, hb_min=45)
    fresh = put(fake, hb_min=5)
    other_role = put(fake, role="box", hb_min=300)                                       # not a job lease: the reaper never looks at it
    foreign_name = put(fake, name="other-service+db", hb_min=300)                         # role job + ours, but not named ddc-*
    not_ours = put(fake, ours=False, hb_min=300)                                          # a ddc-job name without our ownership flag
    adopted = put(fake, hb_min=300, adopted=True)                                         # rebuilt from the account: only the user closes it
    foreign_dep = fake.add_foreign("1000000000099")                                       # on the account, in no state at all
    assert watchdog() == 0
    assert active(fake) == {fresh, other_role, foreign_name, not_ours, adopted, foreign_dep}
    assert ddc.load_state()["deps"][stale]["status"] == "closed"
    assert "closing job lease" in alerts() and stale in alerts() and "silent for 45 min" in alerts()
    assert [c for c in fake.calls if c[0] == "DELETE"] == [("DELETE", f"/v1/deployments/{stale}")]       # one DELETE, for the stale one only
    assert ddc.read_json(ddc.STATE_DIR / "watchdog.json", {})["last_result"] == "no dev box"


def test_a_job_lease_past_its_own_deadline_is_closed_even_with_a_fresh_heartbeat(fake):
    put(fake, hb_min=0, deadline_in_min=-3)
    ok = put(fake, hb_min=0, deadline_in_min=30)
    watchdog()
    assert active(fake) == {ok} and "past its own deadline" in alerts()


def test_a_job_that_leases_late_is_not_closed_as_never_ready_while_its_runner_is_alive(fake):
    """`bid early, lease late`: the deployment may sit in status "created" for a long time while local prep runs. The never-became-ready rule
    (30 min) must not close it; the heartbeat rule decides, so a live runner keeps it and a dead one loses it."""
    waiting = put(fake, hb_min=1, status="created", created_at=fake.clock - 2 * 3600)
    dead = put(fake, hb_min=40, status="created", created_at=fake.clock - 2 * 3600)
    watchdog()
    assert active(fake) == {waiting} and ddc.load_state()["deps"][dead]["status"] == "closed"


def test_a_dead_runner_is_noticed_even_when_it_is_the_only_lease(fake):
    put(fake, hb_min=31)
    assert watchdog() == 0 and not active(fake)
    assert ddc.read_json(ddc.STATE_DIR / "watchdog.json", {})["last_result"].startswith("closed 1 orphaned job lease")


def test_job_orphan_minutes_is_configurable_defaults_to_30_and_is_validated(fake):
    a = put(fake, hb_min=15)
    ddc.CFG["job_orphan_minutes"] = 12
    watchdog()
    assert a not in active(fake)
    b = put(fake, hb_min=29)
    ddc.CFG.pop("job_orphan_minutes")
    watchdog()
    assert b in active(fake)                                                              # default: 30
    for bad in (0, -1, 5, 9.9, "10", True, float("nan")):                                  # below the 10 minute floor is invalid too
        assert any("job_orphan_minutes" in p for p in ddc.config_problems({**ddc.CFG, "job_orphan_minutes": bad}))
    assert not any("job_orphan_minutes" in p for p in ddc.config_problems({**ddc.CFG, "job_orphan_minutes": 45}))
    assert not any("job_orphan_minutes" in p for p in ddc.config_problems({**ddc.CFG, "job_orphan_minutes": 10}))
    ddc.CFG["job_orphan_minutes"] = 3                                                     # a value under the floor that got past the config check still cannot shorten the rule
    assert not ddc.job_reap_reason({"heartbeat_at": fake.clock - 20 * 60})
    ddc.CFG["job_orphan_minutes"] = "garbage"                                             # a malformed value never switches the rule off: the default applies
    assert ddc.job_reap_reason({"heartbeat_at": fake.clock - 31 * 60}) and not ddc.job_reap_reason({"heartbeat_at": fake.clock - 29 * 60})


def test_a_record_without_any_timestamp_counts_as_silent_but_only_for_owned_job_leases(fake):
    d = put(fake, heartbeat_at=None, created_at=None)
    ddc.update_dep(d, heartbeat_at=0, created_at=0)
    assert ddc.job_reap_reason(ddc.load_state()["deps"][d])
    watchdog()
    assert d not in active(fake)


def test_a_failed_close_is_alerted_retried_and_never_swallowed(fake):
    stale = put(fake, hb_min=60)
    fake.fail_next("DELETE /v1/deployments", "error")
    assert watchdog() == 0                                                                # the pass itself survives
    assert stale in active(fake) and "could not close the job lease" in alerts() and f"ddc stop --only {stale}" in alerts()
    watchdog()                                                                            # the next pass tries again
    assert stale not in active(fake)


def test_the_reaper_does_not_run_while_another_ddc_command_holds_the_lock(fake):
    stale = put(fake, hb_min=60)
    with ddc.FileLock(wait_s=0):
        assert ddc.reap_jobs() == []
    assert stale in active(fake)
    assert ddc.reap_jobs() == [stale] and stale not in active(fake)


def test_a_heartbeat_that_arrives_while_the_reaper_waits_saves_the_lease(fake):
    stale = put(fake, hb_min=60)
    real = ddc.job_reap_reason
    calls = []

    def reason(rec, t=None):
        calls.append(1)
        if len(calls) == 2:                                                               # between the first look and the close, the runner is heard from
            ddc.update_dep(stale, heartbeat_at=fake.clock)
            rec = ddc.load_state()["deps"][stale]
        return real(rec, t)

    ddc.job_reap_reason = reason
    try:
        assert ddc.reap_jobs() == []
    finally:
        ddc.job_reap_reason = real
    assert stale in active(fake)


def test_budget_exhaustion_closes_job_leases_too(fake):
    job = put(fake, hb_min=0, usd_hr=4.0)
    ddc.CFG["budget_usd"] = 1.0                                                           # spent + a quarter hour of burn >= budget
    assert watchdog() == 0
    assert job not in active(fake) and "budget nearly exhausted" in alerts()


def test_the_runner_and_the_reaper_never_book_one_lease_twice(fake):
    stale = put(fake, hb_min=60)
    ddc.reap_jobs()
    n = len([e for e in ddc.ledger_read()[0] if e["event"] == "closed"])
    ddc.close_box(stale)                                                                  # a late close by the runner / `ddc job close`
    assert len([e for e in ddc.ledger_read()[0] if e["event"] == "closed"]) == n


# ------------------------------------------------------------------ status / stop / reconcile / refresh_live with role "job"
def test_status_shows_job_leases_without_crashing(fake, capsys):
    put(fake, hb_min=2)
    assert ddc.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "ddc-job-t1" in out and "ddc job close" in out and "silent for 30 min" in out


def test_the_users_plain_stop_closes_owned_job_leases_but_the_narrower_forms_and_the_watchdog_do_not(fake, capsys):
    """[25] `ddc stop` is the kill switch: it must stop job billing too (it used to leave job leases running unless named).  --workers-only, --only and the
    programmatic stop the watchdog uses (no include_jobs) keep their old, narrower meaning; jobs are covered there by the orphan rule and the budget stop."""
    job = put(fake, hb_min=1)
    ddc.cmd_stop(types.SimpleNamespace(workers_only=False, force=False, discard_workspace=False))   # what the watchdog's idle stop passes
    assert job in active(fake)
    assert ddc.main(["stop", "--workers-only", "--yes"]) == 0 and job in active(fake)
    assert ddc.main(["stop", "--dry-run"]) == 0
    assert f"{job}" in capsys.readouterr().out and job in active(fake)                         # the plan names the job lease, nothing closed by a dry run
    assert ddc.main(["stop", "--yes"]) == 0
    assert job not in active(fake) and ddc.load_state()["deps"][job]["status"] == "closed"


def test_the_users_stop_does_not_touch_other_roles_adopted_or_foreign_names_beyond_the_old_rules(fake):
    box = put(fake, role="box", hb_min=1)
    foreign_name = put(fake, name="other-service+db", hb_min=1)                                 # role job + ours, but not ddc-*: never closed
    not_ours = put(fake, ours=False, hb_min=1)
    job = put(fake, hb_min=1)
    assert ddc.main(["stop", "--yes"]) == 0
    assert active(fake) == {box, foreign_name, not_ours} and job not in active(fake)


def test_named_stop_still_closes_a_job_lease(fake, capsys):
    job = put(fake, hb_min=1)
    assert ddc.main(["stop", "--only", job, "--dry-run"]) == 0 and "NOT checkpointed" in capsys.readouterr().out         # named: it is in the plan, with the warning
    assert ddc.main(["stop", "--only", job, "--yes"]) == 0
    assert job not in active(fake) and ddc.load_state()["deps"][job]["status"] == "closed"


def test_one_malformed_job_record_never_aborts_the_pass_and_others_are_still_reaped(fake):
    """[24] a corrupt timestamp used to raise out of reap_jobs and so out of the whole watchdog pass."""
    bad = put(fake, hb_min=1, deadline_at="abc", heartbeat_at="not-a-time")
    stale = put(fake, hb_min=90)
    assert watchdog() == 0
    assert stale not in active(fake)
    assert bad not in active(fake)                                                              # an unreadable heartbeat is stale-after-created_at, not alive for ever


def test_an_unparseable_heartbeat_counts_as_silent_after_created_at(fake):
    d = put(fake, hb_min=0, heartbeat_at="garbage", created_at=fake.clock - 5 * 60)
    assert ddc.job_reap_reason(ddc.load_state()["deps"][d]) is None                            # created 5 min ago: not yet silent
    ddc.update_dep(d, created_at=fake.clock - 60 * 60)
    assert "silent" in ddc.job_reap_reason(ddc.load_state()["deps"][d])


def test_reap_jobs_skips_a_record_it_cannot_evaluate_with_an_alert(fake, monkeypatch):
    a, b = put(fake, hb_min=90), put(fake, hb_min=90)
    real = ddc.job_reap_reason
    monkeypatch.setattr(ddc, "job_reap_reason", lambda rec, t=None: (_ for _ in ()).throw(ValueError("corrupt")) if rec.get("job") == "t1" and rec is not None and rec.get("poison") else real(rec, t))
    ddc.update_dep(a, poison=True)
    closed = ddc.reap_jobs()
    assert closed == [b] and a in active(fake) and "cannot be evaluated" in alerts()


def test_stop_only_by_job_name_closes_just_that_lease(fake):
    a, b = put(fake, name="ddc-job-aa-0101-0001"), put(fake, name="ddc-job-bb-0101-0002")
    assert ddc.main(["stop", "--only", "ddc-job-aa-0101-0001", "--yes"]) == 0
    assert active(fake) == {b} and a not in active(fake)


def test_refresh_live_records_a_job_lease_the_platform_closed(fake):
    job = put(fake, hb_min=1)
    fake.deployments[job]["state"] = "closed"                                              # the runtime limit (or the provider) ended it
    ddc.refresh_live()
    assert ddc.load_state()["deps"][job]["status"] == "closed"
    assert [e["by"] for e in ddc.ledger_read()[0] if e["event"] == "closed"] == ["platform"]


def test_reconcile_lists_and_adopts_an_unknown_job_deployment_but_the_reaper_leaves_it_to_the_user(fake, capsys):
    ghost = fake.add_foreign("1000000000555", name="ddc-job-t1-0101-beef")                  # created by a runner whose state record was lost
    assert ddc._role_from_name("ddc-job-t1-0101-beef") == ("job", 0)
    assert ddc.main(["reconcile"]) == 0
    assert ghost in capsys.readouterr().out
    assert ddc.main(["reconcile", "--adopt", "--yes"]) == 0
    rec = ddc.load_state()["deps"][ghost]
    assert rec["role"] == "job" and rec["adopted"] is True and rec["ours"] is True
    fake.clock += 3 * 3600
    ddc.update_dep(ghost, heartbeat_at=0)
    assert ddc.reap_jobs() == [] and ghost in active(fake)                                  # adopted records are only ever closed by the user (ddc stop --only)
    assert ddc.main(["stop", "--only", ghost, "--yes"]) == 0 and ghost not in active(fake)


def test_job_close_command_closes_only_owned_job_leases(fake, capsys):
    mine = put(fake, job="nightly")
    foreign = fake.add_foreign("1000000000777", name="ddc-job-nightly-0101-ffff")           # same name pattern, but not in our state
    box = put(fake, role="box", job="nightly")
    assert ddc.main(["job", "close", "nightly"]) == 0
    assert active(fake) == {foreign, box}
    assert ddc.main(["job", "close", "nothing-here"]) == 0 and "no active job lease" in capsys.readouterr().out
    assert mine not in active(fake)


# ------------------------------------------------------------------ the provider (CloudDeck) path
def test_the_provider_lists_terminates_and_refuses_to_resize_a_job_lease(fake):
    job = put(fake, hb_min=1)
    dev = put(fake, role="dev", hb_min=0, name="ddc-dev-32-x")
    p = providers.get("akash")
    got = {i.id: i for i in p.instances()}
    assert got[job].role == "job" and got[job].state == "running" and got[job].managed and got[dev].role == "dev"
    assert [i.role for i in p.instances()].index("dev") < [i.role for i in p.instances()].index("job")
    with pytest.raises(ProviderError, match="cannot be resized"):
        p.resize(got[job], types.SimpleNamespace(with_=lambda **k: None))
    p.terminate(job)
    assert job not in active(fake) and dev in active(fake)
    assert "job" in p.spend().by_role


def test_the_tuis_stop_everything_closes_job_leases_too(fake):
    """[25] the TUI's stop-everything is the user's kill switch like `ddc stop`: it names job leases among what it closes."""
    job = put(fake, hb_min=1)
    p = providers.get("akash")
    assert "job" in p.stop_all_roles
    p.stop_all()
    assert job not in active(fake)

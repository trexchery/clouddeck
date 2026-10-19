"""`ddc job` runner: the whole flow on virtual time (fake Akash API, fake box, fake clock): overlap, streaming, retrieval, verified close, failure
and interrupt handling, image fallback, resume, telemetry, heartbeat, the old-vs-new simulation.  No network, no money."""
import json
import os
import signal
from pathlib import Path

import pytest
from job_fakes import FakeRemote, TimerPrep
from job_helpers import RESULT, SHA, make_spec, run_job

from clouddeck.engine import ddc
from clouddeck.job import preflight, telemetry
from clouddeck.job.market import History, rank_bids
from clouddeck.job.runner import HEARTBEAT_S, JobError, JobRunner


def post_dep(jw, name="ddc-job-x-1"):
    """A deployment created directly on the fake (what a runner that crashed would have left behind)."""
    return jw.akash.api("POST", "/v1/deployments", {"data": {"sdl": 'image: "x"', "name": name, "runtimeLimitHours": 1}})["data"]["dseq"]


# ------------------------------------------------------------------ the full flow
def test_full_run_overlaps_everything_and_closes_verified(jw):
    s = make_spec(jw.tmp)
    jw.akash.boot_s = {"prebuilt": 20, "default": 60}
    run_job(jw, s)
    t = jw.runner.tel.to_dict(jw.runner.usd_hr)
    m = t["marks_epoch"]
    for a, b in [("create", "bid"), ("bid", "lease"), ("lease", "ssh_ready"), ("ssh_ready", "first_job_start"), ("first_job_start", "job_done"),
                 ("job_done", "artifact_retrieved"), ("artifact_retrieved", "closed")]:
        assert m[a] <= m[b], (a, b)
    assert (jw.tmp / "got" / "out" / "result.bin").read_bytes() == RESULT           # retrieved + sha verified
    assert t["artifacts"][0]["sha256"] == SHA(RESULT)
    dseq = jw.runner.dseq
    assert jw.akash.deps[dseq]["state"] == "closed" and dseq not in jw.akash.active_leases()
    rec = ddc.load_state()["deps"][dseq]
    assert rec["status"] == "closed" and rec["role"] == "job" and rec["ours"] is True and rec["name"].startswith(ddc.NAME_PREFIX)
    assert t["summary"]["productive_s"] == pytest.approx(600, abs=1)
    assert jw.remotes[0].files["in/in.bin"] == b"input" * 5000                       # input uploaded + verified
    assert ("POST", "/v1/leases") in jw.akash.calls and sum(1 for c in jw.akash.calls if c[0] == "DELETE") == 1
    assert jw.akash.posts[0]["runtimeLimitHours"] == 2 and ddc.load_state().get("pending") == {}
    assert [e["event"] for e in ddc.ledger_read()[0]] == ["created", "leased", "closed"]


def test_job_streams_start_when_its_prerequisites_are_ready_not_after_everything(jw):
    # two independent steps: `quick` needs only the input, `slow` needs the 120 s download. quick must start before the download ends.
    s = make_spec(jw.tmp, step=[{"name": "quick", "run": "x", "needs": ["inputs"], "artifacts": ["out/*.bin"]}, {"name": "slow", "run": "y", "needs": ["cache-m"]}])
    jw.durations = {"cache-m": 120, "quick": 30, "slow": 30}
    run_job(jw, s, remote_factory=lambda h, p: jw.remote({"quick": {"out/result.bin": RESULT}}))
    st = jw.runner.tel.steps
    assert st["quick"]["start"] < st["cache-m"]["end"] <= st["slow"]["start"]


def test_bid_early_lease_late_saves_the_idle_wait(jw):
    # local prep takes 900 s; with an ETA the lease is requested ~boot+setup before it ends, not at t0
    out = {jw.tmp / "ref.bin": b"data" * 100}
    s = make_spec(jw.tmp, inputs=["in.bin", "ref.bin"], prep={"cmd": "prep", "estimate_s": 900, "produces": ["ref.bin"]}, setup_estimate_s=120)
    run_job(jw, s, prep_factory=lambda sp: TimerPrep(jw.clock, 900, out))
    m = jw.runner.tel.marks
    assert m["lease"] - m["create"] > 400                      # lease deliberately late
    assert m["ssh_ready"] - m["lease"] < 120                   # but the box is up by the time prep ends
    assert m["prep_done"] <= m["inputs_ready"] <= m["first_job_start"]


def test_legacy_sequential_mode_pays_the_wait(jw):
    out = {jw.tmp / "ref.bin": b"data" * 100}
    s = make_spec(jw.tmp, inputs=["in.bin", "ref.bin"], prep={"cmd": "prep", "estimate_s": 900, "produces": ["ref.bin"]})
    run_job(jw, s, overlap=False, prep_factory=lambda sp: TimerPrep(jw.clock, 900, out))
    m = jw.runner.tel.marks
    assert m["lease"] - m["create"] < 120                       # leased immediately
    assert m["prep_done"] - m["ssh_ready"] >= 899               # and then paid for the whole prep


def test_failure_in_a_step_still_closes_and_salvages_finished_artifacts(jw):
    s = make_spec(jw.tmp, step=[{"name": "a", "run": "x", "artifacts": ["out/*.bin"]}, {"name": "b", "run": "y"}])
    jw.durations = {"cache-m": 10, "a": 10, "b": 10}
    jw.remote_kw = {"fail": {"b": 3}}
    with pytest.raises(JobError, match="step b failed"):
        run_job(jw, s, remote_factory=lambda h, p: jw.remote({"a": {"out/result.bin": RESULT}}))
    assert jw.akash.deps[jw.runner.dseq]["state"] == "closed"
    assert (jw.tmp / "got" / "out" / "result.bin").read_bytes() == RESULT   # the finished step's artifact survived


def test_keyboard_interrupt_closes_the_lease(jw, monkeypatch):
    calls = {"n": 0}
    orig = FakeRemote.markers

    def boom(self):
        calls["n"] += 1
        if calls["n"] == 3:
            raise KeyboardInterrupt
        return orig(self)

    monkeypatch.setattr(FakeRemote, "markers", boom)
    with pytest.raises(KeyboardInterrupt):
        run_job(jw, make_spec(jw.tmp))
    assert jw.akash.deps[jw.runner.dseq]["state"] == "closed" and jw.runner.closed


def test_atexit_safety_net_closes_if_finally_was_skipped(jw):
    r = JobRunner(make_spec(jw.tmp), clock=jw.clock, remote_factory=jw.factory, install_signals=False, log=lambda m: None)
    r.tel.mark("create")
    r.market = None
    r.dseq = post_dep(jw)
    ddc.update_dep(r.dseq, ours=True, name="ddc-job-x-1", role="job", status="created", created_at=jw.clock.time())
    r._atexit()
    assert jw.akash.deps[r.dseq]["state"] == "closed"


def test_wall_clock_limit_stops_a_runaway_job_before_the_platform_limit_and_cost_stays_bounded(jw):
    s = make_spec(jw.tmp, runtime_hours=1.5, max_cost_usd=6.5)
    jw.durations = {"cache-m": 10, "work": 3 * 3600}
    with pytest.raises(JobError, match="wall-clock"):
        run_job(jw, s)
    assert jw.akash.deps[jw.runner.dseq]["state"] == "closed"
    assert jw.akash.deps[jw.runner.dseq]["limit"] == 2                       # platform backstop (ceil of 1.5 h) is still set
    assert jw.akash.cost(jw.runner.dseq) <= 1.5 * 3.0 <= 6.5                 # closed by us before the lease could run to the limit


def test_idle_abort_when_nothing_runs_or_transfers(jw, monkeypatch):
    s = make_spec(jw.tmp, idle_abort_s=300, step=[{"name": "work", "run": "x", "needs": ["inputs"], "artifacts": []}])
    orig = FakeRemote.mark_done
    monkeypatch.setattr(FakeRemote, "mark_done", lambda self, name: None if name == "inputs" else orig(self, name))  # the box never gets its inputs marker
    jw.durations = {"cache-m": 5, "work": 10}
    with pytest.raises(JobError, match="idle"):
        run_job(jw, s)
    assert jw.akash.deps[jw.runner.dseq]["state"] == "closed"


def test_real_signal_handler_closes_lease_and_second_ctrl_c_skips_salvage(jw, monkeypatch):
    calls = {"n": 0}
    orig = FakeRemote.markers

    def markers(self):
        calls["n"] += 1
        if calls["n"] in (3, 4):          # 3rd poll: first Ctrl-C; 4th call is inside salvage: second Ctrl-C
            signal.raise_signal(signal.SIGINT)
        return orig(self)

    monkeypatch.setattr(FakeRemote, "markers", markers)
    before = signal.getsignal(signal.SIGINT)
    with pytest.raises(KeyboardInterrupt):
        run_job(jw, make_spec(jw.tmp), install_signals=True)
    assert jw.runner.interrupts == 2 and jw.runner.hard_stop
    assert jw.akash.deps[jw.runner.dseq]["state"] == "closed" and jw.runner.dseq not in jw.akash.active_leases()
    assert signal.getsignal(signal.SIGINT) is before          # handler restored


# ------------------------------------------------------------------ image fallback + provider affinity
def test_prebuilt_failure_falls_back_to_stock_image_and_remembers_the_failure(jw):
    jw.akash.boot_s = {"ghcr.io/example/base": float("inf"), "default": 60}   # prebuilt never boots (e.g. pull failure)
    run_job(jw, make_spec(jw.tmp))
    assert [d["image"] for d in jw.akash.deps.values()] == ["ghcr.io/example/base:1.0", "ubuntu:24.04"]
    assert all(d["state"] == "closed" for d in jw.akash.deps.values())
    t = jw.runner.tel.to_dict(jw.runner.usd_hr)
    assert t["failed_attempts"][0]["image_kind"] == "prebuilt" and "image_fallback" in t["marks_epoch"]
    assert History(ddc.STATE_DIR / "provider_history.json", jw.clock).blocked("akash1cheap", "ghcr.io/example/base:1.0")


def test_no_bids_retries_same_image_once_then_fails_without_blaming_the_image(jw):
    jw.akash.bid_delay_s = 10 ** 9                       # providers look healthy but nobody bids
    with pytest.raises(JobError, match="could not provision"):
        run_job(jw, make_spec(jw.tmp))
    assert [d["image"] for d in jw.akash.deps.values()] == ["ghcr.io/example/base:1.0"] * 2    # same (prebuilt) image twice, no fallback
    assert all(d["state"] == "closed" for d in jw.akash.deps.values())


def test_provider_affinity_prefers_provider_that_booted_the_image_before(jw):
    h = History(ddc.STATE_DIR / "provider_history.json", jw.clock)
    h.record("akash1mid", "img:1", ok=True, boot_s=25)
    s = make_spec(jw.tmp, estimated_runtime_min=10)  # short job: a 300 s cold boot on the cheaper provider outweighs the $0.40/h gap
    dseq = post_dep(jw, "ddc-job-a-1")
    jw.clock.sleep(60)
    raw = jw.akash.api("GET", f"/v1/bids?dseq={dseq}")["data"]
    r = rank_bids(raw, s, h, "img:1", jw.clock)
    assert [x["provider"] for x in r] == ["akash1mid", "akash1cheap"] and r[0]["known"]   # flaky (uptime 0.5) excluded
    long = make_spec(jw.tmp, estimated_runtime_min=600)
    assert [x["provider"] for x in rank_bids(raw, long, h, "img:1", jw.clock)][0] == "akash1cheap"  # long job: price dominates


def test_a_bid_above_the_users_cap_is_never_leased(jw):
    dseq = post_dep(jw, "ddc-job-a-1")
    jw.clock.sleep(60)
    raw = jw.akash.api("GET", f"/v1/bids?dseq={dseq}")["data"]
    h = History(ddc.STATE_DIR / "provider_history.json", jw.clock)
    tight = make_spec(jw.tmp, hardware={"cpu": 4, "memory": "16Gi", "disk": "50Gi", "max_usd_hr": 2.1, "gpu": {"vendor": "vendor-a", "models": ["model-a"]}})
    assert [x["provider"] for x in rank_bids(raw, tight, h, "i:1", jw.clock)] == ["akash1cheap"]            # the $2.40 bid is over the $2.10 cap


# ------------------------------------------------------------------ resumability
def test_upload_resumes_after_a_dropped_connection_and_verifies_sha(jw):
    s = make_spec(jw.tmp)
    (jw.tmp / "in.bin").write_bytes(os.urandom(3 << 20))   # 3 chunks of 1 MB
    jw.remote_kw = {"drop_after": 8}
    run_job(jw, s)
    assert jw.remotes[0].dropped and jw.remotes[0].files["in/in.bin"] == (jw.tmp / "in.bin").read_bytes()


def test_rerun_after_crash_resumes_the_open_lease_instead_of_creating_a_new_one(jw):
    s = make_spec(jw.tmp)
    jw.durations = {"cache-m": 10, "work": 3000}
    r = JobRunner(s, clock=jw.clock, remote_factory=jw.factory, install_signals=False, log=lambda m: None)
    r._monitor = lambda: (_ for _ in ()).throw(SystemExit("simulated hard kill"))
    r._atexit = lambda: None
    with pytest.raises(SystemExit):
        r._run()                     # _finish is not called: the process "died" with the lease open
    first = r.dseq
    assert jw.akash.deps[first]["state"] == "active"
    assert ddc.load_state()["deps"][first]["status"] == "ready"          # the state says so too: the watchdog's never-became-ready rule leaves it alone
    run_job(jw, s)                   # a fresh runner resumes
    assert list(jw.akash.deps) == [first] and jw.akash.deps[first]["state"] == "closed"
    assert (jw.tmp / "got" / "out" / "result.bin").exists()


def test_orphaned_unresumable_lease_is_closed_before_a_new_run(jw):
    dseq = post_dep(jw, "ddc-job-t1-0101-aa11")
    ddc.update_dep(dseq, ours=True, name="ddc-job-t1-0101-aa11", role="job", status="created", created_at=jw.clock.time(), limit_h=1, max_usd_hr=1.0)
    ddc.write_json(ddc.STATE_DIR / "jobs" / "t1.json", {"name": "t1", "dseq": dseq, "status": "leased"})
    run_job(jw, make_spec(jw.tmp))
    assert jw.akash.deps[dseq]["state"] == "closed" and len(jw.akash.deps) == 2


# ------------------------------------------------------------------ closing is the controller's, once
def test_a_lease_already_closed_by_someone_else_is_not_booked_twice(jw):
    """`ddc stop --only`, the watchdog or `ddc job close` may close the lease while the runner is still going: the runner's own close must not
    add a second ledger entry for the same lease."""
    s = make_spec(jw.tmp)
    jw.durations = {"cache-m": 10, "work": 3000}
    r = JobRunner(s, clock=jw.clock, remote_factory=jw.factory, install_signals=False, log=lambda m: None)
    r._monitor = lambda: (_ for _ in ()).throw(SystemExit("simulated hard kill"))
    r._atexit = lambda: None
    with pytest.raises(SystemExit):
        r._run()
    ddc.close_box(r.dseq)                                                # someone else closes it
    closed_events = [e for e in ddc.ledger_read()[0] if e["event"] == "closed"]
    r._close()
    assert r.closed and [e for e in ddc.ledger_read()[0] if e["event"] == "closed"] == closed_events


# ------------------------------------------------------------------ heartbeat + record fields the watchdog's orphan rule reads
def test_heartbeat_is_recorded_throttled_and_the_lease_carries_a_deadline(jw, monkeypatch):
    beats = []
    real = ddc.update_dep
    monkeypatch.setattr(ddc, "update_dep", lambda dseq, **f: (beats.append(f["heartbeat_at"]) if "heartbeat_at" in f else None, real(dseq, **f))[1])
    jw.durations = {"cache-m": 10, "work": 1800}
    run_job(jw, make_spec(jw.tmp))
    rec = ddc.load_state()["deps"][jw.runner.dseq]
    assert rec["role"] == "job" and rec["job"] == "t1" and rec["deadline_at"] == pytest.approx(rec["lease_at"] + 2 * 3600)
    assert all(b - a >= HEARTBEAT_S - 1e-6 for a, b in zip(beats[2:], beats[3:]))     # throttled (beats[0] is the lease record, beats[1] the first runner beat)
    assert 20 <= len(beats) <= 60                                                      # ~one a minute over a 30+ minute run, not one per poll
    assert rec["heartbeat_at"] >= rec["lease_at"] + 1500


def test_the_runner_clamps_its_caps_to_the_config_before_it_creates_anything(jw):
    ddc.CFG["max_price_usd_hr"] = 2.5
    s = make_spec(jw.tmp, runtime_hours=2, hardware={"cpu": 4, "memory": "16Gi", "disk": "50Gi", "max_usd_hr": 9.0, "gpu": {"vendor": "vendor-a", "models": ["model-a"]}})
    run_job(jw, s)
    sdl = jw.akash.posts[0]["sdl"]
    assert f"amount: {ddc.uact_block_from_usd_hr(2.5)}\n" in sdl and f"amount: {ddc.uact_block_from_usd_hr(9.0)}\n" not in sdl


# ------------------------------------------------------------------ telemetry
def test_telemetry_breakdown_math():
    marks = {"create": 0, "lease": 100, "ssh_ready": 150, "first_job_start": 400, "job_done": 1400, "closed": 1500}
    steps = {"a": {"start": 400, "end": 800, "productive": True}, "b": {"start": 900, "end": 1400, "productive": True}, "dl": {"start": 150, "end": 380, "productive": False}}
    b = telemetry.breakdown(marks, steps, 3.6)
    assert b["billed_s"] == 1400 and b["productive_s"] == 900 and b["parts"]["boot_s"] == 50
    assert b["parts"]["wait_before_first_job_s"] == 250 and b["parts"]["gaps_between_jobs_s"] == 100 and b["parts"]["after_last_job_s"] == 100
    assert b["billed_usd_est"] == pytest.approx(1400 / 3600 * 3.6, abs=1e-4) and b["productive_pct"] == pytest.approx(64.3, abs=0.1)
    assert telemetry.union_len([(0, 10), (5, 12), (20, 25)]) == 17


def test_telemetry_json_written_with_all_phases(jw):
    run_job(jw, make_spec(jw.tmp))
    f = list((ddc.STATE_DIR / "telemetry").glob("t1-*.json"))
    assert len(f) == 1
    d = json.loads(f[0].read_text())
    for k in ("create", "bid", "lease", "manifest", "container_ready", "ssh_ready", "setup_done", "first_job_start", "job_done", "artifact_retrieved", "closed"):
        assert k in d["marks_epoch"], k
    assert d["ok"] is True and "billed" in telemetry.format_summary(d)
    assert preflight.limit_h(make_spec(jw.tmp, runtime_hours=0.2)) == 1


def test_a_hostile_artifact_path_from_the_box_cannot_escape_the_artifacts_dir(jw, monkeypatch):
    """The box is not trusted: a path it reports must stay below artifacts_dir even if the box's glob answer is forged."""
    s = make_spec(jw.tmp)
    jw.durations = {"cache-m": 10, "work": 10}
    monkeypatch.setattr(FakeRemote, "glob", lambda self, pats: [("../../escaped.bin", 3, SHA(b"abc"))])
    with pytest.raises(JobError, match="refusing artifact path"):
        run_job(jw, s)
    assert not (jw.tmp.parent / "escaped.bin").exists() and jw.akash.deps[jw.runner.dseq]["state"] == "closed"


def test_url_substitution_only_uses_declared_variables(jw, monkeypatch):
    monkeypatch.setenv("UNRELATED_LOCAL_VALUE", "do-not-leak")
    monkeypatch.setenv("DECLARED_TOKEN", "tok123")
    s = make_spec(jw.tmp, env={"REGION": "eu", "secrets": ["DECLARED_TOKEN"]},
                  cache={"item": [{"name": "m", "dest": "m.bin", "url": "https://h.example/$REGION/${DECLARED_TOKEN}/${UNRELATED_LOCAL_VALUE}/m.bin", "sha256": "a" * 64}]})
    r = JobRunner(s, clock=jw.clock, remote_factory=jw.factory, install_signals=False, log=lambda m: None)
    assert r._urls(s["cache"]["items"][0]) == ["https://h.example/eu/tok123/${UNRELATED_LOCAL_VALUE}/m.bin"]


# ------------------------------------------------------------------ simulation (old vs new)
def test_simulation_shows_the_new_flow_cheaper_for_the_same_work():
    import job_sim
    r = job_sim.scenarios()
    A, B = r["A_session"], r["B_single_job"]
    assert A["new"]["billed_s"] < A["old"]["billed_s"] and B["new"]["billed_s"] < B["old"]["billed_s"]
    assert A["new"]["productive_s"] == A["old"]["productive_s"]         # same work done: the saving is idle/overhead only
    assert B["new"]["productive_pct"] > B["old"]["productive_pct"]
    s = r["A_sensitivity_new"]
    assert s["prebuilt image saves 0 s of apt"]["billed_s"] >= s["prebuilt image saves 15 s of apt"]["billed_s"]
    assert s["prep ETA 300 s too optimistic"]["billed_s"] > A["new"]["billed_s"] >= s["no prep ETA given (lease after prep done)"]["billed_s"]
    assert s["no prep ETA given (lease after prep done)"]["job_done_after_create_s"] > A["new"]["job_done_after_create_s"]  # safe side costs latency, not money


def test_the_simulation_script_runs_standalone_in_a_throwaway_state_dir():
    """`python tests/job_sim.py` must never touch real state: it redirects the controller's directories before importing it."""
    import subprocess
    import sys
    import tempfile
    here = Path(__file__).resolve().parent
    env = {k: v for k, v in os.environ.items() if not k.startswith("DDC_") and k != "PYTHONPATH"} | {"PYTHONDONTWRITEBYTECODE": "1"}   # no PYTHONPATH: a plain checkout must work (docs/JOBS.md)
    cp = subprocess.run([sys.executable, str(here / "job_sim.py"), "--json"], env=env, capture_output=True, text=True, cwd=tempfile.gettempdir(), timeout=300)
    assert cp.returncode == 0, cp.stderr[-800:]
    assert json.loads(cp.stdout)["A_session"]["new"]["billed_s"] > 0

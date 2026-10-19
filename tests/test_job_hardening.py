"""Regression tests for the review of the `ddc job` integration: artifact retrieval limits, per-lease host keys, secrets rules, strict validators and
path names, bid and price sanity, an interrupt during the close, an interrupt between POST and return, SDL price read-back, heartbeat during blocking
calls, one run per job name.  Virtual-time fake Akash (tests/job_fakes.py), isolated state, no network."""
from __future__ import annotations

import importlib.util
import signal
import subprocess
import types
from pathlib import Path

import pytest
from job_fakes import FakeRemote
from job_helpers import RESULT, make_spec, run_job

from clouddeck import sdlgen
from clouddeck.engine import ddc
from clouddeck.job import preflight
from clouddeck.job import remote as remotemod
from clouddeck.job import runner as runnermod
from clouddeck.job import spec as specmod
from clouddeck.job.market import History, Market, ProvisionFailed, rank_bids
from clouddeck.job.remote import HostKeyChanged, SshRemote
from clouddeck.job.runner import JobError, JobRunner
from clouddeck.job.telemetry import Telemetry

ROOT = Path(__file__).resolve().parent.parent
PUB = "ssh-ed25519 AAAATESTPUBLIC test"


def job_sdl(s):
    return specmod.render_sdl(s, "i:1", PUB)


def closed_deps(jw):
    return [k for k, d in jw.akash.deps.items() if d["state"] == "closed"]


# ------------------------------------------------------------------ [6] validators: no trailing newline, anywhere
@pytest.mark.parametrize("rx, ok", [(specmod.NAME_RE, "job-1"), (specmod.STEP_RE, "step_1"), (specmod.ENV_NAME, "FOO_1"), (specmod.SIZE_RE, "16Gi"),
                                     (specmod.GLOB_RE, "out/*.bin"), (specmod.SHA256_RE, "a" * 64), (specmod.URL_RE, "https://h.example/x")])
def test_validators_reject_a_trailing_newline(rx, ok):
    assert rx.fullmatch(ok) and not rx.fullmatch(ok + "\n")


def test_spec_and_remote_refuse_newline_smuggling(jw):
    with pytest.raises(specmod.SpecError, match="name"):
        make_spec(jw.tmp, name="t1\n")
    with pytest.raises(specmod.SpecError, match="artifact pattern"):
        make_spec(jw.tmp, step=[{"name": "work", "run": "x", "artifacts": ["a\n", "b"]}])
    r = SshRemote("203.0.113.5", 22, Path("k"), Path("kh"), jw.clock)
    with pytest.raises(ValueError):
        r.glob(["out/a\n"])
    with pytest.raises(ValueError):
        r.finalize("x.bin", "a" * 64 + "\n")


# ------------------------------------------------------------------ [8] box-controlled text and names
@pytest.mark.parametrize("bad", ["CON", "x/NUL", "a/con.txt", "LPT1", "a.txt:stream", "dir./f", "f.", "f ", 'a"b', "a<b", "a|b", "a\\b", "a\x1bb", "a\nb", "x/../y", "/abs", "", "a//b", "a?b"])
def test_safe_rel_path_rejects_hostile_and_windows_special_names(bad):
    assert not specmod.safe_rel_path(bad)


def test_safe_rel_path_accepts_ordinary_paths_and_globs_only_in_glob_mode():
    assert specmod.safe_rel_path("out/result-1.bin") and specmod.safe_rel_path("a/b.c/d_e")
    assert not specmod.safe_rel_path("out/*.bin") and specmod.safe_rel_path("out/*.bin", glob=True)
    assert not specmod.safe_rel_path("out/*:x", glob=True)


def test_scrub_removes_escape_and_control_characters_but_keeps_lines():
    out = specmod.scrub("ok\x1b[2J\x1b]0;pwned\x07 line1\nline2\r\x9b31m tab\there")
    assert "\x1b" not in out and "\x07" not in out and "\r" not in out and "\x9b" not in out and "line1\nline2" in out and "\t" in out


def test_a_hostile_log_tail_and_reason_never_reach_the_log_raw(jw, monkeypatch):
    monkeypatch.setattr(FakeRemote, "tail_log", lambda self, name, n=15: "boom\x1b[31m red \x1b]0;title\x07\n")
    jw.remote_kw = {"fail": {"work": 3}}
    logs: list[str] = []
    with pytest.raises(JobError) as ei:
        run_job(jw, make_spec(jw.tmp), log=logs.append)
    assert "\x1b" not in str(ei.value) and "\x07" not in str(ei.value) and "boom" in str(ei.value)
    assert not any("\x1b" in m for m in logs)


# ------------------------------------------------------------------ [5] secrets rules
@pytest.mark.parametrize("name", ["AKASH_API_KEY", "akash_api_key", "DDC_STATE_DIR", "AKASH_NODE"])
def test_credentials_of_the_controller_are_refused_as_job_secrets(jw, name):
    with pytest.raises(specmod.SpecError, match="never pushed"):
        make_spec(jw.tmp, env={"secrets": [name]})


def test_push_secrets_refuses_them_as_the_last_line_of_defence(jw):
    r = SshRemote("203.0.113.5", 22, Path("k"), Path("kh"), jw.clock)
    with pytest.raises(ValueError, match="never pushed"):
        r.push_secrets({"AKASH_API_KEY": "x"})


def test_preflight_lists_the_names_it_will_push_and_never_a_value(jw, monkeypatch):
    monkeypatch.setenv("EXAMPLE_API_TOKEN", "v4lue-xyz")
    monkeypatch.setenv("OTHER_SECRET_THING", "another-v4lue")
    cs = {c.name: c for c in preflight.run(make_spec(jw.tmp, env={"secrets": ["EXAMPLE_API_TOKEN", "OTHER_SECRET_THING"]}), offline=True)}
    d = cs["secrets"].detail
    assert "EXAMPLE_API_TOKEN" in d and "OTHER_SECRET_THING" in d and "will be pushed" in d and "v4lue" not in d


# ------------------------------------------------------------------ [7] download sources and checksums in the spec
def item(**kw):
    return {"cache": {"item": [{"name": "m", "dest": "m.bin", "url": "https://h.example/m.bin", **kw}]}}


def test_a_cache_item_needs_a_checksum_unless_explicitly_unverified(jw):
    with pytest.raises(specmod.SpecError, match="sha256 is required"):
        make_spec(jw.tmp, **item())
    s = make_spec(jw.tmp, **item(allow_unverified=True))
    assert s["cache"]["items"][0]["allow_unverified"] is True and not s["cache"]["items"][0]["sha256"]
    assert [c for c in preflight.run(s, offline=True) if c.name == "cache"][0].status == "warn"
    with pytest.raises(specmod.SpecError, match="64 hex"):
        make_spec(jw.tmp, **item(sha256="abc"))


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://h.example/x", "-o/tmp/x", "--config=x", "https://h.example/a b", "http://"])
def test_download_sources_must_be_plain_http_urls(jw, url):
    with pytest.raises(specmod.SpecError, match="http"):
        make_spec(jw.tmp, cache={"item": [{"name": "m", "dest": "m.bin", "url": url, "sha256": "a" * 64}]})
    with pytest.raises(specmod.SpecError, match="mirrors"):
        make_spec(jw.tmp, cache={"mirrors": [url], "item": item(sha256="a" * 64)["cache"]["item"]})


def test_a_variable_cannot_turn_a_source_into_an_option(jw, monkeypatch):
    monkeypatch.setenv("EVIL_TOKEN", "-o /tmp/x")
    s = make_spec(jw.tmp, env={"secrets": ["EVIL_TOKEN"]}, cache={"item": [{"name": "m", "dest": "m.bin", "url": "https://h.example/${EVIL_TOKEN}", "sha256": "a" * 64}]})
    r = JobRunner(s, clock=jw.clock, remote_factory=jw.factory, install_signals=False, log=lambda m: None)
    with pytest.raises(JobError, match="plain http"):
        r._urls(s["cache"]["items"][0])


# ------------------------------------------------------------------ [14] GPU vendor is explicit
def test_a_gpu_spec_must_name_its_vendor(jw):
    hw = {"cpu": 4, "memory": "16Gi", "disk": "50Gi", "max_usd_hr": 3.0, "gpu": {"models": ["model-a"]}}
    with pytest.raises(specmod.SpecError) as ei:
        make_spec(jw.tmp, hardware=hw)
    assert "hardware.gpu.vendor" in str(ei.value) and "nvidia" not in str(ei.value).lower()
    hw["gpu"]["vendor"] = "vendor-a"
    assert make_spec(jw.tmp, hardware=hw)["hardware"]["gpu"]["vendor"] == "vendor-a"


# ------------------------------------------------------------------ [3] bounded artifact retrieval
def test_a_box_that_reports_a_huge_artifact_is_not_pulled_and_the_lease_is_closed(jw, monkeypatch):
    monkeypatch.setattr(FakeRemote, "glob", lambda self, pats: [("out/result.bin", 10**13, "a" * 64)])
    gets: list[int] = []
    monkeypatch.setattr(FakeRemote, "get", lambda self, p, o, n: gets.append(o) or b"x")
    with pytest.raises(JobError, match="max_artifact_gb"):
        run_job(jw, make_spec(jw.tmp))
    assert not gets and jw.akash.deps[jw.runner.dseq]["state"] == "closed"


def test_the_artifact_cap_is_a_validated_spec_key_with_a_default_and_applies_per_job(jw):
    assert make_spec(jw.tmp)["max_artifact_gb"] == specmod.DEFAULT_MAX_ARTIFACT_GB > 0
    for bad in (0, -1, "10", True):
        with pytest.raises(specmod.SpecError, match="max_artifact_gb"):
            make_spec(jw.tmp, max_artifact_gb=bad)
    with pytest.raises(JobError, match="max_artifact_gb"):
        run_job(jw, make_spec(jw.tmp, max_artifact_gb=len(RESULT) / (1 << 30) / 2))      # half of the real size: refused before any byte is pulled
    assert jw.akash.deps[jw.runner.dseq]["state"] == "closed" and not (jw.tmp / "got" / "out" / "result.bin").exists()


def test_retrieval_checks_free_local_disk_before_pulling(jw, monkeypatch):
    monkeypatch.setattr(runnermod.shutil, "disk_usage", lambda p: types.SimpleNamespace(total=1, used=1, free=1000))
    gets: list[int] = []
    orig = FakeRemote.get
    monkeypatch.setattr(FakeRemote, "get", lambda self, p, o, n: gets.append(o) or orig(self, p, o, n))
    with pytest.raises(JobError, match="free disk"):
        run_job(jw, make_spec(jw.tmp))
    assert not gets and jw.akash.deps[jw.runner.dseq]["state"] == "closed"


def test_retrieval_stops_at_the_jobs_own_wall_clock_and_budget_limits(jw, monkeypatch):
    """The box answers slowly forever: the loop checks the same limits as the monitor and aborts (the lease is then closed)."""
    orig = FakeRemote.get

    def slow(self, path, offset, n):
        self.clock.sleep(4 * 3600)
        return orig(self, path, offset, 4000)

    monkeypatch.setattr(FakeRemote, "get", slow)
    with pytest.raises(JobError, match="budget guard|wall-clock"):
        run_job(jw, make_spec(jw.tmp))
    assert jw.akash.deps[jw.runner.dseq]["state"] == "closed" and not (jw.tmp / "got" / "out" / "result.bin").exists()


def test_a_box_cannot_make_the_runner_write_more_than_the_announced_size(jw, monkeypatch):
    orig = FakeRemote.get
    monkeypatch.setattr(FakeRemote, "get", lambda self, p, o, n: orig(self, p, o, n) + b"EXTRA" * 100)
    run_job(jw, make_spec(jw.tmp))
    assert (jw.tmp / "got" / "out" / "result.bin").read_bytes() == RESULT          # the surplus was cut at the announced size, the sha256 still matched


# ------------------------------------------------------------------ [4] [20] host keys per lease, a changed key is fatal
def test_known_hosts_is_per_lease_and_forgotten_when_the_lease_is_closed(jw):
    r = JobRunner(make_spec(jw.tmp), clock=jw.clock, remote_factory=jw.factory, install_signals=False, log=lambda m: None, state_dir=jw.tmp / "jobs")
    assert r._known_hosts("1001") != r._known_hosts("1002") and "1001" in r._known_hosts("1001").name and "t1" in r._known_hosts("1001").name
    r.dseq = "1001"
    remote = r._ssh_remote("203.0.113.9", 22)
    assert remote.known_hosts == r._known_hosts("1001") and remote.beat == r._beat
    r.dseq = "1002"
    assert r._ssh_remote("203.0.113.9", 22).known_hosts != remote.known_hosts
    run_job(jw, make_spec(jw.tmp), state_dir=jw.tmp / "jobs2")
    kh = jw.runner._known_hosts(jw.runner.dseq)
    kh.parent.mkdir(parents=True, exist_ok=True)
    kh.write_text("x")
    jw.runner.closed = False                                   # close again (the fake reports it already closed): the file goes away with the lease
    jw.runner._close()
    assert not kh.exists()


def test_ssh_host_key_changed_is_fatal_at_once_not_retried_until_the_boot_timeout(jw, monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 255, b"", b"@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@\r\nHost key verification failed.\r\n")

    monkeypatch.setattr(remotemod.subprocess, "run", fake_run)
    r = SshRemote("203.0.113.5", 22, Path("k"), Path("kh"), jw.clock)
    t0 = jw.clock.time()
    with pytest.raises(HostKeyChanged):
        r.run("true")
    assert len(calls) == 1 and jw.clock.time() == t0           # one attempt, no backoff sleeps
    with pytest.raises(HostKeyChanged):
        r.probe()                                              # not swallowed as "sshd is not up yet"


def test_a_changed_host_key_fails_the_attempt_closes_the_lease_and_the_retry_uses_a_fresh_one(jw):
    probes = []

    class Changed(FakeRemote):
        def probe(self):
            probes.append(1)
            raise HostKeyChanged("ssh refused: its host key changed")

    def factory(host, port):
        r = Changed(jw.clock, jw.akash, durations=jw.durations)
        r.dseq = list(jw.akash.deps)[-1]
        return r

    t0 = jw.clock.time()
    with pytest.raises(JobError, match="could not provision"):
        run_job(jw, make_spec(jw.tmp), remote_factory=factory)
    assert len(probes) == 2 and len(jw.akash.posts) == 2        # one probe per attempt, then a fresh lease, then give up
    assert len(closed_deps(jw)) == 2 and not jw.akash.active_leases()
    assert jw.clock.time() - t0 < 1500                           # nowhere near two boot timeouts of waiting


# ------------------------------------------------------------------ [13] SIGHUP and signals during the close
@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="POSIX only")
def test_sighup_is_handled_like_sigterm(jw):
    r = JobRunner(make_spec(jw.tmp), clock=jw.clock, remote_factory=jw.factory, log=lambda m: None)
    old = r._install_signals()
    try:
        assert signal.SIGHUP in old
        with pytest.raises(KeyboardInterrupt):
            signal.getsignal(signal.SIGHUP)(signal.SIGHUP, None)
    finally:
        r._restore_signals(old)


def test_a_signal_during_the_close_is_deferred_not_raised(jw):
    r = JobRunner(make_spec(jw.tmp), clock=jw.clock, remote_factory=jw.factory, log=lambda m: None)
    old = r._install_signals()
    try:
        h = signal.getsignal(signal.SIGINT)
        r._closing = True
        h(signal.SIGINT, None)                                  # does not raise
        assert r._deferred_signal == signal.SIGINT and r.interrupts == 1
        r._closing = False
        with pytest.raises(KeyboardInterrupt):
            h(signal.SIGINT, None)
    finally:
        r._restore_signals(old)


# ------------------------------------------------------------------ [18] an interrupt inside the verified close
def test_a_keyboard_interrupt_during_the_close_does_not_abort_it(jw, monkeypatch):
    real, n = ddc.close_box, [0]

    def close_once_interrupted(dseq, listed=None):
        n[0] += 1
        if n[0] == 1:
            raise KeyboardInterrupt
        return real(dseq, listed)

    monkeypatch.setattr(ddc, "close_box", close_once_interrupted)
    with pytest.raises(KeyboardInterrupt):                      # honoured, but only after the lease was closed
        run_job(jw, make_spec(jw.tmp))
    r = jw.runner
    assert r.closed and jw.akash.deps[r.dseq]["state"] == "closed" and n[0] == 2
    assert [e["event"] for e in ddc.ledger_read()[0]].count("closed") == 1
    assert list((ddc.STATE_DIR / "telemetry").glob("t1-*.json"))   # the report was still written


def test_the_exit_hook_stays_registered_until_the_lease_is_really_closed(jw, monkeypatch):
    seen = {"reg": 0, "unreg": 0}
    fake_atexit = types.SimpleNamespace(register=lambda f: seen.__setitem__("reg", seen["reg"] + 1), unregister=lambda f: seen.__setitem__("unreg", seen["unreg"] + 1))
    monkeypatch.setattr(runnermod, "atexit", fake_atexit)
    real = ddc.close_box
    monkeypatch.setattr(ddc, "close_box", lambda dseq, listed=None: (_ for _ in ()).throw(ddc.DdcError("api down")))
    run_job(jw, make_spec(jw.tmp))                              # the failed close is reported, not raised
    r = jw.runner
    assert not r.closed and seen == {"reg": 1, "unreg": 0}
    monkeypatch.setattr(ddc, "close_box", real)
    r._atexit()                                                 # what the interpreter runs at exit
    assert r.closed and jw.akash.deps[r.dseq]["state"] == "closed"


# ------------------------------------------------------------------ [19] an interrupt between POST and return
def test_an_interrupt_after_the_post_is_resolved_by_name_and_closed(jw, monkeypatch):
    real = ddc.update_state

    def interrupted(fn):
        if getattr(fn, "__name__", "") == "promote":            # the POST has executed; the record is still the write-ahead `pending` one
            raise KeyboardInterrupt
        return real(fn)

    monkeypatch.setattr(ddc, "update_state", interrupted)
    with pytest.raises(KeyboardInterrupt):
        run_job(jw, make_spec(jw.tmp))
    assert len(jw.akash.posts) == 1 and len(closed_deps(jw)) == 1 and not jw.akash.active_leases()
    assert ddc.pending_creations() == {}


def test_an_interrupt_after_the_record_was_promoted_still_closes_the_lease(jw, monkeypatch):
    real = ddc.ledger_add

    def interrupted(entry):
        if entry.get("event") == "created":
            real(entry)
            raise KeyboardInterrupt
        return real(entry)

    monkeypatch.setattr(ddc, "ledger_add", interrupted)
    with pytest.raises(KeyboardInterrupt):
        run_job(jw, make_spec(jw.tmp))
    assert len(closed_deps(jw)) == 1 and jw.runner.closed and jw.runner.dseq in jw.akash.deps


# ------------------------------------------------------------------ [17] bid and price sanity
def bid(amount, denom="uact", prov="akash1cheap", state="open"):
    return {"bid": {"id": {"provider": prov, "dseq": "1", "gseq": 1, "oseq": 1}, "state": state, "price": {"denom": denom, "amount": amount}}}


def ranker(jw, bids, **over):
    s = make_spec(jw.tmp, **over)
    return rank_bids(bids, s, History(ddc.STATE_DIR / "provider_history.json", jw.clock), "i:1", jw.clock)


@pytest.mark.parametrize("amount", ["nan", "NaN", "inf", "-inf", "0", "-5", "abc", None, "", "1e999"])
def test_job_bid_ranking_drops_prices_that_are_not_finite_and_positive(jw, amount):
    assert ranker(jw, [bid(amount)]) == []


def test_job_bid_ranking_drops_a_foreign_denom_and_keeps_a_real_bid(jw):
    assert ranker(jw, [bid("1000", denom="uakt")]) == []
    ok = ranker(jw, [bid("1000"), bid("nan", prov="akash1mid")])
    assert [b["provider"] for b in ok] == ["akash1cheap"] and ok[0]["usd_hr"] == pytest.approx(0.6)
    assert ddc.bid_usd_hr({"denom": "uact", "amount": "1000"}) == pytest.approx(0.6) and ddc.bid_usd_hr("junk") is None


@pytest.mark.parametrize("usd_hr", [float("nan"), float("inf"), 0.0, -1.0, 99.0, "2", None, True])
def test_lease_refuses_a_choice_with_an_unusable_price_before_calling_the_api(jw, usd_hr):
    m = Market(make_spec(jw.tmp), jw.clock, Telemetry("t1", jw.clock), History(ddc.STATE_DIR / "provider_history.json", jw.clock), lambda m: None)
    m.dseq, m.manifest = "1000001", {"m": 1}
    with pytest.raises(ddc.DdcError, match="refusing to lease"):
        m.lease({"id": {"provider": "akash1cheap", "gseq": 1, "oseq": 1}, "provider": "akash1cheap", "host": "h", "usd_hr": usd_hr})
    assert ("POST", "/v1/leases") not in jw.akash.calls


def rec(**kw):
    return {"ours": True, "name": "ddc-job-x-0101", "role": "job", "status": "ready", "limit_h": 24, "lease_at": ddc.now(), "created_at": ddc.now(), "provider": "p", **kw}


def test_a_nan_or_negative_price_in_a_record_is_costed_at_its_cap_never_as_free(jw):
    nan = float("nan")
    assert ddc.dep_usd_hr({"usd_hr": nan, "max_usd_hr": 1.5}) == 1.5
    assert ddc.dep_usd_hr({"usd_hr": -3, "max_usd_hr": 1.5}) == 1.5
    assert ddc.dep_usd_hr({"usd_hr": nan}) == ddc.price_ceiling()
    assert ddc.dep_usd_hr({"usd_hr": 0.4, "max_usd_hr": 1.5}) == 0.4 and ddc.dep_usd_hr({}) == 0.0
    spent, committed = ddc.dep_running_cost(rec(usd_hr=nan, max_usd_hr=1.0, lease_at=nan, limit_h=nan, transferred_usd=nan))
    assert 0 <= spent < 1 and committed > 0 and committed == committed


def test_a_nan_record_cannot_make_the_budget_guard_accept_an_order_it_should_refuse(jw):
    ddc.CFG["budget_usd"] = 5.0
    ddc.update_state(lambda st: st["deps"].__setitem__("9000", rec(usd_hr=float("nan"), max_usd_hr=1.0)))
    v = ddc.budget_view()
    assert v["committed"] > 0 and v["worst"] == v["worst"]
    with pytest.raises(ddc.LimitExceeded, match="budget"):
        ddc.check_spend("x", 1, 24, 3.0, "job")                 # a $72 worst case against a $5 budget


def test_can_afford_refuses_when_any_number_is_not_finite_or_the_order_is_free(jw):
    ok = {"spent": 0.0, "committed": 0.0, "worst": 0.0}
    assert ddc.can_afford(ok, 100.0, 1.0, 2, 10.0, 1.0)[0] is True
    nan = float("nan")
    assert not ddc.can_afford({**ok, "worst": nan}, 100.0, 1.0, 2, 10.0, 1.0)[0]
    assert not ddc.can_afford(ok, nan, 1.0, 2, 10.0, 1.0)[0] and not ddc.can_afford(ok, 100.0, nan, 2, 10.0, 1.0)[0]
    assert not ddc.can_afford(ok, 100.0, -1.0, 2, 10.0, 1.0)[0] and not ddc.can_afford(ok, 100.0, 1.0, 0, 10.0, 1.0)[0]


# ------------------------------------------------------------------ [21] the price inside the SDL is checked against the cap
def test_launch_lease_refuses_an_sdl_priced_above_the_cap_it_was_budgeted_at(jw):
    sdl = job_sdl(make_spec(jw.tmp))                            # priced at the $3/h cap of the spec
    with pytest.raises(ddc.DdcError, match="above the \\$1/h cap"):
        ddc.launch_lease("ddc-job-t1-0101-ab12", sdl, role="job", limit_h=2, cap_usd_hr=1.0, label="x")
    assert not jw.akash.posts and ddc.pending_creations() == {}


@pytest.mark.parametrize("edit, why", [
    (lambda t: t.replace("count: 1", "count: 2"), "one instance"),
    (lambda t: t.replace('denom: "uact"', 'denom: "uakt"'), "uact"),
    (lambda t: t + '        denom: "uact"\n        amount: 5\n', "exactly one price"),
    (lambda t: t.replace("amount: ", "amount: x"), "exactly one price"),
])
def test_launch_lease_refuses_an_sdl_it_cannot_read_exactly(jw, edit, why):
    sdl = edit(job_sdl(make_spec(jw.tmp)))
    with pytest.raises(ddc.DdcError, match=why):
        ddc.launch_lease("ddc-job-t1-0101-ab12", sdl, role="job", limit_h=2, cap_usd_hr=3.0, label="x")
    assert not jw.akash.posts


def test_the_sdls_this_code_base_generates_are_accepted(jw):
    s = make_spec(jw.tmp)
    assert sdlgen.single_price(job_sdl(s)) == specmod.uact_block(3.0)
    assert sdlgen.single_price(ddc.render_sdl("dev", "32")) >= 1
    cpu = make_spec(jw.tmp, hardware={"cpu": 2, "memory": "4Gi", "disk": "20Gi", "max_usd_hr": 0.2})
    assert sdlgen.single_price(job_sdl(cpu)) == specmod.uact_block(0.2)
    assert len(jw.akash.posts) == 0
    run_job(jw, make_spec(jw.tmp))                              # and a whole real run goes through the check
    assert len(jw.akash.posts) == 1


# ------------------------------------------------------------------ [22] heartbeat while blocked
def test_ssh_calls_beat_on_every_attempt_including_the_reconnect_loop(jw, monkeypatch):
    outcomes = [255, 255, 255, 0]
    monkeypatch.setattr(remotemod.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, outcomes.pop(0), b"ok", b"connection refused"))
    beats: list[float] = []
    r = SshRemote("203.0.113.5", 22, Path("k"), Path("kh"), jw.clock, beat=lambda: beats.append(jw.clock.time()))
    assert r.run("true") == (0, b"ok")
    assert len(beats) == 4 and beats[-1] - beats[0] >= 9      # a beat per attempt, spread over the backoff sleeps


def test_waiting_for_the_endpoint_beats(jw):
    m = Market(make_spec(jw.tmp), jw.clock, Telemetry("t1", jw.clock), History(ddc.STATE_DIR / "provider_history.json", jw.clock), lambda m: None)
    m.dseq = "1000001"
    jw.akash.deps["1000001"] = {"name": "ddc-job-t1-x", "state": "active", "created": jw.clock.time(), "image": "i", "limit": 2, "boot": 1.0, "lease_at": None, "closed_at": None}
    beats: list[int] = []
    with pytest.raises(ProvisionFailed):
        m.endpoint(60, beat=lambda: beats.append(1))
    assert len(beats) >= 10


def test_the_orphan_floor_is_documented_and_validated():
    assert ddc.JOB_ORPHAN_MIN_MINUTES == 10
    assert any("job_orphan_minutes" in p for p in ddc.config_problems({**ddc.CFG, "job_orphan_minutes": 9}))
    assert "10" in (ROOT / "docs" / "COSTS-AND-SAFETY.md").read_text(encoding="utf-8").split("job_orphan_minutes", 1)[1][:400]


# ------------------------------------------------------------------ [23] one run per job name
def test_a_second_concurrent_run_of_the_same_job_refuses_to_start_and_the_lock_is_released_after(jw):
    with ddc.FileLock("jobs/t1.lock", wait_s=0):                # what a live run of `t1` holds
        with pytest.raises(JobError, match="another `ddc job run` of 't1'"):
            run_job(jw, make_spec(jw.tmp))
        assert not jw.akash.posts and not (ddc.STATE_DIR / "jobs" / "t1.json").exists()
    run_job(jw, make_spec(jw.tmp))                              # released: a later run (or a crash-resume) works
    assert jw.runner.closed
    with pytest.raises(JobError):
        run_job(jw, make_spec(jw.tmp, step=[{"name": "work", "run": "x", "artifacts": ["nothing/*"]}]))   # a failing run releases it too
    with ddc.FileLock("jobs/t1.lock", wait_s=0):
        pass


def test_a_different_job_name_is_not_blocked(jw):
    with ddc.FileLock("jobs/other.lock", wait_s=0):
        run_job(jw, make_spec(jw.tmp))
    assert jw.runner.closed


# ------------------------------------------------------------------ [11] wheel and docs agree about examples/
def test_check_dist_expects_examples_in_the_sdist_and_not_in_the_wheel():
    spec = importlib.util.spec_from_file_location("check_dist", ROOT / "scripts" / "check_dist.py")
    cd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cd)
    assert "examples/cpu-smoke.toml" in cd.SDIST_NEED and "examples/" in cd.WHEEL_BAN
    assert not any(n.startswith("examples/") for n in cd.WHEEL_NEED)
    for doc in ("README.md", "docs/QUICKSTART.md", "docs/JOBS.md"):
        text = (ROOT / doc).read_text(encoding="utf-8")
        assert "wheel" in text and "examples/" in text, doc      # each says where the examples live for a pip install


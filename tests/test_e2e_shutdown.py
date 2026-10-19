"""End-to-end shutdown paths on the watchdog's clock and through `ddc stop`: idle stop with a verified checkpoint, the runtime limit, the user coming back,
leases the platform closed, the stop plan / --dry-run / --only / --yes, and the rule that a lease that is not ours is never touched.
Offline, deterministic (fake clock), Windows and Linux."""
import pytest

from clouddeck.engine import ddc

pytest.register_assert_rewrite("e2e_helpers")      # so assertion rewriting also applies to the helpers (they are loaded as a plugin below)
from e2e_helpers import alerts_text, assert_foreign_untouched, deleted, deps, ledger_events, live, run_ddc, watchdog  # noqa: E402

pytest_plugins = ["e2e_helpers"]       # the e2e / fleet fixtures


@pytest.fixture
def traced(fleet, monkeypatch):
    """fleet + a merged, ordered trace of checkpoint runs and DELETEs."""
    trace = []
    real_ssh, real_api = fleet.ssh, fleet.api

    def ssh(d, cmd, *a, **k):
        if "checkpoint.sh" in cmd:
            trace.append(("checkpoint", cmd.split("checkpoint.sh ")[1].split()[0]))
        return real_ssh(d, cmd, *a, **k)

    def api(method, path, *a, **k):
        if method == "DELETE":
            trace.append(("delete", path.rsplit("/", 1)[1]))
        return real_api(method, path, *a, **k)
    monkeypatch.setattr(ddc, "ssh", ssh)
    monkeypatch.setattr(ddc, "api", api)
    fleet.trace = trace
    return fleet


# ------------------------------------------------------------------------------------------------ idle stop
def jobs_tick(fleet, minutes=5):
    """A watchdog tick while a job keeps running on the workers (so only the dev box's idleness decides)."""
    fleet.clock += minutes * 60
    fleet.worker_activity = fleet.clock
    return ddc.main(["watchdog", "--once"])


def test_idle_box_is_checkpointed_verified_and_only_then_everything_is_closed_workers_first(traced):
    fleet = traced
    foreign = fleet.add_foreign()
    dev = ddc.dev_box()["dseq"]
    fleet.busy = "claude"
    assert [jobs_tick(fleet) for _ in range(6)] == [0] * 6 and len(fleet.open_dseqs()) == 4      # the user works for 30 minutes
    fleet.busy = ""
    fleet.trace.clear()
    ticks = 0
    while dev in fleet.open_dseqs() and ticks < 14:                  # then idle: 5-minute ticks until the 40-minute limit
        assert jobs_tick(fleet) == 0
        ticks += 1
    assert ticks == 8, f"stopped after {ticks * 5} idle minutes"
    assert fleet.open_dseqs() == [foreign]
    kinds = [k for k, _ in fleet.trace]
    assert ("checkpoint", "final-stop") in fleet.trace and kinds.count("delete") == 3
    assert kinds.index("checkpoint") < kinds.index("delete"), "nothing is closed before the dev box's checkpoint is verified"
    assert fleet.trace[-1] == ("delete", dev), "the dev box is closed last"
    assert "final-idle" in [r for r, _ in fleet.checkpoint_runs], "the agent took its own final checkpoint before asking"
    assert len(ledger_events("closed")) == 3 and not [a for a in ddc.recent_alerts(50) if "aborted" in a]
    assert_foreign_untouched(fleet, foreign)


def test_idle_stop_leaves_the_box_running_when_the_final_checkpoint_does_not_verify_and_says_so(fleet):
    fleet.busy = ""
    fleet.minutes(46)                                                # idle past the limit (the model's agent already asked for the shutdown)
    fleet.verify = "fail"
    assert watchdog(fleet, 1) == 1
    assert len(fleet.open_dseqs()) == 3 and not deleted(fleet), "nothing, not even the workers, was closed"
    assert "stop aborted, dev box left RUNNING" in alerts_text()
    fleet.verify = "ok"
    assert watchdog(fleet, 5) == 0 and fleet.open_dseqs() == []


def test_the_user_coming_back_while_the_final_checkpoint_runs_aborts_the_stop(fleet):
    fleet.minutes(46)
    assert fleet.agent_state()["shutdown_requested"] == "idle"       # the agent asked: 46 idle minutes
    def keystroke(host, cmd):                                        # two minutes into the checkpoint the user types
        if "checkpoint.sh final-stop" in cmd:
            fleet.minutes(2)
            fleet.activity()
    fleet.hooks.append(keystroke)
    rc = watchdog(fleet, 0)
    assert rc == 1, "the aborted stop is a failure of that pass"
    assert len(fleet.open_dseqs()) == 3 and not deleted(fleet), "neither the dev box nor its workers were closed"
    assert "activity resumed during the final checkpoint" in alerts_text()
    assert ("final-stop" in [r for r, _ in fleet.checkpoint_runs]), "the work was saved anyway"
    fleet.hooks.clear()
    fleet.busy = "claude"
    dev = ddc.dev_box()["dseq"]
    assert watchdog(fleet, 5) == 0 and dev in fleet.open_dseqs() and dev not in deleted(fleet), "back to normal while the user works (idle workers may go)"
    fleet.busy = ""
    assert [watchdog(fleet, 10) for _ in range(5)] == [0] * 5 and fleet.open_dseqs() == [], "idle again for 45 minutes: now it closes"


# ------------------------------------------------------------------------------------------------ runtime limit, platform closes
def test_the_max_runtime_path_checkpoints_and_closes_even_while_the_user_is_working(fleet):
    fleet.busy = "claude"
    assert [jobs_tick(fleet, 60) for _ in range(7)] == [0] * 7       # 7 hours of work
    assert ddc.dev_box() is not None and len(ddc.workers()) == 2
    assert jobs_tick(fleet, 45) == 0                                 # 7 h 45: the agent asked at 7 h 40 (max_runtime); the platform limit is 8 h
    assert fleet.open_dseqs() == []
    runs = [r for r, _ in fleet.checkpoint_runs]
    assert "final-max_runtime" in runs and "final-stop" in runs
    assert len(ledger_events("closed")) == 3


def test_a_lease_the_platform_closed_is_detected_booked_and_never_deleted_again(fleet, capsys):
    w1 = ddc.workers()[0]["dseq"]
    fleet.deployments[w1]["state"] = "closed"                        # runtime limit hit / provider closed it
    n_del = len(deleted(fleet))
    assert watchdog(fleet, 5) == 0
    assert deps()[w1]["status"] == "closed" and [e for e in ledger_events("closed") if e["dseq"] == w1][0]["by"] == "platform"
    assert len(live("worker")) == 1 and ddc.dev_box() is not None and len(deleted(fleet)) == n_del, "no DELETE for a lease that is already closed"
    assert "closed by the platform" in capsys.readouterr().out
    assert run_ddc(capsys, "scale", "2", "--yes")[0] == 0 and len(live("worker")) == 2, "the slot is free again"


def test_budget_exhaustion_stops_everything_even_though_the_user_is_working(fleet):
    ddc.CFG["budget_usd"] = 1.0
    fleet.busy = "claude"
    ticks = 0
    while fleet.open_dseqs() and ticks < 40:
        watchdog(fleet, 15)
        ticks += 1
    assert fleet.open_dseqs() == [] and "budget nearly exhausted" in alerts_text() and ticks < 40
    assert 0.4 < ddc.budget_view()["spent"] < 1.5, "stopped near the budget, not long after it"


def test_a_watchdog_tick_while_another_command_holds_the_lock_changes_nothing(fleet, capsys, monkeypatch):
    clock = iter(range(0, 10**6, 3))
    monkeypatch.setattr(ddc, "_mono", lambda: next(clock))           # the 5 s lock wait elapses at once instead of in real time
    with ddc.FileLock():
        fleet.minutes(60)
        assert watchdog(fleet, 0) == 0
    assert len(fleet.open_dseqs()) == 3
    assert "another ddc command holds the lock" in capsys.readouterr().out


# ------------------------------------------------------------------------------------------------ `ddc stop`
def test_stop_dry_run_lists_exactly_what_would_close_and_changes_nothing(fleet, capsys):
    foreign = fleet.add_foreign()
    other_pc = fleet.add_foreign("1000000000888", "ddc-w05-32-01011200-abcd")                 # ddc-named, but not in OUR state
    ddc.update_dep("9000000000001", ours=True, name="ddc-box-16-01011200-abcd", role="box", status="ready", host="exp.example", port=30999, usd_hr=0.1, limit_h=2)
    fleet.deployments["9000000000001"] = {"name": "ddc-box-16-01011200-abcd", "owner": "akash1fakeowner", "state": "active", "provider": "akash1prova",
                                          "t0": fleet.clock, "leased": True, "host": "exp.example"}
    n_calls, n_ssh = len(fleet.calls), len(fleet.ssh_log)
    rc, out = run_ddc(capsys, "stop", "--dry-run")
    assert rc == 0 and "Will close 3 lease(s)" in out
    assert out.count("workers hold no data") == 2 and "verified first" in out
    left = out.split("Left running")[1]
    assert "9000000000001" in left and "--only 9000000000001" in left and foreign not in out and other_pc not in out
    assert not [c for m, c in fleet.calls[n_calls:] if m in ("DELETE", "POST")] and not fleet.commands("checkpoint.sh")
    assert len(fleet.ssh_log) - n_ssh <= 1, "at most the read of the agent state"


def test_stop_needs_a_yes_without_a_terminal_and_a_typed_yes_with_one(fleet, capsys, monkeypatch):
    rc, out = run_ddc(capsys, "stop")
    assert rc == 1 and "--yes" in out and len(fleet.open_dseqs()) == 3
    monkeypatch.setattr("sys.stdin", type("TTY", (), {"isatty": lambda self: True})())
    for answer in ("", "y", "no", "yes please"):
        monkeypatch.setattr("builtins.input", lambda prompt="", a=answer: a)
        rc, out = run_ddc(capsys, "stop")
        assert rc == 1 and "aborted" in out and len(fleet.open_dseqs()) == 3 and not deleted(fleet), repr(answer)
    monkeypatch.setattr("builtins.input", lambda prompt="": "YES")
    assert run_ddc(capsys, "stop")[0] == 0 and fleet.open_dseqs() == []


def test_stop_only_closes_what_is_named_and_a_worker_needs_no_checkpoint(fleet, capsys):
    w2 = [w for w in ddc.workers() if w["idx"] == 2][0]
    rc, out = run_ddc(capsys, "stop", "--only", "w02", "--yes")
    assert rc == 0 and deleted(fleet) == [w2["dseq"]] and not fleet.commands("checkpoint.sh")
    rc, out = run_ddc(capsys, "stop", "--only", w2["name"], "--yes")
    assert rc == 1 and "matches no active lease" in out
    rc, out = run_ddc(capsys, "stop", "--only", "w09", "--yes")
    assert rc == 1 and "matches no active lease" in out and len(fleet.open_dseqs()) == 2


def test_stop_workers_only_keeps_the_dev_box_and_asks_for_no_checkpoint(fleet, capsys):
    rc, out = run_ddc(capsys, "stop", "--workers-only", "--yes")
    assert rc == 0 and len(live("worker")) == 0 and ddc.dev_box() is not None and not fleet.commands("checkpoint.sh")


def test_stop_yes_checkpoints_first_then_closes_workers_then_the_dev_box_and_leaves_what_is_not_ours(traced, capsys):
    fleet = traced
    foreign = fleet.add_foreign()
    other_pc = fleet.add_foreign("1000000000888", "ddc-w05-32-01011200-abcd")
    dev = ddc.dev_box()["dseq"]
    rc, out = run_ddc(capsys, "stop", "--yes")
    assert rc == 0, out
    assert fleet.trace[0] == ("checkpoint", "final-stop") and fleet.trace[-1] == ("delete", dev) and len(fleet.trace) == 4
    assert sorted(fleet.open_dseqs()) == sorted([foreign, other_pc]), "neither the foreign deployment nor a ddc-named lease of another machine was touched"
    assert_foreign_untouched(fleet, foreign)
    assert_foreign_untouched(fleet, other_pc)
    assert "billing stopped" in out and "No ddc leases running" in run_ddc(capsys, "status")[1]


def test_stop_with_one_close_failing_closes_the_rest_exits_non_zero_and_a_rerun_finishes(fleet, capsys):
    fleet.fail_next("DELETE /v1/deployments", "reject", times=1)
    rc, out = run_ddc(capsys, "stop", "--yes")
    assert rc == 1 and "some leases were NOT closed" in out and "may still be billing" in out
    assert len(fleet.open_dseqs()) == 1 and len(live()) == 1, "exactly the lease whose close failed is still open and still recorded as live"
    rc, out = run_ddc(capsys, "stop", "--yes")
    assert rc == 0 and fleet.open_dseqs() == []


def test_stop_does_nothing_at_all_when_the_dev_checkpoint_fails_not_even_closing_workers(fleet, capsys):
    fleet.checkpoint = "fail"
    rc, out = run_ddc(capsys, "stop", "--yes")
    assert rc == 1 and "stop aborted, nothing was closed" in out and len(fleet.open_dseqs()) == 3 and not deleted(fleet)
    fleet.checkpoint = "ok"
    assert run_ddc(capsys, "stop", "--yes")[0] == 0 and fleet.open_dseqs() == []


def test_force_alone_never_destroys_a_workspace_it_could_not_save_force_with_discard_does_and_says_so(fleet, capsys):
    fleet.checkpoint = "fail"
    rc, out = run_ddc(capsys, "stop", "--yes", "--force")
    assert rc == 1 and "--discard-workspace" in out and len(fleet.open_dseqs()) == 3 and not deleted(fleet)
    rc, out = run_ddc(capsys, "stop", "--yes", "--discard-workspace")           # discarding is only for a box that has checkpoints off; --force is what overrides a failure
    assert rc == 1 and len(fleet.open_dseqs()) == 3 and not deleted(fleet)
    rc, out = run_ddc(capsys, "stop", "--yes", "--force", "--discard-workspace")
    assert rc == 0 and fleet.open_dseqs() == [] and "AUDIT FORCED CLOSE" in out, out


def test_a_workspace_that_shrank_is_refused_by_the_box_and_allow_shrink_is_passed_through(fleet, capsys):
    fleet.checkpoint = "refuse"
    rc, out = run_ddc(capsys, "stop", "--yes")
    assert rc == 1 and "the dev box refused the checkpoint" in out and "--allow-shrink" in out and len(fleet.open_dseqs()) == 3
    fleet.checkpoint = "ok"
    assert run_ddc(capsys, "stop", "--yes", "--allow-shrink")[0] == 0
    assert any("checkpoint.sh final-stop --allow-shrink" in c for _, c in fleet.ssh_log)


# ------------------------------------------------------------------------------------------------ ownership, across everything
def test_a_lease_that_is_not_ours_survives_start_scale_idle_stop_and_a_budget_stop(e2e, capsys):
    foreign = e2e.add_foreign()
    other_pc = e2e.add_foreign("1000000000888", "ddc-w05-32-01011200-abcd")
    stranger_owner = e2e.add_foreign("1000000000999", "ddc-dev-32-01011200-ffff", owner="akash1someoneelse")
    assert run_ddc(capsys, "start", "-w", "2")[0] == 0
    assert run_ddc(capsys, "scale", "1", "--yes")[0] == 0
    e2e.busy = "claude"
    watchdog(e2e, 15)
    e2e.busy = ""
    for _ in range(10):                                               # idle stop
        watchdog(e2e, 5)
    assert sorted(e2e.open_dseqs()) == sorted([foreign, other_pc, stranger_owner])
    assert run_ddc(capsys, "start")[0] == 0
    ddc.CFG["budget_usd"] = 0.2
    e2e.busy = "claude"
    for _ in range(5):                                                # budget stop
        watchdog(e2e, 30)
    assert sorted(e2e.open_dseqs()) == sorted([foreign, other_pc, stranger_owner])
    for d in (foreign, other_pc, stranger_owner):
        assert_foreign_untouched(e2e, d)
    assert set(deleted(e2e)) <= {k for k in e2e.deployments if k not in (foreign, other_pc, stranger_owner)}

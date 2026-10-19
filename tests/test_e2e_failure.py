"""End-to-end failure recovery through the real CLI: uncertain API writes, damaged state / ledger, watchdog crash loops, ssh outages and how they are
told apart, and rollback of partial provisioning.  Offline, deterministic (fake clock), Windows and Linux."""
import json
import subprocess

import pytest

from clouddeck.engine import ddc

pytest.register_assert_rewrite("e2e_helpers")      # so assertion rewriting also applies to the helpers (they are loaded as a plugin below)
from e2e_helpers import alerts_text, assert_foreign_untouched, created, deleted, deps, ledger_events, live, run_ddc, watchdog  # noqa: E402

pytest_plugins = ["e2e_helpers"]       # the e2e / fleet fixtures


# ------------------------------------------------------------------------------------------------ ApiUncertain: a POST that timed out
def test_a_create_that_timed_out_after_the_server_made_it_is_found_closed_and_never_duplicated(e2e, capsys):
    e2e.fail_next("POST /v1/deployments", "timeout_after")
    rc, out = run_ddc(capsys, "start")
    assert rc == 0, out                                             # the lost deployment was closed, the start went on with a fresh one
    assert "creation outcome unknown" in out and "NOT retrying" in out and "was found and closed again" in out
    assert created(e2e) == 2 and len(e2e.open_dseqs()) == 1, "exactly one live box, the lost one is closed"
    lost = next(k for k, v in e2e.deployments.items() if v["state"] == "closed")
    assert deleted(e2e) == [lost] and deps()[lost]["status"] == "closed"
    assert ledger_events("created")[0].get("adopted") is True
    assert ddc.load_state().get("pending") in (None, {}) and ddc.dev_box()["dseq"] != lost


def test_a_create_that_never_reached_the_server_stays_pending_counts_in_the_budget_and_expires_by_reconcile(e2e, capsys):
    e2e.fail_next("POST /v1/deployments", "timeout_before")
    rc, out = run_ddc(capsys, "start")
    assert rc == 1 and "outcome unknown" in out and "ddc reconcile" in out
    assert e2e.deployments == {} and len(ddc.load_state()["pending"]) == 1
    assert ddc.budget_view()["committed"] > 0, "an unknown creation is counted at its price cap until it is resolved"
    rc, out = run_ddc(capsys, "status")
    assert rc == 0 and "PENDING creation" in out
    rc, out = run_ddc(capsys, "reconcile")                           # too early to drop: it may still show up
    assert rc == 0 and len(ddc.load_state()["pending"]) == 1 and "dropped" not in out
    e2e.minutes(61)
    rc, out = run_ddc(capsys, "reconcile")
    assert rc == 0 and "matches no deployment" in out and not ddc.load_state().get("pending")
    assert ddc.budget_view()["committed"] == 0
    assert "PENDING" not in run_ddc(capsys, "status")[1]


def test_a_deployment_that_shows_up_later_is_adopted_and_closed_by_reconcile_and_nothing_else_is_touched(e2e, capsys):
    foreign = e2e.add_foreign()
    e2e.fail_next("POST /v1/deployments", "timeout_before")
    assert run_ddc(capsys, "start")[0] == 1
    name = next(iter(ddc.load_state()["pending"]))
    late = e2e.add_foreign("1000000000777", name, owner="akash1fakeowner")       # the request WAS executed after all
    e2e.deployments[late].pop("foreign")
    rc, out = run_ddc(capsys, "reconcile")
    assert rc == 0 and f"pending {name} was created as dseq {late}" in out and f"closed {late}" in out, out
    assert deleted(e2e) == [late] and not ddc.load_state().get("pending") and deps()[late]["status"] == "closed"
    assert_foreign_untouched(e2e, foreign)


def test_a_lost_delete_is_checked_before_it_is_repeated_and_repeated_when_it_did_not_happen(e2e, capsys):
    assert run_ddc(capsys, "start", "-w", "2")[0] == 0
    e2e.fail_next("DELETE /v1/deployments", "timeout_after")         # executed, the reply was lost
    assert run_ddc(capsys, "scale", "1", "--yes")[0] == 0
    assert len(deleted(e2e)) == 1 and len(ddc.workers()) == 1, "the state was read first: it was already closed, no second DELETE"
    e2e.fail_next("DELETE /v1/deployments", "timeout_before")        # never arrived
    assert run_ddc(capsys, "scale", "0", "--yes")[0] == 0
    assert len(deleted(e2e)) == 3 and ddc.workers() == [] and len(e2e.open_dseqs()) == 1, "never executed: sent again"


def test_an_api_that_answers_with_empty_bodies_never_makes_a_running_lease_look_closed(e2e, capsys):
    assert run_ddc(capsys, "start")[0] == 0
    e2e.fail_next("GET /v1/deployments/", "empty", times=1)           # `{}`: no deployment state in it
    rc, out = run_ddc(capsys, "status")
    assert rc == 0 and "no usable answer" in out and len(live()) == 1 and live()[0]["status"] == "ready", "an unknown answer is not 'closed'"
    e2e.fail_next("GET /v1/deployments/", "error", times=1)           # the API errors out
    assert run_ddc(capsys, "status")[0] == 0 and len(live()) == 1 and ledger_events("closed") == []
    e2e.fail_next("GET /v1/deployments/", "empty", times=1)
    assert run_ddc(capsys, "stop", "--yes")[0] == 0 and e2e.open_dseqs() == [], "stop sends the DELETE anyway: an empty answer never means 'already closed'"
    assert len(deleted(e2e)) == 1


# ------------------------------------------------------------------------------------------------ damaged state.json
def corrupt_state():
    ddc.STATE_FILE.write_text('{"deps": {"100000000', encoding="utf-8")     # a torn write


def test_a_damaged_state_file_blocks_new_leases_but_status_and_stop_still_work_and_reconcile_repairs_it(e2e, capsys):
    foreign = e2e.add_foreign()
    assert run_ddc(capsys, "start", "-w", "1")[0] == 0
    dev, worker = ddc.dev_box()["dseq"], ddc.workers()[0]["dseq"]
    n = created(e2e)
    corrupt_state()
    rc, out = run_ddc(capsys, "status")
    assert rc == 0 and "STATE PROBLEM" in out and dev in out and f"{foreign}" in out and "(not ours)" in out
    for argv in (("start",), ("scale", "3", "--yes")):
        rc, out = run_ddc(capsys, *argv)
        assert rc == 1 and "reconcile" in out and created(e2e) == n, f"{argv} must not create anything on a damaged state"
    rc, out = run_ddc(capsys, "stop", "--dry-run")                    # the plan comes from a read-only listing of ddc-* deployments
    assert rc == 0 and dev in out and worker in out and foreign not in out
    rc, out = run_ddc(capsys, "reconcile")
    assert rc == 0 and "Nothing was changed" in out and "--restore-backup" in out
    assert len(e2e.open_dseqs()) == 3
    rc, out = run_ddc(capsys, "reconcile", "--restore-backup")
    assert rc == 0 and "restored state.json" in out
    rc, out = run_ddc(capsys, "status")
    assert rc == 0 and "STATE PROBLEM" not in out and len(live()) == 2
    rc, out = run_ddc(capsys, "stop", "--yes")
    assert rc == 0 and e2e.open_dseqs() == [foreign]
    assert sorted(deleted(e2e)) == sorted([dev, worker])


def test_without_a_usable_backup_adopt_rebuilds_the_records_from_the_account_and_stop_can_close_them(e2e, capsys):
    foreign = e2e.add_foreign()
    assert run_ddc(capsys, "start")[0] == 0
    dev = ddc.dev_box()["dseq"]
    ddc.STATE_FILE.write_text("garbage", encoding="utf-8")
    ddc.STATE_FILE.with_name("state.json.bak").write_text("also garbage", encoding="utf-8")
    assert run_ddc(capsys, "status")[0] == 0
    rc, out = run_ddc(capsys, "reconcile", "--adopt")                 # recording leases as ours is a decision: it asks first
    assert rc == 1 and "--yes" in out
    rc, out = run_ddc(capsys, "reconcile", "--adopt", "--yes")
    assert rc == 0 and f"adopted {dev}" in out and "state problem cleared" in out, out
    assert foreign not in deps() and deps()[dev]["adopted"] is True
    assert run_ddc(capsys, "stop", "--yes")[0] == 0
    assert e2e.open_dseqs() == [foreign] and deleted(e2e) == [dev]


def test_the_watchdog_with_a_damaged_state_file_fails_loudly_and_after_three_failures_makes_a_safe_stop(e2e, capsys):
    foreign = e2e.add_foreign()
    assert run_ddc(capsys, "start")[0] == 0
    e2e.activity()
    corrupt_state()
    assert watchdog(e2e) == 1 and "the watchdog is failing (1 in a row)" in alerts_text()
    assert watchdog(e2e) == 1 and alerts_text().count("the watchdog is failing") == 1, "the alert is throttled, the failure is still counted"
    assert len(e2e.open_dseqs()) == 2
    assert watchdog(e2e) == 1                                        # the third failure in a row: a safe (checkpointed, verified) stop
    assert e2e.open_dseqs() == [foreign] and any(r == "final-stop" for r, _ in e2e.checkpoint_runs)
    assert "safe stop after repeated failures completed" in capsys.readouterr().out


# ------------------------------------------------------------------------------------------------ watchdog crash loops
def test_a_crashing_watchdog_alerts_once_per_half_hour_retries_the_safe_stop_every_sixth_failure_and_says_when_the_box_is_still_running(e2e, monkeypatch, capsys):
    assert run_ddc(capsys, "start")[0] == 0
    e2e.activity()
    e2e.verify = "fail"                                              # nothing can be proven restorable: the safe stop must keep the box
    monkeypatch.setattr(ddc, "dev_box", lambda: (_ for _ in ()).throw(RuntimeError("boom: unexpected bug")))
    rcs = [watchdog(e2e, 5) for _ in range(9)]
    assert rcs == [1] * 9
    txt = alerts_text()
    assert txt.count("the watchdog is failing") == 2, "one alert per 30 minutes: tick 1 and tick 7"
    assert txt.count("STILL RUNNING") == 1, "the safe stop (3rd failure) did not complete: said once per hour"
    assert len(e2e.open_dseqs()) == 1 and not deleted(e2e)
    attempts = sum(1 for r, _ in e2e.checkpoint_runs if r == "final-stop")
    assert attempts == 2, "the safe stop was tried at the 3rd and the 9th failure only"
    e2e.verify = "ok"
    monkeypatch.undo()                                               # the bug is fixed: the next pass is healthy and resets the counter
    assert watchdog(e2e, 5) == 0 and ddc.read_json(ddc.STATE_DIR / "watchdog.json", {})["fails"] == 0


def test_a_flapping_watchdog_resets_its_failure_count_on_every_good_pass(e2e, monkeypatch, capsys):
    assert run_ddc(capsys, "start")[0] == 0
    e2e.busy = "claude"
    real = ddc.dev_box
    for _ in range(4):
        monkeypatch.setattr(ddc, "dev_box", lambda: (_ for _ in ()).throw(RuntimeError("flaky")))
        assert watchdog(e2e) == 1 and watchdog(e2e) == 1
        monkeypatch.setattr(ddc, "dev_box", real)
        assert watchdog(e2e) == 0
    assert len(e2e.open_dseqs()) == 1 and not e2e.checkpoint_runs, "never three in a row: the safe stop never fires"


# ------------------------------------------------------------------------------------------------ ssh outages, told apart
def test_a_network_outage_is_tolerated_for_thirty_minutes_recovery_resets_it_and_a_long_one_closes_the_box(fleet):
    foreign = fleet.add_foreign()
    host = ddc.dev_box()["host"]
    fleet.busy = "procs"                                             # (what the box was doing; keeps the idle logic out of this test)
    fleet.unreachable.add(host)
    assert [watchdog(fleet, 10), watchdog(fleet, 10)] == [0, 0] and ddc.load_state()["dev_unreachable_since"]
    assert len(fleet.open_dseqs()) == 4, "20 minutes of outage: nothing is closed, the workspace may be fine"
    fleet.unreachable.clear()
    assert watchdog(fleet, 5) == 0 and ddc.load_state()["dev_unreachable_since"] is None, "recovery resets the clock"
    fleet.unreachable.add(host)
    assert [watchdog(fleet, 10) for _ in range(4)] == [0, 0, 0, 0]   # the outage began at the first of these ticks: 30 minutes is not yet "over 30"
    assert len(fleet.open_dseqs()) == 4
    assert watchdog(fleet, 10) == 0                                  # 40 minutes in a row
    assert fleet.open_dseqs() == [foreign], "dev box and workers closed (the last checkpoint is in the state repo); the foreign one untouched"
    assert "unreachable for 30 min" in alerts_text()


@pytest.mark.parametrize("message", ["Permission denied (publickey).", "Load key \"/keys/access\": invalid format", "REMOTE HOST IDENTIFICATION HAS CHANGED!",
                                     "Host key verification failed."])
def test_a_problem_on_this_pc_is_never_mistaken_for_an_unreachable_box_and_never_closes_it(e2e, capsys, message):
    assert run_ddc(capsys, "start")[0] == 0
    e2e.activity()
    e2e.fail_ssh("", message, times=1000, code=255)                  # every ssh from this PC fails the same way, for 100 minutes
    for _ in range(20):
        assert watchdog(e2e, 5) == 1
    txt = alerts_text()
    assert "NOT being closed" in txt and txt.count("the watchdog cannot check the dev box") == 2, "alerted (hourly), not silently ignored"
    assert len(e2e.open_dseqs()) == 1 and not deleted(e2e) and ddc.load_state().get("dev_unreachable_since") is None
    assert "unreachable for 30 min" not in txt and "STILL RUNNING" in txt


def test_a_command_timeout_counts_as_unreachable_and_an_unreadable_reply_does_not(e2e, capsys):
    assert run_ddc(capsys, "start")[0] == 0
    e2e.activity()
    e2e.fail_ssh("cat /run/ddc/state.json", "", times=1, exc=subprocess.TimeoutExpired("ssh", 40))
    assert watchdog(e2e) == 0 and ddc.load_state()["dev_unreachable_since"], "a timeout is an unreachable box"
    e2e.reply_with("cat /run/ddc/state.json", "<html>502 Bad Gateway</html>")
    assert watchdog(e2e) == 1 and ddc.load_state().get("dev_unreachable_since") is None, "garbage from a reachable box is a failure to alert on, not an outage"
    assert watchdog(e2e) == 0 and len(e2e.open_dseqs()) == 1


# ------------------------------------------------------------------------------------------------ ledger
def test_a_torn_final_ledger_line_is_repaired_by_the_next_write_and_never_loses_spend(e2e, capsys):
    assert run_ddc(capsys, "start", "-w", "1")[0] == 0
    with ddc.LEDGER.open("a", encoding="utf-8") as f:
        f.write('{"event": "clos')                                    # power loss in the middle of a write
    rc, out = run_ddc(capsys, "status")
    assert rc == 0
    e2e.minutes(30)
    rc, out = run_ddc(capsys, "stop", "--yes")
    assert rc == 0 and e2e.open_dseqs() == [], out
    rows, bad = ddc.ledger_read()
    assert bad == [] and [r["event"] for r in rows].count("closed") == 2
    assert list(ddc.STATE_DIR.glob("ledger.jsonl.torn-*")), "the fragment was kept aside, not deleted"
    assert ddc.budget_view()["spent"] > 0


def test_a_corrupt_ledger_line_blocks_new_leases_but_never_status_stop_or_the_watchdog(fleet, capsys):
    lines = ddc.LEDGER.read_text(encoding="utf-8").splitlines()
    ddc.LEDGER.write_text("\n".join([lines[0], "{this is not json", *lines[1:]]) + "\n", encoding="utf-8")
    n = created(fleet)
    rc, out = run_ddc(capsys, "scale", "3", "--yes")
    assert rc == 1 and "unreadable lines" in out and created(fleet) == n
    assert run_ddc(capsys, "status")[0] == 0 and watchdog(fleet) == 0
    rc, out = run_ddc(capsys, "stop", "--yes")
    assert rc == 0 and fleet.open_dseqs() == []


# ------------------------------------------------------------------------------------------------ partial provisioning
def test_a_start_that_fails_after_the_lease_closes_only_what_it_created(e2e, capsys):
    foreign = e2e.add_foreign()
    other_machine = e2e.add_foreign("1000000000888", "ddc-w05-32-01011200-abcd")        # a ddc-named lease that is NOT in our state (another PC)
    e2e.fail_next("POST /v1/leases", "reject", times=2)               # both attempts: the provider refuses the lease
    rc, out = run_ddc(capsys, "start")
    assert rc == 1 and "rejected" in out
    assert sorted(e2e.open_dseqs()) == sorted([foreign, other_machine]), "the half-created deployments were closed, nothing else"
    assert set(deleted(e2e)) == {k for k in e2e.deployments if k not in (foreign, other_machine)}
    assert_foreign_untouched(e2e, foreign)
    assert_foreign_untouched(e2e, other_machine)
    assert json.dumps(ddc.load_state()).count(other_machine) == 0


def test_when_closing_the_failed_lease_also_fails_the_error_says_it_may_still_be_billing(e2e, capsys):
    e2e.unreachable.update(e2e.providers.values())                  # ssh never comes up ...
    e2e.fail_next("DELETE /v1/deployments", "reject", times=40)       # ... and the API refuses to close the lease
    rc, out = run_ddc(capsys, "start")
    assert rc == 1 and "may still be billing" in out and "ddc stop" in out
    assert len(e2e.open_dseqs()) >= 1 and all(d["status"] != "closed" for d in live()), "recorded as live, so `ddc stop` and the watchdog can still find it"
    e2e._fail.clear()
    rc, out = run_ddc(capsys, "stop", "--yes", "--discard-workspace")
    assert rc == 0 and e2e.open_dseqs() == []

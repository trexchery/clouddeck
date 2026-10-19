"""Lease ownership and the stop / scale-down / heal safety rules: only our own, recorded, ddc-named leases are ever closed; the plan is
shown first; experiments are left alone unless named; one failing close never strands the others; unsaved work is never destroyed
silently."""
import io
import re
import types

import pytest

from clouddeck.engine import ddc

ARGS = types.SimpleNamespace(profile="32", workers=0, worker_profile=None)


def deletes(fake):
    return [c for c in fake.calls if c[0] == "DELETE"]


@pytest.fixture
def fleet(fake):
    """A dev box and two workers, all provisioned through the real code on the fake."""
    ddc.cmd_start(ARGS)
    ddc.cmd_scale(types.SimpleNamespace(n=2, profile="32"))
    assert len(fake.open_dseqs()) == 3
    fake.fleet = {"dev": ddc.dev_box()["dseq"], "workers": [w["dseq"] for w in ddc.workers()]}
    return fake


def stop(*argv):
    return ddc.main(["stop", *argv])


def everything(fleet):
    return [fleet.fleet["dev"], *fleet.fleet["workers"]]


# ------------------------------------------------------------------ ownership
def own(dseq, **kw):
    ddc.write_json(ddc.STATE_FILE, {"deps": {dseq: {"ours": True, "name": "ddc-dev-32-x", "role": "dev", "status": "ready", "host": "h.example", "port": 22,
                                                    "usd_hr": 0.3, "lease_at": ddc.now(), "limit_h": 8, **kw}}})


def test_a_foreign_deployment_is_never_closed_whatever_the_state_says(fake):
    foreign = fake.add_foreign("1000000000001", "other-service+db+web")          # a real, unrelated deployment on the same account
    with pytest.raises(ddc.DdcError, match="not created by ddc"):                # not recorded at all
        ddc.close_box(foreign)
    own(foreign, name="ddc-dev-32-x")                                           # recorded, but the console says it is something else
    with pytest.raises(ddc.DdcError, match=re.escape("console name 'other-service+db+web' is not ours")):
        ddc.close_box(foreign)
    ddc.write_json(ddc.STATE_FILE, {"deps": {foreign: {"ours": False, "name": "ddc-dev-32-x", "status": "ready"}}})
    with pytest.raises(ddc.DdcError, match="not created by ddc"):               # recorded as not ours
        ddc.close_box(foreign)
    ddc.write_json(ddc.STATE_FILE, {"deps": {foreign: {"ours": True, "name": "prod-box", "status": "ready"}}})
    with pytest.raises(ddc.DdcError, match="recorded name"):                    # our own record has a non-ddc name
        ddc.close_box(foreign)
    assert not deletes(fake) and fake.open_dseqs() == [foreign]


def test_a_lease_of_another_account_is_refused_even_with_a_ddc_name(fake):
    box = ddc.create_box("worker", "32", 1, {}, limit_h=4)
    assert ddc.load_state()["deps"][box["dseq"]]["owner"] == "akash1fakeowner"
    other = "1000000000555"
    fake.add_foreign(other, "ddc-w02-32-copycat", owner="akash1someoneelse")
    ddc.update_dep(other, ours=True, name="ddc-w02-32-copycat", role="worker", status="ready", limit_h=4)
    with pytest.raises(ddc.DdcError, match="belongs to account akash1someoneelse"):
        ddc.close_box(other)
    assert not deletes(fake) and other in fake.open_dseqs()


def test_an_empty_api_answer_is_not_read_as_already_closed_the_delete_is_still_sent(fake):
    box = ddc.create_box("worker", "32", 1, {}, limit_h=4)
    fake.fail_next(f"GET /v1/deployments/{box['dseq']}", "empty")
    ddc.close_box(box["dseq"])
    assert deletes(fake) == [("DELETE", f"/v1/deployments/{box['dseq']}")] and not fake.open_dseqs()
    # ...and without details nothing can be verified against the console, so a record without a name is refused
    ddc.update_dep("9", ours=True, status="ready")
    fake.fail_next("GET /v1/deployments/9", "empty")
    with pytest.raises(ddc.DdcError, match="no name to verify"):
        ddc.close_box("9")


def test_delete_that_may_or_may_not_have_gone_through_is_checked_before_it_is_repeated(fake):
    box = ddc.create_box("worker", "32", 1, {}, limit_h=4)
    fake.fail_next("DELETE /v1/deployments/", "timeout_after")                   # it was executed, the reply was lost
    ddc.close_box(box["dseq"])
    assert len(deletes(fake)) == 1 and ddc.load_state()["deps"][box["dseq"]]["status"] == "closed"
    box2 = ddc.create_box("worker", "32", 2, {}, limit_h=4)
    fake.fail_next("DELETE /v1/deployments/", "timeout_before")                  # it was NOT executed: the second DELETE is sent
    ddc.close_box(box2["dseq"])
    assert len(deletes(fake)) == 3 and not fake.open_dseqs()


# ------------------------------------------------------------------ stop: plan, confirmation, --only, experiments
def test_stop_lists_exactly_what_it_will_close_and_needs_confirmation(fleet, capsys, monkeypatch):
    fleet.add_foreign()
    ddc.update_dep("555", ours=True, name="ddc-box-32-hand", role="box", status="ready", usd_hr=0.2, lease_at=fleet.clock, host="x.example", port=22)
    fleet.deployments["555"] = {"name": "ddc-box-32-hand", "owner": "akash1fakeowner", "state": "active", "t0": fleet.clock, "leased": True, "provider": "akash1provA"}
    assert stop("--dry-run") == 0
    out = capsys.readouterr().out
    assert "Will close 3 lease(s)" in out and fleet.fleet["dev"] in out and all(w in out for w in fleet.fleet["workers"])
    assert "ddc-dev-32-" in out and "$0.300/h" in out and "workers hold no data" in out and "a final one is taken and verified first" in out
    assert "Left running" in out and "555" in out and "--only 555" in out and "1000000000001" not in out.split("Left running")[0]
    assert not deletes(fleet)                                                   # dry run: nothing closed

    monkeypatch.setattr(ddc.sys, "stdin", io.StringIO(""))                       # no terminal and no --yes: refuses instead of closing
    assert stop() == 1 and "pass --yes" in capsys.readouterr().out and not deletes(fleet)

    tty = types.SimpleNamespace(isatty=lambda: True)
    monkeypatch.setattr(ddc.sys, "stdin", tty)
    monkeypatch.setattr("builtins.input", lambda prompt="": "no")
    assert stop() == 1 and not deletes(fleet)
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")
    assert stop() == 0
    assert sorted(fleet.open_dseqs()) == sorted(["1000000000001", "555"])       # the experiment and the foreign lease are still there
    assert ddc.load_state()["deps"]["555"]["status"] == "ready"


def test_stop_only_closes_what_is_named_and_can_name_an_experiment(fleet, capsys):
    w1, w2 = fleet.fleet["workers"]
    assert stop("--only", w1, "--yes") == 0 and sorted(fleet.open_dseqs()) == sorted([fleet.fleet["dev"], w2])      # no dev checkpoint for a worker
    assert stop("--only", "w02", "--yes") == 0 and fleet.open_dseqs() == [fleet.fleet["dev"]]
    assert stop("--only", "123456", "--yes") == 1 and "matches no active lease" in capsys.readouterr().out
    assert stop("--only", "1000000000001", "--yes") == 1 and fleet.open_dseqs() == [fleet.fleet["dev"]]            # a foreign dseq is not even listed
    assert not [d for d in deletes(fleet) if d[1].endswith("1000000000001")]


def test_one_failing_close_does_not_strand_the_other_leases_and_the_exit_status_says_so(fleet, capsys):
    w1, w2 = fleet.fleet["workers"]
    fleet.fail_next(f"DELETE /v1/deployments/{w1}", "error")
    assert stop("--yes") == 1
    out = capsys.readouterr().out
    assert w1 in out and "NOT closed" in out
    assert fleet.open_dseqs() == [w1]                                            # the dev box and the other worker were still closed
    assert ddc.load_state()["deps"][w1]["status"] == "ready"                     # and the stuck one is still tracked (the watchdog retries)


def test_a_record_that_is_not_ours_is_skipped_and_reported_while_the_real_leases_are_closed(fleet, capsys):
    ddc.update_dep("77", ours=False, name="ddc-dev-32-stub", role="worker", status="ready")      # a stub without ours
    assert stop("--yes") == 1
    assert "77" in capsys.readouterr().out and not fleet.open_dseqs() and not [d for d in deletes(fleet) if d[1].endswith("/77")]


# ------------------------------------------------------------------ stop and unsaved work (checkpoints)
def dev_commands(fake):
    return [c for h, c in fake.ssh_log if "checkpoint.sh" in c]


def test_stop_with_checkpoints_disabled_refuses_the_dev_box_unless_discard_workspace(fleet, capsys):
    ddc.CFG["state_repo"] = ""
    assert stop("--yes") == 1
    out = capsys.readouterr().out
    assert "checkpoints are disabled" in out and "--discard-workspace" in out and "left RUNNING" in out
    assert sorted(fleet.open_dseqs()) == sorted(everything(fleet))               # nothing is closed unless the dev box can be closed too
    assert stop("--force", "--yes") == 1 and len(fleet.open_dseqs()) == 3        # --force does not mean "destroy my work"
    assert not dev_commands(fleet)
    assert stop("--discard-workspace", "--yes") == 0 and not fleet.open_dseqs()


def test_stop_plan_warns_that_the_workspace_will_be_destroyed(fleet, capsys):
    ddc.CFG["state_repo"] = ""
    stop("--dry-run")
    assert "CHECKPOINTS DISABLED" in capsys.readouterr().out and fleet.open_dseqs()


def test_stop_with_a_failing_checkpoint_leaves_the_dev_box_running_and_alerts(fleet, capsys):
    fleet.checkpoint = "fail"
    assert stop("--yes") == 1
    out = capsys.readouterr().out
    assert "left RUNNING" in out and "nothing was closed" in out and "Repository not found" in out
    assert sorted(fleet.open_dseqs()) == sorted(everything(fleet))               # the workers are not closed behind the back of a dev box that stays
    assert any("final checkpoint did not verify" in a for a in ddc.recent_alerts())


def test_stop_with_a_checkpoint_that_does_not_verify_here_keeps_the_box(fleet, capsys):
    fleet.verify = "fail"
    assert stop("--yes") == 1 and "sha256 mismatch" in capsys.readouterr().out and sorted(fleet.open_dseqs()) == sorted(everything(fleet))


def test_force_alone_does_not_close_without_a_verified_checkpoint_force_with_discard_does_and_leaves_an_audit_line(fleet, capsys):
    fleet.checkpoint = "fail"
    assert stop("--force", "--yes") == 1 and len(fleet.open_dseqs()) == 3           # losing the workspace has to be asked for explicitly
    assert "--discard-workspace" in capsys.readouterr().out
    assert stop("--force", "--discard-workspace", "--yes") == 0 and not fleet.open_dseqs()
    log = (ddc.STATE_DIR / "ddc.log").read_text(encoding="utf-8")
    assert "AUDIT FORCED CLOSE" in log and "WITHOUT a verified checkpoint" in log and fleet.fleet["dev"] in log


def test_stop_with_a_verified_checkpoint_closes_everything(fleet, capsys):
    assert stop("--yes") == 0 and not fleet.open_dseqs()
    cmd = dev_commands(fleet)[-1]
    assert "checkpoint.sh final-stop" in cmd and "--allow-shrink" not in cmd
    assert "closed 3 lease(s)" in capsys.readouterr().out


def test_allow_shrink_is_passed_through_stop_and_checkpoint_and_refusals_are_explained(fleet, capsys):
    fleet.checkpoint = "refuse"
    assert stop("--yes") == 1
    out = capsys.readouterr().out
    assert "the dev box refused the checkpoint: refusing: workspace web-app lost more than half" in out and "--allow-shrink" in out
    assert fleet.fleet["dev"] in fleet.open_dseqs()
    fleet.checkpoint = "ok"
    assert stop("--allow-shrink", "--yes") == 0
    assert dev_commands(fleet)[-1].endswith("final-stop --allow-shrink")
    ddc.cmd_start(ARGS)
    ddc.cmd_checkpoint(types.SimpleNamespace(allow_shrink=True))
    assert dev_commands(fleet)[-1].endswith("manual --allow-shrink")
    ddc.cmd_checkpoint(None)
    assert dev_commands(fleet)[-1].endswith("manual")


def test_stop_of_a_dev_lease_that_never_became_ready_needs_no_checkpoint(fake):
    own("5", status="leased")
    fake.deployments["5"] = {"name": "ddc-dev-32-x", "owner": "akash1fakeowner", "state": "active", "t0": fake.clock, "leased": True, "provider": "akash1provA"}
    ddc.CFG["state_repo"] = ""
    assert stop("--yes") == 0 and not fake.open_dseqs() and not dev_commands(fake)


def test_stop_called_by_the_tui_or_watchdog_without_cli_flags_never_prompts(fleet):
    ddc.cmd_stop(types.SimpleNamespace(workers_only=False, force=False))          # exactly what providers/akash.py passes
    assert not fleet.open_dseqs()


# ------------------------------------------------------------------ scale down
def test_scale_down_lists_what_it_closes_supports_dry_run_and_only(fleet, capsys):
    w1, w2 = fleet.fleet["workers"]
    assert ddc.main(["scale", "1", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Will close 1 lease(s)" in out and w2 in out and not deletes(fleet)    # the highest-numbered worker by default
    assert ddc.main(["scale", "1", "--only", w1, "--yes"]) == 0
    assert sorted(fleet.open_dseqs()) == sorted([fleet.fleet["dev"], w2])        # the named one, not the default
    assert ddc.main(["scale", "0", "--only", "9999", "--yes"]) == 1 and "--only matches none" in capsys.readouterr().out
    assert ddc.main(["scale", "3", "--dry-run"]) == 0 and "Would create 2 worker lease(s)" in capsys.readouterr().out
    assert len(fleet.open_dseqs()) == 2

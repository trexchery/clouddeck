"""End-to-end provisioning through the real CLI (`ddc start`) against the fake Akash: create -> bids -> lease -> ssh ready -> restore -> session,
and what happens when something in that chain refuses or breaks.  Offline, deterministic (fake clock), runs on Windows and Linux."""
import pytest

from clouddeck.engine import ddc

pytest.register_assert_rewrite("e2e_helpers")      # so assertion rewriting also applies to the helpers (they are loaded as a plugin below)
from e2e_helpers import created, deleted, deps, ledger_events, live, run_ddc  # noqa: E402

pytest_plugins = ["e2e_helpers"]       # the e2e fixture


# ------------------------------------------------------------------------------------------------ the happy path
def test_start_runs_the_whole_chain_in_order_and_ends_with_a_session(e2e, capsys):
    rc, out = run_ddc(capsys, "start", "-p", "32")
    assert rc == 0, out
    # API order: balance check, create, bids, lease, then polling the lease for its ssh port
    routes = [f"{m} {r}" for m, r in e2e.calls]
    first = {k: routes.index(k) for k in ("GET /v1/balances", "POST /v1/deployments", "GET /v1/bids", "POST /v1/leases")}
    assert first["GET /v1/balances"] < first["POST /v1/deployments"] < first["GET /v1/bids"] < first["POST /v1/leases"]
    assert routes.count("POST /v1/deployments") == 1 and routes.count("POST /v1/leases") == 1
    # lease side order: scripts, env, keys, setup, restore, session
    cmds = [c for _, c in e2e.ssh_log]
    idx = lambda s: next(i for i, c in enumerate(cmds) if s in c)  # noqa: E731
    assert idx("test -f /run/ddc-role") < idx("tar -xf - -C /opt/ddc") < idx("/home/dev/.config/ddc/env") < idx("setup.sh dev") < idx("restore.sh") < idx("tmux has-session")
    d = ddc.dev_box()
    assert d["status"] == "ready" and d["role"] == "dev" and d["ours"] and d["name"].startswith("ddc-dev-32-") and d["host"] and d["port"]
    assert [e["event"] for e in ledger_events()] == ["created", "leased"]
    assert "Ready." in out and "hard stop after 8 h" in out
    # the cheapest acceptable bid under the price cap was taken, and the record says what it costs
    assert d["usd_hr"] == pytest.approx(0.3) and d["limit_h"] == 8


def test_a_second_start_reuses_the_box_and_only_rechecks_provisioning(e2e, capsys):
    assert run_ddc(capsys, "start")[0] == 0
    n = created(e2e)
    rc, out = run_ddc(capsys, "start")
    assert rc == 0 and created(e2e) == n == 1 and "dev box already running" in out
    assert len(e2e.open_dseqs()) == 1


def test_start_with_workers_scales_after_the_dev_box_is_ready(e2e, capsys):
    rc, out = run_ddc(capsys, "start", "-w", "2")
    assert rc == 0, out
    assert [d["role"] for d in live()].count("worker") == 2 and len(e2e.open_dseqs()) == 3
    conf = [i for i in e2e.inputs if "config.d/workers" in i[1]][-1][2].decode()
    assert "Host w01" in conf and "Host w02" in conf and "StrictHostKeyChecking accept-new" in conf


def test_no_secret_ever_appears_on_a_command_line_or_in_the_logs(e2e, capsys):
    rc, out = run_ddc(capsys, "start", "-w", "1")
    assert rc == 0
    wire = " ".join(c for _, c in e2e.ssh_log)
    for secret in ("fake-key", "fake-pass"):
        assert secret not in wire and secret not in out and secret not in ddc.LOG.read_text(encoding="utf-8")
    inputs = b"".join(i[2] or b"" for i in e2e.inputs)
    assert b"fake-pass" in inputs, "the passphrase travels on stdin to a 0600 file, not on a command line"
    assert b"fake-key" not in inputs, "the Akash API key never goes to a lease at all"


def test_start_without_a_state_repo_warns_and_provisions_without_gpg_or_deploy_key(e2e, capsys):
    ddc.CFG["state_repo"] = ""
    rc, out = run_ddc(capsys, "start")
    assert rc == 0 and "checkpoints are disabled" in out
    env = next(i[2] for i in e2e.inputs if i[2] and i[2].startswith(b"export DDC_STATE_REPO")).decode()
    assert 'DDC_STATE_REPO=""' in env
    assert not any("state_deploy_key" in c for _, c in e2e.ssh_log)


# ------------------------------------------------------------------------------------------------ refusals before anything is created
def test_budget_guard_refuses_before_any_deployment_exists(e2e, capsys):
    e2e.balance = 15.0                                    # reserve 10 + a worst-case 8 h dev box ($7.20) does not fit
    rc, out = run_ddc(capsys, "start")
    assert rc == 1 and ("budget" in out.lower() or "reserve" in out.lower()), out
    assert created(e2e) == 0 and e2e.deployments == {} and deps() == {} and ddc.load_state().get("pending") in (None, {})
    assert not e2e.ssh_log, "no lease, so nothing to ssh to"


def test_budget_guard_counts_what_is_already_running(e2e, capsys):
    assert run_ddc(capsys, "start")[0] == 0
    e2e.balance = 10.0 + 8 * 0.3 + 0.5                    # enough for the box that exists, not for one more worker at its cap
    n = created(e2e)
    rc, out = run_ddc(capsys, "scale", "1", "--yes")
    assert rc == 1 and created(e2e) == n and len(e2e.open_dseqs()) == 1


def test_lease_count_cap_refuses_a_fifth_lease_without_calling_the_api(e2e, capsys):
    assert run_ddc(capsys, "start", "-w", "3")[0] == 0     # 4 = max_total_leases
    n = created(e2e)
    rc, out = run_ddc(capsys, "scale", "4", "--yes")
    assert rc == 1 and ("max_total_leases" in out or "max_workers" in out), out
    assert created(e2e) == n and len(e2e.open_dseqs()) == 4


def test_start_refuses_when_the_machine_cannot_run_it_before_spending_anything(e2e, monkeypatch, capsys):
    monkeypatch.setattr(ddc, "preflight", lambda role="dev": ["`gpg` was not found (gpg): install it and put it on PATH"])
    rc, out = run_ddc(capsys, "start")
    assert rc == 1 and "preflight failed, nothing was created" in out and "gpg" in out
    assert e2e.calls == [] and e2e.deployments == {}


# ------------------------------------------------------------------------------------------------ bids
def test_no_bids_closes_the_empty_deployment_retries_once_with_a_fresh_budget_check_then_gives_up(e2e, capsys):
    e2e.providers.clear()
    t0 = e2e.clock
    rc, out = run_ddc(capsys, "start")
    assert rc == 1 and "no acceptable bids" in out
    assert created(e2e) == 2, "one retry with another provider, never a third"
    assert e2e.open_dseqs() == [] and all(v["state"] == "closed" for v in e2e.deployments.values())
    assert len(ledger_events("closed")) == 2 and ddc.load_state().get("pending") in (None, {})
    assert e2e.clock - t0 <= 2 * (30 * 4 + 30), "bounded waiting: 30 polls x 4 s per attempt"
    assert not any(d["status"] != "closed" for d in deps().values())
    assert ddc.budget_view()["committed"] == 0


def test_bids_above_the_price_cap_are_not_accepted(e2e, capsys):
    e2e.bid_uact = 3000                                   # $1.80/h against the $0.90 cap of profile 32
    rc, out = run_ddc(capsys, "start")
    assert rc == 1 and "no acceptable bids" in out and e2e.open_dseqs() == []


def test_unreliable_providers_are_skipped_even_when_cheap(e2e, monkeypatch, capsys):
    monkeypatch.setattr(e2e, "public_get", lambda path: [{"owner": p, "uptime7d": 0.5, "hostUri": f"https://{h}:8443", "attributes": []} for p, h in e2e.providers.items()])
    monkeypatch.setattr(ddc, "public_get", e2e.public_get)
    rc, out = run_ddc(capsys, "start")
    assert rc == 1 and "no acceptable bids" in out and e2e.open_dseqs() == []


# ------------------------------------------------------------------------------------------------ the provider restarts / disappears mid-provisioning
def test_ssh_outage_during_provisioning_leaves_a_recorded_lease_and_the_next_start_resumes_it(e2e, capsys):
    e2e.fail_ssh("restore.sh", "Connection reset by peer", times=1)       # the provider restarted the container while the box was being set up
    rc, out = run_ddc(capsys, "start")
    assert rc == 1 and "Connection reset by peer" in out
    assert len(e2e.open_dseqs()) == 1 and ddc.dev_box() is not None, "the lease exists and is recorded: nothing is hidden, nothing leaks"
    rc, out = run_ddc(capsys, "start")
    assert rc == 0 and created(e2e) == 1, out
    assert "dev box already running" in out and ledger_events("closed") == []


def test_provider_restart_between_runs_is_repaired_by_the_next_start(e2e, capsys):
    assert run_ddc(capsys, "start")[0] == 0
    host = ddc.dev_box()["host"]
    e2e.restarted.add(host)                                               # new container: empty disk, no /opt/ddc/setup.sh
    n_setup = len(e2e.commands("setup.sh dev"))
    rc, out = run_ddc(capsys, "start")
    assert rc == 0 and created(e2e) == 1
    assert len(e2e.commands("setup.sh dev")) == n_setup + 1 and host not in e2e.restarted, "tools were installed again on the same lease"


def test_a_lease_the_provider_closed_while_starting_is_recorded_as_closed_by_the_platform_and_start_creates_a_new_one(e2e, capsys):
    assert run_ddc(capsys, "start")[0] == 0
    first = ddc.dev_box()["dseq"]
    e2e.deployments[first]["state"] = "closed"                            # provider gone / runtime limit
    rc, out = run_ddc(capsys, "start")
    assert rc == 0 and created(e2e) == 2 and ddc.dev_box()["dseq"] != first, out
    assert deps()[first]["status"] == "closed" and ledger_events("closed")[0]["by"] == "platform"


def test_ssh_that_never_comes_up_rolls_back_only_the_lease_this_start_created(e2e, capsys):
    foreign = e2e.add_foreign()
    e2e.unreachable.update(e2e.providers.values())
    rc, out = run_ddc(capsys, "start")
    assert rc == 1 and "ssh never came up" in out
    assert e2e.open_dseqs() == [foreign], "both attempts were rolled back; the foreign deployment was never touched"
    assert created(e2e) == 2 and foreign not in deleted(e2e)

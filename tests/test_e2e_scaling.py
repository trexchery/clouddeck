"""End-to-end scaling through the real CLI: scale up / down / to zero, partial failures, heal after a container restart, and the worker idle
shutdown on the watchdog's clock.  Offline, deterministic (fake clock), Windows and Linux."""
import subprocess
import threading

import pytest

from clouddeck.engine import ddc

pytest.register_assert_rewrite("e2e_helpers")      # so assertion rewriting also applies to the helpers (they are loaded as a plugin below)
from e2e_helpers import assert_foreign_untouched, created, deleted, deps, ledger_events, run_ddc, watchdog  # noqa: E402

pytest_plugins = ["e2e_helpers"]       # the e2e / fleet fixtures


def workers_idx():
    return [w["idx"] for w in ddc.workers()]


# ------------------------------------------------------------------------------------------------ up / down / zero
def test_scale_up_down_and_to_zero_closes_the_highest_numbered_workers_and_never_the_dev_box(e2e, capsys):
    foreign = e2e.add_foreign()
    assert run_ddc(capsys, "start")[0] == 0
    dev = ddc.dev_box()
    rc, out = run_ddc(capsys, "scale", "3", "--yes")
    assert rc == 0 and workers_idx() == [1, 2, 3], out
    assert len(e2e.open_dseqs()) == 5 and {w["provider"] for w in ddc.workers()} != {dev["provider"]}, "workers never share the dev box's provider"
    assert all(w["status"] == "ready" and w["limit_h"] == 4 for w in ddc.workers())
    conf = [i for i in e2e.inputs if "config.d/workers" in i[1]][-1][2].decode()
    assert conf.count("Host w") == 3
    by_idx = {w["idx"]: w["dseq"] for w in ddc.workers()}

    rc, out = run_ddc(capsys, "scale", "1", "--yes")
    assert rc == 0 and workers_idx() == [1]
    assert sorted(deleted(e2e)) == sorted([by_idx[2], by_idx[3]]), "w02 and w03 go: the highest numbers, w01 stays"
    conf = [i for i in e2e.inputs if "config.d/workers" in i[1]][-1][2].decode()
    assert conf.count("Host w") == 1 and "Host w01" in conf, "the dev box no longer knows the closed workers"

    rc, out = run_ddc(capsys, "scale", "0", "--yes")
    assert rc == 0 and ddc.workers() == [] and ddc.dev_box()["dseq"] == dev["dseq"]
    assert sorted(e2e.open_dseqs()) == sorted([dev["dseq"], foreign]) and dev["dseq"] not in deleted(e2e)
    assert len(ledger_events("closed")) == 3
    assert_foreign_untouched(e2e, foreign)


def test_scale_up_again_reuses_free_numbers_and_scale_to_the_same_number_is_a_no_op(e2e, capsys):
    assert run_ddc(capsys, "start", "-w", "2")[0] == 0
    first = [w["dseq"] for w in ddc.workers() if w["idx"] == 1][0]
    assert run_ddc(capsys, "scale", "2", "--yes")[0] == 0 and created(e2e) == 3
    rc, out = run_ddc(capsys, "scale", "2", "--yes", "--only", "w02")      # --only only selects who goes when shrinking
    assert rc == 0 and created(e2e) == 3 and not deleted(e2e)
    rc, out = run_ddc(capsys, "scale", "1", "--only", "w01", "--yes")      # close w01 instead of the highest number
    assert rc == 0 and deleted(e2e) == [first] and workers_idx() == [2]
    assert run_ddc(capsys, "scale", "2", "--yes")[0] == 0 and workers_idx() == [1, 2], "the freed number is reused"


def test_scale_dry_run_and_the_confirmation_change_nothing(e2e, capsys, monkeypatch):
    assert run_ddc(capsys, "start", "-w", "2")[0] == 0
    n, d = created(e2e), len(deleted(e2e))
    rc, out = run_ddc(capsys, "scale", "0", "--dry-run")
    assert rc == 0 and "Will close 2 lease(s)" in out and "workers hold no data" in out and len(e2e.open_dseqs()) == 3
    rc, out = run_ddc(capsys, "scale", "5", "--dry-run")
    assert rc == 0 and "Would create 3 worker lease(s)" in out and "worst case" in out and created(e2e) == n
    rc, out = run_ddc(capsys, "scale", "0")                                # no terminal, no --yes: refuses instead of guessing
    assert rc == 1 and "--yes" in out and len(deleted(e2e)) == d
    monkeypatch.setattr("sys.stdin", type("TTY", (), {"isatty": lambda self: True, "readline": lambda self: "no\n"})())
    monkeypatch.setattr("builtins.input", lambda prompt="": "no")
    rc, out = run_ddc(capsys, "scale", "0")
    assert rc == 1 and "aborted" in out and len(deleted(e2e)) == d
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")
    rc, out = run_ddc(capsys, "scale", "0")
    assert rc == 0 and ddc.workers() == []


def test_scale_without_a_dev_box_or_beyond_the_limits_is_refused_before_the_api_is_called(e2e, capsys):
    rc, out = run_ddc(capsys, "scale", "2", "--yes")
    assert rc == 1 and "start the dev box first" in out and created(e2e) == 0
    assert run_ddc(capsys, "start")[0] == 0
    n = created(e2e)
    for n_workers in ("9", "-1"):
        rc, out = run_ddc(capsys, "scale", n_workers, "--yes")
        assert rc == 1 and "cannot scale" in out
    rc, out = run_ddc(capsys, "scale", "5", "--yes")                       # max_workers = 4 in the config
    assert rc == 1 and ("max_workers" in out or "max_total_leases" in out), out
    assert created(e2e) == n and len(e2e.open_dseqs()) == 1


def test_one_worker_that_cannot_be_provisioned_is_closed_the_others_stay_and_only_ours_is_ever_deleted(e2e, capsys):
    foreign = e2e.add_foreign()
    assert run_ddc(capsys, "start")[0] == 0
    e2e.fail_ssh("setup.sh worker", "dpkg: error processing package", times=1)
    rc, out = run_ddc(capsys, "scale", "3", "--yes")
    assert rc == 0, out
    assert len(ddc.workers()) == 2 and created(e2e) == 4 and len(deleted(e2e)) == 1, "exactly the failed lease was closed"
    assert "provisioning failed" in out and "closing the new lease" in out
    gone = deleted(e2e)[0]
    assert deps()[gone]["status"] == "closed" and deps()[gone]["name"].startswith("ddc-w")
    assert len(e2e.open_dseqs()) == 4, "dev + 2 workers + the foreign deployment"
    assert_foreign_untouched(e2e, foreign)
    assert run_ddc(capsys, "scale", "3", "--yes")[0] == 0 and len(ddc.workers()) == 3, "a later scale-up fills the gap"


def test_concurrent_scale_ups_cannot_exceed_the_worker_cap(e2e, capsys):
    assert run_ddc(capsys, "start")[0] == 0
    results = []

    def go():
        results.append(ddc.main(["scale", "3", "--yes", "--approve-usd", "1000"]))
    threads = [threading.Thread(target=go) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    capsys.readouterr()
    assert len(ddc.workers()) == 3 and len(e2e.open_dseqs()) == 4 and created(e2e) == 4, "three racing `ddc scale 3` made exactly three workers"


# ------------------------------------------------------------------------------------------------ heal
def test_heal_reprovisions_only_the_worker_whose_container_restarted(e2e, capsys):
    ddc.CFG["max_leases_per_provider"] = 1                     # every worker on its own host: the probe addresses a host
    ddc.CFG["worker_prep_command"] = "bash /work/web-app/prep.sh"
    e2e.patch_subprocess()
    assert run_ddc(capsys, "start", "-w", "1")[0] == 0 and run_ddc(capsys, "scale", "2", "--yes")[0] == 0     # one worker at a time: parallel workers may pick the same provider
    w1, w2 = ddc.workers()
    assert w1["provider_host"] != w2["provider_host"]
    e2e.restarted.add(w2["host"])                              # new container: new host key, empty disk
    setups = len(e2e.commands("setup.sh worker"))

    rc, out = run_ddc(capsys, "heal", "--dry-run")
    assert rc == 0 and "would re-provision w02" in out and "w01" not in out.split("would re-provision")[-1]
    assert len(e2e.commands("setup.sh worker")) == setups and w2["host"] in e2e.restarted

    rc, out = run_ddc(capsys, "heal")
    assert rc == 0 and f"healed {w2['name']}" in out, out
    assert len(e2e.commands("setup.sh worker")) == setups + 1 and w2["host"] not in e2e.restarted
    prep = [c for _, c in e2e.ssh_log if "DIST_HOSTS" in c]
    assert len(prep) == 1 and "DIST_HOSTS=w02 " in prep[0] and "w01" not in prep[0], "the prep command only runs for the healed worker"
    assert len(e2e.open_dseqs()) == 3 and not deleted(e2e), "the lease was reused, nothing was closed or re-created"
    rc, out = run_ddc(capsys, "heal")
    assert rc == 0 and "healed" not in out and len(e2e.commands("setup.sh worker")) == setups + 1


def test_heal_recognises_a_changed_host_key_as_a_restarted_container(e2e, capsys, monkeypatch):
    ddc.CFG["max_leases_per_provider"] = 1
    e2e.patch_subprocess()
    assert run_ddc(capsys, "start", "-w", "1")[0] == 0 and run_ddc(capsys, "scale", "2", "--yes")[0] == 0     # one worker at a time: parallel workers may pick the same provider
    w1, w2 = ddc.workers()
    changed, real = {"on": True}, subprocess.run

    def run(argv, *a, **k):
        if changed["on"] and argv and argv[0] == ddc.SSH and argv[-2].endswith("@" + w2["host"]):    # ssh refuses: the host key is not the one we know
            return subprocess.CompletedProcess(argv, 255, b"", b"@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@ Host key verification failed.")
        return real(argv, *a, **k)
    monkeypatch.setattr(subprocess, "run", run)
    setups = len(e2e.commands("setup.sh worker"))
    seen = []
    real_provision = ddc.provision_worker
    monkeypatch.setattr(ddc, "provision_worker", lambda w: (seen.append(w["idx"]), changed.update(on=False), real_provision(w))[2])
    rc, out = run_ddc(capsys, "heal")
    assert rc == 0 and seen == [2] and f"healed {w2['name']}" in out, out
    assert len(e2e.commands("setup.sh worker")) == setups + 1 and not deleted(e2e)
    assert any("ssh-keygen -f ~/.ssh/known_hosts_workers -R" in c for _, c in e2e.ssh_log), "the dev box forgets the old host key of that worker too"


def test_heal_reports_an_unreachable_worker_instead_of_reprovisioning_it(e2e, capsys):
    ddc.CFG["max_leases_per_provider"] = 1
    e2e.patch_subprocess()
    assert run_ddc(capsys, "start", "-w", "1")[0] == 0 and run_ddc(capsys, "scale", "2", "--yes")[0] == 0     # one worker at a time: parallel workers may pick the same provider
    w2 = ddc.workers()[1]
    e2e.unreachable.add(w2["host"])
    setups = len(e2e.commands("setup.sh worker"))
    rc, out = run_ddc(capsys, "heal")
    assert rc == 0 and f"{w2['name']} unreachable" in out and len(e2e.commands("setup.sh worker")) == setups


# ------------------------------------------------------------------------------------------------ worker idle shutdown, on the watchdog's clock
def test_idle_workers_are_closed_after_the_grace_period_while_the_dev_box_keeps_running(fleet):
    fleet.busy = "claude"                                      # the user is working on the dev box; no jobs on the workers
    results = [watchdog(fleet, 5), watchdog(fleet, 4)]         # 9 minutes since the workers came up: under worker_idle_minutes = 10
    assert results == [0, 0] and len(ddc.workers()) == 2
    assert watchdog(fleet, 2) == 0                             # 11 minutes
    assert ddc.workers() == [] and ddc.dev_box() is not None and len(fleet.open_dseqs()) == 1
    assert len(ledger_events("closed")) == 2


def test_a_job_dispatched_to_the_workers_resets_their_idle_timer(fleet):
    fleet.busy = "claude"
    watchdog(fleet, 8)
    ddc.touch_worker_activity(ddc.dev_box())                   # what CloudDeck's dispatch / the controller's bench do
    watchdog(fleet, 8)                                         # 16 min after the lease, 8 after the job
    assert len(ddc.workers()) == 2
    watchdog(fleet, 3)                                         # 11 min after the job
    assert ddc.workers() == []


def test_cpu_or_process_activity_on_the_dev_box_keeps_the_workers(fleet):
    fleet.busy = "procs"                                       # a test run / build is going on
    for _ in range(4):
        assert watchdog(fleet, 5) == 0
    assert len(ddc.workers()) == 2 and len(fleet.open_dseqs()) == 3
    fleet.busy = "claude"
    watchdog(fleet, 1)
    assert ddc.workers() == []


def test_a_freshly_scaled_up_worker_gets_the_full_grace_period(fleet, capsys):
    fleet.busy = "claude"
    watchdog(fleet, 12)
    assert ddc.workers() == []
    fleet.worker_activity = 0
    assert run_ddc(capsys, "scale", "1", "--yes")[0] == 0 and len(ddc.workers()) == 1
    assert watchdog(fleet, 6) == 0 and len(ddc.workers()) == 1, "the dev box's idle counter is old, the new lease is not"
    assert watchdog(fleet, 6) == 0 and ddc.workers() == []


def test_workers_whose_dev_box_is_gone_are_not_left_billing(fleet):
    dev = ddc.dev_box()["dseq"]
    fleet.deployments[dev]["state"] = "closed"                 # the platform closed the dev box (runtime limit / provider)
    assert watchdog(fleet, 5) == 0
    assert ddc.workers() == [] and fleet.open_dseqs() == []
    assert any(e.get("by") == "platform" for e in ledger_events("closed"))

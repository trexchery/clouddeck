"""The per-command cost approval of the controller (--approve-usd on `ddc start` / `ddc scale` up) and its exposure accounting.  Offline."""
import pytest
from job_helpers import make_spec

from clouddeck.engine import ddc
from clouddeck.job.spec import render_sdl

pytest.register_assert_rewrite("e2e_helpers")
from e2e_helpers import created, run_ddc  # noqa: E402

pytest_plugins = ["e2e_helpers"]


def raw(capsys, *argv):
    """`ddc <argv>` exactly as typed (run_ddc adds a generous --approve-usd to start/scale): (rc, output)."""
    rc = ddc.main(list(argv))
    cap = capsys.readouterr()
    return rc, cap.out + cap.err


def test_start_without_approval_is_refused_and_creates_nothing(e2e, capsys):
    rc, out = raw(capsys, "start", "-p", "32")
    assert rc == 1 and "worst case" in out and "--approve-usd" in out and created(e2e) == 0
    rc, out = raw(capsys, "start", "-p", "32", "--approve-usd", "0.01")
    assert rc == 1 and "above the approved $0.01" in out and created(e2e) == 0 and ddc.APPROVAL is None


def test_start_with_an_adequate_approval_works_and_scale_up_needs_its_own(e2e, capsys):
    worst = ddc.CFG["profiles"]["32"]["max_usd_hr"] * int(ddc.CFG["max_runtime_hours"])
    rc, out = run_ddc(capsys, "start", "-p", "32", "--approve-usd", f"{ddc.approve_hint(worst)}")
    assert rc == 0, out and created(e2e) == 1
    assert "--approve-usd" in raw(capsys, "scale", "1", "--yes", "-p", "32")[1] and created(e2e) == 1
    assert "above the approved" in raw(capsys, "scale", "1", "--yes", "-p", "32", "--approve-usd", "0.01")[1] and created(e2e) == 1
    assert run_ddc(capsys, "scale", "1", "--yes", "-p", "32")[0] == 0 and created(e2e) == 2


def test_the_approval_does_not_carry_over_between_commands(e2e, capsys):
    assert run_ddc(capsys, "start", "-p", "32")[0] == 0
    ddc.APPROVAL = {"usd": 1e9, "live": {}, "spent": 0.0}                 # a stale one from an earlier command in the same process
    assert "--approve-usd" in raw(capsys, "scale", "1", "--yes", "-p", "32")[1]   # main() resets it first, so this is refused
    assert created(e2e) == 1


def test_exposure_counts_live_leases_at_worst_case_and_closed_ones_at_their_cost(jw):
    spec = make_spec(jw.tmp)
    sdl = render_sdl(spec, "ubuntu:24.04", "ssh-ed25519 AAAA")
    launch = lambda n: ddc.launch_lease(f"ddc-job-t1-0101-{n}", sdl, role="job", limit_h=2, cap_usd_hr=3.0, label="x")  # noqa: E731
    ddc.require_approval(10.0, 6.0, "test")
    one = launch("aaaa")["dseq"]
    with pytest.raises(ddc.DdcError, match=r"exposure would reach \$12.00, above the \$10.00 approved"):
        launch("bbbb")
    assert len(jw.akash.posts) == 1                                        # the refused one never reached the API
    assert ddc.close_box(one) == 0                                         # closed before it ever billed: its reservation is released at cost 0
    assert ddc.APPROVAL["spent"] == 0 and ddc.APPROVAL["live"] == {}
    two = launch("cccc")["dseq"]
    assert ddc.APPROVAL["live"] == {two: 6.0}
    ddc.close_box(two)


def test_a_stale_record_of_a_dead_dev_box_does_not_skip_the_approval(e2e, capsys):
    assert run_ddc(capsys, "start", "-p", "32")[0] == 0
    dseq = next(k for k, d in ddc.active_deps().items() if d.get("role") == "dev")
    e2e.deployments[dseq]["state"] = "closed"          # the platform ended it (runtime limit); state.json still says ready
    rc, out = raw(capsys, "start", "-p", "32")
    assert rc == 1 and "--approve-usd" in out and created(e2e) == 1

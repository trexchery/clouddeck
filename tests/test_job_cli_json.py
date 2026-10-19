"""`ddc job` CLI: the per-run cost approval (--approve-usd) and the --json output of run / check / hardware / report / close.  Offline: the 'jw' world
(tests/job_helpers.py) drives the real JobRunner through `clouddeck.job.cli.main`."""
import atexit
import functools
import json

import pytest
from job_helpers import make_spec

from clouddeck.engine import ddc
from clouddeck.job import cli
from clouddeck.job.runner import JobRunner


@pytest.fixture
def jcli(jw, monkeypatch, capsys):
    """cli.main on the virtual-time world: (argv...) -> (rc, stdout, stderr).  The spec is the one of make_spec: worst case $3/h x 2 h = $6.00."""
    monkeypatch.setattr(cli, "load_spec", lambda path: make_spec(jw.tmp))
    monkeypatch.setattr(cli, "JobRunner", functools.partial(JobRunner, clock=jw.clock, remote_factory=jw.factory, install_signals=False, log=lambda m: None))

    def go(*argv):
        rc = cli.main(list(argv))
        cap = capsys.readouterr()
        return rc, cap.out, cap.err
    go.w = jw
    return go


def test_run_without_approval_refuses_and_creates_nothing(jcli):
    rc, out, err = jcli("run", "spec.toml")
    assert rc == 1 and "worst case $6.00" in err and "--approve-usd 6.00" in err
    assert jcli.w.akash.posts == [] and ddc.APPROVAL is None


def test_run_with_too_low_an_approval_refuses_and_creates_nothing(jcli):
    rc, out, err = jcli("run", "spec.toml", "--approve-usd", "5.99")
    assert rc == 1 and "above the approved $5.99" in err
    assert jcli.w.akash.posts == []


def test_run_with_an_adequate_approval_runs_and_reports_json(jcli):
    rc, out, err = jcli("run", "spec.toml", "--approve-usd", "6", "--json")
    j = json.loads(out)                                                   # stdout is exactly one JSON object
    assert rc == 0 and j["ok"] is True and j["error"] is None and j["job"] == "t1"
    assert j["closed"] is True and j["dseq"] and j["provider"] and j["usd_hr"] > 0 and j["cost_usd"] is not None
    (art,) = j["artifacts"]
    assert art["path"] == "out/result.bin" and len(art["sha256"]) == 64 and art["size"] > 0 and art["local"].endswith("result.bin")
    assert j["artifacts_dir"].endswith("got") and j["telemetry_file"].endswith(".json") and "billed_s" in j["summary"]
    assert len(jcli.w.akash.posts) == 1 and jcli.w.akash.deps[j["dseq"]]["state"] == "closed"


def test_run_json_reports_an_unverified_close_as_a_failure(jcli, monkeypatch):
    real = ddc.close_box

    def flaky(dseq, listed=None):
        raise ddc.DdcError("platform did not confirm")
    monkeypatch.setattr(ddc, "close_box", flaky)
    made, real_runner = [], cli.JobRunner
    monkeypatch.setattr(cli, "JobRunner", lambda *a, **kw: made.append(real_runner(*a, **kw)) or made[-1])
    rc, out, err = jcli("run", "spec.toml", "--approve-usd", "6", "--json")
    monkeypatch.setattr(ddc, "close_box", real)
    j = json.loads(out)
    assert rc == 1 and j["ok"] is False and j["closed"] is False and "NOT verified closed" in j["error"]
    ddc.close_box(j["dseq"])
    atexit.unregister(made[0]._atexit)                                    # the real exit hook would retry the close after the test world is gone


def test_check_json_shape(jcli):
    rc, out, err = jcli("check", "spec.toml", "--offline", "--json")
    j = json.loads(out)
    assert rc in (0, 2) and j["ok"] == (rc == 0) and j["job"] == "t1"
    assert j["worst_case_usd"] == 6.0 and j["cap_usd_hr"] == 3.0 and j["limit_h"] == 2 and j["approve_usd"] == 6.0
    assert j["gpu"]["vendor"] == "vendor-a" and j["image"]["stock"] == "ubuntu:24.04"
    assert all(set(c) == {"status", "name", "detail"} for c in j["checks"]) and j["checks"]
    assert jcli.w.akash.posts == []


def test_run_dry_run_json_is_the_check_object_and_needs_no_approval(jcli):
    rc, out, err = jcli("run", "spec.toml", "--dry-run", "--offline", "--json")
    assert json.loads(out)["approve_usd"] == 6.0 and jcli.w.akash.posts == []


def test_hardware_json_filters_and_sorting(jcli):
    rc, out, _ = jcli("hardware", "--json")
    (m,) = json.loads(out)["models"]
    assert m == {"vendor": None, "model": "model-a", "ram": "80Gi", "interface": "sxm", "available": 6, "total": 16, "price_min": 1.8, "price_med": 2.2, "price_max": 3.0}
    assert json.loads(jcli("hardware", "--json", "--gpu", "nope")[1])["models"] == []
    assert json.loads(jcli("hardware", "--json", "--min-vram", "81")[1])["models"] == []
    assert len(json.loads(jcli("hardware", "--json", "--min-vram", "80", "--available", "--gpu", "MODEL")[1])["models"]) == 1
    assert "CPU-only" in json.loads(out)["note"]


def test_hardware_works_without_a_config_file(jcli, monkeypatch):
    monkeypatch.setattr(ddc, "CONFIG_ERROR", "no config file")
    rc, out, err = jcli("hardware", "--json")
    assert rc == 0 and json.loads(out)["ok"] is True
    rc, out, err = jcli("check", "spec.toml", "--json")                   # everything else still refuses
    assert rc == 1 and json.loads(out) == {"ok": False, "error": "no config file"}


def test_close_json_for_an_unknown_job_and_report_without_telemetry(jcli):
    rc, out, _ = jcli("close", "nope", "--json")
    assert rc == 0 and json.loads(out)["found"] is False and json.loads(out)["closed"] == []
    rc, out, _ = jcli("report", "--json")
    assert rc == 1 and json.loads(out)["ok"] is False


def test_report_json_and_close_json_after_a_run(jcli):
    j = json.loads(jcli("run", "spec.toml", "--approve-usd", "6", "--json")[1])
    r = json.loads(jcli("report", "t1", "--json")[1])
    assert r["telemetry_file"] == j["telemetry_file"] and r["telemetry"]["job"] == "t1" and r["telemetry"]["ok"] is True
    assert json.loads(jcli("close", "t1", "--json")[1])["found"] is False   # already closed by the run

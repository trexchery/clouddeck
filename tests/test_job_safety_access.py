"""`ddc job` access safety: the provider-reported ssh endpoint is validated before use, the ssh options are the controller's (accept-new host keys, never
off), secrets travel over ssh only and are never logged, a job never closes anything that is not its own, and `ddc job` is wired into `ddc`."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from job_helpers import make_spec, run_job

from clouddeck.engine import ddc
from clouddeck.job.clock import FakeClock
from clouddeck.job.remote import SshRemote

ROOT = Path(__file__).resolve().parent.parent
SPEC_TOML = ('name = "cli"\nruntime_hours = 1\nmax_cost_usd = 5\n[hardware]\nmax_usd_hr = 1.0\ncpu = 2\nmemory = "4Gi"\ndisk = "20Gi"\n'
             '[image]\nstock = "ubuntu:24.04"\n[[step]]\nname = "go"\nrun = "echo hi"\n')


# ------------------------------------------------------------------ the provider-reported endpoint is untrusted
@pytest.mark.parametrize("host", ["bad host; rm -rf /", "a$(id).example", "host\nProxyCommand evil", "-oProxyCommand=x", "", "a" * 300, "under_score.example", "x/y"])
def test_a_hostile_ssh_host_aborts_the_attempt_before_any_ssh_and_the_lease_is_closed(jw, host):
    jw.akash.next_host = host
    made = []
    with pytest.raises(ddc.DdcError, match="unusable ssh endpoint"):
        run_job(jw, make_spec(jw.tmp), remote_factory=lambda h, p: made.append((h, p)))
    assert made == []                                                                    # no ssh connection object was even built
    assert jw.akash.deps[jw.runner.dseq]["state"] == "closed"
    assert "bad_until" in ddc.read_json(ddc.PERF, {}).get("akash1cheap", {})            # and the provider is not offered again for a day
    assert "host" not in ddc.load_state()["deps"][jw.runner.dseq]                        # the bad value was never recorded as an endpoint


def test_a_bad_port_is_refused_like_a_bad_host(jw, monkeypatch):
    real = jw.akash.api

    def api(method, path, body=None, **kw):
        out = real(method, path, body, **kw)
        if method == "GET" and "/v1/deployments/" in path and out and out["data"]["leases"]:
            out["data"]["leases"][0]["status"]["forwarded_ports"]["job"][0]["externalPort"] = 70000
        return out

    monkeypatch.setattr(ddc, "api", api)
    with pytest.raises(ddc.DdcError, match="unusable ssh endpoint"):
        run_job(jw, make_spec(jw.tmp))
    assert jw.akash.deps[jw.runner.dseq]["state"] == "closed"


@pytest.mark.parametrize("host, port", [("a b", 22), ("h.example", 0), ("h.example", 70000), ("h.example", "x"), ("$(id)", 22), (None, 22)])
def test_the_ssh_remote_itself_refuses_a_bad_endpoint(host, port):
    with pytest.raises(ddc.DdcError, match="refusing to use the ssh endpoint"):
        SshRemote(host, port, None, Path("kh"), FakeClock())


def test_resume_of_a_record_with_a_poisoned_endpoint_is_refused(jw):
    s = make_spec(jw.tmp)
    jw.durations = {"cache-m": 10, "work": 3000}
    from clouddeck.job.runner import JobRunner
    r = JobRunner(s, clock=jw.clock, remote_factory=jw.factory, install_signals=False, log=lambda m: None)
    r._monitor = lambda: (_ for _ in ()).throw(SystemExit("simulated hard kill"))
    r._atexit = lambda: None
    with pytest.raises(SystemExit):
        r._run()
    rec = ddc.read_json(ddc.STATE_DIR / "jobs" / "t1.json", {})
    rec["host"] = "evil host; id"
    ddc.write_json(ddc.STATE_DIR / "jobs" / "t1.json", rec)
    with pytest.raises(ddc.DdcError, match="refusing to use the ssh endpoint"):
        run_job(jw, s, remote_factory=None)                                              # the real SshRemote factory validates it
    assert jw.akash.deps[r.dseq]["state"] == "closed"                                    # and the failed run still closed the lease


# ------------------------------------------------------------------ the ssh options are the controller's
def test_ssh_argv_is_the_controllers_option_set_plus_only_the_command_and_a_per_job_known_hosts(tmp_path):
    kh = tmp_path / "t1.known_hosts"
    r = SshRemote("host.example", 2222, None, kh, FakeClock())
    argv = r._argv("true")
    ctl = ddc.ssh_args({"host": "host.example", "port": 2222}, user="root", known_hosts=kh)
    assert argv[:-1] == ctl and argv[0] == ddc.SSH and argv[-1].endswith("true")
    joined = " ".join(argv)
    assert "StrictHostKeyChecking=accept-new" in joined and "BatchMode=yes" in joined and "IdentitiesOnly=yes" in joined
    assert f"UserKnownHostsFile={kh.as_posix()}" in joined and "root@host.example" in joined and "-p 2222" in joined
    assert "StrictHostKeyChecking=no" not in joined and "UserKnownHostsFile=/dev/null" not in joined and str(ddc.KEYS / "access") in joined
    default = ddc.ssh_args({"host": "host.example", "port": 2222})                       # existing callers are unchanged: the shared known_hosts file
    assert f"UserKnownHostsFile={(ddc.STATE_DIR / 'known_hosts').as_posix()}" in " ".join(default)


def test_the_job_package_never_weakens_host_key_checking():
    src = "\n".join(p.read_text(encoding="utf-8") for p in sorted((ROOT / "clouddeck" / "job").glob("*.py")))
    assert "StrictHostKeyChecking" not in src and "UserKnownHostsFile" not in src        # all ssh options come from ddc.ssh_args


# ------------------------------------------------------------------ secrets: ssh only, never in the SDL, never logged
def test_secrets_are_pushed_over_ssh_only_and_appear_nowhere_else(jw, monkeypatch):
    value = "s3cr3t-value-ABC-123"
    monkeypatch.setenv("EXAMPLE_API_TOKEN", value)
    logs: list[str] = []
    s = make_spec(jw.tmp, env={"MODE": "x", "secrets": ["EXAMPLE_API_TOKEN"]})
    jw.durations = {"cache-m": 5, "work": 5}
    run_job(jw, s, log=logs.append)
    assert jw.remotes[0].secrets == {"EXAMPLE_API_TOKEN": value}                          # delivered to the box over the ssh interface
    assert value not in jw.akash.posts[0]["sdl"] and "EXAMPLE_API_TOKEN" not in jw.akash.posts[0]["sdl"]
    assert not any(value in line for line in logs)
    on_disk = "".join(p.read_text(encoding="utf-8", errors="replace") for p in ddc.STATE_DIR.rglob("*") if p.is_file())
    assert value not in on_disk                                                           # not in state, ledger, telemetry, job record or history
    assert value not in "".join(map(str, jw.akash.calls))


def test_preflight_names_the_secrets_exposure_without_printing_values(jw, monkeypatch):
    from clouddeck.job import preflight
    monkeypatch.setenv("EXAMPLE_API_TOKEN", "v4lue-xyz")
    cs = {c.name: c for c in preflight.run(make_spec(jw.tmp, env={"secrets": ["EXAMPLE_API_TOKEN"]}), offline=True)}
    assert "can read them while the lease runs" in cs["secrets"].detail and "v4lue-xyz" not in cs["secrets"].detail


# ------------------------------------------------------------------ a job never closes what is not its own
def test_a_job_record_that_points_at_a_foreign_deployment_is_ignored_and_nothing_foreign_is_closed(jw):
    foreign = jw.akash.add_foreign()
    ddc.write_json(ddc.STATE_DIR / "jobs" / "t1.json", {"name": "t1", "dseq": foreign, "status": "ready", "host": "h.example", "port": 22})
    r = run_job(jw, make_spec(jw.tmp))                                                    # the stale record is discarded, the job runs on its own new lease
    assert jw.akash.deps[foreign]["state"] == "active" and foreign not in {c[1].rsplit("/", 1)[-1] for c in jw.akash.calls if c[0] == "DELETE"}
    assert len(jw.akash.posts) == 1 and r.dseq != foreign and r.closed


def test_a_ddc_named_state_record_without_ownership_is_not_closed_either(jw):
    d = jw.akash.add_foreign("1000000000042", name="ddc-job-t1-0101-aaaa")
    ddc.update_dep(d, name="ddc-job-t1-0101-aaaa", role="job", status="ready", ours=False)
    ddc.write_json(ddc.STATE_DIR / "jobs" / "t1.json", {"name": "t1", "dseq": d, "status": "ready", "host": "h.example", "port": 22})
    r = run_job(jw, make_spec(jw.tmp))
    assert jw.akash.deps[d]["state"] == "active" and r.closed and r.dseq != d


def test_close_orphan_only_closes_an_active_owned_job_lease_even_if_another_ours_is_named(jw):
    """[9] a record pointing at another lease of ours that is not a job lease (the dev box, a worker) must not be closed by the job runner."""
    dseq = jw.akash.add_foreign("1000000000077", name="ddc-dev-32-0101-aaaa")
    ddc.update_dep(dseq, name="ddc-dev-32-0101-aaaa", role="dev", status="ready", ours=True)
    ddc.write_json(ddc.STATE_DIR / "jobs" / "t1.json", {"name": "t1", "dseq": dseq, "status": "ready", "host": "h.example", "port": 22})
    run_job(jw, make_spec(jw.tmp))
    assert jw.akash.deps[dseq]["state"] == "active"


def test_the_runners_close_only_ever_targets_its_own_dseq_and_goes_through_close_box(jw, monkeypatch):
    closed = []
    real = ddc.close_box
    monkeypatch.setattr(ddc, "close_box", lambda dseq, listed=None: (closed.append(dseq), real(dseq, listed))[1])
    jw.akash.add_foreign()
    run_job(jw, make_spec(jw.tmp))
    assert closed == [jw.runner.dseq] and jw.akash.deps["1000000000001"]["state"] == "active"


def test_close_box_refuses_a_job_record_whose_console_name_is_not_ours(jw):
    d = jw.akash.add_foreign("1000000000043", name="somebody-elses-service")
    ddc.update_dep(d, name="ddc-job-t1-0101-aaaa", role="job", status="ready", ours=True)       # the record claims a ddc name, the console says otherwise
    with pytest.raises(ddc.DdcError, match="console name"):
        ddc.close_box(d)
    assert jw.akash.deps[d]["state"] == "active"


# ------------------------------------------------------------------ `ddc job` is wired into `ddc`
def test_ddc_job_dispatches_to_the_job_cli_and_dry_run_creates_nothing(jw, tmp_path, capsys):
    spec = tmp_path / "job.toml"
    spec.write_text(SPEC_TOML)
    assert ddc.main(["job", "run", str(spec), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "budget:" in out and "dry run: nothing created." in out and "spend_gate" in out
    assert not any(m in ("POST", "DELETE") for m, _ in jw.akash.calls)
    assert ddc.main(["job", "sdl", str(spec)]) == 0
    sdl = capsys.readouterr().out
    assert "gpu" not in sdl and "# --- stock: ubuntu:24.04" in sdl
    assert ddc.main(["job", "check", str(spec), "--offline"]) == 0
    assert ddc.main(["job", "check", str(tmp_path / "missing.toml"), "--offline"]) == 1


def test_ddc_job_help_and_the_top_level_help_list_it(capsys):
    with pytest.raises(SystemExit) as e:
        ddc.main(["job", "--help"])
    assert e.value.code == 0 and "close JOB" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        ddc.main(["--help"])
    top = capsys.readouterr().out
    assert re.search(r"^\s+job\s", top, re.M) and "ddc job ..." in top
    with pytest.raises(SystemExit) as e:
        ddc.main(["job", "nonsense"])
    assert e.value.code == 2


def test_python_dash_m_clouddeck_job_works_and_there_is_no_ddcjob_package(tmp_path):
    env = {"PYTHONPATH": str(ROOT), "DDC_STATE_DIR": str(tmp_path / "s"), "DDC_SECRETS_DIR": str(tmp_path / "k"), "PYTHONDONTWRITEBYTECODE": "1"}
    cp = subprocess.run([sys.executable, "-m", "clouddeck.job", "--help"], env={**os.environ, **env}, capture_output=True, text=True, cwd=ROOT)
    assert cp.returncode == 0 and "python -m clouddeck.job" in cp.stdout and "close JOB" in cp.stdout, cp.stderr
    assert subprocess.run([sys.executable, "-c", "import ddcjob"], env={**os.environ, **env}, capture_output=True, cwd=ROOT).returncode != 0


def test_ddc_job_needs_a_config_like_every_other_command(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(ddc, "CONFIG_ERROR", "no config found: run `ddc init`")
    assert ddc.main(["job", "check", str(tmp_path / "x.toml"), "--offline"]) == 1


def test_report_rejects_names_that_are_not_job_names(jw, capsys):
    from clouddeck.job.cli import main as job_main
    assert job_main(["report", "../../etc/*"]) == 1
    assert "not a job name" in capsys.readouterr().err


def test_telemetry_report_prints_the_last_run(jw, capsys):
    from clouddeck.job.cli import main as job_main
    jw.durations = {"cache-m": 5, "work": 5}
    run_job(jw, make_spec(jw.tmp))
    assert job_main(["report", "t1"]) == 0
    assert "startup/shutdown telemetry" in capsys.readouterr().out
    assert json.loads(next((ddc.STATE_DIR / "telemetry").glob("t1-*.json")).read_text())["ok"] is True

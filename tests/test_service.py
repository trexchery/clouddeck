"""Service layer (probes, config-backed projects, sessions, dispatch) and the remote controller API."""
import json
import os
import re
import subprocess
import threading
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from clouddeck import providers, remote, remote_client
from clouddeck.engine import ddc
from clouddeck.providers.base import ProviderError
from clouddeck.service import PROBE_CMD, Service, parse_probe


@pytest.fixture
def svc():
    return Service(providers.get("demo"))


def test_parse_probe_full_and_garbage():
    p = parse_probe("cores_used=8.00\ncpus=32\nmem_used=%d\nmem_max=%d\ndisk=%d %d\nload=7.5\nuptime_s=120\njob=npm test -x\ntop=node:790 \ngpu=55, 1200, 24564\n"
                    % (16 * 2**30, 64 * 2**30, 10 * 2**30, 100 * 2**30))
    assert p.ok and p.cpu_frac == 0.25 and p.mem_frac() == 0.25 and p.disk_frac == 0.1 and p.job == "npm test -x"
    assert p.gpu_util == 0.55 and p.gpu_mem_mb == 24564
    bad = parse_probe("nonsense\n")
    assert not bad.ok and bad.cpu_frac is None and bad.error
    assert parse_probe("cpus=4\ncores_used=9\nmem_max=max\n").cpu_frac == 1.0       # clamped; 'max' memory limit ignored
    assert PROBE_CMD.startswith("# cd:probe")


def test_probe_all_skips_unreachable_and_survives_errors(svc):
    insts = svc.provider.instances()
    res = svc.probe_all(insts)
    assert set(res) == {i.id for i in insts if i.reachable} and all(p.ok for p in res.values())
    assert insts[-1].id not in res                                                  # external: never probed

    class Boom(type(svc.provider)):
        def run(self, *a, **k):
            raise ProviderError("x: ssh timeout")
    assert Service(Boom(populate=True)).probe(insts[0]).error == "ssh timeout"


def test_config_edit_preserves_comments_crlf_and_budget_guard(svc, isolated, tmp_path):
    raw = isolated.read_bytes().replace(b"\n", b"\r\n")
    isolated.write_bytes(raw)
    folder = tmp_path / "proj"
    (folder / ".git").mkdir(parents=True)
    p = svc.add_project("my-proj", str(folder), github="me/my-proj", push=True, environment="ml")
    assert p.kind == "repo" and p.push
    text = isolated.read_bytes().decode()
    assert "# max aggregate spend" in text and "budget_guard = true" in text            # comments + policy intact
    assert "\r\n" in text and "\n" not in text.replace("\r\n", "")                      # line endings preserved
    assert isolated.with_suffix(".toml.bak").read_bytes() == raw                         # backup of the previous file
    reread = tomllib.loads(text)
    assert [r["name"] for r in reread["repos"]][-1] == "my-proj" and reread["repos"][-1]["environment"] == "ml"
    assert {x.name for x in svc.projects()} >= {"my-proj", "web-app", "notes"}
    svc.remove_project("my-proj")
    assert "my-proj" not in isolated.read_text()
    assert "my-proj" not in {x.name for x in svc.projects()}


def test_add_project_validation(svc, tmp_path):
    for name, path, gh in [("Bad Name", str(tmp_path), ""), ("web-app", str(tmp_path), ""), ("ok", str(tmp_path / "missing"), ""), ("ok", str(tmp_path), "nonsense"),
                           ("results", str(tmp_path), "")]:
        with pytest.raises(ProviderError):
            svc.add_project(name, path, gh)
    d = svc.add_project("plain", str(tmp_path))
    assert d.kind == "dir"


def test_only_idle_and_checkpoint_settings_are_tunable(svc, isolated):
    svc.set_tunable("idle_minutes", 60)
    assert "idle_minutes = 60          # dev box idle" in isolated.read_text()           # trailing comment survives
    assert ddc.CFG["idle_minutes"] == 60
    for key in ("budget_guard", "budget_usd", "state_repo"):
        with pytest.raises(ProviderError):
            svc.set_tunable(key, 1)
    with pytest.raises(ProviderError):
        svc.set_tunable("idle_minutes", 0)
    assert "budget_guard = true" in isolated.read_text()


def test_project_environment_binding(svc, isolated):
    svc.set_project_environment("notes", "ml")
    assert {p.name: p.environment for p in svc.projects()}["notes"] == "ml"
    svc.set_project_environment("notes", "")
    assert {p.name: p.environment for p in svc.projects()}["notes"] == ""
    assert "ml" in svc.environments() and "profile 32" in svc.environments()


def test_sessions_lifecycle_and_builtin_windows_are_protected(svc):
    dev = svc.provider.instances()[0]
    assert {s.name for s in svc.sessions(dev)} == {"claude", "workers", "shell"}
    name = svc.new_session(dev, "web-app")
    assert name == "cc-web-app" and name in {s.name for s in svc.sessions(dev)}
    assert svc.new_session(dev, "web-app", resume=True) == "cc-web-app-2"       # never reuses a taken window name
    svc.kill_session(dev, name)
    assert name not in {s.name for s in svc.sessions(dev)}
    with pytest.raises(ProviderError):
        svc.kill_session(dev, "claude")
    with pytest.raises(ProviderError):
        svc.new_session(dev, "not-a-project")
    assert svc.window_name("a.b c") == "cc-a-b-c"


def test_seed_refuses_to_overwrite_an_existing_workspace(svc):
    dev = svc.provider.instances()[0]
    with pytest.raises(ProviderError, match="already on the dev box"):
        svc.seed_missing(dev, "web-app")


def test_dispatch_runs_on_every_target_and_can_be_stopped(svc):
    workers = [i for i in svc.provider.instances() if i.role == "worker"]
    seen = []
    res = svc.dispatch("echo hi", workers, lambda h, l: seen.append((h, l)))
    assert len(res) == 3 and all(r.code == 0 for r in res) and len(seen) == 9 and {h for h, _ in seen} == {"w01", "w02", "w03"}
    stop = threading.Event()
    stop.set()
    res = svc.dispatch("sleep 1", workers, lambda h, l: None, stop)
    assert all(r.code == 143 for r in res)
    with pytest.raises(ProviderError):
        svc.dispatch("x", [], lambda h, l: None)


def test_workloads_merge_builtin_and_config(svc):
    names = [w["name"] for w in svc.workloads()]
    assert "Smoke test" in names and "Unit tests" in names


# ------------------------------------------------------------------ remote controller API
@pytest.fixture
def api():
    p = providers.get("demo")
    srv = remote.make_server(p, 0)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    yield p, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def req(url, path, token=None, method="GET", body=None):
    r = urllib.request.Request(url + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                               headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_remote_requires_a_valid_token_and_stores_only_hashes(api):
    p, url = api
    assert req(url, "/v1/status")[0] == 401
    assert req(url, "/v1/status", "cdk_wrong")[0] == 401
    tok = remote.create_token("laptop", "read")
    assert tok not in (ddc.STATE_DIR / "clouddeck-tokens.json").read_text()          # only a hash on disk
    code, out = req(url, "/v1/status", tok)
    assert code == 200 and out["provider"] == "demo" and len(out["instances"]) == 4 and all(i["managed"] for i in out["instances"])   # the external deployment stays private
    assert remote.revoke_token("laptop") and req(url, "/v1/status", tok)[0] == 401


def test_remote_scopes_and_caps(api):
    p, url = api
    read, fleet = remote.create_token("r", "read"), remote.create_token("f", "fleet")
    assert req(url, "/v1/scale", read, "POST", {"n": 1})[0] == 403                   # read cannot mutate
    assert req(url, "/v1/scale", fleet, "POST", {"n": 99})[0] == 400                 # remote_max_workers / max_workers cap (4)
    assert req(url, "/v1/scale", fleet, "POST", {"n": 5, "preset": "32"})[0] == 400
    code, out = req(url, "/v1/scale", fleet, "POST", {"n": 4, "preset": "32"})
    assert code == 200 and out["workers"] == 4
    code, out = req(url, "/v1/dispatch", fleet, "POST", {"command": "hostname"})
    assert code == 200 and len(out["results"]) == 4 and out["results"][0]["code"] == 0
    assert req(url, "/v1/dispatch", fleet, "POST", {"command": ""})[0] == 400
    assert req(url, "/v1/checkpoint", fleet, "POST", {})[0] == 200
    # nothing in the API can terminate or deploy, and unknown routes are 404
    for path in ("/v1/terminate", "/v1/deploy", "/v1/config", "/v1/secrets"):
        assert req(url, path, fleet, "POST", {})[0] == 404
    assert not [c for c in p.calls if c[0] == "terminate"]
    log = (ddc.STATE_DIR / "remote-audit.log").read_text()
    assert " r POST /v1/scale -> 403 " in log and "-> 200" in log


def test_remote_server_binds_loopback_only(api):
    p, url = api
    assert url.startswith("http://127.0.0.1:")
    srv = remote.make_server(p, 0)
    assert srv.server_address[0] == "127.0.0.1"
    srv.server_close()


def test_remote_client_roundtrip(api, capsys, monkeypatch):
    p, url = api
    monkeypatch.setenv("CLOUDDECK_TOKEN", remote.create_token("c", "fleet"))
    assert remote_client.main(["status"], url=url) == 0
    out = capsys.readouterr().out
    assert "demo:" in out and "(external)" not in out and "w01" in out
    assert remote_client.main(["scale", "4"], url=url) == 0
    assert remote_client.main(["dispatch", "echo", "hi"], url=url) == 0
    assert "w01" in capsys.readouterr().out
    monkeypatch.setenv("CLOUDDECK_TOKEN", "cdk_bad")
    with pytest.raises(SystemExit, match="401"):
        remote_client.main(["status"], url=url)


# ------------------------------------------------------------------ SAF-08: looking at a box must not keep it awake
def _find_bash():
    import shutil
    b = shutil.which("bash")
    if os.name == "nt" and (not b or "system32" in b.lower()):   # System32\bash.exe is the WSL launcher
        b = next((p for p in ("C:/Program Files/Git/bin/bash.exe", "C:/Program Files (x86)/Git/bin/bash.exe") if Path(p).exists()), None)
    return b


BASH = _find_bash()
LEASE = Path(__file__).resolve().parent.parent / "lease"
needs_bash = pytest.mark.skipif(not BASH, reason="needs bash")


def px(p):
    s = str(p).replace("\\", "/")
    return re.sub(r"^([A-Za-z]):", lambda m: "/" + m.group(1).lower(), s) if os.name == "nt" else s


def fake_ps(listing_busy_format, listing_cpu_format):
    """A `ps` stand-in as a shell function (a function wins over any ps on PATH, and needs no file): the lease agent asks for
    `pid= comm= args=`, the probe for `pcpu= args=`."""
    import shlex

    def out(lines):
        return "printf '%s\\n' " + " ".join(shlex.quote(x) for x in lines)
    return 'ps() { case "$*" in *pcpu=*) ' + out(listing_cpu_format) + ' ;; *) ' + out(listing_busy_format) + ' ;; esac; }; '


def test_probe_command_has_no_workload_specific_words():
    for word in ("vitest", "stryker", "wine", "campaign", "pgrep", "cd:probe'"):
        assert word not in PROBE_CMD, word
    assert PROBE_CMD.startswith("# cd:probe") and "job=" in PROBE_CMD


@needs_bash
def test_the_probe_is_not_work_for_the_lease_agent_and_names_the_real_job():
    probe = "bash -c " + PROBE_CMD.replace("\n", " ")                       # how sshd runs it on the box
    busy_fmt = ["101 bash " + probe, "102 sshd sshd: dev@notty", "103 sleep sleep 0.5", "104 awk awk -v a=1 BEGIN{print}", "105 ps ps -eo pcpu=,args="]
    cpu_fmt = ["1.0 /usr/bin/awk awk x", "0.5 bash -c " + PROBE_CMD.replace("\n", " "), "0.1 sshd: dev@notty"]
    lib = f'. "{px(LEASE)}/ddc-lib.sh"; ddc_busy_procs "$DDC_BUSY_DEFAULT"'
    cp = subprocess.run([BASH, "-c", fake_ps(busy_fmt, cpu_fmt) + lib], capture_output=True)
    assert cp.returncode == 1, cp.stderr                                                          # not busy: our own probe does not count
    # the job column: the top CPU user that is not a wrapper (here node, found among the wrappers), nothing when the box is idle
    job_line = next(line for line in PROBE_CMD.splitlines() if line.startswith("echo job="))
    cpu_fmt = ["0.2 sshd: dev@notty", "93.0 node /w/app/build.js --prod", "40.0 bash -c sleep 100", "12.0 ssh w01 hostname"]
    out = subprocess.run([BASH, "-c", fake_ps(busy_fmt, cpu_fmt) + job_line], capture_output=True, text=True).stdout.strip()
    assert out == "job=node /w/app/build.js --prod"
    idle = subprocess.run([BASH, "-c", fake_ps(busy_fmt, ["0.2 sshd: dev@notty", "3.0 python3 idle.py"]) + job_line], capture_output=True, text=True).stdout.strip()
    assert idle == "job="


def test_demo_probe_and_workspace_data_are_generic(svc):
    insts = svc.provider.instances()
    jobs = {p.job for p in svc.probe_all(insts).values()}
    assert not any(w in j for j in jobs for w in ("vitest", "stryker"))
    ws = svc.workspaces(insts[0])
    assert "feat/search" in {w.branch for w in ws.values()}


# ------------------------------------------------------------------ dispatch keeps the dev box's worker-idle timer honest (SAF-24)
def watch_heartbeat(svc, monkeypatch):
    """Record every touch of the marker and when the heartbeat thread ends, without any sleeping: `ended` is set by the thread itself."""
    import clouddeck.service as service
    monkeypatch.setattr(service, "HEARTBEAT_S", 0.01)
    beats, ended, order = [], threading.Event(), []
    real_run, real_beat = svc.provider.run, svc._heartbeat

    def run(inst, cmd, timeout=60):
        if cmd == service.HEARTBEAT_CMD:
            beats.append(inst.role)
            order.append("beat")
        return real_run(inst, cmd, timeout)
    monkeypatch.setattr(svc.provider, "run", run)

    def heartbeat(done):
        try:
            real_beat(done)
        finally:
            ended.set()
    monkeypatch.setattr(svc, "_heartbeat", heartbeat)
    return service, beats, ended, order


def test_dispatch_to_workers_touches_the_activity_marker_during_and_after(svc, monkeypatch):
    service, beats, ended, order = watch_heartbeat(svc, monkeypatch)
    p = svc.provider
    workers = [i for i in p.instances() if i.role == "worker"]
    real_stream = p.stream

    def stream(inst, cmd, on_line, stop=None, timeout=3600):
        end = time.time() + 30
        while len(beats) < 3 and time.time() < end:    # the job keeps running until the heartbeat has demonstrably beaten several times
            time.sleep(0.005)
        return real_stream(inst, cmd, on_line, stop, timeout)
    monkeypatch.setattr(p, "stream", stream)
    svc.dispatch("echo hi", workers, lambda h, l: order.append("line"))
    assert ended.wait(30)                                      # the heartbeat stops with the job
    n = len(beats)
    assert n >= 3 and set(beats) == {"dev"} and service.HEARTBEAT_CMD.startswith("touch /run/ddc/worker_activity")
    assert order[-1] == "beat" and order.index("line") < len(order) - 1      # one last touch after the output: the idle timer starts from the end of the job
    assert len(beats) == n                                      # and nothing after that


def test_dispatch_to_the_dev_box_alone_starts_no_heartbeat(svc, monkeypatch):
    service, beats, ended, order = watch_heartbeat(svc, monkeypatch)
    svc.dispatch("echo hi", [i for i in svc.provider.instances() if i.role == "dev"], lambda h, l: None)
    assert not ended.is_set() and not beats                    # the thread never started: the agent sees commands on the dev box itself


def test_on_akash_the_heartbeat_goes_through_the_controllers_own_marker_touch(svc, monkeypatch):
    """SAF-24: the controller owns what 'worker activity' means (ddc.touch_worker_activity); CloudDeck only calls it."""
    service, beats, ended, order = watch_heartbeat(svc, monkeypatch)
    touched = []
    monkeypatch.setattr(ddc, "touch_worker_activity", lambda d: touched.append(d))
    monkeypatch.setattr(svc.provider, "id", "akash", raising=False)
    svc.dispatch("echo hi", [i for i in svc.provider.instances() if i.role == "worker"], lambda h, l: None)
    assert ended.wait(30)
    dev = next(i for i in svc.provider.instances() if i.role == "dev")
    assert len(touched) >= 2 and all(t == {"host": dev.host, "port": dev.port} for t in touched) and not beats     # not through the provider's generic run


def test_a_heartbeat_that_fails_never_breaks_the_job(svc, monkeypatch):
    service, beats, ended, order = watch_heartbeat(svc, monkeypatch)

    def boom(d):
        raise OSError("ssh is down")
    monkeypatch.setattr(ddc, "touch_worker_activity", boom)
    monkeypatch.setattr(svc.provider, "id", "akash", raising=False)
    res = svc.dispatch("echo hi", [i for i in svc.provider.instances() if i.role == "worker"], lambda h, l: None)
    assert ended.wait(30) and all(r.code == 0 for r in res)


def test_seed_refuses_unknown_projects_and_a_missing_dev_box(svc, monkeypatch, tmp_path):
    dev = svc.provider.instances()[0]
    called = []
    monkeypatch.setattr(ddc, "seed", lambda *a, **k: called.append(a))
    monkeypatch.setattr(svc.provider, "id", "akash", raising=False)
    with pytest.raises(ProviderError, match="unknown project"):                  # ddc.seed() would skip it silently and we would report success
        svc.seed_missing(dev, "nope")
    svc.add_project("fresh", str(tmp_path))
    monkeypatch.setattr(ddc, "dev_box", lambda: None)
    with pytest.raises(ProviderError, match="no dev box"):
        svc.seed_missing(dev, "fresh")
    assert not called
    box = {"host": dev.host, "port": dev.port}
    monkeypatch.setattr(ddc, "dev_box", lambda: box)
    assert svc.seed_missing(dev, "fresh") == "seeded fresh" and called == [(box, ["fresh"])]


def test_config_edits_cannot_make_the_config_unsafe(svc, isolated):
    before = isolated.read_bytes()
    with pytest.raises(ProviderError, match="refusing to write config.toml.*max_workers"):
        svc._edit_config(lambda doc: doc.__setitem__("max_workers", 50))        # above the controller's hard ceiling
    assert isolated.read_bytes() == before and not isolated.with_suffix(".toml.tmp").exists()
    isolated.write_text(isolated.read_text().replace("max_workers = 4", "max_workers = 50"), encoding="utf-8")   # a problem that was already there
    svc.set_tunable("idle_minutes", 60)                                          # does not block an unrelated edit
    assert "idle_minutes = 60" in isolated.read_text()


def test_projects_take_only_known_environments_and_real_github_names(svc, tmp_path):
    for env in ("nope", "ml;x"):
        with pytest.raises(ProviderError, match="unknown environment"):
            svc.add_project("p2", str(tmp_path), environment=env)
        with pytest.raises(ProviderError, match="unknown environment"):
            svc.set_project_environment("notes", env)
    for gh in ("-bad/x", "a b/c", "x/y;z", "/x", "x/"):
        with pytest.raises(ProviderError, match="github"):
            svc.add_project("p3", str(tmp_path), github=gh)
    assert svc.add_project("p4", str(tmp_path), github="me/ok.repo-1", environment="ml").environment == "ml"


def test_seed_refuses_a_name_that_is_not_a_plain_project_name(svc, monkeypatch):
    dev = svc.provider.instances()[0]
    called = []
    monkeypatch.setattr(ddc, "seed", lambda *a, **k: called.append(a))
    for bad in ("x; touch /tmp/pwned #", "", "../etc", "A B"):
        with pytest.raises(ProviderError, match="not a valid project name"):
            svc.seed_missing(dev, bad)
    assert not called


# ------------------------------------------------------------------ CLI: doctor and friendly failures
def run_cli(capsys, *argv):
    from clouddeck import cli
    code = cli.main(list(argv))
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def no_config(monkeypatch):
    monkeypatch.setattr(ddc, "CONFIG_FILE", None)
    monkeypatch.setattr(ddc, "CONFIG_ERROR", "no config found: run `ddc init`, or set DDC_CONFIG")


def offline(monkeypatch):
    def boom(*a, **k):
        raise OSError("offline test")
    monkeypatch.setattr(ddc, "public_get", boom)
    monkeypatch.setattr(ddc, "balance_usd", boom)
    monkeypatch.setattr(ddc, "watchdog_installed", lambda: True)   # the real one asks the OS scheduler: never from a test


def test_doctor_without_a_config_warns_for_demo_and_fails_with_a_fix_for_real_providers(capsys, monkeypatch):
    no_config(monkeypatch)
    code, out, _ = run_cli(capsys, "doctor", "-p", "demo")
    assert code == 0 and "WARN  config: none found" in out and "FAIL" not in out and "Traceback" not in out
    assert run_cli(capsys, "-p", "demo", "doctor")[0] == 0                       # -p before or after the subcommand
    offline(monkeypatch)
    code, out, _ = run_cli(capsys, "doctor")
    assert code == 1 and "FAIL  config" in out and "run `ddc init`" in out and "Traceback" not in out


def test_doctor_reports_the_spend_guard_and_a_damaged_state_file(capsys, monkeypatch):
    offline(monkeypatch)
    ddc.CFG["budget_guard"] = True
    out = run_cli(capsys, "doctor")[1]
    assert "PASS  budget_guard = True" in out and "leases" in out
    ddc.CFG["budget_guard"] = False
    out = run_cli(capsys, "doctor")[1]
    assert "PASS  budget_guard" not in out and "WARN  budget_guard = false" in out and "set budget_guard = true" in out     # off is never a PASS

    def damaged():
        raise ddc.DdcError(f"state file {ddc.STATE_FILE} is damaged (invalid JSON)")
    monkeypatch.setattr(ddc, "load_state", damaged)
    code, out, _ = run_cli(capsys, "doctor")
    assert code == 1 and "FAIL  state file" in out and "damaged" in out and "ddc reconcile" in out and "Traceback" not in out


def test_doctor_reports_config_problems_warnings_and_how_to_fix_them(capsys, monkeypatch):
    offline(monkeypatch)
    ddc.CFG["max_workers"] = 99                                                  # above the hard ceiling: the controller refuses it too
    ddc.CFG["state_repo"] = ""
    ddc.CFG["push_claude_credentials"] = True
    ddc.CFG["budget_guard"] = False                                              # (the fixture has it on)
    code, out, _ = run_cli(capsys, "doctor")
    assert code == 1
    assert "FAIL  config problem: max_workers" in out and f"Fix: edit {ddc.CONFIG_FILE}" in out
    assert "WARN  state_repo is empty" in out and "then run `ddc install`" in out
    assert "WARN  push_claude_credentials = true" in out and "set push_claude_credentials = false" in out
    assert "WARN  image is a mutable tag" in out and "@sha256" in out and "WARN  budget_guard = false" in out
    code, out, _ = run_cli(capsys, "doctor", "-p", "demo")                       # the simulated provider: the same findings, but nothing fails
    assert code == 0 and "WARN  config problem: max_workers" in out and "FAIL" not in out and "WARN  state_repo is empty" in out
    ddc.CFG["max_workers"] = 4
    ddc.CFG["state_repo"] = "OWNER/your-private-state-repo"
    out = run_cli(capsys, "doctor", "-p", "demo")[1]
    assert "WARN  state_repo = OWNER/your-private-state-repo" in out and "documentation placeholder" in out


def test_doctor_says_whether_the_watchdog_is_installed(capsys, monkeypatch):
    offline(monkeypatch)
    monkeypatch.setattr(ddc, "watchdog_installed", lambda: True)
    assert "PASS  watchdog installed" in run_cli(capsys, "doctor")[1]
    monkeypatch.setattr(ddc, "watchdog_installed", lambda: None)
    assert "WARN  watchdog: cannot tell" in run_cli(capsys, "doctor")[1]

    def broken():
        raise OSError("scheduler unavailable")
    monkeypatch.setattr(ddc, "watchdog_installed", broken)
    code, out, _ = run_cli(capsys, "doctor")
    assert "WARN  watchdog: cannot tell" in out and "Traceback" not in out
    monkeypatch.setattr(ddc, "watchdog_installed", lambda: False)
    monkeypatch.setattr(ddc, "load_state", lambda: {"deps": {}})
    out = run_cli(capsys, "doctor")[1]                                           # nothing running: a warning with the fix
    assert "WARN  watchdog is NOT installed" in out and "run `ddc install`" in out
    monkeypatch.setattr(ddc, "load_state", lambda: {"deps": {"1": {"status": "ready"}}})
    code, out, _ = run_cli(capsys, "doctor")                                     # something is billing and nothing will close it: a failure
    assert code == 1 and "FAIL  watchdog is NOT installed" in out
    assert "watchdog" not in run_cli(capsys, "doctor", "-p", "demo")[1]


def test_commands_fail_with_one_clear_line_not_a_traceback(capsys, monkeypatch):
    no_config(monkeypatch)
    code, out, err = run_cli(capsys, "status")
    assert code == 1 and "ddc init" in err and "clouddeck -p demo" in err and "Traceback" not in err
    code, out, err = run_cli(capsys, "-p", "demo", "status")
    assert code == 0 and "Demo (simulated)" in out                               # the simulated provider needs no config

    class Damaged(ddc.DdcError):                                                  # a subclass, as a damaged-state error would be
        pass

    def boom(*a, **k):
        raise Damaged("state file is damaged; nothing was changed")
    monkeypatch.setattr(ddc, "CONFIG_ERROR", None)
    monkeypatch.setattr(providers.get("demo").__class__, "spend", boom)
    code, out, err = run_cli(capsys, "-p", "demo", "status")
    assert code == 1 and err.strip() == "error: state file is damaged; nothing was changed"

    def bug(*a, **k):
        raise ZeroDivisionError("oops")
    monkeypatch.setattr(providers.get("demo").__class__, "spend", bug)
    code, out, err = run_cli(capsys, "-p", "demo", "status")
    assert code == 1 and "ZeroDivisionError: oops" in err and "CLOUDDECK_DEBUG" in err and "Traceback" not in err
    monkeypatch.setenv("CLOUDDECK_DEBUG", "1")
    with pytest.raises(ZeroDivisionError):
        run_cli(capsys, "-p", "demo", "status")


@pytest.fixture
def damaged_state():
    ddc.STATE_FILE.write_text('{"deps": {"x": ', encoding="utf-8")
    yield
    for p in ddc.STATE_DIR.glob("state.*"):
        if p.name != "state.json" or p.exists():
            p.unlink(missing_ok=True)


def test_a_really_damaged_state_file_reaches_the_user_as_one_helpful_message(capsys, monkeypatch, damaged_state):
    offline(monkeypatch)
    code, out, _ = run_cli(capsys, "doctor")
    assert code == 1 and "FAIL  state file" in out and "will NOT create leases" in out and "Traceback" not in out
    code, out, err = run_cli(capsys, "status")
    assert code == 1 and err.startswith("error: ") and "ddc reconcile" in err and "Traceback" not in err

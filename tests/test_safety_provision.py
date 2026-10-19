"""Safe provisioning: no blind POST/DELETE retries, write-ahead + reconcile (never a duplicate), rollback of half-created leases,
provider-controlled host/port validation, preflight before any lease, shell/env quoting."""
import io
import subprocess
import types
import urllib.error

import pytest

from clouddeck.engine import ddc

_REAL_PREFLIGHT = ddc.preflight          # the `fake` fixture replaces ddc.preflight; tests that want the real one put this back


# ------------------------------------------------------------------ the real api(): retry rules (urlopen is faked, nothing leaves the machine)
class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def wire(monkeypatch):
    """Script of responses for ddc.api(): each item is an exception to raise or bytes to return. Counts the requests."""
    calls, script = [], []
    monkeypatch.setattr(ddc, "load_secrets", lambda: {"AKASH_API_KEY": "k"})
    monkeypatch.setattr(ddc.time, "sleep", lambda s: None)

    def urlopen(req, timeout=None):
        calls.append((req.get_method(), req.full_url))
        item = script.pop(0)
        if isinstance(item, Exception):
            raise item
        return _Resp(item)
    monkeypatch.setattr(ddc.urllib.request, "urlopen", urlopen)
    return calls, script


def http_error(code, body=b"boom"):
    return urllib.error.HTTPError("http://x", code, "m", {}, io.BytesIO(body))


def test_post_is_never_retried_after_a_timeout_or_5xx_and_says_so(wire):
    calls, script = wire
    for err in (TimeoutError("timed out"), urllib.error.URLError("reset"), http_error(503), http_error(500), ConnectionResetError()):
        calls.clear()
        script[:] = [err, b'{"data": {"dseq": "1"}}']          # a second response is available: it must NOT be used
        with pytest.raises(ddc.ApiUncertain, match="may have been executed"):
            ddc.api("POST", "/v1/deployments", {"data": {}})
        assert len(calls) == 1, err


def test_delete_is_not_retried_blindly_either(wire):
    calls, script = wire
    script[:] = [http_error(502), b"{}"]
    with pytest.raises(ddc.ApiUncertain):
        ddc.api("DELETE", "/v1/deployments/5")
    assert len(calls) == 1


def test_a_definitive_4xx_is_a_plain_error_and_an_unreadable_reply_to_a_post_is_uncertain(wire):
    calls, script = wire
    script[:] = [http_error(400, b"bad sdl")]
    with pytest.raises(ddc.DdcError, match="HTTP 400") as e:
        ddc.api("POST", "/v1/deployments", {})
    assert not isinstance(e.value, ddc.ApiUncertain)
    script[:] = [b"<html>gateway</html>"]
    with pytest.raises(ddc.ApiUncertain, match="unreadable"):
        ddc.api("POST", "/v1/deployments", {})


def test_get_is_retried_and_429_is_retried_for_every_method(wire):
    calls, script = wire
    script[:] = [TimeoutError(), http_error(503), b'{"data": 1}']
    assert ddc.api("GET", "/v1/balances") == {"data": 1} and len(calls) == 3
    calls.clear()
    script[:] = [http_error(429), b'{"ok": true}']              # rate limited = rejected before processing: safe to repeat
    assert ddc.api("POST", "/v1/leases", {}) == {"ok": True} and len(calls) == 2
    calls.clear()
    script[:] = [TimeoutError()] * 4
    with pytest.raises(ddc.DdcError):
        ddc.api("GET", "/v1/balances")
    assert len(calls) == 4


# ------------------------------------------------------------------ write-ahead + reconcile: a timed-out create is never duplicated
def posts(fake):
    return len([c for c in fake.calls if c == ("POST", "/v1/deployments")])


def test_timeout_after_the_server_created_it_is_reconciled_not_duplicated(fake):
    fake.fail_next("POST /v1/deployments", "timeout_after")
    with pytest.raises(ddc.DdcError, match="found and closed again"):
        ddc.create_box("dev", "32")
    assert posts(fake) == 1                                       # exactly one POST, no blind retry
    assert len(fake.deployments) == 1 and not fake.open_dseqs()   # the one that was created is closed again
    st = ddc.load_state()
    (dseq, rec), = st["deps"].items()
    assert rec["ours"] and rec["status"] == "closed" and rec["name"].startswith("ddc-dev-32-") and not st.get("pending")
    assert [e["event"] for e in ddc.ledger_read()[0]] == ["created", "closed"]


def test_timeout_before_the_server_saw_it_keeps_a_pending_record_and_does_not_retry(fake):
    fake.fail_next("POST /v1/deployments", "timeout_before")
    with pytest.raises(ddc.OutcomeUnknown, match="NOT retried"):
        ddc.create_box("dev", "32")
    assert posts(fake) == 1 and not fake.deployments
    pend = ddc.pending_creations()
    (name, rec), = pend.items()
    assert rec["role"] == "dev" and rec["max_usd_hr"] == 0.9
    assert ddc.budget_view()["committed"] == pytest.approx(0.9 * 8, rel=1e-2)   # counted at its cap until resolved
    assert ddc.reconcile_pending() == []                                    # too young to drop, nothing found
    fake.clock += ddc.PENDING_GRACE_S + 60
    assert "dropped" in ddc.reconcile_pending()[0] and not ddc.pending_creations()


def test_a_deployment_that_shows_up_later_is_adopted_and_closed_by_reconcile(fake):
    fake.fail_next("POST /v1/deployments", "timeout_before")
    with pytest.raises(ddc.OutcomeUnknown):
        ddc.create_box("worker", "32", 3, limit_h=4)
    (name, _), = ddc.pending_creations().items()
    fake.deployments["1000000000777"] = {"name": name, "owner": "akash1fakeowner", "state": "active", "t0": fake.clock}   # it did get created after all
    msgs = ddc.reconcile_pending()
    assert any("recorded as ours" in m for m in msgs) and not fake.open_dseqs()
    rec = ddc.load_state()["deps"]["1000000000777"]
    assert rec["ours"] and rec["role"] == "worker" and rec["status"] == "closed" and not ddc.pending_creations()


def test_when_the_listing_also_fails_the_record_stays_and_the_message_names_reconcile(fake):
    fake.fail_next("POST /v1/deployments", "timeout_before")
    fake.fail_next("GET /v1/deployments", "error")
    with pytest.raises(ddc.OutcomeUnknown, match="ddc reconcile"):
        ddc.create_box("dev", "32")
    assert len(ddc.pending_creations()) == 1 and posts(fake) == 1


def test_a_definitive_rejection_clears_the_pending_record(fake):
    fake.fail_next("POST /v1/deployments", "reject")
    with pytest.raises(ddc.DdcError, match="HTTP 400"):
        ddc.create_box("dev", "32")
    assert not ddc.pending_creations() and not fake.deployments


def test_start_never_retries_after_an_unknown_outcome_or_a_failed_cleanup(fake):
    args = types.SimpleNamespace(profile="32", workers=0, worker_profile=None)
    fake.fail_next("POST /v1/deployments", "timeout_before")
    with pytest.raises(ddc.OutcomeUnknown):
        ddc.cmd_start(args)
    assert posts(fake) == 1
    ddc.write_json(ddc.STATE_FILE, {"deps": {}})
    fake.deployments.clear()
    fake.fail_next("POST /v1/leases", "reject")
    fake.fail_next("DELETE /v1/deployments/", "error")             # and the cleanup of the half-created lease fails too
    n = posts(fake)
    with pytest.raises(ddc.RollbackFailed, match="ddc stop"):
        ddc.cmd_start(args)
    assert posts(fake) == n + 1                                    # no second box while the first may still be billing
    assert len(fake.open_dseqs()) == 1 and ddc.active_deps()      # still recorded, so stop/watchdog can close it


def test_start_retries_a_plain_provisioning_failure_once_with_a_fresh_budget_check(fake):
    fake.providers.clear()                                         # nobody bids
    n = posts(fake)
    with pytest.raises(ddc.DdcError, match="no acceptable bids"):
        ddc.cmd_start(types.SimpleNamespace(profile="32", workers=0, worker_profile=None))
    assert posts(fake) == n + 2 and not fake.open_dseqs()          # 2 attempts, both cleaned up
    assert len([c for c in fake.calls if c == ("GET", "/v1/balances")]) >= 3   # the budget was re-checked for the retry


# ------------------------------------------------------------------ rollback of half-created leases
def test_failed_lease_closes_only_the_deployment_this_call_created(fake):
    foreign = fake.add_foreign()
    other = fake.add_foreign("1000000000002", "ddc-looks-ours-but-is-not-recorded")
    fake.fail_next("POST /v1/leases", "reject")
    with pytest.raises(ddc.DdcError, match="HTTP 400"):
        ddc.create_box("worker", "32", 1, {}, limit_h=4)
    assert sorted(fake.open_dseqs()) == [foreign, other]           # neither untouched lease was closed
    (dseq, rec), = ddc.load_state()["deps"].items()
    assert rec["status"] == "closed" and fake.deployments[dseq]["state"] == "closed"


def test_lease_post_that_timed_out_but_went_through_continues_and_one_that_did_not_is_rolled_back(fake):
    fake.fail_next("POST /v1/leases", "timeout_after")
    box = ddc.create_box("worker", "32", 1, {}, limit_h=4)
    assert box["status"] == "ready" and fake.open_dseqs() == [box["dseq"]]
    fake.fail_next("POST /v1/leases", "timeout_before")
    with pytest.raises(ddc.ApiUncertain):
        ddc.create_box("worker", "32", 2, {}, limit_h=4)
    assert fake.open_dseqs() == [box["dseq"]]                      # the half-created second lease was closed


def test_ctrl_c_while_waiting_for_bids_closes_the_new_lease(fake):
    fake.fail_next("GET /v1/bids", "interrupt")
    with pytest.raises(KeyboardInterrupt):
        ddc.create_box("worker", "32", 1, {}, limit_h=4)
    assert not fake.open_dseqs()


def test_ssh_never_coming_up_rolls_back(fake, monkeypatch):
    monkeypatch.setattr(ddc, "ssh", lambda *a, **k: "")
    with pytest.raises(ddc.DdcError, match="ssh never came up"):
        ddc.create_box("worker", "32", 1, {}, limit_h=4)
    assert not fake.open_dseqs()


def test_worker_that_fails_to_provision_is_closed_not_left_billing(fake, monkeypatch):
    ddc.write_json(ddc.STATE_FILE, {"deps": {"d": {"ours": True, "name": "ddc-dev-32-x", "role": "dev", "status": "ready", "provider": "p", "host": "dev.example",
                                                  "port": 22, "usd_hr": 0.3, "lease_at": fake.clock, "limit_h": 8}}})
    monkeypatch.setattr(ddc, "provision_worker", lambda w: (_ for _ in ()).throw(ddc.DdcError("setup.sh failed")))
    ddc.cmd_scale(types.SimpleNamespace(n=1, profile="32"))
    assert not fake.open_dseqs() and not ddc.workers()


# ------------------------------------------------------------------ provider-controlled host / port
BAD_HOSTS = ["evil\nProxyCommand curl x|sh", "a b", "host;rm -rf /", "$(id)", "x`id`", "-oProxyCommand=x", "", "a" * 300, "h'ost", "fe80::1%eth0", "..", "a..b", "-a.com"]


@pytest.mark.parametrize("host", BAD_HOSTS)
def test_hostile_provider_host_is_rejected_and_the_lease_closed(fake, host):
    fake.next_host = host
    with pytest.raises(ddc.DdcError, match="unusable ssh endpoint|never exposed ssh"):
        ddc.create_box("worker", "32", 1, {}, limit_h=4)
    assert not fake.open_dseqs()
    assert not [c for h, c in fake.ssh_log if "setup.sh" in c or "authorized" in c]     # nothing was ever sent to that host


def test_host_and_port_validators():
    for h in ("provider.example.com", "10.0.0.7", "2001:db8::1", "a-b.c1", "localhost", "w01"):
        assert ddc.valid_host(h), h
    for p in (22, "30022", 65535):
        assert ddc.valid_port(p), p
    for p in (0, 65536, -1, "22; id", "２２", True, None, 22.5, "", "0x16"):
        assert not ddc.valid_port(p), p


def test_stored_hostile_endpoint_cannot_reach_ssh_config_or_commands(fake):
    bad = {"idx": 1, "host": "w.example\n  ProxyCommand evil", "port": 22, "name": "ddc-w01-32-x", "dseq": "9"}
    with pytest.raises(ddc.DdcError, match="refusing to use the ssh endpoint"):
        ddc.ssh_args(bad)
    with pytest.raises(ddc.DdcError):
        ddc.ssh_args({**bad, "host": "ok.example", "port": "22 -o ProxyCommand=x"})
    ddc.write_json(ddc.STATE_FILE, {"deps": {
        "d": {"ours": True, "name": "ddc-dev-32-x", "role": "dev", "status": "ready", "host": "dev.example", "port": 22},
        "w": {"ours": True, "name": "ddc-w01-32-x", "role": "worker", "status": "ready", "idx": 1, "host": bad["host"], "port": 22}}})
    fake.ssh_log.clear()
    with pytest.raises(ddc.DdcError):
        ddc.sync_worker_hosts()
    assert not fake.ssh_log                                         # nothing was written to the dev box's ssh config


def test_heal_quotes_and_only_dry_run(fake, monkeypatch):
    t = fake.clock
    ddc.write_json(ddc.STATE_FILE, {"deps": {
        "d": {"ours": True, "name": "ddc-dev-32-x", "role": "dev", "status": "ready", "host": "dev.example", "port": 22, "lease_at": t},
        "1": {"ours": True, "name": "ddc-w01-32-x", "role": "worker", "status": "ready", "idx": 1, "host": "w1.example", "port": 30001, "lease_at": t},
        "2": {"ours": True, "name": "ddc-w02-32-x", "role": "worker", "status": "ready", "idx": 2, "host": "w2.example", "port": 30002, "lease_at": t}}})
    fake.patch_subprocess()
    fake.restarted.update({"w1.example", "w2.example"})
    provisioned = []
    monkeypatch.setattr(ddc, "provision_worker", lambda w: provisioned.append(w["name"]))
    assert ddc.cmd_heal(types.SimpleNamespace(only=["w02"], dry_run=True)) == ["w02"] and not provisioned
    assert ddc.cmd_heal(types.SimpleNamespace(only=["2"], dry_run=False)) == ["w02"] and provisioned == ["ddc-w02-32-x"]
    ssh_keygen_calls = [c for h, c in fake.ssh_log if "known_hosts_workers" in c]
    assert ssh_keygen_calls and "'[w2.example]:30002'" in ssh_keygen_calls[0]


# ------------------------------------------------------------------ preflight: nothing is created when the local machine cannot finish the job
def test_preflight_runs_before_any_api_call(fake, monkeypatch, tmp_path):
    monkeypatch.setattr(ddc, "preflight", _REAL_PREFLIGHT)
    monkeypatch.setattr(ddc.shutil, "which", lambda exe, *a, **k: None)
    ddc.CFG["repos"] = [{"name": "ghost", "path": str(tmp_path / "nope")}]
    (ddc.SECRETS_DIR / "secrets.env").write_text("")
    for k in ("access", "state_deploy"):
        (ddc.KEYS / k).unlink(missing_ok=True)
    with pytest.raises(ddc.DdcError) as e:
        ddc.create_box("dev", "32")
    msg = str(e.value)
    for needle in ("`ssh` was not found", "`git` was not found", "`gpg` was not found", "AKASH_API_KEY", "DDC_BACKUP_PASSPHRASE", "ghost", "access", "state_deploy"):
        assert needle in msg, (needle, msg)
    assert not fake.calls                                           # no API call, no lease


def test_preflight_is_quiet_when_everything_is_there_and_skips_gpg_without_checkpoints(fake, monkeypatch):
    monkeypatch.setattr(ddc, "preflight", _REAL_PREFLIGHT)
    monkeypatch.setattr(ddc.shutil, "which", lambda exe, *a, **k: None if "gpg" in str(exe) else exe)
    ddc.CFG["state_repo"] = ""
    assert ddc.preflight("dev") == []
    ddc.CFG["state_repo"] = "o/state"
    assert any("gpg" in p for p in ddc.preflight("dev"))
    assert not [p for p in ddc.preflight("worker") if "state_deploy" in p or "BACKUP" in p]       # a worker needs far less than the dev box


# ------------------------------------------------------------------ remote shell / lease env quoting
def test_put_file_quotes_the_path_and_validates_the_mode(fake):
    d = {"host": "h.example", "port": 22, "dseq": "1"}
    ddc.put_file(d, "/home/dev/it's a path; touch /tmp/x", b"data")
    cmd = fake.ssh_log[-1][1]
    assert "'/home/dev/it'\"'\"'s a path; touch /tmp/x'" in cmd and "; touch /tmp/x " not in cmd.replace("'", "")[:5]
    with pytest.raises(ddc.DdcError, match="invalid file mode"):
        ddc.put_file(d, "/x", b"", mode="600; id")


def test_seed_refuses_hostile_names_and_moves_existing_dirs_instead_of_deleting(fake, tmp_path):
    src = tmp_path / "notes"
    src.mkdir()
    (src / "a.txt").write_text("hi")
    d = {"host": "h.example", "port": 22, "dseq": "1"}
    for bad in ("x; touch /tmp/pwned #", "", "a b", "$(id)"):
        ddc.CFG["dirs"] = [{"name": bad, "path": str(src)}]
        fake.ssh_log.clear()
        with pytest.raises(ddc.DdcError, match="refusing to seed"):
            ddc.seed(d, [bad])
        assert not fake.ssh_log
    ddc.CFG["dirs"] = [{"name": "notes", "path": str(src)}]
    ddc.seed(d, ["notes"])
    cmds = " ".join(c for h, c in fake.ssh_log)
    assert "rm -rf /work" not in cmds and "/work/.ddc/trash/notes-" in cmds and "tar -xzf -" in cmds
    ddc.CFG["repos"] = [{"name": "r", "path": str(src), "github": "o/r; id", "push": True}]
    with pytest.raises(ddc.DdcError, match="OWNER/NAME"):
        ddc.seed(d, ["r"])


def unquoted_env(text, name):
    """What bash puts into $name after sourcing the lease env file."""
    from test_lease_scripts import BASH as bash  # Git Bash on Windows, never the System32 WSL launcher (which one is first on PATH varies)
    if not bash:
        pytest.skip("bash not available")
    return subprocess.run([bash, "-c", f'eval "$(cat)"; printf %s "${name}"'], input=text.replace("\r", ""), capture_output=True, text=True).stdout


def test_lease_env_values_cannot_expand_or_run_code_on_the_lease(fake, tmp_path):
    nasty = 'echo "$HOME" `id` $(id) \\ done'
    ddc.CFG.update(bench_cmd=nasty, busy_regex="a$|b", bench_dir="my dir")
    text = ddc.lease_env("42")
    assert unquoted_env(text, "DDC_BENCH_CMD") == nasty and unquoted_env(text, "DDC_BUSY_REGEX") == "a$|b"
    assert unquoted_env(text, "DDC_DSEQ") == "42" and 'export DDC_DSEQ="42"\n' in text       # the shape setup.sh greps
    ddc.CFG["bench_cmd"] = "line1\nline2"
    with pytest.raises(ddc.DdcError, match="control character"):
        ddc.lease_env("1")


def test_lease_env_exports_the_new_knobs_and_gates_the_claude_login(fake):
    s = ddc.lease_env("1")
    assert 'export DDC_INSTALL_CLAUDE_CODE="1"' in s and "DDC_CLAUDE_CODE_VERSION" not in s and "DDC_BENCH_CMD" not in s and "DDC_SYNC_EXCLUDES" not in s
    (ddc.SECRETS_DIR / "secrets.env").write_text("AKASH_API_KEY=k\nCLAUDE_CODE_OAUTH_TOKEN=tok-secret\n")
    assert "tok-secret" not in ddc.lease_env("1") and ddc.push_claude_credentials() is False      # opt-in
    ddc.CFG.update(push_claude_credentials=True, install_claude_code=False, claude_code_version="2.1.292", bench_cmd="npm test", bench_dir="app",
                   sync_excludes=["dist", ".cache"], busy_regex="x")
    s = ddc.lease_env("1")
    for line in ('DDC_INSTALL_CLAUDE_CODE="0"', 'DDC_CLAUDE_CODE_VERSION="2.1.292"', 'DDC_BENCH_CMD="npm test"', 'DDC_BENCH_DIR="app"',
                 'DDC_SYNC_EXCLUDES="dist .cache"', 'DDC_BUSY_REGEX="x"', 'CLAUDE_CODE_OAUTH_TOKEN="tok-secret"'):
        assert f"export {line}\n" in s, line
    assert 'export DDC_STATE_REPO="example/state"' in s


def test_claude_login_file_is_only_pushed_when_opted_in(fake):
    (ddc.SECRETS_DIR / "claude-credentials.json").write_text('{"claudeAiOauth": {}}')
    d = {"host": "dev.example", "port": 22, "dseq": "1", "name": "ddc-dev-32-x"}
    ddc.provision_dev(d)
    assert not [1 for h, c in fake.ssh_log if "claude-credentials.json" in c]
    ddc.CFG["push_claude_credentials"] = True
    ddc.provision_dev(d)
    assert [1 for h, c in fake.ssh_log if "claude-credentials.json" in c]


def test_full_start_on_the_fake_provisions_one_dev_box_and_is_idempotent(fake, capsys):
    args = types.SimpleNamespace(profile="32", workers=0, worker_profile=None)
    ddc.cmd_start(args)
    dev = ddc.dev_box()
    assert dev and dev["name"].startswith("ddc-dev-32-") and dev["status"] == "ready" and len(fake.open_dseqs()) == 1
    assert ddc.valid_host(dev["host"]) and fake.deployments[dev["dseq"]]["limit_h"] == 8
    ddc.cmd_start(args)                                                # second start reuses the box
    assert len(fake.open_dseqs()) == 1 and posts(fake) == 1
    assert "Ready." in capsys.readouterr().out


# ------------------------------------------------------------------ `ddc install` never force-pushes over a foreign repo (SAF-11)
def _gh(monkeypatch, branches, manifest_ok=True, list_ok=True):
    def run(argv, **k):
        argv = list(argv)
        if argv[:2] == ["gh", "api"] and "/branches" in argv[2]:
            return types.SimpleNamespace(returncode=0 if list_ok else 1, stdout="\n".join(branches).encode(), stderr=b"HTTP 404")
        if argv[:2] == ["gh", "api"] and "manifest.json" in argv[2]:
            return types.SimpleNamespace(returncode=0 if manifest_ok else 1, stdout=b"", stderr=b"")
        raise AssertionError(argv)
    monkeypatch.setattr(ddc.subprocess, "run", run)


def test_install_refuses_an_existing_repo_that_is_not_a_ddc_state_repo(monkeypatch):
    _gh(monkeypatch, [])
    ddc.ensure_state_repo_is_ours("o/state")                                   # empty repo: fine
    _gh(monkeypatch, ["main", "prev"])
    ddc.ensure_state_repo_is_ours("o/state")                                   # an existing ddc state repo: fine
    for branches, manifest in ((["main", "develop"], True), (["master"], True), (["main"], False), (["main", "prev", "gh-pages"], True)):
        _gh(monkeypatch, branches, manifest_ok=manifest)
        with pytest.raises(ddc.DdcError, match="not a ddc state repo"):
            ddc.ensure_state_repo_is_ours("o/real-project")
    _gh(monkeypatch, [], list_ok=False)
    with pytest.raises(ddc.DdcError, match="cannot list the branches"):
        ddc.ensure_state_repo_is_ours("o/state")


def test_install_runs_the_repo_check_before_adding_any_deploy_key(monkeypatch, capsys):
    ddc.CFG.update(state_repo="o/real-project", repos=[], dirs=[])
    (ddc.SECRETS_DIR / "secrets.env").write_text("AKASH_API_KEY=k\nDDC_BACKUP_PASSPHRASE=p\n")
    calls = []

    def run(argv, **k):
        argv = list(argv)
        calls.append(argv)
        out = b"main\nfeature-x\n" if argv[:2] == ["gh", "api"] and "/branches" in argv[2] else b""
        return types.SimpleNamespace(returncode=0, stdout=out, stderr=b"")
    monkeypatch.setattr(ddc.subprocess, "run", run)
    assert ddc.main(["install", "--yes"]) == 1
    assert "not a ddc state repo" in capsys.readouterr().out
    assert not [c for c in calls if "deploy-key" in c or c[:3] == ["gh", "repo", "create"]]

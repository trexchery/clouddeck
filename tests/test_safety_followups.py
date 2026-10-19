"""Follow-up safety fixes: close_box never reads an empty answer as closed, stop / the watchdog work with a damaged state file (read-only
account listing, ddc-* names only), adopted records are never auto-closed, activity is re-checked before an idle close, ssh failures are
classified, the agent restart command really restarts, reconcile holds the controller lock, --force needs --discard-workspace,
restore.sh marks claude/config archives complete, and the small input checks."""
import json
import os
import subprocess
import sys
import time
import types

import pytest
from fake_akash import FAKE_OWNER

from clouddeck.engine import ddc

ARGS = types.SimpleNamespace(profile="32", workers=0, worker_profile=None)


def alerts():
    return " | ".join(ddc.recent_alerts(50))


@pytest.fixture
def fleet(fake):
    ddc.cmd_start(ARGS)
    ddc.cmd_scale(types.SimpleNamespace(n=2, profile="32"))
    fake.fleet = {"dev": ddc.dev_box()["dseq"], "workers": [w["dseq"] for w in ddc.workers()]}
    fake.agent.update(idle_min=3, active_reason="cpu=2.0cores")
    return fake


def ledger_closed(dseq):
    return [e for e in ddc.ledger_read()[0] if e.get("event") == "closed" and e.get("dseq") == dseq]


# ------------------------------------------------------------------ close_box: an empty answer is UNKNOWN, never "closed"
@pytest.mark.parametrize("body", [{}, {"data": None}, {"data": {}}, {"data": {"deployments": None}}, {"data": []}, {"data": {"deployments": "x"}}])
def test_a_spend_rate_without_a_deployments_list_is_unknown_not_nothing_is_billing(fake, monkeypatch, body):
    monkeypatch.setattr(ddc, "api", lambda *a, **k: body)
    assert ddc.billing_dseqs() is None
    monkeypatch.setattr(ddc, "api", lambda *a, **k: {"data": {"deployments": []}})
    assert ddc.billing_dseqs() == set()                                       # a well-formed empty list does mean nothing is billing


def test_close_box_with_empty_deployment_and_spend_rate_answers_does_not_record_closed(fake):
    box = ddc.create_box("worker", "32", 1, {}, limit_h=4)
    d = box["dseq"]
    fake.fail_next("DELETE /v1/deployments", "timeout_before")               # the first DELETE is never executed
    fake.fail_next("GET /v1/deployments/", "empty", 1000)
    fake.fail_next("GET /v1/spend-rate", "empty", 1000)
    with pytest.raises(ddc.DdcError, match="not confirmed closed"):
        ddc.close_box(d)
    assert ddc.load_state()["deps"][d]["status"] == "ready" and not ledger_closed(d)       # nothing was booked as closed
    assert len([c for c in fake.calls if c[0] == "DELETE"]) == 3              # and the DELETE was sent again (it executed on the 2nd try)


def test_close_box_accepts_a_well_formed_spend_rate_that_no_longer_lists_it_when_the_deployment_cannot_be_read(fake):
    box = ddc.create_box("worker", "32", 1, {}, limit_h=4)
    fake.fail_next("GET /v1/deployments/", "empty", 1000)                    # the details are unreadable, the spend-rate is fine
    ddc.close_box(box["dseq"])
    assert not fake.open_dseqs() and ddc.load_state()["deps"][box["dseq"]]["status"] == "closed" and ledger_closed(box["dseq"])


def test_an_odd_shaped_deployment_reply_is_unknown_not_a_crash(fake, monkeypatch):
    box = ddc.create_box("worker", "32", 1, {}, limit_h=4)
    for reply in (["rate limited"], {"data": "maintenance"}, {"data": {"deployment": "gone"}}):
        monkeypatch.setattr(ddc, "api", lambda *a, _r=reply, **k: _r)
        status, _ = ddc.deployment_status(box["dseq"])
        assert status == "unknown"
    monkeypatch.undo()
    assert ddc.read_json(ddc.STATE_DIR / "nonexistent.json", {}) == {}


def test_a_damaged_bookkeeping_file_is_replaced_not_fatal(fake):
    for raw in (b"\xff\xfe\x00bad", b"[]", b"null", b"{"):
        (ddc.STATE_DIR / "watchdog.json").write_bytes(raw)
        assert ddc.read_json(ddc.STATE_DIR / "watchdog.json", {}) == {}
        ddc.wd_update(lambda h: h.__setitem__("x", 1))                        # overwrites it with a good file
        assert ddc.read_json(ddc.STATE_DIR / "watchdog.json", {}) == {"x": 1}


def test_the_watchdog_still_enforces_with_a_damaged_watchdog_json(fleet):
    (ddc.STATE_DIR / "watchdog.json").write_bytes(b"\xff\xfe\x00bad")
    fleet.clock += 7.9 * 3600
    assert ddc.cmd_watchdog(None) == 0 and not fleet.open_dseqs()


# ------------------------------------------------------------------ a damaged state.json: stop and the watchdog fall back to the account listing
def damage_state():
    ddc.STATE_FILE.write_text("{ broken", encoding="utf-8")


def test_stop_only_works_from_the_account_listing_when_state_json_is_damaged(fleet, capsys):
    fleet.add_foreign("1000000000001", "prod-box")                            # a hand-made deployment on the same account
    w1, w2 = fleet.fleet["workers"]
    damage_state()
    assert ddc.main(["stop", "--only", w1, "--yes"]) == 0
    assert sorted(fleet.open_dseqs()) == sorted([fleet.fleet["dev"], w2, "1000000000001"])
    assert (ddc.STATE_DIR / "state.corrupt").exists() and not ddc.STATE_FILE.exists()      # nothing was written to the state file
    assert ledger_closed(w1)[0]["by"] == "fallback-listing"
    assert ddc.main(["stop", "--only", "1000000000001", "--yes"]) == 1       # a hand-made deployment is not even offered
    assert "matches no active lease" in capsys.readouterr().out and "1000000000001" in fleet.open_dseqs()


def test_stop_without_only_in_a_damaged_state_checkpoints_the_dev_box_then_closes_our_leases(fleet):
    fleet.add_foreign("1000000000001", "prod-box")
    damage_state()
    assert ddc.main(["stop", "--yes"]) == 0
    assert fleet.open_dseqs() == ["1000000000001"]
    assert any("checkpoint.sh final-stop" in c for h, c in fleet.ssh_log)


def test_a_failing_checkpoint_keeps_everything_in_the_fallback_too(fleet):
    damage_state()
    fleet.checkpoint = "fail"
    assert ddc.main(["stop", "--yes"]) == 1 and len(fleet.open_dseqs()) == 3


def test_fallback_never_touches_a_ddc_deployment_of_another_owner(fleet):
    good = ddc.load_state()                                                   # a good state exists, so state.json.bak holds our owner
    ddc.write_json(ddc.STATE_FILE, good)
    fleet.add_foreign("1000000000777", "ddc-w09-32-copycat", owner="akash1someoneelse")
    damage_state()
    ddc.main(["stop", "--yes"])
    assert fleet.open_dseqs() == ["1000000000777"]


def test_the_watchdog_closes_through_the_fallback_after_three_failures(fleet):
    damage_state()
    for _ in range(2):
        assert ddc.cmd_watchdog(None) == 1
    assert len(fleet.open_dseqs()) == 3 and "will NOT create leases" in alerts()
    assert ddc.cmd_watchdog(None) == 1                                       # 3rd failure: the safe stop (verified checkpoint) runs from the listing
    assert not fleet.open_dseqs() and any("final-stop" in c for h, c in fleet.ssh_log)


# ------------------------------------------------------------------ adopted records, never-ready dev boxes
def unleased_dev(fake, dseq="77", name="ddc-dev-32-10170000-ab12"):
    fake.deployments[dseq] = {"name": name, "owner": FAKE_OWNER, "state": "active", "t0": fake.clock, "provider": "akash1provA"}


def test_an_adopted_lease_is_never_closed_by_the_watchdog_or_start_and_stop_needs_discard_workspace(fake, capsys):
    unleased_dev(fake)
    assert ddc.main(["reconcile", "--adopt", "--yes"]) == 0
    rec = ddc.load_state()["deps"]["77"]
    assert rec["adopted"] and rec["status"] == "leased" and rec["role"] == "dev" and rec["max_usd_hr"] == 0.9
    assert ddc.budget_view()["committed"] > 0                                  # adopted spend is committed at the price cap, not at $0
    fake.clock += 3 * 3600
    assert ddc.cmd_watchdog(None) == 0 and fake.open_dseqs() == ["77"]        # not "stale unready"
    with pytest.raises(ddc.DdcError, match="adopted dev lease 77"):
        ddc.cmd_start(ARGS)
    assert fake.open_dseqs() == ["77"]
    assert ddc.main(["stop", "--only", "77", "--yes"]) == 1 and fake.open_dseqs() == ["77"]
    assert "--discard-workspace" in capsys.readouterr().out
    assert ddc.main(["stop", "--only", "77", "--discard-workspace", "--yes"]) == 0 and not fake.open_dseqs()


def test_adopt_lists_and_asks_first_and_only_filters(fake, monkeypatch, capsys):
    unleased_dev(fake, "77")
    unleased_dev(fake, "78", "ddc-w01-32-10170000-cd34")
    monkeypatch.setattr(ddc.sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
    assert ddc.main(["reconcile", "--adopt"]) == 1 and "pass --yes" in capsys.readouterr().out and not ddc.load_state()["deps"]
    assert ddc.main(["reconcile", "--adopt", "--only", "78", "--yes"]) == 0
    assert list(ddc.load_state()["deps"]) == ["78"]


def test_a_never_ready_dev_lease_of_ours_gets_a_checkpoint_attempt_before_the_watchdog_closes_it(fake):
    box = ddc.create_box("dev", "32", 0, {}, limit_h=8)
    ddc.update_dep(box["dseq"], status="leased", created_at=fake.clock)
    fake.clock += 20 * 60
    ddc.cmd_watchdog(None)
    assert fake.open_dseqs() == [box["dseq"]]                                  # 30 minutes, not 15
    fake.clock += 11 * 60
    fake.ssh_log.clear()
    ddc.cmd_watchdog(None)
    assert not fake.open_dseqs() and any("checkpoint.sh final-stop" in c for h, c in fake.ssh_log)


def test_the_watchdog_leaves_a_lease_alone_while_another_command_holds_the_lock(fake, monkeypatch):
    box = ddc.create_box("worker", "32", 1, {}, limit_h=4)
    ddc.update_dep(box["dseq"], status="leased", created_at=fake.clock)
    fake.clock += 40 * 60
    real = ddc.FileLock
    monkeypatch.setattr(ddc, "FileLock", lambda name="ddc.lock", wait_s=900: real(name, min(wait_s, 0.2)))
    calls = []
    orig = ddc._close_unready
    monkeypatch.setattr(ddc, "_close_unready", lambda *a, **k: calls.append(a) or orig(*a, **k))
    with real(wait_s=1):                                                       # a slow `start` holds the lock: the pass itself is skipped
        assert ddc.cmd_watchdog(None) == 0
    assert not calls and fake.open_dseqs() == [box["dseq"]]


# ------------------------------------------------------------------ user returns during the final checkpoint
def test_an_idle_stop_is_aborted_when_the_user_comes_back_during_the_final_checkpoint(fleet):
    fleet.agent.update(shutdown_requested="idle", active_reason="")
    real = fleet.ssh

    def returns(d, cmd, **k):
        out = real(d, cmd, **k)
        if "checkpoint.sh" in cmd:
            fleet.agent.update(active_reason="tty", shutdown_requested="")   # the agent withdrew the request: someone is typing again
        return out
    fleet.mp.setattr(ddc, "ssh", returns)
    assert ddc.cmd_watchdog(None) == 1
    assert len(fleet.open_dseqs()) == 3                                       # nothing closed
    assert "activity resumed during the final checkpoint" in alerts()


def test_a_manual_stop_does_not_recheck_activity(fleet):
    fleet.agent.update(active_reason="tty", shutdown_requested="")
    assert ddc.main(["stop", "--yes"]) == 0 and not fleet.open_dseqs()


# ------------------------------------------------------------------ ssh failures: only connect / timeout means "unreachable"
@pytest.mark.parametrize("msg,unreachable", [
    ("ssh dev@h: `cat` exit 255: ssh: connect to host h port 22: Connection timed out", True),
    ("ssh dev@h: `cat` exit 255: ssh: connect to host h port 22: Connection refused", True),
    ("ssh dev@h: `cat` exit 255: ssh: connect to host h port 22: No route to host", True),
    ("ssh dev@h: `cat` exit 255: ssh: Could not resolve hostname h: Name or service not known", True),
    ("ssh dev@h: `cat` exit 255: Load key \"C:\\\\keys\\\\access\": No such file or directory\ndev@h: Permission denied (publickey).", False),
    ("ssh dev@h: `cat` exit 255: dev@h: Permission denied (publickey).", False),
    ("ssh dev@h: `cat` exit 255: Host key verification failed.", False),
    ("ssh dev@h: `cat` exit 255: WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!", False),
    ("ssh dev@h: `cat` exit 1: cat: /run/ddc/state.json: No such file or directory", False),
])
def test_ssh_failures_are_classified(msg, unreachable):
    assert ddc.ssh_unreachable(ddc.DdcError(msg)) is unreachable
    assert ddc.ssh_unreachable(subprocess.TimeoutExpired("ssh", 40)) is True
    assert ddc.ssh_unreachable(FileNotFoundError("ssh")) is False             # no ssh binary on this PC
    assert ddc.ssh_unreachable(ValueError("not json")) is False


def test_a_local_key_or_auth_error_never_force_closes_a_healthy_dev_box(fleet):
    real = fleet.ssh

    def broken(d, cmd, **k):
        raise ddc.DdcError("ssh dev@h: `cat /run/ddc/state.json` exit 255: Load key \"x\": No such file or directory\ndev@h: Permission denied (publickey).")
    fleet.mp.setattr(ddc, "ssh", broken)
    for _ in range(8):                                                        # 40 minutes of 5-minute passes
        assert ddc.cmd_watchdog(None) == 1
        fleet.clock += 300
    fleet.mp.setattr(ddc, "ssh", real)
    assert len(fleet.open_dseqs()) == 3 and "unreachable for 30 min" not in alerts()
    assert "not 'unreachable'" in alerts() and ddc.load_state().get("dev_unreachable_since") is None


# ------------------------------------------------------------------ the stale-agent restart command
def test_the_restart_command_never_contains_the_literal_path_it_searches_for():
    cmd = ddc.agent_restart_command()
    assert "/opt/ddc/agent.sh" not in cmd and "[/]opt/ddc/agent" in cmd and "pgrep" in cmd and '"$d/agent.sh"' in cmd


@pytest.mark.skipif(os.name == "nt", reason="needs a POSIX shell with pgrep")
@pytest.mark.parametrize("shell", ["bash", "sh"])
def test_the_restart_command_starts_a_missing_agent_and_leaves_a_running_one_alone(tmp_path, shell):
    d = tmp_path / "ddc-lease"
    d.mkdir()
    marker = tmp_path / "started"
    (d / "agent.sh").write_text(f"#!/bin/sh\necho started >> {marker}\nsleep 20\n", newline="\n")
    (d / "agent.sh").chmod(0o755)
    cmd = ddc.agent_restart_command(str(d), str(tmp_path / "agent.log"))
    procs = []
    try:
        subprocess.run([shell, "-c", cmd], check=True, timeout=30)             # no agent: it must be started (the bug: pgrep matched its own shell)
        for _ in range(50):
            if marker.exists():
                break
            time.sleep(0.1)
        assert marker.read_text().count("started") == 1
        subprocess.run([shell, "-c", cmd], check=True, timeout=30)             # running now: not started a second time
        time.sleep(1)
        assert marker.read_text().count("started") == 1
    finally:
        subprocess.run(["pkill", "-f", str(d / "agent.sh")], capture_output=True)
        for p in procs:
            p.kill()


def test_a_dead_agent_restart_uses_that_command(fleet):
    fleet.ssh_log.clear()
    ddc.ssh(ddc.dev_box(), ddc.agent_restart_command(), check=False)
    assert fleet.agent_restarts == 1


# ------------------------------------------------------------------ reconcile holds the controller lock
def test_reconcile_waits_for_the_lock_so_it_cannot_close_a_lease_that_start_is_provisioning(fake, monkeypatch, capsys):
    ddc.update_state(lambda st: st.setdefault("pending", {}).__setitem__("ddc-w01-32-x", {"role": "worker", "profile": "32", "idx": 1, "limit_h": 4,
                                                                                         "max_usd_hr": 0.9, "created_at": fake.clock}))
    fake.deployments["500"] = {"name": "ddc-w01-32-x", "owner": FAKE_OWNER, "state": "active", "t0": fake.clock, "leased": True, "provider": "akash1provA"}
    real = ddc.FileLock
    monkeypatch.setattr(ddc, "FileLock", lambda name="ddc.lock", wait_s=900: real(name, min(wait_s, 0.2)))
    with real(wait_s=1):                                                       # `ddc start` is between its write-ahead record and its promote
        assert ddc.main(["reconcile"]) == 1
    assert "holds" in capsys.readouterr().out and fake.open_dseqs() == ["500"] and "ddc-w01-32-x" in ddc.pending_creations()
    assert ddc.main(["reconcile"]) == 0 and not fake.open_dseqs()             # lock free: the recovered lease is closed (its create reply was lost)


def test_the_first_reconcile_after_state_json_was_deleted_works(fleet):
    ddc.STATE_FILE.unlink()
    assert ddc.main(["reconcile", "--adopt", "--yes"]) == 0                   # used to fail once with StateCorrupt before the flag existed
    assert len(ddc.load_state()["deps"]) == 3


# ------------------------------------------------------------------ SAF-19: --force needs --discard-workspace
def test_final_checkpoint_force_without_discard_is_refused_and_with_discard_leaves_an_audit_line(fleet):
    fleet.checkpoint = "fail"
    d = ddc.dev_box()
    with pytest.raises(ddc.DiscardRefused, match="--discard-workspace"):
        ddc.final_checkpoint(d, force=True)
    ddc.final_checkpoint(d, force=True, discard_workspace=True)
    assert "AUDIT FORCED CLOSE" in (ddc.STATE_DIR / "ddc.log").read_text(encoding="utf-8")


def test_a_failing_idle_stop_alerts_once_an_hour_not_every_pass(fleet, monkeypatch):
    notified = []
    monkeypatch.setattr(ddc, "_notify", notified.append)
    fleet.agent.update(shutdown_requested="idle", active_reason="")
    fleet.checkpoint = "fail"
    for _ in range(12):                                                       # an hour of 5-minute passes
        ddc.cmd_watchdog(None)
        fleet.clock += 300
    assert len([m for m in notified if m.startswith("stop aborted, dev box left RUNNING")]) == 1 and len(notified) <= 4


def test_the_checkpoint_must_contain_an_archive_for_every_live_workspace(fake):
    ddc.CFG["repos"] = [{"name": "web-app", "path": str(ddc.STATE_DIR), "github": "o/web-app", "push": False}]
    ddc.CFG["dirs"] = [{"name": "notes", "path": str(ddc.STATE_DIR)}]
    ddc.cmd_start(ARGS)
    d = ddc.dev_box()
    ddc.final_checkpoint(d)                                                    # git repo + plain dir: both archived
    fake.drop_archives.add("notes")
    with pytest.raises(ddc.DdcError, match="no archive for /work/notes"):
        ddc.final_checkpoint(d)


def test_cmd_checkpoint_reports_a_skipped_checkpoint_as_an_error(fleet, monkeypatch):
    monkeypatch.setattr(ddc, "run_checkpoint", lambda d, reason, allow_shrink=False: "checkpoint_skip disabled (no state repo configured)")
    with pytest.raises(ddc.DdcError, match="no checkpoint was taken"):
        ddc.cmd_checkpoint(None)


# ------------------------------------------------------------------ small input checks
def test_start_refuses_an_impossible_worker_count_before_the_dev_box_exists(fake):
    ddc.CFG["max_workers"] = 2
    with pytest.raises(ddc.DdcError, match="max_workers = 2"):
        ddc.cmd_start(types.SimpleNamespace(profile="32", workers=5, worker_profile=None))
    with pytest.raises(ddc.DdcError, match="cannot scale to 50"):
        ddc.cmd_start(types.SimpleNamespace(profile="32", workers=50, worker_profile=None))
    ddc.CFG["max_workers"], ddc.CFG["max_total_leases"] = 4, 3
    with pytest.raises(ddc.DdcError, match="max_total_leases = 3"):
        ddc.cmd_start(types.SimpleNamespace(profile="32", workers=3, worker_profile=None))
    assert not fake.open_dseqs() and not fake.calls


@pytest.mark.parametrize("hours", [-5, 0, 0.5, "4", None, float("nan")])
def test_runtime_limits_below_one_hour_are_refused(fake, hours):
    with pytest.raises(ddc.DdcError):
        ddc.check_budget("32", 1, hours, role="worker")
    if hours is not None:
        with pytest.raises(ddc.DdcError):
            ddc.create_box("worker", "32", 1, limit_h=hours)
    assert not [c for c in fake.calls if c[0] == "POST"]


@pytest.mark.parametrize("price", [1e-9, 0.0001, 0.0005])
def test_a_price_cap_that_renders_as_zero_is_rejected_up_front(price):
    ddc.CFG["profiles"]["tiny"] = {"cpu": 1, "mem": "1Gi", "disk": "1Gi", "max_usd_hr": price}
    assert any("too small" in p for p in ddc.config_problems(profile="tiny"))
    ddc.CFG["profiles"]["tiny"]["max_usd_hr"] = 0.001
    assert ddc.config_problems(profile="tiny") == []


def test_a_crontab_that_cannot_be_read_is_never_overwritten(monkeypatch, tmp_path):
    written = []

    def run(argv, **k):
        if argv[:2] == ["crontab", "-l"]:
            return subprocess.CompletedProcess(argv, 1, b"", b"crontab: temporary failure")
        written.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")
    monkeypatch.setattr(ddc.subprocess, "run", run)
    monkeypatch.setattr(ddc, "PLATFORM", "linux")
    monkeypatch.setattr(ddc, "WIN", False)
    monkeypatch.setattr(ddc, "_systemd_user", lambda: False)
    monkeypatch.setattr(ddc.shutil, "which", lambda x: f"/usr/bin/{x}" if x == "crontab" else None)
    with pytest.raises(ddc.DdcError, match="leaving your crontab untouched"):
        ddc.install_watchdog()
    assert not written and ddc.watchdog_installed() is None


def test_the_old_crontab_is_copied_aside_before_it_is_changed(monkeypatch):
    state = {"text": "MAILTO=me\n0 3 * * * backup.sh\n"}

    def run(argv, **k):
        if argv[:2] == ["crontab", "-l"]:
            return subprocess.CompletedProcess(argv, 0, state["text"].encode(), b"")
        state["text"] = k["input"].decode()
        return subprocess.CompletedProcess(argv, 0, b"", b"")
    monkeypatch.setattr(ddc.subprocess, "run", run)
    monkeypatch.setattr(ddc, "PLATFORM", "linux")
    monkeypatch.setattr(ddc, "WIN", False)
    monkeypatch.setattr(ddc, "_systemd_user", lambda: False)
    monkeypatch.setattr(ddc.shutil, "which", lambda x: f"/usr/bin/{x}" if x == "crontab" else None)
    assert ddc.install_watchdog() == "cron"
    backups = list(ddc.STATE_DIR.glob("crontab.backup-*"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == "MAILTO=me\n0 3 * * * backup.sh\n"


def test_macos_install_says_loudly_that_no_watchdog_was_installed(monkeypatch, capsys):
    ddc.CFG["state_repo"], ddc.CFG["repos"], ddc.CFG["dirs"] = "", [], []
    (ddc.SECRETS_DIR / "secrets.env").write_text("AKASH_API_KEY=k\nDDC_BACKUP_PASSPHRASE=p\n")
    for k in ("access", "worker"):
        (ddc.KEYS / k).write_text(k)
    (ddc.KEYS / "gitconfig").write_text("")
    monkeypatch.setattr(ddc, "PLATFORM", "darwin")
    monkeypatch.setattr(ddc, "WIN", False)
    monkeypatch.setattr(ddc.subprocess, "run", lambda argv, **k: subprocess.CompletedProcess(argv, 0, json.dumps({"ssh_keys": []}).encode(), b""))
    monkeypatch.setattr(ddc, "setup_alerts", lambda: None)
    assert ddc.main(["install", "--yes"]) == 0
    assert "NO watchdog is registered" in capsys.readouterr().out


def test_running_test_ddc_py_directly_ignores_a_real_state_dir_from_the_environment(tmp_path):
    """`python test_ddc.py` (no pytest, so no conftest redirection) must use its own temp dirs even when the environment names a state dir."""
    real = tmp_path / "real-state"                                            # stands for a user's live state dir
    env = {**os.environ, "DDC_STATE_DIR": str(real), "DDC_SECRETS_DIR": str(real), "PYTHONDONTWRITEBYTECODE": "1"}
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cp = subprocess.run([sys.executable, os.path.join(root, "test_ddc.py")], env=env, cwd=str(tmp_path), capture_output=True, text=True, timeout=120)
    assert cp.returncode == 0, cp.stderr
    assert not real.exists()
